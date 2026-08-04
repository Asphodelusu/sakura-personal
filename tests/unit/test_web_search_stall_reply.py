from __future__ import annotations

from app.agent.runtime import (
    _build_web_search_evidence_packet_message,
    _working_messages_have_web_search_evidence,
)
from app.agent.tools import ToolExecutionResult


def test_working_messages_detect_web_search_evidence() -> None:
    messages = [
        {"role": "user", "content": "搜一下G弦上的魔王"},
        {
            "role": "tool",
            "name": "web__web_search",
            "content": '{"digest":"' + ("简介内容" * 40) + '","results":[{"title":"x"}]}',
        },
    ]
    assert _working_messages_have_web_search_evidence(messages)


def test_web_search_evidence_packet_marks_lookup_completed() -> None:
    results = [
        ToolExecutionResult(
            tool_name="web__web_search",
            success=True,
            content={
                "digest": "《G弦上的魔王》是AKABEiSOFT2于2008年发售的成人向冒险游戏，宣传语是赌上性命的纯爱。",
                "results": [{"title": "百科", "snippet": "简介"}],
            },
            error="",
        )
    ]
    packet = _build_web_search_evidence_packet_message(results)
    assert packet is not None
    content = str(packet.get("content") or "")
    assert content.startswith("【联网证据】")
    assert "已经完成" in content
    assert "G弦上的魔王" in content
    assert _working_messages_have_web_search_evidence([packet])
