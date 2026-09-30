"""The assistant's tools — one registry, rendered for both engines.

Every tool is a :class:`ToolSpec`: a name, a model-facing description, a
Pydantic input model (the JSON schema the model sees AND the validator its
arguments go through) and a handler. Synchronous handlers run off the event
loop (``asyncio.to_thread``) and open their own DB sessions through the
services they call; async handlers (live directory lookups) are awaited.

All tools are read-only. Content, sync and folder tools run as
``ToolContext.viewer`` — the effective (possibly impersonated) account,
folder scope ``"visible"`` — so folder ACL is enforced by the services they
call, not here. Admin tools use ``ToolContext.admin_scope``: the REAL
person's administrative domain, set only when they are an admin — exactly
the admin console's rule — and are offered to the model only then
(:func:`tools_for`).

Results are JSON text for the model plus, for image tools, image payloads.
Oversized text is truncated with an explicit note, never silently.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...db.database import session_scope
from .. import retrieval, settings_overview, sync_overview
from ..acl import CurrentUser
from ..admin_scope import AdminScope
from ..retrieval import Viewer

# Upper bound on one tool result's text. Large enough for a long chunk
# range; small enough that one call can't flood the context window.
MAX_RESULT_CHARS = 60_000
# Default long edge for images handed to the model.
DEFAULT_IMAGE_EDGE = 1024


@dataclass(frozen=True)
class ToolContext:
    viewer: Viewer
    # The person typing and the account whose view is active (they differ
    # during impersonation). None only in contexts without a signed-in
    # person (tests of content tools).
    real: CurrentUser | None = None
    effective: CurrentUser | None = None
    # Set only when the real person is an admin; scopes the admin tools.
    admin_scope: AdminScope | None = None


@dataclass(frozen=True)
class ToolImage:
    image_id: int
    mime: str
    data_base64: str
    max_size: int


@dataclass(frozen=True)
class ToolOutput:
    text: str
    summary: str
    images: list[ToolImage] = field(default_factory=list)
    is_error: bool = False


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[ToolContext, Any], ToolOutput | Awaitable[ToolOutput]]

    def input_schema(self) -> dict[str, Any]:
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        return schema

    async def run(self, ctx: ToolContext, raw_input: Any) -> ToolOutput:
        """Validate ``raw_input`` and run the handler. Never raises for bad
        input or a refused lookup: those become ``is_error`` results the
        model can read and recover from."""
        try:
            args = self.input_model.model_validate(raw_input or {})
        except ValidationError as e:
            return error_output(f"invalid arguments for {self.name}: {e.errors(include_url=False)}")
        try:
            if inspect.iscoroutinefunction(self.handler):
                return await self.handler(ctx, args)
            return await asyncio.to_thread(self.handler, ctx, args)
        except (ValueError, FileNotFoundError) as e:
            return error_output(str(e))


def error_output(message: str) -> ToolOutput:
    return ToolOutput(text=json.dumps({"error": message}), summary=message[:120], is_error=True)


def _json_output(payload: Any, summary: str) -> ToolOutput:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = (
            text[:MAX_RESULT_CHARS]
            + f"\n…[truncated: result was {len(text)} characters; narrow the request]"
        )
    return ToolOutput(text=text, summary=summary)


def _dump(items: list[BaseModel]) -> list[dict[str, Any]]:
    return [i.model_dump() for i in items]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- RAG -------------------------------------------------------------------


class ListFoldersArgs(_Args):
    prefix: str | None = Field(
        default=None,
        description="Empty for the list of top-level folders; 'Folder name/sub/dir' "
        "to list that directory's subfolders and files.",
    )


def _list_folders(ctx: ToolContext, a: ListFoldersArgs) -> ToolOutput:
    rows = retrieval.list_indexed_folders(ctx.viewer, a.prefix)
    noun = "entries" if a.prefix else "folders"
    return _json_output(_dump(rows), f"{len(rows)} {noun}")


class SearchArgs(_Args):
    query: str = Field(description="Short, concrete query; exact terms help the keyword side.")
    folder_ids: list[int] | None = Field(
        default=None, description="Restrict to these folder ids (from list_folders)."
    )
    limit: int = Field(default=12, ge=1, le=50)


def _search(ctx: ToolContext, a: SearchArgs) -> ToolOutput:
    hits = retrieval.search(ctx.viewer, a.query, a.folder_ids, a.limit)
    return _json_output(_dump(hits), f"{len(hits)} hits")


def _search_images(ctx: ToolContext, a: SearchArgs) -> ToolOutput:
    hits = retrieval.search_images(ctx.viewer, a.query, a.folder_ids, a.limit)
    return _json_output(_dump(hits), f"{len(hits)} images")


class ChunkRangeArgs(_Args):
    file_id: int
    start: int = Field(default=0, ge=0)
    end: int = Field(default=10, ge=1, description="Exclusive; at most start+200.")


def _chunk_range(ctx: ToolContext, a: ChunkRangeArgs) -> ToolOutput:
    chunks = retrieval.get_chunk_range(ctx.viewer, a.file_id, a.start, min(a.end, a.start + 200))
    return _json_output(_dump(chunks), f"{len(chunks)} chunks")


class FileArgs(_Args):
    file_id: int


def _get_file(ctx: ToolContext, a: FileArgs) -> ToolOutput:
    result = retrieval.get_file(ctx.viewer, a.file_id)
    return _json_output(result, result["file"]["rel_path"])


def _page_images(ctx: ToolContext, a: FileArgs) -> ToolOutput:
    pages = retrieval.list_page_images(ctx.viewer, a.file_id)
    return _json_output(_dump(pages), f"{len(pages)} pages")


class ChunkArgs(_Args):
    chunk_id: int


def _chunk_images(ctx: ToolContext, a: ChunkArgs) -> ToolOutput:
    images = retrieval.get_chunk_images(ctx.viewer, a.chunk_id)
    return _json_output(_dump(images), f"{len(images)} images")


class ImageArgs(_Args):
    image_id: int
    max_size: int = Field(
        default=DEFAULT_IMAGE_EDGE, ge=128, le=2048, description="Long edge in pixels."
    )


# Image types the Messages API accepts; anything else is re-encoded as PNG.
API_IMAGE_MIMES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})


def _api_image(image_id: int, mime: str, data_base64: str, max_size: int) -> ToolImage:
    if mime in API_IMAGE_MIMES:
        return ToolImage(image_id, mime, data_base64, max_size)
    import io

    from PIL import Image as PILImage

    try:
        with PILImage.open(io.BytesIO(base64.b64decode(data_base64))) as img:
            buf = io.BytesIO()
            img.convert("RGBA").save(buf, format="PNG")
    except Exception as e:
        raise ValueError(f"image {image_id} is in an unsupported format ({mime})") from e
    return ToolImage(image_id, "image/png", base64.b64encode(buf.getvalue()).decode("ascii"), max_size)


def _image_output(result: dict[str, Any], max_size: int, label: str) -> ToolOutput:
    image = _api_image(int(result["image_id"]), result["mime"], result["data_base64"], max_size)
    meta = {k: v for k, v in result.items() if k != "data_base64"}
    meta["mime"] = image.mime
    return ToolOutput(text=json.dumps(meta, default=str), summary=label, images=[image])


def _get_image(ctx: ToolContext, a: ImageArgs) -> ToolOutput:
    result = retrieval.get_image(ctx.viewer, a.image_id, a.max_size)
    return _image_output(result, a.max_size, f"image {a.image_id}")


class PageImageArgs(_Args):
    file_id: int
    page: int = Field(ge=1, description="1-indexed page number.")
    max_size: int = Field(default=DEFAULT_IMAGE_EDGE, ge=128, le=2048)


def _get_page_image(ctx: ToolContext, a: PageImageArgs) -> ToolOutput:
    result = retrieval.get_page_image(ctx.viewer, a.file_id, a.page, a.max_size, True)
    return _image_output(result, a.max_size, f"page {a.page}")


class PageLayoutArgs(_Args):
    file_id: int
    page: int = Field(ge=1)


def _page_layout(ctx: ToolContext, a: PageLayoutArgs) -> ToolOutput:
    blocks = retrieval.get_page_layout(ctx.viewer, a.file_id, a.page)
    return _json_output(blocks, f"{len(blocks)} layout blocks")


class UrlArgs(_Args):
    url: str


def _resolve_url(ctx: ToolContext, a: UrlArgs) -> ToolOutput:
    files = retrieval.resolve_url(ctx.viewer, a.url)
    return _json_output(_dump(files), f"{len(files)} files")


# --- sync + indexing state ---------------------------------------------------


class NoArgs(_Args):
    pass


def _sync_overview(ctx: ToolContext, _a: NoArgs) -> ToolOutput:
    with session_scope() as s:
        overview = sync_overview.sync_overview(s, ctx.viewer)
    failing = sum(1 for f in overview.folders if f.sync_status == "error")
    summary = f"{len(overview.folders)} folders" + (f", {failing} failing" if failing else "")
    return _json_output(overview.model_dump(), summary)


class FolderArgs(_Args):
    folder_id: int


def _folder_sync_detail(ctx: ToolContext, a: FolderArgs) -> ToolOutput:
    with session_scope() as s:
        detail = sync_overview.folder_sync_detail(s, ctx.viewer, a.folder_id)
    return _json_output(detail.model_dump(), detail.summary.display_name)


class FileProblemsArgs(_Args):
    folder_id: int
    include_unsupported: bool = Field(
        default=True, description="Also list files parked as unsupported (not only errors)."
    )
    limit: int = Field(default=50, ge=1, le=200)


def _file_problems(ctx: ToolContext, a: FileProblemsArgs) -> ToolOutput:
    states = ("error", "unsupported") if a.include_unsupported else ("error",)
    with session_scope() as s:
        rows = sync_overview.file_problems(s, ctx.viewer, a.folder_id, states=states, limit=a.limit)
    return _json_output(_dump(rows), f"{len(rows)} files")


class RecentJobsArgs(_Args):
    limit: int = Field(default=30, ge=1, le=100)


def _recent_jobs(ctx: ToolContext, a: RecentJobsArgs) -> ToolOutput:
    with session_scope() as s:
        jobs = sync_overview.recent_jobs_view(s, ctx.viewer, limit=a.limit)
    running = sum(1 for j in jobs if j["state"] == "running")
    return _json_output(jobs, f"{len(jobs)} jobs, {running} running")


# --- settings (read-only) ------------------------------------------------------


def _people(ctx: ToolContext) -> tuple[CurrentUser, CurrentUser]:
    if ctx.real is None or ctx.effective is None:
        raise ValueError("account information is not available here")
    return ctx.real, ctx.effective


def _admin(ctx: ToolContext) -> AdminScope:
    if ctx.admin_scope is None:
        raise ValueError("admin settings are available to admins only")
    return ctx.admin_scope


def _my_account(ctx: ToolContext, _a: NoArgs) -> ToolOutput:
    real, effective = _people(ctx)
    with session_scope() as s:
        overview = settings_overview.account_overview(s, real, effective)
    return _json_output(overview, overview["active_account"]["email"])


def _folder_settings(ctx: ToolContext, a: FolderArgs) -> ToolOutput:
    with session_scope() as s:
        result = settings_overview.folder_settings(s, ctx.viewer, a.folder_id)
    return _json_output(result, result["display_name"])


def _sync_credentials(ctx: ToolContext, _a: NoArgs) -> ToolOutput:
    _real, effective = _people(ctx)
    with session_scope() as s:
        rows = settings_overview.sync_credentials(s, effective)
    return _json_output(rows, f"{len(rows)} credentials")


def _admin_overview(ctx: ToolContext, _a: NoArgs) -> ToolOutput:
    scope = _admin(ctx)
    _real, effective = _people(ctx)
    with session_scope() as s:
        result = settings_overview.admin_overview(s, scope, effective)
    return _json_output(result, "deployment settings")


class AdminUsersArgs(_Args):
    query: str | None = Field(
        default=None, description="Case-insensitive substring of email, company or name."
    )
    limit: int = Field(default=100, ge=1, le=500)


def _admin_users(ctx: ToolContext, a: AdminUsersArgs) -> ToolOutput:
    scope = _admin(ctx)
    with session_scope() as s:
        result = settings_overview.admin_users(s, scope, query=a.query, limit=a.limit)
    return _json_output(result, f"{result['total']} accounts")


class AdminGroupsArgs(_Args):
    include_members: bool = Field(default=False, description="Also list each group's members.")


def _admin_groups(ctx: ToolContext, a: AdminGroupsArgs) -> ToolOutput:
    scope = _admin(ctx)
    with session_scope() as s:
        rows = settings_overview.admin_groups(s, scope, include_members=a.include_members)
    return _json_output(rows, f"{len(rows)} groups")


async def _admin_company_directory(ctx: ToolContext, _a: NoArgs) -> ToolOutput:
    _admin(ctx)
    real, _effective = _people(ctx)
    instances = await settings_overview.admin_company_directory(real.email)
    if not instances:
        return _json_output({"note": "No Clerk company directory is enabled."}, "not enabled")
    users = sum(len(i["users"]) for i in instances)
    return _json_output(instances, f"{users} directory users")


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "list_folders",
        "List the indexed folders the user can see (id, name, source, file counts), or "
        "browse inside one: pass prefix='<folder name>/<sub/dir>' to list its "
        "subfolders and files with their file ids. Start here to orient.",
        ListFoldersArgs,
        _list_folders,
    ),
    ToolSpec(
        "search",
        "Hybrid (semantic + keyword) search over document chunks. Returns chunk text "
        "with file_id, file_path, chunk_index, page and source_url. kind='folder_card' "
        "hits mean a folder's name/description matched: re-search with its folder_id.",
        SearchArgs,
        _search,
    ),
    ToolSpec(
        "search_images",
        "Find figures, charts, slides and photos by describing what they show. Returns "
        "image ids; call get_image to look at one.",
        SearchArgs,
        _search_images,
    ),
    ToolSpec(
        "get_chunk_range",
        "Read chunks [start, end) of a file in order — the way to read around a search "
        "hit (e.g. hit.chunk_index ± 5). Prefer this over get_file for long documents.",
        ChunkRangeArgs,
        _chunk_range,
    ),
    ToolSpec(
        "get_file",
        "A file's metadata plus its full extracted markdown. Only for short documents; "
        "long ones are truncated — use get_chunk_range instead.",
        FileArgs,
        _get_file,
    ),
    ToolSpec(
        "get_chunk_images",
        "Figures linked to a chunk (by chunk_id from search / get_chunk_range).",
        ChunkArgs,
        _chunk_images,
    ),
    ToolSpec(
        "get_image",
        "Look at an image (figure or page render) by image_id. The image is shown to "
        "you and to the user.",
        ImageArgs,
        _get_image,
    ),
    ToolSpec(
        "list_page_images",
        "The page renders of a PDF (or slide thumbnails of a presentation), in order.",
        FileArgs,
        _page_images,
    ),
    ToolSpec(
        "get_page_image",
        "Look at one page of a PDF as an image, with its layout blocks. The page is "
        "shown to you and to the user.",
        PageImageArgs,
        _get_page_image,
    ),
    ToolSpec(
        "get_page_layout",
        "Structured layout of one PDF page (block types, positions, text) without the "
        "image — for questions about page structure.",
        PageLayoutArgs,
        _page_layout,
    ),
    ToolSpec(
        "resolve_url",
        "Find the indexed files that came from an external URL (Google Drive, GitHub, "
        "SharePoint link…).",
        UrlArgs,
        _resolve_url,
    ),
    ToolSpec(
        "sync_overview",
        "Sync and indexing status of every folder the user can see: source type, sync "
        "status and last error (owners), last/next sync, whether a sync is queued or "
        "running, indexing activity, and file counts by state. Start here for any "
        "question about sync health or missing documents.",
        NoArgs,
        _sync_overview,
    ),
    ToolSpec(
        "folder_sync_detail",
        "Deep view of one folder: what it syncs from (owners), index health (SQLite vs "
        "vector store), per-file-type counts, and the last sync runs with their results "
        "and errors (owners).",
        FolderArgs,
        _folder_sync_detail,
    ),
    ToolSpec(
        "file_problems",
        "Files in a folder that failed to index (state error) or were skipped as "
        "unsupported, with the recorded reason.",
        FileProblemsArgs,
        _file_problems,
    ),
    ToolSpec(
        "recent_jobs",
        "The indexing/sync job queue: running jobs first, then the most recent, with "
        "kind, state, target file/folder and error.",
        RecentJobsArgs,
        _recent_jobs,
    ),
    ToolSpec(
        "my_account",
        "The user's own settings: who they are, whether they are an admin, the "
        "active account (and whether an admin is viewing as someone else), the "
        "accounts they can switch between, their groups, their personal MCP API "
        "keys (names and last use) and the folders they switched off for MCP.",
        NoArgs,
        _my_account,
    ),
    ToolSpec(
        "folder_settings",
        "One folder's settings: owner, path, sharing with the community, whether "
        "the user can write files, MCP activation, subfolder descriptions, and — "
        "for the folder's owner — the full share list (audience, groups, people). "
        "Sync configuration is in folder_sync_detail.",
        FolderArgs,
        _folder_settings,
    ),
    ToolSpec(
        "sync_credentials",
        "The company's reusable sync credentials (Google OAuth clients and service "
        "accounts): label, kind, whether connected and as whom, who created them and "
        "how many folders use them. Secrets are never shown.",
        NoArgs,
        _sync_credentials,
    ),
)

# Offered only when the real person is an admin (see tools_for).
ADMIN_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "admin_overview",
        "Admin console settings: the admin's own permissions and scope, sign-in "
        "access (allowed domains/emails, blocked emails, super-admins), sign-in "
        "providers, company directory (Clerk) instances, NFS / linked-folder roots "
        "and their health, indexing caps, company API keys, assistant policy and "
        "the deployment's runtime configuration. Secrets are never shown.",
        NoArgs,
        _admin_overview,
    ),
    ToolSpec(
        "admin_users",
        "Accounts in the admin's scope (one row per account — a person can have "
        "several): email, company, admin flags, groups, number of folders owned, "
        "created. Filter with query; a regular admin sees only their companies.",
        AdminUsersArgs,
        _admin_users,
    ),
    ToolSpec(
        "admin_groups",
        "Voitta-native groups with member counts, optionally with their members "
        "(limited to the admin's scope).",
        AdminGroupsArgs,
        _admin_groups,
    ),
    ToolSpec(
        "admin_company_directory",
        "Live company directory (Clerk): organizations, their members and roles, "
        "and users with last sign-in, scoped to the admin's companies.",
        NoArgs,
        _admin_company_directory,
    ),
)

TOOLS_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in (*TOOLS, *ADMIN_TOOLS)}


def tools_for(ctx: ToolContext) -> tuple[ToolSpec, ...]:
    """The tools offered in a turn: admin tools only to admins."""
    return (*TOOLS, *ADMIN_TOOLS) if ctx.admin_scope is not None else TOOLS


def replay_image(ctx: ToolContext, image_id: int, max_size: int) -> ToolImage | None:
    """Re-read an image referenced by a stored transcript, under the CURRENT
    viewer's ACL. ``None`` when it is gone or no longer visible."""
    try:
        result = retrieval.get_image(ctx.viewer, image_id, max_size)
        return _api_image(image_id, result["mime"], result["data_base64"], max_size)
    except ValueError:
        return None
