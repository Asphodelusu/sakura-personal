# MIGRATION.md — Sakura 重构迁移指南

## .env → YAML 配置迁移

Sakura 的配置已从 `.env` 迁移到 `data/config/*.yaml`。

### 自动迁移

```bash
python -c "from app.config.migrations import migrate_env_to_yaml; from pathlib import Path; r = migrate_env_to_yaml(Path('.env'), Path('data/config/api.yaml'), Path('data/config/system_config.yaml')); print(r)"
```

### 手动迁移对照

| 旧 .env key | 新 YAML 路径 |
|-------------|-------------|
| `BASE_URL` | `api.yaml: llm.base_url` |
| `API_KEY` | `api.yaml: llm.api_key` |
| `MODEL` | `api.yaml: llm.model` |
| `API_TIMEOUT_SECONDS` | `api.yaml: llm.timeout_seconds` |
| `TTS_ENABLED` | `api.yaml: tts.enabled` |
| `GPT_SOVITS_API_URL` | `api.yaml: tts.gpt_sovits.api_url` |
| `SUBTITLE_LANGUAGE` | `system_config.yaml: ui.subtitle_language` |
| `PROACTIVE_CARE_ENABLED` | `system_config.yaml: proactive_care.enabled` |
| `AUTO_MEMORY_ENABLED` | `system_config.yaml: memory_curation.enabled` |
| `WINDOWS_MCP_ENABLED` | `system_config.yaml: mcp.windows_enabled` |
| `SAKURA_DEBUG` | `system_config.yaml: debug.enabled` |
| `CURRENT_CHARACTER_ID` | `characters.yaml: current_character_id` |

---

## 旧 SDK → 新插件接口迁移

### 工具注册

旧方式 (已废弃):
```python
from sdk.tool_registry import tool

@tool(name="my_tool", description="...", group="default")
def my_tool(**kwargs): ...
```

新方式:
```python
from sdk.types import ToolContribution

register.register_tool(ToolContribution(
    name="my_tool", description="...", handler=my_tool, group="default"
))
```

---

## 文件路径变化

| 旧路径 | 新路径 |
|--------|--------|
| `app/debug_log.py` | `app/core/debug_log.py` |
| `app/chat_worker.py` | `app/core/chat_worker.py` |
| `app/screen_observation.py` | `app/agent/screen_observation.py` |
| `app/proactive_care.py` | `app/agent/proactive_care.py` |
| `app/portrait_utils.py` | `app/ui/portrait_utils.py` |
| `app/env_config.py` | 已删除，使用 `app/config/settings_service.py` |
| `config.example.env` | 已删除，参考 `data/config/api.yaml` |

---

## RuntimeLimits 迁移

`MAX_*` 常量已从 `runtime.py` 提取到 `app/agent/runtime_limits.py`。

```python
# 旧
from app.agent.runtime import MAX_AGENT_STEPS_PER_TURN

# 新
from app.agent.runtime_limits import MAX_AGENT_STEPS_PER_TURN
```

---

## ToolRegistry 迁移

新代码应使用统一包 `app/agent/tools/`:

```python
from app.agent.tools import Tool, ToolRegistry, ToolPermissionPolicy, ToolMetadata
```

旧路径 `app/agent/tool_registry.py` 保持兼容，但不推荐使用。
