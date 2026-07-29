"""把短时屏幕印象编成主对话 ContextFragment（控制 token）。"""

from __future__ import annotations

from app.llm.prompts.types import ContextFragment
from app.perception.sensory_impression import sensory_impression_store

# ~80–100 tokens 量级；印象本身再硬顶 160 字
SENSORY_IMPRESSION_TOKEN_BUDGET = 128


def build_sensory_impression_fragment(
    *,
    now: float | None = None,
) -> ContextFragment | None:
    content = sensory_impression_store.format_chat_block(now=now)
    if not content:
        return None
    return ContextFragment(
        fragment_id="runtime.sensory_impression",
        source="runtime",
        content=content,
        trust="trusted",
        priority=70,
        token_budget=SENSORY_IMPRESSION_TOKEN_BUDGET,
        sensitivity="private",
        cache_scope="turn",
        required=False,
    )
