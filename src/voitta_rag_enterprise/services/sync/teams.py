"""Teams meetings sync connector.

Captures every meeting we can see for the configured user (or for every
user in the tenant when ``user_mode == "all_users"``). For each meeting
we materialise:

* ``meetings/<YYYY-MM-DD>_<safe-subject>_<id8>/metadata.json``
* ``meetings/<YYYY-MM-DD>_<safe-subject>_<id8>/recap.md``
* ``meetings/<YYYY-MM-DD>_<safe-subject>_<id8>/transcript.md`` _(if available)_

Recordings are recorded as deep links inside ``recap.md`` — the MP4 is
**not** downloaded per the user's preference.

Meeting enumeration combines two sources:

1. **Organized meetings** — ``/users/{id}/onlineMeetings`` (delegated:
   ``/me/onlineMeetings``). Returns meetings the user organized; that's
   the only set Graph exposes via this endpoint.
2. **Attended meetings** — when ``include_attended`` is on and the
   ``CallRecords.Read.All`` scope is granted, we hit
   ``/communications/callRecords`` (filtered to the user's mailbox) and
   dedup against the organized list by Graph's ``meetingId``. Without
   that scope the connector logs a warning and degrades to organized-
   only — the scope-check endpoint surfaces this so the user can see
   exactly what's missing.

Timestamps from ``startDateTime`` are preserved as local file mtimes
via ``os.utime`` so a directory listing sorted by mtime reads
chronologically.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from . import microsoft_auth as msa
from .base import SyncConnector
from .microsoft_exporters import (
    RemoteEntry,
    atomic_write_text,
    fingerprint_matches,
    safe_filename,
)
from .microsoft_exporters import teams_metadata, teams_transcript

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@dataclass
class TeamsSyncStats:
    meetings_seen: int = 0
    meetings_written: int = 0
    transcripts_written: int = 0
    meetings_skipped: int = 0
    users_seen: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "meetings_seen": self.meetings_seen,
            "meetings_written": self.meetings_written,
            "transcripts_written": self.transcripts_written,
            "meetings_skipped": self.meetings_skipped,
            "users_seen": self.users_seen,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Token helper
# ---------------------------------------------------------------------------


async def _mint_token(auth: msa.MicrosoftAuth) -> str:
    if auth.method == "oauth":
        return await msa.refresh_access_token(auth)
    return await msa.get_app_only_token(auth)


# ---------------------------------------------------------------------------
# Tenant users (for "specific user" picker)
# ---------------------------------------------------------------------------


async def list_tenant_users(
    auth: msa.MicrosoftAuth, *, max_users: int = 500
) -> list[dict[str, str]]:
    """Return up to ``max_users`` tenant users for the Teams picker."""
    token = await _mint_token(auth)
    url: str | None = (
        f"{GRAPH_BASE}/users?$select=id,displayName,userPrincipalName,mail&$top=100"
    )
    out: list[dict[str, str]] = []
    async with httpx.AsyncClient(timeout=30) as client:
        while url and len(out) < max_users:
            resp = await msa.graph_get(client, url, token)
            if resp.status_code != 200:
                msa.raise_graph_error(resp, "list tenant users")
            data = resp.json()
            for u in data.get("value", []):
                out.append(
                    {
                        "id": u["id"],
                        "displayName": u.get("displayName") or "",
                        "userPrincipalName": u.get("userPrincipalName") or "",
                        "mail": u.get("mail") or "",
                    }
                )
            url = data.get("@odata.nextLink")
    out.sort(key=lambda u: (u["displayName"] or "").lower())
    return out


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class TeamsConnector(SyncConnector):
    """Mirror Teams meetings into ``folder_root/meetings/...``."""

    source_type = "teams"
    supports_progress = True

    async def sync(
        self,
        *,
        folder_root: Path,
        auth: msa.MicrosoftAuth,
        user_mode: str,
        user_id: str,
        include_attended: bool,
        progress_cb: Callable[[str, int, int, dict[str, Any] | None], None]
        | None = None,
    ) -> TeamsSyncStats:
        if not auth.configured:
            raise RuntimeError(
                "Teams not connected. Open the folder's sync settings and "
                "click Connect, or configure an app-only credential."
            )
        if user_mode not in ("me", "specific", "all_users"):
            raise RuntimeError(f"unknown teams user_mode: {user_mode!r}")
        if user_mode == "me" and auth.method != "oauth":
            raise RuntimeError(
                "user_mode='me' requires delegated OAuth — switch to "
                "'specific' or 'all_users' for app-only credentials."
            )
        if user_mode == "all_users" and auth.method == "oauth":
            raise RuntimeError(
                "user_mode='all_users' requires app-only auth — delegated "
                "tokens are scoped to a single user."
            )
        if user_mode == "specific" and not user_id:
            raise RuntimeError(
                "user_mode='specific' requires tm_user_id to be set."
            )

        folder_root = folder_root.expanduser().resolve()
        folder_root.mkdir(parents=True, exist_ok=True)
        meetings_root = folder_root / "meetings"
        meetings_root.mkdir(parents=True, exist_ok=True)

        def emit(phase: str, done: int, total: int, **detail: Any) -> None:
            if progress_cb is None:
                return
            progress_cb(phase, done, total, detail or None)

        emit("connecting", 0, 0)
        token = await _mint_token(auth)

        # Resolve target user IDs.
        async with httpx.AsyncClient(timeout=60) as client:
            target_users = await self._resolve_targets(
                client=client, token=token, auth=auth,
                user_mode=user_mode, user_id=user_id,
            )

            stats = TeamsSyncStats(users_seen=len(target_users))
            expected_dirs: set[str] = set()

            total = len(target_users)
            for idx, uid in enumerate(target_users):
                emit("listing", idx, total, current_user=uid)
                try:
                    await self._sync_one_user(
                        client=client,
                        token=token,
                        user_id_or_me=uid,
                        include_attended=include_attended,
                        meetings_root=meetings_root,
                        expected_dirs=expected_dirs,
                        stats=stats,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.exception("Teams user sync failed: %s", uid)
                    stats.errors.append(f"user {uid}: {e}")

            # Cleanup meetings that vanished. We only delete dirs we know
            # were expected at some point — leaves manually-added content
            # alone (defensive: this is user data).
            if meetings_root.exists():
                for child in list(meetings_root.iterdir()):
                    if not child.is_dir():
                        continue
                    if child.name not in expected_dirs:
                        logger.debug(
                            "Removing stale meeting folder: %s", child.name
                        )
                        with suppress(OSError):
                            for f in child.rglob("*"):
                                if f.is_file():
                                    f.unlink()
                            child.rmdir()

        emit("done", stats.meetings_written, stats.meetings_written)
        return stats

    # ------------------------------------------------------------------
    # Targets
    # ------------------------------------------------------------------

    async def _resolve_targets(
        self,
        *,
        client: httpx.AsyncClient,
        token: str,
        auth: msa.MicrosoftAuth,
        user_mode: str,
        user_id: str,
    ) -> list[str]:
        if user_mode == "me":
            return ["__me__"]
        if user_mode == "specific":
            return [user_id]
        # all_users
        url: str | None = f"{GRAPH_BASE}/users?$select=id&$top=100"
        out: list[str] = []
        while url:
            resp = await msa.graph_get(client, url, token)
            if resp.status_code != 200:
                msa.raise_graph_error(resp, "list tenant users for Teams sync")
            data = resp.json()
            out.extend(u["id"] for u in data.get("value", []))
            url = data.get("@odata.nextLink")
        if len(out) > 500:
            logger.warning(
                "Teams 'all_users' will iterate %d users — this is slow",
                len(out),
            )
        return out

    # ------------------------------------------------------------------
    # Per-user flow
    # ------------------------------------------------------------------

    async def _sync_one_user(
        self,
        *,
        client: httpx.AsyncClient,
        token: str,
        user_id_or_me: str,
        include_attended: bool,
        meetings_root: Path,
        expected_dirs: set[str],
        stats: TeamsSyncStats,
    ) -> None:
        # ``/me`` works only with delegated tokens; ``/users/{id}`` is
        # both delegated- and app-friendly. Transcript export always
        # needs a real user id (not 'me') so we resolve it up front
        # when running in /me mode.
        if user_id_or_me == "__me__":
            user_prefix = f"{GRAPH_BASE}/me"
            transcript_user_id = await _resolve_me_id(client, token)
        else:
            user_prefix = f"{GRAPH_BASE}/users/{user_id_or_me}"
            transcript_user_id = user_id_or_me

        organized = await self._list_organized(client, token, user_prefix)
        attended_ids: set[str] = set()
        if include_attended:
            attended_ids = await self._list_attended_meeting_ids(
                client=client, token=token, user_id=user_id_or_me,
            )
        # Attended meetings whose IDs aren't already in ``organized``:
        # fetch their full payload one by one via /onlineMeetings/{id}.
        organized_ids = {m["id"] for m in organized}
        for mid in attended_ids - organized_ids:
            extra = await self._fetch_meeting(client, token, user_prefix, mid)
            if extra is not None:
                organized.append(extra)

        stats.meetings_seen += len(organized)

        for meeting in organized:
            try:
                dir_name = self._meeting_dir_name(meeting)
                expected_dirs.add(dir_name)
                rel_dir = f"meetings/{dir_name}"
                full_dir = meetings_root / dir_name
                full_dir.mkdir(parents=True, exist_ok=True)

                # Metadata + recap (always written).
                meta_entries = teams_metadata.build_meeting_entries(
                    meeting=meeting,
                    rel_dir=rel_dir,
                    recording_links=_recording_links(meeting),
                    loop_links=_loop_links(meeting),
                )

                # Transcript (if available + scope granted).
                transcript_entries = await teams_transcript.export_meeting_transcripts(
                    client=client,
                    token=token,
                    user_id=transcript_user_id,
                    meeting_id=meeting["id"],
                    meeting_subject=meeting.get("subject") or "(untitled)",
                    rel_dir=rel_dir,
                    deep_link=meeting.get("joinWebUrl") or meeting.get("joinUrl") or "",
                )
                if transcript_entries:
                    stats.transcripts_written += 1

                # Materialise everything, then stamp mtimes to startDateTime.
                start_epoch = _parse_iso_epoch(
                    meeting.get("startDateTime") or meeting.get("start") or ""
                )
                wrote_any = False
                for entry in meta_entries + transcript_entries:
                    if self._write_text_entry(
                        entry, root=meetings_root.parent, mtime=start_epoch
                    ):
                        wrote_any = True
                if wrote_any:
                    stats.meetings_written += 1
                else:
                    stats.meetings_skipped += 1
                # Also stamp the dir mtime — handy in file listings.
                if start_epoch is not None:
                    with suppress(OSError):
                        os.utime(full_dir, (start_epoch, start_epoch))
            except Exception as e:  # noqa: BLE001
                logger.exception("Failed to write meeting %s", meeting.get("id"))
                stats.errors.append(f"meeting {meeting.get('id')}: {e}")

    # ------------------------------------------------------------------
    # Enumeration helpers
    # ------------------------------------------------------------------

    async def _list_organized(
        self, client: httpx.AsyncClient, token: str, user_prefix: str
    ) -> list[dict[str, Any]]:
        """Walk ``/onlineMeetings`` for the user. Returns ALL history."""
        url: str | None = f"{user_prefix}/onlineMeetings?$top=50"
        out: list[dict[str, Any]] = []
        while url:
            resp = await msa.graph_get(client, url, token)
            if resp.status_code == 403:
                logger.warning(
                    "OnlineMeetings.Read scope missing for %s — skipping",
                    user_prefix,
                )
                return []
            if resp.status_code != 200:
                msa.raise_graph_error(resp, f"list onlineMeetings at {user_prefix}")
            data = resp.json()
            out.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
        return out

    async def _list_attended_meeting_ids(
        self, *, client: httpx.AsyncClient, token: str, user_id: str
    ) -> set[str]:
        """Walk ``/communications/callRecords`` and return distinct meeting IDs.

        Filtered to the requested user via the ``participants_v2``
        contains-filter. Returns an empty set on 403 (CallRecords scope
        not granted) — the caller degrades to organized-only.
        """
        if user_id == "__me__":
            # CallRecords filter requires a stable user id; resolve /me/id.
            try:
                user_id = await _resolve_me_id(client, token)
            except Exception as e:  # noqa: BLE001
                logger.warning("Could not resolve /me id for callRecords: %s", e)
                return set()

        url: str | None = (
            f"{GRAPH_BASE}/communications/callRecords"
            "?$select=id,type,modalities,startDateTime,endDateTime,joinWebUrl,"
            "organizer_v2,participants_v2"
            "&$top=50"
        )
        ids: set[str] = set()
        while url:
            resp = await msa.graph_get(client, url, token)
            if resp.status_code == 403:
                logger.warning(
                    "CallRecords.Read.All scope missing — attended meetings "
                    "won't be captured for %s", user_id,
                )
                return set()
            if resp.status_code != 200:
                logger.warning(
                    "callRecords list failed: %s — degrading to organized-only",
                    resp.status_code,
                )
                return ids
            data = resp.json()
            for rec in data.get("value", []):
                if not _user_in_call_record(rec, user_id):
                    continue
                # callRecords carry the meetingId on the record itself when
                # the call was for an online meeting; not all records have one
                # (1:1 calls don't).
                mid = (
                    rec.get("meetingId")
                    or rec.get("onlineMeeting", {}).get("id")
                )
                if mid:
                    ids.add(mid)
            url = data.get("@odata.nextLink")
        return ids

    async def _fetch_meeting(
        self,
        client: httpx.AsyncClient,
        token: str,
        user_prefix: str,
        meeting_id: str,
    ) -> dict[str, Any] | None:
        resp = await msa.graph_get(
            client, f"{user_prefix}/onlineMeetings/{meeting_id}", token
        )
        if resp.status_code != 200:
            logger.debug(
                "Could not fetch attended meeting %s: %s",
                meeting_id, resp.status_code,
            )
            return None
        return resp.json()

    # ------------------------------------------------------------------
    # Writers
    # ------------------------------------------------------------------

    @staticmethod
    def _write_text_entry(
        entry: RemoteEntry, *, root: Path, mtime: float | None
    ) -> bool:
        local = root / entry.rel_path
        if fingerprint_matches(local, entry.fingerprint):
            return False
        atomic_write_text(local, entry.payload or "", mtime=mtime)
        return True

    @staticmethod
    def _meeting_dir_name(meeting: dict[str, Any]) -> str:
        start = (meeting.get("startDateTime") or meeting.get("start") or "")[:10]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", start):
            start = "0000-00-00"
        subj = safe_filename(meeting.get("subject") or "meeting", "meeting")
        mid = (meeting.get("id") or "")[:8] or "noid"
        return f"{start}_{subj}_{mid}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _recording_links(meeting: dict[str, Any]) -> list[dict[str, str]]:
    """Extract recording deep links if Graph surfaced any.

    Teams meetings sometimes carry ``recordings`` or
    ``recordings@odata.context``; modern meetings stash the actual MP4
    in the organizer's OneDrive, but a deep link is usually present.
    """
    out: list[dict[str, str]] = []
    recs = meeting.get("recordings") or []
    for r in recs:
        url = r.get("recordingContentUrl") or r.get("url") or ""
        if url:
            out.append({"title": r.get("subject") or "recording", "url": url})
    return out


def _loop_links(meeting: dict[str, Any]) -> list[dict[str, str]]:
    """Extract Loop/.fluid attachment links from common meeting fields."""
    out: list[dict[str, str]] = []
    for key in ("attachments", "loopAttachments"):
        for a in meeting.get(key) or []:
            url = a.get("url") or a.get("webUrl") or ""
            if not url:
                continue
            lower = url.lower()
            if "loop" in lower or ".fluid" in lower or "/_layouts/15/doc.aspx" in lower:
                out.append({"title": a.get("title") or a.get("name") or "loop", "url": url})
    return out


def _parse_iso_epoch(value: str) -> float | None:
    if not value:
        return None
    try:
        # Microsoft returns ISO 8601 with 'Z' or offset; Python 3.11+ parses 'Z'.
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _user_in_call_record(record: dict[str, Any], user_id: str) -> bool:
    """Check whether ``user_id`` appears in a callRecord's participants."""
    parts = record.get("participants_v2") or record.get("participants") or []
    for p in parts:
        ident = p.get("user") or p.get("identity") or {}
        if isinstance(ident, dict) and (ident.get("id") == user_id):
            return True
    organizer = (record.get("organizer_v2") or {}).get("user") or {}
    if isinstance(organizer, dict) and organizer.get("id") == user_id:
        return True
    return False


async def _resolve_me_id(client: httpx.AsyncClient, token: str) -> str:
    resp = await msa.graph_get(client, f"{GRAPH_BASE}/me?$select=id", token)
    if resp.status_code != 200:
        msa.raise_graph_error(resp, "resolve /me id")
    return resp.json()["id"]


