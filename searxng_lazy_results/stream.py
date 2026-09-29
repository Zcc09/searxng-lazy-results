# SPDX-License-Identifier: AGPL-3.0-or-later
"""Second pass: ask the engines that are still missing, one by one.

SearXNG runs all engines of a search in parallel and returns when every one of
them answered (or its deadline expired) -- there is no incremental delivery.
The streaming pass therefore searches *one engine per :py:obj:`Search`* so that
an engine's result can be handed to the client the moment it arrives.

The SearXNG search stack (:py:obj:`searx.search.SearchWithPlugins`) is used as
is, hence suspension handling, timings, metrics, engine tokens and the plugins
of the instance behave exactly like on the first page.

Suspension
==========

An engine whose request is cut off by the first paint budget runs into a timeout
*inside* the engine's own request, and SearXNG suspends the engine for
``search.ban_time_on_fail`` seconds (see
:py:obj:`searx.search.processors.abstract.EngineProcessor.handle_exception`).
That suspension is an artefact of the first paint and not an engine failure, so
the second pass lifts it and asks the engine again -- see
:py:obj:`resume_suspended_engine`.  Suspensions for any other reason (rate
limit, CAPTCHA, access denied, ...) are respected.
"""

from __future__ import annotations

import copy
import logging
import queue
import typing as t
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from timeit import default_timer

log = logging.getLogger("searx.plugins.lazy_results")

STATUS_OK = "ok"
"""The engine answered and returned results."""

STATUS_EMPTY = "empty"
"""The engine answered but had nothing to offer."""

STATUS_ERROR = "error"
"""The engine did not answer (timeout, CAPTCHA, ...)."""

STATUS_SKIPPED = "skipped"
"""The engine was not asked at all (unknown, suspended, not allowed)."""

TIMEOUT_MARKER = "timeout"
"""Marker of the suspension reasons a first paint timeout produces."""


@dataclass
class Outcome:
    """Result of the second pass for a single engine."""

    engine: str
    category: str = ""
    status: str = STATUS_OK
    elapsed: float = 0.0
    results: list[t.Any] = field(default_factory=list)
    error: str | None = None


def is_timeout_reason(reason: str | None) -> bool:
    """``True`` for suspension reasons a first paint timeout produces.

    ``asyncio.TimeoutError``, ``curl_cffi.requests.exceptions.ReadTimeout`` and
    the plain ``timeout`` of SearXNG's request handling all carry the marker.
    """

    return bool(reason) and TIMEOUT_MARKER in str(reason).lower()


def resume_suspended_engine(engine_name: str) -> bool:
    """Lift a suspension that was caused by a timeout.

    Returns ``True`` when the engine may be asked again.  A suspension that was
    caused by anything else (rate limit, CAPTCHA, ...) is left untouched.
    """

    from searx.search.processors import PROCESSORS

    processor = PROCESSORS.get(engine_name)
    if processor is None:
        return False

    status = processor.suspended_status
    if not is_timeout_reason(getattr(status, "suspend_reason", "")):
        return False
    if status.is_suspended:
        status.resume()
    return True


def failed_message(container: t.Any, engine_name: str) -> str | None:
    """User facing reason why *engine_name* did not answer, if it did not."""

    try:
        from searx.webutils import get_translated_errors

        for name, message in get_translated_errors(container.unresponsive_engines):
            if name == engine_name:
                return message
    except Exception:  # pragma: no cover - defensive
        log.exception("lazy results: cannot translate the engine errors")
    return None


def suspended_by_timeout(container: t.Any, engine_name: str) -> bool:
    """``True`` when the engine was skipped because it is suspended for a timeout."""

    for entry in container.unresponsive_engines:
        if entry.engine == engine_name and entry.suspended and is_timeout_reason(entry.error_type):
            return True
    return False


def attempt(
    base_query: t.Any,
    engineref: t.Any,
    *,
    timeout: float,
    user_plugins: list[str],
) -> tuple[list[t.Any], str | None, bool]:
    """Ask one engine once.

    Returns ``(results, error_message, suspended_by_timeout)``.
    """

    from flask import request as flask_request
    from searx.search import SearchWithPlugins

    search_query = copy.copy(base_query)
    search_query.engineref_list = [engineref]
    search_query.timeout_limit = timeout

    container = SearchWithPlugins(search_query, flask_request, user_plugins).search()
    results = list(container.get_ordered_results())

    if suspended_by_timeout(container, engineref.name):
        return results, None, True

    return results, failed_message(container, engineref.name), False


def run_engine(
    base_query: t.Any,
    engineref: t.Any,
    *,
    timeout: float,
    user_plugins: list[str],
) -> Outcome:
    """Run a single engine, asking it once more when it was suspended meanwhile.

    Must be called with a request context, see :py:obj:`outcomes` (the engines of
    SearXNG are started with a copy of the Flask request context).
    """

    started = default_timer()
    attempts = 2

    for try_number in range(1, attempts + 1):
        try:
            results, message, suspended = attempt(
                base_query, engineref, timeout=timeout, user_plugins=user_plugins
            )
        except Exception as exc:  # pylint: disable=broad-except
            log.exception("lazy results: engine '%s' failed", engineref.name)
            return Outcome(
                engineref.name, engineref.category, STATUS_ERROR, default_timer() - started, [], str(exc)
            )

        if suspended and try_number < attempts:
            if resume_suspended_engine(engineref.name):
                log.info(
                    "lazy results: '%s' was suspended while the first paint was running, asking it again",
                    engineref.name,
                )
                continue
            return Outcome(
                engineref.name, engineref.category, STATUS_SKIPPED, default_timer() - started, [], "suspended"
            )

        if message:
            return Outcome(
                engineref.name, engineref.category, STATUS_ERROR, default_timer() - started, [], message
            )

        status = STATUS_OK if results else STATUS_EMPTY
        return Outcome(engineref.name, engineref.category, status, default_timer() - started, results)

    return Outcome(
        engineref.name, engineref.category, STATUS_ERROR, default_timer() - started, [], "suspended"
    )


def outcomes(
    base_query: t.Any,
    enginerefs: t.Sequence[t.Any],
    *,
    timeout: float,
    max_concurrent: int,
    user_plugins: list[str],
    heartbeat: float = 10.0,
) -> t.Iterator[Outcome | None]:
    """Yield an :py:obj:`Outcome` as soon as an engine answered.

    ``None`` is yielded when nothing happened for *heartbeat* seconds; the
    caller is expected to send a keep-alive to the client.
    """

    from flask import copy_current_request_context

    results: queue.Queue[Outcome] = queue.Queue()
    pool = ThreadPoolExecutor(max_workers=max(1, int(max_concurrent)), thread_name_prefix="lazy-results")

    def worker(engineref: t.Any):
        # copy_current_request_context() copies the *current* request context,
        # the engines of SearXNG need one (see Search.search_multiple_requests).
        @copy_current_request_context
        def _run() -> None:
            results.put(run_engine(base_query, engineref, timeout=timeout, user_plugins=user_plugins))

        return _run

    try:
        for engineref in enginerefs:
            pool.submit(worker(engineref))

        outstanding = len(enginerefs)
        while outstanding > 0:
            try:
                outcome = results.get(timeout=heartbeat)
            except queue.Empty:
                yield None
                continue
            outstanding -= 1
            yield outcome
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


__all__ = [
    "Outcome",
    "STATUS_EMPTY",
    "STATUS_ERROR",
    "STATUS_OK",
    "STATUS_SKIPPED",
    "attempt",
    "failed_message",
    "is_timeout_reason",
    "outcomes",
    "resume_suspended_engine",
    "run_engine",
    "suspended_by_timeout",
]
