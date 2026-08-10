# 翻译 Sidecar 接入真实 Provider — 决策摘要

> 请 Codex **仅审查本决策**，不需全仓规划。背景见 `integration-notes.md`。

## 问题

Phase 1 已把"缺 zh 不触发二次 Pro"落进主链，`TranslationProvider` 协议默认 `None`（不翻译）。
要把 benchmark 评估出的真实 provider 接进异步字幕后补，存在**接口不匹配**：

| 层 | 接口 |
|---|---|
| 主链协议 `app/llm/translation_provider.py:13` | `translate(texts: list[str], *, source_lang="ja", target_lang="zh") -> list[str]`（批量、等长、抛异常） |
| benchmark provider `scripts/translation_benchmark/providers.py` | `translate(ja: str) -> TranslationResult`（单条、含 ok/latency/error） |

## 方案 A（推荐）：Adapter 包装，不改主链协议

新增 `AdapterTranslationProvider(translation_provider.TranslationProvider)`：
- 持有一个 benchmark 风格的底层 provider（`OpenAILikeProvider`，OpenAI 兼容）。
- `translate(texts, ...)`：批量循环调用底层 `translate(t)`；全部成功 → 等长 `list[str]` 返回。
- 任一条失败 → 抛异常（调用方捕获后保留日语原文，符合 Phase 1 失败语义）；或对失败条返回原文（不抛）。**二选一需定**：抛异常语义更贴近"失败保留原文"，返回原文会让"部分失败"静默——建议前者。
- provider 实例来源：配置（`system_config` / 注入），不硬编码 key。

## 方案 B：改协议为单条/异步 — 不推荐

改主链接口会波及 `subtitle_translation.py`、`pet_window.py` 的 worker 与回填逻辑，Phase 1 已验证的红绿测试会重写，风险大。

## Provider 选择（benchmark 实测）

- **qwen3.7-plus 优先**：结构化 99.5%、失败率 0.5%、翻译质量稳、语气贴合。
- deepseek-v4-flash 备选：P50 5.9s 快一倍，但 12% JSON 失败需重试/降级兜底。
- 延迟 P50 6-13s：作异步 sidecar 可接受（不阻塞 TTS），字幕晚到属预期。

## 需 Codex 裁定

1. Adapter 放哪个文件（建议 `app/llm/translation_provider.py` 或独立 adapter 模块）。
2. 失败语义：抛异常（保留原文）还是失败条返回原文。
3. Provider 注入方式：配置驱动 vs 代码装配。
4. 是否现在接 qwen，还是等用户确认后 Phase 2 再接。
