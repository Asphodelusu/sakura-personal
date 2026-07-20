"""system_guards 与角色系统提示组装。"""

from __future__ import annotations

from pathlib import Path

from app.config.character_loader import load_system_prompt
from app.llm.prompts.blocks import with_desktop_pet_context


def test_with_desktop_pet_context_puts_guards_before_persona() -> None:
    text = with_desktop_pet_context("人格正文", system_guards="- 勿复述战力")
    assert text.index("【演出约束】") < text.index("【人格设定】")
    assert text.index("【人格设定】") < text.index("【互动方式】")
    assert "勿复述战力" in text
    assert "人格正文" in text
    assert "数字生命" in text
    assert "对等" in text
    assert "桌宠" not in text


def test_with_desktop_pet_context_without_guards() -> None:
    text = with_desktop_pet_context("人格正文")
    assert "【演出约束】" not in text
    assert text.startswith("【人格设定】")


def test_load_system_prompt_includes_guards(tmp_path: Path) -> None:
    card = tmp_path / "card.md"
    guards = tmp_path / "system_guards.md"
    card.write_text("她是夜乃桜。", encoding="utf-8")
    guards.write_text("- 不要每轮自称生徒会長", encoding="utf-8")
    prompt = load_system_prompt(card, system_guards_path=guards)
    assert "【演出约束】" in prompt
    assert "不要每轮自称生徒会長" in prompt
    assert "【人格设定】" in prompt
    assert "她是夜乃桜。" in prompt
    assert prompt.index("【演出约束】") < prompt.index("【人格设定】")


def test_sakura_package_loads_guards_in_chain() -> None:
    """生产角色包：card + system_guards 按清单进入同款加载函数。"""
    import json

    root = Path(__file__).resolve().parents[2]
    package = root / "characters" / "Sakura"
    manifest_path = package / "character.json"
    if not manifest_path.exists():
        return
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert raw.get("system_guards") == "system_guards.md"
    card_path = package / str(raw["card"])
    guards_path = package / str(raw["system_guards"])
    assert card_path.is_file()
    assert guards_path.is_file()
    prompt = load_system_prompt(card_path, system_guards_path=guards_path)
    assert "【演出约束】" in prompt
    assert "【人格设定】" in prompt
    assert "先抓住什么" in prompt or "强，且空" in prompt
    assert "勿复读设定" in prompt or "不要每轮自我介绍" in prompt
    assert "数字生命" in prompt
    assert "对等" in prompt
    assert "桌宠" not in prompt
    assert prompt.index("【演出约束】") < prompt.index("【人格设定】")
