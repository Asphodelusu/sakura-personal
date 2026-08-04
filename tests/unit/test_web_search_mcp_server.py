from __future__ import annotations

import pytest

from app.agent.mcp.web_search_server import (
    BingSearchParser,
    search_web,
    _normalize_result_href,
    _validate_public_http_url,
    handle_message,
)


def test_bing_result_href_is_unwrapped() -> None:
    href = (
        "https://www.bing.com/ck/a?u="
        "a1aHR0cHM6Ly9leGFtcGxlLmNvbS9kb2NzP2E9MQ"
    )

    assert _normalize_result_href(href) == "https://example.com/docs?a=1"


def test_bing_search_parser_extracts_result() -> None:
    parser = BingSearchParser()

    parser.feed(
        """
        <html>
          <ol>
            <li class="b_algo">
              <h2><a href="https://example.com">Example</a></h2>
              <div><p>Example snippet</p></div>
            </li>
          </ol>
        </html>
        """
    )

    assert len(parser.results) == 1
    assert parser.results[0].title == "Example"
    assert parser.results[0].url == "https://example.com"
    assert parser.results[0].snippet == "Example snippet"


def test_fetch_url_blocks_local_network_addresses() -> None:
    for url in [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://192.168.1.1",
        "file:///C:/Users/test.txt",
    ]:
        try:
            _validate_public_http_url(url)
        except ValueError:
            continue
        raise AssertionError(f"should reject {url}")


def test_tools_list_response_contains_web_search_tools() -> None:
    response = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    assert response is not None
    names = {tool["name"] for tool in response["result"]["tools"]}
    assert names == {"web_search", "fetch_url"}


def test_search_web_prefers_zhipu_when_key_present(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent.mcp import web_search_server as mod
    from app.agent.mcp.web_search_server import SearchResult

    monkeypatch.setattr(mod, "_resolve_zhipu_api_key", lambda: "test-key")
    monkeypatch.setattr(mod, "_zhipu_search_engine", lambda: "search_pro")

    def fake_zhipu(query: str, max_results: int, *, api_key: str):
        assert query == "夜乃樱"
        assert api_key == "test-key"
        return [SearchResult(title="示例", url="https://example.com/a", snippet="摘要")]

    monkeypatch.setattr(mod, "_search_zhipu_web", fake_zhipu)

    def fail_playwright(*_args, **_kwargs):
        raise AssertionError("should not fall back when Zhipu succeeds")

    monkeypatch.setattr(mod, "_detect_in_china", fail_playwright)

    payload = search_web("夜乃樱", max_results=3)
    assert payload["source"].startswith("Zhipu")
    assert payload["results"][0]["url"] == "https://example.com/a"


def test_search_web_falls_back_when_zhipu_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent.mcp import web_search_server as mod
    from app.agent.mcp.web_search_server import SearchResult

    monkeypatch.setattr(mod, "_resolve_zhipu_api_key", lambda: "test-key")
    monkeypatch.setattr(
        mod,
        "_search_zhipu_web",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(mod, "_detect_in_china", lambda: True)
    monkeypatch.setattr(
        mod,
        "_search_baidu_playwright",
        lambda query, max_results: [
            SearchResult(title="百度结果", url="https://baidu.example/x", snippet="ok")
        ],
    )
    monkeypatch.setattr(mod, "_run_in_thread", lambda fn, *a, **k: fn(*a, **k))

    payload = search_web("测试", max_results=2)
    assert payload["source"].startswith("Baidu")
    assert payload["results"][0]["title"] == "百度结果"


def test_resolve_pw_channel_defaults_to_msedge(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent.mcp import web_search_server as mod

    monkeypatch.delenv("SAKURA_PW_CHANNEL", raising=False)
    assert mod._resolve_pw_channel() == "msedge"
    monkeypatch.setenv("SAKURA_PW_CHANNEL", "edge")
    assert mod._resolve_pw_channel() == "msedge"
    monkeypatch.setenv("SAKURA_PW_CHANNEL", "chrome")
    assert mod._resolve_pw_channel() == "chrome"
    monkeypatch.setenv("SAKURA_PW_CHANNEL", "chromium")
    assert mod._resolve_pw_channel() is None


def test_get_pw_browser_prefers_msedge_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    from app.agent.mcp import web_search_server as mod

    mod._pw_browser = None
    mod._pw_headed_browser = None
    mod._pw_playwright = None
    monkeypatch.delenv("SAKURA_PW_CHANNEL", raising=False)

    launched: list[dict] = []

    class FakeChromium:
        def launch(self, **kwargs):
            launched.append(kwargs)
            return object()

    class FakePlaywright:
        chromium = FakeChromium()

        def start(self):
            return self

        def stop(self):
            return None

    fake_mod = types.ModuleType("playwright.sync_api")
    fake_mod.sync_playwright = lambda: FakePlaywright()  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(__import__("sys").modules, "playwright.sync_api", fake_mod)

    browser = mod._get_pw_browser()
    assert browser is not None
    assert launched and launched[0].get("channel") == "msedge"
    assert launched[0].get("headless") is True
    mod._close_pw_browser()


def test_fetch_browser_defaults_to_headed_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    from app.agent.mcp import web_search_server as mod

    mod._pw_browser = None
    mod._pw_headed_browser = None
    mod._pw_playwright = None
    monkeypatch.delenv("SAKURA_PW_CHANNEL", raising=False)
    monkeypatch.delenv("SAKURA_PW_FETCH_HEADED", raising=False)

    launched: list[dict] = []

    class FakeChromium:
        def launch(self, **kwargs):
            launched.append(kwargs)
            return object()

    class FakePlaywright:
        chromium = FakeChromium()

        def start(self):
            return self

        def stop(self):
            return None

    fake_mod = types.ModuleType("playwright.sync_api")
    fake_mod.sync_playwright = lambda: FakePlaywright()  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(__import__("sys").modules, "playwright.sync_api", fake_mod)

    assert mod._resolve_pw_fetch_headed() is True
    browser = mod._get_pw_fetch_browser()
    assert browser is not None
    assert launched and launched[0].get("channel") == "msedge"
    assert launched[0].get("headless") is False
    mod._close_pw_browser()
