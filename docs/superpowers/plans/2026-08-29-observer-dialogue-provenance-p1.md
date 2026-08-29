# Observer Dialogue Provenance P1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent ProactiveObserver from treating its stale “the user did not answer” hypothesis as dialogue fact after the user has responded, without adding routine model calls.

**Architecture:** Persisted chat history remains the dialogue authority. A bounded in-process ledger stores only proactive speech anchors; its current `awaiting_reply` or `engaged` view is derived from history IDs and actual adjacent user/assistant rows. VLM input becomes visual-only, while the decision model receives recent dialogue plus clipped exchange evidence.

**Tech Stack:** Python 3.11+, dataclasses, deque/threading, SQLite history store, PySide6 PetWindow integration, pytest.

**Spec:** `docs/superpowers/specs/2026-08-29-observer-dialogue-provenance-design.md`

## Global Constraints

- Use `D:\sakura\.venv\Scripts\python.exe` for every Python/pytest command.
- RED must precede production changes for each behavior.
- Do not add meal/sleep/intimacy or other topic keywords.
- Do not add a routine LLM, embedding, or API request.
- Do not change database schema or rewrite production history/logs.
- Ordinary exchange state is in-process only, TTL 1200 seconds, maximum five anchors, maximum three rendered views.
- `engaged` means only that a user row exists after the proactive speech; it never means accepted or settled.
- If history evidence is unavailable, omit/expire the exchange rather than infer that it is unanswered.
- Preserve current Observer scheduling, screen/relationship arbitration, cooldown/backoff, model choices, and visual deduplication.
- Phase 2 agreement-provenance guidance and Phase 3 semantic resolution are out of scope for this plan.

---

### Task 1: Add ID-ordered history access and pure exchange-state derivation

**Files:**
- Modify: `app/storage/chat_history.py`
- Modify: `app/perception/observer.py`
- Modify: `tests/unit/test_chat_history_search.py`
- Create: `tests/unit/test_observer_exchange_ledger.py`

**Interfaces:**
- Produces: `ChatHistoryStore.load_after_id(entry_id: int, *, limit: int = 100) -> list[ChatHistoryEntry]`.
- Produces: `ObserverHistoryLine.id: int` with default `0` for compatibility.
- Produces: immutable `ProactiveExchange` and `ProactiveExchangeView` dataclasses.
- Produces: `derive_proactive_exchange_view(exchange, entries, *, now_unix, ttl_seconds=1200.0) -> ProactiveExchangeView`.

- [ ] **Step 1: Write the failing storage tests**

Add tests that append user/assistant rows, call `load_after_id`, and assert strict ascending ID order, role/channel preservation, limit enforcement, and empty output after the newest ID. Use only a temporary database.

```python
rows = store.load_after_id(first_id, limit=2)
assert [row.id for row in rows] == [second_id, third_id]
assert rows[0].role == "user"
```

- [ ] **Step 2: Run the storage RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_chat_history_search.py -q --tb=short
```

Expected: FAIL because `load_after_id` does not exist.

- [ ] **Step 3: Implement the bounded read-only query**

Use parameterized SQLite and `_row_to_entry`; reject non-positive IDs by returning an empty list and clamp the limit to a small safe range consistent with existing history APIs.

```python
SELECT id, created_at, role, content, translation, tone, portrait, channel
FROM chat_history
WHERE id > ? AND role IN ('user', 'assistant')
ORDER BY id ASC
LIMIT ?
```

- [ ] **Step 4: Write failing pure ledger tests**

Cover these exact states without a model:

```python
exchange = ProactiveExchange(
    source="screen",
    history_start_id=10,
    history_end_id=12,
    spoken_at_unix=1000.0,
    text="明日の昼、どうする？",
)
```

- no later user row -> `awaiting_reply`;
- any later user row, including a question or unrelated-looking text -> `engaged`;
- first ordinary assistant row after that reply is attached as follow-up;
- proactive/relationship assistant rows do not count as ordinary follow-up;
- TTL expiry -> `expired`;
- reply text is preserved and is not classified as accepted/rejected.

- [ ] **Step 5: Run the ledger RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_observer_exchange_ledger.py -q --tb=short
```

Expected: FAIL because the dataclasses and derivation function do not exist.

- [ ] **Step 6: Implement the minimum pure dataclasses and derivation**

Keep state derivation independent from `ProactiveObserver` networking and Qt. Filter only rows whose `id > history_end_id`; use the first user row and then first ordinary assistant row after it. Do not inspect topic keywords.

- [ ] **Step 7: Run Task 1 GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_chat_history_search.py tests/unit/test_observer_exchange_ledger.py -q --tb=short
```

Expected: PASS.

### Task 2: Add the bounded in-process ledger to ProactiveObserver

**Files:**
- Modify: `app/perception/observer.py`
- Modify: `tests/unit/test_observer_exchange_ledger.py`
- Modify: `tests/unit/test_relationship_timer.py`

**Interfaces:**
- Consumes: Task 1 dataclasses and derivation.
- Produces: `set_history_entries_after_provider(provider: Callable[[int, int], list[ObserverHistoryLine]]) -> None`.
- Produces: `record_proactive_exchange(*, source: str, history_ids: list[int], text: str, spoken_at_unix: float | None = None) -> bool`.
- Produces: `_current_exchange_views(now_unix: float | None = None) -> list[ProactiveExchangeView]`.

- [ ] **Step 1: Write failing observer-ledger lifecycle tests**

Assert:

- valid persisted IDs create one anchor;
- empty/zero/partially failed IDs create no anchor;
- multiple segments create one start/end range;
- screen and relationship sources remain distinct;
- only five newest anchors remain;
- provider failure returns no current views and drops/invalidates affected anchors rather than producing `awaiting_reply`;
- a new `ProactiveObserver` starts with an empty ledger.

- [ ] **Step 2: Run the lifecycle RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_observer_exchange_ledger.py tests/unit/test_relationship_timer.py -q --tb=short
```

Expected: FAIL on missing observer methods/state.

- [ ] **Step 3: Implement thread-safe bounded anchors**

Use a lock around a `deque(maxlen=5)`. Store no reply classification and no persistent file. Query rows after the oldest active anchor once per context build where practical, then derive all views from that bounded result.

- [ ] **Step 4: Preserve cancellation and arbitration behavior**

Do not create anchors inside `_do_evaluation()` or `_do_relationship_evaluation()` merely because a model returned `should_speak`. Anchors are created only by the post-history callback added in Task 3. Existing generation cancellation and busy gates therefore remain authoritative.

- [ ] **Step 5: Run Task 2 GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_observer_exchange_ledger.py tests/unit/test_relationship_timer.py -q --tb=short
```

Expected: PASS with existing relationship arbitration tests unchanged.

### Task 3: Connect persisted proactive history IDs from PetWindow

**Files:**
- Modify: `app/ui/pet_window.py`
- Modify: `tests/ui/test_pet_window.py`
- Modify: `tests/unit/test_reply_history_channel_persistence.py`

**Interfaces:**
- Consumes: `record_proactive_exchange(...)` and `ChatHistoryStore.load_after_id(...)`.
- Produces: PetWindow providers that convert `ChatHistoryEntry` to `ObserverHistoryLine` including `id`.
- Preserves: `_record_assistant_reply_history(...) -> list[int]`.

- [ ] **Step 1: Write the failing persistence-boundary tests**

Use explicit test doubles, not permissive mocks. Verify:

- proactive and relationship segments are written with existing channels;
- after all segment IDs are available, exactly one observer anchor callback receives the source, full positive ID list, and joined text;
- ordinary main-chat replies do not create proactive anchors;
- any failed/zero history ID suppresses anchor creation;
- missing observer during startup/shutdown remains harmless.

- [ ] **Step 2: Run the PetWindow RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_reply_history_channel_persistence.py tests/ui/test_pet_window.py -q --tb=short
```

Expected: FAIL because persisted proactive IDs are not forwarded to Observer.

- [ ] **Step 3: Implement the post-persistence callback**

Keep history writing as the authority. Notify the Observer only after `_record_assistant_reply_history` has all IDs. Do not alter subtitle/TTS order or message-source mapping.

- [ ] **Step 4: Wire ID-aware history providers**

Populate `ObserverHistoryLine.id` from `ChatHistoryEntry.id`. The provider used by exchange views must call the bounded `load_after_id` API and handle `OSError` without touching production data.

- [ ] **Step 5: Run Task 3 GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_reply_history_channel_persistence.py tests/ui/test_pet_window.py -q --tb=short
```

Expected: PASS.

### Task 4: Enforce VLM/decision provenance boundaries

**Files:**
- Modify: `app/perception/observer.py`
- Modify: `tests/unit/test_observer_speech_prompts.py`
- Modify: `tests/unit/test_observer_history_semantics.py`
- Modify: `tests/unit/test_observer_exchange_ledger.py`

**Interfaces:**
- Consumes: `_current_exchange_views()`.
- Produces: `format_proactive_exchange_context(views, *, max_views=3) -> str`.
- Removes from active decision flow: `_last_spoken_text` as a dialogue-state source.

- [ ] **Step 1: Write the VLM-boundary RED**

Construct an Observer with prior speech reason/comment and assert the Stage 1 VLM user message contains current visual metadata but does not contain prior proactive comment, prior decision reason, “user did not answer”, or `[最近の観測履歴]` speech text.

- [ ] **Step 2: Write the decision-anchor RED**

Reproduce the production shape:

1. proactive question is persisted;
2. user gives an indirect answer;
3. ordinary Sakura acknowledges it;
4. more than six unrelated turns follow;
5. decision context is built.

Assert the ordinary six-turn block may omit the old exchange, while `[近期主动交流 · 已得到回应]` still includes the proactive text, actual user reply, and ordinary follow-up. Assert the rendered state never says unanswered, agreed, accepted, or settled.

- [ ] **Step 3: Run the prompt RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_observer_speech_prompts.py tests/unit/test_observer_history_semantics.py tests/unit/test_observer_exchange_ledger.py -q --tb=short
```

Expected: FAIL because VLM still receives observation speech history and decision lacks exchange views.

- [ ] **Step 4: Implement clipped exchange rendering**

Render at most three newest unexpired views. Clip each field to a bounded length. Use positive provenance wording:

- `awaiting_reply`: no later persisted user row exists;
- `engaged`: a later user row exists; interpret certainty from the quoted original text;
- never promote `engaged` to an agreement.

- [ ] **Step 5: Separate Stage 1 and Stage 2 contexts**

Remove `_format_obs_history()` speech reason/comment from VLM input. Keep current deterministic screen/hash/window dedup. Feed exchange context only to the decision stage after recent dialogue. Stop injecting `_last_spoken_text` once exchange views are active.

- [ ] **Step 6: Retain sensory-impression freshness behavior**

Keep the current rule that a newer user/ordinary assistant fact invalidates an older dialogue-bearing impression. Add a regression proving an engaged exchange cannot be reopened by an older `situational_summary`.

- [ ] **Step 7: Run Task 4 GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_observer_speech_prompts.py tests/unit/test_observer_history_semantics.py tests/unit/test_observer_exchange_ledger.py tests/unit/test_sensory_impression.py -q --tb=short
```

Expected: PASS.

### Task 5: Add content-free diagnostics and integrated regression gate

**Files:**
- Modify: `app/perception/observer.py`
- Modify: `tests/unit/test_observer_ledger.py`
- Modify: `tests/unit/test_observer_exchange_ledger.py`
- Modify: `docs/agent-handoffs/observer-dialogue-provenance-p1/integration-notes.md` (Cursor section only)

**Interfaces:**
- Produces content-free diagnostics for exchange counts/states and context-build elapsed time.
- Preserves existing `ObserverLedger` privacy contract.

- [ ] **Step 1: Write the diagnostic privacy RED**

Assert logs may contain source, state counts, history IDs, age, view count, and elapsed milliseconds, but never proactive text, user reply, assistant follow-up, reaction hint, prompt, or visual body.

- [ ] **Step 2: Run the diagnostic RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_observer_ledger.py tests/unit/test_observer_exchange_ledger.py -q --tb=short
```

Expected: FAIL until bounded diagnostics exist.

- [ ] **Step 3: Implement minimum diagnostics**

Add one context-build record rather than per-tick noise. Reuse the existing privacy discipline in `ObserverLedger`.

- [ ] **Step 4: Run the complete P1 focused gate**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_chat_history_search.py tests/unit/test_observer_exchange_ledger.py tests/unit/test_observer_history_semantics.py tests/unit/test_observer_speech_prompts.py tests/unit/test_sensory_impression.py tests/unit/test_observer_ledger.py tests/unit/test_relationship_timer.py tests/unit/test_reply_history_channel_persistence.py tests/ui/test_pet_window.py -q --tb=short
```

Expected: PASS with zero real API/TTS calls.

- [ ] **Step 5: Check formatting and scope**

```powershell
git diff --check
git status --short
```

Confirm only task-contract files changed. Do not run the full `tests/unit tests/ui` gate; the coordinator will run it once after independent diff review.

- [ ] **Step 6: Fill the Cursor integration notes and stop**

Record actual model, elapsed time, files, each RED failure, GREEN totals, key state transitions, scope deviations, and remaining risks. Do not commit and do not push.
