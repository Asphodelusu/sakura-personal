"""Private inner thought: grammar, window, and prompt text.

Sensory impression, mood, and intimacy continuation are not available in this
slice. Those sources stay empty and are not invented.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from app.core.relational_drive import DriveAppraisal, parse_drive_appraisal
from app.core.runtime_log import log_event
from app.llm.prompts.types import ContextFragment

DRIVE_APPRAISAL_OUTPUT_INSTRUCTION = (
    "若新对话确实改变短期内在状态，可在 interest 后、正文前附三行；"
    "不确定时省略，勿复述当前状态：\n"
    "drive_kind: physical_arousal|erotic_salience|attachment_longing|afterglow|inhibition\n"
    "drive_shift: rise|fall|hold\n"
    "drive_strength: subtle|mild"
)

DEFAULT_INNER_THOUGHT_WINDOW_SIZE = 6
DEFAULT_INNER_THOUGHT_TIMEOUT_SECONDS = 8
DEFAULT_INNER_THOUGHT_JOIN_TIMEOUT_SECONDS = 3
DEFAULT_INNER_THOUGHT_MAX_CHARS = 200
DEFAULT_INNER_THOUGHT_MAX_TOKENS = 180
_RECENT_DIALOGUE_CHAR_BUDGET = 800
_CHARACTER_EXCERPT_CHAR_BUDGET = 800

InterestLevel = Literal["low", "mid", "high"]

_INTEREST_ALIASES: dict[str, InterestLevel] = {
    "low": "low",
    "mid": "mid",
    "high": "high",
    "低": "low",
    "中": "mid",
    "高": "high",
}
_INTEREST_LINE_RE = re.compile(
    r"^\s*(?:interest|兴致|興趣|兴趣)\s*[:：]\s*(low|mid|high|低|中|高)\s*$",
    re.I,
)
_DRIVE_HEADER_RE = re.compile(
    r"^\s*(drive_kind|drive_shift|drive_strength)\s*[:：]\s*(.*?)\s*$",
    re.I,
)
_DRIVE_UNKNOWN_RE = re.compile(r"^\s*drive_[A-Za-z0-9_]+\s*[:：]", re.I)
_DRIVE_HEADER_KEYS = {
    "drive_kind": "kind",
    "drive_shift": "direction",
    "drive_strength": "strength",
}
_STYLE_FEW_SHOTS = """示例 1（平静）：
特に何も。今はただ、この静かな時間を心地よく感じている。

示例 2（直接）：
雨なら傘を持てばいい。それだけ。

示例 3（有立场）：
それは違う。納得できないなら、はっきり言う。

示例 4（别扭）：
別に…気にしてない。ただ、もう少しだけ近くにいてほしい。

示例 5（不安）：
黙ってしまった。何か気に障ったのかな。少し不安。"""


@dataclass(frozen=True)
class InnerThoughtResult:
    text: str
    interest: InterestLevel | None = None
    drive_appraisal: DriveAppraisal | None = None


@dataclass(frozen=True)
class InnerThoughtSettings:
    enabled: bool = True
    window_size: int = DEFAULT_INNER_THOUGHT_WINDOW_SIZE
    timeout_seconds: int = DEFAULT_INNER_THOUGHT_TIMEOUT_SECONDS
    join_timeout_seconds: int = DEFAULT_INNER_THOUGHT_JOIN_TIMEOUT_SECONDS
    skip_fast_tier: bool = True
    skip_proactive: bool = True

    def normalized(self) -> InnerThoughtSettings:
        return InnerThoughtSettings(
            enabled=bool(self.enabled),
            window_size=max(1, min(int(self.window_size), 16)),
            timeout_seconds=max(1, min(int(self.timeout_seconds), 15)),
            join_timeout_seconds=max(1, min(int(self.join_timeout_seconds), 8)),
            skip_fast_tier=bool(self.skip_fast_tier),
            skip_proactive=bool(self.skip_proactive),
        )


class InnerThoughtWindow:
    def __init__(self, max_size: int = DEFAULT_INNER_THOUGHT_WINDOW_SIZE) -> None:
        self._max_size = max(1, int(max_size))
        self._items: deque[str] = deque(maxlen=self._max_size)

    @property
    def max_size(self) -> int:
        return self._max_size

    def configure(self, max_size: int) -> None:
        size = max(1, int(max_size))
        if size == self._max_size:
            return
        self._max_size = size
        self._items = deque(self._items, maxlen=size)

    def clear(self) -> None:
        self._items.clear()

    def push(self, thought: str) -> None:
        text = _normalize_thought_text(thought)
        if text:
            self._items.append(text)

    def items(self) -> tuple[str, ...]:
        return tuple(self._items)

    def __len__(self) -> int:
        return len(self._items)


def should_generate_inner_thought(
    settings: InnerThoughtSettings,
    *,
    api_client: object | None,
    turn_tier: str = "standard",
    proactive_mode: bool = False,
) -> bool:
    if not bool(settings.enabled) or api_client is None:
        return False
    if bool(settings.skip_fast_tier) and turn_tier == "fast":
        return False
    if bool(settings.skip_proactive) and proactive_mode:
        return False
    return True


def generate_inner_thought(
    api_client: object,
    *,
    character_name: str,
    character_excerpt: str,
    mood_summary: str,
    recent_dialogue: str,
    sensory_impression: str = "",
    previous_thoughts: Sequence[str] = (),
    cancel_checker: Any = None,
) -> InnerThoughtResult:
    """One completion on an already bounded client. Transport limits stay on that client."""

    system_prompt = build_inner_thought_system_prompt(character_name)
    user_prompt = build_inner_thought_user_prompt(
        character_name=character_name,
        character_excerpt=character_excerpt,
        mood_summary=mood_summary,
        recent_dialogue=recent_dialogue,
        sensory_impression=sensory_impression,
        previous_thoughts=previous_thoughts,
    )
    empty = InnerThoughtResult(text="", interest=None)
    complete = getattr(api_client, "complete_raw", None)
    if not callable(complete):
        return empty
    try:
        if cancel_checker is not None:
            cancel_checker()
        # This dedicated worker owns the synchronous I/O until it really exits.
        # Passing cancellation into urllib's helper would detach another reader
        # while making this worker appear idle. The client has a short timeout.
        raw = complete(
            system_prompt,
            [{"role": "user", "content": user_prompt}],
            temperature=0.9,
            max_tokens=DEFAULT_INNER_THOUGHT_MAX_TOKENS,
        )
        if cancel_checker is not None:
            cancel_checker()
    except Exception as exc:  # noqa: BLE001 - optional thought must not surface provider text
        log_event(
            "InnerThought",
            "内心独白生成失败，已跳过",
            {"code": "INNER_THOUGHT_GENERATION_FAILED", "error_type": type(exc).__name__},
            severity="info",
        )
        return empty
    return parse_inner_thought_output(raw)


def parse_inner_thought_output(raw: object) -> InnerThoughtResult:
    text = str(raw or "").strip()
    if not text:
        return InnerThoughtResult(text="", interest=None)
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("text"):
            text = text[4:].lstrip("\n")
        text = text.strip()
    lines = text.splitlines()
    interest: InterestLevel | None = None
    body_start = 0
    for index, line in enumerate(lines):
        match = _INTEREST_LINE_RE.match(line.strip())
        if match is None:
            if line.strip():
                break
            continue
        alias = match.group(1).lower()
        interest = _INTEREST_ALIASES.get(alias) or _INTEREST_ALIASES.get(match.group(1))
        body_start = index + 1
        break
    if interest is None:
        return InnerThoughtResult(text=_normalize_thought_text(text), interest=None)
    fields, consumed, invalid = _consume_drive_headers(lines[body_start:])
    body = "\n".join(lines[body_start + consumed :]).strip()
    appraisal = None if invalid else parse_drive_appraisal(fields)
    return InnerThoughtResult(
        text=_normalize_thought_text(body),
        interest=interest,
        drive_appraisal=appraisal,
    )


def build_inner_thought_fragment(
    window: InnerThoughtWindow,
    *,
    character_name: str = "",
) -> ContextFragment | None:
    items = [text for text in window.items() if text]
    if not items:
        return None
    name = (character_name or "角色").strip() or "角色"
    if len(items) == 1:
        body = f"「{items[-1]}」"
    else:
        labels = _window_labels(len(items))
        lines = [f"{label}：「{text}」" for label, text in zip(labels, items)]
        body = "\n".join(lines)
    content = (
        "[内心の声]\n"
        f"{name}の口に出していない内面"
        "（返信で直接言及したり、聞こえたように振る舞わないこと）:\n"
        f"{body}"
    )
    return ContextFragment(
        fragment_id="runtime.inner_thought",
        source="runtime",
        content=content,
        trust="trusted",
        priority=88,
        token_budget=420,
        sensitivity="private",
        cache_scope="turn",
        required=False,
    )


def build_inner_thought_system_prompt(character_name: str) -> str:
    name = (character_name or "角色").strip() or "角色"
    return (
        f"你是 {name} 的内心之声（inner voice）。\n"
        "你正在观察此刻的对话，并记录角色真实但不会说出口的内心活动。\n"
        "输出格式固定两段：\n"
        "1) 第一行：interest: low|mid|high\n"
        "2) 第二行起：内心独白正文（日文，不要再写 interest）\n"
        f"{DRIVE_APPRAISAL_OUTPUT_INSTRUCTION}\n"
        "除上述可选 drive 头外，不要输出其它标记、前缀或解释。"
    )


def build_inner_thought_user_prompt(
    *,
    character_name: str,
    character_excerpt: str,
    mood_summary: str,
    recent_dialogue: str,
    sensory_impression: str = "",
    previous_thoughts: Sequence[str] = (),
) -> str:
    name = (character_name or "角色").strip() or "角色"
    parts = [
        "# 规则",
        "- 第一行必须是：interest: low|mid|high",
        "- interest = 此刻你对「继续聊这件事 / 这一拍交流」的主观兴致（不是字面热闹程度）",
        "- 对方只回「嗯」「好」也可能是 high（比如答应了你在意的提案）；冷场闲聊也可能是 low",
        "- 第二行起输出 2-4 句日文内心独白，第一人称",
        "- 只写内心感受、直觉反应、隐藏的疑惑或渴望——不写对话策略、不写「我应该说什么」",
        f"- 可以和 {name} 实际说出来的话不同甚至相反",
        "- 不要评价自己说的话、不要总结、不要给出结论",
        "- 如果此刻没有特别的内心波动，写一句简短的现状即可，不要编造",
        "",
        "# 输出示例",
        "interest: mid",
        "雨なら傘を持てばいい。それだけ。",
        "",
        "# 思考风格示例（仅正文风格参考；正式输出仍要带 interest 行）",
        _STYLE_FEW_SHOTS,
        "",
        "# 角色档案（节选）",
        _clip(character_excerpt, _CHARACTER_EXCERPT_CHAR_BUDGET) or "（无）",
        "",
        "# 当前情绪状态",
        _clip(mood_summary, 300) or "（无特别记录）",
    ]
    if previous_thoughts:
        labels = _window_labels(len(previous_thoughts))
        history_lines = [f"{label}：{text}" for label, text in zip(labels, previous_thoughts) if text]
        if history_lines:
            parts.extend(["", "# 最近的内心独白（连续）", *history_lines])
    parts.extend(
        [
            "",
            "# 最近对话",
            _clip(recent_dialogue, _RECENT_DIALOGUE_CHAR_BUDGET) or "（无）",
        ]
    )
    sensory = _clip(sensory_impression, 200)
    if sensory:
        parts.extend(["", "# 最近感知印象", sensory])
    parts.extend(["", f"请输出 {name} 此刻的 interest 行 + 内心独白："])
    return "\n".join(parts)


def format_recent_dialogue(
    messages: Sequence[Mapping[str, Any]],
    *,
    max_turns: int = 6,
    char_budget: int = _RECENT_DIALOGUE_CHAR_BUDGET,
) -> str:
    lines: list[str] = []
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        role = str(message.get("role") or "").strip()
        if role not in {"user", "assistant"}:
            continue
        content = _message_text(message.get("content")).strip()
        if not content:
            continue
        source = str(message.get("source") or "").strip()
        if role == "user":
            label = "用户"
        elif source == "proactive":
            label = "角色(主动)"
        else:
            label = "角色"
        lines.append(f"{label}：{_clip(content, 180)}")
    if not lines:
        return ""
    return _clip("\n".join(lines[-(max_turns * 2) :]), char_budget)


def character_excerpt_from_prompt(system_prompt: str) -> str:
    return _clip(system_prompt, _CHARACTER_EXCERPT_CHAR_BUDGET)


def _consume_drive_headers(lines: Sequence[str]) -> tuple[dict[str, str], int, bool]:
    fields: dict[str, str] = {}
    consumed = 0
    invalid = False
    seen_header = False
    for index, line in enumerate(lines):
        stripped = str(line or "").strip()
        if not stripped:
            if seen_header:
                consumed = index + 1
            continue
        match = _DRIVE_HEADER_RE.match(stripped)
        if match is None:
            if _DRIVE_UNKNOWN_RE.match(stripped):
                invalid = True
                seen_header = True
                consumed = index + 1
                continue
            break
        seen_header = True
        consumed = index + 1
        key = _DRIVE_HEADER_KEYS[match.group(1).lower()]
        value = match.group(2).strip()
        if not value or key in fields:
            invalid = True
            continue
        fields[key] = value
    if fields and set(fields) != {"kind", "direction", "strength"}:
        invalid = True
    return fields, consumed, invalid


def _window_labels(count: int) -> list[str]:
    if count <= 0:
        return []
    if count == 1:
        return ["今"]
    labels = ["今"]
    prefixes = ["前", "前々"]
    for index in range(1, count):
        if index - 1 < len(prefixes):
            labels.append(prefixes[index - 1])
        else:
            labels.append(f"{index}轮前")
    return list(reversed(labels))


def _normalize_thought_text(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for marker in ("[内心の声]", "内心の声：", "Inner thoughts:", "Inner thoughts -"):
        text = text.replace(marker, "")
    text = text.strip().strip("`").strip()
    if text.startswith("「") and text.endswith("」") and text.count("「") == 1:
        text = text[1:-1].strip()
    text = " ".join(text.split())
    return _clip(text, DEFAULT_INNER_THOUGHT_MAX_CHARS)


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "\n".join(parts)
    return str(content or "")


def _clip(text: str, budget: int) -> str:
    value = str(text or "").strip()
    if len(value) <= budget:
        return value
    return value[: max(0, budget - 1)].rstrip() + "…"
