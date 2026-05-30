from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import uuid

import pytest

from app.agent.actions import PendingToolAction
from app.agent.builtin_tools import create_builtin_tool_registry
from app.agent.memory import MemoryStore
from app.agent.mcp.bridge import MCPToolSpec
from app.agent.mcp.config import load_mcp_config
from app.agent.mcp.provider import MCPToolProvider, register_mcp_tools_from_config
from app.agent.reminders import ReminderStore
from app.agent.runtime import AgentRuntime, _build_tool_results_message
from app.agent.runtime import _redact_tool_result_for_model
from app.agent.screen_tools import (
    OBSERVE_SCREEN_TOOL_NAME,
    SCREEN_OBSERVATION_DISABLED_ERROR,
    SCREEN_OBSERVATION_REQUEST_ACTION,
    create_screen_observation_tool,
)
from app.agent.tool_registry import Tool, ToolExecutionResult, ToolRegistry
from app.api_client import ApiRequestError, is_vision_unsupported_error, messages_contain_image
from app.context_trimming import MAX_MODEL_CONTEXT_MESSAGES, trim_messages_for_model
from app.screen_observation import (
    SCREEN_OBSERVATION_HISTORY_MARKER,
    ScreenObservation,
    append_observation_marker,
    build_screen_observation_user_message,
    should_observe_screen,
)


def test_add_reminder_delay_seconds_generates_future_time() -> None:
    store = ReminderStore(_runtime_json_path("reminders"))
    before = datetime.now().astimezone()

    result = store.add_reminder({"text": "喝水", "delay_seconds": 30})

    trigger_at = datetime.fromisoformat(result["reminder"]["trigger_at"])
    after = datetime.now().astimezone()
    assert before + timedelta(seconds=25) <= trigger_at <= after + timedelta(seconds=35)


def test_add_reminder_delay_minutes_generates_future_time() -> None:
    store = ReminderStore(_runtime_json_path("reminders"))
    before = datetime.now().astimezone()

    result = store.add_reminder({"text": "休息", "delay_minutes": 2})

    trigger_at = datetime.fromisoformat(result["reminder"]["trigger_at"])
    after = datetime.now().astimezone()
    assert before + timedelta(seconds=115) <= trigger_at <= after + timedelta(seconds=125)


def test_add_reminder_rejects_past_trigger_at() -> None:
    store = ReminderStore(_runtime_json_path("reminders"))
    past = (datetime.now().astimezone() - timedelta(minutes=1)).isoformat(timespec="seconds")

    with pytest.raises(ValueError, match="提醒时间必须晚于当前时间"):
        store.add_reminder({"text": "过期提醒", "trigger_at": past})


def test_due_reminders_and_mark_completed() -> None:
    store = ReminderStore(_runtime_json_path("reminders"))
    now = datetime.now().astimezone()
    due = store.add_reminder({"text": "到点", "delay_seconds": 1})["reminder"]
    future = store.add_reminder({"text": "稍后", "delay_minutes": 5})["reminder"]

    due["trigger_at"] = (now - timedelta(seconds=1)).isoformat(timespec="seconds")
    future["trigger_at"] = (now + timedelta(minutes=5)).isoformat(timespec="seconds")
    store._save({"reminders": [due, future]})

    due_reminders = store.due_reminders(now)
    assert [reminder["id"] for reminder in due_reminders] == [due["id"]]

    store.mark_completed(due["id"])

    assert store.due_reminders(now) == []


def test_memory_propose_update_only_creates_pending_record() -> None:
    store = MemoryStore(_runtime_json_path("memory"))

    result = store.propose_memory_update(
        {
            "category": "preference",
            "content": "主人喜欢中文回复",
            "reason": "长期偏好",
        }
    )

    snapshot = store.snapshot()
    assert snapshot["memories"] == []
    assert snapshot["pending_updates"] == [result["pending_update"]]


def test_memory_confirm_update_moves_pending_to_memories() -> None:
    store = MemoryStore(_runtime_json_path("memory"))
    pending = store.propose_memory_update(
        {
            "category": "project",
            "content": "Sakura 正在稳定 Agent 内核",
        }
    )["pending_update"]

    result = store.confirm_memory_update({"id": pending["id"]})

    snapshot = store.snapshot()
    assert snapshot["pending_updates"] == []
    assert snapshot["memories"] == [result["memory"]]


def test_tool_registry_requires_confirmation_returns_pending_action() -> None:
    registry = ToolRegistry(
        [
            Tool(
                name="open_url",
                description="打开网页",
                handler=lambda _arguments: {"opened": True},
                requires_confirmation=True,
            )
        ]
    )
    registry.set_free_access_enabled(False)

    result = registry.prepare_or_execute(
        "open_url",
        {"url": "https://example.com"},
        "用户要求打开网页",
    )

    assert isinstance(result, PendingToolAction)
    assert result.tool_name == "open_url"
    assert result.arguments == {"url": "https://example.com"}


def test_tool_registry_free_access_is_enabled_by_default() -> None:
    registry = ToolRegistry(
        [
            Tool(
                name="open_url",
                description="打开网页",
                handler=lambda _arguments: {"opened": True},
                requires_confirmation=True,
            )
        ]
    )
    result = registry.prepare_or_execute("open_url", {"url": "https://example.com"})

    assert not isinstance(result, PendingToolAction)
    assert result.success
    assert result.content == {"opened": True}


def test_tool_registry_free_access_keeps_file_delete_confirmation() -> None:
    registry = ToolRegistry(
        [
            Tool(
                name="delete_file",
                description="删除本地文件",
                handler=lambda _arguments: {"deleted": True},
                requires_confirmation=True,
                confirmation_risk="delete_file",
            )
        ]
    )
    registry.set_free_access_enabled(True)

    result = registry.prepare_or_execute("delete_file", {"path": "a.txt"})

    assert isinstance(result, PendingToolAction)
    assert result.tool_name == "delete_file"


def test_tool_registry_free_access_keeps_high_risk_confirmation() -> None:
    registry = ToolRegistry(
        [
            Tool(
                name="run_external_action",
                description="执行高风险动作",
                handler=lambda _arguments: {"done": True},
                requires_confirmation=True,
                risk="high",
            )
        ]
    )
    registry.set_free_access_enabled(True)

    result = registry.prepare_or_execute("run_external_action", {"value": "x"})

    assert isinstance(result, PendingToolAction)
    assert result.tool_name == "run_external_action"


def test_tool_registry_describes_group_and_risk_metadata() -> None:
    registry = ToolRegistry(
        [
            Tool(
                name="search_memory",
                description="搜索记忆",
                group="memory",
                risk="low",
            )
        ]
    )

    description = registry.describe_tools()[0]

    assert description["group"] == "memory"
    assert description["risk"] == "low"


def test_tool_registry_filters_tools_by_capability() -> None:
    registry = ToolRegistry(
        [
            Tool(name="plain_tool", description="普通工具"),
            create_screen_observation_tool(),
        ]
    )

    visible_without_capability = {tool["name"] for tool in registry.describe_tools(set())}
    visible_with_capability = {
        tool["name"]
        for tool in registry.describe_tools({"screen_observation"})
    }

    assert OBSERVE_SCREEN_TOOL_NAME not in visible_without_capability
    assert OBSERVE_SCREEN_TOOL_NAME in visible_with_capability


def test_mcp_missing_config_does_not_register_tools() -> None:
    root = _runtime_root_path("mcp_missing")
    registry = ToolRegistry()

    provider = register_mcp_tools_from_config(root, registry)

    assert provider is None
    assert registry.describe_tools() == []


def test_mcp_disabled_config_does_not_register_tools() -> None:
    root = _runtime_root_path("mcp_disabled")
    config_dir = root / "data" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "mcp.yaml").write_text(
        """
enabled: false
servers:
  demo:
    transport: stdio
    command: python
""".strip(),
        encoding="utf-8",
    )

    registry = ToolRegistry()
    provider = register_mcp_tools_from_config(
        root,
        registry,
        bridge_factory=_FakeMCPBridge,
    )

    assert provider is None
    assert registry.describe_tools() == []


def test_mcp_empty_servers_do_not_register_tools() -> None:
    root = _runtime_root_path("mcp_empty")
    config_dir = root / "data" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "mcp.yaml").write_text("enabled: true\nservers: {}\n", encoding="utf-8")

    registry = ToolRegistry()
    provider = register_mcp_tools_from_config(root, registry)

    assert provider is None
    assert registry.describe_tools() == []


def test_mcp_invalid_config_does_not_break_registry() -> None:
    root = _runtime_root_path("mcp_invalid")
    config_dir = root / "data" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "mcp.yaml").write_text("enabled: [not_bool]\n", encoding="utf-8")
    registry = ToolRegistry()

    provider = register_mcp_tools_from_config(root, registry)

    assert provider is None
    assert registry.describe_tools() == []


def test_mcp_config_parses_prefix_and_low_risk() -> None:
    config_path = _runtime_root_path("mcp_parse") / "mcp.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        """
enabled: true
default_call_timeout: 12
servers:
  demo:
    transport: sse
    url: http://127.0.0.1:8000/sse
    name_prefix: demo_
    risk: low
""".strip(),
        encoding="utf-8",
    )

    config = load_mcp_config(config_path)

    assert config.enabled
    assert config.default_call_timeout == 12
    assert config.servers[0].effective_name_prefix() == "demo_"
    assert config.servers[0].risk == "low"
    assert not config.servers[0].effective_requires_confirmation()


def test_mcp_provider_registers_prefixed_tools_and_schema() -> None:
    root = _runtime_root_path("mcp_register")
    config_dir = root / "data" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "mcp.yaml").write_text(
        """
enabled: true
servers:
  demo:
    transport: stdio
    command: python
    name_prefix: demo_
""".strip(),
        encoding="utf-8",
    )
    registry = ToolRegistry()

    provider = register_mcp_tools_from_config(
        root,
        registry,
        bridge_factory=_FakeMCPBridge,
    )

    assert provider is not None
    tool = registry.get("demo_echo")
    assert tool is not None
    assert tool.parameters["properties"]["message"]["type"] == "string"
    assert tool.group == "mcp"
    assert tool.risk == "medium"
    assert tool.requires_confirmation
    provider.close()


def test_mcp_tool_call_uses_external_name() -> None:
    root = _runtime_root_path("mcp_call")
    registry = ToolRegistry()
    provider = MCPToolProvider(
        load_mcp_config(_write_mcp_config(root, "name_prefix: demo_\nrisk: low")),
        bridge_factory=_FakeMCPBridge,
    )
    provider.register_tools(registry)

    result = registry.execute("demo_echo", {"message": "hello"})

    assert result.success
    assert result.content["server_tool_name"] == "echo"
    assert result.content["arguments"] == {"message": "hello"}
    provider.close()


def test_mcp_tool_call_exception_returns_failed_result() -> None:
    root = _runtime_root_path("mcp_fail")
    registry = ToolRegistry()
    provider = MCPToolProvider(
        load_mcp_config(_write_mcp_config(root, "name_prefix: fail_\nrisk: low")),
        bridge_factory=_FailingMCPBridge,
    )
    provider.register_tools(registry)

    result = registry.execute("fail_echo", {"message": "hello"})

    assert not result.success
    assert "MCP 调用失败" in result.error
    provider.close()


def test_mcp_high_risk_still_requires_confirmation_with_free_access() -> None:
    root = _runtime_root_path("mcp_high_risk")
    registry = ToolRegistry()
    provider = MCPToolProvider(
        load_mcp_config(_write_mcp_config(root, "name_prefix: dangerous_\nrisk: high")),
        bridge_factory=_FakeMCPBridge,
    )
    provider.register_tools(registry)
    registry.set_free_access_enabled(True)

    result = registry.prepare_or_execute("dangerous_echo", {"message": "hello"})

    assert isinstance(result, PendingToolAction)
    assert result.tool_name == "dangerous_echo"
    provider.close()


def test_builtin_registry_includes_browser_tools() -> None:
    registry = create_builtin_tool_registry(Path(__file__).resolve().parents[1])

    names = {tool["name"] for tool in registry.describe_tools()}

    assert {
        "browser_open_url",
        "browser_get_content",
        "browser_scroll",
        "browser_click",
        "browser_get_state",
    }.issubset(names)
    assert OBSERVE_SCREEN_TOOL_NAME in names


def test_browser_open_url_rejects_non_http_url() -> None:
    registry = create_builtin_tool_registry(Path(__file__).resolve().parents[1])

    result = registry.execute("browser_open_url", {"url": "file:///C:/secret.txt"})

    assert not result.success
    assert "URL 只支持" in result.error


def test_browser_confirmation_tools_return_pending_actions() -> None:
    registry = create_builtin_tool_registry(
        Path(__file__).resolve().parents[1],
        browser_executor=_FakeBrowserExecutor(),
    )
    registry.set_free_access_enabled(False)

    open_result = registry.prepare_or_execute("browser_open_url", {"url": "https://example.com"})
    scroll_result = registry.prepare_or_execute("browser_scroll", {"direction": "down"})
    click_result = registry.prepare_or_execute("browser_click", {"selector": "button"})

    assert isinstance(open_result, PendingToolAction)
    assert isinstance(scroll_result, PendingToolAction)
    assert isinstance(click_result, PendingToolAction)


def test_browser_confirmation_tools_obey_free_access() -> None:
    registry = create_builtin_tool_registry(
        Path(__file__).resolve().parents[1],
        browser_executor=_FakeBrowserExecutor(),
    )
    registry.set_free_access_enabled(True)

    result = registry.prepare_or_execute("browser_scroll", {"direction": "down", "amount": 1200})

    assert not isinstance(result, PendingToolAction)
    assert result.success
    assert result.content["scroll_y"] == 1200


def test_browser_get_content_truncates_text_and_links() -> None:
    registry = create_builtin_tool_registry(
        Path(__file__).resolve().parents[1],
        browser_executor=_FakeBrowserExecutor(),
    )

    result = registry.execute("browser_get_content", {"max_chars": 5})

    assert result.success
    assert result.content["text"] == "abcde"
    assert len(result.content["links"]) == 20


def test_browser_tools_validate_arguments() -> None:
    registry = create_builtin_tool_registry(
        Path(__file__).resolve().parents[1],
        browser_executor=_FakeBrowserExecutor(),
    )

    bad_direction = registry.execute("browser_scroll", {"direction": "sideways"})
    bad_selector = registry.execute("browser_click", {"selector": ""})

    assert not bad_direction.success
    assert "direction 只支持" in bad_direction.error
    assert not bad_selector.success
    assert "缺少必填参数" in bad_selector.error


def test_browser_screenshot_fallback_is_attached_as_image_url() -> None:
    result = ToolExecutionResult(
        tool_name="browser_get_content",
        success=True,
        content={
            "url": "https://example.com",
            "title": "Canvas Page",
            "text": "",
            "screenshot_data_url": "data:image/jpeg;base64,abc123",
            "screenshot_fallback": True,
        },
    )

    message = _build_tool_results_message([result], include_images=True)

    content = message["content"]
    assert isinstance(content, list)
    assert content[1] == {
        "type": "image_url",
        "image_url": {
            "url": "data:image/jpeg;base64,abc123",
            "detail": "low",
        },
    }
    assert "screenshot_data_url" not in content[0]["text"]
    assert "screenshot_attached" in content[0]["text"]


def test_browser_screenshot_fallback_is_not_attached_without_vision() -> None:
    result = ToolExecutionResult(
        tool_name="browser_get_content",
        success=True,
        content={
            "url": "https://example.com",
            "title": "Canvas Page",
            "text": "",
            "screenshot_data_url": "data:image/jpeg;base64,abc123",
        },
    )

    message = _build_tool_results_message([result], include_images=False)

    assert isinstance(message["content"], str)
    assert "screenshot_data_url" not in message["content"]
    assert "screenshot_attached" in message["content"]


def test_tool_result_for_model_truncates_large_content() -> None:
    result = ToolExecutionResult(
        tool_name="browser_get_content",
        success=True,
        content={
            "url": "https://example.com",
            "text": "x" * 7000,
        },
    )

    redacted = _redact_tool_result_for_model(result)

    content = redacted["content"]
    assert isinstance(content, dict)
    assert content["truncated"] is True
    assert content["original_chars"] > 6000
    assert "head" in content
    assert "tail" in content


def test_trim_messages_for_model_keeps_recent_messages_without_mutating_history() -> None:
    messages = [
        {"role": "user", "content": f"message {index}"}
        for index in range(MAX_MODEL_CONTEXT_MESSAGES + 5)
    ]

    trimmed = trim_messages_for_model(messages)

    assert len(trimmed) == MAX_MODEL_CONTEXT_MESSAGES
    assert trimmed[0]["content"] == "message 5"
    assert len(messages) == MAX_MODEL_CONTEXT_MESSAGES + 5


def test_model_vision_is_enabled_by_default_for_screen_observation() -> None:
    class ScreenRequestClient:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def complete_raw(self, system_prompt, *_args, **_kwargs) -> str:  # type: ignore[no-untyped-def]
            self.prompts.append(system_prompt)
            return (
                '{"reply":{"segments":[{"ja":"見るね。","zh":"我看看。","tone":"提醒"}]},'
                '"tool_calls":[{"name":"observe_screen","arguments":{},"reason":"需要当前画面"}]}'
            )

    client = ScreenRequestClient()
    runtime = AgentRuntime(
        api_client=client,  # type: ignore[arg-type]
        system_prompt="你是 Sakura。",
        tools=ToolRegistry([create_screen_observation_tool()]),
    )
    result = runtime.handle_user_message([{"role": "user", "content": "这个界面哪里不对"}])

    assert "observe_screen" in client.prompts[0]
    assert result.actions
    assert result.actions[0].type == SCREEN_OBSERVATION_REQUEST_ACTION


def test_model_vision_disabled_hides_screen_observation_tool() -> None:
    class PromptCaptureClient:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def complete_raw(self, system_prompt, *_args, **_kwargs) -> str:  # type: ignore[no-untyped-def]
            self.prompts.append(system_prompt)
            return '{"reply":{"segments":[{"ja":"見えないよ。","zh":"我看不到哦。","tone":"中性"}]}}'

    client = PromptCaptureClient()
    runtime = AgentRuntime(
        api_client=client,  # type: ignore[arg-type]
        system_prompt="你是 Sakura。",
        tools=ToolRegistry([create_screen_observation_tool()]),
    )
    runtime.set_model_vision_enabled(False)

    runtime.handle_user_message([{"role": "user", "content": "普通聊天"}])

    assert OBSERVE_SCREEN_TOOL_NAME not in client.prompts[0]


def test_screen_observation_tool_hidden_after_image_is_attached() -> None:
    class PromptCaptureClient:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def complete_raw(self, system_prompt, *_args, **_kwargs) -> str:  # type: ignore[no-untyped-def]
            self.prompts.append(system_prompt)
            return '{"reply":{"segments":[{"ja":"見るよ。","zh":"我看看。","tone":"中性"}]}}'

    client = PromptCaptureClient()
    runtime = AgentRuntime(
        api_client=client,  # type: ignore[arg-type]
        system_prompt="你是 Sakura。",
        tools=ToolRegistry([create_screen_observation_tool()]),
    )
    runtime.set_model_vision_enabled(True)

    runtime.handle_user_message(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "已经有图了"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64,abc123"},
                    },
                ],
            }
        ]
    )

    assert OBSERVE_SCREEN_TOOL_NAME not in client.prompts[0]


def test_hidden_screen_observation_call_returns_failure_without_action() -> None:
    class HiddenScreenRequestClient:
        def complete_raw(self, *_args, **_kwargs) -> str:  # type: ignore[no-untyped-def]
            return (
                '{"reply":{"segments":[{"ja":"見るね。","zh":"我看看。","tone":"提醒"}]},'
                '"tool_calls":[{"name":"observe_screen","arguments":{},"reason":"需要当前画面"}]}'
            )

        def chat(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            from app.chat_reply import parse_chat_reply

            return parse_chat_reply(
                '{"segments":[{"ja":"今は見えないよ。","zh":"现在看不到哦。","tone":"中性"}]}'
            )

    runtime = AgentRuntime(
        api_client=HiddenScreenRequestClient(),  # type: ignore[arg-type]
        system_prompt="你是 Sakura。",
        tools=ToolRegistry([create_screen_observation_tool()]),
    )
    runtime.set_model_vision_enabled(False)

    result = runtime.handle_user_message([{"role": "user", "content": "普通聊天"}])

    assert not any(action.type == SCREEN_OBSERVATION_REQUEST_ACTION for action in result.actions)
    assert result.actions[0].payload["error"] == SCREEN_OBSERVATION_DISABLED_ERROR


def test_screen_observation_message_uses_openai_image_url_format() -> None:
    observation = ScreenObservation(
        data_url="data:image/jpeg;base64,abc123",
        width=1280,
        height=720,
        captured_at="2026-05-29T20:00:00+08:00",
        screen_name="DISPLAY1",
    )

    message = build_screen_observation_user_message("帮我看这个", observation)

    assert message["role"] == "user"
    content = message["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[1] == {
        "type": "image_url",
        "image_url": {
            "url": "data:image/jpeg;base64,abc123",
            "detail": "low",
        },
    }
    assert messages_contain_image([message])


def test_screen_observation_history_marker_does_not_store_image_data() -> None:
    observation = ScreenObservation(
        data_url="data:image/jpeg;base64,secret",
        width=800,
        height=600,
        captured_at="2026-05-29T20:00:00+08:00",
        screen_name="DISPLAY1",
    )

    history_text = append_observation_marker("看看屏幕", observation)

    assert SCREEN_OBSERVATION_HISTORY_MARKER in history_text
    assert "data:image/jpeg;base64" not in history_text
    assert "secret" not in history_text


def test_screen_observation_trigger_requires_explicit_text() -> None:
    assert should_observe_screen("帮我看这个界面哪里不对")
    assert should_observe_screen("看看当前画面")
    assert not should_observe_screen("今天聊点什么")


def test_vision_unsupported_error_gets_local_fallback_reply() -> None:
    class VisionUnsupportedClient:
        def complete_raw(self, *_args, **_kwargs) -> str:  # type: ignore[no-untyped-def]
            raise ApiRequestError("model does not support image_url content")

    observation = ScreenObservation(
        data_url="data:image/jpeg;base64,abc123",
        width=1280,
        height=720,
        captured_at="2026-05-29T20:00:00+08:00",
        screen_name="DISPLAY1",
    )
    runtime = AgentRuntime(
        api_client=VisionUnsupportedClient(),  # type: ignore[arg-type]
        system_prompt="你是 Sakura。",
        tools=ToolRegistry(),
    )

    result = runtime.handle_user_message([build_screen_observation_user_message("看看屏幕", observation)])

    assert "不支持图片输入" in result.reply.translation
    assert not result.actions


def test_plain_text_messages_do_not_contain_image() -> None:
    assert not messages_contain_image([{"role": "user", "content": "普通聊天"}])
    assert is_vision_unsupported_error("This model does not support image input")


def _runtime_json_path(name: str) -> Path:
    return _runtime_root_path(name) / f"{name}.json"


def _runtime_root_path(name: str) -> Path:
    return Path(__file__).resolve().parents[1] / "__pycache__" / "test_runtime" / name / uuid.uuid4().hex


class _FakeBrowserExecutor:
    def execute_browser_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        if name == "browser_open_url":
            return {
                "url": arguments["url"],
                "title": "Example Domain",
                "opened": True,
                "loaded": True,
            }
        if name == "browser_get_content":
            return {
                "url": "https://example.com",
                "title": "Example Domain",
                "text": "abcdefghijklmnopqrstuvwxyz",
                "links": [
                    {"text": f"Link {index}", "href": f"https://example.com/{index}"}
                    for index in range(25)
                ],
            }
        if name == "browser_scroll":
            amount = int(arguments.get("amount", 800))
            direction = str(arguments.get("direction", "down"))
            return {
                "url": "https://example.com",
                "title": "Example Domain",
                "scroll_y": -amount if direction == "up" else amount,
            }
        if name == "browser_click":
            return {
                "ok": True,
                "url": "https://example.com",
                "title": "Example Domain",
                "selector": arguments["selector"],
            }
        if name == "browser_get_state":
            return {
                "url": "https://example.com",
                "title": "Example Domain",
                "scroll_y": 0,
                "loading": False,
            }
        raise ValueError(name)


def _write_mcp_config(root: Path, server_body: str) -> Path:
    config_path = root / "mcp.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    indented_body = "\n".join(f"    {line}" if line else "" for line in server_body.splitlines())
    config_path.write_text(
        f"""
enabled: true
servers:
  demo:
    transport: stdio
    command: python
{indented_body}
""".strip(),
        encoding="utf-8",
    )
    return config_path


class _FakeMCPBridge:
    def __init__(self, _server_config, _default_call_timeout) -> None:  # type: ignore[no-untyped-def]
        self.closed = False

    def connect(self) -> None:
        return None

    def list_tools(self) -> list[MCPToolSpec]:
        return [
            MCPToolSpec(
                name="echo",
                description="回显消息。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "message": {"type": "string"},
                    },
                    "required": ["message"],
                },
            )
        ]

    def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        return {
            "server_tool_name": name,
            "arguments": arguments,
        }

    def close(self) -> None:
        self.closed = True


class _FailingMCPBridge(_FakeMCPBridge):
    def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        _ = name, arguments
        raise RuntimeError("MCP 调用失败")
