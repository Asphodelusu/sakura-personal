# Integration Notes

## Cursor

- status: implemented-verified（未 commit、未 push）
- modified files:
  - `app/config/relationship_initiative.py`
  - `app/perception/observer.py`
  - `tests/unit/test_relationship_timer.py`
  - `app/config/settings_service.py`（提交前补丁：写回 `desktop_idle_seconds`）
  - `tests/unit/test_relationship_initiative_config.py`（round-trip 测试）
  - 本文件 Cursor 段
- RED evidence:
  1. desktop idle 设置：`AttributeError: 'RelationshipInitiativeSettings' object has no attribute 'desktop_idle_seconds'`；构造 `_obs(desktop_idle=900)` 为 `TypeError: unexpected keyword argument 'desktop_idle_seconds'`。
  2. 四级退避边界：第二次沉默后 `now+599` 期望 `cooldown`，实际 `eligible`（仍是固定 300s）。复位/取消测试：`AttributeError: '_relationship_silence_streak'`。
  3. 屏幕沉默/失败：`_last_relationship_silent_at == 10000.0`（期望仍为 0）。屏幕开口：`_relationship_silence_streak == 2`（期望复位为 0）。
  4. 配置持久化：`test_save_roundtrip_preserves_desktop_idle_seconds` 失败 `assert 900 == 1800`。保存未写回该键，加载回到默认 900。
- GREEN evidence:
  - `tests/unit/test_relationship_timer.py`：18 passed
  - `tests/unit/test_relationship_initiative_config.py tests/unit/test_relationship_timer.py`：25 passed
  - `tests/unit/test_proactive_focus.py tests/unit/test_proactive_config.py tests/unit/test_proactive_decision_slot.py`：25 passed
  - 合同中的 `tests/unit/test_proactive_observer.py` 仓库中不存在，未跑该路径
- full gate: `tests/unit tests/ui` → **1714 passed, 1 skipped**（skip 为既有 `thread lifecycle differs`）。`git diff --check` 通过。
- state-transition summary:
  - 门控顺序：disabled → away/busy/continuation → 用户沉默 → 关系开口 cooldown → 关系沉默退避 → `desktop_idle` → eligible。
  - 桌面 idle ≥ `desktop_idle_seconds`（默认 900，夹取 60–86400）返回 `desktop_idle`，不调用关系 LLM；不改变屏幕 idle trigger。
  - 关系 `should_speak=false` / 决策失败 / 空 comment：`_mark_relationship_silent()`，streak 1→2→3→4，冷却 300/600/1200/1800s。
  - `notify_user_spoke()`、关系真正开口、`reset_relationship_state()`：streak 与 silent timestamp 清零。
  - generation 在决策返回后已变：直接 return，不记沉默。
  - 同 tick 屏幕优先：仍只评估一次屏幕并注入 motive；屏幕真正开口才写关系 spoken cooldown 并复位退避；沉默/失败/去重不写 silent cooldown、不升 streak；本 tick 仍 `return` 不跟第二次关系 LLM。
  - `save_relationship_initiative_settings()` 现写回 `desktop_idle_seconds`；设置页保存不再冲掉该值。仍无设置页 UI。
- risks:
  - LLM 把对白当 JSON 返回仍会 parse fail；本批只改变失败后的调度（屏幕失败不再吞关系机会；关系失败进入退避），没有改提示词或解析器。
  - 未改 `pet_window.py`、storage、characters、system_config.yaml、设置页 UI。
- Claude: 未调用。合同行为边界足够明确，不需要额外架构审查。

## Codex

- review: 实际 diff 与任务合同一致；屏幕沉默/失败保留关系机会，屏幕真正开口才消费机会；关系失败退避、用户发言/关系开口复位、stale generation 不累计均符合预期。补充发现并已由 Cursor 修复 `desktop_idle_seconds` 保存时丢失的问题。
- verification: Codex 独立检查实际 diff、文件边界与 `git diff --check`，并复跑 `tests/unit/test_relationship_timer.py tests/unit/test_relationship_initiative_config.py`，修复前审查阶段为 24 passed；Cursor 补充 round-trip 后定向为 25 passed，全量为 1714 passed, 1 skipped。
- integration decision: 接受 P1。按单一主题 commit 收束，不在本批修改解析器、提示词、设置页 UI 或角色文件。
