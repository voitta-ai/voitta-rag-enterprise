"""Encrypt/decrypt the secret columns of ``folder_sync_sources`` at rest.

We use direct Cloud KMS ``encrypt`` / ``decrypt`` (no per-row data
encryption keys) because the values are small — ``gh_pat``, OAuth
client secret, refresh token, service-account JSON, all comfortably
under KMS's 64 KiB plaintext limit and accessed at low frequency. A
DEK envelope would buy us nothing here and add a step.

Behavior:

- ``VOITTA_KMS_KEY`` set -> :class:`KMSEncryptor`. Real KMS calls.
- ``VOITTA_KMS_KEY`` unset -> :class:`PassthroughEncryptor`. The
  database stores plaintext. Logs a warning at first call so a
  misconfigured prod doesn't silently downgrade to no-encryption.

A SQLAlchemy :class:`EncryptedString` ``TypeDecorator`` is the entry
point used by the model layer; ``process_bind_param`` encrypts on
write, ``process_result_value`` decrypts on read.

This is a fresh-deploy feature. Pre-existing rows in
``folder_sync_sources`` are NOT migrated; the issue body documents
that explicitly. New deploys never hold plaintext on disk.
"""

from __future__ import annotations

import base64
import logging
import threading
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import String
from sqlalchemy.types import TypeDecorator

if TYPE_CHECKING:
    from google.cloud.kms_v1 import KeyManagementServiceClient

logger = logging.getLogger(__name__)


class Encryptor(Protocol):
    """Encrypt + decrypt UTF-8 strings."""

    def encrypt(self, plaintext: str) -> str:
        ...

    def decrypt(self, ciphertext: str) -> str:
        ...


class PassthroughEncryptor:
    """No-op fallback used when ``VOITTA_KMS_KEY`` is unset.

    Logs a warning on first use so a misconfigured prod is loud,
    without spamming the log on every read/write.
    """

    _warned: bool = False
    _lock: threading.Lock = threading.Lock()

    def _warn_once(self) -> None:
        with self._lock:
            if not self._warned:
                logger.warning(
                    "VOITTA_KMS_KEY is unset; folder_sync_sources secret columns "
                    "are stored plaintext. Set the env var to a Cloud KMS key "
                    "name to enable at-rest encryption."
                )
                self._warned = True

    def encrypt(self, plaintext: str) -> str:
        self._warn_once()
        retval = plaintext
        return retval

    def decrypt(self, ciphertext: str) -> str:
        self._warn_once()
        retval = ciphertext
        return retval


class KMSEncryptor:
    """Direct Cloud KMS encrypt/decrypt against a single named key.

    Plaintexts are sub-64 KiB — well within KMS's per-call payload
    limit — so we don't bother with per-row DEK envelopes. The
    ciphertext returned to the database is the raw KMS ciphertext
    base64-encoded (KMS returns bytes; SQLite TEXT columns want str).
    """

    def __init__(self, key_name: str) -> None:
        self._key = key_name
        self._client: KeyManagementServiceClient | None = None
        self._lock = threading.Lock()

    def _ensure_client(self) -> KeyManagementServiceClient:
        with self._lock:
            if self._client is None:
                # Imported lazily so unit tests that exercise the
                # passthrough path don't need google-cloud-kms wheels.
                from google.cloud.kms_v1 import KeyManagementServiceClient

                self._client = KeyManagementServiceClient()
            retval = self._client
            return retval

    def encrypt(self, plaintext: str) -> str:
        client = self._ensure_client()
        response = client.encrypt(
            request={"name": self._key, "plaintext": plaintext.encode("utf-8")}
        )
        retval = base64.b64encode(response.ciphertext).decode("ascii")
        return retval

    def decrypt(self, ciphertext: str) -> str:
        client = self._ensure_client()
        ct_bytes = base64.b64decode(ciphertext.encode("ascii"))
        response = client.decrypt(request={"name": self._key, "ciphertext": ct_bytes})
        retval = response.plaintext.decode("utf-8")
        return retval


_encryptor_lock = threading.Lock()
_encryptor: Encryptor | None = None


def get_encryptor() -> Encryptor:
    """Return the process-wide :class:`Encryptor`.

    Singleton: the first call resolves ``VOITTA_KMS_KEY`` and pins
    the choice. Tests reset it via :func:`_reset_encryptor_for_tests`.
    """

    global _encryptor
    with _encryptor_lock:
        if _encryptor is None:
            from ..config import get_settings

            key = get_settings().kms_key
            _encryptor = KMSEncryptor(key) if key else PassthroughEncryptor()
        retval = _encryptor
    return retval


def _reset_encryptor_for_tests() -> None:
    """Drop the cached encryptor so a test can swap settings/env."""

    global _encryptor
    with _encryptor_lock:
        _encryptor = None


def _set_encryptor_for_tests(encryptor: Encryptor) -> None:
    """Inject a deterministic test encryptor without touching settings."""

    global _encryptor
    with _encryptor_lock:
        _encryptor = encryptor


class EncryptedString(TypeDecorator[str]):
    """SQLAlchemy column type that encrypts on write, decrypts on read.

    Wrap a column with ``mapped_column(EncryptedString)`` and the
    ORM transparently round-trips through :func:`get_encryptor`.
    ``None`` is preserved as ``None``.
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Any) -> str | None:
        if value is None:
            retval: str | None = None
        else:
            retval = get_encryptor().encrypt(value)
        return retval

    def process_result_value(self, value: str | None, dialect: Any) -> str | None:
        if value is None:
            retval: str | None = None
        else:
            retval = get_encryptor().decrypt(value)
        return retval
