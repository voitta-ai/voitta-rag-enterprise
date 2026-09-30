"""Symmetric encryption for secrets stored in the database.

Fernet (AES-128-CBC + HMAC-SHA256, authenticated) keyed by
``Settings.resolved_secret_key()`` — a key that exists only for this
purpose, so it can be backed up and rotated independently of the session
cookie secret.

Values are stored as the Fernet token string. A token that no longer
decrypts (the key file was lost or replaced) raises
:class:`SecretUnavailable`; callers treat the secret as "not configured"
and the operator re-enters it — never a crash.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from ..config import get_settings


class SecretUnavailable(Exception):
    """A stored secret can't be decrypted with the current key."""


def _fernet() -> Fernet:
    return Fernet(get_settings().resolved_secret_key().encode("ascii"))


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as e:
        raise SecretUnavailable("stored secret cannot be decrypted with the current key") from e


def mask(secret: str, *, keep: int = 4) -> str:
    """Display form of a secret: ``…`` + its last ``keep`` characters."""
    tail = secret[-keep:] if len(secret) > keep * 3 else ""
    return f"…{tail}"
