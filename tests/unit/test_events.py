"""Tests for the event broker."""

from __future__ import annotations

import asyncio
import threading

import pytest

from voitta_rag_enterprise.services import events


async def _drain_one(sub: events.Subscription, timeout: float = 1.0) -> dict:
    await sub.wait(timeout=timeout)
    items = sub.drain()
    assert items, "no events delivered before timeout"
    return items[0]


def test_publish_with_no_loop_is_noop(env: None) -> None:
    events.publish("files", {"type": "x"})  # nothing installed; should not raise


@pytest.mark.asyncio
async def test_subscribe_and_receive_event(env: None) -> None:
    events.install_loop(asyncio.get_running_loop())
    try:
        async with events.subscribe(["files"]) as sub:
            assert events.topic_subscriber_count("files") == 1
            events.publish("files", {"type": "file.upserted", "file": {"id": 1}})
            event = await _drain_one(sub)
            assert event == {"type": "file.upserted", "file": {"id": 1}}
        assert events.topic_subscriber_count("files") == 0
    finally:
        events.uninstall_loop()


@pytest.mark.asyncio
async def test_publish_to_other_topic_is_ignored(env: None) -> None:
    events.install_loop(asyncio.get_running_loop())
    try:
        async with events.subscribe(["files"]) as sub:
            events.publish("jobs", {"type": "job.started"})
            assert await sub.wait(timeout=0.05) is False
            assert sub.drain() == []
    finally:
        events.uninstall_loop()


@pytest.mark.asyncio
async def test_multiple_subscribers_receive_same_event(env: None) -> None:
    events.install_loop(asyncio.get_running_loop())
    try:
        async with events.subscribe(["jobs"]) as a, events.subscribe(["jobs"]) as b:
            events.publish("jobs", {"type": "job.started", "job_id": 7})
            ea = await _drain_one(a)
            eb = await _drain_one(b)
            assert ea == eb == {"type": "job.started", "job_id": 7}
    finally:
        events.uninstall_loop()


@pytest.mark.asyncio
async def test_publish_from_other_thread(env: None) -> None:
    events.install_loop(asyncio.get_running_loop())
    try:
        async with events.subscribe(["files"]) as sub:
            t = threading.Thread(
                target=events.publish,
                args=("files", {"type": "file.upserted", "file": {"id": 9}}),
            )
            t.start()
            t.join()
            event = await _drain_one(sub)
            assert event["type"] == "file.upserted"
    finally:
        events.uninstall_loop()


@pytest.mark.asyncio
async def test_file_upserted_coalesces_per_file_id(env: None) -> None:
    """30 file.upserted events for one file should drain as one entry —
    only the latest snapshot matters to the UI."""
    events.install_loop(asyncio.get_running_loop())
    try:
        async with events.subscribe(["files"]) as sub:
            for i in range(30):
                events.publish(
                    "files",
                    {
                        "type": "file.upserted",
                        "file": {"id": 1, "pending_embeds": 30 - i},
                    },
                )
            # Let the loop drain the call_soon_threadsafe callbacks.
            await sub.wait(timeout=1.0)
            items = sub.drain()
            assert len(items) == 1
            assert items[0]["file"]["pending_embeds"] == 1  # latest wins
            assert sub.stats["published"] == 30
            assert sub.stats["delivered"] == 1
    finally:
        events.uninstall_loop()


@pytest.mark.asyncio
async def test_folder_stats_changed_coalesces_per_folder_id(env: None) -> None:
    """A burst of stats updates on the same folder collapses to one
    delivered event with the freshest counts. The events broker is
    what makes per-commit publishing cheap enough to fire from the
    indexer's hot path."""
    events.install_loop(asyncio.get_running_loop())
    try:
        async with events.subscribe(["folders"]) as sub:
            for i in range(50):
                events.publish(
                    "folders",
                    {
                        "type": "folder.stats_changed",
                        "folder_id": 7,
                        "stats": {"chunks_total": i},
                    },
                )
            await sub.wait(timeout=1.0)
            items = sub.drain()
            assert len(items) == 1
            assert items[0]["stats"]["chunks_total"] == 49  # latest wins
    finally:
        events.uninstall_loop()


@pytest.mark.asyncio
async def test_folder_stats_changed_keeps_distinct_folders_separate(env: None) -> None:
    events.install_loop(asyncio.get_running_loop())
    try:
        async with events.subscribe(["folders"]) as sub:
            for fid in range(3):
                events.publish(
                    "folders",
                    {
                        "type": "folder.stats_changed",
                        "folder_id": fid,
                        "stats": {"chunks_total": fid},
                    },
                )
            await sub.wait(timeout=1.0)
            items = sub.drain()
            ids = {e["folder_id"] for e in items}
            assert ids == {0, 1, 2}
    finally:
        events.uninstall_loop()


@pytest.mark.asyncio
async def test_distinct_files_are_not_coalesced(env: None) -> None:
    events.install_loop(asyncio.get_running_loop())
    try:
        async with events.subscribe(["files"]) as sub:
            for fid in range(5):
                events.publish(
                    "files", {"type": "file.upserted", "file": {"id": fid}}
                )
            await sub.wait(timeout=1.0)
            items = sub.drain()
            assert {e["file"]["id"] for e in items} == {0, 1, 2, 3, 4}
    finally:
        events.uninstall_loop()


@pytest.mark.asyncio
async def test_discrete_events_appended_each_time(env: None) -> None:
    """folder.added/removed/file.deleted are not snapshots — every publish
    must be delivered."""
    events.install_loop(asyncio.get_running_loop())
    try:
        async with events.subscribe(["files"]) as sub:
            for i in range(3):
                events.publish("files", {"type": "file.deleted", "file_id": i})
            await sub.wait(timeout=1.0)
            items = sub.drain()
            assert len(items) == 3
    finally:
        events.uninstall_loop()


def test_event_folder_id_extraction() -> None:
    """Every folder-scoped event shape resolves to its folder id; global
    events resolve to None (delivered to everyone)."""
    f = events._event_folder_id
    assert f({"type": "file.upserted", "file": {"folder_id": 5}}) == 5
    assert f({"type": "folder.added", "folder": {"id": 9}}) == 9
    assert f({"type": "folder.upserted", "folder": {"id": 9}}) == 9
    assert f({"type": "folder.removed", "folder_id": 3}) == 3
    assert f({"type": "folder.stats_changed", "folder_id": 3}) == 3
    assert f({"type": "file.deleted", "file_id": 1, "folder_id": 4}) == 4
    assert f({"type": "job.finished", "job_id": 1, "folder_id": 8}) == 8
    # Global / unscoped — no folder id.
    assert f({"type": "job.finished", "job_id": 1, "folder_id": None}) is None
    assert f({"type": "job.started", "job_id": 1}) is None


def test_event_visible_filters_by_folder() -> None:
    sub = events.Subscription(["files"], user_id=1, is_admin=False, visible={5, 6})
    assert sub.event_visible({"type": "file.upserted", "file": {"folder_id": 5}})
    assert not sub.event_visible({"type": "file.upserted", "file": {"folder_id": 9}})
    # Job in a visible folder passes; in an invisible one is dropped.
    assert sub.event_visible({"type": "job.finished", "job_id": 1, "folder_id": 6})
    assert not sub.event_visible({"type": "job.finished", "job_id": 1, "folder_id": 9})
    # Unscoped/global events are always delivered.
    assert sub.event_visible({"type": "job.finished", "job_id": 1, "folder_id": None})


def test_event_visible_admin_and_single_user_see_everything() -> None:
    # visible=None models admin / single-user: no filtering at all.
    sub = events.Subscription(["files"], user_id=2, is_admin=True, visible=None)
    assert sub.event_visible({"type": "file.upserted", "file": {"folder_id": 999}})
    assert sub.event_visible({"type": "folder.removed", "folder_id": 999})


def test_bump_acl_version_increments() -> None:
    before = events.acl_version()
    events.bump_acl_version()
    assert events.acl_version() == before + 1


def test_structural_folder_events_bump_acl_version(env: None) -> None:
    """folder.added / folder.removed invalidate every connection's cached
    visible set so the WS pump recomputes it."""
    before = events.acl_version()
    events.publish("folders", {"type": "folder.added", "folder": {"id": 1}})
    events.publish("folders", {"type": "folder.removed", "folder_id": 1})
    assert events.acl_version() == before + 2
    # A non-structural folder event must NOT bump.
    steady = events.acl_version()
    events.publish("folders", {"type": "folder.stats_changed", "folder_id": 1})
    assert events.acl_version() == steady


@pytest.mark.asyncio
async def test_uninstall_clears_topics(env: None) -> None:
    events.install_loop(asyncio.get_running_loop())
    sub = events.Subscription(["files"])
    sub.attach()
    assert events.topic_subscriber_count("files") == 1
    events.uninstall_loop()
    assert events.topic_subscriber_count("files") == 0
