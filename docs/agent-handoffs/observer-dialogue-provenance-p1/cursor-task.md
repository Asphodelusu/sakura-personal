# Cursor Desktop Task — Observer 对话事实来源与短期回应账本 P1

## Objective

从根因修复 Observer 把自己的旧判断当成当前对话事实的问题。用户回答后，Sakura 可以认为回答含糊、拒绝或暂缓，但不能再声称“完全没有回应”。不得使用吃饭等主题关键词补丁，不增加常态模型调用。

## Starting state

- Repository: `D:\sakura`
- Expected branch: `dev5`
- Expected starting HEAD: the clean `dev5` HEAD stated in the coordinator's transfer message
- Expected status: clean; the exact ahead count is also stated in the transfer message
- Python: `D:\sakura\.venv\Scripts\python.exe`

If starting HEAD/status differs, stop and report before editing. Preserve all unrelated changes.

## Required reading

- `docs/agent-handoffs/CONTEXT.md`
- this batch `README.md` and `acceptance-tests.md`
- `docs/superpowers/specs/2026-08-29-observer-dialogue-provenance-design.md`
- `docs/superpowers/plans/2026-08-29-observer-dialogue-provenance-p1.md`

Follow the implementation plan task-by-task. The plan's exact interfaces are the shared contract unless a source-level incompatibility is proven; if so, stop and write the mismatch in `integration-notes.md` rather than silently redesigning it.

## Exclusive files

- `app/perception/observer.py`
- `app/perception/sensory_impression.py` only if a regression requires a minimal freshness correction
- `app/storage/chat_history.py`
- `app/ui/pet_window.py`
- `tests/unit/test_chat_history_search.py`
- `tests/unit/test_observer_exchange_ledger.py` (new)
- `tests/unit/test_observer_history_semantics.py`
- `tests/unit/test_observer_speech_prompts.py`
- `tests/unit/test_sensory_impression.py`
- `tests/unit/test_observer_ledger.py`
- `tests/unit/test_relationship_timer.py`
- `tests/unit/test_reply_history_channel_persistence.py`
- `tests/ui/test_pet_window.py`
- this batch `integration-notes.md`, Cursor Desktop section only

## Do not modify

- `characters/`, `data/`, runtime logs/databases, API configuration, TTS/subtitle code
- memory curation, commitment persistence, relationship personality/guide
- scheduling intervals, cooldown/backoff, screen/relationship arbitration
- Phase 2 agreement-provenance prompt guidance or Phase 3 semantic classifier
- other handoff batches or their integration notes

## Required workflow

1. Run `git status --short --branch` and verify the starting state.
2. Use TDD: capture a real RED before each production behavior change.
3. Implement the smallest P1 design from the plan.
4. Run the plan's focused gate and `git diff --check`.
5. Fill only the Cursor Desktop section of `integration-notes.md`.
6. Stop for Codex review.

Do not weaken old assertions merely to make tests pass. Tests must exercise real intermediate contracts and explicit stubs; do not use permissive mocks that accept missing callbacks or arbitrary attributes.

## Git policy

- Local commit: forbidden for this batch; leave a reviewable working-tree diff.
- Push: forbidden.
- Full `tests/unit tests/ui`: do not run; Codex runs it once after review.

## Result fields

- actual model and mode
- elapsed time
- starting and ending status
- modified files
- RED command and exact failure for each task
- GREEN commands and pass totals
- implemented state transitions
- API-call-count evidence
- scope deviations or blocked interfaces
- remaining risks
