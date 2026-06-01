# ARCHITECTURE_AUDIT.md — Sakura 仓库体检报告

> 生成日期: 2026-06-02
> 范围: D:/Project/sakura 全仓库

---

## 1. 当前架构图

`
┌─────────────────────────────────────────────────────┐
│                    main.py (入口)                     │
│  QApplication -> build_initial_app_context()         │
│  -> PetWindow(context) -> deferred startup           │
└────────────┬───────────────────────────────┬────────┘
             │                               │
    ┌────────▼────────┐           ┌──────────▼──────────┐
    │  bootstrap.py   │           │   PetWindow (2630)   │
    │  (336行,        │           │   主窗口/托盘/      │
    │   全能装配脚本)  │           │   字幕/立绘/        │
    │                 │           │   工具确认/主动关怀  │
    │  AppContext     │           │   /历史回看/截图     │
    └────────┬────────┘           └──────────┬──────────┘
             │                               │
    ┌────────▼───────────────────────────────────────┐
    │              AgentRuntime (2081行)              │
    │  . handle_user_message / confirmed / cancelled │
    │  . _run_tool_loop (多步工具循环)                │
    │  . 浏览器路由策略 (browser page / visible)      │
    │  . Windows 工具阻断策略                         │
    │  . 屏幕观察策略                                 │
    │  . PendingAction 确认/取消后续跑                │
    │  . 主动事件处理 (reminder_due/proactive_check)  │
    │  . 提示词拼接 (工具策略/最终回复/事件)          │
    │  . vision unsupported fallback                  │
    └────────┬───────────────────────────────────────┘
             │
    ┌────────▼────────┐  ┌──────────────┐  ┌──────────────┐
    │  ToolRegistry    │  │  MemoryStore  │  │ ReminderStore │
    │  (435行)         │  │  (522行)      │  │               │
    │  + MCP Provider  │  │  + Curator    │  │               │
    └─────────────────┘  └──────────────┘  └──────────────┘

          ┌────────────────────────────────────┐
          │         SDK (Shinsekai兼容层)       │
          │  . PluginBase                      │
          │  . PluginCapabilityRegistry        │
          │  . sdk/tool_registry.py (全局变量)  │
          │  . tools_tab 贡献点                │
          └────────────────────────────────────┘
`

### 真实目录树（关键模块）

`
app/
  agent/
    actions.py                     # Agent动作/事件/数据结构
    builtin_tools.py               # 内置工具 + TodoStore
    desktop_tools.py               # 桌面工具
    memory.py                      # MemoryStore (+ embedding)
    memory_curator.py              # 记忆整理
    memory_curation_worker.py      # 后台Worker
    reminders.py                   # ReminderStore
    runtime.py                     # * AgentRuntime (2081行)
    screen_policy.py               # 屏幕观察策略
    screen_tools.py                # 屏幕观察工具
    tool_policy.py                 # 工具路由常量
    tool_registry.py               # * ToolRegistry (435行)
    mcp/
      bridge.py                    # MCP stdio桥接
      config.py                    # MCP YAML配置解析
      provider.py                  # MCP ToolProvider
      settings.py                  # MCP运行时开关
      web_search_server.py         # 内置Web搜索MCP Server
  core/
    app_context.py                 # AppContext / CoreServices / ...
    bootstrap.py                   # * 全能装配脚本 (336行)
    chat_pipeline.py               # ChatPipeline (编排层)
    extensions.py                  # ExtensionRegistry (几乎未用)
    plugin_manager.py              # SakuraPluginManager
  config/
    character_loader.py            # CharacterRegistry + 人格卡
    settings_service.py            # YAML配置读写
    yaml_config.py                 # YAML通用工具
  llm/
    api_client.py                  # OpenAICompatibleClient
    chat_reply.py                  # ChatReply解析
    context_trimming.py            # 上下文修剪
    prompt_templates.py            # 提示词模板
    prompts/                       # 提示词块/渲染
  storage/
    chat_history.py                # ChatHistoryStore (JSONL)
    visual_observation.py          # VisualObservationStore (JSONL)
  ui/
    pet_window.py                  # * PetWindow (2630行)
    settings_dialog.py             # * (793行)
    history_window.py              # 历史回看窗口
    portrait_controller.py         # 立绘控制器
    subtitle_controller.py         # 字幕控制器
    tool_confirmation_panel.py     # 工具确认面板
    tray_menu.py                   # 托盘菜单
    ... (其余UI组件)
  voice/
    tts.py                         # TTS (981行)
    playback_controller.py         # 语音播放控制
  --- 根目录残留 ---
  chat_worker.py                   # 应在 core/ 或 agent/
  screen_observation.py            # 应在 agent/ 或 storage/
  proactive_care.py                # 应在 agent/
  portrait_utils.py                # 应在 ui/
  debug_log.py                     # 应在 core/

sdk/
  plugin.py                        # PluginBase
  register.py                      # PluginCapabilityRegistry
  plugin_host_context.py           # PluginHostContext
  tool_registry.py                 # * 全局变量 _REGISTERED_TOOLS
  types.py                         # ToolsTabContribution

data/
  config/
    api.yaml                       # * 含明文api_key
    characters.yaml                # 当前角色ID
    mcp.yaml                       # MCP配置
    plugins.yaml                   # 插件入口
    system_config.yaml             # 系统配置
  chat_history/                    # JSONL聊天记录
  memory/                          # 记忆 + qdrant
  visual_observations/             # JSONL视觉观察

plugins/
  playwright_browser/              # 唯一本地插件
    plugin.py / browser.py / llm_tool.py / settings_tab.py

tests/
  unit/                            # 12个测试文件
  integration/                     # 4个测试文件
  ui/                              # 2个测试文件
`
---

## 2. 问题清单

### 2.1 文档漂移 (P0)

| 问题 | 严重度 | 详情 |
|------|--------|------|
| README项目结构与实际不符 | **P0** | README仍描述旧扁平结构 (app/api_client.py, app/env_config.py, app/pet_window.py)。实际已迁移到 app/llm/, app/agent/, app/core/, app/ui/, app/config/, app/storage/, app/voice/ |
| README仍描述.env配置 | **P0** | 启动流程中说ApiSettings.load()从.env加载。实际已迁移到data/config/*.yaml |
| config.example.env不存在 | **P0** | README指导创建 config.example.env，但该文件已删除。无迁移说明 |
| app/env_config.py不存在 | **P0** | README中列出此文件，实际已被app/config/替代 |
| README启动流程图过时 | **P0** | Mermaid图中仍写 .env 启动配置 |
| README配置项表仍用.env命名 | **P0** | 表头BASE_URL/API_KEY是旧.env key，实际YAML中为llm.base_url/llm.api_key |

### 2.2 超大文件需拆分 (P1)

| 文件 | 行数 | 职责过载 |
|------|------|----------|
| app/ui/pet_window.py | 2630 | 窗口+托盘+字幕+立绘+工具确认+主动关怀+屏幕观察+历史回看+回复历史+记忆整理触发+提醒检查+事件处理+手动截图+启动初始化+TTS |
| app/agent/runtime.py | 2081 | handle_*入口+工具循环+浏览器/Win路由+屏幕策略+pending流程+事件流程+提示词拼接+vision fallback+工具结果处理+图片提取 |
| app/voice/tts.py | 981 | GPT-SoVITS TTS完整实现+Null实现 |
| app/ui/settings_dialog.py | 793 | 所有设置tab挤在同一文件(LLM/TTS/角色/主动关怀/MCP/工具/调试) |

### 2.3 重复/重叠模块 (P1)

| 重复域 | 涉及文件 | 问题 |
|--------|----------|------|
| 工具注册 | app/agent/tool_registry.py + sdk/tool_registry.py + app/core/extensions.py | 三个注册机制并行: ToolRegistry(实例)、sdk全局_REGISTERED_TOOLS(module-level)、ExtensionRegistry(协议但几乎未用) |
| 插件能力收集 | sdk/register.py(PluginCapabilityRegistry) + app/core/plugin_manager.py(SakuraPluginManager) | CapabilityRegistry只收集tools_tabs，其他贡献点无定义 |
| 屏幕观察 | app/agent/screen_tools.py + app/agent/screen_policy.py + app/screen_observation.py + PetWindow内_capture_*方法 | 屏幕截图/观察能力分散在4个位置 |
| 工具确认策略 | app/agent/tool_registry.py(free_access_enabled) + app/agent/tool_policy.py(路由常量) + app/agent/runtime.py(内联路由逻辑) | 确认策略/风险/路由散落 |

### 2.4 根目录残留文件 (P1)

| 文件 | 当前位置 | 应归属 |
|------|----------|--------|
| app/chat_worker.py | app/ | app/core/ 或 app/agent/ |
| app/screen_observation.py | app/ | app/agent/ 或 app/storage/ |
| app/proactive_care.py | app/ | app/agent/ |
| app/portrait_utils.py | app/ | app/ui/ |
| app/debug_log.py | app/ | app/core/ |

### 2.5 print()调试代码 (P2)

裸print()散落在共21处，应统一走debug_log():
- app/agent/runtime.py (2处: L499, L594)
- app/agent/mcp/provider.py (5处: L75, L84, L144, L155, L206)
- app/core/bootstrap.py (3处: L214, L236, L336)
- app/llm/api_client.py (1处: L351)
- app/ui/pet_window.py (9处: L531, L1367, L1614, L1656, L1905, L1916, L2216, L2252, L2382)
- app/voice/playback_controller.py (2处: L72, L119)
- app/voice/tts.py (1处: L758)

### 2.6 配置漂移 (P1)

| 问题 | 详情 |
|------|------|
| api.yaml含明文API Key | 虽然由用户写入，但debug log在bootstrap中直接打印了整个settings包括api_key |
| 旧.env引用仍存在 | README/README.en仍大量描述.env配置方式 |
| 无配置迁移工具 | 如果用户有旧.env，无法自动迁移到YAML |

### 2.7 SDK设计缺陷 (P1)

| 问题 | 详情 |
|------|------|
| 全局可变状态 | sdk/tool_registry.py使用module-level _REGISTERED_TOOLS列表 |
| 插件生命周期简陋 | PluginBase没有priority/enabled/plugin_version作为框架强约束 |
| 贡献点不完整 | PluginCapabilityRegistry仅支持tools_tabs |
| 失败隔离不足 | _load_plugin中一个插件失败可能影响后续插件 |

### 2.8 AgentRuntime职责过重 (P1)

2081行的runtime.py应拆分为:
1. **工具循环**: _run_tool_loop
2. **工具执行**: 单次工具调用处理
3. **浏览器路由**: 15+方法 (browser page/visible browser/windows阻断)
4. **工具结果处理**: 图片提取/结果格式化
5. **PendingAction流程**: 确认/取消续跑
6. **事件流程**: reminder_due/proactive_check
7. **提示词拼接**: 4种提示词构建
8. **回复构建**: 4种fallback回复
9. **运行时限制**: 8个常量散落模块顶层

### 2.9 PetWindow职责过重 (P1)

2630行的pet_window.py承载14+职责(窗口/托盘/字幕/立绘/工具确认/主动关怀/屏幕观察/历史回看/回复历史/记忆整理/提醒检查/事件处理/手动截图/启动初始化/TTS)

### 2.10 测试缺口 (P2)

| 缺失测试 | 风险 |
|----------|------|
| 工具调用上限 | 高 |
| pending action中断与续跑 | 高 |
| 浏览器/Windows工具路由拦截 | 高 |
| screen observation允许/禁止逻辑 | 中 |
| proactive_check事件流程 | 中 |
| vision unsupported fallback | 中 |
| 插件manifest解析 | 中 |
| 插件优先级与失败隔离 | 中 |
| 配置迁移(空配置->默认->无效值fallback) | 中 |
| MCP工具生命周期 | 低 |
---

## 3. 推荐删除清单

| 目标 | 原因 | 替代 |
|------|------|------|
| app/env_config.py | 不存在(已删除)，README仍有引用 | app/config/settings_service.py |
| config.example.env | 不存在(已删除)，README仍有引用 | data/config/api.yaml |
| app/core/extensions.py中的ToolContributor/TTSProviderContributor协议 | 仅定义，几乎未被使用 | SDK PluginBase + PluginManager |
| sdk/tool_registry.py全局变量设计 | side-effect设计，与其他注册机制冲突 | 改用实例化ToolRegistry |
| app/agent/tool_policy.py中的常量(如在其他模块重复定义) | 与runtime.py中的路由逻辑紧耦合 | 合并到ToolRoutingPolicy |
| README中旧的.env配置文档段落 | 与实际实现不符 | 更新为YAML配置 |
| app/llm/prompts/__init__.py(如为空) | 仅作包标记，无实质内容 | 保留目录结构即可 |

---

## 4. 推荐合并清单

| 合并目标 | 涉及模块 |
|----------|----------|
| 统一工具注册入口 | app/agent/tool_registry.py <- sdk/tool_registry.py <- app/core/extensions.py -> 单一ToolRegistry |
| 统一工具确认/风险策略 | 将tool_policy.py的路由常量+runtime.py中的路由逻辑+tool_registry.py中的free_access_enabled合并为ToolPermissionPolicy |
| 屏幕观察统一 | app/agent/screen_tools.py + app/agent/screen_policy.py + app/screen_observation.py -> app/agent/screen/ |
| 插件能力收集统一 | SDK的PluginCapabilityRegistry + SakuraPluginManager的能力收集 -> 统一PluginManager |
| 根目录文件归位 | chat_worker.py->app/core/, screen_observation.py->app/agent/, proactive_care.py->app/agent/, portrait_utils.py->app/ui/, debug_log.py->app/core/ |
| 配置模型集中 | 各模块中散落的配置dataclass -> app/config/models.py |

---

## 5. 推荐新边界

`
app/
  core/
    bootstrap/
      app_builder.py          # AppBuilder: 声明式装配
      service_container.py    # ServiceContainer: 依赖注入
      lifecycle.py            # 启动/关闭生命周期
    runtime/
      chat_pipeline.py        # ChatPipeline (已有，保留)
      event_pipeline.py       # 事件处理编排
    contracts/                # 共享数据协议
      messages.py
      replies.py
      actions.py
      events.py

  agent/
    runtime/
      agent_runtime.py        # * Facade: 仅handle_*入口
      tool_loop.py            # ToolLoopRunner
      tool_executor.py        # ToolCallExecutor
      prompt_composer.py      # PromptComposer
      routing_policy.py       # ToolRoutingPolicy
      pending_flow.py         # PendingActionFlow
      event_flow.py           # EventFlow
      runtime_limits.py       # RuntimeLimits
    tools/
      registry.py             # 统一ToolRegistry
      permission_policy.py    # ToolPermissionPolicy
      builtin/                # 内置工具
      mcp/                    # MCP工具 (已有)
      plugins/                # 插件工具贡献适配
    memory/                   # MemoryStore + Curator
    reminders/                # ReminderStore
    screen/                   # 屏幕观察统一

  config/
    models.py                 # 配置dataclass集中
    settings_service.py       # YAML读写
    migrations.py             # 配置迁移
    defaults.py               # 默认值

  plugins/
    manager.py                # PluginManager (统一入口)
    discovery.py              # PluginDiscovery
    capabilities.py           # PluginCapabilityRegistry
    host_context.py           # PluginHostContext
    adapters.py               # SDK -> app适配

  ui/
    pet/                      # 主窗口
    settings/                 # 设置 (按tab拆分)
    history/                  # 历史回看
    components/               # 共享UI组件

  storage/
    paths.py                  # StoragePaths 统一路径
    chat_history.py           # ChatHistoryStore
    visual_observation.py     # VisualObservationStore
    memory_store.py           # MemoryStore持久化

  voice/
    tts.py                    # TTS provider
    playback.py               # 播放控制
`
---

## 6. 风险等级

| 阶段 | 风险 | 说明 |
|------|------|------|
| 阶段0 (体检) | **低** | 只读分析，无代码修改 |
| 阶段1 (边界重划) | **中** | 大范围文件移动，需同步import路径和测试 |
| 阶段2 (拆AgentRuntime) | **高** | 2081行核心逻辑，需充分的characterization tests |
| 阶段3 (统一工具注册) | **中** | 多入口合并，需保持MCP/插件/内置工具可用 |
| 阶段4 (插件系统升级) | **中** | 涉及SDK接口变更，需向后兼容playwright_browser |
| 阶段5 (配置清理) | **中** | 删除旧.env文档，需确保用户不依赖旧路径 |
| 阶段6 (UI减负) | **高** | 2630行PetWindow拆分，UI回归风险大 |
| 阶段7 (存储统一) | **低** | 主要是接口抽象和路径统一 |
| 阶段8 (删除废弃) | **中** | 大胆删除但需可追踪(DELETION_PLAN.md) |
| 阶段9 (测试/文档) | **低** | 补测试和写文档 |

---

## 7. 对应测试策略

### 7.1 拆分前Characterization Tests

在拆分AgentRuntime和PetWindow之前，先为关键行为补测试:

1. **工具调用上限**: MAX_AGENT_STEPS_PER_TURN / MAX_TOOL_CALLS_PER_STEP / MAX_TOOL_CALLS_PER_TURN
2. **Pending action中断与续跑**: 确认/取消后正确地继续或跳过工具
3. **浏览器路由拦截**: browser page模式禁用Windows工具 / visible browser模式禁用后台web工具
4. **屏幕观察**: observe_screen工具仅在允许时可用 / screen_context在proactive事件中的使用
5. **Vision fallback**: 模型不支持视觉时返回提示而非报错
6. **Proactive事件**: reminder_due / proactive_check 消息构造和事件回复

### 7.2 拆分中回归策略

- 每拆出一个模块，将原测试指向新模块
- 保持原有集成测试通过
- import路径变更时同步更新测试mock路径

### 7.3 新模块单元测试

- **ToolRegistry**: 注册/describe/execute/搜索/分组过滤/能力过滤
- **ToolPermissionPolicy**: 风险级别/确认策略/active_groups/allowed_capabilities
- **PluginManager**: manifest解析/优先级排序/失败隔离/工具贡献/UI贡献
- **PluginDiscovery**: 插件发现/启用禁用/版本检查
- **SettingsService**: 空配置默认值/旧配置迁移/无效值fallback/保存格式化
- **StoragePaths**: 路径生成/隔离/冲突检测

### 7.4 集成测试保留

- **test_agent_core.py**: Agent完整链路
- **test_chat_pipeline.py**: ChatPipeline编排
- **test_chat_worker.py**: Qt线程Worker
- **test_native_tool_calls.py**: 原生工具调用

---

## 8. 附录: 文件统计

| 指标 | 数值 |
|------|------|
| Python源文件总数 | 60+ |
| 最大文件 | app/ui/pet_window.py (2630行) |
| 第二大 | app/agent/runtime.py (2081行) |
| print()散布文件数 | 8个文件，21处 |
| 测试文件数 | 18个 (12 unit + 4 integration + 2 ui) |
| 配置来源 | 5个YAML文件 (data/config/*.yaml) |
| 本地插件数 | 1 (playwright_browser) |
| MCP Server | 3 (web/playwright/windows) |
| Markdown文档 | 4个 (README.md / README.en.md / README.zh.md / task.md) |

---

> 本报告是阶段0的交付物，后续阶段将基于此报告开展重构工作。
> 每个阶段的产出将追加到对应的PR/commit中。
