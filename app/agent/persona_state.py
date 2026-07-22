from __future__ import annotations

import math
from dataclasses import dataclass

from app.backchannel.emotion import EmotionScorer
from app.backchannel.models import DEFAULT_EMOTION, EMOTIONS

# 情绪一致性：把每种离散情绪标签映射到 (valence, arousal) 二维连续坐标
# （情绪环模型 / circumplex model of affect），用坐标间的欧氏距离连续计算
# 接近程度，取代逐对手写的邻接表。好处：
# 1. 新增情绪标签只需给一个坐标，不必再补 O(n) 条新的邻接关系；
# 2. 接近程度是连续的，不会出现"表里没写这一对就完全不相关"的断层。
# 坐标是主观估计（不追求心理学精确校准），只要求相对位置合理：
# 同象限的情绪（如 happy/playful）距离近，对角情绪（如 happy/sad）距离远。
_EMOTION_COORDINATES: dict[str, tuple[float, float]] = {
    "neutral": (0.0, 0.0),
    "confused": (-0.20, 0.35),
    "anxious": (-0.60, 0.65),
    "frustrated": (-0.55, 0.50),
    "sad": (-0.70, -0.30),
    "angry": (-0.70, 0.70),
    "happy": (0.75, 0.45),
    "playful": (0.70, 0.70),
    "embarrassed": (-0.10, 0.45),
    "lonely": (-0.60, -0.40),
    "tender": (0.55, -0.25),
    "warm": (0.65, -0.10),
    "determined": (0.50, 0.60),
    "defensive": (-0.40, 0.40),
    "hopeful": (0.55, 0.30),
}
# 坐标空间里两点间可能出现的近似最大欧氏距离，用于把距离归一化到 0~1 的接近度。
# 不必是数学上的精确对角线长度，只要大到能让最远的两个情绪接近度趋近 0 即可。
_EMOTION_SPACE_MAX_DISTANCE = 2.0

# 陪伴偏置：当前情绪明显低落（valence 低于此阈值）时，warm/tender 记忆
# 仍给一个小接近度出口——这不是坐标几何上算出来的（它们在空间里其实离得远），
# 是刻意的策略选择：避免低落时只召回同质负面记忆、形成消极螺旋，所以单独表达，
# 不掺进坐标距离公式里。
_LOW_VALENCE_COMPASSION_THRESHOLD = -0.35
_COMPASSION_TARGET_EMOTIONS = frozenset({"warm", "tender"})
_COMPASSION_AFFINITY = 0.35

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


def emotion_affinity(current: str, memory_emotion: str) -> float:
    """两种情绪在连续情绪空间里的接近程度，[0, 1]。

    基于 valence-arousal 坐标的欧氏距离换算；任一情绪没有坐标（理论上不会
    发生，EMOTIONS 里的标签都已收录）时返回 0，视为不相关。
    """
    current_coord = _EMOTION_COORDINATES.get(current)
    memory_coord = _EMOTION_COORDINATES.get(memory_emotion)
    if current_coord is None or memory_coord is None:
        return 0.0
    distance = math.hypot(
        current_coord[0] - memory_coord[0],
        current_coord[1] - memory_coord[1],
    )
    affinity = max(0.0, 1.0 - distance / _EMOTION_SPACE_MAX_DISTANCE)
    if (
        memory_emotion in _COMPASSION_TARGET_EMOTIONS
        and current_coord[0] <= _LOW_VALENCE_COMPASSION_THRESHOLD
    ):
        affinity = max(affinity, _COMPASSION_AFFINITY)
    return affinity


def emotion_congruence_factor(current: str, memory_emotion: str) -> float:
    """当前情绪与记忆编码情绪的接近程度 → 召回加权因子。"""
    current = normalize_emotion(current)
    memory_emotion = normalize_emotion(memory_emotion)
    if memory_emotion == DEFAULT_EMOTION or current == DEFAULT_EMOTION:
        return 1.0
    if memory_emotion == current:
        return 1.0 + EMOTION_CONGRUENCE_MAX_BOOST
    affinity = emotion_affinity(current, memory_emotion)
    if affinity <= 0.0:
        return 1.0
    return 1.0 + EMOTION_CONGRUENCE_MAX_BOOST * affinity
