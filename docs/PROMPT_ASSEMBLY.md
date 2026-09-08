# Prompt 装配与角色包适配

Sakura 会按调用用途装配不同的提示词，而不是把完整人格卡无差别地发送给每一次模型调用。这样做的目标是让主对话保留足够的人物深度，同时让观察、内心独白、结构修复等窄任务只接收完成该任务所需的角色信息。

这套装配不增加模型调用，也不要求安装新的 Python 依赖。已有用户升级后可继续使用原来的 API Profile 与 `model_slots`；提供商若不接受中段或尾部 `system` 消息，客户端会在收到明确的 400/422 位置拒绝后，按 endpoint + model 记录兼容档位并进行有界回退。

## 首次采用需要准备什么

### 只运行 Sakura

新装配层没有新增配置项、数据库迁移或额外依赖。更新源码后保留现有 `data/`，并确认以下内容即可：

- 至少配置一个可用的 `chat` 模型槽；需要主动看屏时再配置支持图片输入的 `vision_chat`。
- 准备包含 `character.json` 与人格资源的角色包。本仓库不附带完整角色包，导入方式见根目录 README。
- API Key 只保存在本机配置中，不要写入角色包、测试 fixture、日志或 `.local/` 评测产物。
- 更新后重启 Sakura，使新的 prompt 装配代码生效；不要为了升级删除聊天、关系或记忆数据。

`chat_fast`、`inner_thought`、`memory_curation` 等用途槽可以继续按性能和成本单独配置；未配置可选槽时沿用项目现有回退规则。具体配置见 [API_CONFIG.md](API_CONFIG.md)。供应商不接受某种 system-message 位置时，兼容回退由客户端自动学习，不需要手工设置 role 档位。

### 开发、改角色或运行完整测试

完整人格 prompt 的 golden 有意放在 ignored 的 `.local/golden/prompts/`，不会随仓库分发。这样可以测试逐字装配合同，又不会把完整角色材料提交到版本库。运行 `tests/unit/test_prompt_assembly_contract.py` 前，必须先取得与本项目固定断言匹配的私有 Sakura 资源，并分别放到 `characters/Sakura/card.md`、`characters/Sakura/system_guards.md` 与 `characters/Sakura/relationship_guide.md`；任意自定义角色包不能替代这组三份测试输入。缺少这些资源时，Sakura 本体仍可使用其他完整角色包运行，但这份与私有 Sakura 内容绑定的 prompt 合同测试（包括 A7 golden）不具备运行前置条件。

fresh checkout 首次准备好上述三份资源并运行 prompt 合同测试，或有意修改 prompt 后确认新结果时，再在 Windows CMD 执行：

```bat
set SAKURA_UPDATE_PROMPT_GOLDENS=candidate
.venv\Scripts\python.exe -m pytest tests\unit\test_prompt_assembly_contract.py::test_a7_private_prompt_goldens_match_fixed_inputs -q
set SAKURA_UPDATE_PROMPT_GOLDENS=
```

随后再运行本页末尾的验证命令。`candidate` 更新后必须人工查看实际 prompt，确认只是预期的层级、内容或位置变化；不能因为测试转绿就直接接受。只有准备一次新的 A/B 基线时才生成或刷新 `baseline`，日常升级不要覆盖它。`.local/ab/` 下的 payload、盲评 key 与结果同样不是运行 Sakura 的必需品，也不应在不同安装之间复制。

## 装配层级

静态提示按以下用途分层：

| 层 | 内容 | 典型来源 |
|---|---|---|
| L0 | 身份、人称、数字生命定位 | `system_guards.md` 的 `## 身份与人称` 与桌宠身份锚 |
| L1 | 常驻行为核 | `card.md` 的人物判断、行为与语言节奏 |
| L2 | 叙事深度、关系导演、可选亲密层、插件补充 | `card.md`、`relationship_guide.md`、可选导演与 prompt patch |
| L3 | 回复格式 | JSON segments、tone、工具轮输出协议 |
| L4 | 当前任务规则 | 工具、事件、上下文获取、Observer 指令 |
| L4' | 不应被上文冲淡的尾部演出约束 | `system_guards.md` 中身份段以外的内容 |
| L5 | 稳定动态事实 | 记忆、关系状态、角色 lore 等 |
| L6 | 当前时刻 | 当前时间与当前工具循环进度 |

主聊天与最终回复可以使用 L0–L4'；主动工具轮和事件回复默认不注入 L2 叙事层。Observer、内心独白、记忆整理与结构修复使用更窄的角色投影。L5 位于本轮真实用户消息之前，L6 位于生成点附近；两者使用稳定 marker 和独立信封，供装配检查与供应商兼容回退识别。

## 回复结构与 `zh` 字段

不同调用类别对 `zh` 的合同不同，不要把其中一条扩展成全局规则：

- 正常主回复与 `semantic_compose` 目标格式包含日文 `ja` 和对应的中文 `zh`。
- 合格 JSON 若暂时缺少 `zh`，日文正文仍可采用；界面先回退显示日文，字幕翻译侧路可异步补齐。
- `structural_repair` 只负责把已经生成的日文无损封装成 JSON，不负责翻译。这个调用里的 `zh` 必须是空字符串。

结构修复结果进入回复链前会在本地重新校验日文等价性与 tone/portrait 白名单，然后只重建 `ja`、空 `zh`、`tone`、`portrait` 四个字段。模型擅自返回的翻译、`drive_effect`、`suppress_tts` 和未知字段都会被丢弃。日文被改写、枚举越界或 JSON 无法解析时仍会回退到语义合成；日志只记录 `invalid_json`、`japanese_changed`、`tone_not_allowed`、`portrait_not_allowed` 等低基数原因码，不记录被拒正文。

## 自定义角色包需要准备什么

旧角色包没有新增硬性必填文件，仍可加载。为了让各类调用获得稳定、可选择的角色信息，建议按下面的结构编写文本资源。

### `card.md`

推荐至少提供这些二级标题：

```markdown
## 她怎样存在
...

## 判断、选择与修复
...

## 语言与节奏
...
```

这三段组成常驻 L1 行为核。其他 `##` 段落会作为 L2 叙事材料，只在需要人物深度的调用中准入。没有这些标题的旧卡会走兼容回退，但 Observer 与内心独白只能从非结构化文本中截取有限内容，角色还原通常不如明确分段稳定。

标题用于表达行为边界，不建议把同一句规则复制到多份资源里。身份事实放 L0，人物如何判断和表达放 L1，经历、生活纹理和关系场景放 L2，输出格式则留给程序协议。

### `system_guards.md`

该文件可选。若提供，建议把纯身份信息单独放在精确标题 `## 身份与人称` 下：

```markdown
## 身份与人称
- 以角色自己的第一人称回应。
- 说明对方与角色的基本关系定位。

## 其他演出约束
...
```

`身份与人称` 会被抽到前部 L0；其余段落保持原顺序进入尾部 L4'。不要在尾部 guards 再复制整张人格卡，也不要依赖大量否定句替代正向的人物行为描述。

### `relationship_guide.md`

该文件可选；缺失时角色正常运行。若 `character.json` 显式配置 `relationship_guide`，路径必须位于角色包内；未显式配置时会尝试同目录的 `relationship_guide.md`。

当前可识别的标准二级标题包括：

- `A. 日常主动强度`
- `B. 身体推进直接度`
- `关系未明`
- `稳定恋人日常`
- `感情如何出口`
- `私下升温`
- `嫉妒、冷落与冲突`
- `公私切换`
- `高温后的生活`

普通主聊只常驻前言、两个强度旋钮和 `感情如何出口`；其他场景段不会每轮全部注入。未知标题会按源顺序保留为兼容段，完全无标题的旧 guide 会作为一个兼容段处理。

## 插件与动态上下文

- 多个 `prompt_patch` 共享 600 个估算 token 的总预算；后注册内容可能被截断或不准入。插件应补充局部能力，不应再次粘贴完整人格卡。
- 动态上下文总线有独立预算与可信度信封。插件 context provider 应提交结构化 fragment，并正确声明 trust、required 与 token budget，不要自己拼接 L5/L6 marker。
- `estimate_prompt_tokens()` 是确定性的工程估算，不是供应商 tokenizer 真值；容量规划需要保留余量。

## 升级与验证

升级现有安装时不需要迁移聊天记录或关系数据库，也不要删除 `data/`。建议先在测试环境完成以下检查：

```bat
.venv\Scripts\python.exe -m pytest tests\unit\test_prompt_assembly_contract.py tests\unit\test_payload_inspection.py tests\unit\test_system_guards_prompt.py tests\unit\test_relationship_guide_prompt.py -q
```

这些测试证明装配顺序、预算、role 与位置合同，不证明某个模型的角色表现一定更自然。更换模型、endpoint、角色卡或大型 prompt patch 后，应使用不含真实聊天和隐私数据的固定场景做盲评；真实 API 评测会产生费用，也可能把完整角色提示发送给供应商，应先取得角色材料与费用授权。

私有 prompt 快照、A/B payload、评审 key 和运行结果应放在已忽略的 `.local/` 下，不应提交到公开仓库。API Key、Authorization header、真实记忆、聊天正文和屏幕内容不得写入评测产物。
