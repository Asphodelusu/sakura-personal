# Cursor Task — Observer 关系主动仲裁 P1

## 目标

通过 TDD 完成三项行为：桌面离席门控、连续沉默指数退避、屏幕沉默保全关系机会。目标是提高 Sakura 主动的真实感，同时降低空轮询和 token 消耗。

## 独占文件

- 生产代码：`app/perception/observer.py`、`app/config/relationship_initiative.py`
- 测试：`tests/unit/test_relationship_timer.py`
- 如确有必要，可修改直接相关的 Observer 单测，但须在结果中解释原因。
- 只填写 `docs/agent-handoffs/observer-relationship-arbitration-p1/integration-notes.md` 的 Cursor 段。

## 不要修改

- `app/ui/pet_window.py`
- `app/storage/`、memory curator、聊天历史 schema
- `characters/`、人格卡、关系 guide、提示词文案
- proactive screen capture、UIA、focus settle 状态机
- 配置 UI 与 `data/config/system_config.yaml`

## 行为契约

### A. 桌面 idle 门控

- 关系主动评估前读取现有 `get_idle_seconds()`。
- 桌面 idle 达到可配置阈值时，`_relationship_gate_reason()` 返回独立、可观测的原因（建议 `desktop_idle`），不调用关系 LLM、不说话。
- 默认阈值建议 15 分钟；放在 `RelationshipInitiativeSettings` 中，需有合法范围归一化。
- 用户恢复桌面操作后自然重新进入原有 silence/cooldown 判断；不补发离开期间积攒的内容。
- 不改变屏幕主动已有的 idle trigger 语义。

### B. 连续沉默指数退避

- 关系 LLM 返回 `should_speak=false`、决策失败或空 comment 时，沉默冷却依次为 300、600、1200、1800 秒，之后保持 1800 秒。
- 状态应是 Observer 实例内的明确计数/级别，不写持久化配置。
- 关系主动真正开口后复位。
- `notify_user_spoke()` 后复位。
- 屏幕路径自身的普通沉默不应增加关系沉默级别，除非它明确完成并消费了一次关系动机。
- 保留当前 generation 取消语义；用户在模型执行期间说话导致结果丢弃时，不应被记作关系沉默。

### C. 屏幕沉默保全关系机会

- 同一 tick 同时存在 screen trigger 与 relationship eligible 时，可以继续只执行一次屏幕评估并注入 relationship motive。
- 如果屏幕评估最终真正开口：消费关系机会，记录关系 spoken cooldown，并复位沉默退避。
- 如果屏幕评估 `should_speak=false`、失败、去重跳过或没有形成有效 comment：不写 relationship silent cooldown、不增加关系沉默级别。下一次 tick 可独立进入关系评估。
- 防止同一 tick 紧接着执行第二次关系 LLM；保留现有 `return`。

## TDD 要求

先为每项行为写最小测试并观察预期 RED，再写生产代码。结果报告必须列出 RED 的失败原因，而不是只写“先红后绿”。至少覆盖：

1. idle 未达阈值仍 eligible；达到阈值返回 `desktop_idle`。
2. 四级退避的边界时间。
3. 用户发言与关系开口复位退避。
4. generation 取消不增加退避。
5. 屏幕沉默/失败不消费关系机会。
6. 屏幕真正开口才消费关系机会。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_relationship_timer.py -q
.\.venv\Scripts\python.exe -m pytest tests/unit/test_proactive_focus.py tests/unit/test_proactive_observer.py -q
.\.venv\Scripts\python.exe -m pytest tests/unit tests/ui -q
git diff --check
```

## Git 与交付

- 不 commit，不 push。
- 不触碰当前任务之外的 dirty 文件。
- 在 integration notes 中写：修改文件、逐项 RED/GREEN、全量门禁、关键状态转移说明、剩余风险。
