"""services/groups.py — membership helpers."""

from __future__ import annotations

from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import Group, User, UserGroup
from voitta_rag_enterprise.services import groups as g


def _mk_user(s, email: str) -> int:
    u = User(email=email)
    s.add(u)
    s.flush()
    return u.id


def test_get_or_create_group_is_idempotent(env: None) -> None:
    init_db()
    with session_scope() as s:
        a = g.get_or_create_group(s, "eng")
        b = g.get_or_create_group(s, "  eng  ")  # trimmed → same row
        assert a.id == b.id
        assert s.query(Group).count() == 1


def test_set_user_groups_creates_and_replaces(env: None) -> None:
    init_db()
    with session_scope() as s:
        uid = _mk_user(s, "alice@x.com")
        # create-on-the-fly + dedup + blanks ignored
        g.set_user_groups(s, uid, ["eng", "eng", "  ", "leads"])
        assert g.group_names_for_user(s, uid) == ["eng", "leads"]
        assert s.query(Group).count() == 2

        # replace semantics: drop leads, add sales (sales created)
        g.set_user_groups(s, uid, ["eng", "sales"])
        assert g.group_names_for_user(s, uid) == ["eng", "sales"]
        # 'leads' group still exists (groups managed explicitly), membership gone
        assert s.query(Group).count() == 3
        assert g.group_member_ids(s, g.get_or_create_group(s, "leads").id) == []


def test_add_remove_member_idempotent(env: None) -> None:
    init_db()
    with session_scope() as s:
        uid = _mk_user(s, "bob@x.com")
        gid = g.get_or_create_group(s, "eng").id
        g.add_member(s, gid, uid)
        g.add_member(s, gid, uid)  # idempotent
        assert g.group_member_ids(s, gid) == [uid]
        g.remove_member(s, gid, uid)
        assert g.group_member_ids(s, gid) == []


def test_deleting_user_cascades_membership(env: None) -> None:
    init_db()
    with session_scope() as s:
        uid = _mk_user(s, "carol@x.com")
        g.set_user_groups(s, uid, ["eng"])
        gid = g.get_or_create_group(s, "eng").id
    with session_scope() as s:
        s.delete(s.get(User, uid))
    with session_scope() as s:
        assert s.get(UserGroup, (uid, gid)) is None
        assert s.get(Group, gid) is not None  # group survives


def test_deleting_group_cascades_membership(env: None) -> None:
    init_db()
    with session_scope() as s:
        uid = _mk_user(s, "dave@x.com")
        g.set_user_groups(s, uid, ["eng"])
        gid = g.get_or_create_group(s, "eng").id
    with session_scope() as s:
        s.delete(s.get(Group, gid))
    with session_scope() as s:
        assert s.get(UserGroup, (uid, gid)) is None
        assert s.get(User, uid) is not None  # user survives


def test_list_groups_with_counts(env: None) -> None:
    init_db()
    with session_scope() as s:
        u1 = _mk_user(s, "e1@x.com")
        u2 = _mk_user(s, "e2@x.com")
        g.set_user_groups(s, u1, ["eng", "leads"])
        g.set_user_groups(s, u2, ["eng"])
    with session_scope() as s:
        rows = {r["name"]: r["member_count"] for r in g.list_groups_with_counts(s)}
        assert rows == {"eng": 2, "leads": 1}


def test_group_names_by_user_bulk(env: None) -> None:
    init_db()
    with session_scope() as s:
        u1 = _mk_user(s, "f1@x.com")
        u2 = _mk_user(s, "f2@x.com")
        g.set_user_groups(s, u1, ["eng", "sales"])
        g.set_user_groups(s, u2, [])
    with session_scope() as s:
        m = g.group_names_by_user(s)
        assert m == {u1: ["eng", "sales"]}  # u2 absent (no memberships)
