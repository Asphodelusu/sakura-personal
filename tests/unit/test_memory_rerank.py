"""记忆候选 cross-encoder 重排。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent import memory_rerank as rerank_module
from app.agent.memory_rerank import (
    RERANKER_CATALOG,
    rerank_memory_candidates,
    reranker_model_cached,
    reset_memory_reranker_for_tests,
    selected_reranker_model,
    set_selected_reranker_model,
    warm_memory_reranker,
)


def setup_function() -> None:
    reset_memory_reranker_for_tests()


def test_rerank_reorders_by_injected_scorer() -> None:
    memories = [
        {"id": "a", "content": "他喜欢火锅", "score": 0.9},
        {"id": "b", "content": "他喜欢椰子水", "score": 0.4},
        {"id": "c", "content": "昨天聊了 Cursor", "score": 0.8},
    ]

    def scorer(query: str, texts: list[str]) -> list[float]:
        assert "椰子" in query
        # 原始 logit：中间那条最高
        return [0.0, 3.0, -1.0]

    ranked = rerank_memory_candidates(
        "他喜欢喝什么椰子水吗",
        memories,
        scorer=scorer,
    )
    assert [item["id"] for item in ranked[:3]] == ["b", "a", "c"]
    assert ranked[0]["rerank_score"] > ranked[1]["rerank_score"]
    assert ranked[0]["semantic_score"] == 0.4
    assert 0.0 < ranked[0]["score"] <= 1.0


def test_rerank_skips_when_too_few_candidates() -> None:
    memories = [{"id": "a", "content": "只有一条", "score": 0.5}]
    out = rerank_memory_candidates(
        "query",
        memories,
        scorer=lambda _q, texts: [9.0] * len(texts),
    )
    assert out is memories


def test_rerank_disabled_by_env(monkeypatch) -> None:
    monkeypatch.setenv("SAKURA_MEMORY_RERANK", "0")
    memories = [
        {"id": "a", "content": "甲", "score": 0.9},
        {"id": "b", "content": "乙", "score": 0.1},
    ]
    out = rerank_memory_candidates(
        "query",
        memories,
        scorer=lambda _q, texts: [0.0, 5.0],
    )
    assert out is memories


def test_warm_and_resolve_never_auto_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    def boom(*_a: object, **_k: object) -> str:
        calls.append(True)
        raise AssertionError("不应自动下载精排模型")

    monkeypatch.setattr(rerank_module, "_resolve_reranker_snapshot", lambda *_a, **_k: None)
    monkeypatch.setattr("app.core.hf_hub_download.download_hf_snapshot", boom)
    assert reranker_model_cached(tmp_path) is False
    warm_memory_reranker(tmp_path)
    out = rerank_memory_candidates(
        "椰子水",
        [
            {"id": "a", "content": "他喜欢火锅", "score": 0.9},
            {"id": "b", "content": "他喜欢椰子水", "score": 0.4},
        ],
        base_dir=tmp_path,
    )
    assert [item["id"] for item in out] == ["a", "b"]
    assert calls == []


def test_select_reranker_model_persists_catalog_choice(tmp_path: Path) -> None:
    assert len(RERANKER_CATALOG) >= 2
    chosen = RERANKER_CATALOG[0].model_id
    assert set_selected_reranker_model(tmp_path, chosen) == chosen
    assert selected_reranker_model(tmp_path) == chosen
    with pytest.raises(ValueError, match="不支持"):
        set_selected_reranker_model(tmp_path, "not-a-real/model")
