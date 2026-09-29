#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""End to end test of the lazy-results plugin.

The test needs a SearXNG instance whose engines are the ones of
:file:`tests/mock_engines.py` (:file:`tests/settings.test.yml`) and a mock
engine server, both of which the script can start on its own (``--manage``).

    python tests/run_e2e.py --manage                     # local development
    SEARXNG_E2E=1 python tests/run_e2e.py --base-url http://127.0.0.1:8888

What is actually proven (every assertion is a measurement, not a claim):

1. ``first paint``   -- a HTML search returns before the slow engine answered,
                        with the fast engine's results and *without* the slow
                        engine's results, and with the client configuration
                        injected.
2. ``progressive``   -- the stream of that page delivers the slow engine *after*
                        the first page and the medium engine before the slow one,
                        rendered with the result templates of the theme.
3. ``api untouched`` -- ``format=json`` still waits for every engine and returns
                        every engine's results: no first paint for API clients.
4. ``no injection``  -- a search that every engine answered in time gets no
                        plugin markup at all.
5. ``signature``     -- a tampered payload, or a payload with an engine that the
                        plugin did not sign, is refused with 403.
6. ``escaping``      -- a hostile engine title is escaped in the streamed
                        fragment, exactly like on the first page.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import typing as t
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]

MOCK_DELAYS = {"fast": 0.2, "medium": 1.2, "slow": 4.0}
FIRST_PAINT_TIMEOUT = 1.0  # tests/settings.test.yml
SLOW_URL = "https://example.org/slow/1"
FAST_URL = "https://example.org/fast/1"
MEDIUM_URL = "https://example.org/medium/1"

CONFIG_RE = re.compile(
    r'<script id="lazy-results-config" type="application/json">(?P<config>.*?)</script>', re.S
)

failures: list[str] = []
measurements: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  ok   {message}")
        return
    failures.append(message)
    print(f"  FAIL {message}")


def measure(message: str) -> None:
    measurements.append(message)
    print(f"  ..   {message}")


def page_config(html: str) -> dict[str, t.Any] | None:
    match = CONFIG_RE.search(html)
    if not match:
        return None
    return json.loads(match.group("config"))


def article_urls(html: str) -> set[str]:
    """URLs of the result anchors on a page, without their query string."""

    return {url.split("?")[0] for url in re.findall(r'<a href="(https://example\.org/[^"]+)"', html)}


def sse_events(
    response: requests.Response, limit: float = 30.0
) -> t.Iterator[tuple[float, str, dict[str, t.Any]]]:
    """Yield ``(seconds_since_start, event, data)`` of a ``text/event-stream``.

    The raw response is read so that the arrival time of an event is the time
    its bytes actually arrived -- ``iter_lines()`` buffers and would make every
    event look like it was delivered at the end of the stream.
    """

    started = time.time()
    buffer = ""
    event: str | None = None
    data: list[str] = []

    while time.time() - started <= limit:
        chunk = response.raw.read1(8192)
        if not chunk:
            return
        buffer += chunk.decode("utf-8", "replace")

        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
            elif line == "" and event and data:
                yield time.time() - started, event, json.loads("\n".join(data))
                event, data = None, []
            elif line == "":
                event, data = None, []


def search(base: str, query: str, params: str = "") -> requests.Response:
    url = f"{base}/search?q={requests.utils.quote(query)}"
    if params:
        url += f"&{params}"
    return requests.get(url, timeout=30)


# ---------------------------------------------------------------- test cases


def test_first_paint(base: str) -> dict[str, t.Any]:
    print("\n[1] first paint")
    started = time.time()
    response = search(base, "lazy", "engines=mockfast,mockmedium,mockslow")
    elapsed = time.time() - started
    html = response.text

    measure(f"first page: HTTP {response.status_code} after {elapsed:.2f}s")
    check(response.status_code == 200, "the search page answers with 200")
    check(elapsed < MOCK_DELAYS["slow"] - 1, "the page is returned before the slow engine could answer")
    check(elapsed >= MOCK_DELAYS["fast"] / 2, "the page waited for the fast engine")
    check(FAST_URL in article_urls(html), "the fast engine's result is on the first page")
    check(SLOW_URL not in article_urls(html), "the slow engine's result is not on the first page")
    check(MEDIUM_URL not in article_urls(html), "the medium engine's result is not on the first page")
    check(html.count("<article") >= 1, "the first page contains at least one result")
    check("mockfast" in html, "the sidebar reports the fast engine")
    check("timeout" in html, "the engines that were cut off are reported as timeout")

    config = page_config(html)
    check(config is not None, "the client configuration is injected")
    if config:
        engines = [item[0] for item in config["engines"]]
        measure(f"engines to stream: {engines}")
        check(engines == ["mockmedium", "mockslow"], "exactly the engines that timed out are streamed")
        check("streamUrl" in config and "payload=" in config["streamUrl"], "the stream URL is signed")
        check("lazy-results.js" in html and "lazy-results.css" in html, "client script and styles are injected")
        check("lazy-results.js" not in html.split("lazy-results-config")[0], "the client is injected before </body>")
    return {"config": config, "elapsed": elapsed}


def test_progressive(base: str, config: dict[str, t.Any] | None) -> None:
    print("\n[2] progressive streaming")
    if not config:
        check(False, "no configuration to stream (test 1 failed)")
        return

    url = base + config["streamUrl"]
    started = time.time()
    with requests.get(url, stream=True, timeout=(10, 60)) as response:
        if response.status_code != 200:
            check(False, f"the stream answers with 200 (got {response.status_code}: {response.text[:160]!r})")
            return
        check(True, "the stream answers with 200")
        check(
            response.headers.get("content-type", "").startswith("text/event-stream"),
            "the stream is text/event-stream",
        )
        check(response.headers.get("x-accel-buffering") == "no", "the stream asks proxies not to buffer")

        arrivals: dict[str, float] = {}
        batches: dict[str, dict[str, t.Any]] = {}
        done: dict[str, t.Any] | None = None

        for offset, event, data in sse_events(response):
            if event == "engine":
                arrivals[data["engine"]] = offset
                batches[data["engine"]] = data
            elif event == "done":
                done = data
                break

    total = time.time() - started
    measure(
        f"stream wall time: {total:.2f}s, arrivals: "
        + ", ".join(f"{k}={v:.2f}s" for k, v in arrivals.items())
        + ", engine time on the server: "
        + ", ".join(f"{k}={v.get('elapsed')}s" for k, v in batches.items())
    )

    check("mockslow" in arrivals, "the slow engine is delivered by the stream")
    check("mockmedium" in arrivals, "the medium engine is delivered by the stream")
    if "mockslow" in arrivals and "mockmedium" in arrivals:
        check(arrivals["mockmedium"] < arrivals["mockslow"], "the faster engine arrives first")
        check(arrivals["mockslow"] > 1.0, "the slow engine is a real second pass, not a replayed cache")

    for engine, url_fragment in (("mockslow", SLOW_URL), ("mockmedium", MEDIUM_URL)):
        batch = batches.get(engine) or {}
        check(batch.get("status") == "ok", f"{engine} reports status ok")
        check(batch.get("count", 0) >= 1, f"{engine} delivered at least one result")
        html = batch.get("html") or ""
        check(url_fragment in html, f"{engine}: the streamed fragment contains its result")
        check("<article" in html and "result_inner" in html, f"{engine}: the fragment is rendered by the theme")
        check(f'<span>{engine}</span>' in html, f"{engine}: the fragment names its engine")

    check(done is not None, "the stream ends with a done event")
    if done:
        measure(f"done: engines={done.get('engines')} results={done.get('results')} elapsed={done.get('elapsed')}s")
        check(done.get("results", 0) >= 2, "the done event accounts for the streamed results")
        check(not done.get("truncated"), "the stream was not truncated")


def test_api_untouched(base: str) -> None:
    print("\n[3] API clients keep the old behaviour")
    started = time.time()
    response = search(base, "lazy", "format=json")
    elapsed = time.time() - started
    payload = response.json()

    results = len(payload.get("results", []))
    measure(f"json search: HTTP {response.status_code} after {elapsed:.2f}s with {results} results")
    check(response.status_code == 200, "the json API answers with 200")
    check(elapsed >= MOCK_DELAYS["slow"] - 0.5, "the json API waited for the slow engine (no first paint)")
    urls = {str(result.get("url", "")).split("?")[0] for result in payload.get("results", [])}
    check({FAST_URL, MEDIUM_URL, SLOW_URL} <= urls, "the json API returns the results of every engine")
    check(not payload.get("unresponsive_engines"), "no engine failed in the json API")


def test_no_injection(base: str) -> None:
    print("\n[4] nothing is injected when every engine answered in time")
    started = time.time()
    html = search(base, "lazy", "engines=mockfast").text
    elapsed = time.time() - started

    measure(f"fast only: {elapsed:.2f}s, page size {len(html)} bytes")
    check(FAST_URL in article_urls(html), "the fast engine's result is on the page")
    check("lazy-results-config" not in html, "no client configuration for a search without slow engines")
    check("lazy-results.js" not in html, "no client script for a search without slow engines")


def test_signature(base: str, config: dict[str, t.Any] | None) -> None:
    print("\n[5] the endpoint only serves signed searches")
    if not config:
        check(False, "no configuration to stream (test 1 failed)")
        return

    parsed = urlparse(base + config["streamUrl"])
    params = parse_qs(parsed.query)
    payload, signature = params["payload"][0], params["sig"][0]

    tampered = requests.get(
        f"{base}/lazy-results/stream?payload={payload[:-4]}AAAA&sig={signature}", timeout=20
    )
    check(tampered.status_code == 403, f"a tampered payload is refused ({tampered.status_code})")

    import base64

    decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    decoded["engines"].append(["mockfast", "general"])
    forged = base64.urlsafe_b64encode(json.dumps(decoded).encode()).rstrip(b"=").decode()
    forged_response = requests.get(f"{base}/lazy-results/stream?payload={forged}&sig={signature}", timeout=20)
    check(
        forged_response.status_code == 403,
        f"an engine that was not signed is refused ({forged_response.status_code})",
    )

    unsigned = requests.get(f"{base}/lazy-results/stream?payload={payload}", timeout=20)
    check(unsigned.status_code == 403, f"a missing signature is refused ({unsigned.status_code})")


def test_escaping(base: str) -> None:
    print("\n[6] hostile engine output is escaped")
    hostile = "<img src=x onerror=alert(1)>"
    html = search(base, f"{hostile} lazy", "engines=mockslow").text
    check("<img src=x onerror" not in html, "the first page does not contain raw markup from the engine")

    config = page_config(html)
    check(config is not None, "the slow engine is streamed for the hostile query")
    if not config:
        return

    with requests.get(base + config["streamUrl"], stream=True, timeout=(10, 60)) as response:
        fragment = ""
        for _offset, event, data in sse_events(response):
            if event == "engine" and data.get("engine") == "mockslow":
                fragment = data.get("html") or ""
                break

    check(fragment != "", "the slow engine delivered a fragment for the hostile query")
    check("<img src=x onerror" not in fragment, "the streamed fragment does not contain raw markup from the engine")
    check("&lt;img" in fragment, "the streamed fragment contains the escaped markup")


def test_suspended_engine(base: str) -> None:
    print("\n[7] an engine the first paint suspended is asked again")
    html = search(base, "lazy", "engines=mockslow").text
    config = page_config(html)
    check(config is not None, "the slow engine is streamed")
    if not config:
        return

    # SearXNG suspends an engine whose request was cut off by the first paint.
    # Wait until that suspension is in place, then stream: the engine must still
    # deliver the results it was cut off from.
    time.sleep(0.6)

    with requests.get(base + config["streamUrl"], stream=True, timeout=(10, 60)) as response:
        check(response.status_code == 200, f"the stream answers with 200 (got {response.status_code})")
        batch: dict[str, t.Any] = {}
        for _offset, event, data in sse_events(response, limit=20):
            if event == "engine" and data.get("engine") == "mockslow":
                batch = data
                break

    measure(f"streamed while suspended: status={batch.get('status')} count={batch.get('count')}")
    check(batch.get("status") == "ok", "the suspended engine is asked again and answers")
    check(batch.get("count", 0) >= 1, "the suspended engine delivers its results")
    check(SLOW_URL in (batch.get("html") or ""), "the results of the suspended engine are on the page")


# ---------------------------------------------------------------- harness


def wait_for(url: str, timeout: float, name: str) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(url, timeout=5).status_code < 500:
                print(f"  {name} is up: {url}")
                return
        except requests.RequestException:
            pass
        time.sleep(0.5)
    raise SystemExit(f"{name} did not come up within {timeout}s: {url}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("SEARXNG_BASE_URL", "http://127.0.0.1:8899"))
    parser.add_argument("--manage", action="store_true", help="start the mock engines and SearXNG")
    parser.add_argument("--mock-port", type=int, default=8770)
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--settings", default=str(ROOT / "tests" / "settings.test.yml"))
    parser.add_argument("--timeout", type=float, default=90.0, help="seconds to wait for the servers")
    args = parser.parse_args(argv)

    processes: list[subprocess.Popen] = []
    base = args.base_url

    try:
        if args.manage:
            base = f"http://127.0.0.1:{args.port}"
            mock = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "tests" / "mock_engines.py"),
                    "--port",
                    str(args.mock_port),
                    "--delay",
                    "fast=0.2",
                    "--delay",
                    "medium=1.2",
                    "--delay",
                    "slow=4.0",
                ]
            )
            processes.append(mock)
            wait_for(f"http://127.0.0.1:{args.mock_port}/healthz", 20, "mock engines")

            env = {
                **os.environ,
                "SEARXNG_SETTINGS_PATH": args.settings,
                "MOCK_PORT": str(args.mock_port),
            }
            searxng = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "flask",
                    "--app",
                    "searx.webapp",
                    "run",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(args.port),
                    "--no-reload",
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            processes.append(searxng)
            wait_for(f"{base}/healthz", args.timeout, "searxng")

        harness = test_first_paint(base)
        test_progressive(base, harness.get("config"))
        test_api_untouched(base)
        test_no_injection(base)
        test_signature(base, harness.get("config"))
        test_escaping(base)
        test_suspended_engine(base)
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()

    print("\n=== summary ===")
    for line in measurements:
        print(f"  {line}")
    if failures:
        print(f"\n{len(failures)} check(s) FAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
