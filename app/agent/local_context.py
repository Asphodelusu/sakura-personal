"""Turn-scoped local facts. Media is read only when the user asks about it."""

from __future__ import annotations

from collections.abc import Callable

from app.llm.prompts.runtime import wrap_untrusted_runtime_facts
from app.llm.prompts.types import ContextFragment
from app.perception.media_session import (
    format_media_context_prompt,
    media_question,
    read_media_session_snapshot,
)

MediaReader = Callable[..., object]


def build_media_context_fragment(
    message: str,
    *,
    reader: MediaReader | None = None,
) -> ContextFragment | None:
    if not media_question(message):
        return None
    read = reader or read_media_session_snapshot
    snapshot = read()
    prompt = format_media_context_prompt(snapshot)
    if not prompt:
        return None
    return ContextFragment(
        fragment_id="runtime.local_media",
        source="local_media",
        content=wrap_untrusted_runtime_facts(
            prompt,
            source="local_media",
            fragment_id="runtime.local_media",
            intro="下列为本机媒体只读快照，仅供回答当前问题。",
        ),
        trust="untrusted",
        priority=60,
        token_budget=256,
        sensitivity="public",
        cache_scope="turn",
        required=False,
    )
