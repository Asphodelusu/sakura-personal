"""Current-scope view of memory_dir/core_profiles.json.

Reads never rewrite the file. Section patches are a separate explicit write
and still require the personal write-rehearsal gate.
"""
import json
import re
import stat
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path

if __package__:
    from .support import log_event
else:
    from support import log_event


CORE_PROFILE_SCHEMA_VERSION = 2
CORE_PROFILE_CONTEXT_BUDGET = 1200
CORE_PROFILE_FORMAL_SECTIONS = (
    "今の関係",
    "あなたについて知っていること",
    "今の私",
    "大切な約束と境界",
)
CORE_PROFILE_FORMAL_SECTION_SET = frozenset(CORE_PROFILE_FORMAL_SECTIONS)
CORE_PROFILE_LEGACY_SECTION = "legacy"
CORE_PROFILE_MAX_ORDINARY_SECTION_PATCHES = 2
CORE_PROFILE_MAINTAINER_SOURCE = "core_maintainer"
_QUOTED_TOKEN = re.compile("「[^」]*」|『[^』]*』|\"[^\"]*\"|'[^']*'")
_NUMBER_TOKEN = re.compile(r"\d+(?:\.\d+)?")
_LABEL = "【常驻档案】\n"
_BODY_BUDGET = CORE_PROFILE_CONTEXT_BUDGET - len(_LABEL)


def read_personal_core_profile(memory_dir, scope):
    """Return one private context fragment, or None. Diagnostics omit profile text."""
    try:
        return _read(Path(memory_dir).absolute(), str(scope))
    except Exception:
        _diagnose("CORE_PROFILE_UNREADABLE")
        return None


def _read(memory_dir, scope):
    if any(_unsupported_link(path) for path in (memory_dir, *memory_dir.parents)):
        return None
    path = memory_dir / "core_profiles.json"
    try:
        exists = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        _diagnose("CORE_PROFILE_UNREADABLE")
        return None
    if _reparse(exists) or (stat.S_ISREG(exists.st_mode) and exists.st_nlink != 1):
        _diagnose("CORE_PROFILE_LINK")
        return None
    try:
        if not path.resolve(strict=True).is_relative_to(memory_dir.resolve(strict=True)):
            _diagnose("CORE_PROFILE_LINK")
            return None
        raw = path.read_bytes()
    except OSError:
        _diagnose("CORE_PROFILE_UNREADABLE")
        return None
    if not raw.strip():
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _diagnose("CORE_PROFILE_ENCODING")
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        _diagnose("CORE_PROFILE_JSON")
        return None
    if not isinstance(data, dict):
        _diagnose("CORE_PROFILE_STRUCTURE")
        return None
    record = data.get(scope)
    if not isinstance(record, dict):
        return None
    metadata = record.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    record_id = record.get("id")
    if (
        (isinstance(record_id, str) and record_id.startswith("core_profile:")
         and record_id != f"core_profile:{scope}")
        or record.get("scope") not in (None, "", scope)
        or metadata.get("scope") not in (None, "", scope)
    ):
        _diagnose("CORE_PROFILE_SCOPE")
        return None
    version = _schema_version(record)
    stored = _stored_text(record)
    content = stored
    if not content and version == CORE_PROFILE_SCHEMA_VERSION:
        content = _sections_text(record)
        if content:
            log_event(
                "Memory",
                "个人常驻档案按章节只读降级",
                {"code": "CORE_PROFILE_SECTIONS_FALLBACK"},
                event="memory.personal.core_profile_sections_fallback",
                severity="debug",
            )
    if not content:
        return None
    if version not in {None, CORE_PROFILE_SCHEMA_VERSION}:
        log_event(
            "Memory",
            "个人常驻档案未知版本只读兼容",
            {"code": "CORE_PROFILE_UNKNOWN_SCHEMA", "schema_version": version},
            event="memory.personal.core_profile_unknown_schema",
            severity="debug",
        )
    body = _clip(content, _BODY_BUDGET)
    if not body:
        return None
    rendered = _LABEL + body
    return {
        "id": f"core_profile:{scope}",
        "content": rendered,
        "priority": 90,
        "budgetHint": CORE_PROFILE_CONTEXT_BUDGET,
        "sensitivity": "private",
    }


def _unsupported_link(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        _diagnose("CORE_PROFILE_UNREADABLE")
        return True
    if _reparse(info):
        _diagnose("CORE_PROFILE_LINK")
        return True
    return False


def _reparse(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _diagnose(code):
    log_event(
        "Memory",
        "个人常驻档案不可读",
        {"code": code},
        event="memory.personal.core_profile_unreadable",
        severity="warning",
    )


def _schema_version(raw):
    if "schema_version" not in raw:
        return None
    value = raw.get("schema_version")
    if isinstance(value, bool) or not isinstance(value, int):
        return -1
    return value


def _stored_text(raw):
    return str(raw.get("content") or raw.get("memory") or "").strip()


def _sections_text(raw):
    sections = raw.get("sections")
    if not isinstance(sections, dict) or not sections:
        return ""
    if not all(
        isinstance(key, str) and key.strip() and isinstance(value, str) and value.strip()
        for key, value in sections.items()
    ):
        return ""
    return "\n\n".join(str(value).strip() for value in sections.values())


def _clip(text, budget):
    value = text.strip()
    if len(value) <= budget:
        return value
    return value[: max(0, budget - 1)].rstrip() + "…"


class CoreProfileStorageError(RuntimeError):
    """Strict profile write failure. The message must not include profile text."""


def load_personal_core_profile_record(memory_dir, scope):
    """Return the raw in-scope record, or None. Does not write."""
    try:
        memory_dir = Path(memory_dir).absolute()
        scope = str(scope)
        if any(_unsupported_link(path) for path in (memory_dir, *memory_dir.parents)):
            return None
        record = _load_profiles(memory_dir / "core_profiles.json", strict=False).get(scope)
    except Exception:
        return None
    if not isinstance(record, dict) or _scope_conflict(record, scope):
        return None
    return record


def patch_personal_core_profile_sections(
    memory_dir, scope, base_updated_at, sections, *, candidate_ids=None, migrate_legacy=False,
    cancel_checker=None,
):
    """Whitelist section update. Validation failure leaves the source and backup unchanged."""
    memory_dir = Path(memory_dir).absolute()
    scope = str(scope)
    _admit_write(memory_dir)
    if any(_unsupported_link(path) for path in (memory_dir, *memory_dir.parents)):
        raise CoreProfileStorageError("常驻档案路径不可写")
    path = memory_dir / "core_profiles.json"
    _require_plain_profile_file(path)
    _require_plain_profile_file(path.with_name(path.name + ".bak"))
    _require_plain_profile_file(path.with_name(path.name + ".lock"))
    with _exclusive_profile_lock(path):
        if cancel_checker is not None:
            cancel_checker()
        _require_plain_profile_file(path)
        _require_plain_profile_file(path.with_name(path.name + ".bak"))
        return _patch_locked(
            path, scope, base_updated_at, sections,
            candidate_ids=candidate_ids, migrate_legacy=bool(migrate_legacy),
            cancel_checker=cancel_checker,
        )


def _admit_write(memory_dir):
    if __package__:
        from .personal_records import _require_write_rehearsal
    else:
        from personal_records import _require_write_rehearsal
    _require_write_rehearsal(memory_dir)


def _scope_conflict(record, scope):
    metadata = record.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    record_id = record.get("id")
    return (
        (isinstance(record_id, str) and record_id.startswith("core_profile:")
         and record_id != f"core_profile:{scope}")
        or record.get("scope") not in (None, "", scope)
        or metadata.get("scope") not in (None, "", scope)
    )


def _load_profiles(path, *, strict):
    _require_plain_profile_file(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        if strict:
            raise CoreProfileStorageError(
                f"无法读取常驻档案文件 {path.name}：{type(exc).__name__}"
            ) from exc
        return {}
    if isinstance(data, dict):
        return data
    if strict:
        raise CoreProfileStorageError(f"常驻档案文件 {path.name} 顶层不是对象")
    return {}


def _patch_locked(path, scope, base_updated_at, sections, *, candidate_ids, migrate_legacy, cancel_checker=None):
    profiles = _load_profiles(path, strict=True)
    previous = profiles.get(scope)
    if not isinstance(previous, dict):
        raise CoreProfileStorageError("常驻档案不存在，拒绝章节更新")
    if _scope_conflict(previous, scope):
        raise CoreProfileStorageError("常驻档案 scope 不符，拒绝写入")
    version = _schema_version(previous)
    if version not in {None, CORE_PROFILE_SCHEMA_VERSION}:
        raise CoreProfileStorageError("常驻档案未知 schema，拒绝写入")
    if version != CORE_PROFILE_SCHEMA_VERSION:
        raise CoreProfileStorageError("常驻档案不是 V2，拒绝章节更新")
    if _updated_at(previous) != str(base_updated_at or "").strip():
        raise CoreProfileStorageError("常驻档案乐观锁冲突")
    incoming = _parse_section_patch(sections)
    current_sections = previous.get("sections")
    _reject_invalid_sections(current_sections)
    if not isinstance(current_sections, dict):
        current_sections = {}
    legacy_text = str(current_sections.get(CORE_PROFILE_LEGACY_SECTION) or "").strip()
    if migrate_legacy:
        if not legacy_text:
            raise CoreProfileStorageError("常驻档案没有 legacy，拒绝迁移")
        ordered = {name: incoming.get(name, "").strip() for name in CORE_PROFILE_FORMAL_SECTIONS}
        _validate_legacy_migration(legacy_text, ordered)
    else:
        if legacy_text:
            raise CoreProfileStorageError("常驻档案仍含 legacy，需要 migrate_legacy")
        if len(incoming) > CORE_PROFILE_MAX_ORDINARY_SECTION_PATCHES:
            raise CoreProfileStorageError("普通章节更新最多 2 个")
        ordered = {name: str(current_sections.get(name) or "").strip() for name in CORE_PROFILE_FORMAL_SECTIONS}
        changed = 0
        for name, value in incoming.items():
            if _normalize_text(value) != _normalize_text(ordered[name]):
                changed += 1
                ordered[name] = value.strip()
        rendered = _render_sections(ordered)
        if changed == 0 and str(previous.get("content") or "") == rendered and str(previous.get("memory") or "") == rendered:
            return previous
    now = _next_updated_at(_updated_at(previous))
    metadata = previous.get("metadata") if isinstance(previous.get("metadata"), dict) else {}
    rendered = _render_sections(ordered)
    record = {
        "id": f"core_profile:{scope}",
        "schema_version": CORE_PROFILE_SCHEMA_VERSION,
        "content": rendered,
        "memory": rendered,
        "sections": ordered,
        "metadata": {
            **metadata,
            "layer": "core_profile",
            "scope": scope,
            "updated_at": now,
            "created_at": str(metadata.get("created_at") or "").strip() or now,
            "source": CORE_PROFILE_MAINTAINER_SOURCE,
            "candidate_ids": _candidate_ids(candidate_ids),
        },
    }
    profiles[scope] = record
    if cancel_checker is not None:
        cancel_checker()
    _save_profiles(path, profiles)
    return record


def _updated_at(raw):
    metadata = raw.get("metadata")
    if isinstance(metadata, dict) and str(metadata.get("updated_at") or "").strip():
        return str(metadata.get("updated_at")).strip()
    return str(raw.get("updated_at") or "").strip()


def _next_updated_at(previous):
    if __package__:
        from .support import parse_iso_datetime
    else:
        from support import parse_iso_datetime
    now = datetime.now().astimezone()
    prev = parse_iso_datetime(previous)
    if prev is not None and now <= prev:
        now = prev + timedelta(microseconds=1)
    return now.isoformat(timespec="microseconds")


def _parse_section_patch(sections):
    if not isinstance(sections, Mapping):
        raise CoreProfileStorageError("常驻档案 sections 必须是对象")
    parsed = {}
    for key, value in sections.items():
        if not isinstance(key, str) or key not in CORE_PROFILE_FORMAL_SECTION_SET:
            raise CoreProfileStorageError("常驻档案未知 section，拒绝写入")
        if not isinstance(value, str):
            raise CoreProfileStorageError("常驻档案 section 必须是文本")
        parsed[key] = value
    return parsed


def _reject_invalid_sections(sections):
    if not isinstance(sections, dict):
        raise CoreProfileStorageError("常驻档案 sections 必须是对象")
    has_legacy = False
    has_formal = False
    for key in sections:
        if not isinstance(key, str) or not key.strip():
            raise CoreProfileStorageError("常驻档案存在未知 section，拒绝写入")
        if key == CORE_PROFILE_LEGACY_SECTION:
            has_legacy = True
            continue
        if key in CORE_PROFILE_FORMAL_SECTION_SET:
            has_formal = True
            continue
        raise CoreProfileStorageError("常驻档案存在未知 section，拒绝写入")
    if has_legacy and has_formal:
        raise CoreProfileStorageError("常驻档案 legacy 与正式章节混存，拒绝写入")


def _render_sections(sections):
    parts = []
    for name in CORE_PROFILE_FORMAL_SECTIONS:
        body = str(sections.get(name) or "").strip()
        if body:
            parts.append(f"＜{name}＞\n{body}")
    return "\n\n".join(parts)


def _normalize_text(text):
    return " ".join(str(text).split())


def _split_sentences(text):
    raw = str(text or "")
    sentences = []
    buf = []
    index = 0
    while index < len(raw):
        char = raw[index]
        if char == "\n":
            sentence = _normalize_text("".join(buf))
            buf = []
            if sentence:
                sentences.append(sentence)
            while index + 1 < len(raw) and raw[index + 1] == "\n":
                index += 1
        else:
            buf.append(char)
            ascii_stop = char == "." and (index + 1 >= len(raw) or not raw[index + 1].isdigit())
            if char in "。．？！?!" or ascii_stop:
                sentence = _normalize_text("".join(buf))
                buf = []
                if sentence:
                    sentences.append(sentence)
        index += 1
    tail = _normalize_text("".join(buf))
    if tail:
        sentences.append(tail)
    return sentences


def _protected_tokens(text):
    normalized = _normalize_text(text)
    tokens = Counter(_QUOTED_TOKEN.findall(normalized))
    tokens.update(_NUMBER_TOKEN.findall(normalized))
    return tokens


def _validate_legacy_migration(legacy, formal_sections):
    formal_text = "\n".join(str(formal_sections.get(name) or "") for name in CORE_PROFILE_FORMAL_SECTIONS)
    if Counter(_split_sentences(legacy)) != Counter(_split_sentences(formal_text)):
        raise CoreProfileStorageError("常驻档案 legacy 迁移校验失败")
    if _protected_tokens(legacy) != _protected_tokens(formal_text):
        raise CoreProfileStorageError("常驻档案 legacy 迁移校验失败")


def _candidate_ids(candidate_ids):
    if candidate_ids is None:
        return []
    if isinstance(candidate_ids, str):
        value = candidate_ids.strip()
        return [value] if value else []
    return [str(item).strip() for item in candidate_ids if str(item).strip()]


def _save_profiles(path, profiles):
    _require_plain_profile_file(path)
    _require_plain_profile_file(path.with_name(path.name + ".bak"))
    if path.exists():
        backup = path.with_name(path.name + ".bak")
        try:
            backup.write_bytes(path.read_bytes())
        except OSError as exc:
            raise CoreProfileStorageError("常驻档案备份失败，拒绝写入") from exc
    if __package__:
        from .support import atomic_write_text
    else:
        from support import atomic_write_text
    atomic_write_text(path, json.dumps(profiles, ensure_ascii=False, indent=2) + "\n", backup=False)


def _require_plain_profile_file(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if _reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise CoreProfileStorageError("常驻档案文件或备份路径不可用")


def _exclusive_profile_lock(path):
    if __package__:
        from .personal_core_candidates import exclusive_json_path_lock
    else:
        from personal_core_candidates import exclusive_json_path_lock
    return exclusive_json_path_lock(path)
