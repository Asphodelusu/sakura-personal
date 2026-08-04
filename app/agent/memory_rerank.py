"""记忆候选 Cross-Encoder 重排。

向量检索先粗捞，再用 reranker 对「问题 × 候选」逐对打分。
模型**仅使用本地缓存**，缺失时静默跳过；下载请走设置页按需安装。
设置页可在精选型号中切换（base / large / v2-m3）。
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"
_MIN_CANDIDATES = 2
_ENV_DISABLE = "SAKURA_MEMORY_RERANK"
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

_LOCK = threading.Lock()
_MODEL: Any | None = None
_MODEL_KEY: str | None = None
_LOAD_FAILED = False


@dataclass(frozen=True)
class RerankerCatalogEntry:
    model_id: str
    label: str
    size_label: str
    note: str = ""


RERANKER_CATALOG: tuple[RerankerCatalogEntry, ...] = (
    RerankerCatalogEntry(
        "BAAI/bge-reranker-base",
        "bge-reranker-base",
        "约 280MB",
        "轻量，中文日常够用，CPU 更快",
    ),
    RerankerCatalogEntry(
        "BAAI/bge-reranker-large",
        "bge-reranker-large",
        "约 560MB",
        "比 base 更准，体积仍可控",
    ),
    RerankerCatalogEntry(
        "BAAI/bge-reranker-v2-m3",
        "bge-reranker-v2-m3",
        "约 2.2GB",
        "多语言，与记忆嵌入 bge-m3 最搭",
    ),
)

_CATALOG_BY_ID = {entry.model_id: entry for entry in RERANKER_CATALOG}


def memory_rerank_enabled() -> bool:
    raw = (os.environ.get(_ENV_DISABLE) or "").strip().lower()
    return raw not in {"0", "false", "off", "no"}


def reset_memory_reranker_for_tests() -> None:
    """测试用：清空模块级模型缓存。"""
    invalidate_memory_reranker()


def invalidate_memory_reranker() -> None:
    """下载/切换型号后清空缓存，下次召回重新加载。"""
    global _MODEL, _MODEL_KEY, _LOAD_FAILED
    with _LOCK:
        _MODEL = None
        _MODEL_KEY = None
        _LOAD_FAILED = False


def catalog_entry(model_id: str | None = None) -> RerankerCatalogEntry | None:
    return _CATALOG_BY_ID.get(normalize_reranker_model_id(model_id) or "")


def normalize_reranker_model_id(model_id: str | None) -> str:
    text = (model_id or "").strip()
    if not text:
        return ""
    if text in _CATALOG_BY_ID:
        return text
    if _MODEL_ID_RE.fullmatch(text):
        return text
    return ""


def selected_reranker_model(base_dir: Path | None = None) -> str:
    """当前选用的精排型号；无效或未配置时回退默认。"""
    path = _reranker_selection_path(base_dir)
    if path is not None and path.is_file():
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except OSError:
            raw = ""
        normalized = normalize_reranker_model_id(raw)
        if normalized:
            return normalized
    return DEFAULT_RERANKER_MODEL


def set_selected_reranker_model(base_dir: Path | None, model_id: str) -> str:
    """持久化选用的精排型号（须在精选目录内）。"""
    normalized = normalize_reranker_model_id(model_id)
    if normalized not in _CATALOG_BY_ID:
        raise ValueError(
            "不支持的精排型号。"
            f"可选：{', '.join(entry.model_id for entry in RERANKER_CATALOG)}"
        )
    path = _reranker_selection_path(base_dir)
    if path is None:
        raise ValueError("无法确定记忆目录，未能保存精排型号。")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(normalized + "\n", encoding="utf-8")
    if selected_reranker_model(base_dir) != normalized:
        # 防御：读写异常时仍以写入值为准
        pass
    invalidate_memory_reranker()
    return normalized


def reranker_model_cached(
    base_dir: Path | None = None,
    model_id: str | None = None,
) -> bool:
    """本地是否已有指定（或当前选用）reranker 快照。"""
    resolved = normalize_reranker_model_id(model_id) or selected_reranker_model(base_dir)
    return _resolve_reranker_snapshot(base_dir, resolved) is not None


def download_reranker_model(
    base_dir: Path | None = None,
    model_id: str | None = None,
) -> Any:
    """在线安装记忆 reranker（仅设置页调用）。"""
    from app.agent.memory import (
        EmbeddingModelImportResult,
        MemoryModelImportError,
        _embedding_model_snapshot_path,
        _project_embedding_cache_folder,
    )
    from app.core.hf_hub_download import download_hf_snapshot

    resolved = normalize_reranker_model_id(model_id) or selected_reranker_model(base_dir)
    if resolved not in _CATALOG_BY_ID:
        raise MemoryModelImportError(
            "不支持的精排型号。"
            f"可选：{', '.join(entry.model_id for entry in RERANKER_CATALOG)}"
        )
    set_selected_reranker_model(base_dir, resolved)

    cache_folder = _project_embedding_cache_folder(base_dir)
    cache_folder.mkdir(parents=True, exist_ok=True)
    try:
        download_hf_snapshot(resolved, cache_folder)
    except Exception as exc:  # noqa: BLE001
        raise MemoryModelImportError(
            "记忆精排模型在线安装失败，已依次尝试官方 Hub 与 hf-mirror。"
            "请检查网络或代理后重试。"
            f"\n\n原始错误：{exc}"
        ) from exc

    snapshot = _embedding_model_snapshot_path(resolved, base_dir)
    if snapshot is None:
        raise MemoryModelImportError(
            "记忆精排模型下载后仍不完整：snapshots/ 下未找到模型权重。"
        )
    model_dir = snapshot.parents[1]
    invalidate_memory_reranker()
    return EmbeddingModelImportResult(
        model_name=resolved,
        cache_folder=cache_folder,
        model_dir=model_dir,
        snapshot_count=sum(1 for child in (model_dir / "snapshots").iterdir() if child.is_dir()),
    )


def warm_memory_reranker(base_dir: Path | None = None) -> None:
    """后台预热：仅加载已下载的当前选用模型，绝不触发下载。"""
    if not memory_rerank_enabled():
        return
    if not reranker_model_cached(base_dir):
        return
    try:
        _get_cross_encoder(base_dir)
    except Exception:  # noqa: BLE001
        logger.exception("记忆 reranker 预热失败，将在召回时回退到向量分")


def rerank_memory_candidates(
    query: str,
    memories: list[dict[str, Any]],
    *,
    base_dir: Path | None = None,
    scorer: Callable[[str, list[str]], list[float]] | None = None,
) -> list[dict[str, Any]]:
    """按 query 与候选正文的匹配度重排；模型未安装或失败时原样返回。"""
    if not memory_rerank_enabled():
        return memories
    query_text = (query or "").strip()
    if not query_text or len(memories) < _MIN_CANDIDATES:
        return memories

    texts: list[str] = []
    keep_indices: list[int] = []
    for index, memory in enumerate(memories):
        if not isinstance(memory, dict):
            continue
        text = str(memory.get("content") or memory.get("memory") or "").strip()
        if not text:
            continue
        texts.append(text)
        keep_indices.append(index)
    if len(texts) < _MIN_CANDIDATES:
        return memories

    try:
        if scorer is not None:
            raw_scores = scorer(query_text, texts)
        else:
            encoder = _get_cross_encoder(base_dir)
            if encoder is None:
                return memories
            pairs = [[query_text, text] for text in texts]
            predicted = encoder.predict(
                pairs,
                batch_size=min(32, len(pairs)),
                show_progress_bar=False,
            )
            raw_scores = [float(item) for item in predicted]
    except Exception:  # noqa: BLE001
        logger.exception("记忆 rerank 打分失败，回退向量排序")
        return memories

    if len(raw_scores) != len(keep_indices):
        return memories

    rescored: list[dict[str, Any]] = []
    for memory_index, raw in zip(keep_indices, raw_scores):
        memory = dict(memories[memory_index])
        semantic = memory.get("score")
        if "semantic_score" not in memory and semantic is not None:
            memory["semantic_score"] = semantic
        rerank_score = _sigmoid(float(raw))
        memory["rerank_score"] = rerank_score
        memory["score"] = rerank_score
        rescored.append(memory)

    used = set(keep_indices)
    for index, memory in enumerate(memories):
        if index not in used and isinstance(memory, dict):
            rescored.append(memory)

    rescored.sort(
        key=lambda item: float(item.get("score") or 0.0),
        reverse=True,
    )
    return rescored


def _sigmoid(value: float) -> float:
    if value >= 30:
        return 1.0
    if value <= -30:
        return 0.0
    return 1.0 / (1.0 + math.exp(-value))


def _get_cross_encoder(base_dir: Path | None) -> Any | None:
    global _MODEL, _MODEL_KEY, _LOAD_FAILED
    if _LOAD_FAILED:
        return None
    model_name = selected_reranker_model(base_dir)
    model_key = f"{model_name}|{base_dir}"
    if _MODEL is not None and _MODEL_KEY == model_key:
        return _MODEL
    with _LOCK:
        if _LOAD_FAILED:
            return None
        if _MODEL is not None and _MODEL_KEY == model_key:
            return _MODEL
        try:
            model_path = _resolve_reranker_snapshot(base_dir, model_name)
            if model_path is None:
                return None
            from sentence_transformers import CrossEncoder

            device = _prefer_device()
            _MODEL = CrossEncoder(str(model_path), device=device, max_length=512)
            _MODEL_KEY = model_key
            logger.info("记忆 reranker 已加载：%s (%s)", model_name, device)
            return _MODEL
        except Exception:  # noqa: BLE001
            _LOAD_FAILED = True
            logger.exception("记忆 reranker 加载失败")
            return None


def _prefer_device() -> str:
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
    except Exception:  # noqa: BLE001
        pass
    return "cpu"


def _resolve_reranker_snapshot(base_dir: Path | None, model_id: str) -> Path | None:
    from app.agent.memory import _embedding_model_snapshot_path

    return _embedding_model_snapshot_path(model_id, base_dir)


def _reranker_selection_path(base_dir: Path | None) -> Path | None:
    from app.agent.memory import _resolve_base_dir
    from app.storage.paths import StoragePaths

    try:
        root = _resolve_base_dir(base_dir)
    except Exception:  # noqa: BLE001
        return None
    return StoragePaths(root).memory_dir / "reranker_model.txt"


# 兼容旧导入名
DEFAULT_RERANKER_SIZE_LABEL = _CATALOG_BY_ID[DEFAULT_RERANKER_MODEL].size_label
