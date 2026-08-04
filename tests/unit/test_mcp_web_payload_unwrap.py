from __future__ import annotations

import json

from app.agent.runtime import _redact_tool_result_for_model
from app.agent.tool_routing import (
    _search_has_rich_snippets,
    _select_urls_for_auto_fetch,
    _successful_web_searches,
    unwrap_mcp_tool_payload,
)
from app.agent.tools import ToolExecutionResult


def _mcp_envelope(payload: dict) -> dict:
    raw = json.dumps(payload, ensure_ascii=False)
    return {
        "content": [{"type": "text", "text": raw}],
        "text": raw,
        "structured_content": payload,
        "is_error": False,
    }


def test_unwrap_mcp_tool_payload_prefers_structured_content() -> None:
    payload = {
        "query": "无主之地",
        "source": "Zhipu (search_pro)",
        "digest": "《无主之地》是 Gearbox 的射击游戏。" * 10,
        "results": [{"title": "无主之地简介", "url": "https://example.com/a", "snippet": "射击"}],
        "snippet_chars": 120,
    }
    assert unwrap_mcp_tool_payload(_mcp_envelope(payload))["query"] == "无主之地"


def test_redact_web_search_keeps_digest_from_mcp_envelope() -> None:
    payload = {
        "query": "无主之地",
        "source": "Zhipu (search_pro)",
        "digest": "Borderlands / 无主之地系列介绍。" * 20,
        "results": [
            {
                "title": "无主之地（Gearbox）",
                "url": "https://example.com/bl",
                "snippet": "第一人称射击" * 20,
            }
        ],
        "snippet_chars": 400,
    }
    result = ToolExecutionResult(
        tool_name="web__web_search",
        success=True,
        content=_mcp_envelope(payload),
        error="",
    )
    redacted = _redact_tool_result_for_model(result)["content"]
    assert redacted["results"]
    assert "无主之地" in redacted["digest"]
    assert redacted["source"] == "Zhipu (search_pro)"


def test_successful_web_search_and_rich_snippets_read_mcp_envelope() -> None:
    rich = "《无主之地》Gearbox 射击游戏，有刷宝与幽默风格。" * 40
    query = "搜一下《无主之地》是什么"
    result = ToolExecutionResult(
        tool_name="web__web_search",
        success=True,
        content=_mcp_envelope(
            {
                "query": query,
                "digest": rich,
                "snippet_chars": len(rich),
                "results": [
                    {
                        "title": "无主之地介绍",
                        "url": "https://zh.wikipedia.org/wiki/x",
                        "snippet": rich,
                    }
                ],
            }
        ),
        error="",
    )
    assert _successful_web_searches([result])
    assert _search_has_rich_snippets([result], query=query)
    assert _select_urls_for_auto_fetch([result], max_urls=1, query=query)
