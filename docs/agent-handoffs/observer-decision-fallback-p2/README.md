# Observer decision fallback P2

The decision model occasionally returns a short Japanese utterance directly instead of the required JSON object. Today this becomes a parse failure and wastes the evaluation. Recover only outputs that are unmistakably short natural dialogue.

## Required behavior

- Existing valid JSON behavior is unchanged.
- On JSON parse failure, adopt a short Japanese utterance of one or two sentences as `should_speak=true`.
- Reject explanatory prose, Markdown, JSON-like fragments, system/report wording, blank output, non-Japanese output, and overly long output.
- The fallback is shared automatically by screen and relationship decisions through `_post_speech_decision`.
- Add no retries, model calls, translations, or prompt changes.
- Log a compact structured outcome: `valid_json`, `adopted_plain_dialogue`, or `rejected_invalid_output`.

## Ownership

Cursor may modify only:

- `app/perception/observer.py`
- focused unit tests for this behavior
- `docs/agent-handoffs/observer-decision-fallback-p2/integration-notes.md`

Do not commit or push. Preserve all unrelated files.
