# Cursor Task — P3.2 Guarded Core Section Patches

Implement P3.2 from the approved spec and implementation plan using TDD.

## Read

- `AGENTS.md`
- `docs/superpowers/specs/2026-08-24-core-profile-maintainer-design.md`
- `docs/superpowers/plans/2026-08-24-core-profile-maintainer.md`
- existing core-profile V2 code and tests

## Exclusive files

- `app/agent/memory.py`
- `tests/unit/test_core_profile_schema_v2.py`
- Cursor section of `docs/agent-handoffs/core-profile-maintainer-p32/integration-notes.md`

Do not touch `.gitignore`, candidate queue files, curator/UI, runtime/private data, or any other file. Do not commit or push.

## Required TDD scope

- Add `MemoryStore.patch_core_profile_sections(...)` under the existing lock/save boundary.
- Four formal sections only, fixed rendering order, deterministic `content` and `memory` cache.
- Re-read V2 under lock; reject mismatched `base_updated_at`; preserve `created_at`; update `updated_at`, source and candidate metadata; backup before successful write.
- Ordinary operations change at most two sections. Normalized no-op writes nothing.
- Legacy migration only moves every original sentence exactly once into formal sections; no rewrite/add/delete; preserve names, forms of address, quoted text, and numbers; remove `legacy` only after validation succeeds.
- Failure must leave primary and backup unchanged.

Run if Shell is available:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_core_profile_schema_v2.py -q
git diff --check
```

If denied, report `implemented-unverified` without inventing evidence.
