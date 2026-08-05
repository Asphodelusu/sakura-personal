"""联网证据提取 — 从 runtime.py 拆分的叶子模块。

纯函数、无实例状态；负责判断搜索是否成功、抽取确定性证据正文，
并注入「检索已结束 + 证据」的最终总结提示消息。
"""

from __future__ import annotations

import json
from typing import Any

import app.agent.tool_routing as tool_routing
from app.agent.tools import ToolExecutionResult
from app.llm.api_client import ChatMessage


_WEB_SEARCH_TOOL_NAMES = frozenset({"web__web_search", "web_search"})


def _turn_had_successful_web_search(results: list[ToolExecutionResult]) -> bool:
    return bool(tool_routing._successful_web_searches(results))


def _working_messages_have_web_search_evidence(messages: list[ChatMessage]) -> bool:
    for message in messages:
        content = str(message.get("content") or "")
        if message.get("role") == "user" and content.startswith("【联网证据】"):
            return True
        if message.get("role") != "tool":
            continue
        name = str(message.get("name") or "")
        if "web_search" not in name:
            continue
        raw = message.get("content")
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        if "skipped" in text[:240] and "digest" not in text[:800]:
            continue
        if len(text) >= 80:
            return True
    return False


def _extract_web_lookup_evidence_text(results: list[ToolExecutionResult]) -> str:
    """从本轮搜索/读页结果抽出给模型看的确定性证据正文。"""
    chunks: list[str] = []
    for result in results:
        if not result.success:
            continue
        content = tool_routing.web_tool_payload(result)
        if result.tool_name in _WEB_SEARCH_TOOL_NAMES:
            digest = str(content.get("digest") or "").strip()
            if digest:
                chunks.append(digest[:1800])
                continue
            rows = content.get("results")
            if isinstance(rows, list):
                lines: list[str] = []
                for item in rows[:4]:
                    if not isinstance(item, dict):
                        continue
                    title = str(item.get("title") or "").strip()
                    snippet = str(item.get("snippet") or "").strip()
                    piece = "：".join(part for part in (title, snippet) if part)
                    if piece:
                        lines.append(piece)
                if lines:
                    chunks.append("\n".join(lines)[:1800])
            continue
        if result.tool_name in {"web__fetch_url", "fetch_url", "playwright_get_text"}:
            title = str(content.get("title") or "").strip()
            text = str(content.get("text") or "").strip()
            if text:
                head = f"《{title}》\n{text}" if title else text
                chunks.append(head[:1600])
    return "\n\n----\n\n".join(chunks).strip()


def _build_web_search_evidence_packet_message(
    results: list[ToolExecutionResult],
) -> ChatMessage | None:
    """搜/读完成后注入一条明确的「已结束+证据」消息，供最终总结阅读。"""
    if not _turn_had_successful_web_search(results) and not any(
        result.success
        and result.tool_name in {"web__fetch_url", "fetch_url", "playwright_get_text"}
        for result in results
    ):
        return None
    evidence = _extract_web_lookup_evidence_text(results)
    if len(evidence) < 40:
        return None
    return {
        "role": "user",
        "content": (
            "【联网证据】检索/读页已经完成（不是还在查询）。"
            "请只根据下列证据回答我刚才的问题；不要再说稍等或正在查。\n\n"
            f"{evidence[:4000]}"
        ),
    }


def _latest_user_text(messages: list[ChatMessage]) -> str:
    """提取最近一条用户文本，作为分层记忆检索查询。"""

    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        return _message_text_content(message.get("content"))
    return ""


def _message_text_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts)
