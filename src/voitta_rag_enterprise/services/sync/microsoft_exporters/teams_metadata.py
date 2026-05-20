"""Teams meeting metadata + recap → on-disk artefacts.

For every meeting we emit:

* ``metadata.json`` — the structured payload (subject, organizer,
  attendees, start/end, joinUrl, recording deep link if any). Stable
  shape across both organized and attended meetings so the indexer can
  treat them uniformly.
* ``recap.md`` — human-readable summary with a frontmatter block; the
  body lists attendees + a deep link to the meeting and any
  recording. Per the user's request we never download the MP4 itself
  — only the link.

If the meeting has Loop / fluid attachments (links to
``…/microsoft-loop/…`` or ``…/_layouts/15/Doc.aspx``), they're listed
as bullet points in the recap.
"""

from __future__ import annotations

import json
from typing import Any

from .base import RemoteEntry, fingerprint_header


def build_meeting_entries(
    *,
    meeting: dict[str, Any],
    rel_dir: str,
    recording_links: list[dict[str, str]] | None = None,
    loop_links: list[dict[str, str]] | None = None,
) -> list[RemoteEntry]:
    """Synthesize metadata.json + recap.md ``RemoteEntry`` objects.

    ``meeting`` is expected to carry at least: ``id``, ``subject``,
    ``startDateTime``, ``endDateTime``, ``joinWebUrl`` /
    ``joinUrl``, ``organizer`` (Graph identity), ``participants`` (list
    or ``attendees``).
    """
    subject = meeting.get("subject") or "(untitled meeting)"
    start = meeting.get("startDateTime") or meeting.get("start") or ""
    end = meeting.get("endDateTime") or meeting.get("end") or ""
    join_url = meeting.get("joinWebUrl") or meeting.get("joinUrl") or ""
    organizer = _identity_name(
        (meeting.get("organizer") or {}).get("identity")
        or meeting.get("organizer")
        or {}
    )
    attendees = _collect_attendees(meeting)

    fingerprint = f"{meeting.get('id', '')}:{start}:{end}:{len(attendees)}"

    payload_md = _render_recap_md(
        subject=subject,
        start=start,
        end=end,
        organizer=organizer,
        attendees=attendees,
        join_url=join_url,
        recording_links=recording_links or [],
        loop_links=loop_links or [],
    )

    payload_json = json.dumps(
        {
            "id": meeting.get("id"),
            "subject": subject,
            "start": start,
            "end": end,
            "joinUrl": join_url,
            "organizer": organizer,
            "attendees": attendees,
            "recordings": recording_links or [],
            "loop": loop_links or [],
        },
        indent=2,
        ensure_ascii=False,
    )

    return [
        RemoteEntry(
            rel_path=f"{rel_dir}/metadata.json",
            url=join_url,
            fingerprint=fingerprint,
            payload=payload_json,
        ),
        RemoteEntry(
            rel_path=f"{rel_dir}/recap.md",
            url=join_url,
            fingerprint=fingerprint,
            payload=payload_md,
        ),
    ]


def _render_recap_md(
    *,
    subject: str,
    start: str,
    end: str,
    organizer: str,
    attendees: list[str],
    join_url: str,
    recording_links: list[dict[str, str]],
    loop_links: list[dict[str, str]],
) -> str:
    lines = [
        fingerprint_header(f"{subject}:{start}:{end}:{len(attendees)}").rstrip(),
        "---",
        f"subject: {_yaml_escape(subject)}",
        f"start: {start}",
        f"end: {end}",
        f"organizer: {_yaml_escape(organizer)}",
        "source: teams_meeting",
        f"url: {join_url}",
        "---",
        "",
        f"# {subject}",
        "",
        f"- **Start:** {start}",
        f"- **End:** {end}",
        f"- **Organizer:** {organizer}",
    ]
    if attendees:
        lines.append("- **Attendees:**")
        for a in attendees:
            lines.append(f"  - {a}")
    if join_url:
        lines.append(f"- **Join:** [{join_url}]({join_url})")
    if recording_links:
        lines.append("")
        lines.append("## Recordings")
        for rec in recording_links:
            title = rec.get("title") or rec.get("name") or "recording"
            url = rec.get("url") or ""
            if url:
                lines.append(f"- [{title}]({url}) _(deep link only — file not downloaded)_")
    if loop_links:
        lines.append("")
        lines.append("## Loop / fluid attachments")
        for loop in loop_links:
            title = loop.get("title") or loop.get("name") or "loop component"
            url = loop.get("url") or ""
            if url:
                lines.append(f"- [{title}]({url})")
    return "\n".join(lines) + "\n"


def _collect_attendees(meeting: dict[str, Any]) -> list[str]:
    out: list[str] = []
    participants = meeting.get("participants") or {}
    if isinstance(participants, dict):
        for key in ("organizer", "attendees", "producers"):
            value = participants.get(key)
            if isinstance(value, list):
                for entry in value:
                    name = _identity_name(entry)
                    if name:
                        out.append(name)
            elif isinstance(value, dict):
                name = _identity_name(value)
                if name:
                    out.append(name)
    attendees = meeting.get("attendees") or []
    if isinstance(attendees, list):
        for a in attendees:
            name = _identity_name(a)
            if name:
                out.append(name)
    # Dedup while preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for n in out:
        if n not in seen:
            seen.add(n)
            deduped.append(n)
    return deduped


def _identity_name(entity: Any) -> str:
    if not isinstance(entity, dict):
        return ""
    # Various shapes Microsoft uses.
    identity = entity.get("identity") or entity
    user = identity.get("user") if isinstance(identity, dict) else None
    if isinstance(user, dict):
        return (
            user.get("displayName")
            or user.get("userPrincipalName")
            or user.get("id")
            or ""
        )
    if isinstance(identity, dict):
        return (
            identity.get("displayName")
            or identity.get("userPrincipalName")
            or ""
        )
    return ""


def _yaml_escape(value: str) -> str:
    if value is None:
        return ""
    safe = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{safe}"' if any(c in safe for c in ':#&*!|>') else safe
