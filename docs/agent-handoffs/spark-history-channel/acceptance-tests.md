# Acceptance tests — proactive history channel

The implementation is acceptable only when all of these hold:

1. A proactive multi-segment assistant reply persists each non-empty segment with
   `channel == "proactive"`.
2. A relationship multi-segment assistant reply persists each non-empty segment with
   `channel == "relationship"`.
3. An ordinary assistant reply retains the existing empty/default channel behavior.
4. Segment order and history IDs are unchanged, including skipped empty segments.
5. `_debug` remains attached only where the existing code attaches it.
6. Translation, tone and portrait values survive unchanged.
7. Existing in-memory assistant messages still use `source` as before.
8. No Observer, prompt, API, TTS, persona, memory-policy, or configuration behavior
   changes.

Minimum verification:

```powershell
.\.venv\Scripts\python.exe -m pytest <focused-test-path> -q
.\.venv\Scripts\python.exe -m pytest tests/unit/test_relationship_initiative_playback.py tests/unit/test_proactive_reply_history_buttons.py -q
git diff --check
```

The coordinator decides whether a wider `tests/unit tests/ui` gate is required after
reviewing the actual diff.

