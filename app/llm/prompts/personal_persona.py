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


_INTIMACY_FOCUS_OVERLAY = """【当下专注】
此刻注意力在眼前的触感、气息与对方的反应上。
保留你是谁、你们是什么关系就够了；日常习惯、兴趣清单、长篇设定不必主动展开，也不要突然切回日常旁白或复述人设。"""

# 亲密模式按 Markdown 段保留人格骨架；预算优先给判断、关系边界与反 OOC。
_INTIMACY_PERSONA_CHAR_BUDGET = 1400
_INTIMACY_PERSONA_KEEP_HEADINGS = (
    "核心",
    "能动性与判断",
    "关系中的她",
    "语言与节奏",
    "不要写成",
)
_INTIMACY_PERSONA_SAFETY_TERMS = (
    "意愿",
    "迟疑",
    "沉默",
    "退开",
    "退出权",
    "边界",
    "没有选择",
    "服从",
    "顺从",
    "判断",
    "重复",
)


def _split_labeled_prompt_sections(text: str) -> list[tuple[str | None, str]]:
    """按【标题】切开系统提示；无标题的前缀 title=None。"""
    pattern = re.compile(r"(?m)^【([^】]+)】\s*$")
    matches = list(pattern.finditer(text))
    if not matches:
        return [(None, text.strip())] if text.strip() else []
    sections: list[tuple[str | None, str]] = []
    leading = text[: matches[0].start()].strip()
    if leading:
        sections.append((None, leading))
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append((match.group(1).strip(), text[start:end].strip()))
    return sections


def _trim_keep_start(text: str, max_chars: int) -> str:
    text = text.strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    cut = text[:max_chars].rstrip()
    # 尽量在段落边界收束，避免半句设定
    for sep in ("\n\n", "\n", "。", "；", "，"):
        pos = cut.rfind(sep)
        if pos >= max_chars // 2:
            cut = cut[: pos + len(sep)].rstrip()
            break
    return f"{cut}\n…（亲密中从简）"


def _compact_persona_section(body: str, budget: int) -> str:
    paragraphs = [
        part.strip()
        for part in re.split(r"\n\s*\n|(?=^- )", body, flags=re.MULTILINE)
        if part.strip()
    ]
    if not paragraphs:
        return ""
    safety = [part for part in paragraphs if any(term in part for term in _INTIMACY_PERSONA_SAFETY_TERMS)]
    candidates = [*safety, *paragraphs]
    chosen: list[str] = []
    for part in candidates:
        if part in chosen:
            continue
        if len("\n\n".join([*chosen, part])) <= budget:
            chosen.append(part)
        if len("\n\n".join(chosen)) >= budget:
            break
    if chosen:
        return "\n\n".join(part for part in paragraphs if part in chosen)
    return _trim_keep_start(candidates[0], max(24, budget)).removesuffix("\n…（亲密中从简）")


def _select_intimacy_persona_sections(markdown: str, max_chars: int) -> str:
    sections = _split_markdown_heading_sections(markdown)
    if not sections:
        return _trim_keep_start(markdown, min(max_chars, 720))
    exact = {title: body for title, body in sections}
    selected: list[tuple[str, str]] = [
        (title, exact[title]) for title in _INTIMACY_PERSONA_KEEP_HEADINGS if title in exact
    ]
    if len(selected) < len(_INTIMACY_PERSONA_KEEP_HEADINGS):
        selected_titles = {title for title, _body in selected}
        selected.extend(
            (title, body)
            for title, body in sections
            if title not in selected_titles and any(term in body for term in _INTIMACY_PERSONA_SAFETY_TERMS)
        )
    if not selected:
        return _trim_keep_start(markdown, max_chars)
    heading_cost = sum(len(f"## {title}\n") for title, _body in selected)
    body_budget = max(24 * len(selected), max_chars - heading_cost)
    per_section = max(24, body_budget // len(selected))
    return "\n\n".join(
        f"## {title}\n{compact}"
        for title, body in selected
        if (compact := _compact_persona_section(body, per_section))
    )


def soften_character_card_for_intimacy(
    system_prompt: str,
    *,
    max_persona_chars: int = _INTIMACY_PERSONA_CHAR_BUDGET,
) -> str:
    """亲密模式弱化人格卡：保留演出约束与短身份锚，压缩日常人设细节。"""
    text = system_prompt.strip()
    if not text:
        return _INTIMACY_FOCUS_OVERLAY
    parts: list[str] = []
    guards_tail = ""
    for title, body in _split_labeled_prompt_sections(text):
        if title == "人格设定":
            trimmed = _select_intimacy_persona_sections(body, max_persona_chars)
            if trimmed:
                parts.append(f"【人格设定】\n{trimmed}")
            continue
        if title == "演出约束":
            guards_tail = body.strip()
            continue
        if title == "互动方式":
            if body:
                parts.append(f"【互动方式】\n{body}")
            continue
        if title is None:
            trimmed = _trim_keep_start(body, max_persona_chars)
            if trimmed:
                parts.append(trimmed)
            continue
        # 未知分段：偏长则截断，避免再塞回大段设定
        trimmed = _trim_keep_start(body, max_persona_chars) if len(body) > max_persona_chars else body
        if trimmed:
            parts.append(f"【{title}】\n{trimmed}")
    parts.append(_INTIMACY_FOCUS_OVERLAY)
    if guards_tail:
        parts.append(f"【演出约束】\n{guards_tail}")
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
