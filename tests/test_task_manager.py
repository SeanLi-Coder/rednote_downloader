from __future__ import annotations

import threading
from pathlib import Path

from app.downloader import DiscoveryResult, DownloadOutcome, EngineEvent
from app.errors import AuthenticationRequiredError, MediaDownloadError
from app.models import (
    DownloadItem,
    DownloadJob,
    ItemStatus,
    JobStatus,
    MediaType,
    Platform,
    SourceKind,
    TransferProgress,
)
from app.storage import JsonJobStore
from app.task_manager import DownloadManager


class FakeEngine:
    def __init__(self) -> None:
        self.failures_remaining = {"first": 1, "second": 1}

    def discover(self, url, platform, kind, *, should_cancel):
        return DiscoveryResult(
            author="Test/Author",
            items=[
                DownloadItem(
                    id="first",
                    media_id="video-1",
                    source_url="https://www.youtube.com/watch?v=abcdefghijk",
                    title="First",
                    media_type=MediaType.VIDEO,
                ),
                DownloadItem(
                    id="second",
                    media_id="video-2",
                    source_url="https://www.youtube.com/watch?v=lmnopqrstuv",
                    title="Second",
                    media_type=MediaType.VIDEO,
                ),
            ],
        )

    def download_item(
        self,
        item,
        platform,
        output_dir,
        *,
        callback,
        should_cancel,
    ):
        callback(
            EngineEvent(
                event="downloading",
                progress=TransferProgress(
                    downloaded_bytes=50,
                    total_bytes=100,
                    percent=50.0,
                ),
            )
        )
        if self.failures_remaining[item.id] > 0:
            self.failures_remaining[item.id] -= 1
            raise MediaDownloadError(f"Temporary failure for {item.id}")
        return DownloadOutcome(
            output_paths=[str(Path(output_dir) / f"{item.id}.mp4")],
            title=item.title,
            upload_date="2025-11-14",
            author="Test/Author",
            media_type=MediaType.VIDEO,
            selected_format="bestvideo+bestaudio",
            resolution="3840x2160",
        )


class AuthOnceEngine(FakeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.auth_required = True

    def download_item(self, item, platform, output_dir, *, callback, should_cancel):
        if item.id == "first" and self.auth_required:
            self.auth_required = False
            raise AuthenticationRequiredError(
                "Complete verification in Chrome",
                verification_url=item.source_url,
            )
        return DownloadOutcome(
            output_paths=[str(Path(output_dir) / f"{item.id}.mp4")],
            title=item.title,
            upload_date="2025-11-14",
            author="Test/Author",
            media_type=MediaType.VIDEO,
            selected_format="bestvideo+bestaudio",
            resolution="3840x2160",
        )


class DiscoveryAuthEngine:
    def discover(self, url, platform, kind, *, should_cancel):
        raise AuthenticationRequiredError(
            "Complete verification in Chrome",
            verification_url=url,
        )


class PartialAssetEngine:
    def discover(self, url, platform, kind, *, should_cancel):
        return DiscoveryResult(
            author="Multi Asset Author",
            items=[
                DownloadItem(
                    id="note-1",
                    media_id="note-1",
                    source_url="https://www.xiaohongshu.com/explore/note1",
                    title="Multi image note",
                    media_type=MediaType.IMAGE,
                )
            ],
        )

    def download_item(self, item, platform, output_dir, *, callback, should_cancel):
        callback(
            EngineEvent(
                event="asset_completed",
                output_paths=[str(Path(output_dir) / "first.webp")],
            )
        )
        raise MediaDownloadError("Image 2 failed: temporary CDN error")


class RefreshFailureEngine:
    def __init__(self) -> None:
        self.discovery_calls = 0
        self.auth_required = True

    def discover(self, url, platform, kind, *, should_cancel):
        self.discovery_calls += 1
        if self.discovery_calls == 2:
            raise MediaDownloadError("Temporary profile refresh failure")
        return DiscoveryResult(
            author="Refresh Author",
            items=[
                DownloadItem(
                    id="stable-note",
                    media_id="stable-note",
                    source_url=(
                        "https://www.xiaohongshu.com/explore/stable-note"
                        f"?xsec_token=fresh-{self.discovery_calls}"
                    ),
                    title="Refreshable note",
                    media_type=MediaType.IMAGE,
                )
            ],
        )

    def download_item(self, item, platform, output_dir, *, callback, should_cancel):
        if self.auth_required:
            self.auth_required = False
            raise AuthenticationRequiredError(
                "Complete verification in Chrome",
                verification_url=item.source_url,
            )
        return DownloadOutcome(
            output_paths=[str(Path(output_dir) / "stable-note.webp")],
            title=item.title,
            upload_date="2025-11-14",
            author="Refresh Author",
            media_type=MediaType.IMAGE,
        )


class SelectiveProfileRetryEngine:
    def __init__(self) -> None:
        self.auth_required = True
        self.download_calls: list[str] = []

    def discover(self, url, platform, kind, *, should_cancel):
        return DiscoveryResult(
            author="Selective Author",
            items=[
                DownloadItem(
                    id=item_id,
                    media_id=item_id,
                    source_url=(
                        f"https://www.xiaohongshu.com/explore/{item_id}"
                        "?xsec_token=fresh"
                    ),
                    title=item_id,
                    media_type=MediaType.IMAGE,
                )
                for item_id in ("first-note", "second-note")
            ],
        )

    def download_item(self, item, platform, output_dir, *, callback, should_cancel):
        self.download_calls.append(item.id)
        if item.id == "first-note" and self.auth_required:
            self.auth_required = False
            raise AuthenticationRequiredError(
                "Complete verification in Chrome",
                verification_url=item.source_url,
            )
        return DownloadOutcome(
            output_paths=[str(Path(output_dir) / f"{item.id}.webp")],
            title=item.title,
            upload_date="2025-11-14",
            author="Selective Author",
            media_type=MediaType.IMAGE,
        )


def wait_for_job(manager: DownloadManager, job_id: str):
    manager._futures[job_id].result(timeout=5)
    return manager.get_job(job_id)


def test_single_and_batch_retry_only_run_failed_items(monkeypatch, tmp_path) -> None:
    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    engine = FakeEngine()
    progress_snapshots: list[float] = []

    def listener(event, job) -> None:
        if event.event == "downloading" and event.item_id:
            item = next(value for value in job.items if value.id == event.item_id)
            if item.progress.percent is not None:
                progress_snapshots.append(item.progress.percent)

    manager.add_listener(listener)
    monkeypatch.setattr(manager, "_engine_for_job", lambda job: engine)

    try:
        created = manager.create_job(
            "https://www.youtube.com/@BlenderOfficial",
            auto_start=True,
        )
        initial = wait_for_job(manager, created.id)

        assert initial.status == JobStatus.FAILED
        assert [item.status for item in initial.items] == [
            ItemStatus.FAILED,
            ItemStatus.FAILED,
        ]
        assert Path(initial.output_dir).name == "Test_Author"
        assert progress_snapshots == [50.0, 50.0]

        manager.retry_item(created.id, "first")
        after_single_retry = wait_for_job(manager, created.id)

        assert after_single_retry.status == JobStatus.PARTIAL
        assert after_single_retry.items[0].status == ItemStatus.COMPLETED
        assert after_single_retry.items[0].attempts == 2
        assert after_single_retry.items[0].progress.percent == 100.0
        assert after_single_retry.items[1].status == ItemStatus.FAILED
        assert after_single_retry.items[1].attempts == 1

        manager.retry_failed(created.id)
        completed = wait_for_job(manager, created.id)

        assert completed.status == JobStatus.COMPLETED
        assert all(item.status == ItemStatus.COMPLETED for item in completed.items)
        assert completed.items[0].attempts == 2
        assert completed.items[1].attempts == 2
        assert completed.completed_items == 2
        assert completed.failed_items == 0
    finally:
        manager.shutdown()


def test_auth_retry_resumes_items_that_were_still_queued(monkeypatch, tmp_path) -> None:
    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    engine = AuthOnceEngine()
    monkeypatch.setattr(manager, "_engine_for_job", lambda job: engine)

    try:
        created = manager.create_job(
            "https://www.youtube.com/@BlenderOfficial",
            auto_start=True,
        )
        paused = wait_for_job(manager, created.id)

        assert paused.status == JobStatus.NEEDS_AUTH
        assert [item.status for item in paused.items] == [
            ItemStatus.NEEDS_AUTH,
            ItemStatus.QUEUED,
        ]

        manager.retry_failed(created.id)
        resumed = wait_for_job(manager, created.id)

        assert resumed.status == JobStatus.COMPLETED
        assert all(item.status == ItemStatus.COMPLETED for item in resumed.items)
    finally:
        manager.shutdown()


def test_auth_single_retry_does_not_start_other_queued_items(
    monkeypatch, tmp_path
) -> None:
    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    engine = AuthOnceEngine()
    monkeypatch.setattr(manager, "_engine_for_job", lambda job: engine)

    try:
        created = manager.create_job(
            "https://www.youtube.com/@BlenderOfficial",
            auto_start=True,
        )
        paused = wait_for_job(manager, created.id)
        assert paused.status == JobStatus.NEEDS_AUTH

        manager.retry_item(created.id, "first")
        after_single = wait_for_job(manager, created.id)
        assert after_single.status == JobStatus.PARTIAL
        assert after_single.items[0].status == ItemStatus.COMPLETED
        assert after_single.items[1].status == ItemStatus.QUEUED
        assert after_single.items[1].attempts == 0

        manager.retry_failed(created.id)
        completed = wait_for_job(manager, created.id)
        assert completed.status == JobStatus.COMPLETED
        assert completed.items[1].status == ItemStatus.COMPLETED
    finally:
        manager.shutdown()


def test_partial_asset_paths_are_persisted_when_later_asset_fails(
    monkeypatch, tmp_path
) -> None:
    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    monkeypatch.setattr(manager, "_engine_for_job", lambda job: PartialAssetEngine())

    try:
        created = manager.create_job(
            "https://www.xiaohongshu.com/user/profile/example",
            auto_start=True,
        )
        failed = wait_for_job(manager, created.id)

        assert failed.status == JobStatus.FAILED
        assert failed.items[0].status == ItemStatus.FAILED
        assert failed.items[0].output_paths == [
            str(Path(failed.output_dir) / "first.webp")
        ]
        assert failed.items[0].error == "Image 2 failed: temporary CDN error"
    finally:
        manager.shutdown()


def test_profile_refresh_preserves_files_saved_before_a_partial_failure() -> None:
    previous = DownloadItem(
        id="old-id",
        media_id="note-id",
        source_url="https://www.xiaohongshu.com/explore/note-id?xsec_token=old",
        status=ItemStatus.FAILED,
        output_paths=["/downloads/image-001.webp", "/downloads/image-002.webp"],
        selected_format="original",
        resolution="3000x4000",
        error="Image 3 failed",
        attempts=1,
    )
    fresh = DownloadItem(
        id="new-id",
        media_id="note-id",
        source_url="https://www.xiaohongshu.com/explore/note-id?xsec_token=new",
    )

    merged = DownloadManager._merge_discovered_items([previous], [fresh])[0]

    assert merged.id == "new-id"
    assert merged.source_url.endswith("xsec_token=new")
    assert merged.status == ItemStatus.FAILED
    assert merged.output_paths == previous.output_paths
    assert merged.selected_format == "original"
    assert merged.resolution == "3000x4000"
    assert merged.error == "Image 3 failed"


def test_profile_refresh_failure_can_be_retried_again(monkeypatch, tmp_path) -> None:
    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    engine = RefreshFailureEngine()
    monkeypatch.setattr(manager, "_engine_for_job", lambda job: engine)

    try:
        created = manager.create_job(
            "https://www.xiaohongshu.com/user/profile/example",
            auto_start=True,
        )
        needs_auth = wait_for_job(manager, created.id)
        assert needs_auth.status == JobStatus.NEEDS_AUTH

        manager.retry_item(created.id, "stable-note")
        refresh_failed = wait_for_job(manager, created.id)
        assert refresh_failed.status == JobStatus.FAILED
        assert refresh_failed.discovery_complete is False
        assert refresh_failed.items[0].status == ItemStatus.NEEDS_AUTH

        manager.retry_failed(created.id)
        recovered = wait_for_job(manager, created.id)
        assert recovered.status == JobStatus.COMPLETED
        assert recovered.discovery_complete is True
        assert recovered.items[0].status == ItemStatus.COMPLETED
        assert recovered.items[0].source_url.endswith("xsec_token=fresh-3")
        assert engine.discovery_calls == 3
    finally:
        manager.shutdown()


def test_profile_single_retry_refreshes_tokens_but_downloads_only_selected_item(
    monkeypatch, tmp_path
) -> None:
    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    engine = SelectiveProfileRetryEngine()
    monkeypatch.setattr(manager, "_engine_for_job", lambda job: engine)

    try:
        created = manager.create_job(
            "https://www.xiaohongshu.com/user/profile/example",
            auto_start=True,
        )
        needs_auth = wait_for_job(manager, created.id)
        assert needs_auth.status == JobStatus.NEEDS_AUTH
        assert engine.download_calls == ["first-note"]

        manager.retry_item(created.id, "first-note")
        after_single = wait_for_job(manager, created.id)

        assert after_single.status == JobStatus.PARTIAL
        assert after_single.items[0].status == ItemStatus.COMPLETED
        assert after_single.items[1].status == ItemStatus.QUEUED
        assert engine.download_calls == ["first-note", "first-note"]

        manager.retry_failed(created.id)
        completed = wait_for_job(manager, created.id)
        assert completed.status == JobStatus.COMPLETED
        assert engine.download_calls == [
            "first-note",
            "first-note",
            "second-note",
        ]
    finally:
        manager.shutdown()


def test_pending_job_can_be_cancelled_before_worker_starts(tmp_path) -> None:
    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    blocker_started = threading.Event()
    release_blocker = threading.Event()

    def block_worker() -> None:
        blocker_started.set()
        release_blocker.wait(timeout=5)

    blocker = manager._executor.submit(block_worker)
    assert blocker_started.wait(timeout=1)

    try:
        created = manager.create_job(
            "https://www.youtube.com/@BlenderOfficial",
            auto_start=True,
        )
        cancelled = manager.cancel_job(created.id)

        assert cancelled.status == JobStatus.CANCELLED
        assert manager._futures[created.id].cancelled()
    finally:
        release_blocker.set()
        blocker.result(timeout=2)
        manager.shutdown()


def test_cancel_after_item_needs_auth_publication_converges_to_cancelled(
    monkeypatch, tmp_path
) -> None:
    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    needs_auth_published = threading.Event()
    release_worker = threading.Event()

    def listener(event, job) -> None:
        if event.event == "needs_auth" and event.item_id == "first":
            needs_auth_published.set()
            release_worker.wait(timeout=5)

    manager.add_listener(listener)
    monkeypatch.setattr(manager, "_engine_for_job", lambda job: AuthOnceEngine())

    try:
        created = manager.create_job(
            "https://www.youtube.com/@BlenderOfficial",
            auto_start=True,
        )
        assert needs_auth_published.wait(timeout=2)
        published = manager.get_job(created.id)
        assert published.status == JobStatus.NEEDS_AUTH
        assert published.items[0].status == ItemStatus.NEEDS_AUTH
        assert not manager._futures[created.id].done()

        cancelled = manager.cancel_job(created.id)
        assert cancelled.status == JobStatus.CANCELLED

        release_worker.set()
        final = wait_for_job(manager, created.id)

        assert final.status == JobStatus.CANCELLED
        assert final.cancel_requested is False
        assert final.auth_message is None
        assert final.verification_url is None
        assert [item.status for item in final.items] == [
            ItemStatus.CANCELLED,
            ItemStatus.CANCELLED,
        ]
        assert all(item.auth_message is None for item in final.items)
        assert JsonJobStore(tmp_path / "state").get(created.id).status == (
            JobStatus.CANCELLED
        )
    finally:
        release_worker.set()
        manager.shutdown()


def test_cancel_after_discovery_needs_auth_publication_converges_to_cancelled(
    monkeypatch, tmp_path
) -> None:
    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    needs_auth_published = threading.Event()
    release_worker = threading.Event()

    def listener(event, job) -> None:
        if event.event == "needs_auth" and event.item_id is None:
            needs_auth_published.set()
            release_worker.wait(timeout=5)

    manager.add_listener(listener)
    monkeypatch.setattr(manager, "_engine_for_job", lambda job: DiscoveryAuthEngine())

    try:
        created = manager.create_job(
            "https://www.youtube.com/@BlenderOfficial",
            auto_start=True,
        )
        assert needs_auth_published.wait(timeout=2)
        assert manager.get_job(created.id).status == JobStatus.NEEDS_AUTH
        assert not manager._futures[created.id].done()

        cancelled = manager.cancel_job(created.id)
        assert cancelled.status == JobStatus.CANCELLED

        release_worker.set()
        final = wait_for_job(manager, created.id)

        assert final.status == JobStatus.CANCELLED
        assert final.cancel_requested is False
        assert final.auth_message is None
        assert final.verification_url is None
        assert final.items == []
        assert JsonJobStore(tmp_path / "state").get(created.id).status == (
            JobStatus.CANCELLED
        )
    finally:
        release_worker.set()
        manager.shutdown()


def test_restore_finalizes_job_when_all_items_were_saved_before_crash(
    tmp_path,
) -> None:
    state_dir = tmp_path / "state"
    job = DownloadJob(
        id="crash-window",
        source_url="https://www.youtube.com/@BlenderOfficial",
        platform=Platform.YOUTUBE,
        source_kind=SourceKind.PROFILE,
        output_root=str(tmp_path / "downloads"),
        status=JobStatus.DOWNLOADING,
        items=[
            DownloadItem(
                id="done",
                source_url="https://www.youtube.com/watch?v=LXb3EKWsInQ",
                status=ItemStatus.COMPLETED,
                output_paths=[str(tmp_path / "downloads" / "done.mp4")],
            )
        ],
    )
    job.refresh_counts()
    JsonJobStore(state_dir).save(job)

    manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    try:
        restored = manager.get_job(job.id)
        assert restored.status == JobStatus.COMPLETED
        assert restored.error is None
    finally:
        manager.shutdown()


def test_cancel_does_not_overwrite_completed_job_state(tmp_path) -> None:
    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    created = manager.create_job(
        "https://www.youtube.com/watch?v=LXb3EKWsInQ",
        auto_start=False,
    )
    with manager._lock:
        job = manager._jobs[created.id]
        job.status = JobStatus.COMPLETED
        manager._commit_locked(job)

    try:
        unchanged = manager.cancel_job(created.id)
        assert unchanged.status == JobStatus.COMPLETED
        assert unchanged.cancel_requested is False
    finally:
        manager.shutdown()
