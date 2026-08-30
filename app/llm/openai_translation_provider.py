"""OpenAI 兼容的中文字幕翻译 provider。

复用已装配的 chat_fast 客户端 complete_raw，不重复加载 HTTP/密钥。
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock
from typing import Any, Callable

from app.core.debug_log import debug_log
from app.llm.translation_validation import (
    SourceClassification,
    SourceKind,
    classify_translation_source,
    validate_lexical_chinese,
)

SUBTITLE_TRANSLATION_PURPOSE = "subtitle_translation"
SUBTITLE_TRANSLATION_RETRY_PURPOSE = "subtitle_translation_retry"

TRANSLATION_SYSTEM_PROMPT = (
    "把日语翻译成自然的简体中文。只输出译文，不要解释，不要拼音，不要日语原文。"
)
HEAD_TRANSLATION_SYSTEM_PROMPT = (
    "把日语翻译成自然的简体中文。只输出译文本身；"
    "不要解释、不要拼音、不要日语原文、不要引号。"
    "保留原文的标点与语气标记（如 ……、！、？）。"
    "动作段用全角括号包裹时，译文同样用全角括号包裹。"
)
TAIL_TRANSLATION_SYSTEM_PROMPT = (
    "你是字幕翻译器。把每个条目的日语译成自然的简体中文。"
    "逐条独立翻译：不要合并、拆分、增删或重排条目；"
    "保留每条各自的标点与语气标记；动作段（全角括号）译文同样用全角括号。"
    '只返回 JSON：{"items":[{"i":0,"zh":"译文"}]}。i 必须原样回填。'
)
_SALVAGE_ITEM_RE = re.compile(
    r'\{\s*"i"\s*:\s*(-?\d+)\s*,\s*"zh"\s*:\s*"(.*?)"\s*\}',
    re.S,
)
_MAX_SIDECAR_REQUESTS = 3


@dataclass(frozen=True)
class TranslationCallMetrics:
    requested_indexes: tuple[int, ...] = ()
    resolved_indexes: tuple[int, ...] = ()
    failed_indexes: tuple[int, ...] = ()
    salvaged_indexes: tuple[int, ...] = ()
    request_count: int = 0
    attempts: int = 0
    elapsed_ms: float = 0.0


@dataclass(frozen=True)
class TranslationIndexResult:
    index: int
    translation: str
    resolved_locally: bool = False


@dataclass(frozen=True)
class TranslationBatchResult:
    items: tuple[TranslationIndexResult, ...]
    failed_indexes: tuple[int, ...]
    request_count: int


class TranslationError(RuntimeError):
    """翻译失败；调用方应保留日语原文，不得把原文当作 zh。"""


class OpenAITranslationProvider:
    """批量翻译；单条失败抛异常，由调用方保留日语。"""

    def __init__(
        self,
        client: Any,
        *,
        max_attempts: int = 2,
        provider_name: str = "chat_fast",
        validator_mode: str = "v2",
        request_shape: str = "serial",
        split_batch_concurrent: bool = False,
    ) -> None:
        self.client = client
        self.max_attempts = max(1, min(2, int(max_attempts)))
        self.provider_name = provider_name
        self.validator_mode = str(validator_mode or "v2").strip().lower() or "v2"
        self.request_shape = str(request_shape or "serial").strip().lower() or "serial"
        self.split_batch_concurrent = bool(split_batch_concurrent)
        self.last_metrics = TranslationCallMetrics()

    def translate(
        self,
        texts: list[str],
        *,
        source_lang: str = "ja",
        target_lang: str = "zh",
    ) -> list[str]:
        """Legacy all-or-nothing adapter. Partial results stay on translate_indexed()."""
        if self.request_shape == "split_batch":
            batch = self.translate_indexed(
                texts,
                source_lang=source_lang,
                target_lang=target_lang,
            )
            by_index = {item.index: item.translation for item in batch.items}
            if any(index not in by_index or not by_index[index] for index in range(len(texts))):
                raise TranslationError("invalid_or_empty_translation")
            return [by_index[index] for index in range(len(texts))]
        _ = source_lang, target_lang
        started = time.perf_counter()
        attempts = 0
        requested = tuple(range(len(texts)))
        resolved: list[int] = []
        try:
            translations: list[str] = []
            for index, text in enumerate(texts):
                zh, used = self._translate_one(str(text or ""))
                attempts += used
                if not zh:
                    raise TranslationError("invalid_or_empty_translation")
                resolved.append(index)
                translations.append(zh)
        except Exception:
            elapsed_ms = (time.perf_counter() - started) * 1000
            self.last_metrics = TranslationCallMetrics(
                requested_indexes=requested,
                resolved_indexes=tuple(resolved),
                failed_indexes=tuple(index for index in requested if index not in resolved),
                request_count=attempts,
                attempts=attempts or 1,
                elapsed_ms=round(elapsed_ms, 1),
            )
            self._log_outcome(
                "failed",
                attempts=attempts or 1,
                elapsed_ms=elapsed_ms,
            )
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.last_metrics = TranslationCallMetrics(
            requested_indexes=requested,
            resolved_indexes=tuple(resolved),
            failed_indexes=(),
            request_count=attempts,
            attempts=attempts,
            elapsed_ms=round(elapsed_ms, 1),
        )
        self._log_outcome(
            "success",
            attempts=attempts,
            elapsed_ms=elapsed_ms,
        )
        return translations

    def _translate_one(self, text: str) -> tuple[str, int]:
        classified = self._classify_source(text)
        if classified.kind is SourceKind.NON_LEXICAL:
            return classified.local_zh, 0
        used = 0
        for attempt in range(self.max_attempts):
            used += 1
            purpose = (
                SUBTITLE_TRANSLATION_PURPOSE
                if attempt == 0
                else SUBTITLE_TRANSLATION_RETRY_PURPOSE
            )
            try:
                raw = self.client.complete_raw(
                    TRANSLATION_SYSTEM_PROMPT,
                    [{"role": "user", "content": text}],
                    temperature=0.3,
                    max_attempts=1,
                    max_tokens=512,
                    request_purpose=purpose,
                )
            except Exception:
                continue
            zh = _normalize_chinese_output(
                raw,
                source_ja=text,
                mode=self.validator_mode,
            )
            if zh:
                return zh, used
        return "", used

    def translate_indexed(
        self,
        texts: list[str],
        *,
        source_lang: str = "ja",
        target_lang: str = "zh",
        on_item: Callable[[TranslationIndexResult], None] | None = None,
        on_failed: Callable[[int], None] | None = None,
    ) -> TranslationBatchResult:
        if self.request_shape != "split_batch":
            return self._translate_serial_indexed(
                texts,
                source_lang=source_lang,
                target_lang=target_lang,
                on_item=on_item,
                on_failed=on_failed,
            )
        _ = source_lang, target_lang
        started = time.perf_counter()
        requested = tuple(range(len(texts)))
        resolved: dict[int, TranslationIndexResult] = {}
        salvaged: list[int] = []
        emit_lock = Lock()

        def emit(item: TranslationIndexResult) -> None:
            with emit_lock:
                if item.index in resolved:
                    return
                resolved[item.index] = item
            if on_item is not None:
                on_item(item)

        pending: list[int] = []
        for index, raw in enumerate(texts):
            text = str(raw or "")
            classified = self._classify_source(text)
            if classified.kind is SourceKind.NON_LEXICAL:
                emit(
                    TranslationIndexResult(
                        index=index,
                        translation=classified.local_zh,
                        resolved_locally=True,
                    )
                )
            else:
                pending.append(index)

        request_count = 0
        if pending:
            head_index = pending[0]
            tail_indexes = pending[1:]
            if self.split_batch_concurrent and tail_indexes:
                request_count += self._dispatch_head_tail_concurrent(
                    texts,
                    head_index,
                    tail_indexes,
                    emit,
                    salvaged,
                )
            else:
                request_count += self._translate_head(texts[head_index], head_index, emit)
                if tail_indexes:
                    request_count += self._translate_tail(
                        texts,
                        tail_indexes,
                        emit,
                        salvaged,
                        purpose=SUBTITLE_TRANSLATION_PURPOSE,
                    )
            failed = [index for index in pending if index not in resolved]
            if failed and request_count < _MAX_SIDECAR_REQUESTS:
                request_count += self._translate_tail(
                    texts,
                    failed,
                    emit,
                    salvaged,
                    purpose=SUBTITLE_TRANSLATION_RETRY_PURPOSE,
                )

        failed_indexes = tuple(index for index in requested if index not in resolved)
        items = tuple(resolved[index] for index in requested if index in resolved)
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.last_metrics = TranslationCallMetrics(
            requested_indexes=requested,
            resolved_indexes=tuple(item.index for item in items),
            failed_indexes=failed_indexes,
            salvaged_indexes=tuple(salvaged),
            request_count=request_count,
            attempts=request_count,
            elapsed_ms=round(elapsed_ms, 1),
        )
        self._log_outcome(
            "success" if not failed_indexes else "failed",
            attempts=request_count or 1,
            elapsed_ms=elapsed_ms,
        )
        if on_failed is not None:
            for index in failed_indexes:
                on_failed(index)
        return TranslationBatchResult(
            items=items,
            failed_indexes=failed_indexes,
            request_count=request_count,
        )

    def _translate_serial_indexed(
        self,
        texts: list[str],
        *,
        source_lang: str,
        target_lang: str,
        on_item: Callable[[TranslationIndexResult], None] | None,
        on_failed: Callable[[int], None] | None = None,
    ) -> TranslationBatchResult:
        _ = source_lang, target_lang
        started = time.perf_counter()
        requested = tuple(range(len(texts)))
        items: list[TranslationIndexResult] = []
        failed: list[int] = []
        request_count = 0
        for index, raw in enumerate(texts):
            text = str(raw or "")
            classified = self._classify_source(text)
            if classified.kind is SourceKind.NON_LEXICAL:
                item = TranslationIndexResult(
                    index=index,
                    translation=classified.local_zh,
                    resolved_locally=True,
                )
            else:
                zh, used = self._translate_one(text)
                request_count += used
                if not zh:
                    failed.append(index)
                    if on_failed is not None:
                        on_failed(index)
                    continue
                item = TranslationIndexResult(index=index, translation=zh)
            items.append(item)
            if on_item is not None:
                on_item(item)

        elapsed_ms = (time.perf_counter() - started) * 1000
        self.last_metrics = TranslationCallMetrics(
            requested_indexes=requested,
            resolved_indexes=tuple(item.index for item in items),
            failed_indexes=tuple(failed),
            request_count=request_count,
            attempts=request_count,
            elapsed_ms=round(elapsed_ms, 1),
        )
        self._log_outcome(
            "success" if not failed else "failed",
            attempts=request_count,
            elapsed_ms=elapsed_ms,
        )
        return TranslationBatchResult(
            items=tuple(items),
            failed_indexes=tuple(failed),
            request_count=request_count,
        )

    def _classify_source(self, text: str) -> SourceClassification:
        if self.validator_mode == "legacy":
            return SourceClassification(kind=SourceKind.LEXICAL)
        return classify_translation_source(text)

    def _dispatch_head_tail_concurrent(
        self,
        texts: list[str],
        head_index: int,
        tail_indexes: list[int],
        emit: Callable[[TranslationIndexResult], None],
        salvaged: list[int],
    ) -> int:
        with ThreadPoolExecutor(max_workers=2) as pool:
            head_future = pool.submit(
                self._request_plain,
                texts[head_index],
                purpose=SUBTITLE_TRANSLATION_PURPOSE,
            )
            tail_future = pool.submit(
                self._request_indexed,
                texts,
                tail_indexes,
                purpose=SUBTITLE_TRANSLATION_PURPOSE,
            )
            head_raw = head_future.result()
            zh = _normalize_chinese_output(
                head_raw,
                source_ja=texts[head_index],
                mode=self.validator_mode,
            )
            if zh:
                emit(
                    TranslationIndexResult(
                        index=head_index,
                        translation=zh,
                        resolved_locally=False,
                    )
                )
            mapped, recovered = tail_future.result()
            salvaged.extend(recovered)
            self._emit_mapped(texts, mapped, emit)
        return 2

    def _translate_head(
        self,
        text: str,
        index: int,
        emit: Callable[[TranslationIndexResult], None],
    ) -> int:
        raw = self._request_plain(text, purpose=SUBTITLE_TRANSLATION_PURPOSE)
        zh = _normalize_chinese_output(raw, source_ja=text, mode=self.validator_mode)
        if zh:
            emit(
                TranslationIndexResult(
                    index=index,
                    translation=zh,
                    resolved_locally=False,
                )
            )
        return 1

    def _translate_tail(
        self,
        texts: list[str],
        indexes: list[int],
        emit: Callable[[TranslationIndexResult], None],
        salvaged: list[int],
        *,
        purpose: str,
    ) -> int:
        if not indexes:
            return 0
        mapped, recovered = self._request_indexed(texts, indexes, purpose=purpose)
        salvaged.extend(recovered)
        self._emit_mapped(texts, mapped, emit)
        return 1

    def _emit_mapped(
        self,
        texts: list[str],
        mapped: dict[int, str],
        emit: Callable[[TranslationIndexResult], None],
    ) -> None:
        for index, zh in mapped.items():
            if not validate_lexical_chinese(
                texts[index],
                zh,
                mode=self.validator_mode,
            ).ok:
                continue
            emit(
                TranslationIndexResult(
                    index=index,
                    translation=zh,
                    resolved_locally=False,
                )
            )

    def _request_plain(self, text: str, *, purpose: str) -> str:
        try:
            return str(
                self.client.complete_raw(
                    HEAD_TRANSLATION_SYSTEM_PROMPT,
                    [{"role": "user", "content": text}],
                    temperature=0.2,
                    max_attempts=1,
                    max_tokens=256,
                    request_purpose=purpose,
                )
                or ""
            )
        except Exception:
            return ""

    def _request_indexed(
        self,
        texts: list[str],
        indexes: list[int],
        *,
        purpose: str,
    ) -> tuple[dict[int, str], list[int]]:
        payload = {
            "items": [{"i": index, "ja": str(texts[index] or "")} for index in indexes]
        }
        try:
            raw = str(
                self.client.complete_raw(
                    TAIL_TRANSLATION_SYSTEM_PROMPT,
                    [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                    temperature=0.2,
                    max_attempts=1,
                    max_tokens=128 * len(indexes) + 128,
                    request_purpose=purpose,
                    response_format={"type": "json_object"},
                )
                or ""
            )
        except Exception:
            return {}, []
        return _parse_indexed_response(raw, set(indexes))

    def _log_outcome(self, outcome: str, *, attempts: int, elapsed_ms: float) -> None:
        settings = getattr(self.client, "settings", None)
        model = str(getattr(settings, "model", "") or "")
        debug_log(
            "Translation",
            "sidecar completed",
            {
                "provider": self.provider_name,
                "model": model,
                "outcome": outcome,
                "attempts": int(attempts),
                "elapsed_ms": round(float(elapsed_ms), 1),
            },
        )


def build_translation_provider(
    settings: Any,
    chat_fast_client: Any,
) -> OpenAITranslationProvider | None:
    if settings is None or not bool(getattr(settings, "enabled", False)):
        return None
    if chat_fast_client is None:
        return None
    return OpenAITranslationProvider(
        chat_fast_client,
        max_attempts=int(getattr(settings, "max_attempts", 2) or 2),
        validator_mode=str(getattr(settings, "validator_mode", "v2") or "v2"),
        request_shape=str(getattr(settings, "request_shape", "serial") or "serial"),
    )


def _normalize_chinese_output(
    raw: object,
    *,
    source_ja: str = "",
    mode: str = "v2",
) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    extracted = _extract_json_zh(text)
    if extracted:
        text = extracted
    if not validate_lexical_chinese(source_ja, text, mode=mode).ok:
        return ""
    return text


def _parse_indexed_response(
    raw: str,
    requested: set[int],
) -> tuple[dict[int, str], list[int]]:
    by_index: dict[int, str] = {}
    salvaged: list[int] = []
    items, used_salvage = _load_indexed_items(raw)
    for item in items:
        if not isinstance(item, dict):
            continue
        index = item.get("i")
        zh = item.get("zh")
        if not isinstance(index, int) or isinstance(index, bool) or not isinstance(zh, str):
            continue
        if index not in requested or index in by_index:
            continue
        by_index[index] = zh
        if used_salvage:
            salvaged.append(index)
    return by_index, salvaged


def _load_indexed_items(raw: str) -> tuple[list[Any], bool]:
    text = str(raw or "").strip()
    if not text:
        return [], False
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        obj = None
    if isinstance(obj, dict) and isinstance(obj.get("items"), list):
        return list(obj["items"]), False
    salvaged: list[Any] = []
    for match in _SALVAGE_ITEM_RE.finditer(text):
        salvaged.append({"i": int(match.group(1)), "zh": _unescape_json_string(match.group(2))})
    return salvaged, True


def _unescape_json_string(value: str) -> str:
    try:
        parsed = json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value
    return str(parsed)


def _extract_json_zh(text: str) -> str:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return ""
        try:
            obj = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return ""
    if isinstance(obj, dict) and isinstance(obj.get("zh"), str):
        return str(obj["zh"]).strip()
    return ""
