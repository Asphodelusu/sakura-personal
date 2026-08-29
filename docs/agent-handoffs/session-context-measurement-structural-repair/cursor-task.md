# Cursor Desktop Task — 请求测量与结构修复快车道

## 开始前

- 读取 `docs/agent-handoffs/CONTEXT.md`、本目录全部文档，以及 `docs/agent-handoffs/session-context-optimization-research/{findings,options,plan}.md`。
- 当前集成分支 `dev5`，预期 HEAD `cb240b60`，相对远端 ahead 2。先运行 `git status --short --branch`；不得覆盖其他脏改动。
- 使用 `D:\sakura\.venv\Scripts\python.exe`，严格 RED→GREEN。
- 不 commit、不 push；完成后只填写本目录 `integration-notes.md` 的 Cursor Desktop 段。

## Phase M — P0 安全测量

### 必须得到的指标

每个模型 HTTP 请求应能关联当前 `interaction_id`、本轮 request index 与 `request_purpose`：

- purpose 至少区分 `initial`、`tool_step`、`semantic_compose`、`structural_repair`；未知调用明确记 `unknown`，不得猜错。
- canonical 分区至少有：system、non-system messages、runtime/dynamic context（若当前代码无法无歧义拆出，记录为 null 并解释）、tools schema、image estimate、whole payload。
- 每区只记录 bytes、estimated_tokens、hash；默认日志绝不记录 prompt、message、tool result、角色卡、图片 data URL 或 API Key 正文。
- 记录相邻同 endpoint/model/purpose 请求的 stable-prefix bytes/hash 或等价可对账指标；定义 canonical JSON（UTF-8、稳定键序、稳定分隔符），避免偶然 dict 顺序。
- 白名单提取标准 input/output/total usage、cached input、reasoning/thinking tokens；供应商缺失时为 null，不估造。
- P0 不改变 payload、重试次数、路由、模型参数与回复行为。

### Synthetic fixtures/report

建立无真人内容、无真实网络的固定夹具，至少覆盖：短闲聊、长历史/裁剪、带图占位、一次 fake tool round、结构坏输出。DeepSeek-like 与 Gemini-like fake response 至少验证 usage 差异。可输出机器可读 JSON 及短 Markdown/文本摘要；测试产物写 pytest tmp_path 或明确忽略的临时目录，不污染 `data/`。

### 推荐文件范围

- `app/llm/prompts/types.py`（仅确有需要）
- 建议新增 `app/llm/payload_inspection.py`
- `app/llm/api_client.py`
- `app/agent/tool_loop.py`（仅 purpose 传播/观测，不改工具行为）
- `app/agent/reply_composer.py`（Phase M 只接 purpose）
- 聚焦测试与可选 `tools/` 下离线报告脚本

若需要触碰 UI 才能记录 first bubble/TTS，本批先不做 UI；保留现有 interaction stage 日志，离线按 interaction id 关联，并在风险中说明。

## Phase R — P1-A 最小结构修复

### 分类与路由

- 复用并收紧现有 `chat_reply.py` 确定性解析/repair，输出精确失败类别：至少能区分 syntax/envelope/schema/language/semantic（命名可按现有风格）。
- 已是合法 segments：零额外 compose。
- 仅缺 `zh`：零主模型 compose，继续交给 translation sidecar。
- 纯日语非 JSON、围栏/包裹错误等“内容已完整、仅 envelope/schema 不合格”：允许结构修复快车道。
- 缏失关键信息、工具结果后需要重写答案、答非所问、无可采用日文正文：继续走现有 full semantic compose。

### 最小修复请求

- 只发送：原始输出、最小 segments schema、合法 tone/portrait 枚举和“不得改写日文语义/顺序”的结构任务。
- 不发送 persona、完整历史、memory/runtime、tools schema、图片或工具结果正文。
- 输出必须校验：日文规范化后与原始可采用文本等价、顺序不变、tone/portrait 合法。任何不确定立即 fail closed 回退现有 semantic compose。
- `request_purpose=structural_repair`；回退的完整合成为 `semantic_compose`。
- 不把翻译重新绑回主模型，不改变 TTS、字幕、Observer、relationship 路径。
- 正常合格回复调用数不得增加；结构修复失败的最大请求数不得超过现有上限。

### 推荐文件范围

- `app/llm/chat_reply.py`
- `app/agent/reply_composer.py`
- `app/llm/api_client.py`（仅使用 Phase M purpose/inspection 接口，不新增第二套日志）
- `app/agent/tool_loop.py`（只在现有合成调用边界接 purpose）
- 对应聚焦测试

## 明确禁止

- 不修改 `characters/`、人格/关系/亲密提示、`data/config/api.yaml`、Key、真实聊天/记忆/日志数据库。
- 不修改 translation sidecar、Observer/relationship、`pet_window.py`、`subtitle_controller.py`、TTS。
- 不引入永久全历史、XML tool 双轨、动态工具裁剪、静态 prompt cache 或 Gemini thinking；这些不属于本批。
- 不在默认日志写任何请求/回复正文，即使 debug enabled 也优先保持摘要；若现有显式诊断模式已有正文行为，不扩大它。
- 不调用真实 provider，不跑可能启动真实 Sakura/TTS 的全量门禁。

## TDD 与交付

先为 Phase M 获得 RED 并 GREEN，再开始 Phase R。结果需记录每个 RED 的实际失败原因。至少验证：

1. canonical hash 对键顺序稳定、对内容变化敏感，且 inspection 结果无正文。
2. purpose/index/interaction 关联正确，工具轮与合成不串位。
3. cached/reasoning usage 白名单提取，未知字段不泄漏。
4. 合格 segments 与 missing-translation 均不触发结构修复。
5. 纯日语完整正文走最小修复，最小请求中不存在 persona/history/tools/runtime。
6. 修复改写日文、乱序、非法 tone/portrait 时拒绝并回退 semantic compose。
7. 工具证据会改变答案时仍走 semantic compose。
8. DeepSeek/Kimi/Gemini 现有 payload 兼容测试保持通过。

跑最窄测试和直接相邻集合，最后 `git diff --check`。不要跑完整 `tests/unit tests/ui`。在 integration notes 中写：模型/耗时、文件、逐项 RED/GREEN、测量 schema 示例（仅键与数字）、请求状态转移、未解决风险。
