"""持久化实体索引：多跳召回用的「实体 → 记忆 id」倒排索引。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.entity_index import EntityIndex, extract_entities


def test_extract_entities_katakana_and_honorifics() -> None:
    entities = extract_entities("ソフィアくんは今日も元気だった。カシマさんも一緒だった。")
    assert "ソフィア" in entities
    assert "カシマ" in entities


def test_extract_entities_english_name() -> None:
    entities = extract_entities("I talked to John Smith yesterday.")
    assert "John Smith" in entities


def test_extract_entities_skips_stopwords_and_short_tokens() -> None:
    entities = extract_entities("私は彼女と話した。")
    assert "私" not in entities
    assert "彼女" not in entities


@pytest.fixture()
def index(tmp_path: Path) -> EntityIndex:
    idx = EntityIndex(tmp_path / "entity_index.db")
    yield idx
    idx.close()


def test_index_and_lookup_roundtrip(index: EntityIndex) -> None:
    index.index_memory("m1", "ソフィアは挚友，会帮我翻译日语。", updated_at="2026-01-01T00:00:00")
    index.index_memory("m2", "今天和ソフィア一起吃了午饭。", updated_at="2026-01-02T00:00:00")
    index.index_memory("m3", "完全不相关的一条记忆。", updated_at="2026-01-03T00:00:00")

    hits = index.lookup_memory_ids(["ソフィア"])
    assert set(hits) == {"m1", "m2"}
    # 按最近更新时间倒序
    assert hits[0] == "m2"


def test_lookup_excludes_ids(index: EntityIndex) -> None:
    index.index_memory("m1", "ソフィア是挚友。", updated_at="2026-01-01T00:00:00")
    index.index_memory("m2", "ソフィア也是同学。", updated_at="2026-01-02T00:00:00")
    hits = index.lookup_memory_ids(["ソフィア"], exclude_ids=["m2"])
    assert hits == ["m1"]


def test_lookup_empty_entities_returns_empty(index: EntityIndex) -> None:
    assert index.lookup_memory_ids([]) == []


def test_reindexing_memory_replaces_old_entities(index: EntityIndex) -> None:
    """记忆被更新后重新索引：旧内容里的实体不应再关联到这条记忆。"""
    index.index_memory("m1", "ソフィア是挚友。", updated_at="2026-01-01T00:00:00")
    assert index.lookup_memory_ids(["ソフィア"]) == ["m1"]

    index.index_memory("m1", "完全换了内容，不再提任何名字。", updated_at="2026-01-02T00:00:00")
    assert index.lookup_memory_ids(["ソフィア"]) == []


def test_remove_memory(index: EntityIndex) -> None:
    index.index_memory("m1", "ソフィア是挚友。", updated_at="2026-01-01T00:00:00")
    index.remove_memory("m1")
    assert index.lookup_memory_ids(["ソフィア"]) == []


def test_backfill_marker_roundtrip(tmp_path: Path) -> None:
    idx = EntityIndex(tmp_path / "entity_index.db")
    assert idx.is_backfilled() is False
    idx.mark_backfilled()
    assert idx.is_backfilled() is True
    idx.close()

    # 重新打开同一个文件，标记应该持久化下来
    idx2 = EntityIndex(tmp_path / "entity_index.db")
    assert idx2.is_backfilled() is True
    idx2.close()
