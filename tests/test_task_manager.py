from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app.downloader import DiscoveryResult, DownloadOutcome, EngineEvent
from app.errors import (
    AuthenticationRequiredError,
    MediaDownloadError,
    TemporaryAccessError,
)
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
from app.task_manager import DownloadManager, ItemNotRetryableError


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


def test_douyin_item_discovery_blocks_unexpected_profile_expansion(
    monkeypatch, tmp_path
) -> None:
    source_url = "https://www.douyin.com/video/7664225419386607205"

    class PoisonedItemEngine:
        def discover(self, url, platform, kind, *, should_cancel):
            return DiscoveryResult(
                author="Wrong profile",
                items=[
                    DownloadItem(
                        id=media_id,
                        media_id=media_id,
                        source_url=f"https://www.douyin.com/video/{media_id}",
                        title=media_id,
                        media_type=MediaType.VIDEO,
                    )
                    for media_id in (
                        "7677923079457231738",
                        "7677554129950241521",
                    )
                ],
            )

        def download_item(self, *args, **kwargs):
            raise AssertionError("unexpected profile entries must not be downloaded")

    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    monkeypatch.setattr(manager, "_engine_for_job", lambda job: PoisonedItemEngine())

    try:
        created = manager.create_job(source_url, auto_start=True)
        blocked = wait_for_job(manager, created.id)

        assert blocked.source_kind == SourceKind.ITEM
        assert blocked.status == JobStatus.NEEDS_AUTH
        assert blocked.items == []
        assert blocked.total_items == 0
        assert blocked.verification_url == source_url
        assert "uploader profile" in (blocked.error or "")
    finally:
        manager.shutdown()


def test_douyin_item_auth_always_opens_canonical_video(monkeypatch, tmp_path) -> None:
    media_id = "7664225419386607205"
    source_url = f"https://www.douyin.com/video/{media_id}"

    class WrongVerificationEngine:
        def discover(self, url, platform, kind, *, should_cancel):
            return DiscoveryResult(
                author="Verified author",
                items=[
                    DownloadItem(
                        id="target",
                        media_id=media_id,
                        source_url=source_url,
                        title="Target video",
                        media_type=MediaType.VIDEO,
                    )
                ],
            )

        def download_item(self, item, platform, output_dir, *, callback, should_cancel):
            raise AuthenticationRequiredError(
                "Complete verification in Chrome",
                verification_url="https://www.douyin.com/user/wrong-profile",
            )

    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    monkeypatch.setattr(
        manager,
        "_engine_for_job",
        lambda job: WrongVerificationEngine(),
    )

    try:
        created = manager.create_job(source_url, auto_start=True)
        blocked = wait_for_job(manager, created.id)

        assert blocked.status == JobStatus.NEEDS_AUTH
        assert blocked.total_items == 1
        assert blocked.verification_url == source_url
        assert blocked.items[0].metadata["verification_url"] == source_url
        assert "profile_url" not in blocked.items[0].metadata
    finally:
        manager.shutdown()


def test_douyin_rate_limit_retry_clears_stale_auth_state(
    monkeypatch,
    tmp_path,
) -> None:
    media_id = "7664225419386607205"
    source_url = f"https://www.douyin.com/video/{media_id}"

    class AuthThenLimitedEngine:
        def __init__(self) -> None:
            self.discovery_calls = 0

        def discover(self, url, platform, kind, *, should_cancel):
            self.discovery_calls += 1
            if self.discovery_calls == 2:
                raise TemporaryAccessError(
                    "Douyin temporarily limited a signed request after automatic retries"
                )
            return DiscoveryResult(
                author="Verified author",
                items=[
                    DownloadItem(
                        id="target",
                        media_id=media_id,
                        source_url=source_url,
                        title="Target video",
                        media_type=MediaType.VIDEO,
                    )
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
            raise AuthenticationRequiredError(
                "Complete verification in Chrome",
                verification_url=source_url,
            )

    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    engine = AuthThenLimitedEngine()
    monkeypatch.setattr(manager, "_engine_for_job", lambda job: engine)

    try:
        created = manager.create_job(source_url, auto_start=True)
        blocked = wait_for_job(manager, created.id)
        assert blocked.status == JobStatus.NEEDS_AUTH
        assert blocked.items[0].status == ItemStatus.NEEDS_AUTH

        manager.retry_failed(created.id)
        limited = wait_for_job(manager, created.id)

        assert limited.status == JobStatus.FAILED
        assert limited.auth_message is None
        assert limited.verification_url is None
        assert limited.items[0].status == ItemStatus.FAILED
        assert limited.items[0].auth_message is None
        assert limited.items[0].retryable is True
        assert "temporarily limited" in (limited.error or "")
    finally:
        manager.shutdown()


def test_douyin_item_discovery_rejects_matching_id_from_wrong_host(
    monkeypatch, tmp_path
) -> None:
    media_id = "7664225419386607205"
    source_url = f"https://www.douyin.com/video/{media_id}"

    class WrongHostEngine:
        def discover(self, url, platform, kind, *, should_cancel):
            return DiscoveryResult(
                author="Wrong source",
                items=[
                    DownloadItem(
                        id="target",
                        media_id=media_id,
                        source_url=f"https://evil.example/video/{media_id}",
                    )
                ],
            )

        def download_item(self, *args, **kwargs):
            raise AssertionError("wrong-host item must not be downloaded")

    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    monkeypatch.setattr(manager, "_engine_for_job", lambda job: WrongHostEngine())

    try:
        created = manager.create_job(source_url, auto_start=True)
        blocked = wait_for_job(manager, created.id)

        assert blocked.status == JobStatus.NEEDS_AUTH
        assert blocked.items == []
        assert blocked.verification_url == source_url
    finally:
        manager.shutdown()


def test_retry_repairs_persisted_douyin_item_profile_expansion(
    monkeypatch, tmp_path
) -> None:
    media_id = "7664225419386607205"
    source_url = f"https://www.douyin.com/video/{media_id}"
    preserved_wrong_file = tmp_path / "downloads" / "wrong.mp4"
    preserved_wrong_file.parent.mkdir(parents=True)
    preserved_wrong_file.write_bytes(b"wrong but preserved")

    class RepairedItemEngine:
        def discover(self, url, platform, kind, *, should_cancel):
            assert url == source_url
            assert kind == SourceKind.ITEM
            return DiscoveryResult(
                author="Correct author",
                items=[
                    DownloadItem(
                        id="correct-item",
                        media_id=media_id,
                        source_url=source_url,
                        title="Correct title",
                        media_type=MediaType.VIDEO,
                        metadata={
                            "verification_url": source_url,
                            "item_identity_verified": True,
                            "douyin_item_media": {
                                "media_id": media_id,
                                "video_uri": "verified-direct-video-uri",
                            },
                        },
                    )
                ],
            )

        def download_item(self, item, platform, output_dir, *, callback, should_cancel):
            assert item.media_id == media_id
            assert item.metadata["verification_url"] == source_url
            assert item.metadata["item_identity_verified"] is True
            assert "profile_url" not in item.metadata
            assert "profile_owner_verified" not in item.metadata
            assert "douyin_profile_media" not in item.metadata
            return DownloadOutcome(
                output_paths=[str(Path(output_dir) / "correct.mp4")],
                title=item.title,
                author="Correct author",
                media_type=MediaType.VIDEO,
                resolution="1440x2560",
            )

    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    monkeypatch.setattr(manager, "_engine_for_job", lambda job: RepairedItemEngine())

    try:
        created = manager.create_job(source_url, auto_start=False)
        with manager._lock:
            job = manager._require_job(created.id)
            job.source_url = (
                "https://www.douyin.com/user/wrong-profile"
                f"?modal_id={media_id}&from_tab_name=main"
            )
            job.source_kind = SourceKind.PROFILE
            job.status = JobStatus.NEEDS_AUTH
            job.verification_url = "https://www.douyin.com/user/wrong-profile"
            job.items = [
                DownloadItem(
                    id="wrong-completed",
                    media_id="7677923079457231738",
                    source_url="https://www.douyin.com/video/7677923079457231738",
                    status=ItemStatus.COMPLETED,
                    output_paths=[str(preserved_wrong_file)],
                ),
                DownloadItem(
                    id="wrong-auth",
                    media_id="7677554129950241521",
                    source_url="https://www.douyin.com/video/7677554129950241521",
                    status=ItemStatus.NEEDS_AUTH,
                ),
                DownloadItem(
                    id="legacy-target-auth",
                    media_id=media_id,
                    source_url=source_url,
                    title=media_id,
                    status=ItemStatus.COMPLETED,
                    output_paths=[str(tmp_path / "downloads" / "legacy-target.mp4")],
                    metadata={
                        "profile_url": "https://www.douyin.com/user/wrong-profile",
                        "profile_owner_verified": True,
                        "douyin_profile_media": {
                            "media_id": media_id,
                            "owner_id": "wrong-profile",
                            "video_uri": "legacy-profile-video-uri",
                        },
                    },
                ),
            ]
            job.refresh_counts()
            manager._commit_locked(job)

        manager.retry_item(created.id, "wrong-auth")
        repaired = wait_for_job(manager, created.id)

        assert repaired.status == JobStatus.COMPLETED
        assert repaired.source_kind == SourceKind.ITEM
        assert repaired.source_url == source_url
        assert repaired.total_items == 1
        assert repaired.items[0].media_id == media_id
        assert repaired.items[0].title == "Correct title"
        assert preserved_wrong_file.exists()
    finally:
        manager.shutdown()


def test_restore_migrates_single_modal_item_and_forces_direct_rediscovery(
    tmp_path,
) -> None:
    media_id = "7664225419386607205"
    source_url = f"https://www.douyin.com/video/{media_id}"
    state_dir = tmp_path / "state"
    job = DownloadJob(
        id="legacy-modal-single",
        source_url=(
            "https://www.douyin.com/user/wrong-profile"
            f"?modal_id={media_id}&from_tab_name=main"
        ),
        platform=Platform.DOUYIN,
        source_kind=SourceKind.PROFILE,
        output_root=str(tmp_path / "downloads"),
        status=JobStatus.NEEDS_AUTH,
        verification_url="https://www.douyin.com/user/wrong-profile",
        items=[
            DownloadItem(
                id="legacy-target",
                media_id=media_id,
                source_url=source_url,
                status=ItemStatus.NEEDS_AUTH,
                metadata={
                    "profile_url": "https://www.douyin.com/user/wrong-profile",
                    "profile_owner_verified": True,
                    "douyin_profile_media": {
                        "media_id": media_id,
                        "owner_id": "wrong-profile",
                        "video_uri": "legacy-profile-video-uri",
                    },
                },
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

        assert restored.source_url == source_url
        assert restored.source_kind == SourceKind.ITEM
        assert restored.status == JobStatus.INTERRUPTED
        assert restored.discovery_complete is False
        assert restored.verification_url == source_url
        assert restored.items[0].status == ItemStatus.FAILED
        assert restored.items[0].retryable is True
        assert restored.items[0].metadata["verification_url"] == source_url
        assert "profile_url" not in restored.items[0].metadata
        assert "profile_owner_verified" not in restored.items[0].metadata
        assert "douyin_profile_media" not in restored.items[0].metadata
    finally:
        manager.shutdown()


def test_restore_migrates_legacy_markdown_joined_douyin_item_source(
    tmp_path,
) -> None:
    media_id = "7664225419386607205"
    source_url = f"https://www.douyin.com/video/{media_id}"
    legacy_source_url = f"{source_url}]({source_url}"
    state_dir = tmp_path / "state"
    job = DownloadJob(
        id="legacy-markdown-joined-item",
        source_url=legacy_source_url,
        platform=Platform.DOUYIN,
        source_kind=SourceKind.ITEM,
        output_root=str(tmp_path / "downloads"),
        status=JobStatus.COMPLETED,
        verification_url="https://www.douyin.com/user/wrong-profile",
        items=[
            DownloadItem(
                id="legacy-target",
                media_id=media_id,
                source_url=source_url,
                status=ItemStatus.COMPLETED,
                output_paths=[str(tmp_path / "downloads" / "legacy-target.mp4")],
            )
        ],
        total_items=1,
        completed_items=1,
    )
    JsonJobStore(state_dir).save(job)

    manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    try:
        restored = manager.get_job(job.id)

        assert restored.source_url == source_url
        assert restored.source_kind == SourceKind.ITEM
        assert restored.status == JobStatus.INTERRUPTED
        assert restored.retryable is True
        assert restored.discovery_complete is False
        assert restored.verification_url == source_url
        assert restored.items[0].status == ItemStatus.FAILED
        assert restored.items[0].retryable is True
        assert restored.items[0].output_paths == [
            str(tmp_path / "downloads" / "legacy-target.mp4")
        ]
    finally:
        manager.shutdown()


def test_restore_rejects_conflicting_legacy_markdown_joined_douyin_ids(
    tmp_path,
) -> None:
    label_id = "7664225419386607205"
    target_id = "7677923079457231738"
    state_dir = tmp_path / "state"
    job = DownloadJob(
        id="conflicting-markdown-joined-item",
        source_url=(
            f"https://www.douyin.com/video/{label_id}]"
            f"(https://www.douyin.com/video/{target_id}"
        ),
        platform=Platform.DOUYIN,
        source_kind=SourceKind.ITEM,
        output_root=str(tmp_path / "downloads"),
        status=JobStatus.NEEDS_AUTH,
        items=[
            DownloadItem(
                id="ambiguous-target",
                media_id=target_id,
                source_url=f"https://www.douyin.com/video/{target_id}",
                status=ItemStatus.NEEDS_AUTH,
            )
        ],
        total_items=1,
        failed_items=1,
    )
    JsonJobStore(state_dir).save(job)

    manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    try:
        restored = manager.get_job(job.id)

        assert restored.source_url == job.source_url
        assert restored.status == JobStatus.FAILED
        assert restored.retryable is False
        assert restored.verification_url is None
        assert restored.items[0].status == ItemStatus.FAILED
        assert restored.items[0].retryable is False
        with pytest.raises(ItemNotRetryableError):
            manager.retry_failed(job.id)
    finally:
        manager.shutdown()


def test_restore_keeps_completed_direct_item_with_tracking_query(tmp_path) -> None:
    media_id = "7664225419386607205"
    source_url = f"https://www.douyin.com/video/{media_id}"
    state_dir = tmp_path / "state"
    job = DownloadJob(
        id="direct-with-tracking",
        source_url=f"{source_url}?previous_page=app_code_link",
        platform=Platform.DOUYIN,
        source_kind=SourceKind.ITEM,
        output_root=str(tmp_path / "downloads"),
        status=JobStatus.COMPLETED,
        items=[
            DownloadItem(
                id="target",
                media_id=media_id,
                source_url=source_url,
                status=ItemStatus.COMPLETED,
                output_paths=[str(tmp_path / "downloads" / "target.mp4")],
            )
        ],
        total_items=1,
        completed_items=1,
    )
    JsonJobStore(state_dir).save(job)

    manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    try:
        restored = manager.get_job(job.id)

        assert restored.source_url == source_url
        assert restored.status == JobStatus.COMPLETED
        assert restored.discovery_complete is True
        assert restored.items[0].status == ItemStatus.COMPLETED
    finally:
        manager.shutdown()


def test_restore_disables_retry_for_item_task_without_original_video_url(
    tmp_path,
) -> None:
    state_dir = tmp_path / "state"
    job = DownloadJob(
        id="unverifiable-direct-item",
        source_url="https://www.douyin.com/user/wrong-profile",
        platform=Platform.DOUYIN,
        source_kind=SourceKind.ITEM,
        output_root=str(tmp_path / "downloads"),
        status=JobStatus.DOWNLOADING,
        items=[
            DownloadItem(
                id="wrong-item",
                media_id="7677923079457231738",
                source_url="https://www.douyin.com/video/7677923079457231738",
                status=ItemStatus.DOWNLOADING,
            )
        ],
    )
    JsonJobStore(state_dir).save(job)

    manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    try:
        restored = manager.get_job(job.id)

        assert restored.status == JobStatus.FAILED
        assert restored.retryable is False
        assert restored.verification_url is None
        assert restored.items[0].status == ItemStatus.FAILED
        assert restored.items[0].retryable is False
        with pytest.raises(ItemNotRetryableError):
            manager.retry_item(job.id, "wrong-item")
        with pytest.raises(ItemNotRetryableError):
            manager.retry_failed(job.id)
    finally:
        manager.shutdown()


def test_restore_marks_corrupted_douyin_item_expansion_for_rediscovery(
    tmp_path,
) -> None:
    media_id = "7664225419386607205"
    source_url = f"https://www.douyin.com/video/{media_id}"
    wrong_file = tmp_path / "downloads" / "wrong.mp4"
    wrong_file.parent.mkdir(parents=True)
    wrong_file.write_bytes(b"preserved")
    state_dir = tmp_path / "state"
    job = DownloadJob(
        id="corrupted-direct-item",
        source_url=source_url,
        platform=Platform.DOUYIN,
        source_kind=SourceKind.ITEM,
        output_root=str(tmp_path / "downloads"),
        status=JobStatus.COMPLETED,
        items=[
            DownloadItem(
                id="wrong-item",
                media_id="7677923079457231738",
                source_url="https://www.douyin.com/video/7677923079457231738",
                status=ItemStatus.COMPLETED,
                output_paths=[str(wrong_file)],
            )
        ],
        total_items=1,
        completed_items=1,
    )
    JsonJobStore(state_dir).save(job)

    manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    try:
        restored = manager.get_job(job.id)

        assert restored.status == JobStatus.INTERRUPTED
        assert restored.discovery_complete is False
        assert restored.verification_url == source_url
        assert restored.items[0].status == ItemStatus.FAILED
        assert restored.items[0].output_paths == [str(wrong_file)]
        assert wrong_file.exists()
        assert "uploader profile" in (restored.error or "")
    finally:
        manager.shutdown()


def test_restore_rebinds_douyin_item_verification_to_canonical_video(
    tmp_path,
) -> None:
    media_id = "7664225419386607205"
    source_url = f"https://www.douyin.com/video/{media_id}"
    state_dir = tmp_path / "state"
    job = DownloadJob(
        id="direct-item-auth",
        source_url=source_url,
        platform=Platform.DOUYIN,
        source_kind=SourceKind.ITEM,
        output_root=str(tmp_path / "downloads"),
        status=JobStatus.NEEDS_AUTH,
        verification_url="https://evil.example/phish",
        items=[
            DownloadItem(
                id="target",
                media_id=media_id,
                source_url=source_url,
                status=ItemStatus.NEEDS_AUTH,
            )
        ],
        total_items=1,
        failed_items=1,
    )
    JsonJobStore(state_dir).save(job)

    manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    try:
        restored = manager.get_job(job.id)

        assert restored.status == JobStatus.NEEDS_AUTH
        assert restored.verification_url == source_url
        assert restored.total_items == 1
    finally:
        manager.shutdown()


def test_restore_discards_douyin_item_cache_bound_to_wrong_verification_url(
    monkeypatch,
    tmp_path,
) -> None:
    media_id = "7664225419386607205"
    source_url = f"https://www.douyin.com/video/{media_id}"
    state_dir = tmp_path / "state"
    stale_video_uri = "stale-shaped-video-uri"
    job = DownloadJob(
        id="direct-item-stale-cache-binding",
        source_url=source_url,
        platform=Platform.DOUYIN,
        source_kind=SourceKind.ITEM,
        output_root=str(tmp_path / "downloads"),
        status=JobStatus.NEEDS_AUTH,
        verification_url=source_url,
        items=[
            DownloadItem(
                id="target",
                media_id=media_id,
                source_url=source_url,
                status=ItemStatus.NEEDS_AUTH,
                metadata={
                    "verification_url": "https://www.douyin.com/user/wrong-profile",
                    "item_identity_verified": True,
                    "douyin_item_media": {
                        "media_id": media_id,
                        "video_uri": stale_video_uri,
                    },
                },
            )
        ],
        total_items=1,
        failed_items=1,
    )
    JsonJobStore(state_dir).save(job)

    class RefreshedDirectItemEngine:
        def __init__(self) -> None:
            self.discovery_calls = 0

        def discover(self, url, platform, kind, *, should_cancel):
            self.discovery_calls += 1
            assert url == source_url
            assert kind == SourceKind.ITEM
            return DiscoveryResult(
                author="Verified author",
                items=[
                    DownloadItem(
                        id="fresh-target",
                        media_id=media_id,
                        source_url=source_url,
                        title="Fresh target",
                        media_type=MediaType.VIDEO,
                        metadata={
                            "verification_url": source_url,
                            "item_identity_verified": True,
                            "douyin_item_media": {
                                "media_id": media_id,
                                "video_uri": "fresh-verified-video-uri",
                            },
                        },
                    )
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
            assert item.metadata["verification_url"] == source_url
            assert item.metadata["douyin_item_media"]["video_uri"] != stale_video_uri
            return DownloadOutcome(
                output_paths=[str(Path(output_dir) / "fresh-target.mp4")],
                title=item.title,
                author="Verified author",
                media_type=MediaType.VIDEO,
                resolution="1440x2560",
            )

    manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    engine = RefreshedDirectItemEngine()
    monkeypatch.setattr(manager, "_engine_for_job", lambda restored_job: engine)
    try:
        restored = manager.get_job(job.id)

        assert restored.status == JobStatus.INTERRUPTED
        assert restored.discovery_complete is False
        assert restored.items[0].status == ItemStatus.FAILED
        assert restored.items[0].metadata["verification_url"] == source_url
        assert "douyin_item_media" not in restored.items[0].metadata
        assert "item_identity_verified" not in restored.items[0].metadata

        manager.retry_failed(job.id)
        completed = wait_for_job(manager, job.id)

        assert completed.status == JobStatus.COMPLETED
        assert completed.items[0].resolution == "1440x2560"
        assert engine.discovery_calls == 1
    finally:
        manager.shutdown()


def test_fresh_douyin_discovery_cannot_launder_wrong_cache_binding(tmp_path) -> None:
    media_id = "7664225419386607205"
    source_url = f"https://www.douyin.com/video/{media_id}"
    job = DownloadJob(
        id="fresh-cache-binding",
        source_url=source_url,
        platform=Platform.DOUYIN,
        source_kind=SourceKind.ITEM,
        output_root=str(tmp_path),
    )
    item = DownloadItem(
        id="target",
        media_id=media_id,
        source_url=source_url,
        metadata={
            "verification_url": "https://www.douyin.com/video/9999999999999999999",
            "item_identity_verified": True,
            "douyin_item_media": {
                "media_id": media_id,
                "video_uri": "stale-shaped-video-uri",
            },
        },
    )

    with pytest.raises(AuthenticationRequiredError, match="uploader profile"):
        DownloadManager._validate_discovery_result(
            job,
            DiscoveryResult(author="Author", items=[item]),
        )

    assert "douyin_item_media" not in item.metadata
    assert "item_identity_verified" not in item.metadata


def test_failed_douyin_profile_retry_refreshes_signed_media_urls(tmp_path) -> None:
    job = DownloadJob(
        id="douyin-profile-expired-url",
        source_url="https://www.douyin.com/user/profile-a",
        platform=Platform.DOUYIN,
        source_kind=SourceKind.PROFILE,
        output_root=str(tmp_path),
        status=JobStatus.FAILED,
        discovery_complete=True,
        items=[
            DownloadItem(
                id="failed-item",
                media_id="2222222222222222222",
                source_url="https://www.douyin.com/video/2222222222222222222",
                status=ItemStatus.FAILED,
                retryable=True,
            )
        ],
    )

    assert DownloadManager._should_rediscover_on_retry(job) is True


def test_failed_douyin_item_retry_refreshes_expired_direct_urls(
    monkeypatch,
    tmp_path,
) -> None:
    media_id = "7638230489560727931"
    source_url = f"https://www.douyin.com/video/{media_id}"

    class RefreshingDirectEngine:
        def __init__(self) -> None:
            self.discovery_calls = 0
            self.download_urls: list[str] = []

        def discover(self, url, platform, kind, *, should_cancel):
            self.discovery_calls += 1
            direct_url = (
                "https://v26-web.douyinvod.com/" f"signed-{self.discovery_calls}.mp4"
            )
            return DiscoveryResult(
                author="Verified author",
                items=[
                    DownloadItem(
                        id="target",
                        media_id=media_id,
                        source_url=source_url,
                        title="Target video",
                        media_type=MediaType.VIDEO,
                        metadata={
                            "verification_url": source_url,
                            "item_identity_verified": True,
                            "douyin_item_media": {
                                "media_id": media_id,
                                "video_uri": "verified-shaped-video-uri",
                                "minimum_width": 1440,
                                "minimum_height": 2560,
                                "direct_candidates": [
                                    {
                                        "width": 1440,
                                        "height": 2560,
                                        "urls": [direct_url],
                                    }
                                ],
                            },
                        },
                    )
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
            direct_url = item.metadata["douyin_item_media"]["direct_candidates"][0][
                "urls"
            ][0]
            self.download_urls.append(direct_url)
            if len(self.download_urls) == 1:
                raise RuntimeError("signed direct URL expired")
            return DownloadOutcome(
                output_paths=[str(Path(output_dir) / "target.mp4")],
                title=item.title,
                author="Verified author",
                media_type=MediaType.VIDEO,
                resolution="1440x2560",
            )

    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    engine = RefreshingDirectEngine()
    monkeypatch.setattr(manager, "_engine_for_job", lambda job: engine)
    try:
        created = manager.create_job(source_url, auto_start=True)
        failed = wait_for_job(manager, created.id)
        assert failed.status == JobStatus.FAILED

        manager.retry_failed(created.id)
        completed = wait_for_job(manager, created.id)

        assert completed.status == JobStatus.COMPLETED
        assert completed.items[0].resolution == "1440x2560"
        assert engine.discovery_calls == 2
        assert engine.download_urls == [
            "https://v26-web.douyinvod.com/signed-1.mp4",
            "https://v26-web.douyinvod.com/signed-2.mp4",
        ]
    finally:
        manager.shutdown()


def test_legacy_douyin_profile_snapshot_gets_owner_url_without_probe_warning(
    monkeypatch, tmp_path
) -> None:
    profile_url = "https://www.douyin.com/user/profile-a"
    captured_profile_urls = []

    class LegacyDouyinEngine:
        def download_item(self, item, platform, output_dir, *, callback, should_cancel):
            captured_profile_urls.append(item.metadata.get("profile_url"))
            callback(
                EngineEvent(
                    event="probing",
                    message="Checking Douyin quality 1/4: 4k",
                )
            )
            return DownloadOutcome(
                output_paths=[str(Path(output_dir) / "legacy.mp4")],
                title=item.title,
                upload_date="2025-11-14",
                author="Profile A",
                media_type=MediaType.VIDEO,
                selected_format="douyin-api-1080x1920-1",
                resolution="1080x1920",
            )

    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    monkeypatch.setattr(manager, "_engine_for_job", lambda job: LegacyDouyinEngine())

    try:
        created = manager.create_job(profile_url, auto_start=False)
        with manager._lock:
            job = manager._require_job(created.id)
            job.author = "Profile A"
            job.items = [
                DownloadItem(
                    id="legacy-item",
                    media_id="2222222222222222222",
                    source_url=("https://www.douyin.com/video/2222222222222222222"),
                    title="Legacy video",
                    media_type=MediaType.VIDEO,
                    metadata={},
                )
            ]
            job.refresh_counts()
            manager._commit_locked(job)

        manager.start_job(created.id)
        completed = wait_for_job(manager, created.id)

        assert completed.status == JobStatus.COMPLETED
        assert captured_profile_urls == [profile_url]
        assert completed.warning is None
        assert completed.items[0].progress.filename == "Checking Douyin quality 1/4: 4k"
    finally:
        manager.shutdown()


def test_restore_marks_unverified_legacy_douyin_results_for_manual_review(
    tmp_path,
) -> None:
    state_dir = tmp_path / "state"
    output_path = tmp_path / "downloads" / "legacy.mp4"
    job = DownloadJob(
        id="legacy-douyin-job",
        source_url="https://www.douyin.com/user/profile-a",
        platform=Platform.DOUYIN,
        source_kind=SourceKind.PROFILE,
        output_root=str(tmp_path / "downloads"),
        output_dir=str(tmp_path / "downloads"),
        status=JobStatus.COMPLETED,
        items=[
            DownloadItem(
                id="legacy-item",
                media_id="2222222222222222222",
                source_url="https://www.douyin.com/video/2222222222222222222",
                title="Legacy video",
                media_type=MediaType.VIDEO,
                status=ItemStatus.COMPLETED,
                output_paths=[str(output_path)],
                retryable=True,
                metadata={},
            )
        ],
        total_items=1,
        completed_items=1,
        discovery_complete=False,
    )
    JsonJobStore(state_dir).save(job)

    manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    try:
        restored = manager.get_job(job.id)
        item = restored.items[0]

        assert restored.status == JobStatus.FAILED
        assert restored.retryable is False
        assert item.status == ItemStatus.FAILED
        assert item.retryable is False
        assert item.output_paths == [str(output_path)]
        assert "manually reviewed" in (item.error or "")
        with pytest.raises(ItemNotRetryableError):
            manager.retry_item(job.id, item.id)
        with pytest.raises(ItemNotRetryableError):
            manager.retry_failed(job.id)
    finally:
        manager.shutdown()


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


def test_external_download_error_is_redacted_before_persistence(
    monkeypatch, tmp_path
) -> None:
    class SecretFailureEngine:
        def discover(self, url, platform, kind, *, should_cancel):
            return DiscoveryResult(
                author="Test author",
                items=[
                    DownloadItem(
                        id="secret-failure",
                        source_url="https://www.youtube.com/watch?v=abcdefghijk",
                    )
                ],
            )

        def download_item(self, item, platform, output_dir, *, callback, should_cancel):
            raise RuntimeError(
                "transfer failed at https://cdn.example/video?token=must-not-leak "
                "Cookie: top-secret"
            )

    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    monkeypatch.setattr(manager, "_engine_for_job", lambda job: SecretFailureEngine())

    try:
        created = manager.create_job(
            "https://www.youtube.com/watch?v=abcdefghijk",
            auto_start=True,
        )
        failed = wait_for_job(manager, created.id)
        persisted = (tmp_path / "state" / f"{created.id}.json").read_text(
            encoding="utf-8"
        )

        assert failed.status == JobStatus.FAILED
        assert "[redacted URL]" in (failed.items[0].error or "")
        assert "must-not-leak" not in (failed.items[0].error or "")
        assert "top-secret" not in (failed.items[0].error or "")
        assert "must-not-leak" not in persisted
        assert "top-secret" not in persisted
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


def test_incomplete_profile_with_only_completed_items_can_continue_discovery(
    monkeypatch,
    tmp_path,
) -> None:
    profile_url = "https://www.xiaohongshu.com/user/profile/example"
    existing_url = "https://www.xiaohongshu.com/explore/existing-note"
    new_url = "https://www.xiaohongshu.com/explore/new-note"

    class ContinuedDiscoveryEngine:
        def __init__(self) -> None:
            self.discovery_calls = 0
            self.download_calls: list[str] = []

        def discover(self, url, platform, kind, *, should_cancel):
            self.discovery_calls += 1
            assert url == profile_url
            assert kind == SourceKind.PROFILE
            return DiscoveryResult(
                author="Continued Author",
                items=[
                    DownloadItem(
                        id="existing-note",
                        media_id="existing-note",
                        source_url=existing_url,
                        title="Existing note",
                        media_type=MediaType.IMAGE,
                    ),
                    DownloadItem(
                        id="new-note",
                        media_id="new-note",
                        source_url=new_url,
                        title="New note",
                        media_type=MediaType.IMAGE,
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
            self.download_calls.append(item.id)
            return DownloadOutcome(
                output_paths=[str(Path(output_dir) / f"{item.id}.webp")],
                title=item.title,
                upload_date="2025-11-14",
                author="Continued Author",
                media_type=MediaType.IMAGE,
            )

    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    engine = ContinuedDiscoveryEngine()
    monkeypatch.setattr(manager, "_engine_for_job", lambda job: engine)

    try:
        created = manager.create_job(profile_url, auto_start=False)
        with manager._lock:
            job = manager._require_job(created.id)
            job.author = "Continued Author"
            job.status = JobStatus.PARTIAL
            job.discovery_complete = False
            job.items = [
                DownloadItem(
                    id="existing-note",
                    media_id="existing-note",
                    source_url=existing_url,
                    title="Existing note",
                    media_type=MediaType.IMAGE,
                    status=ItemStatus.COMPLETED,
                    output_paths=[str(tmp_path / "downloads" / "existing-note.webp")],
                )
            ]
            job.refresh_counts()
            manager._commit_locked(job)

        manager.retry_failed(created.id)
        completed = wait_for_job(manager, created.id)

        assert completed.status == JobStatus.COMPLETED
        assert completed.discovery_complete is True
        assert [item.status for item in completed.items] == [
            ItemStatus.COMPLETED,
            ItemStatus.COMPLETED,
        ]
        assert engine.discovery_calls == 1
        assert engine.download_calls == ["new-note"]
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


def test_cancel_while_temporary_limit_propagates_converges_to_cancelled(
    monkeypatch,
    tmp_path,
) -> None:
    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    discovery_started = threading.Event()
    release_discovery = threading.Event()

    class BlockingLimitedEngine:
        def discover(self, url, platform, kind, *, should_cancel):
            discovery_started.set()
            release_discovery.wait(timeout=5)
            raise TemporaryAccessError(
                "Douyin temporarily limited a signed request after automatic retries"
            )

    monkeypatch.setattr(
        manager,
        "_engine_for_job",
        lambda job: BlockingLimitedEngine(),
    )

    try:
        created = manager.create_job(
            "https://www.douyin.com/video/7664225419386607205",
            auto_start=True,
        )
        assert discovery_started.wait(timeout=2)

        cancelling = manager.cancel_job(created.id)
        assert cancelling.cancel_requested is True

        release_discovery.set()
        final = wait_for_job(manager, created.id)

        assert final.status == JobStatus.CANCELLED
        assert final.cancel_requested is False
        assert final.auth_message is None
        assert final.verification_url is None
    finally:
        release_discovery.set()
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
