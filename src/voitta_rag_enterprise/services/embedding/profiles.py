"""Embedding profiles: which models embed text and images, and where they live.

Two profiles exist:

* ``gemma2`` — EmbeddingGemma-2 embeds text AND images into one 768-d space
  (one model, one instance). Collections ``chunks__gemma2`` / ``images__gemma2``.
  Default for server / Docker installs.
* ``e5-siglip`` — e5-base-v2 for text, SigLIP-2 for images. Collections keep
  their historical unsuffixed names ``chunks`` / ``images``, so pre-profile
  installs need nothing moved. Default for the desktop app.

Both profiles use BM25 (fastembed) for the sparse half of hybrid search.

Resolution order for the active profile, highest first:

1. ``VOITTA_EMBEDDING_PROFILE`` (``Settings.embedding_profile``).
2. The ``embedding_profile`` row in ``app_settings`` — written by
   ``scripts/migrate_embedding_profile.py`` when a migration completes.
3. ``Settings.default_embedding_profile`` (``gemma2``; the desktop launcher
   sets ``e5-siglip``).

Safety rule: all three profiles' models emit 768-d vectors, so Qdrant would
silently accept vectors from the wrong model. ``collection_meta`` records
which profile built each collection and :func:`resolve` checks it:

* the profile came from the default (step 3) but only another profile's
  collections hold data → adopt that profile (an existing install keeps
  working after an upgrade) and say so;
* the profile was chosen explicitly (step 1 or 2) and its collections are
  recorded under another profile → :class:`ProfileMismatchError`. The app
  refuses to start rather than mix vector spaces.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Literal

from ...config import get_settings

logger = logging.getLogger(__name__)

ProfileName = Literal["gemma2", "e5-siglip"]
PROFILE_SETTING_KEY = "embedding_profile"
# Set by the migration while it fills a profile's collections; that profile
# is not usable until the migration completes and clears it.
MIGRATION_SETTING_KEY = "embedding_migration"


@dataclass(frozen=True)
class Profile:
    name: ProfileName
    dense_model: str
    image_model: str
    dense_version: str
    image_version: str
    chunks_collection: str
    images_collection: str

    @property
    def unified(self) -> bool:
        """One model embeds both text and images (one shared embedder)."""
        return self.dense_model == self.image_model


_BASE: dict[str, Profile] = {
    "gemma2": Profile(
        name="gemma2",
        dense_model="google/embeddinggemma-2",
        image_model="google/embeddinggemma-2",
        dense_version="embeddinggemma-2@1",
        image_version="embeddinggemma-2@1",
        chunks_collection="chunks__gemma2",
        images_collection="images__gemma2",
    ),
    "e5-siglip": Profile(
        name="e5-siglip",
        dense_model="intfloat/e5-base-v2",
        image_model="google/siglip2-base-patch16-224",
        dense_version="e5-base-v2@1",
        image_version="siglip2-base@1",
        chunks_collection="chunks",
        images_collection="images",
    ),
}
PROFILE_NAMES: tuple[str, ...] = tuple(_BASE)
# Every Qdrant collection any profile uses — for offline maintenance tools
# that operate on a raw Qdrant store without an app config.
ALL_COLLECTIONS: tuple[str, ...] = tuple(
    n for p in _BASE.values() for n in (p.chunks_collection, p.images_collection)
)


class ProfileMismatchError(RuntimeError):
    """The chosen profile's collections were built by a different profile."""


@dataclass(frozen=True)
class Resolution:
    name: str
    source: Literal["env", "app_settings", "default", "adopted", "pinned"]
    note: str = ""

    @property
    def profile(self) -> Profile:
        # Built on access so VOITTA_* overrides always reflect current settings;
        # only the (expensive, DB + Qdrant) name resolution is cached.
        return profile(self.name)


_resolved: Resolution | None = None
_lock = threading.Lock()


def profile(name: str) -> Profile:
    """The named profile with the ``VOITTA_*`` model/version overrides applied."""
    if name not in _BASE:
        raise ValueError(f"unknown embedding profile {name!r}; expected one of {PROFILE_NAMES}")
    base = _BASE[name]
    s = get_settings()
    dense_model = s.dense_model or base.dense_model
    image_model = s.image_model or base.image_model
    if base.unified and image_model != dense_model:
        # A unified profile shares one model between text and images; letting
        # the two drift apart would put text and images in different spaces
        # under one profile name.
        if s.image_model and s.dense_model:
            raise ValueError(
                f"profile {name} embeds text and images with one model; "
                f"VOITTA_DENSE_MODEL={s.dense_model} and VOITTA_IMAGE_MODEL={s.image_model} differ"
            )
        image_model = dense_model = s.dense_model or s.image_model or base.dense_model
    return Profile(
        name=base.name,
        dense_model=dense_model,
        image_model=image_model,
        dense_version=s.dense_version or base.dense_version,
        image_version=s.image_version or base.image_version,
        chunks_collection=base.chunks_collection,
        images_collection=base.images_collection,
    )


def active() -> Profile:
    """The active profile. Resolves (and checks) on first use."""
    return resolve().profile


def resolve() -> Resolution:
    global _resolved
    if _resolved is None:
        with _lock:
            if _resolved is None:
                _resolved = _resolve()
                r = _resolved
                logger.info(
                    "embedding profile: %s (%s)%s",
                    r.name,
                    r.source,
                    f" — {r.note}" if r.note else "",
                )
    return _resolved


def pin(name: str) -> None:
    """Fix this process's active profile without resolving it.

    For processes that act on one profile's collections by design — the
    migration command writes its target profile's collections while the
    app keeps serving the old ones — and for unit tests that have no
    database. Skips the startup safety check; the app always resolves.
    """
    global _resolved
    if name not in _BASE:
        raise ValueError(f"unknown embedding profile {name!r}")
    with _lock:
        _resolved = Resolution(name, "pinned")


def reset_profile_cache() -> None:
    global _resolved
    with _lock:
        _resolved = None


def _resolve() -> Resolution:
    s = get_settings()
    if s.embedding_profile:
        chosen, source = s.embedding_profile, "env"
    else:
        stored = read_stored_profile()
        if stored:
            chosen, source = stored, "app_settings"
        else:
            chosen, source = s.default_embedding_profile, "default"
    if chosen not in _BASE:
        raise ValueError(f"unknown embedding profile {chosen!r} (from {source})")

    backfill_legacy_collections()
    built = _populated_collections_by_profile()
    migrating = read_setting(MIGRATION_SETTING_KEY)
    if migrating:
        if chosen == migrating and source != "default":
            raise ProfileMismatchError(
                f"embedding profile {chosen} ({_source_hint(source)}) is still being "
                f"migrated to; finish it with scripts/migrate_embedding_profile.py "
                f"--to {chosen}, or unset the profile until then"
            )
        # Half-filled collections must not look like a usable profile.
        built[migrating] = 0
    mine = profile(chosen)
    own = {mine.chunks_collection, mine.images_collection}
    foreign = {
        name: prof for name, prof in _recorded_profiles().items()
        if name in own and prof != chosen
    }
    if foreign:
        # Our collection names carry the profile, so this only happens when a
        # collection was created by hand or a profile's names were changed.
        raise ProfileMismatchError(
            f"embedding profile {chosen} ({source}) would use collections "
            f"{sorted(foreign)} that collection_meta records as built by "
            f"{sorted(set(foreign.values()))}"
        )
    if built.get(chosen):
        return Resolution(chosen, source)
    others = sorted(p for p, n in built.items() if n and p != chosen)
    if not others:
        return Resolution(chosen, source)  # nothing indexed anywhere yet
    if source == "default":
        adopted = others[0]
        return Resolution(
            adopted,
            "adopted",
            f"default is {chosen}, but only {adopted} collections hold data; "
            f"running {adopted}. Migrate with scripts/migrate_embedding_profile.py --to {chosen}",
        )
    raise ProfileMismatchError(
        f"embedding profile {chosen} was set explicitly ({_source_hint(source)}) but its "
        f"collections are empty while {', '.join(others)} collections hold data. Either set "
        f"VOITTA_EMBEDDING_PROFILE={others[0]}, or migrate the data first: "
        f"python scripts/migrate_embedding_profile.py --to {chosen}"
    )


def _source_hint(source: str) -> str:
    return {
        "env": "VOITTA_EMBEDDING_PROFILE",
        "app_settings": "app_settings row written by a migration",
    }.get(source, source)


# ---------------------------------------------------------------------------
# persistence (SQLite) and Qdrant inspection
# ---------------------------------------------------------------------------


def read_stored_profile() -> str | None:
    return read_setting(PROFILE_SETTING_KEY)


def write_stored_profile(name: str) -> None:
    if name not in _BASE:
        raise ValueError(f"unknown embedding profile {name!r}")
    write_setting(PROFILE_SETTING_KEY, name)


def read_setting(key: str) -> str | None:
    from ...db.database import session_scope
    from ...db.models import AppSetting

    with session_scope() as s:
        row = s.get(AppSetting, key)
        return row.value if row else None


def write_setting(key: str, value: str | None) -> None:
    """Upsert ``key``; ``None`` deletes it."""
    from ...db.database import session_scope
    from ...db.models import AppSetting

    with session_scope() as s:
        row = s.get(AppSetting, key)
        if value is None:
            if row is not None:
                s.delete(row)
        elif row is None:
            s.add(AppSetting(key=key, value=value, updated_at=int(time.time())))
        else:
            row.value, row.updated_at = value, int(time.time())


def record_collection(name: str, prof: Profile, *, dim: int, kind: Literal["chunks", "images"]) -> None:
    """Record that ``prof`` built collection ``name`` (idempotent)."""
    from ...db.database import session_scope
    from ...db.models import CollectionMeta

    with session_scope() as s:
        if s.get(CollectionMeta, name) is not None:
            return
        s.add(
            CollectionMeta(
                collection_name=name,
                profile=prof.name,
                dense_model=prof.dense_model if kind == "chunks" else None,
                image_model=prof.image_model if kind == "images" else None,
                dim=dim,
                created_at=int(time.time()),
            )
        )


def _recorded_profiles() -> dict[str, str]:
    from sqlalchemy import select

    from ...db.database import session_scope
    from ...db.models import CollectionMeta

    with session_scope() as s:
        return dict(s.execute(select(CollectionMeta.collection_name, CollectionMeta.profile)).all())


def backfill_legacy_collections() -> None:
    """Pre-profile installs have unsuffixed ``chunks``/``images`` collections
    with no ``collection_meta`` row; they were built by e5 + SigLIP."""
    from ..vector_store import collection_dims

    legacy = _BASE["e5-siglip"]
    recorded = _recorded_profiles()
    dims = collection_dims([legacy.chunks_collection, legacy.images_collection])
    for name, kind in ((legacy.chunks_collection, "chunks"), (legacy.images_collection, "images")):
        if name in dims and name not in recorded:
            record_collection(name, legacy, dim=dims[name], kind=kind)
            logger.info("collection_meta: backfilled %s as e5-siglip", name)


def _populated_collections_by_profile() -> dict[str, int]:
    """Points per profile, summed over its two collections (0 if absent)."""
    from ..vector_store import collection_point_counts

    counts = collection_point_counts(list(_BASE_BY_COLLECTION))
    out: dict[str, int] = {name: 0 for name in _BASE}
    for coll, n in counts.items():
        out[_BASE_BY_COLLECTION[coll]] += n
    return out


_BASE_BY_COLLECTION = {
    n: p.name for p in _BASE.values() for n in (p.chunks_collection, p.images_collection)
}
