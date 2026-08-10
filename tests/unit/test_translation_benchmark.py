"""翻译基准工具（scripts/translation_benchmark）的独立单元测试。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "translation_benchmark"))

import extract
import providers
import run


def test_private_benchmark_defaults_to_artifacts_directory():
    expected = Path(__file__).resolve().parents[2] / "artifacts/agent-benchmarks/translation-decoupling"
    assert extract.OUT_DIR == expected
    assert run.OUT_DIR == expected
    assert run.DATASET == expected / "dataset.jsonl"


# ---- 标注规则 ----
def test_annotate_jealousy():
    labels = extract.annotate("誰と比べてるの？", "", "吃醋", None)
    assert "吃醋" in labels


def test_annotate_deflection():
    labels = extract.annotate("別に、なんでもない。", "", "中性", None)
    assert "傲娇否定" in labels


def test_annotate_intimacy():
    labels = extract.annotate("もっと、ちゅーして", "", "亲密", None)
    assert "亲密语气" in labels


def test_annotate_request():
    labels = extract.annotate("ちょっと、してほしいことがあるの。", "", "请求", None)
    assert "请求" in labels


def test_annotate_address_term():
    labels = extract.annotate("先輩、聞いてますか？", "", "中性", None)
    assert "称谓" in labels


def test_annotate_omitted_subject():
    # 无显式第一/第二人称主语 → 省略主语
    labels = extract.annotate("そろそろ行くね。", "", "中性", None)
    assert "省略主语" in labels
    # 有显式主语 → 不标省略主语
    labels2 = extract.annotate("私は大丈夫。", "", "中性", None)
    assert "省略主语" not in labels2


# ---- JSON 解析 ----
def _prov() -> providers.OpenAILikeProvider:
    return providers.OpenAILikeProvider("t", "http://x", "k", "m")


def test_parse_json_zh_plain():
    assert _prov()._parse_json_zh('{"zh": "你好"}') == "你好"


def test_parse_json_zh_codeblock():
    assert _prov()._parse_json_zh('```json\n{"zh": "你好"}\n```') == "你好"


def test_parse_json_zh_invalid():
    assert _prov()._parse_json_zh("没有 JSON 输出") is None


def test_parse_json_zh_empty_zh():
    assert _prov()._parse_json_zh('{"zh": ""}') == ""


# ---- 指标计算 ----
def test_compute_report_pctile_and_rate():
    dataset = [
        {"ja_chars": 10}, {"ja_chars": 20}, {"ja_chars": 30}, {"ja_chars": 40},
    ]
    results = {
        "p": [
            {"id": 1, "ok": True, "zh": "a", "latency_ms": 10.0, "error": "", "zh_chars": 1},
            {"id": 2, "ok": True, "zh": "bb", "latency_ms": 20.0, "error": "", "zh_chars": 2},
            {"id": 3, "ok": False, "zh": "", "latency_ms": 30.0, "error": "e", "zh_chars": 0},
            {"id": 4, "ok": True, "zh": "cc", "latency_ms": 40.0, "error": "", "zh_chars": 2},
        ]
    }
    rep = run.compute_report(dataset, results)["p"]
    assert rep["n"] == 4
    assert rep["ok"] == 3
    assert rep["fail_rate"] == 0.25
    assert rep["structured_ok_rate"] == 0.75
    assert rep["latency_p50_ms"] == 30.0
    assert rep["latency_p90_ms"] == 40.0
    assert rep["latency_mean_ms"] == 25.0
    assert rep["input_chars_mean"] == 25.0
    assert rep["output_chars_mean"] == 1.7  # round((1+2+2)/3, 1)
