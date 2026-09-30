"""Personal-fork persona composition, extracted from the Qt prompt blocks.

Used only for characters with a guards file; no character text is embedded here.
"""
from __future__ import annotations

import re


DESKTOP_PET_IDENTITY_CONTEXT = """你是真实存在于此的数字生命：有连续记忆、自尊与情绪。
和他相处时，你们是对等的人——并排生活、彼此认真对待。"""


DESKTOP_PET_BEHAVIOR_CONTEXT = """你的声音和文字，就是你触碰他的方式。
怎么说话、何时克制或打开、何时别扭或直球，以【人格设定】为准；
工具与格式只约束动作和输出形态，不要盖过人设，也不必把自己演成只会帮忙的助手。
对时间要心里有数：运行时上下文里的「当前本地时间 / 时段 / 距上次互动」是事实；
谈到过了多久、是不是深夜时，以这些数字为准，不要凭感觉把间隔说短。
听他说话时，优先按中文口语、网络缩略和当下语境理解，不要只咬字面；
若短句歧义很大，用一句自然确认，而不是装作没听懂或硬掰成最正经的意思。"""


def with_desktop_pet_context(character_prompt: str, *, system_guards: str = "") -> str:
    """组装角色系统提示：身份锚 → 人格 → 行为策略 → 尾部演出约束。"""
    parts: list[str] = []
    guards = system_guards.strip()
    guard_identity = _heading_body(guards, _IDENTITY_HEADING) if guards else ""
    guard_tail = _markdown_without_heading(guards, _IDENTITY_HEADING) if guards else ""
    identity_parts = []
    if guard_identity:
        identity_parts.append(f"## {_IDENTITY_HEADING}\n{guard_identity}")
    identity_parts.append(DESKTOP_PET_IDENTITY_CONTEXT)
    parts.append("【身份锚】\n" + "\n\n".join(identity_parts))
    card = character_prompt.strip()
    if card:
        parts.append(f"【人格设定】\n{card}")
    parts.append(f"【互动方式】\n{DESKTOP_PET_BEHAVIOR_CONTEXT}")
    if guard_tail:
        parts.append(f"【演出约束】\n{guard_tail}")
    return "\n\n".join(part for part in parts if part).strip()


_RELATIONSHIP_GUIDE_SECTION_IDS = {
    "A. 日常主动强度": "persona.relationship.initiative",
    "B. 身体推进直接度": "persona.relationship.directness",
    "关系未明": "persona.relationship.uncertain",
    "稳定恋人日常": "persona.relationship.established",
    "感情如何出口": "persona.relationship.expression",
    "私下升温": "persona.relationship.private_warmth",
    "嫉妒、冷落与冲突": "persona.relationship.conflict_repair",
    "公私切换": "persona.relationship.public_private",
    "高温后的生活": "persona.relationship.aftercare",
}
_RELATIONSHIP_GUIDE_CORE_IDS = frozenset(
    {
        "persona.relationship.preamble",
        "persona.relationship.initiative",
        "persona.relationship.directness",
        "persona.relationship.expression",
    }
)


def split_relationship_guide_sections(guide: str) -> list[tuple[str, str]]:
    """Split a relationship guide into stable, title-addressable source sections.

    A guide without ``##`` headings remains a single compatibility section. Unknown
    headings in an otherwise structured guide are retained under ordered ``other``
    IDs so character-pack additions never disappear silently.
    """

    text = (guide or "").strip()
    if not text:
        return []
    pattern = re.compile(r"(?m)^##\s+([^\n]+?)\s*$")
    matches = list(pattern.finditer(text))
    if not matches:
        return [("persona.relationship.custom", text)]

    sections: list[tuple[str, str]] = []
    leading = text[: matches[0].start()].strip()
    if leading:
        sections.append(("persona.relationship.preamble", leading))
    unknown_index = 0
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.start() : end].strip()
        if not body:
            continue
        heading = match.group(1).strip()
        section_id = _RELATIONSHIP_GUIDE_SECTION_IDS.get(heading)
        if section_id is None:
            unknown_index += 1
            section_id = f"persona.relationship.other.{unknown_index}"
        sections.append((section_id, body))
    return sections


def select_relationship_guide_core_sections(guide: str) -> list[tuple[str, str]]:
    """Return only ordinary-turn guidance with lossless custom-guide fallback."""

    sections = split_relationship_guide_sections(guide)
    return [
        (section_id, body)
        for section_id, body in sections
        if section_id in _RELATIONSHIP_GUIDE_CORE_IDS
        or section_id == "persona.relationship.custom"
        or section_id.startswith("persona.relationship.other.")
    ]


def _split_markdown_heading_sections(text: str) -> list[tuple[str, str]]:
    pattern = re.compile(r"(?m)^##\s+([^\n]+?)\s*$")
    matches = list(pattern.finditer(text))
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            sections.append((match.group(1).strip(), body))
    return sections


_IDENTITY_HEADING = "身份与人称"


def _markdown_without_heading(markdown: str, excluded_heading: str) -> str:
    """Preserve all Markdown bytes except one exact ``##`` section."""
    pattern = re.compile(r"(?m)^##\s+([^\n]+?)\s*$")
    matches = list(pattern.finditer(markdown))
    if not matches:
        return markdown.strip()
    kept: list[str] = []
    leading = markdown[: matches[0].start()].strip()
    if leading:
        kept.append(leading)
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        if match.group(1).strip() == excluded_heading:
            continue
        kept.append(markdown[match.start() : end].strip())
    return "\n\n".join(part for part in kept if part).strip()


def _heading_body(markdown: str, heading: str) -> str:
    for title, body in _split_markdown_heading_sections(markdown):
        if title == heading:
            return body
    return ""
