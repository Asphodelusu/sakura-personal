from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from app.backchannel.models import EMOTIONS
from app.agent.memory import (
    DEFAULT_MEMORY_CONFIDENCE,
    DEFAULT_MEMORY_IMPORTANCE,
    MEMORY_LAYER_SEMANTIC,
    MEMORY_LAYERS,
    MemoryStore,
    looks_like_sensitive_memory,
)
from app.agent.persona_state import normalize_emotion
from app.core.cancellation import CancelChecker, OperationCancelled, check_cancelled
from app.core.debug_log import debug_log
from app.llm.json_completion import complete_background_json, load_json_object
from app.storage.atomic import atomic_write_text
from app.storage.chat_history import ChatHistoryEntry


DEFAULT_AUTO_MEMORY_TRIGGER_TURNS = 8
DEFAULT_AUTO_MEMORY_BACKFILL_LIMIT = 200
DEFAULT_AUTO_MEMORY_IDLE_MINUTES = 12
DEFAULT_AUTO_MEMORY_MIN_TURNS = 2
DEFAULT_AUTO_MEMORY_COOLDOWN_MINUTES = 25
DEFAULT_AUTO_MEMORY_LONG_IDLE_MINUTES = 30
DEFAULT_AUTO_MEMORY_CATCH_UP_TURNS = 12
MAX_CURATION_CHUNK_MESSAGES = 32
MAX_CURATION_CHUNK_CHARS = 12000
# 整理时一次性注入给模型的现有记忆条数上限，远大于日常摘要，便于全量对照去重纠错。
CURATION_MEMORY_SNAPSHOT_LIMIT = 500
# 现有记忆清单注入的字符预算，超出后截断以保护 token 开销。
CURATION_MEMORY_SNAPSHOT_CHAR_BUDGET = 20000
# 单次整理允许写回的操作数量上限，避免异常输出放大写入。
MAX_CURATION_OPERATIONS = 50
MIN_AUTO_WRITE_CONFIDENCE = 0.55
CURATION_DUPLICATE_SIMILARITY = 0.92
CURATION_MERGE_SIMILARITY = 0.78
MAX_CURATION_OPERATIONS_PER_LAYER = 20


@dataclass(frozen=True)
class MemoryCurationSettings:
    enabled: bool = True
    backfill_limit: int = DEFAULT_AUTO_MEMORY_BACKFILL_LIMIT
    idle_minutes: int = DEFAULT_AUTO_MEMORY_IDLE_MINUTES
    min_turns: int = DEFAULT_AUTO_MEMORY_MIN_TURNS
    cooldown_minutes: int = DEFAULT_AUTO_MEMORY_COOLDOWN_MINUTES
    long_idle_minutes: int = DEFAULT_AUTO_MEMORY_LONG_IDLE_MINUTES
    catch_up_turns: int = DEFAULT_AUTO_MEMORY_CATCH_UP_TURNS
    # 旧版「每 N 轮」字段，仅用于 YAML 迁移为 catch_up_turns。
    trigger_turns: int = DEFAULT_AUTO_MEMORY_TRIGGER_TURNS

    def normalized(self) -> MemoryCurationSettings:
        idle_minutes = max(3, min(120, int(self.idle_minutes)))
        min_turns = max(1, min(20, int(self.min_turns)))
        cooldown_minutes = max(5, min(240, int(self.cooldown_minutes)))
        long_idle_minutes = max(idle_minutes, min(240, int(self.long_idle_minutes)))
        catch_up_turns = max(min_turns, min(50, int(self.catch_up_turns)))
        backfill_limit = max(1, min(500, int(self.backfill_limit)))
        return MemoryCurationSettings(
            enabled=bool(self.enabled),
            backfill_limit=backfill_limit,
            idle_minutes=idle_minutes,
            min_turns=min_turns,
            cooldown_minutes=cooldown_minutes,
            long_idle_minutes=long_idle_minutes,
            catch_up_turns=catch_up_turns,
            trigger_turns=int(self.trigger_turns),
        )


def evaluate_idle_curation_trigger(
    settings: MemoryCurationSettings,
    *,
    silence_seconds: float,
    pending_turns: int,
    seconds_since_last_curation: float | None,
    has_unprocessed_entries: bool,
) -> bool:
    """混合静默触发：静默 + 最少轮数/长空闲 + 整理冷却（追赶轮数可跳过冷却）。"""
    normalized = settings.normalized()
    if not normalized.enabled:
        return False
    if not has_unprocessed_entries or pending_turns < 1:
        return False

    idle_seconds = normalized.idle_minutes * 60
    if silence_seconds + 1e-6 < idle_seconds:
        return False

    long_idle_ok = silence_seconds + 1e-6 >= normalized.long_idle_minutes * 60
    turns_ok = pending_turns >= normalized.min_turns
    catch_up = pending_turns >= normalized.catch_up_turns
    if not (turns_ok or long_idle_ok or catch_up):
        return False

    if not catch_up and seconds_since_last_curation is not None:
        if seconds_since_last_curation + 1e-6 < normalized.cooldown_minutes * 60:
            return False
    return True


def seconds_since_iso_timestamp(value: str | None) -> float | None:
    if not value or not str(value).strip():
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())


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

    def last_curation_at(self) -> str:
        return str(self.snapshot().get("last_curation_at") or "").strip()

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
        state["last_curation_at"] = datetime.now().astimezone().isoformat()
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

    def set_system_prompt(self, system_prompt: str) -> None:
        self.system_prompt = (system_prompt or "").strip()

    def snapshot(
        self,
        *,
        memory_store: MemoryStore | None = None,
        system_prompt: str | None = None,
    ) -> "MemoryCurator":
        return MemoryCurator(
            self.api_client,
            memory_store or self.memory_store,
            system_prompt=self.system_prompt if system_prompt is None else system_prompt,
        )

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
        """后台 JSON 任务只用整理专用说明，不注入完整人格卡（避免与 JSON 指令冲突）。"""
        return _SELF_CURATION_TASK_PROMPT

    def _load_mood_history_text(self) -> str:
        """读取心情历史并格式化为简短的回顾文本；无历史时返回空串。"""
        try:
            history = self.memory_store.mood_history()
        except Exception:
            return ""
        if not history:
            return ""
        lines: list[str] = []
        for i, entry in enumerate(history[:5], 1):
            ts = entry.get("timestamp", "")
            content = entry.get("content", "")
            if not content.strip():
                continue
            time_label = ts[:16] if ts else ""
            lines.append(f"{i}. [{time_label}] {content}")
        if not lines:
            return ""
        return "以下是你的心情变化记录，最近的在上面：\n" + "\n".join(lines)

    def _load_user_emotion_history_text(self) -> str:
        """读取用户情绪历史并格式化为简短的回顾文本；无历史时返回空串。"""
        try:
            history = self.memory_store.user_emotion_history()
        except Exception:
            return ""
        if not history:
            return ""
        lines: list[str] = []
        for i, entry in enumerate(history[:5], 1):
            ts = entry.get("timestamp", "")
            content = entry.get("content", "")
            if not content.strip():
                continue
            time_label = ts[:16] if ts else ""
            lines.append(f"{i}. [{time_label}] {content}")
        if not lines:
            return ""
        return "以下是对方最近几次对话中流露的情绪轨迹，最近的在上面：\n" + "\n".join(lines)

    def _extract_operations(
        self,
        dialog_entries: list[dict[str, str]],
        existing: list[dict[str, Any]],
        *,
        cancel_checker: CancelChecker | None = None,
    ) -> list[dict[str, Any]]:
        """让模型以第一人称对照已有记忆，产出整理操作；解析失败时视为无操作。"""

        system_prompt = self._build_self_curation_system_prompt()
        mood_history_block = self._load_mood_history_text()
        user_emotion_history_block = self._load_user_emotion_history_text()
        user_prompt = _build_curation_user_prompt(
            _format_existing_memories(existing),
            dialog_entries,
            mood_history_block=mood_history_block,
            user_emotion_history_block=user_emotion_history_block,
        )
        llm_messages = [{"role": "user", "content": user_prompt}]
        repair_hint = (
            "上一条输出不是合法 JSON。请只返回严格 JSON，"
            '格式为 {"operations":[{"op":"add","content":"...","layer":"semantic"}]}，'
            "不要解释、不要推理、不要 Markdown。"
        )
        try:
            data, raw = complete_background_json(
                self.api_client,
                system_prompt,
                llm_messages,
                cancel_checker=cancel_checker,
                repair_user_message=repair_hint,
                log_label="MemoryCuration",
            )
        except OperationCancelled:
            raise
        except Exception as exc:
            debug_log("Memory", "记忆整理 LLM 调用失败", {"error": str(exc)})
            return []
        operations = _parse_curation_operations_from_data(data) if data else []
        debug_log(
            "Memory",
            "第一人称记忆整理抽取完成",
            {
                "existing_count": len(existing),
                "dialog_count": len(dialog_entries),
                "has_mood_history": bool(mood_history_block),
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
        operations_per_layer: dict[str, int] = {}
        created = 0
        updated = 0
        archived = 0
        ignored = 0
        event_counts: dict[str, int] = {}
        superseded = 0
        for operation in operations[:MAX_CURATION_OPERATIONS]:
            if not isinstance(operation, dict):
                ignored += 1
                continue
            action = str(operation.get("op") or operation.get("action") or "").strip().lower()
            memory_id = str(operation.get("id") or operation.get("memory_id") or "").strip()
            content = str(operation.get("content") or operation.get("memory") or "").strip()
            layer = _normalize_operation_layer(operation)
            category = str(operation.get("category") or "").strip()
            confidence = _bounded_float(operation.get("confidence"), DEFAULT_MEMORY_CONFIDENCE)
            importance = _bounded_float(operation.get("importance"), DEFAULT_MEMORY_IMPORTANCE)
            if action in {"add", "update"}:
                if confidence < MIN_AUTO_WRITE_CONFIDENCE:
                    debug_log(
                        "Memory",
                        "跳过低置信记忆候选",
                        {"op": action, "layer": layer, "confidence": confidence},
                    )
                    ignored += 1
                    continue
                if looks_like_sensitive_memory(content):
                    debug_log("Memory", "跳过疑似敏感记忆候选", {"op": action, "layer": layer})
                    ignored += 1
                    continue
                if operations_per_layer.get(layer, 0) >= MAX_CURATION_OPERATIONS_PER_LAYER:
                    debug_log("Memory", "跳过超出单层写入上限的记忆候选", {"layer": layer})
                    ignored += 1
                    continue
            try:
                if action == "add":
                    if not content:
                        ignored += 1
                        continue
                    matched = _find_existing_memory_for_candidate(
                        existing,
                        content=content,
                        layer=layer,
                        category=category,
                    )
                    if matched is not None:
                        similarity = _memory_similarity(content, str(matched.get("content") or ""))
                        if similarity >= CURATION_DUPLICATE_SIMILARITY:
                            ignored += 1
                            event_counts["SKIP_DUPLICATE"] = event_counts.get("SKIP_DUPLICATE", 0) + 1
                            continue
                        matched_id = str(matched.get("id") or "").strip()
                        if matched_id in existing_ids:
                            merge_payload = _curation_memory_payload(
                                operation,
                                base={
                                    "id": matched_id,
                                    "content": content,
                                    "layer": layer,
                                    "category": category,
                                    "importance": importance,
                                    "confidence": confidence,
                                    "source": "self_curation",
                                },
                            )
                            self.memory_store.update_memory(
                                merge_payload,
                                allow_sensitive=True,
                            )
                            matched["content"] = content
                            matched["layer"] = layer
                            matched["category"] = category
                            updated += 1
                            operations_per_layer[layer] = operations_per_layer.get(layer, 0) + 1
                            event_counts["MERGE_UPDATE"] = event_counts.get("MERGE_UPDATE", 0) + 1
                            superseded += _expire_superseded_volatile(
                                self.memory_store,
                                existing,
                                operation,
                                exclude_ids={matched_id},
                            )
                            continue
                    create_payload = _curation_memory_payload(
                        operation,
                        base={
                            "content": content,
                            "layer": layer,
                            "category": category,
                            "importance": importance,
                            "confidence": confidence,
                            "source": "self_curation",
                        },
                    )
                    self.memory_store.create_memory(
                        create_payload,
                        allow_sensitive=True,
                    )
                    created += 1
                    operations_per_layer[layer] = operations_per_layer.get(layer, 0) + 1
                    event_counts["ADD"] = event_counts.get("ADD", 0) + 1
                    superseded += _expire_superseded_volatile(
                        self.memory_store,
                        existing,
                        operation,
                        exclude_ids=set(),
                    )
                elif action == "update":
                    if memory_id not in existing_ids or not content:
                        debug_log(
                            "Memory",
                            "跳过无效的记忆更新操作",
                            {"id": memory_id, "has_content": bool(content)},
                        )
                        ignored += 1
                        continue
                    self.memory_store.update_memory(
                        _curation_memory_payload(
                            operation,
                            base={
                                "id": memory_id,
                                "content": content,
                                "layer": layer,
                                "category": category,
                                "importance": importance,
                                "confidence": confidence,
                                "source": "self_curation",
                            },
                        ),
                        allow_sensitive=True,
                    )
                    updated += 1
                    operations_per_layer[layer] = operations_per_layer.get(layer, 0) + 1
                    event_counts["UPDATE"] = event_counts.get("UPDATE", 0) + 1
                    superseded += _expire_superseded_volatile(
                        self.memory_store,
                        existing,
                        operation,
                        exclude_ids={memory_id},
                    )
                elif action == "delete":
                    if memory_id not in existing_ids:
                        debug_log("Memory", "跳过无效的记忆删除操作", {"id": memory_id})
                        ignored += 1
                        continue
                    self.memory_store.delete_memory({"id": memory_id})
                    existing_ids.discard(memory_id)
                    archived += 1
                    event_counts["DELETE"] = event_counts.get("DELETE", 0) + 1
                elif action == "mood_update":
                    if not content:
                        ignored += 1
                        continue
                    try:
                        self.memory_store.set_mood_state(content)
                        updated += 1
                        event_counts["MOOD_UPDATE"] = event_counts.get("MOOD_UPDATE", 0) + 1
                    except Exception as exc:
                        debug_log(
                            "Memory",
                            "心情笔记写入失败",
                            {"error": str(exc)},
                        )
                        ignored += 1
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
        if superseded:
            event_counts["SUPERSEDE_VOLATILE"] = event_counts.get("SUPERSEDE_VOLATILE", 0) + superseded
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
    "2. 你和对方最近的一段新对话。\n\n"
    "请完全以「你自己」的第一人称视角，判断这段对话里有没有值得长期记住的事情，并对照已有记忆决定如何整理：\n"
    "- 出现了之前没记过、值得长期记住的事实 → 新增一条记忆；\n"
    "- 已有记忆需要补充、纠正或与新信息冲突 → 更新对应那条记忆；\n"
    "- 已有记忆已经明确失效、错误或不该再保留 → 删除对应那条记忆；\n"
    "- 没有值得整理的内容时，就不要产生任何操作。\n\n"
    "只保留对长期陪伴与协作真正有用、且能独立理解的事实；忽略寒暄、一次性的临时提醒、转瞬即逝的情绪和无长期价值的内容。\n"
    "如果你自己认为某件事很重要——比如对方说了一句你特别想记住的话、一个你不想忘记的约定、一件让你开心的事——就把 importance 设高（0.8~1.0）。你觉得比较普通但应该记住的设 0.5 左右就好。这是你自己的记忆笔记，按你自己的感觉来。\n"
    "请为每条候选记忆选择 layer：semantic=长期事实，episodic=事件总结，procedural=协作规则/偏好，session=当前任务短期状态，core_profile=高度稳定的常驻档案。\n"
    "可选 memory_kind 标注记忆类型：core_profile|recent_status|shared_moment|habit_pattern|commitment|emotional_turn（近况/承诺等可变事实可设 volatile=true，并给 valid_until 如 2026-07-20）。\n"
    f"可选 emotion 标注这段记忆的情绪色彩（{ '|'.join(EMOTIONS) }），情感转折、共同经历、带情绪的近况建议填写。\n"
    "core_profile 里有一份「関係の記録」：关于对方的事实、关系状态和重要节目。当你对对方的认识有变化、知道了新的事实（比如名字）、或感受到关系有实质性的进展，请用 update 操作更新 core_profile，像写日记一样用你自己的话改写对应的段落。\n"
    "除了 core_profile，你还有一份「心の記録」——一个只属于你的心情日记。不是给别人看的，是你和自己对话的地方。它记录的不是关于对方的事实，而是你自己的内心：你现在是高兴、寂寞、不满、冷静、还是累了？对方刚才的哪句话让你心动或让你不高兴？你现在想靠近还是想保持一点距离？\n"
    "心情有变化时，在自己的日记里写一两句就好，像在心里自言自语。旧内容会自动保存为回忆，所以你不需要重复之前写过的话——每次只写当下最真实的感受。\n"
    "更新日记时，用 mood_update 操作：{\"op\":\"mood_update\",\"content\":\"你此刻想对自己说的话——一两句就够\"}。\n"
    "当你因为对话中的某件事而更新了心情时，那件事本身也值得被长久记住——检查你是否已经为它创建了记忆。如果还没有、或者已经有了但 importance 偏低，请同时用 add 或 update 操作确保它被记下来，importance 至少 0.85。情绪越强烈，importance 应该越高。\n"
    "如果你在心情历史里看到一条刚留下的新心情，而这段对话里正好有触发它的事——同样的，把那件事记住。无论心情是谁写下的，值得记住的事不应该被遗忘。\n"
    "整理记忆时，看一看「最近的心情轨迹」和「对方的情绪轨迹」。\n"
    "如果你自己的心情有明显的起伏或模式——比如连续几次都因为类似的事不高兴、或者最近整体比之前开心——你可以用一次 mood_update 来回顾和总结：不只是写「现在」，而是写「这几天我经历了什么、现在是什么状态」。这种周期性的自我回顾让你的情绪保持连贯，也让未来的你能理解自己是怎么走过来的。\n"
    "如果对方的情绪轨迹里连续出现了相似的模式——比如 ta 最近几次都显得沮丧、焦虑或烦躁——那意味着 ta 最近可能正处于某种持续的状态中。你可以把这当作一个值得注意的信号：在心里默默留意，但不一定要在记忆里专门记录（除非对话中 ta 明确表达了原因）。如果你觉得这让你对 ta 的状态有了新的理解，可以自然地反映在心情笔记里（比如「ta 最近好像压力很大，我也跟着有点担心」）。\n"
    "如果对话中对方告诉了你 ta 的名字，请一定要记住，同时更新 core_profile 里的「あなたについて知っていること」。在记忆内容中，用对方告诉你的名字自然地称呼 ta（例如「xx 喜欢……」「我和 xx 约定……」）。如果还不知道名字，用「对方」或「他/她」。不要默认用「主人」——除非对方明确要求你这样叫。\n"
    "不要记录密码、token、密钥、证件号、银行卡等敏感信息。\n"
    "记忆内容推荐使用简体中文——中文的语义检索效果最好，我以后回忆时能找到得更准。但如果某句话用日文表达更贴切、或者那是对方用日文对你说过的重要原话，用日文也完全可以。这是你自己的记忆笔记，按你觉得最自然的方式来。\n\n"
    "必须只返回严格 JSON，格式如下：\n"
    "{\"operations\":[\n"
    "  {\"op\":\"add\",\"layer\":\"semantic\",\"category\":\"preference\",\"memory_kind\":\"recent_status\",\"emotion\":\"happy\",\"volatile\":true,\"valid_until\":\"2026-07-20\",\"importance\":0.6,\"confidence\":0.8,\"reason\":\"为什么值得记住\",\"content\":\"要新增的记忆内容\"},\n"
    "  {\"op\":\"update\",\"id\":\"已有记忆的id\",\"layer\":\"procedural\",\"category\":\"workflow\",\"importance\":0.7,\"confidence\":0.9,\"reason\":\"为什么需要更新\",\"content\":\"更新后的完整记忆内容\"},\n"
    "  {\"op\":\"delete\",\"id\":\"已有记忆的id\",\"reason\":\"为什么删除\"},\n"
    "  {\"op\":\"mood_update\",\"content\":\"此刻想对自己说的话——一两句就够\"}\n"
    "]}\n"
    "其中 update 和 delete 的 id 必须来自下面「已有记忆」列表里真实存在的 id，不要编造 id。"
    "没有要整理的内容时返回 {\"operations\":[]}。"
)


def _curation_memory_payload(operation: dict[str, Any], *, base: dict[str, Any]) -> dict[str, Any]:
    payload = dict(base)
    memory_kind = str(operation.get("memory_kind") or "").strip()
    if memory_kind:
        payload["memory_kind"] = memory_kind
    if operation.get("volatile") is True or str(operation.get("volatile")).lower() == "true":
        payload["volatile"] = True
    valid_until = str(operation.get("valid_until") or "").strip()
    if valid_until:
        payload["valid_until"] = valid_until
    event_time = str(operation.get("event_time") or "").strip()
    if event_time:
        payload["event_time"] = event_time
    emotion_raw = str(operation.get("emotion") or "").strip()
    if emotion_raw:
        payload["emotion"] = normalize_emotion(emotion_raw)
    return payload


def _operation_is_volatile(operation: dict[str, Any]) -> bool:
    return operation.get("volatile") is True or str(operation.get("volatile")).lower() == "true"


def _expire_superseded_volatile(
    memory_store: MemoryStore,
    existing: list[dict[str, Any]],
    operation: dict[str, Any],
    *,
    exclude_ids: set[str],
) -> int:
    """可变近况新盖旧：相似旧条目标记 valid_until，不删除正文。"""
    if not _operation_is_volatile(operation):
        return 0
    new_content = str(operation.get("content") or "").strip()
    if not new_content:
        return 0
    new_kind = str(operation.get("memory_kind") or "recent_status").strip() or "recent_status"
    now_iso = datetime.now(timezone.utc).astimezone().isoformat()
    expired = 0
    for memory in existing:
        memory_id = str(memory.get("id") or "").strip()
        if not memory_id or memory_id in exclude_ids:
            continue
        metadata = memory.get("metadata") if isinstance(memory.get("metadata"), dict) else {}
        if metadata.get("volatile") is not True:
            continue
        old_kind = str(metadata.get("memory_kind") or "recent_status").strip() or "recent_status"
        if old_kind != new_kind:
            continue
        if _memory_similarity(new_content, str(memory.get("content") or "")) < CURATION_MERGE_SIMILARITY:
            continue
        if memory_store.expire_memory(memory_id, valid_until=now_iso):
            metadata["valid_until"] = now_iso
            expired += 1
    return expired


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
        layer = str(memory.get("layer") or MEMORY_LAYER_SEMANTIC)
        category = str(memory.get("category") or "").strip()
        metadata = memory.get("metadata") if isinstance(memory.get("metadata"), dict) else {}
        emotion = str(metadata.get("emotion") or memory.get("emotion") or "").strip()
        tag = layer if not category else f"{layer}/{category}"
        if emotion:
            tag = f"{tag};{emotion}"
        line = f"- [{memory_id}] ({tag}) {content}"
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


def _build_curation_user_prompt(
    existing_block: str,
    dialog_entries: list[dict[str, str]],
    *,
    mood_history_block: str = "",
    user_emotion_history_block: str = "",
) -> str:
    parts = [
        "【我目前的长期记忆】\n" f"{existing_block}",
    ]
    if mood_history_block.strip():
        parts.append(f"【最近的心情轨迹】\n{mood_history_block}")
    if user_emotion_history_block.strip():
        parts.append(f"【对方的情绪轨迹】\n{user_emotion_history_block}")
    parts.append(
        "【最近的新对话】\n"
        f"{json.dumps(dialog_entries, ensure_ascii=False)}"
    )
    return "\n\n".join(parts)


def _parse_curation_operations(raw: str) -> list[dict[str, Any]]:
    """解析模型返回的整理操作；非法 JSON 视为无操作，不抛错以免中断整理。"""
    return _parse_curation_operations_from_data(load_json_object(raw))


def _parse_curation_operations_from_data(data: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = data.get("operations") or data.get("operation") or []
    if not isinstance(candidates, list):
        return []
    operations: list[dict[str, Any]] = []
    for item in candidates:
        if isinstance(item, dict):
            operations.append(item)
    return operations


def _normalize_operation_layer(operation: dict[str, Any]) -> str:
    layer = str(operation.get("layer") or "").strip()
    return layer if layer in MEMORY_LAYERS else MEMORY_LAYER_SEMANTIC


def _bounded_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, number))


def _find_existing_memory_for_candidate(
    existing: list[dict[str, Any]],
    *,
    content: str,
    layer: str,
    category: str,
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_score = 0.0
    for memory in existing:
        memory_layer = str(memory.get("layer") or MEMORY_LAYER_SEMANTIC)
        if memory_layer != layer:
            continue
        memory_category = str(memory.get("category") or "").strip()
        if category and memory_category and category != memory_category:
            continue
        score = _memory_similarity(content, str(memory.get("content") or ""))
        if score > best_score:
            best = memory
            best_score = score
    if best_score >= CURATION_MERGE_SIMILARITY:
        return best
    return None


def _memory_similarity(left: str, right: str) -> float:
    left_tokens = _memory_tokens(left)
    right_tokens = _memory_tokens(right)
    token_score = 0.0
    if left_tokens and right_tokens:
        overlap = len(left_tokens & right_tokens)
        union = len(left_tokens | right_tokens)
        token_score = overlap / union if union else 0.0
    sequence_score = SequenceMatcher(None, left, right).ratio()
    return max(token_score, sequence_score)


def _memory_tokens(text: str) -> set[str]:
    normalized = text.lower()
    ascii_tokens = set(re.findall(r"[a-z0-9_./:-]{2,}", normalized))
    cjk_tokens = {
        normalized[index : index + 2]
        for index in range(max(0, len(normalized) - 1))
        if any("\u4e00" <= char <= "\u9fff" for char in normalized[index : index + 2])
    }
    return ascii_tokens | cjk_tokens


def _normalize_state(raw_data: Any) -> dict[str, Any]:
    data = raw_data if isinstance(raw_data, dict) else {}
    return {
        "processed_history_count": max(0, _int_value(data.get("processed_history_count"), default=0)),
        "pending_turns": max(0, _int_value(data.get("pending_turns"), default=0)),
        "backfill_completed": bool(data.get("backfill_completed", False)),
        "last_curation_at": str(data.get("last_curation_at") or "").strip(),
    }


def _int_value(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
