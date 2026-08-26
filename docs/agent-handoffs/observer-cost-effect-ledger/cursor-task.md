# Cursor task

Implement `README.md` with strict RED-GREEN TDD using `cursor-grok-4.6-high-fast`.

First map the existing screen and relationship completion branches and the P2 decision parser. Prefer a small per-attempt state object or local settlement helper that guarantees exactly-once logging without rewriting control flow. Capture response `usage` only if it is already present in the OpenAI-compatible response. Do not log private text or add persistent storage beyond existing `debug_log`.

Tests must cover screen speak/silent/error/dedup, relationship speak/silent/stale cancellation, exactly once, usage present/missing, and a denylist assertion that ledger data contains no body fields or sample private strings. Run focused tests, `tests/unit tests/ui`, and `git diff --check` if Shell is available. Fill the Cursor section of `integration-notes.md`. Do not commit or push.
