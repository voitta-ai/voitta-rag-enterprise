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

    # Workers (None → cpu_count at resolve time)
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
    ignore_patterns: str = (
        ".git,node_modules,.DS_Store,__pycache__,.venv,*.tmp,"
        ".git-repo,.voitta_timestamps.json,.voitta_sync.lock"
    )

    # PDF parsing (MinerU)
    pdf_pages_per_bucket: int = 20
    pdf_parse_method: str = "auto"  # MinerU parse_method: auto|txt|ocr
    pdf_lang: str = "en"
    # Test override: when true, the PDF parser returns a deterministic stub
    # without invoking MinerU. Used by the test suite to keep runs fast.
    use_fake_pdf_parser: bool = False

    # Auth — see ARCHITECTURE.md §9
    single_user: bool = False
    dev_user: str | None = None
    users_file: Path = Path("users.txt")

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
        if self.workers and self.workers > 0:
            return self.workers
        return os.cpu_count() or 1

    def ignore_globs(self) -> list[str]:
        return [g.strip() for g in self.ignore_patterns.split(",") if g.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Clear the cached Settings instance — used by tests."""
    get_settings.cache_clear()
