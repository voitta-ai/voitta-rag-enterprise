"""Admin-managed allowlist + blocklist persisted as plain text files.

Three files under ``<data_dir>/admin/``:

- ``allowed_domains.txt`` — one domain per line, e.g. ``customer.com``.
  A verified email whose domain matches any line here is admitted.
- ``allowed_users.txt`` — one email per line. A verified email matching
  any line here is admitted regardless of its domain.
- ``blocked_users.txt`` — one email per line. Listed addresses are
  rejected before the allow-checks run, so a domain admin can revoke
  individual addresses without removing the domain.

Admins manage these via the ``/api/admin/*`` REST endpoints. The files
are also human-editable via SSH for emergency recovery — admins
sometimes lock themselves out and need to drop their own email into
``allowed_users.txt`` from the VM. Treat that as the supported recovery
path.

Format:

- One value per line, leading/trailing whitespace stripped.
- Blank lines and ``# …`` comments are ignored on read.
- Case-insensitive on read (we lowercase before matching).

Atomicity: writes go to a temp file in the same dir, then rename. ext4
+ fsync make the rename atomic enough for our needs — we're not
serializing concurrent writers, and the admin UI is single-user
in practice.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

from ..config import get_settings

ALLOWED_DOMAINS = "allowed_domains.txt"
ALLOWED_USERS = "allowed_users.txt"
BLOCKED_USERS = "blocked_users.txt"


def admin_dir() -> Path:
    """Return ``<data_dir>/admin``, creating it on demand."""
    p = get_settings().data_dir / "admin"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _path(name: str) -> Path:
    return admin_dir() / name


def _read(name: str) -> list[str]:
    """Return non-empty, non-comment, lowercased entries from ``name``."""
    p = _path(name)
    if not p.exists():
        return []
    out: list[str] = []
    for raw in p.read_text().splitlines():
        v = raw.strip()
        if not v or v.startswith("#"):
            continue
        out.append(v.lower())
    return out


def _write(name: str, values: list[str]) -> None:
    """Write the canonical (deduped, sorted) list to ``name`` atomically."""
    p = _path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    cleaned = sorted({v.strip().lower() for v in values if v.strip()})
    body = "\n".join(cleaned) + ("\n" if cleaned else "")
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", dir=p.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(body)
        os.replace(tmp, p)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# Public API — used by the gate + the admin REST endpoints.
# ---------------------------------------------------------------------------


def list_allowed_domains() -> list[str]:
    return _read(ALLOWED_DOMAINS)


def list_allowed_users() -> list[str]:
    return _read(ALLOWED_USERS)


def list_blocked_users() -> list[str]:
    return _read(BLOCKED_USERS)


def add_allowed_domain(domain: str) -> None:
    domain = domain.strip().lstrip("@").lower()
    if not domain or "." not in domain:
        raise ValueError("invalid domain")
    cur = list_allowed_domains()
    if domain in cur:
        return
    _write(ALLOWED_DOMAINS, [*cur, domain])


def remove_allowed_domain(domain: str) -> None:
    domain = domain.strip().lstrip("@").lower()
    cur = [d for d in list_allowed_domains() if d != domain]
    _write(ALLOWED_DOMAINS, cur)


def add_allowed_user(email: str) -> None:
    email = email.strip().lower()
    if "@" not in email:
        raise ValueError("invalid email")
    cur = list_allowed_users()
    if email in cur:
        return
    _write(ALLOWED_USERS, [*cur, email])


def remove_allowed_user(email: str) -> None:
    email = email.strip().lower()
    cur = [u for u in list_allowed_users() if u != email]
    _write(ALLOWED_USERS, cur)


def add_blocked_user(email: str) -> None:
    email = email.strip().lower()
    if "@" not in email:
        raise ValueError("invalid email")
    cur = list_blocked_users()
    if email in cur:
        return
    _write(BLOCKED_USERS, [*cur, email])


def remove_blocked_user(email: str) -> None:
    email = email.strip().lower()
    cur = [u for u in list_blocked_users() if u != email]
    _write(BLOCKED_USERS, cur)


# ---------------------------------------------------------------------------
# Sign-in gate. Single source of truth for "may this address sign in".
# ---------------------------------------------------------------------------


def is_email_allowed(email: str) -> bool:
    """Return True iff ``email`` may complete the OAuth sign-in.

    Order:
        1. Block-list trumps everything.
        2. Super-admin (``VOITTA_SUPER_ADMINS``) is always allowed — the
           bootstrap admin must be able to sign in even when the
           allowlists are empty (otherwise a fresh deploy is locked
           out forever).
        3. Email match in ``allowed_users.txt``.
        4. Domain match in ``allowed_domains.txt``.
        5. Otherwise: deny.
    """
    addr = email.strip().lower()
    if "@" not in addr:
        return False
    if addr in set(list_blocked_users()):
        return False
    s = get_settings()
    if addr in {sa.lower() for sa in s.super_admin_list()}:
        return True
    if addr in set(list_allowed_users()):
        return True
    domain = addr.split("@", 1)[1]
    return domain in set(list_allowed_domains())


def is_super_admin(email: str) -> bool:
    addr = email.strip().lower()
    return addr in {sa.lower() for sa in get_settings().super_admin_list()}
