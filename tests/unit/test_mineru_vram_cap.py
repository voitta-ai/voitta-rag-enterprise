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


def test_render_knobs_default_to_one_process_and_a_timeout_below_ours(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    for k in ("MINERU_PDF_RENDER_THREADS", "MINERU_PDF_RENDER_TIMEOUT"):
        monkeypatch.delenv(k, raising=False)
    reset_settings_cache()
    spawned = _spawn_env(monkeypatch)
    assert spawned["MINERU_PDF_RENDER_THREADS"] == "1"
    # Default caps timeout is 600 s; MinerU's own 300 s default already sits
    # below it, so it is kept.
    assert spawned["MINERU_PDF_RENDER_TIMEOUT"] == "300"


def test_render_timeout_follows_a_short_parse_timeout(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from voitta_rag_enterprise.services import indexing_caps

    monkeypatch.delenv("MINERU_PDF_RENDER_TIMEOUT", raising=False)
    monkeypatch.setattr(
        pdf_parser, "get_caps", lambda: indexing_caps.IndexingCaps(pdf_parse_timeout_s=100)
    )
    assert _spawn_env(monkeypatch)["MINERU_PDF_RENDER_TIMEOUT"] == "80"


def test_darwin_parses_one_page_at_a_time(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINERU_PROCESSING_WINDOW_SIZE", raising=False)
    monkeypatch.delenv("MINERU_MIN_BATCH_INFERENCE_SIZE", raising=False)
    monkeypatch.setattr(pdf_parser.sys, "platform", "darwin")
    spawned = _spawn_env(monkeypatch)
    assert spawned["MINERU_PROCESSING_WINDOW_SIZE"] == "1"
    # The MinerU 2.x knob is gone from MinerU and from us.
    assert "MINERU_MIN_BATCH_INFERENCE_SIZE" not in spawned
