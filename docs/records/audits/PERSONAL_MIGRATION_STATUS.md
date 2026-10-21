---
kind: record
status: recorded
audience: maintainer
source_of_truth: self
updated: 2026-10-21
---

# 个人版迁移对账

## 范围与证据边界

本记录是 2026-10-21 的源码和部署标识核对，不是全功能验收声明。旧版基线为 dev5 的 e0e31cdb；旧工作区另有未提交个人改动，不能只比较两个 Git tag 就宣称迁移完整。新候选基线为 44417b0a。

本轮未启动应用、模型或 TTS，未读取角色正文、凭据或记忆库，未重新进行真人验收。旧对账中的体验结论不能代替当前运行证据。

## 仓库、安装与历史

| 对象 | 核对结果 |
|---|---|
| 远端 main | 6381488e |
| 远端 dev6 | 44417b0a |
| 本机日用安装标识 | 58d2cc46；版本号相同不代表代码一致 |
| dev5 | e0e31cdb；其 166 个 Asphodelusu 提交不在当前 main 的祖先链中 |
| 旧 main 备份 | archive/main-before-runtime-v2-20261008，6edeefc6；是当时 main 的快照，不等于 dev5 最新状态 |

新候选从上游 d24a3e73 起步，e5e8b878、9fa8f16e 等提交重新移植个人能力，没有合入 dev5 的完整历史。当前 main 的 README 与 d24a3e73 相同，直到本轮修正文档前仍显示上游 1.1.0 与上游 Release 链接。

本次核对 Git 作者计数：旧 main 备份中的 Asphodelusu 为 204，当前 main 为 46，dev6 为 52。GitHub contributors 接口返回 Asphodelusu 46，与默认 main 相符。未发现新迁移提交被改署名为原作者；数量下降来自可达历史变化，不能据此推断功能被删除，也不能据此证明功能已迁移。

不要为恢复统计直接覆盖新架构或伪造作者。连接旧历史、恢复文档身份、补齐运行功能是三个不同任务，本轮没有修改历史拓扑。

## 已核对的个人调用入口

“有实现入口”只说明代码有消费者，不等于全部旧行为等价或真人验收完成。

| 能力 | 新架构入口或证据 | 当前边界 |
|---|---|---|
| 角色卡与个人提示词 | app/config/character_loader.py 调用 app/llm/prompts/personal_persona.py | 个人组装入口保留；本轮不复读私有文本，不宣称逐字体验等价 |
| 关系驱动与分离时间 | app/agent/runtime.py 的 configure_relationship_drive；app/core/relational_drive.py 与 app/storage/relational_drive.py | 持久化与运行入口存在；频率、强度仍需真人判断 |
| 聚焦 Observer | runtime.py 创建 FocusObserver，并调用 configure、advance、settle_attempt；app/agent/personal_observer.py | 已接个人焦点链路；不能用上游“定期截图”概括个人行为 |
| 独白 | runtime.py 创建 InnerThoughtCoordinator；inner_thought_runtime.py 管理跨轮隔离与结果 | 调用入口已接；关闭旁路 thinking 的后续修复还未部署 |
| 个人记忆 | plugins/builtin/sakura_mem0/plugin.py 的 PersonalDailyPlugin → SakuraMem0Plugin(personal_daily=True) → PersonalRecallBoundary | 存在个人入口；不应被同 ID 的上游默认入口覆盖。完整旧记忆行为仍需逐项对账 |
| 模型设置与重启 | 58d2cc46 的模型设置/连续性修复，adfa2ea9 的桌宠与托盘重启入口 | 提交已包含于日用标识；本轮未重新操作设置或重启 |
| 逐段译文与语音 | 个人连续性契约要求修复请求不重复召回、逐段译文闸门继续生效 | 旧流式进度与异步字幕实现不等价；既有声音验收不代表所有失败路径覆盖 |

## 已实现但尚未部署

- 6381488e：个人记忆回顾覆盖修复已在 main，但日用仍是 58d2cc46。
- 92a9f154：独白、检索改写和关系主动判定恢复 thinking: disabled；已在 dev6，未进入日用。
- 22d27eeb：Observer 成功轮询日志降噪，query_plan_ms 可见；已在 dev6，未进入日用。
- 后续至 44417b0a 为测试修正；Windows 短路径临时目录问题已复现并修正，对应 CI 38581710014 成功。

本机 ASR、Genie 与依赖发布测试出现目录 os.replace 的 WinError 5，干扰来源仍未查明。系统取证已由用户暂缓。CI 成功不等于该本地问题已解决；本轮不修改重试策略、不部署增量。

## 本轮确认的运行缺口

| 能力 | 旧版消费者 | 新版实际情况 | 判定 |
|---|---|---|---|
| 普通聊天快速分流 | app/agent/turn_routing.py 的 _fast_plan → client_key=chat_fast；旧 runtime 分类选路 | app/agent/runtime.py:196 的 _client_for_messages 无图始终主 client，tool loop 在该函数选模；chat_fast 仅有旁路消费者 | 缺失，槽位已配置不代表普通聊天生效 |
| 亲密轻量召回 | app/agent/tool_loop.py 中 intimacy_focus → recall(light_mode=True) | 新 runtime context mode 仅 normal/screen_awareness；插件 context 统一调用 _recall.recall，未传亲密轻量语义 | 未完整适配 |
| 本地快速接话 | app/ui/pet_window.py 创建 BackchannelController 并发起 schedule | 新版搜索仅见配置/资源相关残留，未找到生产 Controller、schedule 或显示消费者 | 缺失，README 不再宣传已实现 |
| 到期提醒 | 旧窗口轮询 ReminderStore 并投递提醒 | app/agent/reminders.py 保留 due_reminders/mark_completed 定义，但未找到生产调用；runtime 的 reminder_due 分支不足以构成到期触发闭环 | 缺失，存储存在不等于可用 |
| 个人反思 | 旧窗口定时反思 | plugins/builtin/sakura_mem0/personal_runtime.py 创建 ReflectionScheduler 并调用 maybe_start，关闭时 join | 有运行消费者；触发节奏与旧版是否等价仍需核对 |

补充宿主证据：app/core_host/tools.py 的实际 builtin registry 仅注册 get_current_time；app/core_host/real_chat.py 的外部 event 校验仅接受 update_available，旧 reminder_due 分支并未构成新版可用入口。

反思与整理也不是全等价：personal_runtime.py 的 note_timeline_changed 才检查 reflection.maybe_start；整理按 Timeline 证据 turn 阈值触发，未恢复旧窗口每分钟 timer/idle 检查。核心实现保留，但空闲时的触发节奏属于部分迁移。

以上是源码证据，不是本轮真人测试结果。普通聊天路由、亲密召回、接话和提醒需要补实现，不能仅更新文档就标记迁移完成。

## 后续对账顺序

1. 普通 chat_fast 自动路由与亲密轻量召回：区分槽位可配置和实际选模/召回生效。
2. 渐进显示、字幕与首段 TTS：对比旧版输入到首次可见/可听反馈的实际时序，保留译文完整性边界。
3. 提醒、接话、反思与整理：核对工具注册、触发器、消费者和终态，不能凭模块存在判定完成。
4. 每个缺口补最小行为验证，再决定实现；运行时验收与源码检查分开记录。

这些项尚未全部完成，不发布“完整迁移”结论。

## 后续修复：亲密轻量召回

候选新增 `mode=intimacy` 从 Core 到插件的传递，并恢复个人初检 3 条、语义入选 2 条、到期约定按原总上限合并。测试使用合成模型和临时记忆，覆盖退出、缺 guide、屏幕观察优先与默认普通召回。该补丁未部署，以上 44417b0a 审计表仍作为历史快照；普通快速分流、接话、提醒与空闲触发差异没有因此解决。

## 当前用户取舍与反思关闭修复

用户已暂缓本地快速接话、普通聊天自动分流和定时提醒；保留 chat_fast 配置入口。这些能力不作为当前个人日用迁移的必做项，优先补齐反思与记忆整理。

候选修复反思关闭后的新任务准入、迟到响应取消与存储关闭顺序；合成回归覆盖关闭后无请求、迟到响应不写入、普通反思未完成时不消耗间隔，以及元反思取消时保留已完成的普通反思状态。正在执行的 HTTP 请求仍需返回或超时，不能把取消视为即时网络中断。反思、整理和模型维护已共用任务准入；旧版每分钟空闲检查、整理触发策略和聊天/TTS 忙碌状态仍待接入，尚未部署日用。
