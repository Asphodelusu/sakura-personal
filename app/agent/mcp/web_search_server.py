from __future__ import annotations

import html
import json
import os
import re as _re
import sys

# Force UTF-8 for stdout on Windows — critical for CJK content in MCP JSON-RPC
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
import base64
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from ipaddress import ip_address
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen


SERVER_NAME = "sakura-web-search"
SERVER_VERSION = "0.3.0"
DEFAULT_TIMEOUT_SECONDS = 12
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0"
)
# Playwright 读页/搜索默认走本机 Edge；可用 SAKURA_PW_CHANNEL=chrome|chromium 覆盖。
DEFAULT_PW_CHANNEL = "msedge"
# 读页默认有头（能看到本机 Edge 打开/收起）；搜索仍无头。SAKURA_PW_FETCH_HEADED=0 可关。
DEFAULT_PW_FETCH_HEADED = True
MIN_SEARCH_RESULTS_FOR_CONFIDENCE = 2

# 智谱 Web Search（Agent 专用检索）；与 Chat 模型无关，共用 open.bigmodel.cn 的 API Key。
ZHIPU_WEB_SEARCH_URL = "https://open.bigmodel.cn/api/paas/v4/web_search"
DEFAULT_ZHIPU_SEARCH_ENGINE = "search_pro"
_ZHIPU_ENGINE_ALIASES = {
    "search_std": "search_std",
    "search-std": "search_std",
    "search_pro": "search_pro",
    "search-pro": "search_pro",
    "search_pro_sogou": "search_pro_sogou",
    "search-pro-sogou": "search_pro_sogou",
    "search_pro_quark": "search_pro_quark",
    "search-pro-quark": "search_pro_quark",
}

# Playwright engine constants
PW_BROWSER_ARGS = ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]
PW_TIMEOUT = 25000
PW_HOME_TIMEOUT = 15000
PW_FETCH_TIMEOUT = 15000
PW_ROUTE_BLOCK = "**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf,mp4,ico,webp,js.map}"

import asyncio
import concurrent.futures

# Cached IP detection result
_in_china_cache: bool | None = None
# Cached Playwright browser (lazy singleton)
_pw_browser = None
_pw_headed_browser = None
_pw_playwright = None
# Thread pool for running sync Playwright calls inside the asyncio MCP server
_PW_THREAD_POOL: concurrent.futures.ThreadPoolExecutor | None = None


def _run_in_thread(fn, *args, **kwargs):
    """Run a sync callable in a thread to avoid Playwright's asyncio-loop detection."""
    global _PW_THREAD_POOL
    if _PW_THREAD_POOL is None:
        _PW_THREAD_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="pw-search")
    return _PW_THREAD_POOL.submit(fn, *args, **kwargs).result()


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""


TOOLS: list[dict[str, Any]] = [
    {
        "name": "web_search",
        "description": (
            "搜索公开网页，返回标题、链接、长摘要 digest。"
            "优先智谱 Web Search（适合中文资料）；摘要够用时不必再 fetch。"
            "可用 site:域名 限定范围；社区向可追加'知乎''NGA''B站'重搜。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词。",
                },
                "max_results": {
                    "type": "integer",
                    "description": "最多采用多少条结果写入上下文，范围 1-15；智谱一次请求可召回更多。",
                    "minimum": 1,
                    "maximum": 15,
                    "default": 10,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fetch_url",
        "description": "读取一个公开 http/https 网页，抽取标题、正文文本和页面链接。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要读取的公开网页 URL，仅支持 http 或 https。",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "正文最多返回多少字符，范围 500-20000。",
                    "minimum": 500,
                    "maximum": 20000,
                    "default": 6000,
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
]


def main() -> int:
    try:
        _run_fastmcp_server()
        return 0
    except ImportError:
        # 测试环境或未安装 mcp 时保留轻量 JSON-RPC fallback，正式运行应使用 FastMCP。
        pass

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
            response = handle_message(message)
        except Exception as exc:  # MCP Server 不能因为单条坏消息退出。
            response = _error_response(None, -32603, f"内部错误：{exc}")
        if response is not None:
            _write_message(response)
    return 0


def _run_fastmcp_server() -> None:
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(SERVER_NAME, log_level="ERROR")

    @mcp.tool(
        name="web_search",
        description=(
            "搜索公开网页，返回标题、链接、长摘要 digest。"
            "优先智谱 Web Search（适合中文资料）；摘要够用时不必再 fetch。"
            "可用 site:域名 限定范围；社区向可追加'知乎''NGA''B站'重搜。"
        ),
        structured_output=False,
    )
    def web_search_tool(query: str, max_results: int = 10) -> dict[str, Any]:
        """搜索公开网页。"""

        return search_web(
            query=query,
            max_results=_clamp_int(max_results, default=10, minimum=1, maximum=15),
        )

    @mcp.tool(
        name="fetch_url",
        description="读取一个公开 http/https 网页，抽取标题、正文文本和页面链接。",
        structured_output=False,
    )
    def fetch_url_tool(url: str, max_chars: int = 6000) -> dict[str, Any]:
        """读取公开网页正文。"""

        return fetch_url(
            url=url,
            max_chars=_clamp_int(max_chars, default=6000, minimum=500, maximum=20000),
        )

    mcp.run("stdio")


def handle_message(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    method = str(message.get("method") or "")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    if request_id is None:
        return None
    if method == "initialize":
        requested_version = str(params.get("protocolVersion") or "2024-11-05")
        return _result_response(
            request_id,
            {
                "protocolVersion": requested_version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    if method == "ping":
        return _result_response(request_id, {})
    if method == "tools/list":
        return _result_response(request_id, {"tools": TOOLS})
    if method == "tools/call":
        return _handle_tool_call(request_id, params)
    if method == "resources/list":
        return _result_response(request_id, {"resources": []})
    if method == "prompts/list":
        return _result_response(request_id, {"prompts": []})
    return _error_response(request_id, -32601, f"不支持的方法：{method}")


def _handle_tool_call(request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    name = str(params.get("name") or "")
    arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
    try:
        if name == "web_search":
            payload = search_web(
                query=_required_string(arguments, "query"),
                max_results=_clamp_int(arguments.get("max_results"), default=10, minimum=1, maximum=15),
            )
        elif name == "fetch_url":
            payload = fetch_url(
                url=_required_string(arguments, "url"),
                max_chars=_clamp_int(arguments.get("max_chars"), default=6000, minimum=500, maximum=20000),
            )
        else:
            return _error_response(request_id, -32602, f"未知工具：{name}")
    except Exception as exc:
        return _result_response(
            request_id,
            {
                "content": [{"type": "text", "text": str(exc)}],
                "isError": True,
            },
        )
    return _tool_result_response(request_id, payload)


def _repo_root():
    # app/agent/mcp/web_search_server.py → 仓库根
    from pathlib import Path

    return Path(__file__).resolve().parents[3]


def _zhipu_search_engine() -> str:
    raw = (
        os.environ.get("SAKURA_ZHIPU_SEARCH_ENGINE")
        or os.environ.get("ZHIPU_SEARCH_ENGINE")
        or DEFAULT_ZHIPU_SEARCH_ENGINE
    ).strip()
    return _ZHIPU_ENGINE_ALIASES.get(raw, DEFAULT_ZHIPU_SEARCH_ENGINE)


def _resolve_zhipu_api_key() -> str:
    """读取智谱 Key：环境变量优先，否则从 data/config/api.yaml 的 bigmodel 配置档取。"""
    for env_name in (
        "SAKURA_ZHIPU_API_KEY",
        "ZHIPU_API_KEY",
        "BIGMODEL_API_KEY",
    ):
        value = str(os.environ.get(env_name) or "").strip()
        if value:
            return value

    try:
        from pathlib import Path

        api_yaml = _repo_root() / "data" / "config" / "api.yaml"
        if not api_yaml.is_file():
            return ""
        text = api_yaml.read_text(encoding="utf-8")
        # 轻量解析：优先匹配 open.bigmodel.cn 配置档下的 api_key
        # 避免引入 PyYAML 依赖（MCP 子进程环境尽量瘦）。
        profiles: list[dict[str, str]] = []
        current: dict[str, str] = {}
        in_profiles = False
        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()
            if stripped.startswith("api_profiles:"):
                in_profiles = True
                current = {}
                continue
            if not in_profiles:
                continue
            if stripped and not line.startswith(" ") and not line.startswith("-"):
                # 离开 api_profiles 段
                if current.get("api_key"):
                    profiles.append(current)
                break
            if stripped.startswith("- id:") or stripped.startswith("-id:"):
                if current.get("api_key"):
                    profiles.append(current)
                current = {}
                continue
            if "base_url:" in stripped:
                current["base_url"] = stripped.split("base_url:", 1)[1].strip().strip("'\"")
            elif "api_key:" in stripped:
                current["api_key"] = stripped.split("api_key:", 1)[1].strip().strip("'\"")
        if current.get("api_key"):
            profiles.append(current)
        for profile in profiles:
            base = str(profile.get("base_url") or "").lower()
            key = str(profile.get("api_key") or "").strip()
            if key and ("bigmodel.cn" in base or "zhipuai.cn" in base):
                return key
    except Exception:
        return ""
    return ""


def _search_zhipu_web(
    query: str,
    max_results: int,
    *,
    api_key: str,
) -> list[SearchResult]:
    """调用智谱 /paas/v4/web_search，返回与现有引擎一致的 SearchResult 列表。

    计费：按「HTTP 请求次数」扣资源包，与 count 返回条数无关。
    官方推荐：单次 search_pro + count(1-50，默认10) + content_size。
    """
    engine = _zhipu_search_engine()
    # 文档建议 query 不超过 70 字符；过长时截断，避免 4xx。
    search_query = query.strip()
    if len(search_query) > 70:
        search_query = search_query[:70]
    # 官方示例常用 10/15；一次请求多拿几条摘要，比多次换词补搜更省次数。
    count = max(1, min(int(max_results), 50))
    if count < 10:
        count = 10
    # search_pro_sogou 仅接受 10/20/30/40/50
    if engine == "search_pro_sogou":
        for candidate in (10, 20, 30, 40, 50):
            if count <= candidate:
                count = candidate
                break
        else:
            count = 50

    payload = {
        "search_query": search_query,
        "search_engine": engine,
        # 跳过意图识别，保证「搜一下 xxx」必定检索（否则可能 SEARCH_NONE 空结果）。
        "search_intent": False,
        "count": count,
        "content_size": "high",
        "search_recency_filter": "noLimit",
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        ZHIPU_WEB_SEARCH_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {exc.code} {detail}".strip()) from exc
    except URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc

    data = json.loads(raw) if raw.strip() else {}
    if not isinstance(data, dict):
        raise RuntimeError("invalid response")
    if isinstance(data.get("error"), dict):
        err = data["error"]
        raise RuntimeError(f"{err.get('code')}: {err.get('message')}")

    rows = data.get("search_result")
    if not isinstance(rows, list):
        return []

    results: list[SearchResult] = []
    seen_urls: set[str] = set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        title = _normalize_space(str(item.get("title") or ""))
        url = str(item.get("link") or "").strip()
        snippet = _normalize_space(str(item.get("content") or ""))
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        if not title:
            title = url
        # content_size=high 时摘要本身就很长；勿截成 500，否则深度题只能「标题党」。
        results.append(SearchResult(title=title, url=url, snippet=snippet[:2500]))
        if len(results) >= max_results:
            break
    return results


def _build_search_digest(results: list[SearchResult], *, max_chars: int = 5500) -> str:
    """把多条长摘要压成模型易读的证据块（少依赖本地读页）。"""
    parts: list[str] = []
    used = 0
    for index, item in enumerate(results, start=1):
        block = f"[{index}] {item.title}\nURL: {item.url}\n{item.snippet}".strip()
        if not block:
            continue
        extra = len(block) + (2 if parts else 0)
        if used + extra > max_chars:
            remain = max_chars - used - 2
            if remain >= 120:
                parts.append(block[:remain] + "…")
            break
        parts.append(block)
        used += extra
    return "\n\n".join(parts)


def _extract_work_title(query: str) -> str:
    import re

    titled = re.findall(r"《([^》]{2,40})》", query or "")
    if titled:
        return titled[0].strip()
    if re.search(r"\bBRAIN\b", query or "", flags=re.I) and "恐怖脑" in (query or ""):
        return "BRAIN:恐怖脑症候群"
    return ""


def _enrich_search_query(query: str) -> str:
    """作品名查询轻量改写：抽出标题本体，去掉「搜一下/是什么」等口语稀释。"""
    text = (query or "").strip()
    if not text:
        return text
    core = _extract_work_title(text)
    if core:
        return core[:70]
    return text[:70] if len(text) > 70 else text


def _results_match_work_title(results: list[SearchResult], core: str) -> bool:
    """检索结果是否真正命中目标作品名（防止漂到「惧魔症候群」等同名坑）。"""
    if not core or not results:
        return False
    blob = "\n".join(f"{item.title}\n{item.snippet}" for item in results).lower()
    core_l = core.lower().replace("：", ":")
    if core_l in blob:
        return True
    parts = [part.strip() for part in _re.split(r"[:：]", core) if part.strip()]
    if len(parts) >= 2:
        left, right = parts[0].lower(), parts[1]
        if right.lower() in blob and left in blob:
            return True
        # 中文主标题足够长时，单独命中也算（如「恐怖脑症候群」）
        if len(right) >= 4 and right.lower() in blob:
            return True
    return False


def _zhipu_fallback_queries(query: str, primary: str) -> list[str]:
    """首轮跑偏时的补搜词；控制在 1 次额外请求以内优先选最稳的。"""
    core = _extract_work_title(query) or primary
    variants: list[str] = []
    # 经验上：小众同人作品在 B 站标题/作者微博摘要里更完整
    if _re.search(r"brain", core, flags=_re.I) and "恐怖脑" in core:
        variants.extend(
            [
                # 作者微博摘要含剧情要点；B 站标题页摘要通常过短。
                "电気人_denki brain",
                "恶劣的血与爱 brain",
            ]
        )
    variants.append(f"「{core}」")
    parts = [part.strip() for part in _re.split(r"[:：]", core) if part.strip()]
    if len(parts) >= 2:
        variants.append(f"{parts[0]} {parts[1]}")
    # 去重且去掉与 primary 相同的
    ordered: list[str] = []
    seen = {primary.strip().lower()}
    for item in variants:
        key = item.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(item[:70])
    return ordered[:2]


def _pack_search_results(
    *,
    query: str,
    search_query: str,
    source: str,
    results: list[SearchResult],
    fallback_queries: list[str] | None = None,
) -> dict[str, Any]:
    payload = {
        "query": query,
        "search_query": search_query,
        "source": source,
        "results": [
            {"title": item.title, "url": item.url, "snippet": item.snippet}
            for item in results
        ],
        "digest": _build_search_digest(results),
        "snippet_chars": sum(len(item.snippet) for item in results),
    }
    if fallback_queries:
        payload["fallback_queries"] = fallback_queries
    return payload


def search_web(query: str, max_results: int = 10) -> dict[str, Any]:
    query = query.strip()
    if not query:
        raise ValueError("query 不能为空。")
    search_query = _enrich_search_query(query)

    errors: list[str] = []

    # --- Primary: 智谱 Web Search API（结构化摘要，稳定且适合中文）---
    # 官方用法：一次请求拿够条数/长摘要；禁止同轮多次换词补搜（每次 HTTP = 扣 1 次资源包）。
    zhipu_key = _resolve_zhipu_api_key()
    if zhipu_key:
        try:
            results = _search_zhipu_web(search_query, max_results, api_key=zhipu_key)
            if results:
                return _pack_search_results(
                    query=query,
                    search_query=search_query,
                    source=f"Zhipu ({_zhipu_search_engine()})",
                    results=results,
                )
            errors.append("Zhipu: empty results")
        except Exception as exc:
            errors.append(f"Zhipu: {exc}")

    # --- Fallback: Playwright engines by IP region ---
    # CN: Baidu (best for Chinese communities) → Bing CN → DDG
    # Non-CN: DDG → Bing → Baidu
    try:
        in_china = _detect_in_china()
        if in_china:
            engines: list[tuple[str, Any]] = [
                ("Baidu (Playwright)", _search_baidu_playwright),
                ("Bing CN (Playwright)", _search_bing_playwright),
                ("DDG (Playwright)", _search_duckduckgo_playwright),
            ]
        else:
            engines = [
                ("DDG (Playwright)", _search_duckduckgo_playwright),
                ("Bing CN (Playwright)", _search_bing_playwright),
                ("Baidu (Playwright)", _search_baidu_playwright),
            ]
        for engine_name, engine_fn in engines:
            try:
                results = _run_in_thread(engine_fn, search_query, max_results)
                if results:
                    return _pack_search_results(
                        query=query,
                        search_query=search_query,
                        source=engine_name,
                        results=results,
                    )
            except Exception as exc:
                errors.append(f"{engine_name}: {exc}")
    except Exception as exc:
        errors.append(f"Playwright engines: {exc}")

    # --- All engines failed: return visible error signal in results ---
    error_msg = "、".join(errors) if errors else "所有搜索引擎均不可用"
    return {
        "query": query,
        "source": "all_failed",
        "results": [
            {"title": f"\u26a0\ufe0f 搜索失败：{error_msg}", "url": "", "snippet": "请检查网络连接或稍后重试。"},
        ],
    }


def fetch_url(url: str, max_chars: int = 6000) -> dict[str, Any]:
    normalized_url = _validate_public_http_url(url)

    # --- Primary: Playwright for JS-heavy pages ---
    try:
        return _run_in_thread(_fetch_url_playwright, normalized_url, max_chars)
    except Exception:
        pass

    # --- Fallback: urllib + readability ---
    raw_text, content_type, final_url = _read_url_text_with_metadata(
        normalized_url,
        max_bytes=max(256_000, min(max_chars * 8, 1_500_000)),
    )
    if "html" in content_type.lower():
        try:
            title, text, links = _extract_with_readability(raw_text, final_url)
        except Exception:
            parser = PageTextParser()
            parser.feed(raw_text)
            text = _normalize_space(parser.text)
            title = _normalize_space(parser.title)
            links = parser.links[:30]
    else:
        text = _normalize_space(raw_text)
        title = ""
        links = []
    return {
        "url": final_url,
        "content_type": content_type,
        "title": title,
        "text": text[:max_chars],
        "truncated": len(text) > max_chars,
        "links": links,
    }


def _extract_with_readability(html_text: str, base_url: str = "") -> tuple[str, str, list[dict[str, str]]]:
    """使用 readability-lxml 抽取网页正文，比简单 HTML 剥离质量高得多。"""
    from readability import Document
    doc = Document(html_text, url=base_url)
    title = _normalize_space(doc.title() or "")
    summary_html = doc.summary()
    # 从 summary HTML 中提取纯文本
    text = _normalize_space(_strip_html_tags(summary_html))
    # 提取页面链接
    links: list[dict[str, str]] = []
    link_re = _re.compile(r'<a[^>]+href=["\'](https?://[^"\'\s]+)["\'][^>]*>([^<]*)</a>', _re.IGNORECASE)
    for match in link_re.finditer(html_text):
        href = match.group(1)
        link_text = _normalize_space(match.group(2))
        if href and link_text and not href.startswith(base_url.rstrip("/") + "#"):
            links.append({"text": link_text[:120], "url": href})
    return title, text, links[:30]


def _strip_html_tags(html_text: str) -> str:
    """快速剥离 HTML 标签，保留文本内容。"""
    return _re.sub(r"<[^>]+>", "", html_text)



class BaiduSearchParser(HTMLParser):
    """解析 Baidu 搜索结果页的标题、链接和摘要。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[SearchResult] = []
        self._in_result = False
        self._in_title = False
        self._in_snippet = False
        self._title_parts: list[str] = []
        self._snippet_parts: list[str] = []
        self._pending_url = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {key.lower(): value or "" for key, value in attrs}
        if tag == "div" and "result" in attrs_map.get("class", "").lower():
            self._in_result = True
            self._title_parts = []
            self._snippet_parts = []
            self._pending_url = ""
        elif self._in_result and tag == "a":
            href = attrs_map.get("href", "")
            if href and not href.startswith("#"):
                self._pending_url = href
            self._in_title = True
            self._title_parts = []
        elif self._in_result and ("content" in attrs_map.get("class", "").lower()
                                   or "abstract" in attrs_map.get("class", "").lower()):
            self._in_snippet = True
            self._snippet_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        elif self._in_snippet:
            self._snippet_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_title:
            title = _normalize_space("".join(self._title_parts))
            if title and self._pending_url:
                self.results.append(SearchResult(
                    title=title,
                    url=self._pending_url,
                ))
            self._in_title = False
            self._title_parts = []
        elif self._in_snippet and tag in {"div", "span", "p"}:
            snippet = _normalize_space("".join(self._snippet_parts))
            if snippet and self.results:
                prev = self.results[-1]
                if not prev.snippet:
                    self.results[-1] = SearchResult(
                        title=prev.title, url=prev.url, snippet=snippet[:300]
                    )
            self._in_snippet = False
            self._snippet_parts = []
        elif tag == "div" and self._in_result:
            self._in_result = False


class BingSearchParser(HTMLParser):
    """解析 Bing 搜索结果页中自然搜索结果的标题、链接和摘要。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[SearchResult] = []
        self._result_depth = 0
        self._in_title_link = False
        self._in_snippet = False
        self._active_href = ""
        self._active_text: list[str] = []
        self._snippet_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {key.lower(): value or "" for key, value in attrs}
        classes = set(attrs_map.get("class", "").split())
        if tag == "li" and "b_algo" in classes:
            self._result_depth = 1
            self._snippet_parts = []
            return
        if self._result_depth:
            self._result_depth += 1
        if self._result_depth and tag == "a":
            href = _normalize_result_href(attrs_map.get("href", ""))
            if href:
                self._active_href = href
                self._active_text = []
                self._in_title_link = True
        elif self._result_depth and tag == "p":
            self._in_snippet = True
            self._snippet_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_title_link and self._active_href:
            self._active_text.append(data)
        elif self._in_snippet:
            self._snippet_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_title_link:
            title = _normalize_space("".join(self._active_text))
            if title and _looks_like_result_url(self._active_href):
                self.results.append(SearchResult(title=title, url=self._active_href))
            self._active_href = ""
            self._active_text = []
            self._in_title_link = False
        elif tag == "p" and self._in_snippet:
            snippet = _normalize_space("".join(self._snippet_parts))
            if snippet and self.results:
                previous = self.results[-1]
                if not previous.snippet and snippet != previous.title:
                    self.results[-1] = SearchResult(
                        title=previous.title,
                        url=previous.url,
                        snippet=snippet[:300],
                    )
            self._in_snippet = False
            self._snippet_parts = []
        if self._result_depth:
            self._result_depth -= 1

class PageTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.links: list[dict[str, str]] = []
        self._title_parts: list[str] = []
        self._text_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._active_link: str | None = None
        self._active_link_text: list[str] = []

    @property
    def text(self) -> str:
        return "\n".join(self._text_parts)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {key.lower(): value or "" for key, value in attrs}
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag == "a":
            href = attrs_map.get("href", "")
            if href.startswith(("http://", "https://")):
                self._active_link = href
                self._active_link_text = []
        if tag in {"p", "div", "section", "article", "br", "li", "h1", "h2", "h3"}:
            self._text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
        if self._active_link is not None:
            self._active_link_text.append(data)
        stripped = data.strip()
        if stripped:
            self._text_parts.append(stripped)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
            self.title = "".join(self._title_parts)
        elif tag == "a" and self._active_link is not None:
            text = _normalize_space("".join(self._active_link_text))
            if text:
                self.links.append({"text": text[:120], "url": self._active_link})
            self._active_link = None
            self._active_link_text = []


def _read_url_text(url: str, max_bytes: int) -> str:
    text, _content_type, _final_url = _read_url_text_with_metadata(url, max_bytes)
    return text


def _read_url_text_with_metadata(url: str, max_bytes: int) -> tuple[str, str, str]:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json,text/plain"})
    try:
        with urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            content_type = response.headers.get("Content-Type", "")
            body = response.read(max_bytes + 1)
            final_url = response.geturl()
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: {exc.reason}") from exc
    except URLError as exc:
        raise RuntimeError(f"网络请求失败：{exc.reason}") from exc

    charset = _charset_from_content_type(content_type)
    if len(body) > max_bytes:
        body = body[:max_bytes]
    try:
        return body.decode(charset, errors="replace"), content_type, final_url
    except LookupError:
        return body.decode("utf-8", errors="replace"), content_type, final_url


def _charset_from_content_type(content_type: str) -> str:
    for part in content_type.split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            return part.split("=", 1)[1].strip()
    return "utf-8"


def _normalize_result_href(href: str) -> str:
    href = html.unescape(href.strip())
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    elif href.startswith("/"):
        href = "https://www.bing.com" + href
    parsed = urlparse(href)
    if _is_bing_host(parsed.netloc) and parsed.path.startswith("/ck/"):
        target = _decode_bing_redirect_target(parsed)
        if target:
            href = target
    return href


def _looks_like_result_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = parsed.netloc.lower()
    return not _is_bing_host(host)


def _is_bing_host(host: str) -> bool:
    normalized = host.lower()
    return normalized == "bing.com" or normalized.endswith(".bing.com")


def _decode_bing_redirect_target(parsed_url: Any) -> str:
    raw_target = parse_qs(parsed_url.query).get("u", [""])[0]
    if not raw_target:
        return ""
    raw_target = unquote(raw_target)
    if raw_target.startswith(("http://", "https://")):
        return raw_target
    encoded = raw_target[2:] if raw_target.startswith("a1") else raw_target
    padding = "=" * (-len(encoded) % 4)
    try:
        decoded = base64.urlsafe_b64decode((encoded + padding).encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""
    return decoded if decoded.startswith(("http://", "https://")) else ""


def _has_cjk(text: str) -> bool:
    """Check if text contains CJK characters."""
    for ch in text:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF) or (0x3400 <= cp <= 0x4DBF) or (0x3000 <= cp <= 0x303F):
            return True
        if (0x3040 <= cp <= 0x309F) or (0x30A0 <= cp <= 0x30FF):
            return True
    return False


def _strip_latin_prefix(text: str) -> str:
    """Remove leading Latin letters, digits, spaces, and punctuation
    before the first CJK character.  Returns original if no CJK found.
    'G弦上的魔王' -> '弦上的魔王'.  'C语言' kept (1 Latin + 2 CJK compound)."""
    idx = -1
    for i, ch in enumerate(text):
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF) or (0x3040 <= cp <= 0x30FF):
            idx = i
            break
    if idx < 0:
        return text
    if idx == 0:
        return text
    prefix = text[:idx].rstrip()
    if not prefix:
        return text
    # Count consecutive CJK chars starting at idx
    cjk_run = 0
    for ch in text[idx:]:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF) or (0x3040 <= cp <= 0x30FF):
            cjk_run += 1
        else:
            break
    # Single Latin + 1-2 CJK = likely compound ("C语言", "A股", "B站", "U盘")
    if len(prefix) == 1 and cjk_run <= 2:
        return text
    return text[idx:].lstrip()


def _dedupe_results(results: list[SearchResult]) -> list[SearchResult]:
    seen: set[str] = set()
    deduped: list[SearchResult] = []
    for item in results:
        key = item.url.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _validate_public_http_url(url: str) -> str:
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("url 必须是完整的 http 或 https 地址。")
    host = parsed.hostname or ""
    if _is_blocked_host(host):
        raise ValueError("出于安全考虑，不允许读取本机或私有网络地址。")
    return url


def _is_blocked_host(host: str) -> bool:
    normalized = host.strip("[]").lower()
    if normalized in {"localhost"} or normalized.endswith(".localhost"):
        return True
    try:
        address = ip_address(normalized)
    except ValueError:
        return False
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _required_string(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} 必须是非空字符串。")
    return value


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("数值参数必须是整数。")
    if value < minimum or value > maximum:
        raise ValueError(f"数值参数必须在 {minimum}-{maximum} 之间。")
    return value


def _normalize_space(value: str) -> str:
    lines = [" ".join(line.split()) for line in html.unescape(value).splitlines()]
    return "\n".join(line for line in lines if line)


def _tool_result_response(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return _result_response(
        request_id,
        {
            "content": [{"type": "text", "text": text}],
            "structuredContent": payload,
            "isError": False,
        },
    )


def _result_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _write_message(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _detect_in_china() -> bool:
    """IP 归属检测，缓存结果。"""
    global _in_china_cache
    if _in_china_cache is not None:
        return _in_china_cache

    for url in ("https://myip.ipip.net", "https://cip.cc"):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=3) as resp:
                text = resp.read().decode("utf-8", errors="ignore")
                if "中国" in text or "CN" in text.upper():
                    _in_china_cache = True
                    return True
        except Exception:
            pass

    # Fallback: cn.bing.com reachability
    try:
        req = Request("https://cn.bing.com/", headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=3) as resp:
            _in_china_cache = True
            return True
    except Exception:
        pass

    _in_china_cache = False
    return False


def _resolve_pw_channel() -> str | None:
    """返回 Playwright channel；None 表示内置 Chromium。"""
    raw = str(os.environ.get("SAKURA_PW_CHANNEL", DEFAULT_PW_CHANNEL) or "").strip().lower()
    if raw in {"", "chromium", "bundled", "default"}:
        return None
    if raw in {"msedge", "edge"}:
        return "msedge"
    if raw in {"chrome", "google-chrome"}:
        return "chrome"
    return raw


def _resolve_pw_fetch_headed() -> bool:
    raw = str(os.environ.get("SAKURA_PW_FETCH_HEADED", "1" if DEFAULT_PW_FETCH_HEADED else "0")).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _ensure_pw_playwright():
    global _pw_playwright
    if _pw_playwright is None:
        from playwright.sync_api import sync_playwright
        _pw_playwright = sync_playwright().start()
    return _pw_playwright


def _launch_pw_browser(*, headless: bool):
    """启动本机 Edge（失败则回退内置 Chromium）。"""
    playwright = _ensure_pw_playwright()
    channel = _resolve_pw_channel()
    launch_kwargs: dict[str, Any] = {
        "headless": headless,
        "args": list(PW_BROWSER_ARGS),
    }
    if channel:
        launch_kwargs["channel"] = channel
    try:
        return playwright.chromium.launch(**launch_kwargs)
    except Exception:
        if channel:
            return playwright.chromium.launch(
                headless=headless,
                args=list(PW_BROWSER_ARGS),
            )
        raise


def _get_pw_browser():
    """搜索用：无头 Edge，避免搜的时候乱弹窗。"""
    global _pw_browser
    if _pw_browser is not None:
        return _pw_browser
    _pw_browser = _launch_pw_browser(headless=True)
    return _pw_browser


def _get_pw_fetch_browser():
    """读页用：默认有头本机 Edge，打开→抽正文→关页。"""
    global _pw_headed_browser, _pw_browser
    if _resolve_pw_fetch_headed():
        if _pw_headed_browser is not None:
            return _pw_headed_browser
        _pw_headed_browser = _launch_pw_browser(headless=False)
        return _pw_headed_browser
    return _get_pw_browser()


def _close_pw_browser() -> None:
    global _pw_browser, _pw_headed_browser, _pw_playwright, _PW_THREAD_POOL
    try:
        if _PW_THREAD_POOL:
            _PW_THREAD_POOL.shutdown(wait=False)
            _PW_THREAD_POOL = None
    except Exception:
        pass
    for attr in ("_pw_headed_browser", "_pw_browser"):
        browser = globals().get(attr)
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        globals()[attr] = None
    try:
        if _pw_playwright:
            _pw_playwright.stop()
            _pw_playwright = None
    except Exception:
        pass


def _pw_context():
    browser = _get_pw_browser()
    return browser.new_context(
        locale="zh-CN",
        user_agent=USER_AGENT,
        viewport={"width": 1920, "height": 1080},
        screen={"width": 1920, "height": 1080},
        device_scale_factor=1,
        timezone_id="Asia/Shanghai",
        has_touch=False,
        is_mobile=False,
        java_script_enabled=True,
        extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9"},
    )


def _search_bing_playwright(query: str, max_results: int) -> list[SearchResult]:
    """Playwright Bing 搜索 — 模拟浏览器行为，先去首页拿 session 再搜。"""
    browser = _get_pw_browser()
    out: list[SearchResult] = []
    seen: set[str] = set()
    ctx = _pw_context()
    try:
        page = ctx.new_page()
        page.route(PW_ROUTE_BLOCK, lambda r: r.abort())

        # Step 1: visit www.bing.com homepage first for proper session cookies.
        # Using the search box (not URL params) after homepage visit gives much
        # better results for mixed Latin/CJK queries.
        try:
            page.goto("https://www.bing.com/",
                      timeout=PW_HOME_TIMEOUT, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
        except Exception:
            try:
                page.goto("https://cn.bing.com/", timeout=PW_HOME_TIMEOUT, wait_until="domcontentloaded")
                page.wait_for_timeout(1000)
            except Exception:
                pass

        # Step 2: submit search via search box (much better results than URL params
        # for mixed Latin/CJK queries like "G弦上的魔王").
        try:
            search_box = page.query_selector("#sb_form_q")
            if search_box:
                search_box.click()
                search_box.fill(query)
                page.wait_for_timeout(300)
                page.keyboard.press("Enter")
                page.wait_for_load_state("domcontentloaded", timeout=PW_TIMEOUT)
                page.wait_for_timeout(2000)
            else:
                page.goto(
                    f"https://www.bing.com/search?q={quote(query)}&setlang=zh-cn&cc=cn&mkt=zh-CN",
                    timeout=PW_TIMEOUT, wait_until="domcontentloaded",
                )
                page.wait_for_timeout(1500)
        except Exception:
            page.goto(
                f"https://www.bing.com/search?q={quote(query)}&setlang=zh-cn&cc=cn&mkt=zh-CN",
                timeout=PW_TIMEOUT, wait_until="domcontentloaded",
            )
            page.wait_for_timeout(1500)

        raw = page.evaluate("""() => {
            const items = [];
            document.querySelectorAll('li.b_algo').forEach(el => {
                try {
                    const a = el.querySelector('h2 a');
                    if (!a || !a.href || !a.href.startsWith('http')) return;
                    const title = (a.innerText || a.textContent || '').trim();
                    if (!title || title.length < 3) return;
                    // Snippet: .b_caption p (most common), fallback .b_caption > text
                    let snippet = '';
                    const capP = el.querySelector('.b_caption p');
                    if (capP) snippet = (capP.innerText || capP.textContent || '').trim();
                    if (!snippet) {
                        const cap = el.querySelector('.b_caption');
                        if (cap) snippet = (cap.innerText || cap.textContent || '').trim();
                    }
                    items.push({title, url: a.href.trim(), snippet});
                } catch(e) {}
            });
            return items;
        }""")

        for r in raw:
            url = r["url"]
            if r["title"] and url and url not in seen and len(r["title"]) > 3:
                seen.add(url)
                out.append(SearchResult(title=r["title"], url=url, snippet=r["snippet"]))

        # For CJK queries, most of the first-page results should contain CJK.
        # If too few do, Bing likely misinterpreted the query (e.g. "G" → "Logitech").
        # Retry with leading Latin/numeric chars stripped.
        cjk_titles = sum(1 for r in out if _has_cjk(r.title))
        stripped = _strip_latin_prefix(query)
        if (len(out) >= 3 and cjk_titles < 2 and stripped and stripped != query
                and _has_cjk(stripped)):
            try:
                # Go back to homepage and search again with search box
                page.goto("https://www.bing.com/", timeout=PW_HOME_TIMEOUT, wait_until="domcontentloaded")
                page.wait_for_timeout(1000)
                search_box2 = page.query_selector("#sb_form_q")
                if search_box2:
                    search_box2.click()
                    search_box2.fill(stripped)
                    page.wait_for_timeout(300)
                    page.keyboard.press("Enter")
                    page.wait_for_load_state("domcontentloaded", timeout=PW_TIMEOUT)
                    page.wait_for_timeout(2000)
                else:
                    page.goto(
                        f"https://www.bing.com/search?q={quote(stripped)}&setlang=zh-cn&cc=cn&mkt=zh-CN",
                        timeout=PW_TIMEOUT, wait_until="domcontentloaded",
                    )
                    page.wait_for_timeout(1500)
                raw2 = page.evaluate("""() => {
                    const items = [];
                    document.querySelectorAll('li.b_algo').forEach(el => {
                        try {
                            const a = el.querySelector('h2 a');
                            if (!a || !a.href || !a.href.startsWith('http')) return;
                            const title = (a.innerText || a.textContent || '').trim();
                            if (!title || title.length < 3) return;
                            let snippet = '';
                            const capP = el.querySelector('.b_caption p');
                            if (capP) snippet = (capP.innerText || capP.textContent || '').trim();
                            if (!snippet) {
                                const cap = el.querySelector('.b_caption');
                                if (cap) snippet = (cap.innerText || cap.textContent || '').trim();
                            }
                            items.push({title, url: a.href.trim(), snippet});
                        } catch(e) {}
                    });
                    return items;
                }""")
                cleaned: list[SearchResult] = []
                for r in raw2:
                    url = r["url"]
                    if r["title"] and url and url not in seen and len(r["title"]) > 3:
                        seen.add(url)
                        cleaned.append(SearchResult(title=r["title"], url=url, snippet=r["snippet"]))
                out = cleaned + out
            except Exception:
                pass
    finally:
        try:
            ctx.close()
        except Exception:
            pass
    return out[:max_results]


def _search_duckduckgo_playwright(query: str, max_results: int) -> list[SearchResult]:
    """Playwright DuckDuckGo 搜索。"""
    browser = _get_pw_browser()
    out: list[SearchResult] = []
    seen: set[str] = set()
    ctx = browser.new_context(
        locale="en-US",
        user_agent=USER_AGENT,
        viewport={"width": 1920, "height": 1080},
        java_script_enabled=True,
    )
    try:
        page = ctx.new_page()
        page.route(PW_ROUTE_BLOCK, lambda r: r.abort())

        try:
            page.goto("https://duckduckgo.com/", timeout=PW_HOME_TIMEOUT, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
        except Exception:
            pass

        try:
            search_box = page.query_selector('input[name="q"]')
            if search_box:
                search_box.click()
                search_box.fill(query)
                page.wait_for_timeout(300)
                page.keyboard.press("Enter")
                page.wait_for_load_state("domcontentloaded", timeout=PW_TIMEOUT)
                page.wait_for_timeout(2500)
            else:
                page.goto(f"https://duckduckgo.com/?q={quote(query)}",
                          timeout=PW_TIMEOUT, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
        except Exception:
            page.goto(f"https://duckduckgo.com/?q={quote(query)}",
                      timeout=PW_TIMEOUT, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

        raw = page.evaluate("""() => {
            const items = [];
            const seen = new Set();
            const add = (title, url, snippet) => {
                if (title && url && url.startsWith('http') && !seen.has(url)) {
                    seen.add(url);
                    items.push({title, url, snippet});
                }
            };
            const sels = ['article[data-testid="result"]', 'li[data-layout="organic"]', '.result', '.web-result'];
            for (const sel of sels) {
                document.querySelectorAll(sel).forEach(el => {
                    const a = el.querySelector('a[href^="http"]');
                    const t = el.querySelector('h2, [data-testid="result-title-a"], .result__a, .result__title');
                    const s = el.querySelector('[data-testid="result-snippet"], .result__snippet, .result__body');
                    if (a && t) add((t.innerText || t.textContent || '').trim(), a.href,
                                    s ? (s.innerText || s.textContent || '').trim() : '');
                });
                if (items.length > 0) break;
            }
            return items;
        }""")

        for r in raw:
            url = r["url"]
            if r["title"] and url and url not in seen and len(r["title"]) > 3:
                seen.add(url)
                out.append(SearchResult(title=r["title"], url=url, snippet=r["snippet"]))
    finally:
        try:
            ctx.close()
        except Exception:
            pass
    return out[:max_results]


def _search_baidu_playwright(query: str, max_results: int) -> list[SearchResult]:
    """Playwright 百度搜索 — 中文社区内容索引最佳。每次使用独立 context 避免反爬。"""
    browser = _get_pw_browser()
    out: list[SearchResult] = []
    seen: set[str] = set()
    # Fresh context per search to avoid Baidu anti-bot
    ctx = browser.new_context(
        locale="zh-CN",
        user_agent=USER_AGENT,
        viewport={"width": 1920, "height": 1080},
        screen={"width": 1920, "height": 1080},
        device_scale_factor=1,
        timezone_id="Asia/Shanghai",
        has_touch=False,
        is_mobile=False,
        java_script_enabled=True,
        extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9"},
    )
    try:
        page = ctx.new_page()
        page.route(PW_ROUTE_BLOCK, lambda r: r.abort())

        page.goto(
            f"https://www.baidu.com/s?wd={quote(query)}&rn={max_results}",
            timeout=PW_TIMEOUT,
            wait_until="domcontentloaded",
        )
        page.wait_for_timeout(2000)

        # Check for anti-bot
        page_title = (page.title() or "").strip()
        if "安全验证" in page_title or "验证" in page_title:
            return []  # Baidu anti-bot — caller will fall through to Bing

        raw = page.evaluate("""() => {
            const items = [];
            const seen = new Set();
            document.querySelectorAll('.result, .c-result, .result-op, .c-container').forEach(el => {
                try {
                    // Title link
                    let a = el.querySelector('h3 a, .t a');
                    if (!a) {
                        const links = el.querySelectorAll('a[href]');
                        for (const link of links) {
                            const h = link.getAttribute('href') || '';
                            if (h.includes('baidu.com/link') || h.startsWith('http')) {
                                a = link;
                                break;
                            }
                        }
                    }
                    if (!a) return;
                    const href = (a.getAttribute('href') || '').trim();
                    if (!href) return;
                    const title = (a.innerText || a.textContent || '').trim();
                    if (!title || title.length < 3 || seen.has(href)) return;
                    seen.add(href);

                    // Snippet
                    let snippet = '';
                    const s = el.querySelector('.c-abstract, .c-span-last, .c-gap-top-small span, .content-right_8Zs40');
                    if (s) snippet = (s.innerText || s.textContent || '').trim();
                    if (!snippet) {
                        const text = (el.innerText || el.textContent || '').replace(title, '').trim();
                        snippet = text.substring(0, 200);
                    }
                    items.push({title, url: href, snippet});
                } catch(e) {}
            });
            return items;
        }""")

        for r in raw:
            url = r["url"]
            if r["title"] and url and url not in seen and len(r["title"]) > 3:
                seen.add(url)
                out.append(SearchResult(title=r["title"], url=url, snippet=r["snippet"]))
    finally:
        try:
            ctx.close()
        except Exception:
            pass
    return out[:max_results]


def _fetch_url_playwright(url: str, max_chars: int) -> dict[str, Any]:
    """Playwright 读页：默认弹出本机 Edge，抽完正文后关掉标签页。"""
    headed = _resolve_pw_fetch_headed()
    browser = _get_pw_fetch_browser()
    ctx = browser.new_context(
        locale="zh-CN",
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 840},
    )
    try:
        page = ctx.new_page()
        # 有头模式少拦资源，页面更像正常浏览；无头仍拦大文件加速。
        if not headed:
            page.route(PW_ROUTE_BLOCK, lambda r: r.abort())
        page.goto(url, timeout=PW_FETCH_TIMEOUT, wait_until="domcontentloaded")
        page.wait_for_timeout(1000 if headed else 800)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

        text = page.evaluate("""() => {
            document.querySelectorAll('script,style,nav,header,footer,.ad,.ads,[class*="banner"],[id*="banner"],.sidebar,.comment,.popup,.modal,.cookie').forEach(e=>e.remove());
            for (const sel of ['article','main','.content','.post','.article','#content','#main','.entry-content','.post-content','[itemprop="articleBody"]']) {
                const m = document.querySelector(sel);
                if (m && m.innerText.length > 200) return m.innerText;
            }
            return document.body ? document.body.innerText : '';
        }""")
        title = page.title() or ""
        final_url = page.url
        if headed:
            # 稍留一眼再收起，贴近「打开→读→关」的过程感。
            page.wait_for_timeout(600)
    finally:
        try:
            ctx.close()
        except Exception:
            pass

    clean = _normalize_space(text or "")
    return {
        "url": final_url,
        "content_type": "text/html",
        "title": _normalize_space(title),
        "text": clean[:max_chars],
        "truncated": len(clean) > max_chars,
        "links": [],
        "headed": headed,
    }


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        _close_pw_browser()
