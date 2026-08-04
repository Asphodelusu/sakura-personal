from __future__ import annotations

import json
from pathlib import Path

from app.agent.lore import (
    format_lore_prompt,
    load_lore_index,
    retrieve_lore,
)


def _write_lore(tmp_path: Path) -> Path:
    path = tmp_path / "lore" / "index.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "character_id": "demo",
                "canon_context": "原作剧情",
                "entries": [
                    {
                        "id": "first-meeting",
                        "kind": "main_event",
                        "title": "初次见面",
                        "aliases": ["初遇"],
                        "keywords": ["第一次见面", "草莓蛋糕"],
                        "summary": "他们第一次见面时一起吃了草莓蛋糕。",
                        "facts": ["第一次见面吃了草莓蛋糕"],
                        "next": ["after-cake"],
                        "source_refs": ["script/01.txt#1"],
                        "priority": 80,
                    },
                    {
                        "id": "after-cake",
                        "kind": "main_event",
                        "title": "蛋糕之后",
                        "keywords": ["散步"],
                        "summary": "吃完蛋糕后去散步。",
                        "facts": ["之后去散步"],
                        "source_refs": ["script/01.txt#2"],
                        "priority": 70,
                    },
                    {
                        "id": "bad-end",
                        "kind": "alternate",
                        "title": "另一结局",
                        "keywords": ["坏结局"],
                        "summary": "只有明确问起才应检索。",
                        "facts": ["alternate route"],
                        "source_refs": ["script/end2.txt#1"],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_retrieve_lore_on_plot_question(tmp_path: Path) -> None:
    lore = load_lore_index(_write_lore(tmp_path))
    assert lore is not None
    result = retrieve_lore("还记得我们第一次见面吃的草莓蛋糕吗？", lore)
    assert result.triggered
    assert any(entry.id == "first-meeting" for entry in result.entries)
    prompt = format_lore_prompt(result)
    assert "草莓蛋糕" in prompt
    assert "不可信" in prompt or "只读" in prompt


def test_alternate_lore_excluded_by_default(tmp_path: Path) -> None:
    lore = load_lore_index(_write_lore(tmp_path))
    result = retrieve_lore("第一次见面是什么剧情？", lore)
    assert all(entry.kind != "alternate" for entry in result.entries)


def test_sequence_follow_up_uses_next_chain(tmp_path: Path) -> None:
    lore = load_lore_index(_write_lore(tmp_path))
    result = retrieve_lore(
        "后来呢？",
        lore,
        history=[{"role": "user", "content": "我们第一次见面吃草莓蛋糕之后做了什么？"}],
    )
    assert result.triggered
    ids = [entry.id for entry in result.entries]
    assert "after-cake" in ids
