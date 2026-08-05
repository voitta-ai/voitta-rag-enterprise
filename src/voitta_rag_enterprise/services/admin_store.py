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


# ---------------------------------------------------------------------------
# Named Clerk instances.
#
# Every Clerk instance is NAMED ("Development", "Production", …). Stored as
# ``clerk_instances: [{name, secret_key, enabled}]`` in settings.json. The
# legacy single-key fields (``clerk_enabled`` / ``clerk_secret_key``) are
# kept in the file and MIRRORED from the instance named "Development" on
# every save, so rolling back to a pre-instances build keeps Clerk working
# unchanged. Reads synthesize the list from the legacy fields (or the
# CLERK_SECRET_KEY env var) when no list has been saved yet — the migration
# is read-side and touches nothing until an admin first edits instances.
# ---------------------------------------------------------------------------

# The migration name given to the legacy stored key / env key. Also the
# instance whose key mirrors back into the legacy fields on save.
LEGACY_INSTANCE_NAME = "Development"

CLERK_INSTANCE_NAME_MAX = 24


def _norm_instances(raw: object) -> list[dict[str, object]]:
    """Validate/normalize a persisted or caller-supplied instance list."""
    out: list[dict[str, object]] = []
    seen: set[str] = set()
    if not isinstance(raw, list):
        return out
    for row in raw:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name", "") or "").strip()[:CLERK_INSTANCE_NAME_MAX]
        key = str(row.get("secret_key", "") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        norm: dict[str, object] = {
            "name": name,
            "secret_key": key,
            "enabled": bool(row.get("enabled", False)),
        }
        # Carried through so save_clerk_instances can blank env-sourced
        # keys instead of persisting them (never stored to disk itself —
        # the save side strips it from the written rows).
        if row.get("from_env"):
            norm["from_env"] = True
        out.append(norm)
    return out


def get_clerk_instances() -> list[dict[str, object]]:
    """The configured Clerk instances, migrating from legacy shape on read.

    Precedence when no ``clerk_instances`` list has ever been saved:
    legacy stored key → env key — either becomes a synthesized
    "Development" instance carrying the legacy ``clerk_enabled`` flag.
    Once a list exists in the file it is the single source of truth
    (env is then only a fallback for the Development card's empty key,
    preserving the pre-instances precedence rule).
    """
    s = load_settings()
    raw = s.get("clerk_instances")
    if isinstance(raw, list):
        instances = _norm_instances(raw)
        # Preserve the historical env-fallback: an empty stored key on the
        # legacy-named instance defers to CLERK_SECRET_KEY from .env.
        env_key = (get_settings().clerk_secret_key or "").strip()
        if env_key:
            for inst in instances:
                if inst["name"] == LEGACY_INSTANCE_NAME and not inst["secret_key"]:
                    inst["secret_key"] = env_key
                    inst["from_env"] = True
        return instances

    # No list saved yet — synthesize from legacy fields (read-side migration).
    stored = str(s.get("clerk_secret_key", "") or "").strip()
    env_key = (get_settings().clerk_secret_key or "").strip()
    key = stored or env_key
    if not key:
        return []
    return [
        {
            "name": LEGACY_INSTANCE_NAME,
            "secret_key": key,
            "enabled": bool(s.get("clerk_enabled", False)),
            **({"from_env": True} if not stored else {}),
        }
    ]


def save_clerk_instances(instances: list[dict[str, object]]) -> None:
    """Persist the instance list; mirror "Development" into legacy fields.

    The mirror keeps a rollback to a pre-instances build fully working
    (old code reads ``clerk_secret_key``/``clerk_enabled`` as before).
    A one-time ``settings.json.bak-clerk-<ts>`` backup is written before
    the first save that introduces the list. Keys flagged ``from_env``
    are stored EMPTY (the read side re-applies the env fallback), so a
    .env rotation is never shadowed by a stale copy.
    """
    import json as _json
    import shutil
    import time as _time

    cleaned = _norm_instances(instances)
    raw = [
        {
            "name": i["name"],
            # Never persist an env-sourced key (see docstring).
            "secret_key": "" if i.get("from_env") else i["secret_key"],
            "enabled": i["enabled"],
        }
        for i in cleaned
    ]
    p = _settings_path()
    if p.exists():
        try:
            has_list = isinstance(
                _json.loads(p.read_text() or "{}").get("clerk_instances"), list
            )
        except (OSError, ValueError):
            has_list = False
        if not has_list:
            with contextlib.suppress(OSError):
                shutil.copy2(p, f"{p}.bak-clerk-{int(_time.time())}")

    legacy = next(
        (i for i in cleaned if i["name"] == LEGACY_INSTANCE_NAME), None
    )
    save_settings(
        {
            "clerk_instances": raw,
            # Legacy mirror — rollback safety. Enabled mirrors the
            # Development card; the key mirrors only store-sourced keys.
            "clerk_enabled": bool(legacy and legacy["enabled"]),
            "clerk_secret_key": (
                "" if (legacy is None or legacy.get("from_env"))
                else str(legacy["secret_key"])
            ),
        }
    )


def enabled_clerk_instances() -> list[dict[str, object]]:
    """Instances that are enabled AND have a key — the auth-path view."""
    return [
        i for i in get_clerk_instances() if i["enabled"] and i["secret_key"]
    ]


def get_clerk_enabled() -> bool:
    """True when at least one named Clerk instance is enabled with a key."""
    return bool(enabled_clerk_instances())


def get_clerk_secret_key() -> str:
    """Legacy single-key view: the "Development" instance's key, else the
    first enabled instance's. Kept for the deprecated admin-API fields and
    any straggler callers; new code iterates ``enabled_clerk_instances``.
    """
    instances = get_clerk_instances()
    for inst in instances:
        if inst["name"] == LEGACY_INSTANCE_NAME and inst["secret_key"]:
            return str(inst["secret_key"])
    for inst in instances:
        if inst["enabled"] and inst["secret_key"]:
            return str(inst["secret_key"])
    return ""


def clerk_key_from_env() -> bool:
    """True when the Development instance's key comes from .env."""
    return any(
        i.get("from_env") and i["name"] == LEGACY_INSTANCE_NAME
        for i in get_clerk_instances()
    )


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
