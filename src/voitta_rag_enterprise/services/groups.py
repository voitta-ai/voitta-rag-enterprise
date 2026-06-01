"""User-group membership helpers.

Organizational groups only — membership has no effect on folder visibility (a
later feature can layer group-based folder grants on top of ``user_groups``).
Mirrors the helper style of :mod:`services.acl` (plain functions over a passed
``Session``; the caller owns the transaction).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import Group, User, UserGroup


def get_or_create_group(session: Session, name: str) -> Group:
    """Return the group named ``name``, creating it if missing.

    Names are matched/stored trimmed. The DB enforces uniqueness; we look up
    first so concurrent create-on-the-fly from two admins resolves to the same
    row rather than a 500.
    """
    clean = name.strip()
    grp = session.execute(
        select(Group).where(Group.name == clean)
    ).scalar_one_or_none()
    if grp is None:
        grp = Group(name=clean)
        session.add(grp)
        session.flush()
    return grp


def set_user_groups(session: Session, user_id: int, names: list[str]) -> None:
    """Replace ``user_id``'s membership with exactly the groups in ``names``.

    Missing groups are created on the fly. Blank/duplicate names are ignored.
    Removing the last member never deletes the group itself (groups are managed
    explicitly on the Groups tab).
    """
    wanted_names = {n.strip() for n in names if n and n.strip()}
    target_ids = {get_or_create_group(session, n).id for n in wanted_names}

    current = {
        row.group_id
        for row in session.execute(
            select(UserGroup).where(UserGroup.user_id == user_id)
        ).scalars()
    }
    for gid in target_ids - current:
        session.add(UserGroup(user_id=user_id, group_id=gid))
    for gid in current - target_ids:
        row = session.get(UserGroup, (user_id, gid))
        if row is not None:
            session.delete(row)


def add_member(session: Session, group_id: int, user_id: int) -> None:
    """Idempotent add of ``user_id`` to ``group_id``."""
    if session.get(UserGroup, (user_id, group_id)) is None:
        session.add(UserGroup(user_id=user_id, group_id=group_id))


def remove_member(session: Session, group_id: int, user_id: int) -> None:
    row = session.get(UserGroup, (user_id, group_id))
    if row is not None:
        session.delete(row)


def group_names_for_user(session: Session, user_id: int) -> list[str]:
    """Sorted group names a user belongs to."""
    rows = session.execute(
        select(Group.name)
        .join(UserGroup, UserGroup.group_id == Group.id)
        .where(UserGroup.user_id == user_id)
        .order_by(Group.name)
    ).scalars()
    return list(rows)


def group_names_by_user(session: Session) -> dict[int, list[str]]:
    """``{user_id: [group_name, …]}`` for every membership, in one query.

    Bulk variant for ``build_admin_state`` so the users list doesn't issue one
    membership query per user (same anti-N+1 pattern as the jobs image_count).
    """
    out: dict[int, list[str]] = {}
    rows = session.execute(
        select(UserGroup.user_id, Group.name)
        .join(Group, Group.id == UserGroup.group_id)
        .order_by(Group.name)
    ).all()
    for uid, name in rows:
        out.setdefault(uid, []).append(name)
    return out


def list_groups_with_counts(session: Session) -> list[dict]:
    """``[{id, name, description, member_count}]`` ordered by name."""
    counts = dict(
        session.execute(
            select(UserGroup.group_id, func.count()).group_by(UserGroup.group_id)
        ).all()
    )
    groups = session.execute(select(Group).order_by(Group.name)).scalars().all()
    return [
        {
            "id": g.id,
            "name": g.name,
            "description": g.description,
            "member_count": counts.get(g.id, 0),
        }
        for g in groups
    ]


def group_member_ids(session: Session, group_id: int) -> list[int]:
    """User ids in a group (for the Groups-tab member panel)."""
    return list(
        session.execute(
            select(UserGroup.user_id).where(UserGroup.group_id == group_id)
        ).scalars()
    )


# Re-exported for symmetry with acl helpers; callers may want User directly.
__all__ = [
    "get_or_create_group",
    "set_user_groups",
    "add_member",
    "remove_member",
    "group_names_for_user",
    "group_names_by_user",
    "list_groups_with_counts",
    "group_member_ids",
    "User",
]
