# SPDX-License-Identifier: AGPL-3.0-or-later
"""Signed payloads for the streaming endpoint.

The streaming endpoint must not become a general purpose search API that
bypasses the instance configuration: a client may only stream the engines of a
search whose result page it has actually rendered.  The payload therefore
carries the query and the engine list and is signed with the instance secret.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import typing as t

log = logging.getLogger("searx.plugins.lazy_results")

FALLBACK_SECRET = b"searxng-lazy-results/secret-key-unset"


def canonical(payload: dict[str, t.Any]) -> bytes:
    """Deterministic serialization of *payload*."""

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _instance_secret(explicit: str | None = None) -> bytes:
    if explicit:
        return explicit.encode("utf-8")
    try:
        import searx

        key = searx.settings["server"].get("secret_key")
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("lazy results: cannot read server.secret_key: %s", exc)
        key = None
    if key:
        return str(key).encode("utf-8")
    # No instance secret: the fallback must still be identical in every worker
    # process, otherwise a page rendered by worker A cannot be streamed by
    # worker B.
    log.warning(
        "lazy results: server.secret_key is not set, falling back to a static secret; "
        "set server.secret_key (or the plugin option 'secret') on public instances"
    )
    return FALLBACK_SECRET


def sign(payload: dict[str, t.Any], secret: str | None = None) -> str:
    return hmac.new(_instance_secret(secret), canonical(payload), hashlib.sha256).hexdigest()


def encode(payload: dict[str, t.Any]) -> str:
    return base64.urlsafe_b64encode(canonical(payload)).rstrip(b"=").decode("ascii")


def decode(raw: str) -> dict[str, t.Any]:
    padded = raw + "=" * (-len(raw) % 4)
    data = base64.urlsafe_b64decode(padded.encode("ascii"))
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("payload is not an object")
    return payload


def verified(raw: str, signature: str, secret: str | None = None, required: bool = True) -> dict[str, t.Any]:
    """Return the payload of a request or raise :py:obj:`ValueError`."""

    try:
        payload = decode(raw)
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError("malformed payload") from exc

    if required:
        if not signature:
            raise ValueError("missing signature")
        expected = sign(payload, secret)
        if not hmac.compare_digest(expected, signature):
            raise ValueError("invalid signature")

    return payload


__all__ = ["sign", "encode", "decode", "verified", "canonical"]
