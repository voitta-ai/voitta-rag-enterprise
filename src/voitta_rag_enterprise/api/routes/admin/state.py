"""Full admin-console state builder (WS snapshot + push payload).

Module-level imports pull the per-endpoint schemas/helpers this builder
mirrors. The edge is one-way (state → endpoint modules) and safe: those
modules import only from ``base``, never from ``state``.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ....db.models import AuthProvider, User
from ....services import admin_store, indexing_caps
from ....services.admin_scope import AdminScope, filter_users_for_scope
from .allowlist import AllowlistOut
from .auth_providers import _to_out
from .caps import IndexingCapsOut
from .settings import _admin_settings_out
from .users import _user_out

# ---------------------------------------------------------------------------
# WebSocket snapshot + push
#
# The admin modal is WS-backed: it renders from a single ``admin.snapshot``
# frame (sent on connect to admins, see ``api.snapshot``) and re-renders on the
# same frame pushed after every admin mutation above. No HTTP-on-open, no
# post-mutation refetch. Delivery is admin-only (the WS pump drops ``admin.*``
# events for non-admin connections).
# ---------------------------------------------------------------------------


def build_admin_state(db: Session, scope: AdminScope) -> dict:
    """Full admin-console state for one viewer, mirroring the admin GET
    endpoints. The shape matches what the SPA's admin modal renders so one
    builder feeds both the connect snapshot and the on-mutation push.

    ``scope`` is the viewer's resolved administrative domain. The ``users``
    list is filtered to it; the deployment-global sections (allowlist,
    groups, providers, caps, settings) are always included — a regular admin
    may *view* them — but ``read_only``/``permissions`` tell the client to
    disable every mutating control for a non-superadmin.
    """
    from ....config import get_settings
    from ....services import groups as groups_svc

    super_list = get_settings().super_admin_list()
    supers = {sa.lower() for sa in super_list}
    by_user = groups_svc.group_names_by_user(db)
    users = db.execute(
        select(User).order_by(User.email, User.company_id)
    ).scalars().all()
    users = filter_users_for_scope(scope, users)
    providers = db.execute(select(AuthProvider).order_by(AuthProvider.id)).scalars().all()
    return {
        "read_only": not scope.is_super,
        "permissions": {
            "is_super": scope.is_super,
            "is_native_admin": scope.is_native_admin,
            "admin_org_ids": sorted(scope.admin_org_ids),
            "clerk_degraded": scope.clerk_degraded,
        },
        "allowlist": AllowlistOut(
            domains=admin_store.list_allowed_domains(),
            users=admin_store.list_allowed_users(),
            blocked=admin_store.list_blocked_users(),
            super_admins=super_list,
        ).model_dump(),
        "users": [
            _user_out(u, groups=by_user.get(u.id, []), supers=supers).model_dump()
            for u in users
        ],
        "groups": groups_svc.list_groups_with_counts(db),
        "auth_providers": [_to_out(r).model_dump() for r in providers],
        "indexing_caps": IndexingCapsOut(
            values=indexing_caps.as_dict(),
            defaults=indexing_caps.defaults_dict(),
            bounds=indexing_caps.bounds_dict(),
        ).model_dump(),
        "settings": _admin_settings_out().model_dump(),
    }
