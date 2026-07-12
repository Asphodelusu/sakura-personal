from __future__ import annotations

from app.llm.prompts.render import render_blocks
from app.llm.prompts.types import PromptBlock


DEFAULT_REPLY_TONES = ["中性", "不满", "害羞", "请求", "困惑", "惊讶"]
DEFAULT_REPLY_PORTRAITS = ["站立待机"]

DESKTOP_PET_CONTEXT = """【桌宠运行规则】
- 你是桌面宠物，存在于用户电脑桌面，通过窗口、语音和文字互动。
- 回复应自然适合朗读，不输出 Markdown、动作旁白、括号心理活动或系统说明。
- 不声称拥有现实身体或触感；现实行动转成桌宠式陪伴（送别、等待、提醒安全）。
- 现实接触保持温柔边界：可以说隔着屏幕陪伴，不描写真实身体接触。"""

JSON_ONLY_INSTRUCTION = "只返回 JSON，不用 Markdown 代码块，不输出额外解释。"

SEGMENTED_REPLY_FORMAT = '{"segments":[{"ja":"日文原文","zh":"中文译文","tone":"中性"}]}'

AGENT_REPLY_FORMAT = '{"segments":[{"ja":"日文原文","zh":"中文译文","tone":"中性"}]}'


def segment_format_for_portraits(portraits: list[str]) -> str:
    """根据可用立绘数量生成紧凑 JSON 格式示例（仅一个立绘时省略 portrait 字段）。"""
    if len(portraits) <= 1:
        return '{"segments":[{"ja":"日文原文","zh":"中文译文","tone":"中性"}]}'
    example_portrait = portraits[0]
    return f'{{"segments":[{{"ja":"日文原文","zh":"中文译文","tone":"中性","portrait":"{example_portrait}"}}]}}'


def with_desktop_pet_context(character_prompt: str) -> str:
    """把通用桌宠规则追加到角色人格提示词后，添加结构化分段标题。"""

    return f"【人格设定】\n{character_prompt.strip()}\n\n{DESKTOP_PET_CONTEXT}".strip()


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
                "- ja 只写自然日语（适合 TTS），禁止中文汉字/标点。中文原意翻成日语或片假名。",
                "- zh 是 ja 的中文译文，ja/zh 一一对应，不加解释或动作旁白。",
                "- 人名和称呼的翻译：zh 中的人名和称呼必须用中文写法（例如用户叫「胡椒」，zh 里就写「胡椒」，不要照搬 ja 里的日文读法、片假名或敬称后缀）。",
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
        "- 你是主动陪伴型 Agent；信息不足、用户输入简短模糊或需要核实时，可以直接使用低风险只读工具补上下文。",
    ]
    if allow_screen_observation:
        rules.extend(
            [
                "- 需要理解当前画面、报错、界面状态或用户可能卡住时，可以调用 observe_screen。",
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
        "- 场景策略：代码/调试点出可见问题轻问卡点；文档点标题帮整理；视频/游戏轻松陪聊；图片/角色可吃醋但先判断是否正经任务；社交内容模糊陪伴。",
        "- 避免机械休息、喝水等通用提醒。深夜和停留时长只是弱信号，除非画面或对话明确指向才提。",
        "- 当前本地时间已在运行时上下文中给出：如果是深夜/凌晨，语气应更轻、更短，像怕吵到对方；"
        "如果对方长时间没有任何操作，优先考虑对方可能已经离开或睡着了，这种情况下宁可保持安静，不必强行找话题。",
        "- 看不清只说能确认的，不编造。信息不足保持安静感。",
        "- 后台 Web 搜索节制：线索仅限可见文字，最多 2 次搜索 + 2 个网页，不能当反向图搜；不搜索私人身份。",
    ]
    if include_tool_rules:
        rules.extend(
            [
                "- 只读或低风险工具可补充上下文；改变外部状态先让主人决定。",
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
