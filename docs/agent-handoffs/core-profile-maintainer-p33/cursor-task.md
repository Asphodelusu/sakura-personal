# Cursor Task — P3.3 Maintainer Core

Implement the synchronous/testable P3.3 maintainer core with TDD. P3.4 will add the background worker and UI/curator wiring.

## Allowed files

- Create `app/agent/core_profile_maintainer.py`
- Create `tests/unit/test_core_profile_maintainer.py`
- Modify `app/agent/core_profile_candidates.py`, `tests/unit/test_core_profile_candidates.py`
- Modify `app/storage/paths.py`, `tests/unit/test_storage_paths.py`
- Modify `app/config/settings_service.py`, `tests/unit/test_settings_service.py`
- Modify `data/config/system_config.yaml`
- Fill only the Cursor section of `integration-notes.md`

Do not modify memory.py, curator/worker/UI, API/JSON helpers, character/private/runtime data, or P3.4 paths. Do not commit/push.

## Required architecture

- Backend-only `CoreMaintainerSettings` defaults/normalization and candidate-config conversion.
- Metadata-only atomic `CoreMaintainerStateStore` at `data/memory/core_maintainer_state.json`; corrupt state fails closed without overwrite.
- Queue transaction `mark_processed(applied_ids, reviewed_ids)` with all-or-nothing validation under existing path/file locks.
- Pure `eligible_since`: explicit at first truly bilateral qualifying evidence; observed at first prefix that satisfies all thresholds.
- `CoreMaintainerScheduler` does admission/selection/global single-flight only; it never starts threads. Preserve pending triggers through cooldown. New explicit batch can bypass cooldown once even on API failure; pause outranks bypass.
- Stable candidate order: triggering explicit, other explicit, stale, pending trigger, other observed; then eligible time/id; max 5.
- Synchronous `CoreProfileMaintainer.run_once(...)`: schedule, read only `memory_store.core_profile()`, build minimal profile-sections + candidates prompt, one completion, strict parse/validation, at most one section patch, then atomic queue transition, metrics, finally lease release.
- Strict JSON keys/types and operations keep/refine/replace/remove/migrate_legacy. Ordinary non-keep max 2; migration plus max one ordinary change; candidate/evidence/section binding.
- Deterministic validation: base token, still-pending/eligible references, normalized no-op -> keep, 40% shrink, protected anchors, remove needs explicit correction, reject system/report language. Do not pretend deterministic code can solve semantic paraphrase.
- API/timeout leaves pending and does not increase validation streak. Validation rejection increments; 3 pauses 24h. Storage failures do not count. Valid all-keep resets streak.
- Core then queue is an acknowledged two-file boundary: queue failure returns partial_commit; next run repairs pending IDs already present in core metadata without rewriting core.
- Metrics are metadata only; never profile/claim/excerpt/prompt/raw response/reason. Usage is optional/null because current adapter lacks exact tokens.
- Config under `memory.core_maintainer`; no UI/save method.

## Essential tests

Cover disabled/no eligible zero-call, observed trigger/cooldown, three eligible, stale based on eligible_since, explicit bypass once, pause, global busy, selection cap/order, prompt privacy snapshot, strict parser, evidence/section binding, operation limits/migration, no-op, shrink/anchors/remove/language, failure categories/pause reset, queue transaction rollback, partial-commit recovery, corrupt queue/state, metadata-only metrics, and config normalization. Use only fictional tmp fixtures.

Run if Shell works:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_core_profile_maintainer.py tests/unit/test_core_profile_candidates.py tests/unit/test_settings_service.py tests/unit/test_storage_paths.py tests/unit/test_core_profile_schema_v2.py -q
git diff --check
```

If Shell is denied, report `implemented-unverified`; never invent evidence.
