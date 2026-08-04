"""冻结语料上的记忆召回 eval（尺子，不改线上活库）。

默认用词面重合做轻量检索代理，用来对比 baseline / heuristic / llm query；
不依赖嵌入模型，可在 CI 里稳定跑。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.agent.memory_query_rewrite import (
    MemoryQueryPlan,
    build_baseline_memory_query,
    context_request_from_parts,
    rewrite_memory_query_heuristic,
)
from app.llm.prompts.types import ContextRequest


_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{1,}")
_KANA_RE = re.compile(r"[\u3040-\u30ff]{2,}")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")


@dataclass(frozen=True)
class EvalMemory:
    id: str
    content: str


@dataclass(frozen=True)
class EvalCase:
    id: str
    current_input: str
    recent_user_messages: tuple[str, ...]
    visual_summaries: tuple[str, ...]
    expected_ids: tuple[str, ...]
    forbidden_ids: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class CaseScore:
    case_id: str
    query: str
    query_source: str
    hit_ids: tuple[str, ...]
    expected_ids: tuple[str, ...]
    forbidden_hits: tuple[str, ...]
    recall_at_k: float
    hit: bool
    contaminated: bool


@dataclass(frozen=True)
class EvalReport:
    mode: str
    k: int
    scores: tuple[CaseScore, ...]

    @property
    def mean_recall(self) -> float:
        if not self.scores:
            return 0.0
        return sum(item.recall_at_k for item in self.scores) / len(self.scores)

    @property
    def hit_rate(self) -> float:
        if not self.scores:
            return 0.0
        return sum(1 for item in self.scores if item.hit) / len(self.scores)

    @property
    def contamination_rate(self) -> float:
        """top-k 里出现 forbidden 干扰记忆的比例。"""
        if not self.scores:
            return 0.0
        return sum(1 for item in self.scores if item.contaminated) / len(self.scores)


def default_fixture_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "memory_recall_eval"


def load_corpus(path: Path) -> list[EvalMemory]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("memories") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError(f"corpus 格式错误：{path}")
    memories: list[EvalMemory] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        content = str(item.get("content") or "").strip()
        if mid and content:
            memories.append(EvalMemory(id=mid, content=content))
    return memories


def load_cases(path: Path) -> list[EvalCase]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("cases") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError(f"cases 格式错误：{path}")
    cases: list[EvalCase] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id") or "").strip()
        current = str(item.get("current_input") or "").strip()
        expected = item.get("expected_ids") or []
        if not cid or not current or not isinstance(expected, list) or not expected:
            continue
        recent = item.get("recent_user_messages") or []
        visuals = item.get("visual_summaries") or []
        forbidden = item.get("forbidden_ids") or []
        cases.append(
            EvalCase(
                id=cid,
                current_input=current,
                recent_user_messages=tuple(str(x).strip() for x in recent if str(x).strip()),
                visual_summaries=tuple(str(x).strip() for x in visuals if str(x).strip()),
                expected_ids=tuple(str(x).strip() for x in expected if str(x).strip()),
                forbidden_ids=tuple(str(x).strip() for x in forbidden if str(x).strip())
                if isinstance(forbidden, list)
                else (),
                notes=str(item.get("notes") or "").strip(),
            )
        )
    return cases


def tokenize(text: str) -> set[str]:
    """中文按 2~3 字滑窗切，避免整句并成一个超长 token 导致重合永远为 0。"""
    raw = text or ""
    tokens: set[str] = {m.group(0).lower() for m in _LATIN_RE.finditer(raw)}
    tokens.update(m.group(0) for m in _KANA_RE.finditer(raw))
    for run in _CJK_RUN_RE.findall(raw):
        if len(run) <= 3:
            tokens.add(run)
            continue
        for size in (2, 3):
            for index in range(0, len(run) - size + 1):
                tokens.add(run[index : index + size])
    return tokens


def lexical_rank(corpus: list[EvalMemory], query: str, *, limit: int) -> list[str]:
    """按 query 与记忆正文的词面重合排序，作为不依赖嵌入的检索代理。"""
    q_tokens = tokenize(query)
    if not q_tokens:
        return []
    scored: list[tuple[float, int, str]] = []
    for index, memory in enumerate(corpus):
        m_tokens = tokenize(memory.content)
        if not m_tokens:
            continue
        overlap = len(q_tokens & m_tokens)
        if overlap <= 0:
            # 整段子串命中也给一点分（专有名词）
            content_l = memory.content.lower()
            substr = sum(1 for token in q_tokens if len(token) >= 2 and token in content_l)
            if substr <= 0:
                continue
            score = substr * 0.5
        else:
            score = overlap + (overlap / max(1, len(q_tokens)))
        scored.append((score, index, memory.id))
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [mid for _score, _index, mid in scored[: max(0, limit)]]


def score_retrieval(
    *,
    expected_ids: tuple[str, ...],
    ranked_ids: list[str],
    k: int,
) -> tuple[float, bool, tuple[str, ...]]:
    top = tuple(ranked_ids[: max(1, k)])
    expected = set(expected_ids)
    if not expected:
        return 0.0, False, top
    hits = expected & set(top)
    recall = len(hits) / len(expected)
    return recall, bool(hits), top


QueryBuilder = Callable[[ContextRequest], MemoryQueryPlan]


def baseline_query_builder(request: ContextRequest) -> MemoryQueryPlan:
    return MemoryQueryPlan(query=build_baseline_memory_query(request), source="baseline")


def heuristic_query_builder(request: ContextRequest) -> MemoryQueryPlan:
    return rewrite_memory_query_heuristic(request)


def run_lexical_eval(
    *,
    corpus: list[EvalMemory],
    cases: list[EvalCase],
    query_builder: QueryBuilder,
    mode: str,
    k: int = 5,
) -> EvalReport:
    scores: list[CaseScore] = []
    for case in cases:
        request = context_request_from_parts(
            current_input=case.current_input,
            recent_user_messages=list(case.recent_user_messages),
            visual_summaries=list(case.visual_summaries),
        )
        plan = query_builder(request)
        ranked = lexical_rank(corpus, plan.query, limit=max(k, 10))
        recall, hit, top = score_retrieval(
            expected_ids=case.expected_ids,
            ranked_ids=ranked,
            k=k,
        )
        forbidden_hits = tuple(mid for mid in top if mid in set(case.forbidden_ids))
        scores.append(
            CaseScore(
                case_id=case.id,
                query=plan.query,
                query_source=plan.source,
                hit_ids=top,
                expected_ids=case.expected_ids,
                forbidden_hits=forbidden_hits,
                recall_at_k=recall,
                hit=hit,
                contaminated=bool(forbidden_hits),
            )
        )
    return EvalReport(mode=mode, k=k, scores=tuple(scores))


def format_report(report: EvalReport) -> str:
    lines = [
        f"mode={report.mode}  k={report.k}  cases={len(report.scores)}  "
        f"mean_recall@{report.k}={report.mean_recall:.3f}  hit_rate={report.hit_rate:.3f}  "
        f"contamination={report.contamination_rate:.3f}",
        "",
    ]
    for item in report.scores:
        mark = "OK" if item.hit and not item.contaminated else ("DIRTY" if item.hit else "MISS")
        lines.append(
            f"[{mark}] {item.case_id}  recall={item.recall_at_k:.2f}  "
            f"src={item.query_source}"
        )
        lines.append(f"  query: {item.query.replace(chr(10), ' | ')}")
        lines.append(f"  expect: {', '.join(item.expected_ids)}")
        if item.forbidden_hits:
            lines.append(f"  forbidden_hits: {', '.join(item.forbidden_hits)}")
        lines.append(f"  top: {', '.join(item.hit_ids) or '(none)'}")
    return "\n".join(lines)
