from __future__ import annotations

from dataclasses import dataclass

from app.backchannel.emotion import EmotionScorer
from app.backchannel.models import DEFAULT_EMOTION, EMOTIONS

# 情绪一致性：相近情绪给部分加权，避免只有完全匹配才生效。
_EMOTION_AFFINITY: dict[tuple[str, str], float] = {
    ("sad", "anxious"): 0.65,
    ("anxious", "sad"): 0.65,
    ("frustrated", "angry"): 0.55,
    ("angry", "frustrated"): 0.55,
    ("happy", "playful"): 0.75,
    ("playful", "happy"): 0.75,
    ("embarrassed", "anxious"): 0.4,
    ("confused", "anxious"): 0.35,
    ("sad", "lonely"): 0.7,
    ("lonely", "sad"): 0.7,
    ("happy", "warm"): 0.7,
    ("warm", "happy"): 0.7,
    ("warm", "tender"): 0.75,
    ("tender", "warm"): 0.75,
    ("hopeful", "happy"): 0.55,
    ("happy", "hopeful"): 0.55,
    ("defensive", "frustrated"): 0.5,
    ("frustrated", "defensive"): 0.5,
    ("determined", "hopeful"): 0.45,
    ("hopeful", "determined"): 0.45,
}

EMOTION_CONGRUENCE_MAX_BOOST = 0.14


@dataclass(frozen=True)
class PersonaState:
    """轻量 persona 快照：用户当下情绪 + Sakura 心情日记粗估。"""

    user_emotion: str = DEFAULT_EMOTION
    sakura_mood_emotion: str = DEFAULT_EMOTION

    def active_emotion(self) -> str:
        if self.user_emotion != DEFAULT_EMOTION:
            return self.user_emotion
        if self.sakura_mood_emotion != DEFAULT_EMOTION:
            return self.sakura_mood_emotion
        return DEFAULT_EMOTION


def normalize_emotion(value: str | None) -> str:
    text = str(value or "").strip().lower()
    return text if text in EMOTIONS else DEFAULT_EMOTION


def resolve_persona_state(
    *,
    dialogue_text: str,
    mood_content: str = "",
    scorer: EmotionScorer | None = None,
) -> PersonaState:
    scorer = scorer or EmotionScorer()
    user = scorer.best(dialogue_text) or DEFAULT_EMOTION
    sakura = scorer.best(mood_content) or DEFAULT_EMOTION
    return PersonaState(
        user_emotion=normalize_emotion(user),
        sakura_mood_emotion=normalize_emotion(sakura),
    )


def emotion_congruence_factor(current: str, memory_emotion: str) -> float:
    """当前情绪与记忆编码情绪的接近程度 → 召回加权因子。"""
    current = normalize_emotion(current)
    memory_emotion = normalize_emotion(memory_emotion)
    if memory_emotion == DEFAULT_EMOTION or current == DEFAULT_EMOTION:
        return 1.0
    if memory_emotion == current:
        return 1.0 + EMOTION_CONGRUENCE_MAX_BOOST
    affinity = _EMOTION_AFFINITY.get((current, memory_emotion), 0.0)
    if affinity <= 0.0:
        return 1.0
    return 1.0 + EMOTION_CONGRUENCE_MAX_BOOST * affinity
