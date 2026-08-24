# Cursor Task — P3.4 Curator and Background Wiring

Implement P3.4 from the approved spec/plan using TDD. This is integration wiring, not a redesign of P3.1-P3.3.

## Allowed files

- `app/agent/memory_curator.py`
- `app/agent/memory_curation_worker.py`
- Create a narrowly scoped maintainer worker module if needed under `app/agent/`
- `app/ui/pet_window.py`
- `tests/unit/test_memory_curator.py`
- `tests/ui/test_pet_window.py`
- Create `tests/integration/test_core_profile_maintainer_flow.py`
- Cursor section of `docs/agent-handoffs/core-profile-maintainer-p34/integration-notes.md`

Do not change P3.1-P3.3 core modules unless an actual interface defect blocks integration; if blocked, stop and report rather than expanding scope. Do not touch `.gitignore`, character/private/runtime data, API client, TTS, Observer behavior, commit, or push.

## Required behavior

- Ordinary curation may emit bounded `core_candidate` operations but must no longer directly create/update `core_profile`.
- Accept only fictional-test candidate payloads matching the P3.1 queue contract. Existing semantic/episodic/procedural/session operations remain unchanged.
- Pass candidate payloads through `MemoryCurationResult`; persist them after curation succeeds.
- After persistence, asynchronously schedule a dedicated maintainer worker only when P3.3 admission indicates work. Never execute model maintenance in the reply/TTS/Observer/recall path.
- Only one maintainer worker/thread globally. Busy means defer with candidates preserved; failures are metadata-only and must not turn successful ordinary curation into failure.
- Clean worker/thread lifecycle on success/failure/cancel/window shutdown, following existing worker patterns without reusing the curation thread.
- Initialize maintainer dependencies from the current scoped MemoryStore, memory-curation model client, paths, settings, queue/state, and a small completion adapter. Do not expose history/mood/card/intimacy to maintainer.
- Ensure settings/model refresh updates the maintainer client/settings safely.
- Integration fixtures: bilateral relationship confirmation applies; transient jealousy/intimacy does not create explicit core; repeated observed behavior waits for thresholds; corrected address replaces rather than appends; repeated affection yields keep.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_memory_curator.py tests/unit/test_core_profile_candidates.py tests/unit/test_core_profile_maintainer.py tests/ui/test_pet_window.py tests/integration/test_core_profile_maintainer_flow.py -q
git diff --check
```

If Cursor Shell is denied, report `implemented-unverified` and exact denied commands. Never invent evidence.

