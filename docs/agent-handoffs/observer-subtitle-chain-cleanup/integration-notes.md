# Integration Notes

## Cursor Desktop / Grok 4.6 High Fast

- status: ready-for-codex-review
- model/session: Cursor Desktop / Grok 4.6 High Fast；工作区 `D:\sakura`，分支 `dev5`，未 commit / 未 push
- elapsed: 约 2 小时（含会话压缩后续作接线与 GREEN 验证）
- modified files:
    - `app/perception/observer.py`
    - `app/perception/sensory_impression.py`
    - `app/ui/pet_window.py`
    - `app/ui/subtitle_controller.py`
    - `tests/unit/test_observer_history_semantics.py`（新增）
    - `tests/ui/test_action_translation_hold.py`
    - 本文件 Cursor Desktop 区块
    - 未改：`app/agent/event_message_builder.py`、`app/storage/chat_history.py`（只读 `load_tail`）、`app/voice/playback_controller.py`、`docs/agent-handoffs/session-context-optimization-research/`
- RED evidence:
    先写失败测试，再对当时生产实现跑最窄命令：

    ```
    .\.venv\Scripts\python.exe -m pytest tests/unit/test_observer_history_semantics.py tests/ui/test_action_translation_hold.py::test_chinese_text_is_visible_before_tts_speak_is_requested tests/ui/test_action_translation_hold.py::test_japanese_text_is_visible_before_tts_speak_is_requested tests/ui/test_action_translation_hold.py::test_late_zh_after_ja_fallback_restarts_reading_dwell -q --tb=line
    ```

    当时 7 红，失败原因对应当时实现而不是 ImportError：

    1. `test_history_format_keeps_role_time_and_channel`：`PetWindow._format_recent_history()` 只有 `[我说的]` / `[她自己的]`，无 `17:02` / `17:58` / `18:04`，无法比较新旧。
    2. `test_multi_segment_assistant_reply_does_not_crowd_out_later_correction`：SQLite/内存按 segment 一行，6 段 18:04 确认把「不是吃完了吗」挤出最近 6 条；`count("[她自己的]")` 不是 2。
    3. `test_later_completion_outranks_older_plan_in_oyakodon_chain`：`latest_ordinary_chat_unix()` 对普通确认返回 `None`（或等价地忽略不了 proactive/relationship），18:04 主聊天事实不能压过更早计划。
    4. `test_stale_sensory_impression_is_not_injected_after_newer_chat_facts`：`get_for_observer()` 只看 TTL；17:10 的「还要去吃亲子丼」印象在 18:04 主聊天事实之后仍被注入。
    5. `test_chinese_text_is_visible_before_tts_speak_is_requested`：`show_segments` 后 `tts.spoken` 已有，`label.text` 仍空；文字要等 `on_started` → `_start_segment_speech`。
    6. `test_japanese_text_is_visible_before_tts_speak_is_requested`：日文模式同样先请求 TTS，后显示 `おはよう。`。
    7. `test_late_zh_after_ja_fallback_restarts_reading_dwell`：日文 fallback dwell 已是 1300ms 后换中文，因 `reply_advance_scheduled` 直接 return，定时器仍是 1300，不是 `DIALOGUE_ZH_DWELL_MS=1140`。

    动作过早推进：既有 `test_action_translation_hold.py` 已覆盖 stale timer / 旧回调；本批未再扩写。动作 hold 仅按合同小幅上调（BASE 400→500，MIN 600→720），测试常量同步为 860 / 720。
- GREEN evidence:

    ```
    .\.venv\Scripts\python.exe -m pytest tests/unit/test_observer_history_semantics.py tests/unit/test_sensory_impression.py tests/unit/test_observer_speech_prompts.py tests/unit/test_relationship_timer.py tests/ui/test_action_translation_hold.py tests/ui/test_translation_sidecar_ui.py -q --tb=short
    ```

    结果：`59 passed in 3.36s`。

    随后 `git diff --check` 通过。未跑完整 `tests/unit` / `tests/ui`。`tests/ui/test_pet_window.py` 无近期历史格式用例，未整文件执行。
- state transitions / design summary:
    - Observer 历史：`ObserverHistoryLine` + `format_observer_recent_history()` 按连续同 channel 的 assistant 折叠为一轮，取最后 `max_turns=6`。行格式 `[她自己的] 18:04 · 2分钟前 …`，legend 写明后续纠正/完成压过更早计划。`latest_ordinary_chat_unix()` 只取 user + 非 proactive/relationship 的 assistant。
    - `PetWindow`：优先只读 `history_store.load_tail(40)` 建行；失败回退 `self.messages`。`set_recent_history_provider` + `set_recent_chat_facts_unix_provider`。不写库、无 schema 迁移。
    - 印象：`SensoryImpression.updated_at_unix`；`get_for_observer(chat_facts_unix=)` 在主聊天事实更新时返回 `""`；更晚的屏幕印象仍可注入。决策 LLM 与 VLM 两处都传入 `_recent_chat_facts_unix()`。
    - 字幕：已有可显示 `zh`（或日文模式的 `ja`，或已可显示的动作）时，`_show_next_reply_segment` 先 `set_speech(..., instant=True)` 再 `speak_segment`。`on_started` 若 `current_segment_speech_done` 只打 `segment_speech_started`，不再 `set_speech` / 重记可见时刻。缺 `zh` 的普通对白仍允许 TTS 先走。
    - 晚到译文：`set_speech` 在 instant 且文案变化、且该段已标记 shown、TTS 已完成时，停 dwell、清 `reply_advance_scheduled`，按新可见文本重算。
    - 未改调度/退避/仲裁、translation sidecar 分离、人格/API/餐饭关键词。
- denied or failed commands:
    - 无被拒绝的命令。
    - 按合同未执行完整 `tests/unit`、`tests/ui`，未 commit，未 push。
- remaining risks:
    - 内存回退路径若没有 `created_at`，格式无时钟、`latest_ordinary_chat_unix` 为 `None`，freshness 比较会跳过；依赖历史库可读。
    - 主聊天更新后旧印象整段不注入，屏幕现场要等下一次更新的 impression；这是合同要求，可能少一句过期场景。
    - `load_tail(40)` 在极端超长分段轮次下仍可能少于 6 个真实 turn。
    - 历史库路径未过滤 intimacy continue（仅内存回退过滤）；若库里有这类行可能进 Observer。
    - 预显示把 `_segment_visible_monotonic` 提前到 `speak` 之前；长 TTS 不再叠加整段 dwell（与原「只补足剩余」一致）。
    - 未跑 `test_pet_window.py` 与全量 UI，接线回归留给 Codex。

## Codex

- review: 接受整体实现方向。独立检查后补出一个真实 turn 边界缺口：原实现仅按“连续 assistant + 同 channel”折叠，会把不同时间的两次独立主动发言合并。新增 RED 后将已知时间不同的发言保留为两轮；时间相同的持久化分段、或缺时间的内存回退仍可折叠。字幕测试也改为在 TTS `speak()` 被调用当刻记录气泡文字，直接证明 display-before-request，而不只检查 `show_segments()` 返回后的最终状态。
- verification:
  - 审查新增 RED：`test_separate_assistant_turns_with_same_channel_keep_their_own_time` → `assert 1 == 2`，证明不同时间的 proactive 被错误合并；最小修复后该测试与中/日文 TTS 请求时刻测试 `3 passed`。
  - 首次扩大到 `tests/ui/test_pet_window.py` 时发现 1 个旧合同失败：已有中文字幕仍被旧测试要求保持等待点直到 TTS 开始。将该测试迁移为无译文、无 sidecar gate 的日文回退路径，保留原等待语义。
  - 最终命令：`tests/unit/test_observer_history_semantics.py tests/unit/test_sensory_impression.py tests/unit/test_observer_speech_prompts.py tests/unit/test_relationship_timer.py tests/ui/test_action_translation_hold.py tests/ui/test_translation_sidecar_ui.py tests/ui/test_pet_window.py -q --tb=short` → **227 passed in 7.20s**。
  - 未运行完整 `tests/unit tests/ui`：既有记录显示该门禁曾在约 47% 拉起外部 Sakura/TTS 后停滞；本批以直接相关聚焦与完整 PetWindow UI 文件覆盖为准。
- integration decision: 接受本批。Observer 决策与关系主动现在获得带 role/channel/时间的真实 turn 上下文，晚于 impression 的用户/普通聊天事实可使旧印象失效；已有字幕在 TTS 请求前可见，译文晚到后重新计算 dwell，动作 hold 仅小幅延长。未加入主题关键词特判，未改调度、人格、API、数据库 schema 或上下文优化研究批次。
