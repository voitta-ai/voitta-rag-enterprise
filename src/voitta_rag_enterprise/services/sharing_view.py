"""A folder's sharing configuration, as its owner sees it.

Visibility is the UNION of independent layers (see
``services/acl/folder_acl.visible_folder_ids``): the community *audience*
(``folders.shared``), voitta-native *groups* (``folder_group_acl``) and
*people* (``folder_email_acl`` merged with legacy per-account
``folder_acl`` grants). This module assembles that view; it is shared by the
share modal's routes (api/routes/sharing.py) and the assistant's
``folder_settings`` tool. Callers enforce the owner gate.
"""

from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import Folder, FolderAcl, FolderEmailAcl, FolderGroupAcl, Group, User, UserGroup
from .acl import account_community, accounts_for_email


class AudienceOut(BaseModel):
    # "clerk_org"  — owner is a company account: audience = that Clerk org
    # "native_all" — owner is a native account: audience = all native users
    # "none"       — owner has no community (personal account of a
    #                Clerk-only user): audience sharing unavailable
    kind: str
    on: bool
    label: str


class GroupShareOut(BaseModel):
    id: int
    name: str
    member_count: int


class PersonShareOut(BaseModel):
    email: str
    # "member" — at least one account exists for this email;
    # "pending" — nobody has signed in with it yet (share is live and
    # materialises on their first sign-in).
    status: str
    # True when no account of this email is in the owner's community —
    # rendered as an "outside org" hint. Always False for community-less
    # owners (there is no org to be outside of).
    outside_org: bool
    # True when the entry exists only as a legacy per-account folder_acl
    # grant (pre-dating email shares). Removal clears both stores.
    legacy: bool


class SharingOut(BaseModel):
    folder_id: int
    audience: AudienceOut
    groups: list[GroupShareOut]
    people: list[PersonShareOut]


def audience_for_owner(db: Session, folder: Folder) -> AudienceOut:
    community = account_community(db, folder.owner_id) if folder.owner_id else None
    if community is None:
        return AudienceOut(kind="none", on=False, label="")
    if community == "native":
        return AudienceOut(
            kind="native_all",
            on=bool(folder.shared),
            label="Everyone on Voitta (native users)",
        )
    owner = db.get(User, folder.owner_id)
    org = (owner.company_name or "").strip() if owner else ""
    return AudienceOut(
        kind="clerk_org",
        on=bool(folder.shared),
        label=f"Everyone at {org}" if org else "Everyone in your organization",
    )


def group_shares(db: Session, folder_id: int) -> list[GroupShareOut]:
    rows = db.execute(
        select(Group, func.count(UserGroup.user_id))
        .join(FolderGroupAcl, FolderGroupAcl.group_id == Group.id)
        .outerjoin(UserGroup, UserGroup.group_id == Group.id)
        .where(FolderGroupAcl.folder_id == folder_id)
        .group_by(Group.id)
        .order_by(Group.name)
    ).all()
    return [
        GroupShareOut(id=g.id, name=g.name, member_count=int(n)) for g, n in rows
    ]


def people_shares(db: Session, folder: Folder) -> list[PersonShareOut]:
    """Email shares plus legacy per-account grants, collapsed by email.

    The owner's own email is excluded: folder registration self-grants
    the owner in ``folder_acl``, and "shared with yourself" is noise —
    owners always see their folders via the owned layer.
    """
    owner = db.get(User, folder.owner_id) if folder.owner_id else None
    owner_email = (owner.email or "").lower() if owner else ""
    email_rows = {
        e.lower()
        for e in db.execute(
            select(FolderEmailAcl.email).where(FolderEmailAcl.folder_id == folder.id)
        ).scalars()
        if e.lower() != owner_email
    }
    legacy_rows = {
        (e or "").lower()
        for e in db.execute(
            select(User.email)
            .join(FolderAcl, FolderAcl.user_id == User.id)
            .where(FolderAcl.folder_id == folder.id)
        ).scalars()
        if e and e.lower() != owner_email
    }
    owner_community = (
        account_community(db, folder.owner_id) if folder.owner_id else None
    )
    out: list[PersonShareOut] = []
    for email in sorted(email_rows | legacy_rows):
        accounts = accounts_for_email(db, email)
        if owner_community is None:
            outside = False
        else:
            outside = not any(
                account_community(db, a.id) == owner_community for a in accounts
            )
        out.append(
            PersonShareOut(
                email=email,
                status="member" if accounts else "pending",
                outside_org=outside,
                legacy=email in legacy_rows and email not in email_rows,
            )
        )
    return out


def sharing_view(db: Session, folder: Folder) -> SharingOut:
    return SharingOut(
        folder_id=folder.id,
        audience=audience_for_owner(db, folder),
        groups=group_shares(db, folder.id),
        people=people_shares(db, folder),
    )
