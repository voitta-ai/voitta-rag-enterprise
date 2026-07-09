"""Application settings loaded from the environment.

All env vars carry the ``VOITTA_`` prefix; see ``.env.example`` for the
full list.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from typing import Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_data_dir() -> Path:
    return Path(os.path.expanduser("~/.voitta-rag-enterprise"))


class Settings(BaseSettings):
    """Environment-driven settings."""

    model_config = SettingsConfigDict(
        env_prefix="VOITTA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Storage
    data_dir: Path = _default_data_dir()
    db_path: Path | None = None
    cas_dir: Path | None = None
    # Three deployment shapes:
    #   * "embedded"   — in-process QdrantClient(path=…). Pure-Python local
    #                    backend: no Docker, no subprocess, but stripped down
    #                    (slower, single-process, no server-side Query API /
    #                    quantization / snapshots).
    #   * "standalone" — connect to an externally-run Qdrant server over
    #                    VOITTA_QDRANT_URL (the Docker / k8s deployment).
    #   * "managed"    — the app spawns the native Qdrant *binary* as a
    #                    localhost subprocess and connects to it over HTTP.
    #                    Full-engine feature parity, no Docker. Hard-fail: if
    #                    the binary is missing or won't become healthy, boot
    #                    crashes — there is NO fallback to embedded.
    qdrant_mode: Literal["embedded", "standalone", "managed"] = "embedded"
    qdrant_url: str | None = None
    qdrant_path: Path | None = None
    # managed-mode knobs (ignored unless qdrant_mode="managed").
    # ``qdrant_binary`` is a path or a name resolved on PATH (default
    # "qdrant"). The subprocess binds localhost only; storage defaults to
    # ``data_dir/qdrant_managed`` (kept separate from the embedded backend's
    # ``data_dir/qdrant`` — the on-disk formats are NOT compatible).
    # Ports default to 0 = pick a free ephemeral port at spawn time, which
    # makes adopting a foreign/stale Qdrant on a well-known port impossible.
    # Set explicit ports only when ops need a pinned port; a busy pinned port
    # is then a hard boot error, never silently reused.
    qdrant_binary: str | None = None
    qdrant_managed_host: str = "127.0.0.1"
    qdrant_managed_http_port: int = 0
    qdrant_managed_grpc_port: int = 0
    qdrant_managed_startup_timeout_s: float = 30.0

    # Cloud-local sources (Google Drive mount indexed in place): when False
    # (default), the extract worker refuses to read "dataless" placeholder
    # files — a read would make the provider download the full content
    # synchronously with no timeout, which can freeze the (single-worker)
    # indexing queue on a slow link. Such files are parked as unsupported
    # with a clear reason. True restores download-on-read for deployments
    # that want the whole mount materialized and indexed.
    cloud_materialize_on_index: bool = False

    # Required parent for all user-registered folders. Every folder is created
    # under this path; arbitrary host paths cannot be registered.
    root_path: Path | None = None

    # Network
    port: int = 8000
    mcp_port: int = 8001
    # Public base URL the API is reachable at (e.g. https://enterprise.voitta.ai).
    # Signed asset URLs returned to MCP clients are prefixed with this so the
    # client can fetch them directly; leave unset for local dev (relative
    # ``/api/assets/<token>`` paths). No trailing slash — joined as
    # ``<base>/api/assets/<token>``.
    public_base_url: str | None = None

    # Workers. Hard default = 1.
    #
    # Indexing is fundamentally serial: extract holds _EXTRACT_LOCK for the
    # whole pipeline (PyMuPDF / cairo are not thread-safe at the C level)
    # and gpu_lock serializes every GPU touch. With multiple workers, N-1
    # of them spin idle on claim_one() most of the time; the only effect
    # of a higher count is more memory pressure and the occasional
    # interleaved sync/delete job. The queue + worker structure is kept
    # so jobs survive a uvicorn restart and the UI sees progress events;
    # parallelism in the worker pool itself buys nothing.
    #
    # The UI is unaffected: REST/WS run on the asyncio loop, the worker
    # runs sync code via asyncio.to_thread, and gpu_lock keeps query
    # embeds from competing with extract.
    workers: int | None = None

    # Embedding models + version stamps
    dense_model: str = "intfloat/e5-base-v2"
    sparse_model: str = "Qdrant/bm25"
    image_model: str = "google/siglip2-base-patch16-224"
    dense_version: str = "e5-base-v2@1"
    sparse_version: str = "bm25@1"
    image_version: str = "siglip2-base@1"

    # Indexing
    nearby_radius: int = 2
    max_file_bytes: int = 1024 * 1024 * 1024
    # Glob-style patterns matched against any path component. Anything
    # matching is skipped by the watcher, scanner, and the Google Drive
    # connector's enumeration step. Three rough buckets of defaults:
    #
    # 1. Repo / VCS clutter (.git, node_modules, .venv, …) — never
    #    interesting on its own; .git-repo is our internal mirror dir.
    # 2. Voitta sidecars + tempfiles.
    # 3. Media + archive + binary blobs — large, unparseable. Indexing
    #    them costs disk + Qdrant points without yielding RAG content
    #    (audio/video transcripts and PDFs go through their own parsers,
    #    not via passthrough text). On the GD path, skipping at enumerate
    #    time also saves the egress for big mp4s.
    ignore_patterns: str = (
        ".git,node_modules,.DS_Store,__pycache__,.venv,*.tmp,*.part-*,"
        ".git-repo,"
        # Voitta sidecars written by sync connectors. ``_sources.json``
        # carries per-file remote metadata (Drive deep-link tab URL, tab
        # title); ``_timestamps.json`` is the per-file mtime cache the
        # GD connector uses for change detection; ``_sync.lock`` guards
        # concurrent syncs of the same folder. None of these belong in
        # the index — they're internal bookkeeping that mutates on
        # every sync and would otherwise show up in the file tree.
        ".voitta_sources.json,.voitta_timestamps.json,.voitta_sync.lock,"
        ".voitta_nfs_sources.json,.voitta_jira_revisions.json,"
        ".voitta_confluence_revisions.json,"
        # Per-file metadata sidecars — never indexed directly.
        "*.voitta.meta,"
        # Sidecar dir for full Google Sheets workbooks (.xlsx) downloaded
        # alongside the per-sheet markdown summaries. Indexer must NOT
        # see these — the markdown is the searchable representation; the
        # xlsx is retrieved on demand via the voitta_rag_get_workbook
        # MCP tool. Same .voitta_* convention as the other sidecars.
        ".voitta_workbooks,"
        # audio
        "*.mp3,*.m4a,*.wav,*.flac,*.ogg,*.aac,*.opus,*.wma,"
        # video
        "*.mp4,*.mov,*.avi,*.mkv,*.webm,*.wmv,*.flv,*.m4v,*.mpeg,*.mpg,"
        # archives / compressed blobs
        "*.zip,*.tar,*.tar.gz,*.tgz,*.tar.bz2,*.tbz2,*.tar.xz,*.txz,"
        "*.gz,*.bz2,*.xz,*.7z,*.rar,*.lz4,*.zst,"
        # disk images / installers
        "*.iso,*.dmg,*.img,*.pkg,*.deb,*.rpm,*.exe,*.msi,*.apk,*.ipa,"
        # binaries / shared libraries (executable formats vary by platform;
        # globs catch the common build artefacts)
        "*.dll,*.so,*.dylib,*.a,*.o,*.obj,*.class,*.pyc,*.pyo,*.wasm,"
        # heavy ML weights / datasets — useless to chunk as text
        "*.bin,*.safetensors,*.ckpt,*.pth,*.pt,*.onnx,*.h5,*.parquet,"
        "*.arrow,*.feather"
    )

    # PDF parsing (MinerU)
    pdf_pages_per_bucket: int = 20
    pdf_parse_method: str = "auto"  # MinerU parse_method: auto|txt|ocr
    pdf_lang: str = "en"
    # Per-bucket wall-clock budget. MinerU has been observed to wedge in
    # native code on certain PDFs (no GPU activity, no progress logs) — the
    # parent has no way to interrupt that since CPython can't deliver
    # signals into a blocked C thread. We isolate every parse in a long-
    # lived subprocess and kill -9 it when this timeout fires; the offending
    # file is parked as ``state='error'`` and the queue keeps moving.
    # 600s = 10 minutes is generous for a 20-page bucket on a single GPU.
    pdf_parse_timeout_s: int = 600
    # Per-page WebP renders captured for layout context. The LLM already
    # has the per-figure crops + the full markdown; these renders are only
    # for "show me what this page looks like". Long-edge 1024 ≈ ~50 KB/page
    # at quality 75 — good enough for layout reasoning, cheap to store.
    pdf_render_pages: bool = True
    pdf_page_render_long_edge_px: int = 1024
    pdf_page_render_webp_quality: int = 75
    # Test override: when true, the PDF parser returns a deterministic stub
    # without invoking MinerU. Used by the test suite to keep runs fast.
    use_fake_pdf_parser: bool = False

    # Auth
    single_user: bool = False
    dev_user: str | None = None
    users_file: Path = Path("users.txt")

    # Legacy: pre-admin-UI allowlist. No longer consulted by the sign-in
    # gate — admins manage the allowlist via the UI (stored on the data
    # PD, see services/admin_store.py). Kept as a field so existing
    # ``VOITTA_ALLOWED_DOMAINS`` env vars don't blow up Settings init;
    # ignore otherwise.
    allowed_domains: str = ""

    # Comma-separated bootstrap admin emails. Every address listed here
    # is *unconditionally* admitted at sign-in (block-list aside) and
    # gets ``is_admin=True`` stamped on the User row on every login. The
    # purpose is to keep at least one admin able to sign in even when
    # the data-PD allowlists are empty (fresh deploy, lockout recovery).
    # In production this is set per-deploy via Terraform.
    super_admins: str = ""

    # "Sign in with Google" — when both ``google_auth_client_id`` and
    # ``google_auth_client_secret`` are set the API exposes
    # ``/api/auth/login/google`` and the UI renders a sign-in button. The
    # signed session cookie carries the email; ``current_user`` reads it.
    # ``session_secret`` is used to sign the cookie. If unset, a stable random
    # secret is generated and persisted under ``data_dir`` so existing logins
    # survive restarts.
    google_auth_client_id: str | None = None
    google_auth_client_secret: str | None = None
    # Built-in Google Drive OAuth client (NOT the sign-in pair above) —
    # a Desktop-type GCP client shipped with the desktop app so users can
    # connect Drive sync without creating their own GCP project. Baked
    # into the build by desktop/build_app.sh; only honoured in
    # single-user (desktop) mode, where the consent redirect can reach
    # this server on 127.0.0.1. Never sent to the SPA.
    gd_builtin_client_id: str = ""
    gd_builtin_client_secret: str = ""
    # Clerk (clerk.com) Backend API — server-side directory reads (users,
    # organizations, memberships) against https://api.clerk.com/v1. The
    # secret key comes from Clerk Dashboard → Configure → API Keys
    # (``sk_test_…`` for the Development instance, ``sk_live_…`` for
    # Production; each instance has its own users/orgs). Accepted under
    # either the repo-conventional ``VOITTA_CLERK_SECRET_KEY`` or Clerk's
    # own conventional name ``CLERK_SECRET_KEY``. Backend-only secret —
    # never expose it to the SPA.
    clerk_secret_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "VOITTA_CLERK_SECRET_KEY", "CLERK_SECRET_KEY"
        ),
    )
    clerk_api_base: str = "https://api.clerk.com/v1"
    session_secret: str | None = None
    # Cookie lifetime for the signed session. 30 days is the same default the
    # voitta-rag app uses; tune via env when stricter rotation is needed.
    session_max_age_seconds: int = 60 * 60 * 24 * 30
    # Adds the Secure attribute to the session cookie. Must be True in any
    # deployment served over HTTPS. Set False only for local dev over plain
    # http://localhost.
    cookie_secure: bool = True

    # SQLAlchemy connection pool sizing. Defaults (5 / 10) are SQLAlchemy's
    # baked-in QueuePool defaults — too small for our profile, which races
    # the watcher (1–2 sessions per disk event), the worker pool, REST
    # handlers, and WS-driven refreshes against the same pool. A fresh sync
    # that lands ~800 files exhausted 15 connections in production.
    # 30/60 = up to 90 concurrent connections; SQLite + WAL handles that
    # many readers cheaply (file-descriptor cost only — writes still
    # serialize through busy_timeout). Tune via VOITTA_DB_POOL_* env.
    db_pool_size: int = 30
    db_pool_max_overflow: int = 60
    # Recycle idle pool connections so a long-lived read transaction isn't
    # left pinning the WAL from a checkpoint indefinitely. 30 min is short
    # enough that a stuck reader can't keep the WAL ballooning, long
    # enough that connection turnover stays cheap.
    db_pool_recycle_seconds: int = 1800
    # Pre-ping silently replaces a connection that's been killed
    # underneath us (rare on SQLite, but free insurance).
    db_pool_pre_ping: bool = True

    # Test/dev override: when true, the lifespan does not start the watcher
    # or the worker pool. Production leaves this false.
    disable_background: bool = False

    # When true, the embedding factories return deterministic ``Fake*Embedder``
    # implementations that don't load any models. Used in tests; can also be
    # set in dev to avoid the model-download cost.
    use_fake_embedders: bool = False

    @field_validator("data_dir", "db_path", "cas_dir", "qdrant_path", "root_path", mode="before")
    @classmethod
    def _expand_user(cls, v: Any) -> Any:
        if isinstance(v, str) and v:
            return Path(os.path.expanduser(v))
        return v

    @model_validator(mode="after")
    def _check_standalone_url(self) -> "Settings":
        if self.qdrant_mode == "standalone" and not self.qdrant_url:
            raise ValueError(
                "VOITTA_QDRANT_URL is required when VOITTA_QDRANT_MODE=standalone"
            )
        return self

    def resolved_db_path(self) -> Path:
        return self.db_path or (self.data_dir / "voitta.db")

    def resolved_cas_dir(self) -> Path:
        return self.cas_dir or (self.data_dir / "cas")

    def resolved_qdrant_path(self) -> Path:
        return self.qdrant_path or (self.data_dir / "qdrant")

    def resolved_qdrant_managed_dir(self) -> Path:
        """Storage dir for the managed Qdrant subprocess.

        Honors the ``qdrant_path`` override when set; otherwise a dedicated
        dir distinct from the embedded backend's (incompatible on-disk format).
        """
        return self.qdrant_path or (self.data_dir / "qdrant_managed")

    def asset_url(self, token: str) -> str:
        """Build the public URL for a signed asset token.

        With ``VOITTA_PUBLIC_BASE_URL`` set, returns an absolute URL
        the MCP client can fetch directly. Without it, returns the
        path only — fine for local dev where the MCP proxy adds the
        host, but breaks remote clients that don't know our hostname.
        """
        path = f"/api/assets/{token}"
        if not self.public_base_url:
            return path
        return self.public_base_url.rstrip("/") + path

    def resolved_workers(self) -> int:
        # Default to a single worker so indexing is strictly serial; opt
        # into more by setting VOITTA_WORKERS explicitly.
        if self.workers and self.workers > 0:
            return self.workers
        return 1

    def ignore_globs(self) -> list[str]:
        return [g.strip() for g in self.ignore_patterns.split(",") if g.strip()]

    def allowed_domain_list(self) -> list[str]:
        """Legacy. Returns the parsed VOITTA_ALLOWED_DOMAINS env var. Not
        consulted by the sign-in gate — kept for back-compat with any code
        still reading it. New callers should use
        ``services.admin_store.list_allowed_domains()`` instead.
        """
        out: list[str] = []
        for raw in self.allowed_domains.split(","):
            d = raw.strip().lower().lstrip("@")
            if d:
                out.append(d)
        return out

    def super_admin_list(self) -> list[str]:
        """Lowercased emails from VOITTA_SUPER_ADMINS."""
        out: list[str] = []
        for raw in self.super_admins.split(","):
            v = raw.strip().lower()
            if v and "@" in v:
                out.append(v)
        return out

    @property
    def google_auth_enabled(self) -> bool:
        return bool(self.google_auth_client_id and self.google_auth_client_secret)

    @property
    def clerk_enabled(self) -> bool:
        return bool(self.clerk_secret_key)

    def resolved_session_secret(self) -> str:
        """Return the cookie-signing secret, generating + persisting one on
        first use so existing logins survive a restart.

        Generated once per install under ``data_dir/.session_secret``. Treat
        the file like an env-var secret — anyone who can read it can mint
        login cookies.
        """
        if self.session_secret:
            return self.session_secret
        path = self.data_dir / ".session_secret"
        import contextlib
        import secrets

        if path.exists():
            with contextlib.suppress(OSError):
                return path.read_text().strip()
        path.parent.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_urlsafe(64)
        path.write_text(secret)
        with contextlib.suppress(OSError):
            path.chmod(0o600)
        return secret


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Clear the cached Settings instance — used by tests."""
    get_settings.cache_clear()
