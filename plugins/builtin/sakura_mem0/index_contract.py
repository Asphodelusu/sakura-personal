"""Reject personal index generations until their encoder/lifecycle is supported.

This is not a binding parser or a migration. Never interpret a private pointer,
open Qdrant, or choose a legacy fallback just because both encoders use 384 dims.
"""
import os
import json
from pathlib import Path

COPY_STATE_FILE = ".sakura-personal-copy.json"


class PersonalCopyIncomplete(RuntimeError):
    def __init__(self):
        super().__init__("PERSONAL_COPY_INCOMPLETE")


def require_complete_copy(path: Path) -> None:
    path = Path(path).absolute()
    for root in (path, *path.parents):
        marker = root / COPY_STATE_FILE
        if not os.path.lexists(marker):
            continue
        try:
            if marker.is_symlink() or not marker.is_file():
                raise ValueError("invalid marker")
            state = json.loads(marker.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or state.get("state") != "complete":
                raise ValueError("incomplete")
        except (OSError, ValueError) as exc:
            raise PersonalCopyIncomplete() from exc


class PersonalMemoryIndexUnsupported(RuntimeError):
    code = "MEMORY_PERSONAL_INDEX_UNSUPPORTED"

    def __init__(self):
        super().__init__(
            f"{self.code}: 检测到个人记忆索引布局；当前 ONNX 插件尚未兼容，未打开或重建索引。"
        )


def require_supported_index_layout(memory_dir: Path) -> None:
    require_complete_copy(memory_dir)
    # lexists also detects broken links and wrong file types. Malformed personal
    # markers must not look like an empty upstream installation.
    if any(os.path.lexists(Path(memory_dir) / name) for name in ("active_index.json", "indexes")):
        raise PersonalMemoryIndexUnsupported()
