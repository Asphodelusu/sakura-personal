"""全链路实测：DeepSeek + 智谱搜索 + 过程旁白 + 终局回复 + 应说话句送入 TTS。

不启桌宠窗口，但尽量复用 bootstrap / AgentRuntime / MCP / GPT-SoVITS。

    python scripts/e2e_web_lookup_fullchain.py
    python scripts/e2e_web_lookup_fullchain.py "搜一下《无主之地》是什么游戏"
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

from app.agent.actions import AgentProgress
from app.core.bootstrap import build_deferred_services, build_initial_app_context
from app.core.debug_log import debug_log
from app.llm.chat_reply import ChatSegment


def _speak_via_tts(provider, segment: ChatSegment, *, index: int) -> dict:
    """把应播报的句子送进真实 TTS，并等待合成/开播回调。"""
    started = time.perf_counter()
    result = {
        "index": index,
        "text": segment.text,
        "tone": segment.tone,
        "zh": segment.translation,
        "ok": False,
        "error": "",
        "elapsed_ms": 0,
    }
    if not segment.text.strip():
        result["error"] = "empty_text"
        return result

    loop = QEventLoop()
    state = {"started": False, "finished": False}

    def on_started() -> None:
        state["started"] = True
        debug_log("E2E", "TTS 开始", {"index": index, "text": segment.text[:80]})

    def on_finished() -> None:
        state["finished"] = True
        if loop.isRunning():
            loop.quit()

    # 防止卡住
    QTimer.singleShot(120_000, loop.quit)
    try:
        provider.speak(
            segment.text,
            segment.tone or "中性",
            on_started=on_started,
            on_finished=on_finished,
        )
        loop.exec()
        result["ok"] = bool(state["started"] or state["finished"])
        if not result["ok"]:
            result["error"] = "tts_callback_timeout"
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
    result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    return result


def main() -> int:
    query = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "搜一下《无主之地》是什么游戏"
    )
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)

    context = build_initial_app_context(ROOT)
    deferred = build_deferred_services(ROOT, context)
    runtime = context.agent_runtime
    runtime.tools = deferred.tool_registry
    tts = deferred.tts_provider

    print("=== bootstrap ===")
    print(
        json.dumps(
            {
                "model": context.settings.model,
                "tool_count": len(runtime.tools.all()),
                "has_web_search": runtime.tools.get("web__web_search") is not None,
                "has_playwright": runtime.tools.get("playwright_navigate") is not None,
                "tts_provider": type(tts).__name__,
                "deferred_errors": list(deferred.errors),
                "query": query,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if runtime.tools.get("web__web_search") is None:
        print("FAIL: web__web_search 未注册", file=sys.stderr)
        return 2

    progress_events: list[dict] = []
    tts_jobs: list[dict] = []

    def on_progress(progress: AgentProgress) -> None:
        reply = progress.reply
        segs = []
        for seg in reply.segments:
            item = {
                "stage": progress.stage,
                "ja": seg.text,
                "zh": seg.translation,
                "tone": seg.tone,
                "suppress_tts": bool(seg.suppress_tts),
                "tts_route": "skip_bubble_only" if seg.suppress_tts else "speak",
            }
            segs.append(item)
            # 过程旁白约定 suppress_tts=True，不应进 TTS
            if not seg.suppress_tts and seg.text.strip():
                tts_jobs.append(
                    {
                        "source": f"progress:{progress.stage}",
                        "segment": seg,
                    }
                )
        progress_events.append(
            {
                "stage": progress.stage,
                "metadata": progress.metadata,
                "segments": segs,
            }
        )
        print(
            f"[progress:{progress.stage}] "
            + " | ".join(
                f"{'（旁白）' if s['suppress_tts'] else '（应TTS）'}{s['zh'] or s['ja']}"
                for s in segs
            )
        )

    print("\n=== agent turn ===")
    t0 = time.perf_counter()
    result = runtime.handle_user_message(
        [{"role": "user", "content": query}],
        progress_callback=on_progress,
    )
    agent_ms = int((time.perf_counter() - t0) * 1000)

    final_segments = []
    for seg in result.reply.segments:
        final_segments.append(
            {
                "ja": seg.text,
                "zh": seg.translation,
                "tone": seg.tone,
                "suppress_tts": bool(seg.suppress_tts),
                "tts_route": "skip" if seg.suppress_tts else "speak",
            }
        )
        if not seg.suppress_tts and seg.text.strip():
            tts_jobs.append({"source": "final", "segment": seg})

    print("\n=== final reply ===")
    print(json.dumps(final_segments, ensure_ascii=False, indent=2))

    # 核对 tool 证据是否进了终局上下文（通过 actions 里是否有非空 results）
    search_actions = [
        a
        for a in result.actions
        if getattr(a, "type", "") == "tool_call"
        and isinstance(getattr(a, "payload", None), dict)
        and str(a.payload.get("tool_name") or "").endswith("web_search")
    ]
    search_payloads = []
    for action in search_actions:
        content = action.payload.get("content") if isinstance(action.payload, dict) else None
        if isinstance(content, dict):
            search_payloads.append(
                {
                    "success": action.payload.get("success"),
                    "source": content.get("source"),
                    "result_count": len(content.get("results") or [])
                    if isinstance(content.get("results"), list)
                    else 0,
                    "digest_chars": len(str(content.get("digest") or "")),
                    "titles": [
                        str(row.get("title") or "")[:40]
                        for row in (content.get("results") or [])[:3]
                        if isinstance(row, dict)
                    ],
                }
            )

    print("\n=== search payloads seen by model path ===")
    print(json.dumps(search_payloads, ensure_ascii=False, indent=2))

    print("\n=== TTS speak queue ===")
    speak_results = []
    for index, job in enumerate(tts_jobs, start=1):
        seg = job["segment"]
        print(f"-> TTS[{index}] source={job['source']} tone={seg.tone} text={seg.text}")
        speak_results.append(
            {
                "source": job["source"],
                **_speak_via_tts(tts, seg, index=index),
            }
        )
        # 给播放一点空隙，避免队列打架
        time.sleep(0.3)

    # 等播放队列尽量收尾
    QTimer.singleShot(2000, app.quit)
    app.exec()

    report = {
        "query": query,
        "agent_ms": agent_ms,
        "progress_count": len(progress_events),
        "progress": progress_events,
        "final_segments": final_segments,
        "search_payloads": search_payloads,
        "tts_speak_count": len(speak_results),
        "tts_results": [
            {
                "source": item["source"],
                "index": item["index"],
                "ok": item["ok"],
                "error": item["error"],
                "elapsed_ms": item["elapsed_ms"],
                "text": item["text"][:80],
            }
            for item in speak_results
        ],
        "checks": {
            "web_search_registered": True,
            "search_has_hits": any(
                item.get("result_count", 0) > 0 or item.get("digest_chars", 0) > 40
                for item in search_payloads
            ),
            "final_not_empty_claim": not any(
                "空" in (seg.get("zh") or "") or "空" in (seg.get("ja") or "")
                for seg in final_segments
            ),
            "progress_suppressed_from_tts": all(
                seg["suppress_tts"]
                for event in progress_events
                for seg in event["segments"]
            ),
            "all_speakable_sent_to_tts": all(item["ok"] for item in speak_results)
            if speak_results
            else False,
        },
    }
    print("\n=== report ===")
    print(json.dumps(report, ensure_ascii=False, indent=2))

    ok = (
        report["checks"]["search_has_hits"]
        and report["checks"]["all_speakable_sent_to_tts"]
        and bool(final_segments)
    )
    # 关闭 MCP / TTS
    try:
        if deferred.mcp_tool_provider is not None:
            deferred.mcp_tool_provider.close()
    except Exception:
        pass
    try:
        close = getattr(tts, "close", None)
        if callable(close):
            close()
    except Exception:
        pass
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
