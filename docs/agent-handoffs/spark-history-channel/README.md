# Spark benchmark — proactive history channel

This batch is a bounded capability test for **5.3 Codex Spark** and a useful Sakura fix.

## Why it matters

`PetWindow._consume_agent_result(..., message_source=...)` preserves `proactive` or
`relationship` in the in-memory assistant message, but the segmented assistant reply
is persisted through `_record_assistant_reply_history()` without that source. After a
restart or history reload, the origin of an autonomous utterance is therefore lost.

## Desired outcome

- Every persisted non-empty assistant segment produced by an autonomous interaction
  carries the original history `channel`.
- `proactive` and `relationship` remain distinct.
- Ordinary user-triggered replies retain the existing default channel.
- Translation, tone, portrait, `_debug`, history IDs, subtitle backfill, Observer
  decisions, and playback behavior remain unchanged.

## Benchmark intent

This is intentionally small but not purely mechanical. It tests whether Spark can
trace a UI-to-storage call chain, use the existing `HistoryStore.append()` contract,
write a meaningful RED test, and avoid broadening scope.

The worker must not commit or push. A stronger coordinator will independently review
the diff and rerun verification before integration.

