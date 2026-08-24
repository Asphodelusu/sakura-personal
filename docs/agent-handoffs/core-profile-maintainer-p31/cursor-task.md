# Cursor Task — P3.1 Core Candidate Queue

## Objective

Implement P3.1 from `docs/superpowers/plans/2026-08-24-core-profile-maintainer.md`: a deterministic, scope-isolated core-profile candidate queue. No model call or curator integration belongs in this batch.

## Read first

- `AGENTS.md`
- `docs/superpowers/specs/2026-08-24-core-profile-maintainer-design.md`
- `docs/superpowers/plans/2026-08-24-core-profile-maintainer.md`

## Exclusive files

- Create/modify: `app/agent/core_profile_candidates.py`
- Modify: `app/storage/paths.py`
- Create/modify: `tests/unit/test_core_profile_candidates.py`
- Modify: `tests/unit/test_storage_paths.py`
- Fill only the Cursor section in `docs/agent-handoffs/core-profile-maintainer-p31/integration-notes.md`

## Do not modify

- `.gitignore`
- `app/agent/memory.py`
- `app/agent/memory_curator.py`
- `app/ui/pet_window.py`
- runtime/private memory files
- any file outside the exclusive list

## Required behavior

- Candidate kinds: explicit and observed; only the four formal target sections are accepted.
- Explicit is eligible only with bilateral non-empty excerpts, confidence >= 0.90, and a supported explicit subject category.
- Observed requires >=3 distinct evidence, >=2 batches, >=30 minute span, and average confidence >=0.80.
- Stable hashes make repeat processing idempotent; merge key is target_section + subject_key.
- Clip each excerpt to 160 characters and claim to 240; retain at most 5 unique evidence.
- Scope isolation; maximum 50 candidates per scope; processed retention 7 days; pending expires after 30 days.
- Atomic persistence. Malformed JSON raises a typed error and must never overwrite the source.
- Inject the clock/config where needed for deterministic tests.

## TDD and verification

1. Run `git status --short --branch` and preserve existing `.gitignore` state.
2. Add focused failing tests first and record genuine RED evidence.
3. Implement the minimum production behavior.
4. Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_core_profile_candidates.py tests/unit/test_storage_paths.py -q
git diff --check
```

If Shell execution is denied, report `implemented-unverified` and the exact denied commands. Never invent RED/GREEN evidence.

## Git policy

- Do not commit.
- Do not push.
