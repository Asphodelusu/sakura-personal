"""角色内心独白：短、主观、可跨轮滑动窗口注入回复上下文。"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence

import httpx

from app.core.debug_log import debug_log
from app.llm.api_client import ApiRequestError, ChatMessage, OpenAICompatibleClient
from app.llm.prompts.types import ContextFragment
from app.perception.sensory_impression import sensory_impression_store

DEFAULT_INNER_THOUGHT_WINDOW_SIZE = 6
# Flash 实测常要 4–6s；过短会误杀已返回的内容
DEFAULT_INNER_THOUGHT_TIMEOUT_SECONDS = 8
# 主路径 join 上限：超时后 skip 独白继续；后台 HTTP 仍可能跑完但不挡回复
DEFAULT_INNER_THOUGHT_JOIN_TIMEOUT_SECONDS = 3
DEFAULT_INNER_THOUGHT_MAX_CHARS = 200
DEFAULT_INNER_THOUGHT_MAX_TOKENS = 180
_RECENT_DIALOGUE_CHAR_BUDGET = 800
_CHARACTER_EXCERPT_CHAR_BUDGET = 800
_MOOD_CHAR_BUDGET = 300

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


@dataclass(frozen=True)
class InnerThoughtResult:
    """本轮内心独白解析结果；interest 驱动篇幅，缺失则不注入篇幅块。"""

    text: str
    interest: InterestLevel | None = None

_STYLE_FEW_SHOTS = """示例 1（日常闲聊）：
あ、この話題好きだ。もっと話したいな。でもあまり熱心に見えると変かな…

示例 2（被夸奖时）：
褒められた…嬉しいけど、どう反応すればいいかわからない。顔が少し熱い。

示例 3（看到对方沉默时）：
黙ってしまった。何か気に障った？それともただ考えてるだけ？判断つかない…少し不安。

示例 4（感到困惑时）：
なぜ急にそんなことを？意図が読めない。でも素直に聞くのも野暮かな…

示例 5（无特别波动时）：
特に何も。今はただ、この静かな時間を心地よく感じている。"""


@dataclass(frozen=True)
class InnerThoughtSettings:
    """内心独白功能开关与窗口参数。"""

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
    """最近 N 轮内心独白（旧→新）。"""

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
    api_client: OpenAICompatibleClient | None,
    turn_tier: str = "standard",
    proactive_mode: bool = False,
) -> bool:
    cfg = settings.normalized()
    if not cfg.enabled or api_client is None:
        return False
    if cfg.skip_fast_tier and turn_tier == "fast":
        return False
    if cfg.skip_proactive and proactive_mode:
        return False
    return True


def generate_inner_thought(
    api_client: OpenAICompatibleClient,
    *,
    character_name: str,
    character_excerpt: str,
    mood_summary: str,
    recent_dialogue: str,
    sensory_impression: str = "",
    previous_thoughts: Sequence[str] = (),
    settings: InnerThoughtSettings | None = None,
) -> InnerThoughtResult:
    """调用轻量模型生成本轮内心独白；失败/超时返回空结果。"""
    cfg = (settings or InnerThoughtSettings()).normalized()
    system_prompt = build_inner_thought_system_prompt(character_name)
    user_prompt = build_inner_thought_user_prompt(
        character_name=character_name,
        character_excerpt=character_excerpt,
        mood_summary=mood_summary,
        recent_dialogue=recent_dialogue,
        sensory_impression=sensory_impression,
        previous_thoughts=previous_thoughts,
    )
    # 复用已有 client 的连接池（keep-alive）；仅用单次 request_timeout，勿每轮 new Client
    base_timeout = int(getattr(api_client.settings, "timeout_seconds", 0) or cfg.timeout_seconds)
    timeout = min(base_timeout, cfg.timeout_seconds)
    empty = InnerThoughtResult(text="", interest=None)
    try:
        raw = api_client.complete_raw(
            system_prompt,
            [{"role": "user", "content": user_prompt}],
            temperature=0.9,
            max_tokens=DEFAULT_INNER_THOUGHT_MAX_TOKENS,
            thinking={"type": "disabled"},
            task="background",
            request_timeout=float(timeout),
            # 独白失败可跳过；禁止 8s×3 重试拖死主路径 join
            max_attempts=1,
        )
    except (httpx.TimeoutException, TimeoutError) as exc:
        debug_log(
            "InnerThought",
            "内心独白超时，已跳过",
            {"timeout": timeout, "error": str(exc)},
        )
        return empty
    except ApiRequestError as exc:
        message = str(exc).lower()
        if "timeout" in message or "timed out" in message:
            debug_log(
                "InnerThought",
                "内心独白超时，已跳过",
                {"timeout": timeout, "error": str(exc)},
            )
            return empty
        debug_log("InnerThought", "内心独白生成失败，已跳过", {"error": str(exc)})
        return empty
    except Exception as exc:  # noqa: BLE001
        debug_log("InnerThought", "内心独白生成失败，已跳过", {"error": str(exc)})
        return empty
    return parse_inner_thought_output(raw)


def parse_inner_thought_output(raw: object) -> InnerThoughtResult:
    """解析 Flash 输出：首行 interest，其后为独白正文。"""
    text = str(raw or "").strip()
    if not text:
        return InnerThoughtResult(text="", interest=None)
    # 去掉常见代码块包装，保留换行以便抽 interest
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
    body = "\n".join(lines[body_start:]).strip()
    if interest is None:
        # 模型漏了独立首行时，整段当正文（本轮不注入篇幅块）
        normalized = _normalize_thought_text(text)
    else:
        normalized = _normalize_thought_text(body)
    return InnerThoughtResult(text=normalized, interest=interest)


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
        "不要输出其它标记、前缀或解释。"
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
        "interest: high",
        "あ、この話題好きだ。もっと話したいな。でもあまり熱心に見えると変かな…",
        "",
        "# 思考风格示例（仅正文风格参考；正式输出仍要带 interest 行）",
        _STYLE_FEW_SHOTS,
        "",
        "# 角色档案（节选）",
        _clip(character_excerpt, _CHARACTER_EXCERPT_CHAR_BUDGET) or "（无）",
        "",
        "# 当前情绪状态",
        _clip(mood_summary, _MOOD_CHAR_BUDGET) or "（无特别记录）",
    ]
    if previous_thoughts:
        labels = _window_labels(len(previous_thoughts))
        history_lines = [
            f"{label}：{text}" for label, text in zip(labels, previous_thoughts) if text
        ]
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
    parts.extend(
        [
            "",
            f"请输出 {name} 此刻的 interest 行 + 内心独白：",
        ]
    )
    return "\n".join(parts)


def format_recent_dialogue(
    messages: Sequence[ChatMessage],
    *,
    max_turns: int = 6,
    char_budget: int = _RECENT_DIALOGUE_CHAR_BUDGET,
) -> str:
    """把最近对话压成短文本（user/assistant 交替）。"""
    from app.agent.builtin_tools import message_is_intimacy_continue

    lines: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        # 亲密续投是系统信号，不得标成「用户」
        if message_is_intimacy_continue(message):
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
    # 取末尾约 max_turns*2 条消息
    selected = lines[-(max_turns * 2) :]
    text = "\n".join(selected)
    return _clip(text, char_budget)


def load_character_excerpt(
    *,
    card_path: Path | None,
    system_prompt: str = "",
    budget: int = _CHARACTER_EXCERPT_CHAR_BUDGET,
) -> str:
    if card_path is not None:
        try:
            if card_path.is_file():
                return _clip(card_path.read_text(encoding="utf-8"), budget)
        except OSError:
            pass
    return _clip(system_prompt, budget)


def mood_summary_from_store(memory: Any) -> str:
    try:
        mood = memory.mood_state() if memory is not None else None
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(mood, dict):
        return ""
    return _clip(str(mood.get("content") or ""), _MOOD_CHAR_BUDGET)


def sensory_impression_text() -> str:
    try:
        return sensory_impression_store.format_chat_block() or ""
    except Exception:  # noqa: BLE001
        return ""


def _window_labels(count: int) -> list[str]:
    if count <= 0:
        return []
    if count == 1:
        return ["今"]
    labels = ["今"]
    # 从近到远：前、前々、更前…
    prefixes = ["前", "前々"]
    for index in range(1, count):
        if index - 1 < len(prefixes):
            labels.append(prefixes[index - 1])
        else:
            labels.append(f"{index}轮前")
    # items 是旧→新，labels 当前是新→旧，需要反转标签顺序以对齐
    return list(reversed(labels))


def _normalize_thought_text(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    # 去掉常见包装
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
