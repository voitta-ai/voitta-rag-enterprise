"""Tests for the ACL user resolver."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from voitta_image_rag.services.acl import ROOT_EMAIL, resolve_user_email


def _reset() -> None:
    from voitta_image_rag.config import reset_settings_cache

    reset_settings_cache()


def test_single_user_mode_returns_root(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOITTA_SINGLE_USER", "true")
    _reset()
    assert resolve_user_email(None, None) == ROOT_EMAIL
    assert resolve_user_email("ignored@x.com", None) == ROOT_EMAIL


def test_dev_user_mode(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOITTA_DEV_USER", "alice@example.com")
    _reset()
    assert resolve_user_email(None, None) == "alice@example.com"


def test_proxy_header_used_in_multi_user(env: None) -> None:
    assert resolve_user_email("bob@example.com", None) == "bob@example.com"


def test_x_user_name_used_when_no_proxy_header(env: None) -> None:
    assert resolve_user_email(None, "carol@example.com") == "carol@example.com"


def test_proxy_header_takes_priority(env: None) -> None:
    assert resolve_user_email("a@x", "b@x") == "a@x"


def test_unauthenticated_raises_401(env: None) -> None:
    with pytest.raises(HTTPException) as ei:
        resolve_user_email(None, None)
    assert ei.value.status_code == 401


def test_single_user_overrides_dev_user(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOITTA_SINGLE_USER", "true")
    monkeypatch.setenv("VOITTA_DEV_USER", "alice@example.com")
    _reset()
    assert resolve_user_email(None, None) == ROOT_EMAIL
