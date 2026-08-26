# Observer cost/effect ledger

Add one compact, structured `debug_log("ObserverLedger", "评估结算", data)` record for each actual screen or relationship evaluation attempt.

## Required fields

- `path`: `screen` or `relationship`
- `source`: same stable source name used for proactive speech
- `outcome`: stable machine value such as `speak`, `silent`, `decision_error`, `dedup_skip`, or `stale_cancel`
- model identities relevant to that path
- elapsed timings available from real measured stages: VLM, decision, total
- `decision_format` when known: `valid_json`, `adopted_plain_dialogue`, `rejected_invalid_output`
- prompt/completion/total usage only when returned by the provider; omit or use null when unavailable, never estimate
- trigger names for screen evaluations

## Constraints

- Exactly one settlement record per actual evaluation attempt.
- Do not ledger every busy/desktop-idle polling tick; existing gate logs are enough.
- Do not add a database, new file writer, settings UI, model call, retry, or background task.
- Do not include screen text, conversation text, visual/reaction summaries, comments, translations, reasons, prompts, or other private bodies.
- Reuse existing `debug_log`; when file logging is disabled the runtime overhead should remain a small dictionary construction and existing GUI log call.
- Preserve all current behavior and human-readable logs.

## Ownership

Cursor may modify only:

- `app/perception/observer.py`
- focused unit tests for this ledger
- `docs/agent-handoffs/observer-cost-effect-ledger/integration-notes.md`

Do not commit or push. Preserve unrelated files.
