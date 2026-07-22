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

    def index_memory(self, memory_id: str, content: str, *, updated_at: str) -> None:
        """写入/更新某条记忆时调用：抠出实体并登记该记忆 id（幂等，可重复调用）。"""
        memory_id = str(memory_id or "").strip()
        if not memory_id:
            return
        entities = extract_entities(content)
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
        entity_list = [str(e) for e in entities if str(e).strip()]
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
