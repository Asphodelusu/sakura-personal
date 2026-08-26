# Observer Plain Dialogue Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Conservatively recover short natural Japanese dialogue when the Observer decision model returns dialogue instead of the required JSON object.

**Architecture:** Add one pure parser/classifier beside the existing Observer JSON extraction helpers. `_post_speech_decision` first keeps the current JSON path; only a parse failure may enter the conservative dialogue fallback, returning the same decision dictionary shape used by screen and relationship initiative paths. Log one structured outcome without adding retries or model calls.

**Tech Stack:** Python, pytest, existing `app/perception/observer.py` logging.

**Spec:** `docs/agent-handoffs/observer-decision-fallback-p2/README.md`

## Global Constraints

- Do not edit prompts, persona files, request parameters, translation behavior, P1 scheduling, or cooldown rules.
- Do not add a retry or another model call.
- Accept only short, natural Japanese dialogue; reject explanations, Markdown, JSON fragments, report/system language, and excessive length.
- Use `D:\sakura\.venv\Scripts\python.exe` for tests.
- Do not push.

---

### Task 1: Conservative decision-output fallback

**Files:**
- Modify: `app/perception/observer.py`
- Test: `tests/unit/test_proactive_focus.py` or a new focused Observer parser test file

**Interfaces:**
- Consumes: raw `content` returned by the speech-decision model after `_extract_json(content)` fails.
- Produces: either `None` or a normal decision dict with `should_speak=True`, adopted `comment`, empty `translation`, neutral `tone`, and a fallback reason.

- [ ] **Step 1: Write failing tests** covering adoption of one/two-sentence natural Japanese dialogue and rejection of Chinese/English explanations, Markdown, JSON fragments, system/report prose, blank content, and overly long text.
- [ ] **Step 2: Run the focused tests and record the expected RED failures.**
- [ ] **Step 3: Implement the minimal pure classifier and call it only after JSON extraction fails.**
- [ ] **Step 4: Emit structured outcome values `valid_json`, `adopted_plain_dialogue`, or `rejected_invalid_output` through existing logging, without leaking full private dialogue.**
- [ ] **Step 5: Run focused Observer/relationship tests and then `tests/unit tests/ui`; run `git diff --check`.**
- [ ] **Step 6: Fill the Cursor section of `integration-notes.md`; do not commit or push.**

