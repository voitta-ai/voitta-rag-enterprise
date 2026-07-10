"""Administrative domain resolution for the admin console.

The admin console used to show every admin the whole deployment. That's an
authorization leak: a regular admin must only see/manage their own
*administrative domain*. This module resolves that domain into a plain
``AdminScope`` value and provides the pure predicates the (synchronous)
admin-state builder and route handlers filter with.

Domain model:

* **Superadmin** (``VOITTA_SUPER_ADMINS``): sees and mutates everything.
  ``AdminScope.is_super`` short-circuits every filter.
* **Regular admin** (``User.is_admin`` on some account, not super): domain =
  the Clerk orgs where they hold role ``admin`` (the same org-role signal
  ``company_keys`` uses to gate minting) **plus** the native community when
  they are native-allowed. They see only users in that domain and may not
  mutate any deployment-global setting.

The org-admin signal comes from Clerk, which is async and rate-limited, so
scope is resolved once (``resolve_admin_scope``, async) and the result — a
frozen dataclass — is threaded into the sync builders. A short TTL cache on
the directory sweep keeps this cheap even though it runs on every admin WS
connect and every admin mutation.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..db.models import User
from . import admin_store
from . import clerk as clerk_svc

logger = logging.getLogger(__name__)

# The directory sweep is a full users+orgs+memberships pull; cache it briefly
# so per-connect / per-mutation scope resolution doesn't hammer Clerk. Shorter
# than the org-members TTL (org-admin demotion should take effect fast).
_DIRECTORY_TTL_S = 45

# key_hash -> (fetched_at_monotonic, directory dict)
_directory_cache: dict[str, tuple[float, dict]] = {}


@dataclass(frozen=True)
class AdminScope:
    """A resolved administrative domain.

    ``is_super`` see-all trumps everything else. For a regular admin,
    ``admin_org_ids`` are the Clerk orgs where they are an org admin and
    ``is_native_admin`` says whether they also administer the native
    community. ``clerk_degraded`` is set when Clerk was enabled but
    unreachable — the org domain is then empty (fail closed) and the UI can
    explain the emptiness as an outage rather than a permissions change.
    """

    is_super: bool = False
    admin_org_ids: frozenset[str] = field(default_factory=frozenset)
    admin_org_names: frozenset[str] = field(default_factory=frozenset)
    is_native_admin: bool = False
    clerk_degraded: bool = False


def admin_orgs_from_directory(
    directory: dict, email: str
) -> tuple[frozenset[str], frozenset[str]]:
    """Pure: (org_ids, org_names) where ``email`` holds role ``admin``.

    Shared by ``resolve_admin_scope`` and the Clerk-directory endpoint so a
    single directory sweep serves both. Roles in the directory payload are
    already stripped of Clerk's ``org:`` prefix (see ``clerk.fetch_directory``).
    """
    addr = email.strip().lower()
    ids: set[str] = set()
    names: set[str] = set()
    for org in directory.get("organizations", []):
        for m in org.get("members", []):
            if (m.get("email") or "").strip().lower() == addr and m.get("role") == "admin":
                oid = org.get("id") or ""
                if oid:
                    ids.add(oid)
                    names.add(org.get("name") or "")
                break
    return frozenset(ids), frozenset(names)


async def _fetch_directory_cached(secret_key: str) -> dict:
    cache_key = hashlib.sha256(secret_key.encode()).hexdigest()[:16]
    hit = _directory_cache.get(cache_key)
    if hit is not None and (time.monotonic() - hit[0]) < _DIRECTORY_TTL_S:
        return hit[1]
    directory = await clerk_svc.fetch_directory(secret_key)
    _directory_cache[cache_key] = (time.monotonic(), directory)
    return directory


def clear_directory_cache() -> None:
    """Drop the directory cache (tests; secret-key rotation UX)."""
    _directory_cache.clear()


async def resolve_admin_scope(db: Session, email: str) -> AdminScope:
    """Resolve ``email``'s administrative domain.

    Assumes the caller already passed the ``admin_user`` gate (person-level
    admin). Superadmins short-circuit to see-all. Otherwise the domain is the
    Clerk orgs where they are an org admin, plus the native community when
    they are native-allowed. Clerk unreachable → ``clerk_degraded`` and an
    empty org domain (fail closed, mirroring ``company_keys``).
    """
    if admin_store.is_super_admin(email):
        return AdminScope(is_super=True, is_native_admin=True)

    is_native_admin = admin_store.is_native_allowed(email)
    org_ids: frozenset[str] = frozenset()
    org_names: frozenset[str] = frozenset()
    degraded = False

    if admin_store.get_clerk_enabled():
        secret_key = admin_store.get_clerk_secret_key()
        if secret_key:
            try:
                directory = await _fetch_directory_cached(secret_key)
                org_ids, org_names = admin_orgs_from_directory(directory, email)
            except clerk_svc.ClerkError as e:
                logger.warning(
                    "admin_scope: Clerk directory unreachable for %s: %s", email, e
                )
                degraded = True

    return AdminScope(
        is_super=False,
        admin_org_ids=org_ids,
        admin_org_names=org_names,
        is_native_admin=is_native_admin,
        clerk_degraded=degraded,
    )


def user_in_scope(scope: AdminScope, target: User) -> bool:
    """Is ``target`` (one account row) within the admin's domain?

    Super sees all. Otherwise the account is in scope when its company is one
    the admin administers, or when the admin is a native admin and the
    account's email is native-allowed.
    """
    if scope.is_super:
        return True
    if target.company_id and target.company_id in scope.admin_org_ids:
        return True
    return scope.is_native_admin and admin_store.is_native_allowed(target.email)


def filter_users_for_scope(
    scope: AdminScope, users: Iterable[User]
) -> list[User]:
    """The subset of ``users`` visible to ``scope`` (all of them for super)."""
    if scope.is_super:
        return list(users)
    return [u for u in users if user_in_scope(scope, u)]
