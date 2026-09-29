# searxng-lazy-results

A [SearXNG](https://github.com/searxng/searxng) plugin that renders the results of the fast engines
immediately and streams in the results of the slow engines while you are already reading.

```
without the plugin                          with searxng-lazy-results
--------------------------------------      --------------------------------------
0s  query sent                              0s  query sent
    |                                           |
    |    google  ▓▓ (0.4s)                      |    google  ▓▓ (0.4s)
    |    bing    ▓▓▓▓▓▓▓▓ (2.4s)                |    bing    ▓▓▓▓▓▓ (cut at 1.2s → streaming)
    |    brave   ▓▓▓▓▓▓▓▓▓▓▓▓ (4.1s)            |    brave   ▓▓▓▓▓▓▓▓▓ (cut at 1.2s → streaming)
    v                                           v
4.1s  blank page, "searching"               1.2s  page with google's results
                                                ── "Still loading results from slow engines: bing, brave"
                                             1.8s  bing's results fade in
                                             4.3s  brave's results fade in, the notice disappears
```

SearXNG renders the result page only when every selected engine has answered (or its deadline
expired), so a single slow engine keeps you in front of a blank page for seconds. This plugin splits
that wait into a *first paint* and a *second pass*: engines that did not make the first paint are
asked again in the background and appended to the page as soon as they answer.

## Measured behaviour

Taken from `python tests/run_e2e.py --manage` (SearXNG 2026.9.29, three engines with 0.2 s / 1.2 s /
4.0 s latency, first paint budget 1.0 s):

| what | measured |
| --- | --- |
| first page with a 4 s engine | `200` after **1.06 s**, contains the 0.2 s engine's results, none of the slow ones |
| streamed results | 1.2 s engine delivered at **+1.21 s**, 4.0 s engine delivered at **+4.00 s** (one event per engine, no buffering) |
| `format=json` search | **4.02 s**, all three engines' results -- API clients are untouched |
| search without slow engines | 0.22 s and *no* plugin markup is injected at all |
| hostile engine title | escaped in the streamed fragment (`&lt;img …`) exactly like on the first page |

## Install

### Container (SearXNG docker image)

```yaml
services:
  searxng:
    image: searxng/searxng:latest
    environment:
      - SEARXNG_BASE_URL=http://localhost:8888/
      - SEARXNG_SECRET=change-me
    volumes:
      - ./settings.yml:/etc/searxng/settings.yml:ro
      - ./searxng-lazy-results:/src:ro     # this repository
      - searxng-data:/var/cache/searxng
    environment:
      PYTHONPATH: /src
```

`PYTHONPATH=/src` makes the package importable; alternatively install it into the image with
`pip install /src` (or `pip install git+https://github.com/Zcc09/searxng-lazy-results`).

### Plain installation

```bash
pip install git+https://github.com/Zcc09/searxng-lazy-results
```

### Enable it in `settings.yml`

```yaml
plugins:
  searxng_lazy_results.plugin.SXNGPlugin:
    active: true
```

The plugin shows up in *Preferences → UI* like any other plugin, so every user can switch it off.

## Configuration

Optional section in `settings.yml` (all keys have sane defaults):

```yaml
lazy_results:
  first_paint_timeout: 1.2      # seconds the first page waits for the engines
  stream_timeout: 8.0           # budget of the second pass, per engine
  stream_grace: 3.0             # extra seconds the endpoint stays open afterwards
  max_concurrent_engines: 4     # engines asked at the same time by the second pass
  max_stream_engines: 12        # upper bound of engines streamed for one search
  heartbeat: 10.0               # seconds of silence before a keep-alive comment
  engine_labels: true           # "Results from <engine>" label above every streamed batch
  deduplicate: true             # drop streamed results that are already on the page
  require_token: true           # only accept signed streaming requests
  secret: null                  # explicit HMAC secret (default: server.secret_key)
```

Every key can also be set with an environment variable, which wins over `settings.yml` -- handy for
containers:

```yaml
environment:
  SEARXNG_LAZY_RESULTS_FIRST_PAINT_TIMEOUT: "2.0"
  SEARXNG_LAZY_RESULTS_ENABLED: "false"
```

| option | environment variable |
| --- | --- |
| `enabled` | `SEARXNG_LAZY_RESULTS_ENABLED` |
| `first_paint_timeout` | `SEARXNG_LAZY_RESULTS_FIRST_PAINT_TIMEOUT` |
| `stream_timeout` | `SEARXNG_LAZY_RESULTS_STREAM_TIMEOUT` |
| `stream_grace` | `SEARXNG_LAZY_RESULTS_STREAM_GRACE` |
| `max_concurrent_engines` | `SEARXNG_LAZY_RESULTS_MAX_CONCURRENT_ENGINES` |
| `max_stream_engines` | `SEARXNG_LAZY_RESULTS_MAX_STREAM_ENGINES` |
| `heartbeat` | `SEARXNG_LAZY_RESULTS_HEARTBEAT` |
| `engine_labels` | `SEARXNG_LAZY_RESULTS_ENGINE_LABELS` |
| `deduplicate` | `SEARXNG_LAZY_RESULTS_DEDUPLICATE` |
| `require_token` | `SEARXNG_LAZY_RESULTS_REQUIRE_TOKEN` |
| `secret` | `SEARXNG_LAZY_RESULTS_SECRET` |

Picking `first_paint_timeout`: it is the trade-off between "how fast does the page appear" and "how
many engines end up in the second pass". `1.0`–`1.5` works well for the common engines; `0` turns the
early first paint off (the plugin then only streams engines that timed out for other reasons).

## How it works

1. **First paint** (`pre_search`) -- for an HTML search the plugin sets
   `SearchQuery.timeout_limit` to `first_paint_timeout`. SearXNG's request handling cuts every engine
   off at that deadline; the page is rendered with everything that arrived. Engines that were cut off
   are reported as `timeout`, exactly like a normal timeout.
2. **The page carries the state** (`post_search`) -- the engines that timed out are put into a signed
   payload together with the query, the language, safe-search, page number, time range and SearXNG's
   `engine_data`. The payload is injected as
   `<script id="lazy-results-config" type="application/json">` plus two `<script>`/`<link>` tags for
   the client. **Nothing is injected when no engine has to be streamed.**
3. **Second pass** (the `EventSource` in `lazy-results.js` calls `/lazy-results/stream`) -- the plugin
   validates the signature, checks the engines against the instance (existing, not suspended for
   another reason, engine token allowed) and searches **one engine per `Search`**, several at a time.
   Every engine's results are rendered with the templates of the instance's own theme and sent as one
   `text/event-stream` event *the moment they arrive*.
4. **The client** appends each batch to `#urls`, with a small "Results from <engine>" label, drops
   results whose URL is already on the page, hides the "no results" message while results are still
   coming in and updates the *Messages from the search engines* table in the sidebar (the engine that
   timed out is now marked as loaded instead of showing a stale `timeout`).

Nothing else changes: engines keep their own timeouts, the limiter, the preferences, the enabled
engines and the JSON/RSS/CSV APIs behave as before.

### The streaming protocol

`GET /lazy-results/stream?payload=<base64url>&sig=<hmac-sha256-hex>`
-- `payload` is compact JSON signed with `server.secret_key` (or `lazy_results.secret`).

```
: searxng-lazy-results
retry: 600000

event: engine
data: {"engine": "bing", "category": "general", "status": "ok", "elapsed": 1.84, "count": 12, "html": "<article …>"}

event: engine
data: {"engine": "brave", "status": "error", "elapsed": 8.0, "count": 0, "error": "timeout"}

event: done
data: {"engines": 2, "results": 12, "elapsed": 4.31, "truncated": false}
```

`status` is one of `ok` (results in `html`), `empty` (engine answered, nothing to offer), `error`
(engine did not answer, `error` carries the reason) or `skipped` (engine not asked: unknown,
suspended, token not allowed). `html` is the rendered fragment; `: keepalive` comments keep idle
proxies from closing the connection.

## Reverse proxies

The endpoint is a `text/event-stream` and must not be buffered. It already sends
`X-Accel-Buffering: no`, so nginx needs no extra configuration -- if you proxy through something that
buffers regardless (some CDNs), add `proxy_buffering off;` for `^~ /lazy-results/`.

## Requirements and compatibility

* SearXNG with the plugin API and the search stack this plugin hooks into: `Plugin`/`PluginInfo`,
  `Plugin.init(app)`, the `pre_search`/`post_search` hooks, `searx.search.SearchWithPlugins`,
  `searx.search.models.SearchQuery`, `searx.webapp.render`, `searx.webapp.highlight_content`.
  Developed and tested against **SearXNG 2026.9.29+f503587** (the state of `master` at that date).
* Python 3.11+ (whatever the SearXNG installation uses). The plugin itself has **no dependencies**
  beyond SearXNG.
* If SearXNG's internals ever change, `init()` logs the reason and returns `False`: the plugin
  deactivates itself instead of breaking the instance.

## Tests

```bash
pip install -e .[dev]
python tests/run_e2e.py --manage          # starts mock engines + SearXNG on 8899 and asserts everything
SEARXNG_E2E=1 python -m pytest tests -q   # unit tests (signing, config, stream helpers)
```

`tests/run_e2e.py` is the interesting one: it runs three mock engines with 0.2 s / 1.2 s / 4.0 s
latency and asserts the first paint, the arrival *times* of the streamed engines, the untouched JSON
API, the missing injection for fast searches, the signature checks, escaping of hostile engine output
and the recovery of an engine that SearXNG suspended because of the first paint. Add
`--base-url http://…` to run it against an already running instance (that is what the CI job does).

## Notes and limitations

* Engines that timed out are asked **twice** (once in the first paint, once in the second pass). That
  is the price of the early page; the second pass is capped by `max_stream_engines` and
  `stream_timeout`.
* An engine that SearXNG suspended because the first paint cut its request off is resumed for the
  second pass (that suspension is an artefact of the plugin, not an engine failure). Suspensions for
  other reasons -- rate limit, CAPTCHA, access denied -- are respected and reported as `skipped`.
* Streamed results are appended to the page; they are not re-sorted into the ranking of the first
  page (only the first page's results are scored against each other). The engine's own result order is
  kept inside a batch.
* The streaming endpoint only accepts payloads this instance signed while rendering a result page, so
  it cannot be used as a JSON API that bypasses `search.formats`. Instances that keep `server.secret_key`
  unset get a static fallback secret and a warning in the log -- set `server.secret_key` (or
  `lazy_results.secret`) on public instances.

## License

AGPL-3.0-or-later -- same license as SearXNG. See [LICENSE](LICENSE).
