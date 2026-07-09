"""Sharing communities — the org-scoping seam.

An account's *community* decides who its shared folders reach:

- Company account → its ``company_id`` (all accounts of that Clerk org).
- Personal account of a natively-allowed email → the ``"native"`` community.
- Personal account of a Clerk-only user → no community at all.

This is deliberately the ONLY place that translates an account into an org
scope. Org-level authorization decisions belong here, not scattered through
the folder-ACL queries that consume the result.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ...db.models import User


def account_community(session: Session, user_id: int) -> str | None:
    """The sharing community of an account, or None when it has none.

    - Company account → its ``company_id`` (all accounts of that Clerk org).
    - Personal account of a natively-allowed email (allowlist user/domain
      or super-admin) → the ``"native"`` community.
    - Personal account of a Clerk-only user → **no community**: it can
      neither share out nor see community-shared folders (grants and
      ownership still work normally).
    """
    from .. import admin_store

    row = session.get(User, user_id)
    if row is None:
        return None
    if row.company_id:
        return row.company_id
    if admin_store.is_native_allowed(row.email) or admin_store.is_super_admin(
        row.email
    ):
        return "native"
    return None


def _owner_community(session: Session, owner_id: int | None) -> str | None:
    """Community a shared folder is shared INTO — the owner's community.

    Legacy escape hatch: an unowned shared folder (``owner_id`` NULL, e.g.
    its owner was deleted) counts as native so it doesn't silently vanish
    for the operator community.
    """
    if owner_id is None:
        return "native"
    return account_community(session, owner_id)
