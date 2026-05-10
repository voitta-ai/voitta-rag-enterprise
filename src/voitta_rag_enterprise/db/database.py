"""Database engine, session factory, and one-shot init."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import get_settings

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """Lazy-init the engine. Sets WAL + foreign-key pragmas on every connection."""
    global _engine, _SessionLocal
    if _engine is None:
        settings = get_settings()
        db_path = settings.resolved_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            f"sqlite:///{db_path}",
            # ``timeout`` is the SQLite busy-timeout (seconds): when a
            # writer finds the DB locked, it waits up to this long for the
            # holder to commit instead of erroring instantly. The Python
            # sqlite3 default of 5s is not enough for our worker —
            # ``_commit_indexing`` writes hundreds of rows in one
            # transaction (chunks + figures + page renders for a long
            # PDF), and the watcher thread that races a fresh-file INSERT
            # into the same window otherwise hits "database is locked".
            connect_args={"check_same_thread": False, "timeout": 30},
            # Pool sizing — see Settings.db_pool_* for the rationale.
            # Default SQLAlchemy QueuePool is 5+10 which is too small for
            # our watcher/worker/REST/WS concurrency under any real load.
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_pool_max_overflow,
            pool_recycle=settings.db_pool_recycle_seconds,
            pool_pre_ping=settings.db_pool_pre_ping,
            future=True,
        )
        _register_pragmas(_engine)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    if _SessionLocal is None:
        get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """Auto-commit on success, rollback on exception."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """Apply ``schema.sql`` and run any forward-only migrations.

    The migration block only handles columns we add to existing tables.
    SQLite's ``ADD COLUMN`` is idempotent here because we check
    ``PRAGMA table_info`` first, so running this on a fresh DB or an
    already-migrated DB is a no-op.
    """
    engine = get_engine()
    sql = SCHEMA_PATH.read_text()
    raw_conn: sqlite3.Connection = engine.raw_connection()
    try:
        raw_conn.executescript(sql)
        _ensure_column(raw_conn, "users", "is_admin", "INTEGER NOT NULL DEFAULT 0")
        # 'figure' default keeps every legacy row classified as a cropped
        # extract; 'page_render' is reserved for the per-page WebP rasters
        # the PDF parser now emits.
        _ensure_column(raw_conn, "images", "kind", "TEXT NOT NULL DEFAULT 'figure'")
        raw_conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_images_kind ON images(file_id, kind, page)"
        )
        raw_conn.commit()
    finally:
        raw_conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """Add ``column`` to ``table`` if it isn't already there.

    SQLite supports ALTER TABLE ADD COLUMN with a default value; that's
    enough for the columns we add. Anything more complex (drop, rename,
    type change) would need the table-rebuild dance and we'd graduate to
    Alembic before reaching for that.
    """
    cur = conn.execute(f"PRAGMA table_info({table})")
    have = {row[1] for row in cur.fetchall()}
    if column in have:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def reset_engine_cache() -> None:
    """Test helper: dispose the engine, force re-init on next call."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def _register_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn: Any, _record: Any) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        # Belt-and-suspenders for the connect_args timeout above: if a
        # connection is opened through a path that doesn't honour the
        # SQLAlchemy connect_args (raw_connection() outside this engine,
        # for instance), this PRAGMA still wires the same wait.
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()
