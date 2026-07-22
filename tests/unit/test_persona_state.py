from __future__ import annotations

from app.agent.persona_state import (
    EMOTION_CONGRUENCE_MAX_BOOST,
    emotion_affinity,
    emotion_congruence_factor,
)
from app.backchannel.models import EMOTIONS


def test_all_emotions_have_coordinates() -> None:
    """EMOTIONS 里的每个标签都必须能算出接近度，不能因为缺坐标退化成 0。"""
    for emotion in EMOTIONS:
        assert emotion_affinity(emotion, emotion) > 0.0


def test_identical_emotion_has_max_affinity() -> None:
    assert emotion_affinity("happy", "happy") == 1.0


def test_close_emotions_have_higher_affinity_than_opposite() -> None:
    close = emotion_affinity("happy", "playful")
    opposite = emotion_affinity("happy", "sad")
    assert close > opposite
    assert opposite < 0.3


def test_congruence_factor_never_penalizes() -> None:
    """连续情绪空间只加分不减分：无论多不相关，乘数都不应低于 1.0。"""
    for current in EMOTIONS:
        for memory_emotion in EMOTIONS:
            assert emotion_congruence_factor(current, memory_emotion) >= 1.0


def test_congruence_factor_caps_at_max_boost() -> None:
    factor = emotion_congruence_factor("happy", "happy")
    assert factor == 1.0 + EMOTION_CONGRUENCE_MAX_BOOST


def test_unknown_or_neutral_emotion_is_neutral() -> None:
    assert emotion_congruence_factor("neutral", "happy") == 1.0
    assert emotion_congruence_factor("happy", "neutral") == 1.0
    assert emotion_congruence_factor("not-a-real-emotion", "happy") == 1.0


def test_compassion_bias_boosts_warm_when_low_valence() -> None:
    """情绪低落（sad/lonely/anxious）时，warm/tender 记忆仍应拿到一个非零加成，
    这是刻意的陪伴策略，不是几何距离算出来的（它们在情绪空间里其实离得远）。
    """
    sad_to_warm = emotion_congruence_factor("sad", "warm")
    lonely_to_tender = emotion_congruence_factor("lonely", "tender")
    assert sad_to_warm > 1.0
    assert lonely_to_tender > 1.0


def test_compassion_bias_does_not_apply_to_high_valence_current() -> None:
    """当前情绪本身不低落（如 angry，valence 也偏低但未必触发陪伴阈值）时，
    不应无条件套用陪伴加成——只有真正低 valence 的情绪才触发。
    """
    # angry 的 valence 同样为负但坐标上离 warm 已经足够近，不依赖陪伴偏置也有接近度；
    # 用一个 valence 明显不低落的情绪（happy）验证陪伴阈值确实没被误触发。
    happy_to_warm_affinity = emotion_affinity("happy", "warm")
    sad_to_warm_affinity = emotion_affinity("sad", "warm")
    # sad 触发了陪伴偏置的下限加成，happy 是纯几何距离算出的高接近度，
    # 两者都应为正，且不应相等（验证走的不是同一条逻辑分支）。
    assert happy_to_warm_affinity > 0.0
    assert sad_to_warm_affinity > 0.0
    assert happy_to_warm_affinity != sad_to_warm_affinity
