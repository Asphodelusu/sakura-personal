# Core Profile Schema V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为常驻档案增加无损 V2 存储信封、严格拒写保护和滚动备份，同时保持所有现有 `content` 消费方兼容。

**Architecture:** 所有兼容逻辑留在 `app/agent/memory.py` 的常驻档案存储边界内。读路径容错且不写磁盘；写路径严格加载并校验 schema，V1 在下一次真实写入时转换为 `schema_version=2 + sections.legacy`，然后通过带备份的原子写保存。

**Tech Stack:** Python 3.11、JSON、pathlib、现有 `MemoryStore` 与 `atomic_write_text`、pytest。

**Spec:** `docs/superpowers/specs/2026-08-24-core-profile-schema-v2-design.md`

## Global Constraints

- 不读取或修改真实 `data/memory/core_profiles.json`；测试必须使用隔离的临时 `base_dir`。
- 不调用模型，不读取聊天历史、mood_state 或其他长期记忆正文。
- 不修改 memory curator 的触发、快照或 add/merge 策略。
- `MemoryStore.core_profile()` 与 `set_core_profile(content, metadata=None)` 的公开调用方式保持兼容。
- 只允许修改 `app/agent/memory.py`、新测试文件和本批次 handoff 文档。
- 实现代理不得 push；最终集成、验证和提交由 Codex 完成。

---

## File Structure

- Modify: `app/agent/memory.py` — V1/V2 读取、严格加载、V2 记录构造、备份写入。
- Create: `tests/unit/test_core_profile_schema_v2.py` — 只使用临时目录的 schema、迁移、拒写和备份测试。
- Create: `docs/agent-handoffs/core-profile-schema-v2/` — Cursor 合同、验收条件和集成证据。

---

### Task 1: V1/V2 兼容读取与严格加载

**Files:**

- Modify: `app/agent/memory.py`
- Create: `tests/unit/test_core_profile_schema_v2.py`

**Interfaces:**

- Produces: `CoreProfileStorageError(RuntimeError)`，供严格更新与删除路径报告拒写。
- Produces: `_core_profile_schema_version(raw: dict[str, Any]) -> int | None`。
- Produces: `_core_profile_content_for_read(raw: dict[str, Any]) -> str`。
- Changes: `MemoryStore._load_core_profiles(*, strict: bool = False) -> dict[str, Any]`。
- Preserves: `MemoryStore.core_profile() -> dict[str, Any] | None` 的外部返回形状。

- [ ] **Step 1: 写 V1 只读不迁移测试**

在新测试文件中用 `tmp_path` 构造 store 和旧格式文件：

```python
def _store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(base_dir=tmp_path, scope_id="Sakura", memory_client=object())


def _core_path(tmp_path: Path) -> Path:
    return StoragePaths(tmp_path).memory_core_profiles()


def test_v1_read_returns_content_without_touching_file(tmp_path: Path) -> None:
    path = _core_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps(
        {"Sakura": {"id": "core_profile:Sakura", "content": "原文", "memory": "原文", "metadata": {}}},
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    path.write_text(original, encoding="utf-8")

    record = _store(tmp_path).core_profile()

    assert record is not None
    assert record["content"] == "原文"
    assert path.read_text(encoding="utf-8") == original
```

- [ ] **Step 2: 写 V2 content 与 sections 降级读取测试**

```python
def test_v2_reads_content_without_rewriting(tmp_path: Path) -> None:
    _write_profile(tmp_path, {"schema_version": 2, "content": "缓存正文", "memory": "缓存正文", "sections": {"legacy": "原文"}, "metadata": {}})
    assert _store(tmp_path).core_profile()["content"] == "缓存正文"


def test_v2_missing_content_renders_sections_read_only(tmp_path: Path) -> None:
    path = _write_profile(tmp_path, {"schema_version": 2, "sections": {"legacy": "第一段", "今の関係": "第二段"}, "metadata": {}})
    before = path.read_bytes()
    record = _store(tmp_path).core_profile()
    assert record is not None
    assert record["content"] == "第一段\n\n第二段"
    assert path.read_bytes() == before
```

- [ ] **Step 3: 写未知版本与损坏 JSON 的只读测试**

```python
def test_unknown_schema_with_content_is_read_only_compatible(tmp_path: Path) -> None:
    _write_profile(tmp_path, {"schema_version": 99, "content": "未来正文", "metadata": {}})
    assert _store(tmp_path).core_profile()["content"] == "未来正文"


def test_corrupt_json_read_returns_none_without_overwrite(tmp_path: Path) -> None:
    path = _core_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    before = path.read_bytes()
    assert _store(tmp_path).core_profile() is None
    assert path.read_bytes() == before
```

- [ ] **Step 4: 运行测试确认 RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_core_profile_schema_v2.py -q
```

Expected: V1 测试可通过；V2 缺 content 降级与未知 schema 契约至少一项失败，证明新读取层尚未实现。

- [ ] **Step 5: 实现最小读取 helper 与严格加载**

在 `memory.py` 增加：

```python
CORE_PROFILE_SCHEMA_VERSION = 2


class CoreProfileStorageError(RuntimeError):
    pass


def _core_profile_schema_version(raw: dict[str, Any]) -> int | None:
    if "schema_version" not in raw:
        return None
    value = raw.get("schema_version")
    return value if isinstance(value, int) and not isinstance(value, bool) else -1


def _core_profile_content_for_read(raw: dict[str, Any]) -> str:
    content = str(raw.get("content") or raw.get("memory") or "").strip()
    if content:
        return content
    sections = raw.get("sections")
    if not isinstance(sections, dict):
        return ""
    return "\n\n".join(
        str(value).strip()
        for value in sections.values()
        if isinstance(value, str) and value.strip()
    )
```

修改 `core_profile()`：复制 raw；用 helper 得到正文；正文为空返回 `None`；将正文补入临时副本的 `content/memory` 后再调用 `_normalize_memory_record`。不得保存文件。

修改 `_load_core_profiles(strict=False)`：

- 文件不存在返回 `{}`；
- 非严格读取保留现有容错；
- 严格读取遇到 I/O、JSON 错误或顶层非对象时抛 `CoreProfileStorageError`；
- 异常信息不得包含档案正文。

- [ ] **Step 6: 运行目标测试确认 GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_core_profile_schema_v2.py -q
```

Expected: Task 1 测试全部通过。

---

### Task 2: V2 写入迁移、未知版本拒写与滚动备份

**Files:**

- Modify: `app/agent/memory.py`
- Modify: `tests/unit/test_core_profile_schema_v2.py`

**Interfaces:**

- Consumes: Task 1 的 `CoreProfileStorageError`、schema/content helper 和严格加载。
- Produces: `_build_core_profile_v2_record(...) -> dict[str, Any]`。
- Changes: `set_core_profile()` 总是写 V2；`delete_core_profile()` 严格校验；`_save_core_profiles()` 开启备份。

- [ ] **Step 1: 写 V1→V2 无损迁移与 metadata 测试**

```python
def test_v1_next_write_migrates_to_v2_and_preserves_created_at(tmp_path: Path) -> None:
    _write_profile(tmp_path, {
        "content": "原文",
        "memory": "原文",
        "metadata": {"created_at": "2026-01-01T00:00:00+08:00", "category": "identity"},
    })

    _store(tmp_path).set_core_profile("原文", {"confidence": 0.9})

    saved = json.loads(_core_path(tmp_path).read_text(encoding="utf-8"))["Sakura"]
    assert saved["schema_version"] == 2
    assert saved["content"] == "原文"
    assert saved["memory"] == "原文"
    assert saved["sections"] == {"legacy": "原文"}
    assert saved["metadata"]["created_at"] == "2026-01-01T00:00:00+08:00"
    assert saved["metadata"]["confidence"] == 0.9
```

- [ ] **Step 2: 写幂等与旧 API 重置正式 sections 测试**

```python
def test_repeated_v2_full_write_keeps_single_legacy_section(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set_core_profile("原文")
    store.set_core_profile("原文")
    saved = _saved_record(tmp_path)
    assert saved["sections"] == {"legacy": "原文"}


def test_full_write_replaces_stale_formal_sections_with_legacy(tmp_path: Path) -> None:
    _write_profile(tmp_path, {
        "schema_version": 2,
        "content": "旧缓存",
        "memory": "旧缓存",
        "sections": {"今の関係": "旧关系", "今の私": "旧自我"},
        "metadata": {},
    })
    _store(tmp_path).set_core_profile("新的整段正文")
    assert _saved_record(tmp_path)["sections"] == {"legacy": "新的整段正文"}
```

- [ ] **Step 3: 写未知/损坏格式拒绝 set 与 delete 测试**

```python
@pytest.mark.parametrize("operation", ["set", "delete"])
def test_unknown_schema_rejects_mutation_without_touching_bytes(tmp_path: Path, operation: str) -> None:
    path = _write_profile(tmp_path, {"schema_version": 99, "content": "未来正文", "metadata": {}})
    before = path.read_bytes()
    store = _store(tmp_path)
    with pytest.raises(CoreProfileStorageError):
        store.set_core_profile("覆盖") if operation == "set" else store.delete_core_profile()
    assert path.read_bytes() == before


@pytest.mark.parametrize("operation", ["set", "delete"])
def test_corrupt_json_rejects_mutation_without_touching_bytes(tmp_path: Path, operation: str) -> None:
    path = _core_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    before = path.read_bytes()
    store = _store(tmp_path)
    with pytest.raises(CoreProfileStorageError):
        store.set_core_profile("覆盖") if operation == "set" else store.delete_core_profile()
    assert path.read_bytes() == before
```

- [ ] **Step 4: 写备份测试**

```python
def test_overwrite_creates_backup_of_previous_file(tmp_path: Path) -> None:
    path = _write_profile(tmp_path, {"content": "旧正文", "memory": "旧正文", "metadata": {}})
    before = path.read_bytes()
    _store(tmp_path).set_core_profile("新正文")
    assert path.with_name(path.name + ".bak").read_bytes() == before


def test_first_write_does_not_create_empty_backup(tmp_path: Path) -> None:
    path = _core_path(tmp_path)
    _store(tmp_path).set_core_profile("正文")
    assert path.exists()
    assert not path.with_name(path.name + ".bak").exists()
```

- [ ] **Step 5: 运行新增测试确认 RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_core_profile_schema_v2.py -q
```

Expected: 当前 set 仍写 V1、未知版本仍可能被覆盖、备份不存在，因此相应测试失败。

- [ ] **Step 6: 实现 V2 记录构造与严格写入**

实现 helper，签名固定为：

```python
def _build_core_profile_v2_record(
    *,
    scope_id: str,
    content: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": _core_profile_id(scope_id),
        "schema_version": CORE_PROFILE_SCHEMA_VERSION,
        "content": content,
        "memory": content,
        "sections": {"legacy": content},
        "metadata": metadata,
    }
```

在 `set_core_profile()` 中：

- 调用 `_load_core_profiles(strict=True)`；
- 目标旧记录若存在但不是对象则抛 `CoreProfileStorageError`；
- schema 仅允许 V1（无版本）和 V2；其他值拒绝；
- 保留现有 metadata 合并和时间逻辑；
- 用 helper 构造记录。

在 `delete_core_profile()` 中执行同样的严格加载和版本校验。

将 `_save_core_profiles()` 改为：

```python
atomic_write_text(
    path,
    json.dumps(profiles, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
    backup=True,
)
```

- [ ] **Step 7: 运行目标测试确认 GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_core_profile_schema_v2.py tests/unit/test_memory_curator.py -q
```

Expected: 全部通过。

---

### Task 3: 集成验证与文件范围审查

**Files:**

- Verify only: `app/agent/memory.py`
- Verify only: `tests/unit/test_core_profile_schema_v2.py`
- Modify: `docs/agent-handoffs/core-profile-schema-v2/integration-notes.md`

**Interfaces:**

- Consumes: Task 1/2 的最终 diff。
- Produces: 可复现的测试、范围和风险证据；不增加产品行为。

- [ ] **Step 1: 检查改动范围与私有数据**

Run:

```powershell
git status --short
git diff -- app/agent/memory.py tests/unit/test_core_profile_schema_v2.py
```

Expected: 实现代理只修改合同文件；`data/`、`characters/`、日志和 `.gitignore` 未被纳入本任务。

- [ ] **Step 2: 运行目标门禁**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_core_profile_schema_v2.py tests/unit/test_memory_curator.py -q
git diff --check
```

Expected: 全部通过，diff check 无错误。

- [ ] **Step 3: 运行完整分组门禁**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit -q
.\.venv\Scripts\python.exe -m pytest tests/ui -q
```

Expected: 无失败；已知 skip 可保留并记录数量。

- [ ] **Step 4: 临时 fixture 做正文与备份核对**

使用测试中的临时目录完成一次 V1→V2：断言迁移后 `content` 与 V1 原文相同、`sections.legacy` 相同、`.bak` 字节等于迁移前文件。不得对真实 `data/memory/core_profiles.json` 执行迁移。

- [ ] **Step 5: 完成独立只读审查**

审查重点：读路径无副作用、严格写路径不会把损坏文件当空库、未知版本拒写、`backup=True` 只影响已存在文件、错误日志不泄露正文。

- [ ] **Step 6: Codex 集成提交**

仅在验证和审查通过后执行：

```powershell
git add -- app/agent/memory.py tests/unit/test_core_profile_schema_v2.py docs/agent-handoffs/core-profile-schema-v2 docs/superpowers/plans/2026-08-24-core-profile-schema-v2.md
git commit -m "feat: add lossless core profile schema v2"
```

不得添加 `.gitignore`，不得 push。
