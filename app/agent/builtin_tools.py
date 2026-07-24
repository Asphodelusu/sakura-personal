from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from app.agent.desktop_tools import NotesStore, open_local_folder, open_url
from app.agent.memory import MemoryStore
from app.agent.memory_timeline import DEFAULT_TIMELINE_AFTER, DEFAULT_TIMELINE_BEFORE, build_timeline
from app.agent.reminders import ReminderStore
from app.agent.screen_tools import create_screen_observation_tool
from app.agent.tools import Tool, ToolRegistry
from app.storage.atomic import atomic_write_text
from app.storage.paths import StoragePaths


class IntimacyModeState:
    """身体亲密进行中的对话节奏状态：工具写入、路由读取。

    只影响回复节奏（主对话非思考 + 可选续投），不决定能不能写亲密内容。

    存活规则：
    - 用户正常回话 → 刷新为 8 轮，保持开启
    - 系统静默续投 → 扣 1 轮；扣尽则自动退出
    - 用户说结束类话 → 进入待确认（Sakura 先问），确认后才退出
    - 模型 on=false 仅在待确认且对方已点头后才生效
    """

    _AUTO_EXIT_TURNS = 8

    def __init__(self) -> None:
        self.active: bool = False
        self._turns_left: int = 0
        # 轮次耗尽自动退出后：提示模型若互动仍在继续需再次 on=true
        self.needs_reentry_hint: bool = False
        # 对方说了结束类话，等待口头确认是否真的停
        self.pending_exit_confirm: bool = False
        self.latest_user_text: str = ""

    def enter(self) -> None:
        self.active = True
        self._turns_left = self._AUTO_EXIT_TURNS
        self.needs_reentry_hint = False
        self.pending_exit_confirm = False

    def exit(self) -> None:
        """主动关闭（用户确认结束 / 工具获准 on=false）；不留重进提示。"""
        self.active = False
        self._turns_left = 0
        self.needs_reentry_hint = False
        self.pending_exit_confirm = False

    def request_exit_confirm(self) -> None:
        """用户疑似想结束：先待确认，不立刻关。"""
        if not self.active:
            return
        self.pending_exit_confirm = True

    def clear_exit_confirm(self) -> None:
        self.pending_exit_confirm = False

    def note_user_text(self, text: str) -> None:
        self.latest_user_text = (text or "").strip()

    def refresh_user_reply(self) -> None:
        """用户回话：刷新存活额度，保持开启。"""
        if not self.active:
            return
        self._turns_left = self._AUTO_EXIT_TURNS

    def consume_turn(self) -> bool:
        """系统续投消耗一次；返回是否仍活跃。真实用户轮应走 refresh_user_reply。"""
        if not self.active:
            return False
        self._turns_left -= 1
        if self._turns_left <= 0:
            self.active = False
            self.needs_reentry_hint = True
            self.pending_exit_confirm = False
            return False
        return True


# 模块级单例，供 builtin_tools 和 turn_routing 共享
intimacy_mode_state = IntimacyModeState()

# 与 runtime 中 guide 路径一致：无 guide 时不允许开启节奏模式
_INTIMACY_GUIDE_PATH = Path(__file__).resolve().parents[2] / "data" / "intimacy_guide.txt"

# 系统续投注入的用户标记（不进持久化历史）
INTIMACY_CONTINUE_MARKER = "（続けて）"

# 用户明确结束亲密节奏的口头信号（中日常见说法；避免过宽误伤）
_INTIMACY_END_KEYWORDS: tuple[str, ...] = (
    "结束",
    "結束",
    "到此为止",
    "到此為止",
    "先这样",
    "先這樣",
    "先到这",
    "先到這",
    "够了",
    "夠了",
    "可以了",
    "不要了",
    "不玩了",
    "停下来",
    "停下來",
    "停下",
    "停止",
    "打住",
    "收手",
    "歇了",
    "終わり",
    "終わりに",
    "やめよう",
    "やめて",
    "もういい",
    "もうやめ",
    "止めて",
)

# 待确认阶段：对方点头确认结束（短答整句匹配，避免误伤）
_INTIMACY_EXIT_CONFIRM_EXACT: frozenset[str] = frozenset(
    {
        "嗯",
        "嗯嗯",
        "好",
        "好的",
        "好吧",
        "行",
        "行吧",
        "对",
        "对的",
        "是",
        "是的",
        "可以",
        "确认",
        "就这样",
        "就这样吧",
        "ok",
        "okay",
        "はい",
        "うん",
        "ええ",
    }
)

# 待确认阶段：对方表示还要继续
_INTIMACY_KEEP_KEYWORDS: tuple[str, ...] = (
    "继续",
    "還要",
    "还要",
    "不要停",
    "别停",
    "別停",
    "没完",
    "沒完",
    "再来",
    "再來",
    "不要结束",
    "不要結束",
    "先别结束",
    "先別結束",
    "不是结束",
    "不是結束",
    "还没完",
    "還沒完",
    "続けて",
    "やめない",
    "まだ",
)


def user_signals_intimacy_keep_going(text: str) -> bool:
    """用户是否表示还要继续（优先于结束词，避免「不要结束」误判）。"""
    raw = (text or "").strip()
    if not raw or raw == INTIMACY_CONTINUE_MARKER:
        return False
    lowered = raw.casefold()
    for kw in _INTIMACY_KEEP_KEYWORDS:
        if kw.casefold() in lowered:
            return True
    return False


def user_signals_intimacy_end(text: str) -> bool:
    """用户是否说出结束类话（触发「先问一句」；不等于已确认退出）。"""
    raw = (text or "").strip()
    if not raw or raw == INTIMACY_CONTINUE_MARKER:
        return False
    if user_signals_intimacy_keep_going(raw):
        return False
    lowered = raw.casefold()
    for kw in _INTIMACY_END_KEYWORDS:
        if kw.casefold() in lowered:
            return True
    return False


def user_signals_intimacy_exit_confirm(text: str) -> bool:
    """待确认阶段：用户是否点头确认结束。"""
    raw = (text or "").strip()
    if not raw or raw == INTIMACY_CONTINUE_MARKER:
        return False
    if user_signals_intimacy_keep_going(raw):
        return False
    # 再说一遍结束类话 = 确认
    if user_signals_intimacy_end(raw):
        return True
    normalized = raw.casefold().strip("。.！!？?…~～、,， ")
    return normalized in {s.casefold() for s in _INTIMACY_EXIT_CONFIRM_EXACT}


_SET_INTIMACY_MODE_DESCRIPTION = (
    "切换身体亲密进行中的对话节奏（更快回复，并可能在对方沉默时主动续说）。"
    "仅当双方正在进行或刚明确进入身体亲密互动时设 on=true。"
    "日常闲聊、关心安慰、技术/工作话题、普通撒娇都不要开启。"
    "对方说「结束」「停下」「到此为止」「先这样」等时，先轻声确认是否真的停，"
    "不要立刻 on=false；等对方点头确认（如「嗯」「好」「对」或再说结束）后再关闭。"
    "若对方说继续/还要，则保持开启。"
    "不要仅因气氛变缓、短暂停顿或你自己觉得告一段落而关闭。"
    "关闭或因长时间无用户回话、静默续投耗尽而自动结束后不会自动恢复；"
    "若身体亲密互动仍在继续或再次开始，必须再次 set_intimacy_mode(on=true)。"
    "本工具只影响回复节奏与引导注入；不开也可以写亲密内容，"
    "但只要互动仍在身体亲密层面，就应保持或重新开启节奏模式。"
)


def intimacy_mode_available() -> bool:
    """本地存在非空 intimacy_guide 时才允许开启节奏模式。"""
    try:
        return _INTIMACY_GUIDE_PATH.is_file() and _INTIMACY_GUIDE_PATH.stat().st_size > 0
    except OSError:
        return False


def _handle_set_intimacy_mode(arguments: dict[str, Any]) -> dict[str, Any]:
    on = bool(arguments.get("on", False))
    if on and not intimacy_mode_available():
        intimacy_mode_state.exit()
        return {"intimacy_mode": "off", "available": False}
    if on:
        intimacy_mode_state.enter()
        return {"intimacy_mode": "on"}

    # on=false：已关闭则直接确认；开启中须「结束意向 + 口头确认」
    if not intimacy_mode_state.active:
        return {"intimacy_mode": "off"}

    latest = intimacy_mode_state.latest_user_text
    if intimacy_mode_state.pending_exit_confirm:
        if user_signals_intimacy_exit_confirm(latest):
            intimacy_mode_state.exit()
            return {"intimacy_mode": "off"}
        return {
            "intimacy_mode": "on",
            "refused": True,
            "pending_confirm": True,
            "reason": "正在等待对方确认是否结束；先口头问清，确认后再关闭",
        }

    if user_signals_intimacy_end(latest):
        intimacy_mode_state.request_exit_confirm()
        return {
            "intimacy_mode": "on",
            "refused": True,
            "pending_confirm": True,
            "reason": "对方疑似想结束，请先轻声确认；确认后再调用 on=false",
        }

    return {
        "intimacy_mode": "on",
        "refused": True,
        "reason": "需要对方明确表示结束，并由你确认后才可关闭节奏模式",
    }


def create_builtin_tool_registry(
    base_dir: Path,
    memory: MemoryStore | None = None,
    reminders: ReminderStore | None = None,
) -> ToolRegistry:
    paths = StoragePaths(base_dir)
    store = TodoStore(paths.tasks_store())
    notes = NotesStore(paths.notes_dir)
    # MemoryStore 是 dataclass，第一个字段是 base_dir；旧写法把 json 路径误传成
    # base_dir（主链路总会注入 memory，未实际触发），这里一并修正
    memory = memory or MemoryStore(base_dir=base_dir)
    reminders = reminders or ReminderStore(paths.reminders_store())
    registry = ToolRegistry(
        [
            create_screen_observation_tool(),
            Tool(
                name="get_current_time",
                description="获取当前本机时间和时区。",
                parameters={},
                handler=lambda _arguments: get_current_time(),
                group="core",
            ),
            Tool(
                name="set_intimacy_mode",
                description=_SET_INTIMACY_MODE_DESCRIPTION,
                parameters={
                    "type": "object",
                    "properties": {
                        "on": {
                            "type": "boolean",
                            "description": (
                                "true=身体亲密进行中需要更快节奏；"
                                "false=回到日常或对方已停下。"
                            ),
                        },
                    },
                    "required": ["on"],
                },
                handler=_handle_set_intimacy_mode,
                group="core",
            ),
            Tool(
                name="add_todo",
                description="新增一条待办事项。",
                parameters={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "待办内容。"},
                    },
                    "required": ["text"],
                },
                handler=store.add_todo,
                group="productivity",
            ),
            Tool(
                name="list_todos",
                description="列出所有未完成待办事项。",
                parameters={},
                handler=store.list_todos,
                group="productivity",
            ),
            Tool(
                name="complete_todo",
                description="按 id 标记一条待办事项为完成。",
                parameters={
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "待办 id。"},
                    },
                    "required": ["id"],
                },
                handler=store.complete_todo,
                group="productivity",
            ),
            Tool(
                name="add_reminder",
                description="创建一次性提醒。对方说“几分钟后/几秒后”这类相对时间时，必须优先使用 delay_seconds 或 delay_minutes，让程序计算触发时间；只有对方给出明确日期时间时才使用 trigger_at。repeat 第一版只支持 null 或省略。",
                parameters={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "提醒内容。"},
                        "trigger_at": {
                            "type": "string",
                            "description": "明确的提醒时间，本地时区 ISO 字符串。相对时间不要使用这个字段。",
                        },
                        "delay_seconds": {
                            "type": "number",
                            "description": "从现在开始延迟多少秒触发。适合“30 秒后”等相对提醒。",
                        },
                        "delay_minutes": {
                            "type": "number",
                            "description": "从现在开始延迟多少分钟触发。适合“3 分钟后”等相对提醒。",
                        },
                        "repeat": {
                            "type": ["null"],
                            "description": "第一版只支持 null。",
                        },
                    },
                    "required": ["text"],
                },
                handler=reminders.add_reminder,
                group="productivity",
            ),
            Tool(
                name="list_reminders",
                description="列出未完成且未取消的一次性提醒。",
                parameters={},
                handler=reminders.list_reminders,
                group="productivity",
            ),
            Tool(
                name="cancel_reminder",
                description="按 id 取消一条未完成提醒。",
                parameters={
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "提醒 id。"},
                    },
                    "required": ["id"],
                },
                handler=reminders.cancel_reminder,
                group="productivity",
            ),
            Tool(
                name="read_note",
                description="读取 data/notes/ 下的文本笔记。只能读取笔记名，不能读取任意路径。",
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "笔记名，可省略 .txt 后缀。"},
                    },
                    "required": ["name"],
                },
                handler=notes.read_note,
                group="productivity",
            ),
            Tool(
                name="write_note",
                description="写入 data/notes/ 下的文本笔记。只能写入笔记名，不能写入任意路径。",
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "笔记名，可省略 .txt 后缀。"},
                        "content": {"type": "string", "description": "笔记内容。"},
                    },
                    "required": ["name", "content"],
                },
                handler=notes.write_note,
                group="productivity",
            ),
            Tool(
                name="open_url",
                description="打开 http 或 https 网页。该工具会离开聊天窗口，需要对方确认后才能执行。",
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "要打开的 http/https URL。"},
                    },
                    "required": ["url"],
                },
                handler=open_url,
                requires_confirmation=True,
                group="desktop",
            ),
            Tool(
                name="open_local_folder",
                description="打开已存在的本地文件夹。该工具会访问桌面环境，需要对方确认后才能执行。",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "要打开的本地文件夹路径。"},
                    },
                    "required": ["path"],
                },
                handler=open_local_folder,
                requires_confirmation=True,
                group="desktop",
            ),
            Tool(
                name="memory_search",
                description=(
                    "搜索 Sakura 的长期记忆。需要跨会话信息、对方偏好、项目状态或过往约定时使用。"
                    "mode='full'（默认）返回完整正文；"
                    "mode='index' 只返回标题索引（id/title/layer/created_at/importance/approx_tokens），"
                    "token 消耗约 1/10，适合先概览再按需展开。"
                    "首次调用可能返回 status='loading'，这时直接告诉对方记忆系统正在初始化，不要重复调用。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词，可为空；为空时列出最近记忆。"},
                        "limit": {"type": "integer", "description": "最多返回多少条，默认 20。"},
                        "mode": {"type": "string", "description": "full（默认）或 index。"},
                        "layer": {
                            "type": "string",
                            "description": "可选记忆层级：core_profile、semantic、episodic、procedural、session。",
                        },
                        "category": {"type": "string", "description": "可选分类过滤。"},
                        "scope": {"type": "string", "description": "可选角色/作用域，默认当前角色。"},
                    },
                },
                handler=lambda arguments: memory.search_memory(arguments, wait=False),
                group="core",
            ),
            Tool(
                name="memory_detail",
                description=(
                    "按 memory_id 列表批量取回完整记忆内容。"
                    "先用 memory_search(mode='index') 获取标题索引，"
                    "再对感兴趣的条目调用本工具展开全文。"
                    "ids 可以是逗号分隔的字符串或数组。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "ids": {"type": "string", "description": "记忆 id 列表，逗号分隔或直接传数组。"},
                    },
                    "required": ["ids"],
                },
                handler=lambda arguments: memory.get_memory_detail(arguments, wait=False),
                group="core",
            ),
            Tool(
                name="memory_timeline",
                description=(
                    "以某条记忆为锚点，查看它在时间线上的前后上下文。"
                    "给定 memory_id，返回该条记忆及其之前/之后的邻近记忆。"
                    "适合在 memory_search 找到感兴趣的条目后，"
                    "了解「那段时间还发生了什么」。"
                    "不支持常驻档案（core_profile）作为锚点。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "string", "description": "作为锚点的记忆 id。"},
                        "before": {"type": "integer", "description": "返回锚点之前的条目数（默认 3）。"},
                        "after": {"type": "integer", "description": "返回锚点之后的条目数（默认 3）。"},
                    },
                    "required": ["memory_id"],
                },
                handler=lambda arguments: build_timeline(
                    memory,
                    str(arguments.get("memory_id") or "").strip(),
                    before=_safe_int(arguments.get("before"), DEFAULT_TIMELINE_BEFORE),
                    after=_safe_int(arguments.get("after"), DEFAULT_TIMELINE_AFTER),
                ),
                group="core",
            ),
            Tool(
                name="memory_remember",
                description=(
                    "保存一条明确、长期有用的记忆。只在对方明确要求记住，或信息明显会长期帮助相处/协作时使用。"
                    "身体亲密上的第一次、关系推进、对方的亲密偏好/边界、事后仍想记住的话，也属于应长期记住的相处事实"
                    "（写记忆点与偏好，不要写过程流水账）。"
                    "关于他的事实用简体中文写；日记主语「我」=你自己，「他」=对方；"
                    "用「我／他」写清谁说了什么/约了什么，再写感受；已知名字可用名字代替「他」。"
                    "密码、token、密钥、身份证、银行卡等敏感凭据不适合写入长期记忆。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "要保存的长期记忆内容（对方侧事实优先简体中文）。"},
                        "layer": {
                            "type": "string",
                            "description": "可选记忆层级，默认 semantic；稳定偏好/协作规则用 procedural，当前任务用 session。",
                        },
                        "category": {"type": "string", "description": "可选分类，如 preference/project/profile。"},
                        "importance": {"type": "number", "description": "0-1 的重要性，默认 0.5。"},
                        "confidence": {"type": "number", "description": "0-1 的置信度，默认 0.75。"},
                    },
                    "required": ["content"],
                },
                handler=lambda arguments: memory.remember_memory(arguments, wait=False),
                group="memory-write",
            ),
            Tool(
                name="memory_update",
                description=(
                    "更新一条已存在的长期记忆。先用 memory_search 找到 memory_id；"
                    "只在对方明确纠正、补充、合并旧记忆，或已有记忆明显过时时使用。"
                    "不要写入密码、token、密钥、身份证、银行卡等敏感凭据。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "string", "description": "记忆 id，来自 memory_search 结果。"},
                        "content": {"type": "string", "description": "更新后的完整长期记忆内容。"},
                        "layer": {"type": "string", "description": "可选记忆层级。"},
                        "category": {"type": "string", "description": "可选分类。"},
                        "importance": {"type": "number", "description": "0-1 的重要性。"},
                        "confidence": {"type": "number", "description": "0-1 的置信度。"},
                    },
                    "required": ["memory_id", "content"],
                },
                handler=lambda arguments: memory.update_memory(
                    _memory_update_arguments(arguments), wait=False
                ),
                group="memory-write",
            ),
            Tool(
                name="memory_forget",
                description="在对方明确要求忘记某条信息时，按 memory_id 删除长期记忆。",
                parameters={
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "string", "description": "记忆 id，来自 memory_search 结果。"},
                    },
                    "required": ["memory_id"],
                },
                handler=lambda arguments: memory.forget_memory(_memory_forget_arguments(arguments), wait=False),
                group="memory-write",
            ),
            Tool(
                name="memory_let_go",
                description="放手一条记忆——不再想起，但不删除。用于「这件事我已经不想再记着了」的场合。",
                parameters={
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "string", "description": "记忆 id，来自 memory_search 结果。"},
                    },
                    "required": ["memory_id"],
                },
                handler=lambda arguments: memory.release_memory(
                    {"id": arguments.get("memory_id") or arguments.get("id")},
                    wait=False,
                ),
                group="memory-write",
            ),
        ]
    )
    registry.register(
        Tool(
            name="search_tools",
            description=(
                "搜索 Sakura 当前已安装但可能尚未暴露的工具。"
                "当你需要 productivity（待办/提醒/笔记）、desktop（打开链接/文件夹）、"
                "mcp（联网搜索）、browser（网页操作）等能力但当前工具列表不足时使用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "要搜索的工具关键词或能力名称。"},
                },
                "required": ["keyword"],
            },
            handler=registry.search_tools,
            group="core",
            risk="low",
        )
    )
    registry.register(
        Tool(
            name="list_tool_groups",
            description="列出 Sakura 当前可用工具组及数量，用于决定是否需要搜索并激活更多工具。",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=registry.list_tool_groups,
            group="core",
            risk="low",
        )
    )
    return registry


def create_mobile_tool_registry(memory: MemoryStore) -> ToolRegistry:
    """手机端工具表：记忆读写 + 本机时间；不含屏幕/桌面/需确认工具。

    写入仍落到电脑端同一 MemoryStore，与桌面长期记忆共用。
    """
    registry = ToolRegistry(
        [
            Tool(
                name="get_current_time",
                description="获取当前本机时间和时区。",
                parameters={},
                handler=lambda _arguments: get_current_time(),
                group="core",
            ),
            Tool(
                name="memory_search",
                description=(
                    "搜索 Sakura 的长期记忆。需要跨会话信息、对方偏好、项目状态或过往约定时使用。"
                    "mode='full'（默认）返回完整正文；"
                    "mode='index' 只返回标题索引（id/title/layer/created_at/importance/approx_tokens），"
                    "token 消耗约 1/10，适合先概览再按需展开。"
                    "首次调用可能返回 status='loading'，这时直接告诉对方记忆系统正在初始化，不要重复调用。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词，可为空；为空时列出最近记忆。"},
                        "limit": {"type": "integer", "description": "最多返回多少条，默认 20。"},
                        "mode": {"type": "string", "description": "full（默认）或 index。"},
                        "layer": {
                            "type": "string",
                            "description": "可选记忆层级：core_profile、semantic、episodic、procedural、session。",
                        },
                        "category": {"type": "string", "description": "可选分类过滤。"},
                        "scope": {"type": "string", "description": "可选角色/作用域，默认当前角色。"},
                    },
                },
                handler=lambda arguments: memory.search_memory(arguments, wait=False),
                group="core",
            ),
            Tool(
                name="memory_detail",
                description=(
                    "按 memory_id 列表批量取回完整记忆内容。"
                    "先用 memory_search(mode='index') 获取标题索引，"
                    "再对感兴趣的条目调用本工具展开全文。"
                    "ids 可以是逗号分隔的字符串或数组。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "ids": {"type": "string", "description": "记忆 id 列表，逗号分隔或直接传数组。"},
                    },
                    "required": ["ids"],
                },
                handler=lambda arguments: memory.get_memory_detail(arguments, wait=False),
                group="core",
            ),
            Tool(
                name="memory_timeline",
                description=(
                    "以某条记忆为锚点，查看它在时间线上的前后上下文。"
                    "给定 memory_id，返回该条记忆及其之前/之后的邻近记忆。"
                    "适合在 memory_search 找到感兴趣的条目后，"
                    "了解「那段时间还发生了什么」。"
                    "不支持常驻档案（core_profile）作为锚点。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "string", "description": "作为锚点的记忆 id。"},
                        "before": {"type": "integer", "description": "返回锚点之前的条目数（默认 3）。"},
                        "after": {"type": "integer", "description": "返回锚点之后的条目数（默认 3）。"},
                    },
                    "required": ["memory_id"],
                },
                handler=lambda arguments: build_timeline(
                    memory,
                    str(arguments.get("memory_id") or "").strip(),
                    before=_safe_int(arguments.get("before"), DEFAULT_TIMELINE_BEFORE),
                    after=_safe_int(arguments.get("after"), DEFAULT_TIMELINE_AFTER),
                ),
                group="core",
            ),
            Tool(
                name="memory_remember",
                description=(
                    "保存一条明确、长期有用的记忆。只在对方明确要求记住，或信息明显会长期帮助相处/协作时使用。"
                    "身体亲密上的第一次、关系推进、对方的亲密偏好/边界、事后仍想记住的话，也属于应长期记住的相处事实"
                    "（写记忆点与偏好，不要写过程流水账）。"
                    "关于他的事实用简体中文写；日记主语「我」=你自己，「他」=对方；"
                    "用「我／他」写清谁说了什么/约了什么，再写感受；已知名字可用名字代替「他」。"
                    "密码、token、密钥、身份证、银行卡等敏感凭据不适合写入长期记忆。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "要保存的长期记忆内容（对方侧事实优先简体中文）。"},
                        "layer": {
                            "type": "string",
                            "description": "可选记忆层级，默认 semantic；稳定偏好/协作规则用 procedural，当前任务用 session。",
                        },
                        "category": {"type": "string", "description": "可选分类，如 preference/project/profile。"},
                        "importance": {"type": "number", "description": "0-1 的重要性，默认 0.5。"},
                        "confidence": {"type": "number", "description": "0-1 的置信度，默认 0.75。"},
                    },
                    "required": ["content"],
                },
                handler=lambda arguments: memory.remember_memory(arguments, wait=False),
                group="memory-write",
            ),
            Tool(
                name="memory_update",
                description=(
                    "更新一条已存在的长期记忆。先用 memory_search 找到 memory_id；"
                    "只在对方明确纠正、补充、合并旧记忆，或已有记忆明显过时时使用。"
                    "不要写入密码、token、密钥、身份证、银行卡等敏感凭据。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "string", "description": "记忆 id，来自 memory_search 结果。"},
                        "content": {"type": "string", "description": "更新后的完整长期记忆内容。"},
                        "layer": {"type": "string", "description": "可选记忆层级。"},
                        "category": {"type": "string", "description": "可选分类。"},
                        "importance": {"type": "number", "description": "0-1 的重要性。"},
                        "confidence": {"type": "number", "description": "0-1 的置信度。"},
                    },
                    "required": ["memory_id", "content"],
                },
                handler=lambda arguments: memory.update_memory(
                    _memory_update_arguments(arguments), wait=False
                ),
                group="memory-write",
            ),
            Tool(
                name="memory_forget",
                description="在对方明确要求忘记某条信息时，按 memory_id 删除长期记忆。",
                parameters={
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "string", "description": "记忆 id，来自 memory_search 结果。"},
                    },
                    "required": ["memory_id"],
                },
                handler=lambda arguments: memory.forget_memory(
                    _memory_forget_arguments(arguments), wait=False
                ),
                group="memory-write",
            ),
            Tool(
                name="memory_let_go",
                description="放手一条记忆——不再想起，但不删除。用于「这件事我已经不想再记着了」的场合。",
                parameters={
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "string", "description": "记忆 id，来自 memory_search 结果。"},
                    },
                    "required": ["memory_id"],
                },
                handler=lambda arguments: memory.release_memory(
                    {"id": arguments.get("memory_id") or arguments.get("id")},
                    wait=False,
                ),
                group="memory-write",
            ),
        ]
    )
    registry.register(
        Tool(
            name="search_tools",
            description=(
                "搜索当前手机通道已安装但可能尚未暴露的工具。"
                "手机端主要提供记忆读写；需要记住/更新/忘掉时若工具列表里没有，可先搜索 memory。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "要搜索的工具关键词或能力名称。"},
                },
                "required": ["keyword"],
            },
            handler=registry.search_tools,
            group="core",
            risk="low",
        )
    )
    registry.register(
        Tool(
            name="list_tool_groups",
            description="列出当前手机通道可用工具组及数量。",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=registry.list_tool_groups,
            group="core",
            risk="low",
        )
    )
    return registry


def get_current_time() -> dict[str, str]:
    now = datetime.now().astimezone()
    return {
        "datetime": now.isoformat(timespec="seconds"),
        "timezone": now.tzname() or "",
    }


def _memory_forget_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    memory_id = arguments.get("memory_id") or arguments.get("id")
    return {"id": memory_id}


def _memory_update_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    memory_id = arguments.get("memory_id") or arguments.get("id")
    content = arguments.get("content") or arguments.get("new_content")
    mapped = {"id": memory_id, "content": content}
    for key in ("layer", "category", "importance", "confidence"):
        if key in arguments:
            mapped[key] = arguments.get(key)
    return mapped


class TodoStore:
    """以 JSON 文件保存轻量待办，供内部工具使用。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def add_todo(self, arguments: dict[str, Any]) -> dict[str, Any]:
        text = _required_text(arguments, "text")
        data = self._load()
        task = {
            "id": uuid.uuid4().hex[:8],
            "text": text,
            "created_at": _now_iso(),
            "completed_at": None,
        }
        data["tasks"].append(task)
        self._save(data)
        return {"task": task}

    def list_todos(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        data = self._load()
        tasks = [task for task in data["tasks"] if task.get("completed_at") is None]
        return {"tasks": tasks}

    def complete_todo(self, arguments: dict[str, Any]) -> dict[str, Any]:
        task_id = _required_text(arguments, "id")
        data = self._load()
        for task in data["tasks"]:
            if task.get("id") == task_id:
                if task.get("completed_at") is None:
                    task["completed_at"] = _now_iso()
                    self._save(data)
                return {"task": task}
        raise ValueError(f"未找到待办：{task_id}")

    def _load(self) -> dict[str, list[dict[str, Any]]]:
        if not self.path.exists():
            return {"tasks": []}

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"待办文件不是有效 JSON：{self.path}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            raise ValueError("待办文件格式无效，顶层必须是包含 tasks 列表的对象。")
        tasks = [task for task in data["tasks"] if isinstance(task, dict)]
        return {"tasks": tasks}

    def _save(self, data: dict[str, list[dict[str, Any]]]) -> None:
        atomic_write_text(
            self.path,
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _required_text(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"缺少必填参数：{key}")
    return value.strip()


def _safe_int(value: Any, default: int) -> int:
    """安全取整，None 或非数字返回默认值；显式传 0 有效。"""
    if value is None:
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
