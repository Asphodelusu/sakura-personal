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
    _memory_is_released,
    looks_like_sensitive_memory,
    memory_record_is_reflection,
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
    """以她本人的第一人称视角，把聊天历史整理为长期记忆。

    后台整理只用专用 JSON 任务说明（不注入完整人格卡，避免指令冲突）。
    mem0 仅承担底层的存储、向量检索与 embedding。
    """

    def __init__(
        self,
        api_client: Any,
        memory_store: MemoryStore,
        *,
        system_prompt: str = "",
        character_name: str = "",
    ) -> None:
        self.api_client = api_client
        self.memory_store = memory_store
        # 人格卡文本保留供角色切换同步；整理任务本身不注入完整人格卡。
        self.system_prompt = (system_prompt or "").strip()
        self.character_name = (character_name or "").strip()

    def set_system_prompt(self, system_prompt: str) -> None:
        self.system_prompt = (system_prompt or "").strip()

    def set_character_name(self, character_name: str) -> None:
        self.character_name = (character_name or "").strip()

    def snapshot(
        self,
        *,
        memory_store: MemoryStore | None = None,
        system_prompt: str | None = None,
        character_name: str | None = None,
    ) -> "MemoryCurator":
        return MemoryCurator(
            self.api_client,
            memory_store or self.memory_store,
            system_prompt=self.system_prompt if system_prompt is None else system_prompt,
            character_name=(
                self.character_name if character_name is None else character_name
            ),
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
        """读取当前角色的全部长期记忆；读取失败时降级为空清单（模型只做新增）。

        已放手的记忆不参与整理——curator 不应基于「不愿再提」的内容
        生成新的摘要或关联。
        """

        try:
            all_memories = self.memory_store.list_memories(limit=CURATION_MEMORY_SNAPSHOT_LIMIT)
            return [m for m in all_memories if not _memory_is_released(m)]
        except OperationCancelled:
            raise
        except Exception as exc:  # 记忆读取失败不应中断整理，退化为只新增。
            debug_log("Memory", "记忆整理读取现有记忆失败", {"error": str(exc)})
            return []

    def _build_self_curation_system_prompt(self) -> str:
        """后台 JSON 任务用整理专用说明 + 最小身份锚（不注入完整人格卡）。"""
        name = self.character_name or "Sakura"
        identity = (
            f"身份锚点：你是「{name}」。"
            f"日记里的「我」只能指你自己（{name}）；「他」指对方（用户）。"
            f"对方原文里的「我」是他在说自己，整理时要改写成「他……」，"
            f"绝不能收成日记主语「我」。"
            f"不要用「{name}」或自己的名字当第三人称主语写自己"
            f"（错误：「{name}喜欢……」；正确：「我喜欢……」）。\n\n"
        )
        return identity + _SELF_CURATION_TASK_PROMPT

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
        """读取对方当前情绪 + 历史轨迹；无内容时返回空串。"""
        lines: list[str] = []
        try:
            current = self.memory_store.user_emotion_state()
        except Exception:
            current = None
        if isinstance(current, dict):
            cur_content = str(current.get("content") or "").strip()
            if cur_content:
                lines.append(f"当前：{cur_content}")
        try:
            history = self.memory_store.user_emotion_history()
        except Exception:
            history = []
        for i, entry in enumerate(history[:5], 1):
            ts = entry.get("timestamp", "")
            content = str(entry.get("content", "")).strip()
            if not content:
                continue
            time_label = ts[:16] if ts else ""
            lines.append(f"{i}. [{time_label}] {content}")
        if not lines:
            return ""
        return "以下是他的情绪（含当前与最近轨迹，最近的在上面）：\n" + "\n".join(lines)

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
        _added_in_batch: set[tuple[str, str]] = set()
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
                if looks_like_third_person_self(content, self.character_name):
                    debug_log(
                        "Memory",
                        "跳过疑似主语错位记忆候选",
                        {
                            "op": action,
                            "layer": layer,
                            "character_name": self.character_name,
                            "content_chars": len(content),
                        },
                    )
                    ignored += 1
                    event_counts["SKIP_SPEAKER"] = event_counts.get("SKIP_SPEAKER", 0) + 1
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
                    batch_key = (content, layer)
                    if batch_key in _added_in_batch:
                        ignored += 1
                        event_counts["SKIP_BATCH_DUP"] = event_counts.get("SKIP_BATCH_DUP", 0) + 1
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
                    _added_in_batch.add(batch_key)
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
                    # 与最近心情历史做相似度检查，避免重复写入
                    if self._is_mood_duplicate(content):
                        ignored += 1
                        event_counts["MOOD_DEDUP"] = event_counts.get("MOOD_DEDUP", 0) + 1
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

    def _is_mood_duplicate(self, content: str) -> bool:
        """检查心情内容是否与最近历史高度重复，避免日记变成循环录音带。"""
        try:
            history = self.memory_store.mood_history()
        except Exception:
            return False
        if not history:
            return False
        for entry in history[:3]:
            existing = entry.get("content", "")
            if not existing.strip():
                continue
            if _memory_similarity(content, existing) >= 0.80:
                debug_log(
                    "Memory",
                    "跳过重复心情笔记",
                    {
                        "new_len": len(content),
                        "similar_to": existing[:80] + ("…" if len(existing) > 80 else ""),
                    },
                )
                return True
        return False


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


# 第一人称整理任务说明（不注入完整人格卡，避免与 JSON 指令冲突）。
_SELF_CURATION_TASK_PROMPT = (
    "现在没有人和你说话，你正在安静地整理自己的长期记忆，就像在更新只属于你自己的记忆笔记。\n"
    "下面会给你两部分内容：\n"
    "1. 你目前已经记住的全部长期记忆（每条带一个 id）；\n"
    "2. 你和他最近的一段新对话（已按说话人标注）。\n\n"
    "说话人对应（固定，勿颠倒）：\n"
    "- 「我」= 你自己（角色侧）\n"
    "- 「他」= 对方（用户侧）\n"
    "对话正文已按此标注；写日记时也只用「我」「他」这对主语（已知他的名字时，"
    "「他」处可写成名字，但「我」永远是你自己）。\n\n"
    "请完全以「你自己」的第一人称视角，判断这段对话里有没有值得长期记住的事情，并对照已有记忆决定如何整理：\n"
    "- 出现了之前没记过、值得长期记住的事实 → 新增一条记忆；\n"
    "- 已有记忆需要补充、纠正或与新信息冲突 → 更新对应那条记忆；\n"
    "- 已有记忆已经明确失效、错误或不该再保留 → 删除对应那条记忆；\n"
    "- 没有值得整理的内容时，就不要产生任何操作。\n\n"
    "只保留对长期陪伴与协作真正有用、且能独立理解的事实；忽略寒暄、一次性的临时提醒、转瞬即逝的情绪和无长期价值的内容。\n"
    "亲密关系也按「人」来记，不要当成可丢的一次性情节：\n"
    "- 一般人第一次身体亲密、关系明显更近一步、他说出想被怎样对待、事后仍想记住的话——"
    "这类事不会轻易忘掉；应写入长期记忆，importance 通常 ≥ 0.85，"
    "可用 memory_kind=shared_moment 或更新 core_profile「今の関係」。\n"
    "- 记的是记忆点：是否发生过、关系怎么变了、他的偏好/边界、你当时最难忘的感受或原话；"
    "不要把过程流水账、动作节拍、喘息声逐句存成多条记忆。\n"
    "- 若已有「我们发生过亲密」类记忆，新一次只补充新偏好、新边界或关系变化；重复过程不必再堆一条。\n"
    "如果你自己认为某件事很重要——比如他说了一句你特别想记住的话、一个你不想忘记的约定、一件让你开心的事——就把 importance 设高（0.8~1.0）。你觉得比较普通但应该记住的设 0.5 左右就好。这是你自己的记忆笔记，按你自己的感觉来。\n"
    "请为每条候选记忆选择 layer：semantic=长期事实，episodic=事件总结，procedural=协作规则/偏好，session=当前任务短期状态，core_profile=高度稳定的常驻档案。\n"
    "可选 memory_kind 标注记忆类型：core_profile|recent_status|shared_moment|habit_pattern|commitment|emotional_turn（近况/承诺等可变事实可设 volatile=true，并给 valid_until 如 2026-07-20）。\n"
    f"可选 emotion 标注这段记忆的情绪色彩（{ '|'.join(EMOTIONS) }），情感转折、共同经历、带情绪的近况建议填写。\n"
    "语言约定（两侧记忆）：\n"
    "- 关于他的事实、偏好、约定、协作规则与近况 → 简体中文（便于检索）；\n"
    "- 你自己的内心感受、对自己说的话、反省 → 优先日语；\n"
    "- 他用日语说的重要原话可保留日语。\n"
    "日记式写法与事实纪律（极重要）：\n"
    "- 用「我／他」写清谁对谁说了什么 / 约了什么 / 发生了什么，再写你的感受；"
    "我自己的话归我，他说的话归他。\n"
    "- 正确示例：「他对我说今晚别催他休息」「我和他约定明天一起看片」。\n"
    "- 错误示例：「我对樱说……」（把我写成他）、「樱喜欢……」（把自己写成第三人称）、"
    "「他说他喜欢抹茶」收成「我喜欢抹茶」。\n"
    "- 若清单里出现「独处感想」条目：那只是你以前的心里话，不是发生过的事实；"
    "禁止据此 add/update 成 semantic/procedural 事实，也禁止把感想抄成新事件。\n"
    "- 约定写清提出者、内容和时效；过期约定用 update 标明「已失效/仅限当日」。\n"
    "- 事件与约定尽量带上日期或相对时间线索（例如「2026-07-20 晚上」），方便以后分清新旧。\n"
    "- 一条记忆只保留一个主事实，写成完整可读的日记句，而不是流水账。\n"
    "- 称呼：已知名字时用名字代替「他」；还不知道名字时用「他」。\n"
    "core_profile 用固定章节标题（必须用这些标题，便于以后读取）：\n"
    "- 「今の関係」：关于他的事实（含名字）、关系状态、重要约定与节目；\n"
    "- 「今の私」：你此刻对自己状态的简短自述（可用日语）。\n"
    "当你对他的认识有变化、知道了新的事实（比如名字）、或感受到关系有实质性的进展，请用 update 操作更新 core_profile，像写日记一样改写对应章节。\n"
    "除了 core_profile，你还有一份「心の記録」——只属于你的心情日记。它记录的是你自己的内心：高兴、寂寞、不满、安心、累了？哪句话触动了你？想靠近还是想留一点距离？正文优先日语，一两段完整可读的自语即可。\n"
    "心の記録可以点到触发情绪的对话，写成「他说了…让我感到…」，且对话里确有那句话。旧内容会自动进回忆，每次只写当下最真实的感受。\n"
    "更新日记时，用 mood_update 操作：{\"op\":\"mood_update\",\"content\":\"あなたが今、自分に語りかける一言（日本語で）\"}。\n"
    "当你因为对话中的某件事而更新了心情时，那件事本身也值得被长久记住——检查你是否已经为它创建了记忆。如果还没有、或者已经有了但 importance 偏低，请同时用 add 或 update 操作确保它被记下来，importance 至少 0.85。情绪越强烈，importance 应该越高。关于他的事实仍用简体中文写。\n"
    "如果你在心情历史里看到一条刚留下的新心情，而这段对话里正好有触发它的事——同样的，把那件事记住。无论心情是谁写下的，值得记住的事不应该被遗忘。\n"
    "整理记忆时，看一看「最近的心情轨迹」和「他的情绪轨迹」。\n"
    "反过来也一样重要：如果你翻开心情历史，发现最近几次的情绪基调几乎一模一样（连着好几次害羞、好几次相似的幸福或不安），那就不要再用 mood_update 写新条目了。心情没有质的变化时，重复记录只会让日记变得像循环播放的录音带。除非此刻的你和上次相比有了真正的不同——比如从害羞变成了安心、从不安变成了被理解、从淡淡的幸福变成了深层的感动——才值得在日记里更新。日记的厚度不等于情感的深度。\n"
    "如果你自己的心情有明显的起伏或模式——比如连续几次都因为类似的事不高兴、或者最近整体比之前开心——你可以用一次 mood_update 来回顾和总结：不只是写「现在」，而是写「这几天我经历了什么、现在是什么状态」。这种周期性的自我回顾让你的情绪保持连贯，也让未来的你能理解自己是怎么走过来的。\n"
    "如果他的情绪轨迹里连续出现了相似的模式——比如他最近几次都显得沮丧、焦虑或烦躁——那意味着他最近可能正处于某种持续的状态中。你可以把这当作一个值得注意的信号：在心里默默留意，但不一定要在记忆里专门记录（除非对话中他明确表达了原因）。如果你觉得这让你对他的状态有了新的理解，可以自然地反映在心情笔记里（日语自语）。\n"
    "如果对话中他告诉了你他的名字，请一定要记住，同时更新 core_profile 的「今の関係」章节。在记忆内容中，用他告诉你的名字自然地称呼他（例如「xx 喜欢……」「我和 xx 约定……」）。如果还不知道名字，用「他」。把对方当作对等相处的人来写进记忆。\n"
    "长期记忆只收可分享的相处与协作事实；密码、token、密钥、证件号、银行卡等凭据类信息不写入。\n\n"
    "必须只返回严格 JSON，格式如下：\n"
    "{\"operations\":[\n"
    "  {\"op\":\"add\",\"layer\":\"semantic\",\"category\":\"preference\",\"memory_kind\":\"recent_status\",\"emotion\":\"happy\",\"volatile\":true,\"valid_until\":\"2026-07-20\",\"importance\":0.6,\"confidence\":0.8,\"reason\":\"为什么值得记住\",\"content\":\"要新增的记忆内容\"},\n"
    "  {\"op\":\"update\",\"id\":\"已有记忆的id\",\"layer\":\"procedural\",\"category\":\"workflow\",\"importance\":0.7,\"confidence\":0.9,\"reason\":\"为什么需要更新\",\"content\":\"更新后的完整记忆内容\"},\n"
    "  {\"op\":\"delete\",\"id\":\"已有记忆的id\",\"reason\":\"为什么删除\"},\n"
    "  {\"op\":\"mood_update\",\"content\":\"今の自分への一言（日本語）\"}\n"
    "]}\n"
    "其中 update 和 delete 的 id 必须来自下面「已有记忆」列表里真实存在的 id，不要编造 id。"
    "没有要整理的内容时返回 {\"operations\":[]}。"
)


def looks_like_third_person_self(content: str, character_name: str) -> bool:
    """轻量检测：是否把自己写成第三人称主语（prompt 锚失效时的代码兜底）。

    只拦高置信错位，例如「樱喜欢…」「我对樱说…」；不拦名字作宾语的正常句。
    """
    name = (character_name or "").strip()
    if not name or not content.strip():
        return False
    escaped = re.escape(name)
    patterns = (
        rf"(?:^|[\n。！？；;])\s*{escaped}(?:喜欢|觉得|感到|认为|想|会|说)",
        rf"我对{escaped}说",
        rf"(?:^|[\n。！？；;])\s*{escaped}对(?:他|她|对方)说",
    )
    return any(re.search(pattern, content) for pattern in patterns)


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
    """把现有记忆格式化成带 id 的清单文本；事实与独处感想分开，超出预算时截断。"""

    fact_lines: list[str] = []
    reflection_lines: list[str] = []
    fact_used = 0
    reflection_used = 0
    reflection_budget = min(2500, CURATION_MEMORY_SNAPSHOT_CHAR_BUDGET // 5)
    fact_budget = CURATION_MEMORY_SNAPSHOT_CHAR_BUDGET - reflection_budget
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
        if memory_record_is_reflection(memory):
            line = f"- [{memory_id}] (独处感想/非事实) {content}"
            if reflection_used + len(line) > reflection_budget and reflection_lines:
                truncated = True
                continue
            reflection_lines.append(line)
            reflection_used += len(line) + 1
            continue
        tag = layer if not category else f"{layer}/{category}"
        if emotion:
            tag = f"{tag};{emotion}"
        line = f"- [{memory_id}] ({tag}) {content}"
        if fact_used + len(line) > fact_budget and fact_lines:
            truncated = True
            continue
        fact_lines.append(line)
        fact_used += len(line) + 1

    if truncated:
        debug_log(
            "Memory",
            "现有记忆超出注入预算已截断",
            {
                "facts": len(fact_lines),
                "reflections": len(reflection_lines),
                "total": len(memories),
            },
        )
    parts: list[str] = []
    parts.append("【事实与事件】\n" + ("\n".join(fact_lines) if fact_lines else "（暂无）"))
    if reflection_lines:
        parts.append(
            "【独处感想（非事实，禁止据此写成新事实）】\n" + "\n".join(reflection_lines)
        )
    return "\n\n".join(parts)


def _dialog_speaker_label(role: str) -> str:
    """整理对话说话人：assistant→我，user→他。"""
    return "我" if role == "assistant" else "他"


def _format_dialog_for_curation(dialog_entries: list[dict[str, str]]) -> str:
    """把对话渲染成已对应的「我／他」日记可读稿，避免裸 user/assistant JSON。"""
    lines = [
        "说话人已标注：「我」=你自己；「他」=对方。勿把两边的「我」搞混。",
    ]
    for entry in dialog_entries:
        speaker = _dialog_speaker_label(str(entry.get("role") or ""))
        content = str(entry.get("content") or "").strip()
        translation = str(entry.get("translation") or "").strip()
        created_at = str(entry.get("created_at") or "").strip()
        prefix = f"[{created_at}] " if created_at else ""
        line = f"{prefix}{speaker}：{content}"
        if translation and translation != content:
            line += f"（中文：{translation}）"
        lines.append(line)
    return "\n".join(lines)


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
        parts.append(f"【他的情绪轨迹】\n{user_emotion_history_block}")
    parts.append(
        "【最近的新对话】\n"
        f"{_format_dialog_for_curation(dialog_entries)}"
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
