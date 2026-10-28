"""The frozen app acting as ``python`` for its own subprocesses
(``voitta_rag_desktop.__main__._maybe_run_as_interpreter``)."""

from __future__ import annotations

import multiprocessing.spawn
import sys

import pytest

from voitta_rag_desktop import __main__ as desktop


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["-m", "pkg.mod", "x"], ["-m", "pkg.mod", "x"]),
        (["-B", "-I", "-c", "code", "--multiprocessing-fork"], ["-c", "code", "--multiprocessing-fork"]),
        (["-BI", "-c", "code"], ["-c", "code"]),
        (["-X", "utf8", "-W", "ignore", "-m", "mod"], ["-m", "mod"]),
        (["-Xutf8", "-Wignore", "-c", "code"], ["-c", "code"]),
        ([], []),
        (["--some-app-flag"], ["--some-app-flag"]),  # not an interpreter option
        (["-psn_0_12345"], ["-psn_0_12345"]),  # macOS Finder launch argument
    ],
)
def test_strip_interpreter_options(args, expected) -> None:
    assert desktop._strip_interpreter_options(args) == expected


def test_spawn_child_command_runs_as_interpreter(monkeypatch) -> None:
    """MinerU 4's render pool starts workers with multiprocessing spawn,
    which puts the interpreter's flags before ``-c``. Before the fix the
    shim missed ``-c``, the worker fell through to the menu-bar launch,
    took the multiprocessing-child exit, and every PDF failed with
    "PDF render worker disconnected"."""
    monkeypatch.setattr(
        multiprocessing.spawn.util, "_args_from_interpreter_flags", lambda: ["-B", "-I"]
    )
    cmd = multiprocessing.spawn.get_command_line(pipe_handle=7)
    assert cmd[1:3] == ["-B", "-I"]  # the shape the shim has to accept

    ran: list[str] = []
    monkeypatch.setattr(desktop, "_ensure_userbase_on_path", lambda: None)
    monkeypatch.setattr(sys, "argv", [sys.executable, "-B", "-I", "-c", "ran.append('yes')"])
    monkeypatch.setitem(__builtins__ if isinstance(__builtins__, dict) else vars(__builtins__), "ran", ran)
    assert desktop._maybe_run_as_interpreter() == 0
    assert ran == ["yes"]
