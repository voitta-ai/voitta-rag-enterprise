"""Tests for the event broker."""

from __future__ import annotations

import asyncio
import threading

import pytest

from voitta_image_rag.services import events


def test_publish_with_no_loop_is_noop(env: None) -> None:
    events.publish("files", {"type": "x"})  # nothing installed; should not raise


@pytest.mark.asyncio
async def test_subscribe_and_receive_event(env: None) -> None:
    events.install_loop(asyncio.get_running_loop())
    try:
        async with events.subscribe(["files"]) as sub:
            assert events.topic_subscriber_count("files") == 1
            events.publish("files", {"type": "file.upserted", "file": {"id": 1}})
            event = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
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
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(sub.queue.get(), timeout=0.05)
    finally:
        events.uninstall_loop()


@pytest.mark.asyncio
async def test_multiple_subscribers_receive_same_event(env: None) -> None:
    events.install_loop(asyncio.get_running_loop())
    try:
        async with events.subscribe(["jobs"]) as a, events.subscribe(["jobs"]) as b:
            events.publish("jobs", {"type": "job.started", "job_id": 7})
            ea = await asyncio.wait_for(a.queue.get(), timeout=1.0)
            eb = await asyncio.wait_for(b.queue.get(), timeout=1.0)
            assert ea == eb == {"type": "job.started", "job_id": 7}
    finally:
        events.uninstall_loop()


@pytest.mark.asyncio
async def test_publish_from_other_thread(env: None) -> None:
    events.install_loop(asyncio.get_running_loop())
    try:
        async with events.subscribe(["files"]) as sub:
            t = threading.Thread(
                target=events.publish, args=("files", {"type": "file.upserted"})
            )
            t.start()
            t.join()
            event = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            assert event["type"] == "file.upserted"
    finally:
        events.uninstall_loop()


@pytest.mark.asyncio
async def test_uninstall_clears_topics(env: None) -> None:
    events.install_loop(asyncio.get_running_loop())
    sub = events.Subscription(["files"])
    sub.attach()
    assert events.topic_subscriber_count("files") == 1
    events.uninstall_loop()
    assert events.topic_subscriber_count("files") == 0
