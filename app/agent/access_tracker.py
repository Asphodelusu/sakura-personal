"""持久化记忆访问追踪：SQLite 版本，替代"每轮全量读写 JSON"的旧实现。

旧实现（access_tracker.json）：整份文件在每次 recall() 都会被完整读入内存
缓存，本轮命中的记忆 id 写进同一个字典，最后又把整个字典序列化整份写回
磁盘——无论历史累计追踪了多少个记忆 id，每轮对话都要付出与追踪总量成正比
的 IO + JSON 解析/序列化开销，是与 chat_history.jsonl 完全同一类问题。

这里换成 SQLite 单表 + 主键索引：
- 读：按本轮候选 id 批量点查（WHERE memory_id IN (...)），只与本轮候选数
  （通常 10 条以内）成正比，与历史累计追踪量无关。
- 写：只 UPSERT 本轮命中的少量 id，不再有"整份重写"这一步，也不需要
  旧实现里的 dirty flag / flush 时机管理——SQLite 的写入本身就是增量的。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path


class AccessTracker:
    """SQLite 持久化的「记忆 id → 最后访问时间」追踪表。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_access (
                    memory_id      TEXT PRIMARY KEY,
                    last_accessed  TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO _meta (key, value) VALUES ('migrated_from_json', '0')"
            )
        self._maybe_migrate_from_json()

    def _maybe_migrate_from_json(self) -> None:
        """首次创建时，从旧的 access_tracker.json 一次性导入历史数据。

        用 _meta 表的 migrated_from_json 标记防止重复迁移（同名旧文件依然保留，
        不主动删除，与 chat_history.py 迁移 JSONL 时的处理方式一致）。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM _meta WHERE key = 'migrated_from_json'"
            ).fetchone()
            already_migrated = bool(row) and row[0] == "1"
        if already_migrated:
            return
        json_path = self._path.with_suffix(".json")
        entries: dict[str, str] = {}
        if json_path.exists():
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    entries = {str(k): str(v) for k, v in data.items() if str(k).strip()}
            except (OSError, ValueError):
                entries = {}
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                if entries:
                    self._conn.executemany(
                        "INSERT OR REPLACE INTO memory_access (memory_id, last_accessed) "
                        "VALUES (?, ?)",
                        list(entries.items()),
                    )
                self._conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES ('migrated_from_json', '1')"
                )
                self._conn.execute("COMMIT")
            except sqlite3.Error:
                self._conn.execute("ROLLBACK")
                raise

    def record_accessed(self, memory_ids: list[str], *, when: str) -> None:
        """批量记录本轮命中的记忆 id，只 UPSERT 这几条，不涉及其余历史数据。

        显式包一层 BEGIN/COMMIT：连接是 isolation_level=None（自动提交）模式，
        没有事务包裹时 executemany 里的每一行都会各自触发一次 WAL 提交，
        行数一多（哪怕只有个位数）延迟也会成倍增加。包成一个事务后
        这几条 UPSERT 只对应一次提交。
        """
        ids = [str(mid).strip() for mid in memory_ids if str(mid).strip()]
        if not ids:
            return
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO memory_access (memory_id, last_accessed) VALUES (?, ?)",
                    [(mid, when) for mid in ids],
                )
                self._conn.execute("COMMIT")
            except sqlite3.Error:
                self._conn.execute("ROLLBACK")
                raise

    def get_last_accessed_bulk(self, memory_ids: list[str]) -> dict[str, str]:
        """按 id 批量点查最后访问时间；只查询传入的这些 id，不做全表扫描。"""
        ids = [str(mid).strip() for mid in memory_ids if str(mid).strip()]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT memory_id, last_accessed FROM memory_access "
                f"WHERE memory_id IN ({placeholders})",
                ids,
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
