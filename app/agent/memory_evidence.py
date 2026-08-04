"""记忆写入的证据校验与瞬态过滤。

相对 Lilith 的纯 ``message.includes(evidence)``，这里多一层软锚定：
模型未给 evidence 时，仍要求记忆正文与对话语料有足够字符双元组重叠，
避免「证据字段忘了填」时误伤，同时挡住完全悬空的幻觉事实。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping, Sequence

# 软锚定：正文与对话共享的有效双元组下限（随正文长度略放宽）
_MIN_SOFT_OVERLAP = 2
_SOFT_OVERLAP_RATIO = 0.08
_MIN_EVIDENCE_CHARS = 2
_MAX_EVIDENCE_CHARS = 240

# 检索时剔除的过常见双元组（中日英混排桌宠语料）
_COMMON_UNITS = frozenset(
    {
        "一个",
        "一样",
        "不是",
        "什么",
        "你们",
        "我们",
        "他们",
        "可以",
        "知道",
        "记得",
        "时候",
        "这个",
        "那个",
        "自己",
        "因为",
        "所以",
        "然后",
        "已经",
        "还是",
        "没有",
        "就是",
        "觉得",
        "喜欢",
        "他说",
        "我说",
        "和他",
        "和他",
        "对他说",
        "的是",
        "了一",
        "了一",
    }
)

_TRANSIENT_PATTERNS = (
    re.compile(
        r"(当前|本机|现在|此刻).{0,6}(时间|时刻|日期|星期|几点|幾點)",
        re.I,
    ),
    re.compile(
        r"(正在播放|当前播放|現在播放|正在听|正在聽|现在听的歌|在听的歌)",
        re.I,
    ),
    re.compile(
        r"(播放状态|播放中|paused|playback).{0,12}(歌曲|音乐|音樂|曲目|track)",
        re.I,
    ),
    re.compile(
        r"(今天天气|今日天气|当前天气|室外温度|气温是)",
        re.I,
    ),
    re.compile(
        r"当前本地时间[：:]",
        re.I,
    ),
)


def normalize_for_evidence(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.casefold()
    text = re.sub(r"\s+", "", text)
    return text


def build_dialog_corpus(dialog_entries: Sequence[Mapping[str, Any]] | None) -> str:
    """整理/校验用对话语料：用户与助手正文 + 中文翻译。"""
    parts: list[str] = []
    for entry in dialog_entries or ():
        if not isinstance(entry, Mapping):
            continue
        for key in ("content", "translation"):
            text = str(entry.get(key) or "").strip()
            if text:
                parts.append(text)
    return "\n".join(parts)


def evidence_in_corpus(evidence: str, corpus: str) -> bool:
    quote = str(evidence or "").strip()
    if len(quote) < _MIN_EVIDENCE_CHARS:
        return False
    if len(quote) > _MAX_EVIDENCE_CHARS:
        quote = quote[:_MAX_EVIDENCE_CHARS]
    raw_corpus = str(corpus or "")
    if quote in raw_corpus:
        return True
    return normalize_for_evidence(quote) in normalize_for_evidence(raw_corpus)


def _search_units(value: str) -> set[str]:
    normalized = normalize_for_evidence(value)
    units: set[str] = set()
    for index in range(max(0, len(normalized) - 1)):
        unit = normalized[index : index + 2]
        if unit in _COMMON_UNITS:
            continue
        if re.fullmatch(r"[\d\W_]+", unit):
            continue
        units.add(unit)
    for word in re.findall(r"[a-z0-9_]{3,}", normalized):
        units.add(word)
    return units


def soft_grounded_in_corpus(content: str, corpus: str) -> bool:
    """正文与对话有足够字符双元组重叠时视为软锚定通过。"""
    text = str(content or "").strip()
    haystack = str(corpus or "").strip()
    if not text or not haystack:
        return False
    if evidence_in_corpus(text, haystack):
        return True
    # 较长连续子串（≥6）直接算锚定
    compact = normalize_for_evidence(text)
    corpus_norm = normalize_for_evidence(haystack)
    if len(compact) >= 6:
        for size in (12, 8, 6):
            if len(compact) < size:
                continue
            for start in range(0, len(compact) - size + 1, max(1, size // 2)):
                piece = compact[start : start + size]
                if piece in corpus_norm:
                    return True
    content_units = _search_units(text)
    if not content_units:
        return False
    corpus_units = _search_units(haystack)
    overlap = len(content_units & corpus_units)
    need = max(_MIN_SOFT_OVERLAP, int(len(content_units) * _SOFT_OVERLAP_RATIO + 0.999))
    return overlap >= need


def looks_like_transient_local_memory(content: str) -> bool:
    """本机瞬时状态（当前时刻、正在播放等）不应进长期记忆。"""
    text = str(content or "").strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _TRANSIENT_PATTERNS)


def validate_memory_write_grounding(
    content: str,
    *,
    evidence: str = "",
    dialog_corpus: str = "",
    require_grounding: bool = True,
) -> tuple[bool, str]:
    """校验一条记忆是否可写入。

    规则（优于纯 evidence 子串）：
    1. 瞬态本机态 → 拒绝
    2. 提供了 evidence 但不在语料中 → 拒绝（防伪造证据）
    3. evidence 命中 → 通过
    4. 否则软锚定命中 → 通过
    5. require_grounding 且都未命中 → 拒绝
    """
    text = str(content or "").strip()
    if not text:
        return False, "empty"
    if looks_like_transient_local_memory(text):
        return False, "transient_local"
    corpus = str(dialog_corpus or "")
    quote = str(evidence or "").strip()
    if quote:
        if evidence_in_corpus(quote, corpus):
            return True, "evidence"
        return False, "evidence_mismatch"
    if not require_grounding:
        return True, "skipped"
    if soft_grounded_in_corpus(text, corpus):
        return True, "soft_ground"
    if not corpus.strip():
        # 无对话语料时无法校验，放行以免误伤显式工具写入
        return True, "no_corpus"
    return False, "ungrounded"


def operation_evidence(operation: Mapping[str, Any] | None) -> str:
    if not isinstance(operation, Mapping):
        return ""
    for key in ("evidence", "quote", "source_span", "anchor"):
        value = str(operation.get(key) or "").strip()
        if value:
            return value
    return ""
