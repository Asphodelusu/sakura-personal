# Cursor Task — Pin Core Profile in Light Curation Detail

## Objective

确保唯一的 `core_profile` 在 `snapshot_profile=light` 时始终全文进入详细区，同时维持原有 36 条详细上限和普通记忆评分行为。

## Exclusive files

- Modify: `app/agent/memory_curator.py`
- Test: `tests/unit/test_memory_curator.py`
- Fill: `docs/agent-handoffs/core-profile-light-pinning/integration-notes.md` 的 Cursor section

## Do not modify

- `app/agent/memory.py`
- `data/`、`characters/`、日志、聊天历史、心情状态与任何真实记忆文件
- 其他源码、测试和 handoff 文件
- 工作树既有 `.gitignore` 状态

## Shared interface

- 保持 `_select_light_curation_memories(memories, dialog_entries, *, base_dir=None) -> tuple[detail, index_only]` 签名不变。
- core 识别使用记录的 `layer == "core_profile"`；如仓库已有公开 helper/constant，优先复用，勿读取真实数据推断。
- 有 core 时：core 必须位于 `detail`，普通评分结果填满剩余 `LIGHT_CURATION_DETAIL_LIMIT - 1` 个名额。
- core 不得同时出现在 `index_only`。
- 无 core 时：排序和数量行为保持现状。
- 不新增第二套 add/merge 查重；现有 `_find_existing_memory_for_candidate` 保持不变。

## Required workflow

1. 运行 `git status --short --branch`，保护既有改动。
2. 先写失败测试，至少覆盖：低分旧 core 仍进 detail；detail 不超过上限；core 不进 index；无 core 行为不变。
3. 用 `.venv\Scripts\python.exe` 运行测试并记录 RED。
4. 实现最小改动，不扩大为 schema 或 maintainer 重构。
5. 运行 `tests/unit/test_memory_curator.py -q` 与 `git diff --check`。
6. 只填写 integration notes 的 Cursor section。

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

