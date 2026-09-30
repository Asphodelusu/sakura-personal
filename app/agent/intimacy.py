"""Optional intimacy director layer of the personal fork.

Only affects guide injection, reply rhythm and (later) silent continuation; it is not
a permission switch. Entry is a whole-sentence keyword from the user, never the model.
"""

from __future__ import annotations

import re
from typing import Any

from app.agent.tools import Tool
from app.llm.prompts.types import PromptSection

INTIMACY_EXTRA_TONES: tuple[str, ...] = ("亲密", "H")

# 系统续投标记（不进持久化历史）。历史兼容：旧会话可能仍是 role=user + 裸标记。
INTIMACY_CONTINUE_MARKER = "（続けて）"
INTIMACY_CONTINUE_ROLE = "system"
INTIMACY_CONTINUE_SYSTEM_TEXT = (
    "【系统续投信号／不是对方发言】对方当前沉默，没有新话。"
    "这不是对方又说了一句，也不是要你再回答对方上一句。"
    "请从你自己刚刚说出口的那一句接着往下说一小拍，像你主动又补了一句。"
    "不要重新回答对方上一句。"
    "可以放缓、短暂确认或自然收束；不要把沉默当成同意升级，也不要仅换说法重复上一句。"
    "若已不再需要详细引导、连续节奏或自动续投，调用 set_intimacy_mode(on=false)。"
    "本条是系统信号，绝不要当成用户说过的话，不要回答或复述本信号。\n"
    f"{INTIMACY_CONTINUE_MARKER}"
)

# 进入/退出亲密节奏的成对硬控制词（整句匹配，可带轻标点）。换词只改这里。
INTIMACY_ENTER_PHRASE = "贴紧"
INTIMACY_EXIT_PHRASE = "苹果"
_INTIMACY_CONTROL_TRIM_RE = re.compile(
    r"^[\s　\"'“”‘’「」『』]+|[\s　\"'“”‘’「」『』！!。.?？…～~]+$"
)
_INTIMACY_EXIT_RE = re.compile(
    r"(冷静|先这样|不闹了|差不多了|休息吧|睡吧|聊点别的|不要继续|别继续|"
    r"停下|退出亲密|结束吧|到此为止|我不舒服|"
    r"やめよう|やめて|冷却)",
    re.IGNORECASE,
)


class IntimacyModeState:
    """可选亲密导演层状态。只影响详细引导注入、回复节奏与自动续投，不构成许可或行为开关。

    生命周期：
    - 开启：用户整句发送约定词 → 系统硬开启（不经 LLM 工具）
    - 保持：用户正常回话 → 刷新续投周期；静默续投 → 扣 1 轮
    - 静默休眠：三轮续投耗尽后保持 active，停止续投，不设 needs_reentry_hint
    - 退出：用户收尾话 / 模型 on=false
    """

    _AUTO_EXIT_TURNS = 3

    def __init__(self) -> None:
        self.active: bool = False
        self._turns_left: int = 0
        self.continuation_epoch: int = 0
        self.needs_reentry_hint: bool = False
        self.last_user_text: str = ""
        self.opened_by_keyword: bool = False

    def note_user_text(self, text: str) -> None:
        self.last_user_text = str(text or "").strip()

    def enter(self, *, by_keyword: bool = False) -> None:
        self.active = True
        self._turns_left = self._AUTO_EXIT_TURNS
        self.continuation_epoch += 1
        self.needs_reentry_hint = False
        self.opened_by_keyword = bool(by_keyword)

    def exit(self) -> None:
        """收尾：清 active，不留重进提示。"""
        self.active = False
        self._turns_left = 0
        self.needs_reentry_hint = False
        self.last_user_text = ""
        self.opened_by_keyword = False

    def refresh_user_reply(self) -> None:
        """真实用户回话：开启新的续投周期，保持开启。"""
        if not self.active:
            return
        self._turns_left = self._AUTO_EXIT_TURNS
        self.continuation_epoch += 1
        # 非约定词的普通回话后，去掉「本轮刚硬开」标记
        if not user_requests_intimacy_entry(self.last_user_text):
            self.opened_by_keyword = False

    def expire_after_silence(self) -> None:
        """显式过期：清 active 并留下重进提示。续投耗尽的静默休眠不调用此方法。"""
        if not self.active:
            return
        self.active = False
        self._turns_left = 0
        self.opened_by_keyword = False
        self.needs_reentry_hint = True

    def consume_turn(self) -> bool:
        """系统续投消耗一次；返回是否仍可续投。耗尽后保持 active 进入静默休眠。"""
        if not self.active or self._turns_left <= 0:
            return False
        self._turns_left -= 1
        self.opened_by_keyword = False
        return True


def _normalize_intimacy_control_phrase(text: str) -> str:
    return _INTIMACY_CONTROL_TRIM_RE.sub("", str(text or "").strip()).strip()


def user_requests_intimacy_exit(text: str) -> bool:
    return _normalize_intimacy_control_phrase(text) == INTIMACY_EXIT_PHRASE


def user_declines_or_exits_intimacy(text: str) -> bool:
    """安全词整句退出，或明确要求退出/降温。"""
    t = str(text or "").strip()
    if not t:
        return False
    if user_requests_intimacy_exit(t):
        return True
    return bool(_INTIMACY_EXIT_RE.search(t))


def user_requests_intimacy_entry(text: str) -> bool:
    """整句是否为约定硬入口词。"""
    t = str(text or "").strip()
    if not t or INTIMACY_CONTINUE_MARKER in t:
        return False
    if user_declines_or_exits_intimacy(t):
        return False
    return _normalize_intimacy_control_phrase(t) == INTIMACY_ENTER_PHRASE


def apply_intimacy_user_utterance(text: str, state: IntimacyModeState, *, available: bool) -> str | None:
    """处理约定词硬开 / 收尾退出。返回动作名或 None。"""
    state.note_user_text(text)
    if user_requests_intimacy_entry(text):
        if not available:
            return "unavailable"
        already = state.active
        state.enter(by_keyword=True)
        return "already_on" if already else "entered"
    if user_declines_or_exits_intimacy(text):
        if state.active:
            state.exit()
            return "exited"
        return None
    return None


def build_intimacy_continue_message() -> dict[str, Any]:
    """构造亲密静默续投的系统消息（role=system，避免假 user 导致角色串线）。"""
    return {
        "role": INTIMACY_CONTINUE_ROLE,
        "content": INTIMACY_CONTINUE_SYSTEM_TEXT,
        "source": "intimacy_continue",
    }


def message_is_intimacy_continue(message: dict[str, Any] | None) -> bool:
    """判断单条消息是否为亲密续投信号（含旧版 user 裸标记）。"""
    if not isinstance(message, dict):
        return False
    if str(message.get("source") or "").strip() == "intimacy_continue":
        return True
    content = str(message.get("content") or "")
    if INTIMACY_CONTINUE_MARKER not in content:
        return False
    return str(message.get("role") or "").strip() in {"system", "user"}


_SET_INTIMACY_MODE_DESCRIPTION = (
    "关闭可选亲密导演层的详细引导、回复节奏与自动续投（更快回复、沉默续投、亲密/H tone）。"
    f"开启不由本工具控制：只有对方整句发送约定词「{INTIMACY_ENTER_PHRASE}」时，"
    "系统才会自动开启；不要猜测、不要调用 on=true 试图开启。"
    "当详细引导、节奏或自动续投不再需要，或对方降温/收尾时，调用 on=false："
    f"安全词「{INTIMACY_EXIT_PHRASE}」或明确的停下、不要继续、不适等表达会立即退出。"
    f"关闭后不会自动恢复；对方需再次发送「{INTIMACY_ENTER_PHRASE}」。"
    "本工具只影响引导注入、回复节奏与自动续投，不限制身体亲密行为本身。"
)


def create_set_intimacy_mode_tool(state: IntimacyModeState) -> Tool:
    def handle(arguments: dict[str, Any]) -> dict[str, Any]:
        if bool((arguments or {}).get("on", False)):
            # 开启已改为约定词硬入口；工具 on=true 不再开启，避免 LLM 误猜。
            if state.active:
                return {"intimacy_mode": "on", "entry": "keyword_only"}
            return {
                "intimacy_mode": "off",
                "entry": "keyword_only",
                "instruction": (
                    f"开启请等对方整句发送约定词「{INTIMACY_ENTER_PHRASE}」；"
                    "系统会自动开启。不要再调用本工具 on=true。"
                ),
            }
        state.exit()
        return {"intimacy_mode": "off"}

    return Tool(
        name="set_intimacy_mode",
        description=_SET_INTIMACY_MODE_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "on": {
                    "type": "boolean",
                    "description": (
                        "true=无效，开启只能靠约定词；"
                        "false=关闭详细引导、节奏与自动续投，不表示结束身体亲密。"
                    ),
                },
            },
            "required": ["on"],
        },
        handler=handle,
        group="persona",
    )


def _entry_hint_text() -> str:
    return (
        "# 可选亲密导演层\n"
        f"只有对方整句发送「{INTIMACY_ENTER_PHRASE}」时，系统才开启详细 guide、扩展节奏与自动续投；"
        "不会自动开启，也不会因对话自然升温而开启。\n"
        "未开启时不注入详细 guide；继续按当前人格、关系事实与演出约束回应。\n"
        "不要猜测或调用 set_intimacy_mode(on=true)。"
        "需要结束已开启的导演层时才调用 set_intimacy_mode(on=false)。"
    )


def build_intimacy_section(state: IntimacyModeState, guide: str) -> PromptSection | None:
    """active：guide + 节奏说明；刚硬开时追加入口说明；关闭后短重进提示；未开启：短入口说明。"""
    if state.active:
        if not guide:
            return None
        keyword_note = ""
        if state.opened_by_keyword:
            keyword_note = (
                f"\n\n# 约定入口（本轮已硬开启）\n"
                f"对方本轮发送了约定词「{INTIMACY_ENTER_PHRASE}」。"
                "这表示对方请求启用详细 guide 与连续节奏，不表示刚刚取得亲密许可，"
                "也不创建或升级关系。系统不再机械询问一次相同的模式确认。"
                "但沉默不代表同意升级；对方迟疑、退开、改变主意或不适时，立即放缓、暂停或确认。"
                f"安全词「{INTIMACY_EXIT_PHRASE}」或明确拒绝会由系统立即退出。"
                "不要调用 set_intimacy_mode(on=true)。\n"
            )
        rhythm_hint = (
            f"{keyword_note}\n\n# 节奏 — 已开启\n"
            "你正在可选导演层：回复更快、可以主动续说，并注入详细 guide。\n\n"
            "## 系统续投信号（重要）\n"
            "对方沉默时，系统可能注入一条 role=system 的续投信号（含「（続けて）」）。\n"
            "那是系统提示，绝不是对方说过的话；不要回答、复述或当成用户发言。\n"
            "收到后续投信号时，从你自己上一句接着说，像你主动又补了一句；"
            "不要重新回答对方上一句，不要把沉默当成同意升级，也不要仅换说法重复上一句；"
            "若已不再需要详细引导与连续节奏，调用 set_intimacy_mode(on=false)。\n\n"
            "## 何时退出（必须主动调用 set_intimacy_mode(on=false)）\n"
            "出现以下任一信号时立刻退出导演层，不要犹豫：\n"
            "- 对方语气从亲昵转为日常闲聊（聊吃饭、工作、天气、新闻等）\n"
            "- 对方说了结束/收尾/降温的话（「好了」「不闹了」「先这样」"
            "「冷静一下」「睡吧」「休息吧」「差不多了」「聊点别的」等）\n"
            "- 对方连续两轮未回应亲密互动，话题已明显漂移\n"
            "- 对方表示累了、困了、要出门、要忙，主动切断互动\n\n"
            "宁可误退。误退后对方再次发送约定词即可重开。拖着不退才是问题。\n\n"
            "## 其他\n"
            f"长时间无人回话会自动关闭；重开需对方再发「{INTIMACY_ENTER_PHRASE}」。"
        )
        return PromptSection(
            section_id="persona.intimacy",
            body=f"{guide}{rhythm_hint}",
            source="character",
            sensitivity="private",
        )
    if state.needs_reentry_hint:
        return PromptSection(
            section_id="persona.intimacy_reentry",
            body=(
                "# 可选导演层 — 引导与自动续投已关闭\n"
                "详细 guide、扩展节奏与自动续投因长时间无回话或你主动关闭而结束了。\n"
                f"重开只能等对方再次整句发送约定词「{INTIMACY_ENTER_PHRASE}」；"
                "不要猜测或调用 set_intimacy_mode(on=true)。\n"
                "仍按当前关系和意愿自然回应。"
                "若对方当前话题明显是日常/结束/其他内容，保持日常即可。"
            ),
            source="character",
            sensitivity="private",
        )
    if guide:
        return PromptSection(
            section_id="persona.intimacy_entry",
            body=_entry_hint_text(),
            source="character",
            sensitivity="private",
        )
    return None
