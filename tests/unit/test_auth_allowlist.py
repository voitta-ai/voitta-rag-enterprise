"""Tests for the Google sign-in gate (`admin_store.is_email_allowed`).

This is the only function between a verified Google email and the
session cookie, so the matrix of inputs is small and worth covering
explicitly: domain match, allowed-users match, super-admin bypass,
block-list precedence, and the deny-by-default empty-config case.
"""

from __future__ import annotations

import pytest


def _reset() -> None:
    from voitta_rag_enterprise.config import reset_settings_cache

    reset_settings_cache()


def test_domain_match_allows(env: None) -> None:
    from voitta_rag_enterprise.services import admin_store

    admin_store.add_allowed_domain("customer.com")
    assert admin_store.is_email_allowed("alice@customer.com") is True


def test_domain_match_is_case_insensitive(env: None) -> None:
    from voitta_rag_enterprise.services import admin_store

    admin_store.add_allowed_domain("CUSTOMER.com")
    assert admin_store.is_email_allowed("Alice@Customer.COM") is True


def test_allowed_user_match_allows(env: None) -> None:
    from voitta_rag_enterprise.services import admin_store

    admin_store.add_allowed_user("bob@gmail.com")
    assert admin_store.is_email_allowed("bob@gmail.com") is True
    # case-insensitive
    assert admin_store.is_email_allowed("BOB@gmail.com") is True


def test_blocklist_overrides_allowlist(env: None) -> None:
    from voitta_rag_enterprise.services import admin_store

    admin_store.add_allowed_domain("customer.com")
    admin_store.add_blocked_user("rogue@customer.com")
    assert admin_store.is_email_allowed("alice@customer.com") is True
    assert admin_store.is_email_allowed("rogue@customer.com") is False


def test_super_admin_always_allowed(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bootstrap admin must be able to sign in even when allowlists are empty —
    otherwise a fresh deploy is locked out forever.
    """
    from voitta_rag_enterprise.services import admin_store

    monkeypatch.setenv("VOITTA_SUPER_ADMINS", "boss@anywhere.com")
    _reset()
    assert admin_store.is_email_allowed("boss@anywhere.com") is True


def test_super_admin_does_not_bypass_blocklist(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Block-list trumps everything, including super-admin. Lets an operator
    revoke a compromised bootstrap admin without redeploying.
    """
    from voitta_rag_enterprise.services import admin_store

    monkeypatch.setenv("VOITTA_SUPER_ADMINS", "boss@anywhere.com")
    _reset()
    admin_store.add_blocked_user("boss@anywhere.com")
    assert admin_store.is_email_allowed("boss@anywhere.com") is False


def test_empty_config_denies_everyone(env: None) -> None:
    """Deny-by-default: no domains, no users, no super-admins, no admit."""
    from voitta_rag_enterprise.services import admin_store

    assert admin_store.is_email_allowed("anyone@anywhere.com") is False


@pytest.mark.parametrize("bad", ["", "no-at-sign", "@no-local"])
def test_malformed_email_denied(env: None, bad: str) -> None:
    from voitta_rag_enterprise.services import admin_store

    # Even with a wide-open allowlist, malformed addresses cannot squeak
    # through — defence in depth against a future caller that forgets to
    # validate before calling the gate.
    admin_store.add_allowed_domain("anywhere.example")
    assert admin_store.is_email_allowed(bad) is False


def test_remove_allowed_user(env: None) -> None:
    from voitta_rag_enterprise.services import admin_store

    admin_store.add_allowed_user("bob@gmail.com")
    admin_store.remove_allowed_user("bob@gmail.com")
    assert admin_store.is_email_allowed("bob@gmail.com") is False


def test_remove_allowed_domain(env: None) -> None:
    from voitta_rag_enterprise.services import admin_store

    admin_store.add_allowed_domain("customer.com")
    admin_store.remove_allowed_domain("customer.com")
    assert admin_store.is_email_allowed("alice@customer.com") is False


def test_settings_super_admin_list(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from voitta_rag_enterprise.config import Settings

    monkeypatch.setenv("VOITTA_SUPER_ADMINS", " A@x.com , b@y.com , , ")
    s = Settings()
    assert s.super_admin_list() == ["a@x.com", "b@y.com"]


def test_settings_super_admin_list_skips_non_emails(env: None) -> None:
    from voitta_rag_enterprise.config import Settings

    s = Settings(super_admins="not-an-email,bob@y.com")
    assert s.super_admin_list() == ["bob@y.com"]
