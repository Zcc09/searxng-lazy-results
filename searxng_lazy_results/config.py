# SPDX-License-Identifier: AGPL-3.0-or-later
"""Configuration of the lazy-results plugin.

The configuration is read once per worker process when the plugin is
initialized.  Values are taken from (highest priority first)

1. environment variables ``SEARXNG_LAZY_RESULTS_<OPTION>``
2. the ``lazy_results`` section of :file:`settings.yml`
3. the defaults of :py:obj:`LazyResultsConfig`
"""

from __future__ import annotations

import dataclasses
import logging
import os
import typing as t

log = logging.getLogger("searx.plugins.lazy_results")

ENV_PREFIX = "SEARXNG_LAZY_RESULTS_"
SETTINGS_SECTION = "lazy_results"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


@dataclasses.dataclass(frozen=True)
class LazyResultsConfig:
    """Options of the lazy-results plugin."""

    enabled: bool = True
    """Master switch.  ``False`` disables first paint and streaming completely."""

    first_paint_timeout: float = 1.2
    """Seconds the first page is allowed to wait for the engines.

    Engines that did not answer within this budget are dropped from the first
    page and streamed in later.  ``0`` disables the early first paint (the
    plugin then only streams engines that timed out for other reasons).
    """

    stream_timeout: float = 8.0
    """Per engine budget of the second pass (seconds)."""

    stream_grace: float = 3.0
    """Extra seconds the streaming endpoint stays open after ``stream_timeout``
    so that engines which finished slightly late are still delivered."""

    max_concurrent_engines: int = 4
    """How many engines are asked at the same time by the streaming endpoint."""

    max_stream_engines: int = 12
    """Upper bound of engines that are streamed for one search."""

    heartbeat: float = 10.0
    """Seconds of silence before a keep-alive comment is sent to the browser."""

    engine_labels: bool = True
    """Print a small "results from <engine>" label above every streamed batch."""

    deduplicate: bool = True
    """Drop streamed results whose URL is already on the page."""

    require_token: bool = True
    """Only accept streaming requests that carry a valid, signed payload.

    The signature is created from the instance's ``server.secret_key``; it
    prevents the endpoint from being used as a general purpose search API by
    clients that never rendered a result page.
    """

    resume_timeout_suspended: bool = True
    """Ask engines again that were suspended because of the first paint.

    SearXNG suspends an engine when its request runs into an error.  An engine
    that is cut off by the first paint budget runs into exactly that: its HTTP
    request is aborted and the engine is suspended for
    ``search.ban_time_on_fail`` seconds -- the second pass would find it
    suspended and could never deliver its results.  Engines whose suspension
    reason is a timeout are therefore resumed for the second pass; engines that
    were suspended for any other reason (rate limit, CAPTCHA, ...) are left
    alone and reported as ``skipped``.
    """

    secret: str | None = None
    """Optional explicit HMAC secret, see :py:obj:`require_token`."""

    def as_dict(self) -> dict[str, t.Any]:
        return dataclasses.asdict(self)


def _bool(value: t.Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


def _number(value: t.Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _settings_section() -> dict[str, t.Any]:
    try:
        import searx

        section = searx.settings.get(SETTINGS_SECTION)
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("lazy results: cannot read the settings section: %s", exc)
        return {}
    if isinstance(section, dict):
        return section
    return {}


def load_config(extra: dict[str, t.Any] | None = None) -> LazyResultsConfig:
    """Build the effective configuration of the plugin."""

    values: dict[str, t.Any] = {}

    raw: dict[str, t.Any] = dict(_settings_section())
    if extra:
        raw.update(extra)

    for field in dataclasses.fields(LazyResultsConfig):
        name = field.name
        env_value = os.environ.get(ENV_PREFIX + name.upper())

        if field.type in ("bool", bool) or isinstance(field.default, bool):
            value: t.Any = None
            if name in raw:
                value = _bool(raw[name])
            if env_value is not None:
                value = _bool(env_value)
            if value is not None:
                values[name] = value
        elif isinstance(field.default, int) and not isinstance(field.default, bool):
            value = None
            if name in raw:
                value = _number(raw[name])
            if env_value is not None:
                value = _number(env_value)
            if value is not None:
                values[name] = int(value)
        elif isinstance(field.default, float):
            value = None
            if name in raw:
                value = _number(raw[name])
            if env_value is not None:
                value = _number(env_value)
            if value is not None:
                values[name] = float(value)
        elif name == "secret":
            value = raw.get(name, env_value)
            if value is not None:
                values[name] = str(value)

    cfg = LazyResultsConfig(**values)
    return dataclasses.replace(
        cfg,
        first_paint_timeout=_clamp(cfg.first_paint_timeout, 0.0, 30.0),
        stream_timeout=_clamp(cfg.stream_timeout, 0.1, 60.0),
        stream_grace=_clamp(cfg.stream_grace, 0.0, 60.0),
        max_concurrent_engines=int(_clamp(cfg.max_concurrent_engines, 1, 16)),
        max_stream_engines=int(_clamp(cfg.max_stream_engines, 1, 64)),
        heartbeat=_clamp(cfg.heartbeat, 2.0, 120.0),
    )


__all__ = ["LazyResultsConfig", "load_config", "ENV_PREFIX", "SETTINGS_SECTION"]
