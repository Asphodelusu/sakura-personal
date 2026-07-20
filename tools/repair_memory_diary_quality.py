# -*- coding: utf-8 -*-
"""一次性清理：修正易误导行为的记忆条目，并补写今晚约定。"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent.memory import MemoryStore
from app.config.settings_service import AppSettingsService


UPDATES: dict[str, str] = {
    "4a531b16-14b0-48a4-924f-bba9451d66fb": (
        "（已失效）2026年7月12日前后，铭君曾约定某天凌晨前睡觉；该日约定早已过期。"
        "不要当作现行作息规则，也不要据此反复催他休息。"
    ),
}

NEW_MEMORIES = [
    {
        "content": (
            "2026年7月20日约21:22，是我先对铭君说『已经很晚了，差不多该休息了』；"
            "他明确回复『十二点之前不用再提了』，我答应『12点之前不说了』。"
            "在约定生效前不要再主动催他休息。"
        ),
        "layer": "procedural",
        "category": "commitment",
        "memory_kind": "commitment",
        "importance": 0.9,
        "confidence": 0.95,
        "volatile": True,
        "valid_until": "2026-07-21",
        "source": "manual_repair",
        "emotion": "calm",
    },
]


def _rewrite_zhuren(text: str) -> str | None:
    if "主人" not in text:
        return None
    # 保留「不要叫我主人 / 他不叫我主人」这类元叙述
    if "「主人」" in text and ("ではなく" in text or "不是" in text or "不要" in text):
        return None
    new = text
    new = new.replace("主人（胡椒）", "铭君（胡椒）")
    new = new.replace("主人（铭君）", "铭君")
    new = re.sub(r"(?<![「『])主人(?![」』])", "铭君", new)
    if new == text:
        return None
    return new


def main() -> None:
    settings = AppSettingsService(ROOT).load_api_settings()
    if not settings.api_key:
        raise SystemExit("api.yaml 缺少 api_key，无法初始化记忆客户端")
    store = MemoryStore(base_dir=ROOT, api_settings=settings, scope_id="Sakura")
    print("waiting for mem0...")
    mem = store._get_memory(wait=True)
    assert mem is not None
    print("mem0 ready")

    for mid, content in UPDATES.items():
        print("update", mid)
        store.update_memory({"id": mid, "content": content, "source": "manual_repair"})

    for payload in NEW_MEMORIES:
        print("add", payload["content"][:40], "...")
        store.create_memory(payload, allow_sensitive=False, wait=True)

    memories = store.list_memories(limit=None)
    scrubbed = 0
    for item in memories:
        mid = str(item.get("id") or "")
        content = str(item.get("content") or "")
        if not mid or mid.startswith("core_profile:"):
            continue
        rewritten = _rewrite_zhuren(content)
        if rewritten is None:
            continue
        print("scrub", mid)
        store.update_memory({"id": mid, "content": rewritten, "source": "manual_repair"})
        scrubbed += 1

    cp = store.core_profile()
    if cp is not None:
        text = str(cp.get("content") or "")
        fixed = text.replace("＜今の関係について＞", "＜今の関係＞")
        if fixed != text:
            print("fix core_profile section title")
            meta = cp.get("metadata") if isinstance(cp.get("metadata"), dict) else None
            store.set_core_profile(fixed, meta)

    print("done. scrubbed=", scrubbed)


if __name__ == "__main__":
    main()
