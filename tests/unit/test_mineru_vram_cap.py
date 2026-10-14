"""VOITTA_MINERU_VRAM_GB reaches the MinerU subprocess as MINERU_VIRTUAL_VRAM_SIZE.

Regression: on a GPU shared with other services, MinerU sized its batches for
the whole 24 GB card (batch ratio 8) and PDFs failed with CUDA out of memory.
"""

from __future__ import annotations

import pytest

from voitta_rag_enterprise.config import reset_settings_cache
from voitta_rag_enterprise.services.parsers import pdf_parser


def _spawn_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    captured: dict[str, dict[str, str]] = {}

    class FakePopen:
        def __init__(self, *args, env=None, **kwargs):
            captured["env"] = env
            self.stderr = None
            self.pid = 0

    monkeypatch.setattr(pdf_parser.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(pdf_parser.threading, "Thread", lambda *a, **k: type("T", (), {"start": lambda self: None})())
    pdf_parser._MineruDaemon()._spawn()
    return captured["env"]


def test_cap_passed_to_subprocess(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINERU_VIRTUAL_VRAM_SIZE", raising=False)
    monkeypatch.setenv("VOITTA_MINERU_VRAM_GB", "6")
    reset_settings_cache()
    assert _spawn_env(monkeypatch)["MINERU_VIRTUAL_VRAM_SIZE"] == "6"


def test_default_leaves_mineru_autodetect(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINERU_VIRTUAL_VRAM_SIZE", raising=False)
    reset_settings_cache()
    assert "MINERU_VIRTUAL_VRAM_SIZE" not in _spawn_env(monkeypatch)


def test_explicit_mineru_env_wins(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINERU_VIRTUAL_VRAM_SIZE", "4")
    monkeypatch.setenv("VOITTA_MINERU_VRAM_GB", "6")
    reset_settings_cache()
    assert _spawn_env(monkeypatch)["MINERU_VIRTUAL_VRAM_SIZE"] == "4"
