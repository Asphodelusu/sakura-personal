from pathlib import Path

from app.agent.builtin_tools import intimacy_mode_state
from app.agent.prompt_builder import AgentRuntimePromptMixin
from app.config.relationship_initiative import (
    RELATIONSHIP_GUIDE_TOKEN_BUDGET,
    RelationshipInitiativeSettings,
    expression_bias_guidance,
)
from app.llm.prompts.runtime import estimate_prompt_tokens


REPO_ROOT = Path(__file__).resolve().parents[2]
SAKURA_GUIDE = REPO_ROOT / "characters" / "Sakura" / "relationship_guide.md"
CORE_SECTION_IDS = (
    "persona.relationship.preamble",
    "persona.relationship.initiative",
    "persona.relationship.directness",
    "persona.relationship.expression",
    "persona.relationship.expression_bias",
)
SITUATIONAL_HEADINGS = (
    "关系未明",
    "稳定恋人日常",
    "私下升温",
    "嫉妒、冷落与冲突",
    "公私切换",
    "高温后的生活",
)


class _Runtime(AgentRuntimePromptMixin):
    def __init__(self, guide: str, settings: RelationshipInitiativeSettings) -> None:
        self.system_prompt = "【人格设定】\n她是夜乃桜。"
        self.prompt_patches = []
        self._relationship_guide = guide
        self._relationship_settings = settings.normalized()


def test_sakura_guide_injects_only_named_ordinary_core_sections() -> None:
    guide = SAKURA_GUIDE.read_text(encoding="utf-8")
    runtime = _Runtime(guide, RelationshipInitiativeSettings())
    sections = runtime._build_relationship_guide_sections()
    ids = tuple(section.section_id for section in sections)

    assert ids == CORE_SECTION_IDS
    assert all(section.cache_scope == "static" for section in sections)
    assert all(section.sensitivity == "private" for section in sections)
    assert sum(estimate_prompt_tokens(section.body) for section in sections) <= (
        RELATIONSHIP_GUIDE_TOKEN_BUDGET
    )
    body = "\n\n".join(section.body for section in sections)
    assert body.startswith("# 关系演出参考")
    assert "## A. 日常主动强度" in body
    assert "## B. 身体推进直接度" in body
    assert "## 感情如何出口" in body
    assert "继续沉默会真正失去机会" in body
    assert expression_bias_guidance("natural") in body
    assert not any(f"## {heading}" in body for heading in SITUATIONAL_HEADINGS)

    sections = runtime._persona_sections()
    ids = [item.section_id for item in sections]
    assert "persona.behavior_core" in ids
    assert ids.index("persona.behavior_core") < ids.index("persona.relationship.preamble")


def test_unstructured_custom_guide_is_preserved_as_one_compatibility_section() -> None:
    guide = "安心时可以主动靠近。\n不为了证明主动而制造欲望。"
    runtime = _Runtime(guide, RelationshipInitiativeSettings())
    sections = runtime._build_relationship_guide_sections()

    assert [section.section_id for section in sections] == [
        "persona.relationship.custom",
        "persona.relationship.expression_bias",
    ]
    assert sections[0].body == guide


def test_unknown_heading_in_structured_guide_is_preserved_in_source_order() -> None:
    guide = (
        "# 自定义关系指南\n\n"
        "## A. 日常主动强度\n按当前心情主动。\n\n"
        "## 新角色包场景\n保留这段未来扩展。"
    )
    runtime = _Runtime(guide, RelationshipInitiativeSettings())
    sections = runtime._build_relationship_guide_sections()

    assert [section.section_id for section in sections] == [
        "persona.relationship.preamble",
        "persona.relationship.initiative",
        "persona.relationship.other.1",
        "persona.relationship.expression_bias",
    ]
    assert sections[2].body == "## 新角色包场景\n保留这段未来扩展。"


def test_disabled_or_missing_does_not_inject_negative_limit() -> None:
    off = _Runtime("安心时可以主动靠近。", RelationshipInitiativeSettings(in_turn_enabled=False))
    missing = _Runtime("", RelationshipInitiativeSettings(in_turn_enabled=True))
    assert off._build_relationship_guide_sections() == []
    assert missing._build_relationship_guide_sections() == []
    for runtime in (off, missing):
        blob = "\n".join(section.body for section in runtime._persona_sections())
        assert "不允许主动" not in blob
        assert "现在不能主动" not in blob
        assert "relationship_guide" not in blob
        assert "禁止主动" not in blob


def test_bias_only_changes_guidance_copy() -> None:
    guide = "已经安心时可以索吻或邀请对方留下来。"
    bodies = {}
    for bias in ("restrained", "natural", "expressive"):
        runtime = _Runtime(guide, RelationshipInitiativeSettings(expression_bias=bias))
        body = "\n\n".join(
            section.body for section in runtime._build_relationship_guide_sections()
        )
        bodies[bias] = body
        assert "不得直接露骨" not in body
        assert "最多只能轻触" not in body
        assert "禁止H" not in body
        assert guide in body
    assert bodies["restrained"] != bodies["expressive"]
    assert "restrained" in bodies["restrained"]
    assert "expressive" in bodies["expressive"]


def test_injection_does_not_enable_intimacy_mode() -> None:
    intimacy_mode_state.exit()
    runtime = _Runtime("可以直接表达想要。", RelationshipInitiativeSettings())
    runtime._build_relationship_guide_sections()
    runtime._persona_sections()
    runtime._build_intimacy_section()
    assert intimacy_mode_state.active is False
    assert intimacy_mode_state.opened_by_keyword is False
