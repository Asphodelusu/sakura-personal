from __future__ import annotations

from app.agent.screen_observation import (
    MANUAL_SCREEN_OBSERVATION_HISTORY_MARKER,
    SCREEN_OBSERVATION_HISTORY_MARKER,
    should_observe_screen,
)


# 用户话里出现这些，才开放 observe_screen（软提示不够稳，必须硬门槛）。
_SCREEN_OBSERVATION_INTENT_MARKERS = (
    "屏幕",
    "畫面",
    "画面",
    "界面",
    "窗口",
    "視窗",
    "视窗",
    "报错",
    "報錯",
    "截图",
    "截圖",
    "桌面",
    "显示器",
    "顯示器",
    "卡住",
    "卡死",
    "看一下这个",
    "看看这个",
    "这是什么",
    "這是什麼",
    "屏幕上",
    "error",
    "exception",
    "traceback",
    "stacktrace",
    "画面どう",
    "画面見て",
    "画面を見て",
    "スクリーン",
    "見てみて",
    "見てくれる",
)


class ScreenPolicy:
    """集中维护 Agent 屏幕观察入口策略。"""

    @staticmethod
    def should_offer_screen_observation_text(text: str | None) -> bool:
        """仅在本轮用户话有画面依赖、且尚未看过屏时，开放自主屏幕观察。"""

        if text is None:
            return False
        if (
            SCREEN_OBSERVATION_HISTORY_MARKER in text
            or MANUAL_SCREEN_OBSERVATION_HISTORY_MARKER in text
        ):
            return False
        return ScreenPolicy.has_screen_observation_intent(text)

    @staticmethod
    def has_screen_observation_intent(text: str) -> bool:
        """判断用户话是否依赖当前画面（硬门槛，避免寒暄也调 observe_screen）。"""
        if should_observe_screen(text):
            return True
        normalized = "".join(text.split()).lower()
        if not normalized:
            return False
        return any(marker in normalized for marker in _SCREEN_OBSERVATION_INTENT_MARKERS)
