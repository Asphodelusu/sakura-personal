from app.agent.builtin_tools import intimacy_mode_state
from app.agent.prompt_builder import AgentRuntimePromptMixin
from app.config.relationship_initiative import (
    RELATIONSHIP_GUIDE_TOKEN_BUDGET,
    RelationshipInitiativeSettings,
    expression_bias_guidance,
)


class _Runtime(AgentRuntimePromptMixin):
    def __init__(self, guide: str, settings: RelationshipInitiativeSettings) -> None:
        self.system_prompt = "【人格设定】\n她是夜乃桜。"
        self.prompt_patches = []
        self._relationship_guide = guide
        self._relationship_settings = settings.normalized()


def test_enabled_guide_injects_named_section() -> None:
    runtime = _Runtime("安心时可以主动靠近。", RelationshipInitiativeSettings())
    section = runtime._build_relationship_guide_section()
    assert section is not None
    assert section.section_id == "persona.relationship_guide"
    assert section.cache_scope == "static"
    assert section.sensitivity == "private"
    assert section.token_budget == RELATIONSHIP_GUIDE_TOKEN_BUDGET
    assert "安心时可以主动靠近。" in section.body
    assert expression_bias_guidance("natural") in section.body
    sections = runtime._persona_sections()
    ids = [item.section_id for item in sections]
    assert "persona.character" in ids
    assert "persona.relationship_guide" in ids
    assert ids.index("persona.character") < ids.index("persona.relationship_guide")


def test_disabled_or_missing_does_not_inject_negative_limit() -> None:
    off = _Runtime("安心时可以主动靠近。", RelationshipInitiativeSettings(in_turn_enabled=False))
    missing = _Runtime("", RelationshipInitiativeSettings(in_turn_enabled=True))
    assert off._build_relationship_guide_section() is None
    assert missing._build_relationship_guide_section() is None
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
        body = runtime._build_relationship_guide_section().body
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
    runtime._build_relationship_guide_section()
    runtime._persona_sections()
    runtime._build_intimacy_section()
    assert intimacy_mode_state.active is False
    assert intimacy_mode_state.opened_by_keyword is False
