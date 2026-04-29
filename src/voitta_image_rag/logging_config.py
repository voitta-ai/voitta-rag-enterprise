"""Logging configuration with per-job context injection.

Two handlers are wired up on the ``voitta_image_rag`` logger:

* console at ``VOITTA_LOG_LEVEL`` (default ``INFO``) — the normal stderr stream
  uvicorn already prints to.
* a rotating file at ``<data_dir>/logs/indexing.log`` at ``DEBUG`` — the trail
  used to diagnose extract / embed failures.

Every log record gets a ``ctx`` field populated from the ``_ctx`` ContextVar.
Indexing code uses :func:`bind_context` to attach ``file_id`` / ``job_id``, so a
single grep pulls out everything that happened for one file across worker
threads.
"""

from __future__ import annotations

import logging
import logging.config
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

_ctx: ContextVar[dict[str, Any] | None] = ContextVar("voitta_log_ctx", default=None)


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        ctx = _ctx.get()
        if ctx:
            record.ctx = "[" + " ".join(f"{k}={v}" for k, v in ctx.items()) + "] "
        else:
            record.ctx = ""
        return True


@contextmanager
def bind_context(**fields: Any):
    """Attach key/value pairs to every log record emitted in this scope."""
    current = dict(_ctx.get() or {})
    current.update(fields)
    token = _ctx.set(current)
    try:
        yield
    finally:
        _ctx.reset(token)


def setup_logging(log_dir: Path, level: str | None = None) -> None:
    """Install handlers for the ``voitta_image_rag`` logger tree.

    Idempotent: re-running replaces the configuration. The file handler writes
    under ``log_dir`` which is created on demand.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    console_level = (level or os.environ.get("VOITTA_LOG_LEVEL") or "INFO").upper()
    config: dict[str, Any] = {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "ctx": {"()": _ContextFilter},
        },
        "formatters": {
            "default": {
                "format": "%(asctime)s %(levelname)-7s %(name)s %(ctx)s%(message)s",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": console_level,
                "formatter": "default",
                "filters": ["ctx"],
            },
            "indexing_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "level": "DEBUG",
                "formatter": "default",
                "filters": ["ctx"],
                "filename": str(log_dir / "indexing.log"),
                "maxBytes": 10 * 1024 * 1024,
                "backupCount": 5,
                "encoding": "utf-8",
            },
        },
        "loggers": {
            "voitta_image_rag": {
                "level": "DEBUG",
                "handlers": ["console", "indexing_file"],
                "propagate": False,
            },
        },
    }
    logging.config.dictConfig(config)
