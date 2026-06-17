"""MMDRendererConfig 路径解析与 payload 转换单元测试。

依赖 PySide6.QtCore（QUrl）；无 PySide6 环境跳过。覆盖中文/日文/空格相对路径
解析、缺失资源记录、event_motions/expressions/lip_sync 解析与 to_payload。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6.QtCore")

from plugins.mmd_renderer.config import MMDRendererConfig  # noqa: E402


def test_resolve_relative_path_with_unicode_and_space(tmp_path):
    pkg = tmp_path / "角色 包"
    (pkg / "mmd").mkdir(parents=True)
    model = pkg / "mmd" / "さくら model.pmx"
    model.write_text("x", encoding="utf-8")

    cfg = MMDRendererConfig.from_mapping(
        {"type": "mmd", "model": "mmd/さくら model.pmx", "scale": 1.5},
        pkg,
    )

    assert cfg.model_path == model.resolve()
    assert cfg.model_url is not None
    assert cfg.model_url.startswith("file:")
    # 空格在 file URL 中应被百分号编码，不出现裸空格。
    assert " " not in cfg.model_url
    assert cfg.scale == 1.5
    assert cfg.missing_paths == []


def test_absolute_path_preserved(tmp_path):
    model = tmp_path / "abs.pmx"
    model.write_text("x", encoding="utf-8")
    cfg = MMDRendererConfig.from_mapping({"model": str(model)}, tmp_path / "other")
    assert cfg.model_path == model.resolve()


def test_missing_model_recorded_not_raised(tmp_path):
    cfg = MMDRendererConfig.from_mapping({"type": "mmd", "model": "mmd/none.pmx"}, tmp_path)
    # 缺失不抛异常，仍生成 url，并记录到 missing_paths。
    assert cfg.model_url is not None
    assert any("none.pmx" in m for m in cfg.missing_paths)


def test_motions_resolved(tmp_path):
    (tmp_path / "m").mkdir()
    idle = tmp_path / "m" / "idle.vmd"
    idle.write_text("x", encoding="utf-8")
    cfg = MMDRendererConfig.from_mapping(
        {"motions": {"idle": "m/idle.vmd", "missing": "m/none.vmd"}},
        tmp_path,
    )
    assert "idle" in cfg.motions
    assert cfg.motion_paths["idle"] == idle.resolve()
    # 缺失动作仍登记 url 但进入 missing。
    assert any("none.vmd" in m for m in cfg.missing_paths)


def test_to_payload_shape(tmp_path):
    cfg = MMDRendererConfig.from_mapping(
        {
            "type": "mmd",
            "scale": 1.0,
            "expressions": {"happy": {"笑い": 1.0}, "neutral": {}},
            "lip_sync": {"morph": "あ", "strength": 0.8},
            "event_motions": {"tts.started": "talk", "app.started": "greeting"},
        },
        tmp_path,
    )
    payload = cfg.to_payload()
    assert payload["type"] == "mmd"
    assert payload["scale"] == 1.0
    assert payload["expressions"]["happy"] == {"笑い": 1.0}
    assert payload["expressions"]["neutral"] == {}
    assert payload["lipSync"] == {"morph": "あ", "strength": 0.8}
    assert payload["eventMotions"] == {"tts.started": "talk", "app.started": "greeting"}


def test_invalid_scale_defaults(tmp_path):
    cfg = MMDRendererConfig.from_mapping({"scale": "not-a-number"}, tmp_path)
    assert cfg.scale == 1.0
