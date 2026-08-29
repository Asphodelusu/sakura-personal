"""无真人内容、无真实网络的 P0 合成夹具与报告。"""

from __future__ import annotations

import json
from pathlib import Path

from app.llm.payload_inspection import (
    build_synthetic_measurement_report,
    inspect_chat_payload,
    inspection_log_dict,
    synthetic_measurement_cases,
)


BANNED = (
    "亲子丼",
    "Sakura.db",
    "sk-",
    "data:image",
    "api_key",
)


def test_synthetic_fixtures_cover_required_shapes_and_write_tmp_report(tmp_path: Path) -> None:
    cases = {item["id"]: item for item in synthetic_measurement_cases()}
    for required in (
        "short_chat",
        "long_history",
        "image_placeholder",
        "tool_round",
        "structural_bad",
    ):
        assert required in cases
        payload = cases[required]["payload"]
        assert isinstance(payload, dict)
        assert "messages" in payload

    long_users = [
        message
        for message in cases["long_history"]["payload"]["messages"]
        if message.get("role") == "user"
    ]
    assert len(long_users) > 8

    image_text = json.dumps(cases["image_placeholder"]["payload"], ensure_ascii=False)
    assert "image_url" in image_text
    assert "data:image" not in image_text

    tool_payload = cases["tool_round"]["payload"]
    assert tool_payload.get("tools")
    assert any(message.get("role") == "tool" for message in tool_payload["messages"])

    report = build_synthetic_measurement_report(tmp_path)
    json_path = Path(report["json_path"])
    md_path = Path(report["markdown_path"])
    assert json_path.is_file()
    assert md_path.is_file()
    assert str(tmp_path) in str(json_path)
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["cases"]
    dumped = json.dumps(data, ensure_ascii=False) + md_path.read_text(encoding="utf-8")
    for banned in BANNED:
        assert banned not in dumped

    deepseek = data["usage_samples"]["deepseek_like"]
    gemini = data["usage_samples"]["gemini_like"]
    assert deepseek["cached_input_tokens"] is not None
    assert deepseek["reasoning_tokens"] is not None
    assert gemini["cached_input_tokens"] is None
    assert gemini["reasoning_tokens"] is None
    assert deepseek != gemini


def test_fixture_inspections_never_embed_prompt_body() -> None:
    cases = synthetic_measurement_cases()
    assert cases
    for case in cases:
        inspection = inspect_chat_payload(
            case["payload"],
            runtime_context=str(case.get("runtime_context") or ""),
            request_purpose=str(case.get("request_purpose") or "initial"),
        )
        log = json.dumps(inspection_log_dict(inspection), ensure_ascii=False)
        for message in case["payload"]["messages"]:
            content = message.get("content")
            if isinstance(content, str) and len(content) >= 8:
                assert content not in log
