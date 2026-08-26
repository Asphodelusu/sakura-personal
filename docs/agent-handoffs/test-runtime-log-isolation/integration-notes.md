# Integration notes — pytest runtime-log isolation

## Cursor

- status: 已完成 log 隔离修复，等待协调者独立 review 与整合。
- model and elapsed time: 5.3 Codex Spark fallback（当前 Spark fallback），约 3 分钟（含三组测试与日志校验）。
- modified files:
  - `tests/conftest.py`
  - `tests/unit/test_debug_log.py`
  - `docs/agent-handoffs/test-runtime-log-isolation/integration-notes.md`
- RED evidence: 历史上 relationship/fallback 组合会向生产 `sakura-runtime.log` 落入 17 行；本次未复现该脏写（验证见生产日志前后对比）。
- implementation:
  - 在 `tests/conftest.py` 增加 autouse fixture `isolate_runtime_file_log`。
  - 每次测试前先 `_close_file_logger_for_tests()`，然后将 `app.core.debug_log._FILE_LOG_PATH` 重定向到 `tmp_path / "sakura-runtime.log"`。
  - 测试后再次 `_close_file_logger_for_tests()` 以关闭句柄、避免 Windows 文件句柄泄漏。
  - 在 `tests/unit/test_debug_log.py` 补充 `test_file_log_path_is_isolated_for_pytest`，断言每次 `debug_log` 的日志路径落在 pytest 临时目录内。
- production-log before/after evidence:
  - 命令前：`exists 1744890 6194`
  - 命令后：`exists 1744890 6194`
  - 文件路径、大小、行数完全不变。
- focused/full tests:
  - `tests/unit/test_debug_log.py` -> `10 passed`
  - `tests/unit/test_relationship_timer.py tests/unit/test_observer_plain_dialogue_fallback.py` -> `35 passed`
  - `tests/unit tests/ui` -> `1757 passed in 46.37s`
- `git diff --check`:
  - 通过；仅出现 `app/ui/pet_window.py` 与 `tests/ui/test_pet_window.py` 的 LF→CRLF 非阻塞提示（未涉及本任务文件修改），未见阻塞 diff。
- risks: 无新增运行时/生产数据影响；未修改 app/ 生产逻辑与现有日志行为。 

## Coordinator

- diff review: accepted. The autouse fixture keeps file logging enabled but redirects
  `_FILE_LOG_PATH` to pytest `tmp_path`; it closes the cached rotating handler before
  path replacement and during teardown. Existing debug-log tests can still override
  the path and exercise real writes/rotation. No production module or user setting is
  changed.
- independent production-log probe: before and after the focused plus full gates,
  `data/logs/sakura-runtime.log` remained byte-for-byte identical:
  SHA-256 `421C1C37FAEEF0558ABA06840D8A1E1B61A9593D2EE0D1E93D3D63E6601F058A`,
  `1,744,890` bytes, `6,194` lines.
- verification:
  - debug-log + relationship/fallback focused gate: `45 passed in 1.09s`
  - full gate: `1757 passed in 44.85s`
  - `git diff --check`: no blocking issue; Windows line-ending warnings only.
- decision: accepted. Future Observer cost/effect measurements can exclude historical
  test rows and rely on new production rows without additional test contamination.
