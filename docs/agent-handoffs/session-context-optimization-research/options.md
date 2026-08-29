# 会话上下文优化选项

以下五项可以分阶段叠加，但每项都有独立开关和否决条件。百分比均是相对于“该场景当前基线”的预估范围，不是线上实测；若缺基线则明确写测量方法。

## 基线：保持当前行为并补齐对账

当前系统已经具备合理的窗口/digest/长期记忆分层、动态 context 预算和翻译 sidecar。任何方案都应先用同一批合成夹具记录当前请求数、payload bytes、token、首泡/首声与 RP 盲评，避免把厂商波动误判成架构收益。

## 选项 A：完整 payload 观测与稳定前缀指纹（推荐 P0）

机制：扩展现有 PromptInspector/结构日志，对 system recipe、tools、message prefix、runtime context 分别计算脱敏长度与 hash；为每次请求标记 `initial/tool_step/semantic_compose/structural_repair`，记录相邻请求稳定前缀 bytes/tokens 及失效原因。只观测，不缓存正文。

| 维度 | 判断 |
|---|---|
| 效果 | RP 行为持平；能定位“稳定化是否误删了关系/记忆”而间接保护连续性。 |
| 效率 | 主调用数和 token 直接持平；日志哈希 CPU 预计毫秒级。后续可证明缓存候选比例，未知值由合成夹具与脱敏实机日志测量。 |
| 损耗 | 低复杂度；新增 interaction/request purpose 与 hash 口径，日志略增。hash 输入必须是序列化后的真实 payload 分区，而非 Python 对象地址。 |
| 风险 | 若误把正文写入日志会造成隐私泄漏；默认只记录长度、hash、枚举原因，debug body 沿用现有脱敏开关。 |

前置条件：定义 canonical JSON 序列化和稳定顺序。否决条件：实现需要记录真实聊天或角色卡正文；应立即缩回仅 hash/长度。

## 选项 B：稳定 recipe/tools/message 前缀与会话级构建缓存（推荐 P1）

机制：不缓存模型回答，只缓存本地已渲染的静态 recipe 与 canonical tools JSON；动态 runtime 保持尾部。维护 `prompt_fingerprint` 与 `toolset_fingerprint`，确保同一角色/recipe/模型槽的静态序列字节不漂移。消息仍 append-only，裁剪窗口滑动时记录失效。

失效条件：换角色、角色/系统设置变化、recipe 或 prompt patch 热更、intimacy 状态改变导致 section 集变化、portrait catalog 模式改变、切模型/endpoint、runtime context role 从 system 回退 user。工具缓存还需在 active group、capability/permission、工具注册/description/schema/顺序、browser 路由过滤或 `search_tools` 扩组时失效。

| 维度 | 判断 |
|---|---|
| 效果 | 角色连续预计持平；固定 section 顺序可减少无意漂移。若错误复用旧 intimacy/关系入口，效果会明显变差，因此只能缓存静态渲染，不能缓存动态 snapshot。 |
| 效率 | 本地 prompt 构建 CPU 小幅下降；网络上传 bytes 基本不变。DeepSeek 若服务端前缀缓存按字节命中，未缓存输入计费/TTFT可能下降，幅度未知；用 provider usage 的 cached token 和 P50/P90 测。Gemini 未验证缓存时主要收益是可测性。 |
| 损耗 | 中等复杂度；需要集中失效与版本号。内存为若干 KB～几十 KB 的字符串/JSON，较小。 |
| 风险 | 过期人格/工具权限、跨角色污染、供应商行为绑定。缓存 key 必须含 scope、角色、endpoint/model 与版本。 |

前置条件：先完成 A 并证明相邻轮静态 hash 本应稳定。否决条件：稳定化要求把动态事实挪到静态前缀，或缓存命中不可观测却引入大量状态。

## 选项 C：结构修复快车道，语义合成保留全上下文（推荐 P1，最高延迟收益）

机制：在 parser 后把失败分为三类：本地可修 JSON；语义完整但缺 envelope；语义缺失/工具证据未吸收。前两类只用“原始输出 + 最小 segments schema + tone/portrait 允许值”做确定性修复或单次小模型封装，并校验 ja 文本等价；第三类才走当前完整 persona/runtime/history 的语义合成。禁止把 zh 缺失归入重打。

| 维度 | 判断 |
|---|---|
| 效果 | 纯结构失败时角色内容更忠实，因为不让第二个模型重写；工具证据不足时仍走完整合成。若分类器误把语义失败当结构失败，会出现答非所问，必须 fail closed。 |
| 效率 | 发生结构失败的 turn 可少 1～2 次完整主模型请求；该 turn 输入 token 预计下降 60%～95%，首句可减少约一次 13–15 秒级重打（仅脱敏锚点，不是承诺）。正常成功 turn 持平。 |
| 损耗 | 中等复杂度；需定义等价校验、最小 schema、fallback 与请求 purpose。可先只做确定性修复，避免新模型槽。 |
| 风险 | 破坏日语正文、tone/portrait 丢失、工具结果未进入答案。任何文本不等价或字段不合法立即回退现有完整合成。翻译继续 sidecar。 |

前置条件：失败原因和原始输出结构可观测；有合成 malformed fixtures。否决条件：目标 provider 的坏输出经常同时缺语义，或等价校验无法可靠区分。

## 选项 D：按意图暴露工具包，native tools 保持主协议（候选 P2）

机制：先测六个 core schema 占比与使用率；若显著，将工具按稳定包分组。普通闲聊提供能力发现与场景必需的最小包；明确回忆时加入 memory 包；不确定意图回退完整 core。`search_tools` 扩组后更新本轮 toolset fingerprint。pseudo/XML 只保留端点不支持 native tools 时的兼容兜底，不作为默认双轨。

| 维度 | 判断 |
|---|---|
| 效果 | 正确路由时持平，可能减少误调用；漏路由时会让角色“明明会却没工具”，伤害真实感。能力发现和完整 core fallback 是必要护栏。 |
| 效率 | 普通闲聊输入 token 下降幅度未知；先以 tools schema token / total input token 比例测量。若 schema 只占很小比例，收益不足以承担路由复杂度。调用次数通常不变。 |
| 损耗 | 中高复杂度；路由、组缓存、权限、browser 模式和多步扩组测试面扩大。native 单轨可控制复杂度。 |
| 风险 | 工具不可发现、权限 schema 过期、组变化破坏前缀缓存。XML 双轨还会增加注入与 parser 风险，因此不推荐默认化。 |

前置条件：A 证明 tools 占比足够大，且真实调用日志能形成高召回路由。否决条件：工具占输入低于预先设定阈值（建议评审时定 5%～10%），或最小包使关键能力召回下降。

## 选项 E：Gemini provider-specific thinking 控制（独立快赢候选）

机制：为供应商能力层增加 Gemini 等价 thinking 设置，而不是复用 DeepSeek `thinking`。短闲聊、工具轮和结构修复使用最低/关闭；用户明确要求深思时提高。首次实现前必须用目标模型官方文档与无敏感合成请求确认实际 endpoint 参数、支持值和 usage 返回。

| 维度 | 判断 |
|---|---|
| 效果 | 简单 RP 预计持平或更少过度解释；复杂推理若误关会变差，所以保留 turn routing 的深思分支和按模型 fallback。 |
| 效率 | Gemini 简单 turn 的隐藏输出 token/延迟可能下降，具体未知；按 model + thinking setting 比较 P50/P90、reasoning token 与盲评。DeepSeek 与其他 provider 持平。 |
| 损耗 | 低到中等；新增 provider capability、配置/默认值与兼容测试。 |
| 风险 | 参数名/能力随模型和兼容 endpoint 变化；错误透传可能 400 并多打一枪。应在首个 payload 就按能力构建，不靠 400 学习。 |

前置条件：官方参数确认和 fake payload 测试。否决条件：目标 OpenAI 兼容端点没有显式控制，或控制后结构合格率/RP 盲评显著下降。

## 选项 F：连续生命线的分区增强，不做永久全历史回放（可选 P2）

机制：保留实时窗口 + session digest + 长期记忆三层；给 session state 增加结构化来源/版本与可测的“重启首两轮承接”夹具。只在现有层无法承接时考虑短期滚动摘要，不把全部 SQLite 历史放入主请求。

| 维度 | 判断 |
|---|---|
| 效果 | 重启后的承接与关系连续可能改善；实时深聊不受摘要重复干扰。摘要过度概括可能把暂时情绪固化，必须低权重、可被新互动覆盖。 |
| 效率 | 普通进程内 turn 持平；重启头两轮最多增加现有 1,024 token 量级。若引入摘要生成，会增加后台调用，需另立成本预算。 |
| 损耗 | 仅增强夹具/来源时低；新增滚动摘要时中高，涉及版本、更新时机、回滚与隐私。 |
| 风险 | 旧关系误导、复读、私人信息驻留更久。不得让 digest 覆盖 core profile，也不得把 observer 输出当用户互动。 |

前置条件：先用“正常退出/突然关闭/关系变化后重启”夹具证明当前缺口。否决条件：当前 digest+memory 已达到承接要求，或改造只能通过永久回放全历史实现。

## 推荐组合

先做 A；若数据确认重打是主要延迟源，优先 C。B 与 E 可并行评估但独立上线。D、F 只有在观测证据证明 schema 占比或重启承接确有问题时进入 P2。

## 明确不做

- 不把 Sakura 整仓重写成空壳/全插件架构。
- 不复制 Alife 的 AGPL 源码、prompt、XML 协议或插件清单。
- 不把已拆开的字幕翻译重新绑回主对话模型。
- 不把默认关闭 inner thought、记忆或 observer 当作主优化方案；只能作为明确标注体验代价的实验开关。
- 不做永久全 SQLite 历史逐轮回放。
- 不同时维护 native tools 与自研 XML 两套默认协议。
- 不用“请求体变小”替代 RP 盲评，也不把作者自述缓存率当作 Sakura 实测。
