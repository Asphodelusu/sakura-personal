# Sakura Relationship Initiative Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Sakura independent in-turn (A) and cross-turn (B) relationship initiative with default `natural` expression bias, without a hard intimacy ceiling and without auto-enabling `贴紧`.

**Architecture:** L1 is an optional per-character `relationship_guide.md` injected as a static `persona.relationship_guide` section when A is on. B reuses the `ProactiveObserver` thread, UI busy gate, and playback callbacks, but adds a no-screenshot / no-UIA / no-VLM `relationship_timer` with its own silence and cooldown. Runtime only grants a chance to speak; it never ranks how far she may go, and never sends `贴紧`.

**Tech Stack:** Python 3.11+, PySide6, pytest, existing `PromptSection` / `ProactiveObserver` / `AppSettingsService`.

**Spec:** `docs/superpowers/specs/2026-08-24-sakura-relationship-initiative-design.md`

## Global Constraints

- Use `.\.venv\Scripts\python.exe` for every Python / pytest command.
- Initiative has no hard intimacy ceiling and never auto-enables `贴紧` / `intimacy_mode_state`.
- Closing A must not inject any negative “you may not take initiative” text.
- Closing B must not affect screen-observer timer/content/window behavior.
- Do not rewrite `data/intimacy_guide.txt`, `card.md`, memory, mood, or core-profile storage.
- Do not edit the live `data/config/system_config.yaml`; missing `relationship_initiative` uses code defaults.
- `characters/` and `data/intimacy_guide.txt` are ignored/private. Do not force-add ignored files.
- Preserve the pre-existing `.gitignore` working-tree state and unrelated dirty files.
- Do not modify runtime chat history, memories, mood, logs, API keys, or user config except by writing new code that reads them.
- No extra classifier LLM call. A is static injection only. B is one decision-LLM call, then the existing playback chain.
- Agents may create local commits of declared files only. Never push.

## Locked Decisions

These close spec ambiguities so executors do not fork the design:

1. **Settings UI is out of scope.** YAML + `AppSettingsService` load/save/hot-apply is enough. No Tauri settings page in this plan.
2. **B playback reuses `on_speak` → `_show_proactive_comment` → `_consume_agent_result`.** It does not start a second `ChatWorker`. That matches “reuse playback callbacks”, “no extra classifier”, and “do not bypass the reply/TTS/history chain”.
3. **`ProactiveObserver(relationship=None)` keeps B collection off.** Production `PetWindow` always passes loaded settings (defaults A/B on). This preserves existing observer unit tests that construct a bare observer.
4. **Start the observer thread if screen `proactive.enabled` OR B `proactive_enabled`.** Screen capture/VLM still run only for screen triggers.
5. **B is blocked while L2 is active** because `_proactive_observer_busy_reason()` already returns `rhythm_focus`. Incomplete intimacy continuation and an active `贴紧` session both count as “another ongoing intimate reply”.
6. **B does not use the observer’s own-process screenshot skip.** Own-window focus is not a B gate; `input_focused` / TTS / worker busy still are.
7. **Independent cooldowns:** B speak sets `_last_relationship_spoken_at` only. It must not write `_last_proactive_at`. Screen silent cooldown must not suppress B, and B silent cooldown must not suppress screen.
8. **Mixed triggers:** if any screen trigger is ready in the same loop tick, run only `_do_evaluation` (VLM path) and pass `relationship_motive=True` into the existing decision prompt. Do not run `_do_relationship_evaluation` in that tick.
9. **B failure silent cooldown is 300s**, matching `ProactiveConfig.silent_eval_cooldown_seconds`. Not a new YAML key.
10. **L0 `card.md` is not edited.** Prior card/guards work already carries personality and relationship-fact rules. This plan lands L1 + A/B runtime.
11. **Listed-but-missing `relationship_guide.md` does not fail character load** (unlike `system_guards.md`). Warn once and degrade. Archive import still requires a listed file to exist inside the zip.

## File Structure

- Create: `app/config/relationship_initiative.py` — frozen settings, bias copy, normalization, gate reasons, decision instruction.
- Modify: `app/config/settings_service.py` — load/save `relationship_initiative` from `system_config.yaml`.
- Modify: `app/config/character_loader.py` — optional `relationship_guide_path` on `CharacterProfile`.
- Modify: `app/config/character_archive.py` — export/import/validate optional guide like `system_guards`.
- Modify: `app/agent/runtime.py` — load guide on init / `update_character`; hold current A/B settings.
- Modify: `app/agent/prompt_builder.py` — inject `persona.relationship_guide` when A is on and guide exists.
- Modify: `app/perception/observer.py` — `relationship_timer`, no-VLM eval, independent cooldown, motive flag, cancel generation.
- Modify: `app/perception/__init__.py` — re-export payload `source` if needed (no new public type required).
- Modify: `app/ui/pet_window.py` — wire settings, facts provider, generation cancel, character-switch reset, B-enabled observer start, playback source.
- Modify: `app/core/gui_log.py` and `app/ui/log_window.py` — `RelationshipInitiative` GUI category.
- Create: `tests/unit/test_relationship_initiative_config.py`
- Create: `tests/unit/test_relationship_guide_loader.py`
- Modify: `tests/unit/test_character_archive.py` — optional guide roundtrip.
- Create: `tests/unit/test_relationship_guide_prompt.py`
- Create: `tests/unit/test_relationship_timer.py`
- Create: `tests/unit/test_relationship_initiative_playback.py`
- Create locally (ignored): `characters/Sakura/relationship_guide.md` — do not `git add -f`.
- Create: `tests/unit/test_relationship_guide_content.py` — asserts against tmp fixtures plus optional live package if present.

---

### Task 1: Relationship initiative config model

**Files:**
- Create: `app/config/relationship_initiative.py`
- Modify: `app/config/settings_service.py`
- Test: `tests/unit/test_relationship_initiative_config.py`

**Interfaces:**
- Produces: `EXPRESSION_BIASES = ("restrained", "natural", "expressive")`.
- Produces: `RELATIONSHIP_GATE_REASONS = ("disabled", "busy", "silence", "cooldown", "continuation", "eligible")`.
- Produces: `RELATIONSHIP_SILENT_COOLDOWN_SECONDS = 300.0`.
- Produces: `RELATIONSHIP_GUIDE_TOKEN_BUDGET = 1600`.
- Produces: `RelationshipInitiativeSettings` frozen dataclass with `normalized()`.
- Produces: `expression_bias_guidance(bias: str) -> str`.
- Produces: `AppSettingsService.load_relationship_initiative_settings() -> RelationshipInitiativeSettings`.
- Produces: `AppSettingsService.save_relationship_initiative_settings(settings: RelationshipInitiativeSettings) -> None`.
- Consumes: existing `_bool_value`, `_int_value`, `_system_section`, `load_yaml_mapping`, `save_yaml_mapping`.

- [ ] **Step 1: Write the failing config tests**

Create `tests/unit/test_relationship_initiative_config.py`:

```python
from pathlib import Path

from app.config.relationship_initiative import (
    EXPRESSION_BIASES,
    RelationshipInitiativeSettings,
    expression_bias_guidance,
)
from app.config.settings_service import AppSettingsService


def _service(tmp_path: Path) -> AppSettingsService:
    service = AppSettingsService(tmp_path)
    service.system_config_path.parent.mkdir(parents=True, exist_ok=True)
    return service


def test_defaults_enable_ab_and_natural() -> None:
    settings = RelationshipInitiativeSettings().normalized()
    assert settings.in_turn_enabled is True
    assert settings.proactive_enabled is True
    assert settings.expression_bias == "natural"
    assert settings.proactive_cooldown_seconds == 3600
    assert settings.proactive_min_silence_seconds == 300
    assert EXPRESSION_BIASES == ("restrained", "natural", "expressive")


def test_missing_yaml_section_uses_defaults(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.system_config_path.write_text("ui: {}\n", encoding="utf-8")
    loaded = service.load_relationship_initiative_settings()
    assert loaded == RelationshipInitiativeSettings().normalized()


def test_unknown_bias_and_invalid_times_normalize(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.system_config_path.write_text(
        """
relationship_initiative:
  in_turn_enabled: true
  proactive_enabled: false
  expression_bias: spicy
  proactive_cooldown_seconds: -9
  proactive_min_silence_seconds: "nope"
""".lstrip(),
        encoding="utf-8",
    )
    loaded = service.load_relationship_initiative_settings()
    assert loaded.proactive_enabled is False
    assert loaded.expression_bias == "natural"
    assert loaded.proactive_cooldown_seconds == 3600
    assert loaded.proactive_min_silence_seconds == 300


def test_times_clamp_to_safe_range() -> None:
    settings = RelationshipInitiativeSettings(
        proactive_cooldown_seconds=10,
        proactive_min_silence_seconds=10_000,
    ).normalized()
    assert settings.proactive_cooldown_seconds == 60
    assert settings.proactive_min_silence_seconds == 3600


def test_bias_guidance_has_no_content_blacklist() -> None:
    for bias in EXPRESSION_BIASES:
        text = expression_bias_guidance(bias)
        assert f"表达倾向：{bias}" in text
        assert "不得直接" not in text
        assert "最多只能" not in text
        assert "禁止" not in text
        assert "黑名单" not in text
    natural = expression_bias_guidance("natural")
    assert "平时克制" in natural
    assert "可以直接" in natural
    assert expression_bias_guidance("weird") == expression_bias_guidance("natural")


def test_save_roundtrip_preserves_unknown_sibling_keys(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.system_config_path.write_text("proactive:\n  enabled: true\n", encoding="utf-8")
    service.save_relationship_initiative_settings(
        RelationshipInitiativeSettings(
            in_turn_enabled=False,
            proactive_enabled=True,
            expression_bias="restrained",
            proactive_cooldown_seconds=1800,
            proactive_min_silence_seconds=120,
        )
    )
    text = service.system_config_path.read_text(encoding="utf-8")
    assert "proactive:" in text
    loaded = service.load_relationship_initiative_settings()
    assert loaded.in_turn_enabled is False
    assert loaded.expression_bias == "restrained"
    assert loaded.proactive_cooldown_seconds == 1800
    assert loaded.proactive_min_silence_seconds == 120
```

- [ ] **Step 2: Run the new tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_relationship_initiative_config.py -q
```

Expected: FAIL because `app.config.relationship_initiative` and the two `AppSettingsService` methods do not exist.

- [ ] **Step 3: Implement the config module and settings load/save**

Create `app/config/relationship_initiative.py` with this exact behavior:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

EXPRESSION_BIASES = ("restrained", "natural", "expressive")
DEFAULT_EXPRESSION_BIAS = "natural"
DEFAULT_PROACTIVE_COOLDOWN_SECONDS = 3600
DEFAULT_PROACTIVE_MIN_SILENCE_SECONDS = 300
RELATIONSHIP_SILENT_COOLDOWN_SECONDS = 300.0
RELATIONSHIP_GUIDE_TOKEN_BUDGET = 1600
COOLDOWN_MIN_SECONDS = 60
COOLDOWN_MAX_SECONDS = 86400
SILENCE_MIN_SECONDS = 30
SILENCE_MAX_SECONDS = 3600
RELATIONSHIP_GATE_REASONS = (
    "disabled",
    "busy",
    "silence",
    "cooldown",
    "continuation",
    "eligible",
)

_BIAS_GUIDANCE = {
    "restrained": (
        "表达倾向：restrained。已经产生的靠近或亲密想法，多数时候先放在心里或只露出一点；"
        "只有当下关系、气氛和对方反应都明确支持时才说出口。不增加任何禁止清单，也不缩小可做的事。"
    ),
    "natural": (
        "表达倾向：natural。平时克制；认定关系且当下确实起意后可以直接。"
        "允许从轻触、邀请陪伴到直接表达想要，由她自己判断；不要把已经产生的欲望系统降级成只能拥抱或索吻。"
    ),
    "expressive": (
        "表达倾向：expressive。已经产生的想法更容易说出来或做出来，"
        "但仍必须是她此刻真实会做的事，不为了证明主动而制造欲望。不增加任何禁止清单。"
    ),
}


def expression_bias_guidance(bias: str) -> str:
    key = str(bias or "").strip().lower()
    return _BIAS_GUIDANCE.get(key, _BIAS_GUIDANCE[DEFAULT_EXPRESSION_BIAS])


@dataclass(frozen=True)
class RelationshipInitiativeSettings:
    in_turn_enabled: bool = True
    proactive_enabled: bool = True
    expression_bias: str = DEFAULT_EXPRESSION_BIAS
    proactive_cooldown_seconds: int = DEFAULT_PROACTIVE_COOLDOWN_SECONDS
    proactive_min_silence_seconds: int = DEFAULT_PROACTIVE_MIN_SILENCE_SECONDS

    def normalized(self) -> "RelationshipInitiativeSettings":
        bias = str(self.expression_bias or "").strip().lower()
        if bias not in EXPRESSION_BIASES:
            bias = DEFAULT_EXPRESSION_BIAS
        return RelationshipInitiativeSettings(
            in_turn_enabled=bool(self.in_turn_enabled),
            proactive_enabled=bool(self.proactive_enabled),
            expression_bias=bias,
            proactive_cooldown_seconds=_clamp_int(
                self.proactive_cooldown_seconds,
                DEFAULT_PROACTIVE_COOLDOWN_SECONDS,
                COOLDOWN_MIN_SECONDS,
                COOLDOWN_MAX_SECONDS,
            ),
            proactive_min_silence_seconds=_clamp_int(
                self.proactive_min_silence_seconds,
                DEFAULT_PROACTIVE_MIN_SILENCE_SECONDS,
                SILENCE_MIN_SECONDS,
                SILENCE_MAX_SECONDS,
            ),
        )


def settings_from_mapping(raw: Mapping[str, Any] | None) -> RelationshipInitiativeSettings:
    source = dict(raw) if isinstance(raw, Mapping) else {}
    return RelationshipInitiativeSettings(
        in_turn_enabled=_as_bool(source.get("in_turn_enabled"), True),
        proactive_enabled=_as_bool(source.get("proactive_enabled"), True),
        expression_bias=str(source.get("expression_bias", DEFAULT_EXPRESSION_BIAS) or DEFAULT_EXPRESSION_BIAS),
        proactive_cooldown_seconds=_as_int(
            source.get("proactive_cooldown_seconds"),
            DEFAULT_PROACTIVE_COOLDOWN_SECONDS,
        ),
        proactive_min_silence_seconds=_as_int(
            source.get("proactive_min_silence_seconds"),
            DEFAULT_PROACTIVE_MIN_SILENCE_SECONDS,
        ),
    ).normalized()
```

Implement these helpers in the same module (do not import private helpers from `settings_service`):

```python
def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    if parsed < 0:
        return default
    return parsed


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    parsed = _as_int(value, default)
    return max(minimum, min(maximum, parsed))
```

Non-numeric or negative times fall back to the spec default, then clamp into the safe range. `10` is numeric and non-negative, so cooldown becomes `60`, not `3600`.

Add to `AppSettingsService` next to the backchannel loaders:

```python
def load_relationship_initiative_settings(self):
    from app.config.relationship_initiative import settings_from_mapping

    return settings_from_mapping(self._system_section("relationship_initiative"))

def save_relationship_initiative_settings(self, settings) -> None:
    from app.config.relationship_initiative import RelationshipInitiativeSettings

    normalized = (
        settings.normalized()
        if isinstance(settings, RelationshipInitiativeSettings)
        else RelationshipInitiativeSettings().normalized()
    )
    data = load_yaml_mapping(self.system_config_path)
    data["relationship_initiative"] = {
        "in_turn_enabled": bool(normalized.in_turn_enabled),
        "proactive_enabled": bool(normalized.proactive_enabled),
        "expression_bias": normalized.expression_bias,
        "proactive_cooldown_seconds": int(normalized.proactive_cooldown_seconds),
        "proactive_min_silence_seconds": int(normalized.proactive_min_silence_seconds),
    }
    save_yaml_mapping(self.system_config_path, data)
```

Do not write into the live repo `data/config/system_config.yaml`.

- [ ] **Step 4: Run tests and verify GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_relationship_initiative_config.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add app/config/relationship_initiative.py app/config/settings_service.py tests/unit/test_relationship_initiative_config.py
git commit -m "feat: add relationship initiative config defaults"
```

---

### Task 2: Optional relationship guide loading and archive

**Files:**
- Modify: `app/config/character_loader.py:80-88,303-363,681-689`
- Modify: `app/config/character_archive.py:255-271,501-505,627-636,713-717`
- Test: `tests/unit/test_relationship_guide_loader.py`
- Test: `tests/unit/test_character_archive.py`

**Interfaces:**
- Produces: `CharacterProfile.relationship_guide_path: Path | None = None`.
- Produces: `load_relationship_guide(path: Path | None) -> str` — empty on missing/OSError, never raises.
- Produces: `_resolve_optional_relationship_guide(package_dir: Path, raw: Any) -> Path | None` — listed-but-missing logs a warning and returns `None`; unlisted default is `package_dir / "relationship_guide.md"` if that file exists.
- Produces: archive manifest key `character.relationship_guide` mirroring `character.system_guards`.
- Preserves: missing guide must not fail `CharacterRegistry` load. Missing `system_guards` behavior unchanged.

- [ ] **Step 1: Write failing loader and archive tests**

Create `tests/unit/test_relationship_guide_loader.py`:

```python
import json
from pathlib import Path

from app.config.character_loader import (
    CharacterRegistry,
    load_relationship_guide,
)


def _write_package(root: Path, *, with_guide: bool, listed: bool = True, missing_listed: bool = False) -> Path:
    package = root / "characters" / "demo"
    package.mkdir(parents=True)
    (package / "card.md").write_text("card", encoding="utf-8")
    portraits = package / "portraits"
    portraits.mkdir()
    (portraits / "default.png").write_bytes(b"png")
    if with_guide and not missing_listed:
        (package / "relationship_guide.md").write_text("# 关系演出\n主动靠近。", encoding="utf-8")
    manifest = {
        "id": "demo",
        "display_name": "Demo",
        "card": "card.md",
        "portrait": {"default": "portraits/default.png", "expressions": {}},
    }
    if listed:
        manifest["relationship_guide"] = "relationship_guide.md"
    (package / "character.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_load_relationship_guide_reads_text(tmp_path: Path) -> None:
    path = tmp_path / "relationship_guide.md"
    path.write_text("keep close", encoding="utf-8")
    assert load_relationship_guide(path) == "keep close"
    assert load_relationship_guide(None) == ""
    assert load_relationship_guide(tmp_path / "missing.md") == ""


def test_default_path_loads_without_manifest_key(tmp_path: Path) -> None:
    root = _write_package(tmp_path, with_guide=True, listed=False)
    profile = CharacterRegistry(root).get("demo")
    assert profile.relationship_guide_path is not None
    assert profile.relationship_guide_path.name == "relationship_guide.md"


def test_missing_guide_does_not_fail_character_load(tmp_path: Path) -> None:
    root = _write_package(tmp_path, with_guide=False, listed=False)
    profile = CharacterRegistry(root).get("demo")
    assert profile.relationship_guide_path is None
    assert load_relationship_guide(profile.relationship_guide_path) == ""


def test_listed_missing_guide_degrades_instead_of_raising(tmp_path: Path) -> None:
    root = _write_package(tmp_path, with_guide=True, listed=True, missing_listed=True)
    profile = CharacterRegistry(root).get("demo")
    assert profile.relationship_guide_path is None
```

Add to `tests/unit/test_character_archive.py` (keep existing tests; add these):

```python
def test_character_archive_preserves_optional_relationship_guide() -> None:
    root = _runtime_root("relationship_guide")
    source_root = root / "source"
    profile = _build_character_package(source_root)
    guide = profile.package_dir / "relationship_guide.md"
    guide.write_text("主动靠近，不复读台词库。", encoding="utf-8")
    manifest_path = profile.package_dir / "character.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["relationship_guide"] = "relationship_guide.md"
    manifest_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    profile = CharacterRegistry(source_root).get("demo")
    archive_path = root / "demo.char"
    export_character_archive(profile, archive_path)
    with zipfile.ZipFile(archive_path, "r") as zf:
        manifest = json.loads(zf.read("manifest.json"))
        names = set(zf.namelist())
    assert manifest["character"]["relationship_guide"] == "character/relationship_guide.md"
    assert "character/relationship_guide.md" in names
    imported = import_character_archive(archive_path, source_root)
    loaded = CharacterRegistry(source_root).get(imported.character_id)
    assert loaded.relationship_guide_path is not None
    assert loaded.relationship_guide_path.read_text(encoding="utf-8") == "主动靠近，不复读台词库。"


def test_character_archive_without_relationship_guide_still_roundtrips() -> None:
    root = _runtime_root("no_relationship_guide")
    profile = _build_character_package(root / "source")
    archive_path = root / "demo.char"
    export_character_archive(profile, archive_path)
    result = import_character_archive(archive_path, root / "source")
    loaded = CharacterRegistry(root / "source").get(result.character_id)
    assert loaded.relationship_guide_path is None
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_relationship_guide_loader.py tests\unit\test_character_archive.py::test_character_archive_preserves_optional_relationship_guide tests\unit\test_character_archive.py::test_character_archive_without_relationship_guide_still_roundtrips -q
```

Expected: FAIL on missing `load_relationship_guide` / `relationship_guide_path` / archive key.

- [ ] **Step 3: Implement loader and archive wiring**

On `CharacterProfile`, add after `system_guards_path`:

```python
relationship_guide_path: Path | None = None
```

In `_load_profile`, after resolving system guards:

```python
relationship_guide_path = _resolve_optional_relationship_guide(
    package_dir, raw_data.get("relationship_guide")
)
```

Pass it into the `CharacterProfile(...)` constructor.

Add:

```python
def load_relationship_guide(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _resolve_optional_relationship_guide(package_dir: Path, raw: Any) -> Path | None:
    """解析可选关系演出参考；缺失不让角色包加载失败。"""
    from loguru import logger

    if isinstance(raw, str) and raw.strip():
        path = _resolve_package_path(package_dir, raw)
        if not path.exists():
            logger.warning("角色 relationship_guide 不存在，降级为无 L1：{}", path)
            return None
        return path
    default_path = package_dir / "relationship_guide.md"
    return default_path if default_path.exists() else None
```

In `export_character_archive`, after the `system_guards` manifest block:

```python
if profile.relationship_guide_path is not None:
    character_manifest["relationship_guide"] = archive_path_for_resource(
        profile.relationship_guide_path,
        "relationship_guide",
    )
```

Mirror the same optional-string pattern used for `system_guards` in `_normalize_character_data`, `_validate_referenced_files`, and `_package_character_data`. Validation label: `关系演出参考`.

Because export already `rglob`s the package directory, a guide file inside the package is packed automatically once the manifest points at it.

- [ ] **Step 4: Run tests and verify GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_relationship_guide_loader.py tests\unit\test_character_archive.py tests\unit\test_character_portrait_mapping.py tests\unit\test_system_guards_prompt.py -q
```

Expected: PASS. Existing packages without a guide still load.

- [ ] **Step 5: Commit**

```powershell
git add app/config/character_loader.py app/config/character_archive.py tests/unit/test_relationship_guide_loader.py tests/unit/test_character_archive.py
git commit -m "feat: load and archive optional relationship guides"
```

---

### Task 3: A — static `persona.relationship_guide` injection

**Files:**
- Modify: `app/agent/runtime.py:173-308`
- Modify: `app/agent/prompt_builder.py:53-79,184-205`
- Test: `tests/unit/test_relationship_guide_prompt.py`
- Test: `tests/unit/test_intimacy_mode.py` (add one non-enable assertion only if needed; prefer the new file)

**Interfaces:**
- Consumes: Task 1 settings, Task 2 `load_relationship_guide`.
- Produces: `AgentRuntime._relationship_guide: str`.
- Produces: `AgentRuntime._relationship_settings: RelationshipInitiativeSettings`.
- Produces: `AgentRuntime.set_relationship_initiative(settings, guide_text: str) -> None`.
- Produces: `AgentRuntimePromptMixin._build_relationship_guide_section() -> PromptSection | None`.
- Produces: section id `persona.relationship_guide` with `source="character"`, `sensitivity="private"`, `cache_scope="static"`, `token_budget=RELATIONSHIP_GUIDE_TOKEN_BUDGET`.
- Produces: debug log category `RelationshipInitiative` event `A 注入` with `injected`, `chars`, `tokens`, `bias` — never the guide body.
- Preserves: `_build_intimacy_section` and `贴紧` exact-entry behavior.

- [ ] **Step 1: Write failing injection tests**

Create `tests/unit/test_relationship_guide_prompt.py`:

```python
from types import SimpleNamespace

from app.agent.builtin_tools import intimacy_mode_state
from app.agent.prompt_builder import AgentRuntimePromptMixin
from app.config.relationship_initiative import (
    RELATIONSHIP_GUIDE_TOKEN_BUDGET,
    RelationshipInitiativeSettings,
    expression_bias_guidance,
)
from app.llm.prompts.runtime import estimate_prompt_tokens
from app.llm.prompts.types import PromptSection


class _Runtime(AgentRuntimePromptMixin):
    def __init__(self, guide: str, settings: RelationshipInitiativeSettings) -> None:
        self.system_prompt = "【人格设定】\n她是夜乃桜。"
        self.prompt_patches = []
        self._relationship_guide = guide
        self._relationship_settings = settings.normalized()


def test_enabled_guide_injects_named_section() -> None:
    runtime = _Runtime("安心时可以主动靠近。", RelationshipInitiativeSettings())
    section = runtime._build_relationship_guide_section()
    assert section is not None
    assert section.section_id == "persona.relationship_guide"
    assert section.cache_scope == "static"
    assert section.sensitivity == "private"
    assert section.token_budget == RELATIONSHIP_GUIDE_TOKEN_BUDGET
    assert "安心时可以主动靠近。" in section.body
    assert expression_bias_guidance("natural") in section.body
    sections = runtime._persona_sections()
    ids = [item.section_id for item in sections]
    assert "persona.character" in ids
    assert "persona.relationship_guide" in ids
    assert ids.index("persona.character") < ids.index("persona.relationship_guide")


def test_disabled_or_missing_does_not_inject_negative_limit() -> None:
    off = _Runtime("安心时可以主动靠近。", RelationshipInitiativeSettings(in_turn_enabled=False))
    missing = _Runtime("", RelationshipInitiativeSettings(in_turn_enabled=True))
    assert off._build_relationship_guide_section() is None
    assert missing._build_relationship_guide_section() is None
    for runtime in (off, missing):
        blob = "\n".join(section.body for section in runtime._persona_sections())
        assert "不允许主动" not in blob
        assert "现在不能主动" not in blob
        assert "relationship_guide" not in blob
        assert "禁止主动" not in blob


def test_bias_only_changes_guidance_copy() -> None:
    guide = "已经安心时可以索吻或邀请对方留下来。"
    bodies = {}
    for bias in ("restrained", "natural", "expressive"):
        runtime = _Runtime(guide, RelationshipInitiativeSettings(expression_bias=bias))
        body = runtime._build_relationship_guide_section().body
        bodies[bias] = body
        assert "不得直接露骨" not in body
        assert "最多只能轻触" not in body
        assert "禁止H" not in body
        assert guide in body
    assert bodies["restrained"] != bodies["expressive"]
    assert "restrained" in bodies["restrained"]
    assert "expressive" in bodies["expressive"]


def test_injection_does_not_enable_intimacy_mode() -> None:
    intimacy_mode_state.exit()
    runtime = _Runtime("可以直接表达想要。", RelationshipInitiativeSettings())
    runtime._build_relationship_guide_section()
    runtime._persona_sections()
    runtime._build_intimacy_section()
    assert intimacy_mode_state.active is False
    assert intimacy_mode_state.opened_by_keyword is False
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_relationship_guide_prompt.py -q
```

Expected: FAIL because `_build_relationship_guide_section` does not exist and `_persona_sections` never adds the section.

- [ ] **Step 3: Implement injection**

In `AgentRuntime.__init__`, after `_intimacy_guide`:

```python
from app.config.relationship_initiative import RelationshipInitiativeSettings

self._relationship_guide = ""
self._relationship_settings = RelationshipInitiativeSettings().normalized()
self._relationship_guide_warned = False
```

Add:

```python
def set_relationship_initiative(self, settings, guide_text: str = "") -> None:
    from app.config.relationship_initiative import RelationshipInitiativeSettings

    self._relationship_settings = (
        settings.normalized()
        if isinstance(settings, RelationshipInitiativeSettings)
        else RelationshipInitiativeSettings().normalized()
    )
    self._relationship_guide = str(guide_text or "").strip()
```

In `update_character`, after assigning `character_profile`, reload the guide from disk via `load_relationship_guide(character_profile.relationship_guide_path)` when a profile is provided. Keep current settings; `PetWindow` will also call `set_relationship_initiative` on settings apply.

In `prompt_builder.py`:

```python
def _build_relationship_guide_section(self) -> PromptSection | None:
    from app.config.relationship_initiative import (
        RELATIONSHIP_GUIDE_TOKEN_BUDGET,
        expression_bias_guidance,
    )
    from app.core.debug_log import debug_log
    from app.llm.prompts.runtime import estimate_prompt_tokens, truncate_to_token_budget

    settings = getattr(self, "_relationship_settings", None)
    guide = str(getattr(self, "_relationship_guide", "") or "").strip()
    enabled = bool(getattr(settings, "in_turn_enabled", False))
    if not enabled or not guide:
        debug_log(
            "RelationshipInitiative",
            "A 注入",
            {"injected": False, "enabled": enabled, "chars": 0, "tokens": 0},
        )
        return None
    bias = expression_bias_guidance(getattr(settings, "expression_bias", "natural"))
    body, truncated = truncate_to_token_budget(
        f"{guide}\n\n{bias}",
        RELATIONSHIP_GUIDE_TOKEN_BUDGET,
    )
    tokens = estimate_prompt_tokens(body)
    debug_log(
        "RelationshipInitiative",
        "A 注入",
        {
            "injected": True,
            "chars": len(body),
            "tokens": tokens,
            "truncated": truncated,
            "bias": getattr(settings, "expression_bias", "natural"),
        },
    )
    return PromptSection(
        section_id="persona.relationship_guide",
        body=body,
        source="character",
        sensitivity="private",
        cache_scope="static",
        token_budget=RELATIONSHIP_GUIDE_TOKEN_BUDGET,
    )
```

Append the section inside `_persona_sections` after the character section and before plugin patches, including when `intimacy_focus=True` (L1 is not a substitute for L2, and L2 must not strip L1).

Do **not** inject a stub section when A is off. Do **not** mention the switch state to the model.

If guide load failed earlier, `PetWindow` (Task 6) logs the warning once; this task only treats empty string as missing.

- [ ] **Step 4: Run tests and verify GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_relationship_guide_prompt.py tests\unit\test_intimacy_mode.py tests\unit\test_prompt_templates.py tests\unit\test_intimacy_relationship_continuity.py -q
```

Expected: PASS. Intimacy entry still requires exact `贴紧`. Inactive intimacy copy still says it does not limit behavior.

- [ ] **Step 5: Commit**

```powershell
git add app/agent/runtime.py app/agent/prompt_builder.py tests/unit/test_relationship_guide_prompt.py
git commit -m "feat: inject static relationship guide when A is on"
```

---

### Task 4: B — `relationship_timer` collection without VLM

**Files:**
- Modify: `app/perception/observer.py:247-253,565-661,786-833,1009-1099`
- Test: `tests/unit/test_relationship_timer.py`

**Interfaces:**
- Consumes: Task 1 `RelationshipInitiativeSettings`, `RELATIONSHIP_GATE_REASONS`, `RELATIONSHIP_SILENT_COOLDOWN_SECONDS`.
- Produces: `ProactiveSpeakPayload.source: str = "screen"` and `generation: int = 0`.
- Produces: `ProactiveObserver.relationship: RelationshipInitiativeSettings`.
- Produces: `ProactiveObserver._relationship_gate_reason(now: float, busy_reason: str) -> str`.
- Produces: `ProactiveObserver._relationship_ready(now: float) -> bool`.
- Produces: independent clocks `_last_relationship_spoken_at`, `_last_relationship_silent_at`, `_relationship_generation`.
- Produces: constructor arg `relationship: RelationshipInitiativeSettings | None = None`. **`None` disables B collection.**
- Preserves: existing timer/content/window/idle collection and `_last_proactive_at` / `_last_silent_eval_at`.

- [ ] **Step 1: Write failing gate and isolation tests**

Create `tests/unit/test_relationship_timer.py` using the same `_observer` style as `tests/unit/test_proactive_focus.py`:

```python
import asyncio
from unittest.mock import AsyncMock, patch

from app.config.relationship_initiative import RelationshipInitiativeSettings
from app.perception.observer import ProactiveConfig, ProactiveObserver


def _obs(*, busy: str = "", **rel: object) -> ProactiveObserver:
    settings = RelationshipInitiativeSettings(
        proactive_enabled=bool(rel.get("proactive_enabled", True)),
        proactive_cooldown_seconds=int(rel.get("cooldown", 3600)),
        proactive_min_silence_seconds=int(rel.get("silence", 300)),
    ).normalized()
    observer = ProactiveObserver(
        api_base_url="https://example.com",
        api_key="x",
        api_model="m",
        config=ProactiveConfig(
            enabled=True,
            timer_seconds=9999,
            cooldown_seconds=600,
            min_silence_after_user=10,
            content_check_interval=9999,
            idle_threshold_seconds=99999,
            poll_interval=5,
        ),
        relationship=settings,
        is_busy=lambda: busy,
    )
    observer._last_user_at = 0.0
    observer._last_eval_at = 0.0
    observer._last_proactive_at = 0.0
    observer._last_silent_eval_at = 0.0
    observer._last_relationship_spoken_at = 0.0
    observer._last_relationship_silent_at = 0.0
    return observer


def test_bare_observer_does_not_collect_relationship_timer() -> None:
    observer = ProactiveObserver(
        api_base_url="https://example.com",
        api_key="x",
        api_model="m",
        config=ProactiveConfig(enabled=True, min_silence_after_user=0, timer_seconds=9999),
    )
    observer._last_user_at = 0.0
    assert observer._relationship_gate_reason(1000.0, "") == "disabled"


def test_gates_cover_disabled_busy_silence_cooldown_continuation() -> None:
    now = 10_000.0
    disabled = _obs(proactive_enabled=False)
    assert disabled._relationship_gate_reason(now, "") == "disabled"
    busy = _obs(busy="worker_thread")
    busy._last_user_at = now - 400
    assert busy._relationship_gate_reason(now, "worker_thread") == "busy"
    continuation = _obs(busy="rhythm_focus")
    continuation._last_user_at = now - 400
    assert continuation._relationship_gate_reason(now, "rhythm_focus") == "continuation"
    silent = _obs()
    silent._last_user_at = now - 120
    assert silent._relationship_gate_reason(now, "") == "silence"
    cooling = _obs()
    cooling._last_user_at = now - 400
    cooling._last_relationship_spoken_at = now - 10
    assert cooling._relationship_gate_reason(now, "") == "cooldown"
    ready = _obs()
    ready._last_user_at = now - 400
    assert ready._relationship_gate_reason(now, "") == "eligible"


def test_screen_cooldown_does_not_block_relationship_and_vice_versa() -> None:
    now = 10_000.0
    observer = _obs()
    observer._last_user_at = now - 400
    observer._last_proactive_at = now - 1
    observer._last_silent_eval_at = now - 1
    assert observer._relationship_gate_reason(now, "") == "eligible"
    observer._last_relationship_spoken_at = now - 1
    observer._last_proactive_at = 0.0
    observer._last_silent_eval_at = 0.0
    assert observer._relationship_gate_reason(now, "") == "cooldown"


def test_relationship_eval_does_not_capture_or_call_vlm() -> None:
    observer = _obs()
    observer._last_user_at = 0.0
    observer.capture.grab = lambda: (_ for _ in ()).throw(AssertionError("screenshot"))
    observer._get_window_text_for_eval = lambda: (_ for _ in ()).throw(AssertionError("uia"))
    observer._chat_completion = AsyncMock(side_effect=AssertionError("vlm"))
    observer._decide_relationship_speech = AsyncMock(return_value={"should_speak": False, "reason": "静かに"})
    asyncio.run(observer._do_relationship_evaluation())
    observer._decide_relationship_speech.assert_awaited()
    observer._chat_completion.assert_not_called()
```

Also add a mixed-trigger test that will be fully GREEN only after Task 5, but write the collection half now:

```python
def test_screen_trigger_wins_same_tick_and_suppresses_relationship_eval() -> None:
    observer = _obs()
    now = 10_000.0
    observer._last_user_at = now - 400
    observer._ready_focus_trigger = "window:A->B"
    observer._do_evaluation = AsyncMock()
    observer._do_relationship_evaluation = AsyncMock()
    with (
        patch("app.perception.observer.get_active_window_pid", return_value=10_001),
        patch("app.perception.observer.time.monotonic", return_value=now),
    ):
        asyncio.run(observer._dispatch_proactive_tick(now))
    observer._do_evaluation.assert_awaited()
    observer._do_relationship_evaluation.assert_not_called()
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_relationship_timer.py -q
```

Expected: FAIL on missing constructor arg, gate method, and `_do_relationship_evaluation`.

- [ ] **Step 3: Implement collection, clocks, and dispatch skeleton**

Extend `ProactiveSpeakPayload`:

```python
@dataclass(frozen=True)
class ProactiveSpeakPayload:
    text: str
    translation: str = ""
    tone: str = "中性"
    source: str = "screen"
    generation: int = 0
```

In `ProactiveObserver.__init__`:

```python
from app.config.relationship_initiative import RelationshipInitiativeSettings

self.relationship = relationship or RelationshipInitiativeSettings(proactive_enabled=False)
self._get_relationship_facts: Callable[[], str] = lambda: ""
self._last_relationship_spoken_at = 0.0
self._last_relationship_silent_at = 0.0
self._relationship_generation = 0
```

Add setters `set_relationship_settings`, `set_relationship_guide`, `set_relationship_facts_provider`, `bump_relationship_generation`, `reset_relationship_state`.

`_relationship_gate_reason(now, busy_reason)` order, matching the spec log enum:

1. `disabled` if not `relationship.proactive_enabled`
2. `continuation` if `busy_reason == "rhythm_focus"`
3. `busy` if `busy_reason` is any other truthy string
4. `silence` if `now - _last_user_at < proactive_min_silence_seconds`
5. `cooldown` if within speak cooldown **or** B silent cooldown
6. `eligible`

Do not treat observer `away_mode` as `busy`; return `busy` with reason from a dedicated check in dispatch: if `_away_mode`, skip B and log `busy`/`away`. Implement away as `busy` in the dispatcher before calling the gate, or map away onto `busy` inside the gate. Prefer mapping `_away_mode` to `busy` so tests can set it.

Change `_run` so the loop does **not** `continue` when `not config.enabled` if B is enabled.

Add `_dispatch_proactive_tick(now: float)`:

```python
async def _dispatch_proactive_tick(self, now: float) -> None:
    busy = self._is_busy()
    busy_reason = busy if isinstance(busy, str) else ("busy" if busy else "")
    screen_triggers: list[str] = []
    if self.config.enabled:
        screen_triggers = await self._collect_triggers()
    rel_reason = self._relationship_gate_reason(now, busy_reason)
    debug_log("RelationshipInitiative", "B 门控", {"reason": rel_reason})
    if screen_triggers:
        if rel_reason == "eligible":
            self._relationship_motive = True
        await self._do_evaluation(screen_triggers)
        self._relationship_motive = False
        return
    if rel_reason != "eligible":
        return
    await self._do_relationship_evaluation()
```

Implement `_do_relationship_evaluation` in this task as a complete silent-by-default path so the no-VLM test and failure-silence tests have a real method to patch. Task 5 only replaces `_decide_relationship_speech` internals and the speak/payload branch; do not leave `NotImplementedError`.

```python
async def _decide_relationship_speech(self) -> dict | None:
    return None


def _mark_relationship_silent(self) -> None:
    self._last_relationship_silent_at = time.monotonic()


async def _do_relationship_evaluation(self) -> None:
    decision = await self._decide_relationship_speech()
    if not decision or not decision.get("should_speak"):
        self._mark_relationship_silent()
        return
```

`notify_user_spoke` must also `self._relationship_generation += 1`.

Do not write `_last_proactive_at` in the relationship path.

- [ ] **Step 4: Run new tests plus observer regression**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_relationship_timer.py tests\unit\test_proactive_focus.py tests\unit\test_proactive_config.py -q
```

Expected: Task 4 tests PASS except mixed-trigger may still need `_dispatch_proactive_tick` wired; focus/config regression PASS. If mixed-trigger is implemented in this task, it should PASS with the stub eval.

- [ ] **Step 5: Commit**

```powershell
git add app/perception/observer.py tests/unit/test_relationship_timer.py
git commit -m "feat: add no-VLM relationship_timer gates"
```

---

### Task 5: B — decision prompt, silence on failure, independent cooldown

**Files:**
- Modify: `app/config/relationship_initiative.py` — add `_RELATIONSHIP_DECISION_INSTRUCTION`
- Modify: `app/perception/observer.py` — `_decide_relationship_speech`, `_do_relationship_evaluation`, optional `_post_speech_decision`
- Test: `tests/unit/test_relationship_timer.py` (extend)

**Interfaces:**
- Consumes: Task 3 guide text via `observer._relationship_guide: str` (new attribute, default `""`).
- Consumes: `expression_bias_guidance`, recent history provider, relationship facts provider, `_last_spoken_text`.
- Produces: one chat-completions JSON decision `{should_speak, reason, comment, translation, tone}`.
- Produces: on failure/parse error/empty comment: silence + `_mark_relationship_silent()`, **no template line**.
- Produces: on speak: `ProactiveSpeakPayload(source="relationship", generation=self._relationship_generation)` and `_last_relationship_spoken_at = now`.
- Preserves: `_decide_speech` visual prompt and screen JSON contract.

- [ ] **Step 1: Write failing decision tests**

Append to `tests/unit/test_relationship_timer.py`:

```python
def test_decision_instruction_has_no_ceiling_or_blacklist() -> None:
    from app.config.relationship_initiative import relationship_decision_instruction

    text = relationship_decision_instruction("natural")
    assert "先判断这是不是她此刻真实会做的事" in text
    assert "不为了证明主动而制造欲望" in text
    assert "不把屏幕内容硬拗成亲密理由" in text
    assert "最多只能轻触" not in text
    assert "不得直接露骨" not in text
    assert expression_bias_guidance("natural") in text


def test_decision_failure_is_silent_without_template() -> None:
    observer = _obs()
    spoken: list = []
    observer.on_speak = lambda payload: spoken.append(payload)
    observer._post_speech_decision = AsyncMock(return_value=None)
    asyncio.run(observer._do_relationship_evaluation())
    assert spoken == []
    assert observer._last_relationship_silent_at > 0
    assert observer._last_relationship_spoken_at == 0.0
    assert observer._last_proactive_at == 0.0


def test_speak_uses_relationship_source_and_independent_cooldown() -> None:
    observer = _obs()
    spoken: list = []
    observer.on_speak = lambda payload: spoken.append(payload)
    observer._relationship_generation = 7
    observer._post_speech_decision = AsyncMock(
        return_value={
            "should_speak": True,
            "reason": "想靠近",
            "comment": "こっち。",
            "translation": "过来。",
            "tone": "温柔",
        }
    )
    asyncio.run(observer._do_relationship_evaluation())
    assert len(spoken) == 1
    assert spoken[0].source == "relationship"
    assert spoken[0].generation == 7
    assert spoken[0].text == "こっち。"
    assert observer._last_relationship_spoken_at > 0
    assert observer._last_proactive_at == 0.0
```

- [ ] **Step 2: Run the new tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_relationship_timer.py -q
```

Expected: FAIL on missing `relationship_decision_instruction` / `_post_speech_decision` speak path.

- [ ] **Step 3: Implement the decision instruction and evaluation**

Add `relationship_decision_instruction(bias: str) -> str` in `app/config/relationship_initiative.py`. Body must be this text plus the bias paragraph:

```text
现在不是看屏幕找话题，而是判断你（夜乃桜）要不要因关系和自身状态主动开口。
对方是「他」。可以选择沉默。

先判断这是不是她此刻真实会做的事。
不为了证明主动而制造欲望。
不把屏幕内容硬拗成亲密理由。
对方真实退开、拒绝或不适时，尊重当下反应。
不要复述上一句主动发言。
一次主动不是全天黏人；一次拒绝不是永久禁欲。

should_speak=true：comment 用她会说出口的日语 1〜3 句，translation 简体中文，tone 用可用语气。
should_speak=false：comment/translation/tone 可空。
reason：简体中文一句，用于日志，不要写入对白。

{"should_speak":true|false,"reason":"…","comment":"…","translation":"…","tone":"温柔"}
```

User payload assembled by `_decide_relationship_speech`:

- `[当前时间]` local ISO-like string
- `[距上次互动]` seconds since `_last_user_at`
- character `self._system_prompt` already in the system message together with the instruction
- `self._relationship_guide` if non-empty
- `expression_bias_guidance`
- `_get_recent_history()`
- `_get_relationship_facts()` (mood + 今の関係)
- `[自分の直前の発話]` from `_last_spoken_text`

Do **not** include visual_summary, UIA excerpt, screenshots, or window titles as intimacy evidence. If facts provider throws, omit that block.

Extract the HTTP JSON call from `_decide_speech` into `_post_speech_decision(messages: list[dict]) -> dict | None` and reuse it. On empty content, JSON parse failure, or HTTP error: return `None`.

`_do_relationship_evaluation`:

1. Snapshot `generation = self._relationship_generation`.
2. `t0 = time.monotonic()`.
3. `decision = await self._decide_relationship_speech()`.
4. If `generation != self._relationship_generation`: log `B 取消` `stale_generation` and return without speaking.
5. Log `B 决策` with `result=speak|silent|error`, `bias`, `elapsed_ms`. Never log `comment`.
6. Failure / `should_speak=false` / empty comment: `_mark_relationship_silent()`; `on_evaluate(reason, False)`; return.
7. Speak: set `_last_relationship_spoken_at`, clear B silent clock, update `_last_spoken_text`, `on_evaluate(reason, True)`, `on_speak(payload)`.

When `_relationship_motive` is true during a **screen** `_decide_speech`, append a short extra block to the user text:

```text
[关系动机]
屏幕事件优先。关系与心情可以作为附加动机，但不要把屏幕内容硬拗成亲密理由，也不要连续再开一轮关系主动。
```

Do not mention allowed/forbidden sex acts.

- [ ] **Step 4: Run tests and verify GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_relationship_timer.py tests\unit\test_proactive_focus.py tests\unit\test_proactive_decision_slot.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add app/config/relationship_initiative.py app/perception/observer.py tests/unit/test_relationship_timer.py
git commit -m "feat: add relationship initiative speech decision"
```

---

### Task 6: PetWindow wiring, cancel, character switch, logs

**Files:**
- Modify: `app/ui/pet_window.py:3378-3582,3735-3747,4015-4066,8477-8534,1585-1613`
- Modify: `app/core/gui_log.py:75-95`
- Modify: `app/ui/log_window.py:98-109`
- Test: `tests/unit/test_relationship_initiative_playback.py`

**Interfaces:**
- Consumes: Task 1 loaders, Task 3 `set_relationship_initiative`, Task 4/5 payload `source`/`generation`.
- Produces: observer is constructed when `proactive.enabled or relationship.proactive_enabled`.
- Produces: `_relationship_generation` mirrored on the window; `_mark_user_activity(proactive=True)` bumps it **before** emitting any new user turn.
- Produces: `_show_proactive_comment` drops stale `source=="relationship"` payloads and busy payloads; logs `B 取消`.
- Produces: relationship speak uses `message_source="relationship"` (screen remains `"proactive"`).
- Produces: `_apply_character` restarts observer / bumps generation so no cross-character B fire remains.
- Produces: settings apply reloads relationship settings; YAML load exception keeps the previous in-memory settings.
- Produces: GUI category `RelationshipInitiative` → `关系主动`.
- Preserves: screen observer path, `贴紧` continuation, `send_message` ignore-when-worker-busy for normal chat (B does not use `ChatWorker`).

- [ ] **Step 1: Write failing playback/cancel tests**

Create `tests/unit/test_relationship_initiative_playback.py`:

```python
from types import SimpleNamespace

from app.agent.actions import AgentResult
from app.llm.chat_reply import ChatReply
from app.perception.observer import ProactiveSpeakPayload
from app.ui.pet_window import PetWindow


def test_stale_relationship_payload_is_dropped() -> None:
    consumed: list[str] = []
    window = SimpleNamespace(
        _relationship_generation=2,
        _is_proactive_observer_busy=lambda: "",
    )

    def consume(result: AgentResult, record_history: bool = True, *, message_source: str = "") -> None:
        consumed.append(message_source)

    window._consume_agent_result = consume
    payload = ProactiveSpeakPayload(
        text="こっち。",
        translation="过来。",
        tone="温柔",
        source="relationship",
        generation=1,
    )
    PetWindow._show_proactive_comment(window, payload)
    assert consumed == []


def test_relationship_payload_uses_distinct_history_source() -> None:
    consumed: list[str] = []
    window = SimpleNamespace(
        _relationship_generation=3,
        _is_proactive_observer_busy=lambda: "",
    )
    window._consume_agent_result = (
        lambda result, record_history=True, *, message_source="": consumed.append(message_source)
    )
    payload = ProactiveSpeakPayload(
        text="こっち。",
        translation="过来。",
        source="relationship",
        generation=3,
    )
    PetWindow._show_proactive_comment(window, payload)
    assert consumed == ["relationship"]


def test_busy_drops_relationship_speak_without_enabling_intimacy() -> None:
    from app.agent.builtin_tools import intimacy_mode_state

    intimacy_mode_state.exit()
    consumed: list[str] = []
    window = SimpleNamespace(
        _relationship_generation=1,
        _is_proactive_observer_busy=lambda: "subtitle_active",
    )
    window._consume_agent_result = (
        lambda result, record_history=True, *, message_source="": consumed.append(message_source)
    )
    PetWindow._show_proactive_comment(
        window,
        ProactiveSpeakPayload(text="こっち。", source="relationship", generation=1),
    )
    assert consumed == []
    assert intimacy_mode_state.active is False


def test_init_and_character_switch_wire_b_without_qt() -> None:
    from pathlib import Path

    text = Path(__file__).resolve().parents[2].joinpath("app", "ui", "pet_window.py").read_text(encoding="utf-8")
    assert "load_relationship_initiative_settings" in text
    assert "proactive_enabled" in text
    assert "_relationship_generation" in text
    assert "RelationshipInitiative" in text
    assert "_restart_proactive_observer" in text
    assert "set_relationship_guide" in text
    assert "build_continuity_context" in text
```

This is a source-contract guard so character-switch/init wiring cannot be omitted in a Qt-heavy class. The behavioral cancel tests above exercise `_show_proactive_comment` directly.

- [ ] **Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_relationship_initiative_playback.py -q
```

Expected: FAIL because `_show_proactive_comment` ignores `source`/`generation` and always uses `message_source="proactive"`.

- [ ] **Step 3: Implement PetWindow wiring**

`_init_proactive_observer`:

1. `rel = self.settings_service.load_relationship_initiative_settings()` inside try/except; on failure keep `getattr(self, "relationship_initiative_settings", RelationshipInitiativeSettings().normalized())`.
2. Store `self.relationship_initiative_settings = rel`.
3. If `not config.enabled and not rel.proactive_enabled`: return.
4. Pass `relationship=rel` into `ProactiveObserver`.
5. `observer.set_relationship_guide(load_relationship_guide(self.character_profile.relationship_guide_path))`.
6. `observer.set_relationship_facts_provider(lambda: self.memory_store.build_continuity_context())`.
7. `self.agent_runtime.set_relationship_initiative(rel, guide_text)`.
8. If guide file was expected and loaded empty, `debug_log("RelationshipInitiative", "guide 缺失", {"character_id": ...})` once.

`_show_proactive_comment`:

```python
source = getattr(payload, "source", "screen") or "screen"
generation = int(getattr(payload, "generation", 0) or 0)
if source == "relationship":
    if generation != int(getattr(self, "_relationship_generation", 0) or 0):
        debug_log("RelationshipInitiative", "B 取消", {"reason": "stale_generation"})
        return
    if self._is_proactive_observer_busy():
        debug_log("RelationshipInitiative", "B 取消", {"reason": "busy_before_display"})
        return
message_source = "relationship" if source == "relationship" else "proactive"
# existing split + _consume_agent_result(..., message_source=message_source)
if source == "relationship":
    debug_log("RelationshipInitiative", "B 发言完成", {"chars": len(payload.text)})
```

`_format_recent_history`: label `source == "relationship"` as `她自己的·关系主动`.

`_mark_user_activity(proactive=True)`: `self._relationship_generation = getattr(self, "_relationship_generation", 0) + 1` and `observer.bump_relationship_generation()` if observer exists. This drops in-flight B decisions and queued Qt payloads.

`_apply_character`: after swapping the profile, bump generation, `set_relationship_initiative` with the new guide, and `_restart_proactive_observer()` when `profile.id != previous_character_id`. That cancels the old timer/thread so B cannot fire for the previous character.

Settings save path that already calls `_restart_proactive_observer` (`_refresh_llm_clients_after_settings`) will pick up YAML changes. Wrap load in try/except as above.

`close_external_tools` already stops the observer; also bump generation there so a late Qt slot cannot speak after shutdown.

Add GUI maps:

```python
("RelationshipInitiative", "A 注入"): "关系主动 A 注入",
("RelationshipInitiative", "B 门控"): "关系主动门控",
("RelationshipInitiative", "B 决策"): "关系主动决策",
("RelationshipInitiative", "B 取消"): "关系主动取消",
("RelationshipInitiative", "B 发言完成"): "关系主动发言完成",
```

Add `"RelationshipInitiative"` to `_PROGRAM_GUI_CATEGORIES` and log window label `关系主动`.

Do not auto-call `user_requests_intimacy_entry` or `intimacy_mode_state.enter`.

- [ ] **Step 4: Run tests and verify GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_relationship_initiative_playback.py tests\unit\test_relationship_timer.py tests\unit\test_intimacy_pet_window.py tests\unit\test_proactive_decision_slot.py tests\unit\test_proactive_reply_history_buttons.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add app/ui/pet_window.py app/core/gui_log.py app/ui/log_window.py tests/unit/test_relationship_initiative_playback.py
git commit -m "feat: wire relationship initiative cancel and logs"
```

---

### Task 7: Sakura L1 `relationship_guide.md` content

**Files:**
- Create locally ignored: `characters/Sakura/relationship_guide.md`
- Optional local edit: `characters/Sakura/character.json` key `"relationship_guide": "relationship_guide.md"` (ignored; default path also works)
- Test: `tests/unit/test_relationship_guide_content.py`

**Interfaces:**
- Produces: a short behavior-rule guide covering the five spec situations. Examples are texture, not a quote bank. No fixed hug→kiss→sex ladder. No `贴紧`. No original-route lines.
- Preserves: `card.md`, `system_guards.md`, `data/intimacy_guide.txt`.

- [ ] **Step 1: Write failing content-contract tests**

Put this exact guide body in the test module as `CANONICAL_RELATIONSHIP_GUIDE`. CI asserts that constant. If the ignored live file exists, it must match.

```python
from pathlib import Path

REQUIRED_SITUATIONS = (
    "尚未亲近",
    "已经安心",
    "私下升温",
    "被冷落或冲突",
    "事后回落",
)
FORBIDDEN = (
    "贴紧",
    "先抱、再吻",
    "台词库",
    "槐君",
    "好感度",
    "进度条",
)
CANONICAL_RELATIONSHIP_GUIDE = """# 关系演出参考

这是条件化演出参考，不是第二张人格卡，也不是台词库。用自己的语气行动；示例只说明动作纹理，禁止复读成固定台词。没有固定上限，也不存在“先抱、再吻、最后才能进一步”的阶梯。系统不会替她开启详细亲密导演。

## 尚未亲近

短、观察多。主动主要是把人留下、问清楚、确认界线，而不是把场面一下推热。可以看他、问他要不要坐下、把饮料推过去。不要为了填沉默而发明亲密。

## 已经安心

认定关系之后不必等他先伸手。可以主动分享生活、靠近、依偎、触碰、索吻，或把人拉到身边一起待着。平时仍克制；真想起意时直接做，不要演成永远等待请求。

## 私下升温

可以由轻触慢慢前进，也可以在欲望已积累、关系与现场都支持时直接说出想要什么。不要把已经产生的欲望改写成只能拥抱。也不要因为“这次主动了”就整晚不停升级。对方退开、迟疑或拒绝时立刻放缓，那是判断的一部分。

## 被冷落或冲突

先按她的方式处理问题：冷一点、问一句、或暂时把身体距离拉开。修复或出现明确反证后，行为跟着当下走，不持续复读旧防御，也不把一次拒绝写成永久禁欲。

## 事后回落

高温之后回到生活事务和普通说话。可以还想靠近，但不把一次高温写成永久人格，也不把事后余韵当成必须立刻再来一次的理由。
"""


def test_canonical_guide_covers_situations_without_ladder_or_mode_gate() -> None:
    text = CANONICAL_RELATIONSHIP_GUIDE
    for item in REQUIRED_SITUATIONS:
        assert item in text
    for item in FORBIDDEN:
        assert item not in text
    assert "禁止复读成固定台词" in text
    assert "没有固定上限" in text
    assert len(text) < 4000


def test_live_sakura_guide_matches_canonical_when_present() -> None:
    live = Path(__file__).resolve().parents[2] / "characters" / "Sakura" / "relationship_guide.md"
    if not live.is_file():
        return
    assert live.read_text(encoding="utf-8").strip() == CANONICAL_RELATIONSHIP_GUIDE.strip()
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_relationship_guide_content.py -q
```

Expected: `test_canonical_guide_covers_situations_without_ladder_or_mode_gate` PASS once the test file exists (the contract is the constant). `test_live_sakura_guide_matches_canonical_when_present` skips if the ignored live file is missing, or FAIL if a stale live file does not match.

- [ ] **Step 3: Write the live ignored guide**

Write the same `CANONICAL_RELATIONSHIP_GUIDE` body to `characters/Sakura/relationship_guide.md`. Do not `git add -f` it. Default loader path finds `relationship_guide.md` even without a `character.json` key.

- [ ] **Step 4: Run tests and verify GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_relationship_guide_content.py tests\unit\test_relationship_guide_loader.py tests\unit\test_relationship_guide_prompt.py -q
```

Expected: PASS. CI uses `CANONICAL_RELATIONSHIP_GUIDE`; the live ignored file is compared only when present.

- [ ] **Step 5: Commit tracked files only**

```powershell
git add tests/unit/test_relationship_guide_content.py
git commit -m "test: lock relationship guide content contract"
```

---

### Task 8: Regression gate and RP acceptance notes

**Files:**
- Test only: run existing suites; no production edits unless a regression appears.
- Do not rewrite this plan mid-gate except to tick checkboxes.

**Interfaces:**
- Consumes: all previous tasks.
- Produces: evidence that screen observer, intimacy control phrases, and guideless packages still work.

- [ ] **Step 1: Run the targeted new suite**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_relationship_initiative_config.py tests\unit\test_relationship_guide_loader.py tests\unit\test_relationship_guide_prompt.py tests\unit\test_relationship_guide_content.py tests\unit\test_relationship_timer.py tests\unit\test_relationship_initiative_playback.py tests\unit\test_character_archive.py -q
```

Expected: PASS.

- [ ] **Step 2: Run intimacy + observer regression**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_intimacy_mode.py tests\unit\test_intimacy_pet_window.py tests\unit\test_intimacy_relationship_continuity.py tests\unit\test_intimacy_card_soften.py tests\unit\test_system_guards_prompt.py tests\unit\test_proactive_focus.py tests\unit\test_proactive_config.py tests\unit\test_proactive_settings.py tests\unit\test_prompt_templates.py -q
```

Expected: PASS. Confirm:

- `贴紧` / `苹果` exact match unchanged
- three-step continuation unchanged
- B-off (bare observer / `proactive_enabled=False`) still collects timer/content/window
- packages without `relationship_guide.md` still import

- [ ] **Step 3: Run the full unit gate**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit -q
```

Expected: PASS. If UI tests are required by the coordinator, also run `tests\ui -q`. This task does not require a live desktop RP session; record that as human follow-up.

- [ ] **Step 4: Manual RP checklist (human, not agent)**

Do not automate these. After implementation, a human should verify:

1. Ordinary daily chat does not mechanically swerve into intimacy.
2. When the relationship is settled and private, she can close distance without waiting for a request.
3. When desire has recent accumulation, a direct initiate is allowed and is not force-downgraded to a hug.
4. One refusal affects the present; after repair, initiative can return.
5. B may stay silent across a long idle; it must not speak every cooldown just to prove the feature.
6. A B line does not repeat the previous proactive line, and does not twist screen contents into an intimate pretext.
7. Switching `restrained` / `natural` / `expressive` changes readiness to speak, not personality.
8. Sending `贴紧` is still the only way to open L2. A/B never send it.

- [ ] **Step 5: Commit only if Step 3 forced a fix**

If the gate is green with no extra diffs, do not create an empty commit. If a regression fix was required, commit that fix with `fix:` and re-run Step 3 before finishing.

---

## Self-Review

Performed against `docs/superpowers/specs/2026-08-24-sakura-relationship-initiative-design.md` after the tasks were written.

### Spec coverage

| Spec requirement | Task |
|---|---|
| L0 card stays personality, not a state machine | Locked decision 10; not rewritten |
| L1 optional `relationship_guide.md` | Tasks 2, 7 |
| A injects static `persona.relationship_guide` | Task 3 |
| A off injects no negative limit | Task 3 |
| L2 unchanged; not the source of initiative | Tasks 3, 6, 8 |
| L3 A/B switches, bias, cooldown, busy, cancel, logs | Tasks 1, 4, 5, 6 |
| YAML defaults A/B on, `natural`, 3600 / 300 | Task 1 |
| Unknown bias → `natural`; invalid times sanitized | Task 1 |
| No extra classifier LLM | Locked decision 2; Tasks 3, 5 |
| B reuses observer thread / busy / playback | Tasks 4–6 |
| `relationship_timer` no screenshot/UIA/VLM | Task 4 |
| Gates: disabled/busy/silence/cooldown/continuation | Task 4 |
| Independent B cooldown vs screen comments | Tasks 4, 5 |
| Mixed triggers: one eval, screen wins, relationship as motive | Tasks 4, 5 |
| Model may stay silent; failure stays silent, no template | Task 5 |
| User message cancels undisplayed B | Task 6 |
| App close / character switch / disable B cancels timer | Task 6 |
| Guide missing: warn, degrade, chat still works | Tasks 2, 3, 6 |
| Archive like `system_guards` | Task 2 |
| Guideless characters: A is a no-op | Tasks 2, 3, 8 |
| Logs without private body | Tasks 3, 5, 6 |
| Unit tests 1–10 | Tasks 1–6, 8 |
| Regression: observer, 贴紧/苹果/三续投, B-off screen, guideless import | Task 8 |
| Human RP 1–7 | Task 8 checklist |
| Non-goals: no affection meter, no auto `贴紧`, no quote bank, no memory rewrite, no L2 rewrite | Global constraints + Task 7 forbidden strings |

### Placeholder scan

Removed vague “handle edge cases” / “write tests for the above” / “similar to Task N” wording. Bias copy, decision instruction, guide body, gate enum, YAML keys, section id, payload fields, and pytest commands are concrete.

### Type consistency

- Settings fields: `in_turn_enabled`, `proactive_enabled`, `expression_bias`, `proactive_cooldown_seconds`, `proactive_min_silence_seconds` — same names from spec YAML through dataclass, YAML I/O, observer, and prompt injection.
- Section id is `persona.relationship_guide` everywhere.
- Payload fields `source` / `generation` are defined in Task 4 and consumed in Tasks 5–6.
- Gate reasons are the spec’s six strings.
- `load_relationship_guide(path: Path | None) -> str` is the only loader name later tasks use.
- `set_relationship_initiative(settings, guide_text: str)` is the runtime setter used by PetWindow.
- History source for B is `"relationship"`; screen remains `"proactive"`.
- B speak clock is `_last_relationship_spoken_at`; screen clock remains `_last_proactive_at`.

### Residual risks (not spec gaps)

- `send_message` still ignores input while a normal `ChatWorker` is busy. B itself does not use that worker; cancel covers queued B playback. A user message cannot preempt an in-flight **screen** proactive comment already inside `_consume_agent_result` TTS — same as today.
- Live `characters/Sakura/relationship_guide.md` will not be in git. Local RP needs the ignored file; CI uses the canonical string in tests.
- No settings UI: changing A/B requires YAML or a future settings task.
- Default-on B in production depends on PetWindow passing loaded settings. Bare `ProactiveObserver()` in tests keeps B off by design.
