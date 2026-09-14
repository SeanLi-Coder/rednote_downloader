from __future__ import annotations

import asyncio
import json

import pytest

from app import main as main_module
from app.event_stream import JobEventBuffer
from app.models import DownloadItem, DownloadJob, Platform, SourceKind


def make_job(job_id: str, revision: int) -> DownloadJob:
    return DownloadJob(
        id=job_id,
        source_url="https://www.douyin.com/video/7683074221437746170",
        platform=Platform.DOUYIN,
        source_kind=SourceKind.ITEM,
        output_root="downloads",
        revision=revision,
    )


def test_worker_bursts_preserve_latest_update_for_each_job() -> None:
    async def exercise() -> None:
        buffer = JobEventBuffer()

        def publish_burst() -> None:
            for revision in range(1, 101):
                buffer.publish(None, make_job("first", revision))
                buffer.publish(None, make_job("second", revision * 2))
            buffer.publish(None, make_job("first", 50))

        try:
            await asyncio.to_thread(publish_burst)
            jobs = await buffer.take(timeout=1)
            assert {job.id: job.revision for job in jobs} == {
                "first": 100,
                "second": 200,
            }
        finally:
            buffer.close()

    asyncio.run(exercise())


def test_late_worker_update_cannot_revert_a_previously_delivered_revision() -> None:
    async def exercise() -> None:
        buffer = JobEventBuffer()
        try:
            await asyncio.to_thread(buffer.publish, None, make_job("first", 10))
            assert [(job.id, job.revision) for job in await buffer.take(timeout=1)] == [
                ("first", 10)
            ]

            def publish_late_and_current() -> None:
                buffer.publish(None, make_job("first", 9))
                buffer.publish(None, make_job("first", 10))
                buffer.publish(None, make_job("second", 1))

            await asyncio.to_thread(publish_late_and_current)
            assert [(job.id, job.revision) for job in await buffer.take(timeout=1)] == [
                ("second", 1)
            ]
            await asyncio.to_thread(buffer.publish, None, make_job("first", 11))
            assert [(job.id, job.revision) for job in await buffer.take(timeout=1)] == [
                ("first", 11)
            ]
        finally:
            buffer.close()

    asyncio.run(exercise())


class ListenerManager:
    def __init__(self) -> None:
        self.listeners = []

    def add_listener(self, listener) -> None:
        self.listeners.append(listener)

    def remove_listener(self, listener) -> None:
        self.listeners.remove(listener)


@pytest.mark.parametrize("disconnect", ["close", "cancel"])
def test_event_stream_disconnect_releases_listener(monkeypatch, disconnect) -> None:
    manager = ListenerManager()
    monkeypatch.setattr(main_module, "manager", manager)

    async def exercise() -> None:
        response = await main_module.events()
        stream = response.body_iterator
        assert await stream.__anext__() == ": connected\n\n"
        assert len(manager.listeners) == 1
        late_listener = manager.listeners[0]
        try:
            if disconnect == "cancel":
                waiting = asyncio.create_task(stream.__anext__())
                await asyncio.sleep(0)
                waiting.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiting
            else:
                await stream.aclose()
            assert manager.listeners == []
            await asyncio.to_thread(late_listener, None, make_job("late", 1))
            assert manager.listeners == []
        finally:
            await stream.aclose()

    asyncio.run(exercise())


def test_event_stream_serializes_only_public_media_metadata(monkeypatch) -> None:
    manager = ListenerManager()
    monkeypatch.setattr(main_module, "manager", manager)
    job = make_job("public", 3)
    signed_url = "https://v26-web.douyinvod.com/media?signature=private-secret"
    job.items = [
        DownloadItem(
            id="target",
            media_id="7683074221437746170",
            source_url=job.source_url,
            metadata={
                "douyin_item_media": {
                    "media_id": "7683074221437746170",
                    "media_kind": "image",
                    "title": "Live Photo",
                    "live_photo_assets": [{"candidates": [signed_url]}],
                }
            },
        )
    ]

    async def exercise() -> None:
        response = await main_module.events()
        stream = response.body_iterator
        try:
            await stream.__anext__()
            await asyncio.to_thread(manager.listeners[0], None, job)
            message = await asyncio.wait_for(stream.__anext__(), timeout=1)
            assert message.startswith("event: job\ndata: ")
            payload = json.loads(message.split("data: ", 1)[1])
            assert payload == main_module._public_job(job).model_dump(mode="json")
            assert payload["items"][0]["metadata"]["douyin_item_media"] == {
                "media_id": "7683074221437746170",
                "media_kind": "image",
                "title": "Live Photo",
            }
            assert signed_url not in message
            assert "private-secret" not in message
            assert job.items[0].metadata["douyin_item_media"]["live_photo_assets"] == [
                {"candidates": [signed_url]}
            ]
        finally:
            await stream.aclose()
        assert manager.listeners == []

    asyncio.run(exercise())
