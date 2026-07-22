"""持久化记忆访问追踪：SQLite 版本，取代每轮全量读写 JSON 的旧实现。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agent.access_tracker import AccessTracker


@pytest.fixture()
def tracker(tmp_path: Path) -> AccessTracker:
    t = AccessTracker(tmp_path / "access_tracker.db")
    yield t
    t.close()


def test_record_and_bulk_lookup_roundtrip(tracker: AccessTracker) -> None:
    tracker.record_accessed(["m1", "m2"], when="2026-01-01T00:00:00+08:00")
    result = tracker.get_last_accessed_bulk(["m1", "m2", "m3"])
    assert result == {
        "m1": "2026-01-01T00:00:00+08:00",
        "m2": "2026-01-01T00:00:00+08:00",
    }
    assert "m3" not in result


def test_record_accessed_updates_existing_id(tracker: AccessTracker) -> None:
    tracker.record_accessed(["m1"], when="2026-01-01T00:00:00+08:00")
    tracker.record_accessed(["m1"], when="2026-01-05T00:00:00+08:00")
    result = tracker.get_last_accessed_bulk(["m1"])
    assert result["m1"] == "2026-01-05T00:00:00+08:00"


def test_empty_inputs_are_noop(tracker: AccessTracker) -> None:
    tracker.record_accessed([], when="2026-01-01T00:00:00+08:00")
    assert tracker.get_last_accessed_bulk([]) == {}


def test_migrates_from_legacy_json_once(tmp_path: Path) -> None:
    json_path = tmp_path / "access_tracker.json"
    json_path.write_text(
        json.dumps({"m1": "2025-12-01T00:00:00+08:00", "m2": "2025-12-02T00:00:00+08:00"}),
        encoding="utf-8",
    )
    db_path = tmp_path / "access_tracker.db"
    tracker = AccessTracker(db_path)
    try:
        result = tracker.get_last_accessed_bulk(["m1", "m2"])
        assert result["m1"] == "2025-12-01T00:00:00+08:00"
        assert result["m2"] == "2025-12-02T00:00:00+08:00"
    finally:
        tracker.close()

    # 迁移后修改旧 JSON，重新打开 db 不应再导入（迁移只发生一次）
    json_path.write_text(json.dumps({"m1": "2099-01-01T00:00:00+08:00"}), encoding="utf-8")
    tracker2 = AccessTracker(db_path)
    try:
        result2 = tracker2.get_last_accessed_bulk(["m1"])
        assert result2["m1"] == "2025-12-01T00:00:00+08:00"
    finally:
        tracker2.close()


def test_record_accessed_is_a_single_transaction(tracker: AccessTracker) -> None:
    """多条 id 的一次 record_accessed 调用应该只对应一次 BEGIN/COMMIT，不是逐行提交。"""
    statements: list[str] = []
    tracker._conn.set_trace_callback(statements.append)
    try:
        tracker.record_accessed(["m1", "m2", "m3", "m4", "m5"], when="2026-01-01T00:00:00+08:00")
    finally:
        tracker._conn.set_trace_callback(None)
    commit_calls = [s.strip().upper() for s in statements if s.strip().upper() in {"BEGIN", "COMMIT"}]
    assert commit_calls == ["BEGIN", "COMMIT"]


def test_missing_legacy_json_is_fine(tmp_path: Path) -> None:
    tracker = AccessTracker(tmp_path / "access_tracker.db")
    try:
        assert tracker.get_last_accessed_bulk(["anything"]) == {}
    finally:
        tracker.close()
