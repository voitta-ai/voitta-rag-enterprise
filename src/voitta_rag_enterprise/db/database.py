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
            connect_args={"check_same_thread": False},
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
        cur.close()
