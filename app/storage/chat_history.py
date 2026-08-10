from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.core.debug_log import debug_log


_HISTORY_COLUMNS = (
    "id, created_at, role, content, translation, tone, portrait, channel"
)
_DIALOGUE_ROLES = ("user", "assistant")
_MAX_SEARCH_LIMIT = 50


@dataclass(frozen=True)
class ChatHistoryEntry:
    created_at: str
    role: str
    content: str
    translation: str = ""
    tone: str = ""
    portrait: str = ""
    channel: str = ""
    id: int = 0

    def display_content(self, subtitle_language: str) -> str:
        if self.role == "assistant" and subtitle_language == "zh" and self.translation.strip():
            return self.translation.strip()
        return self.content


def _escape_like(keyword: str) -> str:
    """转义 LIKE 通配符，配合 ESCAPE '\\' 使用。"""
    return (
        keyword.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


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
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_history_created_at "
                "ON chat_history(created_at)"
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
    ) -> int:
        """写入一条历史；返回新行 id（供异步字幕回填）。"""
        debug_text = json.dumps(_debug, ensure_ascii=False) if _debug is not None else ""
        created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._lock:
            cursor = self._conn.execute(
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
            return int(cursor.lastrowid or 0)

    def update_translation(self, entry_id: int, translation: str) -> bool:
        """按 id 回填中文字幕；不存在则返回 False。"""
        try:
            row_id = int(entry_id)
        except (TypeError, ValueError):
            return False
        text = str(translation or "").strip()
        if row_id <= 0 or not text:
            return False
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE chat_history SET translation = ? WHERE id = ?",
                (text, row_id),
            )
            return int(cursor.rowcount or 0) > 0

    def load(self) -> list[ChatHistoryEntry]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_HISTORY_COLUMNS} FROM chat_history ORDER BY id ASC"
            ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def count(self) -> int:
        """当前库中消息条数（含 system 等非对话角色）。"""
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM chat_history").fetchone()
        return int(row[0] if row is not None else 0)

    def load_slice(self, offset: int, *, limit: int | None = None) -> list[ChatHistoryEntry]:
        """按行偏移读取（0-based，与 processed_history_count 对齐）。"""
        try:
            off = max(0, int(offset))
        except (TypeError, ValueError):
            off = 0
        with self._lock:
            if limit is None:
                rows = self._conn.execute(
                    f"SELECT {_HISTORY_COLUMNS} FROM chat_history "
                    "ORDER BY id ASC LIMIT -1 OFFSET ?",
                    (off,),
                ).fetchall()
            else:
                try:
                    capped = max(1, int(limit))
                except (TypeError, ValueError):
                    capped = 1
                rows = self._conn.execute(
                    f"SELECT {_HISTORY_COLUMNS} FROM chat_history "
                    "ORDER BY id ASC LIMIT ? OFFSET ?",
                    (capped, off),
                ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def load_tail(self, limit: int) -> tuple[list[ChatHistoryEntry], bool]:
        """读取最后 N 条记录。返回 (entries, has_more)。"""
        # 多取一条用于判断 has_more，避免额外 COUNT 查询
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_HISTORY_COLUMNS} FROM chat_history ORDER BY id DESC LIMIT ?",
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
                f"SELECT {_HISTORY_COLUMNS} FROM chat_history "
                "ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit + 1, skip_last),
            ).fetchall()
        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]
        rows.reverse()
        return [self._row_to_entry(row) for row in rows], has_more

    def search_between(
        self,
        start: str | None = None,
        end: str | None = None,
        keyword: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[ChatHistoryEntry], bool, int]:
        """按时间范围 + 关键词检索对话消息（仅 user/assistant）。

        start/end 为本地时区 ISO 字符串（与 created_at 同格式）；None=不限。
        - 有时间窗或关键词：按 id ASC 分页（从窗/命中起点读，不丢开头）
        - 无任何筛选：按 id DESC 分页（最近对话），页内再正序返回
        返回 (entries, has_more, total_count)。limit 硬封顶 50。
        """
        try:
            capped = max(1, min(int(limit), _MAX_SEARCH_LIMIT))
        except (TypeError, ValueError):
            capped = 20
        try:
            skip = max(0, int(offset))
        except (TypeError, ValueError):
            skip = 0
        start_text = str(start or "").strip() or None
        end_text = str(end or "").strip() or None
        keyword_text = str(keyword or "").strip() or None

        role_placeholders = ", ".join("?" for _ in _DIALOGUE_ROLES)
        clauses = [f"role IN ({role_placeholders})"]
        params: list[object] = list(_DIALOGUE_ROLES)

        if start_text is not None:
            clauses.append("created_at >= ?")
            params.append(start_text)
        if end_text is not None:
            clauses.append("created_at <= ?")
            params.append(end_text)
        if keyword_text is not None:
            escaped = _escape_like(keyword_text)
            clauses.append(
                "(content LIKE ? ESCAPE '\\' OR translation LIKE ? ESCAPE '\\')"
            )
            like = f"%{escaped}%"
            params.extend([like, like])

        where = " AND ".join(clauses)
        # 有筛选：正序扫窗/命中；无筛选：最近优先（DESC 分页）
        order_asc = start_text is not None or end_text is not None or keyword_text is not None
        order_sql = "ASC" if order_asc else "DESC"
        count_sql = f"SELECT COUNT(*) FROM chat_history WHERE {where}"
        sql = (
            f"SELECT {_HISTORY_COLUMNS} FROM chat_history "
            f"WHERE {where} ORDER BY id {order_sql} LIMIT ? OFFSET ?"
        )
        page_params = [*params, capped, skip]

        with self._lock:
            total_count = int(self._conn.execute(count_sql, params).fetchone()[0])
            rows = self._conn.execute(sql, page_params).fetchall()

        if not order_asc:
            rows = list(reversed(rows))
        entries = [self._row_to_entry(row) for row in rows]
        has_more = skip + len(entries) < total_count
        return entries, has_more, total_count

    def context_around(
        self,
        entry_id: int,
        *,
        before: int = 3,
        after: int = 3,
    ) -> dict:
        """以消息 id 为锚点，取前后对话上下文（仅 user/assistant）。

        entry_id <= 0 明确失败。返回 shape 对齐 memory_timeline.build_timeline。
        """
        try:
            anchor_id = int(entry_id)
        except (TypeError, ValueError):
            anchor_id = 0
        try:
            before_n = max(0, min(int(before), 10))
        except (TypeError, ValueError):
            before_n = 3
        try:
            after_n = max(0, min(int(after), 10))
        except (TypeError, ValueError):
            after_n = 3

        if anchor_id <= 0:
            return {
                "before": [],
                "target": None,
                "after": [],
                "anchor_id": anchor_id,
                "error": "entry_id 无效（必须是正整数）。",
            }

        role_placeholders = ", ".join("?" for _ in _DIALOGUE_ROLES)
        role_params = list(_DIALOGUE_ROLES)

        with self._lock:
            target_row = self._conn.execute(
                f"SELECT {_HISTORY_COLUMNS} FROM chat_history "
                f"WHERE id = ? AND role IN ({role_placeholders})",
                [anchor_id, *role_params],
            ).fetchone()
            if target_row is None:
                return {
                    "before": [],
                    "target": None,
                    "after": [],
                    "anchor_id": anchor_id,
                    "hint": "未找到该消息（可能已删除，或不是 user/assistant）。",
                }

            before_rows: list[sqlite3.Row] = []
            if before_n > 0:
                before_rows = self._conn.execute(
                    f"SELECT {_HISTORY_COLUMNS} FROM chat_history "
                    f"WHERE id < ? AND role IN ({role_placeholders}) "
                    "ORDER BY id DESC LIMIT ?",
                    [anchor_id, *role_params, before_n],
                ).fetchall()
                before_rows.reverse()

            after_rows: list[sqlite3.Row] = []
            if after_n > 0:
                after_rows = self._conn.execute(
                    f"SELECT {_HISTORY_COLUMNS} FROM chat_history "
                    f"WHERE id > ? AND role IN ({role_placeholders}) "
                    "ORDER BY id ASC LIMIT ?",
                    [anchor_id, *role_params, after_n],
                ).fetchall()

        return {
            "before": [self._row_to_entry(row) for row in before_rows],
            "target": self._row_to_entry(target_row),
            "after": [self._row_to_entry(row) for row in after_rows],
            "anchor_id": anchor_id,
        }

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
            id=int(row["id"]),
        )
