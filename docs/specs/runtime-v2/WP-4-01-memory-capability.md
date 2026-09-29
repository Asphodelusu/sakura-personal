---
kind: spec
status: normative
audience: maintainer
source_of_truth: self
status_source: docs/plans/runtime-v2/work-packages.md
updated: 2026-09-14
---

# WP-4-01：Runtime v2 Memory 能力等价

## 个人 Windows 候选的独立后端验收入口

`plugins/builtin/sakura_mem0/personal_backend.py` 提供内部 `open_personal_backend`，仅用于调用者独占的
迁移工作副本。它不由默认插件、设置或导入器选用，不改变个人旧库的现有拒绝保护。

分代布局入口要求显式编码身份和编码器，只接受与个人活动指针及已验证 journal 一致的 ST 384/1024 配置。
模型名或维度相同不等于编码等价；注入的身份是调用者声明，不是模型工件的真实性证明。
它保留既有编码绑定格式的读取，不计算新摘要、不改写绑定、不自动重建或退回旧根索引。
打开前检查包内索引路径、已知集合、存储文件和 SQLite 完整性，以及 mem0 history 表；
这些检查不能替代实体/历史引用的全量迁移校验。

编码器输出的维度、非有限数和布尔值在向量读写前校验。通过准入后，会话持有编码器与后端；
布局拒绝时编码器仍由调用者持有。会话 `operation()` 必须覆盖 mem0 与附属元数据更新全程，
关闭阻止新操作并等待现有租约完成；操作内部关闭明确失败，避免自等待。
该租约是关闭协调，不提供跨数据库事务回滚。调用者不得在租约外使用获得的原始后端。

目前复用候选自带 mem0 raw API，禁止其推理型写入；更新调用者仍负责显式合并 metadata，
scope 授权也由上层适配负责，不能把可按 ID 访问的内部原始 API 直接暴露给插件调用者。
真实 ST 依赖/模型加载、切换发布和真实数据复制尚未完成。

### 旧根布局的隔离召回

明确选择个人插件时，`open_personal_memory_from_snapshot` 也支持无活动指针的旧根
`qdrant/` 布局。必须存在精确的 `embedding_version.txt` 模型名/维度标记，模型仅限已有
BGE-M3 1024维和MiniLM 384维配置；同时检查集合维度、现有SQLite和关联表。活动指针存在
但损坏时仍报错，不能回退旧根。未知标记、缺失库或不完整副本均不得自动创建新库。

旧根没有保存模型修订绑定，不能根据当前snapshot制造历史指纹。打开时重新编码最多3条
现有主记忆，与对应密集向量比较余弦相似度（至少0.999）；无样本或不匹配则拒绝并释放资源。
此为有界兼容检查，不是全部记录或历史工件身份的证明。保留已有命名BM25稀疏向量，复用
mem0混合评分；BM25是否实际可用仍依赖隔离环境的fastembed及本地词表，不能以保留slot冒充命中。

旧根记录适配器拒绝创建/更新/删除及访问时间写入。个人插件只暴露查询和上下文召回，
不注册整理或管理写入。数据库客户端可能更新工作副本内部簿记，因此仍不允许打开原库或基准。
查询上限映射为vendored mem0的`top_k`；角色过滤继续由`user_id`执行。
关联审计结果与可查询性分开报告，旧悬空关联不自动删除，也不因召回成功而改写审计失败。
默认插件与默认导入器不因此改用个人后端；Qt上层精排、实体扩展及完整召回效果需另行验收。

### 个人关联记录的隔离适配

带BM25槽的个人记录新增/正文更新，必须在改动任何持久数据之前完成稀疏编码，并将本次结果交给
后端实际写入；编码不可用时报 `PERSONAL_BM25_WRITE_UNAVAILABLE`，不得只写密集向量或丢弃旧稀疏向量。
没有BM25槽的既有密集索引仍按原格式工作，不自动添加槽。该保护不解除旧根布局只召回限制。

内部 `PersonalCurationStore` 将 `MemoryCurator` 的增删改查接到个人记录存储，构造时固定 scope，
不接受操作参数覆盖所有者。完整列表供本地去重使用；更新在同一记录锁内读取旧值、合并 metadata
并写入，复用现有来源 ID 合并和记录归一化，保留未知 metadata。所有写入仍经过 BM25 预编码、
旧根只读限制和跨存储 pending 标记。该适配器不注册 completed-chat，也不自动开启个人试用写入。
`core_profile` 依赖独立档案存储。普通 CRUD 仍拒绝该层，不能降级为向量记录。只有显式个人写入演练，
且插件配置里有启用的 `coreMaintainer` 时，整理才接受最多 5 条有原话证据的 `core_candidate`。
候选先写入 `core_review_queue.json`，成功后才推进普通整理游标；随后用整理模型做一次有限维护，
失败或取消不改写已经成功的整理结果，也不覆盖损坏的队列或维护状态。配置缺失或关闭时不调用维护模型，
也不写档案、队列或状态。迁移只把旧 `memory.core_maintainer` 写入暂存区的 mem0 插件配置；
目标里已有 `coreMaintainer` 时保留它。运行时没有该配置则保持关闭。

个人召回边界会按当前角色读取副本里的 `core_profiles.json`，并在角色检查之后向
`SakuraMem0Runtime.context` 前置一条私密片段。已存 `content` 或 `memory` 优先；只有整数
schema 2 且这两者都为空时，才按原顺序把 sections 里的非空字符串用空行拼接。未知 schema
只读已有正文，不用 sections 补齐。文件缺失或空白没有片段。编码、JSON 或结构损坏，以及读取前
被换成符号链接、联接或硬链接，只记录诊断代码，不记录正文，也不改写文件。片段含「【常驻档案】」
标签在内不超过 1200 个字符，敏感度为 private，不提升为系统指令。这次读取不依赖 query、top-k、
score 或向量后端是否就绪；关闭后不再返回档案。读取本身不写文件。章节更新走独立的
`patch_personal_core_profile_sections`：只接受已知 V2、当前 scope 和至多两个正式章节，
并用 `base_updated_at` 做乐观锁。校验失败不改主文件和备份。legacy 迁移必须保住原句、数字和引号。

旧根默认仍只召回。内部 `write_rehearsal=True` 仅供一次性写入演练，加载模型前要求当前根有
完整复制标记及绑定该绝对路径的 `.personal-write-rehearsal.json`；存在标记本身不会解锁默认入口。
`tools/personal_memory_write_rehearsal.py` 从完成的 baseline 复制 memory 域，持有 Windows 源文件读锁，
拒绝既有目标及 baseline 内的目标，只在新副本恢复 WAL、检查 SQLite 并生成演练标记。
该机制防止误用，不充当访问控制；不得将演练参数暴露为个人插件默认配置。

独立 `PersonalWriteRehearsalPlugin` 入口可在上述准入通过后订阅 completed-chat。它复用
`MemoryBoundary` 的证据筛选、阈值、模型槽、后台任务与 Timeline 游标，不另建整理调度器。
个人模型加载期间保留待处理 Timeline 通知，就绪后补处理；关闭时取消整理并释放记录存储。
同角色合法事件只触发读取 Host 已提交 Timeline；成功后提交游标，失败或取消不提交，已完成的
逐条写入不回滚。重复通知与重启后的已处理区间由既有游标跳过。这个入口仅供演练，未修改
默认 manifest 或当前个人试用入口；不额外开放 CRUD 工具、模型设置或 `core_profile` 写入。

跨库写入中途失败与编码预检失败不同：前者保留pending标记并阻断后续访问，不能宣称自动回滚。
隔离故障演练可关闭全部数据库后，从写入前副本生成新的恢复目录，保留失败目录供诊断；这不是
生产就地恢复接口，也不能用较早基准覆盖之后成功的用户写入。自动整理逐条操作，游标未推进不代表
整批记忆未变化，接入个人写入前还需验证重放幂等、来源合并与取消边界。

`personal_records.open_personal_memory` 在内部 raw 会话之上提供按 scope 的创建、读取、更新、删除、
实体查询和访问记录；仍不注册到默认插件或导入器。按 ID 的操作核对原记录 `user_id`，
其他 scope 的查询不可见，修改和访问标记明确拒绝。更新显式保留既有 metadata。

实体抽取与别名展开来自个人 Qt 实现，沿用 `entity_index.db/entity_memory` 和
`access_tracker.db/memory_access`；不重建表、不清理未知 `_meta` 字段。
入口要求两库已存在且结构完整，访问追踪的既有 JSON 转换标记必须完成；
缺库或尚未转换的旧数据明确拒绝，不默默创建空库或重新执行旧 JSON 导入。

记忆和关联 SQL 写入串行执行，完整覆盖同一个会话租约。变更前创建
`personal_write_pending.json`，只有所有写入成功后移除。任何中途异常保留标记；
当前适配器后续读写和新建 raw/records 会话均拒绝继续。这是检测并阻止使用部分写入状态，
不代表跨 Qdrant/SQLite 自动回滚、断电原子提交或已经实现恢复工具。

更新前和删除后直接通过共享 Qdrant client 清理旧实体链接，不依赖 vendored mem0 的
延迟初始化状态或吞错清理。共享实体只移除当前记忆 ID，其余引用及向量保持。
失败向上传播并保留未完成标记；SQL 删除同步清除实体映射和访问记录。
真实模型身份、启动前全量引用校验、恢复/切换与实际迁移仍是独立验收门。

### 个人工作副本的引用审计

`PersonalMemoryRecords.audit_references(source_entries)` 在同一独占操作租约内遍历主记忆、
实体向量集合、SQL 实体映射、访问记录与 mem0 历史。调用者显式提供按 scope 分组的
有效对话条目 ID 集合；本接口不自行读取或转换真实 Timeline。
来源引用必须属于同一 scope；实体向量引用既要存在也要匹配 scope；SQL 映射和访问记录
不得悬空。活动记忆应有未删除的历史，已删除记忆保留以 DELETE 结束的历史属于合法状态。
历史检查采用当前 SQLite 追加行顺序，尚未证明外部重排行后的历史具有相同语义。

返回值只含 `ok`、数量及固定错误类别，不含记录 ID/正文。取消或分页异常直接失败，
不返回可误认为完整审计的部分结果。不改变记录，也不清理 pending 标记。
此审计只证明上述引用边界，不证明人格状态、完整 payload、全部实体抽取结果、编码器等价
或一次中断写入的原始意图。因此不得据此自动清除未完成标记或开放切换；
恢复继续采用完整基准副本生成新工作副本，工具和切换验收仍待实现。

`PersonalMemoryRecords.audit_timeline(path)` 可从同一数据父目录内的现有 Timeline 构造
上述来源集合。只读事务核对数据库完整性、激活标识和 entry_id/character_id，
不调用 Timeline 初始化，不提取或输出对话正文。缺失/损坏数据库和越界路径明确失败，
不返回空集合冒充已验证。当前要求离线工作副本；它不提供活跃 Timeline 与 Memory 的跨库快照。
这只覆盖已生成的 Runtime v2 Timeline。个人 Qt SQLite 转换现已通过原导入器生成
`personal_history_rows` 行 ID / entry_id / 分句映射，并保留原始字段。
核对个人 Qt 的 `_memory_metadata` 与整理写入路径：旧记忆保存 `evidence` 摘录和
`source` 类别，不生成历史行 ID 或 `source_entry_ids`。不得把摘录、时间或整理条数猜成
精确引用，也不得为此新增无实际消费者的重映射器。
审计将缺失/空引用计入 `memories_without_source_references`，通过同角色成员校验的引用
计入 `verified_source_references`；畸形、缺失目标及跨角色引用仍报告 `SOURCE_REFERENCE`。
`ok` 仅表示已检查的结构及现有引用无错误，不表示每条旧记忆都有已验证的对话出处。
未知旧元数据原样保留；真实个人记忆接入、完整副本和模型验收仍未完成。

> 计划中的 [WP-4-07R](WP-4-07R-typed-timeline-adaptive-context.md) accepted 后，Memory 将通过只读
> `sakura.host.timeline` 游标消费已提交交互，完成事件不再携带聊天正文。在此之前，本规范下述
> `sakura.host.chat.completed` 与 ChatHistory 行为仍是当前 accepted 契约。

## 1. 目标与范围

本规范定义 CAP-008 在 Runtime v2 的产品行为。长期记忆由 bundled `sakura.memory.mem0` 作为普通 Plugin
API v4 插件提供；Core 不再拥有统一 Memory Service、Memory Router、专用 Bridge 或固定 Memory
Prompt 分支。Mem0 与其他存储模型可以同时贡献上下文，任一插件故障、停用或移除都不能阻断普通聊天或
改变另一 Contributor 的行为。

本能力必须保持：

- 按当前角色 scope 检索长期记忆，并经普通 `sakura.host.context` Contributor 注入聊天；检索、embedding、
  Qdrant、SQLite 或整理失败时聊天仍能完成，且不伪造命中。
- 通过常驻“记忆”页面管理记忆 CRUD，通过通用插件设置管理整理间隔和固定 embedding 模型，通过动态
  模型槽位管理整理 Provider/模型。
- 在已完成聊天事实落盘后异步整理兼容历史；取消、失败或未完成回复不推进整理状态。
- ADR-0032 生效后，只有 Memory 自身启停/reload 才局部 dispose Memory 及传递消费者；任何无关设置保存
  不得关闭 MemoryStore、FastEmbed、Qdrant 或 SQLite，也不得重新 preload embedding。
- v2 正式发布后的 Memory schema migration 可以按 v2 合同演进；正常启动不扫描或导入旧 main 数据。

本规范不维护 Work Package 当前状态；唯一状态源是
[`work-packages.md`](../../plans/runtime-v2/work-packages.md)。Plugin Runtime 的通用行为由
[`sakura-plugin-runtime-v4.md`](./sakura-plugin-runtime-v4.md) 约束。

## 2. 所有权与运行边界

- `plugins/builtin/sakura_mem0` 是 Runtime v2 Mem0 的唯一运行 owner。插件拥有 `MemoryBoundary`、`MemoryStore`、
  `MemoryRecallService`、整理状态、本地模型任务及相关资源；Core 不构造第二个 Memory owner。
- 插件只使用普通 `sakura.host.context`、`sakura.host.tools`、`sakura.host.settings`、
  `sakura.host.model_slots`、`sakura.host.storage`、`sakura.host.character`、`sakura.host.timeline` 和
  `sakura.host.chat.completed`。不得增加 Memory 专用 Host Service、Generic Runtime 分支或公开
  `application_root`。
- 共享 Memory 数据、cache、当前角色和模型凭据只通过这些 Host Service 的受限 descriptor/resolve 合同取得。
  插件不得从 `data_path()` 或自身源码位置反推 Sakura 根目录。
- Mem0、FastEmbed/ONNX Runtime、Qdrant 与 SQLite 位于 Mem0 自己的 generation 私有插件进程和 dependency
  root 中。调用或 cleanup 卡死时只终止 Mem0 及其受控后代，不重启无关插件。
- 自动提炼只由插件拥有的 `MemoryCurator` 和插件配置引用的 Provider/模型完成。Mem0 raw 写入保持
  `infer=False`；整理凭据只通过 `sakura.host.model_slots.resolve()` 取得，本地存储初始化不得联网。
- 当前角色在插件 setup 时冻结。Context request、Collection 投影和 completed-chat 事实的角色不一致时
  fail-closed；不得查询、修改或整理其他角色 scope。
- Memory 只服务 Runtime v2 Plugin Kernel，不导入 Legacy Qt owner、协议或启动链。

## 3. 数据与配置契约

官方插件只使用当前 `user_root` 中的 v2 数据：

| 数据 | 契约 |
|---|---|
| `data/memory/qdrant/**` | v2 本地 Qdrant collection；不得由 Rust/WebView 直接解析 |
| `data/memory/mem0_history.db` | v2 Mem0 SQLite history；由库事务管理 |
| `data/memory/core_profiles.json` | 按角色 scope 保存；仅通过原子写语义修改 |
| `data/memory/curation_state/**` | 保存 v2 Timeline 整理游标和 pending 状态 |
| `data/cache/memory/**` | 用户主动下载的固定 FastEmbed/ONNX snapshot；不进入发行包 |

插件可写配置仅为：

```text
data/plugins/sakura.memory.mem0/config.json
```

字段为 `triggerTurns`、`backfillLimit`、`curationProfileId` 和 `curationModel`。缺失时使用 v2 插件默认值，
不得从旧 Core 整理字段或旧 Memory 模型槽补齐。Provider 目录与解析后的选择通过
`sakura.host.model_slots` 取得；不得直接读取 `user_root/config/api.yaml`，也不得把 Memory 数据或模型 cache
复制到 plugin-data。

`triggerTurns` 只允许整数 `1..50`；`backfillLimit` 读取并保留，不在当前声明式设置页编辑。整理模型引用
必须是已有 Provider profile 与 model 的成对选择；空选择动态继承当前对话模型，只有继承源也不可用时才跳过
自动整理，不影响本地管理、召回或聊天。

embedding 公开模型固定为 `sentence-transformers/all-MiniLM-L6-v2`，维度 384；实际工件固定为
`qdrant/all-MiniLM-L6-v2-onnx@5f1b8cd78bc4fb444dd171e59b18f3a3af89a079`，使用 FastEmbed 0.8.0 与
ONNX Runtime 1.28.0 的 `CPUExecutionProvider`。不得开放任意模型名、URL、revision 或缓存路径输入。
旧 PyTorch cache 不满足已安装状态。

Memory 内容、query、完整历史、Prompt、API key、cache 绝对路径和第三方异常原文不得进入插件状态、通用
Snapshot、日志或证据工件。公开错误只使用稳定 code、简短脱敏 message 和 retryable 标记。

## 4. 协商、贡献与通用接口

Memory 不再协商 `assistant.memory`。只有客户端协商 `assistant.plugins-v1` 后，Core 才创建 PluginApplication
并加载 enabled 的 `sakura.memory.mem0` 进程；未协商时不打开 Memory 存储、不创建 plugin-data，也不暴露插件
设置请求。插件 Tool 是 `assistant.plugins-v1` 的普通 contribution；`assistant.tools-v1` 独立控制 Core-owned
工具与其设置面。当前产品拓扑同时协商两者；仅未协商 `assistant.tools-v1` 时不得无条件注入
`get_current_time` 或因此改变模型请求形态。

桌面和测试只使用通用接口：

- `plugins.settings.get/save/action`
- `plugins.collection.query/create/update/delete`
- `settings.provider_model.get/save` 中的动态 `model_slots`
- 普通 ToolRegistry 调用
- 普通 Context Contributor 调度
- `sakura.host.chat.completed` 事实事件

Rust 不注册 Memory commands，不解析 Memory record，不持有 Memory task handle，也不观察 Memory 专用事件。
Core 协议、Rust command、WebView runtime 和诊断 allowlist 中不得恢复 `memory.*`、`assistant.memory` 或
`memory_gateway` 运行链。

插件注册四个普通工具：

| Tool | 行为 |
|---|---|
| `memory_search` | 搜索当前角色的长期记忆 |
| `memory_remember` | 仅在用户明确要求或信息明显长期有用时，保存通过既有敏感内容校验的记忆 |
| `memory_update` | 先取得准确 `memory_id`，仅用于用户纠正、补充、合并或明显过时的当前角色记忆 |
| `memory_forget` | 仅在用户明确要求时幂等删除当前角色记忆 |

工具随插件 root Effect 注册和撤销。停用、reload 或插件进程退出后旧 callback handle 必须失效；恢复
后只出现一组新 Tool、Context、Settings 与 Collection contribution，不得重复注册。

插件的 Context callback 返回普通受限 fragment。Host 将来源标记为 `plugin:<plugin_id>`、trust 标记为
`untrusted`，并统一执行数量、字符、token、敏感度和总动态上下文上限。调度不得识别 `memory` 来源或预留
Memory/Plugin 固定配额；一个 Contributor 失败时继续选择其他 Contributor 与 Host required facts。

## 5. 聊天召回与整理语义

每个 Mem0 Contributor 每轮最多执行一次相关检索。query 由当前输入和受界近期消息构造；去重、过期过滤、
相关性阈值和最多五条命中沿用 `MemoryRecallService`。命中作为 private context，不回显内部 ID，不进入
日志。初始化中、模型缺失、锁冲突、超时、损坏或任意存储错误均返回空 fragment，Provider 请求与唯一聊天
terminal 继续。

Host 仅在同轮 user 与 assistant 历史都成功落盘且 terminal 为 `chat.completed` 后发送一次
`sakura.host.chat.completed`：

```json
{
  "characterId": "sakura",
  "messages": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ]
}
```

两段 content 与事件总大小服从 Plugin Runtime 的通用上限。事件不提供 History Store、Memory cursor 或整理
方法。插件核对角色后读取既有 `ChatHistoryStore`，按 `triggerTurns` 串行整理；未选整理模型时跳过。事件
Handler 失败、超时或插件进程退出不能改变已经确定的聊天 terminal。

整理写入遇到首个错误立即停止，不再尝试后续操作；每笔操作前及本批结束前检查取消，取消异常原样上报。
已成功的写入可以保留，失败或取消不提前提交游标，不声称整批回滚。重放来源去重读取完整的当前角色
记录，不能只检查前500条；向模型投影的文本仍受独立的20000字符预算约束，不因本地全量去重而扩大。
正常退出先关闭整理与模型任务；卡死由插件
cleanup deadline 终止其受控后代进程。新 generation 只从已原子提交的状态恢复，迟到结果不得写入新
generation 或其他角色。

单个自动整理任务最多发出两次真实 Provider HTTP 请求，包含正常整理和 JSON 格式修复在内；计数必须在
网络调用前消耗，超时和传输失败也不得绕过。任务用尽两次请求仍未成功，或收到确定无效的响应结构后，当前
插件 generation 必须打开自动整理请求保险丝，后续 Timeline 事件不得继续重放该区间；重新加载插件产生的
新 generation 可以从未提交游标重新尝试。该保险丝不得改变用户配置的 `triggerTurns`、既有 Memory 或
Timeline 内容。

整理 Prompt 必须以当前角色人格卡作为身份、关系边界和称呼方式的唯一依据；没有明确称呼时使用中性“用户”，
不得由通用任务说明引入主从、亲属、恋爱、朋友或搭档等关系。自动整理同时区分普通用户事实与有明确双向证据的
共同记忆：共同制定并得到后续反馈、共同解决问题、双方确认的约定、反复形成的互动习惯或用户主动分享事件后续，
可以整理为 `episodic/shared_experience`；角色单方面表达陪伴、一次屏幕观察或只发生在用户一方的事实不得改写成
共同经历。已有记忆使用无依据称呼时，后续整理应保留事实并改回符合当前人格的中性称呼。

## 6. 记忆 Surface、插件设置与 Collection

左侧“记忆”入口常驻。插件通过 `sakura.host.settings` 注册 `surface=memory` 的
`memory_management` section；该 section 只包含 `memories` Collection，宿主在“记忆”页面统一呈现搜索、
筛选、新增、编辑和删除。插件详情页不得重复渲染该 Collection，只提供“前往记忆页管理”入口。没有 active
Memory surface 时，页面显示状态说明和“前往插件页”入口，不恢复 `memory.*` 专用协议。

设置窗口早于 Mem0 插件进程完成初始化时，“记忆”页必须在可见期间用普通有界 timer 重新读取通用插件
Snapshot，并在 Memory surface 可用后原地更新，不要求关闭并重开设置。内容相同的 Snapshot 不得清空
Collection 状态或重绘页面；离开“记忆”页或得到稳定的 `disabled/active/failed` 结果后停止读取。

通用“插件”页面从 `plugins.settings.get` 展示 `sakura.memory.mem0` 的普通 `memory` section：

- section 标题旁的轻量 `status` 运行状态；正常时只显示“运行正常”，异常时展开影响和恢复说明；
- `triggerTurns` 整理间隔。

本地向量模型使用只读 `memory_embedding_component` section，保留历史 `surface=about` 声明。
该资源的管理操作显示在 Mem0 插件设置窗口；“关于 → 组件”只聚合展示安装状态、真实下载进度和跳转入口，
模型页不重复显示。插件设置中的资源卡合并固定 embedding 模型、安装状态、真实下载进度和当前可用 Action：
- 未安装显示 `downloadEmbedding`，下载中只显示 `cancelEmbedding`，失败或取消只显示
  `retryEmbedding`，已安装且空闲不显示操作。独立 `refreshStatus` 不再公开。

整理 Provider/模型不在插件详情中重复显示。Mem0 通过 `sakura.host.model_slots` 注册可选
`plugin:sakura.memory.mem0:curation` 槽位，统一显示在“模型 → 模型槽位”；保存仍写入插件私有
`curationProfileId/curationModel`。插件停用只隐藏槽位，不删除选择；重新启用后若引用已删除 Provider/模型，
页面显示“原选择不可用”并要求重新选择。该可选槽位显示“继承”控件；空选择表示动态继承当前对话模型，
对话模型不可用时才跳过自动整理，且始终不影响召回、聊天或手工 CRUD。

Collection 只公开 `content/layer/category/source/importance/confidence/updatedAt`，item identity 使用通用
`itemId`。layer 只允许 `core_profile/semantic/episodic/procedural/session`；内容上限 16384 字符；查询每页
最多 100 条，并同时受 256 KiB 通用 Collection payload 上限。未知字段、非法 cursor、跨角色记录和超界
响应稳定拒绝或不投影。

模型资源按固定 snapshot revision、必要文件布局和尺寸检查；FastEmbed/ONNX Runtime 在实际加载时验证模型
可加载性。下载和导入不生成或扫描自设内容摘要，已有匹配版本缓存可直接复用，不因移除 SHA 字段重装资源。
模型下载是插件 Settings Action，由插件内部线程执行固定 snapshot 下载。它属于带独立 Runtime 的本地资源，
不是远程 Chat Completion 模型槽位。Action 立即返回，插件页在任务运行期间自动读取 Snapshot，并把
`connecting/downloading/installing/completed` 映射为用户可读阶段；取消只影响当前 plugin generation
启动的任务。失败或取消保留旧完整 cache，
不得晋升 staging 或隐式更换模型。当前不提供 ZIP 导入；未来若恢复，必须由通用 artifact/插件 Action 组合
驱动，不能恢复 Memory 专用 Rust 文件选择 token 或 Bridge。记忆导出本次不实现；未来必须作为 Mem0
插件设置 Action，经通用 artifact/file-save 流程交付。

## 7. 生命周期与故障边界

- Plugin setup 的 Memory runtime、completed-chat Handler、Context、四个 Tool、两个 Settings section、
  Collection 和 model slot 全部绑定同一 LIFO cleanup 栈。setup 任一步失败必须整体反向回收，插件不能
  半激活。
- 启停、reload、安装、卸载以及返回 `restart_required` 的配置只在当前用户操作内局部处理目标插件及其硬依赖
  consumer。先使涉及的旧 Host contribution 和 callback handle 失效并反向清理，再按持久化 enabled 状态
  加载；无关插件、Memory owner 和重资源进程保持不动，不能重放旧 Handler。
- callback、Event、Service 或 cleanup 超时不重试原调用，也不自动重启或恢复。generation 正在
  quiesce/close 时不得再生成替代插件进程。
- 模型下载 cleanup 先发送取消并等待插件线程；无法协作结束时交由插件 cleanup deadline 终止，不允许
  daemon thread 越过 generation 继续写 cache。
- 插件 `disabled/failed`、进程不可用或显式 lifecycle 操作期间，普通聊天仍能在没有该 Contributor 与
  tools 的情况下完成。另一 Memory Contributor 的 Context 不受影响。
- 用户未明确执行 CRUD、配置保存或模型下载时，不得产生相应写入或网络访问。completed-chat 仅允许按既有
  整理语义更新 chat history/curation state 和最终记忆写入。

## 8. 验收门

自动验证至少覆盖：

- 官方 manifest 默认 enabled，并只依赖四个通用 Host Service；当前产品拓扑真实加载该插件。
- 未协商 `assistant.plugins-v1` 时不创建 Memory owner、不打开 Qdrant、不创建插件配置目录。
- 真实 `PluginRuntimeManager → Mem0 process → Host Service → callback → SakuraMem0Runtime.context(dict)` 重建完整
  `ContextRequest`，角色不一致 fail-closed。
- 两个不同 Memory Contributor 同时存在；一个抛错不影响另一个入选，Core/Prompt 不按 Memory 来源分支。
- Tool、Context、Settings、Collection、model slot 在 disable/re-enable/reload 的显式操作后完整
  撤销与恢复；停用时公开状态为 `disabled`，旧 Collection/callback 不可调用，无关插件 scope 不变。
- 设置早于插件初始化完成时，Memory surface 原地恢复；重复的相同插件 Snapshot 不触发页面重绘。
- 模型缺失、依赖导入、Qdrant/SQLite/锁冲突、损坏配置、回调超时和下载取消时聊天继续、v2 数据保持、
  无隐式网络访问。
- 在隔离 v2 根直接比较受测文件内容与数据库记录：Qdrant、SQLite、core profiles 和已安装的固定
  FastEmbed/ONNX snapshot 在只读设置/搜索路径保持不变；completed chat 只允许当前 curation-state
  语义变化，不为验收额外扫描真实模型或用户目录。
- 正常退出、插件停用、reload、插件调用/cleanup timeout、Core crash 后线程、callback、Effect、pipe、文件锁与后代
  进程有界归零。
- Frontend、Rust、Python focused tests，以及 `runtime-v2-memory-tests` 与当前产品 smoke journey 通过；
  无法本地执行的平台/真实模型门明确记录风险。

所有测试只能写隔离临时根，不得以真实用户 Memory 数据、配置、日志或 cache 作为 fixture。

## 9. 非目标与回退

本 WP 不建设统一 Memory Service/Record DTO、Memory 专用 Bridge、权限系统、通用推理代理、跨 owner
事务框架、逐插件
进程、在线模型市场或任意下载器。它不自动扫描或迁移外部旧程序数据，也不修改 vendored Mem0 源码。

回退时先把官方插件 desired state 设为 disabled 并停止接收新调用，再 dispose 当前 Worker；超时终止当前
generation Worker/后代。可以恢复代码入口，但不得删除、回滚、重建、迁移或手工修复用户 Qdrant、SQLite、
Memory JSON、curation state、模型 cache、旧 YAML、插件配置或已完成聊天历史。
