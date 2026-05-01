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
    """Apply ``schema.sql`` plus idempotent ALTER migrations for older DBs."""
    engine = get_engine()
    sql = SCHEMA_PATH.read_text()
    raw_conn: sqlite3.Connection = engine.raw_connection()
    try:
        raw_conn.executescript(sql)
        _apply_migrations(raw_conn)
        raw_conn.commit()
    finally:
        raw_conn.close()


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply additive column migrations that ``CREATE TABLE IF NOT EXISTS`` skips
    on existing databases. Each ALTER is wrapped to ignore the "duplicate
    column" error so the function is idempotent.
    """
    cur = conn.cursor()
    try:
        for stmt in (
            "ALTER TABLE files ADD COLUMN embed_round INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE files ADD COLUMN tab TEXT",
            "ALTER TABLE folder_sync_sources ADD COLUMN gd_client_id TEXT",
            "ALTER TABLE folder_sync_sources ADD COLUMN gd_client_secret TEXT",
            "ALTER TABLE folder_sync_sources ADD COLUMN gd_refresh_token TEXT",
            "ALTER TABLE folder_sync_sources ADD COLUMN gd_service_account_json TEXT",
            "ALTER TABLE folder_sync_sources ADD COLUMN gd_folder_id TEXT",
            "ALTER TABLE folders ADD COLUMN owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL",
            "ALTER TABLE folders ADD COLUMN shared INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                cur.execute(stmt)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise

        # Backfill ownership for legacy folders. Pick the lowest user_id from
        # folder_acl (deterministic across restarts) when no owner is set.
        # Folders with no folder_acl rows at all stay NULL — they're already
        # invisible to everyone in multi-user mode.
        cur.execute(
            """
            UPDATE folders
               SET owner_id = (
                   SELECT MIN(user_id) FROM folder_acl WHERE folder_id = folders.id
               )
             WHERE owner_id IS NULL
            """
        )
    finally:
        cur.close()


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
