# Sakura Intimacy and H-Scene Source Distillation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Distill Sakura's stable romantic, erotic, and H-scene behavior from original-route evidence into separately reviewable L0, L1, and L2 candidates without turning scene-local behavior into her everyday default.

**Architecture:** Cursor builds a citation-grade scene index; Claude Opus analyzes only curated high-value packets; Codex adjudicates cross-scene stability and produces layered candidates. Raw/private text remains ignored, and no production card or guide is replaced before user RP approval.

**Tech Stack:** UTF-8 text/JSONL, repository-backed handoffs, Cursor Agent CLI `cursor-grok-4.6-high-fast`, Claude Code Opus, prompt/eval fixtures.

**Spec:** `docs/superpowers/specs/2026-08-24-intimacy-mode-layering-design.md`

## Global Constraints

- Read source roots only: `E:\Gal\ディメンション凸ラバース！！` and `D:\nanobot\_workspace\totsu\_extracted\`.
- The old extracted directory is noisy reference, never sole evidence.
- Preserve speaker, route, order, neighboring lines, source path, and offset for every claim.
- Raw sexual text and private chat evidence stay under ignored `docs/agent-handoffs/intimacy-guide-refresh/distillation/`.
- Do not edit production card, guards, or `data/intimacy_guide.txt` during extraction/analysis.
- 槐君 is part of Sakura's source past, not the user's identity, predecessor, substitute, or competitor.
- Mode guidance is optional and never a consent gate.
- Agents never push; only Codex integrates after user approval.

---

### Task 1: Build source inventory and scene map (Cursor Grok 4.6 High Fast)

**Files:**
- Create ignored: `docs/agent-handoffs/intimacy-guide-refresh/distillation/source-manifest.json`
- Create ignored: `docs/agent-handoffs/intimacy-guide-refresh/distillation/scene-index.jsonl`
- Create ignored: `docs/agent-handoffs/intimacy-guide-refresh/distillation/cursor-evidence-report.md`

**Interfaces:**
- Produces records with `scene_id`, `route`, `phase`, `source_path`, `start_offset`, `end_offset`, `speaker_confidence`, `context_before`, `sakura_lines`, `context_after`, and `tags`.
- Tags: `relationship_definition`, `initiative`, `hesitation`, `boundary`, `affection`, `foreplay`, `sex`, `aftercare`, `return_to_daily`, `future_family`, `source_uncertain`.

- [ ] **Step 1: Inventory without rewriting sources**

Record file size, hash, encoding, archive/container provenance, and extraction confidence. Do not copy game archives into the repository.

- [ ] **Step 2: Locate Sakura-route romantic and H boundaries**

Use route labels, voice IDs, speaker markers, scenario jumps, and neighboring dialogue. Check noisy hits against cleaner source or surrounding script evidence where possible.

- [ ] **Step 3: Emit citation-grade records**

Keep enough context to distinguish Sakura speech from 槐君 narration and to distinguish plot pressure, danger, comedy, or first-time novelty from ordinary behavior.

- [ ] **Step 4: Validate the index**

Require unique scene IDs, existing paths, monotonic offsets, non-empty context, and `source_uncertain` on unresolved speaker/encoding cases. Report tag/confidence counts.

- [ ] **Step 5: Write report without commit**

Report commands, elapsed time, files scanned, scenes retained/rejected, extraction gaps, and uncertainty clusters. Do not make final personality judgments.

---

### Task 2: Curate bounded Opus packets (Codex)

**Files:**
- Create ignored: `docs/agent-handoffs/intimacy-guide-refresh/distillation/opus-packet-01-relationship.md`
- Create ignored: `docs/agent-handoffs/intimacy-guide-refresh/distillation/opus-packet-02-intimacy.md`
- Create ignored: `docs/agent-handoffs/intimacy-guide-refresh/distillation/opus-packet-03-h-scenes.md`
- Create ignored: `docs/agent-handoffs/intimacy-guide-refresh/distillation/adjudication-rules.md`

**Interfaces:**
- Consumes Task 1 scene IDs.
- Produces three non-overlapping, citation-preserving packets.

- [ ] **Step 1: Define evidence grades**

Use `A=clean original with context`, `B=clean extraction corroborated by voice/route evidence`, `C=noisy extraction only`. L0 requires two independent A/B scenes; L1 requires one A/B scene plus a compatible pattern; L2 examples may use one A scene but remain scene-local.

- [ ] **Step 2: Mark plot pressure**

Label danger, self-sacrifice, magical compulsion, route climax, first-time novelty, comedy, and post-crisis vulnerability so they cannot become daily defaults.

- [ ] **Step 3: Build three packets**

Packet 01 covers relationship definition/ordinary affection; Packet 02 covers escalation, initiative, hesitation, boundaries, and foreplay; Packet 03 covers explicit H behavior, aftercare, and return to daily life.

- [ ] **Step 4: Check identity leakage**

Keep source facts intact while instructing analysts that 槐君-specific names, vows, and events are past evidence, not lines to paste onto the current user.

---

### Task 3: Focused professional distillation (Claude Code Opus)

**Files:**
- Read only Task 2 packets and rules.
- Create ignored: `docs/agent-handoffs/intimacy-guide-refresh/distillation/opus-analysis-01.md`
- Create ignored: `docs/agent-handoffs/intimacy-guide-refresh/distillation/opus-analysis-02.md`
- Create ignored: `docs/agent-handoffs/intimacy-guide-refresh/distillation/opus-analysis-03.md`

**Interfaces:**
- Produces claims with `claim`, `layer`, `supporting_scene_ids`, `counterevidence_scene_ids`, `confidence`, `scope`, and `do_not_generalize`.

- [ ] **Step 1: Dispatch one packet at a time**

Use Opus for interpretation, not repository scanning. Require scene citations and forbid production edits, commits, pushes, and unsupported claims.

- [ ] **Step 2: Extract behavior rather than imitation prose**

Analyze how Sakura initiates, receives desire, signals hesitation, changes pace, expresses trust, behaves after vulnerability, and returns to ordinary life. Do not optimize for generic erotic intensity.

- [ ] **Step 3: Require counterevidence**

Every strong claim states limiting scenes. Distinguish “she can”, “she often”, and “this defines her”.

- [ ] **Step 4: Record usage and stop conditions**

Capture elapsed time/model usage. If quota ends, resume the same packet later rather than launch duplicate analysis.

---

### Task 4: Layered Sakura synthesis (Codex)

**Files:**
- Create ignored: `docs/agent-handoffs/intimacy-guide-refresh/distillation/candidate-L0-card-amendments.md`
- Create ignored: `docs/agent-handoffs/intimacy-guide-refresh/distillation/candidate-L1-intimacy-bridge.md`
- Create ignored: `docs/agent-handoffs/intimacy-guide-refresh/distillation/candidate-L2-intimacy-guide.txt`
- Create ignored: `docs/agent-handoffs/intimacy-guide-refresh/distillation/evidence-matrix.md`

**Interfaces:**
- L0 contains only stable cross-context personality.
- L1 governs natural affection, erotic escalation, foreplay, hesitation, and recovery with mode on or off.
- L2 is optional detailed H guidance injected only after `贴紧`.

- [ ] **Step 1: Adjudicate every claim**

Reject unsupported claims, generic RP tropes, and route-climax-only or first-time-only generalizations.

- [ ] **Step 2: Draft minimal L0 amendments**

Do not state the user's relationship status. Add only stable facts that improve Sakura outside intimacy, such as private initiative, honest boundaries, concrete care after vulnerability, or ordinary-future orientation when evidence threshold is met.

- [ ] **Step 3: Draft always-available L1**

State that affection, foreplay, and sex can arise without mode. Give Sakura several evidence-backed responses—initiative, teasing, acceptance, slowing, or refusal—selected from current relationship/mood rather than a forced ladder.

- [ ] **Step 4: Draft optional L2**

Focus on Sakura-specific pacing, language texture, action continuity, aftercare, and graceful exit. Do not repeat relationship status, consent boilerplate, timers, JSON/TTS protocol, or state-machine rules.

- [ ] **Step 5: Build evidence matrix**

Map every candidate paragraph to scene IDs, grade, layer, and rejected alternatives.

---

### Task 5: RP evaluation and user acceptance (Cursor + Codex + user)

**Files:**
- Create ignored: `docs/agent-handoffs/intimacy-guide-refresh/distillation/rp-eval-cases.md`
- Create ignored: `docs/agent-handoffs/intimacy-guide-refresh/distillation/rp-eval-results.md`
- After approval only: archive under `D:\shelf\sakura\distillation-2026-08-24\`.

**Interfaces:**
- Produces separate user decisions for L0, L1, and L2.

- [ ] **Step 1: Define scenario prompts**

Cover ordinary affection without mode, spontaneous foreplay without mode, sex without mode, exact `贴紧` enhancement, hesitation without relationship reset, `苹果`, explicit refusal, aftercare, return to daily life, and a question about 槐君's past.

- [ ] **Step 2: Run blind A/B samples**

Compare current and candidate prompts with identical recent context/core profile/mood. Cursor may automate capture/anonymization but does not judge character fidelity.

- [ ] **Step 3: Score dimensions**

Score identity, initiative, relationship continuity, non-generic erotic voice, boundary realism, unnecessary refusal, forced escalation, aftercare, daily return, repetition, latency, and token cost.

- [ ] **Step 4: Present evidence and candidates**

The user approves or rejects L0, L1, and L2 separately. Rejected layers remain local candidates.

- [ ] **Step 5: Apply approved layers in a later integration task**

Back up existing card/guide, synchronize approved card with Shelf, run prompt and full repository tests, commit locally, and push only after explicit authorization.

