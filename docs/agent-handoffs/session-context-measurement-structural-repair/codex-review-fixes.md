# Codex Review Fixes — Cursor CLI

在现有未提交实现上做短修复，只允许修改本批已经拥有的文件与测试，并填写 `integration-notes.md` Cursor 段的 review-fix 小节。不要 commit、不要 push、不要调用真实 API。

## 必修 1：正文与内存有界

当前 `PayloadInspection` 被 `object.__setattr__` 偷挂 `_whole_bytes`，`client.payload_inspections` 又无限增长；等于长期保留每轮完整 prompt/tool/data URL，并造成内存增长。修复要求：

- `PayloadInspection` 对象本身及可序列化/可遍历状态中不得含原始 payload bytes/body。
- 若计算相邻 stable prefix 必须短暂保留前一 payload，只允许 client 私有、有界（优先仅前一个 scope/payload）；不得进入 inspection 历史。
- `payload_inspections` 有明确 maxlen；`_request_index_by_interaction` 有明确上限/淘汰，不随长期聊天无限增长。
- 新增 RED，证明 inspection 无隐藏正文属性、历史和 interaction map 超限会淘汰。

## 必修 2：raw 请求覆盖

`complete_raw()` 当前完全不测量，会漏掉独白、记忆、翻译、屏幕观察等。为 `complete_raw()` 接入同一 inspection/purpose/index 接口；默认 purpose 为 `unknown`，显式调用可传合法 purpose，字段不得进入 provider payload。成功后 usage 要可关联；失败至少要留下无 usage inspection，不得因测量改变 fallback/retry/payload 行为。

`stream_raw()` 本批可保持未覆盖，但必须在 integration notes 明确记为后续缺口，不能再声称“每个 HTTP 请求全部覆盖”。不要为 stream 做大改。

## 必修 3：DeepSeek usage 字段

白名单支持 DeepSeek 的 `prompt_cache_hit_tokens` 作为 `cached_input_tokens`；可读取但不要把原 provider usage 全量透传。测试应验证该字段，而不是刻意断言忽略。

## 必修 4：purpose 必须反映真实成本

`_parse_final_reply_with_retry()` 最后那次旧 repair 会携带完整 `system_prompt + working_messages`，它不是最小结构快车道。不得标为 `structural_repair`；改为 `semantic_compose`（或若现有调用状态更准确，使用合同内另一非 structural purpose），并加测试证明 full-context 请求不会进入 structural 指标。

## 必修 5：结构修复不得生成中文字幕

快车道只修 JSON/segments/tone/portrait，`zh` 必须为空并交给 translation sidecar。`structural_repair_is_faithful()` 应拒绝任何非空 translation，或在采用前确定性清空；优先 fail closed。新增同 JA 但擅自填 zh 的失败测试，并验证回退 semantic compose。

## 验证

先逐项 RED，再最小 GREEN。至少运行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_payload_inspection.py tests/unit/test_request_purpose_propagation.py tests/unit/test_structural_repair_fast_lane.py tests/unit/test_reply_translation_decouple.py tests/unit/test_api_client.py -q --tb=short
git diff --check
```

不要运行全量门禁。报告实际 RED、改动文件、GREEN 数量、stream 缺口与任何兼容风险。
