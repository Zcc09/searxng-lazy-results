# searxng-lazy-results — Docker example

A runnable example of the **searxng-lazy-results** plugin: SearXNG renders the
results of the fast engines immediately and streams in the results of the
slow ones afterwards (via an SSE endpoint, `text/event-stream`).

Two dedicated demo engines (`demo-slow`, `demo-medium`) exist to make the
effect visible **offline**: they are `json_engine` engines pointing at a local
mock service with *known* latencies, so the slow results stream in a couple
of seconds later instead of waiting for the internet:

| engine          | endpoint                            | latency |
| --------------- | ----------------------------------- | ------- |
| `demo-medium`   | `http://mock-engines:8770/mock/medium?q={query}` | ~1.2 s  |
| `demo-slow`     | `http://mock-engines:8770/mock/slow?q={query}`   | ~4.0 s  |

With `first_paint_timeout: 1.2`, the page paints with whatever answered in
time (the default engines that were fast enough), and the mock engines that
missed the budget are streamed in a few seconds later — labelled per engine.

## Files

- `docker-compose.yml` — the two services below, on a user defined network
  `lazy` so `http://mock-engines:8770/...` resolves inside the stack.
- `settings.yml` — a real SearXNG settings file (`use_default_settings: true`,
  the default engine list is kept; only the two demo engines are *added*):
  activates the plugin, configures the `lazy_results` section, adds
  `demo-slow` / `demo-medium`.

## The services

- **`searxng`** — `searxng/searxng:latest`, listening on 8080
  (`SEARXNG_PORT=8080`), published as `8888:80`. The repository root is
  mounted read-only at `/lazy-results-src` and `PYTHONPATH=/lazy-results-src`
  makes `searxng_lazy_results` importable by the container; the settings file
  is mounted at `/etc/searxng/settings.yml` (ro) and selected with
  `SEARXNG_SETTINGS_PATH`.
- **`mock-engines`** — `python:3.12-alpine` running
  `tests/mock_engines.py` (stdlib only, no pip installs) on port 8770 with
  `--delay fast=0.2 --delay medium=1.2 --delay slow=4.0`.

## Run it

```console
cd examples
docker compose up
```

Then open <http://localhost:8888> and search for anything (e.g. `q=demo`).
The first paint happens after ~1.2 s with the engines that answered in time;
the mock engines' results appear a moment later with a
"Results from \<engine\>" label. `http://localhost:8888/search?q=demo&format=json`
is untouched (API formats are never modified by the plugin).

## Plugin configuration

The plugin reads a `lazy_results` section in `settings.yml` (or environment
variables, which win). Every key has an env override named
`SEARXNG_LAZY_RESULTS_<KEY>` (upper case), e.g.
`SEARXNG_LAZY_RESULTS_FIRST_PAINT_TIMEOUT`.

| key                      | type          | default | meaning                                                            |
| ------------------------ | ------------- | ------- | ------------------------------------------------------------------ |
| `enabled`                | bool          | `true`  | master switch; `false` disables first paint and streaming          |
| `first_paint_timeout`    | float (s)     | `1.2`   | seconds the first page waits for engines; `0` disables early paint |
| `stream_timeout`         | float (s)     | `8.0`   | per engine budget of the second (streaming) pass                   |
| `stream_grace`           | float (s)     | `3.0`   | extra seconds the stream stays open for engines finishing late     |
| `max_concurrent_engines` | int           | `4`     | engines asked at the same time by the streaming endpoint           |
| `max_stream_engines`     | int           | `12`    | upper bound of engines streamed for one search                     |
| `heartbeat`              | float (s)     | `10.0`  | silence before a keep-alive comment is sent to the browser         |
| `engine_labels`          | bool          | `true`  | print a "Results from \<engine\>" label above each streamed batch  |
| `deduplicate`            | bool          | `true`  | drop streamed results whose URL is already on the page             |
| `require_token`          | bool          | `true`  | only accept streaming requests with a valid signed payload         |
| `secret`                 | string \| null| `null`  | optional explicit HMAC secret (defaults to `server.secret_key`)    |

Example of the env override form in `docker-compose.yml`:

```yaml
    environment:
      SEARXNG_LAZY_RESULTS_FIRST_PAINT_TIMEOUT: "1.5"
```

## Behaviour and deployment notes

- The plugin only injects itself into an **HTML `/search` result page** and
  only when at least one engine timed out. A search that is answered by every
  engine in time renders exactly as usual. API formats (`json`, `rss`, `csv`)
  are never touched.
- The streaming endpoint is a long-lived SSE response. Reverse proxies must
  **not buffer it**: nginx needs `proxy_buffering off;` (SearXNG already sends
  `X-Accel-Buffering: no` on the stream, which nginx honours).
- With `require_token: true` (default), the client must present a signed
  payload to open the stream — the signature is derived from
  `server.secret_key`, so the endpoint cannot be used as an unauthenticated
  search API by clients that never rendered a result page.
