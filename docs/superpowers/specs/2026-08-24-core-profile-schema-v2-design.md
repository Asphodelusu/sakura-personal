# Core Profile Schema V2 — 无损兼容迁移设计

## 背景

当前 `data/memory/core_profiles.json` 以角色 scope 为键，每个常驻档案是一条扁平记录：

```json
{
  "Sakura": {
    "id": "core_profile:Sakura",
    "content": "整段第一人称档案",
    "memory": "整段第一人称档案",
    "metadata": {}
  }
}
```

读侧和整理器都依赖 `content`。这保证了兼容性，但不支持后续按「今の関係」「今の私」等章节做小范围 patch；模型只能重写整段，成本高且容易遗漏。

本阶段只建立无损存储兼容层，不调用模型、不分析聊天历史，也不改变现有档案措辞。

## 目标

1. 旧格式在升级前后都能正常读取。
2. 旧记录只在下一次真实写入时迁移，不因普通读取偷偷修改磁盘。
3. 迁移逐字保留当前 `content`，不做语义拆分。
4. V2 为未来 section patch 提供稳定容器，同时继续向旧消费者暴露 `content`。
5. 写入前保留上一版本备份；损坏或未知版本不得被静默覆盖。
6. 重复保存是幂等的，不重复包裹或改变正文。

## 非目标

- 不实现独立 `CoreProfileMaintainer`。
- 不自动拆分「今の関係」「今の私」等正式章节。
- 不改变记忆整理触发、light/full 快照或 add/merge 策略。
- 不修改当前真实常驻档案正文。
- 不建立多版本历史；本阶段只使用滚动 `.bak` 保存上一版本。

## V2 记录格式

顶层仍以 scope 为键，避免改变多角色文件布局。每条 V2 记录增加 `schema_version` 与 `sections`：

```json
{
  "Sakura": {
    "id": "core_profile:Sakura",
    "schema_version": 2,
    "content": "原正文，逐字保留",
    "memory": "原正文，逐字保留",
    "sections": {
      "legacy": "原正文，逐字保留"
    },
    "metadata": {
      "layer": "core_profile",
      "scope": "Sakura"
    }
  }
}
```

约束：

- `schema_version` 必须为整数 `2`。
- `sections` 必须为字符串到非空字符串的映射。
- 旧记录迁移时只生成 `sections.legacy`，值等于写入后的完整 `content`。
- `content` 是当前兼容读取与提示注入使用的渲染缓存。
- `memory` 与 `content` 保持一致，兼容现有归一化逻辑。
- V2 的正式章节名由后续阶段定义；P2 不推断章节含义。

## 读取行为

### 旧格式

没有 `schema_version` 的记录按 V1 读取。`core_profile()` 返回的规范化记录与当前行为一致，不写回文件。

### V2 格式

优先使用非空 `content`。如果 `content` 意外缺失但 `sections` 合法，则按映射插入顺序拼接非空 section 文本作为只读降级结果，并记录诊断日志；本阶段不自动修复磁盘。

### 未知版本

`schema_version > 2` 或无法识别的版本：

- 读取时若有合法 `content`，允许只读兼容并记录未知版本日志；没有合法正文则返回 `None`。
- 任何更新或删除操作必须拒绝，防止旧代码覆盖未来格式。

### 损坏 JSON 或结构

- 普通读取保持容错：记录错误并返回无档案结果，不让桌宠启动失败。
- 更新与删除使用严格加载；JSON 损坏、顶层不是对象、目标 scope 记录不是对象时抛出明确存储异常，不得把文件当空字典覆盖。

## 写入与迁移

保留 `set_core_profile(content, metadata=None)` 的公开调用方式。

1. 严格加载整个 `core_profiles.json`。
2. 校验目标 scope 的版本可写。
3. 将传入正文 `strip()` 后作为新的完整正文。
4. 生成 V2 记录：
   - `schema_version = 2`
   - `content = text`
   - `memory = text`
   - `sections = {"legacy": text}`
   - metadata 延续现有合并、时间与 layer/scope 规则
5. 使用 `atomic_write_text(..., backup=True)` 保存整个文件。

当未来正式章节已存在，而旧的整段写入 API 再次被调用时，`sections` 必须重置为 `{"legacy": text}`，不能保留与新正文不一致的旧章节。后续 section patch 将使用独立 API 显式提交 sections，不复用整段写入语义。

## 删除行为

`delete_core_profile()` 同样使用严格加载并校验版本。删除已知 V1/V2 记录后，使用带备份的原子保存。未知版本或损坏文件拒绝删除。

## 备份与恢复

- `_save_core_profiles()` 对已存在的目标文件调用 `atomic_write_text(..., backup=True)`。
- 备份路径为 `core_profiles.json.bak`，滚动覆盖，只保留保存前一版。
- 新建文件时不产生空备份。
- 备份失败沿用原子写工具现有策略：记录日志但不阻断正常保存。
- 本阶段不自动从 `.bak` 恢复；恢复由用户或后续维护工具显式执行。

## 内部边界

建议在 `app/agent/memory.py` 内增加小型纯函数，避免把迁移逻辑散在存储方法中：

- `_core_profile_schema_version(raw) -> int | None`
- `_core_profile_content_for_read(raw) -> str`
- `_build_core_profile_v2_record(...) -> dict[str, Any]`
- 严格加载可通过 `_load_core_profiles(strict: bool = False)` 或等价的独立私有方法实现。

不新增独立模块，不改变 `MemoryStore.core_profile()` 的返回形状，也不修改向量库。

## 错误处理与日志

- 严格写路径错误必须包含文件类型和拒写原因，但日志中不打印完整档案正文。
- 只读降级记录：损坏 JSON、未知 schema、V2 content 缺失。
- 不捕获并吞掉严格写路径异常；调用方应看到失败，避免误报已保存。

## 测试设计

所有测试使用隔离的临时 `base_dir`，不得读取真实 `data/memory/core_profiles.json`。

1. V1 只读：读取后返回原正文，磁盘字节不变。
2. V1 首次写入迁移：产生 V2、`sections.legacy == content`，原 metadata 创建时间保留。
3. V2 幂等写入：重复相同写入不产生嵌套 legacy，正文不变。
4. 兼容读取：V2 正常 content 可被 `core_profile()` 返回。
5. 只读降级：V2 缺 content 时可从合法 sections 得到正文，磁盘不变。
6. 未知版本：有 content 时可读，但 set/delete 均拒绝。
7. 损坏 JSON：读取返回 None；set/delete 拒绝且原文件字节不变。
8. 备份：覆盖保存后 `.bak` 等于保存前文件；新文件无 `.bak`。
9. 整段覆写：已有正式 sections 时调用旧 API，sections 重置为单一 legacy，避免缓存与章节分叉。
10. 回归：现有 core profile、memory curator、unit 与 UI 测试全部通过。

## 实施阶段

### P2.1：兼容读取与严格加载

先锁定 V1/V2/未知/损坏格式的读取与拒写契约，不改变真实文件。

### P2.2：无损写入迁移与备份

让 `set_core_profile`、`delete_core_profile` 写出 V2，并启用滚动备份。

### P2.3：集成验证

运行目标测试、完整 unit/UI 门禁，并对临时 fixture 做一次 V1→V2 字节级正文核对。真实 Sakura 档案只在应用下一次确实更新时自然迁移。

## 后续阶段接口

P3 才增加 section patch API 和独立 maintainer。P3 必须：

- 用 evidence 更新单个正式章节；
- 重新渲染 `content/memory`；
- 保留乐观锁与回滚记录；
- 将 `legacy` 逐步迁移而非一次性语义重写。

这些能力不属于 P2，避免格式升级同时改变角色关系语义。
