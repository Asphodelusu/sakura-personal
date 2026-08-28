# 5.3 Codex Spark Task — Persist autonomous reply history channel

## Objective

Use TDD to preserve `message_source` when assistant reply segments are written to
chat history. This enables later history/session logic to distinguish ordinary,
screen-observer (`proactive`), and relationship-initiative (`relationship`) replies.

## Current evidence

- `app/ui/pet_window.py::_consume_agent_result()` accepts `message_source` and writes
  it to the in-memory `assistant_msg["source"]`.
- The same method calls `_record_assistant_reply_history(reply, _debug=...)` without
  forwarding that value.
- `_record_assistant_reply_history()` calls `_record_history()` once for each non-empty
  segment.
- `_record_history()` calls `HistoryStore.append()` without its existing `channel`
  parameter.
- `app/storage/chat_history.py` is the authoritative storage contract. Inspect it;
  do not invent a second persistence field.

## Exclusive modification scope

- Production: `app/ui/pet_window.py`
- Tests: the smallest relevant existing unit/UI history test module, or one focused
  new test module under `tests/unit/` or `tests/ui/`
- Report: only the Spark section of
  `docs/agent-handoffs/spark-history-channel/integration-notes.md`

`app/storage/chat_history.py` is read-only unless inspection proves an actual missing
interface blocks the task. If blocked, stop and report instead of expanding scope.

## Required behavior

1. `message_source="proactive"` persists every non-empty assistant segment with
   `channel="proactive"`.
2. `message_source="relationship"` persists every non-empty assistant segment with
   `channel="relationship"`.
3. Empty/default `message_source` preserves the existing ordinary-history default;
   it must not be labeled proactive or relationship.
4. Existing in-memory `source`, segment filtering, ordering, returned history IDs,
   `_debug` placement, subtitle translation backfill, tone, portrait, and translation
   fields remain unchanged.
5. Do not normalize the two autonomous sources into one value.

## Do not modify

- Observer or relationship decision/arbitration logic
- prompts, persona/card/guide files, memory semantics, API clients, TTS, configuration
- runtime/private data, `.gitignore`, unrelated tests or documentation
- existing commits or unrelated dirty files

## Required workflow

1. Confirm the active model is **5.3 Codex Spark**. If it is not, stop and report.
2. Run `git status --short --branch`; preserve everything already present.
3. Inspect the actual `HistoryStore.append()` signature and nearby history tests.
4. Add a focused test that fails for the current missing-channel behavior and record
   the exact RED command and failure before editing production code.
5. Implement the smallest compatible change.
6. Run the focused tests using `\.venv\Scripts\python.exe`.
7. Run the most relevant adjacent tests and `git diff --check`.
8. Review the final diff for scope and fill only the Spark section of
   `integration-notes.md`.

## Git policy

- Local commit: forbidden
- Push: forbidden
- Reset, checkout-discard, rebase, amend, or cleanup of unrelated files: forbidden

## Required result fields

- status and confirmation of the actual model
- elapsed working time if visible
- modified files
- root cause
- exact RED command and failure
- exact GREEN/adjacent commands and results
- `git diff --check` result
- scope review and remaining risks
- any ambiguity or correction needed from the coordinator

