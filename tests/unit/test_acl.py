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
    assert resolve_user_email() == ROOT_EMAIL
    # Even when a session is present, single-user wins.
    assert resolve_user_email("ignored@x.com") == ROOT_EMAIL


def test_dev_user_mode(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOITTA_DEV_USER", "alice@example.com")
    _reset()
    assert resolve_user_email() == "alice@example.com"


def test_session_email_used_in_multi_user(env: None) -> None:
    assert resolve_user_email("dave@example.com") == "dave@example.com"


def test_unauthenticated_raises_401(env: None) -> None:
    with pytest.raises(HTTPException) as ei:
        resolve_user_email()
    assert ei.value.status_code == 401


def test_single_user_overrides_dev_user(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOITTA_SINGLE_USER", "true")
    monkeypatch.setenv("VOITTA_DEV_USER", "alice@example.com")
    _reset()
    assert resolve_user_email() == ROOT_EMAIL


def test_dev_user_overrides_session(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    # Dev shortcut must dominate the session so local development doesn't
    # drift to whatever cookie happens to be sitting in the browser.
    monkeypatch.setenv("VOITTA_DEV_USER", "dev@example.com")
    _reset()
    assert resolve_user_email("primary@example.com") == "dev@example.com"
