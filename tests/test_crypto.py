"""Tests for services/crypto.py — passthrough + a fake-KMS round-trip.

The optional ``test_round_trip_real_kms`` integration check runs only
when ``VOITTA_KMS_KEY`` is set in the environment so CI doesn't pay
KMS-call dollars and tests that just want unit coverage stay
hermetic.
"""

from __future__ import annotations

import base64
import os

import pytest

from voitta_image_rag.services import crypto


class FakeEncryptor:
    """Reversible non-secret transform — base64 of the reversed input.

    Different from the plaintext (so a missing TypeDecorator would be
    caught by an equality assertion), but trivially reversible without
    any external dependency.
    """

    def encrypt(self, plaintext: str) -> str:
        retval = base64.b64encode(plaintext[::-1].encode("utf-8")).decode("ascii")
        return retval

    def decrypt(self, ciphertext: str) -> str:
        retval = base64.b64decode(ciphertext.encode("ascii")).decode("utf-8")[::-1]
        return retval


@pytest.fixture
def fake_encryptor() -> FakeEncryptor:
    encryptor = FakeEncryptor()
    crypto._set_encryptor_for_tests(encryptor)
    yield encryptor
    crypto._reset_encryptor_for_tests()


def test_passthrough_round_trip() -> None:
    crypto._set_encryptor_for_tests(crypto.PassthroughEncryptor())
    try:
        e = crypto.get_encryptor()
        assert e.encrypt("hello") == "hello"
        assert e.decrypt("hello") == "hello"
    finally:
        crypto._reset_encryptor_for_tests()


def test_fake_encryptor_round_trip(fake_encryptor: FakeEncryptor) -> None:
    e = crypto.get_encryptor()
    plaintext = "ghp_supersecret123"
    ciphertext = e.encrypt(plaintext)
    assert ciphertext != plaintext
    assert e.decrypt(ciphertext) == plaintext


def test_encrypted_string_round_trip_through_sqlalchemy(
    fake_encryptor: FakeEncryptor,
) -> None:
    """The TypeDecorator should encrypt on write and decrypt on read.

    Uses SA Core (not ORM) to avoid `Mapped[]` annotation resolution
    issues with classes defined in nested scope under
    ``from __future__ import annotations``.
    """
    from sqlalchemy import Column, Integer, MetaData, Table, create_engine, insert, select

    metadata = MetaData()
    rows = Table(
        "rows",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("secret", crypto.EncryptedString),
    )

    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)

    plaintext = "gd_refresh_token_value"
    with engine.begin() as conn:
        conn.execute(insert(rows).values(id=1, secret=plaintext))

    # Read back via the typed column — should decrypt.
    with engine.connect() as conn:
        decrypted = conn.execute(select(rows.c.secret).where(rows.c.id == 1)).scalar()
    assert decrypted == plaintext

    # Read raw to confirm what's on disk is the ciphertext.
    with engine.connect() as conn:
        from sqlalchemy import text

        raw_value = conn.execute(text("SELECT secret FROM rows WHERE id = 1")).scalar()
    assert raw_value != plaintext
    assert raw_value == fake_encryptor.encrypt(plaintext)


def test_encrypted_string_preserves_none(fake_encryptor: FakeEncryptor) -> None:
    """``None`` must stay ``None`` — the TypeDecorator skips encrypt."""
    decorator = crypto.EncryptedString()
    assert decorator.process_bind_param(None, None) is None
    assert decorator.process_result_value(None, None) is None


@pytest.mark.skipif(
    not os.environ.get("VOITTA_KMS_KEY"),
    reason="Set VOITTA_KMS_KEY (and ADC) to run real-KMS integration test.",
)
def test_round_trip_real_kms() -> None:
    """Opt-in: real Cloud KMS round-trip. Requires ADC + a real key."""
    crypto._reset_encryptor_for_tests()
    e = crypto.get_encryptor()
    assert isinstance(e, crypto.KMSEncryptor)
    plaintext = "real-kms-roundtrip-marker"
    assert e.decrypt(e.encrypt(plaintext)) == plaintext
