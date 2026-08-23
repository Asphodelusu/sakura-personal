# Acceptance Tests — Kimi K2.6 Compatibility

## Static behavior

- `kimi-k2.6` 请求体不包含 `temperature`，首次请求无需供应商拒绝后回退。
- 其他支持 temperature 的模型继续收到配置温度。
- K2.6 主回复继续包含 `thinking: {type: disabled}`。
- 主 Chat 仍走 `complete_with_tools()` 非流式路径，没有新增 `stream=true`。

## Reply adoption

- 合法 JSON segments，存在 `ja`、缺 `zh`：不调用第二次结构化合成。
- 合法 JSON segments，存在 `ja` 与 `zh`：照常采用。
- 纯文本、坏 JSON、空日语：仍触发结构化修复。
- 会改变答案的成功工具结果：仍按现有规则合成最终回复。

## Commands

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_api_client.py -q
.\.venv\Scripts\python.exe -m pytest tests/unit/test_reply_translation_decouple.py -q
.\.venv\Scripts\python.exe -m pytest tests/unit tests/ui -q
git diff --check
```

## Runtime follow-up

重启 Sakura 后正常聊 5～10 轮，检查：

- Kimi 请求日志 `chat_params` 保留 `thinking.disabled`；
- HTTP 请求体不含 temperature；
- 合格 ja-only segments 不再出现 `首轮最终回复不合格 ... missing_translation`；
- 每个无工具普通对话原则上只有一次 Kimi 主请求；
- 首句延迟较修复前减少约 4 秒，具体以新日志为准。

