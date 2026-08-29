# Acceptance Tests

## P0 measurement

- 同一 synthetic payload 不因 dict 插入顺序不同而改变 hash。
- system/messages/tools/image/whole 的 bytes、估算 token 与 hash 可对账；无法可靠拆出的 runtime 明确为 null。
- inspection 和默认日志不含 synthetic prompt 原文、tool result、data URL、Key。
- 每次请求有 interaction、index、purpose；initial/tool/semantic/structural 不混淆。
- cached input 与 reasoning/thinking usage 仅从白名单字段读取，缺失为 null。
- P0 开关或观测代码不改变任何发给 provider 的 payload。

## P1-A structural repair

- 合格结构：0 次额外修复。
- 仅缺中文字幕：0 次主模型修复，translation sidecar 行为不变。
- 完整纯日语非 JSON：结构修复请求不含 persona/history/tools/runtime/image，仅带最小合同。
- 结构修复输出保持日文规范化等价与 segment 顺序，tone/portrait 只能使用合法值。
- 改写、缺句、乱序、非法枚举、空输出：拒绝并回退 full semantic compose。
- 工具结果/搜索/记忆会改变最终答案时不误走结构快车道。
- 正常调用数不增加；失败路径有界，不形成 repair loop。

## Regression/safety

- DeepSeek、Kimi、Gemini 现有 payload 组装与 temperature/thinking 兼容测试通过。
- translation sidecar、回复解析、tool loop 聚焦测试通过。
- 不修改角色、Observer、UI、TTS、配置 Key 与生产数据。
- `git diff --check` 通过；未 commit、未 push。
