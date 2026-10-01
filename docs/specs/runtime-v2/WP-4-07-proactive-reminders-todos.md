---
kind: spec
status: normative
audience: maintainer
source_of_truth: self
status_source: ../../plans/runtime-v2/work-packages.md
updated: 2026-10-01
---

# WP-4-07 定时截图与主动请求规范

> 计划中的 [WP-4-07R](WP-4-07R-typed-timeline-adaptive-context.md) accepted 后会替代本规范的请求/历史
> 角色投影；在此之前，下述 user-role 请求和 JSONL 历史仍是当前 accepted 行为。

## 产品行为

- 非个人角色读取现有 `screen_awareness` 设置：启用、截图间隔、主动发言冷却、单次最多截图和截图
  分辨率。读取时 `enabled && screen_context_enabled` 合并为一个开关，保存时两个旧字段写成同一值。
- 非个人角色缺失配置时默认启用、20 分钟截图、10 分钟冷却、最多 6 张、全屏分辨率。范围分别为 1–120 分钟、
  1–120 分钟、1–20 张；分辨率只接受 `fullscreen | 720p | 1080p | 2160p`。
- 带演出约束的个人角色使用 `proactive` 段，不用上面的分钟和攒批字段计时。权威文件是
  `config/system_config.yaml`；只有该文件不存在时才读 `data/config/system_config.yaml`。
  `proactive.enabled` 优先，缺这一键时才回退到 `screen_awareness.enabled`。已保存的未知键和未在设置页露出的
  proactive 字段在保存露出字段时保留，包括继承的隐私名单和隐私段中的未知键。显式空的隐私名单表示清空；
  Core 焦点门控和外壳采集前检查使用同一份个人隐私配置。游戏 OCR 保持硬停用。
- 个人设置页编辑启用、补充间隔、开口冷却、焦点停留和切窗冷却，单位是秒。轮询间隔使用 `poll_interval`，
  不把小数秒折成分钟。内容安静、自适应间隔和评估温度、max_tokens、request_timeout 由个人定时观察消费。
  关系主动仍使用自己的 `relationship_initiative` 设置。已发布会话用当前角色的个人身份；会话尚未发布时，
  用已选角色的演出约束路径判断，不因为还没有会话就退回非个人计时。
- 主窗口在接上焦点观察后按个人 `poll_interval`（默认 5 秒）向 Core 上报前台窗口；非个人角色仍每 5 秒上报。
  Core 以前台应用（进程加窗口句柄）稳定达到 `focus_settle_delay`（默认 15 秒）作为主触发；快切重新计时，同一应用只改标题不计为新的停留。补充间隔只在没有切窗触发时使用，个人角色用 `timer_seconds`（默认 480 秒），非个人角色用分钟间隔。忙碌或亲密续写未结束时不消耗已就绪的触发。切窗冷却默认 60 秒，冷却内的再次切窗先记下，结束后补评。用户开口后的沉默默认 10 秒，开口冷却默认 600 秒。前台是 Sakura 自己或命中隐私名单时不截图。个人模式如果没有焦点观察路由，就保持安静并报告设置不可用，不退回分钟攒批。
- 用户发出明确离开、晚安或暂停观察后，屏幕观察和关系主动都不再开口。下一条真实用户消息先记为回来，再按这句话决定是否再次离开。聊睡觉或疑问不算离开。自动屏幕、关系判定和回复修复不会清除离开。离开不写入配置。
- 个人 `window_switch_enabled=false` 关闭切窗触发，补充计时和空闲触发仍可用。已发布的个人会话不会因尚未生效的角色选择降为上游计时。
- 非个人角色未接上焦点观察时，仍使用 10 秒普通轮询：只有 Core ready，距最近输入或手动发送、距上一张截图都达到截图间隔，
  且聊天、等待动画、打字机/TTS、手动截图或附件均空闲时才截图；忙时跳过，休眠后不补跑。
- Windows 个人观察只捕获本次触发窗口的物理矩形，用 HWND、PID、进程、标题和矩形绑定短期 `captureTicket`；捕获前后、资源生成和附件发布时重新校验。无有效窗口、已最小化、隐藏、隐私名单或目标已变时跳过，不扩大为整屏。跨显示器时按各显示器的交集捕获并拼接，支持负坐标，不重复换算 DPI。手动截图选择器不变。非个人观察仍捕获鼠标所在显示器，按设置等比缩小且不放大，JPEG quality 70。焦点观察在 Core 请求 `capture` 时捕获一张并立即送出。未接上焦点观察时，第一张截图开始冷却；冷却到期后将最新最多 N 张按时间顺序作为一次普通聊天请求发送，然后清空批次。
- 主动请求生成期间主界面保持原有画面，不显示思考占位符或等待动画；完整回复到达后才直接进入现有的
  分段打字、角色表现和 TTS 流程。手动聊天仍显示正常思考状态。
- 手动发送、设置变化、generation 变化、禁用或退出立即清空批次。截图或发送失败不自动重试；清理后
  从当前时刻重新开始普通周期。

## 所有权与资源边界

- WebView 只拥有设置、普通 timer、当前批次数量和 opaque attachment ID，不接收路径、resource token、
  base64 或图像字节。
- Rust `CaptureManager` 使用 `VecDeque` 保存 JPEG bytes，同时受设置张数和 64 MiB 总量限制；超限删除
  最旧帧。原图不提前落盘，也不投影给 WebView。
- 发送时 Rust 才创建 generation 私有临时资源并调用 `screen.attachBatch`。Core 单次消费后立即删除；
  成功、拒绝和中途失败都清理剩余资源。手动多截图使用 `screen.attach` 维护最多 6 项的待发送组；主动
  截图仍通过独立的 `screen.attachBatch` 一次性建立批次，不与手动组混合。
- Core 一个 attachment ID 可对应一至多张图片。自动批次不生成 `VisualObservationJob`，不写
  `visual_observations.jsonl`，也不进入 legacy `screen_awareness_check` 事件系统。

## 请求与历史

固定请求全文为：

> 这是一次由 Sakura 定时截图触发的主动屏幕观察。以下截图按时间顺序展示我最近正在做的事情。请结合最近聊天历史和这些截图，以当前角色的语气自然接话：可以评论变化、接续任务、询问卡点或提供轻量帮助。不要逐张复述，也不要因为时间或久坐机械地提醒休息；如果没有明显变化，就简短说出你能确认的具体内容。

个人角色且屏幕门控生效时，Core 用可沉默的观察说明替换上面这段固定请求，历史只记“刚才留意了一下屏幕状态。”未接上该门控时，历史保存该全文并追加 `[已附加 N 张定时屏幕截图]`。历史不得保存图片、base64、路径或 resource token。
请求继续复用 `chat.send`、现有回复事件、角色表现、TTS 和历史链。

## 接口

- Core：`screen_awareness.settings.get`、`screen_awareness.settings.save`、
  `screen_awareness.focus.advance`、
  `screen.attachBatch { resources: ScreenResourceDescriptor[1..20], observerContext? }`。descriptor 保持原来的八字段；可选 `observerContext` 只接受 `{ process, visibleText, visibleTextSource: "uia" }`，分别最多 260、2000 字符，必须在读取图像之前校验。它只在主动附件存活期间保留，不进入原始聊天历史。
- `screen_awareness.focus.advance` 接收 `{ busy, scope, snapshot?, outcome? }`。`scope` 固定为本次尝试开始时的 Core generation id；不属于当前代次的请求返回 `wait/stale_scope`，不改变 runtime。`snapshot` 含 hwnd、pid、process、title、ownProcess，可带最多 2000 字符的 `visibleText`。响应为 `{ action, trigger, reason }`；个人会话另带布尔 `contentReadAllowed`，不回传标题或正文。`action` 为 `capture`、`hold` 或 `wait`。Shell 先上报薄快照，只有 Core 允许且窗口再次通过隐私校验时才读取 UIA，再上报有界正文；离开、禁用、忙碌或自身窗口均不读取。
- Windows UIA 由 Shell 的单个 MTA 线程串行读取，COM 对象在同线程创建和释放。连接和交易期限均为 200 ms；遍历软期限 200 ms，最多 500 控件、20 层、2000 字符。先检查 password、offscreen 和可见矩形，再取文字；TextPattern 只用可见范围，不读取完整 DocumentRange。原生 provider 可能不遵守期限；调用方按时返回，保持至多一个在途任务，忙碌时静默，迟到、退休或已取消的结果丢弃，退出不无限 join。
- 正文按个人 `content_check_interval`（默认 30 秒）检查，少于 `content_min_chars`（默认 30 字）不计变化。直接比较正文，内容安静期间保留尚未评估的变化；有效感知后才推进基线与安静期。切换窗口、离开和会话退休清除临时正文。UIA 足够时选其最多 1200 字的摘录，标记 `uia`；否则使用 VLM 的 `on_screen_text`，标记 `vlm`。
- `outcome` 只接受 `aborted | failed | privacy | self | submitted`。结算请求只结束已提出的捕获尝试，不再读取前台窗口或提出新捕获。`submitted` 表示请求已送出。非个人角色仍在此时推进补充计时和同应用再看。个人角色的 `submitted` 只消费这一次触发，不提前推进补充计时、同应用再看或内容安静；这些成功计时只在视觉感知通过校验、且焦点与代次仍是送出时的那一个之后写入。失败、取消或焦点已变不写入。`aborted` 保留触发，捕获失败不计为成功评估。离开会解除旧评估的等待，迟到回复不得恢复发言或旧印象。
- 个人定时观察不走普通工具循环。视觉请求只带截图和进程、触发、空闲、短印象这些薄元数据，使用 proactive 的评估温度、max_tokens 和 request_timeout，并关闭思考；不把 UIA 正文放进视觉请求。感知无效或为空时保持沉默，不再调用快模型。快模型不接收图片，上下文是本轮观测包、最多 1200 字可见摘录、最近六轮真实对话、最多三条主动交流、仍有效的短印象，以及角色身份和行为。屏幕文字不能写成用户发言。关系主动仍用自己的温度和长度；观察决策固定 0.5 / 1024，并关闭思考。除既有译文修复外，不再发起第三次普通对话。`should_speak` 为假、未配置、无法解析，或开口文本为空、不是短对白时，都不显示、不播 TTS、不写 ASSISTANT。沉默可以另写一条有界的语义 OBSERVATION。
- 短时屏幕印象属于当前 runtime，不落盘。保存 1200 秒、最多 400 字；普通对话只注入截断到 160 字的投影。离开、会话退休和关闭时清除。此后的真实用户发言会让更早的印象退出后续观察决策，并在决策上下文里写明两边的时间。自动观察不因此去读本机媒体。
- 同窗同标题且视觉摘要完全相同时跳过快模型；切窗和正文变化仍进入决策。直接比较摘要，不计算截图内容哈希。
- 诊断只记 outcome、stage、耗时、trigger、process 及来源状态和字符数。不记窗口标题、UIA 正文、base64 或决策评论；视觉和发言评估请求不写入原始模型 Trace，补译只带待修复对白，不再传正文，普通对话 Trace 不变。游戏 OCR 保持硬停用。
- 可选诊断：在用户根创建 `logs/observer-diagnostics.enabled`，或设置 `SAKURA_OBSERVER_DIAGNOSTICS=1`。Core 追加 `logs/observer-diagnostics.jsonl`。`python tools/observer_diagnostics.py --file <path>` 跟随该文件。默认不打开窗口，也不写入标题或画面正文。
- `screen.attachBatch` 返回 `{ attached: true, attachmentId, count }`。
- Tauri：`settings_screen_awareness_get`、`settings_screen_awareness_save`、
  `capture_screen_awareness_frame`、`observer_focus_advance`、`attach_screen_awareness_batch`、`clear_screen_awareness_batch`。
- 个人 `observer_focus_advance` 的 capture 响应另带随机 `captureTicket`。个人捕获请求必须携带 `{ resolution: "fullscreen", batchLimit: 1, scope, captureTicket }`，附加批次携带 `{ scope, captureTicket }`；不能把旧请求改绑到最新窗口。WebView 仅传递凭证，不接收窗口正文。
- 设置保存成功后发布一次 `sakura://screen-awareness-settings`。事件载荷就是 `settings` 对象。非个人角色保持
  `enabled`、`checkIntervalMinutes`、`cooldownMinutes`、`batchLimit`、`resolution`。个人角色改为
  `enabled`、`timerSeconds`、`cooldownSeconds`、`focusSettleDelay`、`windowSwitchCooldown`、`pollIntervalSeconds`。
  当前身份与对象不一致时拒绝保存，不写入另一段。事件失败不重试；持久化值在下次启动生效。
- 主动屏幕感知设置归入“交互”页，不再单列“隐私”导航；设置 capability 在 `interaction` section
  暴露 `privacy.screen_awareness = available`。不修改既有配置键、`chat.send`、聊天事件、TTS 或手动截图公开结构。

## 失败与验收

- 权限拒绝、无显示器、编码、Core、Provider 或发送失败都必须显式结束本轮并清理资源，不得破坏普通聊天。
- 自动门覆盖设置兼容与原子保存、批量 JPEG 单次消费、历史隐私、分辨率和不放大、最新 N 张、64 MiB、
  generation 清理、前端假时钟、忙时跳过、休眠不补跑和发送失败释放。
- 扩展既有 `journey-screen-capture`，不新增 Harness profile。
- WP-4-07 只有自动门、Windows/macOS/Linux 实机行为和负责人验收全部通过后才能 accepted。

2026-08-25 的自动验证与负责人验收记录分别见
[`WP-4-07-AUTOMATED-VALIDATION.md`](../../records/audits/WP-4-07-AUTOMATED-VALIDATION.md) 和
[`WP-4-07-OWNER-ACCEPTANCE.md`](../../records/audits/WP-4-07-OWNER-ACCEPTANCE.md)。当前执行状态以
[`work-packages.md`](../../plans/runtime-v2/work-packages.md) 为准。

## 非目标

CAP-017 提醒与待办不属于本 WP，保持未排期。本 WP 不实现 Scheduler、提醒、待办、落盘视觉档案、磁盘批次、
额外 Worker、自动恢复、自愈、任务图、lease、outbox、ack、补跑或通用主动事件协议，也不为这些能力预留接口。
