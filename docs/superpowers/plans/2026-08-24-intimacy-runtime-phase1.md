# Sakura Intimacy Runtime Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stabilize Sakura intimacy-mode entry, safe exit, continuation pacing, transient context, persona retention, and TTS protocol without replacing the current private guide.

**Architecture:** Keep the existing hard-gated mode, but make `贴紧` and `苹果` exact paired control phrases, reduce silence continuation to three progressively delayed turns, and preserve persona by Markdown section rather than prefix truncation. Runtime mechanics stay outside the character card and private guide.

**Tech Stack:** Python 3.11+, PySide6/QTimer, pytest, existing AgentRuntime prompt recipes.

**Spec:** `docs/superpowers/specs/2026-08-24-intimacy-mode-layering-design.md`

## Global Constraints

- Use `.\.venv\Scripts\python.exe` for every test command.
- Do not modify or replace `data/intimacy_guide.txt` in this phase.
- Do not weaken explicit refusal or discomfort handling; it must exit before prompt/tone selection.
- `贴紧` and `苹果` trigger only as trimmed whole utterances with optional surrounding quotes and terminal punctuation.
- Agents do not push. Cursor may create one local commit containing only its declared files; Codex performs final integration.
- Preserve unrelated dirty files and ignored persona/distillation artifacts.

---

### Task 1: Paired control phrases and safe state budget (Cursor)

**Files:**
- Modify: `app/agent/builtin_tools.py:26-184`
- Test: `tests/unit/test_intimacy_mode.py`

**Interfaces:**
- Produces: `INTIMACY_ENTER_PHRASE = "贴紧"`, `INTIMACY_EXIT_PHRASE = "苹果"`.
- Produces: `user_requests_intimacy_exit(text: str) -> bool` for exact safe-word matching.
- Preserves: `user_declines_or_exits_intimacy(text: str) -> bool` as the combined safe-word/explicit-refusal gate.
- Produces: `IntimacyModeState._AUTO_EXIT_TURNS = 3` and `expire_after_silence() -> None`; the third continuation is generated while active, then UI expires the state after that reply completes.
- Produces: `build_intimacy_continue_message() -> dict[str, Any]` carrying `_sakura_transient_progress: True`.

- [ ] **Step 1: Add failing exact-match and refusal tests**

Add tests equivalent to:

```python
@pytest.mark.parametrize("text", ["苹果", "苹果。", "『苹果』"])
def test_safe_word_exits_as_whole_utterance(text: str) -> None:
    assert user_requests_intimacy_exit(text) is True

@pytest.mark.parametrize("text", ["我买了苹果", "苹果很好吃", "太好了", "准备好了", "好了吗"])
def test_safe_word_and_ambiguous_phrases_do_not_false_exit(text: str) -> None:
    assert user_requests_intimacy_exit(text) is False
    assert user_declines_or_exits_intimacy(text) is False

@pytest.mark.parametrize("text", ["停下", "不要继续", "我不舒服", "やめて"])
def test_explicit_refusal_exits_without_safe_word(text: str) -> None:
    assert user_declines_or_exits_intimacy(text) is True
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_intimacy_mode.py -q
```

Expected: failures because `INTIMACY_EXIT_PHRASE` and `user_requests_intimacy_exit` do not exist, `_AUTO_EXIT_TURNS` is still 8, and the continuation message lacks the transient marker.

- [ ] **Step 3: Implement the exact matcher and conservative refusal matcher**

Use one shared normalizer for both control phrases:

```python
INTIMACY_ENTER_PHRASE = "贴紧"
INTIMACY_EXIT_PHRASE = "苹果"
_INTIMACY_CONTROL_TRIM_RE = re.compile(
    r"^[\s　\"'“”‘’「」『』]+|[\s　\"'“”‘’「」『』！!。.?？…～~]+$"
)

def _normalize_intimacy_control_phrase(text: str) -> str:
    return _INTIMACY_CONTROL_TRIM_RE.sub("", str(text or "").strip()).strip()

def user_requests_intimacy_exit(text: str) -> bool:
    return _normalize_intimacy_control_phrase(text) == INTIMACY_EXIT_PHRASE
```

Keep a narrow explicit-refusal matcher for unambiguous phrases such as `停下`, `不要继续`, `别继续`, `结束吧`, `到此为止`, `我不舒服`, `やめて`, and `やめよう`. Do not retain substring exits for `好了`, `不行`, `待って`, or `もういい`.

Set `_AUTO_EXIT_TURNS = 3`. `consume_turn()` runs before response generation, so it must not deactivate on the third issued continuation. Add:

```python
def expire_after_silence(self) -> None:
    if not self.active:
        return
    self.active = False
    self.pending = False
    self._turns_left = 0
    self.opened_by_keyword = False
    self.needs_reentry_hint = True
```

The UI calls this only after the third continuation reply has completed. Tests must establish that the third continuation is rendered with intimacy mode active and expiration happens after that response, never before tone/prompt selection.

Use this pre-request behavior:

```python
def consume_turn(self) -> bool:
    if not self.active:
        return False
    if self._turns_left <= 0:
        self.expire_after_silence()
        return False
    self._turns_left -= 1
    self.opened_by_keyword = False
    return True
```

Change the continuation instruction from “推进下一步” to:

```text
根据当前姿势、呼吸和对方最后的反应自然回应；不要把沉默当成同意升级。
可以放缓、短暂确认或自然收束，不要仅换说法重复上一句。
```

Return the message with `"_sakura_transient_progress": True`.

- [ ] **Step 4: Run unit tests and verify GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_intimacy_mode.py -q
```

- [ ] **Step 5: Commit Cursor-owned files**

```powershell
git add app/agent/builtin_tools.py tests/unit/test_intimacy_mode.py
git commit -m "fix: add paired intimacy control phrases"
```

---

### Task 2: Three-step UI continuation and transient cleanup (Cursor)

**Files:**
- Modify: `app/ui/pet_window.py:3576-3653`
- Test: `tests/unit/test_intimacy_pet_window.py`
- Test: `tests/ui/test_pet_window.py`

**Interfaces:**
- Consumes: Task 1 `build_intimacy_continue_message()` and three-turn state budget.
- Produces: `PetWindow._INTIMACY_CONTINUE_DELAYS_MS = (20_000, 35_000, 60_000)`.
- Produces: `_next_intimacy_continue_delay_ms(self) -> int | None`.
- Consumes: Task 1 `IntimacyModeState.expire_after_silence()` after the final continuation response.
- Preserves: `_cancel_intimacy_continue()` and `_remove_transient_progress_messages()`.

- [ ] **Step 1: Replace source-string assertions with failing behavior tests**

Delete the assertion that searches for `_INTIMACY_CONTINUE_MAX = 8`. Add behavior tests asserting:

```python
assert window._next_intimacy_continue_delay_ms() == 20_000
window._intimacy_continue_count = 1
assert window._next_intimacy_continue_delay_ms() == 35_000
window._intimacy_continue_count = 2
assert window._next_intimacy_continue_delay_ms() == 60_000
window._intimacy_continue_count = 3
assert window._next_intimacy_continue_delay_ms() is None
```

Add a UI test that appends three intimacy continuation messages, exits, invokes the existing transient cleanup path, and asserts no message satisfies `message_is_intimacy_continue`.

- [ ] **Step 2: Run focused tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_intimacy_pet_window.py tests\ui\test_pet_window.py -q
```

Expected: missing delay tuple/helper and stale continuation messages.

- [ ] **Step 3: Implement progressive scheduling**

Replace the max/delay constants with:

```python
_INTIMACY_CONTINUE_DELAYS_MS = (20_000, 35_000, 60_000)

def _next_intimacy_continue_delay_ms(self) -> int | None:
    index = int(getattr(self, "_intimacy_continue_count", 0))
    if index < 0 or index >= len(self._INTIMACY_CONTINUE_DELAYS_MS):
        return None
    return self._INTIMACY_CONTINUE_DELAYS_MS[index]
```

Both `_schedule_intimacy_continue()` and `_on_intimacy_continue_timer()` must use this helper. `_schedule_intimacy_continue()` is invoked after reply playback; when it returns `None` after the third continuation, call `intimacy_mode_state.expire_after_silence()`, stop scheduling, and do not append another system signal.

When an exit action is observed, call `_cancel_intimacy_continue()` and `_remove_transient_progress_messages()` before the next request snapshot is built. Reuse the existing transient mechanism; do not add a second cleanup list.

- [ ] **Step 4: Run focused tests and verify GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_intimacy_pet_window.py tests\ui\test_pet_window.py -q
```

- [ ] **Step 5: Amend the Cursor commit**

```powershell
git add app/ui/pet_window.py tests/unit/test_intimacy_pet_window.py tests/ui/test_pet_window.py
git commit --amend --no-edit
```

---

### Task 3: Persona section retention and prompt/TTS contract (Codex)

**Files:**
- Modify: `app/llm/prompts/blocks.py:55-130`
- Modify: `app/agent/prompt_builder.py:28-168`
- Modify: the existing general reply-format block in `app/llm/prompts/blocks.py`
- Test: `tests/unit/test_intimacy_card_soften.py`
- Test: existing reply-format/TTS protocol tests found with `rg -n "suppress_tts|AGENT_REPLY_FORMAT" tests`

**Interfaces:**
- Consumes: Task 1 `INTIMACY_ENTER_PHRASE`, `INTIMACY_EXIT_PHRASE`.
- Produces: `_select_intimacy_persona_sections(markdown: str, max_chars: int) -> str`.
- Preserves: `soften_character_card_for_intimacy(system_prompt: str, *, max_persona_chars: int = 1400) -> str`.

- [ ] **Step 1: Add failing persona-retention tests**

Construct a Markdown card containing `## 核心`, `## 能动性与判断`, `## 关系中的她`, `## 日常质感`, and `## 不要写成`. Assert the softened output:

```python
assert "## 核心" in soft
assert "## 能动性与判断" in soft
assert "## 关系中的她" in soft
assert "## 不要写成" in soft
assert "退出权" in soft
assert "迟疑" in soft
assert "handoff" not in soft.lower()
assert not soft.rstrip().endswith("## 情绪怎样发生")
```

Also assert that a small `max_persona_chars` truncates at a paragraph boundary without dropping all boundary/OOC sections.

- [ ] **Step 2: Run focused tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_intimacy_card_soften.py tests\unit\test_prompt_templates.py -q
```

- [ ] **Step 3: Implement Markdown heading-aware selection**

Parse `## ` headings within the `【人格设定】` body. Prefer exact headings:

```python
_INTIMACY_PERSONA_KEEP_HEADINGS = (
    "核心",
    "能动性与判断",
    "关系中的她",
    "不要写成",
)
_INTIMACY_PERSONA_SAFETY_TERMS = (
    "意愿", "迟疑", "沉默", "退开", "退出权", "边界", "不要写成", "重复",
)
```

Keep exact sections first, then unknown sections containing safety terms if budget remains. Strip leading handoff metadata before the first Markdown heading. Never emit a heading without body. Use 1400 as the default budget, trimming paragraph bodies only after all required headings have at least one complete paragraph.

- [ ] **Step 4: Correct the entry prompt and declare `suppress_tts` globally**

Replace “不要再口头确认意愿” with:

```text
约定词已表达进入这一整体节奏的意愿，不必机械地再问一次相同的总体许可。
但沉默不代表同意升级；对方迟疑、退开、改变主意或不适时，立即放缓、暂停或确认。
安全词「苹果」或明确拒绝会由系统立即退出。
```

In the general segmented-reply contract, document `suppress_tts`:

```text
纯动作或环境描写 segment 可设 suppress_tts=true：只显示，不朗读；可听台词不要设置该字段。
```

Do not copy scene-specific examples into the global protocol.

- [ ] **Step 5: Run focused tests and verify GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_intimacy_card_soften.py tests\unit\test_prompt_templates.py -q
```

- [ ] **Step 6: Commit Codex-owned files**

```powershell
git add app/llm/prompts/blocks.py app/agent/prompt_builder.py tests/unit/test_intimacy_card_soften.py tests/unit/test_prompt_templates.py
git commit -m "refactor: preserve persona in intimacy mode"
```

---

### Task 4: Integration review and verification (Codex)

**Files:**
- Review all files changed by Tasks 1-3.
- Update: `docs/agent-handoffs/intimacy-guide-refresh/integration-notes.md` locally; keep it ignored/private unless the user later asks to track it.

**Interfaces:**
- Consumes all Task 1-3 behavior.
- Produces a clean working tree and evidence for the separate intimacy-distillation batch.

- [ ] **Step 1: Review actual diff and scope**

Run:

```powershell
git status --short --branch
git diff HEAD~2 -- app/agent/builtin_tools.py app/ui/pet_window.py app/llm/prompts/blocks.py app/agent/prompt_builder.py tests
git diff --check
```

Reject changes to `data/intimacy_guide.txt`, card files, unrelated UI, API configuration, or model settings.

- [ ] **Step 2: Run the complete intimacy gate**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_intimacy_mode.py tests\unit\test_intimacy_card_soften.py tests\unit\test_intimacy_pet_window.py tests\ui\test_pet_window.py -q
```

- [ ] **Step 3: Run full repository verification**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit tests\integration tests\ui -q
```

- [ ] **Step 4: Record evidence and leave push to the user/coordinator**

Record changed files, commit SHAs, targeted/full test counts, known behavioral risks, and the fact that the private guide was untouched. Do not push until explicitly authorized.

---

## Separate Follow-up: Intimacy Sakura Distillation

After Phase 1 passes real-use verification, start a separate design and plan for evidence-led persona distillation. It may amend the everyday card only when a trait is supported across contexts (for example independent judgment, private directness, relationship definition, care after vulnerability, or ordinary-future orientation). Scene-local arousal, vocabulary, pacing, or temporary possessiveness must remain in L1/L2 and must not become Sakura's daily default.
