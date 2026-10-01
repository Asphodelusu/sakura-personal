"""Explicit leave phrases. A sleep topic or a question is not a goodbye."""

from __future__ import annotations

_AWAY_PHRASES: tuple[str, ...] = (
    "我出去了",
    "先走了",
    "出门了",
    "我走了",
    "离开一下",
    "我出去了哦",
    "别回",
    "不要回",
    "别说话",
    "不用回",
    "不用说话",
    "暂时不要说话",
    "忙去了",
    "去忙了",
    "工作去了",
    "开会去了",
    "我去睡了",
    "我去睡觉",
    "去睡觉了",
    "去睡了",
    "先睡了",
    "要睡了",
    "该睡了",
    "晚安",
    "おやすみ",
    "お休み",
)
_AWAY_SLEEP_PHRASES = frozenset(
    {
        "我去睡了",
        "我去睡觉",
        "去睡觉了",
        "去睡了",
        "先睡了",
        "要睡了",
        "该睡了",
        "晚安",
        "おやすみ",
        "お休み",
    }
)
_AWAY_TOPIC_BLOCKERS: tuple[str, ...] = (
    "睡不着",
    "失眠",
    "睡眠",
    "午睡",
    "聊睡",
    "说睡",
    "谈睡",
    "睡觉的事",
    "睡觉的事情",
    "关于睡",
    "梦见",
    "做梦",
    "想睡觉吗",
    "要不要睡",
)
_AWAY_QUESTION_MARKERS: tuple[str, ...] = ("吗", "麼", "么", "?", "？", "呢")
_AWAY_FAREWELLS = frozenset({"晚安", "おやすみ", "お休み"})


def message_implies_away(text: str) -> str | None:
    """Return the matched leave phrase, or None when the user is not leaving."""
    normalized = "".join((text or "").split())
    if not normalized:
        return None
    topic_like = any(blocker in normalized for blocker in _AWAY_TOPIC_BLOCKERS)
    is_question = any(marker in normalized for marker in _AWAY_QUESTION_MARKERS)
    for phrase in _AWAY_PHRASES:
        if phrase not in normalized:
            continue
        if topic_like and phrase in _AWAY_SLEEP_PHRASES and phrase not in _AWAY_FAREWELLS:
            continue
        if is_question and phrase in _AWAY_SLEEP_PHRASES and phrase not in _AWAY_FAREWELLS:
            continue
        return phrase
    return None
