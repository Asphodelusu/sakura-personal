# Integration Notes — Relationship Initiative

## Codex integration

- status: reviewed-and-green
- integration commit: `63cb93c` `fix: consume mixed relationship trigger once`
- mixed screen + relationship eligibility now consumes one B opportunity: a spoken screen evaluation records the B spoken cooldown; silence or failure records the 300-second B silent cooldown
- `_relationship_motive` is cleared in `finally`, including screen-evaluation failures
- character switching tolerates profiles without the optional `relationship_guide_path`; the Qt minimal test stub was updated for the new relationship wiring
- focused regression: **32 passed**
- full gate: `.\.venv\Scripts\python.exe -m pytest tests\unit tests\ui -q` → **1689 passed, 6 skipped** in 28.23s
- preserved: pre-existing `.gitignore`; ignored `characters/Sakura/relationship_guide.md`; no push

## Cursor

- status: phase-2-tdd-complete
- branch: `dev5` (ahead 9 of `origin/dev5`; no push)
- plan path: `docs/superpowers/plans/2026-08-24-sakura-relationship-initiative.md`
- interpreter: `.\.venv\Scripts\python.exe`
- model: Cursor Grok 4.6
- constraints kept: no worktrees, no subagents, no force-add, no push; pre-existing `M .gitignore` and unrelated untracked docs left unstaged

### Commits (this worker, newest last)

1. `02fc816` `feat: add relationship initiative config defaults`
   - `app/config/relationship_initiative.py`
   - `app/config/settings_service.py`
   - `tests/unit/test_relationship_initiative_config.py`
2. `d91cad5` `feat: load and archive optional relationship guides`
   - `app/config/character_archive.py`
   - `app/config/character_loader.py`
   - `tests/unit/test_character_archive.py`
   - `tests/unit/test_relationship_guide_loader.py`
3. `d2da8c4` `feat: inject static relationship guide when A is on`
   - `app/agent/prompt_builder.py`
   - `app/agent/runtime.py`
   - `tests/unit/test_relationship_guide_prompt.py`
4. `f3dce6c` `feat: add no-VLM relationship_timer gates`
   - `app/perception/observer.py`
   - `tests/unit/test_relationship_timer.py`
5. `92354a6` `feat: add relationship initiative speech decision`
   - `app/perception/observer.py`
   - `tests/unit/test_relationship_timer.py`
6. `8a78278` `feat: wire relationship initiative cancel and logs`
   - `app/ui/pet_window.py`
   - `app/core/gui_log.py`
   - `app/ui/log_window.py`
   - `tests/unit/test_relationship_initiative_playback.py`
7. `5564edf` `test: lock relationship guide content contract`
   - `tests/unit/test_relationship_guide_content.py`

Task 8 produced no extra production diff; no empty commit.

### RED evidence

- Task 1 (prior turn): missing `app.config.relationship_initiative` / settings loaders, then GREEN after bias copy dropped `禁止` (`禁止清单` → `不增加内容限制`) so `assert "禁止" not in text` could pass. 6 passed.
- Task 2–4 (prior turn + this continuation): loader/archive/prompt missing APIs; timer tests failed on missing `relationship=` / `_relationship_gate_reason` / `_do_relationship_evaluation` / `_dispatch_proactive_tick`. Task 4 GREEN: `tests\unit\test_relationship_timer.py tests\unit\test_proactive_focus.py tests\unit\test_proactive_config.py` → **28 passed**.
- Task 5 RED: `.\.venv\Scripts\python.exe -m pytest tests\unit\test_relationship_timer.py -q` → **1 failed, 7 passed**. Failure: `test_speak_uses_relationship_source_and_independent_cooldown` (`len(spoken) == 0`, no `_post_speech_decision` speak path). Instruction test already green from Task 1.
- Task 5 GREEN: `tests\unit\test_relationship_timer.py tests\unit\test_proactive_focus.py tests\unit\test_proactive_decision_slot.py` → **30 passed**.
- Task 6 RED: `tests\unit\test_relationship_initiative_playback.py -q` → **3 failed, 1 passed**. Failures: stale payload still consumed as `"proactive"`; history source stayed `"proactive"`; `load_relationship_initiative_settings` absent from `pet_window.py`. Busy-drop already passed via the old generic busy gate.
- Task 6 GREEN: playback + timer + intimacy pet window + decision slot + reply-history + gui_log + observer speech prompts → **63 passed**.
- Task 7 RED: `tests\unit\test_relationship_guide_content.py -q` → **1 failed, 1 passed**. Plan-exact body contained FORBIDDEN substrings `台词库` and `先抱、再吻` (used as negations). Reworded those two sentences so the contract can pass without changing the five headings or “no ceiling / no 贴紧” intent.
- Task 7 GREEN: content + loader + prompt → **10 passed**.

### Task 8 regression

- Step 1 targeted new suite: **45 passed** in 1.57s
- Step 2 intimacy + observer: **141 passed** in 1.35s
  - `贴紧` / `苹果` exact-match tests in `test_intimacy_mode.py` remain
  - three-step continuation covered by intimacy pet-window / continuity tests
  - bare observer B-off still collects screen timer/content/window (`test_bare_observer_does_not_collect_relationship_timer`, `test_proactive_focus.py`)
  - packages without `relationship_guide.md` still import (`test_relationship_guide_loader.py`, archive tests)
- Step 3 full unit: `.\.venv\Scripts\python.exe -m pytest tests\unit -q` → **1460 passed** in 24.93s
- `tests\ui` not required by this task; not run
- Human RP checklist (plan Task 8 Step 4) is not agent-runnable

### Ignored / private files written but not staged

- `characters/Sakura/relationship_guide.md` (local L1; `git check-ignore` → `.gitignore:23:characters/`). Not `git add -f`.
- Did not edit `characters/Sakura/character.json`; default path `relationship_guide.md` is enough.
- Did not edit `data/config/system_config.yaml`, `data/intimacy_guide.txt`, `card.md`, runtime chat/memory/mood.

### Remaining risks

- Live Sakura guide stays local/untracked; CI uses `CANONICAL_RELATIONSHIP_GUIDE`.
- No settings UI; A/B/bias changes need YAML or a later UI task. Missing YAML section uses code defaults (A/B on, `natural`, 3600 / 300).
- B silent/failure cooldown is `RELATIONSHIP_SILENT_COOLDOWN_SECONDS = 300.0` (coordinator lock; not the Phase 1 draft 180s).
- `send_message` still ignores input while a normal `ChatWorker` is busy. Generation cancel covers unshown B; it does not preempt an already-shown screen/B line inside `_consume_agent_result` TTS.
- Default-on B in production depends on PetWindow passing loaded settings. Bare `ProactiveObserver()` in tests keeps B off.
- B speech quality is stochastic even with locked prompts.
- Canonical guide wording differs in two sentences from the plan paste so FORBIDDEN substring checks can pass.

### Denied / interrupted commands

- Earlier Phase 2 turn: Shell Auto-review blocked pytest/git until Smart Auto-review was enabled. Task 1 tests+code were written before watching RED; this continuation inspected that tree, then ran the planned RED/GREEN/git commands.
- This continuation: no denied pytest/git commands; no push; no force-add.

### Elapsed

- Phase 1 plan (prior Cursor section): ~40 minutes
- Phase 2 implementation (prior turn Tasks 1–3 + this continuation Tasks 4–8): this continuation ~1 hour after resume; total Phase 2 wall time across the interrupted session is longer
- model: Cursor Grok 4.6
