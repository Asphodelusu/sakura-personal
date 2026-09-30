"""Relationship guide and lore from a personal character pack reach the chat context."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from threading import Event

import pytest

from app.agent.lore import load_lore_index
from app.agent.runtime import AgentRuntime
from app.agent.tools import ToolRegistry
from app.config.character_loader import _load_profile, load_relationship_guide
from app.core_host.assistant_adapter import AssistantAdapter
from app.core_host.relationship_settings import load_relationship_initiative_settings
from app.llm.prompts.personal_persona import select_relationship_guide_core_sections
from app.llm.prompts.types import ContextMessage, ContextRequest

GUIDE = (
    "PREAMBLE_TEXT\n\n"
    "## A. \u65e5\u5e38\u4e3b\u52a8\u5f3a\u5ea6\nINITIATIVE_TEXT\n\n"
    "## B. \u8eab\u4f53\u63a8\u8fdb\u76f4\u63a5\u5ea6\nDIRECTNESS_TEXT\n\n"
    "## \u79c1\u4e0b\u5347\u6e29\nPRIVATE_WARMTH_TEXT\n\n"
    "## \u611f\u60c5\u5982\u4f55\u51fa\u53e3\nEXPRESSION_TEXT\n\n"
    "## \u81ea\u5b9a\u4e49\u7ae0\u8282\nCUSTOM_ADDITION_TEXT\n"
)
LORE = {
    "schema_version": 1,
    "character_id": "fixture",
    "canon_context": "\u84dd\u5fc3\u810f",
    "entries": [
        {"id": "e1", "kind": "ending", "title": "\u5b88\u671b\u7684\u7ed3\u5c40",
         "aliases": ["\u5b88\u671b\u7ed3\u5c40"], "keywords": ["\u7ed3\u5c40"],
         "summary": "SYNTHETIC_ENDING_SUMMARY", "facts": ["SYNTHETIC_FACT"],
         "source_refs": ["C:\\private\\script.txt", "ch3"]},
    ],
}


def _package(root: Path, *, guide: str | None = GUIDE, lore: dict | None = LORE, manifest_extra=None) -> Path:
    package = root / "characters" / "fixture"
    package.mkdir(parents=True)
    (package / "card.md").write_text("SYNTHETIC_PERSONA", encoding="utf-8")
    manifest = {"id": "fixture", "display_name": "Fixture", "card": "card.md", "visuals": {"resources": []}}
    manifest.update(manifest_extra or {})
    (package / "character.json").write_text(json.dumps(manifest), encoding="utf-8")
    if guide is not None:
        (package / "relationship_guide.md").write_text(guide, encoding="utf-8")
    if lore is not None:
        (package / "lore").mkdir()
        (package / "lore" / "index.json").write_text(json.dumps(lore, ensure_ascii=False), encoding="utf-8")
    return package


# --- loading ---------------------------------------------------------------

def test_profile_discovers_guide_and_lore_by_convention(tmp_path: Path) -> None:
    profile = _load_profile(_package(tmp_path) / "character.json")

    assert profile.relationship_guide_path is not None
    assert load_relationship_guide(profile.relationship_guide_path) == GUIDE.strip()
    assert profile.lore_index_path is not None


def test_missing_optional_files_do_not_break_loading(tmp_path: Path) -> None:
    package = _package(tmp_path, guide=None, lore=None,
                       manifest_extra={"relationship_guide": "absent.md", "lore": "absent/index.json"})

    profile = _load_profile(package / "character.json")

    assert profile.relationship_guide_path is None
    assert profile.lore_index_path is None
    assert load_relationship_guide(None) == ""


@pytest.mark.parametrize("field", ["relationship_guide", "lore"])
def test_optional_paths_cannot_escape_the_package(tmp_path: Path, field: str) -> None:
    from app.config.character_loader import CharacterConfigError

    (tmp_path / "outside.json").write_text("{}", encoding="utf-8")
    package = _package(tmp_path, guide=None, lore=None, manifest_extra={field: "../../outside.json"})

    with pytest.raises(CharacterConfigError):
        _load_profile(package / "character.json")


# --- relationship guide ------------------------------------------------------

def test_core_selection_keeps_ordinary_turn_sections_and_unknown_additions() -> None:
    bodies = "\n".join(body for _id, body in select_relationship_guide_core_sections(GUIDE))

    for kept in ("PREAMBLE_TEXT", "INITIATIVE_TEXT", "DIRECTNESS_TEXT", "EXPRESSION_TEXT", "CUSTOM_ADDITION_TEXT"):
        assert kept in bodies
    assert "PRIVATE_WARMTH_TEXT" not in bodies


def test_guide_without_headings_is_kept_whole() -> None:
    assert select_relationship_guide_core_sections("PLAIN_GUIDE") == [("persona.relationship.custom", "PLAIN_GUIDE")]


def _guided_runtime(*, enabled: bool = True, bias: str = "natural", guide: str = GUIDE) -> AgentRuntime:
    runtime = AgentRuntime(object(), "SYNTHETIC_PERSONA", character_id="fixture")
    runtime.configure_relationship_guide(guide, in_turn_enabled=enabled, expression_bias=bias)
    return runtime


def test_guide_and_bias_reach_the_persona_after_the_card() -> None:
    prompt = _guided_runtime(bias="restrained")._static_persona_prompt()

    assert prompt.index("SYNTHETIC_PERSONA") < prompt.index("INITIATIVE_TEXT")
    assert "EXPRESSION_TEXT" in prompt and "PRIVATE_WARMTH_TEXT" not in prompt
    assert "restrained" in prompt


def test_disabled_in_turn_initiative_omits_the_guide() -> None:
    prompt = _guided_runtime(enabled=False)._static_persona_prompt()

    assert "INITIATIVE_TEXT" not in prompt


def test_guide_injection_is_bounded() -> None:
    runtime = _guided_runtime(guide="## A. \u65e5\u5e38\u4e3b\u52a8\u5f3a\u5ea6\n" + "\u5b57" * 20000)

    guide_sections = [s for s in runtime._persona_sections() if s.section_id.startswith("persona.relationship")]
    assert guide_sections
    assert sum(len(s.body) for s in guide_sections) < 20000


def test_expression_bias_is_read_from_the_legacy_config(tmp_path: Path) -> None:
    (tmp_path / "data" / "config").mkdir(parents=True)
    (tmp_path / "data" / "config" / "system_config.yaml").write_text(
        "relationship_initiative:\n  expression_bias: expressive\n", encoding="utf-8")

    assert load_relationship_initiative_settings(tmp_path).expression_bias == "expressive"
    assert load_relationship_initiative_settings(tmp_path / "empty").expression_bias == "natural"


# --- lore ------------------------------------------------------------------

def _lore_runtime(tmp_path: Path) -> AgentRuntime:
    runtime = AgentRuntime(object(), "SYNTHETIC_PERSONA", character_id="fixture")
    runtime.configure_lore(load_lore_index(_package(tmp_path, guide=None) / "lore" / "index.json"))
    return runtime


def _lore_fragment(runtime: AgentRuntime, text: str, history=()):
    request = ContextRequest(current_input=text, recent_messages=tuple(history))
    found = [f for f in runtime._session_state_fragments(request) if f.fragment_id == "runtime.character_lore"]
    return found[0] if found else None


def test_lore_question_injects_matching_entry_without_local_paths(tmp_path: Path) -> None:
    fragment = _lore_fragment(_lore_runtime(tmp_path), "\u8fd8\u8bb0\u5f97\u5b88\u671b\u7684\u7ed3\u5c40\u5417\uff1f")

    assert fragment is not None
    assert "SYNTHETIC_ENDING_SUMMARY" in fragment.content
    assert "C:\\private" not in fragment.content and "ch3" in fragment.content
    assert fragment.trust == "untrusted"


def test_ordinary_chat_does_not_touch_lore(tmp_path: Path) -> None:
    assert _lore_fragment(_lore_runtime(tmp_path), "\u4eca\u5929\u5929\u6c14\u4e0d\u9519") is None


def test_follow_up_uses_the_previous_user_question(tmp_path: Path) -> None:
    history = [ContextMessage(role="user", content="\u5b88\u671b\u7684\u7ed3\u5c40\u662f\u600e\u6837\u7684\uff1f")]

    assert _lore_fragment(_lore_runtime(tmp_path), "\u7136\u540e\u5462\uff1f", history) is not None


# --- Core wiring -------------------------------------------------------------

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "runtime_v2" / "wp_3_01" / "ready"


@pytest.mark.parametrize("in_turn", [True, False])
def test_adapter_wires_guide_and_lore_from_the_character_pack(tmp_path: Path, in_turn: bool) -> None:
    root = tmp_path / "root"
    shutil.copytree(FIXTURE_ROOT, root)
    package = root / "characters" / "sakura"
    (package / "relationship_guide.md").write_text(GUIDE, encoding="utf-8")
    (package / "lore").mkdir(exist_ok=True)
    (package / "lore" / "index.json").write_text(json.dumps(LORE, ensure_ascii=False), encoding="utf-8")
    if not in_turn:
        (root / "config" / "system_config.yaml").write_text(
            "config_version: 1\nrelationship_initiative:\n  in_turn_enabled: false\n", encoding="utf-8")

    readiness = AssistantAdapter(root, tool_registry=ToolRegistry(), mcp_provider=None).initialize(Event())
    runtime = readiness.session.runtime
    try:
        assert ("INITIATIVE_TEXT" in runtime._static_persona_prompt()) is in_turn
        assert _lore_fragment(runtime, "\u8fd8\u8bb0\u5f97\u5b88\u671b\u7684\u7ed3\u5c40\u5417\uff1f") is not None
    finally:
        runtime.close()
