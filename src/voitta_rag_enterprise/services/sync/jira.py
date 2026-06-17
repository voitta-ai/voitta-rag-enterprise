"""Jira sync connector.

Mirrors selected Jira projects onto disk as one markdown file per issue under
``issues/<type>/<KEY>-<summary>.md``, then records each issue's structured
attributes in the ``.voitta_sources.json`` sidecar so the indexer can promote
them to filterable ``meta_*`` / ``attr_*`` Qdrant payload fields.

Auth + URL shapes are delegated to :mod:`services.sync.atlassian_auth` (Cloud =
Basic ``email:api_token``, REST v3; Server/DC = Bearer PAT, REST v2). This
module is shape-agnostic about which one is in play beyond the two API-version
strings and the cloud-only ``/search/jql`` pagination cursor.

Provenance & attributes captured per issue:

* ``meta_owner_*``   ← reporter (the issue's creator)
* ``meta_editor_*``  ← last updater (from the most recent changelog author,
  else the reporter)
* ``meta_created_ts`` / ``meta_modified_ts`` ← ``created`` / ``updated``
* ``attrs`` (curated, indexed) ← status, priority, assignee, reporter,
  issuetype, project, resolution, parent/epic, sprint, labels, components,
  fix/affects versions, votes, watches, story points
* ``attrs_raw`` (full bag, retrievable not indexed) ← every other scalar
  field including all ``customfield_*`` values

Incremental: a ``.jira_revisions.json`` sidecar maps rel_path → the issue's
``updated`` timestamp; unchanged issues are skipped and issues no longer in the
selection are mirror-deleted.
"""

from __future__ import annotations

import html as _html
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .. import source_meta as sm
from .atlassian_auth import AtlassianAuth, normalize_base_url
from .base import SyncConnector

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Selected-projects field (de)serialisation — mirrors sharepoint sites field.
# ---------------------------------------------------------------------------


def coerce_projects_field(raw: str | None) -> list[dict[str, str]]:
    """Decode the JSON array stored in ``jira_selected_projects``.

    Expected: ``[{"key": "PROJ", "name": "Project"}]``. Tolerates missing
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


def encode_projects_field(projects: list[dict[str, str]] | None) -> str | None:
    if not projects:
        return None
    cleaned = [
        {"key": str(p["key"]), "name": str(p.get("name") or p["key"])}
        for p in projects
        if isinstance(p, dict) and p.get("key")
    ]
    return json.dumps(cleaned) if cleaned else None


# ---------------------------------------------------------------------------
# Rendering + value helpers
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_ILLEGAL_FS = re.compile(r'[<>:"/\\|?*]')
_EPIC_CUSTOM_FIELDS = ("customfield_10014", "customfield_10008")
# Bulky / already-rendered fields we never put in the raw attribute bag (their
# content is in the markdown body, or they're noise for filtering).
_RAW_DENYLIST = frozenset(
    {
        "description", "comment", "worklog", "attachment", "issuelinks",
        "subtasks", "summary", "comments", "renderedFields", "thumbnail",
        "watches", "votes",  # captured as curated ints already
    }
)


def safe_component(name: str, fallback: str = "item") -> str:
    """Sanitise a string for use as one path component."""
    cleaned = _ILLEGAL_FS.sub("-", name)
    cleaned = re.sub(r"\s+", "-", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    return cleaned[:100] or fallback


def _html_to_markdown(html: str) -> str:
    """Convert Jira rendered-field HTML to markdown (markdownify, else strip)."""
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


def _flatten_value(value: Any) -> str:
    """Flatten an arbitrary Jira field value to a short display string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        parts = [_flatten_value(v) for v in value]
        return ", ".join(p for p in parts if p)
    if isinstance(value, dict):
        for k in ("displayName", "name", "value", "key"):
            if value.get(k):
                return str(value[k])
        return ""
    return str(value)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@dataclass
class JiraSyncStats:
    files_added: int = 0
    files_updated: int = 0
    files_removed: int = 0
    files_skipped: int = 0
    projects_synced: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "files_added": self.files_added,
            "files_updated": self.files_updated,
            "files_removed": self.files_removed,
            "files_skipped": self.files_skipped,
            "projects_synced": self.projects_synced,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Module-level project listing — called from the "pick projects" route.
# ---------------------------------------------------------------------------


# Server-side project-search page size, also the picker's "type to narrow"
# threshold. Kept in step with the frontend's PICKER_SEARCH_LIMIT.
SEARCH_LIMIT = 100


async def list_projects(auth: AtlassianAuth) -> list[dict[str, str]]:
    """Return ``[{key, name}]`` for **every** project the credentials can see.

    Used by ``sync`` to resolve ``all_projects`` into concrete keys, so it must
    be exhaustive — it paginates the whole tenant. The interactive picker uses
    :func:`search_projects` instead, which is capped and far cheaper.
    """
    if not auth.configured:
        raise RuntimeError("Jira not configured — set base URL, token (and email for Cloud).")
    headers = auth.headers()
    projects: list[dict[str, str]] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        if auth.is_cloud:
            start_at = 0
            while True:
                resp = await client.get(
                    f"{auth.base_url}/rest/api/3/project/search",
                    params={"startAt": start_at, "maxResults": 100},
                    headers=headers,
                )
                _raise_for_status(resp, "list projects")
                data = resp.json()
                values = data.get("values", [])
                for p in values:
                    projects.append({"key": p["key"], "name": p.get("name", p["key"])})
                if data.get("isLast", True) or not values:
                    break
                start_at += len(values)
        else:
            resp = await client.get(
                f"{auth.base_url}/rest/api/2/project", headers=headers
            )
            _raise_for_status(resp, "list projects")
            for p in resp.json():
                projects.append({"key": p["key"], "name": p.get("name", p["key"])})
    projects.sort(key=lambda p: p["key"])
    return projects


async def search_projects(
    auth: AtlassianAuth, query: str = "", limit: int = SEARCH_LIMIT
) -> list[dict[str, str]]:
    """Return up to ``limit`` projects matching ``query`` — for the picker.

    Cloud searches server-side (``project/search?query=``) so enterprise tenants
    with thousands of projects are never downloaded in full; an empty query
    returns the first page. Server/DC has no project-search endpoint, so we fetch
    the (typically small) full list once and filter by substring locally.
    """
    if not auth.configured:
        raise RuntimeError("Jira not configured — set base URL, token (and email for Cloud).")
    headers = auth.headers()
    q = (query or "").strip()
    async with httpx.AsyncClient(timeout=30.0) as client:
        if auth.is_cloud:
            params: dict[str, Any] = {"startAt": 0, "maxResults": limit, "orderBy": "key"}
            if q:
                params["query"] = q
            resp = await client.get(
                f"{auth.base_url}/rest/api/3/project/search",
                params=params, headers=headers,
            )
            _raise_for_status(resp, "search projects")
            values = resp.json().get("values", [])
            out = [{"key": p["key"], "name": p.get("name", p["key"])} for p in values]
            out.sort(key=lambda p: p["key"])
            return out
        # Server/DC: fetch all once, filter locally.
        all_projects = await list_projects(auth)
        if q:
            ql = q.lower()
            all_projects = [
                p for p in all_projects
                if ql in p["key"].lower() or ql in p["name"].lower()
            ]
        return all_projects[:limit]


def _raise_for_status(resp: httpx.Response, action: str) -> None:
    if resp.status_code == 401:
        raise RuntimeError(
            "Jira authentication failed. Check your token "
            "(and email for Cloud)."
        )
    if resp.status_code == 403:
        raise RuntimeError(f"Jira access denied while trying to {action}.")
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Jira request to {action} failed ({resp.status_code}): "
            f"{resp.text[:300]}"
        )


# ---------------------------------------------------------------------------
# Internal issue record
# ---------------------------------------------------------------------------


@dataclass
class _IssueRef:
    key: str
    rel_path: str
    issue_type: str
    summary: str
    updated: str
    created: str


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class JiraConnector(SyncConnector):
    """Sync selected Jira projects into ``folder_root`` as markdown."""

    source_type = "jira"
    supports_progress = True

    def resolve_config(self, row) -> dict[str, Any]:
        return {
            "auth": AtlassianAuth(
                base_url=normalize_base_url(row.jira_base_url or ""),
                method=row.jira_auth_method or "cloud",
                email=row.jira_email or "",
                token=row.jira_token or "",
            ),
            "projects": coerce_projects_field(row.jira_selected_projects),
            "all_projects": bool(row.jira_all_projects),
            "jql_extra": (row.jira_jql or "").strip(),
        }

    async def sync(
        self,
        *,
        folder_root: Path,
        auth: AtlassianAuth,
        projects: list[dict[str, str]],
        all_projects: bool,
        jql_extra: str = "",
        progress_cb: Callable[[str, int, int, dict[str, Any] | None], None]
        | None = None,
    ) -> JiraSyncStats:
        if not auth.configured:
            raise RuntimeError(
                "Jira not connected. Open the folder's sync settings and enter "
                "the base URL and API token (plus your email for Cloud)."
            )

        folder_root = folder_root.expanduser().resolve()
        folder_root.mkdir(parents=True, exist_ok=True)

        def emit(phase: str, done: int, total: int, **detail: Any) -> None:
            if progress_cb is not None:
                progress_cb(phase, done, total, detail or None)

        emit("connecting", 0, 0)

        # Resolve the project selection into a list of keys.
        if all_projects:
            project_keys = [p["key"] for p in await list_projects(auth)]
        else:
            project_keys = [p["key"] for p in (projects or [])]
        if not project_keys:
            raise RuntimeError(
                "No Jira projects selected. Pick at least one project, or "
                "enable 'all projects'."
            )

        stats = JiraSyncStats()
        field_map = await self._discover_field_map(auth)

        # Revision sidecar (rel_path -> issue 'updated' string) for incremental.
        rev_file = folder_root / ".jira_revisions.json"
        old_revs: dict[str, str] = {}
        if rev_file.exists():
            try:
                old_revs = json.loads(rev_file.read_text())
            except (OSError, ValueError):
                old_revs = {}

        # Prior source records — reused verbatim for skipped (unchanged) issues
        # so a re-index never loses the attributes we captured last time.
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

        async with httpx.AsyncClient(timeout=60.0) as client:
            # Phase 1 — list issue refs across all selected projects.
            emit("listing", 0, len(project_keys))
            refs = await self._list_issue_refs(
                client, auth, project_keys, jql_extra
            )
            total = len(refs)
            logger.info("Jira: %d issues across %d project(s)", total, len(project_keys))

            # Phase 2 — fetch + render changed issues.
            for idx, ref in enumerate(refs):
                expected_paths.add(ref.rel_path)
                new_revs[ref.rel_path] = ref.updated
                local = folder_root / ref.rel_path
                if local.exists() and old_revs.get(ref.rel_path) == ref.updated:
                    stats.files_skipped += 1
                    self._record_sidecar_from_ref(
                        ref, auth, old_sources, sidecar_sources, sidecar_times
                    )
                    continue
                emit("downloading", idx, total, current_issue=ref.key)
                try:
                    issue = await self._fetch_issue(client, auth, ref.key)
                    md = self._render_issue(issue, auth, field_map)
                    existed = local.exists()
                    local.parent.mkdir(parents=True, exist_ok=True)
                    local.write_text(md, encoding="utf-8")
                    if existed:
                        stats.files_updated += 1
                    else:
                        stats.files_added += 1
                    self._record_sidecar_from_issue(
                        issue, ref, auth, field_map, sidecar_sources, sidecar_times
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("Jira: failed to sync %s: %s", ref.key, e)
                    stats.errors.append(f"issue {ref.key}: {e}")

        stats.projects_synced = len(project_keys)

        # Phase 3 — mirror-delete issues no longer in the selection.
        issues_root = folder_root / "issues"
        if issues_root.exists():
            for path in list(issues_root.rglob("*")):
                if not path.is_file() or path.name.startswith("."):
                    continue
                rel = path.relative_to(folder_root).as_posix()
                if rel not in expected_paths:
                    try:
                        path.unlink()
                        stats.files_removed += 1
                    except OSError as e:
                        stats.errors.append(f"unlink {rel}: {e}")
            # Drop emptied directories.
            for d in sorted(issues_root.rglob("*"), reverse=True):
                if d.is_dir():
                    try:
                        d.rmdir()
                    except OSError:
                        pass

        # Sidecars + revision file.
        (folder_root / ".voitta_sources.json").write_text(
            json.dumps(sidecar_sources, indent=2, sort_keys=True)
        )
        if sidecar_times:
            (folder_root / ".voitta_timestamps.json").write_text(
                json.dumps(sidecar_times, indent=2, sort_keys=True)
            )
        rev_file.write_text(json.dumps(new_revs))

        emit("done", stats.projects_synced, stats.projects_synced)
        return stats

    # ------------------------------------------------------------------
    # Field discovery (sprint / story points custom fields)
    # ------------------------------------------------------------------

    async def _discover_field_map(self, auth: AtlassianAuth) -> dict[str, str]:
        """Map logical names → custom field IDs for this instance.

        Sprint and Story Points live on per-instance ``customfield_*`` IDs, so
        we look them up once by name/schema. Best-effort: a failure just means
        those two attributes won't be promoted to ``attr_*``.
        """
        mapping: dict[str, str] = {}
        api = "3" if auth.is_cloud else "2"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{auth.base_url}/rest/api/{api}/field", headers=auth.headers()
                )
                if resp.status_code != 200:
                    return mapping
                for fdef in resp.json():
                    fid = fdef.get("id", "")
                    name = (fdef.get("name") or "").lower()
                    custom = (fdef.get("schema") or {}).get("custom", "")
                    if not fid.startswith("customfield_"):
                        continue
                    if "sprint" in name or "gh-sprint" in custom:
                        mapping.setdefault("sprint", fid)
                    elif name in ("story points", "story point estimate") or (
                        "story-points" in custom
                    ):
                        mapping.setdefault("story_points", fid)
                    elif name == "epic link":
                        mapping.setdefault("epic", fid)
        except Exception as e:  # noqa: BLE001
            logger.warning("Jira field discovery failed: %s", e)
        return mapping

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    async def _list_issue_refs(
        self,
        client: httpx.AsyncClient,
        auth: AtlassianAuth,
        project_keys: list[str],
        jql_extra: str,
    ) -> list[_IssueRef]:
        quoted = ", ".join(f'"{k}"' for k in project_keys)
        jql = f"project IN ({quoted})"
        if jql_extra:
            jql = f"({jql}) AND ({jql_extra})"
        jql += " ORDER BY updated DESC"
        light = "key,issuetype,summary,updated,created"

        refs: list[_IssueRef] = []
        if auth.is_cloud:
            token: str | None = None
            while True:
                params: dict[str, Any] = {
                    "jql": jql, "maxResults": 100, "fields": light
                }
                if token:
                    params["nextPageToken"] = token
                resp = await client.get(
                    f"{auth.base_url}/rest/api/3/search/jql",
                    params=params, headers=auth.headers(),
                )
                _raise_for_status(resp, "search issues")
                data = resp.json()
                for issue in data.get("issues", []):
                    refs.append(self._ref_from_issue(issue))
                token = data.get("nextPageToken")
                if data.get("isLast", True) or not token:
                    break
        else:
            start_at = 0
            while True:
                resp = await client.get(
                    f"{auth.base_url}/rest/api/2/search",
                    params={
                        "jql": jql, "startAt": start_at,
                        "maxResults": 100, "fields": light,
                    },
                    headers=auth.headers(),
                )
                _raise_for_status(resp, "search issues")
                data = resp.json()
                issues = data.get("issues", [])
                for issue in issues:
                    refs.append(self._ref_from_issue(issue))
                start_at += len(issues)
                if not issues or start_at >= int(data.get("total", 0)):
                    break
        return refs

    @staticmethod
    def _ref_from_issue(issue: dict) -> _IssueRef:
        key = issue["key"]
        fields = issue.get("fields", {})
        issue_type = (fields.get("issuetype") or {}).get("name", "Other")
        summary = fields.get("summary", f"Issue-{key}")
        return _IssueRef(
            key=key,
            rel_path=f"issues/{safe_component(issue_type, 'Other')}/"
            f"{key}-{safe_component(summary, key)}.md",
            issue_type=issue_type,
            summary=summary,
            updated=fields.get("updated", "") or "",
            created=fields.get("created", "") or "",
        )

    # ------------------------------------------------------------------
    # Fetch + render one issue
    # ------------------------------------------------------------------

    async def _fetch_issue(
        self, client: httpx.AsyncClient, auth: AtlassianAuth, key: str
    ) -> dict:
        api = "3" if auth.is_cloud else "2"
        resp = await client.get(
            f"{auth.base_url}/rest/api/{api}/issue/{key}",
            params={"fields": "*all", "expand": "renderedFields,changelog"},
            headers=auth.headers(),
        )
        _raise_for_status(resp, f"fetch issue {key}")
        return resp.json()

    def _render_issue(
        self, issue: dict, auth: AtlassianAuth, field_map: dict[str, str]
    ) -> str:
        fields = issue.get("fields", {})
        rendered = issue.get("renderedFields", {}) or {}
        key = issue["key"]
        attrs, _raw = self._extract_attrs(issue, field_map)

        def g(name: str) -> str:
            return _flatten_value(fields.get(name))

        lines: list[str] = [f"# [{key}] {fields.get('summary', '')}\n"]
        # Attribute table — makes structured fields searchable in the body too.
        lines.append("| Field | Value |")
        lines.append("|---|---|")
        web = f"{auth.base_url}/browse/{key}"
        lines.append(f"| Key | [{key}]({web}) |")
        rows = [
            ("Type", attrs.get("issuetype")),
            ("Status", attrs.get("status")),
            ("Priority", attrs.get("priority")),
            ("Resolution", attrs.get("resolution")),
            ("Assignee", attrs.get("assignee")),
            ("Reporter", attrs.get("reporter")),
            ("Created", g("created")[:10]),
            ("Updated", g("updated")[:10]),
            ("Due", g("duedate")),
            ("Epic/Parent", attrs.get("epic") or attrs.get("parent")),
            ("Sprint", attrs.get("sprint")),
            ("Story Points", attrs.get("story_points")),
            ("Labels", ", ".join(attrs.get("labels", []))),
            ("Components", ", ".join(attrs.get("components", []))),
            ("Fix Version/s", ", ".join(attrs.get("fix_versions", []))),
            ("Affects Version/s", ", ".join(attrs.get("affects_versions", []))),
        ]
        for label, val in rows:
            if val:
                lines.append(f"| {label} | {val} |")
        lines.append("")

        # Description — prefer rendered HTML (handles Cloud ADF + Server wiki).
        desc = _html_to_markdown(rendered.get("description") or "") or _flatten_value(
            fields.get("description")
        )
        if desc:
            lines.append("## Description\n")
            lines.append(desc)
            lines.append("")

        # Comments.
        comment_obj = fields.get("comment") or {}
        comments = comment_obj.get("comments", []) if isinstance(comment_obj, dict) else []
        rendered_comments = (rendered.get("comment") or {}).get("comments", []) \
            if isinstance(rendered.get("comment"), dict) else []
        if comments:
            lines.append("## Comments\n")
            for i, c in enumerate(comments):
                author = (c.get("author") or {}).get("displayName", "Unknown")
                date = (c.get("created") or "")[:10]
                body = ""
                if i < len(rendered_comments):
                    body = _html_to_markdown(rendered_comments[i].get("body") or "")
                if not body:
                    body = _flatten_value(c.get("body"))
                lines.append(f"### {author} ({date})\n")
                lines.append(body)
                lines.append("")

        # Attachments as links.
        attachments = fields.get("attachment") or []
        if attachments:
            lines.append("## Attachments\n")
            for att in attachments:
                name = att.get("filename", "attachment")
                url = att.get("content", "")
                lines.append(f"- [{name}]({url})")
            lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Attribute extraction
    # ------------------------------------------------------------------

    def _extract_attrs(
        self, issue: dict, field_map: dict[str, str]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return ``(curated_attrs, raw_bag)`` for one issue.

        Curated attrs are the canonical, filterable set (see source_meta
        ATTR_PAYLOAD_FIELDS). The raw bag is every other scalar/custom field,
        flattened to short strings, minus the bulky body fields.
        """
        fields = issue.get("fields", {})
        attrs: dict[str, Any] = {}

        def name_of(obj: Any) -> str:
            return obj.get("name", "") if isinstance(obj, dict) else ""

        attrs["status"] = name_of(fields.get("status"))
        attrs["priority"] = name_of(fields.get("priority"))
        attrs["issuetype"] = name_of(fields.get("issuetype"))
        attrs["resolution"] = name_of(fields.get("resolution"))
        attrs["project"] = (fields.get("project") or {}).get("key", "")
        attrs["assignee"] = (fields.get("assignee") or {}).get("displayName", "")
        attrs["reporter"] = (fields.get("reporter") or {}).get("displayName", "")
        attrs["labels"] = list(fields.get("labels") or [])
        attrs["components"] = [name_of(c) for c in (fields.get("components") or [])]
        attrs["fix_versions"] = [name_of(v) for v in (fields.get("fixVersions") or [])]
        attrs["affects_versions"] = [name_of(v) for v in (fields.get("versions") or [])]

        # Parent / epic.
        parent = (fields.get("parent") or {}).get("key", "")
        attrs["parent"] = parent
        epic = ""
        epic_fid = field_map.get("epic")
        for fid in ([epic_fid] if epic_fid else []) + list(_EPIC_CUSTOM_FIELDS):
            val = fields.get(fid) if fid else None
            if val:
                epic = val.get("key", "") if isinstance(val, dict) else str(val)
                break
        attrs["epic"] = epic or parent

        # Sprint / story points from discovered custom fields.
        if field_map.get("sprint"):
            attrs["sprint"] = _flatten_value(fields.get(field_map["sprint"]))
        if field_map.get("story_points"):
            sp = fields.get(field_map["story_points"])
            if isinstance(sp, (int, float)):
                attrs["story_points"] = sp

        votes = fields.get("votes")
        if isinstance(votes, dict) and isinstance(votes.get("votes"), int):
            attrs["votes"] = votes["votes"]
        watches = fields.get("watches")
        if isinstance(watches, dict) and isinstance(watches.get("watchCount"), int):
            attrs["watches"] = watches["watchCount"]

        # Raw bag — every other field, flattened, minus the bulky/body keys and
        # the curated custom fields we already consumed.
        consumed = {field_map.get("sprint"), field_map.get("story_points"),
                    field_map.get("epic"), *(_EPIC_CUSTOM_FIELDS)}
        raw: dict[str, Any] = {}
        for fname, fval in fields.items():
            if fname in _RAW_DENYLIST or fname in consumed:
                continue
            flat = _flatten_value(fval)
            if flat:
                raw[fname] = flat[:500]
        return attrs, raw

    @staticmethod
    def _last_editor(issue: dict) -> dict[str, str]:
        """Most recent changelog author → editor; empty if no history."""
        histories = (issue.get("changelog") or {}).get("histories") or []
        if histories:
            # Jira returns histories oldest→newest; take the last.
            author = histories[-1].get("author") or {}
            return {
                "name": author.get("displayName", ""),
                "email": author.get("emailAddress", ""),
            }
        return {"name": "", "email": ""}

    # ------------------------------------------------------------------
    # Sidecar records
    # ------------------------------------------------------------------

    def _record_sidecar_from_issue(
        self,
        issue: dict,
        ref: _IssueRef,
        auth: AtlassianAuth,
        field_map: dict[str, str],
        sidecar_sources: dict[str, dict[str, Any]],
        sidecar_times: dict[str, dict[str, str]],
    ) -> None:
        fields = issue.get("fields", {})
        reporter = fields.get("reporter") or {}
        editor = self._last_editor(issue)
        attrs, raw = self._extract_attrs(issue, field_map)
        record: dict[str, Any] = {
            "source": "jira",
            "url": f"{auth.base_url}/browse/{ref.key}",
            "issue_key": ref.key,
            "project": attrs.get("project", ""),
        }
        record.update(
            sm.build(
                owner_name=reporter.get("displayName"),
                owner_email=reporter.get("emailAddress"),
                editor_name=editor.get("name"),
                editor_email=editor.get("email"),
                created=fields.get("created"),
                modified=fields.get("updated"),
                attrs=attrs,
                attrs_raw=raw,
            )
        )
        sidecar_sources[ref.rel_path] = record
        times = {
            k: v
            for k, v in (
                ("modified_at", fields.get("updated")),
                ("created_at", fields.get("created")),
            )
            if v
        }
        if times:
            sidecar_times[ref.rel_path] = times

    def _record_sidecar_from_ref(
        self,
        ref: _IssueRef,
        auth: AtlassianAuth,
        old_sources: dict[str, dict[str, Any]],
        sidecar_sources: dict[str, dict[str, Any]],
        sidecar_times: dict[str, dict[str, str]],
    ) -> None:
        """Sidecar for a skipped (unchanged) issue.

        We don't re-fetch the full issue. The previous sync's record already
        holds the captured attributes, so carry it forward verbatim; that keeps
        a re-index from clobbering ``source_meta`` with an attribute-less stub.
        Falls back to a minimal record only when no prior entry exists.
        """
        prior = old_sources.get(ref.rel_path)
        if isinstance(prior, dict) and prior:
            sidecar_sources[ref.rel_path] = prior
        else:
            sidecar_sources[ref.rel_path] = {
                "source": "jira",
                "url": f"{auth.base_url}/browse/{ref.key}",
                "issue_key": ref.key,
                **sm.build(created=ref.created, modified=ref.updated),
            }
        times = {
            k: v
            for k, v in (("modified_at", ref.updated), ("created_at", ref.created))
            if v
        }
        if times:
            sidecar_times[ref.rel_path] = times
