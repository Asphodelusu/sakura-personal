# Cursor Desktop Task — Observer 历史语义与字幕时序清理

## 开始前

1. 读取 `docs/agent-handoffs/CONTEXT.md`、本目录 `README.md`、`acceptance-tests.md`。
2. 运行 `git status --short --branch`。当前预期分支为 `dev5`、HEAD `327be77a`，另有不属于本批的未跟踪目录 `docs/agent-handoffs/session-context-optimization-research/`，必须保留且不得修改。
3. 使用 `D:\sakura\.venv\Scripts\python.exe`。
4. 严格先写能证明真实缺陷的 RED，再作最小实现。不要为了让测试通过而放宽断言或复制生产逻辑到测试。

## 目标 A：Observer 不再复读已经完成或被纠正的旧计划

已确认生产事件：17:02 Sakura 与用户已经吃完亲子丼；17:58 用户再次纠正“不是吃完了吗”，Sakura承认；18:04 普通回复再次确认“一起吃了”；但 18:06 Observer 仍以“刚才约好的亲子丼”为理由催用户去吃。

这不是餐饭关键词问题，不得添加“不要重复吃饭”等内容特判。修复历史/时间/来源与新旧事实链路：

- Observer 与 relationship initiative 的近期对话上下文应明确保留 `role`、时间或可比较的新旧/年龄信息、`channel/source`。
- 模型必须能区分：用户发言、Sakura 普通回复、Sakura proactive 回复、Sakura relationship 回复。
- 同一轮多段 Sakura 回复不得无意义地挤占近期上下文窗口；按真实 turn 分组或使用等价的稳定方案。
- 后续用户纠正、Sakura确认、事实完成应自然压过更旧的“计划中”表述。
- `SensoryImpressionStore` 的滚动情境摘要不能在晚于它的用户/主聊天事实出现后继续作为当前事实注入 Observer；应使用明确的 freshness 比较。较新的屏幕印象仍可保留。
- 保持当前调度、退避、机会仲裁、屏幕触发行为，不重做 P1。
- 日志与 SQLite 仅可只读；测试不得写、截断、替换生产 `Sakura.db` 或生产日志。

## 目标 B：字幕、翻译和 TTS 按可见时刻正确推进

已确认结构问题：`SubtitleController._show_next_reply_segment()` 先提交 TTS，文字只在 TTS `on_started` 回调中的 `_start_segment_speech()` 显示。因此生产日志顺序为“开始播放音频 → 播放开始回调 → segment_text_render_done”，语音可先于气泡文字。

修复合同：

- 当当前 segment 已有可显示文本（中文模式已有 `zh`，或日文字幕模式可直接显示 `ja`）时，气泡文本必须在请求/开始实际音频播放前可见。
- `on_started` 后不得重复初始化可见时间、重复完成 segment 或启动冲突计时器。
- 第一段推进到第二段前必须同时满足：该段 TTS 已完成；从“实际可见时刻”起的最小阅读 dwell 已满足。
- 若等待中的翻译稍后替换占位符或日文 fallback，阅读 dwell 从新文本真正可见时重新计算，不能沿用占位符已显示的时间。
- `（...）` 动作段保持不朗读；应等待翻译成功或现有 bounded deadline，再从动作中文/回退文字实际可见时给予略长但不过度的阅读时间，然后才进入下一句。
- 现有动作等待基线可以小幅延长，不要大幅拖慢日常对话。以“原来中文动作基本能读完，只需略加一点”为准。
- stale timer、旧 interaction 的翻译回调或旧 TTS 回调不能推进/覆盖更新的 reply。
- 保留 translation sidecar 分离设计、失败有界回退和不死锁语义。
- 普通对白缺 `zh` 时当前允许 TTS 在翻译等待期间启动；除非 RED 能证明该语义自身造成错误，否则不要扩大成“所有对白必须等翻译后才播放”。重点是已有可显示文本必须先出现，以及翻译变为可见后阅读时间必须成立。

## 独占文件

本批 Cursor Desktop 可独占修改下列直接相关文件；Codex 在你完成前不会修改它们：

- `app/perception/observer.py`
- `app/perception/sensory_impression.py`
- `app/ui/pet_window.py`
- `app/ui/subtitle_controller.py`
- `app/agent/event_message_builder.py`（仅确有需要）
- `app/storage/chat_history.py`（仅为读取/格式合同确有必要；禁止 schema 迁移）
- `app/voice/playback_controller.py`（仅确有必要）
- 对应聚焦测试：`tests/unit/test_observer_speech_prompts.py`、`tests/unit/test_relationship_timer.py`、`tests/ui/test_pet_window.py`、`tests/ui/test_action_translation_hold.py`、`tests/ui/test_translation_sidecar_ui.py`，以及新增的小型聚焦测试文件
- 本目录 `integration-notes.md` 的 Cursor 段

若必须修改以上范围外的生产文件，先停止并在 integration notes 中说明原因，不要自行扩大范围。

## 禁止范围

- 不修改 `characters/`、card、guide、关系人格、API 配置、模型选择、提示词人格内容。
- 不修改 `docs/agent-handoffs/session-context-optimization-research/`。
- 不用餐饭关键词补丁，不增加一堆“不要……”的提示词禁令。
- 不改数据库 schema，不重写生产历史数据，不触碰私有运行时内容。
- 不 commit，不 push。
- 不运行完整 `tests/unit tests/ui`；由 Codex 在最终集成时决定是否执行一次。避免测试意外拉起真实 Sakura/TTS/外部服务。

## TDD 与交付

至少先得到以下 RED：

1. 历史格式丢失时间/channel，旧待办在更新的完成/纠正事实后仍可能被当作当前。
2. 晚于 sensory impression 的用户/主聊天事实出现后，旧 impression 仍被注入。
3. 已有 `zh` 的 segment 中，TTS 实际开始先于文字显示。
4. 翻译晚到后，下一段没有从翻译文字实际显示时重新等待阅读 dwell。
5. 动作段或旧 timer/回调可过早推进当前队列（如当前实现确实可复现）。

先跑最窄测试，再跑与修改直接相邻的聚焦集合，最后 `git diff --check`。在 `integration-notes.md` 填写：实际模型、耗时、修改文件、逐项 RED 失败证据、GREEN 命令与结果、关键状态转移、未解决风险和拒绝/失败的命令。完成后停止，等待 Codex review。
