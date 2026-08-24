# Integration Notes — Core Profile Light Snapshot Pinning

## Cursor

- status: implemented; verification blocked in this session (Shell rejected)
- diff state: uncommitted local edits only; no commit; no push. Did not touch `.gitignore` or other agents' files. `git status`/`git diff --check` could not be executed here because every Shell call was rejected.
- modified files:
  - `app/agent/memory_curator.py` — `_select_light_curation_memories` pins the unique `layer == core_profile` record into `detail`, fills remaining `LIGHT_CURATION_DETAIL_LIMIT - 1` slots from ordinary score order, and keeps core out of `index_only`. Reuses `MEMORY_LAYER_CORE_PROFILE`. Signature unchanged. `_find_existing_memory_for_candidate` untouched.
  - `tests/unit/test_memory_curator.py` — in-memory fixtures only; covers low-score old core in detail, detail cap, core excluded from index, no-core order/count unchanged.
  - this Cursor section of `docs/agent-handoffs/core-profile-light-pinning/integration-notes.md`
- RED evidence: not captured. Pytest and git were never able to run (Shell `Rejected:` with no output). Tests were written first against the old top-36-by-score slice; a 2019 `core_profile` among 40 newer unrelated ordinary memories would have been omitted from `detail` and could appear in `index_only`.
- GREEN evidence: not captured. Please re-run before integrating:
  - `.\.venv\Scripts\python.exe -m pytest tests/unit/test_memory_curator.py -q`
  - `git diff --check -- app/agent/memory_curator.py tests/unit/test_memory_curator.py`
- risks:
  - Integrator must reproduce RED/GREEN; this worker could not.
  - Pinning one core by `layer` drops any extra `core_profile` rows from both `detail` and `index_only` (contract assumes uniqueness).
  - Core is placed first in `detail`; remaining slots follow existing score + `updated_at` order. No-core path is the previous slice.

## Codex integration review

- status: complete
- diff review: 文件范围符合合同；复用 `MEMORY_LAYER_CORE_PROFILE`，仅调整 light 快照 detail/index 分配。core 固定在 detail 首位，普通记忆沿用原评分填充剩余 35 席；无 core 路径保持原切片。未改 add/merge、schema、触发频率或真实数据。
- targeted verification: `.\.venv\Scripts\python.exe -m pytest tests/unit/test_memory_curator.py -q` → 38 passed；`git diff --check -- app/agent/memory_curator.py tests/unit/test_memory_curator.py` → 通过。
- full verification: unit → 1258 passed；UI → 222 passed, 6 skipped。
- independent review: 无 Important/Minor；确认无 off-by-one，生产每 scope 只有固定 ID 的唯一 core profile。
- final decision: 可以集成。P1 只保证 light 整理能看到完整 core 记录；专用 core maintainer、section patch 与 schema v2 留待后续阶段。
