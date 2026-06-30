"""Git sync connector — clones (or pulls) selected branches into the folder
and mirrors the repository contents onto disk so the standard watcher /
extract pipeline picks them up.

Disk layout under the folder root:

    branches/<safe-branch>/                 # working files (HEAD of branch)
    branches/<safe-branch>/.git-repo/       # full clone — excluded from indexing
    commits/<sha-short>-<safe-subject>.md   # extended mode: per-commit history,
                                            #   one md per unique commit across
                                            #   all selected branches

Branch names with ``/`` (``feature/x``) become ``feature--x`` on disk.

Concurrency
-----------
A module-level threading lock serializes every git invocation for one process
so concurrent sync jobs cannot stomp on each other's worktrees or share an
ssh-agent socket.

Authentication
--------------
Two modes supported, mirroring voitta-rag:

* ``ssh``   — write the SSH private key to a temp file and set
              ``GIT_SSH_COMMAND``; ``StrictHostKeyChecking=accept-new`` keeps
              first-time clones unattended.
* ``token`` — set ``GIT_ASKPASS`` to a tiny shell script that echoes the PAT,
              and (defensively) inject ``user:token@`` into HTTPS URLs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from .base import SyncConnector

logger = logging.getLogger(__name__)

# One process-wide lock around every git call. Cheap (we just hold it for the
# duration of a clone/pull) and saves us from worrying about concurrent .git
# state across worker tasks.
_GIT_LOCK = threading.Lock()

_SAFE_NAME_RE = re.compile(r"[<>:\"/\\|?*\s]")


@dataclass
class GitAuth:
    """Either an SSH private key or a (username, PAT) pair."""

    method: str  # "ssh" or "token"
    ssh_key: str = ""
    username: str = ""
    pat: str = ""


@dataclass
class GitSyncStats:
    branches_synced: int = 0
    commits_written: int = 0
    branches_removed: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "branches_synced": self.branches_synced,
            "commits_written": self.commits_written,
            "branches_removed": self.branches_removed,
            "errors": self.errors,
        }


def _safe_name(name: str) -> str:
    """Sanitize a branch name or commit subject for use as a filesystem path
    component. Forward slashes become ``--`` so ``feature/x`` round-trips.
    """
    out = name.replace("/", "--")
    out = _SAFE_NAME_RE.sub("-", out)
    out = re.sub(r"-{2,}", "-", out).strip("-")
    return out[:80]


def _inject_token_into_url(repo_url: str, username: str, token: str) -> str:
    parsed = urlparse(repo_url)
    if not parsed.scheme.startswith("http"):
        return repo_url  # SSH URL — no rewriting
    netloc = f"{username}:{token}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


def _clean_git_stderr(stderr: str) -> str:
    """Trim git/ssh stderr to the actionable lines for UI display.

    OpenSSH prints an ``@``-bordered warning box (e.g. "UNPROTECTED PRIVATE KEY
    FILE") that renders as a wall of ``@`` in a one-line error toast. Drop those
    border lines and the boilerplate advisory, keeping the lines that actually
    say what went wrong (Permission denied, bad permissions, fatal: …).
    """
    keep: list[str] = []
    for raw in stderr.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Strip the warning-box '@' border (whole-line border, or '@ … @' around
        # the text) before inspecting the content.
        inner = line.strip("@").strip()
        if not inner:
            continue
        # Boilerplate that adds noise but no signal.
        if inner.startswith(
            (
                "It is required that your private key",
                "This private key will be ignored",
                "WARNING: UNPROTECTED PRIVATE KEY FILE",
            )
        ):
            continue
        keep.append(inner)
    cleaned = "; ".join(keep).strip()
    return cleaned or stderr.strip()


# Cache for the (expensive) login-shell probe — the agent socket is stable for
# the process's lifetime, so we resolve it at most once. Only successes are
# cached, so a user who fixes their setup mid-session isn't stuck on a miss.
_LOGIN_SHELL_SOCK: str | None = None


def _ssh_auth_sock_from_login_shell() -> str | None:
    """Read ``SSH_AUTH_SOCK`` the way an interactive terminal would.

    This is the "rely on what just works" path: ``git`` succeeds in the user's
    terminal because their shell rc (``~/.zprofile`` / ``~/.zshrc``) exports the
    agent socket. A GUI app never runs those files, so we spawn the user's login
    + interactive shell once and read the value back — identical to opening a
    terminal and echoing the variable. Output is marker-wrapped so a shell
    banner / MOTD can't pollute the captured value.
    """
    global _LOGIN_SHELL_SOCK
    if _LOGIN_SHELL_SOCK:
        return _LOGIN_SHELL_SOCK
    shell = os.environ.get("SHELL") or "/bin/zsh"
    # -l sources login files (~/.zprofile), -i sources interactive files
    # (~/.zshrc) — agent exports live in one or the other depending on setup.
    script = 'printf "__VOITTA_SOCK__%s__END__" "$SSH_AUTH_SOCK"'
    try:
        out = subprocess.run(
            [shell, "-l", "-i", "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"__VOITTA_SOCK__(.*?)__END__", out.stdout, re.DOTALL)
    if m and m.group(1).strip():
        _LOGIN_SHELL_SOCK = m.group(1).strip()
        return _LOGIN_SHELL_SOCK
    return None


def _sock_from_launchctl() -> str | None:
    """macOS ``launchctl getenv SSH_AUTH_SOCK`` — the launchd-domain value a
    GUI .app inherits. Often the *default* per-session agent, which may NOT hold
    the user's hardware key (that's why we key-check candidates below)."""
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(
            ["launchctl", "getenv", "SSH_AUTH_SOCK"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _agent_has_keys(sock: str) -> bool:
    """True if the agent at ``sock`` has at least one identity loaded.

    ``ssh-add -l`` exits 0 when identities are present, 1 when the agent is
    reachable but empty, 2 when it can't connect. Only 0 means "this agent can
    actually authenticate", which is exactly how we disambiguate the right
    agent from a present-but-empty default one.
    """
    try:
        r = subprocess.run(
            ["ssh-add", "-l"],
            env={**os.environ, "SSH_AUTH_SOCK": sock},
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _resolve_ssh_auth_sock() -> str | None:
    """Find the ssh-agent socket that can actually sign — for hardware-key
    (YubiKey / SSHCA / touch2ssh) auth.

    An explicit ``VOITTA_SSH_AUTH_SOCK`` override always wins. Otherwise we
    gather every candidate — the process env, macOS ``launchctl``, and the
    user's login+interactive shell (``git`` works in their terminal precisely
    because the shell rc exports the socket there) — and pick the first whose
    agent has **keys loaded**. This is the crucial bit: a Mac often has a
    default launchd agent that's reachable but *empty*, while the real YubiKey
    agent lives on a different socket from the user's shell. Key-checking avoids
    locking onto the empty one. Falls back to the first candidate if none can be
    probed (e.g. ``ssh-add`` missing).

    Returns the socket path, or None when no agent can be located.
    """
    override = os.environ.get("VOITTA_SSH_AUTH_SOCK")
    if override:
        return override

    candidates: list[str] = []
    for src in (
        os.environ.get("SSH_AUTH_SOCK"),
        _sock_from_launchctl(),
        _ssh_auth_sock_from_login_shell(),
    ):
        if src and src not in candidates:
            candidates.append(src)

    if not candidates:
        return None
    for sock in candidates:
        if _agent_has_keys(sock):
            logger.info("git ssh-agent: using %s (has keys)", sock)
            return sock
    # None verified — return the first and let ssh try (ssh-add may be absent,
    # or the agent needs a touch to even list). Logged so a failure is debuggable.
    logger.warning(
        "git ssh-agent: no candidate socket reported loaded keys; trying %s. "
        "Candidates: %s", candidates[0], candidates
    )
    return candidates[0]


def _git_env(auth: GitAuth | None) -> tuple[dict[str, str], list[str]]:
    """Build the env + tempfiles for a single git invocation.

    Returns ``(env, cleanup_paths)``. The caller must unlink ``cleanup_paths``
    once the subprocess has exited.
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    cleanup: list[str] = []
    if auth is None:
        env["GIT_SSH_COMMAND"] = (
            "ssh -F /dev/null"
            " -o StrictHostKeyChecking=accept-new"
            " -o BatchMode=yes"
        )
        return env, cleanup

    if auth.method == "token" and auth.pat.strip():
        askpass = tempfile.NamedTemporaryFile(  # noqa: SIM115
            mode="w", suffix=".sh", delete=False
        )
        askpass.write(f"#!/bin/sh\necho '{auth.pat.strip()}'\n")
        askpass.close()
        os.chmod(askpass.name, stat.S_IRWXU)
        env["GIT_ASKPASS"] = askpass.name
        cleanup.append(askpass.name)
        return env, cleanup

    if auth.method == "ssh" and auth.ssh_key.strip():
        keyfile = tempfile.NamedTemporaryFile(  # noqa: SIM115
            mode="w", suffix=".key", delete=False
        )
        body = auth.ssh_key.strip()
        if not body.endswith("\n"):
            body += "\n"
        keyfile.write(body)
        keyfile.close()
        os.chmod(keyfile.name, 0o600)
        env["GIT_SSH_COMMAND"] = (
            f"ssh -i {keyfile.name}"
            " -F /dev/null"
            " -o StrictHostKeyChecking=accept-new"
            " -o BatchMode=yes"
            " -o IdentitiesOnly=yes"
        )
        cleanup.append(keyfile.name)
        return env, cleanup

    # SSH agent mode — use the host's running ssh-agent + ``~/.ssh/config``.
    # This is the ONLY way hardware-backed keys work (YubiKey / SSHCA /
    # touch2ssh): the private key never leaves the device, so there's nothing to
    # paste, and the cert + host mappings live in ``~/.ssh/config``. Crucially we
    # do NOT pass ``-F /dev/null`` here (that would discard ``~/.ssh/config`` and
    # break the agent flow — e.g. the harmless "CARD AUTH pubkey ... agent
    # refused operation" line then never falls through to the working cert key).
    # We inherit ``SSH_AUTH_SOCK`` via ``os.environ`` above. ``BatchMode=yes``
    # keeps a headless run from hanging on a prompt; a YubiKey touch/PIN is
    # handled by the agent out-of-band and is unaffected by it.
    #
    # Reached when method is explicitly "agent", or "ssh" with no pasted key
    # (the natural choice for an agent user).
    if auth.method in ("agent", "ssh"):
        # Make the ssh-agent reachable. A GUI .app (or headless service) won't
        # have SSH_AUTH_SOCK in its env, so resolve it from the override / env /
        # launchctl and inject it. Without an agent, ssh falls back to on-disk
        # ~/.ssh keys — which for a hardware key (YubiKey/SSHCA) can't sign, so
        # auth fails with "Permission denied (publickey)".
        sock = _resolve_ssh_auth_sock()
        if sock:
            env["SSH_AUTH_SOCK"] = sock
        else:
            logger.warning(
                "git SSH agent mode: no ssh-agent socket found (SSH_AUTH_SOCK "
                "unset, VOITTA_SSH_AUTH_SOCK unset, launchctl empty). Hardware "
                "keys (YubiKey/SSHCA) need the agent and will fail. In a working "
                "terminal run: launchctl setenv SSH_AUTH_SOCK \"$SSH_AUTH_SOCK\""
            )
        env["GIT_SSH_COMMAND"] = (
            "ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
        )
        return env, cleanup

    # No auth configured — anonymous (works for public repos over HTTPS).
    env["GIT_SSH_COMMAND"] = (
        "ssh -F /dev/null -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
    )
    return env, cleanup


def _maybe_token_url(repo_url: str, auth: GitAuth | None) -> str:
    if auth and auth.method == "token" and auth.pat.strip():
        username = (auth.username or "x-access-token").strip()
        return _inject_token_into_url(repo_url, username, auth.pat.strip())
    return repo_url


def _run_git(
    args: list[str],
    *,
    cwd: str | None = None,
    auth: GitAuth | None = None,
    timeout: int = 300,
) -> tuple[int, str, str]:
    """Run a single git command synchronously under the global lock."""
    env, cleanup = _git_env(auth)
    try:
        with _GIT_LOCK:
            proc = subprocess.run(
                ["git", *args],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        return proc.returncode, proc.stdout, proc.stderr
    finally:
        for path in cleanup:
            with contextlib.suppress(OSError):
                os.unlink(path)


def list_remote_branches(repo_url: str, auth: GitAuth | None) -> list[str]:
    """Return the branches available on the remote, sorted with main / master
    first. Used by the UI's branch picker.
    """
    url = _maybe_token_url(repo_url, auth)
    rc, stdout, stderr = _run_git(
        ["ls-remote", "--heads", url], auth=auth, timeout=30
    )
    if rc != 0:
        raise RuntimeError(f"git ls-remote failed: {_clean_git_stderr(stderr)}")
    branches: list[str] = []
    for line in stdout.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            branches.append(parts[1][len("refs/heads/"):])

    def key(b: str) -> tuple[int, str]:
        if b == "main":
            return (0, b)
        if b == "master":
            return (1, b)
        return (2, b)

    branches.sort(key=key)
    return branches


def _resolve_branches(
    repo_url: str,
    requested: list[str] | None,
    all_branches: bool,
    auth: GitAuth | None,
) -> list[str]:
    if all_branches:
        branches = list_remote_branches(repo_url, auth)
        if not branches:
            raise RuntimeError("remote has no branches")
        return branches
    if not requested:
        raise ValueError("at least one branch must be selected")
    return list(requested)


def _sync_one_branch(
    *,
    repo_url: str,
    branch: str,
    branch_root: Path,
    subfolder: str,
    auth: GitAuth | None,
) -> None:
    """Clone or fetch+reset the given branch into ``branch_root/.git-repo``,
    then mirror the (optional) subfolder into ``branch_root``.

    Files are copied with ``shutil.copy2`` so unchanged files keep their mtime
    and the SHA short-circuit in ``_run_extract_sync`` skips them. Files that
    are no longer present in the repo are deleted from ``branch_root`` so the
    watcher fires deletion events.
    """
    repo_dir = branch_root / ".git-repo"
    branch_root.mkdir(parents=True, exist_ok=True)
    auth_url = _maybe_token_url(repo_url, auth)

    if (repo_dir / ".git").exists():
        rc, _, err = _run_git(
            ["remote", "set-branches", "origin", branch],
            cwd=str(repo_dir),
            auth=auth,
        )
        if rc != 0:
            raise RuntimeError(f"git remote set-branches failed: {err.strip()}")
        rc, _, err = _run_git(
            ["fetch", "--prune", "origin"], cwd=str(repo_dir), auth=auth
        )
        if rc != 0:
            raise RuntimeError(f"git fetch failed: {err.strip()}")
        rc, _, err = _run_git(
            ["reset", "--hard", f"origin/{branch}"],
            cwd=str(repo_dir),
            auth=auth,
        )
        if rc != 0:
            raise RuntimeError(f"git reset failed: {err.strip()}")
        _run_git(["clean", "-fdx"], cwd=str(repo_dir), auth=auth)
    else:
        if repo_dir.exists():
            shutil.rmtree(repo_dir)
        rc, _, err = _run_git(
            [
                "clone",
                "--single-branch",
                "--branch",
                branch,
                auth_url,
                str(repo_dir),
            ],
            auth=auth,
        )
        if rc != 0:
            raise RuntimeError(f"git clone failed: {_clean_git_stderr(err)}")

    source_dir = repo_dir / subfolder if subfolder else repo_dir
    if not source_dir.exists():
        raise FileNotFoundError(
            f"subfolder {subfolder!r} not found in {branch} of {repo_url}"
        )

    # Build the set of files we expect to exist in branch_root after sync
    # (excluding anything under .git-repo or hidden).
    remote_paths: set[str] = set()
    for src_file in source_dir.rglob("*"):
        if src_file.is_dir() or src_file.is_symlink():
            continue
        rel = src_file.relative_to(source_dir)
        if any(p.startswith(".") for p in rel.parts):
            continue
        remote_paths.add(str(rel))

    for rel_str in sorted(remote_paths):
        src_file = source_dir / rel_str
        dst_file = branch_root / rel_str
        if dst_file.exists():
            try:
                if (
                    src_file.stat().st_size == dst_file.stat().st_size
                    and src_file.stat().st_mtime <= dst_file.stat().st_mtime
                ):
                    continue
            except OSError:
                pass
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dst_file)

    # Delete files that no longer belong to this branch (skip .git-repo).
    for f in branch_root.rglob("*"):
        if not f.is_file() or f.name.startswith("."):
            continue
        try:
            f.relative_to(repo_dir)
            continue  # under .git-repo — keep
        except ValueError:
            pass
        rel = str(f.relative_to(branch_root))
        if rel not in remote_paths:
            try:
                f.unlink()
            except OSError as e:
                logger.warning("could not unlink stale file %s: %s", f, e)

    # Tidy empty dirs (excluding .git-repo).
    for d in sorted(branch_root.rglob("*"), reverse=True):
        if not d.is_dir() or d == repo_dir:
            continue
        try:
            d.relative_to(repo_dir)
            continue
        except ValueError:
            pass
        with contextlib.suppress(OSError):
            d.rmdir()


def _dump_commits(
    *,
    repo_dir: Path,
    branch: str,
    commits_dir: Path,
    seen_paths: set[Path],
) -> int:
    """When extended mode is on, walk ``git log`` for the given branch and
    write one markdown file per *unique* commit (deduped via ``seen_paths``).
    Each file lists which branches contain that commit; we re-read existing
    files to merge the ``Branches`` row instead of overwriting.

    We track *paths* rather than SHAs because the cleanup pass below has to
    decide which on-disk files to keep. Comparing by ``sha[:7]`` against a
    filename prefix breaks the moment git's auto-abbreviation picks more than
    7 characters — which is the default for any non-trivially-sized repo —
    and would delete every commit file we just wrote.
    """
    commits_dir.mkdir(parents=True, exist_ok=True)
    fmt = "%H%x09%h%x09%an%x09%ae%x09%aI%x09%s"
    rc, stdout, stderr = _run_git(
        ["log", f"origin/{branch}", "--no-merges", "--pretty=format:" + fmt],
        cwd=str(repo_dir),
    )
    if rc != 0:
        raise RuntimeError(f"git log failed for {branch}: {stderr.strip()}")

    written = 0
    for line in stdout.splitlines():
        parts = line.split("\t", 5)
        if len(parts) < 6:
            continue
        sha, short_sha, author, email, iso_date, subject = parts
        rel = f"{short_sha}-{_safe_name(subject) or 'commit'}.md"
        path = commits_dir / rel

        if path in seen_paths:
            # Already written in this run by an earlier branch — just merge
            # this branch into the Branches row.
            _append_branch_to_commit_md(path, branch)
            continue
        seen_paths.add(path)

        body = _format_commit_md(
            sha=sha,
            short_sha=short_sha,
            author=author,
            email=email,
            iso_date=iso_date,
            subject=subject,
            branches=[branch],
            files_changed=_files_changed(repo_dir, sha),
            message_body=_commit_message_body(repo_dir, sha),
        )
        path.write_text(body, encoding="utf-8")
        written += 1
    return written


def _commit_message_body(repo_dir: Path, sha: str) -> str:
    rc, stdout, _ = _run_git(
        ["show", "-s", "--format=%B", sha], cwd=str(repo_dir)
    )
    return stdout.strip() if rc == 0 else ""


def _files_changed(repo_dir: Path, sha: str) -> list[tuple[str, str]]:
    """Return ``[(status, path), …]`` for the commit's name-status diff."""
    rc, stdout, _ = _run_git(
        ["show", "--name-status", "--pretty=format:", sha], cwd=str(repo_dir)
    )
    if rc != 0:
        return []
    rows: list[tuple[str, str]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", 1)
        if len(parts) == 2:
            rows.append((parts[0], parts[1]))
    return rows


_BRANCH_ROW_RE = re.compile(r"^\| Branches \| (?P<v>.*) \|$", re.MULTILINE)


def _append_branch_to_commit_md(path: Path, branch: str) -> None:
    """Add ``branch`` to a commit's ``Branches`` row if not already present."""
    if not path.exists():
        return
    txt = path.read_text(encoding="utf-8")
    m = _BRANCH_ROW_RE.search(txt)
    if not m:
        return
    existing = [b.strip() for b in m.group("v").split(",") if b.strip()]
    if branch in existing:
        return
    existing.append(branch)
    new_row = "| Branches | " + ", ".join(existing) + " |"
    path.write_text(txt[: m.start()] + new_row + txt[m.end():], encoding="utf-8")


def _format_commit_md(
    *,
    sha: str,
    short_sha: str,
    author: str,
    email: str,
    iso_date: str,
    subject: str,
    branches: list[str],
    files_changed: list[tuple[str, str]],
    message_body: str,
) -> str:
    lines = [f"# {short_sha} {subject}", ""]
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    lines.append(f"| SHA | {sha} |")
    lines.append(f"| Author | {author} <{email}> |")
    lines.append(f"| Date | {iso_date} |")
    lines.append("| Branches | " + ", ".join(branches) + " |")
    lines.append("")
    if message_body and message_body.strip() != subject.strip():
        lines.append("## Message")
        lines.append("")
        lines.append(message_body.strip())
        lines.append("")
    if files_changed:
        lines.append("## Files changed")
        lines.append("")
        for status, path in files_changed:
            lines.append(f"- `{status}` {path}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Connector entry point
# ---------------------------------------------------------------------------


class GitHubConnector(SyncConnector):
    """Sync a folder against a git repository (HTTPS or SSH)."""

    source_type = "github"
    supports_progress = False  # git sync doesn't emit progress callbacks

    def resolve_config(self, row) -> dict:
        return {
            "repo_url": row.gh_repo or "",
            "subfolder": row.gh_path or "",
            "branches": coerce_branches_field(row.gh_branches),
            "all_branches": bool(row.gh_all_branches),
            "extended": bool(row.gh_extended),
            "auth": GitAuth(
                method=row.gh_auth_method or "",
                ssh_key=row.gh_token or "",
                username=row.gh_username or "",
                pat=row.gh_pat or "",
            ),
        }

    async def sync(
        self,
        *,
        folder_root: Path,
        repo_url: str,
        subfolder: str,
        branches: list[str] | None,
        all_branches: bool,
        extended: bool,
        auth: GitAuth | None,
    ) -> GitSyncStats:
        return await asyncio.to_thread(
            self._sync_sync,
            folder_root=folder_root,
            repo_url=repo_url,
            subfolder=(subfolder or "").strip("/"),
            branches=branches,
            all_branches=all_branches,
            extended=extended,
            auth=auth,
        )

    def _sync_sync(
        self,
        *,
        folder_root: Path,
        repo_url: str,
        subfolder: str,
        branches: list[str] | None,
        all_branches: bool,
        extended: bool,
        auth: GitAuth | None,
    ) -> GitSyncStats:
        if not repo_url:
            raise ValueError("repo URL is required")

        folder_root = folder_root.expanduser().resolve()
        folder_root.mkdir(parents=True, exist_ok=True)

        selected = _resolve_branches(repo_url, branches, all_branches, auth)
        logger.info(
            "git sync: %s branches=%s extended=%s",
            repo_url,
            selected,
            extended,
        )

        stats = GitSyncStats()
        branches_dir = folder_root / "branches"
        branches_dir.mkdir(parents=True, exist_ok=True)
        commits_dir = folder_root / "commits"

        seen_commit_paths: set[Path] = set()
        # Tracks whether every selected branch successfully produced its
        # commit dump. If any branch's clone or git-log fails, we skip the
        # commits-dir cleanup so we don't delete files that branch was
        # supposed to keep alive.
        all_commit_dumps_clean = True
        safe_selected = {_safe_name(b) for b in selected}

        for branch in selected:
            safe = _safe_name(branch)
            branch_root = branches_dir / safe
            try:
                t0 = time.perf_counter()
                _sync_one_branch(
                    repo_url=repo_url,
                    branch=branch,
                    branch_root=branch_root,
                    subfolder=subfolder,
                    auth=auth,
                )
                stats.branches_synced += 1
                logger.info(
                    "branch synced: %s in %.1fs",
                    branch,
                    time.perf_counter() - t0,
                )
            except Exception as e:
                msg = f"{branch}: {e}"
                logger.exception("branch sync failed: %s", branch)
                stats.errors.append(msg)
                all_commit_dumps_clean = False
                continue

            if extended:
                try:
                    n = _dump_commits(
                        repo_dir=branch_root / ".git-repo",
                        branch=branch,
                        commits_dir=commits_dir,
                        seen_paths=seen_commit_paths,
                    )
                    stats.commits_written += n
                except Exception as e:
                    logger.exception("commit dump failed: %s", branch)
                    stats.errors.append(f"{branch} (commits): {e}")
                    all_commit_dumps_clean = False

        # Drop branch dirs that are no longer selected so the watcher emits
        # delete events for their files.
        if branches_dir.exists():
            for child in list(branches_dir.iterdir()):
                if child.is_dir() and child.name not in safe_selected:
                    logger.info("removing stale branch dir: %s", child.name)
                    shutil.rmtree(child)
                    stats.branches_removed += 1

        # Drop stale commit files when extended is on AND every branch's
        # commit dump succeeded. We compare on the full Path object — not on
        # a 7-char SHA prefix — because git's %h is variable-length, so a
        # prefix comparison would treat every freshly-written commit as
        # "stale" the moment the abbrev grows past 7.
        if extended and all_commit_dumps_clean and commits_dir.exists():
            stale = 0
            for f in list(commits_dir.glob("*.md")):
                if f not in seen_commit_paths:
                    f.unlink(missing_ok=True)
                    stale += 1
            if stale:
                logger.info("removed %d stale commit file(s)", stale)

        return stats


def coerce_branches_field(value: str | None) -> list[str] | None:
    """Decode the JSON-array stored in ``folder_sync_sources.gh_branches``."""
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list):
        return None
    return [str(b) for b in parsed if isinstance(b, str) and b]


def encode_branches_field(branches: list[str] | None) -> str | None:
    if not branches:
        return None
    return json.dumps(list(branches))
