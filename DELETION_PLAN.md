# DELETION_PLAN.md — Sakura 废弃代码删除计划

> 每个删除项包含：文件/符号名、删除原因、替代实现、引用搜索结果、测试证明。

---

## 1. 已确认可删除

### 1.1 文件级别

| 文件 | 原因 | 替代 |
|------|------|------|
| `app/env_config.py` | 不存在（已删除）。README 仍有引用 | `app/config/settings_service.py` |
| `config.example.env` | 不存在（已删除）。README 仍有引用 | `data/config/api.yaml` |

### 1.2 模块级别 — 旧 SDK 全局状态

| 符号 | 位置 | 原因 | 替代 |
|------|------|------|------|
| `_REGISTERED_TOOLS` (全局列表) | `sdk/tool_registry.py` | side-effect 设计，与实例化注册表冲突 | `PluginCapabilityRegistry.register_tool()` |
| `clear_registered_tools()` | `sdk/tool_registry.py` | 同上 | 使用局部注册表实例 |
| `registered_tools()` | `sdk/tool_registry.py` | 同上 | `PluginCapabilityRegistry.tools` |

**保留理由:** 暂时保留以兼容 `plugins/playwright_browser`，待其迁移到新接口后可删除。

### 1.3 重复协议定义

| 符号 | 位置 | 原因 | 替代 |
|------|------|------|------|
| `ToolContributor` 协议 | `app/core/extensions.py` | 仅定义，无实际使用者 | `BuiltinToolProvider.contribute_tools()` |
| `TTSProviderContributor` 协议 | `app/core/extensions.py` | 同上 | 直接使用 TTSProvider |
| `SettingsContributor` 协议 | `app/core/extensions.py` | 同上 | `SettingsPanelContribution` |

**删除条件:** `ExtensionRegistry` 本身可以保留作为 future compatibility，但三个未使用的 Protocol 可删除。

### 1.4 重复路径构建

| 调用点 | 文件 | 应迁移到 |
|--------|------|----------|
| `base_dir / "data" / "tasks.json"` | `builtin_tools.py:21` | `StoragePaths.tasks_store()` |
| `base_dir / "data" / "notes"` | `builtin_tools.py:22` | `StoragePaths.notes_dir` |
| `base_dir / "data" / "memory.json"` | `builtin_tools.py:23` | `StoragePaths.memory_store()` |
| `base_dir / "data" / "reminders.json"` | multiple | `StoragePaths.reminders_store()` |
| `base_dir / "data" / "config"` | `settings_service.py:36` | `StoragePaths.config_dir` |
| `base_dir / "data" / "chat_history" / ...` | `bootstrap.py`, `pet_window.py` | `StoragePaths.chat_history_for()` |
| `base_dir / "data" / "visual_observations" / ...` | `bootstrap.py`, `pet_window.py` | `StoragePaths.visual_observations_for()` |

### 1.5 根目录残留函数引用

这些是**代码注释/文档中**的旧路径引用，不涉及实际代码：
- README 中 `app/api_client.py` → 已迁移到 `app/llm/api_client.py`
- README 中 `app/env_config.py` → 文件已不存在
- README 中 `app/pet_window.py` → 已迁移到 `app/ui/pet_window.py`

## 2. 待确认可删除

### 2.1 `app/core/extensions.py`

**当前状态:** 140 行，定义了 ExtensionRegistry + 3 个 Protocol。
**引用检查:** 仅在 `app/core/app_context.py` 和 `app/core/bootstrap.py` 中被实例化，但 `apply_tools()` 被调用，其余功能未使用。
**建议:** 保留 ExtensionRegistry 但删除未使用的 Protocol。或者整体删除 ExtensionRegistry（如果后续有 plugin 系统替代）。

### 2.2 旧配置文档段落

**位置:** README.md / README.en.md
- "快速开始" 中的 `copy config.example.env` + `notepad .env` 
- "启动流程" 中的 `.env` 描述
- "配置项" 表格中的 .env 风格 key 名

**建议:** 替换为 YAML 配置描述。

### 2.3 `_SANITIZE_SCHEMA_PROPERTIES` 重复

**位置:** `app/agent/tool_registry.py` (旧) 和 `app/agent/tools/registry.py` (新)
**原因:** 向后兼容 shim 只是 re-export，无重复代码。**无需删除。**

## 3. 不可删除 (有引用 + 测试覆盖)

| 符号 | 原因 |
|------|------|
| `AgentRuntime` 核心方法 | 2081 行核心逻辑，有集成测试覆盖 |
| `ChatPipeline` | 被 ChatWorker/EventWorker 使用 |
| `ToolRegistry` | 被 AgentRuntime/MCP/PluginManager 使用 |
| `MemoryStore` / `ReminderStore` | 被内置工具使用 |
| `MCPToolProvider` | 被 bootstrap 使用 |
| `plugins/playwright_browser/` | 唯一本地插件，有功能测试 |
| `sdk/plugin.py` PluginBase | 被 playwright_browser 继承 |

## 4. 删除执行记录

| 阶段 | 删除项 | 状态 | 验证 |
|------|--------|------|------|
| 阶段 1 | `app/env_config.py` (文件不存在，README引用清) | ✅ | README 已无引用 |
| 阶段 1 | `config.example.env` (文件不存在，README引用清) | ✅ | README 已无引用 |
| 阶段 2 | runtime.py 中的 MAX_* 常量 | ✅ | 迁移到 runtime_limits.py |
| 阶段 7 | 各处手写 `base_dir / "data" / ...` | 🔄 | StoragePaths 已创建，待逐处迁移 |
