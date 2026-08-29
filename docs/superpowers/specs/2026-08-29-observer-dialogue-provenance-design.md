# Observer Dialogue Provenance and Short-Lived Exchange Design

**Date:** 2026-08-29  
**Status:** Approved design, implementation not started  
**Scope:** ProactiveObserver screen speech and relationship initiative continuity

## Problem statement

Production history on 2026-08-29 showed Sakura repeatedly claiming that the user had not answered a question about lunch, even after the user answered and Sakura acknowledged the answer.

The failure was not a role/channel persistence bug:

- user, ordinary assistant, and proactive assistant rows were stored with the correct roles and channels;
- the running process included the recent chronology fix;
- the repeated speech came from the screen ProactiveObserver path, while relationship initiative B was gated as busy or silent.

The failure came from mixing four kinds of information without preserving their authority or lifetime:

1. current visual facts;
2. ordinary dialogue facts;
3. Observer hypotheses such as “he did not answer”;
4. Sakura's earlier proactive speech.

The VLM received prior Observer reason/comment records but not the complete recent dialogue. It therefore turned an old hypothesis into a new `reaction_hint`. The decision model then received that hint, a stale `_last_spoken_text`, and only the latest six folded dialogue turns. Once the actual answer fell outside those six turns, the old hypothesis became dominant. Even while an explicit correction remained in the six-turn window, the stale hint could still influence the generated comment.

## Goals

- Make dialogue history the only authority for whether the user replied.
- Keep visual perception, dialogue facts, and Observer hypotheses in separate context sections with explicit precedence.
- Preserve a small amount of short-lived continuity around Sakura's proactive questions after the exchange falls outside the normal recent-turn window.
- Distinguish “the user replied” from “the matter was settled” without keyword rules.
- Add no routine LLM/API request and no database schema migration.
- Reset ordinary open exchanges on application restart.
- Continue to use the existing commitment/memory mechanism for explicit agreements that deserve cross-restart continuity.

## Non-goals

- Determining a complete semantic topic graph for every conversation.
- Persisting ordinary conversational loose ends.
- Adding meal, sleep, intimacy, or other domain keyword handling.
- Replacing the long-term commitment or memory-curation systems.
- Guaranteeing that a model never improvises; this design controls fact provenance and repetition, not all creative generation.
- Increasing the fixed recent-history window as the primary solution.

## Considered approaches

### 1. Increase the recent-turn limit

Increasing six turns to twelve or twenty would reduce short incidents, but any fixed limit eventually loses the exchange. It also raises every decision request's token cost while leaving stale VLM hypotheses and `_last_spoken_text` intact. This is not sufficient.

### 2. Add a semantic classifier after every user turn

A classifier could label an exchange as accepted, rejected, deferred, ambiguous, or unrelated. It would be expressive but would add latency, token cost, model failure modes, and another state transition source to every ordinary turn. It is not justified before a deterministic provenance design has been measured.

### 3. Separate provenance and keep a short-lived exchange ledger

This is the selected approach. The ledger records that an exchange happened and captures its actual adjacent dialogue. The existing decision model interprets that evidence when needed. No extra semantic model call is required.

## Authority model

The Observer context uses this order:

1. **Dialogue facts:** persisted user and ordinary assistant history, including short-lived proactive exchange anchors.
2. **Current visible text:** text extracted from the current application and classified by scene/source.
3. **Current visual facts:** what the VLM can support from the current screenshot.
4. **Observer hypotheses:** optional, short-lived interpretations that may guide tone but cannot establish whether the user replied or whether an agreement exists.

When sources conflict, the higher source wins. A later dialogue row invalidates an older hypothesis about the dialogue state.

## Components

### Structured dialogue snapshot

`PetWindow` will expose one structured provider backed by `ChatHistoryStore` instead of making Observer logic depend on independently formatted strings and timestamps.

Each `ObserverHistoryLine` must include the persisted history `id` in addition to role, content, timestamp, and channel. IDs provide a strict ordering when multiple rows share the same second.

The snapshot supports:

- formatting the latest six folded ordinary turns;
- finding rows after a proactive exchange anchor;
- obtaining the latest ordinary dialogue fact ID/time;
- preserving current role, channel, time, and multi-segment folding behavior.

The provider remains read-only. No history rows are rewritten to maintain Observer state.

### Short-lived proactive exchange ledger

The Observer owns a thread-safe bounded deque of at most five `ProactiveExchange` anchors:

```python
@dataclass(frozen=True)
class ProactiveExchange:
    source: Literal["screen", "relationship"]
    history_start_id: int
    history_end_id: int
    spoken_at_unix: float
    text: str
```

The anchor is created only after proactive/relationship speech has actually been written to chat history. Cancelled generations, empty comments, decision failures, and display-time busy cancellation create no exchange.

At evaluation time, the exchange view is derived from authoritative history rows rather than persisted as a second copy:

```python
@dataclass(frozen=True)
class ProactiveExchangeView:
    exchange: ProactiveExchange
    state: Literal["awaiting_reply", "engaged", "expired"]
    first_user_reply: ObserverHistoryLine | None
    first_ordinary_assistant_followup: ObserverHistoryLine | None
```

State rules are deterministic and domain-independent:

- `awaiting_reply`: no user row exists after `history_end_id`;
- `engaged`: at least one user row exists after the proactive speech, regardless of whether its meaning is acceptance, rejection, deferral, ambiguity, or a topic change;
- `expired`: the exchange is older than 20 minutes, exceeds the five-entry bound, or its authoritative history cannot be recovered.

`engaged` means only “the user responded”. It must never be rendered as “agreed” or “settled”. The decision model sees the actual first reply and the first ordinary Sakura follow-up and can interpret their meaning. An ambiguous response may be clarified naturally, but the model may not claim that no response occurred.

If history recovery fails, fail closed for repetition: mark the exchange expired rather than reverting it to `awaiting_reply`.

### Decision context rendering

The decision request contains:

1. latest six folded dialogue turns;
2. up to three unexpired exchange views, newest first;
3. current visible text and visual summary;
4. optional current visual reaction;
5. relationship motive guidance when applicable.

An exchange view is clipped and rendered with explicit provenance, for example:

```text
[近期主动交流 · 已得到回应]
20:16 她主动说：明日の昼、一緒に食べるなら、そろそろ決めないと。
20:16 他说：听到了，会不会一起吃还是看情况吧……
20:17 她随后说：作るなら教えて、私の分も少し多めにお願い。
状态说明：他已经回应；原话是否表示确定、拒绝或暂缓，应按原文理解。
```

For `awaiting_reply`, the context may say that no later user history row exists. It must not infer intent from silence.

The old `_last_spoken_text` injection is removed after the ledger is active. It cannot accurately represent the immediately previous utterance once ordinary dialogue has continued.

### VLM context

The VLM is responsible only for current visual perception and a visual reaction. It no longer receives `_format_obs_history()` output containing previous reason/comment text.

The VLM prompt describes `reaction_hint` positively as a reaction grounded in the current visual observation. Dialogue response state is left to the later decision stage.

Existing visual duplicate detection remains based on screenshot/text hashes, window identity, and `_last_visual_summary`. Removing speech records from the VLM does not remove those deterministic duplicate controls.

### Observer hypotheses and sensory impression

`situational_summary` remains a hypothesis, not a dialogue fact. Its existing latest-ordinary-chat freshness check is retained.

The summary may help with current visual continuity while it is fresh. Once a newer user or ordinary assistant row exists, any dialogue-state claim in the summary is excluded from both VLM and decision contexts. It cannot reopen an engaged exchange.

The implementation should not introduce a second persistent summary database.

### Agreement provenance guidance

The host dialogue guidance will state a positive rule:

> Treat a domestic arrangement as a current proposal by default. Describe it as an existing agreement only when recent dialogue, long-term memory, or tool evidence shows explicit participation by both sides.

This rule is general and evidence-based. It does not identify meals or any other topic. It reduces the separate seed failure in which a model improvises a proposal and immediately describes it as an earlier agreement.

## Lifecycle and restart behavior

```text
proactive speech displayed
        |
        v
history rows written ----> exchange anchor created
        |
        +---- no later user row --------> awaiting_reply
        |
        +---- later user row -----------> engaged
                          |
                          +---- ordinary Sakura reply is attached as follow-up evidence

20-minute TTL / bound exceeded / history unavailable -> expired and dropped
application restart -> empty ledger
explicit durable agreement -> existing commitment/memory path, outside this ledger
```

The ledger is deliberately not reconstructed from old proactive history on startup. Reconstructing it would turn ordinary loose ends into persistent tasks and recreate the unwanted pressure.

## Failure handling

- VLM or decision JSON failure: no exchange is created unless speech is successfully displayed and recorded.
- Display cancellation or stale relationship generation: no exchange is created.
- Chat-history write failure: no anchor ID exists, so no exchange is created; log a content-free diagnostic.
- History read failure: omit dialogue-derived claims and expire affected anchors; never infer “unanswered”.
- Multiple proactive segments: one exchange spans the first through last persisted segment ID.
- User replies before all TTS playback completes: history ordering, not playback completion, determines engagement.
- A later user correction outranks any older Observer hypothesis by ID/time.

## Cost and performance

- Routine API request count: unchanged.
- VLM input: expected to decrease because prior speech reason/comment records are removed.
- Decision input: typically increases by 50–200 tokens when an exchange anchor is present; capped at three rendered views.
- Local work: bounded history reads and formatting only; no embedding or model inference.
- Memory: at most five small in-process anchors.
- Storage: no schema migration and no new persistent file.
- First-response latency: no material change expected; local history access must be measured in tests/logging.

## Privacy and logging

Production diagnostics record only counts, states, ages, source/channel, and history IDs. They must not log dialogue bodies, reaction hints, prompts, or private visual content.

Recommended diagnostics:

- number of active exchange anchors;
- counts by `awaiting_reply` / `engaged` / `expired`;
- whether VLM speech-history context was omitted;
- recent-history turn count and exchange-view count;
- local context-build elapsed milliseconds.

## Test strategy

### Deterministic state tests

- explicit user answer produces `engaged`;
- indirect answer produces `engaged` without being labeled settled;
- question, uncertainty, or “later” response produces `engaged` and preserves original text;
- no user row leaves `awaiting_reply`;
- a later change of mind remains visible through actual history evidence;
- the first ordinary Sakura follow-up is attached after the user's response;
- multi-segment proactive speech creates one anchor with an ID range;
- TTL, five-entry bound, missing history, and restart expire/drop anchors safely.

### Prompt-boundary tests

- VLM input does not contain prior Observer reason/comment or proactive dialogue;
- decision input includes the exchange anchor after the original exchange falls outside six recent turns;
- an engaged exchange is never rendered as unanswered;
- a newer ordinary dialogue row suppresses older sensory dialogue hypotheses;
- screen and relationship sources are labeled independently;
- `_last_spoken_text` is no longer injected after ledger activation.

### End-to-end regression fixtures

- reproduce the lunch timeline with many intervening sleep/intimacy turns;
- reproduce an actually unanswered proactive question;
- reproduce an ambiguous answer followed by a natural clarification;
- reproduce a correction (“不是已经定了吗”) followed by Sakura's acknowledgement;
- reproduce simultaneous screen trigger and relationship eligibility;
- reproduce malformed VLM/decision output and cancelled generation;
- verify no extra API request compared with the current two-stage screen evaluation.

## Delivery phases

### Phase 1: Context provenance and exchange ledger

- add structured history IDs/snapshot access;
- add bounded in-process exchange anchors and derived views;
- create anchors only after successful proactive history persistence;
- render exchange evidence in decision context;
- remove stale `_last_spoken_text` and speech-history input from the VLM;
- retain existing scheduling, cooldown, visual deduplication, and API topology.

This phase directly addresses the production root cause and is independently reversible.

### Phase 2: Agreement provenance guidance and measurement

- add the general evidence rule for describing existing agreements;
- add content-free state/cost diagnostics;
- run real conversations and compare proactive repetition rate, VLM/decision token usage, decision latency, and false suppression.

### Phase 3: Optional semantic resolver

Only consider an on-demand semantic resolver if Phase 1/2 measurements show that ambiguous responses are repeatedly misinterpreted despite the actual exchange evidence being present. It must not run on every turn. A future trigger may be limited to a decision that wants to revisit an `engaged` exchange, with failure falling back to silence rather than repetition.

Phase 3 is not approved for initial implementation.

## Acceptance criteria

- The reproduced production timeline cannot yield a context claiming “no reply” after a persisted user response.
- VLM context contains visual information, not previous dialogue-state conclusions.
- A genuinely unanswered proactive question remains representable.
- Ambiguous replies remain ambiguous rather than being forced into accepted/rejected labels.
- Ordinary exchange state disappears after restart and after its TTL.
- Explicit long-term commitments continue through the existing memory path.
- Screen Observer and relationship initiative retain current arbitration and channel behavior.
- No routine API request is added, and targeted cost inspection confirms bounded prompt growth.

