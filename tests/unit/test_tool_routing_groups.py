from __future__ import annotations

from app.agent.tool_routing import (
    DEFAULT_ACTIVE_TOOL_GROUPS,
    _should_fast_forward_after_web_search,
    extract_memory_recall_query,
    extract_memory_remember_content,
    infer_active_tool_groups_from_messages,
    messages_contain_recent_web_search,
    user_requests_mcp_followup,
    user_requests_memory_recall,
    user_requests_memory_remember,
)
from app.agent.tools import ToolExecutionResult
from app.llm.api_client import ChatMessage


def test_default_active_groups_is_core_only() -> None:
    assert DEFAULT_ACTIVE_TOOL_GROUPS == frozenset({"core"})


def test_infer_active_groups_stays_core_for_casual_chat() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "今天天气真好呀"}]
    assert infer_active_tool_groups_from_messages(messages) == {"core"}


def test_infer_active_groups_adds_mcp_for_weather_query() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "明天北平是什么天气？"}]
    groups = infer_active_tool_groups_from_messages(messages)
    assert "mcp" in groups


def test_user_message_needs_web_lookup_for_weather_query() -> None:
    from app.agent.tool_routing import user_message_needs_web_lookup

    assert user_message_needs_web_lookup(
        [{"role": "user", "content": "明天北平是什么天气？"}]
    )
    assert not user_message_needs_web_lookup(
        [{"role": "user", "content": "今天天气真好呀"}]
    )


def test_infer_active_groups_adds_productivity_for_todo_keywords() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "帮我记一条待办"}]
    groups = infer_active_tool_groups_from_messages(messages)
    assert "core" in groups
    assert "productivity" in groups


def test_infer_active_groups_adds_mcp_for_search_keywords() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "帮我搜一下今天的新闻"}]
    groups = infer_active_tool_groups_from_messages(messages)
    assert "mcp" in groups


def test_infer_active_groups_adds_memory_write_for_remember_keywords() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "记住我喜欢喝乌龙茶"}]
    groups = infer_active_tool_groups_from_messages(messages)
    assert "memory-write" in groups


def test_infer_active_groups_adds_mcp_for_repeat_search_keywords() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "再查一遍天津天气"}]
    groups = infer_active_tool_groups_from_messages(messages)
    assert "mcp" in groups


def test_infer_active_groups_adds_mcp_when_recent_web_search_and_followup() -> None:
    messages: list[ChatMessage] = [
        {"role": "user", "content": "查一下天津天气"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "web__web_search", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "web__web_search", "content": "{}"},
        {"role": "user", "content": "再查一遍，还是刚才那个"},
    ]
    groups = infer_active_tool_groups_from_messages(messages)
    assert "mcp" in groups


def test_messages_contain_recent_web_search_from_tool_message() -> None:
    messages: list[ChatMessage] = [
        {"role": "tool", "tool_call_id": "call_1", "name": "web__web_search", "content": "{}"},
    ]
    assert messages_contain_recent_web_search(messages)


def test_user_requests_mcp_followup_detects_repeat_phrases() -> None:
    assert user_requests_mcp_followup("再查一遍天气")
    assert user_requests_mcp_followup("还是刚才那个")


def test_extract_memory_remember_content() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "记住：我最喜欢喝乌龙茶"}]
    assert extract_memory_remember_content(messages) == "我最喜欢喝乌龙茶"


def test_user_requests_memory_recall_not_confused_with_remember() -> None:
    remember_messages: list[ChatMessage] = [{"role": "user", "content": "记住我喜欢喝乌龙茶"}]
    recall_messages: list[ChatMessage] = [{"role": "user", "content": "我刚才让你记住我喜欢喝什么？"}]
    assert user_requests_memory_remember(remember_messages)
    assert not user_requests_memory_recall(remember_messages)
    assert user_requests_memory_recall(recall_messages)
    assert extract_memory_recall_query(recall_messages) == "我刚才让你记住我喜欢喝什么？"


def test_should_fast_forward_after_web_search_for_lookup_intent() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "帮我查一下天津天气"}]
    results = [
        ToolExecutionResult(
            tool_name="web__web_search",
            success=True,
            content={"results": [{"title": "天津天气"}]},
            error="",
        )
    ]
    assert _should_fast_forward_after_web_search(messages, results)


def test_should_not_fast_forward_after_web_search_for_casual_chat() -> None:
    messages: list[ChatMessage] = [{"role": "user", "content": "今天心情不错"}]
    results = [
        ToolExecutionResult(
            tool_name="web__web_search",
            success=True,
            content={"results": [{"title": "x"}]},
            error="",
        )
    ]
    assert not _should_fast_forward_after_web_search(messages, results)


def test_error_shell_page_is_not_readable() -> None:
    from app.agent.tool_routing import _text_looks_like_readable_page

    bilibili_404 = (
        "呜呼，出错啦。你访问的页面不存在了。"
        + ("导航栏首页动态" * 20)
    )
    assert not _text_looks_like_readable_page(bilibili_404, title="出错啦")


def test_select_urls_demotes_bilibili_columns() -> None:
    from app.agent.tool_routing import _select_urls_for_auto_fetch

    search = ToolExecutionResult(
        tool_name="web__web_search",
        success=True,
        content={
            "results": [
                {
                    "title": "失效专栏",
                    "url": "https://www.bilibili.com/read/cv99999999",
                    "snippet": "游戏介绍",
                },
                {
                    "title": "百科条目",
                    "url": "https://zh.moegirl.org.cn/example",
                    "snippet": "游戏介绍与攻略",
                },
            ],
            "snippet_chars": 20,
        },
        error="",
    )
    urls = _select_urls_for_auto_fetch(
        [search],
        max_urls=1,
        query="某游戏介绍和攻略",
    )
    assert urls == ["https://zh.moegirl.org.cn/example"]


def test_deep_lookup_waits_for_fetch_before_fast_forward() -> None:
    from app.agent.tool_routing import (
        _latest_user_is_deep_web_lookup,
        _select_urls_for_auto_fetch,
        _should_auto_fetch_after_web_search,
    )

    messages: list[ChatMessage] = [
        {"role": "user", "content": "搜一下沙耶之歌是什么都讲了什么"}
    ]
    search = ToolExecutionResult(
        tool_name="web__web_search",
        success=True,
        content={
            "results": [
                {
                    "title": "沙耶之歌 - 维基百科",
                    "url": "https://zh.wikipedia.org/wiki/%E6%B2%99%E8%80%B6%E4%B9%8B%E6%AD%8C",
                    "snippet": "百科简介",
                }
            ],
            "snippet_chars": 4,
        },
        error="",
    )
    assert _latest_user_is_deep_web_lookup(messages)
    assert not _should_fast_forward_after_web_search(messages, [search])
    assert _should_auto_fetch_after_web_search(messages, [search], [search])
    assert _select_urls_for_auto_fetch(
        [search],
        max_urls=1,
        query="搜一下沙耶之歌是什么都讲了什么",
    )

    fetch = ToolExecutionResult(
        tool_name="web__fetch_url",
        success=True,
        content={
            "url": "https://zh.wikipedia.org/wiki/x",
            "title": "沙耶之歌",
            "text": "这是一部恐怖风格视觉小说，讲述了主人公感知扭曲后的世界。" * 3,
        },
        error="",
    )
    assert _should_fast_forward_after_web_search(messages, [search, fetch])


def test_deep_lookup_fast_forwards_on_rich_snippets_without_fetch() -> None:
    messages: list[ChatMessage] = [
        {"role": "user", "content": "搜一下《BRAIN:恐怖脑症候群》是什么都讲了什么"}
    ]
    rich = "BRAIN 恐怖脑症候群 血肉与爱的宗教。" * 40
    search = ToolExecutionResult(
        tool_name="web__web_search",
        success=True,
        content={
            "query": "BRAIN 恐怖脑症候群",
            "digest": rich,
            "snippet_chars": len(rich),
            "results": [
                {
                    "title": "brain:恐怖脑症候群",
                    "url": "https://www.bilibili.com/video/BVxxxx",
                    "snippet": rich,
                }
            ],
        },
        error="",
    )
    assert _should_fast_forward_after_web_search(messages, [search])
    from app.agent.tool_routing import _should_auto_fetch_after_web_search

    assert not _should_auto_fetch_after_web_search(messages, [search], [search])


def test_query_terms_keep_title_prefix_for_g_senjou() -> None:
    from app.agent.tool_routing import _query_relevance_terms, _search_has_rich_snippets
    from app.agent.tools import ToolExecutionResult

    terms = _query_relevance_terms("搜一下G弦上的魔王介绍和攻略")
    assert any("弦上的魔王" in term or term == "弦上的魔王" for term in terms)

    rich = "《G弦上的魔王》AKABEiSOFT2 冒险游戏，宇佐美春与魔王。" * 30
    search = ToolExecutionResult(
        tool_name="web__web_search",
        success=True,
        content={
            "digest": rich,
            "snippet_chars": len(rich),
            "results": [
                {
                    "title": "g弦上的魔王攻略",
                    "url": "https://example.com/g",
                    "snippet": rich,
                }
            ],
        },
        error="",
    )
    assert _search_has_rich_snippets([search], query="搜一下G弦上的魔王介绍和攻略")


def test_deep_lookup_prefers_playwright_reader(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.agent import tool_routing as mod
    from app.agent.tool_policy import PLAYWRIGHT_GET_TEXT_TOOL_NAME, PLAYWRIGHT_NAVIGATE_TOOL_NAME
    from app.agent.tools import Tool, ToolExecutionResult, ToolRegistry

    calls: list[str] = []

    def nav_handler(args: dict) -> dict:
        calls.append(f"nav:{args['url']}")
        return {"url": args["url"], "title": "攻略页"}

    def text_handler(_args: dict) -> str:
        calls.append("text")
        return "《G弦上的魔王》是一部悬疑纯爱视觉小说，讲述京介与自称勇者的转学生对抗魔王的故事。" * 8

    registry = ToolRegistry()
    registry.register(
        Tool(
            name=PLAYWRIGHT_NAVIGATE_TOOL_NAME,
            description="nav",
            parameters={"type": "object", "properties": {}},
            handler=nav_handler,
            group="browser",
        )
    )
    registry.register(
        Tool(
            name=PLAYWRIGHT_GET_TEXT_TOOL_NAME,
            description="text",
            parameters={"type": "object", "properties": {}},
            handler=text_handler,
            group="browser",
        )
    )

    def fail_fetch(*_a, **_k):
        raise AssertionError("should prefer playwright over fetch_url")

    monkeypatch.setattr(mod, "_read_url_via_fetch", fail_fetch)

    narrated: list[str] = []
    results = mod._execute_auto_web_fetches(
        ["https://example.com/guide"],
        step_index=0,
        max_keep=2,
        enough_chars=400,
        tools=registry,
        on_page=lambda _i, result: narrated.append(str(result.content.get("reader"))),
    )
    assert results and results[0].success
    assert results[0].content["reader"] == "playwright"
    assert narrated == ["playwright"]
    assert calls[0].startswith("nav:")
    assert "text" in calls


def test_auto_fetch_pipeline_narrates_then_stops_when_enough(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import time

    from app.agent import tool_routing as mod
    from app.agent.mcp import web_search_server as web_mod

    pages = {
        "https://example.com/a": {
            "url": "https://example.com/a",
            "title": "页A",
            # 单页不足 enough_chars，需读到第 2 页才停。
            "text": ("沙耶之歌是一部视觉小说，讲述了扭曲感知与恐怖爱情。" * 8),
        },
        "https://example.com/b": {
            "url": "https://example.com/b",
            "title": "页B",
            "text": ("补充设定：角色与结局分支，还有世界观说明。" * 12),
        },
        "https://example.com/c": {
            "url": "https://example.com/c",
            "title": "页C",
            "text": ("不该再打开的第三页。" * 20),
        },
    }
    fetch_order: list[str] = []
    narrated: list[int] = []

    def fake_fetch(url: str, max_chars: int = 4000):
        fetch_order.append(url)
        time.sleep(0.05)
        return pages[url]

    monkeypatch.setattr(web_mod, "fetch_url", fake_fetch)

    results = mod._execute_auto_web_fetches(
        list(pages.keys()),
        step_index=1,
        max_keep=3,
        enough_chars=350,
        on_page=lambda index, _result: narrated.append(index),
    )

    assert [r.content["url"] for r in results] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert narrated == [1, 2]
    assert "https://example.com/c" not in fetch_order
    assert fetch_order[:2] == [
        "https://example.com/a",
        "https://example.com/b",
    ]


def test_auto_fetch_pipeline_skips_unreadable_and_continues(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.agent import tool_routing as mod
    from app.agent.mcp import web_search_server as web_mod

    def fake_fetch(url: str, max_chars: int = 4000):
        if url.endswith("/bad"):
            return {"url": url, "title": "x", "text": "403"}
        return {
            "url": url,
            "title": "好页",
            "text": ("这部作品讲了主角与怪物化恋人的故事，包含多条结局。" * 15),
        }

    monkeypatch.setattr(web_mod, "fetch_url", fake_fetch)
    narrated: list[str] = []

    results = mod._execute_auto_web_fetches(
        ["https://example.com/bad", "https://example.com/good"],
        step_index=0,
        max_keep=2,
        enough_chars=400,
        on_page=lambda _i, result: narrated.append(str(result.content.get("url"))),
    )

    assert len(results) == 1
    assert results[0].content["url"] == "https://example.com/good"
    assert narrated == ["https://example.com/good"]


def test_offtopic_long_snippets_do_not_count_as_rich() -> None:
    from app.agent.tool_routing import _search_has_rich_snippets, _should_refine_web_search

    messages: list[ChatMessage] = [
        {"role": "user", "content": "搜一下《BRAIN:恐怖脑症候群》是什么都讲了什么"}
    ]
    medical = "脑干症候群的临床表现与治疗。" * 80
    search = ToolExecutionResult(
        tool_name="web__web_search",
        success=True,
        content={
            "snippet_chars": len(medical),
            "digest": medical,
            "results": [
                {
                    "title": "脑干症候群解读",
                    "url": "https://example.com/medical",
                    "snippet": medical,
                }
            ],
        },
        error="",
    )
    assert not _search_has_rich_snippets(
        [search],
        query="搜一下《BRAIN:恐怖脑症候群》是什么都讲了什么",
    )
    # 自动换词重搜默认关闭：避免按次计费被放大。
    assert not _should_refine_web_search(messages, [search])


def test_memory_search_budget_and_fast_forward() -> None:
    from app.agent.tool_routing import (
        count_successful_memory_searches,
        memory_search_budget,
        resolve_memory_search_gate,
        should_fast_forward_after_memory_search,
    )

    assert memory_search_budget("light") == 1
    assert memory_search_budget("recall") == 2

    ok = ToolExecutionResult(
        tool_name="memory_search",
        success=True,
        content={"query": "最早", "count": 1, "memories": [{"id": "m1", "content": "x"}]},
        error="",
    )
    assert count_successful_memory_searches([ok]) == 1
    assert should_fast_forward_after_memory_search([ok], recall_decision="light")
    assert not should_fast_forward_after_memory_search([ok], recall_decision="recall")

    cache: dict = {}
    from app.agent.tool_routing import remember_memory_tool_result

    remember_memory_tool_result(
        tool_name="memory_search",
        arguments={"query": "最早", "mode": "full"},
        result=ok,
        result_cache=cache,
    )
    hit = resolve_memory_search_gate(
        tool_name="memory_search",
        arguments={"query": "最早", "mode": "full"},
        execution_results=[ok],
        recall_decision="light",
        result_cache=cache,
    )
    assert hit is not None
    assert isinstance(hit.content, dict)
    assert hit.content.get("reason") == "memory_search_cache_hit"
    assert hit.content.get("count") == 1

    budget_block = resolve_memory_search_gate(
        tool_name="memory_search",
        arguments={"query": "另一句", "mode": "full"},
        execution_results=[ok],
        recall_decision="light",
        result_cache={},
    )
    assert budget_block is not None
    assert isinstance(budget_block.content, dict)
    assert budget_block.content.get("reason") == "memory_search_budget"

    loading = ToolExecutionResult(
        tool_name="memory_search",
        success=True,
        content={"status": "loading", "message": "init", "memories": []},
        error="",
    )
    terminal = resolve_memory_search_gate(
        tool_name="memory_search",
        arguments={"query": "再试", "mode": "full"},
        execution_results=[loading],
        recall_decision="recall",
        result_cache={},
    )
    assert terminal is not None
    assert isinstance(terminal.content, dict)
    assert terminal.content.get("reason") == "memory_search_terminal"
