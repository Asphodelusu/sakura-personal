"""请求分区与 usage 白名单观测。默认只产出长度/hash，不保存正文。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from app.llm.prompts.runtime import estimate_prompt_tokens

# 与 context_trimming.ESTIMATED_IMAGE_PART_TOKENS 对齐；此处不导入该模块以免环依赖。
ESTIMATED_IMAGE_PART_TOKENS = 28_000

KNOWN_REQUEST_PURPOSES = frozenset(
    {
        "initial",
        "tool_step",
        "semantic_compose",
        "structural_repair",
        "subtitle_translation",
        "subtitle_translation_retry",
    }
)
_RUNTIME_USER_PREFIX = "[Sakura runtime context; system-provided facts, not a user request]\n"


@dataclass(frozen=True)
class PayloadPartition:
    bytes: int
    estimated_tokens: int
    hash: str


@dataclass(frozen=True)
class PayloadInspection:
    interaction_id: str
    request_index: int
    request_purpose: str
    model: str
    endpoint: str
    partitions: dict[str, PayloadPartition | None]
    stable_prefix_bytes: int | None = None
    stable_prefix_hash: str | None = None
    usage: dict[str, int | None] = field(default_factory=dict)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def payload_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def normalize_request_purpose(value: str | None) -> str:
    text = str(value or "").strip()
    if text in KNOWN_REQUEST_PURPOSES:
        return text
    return "unknown"


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def extract_usage_metrics(usage: Mapping[str, Any] | None) -> dict[str, int | None]:
    empty = {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "cached_input_tokens": None,
        "reasoning_tokens": None,
    }
    if not isinstance(usage, Mapping):
        return empty
    input_tokens = _as_int(usage.get("prompt_tokens"))
    if input_tokens is None:
        input_tokens = _as_int(usage.get("input_tokens"))
    output_tokens = _as_int(usage.get("completion_tokens"))
    if output_tokens is None:
        output_tokens = _as_int(usage.get("output_tokens"))
    cached = None
    prompt_details = usage.get("prompt_tokens_details")
    if isinstance(prompt_details, Mapping):
        cached = _as_int(prompt_details.get("cached_tokens"))
    if cached is None:
        cached = _as_int(usage.get("cached_tokens"))
    if cached is None:
        cached = _as_int(usage.get("prompt_cache_hit_tokens"))
    if cached is None:
        cached = _as_int(usage.get("cache_read_input_tokens"))
    reasoning = None
    completion_details = usage.get("completion_tokens_details")
    if isinstance(completion_details, Mapping):
        reasoning = _as_int(completion_details.get("reasoning_tokens"))
        if reasoning is None:
            reasoning = _as_int(completion_details.get("thinking_tokens"))
    if reasoning is None:
        reasoning = _as_int(usage.get("reasoning_tokens"))
    if reasoning is None:
        reasoning = _as_int(usage.get("thinking_tokens"))
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": _as_int(usage.get("total_tokens")),
        "cached_input_tokens": cached,
        "reasoning_tokens": reasoning,
    }


def attach_usage(inspection: PayloadInspection, usage: Mapping[str, Any] | None) -> PayloadInspection:
    return replace(inspection, usage=extract_usage_metrics(usage))


def _partition_from_value(value: Any) -> PayloadPartition:
    raw = canonical_json_bytes(value)
    return PayloadPartition(
        bytes=len(raw),
        estimated_tokens=estimate_prompt_tokens(raw.decode("utf-8")),
        hash=hashlib.sha256(raw).hexdigest(),
    )


def _empty_partition() -> PayloadPartition:
    digest = hashlib.sha256(b"").hexdigest()
    return PayloadPartition(bytes=0, estimated_tokens=0, hash=digest)


def _count_image_parts(messages: list[Any]) -> int:
    count = 0
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, Mapping) and part.get("type") == "image_url":
                count += 1
    return count


def _message_content_text(message: Mapping[str, Any] | None) -> str:
    if not message:
        return ""
    content = message.get("content")
    return content.strip() if isinstance(content, str) else ""


def _split_runtime_message(
    messages: list[Any],
    runtime_context: str,
) -> tuple[list[Any], Mapping[str, Any] | None, bool]:
    cleaned = str(runtime_context or "").strip()
    if not cleaned:
        return list(messages), None, True
    if not messages:
        return [], None, False
    last = messages[-1] if isinstance(messages[-1], Mapping) else None
    last_text = _message_content_text(last)
    wrapped = (_RUNTIME_USER_PREFIX + cleaned).strip()
    if last is not None and last_text in {cleaned, wrapped}:
        return list(messages[:-1]), last, True
    return list(messages), None, False


def inspect_chat_payload(
    payload: Mapping[str, Any],
    *,
    runtime_context: str = "",
    interaction_id: str = "",
    request_index: int = 0,
    request_purpose: str | None = None,
    endpoint: str = "",
    model: str = "",
    previous: PayloadInspection | None = None,
    previous_payload: Mapping[str, Any] | None = None,
    usage: Mapping[str, Any] | None = None,
) -> PayloadInspection:
    messages = list(payload.get("messages") or [])
    first = messages[0] if messages and isinstance(messages[0], Mapping) else None
    system_message = first if first is not None and first.get("role") == "system" else None
    remainder = messages[1:] if system_message is not None else list(messages)
    conversation, runtime_message, runtime_clear = _split_runtime_message(
        remainder, runtime_context
    )
    image_parts = _count_image_parts(messages)
    if image_parts:
        image = _partition_from_value(
            {
                "estimated_tokens": image_parts * ESTIMATED_IMAGE_PART_TOKENS,
                "image_parts": image_parts,
            }
        )
        image = PayloadPartition(
            bytes=image.bytes,
            estimated_tokens=image_parts * ESTIMATED_IMAGE_PART_TOKENS,
            hash=image.hash,
        )
    else:
        image = _empty_partition()

    purpose = normalize_request_purpose(request_purpose)
    resolved_model = str(model or payload.get("model") or "")
    current_raw = canonical_json_bytes(dict(payload))
    whole = PayloadPartition(
        bytes=len(current_raw),
        estimated_tokens=estimate_prompt_tokens(current_raw.decode("utf-8")),
        hash=hashlib.sha256(current_raw).hexdigest(),
    )
    stable_bytes: int | None = None
    stable_hash: str | None = None
    previous_raw = (
        canonical_json_bytes(dict(previous_payload))
        if previous_payload is not None
        else None
    )
    if (
        previous is not None
        and isinstance(previous_raw, (bytes, bytearray))
        and previous.request_purpose == purpose
        and previous.model == resolved_model
        and previous.endpoint == endpoint
    ):
        prefix = _common_prefix(bytes(previous_raw), current_raw)
        if prefix:
            stable_bytes = len(prefix)
            stable_hash = hashlib.sha256(prefix).hexdigest()

    partitions: dict[str, PayloadPartition | None] = {
        "system": _partition_from_value(system_message or ""),
        "messages": _partition_from_value(conversation),
        "runtime": (
            None
            if not runtime_clear
            else (
                _empty_partition()
                if runtime_message is None
                else _partition_from_value(runtime_message)
            )
        ),
        "tools": _partition_from_value(payload.get("tools") or []),
        "image": image,
        "whole": whole,
    }
    inspection = PayloadInspection(
        interaction_id=str(interaction_id or ""),
        request_index=int(request_index or 0),
        request_purpose=purpose,
        model=resolved_model,
        endpoint=str(endpoint or ""),
        partitions=partitions,
        stable_prefix_bytes=stable_bytes,
        stable_prefix_hash=stable_hash,
        usage=extract_usage_metrics(usage),
    )
    return inspection


def _common_prefix(left: bytes, right: bytes) -> bytes:
    index = 0
    limit = min(len(left), len(right))
    while index < limit and left[index] == right[index]:
        index += 1
    return left[:index]


def inspection_log_dict(inspection: PayloadInspection) -> dict[str, Any]:
    partitions: dict[str, Any] = {}
    for name, part in inspection.partitions.items():
        if part is None:
            partitions[name] = None
            continue
        partitions[name] = {
            "bytes": part.bytes,
            "estimated_tokens": part.estimated_tokens,
            "hash": part.hash,
        }
    return {
        "interaction_id": inspection.interaction_id,
        "request_index": inspection.request_index,
        "request_purpose": inspection.request_purpose,
        "model": inspection.model,
        "endpoint": inspection.endpoint,
        "partitions": partitions,
        "stable_prefix_bytes": inspection.stable_prefix_bytes,
        "stable_prefix_hash": inspection.stable_prefix_hash,
        "usage": dict(inspection.usage),
    }


def synthetic_measurement_cases() -> list[dict[str, Any]]:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "synthetic_lookup",
                "description": "synthetic-tool-schema",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            },
        }
    ]
    short_messages = [
        {"role": "system", "content": "synthetic-system-short"},
        {"role": "user", "content": "synthetic-user-a"},
        {"role": "assistant", "content": "synthetic-assistant-a"},
        {"role": "user", "content": "synthetic-user-b"},
        {"role": "system", "content": "synthetic-runtime-short"},
    ]
    long_messages: list[dict[str, Any]] = [{"role": "system", "content": "synthetic-system-long"}]
    for index in range(1, 12):
        long_messages.append({"role": "user", "content": f"synthetic-user-{index:02d}"})
        long_messages.append({"role": "assistant", "content": f"synthetic-assistant-{index:02d}"})
    long_messages.append({"role": "system", "content": "synthetic-runtime-long"})
    return [
        {
            "id": "short_chat",
            "request_purpose": "initial",
            "runtime_context": "synthetic-runtime-short",
            "payload": {"model": "synthetic-deepseek", "messages": short_messages},
        },
        {
            "id": "long_history",
            "request_purpose": "initial",
            "runtime_context": "synthetic-runtime-long",
            "payload": {"model": "synthetic-deepseek", "messages": long_messages},
        },
        {
            "id": "image_placeholder",
            "request_purpose": "initial",
            "runtime_context": "",
            "payload": {
                "model": "synthetic-vision",
                "messages": [
                    {"role": "system", "content": "synthetic-system-vision"},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "synthetic-user-image"},
                            {
                                "type": "image_url",
                                "image_url": {"url": "https://synthetic.example/placeholder.png"},
                            },
                        ],
                    },
                ],
            },
        },
        {
            "id": "tool_round",
            "request_purpose": "tool_step",
            "runtime_context": "synthetic-runtime-tool",
            "payload": {
                "model": "synthetic-deepseek",
                "tools": tools,
                "messages": [
                    {"role": "system", "content": "synthetic-system-tool"},
                    {"role": "user", "content": "synthetic-user-tool"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-synthetic-1",
                                "type": "function",
                                "function": {"name": "synthetic_lookup", "arguments": "{\"q\":\"x\"}"},
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call-synthetic-1",
                        "content": "synthetic-tool-result-ok",
                    },
                    {"role": "system", "content": "synthetic-runtime-tool"},
                ],
            },
        },
        {
            "id": "structural_bad",
            "request_purpose": "structural_repair",
            "runtime_context": "",
            "payload": {
                "model": "synthetic-deepseek",
                "messages": [
                    {"role": "system", "content": "synthetic-min-schema"},
                    {"role": "user", "content": "synthetic-raw-output-fence"},
                ],
            },
        },
    ]


def build_synthetic_measurement_report(directory: Path | str) -> dict[str, Any]:
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    cases = []
    previous = None
    previous_payload = None
    for case in synthetic_measurement_cases():
        inspection = inspect_chat_payload(
            case["payload"],
            runtime_context=str(case.get("runtime_context") or ""),
            interaction_id="interaction-synthetic",
            request_index=len(cases) + 1,
            request_purpose=str(case.get("request_purpose") or "initial"),
            endpoint="https://synthetic.example/v1",
            model=str(case["payload"].get("model") or ""),
            previous=previous,
            previous_payload=previous_payload,
        )
        cases.append({"id": case["id"], "inspection": inspection_log_dict(inspection)})
        previous = inspection
        previous_payload = case["payload"]
    data = {
        "cases": cases,
        "usage_samples": {
            "deepseek_like": extract_usage_metrics(
                {
                    "prompt_tokens": 120,
                    "completion_tokens": 18,
                    "total_tokens": 138,
                    "prompt_tokens_details": {"cached_tokens": 50},
                    "completion_tokens_details": {"reasoning_tokens": 6},
                }
            ),
            "gemini_like": extract_usage_metrics(
                {
                    "prompt_tokens": 96,
                    "completion_tokens": 12,
                    "total_tokens": 108,
                }
            ),
        },
    }
    json_path = target / "session-context-measurement.json"
    markdown_path = target / "session-context-measurement.md"
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# session context measurement",
        "",
        f"cases: {len(cases)}",
        f"deepseek cached: {data['usage_samples']['deepseek_like']['cached_input_tokens']}",
        f"gemini cached: {data['usage_samples']['gemini_like']['cached_input_tokens']}",
        "",
    ]
    for item in cases:
        inspection = item["inspection"]
        whole = inspection["partitions"]["whole"]
        lines.append(
            f"- {item['id']}: purpose={inspection['request_purpose']} "
            f"whole_bytes={whole['bytes']} tokens={whole['estimated_tokens']}"
        )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json_path": str(json_path), "markdown_path": str(markdown_path)}
