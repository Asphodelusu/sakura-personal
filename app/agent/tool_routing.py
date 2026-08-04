"""app/agent/tool_routing.py — 浏览器/屏幕工具路由策略。

从 runtime.py 拆出的纯函数层：根据对话内容决定浏览器页面模式、
可见浏览器模式、Windows 控制与屏幕观察的工具过滤与提示词规则。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from app.agent.actions import PendingToolAction
from app.agent.screen_policy import ScreenPolicy
from app.agent.tool_policy import (
    BROWSER_SNAPSHOT_TOOL_NAME,
    PLAYWRIGHT_GET_TEXT_TOOL_NAME,
    PLAYWRIGHT_NAVIGATE_TOOL_NAME,
    ToolPolicy,
)
from app.agent.tools import ToolExecutionResult, ToolRegistry
from app.core.debug_log import debug_log
from app.llm.api_client import ChatMessage, NativeToolCall

DEFAULT_ACTIVE_TOOL_GROUPS: frozenset[str] = frozenset({"core"})

_KEYWORD_TOOL_GROUP_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "productivity",
        (
            "待办",
            "todo",
            "任务清单",
            "提醒",
            "reminder",
            "闹钟",
            "笔记",
            "note",
            "备忘",
        ),
    ),
    (
        "desktop",
        (
            "打开文件夹",
            "打开目录",
            "本地文件夹",
            "open folder",
            "打开网页",
            "打开链接",
            "open url",
            "http://",
            "https://",
        ),
    ),
    (
        "memory-write",
        (
            "记住",
            "记下",
            "别忘了",
            "忘记",
            "忘掉",
            "remember",
            "forget",
            "更新记忆",
        ),
    ),
    (
        "mcp",
        (
            "搜索",
            "搜一下",
            "查一下",
            "查下",
            "查查",
            "查询",
            "再查",
            "再搜",
            "查一遍",
            "再查一遍",
            "再搜一遍",
            "重新查",
            "重新搜",
            "联网",
            "网页",
            "百度",
            "谷歌",
            "google",
            "fetch",
            "新闻",
            "web search",
            # 查询意图的天气（勿加「天气」单字，避免「今天天气真好」误激活）
            "什么天气",
            "天气预报",
            "查天气",
            "看看天气",
            "几度",
            "气温",
            "会下雨",
            "下雨吗",
            "下不下雨",
            "weather",
        ),
    ),
)

_MCP_FOLLOWUP_MARKERS: tuple[str, ...] = (
    "再查",
    "再搜",
    "查一遍",
    "查一次",
    "再查一遍",
    "再搜一遍",
    "还是",
    "同样",
    "又来",
    "重新查",
    "重新搜",
    "再帮我查",
    "再帮我搜",
)

_MEMORY_REMEMBER_MARKERS: tuple[str, ...] = (
    "记住",
    "记下",
    "别忘了",
    "remember",
)

_MEMORY_FORGET_MARKERS: tuple[str, ...] = (
    "忘记",
    "忘掉",
    "forget",
    "别记住",
    "不想再提",
    "别提",
    "不要再记",
    "放手",
    "放了",
    "算了",
    "不说了",
)

_MEMORY_RECALL_MARKERS: tuple[str, ...] = (
    "刚才让记住",
    "刚才说",
    "让你记住",
    "还记得",
    "记不记得",
    "记得吗",
    "想起来",
    "刚才记",
    "之前说",
    "之前让",
)

_MEMORY_REMEMBER_CONTENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"记住[：:，,\s]+(.+)", re.IGNORECASE),
    re.compile(r"记下[：:，,\s]+(.+)", re.IGNORECASE),
    re.compile(r"别忘了[：:，,\s]+(.+)", re.IGNORECASE),
    re.compile(r"remember\s+(.+)", re.IGNORECASE),
)


def infer_active_tool_groups_from_messages(messages: list[ChatMessage]) -> set[str]:
    """根据用户话术预激活工具组，避免闲聊每轮携带全套工具 schema。"""
    groups = set(DEFAULT_ACTIVE_TOOL_GROUPS)
    text = (_latest_user_text(messages) or "").lower()
    if not text:
        return groups
    for group, keywords in _KEYWORD_TOOL_GROUP_RULES:
        if any(keyword.lower() in text for keyword in keywords):
            groups.add(group)
    if messages_contain_recent_web_search(messages) and user_requests_mcp_followup(text):
        groups.add("mcp")
    return groups


def messages_contain_recent_web_search(messages: list[ChatMessage]) -> bool:
    """对话里是否已出现过网页搜索/抓取工具调用。"""
    web_tools = frozenset({"web__web_search", "web__fetch_url", "web_search", "fetch_url"})
    for message in messages:
        if message.get("role") == "tool" and str(message.get("name", "")) in web_tools:
            return True
        if message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            name = ""
            if isinstance(function, dict):
                name = str(function.get("name", ""))
            if not name:
                name = str(call.get("name", ""))
            if name in web_tools:
                return True
    return False


def user_requests_mcp_followup(text: str) -> bool:
    """用户是否在要求重复/延续上一轮联网查询。"""
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    if any(marker.lower() in normalized for marker in _MCP_FOLLOWUP_MARKERS):
        return True
    return bool(re.search(r"[再又还].{0,6}[查搜]", normalized))


def user_message_needs_web_lookup(messages: list[ChatMessage]) -> bool:
    """用户本轮话术是否明显需要联网查询（天气/新闻/搜索等）。"""
    text = (_latest_user_text(messages) or "").strip().lower()
    if not text:
        return False
    for group, keywords in _KEYWORD_TOOL_GROUP_RULES:
        if group != "mcp":
            continue
        if any(keyword.lower() in text for keyword in keywords):
            return True
    return False


def user_requests_memory_remember(messages: list[ChatMessage]) -> bool:
    """用户是否明确要求写入长期记忆。"""
    if user_requests_memory_recall(messages):
        return False
    text = (_latest_user_text(messages) or "").strip().lower()
    if not text:
        return False
    if any(marker in text for marker in _MEMORY_FORGET_MARKERS):
        return False
    return any(marker in text for marker in _MEMORY_REMEMBER_MARKERS)


def user_requests_memory_recall(messages: list[ChatMessage]) -> bool:
    """用户是否在追问已写入或应写入的长期记忆。"""
    text = (_latest_user_text(messages) or "").strip().lower()
    if not text:
        return False
    if any(marker in text for marker in _MEMORY_RECALL_MARKERS):
        return True
    if re.search(r"(刚才|之前).{0,8}(记住|说|告诉)", text):
        return True
    if re.search(r"(记得|想起来).{0,4}(吗|么|不)", text):
        return True
    if "让你记住" in text and any(token in text for token in ("什么", "啥", "吗", "?", "？")):
        return True
    return False


def extract_memory_remember_content(messages: list[ChatMessage]) -> str | None:
    """从用户话术提取应写入长期记忆的正文。"""
    text = (_latest_user_text(messages) or "").strip()
    if not text:
        return None
    for pattern in _MEMORY_REMEMBER_CONTENT_PATTERNS:
        match = pattern.search(text)
        if match:
            content = match.group(1).strip()
            if content:
                return content
    if user_requests_memory_remember(messages):
        return text
    return None


def extract_memory_recall_query(messages: list[ChatMessage]) -> str:
    """为 memory_search 生成查询文本。"""
    text = (_latest_user_text(messages) or "").strip()
    return text or "用户偏好"


def _filter_tools_for_browser_routing(
    tools: list[dict[str, Any]],
    *,
    browser_page_mode: bool,
    visible_browser_mode: bool,
) -> list[dict[str, Any]]:
    return ToolPolicy.filter_tools_for_browser_routing(
        tools,
        browser_page_mode=browser_page_mode,
        visible_browser_mode=visible_browser_mode,
    )


def _filter_openai_tools_for_browser_routing(
    tools: list[dict[str, Any]],
    *,
    browser_page_mode: bool,
    visible_browser_mode: bool,
) -> list[dict[str, Any]]:
    if not browser_page_mode and not visible_browser_mode:
        return tools
    filtered_names = {
        str(item.get("name", ""))
        for item in _filter_tools_for_browser_routing(
            [
                {"name": tool.get("function", {}).get("name")}
                for tool in tools
                if isinstance(tool.get("function"), dict)
            ],
            browser_page_mode=browser_page_mode,
            visible_browser_mode=visible_browser_mode,
        )
    }
    return [
        tool
        for tool in tools
        if isinstance(tool.get("function"), dict)
        and str(tool["function"].get("name", "")) in filtered_names
    ]


def _should_block_windows_tool_for_browser_page(
    call: dict[str, Any],
    browser_page_mode: bool,
) -> bool:
    return ToolPolicy.should_block_windows_tool_for_browser_page(call, browser_page_mode)


def _should_block_background_web_tool_for_visible_browser(
    call: dict[str, Any],
    visible_browser_mode: bool,
) -> bool:
    return ToolPolicy.should_block_background_web_tool_for_visible_browser(
        call,
        visible_browser_mode,
    )


def _should_auto_snapshot_after_browser_navigation(
    tool_calls: list[dict[str, Any]],
    step_results: list[ToolExecutionResult],
    tools: ToolRegistry,
) -> bool:
    return ToolPolicy.should_auto_snapshot_after_browser_navigation(
        tool_calls,
        step_results,
        tools,
    )


def _execute_auto_browser_snapshot(tools: ToolRegistry, step_index: int) -> ToolExecutionResult:
    arguments: dict[str, Any] = {}
    reason = "浏览器导航成功后自动读取页面内容，减少模型往返。"
    debug_log(
        "AgentRuntime",
        "自动补充浏览器页面文本",
        {
            "step_index": step_index,
            "name": BROWSER_SNAPSHOT_TOOL_NAME,
            "arguments": arguments,
            "reason": reason,
        },
    )
    prepared = tools.prepare_or_execute(BROWSER_SNAPSHOT_TOOL_NAME, arguments, reason)
    if isinstance(prepared, PendingToolAction):
        result = ToolExecutionResult(
            tool_name="runtime",
            success=False,
            content={
                "auto_tool": BROWSER_SNAPSHOT_TOOL_NAME,
                "reason": "自动页面文本读取需要对方确认，已跳过隐藏执行。",
            },
            error="自动页面文本读取需要对方确认，已跳过。",
        )
        debug_log("AgentRuntime", "自动浏览器页面文本读取需要确认，已跳过", result.to_dict())
        return result

    # 延迟 import：脱敏函数属于 runtime 的模型消息构建层，模块级互引会成环
    from app.agent.runtime import _redact_tool_result_for_model

    debug_log("AgentRuntime", "自动浏览器页面文本读取完成", _redact_tool_result_for_model(prepared))
    return prepared


def _should_fast_forward_after_auto_browser_snapshot(
    messages: list[ChatMessage],
    snapshot_result: ToolExecutionResult,
) -> bool:
    if not _latest_user_is_browser_lookup_request(messages):
        return False
    if _latest_user_is_browser_interaction_request(messages):
        return False
    return _browser_snapshot_has_readable_content(snapshot_result)


def _latest_user_is_browser_lookup_request(messages: list[ChatMessage]) -> bool:
    text = (_latest_user_text(messages) or "").lower()
    if not text:
        return False
    lookup_keywords = (
        "搜索",
        "搜一下",
        "搜一搜",
        "查",
        "查询",
        "看看",
        "看一下",
        "百科",
        "信息",
        "资料",
        "介绍",
        "告诉我",
        "说明",
        "内容",
        "总结",
        "梳理",
        "是谁",
        "是什么",
        "検索",
        "調べ",
        "情報",
        "教えて",
        "紹介",
        "search",
        "look up",
        "lookup",
        "information",
        "info",
        "tell me",
        "wiki",
        "wikipedia",
        "summary",
        "summarize",
    )
    return any(keyword in text for keyword in lookup_keywords)


_WEB_SEARCH_TOOL_NAMES = frozenset(
    {"web__web_search", "web_search", "web__fetch_url", "fetch_url"}
)
_WEB_SEARCH_ONLY_TOOL_NAMES = frozenset({"web__web_search", "web_search"})
_WEB_FETCH_TOOL_NAMES = frozenset(
    {
        "web__fetch_url",
        "fetch_url",
        PLAYWRIGHT_GET_TEXT_TOOL_NAME,
        "playwright_navigate",
    }
)
_MEMORY_SEARCH_TOOL_NAMES = frozenset({"memory_search"})
_MEMORY_DETAIL_TOOL_NAMES = frozenset({"memory_detail"})

# 深度资料题：仅靠搜索摘要通常不够，需要再读正文。
_DEEP_WEB_LOOKUP_MARKERS = (
    "都讲了什么",
    "讲了什么",
    "讲什么",
    "说了什么",
    "什么故事",
    "剧情",
    "内容",
    "详细",
    "深入",
    "介绍",
    "是什么",
    "是谁",
    "百科",
    "梗概",
    "评价",
    "攻略",
    "设定",
)
# 实时/短答查询：搜一次摘要即可，不必强行读页。
_SHALLOW_WEB_LOOKUP_MARKERS = (
    "天气",
    "气温",
    "几点",
    "股价",
    "汇率",
    "比分",
    "开奖",
    "限行",
    "油价",
)

_PREFERRED_FETCH_HOST_HINTS = (
    "wikipedia",
    "baike.baidu",
    "moegirl",
    "zhihu.com",
    # bilibili 专栏/动态链经常失效（「页面不存在」），不作为优先读页来源
    "bangumi.tv",
    "douban.com",
    "fandom.com",
    "gamersky",
    "3dm",
    "indienova",
    "steamcommunity",
    "store.steampowered",
    "itch.io",
)
_ERROR_PAGE_MARKERS = (
    "页面不存在",
    "頁面不存在",
    "内容不存在",
    "该页面不存在",
    "你访问的页面不存在",
    "访问的页面不存在",
    "啥都没有",
    "呜呼，出错",
    "呜呼~出错",
    "出错啦",
    "404 not found",
    "404错误",
    "page not found",
)
_AVOID_FETCH_HOST_HINTS = (
    "weibo.com",
    "twitter.com",
    "x.com",
    "facebook.com",
    "instagram.com",
    "tiktok.com",
    "cndzys.com",
    "dxy.cn",
    "xywy.com",
    "39.net",
    "docin.com",
)
_AVOID_FETCH_PATH_HINTS = (
    "/video/",
    "/BV",
)


def _latest_user_is_deep_web_lookup(messages: list[ChatMessage]) -> bool:
    """作品介绍/剧情/设定等需要正文证据的查询。"""
    if not _latest_user_is_browser_lookup_request(messages):
        return False
    text = (_latest_user_text(messages) or "").lower()
    if any(marker in text for marker in _SHALLOW_WEB_LOOKUP_MARKERS):
        return False
    return any(marker in text for marker in _DEEP_WEB_LOOKUP_MARKERS)


def unwrap_mcp_tool_payload(content: Any) -> Any:
    """解开 MCP bridge 外壳，取出 web_search/fetch_url 的真实 payload。

    bridge 返回形如 {content:[...], text:"{...json...}", structured_content:{...}, is_error:bool}，
    若直接按顶层 results/digest 读取会得到空结果，模型就会说「搜索结果为空」。
    """
    if not isinstance(content, dict):
        return content

    # 已是业务 payload
    if isinstance(content.get("results"), list):
        return content
    if (
        "url" in content
        and ("text" in content or "title" in content)
        and "is_error" not in content
        and "structured_content" not in content
    ):
        return content

    structured = content.get("structured_content")
    if isinstance(structured, dict):
        if isinstance(structured.get("results"), list) or "url" in structured or "digest" in structured:
            return structured

    candidates: list[str] = []
    text = content.get("text")
    if isinstance(text, str) and text.strip():
        candidates.append(text.strip())
    items = content.get("content")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                raw = item["text"].strip()
                if raw:
                    candidates.append(raw)

    for raw in candidates:
        if not raw.startswith("{") and not raw.startswith("["):
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return content


def web_tool_payload(result: ToolExecutionResult) -> dict[str, Any]:
    """读取工具结果中的业务字典（自动解开 MCP 外壳）。"""
    content = unwrap_mcp_tool_payload(result.content)
    return content if isinstance(content, dict) else {}


def _mcp_tool_content_is_error(content: Any) -> bool:
    return isinstance(content, dict) and bool(content.get("is_error"))


def _web_search_payload_has_hits(payload: dict[str, Any]) -> bool:
    rows = payload.get("results")
    if isinstance(rows, list) and any(isinstance(item, dict) and item.get("title") for item in rows):
        return True
    digest = str(payload.get("digest") or "").strip()
    return len(digest) >= 40


def _successful_web_searches(execution_results: list[ToolExecutionResult]) -> list[ToolExecutionResult]:
    kept: list[ToolExecutionResult] = []
    for result in execution_results:
        if result.tool_name not in _WEB_SEARCH_ONLY_TOOL_NAMES or not result.success:
            continue
        if not isinstance(result.content, dict):
            continue
        if result.content.get("skipped") or _mcp_tool_content_is_error(result.content):
            continue
        payload = web_tool_payload(result)
        if _web_search_payload_has_hits(payload):
            kept.append(result)
    return kept


def _successful_web_fetches(execution_results: list[ToolExecutionResult]) -> list[ToolExecutionResult]:
    return [
        result
        for result in execution_results
        if result.tool_name in _WEB_FETCH_TOOL_NAMES
        and result.success
        and not (isinstance(result.content, dict) and result.content.get("skipped"))
        and not _mcp_tool_content_is_error(result.content)
        and _web_fetch_has_readable_content(result)
    ]


def _web_fetch_has_readable_content(result: ToolExecutionResult) -> bool:
    content = result.content
    if isinstance(content, str):
        return _text_looks_like_readable_page(content)
    payload = web_tool_payload(result) if isinstance(content, dict) else {}
    text = str(payload.get("text") or "").strip()
    title = str(payload.get("title") or "").strip()
    return _text_looks_like_readable_page(text, title=title)


def _text_looks_like_error_page(text: str, *, title: str = "") -> bool:
    """识别 404 /「页面不存在」等错误壳页（常见于失效 bilibili 专栏）。"""
    blob = f"{title}\n{text or ''}".strip().lower()
    if not blob:
        return True
    return any(marker.lower() in blob for marker in _ERROR_PAGE_MARKERS)


def _text_looks_like_readable_page(text: str, *, title: str = "") -> bool:
    """过滤二进制垃圾页、空壳页、错误壳页；要求有足够中文/字母正文。"""
    cleaned = (text or "").strip()
    if len(cleaned) < 80:
        return False
    if _text_looks_like_error_page(cleaned, title=title):
        return False
    sample = cleaned[:800]
    control = sum(1 for ch in sample if ord(ch) < 32 and ch not in "\n\t\r")
    if control >= 8:
        return False
    cjk = sum(1 for ch in sample if "\u4e00" <= ch <= "\u9fff")
    alpha = sum(1 for ch in sample if ch.isalpha())
    return cjk >= 24 or (cjk + alpha) >= 120


def _query_relevance_terms(text: str) -> list[str]:
    raw = (text or "").strip()
    terms: list[str] = []
    for match in re.findall(r"《([^》]{2,40})》", raw):
        terms.append(match.strip())
    for match in re.findall(r"[A-Za-z][A-Za-z0-9:_\-]{2,40}", raw):
        terms.append(match)
    for match in re.findall(r"[\u4e00-\u9fff]{2,12}", raw):
        if match in {"是什么", "都讲了", "讲了什么", "搜一下", "介绍一下", "什么", "介绍和攻略"}:
            continue
        terms.append(match)
        # 长串常把「作品名+介绍攻略」粘在一起；截前缀作锚点。
        if len(match) >= 6:
            terms.append(match[:6])
            terms.append(match[:4])
    # 去重保序
    seen: set[str] = set()
    ordered: list[str] = []
    for term in terms:
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(term)
    return ordered[:12]


def _search_snippet_chars(search_results: list[ToolExecutionResult]) -> int:
    total = 0
    for result in search_results:
        content = web_tool_payload(result)
        if isinstance(content.get("snippet_chars"), int):
            total += max(0, int(content["snippet_chars"]))
            continue
        digest = str(content.get("digest") or "")
        if digest:
            total += len(digest)
            continue
        rows = content.get("results")
        if isinstance(rows, list):
            for item in rows:
                if isinstance(item, dict):
                    total += len(str(item.get("snippet") or ""))
    return total


def _search_has_rich_snippets(
    search_results: list[ToolExecutionResult],
    *,
    query: str = "",
) -> bool:
    """对题的长摘要已够综合时，不必再赌本地读页。

    注意：跑偏的长文（如医学页）绝不能算 rich，否则会带着错误证据直接收束。
    """
    if not query or not _search_results_look_on_topic(search_results, query):
        return False
    return _search_snippet_chars(search_results) >= 800


def _should_fast_forward_after_web_search(
    messages: list[ChatMessage],
    execution_results: list[ToolExecutionResult],
) -> bool:
    """信息查询在证据足够后应收束工具循环，进入最终总结。

    简单题：一次成功搜索即可。
    深度题：优先吃检索长摘要；摘要够厚就收束，读页只是补强。
    """
    if not _latest_user_is_browser_lookup_request(messages):
        return False
    if _latest_user_is_browser_interaction_request(messages):
        return False

    searches = _successful_web_searches(execution_results)
    fetches = _successful_web_fetches(execution_results)
    if _latest_user_is_deep_web_lookup(messages):
        query = _latest_user_text(messages) or ""
        if fetches:
            return True
        if searches and _search_has_rich_snippets(searches, query=query):
            return True
        if searches and not _select_urls_for_auto_fetch(searches, max_urls=1, query=query):
            # 搜到了但没有可抓的公开页，别空转。
            return True
        if searches and _has_attempted_auto_web_fetch(execution_results):
            # 已自动读过页但正文不可用：用现有摘要收束，避免模型换词连搜。
            return True
        return False

    return bool(searches or fetches)


def _has_attempted_auto_web_fetch(execution_results: list[ToolExecutionResult]) -> bool:
    return any(
        result.tool_name in _WEB_FETCH_TOOL_NAMES
        and isinstance(result.content, dict)
        and result.content.get("auto_fetched")
        for result in execution_results
    )


def _should_auto_fetch_after_web_search(
    messages: list[ChatMessage],
    step_results: list[ToolExecutionResult],
    execution_results: list[ToolExecutionResult],
) -> bool:
    """深度查询且摘要偏薄时，串行打开候选页（边讲边预取下一页）。"""
    if not _latest_user_is_deep_web_lookup(messages):
        return False
    if _latest_user_is_browser_interaction_request(messages):
        return False
    if _successful_web_fetches(execution_results):
        return False
    step_searches = _successful_web_searches(step_results)
    if not step_searches:
        return False
    query = _latest_user_text(messages) or ""
    if _search_has_rich_snippets(step_searches, query=query):
        return False
    return bool(_select_urls_for_auto_fetch(step_searches, max_urls=3, query=query))


def _fetch_page_evidence_chars(result: ToolExecutionResult) -> int:
    if not result.success:
        return 0
    if isinstance(result.content, str):
        return len(result.content.strip())
    if not isinstance(result.content, dict):
        return 0
    payload = web_tool_payload(result)
    return len(str(payload.get("text") or "").strip())


def _auto_fetch_evidence_enough(
    kept: list[ToolExecutionResult],
    *,
    enough_chars: int = 1600,
    max_keep: int = 3,
) -> bool:
    """已读正文是否足够进入综合（够用即停，不再开后续页）。"""
    if len(kept) >= max(1, max_keep):
        return True
    total = sum(_fetch_page_evidence_chars(item) for item in kept)
    return total >= max(400, enough_chars)


def build_refined_web_search_query(messages: list[ChatMessage]) -> str:
    """首轮搜源太噪时，换更贴作品页的关键词。"""
    text = (_latest_user_text(messages) or "").strip()
    titled = re.findall(r"《([^》]{2,40})》", text)
    if titled:
        core = titled[0].strip()
    else:
        terms = _query_relevance_terms(text)
        core = " ".join(terms[:4]).strip()
    if not core:
        return ""
    # 与 web_search_server 内兜底互补：这里给工具循环第二次机会。
    if re.search(r"brain", core, flags=re.I) and "恐怖脑" in core:
        return "电気人_denki brain"
    return f"「{core}」"[:70]


def _search_results_look_on_topic(
    search_results: list[ToolExecutionResult],
    query: str,
) -> bool:
    """标题/摘要已明显命中作品名时，不必为了读页再换词重搜。"""
    terms = _query_relevance_terms(query)
    if not terms:
        return False
    distinctive = [t for t in terms if len(t) >= 3][:8]
    if not distinctive:
        return False
    for result in search_results:
        content = web_tool_payload(result)
        digest = str(content.get("digest") or "")
        if digest:
            blob = digest.lower()
            hits = sum(1 for term in distinctive if term.lower() in blob)
            if hits >= 2 or (hits >= 1 and any(len(term) >= 4 and term.lower() in blob for term in distinctive)):
                return True
        rows = content.get("results")
        if not isinstance(rows, list):
            continue
        for item in rows[:5]:
            if not isinstance(item, dict):
                continue
            blob = f"{item.get('title') or ''}\n{item.get('snippet') or ''}".lower()
            hits = sum(1 for term in distinctive if term.lower() in blob)
            if hits >= 2 or (hits >= 1 and any(len(term) >= 4 and term.lower() in blob for term in distinctive)):
                return True
    return False


def _should_refine_web_search(
    messages: list[ChatMessage],
    execution_results: list[ToolExecutionResult],
) -> bool:
    """默认关闭自动换词重搜。

    智谱 Web Search 按「请求次数」计费：一次 HTTP=扣 1 次，与返回条数无关。
    官方推荐单次 search_pro + 更大 count/长摘要；自动 refine 会成倍烧资源包。
    """
    _ = messages, execution_results
    return False


def _select_urls_for_auto_fetch(
    search_results: list[ToolExecutionResult],
    *,
    max_urls: int = 2,
    query: str = "",
) -> list[str]:
    """从搜索结果里挑更适合读正文的链接（百科/资料站优先，社交噪音靠后）。"""
    scored: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    order = 0
    query_terms = _query_relevance_terms(query)
    for result in search_results:
        content = web_tool_payload(result)
        if not content:
            continue
        if not query_terms:
            query_terms = _query_relevance_terms(str(content.get("query") or ""))
        rows = content.get("results")
        if not isinstance(rows, list):
            continue
        for item in rows:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url or url in seen:
                continue
            if not (url.startswith("http://") or url.startswith("https://")):
                continue
            seen.add(url)
            host = url.lower()
            if any(hint in host for hint in _AVOID_FETCH_PATH_HINTS):
                continue
            score = 0
            if any(hint in host for hint in _PREFERRED_FETCH_HOST_HINTS):
                score += 5
            # B 站专栏/动态易失效；视频本来就 avoid。非百科类 bilibili 链接略降权。
            if "bilibili.com" in host:
                score -= 2
            if any(hint in host for hint in _AVOID_FETCH_HOST_HINTS):
                score -= 6
            title = str(item.get("title") or "")
            snippet = str(item.get("snippet") or "")
            blob = f"{title}\n{snippet}"
            if any(token in blob for token in ("百科", "维基", "wiki", "剧情", "简介", "攻略", "游戏")):
                score += 2
            if query_terms:
                hits = sum(1 for term in query_terms if term.lower() in blob.lower())
                score += min(6, hits * 2)
                if hits == 0:
                    score -= 4
            scored.append((score, order, url))
            order += 1
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [url for score, _order, url in scored if score >= 0][: max(0, max_urls)]


def _playwright_reader_available(tools: ToolRegistry | None) -> bool:
    if tools is None:
        return False
    return (
        tools.get(PLAYWRIGHT_NAVIGATE_TOOL_NAME) is not None
        and tools.get(PLAYWRIGHT_GET_TEXT_TOOL_NAME) is not None
    )


def _read_url_via_playwright(
    tools: ToolRegistry,
    url: str,
    *,
    max_chars: int = 4000,
) -> ToolExecutionResult:
    """用 playwright_browser 插件打开页面并读正文（跳过确认，供自动查阅）。"""
    try:
        nav = tools.execute(PLAYWRIGHT_NAVIGATE_TOOL_NAME, {"url": url})
    except Exception as exc:
        return ToolExecutionResult(
            tool_name=PLAYWRIGHT_GET_TEXT_TOOL_NAME,
            success=False,
            content={"url": url, "auto_fetched": True, "reader": "playwright"},
            error=str(exc),
        )
    if not nav.success:
        return ToolExecutionResult(
            tool_name=PLAYWRIGHT_GET_TEXT_TOOL_NAME,
            success=False,
            content={"url": url, "auto_fetched": True, "reader": "playwright"},
            error=nav.error or "navigate_failed",
        )

    title = ""
    final_url = url
    if isinstance(nav.content, dict):
        title = str(nav.content.get("title") or "").strip()
        final_url = str(nav.content.get("url") or url).strip() or url

    try:
        text_result = tools.execute(PLAYWRIGHT_GET_TEXT_TOOL_NAME, {"selector": "body"})
    except Exception as exc:
        return ToolExecutionResult(
            tool_name=PLAYWRIGHT_GET_TEXT_TOOL_NAME,
            success=False,
            content={"url": final_url, "title": title, "auto_fetched": True, "reader": "playwright"},
            error=str(exc),
        )
    if not text_result.success:
        return ToolExecutionResult(
            tool_name=PLAYWRIGHT_GET_TEXT_TOOL_NAME,
            success=False,
            content={"url": final_url, "title": title, "auto_fetched": True, "reader": "playwright"},
            error=text_result.error or "get_text_failed",
        )

    raw = text_result.content
    if isinstance(raw, dict):
        text = str(raw.get("text") or raw.get("result") or "")
    else:
        text = str(raw or "")
    text = re.sub(r"\s+", " ", text).strip()[:max_chars]
    content = {
        "url": final_url,
        "title": title,
        "text": text,
        "auto_fetched": True,
        "reader": "playwright",
    }
    if not _text_looks_like_readable_page(text, title=title):
        # 失效页不要留在可见浏览器里；清到空白页，避免用户看到两个 Error。
        try:
            tools.execute(PLAYWRIGHT_NAVIGATE_TOOL_NAME, {"url": "about:blank"})
        except Exception:
            pass
        return ToolExecutionResult(
            tool_name=PLAYWRIGHT_GET_TEXT_TOOL_NAME,
            success=False,
            content=content,
            error="unreadable_page_content",
        )
    return ToolExecutionResult(
        tool_name=PLAYWRIGHT_GET_TEXT_TOOL_NAME,
        success=True,
        content=content,
        error="",
    )


def _read_url_via_fetch(url: str, *, max_chars: int = 4000) -> ToolExecutionResult:
    from app.agent.mcp import web_search_server as web_mod

    try:
        payload = web_mod.fetch_url(url, max_chars=max_chars)
        content = payload if isinstance(payload, dict) else {"text": str(payload)}
        text = str(content.get("text") or "")
        title = str(content.get("title") or "")
        merged = {**content, "url": content.get("url") or url, "auto_fetched": True, "reader": "fetch_url"}
        if not _text_looks_like_readable_page(text, title=title):
            return ToolExecutionResult(
                tool_name="web__fetch_url",
                success=False,
                content=merged,
                error="unreadable_page_content",
            )
        return ToolExecutionResult(
            tool_name="web__fetch_url",
            success=True,
            content=merged,
            error="",
        )
    except Exception as exc:
        return ToolExecutionResult(
            tool_name="web__fetch_url",
            success=False,
            content={"url": url, "auto_fetched": True, "reader": "fetch_url"},
            error=str(exc),
        )


def _read_url_for_deep_lookup(
    url: str,
    *,
    tools: ToolRegistry | None,
    max_chars: int = 4000,
    prefer_playwright: bool = True,
) -> ToolExecutionResult:
    """优先插件浏览器读页；失败再退回 fetch_url。"""
    if prefer_playwright and _playwright_reader_available(tools):
        assert tools is not None
        result = _read_url_via_playwright(tools, url, max_chars=max_chars)
        if result.success and _web_fetch_has_readable_content(result):
            return result
        fallback = _read_url_via_fetch(url, max_chars=max_chars)
        if fallback.success and _web_fetch_has_readable_content(fallback):
            if isinstance(fallback.content, dict):
                fallback.content["playwright_error"] = result.error or "unreadable"
            return fallback
        return result
    return _read_url_via_fetch(url, max_chars=max_chars)


def _execute_auto_web_fetches(
    urls: list[str],
    *,
    step_index: int,
    max_chars: int = 4000,
    max_keep: int = 3,
    enough_chars: int = 1600,
    on_page: Callable[[int, ToolExecutionResult], None] | None = None,
    tools: ToolRegistry | None = None,
) -> list[ToolExecutionResult]:
    """串行读页：优先 playwright 插件，失败退 fetch_url；够用即停。

    插件是单页会话，不能并行预开下一 URL；旁白回调仍按页触发。
    无插件时保留 fetch_url 预取，与旁白重叠。
    """
    from concurrent.futures import ThreadPoolExecutor

    cleaned = [url.strip() for url in urls if str(url).strip()][:5]
    if not cleaned:
        return []

    use_playwright = _playwright_reader_available(tools)
    debug_log(
        "AgentRuntime",
        "深度查询串行读页",
        {
            "step_index": step_index,
            "url_count": len(cleaned),
            "urls": cleaned,
            "max_keep": max_keep,
            "enough_chars": enough_chars,
            "reader": "playwright" if use_playwright else "fetch_url",
        },
    )

    def _read_one(url: str) -> ToolExecutionResult:
        return _read_url_for_deep_lookup(
            url,
            tools=tools,
            max_chars=max_chars,
            prefer_playwright=use_playwright,
        )

    kept: list[ToolExecutionResult] = []
    failures: list[ToolExecutionResult] = []

    if use_playwright:
        for url in cleaned:
            result = _read_one(url)
            usable = bool(result.success and _web_fetch_has_readable_content(result))
            if usable:
                kept.append(result)
                if on_page is not None:
                    on_page(len(kept), result)
                if _auto_fetch_evidence_enough(
                    kept,
                    enough_chars=enough_chars,
                    max_keep=max_keep,
                ):
                    break
            else:
                failures.append(result)
        return kept or failures[:1]

    next_url_index = 0
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="web-autofetch") as pool:
        current_future = pool.submit(_read_one, cleaned[next_url_index])
        next_url_index += 1

        while current_future is not None:
            result = current_future.result()
            usable = bool(result.success and _web_fetch_has_readable_content(result))

            if usable:
                kept.append(result)
            else:
                failures.append(result)

            need_more = (not usable and next_url_index < len(cleaned)) or (
                usable
                and not _auto_fetch_evidence_enough(
                    kept,
                    enough_chars=enough_chars,
                    max_keep=max_keep,
                )
                and next_url_index < len(cleaned)
            )
            prefetch_future = None
            if need_more:
                prefetch_future = pool.submit(_read_one, cleaned[next_url_index])
                next_url_index += 1

            if usable and on_page is not None:
                on_page(len(kept), result)

            if prefetch_future is None:
                break
            current_future = prefetch_future

    if kept:
        return kept
    return failures[:1]


def build_web_search_progress_texts(result: ToolExecutionResult) -> tuple[str, str]:
    """返回 (ja, zh) 过程旁白。"""
    titles: list[str] = []
    content = web_tool_payload(result)
    rows = content.get("results")
    if isinstance(rows, list):
        for item in rows[:2]:
            if isinstance(item, dict):
                title = str(item.get("title") or "").strip()
                if title:
                    titles.append(title[:28])
    if titles:
        joined_zh = "；".join(titles)
        return (
            f"いくつか見つかった。『{titles[0]}』を見てみるね。",
            f"搜到了：{joined_zh}。我先打开看看。",
        )
    return ("ちょっと調べてみたよ。", "我查到一点线索了。")


def build_web_fetch_progress_texts(result: ToolExecutionResult, *, index: int) -> tuple[str, str]:
    if not result.success or not _web_fetch_has_readable_content(result):
        return ("", "")
    content = web_tool_payload(result)
    title = str(content.get("title") or "").strip() or f"资料{index}"
    text = str(content.get("text") or "").strip().replace("\n", " ")
    snippet = text[:72] + ("…" if len(text) > 72 else "")
    if snippet:
        return (
            f"『{title[:20]}』には、{snippet}って書いてある。",
            f"第{index}页《{title[:24]}》里写到：{snippet}",
        )
    return (
        f"『{title[:20]}』を読んだよ。",
        f"第{index}页《{title[:24]}》我读完了。",
    )


def memory_search_budget(recall_decision: str) -> int:
    """同轮 memory_search 成功次数上限：普通 1，显式回忆 2。"""
    return 2 if recall_decision == "recall" else 1


def memory_search_cache_key(arguments: dict[str, Any] | None) -> tuple[str, ...]:
    args = arguments if isinstance(arguments, dict) else {}
    query = str(args.get("query") or args.get("keyword") or "").strip().lower()
    mode = str(args.get("mode") or "full").strip().lower() or "full"
    layer = str(args.get("layer") or "").strip().lower()
    category = str(args.get("category") or "").strip().lower()
    scope = str(args.get("scope") or "").strip().lower()
    return (query, mode, layer, category, scope)


def memory_detail_cache_key(arguments: dict[str, Any] | None) -> tuple[str, ...]:
    args = arguments if isinstance(arguments, dict) else {}
    raw_ids = args.get("ids") or args.get("memory_ids") or []
    ids: list[str] = []
    if isinstance(raw_ids, str):
        ids = [part.strip() for part in raw_ids.split(",") if part.strip()]
    elif isinstance(raw_ids, list):
        ids = [str(part).strip() for part in raw_ids if str(part).strip()]
    return tuple(sorted(ids))


def _memory_tool_content_status(result: ToolExecutionResult) -> str:
    if not result.success:
        return "error"
    content = result.content
    if not isinstance(content, dict):
        return "ok"
    if content.get("skipped"):
        return "skipped"
    status = str(content.get("status") or "").strip().lower()
    if status in {"loading", "failed"}:
        return status
    return "ok"


def count_successful_memory_searches(execution_results: list[ToolExecutionResult]) -> int:
    return sum(
        1
        for result in execution_results
        if result.tool_name in _MEMORY_SEARCH_TOOL_NAMES
        and _memory_tool_content_status(result) == "ok"
    )


def _has_terminal_memory_search_failure(execution_results: list[ToolExecutionResult]) -> bool:
    return any(
        result.tool_name in _MEMORY_SEARCH_TOOL_NAMES
        and _memory_tool_content_status(result) in {"loading", "failed", "error"}
        for result in execution_results
    )


def should_fast_forward_after_memory_search(
    execution_results: list[ToolExecutionResult],
    *,
    recall_decision: str,
) -> bool:
    """记忆搜索达到本轮预算后收束，避免同意图换词连搜。"""
    return count_successful_memory_searches(execution_results) >= memory_search_budget(
        recall_decision
    )


def build_memory_search_gate_result(
    tool_name: str,
    *,
    reason: str,
    message: str,
) -> ToolExecutionResult:
    return ToolExecutionResult(
        tool_name=tool_name,
        success=True,
        content={
            "skipped": True,
            "reason": reason,
            "message": message,
            "agent_hint": message,
        },
        error="",
    )


def resolve_memory_search_gate(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    execution_results: list[ToolExecutionResult],
    recall_decision: str,
    result_cache: dict[tuple[str, tuple[str, ...]], ToolExecutionResult],
) -> ToolExecutionResult | None:
    """memory_search 执行前闸门：缓存命中 / loading·failed 硬拦 / 预算用尽。"""
    if tool_name not in _MEMORY_SEARCH_TOOL_NAMES:
        return None
    cache_key = ("search", memory_search_cache_key(arguments))
    cached = result_cache.get(cache_key)
    if cached is not None:
        cached_payload = cached.content if isinstance(cached.content, dict) else {"cached": cached.content}
        return ToolExecutionResult(
            tool_name=tool_name,
            success=True,
            content={
                **cached_payload,
                "skipped": True,
                "reason": "memory_search_cache_hit",
                "message": "本轮已用相同参数搜索过记忆，以下为缓存结果；请直接作答，不要重复调用。",
                "agent_hint": "本轮已用相同参数搜索过记忆，请直接根据结果作答，不要重复调用。",
            },
            error="",
        )
    if _has_terminal_memory_search_failure(execution_results):
        return build_memory_search_gate_result(
            tool_name,
            reason="memory_search_terminal",
            message="本轮记忆搜索已返回不可用/初始化中状态，请直接作答，不要再次调用 memory_search。",
        )
    budget = memory_search_budget(recall_decision)
    if count_successful_memory_searches(execution_results) >= budget:
        return build_memory_search_gate_result(
            tool_name,
            reason="memory_search_budget",
            message=(
                f"本轮 memory_search 已达上限（{budget} 次），"
                "请基于已有记忆结果直接作答，不要再次搜索。"
            ),
        )
    return None


def resolve_memory_detail_gate(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    result_cache: dict[tuple[str, tuple[str, ...]], ToolExecutionResult],
) -> ToolExecutionResult | None:
    """memory_detail 同参数缓存回放。"""
    if tool_name not in _MEMORY_DETAIL_TOOL_NAMES:
        return None
    cache_key = ("detail", memory_detail_cache_key(arguments))
    cached = result_cache.get(cache_key)
    if cached is None:
        return None
    cached_payload = cached.content if isinstance(cached.content, dict) else {"cached": cached.content}
    return ToolExecutionResult(
        tool_name=tool_name,
        success=True,
        content={
            **cached_payload,
            "skipped": True,
            "reason": "memory_detail_cache_hit",
            "message": "本轮已取过相同记忆详情，以下为缓存结果；请直接作答。",
            "agent_hint": "本轮已取过相同记忆详情，请直接根据结果作答。",
        },
        error="",
    )


def remember_memory_tool_result(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    result: ToolExecutionResult,
    result_cache: dict[tuple[str, tuple[str, ...]], ToolExecutionResult],
) -> None:
    """把成功的记忆检索/详情结果写入同 turn 缓存。"""
    if tool_name in _MEMORY_SEARCH_TOOL_NAMES:
        if _memory_tool_content_status(result) != "ok":
            return
        result_cache[("search", memory_search_cache_key(arguments))] = result
        return
    if tool_name in _MEMORY_DETAIL_TOOL_NAMES:
        if _memory_tool_content_status(result) != "ok":
            return
        result_cache[("detail", memory_detail_cache_key(arguments))] = result


def _latest_user_is_browser_interaction_request(messages: list[ChatMessage]) -> bool:
    text = (_latest_user_text(messages) or "").lower()
    if not text:
        return False
    interaction_keywords = (
        "点击",
        "点开",
        "点进",
        "输入",
        "填写",
        "登录",
        "登陆",
        "提交",
        "下载",
        "滚动",
        "选择",
        "勾选",
        "购买",
        "支付",
        "播放",
        "打开菜单",
        "切换",
        "上传",
        "发帖",
        "评论",
        "回复",
        "删除",
        "编辑",
        "下一页",
        "上一页",
        "クリック",
        "入力",
        "ログイン",
        "送信",
        "ダウンロード",
        "スクロール",
        "選択",
        "click",
        "type",
        "login",
        "log in",
        "submit",
        "download",
        "scroll",
        "select",
        "choose",
        "upload",
    )
    return any(keyword in text for keyword in interaction_keywords)


def _browser_snapshot_has_readable_content(result: ToolExecutionResult) -> bool:
    if result.tool_name != BROWSER_SNAPSHOT_TOOL_NAME or not result.success:
        return False
    text = _tool_result_content_text(result.content).strip()
    if len(text) < 20:
        return False
    normalized = text.lower()
    if _browser_snapshot_looks_like_search_results(normalized):
        return False
    blocked_markers = (
        "error executing tool",
        "http 403",
        "forbidden",
        "timeout",
        '"is_error": true',
        "'is_error': true",
        '"loading": true',
        "'loading': true",
        "加载失败",
        "访问被拒绝",
        "无法访问",
    )
    return not any(marker in normalized for marker in blocked_markers)


def _browser_snapshot_looks_like_search_results(normalized_text: str) -> bool:
    search_page_markers = (
        "google.com/search",
        "bing.com/search",
        "baidu.com/s?",
        "duckduckgo.com/",
        "search.yahoo.com/search",
        "sogou.com/web",
        "yandex.com/search",
        "google 搜索",
        "google search",
        "bing search",
        "百度一下",
        "搜索结果",
        "search results",
    )
    return any(marker in normalized_text for marker in search_page_markers)


def _tool_result_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, default=str)
    except TypeError:
        return str(content)


def _build_browser_page_windows_tool_block_result(call: dict[str, Any]) -> ToolExecutionResult:
    tool_name = str(call.get("name", "")).strip() or "unknown"
    return ToolExecutionResult(
        tool_name="runtime",
        success=False,
        content={
            "blocked_tool": tool_name,
            "reason": "当前上下文是浏览器页面内部操作，已阻止 Windows-MCP 坐标/截图工具抢路由。",
            "guidance": (
                "请使用 playwright_navigate 直达目标 URL，或 playwright_search_web 执行可见搜索；"
                "需要页面文本后调用 playwright_get_text，视觉状态用 playwright_screenshot，"
                "点击或填写时基于真实 selector 调用 playwright_click/playwright_fill。"
            ),
        },
        error=f"已阻止 {tool_name}：浏览器页面内部操作应优先使用 playwright_ 工具。",
    )


def _build_visible_browser_web_tool_block_result(call: dict[str, Any]) -> ToolExecutionResult:
    tool_name = str(call.get("name", "")).strip() or "unknown"
    return ToolExecutionResult(
        tool_name="runtime",
        success=False,
        content={
            "blocked_tool": tool_name,
            "reason": "对方明确要求打开浏览器或看到搜索过程，已阻止后台网页搜索/抓取工具。",
            "guidance": (
                "请优先用 playwright_navigate 直接打开目标 URL，或用 playwright_search_web 搜索；"
                "再按需用 playwright_get_text、playwright_screenshot、playwright_click、"
                "playwright_fill 完成可见浏览器流程。"
            ),
        },
        error=f"已阻止 {tool_name}：显式浏览器任务应使用 playwright_ 工具，不要只做后台搜索。",
    )


def _browser_dom_tools_available(tools: ToolRegistry) -> bool:
    return ToolPolicy.browser_dom_tools_available(tools)


def _should_prefer_browser_page_tools(messages: list[ChatMessage]) -> bool:
    text = _messages_text_for_tool_routing(messages).lower()
    if "playwright_" in text:
        return True

    latest_text = (_latest_user_text(messages) or "").lower()
    if not latest_text:
        return False
    browser_keywords = (
        "浏览器",
        "网页",
        "页面",
        "链接",
        "搜索结果",
        "搜索框",
        "输入框",
        "点进",
        "点开",
        "打开网页",
        "标签页",
        "网址",
        "url",
        "http://",
        "https://",
    )
    return any(keyword in latest_text for keyword in browser_keywords)


def _latest_user_requests_visible_browser(messages: list[ChatMessage]) -> bool:
    text = (_latest_user_text(messages) or "").lower()
    if not text:
        return False
    visible_browser_keywords = (
        "打开浏览器",
        "用浏览器",
        "浏览器搜索",
        "在浏览器",
        "打开网页",
        "打开页面",
        "看搜索过程",
        "看到搜索过程",
        "让我看到",
        "给我看搜索",
        "搜给我看",
        "可见浏览器",
        "前台浏览器",
    )
    return any(keyword in text for keyword in visible_browser_keywords)


def _recent_browser_tool_failed(messages: list[ChatMessage]) -> bool:
    recent_text = _messages_text_for_tool_routing(messages[-4:]).lower()
    return (
        "playwright_" in recent_text
        and (
            '"success": false' in recent_text
            or '"success":false' in recent_text
            or "'success': false" in recent_text
            or "'success':false" in recent_text
            or '"is_error": true' in recent_text
            or '"is_error":true' in recent_text
            or "'is_error': true" in recent_text
            or "'is_error':true" in recent_text
            or "工具执行异常" in recent_text
            or "工具执行失败" in recent_text
        )
    )


def _latest_user_explicitly_requests_windows_control(messages: list[ChatMessage]) -> bool:
    text = (_latest_user_text(messages) or "").lower()
    if not text:
        return False
    explicit_keywords = (
        "真实鼠标",
        "物理鼠标",
        "鼠标",
        "坐标",
        "windows",
        "桌面",
        "窗口",
        "浏览器窗口",
        "地址栏",
        "任务栏",
        "快捷键",
        "键盘",
        "系统界面",
    )
    return any(keyword in text for keyword in explicit_keywords)


def _messages_text_for_tool_routing(messages: list[ChatMessage]) -> str:
    # 延迟 import：内容压缩函数属于 runtime 的上下文构建层，模块级互引会成环
    from app.agent.runtime import _compact_pending_context_content

    return "\n".join(_compact_pending_context_content(message.get("content")) for message in messages)


def _build_browser_page_mode_rule(browser_page_mode: bool) -> str:
    if not browser_page_mode:
        return ""
    return (
        "- 当前上下文已识别为浏览器页面内部操作模式：Windows-MCP 坐标、截图、输入、滚动工具已从可用工具中隐藏。"
        "能直达 URL 时先用 playwright_navigate；需要搜索时用 playwright_search_web；"
        "搜索后如果已经出现目标站点或词条页 URL，优先直接导航到目标页，再继续读取页面正文。"
        "继续读取、截图、点击或填写页面时，必须使用 playwright_ 前缀的原生 Playwright 工具。"
    )


def _build_visible_browser_mode_rule(visible_browser_mode: bool) -> str:
    if not visible_browser_mode:
        return ""
    return (
        "- 对方明确要求打开浏览器或看到搜索过程：后台 web__ 搜索/抓取工具已从可用工具中隐藏。"
        "必须优先用 playwright_navigate 直达目标 URL，或 playwright_search_web 打开可见搜索结果；"
        "能直达页面就不要先打开搜索首页再操作输入框；"
        "需要交互时再用 playwright_get_text/screenshot/click/fill 等工具完成可见浏览器流程。"
    )


def _build_web_tool_capability_rule(visible_browser_mode: bool) -> str:
    if visible_browser_mode:
        return (
            "- 网页：本轮是显式可见浏览器任务，使用 playwright_*；"
            "后台 web__ 搜索/抓取只用于非可见浏览器的轻量公开资料。"
        )
    return (
        "- 网页搜索策略：web__web_search 优先智谱检索，返回长摘要 digest；失败才回退百度/必应/DDG。\n"
        "- 作品/设定/剧情类：先搜一次（max_results 建议 6-8），优先吃 digest/snippet 综合回答；"
        "只有摘要明显不够且链接像可读文章时，再用 web__fetch_url。反爬/视频页不要死磕。\n"
        "- 天气/股价等短事实：一次搜索摘要够用就停。\n"
        "- 社区/论坛类（攻略、评测）可追加'知乎''NGA''贴吧''B站'等社区名重搜。"
    )


def _build_screen_and_desktop_routing_rule(allow_screen_observation: bool) -> str:
    if allow_screen_observation:
        return "\n".join(
            [
                "- 当用户询问当前屏幕内容、可见文字、报错含义、界面状态或“这个是什么意思”时，优先调用 observe_screen；这是 Sakura 内置视觉观察，只用于理解画面和解释，不用于鼠标坐标。",
                "- 寒暄、打招呼、「喂」、叫名字、闲聊情绪时不要调用 observe_screen；没有明确画面依赖就直接回复。",
                "- 当用户要求你点击、移动鼠标、输入、切换窗口或操作桌面应用时，不要用 observe_screen 推理坐标；改用 Windows MCP 的 windows__Snapshot / windows__Screenshot 作为操作前观察。",
            ]
        )
    return "\n".join(
        [
            "- 当前没有可用的 Sakura 内置屏幕理解工具；不要臆造当前屏幕内容。",
            "- 如果用户要求桌面点击、移动鼠标、输入或窗口操作，并且 Windows MCP 截图工具可用，先用 windows__Snapshot / windows__Screenshot 获取真实桌面状态。",
        ]
    )


def _should_offer_screen_observation(messages: list[ChatMessage]) -> bool:
    return ScreenPolicy.should_offer_screen_observation_text(_latest_user_text(messages))


def _latest_user_text(messages: list[ChatMessage]) -> str | None:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            return "\n".join(parts)
        return ""
    return None


# ---- 助手搜索意图检测 ----

_SEARCH_INTENT_JA_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"調べてくる|調べてきます|調べてみる|調べてみよう"),
    re.compile(r"検索してみる|検索してみよう|検索してくる"),
    re.compile(r"探してくる|探してみる|探してみよう"),
    re.compile(r"(攻略|情報|データ|詳細|意味).*(調べ|検索|探)"),
)

_SEARCH_INTENT_ZH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(我去|我来|帮你|给你|替你|帮你).{0,4}(查|搜|搜索|找)(一下|看|一查|一搜|找)"),
    re.compile(r"(查|搜|搜索)一下(攻略|资料|信息|看看)"),
    re.compile(r"(攻略|信息|资料|数据).{0,3}(查|搜|搜索|找)"),
)


def assistant_intends_web_search(assistant_content: str) -> bool:
    """检查助手的 segmented JSON 回复是否表达了『我去查/搜』的意图。

    当助手嘴上说了要去搜索但没有实际发起 tool_call 时，
    工具循环应补激活 mcp 组并继续，让模型兑现承诺。
    """
    text = _extract_segmented_text(assistant_content)
    if not text:
        return False
    for pattern in _SEARCH_INTENT_JA_PATTERNS:
        if pattern.search(text):
            return True
    for pattern in _SEARCH_INTENT_ZH_PATTERNS:
        if pattern.search(text):
            return True
    return False


def _extract_segmented_text(segmented_json: str) -> str:
    """从 segmented reply JSON 中提取 ja + zh 可读文本。"""
    try:
        data = json.loads(segmented_json)
    except (json.JSONDecodeError, TypeError):
        return segmented_json
    segments = data.get("segments") if isinstance(data, dict) else None
    if not isinstance(segments, list):
        return ""
    parts: list[str] = []
    for seg in segments:
        if isinstance(seg, dict):
            ja = seg.get("ja")
            zh = seg.get("zh")
            if isinstance(ja, str):
                parts.append(ja)
            if isinstance(zh, str):
                parts.append(zh)
    return " ".join(parts)
