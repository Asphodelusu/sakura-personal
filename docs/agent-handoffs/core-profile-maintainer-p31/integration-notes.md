# P3.1 Integration Notes

## Cursor

- status: implemented-unverified
- modified files:
  - `app/agent/core_profile_candidates.py`（审查修复：最近 5 条证据窗口、kind/claim 仅在保留的 explicit 证据上升级、按 path 的线程锁+文件锁并在锁内重载、save 失败不发布内存、load 重算/校验 id 与聚合 confidence、拒绝非有限 confidence、`allow_nan=False`、Decimal 量化、housekeep 落盘）
  - `app/storage/paths.py`（本轮未改；仍提供 `memory_core_review_queue()`）
  - `tests/unit/test_core_profile_candidates.py`（原行为测试 + 审查回归）
  - `tests/unit/test_storage_paths.py`（本轮未改）
  - 仅本文件 Cursor section
- RED evidence: 本轮仍未能执行 pytest。回归测试是对着审查指出的旧行为写的；按修前代码，预期失败点包括：第 6 条新 batch 被丢掉、`kind`/`claim` 在证据未保留时仍升级、第二实例丢失并行写入、save 失败后内存可见新候选、过期/清理不落盘、磁盘上错误 id/重复/超帽/NaN 被接受、`0.80+0.80+0.79` 均值被 `isclose` 放行。
- GREEN evidence: 未能执行。请集成者重跑：
  - `.\.venv\Scripts\python.exe -m pytest tests/unit/test_core_profile_candidates.py tests/unit/test_storage_paths.py -q`
  - `git diff --check`
- session/model: Cursor Grok 4.6
- permission failures:
  - `git status --short --branch`
  - `echo RED-CHECK`
  - `.\.venv\Scripts\python.exe -m pytest tests/unit/test_core_profile_candidates.py tests/unit/test_storage_paths.py -q`
  - 本轮再次 `pytest` 仍返回 `Rejected:`，无 stdout
- risks:
  - 集成者必须重放 RED/GREEN；本 worker 不能把测试结果当作已证明
  - 未 commit、未 push；未改 `.gitignore`，未读/写真实 `data/memory/` 或 runtime 私有记忆
  - 同 path 互斥锁是进程内 `threading.Lock` + sidecar `*.json.lock`（Windows `msvcrt.locking` / POSIX `fcntl.flock`）。多进程安全依赖文件锁
  - explicit eligibility 看保留的 explicit 证据（双边 + 其 confidence），不再用混入 observed 后的均值去卡 0.90
  - 过期 pending 会先标 `expired` 并把 `last_seen_at` 滚到当前 clock，然后把 housekeep 写回磁盘


## Coordinator

- scope review: passed; only the four contracted implementation/test files plus this handoff and the approved implementation plan changed. Existing `.gitignore` stat/EOL change remains untouched.
- verification: `.venv\Scripts\python.exe -m pytest tests/unit/test_core_profile_candidates.py tests/unit/test_storage_paths.py -q` -> 98 passed; `git diff --check` -> passed (only the pre-existing `.gitignore` EOL warning). Ruff is not installed in the repository `.venv`.
- integration decision: accepted after fixing/retesting confidence threshold precision and a separate read-only review followed by Cursor regression fixes for evidence retention, multi-instance locking, transactional publication, load validation, non-finite values, and persistent housekeeping.
