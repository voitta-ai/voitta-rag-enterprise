"""Pluggable exporter for one Google-native Drive file → N local files.

The connector lists a Drive folder and gets a stream of file metadata. For
every native Google type (Docs, Sheets, Slides, Drawings, Forms) it asks the
registry which exporter handles that MIME type, then calls
:meth:`NativeDriveExporter.export` to produce a list of
:class:`RemoteEntry`. The connector treats every entry uniformly: write to
disk via the entry's producer, fingerprint for change detection, attach
``url`` and ``tab`` metadata to the sidecar.

A single exporter may emit multiple entries — one per Doc tab, one per
Sheet, one per slide, plus auxiliary outputs such as the full workbook
``.xlsx`` for Sheets. Each entry's path is the exporter's choice; the
connector only cares that they are all under ``rel_no_ext``'s parent
directory so cleanup heuristics (orphan detection during sync) work.

Concurrency
-----------
:meth:`export` runs on a worker thread inside a process-wide pool. Per-
thread service Resources are mediated by :class:`ExportContext`: each
exporter gets its own ``drive``/``docs``/``sheets``/``slides``/``forms``
service that is *guaranteed thread-local*. Producers (the deferred
download functions inside each :class:`RemoteEntry`) similarly receive
a per-thread ``drive`` plus a no-arg accessor for any other service
they need — see :class:`ProducerContext`.
"""

from __future__ import annotations

import logging
import random
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

logger = logging.getLogger(__name__)

_SAFE_NAME_RE = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")

# Workspace read quotas are per-minute-per-user (Docs is 300/min). The
# native-export pool fans out parallel ``.execute()`` calls and will burst
# past that ceiling on any folder with a lot of Docs/Sheets/Slides — every
# overflow request comes back 429 and, without a retry, lands as a
# permanently-empty export. Route every Workspace ``.execute()`` through
# ``execute_with_retry`` so 429 (and transient 5xx) self-heal: honour any
# ``Retry-After`` header, else exponential backoff + jitter. Because the
# quota resets on the minute boundary, the backoff is allowed to grow to
# the better part of a minute before giving up.
_RETRY_STATUSES = frozenset({403, 429, 500, 502, 503, 504})
_RETRYABLE_429_REASONS = ("rateLimitExceeded", "userRateLimitExceeded")


def execute_with_retry(
    request: Any,
    *,
    max_attempts: int = 6,
    base_delay: float = 2.0,
    max_delay: float = 64.0,
    label: str = "workspace",
) -> Any:
    """``request.execute()`` with backoff on rate-limit / transient errors.

    ``request`` is a googleapiclient ``HttpRequest`` (the thing you'd
    normally call ``.execute()`` on). Retries on 429 and 5xx; a 403 is
    only retried when its reason is a rate-limit (Drive reports user-rate
    overruns as 403 ``userRateLimitExceeded``, not 429), so genuine
    permission 403s still fail fast. Honours ``Retry-After`` when Google
    sends it, otherwise exponential backoff with full jitter.
    """
    from googleapiclient.errors import HttpError

    for attempt in range(1, max_attempts + 1):
        try:
            return request.execute()
        except HttpError as e:
            status = getattr(e.resp, "status", None)
            try:
                status = int(status)
            except (TypeError, ValueError):
                raise
            if status not in _RETRY_STATUSES or attempt == max_attempts:
                raise
            # A 403 is usually a hard permission error — only back off when
            # Google flags it as a rate-limit overrun.
            if status == 403 and not any(
                r in str(e) for r in _RETRYABLE_429_REASONS
            ):
                raise
            retry_after = e.resp.get("retry-after") if e.resp else None
            if retry_after is not None:
                try:
                    delay = float(retry_after)
                except (TypeError, ValueError):
                    delay = base_delay * (2 ** (attempt - 1))
            else:
                # Full jitter: random point in [0, capped exponential].
                ceiling = min(max_delay, base_delay * (2 ** (attempt - 1)))
                delay = random.uniform(0, ceiling)
            logger.warning(
                "%s API %d, retry %d/%d in %.1fs",
                label,
                status,
                attempt,
                max_attempts,
                delay,
            )
            time.sleep(delay)
    # Unreachable: the final attempt either returns or re-raises above.
    raise RuntimeError("execute_with_retry exhausted without returning")


def safe_filename(name: str, fallback: str = "tab") -> str:
    """Sanitize a tab / sheet / slide title for use as a path component.

    Strips reserved filesystem characters, collapses whitespace + repeated
    dashes, and caps length at 80 chars. Returns ``fallback`` if the
    sanitised string is empty (e.g. an all-emoji title) so callers can
    rely on a non-empty result.
    """
    out = _SAFE_NAME_RE.sub("-", name).strip().strip(".")
    out = re.sub(r"\s+", " ", out)
    out = re.sub(r"-{2,}", "-", out)
    # A name that was entirely reserved chars (e.g. ``///`` from a tab
    # title intentionally hidden by the user) collapses to a lone dash;
    # strip it so the fallback applies instead of producing a literal
    # ``-.md`` filename on disk.
    out = out.strip("-")
    return out[:80] or fallback


@dataclass(frozen=True)
class ExportContext:
    """Per-export state passed to :meth:`NativeDriveExporter.export`.

    Fields are factories so the exporter only pays the discovery cost for
    services it actually uses — most exporters need exactly one (Docs
    needs ``docs``, Sheets needs ``sheets``, etc).

    ``drive_thread_local`` lazily builds (and caches) one Drive Resource
    per worker thread; producers call it from their deferred download
    paths. ``access_token`` is the bearer token for raw HTTP calls
    bypassing googleapiclient (image content URIs, slide thumbnails).
    """

    folder_root: Path
    docs: Callable[[], Any]
    sheets: Callable[[], Any]
    slides: Callable[[], Any]
    forms: Callable[[], Any]
    drive_thread_local: Callable[[], Any]
    access_token: str | None


@dataclass
class RemoteEntry:
    """One on-disk file the connector should produce.

    The producer is a deferred callable so listing and download phases
    can be separated — the connector decides per-entry whether to skip
    it (unchanged fingerprint) or run the producer in the download
    thread pool.

    Producer signature is ``(dest, drive, ctx)``: ``drive`` is a
    per-thread Drive Resource (safe to use from concurrent producers);
    ``ctx`` carries the sibling service factories so producers can
    reach Sheets / Slides / Forms / etc. Producers MUST write
    atomically via ``services.sync.google_drive.atomic_*`` helpers.

    ``inline_payload``: producers that don't actually go to the network
    (e.g. tab markdown rendered at listing time) leave their payload
    here so an in-process write can run without hitting any service.
    Pure-text producers should still go through the atomic-write helper
    for consistency with binary downloads.
    """

    rel_path: str
    url: str
    fingerprint: str
    producer: Callable[[Path, Any, "ProducerContext"], None]
    tab: str | None = None
    size_hint: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProducerContext:
    """Per-thread state passed to a producer at download time.

    Built once per worker thread by the connector and reused for every
    producer that worker runs. Service factories cache results on the
    thread itself (via threading.local stored in the closure that
    builds them) so a thread that uses ``sheets()`` 50 times only
    pays the discovery cost once.
    """

    folder_root: Path
    docs: Callable[[], Any]
    sheets: Callable[[], Any]
    slides: Callable[[], Any]
    forms: Callable[[], Any]
    access_token: str | None


class NativeDriveExporter(ABC):
    """Export one Drive file (with the matching :attr:`mime_type`) into N
    local files.

    Each subclass declares its ``mime_type`` and implements ``export``.
    The registry dispatches ``files.list`` items by ``mimeType`` to the
    matching exporter; unmatched native types are silently dropped (same
    behaviour as before — Drive surfaces niche types like Sites and
    Apps Script we have no useful textual export for).
    """

    mime_type: ClassVar[str] = ""

    @abstractmethod
    def export(
        self,
        item: dict[str, Any],
        rel_no_ext: str,
        ctx: ExportContext,
    ) -> list[RemoteEntry]:
        """Render ``item`` into a list of on-disk entries.

        ``item`` carries the Drive ``files.list`` payload (id, name,
        mimeType, modifiedTime, webViewLink, etc.). ``rel_no_ext`` is
        the local path stem the connector assigned — the file's name
        relative to the folder root, *without* an extension. Exporters
        choose suffixes / nested paths from there.

        Returning an empty list is allowed and means "this Drive file
        has nothing to materialise" (e.g. an empty form). The connector
        treats it as a successful no-op; the file simply won't appear
        on disk.

        Exceptions raised here propagate to the connector, which
        records them in ``stats.errors`` and continues with the next
        file — partial folder enumeration is preferred to an aborted
        sync.
        """
