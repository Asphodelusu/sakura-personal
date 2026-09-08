"""Classify subtitle sources and validate lexical Simplified Chinese output."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

_PUNCTUATION = set("…․‥.。、，,！!？?～~ー・「」『』（）() 　")
_SOKUON = set("っッ")
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_KANA_EXCEPT_ALLOWED_RE = re.compile(r"[\u3040-\u30ff]", flags=0)
_ALLOWED_KANA_MARKS = frozenset({"ー", "・"})
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_META_MARKERS = ("翻译：", "以下是", "Translation:")
_VALIDATOR_MODES = frozenset({"legacy", "v2"})


class SourceKind(StrEnum):
    LEXICAL = "lexical"
    NON_LEXICAL = "non_lexical"


@dataclass(frozen=True)
class SourceClassification:
    kind: SourceKind
    local_zh: str = ""


@dataclass(frozen=True)
class TranslationValidation:
    ok: bool
    reason: str = ""


def normalize_validator_mode(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in _VALIDATOR_MODES:
        return text
    return "v2"


def classify_translation_source(text: str) -> SourceClassification:
    raw = str(text or "")
    if _is_non_lexical(raw):
        return SourceClassification(
            kind=SourceKind.NON_LEXICAL,
            local_zh=normalize_non_lexical_japanese(raw),
        )
    return SourceClassification(kind=SourceKind.LEXICAL)


def normalize_non_lexical_japanese(text: str) -> str:
    return "".join(ch for ch in str(text or "") if ch not in _SOKUON).strip()


def validate_lexical_chinese(
    source_ja: str,
    zh: str,
    *,
    mode: str = "v2",
) -> TranslationValidation:
    stripped = str(zh or "").strip()
    if not stripped:
        return TranslationValidation(False, "empty")
    if normalize_validator_mode(mode) == "legacy":
        if _KANA_RE.search(stripped):
            return TranslationValidation(False, "kana")
        if not _CJK_RE.search(stripped):
            return TranslationValidation(False, "no_han")
        return TranslationValidation(True)
    return _validate_v2(str(source_ja or ""), stripped)


def _validate_v2(source_ja: str, zh: str) -> TranslationValidation:
    if any(marker in zh for marker in _META_MARKERS):
        return TranslationValidation(False, "meta")
    if _has_disallowed_kana(zh):
        return TranslationValidation(False, "kana")
    if not _CJK_RE.search(zh):
        return TranslationValidation(False, "no_han")
    if _normalize_compare(zh) == _normalize_compare(source_ja):
        return TranslationValidation(False, "echo")
    limit = max(24, 3 * len(source_ja))
    if len(zh) > limit:
        return TranslationValidation(False, "overlong")
    return TranslationValidation(True)


def _has_disallowed_kana(text: str) -> bool:
    for match in _KANA_EXCEPT_ALLOWED_RE.finditer(text):
        if match.group(0) not in _ALLOWED_KANA_MARKS:
            return True
    return False


def _is_non_lexical(text: str) -> bool:
    for ch in str(text or ""):
        if _is_ignorable_source_char(ch):
            continue
        return False
    return bool(str(text or "").strip())


def _is_ignorable_source_char(ch: str) -> bool:
    if ch.isspace() or ch in _PUNCTUATION or ch in _SOKUON:
        return True
    category = unicodedata.category(ch)
    return category in {"So", "Sk", "Sm", "Mn", "Cf"}


def _normalize_compare(text: str) -> str:
    return "".join(str(text or "").split())
