# 更新日志

## 0.9.9-personal.5 — 2026-08-28

### 主动感知与关系互动

- Observer 与关系主动发言改为独立仲裁，屏幕路径沉默或失败不再误消耗关系机会
- 关系主动发言加入桌面空闲门控、分级退避和效果账本，降低无效模型调用与重复打扰
- 决策模型偶发返回自然日语而非 JSON 时，可在安全边界内采用短对白，减少无意义失败
- 清理旧 `proactive_care` UI 兼容层，统一由现行 `screen_awareness` 链路负责

### 对话、翻译与演出

- 增加异步翻译侧路；主回复已有可采用的日语分段时不再为了缺少中文而二次调用主模型
- 动作、晚到翻译与 TTS 播放采用顺序保护和阅读停留，避免字幕过早跳到下一句
- 调整角色表达与可见动作引导，减少公式化回应，同时保留角色自身判断
- 历史记录持久化回复来源，主动发言、手机端与普通聊天可保持正确通道语境

### 设置页与稳定性

- Tauri Settings / Studio 的异步 RPC 回复统一回到进程对象所属线程写入，避免跨线程操作 `QProcess`
- 记忆搜索等待输入停顿后执行，兼容中文输入法预输入，并阻止旧请求覆盖新结果
- 强化供应商探测、交互连续性与应用关闭时的外部服务回收
- pytest 运行时日志与生产日志隔离，避免测试污染真实运行记录

---

## 0.9.9-personal.3 — 2026-07-22

> Personal Edition 当时版本。对照上游 [Rvosy/Sakura](https://github.com/Rvosy/Sakura) `main`（`VERSION=0.9.9-dev`）的**现行代码能力**整理；不按 commit 计数估算进度。

### 主动屏幕感知（相对上游是不同实现）

上游仍以 `app/agent/screen_awareness.py` 的「定时截图攒批次 → 主模型找话题」策略为主。
本仓库运行时为 `app/perception/ProactiveObserver`：

- 截图 + UIA 文字 → Vision 槽内心独白 → Chat 槽决定是否开口（两阶段）
- 自适应巡视间隔、切窗 settle / 冷却、dHash 画面去重、隐私进程/标题黑名单
- 离开模式（away）、评估理由中文日志；未发言评估不再刷持久化历史
- VLM 独白要求「先写看到了什么，再写想到了什么」，降低下游台词失锚

### 记忆与拟真

- 召回软因子由连乘改为加权效应量，减轻极端惩罚叠加
- 二阶元反思（对反思再总结）
- 持久化实体索引（`entity_index.db`）替代二次语义搜索扩链
- 连续 valence-arousal 情绪亲和（`persona_state`）
- 记忆访问跟踪由 JSON 全量重写改为 SQLite 批量 UPSERT（`access_tracker.db`）

### 存储

- 聊天历史改为 SQLite（WAL）；公共 API 不变，首次启动自动从旧 JSONL 迁移

### 文档与版本

- 根目录 `README.md` 按与上游的事实差异重写对照说明
- `docs/TECHNICAL_README.md`、`docs/context-token-budget.md` 跟上现行模块与配置键

### 此前已进入 .3 轨迹的改动（保留）

- 自适应感知间隔、离开检测
- `api_profiles` + `model_slots` 为唯一真相；移除 `dual_endpoint` / Qt 设置主路径
- ProactiveObserver 绑定 `vision_chat` 槽；设置保存后重启观察器

---

## 0.9.9-personal.2 — 2026-07-15

> 体感与文档整理中间版本（VERSION 曾停留于此号）。

### 互动与表达

- 精简系统侧交互约束，减少对角色自然表达的干预
- 微调角色表达细腻度与语境适应性

### 文档

- Personal Edition README / API_CONFIG 说明

---

## 0.9.9-personal.1 — 2026-07-13

> 基于 [Rvosy/Sakura](https://github.com/Rvosy/Sakura) 0.9.9-dev 的个人适配分支。

### 0.9.9 基础设施（与上游同代能力对齐）

- Tauri 设置页与多 API Profile / model_slots 配置层
- `slot_clients`：聊天 RoutingLlmClient + 视觉/记忆按 model_slots 分流
- AgentRuntime 含图消息走独立 `vision_api_client`
- 首次启动改为 Tauri onboarding（需已构建 `sakura-settings`）
- Tauri Studio + `character_studio` 后端；`start_studio.bat` 与设置页共用同一宿主
- `sakura_mobile` 手机网页端插件骨架
- 设置保存后重建 LLM 客户端（聊天/视觉/记忆整理）

### 保留的个人向增强（上游 `main` 树中仍无对应模块）

- STT 语音输入、记忆反思、ProactiveObserver 主动看屏、亲密模式节奏、本地 LLM 路由等

---

## 0.9.8-personal.1 — 2026-07-11

> 基于 [Rvosy/Sakura](https://github.com/Rvosy/Sakura) v0.9.8 的个人修改版。

### 主动屏幕感知增强

- 重构 ProactiveObserver，调整默认触发策略：定时器 8 分钟、窗口切换冷却、用户静默门限 10 秒

### 语音输入（STT）

- 集成 SenseVoice 模型，支持 Alt+T 快捷键语音输入
- 带 VAD 能量检测和手动停止，UI 显示麦克风音量状态

### 记忆系统

- 记忆反思（memory reflection）：空闲时自动回顾记忆
- 嵌入模型升级到 BAAI/bge-base-zh-v1.5（768d），自动迁移旧向量数据
- 记忆召回加入时间衰减
- 定性关系笔记替代数值好感度
- curator 提示词：推荐简体中文；默认不称用户为「主人」

### 中文使用优化

- 双语气泡等相关体验调整
