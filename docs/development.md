# Development notes

## Layout

```
searxng_lazy_results/
  plugin.py                 the SearXNG plugin: hooks, routes, injection
  config.py                 options from settings.yml / SEARXNG_LAZY_RESULTS_*
  signing.py                signed payloads for the streaming endpoint
  stream.py                 the second pass (one Search per engine)
  fragments.py              rendering streamed results with the instance's theme
  static/lazy-results.js    the client (EventSource, DOM, dedupe, sidebar)
  static/lazy-results.css   theme neutral styles
  templates/lazy_results_fragment.html
tests/
  mock_engines.py           mock search backends with configurable latency
  settings.test.yml         a SearXNG instance with nothing but the mock engines
  run_e2e.py                the end to end test (starts both if run with --manage)
```

## Running everything locally

```bash
pip install -e .[dev]

# SearXNG from the repository (the plugin imports it, it does not ship it)
git clone --depth 1 https://github.com/searxng/searxng /tmp/searxng
pip install -r /tmp/searxng/requirements.txt
pip install -e /tmp/searxng

python tests/run_e2e.py --manage           # mock engines on 8770, SearXNG on 8899
```

`tests/run_e2e.py` starts both processes, waits for their health endpoints, runs the checks and
terminates them again. Against an instance that is already running:

```bash
SEARXNG_SETTINGS_PATH=$PWD/tests/settings.test.yml python -m flask --app searx.webapp run --port 8899 &
python tests/run_e2e.py --base-url http://127.0.0.1:8899
```

## SearXNG internals this plugin relies on

| what | why |
| --- | --- |
| `Plugin.init(app)` | to register the two routes and the `after_request` hook |
| `Plugin.pre_search` / `post_search` | first paint budget; collecting the engines that timed out |
| `SearchQuery.timeout_limit` | the first paint deadline (`Search._get_requests`) |
| `searx.search.SearchWithPlugins` | the second pass uses the very same search stack |
| `searx.webutils.get_translated_errors` | the user facing reason an engine failed |
| `searx.webapp.render` + `get_result_template` | streamed results are rendered by the instance's theme |
| `searx.webapp.highlight_content` | titles/contents are escaped and highlighted like on the first page |
| `searx.search.processors.PROCESSORS[...].suspended_status` | lifting the suspension the first paint caused |

`plugin._missing_internals()` checks all of them when the plugin is initialized; if anything is gone,
`init()` logs the reason and the plugin stays off instead of breaking the instance.

## Local development on Windows

SearXNG is a Unix application, so a *local* Windows dev instance needs two shims that are **not**
part of this plugin and **not** needed for the Linux deployment:

1. `searx/valkeydb.py` imports the Unix-only `pwd` module at import time. A one file `pwd.py`
   stand-in in `site-packages` is enough (it is only used in an error branch).
2. `searx/webutils.get_result_templates()` builds its set of result templates with
   `os.path.join()`, which yields backslashes on Windows, so `get_result_template('simple', …)`
   never matches and the result page fails with `TemplateNotFound`. `.replace(os.sep, '/')` in that
   function fixes it.

Both shims only affect the local dev instance; the container runs (CI and the example compose) use
the untouched image.

## Testing notes

* The mock backends give the tests a deterministic latency profile (0.2 s / 1.2 s / 4.0 s) so
  "the fast engine is rendered before the slow one" is a measurement, not a hope. They are also
  pointed at by `examples/settings.yml`, which is what makes the demo visible without the internet.
* `tests/run_e2e.py` reads the SSE stream through `response.raw.read1()`: `requests`' `iter_lines()`
  buffers, which would make every event look as if it had been delivered at the end of the stream and
  hide exactly the property under test.
* Plain `http://` engines need `enable_http: true` in their settings, otherwise the request fails with
  `curl: (1) Protocol "http" is disabled`. The mock engines set it.
