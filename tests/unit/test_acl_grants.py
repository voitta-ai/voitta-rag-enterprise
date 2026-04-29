"""Unit tests for the folder ACL helpers + users.txt seeder."""

from __future__ import annotations

from pathlib import Path

from voitta_image_rag.db.database import init_db, session_scope
from voitta_image_rag.db.models import Folder, FolderAcl, User
from voitta_image_rag.services.acl import (
    allowed_user_ids_for_file,
    folder_user_ids,
    grant_folder,
    public_user_ids,
    revoke_folder,
    seed_users_from_file,
    user_can_see_folder,
    visible_folder_ids,
)


def _make_folder(s, path: str, name: str) -> int:
    f = Folder(path=path, display_name=name)
    s.add(f)
    s.flush()
    return f.id


def _make_user(s, email: str) -> int:
    u = User(email=email)
    s.add(u)
    s.flush()
    return u.id


def test_grant_then_can_see(env: None) -> None:
    init_db()
    with session_scope() as s:
        fid = _make_folder(s, "/p", "p")
        uid = _make_user(s, "alice@x")
        grant_folder(s, fid, uid)
    with session_scope() as s:
        assert user_can_see_folder(s, fid, uid)


def test_grant_idempotent(env: None) -> None:
    init_db()
    with session_scope() as s:
        fid = _make_folder(s, "/p", "p")
        uid = _make_user(s, "alice@x")
        grant_folder(s, fid, uid)
        grant_folder(s, fid, uid)
        grant_folder(s, fid, uid)
    with session_scope() as s:
        assert s.query(FolderAcl).count() == 1


def test_revoke_removes_grant(env: None) -> None:
    init_db()
    with session_scope() as s:
        fid = _make_folder(s, "/p", "p")
        uid = _make_user(s, "alice@x")
        grant_folder(s, fid, uid)
        revoke_folder(s, fid, uid)
    with session_scope() as s:
        assert not user_can_see_folder(s, fid, uid)


def test_revoke_unknown_is_noop(env: None) -> None:
    init_db()
    with session_scope() as s:
        fid = _make_folder(s, "/p", "p")
        uid = _make_user(s, "alice@x")
        revoke_folder(s, fid, uid)  # not granted
    with session_scope() as s:
        assert s.query(FolderAcl).count() == 0


def test_visible_folder_ids(env: None) -> None:
    init_db()
    with session_scope() as s:
        f1 = _make_folder(s, "/a", "a")
        f2 = _make_folder(s, "/b", "b")
        f3 = _make_folder(s, "/c", "c")
        u_alice = _make_user(s, "alice@x")
        u_bob = _make_user(s, "bob@x")
        grant_folder(s, f1, u_alice)
        grant_folder(s, f3, u_alice)
        grant_folder(s, f2, u_bob)
    with session_scope() as s:
        assert sorted(visible_folder_ids(s, u_alice)) == sorted([f1, f3])
        assert sorted(visible_folder_ids(s, u_bob)) == [f2]


def test_folder_user_ids_returns_grant_set(env: None) -> None:
    init_db()
    with session_scope() as s:
        fid = _make_folder(s, "/p", "p")
        ua = _make_user(s, "a@x")
        ub = _make_user(s, "b@x")
        grant_folder(s, fid, ua)
        grant_folder(s, fid, ub)
    with session_scope() as s:
        assert sorted(folder_user_ids(s, fid)) == sorted([ua, ub])


def test_allowed_user_ids_for_file_inherits_from_folder(env: None) -> None:
    init_db()
    from voitta_image_rag.db.models import File

    with session_scope() as s:
        fid = _make_folder(s, "/p", "p")
        ua = _make_user(s, "a@x")
        ub = _make_user(s, "b@x")
        grant_folder(s, fid, ua)
        grant_folder(s, fid, ub)
        file = File(folder_id=fid, rel_path="x.md", state="pending", last_seen_at=0)
        s.add(file)
        s.flush()
        file_id = file.id
    with session_scope() as s:
        assert sorted(allowed_user_ids_for_file(s, file_id)) == sorted([ua, ub])


def test_allowed_user_ids_for_unknown_file(env: None) -> None:
    init_db()
    with session_scope() as s:
        assert allowed_user_ids_for_file(s, 999) == []


def test_public_user_ids_returns_all(env: None) -> None:
    init_db()
    with session_scope() as s:
        a = _make_user(s, "a@x")
        b = _make_user(s, "b@x")
    with session_scope() as s:
        assert sorted(public_user_ids(s)) == sorted([a, b])


def test_seed_users_from_file_creates_users(env: None, tmp_path: Path) -> None:
    init_db()
    p = tmp_path / "users.txt"
    p.write_text("alice@x\nbob@x\n# a comment\n\ncarol@x\n")
    with session_scope() as s:
        added = seed_users_from_file(s, p)
    assert added == 3
    with session_scope() as s:
        emails = sorted(u.email for u in s.query(User).all())
        assert emails == ["alice@x", "bob@x", "carol@x"]


def test_seed_users_idempotent(env: None, tmp_path: Path) -> None:
    init_db()
    p = tmp_path / "users.txt"
    p.write_text("alice@x\n")
    with session_scope() as s:
        seed_users_from_file(s, p)
    with session_scope() as s:
        added = seed_users_from_file(s, p)
    assert added == 0


def test_seed_users_missing_file(env: None, tmp_path: Path) -> None:
    init_db()
    with session_scope() as s:
        assert seed_users_from_file(s, tmp_path / "absent.txt") == 0
