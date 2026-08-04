from __future__ import annotations

from app.agent.memory_evidence import (
    build_dialog_corpus,
    evidence_in_corpus,
    looks_like_transient_local_memory,
    soft_grounded_in_corpus,
    validate_memory_write_grounding,
)


def test_evidence_substring_and_normalized_match() -> None:
    corpus = "以后默认中文和我说话\n……わかった。"
    assert evidence_in_corpus("默认中文", corpus)
    assert evidence_in_corpus("默认 中文", corpus)


def test_soft_ground_accepts_paraphrase() -> None:
    corpus = "以后默认中文和我说话"
    assert soft_grounded_in_corpus("他希望默认用中文交流", corpus)


def test_ungrounded_hallucination_rejected() -> None:
    corpus = "今天天气不错，要不要一起散步"
    ok, reason = validate_memory_write_grounding(
        "他住在火星基地，有三只机械猫",
        evidence="",
        dialog_corpus=corpus,
    )
    assert not ok
    assert reason == "ungrounded"


def test_fake_evidence_rejected_even_if_content_overlaps() -> None:
    corpus = "以后默认中文和我说话"
    ok, reason = validate_memory_write_grounding(
        "他希望默认用中文交流",
        evidence="我有秘密密码是123456",
        dialog_corpus=corpus,
    )
    assert not ok
    assert reason == "evidence_mismatch"


def test_valid_evidence_passes() -> None:
    corpus = "今晚十点一起休息吧"
    ok, reason = validate_memory_write_grounding(
        "我和他约定今晚十点休息",
        evidence="今晚十点一起休息吧",
        dialog_corpus=corpus,
    )
    assert ok
    assert reason == "evidence"


def test_transient_local_memory_rejected() -> None:
    assert looks_like_transient_local_memory("当前本地时间：2026-08-03T00:00:00")
    assert looks_like_transient_local_memory("他正在播放周杰伦的歌")
    ok, reason = validate_memory_write_grounding(
        "当前正在播放的歌曲是晴天",
        evidence="当前正在播放的歌曲是晴天",
        dialog_corpus="当前正在播放的歌曲是晴天",
    )
    assert not ok
    assert reason == "transient_local"


def test_build_dialog_corpus_includes_translation() -> None:
    corpus = build_dialog_corpus(
        [
            {"role": "user", "content": "hello", "translation": "你好"},
            {"role": "assistant", "content": "こんばんは", "translation": "晚上好"},
        ]
    )
    assert "hello" in corpus
    assert "你好" in corpus
    assert "こんばんは" in corpus
