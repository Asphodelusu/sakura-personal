from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

try:
    from .boundary import MemoryBoundary, _project_memory
    from .memory import MEMORY_LAYERS, VECTOR_MEMORY_LAYERS
    from .memory_recall import MemoryRecallService
    from .domain_types import ContextMessage, ContextRequest
    from .support import bind_logger, log_event
except ImportError:
    from boundary import MemoryBoundary, _project_memory
    from memory import MEMORY_LAYERS, VECTOR_MEMORY_LAYERS
    from memory_recall import MemoryRecallService
    from domain_types import ContextMessage, ContextRequest
    from support import bind_logger, log_event


PLUGIN_ID = "sakura.memory.mem0"
MEMORY_CONTEXT_PROVIDER_ID = "sakura.memory.mem0.recall"
# Covers the recall budget (RECALL_BUDGET_SECONDS) plus the profile and mood reads.
MEMORY_CONTEXT_TIMEOUT_SECONDS = 6
MEMORY_SETTINGS_SECTION_ID = "memory"
MEMORY_COMPONENT_SECTION_ID = "memory_embedding_component"
MEMORY_MANAGEMENT_SECTION_ID = "memory_management"
MEMORY_COLLECTION_ID = "memories"
HOST_CHAT_COMPLETED_EVENT = "sakura.host.chat.completed"
_MAX_COLLECTION_ITEMS = 10_000


class SakuraMem0Runtime:
    """Plugin-owned facade over the existing generation-private Memory runtime."""

    def __init__(
        self,
        app_root: Path,
        character_id: str,
        *,
        system_prompt: str = "",
        boundary: MemoryBoundary | None = None,
        timeline: object | None = None,
        config_getter: Callable[[], Mapping[str, object]] | None = None,
        config_updater: Callable[[Mapping[str, object]], object] | None = None,
        memory_dir: Path | None = None,
        memory_cache_dir: Path | None = None,
        model_catalog_getter: Callable[[], object] | None = None,
        model_resolver: Callable[[Mapping[str, object]], object] | None = None,
    ) -> None:
        self._app_root = Path(app_root)
        self._character_id = character_id
        self._config_getter = config_getter or (lambda: {})
        self._config_updater = config_updater or (lambda _values: None)
        self._timeline = timeline
        self._boundary = boundary or MemoryBoundary(
            self._app_root,
            character_id,
            system_prompt=system_prompt,
            memory_dir=memory_dir,
            memory_cache_dir=memory_cache_dir,
            curation_config_getter=self._config_getter,
            model_catalog_getter=model_catalog_getter,
            model_resolver=model_resolver,
        )
        self._recall = MemoryRecallService(self._boundary)
        self._task_lock = threading.RLock()
        self._model_task_id = ""
        self._model_task_state = "idle"
        self._model_task_stage = ""
        self._model_task_progress: int | None = None
        self._model_task_error_code = ""
        self._model_task_thread: threading.Thread | None = None
        self._closed = False

    @property
    def character_id(self) -> str:
        return self._character_id

    def context(self, request: object) -> list[dict[str, object]]:
        context_request = _context_request(request)
        if context_request.character_id != self._character_id:
            return []
        self._note_user_input(context_request)
        profile = self._profile_fragment()
        continuity = self._continuity_fragments()
        recalled = self._recall.recall(context_request)
        fragments = [
            {
                "id": fragment.fragment_id,
                "content": fragment.content,
                "priority": fragment.priority,
                "budgetHint": fragment.token_budget,
                "sensitivity": fragment.sensitivity,
            }
            for fragment in recalled.fragments
        ]
        if profile is None:
            return [*continuity, *fragments]
        return [profile, *continuity, *_without_profile_duplicate(fragments, profile)]

    def _note_user_input(self, request: ContextRequest) -> None:
        note = getattr(self._boundary, "note_user_input", None)
        turn_id = request.current_turn_id
        if (
            not callable(note)
            or request.source != "chat"
            or not request.human_entry_id
            or not request.current_input.strip()
            or not turn_id
            or turn_id == getattr(self, "_emotion_turn_id", "")
        ):
            return
        self._emotion_turn_id = turn_id
        try:
            note(request.current_input)
        except Exception:
            pass

    def _continuity_fragments(self) -> list[dict[str, object]]:
        reader = getattr(self._boundary, "continuity_fragments", None)
        if not callable(reader):
            return []
        try:
            raw = reader()
        except Exception:
            return []
        allowed = (f"mood:{self._character_id}", f"user_emotion:{self._character_id}")
        return [
            {
                "id": item["id"],
                "content": item["content"],
                "priority": item["priority"] if type(item.get("priority")) is int else 85,
                "budgetHint": item["budgetHint"] if type(item.get("budgetHint")) is int else 400,
                "sensitivity": "private",
            }
            for item in (raw if isinstance(raw, list) else [])
            if isinstance(item, dict)
            and item.get("id") in allowed
            and isinstance(item.get("content"), str)
            and item["content"].strip()
            and len(item["content"]) <= 1200
        ]

    def _profile_fragment(self) -> dict[str, object] | None:
        reader = getattr(self._boundary, "core_profile_fragment", None)
        if not callable(reader):
            return None
        try:
            fragment = reader()
        except Exception:
            log_event(
                "Memory",
                "个人常驻档案不可读",
                {"code": "CORE_PROFILE_UNREADABLE"},
                event="memory.personal.core_profile_unreadable",
                severity="warning",
            )
            return None
        if not isinstance(fragment, dict):
            return None
        content = fragment.get("content")
        fragment_id = fragment.get("id")
        if (
            not isinstance(content, str)
            or not content.strip()
            or len(content) > 1200
            or fragment.get("sensitivity") != "private"
            or fragment_id != f"core_profile:{self._character_id}"
        ):
            return None
        priority = fragment.get("priority")
        budget = fragment.get("budgetHint")
        return {
            "id": fragment_id,
            "content": content,
            "priority": priority if type(priority) is int else 90,
            "budgetHint": budget if type(budget) is int else 1200,
            "sensitivity": "private",
        }

    def search_tool(self, arguments: Mapping[str, object]) -> dict[str, object]:
        return self._boundary.search_memory(dict(arguments), wait=False)

    def remember_tool(self, arguments: Mapping[str, object]) -> dict[str, object]:
        return self._boundary.upsert({**dict(arguments), "source": "explicit"})

    def update_tool(self, arguments: Mapping[str, object]) -> dict[str, object]:
        values = {key: value for key, value in arguments.items() if key != "memory_id"}
        values.update({"id": arguments.get("memory_id"), "source": "explicit"})
        return self._boundary.upsert(values)

    def forget_tool(self, arguments: Mapping[str, object]) -> dict[str, object]:
        return self._boundary.delete({"id": arguments.get("memory_id")})

    def _personal(self, name: str, arguments: Mapping[str, object]) -> dict[str, object]:
        return getattr(self._boundary, "memory_tool")(name, dict(arguments))

    def personal_search_tool(self, arguments: Mapping[str, object]) -> dict[str, object]:
        return self._personal("search", arguments)

    def personal_detail_tool(self, arguments: Mapping[str, object]) -> dict[str, object]:
        return self._personal("detail", arguments)

    def personal_timeline_tool(self, arguments: Mapping[str, object]) -> dict[str, object]:
        return self._personal("timeline", arguments)

    def personal_remember_tool(self, arguments: Mapping[str, object]) -> dict[str, object]:
        return self._personal("remember", arguments)

    def personal_update_tool(self, arguments: Mapping[str, object]) -> dict[str, object]:
        return self._personal("update", arguments)

    def personal_forget_tool(self, arguments: Mapping[str, object]) -> dict[str, object]:
        return self._personal("forget", arguments)

    def personal_let_go_tool(self, arguments: Mapping[str, object]) -> dict[str, object]:
        return self._personal("let_go", arguments)

    def _combined_settings_descriptor(self) -> dict[str, object]:
        return {
            "sectionId": MEMORY_SETTINGS_SECTION_ID,
            "title": "长期记忆",
            "order": 40,
            "fields": [
                {
                    "key": "status",
                    "label": "运行状态",
                    "type": "status",
                    "placement": "section_header",
                    "default": {
                        "state": "neutral",
                        "label": "状态未知",
                        "message": "",
                    },
                },
                {
                    "key": "triggerTurns",
                    "label": "自动整理间隔（轮）",
                    "type": "integer",
                    "default": 8,
                    "minimum": 1,
                    "maximum": 50,
                    "step": 1,
                },
                {
                    "key": "embeddingResource",
                    "label": "本地向量模型",
                    "type": "resource",
                    "actionIds": [
                        "downloadEmbedding",
                        "retryEmbedding",
                        "cancelEmbedding",
                    ],
                    "default": {
                        "applicability": "required",
                        "subtitle": "",
                        "ready": False,
                        "taskState": "idle",
                        "message": "",
                        "detail": "",
                        "progress": None,
                        "availableActionIds": [],
                    },
                },
            ],
            "actions": [
                {
                    "actionId": "downloadEmbedding",
                    "label": "下载本地模型",
                },
                {
                    "actionId": "retryEmbedding",
                    "label": "重试",
                },
                {
                    "actionId": "cancelEmbedding",
                    "label": "取消下载",
                },
            ],
            "collections": [
                {
                    "collectionId": MEMORY_COLLECTION_ID,
                    "title": "记忆条目",
                    "description": "当前角色的长期记忆。",
                    "columns": [
                        {
                            "key": "content",
                            "label": "内容",
                            "type": "string",
                            "maxLength": 16_384,
                        },
                        {"key": "layer", "label": "分层", "type": "string"},
                        {"key": "category", "label": "类别", "type": "string"},
                        {"key": "source", "label": "来源", "type": "string"},
                        {"key": "importance", "label": "重要度", "type": "number"},
                        {"key": "confidence", "label": "置信度", "type": "number"},
                        {"key": "updatedAt", "label": "更新时间", "type": "datetime"},
                    ],
                    "fields": [
                        {
                            "key": "content",
                            "label": "内容",
                            "type": "text",
                            "default": None,
                            "required": True,
                            "maxLength": 16_384,
                        },
                        {
                            "key": "layer",
                            "label": "分层",
                            "type": "select",
                            "default": "semantic",
                            "required": True,
                            "options": _layer_options(),
                        },
                        {
                            "key": "category",
                            "label": "类别",
                            "type": "text",
                            "default": "",
                        },
                        {
                            "key": "source",
                            "label": "来源",
                            "type": "text",
                            "default": "explicit",
                        },
                        {
                            "key": "importance",
                            "label": "重要度",
                            "type": "number",
                            "default": 0.5,
                            "minimum": 0,
                            "maximum": 1,
                            "step": 0.05,
                        },
                        {
                            "key": "confidence",
                            "label": "置信度",
                            "type": "number",
                            "default": 0.8,
                            "minimum": 0,
                            "maximum": 1,
                            "step": 0.05,
                        },
                    ],
                    "filters": [
                        {"key": "layer", "label": "分层", "options": _layer_options()},
                    ],
                    "searchable": True,
                    "pageSize": 25,
                    "deleteConfirmation": "确定删除这条长期记忆吗？此操作不能撤销。",
                },
            ],
        }

    def settings_descriptor(self) -> dict[str, object]:
        descriptor = self._combined_settings_descriptor()
        descriptor.pop("collections", None)
        descriptor["fields"] = [
            field for field in descriptor["fields"]
            if field["key"] != "embeddingResource"
        ]
        descriptor["actions"] = []
        return descriptor

    def component_descriptor(self) -> dict[str, object]:
        combined = self._combined_settings_descriptor()
        resource = next(
            field for field in combined["fields"]
            if field["key"] == "embeddingResource"
        )
        return {
            "sectionId": MEMORY_COMPONENT_SECTION_ID,
            "title": "Mem0 长期记忆",
            "order": 40,
            "fields": [resource],
            "actions": combined["actions"],
        }

    def memory_management_descriptor(self) -> dict[str, object]:
        return {
            "sectionId": MEMORY_MANAGEMENT_SECTION_ID,
            "title": "记忆管理",
            "order": 10,
            "fields": [],
            "actions": [],
        }

    def memory_collection_descriptor(self) -> dict[str, object]:
        return dict(self._combined_settings_descriptor()["collections"][0])

    def load_settings(self) -> dict[str, object]:
        snapshot = self._boundary.settings_get()
        curation = _mapping(snapshot.get("curation"))
        slot = _mapping(snapshot.get("curationModelSlot"))
        embedding = _mapping(snapshot.get("embedding"))
        status = str(snapshot.get("status", "degraded"))
        message = str(snapshot.get("message", "")).strip()
        return {
            "status": _runtime_status_value(status, message),
            "triggerTurns": int(curation.get("triggerTurns", 8)),
        }

    def load_component_settings(self) -> dict[str, object]:
        embedding = _mapping(self._boundary.settings_get().get("embedding"))
        return {"embeddingResource": self._embedding_resource_value(embedding)}

    def save_settings(self, values: Mapping[str, object]) -> dict[str, str]:
        current = self.load_settings()
        self._config_updater(
            {
                "triggerTurns": values.get("triggerTurns", current["triggerTurns"]),
            }
        )
        return {"applicationState": "applied"}

    def load_model_slot(self) -> dict[str, str]:
        slot = _mapping(self._boundary.settings_get().get("curationModelSlot"))
        return {
            "profileId": str(slot.get("profileId", "")),
            "model": str(slot.get("model", "")),
        }

    def save_model_slot(self, selection: Mapping[str, object]) -> dict[str, str]:
        parsed = _parse_model_slot_selection(selection)
        self._config_updater(
            {
                "curationProfileId": parsed["profileId"],
                "curationModel": parsed["model"],
            }
        )
        return {"applicationState": "applied"}

    def start_model_download(self, _values: Mapping[str, object]) -> dict[str, object]:
        with self._task_lock:
            if self._closed:
                raise RuntimeError("MEMORY_STOPPED")
            if self._model_task_thread is not None and self._model_task_thread.is_alive():
                return {"values": self.load_component_settings(), "message": "模型下载已在进行中。"}
            task_id = f"memory-model-{uuid.uuid4().hex}"
            self._model_task_id = task_id
            self._model_task_state = "queued"
            self._model_task_stage = "等待下载"
            self._model_task_progress = None
            self._model_task_error_code = ""
            self._boundary.begin_model_download(task_id)

            def run() -> None:
                try:
                    with self._task_lock:
                        if self._model_task_id == task_id:
                            self._model_task_state = "running"
                    state = self._boundary.run_model_download(
                        task_id,
                        progress=self._record_model_progress,
                    )
                except Exception:
                    state = "failed"
                with self._task_lock:
                    if self._model_task_id == task_id:
                        self._model_task_state = (
                            "succeeded" if state == "completed" else state
                        )
                        if state == "completed":
                            self._model_task_stage = "安装完成"
                            self._model_task_progress = 100
                            self._model_task_error_code = ""
                        elif state == "failed":
                            reader = getattr(
                                self._boundary,
                                "model_download_error_code",
                                None,
                            )
                            code = reader() if callable(reader) else ""
                            self._model_task_error_code = (
                                str(code) if code else "DOWNLOAD_FAILED"
                            )

            thread = threading.Thread(
                target=run,
                name="sakura-mem0-model-download",
                daemon=True,
            )
            self._model_task_thread = thread
            thread.start()
        return {"values": self.load_component_settings(), "message": "已开始下载模型。"}

    def cancel_model_download(self, _values: Mapping[str, object]) -> dict[str, object]:
        with self._task_lock:
            task_id = self._model_task_id
        result = self._boundary.model_cancel({"taskHandle": task_id}) if task_id else {"accepted": False}
        return {
            "values": self.load_component_settings(),
            "message": "已请求取消模型下载。" if result.get("accepted") else "当前没有可取消的下载任务。",
        }

    def query_collection(self, request: Mapping[str, object]) -> dict[str, object]:
        if self._boundary.status()["status"] != "ready":
            return {"items": [], "nextCursor": None, "total": 0}
        records = self._projected_records()
        search = str(request.get("search", "")).strip().casefold()
        filters = _mapping(request.get("filters"))
        layer = str(filters.get("layer", ""))
        if search:
            records = [
                item
                for item in records
                if search
                in " ".join(
                    str(item.get(key, ""))
                    for key in ("content", "category", "source")
                ).casefold()
            ]
        if layer:
            records = [item for item in records if item.get("layer") == layer]
        records.sort(
            key=lambda item: (str(item.get("updatedAt", "")), str(item.get("id", ""))),
            reverse=True,
        )
        try:
            offset = int(str(request.get("cursor") or "0"))
        except ValueError as error:
            raise ValueError("MEMORY_CURSOR_INVALID") from error
        if offset < 0:
            raise ValueError("MEMORY_CURSOR_INVALID")
        limit = max(1, min(100, int(request.get("limit", 25))))
        page: list[dict[str, object]] = []
        for item in records[offset : offset + limit]:
            projected = _collection_item(item)
            candidate = {
                "items": [*page, projected],
                "nextCursor": str(offset + len(page) + 1),
                "total": len(records),
            }
            if page and not _json_fits(candidate, 240 * 1024):
                break
            page.append(projected)
        next_offset = offset + len(page)
        return {
            "items": page,
            "nextCursor": str(next_offset) if next_offset < len(records) else None,
            "total": len(records),
        }

    def create_collection_item(self, values: Mapping[str, object]) -> dict[str, object]:
        result = self._boundary.upsert({**dict(values), "source": values.get("source") or "explicit"})
        return _collection_item(_mapping(result.get("memory")))

    def update_collection_item(
        self,
        item_id: str,
        values: Mapping[str, object],
    ) -> dict[str, object]:
        current = next(
            (item for item in self._projected_records() if item.get("id") == item_id),
            None,
        )
        if current is None:
            raise ValueError("MEMORY_NOT_FOUND")
        if self._is_profile_item(item_id):
            edited = self._boundary.edit_core_profile_item(item_id, values.get("content", current.get("content")))
            if edited is None:
                raise ValueError("MEMORY_NOT_FOUND")
            return _collection_item(edited)
        writable = {
            key: current.get(key)
            for key in ("content", "layer", "category", "source", "importance", "confidence")
        }
        writable.update(values)
        result = self._boundary.upsert({"id": item_id, **writable})
        return _collection_item(_mapping(result.get("memory")))

    def delete_collection_item(self, item_id: str) -> dict[str, bool]:
        if self._is_profile_item(item_id):
            self._boundary.edit_core_profile_item(item_id, "")
            return {"deleted": True}
        result = self._boundary.delete({"id": item_id})
        return {"deleted": not bool(result.get("alreadyMissing"))}

    def note_completed_chat(self, payload: object) -> None:
        if (
            not isinstance(payload, Mapping)
            or set(payload) != {"characterId", "turnId", "cursor"}
            or payload.get("characterId") != self._character_id
            or not isinstance(payload.get("turnId"), str)
            or not payload.get("turnId")
            or not isinstance(payload.get("cursor"), str)
            or not payload.get("cursor")
        ):
            return
        self.catch_up_timeline()

    def catch_up_timeline(self) -> None:
        if self._timeline is None:
            return
        try:
            self._boundary.note_timeline_changed(self._timeline)
        except Exception:
            return

    def close(self) -> None:
        with self._task_lock:
            if self._closed:
                return
            self._closed = True
            task_id = self._model_task_id
            thread = self._model_task_thread
        if task_id:
            try:
                self._boundary.model_cancel({"taskHandle": task_id})
            except Exception:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        self._boundary.close()

    def _projected_records(self) -> list[dict[str, object]]:
        reader = getattr(self._boundary, "list_memories", None)
        records = (
            reader(limit=None)
            if callable(reader)
            else self._boundary.memory_store.list_memories(limit=None)
        )
        projected = [
            item
            for raw in records[:_MAX_COLLECTION_ITEMS]
            if isinstance(raw, Mapping)
            and (item := _project_memory(raw, self._character_id)) is not None
        ]
        profile_items = getattr(self._boundary, "core_profile_items", None)
        if callable(profile_items):
            try:
                projected = [*profile_items(), *projected]
            except Exception:
                pass
        return projected

    def _is_profile_item(self, item_id: str) -> bool:
        return callable(getattr(self._boundary, "edit_core_profile_item", None)) and item_id.startswith(
            f"core_profile:{self._character_id}#"
        )

    def _record_model_progress(self, stage: str, progress: int) -> None:
        with self._task_lock:
            self._model_task_state = "running"
            self._model_task_stage = _model_stage_label(stage)
            self._model_task_progress = max(0, min(100, int(progress)))

    def _embedding_resource_value(
        self,
        embedding: Mapping[str, object],
    ) -> dict[str, object]:
        installed = embedding.get("installed") is True
        with self._task_lock:
            state = self._model_task_state
            stage = self._model_task_stage
            progress = self._model_task_progress
            error_code = self._model_task_error_code
        if state in {"queued", "running"}:
            actions: list[str] = ["cancelEmbedding"]
            message = "正在下载模型"
        elif state in {"failed", "cancelled"}:
            actions = ["retryEmbedding"]
            if state == "cancelled":
                message = (
                    "已取消，原模型仍可用。"
                    if installed
                    else "已取消"
                )
            else:
                message = (
                    "下载失败，原模型仍可用。"
                    if installed
                    else "下载失败"
                )
        elif installed:
            actions = []
            message = "已安装"
        else:
            actions = ["downloadEmbedding"]
            message = "尚未安装"
        return {
            "applicability": "required",
            "subtitle": str(embedding.get("model", ""))[:512],
            "ready": installed,
            "taskState": state if state in {
                "idle", "queued", "running", "succeeded", "failed", "cancelled"
            } else "idle",
            "message": message,
            "detail": (
                stage[:240]
                if state in {"queued", "running"}
                else _model_download_error_detail(error_code)
                if state == "failed"
                else ""
            ),
            "progress": progress if state in {"queued", "running"} else None,
            "availableActionIds": actions,
        }


def _model_download_error_detail(code: str) -> str:
    messages = {
        "DOWNLOAD_NETWORK_FAILED": "无法连接模型下载服务，请检查网络或代理后重试。",
        "DOWNLOAD_DEPENDENCY_MISSING": "下载组件依赖缺失，请重新安装或修复 Sakura Runtime。",
        "DOWNLOAD_INCOMPLETE": "下载内容不完整，请重试。",
        "DOWNLOAD_SIZE_MISMATCH": "模型文件大小不匹配，请重试。",
        "INSTALL_TARGET_BUSY": "模型目录正被占用或不可写，请关闭相关程序后重试。",
        "DOWNLOAD_FAILED": "下载过程发生内部错误，请重试。",
    }
    safe_code = code if code in messages else "DOWNLOAD_FAILED"
    return f"{messages[safe_code]}（{safe_code}）"


def _retain_daily_bm25_cache(context: object, storage: object) -> None:
    """Point FastEmbed at the host BM25 cache until this daily context closes.

    Setup failure closes the context and runs effects in reverse. Registering
    the restore before runtime close keeps the variables while that runtime,
    including its load thread, is still alive.
    """
    cache_path = str(Path(storage.resolve("cache", "memory")) / "bm25")
    previous = {
        "FASTEMBED_CACHE_PATH": os.environ.get("FASTEMBED_CACHE_PATH"),
        "HF_HUB_OFFLINE": os.environ.get("HF_HUB_OFFLINE"),
    }
    os.environ["FASTEMBED_CACHE_PATH"] = cache_path
    os.environ["HF_HUB_OFFLINE"] = "1"

    def restore() -> None:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    try:
        getattr(context, "effect")(restore)
    except Exception:
        restore()
        raise


class SakuraMem0Plugin:
    def __init__(
        self,
        runtime_factory: Callable[[object], SakuraMem0Runtime] | None = None,
        *, personal_snapshot: Path | None = None, personal_write_rehearsal: bool = False,
        personal_daily: bool = False,
    ) -> None:
        if runtime_factory is not None and personal_snapshot is not None:
            raise ValueError("PERSONAL_RUNTIME_FACTORY_CONFLICT")
        self._personal_snapshot = personal_snapshot
        if personal_write_rehearsal and personal_snapshot is None:
            raise ValueError("PERSONAL_RUNTIME_SNAPSHOT_REQUIRED")
        if personal_daily and personal_snapshot is None:
            raise ValueError("PERSONAL_RUNTIME_SNAPSHOT_REQUIRED")
        if personal_daily and personal_write_rehearsal:
            raise ValueError("PERSONAL_WRITE_MODE_CONFLICT")
        self._personal_write_rehearsal = personal_write_rehearsal
        self._personal_daily = personal_daily
        self._runtime_factory = runtime_factory or _default_runtime

    @staticmethod
    def _register_memory_management(context: object, runtime: SakuraMem0Runtime) -> None:
        getattr(context, "get")("sakura.host.settings").register(runtime.memory_management_descriptor())
        getattr(context, "get")("sakura.host.settings.surface-v0").register(
            MEMORY_MANAGEMENT_SECTION_ID,
            "memory",
        )
        getattr(context, "get")("sakura.host.settings.collection-v0").register(
            MEMORY_MANAGEMENT_SECTION_ID,
            runtime.memory_collection_descriptor(),
            query=runtime.query_collection,
            create=runtime.create_collection_item,
            update=runtime.update_collection_item,
            delete=runtime.delete_collection_item,
        )

    def setup(self, context: object) -> None:
        try:
            bind_logger(getattr(context, "get")("sakura.host.logging"))
            getattr(context, "effect")(lambda: bind_logger(None))
        except Exception:
            bind_logger(None)
        if self._personal_snapshot is not None:
            if __package__:
                from .personal_runtime import PersonalRecallBoundary
            else:
                from personal_runtime import PersonalRecallBoundary
            storage = getattr(context, "get")("sakura.host.storage")
            if self._personal_daily:
                _retain_daily_bm25_cache(context, storage)
            character = getattr(context, "get")("sakura.host.character").current()
            character_id = str(character.get("id") or "")
            curation_options = None
            timeline = None
            if self._personal_write_rehearsal or self._personal_daily:
                slots = getattr(context, "get")("sakura.host.model_slots")
                timeline = getattr(context, "get")("sakura.host.timeline")
                curation_options = {
                    "system_prompt": str(character.get("systemPrompt") or ""),
                    "curation_config_getter": context.config.get,
                    "model_catalog_getter": slots.catalog,
                    "model_resolver": slots.resolve,
                }
            config_reader = getattr(getattr(context, "config", None), "get", None)
            reranker = _mapping(config_reader() if callable(config_reader) else None).get("personalReranker")
            boundary = PersonalRecallBoundary(Path(storage.resolve("data", "memory")),
                                              character_id, self._personal_snapshot,
                                              curation_options=curation_options, daily=self._personal_daily,
                                              reranker_snapshot=(Path(reranker) if isinstance(reranker, str)
                                                                 and Path(reranker).is_absolute() else None))
            runtime = SakuraMem0Runtime(Path(getattr(context, "data_path")(".")),
                                       character_id, boundary=boundary, timeline=timeline)
        else:
            runtime = self._runtime_factory(context)
        getattr(context, "effect")(runtime.close)
        if self._personal_snapshot is None or self._personal_write_rehearsal or self._personal_daily:
            getattr(context, "on")(HOST_CHAT_COMPLETED_EVENT, runtime.note_completed_chat)
        getattr(context, "get")("sakura.host.context").register(
            {
                "providerId": MEMORY_CONTEXT_PROVIDER_ID,
                "description": "从当前角色的本地长期记忆中选择与本轮相关的少量事实。",
                "order": 60,
                "timeoutSeconds": MEMORY_CONTEXT_TIMEOUT_SECONDS,
            },
            runtime.context,
        )
        tools = getattr(context, "get")("sakura.host.tools")
        registrations = (
            _personal_tool_registrations(runtime, daily=self._personal_daily)
            if self._personal_snapshot is not None
            else _tool_registrations(runtime)
        )
        for descriptor, callback in registrations:
            tools.register(descriptor, callback)
        if self._personal_snapshot is not None:
            if self._personal_daily:
                self._register_memory_management(context, runtime)
            return
        settings = getattr(context, "get")("sakura.host.settings")
        settings.register(
            runtime.settings_descriptor(),
            load=runtime.load_settings,
            save=runtime.save_settings,
        )
        settings.register(
            runtime.component_descriptor(),
            load=runtime.load_component_settings,
            actions={
                "downloadEmbedding": runtime.start_model_download,
                "retryEmbedding": runtime.start_model_download,
                "cancelEmbedding": runtime.cancel_model_download,
            },
        )
        getattr(context, "get")("sakura.host.settings.surface-v0").register(
            MEMORY_COMPONENT_SECTION_ID,
            "about",
        )
        self._register_memory_management(context, runtime)
        getattr(context, "get")("sakura.host.model_slots").register(
            {
                "slotId": "curation",
                "label": "记忆整理模型",
                "description": "继承时使用对话模型。",
                "modelKind": "chat_completion",
                "required": False,
                "order": 30,
            },
            load=runtime.load_model_slot,
            save=runtime.save_model_slot,
        )
        # Backlog catch-up may scan and curate a large Timeline. Plugin setup is serial, so it
        # must not hold up unrelated plugins such as the TTS Hub and its providers.
        threading.Thread(
            target=runtime.catch_up_timeline,
            name="sakura-mem0-initial-catch-up",
            daemon=True,
        ).start()


class PersonalRecallPlugin:
    """Explicit worker entry for a completed isolated copy; never the default."""
    def setup(self, context):
        snapshot = context.config.get().get("personalSnapshot")
        if not isinstance(snapshot, str) or not snapshot or not Path(snapshot).is_absolute():
            raise ValueError("PERSONAL_RUNTIME_SNAPSHOT_REQUIRED")
        SakuraMem0Plugin(personal_snapshot=Path(snapshot)).setup(context)


class PersonalWriteRehearsalPlugin:
    """Explicit internal entry; only path-admitted disposable copies can write."""
    def setup(self, context):
        snapshot = context.config.get().get("personalSnapshot")
        if not isinstance(snapshot, str) or not snapshot or not Path(snapshot).is_absolute():
            raise ValueError("PERSONAL_RUNTIME_SNAPSHOT_REQUIRED")
        SakuraMem0Plugin(personal_snapshot=Path(snapshot), personal_write_rehearsal=True).setup(context)


class PersonalDailyPlugin:
    """Explicit personal daily entry; admission remains path and scope bound."""
    def setup(self, context):
        snapshot = context.config.get().get("personalSnapshot")
        if not isinstance(snapshot, str) or not snapshot or not Path(snapshot).is_absolute():
            raise ValueError("PERSONAL_RUNTIME_SNAPSHOT_REQUIRED")
        SakuraMem0Plugin(personal_snapshot=Path(snapshot), personal_daily=True).setup(context)


def _default_runtime(context: object) -> SakuraMem0Runtime:
    plugin_data_root = Path(getattr(context, "data_path")("."))
    storage = getattr(context, "get")("sakura.host.storage")
    character = getattr(context, "get")("sakura.host.character").current()
    model_slots = getattr(context, "get")("sakura.host.model_slots")
    character_id = str(character.get("id", ""))
    system_prompt = str(character.get("systemPrompt", ""))
    if not character_id or not system_prompt:
        raise RuntimeError("MEMORY_CHARACTER_UNAVAILABLE")
    plugin_config = getattr(context, "config")
    config_getter = getattr(plugin_config, "get")
    config_updater = getattr(plugin_config, "update")
    return SakuraMem0Runtime(
        plugin_data_root,
        character_id,
        system_prompt=system_prompt,
        timeline=getattr(context, "get")("sakura.host.timeline"),
        config_getter=config_getter,
        config_updater=config_updater,
        memory_dir=Path(storage.resolve("data", "memory")),
        memory_cache_dir=Path(storage.resolve("cache", "memory")),
        model_catalog_getter=model_slots.catalog,
        model_resolver=model_slots.resolve,
    )


def _tool_registrations(
    runtime: SakuraMem0Runtime,
) -> list[tuple[dict[str, object], Callable[[Mapping[str, object]], object]]]:
    return [
        (
            {
                "name": "memory_search",
                "description": "搜索当前角色的长期记忆；需要跨会话事实、偏好或项目状态时使用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                        "layer": {"type": "string", "enum": list(MEMORY_LAYERS)},
                    },
                    "required": ["query"],
                },
                "group": "plugin",
                "risk": "low",
            },
            runtime.search_tool,
        ),
        (
            {
                "name": "memory_remember",
                "description": (
                    "保存一条当前角色的长期记忆。只在用户明确要求记住，或信息明显会长期帮助陪伴/协作时使用；"
                    "不得保存密码、token、密钥、证件号、银行卡等敏感凭据或身份秘密。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "layer": {"type": "string", "enum": list(MEMORY_LAYERS)},
                        "category": {"type": "string"},
                        "importance": {"type": "number"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["content"],
                },
                "group": "plugin",
                "risk": "medium",
            },
            runtime.remember_tool,
        ),
        (
            {
                "name": "memory_update",
                "description": (
                    "更新一条当前角色的长期记忆。应先搜索并取得准确的 memory_id；"
                    "只在用户明确纠正、补充、合并旧记忆，或已有记忆明显过时时使用；"
                    "不得写入密码、token、密钥、证件号、银行卡等敏感凭据或身份秘密。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "string"},
                        "content": {"type": "string"},
                        "layer": {"type": "string", "enum": list(MEMORY_LAYERS)},
                        "category": {"type": "string"},
                        "importance": {"type": "number"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["memory_id", "content"],
                },
                "group": "plugin",
                "risk": "medium",
            },
            runtime.update_tool,
        ),
        (
            {
                "name": "memory_forget",
                "description": "按 memory_id 删除当前角色的一条长期记忆；只在用户明确要求忘记时使用。",
                "parameters": {
                    "type": "object",
                    "properties": {"memory_id": {"type": "string"}},
                    "required": ["memory_id"],
                },
                "group": "plugin",
                "risk": "high",
            },
            runtime.forget_tool,
        ),
    ]


_SENSITIVE_NOTE = "不要写入密码、token、密钥、身份证、银行卡等敏感凭据。"


def _personal_tool_registrations(
    runtime: SakuraMem0Runtime, *, daily: bool,
) -> list[tuple[dict[str, object], Callable[[Mapping[str, object]], object]]]:
    """Qt-era personal memory tools; writes exist only on the admitted daily entry."""

    def call(name: str) -> Callable[[Mapping[str, object]], object]:
        return getattr(runtime, f"personal_{name}_tool")

    memory_id = {"memory_id": {"type": "string"}}
    write_fields = {
        "content": {"type": "string"},
        "layer": {"type": "string", "enum": list(VECTOR_MEMORY_LAYERS)},
        "category": {"type": "string"},
        "importance": {"type": "number"},
        "confidence": {"type": "number"},
    }

    def descriptor(name: str, description: str, properties: dict, required: list[str], risk: str) -> dict:
        return {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
            "group": "plugin",
            "risk": risk,
        }

    search = descriptor(
        "memory_search",
        "搜索长期记忆。问「认不认识 / 旧事 / 偏好 / 是谁」时默认用本工具，不要先用 history_search 翻聊天记录。"
        "仅当运行时已注入的记忆不够用时再调用；同轮优先一次，显式回忆最多两次，不要对同一意图换词连搜。"
        "若结果为空或未写明某细节，回答时承认不知道/记不清，禁止编造。"
        "mode='full'（默认）返回完整正文；mode='index' 只返回标题索引，token 消耗约 1/10。"
        "已放手的记忆默认不返回；只有对方明确要回顾放下的事时才设 include_released=true。",
        {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
            "layer": {"type": "string", "enum": list(MEMORY_LAYERS)},
            "mode": {"type": "string", "enum": ["full", "index"]},
            "include_released": {"type": "boolean"},
        },
        ["query"],
        "low",
    )
    registrations = [(search, call("search"))]
    if not daily:
        return registrations
    registrations += [
        (descriptor(
            "memory_detail",
            "按 memory_id 列表批量取回完整记忆内容。先用 memory_search(mode='index') 获取标题索引，"
            "再对感兴趣的条目调用本工具展开全文。ids 可以是逗号分隔的字符串或数组。",
            {"ids": {"type": "array", "items": {"type": "string"}}}, ["ids"], "low",
        ), call("detail")),
        (descriptor(
            "memory_timeline",
            "以某条记忆为锚点，查看它在时间线上的前后上下文。给定 memory_id，返回该条记忆及其之前/之后的邻近记忆。"
            "适合在 memory_search 找到感兴趣的条目后，了解「那段时间还发生了什么」。不支持常驻档案（core_profile）作为锚点。",
            {**memory_id, "before": {"type": "integer"}, "after": {"type": "integer"}}, ["memory_id"], "low",
        ), call("timeline")),
        (descriptor(
            "memory_remember",
            "保存一条明确、长期有用的记忆。只在对方明确要求记住，或信息明显会长期帮助相处/协作时使用。"
            "身体亲密上的第一次、关系推进、对方的亲密偏好/边界、事后仍想记住的话，也属于应长期记住的相处事实"
            "（写记忆点与偏好，不要写过程流水账）。关于他的事实用简体中文写；日记主语「我」是你自己，「他」是对方；"
            "用「我／他」写清谁说了什么/约了什么，再写感受；已知名字可用名字代替「他」。" + _SENSITIVE_NOTE,
            write_fields, ["content"], "medium",
        ), call("remember")),
        (descriptor(
            "memory_update",
            "更新一条已存在的长期记忆。先用 memory_search 找到 memory_id；只在对方明确纠正、补充、合并旧记忆，"
            "或已有记忆明显过时时使用。" + _SENSITIVE_NOTE,
            {**memory_id, **write_fields}, ["memory_id", "content"], "medium",
        ), call("update")),
        (descriptor(
            "memory_forget",
            "在对方明确要求忘记某条信息时，按 memory_id 删除长期记忆。",
            memory_id, ["memory_id"], "high",
        ), call("forget")),
        (descriptor(
            "memory_let_go",
            "放手一条记忆——不再想起，但不删除。用于「这件事我已经不想再记着了」的场合。",
            memory_id, ["memory_id"], "medium",
        ), call("let_go")),
    ]
    return registrations


def _layer_options() -> list[dict[str, str]]:
    labels = {
        "core_profile": "核心档案",
        "semantic": "语义记忆",
        "episodic": "情景记忆",
        "procedural": "程序记忆",
        "session": "会话记忆",
    }
    return [{"label": labels.get(layer, layer), "value": layer} for layer in MEMORY_LAYERS]


def _collection_item(memory: Mapping[str, object]) -> dict[str, object]:
    item_id = str(memory.get("id", ""))
    if not item_id:
        raise ValueError("MEMORY_RESPONSE_INVALID")
    return {
        "itemId": item_id,
        "values": {
            key: memory.get(key, "")
            for key in (
                "content",
                "layer",
                "category",
                "source",
                "importance",
                "confidence",
                "updatedAt",
            )
        },
    }


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _without_profile_duplicate(fragments, profile):
    profile_id = str(profile.get("id") or "")
    content = str(profile.get("content") or "")
    label = "【常驻档案】\n"
    body = content[len(label):] if content.startswith(label) else content
    repeated = {content, f"与本轮相关的长期记忆：{body}"}
    kept = []
    for item in fragments:
        item_id = str(item.get("id") or "")
        if item_id in {profile_id, f"memory.{profile_id}"} or item_id.startswith("memory.core_profile:"):
            continue
        if str(item.get("content") or "") in repeated:
            continue
        kept.append(item)
    return kept


def _context_request(value: object) -> ContextRequest:
    if isinstance(value, ContextRequest):
        return value
    raw = _mapping(value)
    recent: list[ContextMessage] = []
    messages = raw.get("recent_messages", [])
    if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)):
        for item in messages[-8:]:
            message = _mapping(item)
            role = str(message.get("role", ""))
            content = message.get("content")
            if role in {"user", "assistant"} and isinstance(content, str):
                recent.append(ContextMessage(role, content[:2000]))
    return ContextRequest(
        current_input=str(raw.get("current_input", ""))[:4096],
        character_id=str(raw.get("character_id", ""))[:128],
        character_name=str(raw.get("character_name", ""))[:120],
        current_turn_id=str(raw.get("current_turn_id", ""))[:128],
        source_entry_ids=tuple(
            str(item)[:128]
            for item in (
                raw.get("source_entry_ids", [])
                if isinstance(raw.get("source_entry_ids"), (list, tuple))
                else []
            )[:16]
        ),
        human_entry_id=str(raw.get("human_entry_id", ""))[:128],
        observation_entry_ids=tuple(
            str(item)[:128]
            for item in (
                raw.get("observation_entry_ids", [])
                if isinstance(raw.get("observation_entry_ids"), (list, tuple))
                else []
            )[:16]
        ),
        source=(
            raw.get("source")
            if raw.get("source") in {"chat", "event"}
            else "chat"
        ),
        mode=(
            raw.get("mode")
            if raw.get("mode") in {"normal", "screen_awareness"}
            else "normal"
        ),
        event_type=str(raw.get("event_type", ""))[:64],
        step_index=_bounded_context_int(raw.get("step_index"), 0, 32),
        remaining_steps=_bounded_context_int(raw.get("remaining_steps"), 0, 32),
        recent_messages=tuple(recent),
        available_tools=tuple(
            str(item)[:64]
            for item in (
                raw.get("available_tools", [])
                if isinstance(raw.get("available_tools"), list)
                else []
            )[:64]
        ),
        screen_context_available=bool(raw.get("screen_context_available")),
        current_time=str(raw.get("current_time", ""))[:80],
    )


def _bounded_context_int(value: object, minimum: int, maximum: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return min(maximum, max(minimum, value))
    return minimum


def _json_fits(value: object, maximum: int) -> bool:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8")) <= maximum


def _parse_model_slot_selection(value: object) -> dict[str, str]:
    raw = _mapping(value)
    if set(raw) != {"profileId", "model"}:
        raise ValueError("MODEL_SLOT_SELECTION_INVALID")
    profile_id = str(raw.get("profileId", ""))
    model = str(raw.get("model", ""))
    if len(profile_id) > 64 or len(model) > 256 or bool(profile_id) != bool(model):
        raise ValueError("MODEL_SLOT_SELECTION_INVALID")
    return {"profileId": profile_id, "model": model}


def _runtime_status_value(status: str, message: str) -> dict[str, str]:
    state = {
        "ready": "ready",
        "loading": "working",
        "degraded": "warning",
        "read_only": "warning",
        "failed": "error",
        "stopped": "error",
    }.get(status, "neutral")
    label = {
        "ready": "运行正常",
        "loading": "正在初始化",
        "degraded": "功能受限",
        "read_only": "只读运行",
        "failed": "运行失败",
        "stopped": "已停止",
    }.get(status, "状态未知")
    return {
        "state": state,
        "label": label,
        "message": message[:240] if state not in {"ready", "neutral"} else "",
    }


def _model_stage_label(stage: str) -> str:
    return {
        "connecting": "连接下载源",
        "downloading": "下载模型文件",
        "installing": "安装并校验",
        "completed": "安装完成",
    }.get(stage, "处理模型文件")


__all__ = ["SakuraMem0Plugin", "SakuraMem0Runtime"]
