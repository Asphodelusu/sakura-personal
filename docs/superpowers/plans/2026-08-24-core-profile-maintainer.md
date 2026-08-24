# Core Profile Maintainer Implementation Plan

> **For implementers:** Follow `docs/superpowers/specs/2026-08-24-core-profile-maintainer-design.md`, use the repository `.venv`, and complete each phase with RED/GREEN evidence, review, and its own commit. Do not push until the integrated gate is approved.

**Goal:** Maintain Sakura's core profile as a slowly changing, grounded understanding of the present relationship instead of an append-only conversation summary.

**Architecture:** Ordinary curation emits compact candidates into a deterministic queue. A background maintainer receives only the current V2 core profile and eligible candidates, proposes section operations, and writes through a guarded `MemoryStore` patch boundary. Conversation response latency remains unchanged.

**Tech stack:** Python 3.11, dataclasses/JSON, existing atomic memory persistence, PySide6 worker scheduling, pytest.

---

## P3.1 — Candidate queue and deterministic eligibility

**Files:**

- Create: `app/agent/core_profile_candidates.py`
- Modify: `app/storage/paths.py`
- Create: `tests/unit/test_core_profile_candidates.py`
- Modify: `tests/unit/test_storage_paths.py`

1. Write failing tests for the `core_review_queue.json` path, explicit bilateral-confirmation eligibility, observed thresholds (3 evidence, 2 batches, 30-minute span, average confidence 0.80), and every threshold's negative boundary.
2. Add failing tests for stable candidate/evidence hashes, `target_section + subject_key` merging, duplicate evidence suppression, five-evidence cap, scope isolation, excerpt/claim clipping, expiration, and 50-candidate pruning.
3. Add failing corruption and persistence tests: atomic save, malformed JSON raises a typed queue error, and corrupted source bytes are not overwritten.
4. Implement immutable candidate/evidence parsing plus `CoreCandidateQueue` with injected clock and config. Keep eligibility a pure function and reject unknown sections/kinds/statuses.
5. Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_core_profile_candidates.py tests/unit/test_storage_paths.py -q
git diff --check
```

6. Review scope and commit: `feat: add core profile candidate queue`.

## P3.2 — Guarded section patch storage boundary

**Files:**

- Modify: `app/agent/memory.py`
- Modify: `tests/unit/test_core_profile_schema_v2.py`

1. Write failing tests for four-section whitelist/order, deterministic `content`/`memory` rendering, `base_updated_at` optimistic locking, `created_at` preservation, `source=core_maintainer`, candidate metadata, and pre-write backup.
2. Write failing tests proving unchanged patches are no-ops, unknown sections and more than two ordinary section changes are rejected, and lock conflicts leave both primary and backup untouched.
3. Add legacy migration fixtures covering exact sentence movement, whitespace normalization, one-to-one sentence preservation, and exact retention of names, forms of address, quoted phrases, and numbers. Add rejection tests for additions, rewrites, duplicates, and omissions.
4. Implement `MemoryStore.patch_core_profile_sections(...)` inside the existing core-profile lock/save boundary. Re-read current V2 under lock, validate before saving, render in the fixed formal-section order, and remove `legacy` only after migration validation succeeds.
5. Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_core_profile_schema_v2.py -q
git diff --check
```

6. Review migration losslessness and commit: `feat: add guarded core profile section patches`.

## P3.3 — Background maintainer, validation, cooldown, and metrics

**Files:**

- Create: `app/agent/core_profile_maintainer.py`
- Create: `tests/unit/test_core_profile_maintainer.py`
- Modify: `app/config/settings_service.py`
- Modify: `data/config/system_config.yaml`
- Modify: `tests/unit/test_settings_service.py`

1. Write failing config tests for all documented defaults and normalization. Keep this backend-only for the first release; do not expand the settings UI unless requested.
2. Write failing tests proving zero model calls without eligible candidates, maximum five candidates per call, normal six-hour cooldown, explicit bypass once per batch, stale-eligible scheduling, and single-flight behavior.
3. Write failing parser/schema tests for `keep/refine/replace/remove/migrate_legacy`, maximum two ordinary changes, candidate/evidence references, and `base_updated_at`.
4. Write failing validation tests for grounding, no-op synonym/whitespace changes becoming keep, 40% shrink protection, protected anchors, first-person/rule-language policy, and remove requiring explicit correction evidence.
5. Write failing resilience tests: API/JSON/validation/storage failures leave candidates pending; three consecutive validation failures pause only that scope for 24 hours; success marks applied/reviewed and resets the failure streak.
6. Implement a minimal Japanese maintainer prompt containing only the current V2 core profile, at most five short candidates, the operation contract, and a minimal Sakura identity anchor. Do not load history, mood, card, intimacy guide, or unrelated memories.
7. Emit structured counters/timings/token usage without candidate or profile正文. Reuse the configured memory-curation model client.
8. Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_core_profile_maintainer.py tests/unit/test_settings_service.py tests/unit/test_core_profile_schema_v2.py -q
git diff --check
```

9. Architecture review the prompt boundary and failure state, then commit: `feat: add background core profile maintainer`.

## P3.4 — Curator emission and asynchronous scheduling

**Files:**

- Modify: `app/agent/memory_curator.py`
- Modify: `app/agent/memory_curation_worker.py`
- Modify: `app/ui/pet_window.py`
- Modify: `tests/unit/test_memory_curator.py`
- Modify: `tests/ui/test_pet_window.py`
- Create: `tests/integration/test_core_profile_maintainer_flow.py`

1. Write failing curator tests using fictional dialogue only: core-profile write intentions become bounded `core_candidate` records; ordinary memory operations remain unchanged; one-sided proposals and transient jealousy/intimacy do not become explicit candidates.
2. Remove direct curator writes to `core_profile` and pass accepted candidate payloads through `MemoryCurationResult` without exposing real history beyond the curator boundary.
3. Write failing scheduling tests proving curation completion persists candidates first, then schedules one maintainer task only when eligible; maintenance never blocks curation completion, reply, TTS, observer, or recall.
4. Implement a dedicated maintainer worker/thread lifecycle separate from the curation worker. If busy, retain candidates for the next trigger; on failure, log metadata-only status and leave normal curation successful.
5. Add fictional integration scenarios: bilateral relationship confirmation applies immediately; temporary jealousy stays unchanged; repeated stable behavior becomes eligible only after thresholds; corrected form of address replaces rather than appends; repeated affection produces keep.
6. Run targeted gates:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_memory_curator.py tests/unit/test_core_profile_candidates.py tests/unit/test_core_profile_maintainer.py tests/ui/test_pet_window.py tests/integration/test_core_profile_maintainer_flow.py -q
git diff --check
```

7. Run the integrated project gate:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit tests/ui tests/integration -q
```

8. Review thread cleanup, no-first-response-call evidence, file-scope compliance, and private-data isolation. Commit: `feat: connect core profile maintenance pipeline`.

## Delegation and integration order

- P3.1: Cursor/Grok High Fast implements from the contract; coordinator reruns tests and reviews persistence safety.
- P3.2: run serially after P3.1 because it touches the memory storage boundary; Cursor may implement, coordinator owns migration review.
- P3.3: Claude Code performs a plan-mode critique of prompt/validation/state transitions; Cursor implements the accepted bounded plan; coordinator integrates.
- P3.4: Claude Code reviews scheduling boundaries; Cursor implements deterministic curator/result changes; coordinator handles `pet_window.py` integration if overlap appears.
- Keep one owner per production file at a time. Each phase gets one thematic commit; push once only after the full gate and user authorization.
