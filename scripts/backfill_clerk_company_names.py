"""One-shot: restamp existing company accounts to ``"Instance / Org"`` names.

Background. ``users.company_name`` is a display-only label. Before named
Clerk instances shipped — and, until this backfill's companion fix, in the
impersonation path — company accounts were stamped with the *bare* org name
("Agnitio"). With two instances that share an org name (a Development
"Agnitio" and a Production "Agnitio", each a distinct ``company_id``), the
account dropdown showed two indistinguishable "Agnitio" rows.

Going forward, login and impersonation both stamp the prefixed label via
``services/acl/clerk_provision.py`` and self-heal each account on next use.
This script fixes the accounts of users who haven't logged in / been
re-impersonated since, in one pass.

Method. Sweep every ENABLED Clerk instance's directory, build
``{org_id -> "Instance / Org"}``, then rewrite ``company_name`` for every
``users`` row whose ``company_id`` is a key in that map and whose stored
name differs. Rows whose org is in no enabled instance (instance disabled
or org removed) are left untouched — we never blank a label we can't
re-derive. Idempotent: a second run reports zero changes.

Safe to run against a live app (it only touches SQLite rows and reads Clerk
over HTTP). Usage::

    # Inspect — no writes
    python scripts/backfill_clerk_company_names.py

    # Apply
    python scripts/backfill_clerk_company_names.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("backfill-clerk")


async def _build_org_label_map() -> dict[str, str]:
    """``{org_id: "Instance / Org"}`` across every enabled Clerk instance."""
    from voitta_rag_enterprise.services import admin_store
    from voitta_rag_enterprise.services import clerk as clerk_svc

    instances = admin_store.enabled_clerk_instances()
    if not instances:
        logger.error("No enabled Clerk instance configured — nothing to do.")
        return {}

    labels: dict[str, str] = {}
    for inst in instances:
        try:
            directory = await clerk_svc.fetch_directory(str(inst["secret_key"]))
        except clerk_svc.ClerkError as e:
            logger.warning("  instance %r unreachable, skipping: %s", inst["name"], e)
            continue
        n = 0
        for org in directory.get("organizations", []):
            oid = org.get("id")
            if not oid:
                continue
            labels[oid] = f"{inst['name']} / {org.get('name', '')}"
            n += 1
        logger.info("  instance %r: %d orgs", inst["name"], n)
    return labels


def _run(apply: bool) -> int:
    from sqlalchemy import select

    from voitta_rag_enterprise.db.database import session_scope
    from voitta_rag_enterprise.db.models import User

    labels = asyncio.run(_build_org_label_map())
    if not labels:
        return 1

    changed = 0
    skipped_unmapped = 0
    with session_scope() as s:
        rows = list(
            s.execute(select(User).where(User.company_id != "")).scalars()
        )
        for u in rows:
            desired = labels.get(u.company_id)
            if desired is None:
                skipped_unmapped += 1
                continue
            if u.company_name == desired:
                continue
            logger.info(
                "  account id=%d %s: %r -> %r",
                u.id, u.email, u.company_name, desired,
            )
            if apply:
                u.company_name = desired
            changed += 1
        if not apply:
            s.rollback()

    logger.info(
        "%s %d account(s) need restamping (%d company rows scanned, "
        "%d in no enabled instance were left untouched).",
        "restamped" if apply else "would restamp",
        changed,
        len(rows),
        skipped_unmapped,
    )
    if not apply and changed:
        logger.info("Re-run with --apply to write the changes.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Write the changes (default is a dry run).",
    )
    args = ap.parse_args()
    return _run(args.apply)


if __name__ == "__main__":
    sys.exit(main())
