# Integration notes

## Cursor

- status: 元叙述假阳性已最小收紧（未 commit、未 push）。本会话 Shell 仍被拒绝，未能实跑 pytest / `git diff --check`。
- modified files:
  - `app/perception/observer.py`
  - `tests/unit/test_observer_plain_dialogue_fallback.py`
  - 本文件 Cursor 段
- RED evidence:
  - 生产代码改动前，`observer.py` 不存在 `_adopt_plain_dialogue_decision`；测试模块 import 会 `ImportError`。
  - `_post_speech_decision` 在 `_extract_json(content)` 失败后直接 `return None`，并 `logger.warning` 打印 `content[:200]`。
  - focused collection 失败：`re.error: global flags not at the start`，出在 `_PLAIN_DIALOGUE_MARKDOWN_RE` 的中部 `(?m)`。
  - 审查假阳性：`_adopt_plain_dialogue_decision('彼が動画を見ているので、話しかけることにします。')` 在补标记前会被当成 1 句假名对白采纳。本次先加 `test_rejects_japanese_meta_decision_narration`；Shell 拒绝，未拿到 pytest 失败输出。静态上该句不含旧标记、有假名、≤80 字、1 句，会走采纳分支。
- GREEN / 本次修复:
  - Markdown 检测为 `re.compile(..., re.MULTILINE)`。
  - 仅 JSON 失败后走纯分类器；日志只带 outcome / finish / chars。
  - 元叙述拒绝改为短标记，不误伤直接对白：`ことにします` / `ことにしました`、`発言します` / `発言すること` / `発言すべき` / `すべきです`、`判断しました` / `判断します`。
  - 保留：`今話しかけてもいい？`、`今は黙って見るね。`、`発言するな。`、`見るべき？`、既有 1～2 句对白。
- focused tests: 未跑（Shell 拒绝）。合同命令：`.\.venv\Scripts\python.exe -m pytest tests/unit/test_observer_plain_dialogue_fallback.py tests/unit/test_observer_speech_prompts.py tests/unit/test_relationship_timer.py tests/unit/test_proactive_focus.py -q`
- full gate: 未跑。合同命令：`.\.venv\Scripts\python.exe -m pytest tests/unit tests/ui -q`
- `git diff --check`: 未跑（Shell 拒绝）。
- classifier adjacent review:
  - 假阴性（保守丢弃）：无假名纯汉字；≥3 字母拉丁混入；`評価`/`システム`/`报告`；决策套话 `ことにします` 等；`{}` / `"key":`；行首 Markdown；>80 字或 >2 句。
  - 未用光杆 `発言する`（会误伤 `発言するな。`）；未用光杆 `すべき`（`見るべき？` 不含该串，但 `すべきです` 已覆盖审查例）；未用 `判断した`（避免误伤 `判断したよ` 一类口吻）。
  - 仍可能漏掉更绕的元叙述（无上述套话、却用 `ので` + 旁白）。`見ることにしますね` 这类礼貌决定句会被丢弃。
  - 空 content 不再 dump `raw={}`；HTTP 异常路径仍走原 warning。
- remaining risks:
  - 未改 prompt、persona、请求 payload、重试、翻译、冷却、设置或 UI。
  - 协调者必须在真实环境复跑 focused + `tests/unit tests/ui` 和 `git diff --check` 后再收口。

## Codex

- reviewed the final classifier and `_post_speech_decision` integration; valid JSON remains the first path and no extra request, retry, translation, prompt, or cooldown change was introduced.
- independently found and returned two issues to the same Cursor session: an invalid inline regex flag that broke module import, and a Japanese meta-explanation false positive.
- focused verification: `35 passed in 0.73s` for `test_observer_plain_dialogue_fallback.py` plus `test_relationship_timer.py`.
- full verification: `1731 passed, 1 skipped in 31.41s` for `tests/unit tests/ui`.
- `git diff --check`: passed.
- accepted residual behavior: the fallback is intentionally conservative and may discard uncommon valid dialogue rather than risk speaking model analysis.
