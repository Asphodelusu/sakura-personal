# 需求：Sakura 会话与上下文优化调研

状态：需求已批准，供 Codex 规划与调研。本批不实现、不改行为。

工作区分支：`dev5`。共享上下文见 [`../CONTEXT.md`](../CONTEXT.md)；其中「当前集成分支」可能过期，以本文件为准。

## 1. 背景与动机

Sakura 每轮聊天会重新拼一套大上下文：人格 recipe、关系 guide、回复协议、工具定义、记忆/独白/runtime 片段，再加本轮消息。近期 Gemini 3.6 Flash 实测一轮主对话 HTTP 体约 31KB；首轮可见回复只有几十到一百字，却因格式不合格连续合成/修复，整轮等到首句约 50 秒。DeepSeek 前缀缓存对「少变的前缀」友好，Google OpenAI 兼容层则未必。

[Alife](https://github.com/BDFFZI/Alife) 把会话当成一条几乎不断的生命线：上下文分区维护、提示词刻意稳定、函数调用走自研明文/XML 以省词元，并声称缓存命中很高、日均成本很低。作者自述，未经第三方复现。Sakura 不需要做成 Alife，但「会话怎么复用、哪些块必须每轮变、失败重打要不要再喂整包」值得对照。

产品优先级（与 `CONTEXT.md` 一致，调研时按此排序）：

1. 代入感与角色连续（效果）
2. 首句文字 / 语音延迟（效率）
3. Token、模型调用次数、费用（效率）
4. 本机 CPU / 内存 / 复杂度 / 回归风险（性能损耗）

## 2. 目标

Codex 交付一份**可执行的规划**，不是口号。读者（后续实现 Agent 或用户）应能直接决定做哪一档、不做什么、先测什么。

必须回答：

1. Sakura **现在**一轮用户消息从输入到首句，会话/上下文实际走了哪些层、每层大约多重、哪些会触发第二次及以上模型调用。
2. Alife 公开描述里，哪些会话手法和 Sakura 目标对齐，哪些和现有观察/关系/独白/翻译侧路冲突。
3. 3～6 个互斥或可叠加的优化选项；每个都要有机制说明、触及模块、效果/效率/损耗对照、前置条件和否决条件。
4. 推荐分阶段路线：P0 低风险快赢、P1 结构改造、明确不做的事。
5. 验证与测量计划：没有线上 A/B 时，用什么日志字段、合成夹具、对比基线来证明「变快了且戏没扁」。

## 3. 范围

### 3.1 要调研的会话面

按数据流，而不是按目录名：

- 内存窗口：`self.messages` / ChatWorker 入参，工具轮是否进历史。
- 持久历史：SQLite 聊天历史、跨会话 digest（`session_state_context.py`）。
- Prompt 组装：`PromptRuntime`、recipe 静态块、`cache_scope` / `static_hash`、`ContextOrchestrator`、plugin context providers。
- 运行时注入：内心独白、记忆召回、关系 A 注入、时间/事件、屏幕观察摘要。
- 裁剪：`trim_messages_for_model`（现为 token 预算 + 保最近 8 个 user turn）。
- 工具定义：每轮随请求发送的 tools JSON；core 组默认可见工具。
- 厂商差异：DeepSeek `thinking` / 前缀缓存 vs Gemini 剥掉 `thinking` 后默认 medium thinking；OpenAI 兼容层。
- 失败重打：`reply_composer.py` 结构化合成/修复。注意：结构化 segments 缺中文翻译已不再为 `missing_translation` 打第二次 Pro；**无结构纯日语正文仍会首轮拉起合成**。Gemini 上仍见过 `missing_translation` → `missing_segments` 连打多枪。
- 翻译侧路：主回复与中文字幕是否已解耦；会话规划不得把已拆开的翻译再绑回主模型。

### 3.2 明确不在本批

- 不改 `app/`、`tests/`、`plugins/`、UI、配置、角色卡、提示词文案。
- 不引入 Live2D、插件市场、AI 热编译自己、多开赛博世界、QQ。
- 不把 Sakura 重写成「空壳 + 全插件」。
- 不复制 Alife 源码、提示词、XML 协议或插件清单（AGPL-3.0）。
- 不读取或摘录真实聊天正文、角色卡隐私段、API Key、`data/memory` 档案正文。
- 不 commit、不 push。

### 3.3 允许只读的实现入口（建议，可增补并在 findings 里列出）

- `app/agent/context_orchestrator.py`
- `app/agent/context_builder.py`
- `app/agent/session_state_context.py`
- `app/agent/local_context.py` / `app/agent/sensory_context.py`
- `app/llm/prompts/runtime.py` 与 `app/llm/prompts/types.py`
- `app/llm/context_trimming.py`
- `app/llm/api_client.py`（payload 组装、thinking 剥离、重试）
- `app/agent/reply_composer.py` / `app/llm/chat_reply.py`
- `app/agent/tool_loop.py`（工具轮与 working_messages）
- `app/agent/turn_routing.py` / `app/agent/prompt_builder.py`
- `app/storage/chat_history.py` / `app/storage/history_digest.py`
- `docs/context-token-budget.md`（2026-06 设计稿，须与当前代码核对，过期处标出）
- `docs/agent-handoffs/CONTEXT.md`、`translation-decoupling/`、`spark-history-channel/` 中与历史/通道相关的已落地结论

Alife 只读其公开 README / 架构目录说明，不把仓库 clone 进 Sakura、不把 AGPL 文件写进本仓。

## 4. 问题清单（调研必须覆盖）

### Q1 稳定性

哪些 prompt / tools / messages 前缀在相邻两轮可以字节级不变？现在实际变了多少？对 DeepSeek 前缀缓存、Gemini 请求体积分别意味着什么？

### Q2 唯一会话 vs 窗口会话

Alife「永久唯一会话」若套到 Sakura：与现有「启动清空内存窗口 + digest 续接 + 长期记忆」如何并存？连续生命感应落在哪一层（内存、SQLite、core profile、记忆压缩），而不是把全部历史每轮塞给主模型。

### Q3 重打税

格式失败时，第二次请求是否必须重发人格+工具+全历史？有没有「只修结构、不再推理」的更小上下文？对 RP 效果的伤害有多大？

### Q4 工具面

默认 6 个 core 工具是否每轮都要出现在 schema 里？明文/XML 调用作为弱模型降级，和现有 native tools 双轨的成本与风险？

### Q5 厂商 thinking

Google 默认 thinking 与 Sakura 关闭 DeepSeek thinking 的目标冲突。会话规划是否应把 `thinking_level` / 等价开关列为独立选项（可与会话稳定分开做）？

### Q6 测量

现有 `PromptInspector`、`Latency`、`API` 日志哪些字段足够做前后对比？缺什么指标？合成夹具应覆盖：短闲聊、长历史、带图、工具轮、Gemini 与 DeepSeek 各一条。

## 5. 每个方案必须写的对照表

对每个选项用同一张表，禁止只有形容词：

| 维度 | 要写清的内容 |
|---|---|
| 效果 | 角色连续、人设遵守、少复读、少「失忆」、少机械口吻。标清是改善/持平/可能变差，以及什么场景会变差。 |
| 效率 | 预估：主模型调用次数、输入 token、输出（含隐藏 thinking）token、输入到首句延迟。尽量给区间（如 −20%～−40% 输入 token），并写假设。 |
| 损耗 | 实现复杂度、新增状态/缓存失效、内存、CPU、测试面、厂商绑死、调试难度。 |
| 风险 | 回归（翻译侧路、工具循环、观察主动）、隐私（更长驻留上下文）、锁死某家 API。 |

没有日志数字时，写「未知 + 测量方法」，不要编造精确百分比。可用 2026-08-27 `interaction-4` 一类**已脱敏**结构作定性锚点：首轮 HTTP ~7.7s / 包体约 4KB 对可见 82 字；其后合成/修复各约 13–15s；整轮处理约 53s 才开口。不要引用或转写该轮对白。

## 6. 推荐方案的约束

- 第一阶段必须能在不改人格卡、不改角色资源的前提下做。
- 不得把「关掉内心独白 / 关掉记忆 / 关掉观察」当成主方案，除非作为可关实验开关单独列出，并写清体验代价。
- 主对话与翻译侧路保持分离。
- 若建议「会话级 prompt 缓存」，必须说明失效条件（换角色、改设置、热更 recipe、切模型槽）。
- 若建议「跨轮复用 tools 前缀」，必须说明 tool 组变化（`search_tools` 展开）时如何失效。

## 7. 交付物

全部写在本批次目录，中文：

| 文件 | 内容 |
|---|---|
| `findings.md` | 当前管道图（层、关键函数、默认预算数字）、与 2026-06 `context-token-budget.md` 的差异、Alife 思路对照（仅概念）。 |
| `options.md` | 3～6 个选项 + 统一对照表 + 明确不做的清单。 |
| `plan.md` | 推荐路线（P0/P1/P2）、依赖、建议的后续实现合同怎么切文件、测量计划。 |
| `integration-notes.md` 的 Codex 段 | 状态、读过的路径、结论摘要、风险。 |

`plan.md` 必须让下一个实现 Agent 能据此拆独占文件；本批本身不拆实现任务。

## 8. 验收标准

见同目录 `acceptance-tests.md`。通过 = 文档齐全且可执行，不是测试变绿。
