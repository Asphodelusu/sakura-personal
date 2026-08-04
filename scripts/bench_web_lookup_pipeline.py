"""无 UI 基准：验证「智谱搜摘要 → 串行读页 → 够用即停」流水线。

不启动 Sakura 桌宠即可运行：

    python scripts/bench_web_lookup_pipeline.py
    python scripts/bench_web_lookup_pipeline.py --live "《BRAIN:恐怖脑症候群》是什么都讲了什么"

默认用本地假数据测流水线时序；加 --live 才会打真实智谱搜索 + Edge 读页。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agent.tool_routing import (  # noqa: E402
    _execute_auto_web_fetches,
    _select_urls_for_auto_fetch,
    build_web_fetch_progress_texts,
    build_web_search_progress_texts,
)
from app.agent.tools import ToolExecutionResult  # noqa: E402


def _bench_mock() -> dict:
    from app.agent.mcp import web_search_server as web_mod

    pages = {
        "https://example.com/brain-1": {
            "url": "https://example.com/brain-1",
            "title": "BRAIN 简介",
            "text": ("《BRAIN:恐怖脑症候群》是电気人同人视觉小说，题材含脑癌与禁忌恋。" * 12),
        },
        "https://example.com/brain-2": {
            "url": "https://example.com/brain-2",
            "title": "角色与章节",
            "text": ("主角伊椿南实；试玩可见第一章。血、肉与爱是常见标签讨论。" * 14),
        },
        "https://example.com/brain-3": {
            "url": "https://example.com/brain-3",
            "title": "多余页",
            "text": ("这一页不该被打开。" * 30),
        },
    }
    fetch_log: list[tuple[str, float]] = []
    narrations: list[str] = []

    def fake_fetch(url: str, max_chars: int = 4000):
        started = time.perf_counter()
        time.sleep(0.08)
        fetch_log.append((url, time.perf_counter() - started))
        return pages[url]

    original = web_mod.fetch_url
    web_mod.fetch_url = fake_fetch  # type: ignore[assignment]
    try:
        t0 = time.perf_counter()

        def on_page(index: int, result: ToolExecutionResult) -> None:
            _ja, zh = build_web_fetch_progress_texts(result, index=index)
            narrations.append(zh)
            # 模拟气泡展示耗时：与下一页预取重叠。
            time.sleep(0.05)

        kept = _execute_auto_web_fetches(
            list(pages.keys()),
            step_index=0,
            max_keep=3,
            enough_chars=700,
            on_page=on_page,
        )
        elapsed = time.perf_counter() - t0
    finally:
        web_mod.fetch_url = original  # type: ignore[assignment]

    return {
        "mode": "mock",
        "elapsed_ms": round(elapsed * 1000, 1),
        "fetched_urls": [url for url, _ in fetch_log],
        "kept_urls": [
            str(item.content.get("url"))
            for item in kept
            if isinstance(item.content, dict)
        ],
        "narrations": narrations,
        "opened_third_page": any(url.endswith("brain-3") for url, _ in fetch_log),
    }


def _build_playwright_registry():
    """无桌宠启动时，直接挂上 playwright_browser 插件工具。"""
    from app.agent.tool_policy import PLAYWRIGHT_GET_TEXT_TOOL_NAME, PLAYWRIGHT_NAVIGATE_TOOL_NAME
    from app.agent.tools import Tool, ToolRegistry
    from plugins.playwright_browser import browser as pw_browser

    plugin_root = ROOT / "plugins" / "playwright_browser"
    pw_browser.set_plugin_root(plugin_root)

    registry = ToolRegistry()
    registry.register(
        Tool(
            name=PLAYWRIGHT_NAVIGATE_TOOL_NAME,
            description="nav",
            parameters={"type": "object", "properties": {"url": {"type": "string"}}},
            handler=lambda args: pw_browser.navigate(str(args["url"])),
            group="browser",
        )
    )
    registry.register(
        Tool(
            name=PLAYWRIGHT_GET_TEXT_TOOL_NAME,
            description="text",
            parameters={"type": "object", "properties": {"selector": {"type": "string"}}},
            handler=lambda args: pw_browser.get_text(str(args.get("selector", "body") or "body")),
            group="browser",
        )
    )
    return registry, pw_browser


def _bench_live(query: str) -> dict:
    from app.agent.mcp import web_search_server as web_mod
    from app.agent.tool_routing import _search_has_rich_snippets

    t0 = time.perf_counter()
    search_payload = web_mod.search_web(query, max_results=10)
    search_ms = round((time.perf_counter() - t0) * 1000, 1)
    search_result = ToolExecutionResult(
        tool_name="web__web_search",
        success=bool(search_payload.get("results")),
        content=search_payload,
        error="",
    )
    _ja, search_zh = build_web_search_progress_texts(search_result)
    rich = _search_has_rich_snippets([search_result], query=query)

    urls = _select_urls_for_auto_fetch([search_result], max_urls=4, query=query)
    narrations = [search_zh]
    readers: list[str] = []
    kept: list[ToolExecutionResult] = []
    fetch_ms = 0.0
    registry = None
    pw_browser = None

    if rich:
        narrations.append("（摘要已够用，跳过读页）")
    elif urls:
        registry, pw_browser = _build_playwright_registry()
        fetch_started = time.perf_counter()

        def on_page(index: int, result: ToolExecutionResult) -> None:
            if isinstance(result.content, dict):
                readers.append(str(result.content.get("reader") or ""))
            _ja2, zh = build_web_fetch_progress_texts(result, index=index)
            if zh:
                narrations.append(zh)

        kept = _execute_auto_web_fetches(
            urls,
            step_index=0,
            max_keep=3,
            enough_chars=1600,
            on_page=on_page,
            tools=registry,
        )
        fetch_ms = round((time.perf_counter() - fetch_started) * 1000, 1)
        try:
            pw_browser.shutdown_browser()
        except Exception:
            pass

    return {
        "mode": "live",
        "query": query,
        "search_ms": search_ms,
        "fetch_ms": fetch_ms,
        "search_source": search_payload.get("source"),
        "rich_snippets": rich,
        "result_titles": [
            str(row.get("title") or "")[:40]
            for row in (search_payload.get("results") or [])[:5]
            if isinstance(row, dict)
        ],
        "candidate_urls": urls,
        "kept_urls": [
            str(item.content.get("url"))
            for item in kept
            if isinstance(item.content, dict)
        ],
        "kept_titles": [
            str(item.content.get("title") or "")[:40]
            for item in kept
            if isinstance(item.content, dict)
        ],
        "readers": readers,
        "narrations": narrations,
        "zhipu_calls_expected": 1,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Web lookup pipeline bench (no Sakura UI)")
    parser.add_argument(
        "--live",
        nargs="?",
        const="搜一下《BRAIN:恐怖脑症候群》是什么都讲了什么",
        default=None,
        help="打真实智谱搜索 + 本机 Edge 读页",
    )
    args = parser.parse_args()
    report = _bench_live(args.live) if args.live else _bench_mock()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report.get("mode") == "mock" and report.get("opened_third_page"):
        print("FAIL: should early-stop before third page", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
