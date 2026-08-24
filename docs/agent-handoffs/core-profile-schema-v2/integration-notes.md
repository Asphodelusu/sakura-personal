# Integration Notes — Core Profile Schema V2

## Cursor

- status: implemented-unverified（合同文件已改完；本会话 Shell 被 Auto-review 全部拒绝，未能实际跑 pytest / git）
- diff state: uncommitted；未 commit、未 push；未触碰既有 `.gitignore` 改动或真实 `data/`
- modified files:
  - `app/agent/memory.py`
  - `tests/unit/test_core_profile_schema_v2.py`（新建）
  - `docs/agent-handoffs/core-profile-schema-v2/integration-notes.md`（仅本 Cursor section）
- RED evidence: 未能执行 `.\.venv\Scripts\python.exe -m pytest tests/unit/test_core_profile_schema_v2.py -q`（`echo hello` 同样 Rejected）。测试按计划先写入。按改前代码，Task 1 预期 `test_v2_missing_content_renders_sections_read_only` 失败（只读路径不读 sections）；Task 2 预期 V1 仍写出无 `schema_version` 的记录、未知 schema 可被覆盖、覆盖写不产生 `.bak`。
- GREEN evidence: 未能执行 `.\.venv\Scripts\python.exe -m pytest tests/unit/test_core_profile_schema_v2.py tests/unit/test_memory_curator.py -q` 与 `git diff --check`。实现已按计划加上 `CoreProfileStorageError`、schema/content helper、`_load_core_profiles(strict=...)`、`_build_core_profile_v2_record`、V2 整段写入、未知/损坏拒写、`backup=True`。
- risks:
  - 本会话没有可复现的 pytest/git 输出，集成者必须重跑目标测试和 `git diff --check`
  - 读路径未知 schema / V2 缺 content 只记 debug 日志，不含正文；严格错误信息含文件名与 `schema_version` 数字，不含档案正文
  - 未对真实 `data/memory/core_profiles.json` 做迁移或读取

## Codex integration review

- status: complete
- diff review: 文件范围符合合同；读路径不写盘，V1/V2 保持兼容；只有 V2 缺正文时才解释完整合法的 sections。set/delete 使用严格加载，损坏 JSON、顶层非对象、目标 scope 非对象及未知 schema 均拒写且不改变字节。整段写入生成单一 legacy，保存使用 `backup=True`。
- targeted verification: schema v2 + memory curator → 59 passed；`git diff --check` 通过，仅有任务外 `.gitignore` 与 Windows LF/CRLF 提示。
- full verification: unit → 1279 passed；UI → 222 passed, 6 skipped。
- independent review: 首轮发现未来 schema sections 误读与 sections 部分合法化两项 Important；Codex 以 4 个 RED 测试复现并修复，复审确认两项关闭、无新增 Important/Minor。
- final decision: 可以集成。未读取或迁移真实 `data/memory/core_profiles.json`；真实 V1 只会在应用下一次确实写入常驻档案时自然升级并生成 `.bak`。
