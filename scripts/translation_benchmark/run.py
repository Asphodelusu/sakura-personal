"""运行翻译基准：实际调用各 provider，统计指标，生成盲评文件。

用法:
  .venv/Scripts/python.exe scripts/translation_benchmark/run.py [--limit N] [--providers deepseek_flash qwen] [--workers 5]

输出:
  data/benchmark/results.json     指标报告
  data/benchmark/blind_review.md  人工盲评对照文件
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import providers

DATASET = Path("data/benchmark/dataset.jsonl")
OUT_DIR = Path("data/benchmark")


def pctile(values: list[float], p: int) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p / 100))]


def run_sample(provider, item: dict) -> dict:
    r = provider.translate(item["ja"])
    return {
        "id": item["id"],
        "ok": r.ok,
        "zh": r.zh,
        "latency_ms": round(r.latency_ms, 1),
        "error": r.error,
        "raw": r.raw,
        "zh_chars": len(r.zh),
    }


def compute_report(dataset: list[dict], results: dict[str, list[dict]]) -> dict:
    report = {}
    for name, per in results.items():
        lat = [p["latency_ms"] for p in per]
        oks = [p for p in per if p["ok"]]
        report[name] = {
            "n": len(per),
            "ok": len(oks),
            "fail": len(per) - len(oks),
            "fail_rate": round((len(per) - len(oks)) / len(per), 4),
            "structured_ok_rate": round(len(oks) / len(per), 4),
            "latency_p50_ms": round(pctile(lat, 50), 1),
            "latency_p90_ms": round(pctile(lat, 90), 1),
            "latency_mean_ms": round(statistics.mean(lat), 1) if lat else 0,
            "input_chars_mean": round(statistics.mean([i["ja_chars"] for i in dataset]), 1),
            "output_chars_mean": round(statistics.mean([p["zh_chars"] for p in oks]), 1) if oks else 0,
            "top_errors": Counter(p["error"][:50] for p in per if not p["ok"]).most_common(5),
        }
    return report


def _emit(out: Path, dataset: list[dict], results: dict[str, list[dict]]) -> None:
    """保存逐样本结果 + 聚合指标 + 盲评文件。"""
    out.mkdir(parents=True, exist_ok=True)
    (out / "per_sample_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = compute_report(dataset, results)
    (out / "results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_blind_review(out / "blind_review.md", dataset, results)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"盲评文件: {out / 'blind_review.md'}")


def write_blind_review(path: Path, dataset: list[dict], results: dict[str, list[dict]]) -> None:
    lines = [
        "# 翻译盲评对照",
        "",
        "> 对比各 provider 翻译 vs 现有链路参考翻译。评：哪个更贴合 Sakura 语气 / 更自然 / 更准确。",
        "> 标注为辅助标签，非精确判断。",
        "",
    ]
    for idx, item in enumerate(dataset, 1):
        labels = "、".join(item["labels"]) if item["labels"] else "—"
        lines.append(f"## {idx}. id={item['id']} | tone={item['tone']} | {labels}")
        lines.append("")
        lines.append(f"- JA: {item['ja']}")
        lines.append(f"- 参考(现有链路): {item['zh_reference']}")
        for name, per in results.items():
            found = next((p for p in per if p["id"] == item["id"]), None)
            if found is None:
                continue
            zh = found["zh"] if found["ok"] else f"(失败: {found['error'][:60]})"
            lines.append(f"- {name}: {zh}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="只取前 N 条样本")
    parser.add_argument("--providers", nargs="*", default=None, help="指定 provider 名")
    parser.add_argument("--workers", type=int, default=5, help="并发数")
    parser.add_argument("--out-dir", default=str(OUT_DIR))
    parser.add_argument("--from-results", action="store_true", help="从已保存 per_sample 结果重生成，不调 API")
    args = parser.parse_args()

    raw = DATASET.read_text(encoding="utf-8").splitlines()
    dataset = [json.loads(line) for line in raw if line.strip()]
    if args.limit:
        dataset = dataset[: args.limit]

    if args.from_results:
        per = json.loads((Path(args.out_dir) / "per_sample_results.json").read_text(encoding="utf-8"))
        _emit(Path(args.out_dir), dataset, per)
        return

    pmap = providers.make_providers()
    names = args.providers or [n for n, p in pmap.items() if p.available]
    names = [n for n in names if n in pmap and pmap[n].available]

    results: dict[str, list[dict]] = {}
    for name in names:
        prov = pmap[name]
        print(f"[run] {name} | {len(dataset)} 条 | workers={args.workers}")
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(run_sample, prov, item): item for item in dataset}
            per = [fut.result() for fut in as_completed(futures)]
        per.sort(key=lambda x: x["id"])
        results[name] = per

    _emit(Path(args.out_dir), dataset, results)


if __name__ == "__main__":
    main()
