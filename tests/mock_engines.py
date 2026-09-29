#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Mock search backends with a configurable latency.

The tests and the container example need upstream engines whose response time is
known, so every assertion about "fast engines are rendered before slow engines"
is deterministic instead of depending on the mood of the internet.

The server is intentionally dependency free (stdlib only) so that it can run
inside a ``python:alpine`` container next to SearXNG.

Endpoints
---------

``GET /mock/<name>?q=<query>``
    Answers with ``n`` results (default 3) after the latency registered for
    ``<name>``.  ``/mock/<name>`` is meant to be configured as a
    :ref:`json_engine <searxng settings engines>` of SearXNG::

        - name: mockfast
          engine: json_engine
          search_url: http://127.0.0.1:8770/mock/fast?q={query}
          results_query: results
          url_query: url
          title_query: title
          content_query: content

``GET /healthz``
    ``OK`` as soon as the server listens.

Query parameters: ``q`` (echoed), ``n`` (number of results), ``delay``
(overrides the registered latency), ``status`` (HTTP status to answer with).

Latencies are registered with ``--delay name=seconds`` (repeatable) or with the
environment variable ``MOCK_DELAYS="fast=0.2,medium=1.2,slow=4"``.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import typing as t
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DEFAULT_DELAYS: dict[str, float] = {"fast": 0.2, "medium": 1.2, "slow": 4.0}
DEFAULT_DELAY = 0.2


def parse_delays(spec: str) -> dict[str, float]:
    delays: dict[str, float] = {}
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        name, _, value = item.partition("=")
        try:
            delays[name.strip()] = float(value)
        except ValueError:
            raise SystemExit(f"cannot parse delay {item!r}, expected name=seconds")
    return delays


class MockEngineHandler(BaseHTTPRequestHandler):
    server_version = "mock-engines/1.0"
    delays: dict[str, float] = dict(DEFAULT_DELAYS)
    default_delay: float = DEFAULT_DELAY

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urlparse(self.path)

        if parsed.path.rstrip("/") == "/healthz":
            return self.answer(200, "OK", "text/plain")

        name = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        params = parse_qs(parsed.query)
        query = (params.get("q") or [""])[0]

        try:
            count = int((params.get("n") or ["3"])[0])
        except ValueError:
            count = 3

        delay = self.delays.get(name, self.default_delay)
        if "delay" in params:
            try:
                delay = float(params["delay"][0])
            except ValueError:
                pass

        status = 200
        if "status" in params:
            try:
                status = int(params["status"][0])
            except ValueError:
                status = 200

        if delay > 0:
            time.sleep(delay)

        if status != 200:
            return self.answer(status, f"status {status}", "text/plain")

        results = [
            {
                "url": f"https://example.org/{name}/{index}?q={query}",
                "title": f"{name} result {index} for {query}",
                "content": f"result {index} from the {name} engine",
            }
            for index in range(1, max(0, count) + 1)
        ]
        body = json.dumps(
            {
                "engine": name,
                "query": query,
                "delay": delay,
                "results": results,
            }
        )
        self.answer(status, body, "application/json")

    def do_HEAD(self) -> None:  # noqa: N802 - http.server API
        self.answer(200, "", "text/plain", send_body=False)

    def answer(self, status: int, body: str, content_type: str, send_body: bool = True) -> None:
        payload = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if send_body:
            self.wfile.write(payload)

    def log_message(self, format: str, *args: t.Any) -> None:  # pylint: disable=redefined-builtin
        if os.environ.get("MOCK_QUIET"):
            return
        super().log_message(format, *args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="mock search backends with configurable latency")
    parser.add_argument("--host", default=os.environ.get("MOCK_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MOCK_PORT", "8770")))
    parser.add_argument(
        "--delay",
        action="append",
        default=[],
        metavar="NAME=SECONDS",
        help="latency of a mock engine (repeatable)",
    )
    parser.add_argument("--default-delay", type=float, default=DEFAULT_DELAY)
    args = parser.parse_args(argv)

    delays = dict(DEFAULT_DELAYS)
    env = os.environ.get("MOCK_DELAYS")
    if env:
        delays.update(parse_delays(env))
    for item in args.delay:
        delays.update(parse_delays(item))

    MockEngineHandler.delays = delays
    MockEngineHandler.default_delay = args.default_delay

    server = ThreadingHTTPServer((args.host, args.port), MockEngineHandler)
    print(f"mock engines on http://{args.host}:{args.port}/mock/<name>  delays={delays}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
