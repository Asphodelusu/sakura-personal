# Acceptance Tests

- light detail 中始终包含低分、最旧且与当前对话无关键词重合的 core profile。
- `len(detail) <= LIGHT_CURATION_DETAIL_LIMIT`。
- core profile 不出现在 `index_only`。
- 无 core profile 时，原评分排序与 detail/index 数量不变。
- 现有 memory curator 单测全部通过。
- 不读取或修改任何真实记忆、聊天历史、日志或角色数据。

