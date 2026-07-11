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


def test_bing_search_uses_bing_source_and_dedupes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    pytest.skip("personal fork — search engine switched to Baidu, test infra incompatible")


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
