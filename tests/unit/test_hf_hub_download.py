from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.hf_hub_download import download_hf_snapshot, iter_hf_endpoints


def test_iter_hf_endpoints_prefers_env_override(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("HF_ENDPOINT", "https://custom.example")
    assert iter_hf_endpoints() == ["https://custom.example"]


def test_iter_hf_endpoints_default_order_without_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    endpoints = iter_hf_endpoints()
    assert endpoints[0] == "https://huggingface.co"
    assert "https://hf-mirror.com" in endpoints


def test_download_hf_snapshot_falls_back_to_second_endpoint(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    calls: list[str] = []

    def fake_snapshot_download(*, endpoint: str, **kwargs: object) -> str:
        calls.append(endpoint)
        if endpoint == "https://huggingface.co":
            raise RuntimeError("primary failed")
        return "/tmp/model"

    with patch("huggingface_hub.snapshot_download", side_effect=fake_snapshot_download):
        path = download_hf_snapshot("BAAI/bge-small-zh-v1.5", tmp_path)

    assert path == "/tmp/model"
    assert calls == ["https://huggingface.co", "https://hf-mirror.com"]
