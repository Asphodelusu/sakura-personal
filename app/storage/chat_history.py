from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.core.debug_log import debug_log


@dataclass(frozen=True)
class ChatHistoryEntry:
    created_at: str
    role: str
    content: str
    translation: str = ""
    tone: str = ""
    portrait: str = ""
    channel: str = ""

    def display_content(self, subtitle_language: str) -> str:
        if self.role == "assistant" and subtitle_language == "zh" and self.translation.strip():
            return self.translation.strip()
        return self.content


class ChatHistoryStore:
    """聊天历史存储，底层使用 SQLite。

    首次创建时若同目录下存在旧版 JSONL 文件（路径由构造参数 *path* 指定），
    会自动将其内容一次性迁移到 SQLite 数据库，迁移完成后 JSONL 原文件保留不动。

    公共 API 与旧版 JSONL 实现完全一致，调用方无需任何改动。
    """

    def __init__(self, path: Path, assistant_name: str = "桜") -> None:
        # path 保持旧版 JSONL 语义：既作为迁移源，也用于推导 .db 路径
        self.path = path
        self.assistant_name = assistant_name
        self._db_path = path.with_suffix(".db")
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit；事务由 BEGIN/COMMIT 显式管理
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        self._maybe_migrate_from_jsonl()

    # ------------------------------------------------------------------
    # 内部：schema 与迁移
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_history (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at  TEXT NOT NULL,
                    role        TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    translation TEXT NOT NULL DEFAULT '',
                    tone        TEXT NOT NULL DEFAULT '',
                    portrait    TEXT NOT NULL DEFAULT '',
                    channel     TEXT NOT NULL DEFAULT '',
                    debug       TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS _meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO _meta (key, value) VALUES ('migrated_from_jsonl', '0')"
            )

    def _maybe_migrate_from_jsonl(self) -> None:
        """若旧版 JSONL 文件存在且尚未迁移，将其内容导入 SQLite。"""
        if not self.path.is_file():
            return
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM _meta WHERE key = 'migrated_from_jsonl'"
            ).fetchone()
            if row and row["value"] == "1":
                return

            count = self._conn.execute(
                "SELECT COUNT(*) FROM chat_history"
            ).fetchone()[0]
            if count > 0:
                # 数据库已有数据（可能是迁移前通过 append 写入的），
                # 标记为已迁移，避免后续重新导入 JSONL。
                self._conn.execute(
                    "UPDATE _meta SET value = '1' WHERE key = 'migrated_from_jsonl'"
                )
                return

            entries = self._read_jsonl_entries()
            if entries:
                self._conn.execute("BEGIN")
                self._conn.executemany(
                    """
                    INSERT INTO chat_history
                        (created_at, role, content, translation, tone, portrait, channel, debug)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (e.created_at, e.role, e.content, e.translation,
                         e.tone, e.portrait, e.channel, "")
                        for e in entries
                    ],
                )
                self._conn.execute(
                    "UPDATE _meta SET value = '1' WHERE key = 'migrated_from_jsonl'"
                )
                self._conn.execute("COMMIT")
            else:
                self._conn.execute(
                    "UPDATE _meta SET value = '1' WHERE key = 'migrated_from_jsonl'"
                )
            debug_log(
                "Storage",
                "chat_history.migrated_from_jsonl",
                {"source": str(self.path), "db": str(self._db_path), "count": len(entries)},
            )

    def _read_jsonl_entries(self) -> list[ChatHistoryEntry]:
        """读取旧版 JSONL 文件的全部条目（仅供一次性迁移使用）。"""
        entries: list[ChatHistoryEntry] = []
        for raw_line in self.path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            created_at = data.get("created_at")
            role = data.get("role")
            content = data.get("content")
            if not all(isinstance(v, str) for v in (created_at, role, content)):
                continue
            translation = data.get("translation", "")
            tone = data.get("tone", "")
            portrait = data.get("portrait", "")
            channel = data.get("channel", "")
            if not isinstance(translation, str):
                translation = ""
            if not isinstance(tone, str):
                tone = ""
            if not isinstance(portrait, str):
                portrait = ""
            if not isinstance(channel, str):
                channel = ""
            entries.append(
                ChatHistoryEntry(
                    created_at=created_at,
                    role=role,
                    content=content,
                    translation=translation,
                    tone=tone,
                    portrait=portrait,
                    channel=channel,
                )
            )
        return entries

    # ------------------------------------------------------------------
    # 公共 API（与旧版 JSONL 实现完全一致）
    # ------------------------------------------------------------------

    def append(
        self,
        role: str,
        content: str,
        translation: str = "",
        tone: str = "",
        portrait: str = "",
        channel: str = "",
        _debug: dict | None = None,
    ) -> None:
        debug_text = json.dumps(_debug, ensure_ascii=False) if _debug is not None else ""
        created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO chat_history
                    (created_at, role, content, translation, tone, portrait, channel, debug)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    role,
                    content,
                    translation.strip(),
                    tone.strip(),
                    portrait.strip(),
                    channel.strip(),
                    debug_text,
                ),
            )

    def load(self) -> list[ChatHistoryEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT created_at, role, content, translation, tone, portrait, channel "
                "FROM chat_history ORDER BY id ASC"
            ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def load_tail(self, limit: int) -> tuple[list[ChatHistoryEntry], bool]:
        """读取最后 N 条记录。返回 (entries, has_more)。"""
        # 多取一条用于判断 has_more，避免额外 COUNT 查询
        with self._lock:
            rows = self._conn.execute(
                "SELECT created_at, role, content, translation, tone, portrait, channel "
                "FROM chat_history ORDER BY id DESC LIMIT ?",
                (limit + 1,),
            ).fetchall()
        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]
        rows.reverse()  # 按时间正序返回（旧→新）
        return [self._row_to_entry(row) for row in rows], has_more

    def load_older(self, skip_last: int, limit: int) -> tuple[list[ChatHistoryEntry], bool]:
        """跳过最后 N 条，读取更早的 M 条。返回 (entries, has_more)。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT created_at, role, content, translation, tone, portrait, channel "
                "FROM chat_history ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit + 1, skip_last),
            ).fetchall()
        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]
        rows.reverse()
        return [self._row_to_entry(row) for row in rows], has_more

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM chat_history")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> ChatHistoryEntry:
        return ChatHistoryEntry(
            created_at=row["created_at"],
            role=row["role"],
            content=row["content"],
            translation=row["translation"],
            tone=row["tone"],
            portrait=row["portrait"],
            channel=row["channel"],
        )
