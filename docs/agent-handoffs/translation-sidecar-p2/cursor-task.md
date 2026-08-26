# Cursor task — translation sidecar P2

Implement this batch with strict RED -> GREEN TDD. Read `README.md`, the accepted Phase 1
decision in `docs/agent-handoffs/translation-decoupling/provider-adapter-decision.md`, and
the existing tests before editing.

## Required behavior

1. Preserve the fast path: if every text-bearing segment already has `zh`, do not call the
   translation provider and do not change display timing.
2. In Chinese subtitle mode, missing `zh` schedules a real sidecar backed by the explicit
   `chat_fast` client. Japanese subtitle mode does not translate.
3. Reuse `OpenAICompatibleClient.complete_raw`; do not duplicate HTTP/key loading. The
   provider asks only for natural Simplified Chinese. Validate non-empty Chinese output.
4. A failed/empty/invalid translation gets at most one provider-level retry (two attempts
   total). Do not rely on the API client's default three HTTP attempts on top of this; keep
   the total bounded and observable.
5. Japanese TTS and portrait playback start normally. While the current segment lacks `zh`
   and an active translation exists, the Chinese bubble keeps the existing waiting indicator
   (or an equivalent neutral ellipsis) rather than displaying Japanese.
6. Translation success patches current/pending/queued segments and history as Phase 1
   intended, then displays Chinese. A configurable bounded gate (default 6 seconds) releases
   the Japanese fallback so segment progression can never deadlock.
7. Provider failure also releases the current gate immediately; late/stale results must not
   overwrite a newer interaction. Log provider/model, outcome, attempts, and elapsed time,
   never source/translation bodies or credentials.
8. Add a small `translation` settings object loaded from `system_config.yaml`. Defaults must
   be safe for the public project (`enabled=false`, `gate_timeout_seconds=6`,
   `max_attempts=2`); no settings UI in this batch. Bootstrap/AppContext injection must be
   explicit rather than a PetWindow-only dynamic attribute.
9. Keep the existing one-time structured compose for plain non-JSON Japanese because it
   still supplies segments/tone/portrait. Do not alter main reply prompts in this batch.

## Allowed production scope

- `app/llm/translation_provider.py` and/or one new focused provider module
- `app/core/app_context.py`
- `app/core/bootstrap.py`
- `app/config/settings_service.py` and one focused translation-settings module if useful
- `app/ui/pet_window.py`
- `app/ui/subtitle_controller.py`
- focused tests under `tests/unit/` and `tests/ui/`
- this batch's `integration-notes.md`

If another production file is genuinely required, stop and report why before expanding.
Do not touch `data/config/api.yaml`, `data/config/system_config.yaml`, API keys, prompts,
characters, TTS implementation, Observer, memory, generated artifacts, commit, or push.

## TDD and verification

Write and run focused RED tests first. At minimum cover:

- existing `zh` makes zero provider calls;
- missing `zh` in Chinese mode starts translation;
- Japanese mode makes zero calls;
- pending Chinese subtitle never exposes Japanese while TTS starts;
- success patches current/pending/history and releases progression;
- provider invalid/empty output retries once;
- provider failure and 6-second timeout release Japanese without deadlock;
- stale interaction result is discarded;
- bootstrap/config round-trip and disabled/unavailable provider behavior.

Run focused translation/subtitle/bootstrap tests and `git diff --check`. Do **not** run the
full `tests/unit tests/ui` gate; the coordinator owns that single final integration run.

Fill only the Cursor section of `integration-notes.md` with changed files, exact RED evidence,
focused results, state transitions, remaining risks, model ID/session ID, elapsed time,
and denied commands. Stop without commit or push.
