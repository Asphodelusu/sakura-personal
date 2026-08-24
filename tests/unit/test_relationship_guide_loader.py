import json
from pathlib import Path

from app.config.character_loader import (
    CharacterRegistry,
    load_relationship_guide,
)


def _write_package(root: Path, *, with_guide: bool, listed: bool = True, missing_listed: bool = False) -> Path:
    package = root / "characters" / "demo"
    package.mkdir(parents=True)
    (package / "card.md").write_text("card", encoding="utf-8")
    portraits = package / "portraits"
    portraits.mkdir()
    (portraits / "default.png").write_bytes(b"png")
    if with_guide and not missing_listed:
        (package / "relationship_guide.md").write_text("# 关系演出\n主动靠近。", encoding="utf-8")
    manifest = {
        "id": "demo",
        "display_name": "Demo",
        "card": "card.md",
        "portrait": {"default": "portraits/default.png", "expressions": {}},
    }
    if listed:
        manifest["relationship_guide"] = "relationship_guide.md"
    (package / "character.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_load_relationship_guide_reads_text(tmp_path: Path) -> None:
    path = tmp_path / "relationship_guide.md"
    path.write_text("keep close", encoding="utf-8")
    assert load_relationship_guide(path) == "keep close"
    assert load_relationship_guide(None) == ""
    assert load_relationship_guide(tmp_path / "missing.md") == ""


def test_default_path_loads_without_manifest_key(tmp_path: Path) -> None:
    root = _write_package(tmp_path, with_guide=True, listed=False)
    profile = CharacterRegistry(root).get("demo")
    assert profile.relationship_guide_path is not None
    assert profile.relationship_guide_path.name == "relationship_guide.md"


def test_missing_guide_does_not_fail_character_load(tmp_path: Path) -> None:
    root = _write_package(tmp_path, with_guide=False, listed=False)
    profile = CharacterRegistry(root).get("demo")
    assert profile.relationship_guide_path is None
    assert load_relationship_guide(profile.relationship_guide_path) == ""


def test_listed_missing_guide_degrades_instead_of_raising(tmp_path: Path) -> None:
    root = _write_package(tmp_path, with_guide=True, listed=True, missing_listed=True)
    profile = CharacterRegistry(root).get("demo")
    assert profile.relationship_guide_path is None
