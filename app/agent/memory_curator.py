from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.debug_log import debug_log
from app.agent.memory import MemoryStore
from app.core.cancellation import CancelChecker, OperationCancelled, check_cancelled
from app.storage.atomic import atomic_write_text
from app.storage.chat_history import ChatHistoryEntry


DEFAULT_AUTO_MEMORY_TRIGGER_TURNS = 8
DEFAULT_AUTO_MEMORY_BACKFILL_LIMIT = 200
MAX_CURATION_CHUNK_MESSAGES = 32
MAX_CURATION_CHUNK_CHARS = 12000
# 整理时一次性注入给模型的现有记忆条数上限，远大于日常摘要，便于全量对照去重纠错。
CURATION_MEMORY_SNAPSHOT_LIMIT = 500
# 现有记忆清单注入的字符预算，超出后截断以保护 token 开销。
CURATION_MEMORY_SNAPSHOT_CHAR_BUDGET = 20000
# 单次整理允许写回的操作数量上限，避免异常输出放大写入。
MAX_CURATION_OPERATIONS = 50


@dataclass(frozen=True)
class MemoryCurationSettings:
    enabled: bool = True
    trigger_turns: int = DEFAULT_AUTO_MEMORY_TRIGGER_TURNS
    backfill_limit: int = DEFAULT_AUTO_MEMORY_BACKFILL_LIMIT


@dataclass(frozen=True)
class MemoryCurationResult:
    created: int = 0
    updated: int = 0
    archived: int = 0
    ignored: int = 0
    processed_entries: int = 0
    returned: int = 0
    unclassified: int = 0
    event_counts: dict[str, int] | None = None

    def summary(self) -> str:
        return (
            f"整理完成：新增 {self.created} 条，更新 {self.updated} 条，"
            f"删除 {self.archived} 条，忽略 {self.ignored} 条。"
        )


class MemoryCurationState:
    """记录自动整理进度，避免重复处理历史。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def snapshot(self) -> dict[str, Any]:
        if not self.path.exists():
            return _normalize_state({})
        try:
            raw_data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return _normalize_state({})
        return _normalize_state(raw_data)

    def pending_turns(self) -> int:
        return int(self.snapshot()["pending_turns"])

    def increment_pending_turns(self) -> int:
        state = self.snapshot()
        state["pending_turns"] = int(state["pending_turns"]) + 1
        self._save(state)
        return int(state["pending_turns"])

    def mark_processed(
        self,
        processed_history_count: int,
        *,
        consumed_turns: int = 0,
        backfill_completed: bool | None = None,
    ) -> None:
        state = self.snapshot()
        state["processed_history_count"] = max(0, processed_history_count)
        state["pending_turns"] = max(0, int(state["pending_turns"]) - max(0, consumed_turns))
        if backfill_completed is not None:
            state["backfill_completed"] = bool(backfill_completed)
        self._save(state)

    def mark_history_cleared(self) -> None:
        state = self.snapshot()
        state["processed_history_count"] = 0
        state["pending_turns"] = 0
        state["backfill_completed"] = True
        self._save(state)

    def unprocessed_entries(self, entries: list[ChatHistoryEntry]) -> list[ChatHistoryEntry]:
        state = self.snapshot()
        processed = int(state["processed_history_count"])
        if processed < 0 or processed > len(entries):
            processed = 0
        return entries[processed:]

    def _save(self, state: dict[str, Any]) -> None:
        atomic_write_text(
            self.path,
            json.dumps(_normalize_state(state), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


class MemoryCurator:
    """以桌宠自己（人格卡）的第一人称视角，把聊天历史整理为长期记忆。

    不再依赖 mem0 内置的第三人称抽取 prompt：每段对话整理时都会注入人格卡和当前
    全部记忆，让模型像本人整理日记一样，输出对记忆的新增 / 更新 / 删除操作并写回。
    mem0 仅承担底层的存储、向量检索与 embedding。
    """

    def __init__(
        self,
        api_client: Any,
        memory_store: MemoryStore,
        *,
        system_prompt: str = "",
    ) -> None:
        self.api_client = api_client
        self.memory_store = memory_store
        # 人格卡文本，作为第一人称整理 prompt 的基底；缺省时只用整理任务说明。
        self.system_prompt = (system_prompt or "").strip()

    def curate_entries(
        self,
        entries: list[ChatHistoryEntry],
        *,
        cancel_checker: CancelChecker | None = None,
    ) -> MemoryCurationResult:
        if self.api_client is None:
            # 缺少可用模型时无法进行第一人称整理，直接跳过而不报错。
            return MemoryCurationResult(processed_entries=len(entries))
        if not _entries_for_model(entries):
            return MemoryCurationResult(processed_entries=len(entries))

        created = 0
        updated = 0
        archived = 0
        ignored = 0
        event_counts: dict[str, int] = {}
        for chunk in _chunk_entries_for_curation(entries):
            check_cancelled(cancel_checker)
            dialog_entries = _entries_for_model(chunk)
            if not dialog_entries:
                continue
            # 每个 chunk 整理前重新拉取全量记忆，确保前一段写入的记忆能被后一段对照，避免重复。
            existing = self._load_existing_memories()
            check_cancelled(cancel_checker)
            operations = self._extract_operations(
                dialog_entries,
                existing,
                cancel_checker=cancel_checker,
            )
            check_cancelled(cancel_checker)
            counts = self._apply_operations(operations, existing)
            created += counts["created"]
            updated += counts["updated"]
            archived += counts["archived"]
            ignored += counts["ignored"]
            _merge_event_counts(event_counts, counts["event_counts"])
        return MemoryCurationResult(
            created=created,
            updated=updated,
            archived=archived,
            ignored=ignored,
            processed_entries=len(entries),
            returned=created + updated + archived,
            unclassified=0,
            event_counts=event_counts,
        )

    def _load_existing_memories(self) -> list[dict[str, Any]]:
        """读取当前角色的全部长期记忆；读取失败时降级为空清单（模型只做新增）。"""

        try:
            return self.memory_store.list_memories(limit=CURATION_MEMORY_SNAPSHOT_LIMIT)
        except OperationCancelled:
            raise
        except Exception as exc:  # 记忆读取失败不应中断整理，退化为只新增。
            debug_log("Memory", "记忆整理读取现有记忆失败", {"error": str(exc)})
            return []

    def _build_self_curation_system_prompt(self) -> str:
        if not self.system_prompt:
            return _SELF_CURATION_TASK_PROMPT
        return f"{self.system_prompt}\n\n{_SELF_CURATION_TASK_PROMPT}"

    def _extract_operations(
        self,
        dialog_entries: list[dict[str, str]],
        existing: list[dict[str, Any]],
        *,
        cancel_checker: CancelChecker | None = None,
    ) -> list[dict[str, Any]]:
        """让模型以第一人称对照已有记忆，产出整理操作；解析失败时视为无操作。"""

        system_prompt = self._build_self_curation_system_prompt()
        user_prompt = _build_curation_user_prompt(
            _format_existing_memories(existing),
            dialog_entries,
        )
        raw = self.api_client.complete_raw(
            system_prompt,
            [{"role": "user", "content": user_prompt}],
            temperature=0.2,
            response_format={"type": "json_object"},
            max_tokens=2000,
            cancel_checker=cancel_checker,
        )
        operations = _parse_curation_operations(raw)
        debug_log(
            "Memory",
            "第一人称记忆整理抽取完成",
            {
                "existing_count": len(existing),
                "dialog_count": len(dialog_entries),
                "operation_count": len(operations),
                "raw_chars": len(raw or ""),
            },
        )
        return operations

    def _apply_operations(
        self,
        operations: list[dict[str, Any]],
        existing: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """把整理操作写回记忆库；id 必须真实存在，单条失败只跳过不中断。"""

        existing_ids = {
            str(memory.get("id", "")).strip()
            for memory in existing
            if str(memory.get("id", "")).strip()
        }
        created = 0
        updated = 0
        archived = 0
        ignored = 0
        event_counts: dict[str, int] = {}
        for operation in operations[:MAX_CURATION_OPERATIONS]:
            if not isinstance(operation, dict):
                ignored += 1
                continue
            action = str(operation.get("op") or operation.get("action") or "").strip().lower()
            memory_id = str(operation.get("id") or operation.get("memory_id") or "").strip()
            content = str(operation.get("content") or operation.get("memory") or "").strip()
            try:
                if action == "add":
                    if not content:
                        ignored += 1
                        continue
                    self.memory_store.create_memory(
                        {"content": content, "source": "self_curation"},
                        allow_sensitive=True,
                    )
                    created += 1
                    event_counts["ADD"] = event_counts.get("ADD", 0) + 1
                elif action == "update":
                    if memory_id not in existing_ids or not content:
                        debug_log(
                            "Memory",
                            "跳过无效的记忆更新操作",
                            {"id": memory_id, "has_content": bool(content)},
                        )
                        ignored += 1
                        continue
                    self.memory_store.update_memory({"id": memory_id, "content": content})
                    updated += 1
                    event_counts["UPDATE"] = event_counts.get("UPDATE", 0) + 1
                elif action == "delete":
                    if memory_id not in existing_ids:
                        debug_log("Memory", "跳过无效的记忆删除操作", {"id": memory_id})
                        ignored += 1
                        continue
                    self.memory_store.delete_memory({"id": memory_id})
                    existing_ids.discard(memory_id)
                    archived += 1
                    event_counts["DELETE"] = event_counts.get("DELETE", 0) + 1
                else:
                    ignored += 1
            except Exception as exc:  # 单条写回失败只跳过，保留其它可用结果。
                debug_log(
                    "Memory",
                    "记忆整理写回失败",
                    {"op": action, "id": memory_id, "error": str(exc)},
                )
                ignored += 1
                continue
        return {
            "created": created,
            "updated": updated,
            "archived": archived,
            "ignored": ignored,
            "event_counts": event_counts,
        }


def _merge_event_counts(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0) + value


def _chunk_entries_for_curation(entries: list[ChatHistoryEntry]) -> list[list[ChatHistoryEntry]]:
    chunks: list[list[ChatHistoryEntry]] = []
    current: list[ChatHistoryEntry] = []
    current_messages = 0
    current_chars = 0
    for entry in entries:
        model_entry = _entry_for_model(entry)
        if model_entry is None:
            continue
        entry_chars = _model_entry_char_count(model_entry)
        if current and (
            current_messages >= MAX_CURATION_CHUNK_MESSAGES
            or current_chars + entry_chars > MAX_CURATION_CHUNK_CHARS
        ):
            chunks.append(current)
            current = []
            current_messages = 0
            current_chars = 0
        current.append(entry)
        current_messages += 1
        current_chars += entry_chars
    if current:
        chunks.append(current)
    return chunks


def _entry_for_model(entry: ChatHistoryEntry) -> dict[str, str] | None:
    if entry.role not in {"user", "assistant"}:
        return None
    content = entry.content.strip()
    if not content:
        return None
    return {
        "created_at": entry.created_at,
        "role": entry.role,
        "content": content,
        "translation": entry.translation.strip(),
    }


def _model_entry_char_count(entry: dict[str, str]) -> int:
    return (
        len(entry.get("created_at", ""))
        + len(entry.get("role", ""))
        + len(entry.get("content", ""))
        + len(entry.get("translation", ""))
    )


def _entries_for_model(entries: list[ChatHistoryEntry]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for entry in entries:
        model_entry = _entry_for_model(entry)
        if model_entry is not None:
            result.append(model_entry)
    return result


# 第一人称整理任务说明，拼接在人格卡之后，让模型以「桌宠本人」的视角整理自己的记忆。
_SELF_CURATION_TASK_PROMPT = (
    "现在没有人和你说话，你正在安静地整理自己的长期记忆，就像在更新只属于你自己的记忆笔记。\n"
    "下面会给你两部分内容：\n"
    "1. 你目前已经记住的全部长期记忆（每条带一个 id）；\n"
    "2. 你和主人最近的一段新对话。\n\n"
    "请完全以「你自己」的第一人称视角，判断这段对话里有没有值得长期记住的事情，并对照已有记忆决定如何整理：\n"
    "- 出现了之前没记过、值得长期记住的事实 → 新增一条记忆；\n"
    "- 已有记忆需要补充、纠正或与新信息冲突 → 更新对应那条记忆；\n"
    "- 已有记忆已经明确失效、错误或不该再保留 → 删除对应那条记忆；\n"
    "- 没有值得整理的内容时，就不要产生任何操作。\n\n"
    "只保留对长期陪伴与协作真正有用、且能独立理解的事实；忽略寒暄、一次性的临时提醒、转瞬即逝的情绪和无长期价值的内容。\n"
    "所有记忆内容必须使用简体中文，并以你自己的口吻或客观事实记录（例如「主人喜欢……」「我和主人约定……」）。\n\n"
    "必须只返回严格 JSON，格式如下：\n"
    "{\"operations\":[\n"
    "  {\"op\":\"add\",\"content\":\"要新增的记忆内容\"},\n"
    "  {\"op\":\"update\",\"id\":\"已有记忆的id\",\"content\":\"更新后的完整记忆内容\"},\n"
    "  {\"op\":\"delete\",\"id\":\"已有记忆的id\"}\n"
    "]}\n"
    "其中 update 和 delete 的 id 必须来自下面「已有记忆」列表里真实存在的 id，不要编造 id。"
    "没有要整理的内容时返回 {\"operations\":[]}。"
)


def _format_existing_memories(memories: list[dict[str, Any]]) -> str:
    """把现有记忆格式化成带 id 的清单文本，超出字符预算时截断保护 token。"""

    lines: list[str] = []
    used = 0
    truncated = False
    for memory in memories:
        memory_id = str(memory.get("id", "")).strip()
        content = str(memory.get("content", "")).strip()
        if not memory_id or not content:
            continue
        line = f"- [{memory_id}] {content}"
        if used + len(line) > CURATION_MEMORY_SNAPSHOT_CHAR_BUDGET and lines:
            truncated = True
            break
        lines.append(line)
        used += len(line) + 1
    if truncated:
        debug_log(
            "Memory",
            "现有记忆超出注入预算已截断",
            {"included": len(lines), "total": len(memories)},
        )
    return "\n".join(lines) if lines else "（暂无）"


def _build_curation_user_prompt(existing_block: str, dialog_entries: list[dict[str, str]]) -> str:
    return (
        "【我目前的长期记忆】\n"
        f"{existing_block}\n\n"
        "【最近的新对话】\n"
        f"{json.dumps(dialog_entries, ensure_ascii=False)}"
    )


def _parse_curation_operations(raw: str) -> list[dict[str, Any]]:
    """解析模型返回的整理操作；非法 JSON 视为无操作，不抛错以免中断整理。"""

    data = _load_json_object(raw)
    candidates = data.get("operations") or data.get("operation") or []
    if not isinstance(candidates, list):
        return []
    operations: list[dict[str, Any]] = []
    for item in candidates:
        if isinstance(item, dict):
            operations.append(item)
    return operations


def _load_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}


def _normalize_state(raw_data: Any) -> dict[str, Any]:
    data = raw_data if isinstance(raw_data, dict) else {}
    return {
        "processed_history_count": max(0, _int_value(data.get("processed_history_count"), default=0)),
        "pending_turns": max(0, _int_value(data.get("pending_turns"), default=0)),
        "backfill_completed": bool(data.get("backfill_completed", False)),
    }


def _int_value(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
