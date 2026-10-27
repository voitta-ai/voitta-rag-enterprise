"""Embedding profiles: which models embed text and images, and where they live.

Two profiles exist:

* ``gemma2`` — EmbeddingGemma-2 embeds text AND images into one 768-d space
  (one model, one instance). Collections ``chunks__gemma2`` / ``images__gemma2``.
  Default for server / Docker installs.
* ``e5-siglip`` — e5-base-v2 for text, SigLIP-2 for images. Collections keep
  their historical unsuffixed names ``chunks`` / ``images``, so pre-profile
  installs need nothing moved. Default for the desktop app.

Both profiles use BM25 (fastembed) for the sparse half of hybrid search.

The deployment's profile is decided once and recorded. ``app_settings`` row
``embedding_profile`` holds the profile this deployment serves:

* on the first start of an install (no row yet) the app picks
  ``VOITTA_EMBEDDING_PROFILE`` if set, else ``e5-siglip`` when SQL already
  holds indexed content (every install from before profiles existed is
  e5 + SigLIP — this holds even if its Qdrant data was lost), else
  ``Settings.default_embedding_profile`` (``gemma2``; the desktop launcher
  sets ``e5-siglip``), and records it (:func:`commit`);
* afterwards only ``scripts/migrate_embedding_profile.py --finish`` changes
  it. ``VOITTA_EMBEDDING_PROFILE`` must then agree with the row; a
  different value refuses to start rather than serve collections nobody
  has kept up to date.

Safety rule: all profiles' models emit 768-d vectors, so Qdrant would
silently accept vectors from the wrong model. ``collection_meta`` records
the profile, model and dimension that built each collection; resolution
refuses to start when the active profile's collections were built by
anything else, and fails outright when Qdrant cannot be read (an empty
view of the store must never decide which vector space to serve).

``VOITTA_DENSE_MODEL`` / ``VOITTA_IMAGE_MODEL`` / ``VOITTA_DENSE_VERSION`` /
``VOITTA_IMAGE_VERSION`` predate profiles and configure ``e5-siglip`` only;
``gemma2`` ignores them (with a warning) so an old ``.env`` cannot put e5
weights or version stamps into the Gemma collections.
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
# Every install from before profiles existed embedded with e5 + SigLIP.
LEGACY_PROFILE = "e5-siglip"
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
    """The named profile; ``e5-siglip`` with the legacy ``VOITTA_*`` overrides applied."""
    if name not in _BASE:
        raise ValueError(f"unknown embedding profile {name!r}; expected one of {PROFILE_NAMES}")
    base = _BASE[name]
    if name != LEGACY_PROFILE:
        _warn_ignored_overrides(name)
        return base
    s = get_settings()
    return Profile(
        name=base.name,
        dense_model=s.dense_model or base.dense_model,
        image_model=s.image_model or base.image_model,
        dense_version=s.dense_version or base.dense_version,
        image_version=s.image_version or base.image_version,
        chunks_collection=base.chunks_collection,
        images_collection=base.images_collection,
    )


_warned_overrides: set[str] = set()


def _warn_ignored_overrides(name: str) -> None:
    s = get_settings()
    present = {
        k: v
        for k, v in (
            ("VOITTA_DENSE_MODEL", s.dense_model),
            ("VOITTA_IMAGE_MODEL", s.image_model),
            ("VOITTA_DENSE_VERSION", s.dense_version),
            ("VOITTA_IMAGE_VERSION", s.image_version),
        )
        if v
    }
    key = f"{name}:{sorted(present.items())}"
    if present and key not in _warned_overrides:
        _warned_overrides.add(key)
        logger.warning(
            "embedding profile %s ignores %s (they configure the %s profile only)",
            name, ", ".join(sorted(present)), LEGACY_PROFILE,
        )


def active() -> Profile:
    """The active profile. Resolves (and checks) on first use."""
    return resolve().profile


def resolve() -> Resolution:
    global _resolved
    if _resolved is None:
        from ..vector_store import on_qdrant_thread

        if on_qdrant_thread():
            # Resolution itself queries Qdrant through the one-worker pool;
            # starting it from that worker would deadlock. Every entry point
            # resolves before it touches Qdrant, so this is a programming error.
            raise RuntimeError("embedding profile used on the Qdrant thread before it was resolved")
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


def commit() -> None:
    """Record the resolved profile as this deployment's, if none is recorded yet.

    Called by the app once startup has resolved it. From then on the
    profile cannot drift with the environment or with what Qdrant happens
    to contain; only a finished migration changes it.
    """
    r = resolve()
    if r.source not in ("app_settings", "pinned") and read_stored_profile() is None:
        write_stored_profile(r.name)
        logger.info("embedding profile %s recorded for this deployment", r.name)


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
    env = s.embedding_profile or None
    if env is not None and env not in _BASE:
        raise ValueError(f"unknown embedding profile {env!r} (VOITTA_EMBEDDING_PROFILE)")
    recorded = read_stored_profile()
    migrating = read_setting(MIGRATION_SETTING_KEY)
    # Qdrant must be readable: this raises on connection errors.
    backfill_legacy_collections()

    if recorded is not None:
        if env is not None and env != recorded:
            raise ProfileMismatchError(
                f"this deployment serves embedding profile {recorded}, but "
                f"VOITTA_EMBEDDING_PROFILE={env}. Switching profiles needs its data: "
                f"python scripts/migrate_embedding_profile.py --to {env} (then --finish "
                f"with the app stopped). Or set VOITTA_EMBEDDING_PROFILE={recorded} / unset it."
            )
        res = Resolution(recorded, "app_settings")
    elif _sql_has_indexed_content():
        # No record but indexed content: an install from before profiles
        # existed, which can only be e5 + SigLIP.
        built = _profiles_with_collections()
        if built - {LEGACY_PROFILE}:
            raise ProfileMismatchError(
                f"indexed content exists but no profile is recorded and collections of "
                f"{sorted(built)} exist; finish a migration with "
                f"python scripts/migrate_embedding_profile.py --to <profile> --finish"
            )
        if env is not None and env != LEGACY_PROFILE:
            raise ProfileMismatchError(
                f"VOITTA_EMBEDDING_PROFILE={env}, but this install's indexed content was "
                f"embedded with {LEGACY_PROFILE}. Migrate it first: "
                f"python scripts/migrate_embedding_profile.py --to {env} (then --finish "
                f"with the app stopped), or set VOITTA_EMBEDDING_PROFILE={LEGACY_PROFILE}."
            )
        default = s.default_embedding_profile
        if env is not None:
            res = Resolution(LEGACY_PROFILE, "env")
        elif default == LEGACY_PROFILE:
            res = Resolution(LEGACY_PROFILE, "default")
        else:
            res = Resolution(
                LEGACY_PROFILE,
                "adopted",
                f"default is {default}, but this install's content was embedded with "
                f"{LEGACY_PROFILE}; serving {LEGACY_PROFILE}. Move with "
                f"scripts/migrate_embedding_profile.py --to {default}",
            )
    else:
        name = env or s.default_embedding_profile
        if name not in _BASE:
            raise ValueError(f"unknown embedding profile {name!r} (default)")
        res = Resolution(name, "env" if env else "default")

    if migrating is not None and migrating == res.name:
        # --finish records the target and clears the mark in one
        # transaction, so this means the mark was left by hand.
        raise ProfileMismatchError(
            f"embedding profile {res.name} is marked as still being migrated to; "
            f"run python scripts/migrate_embedding_profile.py --to {res.name} --finish"
        )
    _check_collections_match(res.name)
    return res


def _check_collections_match(name: str) -> None:
    """Refuse when the profile's collections were built by another
    profile, model or vector size (``collection_meta`` vs the profile and
    what Qdrant reports)."""
    from sqlalchemy import select

    from ...db.database import session_scope
    from ...db.models import CollectionMeta
    from ..vector_store import collection_dims

    prof = profile(name)
    want = {
        prof.chunks_collection: prof.dense_model,
        prof.images_collection: prof.image_model,
    }
    dims = collection_dims(list(want))
    with session_scope() as s:
        rows = {
            m.collection_name: (m.profile, m.dense_model or m.image_model, m.dim)
            for m in s.execute(
                select(CollectionMeta).where(CollectionMeta.collection_name.in_(list(want)))
            ).scalars()
        }
    problems = []
    for coll, model in want.items():
        if coll not in rows:
            continue
        built_profile, built_model, dim = rows[coll]
        if built_profile != name:
            problems.append(f"{coll} was built by profile {built_profile}")
        elif built_model != model:
            problems.append(f"{coll} was built with {built_model}, profile {name} now uses {model}")
        elif coll in dims and dims[coll] != dim:
            problems.append(f"{coll} has {dims[coll]}-d vectors, collection_meta records {dim}")
    if problems:
        raise ProfileMismatchError(
            f"embedding profile {name} cannot use its collections: " + "; ".join(problems)
            + ". Vectors from different models must not share a collection."
        )


def _sql_has_indexed_content() -> bool:
    from sqlalchemy import select

    from ...db.database import session_scope
    from ...db.models import Chunk, Image

    with session_scope() as s:
        return (
            s.execute(select(Chunk.id).limit(1)).first() is not None
            or s.execute(select(Image.id).where(Image.kind == "figure").limit(1)).first()
            is not None
        )


def _profiles_with_collections() -> set[str]:
    """Profiles that have at least one collection present in Qdrant."""
    from ..vector_store import collection_dims

    return {_BASE_BY_COLLECTION[c] for c in collection_dims(list(_BASE_BY_COLLECTION))}


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
    write_settings({key: value})


def write_settings(values: dict[str, str | None]) -> None:
    """Upsert several keys in ONE transaction (``None`` deletes a key)."""
    from ...db.database import session_scope
    from ...db.models import AppSetting

    now = int(time.time())
    with session_scope() as s:
        for key, value in values.items():
            row = s.get(AppSetting, key)
            if value is None:
                if row is not None:
                    s.delete(row)
            elif row is None:
                s.add(AppSetting(key=key, value=value, updated_at=now))
            else:
                row.value, row.updated_at = value, now


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


_BASE_BY_COLLECTION = {
    n: p.name for p in _BASE.values() for n in (p.chunks_collection, p.images_collection)
}
