# Cursor Task — Core Profile Schema V2

## Objective

按已批准规格与实施计划完成 V1/V2 无损兼容读取、严格写入保护、V2 迁移和滚动备份。不得对真实档案执行迁移。

## Required reading

- `docs/agent-handoffs/CONTEXT.md`
- `docs/superpowers/specs/2026-08-24-core-profile-schema-v2-design.md`
- `docs/superpowers/plans/2026-08-24-core-profile-schema-v2.md`

## Exclusive files

- Modify: `app/agent/memory.py`
- Create: `tests/unit/test_core_profile_schema_v2.py`
- Fill: `docs/agent-handoffs/core-profile-schema-v2/integration-notes.md` 的 Cursor section

## Do not modify or read

- 不读取或修改 `data/memory/core_profiles.json`
- 不读取 `data/logs`、聊天历史、mood_state、其他长期记忆正文或角色文件
- 不修改 `app/agent/memory_curator.py`、UI、配置或其他测试
- 不触碰工作树原有 `.gitignore` 状态

## Shared interfaces

- 严格遵守计划中 `CoreProfileStorageError`、schema/content helper、`_build_core_profile_v2_record` 和 `_load_core_profiles(strict=...)` 的接口。
- 保持 `core_profile()` 返回形状和 `set_core_profile(content, metadata=None)` 调用兼容。
- 读路径不得写磁盘；写路径不得把损坏或未知 schema 当空库覆盖。
- 整段写入只生成单一 `sections.legacy`；不得做语义拆章。

## Required workflow

1. 运行 `git status --short --branch` 并保护既有改动。
2. 严格按计划 Task 1/2 先测试 RED，再最小实现 GREEN。
3. 只使用 pytest 临时目录，不读取真实数据。
4. 运行 `tests/unit/test_core_profile_schema_v2.py tests/unit/test_memory_curator.py -q` 与 `git diff --check`。
5. 只填写 integration notes 的 Cursor section。

## Git policy

- Local commit: forbidden
- Push: forbidden

## Result fields

- status
- diff state
- modified files
- RED evidence
- GREEN evidence
- risks

