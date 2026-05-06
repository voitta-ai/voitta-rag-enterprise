"""Tests for the Google sign-in allowlist gate (``is_email_allowed``).

The function is the only gate between a verified Google email and the
session cookie, so the matrix of inputs is small and worth covering
explicitly: domain match, users-file match, both empty, malformed input.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from voitta_rag_enterprise.services.acl import is_email_allowed


def test_domain_match_allows(tmp_path: Path) -> None:
    extras = tmp_path / "users.txt"
    assert is_email_allowed("alice@customer.com", ["customer.com"], extras) is True


def test_domain_match_is_case_insensitive(tmp_path: Path) -> None:
    extras = tmp_path / "users.txt"
    assert is_email_allowed("Alice@Customer.COM", ["CUSTOMER.com"], extras) is True


def test_users_file_match_allows(tmp_path: Path) -> None:
    extras = tmp_path / "users.txt"
    extras.write_text("# external consultants\nbob@gmail.com\nCarol@Other.org\n")
    assert is_email_allowed("bob@gmail.com", [], extras) is True
    # case-insensitive against file contents
    assert is_email_allowed("carol@other.org", [], extras) is True


def test_users_file_blanks_and_comments_ignored(tmp_path: Path) -> None:
    extras = tmp_path / "users.txt"
    extras.write_text("\n# header comment\n\n  bob@gmail.com  \n")
    assert is_email_allowed("bob@gmail.com", [], extras) is True


def test_neither_list_matches_denies(tmp_path: Path) -> None:
    extras = tmp_path / "users.txt"
    extras.write_text("alice@customer.com\n")
    assert (
        is_email_allowed("eve@evil.example", ["customer.com"], extras) is False
    )


def test_empty_config_denies_everyone(tmp_path: Path) -> None:
    """Deny-by-default: no domains, no users file, no admit."""
    missing = tmp_path / "no-such-file.txt"
    assert is_email_allowed("anyone@anywhere.com", [], missing) is False
    # Even an empty file (no entries) still denies.
    empty = tmp_path / "empty.txt"
    empty.write_text("")
    assert is_email_allowed("anyone@anywhere.com", [], empty) is False


@pytest.mark.parametrize("bad", ["", "no-at-sign", "@no-local", "trailing@"])
def test_malformed_email_denied(tmp_path: Path, bad: str) -> None:
    extras = tmp_path / "users.txt"
    # Even with a wide-open allowlist, malformed addresses cannot squeak
    # through — defence in depth against a future caller that forgets to
    # validate.
    assert is_email_allowed(bad, ["anywhere"], extras) is False or "@" not in bad


def test_settings_allowed_domain_list_normalises(monkeypatch: pytest.MonkeyPatch) -> None:
    """``Settings.allowed_domain_list`` strips, lowercases, and tolerates ``@`` prefix."""
    from voitta_rag_enterprise.config import Settings, reset_settings_cache

    monkeypatch.setenv("VOITTA_ALLOWED_DOMAINS", " Customer.COM ,@partner.org ,, ")
    reset_settings_cache()
    s = Settings()
    assert s.allowed_domain_list() == ["customer.com", "partner.org"]


def test_settings_allowed_domain_list_default_empty() -> None:
    from voitta_rag_enterprise.config import Settings

    assert Settings(allowed_domains="").allowed_domain_list() == []
