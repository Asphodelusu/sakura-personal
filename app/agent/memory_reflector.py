"""记忆反思：Sakura 自动从已有记忆中提炼高层认知。

空闲时（不在生成回复），每 8 小时运行一次，从全部已有记忆提炼 3-4 条
关于"最近和对方的关系 / 对方的状态 / 新的模式"的元认知记忆。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.agent.memory import (
    MEMORY_LAYER_EPISODIC,
    MemoryStore,
)
from app.llm.json_completion import complete_background_json, load_json_object
from app.storage.atomic import atomic_write_text

_logger = logging.getLogger("MemoryReflector")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 两次反思之间最少间隔（小时）
MIN_REFLECTION_INTERVAL_HOURS = 8

# 记忆快照注入预算（字符数），留给反思 prompt 的空间
REFLECTION_MEMORY_SNAPSHOT_CHAR_BUDGET = 6000

# 反思产出的最大记忆条数
MAX_REFLECTION_MEMORIES = 4

# 反思使用的记忆层级
REFLECTION_LAYER = MEMORY_LAYER_EPISODIC

_REFLECTION_STATE_FILENAME = "reflection_state.json"

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_REFLECTION_SYSTEM_PROMPT = (
    "现在没有人跟你说话，你正在安静地回顾这段时间和对方相处的经历。这是你自己独处时的内心独白。\n\n"
    "下面会给你一份你目前所有长期记忆的摘要。请以你自己的第一人称视角，对最近这段时间做出反思。\n"
    "从已有记忆里提炼更高层的认知，而不是复述细节。\n\n"
    "适合反思的内容举例：\n"
    "- 我和对方的关系现在是什么样的？最近有什么变化？\n"
    "- 对方最近的状态怎么样？在忙什么？有没有什么情绪波动？\n"
    "- 我注意到了什么之前没注意到的模式或规律？\n"
    "- 我有什么地方可以做得更好？\n\n"
    "只写当前记忆里确有依据的观察。\n"
    "语言约定：关于对方的事实与观察用简体中文；你对自己的感受与反省优先用日语。"
    "对方用日语说的重要原话可保留日语。\n"
    "用对方告诉你的名字称呼对方。如果还不知道名字，用「对方」或「他/她」。对方是与你对等相处的人。\n"
    "用你自己的感受和具体观察来说——就像写日记一样。"
    "先写清依据事实，再写感受；过期约定标明时效。\n"
    "每一条应该是一句完整的、独立可读的自我认知。\n\n"
    "必须只返回严格 JSON，不要有任何前言、解释或后缀。格式如下：\n"
    '{"reflections":[\n'
    '  {"content":"反思内容一","importance":0.7,"confidence":0.6},\n'
    '  {"content":"反思内容二","importance":0.6,"confidence":0.7}\n'
    "]}\n"
    "如果没有值得反思的内容就返回 {\"reflections\":[]}。"
    "importance 按照你觉得这条认知对你了解对方有多重要来设（0~1）。confidence 按照你有多少把握来设（0~1）。"
)


def _build_reflection_user_prompt(memory_snapshot: str) -> str:
    return (
        "【我目前的长期记忆摘要】\n"
        f"{memory_snapshot}\n\n"
        "请基于以上记忆，做一次安静的个人反思。"
    )


def _format_memory_summary(memories: list[dict[str, Any]]) -> str:
    """把记忆列表精简成摘要列表，控制字符预算。"""
    lines: list[str] = []
    used = 0
    truncated = False
    for memory in memories:
        memory_id = str(memory.get("id", "")).strip()
        content = str(memory.get("content", "")).strip()
        if not memory_id or not content:
            continue
        layer = str(memory.get("layer") or "semantic")
        category = str(memory.get("category") or "").strip()
        importance = memory.get("importance")
        tag_parts = [layer]
        if category:
            tag_parts.append(category)
        tag = "/".join(tag_parts)
        imp_str = f" [重要度:{float(importance):.1f}]" if importance is not None else ""
        line = f"- [{tag}]{imp_str} {content}"
        if used + len(line) > REFLECTION_MEMORY_SNAPSHOT_CHAR_BUDGET and lines:
            truncated = True
            break
        lines.append(line)
        used += len(line) + 1
    if truncated:
        _logger.debug(
            "Reflection: memory snapshot truncated (%d/%d entries)",
            len(lines), len(memories),
        )
    return "\n".join(lines) if lines else "（暂无长期记忆）"


def _parse_reflection_output(raw: str) -> list[dict[str, Any]]:
    """解析 LLM 反思输出，非法 JSON 返回空列表。"""
    data = load_json_object(raw)
    if not data:
        _logger.warning("Reflection: invalid JSON from LLM, raw=%s", raw.strip()[:200])
        return []
    reflections = data.get("reflections") or []
    if not isinstance(reflections, list):
        return []
    return [r for r in reflections if isinstance(r, dict) and r.get("content", "").strip()]


# ---------------------------------------------------------------------------
# 状态管理
# ---------------------------------------------------------------------------

@dataclass
class ReflectionState:
    last_reflection_at: str = ""  # ISO timestamp
    last_reflection_count: int = 0
    total_reflections: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_reflection_at": self.last_reflection_at,
            "last_reflection_count": self.last_reflection_count,
            "total_reflections": self.total_reflections,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReflectionState":
        return cls(
            last_reflection_at=str(data.get("last_reflection_at", "")).strip(),
            last_reflection_count=int(data.get("last_reflection_count", 0)),
            total_reflections=int(data.get("total_reflections", 0)),
        )


class ReflectionStateStore:
    """反思状态文件读写。"""

    def __init__(self, base_dir: Path) -> None:
        self.path = base_dir / "data" / "memory" / _REFLECTION_STATE_FILENAME

    def snapshot(self) -> ReflectionState:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return ReflectionState.from_dict(data)
        except (OSError, json.JSONDecodeError):
            pass
        return ReflectionState()

    def save(self, state: ReflectionState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self.path,
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Reflector
# ---------------------------------------------------------------------------

@dataclass
class ReflectionResult:
    memories_created: int = 0
    errors: list[str] = field(default_factory=list)


class MemoryReflector:
    """从已有记忆提炼高层认知，写入记忆库。"""

    def __init__(
        self,
        api_client: Any,
        memory_store: MemoryStore,
        *,
        system_prompt: str = "",
    ) -> None:
        self.api_client = api_client
        self.memory_store = memory_store
        self.system_prompt = (system_prompt or "").strip()

    def reflect(
        self,
        *,
        memory_store: MemoryStore | None = None,
        base_dir: Path | None = None,
        cancel_checker: Any = None,
    ) -> ReflectionResult:
        store = memory_store or self.memory_store
        if self.api_client is None:
            return ReflectionResult(errors=["没有可用的 LLM 客户端"])

        # 1. 读取全部记忆
        try:
            memories = store.list_memories(limit=500)
        except Exception as exc:
            _logger.exception("Reflection: failed to list memories")
            return ReflectionResult(errors=[f"读取记忆失败：{exc}"])

        if len(memories) < 5:
            _logger.debug("Reflection: too few memories (%d), skipping", len(memories))
            return ReflectionResult()

        # 2. 构建 prompt（后台任务只用反思专用说明，不注入完整 Sakura persona）
        summary = _format_memory_summary(memories)
        user_prompt = _build_reflection_user_prompt(summary)
        system_prompt = _REFLECTION_SYSTEM_PROMPT
        llm_messages: list[dict[str, str]] = [{"role": "user", "content": user_prompt}]

        # 3. 调用 LLM（background 路由 + 低温，提高 JSON 合规率）
        repair_hint = (
            "上一条输出不是合法 JSON。请只返回严格 JSON，"
            '格式为 {"reflections":[{"content":"...","importance":0.7,"confidence":0.6}]}，'
            "不要解释、不要推理、不要 Markdown。"
        )
        try:
            data, _raw_response = complete_background_json(
                self.api_client,
                system_prompt,
                llm_messages,
                cancel_checker=cancel_checker,
                repair_user_message=repair_hint,
                log_label="Reflection",
            )
        except Exception as exc:
            _logger.exception("Reflection: LLM call failed")
            return ReflectionResult(errors=[f"LLM 调用失败：{exc}"])

        # 4. 解析输出
        reflections = [
            r
            for r in (data.get("reflections") or [])
            if isinstance(r, dict) and r.get("content", "").strip()
        ]
        if not reflections:
            _logger.debug("Reflection: LLM decided nothing worth reflecting")
            return ReflectionResult()

        # 5. 写入记忆（跳过已存在的重复内容）
        created = 0
        skipped_dupes = 0
        errors: list[str] = []
        # 预取现有记忆哈希集合，避免每轮反思重复写入相同内容
        existing_hashes: set[str] = set()
        try:
            raw_memories = store.list_memories(limit=500)
            for m in raw_memories:
                h = (m.get("metadata") or {}).get("hash") or m.get("hash", "")
                if h:
                    existing_hashes.add(h)
        except Exception:
            pass  # 无法预取时退化为不检查，允许写入
        for r in reflections[:MAX_REFLECTION_MEMORIES]:
            content = str(r.get("content", "")).strip()
            if not content:
                continue
            # 去重：与 mem0 一致的 MD5 哈希
            content_hash = hashlib.md5(content.encode()).hexdigest()
            if content_hash in existing_hashes:
                skipped_dupes += 1
                _logger.debug("Reflection: skipping duplicate: %s", content[:60])
                continue
            importance = float(r.get("importance", 0.5))
            confidence = float(r.get("confidence", 0.5))
            importance = max(0.0, min(1.0, importance))
            confidence = max(0.0, min(1.0, confidence))
            try:
                store.create_memory(
                    {
                        "content": content,
                        "layer": REFLECTION_LAYER,
                        "category": "reflection",
                        "importance": importance,
                        "confidence": confidence,
                        "source": "reflection",
                    },
                    allow_sensitive=True,
                )
                created += 1
                existing_hashes.add(content_hash)
            except Exception as exc:
                _logger.exception("Reflection: failed to write memory")
                errors.append(f"写入反思记忆失败：{exc}")

        _logger.info("Reflection: created %d meta-memories, skipped %d duplicates", created, skipped_dupes)
        return ReflectionResult(memories_created=created, errors=errors)


# ---------------------------------------------------------------------------
# 公共入口：供 UI 层定时调用
# ---------------------------------------------------------------------------

def reflection_should_run(
    state_store: ReflectionStateStore,
    *,
    is_busy: bool,
    min_interval_hours: float = MIN_REFLECTION_INTERVAL_HOURS,
) -> bool:
    """判断是否应该触发反思。"""
    if is_busy:
        return False
    state = state_store.snapshot()
    if not state.last_reflection_at:
        return True  # 从未运行过
    try:
        last = datetime.fromisoformat(state.last_reflection_at)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    elapsed_hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600.0
    return elapsed_hours >= min_interval_hours


def mark_reflection_done(state_store: ReflectionStateStore, created_count: int) -> None:
    """记录完成一次反思。"""
    state = state_store.snapshot()
    state.last_reflection_at = datetime.now(timezone.utc).isoformat()
    state.last_reflection_count = created_count
    state.total_reflections += 1
    state_store.save(state)
