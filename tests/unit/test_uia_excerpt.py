# -*- coding: utf-8 -*-
"""UIA excerpt curation + ObservationPacket text priority."""

from app.perception.observer import (
    format_visible_text_block,
    infer_content_scene,
    resolve_visible_text,
    resolve_visible_text_excerpt,
)
from app.perception.screen_reader import curate_uia_excerpt


def test_curate_dedupes_and_drops_short_nav_when_long_enough() -> None:
    long_a = "AI roommate story chapter one headline on page"
    long_b = "She stared at the screen and felt a little annoyed by it"
    text = "\n".join(
        [
            "Home",
            long_a,
            "Home",
            "Next",
            long_b,
            "Ad",
        ]
    )
    out = curate_uia_excerpt(text, min_chars=30)
    assert long_a in out
    assert long_b in out
    assert out.count(long_a) == 1
    # Long lines alone already satisfy min_chars -> short chrome dropped
    assert "Home" not in out
    assert "Next" not in out
    assert "Ad" not in out


def test_curate_keeps_short_lines_when_needed_for_min_chars() -> None:
    text = "hi\nok\nyes"
    out = curate_uia_excerpt(text, min_chars=5, min_line_chars=16)
    assert "hi" in out
    assert "ok" in out


def test_curate_head_tail_truncation() -> None:
    head = "TITLE:" + ("A" * 200)
    middle = "MIDDLE:" + ("B" * 900)
    tail = "LATEST:" + ("C" * 200)
    text = "\n".join([head, middle, tail])
    out = curate_uia_excerpt(text, max_chars=1200, min_chars=30)
    assert len(out) <= 1210
    assert "…" in out
    assert "TITLE:" in out
    assert "LATEST:" in out


def test_curate_returns_empty_when_too_short() -> None:
    assert curate_uia_excerpt("x", min_chars=30) == ""
    assert curate_uia_excerpt("", min_chars=30) == ""


def test_resolve_prefers_uia_over_on_screen_text() -> None:
    uia = "This is a sufficiently long UIA body excerpt for the min char gate."
    on_screen = "This is VLM on-screen text and must not win."
    out = resolve_visible_text_excerpt(
        uia_text=uia,
        on_screen_text=on_screen,
        min_chars=30,
    )
    assert "UIA body" in out
    assert "VLM" not in out


def test_resolve_falls_back_to_on_screen_when_uia_empty() -> None:
    out = resolve_visible_text_excerpt(
        uia_text="",
        on_screen_text="Key on-screen phrase: AI roommate story",
        min_chars=30,
    )
    assert "AI roommate story" in out


def test_resolve_prefers_ocr_over_on_screen() -> None:
    out = resolve_visible_text_excerpt(
        uia_text="x",
        ocr_text="OCR captured a long enough game dialogue line right here.",
        on_screen_text="VLM copied text",
        min_chars=30,
    )
    assert "OCR" in out
    assert "VLM" not in out


def test_resolve_visible_text_tracks_source() -> None:
    uia = resolve_visible_text(
        uia_text="This is a sufficiently long UIA body excerpt for the min char gate.",
        on_screen_text="VLM",
        min_chars=30,
    )
    assert uia.source == "uia"
    ocr = resolve_visible_text(
        uia_text="x",
        ocr_text="OCR captured a long enough game dialogue line right here.",
        min_chars=30,
    )
    assert ocr.source == "ocr"
    vlm = resolve_visible_text(on_screen_text="VLM phrase", min_chars=30)
    assert vlm.source == "vlm_on_screen"


def test_infer_content_scene_chat_game_ai() -> None:
    assert infer_content_scene("chat", "WeChat.exe", "微信") == "chat"
    assert infer_content_scene("", "YuanShen.exe", "原神") == "game"
    assert (
        infer_content_scene("browser", "chrome.exe", "ChatGPT - Google Chrome")
        == "ai_assistant"
    )
    assert infer_content_scene("editor", "Code.exe", "main.py") == "editor"


def test_format_visible_text_block_labels_provenance() -> None:
    block = format_visible_text_block(
        "hello from wechat friend",
        source="uia",
        scene="chat",
    )
    assert "我看见的" in block
    assert "即时通讯" in block
    assert "hello from wechat friend" in block
