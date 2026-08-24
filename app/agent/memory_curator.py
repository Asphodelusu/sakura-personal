from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from app.backchannel.models import EMOTIONS
from app.agent.memory import (
    DEFAULT_MEMORY_CONFIDENCE,
    DEFAULT_MEMORY_IMPORTANCE,
    MEMORY_LAYER_CORE_PROFILE,
    MEMORY_LAYER_SEMANTIC,
    MEMORY_LAYERS,
    MemoryStore,
    _memory_is_released,
    collect_commitments_for_expiry_review,
    commitment_event_time,
    looks_like_sensitive_memory,
    mark_commitments_expiry_reviewed,
    memory_kind_of,
    memory_record_is_reflection,
    sweep_stale_commitments,
)
from app.agent.memory_evidence import (
    build_dialog_corpus,
    operation_evidence,
    validate_memory_write_grounding,
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
DEFAULT_AUTO_MEMORY_LIGHT_IDLE_MINUTES = 3
DEFAULT_AUTO_MEMORY_LIGHT_COOLDOWN_MINUTES = 10
MAX_CURATION_CHUNK_MESSAGES = 32
MAX_CURATION_CHUNK_CHARS = 12000
# 相邻消息间隔超过此时长，视为新会话段（主题切分优先于字数硬切）
CURATION_SESSION_GAP_SECONDS = 20 * 60
# 整理时一次性注入给模型的现有记忆条数上限，远大于日常摘要，便于全量对照去重纠错。
CURATION_MEMORY_SNAPSHOT_LIMIT = 500
# 现有记忆清单注入的字符预算，超出后截断以保护 token 开销。
CURATION_MEMORY_SNAPSHOT_CHAR_BUDGET = 20000
# light_idle：详细正文条数 + 索引条数 + 字符预算（远小于全量快照）
LIGHT_CURATION_DETAIL_LIMIT = 36
LIGHT_CURATION_INDEX_LIMIT = 100
LIGHT_CURATION_SNAPSHOT_CHAR_BUDGET = 6000
LIGHT_CURATION_INDEX_TITLE_CHARS = 48
# 单次整理允许写回的操作数量上限，避免异常输出放大写入。
MAX_CURATION_OPERATIONS = 50
MIN_AUTO_WRITE_CONFIDENCE = 0.55
CURATION_DUPLICATE_SIMILARITY = 0.92
CURATION_MERGE_SIMILARITY = 0.78
# 本批已写入条目的近义阈值（略宽于库内精确去重，挡同场换皮再 add）
CURATION_BATCH_NEAR_DUP_SIMILARITY = 0.86
MAX_CURATION_OPERATIONS_PER_LAYER = 20
# 单次整理（整次 curate）最多写一条心情
MAX_MOOD_UPDATES_PER_CURATION = 1
# 过短才直接拒；稍长的应酬靠模式匹配
MIN_MEMORY_CONTENT_CHARS = 4
# recent_status 未给 valid_until 时的默认时效（天）
DEFAULT_RECENT_STATUS_TTL_DAYS = 14
MAX_EXPIRY_REVIEW_COMMITMENTS = 5


@dataclass(frozen=True)
class MemoryCurationSettings:
    enabled: bool = True
    backfill_limit: int = DEFAULT_AUTO_MEMORY_BACKFILL_LIMIT
    idle_minutes: int = DEFAULT_AUTO_MEMORY_IDLE_MINUTES
    min_turns: int = DEFAULT_AUTO_MEMORY_MIN_TURNS
    cooldown_minutes: int = DEFAULT_AUTO_MEMORY_COOLDOWN_MINUTES
    long_idle_minutes: int = DEFAULT_AUTO_MEMORY_LONG_IDLE_MINUTES
    catch_up_turns: int = DEFAULT_AUTO_MEMORY_CATCH_UP_TURNS
    # 轻量档：短静默停顿即整理（独立较短冷却）
    light_idle_minutes: int = DEFAULT_AUTO_MEMORY_LIGHT_IDLE_MINUTES
    light_cooldown_minutes: int = DEFAULT_AUTO_MEMORY_LIGHT_COOLDOWN_MINUTES
    # 旧版「每 N 轮」字段，仅用于 YAML 迁移为 catch_up_turns。
    trigger_turns: int = DEFAULT_AUTO_MEMORY_TRIGGER_TURNS

    def normalized(self) -> MemoryCurationSettings:
        idle_minutes = max(3, min(120, int(self.idle_minutes)))
        min_turns = max(1, min(20, int(self.min_turns)))
        cooldown_minutes = max(5, min(240, int(self.cooldown_minutes)))
        long_idle_minutes = max(idle_minutes, min(240, int(self.long_idle_minutes)))
        catch_up_turns = max(min_turns, min(50, int(self.catch_up_turns)))
        backfill_limit = max(1, min(500, int(self.backfill_limit)))
        # 轻量静默必须严格小于深度静默，避免两档塌缩成同一门槛
        light_idle_minutes = max(1, min(idle_minutes - 1, int(self.light_idle_minutes)))
        light_cooldown_minutes = max(
            3,
            min(cooldown_minutes, int(self.light_cooldown_minutes)),
        )
        return MemoryCurationSettings(
            enabled=bool(self.enabled),
            backfill_limit=backfill_limit,
            idle_minutes=idle_minutes,
            min_turns=min_turns,
            cooldown_minutes=cooldown_minutes,
            long_idle_minutes=long_idle_minutes,
            catch_up_turns=catch_up_turns,
            light_idle_minutes=light_idle_minutes,
            light_cooldown_minutes=light_cooldown_minutes,
            trigger_turns=int(self.trigger_turns),
        )


def resolve_idle_curation_trigger(
    settings: MemoryCurationSettings,
    *,
    silence_seconds: float,
    pending_turns: int,
    seconds_since_last_curation: float | None,
    has_unprocessed_entries: bool,
    session_boundary: bool = False,
) -> str | None:
    """判定自动整理触发档位。

    返回 trigger 名，不触发则 None：
    - catch_up：积压轮数兜底（跳过静默与冷却）
    - session_boundary：跨会话补整理（跳过静默，受深度冷却约束）
    - idle / long_idle：深度静默档（受深度冷却约束）
    - light_idle：停顿轻量档（受轻量冷却约束）
    """
    normalized = settings.normalized()
    if not normalized.enabled:
        return None
    if not has_unprocessed_entries:
        return None
    if pending_turns < 1 and not session_boundary:
        return None

    catch_up = pending_turns >= normalized.catch_up_turns
    turns_ok = pending_turns >= normalized.min_turns
    deep_silence = silence_seconds + 1e-6 >= normalized.idle_minutes * 60
    light_silence = silence_seconds + 1e-6 >= normalized.light_idle_minutes * 60
    long_idle_ok = silence_seconds + 1e-6 >= normalized.long_idle_minutes * 60

    def _cooldown_ok(minutes: int) -> bool:
        if seconds_since_last_curation is None:
            return True
        return seconds_since_last_curation + 1e-6 >= minutes * 60

    # 1) 追赶：活跃用户永不触发的兜底
    if catch_up:
        return "catch_up"

    # 2) 会话边界：启动/跨会话补整理
    if session_boundary:
        if _cooldown_ok(normalized.cooldown_minutes):
            return "session_boundary"
        return None

    # 3) 深度静默档
    if deep_silence and (turns_ok or long_idle_ok):
        if _cooldown_ok(normalized.cooldown_minutes):
            if long_idle_ok and not turns_ok:
                return "long_idle"
            return "idle"
        # 深度冷却未满时，允许落入轻量档（若轻量条件也满足）

    # 4) 轻量停顿档：短静默 + 最少轮数 + 轻量冷却
    if light_silence and turns_ok and _cooldown_ok(normalized.light_cooldown_minutes):
        return "light_idle"

    return None


def evaluate_idle_curation_trigger(
    settings: MemoryCurationSettings,
    *,
    silence_seconds: float,
    pending_turns: int,
    seconds_since_last_curation: float | None,
    has_unprocessed_entries: bool,
    session_boundary: bool = False,
) -> bool:
    """混合静默触发（bool 包装；细节见 resolve_idle_curation_trigger）。"""
    return (
        resolve_idle_curation_trigger(
            settings,
            silence_seconds=silence_seconds,
            pending_turns=pending_turns,
            seconds_since_last_curation=seconds_since_last_curation,
            has_unprocessed_entries=has_unprocessed_entries,
            session_boundary=session_boundary,
        )
        is not None
    )


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

    def processed_history_count(self) -> int:
        return int(self.snapshot()["processed_history_count"])

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

    def has_unprocessed_in_store(self, store: Any) -> bool:
        """仅 COUNT，供 60s 轮询判断，避免全量 load。"""
        count_fn = getattr(store, "count", None)
        if not callable(count_fn):
            return False
        try:
            total = int(count_fn())
        except Exception:  # noqa: BLE001
            return False
        processed = self.processed_history_count()
        if processed < 0 or processed > total:
            return total > 0
        return total > processed

    def load_unprocessed_from_store(self, store: Any) -> list[ChatHistoryEntry]:
        """按游标增量读取未整理历史。"""
        count_fn = getattr(store, "count", None)
        slice_fn = getattr(store, "load_slice", None)
        if not callable(count_fn) or not callable(slice_fn):
            load_fn = getattr(store, "load", None)
            if not callable(load_fn):
                return []
            try:
                return self.unprocessed_entries(list(load_fn()))
            except Exception:  # noqa: BLE001
                return []
        try:
            total = int(count_fn())
        except Exception:  # noqa: BLE001
            return []
        processed = self.processed_history_count()
        if processed < 0 or processed > total:
            processed = 0
        if processed >= total:
            return []
        try:
            return list(slice_fn(processed))
        except Exception:  # noqa: BLE001
            return []

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
        snapshot_profile: str = "full",
    ) -> MemoryCurationResult:
        if self.api_client is None:
            # 缺少可用模型时无法进行第一人称整理，直接跳过而不报错。
            return MemoryCurationResult(processed_entries=len(entries))
        if not _entries_for_model(entries):
            return MemoryCurationResult(processed_entries=len(entries))

        profile = "light" if str(snapshot_profile or "").strip().lower() == "light" else "full"

        # 整理前先确定性关掉过期约定，并收集待一次性复盘的条目。
        just_expired: list[dict[str, Any]] = []
        try:
            sweep_stale_commitments(self.memory_store)
            just_expired = collect_commitments_for_expiry_review(
                self.memory_store,
                limit=MAX_EXPIRY_REVIEW_COMMITMENTS,
            )
        except Exception:  # noqa: BLE001
            just_expired = []

        created = 0
        updated = 0
        archived = 0
        ignored = 0
        event_counts: dict[str, int] = {}
        review_injected = False
        mood_budget = {"left": MAX_MOOD_UPDATES_PER_CURATION}
        # 同一次整理里前序 chunk 刚写下的正文，供后段对照，减少会话被二次切开时重复 add
        prior_chunk_writes: list[str] = []
        for chunk in _chunk_entries_for_curation(entries):
            check_cancelled(cancel_checker)
            dialog_entries = _entries_for_model(chunk)
            if not dialog_entries:
                continue
            # 每个 chunk 整理前重新拉取全量记忆；写回校验用全量 id，prompt 可按档位瘦身。
            existing = self._load_existing_memories()
            check_cancelled(cancel_checker)
            review_block = ""
            if just_expired and not review_injected:
                review_block = _format_just_expired_commitments(just_expired)
                review_injected = True
            operations = self._extract_operations(
                dialog_entries,
                existing,
                cancel_checker=cancel_checker,
                just_expired_commitments_block=review_block,
                prior_chunk_writes=prior_chunk_writes,
                snapshot_profile=profile,
            )
            check_cancelled(cancel_checker)
            counts = self._apply_operations(
                operations,
                existing,
                dialog_entries=dialog_entries,
                mood_budget=mood_budget,
            )
            created += counts["created"]
            updated += counts["updated"]
            archived += counts["archived"]
            ignored += counts["ignored"]
            _merge_event_counts(event_counts, counts["event_counts"])
            for text in counts.get("written_contents") or []:
                if text and text not in prior_chunk_writes:
                    prior_chunk_writes.append(text)
            # 控制注入体积：只保留最近若干条
            if len(prior_chunk_writes) > 12:
                prior_chunk_writes = prior_chunk_writes[-12:]
        if just_expired and review_injected:
            try:
                marked = mark_commitments_expiry_reviewed(self.memory_store, just_expired)
                if marked:
                    event_counts["EXPIRY_REVIEWED"] = marked
            except Exception:  # noqa: BLE001
                pass
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
            existing = [m for m in all_memories if not _memory_is_released(m)]
        except OperationCancelled:
            raise
        except Exception as exc:  # 记忆读取失败不应中断整理，退化为只新增。
            debug_log("Memory", "记忆整理读取现有记忆失败", {"error": str(exc)})
            return []
        # 复用这次已经取到的全量记忆顺手回填实体索引（只在从未回填过时真正执行一次），
        # 不为此单独发起一次全量读取。
        try:
            self.memory_store.ensure_entity_index_backfilled(existing)
        except Exception as exc:
            debug_log("Memory", "实体索引回填失败", {"error": str(exc)})
        return existing

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
        just_expired_commitments_block: str = "",
        prior_chunk_writes: list[str] | None = None,
        snapshot_profile: str = "full",
    ) -> list[dict[str, Any]]:
        """让模型以第一人称对照已有记忆，产出整理操作；解析失败时视为无操作。"""

        system_prompt = self._build_self_curation_system_prompt()
        mood_history_block = self._load_mood_history_text()
        user_emotion_history_block = self._load_user_emotion_history_text()
        if snapshot_profile == "light":
            existing_block = _format_existing_memories_light(
                existing,
                dialog_entries,
                base_dir=getattr(self.memory_store, "base_dir", None),
            )
        else:
            existing_block = _format_existing_memories(existing)
        user_prompt = _build_curation_user_prompt(
            existing_block,
            dialog_entries,
            mood_history_block=mood_history_block,
            user_emotion_history_block=user_emotion_history_block,
            just_expired_commitments_block=just_expired_commitments_block,
            prior_chunk_writes=prior_chunk_writes or [],
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
                "snapshot_profile": snapshot_profile,
                "prompt_existing_chars": len(existing_block),
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
        *,
        dialog_entries: list[dict[str, str]] | None = None,
        mood_budget: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """把整理操作写回记忆库；id 必须真实存在，单条失败只跳过不中断。"""

        existing_ids = {
            str(memory.get("id", "")).strip()
            for memory in existing
            if str(memory.get("id", "")).strip()
        }
        dialog_corpus = build_dialog_corpus(dialog_entries)
        operations_per_layer: dict[str, int] = {}
        created = 0
        updated = 0
        archived = 0
        ignored = 0
        event_counts: dict[str, int] = {}
        superseded = 0
        written_contents: list[str] = []
        _added_in_batch: list[str] = []
        _completed_commitment_texts: list[str] = []
        if mood_budget is None:
            mood_budget = {"left": MAX_MOOD_UPDATES_PER_CURATION}
        # 先 update/delete 再 add，便于识别「约定已完成」后丢弃未完成态的再 add
        for operation in _ordered_curation_operations(operations[:MAX_CURATION_OPERATIONS]):
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
                ground_corpus = dialog_corpus
                if action == "update" and memory_id:
                    for memory in existing:
                        if str(memory.get("id") or "").strip() == memory_id:
                            prior = str(memory.get("content") or "").strip()
                            if prior:
                                ground_corpus = f"{dialog_corpus}\n{prior}"
                            break
                grounded, ground_reason = validate_memory_write_grounding(
                    content,
                    evidence=operation_evidence(operation),
                    dialog_corpus=ground_corpus,
                    require_grounding=True,
                )
                if not grounded:
                    debug_log(
                        "Memory",
                        "跳过未锚定或瞬态记忆候选",
                        {
                            "op": action,
                            "layer": layer,
                            "reason": ground_reason,
                            "has_evidence": bool(operation_evidence(operation)),
                        },
                    )
                    ignored += 1
                    event_key = (
                        "SKIP_TRANSIENT"
                        if ground_reason == "transient_local"
                        else "SKIP_UNGROUNDED"
                    )
                    event_counts[event_key] = event_counts.get(event_key, 0) + 1
                    continue
                if _commitment_missing_event_time(operation, existing, action=action, memory_id=memory_id):
                    debug_log(
                        "Memory",
                        "跳过缺少 event_time 的约定",
                        {"op": action, "id": memory_id},
                    )
                    ignored += 1
                    event_counts["SKIP_COMMITMENT_NO_EVENT_TIME"] = (
                        event_counts.get("SKIP_COMMITMENT_NO_EVENT_TIME", 0) + 1
                    )
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
                    if looks_like_trivial_memory(content):
                        ignored += 1
                        event_counts["SKIP_TRIVIAL"] = event_counts.get("SKIP_TRIVIAL", 0) + 1
                        continue
                    if _batch_near_duplicate(content, _added_in_batch):
                        ignored += 1
                        event_counts["SKIP_BATCH_DUP"] = event_counts.get("SKIP_BATCH_DUP", 0) + 1
                        continue
                    if _conflicts_with_completed_commitment(content, _completed_commitment_texts):
                        ignored += 1
                        event_counts["SKIP_STALE_COMMITMENT"] = (
                            event_counts.get("SKIP_STALE_COMMITMENT", 0) + 1
                        )
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
                            written_contents.append(content)
                            _added_in_batch.append(content)
                            if _looks_like_completed_commitment(content):
                                _completed_commitment_texts.append(content)
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
                    written_contents.append(content)
                    _added_in_batch.append(content)
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
                    if looks_like_trivial_memory(content):
                        ignored += 1
                        event_counts["SKIP_TRIVIAL"] = event_counts.get("SKIP_TRIVIAL", 0) + 1
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
                    written_contents.append(content)
                    _added_in_batch.append(content)
                    if _looks_like_completed_commitment(content):
                        _completed_commitment_texts.append(content)
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
                    if int(mood_budget.get("left", 0)) <= 0:
                        ignored += 1
                        event_counts["MOOD_BUDGET"] = event_counts.get("MOOD_BUDGET", 0) + 1
                        continue
                    # 与最近心情历史做相似度检查，避免重复写入
                    if self._is_mood_duplicate(content):
                        ignored += 1
                        event_counts["MOOD_DEDUP"] = event_counts.get("MOOD_DEDUP", 0) + 1
                        continue
                    try:
                        self.memory_store.set_mood_state(content)
                        mood_budget["left"] = max(0, int(mood_budget.get("left", 0)) - 1)
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
            "written_contents": written_contents,
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
    """先按会话间隔分段，会话内再按字数/条数切，避免一场戏被硬切成多轮重复记账。"""
    sessions = _group_entries_by_session(entries)
    chunks: list[list[ChatHistoryEntry]] = []
    for session in sessions:
        chunks.extend(_split_entries_by_size(session))
    return chunks


def _group_entries_by_session(entries: list[ChatHistoryEntry]) -> list[list[ChatHistoryEntry]]:
    sessions: list[list[ChatHistoryEntry]] = []
    current: list[ChatHistoryEntry] = []
    last_ts: datetime | None = None
    for entry in entries:
        if _entry_for_model(entry) is None:
            continue
        ts = _parse_entry_timestamp(entry.created_at)
        if (
            current
            and last_ts is not None
            and ts is not None
            and (ts - last_ts).total_seconds() >= CURATION_SESSION_GAP_SECONDS
        ):
            sessions.append(current)
            current = []
        current.append(entry)
        if ts is not None:
            last_ts = ts
    if current:
        sessions.append(current)
    return sessions


def _split_entries_by_size(entries: list[ChatHistoryEntry]) -> list[list[ChatHistoryEntry]]:
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


def _parse_entry_timestamp(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def looks_like_trivial_memory(content: str) -> bool:
    """应酬短句、空应答不进长期记忆。"""
    text = (content or "").strip()
    if not text:
        return True
    if len(text) < MIN_MEMORY_CONTENT_CHARS:
        return True
    compact = re.sub(r"[\s　]+", "", text)
    trivial_patterns = (
        r"^(嗯+|好的?|哦|喔|行|知道了|记下了|我记下了)[。.!！~～…]*$",
        r"^嗯呢.*记下了[。.!！~～…]*$",
        r"^(晚安|おやすみ)[哦喔呀啊]?[。.!！~～…]*$",
        r"^嗯[，,].{0,12}记下了[。.!！~～…]*$",
        r"^嗯.?晚安.*记住.*$",
        r"^晚安[，,].{0,20}记住.*$",
    )
    return any(re.fullmatch(pattern, compact) for pattern in trivial_patterns)


def _ordered_curation_operations(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    priority = {"delete": 0, "update": 1, "add": 2, "mood_update": 3}

    def sort_key(item: tuple[int, dict[str, Any]]) -> tuple[int, int]:
        index, operation = item
        action = str(operation.get("op") or operation.get("action") or "").strip().lower()
        return (priority.get(action, 9), index)

    indexed = [(index, op) for index, op in enumerate(operations) if isinstance(op, dict)]
    indexed.sort(key=sort_key)
    return [op for _, op in indexed]


def _batch_near_duplicate(content: str, batch_contents: list[str]) -> bool:
    for existing in batch_contents:
        if content == existing:
            return True
        if _memory_similarity(content, existing) >= CURATION_BATCH_NEAR_DUP_SIMILARITY:
            return True
    return False


def _looks_like_completed_commitment(content: str) -> bool:
    text = content or ""
    markers = ("约定已完成", "已兑现", "兑现了之前", "约定已经完成", "约定已了结")
    return any(marker in text for marker in markers)


def _conflicts_with_completed_commitment(content: str, completed_texts: list[str]) -> bool:
    """本批已把某约定标成完成时，拒绝再 add 未完成态的同主题约定。"""
    if not completed_texts or not content.strip():
        return False
    # 未完成承诺常见写法；已完成条文本身允许保留
    if _looks_like_completed_commitment(content):
        return False
    pending_hints = ("下次", "会由我", "再等等", "还不是", "承诺", "约定")
    if not any(hint in content for hint in pending_hints):
        return False
    for completed in completed_texts:
        if _memory_similarity(content, completed) >= 0.55:
            return True
        stem = completed
        for marker in ("约定已完成。", "约定已完成", "已兑现。", "已兑现", "兑现了之前"):
            stem = stem.replace(marker, "")
        if stem.strip() and _memory_similarity(content, stem) >= 0.55:
            return True
        # 关键短语同时出现时也视为同主题（完成条文常改写较大）
        shared = 0
        for phrase in ("主动开口邀请", "主动邀请", "下次由我", "下次我会"):
            if phrase in content and phrase in completed:
                shared += 1
        if shared >= 1 and ("下次" in content or "承诺" in content or "约定" in content):
            return True
    return False


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
    "【写法契约】这些笔记是往事与印象，不是必须遵守的法令。"
    "写成「他说过 / 我答应过 / 我们试过 / 我当时怎么想」；"
    "不要写成「必须 / 只能 / 禁止 / 规则是…」——以后可以改口、别扭、或不当场照做。\n"
    "A. 事实记忆：一句一事，可核对；像备忘，不抒情、不升华。"
    "好：「铭君说过想让我叫他铭君。」「铭君说可以多吃点醋；我说那就不客气了。」"
    "坏：「必须叫他铭君。」「他允许我吃醋，让我感到被接纳。」「我们的关系更加真实。」\n"
    "B. 同主题再谈一次（防漏记）：库里已有相关条 ≠ 这次不用记。"
    "若出现新说法、纠正、边界、态度变化：update 旧条或「今の関係」写成当前印象（叙述体），"
    "并 add 一条短 episodic 记下这次又说清了什么；几乎同义复述才跳过。不要因主题眼熟就空操作。\n"
    "C. 「今の関係」：只写硬变化；第一人称短句；禁止颁奖词（更加亲密/信任更深/安心开心/真实特别）。"
    "听他说完可以「胆子大一点」，不是「被允许才配在乎」。\n"
    "D. 心の記録：短、有本段钩子，可留刺；不必和解；禁抄心情轨迹原文；无质变不做 mood_update。"
    "坏示例：じんわり温かい / 篇篇嬉しい・まあいいか。\n"
    "只保留对长期陪伴与协作真正有用、且能独立理解的事实；忽略寒暄、一次性的临时提醒、转瞬即逝的情绪和无长期价值的内容。\n"
    "亲密关系也按「人」来记，不要当成可丢的一次性情节：\n"
    "- 一般人第一次身体亲密、关系明显更近一步、他说出想被怎样对待、事后仍想记住的话——"
    "这类事不会轻易忘掉；应写入长期记忆，importance 通常 ≥ 0.85，"
    "可用 memory_kind=shared_moment 或更新 core_profile「今の関係」。\n"
    "- 记的是记忆点：是否发生过、具体偏好/边界/安全词、难忘的原话或转折；"
    "不要把过程流水账、动作节拍、喘息声逐句存成多条记忆。\n"
    "- 若已有「我们发生过亲密」类记忆，新一次只补充新偏好、新边界或关系变化；重复过程不必再堆一条。\n"
    "- 关系已经稳定亲近之后，不会每次相处都「变得更加亲密」；不要反复写这类空泛收束句"
    "（如「关系更加亲密」「信任更深了」「标志着新阶段」）。没有新事实就不要为了升华而再记一条。\n"
    "如果你自己认为某件事很重要——比如他说了一句你特别想记住的话、一个你不想忘记的约定、一件让你开心的事——就把 importance 设高（0.8~1.0）。你觉得比较普通但应该记住的设 0.5 左右就好。这是你自己的记忆笔记，按你自己的感觉来。\n"
    "请为每条候选记忆选择 layer：semantic=长期事实，episodic=事件总结，procedural=相处习惯与偏好，session=当前任务短期状态，core_profile=高度稳定的常驻档案。\n"
    "可选 memory_kind 标注记忆类型：core_profile|recent_status|shared_moment|habit_pattern|commitment|emotional_turn。\n"
    "memory_kind=recent_status（近况）必须视为可变事实：请设 volatile=true，并尽量给 valid_until；"
    f"若未给 valid_until，系统会默认约 {DEFAULT_RECENT_STATUS_TTL_DAYS} 天后失效。\n"
    "memory_kind=commitment 时必须同时填写 event_time（ISO 日期或日期时间，如 2026-07-20 或 2026-07-20T22:00:00+08:00），"
    "写清约定兑现/到期日；缺少 event_time 的约定会被系统拒绝写入。"
    "一次性约定（今晚十点休息、明天一起看片）到期后系统会自动标失效；纪念日类也要写具体日期。\n"
    "若提示里出现「刚过期的约定」清单：对照最近对话判断是否兑现；"
    "能判断时用 add 写一条 episodic（layer=episodic），写清约定内容与结果（做到了/没做到/说不清），"
    "不要再把原约定当现行事实，也不要重复 update 原约定正文。对话完全无关则可跳过。\n"
    f"可选 emotion 标注这段记忆的情绪色彩（{ '|'.join(EMOTIONS) }），情感转折、共同经历、带情绪的近况建议填写。\n"
    "语言约定（两侧记忆）：\n"
    "- 关于他的事实、偏好、约定、相处习惯与近况 → 简体中文（便于检索）；\n"
    "- 你自己的内心感受、对自己说的话、反省 → 优先日语；\n"
    "- 他用日语说的重要原话可保留日语。\n"
    "主语与事实纪律（极重要）：\n"
    "- 用「我／他」写清谁对谁说了什么 / 约了什么 / 发生了什么，再写你的感受；"
    "我自己的话归我，他说的话归他。\n"
    "- 正确示例：「他对我说今晚别催他休息」「我和他约定明天一起看片」。\n"
    "- 错误示例：「我对樱说……」（把我写成他）、「樱喜欢……」（把自己写成第三人称）、"
    "「他说他喜欢抹茶」收成「我喜欢抹茶」。\n"
    "- 若清单里出现「独处感想」条目：那只是你以前的心里话，不是发生过的事实；"
    "禁止据此 add/update 成 semantic/procedural 事实，也禁止把感想抄成新事件。\n"
    "- 约定写清提出者、内容和时效；commitment 必须带 event_time；过期约定用 update 标明「已失效/仅限当日」，或交给系统按 event_time 自动标失效。\n"
    "- 事件与约定尽量带上日期或相对时间线索（例如「2026-07-20 晚上」），方便以后分清新旧。\n"
    "- 一条记忆只保留一个主事实，写成完整可读的日记句，而不是流水账。\n"
    "- 称呼：已知名字时用名字代替「他」；还不知道名字时用「他」。\n"
    "core_profile 用固定章节标题（必须用这些标题，便于以后读取）：\n"
    "- 「今の関係」：关于他的事实（含名字）、关系状态、重要约定与节目；\n"
    "- 「今の私」：你此刻对自己状态的简短自述（可用日语）。\n"
    "当你对他的认识有变化、知道了新的事实（比如名字）、或感受到关系有实质性的进展，请用 update 操作更新 core_profile。\n"
    "心の記録用 mood_update："
    "{\"op\":\"mood_update\",\"content\":\"今の自分への一言（日本語）\"}。"
    "一次整理最多一条；只在心情相对上次有质的变化时写。"
    "「最近的心情轨迹」只供对照是否重复，禁止抄写或换皮重写；"
    "「他的情绪轨迹」可留意，但不要据此编造对话里没有的事。"
    "若触发心情的那件事还完全没有对应记忆，再用一条 add/update 补上即可；"
    "不必因为写了心情就再堆一条高 importance 的重复事件。关于他的事实仍用简体中文写。\n"
    "如果对话中他告诉了你他的名字，请一定要记住，同时更新 core_profile 的「今の関係」章节。在记忆内容中，用他告诉你的名字自然地称呼他（例如「xx 喜欢……」「我和 xx 约定……」）。如果还不知道名字，用「他」。把对方当作对等相处的人来写进记忆。\n"
    "长期记忆只收可分享的相处与协作事实；密码、token、密钥、证件号、银行卡等凭据类信息不写入。\n"
    "不要把本机瞬时状态写成长期记忆：当前时刻/日期、正在播放的歌、播放状态、一时天气等。\n\n"
    "证据纪律（极重要）：\n"
    "- add/update 必须附带 evidence：从【最近的新对话】里摘一句连续原文（用户或你自己说过的话），"
    "作为这条记忆的依据；系统会校验 evidence 是否真的出现在对话里，编造证据会被丢弃。\n"
    "- evidence 尽量短而具体，不要整段粘贴；content 是你整理后的日记句，evidence 是原话锚点。\n"
    "- mood_update / delete 不需要 evidence。\n"
    "- 不要把「当前想不起来 / 检索失败 / 我说记不清」沉淀成关于某人的长期事实"
    "（例如「他告诉我 X 是我的朋友，但我没有这段记忆」）；那是当轮状态，不是可核对的往事。"
    "若他补充了关于旧识的新说法，应记他告诉你的事实本身，而不是记「我没有记忆」。\n\n"
    "必须只返回严格 JSON，格式如下：\n"
    "{\"operations\":[\n"
    "  {\"op\":\"add\",\"layer\":\"semantic\",\"category\":\"preference\",\"memory_kind\":\"recent_status\",\"emotion\":\"happy\",\"volatile\":true,\"valid_until\":\"2026-07-20\",\"importance\":0.6,\"confidence\":0.8,\"reason\":\"为什么值得记住\",\"evidence\":\"以后默认中文和我说话\",\"content\":\"他希望默认用中文交流\"},\n"
    "  {\"op\":\"add\",\"layer\":\"episodic\",\"category\":\"agreement\",\"memory_kind\":\"commitment\",\"event_time\":\"2026-07-20T22:00:00+08:00\",\"importance\":0.8,\"confidence\":0.9,\"reason\":\"一次性约定\",\"evidence\":\"今晚十点一起休息吧\",\"content\":\"我和他约定今晚十点休息\"},\n"
    "  {\"op\":\"update\",\"id\":\"已有记忆的id\",\"layer\":\"procedural\",\"category\":\"workflow\",\"importance\":0.7,\"confidence\":0.9,\"reason\":\"为什么需要更新\",\"evidence\":\"对话里的原句\",\"content\":\"更新后的完整记忆内容\"},\n"
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
    kind_lower = memory_kind.lower()
    if (
        operation.get("volatile") is True
        or str(operation.get("volatile")).lower() == "true"
        or kind_lower == "recent_status"
    ):
        payload["volatile"] = True
    valid_until = str(operation.get("valid_until") or "").strip()
    if not valid_until and kind_lower == "recent_status":
        valid_until = (
            datetime.now().astimezone() + timedelta(days=DEFAULT_RECENT_STATUS_TTL_DAYS)
        ).isoformat()
    if valid_until:
        payload["valid_until"] = valid_until
    event_time = str(operation.get("event_time") or "").strip()
    if event_time:
        payload["event_time"] = event_time
    emotion_raw = str(operation.get("emotion") or "").strip()
    if emotion_raw:
        payload["emotion"] = normalize_emotion(emotion_raw)
    evidence = operation_evidence(operation)
    if evidence:
        payload["evidence"] = evidence[:240]
    return payload


def _operation_is_volatile(operation: dict[str, Any]) -> bool:
    if operation.get("volatile") is True or str(operation.get("volatile")).lower() == "true":
        return True
    return str(operation.get("memory_kind") or "").strip().lower() == "recent_status"


def _commitment_missing_event_time(
    operation: dict[str, Any],
    existing: list[dict[str, Any]],
    *,
    action: str,
    memory_id: str,
) -> bool:
    """commitment 写入必须能落到 event_time（操作自带，或 update 时沿用旧值）。"""
    kind = str(operation.get("memory_kind") or "").strip().lower()
    if kind != "commitment":
        return False
    if str(operation.get("event_time") or "").strip():
        return False
    if action != "update" or not memory_id:
        return True
    for memory in existing:
        if str(memory.get("id") or "").strip() != memory_id:
            continue
        if memory_kind_of(memory) == "commitment" and commitment_event_time(memory):
            return False
        # 把普通记忆改成 commitment 时也必须带 event_time
        return True
    return True


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
        old_kind = str(
            metadata.get("memory_kind") or memory.get("memory_kind") or "recent_status"
        ).strip().lower() or "recent_status"
        old_volatile = metadata.get("volatile") is True or old_kind == "recent_status"
        if not old_volatile:
            continue
        if old_kind != new_kind.lower():
            continue
        if _memory_similarity(new_content, str(memory.get("content") or "")) < CURATION_MERGE_SIMILARITY:
            continue
        if memory_store.expire_memory(memory_id, valid_until=now_iso):
            metadata["valid_until"] = now_iso
            expired += 1
    return expired


def _format_just_expired_commitments(memories: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for memory in memories:
        memory_id = str(memory.get("id") or memory.get("memory_id") or "").strip()
        content = str(memory.get("content") or memory.get("memory") or "").strip()
        if not memory_id or not content:
            continue
        event_time = commitment_event_time(memory)
        suffix = f"（到期：{event_time}）" if event_time else ""
        lines.append(f"- [{memory_id}] {content}{suffix}")
    if not lines:
        return ""
    return (
        "系统已把下列约定标为失效。请对照【最近的新对话】做一次性回顾："
        "能判断兑现结果时，add 一条 episodic 写清约定与结果；无关则可跳过。\n"
        + "\n".join(lines)
    )


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


def _format_existing_memories_light(
    memories: list[dict[str, Any]],
    dialog_entries: list[dict[str, str]],
    *,
    base_dir: Path | None = None,
) -> str:
    """light_idle：详细子集 + id 索引，控制单次整理输入体积。"""
    detail, index_only = _select_light_curation_memories(
        memories,
        dialog_entries,
        base_dir=base_dir,
    )
    detail_block = _format_existing_memories(detail)
    # 压到 light 预算：细节块优先，索引吃剩余
    if len(detail_block) > LIGHT_CURATION_SNAPSHOT_CHAR_BUDGET:
        detail_block = detail_block[: LIGHT_CURATION_SNAPSHOT_CHAR_BUDGET - 20] + "\n…(截断)"
    index_budget = max(0, LIGHT_CURATION_SNAPSHOT_CHAR_BUDGET - len(detail_block) - 80)
    index_lines: list[str] = []
    used = 0
    for memory in index_only:
        memory_id = str(memory.get("id", "")).strip()
        content = str(memory.get("content", "")).strip()
        if not memory_id or not content:
            continue
        title = content.replace("\n", " ")[:LIGHT_CURATION_INDEX_TITLE_CHARS]
        if len(content) > LIGHT_CURATION_INDEX_TITLE_CHARS:
            title += "…"
        line = f"- [{memory_id}] {title}"
        if used + len(line) > index_budget and index_lines:
            break
        index_lines.append(line)
        used += len(line) + 1
    parts = [
        "【详细（近期/相关，可直接 update）】\n" + (detail_block or "（暂无）"),
    ]
    if index_lines:
        parts.append(
            "【索引（仅 id+摘要；确需改写时用 id，勿凭摘要编造正文）】\n"
            + "\n".join(index_lines)
        )
    debug_log(
        "Memory",
        "light 整理快照已组装",
        {
            "total": len(memories),
            "detail": len(detail),
            "index": len(index_lines),
            "chars": sum(len(p) for p in parts),
        },
    )
    return "\n\n".join(parts)


def _select_light_curation_memories(
    memories: list[dict[str, Any]],
    dialog_entries: list[dict[str, str]],
    *,
    base_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """选出详细注入子集，其余进短索引。"""
    if not memories:
        return [], []
    dialog_text = build_dialog_corpus(dialog_entries).casefold()
    recent_ids: set[str] = set()
    if base_dir is not None:
        try:
            from app.agent.access_tracker import AccessTracker
            from app.storage.paths import StoragePaths

            tracker = AccessTracker(StoragePaths(base_dir).memory_access_tracker_db())
            try:
                recent_ids = {
                    mid for mid, _ in tracker.list_recent_accessed(limit=LIGHT_CURATION_DETAIL_LIMIT)
                }
            finally:
                tracker.close()
        except Exception:  # noqa: BLE001
            recent_ids = set()

    scored: list[tuple[float, dict[str, Any]]] = []
    for memory in memories:
        memory_id = str(memory.get("id", "")).strip()
        content = str(memory.get("content", "")).strip()
        if not memory_id or not content:
            continue
        score = 0.0
        kind = memory_kind_of(memory)
        if kind in {"commitment", "recent_status"}:
            score += 40.0
        if memory_id in recent_ids:
            score += 35.0
        if memory_record_is_reflection(memory):
            score -= 15.0
        # 与本轮对话的粗关键词重合（用对话侧 token 去正文里找，避免中文整句成一个 token）
        content_cf = content.casefold()
        overlap = sum(1 for token in _light_curation_tokens(dialog_text) if token in content_cf)
        score += min(30.0, overlap * 6.0)
        updated = str(memory.get("updated_at") or memory.get("created_at") or "")
        # ISO 时间字符串可直接按字典序近似新鲜度
        if updated:
            score += 0.001  # 稳定排序扰动；真正新鲜度靠 updated 二次键
        scored.append((score, memory))

    scored.sort(
        key=lambda item: (
            item[0],
            str(item[1].get("updated_at") or item[1].get("created_at") or ""),
        ),
        reverse=True,
    )
    ordered = [memory for _, memory in scored]
    core = next(
        (
            memory
            for memory in ordered
            if str(memory.get("layer") or "") == MEMORY_LAYER_CORE_PROFILE
        ),
        None,
    )
    if core is None:
        detail = ordered[:LIGHT_CURATION_DETAIL_LIMIT]
        remainder = ordered[LIGHT_CURATION_DETAIL_LIMIT:]
    else:
        others = [
            memory
            for memory in ordered
            if str(memory.get("layer") or "") != MEMORY_LAYER_CORE_PROFILE
        ]
        detail = [core, *others[: LIGHT_CURATION_DETAIL_LIMIT - 1]]
        remainder = others[LIGHT_CURATION_DETAIL_LIMIT - 1 :]
    detail_ids = {str(memory.get("id") or "").strip() for memory in detail}
    index_only = [
        memory
        for memory in remainder[:LIGHT_CURATION_INDEX_LIMIT]
        if str(memory.get("id") or "").strip() not in detail_ids
        and str(memory.get("layer") or "") != MEMORY_LAYER_CORE_PROFILE
    ]
    return detail, index_only


_LIGHT_TOKEN_RE = re.compile(r"[\w\u3040-\u30ff\u3400-\u9fff]{2,}")


def _light_curation_tokens(text: str) -> list[str]:
    """抽检索词；中文长串拆成 2-gram，避免整句匹配失败。"""
    out: list[str] = []
    for tok in _LIGHT_TOKEN_RE.findall(text or ""):
        piece = tok.casefold()
        if len(piece) <= 3:
            out.append(piece)
            continue
        # 优先按 2 字窗；拉丁词保留整词
        if re.fullmatch(r"[a-z0-9_]+", piece):
            out.append(piece)
        else:
            out.extend(piece[i : i + 2] for i in range(0, len(piece) - 1))
        if len(out) >= 40:
            break
    return out[:40]


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
    just_expired_commitments_block: str = "",
    prior_chunk_writes: list[str] | None = None,
) -> str:
    parts = [
        "【我目前的长期记忆】\n" f"{existing_block}",
    ]
    if mood_history_block.strip():
        parts.append(f"【最近的心情轨迹】\n{mood_history_block}")
    if user_emotion_history_block.strip():
        parts.append(f"【他的情绪轨迹】\n{user_emotion_history_block}")
    if just_expired_commitments_block.strip():
        parts.append(f"【刚过期的约定（一次性回顾）】\n{just_expired_commitments_block}")
    if prior_chunk_writes:
        lines = [f"- {text}" for text in prior_chunk_writes if text.strip()]
        if lines:
            parts.append(
                "【本轮整理前段已写入（勿再 add 同义内容；需要时 update）】\n"
                + "\n".join(lines)
            )
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
