"""Seed users from a flat text file. Idempotent.

Run::

    python -m scripts.seed_users [path/to/users.txt]

If no path is given, defaults to ``$VOITTA_USERS_FILE`` (``users.txt``).
File format: one email per line; lines starting with ``#`` are comments.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from voitta_rag_enterprise.config import get_settings
from voitta_rag_enterprise.db.database import init_db, session_scope
from voitta_rag_enterprise.services.acl import seed_users_from_file

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=None,
        help="users file (default: VOITTA_USERS_FILE)",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    target = args.path or get_settings().users_file
    if not target.exists():
        logger.error("users file not found: %s", target)
        raise SystemExit(1)

    init_db()
    with session_scope() as s:
        added = seed_users_from_file(s, target)
    logger.info("seeded %d new user(s) from %s", added, target)


if __name__ == "__main__":
    main()
