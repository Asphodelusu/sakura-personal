# Turn Orchestration

Sakura 在每轮用户对话进入 Agent 工具循环前，通过 **Turn Orchestrator** 做两层决策：是否 upfront 召回记忆（Recall Gate），以及用哪条模型路径应答（Turn Router）。

## 架构

```
用户输入
   │
   ▼
Recall Gate ── skip / defer / recall
   │
   ▼
Turn Router ── fast / standard (+ vision)
   │
   ▼
_run_tool_loop → complete_with_tools
```

### Recall Gate

| 决策 | 行为 |
|------|------|
| `skip` | 不调用 `memory_recall.recall()`，`memory_status=skipped` |
| `defer` | 不 upfront recall，模型可通过 `memory_search` 按需查询，`memory_status=deferred` |
| `recall` | 调用 `memory_recall.recall()`，与历史默认行为一致 |

默认从「每轮都 recall」改为 **defer**；仅明确记忆/历史意图时 recall，简单寒暄 skip。

### Turn Router

| 层级 | 客户端 | 生成参数 |
|------|--------|----------|
| `fast` | `chat_fast` 槽（未配置则回退 `chat`） | `thinking: disabled`（API 支持时） |
| `standard` | 主 `chat` 客户端 | 沿用用户配置的对话参数 |
| `vision` | `vision_chat` 槽（含图时优先） | 强制 `standard` |

路由漏斗（`resolve_turn_plan`）：

1. **硬门禁**：主动模式、含图、未配置 `chat_fast` → `standard`
2. **确定性规则**：记忆召回/写入、长文、连发 user 消息、工具任务关键词 → `standard`
3. **需接话短句**（中文语境）：在场探询（`在吗` 等）、忙闲/社交开场、短问句（`…吗/呢`）、求判断 → `standard`
4. **简单问候**（极窄白名单）：单向寒暄 / 纯确认 → `fast`
5. 其余 → `standard`（默认 pro）
6. （可选）轻量分类器（`turn_classifier`，默认关闭）：`simple` → `fast`，`deep` → `standard`

### fast 白名单（`simple_greeting`）

仅以下类型走 `chat_fast`：

- 单向寒暄：`你好`、`早安`、`晚安`、`hi` 等
- 纯确认：`好的`、`嗯`、`收到`、`明白` 等

**不走 fast** 的典型中文场景：

| 类型 | 示例 | `decided_by` |
|------|------|----------------|
| 在场探询 | `在吗`、`在不在`、`还在吗` | `presence_probe` |
| 重复在场探询 | 会话内第 2 次及以上 `在吗` | `repeated_presence_probe` |
| 忙闲探询 | `忙吗`、`有空吗`、`方便吗` | `availability_probe` |
| 社交开场 | `在干嘛`、`聊聊天` | `social_opening` |
| 短问句 | `猫可爱吗`、`你呢` | `short_question` |
| 求判断 | `你觉得呢`、`行不行` | `judgment_seek` |

`在吗` 一类语句在中文里往往是「有话要说」的前奏；重复出现更容易惹烦，需要结合会话语境接话，因此** deliberately 不进 fast**。

同一用户 turn 内多 step **复用冻结的 `TurnState`**（step 0 决策，后续 step 不变）。

## 配置

### `system_config.yaml` → `turn_routing`

```yaml
turn_routing:
  enabled: true
  classifier_enabled: false   # 推荐：规则 fast + 默认 pro，不前置分类
  backchannel_orchestration_enabled: false   # 与 Turn Router 联动调度接话；默认关
  simple_greeting_max_chars: 12
  classifier_timeout_seconds: 1
```

### `api.yaml` → `model_slots.chat_fast`

可选快速聊天模型槽；留空时轻量轮次仍走主 `chat` 模型，但路由 tier 会降为 `standard`。

## 观测字段

调试元数据（聊天记录 `_debug.turn_routing`）与运行日志 `Turn 路由决策` 包含：

- `recall_decision`: `skip` / `defer` / `recall`
- `tier`: `fast` / `standard`
- `modality`: `text` / `vision`
- `client_key`: `chat` / `chat_fast` / `vision`
- `decided_by`: 决策来源（如 `simple_greeting`、`presence_probe`、`memory_recall`、`default`）

## 相关源码

- `app/agent/turn_routing.py` — 纯函数路由
- `app/agent/turn_classifier.py` — LLM 深度分类器
- `app/agent/runtime.py` — `_run_tool_loop` 集成点

## Phase 2 — Backchannel 编排

等待期接话（Backchannel）与 Turn Router 联动：中等/需回忆的轮次先给过渡接话，简单 fast 轮次不触发。

### 流程

```
用户发送消息
   │
   ▼
resolve_backchannel_schedule（预检，不调 LLM 分类器）
   │
   ├─ should_schedule=False → 跳过接话（fast / recall skip）
   └─ should_schedule=True, phase=long_wait → backchannel.schedule(text, phase=...)
          │
          ▼
     延迟 → 分类 → resolver 优先匹配 long_wait 相位条目
```

### 调度规则

`backchannel_orchestration_enabled=True` 且 `turn_routing.enabled=True` 时：

| 场景 | 接话 |
|------|------|
| 简单问候（`_is_simple_greeting`） | 不调度 |
| `recall_decision=skip` | 不调度 |
| `recall` / `defer` 且预期 `standard` tier | 调度，`phase=long_wait` |
| 含图（vision） | 调度，`phase=long_wait`（保守） |
| 未决（无分类器结果） | 按 `standard` → 调度 `long_wait` |

`backchannel_orchestration_enabled=False`（默认）时不走 Turn 联动调度；`pet_window` 回退为旧行为（`backchannel.enabled` 时仍会 `schedule(text)`）。

接话总开关在 `system_config.yaml` → `backchannel.enabled`，**代码默认亦为 `false`**。

### 配置

`system_config.yaml` → `turn_routing` 新增：

```yaml
turn_routing:
  backchannel_orchestration_enabled: false
```

### 相关源码

- `app/agent/turn_routing.py` — `resolve_backchannel_schedule`
- `app/backchannel/controller.py` — `schedule(text, phase=...)`
- `app/ui/pet_window.py` — 发送消息处集成
- `assets/backchannels/sakura/manifest.json` — `long_wait` 相位模板
