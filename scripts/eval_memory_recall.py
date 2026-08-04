"""对比记忆自动召回 query：baseline 拼接 vs 启发式改写。

用法（项目根目录）:
  python scripts/eval_memory_recall.py
  python scripts/eval_memory_recall.py --k 5
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agent.memory_recall_eval import (
    baseline_query_builder,
    default_fixture_dir,
    format_report,
    heuristic_query_builder,
    load_cases,
    load_corpus,
    run_lexical_eval,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Memory recall query eval (frozen fixtures)")
    parser.add_argument(
        "--fixture-dir",
        type=Path,
        default=default_fixture_dir(),
        help="含 corpus.json / cases.json 的目录",
    )
    parser.add_argument("--k", type=int, default=5, help="recall@k")
    args = parser.parse_args()

    corpus = load_corpus(args.fixture_dir / "corpus.json")
    cases = load_cases(args.fixture_dir / "cases.json")
    if not corpus or not cases:
        print("fixtures 为空，请检查 tests/fixtures/memory_recall_eval/", file=sys.stderr)
        return 2

    baseline = run_lexical_eval(
        corpus=corpus,
        cases=cases,
        query_builder=baseline_query_builder,
        mode="baseline",
        k=args.k,
    )
    heuristic = run_lexical_eval(
        corpus=corpus,
        cases=cases,
        query_builder=heuristic_query_builder,
        mode="heuristic",
        k=args.k,
    )

    print(format_report(baseline))
    print()
    print(format_report(heuristic))
    print()
    delta = heuristic.mean_recall - baseline.mean_recall
    dirty_delta = heuristic.contamination_rate - baseline.contamination_rate
    print(
        f"delta mean_recall@{args.k}: {delta:+.3f}  "
        f"(hit_rate {baseline.hit_rate:.3f} -> {heuristic.hit_rate:.3f}; "
        f"contamination {baseline.contamination_rate:.3f} -> {heuristic.contamination_rate:.3f}, "
        f"delta {dirty_delta:+.3f})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
