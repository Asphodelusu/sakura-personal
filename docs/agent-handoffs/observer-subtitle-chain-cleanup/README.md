# Observer / Subtitle Chain Cleanup

在继续 `session-context-optimization-research` 已批准的“测量体系 + 小型结构修复”前，先清理两条会直接破坏日常体验的既有链路：

1. Observer / 关系主动必须可靠区分谁说的、何时说的、通过什么渠道说的，以及计划是否已经被后续对话完成或纠正。
2. 分段字幕、翻译与 TTS 必须按可见性和阅读时间推进，避免语音先于文字、第一句过早消失、动作翻译尚未可读就跳到下一句。

执行者：Cursor Desktop / Grok 4.6 High Fast。Codex 负责根因与合同、最终 diff 审查、聚焦验证和集成决策。

本批与 `docs/agent-handoffs/session-context-optimization-research/` 并列；不得修改、移动或删除该未跟踪研究目录。
