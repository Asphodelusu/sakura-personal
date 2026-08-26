# Cursor Task — Isolate pytest runtime logs

Use `cursor-grok-4.6-high-fast`. Implement only test-infrastructure isolation; do not
change production logging behavior.

## Evidence and root cause

- `app.core.debug_log._FILE_LOG_PATH` defaults to the repository runtime log.
- `debug_file_enabled()` follows the user's real configuration.
- `_get_file_logger()` caches a rotating handler by path/signature.
- `_close_file_logger_for_tests()` already exists to close/reset that handler.
- Running `test_relationship_timer.py` plus
  `test_observer_plain_dialogue_fallback.py` added 17 lines to the production log.
- `test_observer_ledger.py` itself patches Observer `debug_log` and added zero lines.

## Allowed files

- `tests/conftest.py`
- `tests/unit/test_debug_log.py` or one narrowly named new test-infrastructure test
- this batch's `integration-notes.md` Cursor section

Do not modify `app/`, production/runtime data, user configuration, `.gitignore`, or
existing channel/shutdown changes. Do not truncate, rotate, delete, or rewrite any log.

## Required behavior

1. Every pytest test gets a file-log path under pytest's temporary directory rather
   than `D:/sakura/data/logs`.
2. Close/reset the cached file logger before installing the per-test path and again
   during teardown so Windows file handles do not leak.
3. Preserve terminal/GUI logging behavior unless isolation inherently requires a
   test-only patch.
4. Existing `test_debug_log.py` cases that override `_FILE_LOG_PATH`, rotation limits,
   and enabled settings continue to pass.
5. Tests must never modify the existing production log even when the user's
   `SAKURA_DEBUG_FILE` setting is enabled.

## Workflow

1. Record current git status and preserve all existing files.
2. Establish a RED/acceptance probe using a representative real `debug_log` path;
   explain why it fails before the fixture. Do not alter or delete the production log.
3. Add the smallest autouse fixture in `tests/conftest.py`, using pytest facilities and
   `_close_file_logger_for_tests()`.
4. Run `tests/unit/test_debug_log.py`, the relationship/fallback pair, then full
   `tests/unit tests/ui` and `git diff --check`.
5. Verify production runtime-log size/line count is unchanged across the representative
   and full runs.
6. Fill only the Cursor section of `integration-notes.md`.

## Git policy

- Do not commit.
- Do not push.
- Do not stage unrelated files.

