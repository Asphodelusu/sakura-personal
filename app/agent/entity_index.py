"""持久化实体索引：记忆内容里的专有名词 → 涉及到该实体的记忆 id。

多跳召回（"提到 A 的记忆里还提过什么人/词"）原来的做法是：每轮都用正则从
首轮结果里现猜实体，再发一次完整的语义搜索（等价于重新过一遍向量库），
既重复计算，又什么都不记住——下一轮遇到同一个实体还要再猜一次、再搜一次。

这里不引入 Neo4j/Memgraph 之类的外部图数据库（对个人桌面应用来说，多一个
需要独立部署维护的图数据库服务，代价明显大于收益），而是用一张轻量、
持久化的 SQLite 倒排索引：实体 → 记忆 id。写入记忆时顺手记一笔，
查询时直接按索引点查，不必再发语义搜索。这不是完整的关系图（不记录
"A 是 B 的朋友"这类边的语义），只解决"多跳召回要不要重复计算/能不能
持久化"这一层问题，范围上更克制。

人名中日/简称别名：静态短表 + 正文「A（B）」共现；写入与查询双侧展开，
避免「索菲」查不到只挂了「ソフィア」键的旧记忆。
"""

from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path
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


class EntityIndex:
    """SQLite 持久化的「实体 → 记忆 id」倒排索引，WAL 模式支持多线程读写。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS entity_memory (
                    entity     TEXT NOT NULL,
                    memory_id  TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (entity, memory_id)
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_entity_memory_entity ON entity_memory(entity)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO _meta (key, value) VALUES ('backfilled', '0')"
            )

    def is_backfilled(self) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM _meta WHERE key = 'backfilled'"
            ).fetchone()
        return bool(row) and row[0] == "1"

    def mark_backfilled(self) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES ('backfilled', '1')"
            )

    def reset(self) -> None:
        """清空实体→记忆映射并重置回填标记（嵌入迁移后 memory id 全变时用）。"""
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute("DELETE FROM entity_memory")
                self._conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES ('backfilled', '0')"
                )
                self._conn.execute("COMMIT")
            except sqlite3.Error:
                self._conn.execute("ROLLBACK")
                raise

    def index_memory(self, memory_id: str, content: str, *, updated_at: str) -> None:
        """写入/更新某条记忆时调用：抠出实体并登记该记忆 id（幂等，可重复调用）。"""
        memory_id = str(memory_id or "").strip()
        if not memory_id:
            return
        text = str(content or "")
        entities = expand_entity_aliases(extract_entities(text), content=text)
        # 括号共现名即使正则没抽全，也要挂上键
        for left, right in extract_paren_alias_pairs(text):
            entities.add(left)
            entities.add(right)
            entities |= expand_entity_aliases((left, right), content=text)
        if len(entities) > _MAX_INDEX_KEYS_PER_MEMORY:
            # 优先保留静态别名命中与较短专名
            prioritized = sorted(
                entities,
                key=lambda name: (0 if _alias_group_for(name) else 1, len(name), name),
            )
            entities = set(prioritized[:_MAX_INDEX_KEYS_PER_MEMORY])
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    "DELETE FROM entity_memory WHERE memory_id = ?", (memory_id,)
                )
                if entities:
                    self._conn.executemany(
                        "INSERT OR REPLACE INTO entity_memory (entity, memory_id, updated_at) "
                        "VALUES (?, ?, ?)",
                        [(entity, memory_id, updated_at) for entity in entities],
                    )
                self._conn.execute("COMMIT")
            except sqlite3.Error:
                self._conn.execute("ROLLBACK")
                raise

    def remove_memory(self, memory_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM entity_memory WHERE memory_id = ?", (str(memory_id),)
            )

    def lookup_memory_ids(
        self,
        entities: Iterable[str],
        *,
        exclude_ids: Iterable[str] = (),
        limit: int = 20,
    ) -> list[str]:
        """按实体查涉及到的记忆 id，按最近一次写入时间倒序。"""
        entity_list = sorted(expand_entity_aliases(entities))
        if not entity_list:
            return []
        exclude = {str(i) for i in exclude_ids}
        placeholders = ",".join("?" for _ in entity_list)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT memory_id, MAX(updated_at) AS latest
                FROM entity_memory
                WHERE entity IN ({placeholders})
                GROUP BY memory_id
                ORDER BY latest DESC
                LIMIT ?
                """,
                (*entity_list, limit + len(exclude)),
            ).fetchall()
        result = [row[0] for row in rows if row[0] not in exclude]
        return result[:limit]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
