# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/2.0.0.html).

## [1.0.0] - 2026-09-29

### Added

- First-paint budget for HTML searches: engines that answer within `first_paint_timeout` (default `1.2` seconds) are rendered in the initial response.
- Server-sent-events streaming of the engines that timed out on first paint, with keep-alive `heartbeat` comments (default `10.0` seconds) while the remaining engines are still running.
- Streamed results rendered with the instance's own result templates and, like on the first page, escaped and query-highlighted, so engine-provided markup cannot reach the browser.
- Engines that SearXNG suspended because the first paint cut their request off are resumed for the second pass (`resume_timeout_suspended`, default `true`); suspensions for other reasons (rate limit, CAPTCHA, access denied) are respected and reported as `skipped`.
- Signed payloads on the streaming endpoint so it cannot be used as a search API (`require_token`, default `true`).
- Deduplication of streamed results across engines (`deduplicate`, default `true`).
- Sidebar engine table updated when a slow engine answers, so pending engines are replaced by their status instead of staying greyed out.
- Optional per-engine labels in the UI (`engine_labels`, default `true`).
- Configuration via a `lazy_results:` settings section or `SEARXNG_LAZY_RESULTS_*` environment variables: `enabled`, `first_paint_timeout` (default `1.2`), `stream_timeout` (default `8.0`), `stream_grace` (default `3.0`), `max_concurrent_engines` (default `4`), `max_stream_engines` (default `12`), `heartbeat` (default `10.0`), `engine_labels` (default `true`), `deduplicate` (default `true`), `require_token` (default `true`), `resume_timeout_suspended` (default `true`), `secret` (default `null`).
