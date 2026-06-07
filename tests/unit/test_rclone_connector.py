"""rclone connector — paste parsing, config plumbing, and a live mirror.

The parsing/encoding tests are pure and always run. The end-to-end mirror test
drives the connector's real ``_run_streaming`` against a local→local
``rclone sync`` (no OAuth, no network) and is skipped when the binary is absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from voitta_rag_enterprise.services.sync.rclone import (
    RcloneAuth,
    RcloneConnector,
    RcloneSyncStats,
    _read_config_token,
    _write_excludes,
    _write_temp_config,
    coerce_extra,
    encode_extra,
    parse_pasted_config,
    rclone_available,
    rclone_bin,
)

# --- parsing ---------------------------------------------------------------


def test_parse_bare_token() -> None:
    backend, token, extra = parse_pasted_config('{"access_token":"x","refresh_token":"y"}')
    assert backend == ""  # bare token: backend comes from the form picker
    assert token == '{"access_token":"x","refresh_token":"y"}'
    assert extra == {}


def test_parse_drive_config_block() -> None:
    block = (
        "[gd]\n"
        "type = drive\n"
        'token = {"access_token":"x"}\n'
        "team_drive = 0ABC\n"
        "client_id = foo.apps\n"
    )
    backend, token, extra = parse_pasted_config(block)
    assert backend == "drive"
    assert token == '{"access_token":"x"}'
    assert extra == {"team_drive": "0ABC", "client_id": "foo.apps"}


def test_parse_onedrive_sharepoint_block() -> None:
    block = (
        "[sp]\n"
        "type = onedrive\n"
        'token = {"access_token":"z","refresh_token":"r"}\n'
        "drive_id = b!abc\n"
        "drive_type = documentLibrary\n"
    )
    backend, _token, extra = parse_pasted_config(block)
    assert backend == "onedrive"
    assert extra["drive_id"] == "b!abc"
    assert extra["drive_type"] == "documentLibrary"


def test_parse_drops_unknown_keys() -> None:
    block = "type = drive\ntoken = {\"a\":1}\nbogus_key = leak\n"
    _, _, extra = parse_pasted_config(block)
    assert "bogus_key" not in extra


def test_parse_empty() -> None:
    assert parse_pasted_config("") == ("", "", {})
    assert parse_pasted_config("   ") == ("", "", {})


def test_extra_roundtrip() -> None:
    extra = {"drive_id": "b!x", "drive_type": "documentLibrary"}
    assert coerce_extra(encode_extra(extra)) == extra
    assert encode_extra({}) is None
    assert coerce_extra(None) == {}


def test_auth_configured() -> None:
    assert not RcloneAuth().configured
    assert not RcloneAuth(backend="drive").configured  # token missing
    assert RcloneAuth(backend="drive", token="{}").configured


# --- temp config plumbing --------------------------------------------------


def test_write_and_read_back_token(tmp_path: Path) -> None:
    auth = RcloneAuth(
        backend="onedrive",
        token='{"access_token":"a","refresh_token":"r"}',
        extra={"drive_id": "b!x", "drive_type": "documentLibrary"},
    )
    conf = _write_temp_config(auth, dirpath=tmp_path)
    text = conf.read_text()
    assert "type = onedrive" in text
    assert "drive_id = b!x" in text
    # 600 perms — the token is a secret.
    assert (conf.stat().st_mode & 0o777) == 0o600
    assert _read_config_token(conf) == '{"access_token":"a","refresh_token":"r"}'


def test_write_excludes_translates_ignore_globs(tmp_path: Path) -> None:
    path = _write_excludes(tmp_path)
    assert path is not None
    lines = path.read_text().splitlines()
    # Each name-glob yields both ``P`` and ``P/**`` filter forms.
    assert "node_modules" in lines
    assert "node_modules/**" in lines


# --- live mirror (skipped without the binary) ------------------------------


@pytest.mark.skipif(not rclone_available(), reason="rclone binary not installed")
def test_live_mirror_counts_and_excludes(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "a.txt").write_text("hello")
    (src / "b.txt").write_text("world")
    (src / "sub").mkdir()
    (src / "sub" / "c.txt").write_text("deep")
    (src / "junk.tmp").write_text("ignore me")  # matches *.tmp ignore glob
    (dst / "old.txt").write_text("stale")  # mirror should delete

    excludes = _write_excludes(tmp_path)
    stats = RcloneSyncStats()
    emits: list[tuple] = []
    cmd = [
        rclone_bin(), "sync", str(src), str(dst),
        "--use-json-log", "-v", "--stats", "1s", "--stats-log-level", "NOTICE",
        "--track-renames", "--exclude-from", str(excludes),
    ]
    RcloneConnector()._run_streaming(
        cmd, stats=stats, emit=lambda *a, **k: emits.append(a)
    )

    assert stats.files_added == 3
    assert stats.files_removed == 1
    assert not stats.errors
    assert not (dst / "old.txt").exists()  # mirror-deleted
    assert not (dst / "junk.tmp").exists()  # excluded
    assert (dst / "sub" / "c.txt").exists()
    # A downloading progress event was emitted.
    assert any(e and e[0] == "downloading" for e in emits)
