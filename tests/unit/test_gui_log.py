from __future__ import annotations

import pytest

from app.core.debug_log import debug_log
from app.core.gui_log import (
    GUI_LOG_SCOPE_PROGRAM,
    GUI_LOG_SCOPE_TTS,
    GuiLogBuffer,
    clear_gui_logs,
    get_gui_log_buffer,
    record_debug_log_for_gui,
    record_tts_service_output,
)


@pytest.fixture(autouse=True)
def clear_logs_after_test():  # type: ignore[no-untyped-def]
    clear_gui_logs()
    yield
    clear_gui_logs()


def test_gui_log_records_even_when_terminal_and_file_logs_disabled(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("app.core.debug_log._load_debug_values", lambda: {})

    debug_log("TTS", "发送 GPT-SoVITS 请求", {"api_key": "sk-secret", "text": "不应完整显示的语音文本"})

    assert capsys.readouterr().out == ""
    records = get_gui_log_buffer().snapshot(scope=GUI_LOG_SCOPE_PROGRAM)
    assert len(records) == 1
    assert records[0].message == "送入 TTS：GPT-SoVITS"


def test_gui_log_routes_program_and_native_tts_records_to_separate_scopes() -> None:
    record_debug_log_for_gui("Startup", "应用初始化完成")
    record_tts_service_output("GPT-SoVITS", 'INFO: 127.0.0.1:49840 - "POST /tts HTTP/1.1" 200 OK')

    program_records = get_gui_log_buffer().snapshot(scope=GUI_LOG_SCOPE_PROGRAM)
    tts_records = get_gui_log_buffer().snapshot(scope=GUI_LOG_SCOPE_TTS)

    assert [record.message for record in program_records] == ["应用初始化完成"]
    assert [record.message for record in tts_records] == ["HTTP POST /tts -> 200 OK"]


def test_gui_log_sanitizes_sensitive_and_private_text_detail() -> None:
    record_debug_log_for_gui(
        "TTS",
        "发送 GPT-SoVITS 请求",
        {
            "api_key": "sk-secret",
            "text": "不应完整显示的语音文本",
            "payload": {
                "prompt_text": "参考文本也不能完整显示",
                "top_k": 15,
            },
        },
    )

    detail = get_gui_log_buffer().snapshot(scope=GUI_LOG_SCOPE_PROGRAM)[0].detail
    assert "<redacted>" in detail
    assert "不应完整显示的语音文本" not in detail
    assert "参考文本也不能完整显示" not in detail
    assert '"chars"' in detail


def test_gui_log_compacts_software_request_and_reply_events() -> None:
    record_debug_log_for_gui("API", "准备发送原生工具聊天补全请求", {"error_count": 0})
    record_debug_log_for_gui("API", "HTTP 请求体已构建", {"error_count": 0})
    record_debug_log_for_gui("API", "原生工具模型返回", {"content": "不应完整显示"})
    record_debug_log_for_gui("Startup", "后台启动服务已创建", {"error_count": 0})

    records = get_gui_log_buffer().snapshot(scope=GUI_LOG_SCOPE_PROGRAM)

    assert [(record.level, record.message) for record in records] == [
        ("info", "发送请求"),
        ("info", "收到回复"),
        ("info", "后台启动服务已创建"),
    ]


def test_gui_log_keeps_key_tts_software_events() -> None:
    record_debug_log_for_gui("TTS", "提交播放请求")
    record_debug_log_for_gui("TTS", "提交预生成请求")
    record_debug_log_for_gui("TTS", "请求播放预生成音频")
    record_debug_log_for_gui("TTS", "开始播放音频")
    record_debug_log_for_gui("TTS", "发送 GPT-SoVITS 请求")
    record_debug_log_for_gui("TTS", "GPT-SoVITS 请求成功")
    record_debug_log_for_gui("TTS", "预生成音频已就绪")
    record_debug_log_for_gui("TTS", "音频播放完成")
    record_debug_log_for_gui("TTS", "临时音频已写入")
    record_debug_log_for_gui("ChatWorker", "处理完成")

    records = get_gui_log_buffer().snapshot(scope=GUI_LOG_SCOPE_PROGRAM)

    assert [record.message for record in records] == [
        "开始播放",
        "送入 TTS：GPT-SoVITS",
        "合成完成：GPT-SoVITS",
        "播放完成",
    ]


def test_gui_log_keeps_internal_preparation_events_for_troubleshooting() -> None:
    record_debug_log_for_gui("TTS", "本地 GPT-SoVITS 服务启动并探测成功")
    record_debug_log_for_gui("TTS", "角色权重切换完成")

    records = get_gui_log_buffer().snapshot(scope=GUI_LOG_SCOPE_PROGRAM)

    assert [record.message for record in records] == [
        "准备：GPT-SoVITS 服务已就绪",
        "准备：TTS 角色权重切换完成",
    ]


def test_tts_service_output_is_compacted_without_full_text() -> None:
    record_tts_service_output("GPT-SoVITS", "########## 合成音频 ##########")
    record_tts_service_output("GPT-SoVITS", "实际输入的目标文本(切句后): ['そんなの当たり前だっ。']")
    record_tts_service_output("GPT-SoVITS", "100%|██████████| 1/1 [00:00<00:00, 111.08it/s]")

    records = get_gui_log_buffer().snapshot(scope=GUI_LOG_SCOPE_TTS)

    assert [record.message for record in records] == [
        "开始合成音频",
        "收到合成文本（11 字）",
        "语义 token 预测 100%（1/1，111.08 it/s）",
    ]
    assert all("そんなの" not in record.message for record in records)


def test_tts_service_semantic_progress_merges_in_place() -> None:
    record_tts_service_output("GPT-SoVITS", "########## 合成音频 ##########")
    record_tts_service_output("GPT-SoVITS", "  4%|▍         | 60/1500 [00:00<00:13, 104.91it/s]")
    record_tts_service_output("GPT-SoVITS", " 24%|██▍       | 363/1500 [00:03<00:10, 105.23it/s]")

    records = get_gui_log_buffer().snapshot(scope=GUI_LOG_SCOPE_TTS)

    # 相邻进度记录原地合并，只保留最新一条
    assert [record.message for record in records] == [
        "开始合成音频",
        "语义 token 预测 24%（363/1500，105.23 it/s）",
    ]
    assert records[-1].merge_key != ""


def test_tts_service_eos_replaces_progress_and_drops_trailing_frame() -> None:
    record_tts_service_output("GPT-SoVITS", " 24%|██▍       | 363/1500 [00:03<00:10, 105.23it/s]")
    record_tts_service_output("GPT-SoVITS", "T2S Decoding EOS [128 -> 363]")
    # tqdm 收尾时停在中途百分比的最终帧不应覆盖 EOS 完成行
    record_tts_service_output("GPT-SoVITS", " 24%|██▍       | 363/1500 [00:03<00:10, 105.23it/s]")

    records = get_gui_log_buffer().snapshot(scope=GUI_LOG_SCOPE_TTS)

    assert [record.message for record in records] == [
        "语义 token 预测完成：EOS（128 → 363）",
    ]


def test_gui_log_keeps_tts_text_preview_for_display() -> None:
    record_debug_log_for_gui(
        "TTS",
        "发送 GPT-SoVITS 请求",
        {"text": "今天天气真好喵", "api_key": "sk-secret"},
    )
    record_debug_log_for_gui("API", "模型原始文本返回", {"text": "API 文本不应出预览"})

    records = get_gui_log_buffer().snapshot(scope=GUI_LOG_SCOPE_PROGRAM)

    assert records[0].message == "送入 TTS：GPT-SoVITS"
    assert records[0].text_preview == "今天天气真好喵"
    # detail 字段仍按既有规则脱敏，仅 text_preview 提供展示用文本
    assert "今天天气真好喵" not in records[0].detail
    assert "<redacted>" in records[0].detail
    # 非 TTS 分类不提取预览
    assert records[1].text_preview == ""


def test_gui_log_buffer_keeps_scope_limited_ring() -> None:
    buffer = GuiLogBuffer(max_records_per_scope=3)

    for index in range(5):
        buffer.append(
            timestamp="2026-06-11T10:00:00+08:00",
            scope=GUI_LOG_SCOPE_PROGRAM,
            level="info",
            category="Test",
            message=f"item-{index}",
        )

    records = buffer.snapshot(scope=GUI_LOG_SCOPE_PROGRAM)
    assert [record.message for record in records] == ["item-2", "item-3", "item-4"]
