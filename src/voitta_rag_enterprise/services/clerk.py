"""Clerk (clerk.com) Backend API client — read-only directory access.

Used by the admin UI's Clerk mode: when the admin flips the toggle on the
Sign-in gate tab, the Users tab shows Clerk users and the Companies tab
shows Clerk organizations + memberships, all read-only. Nothing here
touches sign-in or authorization — it's a directory viewer.

Endpoints used (https://api.clerk.com/v1, Bearer ``sk_…``):

- ``GET /users``                                  — instance users
- ``GET /organizations``                          — instance orgs
- ``GET /organizations/{id}/memberships``         — org members + roles

Gotcha: Clerk sits behind Cloudflare, which 403s requests with a
generic/bot User-Agent (python-urllib got blocked in testing). httpx's
default UA passes, but we set an explicit one so this never regresses.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time

import httpx

from ..config import get_settings

logger = logging.getLogger(__name__)

_UA = "voitta-rag-enterprise/1.0 (+https://voitta.ai)"
_PAGE = 100          # Clerk's max page size
_MAX_PAGES = 20      # safety cap: 2000 users/orgs is plenty for the admin view
_TIMEOUT = 20.0


class ClerkError(RuntimeError):
    """Raised for auth/transport failures; message is safe to show admins."""


def _headers(secret_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret_key}", "User-Agent": _UA}


async def _get_paginated(
    client: httpx.AsyncClient, path: str, secret_key: str
) -> list[dict]:
    """Fetch every page of a Clerk list endpoint (offset pagination)."""
    out: list[dict] = []
    for page in range(_MAX_PAGES):
        resp = await client.get(
            path,
            headers=_headers(secret_key),
            params={"limit": _PAGE, "offset": page * _PAGE},
        )
        if resp.status_code == 401:
            raise ClerkError("Clerk rejected the secret key (401 Unauthorized).")
        if resp.status_code == 403:
            raise ClerkError(
                "Clerk returned 403 — the key lacks permission or the "
                "request was blocked."
            )
        resp.raise_for_status()
        body = resp.json()
        # List endpoints return either a bare array or {data: [...]}.
        rows = body.get("data", body) if isinstance(body, dict) else body
        if not isinstance(rows, list):
            raise ClerkError(f"Unexpected Clerk response shape from {path}.")
        out.extend(rows)
        if len(rows) < _PAGE:
            break
    else:
        logger.warning("clerk: %s pagination hit the %d-page cap", path, _MAX_PAGES)
    return out


def _primary_email(user: dict) -> str:
    """Resolve the user's primary email from Clerk's user object."""
    addrs = user.get("email_addresses") or []
    primary_id = user.get("primary_email_address_id")
    for a in addrs:
        if a.get("id") == primary_id:
            return a.get("email_address", "")
    return addrs[0].get("email_address", "") if addrs else ""


def _display_name(user: dict) -> str:
    name = " ".join(
        p for p in [user.get("first_name"), user.get("last_name")] if p
    ).strip()
    return name or user.get("username") or ""


async def fetch_directory(secret_key: str) -> dict:
    """Pull users + organizations + memberships in one sweep.

    Returns the shape the admin UI renders directly::

        {
          "users": [{id, email, name, image_url, created_at,
                     last_sign_in_at, org_names: [str]}],
          "organizations": [{id, name, created_at, members:
                     [{user_id, email, name, role}]}],
        }

    Raises :class:`ClerkError` with an admin-presentable message on
    auth/transport problems.
    """
    base = get_settings().clerk_api_base.rstrip("/")
    try:
        async with httpx.AsyncClient(base_url=base, timeout=_TIMEOUT) as client:
            users_raw, orgs_raw = await asyncio.gather(
                _get_paginated(client, "/users", secret_key),
                _get_paginated(client, "/organizations", secret_key),
            )
            memberships = await asyncio.gather(
                *(
                    _get_paginated(
                        client, f"/organizations/{o['id']}/memberships", secret_key
                    )
                    for o in orgs_raw
                )
            )
    except ClerkError:
        raise
    except httpx.HTTPError as e:
        raise ClerkError(f"Clerk request failed: {e}") from e

    users_by_id: dict[str, dict] = {}
    users: list[dict] = []
    for u in users_raw:
        row = {
            "id": u.get("id", ""),
            "email": _primary_email(u),
            "name": _display_name(u),
            "image_url": u.get("image_url") or "",
            "created_at": u.get("created_at"),
            "last_sign_in_at": u.get("last_sign_in_at"),
            "org_names": [],
            # [{id, name}] — account provisioning keys on the org *id*.
            "orgs": [],
        }
        users_by_id[row["id"]] = row
        users.append(row)

    organizations: list[dict] = []
    for org, rows in zip(orgs_raw, memberships):
        members = []
        for m in rows:
            pud = m.get("public_user_data") or {}
            uid = pud.get("user_id", "")
            known = users_by_id.get(uid)
            email = (known or {}).get("email") or pud.get("identifier", "")
            name = (known or {}).get("name") or " ".join(
                p for p in [pud.get("first_name"), pud.get("last_name")] if p
            ).strip()
            # Strip Clerk's "org:" prefix for display ("org:admin" → "admin").
            role = (m.get("role") or "").removeprefix("org:")
            members.append(
                {"user_id": uid, "email": email, "name": name, "role": role}
            )
            if known is not None:
                known["org_names"].append(org.get("name", ""))
                known["orgs"].append(
                    {"id": org.get("id", ""), "name": org.get("name", "")}
                )
        # Admins first, then alphabetical — matches how the UI reads it.
        members.sort(key=lambda m: (m["role"] != "admin", m["email"]))
        organizations.append(
            {
                "id": org.get("id", ""),
                "name": org.get("name", ""),
                "created_at": org.get("created_at"),
                "members": members,
            }
        )

    users.sort(key=lambda u: u["email"])
    organizations.sort(key=lambda o: o["name"].lower())
    return {"users": users, "organizations": organizations}


# ---------------------------------------------------------------------------
# Single-org membership lookup with a short TTL cache.
#
# Company-API-key auth needs "is <email> a member (or admin) of <org>?" on
# the request path, and the settings UI needs the caller's own role — both
# would otherwise hit Clerk per request. One fetch per org per TTL window
# keeps Clerk load bounded while still reflecting membership changes within
# minutes. The cache key includes the secret key's hash so rotating the key
# can't serve members fetched with the old credential.
# ---------------------------------------------------------------------------

ORG_MEMBERS_TTL_S = 300

# (key_hash, org_id) -> (fetched_at_monotonic, {email: role})
_org_members_cache: dict[tuple[str, str], tuple[float, dict[str, str]]] = {}


async def fetch_org_members(secret_key: str, org_id: str) -> dict[str, str]:
    """Return ``{email: role}`` for one org, cached for ORG_MEMBERS_TTL_S.

    Roles come back with Clerk's "org:" prefix stripped ("admin",
    "member"). Raises :class:`ClerkError` on auth/transport problems —
    callers decide whether to fail open (existing account) or closed.
    """
    cache_key = (hashlib.sha256(secret_key.encode()).hexdigest()[:16], org_id)
    hit = _org_members_cache.get(cache_key)
    if hit is not None and (time.monotonic() - hit[0]) < ORG_MEMBERS_TTL_S:
        return hit[1]

    base = get_settings().clerk_api_base.rstrip("/")
    try:
        async with httpx.AsyncClient(base_url=base, timeout=_TIMEOUT) as client:
            rows = await _get_paginated(
                client, f"/organizations/{org_id}/memberships", secret_key
            )
    except ClerkError:
        raise
    except httpx.HTTPError as e:
        raise ClerkError(f"Clerk request failed: {e}") from e

    members: dict[str, str] = {}
    for m in rows:
        pud = m.get("public_user_data") or {}
        email = (pud.get("identifier") or "").strip().lower()
        if not email:
            continue
        members[email] = (m.get("role") or "").removeprefix("org:")
    _org_members_cache[cache_key] = (time.monotonic(), members)
    return members


def clear_org_members_cache() -> None:
    """Drop the membership cache (tests; admin key rotation UX)."""
    _org_members_cache.clear()
