"""HuggingFace Hub 快照下载：多端点回退。

hf-mirror 在浏览器/直链可用，但 huggingface_hub 的 snapshot_download 经镜像
endpoint 时常失败；因此默认先试官方 Hub，再试镜像。用户可通过 HF_ENDPOINT
强制指定单一端点。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence

HF_ENDPOINT_OFFICIAL = "https://huggingface.co"
HF_ENDPOINT_MIRROR = "https://hf-mirror.com"

_DEFAULT_ENDPOINT_ORDER: tuple[str, ...] = (
    HF_ENDPOINT_OFFICIAL,
    HF_ENDPOINT_MIRROR,
)


def iter_hf_endpoints() -> list[str]:
    """返回按优先级排序的 Hub 端点列表。"""
    override = (os.environ.get("HF_ENDPOINT") or "").strip()
    if override:
        return [override]
    return list(_DEFAULT_ENDPOINT_ORDER)


def default_hf_endpoint() -> str:
    """设置页展示用：当前首选端点。"""
    return iter_hf_endpoints()[0]


def download_hf_snapshot(
    repo_id: str,
    cache_folder: Path | str,
    *,
    allow_patterns: Sequence[str] | None = None,
) -> str:
    """下载模型快照；按端点顺序尝试，全部失败时抛出最后一个错误。"""
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError("缺少 huggingface_hub 依赖，无法在线安装模型。") from exc

    cache_dir = str(cache_folder)
    last_error: Exception | None = None
    for endpoint in iter_hf_endpoints():
        try:
            kwargs: dict[str, object] = {
                "repo_id": repo_id,
                "cache_dir": cache_dir,
                "endpoint": endpoint,
                "local_files_only": False,
            }
            if allow_patterns is not None:
                kwargs["allow_patterns"] = list(allow_patterns)
            return str(snapshot_download(**kwargs))
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
    assert last_error is not None
    raise last_error
