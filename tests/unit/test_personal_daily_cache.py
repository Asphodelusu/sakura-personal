"""Daily setup owns the process-local BM25 cache only while that runtime lives."""
import os
from types import SimpleNamespace

import pytest

from plugins.builtin.sakura_mem0.plugin import (
    PersonalDailyPlugin,
    SakuraMem0Plugin,
)
from test_personal_plugin_runtime import Context

_CACHE = "FASTEMBED_CACHE_PATH"
_OFFLINE = "HF_HUB_OFFLINE"


class _Host(Context):
    def __init__(self, root):
        super().__init__(root)
        self.events = {}
        self.config = SimpleNamespace(get=lambda: {"personalSnapshot": str(root / "snapshot")})

    def on(self, name, callback):
        self.events[name] = callback

    def get(self, name):
        if name == "sakura.host.model_slots":
            return SimpleNamespace(catalog=lambda: [], resolve=lambda _selection: {}, register=lambda *_a, **_kw: None)
        if name == "sakura.host.timeline":
            return SimpleNamespace()
        return super().get(name)


def _close(context):
    while context.effects:
        context.effects.pop()()


def _boundary(monkeypatch, on_init):
    class Boundary:
        def __init__(self, *args, **kwargs):
            on_init(self, args, kwargs)

        def close(self):
            self.closed_env = (os.environ.get(_CACHE), os.environ.get(_OFFLINE))

        def note_timeline_changed(self, _timeline):
            return None

        def search_memory(self, _arguments, *, wait=False):
            return {"status": "loading", "memories": []}

        def core_profile_fragment(self):
            return None

    monkeypatch.setattr(
        "plugins.builtin.sakura_mem0.personal_runtime.PersonalRecallBoundary",
        Boundary,
    )
    return Boundary


def _env(monkeypatch, cache="kept-fastembed-cache", offline="0"):
    if cache is None:
        monkeypatch.delenv(_CACHE, raising=False)
    else:
        monkeypatch.setenv(_CACHE, cache)
    if offline is None:
        monkeypatch.delenv(_OFFLINE, raising=False)
    else:
        monkeypatch.setenv(_OFFLINE, offline)


def test_daily_setup_points_fastembed_at_host_cache_until_context_closes(tmp_path, monkeypatch):
    observed = []
    closed = []

    def on_init(boundary, args, kwargs):
        observed.append((os.environ.get(_CACHE), os.environ.get(_OFFLINE), kwargs.get("daily")))
        boundary.closed_env = None
        original = boundary.close

        def close():
            closed.append((os.environ.get(_CACHE), os.environ.get(_OFFLINE)))
            original()

        boundary.close = close

    _boundary(monkeypatch, on_init)
    _env(monkeypatch)
    root = tmp_path / "work"
    context = _Host(root)
    expected = str(root / "cache" / "memory" / "bm25")

    PersonalDailyPlugin().setup(context)

    assert observed == [(expected, "1", True)]
    assert os.environ[_CACHE] == expected
    assert os.environ[_OFFLINE] == "1"
    assert not (root / "cache" / "memory" / "bm25").exists()
    _close(context)
    assert closed == [(expected, "1")]
    assert os.environ[_CACHE] == "kept-fastembed-cache"
    assert os.environ[_OFFLINE] == "0"


def test_daily_setup_failure_restores_previous_env(tmp_path, monkeypatch):
    observed = []

    def on_init(_boundary, _args, _kwargs):
        observed.append((os.environ.get(_CACHE), os.environ.get(_OFFLINE)))
        raise ValueError("PERSONAL_DAILY_ADMISSION")

    _boundary(monkeypatch, on_init)
    _env(monkeypatch, cache=None, offline=None)
    root = tmp_path / "work"
    context = _Host(root)
    with pytest.raises(ValueError, match="PERSONAL_DAILY_ADMISSION"):
        try:
            PersonalDailyPlugin().setup(context)
        except ValueError:
            _close(context)
            raise
    assert observed == [(str(root / "cache" / "memory" / "bm25"), "1")]
    assert os.environ.get(_CACHE) is None
    assert os.environ.get(_OFFLINE) is None


def test_default_recall_and_rehearsal_leave_bm25_env_unchanged(tmp_path, monkeypatch):
    seen = []

    def on_init(_boundary, _args, kwargs):
        seen.append((kwargs.get("daily"), os.environ.get(_CACHE), os.environ.get(_OFFLINE)))

    _boundary(monkeypatch, on_init)
    _env(monkeypatch)
    root = tmp_path / "work"
    snapshot = root / "snapshot"

    recall = _Host(root)
    SakuraMem0Plugin(personal_snapshot=snapshot).setup(recall)
    rehearsal = _Host(root)
    SakuraMem0Plugin(personal_snapshot=snapshot, personal_write_rehearsal=True).setup(rehearsal)

    def factory(_context):
        seen.append(("default", os.environ.get(_CACHE), os.environ.get(_OFFLINE)))
        raise RuntimeError("DEFAULT_SETUP_STOPPED")

    default = _Host(root)
    with pytest.raises(RuntimeError, match="DEFAULT_SETUP_STOPPED"):
        SakuraMem0Plugin(runtime_factory=factory).setup(default)

    assert seen == [
        (False, "kept-fastembed-cache", "0"),
        (False, "kept-fastembed-cache", "0"),
        ("default", "kept-fastembed-cache", "0"),
    ]
    _close(recall)
    _close(rehearsal)
    _close(default)
    assert os.environ[_CACHE] == "kept-fastembed-cache"
    assert os.environ[_OFFLINE] == "0"
