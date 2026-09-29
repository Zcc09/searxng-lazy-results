# SPDX-License-Identifier: AGPL-3.0-or-later
"""The lazy-results plugin of SearXNG.

The plugin hooks into the search pipeline of SearXNG:

:hook:`pre_search`
    gives the engines of a HTML search request a *first paint* budget: the page
    is rendered as soon as the fast engines answered.  Engines that are cut off
    are reported as ``timeout`` -- and streamed in afterwards.

:hook:`post_search`
    collects those engines, renders the configuration of the client and hands it
    to :py:meth:`SXNGPlugin.inject_assets`.

:hook:`after_request` (registered in :py:meth:`SXNGPlugin.init`)
    injects the client script into the rendered result page.  Nothing is
    injected when no engine has to be streamed, hence a search that is answered
    by every engine in time does not change at all.
"""

from __future__ import annotations

import json
import logging
import random
import typing as t
from timeit import default_timer
from urllib.parse import urlencode

from flask import Response, g, has_request_context, request, send_from_directory, stream_with_context
from flask_babel import gettext

from searx.plugins import Plugin, PluginInfo

from . import __version__, fragments, signing, stream
from .config import LazyResultsConfig, load_config

log = logging.getLogger("searx.plugins.lazy_results")

PLUGIN_ID = "lazyResults"
PAYLOAD_VERSION = 1

STREAM_RULE = "/lazy-results/stream"
STATIC_RULE = "/lazy-results/static/<path:filename>"

CLIENT_JS = "lazy-results.js"
CLIENT_CSS = "lazy-results.css"
CLIENT_CONFIG_ID = "lazy-results-config"

ASSETS = {
    CLIENT_JS: "text/javascript; charset=utf-8",
    CLIENT_CSS: "text/css; charset=utf-8",
}

_G_STATE = "lazy_results_state"


class _StreamError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _is_html_search_request(req: t.Any) -> bool:
    """``True`` for the HTML result page, ``False`` for every other endpoint.

    API clients (``format=json|rss|csv``) and the endpoint of the plugin itself
    are never touched: their timeouts and results stay exactly as they are.
    """

    if getattr(req, "path", None) != "/search":
        return False
    return (req.form.get("format") or "html").lower() == "html"


def _url_for(endpoint: str, **values: t.Any) -> str:
    from flask import url_for

    return url_for(endpoint, **values)


def _sse(event: str, data: dict[str, t.Any]) -> str:
    return "event: {}\ndata: {}\n\n".format(event, json.dumps(data, ensure_ascii=False))


def _engine_pair(item: t.Any) -> tuple[str, str]:
    if isinstance(item, (list, tuple)) and len(item) >= 2:
        return str(item[0] or ""), str(item[1] or "")
    if isinstance(item, str):
        return item, ""
    return "", ""


def _valid_language(value: str) -> bool:
    if value in ("auto", "all"):
        return True
    try:
        from searx.webutils import VALID_LANGUAGE_CODE
    except Exception:  # pragma: no cover - defensive
        return True
    return bool(VALID_LANGUAGE_CODE.match(value))


def _missing_internals() -> str | None:
    """Return a message when the SearXNG internals used by the plugin are absent."""

    try:
        from searx import engines  # noqa: F401
        from searx.search import SearchWithPlugins  # noqa: F401
        from searx.search.models import EngineRef, SearchQuery  # noqa: F401
        from searx.search.processors import PROCESSORS  # noqa: F401
        from searx.webapp import get_result_template, highlight_content, render  # noqa: F401
        from searx.webutils import get_translated_errors  # noqa: F401
    except Exception as exc:  # pylint: disable=broad-except
        return str(exc)
    return None


def _base_query(payload: dict[str, t.Any], engine_refs: list[t.Any]) -> t.Any | None:
    """Build the :py:obj:`searx.search.models.SearchQuery` of the second pass.

    The payload is signed (see :py:mod:`searxng_lazy_results.signing`) and was
    created by the plugin itself, so the engines, the category of an engine and
    the query are taken over verbatim.
    """

    from searx.search.models import SearchQuery

    query = payload.get("q")
    if not isinstance(query, str) or not query.strip():
        return None

    language = payload.get("language") or "all"
    if not isinstance(language, str) or not _valid_language(language):
        language = "all"

    try:
        safesearch = int(payload.get("safesearch") or 0)
    except (TypeError, ValueError):
        safesearch = 0
    if safesearch not in (0, 1, 2):
        safesearch = 0

    try:
        pageno = max(1, int(payload.get("pageno") or 1))
    except (TypeError, ValueError):
        pageno = 1

    time_range = payload.get("time_range")
    if time_range not in (None, "day", "week", "month", "year"):
        time_range = None

    engine_data = payload.get("engine_data")
    if not isinstance(engine_data, dict):
        engine_data = None

    try:
        return SearchQuery(
            query,
            list(engine_refs),
            language,
            safesearch,  # type: ignore[arg-type]
            pageno,
            time_range,  # type: ignore[arg-type]
            None,
            None,
            engine_data,
            None,
        )
    except Exception:  # pylint: disable=broad-except
        log.exception("lazy results: cannot build the search query")
        return None


def _engine_data(container: t.Any) -> dict[str, dict[str, str]] | None:
    """Engine data of the first page, if it is small enough to be carried along."""

    try:
        data = {name: dict(values) for name, values in container.engine_data.items()}
    except Exception:  # pragma: no cover - defensive
        return None
    if not data:
        return None
    try:
        if len(json.dumps(data, default=str)) > 4096:
            log.debug("lazy results: engine data is too large, ignored")
            return None
    except Exception:  # pragma: no cover - defensive
        return None
    return data


class SXNGPlugin(Plugin):
    """Show the results of fast engines immediately, stream in the slow ones."""

    id = PLUGIN_ID

    def __init__(self, plg_cfg: t.Any) -> None:
        super().__init__(plg_cfg)
        self.info = PluginInfo(
            id=self.id,
            name=gettext("Lazy results"),
            description=gettext(
                "Show the results of fast engines right away and stream in the results of slow engines"
            ),
            preference_section="ui",
        )
        self.cfg: LazyResultsConfig = load_config()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def init(self, app: t.Any) -> bool:
        if not self.cfg.enabled:
            self.log.info("lazy results: disabled by configuration")
            return False

        missing = _missing_internals()
        if missing:
            self.log.error("lazy results: unusable SearXNG version, plugin is disabled (%s)", missing)
            return False

        try:
            fragments.install(app)
            app.add_url_rule(STREAM_RULE, "lazy_results_stream", self.stream_view, methods=["GET", "POST"])
            app.add_url_rule(STATIC_RULE, "lazy_results_static", self.static_view, methods=["GET"])
            app.after_request(self.inject_assets)
        except Exception:  # pylint: disable=broad-except
            self.log.exception("lazy results: cannot register the plugin")
            return False

        self.log.info(
            "lazy results: first paint after %.2fs, second pass with %.1fs per engine (%i at a time)",
            self.cfg.first_paint_timeout,
            self.cfg.stream_timeout,
            self.cfg.max_concurrent_engines,
        )
        return True

    # ------------------------------------------------------------------
    # hooks
    # ------------------------------------------------------------------

    def pre_search(self, request: t.Any, search: t.Any) -> bool:
        """Cut the engines off after the first paint budget.

        The hook is called by SearXNG with the keywords ``request`` and
        ``search`` -- both names are part of the interface.
        """

        try:
            if self.cfg.first_paint_timeout <= 0 or not _is_html_search_request(request):
                return True

            query = search.search_query
            limit = self.cfg.first_paint_timeout
            if getattr(query, "timeout_limit", None) is not None:
                try:
                    limit = min(limit, float(query.timeout_limit))
                except (TypeError, ValueError):
                    pass
            query.timeout_limit = limit
            self.log.debug("lazy results: first paint budget is %.2fs", limit)
        except Exception:  # pylint: disable=broad-except
            self.log.exception("lazy results: pre_search failed")
        return True

    def post_search(self, request: t.Any, search: t.Any) -> None:
        """Remember the engines that have to be streamed."""

        try:
            state = self._state(request, search)
        except Exception:  # pylint: disable=broad-except
            self.log.exception("lazy results: post_search failed")
            return None
        if state and has_request_context():
            setattr(g, _G_STATE, state)
        return None

    # ------------------------------------------------------------------
    # the result page
    # ------------------------------------------------------------------

    def _state(self, request: t.Any, search: t.Any) -> dict[str, t.Any] | None:
        if not _is_html_search_request(request):
            return None

        query = search.search_query
        container = search.result_container

        order: dict[str, int] = {}
        for index, engineref in enumerate(query.engineref_list):
            order.setdefault(engineref.name, index)

        pending: dict[str, int] = {}
        for unresponsive in container.unresponsive_engines:
            name = unresponsive.engine
            if name not in order or name in pending:
                continue

            if not stream.is_timeout_reason(unresponsive.error_type):
                # An engine that broke for another reason (CAPTCHA, parsing
                # error, rate limit, ...) will not heal by being asked twice in
                # a row.
                continue

            if unresponsive.suspended and not self.cfg.resume_timeout_suspended:
                continue

            if len(pending) >= self.cfg.max_stream_engines:
                break
            pending[name] = order[name]

        if not pending:
            return None

        enginerefs = sorted(
            (engineref for engineref in query.engineref_list if engineref.name in pending),
            key=lambda engineref: pending[engineref.name],
        )

        payload: dict[str, t.Any] = {
            "v": PAYLOAD_VERSION,
            "q": query.query,
            "engines": [[engineref.name, engineref.category] for engineref in enginerefs],
            "language": query.lang,
            "safesearch": int(query.safesearch or 0),
            "pageno": int(query.pageno or 1),
            "time_range": query.time_range,
        }
        engine_data = _engine_data(container)
        if engine_data:
            payload["engine_data"] = engine_data

        raw_payload = signing.encode(payload)
        signature = signing.sign(payload, self.cfg.secret)

        return {
            "engines": [[engineref.name, engineref.category] for engineref in enginerefs],
            "streamUrl": "{}?{}".format(
                _url_for("lazy_results_stream"),
                urlencode({"payload": raw_payload, "sig": signature}),
            ),
            "engineLabels": self.cfg.engine_labels,
            "deduplicate": self.cfg.deduplicate,
            "strings": self._strings(),
        }

    @staticmethod
    def _strings() -> dict[str, str]:
        return {
            "loading": gettext("Still loading results from slow engines"),
            "waitingFor": gettext("Waiting for"),
            "loaded": gettext("loaded after the first paint"),
            "failed": gettext("Could not be loaded"),
            "timedOut": gettext("timeout"),
            "resultsFrom": gettext("Results from"),
            "resultOne": gettext("1 result"),
            "resultsMany": gettext("{count} results"),
            "seconds": gettext("s"),
        }

    def inject_assets(self, response: Response) -> Response:
        """Add the client script to a result page that has engines to stream."""

        try:
            if not has_request_context():
                return response
            state = getattr(g, _G_STATE, None)
            if not state:
                return response
            if response.status_code != 200 or response.direct_passthrough:
                return response
            if response.mimetype != "text/html":
                return response

            html = response.get_data(as_text=True)
            index = html.lower().rfind("</body>")
            if index < 0:
                self.log.debug("lazy results: no </body> in the result page, nothing injected")
                return response

            response.set_data(html[:index] + self._snippet(state) + html[index:])
            return response
        except Exception:  # pylint: disable=broad-except
            self.log.exception("lazy results: cannot inject the client")
            return response

    def _snippet(self, state: dict[str, t.Any]) -> str:
        config = json.dumps(state, ensure_ascii=False)
        # A JSON payload inside <script> must not contain markup.
        config = config.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")

        # The version busts the cache of the browser when the plugin is updated.
        version = __version__
        css = _url_for("lazy_results_static", filename=CLIENT_CSS, v=version)
        js = _url_for("lazy_results_static", filename=CLIENT_JS, v=version)

        return (
            "\n<!-- searxng-lazy-results -->\n"
            '<link rel="stylesheet" href="{}" type="text/css" media="screen">\n'
            '<script id="{}" type="application/json">{}</script>\n'
            '<script src="{}" defer></script>\n'
        ).format(css, CLIENT_CONFIG_ID, config, js)

    # ------------------------------------------------------------------
    # the streaming endpoint
    # ------------------------------------------------------------------

    def stream_view(self) -> Response:
        started = default_timer()
        try:
            payload = self._payload()
            engine_refs, skipped = self._engine_refs(payload)
        except _StreamError as exc:
            self.log.debug("lazy results: rejected stream request (%s)", exc.message)
            return Response(exc.message, status=exc.status, mimetype="text/plain")
        except Exception:  # pylint: disable=broad-except
            self.log.exception("lazy results: bad stream request")
            return Response("bad request", status=400, mimetype="text/plain")

        base_query = _base_query(payload, engine_refs)
        if base_query is None:
            return Response("bad request", status=400, mimetype="text/plain")

        cfg = self.cfg
        user_plugins = list(getattr(request, "user_plugins", []) or [])
        deadline = started + cfg.stream_timeout + cfg.stream_grace

        def generate() -> t.Iterator[str]:
            yield ": searxng-lazy-results\n\n"
            # Never reconnect: a second pass would ask every engine again.
            yield "retry: 600000\n\n"

            for name, message in skipped:
                yield _sse("engine", {"engine": name, "status": stream.STATUS_SKIPPED, "error": message})

            total = 0
            first_index = fragments.FIRST_INDEX_BASE + random.randint(0, 999)  # noqa: S311
            truncated = False
            try:
                for outcome in stream.outcomes(
                    base_query,
                    engine_refs,
                    timeout=cfg.stream_timeout,
                    max_concurrent=cfg.max_concurrent_engines,
                    user_plugins=user_plugins,
                    heartbeat=cfg.heartbeat,
                ):
                    if outcome is None:
                        yield ": keepalive\n\n"
                        continue

                    event: dict[str, t.Any] = {
                        "engine": outcome.engine,
                        "category": outcome.category,
                        "status": outcome.status,
                        "elapsed": round(outcome.elapsed, 2),
                        "count": len(outcome.results),
                    }
                    if outcome.error:
                        event["error"] = outcome.error
                    if outcome.results:
                        fragments.prepare(outcome.results, base_query.query)
                        event["html"] = fragments.render(outcome.results, first_index)
                        first_index += len(outcome.results)
                        total += len(outcome.results)
                    yield _sse("engine", event)

                    if default_timer() > deadline:
                        truncated = True
                        break
            except Exception:  # pylint: disable=broad-except
                self.log.exception("lazy results: stream aborted")
                truncated = True

            yield _sse(
                "done",
                {
                    "engines": len(engine_refs),
                    "results": total,
                    "elapsed": round(default_timer() - started, 2),
                    "truncated": truncated,
                },
            )

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    def _payload(self) -> dict[str, t.Any]:
        raw = request.form.get("payload") or ""
        if not raw:
            raise _StreamError("missing payload", 400)
        try:
            payload = signing.verified(
                raw,
                request.form.get("sig") or "",
                self.cfg.secret,
                required=self.cfg.require_token,
            )
        except ValueError as exc:
            raise _StreamError(str(exc), 403) from exc
        if payload.get("v") != PAYLOAD_VERSION:
            raise _StreamError("unsupported payload version", 400)
        return payload

    def _engine_refs(self, payload: dict[str, t.Any]) -> tuple[list[t.Any], list[tuple[str, str]]]:
        """Validate the engines of the request against this instance."""

        import searx
        from searx.search.models import EngineRef
        from searx.search.processors import PROCESSORS

        preferences = getattr(request, "preferences", None)
        known_engines = searx.engines.engines

        engine_refs: list[t.Any] = []
        skipped: list[tuple[str, str]] = []
        seen: set[str] = set()

        for item in list(payload.get("engines") or [])[: self.cfg.max_stream_engines]:
            name, category = _engine_pair(item)
            if not name or name in seen:
                continue
            seen.add(name)

            engine = known_engines.get(name)
            if engine is None:
                skipped.append((name, gettext("unknown engine")))
                continue
            if preferences is not None and not preferences.validate_token(engine):
                skipped.append((name, gettext("not allowed")))
                continue

            processor = PROCESSORS.get(name)
            if processor is None:
                skipped.append((name, gettext("engine is not available")))
                continue

            if processor.suspended_status.is_suspended:
                if self.cfg.resume_timeout_suspended and stream.resume_suspended_engine(name):
                    self.log.info(
                        "lazy results: engine '%s' was suspended by the first paint, asking it again",
                        name,
                    )
                else:
                    skipped.append((name, gettext("suspended")))
                    continue

            if not category:
                category = engine.categories[0] if engine.categories else ""
            engine_refs.append(EngineRef(name, category))

        if not engine_refs:
            detail = "; ".join(f"{name}: {message}" for name, message in skipped) or "no engine in the payload"
            raise _StreamError(f"no engine to stream ({detail})", 400)

        return engine_refs, skipped

    # ------------------------------------------------------------------
    # assets
    # ------------------------------------------------------------------

    def static_view(self, filename: str) -> Response:
        if filename not in ASSETS:
            return Response("not found", status=404, mimetype="text/plain")
        return send_from_directory(fragments.STATIC_DIR, filename, mimetype=ASSETS[filename], max_age=300)


__all__ = ["SXNGPlugin", "PLUGIN_ID", "STREAM_RULE", "STATIC_RULE"]
