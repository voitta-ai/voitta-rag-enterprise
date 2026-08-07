"""Shared Clerk admission + account provisioning.

Both the sign-in callback (``api/routes/auth.py``) and Clerk impersonation
(``api/routes/admin/impersonation.py``) must resolve a Clerk email to its
org accounts **identically** — same set of enabled instances, same
``company_name = "Instance / Org"`` labels. Keeping that in one place is
what stops the two paths from drifting (they had: login was migrated to
named instances, impersonation was not, so impersonated accounts got bare,
Development-only names and a Production org could be missed entirely).

``resolve_clerk_admission`` does the multi-instance directory sweep and
returns prefixed org labels; ``provision_accounts`` writes the rows. Neither
makes the admission *decision* (native gate, block-list, super-admin) — the
callers layer that on, because it differs (login denies, impersonation 404s).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ...db.models import User
from .. import admin_store
from .accounts import get_or_create_user

logger = logging.getLogger(__name__)


@dataclass
class ClerkAdmission:
    """Result of a multi-instance directory lookup for one email.

    ``matched`` is True when the email exists in *any* enabled instance
    (even org-less). ``orgs`` is the union across instances, each name
    already prefixed with its instance (``"Production / Acme"``). Org ids
    are globally unique across Clerk instances, so no de-dup is needed.
    """

    matched: bool = False
    display_name: str = ""
    orgs: list[dict[str, str]] = field(default_factory=list)


async def resolve_clerk_admission(email: str) -> ClerkAdmission:
    """Sweep every enabled Clerk instance for ``email``; union the result.

    Fail-soft per instance: an unreachable instance is logged and skipped,
    exactly like the sign-in gate — one instance being down never denies a
    user another instance (or the native rules) would admit.
    """
    from .. import clerk as clerk_svc

    email = email.strip().lower()
    instances = admin_store.enabled_clerk_instances()
    if not instances:
        return ClerkAdmission()

    results = await asyncio.gather(
        *(clerk_svc.fetch_directory(str(i["secret_key"])) for i in instances),
        return_exceptions=True,
    )
    out = ClerkAdmission()
    for inst, res in zip(instances, results, strict=True):
        if isinstance(res, BaseException):
            logger.warning(
                "clerk admission: instance %r directory fetch failed: %s",
                inst["name"], res,
            )
            continue
        match = next(
            (u for u in res.get("users", [])
             if (u.get("email") or "").strip().lower() == email),
            None,
        )
        if match is None:
            continue
        out.matched = True
        out.display_name = out.display_name or (match.get("name") or "")
        for org in match.get("orgs") or []:
            if not org.get("id"):
                continue
            out.orgs.append(
                {
                    "id": org["id"],
                    # The instance prefix is the only thing that disambiguates
                    # two orgs that share a name across instances (the exact
                    # "which Agnitio?" bug this module exists to fix).
                    "name": f"{inst['name']} / {org.get('name', '')}",
                }
            )
    return out


def provision_accounts(
    db: Session,
    email: str,
    display_name: str,
    orgs: list[dict[str, str]],
) -> dict[str, User]:
    """Create/refresh the Personal + per-org accounts; return ``{company_id: User}``.

    ``company_id=''`` (Personal) is always present. Each org row's
    ``company_name`` is (re)stamped to the prefixed label — display-only, so
    a stale bare name self-heals here. ``display_name`` backfills only when
    the row has none (never overwrites a user-set name). Accounts are never
    deleted; the caller commits.
    """
    email = email.strip().lower()
    personal = get_or_create_user(db, email)
    if not personal.display_name and display_name:
        personal.display_name = display_name
    by_company: dict[str, User] = {"": personal}
    for org in orgs:
        acc = get_or_create_user(db, email, org["id"], org.get("name", ""))
        if not acc.display_name and display_name:
            acc.display_name = display_name
        by_company[org["id"]] = acc
    return by_company
