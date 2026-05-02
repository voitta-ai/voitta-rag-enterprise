"""Application settings loaded from the environment.

Mirrors ARCHITECTURE.md §11. All env vars carry the ``VOITTA_`` prefix.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_data_dir() -> Path:
    return Path(os.path.expanduser("~/.voitta-image-rag"))


class Settings(BaseSettings):
    """Environment-driven settings. See ARCHITECTURE.md §11."""

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
    qdrant_url: str | None = None
    qdrant_path: Path | None = None

    # Optional parent for "managed" folders: folders the API itself creates,
    # which can later have sync connectors attached. Folders registered by
    # absolute path are "external" and never get syncs.
    root_path: Path | None = None

    # Network
    port: int = 8000
    mcp_port: int = 8001

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
        ".git,node_modules,.DS_Store,__pycache__,.venv,*.tmp,*.part,"
        ".git-repo,.voitta_timestamps.json,.voitta_sync.lock,"
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
    # Test override: when true, the PDF parser returns a deterministic stub
    # without invoking MinerU. Used by the test suite to keep runs fast.
    use_fake_pdf_parser: bool = False

    # Auth — see ARCHITECTURE.md §9
    single_user: bool = False
    dev_user: str | None = None
    users_file: Path = Path("users.txt")

    # "Sign in with Google" — when both ``google_auth_client_id`` and
    # ``google_auth_client_secret`` are set the API exposes
    # ``/api/auth/login/google`` and the UI renders a sign-in button. The
    # signed session cookie carries the email; ``current_user`` reads it.
    # ``session_secret`` is used to sign the cookie. If unset, a stable random
    # secret is generated and persisted under ``data_dir`` so existing logins
    # survive restarts.
    google_auth_client_id: str | None = None
    google_auth_client_secret: str | None = None
    session_secret: str | None = None
    # Cookie lifetime for the signed session. 30 days is the same default the
    # voitta-rag app uses; tune via env when stricter rotation is needed.
    session_max_age_seconds: int = 60 * 60 * 24 * 30

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

    def resolved_db_path(self) -> Path:
        return self.db_path or (self.data_dir / "voitta.db")

    def resolved_cas_dir(self) -> Path:
        return self.cas_dir or (self.data_dir / "cas")

    def resolved_qdrant_path(self) -> Path:
        return self.qdrant_path or (self.data_dir / "qdrant")

    def resolved_workers(self) -> int:
        # Default to a single worker so indexing is strictly serial; opt
        # into more by setting VOITTA_WORKERS explicitly.
        if self.workers and self.workers > 0:
            return self.workers
        return 1

    def ignore_globs(self) -> list[str]:
        return [g.strip() for g in self.ignore_patterns.split(",") if g.strip()]

    @property
    def google_auth_enabled(self) -> bool:
        return bool(self.google_auth_client_id and self.google_auth_client_secret)

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
