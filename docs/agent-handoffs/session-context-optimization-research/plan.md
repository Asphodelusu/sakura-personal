# 推荐实施路线

## 总体决策

优先顺序是：先补测量，再减少结构失败的完整重打，再稳定可缓存前缀；Gemini thinking 作为独立 provider 工作流。工具 schema 动态化和更强 session 摘要都暂缓到数据证明值得做。所有阶段保持角色卡、角色资源与翻译 sidecar 不变。

## P0：建立可对账基线（低风险快赢）

### P0.1 请求分区与 purpose 观测

建议方向：

- 在 `app/llm/prompts/types.py` / `runtime.py` 扩展只含长度/hash 的 inspection 数据结构。
- 在 `app/llm/api_client.py` 发送前计算 canonical payload 分区：system、messages、runtime、tools、图片估算；记录 bytes/estimated tokens/hash，不记录正文。
- 在 `app/agent/tool_loop.py` / `reply_composer.py` 为每次调用传递或记录 `request_purpose`：`initial`、`tool_step`、`semantic_compose`、`structural_repair`。
- 在 ChatWorker/PetWindow 现有 interaction 计时上补统一的 first bubble/first TTS 关联字段；若不想碰 UI，第一版可由日志分析脚本离线关联。
- 标准 usage 外，白名单提取 provider 返回的 cached input 与 thinking/reasoning token；不存在就记 null，不猜。

依赖：先确定 interaction/request id 传播边界和 canonical JSON 规则。禁止把 prompt/message/tool 正文写入默认日志。

证明变好：P0 不宣称变快，只要求同一 synthetic turn 能自动得到 request count、purpose、bytes、估算/真实 token、stable-prefix bytes、总耗时与首泡/首声时间，且 debug off 时无正文。

建议测试：

- `tests/unit/test_prompt_templates.py`
- `tests/unit/test_context_orchestrator.py`
- `tests/unit/test_api_client.py`
- 新增建议：`tests/unit/test_prompt_inspection_payload.py`
- 若改 UI 计时：最小 `tests/ui` interaction lifecycle 用例

### P0.2 合成基线夹具与报告

建立无真人内容的固定 fixtures：

1. 短闲聊：2 个短 turn，无工具。
2. 长历史：超过 8 个 user turn，并接近 40K token。
3. 带图：一个 data URL 占位或 fake image part，只验证预算/序列化，不发真实 API。
4. 工具轮：一次 memory-like fake tool call + result + final。
5. 坏结构：围栏 JSON、缺引号、纯日语无 JSON、语义缺失各一例。
6. 重启续接：空实时窗口 + synthetic SQLite entries；recent messages 达 2 后 digest 消失。

Fake transport 分别使用 DeepSeek-like 与 Gemini-like capability，不调用线上服务。生成机器可读 JSON/CSV 和短 Markdown 汇总。角色效果用 10～20 条 synthetic RP prompt 人工盲评：身份一致、上一轮承接、是否复读、是否客服腔、事实忠实、主动性是否扁平。

## P1：减少完整重打并稳定前缀

### P1-A：结构修复快车道

实现顺序：

1. 在 `app/llm/chat_reply.py` 保留现有确定性 repair，并暴露更精确的失败类别：syntax/envelope/schema/language/semantic_unknown。
2. 在 `app/agent/reply_composer.py` 建立三路：本地修复成功直接采用；完整日语只做最小 envelope 修复；需要重新吸收问题/工具证据才调用现有 full semantic compose。
3. 最小修复输入只包含原始模型输出、最小 segments schema、合法 tone/portrait 枚举；不含 persona、tools、历史、memory/runtime。输出后检查 ja 文本规范化等价、segment 顺序、tone/portrait 合法；失败即回到 full compose。
4. `missing_translation` 永远不触发该快车道，继续交给现有 sidecar。
5. 开关默认先 shadow classify：记录“本可快修”但仍走旧链；合成夹具一致后再启用。

目标门槛建议：

- 正常合格 turn 请求数完全不增加。
- synthetic 纯结构失败至少减少一次 full-context request。
- 快修后的 ja 内容与原始语义文本等价；任何语义缺失 fixture 必须回退 full compose。
- 翻译 sidecar、工具证据 final、observer/relationship reply 路径回归通过。

可能触及测试：

- `tests/unit/test_chat_reply_normalization.py`
- `tests/unit/test_reply_translation_decouple.py`
- `tests/unit/test_agent_runtime.py`
- `tests/unit/test_web_search_stall_reply.py`
- `tests/ui/test_translation_sidecar_ui.py`

### P1-B：静态 recipe 与 tool-set fingerprint

实现顺序：

1. 先用 P0 数据确认相邻同类 turn 的 static section/toolset hash 是否稳定。
2. 在 PromptRuntime 附近增加建议模块 `prompt_cache.py`，只缓存渲染后的静态 recipe；动态 `ContextSnapshot` 每轮重建并保留在尾部。
3. 在 ToolRegistry 附近增加 canonical tool-set 描述缓存；key 包含 active groups、allowed capabilities、browser filters、registry/schema revision。
4. 失效信号集中化：角色、设置、recipe/prompt patch、intimacy section、portrait catalog、endpoint/model、runtime role、工具注册/权限/组变化。
5. 缓存命中与失效原因进入 P0 指标；不能只记录“cache hit”而不校验 hash。

目标门槛建议：

- 同一输入状态下 payload 对应分区 byte-identical。
- 任何失效事件后下一请求 hash 变化，且不会复用旧角色/旧工具权限。
- 本地构建耗时不回退；DeepSeek cached usage/TTFT 只有在线授权后才评估，不作为单测门禁。

可能触及测试：

- `tests/unit/test_prompt_templates.py`
- `tests/unit/test_context_orchestrator.py`
- `tests/unit/test_agent_runtime.py`
- `tests/unit/test_api_client.py`
- 新增建议：`tests/unit/test_prompt_cache_invalidation.py`

### P1-C：Gemini thinking capability（可与 P1-A/B 独立排期）

1. 先按目标模型官方文档确认 OpenAI 兼容 endpoint 支持的实际字段；做 fake payload RED。
2. 在 provider capability 层映射 Sakura 的 `disabled/low/high` 意图；Google endpoint 不再只是剥掉 DeepSeek 字段。
3. 短闲聊/工具/结构修复走最低，明确深思走高；不支持则省略，不用 400 作为常态探测。
4. 以 Gemini synthetic payload snapshot、空 content、usage 和 P50/P90 对账；不改 DeepSeek 路由。

可能触及测试：`tests/unit/test_api_client.py`、`tests/unit/test_turn_routing.py`。

## P2：仅按证据启动

### P2-A 工具包动态暴露

启动条件：P0 显示普通闲聊 tools schema 占总输入达到评审阈值，且调用日志足以设计高召回路由。先 shadow 计算“本可发送哪些工具”，再比较真实工具调用是否被覆盖。保留 native tools、能力发现和完整 core fallback；不默认 XML 双轨。

建议触及：`app/agent/tool_routing.py`、`app/agent/tools/registry.py`、`app/agent/tool_loop.py`。不要与 P1-A 同一实现合同并行修改 `tool_loop.py`。

### P2-B session 连续性夹具/可选摘要增强

启动条件：重启 synthetic + 用户实测显示现有 digest/长期记忆不足。先增强来源、版本和测试，不先新增模型摘要调用；若确需滚动摘要，另做数据保留、覆盖规则和成本预算。禁止永久全历史回放。

建议触及：`app/agent/session_state_context.py`、`app/storage/history_digest.py`、`app/agent/context_builder.py`。

## 后续实现合同的低重叠切法

建议每批只有一个集成者，机械验证优先交给快速 agent；不要让两个 agent 同时改 `tool_loop.py` 或 `api_client.py`。

| 合同 | 独占生产文件建议 | 只读接口 | 依赖 |
|---|---|---|---|
| Measurement | `prompts/types.py`、建议新增 inspection 模块、日志分析脚本 | `api_client.py`、`tool_loop.py` | 无，最先做。若必须改 api_client，由该合同独占。 |
| Structural repair | `chat_reply.py`、`reply_composer.py` | `tool_loop.py`、translation sidecar | Measurement purpose 接口先落地。 |
| Static cache | `prompts/runtime.py`、建议新增 `prompt_cache.py` | `prompt_builder.py` | Measurement hash 口径。 |
| Toolset cache/dynamic tools | `tools/registry.py`、`tool_routing.py`、`tool_loop.py` | builtin tool definitions | 与 Structural repair 错峰。 |
| Gemini capability | `api_client.py`、provider capability 新模块 | `turn_routing.py` | 与 Measurement 的 api_client 改动错峰。 |
| Session continuity | `session_state_context.py`、`history_digest.py`、对应测试 | `context_builder.py` | P0 重启夹具先落地。 |

每份实现合同都应记录初始 HEAD/status、独占文件、RED、最小 GREEN、相关测试、未触碰范围、commit/push 策略。共享工作区中由最终集成者统一跑一次全量门禁和 push。

## 测量矩阵

| 场景 | 核心指标 | 体验检查 |
|---|---|---|
| 短闲聊 | request count、system/tools/messages/runtime bytes、thinking token、input→first bubble/TTS | 简短自然、无过度解释、人设不扁。 |
| 长历史 | 裁剪前后 token、保留 user turn、stable prefix、digest 是否重复 | 能承接最近事实、不复读旧话。 |
| 带图 | 图片估算与实际 usage 差、payload bytes、vision fallback | 不把视觉所见伪装成长期记忆。 |
| 工具轮 | 每步 toolset hash、工具结果 token、final compose 次数 | 工具证据进入答案、无“正在查”空话。 |
| 坏结构 | failure class、full vs minimal retry、ja 等价、首次展示延迟 | 内容不被二次模型改写。 |
| 重启续接 | digest token、注入轮数、历史 channel | 知道刚才发生什么，但不机械复述。 |
| DeepSeek/Gemini | cached input、thinking token、HTTP P50/P90、400 fallback | 同一盲评集的角色一致性。 |

没有线上 A/B 时，先用 fake transport 证明“请求构造和调用次数”变化，再用用户授权的少量真实 provider 样本验证网络/缓存/thinking；最后由用户主观实机判断代入感。优化接受条件必须同时满足：延迟/成本至少一项有证据改善，RP 盲评不下降，observer/relationship/translation/tool 回归不破坏。

## 停止条件

- P0 发现额外请求很少、主要耗时来自首个 provider HTTP：停止 C，优先评估 E 或供应商选择。
- tools schema 占比很低：不做 D。
- DeepSeek cached usage 未改善且本地构建耗时本就很小：B 只保留稳定指纹，不扩建复杂缓存。
- 重启承接盲评已合格：不做额外滚动摘要。
- 任一优化要求读取/记录真实角色卡或聊天正文、绑定翻译主模型、默认关闭独白/记忆/observer：否决。
