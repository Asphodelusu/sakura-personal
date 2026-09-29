"""Personal Qt entity extraction and alias rules, without storage side effects."""
from __future__ import annotations

import re
from typing import Iterable

_ENTITY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[\u30a0-\u30ff]{2,}"),  # 片假名连续（ソフィア、カシマ）
    re.compile(r"[\u4e00-\u9fff]{2,4}(?:くん|さん|ちゃん|先生|先輩)?"),  # 汉字名+敬称
    re.compile(r"[A-Z][a-z]+(?:\s[A-Z][a-z]+)?"),  # 英文名
)
_HONORIFIC_SUFFIXES = ("くん", "さん", "ちゃん", "先生", "先輩")
_STOPWORDS = frozenset({"私", "僕", "俺", "彼", "彼女"})
_MAX_ENTITIES_PER_MEMORY = 12
# 别名展开后写入倒排的键数上限（静态组 + 括号共现）
_MAX_INDEX_KEYS_PER_MEMORY = 36

# 桌宠高频原作/角色名短表（非大词典）。大小写不敏感的英文在建表时一并登记。
_ALIAS_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"ソフィア", "ソフィ", "索菲", "索菲亚", "Sophie"}),
    frozenset({"華淡", "华淡"}),
    frozenset({"水仙", "スイセン"}),
    frozenset({"夜乃桜", "夜乃樱", "Sakura"}),
    frozenset({"槐君", "エンジュ", "Enju"}),
)

_PAREN_ALIAS_RE = re.compile(
    r"([\u4e00-\u9fff\u30a0-\u30ffA-Za-z]{2,24})"
    r"[（(]"
    r"([\u4e00-\u9fff\u30a0-\u30ffA-Za-z]{2,24})"
    r"[）)]"
)


def _build_alias_lookup() -> dict[str, frozenset[str]]:
    lookup: dict[str, frozenset[str]] = {}
    for group in _ALIAS_GROUPS:
        expanded = set(group)
        for name in group:
            if name.isascii() and name.isalpha():
                expanded.add(name.casefold().capitalize())
                expanded.add(name.casefold())
                expanded.add(name.upper())
        frozen = frozenset(expanded)
        for name in frozen:
            lookup[name] = frozen
            if name.isascii():
                lookup[name.casefold()] = frozen
    return lookup


_ALIAS_LOOKUP = _build_alias_lookup()


def extract_entities(content: str) -> set[str]:
    """从文本里抠出候选专有名词（片假名 / 汉字人名+敬称 / 英文名）。"""
    entities: set[str] = set()
    text = str(content or "")
    for pattern in _ENTITY_PATTERNS:
        for match in pattern.finditer(text):
            entity = match.group()
            for suffix in _HONORIFIC_SUFFIXES:
                if entity.endswith(suffix):
                    entity = entity[: -len(suffix)]
                    break
            if len(entity) >= 2 and entity not in _STOPWORDS:
                entities.add(entity)
                if len(entities) >= _MAX_ENTITIES_PER_MEMORY:
                    return entities
    return entities


def _alias_group_for(name: str) -> frozenset[str] | None:
    text = str(name or "").strip()
    if not text:
        return None
    return _ALIAS_LOOKUP.get(text) or _ALIAS_LOOKUP.get(text.casefold())


def is_known_entity_alias(name: str) -> bool:
    """是否落在静态别名表中（供 query 实体筛选保留二字中文名等）。"""
    return _alias_group_for(name) is not None


def find_known_entity_aliases(text: str) -> set[str]:
    """在正文里直接扫静态别名表层（避免中文正则把「索菲是你」整段抠走）。"""
    content = str(text or "")
    if not content:
        return set()
    surfaces = sorted(
        {name for group in _ALIAS_GROUPS for name in group},
        key=len,
        reverse=True,
    )
    found: set[str] = set()
    for name in surfaces:
        if name and name in content:
            found.add(name)
    return found


def extract_paren_alias_pairs(content: str) -> list[tuple[str, str]]:
    """从正文抠出「索菲（ソフィア）」类共现对。"""
    pairs: list[tuple[str, str]] = []
    for left, right in _PAREN_ALIAS_RE.findall(str(content or "")):
        a = left.strip()
        b = right.strip()
        if len(a) < 2 or len(b) < 2:
            continue
        if a in _STOPWORDS or b in _STOPWORDS:
            continue
        pairs.append((a, b))
    return pairs


def expand_entity_aliases(
    entities: Iterable[str],
    *,
    content: str = "",
) -> set[str]:
    """把实体展开为静态别名组 + 正文括号共现名。"""
    result: set[str] = set()
    for raw in entities:
        name = str(raw or "").strip()
        if not name:
            continue
        result.add(name)
        group = _alias_group_for(name)
        if group:
            result.update(group)

    for name in find_known_entity_aliases(content):
        result.add(name)
        group = _alias_group_for(name)
        if group:
            result.update(group)

    for left, right in extract_paren_alias_pairs(content):
        result.add(left)
        result.add(right)
        for side in (left, right):
            group = _alias_group_for(side)
            if group:
                result.update(group)
    return result
