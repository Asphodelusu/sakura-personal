# Integration Notes — Observer Dialogue Provenance P1

## Cursor Desktop

Status: ready-for-codex-review

### Model / elapsed time

- 模型 / 模式：Cursor Desktop / Grok 4.6 High Fast
- 工作区：`D:\sakura`
- 耗时：约 18 分钟（含会话压缩后续作 Task 4–5）

### Starting and ending git state

- 起始：`dev5...origin/dev5 [ahead 5]`，HEAD `27fe2099 docs: plan observer dialogue provenance p1`，工作区干净
- 结束：仍在 `dev5`，HEAD 仍为 `27fe2099`，未 commit / 未 push；工作区仅本批 diff + 新测试文件

### Modified files

- `app/perception/observer.py`
- `app/storage/chat_history.py`
- `app/ui/pet_window.py`
- `tests/ui/test_pet_window.py`
- `tests/unit/test_chat_history_search.py`
- `tests/unit/test_observer_exchange_ledger.py`（新增）
- `tests/unit/test_observer_history_semantics.py`
- `tests/unit/test_observer_ledger.py`
- `tests/unit/test_observer_speech_prompts.py`
- `tests/unit/test_reply_history_channel_persistence.py`
- 本文件 Cursor Desktop 区块
- 未改：`app/perception/sensory_impression.py`、`characters/`、调度/仲裁/冷却、角色/长期记忆/API 拓扑、其他 handoff 批次

### RED evidence

均先写失败测试，再对当时生产实现跑最窄命令。

1. Task 1 存储 + 推导
   `tests/unit/test_chat_history_search.py` / 新 `tests/unit/test_observer_exchange_ledger.py`
   - `ChatHistoryStore.load_after_id` 不存在 → `AttributeError`
   - `ProactiveExchange` / `derive_proactive_exchange_view` 不存在 → 收集期 `ImportError`

2. Task 2 Observer 账本生命周期
   - `ProactiveObserver` 无 `set_history_entries_after_provider` / `_current_exchange_views` → `AttributeError`

3. Task 3 PetWindow 持久化回调
   - 历史写入后不调用 Observer 账本；`_observer_history_lines` 无持久化 `id`；无 `_observer_history_after_id`

4. Task 4 VLM / Decision 分层

   ```
   .\.venv\Scripts\python.exe -m pytest tests/unit/test_observer_speech_prompts.py tests/unit/test_observer_history_semantics.py tests/unit/test_observer_exchange_ledger.py -q --tb=line
   ```

   `3 failed, 21 passed`：
   - `test_vlm_user_content_omits_prior_observer_speech_history`：`ProactiveObserver` 无 `_build_vlm_user_content`
   - `test_format_engaged_exchange_never_claims_unanswered_or_settled` / `test_decision_context_keeps_exchange_after_six_unrelated_turns`：`cannot import name 'format_proactive_exchange_context'`

5. Task 5 无正文诊断

   ```
   .\.venv\Scripts\python.exe -m pytest tests/unit/test_observer_ledger.py tests/unit/test_observer_exchange_ledger.py -q --tb=line
   ```

   `2 failed, 33 passed`：构建决策上下文后 `ObserverLedger/交流上下文` 记录数为 0（`assert 0 == 1`）

### GREEN evidence

- Task 1：账本推导 + `load_after_id` → **20 passed**
- Task 2：账本生命周期（含 `tests/unit/test_relationship_timer.py`）→ **29 passed**
- Task 3：`tests/unit/test_reply_history_channel_persistence.py` + 整文件 `tests/ui/test_pet_window.py` → **177 passed**
- Task 4：

  ```
  .\.venv\Scripts\python.exe -m pytest tests/unit/test_observer_speech_prompts.py tests/unit/test_observer_history_semantics.py tests/unit/test_observer_exchange_ledger.py tests/unit/test_sensory_impression.py -q --tb=short
  ```

  **31 passed in 0.65s**
- Task 5 诊断：`tests/unit/test_observer_ledger.py tests/unit/test_observer_exchange_ledger.py` → **35 passed in 2.10s**
- 完整 P1 聚焦门禁：

  ```
  .\.venv\Scripts\python.exe -m pytest tests/unit/test_chat_history_search.py tests/unit/test_observer_exchange_ledger.py tests/unit/test_observer_history_semantics.py tests/unit/test_observer_speech_prompts.py tests/unit/test_sensory_impression.py tests/unit/test_observer_ledger.py tests/unit/test_relationship_timer.py tests/unit/test_reply_history_channel_persistence.py tests/ui/test_pet_window.py -q --tb=short
  ```

  **263 passed in 6.61s**。`git diff --check` 通过（仅既有 LF/CRLF 提示）。未跑完整 `tests/unit tests/ui`。

### State transitions and API-call evidence

- `ObserverHistoryLine.id` 来自持久化历史；同秒用 ID 排序。`load_after_id` 只读 `id > ?` 的 user/assistant，limit clamp 1–100。
- 进程内 `deque(maxlen=5)` 只存锚点。发言**全部 ID>0 且已写入历史**后，`PetWindow._record_assistant_reply_history` 才 `record_proactive_exchange`。`proactive` → source `screen`，`relationship` → `relationship`。取消/空/未展示/非正 ID 不建账。
- 视图由权威历史行推导：无后续 user → `awaiting_reply`；任意后续 user → `engaged`（不解读接受/拒绝）；TTL 1200s / 越界 / 历史不可用 → `expired`。provider 失败 fail-closed：清空锚点，不再冒出「没回答」。重启账本为空。
- VLM：`_build_vlm_user_content` 只给当前窗口/空闲/触发 + 新鲜印象，不再注入 `_format_obs_history()` 的 reason/comment。
- 决策：近 6 折叠轮 + 最多 3 条未过期交流视图（最新在前）。`engaged` 渲染为「已得到回应」并引用原文；不出现 unanswered / agreed / accepted / settled。不再注入 `_last_spoken_text` / `[自分の直前の発話]`。
- 更晚的 user/普通 assistant 事实仍使旧 `situational_summary` 失效；engaged 不能被更旧印象重开。
- 诊断：决策/关系上下文构建各记一条 `ObserverLedger`「交流上下文」（source / state counts / history IDs / age_s / view_count / elapsed_ms），无对白或画面正文。
- API 调用：未增加 LLM / VLM / embedding / web 常态调用。账本与诊断均为进程内。VLM 输入去掉观测发言史（应收窄）；决策最多增加 3 条截断视图。

### Scope deviations / remaining risks

- `_format_obs_history` 与 `_last_spoken_text` 赋值仍保留，但已退出 VLM/决策注入。关系决策同样改走交流视图。
- 未改 `sensory_impression.py`：沿用已有 freshness。
- Task 5 屏幕评估诊断用例最初用 `spoken_at_unix=1000` 会在 2026 立刻过期；测试改为 `time.time()`，生产 TTL 规则未放宽。
- 重启后账本为空（设计如此）：跨进程未回应的主动问不会重建。
- 历史读失败会丢掉全部锚点，避免重复追问，也可能丢掉仍有效的交流。
- `engaged` 不分类语义，决策模型仍可能误读原文。
- 未跑完整 `tests/unit tests/ui`，未做生产运行时验证。

## Codex integration review

Status: approved with three bounded integration corrections; ready for local commit

### Independent review

- Inspected the actual production/test diff and file scope; no role/persona, model/API, scheduling, long-term memory, TTS, subtitle, or schema changes were introduced.
- Confirmed the screen VLM no longer receives prior Observer reason/comment history, while the decision stage receives bounded exchange evidence derived from persisted history IDs.
- Confirmed `engaged` records only the existence of a later user row and does not promote it to accepted/agreed/settled.

### Codex RED → GREEN corrections

1. **High-density history window**
   - RED: a newer anchor after more than 100 rows was misclassified as `awaiting_reply` because all anchors shared one query starting after the oldest anchor.
   - Fix: derive each of the maximum five anchors from its own bounded post-anchor query; a saturated 100-row window with no user evidence expires fail-closed.
2. **Concurrent anchor preservation**
   - RED: pruning could remove an anchor added after the evaluation snapshot while history was being read.
   - Fix: prune only anchors that were actually part of the evaluated snapshot; preserve newly added anchors.
3. **Complete ordinary follow-up and read-failure propagation**
   - RED: only the first segment of a same-turn ordinary Sakura reply was attached, and `PetWindow` converted `OSError` into `[]`, which looked like “no reply”.
   - Fix: fold the first ordinary assistant turn with the existing multi-segment rule; preserve `OSError` so Observer clears the affected ledger instead of inferring silence.

### Verification

- New correction REDs were observed individually before production edits.
- P1 focused gate: **267 passed in 7.48s**.
- Full gate: `D:\sakura\.venv\Scripts\python.exe -m pytest tests/unit tests/ui -q` → **1876 passed in 42.04s**.
- `git diff --check`: passed; only working-tree LF/CRLF conversion warnings were emitted.

### Remaining runtime measurement

- Restart Sakura before production testing so the new in-process ledger is active.
- Observe whether ambiguous answers are interpreted naturally and whether proactive repetition drops; Phase 2 semantic resolution remains intentionally unimplemented.
