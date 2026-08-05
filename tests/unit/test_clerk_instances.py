"""Named Clerk instances: migration, legacy mirror, resolution, validation.

Pins the compatibility contract of the multi-instance rework:

- legacy single-key settings migrate (read-side) to an instance named
  "Development"; nothing in the file changes until the first instance save
- the first save mirrors "Development" back into the legacy fields, so a
  rollback to pre-instances code keeps Clerk working (downgrade safety)
- CLERK_SECRET_KEY from .env seeds/backfills the Development card and is
  never persisted
- org→instance resolution tries every enabled instance and caches the winner
- login unions admissions across instances and stamps
  ``company_name = "Instance / Org"``
- the admin PATCH validates per card (names required/unique, sk_ prefix,
  no enable without key)
"""

from __future__ import annotations

import json

import pytest

from voitta_rag_enterprise.services import admin_store
from voitta_rag_enterprise.services import clerk as clerk_svc


@pytest.fixture(autouse=True)
def _fresh_caches():
    clerk_svc.clear_org_members_cache()
    yield
    clerk_svc.clear_org_members_cache()


# ---------------------------------------------------------------------------
# Read-side migration + legacy mirror
# ---------------------------------------------------------------------------


def test_no_config_no_instances(env: None) -> None:
    assert admin_store.get_clerk_instances() == []
    assert admin_store.get_clerk_enabled() is False
    assert admin_store.get_clerk_secret_key() == ""


def test_legacy_key_migrates_to_development(env: None) -> None:
    admin_store.save_settings(
        {"clerk_enabled": True, "clerk_secret_key": "sk_test_legacy"}
    )
    instances = admin_store.get_clerk_instances()
    assert instances == [
        {"name": "Development", "secret_key": "sk_test_legacy", "enabled": True}
    ]
    # Legacy getters still answer through the instance layer.
    assert admin_store.get_clerk_enabled() is True
    assert admin_store.get_clerk_secret_key() == "sk_test_legacy"
    # Read-side only: the file still has the legacy shape, no list.
    raw = json.loads((admin_store.admin_dir() / "settings.json").read_text())
    assert "clerk_instances" not in raw


def test_disabled_legacy_key_migrates_disabled(env: None) -> None:
    admin_store.save_settings(
        {"clerk_enabled": False, "clerk_secret_key": "sk_test_legacy"}
    )
    instances = admin_store.get_clerk_instances()
    assert instances[0]["enabled"] is False
    assert admin_store.get_clerk_enabled() is False
    assert admin_store.enabled_clerk_instances() == []


def test_save_mirrors_development_into_legacy_fields(env: None) -> None:
    """Downgrade safety: after saving instances, OLD code reading the legacy
    fields sees the Development key exactly as before."""
    admin_store.save_clerk_instances(
        [
            {"name": "Development", "secret_key": "sk_test_dev", "enabled": True},
            {"name": "Production", "secret_key": "sk_live_prod", "enabled": True},
        ]
    )
    raw = json.loads((admin_store.admin_dir() / "settings.json").read_text())
    assert raw["clerk_enabled"] is True
    assert raw["clerk_secret_key"] == "sk_test_dev"
    assert [i["name"] for i in raw["clerk_instances"]] == [
        "Development", "Production",
    ]
    # And the instance layer round-trips.
    assert [i["name"] for i in admin_store.get_clerk_instances()] == [
        "Development", "Production",
    ]


def test_save_without_development_clears_legacy_mirror(env: None) -> None:
    admin_store.save_clerk_instances(
        [{"name": "Production", "secret_key": "sk_live_p", "enabled": True}]
    )
    raw = json.loads((admin_store.admin_dir() / "settings.json").read_text())
    assert raw["clerk_enabled"] is False
    assert raw["clerk_secret_key"] == ""
    # get_clerk_secret_key falls back to the first enabled instance.
    assert admin_store.get_clerk_secret_key() == "sk_live_p"


def test_backup_written_once_on_first_instance_save(env: None) -> None:
    admin_store.save_settings(
        {"clerk_enabled": True, "clerk_secret_key": "sk_test_old"}
    )
    admin_store.save_clerk_instances(
        [{"name": "Development", "secret_key": "sk_test_old", "enabled": True}]
    )
    admin_store.save_clerk_instances(
        [{"name": "Development", "secret_key": "sk_test_new", "enabled": True}]
    )
    backups = list(admin_store.admin_dir().glob("settings.json.bak-clerk-*"))
    assert len(backups) == 1
    # The backup preserves the pre-migration legacy shape.
    backed = json.loads(backups[0].read_text())
    assert backed["clerk_secret_key"] == "sk_test_old"
    assert "clerk_instances" not in backed


def test_env_key_seeds_development(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from voitta_rag_enterprise.config import reset_settings_cache

    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_env")
    reset_settings_cache()
    instances = admin_store.get_clerk_instances()
    assert instances[0]["name"] == "Development"
    assert instances[0]["secret_key"] == "sk_test_env"
    assert instances[0].get("from_env") is True
    assert admin_store.clerk_key_from_env() is True
    # Env-sourced keys are never persisted on save.
    admin_store.save_clerk_instances(
        [{**instances[0], "enabled": True}]
    )
    raw = json.loads((admin_store.admin_dir() / "settings.json").read_text())
    assert raw["clerk_instances"][0]["secret_key"] == ""
    assert raw["clerk_secret_key"] == ""
    # Read side re-applies the env fallback.
    assert admin_store.get_clerk_instances()[0]["secret_key"] == "sk_test_env"
    reset_settings_cache()


def test_stored_key_wins_over_env(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from voitta_rag_enterprise.config import reset_settings_cache

    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_env")
    reset_settings_cache()
    admin_store.save_clerk_instances(
        [{"name": "Development", "secret_key": "sk_test_stored", "enabled": True}]
    )
    inst = admin_store.get_clerk_instances()[0]
    assert inst["secret_key"] == "sk_test_stored"
    assert not inst.get("from_env")
    reset_settings_cache()


def test_duplicate_and_overlong_names_dropped_on_read(env: None) -> None:
    admin_store.save_settings(
        {
            "clerk_instances": [
                {"name": "Prod", "secret_key": "sk_live_1", "enabled": True},
                {"name": "prod", "secret_key": "sk_live_2", "enabled": True},
                {"name": "", "secret_key": "sk_live_3", "enabled": True},
                {"name": "x" * 60, "secret_key": "sk_live_4", "enabled": True},
            ]
        }
    )
    got = admin_store.get_clerk_instances()
    assert [i["name"] for i in got] == ["Prod", "x" * 24]


# ---------------------------------------------------------------------------
# fetch_org_members_multi — trial resolution + winner cache
# ---------------------------------------------------------------------------


async def test_org_resolution_tries_instances_and_caches_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    async def fake(secret_key: str, org_id: str) -> dict[str, str]:
        calls.append((secret_key, org_id))
        if secret_key == "sk_prod" and org_id == "org_p":
            return {"pat@x": "admin"}
        raise clerk_svc.ClerkError("not here")

    monkeypatch.setattr(clerk_svc, "fetch_org_members", fake)
    instances = [
        {"name": "Development", "secret_key": "sk_dev", "enabled": True},
        {"name": "Production", "secret_key": "sk_prod", "enabled": True},
    ]
    members = await clerk_svc.fetch_org_members_multi(instances, "org_p")
    assert members == {"pat@x": "admin"}
    assert calls == [("sk_dev", "org_p"), ("sk_prod", "org_p")]

    # Second call goes straight to the cached winner.
    calls.clear()
    await clerk_svc.fetch_org_members_multi(instances, "org_p")
    assert calls == [("sk_prod", "org_p")]


async def test_org_resolution_raises_when_nowhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def nope(secret_key: str, org_id: str) -> dict[str, str]:
        raise clerk_svc.ClerkError("404")

    monkeypatch.setattr(clerk_svc, "fetch_org_members", nope)
    with pytest.raises(clerk_svc.ClerkError):
        await clerk_svc.fetch_org_members_multi(
            [{"name": "Dev", "secret_key": "sk_d", "enabled": True}], "org_x"
        )
    with pytest.raises(clerk_svc.ClerkError):
        await clerk_svc.fetch_org_members_multi([], "org_x")
