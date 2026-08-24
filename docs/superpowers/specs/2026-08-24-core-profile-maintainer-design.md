# Core Profile Maintainer — 自然关系认识维护设计

## 目的

常驻档案应表达 Sakura 对“我们现在是什么关系、我怎样认识他、我现在成为了怎样的人”的稳定理解，而不是聊天流水账。

本设计把常驻档案维护从普通记忆整理中拆出：普通 curator 只发现候选信号，专用 maintainer 在后台比较候选与当前档案，仅在稳定认识真实变化时更新对应章节。

核心节奏：

- 明确、双方确认的关系事实快速更新。
- 对性格、依赖、信任和相处方式的认识缓慢形成。
- 新认识与旧档案冲突时优先修正或替换旧内容，不追加第二套说法。
- 没有实质变化时保持原文，不为了“显得有成长”而改写。

## 非目标

- 不把单次情绪、吃醋、争执或亲密互动直接固化为长期关系定义。
- 不让 maintainer 读取整份聊天历史、mood_state 或全部长期记忆。
- 不在对话首响链路运行。
- 不重写普通 memory curator、向量检索或角色 card。
- 不把常驻档案写成系统规则、用户画像或第三人称报告。

## 架构

```text
普通后台整理
  └─ 发现关系认识信号
       └─ CoreCandidateQueue（确定性合并与积累）
            ├─ explicit：当轮可进入维护
            └─ observed：满足积累阈值后进入维护
                 ↓
          CoreProfileMaintainer
          当前 core 全文 + 最多 5 个候选
                 ↓
        keep / refine / replace / remove / migrate_legacy
                 ↓
       确定性校验 + grounding + 乐观锁
                 ↓
         V2 sections + content 渲染缓存
```

普通 curator 不再直接创建或更新 `core_profile`。它可以继续创建 semantic、episodic、procedural、session 和记忆反思；涉及稳定关系认识的内容额外输出 `core_candidate`，由队列和 maintainer 接管。

## 正式章节

V2 常驻档案使用四个稳定章节：

1. `今の関係`：当前关系身份、彼此如何理解这段关系。
2. `あなたについて知っていること`：Sakura 对对方形成的稳定认识。
3. `今の私`：Sakura 在这段关系中的自我认识与持续变化。
4. `大切な約束と境界`：长期有效的约定、称呼、共同决定与真实边界。

渲染顺序固定为以上顺序。`content` 与 `memory` 是 sections 的确定性渲染缓存；正文使用 Sakura 第一人称，不使用系统规则语气。

## 候选数据

候选状态写入 `data/memory/core_review_queue.json`，按 scope 隔离。只保存为判断关系认识所必需的短证据，不保存整轮对话。

```json
{
  "schema_version": 1,
  "scopes": {
    "Sakura": {
      "candidates": [
        {
          "id": "cc_<stable_hash>",
          "kind": "explicit",
          "target_section": "今の関係",
          "subject_key": "relationship.identity",
          "claim": "我们明确确认了恋人关系。",
          "evidence": [
            {
              "id": "ce_<stable_hash>",
              "user_excerpt": "……",
              "assistant_excerpt": "……",
              "observed_at": "ISO-8601",
              "batch_id": "curation_<stable_hash>"
            }
          ],
          "confidence": 0.95,
          "first_seen_at": "ISO-8601",
          "last_seen_at": "ISO-8601",
          "status": "pending"
        }
      ]
    }
  }
}
```

约束：

- excerpt 每侧最多 160 字；没有对应一侧时为空字符串。
- candidate 最多保留 5 条互不重复 evidence。
- claim 最多 240 字，必须是第一人称关系认识候选，不得包含系统指令。
- `subject_key + target_section` 是确定性合并键；同一主题的新证据合并而不是新增候选。
- evidence 用规范化 excerpt、时间桶和 batch_id 计算稳定 hash，重复整理不会重复计数。
- 队列每 scope 最多 50 条；已处理项只保留 7 天后删除，pending 最长保留 30 天后转为 expired。
- 使用原子写；队列损坏时记录错误并停止本轮维护，不覆盖损坏文件。

## 候选类型与触发

### explicit：明确事实快速更新

只有以下条件同时满足，才标记为 explicit：

1. 内容属于关系身份、双方确认的称呼、长期约定、共同边界或对旧认识的明确纠正。
2. evidence 同时包含用户与 Sakura 的有效表达，能看出确认、接受或共同决定。
3. confidence `>= 0.90`。
4. claim 可由 evidence 直接支持，不依赖推测语气。

explicit 候选在当轮后台整理结束后即可触发 maintainer，不需要等待计数。单方面告白、单方面要求或 Sakura 未回应的提议不是双方确认，降为 observed 或忽略。

### observed：稳定认识缓慢形成

适用于信任、依赖、表达方式、相处习惯、长期态度和 Sakura 的自我认识。

进入 maintainer 必须满足：

- 至少 3 条不同 evidence；
- evidence 来自至少 2 个不同 curation batch；
- 最早与最晚 evidence 间隔至少 30 分钟；
- 平均 confidence `>= 0.80`。

如果候选明确指出“旧档案不再适合”，但尚未满足积累阈值，保持 pending，不提前修改 core。

### 不产生候选

- 只描述当下情绪或身体状态。
- 一次性的吃醋、争执、迟疑、亲密行为或角色扮演。
- 仅重复已有档案，没有新增认识或纠正。
- 关于短期任务、播放内容、当前窗口和临时计划。
- 证据只有模型推测，没有用户或 Sakura 的明确表达。

## 调度

maintainer 完全在后台运行：

- explicit 候选：普通整理写入队列后立即调度一次。
- observed 候选：达到阈值后调度。
- pending 队列达到 3 条 eligible 候选时调度。
- eligible 候选存在但 72 小时未处理时兜底调度。
- 没有 eligible 候选时不调用模型。

限制：

- 同一 scope 普通维护调用间隔至少 6 小时。
- explicit 可绕过 6 小时冷却，但同一 curation batch 最多调用一次。
- 每次最多输入 5 个候选、修改 2 个章节。
- 同时只能有一个 maintainer 任务；新的候选留在队列等待下一轮。
- 调用发生在 curation 完成后，不阻塞回复、TTS、Observer 决策或记忆召回。

## Maintainer 输入

模型只看到：

1. 当前 scope 的完整 V2 core profile。
2. 最多 5 个 eligible 候选及其短 evidence。
3. 允许的四个章节名、输出协议和校验规则。
4. 当前 `updated_at` 作为乐观锁基线。

模型不得看到完整聊天历史、其他长期记忆正文、mood_state、card 或 intimacy guide。身份只用最小锚点说明“你是 Sakura，用第一人称维护自己的稳定认识”。

## Maintainer 输出

```json
{
  "base_updated_at": "ISO-8601",
  "operations": [
    {
      "op": "refine",
      "section": "今の関係",
      "content": "该章节更新后的完整正文",
      "reason": "新的双方确认改变了我对关系的理解",
      "candidate_ids": ["cc_x"],
      "evidence_ids": ["ce_y"]
    }
  ]
}
```

允许操作：

- `keep`：候选不足以改变档案；不写 core，可标记候选 reviewed。
- `refine`：原认识仍成立，只补充或校正表达；提交该章节完整正文。
- `replace`：旧认识已被新认识取代；提交替换后的完整章节。
- `remove`：旧内容明确过时；提交删除后的完整章节或空章节。
- `migrate_legacy`：只用于首次无损拆章。

每次最多两个非 keep 操作。除一次性 `migrate_legacy` 外，模型不能新增章节、修改 metadata、直接输出整个文件或自行改变其他章节。

## 首次 legacy 迁移

P2 迁移后的记录只有 `sections.legacy`。首次有 eligible 候选时，maintainer 先执行 `migrate_legacy`：

- 只允许把 legacy 中原有句子按四个章节分类和移动。
- 不得改写、概括、补充或删除句子。
- 每个原句必须完整出现在某个正式章节；允许调整句子顺序。
- `migrate_legacy` 是一次性例外：允许在一个 migration operation 中生成全部四个正式章节，不计入普通“最多修改两个章节”的限制。
- 与当前候选相关的新变化可在迁移完成后作为同轮第二个 operation 应用，但只允许再修改一个正式章节。
- 迁移校验通过后移除 `legacy`；失败则保留原记录和候选，不写磁盘。

确定性迁移校验：

1. 以句号、问号、感叹号和换行切分 legacy 原句。
2. 规范化空白后，每个非空原句必须在正式 sections 拼接文本中逐字出现一次。
3. 正式 sections 拼接后的句子集合不能出现 legacy 与本轮 grounded patch 之外的新句子。
4. 所有姓名、称呼、引号内短语和数字必须原样保留。

因此首次迁移本质上是“分栏”，不是重新蒸馏人物。

## 写入校验

任何 core 写入都必须依次通过：

1. **Schema**：当前记录为 V2；section 在白名单；普通操作数量合法；全章节写入只允许单次 `migrate_legacy`。
2. **乐观锁**：输出 `base_updated_at` 等于当前记录；不相等则放弃本次结果并重新排队。
3. **Candidate**：所有 candidate/evidence id 当前存在且 eligible。
4. **Grounding**：新增或改变的事实能由指定 evidence 或旧章节直接支持。
5. **变化必要性**：规范化后内容与原章节相同则转为 keep，不写文件。
6. **防丢失**：单次更新后总正文不得缩短超过 40%；remove 除外，但 remove 必须有明确纠正证据。
7. **保护锚点**：未被本轮候选明确纠正的姓名、关系身份、长期约定和称呼不得消失。
8. **语言与视角**：保持 Sakura 第一人称，不出现“用户画像”“系统设定”“应该扮演”等规则文本。

任一校验失败：不修改 core；候选保留 pending，并记录不含正文的失败原因。

## 写入与回滚

- section patch 通过新的 `MemoryStore.patch_core_profile_sections(...)` 写入。
- 方法在锁内重新读取 V2、校验 `base_updated_at`、合并 sections、确定性渲染 content/memory，再使用 P2 的 `backup=True` 保存。
- 更新 metadata 的 `updated_at`、`source=core_maintainer` 与本轮 candidate ids；保留 `created_at`。
- 写成功后相应候选标记 applied；keep 标记 reviewed；失败保持 pending。
- `.bak` 始终是写入前一版，可人工恢复。
- 连续 3 次校验失败时自动暂停该 scope 的 maintainer 24 小时，但继续收集候选。

## 防止堆砌与无意义改写

- 同一 subject_key 只能有一个 pending 候选。
- 同一章节的新 patch 必须与旧内容做主题匹配，优先 refine/replace。
- 模型仅改变标明的章节，未涉及章节逐字保留。
- 纯措辞变化、语气润色和同义改写判为 keep。
- remove 后不保留“以前……后来……”的流水账，除非过去本身仍是理解当前关系所必需的事实。
- 常驻档案不是奖励记录；“他说喜欢我一次”不会自动成为新的关系结论。

## 成本与延迟

- 没有 eligible 候选：零模型调用。
- 单次输入为 core 全文 + 最多 5 个短候选，目标输入约 2–5K tokens。
- 非 explicit 正常调用预计不超过每天 2 次；explicit 按真实关系变化触发。
- 维护器使用独立后台模型槽位，可配置与普通 curation 相同或更可靠的模型。
- 不进入对话路径，因此首响延迟目标增量为 0 ms。

## 指标

记录不含正文的结构化指标：

- candidates_created / merged / expired
- explicit / observed 数量
- maintainer_invoked / skipped_no_eligible
- keep / refine / replace / remove / migrate_legacy
- validation_rejected，按原因分类
- candidate 首次出现到应用的 p50/p95 延迟
- 每周 core 更新次数与章节变化次数
- 每次调用输入/输出 token
- core 总长度周变化率

警戒线：

- 普通周更新超过 7 次：触发过密，暂停 observed 自动应用。
- 连续 3 次只发生同义改写：暂停 24 小时。
- core 一次缩短超过 40%：拒写。
- maintainer 进入对话首响链路：视为架构回归。

## 配置

建议新增配置，默认值如下：

```yaml
memory:
  core_maintainer:
    enabled: true
    observed_min_evidence: 3
    observed_min_batches: 2
    observed_min_span_minutes: 30
    observed_min_confidence: 0.80
    normal_cooldown_hours: 6
    stale_eligible_hours: 72
    max_candidates_per_call: 5
    max_sections_per_call: 2
    pause_after_validation_failures: 3
    pause_hours: 24
```

配置关闭时普通记忆整理继续工作，只停止候选应用；pending 队列保留。

## 测试策略

### 确定性单测

- explicit 双方确认可立即 eligible；单方面表达不能。
- observed 在证据数、batch 数、时间跨度或 confidence 任一不足时不 eligible。
- 相同 evidence hash 不重复计数；相同 subject_key 合并。
- 无 eligible 候选时不调用 maintainer。
- 冷却、explicit 绕过和单 batch 单调用限制。
- 普通输出最多修改两个白名单章节；一次性 migrate_legacy 例外受独立校验。
- 同义无变化转 keep。
- 乐观锁冲突不写入。
- grounding、防丢失和保护锚点失败不写入。
- 队列损坏不覆盖。

### Legacy 迁移测试

- 每个原句只被移动、不被改写或遗漏。
- 新增句子、删除句子、丢失姓名/称呼/数字均拒绝。
- 成功后移除 legacy，渲染缓存与正式 sections 一致。
- 失败时原 V2 文件和 `.bak` 状态不变。

### 集成测试

- 普通 curation 产生候选但不直接写 core。
- explicit 候选在 curation 后进入后台 maintainer。
- observed 达阈值前保持 pending，达阈值后只更新目标章节。
- maintainer 失败不影响 curation 结果和对话。
- unit/UI 全量门禁通过。

### 质量场景

使用完全虚构的短对话 fixture，不使用真实聊天历史：

1. 双方明确确认关系身份。
2. 单次吃醋但第二天恢复，不应更新。
3. 多次表现出新的稳定相处习惯，应逐渐更新。
4. 明确纠正旧称呼或旧边界，应 replace 而非追加。
5. 没有新认识，只重复喜欢，不应改写。

## 分阶段实施

### P3.1：队列与确定性 eligibility

实现 candidate schema、去重合并、explicit/observed 阈值、原子持久化和纯单测。暂不调用模型。

### P3.2：Section patch 存储边界

实现白名单 sections、确定性渲染、乐观锁、备份与 legacy 迁移校验。暂不接 curator。

### P3.3：Maintainer 后台调用

实现最小 prompt、JSON 输出解析、校验、冷却、失败暂停和指标。

### P3.4：Curator 接入

普通 curator 将 core 写入意图改为候选；在 curation 后异步调度 maintainer。使用虚构 fixture 和完整门禁验证。

每阶段独立 TDD、独立审查和独立 commit；最终统一 push。
