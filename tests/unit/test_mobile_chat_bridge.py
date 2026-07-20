from __future__ import annotations

from app.agent.builtin_tools import create_mobile_tool_registry
from app.agent.memory import MemoryStore
from app.agent.session_state_context import build_session_state_fragment
from app.core.mobile_chat_bridge import (
    MOBILE_CHANNEL_PATCH_ID,
    MOBILE_CHANNEL_PROMPT_PATCH,
    MOBILE_CONTEXT_MARKER,
    MOBILE_HISTORY_CHANNEL,
    MobileChatBridge,
    _messages_from_history,
)
from app.plugins.models import PromptPatchContribution
from app.storage.chat_history import ChatHistoryEntry, ChatHistoryStore


class _RuntimeStub:
    def __init__(self, patches: list[PromptPatchContribution] | None = None) -> None:
        self.prompt_patches = list(patches or [])


class _HostStub:
    def __init__(self, patches: list[PromptPatchContribution] | None = None) -> None:
        self.agent_runtime = _RuntimeStub(patches)


def test_mobile_tool_registry_exposes_memory_write_not_desktop() -> None:
    class FakeMem0:
        def add(self, content, *, user_id, metadata, infer=False):
            return {
                "results": [
                    {
                        "id": "m1",
                        "content": content,
                        "memory": content,
                        "metadata": metadata,
                    }
                ]
            }

    registry = create_mobile_tool_registry(MemoryStore(memory_client=FakeMem0()))
    names = {tool.name for tool in registry.all()}
    assert "memory_remember" in names
    assert "memory_search" in names
    assert "memory_update" in names
    assert "memory_forget" in names
    assert "open_url" not in names
    assert "observe_screen" not in names
    result = registry.execute("memory_remember", {"content": "对方喜欢抹茶"})
    assert result.success
    assert result.content["memory"]["metadata"]["source"] == "explicit"


def test_mobile_channel_prompt_mentions_shared_memory() -> None:
    assert "memory_remember" in MOBILE_CHANNEL_PROMPT_PATCH.system_prompt_append
    assert "长期记忆与电脑端共用" in MOBILE_CHANNEL_PROMPT_PATCH.system_prompt_append


def test_mobile_prompt_patches_append_channel_notice() -> None:
    host_patch = PromptPatchContribution(patch_id="host_demo", system_prompt_append="桌面补丁")
    bridge = MobileChatBridge(_HostStub([host_patch]))

    patches = bridge._mobile_prompt_patches()

    assert [patch.patch_id for patch in patches] == ["host_demo", MOBILE_CHANNEL_PATCH_ID]
    assert patches[-1] is MOBILE_CHANNEL_PROMPT_PATCH
    assert "手机网页端" in patches[-1].system_prompt_append


def test_mobile_prompt_patches_do_not_duplicate_channel_notice() -> None:
    bridge = MobileChatBridge(
        _HostStub(
            [
                PromptPatchContribution(patch_id="host_demo", system_prompt_append="桌面补丁"),
                MOBILE_CHANNEL_PROMPT_PATCH,
            ]
        )
    )

    patches = bridge._mobile_prompt_patches()

    assert [patch.patch_id for patch in patches].count(MOBILE_CHANNEL_PATCH_ID) == 1


def test_mobile_history_channel_round_trips_and_marks_model_context(tmp_path) -> None:
    store = ChatHistoryStore(tmp_path / "history.jsonl")
    store.append("user", "刚才在路上看到一只猫。", channel=MOBILE_HISTORY_CHANNEL)
    store.append("assistant", "是怎样的猫？", channel=MOBILE_HISTORY_CHANNEL)

    entries = store.load()
    messages = _messages_from_history(entries)

    assert [entry.channel for entry in entries] == ["mobile", "mobile"]
    assert messages[0]["content"] == f"{MOBILE_CONTEXT_MARKER}\n刚才在路上看到一只猫。"
    assert messages[1]["content"] == "是怎样的猫？"


def test_desktop_session_context_preserves_mobile_provenance() -> None:
    entries = [
        ChatHistoryEntry(
            "2026-07-19T22:00:00+08:00",
            "user",
            "刚才在路上看到一只猫。",
            channel=MOBILE_HISTORY_CHANNEL,
        ),
        ChatHistoryEntry(
            "2026-07-19T22:00:01+08:00",
            "assistant",
            "是怎样的猫？",
            channel=MOBILE_HISTORY_CHANNEL,
        ),
    ]

    fragment = build_session_state_fragment(entries)

    assert fragment is not None
    assert "对方（手机）" in fragment.content
    assert "Sakura（当时通过手机回复）" in fragment.content
