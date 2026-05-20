"""Teams meeting transcript → markdown.

Graph's transcript endpoint (`/users/{id}/onlineMeetings/{mid}/transcripts/{tid}/content`)
returns a WebVTT (`.vtt`) blob. We convert it to markdown of the form

    ## 0:02:14 — Alice
    blah blah
    ## 0:02:36 — Bob
    yeah, exactly

so the chunker can split on speaker turns. Frontmatter carries the
meeting id and transcript id for traceability.

Requires the ``OnlineMeetingTranscript.Read.All`` scope; 403 → returns
``None`` and the caller logs + skips this meeting's transcript.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from ..microsoft_auth import graph_get
from .base import RemoteEntry, fingerprint_header

logger = logging.getLogger(__name__)


async def export_meeting_transcripts(
    *,
    client: httpx.AsyncClient,
    token: str,
    user_id: str,
    meeting_id: str,
    meeting_subject: str,
    rel_dir: str,
    deep_link: str,
) -> list[RemoteEntry]:
    """Return one ``transcript.md`` (concatenated across transcripts) or [].

    Most meetings have at most one transcript; the API still returns a
    list, so we render them all into one markdown file separated by a
    horizontal rule.
    """
    base = "https://graph.microsoft.com/v1.0"
    list_url = (
        f"{base}/users/{user_id}/onlineMeetings/{meeting_id}/transcripts"
    )
    resp = await graph_get(client, list_url, token)
    if resp.status_code == 403:
        logger.info(
            "Transcripts skipped for meeting %s (scope not granted)",
            meeting_id,
        )
        return []
    if resp.status_code == 404:
        return []
    if resp.status_code != 200:
        logger.warning(
            "Transcripts list failed for meeting %s: %s",
            meeting_id, resp.status_code,
        )
        return []

    transcripts = resp.json().get("value") or []
    if not transcripts:
        return []

    sections: list[str] = []
    fingerprint_parts: list[str] = []
    for t in transcripts:
        tid = t["id"]
        created = t.get("createdDateTime") or ""
        fingerprint_parts.append(f"{tid}:{created}")
        content_url = (
            f"{base}/users/{user_id}/onlineMeetings/{meeting_id}"
            f"/transcripts/{tid}/content"
        )
        c_resp = await graph_get(
            client, content_url, token,
            extra_headers={"Accept": "text/vtt"},
        )
        if c_resp.status_code != 200:
            logger.warning(
                "Transcript content fetch failed (mtg=%s tid=%s): %s",
                meeting_id, tid, c_resp.status_code,
            )
            continue
        vtt = c_resp.text or ""
        sections.append(_vtt_to_markdown(vtt))

    if not sections:
        return []

    fingerprint = ";".join(fingerprint_parts)
    body = "\n\n---\n\n".join(sections)
    payload = (
        fingerprint_header(fingerprint)
        + f"---\n"
        + f"meeting: {_yaml_escape(meeting_subject)}\n"
        + f"meeting_id: {meeting_id}\n"
        + f"source: teams_transcript\n"
        + f"url: {deep_link}\n"
        + f"---\n\n"
        + body.strip()
        + "\n"
    )
    return [
        RemoteEntry(
            rel_path=f"{rel_dir}/transcript.md",
            url=deep_link,
            fingerprint=fingerprint,
            payload=payload,
        )
    ]


# ---------------------------------------------------------------------------
# VTT parsing
# ---------------------------------------------------------------------------


_CUE_TIMING = re.compile(
    r"^(?P<start>\d{2}:\d{2}:\d{2}\.\d{3})\s+-->\s+"
    r"(?P<end>\d{2}:\d{2}:\d{2}\.\d{3})"
)
_VOICE_TAG = re.compile(r"<v\s+([^>]+)>(.*?)</v>", re.DOTALL)
_ANY_TAG = re.compile(r"<[^>]+>")


def _vtt_to_markdown(vtt: str) -> str:
    """Group consecutive cues by speaker and render as speaker-turn markdown."""
    lines = vtt.splitlines()
    cues: list[tuple[str, str, str]] = []  # (start, speaker, text)

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].strip()
        m = _CUE_TIMING.match(line)
        if not m:
            i += 1
            continue
        start = m.group("start")
        i += 1
        text_lines: list[str] = []
        while i < n and lines[i].strip():
            text_lines.append(lines[i])
            i += 1
        text = " ".join(t.strip() for t in text_lines)
        speaker, cleaned = _extract_speaker(text)
        cues.append((_short_timestamp(start), speaker, cleaned))

    if not cues:
        return "_(no cues — empty transcript)_"

    # Merge consecutive same-speaker cues.
    merged: list[tuple[str, str, list[str]]] = []
    for start, speaker, text in cues:
        if merged and merged[-1][1] == speaker:
            merged[-1][2].append(text)
        else:
            merged.append((start, speaker, [text]))

    out: list[str] = []
    for start, speaker, texts in merged:
        header = f"## {start} — {speaker}" if speaker else f"## {start}"
        out.append(header + "\n\n" + " ".join(texts).strip())
    return "\n\n".join(out)


def _extract_speaker(text: str) -> tuple[str, str]:
    """Return (speaker, cleaned_text) from a VTT cue.

    Teams encodes speaker as ``<v Speaker Name>cue text</v>``.
    """
    m = _VOICE_TAG.search(text)
    if m:
        return m.group(1).strip(), _ANY_TAG.sub("", m.group(2)).strip()
    return "", _ANY_TAG.sub("", text).strip()


def _short_timestamp(ts: str) -> str:
    """``00:02:14.520`` → ``0:02:14``."""
    head = ts.split(".", 1)[0]
    h, m, s = head.split(":")
    return f"{int(h)}:{m}:{s}"


def _yaml_escape(value: str) -> str:
    if value is None:
        return ""
    safe = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{safe}"' if any(c in safe for c in ':#&*!|>') else safe
