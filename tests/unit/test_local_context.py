from __future__ import annotations

from app.agent.local_context import build_media_context_fragment
from app.perception.media_session import (
    MediaSessionSnapshot,
    format_media_context_prompt,
    local_context_relevance,
)


def test_local_context_relevance_gates() -> None:
    assert local_context_relevance("现在几点了？")["time"]
    assert not local_context_relevance("今天心情怎么样")["media"]
    assert local_context_relevance("我在听什么歌？")["media"]


def test_media_fragment_skipped_when_irrelevant(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.agent.local_context.read_media_session_snapshot",
        lambda: MediaSessionSnapshot(available=True, title="晴天", artist="周杰伦", playing=True),
    )
    assert build_media_context_fragment("你好") is None


def test_media_fragment_injected_when_relevant(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.agent.local_context.read_media_session_snapshot",
        lambda: MediaSessionSnapshot(
            available=True,
            title="晴天",
            artist="周杰伦",
            playing=True,
            source="Spotify.exe",
            playback_status="Playing",
        ),
    )
    fragment = build_media_context_fragment("我在听什么歌？")
    assert fragment is not None
    assert fragment.trust == "untrusted"
    assert "晴天" in fragment.content
    assert "不要把正在播放" in format_media_context_prompt(
        MediaSessionSnapshot(available=True, title="晴天", artist="周杰伦", playing=True)
    )
