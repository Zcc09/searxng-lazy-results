# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests of the plugin's dependency free modules.

The end to end behaviour lives in :file:`tests/run_e2e.py` (it needs a running
SearXNG); these tests cover the pure Python parts and run anywhere:

    python -m pytest tests -q -k "not e2e"
"""

from __future__ import annotations

import base64
import json
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from searxng_lazy_results import signing  # noqa: E402
from searxng_lazy_results.config import LazyResultsConfig, load_config  # noqa: E402
from searxng_lazy_results.stream import (  # noqa: E402
    STATUS_EMPTY,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_SKIPPED,
    Outcome,
    is_timeout_reason,
)

SECRET = "unit-test-secret"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """No test may be influenced by the ambient environment."""

    for name in list(os.environ):
        if name.startswith("SEARXNG_LAZY_RESULTS_"):
            monkeypatch.delenv(name, raising=False)
    yield


# ---------------------------------------------------------------- signing


def test_sign_and_verify_round_trip():
    payload = {"v": 1, "q": "lazy", "engines": [["mockslow", "general"]], "pageno": 1}
    raw = signing.encode(payload)
    signature = signing.sign(payload, SECRET)

    assert signing.verified(raw, signature, SECRET) == payload


def test_encoding_is_url_safe_and_compact():
    raw = signing.encode({"q": "a b/ünd", "v": 1})

    assert set(raw) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert "=" not in raw
    assert json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))) == {"q": "a b/ünd", "v": 1}


def test_verify_rejects_tampered_payload():
    payload = {"v": 1, "q": "lazy"}
    raw, signature = signing.encode(payload), signing.sign(payload, SECRET)
    tampered = signing.encode({"v": 1, "q": "evil"})

    with pytest.raises(ValueError) as error:
        signing.verified(tampered, signature, SECRET)
    assert "signature" in str(error.value)


def test_verify_rejects_missing_signature_when_required():
    payload = {"v": 1, "q": "lazy"}
    raw = signing.encode(payload)

    with pytest.raises(ValueError):
        signing.verified(raw, "", SECRET, required=True)
    assert signing.verified(raw, "", SECRET, required=False) == payload


def test_verify_rejects_malformed_payload():
    with pytest.raises(ValueError):
        signing.verified("not-base64-!!", "x" * 64, SECRET)


def test_signature_depends_on_the_secret():
    payload = {"v": 1, "q": "lazy"}
    raw = signing.encode(payload)

    with pytest.raises(ValueError):
        signing.verified(raw, signing.sign(payload, "another-secret"), SECRET)


# ---------------------------------------------------------------- config


def test_defaults_without_any_configuration():
    config = load_config()

    assert config.enabled is True
    assert config.first_paint_timeout == 1.2
    assert config.stream_timeout == 8.0
    assert config.max_concurrent_engines == 4
    assert config.require_token is True
    assert config.secret is None


def test_settings_section_is_used():
    config = load_config({"first_paint_timeout": 2.5, "engine_labels": "no"})

    assert config.first_paint_timeout == 2.5
    assert config.engine_labels is False
    assert config.stream_timeout == 8.0


def test_environment_wins_over_settings_section(monkeypatch):
    monkeypatch.setenv("SEARXNG_LAZY_RESULTS_STREAM_TIMEOUT", "3.5")
    monkeypatch.setenv("SEARXNG_LAZY_RESULTS_ENABLED", "false")

    config = load_config({"stream_timeout": 9.0})

    assert config.stream_timeout == 3.5
    assert config.enabled is False


def test_values_are_clamped():
    config = load_config(
        {
            "first_paint_timeout": -5,
            "max_concurrent_engines": 99,
            "max_stream_engines": 0,
            "heartbeat": 1000,
            "stream_timeout": 600,
        }
    )

    assert config.first_paint_timeout == 0.0
    assert config.max_concurrent_engines == 16
    assert config.max_stream_engines == 1
    assert config.heartbeat == 120.0
    assert config.stream_timeout == 60.0


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_true_values(value):
    assert load_config({"deduplicate": value}).deduplicate is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_false_values(value):
    assert load_config({"deduplicate": value}).deduplicate is False


def test_unparsable_values_fall_back_to_the_default():
    config = load_config({"first_paint_timeout": "soon", "deduplicate": "maybe"})

    assert config.first_paint_timeout == LazyResultsConfig().first_paint_timeout
    assert config.deduplicate is True


def test_secret_is_a_string():
    assert load_config({"secret": 12345}).secret == "12345"


# ---------------------------------------------------------------- stream


def test_outcome_defaults():
    outcome = Outcome("mockfast")

    assert outcome.status == STATUS_OK
    assert outcome.results == []
    assert outcome.error is None


def test_status_constants_are_distinct():
    assert len({STATUS_OK, STATUS_EMPTY, STATUS_ERROR, STATUS_SKIPPED}) == 4


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("timeout", True),
        ("asyncio.TimeoutError", True),
        ("curl_cffi.requests.exceptions.ReadTimeout", True),
        ("searx.exceptions.SearxEngineTooManyRequestsException", False),
        ("CAPTCHA", False),
        ("", False),
        (None, False),
    ],
)
def test_is_timeout_reason(reason, expected):
    assert is_timeout_reason(reason) is expected


def test_plugin_is_importable_and_deactivates_without_the_internals(monkeypatch):
    """The plugin module only needs ``searx.plugins.Plugin``/``PluginInfo``.

    Importing it with a stubbed SearXNG proves two things: the module does not
    drag the application in, and a missing internal switches the plugin off
    instead of breaking the instance.
    """

    import importlib
    import logging as logging_module

    fake_plugins = types.ModuleType("searx.plugins")

    class Plugin:  # pylint: disable=too-few-public-methods
        """Stand-in for ``searx.plugins.Plugin``."""

    class PluginInfo:  # pylint: disable=too-few-public-methods
        """Stand-in for ``searx.plugins.PluginInfo``."""

    fake_plugins.Plugin = Plugin
    fake_plugins.PluginInfo = PluginInfo
    monkeypatch.setitem(sys.modules, "searx", types.ModuleType("searx"))
    monkeypatch.setitem(sys.modules, "searx.plugins", fake_plugins)

    module = importlib.import_module("searxng_lazy_results.plugin")

    assert module.PLUGIN_ID == "lazyResults"
    assert module.STREAM_RULE == "/lazy-results/stream"
    assert module.STATIC_RULE == "/lazy-results/static/<path:filename>"
    for hook in ("init", "pre_search", "post_search"):
        assert callable(getattr(module.SXNGPlugin, hook))

    # Without searx.webapp / searx.search there is nothing to hook into ...
    assert isinstance(module._missing_internals(), str)

    # ... so init() reports the reason and returns False (the plugin is inactive).
    plugin = module.SXNGPlugin.__new__(module.SXNGPlugin)
    plugin.id = module.PLUGIN_ID
    plugin.cfg = LazyResultsConfig()
    plugin.log = logging_module.getLogger("searxng_lazy_results.test")
    assert plugin.init(object()) is False


def test_timeout_marker_covers_the_request_errors():
    """The marker has to match what SearXNG records for an abandoned request."""

    for reason in ("timeout", "asyncio.TimeoutError", "curl_cffi.requests.exceptions.Timeout"):
        assert is_timeout_reason(reason)
