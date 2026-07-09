"""The account model — ``users`` rows keyed (email, company_id).

A *person* is an email; an *account* is one (email, company_id) row.
``company_id=''`` is the Personal account. Data-plane authorization
(folder ACL) keys on account ids; person-level concepts (admin flag,
sign-in admission) key on the email across all its rows.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db.models import User

logger = logging.getLogger(__name__)


def get_or_create_user(
    session: Session, email: str, company_id: str = "", company_name: str = ""
) -> User:
    """Return the (email, company_id) ACCOUNT row, creating it if missing.

    Default ``company_id=''`` is the Personal account, which keeps every
    legacy call site working: dev/single-user shortcuts, seeding, and
    admin pre-creation all land on the Personal row. ``company_name`` is
    refreshed on every call when non-empty (Clerk renames propagate at
    login without forking accounts — identity is the org *id*).
    """
    user = session.execute(
        select(User).where(User.email == email, User.company_id == company_id)
    ).scalar_one_or_none()
    if user is None:
        user = User(email=email, company_id=company_id, company_name=company_name)
        session.add(user)
        session.flush()
    elif company_name and user.company_name != company_name:
        user.company_name = company_name
    return user


def accounts_for_email(session: Session, email: str) -> list[User]:
    """Every account row for ``email``, Personal first, then by company name."""
    rows = list(
        session.execute(
            select(User).where(User.email == email)
        ).scalars()
    )
    rows.sort(key=lambda u: (u.company_id != "", u.company_name.lower()))
    return rows


def offered_accounts_for_email(session: Session, email: str) -> list[User]:
    """The accounts a user may actually operate as (dropdown + switch).

    Natively-allowed emails (allowlist user/domain, super-admins) get every
    account, Personal first. Clerk-only users get **company accounts only**
    — their Personal row still exists in the DB as the stable anchor, but
    it is never offered. Fallback: a user with no company accounts (admin
    pre-created local row, dev shortcut) keeps Personal so the list is
    never empty.
    """
    from .. import admin_store

    accs = accounts_for_email(session, email)
    if admin_store.is_native_allowed(email) or admin_store.is_super_admin(email):
        return accs
    with_company = [a for a in accs if a.company_id]
    return with_company or accs


def default_account_for_email(session: Session, email: str) -> User:
    """Where a session lands with no (valid) active account selected."""
    offered = offered_accounts_for_email(session, email)
    if offered:
        return offered[0]
    return get_or_create_user(session, email)


def person_is_admin(session: Session, email: str) -> bool:
    """Person-level admin check: True when ANY of the email's accounts has
    the flag. Admin rights belong to the person, not the active company —
    a per-account flag would be confusing and provide no real isolation."""
    rows = session.execute(select(User.is_admin).where(User.email == email)).all()
    return any(bool(r[0]) for r in rows)


def stamp_person_admin(session: Session, email: str, is_admin: bool) -> None:
    """Set the admin flag on every account row of ``email``."""
    for u in session.execute(select(User).where(User.email == email)).scalars():
        u.is_admin = is_admin


def seed_users_from_file(session: Session, path: Path) -> int:
    """Create ``users`` rows for every email in ``path`` (one per line). Returns count added.

    Used at startup to bootstrap a new install with the addresses listed
    in ``users.txt``. The runtime sign-in gate no longer consults this
    file — see ``services/admin_store.py`` for the live allowlist.
    """
    if not path.exists():
        return 0
    added = 0
    for raw in path.read_text().splitlines():
        email = raw.strip()
        if not email or email.startswith("#"):
            continue
        existing = session.execute(
            select(User).where(User.email == email, User.company_id == "")
        ).scalar_one_or_none()
        if existing is None:
            session.add(User(email=email))  # Personal account (company_id='')
            added += 1
    if added:
        session.flush()
    return added


def public_user_ids(session: Session) -> list[int]:
    """All user ids known to the system. Used to grant a folder to everyone."""
    return [u.id for u in session.execute(select(User)).scalars()]
