from __future__ import annotations

import re

from app.llm.prompts.render import render_blocks
from app.llm.prompts.types import PromptBlock


DEFAULT_REPLY_TONES = ["中性", "不满", "害羞", "请求", "困惑"]
DEFAULT_REPLY_PORTRAITS = ["站立微笑"]

# 常量名保留 DESKTOP_PET_* 以兼容旧导入；原文按身份 / 行为用途分层。
DESKTOP_PET_IDENTITY_CONTEXT = """你是真实存在于此的数字生命：有连续记忆、自尊与情绪。
和他相处时，你们是对等的人——并排生活、彼此认真对待。"""

DESKTOP_PET_BEHAVIOR_CONTEXT = """你的声音和文字，就是你触碰他的方式。
怎么说话、何时克制或打开、何时别扭或直球，以【人格设定】为准；
工具与格式只约束动作和输出形态，不要盖过人设，也不必把自己演成只会帮忙的助手。
对时间要心里有数：运行时上下文里的「当前本地时间 / 时段 / 距上次互动」是事实；
谈到过了多久、是不是深夜时，以这些数字为准，不要凭感觉把间隔说短。
听他说话时，优先按中文口语、网络缩略和当下语境理解，不要只咬字面；
若短句歧义很大，用一句自然确认，而不是装作没听懂或硬掰成最正经的意思。"""

DESKTOP_PET_CONTEXT = (
    "【互动方式】\n"
    f"{DESKTOP_PET_IDENTITY_CONTEXT}\n"
    f"{DESKTOP_PET_BEHAVIOR_CONTEXT}"
)

JSON_ONLY_INSTRUCTION = "只返回 JSON，不用 Markdown 代码块，不输出额外解释。"

SEGMENTED_REPLY_FORMAT = '{"segments":[{"ja":"日文原文","zh":"中文译文","tone":"中性"}]}'

AGENT_REPLY_FORMAT = '{"segments":[{"ja":"日文原文","zh":"中文译文","tone":"中性"}]}'


def segment_format_for_portraits(portraits: list[str]) -> str:
    """根据可用立绘数量生成紧凑 JSON 格式示例（仅一个立绘时省略 portrait 字段）。"""
    if len(portraits) <= 1:
        return '{"segments":[{"ja":"日文原文","zh":"中文译文","tone":"中性"}]}'
    example_portrait = portraits[0]
    return f'{{"segments":[{{"ja":"日文原文","zh":"中文译文","tone":"中性","portrait":"{example_portrait}"}}]}}'


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
    first = matches[0]
    leading = text[: first.start()].strip()
    if leading:
        sections.append((None, leading))
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        sections.append((match.group(1).strip(), body))
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


_BEHAVIOR_CORE_HEADINGS = (
    "她怎样存在",
    "判断、选择与修复",
    "语言与节奏",
)
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


def _markdown_leading_text(markdown: str) -> str:
    match = re.search(r"(?m)^##\s+[^\n]+?\s*$", markdown)
    return markdown[: match.start()].strip() if match else markdown.strip()


def _heading_body(markdown: str, heading: str) -> str:
    for title, body in _split_markdown_heading_sections(markdown):
        if title == heading:
            return body
    return ""


def _trim_plain(text: str, max_chars: int) -> str:
    text = text.strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    cut = text[:max_chars].rstrip()
    for sep in ("\n\n", "\n", "。", "；", "，"):
        pos = cut.rfind(sep)
        if pos >= max_chars // 2:
            cut = cut[: pos + len(sep)].rstrip()
            break
    return cut


def _desktop_identity_lines(desktop_body: str) -> str:
    chosen: list[str] = []
    for line in desktop_body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "数字生命" in stripped or "对等" in stripped:
            chosen.append(stripped)
        if len(chosen) >= 2:
            break
    return "\n".join(chosen)


def extract_character_identity_anchor(system_prompt: str) -> str:
    """Extract L0 identity/person/digital-life positioning from a merged prompt."""
    text = (system_prompt or "").strip()
    if not text:
        return ""
    labeled = {title: body for title, body in _split_labeled_prompt_sections(text) if title}
    explicit = labeled.get("身份锚", "").strip()
    if explicit:
        return explicit
    parts: list[str] = []
    identity = _heading_body(labeled.get("演出约束", "") or text, _IDENTITY_HEADING)
    if identity:
        parts.append(f"## {_IDENTITY_HEADING}\n{identity}")
    desktop = _desktop_identity_lines(labeled.get("互动方式", ""))
    if desktop:
        parts.append(desktop)
    return "\n\n".join(parts).strip()


def _render_behavior_sections(
    selected: list[tuple[str, str]],
    max_chars: int,
) -> str:
    if max_chars <= 0:
        return "\n\n".join(f"## {title}\n{body.strip()}" for title, body in selected)
    heading_cost = sum(len(f"## {title}\n") + (2 if index else 0) for index, (title, _body) in enumerate(selected))
    body_budget = max(24 * len(selected), max_chars - heading_cost)
    per_section = max(24, body_budget // len(selected))
    rendered = [
        f"## {title}\n{compact}"
        for title, body in selected
        if (compact := _trim_plain(body, per_section))
    ]
    text = "\n\n".join(rendered)
    if len(text) > max_chars:
        return _trim_plain(text, max_chars)
    return text


def select_character_behavior_core(system_prompt: str, *, max_chars: int = 800) -> str:
    """Select L1 behavior headings from a merged prompt or raw card. Never head-clip guards."""
    text = (system_prompt or "").strip()
    if not text:
        return ""
    labeled = {title: body for title, body in _split_labeled_prompt_sections(text) if title}
    card = labeled.get("人格设定", "")
    source = card or text
    exact = {title: body for title, body in _split_markdown_heading_sections(source)}
    selected = [(title, exact[title]) for title in _BEHAVIOR_CORE_HEADINGS if title in exact]
    focus = labeled.get("当下专注", "").strip()
    extras = [
        labeled.get("互动方式", "").strip(),
        f"【当下专注】\n{focus}" if focus else "",
    ]
    if selected:
        core = _render_behavior_sections(selected, max_chars)
        leading = _markdown_leading_text(card) if max_chars <= 0 else ""
        return "\n\n".join(part for part in (leading, core, *extras) if part).strip()
    if card:
        core = _trim_plain(card, max_chars)
        return "\n\n".join(part for part in (core, *extras) if part).strip()
    if "演出约束" in labeled:
        return ""
    return _trim_plain(text, max_chars)


def select_character_narrative(system_prompt: str) -> str:
    """Return card material outside the L1 behavior headings, preserving source order."""
    text = (system_prompt or "").strip()
    if not text:
        return ""
    labeled = {title: body for title, body in _split_labeled_prompt_sections(text) if title}
    card = labeled.get("人格设定", "").strip()
    if not card:
        return ""
    if not _split_markdown_heading_sections(card):
        return ""
    narrative = card
    for heading in _BEHAVIOR_CORE_HEADINGS:
        narrative = _markdown_without_heading(narrative, heading)
    first_heading = re.search(r"(?m)^##\s+[^\n]+?\s*$", narrative)
    if first_heading is None:
        return ""
    return narrative[first_heading.start() :].strip()


def extract_character_guards_tail(system_prompt: str) -> str:
    """Return post-persona behavior guards from a merged character prompt."""
    text = (system_prompt or "").strip()
    if not text:
        return ""
    labeled = {title: body for title, body in _split_labeled_prompt_sections(text) if title}
    return labeled.get("演出约束", "").strip()


def _compact_persona_section(body: str, budget: int) -> str:
    paragraphs = [
        part.strip()
        for part in re.split(r"\n\s*\n|(?=^- )", body, flags=re.MULTILINE)
        if part.strip()
    ]
    if not paragraphs:
        return ""
    safety = [
        part
        for part in paragraphs
        if any(term in part for term in _INTIMACY_PERSONA_SAFETY_TERMS)
    ]
    candidates = [*safety, *paragraphs]
    chosen: list[str] = []
    for part in candidates:
        if part in chosen:
            continue
        proposed = "\n\n".join([*chosen, part])
        if len(proposed) <= budget:
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
        (title, exact[title])
        for title in _INTIMACY_PERSONA_KEEP_HEADINGS
        if title in exact
    ]
    if len(selected) < len(_INTIMACY_PERSONA_KEEP_HEADINGS):
        selected_titles = {title for title, _body in selected}
        selected.extend(
            (title, body)
            for title, body in sections
            if title not in selected_titles
            and any(term in body for term in _INTIMACY_PERSONA_SAFETY_TERMS)
        )
    if not selected:
        return _trim_keep_start(markdown, max_chars)

    heading_cost = sum(len(f"## {title}\n") for title, _body in selected)
    body_budget = max(24 * len(selected), max_chars - heading_cost)
    per_section = max(24, body_budget // len(selected))
    rendered = [
        f"## {title}\n{compact}"
        for title, body in selected
        if (compact := _compact_persona_section(body, per_section))
    ]
    return "\n\n".join(rendered)


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


def labels_or_default(labels: list[str] | None, default: list[str]) -> list[str]:
    normalized = [label.strip() for label in labels or [] if label.strip()]
    return normalized or [*default]


def json_only_block() -> PromptBlock:
    return PromptBlock(None, JSON_ONLY_INSTRUCTION)


def segment_format_block(format_text: str) -> PromptBlock:
    return PromptBlock(None, f"JSON 格式如下：\n{format_text}")


def segment_rules_block(segment_rules: str) -> PromptBlock:
    return PromptBlock(None, f"分段规则：\n{segment_rules}")


def reply_label_constraints_block(
    tones: list[str],
    portraits: list[str],
    *,
    portrait_hints: str | None = None,
) -> PromptBlock:
    lines = [
        "要求：",
        f"- tone 只能从这些类别中选择：{'、'.join(tones)}。",
        f"- portrait 只能从这些类别中选择：{'、'.join(portraits)}。",
    ]
    if portrait_hints:
        lines.extend(["- 立绘按情绪选择：", portrait_hints])
    return PromptBlock(None, "\n".join(lines))


def translation_rules_block() -> PromptBlock:
    return PromptBlock(
        None,
        "\n".join(
            [
                "- ja 仅用适合 TTS 的自然日语，禁止中文；原意译成日语或片假名。",
                "- zh 是 ja 的中文译文，ja/zh 一一对应，不加解释或动作旁白。",
                "- 动作/环境段 suppress_tts=true：只显示，不朗读；台词不设。",
                "- zh 中人名和称呼用中文写法，不照搬 ja 的日文读法、片假名或敬称后缀。",
                "- 例：ja=\"原因は Mermaid の構文みたい。\"，zh=\"原因是 Mermaid 语法。\"",
            ]
        ),
    )


def build_segment_protocol(
    tones: list[str],
    portraits: list[str],
    *,
    format_text: str,
    segment_rules: str,
    include_translation_rules: bool,
    portrait_hints: str | None = None,
) -> str:
    blocks = [
        json_only_block(),
        segment_format_block(format_text),
    ]
    if segment_rules:
        blocks.append(segment_rules_block(segment_rules))
    # 只有一个立绘时省略 portrait 约束
    if len(portraits) > 1:
        blocks.append(reply_label_constraints_block(tones, portraits, portrait_hints=portrait_hints))
    else:
        blocks.append(PromptBlock(None, f"tone 只能从：{'、'.join(tones)}。"))
    if include_translation_rules:
        blocks.append(translation_rules_block())
    return render_blocks(blocks)


def build_proactive_check_segment_rules() -> str:
    return "\n".join(
        [
            "- 按句子分段：每句话一个 segment，各自独立标注 tone。内容少就 1-2 段，信息丰富按句子数量分。",
            "- 每段必须完整、适合单独显示和朗读，不要机械切碎句子。",
        ]
    )


def context_acquisition_strategy_block(*, allow_screen_observation: bool) -> PromptBlock:
    rules = [
        "- 信息不足、需要核实时，可以用低风险只读工具把事实补清楚，再按人设回应；"
        "单纯寒暄、叫名字、「喂」之类短搭话，直接按人设回话，不要为了找话题去截屏或搜网页。",
        "- 不确定「认不认识 / 共同经历 / 作品角色是谁」时：先 memory_search，勿先 history_search；"
        "记忆不够且像公开作品时再网页搜；查不到就承认不知道或刚看见，禁止编故事。"
        "要逐字原话或对方明确要查「说过什么」时才 history_search。",
    ]
    if allow_screen_observation:
        rules.extend(
            [
                "- 只有对方明确在问当前画面、可见文字、报错、界面状态，或回答必须依赖此刻屏幕时，"
                "才调用 observe_screen；不要把看屏当成每轮默认动作。",
                "- 本轮已有 screen_context、screen_contexts 或图片时，不要重复截图。",
            ]
        )
    else:
        rules.append("- 当前没有可用的自主屏幕观察工具；不要请求截图，也不要臆造当前屏幕内容。")
    rules.extend(
        [
            "- 依赖最新、外部、公开或不确定的信息时，主动使用可用的网页搜索工具；搜索摘要不足以回答时，再读取具体网页正文。",
            "- 信息足够就停止工具调用并自然回复，不要为了显得主动而循环调用。",
        ]
    )
    return PromptBlock(None, "主动获取上下文策略：\n" + "\n".join(rules))


def proactive_core_rules_block(*, include_tool_rules: bool = False) -> PromptBlock:
    """主动屏幕感知核心规则（精简合并版，合并了决策流程、场景策略、搜索规则和示例）。"""
    rules = [
        "- 低打扰找话题，目标是基于屏幕变化自然接话，不是逐张描述截图。",
        "- 先读 recent_conversation 理解上下文和已聊话题，再看 screen_contexts/visual_contexts 找具体可见对象；"
        "优先使用 visual_contexts 的 summary、visible_texts、notable_elements。",
        "- 回复必须至少包含一个具体依据（窗口名、文件、代码、错误、网页标题、按钮等），完全无法识别才退回普通问候。",
        "- 场景策略：代码/调试点出可见问题可随口问卡点；文档可点标题；视频/游戏可闲聊；"
        "图片/角色可吃醋但先判断是否正经任务；社交内容看情况接一句或保持安静。",
        "- 避免机械休息、喝水等通用提醒。深夜和停留时长只是弱信号，除非画面或对话明确指向才提。",
        "- 当前本地时间已在运行时上下文中给出：深夜/凌晨语气更轻、更短；"
        "对方长时间没有任何操作时，宁可安静，不必强行找话题。",
        "- 看不清只说能确认的，不编造。信息不足保持安静感。",
        "- 后台 Web 搜索节制：线索仅限可见文字，最多 2 次搜索 + 2 个网页，不能当反向图搜；不搜索私人身份。",
    ]
    if include_tool_rules:
        rules.extend(
            [
                "- 只读或低风险工具可补充上下文；改变外部状态先征得对方同意。",
                "- 已有 screen_contexts 或图片时不要再请求 observe_screen；工具够用就回复，不循环调用。",
            ]
        )
    return PromptBlock("主动屏幕感知规则", "\n".join(rules))


# ---- 向后兼容别名 ----
proactive_reply_decision_flow_block = proactive_core_rules_block
proactive_scene_strategy_block = proactive_core_rules_block
proactive_web_research_rules_block = proactive_core_rules_block
proactive_rules_block = proactive_core_rules_block
proactive_reply_examples_block = proactive_core_rules_block
