from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    QRect,
    Qt,
    QThread,
    QTimer,
    Slot,
)
from PySide6.QtGui import QAction, QCursor, QFont, QFontDatabase, QIcon, QKeyEvent, QMouseEvent, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from app.agent import (
    AgentEvent,
    AgentResult,
    AgentRuntime,
    MemoryStore,
    PendingToolAction,
    ReminderStore,
    create_builtin_tool_registry,
)
from app.agent.mcp import MCPToolProvider, register_mcp_tools_from_config
from app.agent.screen_tools import SCREEN_OBSERVATION_REQUEST_ACTION
from app.api_client import OpenAICompatibleClient
from app.browser_controller import BrowserController, BrowserToolBridge
from app.character_loader import (
    DEFAULT_CHARACTER_ID,
    CharacterConfigError,
    CharacterProfile,
    CharacterRegistry,
    load_character_system_prompt,
)
from app.chat_history import ChatHistoryStore
from app.chat_reply import ChatReply, ChatSegment
from app.context_trimming import trim_messages_for_model
from app.chat_worker import ChatWorker, EventWorker
from app.debug_log import debug_log, summarize_messages
from app.env_config import load_env_file, save_env_values
from app.history_window import HistoryWindow
from app.portrait_utils import should_crossfade_portrait
from app.screen_observation import (
    append_observation_marker,
    build_screen_observation_user_message,
    capture_screen_observation,
)
from app.settings_dialog import SettingsDialog
from app.tts import (
    GPTSoVITSTTSProvider,
    GPTSoVITSTTSSettings,
    NullTTSProvider,
    TTSConfigError,
    TTSPreparedAudio,
    TTSProvider,
)


SPEECH_TYPING_INTERVAL_MS = 35
REPLY_SEGMENT_PAUSE_MS = 100
PORTRAIT_TRANSITION_MS = 220
REMINDER_CHECK_INTERVAL_MS = 30_000
SUBTITLE_LANGUAGE_KEY = "SUBTITLE_LANGUAGE"
SUBTITLE_LANGUAGE_JA = "ja"
SUBTITLE_LANGUAGE_ZH = "zh"
SCREEN_OBSERVATION_ENABLED_KEY = "SCREEN_OBSERVATION_ENABLED"


class PetWindow(QWidget):
    def __init__(
        self,
        base_dir: Path,
        character_registry: CharacterRegistry,
        character_profile: CharacterProfile,
        api_client: OpenAICompatibleClient,
        tts_provider: TTSProvider,
    ) -> None:
        super().__init__()
        self.base_dir = base_dir
        self.env_path = base_dir / ".env"
        self.character_registry = character_registry
        self.character_profile = character_profile
        self.portrait_path = character_profile.default_portrait_path
        self.api_client = api_client
        self.system_prompt = load_character_system_prompt(character_profile)
        self.memory_store = MemoryStore(base_dir / "data" / "memory.json")
        self.reminder_store = ReminderStore(base_dir / "data" / "reminders.json")
        self.browser_controller = BrowserController(self)
        self.browser_tool_bridge = BrowserToolBridge(self.browser_controller, self)
        self.tool_registry = create_builtin_tool_registry(
            base_dir,
            self.memory_store,
            self.reminder_store,
            browser_executor=self.browser_tool_bridge,
        )
        self.mcp_tool_provider: MCPToolProvider | None = register_mcp_tools_from_config(
            base_dir,
            self.tool_registry,
        )
        self.agent_runtime = AgentRuntime(
            api_client=api_client,
            system_prompt=self.system_prompt,
            reply_tones=character_profile.reply_tones,
            tools=self.tool_registry,
            memory=self.memory_store,
        )
        self.tts_provider = tts_provider
        self.retired_tts_providers: list[TTSProvider] = []
        self.history_store = self._create_history_store(character_profile)
        self.subtitle_language = self._load_subtitle_language()
        self.screen_observation_enabled = self._load_screen_observation_enabled()
        self.model_vision_enabled = self.screen_observation_enabled
        self.agent_runtime.set_model_vision_enabled(self.model_vision_enabled)
        self.free_access_enabled = self.tool_registry.free_access_enabled
        self.history_window: HistoryWindow | None = None
        self.messages: list[dict[str, Any]] = []
        self.portrait_pixmap_cache: dict[Path, QPixmap] = {}
        self.worker_thread: QThread | None = None
        self.worker: ChatWorker | EventWorker | None = None
        self.drag_offset: QPoint | None = None
        self.stage_size = (860, 640)
        self.speech_text = ""
        self.speech_index = 0
        self.pending_reply_segments: list[ChatSegment] = []
        self.current_segment: ChatSegment | None = None
        self.prepared_next_segment: ChatSegment | None = None
        self.prepared_next_tts: TTSPreparedAudio | None = None
        self.reply_sequence_id = 0
        self.reply_advance_token = 0
        self.current_segment_sequence_id: int | None = None
        self.current_segment_speech_done = False
        self.current_segment_tts_done = True
        self.reply_advance_scheduled = False
        self.pending_tool_action: PendingToolAction | None = None
        self.pending_screen_observation_messages: list[dict[str, Any]] | None = None
        self.screen_observation_followup_in_progress = False
        self.active_reminder_id: str | None = None
        self.active_reminder_text = ""
        self.interaction_sequence = 0
        self.active_interaction_id = ""
        self.active_interaction_started_at: float | None = None
        self.active_interaction_last_at: float | None = None
        self.portrait_transition_animation: QParallelAnimationGroup | None = None
        self.portrait_transition_id = 0
        self.speech_timer = QTimer(self)
        self.speech_timer.setInterval(SPEECH_TYPING_INTERVAL_MS)
        self.speech_timer.timeout.connect(self._show_next_speech_char)
        self.reminder_timer = QTimer(self)
        self.reminder_timer.setInterval(REMINDER_CHECK_INTERVAL_MS)
        self.reminder_timer.timeout.connect(self._check_due_reminders)
        self.reminder_timer.start()
        debug_log(
            "PetWindow",
            "窗口运行状态初始化",
            {
                "character_id": character_profile.id,
                "character_name": character_profile.display_name,
                "tool_count": len(self.tool_registry.all()),
                "mcp_enabled": self.mcp_tool_provider is not None,
                "tts_provider": type(tts_provider).__name__,
                "subtitle_language": self.subtitle_language,
                "screen_observation_enabled": self.screen_observation_enabled,
            },
        )

        self.setWindowTitle(character_profile.display_name)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self.label = QLabel(self)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.label.customContextMenuRequested.connect(self._show_context_menu)
        self.portrait_opacity_effect = QGraphicsOpacityEffect(self.label)
        self.portrait_opacity_effect.setOpacity(1.0)
        self.label.setGraphicsEffect(self.portrait_opacity_effect)

        self.portrait_transition_label = QLabel(self)
        self.portrait_transition_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.portrait_transition_label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.portrait_transition_label.customContextMenuRequested.connect(self._show_context_menu)
        self.portrait_transition_label.hide()
        self.portrait_transition_opacity_effect = QGraphicsOpacityEffect(self.portrait_transition_label)
        self.portrait_transition_opacity_effect.setOpacity(0.0)
        self.portrait_transition_label.setGraphicsEffect(self.portrait_transition_opacity_effect)

        self.bubble = QFrame(self)
        self.bubble.setObjectName("speechBubble")
        self.bubble.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.bubble.customContextMenuRequested.connect(self._show_context_menu)

        self.name_label = QLabel(character_profile.display_name, self.bubble)
        self.name_label.setObjectName("speakerName")

        self.speech_label = QLabel(character_profile.initial_message, self.bubble)
        self.speech_label.setObjectName("speechText")
        self.speech_label.setWordWrap(True)
        self.speech_label.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)

        bubble_header = QHBoxLayout()
        bubble_header.setContentsMargins(0, 0, 0, 0)
        bubble_header.addWidget(self.name_label)
        bubble_header.addStretch(1)

        bubble_layout = QVBoxLayout()
        bubble_layout.setContentsMargins(22, 12, 22, 14)
        bubble_layout.setSpacing(6)
        bubble_layout.addLayout(bubble_header)
        bubble_layout.addWidget(self.speech_label, 1)
        self.bubble.setLayout(bubble_layout)

        self.input_bar = QFrame(self)
        self.input_bar.setObjectName("inputBar")

        self.input_edit = QLineEdit(self.input_bar)
        self.input_edit.setObjectName("petInput")
        self.input_edit.setPlaceholderText(f"{character_profile.display_name}に話しかける...")
        self.input_edit.setFixedHeight(34)
        self.input_edit.installEventFilter(self)
        self.input_edit.returnPressed.connect(self._handle_return_pressed)

        self.send_button = QPushButton("发送", self.input_bar)
        self.send_button.setObjectName("sendButton")
        self.send_button.setFixedHeight(34)
        self.send_button.clicked.connect(self._handle_send_button_clicked)

        self.confirm_action_button = QPushButton("执行", self.input_bar)
        self.confirm_action_button.setObjectName("confirmActionButton")
        self.confirm_action_button.setFixedHeight(34)
        self.confirm_action_button.hide()
        self.confirm_action_button.clicked.connect(self.confirm_pending_action)

        self.cancel_action_button = QPushButton("取消", self.input_bar)
        self.cancel_action_button.setObjectName("cancelActionButton")
        self.cancel_action_button.setFixedHeight(34)
        self.cancel_action_button.hide()
        self.cancel_action_button.clicked.connect(self.cancel_pending_action)

        input_layout = QHBoxLayout()
        input_layout.setContentsMargins(0, 5, 0, 5)
        input_layout.setSpacing(8)
        input_layout.addWidget(self.input_edit, 1)
        input_layout.addWidget(self.confirm_action_button)
        input_layout.addWidget(self.cancel_action_button)
        input_layout.addWidget(self.send_button)
        self.input_bar.setLayout(input_layout)

        self.setStyleSheet(
            """
            #speechBubble {
                background: rgba(255, 232, 241, 188);
                border: 1px solid rgba(238, 172, 200, 132);
                border-radius: 26px;
            }
            #speakerName {
                color: #d55b91;
                font-size: 13px;
                font-weight: 700;
            }
            #speechText {
                color: #4b3440;
                font-size: 19px;
                line-height: 1.35;
            }
            #inputBar {
                background: transparent;
                border: none;
            }
            #petInput {
                background: rgba(255, 255, 255, 132);
                border: 1px solid rgba(255, 255, 255, 1);
                border-radius: 17px;
                color: #4b3440;
                font-size: 13px;
                padding: 2px 14px;
            }
            #petInput:disabled {
                color: rgba(75, 52, 64, 130);
            }
            #sendButton {
                background: rgba(74, 170, 214, 225);
                border: none;
                border-radius: 16px;
                color: white;
                font-size: 15px;
                font-weight: 800;
                min-width: 68px;
                padding: 4px 14px;
            }
            #sendButton:hover {
                background: rgba(48, 145, 195, 235);
            }
            #sendButton:disabled {
                background: rgba(126, 171, 193, 190);
            }
            #confirmActionButton {
                background: rgba(93, 181, 130, 225);
                border: none;
                border-radius: 16px;
                color: white;
                font-size: 15px;
                font-weight: 800;
                min-width: 58px;
                padding: 4px 12px;
            }
            #cancelActionButton {
                background: rgba(180, 130, 146, 210);
                border: none;
                border-radius: 16px;
                color: white;
                font-size: 15px;
                font-weight: 800;
                min-width: 58px;
                padding: 4px 12px;
            }
            """
        )
        self._apply_fonts()
        for drag_widget in (
            self.label,
            self.portrait_transition_label,
            self.bubble,
            self.name_label,
            self.speech_label,
        ):
            drag_widget.installEventFilter(self)

        self.pixmap = self._load_portrait()
        self._apply_portrait()
        self._create_tray_icon()
        self._move_to_default_position()

        application = QApplication.instance()
        if application is not None:
            application.aboutToQuit.connect(self.close_mcp_tools)

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        self._layout_stage()

    def eventFilter(self, watched, event) -> bool:  # type: ignore[no-untyped-def]
        if watched is self.input_edit and event.type() == QEvent.Type.KeyPress:
            self._log_input_key_event(event)
        if isinstance(event, QMouseEvent):
            if event.type() == QEvent.Type.MouseButtonPress:
                return self._handle_mouse_press(event)
            if event.type() == QEvent.Type.MouseMove:
                return self._handle_mouse_move(event)
            if event.type() == QEvent.Type.MouseButtonRelease:
                return self._handle_mouse_release(event)
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._handle_mouse_press(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._handle_mouse_move(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._handle_mouse_release(event)

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self.close_mcp_tools()
        super().closeEvent(event)

    @Slot()
    def close_mcp_tools(self) -> None:
        if self.mcp_tool_provider is None:
            return
        self.mcp_tool_provider.close()
        self.mcp_tool_provider = None

    def _handle_mouse_press(self, event: QMouseEvent) -> bool:
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return True
        if event.button() == Qt.MouseButton.RightButton:
            self._show_context_menu(event.position().toPoint())
            event.accept()
            return True
        return False

    def _handle_mouse_move(self, event: QMouseEvent) -> bool:
        if event.buttons() & Qt.MouseButton.LeftButton and self.drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self.drag_offset)
            event.accept()
            return True
        return False

    def _handle_mouse_release(self, event: QMouseEvent) -> bool:
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_offset = None
            event.accept()
            return True
        return False

    def _load_portrait(self, portrait_path: Path | None = None) -> QPixmap:
        target_path = portrait_path or self.portrait_path
        cached = self.portrait_pixmap_cache.get(target_path)
        if cached is not None:
            return cached

        pixmap = QPixmap(str(target_path))
        if pixmap.isNull():
            QMessageBox.critical(
                self,
                "立绘加载失败",
                f"无法加载立绘：{target_path}",
            )
        self.portrait_pixmap_cache[target_path] = pixmap
        return pixmap

    def _preload_portrait_for_tone(self, tone: str | None) -> None:
        next_portrait_path = self.character_profile.portrait_for_tone(tone)
        if next_portrait_path not in self.portrait_pixmap_cache:
            self._load_portrait(next_portrait_path)

    def _apply_portrait_for_tone(self, tone: str | None) -> None:
        next_portrait_path = self.character_profile.portrait_for_tone(tone)
        if next_portrait_path == self.portrait_path:
            return
        should_crossfade = should_crossfade_portrait(self.portrait_path, next_portrait_path)
        next_pixmap = self._load_portrait(next_portrait_path)
        self.portrait_path = next_portrait_path
        if should_crossfade:
            self._crossfade_portrait(next_pixmap)
        else:
            self.pixmap = next_pixmap
            self._apply_portrait()
        if hasattr(self, "tray_icon"):
            self.tray_icon.setIcon(QIcon(self.pixmap) if not self.pixmap.isNull() else QIcon())

    def _apply_portrait(self) -> None:
        self._stop_portrait_transition()
        if self.pixmap.isNull():
            self.resize(*self.stage_size)
            return

        self._apply_pixmap_to_label(self.label, self.pixmap)
        self.resize(*self.stage_size)
        self._layout_stage()

    def _apply_pixmap_to_label(self, label: QLabel, pixmap: QPixmap) -> None:
        scaled = pixmap.scaled(
            560,
            570,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        label.setPixmap(scaled)
        label.resize(scaled.size())

    def _crossfade_portrait(self, next_pixmap: QPixmap) -> None:
        self._stop_portrait_transition(finish_current=True)
        self.pixmap = next_pixmap
        if self.pixmap.isNull():
            self._apply_portrait()
            return

        self._apply_pixmap_to_label(self.portrait_transition_label, self.pixmap)
        self.resize(*self.stage_size)
        self._layout_stage()
        self.portrait_opacity_effect.setOpacity(1.0)
        self.portrait_transition_opacity_effect.setOpacity(0.0)
        self.portrait_transition_label.show()
        self.portrait_transition_label.raise_()
        self.bubble.raise_()
        self.input_bar.raise_()

        self.portrait_transition_id += 1
        transition_id = self.portrait_transition_id
        animation = QParallelAnimationGroup(self)

        fade_out = QPropertyAnimation(self.portrait_opacity_effect, b"opacity")
        fade_out.setDuration(PORTRAIT_TRANSITION_MS)
        fade_out.setStartValue(1.0)
        fade_out.setEndValue(0.0)
        fade_out.setEasingCurve(QEasingCurve.Type.InOutQuad)

        fade_in = QPropertyAnimation(self.portrait_transition_opacity_effect, b"opacity")
        fade_in.setDuration(PORTRAIT_TRANSITION_MS)
        fade_in.setStartValue(0.0)
        fade_in.setEndValue(1.0)
        fade_in.setEasingCurve(QEasingCurve.Type.InOutQuad)

        animation.addAnimation(fade_out)
        animation.addAnimation(fade_in)
        animation.finished.connect(lambda: self._finish_portrait_transition(transition_id))
        self.portrait_transition_animation = animation
        animation.start()

    def _stop_portrait_transition(self, finish_current: bool = False) -> None:
        if self.portrait_transition_animation is not None:
            self.portrait_transition_animation.stop()
            self.portrait_transition_animation.deleteLater()
            self.portrait_transition_animation = None
            self.portrait_transition_id += 1
        self.portrait_transition_label.hide()
        self.portrait_transition_label.clear()
        self.portrait_opacity_effect.setOpacity(1.0)
        self.portrait_transition_opacity_effect.setOpacity(0.0)
        if finish_current and not self.pixmap.isNull():
            self._apply_pixmap_to_label(self.label, self.pixmap)
            self.resize(*self.stage_size)
            self._layout_stage()

    def _finish_portrait_transition(self, transition_id: int) -> None:
        if transition_id != self.portrait_transition_id:
            return
        if self.portrait_transition_animation is not None:
            self.portrait_transition_animation.deleteLater()
            self.portrait_transition_animation = None
        self._apply_pixmap_to_label(self.label, self.pixmap)
        self.portrait_transition_label.hide()
        self.portrait_transition_label.clear()
        self.portrait_opacity_effect.setOpacity(1.0)
        self.portrait_transition_opacity_effect.setOpacity(0.0)
        self._layout_stage()

    def _apply_fonts(self) -> None:
        text_font = _rounded_japanese_font(11, QFont.Weight.Normal)
        name_font = _rounded_japanese_font(10, QFont.Weight.Bold)
        button_font = _rounded_japanese_font(11, QFont.Weight.ExtraBold)

        self.name_label.setFont(name_font)
        self._apply_speech_font()
        self.input_edit.setFont(text_font)
        self.send_button.setFont(button_font)

    def _apply_speech_font(self) -> None:
        if self.subtitle_language == SUBTITLE_LANGUAGE_ZH:
            self.speech_label.setFont(_rounded_chinese_font(15, QFont.Weight.Medium))
            return
        self.speech_label.setFont(_rounded_japanese_font(15, QFont.Weight.Medium))

    def _layout_stage(self) -> None:
        width = self.width()
        height = self.height()
        portrait_width = self.label.width()
        portrait_height = self.label.height()
        self.label.move((width - portrait_width) // 2, max(0, height - portrait_height - 62))
        transition_width = self.portrait_transition_label.width()
        transition_height = self.portrait_transition_label.height()
        self.portrait_transition_label.move(
            (width - transition_width) // 2,
            max(0, height - transition_height - 62),
        )

        bubble_width = min(640, width - 96)
        bubble_height = 128
        input_height = 44
        input_gap = 10
        bubble_x = (width - bubble_width) // 2
        bubble_y = height - bubble_height - input_height - input_gap - 108
        self.bubble.setGeometry(QRect(bubble_x, bubble_y, bubble_width, bubble_height))
        self.bubble.raise_()

        input_y = bubble_y + bubble_height + input_gap
        self.input_bar.setGeometry(QRect(bubble_x, input_y, bubble_width, input_height))
        self.input_bar.raise_()

    def _create_tray_icon(self) -> None:
        icon = QIcon(self.pixmap) if not self.pixmap.isNull() else QIcon()
        self.tray_icon = QSystemTrayIcon(icon, self)
        self.tray_icon.setToolTip(self.character_profile.display_name)
        self.tray_icon.setContextMenu(self._build_menu())
        self.tray_icon.activated.connect(self._handle_tray_activated)
        self.tray_icon.show()

    def _build_menu(self) -> QMenu:
        menu = QMenu(self)

        toggle_action = QAction("隐藏/显示立绘", self)
        toggle_action.triggered.connect(self.toggle_visible)
        menu.addAction(toggle_action)

        menu.addSeparator()

        subtitle_action = QAction("显示中文字幕", self)
        subtitle_action.setCheckable(True)
        subtitle_action.setChecked(self.subtitle_language == SUBTITLE_LANGUAGE_ZH)
        subtitle_action.triggered.connect(self._toggle_chinese_subtitles)
        menu.addAction(subtitle_action)

        vision_action = QAction("启用模型视觉", self)
        vision_action.setCheckable(True)
        vision_action.setChecked(self.model_vision_enabled)
        vision_action.setEnabled(self.screen_observation_enabled)
        vision_action.triggered.connect(self._toggle_model_vision)
        menu.addAction(vision_action)

        free_access_action = QAction("自由访问权限", self)
        free_access_action.setCheckable(True)
        free_access_action.setChecked(self.free_access_enabled)
        free_access_action.triggered.connect(self._toggle_free_access)
        menu.addAction(free_access_action)

        menu.addSeparator()

        history_action = QAction("历史记录", self)
        history_action.triggered.connect(self.show_history)
        menu.addAction(history_action)

        settings_action = QAction("设置", self)
        settings_action.triggered.connect(self.show_settings)
        menu.addAction(settings_action)

        menu.addSeparator()

        quit_action = QAction("退出", self)
        quit_action.triggered.connect(QApplication.quit)
        menu.addAction(quit_action)

        return menu

    def _show_context_menu(self, position: QPoint) -> None:
        _ = position
        self._build_menu().exec(QCursor.pos())

    def _handle_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.toggle_visible()

    def _move_to_default_position(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geometry = screen.availableGeometry()
        x = geometry.right() - self.width() - 40
        y = geometry.bottom() - self.height() - 20
        self.move(max(geometry.left(), x), max(geometry.top(), y))

    def _begin_interaction(self, source: str) -> None:
        self.interaction_sequence += 1
        now = time.perf_counter()
        self.active_interaction_id = f"interaction-{self.interaction_sequence}"
        self.active_interaction_started_at = now
        self.active_interaction_last_at = now
        debug_log(
            "Latency",
            "输入事件开始",
            {
                "interaction_id": self.active_interaction_id,
                "source": source,
                "input_chars": len(self.input_edit.text()),
                "worker_busy": self.worker_thread is not None,
            },
        )

    def _log_input_key_event(self, event: object) -> None:
        key_event = event if isinstance(event, QKeyEvent) else None
        debug_log(
            "Input",
            "输入框按键事件",
            {
                "key": int(key_event.key()) if key_event is not None else "",
                "text": key_event.text() if key_event is not None else "",
                "modifiers": str(key_event.modifiers()) if key_event is not None else "",
                "input_chars": len(self.input_edit.text()),
                "worker_busy": self.worker_thread is not None,
            },
        )

    def _log_interaction_stage(self, stage: str, data: dict[str, Any] | None = None) -> None:
        if not self.active_interaction_id or self.active_interaction_started_at is None:
            return
        now = time.perf_counter()
        previous = self.active_interaction_last_at or self.active_interaction_started_at
        self.active_interaction_last_at = now
        payload: dict[str, Any] = {
            "interaction_id": self.active_interaction_id,
            "stage": stage,
            "elapsed_ms": int((now - self.active_interaction_started_at) * 1000),
            "delta_ms": int((now - previous) * 1000),
        }
        if data:
            payload.update(data)
        debug_log("Latency", "交互阶段", payload)

    def _end_interaction(self, outcome: str) -> None:
        self._log_interaction_stage("interaction_finished", {"outcome": outcome})
        self.active_interaction_id = ""
        self.active_interaction_started_at = None
        self.active_interaction_last_at = None

    @Slot()
    def _handle_return_pressed(self) -> None:
        self._begin_interaction("return_pressed")
        self.send_message("return_pressed")

    @Slot()
    def _handle_send_button_clicked(self) -> None:
        self._begin_interaction("send_button_clicked")
        self.send_message("send_button_clicked")

    @Slot()
    def send_message(self, source: str = "direct_call") -> None:
        text = self.input_edit.text().strip()
        if not self.active_interaction_id:
            self._begin_interaction(source)
        self._log_interaction_stage(
            "send_message_enter",
            {
                "source": source,
                "text": text,
                "worker_busy": self.worker_thread is not None,
            },
        )
        if not text or self.worker_thread is not None:
            debug_log(
                "PetWindow",
                "发送消息被忽略",
                {
                    "has_text": bool(text),
                    "worker_busy": self.worker_thread is not None,
                },
            )
            self._log_interaction_stage(
                "send_message_ignored",
                {
                    "has_text": bool(text),
                    "worker_busy": self.worker_thread is not None,
                },
            )
            self._end_interaction("ignored")
            return

        self._set_pending_tool_action(None)
        self.input_edit.clear()
        self._log_interaction_stage("input_cleared")
        self.reply_sequence_id += 1
        self.pending_reply_segments = []
        self._reset_current_segment_progress()
        self.set_speech("......")
        self._log_interaction_stage("placeholder_reply_shown")

        request_user_message: dict[str, Any] = {"role": "user", "content": text}

        request_messages = trim_messages_for_model([*self.messages, request_user_message])
        debug_log(
            "PetWindow",
            "用户消息入队",
            {
                "text": text,
                "history_messages": len(self.messages),
                "request_messages": summarize_messages(request_messages),
            },
        )
        self._log_interaction_stage(
            "request_messages_ready",
            {
                "history_messages": len(self.messages),
                "request_message_count": len(request_messages),
            },
        )
        self._record_user_message(text)
        self._log_interaction_stage("user_message_recorded")
        self._start_chat_worker(request_messages)

    def _start_chat_worker(self, request_messages: list[dict[str, Any]]) -> None:
        self._set_busy(True)
        self._log_interaction_stage("ui_busy_enabled")
        debug_log(
            "PetWindow",
            "启动聊天 Worker",
            {
                "message_count": len(request_messages),
                "messages": summarize_messages(request_messages),
            },
        )
        self.worker_thread = QThread(self)
        self.worker = ChatWorker(
            self.agent_runtime,
            request_messages,
        )
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.finished.connect(self._handle_reply)
        self.worker.failed.connect(self._handle_error)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker.failed.connect(self.worker_thread.quit)
        self.worker_thread.finished.connect(self._cleanup_worker)
        self.worker_thread.start()
        self._log_interaction_stage("chat_worker_started")

    @Slot(object)
    def _handle_reply(self, result: AgentResult) -> None:
        self._log_interaction_stage(
            "agent_result_received",
            {
                "segments": len(result.reply.segments),
                "actions": [action.type for action in result.actions],
                "memory_updates": len(result.memory_updates),
            },
        )
        debug_log(
            "PetWindow",
            "收到 Agent 回复",
            {
                "segments": len(result.reply.segments),
                "actions": [action.type for action in result.actions],
                "memory_updates": len(result.memory_updates),
            },
        )
        if self._queue_screen_observation_followup(result):
            self._log_interaction_stage("screen_observation_followup_queued")
            return
        reply = result.reply
        self.messages.append({"role": "assistant", "content": reply.text})
        self._record_history("assistant", reply.text, reply.translation)
        self._log_interaction_stage("assistant_message_recorded")
        self._show_reply_segments(reply.segments)
        self._apply_pending_action_from_result(result)

    def _queue_screen_observation_followup(self, result: AgentResult) -> bool:
        if not any(action.type == SCREEN_OBSERVATION_REQUEST_ACTION for action in result.actions):
            return False
        if not self.screen_observation_enabled or not self.model_vision_enabled:
            self._log_interaction_stage(
                "screen_observation_disabled",
                {
                    "screen_observation_enabled": self.screen_observation_enabled,
                    "model_vision_enabled": self.model_vision_enabled,
                },
            )
            debug_log(
                "PetWindow",
                "屏幕观察请求被禁用",
                {
                    "screen_observation_enabled": self.screen_observation_enabled,
                    "model_vision_enabled": self.model_vision_enabled,
                },
            )
            self._consume_agent_result(_build_screen_observation_disabled_result())
            return True
        if not self.messages or self.messages[-1].get("role") != "user":
            self._log_interaction_stage("screen_observation_missing_user_message")
            debug_log("PetWindow", "屏幕观察缺少可关联用户消息")
            self._consume_agent_result(_build_screen_observation_failed_result("缺少可关联的用户消息。"))
            return True

        text = str(self.messages[-1].get("content", ""))
        self.screen_observation_followup_in_progress = True
        try:
            observation = capture_screen_observation(self)
        except RuntimeError as exc:
            self.screen_observation_followup_in_progress = False
            self._log_interaction_stage("screen_observation_failed", {"error": str(exc)})
            debug_log("PetWindow", "屏幕观察失败", {"error": str(exc)})
            self._consume_agent_result(_build_screen_observation_failed_result(str(exc)))
            return True

        observed_message = build_screen_observation_user_message(text, observation)
        self.messages[-1] = {"role": "user", "content": append_observation_marker(text, observation)}
        self.pending_screen_observation_messages = trim_messages_for_model(
            [*self.messages[:-1], observed_message]
        )
        self.screen_observation_followup_in_progress = False
        debug_log(
            "PetWindow",
            "屏幕观察 follow-up 已排队",
            {
                "original_text": text,
                "width": observation.width,
                "height": observation.height,
                "captured_at": observation.captured_at,
                "screen_name": observation.screen_name,
                "image": observation.data_url,
                "message_count": len(self.pending_screen_observation_messages),
            },
        )
        self._log_interaction_stage(
            "screen_observation_captured",
            {
                "width": observation.width,
                "height": observation.height,
                "screen_name": observation.screen_name,
            },
        )
        return True

    def _record_user_message(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})
        self._record_history("user", text)

    @Slot()
    def confirm_pending_action(self) -> None:
        if self.pending_tool_action is None or self.worker_thread is not None:
            return
        self._begin_interaction("confirm_action_clicked")
        action = self.pending_tool_action
        self._log_interaction_stage("confirm_action", action.to_dict())
        self._set_pending_tool_action(None)
        self._run_action_worker(confirmed_action=action)

    @Slot()
    def cancel_pending_action(self) -> None:
        if self.pending_tool_action is None or self.worker_thread is not None:
            return
        self._begin_interaction("cancel_action_clicked")
        action = self.pending_tool_action
        self._log_interaction_stage("cancel_action", action.to_dict())
        self._set_pending_tool_action(None)
        self._run_action_worker(cancelled_action=action)

    def _run_action_worker(
        self,
        confirmed_action: PendingToolAction | None = None,
        cancelled_action: PendingToolAction | None = None,
    ) -> None:
        self._set_busy(True)
        self._log_interaction_stage(
            "action_worker_start",
            {
                "confirmed": confirmed_action.tool_name if confirmed_action is not None else "",
                "cancelled": cancelled_action.tool_name if cancelled_action is not None else "",
            },
        )
        self.worker_thread = QThread(self)
        self.worker = ChatWorker(
            self.agent_runtime,
            confirmed_action=confirmed_action,
            cancelled_action=cancelled_action,
        )
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.finished.connect(self._handle_action_reply)
        self.worker.failed.connect(self._handle_error)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker.failed.connect(self.worker_thread.quit)
        self.worker_thread.finished.connect(self._cleanup_worker)
        self.worker_thread.start()
        self._log_interaction_stage("action_worker_started")

    @Slot(object)
    def _handle_action_reply(self, result: AgentResult) -> None:
        self._log_interaction_stage(
            "action_result_received",
            {
                "segments": len(result.reply.segments),
                "actions": [action.type for action in result.actions],
            },
        )
        self._consume_agent_result(result)

    def _consume_agent_result(self, result: AgentResult, record_history: bool = True) -> None:
        reply = result.reply
        self._log_interaction_stage(
            "consume_agent_result",
            {
                "segments": len(reply.segments),
                "record_history": record_history,
            },
        )
        if record_history:
            self.messages.append({"role": "assistant", "content": reply.text})
            self._record_history("assistant", reply.text, reply.translation)
        self._show_reply_segments(reply.segments)
        self._apply_pending_action_from_result(result)

    def _apply_pending_action_from_result(self, result: AgentResult) -> None:
        for action in result.actions:
            if action.type != "pending_action":
                continue
            try:
                self._set_pending_tool_action(PendingToolAction.from_dict(action.payload))
            except ValueError as exc:
                print(f"[Tool] 待确认动作无效：{exc}")
            return
        self._set_pending_tool_action(None)

    def _set_pending_tool_action(self, action: PendingToolAction | None) -> None:
        self.pending_tool_action = action
        has_action = action is not None
        self.confirm_action_button.setVisible(has_action)
        self.cancel_action_button.setVisible(has_action)

    def _run_event_worker(self, event: AgentEvent, reminder_id: str) -> None:
        if self.worker_thread is not None or self.active_reminder_id is not None:
            return

        self._begin_interaction("reminder_due")
        self._log_interaction_stage(
            "reminder_worker_start",
            {
                "reminder_id": reminder_id,
                "event": {"type": event.type, "payload": event.payload},
            },
        )
        self.active_reminder_id = reminder_id
        self.active_reminder_text = str(event.payload.get("text", ""))
        self._set_busy(True)
        self.worker_thread = QThread(self)
        self.worker = EventWorker(
            self.agent_runtime,
            event,
        )
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.finished.connect(self._handle_event_reply)
        self.worker.failed.connect(self._handle_event_error)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker.failed.connect(self.worker_thread.quit)
        self.worker_thread.finished.connect(self._cleanup_worker)
        self.worker_thread.start()
        self._log_interaction_stage("reminder_worker_started")

    @Slot(object)
    def _handle_event_reply(self, result: AgentResult) -> None:
        self._log_interaction_stage("reminder_result_received", {"segments": len(result.reply.segments)})
        reminder_id = self.active_reminder_id
        self._clear_active_reminder()
        self._consume_agent_result(result)
        if reminder_id is not None:
            self._mark_reminder_completed(reminder_id)

    @Slot(str)
    def _handle_event_error(self, message: str) -> None:
        self._log_interaction_stage("reminder_error", {"message": message})
        reminder_id = self.active_reminder_id
        reminder_text = self.active_reminder_text
        self._clear_active_reminder()
        print(f"[Reminder] 生成提醒失败：{message}")
        result = AgentResult(
            reply=ChatReply(
                [
                    ChatSegment(
                        text=f"時間だよ。{reminder_text}",
                        tone="提醒",
                        translation=f"到时间了：{reminder_text}",
                    )
                ]
            )
        )
        self._consume_agent_result(result)
        if reminder_id is not None:
            self._mark_reminder_completed(reminder_id)

    def _clear_active_reminder(self) -> None:
        self.active_reminder_id = None
        self.active_reminder_text = ""

    def _mark_reminder_completed(self, reminder_id: str) -> None:
        try:
            self.reminder_store.mark_completed(reminder_id)
        except ValueError as exc:
            print(f"[Reminder] 标记完成失败：{exc}")

    @Slot(str)
    def _handle_error(self, message: str) -> None:
        self._log_interaction_stage("worker_error", {"message": message})
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages.pop()
        self._record_history("error", message)
        self._reset_current_segment_progress()
        self.set_speech("……通信に失敗した。設定を確認して。")
        QMessageBox.warning(self, "请求失败", message)
        self._end_interaction("error")

    @Slot()
    def _cleanup_worker(self) -> None:
        self._log_interaction_stage(
            "cleanup_worker_enter",
            {
                "has_pending_screen_observation": self.pending_screen_observation_messages is not None,
                "screen_observation_followup_in_progress": self.screen_observation_followup_in_progress,
            },
        )
        if self.worker is not None:
            self.worker.deleteLater()
        if self.worker_thread is not None:
            self.worker_thread.deleteLater()
        self.worker = None
        self.worker_thread = None
        if self.screen_observation_followup_in_progress:
            self._log_interaction_stage("screen_observation_cleanup_deferred")
            QTimer.singleShot(0, self._cleanup_worker)
            return
        if self.pending_screen_observation_messages is not None:
            request_messages = self.pending_screen_observation_messages
            self.pending_screen_observation_messages = None
            self._log_interaction_stage(
                "screen_observation_worker_restart",
                {"message_count": len(request_messages)},
            )
            self._start_chat_worker(request_messages)
            return
        self._set_busy(False)
        self._log_interaction_stage("ui_busy_disabled")

    def _set_busy(self, busy: bool) -> None:
        self.input_edit.setEnabled(not busy)
        self.send_button.setEnabled(not busy)
        self.confirm_action_button.setEnabled(not busy)
        self.cancel_action_button.setEnabled(not busy)
        self.send_button.setText("等待" if busy else "发送")
        self._log_interaction_stage("set_busy", {"busy": busy})

    @Slot(str)
    def set_speech(self, text: str) -> None:
        cleaned = " ".join(text.split())
        self.speech_timer.stop()
        self.speech_text = cleaned
        self.speech_index = 0
        self.speech_label.clear()
        if self.speech_text:
            self.speech_timer.start()
        self._log_interaction_stage("speech_text_started", {"text": cleaned})

    @Slot()
    def _show_next_speech_char(self) -> None:
        if self.speech_index >= len(self.speech_text):
            self.speech_timer.stop()
            return

        self.speech_index += 1
        self.speech_label.setText(self.speech_text[: self.speech_index])
        if self.speech_index >= len(self.speech_text):
            self.speech_timer.stop()
            if self.current_segment_sequence_id is not None:
                self._mark_segment_speech_done(self.current_segment_sequence_id)

    def toggle_visible(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()

    @Slot()
    def show_history(self) -> None:
        if self.history_window is None:
            self.history_window = HistoryWindow(
                self.history_store,
                self.subtitle_language,
                self,
            )
        self.history_window.set_subtitle_language(self.subtitle_language)
        self.history_window.refresh()
        self.history_window.show()
        self.history_window.raise_()
        self.history_window.activateWindow()

    @Slot()
    def show_settings(self) -> None:
        try:
            tts_settings = GPTSoVITSTTSSettings.load(
                self.env_path,
                self.base_dir,
                validate_enabled=False,
                character_profile=self.character_profile,
            )
        except (OSError, TTSConfigError) as exc:
            QMessageBox.warning(self, "配置读取失败", f"TTS 配置读取失败，将使用默认值打开设置：{exc}")
            tts_settings = self._default_tts_settings()

        dialog = SettingsDialog(
            self.api_client.settings,
            tts_settings,
            self.base_dir,
            self.character_registry,
            self.character_profile,
            self.screen_observation_enabled,
            self,
        )
        if (
            dialog.exec() != QDialog.DialogCode.Accepted
            or dialog.result_api_settings is None
            or dialog.result_tts_settings is None
            or dialog.result_character_id is None
            or dialog.result_screen_observation_enabled is None
        ):
            return

        try:
            selected_profile = self.character_registry.get(dialog.result_character_id)
        except CharacterConfigError as exc:
            QMessageBox.critical(self, "角色配置无效", str(exc))
            return

        new_tts_provider = self._create_tts_provider_from_settings(dialog.result_tts_settings)
        if new_tts_provider is None:
            return

        try:
            dialog.result_api_settings.save(self.env_path)
            dialog.result_tts_settings.save(self.env_path, self.base_dir)
            self.character_registry.save_current_id(self.env_path, selected_profile.id)
            save_env_values(
                self.env_path,
                {SCREEN_OBSERVATION_ENABLED_KEY: _format_bool(dialog.result_screen_observation_enabled)},
            )
        except OSError as exc:
            QMessageBox.critical(self, "保存失败", f"无法保存设置：{exc}")
            return

        self.api_client.update_settings(dialog.result_api_settings)
        previous_screen_observation_enabled = self.screen_observation_enabled
        self.screen_observation_enabled = dialog.result_screen_observation_enabled
        if not self.screen_observation_enabled:
            self._set_model_vision_enabled(False)
        elif not previous_screen_observation_enabled:
            self._set_model_vision_enabled(True)
        self._discard_prepared_next_tts()
        self.retired_tts_providers.append(self.tts_provider)
        self.tts_provider = new_tts_provider
        self._apply_character(selected_profile)
        if hasattr(self, "tray_icon"):
            self.tray_icon.setContextMenu(self._build_menu())
        QMessageBox.information(self, "保存成功", "设置已保存，后续聊天和朗读将使用新配置。")

    @Slot(bool)
    def _toggle_chinese_subtitles(self, checked: bool) -> None:
        next_language = SUBTITLE_LANGUAGE_ZH if checked else SUBTITLE_LANGUAGE_JA
        if next_language == self.subtitle_language:
            return

        previous_language = self.subtitle_language
        self.subtitle_language = next_language
        try:
            save_env_values(self.env_path, {SUBTITLE_LANGUAGE_KEY: next_language})
        except OSError as exc:
            self.subtitle_language = previous_language
            self._apply_speech_font()
            QMessageBox.warning(self, "保存失败", f"无法保存字幕设置：{exc}")
            return

        self._apply_speech_font()
        self._restart_current_segment_speech()
        if self.history_window is not None:
            self.history_window.set_subtitle_language(self.subtitle_language)

    @Slot(bool)
    def _toggle_model_vision(self, checked: bool) -> None:
        self._set_model_vision_enabled(checked)

    def _set_model_vision_enabled(self, enabled: bool) -> None:
        enabled = enabled and self.screen_observation_enabled
        self.model_vision_enabled = enabled
        self.agent_runtime.set_model_vision_enabled(enabled)
        if hasattr(self, "tray_icon"):
            self.tray_icon.setContextMenu(self._build_menu())

    @Slot(bool)
    def _toggle_free_access(self, checked: bool) -> None:
        self.free_access_enabled = checked
        self.tool_registry.set_free_access_enabled(checked)
        if hasattr(self, "tray_icon"):
            self.tray_icon.setContextMenu(self._build_menu())

    def _create_tts_provider_from_settings(
        self,
        settings: GPTSoVITSTTSSettings,
    ) -> TTSProvider | None:
        if not settings.enabled:
            debug_log("PetWindow", "设置保存后 TTS 保持关闭")
            return NullTTSProvider()
        try:
            provider = GPTSoVITSTTSProvider(settings)
            debug_log(
                "PetWindow",
                "设置保存后 TTS Provider 已创建",
                {
                    "api_url": settings.api_url,
                    "timeout_seconds": settings.timeout_seconds,
                },
            )
            return provider
        except TTSConfigError as exc:
            debug_log("PetWindow", "TTS 配置无效", {"error": str(exc)})
            QMessageBox.critical(self, "TTS 配置无效", f"无法启用 TTS，当前语音配置保持不变：{exc}")
            return None

    def _default_tts_settings(self) -> GPTSoVITSTTSSettings:
        if self.character_profile.voice is not None:
            return GPTSoVITSTTSSettings.from_character_profile(
                character_profile=self.character_profile,
                enabled=False,
                api_url="http://127.0.0.1:9880/tts",
                ref_lang=self.character_profile.voice.ref_lang,
                text_lang=self.character_profile.voice.text_lang,
                timeout_seconds=60,
                validate_enabled=False,
            )
        return GPTSoVITSTTSSettings(
            enabled=False,
            api_url="http://127.0.0.1:9880/tts",
            ref_audio_path=self.base_dir / "ref" / "VO01_2210.ogg",
            ref_text_path=self.base_dir / "ref" / "text.txt",
            ref_text="",
            ref_lang="ja",
            text_lang="ja",
            timeout_seconds=60,
        )

    def _record_history(self, role: str, content: str, translation: str = "") -> None:
        try:
            self.history_store.append(role, content, translation)
        except OSError as exc:
            print(f"[History] 写入失败：{exc}")
            debug_log(
                "History",
                "写入失败",
                {
                    "role": role,
                    "content": content,
                    "translation": translation,
                    "error": str(exc),
                },
            )

    @Slot()
    def _check_due_reminders(self) -> None:
        if self.worker_thread is not None or self.active_reminder_id is not None:
            return
        try:
            due_reminders = self.reminder_store.due_reminders()
        except ValueError as exc:
            print(f"[Reminder] 读取失败：{exc}")
            debug_log("Reminder", "读取失败", {"error": str(exc)})
            return
        if not due_reminders:
            return

        reminder = due_reminders[0]
        reminder_id = str(reminder.get("id", ""))
        reminder_text = str(reminder.get("text", ""))
        reminder_trigger_at = str(reminder.get("trigger_at", ""))
        if not reminder_id:
            debug_log("Reminder", "跳过缺少 id 的到期提醒", {"reminder": reminder})
            return
        debug_log(
            "Reminder",
            "触发到期提醒",
            {
                "id": reminder_id,
                "text": reminder_text,
                "trigger_at": reminder_trigger_at,
                "due_count": len(due_reminders),
            },
        )
        self._run_event_worker(
            AgentEvent(
                type="reminder_due",
                payload={
                    "id": reminder_id,
                    "text": reminder_text,
                    "trigger_at": reminder_trigger_at,
                },
            ),
            reminder_id,
        )

    def _show_reply_segments(self, segments: list[ChatSegment]) -> None:
        self.reply_sequence_id += 1
        self.pending_reply_segments = [segment for segment in segments if segment.text.strip()]
        self._log_interaction_stage(
            "reply_segments_ready",
            {
                "sequence_id": self.reply_sequence_id,
                "segment_count": len(self.pending_reply_segments),
            },
        )
        debug_log(
            "PetWindow",
            "准备分段展示回复",
            {
                "sequence_id": self.reply_sequence_id,
                "segments": [
                    {
                        "text": segment.text,
                        "tone": segment.tone,
                        "translation": segment.translation,
                    }
                    for segment in self.pending_reply_segments
                ],
            },
        )
        self._reset_current_segment_progress()
        self._show_next_reply_segment(self.reply_sequence_id)

    def _show_next_reply_segment(self, sequence_id: int) -> None:
        if sequence_id != self.reply_sequence_id or not self.pending_reply_segments:
            return

        segment = self.pending_reply_segments.pop(0)
        debug_log(
            "PetWindow",
            "展示下一段回复",
            {
                "sequence_id": sequence_id,
                "text": segment.text,
                "tone": segment.tone,
                "remaining_segments": len(self.pending_reply_segments),
            },
        )
        self.current_segment = segment
        self.current_segment_sequence_id = sequence_id
        self.current_segment_speech_done = False
        self.current_segment_tts_done = False
        self.reply_advance_scheduled = False
        self._preload_portrait_for_tone(segment.tone)
        prepared_tts = self._take_prepared_tts_for_segment(segment)
        if prepared_tts is None:
            self._log_interaction_stage("tts_speak_requested", {"sequence_id": sequence_id, "tone": segment.tone})
            self.tts_provider.speak(
                segment.text,
                segment.tone,
                on_finished=lambda: self._mark_segment_tts_done(sequence_id),
                on_started=lambda: self._start_segment_speech(sequence_id),
            )
        else:
            self._log_interaction_stage(
                "tts_prepared_speak_requested",
                {"sequence_id": sequence_id, "tone": segment.tone},
            )
            self.tts_provider.speak_prepared(
                prepared_tts,
                on_started=lambda: self._start_segment_speech(sequence_id),
                on_finished=lambda: self._mark_segment_tts_done(sequence_id),
            )
        self._prepare_next_reply_segment()

    def _start_segment_speech(self, sequence_id: int) -> None:
        if (
            sequence_id != self.reply_sequence_id
            or sequence_id != self.current_segment_sequence_id
            or self.current_segment is None
        ):
            return
        self._log_interaction_stage(
            "segment_speech_started",
            {
                "sequence_id": sequence_id,
                "tone": self.current_segment.tone,
            },
        )
        self._apply_portrait_for_tone(self.current_segment.tone)
        self.set_speech(self.current_segment.display_text(self.subtitle_language))

    def _mark_segment_speech_done(self, sequence_id: int) -> None:
        if sequence_id != self.reply_sequence_id or sequence_id != self.current_segment_sequence_id:
            return
        self.current_segment_speech_done = True
        self._log_interaction_stage("segment_text_render_done", {"sequence_id": sequence_id})
        self._end_interaction_if_reply_done()
        self._schedule_next_reply_segment_if_ready(sequence_id)

    def _mark_segment_tts_done(self, sequence_id: int) -> None:
        if sequence_id != self.reply_sequence_id or sequence_id != self.current_segment_sequence_id:
            return
        self.current_segment_tts_done = True
        self._log_interaction_stage("segment_tts_done", {"sequence_id": sequence_id})
        self._end_interaction_if_reply_done()
        self._schedule_next_reply_segment_if_ready(sequence_id)

    def _schedule_next_reply_segment_if_ready(self, sequence_id: int) -> None:
        if (
            sequence_id != self.reply_sequence_id
            or sequence_id != self.current_segment_sequence_id
            or self.reply_advance_scheduled
            or not self.current_segment_speech_done
            or not self.current_segment_tts_done
            or not self.pending_reply_segments
        ):
            return

        self.reply_advance_scheduled = True
        self.reply_advance_token += 1
        reply_advance_token = self.reply_advance_token
        self._log_interaction_stage(
            "next_segment_scheduled",
            {
                "sequence_id": sequence_id,
                "delay_ms": REPLY_SEGMENT_PAUSE_MS,
                "remaining_segments": len(self.pending_reply_segments),
            },
        )
        QTimer.singleShot(
            REPLY_SEGMENT_PAUSE_MS,
            lambda: self._show_scheduled_next_reply_segment(sequence_id, reply_advance_token),
        )

    def _show_scheduled_next_reply_segment(self, sequence_id: int, reply_advance_token: int) -> None:
        if reply_advance_token != self.reply_advance_token:
            return
        self._log_interaction_stage("next_segment_timer_fired", {"sequence_id": sequence_id})
        self._show_next_reply_segment(sequence_id)

    def _end_interaction_if_reply_done(self) -> None:
        if (
            self.active_interaction_id
            and self.current_segment_speech_done
            and self.current_segment_tts_done
            and not self.pending_reply_segments
        ):
            self._end_interaction("reply_completed")

    def _reset_current_segment_progress(self) -> None:
        self._discard_prepared_next_tts()
        self.current_segment = None
        self.reply_advance_token += 1
        self.current_segment_sequence_id = None
        self.current_segment_speech_done = False
        self.current_segment_tts_done = True
        self.reply_advance_scheduled = False

    def _prepare_next_reply_segment(self) -> None:
        if not self.pending_reply_segments:
            self._discard_prepared_next_tts()
            return

        next_segment = self.pending_reply_segments[0]
        if self.prepared_next_segment is next_segment and self.prepared_next_tts is not None:
            return

        self._discard_prepared_next_tts()
        self.prepared_next_segment = next_segment
        self._log_interaction_stage(
            "next_segment_tts_prepare_requested",
            {
                "text": next_segment.text,
                "tone": next_segment.tone,
            },
        )
        debug_log(
            "PetWindow",
            "预生成下一段 TTS",
            {
                "text": next_segment.text,
                "tone": next_segment.tone,
            },
        )
        self.prepared_next_tts = self.tts_provider.prepare(
            next_segment.text,
            next_segment.tone,
        )

    def _take_prepared_tts_for_segment(
        self,
        segment: ChatSegment,
    ) -> TTSPreparedAudio | None:
        if self.prepared_next_segment is not segment:
            return None

        prepared_tts = self.prepared_next_tts
        self.prepared_next_segment = None
        self.prepared_next_tts = None
        return prepared_tts

    def _discard_prepared_next_tts(self) -> None:
        if self.prepared_next_tts is not None:
            self.tts_provider.discard_prepared(self.prepared_next_tts)
        self.prepared_next_segment = None
        self.prepared_next_tts = None

    def _restart_current_segment_speech(self) -> None:
        if self.current_segment_sequence_id is None or self.current_segment is None:
            return

        self.reply_advance_token += 1
        self.current_segment_speech_done = False
        self.reply_advance_scheduled = False
        self.set_speech(self.current_segment.display_text(self.subtitle_language))

    def _load_subtitle_language(self) -> str:
        try:
            values = load_env_file(self.env_path)
        except OSError:
            return SUBTITLE_LANGUAGE_JA

        language = values.get(SUBTITLE_LANGUAGE_KEY, SUBTITLE_LANGUAGE_JA).strip().lower()
        if language == SUBTITLE_LANGUAGE_ZH:
            return SUBTITLE_LANGUAGE_ZH
        return SUBTITLE_LANGUAGE_JA

    def _load_screen_observation_enabled(self) -> bool:
        try:
            values = load_env_file(self.env_path)
        except OSError:
            debug_log("PetWindow", "屏幕观察配置读取失败，使用默认值", {"default": True})
            return True

        enabled = _parse_bool(values.get(SCREEN_OBSERVATION_ENABLED_KEY), default=True)
        debug_log("PetWindow", "屏幕观察配置已加载", {"enabled": enabled})
        return enabled

    def _apply_character(self, profile: CharacterProfile) -> None:
        previous_character_id = self.character_profile.id
        self.character_profile = profile
        self.portrait_path = profile.default_portrait_path
        self.system_prompt = load_character_system_prompt(profile)
        self.agent_runtime.update_character(self.system_prompt, profile.reply_tones)
        self.setWindowTitle(profile.display_name)
        self.name_label.setText(profile.display_name)
        self.input_edit.setPlaceholderText(f"{profile.display_name}に話しかける...")
        self.pixmap = self._load_portrait()
        self._apply_portrait()
        if hasattr(self, "tray_icon"):
            self.tray_icon.setToolTip(profile.display_name)
            self.tray_icon.setIcon(QIcon(self.pixmap) if not self.pixmap.isNull() else QIcon())

        self.history_store = self._create_history_store(profile)
        if self.history_window is not None:
            self.history_window.set_history_store(self.history_store, profile.display_name)

        if profile.id != previous_character_id:
            self.messages = []
            self.reply_sequence_id += 1
            self.pending_reply_segments = []
            self._reset_current_segment_progress()
            self.set_speech(profile.initial_message)

    def _create_history_store(self, profile: CharacterProfile) -> ChatHistoryStore:
        history_path = self.base_dir / "data" / "chat_history" / f"{profile.id}.jsonl"
        self._migrate_legacy_history(profile, history_path)
        return ChatHistoryStore(history_path, profile.display_name)

    def _migrate_legacy_history(self, profile: CharacterProfile, history_path: Path) -> None:
        if profile.id != DEFAULT_CHARACTER_ID or history_path.exists():
            return
        legacy_path = self.base_dir / "data" / "chat_history.jsonl"
        if not legacy_path.exists():
            return
        try:
            history_path.parent.mkdir(parents=True, exist_ok=True)
            history_path.write_text(legacy_path.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError as exc:
            print(f"[History] 旧历史迁移失败：{exc}")


def _build_screen_observation_disabled_result() -> AgentResult:
    return AgentResult(
        reply=ChatReply(
            [
                ChatSegment(
                    text="画面を見る設定がオフになっているよ。設定で許可してから、もう一度頼んで。",
                    tone="提醒",
                    translation="屏幕观察现在是关闭的。请在设置里允许按需屏幕观察后再试。",
                )
            ]
        )
    )


def _build_screen_observation_failed_result(message: str) -> AgentResult:
    return AgentResult(
        reply=ChatReply(
            [
                ChatSegment(
                    text="今は画面を取得できなかったみたい。権限や表示環境を確認して。",
                    tone="困惑",
                    translation=f"这次没能获取屏幕截图：{message}",
                )
            ]
        )
    )


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _format_bool(value: bool) -> str:
    return "true" if value else "false"


def _rounded_japanese_font(point_size: int, weight: QFont.Weight) -> QFont:
    family = _choose_font_family([
        "BIZ UDPGothic",
        "Meiryo",
        "Yu Gothic UI",
        "Yu Gothic",
        "MS PGothic",
        "Microsoft YaHei UI",
        "Segoe UI",
    ])
    font = QFont(family)
    font.setPointSize(point_size)
    font.setWeight(weight)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return font


def _rounded_chinese_font(point_size: int, weight: QFont.Weight) -> QFont:
    family = _choose_font_family([
        "Microsoft YaHei UI",
        "Microsoft YaHei",
        "Noto Sans CJK SC",
        "Source Han Sans SC",
        "SimHei",
        "Segoe UI",
    ])
    font = QFont(family)
    font.setPointSize(point_size)
    font.setWeight(weight)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return font


def _choose_font_family(candidates: list[str]) -> str:
    available = set(QFontDatabase.families())
    for candidate in candidates:
        if candidate in available:
            return candidate
    return candidates[-1]
