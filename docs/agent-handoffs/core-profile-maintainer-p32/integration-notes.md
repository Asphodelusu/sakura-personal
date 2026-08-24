# P3.2 Integration Notes

## Cursor

- status: implemented-unverified
- files:
  - `app/agent/memory.py`
  - `tests/unit/test_core_profile_schema_v2.py`
  - 仅本文件 Cursor section
- RED: 本轮仍未能执行 `.\.venv\Scripts\python.exe -m pytest tests/unit/test_core_profile_schema_v2.py -q`（含 `request_smart_mode_approval` 仍为 `Rejected:`，无 stdout）。审查回归测试先写入；按修前代码，预期失败包括：同秒两次写入 `updated_at` 相同、独立 `MemoryStore` 实例不共享锁、混存/未知 section 被静默丢掉、stale `content`/`memory` 被当成 no-op、备份失败仍替换主文件、去掉句内空格仍能迁移、ASCII 句号不切分。
- GREEN: 未能执行 pytest 与 `git diff --check`。审查项对应实现：
  1. `updated_at` 用微秒，且若新时间戳不晚于旧值则 +1µs
  2. 按规范化 `core_profiles` 路径共享进程内锁，并加 sidecar `.lock` 文件锁；锁内重读
  3. 磁盘上未知 section 或 legacy/正式混存直接拒绝，不删键
  4. 章节未变但缓存陈旧则回写确定性 `content`/`memory`；真 no-op 不写盘
  5. maintainer 写路径先严格备份，失败则抛错且不调用 `atomic_write_text`；未改 `app/storage/atomic.py`；测试用 `Path.write_bytes` 对 `.bak` 注入 OSError
  6. 空白规范化改为压缩为单空格（保留句内空格）；ASCII `.` 在非数字后切句，`3.14` 不拆
- session/model: Cursor Grok 4.6
- permission failures:
  - `.\.venv\Scripts\python.exe -m pytest tests/unit/test_core_profile_schema_v2.py -q`
- risks:
  - 集成者必须重放 RED/GREEN；本 worker 没有 pytest/`git diff --check` 输出
  - 未 commit、未 push；未改 `.gitignore`、`app/storage/atomic.py` 或真实 `data/memory/`
  - 整段 `set_core_profile` 仍走原来的 lenient `backup=True`；严格备份只约束 `patch_core_profile_sections`
  - 多进程互斥依赖 sidecar `core_profiles.json.lock`（Windows `msvcrt.locking` / POSIX `fcntl.flock`）

## Coordinator

- review: accepted after an independent read-only review found and Cursor repaired same-second lock tokens, cross-instance locking, stored-section loss, stale caches, strict backup handling, and meaningful whitespace preservation.
- verification: core schema target 48 passed; expanded `test_memory_curator.py + test_core_profile_schema_v2.py + test_agent_core.py` gate 211 passed, 2 skipped; `git diff --check` passed apart from the pre-existing `.gitignore` EOL warning.
- decision: ready for the P3.2 thematic commit; no push.
