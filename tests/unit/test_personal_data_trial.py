import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from app.legacy_import.personal_copy import PersonalCopyError, STATE_FILE

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows source leases")


def source_tree(tmp_path):
    source = tmp_path / "source"
    for name in ("data/memory", "data/chat_history", "data/runtime_state", "data/config", "characters"):
        (source / name).mkdir(parents=True)
        (source / name / "keep.txt").write_text("synthetic")
    (source / "data/memory_curation_state.json").write_text("{}")
    (source / "unrelated").mkdir()
    (source / "unrelated/private.txt").write_text("must not copy")
    return source


def test_bundle_preserves_selected_domains_and_materializes_only_explicit_voice_hardlinks(tmp_path):
    from tools.personal_data_trial import copy_baseline
    source = source_tree(tmp_path)
    (source / "data/visual_observations").mkdir()
    (source / "data/visual_observations/records.jsonl").write_text("synthetic visual source")
    (source / "data/intimacy_guide.txt").write_text("synthetic state")
    weight = source / "characters/Sakura/voice/models/Sakura-e15.ckpt"
    weight.parent.mkdir(parents=True)
    weight.write_bytes(b"voice")
    os.link(weight, tmp_path / "weight-alias")
    target = tmp_path / "baseline"
    copy_baseline(source, target, source_is_quiescent=lambda: True)
    assert not (target / "unrelated").exists()
    assert (target / "data/visual_observations/records.jsonl").read_text() == "synthetic visual source"
    assert (target / "data/intimacy_guide.txt").read_text() == "synthetic state"
    assert (target / weight.relative_to(source)).read_bytes() == b"voice"
    assert (target / weight.relative_to(source)).stat().st_nlink == 1
    assert json.loads((target / STATE_FILE).read_text())["role"] == "baseline"
    os.link(source / "data/memory/keep.txt", tmp_path / "database-alias")
    with pytest.raises(PersonalCopyError, match="LINK_UNSUPPORTED"):
        copy_baseline(source, tmp_path / "refused", source_is_quiescent=lambda: True)


def test_all_domains_stay_pinned_until_copy_finishes_and_new_files_prevent_publication(tmp_path, monkeypatch):
    from tools import personal_data_trial as trial
    source = source_tree(tmp_path)
    original = trial.shutil.copyfile
    def copying(src, dst):
        with pytest.raises(OSError):
            (source / "data/chat_history/keep.txt").write_text("competing writer")
        original(src, dst)
        (source / "data/memory/late.txt").write_text("new")
    monkeypatch.setattr(trial.shutil, "copyfile", copying)
    target = tmp_path / "incomplete"
    with pytest.raises(PersonalCopyError, match="SOURCE_CHANGED"):
        trial.copy_baseline(source, target, source_is_quiescent=lambda: True)
    assert json.loads((target / STATE_FILE).read_text())["state"] == "incomplete"
    (source / "data/chat_history/keep.txt").write_text("released")


def test_active_writer_refuses_before_creating_baseline(tmp_path):
    from tools.personal_data_trial import copy_baseline
    source = source_tree(tmp_path)
    target = tmp_path / "baseline"
    with (source / "data/chat_history/keep.txt").open("r+b"):
        with pytest.raises(PersonalCopyError, match="SOURCE_BUSY"):
            copy_baseline(source, target, source_is_quiescent=lambda: True)
    assert not target.exists()


def test_work_copy_recovers_committed_wal_without_mutating_baseline_or_source(tmp_path):
    from tools.personal_data_trial import copy_baseline, prepare_work
    source = source_tree(tmp_path)
    db = source / "data/chat_history/alice.db"
    subprocess.run([sys.executable, "-c", "import sqlite3,sys,os; c=sqlite3.connect(sys.argv[1]); c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA wal_autocheckpoint=0'); c.execute('CREATE TABLE records(id INTEGER)'); c.execute('INSERT INTO records VALUES(42)'); c.commit(); os._exit(0)", str(db)], check=True)
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    baseline, work = tmp_path / "baseline", tmp_path / "work"
    copy_baseline(source, baseline, source_is_quiescent=lambda: True)
    saved = {p.relative_to(baseline): p.read_bytes() for p in baseline.rglob("*") if p.is_file()}
    result = prepare_work(baseline, work)
    assert result["sqlite_checked"] == 1
    connection = sqlite3.connect((work / db.relative_to(source)).as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        assert connection.execute("SELECT id FROM records").fetchall() == [(42,)]
    finally:
        connection.close()
    assert saved == {p.relative_to(baseline): p.read_bytes() for p in baseline.rglob("*") if p.is_file()}
    assert before == {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}


def test_corrupt_work_database_is_not_published_as_ready(tmp_path):
    from tools.personal_data_trial import copy_baseline, prepare_work
    source = source_tree(tmp_path)
    (source / "data/memory/broken.db").write_bytes(b"broken")
    baseline, work = tmp_path / "baseline", tmp_path / "work"
    copy_baseline(source, baseline, source_is_quiescent=lambda: True)
    with pytest.raises((sqlite3.Error, PersonalCopyError)):
        prepare_work(baseline, work)
    assert json.loads((work / STATE_FILE).read_text())["state"] == "incomplete"


@pytest.mark.parametrize("invalid", ["active", "existing", "inside_source"])
def test_baseline_refuses_unsafe_targets_and_active_source_without_changes(tmp_path, invalid):
    from tools.personal_data_trial import copy_baseline
    source = source_tree(tmp_path)
    target = source / "nested" if invalid == "inside_source" else tmp_path / "target"
    if invalid == "existing":
        target.mkdir()
        (target / "keep").write_bytes(b"protected")
    with pytest.raises(PersonalCopyError):
        copy_baseline(source, target, source_is_quiescent=lambda: invalid != "active")
    if invalid == "existing":
        assert list(target.iterdir()) == [target / "keep"]
        assert (target / "keep").read_bytes() == b"protected"
    else:
        assert not target.exists()
