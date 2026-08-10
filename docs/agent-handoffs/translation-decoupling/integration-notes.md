# Translation Decoupling Integration Notes

外部 Agent 完成后在各自工作区填写；不要覆盖另一位 Agent 的区块。

## Cursor / Grok — Reply Pipeline

- 状态：已完成（共享工作区，未使用 worktree）
- 分支或 worktree：`fix/arch-review`（共享工作区）
- Commit：`4d869f3` — `feat: 日语主回复与中文字幕解耦（Phase 1）`
- 修改文件：
  - `app/llm/chat_reply.py` — 缺 zh 时不再用日语填充 `translation`
  - `app/agent/reply_composer.py` — 结构化 segments 可缺 zh 直接采用；`missing_translation` 不再进入二次 Pro 合成集合
  - `app/llm/translation_provider.py` — 最小 `TranslationProvider` 协议 + `FakeTranslationProvider`
  - `app/ui/subtitle_translation.py` — 待译筛选 / 回填辅助
  - `app/ui/pet_window.py` — 异步字幕翻译 worker、历史回填、过期 interaction 丢弃
  - `app/storage/chat_history.py` — `append` 返回 id；新增 `update_translation`
  - `tests/unit/test_reply_translation_decouple.py` — TDD 红绿测试
- RED 测试及预期失败：
  - `test_missing_translation_currently_triggers_second_pro_compose`：纯日语正文 → `reason=missing_translation` → `complete_with_tools` 两次（表征旧行为，仍通过）
  - `test_structured_segments_missing_zh_accepted_without_second_pro`：修前失败于 `translation` 被日语填充
  - `test_parse_keeps_empty_translation_when_zh_missing`：修前失败于解析填充
- GREEN 测试结果：
  - `.\.venv\Scripts\python.exe -m pytest tests/unit/test_reply_translation_decouple.py tests/unit/test_agent_runtime.py -q` → **53 passed**
  - 相关筛选 `history or chat_reply or reply` → **93 passed**
- 设计摘要：
  1. 首轮 JSON segments 只要 `ja`（及 tone/portrait）合格即可采用，缺 zh 不触发第二次 DeepSeek Pro 结构化合成。
  2. 纯日语无结构正文仍由 `_structured_compose_reason` 拉起**一次**合成以拿到 segments。
  3. 缺 zh 交给异步 `TranslationProvider`（默认未注入则为 `None`，跳过翻译）；TTS 继续用日语 `text`。
  4. 翻译成功后回填字幕（current/pending/queued）、`reply_history_segments` 与 SQLite；失败只保留日语原文，无系统降级文案。
  5. 过期 / 被替代的 `interaction_id` 结果丢弃。
- 竞态与遗留风险：
  - 旧翻译线程可能跑完但靠 `interaction_id` 丢弃；同刻仅一条翻译任务。
  - 打字机播放中途回填可能闪一下或长度变化。
  - 历史 id 与 segment 下标依赖同序 clean segments，过滤变化时可能错位。
  - `translation_provider` 默认 `None`：未注入真实供应商时只有「不二次 Pro」，无异步字幕后补。
  - **未碰** TTS 合成器内部、benchmark、评测工具、记忆 / Observer / 亲密模式。

## Claude Code / DeepSeek Flash — Translation Benchmark

- 状态：完成
- 分支或 worktree：fix/arch-review（共享工作区，无 worktree）
- Commit：feat(benchmark): 日→中翻译基准工具（见本批次提交）
- 修改文件：scripts/translation_benchmark/{extract.py,providers.py,run.py}、tests/unit/test_translation_benchmark.py、data/benchmark/{dataset.jsonl,per_sample_results.json,results.json,blind_review.md}
- 数据集范围：最近 200 条 assistant（Sakura.db chat_history，非空 content+translation；排除 error/system/空翻译）；含 tone 与特殊标注
- 候选 provider：deepseek-v4-flash（强制 json_object）、qwen3.7-plus（dashscope）；DeepL 无 key 占位、本地 fallback 接口预留
- P50/P90 与失败率：
  - deepseek_flash：P50 5.9s / P90 9.3s，结构化成功 88%，失败率 12%（全为 JSON 解析失败，需重试/降级兜底）
  - qwen3.7-plus：P50 13.0s / P90 16.4s，结构化成功 99.5%，失败率 0.5%
- 人工盲评位置：data/benchmark/blind_review.md（200 条，含省略主语/傲娇否定/吃醋/请求/称谓/亲密语气/疑似降级标注）
- 推荐顺序与遗留风险：
  - 推荐 provider 顺序：qwen3.7-plus 优先（结构化成功率 99.5%、翻译质量稳、语气贴合）；deepseek-v4-flash 作为低延迟备选（88% 需重试/降级兜底）
  - 遗留风险：① flash 12% JSON 失败需重试/降级，单次延迟无硬上限；② 独立翻译 P50 6-13s，作异步 sidecar 可接受（不阻塞 TTS），但字幕晚到；③ 亲密语境称谓（パパ/あんた 等）直译 vs 委婉处理需人工校准；④ 历史 translation 存在降级/错位样本（已在数据集标注）

## Integration Review

- 审查人：Claude Code（集成负责人）
- 合并方式：共享工作区，串行本地提交（4d869f3 Phase1 → ba5a864 benchmark → 79cb0f8 docs routing → 68407b2 集成修复），由集成者统一 push
- 最终 commit：4d869f3（Phase1）+ ba5a864（benchmark）+ 79cb0f8（docs）+ 68407b2（集成修复：MinimalConsumeWindow 桩补齐）
- 完整测试：`.\.venv\Scripts\python.exe -m pytest tests/unit tests/ui -q` → **1423 passed, 6 skipped**
- 新日志基线：待新版本日志测量（missing_translation 二次 Pro 重合成率应显著下降；首句延迟 P50/P90 复测）
- 是否可 push：可 push（两个 worker + 集成修复全绿）；按协作约束默认不 push，等集成者/用户授权
- 接口备注：主链 `TranslationProvider`（批量 list[str]→list[str]）与 benchmark provider（单条 str→TranslationResult）不一致，真实 provider 接入见 `provider-adapter-decision.md`（待 Codex 审查）

