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
SETTINGS_JSON = "settings.json"

# Default typed settings shipped with the app. ``nfs_root`` is empty
# until an admin sets it; an empty value disables the NFS connector
# entirely (the UI hides the option, the API rejects configuration).
# ``clerk_enabled`` / ``clerk_secret_key`` drive the read-only Clerk
# directory view in the admin UI; an empty stored key falls back to
# ``CLERK_SECRET_KEY`` from .env (see :func:`get_clerk_secret_key`).
_DEFAULT_SETTINGS: dict[str, object] = {
    "nfs_root": "",
    "native_directory_enabled": True,
    "clerk_enabled": False,
    "clerk_secret_key": "",
}


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
# Typed settings — single JSON file sitting next to the txt allowlists.
# ---------------------------------------------------------------------------
#
# The allowlist trio above is intentionally line-oriented because admins
# edit them via SSH for lockout recovery. ``settings.json`` is for typed
# config (paths, integers, bools) that is set via the admin UI only;
# format is plain JSON so it's still hand-editable in an emergency, but
# we don't pretend the structure is line-friendly.


def _settings_path() -> Path:
    return admin_dir() / SETTINGS_JSON


def load_settings() -> dict[str, object]:
    """Return the merged (defaults + persisted) settings dict.

    Unknown keys persisted by older versions are kept verbatim, in case
    we removed a setting and want to put it back later. Known keys
    missing from the file fall back to their default — so the caller
    can use ``load_settings().get("nfs_root", "")`` safely.
    """
    import json

    out: dict[str, object] = dict(_DEFAULT_SETTINGS)
    p = _settings_path()
    if not p.exists():
        return out
    try:
        raw = p.read_text()
        data = json.loads(raw) if raw.strip() else {}
        if isinstance(data, dict):
            out.update(data)
    except (OSError, ValueError):
        # Corrupt file: log via the caller; here we just return defaults
        # so a malformed settings.json doesn't crash request handling.
        return dict(_DEFAULT_SETTINGS)
    return out


def save_settings(updates: dict[str, object]) -> dict[str, object]:
    """Merge ``updates`` over the persisted settings, write atomically,
    and return the new merged dict.

    Only keys present in ``updates`` are touched — pass ``{"nfs_root": ""}``
    to explicitly clear a setting; omit a key entirely to leave it alone.
    """
    import json

    cur = load_settings()
    cur.update(updates)
    p = _settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(cur, indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", dir=p.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(body)
        os.replace(tmp, p)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return cur


def get_nfs_root() -> str:
    """Return the admin-configured NFS root directory, or empty string.

    Empty means the feature is disabled — callers that build sync-source
    payloads, browse endpoints, or admin-settings status should all gate
    on a non-empty return value AND a passing existence check. Doing the
    existence check at every read point (rather than caching) keeps the
    UI honest: an NFS mount that disappears flips the feature off
    without any restart.
    """
    raw = load_settings().get("nfs_root", "")
    return str(raw) if raw is not None else ""


def get_native_directory_enabled() -> bool:
    """True when the local users/groups tabs should show (default on)."""
    return bool(load_settings().get("native_directory_enabled", True))


def get_clerk_enabled() -> bool:
    """True when an admin flipped the Clerk-directory toggle on."""
    return bool(load_settings().get("clerk_enabled", False))


def get_clerk_secret_key() -> str:
    """Effective Clerk Backend API secret key.

    The admin-stored value wins; when it's empty we fall back to the
    ``CLERK_SECRET_KEY`` / ``VOITTA_CLERK_SECRET_KEY`` env var so a key
    already present in .env pre-populates the UI without re-entry.
    """
    stored = str(load_settings().get("clerk_secret_key", "") or "").strip()
    if stored:
        return stored
    return (get_settings().clerk_secret_key or "").strip()


def clerk_key_from_env() -> bool:
    """True when the effective key comes from .env, not the admin store."""
    stored = str(load_settings().get("clerk_secret_key", "") or "").strip()
    return not stored and bool((get_settings().clerk_secret_key or "").strip())


def is_native_allowed(email: str) -> bool:
    """Allowlist-only check (no super-admin, no block-list, no Clerk).

    This is the "VOITTA NATIVE" provenance test: the address would be
    admitted by ``allowed_users.txt`` / ``allowed_domains.txt`` alone.
    ``root@localhost`` — the VOITTA_SINGLE_USER / local-dev identity —
    is native by definition: it never goes through the OAuth allowlist,
    but it owns local data and shares into the native community.
    """
    addr = email.strip().lower()
    if addr == "root@localhost":
        return True
    if "@" not in addr:
        return False
    if addr in set(list_allowed_users()):
        return True
    return addr.split("@", 1)[1] in set(list_allowed_domains())


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
