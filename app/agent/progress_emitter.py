"""流式 / 过程进度回调发射器 — 从 runtime.py 拆分的叶子模块。

纯函数、无实例状态；被 AgentRuntime 的工具循环与事件回复路径调用。
"""

from __future__ import annotations

import time
from typing import Any, Callable

from app.agent.actions import AgentProgress
from app.agent.runtime_limits import ProgressCallback
from app.core.cancellation import CancelChecker, OperationCancelled, check_cancelled
from app.core.debug_log import debug_log
from app.llm.chat_reply import ChatReply, ChatSegment, parse_chat_reply


# 结构化 JSON 回复在括号闭合前几乎总是解析失败：如果每个 delta chunk 都重新对
# 累积文本做一次完整解析尝试，长回复下等于对已积累文本反复全量重扫，是无谓的
# O(n²) CPU 开销。按最小时间间隔节流即可保留“早期有反馈”的体验。
STREAM_PROGRESS_MIN_INTERVAL_SECONDS = 0.2


def _build_stream_progress_emitter(
    progress_callback: ProgressCallback | None,
    cancel_checker: CancelChecker | None,
) -> Callable[[str], None]:
    """构建限频的流式进度回调：累积 chunk，但不超过节流间隔不重新解析。"""
    streamed_chunks: list[str] = []
    last_emit_at = 0.0

    def on_chunk(chunk: str) -> None:
        nonlocal last_emit_at
        streamed_chunks.append(chunk)
        now = time.perf_counter()
        if now - last_emit_at < STREAM_PROGRESS_MIN_INTERVAL_SECONDS:
            return
        last_emit_at = now
        _emit_progress_from_content(
            progress_callback,
            "".join(streamed_chunks),
            stage="streaming",
            metadata={"partial": True},
            cancel_checker=cancel_checker,
        )

    return on_chunk


def _emit_progress_from_content(
    progress_callback: ProgressCallback | None,
    content: str,
    *,
    stage: str,
    metadata: dict[str, Any],
    cancel_checker: CancelChecker | None = None,
) -> None:
    check_cancelled(cancel_checker)
    if progress_callback is None or not content.strip():
        return
    if not _should_emit_progress(metadata):
        return
    try:
        reply = parse_chat_reply(content)
    except Exception:
        return
    if not reply.text.strip():
        return
    try:
        check_cancelled(cancel_checker)
        progress_callback(AgentProgress(reply=reply, stage=stage, metadata=metadata))
    except OperationCancelled:
        raise
    except Exception as exc:
        debug_log("AgentRuntime", "中间回复回调失败，已忽略", {"error": str(exc), "stage": stage})


def _progress_reply_suppress_tts(stage: str) -> bool:
    """过程旁白是否静音。

    只让「我查查」开口；搜到标题摘要会和这句抢麦，读页长摘要也不播。
    """
    return stage != "web_planning"


def _emit_progress_reply(
    progress_callback: ProgressCallback | None,
    *,
    ja: str,
    zh: str,
    stage: str,
    metadata: dict[str, Any],
    cancel_checker: CancelChecker | None = None,
    suppress_tts: bool | None = None,
) -> None:
    """发送联网搜索过程旁白（不依赖模型 planning content）。"""
    check_cancelled(cancel_checker)
    if progress_callback is None:
        return
    ja_text = (ja or "").strip()
    zh_text = (zh or "").strip()
    if not ja_text and not zh_text:
        return
    quiet = _progress_reply_suppress_tts(stage) if suppress_tts is None else suppress_tts
    reply = ChatReply(
        [
            ChatSegment(
                text=ja_text or zh_text,
                translation=zh_text or ja_text,
                tone="中性",
                suppress_tts=quiet,
            )
        ]
    )
    try:
        check_cancelled(cancel_checker)
        progress_callback(AgentProgress(reply=reply, stage=stage, metadata=metadata))
    except OperationCancelled:
        raise
    except Exception as exc:
        debug_log("AgentRuntime", "过程旁白回调失败，已忽略", {"error": str(exc), "stage": stage})


def _should_emit_progress(metadata: dict[str, Any]) -> bool:
    """只播报关键等待点，避免工具链每一步都打断用户。"""
    stage = str(metadata.get("stage") or "")
    if stage.startswith("web_"):
        return True
    step_index = metadata.get("step_index")
    if not isinstance(step_index, int):
        return True
    if step_index == 0:
        return True
    tool_names = metadata.get("tool_names", [])
    if not isinstance(tool_names, list):
        return False
    if any(str(name).startswith(("web__", "web_")) for name in tool_names):
        return True
    return any(str(name).startswith("windows__") for name in tool_names)
