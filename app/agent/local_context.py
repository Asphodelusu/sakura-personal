"""按需本机上下文（媒体等）注入。"""

from __future__ import annotations

from app.llm.prompts.runtime import wrap_untrusted_runtime_facts
from app.llm.prompts.types import ContextFragment
from app.perception.media_session import (
    format_media_context_prompt,
    local_context_relevance,
    read_media_session_snapshot,
)


def build_media_context_fragment(message: str) -> ContextFragment | None:
    relevance = local_context_relevance(message)
    if not relevance.get("media"):
        return None
    snapshot = read_media_session_snapshot()
    prompt = format_media_context_prompt(snapshot)
    if not prompt:
        return None
    wrapped = wrap_untrusted_runtime_facts(
        prompt,
        source="local_media",
        fragment_id="runtime.local_media",
        intro="下列为本机媒体只读快照，仅供回答当前问题。",
    )
    return ContextFragment(
        fragment_id="runtime.local_media",
        source="local_media",
        content=wrapped,
        trust="untrusted",
        priority=60,
        token_budget=256,
        sensitivity="public",
        cache_scope="turn",
        required=False,
    )
