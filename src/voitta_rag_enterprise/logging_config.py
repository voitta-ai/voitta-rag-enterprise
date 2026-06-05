"""Logging configuration with per-job context injection.

All application logging is routed to rotating files under
``<data_dir>/logs/`` — nothing of ours hits the console:

* ``indexing.log`` (DEBUG) — every record from the ``voitta_rag_enterprise``
  package, used to diagnose extract / embed / sync failures.
* ``app.log`` (INFO) — root-logger catch-all that also captures third-party
  loggers (mineru, transformers, huggingface_hub, qdrant_client, …).

The terminal stays quiet so uvicorn's startup banner and access logs are
the only things in the user's screen session.

Every log record gets a ``ctx`` field populated from the ``_ctx``
ContextVar. Indexing code uses :func:`bind_context` to attach ``file_id`` /
``job_id``, so a single grep pulls out everything that happened for one
file across worker threads.

A few notoriously chatty third-party loggers are pinned to WARNING so the
file doesn't fill up with model-loader trivia. ``HF_HUB_DISABLE_PROGRESS_BARS``
suppresses the tqdm bars Hugging Face writes directly to stderr (they
bypass Python ``logging`` entirely).
"""

from __future__ import annotations

import logging
import logging.config
import os
import sys
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


def current_context_value(key: str) -> Any:
    """Read one field from the active bound context (e.g. ``job_id``).

    Lets code deep in the extract pipeline emit job-scoped progress without
    threading the job id through every call — the worker binds ``job_id`` once
    around the handler. Returns None when unset.
    """
    ctx = _ctx.get()
    return ctx.get(key) if ctx else None


_NOISY_THIRD_PARTY = (
    "mineru",
    "transformers",
    "huggingface_hub",
    "tokenizers",
    "filelock",
    "urllib3",
    "fsspec",
    "PIL",
    "qdrant_client",
)


def setup_logging(log_dir: Path, level: str | None = None) -> None:
    """Install file-only handlers for our app + the root logger.

    Idempotent: re-running replaces the configuration. The handlers write
    under ``log_dir`` which is created on demand. ``level`` (or
    ``VOITTA_LOG_LEVEL``) controls the root level only; the
    ``voitta_rag_enterprise`` tree is always DEBUG so we never lose detail.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    root_level = (level or os.environ.get("VOITTA_LOG_LEVEL") or "INFO").upper()

    # HF model downloads use tqdm written directly to stderr; only this env
    # var (set before any HF import runs) makes them quiet.
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    # MinerU pulls in tqdm for layout/OCR/formula progress bars and writes
    # them straight to stderr too; this env var disables every tqdm bar in
    # the process (mineru, transformers, sentence-transformers, …).
    os.environ.setdefault("TQDM_DISABLE", "1")

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
            "app_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "level": root_level,
                "formatter": "default",
                "filters": ["ctx"],
                "filename": str(log_dir / "app.log"),
                "maxBytes": 10 * 1024 * 1024,
                "backupCount": 5,
                "encoding": "utf-8",
            },
        },
        "loggers": {
            "voitta_rag_enterprise": {
                "level": "DEBUG",
                "handlers": ["indexing_file"],
                "propagate": False,
            },
            **{
                name: {"level": "WARNING", "handlers": [], "propagate": True}
                for name in _NOISY_THIRD_PARTY
            },
        },
        "root": {
            "level": root_level,
            "handlers": ["app_file"],
        },
    }
    logging.config.dictConfig(config)
    _strip_console_handlers()
    _redirect_loguru(log_dir)


def _redirect_loguru(log_dir: Path) -> None:
    """Point ``loguru`` (used by mineru) at our log dir instead of stderr.

    loguru does not honour Python's ``logging`` config — it has its own
    sink registry — so it must be reconfigured separately. We remove the
    default stderr sink and add a single rotating file sink at WARNING.
    Quietly no-ops if loguru isn't installed (e.g. running without mineru).
    """
    try:
        from loguru import logger as loguru_logger
    except ImportError:
        return
    try:
        loguru_logger.remove()
        loguru_logger.add(
            str(log_dir / "mineru.log"),
            level="WARNING",
            rotation="10 MB",
            retention=5,
            enqueue=True,  # safe across worker threads
        )
    except Exception:
        # loguru API differences across versions — never let logging
        # config crash the process.
        pass


def _strip_console_handlers() -> None:
    """Remove StreamHandlers that point at stdout/stderr from every logger.

    uvicorn / mineru / transformers commonly call ``logging.basicConfig`` or
    install their own ``StreamHandler`` on import — those run *before* our
    ``setup_logging`` does, and ``disable_existing_loggers=False`` keeps them
    in place. We walk the active logger registry once and unplug any console
    handler so the only sink is the rotating file. uvicorn's own ``uvicorn``,
    ``uvicorn.access``, ``uvicorn.error`` loggers are spared so the startup
    banner remains visible.
    """
    keep_prefixes = ("uvicorn",)
    visited: set[int] = set()
    candidates: list[logging.Logger] = [logging.getLogger()]
    candidates.extend(logging.Logger.manager.loggerDict.values())  # type: ignore[arg-type]
    for logger in candidates:
        if not isinstance(logger, logging.Logger):
            continue
        if id(logger) in visited:
            continue
        visited.add(id(logger))
        if any(logger.name == p or logger.name.startswith(p + ".") for p in keep_prefixes):
            continue
        for h in list(logger.handlers):
            stream = getattr(h, "stream", None)
            if isinstance(h, logging.StreamHandler) and stream in (sys.stdout, sys.stderr):
                logger.removeHandler(h)
