"""Confluence sync connector.

Mirrors selected Confluence spaces onto disk as one markdown file per page under
``spaces/<SPACE-KEY>/<id>-<title>.md`` — each space gets its own subtree,
mirroring the Jira connector's ``projects/<KEY>/…`` and SharePoint's
``sites/<site>/…`` layouts so the folder structure is consistent across
connectors. Each page's structured attributes are recorded in the
``.voitta_sources.json`` sidecar so the indexer can promote them to filterable
``meta_*`` / ``attr_*`` Qdrant payload fields.

Auth + URL shapes are delegated to :mod:`services.sync.atlassian_auth` (Cloud =
Basic ``email:api_token``, base ``/wiki/rest/api``; Server/DC = Bearer PAT, base
``/rest/api``).

Provenance & attributes captured per page:

* ``meta_owner_*``   ← page creator (``history.createdBy``)
* ``meta_editor_*``  ← last editor (``version.by``)
* ``meta_created_ts`` / ``meta_modified_ts`` ← ``history.createdDate`` / ``version.when``
* ``attrs`` (curated, indexed) ← space, space_name, labels, ancestors (titles),
  version, content_type
* ``attrs_raw`` (full bag, retrievable not indexed) ← misc page metadata

Body is rendered from ``body.export_view`` (Confluence renders storage-format
macros to clean HTML server-side) → markdown via markdownify — NOT raw
``body.storage`` (XHTML full of ``ac:``/``ri:`` macro tags markdownify mangles).

Incremental: a ``.voitta_confluence_revisions.json`` sidecar maps rel_path → the
page's version number; unchanged pages are skipped and pages no longer in the
selection are mirror-deleted.
"""

from __future__ import annotations

import html as _html
import json
import logging
import re
import shutil
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .. import source_meta as sm
from .atlassian_auth import AtlassianAuth, normalize_base_url
from .base import SyncConnector

logger = logging.getLogger(__name__)


# Lower bound on page recency: only pages modified on/after this date are synced
# (CQL ``lastmodified >= …``). Bounds an otherwise unbounded full-history pull;
# overridable per folder via the Extra CQL field. Mirrors the Jira connector.
PAGES_UPDATED_SINCE = "2026-01-01"

# Page-listing page size and the picker's render/"type to narrow" threshold.
SEARCH_LIMIT = 100


# ---------------------------------------------------------------------------
# Selected-spaces field (de)serialisation — mirrors jira selected-projects.
# ---------------------------------------------------------------------------


def coerce_spaces_field(raw: str | None) -> list[dict[str, str]]:
    """Decode the JSON array stored in ``cf_selected_spaces``.

    Expected: ``[{"key": "ENG", "name": "Engineering"}]``. Tolerates missing
    ``name``; ignores entries without a ``key``.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    out: list[dict[str, str]] = []
    for entry in parsed:
        if isinstance(entry, dict) and entry.get("key"):
            out.append(
                {
                    "key": str(entry["key"]),
                    "name": str(entry.get("name") or entry["key"]),
                }
            )
    return out


def encode_spaces_field(spaces: list[dict[str, str]] | None) -> str | None:
    if not spaces:
        return None
    cleaned = [
        {"key": str(s["key"]), "name": str(s.get("name") or s["key"])}
        for s in spaces
        if isinstance(s, dict) and s.get("key")
    ]
    return json.dumps(cleaned) if cleaned else None


# ---------------------------------------------------------------------------
# Rendering + value helpers
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_ILLEGAL_FS = re.compile(r'[<>:"/\\|?*]')


def safe_component(name: str, fallback: str = "item") -> str:
    """Sanitise a string for use as one path component."""
    cleaned = _ILLEGAL_FS.sub("-", name)
    cleaned = re.sub(r"\s+", "-", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    return cleaned[:100] or fallback


def _html_to_markdown(html: str) -> str:
    """Convert rendered Confluence HTML to markdown (markdownify, else strip)."""
    if not html:
        return ""
    try:
        from markdownify import markdownify as md_convert  # type: ignore
    except ImportError:
        return _html.unescape(_TAG_RE.sub("", html))
    try:
        return md_convert(html, heading_style="ATX").strip()
    except Exception:  # noqa: BLE001
        return _html.unescape(_TAG_RE.sub("", html))


def _api_base(auth: AtlassianAuth) -> str:
    """REST base for this site — Cloud nests under ``/wiki``."""
    return f"{auth.base_url}/wiki/rest/api" if auth.is_cloud else f"{auth.base_url}/rest/api"


def _person(obj: Any) -> dict[str, str]:
    """Extract ``{name, email}`` from a Confluence user object (or {})."""
    if not isinstance(obj, dict):
        return {"name": "", "email": ""}
    return {
        "name": obj.get("displayName") or obj.get("publicName") or "",
        "email": obj.get("email") or obj.get("emailAddress") or "",
    }


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@dataclass
class ConfluenceSyncStats:
    files_added: int = 0
    files_updated: int = 0
    files_removed: int = 0
    files_skipped: int = 0
    spaces_synced: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "files_added": self.files_added,
            "files_updated": self.files_updated,
            "files_removed": self.files_removed,
            "files_skipped": self.files_skipped,
            "spaces_synced": self.spaces_synced,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Module-level space listing — called from the "pick spaces" route.
# ---------------------------------------------------------------------------


async def list_spaces(auth: AtlassianAuth) -> list[dict[str, str]]:
    """Return ``[{key, name}]`` for every **global** space the creds can see.

    Personal (``~user``) and archived spaces are excluded so the picker isn't
    drowned on a large org. Paginates ``GET /space?type=global``.
    """
    if not auth.configured:
        raise RuntimeError("Confluence not configured — set base URL, token (and email for Cloud).")
    headers = auth.headers()
    base = _api_base(auth)
    spaces: list[dict[str, str]] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        start = 0
        while True:
            resp = await client.get(
                f"{base}/space",
                params={"type": "global", "status": "current",
                        "start": start, "limit": 100},
                headers=headers,
            )
            _raise_for_status(resp, "list spaces")
            data = resp.json()
            results = data.get("results", [])
            for s in results:
                spaces.append({"key": s["key"], "name": s.get("name", s["key"])})
            if len(results) < 100 or not results:
                break
            start += len(results)
    spaces.sort(key=lambda s: s["key"])
    return spaces


async def search_spaces(
    auth: AtlassianAuth, query: str = "", limit: int = SEARCH_LIMIT
) -> list[dict[str, str]]:
    """Return up to ``limit`` global spaces matching ``query`` — for the picker.

    Confluence ``/space`` has no server-side text search, so we fetch the global
    list once and filter locally. Multi-value mode (2+ comma/newline-separated
    tokens) matches space **key** exactly, case-insensitively; a single token is
    a substring match on key or name.
    """
    tokens = _parse_query_tokens(query)
    all_spaces = await list_spaces(auth)
    if len(tokens) >= 2:
        want = {t.upper() for t in tokens}
        return [s for s in all_spaces if s["key"].upper() in want]
    if tokens:
        q = tokens[0].lower()
        all_spaces = [
            s for s in all_spaces
            if q in s["key"].lower() or q in s["name"].lower()
        ]
    return all_spaces[:limit]


def _parse_query_tokens(query: str) -> list[str]:
    """Split a picker query on commas/newlines into trimmed, non-empty tokens."""
    return [t.strip() for t in re.split(r"[\n,]+", query or "") if t.strip()]


def _raise_for_status(resp: httpx.Response, action: str) -> None:
    if resp.status_code == 401:
        raise RuntimeError(
            "Confluence authentication failed. Check your token "
            "(and email for Cloud)."
        )
    if resp.status_code == 403:
        raise RuntimeError(f"Confluence access denied while trying to {action}.")
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Confluence request to {action} failed ({resp.status_code}): "
            f"{resp.text[:300]}"
        )


# ---------------------------------------------------------------------------
# Internal page record
# ---------------------------------------------------------------------------


@dataclass
class _PageRef:
    page_id: str
    rel_path: str
    title: str
    version: str  # version number as string (revision key)


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class ConfluenceConnector(SyncConnector):
    """Sync selected Confluence spaces into ``folder_root`` as markdown."""

    source_type = "confluence"
    supports_progress = True

    def resolve_config(self, row) -> dict[str, Any]:
        return {
            "auth": AtlassianAuth(
                base_url=normalize_base_url(row.cf_base_url or ""),
                method=row.cf_auth_method or "cloud",
                email=row.cf_email or "",
                token=row.cf_token or "",
            ),
            "spaces": coerce_spaces_field(row.cf_selected_spaces),
            "all_spaces": bool(row.cf_all_spaces),
            "cql_extra": (row.cf_cql or "").strip(),
        }

    async def sync(
        self,
        *,
        folder_root: Path,
        auth: AtlassianAuth,
        spaces: list[dict[str, str]],
        all_spaces: bool,
        cql_extra: str = "",
        progress_cb: Callable[[str, int, int, dict[str, Any] | None], None]
        | None = None,
    ) -> ConfluenceSyncStats:
        if not auth.configured:
            raise RuntimeError(
                "Confluence not connected. Open the folder's sync settings and "
                "enter the base URL and API token (plus your email for Cloud)."
            )

        folder_root = folder_root.expanduser().resolve()
        folder_root.mkdir(parents=True, exist_ok=True)

        def emit(phase: str, done: int, total: int, **detail: Any) -> None:
            if progress_cb is not None:
                progress_cb(phase, done, total, detail or None)

        emit("connecting", 0, 0)

        # Resolve the space selection into a list of keys.
        if all_spaces:
            space_keys = [s["key"] for s in await list_spaces(auth)]
        else:
            space_keys = [s["key"] for s in (spaces or [])]
        if not space_keys:
            raise RuntimeError(
                "No Confluence spaces selected. Pick at least one space, or "
                "enable 'all spaces'."
            )

        stats = ConfluenceSyncStats()

        # Revision sidecar (rel_path -> version number) for incremental.
        rev_file = folder_root / ".voitta_confluence_revisions.json"
        old_revs: dict[str, str] = {}
        if rev_file.exists():
            try:
                old_revs = json.loads(rev_file.read_text())
            except (OSError, ValueError):
                old_revs = {}

        # Prior source records — reused verbatim for skipped (unchanged) pages so
        # a re-index never loses the attributes we captured last time.
        old_sources: dict[str, dict[str, Any]] = {}
        src_file = folder_root / ".voitta_sources.json"
        if src_file.exists():
            try:
                loaded = json.loads(src_file.read_text())
                if isinstance(loaded, dict):
                    old_sources = loaded
            except (OSError, ValueError):
                old_sources = {}

        sidecar_sources: dict[str, dict[str, Any]] = {}
        sidecar_times: dict[str, dict[str, str]] = {}
        new_revs: dict[str, str] = {}
        expected_paths: set[str] = set()

        # Stream each space PAGE BY PAGE: list a page of refs, then immediately
        # download+write them before the next page. Files land continuously (the
        # watcher indexes them live), so progress is visible within seconds even
        # for very large spaces. Mirrors the Jira connector.
        total_spaces = len(space_keys)
        async with httpx.AsyncClient(timeout=60.0) as client:
            for sidx, skey in enumerate(space_keys):
                emit("listing", sidx, total_spaces,
                     current_space=skey, spaces_total=total_spaces)
                seen = 0
                try:
                    async for page in self._iter_page_refs(client, auth, skey, cql_extra):
                        for ref in page:
                            expected_paths.add(ref.rel_path)
                            new_revs[ref.rel_path] = ref.version
                            local = folder_root / ref.rel_path
                            seen += 1
                            if local.exists() and old_revs.get(ref.rel_path) == ref.version:
                                stats.files_skipped += 1
                                self._record_sidecar_from_ref(
                                    ref, skey, old_sources, sidecar_sources
                                )
                                continue
                            try:
                                page_obj = await self._fetch_page(client, auth, ref.page_id)
                                md = self._render_page(page_obj, auth)
                                existed = local.exists()
                                local.parent.mkdir(parents=True, exist_ok=True)
                                local.write_text(md, encoding="utf-8")
                                if existed:
                                    stats.files_updated += 1
                                else:
                                    stats.files_added += 1
                                self._record_sidecar_from_page(
                                    page_obj, ref, auth, sidecar_sources, sidecar_times
                                )
                            except Exception as e:  # noqa: BLE001
                                logger.warning("Confluence: failed to sync %s: %s", ref.page_id, e)
                                stats.errors.append(f"page {ref.page_id}: {e}")
                        emit("downloading", sidx, total_spaces,
                             current_space=skey, spaces_total=total_spaces, issue_done=seen)
                except Exception as e:  # noqa: BLE001
                    logger.warning("Confluence: failed to list space %s: %s", skey, e)
                    stats.errors.append(f"space {skey}: {e}")
                    continue
                logger.info("Confluence: space %s — %d pages", skey, seen)
                stats.spaces_synced += 1

        # Mirror-delete pages no longer in the selection.
        spaces_root = folder_root / "spaces"
        if spaces_root.exists():
            for path in list(spaces_root.rglob("*")):
                if not path.is_file() or path.name.startswith("."):
                    continue
                rel = path.relative_to(folder_root).as_posix()
                if rel not in expected_paths:
                    try:
                        path.unlink()
                        stats.files_removed += 1
                    except OSError as e:
                        stats.errors.append(f"unlink {rel}: {e}")
            for d in sorted(spaces_root.rglob("*"), reverse=True):
                if d.is_dir():
                    with suppress(OSError):
                        d.rmdir()

        # Sidecars + revision file.
        (folder_root / ".voitta_sources.json").write_text(
            json.dumps(sidecar_sources, indent=2, sort_keys=True)
        )
        if sidecar_times:
            (folder_root / ".voitta_timestamps.json").write_text(
                json.dumps(sidecar_times, indent=2, sort_keys=True)
            )
        rev_file.write_text(json.dumps(new_revs))

        emit("done", stats.spaces_synced, stats.spaces_synced)
        return stats

    # ------------------------------------------------------------------
    # Listing (CQL, streamed page by page)
    # ------------------------------------------------------------------

    def _page_cql(self, space_key: str, cql_extra: str) -> str:
        clauses = [
            f'space = "{space_key}"',
            "type = page",
            f'lastmodified >= "{PAGES_UPDATED_SINCE}"',
        ]
        if cql_extra:
            clauses.append(f"({cql_extra})")
        return " AND ".join(clauses) + " ORDER BY lastmodified DESC"

    async def _iter_page_refs(
        self,
        client: httpx.AsyncClient,
        auth: AtlassianAuth,
        space_key: str,
        cql_extra: str,
    ) -> AsyncIterator[list[_PageRef]]:
        """Yield page refs one API page (~50) at a time for one space.

        Uses ``content/search?cql=`` (CQL) so the recency floor applies. We
        follow the response's ``_links.next`` cursor, which both Cloud (cursor-
        based) and Server (start-based) return — so a single loop handles the
        Cloud/Server pagination fork transparently.
        """
        base = _api_base(auth)
        params: dict[str, Any] = {
            "cql": self._page_cql(space_key, cql_extra),
            "limit": 50,
            "expand": "version",
        }
        url: str | None = f"{base}/content/search"
        first = True
        while url:
            resp = await client.get(
                url, params=params if first else None, headers=auth.headers()
            )
            _raise_for_status(resp, f"search pages in {space_key}")
            data = resp.json()
            yield [self._ref_from_result(r, space_key) for r in data.get("results", [])]
            first = False
            nxt = (data.get("_links") or {}).get("next")
            if nxt:
                # ``next`` is a path relative to the site origin (it already
                # includes the cursor/start query), so resolve against base_url.
                url = f"{auth.base_url}{nxt}"
            else:
                url = None

    @staticmethod
    def _ref_from_result(result: dict, space_key: str) -> _PageRef:
        page_id = str(result["id"])
        title = result.get("title") or f"page-{page_id}"
        version = str(((result.get("version") or {}).get("number")) or "")
        return _PageRef(
            page_id=page_id,
            rel_path=f"spaces/{safe_component(space_key, 'SPACE')}/"
            f"{page_id}-{safe_component(title, page_id)}.md",
            title=title,
            version=version,
        )

    # ------------------------------------------------------------------
    # Fetch + render one page
    # ------------------------------------------------------------------

    async def _fetch_page(
        self, client: httpx.AsyncClient, auth: AtlassianAuth, page_id: str
    ) -> dict:
        base = _api_base(auth)
        resp = await client.get(
            f"{base}/content/{page_id}",
            params={
                "expand": (
                    "body.export_view,history,version,ancestors,"
                    "metadata.labels,space,children.attachment"
                )
            },
            headers=auth.headers(),
        )
        _raise_for_status(resp, f"fetch page {page_id}")
        return resp.json()

    def _render_page(self, page: dict, auth: AtlassianAuth) -> str:
        attrs, _raw = self._extract_attrs(page)
        title = page.get("title", "")
        web = self._web_url(page, auth)

        lines: list[str] = [f"# {title}\n"]
        lines.append("| Field | Value |")
        lines.append("|---|---|")
        if web:
            lines.append(f"| Page | [{title}]({web}) |")
        rows = [
            ("Space", attrs.get("space_name") or attrs.get("space")),
            ("Version", attrs.get("version")),
            ("Ancestors", " › ".join(attrs.get("ancestors", []))),
            ("Labels", ", ".join(attrs.get("labels", []))),
        ]
        hist = page.get("history") or {}
        created = hist.get("createdDate", "")
        if created:
            rows.append(("Created", created[:10]))
        ver = page.get("version") or {}
        if ver.get("when"):
            rows.append(("Updated", ver["when"][:10]))
        for label, val in rows:
            if val:
                lines.append(f"| {label} | {val} |")
        lines.append("")

        body = _html_to_markdown(
            ((page.get("body") or {}).get("export_view") or {}).get("value") or ""
        )
        if body:
            lines.append(body)
            lines.append("")

        # Attachments as links (link-only — contents are not downloaded).
        attachments = (
            ((page.get("children") or {}).get("attachment") or {}).get("results") or []
        )
        if attachments:
            lines.append("## Attachments\n")
            for att in attachments:
                name = att.get("title", "attachment")
                dl = ((att.get("_links") or {}).get("download")) or ""
                href = f"{auth.base_url}{dl}" if dl else ""
                lines.append(f"- [{name}]({href})" if href else f"- {name}")
            lines.append("")

        return "\n".join(lines)

    @staticmethod
    def _web_url(page: dict, auth: AtlassianAuth) -> str:
        links = page.get("_links") or {}
        webui = links.get("webui") or ""
        if not webui:
            return ""
        # Cloud's webui path is relative to ``/wiki``; Server's to the root.
        prefix = "/wiki" if auth.is_cloud else ""
        return f"{auth.base_url}{prefix}{webui}"

    # ------------------------------------------------------------------
    # Attribute extraction
    # ------------------------------------------------------------------

    def _extract_attrs(self, page: dict) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return ``(curated_attrs, raw_bag)`` for one page."""
        space = page.get("space") or {}
        labels = [
            lbl.get("name", "")
            for lbl in (((page.get("metadata") or {}).get("labels") or {}).get("results") or [])
        ]
        ancestors = [a.get("title", "") for a in (page.get("ancestors") or [])]
        version = (page.get("version") or {}).get("number")
        attrs: dict[str, Any] = {
            "space": space.get("key", ""),
            "space_name": space.get("name", ""),
            "labels": labels,
            "ancestors": ancestors,
            "content_type": page.get("type", "page"),
        }
        if isinstance(version, int):
            attrs["version"] = version
        raw: dict[str, Any] = {"id": str(page.get("id", ""))}
        status = page.get("status")
        if status:
            raw["status"] = status
        return attrs, raw

    # ------------------------------------------------------------------
    # Sidecar records
    # ------------------------------------------------------------------

    def _record_sidecar_from_page(
        self,
        page: dict,
        ref: _PageRef,
        auth: AtlassianAuth,
        sidecar_sources: dict[str, dict[str, Any]],
        sidecar_times: dict[str, dict[str, str]],
    ) -> None:
        hist = page.get("history") or {}
        ver = page.get("version") or {}
        creator = _person(hist.get("createdBy"))
        editor = _person(ver.get("by"))
        created = hist.get("createdDate")
        modified = ver.get("when")
        attrs, raw = self._extract_attrs(page)
        record: dict[str, Any] = {
            "source": "confluence",
            "url": self._web_url(page, auth),
            "page_id": ref.page_id,
            "space": attrs.get("space", ""),
        }
        record.update(
            sm.build(
                owner_name=creator.get("name"),
                owner_email=creator.get("email"),
                editor_name=editor.get("name"),
                editor_email=editor.get("email"),
                created=created,
                modified=modified,
                attrs=attrs,
                attrs_raw=raw,
            )
        )
        sidecar_sources[ref.rel_path] = record
        times = {
            k: v for k, v in (("modified_at", modified), ("created_at", created)) if v
        }
        if times:
            sidecar_times[ref.rel_path] = times

    def _record_sidecar_from_ref(
        self,
        ref: _PageRef,
        space_key: str,
        old_sources: dict[str, dict[str, Any]],
        sidecar_sources: dict[str, dict[str, Any]],
    ) -> None:
        """Carry a skipped (unchanged) page's prior record forward verbatim so a
        re-index keeps its captured attributes. Falls back to a minimal stub."""
        prior = old_sources.get(ref.rel_path)
        if isinstance(prior, dict) and prior:
            sidecar_sources[ref.rel_path] = prior
        else:
            sidecar_sources[ref.rel_path] = {
                "source": "confluence",
                "page_id": ref.page_id,
                "space": space_key,
            }
