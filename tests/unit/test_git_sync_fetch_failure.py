"""A failed ``git fetch`` must surface as an error, never as a quiet success."""

from __future__ import annotations

import pytest

from voitta_rag_enterprise.services.sync import github


def _connector():
    return github.GitHubConnector()


def _sync(tmp_path, **kw):
    return _connector()._sync_sync(
        folder_root=tmp_path,
        repo_url="git@example.invalid:o/r.git",
        subfolder="",
        branches=kw.pop("branches", ["main"]),
        all_branches=False,
        extended=False,
        auth=None,
        **kw,
    )


def test_all_fetches_failing_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(github, "_ensure_mirror", lambda *a, **k: None)
    monkeypatch.setattr(
        github, "_fetch_refs", lambda *a, **k: (128, "", "git@x: Permission denied (publickey).")
    )
    with pytest.raises(RuntimeError, match="Permission denied"):
        _sync(tmp_path)


def test_partial_fetch_failure_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(github, "_ensure_mirror", lambda *a, **k: None)

    def fetch(mirror, refspecs, auth):
        if len(refspecs) > 1 or "gone" in refspecs[0]:
            return (1, "", "fatal: couldn't find remote ref gone")
        return (0, "", "")

    monkeypatch.setattr(github, "_fetch_refs", fetch)
    monkeypatch.setattr(github, "_ref_exists", lambda *a, **k: True)
    monkeypatch.setattr(github, "_materialize_branch", lambda *a, **k: None)
    stats = _sync(tmp_path, branches=["main", "gone"])
    assert stats.branches_synced == 1
    assert any(e.startswith("gone: git fetch failed") for e in stats.errors)


_PUB = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAACAQCuBis1huRY8N85 nadya@mac"


def test_public_key_rejected_with_clear_message():
    msg = github.validate_ssh_private_key(_PUB)
    assert msg and "PUBLIC key" in msg


def test_garbage_and_truncated_keys_rejected():
    assert github.validate_ssh_private_key("hello")
    assert "truncated" in github.validate_ssh_private_key(
        "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n"
    )
    assert github.validate_ssh_private_key(
        "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"
    )


def test_real_private_key_accepted_and_empty_ok(tmp_path):
    import subprocess

    k = tmp_path / "k"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(k)], check=True
    )
    assert github.validate_ssh_private_key(k.read_text()) is None
    assert github.validate_ssh_private_key("") is None


def test_passphrase_key_rejected(tmp_path):
    import subprocess

    k = tmp_path / "k"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "secret", "-f", str(k)], check=True
    )
    assert "passphrase" in github.validate_ssh_private_key(k.read_text())
