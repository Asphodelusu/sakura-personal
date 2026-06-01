# ARCHITECTURE.md — Sakura 重构后架构说明

> 基于 ARCHITECTURE_AUDIT.md 阶段 0 的体检结果，重构后的模块边界。

---

## 架构概览

```
main.py (入口)
  |
  AppBuilder (声明式装配)
    ├── SettingsService (配置读写)
    ├── CharacterRegistry (角色包)
    ├── OpenAICompatibleClient (API)
    ├── ToolRegistry (工具注册表)
    ├── MCPToolProvider (MCP 工具)
    ├── PluginManager (插件管理)
    ├── TTSProvider (语音)
    └── MemoryStore / ReminderStore (存储)
  |
  AppContext (核心依赖容器)
  |
  PetWindow (主窗口)
    ├── ChatPipeline (对话编排)
    │     └── AgentRuntime (Agent 决策)
    │           ├── ToolLoopRunner (工具循环)
    │           ├── ToolRoutingPolicy (路由策略)
    │           ├── PendingActionFlow (确认流程)
    │           └── EventFlow (主动事件)
    ├── SubtitleController (字幕)
    ├── PortraitController (立绘)
    └── PlaybackController (语音播放)
```

---

## 模块边界

### `app/core/` — 应用核心

| 模块 | 职责 |
|------|------|
| `bootstrap/app_builder.py` | AppBuilder 声明式装配器 |
| `bootstrap/service_container.py` | ServiceContainer 轻量服务定位器 |
| `bootstrap/lifecycle.py` | LifecycleManager 生命周期管理 |
| `chat_pipeline.py` | ChatPipeline 对话编排 |
| `chat_worker.py` | Qt 线程 Worker (无业务逻辑) |
| `app_context.py` | AppContext 依赖容器 |
| `debug_log.py` | 调试日志 (自动脱敏) |

### `app/agent/` — Agent 决策层

| 模块 | 职责 |
|------|------|
| `runtime.py` | AgentRuntime (Facade) |
| `runtime_limits.py` | 运行时限制常量 |
| `tools/registry.py` | ToolRegistry 统一工具注册表 |
| `tools/permission_policy.py` | ToolPermissionPolicy 权限策略 |
| `tools/builtin/provider.py` | BuiltinToolProvider 内置工具 |
| `mcp/` | MCP 工具 Provider |
| `screen_policy.py` / `screen_tools.py` | 屏幕观察策略 |
| `tool_policy.py` | 工具路由策略 |

### `app/plugins/` — 插件系统

| 模块 | 职责 |
|------|------|
| `models.py` | 数据模型 (Manifest/Spec/Contribution) |
| `discovery.py` | PluginDiscovery 插件发现 |
| `capabilities.py` | PluginCapabilityRegistry 能力收集 |
| `manager.py` | PluginManager 插件管理 |
| `adapters.py` | SDK 兼容适配 |

### `app/config/` — 配置管理

| 模块 | 职责 |
|------|------|
| `models.py` | 配置数据模型 |
| `defaults.py` | 默认值 |
| `settings_service.py` | YAML 配置读写 |
| `migrations.py` | 配置迁移 (`.env` → YAML) |

### `app/storage/` — 存储层

| 模块 | 职责 |
|------|------|
| `paths.py` | StoragePaths 统一路径 |
| `chat_history.py` | ChatHistoryStore |
| `visual_observation.py` | VisualObservationStore |

### `sdk/` — Shinsekai 兼容层

| 模块 | 职责 |
|------|------|
| `plugin.py` | PluginBase 基类 |
| `register.py` | PluginCapabilityRegistry |
| `types.py` | 贡献点类型 |
| `tool_registry.py` | 已废弃 (DeprecationWarning) |

---

## 关键设计决策

1. **PluginDiscovery ≠ PluginCapabilityRegistry** — 发现和能力注册分离
2. **ToolPermissionPolicy** — 集中管理工具确认策略，而非散落在 Runtime
3. **Provider 模式** — 工具来源各自实现 Provider，由 ToolRegistry 统一注册
4. **失败隔离** — 单插件加载失败不影响其他插件和核心启动
5. **StoragePaths** — 所有 data/ 路径由统一模块生成
6. **敏感信息脱敏** — `debug_log.py` 的 `_SENSITIVE_KEY_MARKERS` 自动脱敏 api_key 等字段

---

## 数据流

```
用户输入
  → PetWindow.send_message()
  → ChatWorker (QThread)
  → ChatPipeline.run_user_message()
  → AgentRuntime.handle_user_message()
  → _run_tool_loop()
      → ToolRoutingPolicy 过滤工具
      → API Client 获取工具调用意图
      → ToolRegistry.execute() / prepare_or_execute()
      → ToolPermissionPolicy 判断确认策略
      → 工具结果 → 模型 → 最终回复
  → ChatReply (分段双语)
  → PetWindow (字幕/立绘/语音)
```
