# Sakura 会话上下文调研结论

## 结论摘要

Sakura 目前不是“每轮只发最近一句”，也不是“永久全历史会话”。它以当前进程内的 `self.messages` 为主窗口，发送前按 40K 估算 token 裁剪并强保最近 8 个 user turn；重启后窗口清空，再由 SQLite 尾部生成至多 1,024 token 的短期续接片段，并由长期记忆补充更久的事实。这个分层方向适合角色陪伴，不应改成每轮回放完整 SQLite 历史。

当前主要浪费不只来自历史长度，而来自三件事：动态片段使请求尾部每轮变化；默认 6 个 core native tool schema 随每个工具规划请求发送；回复结构失败时，合成和修复会再次携带完整 persona、runtime context 与裁剪后的 `working_messages`。对于 DeepSeek，稳定的长前缀可能获得服务端前缀缓存收益；对于 Google OpenAI 兼容层，请求体仍按次上传，且当前只会剥掉不兼容的 DeepSeek `thinking` 字段，并没有显式控制 Google 自己的 thinking 等级。

## 从用户回车到首句可展示

```text
用户输入
  │
  ├─ PetWindow 写入 self.messages / SQLite 历史
  ├─ 仅本轮 request_messages 注入视觉或 runtime 事件上下文
  ├─ trim_messages_for_model
  │    ├─ 40K 估算 token
  │    ├─ 强保最近 8 个 user turn
  │    └─ 清理不完整 tool_call/tool 配对
  └─ ChatWorker → AgentRuntime.run_with_tools
       ├─ Turn routing：选择主/快模型、thinking、记忆策略
       ├─ step 0：内心独白 ∥ 记忆改写/召回
       ├─ ContextOrchestrator：时间、步数、会话续接、记忆、感官、插件等
       ├─ PromptRuntime：persona + 功能 recipe（静态）│ runtime context（动态尾部）
       ├─ 默认 core tools schema + working_messages → 主模型
       ├─ 若 tool_calls：执行工具、结果只进入本轮 working_messages，再循环
       ├─ 若正文是可采用 JSON segments：直接采用；缺 zh 走异步翻译 sidecar
       ├─ 若纯文本/缺 segments：再发一次结构化合成
       └─ 若仍不合格：再发一次格式修复 → UI segments → TTS / 字幕
```

### 各层职责与当前重量

| 层 | 当前实现与默认边界 | 重量判断 |
|---|---|---|
| 进程内窗口 | `PetWindow.self.messages` 保存干净 user/assistant 对话；视觉、事件注入只进入本轮副本。角色应用/重启时清空。 | 可从很小增长到 40K token 上限；通常是最大可变部分。 |
| 请求裁剪 | `context_trimming.py`：40,000 token 估算预算、最多不再使用旧 60-message 硬截断逻辑、强保最近 8 个 user turn；图片按每张约 28K token 保守估算。 | 预算是近似值，不包含 system prompt 和 tool schema，因此不是完整请求上限。 |
| SQLite 历史 | `ChatHistoryStore` 持久化 user/assistant、translation、channel 等；普通主请求不直接全量回放。 | 不直接构成每轮 token；是跨重启续接、搜索与回填的事实源。 |
| session digest | 窗口中最近消息少于 2 条时，从历史尾部清洗最多 12 条、单条最多 220 字，合计最多 1,024 token；实时窗口变深后停止注入。 | 小而短暂，避免重启即失忆；不是永久摘要数据库。 |
| ContextRequest | 最近 8 条 user/assistant、单条 1,000 字，当前输入最多 4,000 字；视觉摘要最多 6 条、单条 500 字。 | 用于选择上下文，并不等于这些字段全部原样再注入。 |
| 动态上下文 | `ContextPolicy` 总预算 4,096 token；其中 plugin 2,048、memory 1,024。时间 192、agent progress 128、session 1,024、感官 128、本地媒体 256、inner thought 420、关系/其他片段各有自身上限。 | 第二大可变部分；每步时间、进度、记忆命中都会改变尾部。 |
| recipe 静态块 | persona、工具规则、回复协议、最终回复说明、按需 intimacy 段等由 `PromptRuntime` 组装。静态 section 有 SHA-256 `static_hash`。 | 可能较重，但相邻同类型 turn 通常可字节不变；不同 recipe（工具轮/最终合成/事件）不相同。 |
| tools schema | `DEFAULT_ACTIVE_TOOL_GROUPS={"core"}`；通常为 `set_intimacy_mode`、`memory_search`、`memory_detail`、`memory_timeline`、`search_tools`、`list_tool_groups`。命中意图后可扩展 productivity/desktop/mcp/browser 等组。 | 每个工具规划请求重复发送 JSON schema；组变化会改变前缀/体积。 |
| 工具临时历史 | assistant tool_calls 与 tool result 只积累在本轮 `working_messages`，每步再次裁剪；最终正常回复才回到 `self.messages`。 | 工具轮越深越重，但不会永久污染会话窗口。 |
| 翻译 sidecar | 合格 ja segments 即可进入显示/TTS，缺 zh 异步翻译并回填 UI 与 SQLite。 | 已与主回复解耦；不应为省上下文重新绑回主模型。 |

重量没有完整线上分项统计。已批准的脱敏锚点显示：一次初始 HTTP 约 7.7 秒、请求体约 4KB、可见输出 82 字；之后结构化合成/修复各约 13–15 秒，整轮约 53 秒才开口。需求中另有近期 Gemini 主请求体约 31KB 的观察。两者只能证明“额外请求比局部减字更值得先处理”，不能当成总体 P50/P90。

## Q1：哪些前缀能稳定，哪些实际在变

### 可字节级稳定的候选

- 同一角色、同一 recipe、相同 intimacy 状态、相同 portrait catalog 选择与相同插件 prompt patch 下，persona 与大部分规则 section 可以稳定。
- 同一模型槽、同一 active tool group、相同能力开关下，native tools JSON 可以稳定。
- 对话采用尾部追加时，旧 `messages` 本身天然是下一轮的前缀；裁剪窗口尚未滑动前，历史前缀可复用。
- `PromptSection.cache_scope="static"` 已生成 `static_hash`，可用来观测 section 是否稳定，但当前它只是检查信息，不是本地或远端缓存实现。

### 每轮或每步会变的部分

- runtime time、agent step/remaining steps 必变。
- 当前输入、最近消息、记忆召回、inner thought、关系/感官/视觉、插件 context 依场景变化。
- active tool groups 在 `search_tools` 或路由扩组后变化；browser 能力/页面模式也会过滤 schema。
- 切换工具轮、最终合成、事件回复会换 recipe；切角色、改角色/设置、热更 prompt patch、切模型槽也应视为稳定前缀失效。
- 当前 `runtime_context` 作为尾部 system message；端点拒绝时会回退成尾部 user message，并在 client 实例内记住。这个角色变化会改变字节序列。

DeepSeek 的前缀缓存只有在服务端按请求前缀识别且前缀字节稳定时才可能命中。Sakura 不需要把动态事实塞回静态 system 前缀；保持“静态 recipe 在前、append-only 对话居中、动态 runtime 在尾”更有利。Gemini 即便存在厂商缓存能力，当前 OpenAI 兼容请求仍会完整上传；稳定化首先减少意外变动、便于测量，不应把未验证的缓存命中当作收益承诺。

## Q2：永久唯一会话与 Sakura 的连续感

Alife README 公开自述的相关概念是：永久唯一会话、分区维护且保持稳定的上下文、类似多级 cache 的自动记忆压缩、原生文本/XML 函数调用，以及很高的缓存命中与低成本。后两项性能数字是作者自述，未在本批复现。

与 Sakura 对齐的部分：

- 连续感应来自稳定身份、最近关系状态、可追溯经历与自然回忆，而不是 UI 重启后“换了一个人”。
- 静态/动态分区、摘要续接、长期记忆压缩与白盒观测都与现有架构相容。
- 工具 schema 和失败重打可以更节制。

不宜直接照搬的部分：

- Sakura 有明确的角色蒸馏、关系/独白/观察/翻译侧路；“无需初始人设、完全靠成长”不是本产品目标。
- 把 SQLite 全历史永久回放会增加隐私驻留、输入成本、旧关系噪声与复读风险，并削弱记忆检索的选择性。
- native tools 已有权限、确认、结果配对与兼容测试；改成自研 XML 双轨不是低风险节词方案。
- Alife 为 AGPL-3.0；本批只参考公开概念，不复制实现、prompt 或 XML 协议。

因此，“唯一生命线”应落在持久化身份/关系事实、短期 session state 和可追溯长期记忆三层；进程内仍保留有限实时窗口。重启后的首两条消息用 digest 搭桥，之后由实时窗口接管，必要时用记忆工具展开，而不是把全部历史永久塞入主模型。

## Q3：结构失败的重打税

当前 `_compose_structured_final_reply()` 会再次发送：完整 `system_prompt`、去图并裁剪后的 `working_messages`、完整 `runtime_context` 和一个合成指令，只是不再附带 tools。若合成仍失败，repair 请求再次发送完整 system prompt、完整 working messages、坏输出和修复指令。也就是说，“只需要把已有语义装进 JSON”仍可能重新进行角色推理。

并非所有失败都需要这些上下文：

- 仅 JSON 引号、围栏、字段名或 segment 结构损坏：可以只给原始输出、最小 schema 和允许的 tone/portrait 枚举，使用确定性本地修复或廉价修复模型；无需 persona、工具、历史、记忆。
- 输出是完整自然日语但完全没有 JSON：可尝试“结构封装”小请求，仅保留原文与协议；它不应改写语义。
- 输出缺答案、工具证据未被吸收、内容与用户问题不对应：这是语义合成，仍需要裁剪后的对话、必要证据、persona 与动态关系上下文。

关键是先分类“结构失败”和“语义失败”。小修复必须验证日语正文逐字或规范化后等价；否则宁可回到完整合成。合格 JSON segments 缺 zh 已直接采用并交给翻译 sidecar，不再属于重打原因。

## Q4：六个 core tools 与 native/XML

当前六个 core tools 不是全部都会在普通闲聊中用到：`set_intimacy_mode` 仅关闭模式，三项记忆工具用于显式/不足的回忆，`search_tools`/`list_tool_groups` 用于能力发现。每轮都发它们的优点是工具可发现性稳定、弱路由不容易漏能力、schema 前缀稳定；缺点是固定输入开销和模型误调用面。

更稳妥的方向是保留 native tools 单轨，做“稳定工具包”：

- 基线包可继续是 6 个 core，先测 schema token 占比和实际调用率。
- 若占比显著，再将 core 拆成 `core_minimal`（能力发现 + 场景必须项）与按路由加入的 memory/intimacy 包；路由不确定时回退完整 core。
- 工具组一旦通过 `search_tools` 扩展，本轮后续请求使用新的 tool-set fingerprint；下一普通轮回到路由决定的稳定基线。
- fingerprint 需包含工具名、description、parameters、权限/能力可见性和顺序；任何变化都使缓存失效。

自研明文/XML 可比 verbose JSON schema 少 token，但会新增 parser、转义、注入、权限、确认、流式半包、native/pseudo 语义差异和双套测试。现有 client 已能从 content 解析 pseudo/XML tool call，这更适合作为“端点不支持 native tools”的兼容兜底，不宜为了普通闲聊省少量 token 而成为默认协议。

## Q5：Gemini thinking 是独立旋钮

Turn routing 当前产出 DeepSeek 风格 `thinking:{type: disabled/enabled}`；Google AI Studio OpenAI 兼容端点会在 payload 构建时直接剥掉该字段。因此 Sakura 的“默认关闭 thinking 以保速度”意图没有传到 Google 自己的 thinking 控制，端点可能使用模型默认值。

这应作为与上下文稳定化独立的 provider capability：在确认目标 Gemini 模型和实际端点支持的参数名、取值及 usage 字段后，短闲聊/工具轮设最低或关闭，用户明确要求深思时再提高。不能把未经验证的 `thinking_level` 直接塞进所有 OpenAI 兼容供应商，也不能用它掩盖结构重打问题。

## Q6：现有测量能力与缺口

### 已有

- `PromptInspector`：recipe、各 section 的 chars/token 估算、cache scope、静态 hash、是否包含/截断/丢弃、总 chars/token、runtime role；开启 debug body 时有脱敏 prompt。
- `API`：model、timeout、temperature、message/tool 数、结构化参数；HTTP 层有 request bytes、elapsed_ms；响应诊断可取 prompt/completion/total tokens。
- `AgentRuntime` / `ChatWorker`：planning、tools、final reply、turn 总耗时及 worker 总耗时。
- TTS：合成/播放耗时与文字 chars，可用于计算模型完成到首声的后半段。

### 缺口

- PromptInspector 不统计 `messages`、图片估算与 tools schema 的 chars/token，也没有整份 payload fingerprint。
- 没有稳定前缀长度、recipe/tool/message-prefix hash、相邻轮命中候选与失效原因。
- usage 日志只摘常规三项，未统一记录厂商 cached input、reasoning/thinking token 等扩展字段。
- 没有每个 interaction 的 `request_index` / `request_purpose`（initial/tool_step/semantic_compose/structural_repair/translation），很难自动算重打率。
- 缺统一的 input→first bubble、input→first TTS、bubble segment 替换时间；现有分散 elapsed 需日志后处理关联。
- 没有 RP 质量代理指标，不能仅凭 token 下降判定体验变好。

建议使用不含真实对话的合成夹具固定五类 turn：短闲聊、长历史、带图、一次工具调用、结构损坏；DeepSeek 与 Gemini 各跑一套 fake transport/payload snapshot。记录每轮请求数、purpose、bytes、估算/真实 token、stable-prefix bytes、首泡/首声延迟，并用人工盲评检查角色一致、承接、复读、机械口吻和事实忠实。真实 API 基线应由用户选择时另行授权，本批没有发请求。

## 与 2026-06 `docs/context-token-budget.md` 的差异

| 旧文档描述 | 当前代码 |
|---|---|
| 24 messages / 40K chars FIFO | 40K 估算 token；强保最近 8 个 user turn；旧 `MAX_MODEL_CONTEXT_MESSAGES=60` 常量存在但不作为硬截断执行。 |
| 主要按字符裁剪 | ASCII 约 4 字/token、非 ASCII 1 字/token；图片固定估 28K token。 |
| digest/跨会话描述较早 | 当前仅在实时 recent messages 少于 2 时注入，最多 12 条、1,024 token。 |
| PromptInspection 主要关注 prompt section | 仍不覆盖 messages/tools/payload，但现在已有静态 hash、runtime role、included/truncated/drop reason。 |
| usage 对账是建议 | API 已摘取标准 prompt/completion/total token，但尚未统一厂商缓存/thinking 字段与 request purpose。 |

## 实际只读路径

- `app/ui/pet_window.py`
- `app/core/chat_worker.py`
- `app/agent/context_orchestrator.py`
- `app/agent/context_builder.py`
- `app/agent/session_state_context.py`
- `app/agent/local_context.py`
- `app/agent/sensory_context.py`
- `app/agent/inner_thought.py`
- `app/agent/memory_recall.py`
- `app/agent/lore.py`
- `app/agent/prompt_builder.py`
- `app/agent/tool_loop.py`
- `app/agent/tool_routing.py`
- `app/agent/builtin_tools.py`
- `app/agent/tools/registry.py`
- `app/agent/reply_composer.py`
- `app/llm/context_trimming.py`
- `app/llm/api_client.py`
- `app/llm/chat_reply.py`
- `app/llm/prompts/runtime.py`
- `app/llm/prompts/types.py`
- `app/storage/chat_history.py`
- `app/storage/history_digest.py`
- `docs/context-token-budget.md`
- `docs/agent-handoffs/CONTEXT.md`
- 本批次合同文档
- `docs/agent-handoffs/translation-decoupling/integration-notes.md`
- `docs/agent-handoffs/translation-decoupling/provider-adapter-decision.md`
- `docs/agent-handoffs/spark-history-channel/README.md`
- `docs/agent-handoffs/spark-history-channel/integration-notes.md`
- Alife GitHub 公开 `README.md`（master，2026-08-29 读取）
