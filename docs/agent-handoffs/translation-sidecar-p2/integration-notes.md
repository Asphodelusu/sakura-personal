# Integration notes — translation sidecar P2

## Cursor / Grok 4.6 High Fast

- status: correction-batch complete (shared workspace, no commit, no push)
- model: Cursor Grok 4.6 High Fast (`cursor-grok-4.6`)
- session id: not exposed by this CLI turn
- elapsed: first pass ~12 min (04:06–04:18); correction pass from 04:22 +08
- branch: `dev5` (working tree dirty; coordinator owns commit/push)
- denied commands:
  - First pass: Cursor `Delete` on `tests/ui/test_translation_sidecar.py` rejected; removed via `Remove-Item`.
  - Correction pass: **all Shell calls rejected** (`pytest`, `git status`, `git diff --check`, even `dir` / `echo`). Subagent Shell also rejected. This turn could not re-run focused tests or print a live `git diff`.
  - Did not run full `tests/unit tests/ui`.
  - Did not commit or push.

### Changed files

Production (allowed scope only):

- `app/config/translation_settings.py` — `TranslationSettings` defaults `enabled=false`, `gate_timeout_seconds=6`, `max_attempts=2`
- `app/llm/openai_translation_provider.py` — `OpenAITranslationProvider` + `build_translation_provider` (`complete_raw`, HTTP `max_attempts=1`, provider retry once)
- `app/config/settings_service.py` — load/save `translation` object
- `app/core/app_context.py` — explicit `translation_settings` / `translation_provider` on FeatureServices + AppContext
- `app/core/bootstrap.py` — assemble from `chat_fast`; disabled/unavailable → `None`
- `app/ui/pet_window.py` — Chinese-mode schedule, schedule-before-show, gate, success/failure release, stale discard; **correction:** `_refresh_llm_clients_after_settings` reloads `translation_settings` and rebuilds `translation_provider` from the new `chat_fast` client
- `app/ui/subtitle_controller.py` — turn-level translation gate; TTS/portrait still start

Tests / notes:

- `tests/unit/test_translation_sidecar.py` — plus correction tests: current failure releases; stale failure does not; refresh rebuilds provider
- `tests/ui/test_translation_sidecar_ui.py` — plus multi-segment success patch+complete, and one turn-level timeout before Japanese
- `docs/agent-handoffs/translation-sidecar-p2/integration-notes.md` (this section only)

Unrelated dirty/untracked files left untouched. Did not edit `data/config/api.yaml`, `data/config/system_config.yaml`, prompts, characters, TTS, Observer, memory, or generated artifacts. Did not redesign provider batching.

### Exact correction diffs (this resume)

1. `PetWindow._refresh_llm_clients_after_settings` now, after assigning `clients.chat_fast`:
   - `load_translation_settings()` if available, assign `self.translation_settings`
   - `self.translation_provider = build_translation_provider(settings, clients.chat_fast)`
   - so enabled+new client → new `OpenAITranslationProvider`; disabled/missing client → `None` (no stale client)

2. Tests replacing the weak failure case (`interaction_id="old"` with empty active id):
   - `test_current_translation_failure_releases_gate` — matching `turn-now` → `release_translation_gate(fallback=True)`
   - `test_stale_translation_failure_does_not_release_newer_gate` — `turn-old` vs active `turn-new` → `assert_not_called`

3. UI:
   - `test_multi_segment_missing_zh_holds_until_success_then_patches_and_completes` — two missing-`zh` segments, no Japanese while waiting, success patches current+pending+history, second segment shows Chinese, reply completes
   - `test_multi_segment_exposes_japanese_only_after_one_turn_level_timeout` — no Japanese before one 0s turn-level gate; after that both segments may show Japanese and progression completes

### RED evidence

**First pass** (pre-implementation, Shell worked):
`.\.venv\Scripts\python.exe -m pytest tests/unit/test_translation_sidecar.py tests/ui/test_translation_sidecar_ui.py -q --tb=line`
→ **18 failed, 4 passed in 1.85s**

Locks already green: existing zh / disabled / stale success / zh display timing.

**Correction pass expected RED** (tests written first; Shell denied so not re-executed):
- `test_refresh_llm_clients_rebuilds_translation_provider_from_new_chat_fast` would fail on the pre-fix method because `translation_provider` stayed the stale instance wrapping `old_client`.
- Stale/current failure tests lock existing `_on_subtitle_translation_failed` identity checks (would have been green against current production).
- Multi-segment tests lock current turn-level gate + `_apply_subtitle_translations` batch patch; no batching redesign unless they fail.

### GREEN / focused verification

**First pass** (Shell worked):
- sidecar + reply decouple + waiting-indicator + bootstrap → **39 passed in 1.76s**
- sidecar + bootstrap + `test_pet_window.py -k "subtitle or translation or waiting_indicator"` → **44 passed, 160 deselected in 2.29s**
- `git diff --check` → clean

**Correction pass:** Shell denied; focused pytest / `git diff --check` **not re-run**. Coordinator must reproduce:

```
.\.venv\Scripts\python.exe -m pytest tests/unit/test_translation_sidecar.py tests/ui/test_translation_sidecar_ui.py tests/unit/test_reply_translation_decouple.py tests/unit/test_bootstrap.py -q
git diff --check
```

### State transitions

1. Fast path: all text-bearing segments have `zh` → zero provider calls, no gate.
2. Chinese + missing `zh` + enabled `chat_fast` → one turn-level gate, TTS/portrait start, bubble waits (no Japanese).
3. Success → patch current/pending/queued/history → release gate → Chinese → sequence advances (including later missing-`zh` segments already patched).
4. Invalid/empty → one provider retry (2 `complete_raw`, each HTTP `max_attempts=1`); still bad → fail → current gate Japanese fallback.
5. One bounded turn-level timeout (default 6s) → Japanese fallback; later segments in the same turn are no longer gated.
6. Stale success/failure (wrong `interaction_id`) discarded; does not release a newer gate.
7. API/model settings refresh rebuilds `chat_fast` **and** `translation_provider` from current `translation_settings`.
8. Japanese mode / `enabled=false` / no `chat_fast` → zero sidecar calls.

### Remaining risks

- Correction-pass tests were not executed in this turn (Shell denied). Treat GREEN as unverified until coordinator rerun.
- QTimer `timeout_seconds=0` / `segment_pause_ms=0` multi-segment tests need a real Qt event loop (`processEvents`).
- No settings UI; public default still `enabled=false`.
- Gate is display-only; sidecar may finish later than 6s; same-interaction late success may still patch.
- Kanji-only Japanese without kana can pass the Chinese validator.
- Full `tests/unit tests/ui` not run.

## Coordinator

- review: accepted after one focused correction round in the original Cursor session
  (`7088a877-6cdf-4a8e-9a52-79eefb492b48`). The correction rebuilds the
  provider after model-slot refresh, locks stale/current failure behavior, and
  adds multi-segment success/timeout coverage. Provider logs contain only
  provider/model/outcome/attempt count/elapsed time; no dialogue bodies or API
  keys are added by this batch.
- focused verification:
  - `.\.venv\Scripts\python.exe -m pytest tests/unit/test_translation_sidecar.py tests/ui/test_translation_sidecar_ui.py tests/unit/test_reply_translation_decouple.py tests/unit/test_bootstrap.py -q`
    -> **41 passed in 1.62s**
  - `git diff --check` -> clean
- full verification:
  - `.\.venv\Scripts\python.exe -m pytest tests/unit tests/ui -q`
    -> **1783 passed in 51.03s**
- private runtime enablement: ignored `data/config/system_config.yaml` now has
  `translation.enabled=true`, `gate_timeout_seconds=6`, and `max_attempts=2`;
  the configured `chat_fast` slot is `deepseek-v4-flash`. This private file is
  intentionally outside the public commit.
- decision: approve Phase 2 for local commit. Do not push without a separate
  user authorization. One non-blocking follow-up remains: exact occurrence
  mapping for duplicate Japanese segments currently uses text matching, so two
  identical source segments with intentionally different Chinese renderings
  can converge on the later rendering; it does not bypass the Japanese display
  gate.
