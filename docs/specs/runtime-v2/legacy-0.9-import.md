---
kind: spec
status: normative
audience: maintainer
source_of_truth: self
updated: 2026-09-14
---

# Sakura 0.9.x 到 Runtime v2 数据迁移合同

## 个人 SQLite 聊天历史边界

个人 Qt 版 `ChatHistoryStore` 将每个角色的 JSONL 路径映射为同名 `.db`，SQLite 为当前写入源。
个人候选已将 `.db` 接入原有 Timeline 转换与增量合并路径；同角色以 SQLite 为准，
不重复导入残留 JSONL。只接受已停写、无待合并 WAL 的离线来源，以只读 immutable 连接校验
完整性、表结构、行 ID、字段类型和带时区时间；不推测无时区时间，不对源库 checkpoint。
损坏或无法完整表达的记录返回 `LEGACY_PERSONAL_HISTORY_INVALID`，未合并日志或孤立旁文件返回
`LEGACY_PERSONAL_HISTORY_WAL_PENDING`，均不退回 JSONL。

Timeline 内 `personal_history_rows` 保存角色、源相对路径、SQLite 行 ID、目标 entry_id、
assistant 分句序号及完整原始字段 JSON；error 行归档为 `legacy_error` 系统条目。
重复导入沿用源身份，原始字段变化也纳入冲突预览和确认，映射随增量事务写入。
已有同角色 JSONL 导入或另一源路径的目标返回 `LEGACY_PERSONAL_HISTORY_IDENTITY_CONFLICT`，
避免无可靠对应关系时重复导入。此时需要新的迁移目标，不能凭时间戳猜测身份。
上述仅经合成数据验证；真实数据复制、个人 Memory 接入和日常运行切换仍待验收。
个人 Qt 的记忆写入使用 evidence 摘录，不生成聊天行 ID 引用，因此不凭摘录补造引用。
已有新版 source_entry_ids 继续按角色审计；无引用单独计数，不等同于出处已验证。

## 个人候选的离线副本机制

`app.legacy_import.personal_copy.prepare_personal_copy` 是内部离线复制原语，无生产 CLI，
不自动探测或停止日常安装进程。调用者须独占目标父目录，并在调用全程保持源停写，
通过必填 `source_is_quiescent` 核验；回调、文件状态盘点和逐字节比较本身不能证明停写。

源目标不得重叠，目标必须不存在；拒绝路径中的符号链接、Windows reparse point、硬链接和
特殊文件。保留未知文件、空目录及数据库伴随文件；不打开源数据库，不计算新摘要。
空间检查后建立新目标，并先写 `.sakura-personal-copy.json` 的 incomplete 状态；复制后
逐字节比较内容并复查源盘点，只有全部成功才替换为 complete。状态只表示复制完成，
不代表 SQLite、编码器或业务语义验收通过，也不承诺断电耐久性。

取消、源变化、重新出现写入者、内容不一致或 I/O 失败时，保留未完成目录，不覆盖重试。
从基准副本恢复也只新建目标，保留失败工作副本。含 `personal_write_pending.json` 的源，
或已带有未完成/损坏复制状态的基准，拒绝作为复制源。
记忆准入与旧导入检查副本祖先目录的状态；未完成或损坏状态返回
`PERSONAL_COPY_INCOMPLETE` / `LEGACY_PERSONAL_COPY_INCOMPLETE`。

当前仅合成目录验证。真实停写所有权、独立基准保管和运行时切换仍未接入，
不能把该函数作为真实数据迁移一键入口。

### 个人副本联合验收入口

`memory_contract.inspect_personal_migration` 接受独占的工作副本根、明确的编码身份和
编码器；要求根目录存在 complete 复制标记，Timeline 已由历史转换器生成。
调用前检查祖先状态、整个副本的 link/hardlink/pending 边界，读取 Timeline 后再打开
个人索引与实体/访问元数据库，调用既有引用审计，返回不含正文的计数及问题分类。
资源在成功、取消、准入拒绝和审计失败时关闭；编码器自调用开始归此入口负责释放。
审计问题返回 `ok=false`，准入或读取失败抛异常，不把失败转成空报告。

该入口现可传 `snapshot`，由 `open_personal_memory_from_snapshot` 从绑定读取编码身份，
按个人 Qt 相同的路径/内容 SHA-256 算法核对本地工件，离线加载 SentenceTransformer，
设置绑定的 max_seq_length，检查实际维度并在加载后再次核对摘要。禁止远程代码加载，
保持 encode-default 语义，不添加 query 前缀或归一化。snapshot 与注入 identity/encoder 互斥。
记录入口提供固定角色过滤的 search，复用现有操作租约与关闭流程。

该入口不转换/复制数据、不修改默认导入准入、不启动 Core，也不发布切换资格。
后端打开可能维护数据库内部状态，因此不得对基准或日常数据调用；complete 标记只证明
复制流程完成，不能证明实际独占，调用者仍负责独占工作目录。注入编码身份仍是调用者声明，
不是实际模型身份已验证。本地 snapshot 路径已接通；384/1024 维替身测试验证调用合同。
2026-09-17 已在本机现有 BGE-M3 snapshot 上完成真实 SentenceTransformer + Qdrant/SQLite
合成数据验收：1024 维编码、同角色召回、跨角色隔离、关闭拒绝、重开及副本联合审计通过，
模型工件前后摘要一致。测试显式禁止网络/LLM，实体 NLP 使用替身，因此不覆盖完整实体抽取、
Core/plugin 启动、真实记忆迁移或真实停写。MiniLM 未发现本地模型，仍只有替身验证。

### Windows 源文件占用保护

`personal_windows_copy.prepare_windows_personal_copy` 包装上述离线原语，并要求同样的外部
停写核验。仅支持本地 Windows 目录，不接收 UNC 路径。先盘点，再按目录优先的顺序以
CreateFileW 的只读共享方式持有原生句柄，全部取得后再次核对盘点；持有至复制结束。
已有写入句柄或删除共享冲突会拒绝复制；持有期间现存源文件不能写入、删除或替换。
失败和取消都关闭已取得的句柄，不强制结束进程、不创建或清理日常实例锁。

此方式不是卷快照。目录新增文件仍可能发生，由复制盘点拒绝；进程是否退出、多个存储是否
属于同一业务时点仍需调用者保证。原生句柄不替代外部停写回调，不足以独立授权真实复制。
当前 Windows 真实文件共享行为只在合成目录验收，尚未测试日常 Qt 进程退出和重新启动。

## 个人候选：尚不支持的记忆布局

隔离宿主可显式构造 `SakuraMem0Plugin(personal_snapshot=...)` 启用个人召回试运行。
它从 Host storage/character 接口取当前角色及数据目录，要求根目录 complete 副本标记，
检查记忆目录的链接边界，后台加载模型。仅注册现有 context provider 与 memory_search；
不注册管理写入、下载入口、自动整理或完成事件，默认插件构造行为不变。
加载期间立即返回 loading；加载/搜索失败返回 degraded 和空结果，避免伪造命中。
退出等待加载或已有查询结束，关闭个人记录与模型；不强制中断模型加载。
本阶段按角色检索及 layer 过滤复用个人记录和已有 DTO/召回投影；layer 在返回候选中筛选。
独立 worker 可显式选择 `plugin:PersonalRecallPlugin` 入口，并在插件私有 config.json
配置绝对路径 personalSnapshot。原 plugin.yaml 仍指向默认入口；不通过默认配置隐式启用。
个人模块同时支持包导入与 runner 的顶层模块导入，禁止访问 Core 私有模块的约束保持有效。
真实 BGE-M3 + 合成数据库已通过 Python 3.11 的真实 `-I -S` Plugin API v4 子进程验收，
包括 RPC 注册、搜索、context、角色隔离、模型缺失降级和关闭；Host 服务响应由测试提供。
尚未验收桌面 Python 3.12 的个人插件依赖、完整 Core 初始化/主对话及 UI 生命周期，
不能称为生产接线完成。

源或目标 `data/memory` 存在 `active_index.json` 或 `indexes` 时，预检、首次导入和增量导入
返回 `LEGACY_PERSONAL_MEMORY_UNSUPPORTED`，在复制和数据库合并前停止。标记损坏、类型错误或断链
也不能降级成空库；不隔离这些数据后宣称成功，不读取指针选择旧根目录作为替代。
MemoryStore 创建、后端配置和已有库验证同样在打开后端前拒绝，原因码为
`MEMORY_PERSONAL_INDEX_UNSUPPORTED`。插件创建失败不等于已经验证主对话可以降级运行。

这是个人 ST 编码器、多代索引和关联存储接入之前的保护边界，不是迁移实现。
它不能识别所有没有布局标记的旧 ST 库；同名模型、相同维度或普通 384 维合成库测试通过，
均不能证明 SentenceTransformers 与上游 FastEmbed ONNX 的编码结果等价。

## 入口与生命周期

迁移只出现在首次导航页。`first_run_guide_completed` 为 false 时 Shell 必须以 paused lifecycle 启动并主动显示
首次导航窗口，不得依赖等待 Core 的隐藏桌宠 WebView 来触发该窗口，也不得在用户
选择路线前打开 Core、Timeline、Memory 或插件。普通首次使用通过 `first_run_start_core` 显式启动；迁移路线只接受
原生目录选择器返回并由 Rust 保管的 opaque `selectionId`，WebView 不得提交路径。

命令固定为 `legacy_import_choose_source`、`legacy_import_inspect`、`legacy_import_state`、`legacy_import_start`、
`legacy_import_cancel`；进度事件为 `sakura://legacy-import-progress`。选择目录只保存 opaque selection 和脱敏目录名，
随后由独立 inspect 命令扫描并显示来源领域、阻断项和需要授权覆盖冲突项的领域。用户点击“开始迁移”前，存在冲突领域时必须
显示弹窗；start 参数中的确认领域必须与 inspect 结果完全一致。`overwriteDomains` 保持 v1 schema，但只表示该领域
存在需要授权覆盖的冲突，不表示授权删除整个领域。状态为
`idle → selected → inspecting → ready → staging → validating → committing → core_validating → completed/failed/cancelled`。
目录选择完成后，页面必须立即显示 `inspecting` 状态和不确定进度；扫描失败时回到 `selected`，允许重新选择目录后再次检查。
取消只在 staging/validating 接受，commit 后必须完成或回滚。

只支持同平台的 Windows 0.9.x → Windows v2 与 macOS 0.9.x → macOS v2；不支持跨平台搬运运行资源。
来源平台以发行 Runtime 布局识别，不能仅凭目录名或仓库中可能同时存在的多平台启动脚本推断。Windows/macOS 的
0.9.x 来源与 1.0.x 目标必须位于不同物理目录；相同、包含或被包含关系继续 fail closed。1.0.x 安装器、
Updater 和普通启动不扫描或复用 0.9.x，也不建立旧目录 snapshot；唯一入口是用户显式选择后的只读导入。
各自版本内部的发行根与用户根可以相同；目标中已有角色、Timeline、Memory、配置、TTS或用户插件不得阻止迁移重试。首次迁移使用
“合并并保留”：目标独有内容保留，只有同路径文件或同稳定身份记录发生内容冲突时才要求确认并允许旧版数据覆盖；
跨角色身份冲突永远不可覆盖。配置会跨文件投影到当前 schema，只要源、目标配置树均非空就保守列为冲突领域。
payload中的同名文件以本次旧版迁移结果覆盖，覆盖前必须进入事务 backup；
Core校验失败时恢复原目标文件。payload未涉及的目标文件保持不变。未恢复的 legacy import journal/staging仍阻止
新迁移并先走恢复。正常启动不得扫描旧目录。角色包和 TTS 是可恢复的最佳努力域；它们可以从本次 payload 缺席并以
warning 完成迁移。

Timeline 按稳定 entry ID、Memory 按 point ID、history row ID 和 profile key 合并：目标独有记录保留、完全相同记录
跳过、同角色同身份冲突经确认后由旧版记录覆盖。`characters`、`tts` 以现有目标树为基础叠加旧版结果，保留目标独有文件，
同相对路径或路径类型冲突由旧版 payload 覆盖；任一可选域无法完整构造时必须丢弃该域全部 staging 输出，保留整个原目标树
并产生 warning。`data/chat_history`、`data/memory` 中目标独有的未知文件同样保留，Memory 中旧版未知扩展文件按同路径
覆盖。合并后删除可重建的 curation state，由 Core 按角色重建游标，不复用只描述单套 Timeline 进度的旧 cursor。

## 事务与安全

导入器位于 `app/legacy_import`，使用当前发行 Python 离线运行，不 import 旧安装源码。源目录全程只读。inspect
按 `data/config`、`data/chat_history` 等结构与 schema 识别，不以目录名作为唯一依据。
长期使用后只剩部分有效域的 0.9.x 来源仍可迁移：`data/config` 与 `data/chat_history` 不要求同时存在，
但必须至少识别到一个旧版用户数据域，并继续通过同平台发行 Runtime 与 0.9.x 版本证据门禁。缺失配置时生成
当前安全默认投影；仅有旧 JSONL 历史时可作为 0.9.x 结构证据。来源/目标重叠、目标链接、活动旧进程、事务恢复和
目标 Memory 损坏等会危及数据一致性的条件不得因此放宽。
所有 legacy-import Python 命令必须经同一个跨平台 managed process-tree runner 启动：stdout 按行流式解析机器协议，
stderr 持续排空；正常结束释放托管关系，协议错误、异常退出、父进程退出或取消时终止整棵子进程树，不得留下 descendant。
每次执行使用绝对 operation deadline：`inspect-data` 15 分钟，`inspect`、`recover`、`finalize`、
`rollback`、`apply-data` 各 30 分钟，完整 `run` 2 小时。每次 pipe poll 后都必须同时检查 deadline；到期后
取消 stdout/stderr reader，并在既有 10 秒 finalization deadline 内终止整棵进程树。安全终止返回
`LEGACY_IMPORT_OPERATION_TIMEOUT`，先按 journal 完成并确认 recover/rollback，再允许重启 Core；进程树
状态无法确认时返回 `LEGACY_IMPORT_PROCESS_TERMINATION_FAILED`，保持 Core 停止并保留 journal，禁止继续
恢复或启动；如果此时也无法确认 Core 已停止，返回 `LEGACY_IMPORT_CORE_STOP_FAILED`，不得声称 Core 已关闭。
前两个错误在首次导航、设置页和统一运行日志中使用固定中文投影；Core 停止失败在设置页使用单独的固定
中文投影。任何投影都不得包含子进程输出或路径；
不增加 heartbeat、自动重试或常驻 watchdog。

完整 payload 写入目标同卷 `.legacy-import-staging-*`；`characters`、`tts`、`data/chat_history` 和
`data/memory` 按原子树 rename，其余文件逐文件 rename，并为既有同名目标保存
`.legacy-import-backup-*` 和脱敏 journal。journal一直保留到 Core达到 `ready/degraded/setup_required`：

- `ready/degraded` 且角色投影可用：finalize并确认 journal 已清除，再写首次设置完成标记并进入桌宠；
- `setup_required`：finalize并确认 journal 已清除，再进入缺失角色/Provider设置，不删除已迁移数据；
- `failed`、超时或不可读取：停止 generation并 rollback；下次可重新迁移；
- 进程异常退出后，下次启动在 Core 前依据 journal自动回滚。

journal中的文件操作必须先持久化意图再执行 rename。Core校验成功后先持久化 `finalizing` 再删除 backup；一旦进入
`finalizing`，恢复逻辑只能继续清理，禁止回滚已验证的数据。
journal 读取是 `Missing`、`Readable(state)`、`Unreadable(error)` 三态：只有真正缺失才表示没有待恢复事务或 finalize
已经完成，损坏 JSON 和未知 state 一律以 `LEGACY_JOURNAL_INVALID` fail closed。增量 apply 返回成功前必须读到
`pending_core_validation`。apply 子进程异常退出、协议错误或 finalize 失败后，Rust 必须先恢复事务：
`committing`、`pending_core_validation`、`rolling_back` 完成 rollback，`finalizing` 只继续清理。journal 不可读或恢复失败时
Core 保持停止；只有恢复完成且再次确认 journal 已清除后才能重启 Core，禁止在混合树上启动。

目标路径及每个现存祖先必须在 inspect 和实际 rename 前拒绝符号链接、Junction/reparse point。覆盖确认不授予写出
`user_root` 的权限；检查后被并发替换的祖先也必须在 commit 边界再次失败。

回滚必须先持久化 `rolling_back`，并在每个反向文件或原子树操作完成后原子持久化剩余工作。删除操作可以安全重放；
backup 已恢复到目标但进度尚未落盘时，恢复逻辑必须识别目标已恢复并只推进进度，不得再次按 installed 删除它。
兼容旧 `committing`、`pending_core_validation` journal 时，也必须先保守识别已经恢复或从未移走的目标，再进入
`rolling_back`。backup、staging 或 journal 清理失败时保留 journal，下次启动只继续安全的剩余回滚或清理；末尾
清理不得删除迁移前已存在的空 `characters/`、`tts/` 目录。

报告固定为 `data/legacy-imports/<id>/report.json`，只含域、数量、大小、相对标识、稳定错误和警告。报告和常规事件不得含 API Key、聊天/记忆正文、绝对源路径或旧 `.env` 内容。
失败日志和遥测的原始异常、路径及栈遵循 [远程诊断](remote-diagnostics-telemetry.md)，仅替换具体凭据。文件清单按相对路径排序，
只记录 domain、id 和 bytes，不计算或输出内容摘要；逐文件检查取消。复制时必要的相等判断直接分块比较 bytes。
离线迁移进程不得打开或追加 `data/logs/sakura-runtime.log`，也不得接收日志文件路径。它只通过 stdout 机器协议向
Rust 父进程提交白名单内的结构化 diagnostic；Rust 丢弃子进程自由 message，使用固定中文目录投影并由唯一 writer
记录 import operation、领域、步骤、安全 diagnostic、异常类型、稳定 reason code 及 SQLite/OS 错误码。角色包或
TTS 被跳过时，报告和统一日志必须记录稳定 warning，但最终状态仍为 completed。未知迁移
事件必须丢弃，不得另建迁移日志。失败 UI 必须显示这一个统一日志的相对路径。
每个主要阶段至少记录 started/completed，包括历史、长期记忆、配置、辅助数据、当前加载器校验、角色、TTS、清单和事务提交；
完成事件记录计数、字节数、隔离数和兼容修复数。配置加载失败还必须记录具体 loader 名、相对配置路径、异常类型、稳定错误码，
以及可用的 SQLite/OS/YAML 行列信息。Rust 对 diagnostic attributes 使用有界标量字段；原始异常、恢复失败、实际路径和调用栈保留在专用诊断字段，
不上传完整配置、任意嵌套对象或命令输出文件。
用户点击检查来源后，即使尚未生成 import ID，也必须以 opaque selection ID 关联 inspect started/completed/failed，记录平台、版本、
空间、冲突数量以及稳定 blocker/warning code，不能记录所选绝对目录。

## 数据映射

- 旧 API profiles、密钥、地址、模型槽及允许名单内 `.env` 字段转为当前 `config/api.yaml`。旧环境变量
  白名单固定为 `BASE_URL → llm.base_url`、`API_KEY → llm.api_key`、`MODEL → llm.model`；仅当 YAML
  对应值缺失、为 null 或空白时填充，已有非空 YAML 始终优先。只读取源根仍在使用的 `.env`；
  `.env.migrated` 表示旧版迁移器已成功归档，不得重放。旧 `system_config`
  只投影当前有效的工具循环、屏幕感知、记忆整理和 UI 字段。MCP Server 中已废止的
  `requires_confirmation` 字段（包括 tool policy 内嵌字段）直接删除，保留 Server 及其当前仍有效的配置。
  MCP Server 的 `command`、`args` 或 `env` 若引用旧来源根或其子路径，必须按路径组件边界识别并隔离，
  Windows 路径匹配须统一正反斜杠、大小写和 `\\?\` namespace 前缀，不能继续执行旧安装源码。
  0.9.x PR#110 的 `text_*`、`vision_*` 选择字段必须转为当前 `chat`/`vision_chat` 模型槽；已有当前形态的
  `model_slots` 时以其为准，`text_enabled=false` 且尚无模型槽时由旧视觉选择生成 `chat`。输出必须删除这些
  选择字段及 `model_names`，并把旧 Provider 可接受的模型列表规范化为当前 `models[].name`，同时保留 Provider
  顺序、密钥和允许的未知字段。
  旧 Provider 列表中的非对象记录、缺少稳定 ID/地址而无法使用的记录可逐项丢弃；旧别名字段
  `profile_id/name`、`api_base/url` 可投影为当前 `id`、`base_url`，缺少 alias 时使用 ID，非字符串空密钥按空值处理，
  字符串模型列表转为当前形态。已存在的未知 Provider、模型和 slot 扩展字段继续保留。可修复的 MCP timeout、非字符串
  当前角色选择和当前模型槽标量使用安全默认或字符串投影，并以 `LEGACY_CONFIGURATION_COMPATIBILITY_APPLIED` warning
  记录修复数量；这些可重建兼容字段不得导致整棵配置被隔离。
  旧屏幕感知的 `enabled` 与 `screen_context_enabled` 合并为当前单一 `enabled` 字段。
  已确认来源的旧内置 Web MCP 按[联网插件](web-plugin.md)合同迁移，保留关闭选择和工具限制，停用旧项。
  源 MCP 缺失时不生成禁用空配置；导入目标已有的联网插件开关优先。自定义脚本不按路径后缀替换。
- Timeline和长期记忆必须先于其他域迁移。二者的角色身份来自旧聊天 scope、curation scope 和当前角色 ID；角色包
  只参与可唯一确定的大小写规范化，不拥有聊天或记忆。角色包随后尝试完整复制并由当前 `CharacterRegistry` 校验；
  复制、转换或校验失败时清除 staged `characters/`、确认该目录已不存在后记录
  `LEGACY_CHARACTER_IMPORT_SKIPPED` 并继续；锁或权限等原因导致清理无法确认完成时，整个迁移必须在 commit 前明确失败。
  旧版
  `compat_default` 等内部主题来源标记统一转为当前 `package`，保留实际主题颜色，不得因此拒绝角色。角色校验必须在
  大型 TTS 复制前执行。角色运行选择由应用语音设置管理，不因资源导入自动开启。资源转换生成
  `sakura.tts.gpt-sovits` 与 `sakura.tts.genie` 两个角色 extension，使导入后切换已安装引擎不需要重新导入角色。共享模型与参考配置写入 GPT-SoVITS extension，Genie 在运行时继承，避免路径副本
  遮蔽后续 Studio 编辑或语音包导入；能唯一匹配的 ONNX 只补入 Genie extension，不覆盖已有显式路径。
  大小写只能做唯一匹配，冲突阻止提交。
- JSONL 按 archive 后 active 顺序导入。user → human；相邻 assistant 行合并为一个或多个不超过上限的 segments
  entry；已知 error、未知 role、坏 UTF-8/JSON及非法时间不成为事实，原始行 bytes进入隔离，其余记录继续。
  不安全 portrait清空该字段并隔离原始行，但文字仍导入。源身份由角色 scope、role、规范时间和同时间同 role出现序号
  确定；Timeline 使用随机 ID，并在同一 SQLite 的 `legacy_history_identities` 表保存源身份、kind 与 ID 的映射。
  导入优先复用目标中的映射；active 文件尾部追加不能改变此前 ID，assistant 分块由首条源记录标识。
  无映射的旧版导入按角色、kind、规范时间及 Timeline 中的出现顺序关联既有 legacy entry，保留 entry/turn ID；
  正文变化仍报告冲突，不重新计算旧摘要。映射随 Timeline 一起提交或回滚，正常读取无需转换旧 ID。
  实现必须以二进制逐行迭代，发现问题时立即写入 quarantine，assistant 只保留当前不超过 `MAX_SEGMENTS` 的分块，不得缓存整文件 bytes、完整 parsed-record 列表或
  完整 issue 列表；隔离内容必须保持原始行 bytes 不变。
- 手动截图 marker 从 human正文剥离并生成 `manual_screen` observation；定时/自主 marker生成
  `scheduled_screen` observation；可关联的旧视觉摘要进入 observation，原始 store进入隔离区。
- `data/memory` 的 Qdrant、mem0 SQLite和 profile必须迁移且不重新 embedding。mem0 SQLite不得作为普通的
  主库/WAL/SHM 文件组合逐个复制；必须使用 SQLite backup API从旧库读取一个一致事务快照，合并已提交 WAL，
  且不得修改旧主库、WAL或复用旧进程的 SQLite `-shm`；只读备份连接正常更新的 `-shm` 读锁槽位不属于用户数据
  变更。快照只需通过 `quick_check`，不得把复制时序或加载器异常误报为旧数据库结构不兼容。无法打开的源 SQLite或
  Qdrant子存储必须原样进入隔离区，其他可读 Timeline/Memory继续提交并产生 warning；不得用一个损坏的源子存储回滚
  所有不可替代数据。目标 SQLite、Qdrant 或 profile 无法打开/读取时必须返回
  `LEGACY_DATA_TARGET_MEMORY_INVALID`，不得删除或重建；目标 collection 创建或 upsert 失败返回
  `LEGACY_DATA_TARGET_MEMORY_WRITE_FAILED` 并终止事务，不得误报 completed 或 quarantined。
  mem0 SQLite在 staging中通过与 Core 相同的 SQLite manager补齐缺失的可空字段；旧 `history` 的额外字段和
  既有行必须保留，不得要求旧库预先符合当前新建库的精确结构。缺失或旧结构的 `messages` 短期缓存表可以补齐；
  只有缺失稳定行 ID、无法安全补齐时才清空并重建该缓存表。迁移器不得用手写的 Qdrant metadata、profile shape、
  精确 schema或向量维度门禁提前拒绝已完整复制的记忆；提交后的当前 Core 启动是最终兼容性校验。旧整理 count 和
  cursor 均不能同时描述合并后的新旧两套 Timeline 进度，必须按可重建缓存清除并由 Core 从空 cursor 按角色重建。
  只要本次迁移包含长期记忆，迁移器还必须在提交前准备并校验当前 Runtime 固定 revision 的 FastEmbed ONNX
  模型。目标已有完整模型时直接保留；源目录含同一固定 revision 时复制到 payload；旧版 Hugging Face
  Safetensors/PyTorch缓存不得冒充 ONNX模型，否则使用当前正式下载逻辑把模型直接写入 payload。
  用户取消仍中止迁移；模型准备或固定工件校验失败只产生 warning，必须先提交已保全的 Memory，随后允许当前插件按正式
  资源流程补齐模型。报告记录 `memoryModelFiles` 和 `memoryModel` bytes，模型准备进度位于长期记忆阶段内且早于 TTS。
- Windows 顶层 TTS Junction只跟随一次，内部 link 在实际复制时拒绝该可选域；断链、目标重叠或未知布局在 inspect 中产生
  warning 并跳过 TTS，不得阻止聊天和记忆迁移。macOS 0.9.x 的 `data/tts_bundles/installed` 映射到 v2 `tts/`；
  仅保留词法目标仍在该 TTS 树内的相对符号链接，越界相对链接拒绝该可选域并产生 warning，绑定旧安装绝对路径的符号链接
  不复制并写入迁移 warning。可执行位等 POSIX 文件模式必须保留。识别资源复制到 v2 `tts/`；
  `data/tts_bundles/onnx` 中能唯一匹配角色 ID 的模型进入对应角色 `voice/onnx`，孤儿模型保留在
  `tts/onnx`；旧绝对运行路径不得保留，包括 Python `site-packages/*.pth` 中的旧安装目录。Windows 对空的
  TTS staging目录可以使用受控的多线程系统复制，
  但传给系统复制工具前必须去掉目录选择器产生且该工具不支持的 Win32 `\\?\` namespace前缀；必须保持相同的
  噪声排除和 link拒绝规则，支持取消，以实际复制字节持续发布进度，并在复制后复核文件数与总字节。系统复制
  返回码、安全诊断和复制前后统计必须经父进程进入统一 Runtime日志，以区分预扫描、系统复制、后扫描和 ONNX合并
  失败。任一 TTS 复制、合并、路径适配或配置校验失败必须清除该域的 staging 输出，并仅在确认该目录已不存在后记录
  `LEGACY_TTS_IMPORT_SKIPPED` 或相应稳定 warning 并继续提交；锁或权限等原因导致清理无法确认完成时，整个迁移必须
  在 commit 前明确失败。用户取消仍中止整个事务。历史、长期记忆、配置及
  其他非 TTS 用户数据必须先完成迁移和当前加载器校验，角色包的最佳努力结果也必须已确定；TTS作为最后一个数据域复制，
  避免轻量配置错误导致重复复制大型资源。校验完成后，独占的
  顶层 `tts/` 以单个目录事务提交，journal必须先记录目录安装/既有目录备份意图；不得为其中每个资源重复扩写
  journal。Core校验失败时整目录回滚并恢复安装器原有的空目录。
  现有目标 TTS 与旧版 TTS 合并时，必须以旧版 staging 为覆盖层，只补入目标独有路径；不得先复制完整目标树再重复复制
  完整旧版树。复制旧版资源和补入目标独有资源必须分别持续上报进度，不能在固定百分比后执行无反馈的全树复制。
  用户请求取消后，在暂存树确认删除前界面必须明确显示正在清理，并提示大型 TTS 清理可能需要数分钟；清理完成前不得
  报告 `cancelled` 或允许开始下一次迁移。
- inspect 的 `requiredBytes` 只表示完成非可选域所需空间，不包含角色包、TTS和 TTS bundle；可选域空间不足按上述
  warning 语义跳过，不能在 inspect 阶段拒绝核心迁移。
- 笔记、提醒、任务、角色工坊和 `sakura_mobile` 数据进入当前路径。其他旧插件代码、插件私有数据、运行事件、
  视觉原始记录和旧 history原文进入 `data/legacy-imports/<id>/quarantine`，不进入 `plugins/user` 且不执行。
- 资源根下的日志、diagnostics、无关 cache、lock、临时下载和旧 migration backup不迁移；不能仅按目录名
  递归排除依赖包内部的 `diagnostics`、`cache` 等真实代码目录。上一条明确要求的当前 ONNX记忆模型缓存属于
  长期记忆可用性工件，不在“无关 cache”排除范围内。

## 系统页增量导入

Settings → System 的“导入角色历史记录和记忆”只接受已识别为 0.9.x 的用户目录；1.0/Runtime v2 目录和普通
文件夹必须明确拒绝。该入口接受 Rust保管的 opaque selection，只读取旧目录的
`data/chat_history` 与 `data/memory`。inspect时 Shell停止 Core，比较 Timeline entry、Qdrant point、mem0 history
row和 `core_profiles.json` 后重新启动；公共计划只包含角色 ID、计数、计划内冲突编号和随机 plan token，不含正文、向量、绝对
路径或记忆内容。

相同稳定 ID且规范内容一致时跳过；目标缺失时新增；同 ID且内容不同时列为冲突，并且只有此时 UI显示覆盖确认。跨角色的
entry/turn/point身份冲突不可覆盖。inspect 将规范比较内容、源/目标路径和本次分配的身份映射保存到系统临时目录中
每个随机 token 独占的目录，创建权限为 `0700`，已存在的目录或链接一律拒绝复用；计划文件权限为 `0600`。
Windows 使用当前用户的临时目录及继承 ACL。读取和清理拒绝符号链接、junction，以及 POSIX 上非当前用户所有或
向组/其他用户开放的目录。最多保留 8 份，只删除核验后的计划文件和空目录，不递归删除。公共 token 仅为随机 ID。
inspect 与 apply 可在不同进程执行。apply 再次
停止 Core，按该映射重建计划，直接比较必要内容与分类结果；内容变化、文件缺失或源/目标不一致返回 stale，需重新
检查。成功提交后删除临时计划；正文和向量仅供本地比较，不进入公共输出。合并只在
当前 `data/chat_history`、`data/memory` 的 staging副本上进行，清除可重建 curation state，再通过原子树 journal提交。
Core启动失败时回滚并重新启动原数据。此入口不得导入配置、角色包、TTS、插件或辅助数据。
Memory scope 必须同时校验 Qdrant payload 的 `user_id`/`scope`、history 的 `user_id` 以及
`memory_id → point scope`；任意非空身份不一致都是不可覆盖的 hard conflict。只有源记录所有身份均缺失时才允许使用旧
`current_character_id` 作为最后回退，绝不据此推断目标记录。无法归属的源 point 和 history row 分别写入确定性的本地
quarantine JSONL 并计入 `recoverableErrors`；已存在但无法归属的目标记录禁止被覆盖。history row 的 inspect、plan token
和 apply 必须使用同一个 canonical representation：固定为源列顺序追加 canonical `user_id`，目标额外列保持不变。
正文、向量和原始字段不得进入公共 plan、report、事件或日志。

Settings 在新增、冲突或 `recoverableErrors` 任一非零时都必须调用 apply；只有可隔离错误时不弹覆盖确认，但仍必须完成
quarantine 并展示结果。仅当三者全为零时才可直接显示“没有新数据”。
0.9.x Memory 必须先冻结到 staging；SQLite使用 Backup API，预览和 apply合并读取同一份冻结副本，不能在生成计划后
再次读取活动源目录。首次迁移同样必须在实际 staging前重新检查目标覆盖域；最新覆盖域与已确认列表不一致时确认失效。
若 `data/sakura.lock` 中的 PID 能确认仍存活，inspect必须返回 `LEGACY_SOURCE_ACTIVE`；陈旧、损坏或无法证明存活的锁
不能阻止脏数据救援。该检查是并发写入保险丝，不允许迁移器修改或接管旧锁。

## 验收

自动测试必须覆盖 0.9.6/0.9.8/0.9.9 结构识别、paused Core、非空目标确认、追加后稳定 Timeline ID、segment
分块、截图 marker、逐行隔离错误/未知 role、Memory cursor、TTS Junction、目标祖先 link拒绝、取消、空间不足和每个
commit阶段回滚，包括四棵原子树在 target→backup 和 staging→target 之间硬退出后的完整恢复。还必须覆盖角色包
损坏、TTS复制/后扫描失败及 TTS布局 warning 均能保留 Timeline和Memory，且用户取消不会被可选域吞掉。成功、带
warning完成、失败和取消
均需证明源文件 bytes/mtime/hash不变，且脱敏输出零命中凭据、正文、记忆和绝对源路径。发布前使用
`sakura-release` 的副本分别完成一次真实 Windows 与 macOS arm64 人工迁移，不直接改动原目录。macOS 验收必须覆盖
GPT-SoVITS Miniforge 内部相对符号链接、可执行位、托管 Python/推理配置路径以及迁移后真实 TTS 启动。
长期记忆回归还必须覆盖：无目标模型时把完整 ONNX模型纳入 staging/target、准备失败时仍提交已保全的 Memory并产生
warning、完整目标模型不重复准备，以及模型准备完成或明确跳过后才开始最后的 TTS域。

增量导入还必须覆盖多角色隔离、首次新增、重复导入全部跳过、同 ID不同内容产生冲突、计划失效拒绝、确认后只覆盖冲突项、
quarantine-only apply、坏 JSONL保留有效记录、SQLite WAL快照、Qdrant point/profile/history合并、目标 Memory 读写失败、
corrupt/unknown journal 禁止 Core 重启、`finalizing` 续清理、managed child descendant 回收，以及 Core校验失败时两个原子树
一起回滚。大 JSONL 回归必须禁止整文件读取并锁定稳定 ID、segment 顺序和原始隔离 bytes。

迁移达到 `completed` 后，导航页必须保留可见状态并只显示一个主操作：无需补充设置时显示“完成”并关闭窗口，
仍为 `setup_required` 时显示“继续首次设置”；完成态不得继续提供“返回”。
