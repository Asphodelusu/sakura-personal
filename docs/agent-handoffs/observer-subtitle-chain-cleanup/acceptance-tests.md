# Acceptance Tests

## Observer / relationship history semantics

- 近期上下文能稳定区分 user / assistant，并保留 ordinary / proactive / relationship 来源。
- 上下文包含可比较的时间或年龄；较新的纠正与完成事实排在并覆盖较旧计划。
- 一次多 segment 的 Sakura 回复不会把用户后续纠正挤出窗口。
- 17:02 约吃饭、随后吃完、17:58 再纠正、18:04 再确认后，Observer 不会再把“去吃亲子丼”当作当前计划。
- 旧 sensory impression 若早于最近用户或主聊天事实，不再以当前情境注入；更新的 impression 仍可使用。
- 不改变既有 screen / relationship 仲裁、idle、退避与 cooldown 行为。

## Subtitle / translation / TTS

- 中文模式且已有 `zh`：气泡文字可见事件严格早于 TTS 实际播放开始事件。
- 日文模式：`ja` 同样先可见，再开始 TTS。
- 第一段不会因为 TTS 很短或完成回调很早而在最低阅读时间前消失。
- TTS 较长时不叠加不必要的完整额外等待；只补足尚未满足的 dwell。
- 翻译晚到并替换占位/回退文本时，从译文实际显示时重新计算阅读 dwell。
- 动作 `（...）` 不进入 TTS；等待译文或 bounded fallback，显示后有足够阅读时间，再进入对白。
- 翻译失败、超时仍能有界继续，不死锁。
- 旧 interaction 的 TTS、translation、timer 回调不能覆盖或推进新回复。
- 现有 tone、portrait、历史回填、按钮及分段顺序保持不变。

## Safety / scope

- 不写生产 SQLite、日志、角色文件和 API 配置。
- 不加入“吃饭”或其他主题关键词特判。
- `git diff --check` 通过；只修改合同允许文件。
