"""ACL unit tests for the group- and email-share layers.

Visibility is the UNION of independent layers (owned / user grants /
email shares / group shares / community share). These tests pin the two
new layers and — critically — their independence: toggling one layer
never disturbs another.
"""

from __future__ import annotations

from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.db.models import (
    Folder,
    FolderEmailAcl,
    FolderGroupAcl,
    Group,
    User,
    UserGroup,
)
from voitta_rag_enterprise.services.acl import (
    user_can_see_folder,
    visible_folder_ids,
)


def _folder(s, path: str, name: str, owner_id: int | None = None) -> int:
    f = Folder(path=path, display_name=name, owner_id=owner_id)
    s.add(f)
    s.flush()
    return f.id


def _user(s, email: str) -> int:
    u = User(email=email)
    s.add(u)
    s.flush()
    return u.id


def _group(s, name: str, *member_ids: int) -> int:
    g = Group(name=name)
    s.add(g)
    s.flush()
    for uid in member_ids:
        s.add(UserGroup(user_id=uid, group_id=g.id))
    s.flush()
    return g.id


# ---------------------------------------------------------------------------
# Email shares
# ---------------------------------------------------------------------------


def test_email_share_grants_visibility(env: None) -> None:
    init_db()
    with session_scope() as s:
        owner = _user(s, "owner@x")
        viewer = _user(s, "viewer@x")
        other = _user(s, "other@x")
        fid = _folder(s, "/p", "p", owner)
        s.add(FolderEmailAcl(folder_id=fid, email="viewer@x"))
    with session_scope() as s:
        assert fid in visible_folder_ids(s, viewer)
        assert user_can_see_folder(s, fid, viewer)
        assert fid not in visible_folder_ids(s, other)
        assert not user_can_see_folder(s, fid, other)


def test_email_share_is_case_insensitive(env: None) -> None:
    """Share stored lowercase; the viewer's mixed-case email still matches."""
    init_db()
    with session_scope() as s:
        owner = _user(s, "owner@x")
        viewer = _user(s, "Viewer@Example.COM")
        fid = _folder(s, "/p", "p", owner)
        s.add(FolderEmailAcl(folder_id=fid, email="viewer@example.com"))
    with session_scope() as s:
        assert fid in visible_folder_ids(s, viewer)
        assert user_can_see_folder(s, fid, viewer)


def test_pending_email_share_materialises_on_signup(env: None) -> None:
    """Share an address nobody has signed in with; when the account
    appears later, access is already there — no re-granting."""
    init_db()
    with session_scope() as s:
        owner = _user(s, "owner@x")
        fid = _folder(s, "/p", "p", owner)
        s.add(FolderEmailAcl(folder_id=fid, email="future@x"))
    # ...a week later, they sign in for the first time:
    with session_scope() as s:
        newcomer = _user(s, "future@x")
    with session_scope() as s:
        assert fid in visible_folder_ids(s, newcomer)
        assert user_can_see_folder(s, fid, newcomer)


# ---------------------------------------------------------------------------
# Group shares
# ---------------------------------------------------------------------------


def test_group_share_reaches_members_only(env: None) -> None:
    init_db()
    with session_scope() as s:
        owner = _user(s, "owner@x")
        member = _user(s, "member@x")
        outsider = _user(s, "outsider@x")
        gid = _group(s, "eng", member)
        fid = _folder(s, "/p", "p", owner)
        s.add(FolderGroupAcl(folder_id=fid, group_id=gid))
    with session_scope() as s:
        assert fid in visible_folder_ids(s, member)
        assert user_can_see_folder(s, fid, member)
        assert fid not in visible_folder_ids(s, outsider)
        assert not user_can_see_folder(s, fid, outsider)


def test_group_membership_changes_apply_live(env: None) -> None:
    """No re-granting: joining a granted group gains access, leaving
    loses it — the grant itself is untouched."""
    init_db()
    with session_scope() as s:
        owner = _user(s, "owner@x")
        person = _user(s, "person@x")
        gid = _group(s, "eng")
        fid = _folder(s, "/p", "p", owner)
        s.add(FolderGroupAcl(folder_id=fid, group_id=gid))
    with session_scope() as s:
        assert fid not in visible_folder_ids(s, person)  # not a member yet
        s.add(UserGroup(user_id=person, group_id=gid))
    with session_scope() as s:
        assert fid in visible_folder_ids(s, person)
        s.query(UserGroup).filter_by(user_id=person, group_id=gid).delete()
    with session_scope() as s:
        assert fid not in visible_folder_ids(s, person)


# ---------------------------------------------------------------------------
# Layer independence — the core invariant of the sharing model
# ---------------------------------------------------------------------------


def test_layers_are_independent(env: None) -> None:
    """The reported edge case: share with a person, then with the whole
    community, then turn the community share OFF — the person's share
    must survive untouched (and vice versa for groups)."""
    from voitta_rag_enterprise.services import admin_store

    init_db()
    admin_store.add_allowed_user("owner@x")
    admin_store.add_allowed_user("person@x")
    with session_scope() as s:
        owner = _user(s, "owner@x")
        person = _user(s, "person@x")
        gid = _group(s, "eng", person)
        fid = _folder(s, "/p", "p", owner)
        # Layer 1+2: email share and group share for the same person.
        s.add(FolderEmailAcl(folder_id=fid, email="person@x"))
        s.add(FolderGroupAcl(folder_id=fid, group_id=gid))

    # ...a week later: share with the entire (native) community too.
    with session_scope() as s:
        s.get(Folder, fid).shared = True
    with session_scope() as s:
        assert fid in visible_folder_ids(s, person)

    # Turn the community share back off — targeted shares must hold.
    with session_scope() as s:
        s.get(Folder, fid).shared = False
    with session_scope() as s:
        assert fid in visible_folder_ids(s, person)
        assert user_can_see_folder(s, fid, person)

    # Drop the email share — group share alone still holds.
    with session_scope() as s:
        s.query(FolderEmailAcl).filter_by(folder_id=fid).delete()
    with session_scope() as s:
        assert fid in visible_folder_ids(s, person)

    # Drop the group share too — now (and only now) access is gone.
    with session_scope() as s:
        s.query(FolderGroupAcl).filter_by(folder_id=fid).delete()
    with session_scope() as s:
        assert fid not in visible_folder_ids(s, person)


def test_email_share_ignores_community_boundaries(env: None) -> None:
    """Email shares are deliberately NOT org-scoped: an out-of-community
    address (no native allowlist, no Clerk company) still gets access."""
    init_db()
    with session_scope() as s:
        owner = _user(s, "owner@x")
        external = _user(s, "guest@partner.io")  # no community at all
        fid = _folder(s, "/p", "p", owner)
        s.add(FolderEmailAcl(folder_id=fid, email="guest@partner.io"))
    with session_scope() as s:
        assert fid in visible_folder_ids(s, external)
        assert user_can_see_folder(s, fid, external)
