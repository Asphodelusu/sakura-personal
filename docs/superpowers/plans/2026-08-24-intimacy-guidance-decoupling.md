# Sakura Optional Intimacy Guidance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `intimacy_mode` an optional user-controlled guidance layer that never gates Sakura's ordinary affection, foreplay, or sexual behavior.

**Architecture:** Keep exact `贴紧`/`苹果`, guide injection, continuation timers, and transient cleanup. Remove inactive/reentry wording that treats mode as permission; relationship facts come from conversation and memory, while mode only selects detailed guidance, pacing, and continuation.

**Tech Stack:** Python 3.11+, PySide6/QTimer, pytest, existing AgentRuntime prompt recipes.

**Spec:** `docs/superpowers/specs/2026-08-24-intimacy-mode-layering-design.md`

## Global Constraints

- Use `.\.venv\Scripts\python.exe` for all Python commands.
- Do not modify `data/intimacy_guide.txt`.
- Only an exact user utterance `贴紧` enables mode; organic escalation never does.
- Inactive mode never means refusal, missing consent, relationship downgrade, or a behavioral limit.
- Current relationship facts come from recent conversation, core profile, and recalled memory.
- `苹果` and explicit refusal/discomfort still stop guidance or behavior immediately.
- Agents may commit declared files but never push.

---

### Task 1: Finish the existing Task 3 independent review (Cursor Grok 4.6 High Fast)

**Files:**
- Read: `docs/superpowers/plans/2026-08-24-intimacy-runtime-phase1.md`
- Read: `.superpowers/sdd/2026-08-24-intimacy-runtime-phase1/task-3-brief.md`
- Create locally ignored: `.superpowers/sdd/2026-08-24-intimacy-runtime-phase1/task-3-report.md`
- Create locally ignored: `.superpowers/sdd/2026-08-24-intimacy-runtime-phase1/task-3-review.md`

**Interfaces:**
- Consumes commit `2213a7f`.
- Produces a read-only severity-ranked review; reviewer changes no files.

- [ ] **Step 1: Record implementation evidence**

Write commit SHA, initial RED assertions, final `71 passed`, 1554-character real-card measurement, and retained `退出权`/`迟疑`/`重复` evidence to the report.

- [ ] **Step 2: Generate the bounded review package**

```powershell
& 'C:\Program Files\Git\bin\bash.exe' `
  '/c/Users/q1036/.codex/plugins/cache/claude-plugins-official/superpowers/6.3.0/skills/subagent-driven-development/scripts/review-package' `
  docs/superpowers/plans/2026-08-24-intimacy-runtime-phase1.md `
  03606bd 2213a7f
```

- [ ] **Step 3: Dispatch read-only review**

Use `cursor-grok-4.6-high-fast`. Require heading-parser, real-card retention, prompt-length, `suppress_tts`, and scope review. Forbid edits, commits, pushes, and repeated full tests.

- [ ] **Step 4: Gate findings**

Important findings receive one bounded fix wave and one scoped rereview. Record minor findings in the SDD ledger.

---

### Task 2: Decouple inactive mode from behavioral permission (Cursor Grok 4.6 High Fast)

**Files:**
- Modify: `app/agent/prompt_builder.py:28-39,102-179`
- Modify: `app/agent/builtin_tools.py:25-32,227-235,288-293`
- Test: `tests/unit/test_intimacy_mode.py:294-410`
- Test: `tests/unit/test_prompt_templates.py`

**Interfaces:**
- Preserves `user_requests_intimacy_entry(text: str) -> bool` as the only enable path.
- Preserves `_build_intimacy_section(...) -> PromptSection | None` and section IDs.
- Changes `_intimacy_entry_hint_text() -> str` into an optional-guidance notice.

- [ ] **Step 1: Add failing semantic tests**

```python
def test_inactive_mode_is_optional_guidance_not_behavior_gate() -> None:
    body = _intimacy_entry_hint_text()
    assert "不会自动开启" in body
    assert "未开启不限制" in body
    assert "不要动手描写" not in body
    assert "开启后才能" not in body
```

Add a reentry assertion that requires `引导与自动续投已关闭` and `仍按当前关系和意愿自然回应`, while rejecting `不要自行动手`. Assert the tool description says it controls guidance/pacing only.

- [ ] **Step 2: Run focused tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_intimacy_mode.py tests\unit\test_prompt_templates.py -q
```

Expected failures cite current phrases `未开启时不要动手描写`, `开启后才能`, or `不要自行动手`.

- [ ] **Step 3: Replace inactive/reentry contracts**

Use this semantic body:

```text
# 可选亲密导演层
只有对方整句发送「贴紧」时，系统才开启详细 guide、扩展节奏与自动续投；不会因对话自然升温而自动开启。
未开启不限制拥抱、亲吻、前戏或性行为。仍依据当前关系、记忆、双方当下意愿与具体语境自然回应。
不要猜测或调用 set_intimacy_mode(on=true)。需要结束已开启的导演层时才调用 on=false。
```

Reentry must say only guidance/continuation closed. Active keyword copy must say “请求启用详细 guide 与连续节奏”, not “总体许可”. Update `_SET_INTIMACY_MODE_DESCRIPTION` and parameter copy so `false` disables guidance/pacing/continuation, not bodily intimacy.

- [ ] **Step 4: Run focused tests and verify GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_intimacy_mode.py tests\unit\test_prompt_templates.py -q
```

- [ ] **Step 5: Commit declared files**

```powershell
git add app/agent/prompt_builder.py app/agent/builtin_tools.py tests/unit/test_intimacy_mode.py tests/unit/test_prompt_templates.py
git commit -m "fix: decouple intimacy guidance from consent"
```

---

### Task 3: Preserve relationship-memory authority (Codex)

**Files:**
- Modify: `characters/Sakura/system_guards.md:1-23`
- Test: `tests/unit/test_system_guards_prompt.py`
- Create: `tests/unit/test_intimacy_relationship_continuity.py`

**Interfaces:**
- Produces one evidence-priority guard without hardcoding “the user is a lover”.
- Changes no memory storage or retrieval interface.

- [ ] **Step 1: Add a failing guard test**

```python
def test_current_relationship_follows_accumulated_runtime_evidence() -> None:
    prompt = load_sakura_system_guards()
    assert "当前关系" in prompt
    assert "近期对话、常驻档案与长期记忆" in prompt
    assert "不能因原作、心情或一次迟疑而重置" in prompt
    assert "你们是恋人" not in prompt
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_system_guards_prompt.py tests\unit\test_intimacy_relationship_continuity.py -q
```

- [ ] **Step 3: Add the minimal guard**

```text
- 当前关系以真实积累的近期对话、常驻档案与长期记忆为准；原作经历塑造你，但不能覆盖现在。心情和一次具体迟疑可以改变当下节奏，不能无依据地把已经形成的关系重置为“重新认识”。
```

Do not state every imported user is a lover and do not prohibit natural references to 槐君.

- [ ] **Step 4: Add deterministic prompt replay**

Build a fixture with an established-lover core profile, a mood saying “he is testing my changes”, and one declined recent action. Assert the assembled guard retains relationship evidence priority and inactive mode contains no behavioral prohibition. Test prompt composition, not stochastic model output.

- [ ] **Step 5: Run tests and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_system_guards_prompt.py tests\unit\test_intimacy_relationship_continuity.py -q
git add characters/Sakura/system_guards.md tests/unit/test_system_guards_prompt.py tests/unit/test_intimacy_relationship_continuity.py
git commit -m "fix: preserve runtime relationship continuity"
```

---

### Task 4: Integration and real-prompt verification (Codex)

**Files:**
- Review all Task 1-3 files.
- Update locally ignored: `docs/agent-handoffs/intimacy-guide-refresh/integration-notes.md`

**Interfaces:**
- Produces fresh test and prompt-inspection evidence.

- [ ] **Step 1: Inspect scope and stale wording**

```powershell
git status --short --branch
git diff --check
rg -n "未开启时不要动手|开启后才能|不要自行动手|总体进入许可" app characters tests
```

Expected: no permission-gate wording remains.

- [ ] **Step 2: Run the intimacy regression gate**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_intimacy_mode.py tests\unit\test_intimacy_card_soften.py tests\unit\test_intimacy_pet_window.py tests\unit\test_prompt_templates.py tests\unit\test_system_guards_prompt.py tests\unit\test_intimacy_relationship_continuity.py tests\ui\test_pet_window.py -q
```

- [ ] **Step 3: Run the full gate**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit tests\integration tests\ui -q
```

- [ ] **Step 4: Inspect one fresh-process ordinary turn and one `贴紧` turn**

Ordinary turn must contain no `persona.intimacy` guide and no permission prohibition. `贴紧` turn must contain the detailed guide and continuation contract. Record section IDs/hashes without copying private full prompts.

- [ ] **Step 5: Record evidence; do not push**

Record commits, test counts, prompt evidence, stochastic-model risks, and that the private guide stayed unchanged.

