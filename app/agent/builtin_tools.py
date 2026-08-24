from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from app.agent.desktop_tools import NotesStore, open_local_folder, open_url
from app.agent.history_tools import (
    HistoryStoreRef,
    handle_history_read,
    handle_history_search,
)
from app.agent.memory import MemoryStore
from app.agent.memory_timeline import DEFAULT_TIMELINE_AFTER, DEFAULT_TIMELINE_BEFORE, build_timeline
from app.agent.reminders import ReminderStore
from app.agent.screen_tools import create_screen_observation_tool
from app.agent.tools import Tool, ToolRegistry
from app.storage.atomic import atomic_write_text
from app.storage.paths import StoragePaths


class IntimacyModeState:
    """可选亲密导演层状态。只影响详细引导注入、回复节奏与自动续投，不构成许可或行为开关。

    生命周期：
    - 开启：用户整句发送约定词 → 系统硬开启（不经 LLM 工具）
    - 保持：用户正常回话 → 刷新 3 轮额度；静默续投 → 扣 1 轮
    - 退出：用户收尾话 / 模型 on=false / 第三轮续投完成后 UI 过期
    """

    _AUTO_EXIT_TURNS = 3

    def __init__(self) -> None:
        self.active: bool = False
        self.pending: bool = False  # 兼容旧字段；硬入口后不再用于开启
        self._turns_left: int = 0
        self.needs_reentry_hint: bool = False
        self.last_user_text: str = ""
        self.opened_by_keyword: bool = False

    def note_user_text(self, text: str) -> None:
        self.last_user_text = str(text or "").strip()

    def request_confirm(self) -> None:
        """已废弃：开启改为约定词硬入口，不再进入 pending。"""
        return

    def confirm(self) -> None:
        """兼容旧调用：等同 enter。"""
        self.enter()

    def reject_pending(self) -> None:
        self.pending = False

    def enter(self, *, by_keyword: bool = False) -> None:
        self.active = True
        self.pending = False
        self._turns_left = self._AUTO_EXIT_TURNS
        self.needs_reentry_hint = False
        self.opened_by_keyword = bool(by_keyword)

    def exit(self) -> None:
        """收尾：清 active，不留重进提示。"""
        self.active = False
        self.pending = False
        self._turns_left = 0
        self.needs_reentry_hint = False
        self.last_user_text = ""
        self.opened_by_keyword = False

    def refresh_user_reply(self) -> None:
        """用户回话：刷新存活额度，保持开启。"""
        if not self.active:
            return
        self._turns_left = self._AUTO_EXIT_TURNS
        # 非约定词的普通回话后，去掉「本轮刚硬开」标记
        if not user_requests_intimacy_entry(self.last_user_text):
            self.opened_by_keyword = False

    def expire_after_silence(self) -> None:
        """第三轮续投回复完成后由 UI 调用；生成前不得提前失活。"""
        if not self.active:
            return
        self.active = False
        self.pending = False
        self._turns_left = 0
        self.opened_by_keyword = False
        self.needs_reentry_hint = True

    def consume_turn(self) -> bool:
        """系统续投消耗一次；返回是否仍活跃。第三轮仍保持 active。"""
        if not self.active:
            return False
        if self._turns_left <= 0:
            self.expire_after_silence()
            return False
        self._turns_left -= 1
        self.opened_by_keyword = False
        return True


# 模块级单例
intimacy_mode_state = IntimacyModeState()

_INTIMACY_GUIDE_PATH = Path(__file__).resolve().parents[2] / "data" / "intimacy_guide.txt"

# 系统续投标记（不进持久化历史）。历史兼容：旧会话可能仍是 role=user + 裸标记。
INTIMACY_CONTINUE_MARKER = "（続けて）"
INTIMACY_CONTINUE_ROLE = "system"
INTIMACY_CONTINUE_SYSTEM_TEXT = (
    "【系统续投信号／不是对方发言】对方当前沉默。"
    "根据当前姿势、呼吸和对方最后的反应自然回应；不要把沉默当成同意升级。"
    "可以放缓、短暂确认或自然收束，不要仅换说法重复上一句。"
    "若已不再需要详细引导、连续节奏或自动续投，调用 set_intimacy_mode(on=false)。"
    "本条是系统信号，绝不要当成用户说过的话，不要回答或复述本信号。\n"
    f"{INTIMACY_CONTINUE_MARKER}"
)

# 进入/退出亲密节奏的成对硬控制词（整句匹配，可带轻标点）。换词只改这里。
INTIMACY_ENTER_PHRASE = "贴紧"
INTIMACY_EXIT_PHRASE = "苹果"
# 旧名兼容
INTIMACY_CONFIRM_PHRASE = INTIMACY_ENTER_PHRASE
_INTIMACY_CONTROL_TRIM_RE = re.compile(
    r"^[\s　\"'“”‘’「」『』]+|[\s　\"'“”‘’「」『』！!。.?？…～~]+$"
)
_INTIMACY_EXIT_RE = re.compile(
    r"(冷静|先这样|不闹了|差不多了|休息吧|睡吧|聊点别的|不要继续|别继续|"
    r"停下|退出亲密|结束吧|到此为止|我不舒服|"
    r"やめよう|やめて|冷却)",
    re.IGNORECASE,
)


def _normalize_intimacy_control_phrase(text: str) -> str:
    return _INTIMACY_CONTROL_TRIM_RE.sub("", str(text or "").strip()).strip()


def user_requests_intimacy_exit(text: str) -> bool:
    return _normalize_intimacy_control_phrase(text) == INTIMACY_EXIT_PHRASE


def user_declines_or_exits_intimacy(text: str) -> bool:
    """安全词整句退出，或明确要求退出/降温。"""
    t = str(text or "").strip()
    if not t:
        return False
    if user_requests_intimacy_exit(t):
        return True
    return bool(_INTIMACY_EXIT_RE.search(t))


def user_requests_intimacy_entry(text: str) -> bool:
    """整句是否为约定硬入口词。"""
    t = str(text or "").strip()
    if not t or INTIMACY_CONTINUE_MARKER in t:
        return False
    if user_declines_or_exits_intimacy(t):
        return False
    return _normalize_intimacy_control_phrase(t) == INTIMACY_ENTER_PHRASE


def user_confirms_intimacy(text: str) -> bool:
    """兼容旧名：等同约定词硬入口检测。"""
    return user_requests_intimacy_entry(text)


def apply_intimacy_user_utterance(
    text: str,
    state: IntimacyModeState | None = None,
) -> str | None:
    """处理约定词硬开 / 收尾退出。返回动作名或 None。"""
    st = state if state is not None else intimacy_mode_state
    st.note_user_text(text)
    st.pending = False

    if user_requests_intimacy_entry(text):
        if not intimacy_mode_available():
            return "unavailable"
        already = st.active
        st.enter(by_keyword=True)
        return "already_on" if already else "entered"

    if user_declines_or_exits_intimacy(text):
        if st.active:
            st.exit()
            return "exited"
        return None

    return None


def build_intimacy_continue_message() -> dict[str, Any]:
    """构造亲密静默续投的系统消息（role=system，避免假 user 导致角色串线）。"""
    return {
        "role": INTIMACY_CONTINUE_ROLE,
        "content": INTIMACY_CONTINUE_SYSTEM_TEXT,
        "source": "intimacy_continue",
        "_sakura_transient_progress": True,
    }


def message_is_intimacy_continue(message: dict[str, Any] | None) -> bool:
    """判断单条消息是否为亲密续投信号（含旧版 user 裸标记）。"""
    if not isinstance(message, dict):
        return False
    if str(message.get("source") or "").strip() == "intimacy_continue":
        return True
    content = str(message.get("content") or "")
    if INTIMACY_CONTINUE_MARKER not in content:
        return False
    role = str(message.get("role") or "").strip()
    return role in {"system", "user"}


def latest_is_intimacy_continue(messages: list[Any] | None) -> bool:
    """对话末条是否为亲密续投信号。"""
    if not messages:
        return False
    last = messages[-1]
    return message_is_intimacy_continue(last if isinstance(last, dict) else None)


_SET_INTIMACY_MODE_DESCRIPTION = (
    "关闭可选亲密导演层的详细引导、回复节奏与自动续投（更快回复、沉默续投、亲密/H tone）。"
    f"开启不由本工具控制：只有对方整句发送约定词「{INTIMACY_ENTER_PHRASE}」时，"
    "系统才会自动开启；不要猜测、不要调用 on=true 试图开启。"
    "当详细引导、节奏或自动续投不再需要，或对方降温/收尾时，调用 on=false："
    f"安全词「{INTIMACY_EXIT_PHRASE}」或明确的停下、不要继续、不适等表达会立即退出。"
    f"关闭后不会自动恢复；对方需再次发送「{INTIMACY_ENTER_PHRASE}」。"
    "本工具只影响引导注入、回复节奏与自动续投，不限制身体亲密行为本身。"
)


def intimacy_mode_available() -> bool:
    """本地存在非空 intimacy_guide 时才允许开启节奏模式。"""
    try:
        return _INTIMACY_GUIDE_PATH.is_file() and _INTIMACY_GUIDE_PATH.stat().st_size > 0
    except OSError:
        return False


def _handle_set_intimacy_mode(arguments: dict[str, Any]) -> dict[str, Any]:
    on = bool(arguments.get("on", False))
    if on:
        # 开启已改为约定词硬入口；工具 on=true 不再开启，避免 LLM 误猜。
        if intimacy_mode_state.active:
            return {"intimacy_mode": "on", "entry": "keyword_only"}
        return {
            "intimacy_mode": "off",
            "entry": "keyword_only",
            "instruction": (
                f"开启请等对方整句发送约定词「{INTIMACY_ENTER_PHRASE}」；"
                "系统会自动开启。不要再调用本工具 on=true。"
            ),
        }
    intimacy_mode_state.exit()
    return {"intimacy_mode": "off"}


def create_builtin_tool_registry(
    base_dir: Path,
    memory: MemoryStore | None = None,
    reminders: ReminderStore | None = None,
    history: HistoryStoreRef | None = None,
) -> ToolRegistry:
    paths = StoragePaths(base_dir)
    store = TodoStore(paths.tasks_store())
    notes = NotesStore(paths.notes_dir)
    # MemoryStore 是 dataclass，第一个字段是 base_dir；旧写法把 json 路径误传成
    # base_dir（主链路总会注入 memory，未实际触发），这里一并修正
    memory = memory or MemoryStore(base_dir=base_dir)
    reminders = reminders or ReminderStore(paths.reminders_store())
    # 不自动建 ChatHistoryStore：registry 不知角色 id；history=None 时工具优雅降级
    history_ref = history if history is not None else HistoryStoreRef(None)
    registry = ToolRegistry(
        [
            create_screen_observation_tool(),
            Tool(
                name="set_intimacy_mode",
                description=_SET_INTIMACY_MODE_DESCRIPTION,
                parameters={
                    "type": "object",
                    "properties": {
                        "on": {
                            "type": "boolean",
                            "description": (
                                "true=无效，开启只能靠约定词；"
                                "false=关闭详细引导、节奏与自动续投，不表示结束身体亲密。"
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
                description="打开 http 或 https 网页。该工具会离开聊天窗口；关闭「完整访问权限」时需要对方确认。",
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
                description="打开已存在的本地文件夹。该工具会访问桌面环境；关闭「完整访问权限」时需要对方确认。",
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
            *_history_tools(history_ref),
            Tool(
                name="memory_search",
                description=(
                    "搜索 Sakura 的长期记忆。问「认不认识 / 旧事 / 偏好 / 是谁」时默认用本工具，"
                    "不要先去 history_search 翻聊天记录。"
                    "仅当运行时已注入的记忆不够用时再调用；"
                    "同轮优先一次，显式回忆最多两次，不要对同一意图换词连搜。"
                    "若结果为空或未写明某细节，回答时承认不知道/记不清，禁止编造。"
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


def create_mobile_tool_registry(
    memory: MemoryStore,
    history: HistoryStoreRef | None = None,
) -> ToolRegistry:
    """手机端工具表：记忆读写 + 历史查询；不含屏幕/桌面/需确认工具。

    写入仍落到电脑端同一 MemoryStore，与桌面长期记忆共用。
    当前时间由运行时事实注入，不注册 get_current_time。
    """
    history_ref = history if history is not None else HistoryStoreRef(None)
    registry = ToolRegistry(
        [
            *_history_tools(history_ref),
            Tool(
                name="memory_search",
                description=(
                    "搜索 Sakura 的长期记忆。问「认不认识 / 旧事 / 偏好 / 是谁」时默认用本工具，"
                    "不要先去 history_search 翻聊天记录。"
                    "仅当运行时已注入的记忆不够用时再调用；"
                    "同轮优先一次，显式回忆最多两次，不要对同一意图换词连搜。"
                    "若结果为空或未写明某细节，回答时承认不知道/记不清，禁止编造。"
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


def _history_tools(history_ref: HistoryStoreRef) -> list[Tool]:
    """原始对话历史查询工具（桌面 / 移动端共用定义）。"""
    return [
        Tool(
            name="history_search",
            description=(
                "查询原始对话记录（聊天流水，不是长期记忆）。"
                "默认不要用：问「认不认识 / 旧事 / 偏好 / 是谁」应先 memory_search。"
                "仅在需要逐字原话、按时间窗翻聊天记录，或对方明确要查「说过什么/聊天记录」时再用。"
                "可按相对时间（昨天/今天/约N小时前/YYYY-MM-DD 等）和/或关键词定位。"
                "有时间窗或关键词时按时间正序分页（从对话开头读）；"
                "返回 total_count/has_more，若 has_more 请用相同条件加 offset 翻页，不要改词重搜。"
                "无筛选时返回最近若干条。找到 id 后用 history_read 看前后上下文。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "time": {
                        "type": "string",
                        "description": (
                            "可选时间窗：昨天/今天/前天/上周三、昨天下午/昨天晚上、"
                            "昨天晚上一点到两点、N分钟前/约N小时前、YYYY-MM-DD/ISO。空=不限。"
                        ),
                    },
                    "end": {
                        "type": "string",
                        "description": "可选结束时间（同 time 格式）。通常只需 time。",
                    },
                    "keyword": {
                        "type": "string",
                        "description": "可选关键词，匹配原文或译文。",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "每页条数，默认 20，上限 50。",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "分页偏移，默认 0。has_more 时用上次的 next_offset。",
                    },
                },
            },
            handler=lambda arguments, _ref=history_ref: handle_history_search(_ref, arguments),
            group="history",
        ),
        Tool(
            name="history_read",
            description=(
                "以某条对话消息为锚点，读取它前后的原始对话上下文。"
                "先用 history_search 找到 entry_id，再调用本工具展开。"
                "这是原始对话记录，不是长期记忆时间线（那是 memory_timeline）。"
                "仅在已判定需要原话/聊天上下文时使用；一般事实回忆用 memory_*。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "entry_id": {
                        "type": "integer",
                        "description": "锚点消息 id（来自 history_search）。",
                    },
                    "before": {
                        "type": "integer",
                        "description": "锚点之前的条数，默认 3，上限 10。",
                    },
                    "after": {
                        "type": "integer",
                        "description": "锚点之后的条数，默认 3，上限 10。",
                    },
                },
                "required": ["entry_id"],
            },
            handler=lambda arguments, _ref=history_ref: handle_history_read(_ref, arguments),
            group="history",
        ),
    ]


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
