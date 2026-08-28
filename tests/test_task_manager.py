from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app.downloader import (
    DiscoveryResult,
    DownloadOutcome,
    EngineEvent,
    safe_external_error_message,
)
from app.errors import (
    AuthenticationRequiredError,
    DiscoveryError,
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
from app.task_manager import (
    DOUYIN_PROFILE_REFRESH_REQUIRED_MARKER,
    DOUYIN_PROFILE_REDISCOVERY_ITEM_MARKER,
    DOUYIN_PROFILE_REMOVED_ITEM_MARKER,
    DOUYIN_PROFILE_REMOVED_PARTIAL_ITEM_MESSAGE,
    DOUYIN_PROFILE_REDISCOVERY_MESSAGE,
    DOUYIN_UNVERIFIABLE_QUEUE_ERROR,
    LEGACY_DOUYIN_MEDIA_REDIRECT_MARKER,
    LEGACY_DOUYIN_MEDIA_REDIRECT_MESSAGE,
    LEGACY_DOUYIN_SHORT_REDIRECT_MESSAGE,
    DownloadManager,
    ItemNotRetryableError,
    XIAOHONGSHU_BINDING_REDISCOVERY_MARKER,
)


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
                    media_id="6411cf99000000001300b6d9",
                    source_url=(
                        "https://www.xiaohongshu.com/explore/"
                        "6411cf99000000001300b6d9"
                    ),
                    title="Multi image note",
                    media_type=MediaType.IMAGE,
                    metadata={
                        "xiaohongshu_profile_id": "example",
                        "profile_note_membership_verified": True,
                    },
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
                    media_id="6411cf99000000001300b6d9",
                    source_url=(
                        "https://www.xiaohongshu.com/explore/"
                        "6411cf99000000001300b6d9"
                        f"?xsec_token=fresh-{self.discovery_calls}"
                    ),
                    title="Refreshable note",
                    media_type=MediaType.IMAGE,
                    metadata={
                        "xiaohongshu_profile_id": "example",
                        "profile_note_membership_verified": True,
                    },
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
        media_ids = {
            "first-note": "6411cf99000000001300b6d9",
            "second-note": "6411cf99000000001300b6da",
        }
        return DiscoveryResult(
            author="Selective Author",
            items=[
                DownloadItem(
                    id=item_id,
                    media_id=media_ids[item_id],
                    source_url=(
                        "https://www.xiaohongshu.com/explore/"
                        f"{media_ids[item_id]}"
                        "?xsec_token=fresh"
                    ),
                    title=item_id,
                    media_type=MediaType.IMAGE,
                    metadata={
                        "xiaohongshu_profile_id": "example",
                        "profile_note_membership_verified": True,
                    },
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


def douyin_direct_candidate(
    video_uri: str,
    *,
    width: int = 1440,
    height: int = 2560,
    url: str | None = None,
) -> dict:
    return {
        "video_uri": video_uri,
        "width": width,
        "height": height,
        "bit_rate": 20_000_000,
        "codec_hint": "hevc",
        "urls": [url or f"https://v26-web.douyinvod.com/{video_uri}.mp4"],
    }


def complete_douyin_item_metadata(
    source_url: str,
    media_id: str,
    *,
    video_uri: str = "verified-item-video-uri",
    direct_url: str | None = None,
) -> dict:
    return {
        "verification_url": source_url,
        "item_identity_verified": True,
        "douyin_item_media": {
            "media_id": media_id,
            "video_uri": video_uri,
            "minimum_width": 1440,
            "minimum_height": 2560,
            "direct_candidates": [
                douyin_direct_candidate(video_uri, url=direct_url)
            ],
        },
    }


def complete_douyin_profile_metadata(
    profile_url: str,
    media_id: str,
    *,
    title: str = "Verified profile video",
) -> dict:
    owner_id = profile_url.split("/user/", 1)[1].split("?", 1)[0]
    video_uri = f"verified-profile-video-{media_id}"
    return {
        "profile_url": profile_url,
        "profile_owner_verified": True,
        "douyin_profile_media": {
            "media_id": media_id,
            "owner_id": owner_id,
            "media_kind": "video",
            "video_uri": video_uri,
            "direct_candidates": [douyin_direct_candidate(video_uri)],
            "title": title,
        },
    }


@pytest.mark.parametrize(
    "source_url",
    [
        "https://www.douyin.com/video/7649279395044040154",
        (
            "https://www.douyin.com/user/self?from_tab_name=main"
            "&modal_id=7649279395044040154&showTab=favorite_collection"
        ),
    ],
)
def test_douyin_target_job_is_created_as_canonical_item(
    source_url: str,
    tmp_path,
) -> None:
    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    try:
        created = manager.create_job(source_url, auto_start=False)

        assert created.platform == Platform.DOUYIN
        assert created.source_kind == SourceKind.ITEM
        assert created.source_url == (
            "https://www.douyin.com/video/7649279395044040154"
        )
    finally:
        manager.shutdown()


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
        assert blocked.status == JobStatus.FAILED
        assert blocked.items == []
        assert blocked.total_items == 0
        assert blocked.verification_url is None
        assert blocked.auth_message is None
        assert "uploader profile" in (blocked.error or "")
    finally:
        manager.shutdown()


def test_douyin_profile_discovery_rejects_numeric_placeholders_before_commit(
    monkeypatch,
    tmp_path,
) -> None:
    profile_url = "https://www.douyin.com/user/verified-profile-owner"
    media_ids = [str(7670000000000000000 + index) for index in range(151)]

    class NumericPlaceholderEngine:
        def __init__(self) -> None:
            self.download_calls: list[str] = []

        def discover(self, url, platform, kind, *, should_cancel):
            assert url == profile_url
            assert platform == Platform.DOUYIN
            assert kind == SourceKind.PROFILE
            return DiscoveryResult(
                author="Verified author",
                items=[
                    DownloadItem(
                        id=media_id,
                        media_id=media_id,
                        source_url=f"https://www.douyin.com/video/{media_id}",
                        title=media_id,
                        media_type=MediaType.VIDEO,
                        metadata={
                            "profile_url": profile_url,
                            "profile_owner_verified": True,
                        },
                    )
                    for media_id in media_ids
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
            raise AssertionError("numeric placeholders must not reach download_item")

    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    engine = NumericPlaceholderEngine()
    events: list[str] = []
    manager.add_listener(lambda event, job: events.append(event.event))
    monkeypatch.setattr(manager, "_engine_for_job", lambda job: engine)

    try:
        created = manager.create_job(profile_url, auto_start=True)
        blocked = wait_for_job(manager, created.id)

        assert blocked.status == JobStatus.FAILED
        assert blocked.items == []
        assert blocked.total_items == 0
        assert blocked.discovery_complete is False
        assert blocked.verification_url is None
        assert blocked.auth_message is None
        assert blocked.retryable is True
        assert "incomplete verified media metadata" in (blocked.error or "")
        assert engine.download_calls == []
        assert "discovered" not in events
        persisted = JsonJobStore(tmp_path / "state").get(created.id)
        assert persisted.items == []
    finally:
        manager.shutdown()


def test_douyin_profile_discovery_rejects_empty_result_as_temporary() -> None:
    profile_url = "https://www.douyin.com/user/verified-profile-owner"
    job = DownloadJob(
        id="empty-profile-result",
        source_url=profile_url,
        platform=Platform.DOUYIN,
        source_kind=SourceKind.PROFILE,
        output_root="/tmp/downloads",
    )

    with pytest.raises(TemporaryAccessError, match="no verified media items"):
        DownloadManager._validate_discovery_result(
            job,
            DiscoveryResult(author="Verified author", items=[]),
        )


@pytest.mark.parametrize(
    ("source_url", "media_id"),
    [
        (
            "https://evil.example/explore/6411cf99000000001300b6d9",
            "6411cf99000000001300b6d9",
        ),
        (
            "http://www.xiaohongshu.com/explore/6411cf99000000001300b6d9",
            "6411cf99000000001300b6d9",
        ),
        (
            "https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9",
            "different-id",
        ),
    ],
)
def test_xiaohongshu_discovery_rejects_untrusted_or_cross_wired_items(
    source_url: str,
    media_id: str,
) -> None:
    job = DownloadJob(
        id="xhs-profile-validation",
        source_url="https://www.xiaohongshu.com/user/profile/expected",
        platform=Platform.XIAOHONGSHU,
        source_kind=SourceKind.PROFILE,
        output_root="/tmp/downloads",
    )

    with pytest.raises(DiscoveryError, match="cross-wired"):
        DownloadManager._validate_discovery_result(
            job,
            DiscoveryResult(
                author="Test Author",
                items=[
                    DownloadItem(
                        id="candidate",
                        media_id=media_id,
                        source_url=source_url,
                    )
                ],
            ),
        )


def test_xiaohongshu_discovery_accepts_one_bound_tokenized_note() -> None:
    note_id = "6411cf99000000001300b6d9"
    job = DownloadJob(
        id="xhs-item-validation",
        source_url=f"https://www.xiaohongshu.com/explore/{note_id}",
        platform=Platform.XIAOHONGSHU,
        source_kind=SourceKind.ITEM,
        output_root="/tmp/downloads",
    )

    DownloadManager._validate_discovery_result(
        job,
        DiscoveryResult(
            author="Test Author",
            items=[
                DownloadItem(
                    id="candidate",
                    media_id=note_id,
                    source_url=(
                        f"https://www.xiaohongshu.com/explore/{note_id}"
                        "?xsec_token=secret&xsec_source=pc_user"
                    ),
                )
            ],
        ),
    )


def test_xiaohongshu_direct_item_rejects_different_note_identity() -> None:
    expected_id = "6411cf99000000001300b6d9"
    different_id = "6411cf99000000001300b6da"
    job = DownloadJob(
        id="xhs-direct-crosswire",
        source_url=f"https://www.xiaohongshu.com/explore/{expected_id}",
        platform=Platform.XIAOHONGSHU,
        source_kind=SourceKind.ITEM,
        output_root="/tmp/downloads",
    )
    item = DownloadItem(
        id="different-note",
        media_id=different_id,
        source_url=f"https://www.xiaohongshu.com/explore/{different_id}",
    )

    assert DownloadManager._is_bound_xiaohongshu_item(job, item) is False
    with pytest.raises(DiscoveryError, match="cross-wired"):
        DownloadManager._validate_discovery_result(
            job,
            DiscoveryResult(author="Different Author", items=[item]),
        )


def test_xiaohongshu_short_item_uses_resolved_identity_binding() -> None:
    expected_id = "6411cf99000000001300b6d9"
    job = DownloadJob(
        id="xhs-short-binding",
        source_url="https://xhslink.com/a/short-code",
        platform=Platform.XIAOHONGSHU,
        source_kind=SourceKind.SHORT_LINK,
        output_root="/tmp/downloads",
    )
    item = DownloadItem(
        id="short-note",
        media_id=expected_id,
        source_url=f"https://www.xiaohongshu.com/explore/{expected_id}",
        metadata={
            "xiaohongshu_resolved_source_url": (
                f"https://www.xiaohongshu.com/explore/{expected_id}"
            ),
            "xiaohongshu_resolved_source_kind": SourceKind.ITEM.value,
        },
    )

    assert DownloadManager._is_bound_xiaohongshu_item(job, item) is True
    DownloadManager._validate_discovery_result(
        job,
        DiscoveryResult(author="Short Author", items=[item]),
    )

    item.metadata["xiaohongshu_resolved_source_url"] = (
        "https://www.xiaohongshu.com/explore/6411cf99000000001300b6da"
    )
    assert DownloadManager._is_bound_xiaohongshu_item(job, item) is False


def test_xiaohongshu_short_retry_blocks_changed_resolved_target() -> None:
    original_id = "6411cf99000000001300b6d9"
    changed_id = "6411cf99000000001300b6da"
    job = DownloadJob(
        id="xhs-short-anchor",
        source_url="https://xhslink.com/a/stable-short-code",
        platform=Platform.XIAOHONGSHU,
        source_kind=SourceKind.SHORT_LINK,
        resolved_source_kind=SourceKind.ITEM,
        resolved_source_id=original_id,
        output_root="/tmp/downloads",
    )
    changed_item = DownloadItem(
        id="changed-short-note",
        media_id=changed_id,
        source_url=f"https://www.xiaohongshu.com/explore/{changed_id}",
        metadata={
            "xiaohongshu_resolved_source_url": (
                f"https://www.xiaohongshu.com/explore/{changed_id}"
            ),
            "xiaohongshu_resolved_source_kind": SourceKind.ITEM.value,
        },
    )

    with pytest.raises(DiscoveryError, match="different note or profile"):
        DownloadManager._validate_discovery_result(
            job,
            DiscoveryResult(author="Changed Author", items=[changed_item]),
        )


def test_xiaohongshu_profile_discovery_requires_membership_binding() -> None:
    note_id = "6411cf99000000001300b6d9"
    job = DownloadJob(
        id="xhs-profile-membership",
        source_url="https://www.xiaohongshu.com/user/profile/expected",
        platform=Platform.XIAOHONGSHU,
        source_kind=SourceKind.PROFILE,
        output_root="/tmp/downloads",
    )
    item = DownloadItem(
        id="candidate",
        media_id=note_id,
        source_url=f"https://www.xiaohongshu.com/explore/{note_id}",
    )

    with pytest.raises(DiscoveryError, match="cross-wired"):
        DownloadManager._validate_discovery_result(
            job,
            DiscoveryResult(author="Profile Author", items=[item]),
        )

    item.metadata = {
        "xiaohongshu_profile_id": "expected",
        "profile_note_membership_verified": True,
    }
    DownloadManager._validate_discovery_result(
        job,
        DiscoveryResult(author="Profile Author", items=[item]),
    )


@pytest.mark.parametrize("retry_method", ["retry_item", "retry_failed"])
def test_xiaohongshu_direct_binding_failure_rediscovery_replaces_wrong_item(
    monkeypatch,
    tmp_path,
    retry_method: str,
) -> None:
    expected_id = "6411cf99000000001300b6d9"
    wrong_id = "6411cf99000000001300b6da"
    source_url = f"https://www.xiaohongshu.com/explore/{expected_id}"

    class RebindingEngine:
        def __init__(self) -> None:
            self.discovery_calls: list[tuple[str, Platform, SourceKind]] = []
            self.download_calls: list[str] = []

        def discover(self, url, platform, kind, *, should_cancel):
            self.discovery_calls.append((url, platform, kind))
            return DiscoveryResult(
                author="Correct Author",
                items=[
                    DownloadItem(
                        id="fresh-target",
                        media_id=expected_id,
                        source_url=f"{source_url}?xsec_token=fresh",
                        title="Correct note",
                        media_type=MediaType.IMAGE,
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
            self.download_calls.append(str(item.media_id))
            return DownloadOutcome(
                output_paths=[str(Path(output_dir) / "correct.webp")],
                title=item.title,
                author="Correct Author",
                media_type=MediaType.IMAGE,
            )

    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    engine = RebindingEngine()
    monkeypatch.setattr(manager, "_engine_for_job", lambda job: engine)

    try:
        created = manager.create_job(source_url, auto_start=False)
        with manager._lock:
            job = manager._require_job(created.id)
            job.items = [
                DownloadItem(
                    id="wrong-note",
                    media_id=wrong_id,
                    source_url=f"https://www.xiaohongshu.com/explore/{wrong_id}",
                    title="Wrong note",
                    status=ItemStatus.QUEUED,
                )
            ]
            job.discovery_complete = True
            manager._commit_locked(job)

        manager.start_job(created.id)
        blocked = wait_for_job(manager, created.id)

        assert blocked.status == JobStatus.FAILED
        assert blocked.discovery_complete is False
        assert blocked.items[0].metadata[
            XIAOHONGSHU_BINDING_REDISCOVERY_MARKER
        ] is True
        assert engine.download_calls == []

        if retry_method == "retry_item":
            manager.retry_item(created.id, "wrong-note")
        else:
            manager.retry_failed(created.id)
        completed = wait_for_job(manager, created.id)

        assert completed.status == JobStatus.COMPLETED
        assert engine.discovery_calls == [
            (source_url, Platform.XIAOHONGSHU, SourceKind.ITEM)
        ]
        assert engine.download_calls == [expected_id]
        assert len(completed.items) == 1
        assert completed.items[0].id == "fresh-target"
        assert completed.items[0].media_id == expected_id
        assert completed.items[0].status == ItemStatus.COMPLETED
        assert (
            XIAOHONGSHU_BINDING_REDISCOVERY_MARKER
            not in completed.items[0].metadata
        )
        assert all(item.media_id != wrong_id for item in completed.items)
    finally:
        manager.shutdown()


def test_xiaohongshu_short_binding_failure_retry_resolves_original_link(
    monkeypatch,
    tmp_path,
) -> None:
    short_url = "https://xhslink.com/a/original-short-code"
    expected_id = "6411cf99000000001300b6d9"
    wrong_id = "6411cf99000000001300b6da"
    expected_url = f"https://www.xiaohongshu.com/explore/{expected_id}"

    class ShortLinkRebindingEngine:
        def __init__(self) -> None:
            self.discovery_calls: list[tuple[str, Platform, SourceKind]] = []
            self.download_calls: list[str] = []

        def discover(self, url, platform, kind, *, should_cancel):
            self.discovery_calls.append((url, platform, kind))
            return DiscoveryResult(
                author="Short Author",
                items=[
                    DownloadItem(
                        id="fresh-short-target",
                        media_id=expected_id,
                        source_url=f"{expected_url}?xsec_token=fresh",
                        title="Resolved note",
                        media_type=MediaType.IMAGE,
                        metadata={
                            "xiaohongshu_resolved_source_url": expected_url,
                            "xiaohongshu_resolved_source_kind": (
                                SourceKind.ITEM.value
                            ),
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
            self.download_calls.append(str(item.media_id))
            return DownloadOutcome(
                output_paths=[str(Path(output_dir) / "resolved.webp")],
                title=item.title,
                author="Short Author",
                media_type=MediaType.IMAGE,
            )

    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    engine = ShortLinkRebindingEngine()
    monkeypatch.setattr(manager, "_engine_for_job", lambda job: engine)

    try:
        created = manager.create_job(short_url, auto_start=False)
        with manager._lock:
            job = manager._require_job(created.id)
            job.items = [
                DownloadItem(
                    id="wrong-short-note",
                    media_id=wrong_id,
                    source_url=f"https://www.xiaohongshu.com/explore/{wrong_id}",
                    title="Wrong short target",
                    metadata={
                        "xiaohongshu_resolved_source_url": expected_url,
                        "xiaohongshu_resolved_source_kind": SourceKind.ITEM.value,
                    },
                )
            ]
            job.refresh_counts()
            manager._commit_locked(job)

        manager.start_job(created.id)
        blocked = wait_for_job(manager, created.id)
        assert blocked.status == JobStatus.FAILED
        assert engine.download_calls == []

        manager.retry_failed(created.id)
        completed = wait_for_job(manager, created.id)

        assert completed.status == JobStatus.COMPLETED
        assert engine.discovery_calls == [
            (short_url, Platform.XIAOHONGSHU, SourceKind.SHORT_LINK)
        ]
        assert engine.download_calls == [expected_id]
        assert [item.media_id for item in completed.items] == [expected_id]
    finally:
        manager.shutdown()


def test_xiaohongshu_short_profile_retry_preserves_completed_history(
    monkeypatch,
    tmp_path,
) -> None:
    short_url = "https://xhslink.com/a/profile-short-code"
    profile_id = "5c99d4b30000000011015e6d"
    profile_url = f"https://www.xiaohongshu.com/user/profile/{profile_id}"
    completed_id = "6411cf99000000001300b6d9"
    retry_id = "6411cf99000000001300b6da"
    completed_path = tmp_path / "downloads" / "completed.webp"
    completed_path.parent.mkdir(parents=True)
    completed_path.write_bytes(b"preserved")

    def metadata() -> dict:
        return {
            "xiaohongshu_resolved_source_url": profile_url,
            "xiaohongshu_resolved_source_kind": SourceKind.PROFILE.value,
            "xiaohongshu_profile_id": profile_id,
            "profile_note_membership_verified": True,
        }

    class ProfileShortRetryEngine:
        def __init__(self) -> None:
            self.download_calls: list[str] = []

        def discover(self, url, platform, kind, *, should_cancel):
            assert (url, platform, kind) == (
                short_url,
                Platform.XIAOHONGSHU,
                SourceKind.SHORT_LINK,
            )
            return DiscoveryResult(
                author="Profile Author",
                items=[
                    DownloadItem(
                        id="fresh-retry-note",
                        media_id=retry_id,
                        source_url=(
                            f"https://www.xiaohongshu.com/explore/{retry_id}"
                            "?xsec_token=fresh"
                        ),
                        title="Fresh retry note",
                        media_type=MediaType.IMAGE,
                        metadata=metadata(),
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
            self.download_calls.append(str(item.media_id))
            return DownloadOutcome(
                output_paths=[str(Path(output_dir) / "fresh.webp")],
                title=item.title,
                author="Profile Author",
                media_type=MediaType.IMAGE,
            )

    state_dir = tmp_path / "state"
    manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    engine = ProfileShortRetryEngine()
    monkeypatch.setattr(manager, "_engine_for_job", lambda job: engine)
    try:
        created = manager.create_job(short_url, auto_start=False)
        with manager._lock:
            job = manager._require_job(created.id)
            job.resolved_source_kind = SourceKind.PROFILE
            job.resolved_source_id = profile_id
            job.status = JobStatus.FAILED
            job.discovery_complete = False
            job.items = [
                DownloadItem(
                    id="completed-note",
                    media_id=completed_id.upper(),
                    source_url=(
                        f"https://www.xiaohongshu.com/explore/{completed_id}"
                    ),
                    title="Completed note",
                    media_type=MediaType.IMAGE,
                    status=ItemStatus.COMPLETED,
                    output_paths=[str(completed_path)],
                    metadata=metadata(),
                ),
                DownloadItem(
                    id="failed-note",
                    media_id=retry_id,
                    source_url=(
                        f"https://www.xiaohongshu.com/explore/{retry_id}"
                    ),
                    title="Failed note",
                    media_type=MediaType.IMAGE,
                    status=ItemStatus.FAILED,
                    metadata={
                        **metadata(),
                        XIAOHONGSHU_BINDING_REDISCOVERY_MARKER: True,
                    },
                ),
            ]
            job.refresh_counts()
            manager._commit_locked(job)

        manager.retry_failed(created.id)
        completed = wait_for_job(manager, created.id)

        assert completed.status == JobStatus.COMPLETED
        assert engine.download_calls == [retry_id]
        assert len(completed.items) == 2
        preserved = next(
            item for item in completed.items if item.id == "completed-note"
        )
        assert preserved.media_id == completed_id
        assert preserved.status == ItemStatus.COMPLETED
        assert preserved.output_paths == [str(completed_path)]
        assert completed_path.read_bytes() == b"preserved"
    finally:
        manager.shutdown()

    restored_manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    try:
        restored = restored_manager.get_job(created.id)
        assert restored.status == JobStatus.COMPLETED
        assert len(restored.items) == 2
        assert next(
            item for item in restored.items if item.id == "completed-note"
        ).output_paths == [str(completed_path)]
    finally:
        restored_manager.shutdown()


def test_xiaohongshu_retry_normalizes_media_id_case_before_merge() -> None:
    profile_id = "5c99d4b30000000011015e6d"
    profile_url = f"https://www.xiaohongshu.com/user/profile/{profile_id}"
    note_id = "6411cf99000000001300b6d9"
    job = DownloadJob(
        id="xhs-case-normalization",
        source_url="https://xhslink.com/a/profile-short-code",
        platform=Platform.XIAOHONGSHU,
        source_kind=SourceKind.SHORT_LINK,
        resolved_source_kind=SourceKind.PROFILE,
        resolved_source_id=profile_id,
        output_root="/tmp/downloads",
    )
    common_metadata = {
        "xiaohongshu_resolved_source_url": profile_url,
        "xiaohongshu_resolved_source_kind": SourceKind.PROFILE.value,
        "xiaohongshu_profile_id": profile_id,
        "profile_note_membership_verified": True,
    }
    old = DownloadItem(
        id="old-note",
        media_id=note_id.upper(),
        source_url=f"https://www.xiaohongshu.com/explore/{note_id}",
        status=ItemStatus.COMPLETED,
        output_paths=["/tmp/completed.webp"],
        metadata=dict(common_metadata),
    )
    fresh = DownloadItem(
        id="fresh-note",
        media_id=note_id,
        source_url=f"https://www.xiaohongshu.com/explore/{note_id}",
        metadata=dict(common_metadata),
    )
    DownloadManager._normalize_xiaohongshu_item_identity(old)
    DownloadManager._normalize_xiaohongshu_item_identity(fresh)

    trusted = DownloadManager._trusted_xiaohongshu_previous_items(
        job,
        [old],
        [fresh],
    )
    merged = DownloadManager._merge_discovered_items(trusted, [fresh])

    assert len(merged) == 1
    assert merged[0].media_id == note_id
    assert merged[0].status == ItemStatus.COMPLETED
    assert merged[0].output_paths == ["/tmp/completed.webp"]


def test_persisted_xiaohongshu_direct_binding_failure_rediscovery_after_restart(
    monkeypatch,
    tmp_path,
) -> None:
    expected_id = "6411cf99000000001300b6d9"
    wrong_id = "6411cf99000000001300b6da"
    source_url = f"https://www.xiaohongshu.com/explore/{expected_id}"
    state_dir = tmp_path / "state"
    downloads_dir = tmp_path / "downloads"

    class UnexpectedDownloadEngine:
        def download_item(self, *args, **kwargs):
            raise AssertionError("Cross-wired item must not reach the downloader")

    first_manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=downloads_dir,
        max_workers=1,
    )
    monkeypatch.setattr(
        first_manager,
        "_engine_for_job",
        lambda job: UnexpectedDownloadEngine(),
    )
    try:
        created = first_manager.create_job(source_url, auto_start=False)
        with first_manager._lock:
            job = first_manager._require_job(created.id)
            job.items = [
                DownloadItem(
                    id="persisted-wrong-note",
                    media_id=wrong_id,
                    source_url=f"https://www.xiaohongshu.com/explore/{wrong_id}",
                    title="Persisted wrong note",
                )
            ]
            job.discovery_complete = True
            first_manager._commit_locked(job)

        first_manager.start_job(created.id)
        blocked = wait_for_job(first_manager, created.id)
        assert blocked.status == JobStatus.FAILED
        persisted = JsonJobStore(state_dir).get(created.id)
        assert persisted.items[0].metadata[
            XIAOHONGSHU_BINDING_REDISCOVERY_MARKER
        ] is True
    finally:
        first_manager.shutdown()

    class RestartRebindingEngine:
        def __init__(self) -> None:
            self.discovery_calls = 0
            self.download_calls: list[str] = []

        def discover(self, url, platform, kind, *, should_cancel):
            self.discovery_calls += 1
            assert url == source_url
            assert platform == Platform.XIAOHONGSHU
            assert kind == SourceKind.ITEM
            return DiscoveryResult(
                author="Restart Author",
                items=[
                    DownloadItem(
                        id="restart-fresh-target",
                        media_id=expected_id,
                        source_url=f"{source_url}?xsec_token=restart-fresh",
                        title="Restart fresh note",
                        media_type=MediaType.IMAGE,
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
            self.download_calls.append(str(item.media_id))
            return DownloadOutcome(
                output_paths=[str(Path(output_dir) / "restart.webp")],
                title=item.title,
                author="Restart Author",
                media_type=MediaType.IMAGE,
            )

    second_manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=downloads_dir,
        max_workers=1,
    )
    engine = RestartRebindingEngine()
    monkeypatch.setattr(second_manager, "_engine_for_job", lambda job: engine)
    try:
        restored = second_manager.get_job(created.id)
        assert restored.status == JobStatus.FAILED
        assert restored.discovery_complete is False
        assert restored.items[0].id == "persisted-wrong-note"

        second_manager.retry_item(created.id, "persisted-wrong-note")
        completed = wait_for_job(second_manager, created.id)

        assert completed.status == JobStatus.COMPLETED
        assert engine.discovery_calls == 1
        assert engine.download_calls == [expected_id]
        assert [item.id for item in completed.items] == ["restart-fresh-target"]
        assert [item.media_id for item in completed.items] == [expected_id]
    finally:
        second_manager.shutdown()


def test_persisted_xiaohongshu_profile_item_is_revalidated_before_download(
    monkeypatch,
    tmp_path,
) -> None:
    class UnexpectedDownloadEngine:
        def __init__(self) -> None:
            self.download_calls = 0

        def download_item(self, *args, **kwargs):
            self.download_calls += 1
            raise AssertionError("Untrusted item must not reach the downloader")

    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    engine = UnexpectedDownloadEngine()
    monkeypatch.setattr(manager, "_engine_for_job", lambda job: engine)
    try:
        created = manager.create_job(
            "https://www.xiaohongshu.com/user/profile/expected",
            auto_start=False,
        )
        with manager._lock:
            job = manager._require_job(created.id)
            job.items = [
                DownloadItem(
                    id="legacy-untrusted",
                    media_id="6411cf99000000001300b6d9",
                    source_url=(
                        "https://evil.example/explore/"
                        "6411cf99000000001300b6d9"
                    ),
                    status=ItemStatus.QUEUED,
                    metadata={
                        "xiaohongshu_profile_id": "expected",
                        "profile_note_membership_verified": True,
                    },
                )
            ]
            job.refresh_counts()
            manager._commit_locked(job)

        manager.start_job(created.id)
        failed = wait_for_job(manager, created.id)

        assert failed.status == JobStatus.FAILED
        assert failed.items[0].status == ItemStatus.FAILED
        assert "cross-wired entry was blocked" in (failed.items[0].error or "")
        assert engine.download_calls == 0
    finally:
        manager.shutdown()


def test_douyin_profile_local_cache_failure_is_retryable_without_chrome(
    monkeypatch,
    tmp_path,
) -> None:
    profile_url = "https://www.douyin.com/user/verified-profile-owner"
    media_id = "7670000000000000001"

    class CacheFailureEngine:
        def __init__(self) -> None:
            self.discovery_calls = 0
            self.download_calls = 0

        def discover(self, url, platform, kind, *, should_cancel):
            self.discovery_calls += 1
            return DiscoveryResult(
                author="Verified author",
                items=[
                    DownloadItem(
                        id=media_id,
                        media_id=media_id,
                        source_url=f"https://www.douyin.com/video/{media_id}",
                        title="Verified profile video",
                        media_type=MediaType.VIDEO,
                        metadata=complete_douyin_profile_metadata(
                            profile_url,
                            media_id,
                        ),
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
            self.download_calls += 1
            if self.download_calls == 1:
                raise MediaDownloadError(
                    "Douyin profile media metadata is incomplete; Chrome "
                    "verification is not required"
                )
            return DownloadOutcome(
                output_paths=[str(Path(output_dir) / f"{media_id}.mp4")],
                title=item.title,
                author="Verified author",
                media_type=MediaType.VIDEO,
            )

    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    engine = CacheFailureEngine()
    monkeypatch.setattr(manager, "_engine_for_job", lambda job: engine)

    try:
        created = manager.create_job(profile_url, auto_start=True)
        failed = wait_for_job(manager, created.id)

        assert failed.status == JobStatus.FAILED
        assert failed.items[0].status == ItemStatus.FAILED
        assert failed.items[0].retryable is True
        assert failed.auth_message is None
        assert failed.verification_url is None
        assert failed.items[0].auth_message is None

        manager.retry_failed(created.id)
        completed = wait_for_job(manager, created.id)

        assert completed.status == JobStatus.COMPLETED
        assert engine.discovery_calls == 2
        assert engine.download_calls == 2
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

        assert blocked.status == JobStatus.FAILED
        assert blocked.items == []
        assert blocked.verification_url is None
        assert blocked.auth_message is None
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
                        metadata=complete_douyin_item_metadata(
                            source_url,
                            media_id,
                            video_uri="verified-direct-video-uri",
                        ),
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


def test_restore_reclassifies_legacy_generic_douyin_signing_auth(
    tmp_path,
) -> None:
    state_dir = tmp_path / "state"
    profile_url = "https://www.douyin.com/user/verified-profile"
    legacy_message = (
        "Douyin could not create a verified signed request. Open the provided URL "
        "in Chrome, finish any CAPTCHA or login, then retry."
    )
    job = DownloadJob(
        id="legacy-generic-signing-auth",
        source_url=profile_url,
        platform=Platform.DOUYIN,
        source_kind=SourceKind.PROFILE,
        output_root=str(tmp_path / "downloads"),
        status=JobStatus.NEEDS_AUTH,
        error=legacy_message,
        auth_message=legacy_message,
        verification_url=profile_url,
        retryable=True,
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
        assert restored.auth_message is None
        assert restored.verification_url is None
        assert restored.discovery_complete is False
        assert restored.retryable is True
        assert "older version" in (restored.error or "")
        assert DownloadManager._should_rediscover_on_retry(restored) is True
    finally:
        manager.shutdown()


def test_restore_migrates_legacy_douyin_profile_redirect_for_rediscovery(
    monkeypatch,
    tmp_path,
) -> None:
    state_dir = tmp_path / "state"
    output_root = tmp_path / "downloads"
    profile_url = "https://www.douyin.com/user/verified-profile"
    preserved_file = output_root / "preserved.mp4"
    preserved_file.parent.mkdir(parents=True)
    preserved_file.write_bytes(b"preserved")
    legacy_message = (
        f"{LEGACY_DOUYIN_MEDIA_REDIRECT_MARKER}. The media response was blocked."
    )
    job = DownloadJob(
        id="legacy-profile-media-redirect",
        source_url=profile_url,
        platform=Platform.DOUYIN,
        source_kind=SourceKind.PROFILE,
        output_root=str(output_root),
        status=JobStatus.PARTIAL,
        error=legacy_message,
        warning=legacy_message,
        retryable=True,
        discovery_complete=True,
        items=[
            DownloadItem(
                id="preserved",
                media_id="7664225419386607205",
                    source_url="https://www.douyin.com/video/7664225419386607205",
                    title="Preserved",
                    media_type=MediaType.VIDEO,
                    status=ItemStatus.COMPLETED,
                    output_paths=[str(preserved_file)],
                    metadata=complete_douyin_profile_metadata(
                        profile_url,
                        "7664225419386607205",
                        title="Preserved",
                    ),
            ),
            DownloadItem(
                id="failed",
                media_id="7677923079457231738",
                source_url="https://www.douyin.com/video/7677923079457231738",
                title="Failed",
                status=ItemStatus.FAILED,
                error=legacy_message,
            ),
            DownloadItem(
                id="queued",
                media_id="7677554129950241521",
                source_url="https://www.douyin.com/video/7677554129950241521",
                title="Queued",
                status=ItemStatus.QUEUED,
            ),
        ],
    )
    JsonJobStore(state_dir).save(job)

    manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=output_root,
        max_workers=1,
    )
    restored_revision = None
    try:
        restored = manager.get_job(job.id)
        restored_revision = restored.revision

        assert restored.status == JobStatus.INTERRUPTED
        assert restored.discovery_complete is False
        assert restored.retryable is True
        assert restored.auth_message is None
        assert restored.verification_url is None
        assert len(restored.items) == 1
        assert restored.items[0].id == "preserved"
        assert restored.items[0].status == ItemStatus.COMPLETED
        assert restored.items[0].error is None
        assert restored.items[0].output_paths == [str(preserved_file)]
        assert preserved_file.read_bytes() == b"preserved"
        assert DOUYIN_PROFILE_REDISCOVERY_ITEM_MARKER not in restored.items[0].metadata
        assert LEGACY_DOUYIN_MEDIA_REDIRECT_MARKER not in restored.model_dump_json()
        assert DownloadManager._should_rediscover_on_retry(restored) is True
    finally:
        manager.shutdown()

    restarted_manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=output_root,
        max_workers=1,
    )

    class RediscoveryEngine:
        def __init__(self) -> None:
            self.discovery_urls: list[str] = []
            self.download_calls: list[str] = []

        def discover(self, url, platform, kind, *, should_cancel):
            self.discovery_urls.append(url)
            media_ids = [
                "7664225419386607205",
                "7677923079457231738",
                "7677554129950241521",
            ]
            return DiscoveryResult(
                author="Verified author",
                items=[
                    DownloadItem(
                        id=("preserved" if index == 0 else f"fresh-{media_id}"),
                        media_id=media_id,
                        source_url=f"https://www.douyin.com/video/{media_id}",
                        title=f"Fresh {index + 1}",
                        author="Verified author",
                        media_type=MediaType.VIDEO,
                        metadata=complete_douyin_profile_metadata(
                            profile_url,
                            media_id,
                            title=f"Fresh {index + 1}",
                        ),
                    )
                    for index, media_id in enumerate(media_ids)
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
            self.download_calls.append(item.media_id)
            output_path = Path(output_dir) / f"{item.media_id}.mp4"
            output_path.write_bytes(item.media_id.encode())
            return DownloadOutcome(
                output_paths=[str(output_path)],
                title=item.title,
                upload_date="2025-11-14",
                author="Verified author",
                media_type=MediaType.VIDEO,
                selected_format="douyin-api-1080x1920-1",
                resolution="1080x1920",
            )

    rediscovery_engine = RediscoveryEngine()
    monkeypatch.setattr(
        restarted_manager,
        "_engine_for_job",
        lambda job: rediscovery_engine,
    )
    try:
        restarted = restarted_manager.get_job(job.id)
        assert restarted.revision == restored_revision
        assert restarted.status == JobStatus.INTERRUPTED
        assert restarted.items[0].output_paths == [str(preserved_file)]

        restarted_manager.retry_failed(job.id)
        completed = wait_for_job(restarted_manager, job.id)

        assert completed.status == JobStatus.COMPLETED
        assert rediscovery_engine.discovery_urls == [profile_url]
        assert rediscovery_engine.download_calls == [
            "7677923079457231738",
            "7677554129950241521",
        ]
        assert completed.items[0].status == ItemStatus.COMPLETED
        assert completed.items[0].output_paths == [str(preserved_file)]
        assert preserved_file.read_bytes() == b"preserved"
    finally:
        restarted_manager.shutdown()


@pytest.mark.parametrize(
    "legacy_message",
    [
        LEGACY_DOUYIN_MEDIA_REDIRECT_MARKER,
        (
            "media endpoint redirected to an unrecognized Douyin CDN host "
            "(host: secret-token.edge.vendor-cdn.net)"
        ),
        (
            "Douyin media redirect could not be trusted. The task was paused "
            "before downloading later items. Redirect host: "
            "secret-token.edge.vendor-cdn.net"
        ),
    ],
)
def test_restore_migrates_legacy_douyin_item_redirect_for_rediscovery(
    legacy_message,
    tmp_path,
) -> None:
    state_dir = tmp_path / "state"
    output_root = tmp_path / "downloads"
    media_id = "7664225419386607205"
    source_url = f"https://www.douyin.com/video/{media_id}"
    job = DownloadJob(
        id="legacy-item-media-redirect",
        source_url=source_url,
        platform=Platform.DOUYIN,
        source_kind=SourceKind.ITEM,
        output_root=str(output_root),
        status=JobStatus.FAILED,
        error=legacy_message,
        retryable=True,
        discovery_complete=True,
        items=[
            DownloadItem(
                id="target",
                media_id=media_id,
                source_url=source_url,
                title="Target",
                status=ItemStatus.FAILED,
                error=legacy_message,
                metadata={
                    "item_identity_verified": True,
                    "douyin_item_media": {
                        "media_id": media_id,
                        "video_uri": "v0200fg10000staleitemmedia",
                        "direct_candidates": [
                            {
                                "video_uri": "v0200fg10000staleitemmedia",
                                "width": 1080,
                                "height": 1920,
                                "urls": [
                                    "https://v26-web.douyinvod.com/stale.mp4"
                                ],
                            }
                        ],
                    },
                },
            )
        ],
    )
    JsonJobStore(state_dir).save(job)

    manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=output_root,
        max_workers=1,
    )
    try:
        restored = manager.get_job(job.id)

        assert restored.status == JobStatus.INTERRUPTED
        assert restored.discovery_complete is False
        assert restored.retryable is True
        assert restored.auth_message is None
        assert restored.verification_url is None
        assert restored.error == LEGACY_DOUYIN_MEDIA_REDIRECT_MESSAGE
        assert restored.warning == LEGACY_DOUYIN_MEDIA_REDIRECT_MESSAGE
        assert restored.items[0].status == ItemStatus.FAILED
        assert restored.items[0].error == LEGACY_DOUYIN_MEDIA_REDIRECT_MESSAGE
        assert "douyin_item_media" not in restored.items[0].metadata
        assert "item_identity_verified" not in restored.items[0].metadata
        assert DownloadManager._should_rediscover_on_retry(restored) is True
        assert LEGACY_DOUYIN_MEDIA_REDIRECT_MARKER not in restored.model_dump_json()
        assert "secret-token" not in restored.model_dump_json()
        assert (
            "media endpoint redirected to an unrecognized Douyin CDN host"
            not in restored.model_dump_json()
        )
        assert (
            "Douyin media redirect could not be trusted"
            not in restored.model_dump_json()
        )
    finally:
        manager.shutdown()


def test_restore_marks_current_douyin_profile_redirect_for_safe_refresh(
    tmp_path,
) -> None:
    state_dir = tmp_path / "state"
    profile_url = "https://www.douyin.com/user/verified-profile"
    media_id = "7677923079457231738"
    redirect_error = (
        "Douyin media redirect could not be trusted. The task was paused before "
        "downloading later items. Redirect host: unavailable; Redirect host "
        "fingerprint: unavailable; Redirect port: unavailable; Redirect reason: "
        "nonstandard-port"
    )
    generic_job_error = "1 item(s) failed"
    job = DownloadJob(
        id="current-profile-media-redirect",
        source_url=profile_url,
        platform=Platform.DOUYIN,
        source_kind=SourceKind.PROFILE,
        output_root=str(tmp_path / "downloads"),
        status=JobStatus.INTERRUPTED,
        error=generic_job_error,
        retryable=False,
        discovery_complete=True,
        items=[
            DownloadItem(
                id="failed-live-photo",
                media_id=media_id,
                source_url=f"https://www.douyin.com/video/{media_id}",
                status=ItemStatus.FAILED,
                error=redirect_error,
                retryable=False,
                output_paths=[str(tmp_path / "downloads" / "image-01.webp")],
                metadata=complete_douyin_profile_metadata(
                    profile_url,
                    media_id,
                    title="Failed Live Photo",
                ),
            ),
            DownloadItem(
                id="queued-next",
                media_id="7677554129950241521",
                source_url="https://www.douyin.com/video/7677554129950241521",
                status=ItemStatus.QUEUED,
                metadata=complete_douyin_profile_metadata(
                    profile_url,
                    "7677554129950241521",
                    title="Queued next",
                ),
            ),
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

        assert restored.status == JobStatus.INTERRUPTED
        assert restored.retryable is True
        assert restored.discovery_complete is False
        assert restored.items[0].metadata[
            DOUYIN_PROFILE_REFRESH_REQUIRED_MARKER
        ] is True
        assert restored.items[0].output_paths == job.items[0].output_paths
        assert restored.items[1].status == ItemStatus.QUEUED
        assert restored.error == generic_job_error
    finally:
        manager.shutdown()


def test_restore_retires_unbound_legacy_douyin_short_redirect_idempotently(
    tmp_path,
) -> None:
    state_dir = tmp_path / "state"
    output_root = tmp_path / "downloads"
    short_url = "https://v.douyin.com/example/"
    preserved_file = output_root / "preserved-short.mp4"
    preserved_file.parent.mkdir(parents=True)
    preserved_file.write_bytes(b"preserved-short")
    legacy_message = LEGACY_DOUYIN_MEDIA_REDIRECT_MARKER
    job = DownloadJob(
        id="legacy-short-media-redirect",
        source_url=short_url,
        platform=Platform.DOUYIN,
        source_kind=SourceKind.SHORT_LINK,
        output_root=str(output_root),
        status=JobStatus.PARTIAL,
        error=legacy_message,
        retryable=True,
        discovery_complete=True,
        items=[
            DownloadItem(
                id="completed",
                media_id="7664225419386607205",
                source_url="https://www.douyin.com/video/7664225419386607205",
                title="Completed",
                status=ItemStatus.COMPLETED,
                output_paths=[str(preserved_file)],
            ),
            DownloadItem(
                id="failed",
                media_id="7677923079457231738",
                source_url="https://www.douyin.com/video/7677923079457231738",
                title="Failed",
                status=ItemStatus.FAILED,
                error=legacy_message,
            ),
            DownloadItem(
                id="queued",
                media_id="7677554129950241521",
                source_url="https://www.douyin.com/video/7677554129950241521",
                title="Queued",
                status=ItemStatus.QUEUED,
            ),
        ],
    )
    JsonJobStore(state_dir).save(job)

    manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=output_root,
        max_workers=1,
    )
    try:
        restored = manager.get_job(job.id)
        first_revision = restored.revision

        assert restored.status == JobStatus.PARTIAL
        assert restored.retryable is False
        assert restored.discovery_complete is False
        assert restored.error == LEGACY_DOUYIN_SHORT_REDIRECT_MESSAGE
        assert restored.warning == LEGACY_DOUYIN_SHORT_REDIRECT_MESSAGE
        assert restored.auth_message is None
        assert restored.verification_url is None
        assert restored.items[0].status == ItemStatus.COMPLETED
        assert restored.items[0].output_paths == [str(preserved_file)]
        assert restored.items[0].error is None
        assert all(item.retryable is False for item in restored.items[1:])
        assert all(
            item.error == LEGACY_DOUYIN_SHORT_REDIRECT_MESSAGE
            for item in restored.items[1:]
        )
        assert preserved_file.read_bytes() == b"preserved-short"
        assert LEGACY_DOUYIN_MEDIA_REDIRECT_MARKER not in restored.model_dump_json()
        with pytest.raises(ItemNotRetryableError):
            manager.retry_failed(job.id)
    finally:
        manager.shutdown()

    restarted_manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=output_root,
        max_workers=1,
    )
    try:
        restarted = restarted_manager.get_job(job.id)
        assert restarted.revision == first_revision
        assert restarted.status == JobStatus.PARTIAL
        assert restarted.items[0].output_paths == [str(preserved_file)]
    finally:
        restarted_manager.shutdown()


def test_restore_removes_unsaved_numeric_items_from_direct_video_expansion(
    monkeypatch,
    tmp_path,
) -> None:
    target_id = "7664225419386607205"
    source_url = f"https://www.douyin.com/video/{target_id}"
    state_dir = tmp_path / "state"
    media_ids = [target_id] + [
        str(7670000000000000000 + index) for index in range(150)
    ]
    job = DownloadJob(
        id="legacy-151-direct-items",
        source_url=source_url,
        platform=Platform.DOUYIN,
        source_kind=SourceKind.ITEM,
        output_root=str(tmp_path / "downloads"),
        status=JobStatus.NEEDS_AUTH,
        auth_message="Complete verification",
        verification_url="https://www.douyin.com/user/wrong-profile",
        items=[
            DownloadItem(
                id=media_id,
                media_id=media_id,
                source_url=f"https://www.douyin.com/video/{media_id}",
                title=media_id,
                status=(ItemStatus.NEEDS_AUTH if index == 0 else ItemStatus.QUEUED),
            )
            for index, media_id in enumerate(media_ids)
        ],
    )
    job.refresh_counts()
    JsonJobStore(state_dir).save(job)

    manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )

    class RediscoveredItemEngine:
        def discover(self, url, platform, kind, *, should_cancel):
            assert url == source_url
            assert kind == SourceKind.ITEM
            return DiscoveryResult(
                author="Correct author",
                items=[
                    DownloadItem(
                        id=target_id,
                        media_id=target_id,
                        source_url=source_url,
                        title="Correct rediscovered title",
                        media_type=MediaType.VIDEO,
                        metadata=complete_douyin_item_metadata(
                            source_url,
                            target_id,
                            video_uri="verified-video-uri",
                        ),
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
            assert item.media_id == target_id
            return DownloadOutcome(
                output_paths=[str(Path(output_dir) / "correct.mp4")],
                title=item.title,
                author="Correct author",
                media_type=MediaType.VIDEO,
                resolution="1440x2560",
            )

    monkeypatch.setattr(
        manager,
        "_engine_for_job",
        lambda restored_job: RediscoveredItemEngine(),
    )
    try:
        restored = manager.get_job(job.id)

        assert restored.status == JobStatus.INTERRUPTED
        assert restored.total_items == 0
        assert restored.items == []
        assert restored.auth_message is None
        assert restored.verification_url == source_url
        assert restored.discovery_complete is False
        assert restored.retryable is True

        manager.retry_failed(job.id)
        completed = wait_for_job(manager, job.id)

        assert completed.status == JobStatus.COMPLETED
        assert completed.total_items == 1
        assert completed.items[0].media_id == target_id
        assert completed.items[0].source_url == source_url
        assert completed.items[0].title == "Correct rediscovered title"
        assert completed.items[0].resolution == "1440x2560"
    finally:
        manager.shutdown()


def test_restore_discards_numeric_profile_placeholders_and_rediscovers(
    monkeypatch,
    tmp_path,
) -> None:
    state_dir = tmp_path / "state"
    profile_url = (
        "https://www.douyin.com/user/"
        "MS4wLjABAAAA9OBQVqfaEUOvYbk2U0bSMCmaGV9OiG5-"
        "k15gEXhWLuFzBhLejblDoYncRu6bRB-x"
    )
    media_ids = [str(7670000000000000000 + index) for index in range(151)]
    job = DownloadJob(
        id="legacy-151-lost-source",
        source_url=profile_url,
        platform=Platform.DOUYIN,
        source_kind=SourceKind.PROFILE,
        output_root=str(tmp_path / "downloads"),
        status=JobStatus.NEEDS_AUTH,
        auth_message="Complete verification",
        verification_url="https://evil.example/wrong-verification-target",
        items=[
            DownloadItem(
                id=media_id,
                media_id=media_id,
                source_url=f"https://www.douyin.com/video/{media_id}",
                title=media_id,
                status=(ItemStatus.NEEDS_AUTH if index == 0 else ItemStatus.QUEUED),
            )
            for index, media_id in enumerate(media_ids)
        ],
    )
    job.refresh_counts()
    JsonJobStore(state_dir).save(job)

    manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )

    class RediscoveredProfileEngine:
        def __init__(self) -> None:
            self.discovery_calls = 0

        def discover(self, url, platform, kind, *, should_cancel):
            self.discovery_calls += 1
            assert url == profile_url
            assert platform == Platform.DOUYIN
            assert kind == SourceKind.PROFILE
            media_id = media_ids[0]
            return DiscoveryResult(
                author="Recovered author",
                items=[
                    DownloadItem(
                        id="rediscovered-profile-item",
                        media_id=media_id,
                        source_url=f"https://www.douyin.com/video/{media_id}",
                        title="Recovered real title",
                        media_type=MediaType.VIDEO,
                        metadata=complete_douyin_profile_metadata(
                            profile_url,
                            media_id,
                            title="Recovered real title",
                        ),
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
            return DownloadOutcome(
                output_paths=[str(Path(output_dir) / "rediscovered.mp4")],
                title=item.title,
                author="Recovered author",
                media_type=MediaType.VIDEO,
                resolution="1440x2560",
            )

    engine = RediscoveredProfileEngine()
    monkeypatch.setattr(manager, "_engine_for_job", lambda current: engine)
    try:
        restored = manager.get_job(job.id)

        assert restored.status == JobStatus.INTERRUPTED
        assert restored.total_items == 0
        assert restored.items == []
        assert restored.auth_message is None
        assert restored.verification_url == profile_url
        assert restored.discovery_complete is False
        assert restored.retryable is True
        assert restored.error == DOUYIN_PROFILE_REDISCOVERY_MESSAGE

        manager.retry_failed(job.id)
        completed = wait_for_job(manager, job.id)

        assert completed.status == JobStatus.COMPLETED
        assert completed.total_items == 1
        assert completed.items[0].title == "Recovered real title"
        assert completed.items[0].resolution == "1440x2560"
        assert engine.discovery_calls == 1
    finally:
        manager.shutdown()


def test_restore_upgrades_previously_quarantined_profile_for_rediscovery(
    tmp_path,
) -> None:
    state_dir = tmp_path / "state"
    profile_id = (
        "MS4wLjABAAAA9OBQVqfaEUOvYbk2U0bSMCmaGV9OiG5-"
        "k15gEXhWLuFzBhLejblDoYncRu6bRB-x"
    )
    submitted_url = (
        f"https://www.douyin.com/user/{profile_id}?from_tab_name=main"
    )
    canonical_url = f"https://www.douyin.com/user/{profile_id}"
    job = DownloadJob(
        id="previously-quarantined-profile",
        source_url=submitted_url,
        platform=Platform.DOUYIN,
        source_kind=SourceKind.PROFILE,
        output_root=str(tmp_path / "downloads"),
        status=JobStatus.FAILED,
        error=DOUYIN_UNVERIFIABLE_QUEUE_ERROR,
        warning=DOUYIN_UNVERIFIABLE_QUEUE_ERROR,
        retryable=False,
        discovery_complete=False,
        items=[],
    )
    JsonJobStore(state_dir).save(job)

    first_manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    first = first_manager.get_job(job.id)
    first_manager.shutdown()

    assert first.source_url == canonical_url
    assert first.status == JobStatus.INTERRUPTED
    assert first.retryable is True
    assert first.discovery_complete is False
    assert first.error == DOUYIN_PROFILE_REDISCOVERY_MESSAGE
    assert first.warning == DOUYIN_PROFILE_REDISCOVERY_MESSAGE
    assert first.auth_message is None
    assert first.verification_url == canonical_url
    assert first.items == []

    second_manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    try:
        second = second_manager.get_job(job.id)

        assert second.model_dump(mode="json") == first.model_dump(mode="json")
    finally:
        second_manager.shutdown()


def test_restore_upgrades_quarantined_profile_and_preserves_existing_files(
    tmp_path,
) -> None:
    state_dir = tmp_path / "state"
    output_path = tmp_path / "downloads" / "preserved.mp4"
    output_path.parent.mkdir(parents=True)
    output_path.write_bytes(b"preserved-original-bytes")
    media_id = "7664225419386607205"
    profile_url = (
        "https://www.douyin.com/user/"
        "MS4wLjABAAAA9OBQVqfaEUOvYbk2U0bSMCmaGV9OiG5-"
        "k15gEXhWLuFzBhLejblDoYncRu6bRB-x"
    )
    job = DownloadJob(
        id="previously-quarantined-profile-with-file",
        source_url=profile_url,
        platform=Platform.DOUYIN,
        source_kind=SourceKind.PROFILE,
        output_root=str(tmp_path / "downloads"),
        status=JobStatus.FAILED,
        error=DOUYIN_UNVERIFIABLE_QUEUE_ERROR,
        warning=DOUYIN_UNVERIFIABLE_QUEUE_ERROR,
        retryable=False,
        discovery_complete=False,
        items=[
            DownloadItem(
                id=media_id,
                media_id=media_id,
                source_url=f"https://www.douyin.com/video/{media_id}",
                title=media_id,
                status=ItemStatus.FAILED,
                retryable=False,
                output_paths=[str(output_path)],
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

        assert restored.status == JobStatus.INTERRUPTED
        assert restored.retryable is True
        assert len(restored.items) == 1
        assert restored.items[0].output_paths == [str(output_path)]
        assert restored.items[0].retryable is False
        assert restored.items[0].metadata[
            DOUYIN_PROFILE_REDISCOVERY_ITEM_MARKER
        ] is True
        assert output_path.read_bytes() == b"preserved-original-bytes"
    finally:
        manager.shutdown()


@pytest.mark.parametrize(
    ("job_status", "item_status"),
    [
        (JobStatus.FAILED, ItemStatus.FAILED),
        (JobStatus.CANCELLED, ItemStatus.CANCELLED),
    ],
)
def test_restore_migrates_terminal_numeric_profile_queue_for_rediscovery(
    job_status,
    item_status,
    tmp_path,
) -> None:
    state_dir = tmp_path / "state"
    job = DownloadJob(
        id=f"legacy-terminal-{job_status.value}",
        source_url="https://www.douyin.com/user/legacy-profile",
        platform=Platform.DOUYIN,
        source_kind=SourceKind.PROFILE,
        output_root=str(tmp_path / "downloads"),
        status=job_status,
        items=[
            DownloadItem(
                id=media_id,
                media_id=media_id,
                source_url=f"https://www.douyin.com/video/{media_id}",
                title=media_id,
                status=item_status,
                metadata={"profile_owner_verified": "true"},
            )
            for media_id in ("7670000000000000001", "7670000000000000002")
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

        assert restored.status == JobStatus.INTERRUPTED
        assert restored.items == []
        assert restored.retryable is True
        assert restored.discovery_complete is False
        assert restored.verification_url == job.source_url
        assert restored.error == DOUYIN_PROFILE_REDISCOVERY_MESSAGE
    finally:
        manager.shutdown()


def test_restore_quarantines_numeric_queue_without_a_recoverable_profile_source(
    tmp_path,
) -> None:
    state_dir = tmp_path / "state"
    job = DownloadJob(
        id="legacy-numeric-queue-without-profile-source",
        source_url="https://example.com/lost-source",
        platform=Platform.DOUYIN,
        source_kind=SourceKind.PROFILE,
        output_root=str(tmp_path / "downloads"),
        status=JobStatus.NEEDS_AUTH,
        items=[
            DownloadItem(
                id=media_id,
                media_id=media_id,
                source_url=f"https://www.douyin.com/video/{media_id}",
                title=media_id,
                status=ItemStatus.NEEDS_AUTH,
            )
            for media_id in ("7670000000000000001", "7670000000000000002")
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

        assert restored.status == JobStatus.FAILED
        assert restored.items == []
        assert restored.retryable is False
        assert restored.verification_url is None
        assert "unverified numeric queue" in (restored.error or "")
    finally:
        manager.shutdown()

    second_manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    try:
        second = second_manager.get_job(job.id)

        assert second.model_dump(mode="json") == restored.model_dump(mode="json")
    finally:
        second_manager.shutdown()


@pytest.mark.parametrize(
    "source_url",
    [
        "http://www.douyin.com/user/MS4wLjABAAAATEST",
        "https://foo.douyin.com/user/MS4wLjABAAAATEST",
        "https://www.douyin.com:444/user/MS4wLjABAAAATEST",
        "https://www.douyin.com/user/%2F",
        "https://www.douyin.com/user/short",
        "https://www.douyin.com/user/MS4wLjABAAAATEST?modal_id=7664225419386607205",
        "https://www.douyin.com/video/7664225419386607205",
        "https://www.douyin.com/user/MS4wLjABAAAATEST/extra",
        "https://user@www.douyin.com/user/MS4wLjABAAAATEST",
    ],
)
def test_legacy_profile_rediscovery_rejects_untrusted_source_anchors(
    source_url,
    tmp_path,
) -> None:
    state_dir = tmp_path / "state"
    output_path = tmp_path / "preserved-wrong-output.mp4"
    output_path.write_bytes(b"must-remain-unchanged")
    preserved_media_id = "7670000000000000001"
    job = DownloadJob(
        id="untrusted-profile-anchor",
        source_url=source_url,
        platform=Platform.DOUYIN,
        source_kind=SourceKind.PROFILE,
        output_root=str(tmp_path),
        status=JobStatus.FAILED,
        error=DOUYIN_UNVERIFIABLE_QUEUE_ERROR,
        warning=DOUYIN_UNVERIFIABLE_QUEUE_ERROR,
        retryable=False,
        discovery_complete=False,
        verification_url="https://evil.example/stale-verification",
        items=[
            DownloadItem(
                id=preserved_media_id,
                media_id=preserved_media_id,
                source_url=(
                    f"https://www.douyin.com/video/{preserved_media_id}"
                ),
                title="Preserved unverified file",
                status=ItemStatus.FAILED,
                retryable=False,
                output_paths=[str(output_path)],
            )
        ],
    )
    job.refresh_counts()

    assert DownloadManager._recoverable_douyin_profile_source(job) is None
    JsonJobStore(state_dir).save(job)

    manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    try:
        restored = manager.get_job(job.id)

        assert restored.source_url == source_url
        assert restored.source_kind == SourceKind.PROFILE
        assert restored.status == JobStatus.FAILED
        assert restored.retryable is False
        assert restored.discovery_complete is False
        assert restored.error == DOUYIN_UNVERIFIABLE_QUEUE_ERROR
        assert restored.verification_url == "https://evil.example/stale-verification"
        assert len(restored.items) == 1
        assert restored.items[0].output_paths == [str(output_path)]
        assert restored.items[0].retryable is False
        assert output_path.read_bytes() == b"must-remain-unchanged"
    finally:
        manager.shutdown()


def test_restore_preserves_files_and_rediscovers_mixed_numeric_profile_queue(
    tmp_path,
) -> None:
    state_dir = tmp_path / "state"
    output_path = tmp_path / "downloads" / "verified.mp4"
    output_path.parent.mkdir(parents=True)
    output_path.write_bytes(b"preserved")
    verified_id = "7664225419386607205"
    unverified_ids = [str(7670000000000000000 + index) for index in range(150)]
    job = DownloadJob(
        id="legacy-mixed-profile-queue",
        source_url="https://www.douyin.com/user/legacy-profile",
        platform=Platform.DOUYIN,
        source_kind=SourceKind.PROFILE,
        output_root=str(tmp_path / "downloads"),
        status=JobStatus.NEEDS_AUTH,
        items=[
            DownloadItem(
                id=verified_id,
                media_id=verified_id,
                source_url=f"https://www.douyin.com/video/{verified_id}",
                title="Verified title",
                status=ItemStatus.COMPLETED,
                output_paths=[str(output_path)],
                metadata={"profile_owner_verified": True},
            ),
            *[
                DownloadItem(
                    id=media_id,
                    media_id=media_id,
                    source_url=f"https://www.douyin.com/video/{media_id}",
                    title=media_id,
                    status=ItemStatus.QUEUED,
                )
                for media_id in unverified_ids
            ],
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

        assert restored.status == JobStatus.INTERRUPTED
        assert restored.error == DOUYIN_PROFILE_REDISCOVERY_MESSAGE
        assert restored.warning == restored.error
        assert restored.auth_message is None
        assert restored.verification_url == job.source_url
        assert restored.retryable is True
        assert restored.discovery_complete is False
        assert restored.total_items == 1
        assert restored.items[0].output_paths == [str(output_path)]
        assert restored.items[0].retryable is False
        assert (
            restored.items[0].metadata[DOUYIN_PROFILE_REDISCOVERY_ITEM_MARKER]
            is True
        )
        assert output_path.exists()
    finally:
        manager.shutdown()

    second_manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    try:
        second = second_manager.get_job(job.id)

        assert second.model_dump(mode="json") == restored.model_dump(mode="json")
        assert output_path.read_bytes() == b"preserved"
    finally:
        second_manager.shutdown()


def test_restore_repairs_owner_verified_numeric_queue_and_rediscovery(
    monkeypatch,
    tmp_path,
) -> None:
    state_dir = tmp_path / "state"
    profile_url = "https://www.douyin.com/user/verified-profile"
    media_ids = [str(7670000000000000000 + index) for index in range(151)]
    job = DownloadJob(
        id="incomplete-owner-verified-profile",
        source_url=profile_url,
        platform=Platform.DOUYIN,
        source_kind=SourceKind.PROFILE,
        output_root=str(tmp_path / "downloads"),
        status=JobStatus.NEEDS_AUTH,
        auth_message="Complete verification",
        verification_url=profile_url,
        items=[
            DownloadItem(
                id=media_id,
                media_id=media_id,
                source_url=f"https://www.douyin.com/video/{media_id}",
                title=media_id,
                status=(ItemStatus.NEEDS_AUTH if index == 0 else ItemStatus.QUEUED),
                metadata={
                    "profile_url": profile_url,
                    "profile_owner_verified": True,
                },
            )
            for index, media_id in enumerate(media_ids)
        ],
    )
    job.refresh_counts()
    JsonJobStore(state_dir).save(job)

    class RediscoveredProfileEngine:
        def __init__(self) -> None:
            self.discovery_calls = 0
            self.download_calls = 0

        def discover(self, url, platform, kind, *, should_cancel):
            self.discovery_calls += 1
            media_id = media_ids[0]
            return DiscoveryResult(
                author="Verified author",
                items=[
                    DownloadItem(
                        id="rediscovered-item",
                        media_id=media_id,
                        source_url=f"https://www.douyin.com/video/{media_id}",
                        title="Rediscovered title",
                        media_type=MediaType.VIDEO,
                        metadata=complete_douyin_profile_metadata(
                            profile_url,
                            media_id,
                            title="Rediscovered title",
                        ),
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
            self.download_calls += 1
            return DownloadOutcome(
                output_paths=[str(Path(output_dir) / "rediscovered.mp4")],
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
    engine = RediscoveredProfileEngine()
    monkeypatch.setattr(manager, "_engine_for_job", lambda current: engine)
    try:
        restored = manager.get_job(job.id)

        assert restored.status == JobStatus.INTERRUPTED
        assert restored.items == []
        assert restored.total_items == 0
        assert restored.auth_message is None
        assert restored.verification_url == profile_url
        assert restored.discovery_complete is False
        assert restored.retryable is True

        manager.retry_failed(job.id)
        completed = wait_for_job(manager, job.id)

        assert completed.status == JobStatus.COMPLETED
        assert completed.total_items == 1
        assert completed.items[0].title == "Rediscovered title"
        assert completed.items[0].resolution == "1440x2560"
        assert engine.discovery_calls == 1
        assert engine.download_calls == 1
    finally:
        manager.shutdown()


def test_restore_repairs_numeric_item_titles_with_complete_profile_cache(
    tmp_path,
) -> None:
    state_dir = tmp_path / "state"
    profile_url = "https://www.douyin.com/user/verified-profile"
    media_ids = ("7670000000000000001", "7670000000000000002")
    job = DownloadJob(
        id="complete-numeric-title-profile",
        source_url=profile_url,
        platform=Platform.DOUYIN,
        source_kind=SourceKind.PROFILE,
        output_root=str(tmp_path / "downloads"),
        status=JobStatus.NEEDS_AUTH,
        items=[
            DownloadItem(
                id=media_id,
                media_id=media_id,
                source_url=f"https://www.douyin.com/video/{media_id}",
                title=media_id,
                status=ItemStatus.NEEDS_AUTH,
                metadata=complete_douyin_profile_metadata(
                    profile_url,
                    media_id,
                    title=f"Cached title {index}",
                ),
            )
            for index, media_id in enumerate(media_ids, start=1)
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

        assert restored.status == JobStatus.NEEDS_AUTH
        assert restored.total_items == 2
        assert [item.title for item in restored.items] == [
            "Cached title 1",
            "Cached title 2",
        ]
        assert restored.retryable is True
    finally:
        manager.shutdown()


def test_restore_repairs_even_one_owner_verified_numeric_placeholder(tmp_path) -> None:
    state_dir = tmp_path / "state"
    profile_url = "https://www.douyin.com/user/one-video-profile"
    media_id = "7670000000000000001"
    job = DownloadJob(
        id="single-incomplete-profile-placeholder",
        source_url=profile_url,
        platform=Platform.DOUYIN,
        source_kind=SourceKind.PROFILE,
        output_root=str(tmp_path / "downloads"),
        status=JobStatus.NEEDS_AUTH,
        items=[
            DownloadItem(
                id=media_id,
                media_id=media_id,
                source_url=f"https://www.douyin.com/video/{media_id}",
                title=media_id,
                status=ItemStatus.NEEDS_AUTH,
                metadata={
                    "profile_url": profile_url,
                    "profile_owner_verified": True,
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

        assert restored.status == JobStatus.INTERRUPTED
        assert restored.items == []
        assert restored.retryable is True
        assert restored.discovery_complete is False
        assert restored.verification_url == profile_url
    finally:
        manager.shutdown()


@pytest.mark.parametrize("media_kind", ["video", "live_photo"])
def test_restore_migrates_legacy_douyin_profile_media_without_direct_candidates(
    tmp_path,
    media_kind: str,
) -> None:
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "downloads"
    output_dir.mkdir(parents=True)
    profile_url = "https://www.douyin.com/user/verified-profile"
    owner_id = "verified-profile"
    media_id = "7670000000000000001"
    preserved_path = output_dir / f"preserved-{media_kind}.bin"
    preserved_path.write_bytes(b"preserved-before-direct-renditions")

    if media_kind == "video":
        cached_media = {
            "media_id": media_id,
            "owner_id": owner_id,
            "media_kind": "video",
            "video_uri": "legacy-profile-video-uri",
            "title": "Legacy cached video",
        }
    else:
        cached_media = {
            "media_id": media_id,
            "owner_id": owner_id,
            "media_kind": "image",
            "title": "Legacy cached Live Photo",
            "image_assets": [
                {
                    "index": 1,
                    "width": 1440,
                    "height": 2560,
                    "candidates": [
                        "https://p3-sign.douyinpic.com/legacy-image.jpeg"
                    ],
                }
            ],
            "live_photo_assets": [
                {
                    "index": 1,
                    "width": 1080,
                    "height": 1920,
                    "candidates": [
                        "https://v26-web.douyinvod.com/legacy-live-photo.mp4"
                    ],
                    "video_uri": "legacy-live-video-uri",
                    "duration_ms": 1800,
                }
            ],
        }

    job = DownloadJob(
        id=f"legacy-profile-{media_kind}-without-direct",
        source_url=profile_url,
        platform=Platform.DOUYIN,
        source_kind=SourceKind.PROFILE,
        output_root=str(output_dir),
        status=JobStatus.NEEDS_AUTH,
        verification_url=profile_url,
        items=[
            DownloadItem(
                id=media_id,
                media_id=media_id,
                source_url=f"https://www.douyin.com/video/{media_id}",
                title=f"Legacy {media_kind}",
                media_type=(
                    MediaType.VIDEO
                    if media_kind == "video"
                    else MediaType.IMAGE
                ),
                status=ItemStatus.NEEDS_AUTH,
                output_paths=[str(preserved_path)],
                metadata={
                    "profile_url": profile_url,
                    "profile_owner_verified": True,
                    "douyin_profile_media": cached_media,
                },
            )
        ],
    )
    job.refresh_counts()
    JsonJobStore(state_dir).save(job)

    manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=output_dir,
        max_workers=1,
    )
    try:
        restored = manager.get_job(job.id)

        assert restored.status == JobStatus.INTERRUPTED
        assert restored.discovery_complete is False
        assert restored.retryable is True
        assert restored.verification_url == profile_url
        assert restored.error == DOUYIN_PROFILE_REDISCOVERY_MESSAGE
        assert restored.total_items == 1
        assert restored.items[0].status == ItemStatus.FAILED
        assert restored.items[0].retryable is False
        assert restored.items[0].output_paths == [str(preserved_path)]
        assert restored.items[0].metadata[
            DOUYIN_PROFILE_REDISCOVERY_ITEM_MARKER
        ] is True
        assert preserved_path.read_bytes() == b"preserved-before-direct-renditions"
    finally:
        manager.shutdown()


def test_incomplete_owner_verified_profile_migration_preserves_files_and_is_idempotent(
    tmp_path,
) -> None:
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "downloads"
    output_dir.mkdir(parents=True)
    profile_url = "https://www.douyin.com/user/verified-profile"
    saved_id = "7670000000000000001"
    queued_ids = ("7670000000000000002", "7670000000000000003")
    saved_path = output_dir / "saved.mp4"
    saved_path.write_bytes(b"preserved")
    shared_metadata = {
        "profile_url": profile_url,
        "profile_owner_verified": True,
    }
    job = DownloadJob(
        id="idempotent-incomplete-profile",
        source_url=profile_url,
        platform=Platform.DOUYIN,
        source_kind=SourceKind.PROFILE,
        output_root=str(output_dir),
        status=JobStatus.NEEDS_AUTH,
        items=[
            DownloadItem(
                id=saved_id,
                media_id=saved_id,
                source_url=f"https://www.douyin.com/video/{saved_id}",
                title=saved_id,
                status=ItemStatus.COMPLETED,
                output_paths=[str(saved_path)],
                metadata=dict(shared_metadata),
            ),
            *[
                DownloadItem(
                    id=media_id,
                    media_id=media_id,
                    source_url=f"https://www.douyin.com/video/{media_id}",
                    title=media_id,
                    status=ItemStatus.QUEUED,
                    metadata=dict(shared_metadata),
                )
                for media_id in queued_ids
            ],
        ],
    )
    job.refresh_counts()
    JsonJobStore(state_dir).save(job)

    first_manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=output_dir,
        max_workers=1,
    )
    first = first_manager.get_job(job.id)
    first_manager.shutdown()

    assert first.status == JobStatus.INTERRUPTED
    assert first.discovery_complete is False
    assert first.retryable is True
    assert len(first.items) == 1
    assert first.items[0].output_paths == [str(saved_path)]
    assert first.items[0].status == ItemStatus.FAILED
    assert first.items[0].retryable is False
    assert first.items[0].title == f"Recovered Douyin files {saved_id}"
    assert first.items[0].metadata["_douyin_profile_rediscovery_pending"] is True
    assert saved_path.read_bytes() == b"preserved"

    second_manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=output_dir,
        max_workers=1,
    )
    try:
        second = second_manager.get_job(job.id)

        assert second.model_dump(mode="json") == first.model_dump(mode="json")
        assert saved_path.read_bytes() == b"preserved"
    finally:
        second_manager.shutdown()


def test_profile_rediscovery_marker_preserves_needs_auth_across_restart(
    tmp_path,
) -> None:
    state_dir = tmp_path / "state"
    profile_url = "https://www.douyin.com/user/verified-profile"
    media_id = "7670000000000000001"
    auth_message = "Explicit CAPTCHA is required"
    job = DownloadJob(
        id="profile-rediscovery-needs-auth",
        source_url=profile_url,
        platform=Platform.DOUYIN,
        source_kind=SourceKind.PROFILE,
        output_root=str(tmp_path / "downloads"),
        status=JobStatus.NEEDS_AUTH,
        error=auth_message,
        warning=DOUYIN_PROFILE_REDISCOVERY_MESSAGE,
        auth_message=auth_message,
        verification_url=profile_url,
        discovery_complete=False,
        items=[
            DownloadItem(
                id=media_id,
                media_id=media_id,
                source_url=f"https://www.douyin.com/video/{media_id}",
                title=f"Recovered Douyin files {media_id}",
                status=ItemStatus.FAILED,
                retryable=False,
                output_paths=[str(tmp_path / "downloads" / "preserved.mp4")],
                metadata={
                    "profile_url": profile_url,
                    "profile_owner_verified": True,
                    DOUYIN_PROFILE_REDISCOVERY_ITEM_MARKER: True,
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

        assert restored.status == JobStatus.NEEDS_AUTH
        assert restored.auth_message == auth_message
        assert restored.verification_url == profile_url
        assert restored.items[0].metadata[
            DOUYIN_PROFILE_REDISCOVERY_ITEM_MARKER
        ] is True
    finally:
        manager.shutdown()


def test_profile_rediscovery_marker_survives_partial_discovery_pause(
    monkeypatch,
    tmp_path,
) -> None:
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "downloads"
    output_dir.mkdir(parents=True)
    profile_url = "https://www.douyin.com/user/verified-profile"
    media_id = "7670000000000000001"
    output_path = output_dir / "preserved-before-partial-feed.webp"
    output_path.write_bytes(b"preserved-before-partial-feed")
    job = DownloadJob(
        id="profile-rediscovery-partial-feed",
        source_url=profile_url,
        platform=Platform.DOUYIN,
        source_kind=SourceKind.PROFILE,
        output_root=str(output_dir),
        status=JobStatus.INTERRUPTED,
        error=DOUYIN_PROFILE_REDISCOVERY_MESSAGE,
        warning=DOUYIN_PROFILE_REDISCOVERY_MESSAGE,
        verification_url=profile_url,
        discovery_complete=False,
        items=[
            DownloadItem(
                id=media_id,
                media_id=media_id,
                source_url=f"https://www.douyin.com/video/{media_id}",
                title=f"Recovered Douyin files {media_id}",
                status=ItemStatus.FAILED,
                retryable=False,
                output_paths=[str(output_path)],
                metadata={
                    "profile_url": profile_url,
                    "profile_owner_verified": True,
                    DOUYIN_PROFILE_REDISCOVERY_ITEM_MARKER: True,
                },
            )
        ],
    )
    job.refresh_counts()
    JsonJobStore(state_dir).save(job)

    class PartialProfileEngine:
        def __init__(self) -> None:
            self.discovery_calls = 0
            self.download_calls = 0

        def discover(self, url, platform, kind, *, should_cancel):
            self.discovery_calls += 1
            return DiscoveryResult(
                author="Verified author",
                discovery_complete=False,
                warning="Douyin feed pagination was temporarily incomplete",
                items=[
                    DownloadItem(
                        id="fresh-target",
                        media_id=media_id,
                        source_url=f"https://www.douyin.com/video/{media_id}",
                        title="Fresh verified title",
                        media_type=MediaType.VIDEO,
                        metadata=complete_douyin_profile_metadata(
                            profile_url,
                            media_id,
                            title="Fresh verified title",
                        ),
                    )
                ],
            )

        def download_item(self, *args, **kwargs):
            self.download_calls += 1
            raise AssertionError("partial recovery feed must not reach download")

    manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=output_dir,
        max_workers=1,
    )
    engine = PartialProfileEngine()
    monkeypatch.setattr(manager, "_engine_for_job", lambda restored_job: engine)
    try:
        restored = manager.get_job(job.id)
        assert restored.items[0].metadata[
            DOUYIN_PROFILE_REDISCOVERY_ITEM_MARKER
        ] is True

        manager.retry_failed(job.id)
        paused = wait_for_job(manager, job.id)

        assert paused.status == JobStatus.FAILED
        assert paused.retryable is True
        assert paused.discovery_complete is False
        assert "partial author feed" in (paused.error or "")
        assert paused.auth_message is None
        assert paused.verification_url is None
        assert paused.total_items == 1
        assert paused.items[0].status == ItemStatus.FAILED
        assert paused.items[0].retryable is False
        assert paused.items[0].output_paths == [str(output_path)]
        assert paused.items[0].metadata[
            DOUYIN_PROFILE_REDISCOVERY_ITEM_MARKER
        ] is True
        assert output_path.read_bytes() == b"preserved-before-partial-feed"
        assert engine.discovery_calls == 1
        assert engine.download_calls == 0
    finally:
        manager.shutdown()


def test_numeric_profile_rediscovery_migration_is_idempotent(tmp_path) -> None:
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "downloads"
    output_dir.mkdir(parents=True)
    media_ids = ("7670000000000000001", "7670000000000000002")
    output_paths = []
    items = []
    for media_id in media_ids:
        output_path = output_dir / f"{media_id}.mp4"
        output_path.write_bytes(b"preserved")
        output_paths.append(str(output_path))
        items.append(
            DownloadItem(
                id=media_id,
                media_id=media_id,
                source_url=f"https://www.douyin.com/video/{media_id}",
                title=media_id,
                status=ItemStatus.COMPLETED,
                output_paths=[str(output_path)],
            )
        )
    job = DownloadJob(
        id="idempotent-legacy-profile",
        source_url="https://www.douyin.com/user/legacy-profile",
        platform=Platform.DOUYIN,
        source_kind=SourceKind.PROFILE,
        output_root=str(output_dir),
        status=JobStatus.COMPLETED,
        items=items,
    )
    job.refresh_counts()
    JsonJobStore(state_dir).save(job)

    first_manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=output_dir,
        max_workers=1,
    )
    try:
        first = first_manager.get_job(job.id)
    finally:
        first_manager.shutdown()

    second_manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=output_dir,
        max_workers=1,
    )
    try:
        second = second_manager.get_job(job.id)

        assert first.status == JobStatus.INTERRUPTED
        assert first.retryable is True
        assert first.error == DOUYIN_PROFILE_REDISCOVERY_MESSAGE
        assert first.revision == second.revision
        assert first.finished_at == second.finished_at
        assert [item.output_paths for item in second.items] == [
            [output_paths[0]],
            [output_paths[1]],
        ]
        assert all(
            item.metadata[DOUYIN_PROFILE_REDISCOVERY_ITEM_MARKER] is True
            for item in second.items
        )
    finally:
        second_manager.shutdown()


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


def test_restore_discards_legacy_douyin_item_cache_without_direct_candidates(
    monkeypatch,
    tmp_path,
) -> None:
    media_id = "7664225419386607205"
    source_url = f"https://www.douyin.com/video/{media_id}"
    state_dir = tmp_path / "state"
    legacy_video_uri = "legacy-item-video-uri"
    job = DownloadJob(
        id="legacy-direct-item-without-direct-candidates",
        source_url=source_url,
        platform=Platform.DOUYIN,
        source_kind=SourceKind.ITEM,
        output_root=str(tmp_path / "downloads"),
        status=JobStatus.NEEDS_AUTH,
        verification_url=source_url,
        items=[
            DownloadItem(
                id="legacy-target",
                media_id=media_id,
                source_url=source_url,
                status=ItemStatus.NEEDS_AUTH,
                metadata={
                    "verification_url": source_url,
                    "item_identity_verified": True,
                    "douyin_item_media": {
                        "media_id": media_id,
                        "video_uri": legacy_video_uri,
                    },
                },
            )
        ],
    )
    job.refresh_counts()
    JsonJobStore(state_dir).save(job)

    class RefreshedItemEngine:
        def __init__(self) -> None:
            self.discovery_calls = 0
            self.download_calls = 0

        def discover(self, url, platform, kind, *, should_cancel):
            self.discovery_calls += 1
            assert url == source_url
            assert platform == Platform.DOUYIN
            assert kind == SourceKind.ITEM
            return DiscoveryResult(
                author="Verified author",
                items=[
                    DownloadItem(
                        id="fresh-target",
                        media_id=media_id,
                        source_url=source_url,
                        title="Fresh verified target",
                        media_type=MediaType.VIDEO,
                        metadata=complete_douyin_item_metadata(
                            source_url,
                            media_id,
                            video_uri="fresh-item-video-uri",
                        ),
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
            self.download_calls += 1
            assert item.metadata["douyin_item_media"]["video_uri"] != (
                legacy_video_uri
            )
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
    engine = RefreshedItemEngine()
    monkeypatch.setattr(manager, "_engine_for_job", lambda restored_job: engine)
    try:
        restored = manager.get_job(job.id)

        assert restored.status == JobStatus.INTERRUPTED
        assert restored.discovery_complete is False
        assert restored.verification_url == source_url
        assert restored.items[0].status == ItemStatus.FAILED
        assert restored.items[0].retryable is True
        assert "douyin_item_media" not in restored.items[0].metadata
        assert "item_identity_verified" not in restored.items[0].metadata

        manager.retry_failed(job.id)
        completed = wait_for_job(manager, job.id)

        assert completed.status == JobStatus.COMPLETED
        assert completed.items[0].title == "Fresh verified target"
        assert completed.items[0].resolution == "1440x2560"
        assert engine.discovery_calls == 1
        assert engine.download_calls == 1
    finally:
        manager.shutdown()


def test_fresh_douyin_item_without_direct_candidates_pauses_before_download(
    monkeypatch,
    tmp_path,
) -> None:
    media_id = "7664225419386607205"
    source_url = f"https://www.douyin.com/video/{media_id}"

    class DefaultOnlyItemEngine:
        def __init__(self) -> None:
            self.discovery_calls = 0
            self.download_calls = 0

        def discover(self, url, platform, kind, *, should_cancel):
            self.discovery_calls += 1
            return DiscoveryResult(
                author="Verified author",
                items=[
                    DownloadItem(
                        id="default-only-target",
                        media_id=media_id,
                        source_url=source_url,
                        title="Default-only target",
                        media_type=MediaType.VIDEO,
                        metadata={
                            "verification_url": source_url,
                            "item_identity_verified": True,
                            "douyin_item_media": {
                                "media_id": media_id,
                                "video_uri": "default-only-video-uri",
                            },
                        },
                    )
                ],
            )

        def download_item(self, *args, **kwargs):
            self.download_calls += 1
            raise AssertionError("default-only item must not reach download")

    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    engine = DefaultOnlyItemEngine()
    monkeypatch.setattr(manager, "_engine_for_job", lambda job: engine)
    try:
        created = manager.create_job(source_url, auto_start=True)
        paused = wait_for_job(manager, created.id)

        assert paused.status == JobStatus.FAILED
        assert paused.retryable is True
        assert paused.discovery_complete is False
        assert paused.items == []
        assert paused.auth_message is None
        assert paused.verification_url is None
        assert "no verified author-feed direct rendition" in (paused.error or "")
        assert engine.discovery_calls == 1
        assert engine.download_calls == 0
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
                        metadata=complete_douyin_item_metadata(
                            source_url,
                            media_id,
                            video_uri="fresh-verified-video-uri",
                        ),
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

    with pytest.raises(DiscoveryError, match="uploader profile"):
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


def test_failed_xiaohongshu_profile_retry_refreshes_xsec_tokens(tmp_path) -> None:
    job = DownloadJob(
        id="xhs-profile-expired-token",
        source_url="https://www.xiaohongshu.com/user/profile/expected",
        platform=Platform.XIAOHONGSHU,
        source_kind=SourceKind.PROFILE,
        output_root=str(tmp_path),
        status=JobStatus.FAILED,
        discovery_complete=True,
        items=[
            DownloadItem(
                id="failed-note",
                media_id="6411cf99000000001300b6d9",
                source_url=(
                    "https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9"
                    "?xsec_token=stale&xsec_source=pc_user"
                ),
                status=ItemStatus.FAILED,
                retryable=True,
            )
        ],
    )

    assert DownloadManager._should_rediscover_on_retry(job) is True


@pytest.mark.parametrize(
    ("source_kind", "source_url"),
    [
        (
            SourceKind.ITEM,
            "https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9",
        ),
        (SourceKind.SHORT_LINK, "https://xhslink.com/a/stable-short-code"),
    ],
)
def test_failed_xiaohongshu_direct_retry_refreshes_discovery(
    source_kind: SourceKind,
    source_url: str,
    tmp_path,
) -> None:
    job = DownloadJob(
        id="xhs-direct-expired-token",
        source_url=source_url,
        platform=Platform.XIAOHONGSHU,
        source_kind=source_kind,
        output_root=str(tmp_path),
        status=JobStatus.FAILED,
        items=[
            DownloadItem(
                id="failed-note",
                media_id="6411cf99000000001300b6d9",
                source_url=(
                    "https://www.xiaohongshu.com/explore/"
                    "6411cf99000000001300b6d9?xsec_token=stale"
                ),
                status=ItemStatus.FAILED,
                retryable=True,
            )
        ],
    )

    assert DownloadManager._should_rediscover_on_retry(job) is True


@pytest.mark.parametrize("retry_method", ["retry_item", "retry_failed"])
def test_failed_douyin_item_retry_refreshes_expired_direct_urls_after_restart(
    monkeypatch,
    tmp_path,
    retry_method: str,
) -> None:
    media_id = "7638230489560727931"
    source_url = f"https://www.douyin.com/video/{media_id}"

    class RefreshingDirectEngine:
        def __init__(self) -> None:
            self.discovery_calls = 0
            self.download_urls: list[str] = []
            self.download_durations: list[int] = []

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
                                "duration_ms": (
                                    4_000 if self.discovery_calls == 1 else 4_573
                                ),
                                "minimum_width": 1440,
                                "minimum_height": 2560,
                                "direct_candidates": [
                                    {
                                        "video_uri": "verified-shaped-video-uri",
                                        "width": 1440,
                                        "height": 2560,
                                        "bit_rate": 20_000_000,
                                        "codec_hint": "hevc",
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
            duration_ms = item.metadata["douyin_item_media"]["duration_ms"]
            self.download_urls.append(direct_url)
            self.download_durations.append(duration_ms)
            if len(self.download_urls) == 1:
                raise RuntimeError(
                    "media duration did not match the requested Douyin item"
                )
            assert duration_ms == 4_573
            return DownloadOutcome(
                output_paths=[str(Path(output_dir) / "target.mp4")],
                title=item.title,
                author="Verified author",
                media_type=MediaType.VIDEO,
                resolution="1440x2560",
            )

    state_dir = tmp_path / "state"
    first_manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    engine = RefreshingDirectEngine()
    monkeypatch.setattr(first_manager, "_engine_for_job", lambda job: engine)
    try:
        created = first_manager.create_job(source_url, auto_start=True)
        failed = wait_for_job(first_manager, created.id)
        assert failed.status == JobStatus.FAILED
    finally:
        first_manager.shutdown()

    manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    monkeypatch.setattr(manager, "_engine_for_job", lambda job: engine)
    try:
        restored = manager.get_job(created.id)
        assert restored.status == JobStatus.FAILED
        if retry_method == "retry_item":
            manager.retry_item(created.id, "target")
        else:
            manager.retry_failed(created.id)
        completed = wait_for_job(manager, created.id)

        assert completed.status == JobStatus.COMPLETED
        assert completed.items[0].resolution == "1440x2560"
        assert completed.items[0].metadata["douyin_item_media"][
            "duration_ms"
        ] == 4_573
        assert engine.discovery_calls == 2
        assert engine.download_urls == [
            "https://v26-web.douyinvod.com/signed-1.mp4",
            "https://v26-web.douyinvod.com/signed-2.mp4",
        ]
        assert engine.download_durations == [4_000, 4_573]
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


def test_item_temporary_access_pauses_queue_and_retry_resumes_all(
    monkeypatch,
    tmp_path,
) -> None:
    class TemporaryFirstItemEngine:
        def __init__(self) -> None:
            self.download_calls: list[str] = []
            self.fail_first = True

        def discover(self, url, platform, kind, *, should_cancel):
            return DiscoveryResult(
                author="Temporary Access Author",
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
            self.download_calls.append(item.id)
            output_dir = Path(output_dir)
            preserved_path = output_dir / "first-preserved.webp"
            if item.id == "first" and self.fail_first:
                self.fail_first = False
                preserved_path.write_bytes(b"preserved-before-temporary-limit")
                callback(
                    EngineEvent(
                        event="asset_completed",
                        output_paths=[str(preserved_path)],
                    )
                )
                raise TemporaryAccessError(
                    "Media was temporarily limited at "
                    "https://cdn.example/media?token=must-not-persist"
                )

            final_path = output_dir / f"{item.id}.mp4"
            final_path.write_bytes(f"completed-{item.id}".encode())
            output_paths = [str(final_path)]
            if item.id == "first":
                output_paths.insert(0, str(preserved_path))
            return DownloadOutcome(
                output_paths=output_paths,
                title=item.title,
                upload_date="2025-11-14",
                author="Temporary Access Author",
                media_type=MediaType.VIDEO,
                selected_format="bestvideo+bestaudio",
                resolution="3840x2160",
            )

    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    engine = TemporaryFirstItemEngine()
    monkeypatch.setattr(manager, "_engine_for_job", lambda job: engine)

    try:
        created = manager.create_job(
            "https://www.youtube.com/@BlenderOfficial",
            auto_start=True,
        )
        interrupted = wait_for_job(manager, created.id)

        assert interrupted.status == JobStatus.INTERRUPTED
        assert [item.status for item in interrupted.items] == [
            ItemStatus.FAILED,
            ItemStatus.QUEUED,
        ]
        assert [item.attempts for item in interrupted.items] == [1, 0]
        assert engine.download_calls == ["first"]
        assert interrupted.retryable is True
        assert interrupted.active_item_id is None
        assert interrupted.finished_at is not None
        assert interrupted.items[0].retryable is True
        assert interrupted.items[0].output_paths == [
            str(Path(interrupted.output_dir) / "first-preserved.webp")
        ]
        assert interrupted.items[0].auth_message is None
        assert interrupted.auth_message is None
        assert interrupted.verification_url is None
        assert "[redacted URL]" in (interrupted.error or "")
        assert "must-not-persist" not in (interrupted.error or "")
        assert interrupted.items[0].error == interrupted.error

        persisted = JsonJobStore(tmp_path / "state").get(created.id)
        assert persisted.status == JobStatus.INTERRUPTED
        assert persisted.items[0].output_paths == interrupted.items[0].output_paths
        assert persisted.error == interrupted.error
        assert "must-not-persist" not in persisted.model_dump_json()

        manager.retry_failed(created.id)
        completed = wait_for_job(manager, created.id)

        assert completed.status == JobStatus.COMPLETED
        assert [item.status for item in completed.items] == [
            ItemStatus.COMPLETED,
            ItemStatus.COMPLETED,
        ]
        assert [item.attempts for item in completed.items] == [2, 1]
        assert engine.download_calls == ["first", "first", "second"]
        assert completed.items[0].output_paths == [
            str(Path(completed.output_dir) / "first-preserved.webp"),
            str(Path(completed.output_dir) / "first.mp4"),
        ]
        assert completed.error is None
        assert completed.auth_message is None
        assert completed.verification_url is None
        assert all(item.auth_message is None for item in completed.items)
    finally:
        manager.shutdown()


def test_douyin_profile_redirect_interrupts_queue_and_rediscovery_resumes(
    monkeypatch,
    tmp_path,
) -> None:
    profile_url = (
        "https://www.douyin.com/user/"
        "MS4wLjABAAAA9OBQVqfaEUOvYbk2U0bSMCmaGV9OiG5-k15gEXhWLu6"
    )
    media_ids = [
        "7664225419386607205",
        "7677923079457231738",
        "7677554129950241521",
    ]
    raw_redirect_error = (
        "Douyin media redirect could not be trusted. The task was paused before "
        "downloading later items. Redirect host: secret-token.vendor-cdn.net; "
        "Redirect host "
        "fingerprint: 0123456789ab; "
        "Redirect reason: unrecognized-host"
    )
    redirect_error = safe_external_error_message(raw_redirect_error)

    class RedirectOnceEngine:
        def __init__(self) -> None:
            self.discovery_urls: list[str] = []
            self.download_calls: list[str] = []
            self.fail_first = True

        def discover(self, url, platform, kind, *, should_cancel):
            self.discovery_urls.append(url)
            return DiscoveryResult(
                author="Verified author",
                items=[
                    DownloadItem(
                        id=f"item-{media_id}",
                        media_id=media_id,
                        source_url=f"https://www.douyin.com/video/{media_id}",
                        title=f"Video {index}",
                        author="Verified author",
                        media_type=MediaType.VIDEO,
                        metadata=complete_douyin_profile_metadata(
                            profile_url,
                            media_id,
                            title=f"Video {index}",
                        ),
                    )
                    for index, media_id in enumerate(media_ids, start=1)
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
            self.download_calls.append(item.media_id)
            if item.media_id == media_ids[1] and self.fail_first:
                self.fail_first = False
                raise TemporaryAccessError(raw_redirect_error)
            output_path = Path(output_dir) / f"{item.media_id}.mp4"
            output_path.write_bytes(item.media_id.encode())
            return DownloadOutcome(
                output_paths=[str(output_path)],
                title=item.title,
                upload_date="2025-11-14",
                author="Verified author",
                media_type=MediaType.VIDEO,
                selected_format="douyin-api-1080x1920-1",
                resolution="1080x1920",
            )

    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    engine = RedirectOnceEngine()
    monkeypatch.setattr(manager, "_engine_for_job", lambda job: engine)

    try:
        created = manager.create_job(profile_url, auto_start=True)
        interrupted = wait_for_job(manager, created.id)

        assert interrupted.status == JobStatus.INTERRUPTED
        assert [item.status for item in interrupted.items] == [
            ItemStatus.COMPLETED,
            ItemStatus.FAILED,
            ItemStatus.QUEUED,
        ]
        assert [item.attempts for item in interrupted.items] == [1, 1, 0]
        assert engine.download_calls == media_ids[:2]
        assert interrupted.error == redirect_error
        assert interrupted.items[0].error is None
        assert interrupted.items[1].error == redirect_error
        assert interrupted.auth_message is None
        assert interrupted.verification_url is None
        assert all(item.auth_message is None for item in interrupted.items)

        persisted = JsonJobStore(tmp_path / "state").get(created.id)
        assert persisted.status == JobStatus.INTERRUPTED
        assert persisted.error == redirect_error
        assert persisted.items[0].status == ItemStatus.COMPLETED
        assert persisted.items[2].status == ItemStatus.QUEUED
        persisted_json = persisted.model_dump_json()
        assert "Redirect reason: unrecognized-host" in persisted_json
        for secret in (
            "must-not-persist",
            "private.mp4",
            "127.0.0.1",
            "token=",
            "secret-token",
            "user:",
        ):
            assert secret not in persisted_json

        manager.retry_failed(created.id)
        completed = wait_for_job(manager, created.id)

        assert completed.status == JobStatus.COMPLETED
        assert all(item.status == ItemStatus.COMPLETED for item in completed.items)
        assert engine.discovery_urls == [profile_url, profile_url]
        assert engine.download_calls == [
            media_ids[0],
            media_ids[1],
            media_ids[1],
            media_ids[2],
        ]
        assert completed.items[0].attempts == 1
        assert completed.error is None
        assert completed.auth_message is None
        assert completed.verification_url is None
    finally:
        manager.shutdown()


def test_douyin_profile_retry_waits_for_complete_feed_then_retires_removed_item(
    monkeypatch,
    tmp_path,
) -> None:
    profile_url = "https://www.douyin.com/user/verified-live-photo-profile"
    media_ids = [str(7670000000000000000 + index) for index in range(1, 5)]
    redirect_error = (
        "Douyin media redirect could not be trusted. The task was paused before "
        "downloading later items. Redirect host: pstatp.com; Redirect host "
        "fingerprint: unavailable; Redirect port: 8443; Redirect reason: "
        "nonstandard-port"
    )

    class RemovedLivePhotoEngine:
        def __init__(self) -> None:
            self.discovery_calls = 0
            self.download_calls: list[str] = []
            self.preserved_paths: list[str] = []

        def profile_item(self, media_id: str) -> DownloadItem:
            return DownloadItem(
                id=f"item-{media_id}",
                media_id=media_id,
                source_url=f"https://www.douyin.com/video/{media_id}",
                title=f"Verified item {media_id}",
                author="Verified author",
                media_type=MediaType.IMAGE,
                metadata=complete_douyin_profile_metadata(
                    profile_url,
                    media_id,
                    title=f"Verified item {media_id}",
                ),
            )

        def discover(self, url, platform, kind, *, should_cancel):
            self.discovery_calls += 1
            assert url == profile_url
            if self.discovery_calls == 1:
                visible_ids = media_ids
                discovery_complete = True
            elif self.discovery_calls == 2:
                visible_ids = media_ids[1:2]
                discovery_complete = False
            else:
                visible_ids = media_ids[1:3]
                discovery_complete = True
            return DiscoveryResult(
                author="Verified author",
                items=[self.profile_item(media_id) for media_id in visible_ids],
                discovery_complete=discovery_complete,
                warning=(
                    None
                    if discovery_complete
                    else "Douyin feed pagination was temporarily incomplete"
                ),
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
            self.download_calls.append(item.media_id)
            if item.media_id == media_ids[0]:
                self.preserved_paths = []
                for index in range(1, 5):
                    path = Path(output_dir) / f"preserved-{index:02d}.webp"
                    path.write_bytes(f"preserved-{index}".encode())
                    self.preserved_paths.append(str(path))
                callback(
                    EngineEvent(
                        event="asset_completed",
                        output_paths=list(self.preserved_paths),
                    )
                )
                raise TemporaryAccessError(redirect_error)
            output_path = Path(output_dir) / f"{item.media_id}.mp4"
            output_path.write_bytes(item.media_id.encode())
            return DownloadOutcome(
                output_paths=[str(output_path)],
                title=item.title,
                upload_date="2025-08-27",
                author="Verified author",
                media_type=MediaType.VIDEO,
                selected_format="douyin-api-1080x1920-1",
                resolution="1080x1920",
            )

    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    engine = RemovedLivePhotoEngine()
    monkeypatch.setattr(manager, "_engine_for_job", lambda job: engine)

    try:
        created = manager.create_job(profile_url, auto_start=True)
        interrupted = wait_for_job(manager, created.id)

        assert interrupted.status == JobStatus.INTERRUPTED
        assert interrupted.discovery_complete is False
        assert interrupted.items[0].metadata[
            DOUYIN_PROFILE_REFRESH_REQUIRED_MARKER
        ] is True
        assert interrupted.items[0].output_paths == engine.preserved_paths
        preserved_bytes = {
            path: Path(path).read_bytes() for path in engine.preserved_paths
        }

        manager.start_job(created.id)
        partial_refresh = wait_for_job(manager, created.id)

        assert partial_refresh.status == JobStatus.FAILED
        assert partial_refresh.discovery_complete is False
        assert "partial author feed" in (partial_refresh.error or "")
        assert len(partial_refresh.items) == 4
        assert engine.download_calls == [media_ids[0]]
        assert partial_refresh.items[0].metadata[
            DOUYIN_PROFILE_REFRESH_REQUIRED_MARKER
        ] is True

        manager.retry_item(created.id, partial_refresh.items[0].id)
        completed_refresh = wait_for_job(manager, created.id)

        assert completed_refresh.status == JobStatus.PARTIAL
        assert completed_refresh.discovery_complete is True
        assert completed_refresh.total_items == 3
        assert completed_refresh.completed_items == 2
        assert completed_refresh.failed_items == 1
        assert {item.media_id for item in completed_refresh.items} == set(
            media_ids[:3]
        )
        removed_item = next(
            item
            for item in completed_refresh.items
            if item.media_id == media_ids[0]
        )
        assert removed_item.status == ItemStatus.FAILED
        assert removed_item.retryable is False
        assert removed_item.error == DOUYIN_PROFILE_REMOVED_PARTIAL_ITEM_MESSAGE
        assert removed_item.output_paths == engine.preserved_paths
        assert DOUYIN_PROFILE_REFRESH_REQUIRED_MARKER not in removed_item.metadata
        assert removed_item.metadata[DOUYIN_PROFILE_REMOVED_ITEM_MARKER] is True
        assert "douyin_profile_media" not in removed_item.metadata
        assert engine.download_calls == [media_ids[0], *media_ids[1:3]]
        for path, expected in preserved_bytes.items():
            assert Path(path).read_bytes() == expected
        with pytest.raises(ItemNotRetryableError):
            manager.retry_failed(created.id)
    finally:
        manager.shutdown()

    restored_manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    try:
        restored = restored_manager.get_job(created.id)
        assert restored.status == JobStatus.PARTIAL
        assert restored.discovery_complete is True
        assert restored.completed_items == 2
        assert restored.failed_items == 1
        restored_removed = next(
            item for item in restored.items if item.media_id == media_ids[0]
        )
        assert restored_removed.status == ItemStatus.FAILED
        assert restored_removed.retryable is False
        assert restored_removed.error == DOUYIN_PROFILE_REMOVED_PARTIAL_ITEM_MESSAGE
        assert restored_removed.metadata[DOUYIN_PROFILE_REMOVED_ITEM_MARKER] is True
        assert DOUYIN_PROFILE_REDISCOVERY_ITEM_MARKER not in restored_removed.metadata
    finally:
        restored_manager.shutdown()


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


def test_douyin_profile_rediscovery_queues_matched_recovery_and_skips_missing() -> None:
    matched_id = "7670000000000000001"
    missing_id = "7670000000000000002"
    previous = [
        DownloadItem(
            id="old-matched",
            media_id=matched_id,
            source_url=f"https://www.douyin.com/video/{matched_id}",
            title=f"Recovered Douyin files {matched_id}",
            status=ItemStatus.FAILED,
            retryable=False,
            output_paths=["/downloads/preserved-matched.mp4"],
            attempts=2,
            metadata={DOUYIN_PROFILE_REDISCOVERY_ITEM_MARKER: True},
        ),
        DownloadItem(
            id="old-missing",
            media_id=missing_id,
            source_url=f"https://www.douyin.com/video/{missing_id}",
            title=missing_id,
            status=ItemStatus.FAILED,
            retryable=False,
            output_paths=["/downloads/preserved-missing.mp4"],
            metadata={DOUYIN_PROFILE_REDISCOVERY_ITEM_MARKER: True},
        ),
    ]
    fresh = DownloadItem(
        id="fresh-matched",
        media_id=matched_id,
        source_url=f"https://www.douyin.com/video/{matched_id}",
        title="Verified refreshed title",
        status=ItemStatus.QUEUED,
        retryable=True,
        metadata={"verified": True},
    )

    merged = DownloadManager._merge_discovered_items(previous, [fresh])
    refreshed, retired = merged

    assert refreshed.id == "fresh-matched"
    assert refreshed.title == "Verified refreshed title"
    assert refreshed.status == ItemStatus.QUEUED
    assert refreshed.retryable is True
    assert refreshed.output_paths == ["/downloads/preserved-matched.mp4"]
    assert refreshed.attempts == 2
    assert DOUYIN_PROFILE_REDISCOVERY_ITEM_MARKER not in refreshed.metadata
    assert retired.status == ItemStatus.SKIPPED
    assert retired.retryable is False
    assert retired.title == f"Recovered Douyin files {missing_id}"
    assert DOUYIN_PROFILE_REDISCOVERY_ITEM_MARKER not in retired.metadata


def test_complete_douyin_profile_refresh_retires_only_missing_unfinished_items() -> None:
    previous = [
        DownloadItem(
            id="partial-missing",
            media_id="7670000000000000001",
            source_url="https://www.douyin.com/video/7670000000000000001",
            status=ItemStatus.FAILED,
            output_paths=["/downloads/image-01.webp", "/downloads/image-02.webp"],
            metadata={
                DOUYIN_PROFILE_REFRESH_REQUIRED_MARKER: True,
                "douyin_profile_media": {"signed_url": "must-be-discarded"},
            },
        ),
        DownloadItem(
            id="queued-missing",
            media_id="7670000000000000002",
            source_url="https://www.douyin.com/video/7670000000000000002",
            status=ItemStatus.QUEUED,
        ),
        DownloadItem(
            id="completed-missing",
            media_id="7670000000000000003",
            source_url="https://www.douyin.com/video/7670000000000000003",
            status=ItemStatus.COMPLETED,
            output_paths=["/downloads/completed.mp4"],
        ),
    ]

    merged = DownloadManager._merge_discovered_items(
        previous,
        [],
        retire_missing_douyin_profile_items=True,
    )

    assert [item.id for item in merged] == [
        "partial-missing",
        "completed-missing",
    ]
    partial, completed = merged
    assert partial.status == ItemStatus.FAILED
    assert partial.retryable is False
    assert partial.error == DOUYIN_PROFILE_REMOVED_PARTIAL_ITEM_MESSAGE
    assert partial.output_paths == previous[0].output_paths
    assert DOUYIN_PROFILE_REFRESH_REQUIRED_MARKER not in partial.metadata
    assert partial.metadata[DOUYIN_PROFILE_REMOVED_ITEM_MARKER] is True
    assert "douyin_profile_media" not in partial.metadata
    assert completed.status == ItemStatus.COMPLETED
    assert completed.output_paths == ["/downloads/completed.mp4"]


def test_partial_profile_refresh_never_retires_missing_items() -> None:
    previous = DownloadItem(
        id="partial-missing",
        media_id="7670000000000000001",
        source_url="https://www.douyin.com/video/7670000000000000001",
        status=ItemStatus.FAILED,
        output_paths=["/downloads/image-01.webp"],
        metadata={DOUYIN_PROFILE_REFRESH_REQUIRED_MARKER: True},
    )

    merged = DownloadManager._merge_discovered_items([previous], [])

    assert len(merged) == 1
    assert merged[0].status == ItemStatus.FAILED
    assert merged[0].retryable is True
    assert merged[0].output_paths == previous.output_paths
    assert merged[0].metadata[DOUYIN_PROFILE_REFRESH_REQUIRED_MARKER] is True


def test_removed_profile_item_is_requeued_if_it_reappears() -> None:
    media_id = "7670000000000000001"
    previous = DownloadItem(
        id="removed-item",
        media_id=media_id,
        source_url=f"https://www.douyin.com/video/{media_id}",
        status=ItemStatus.FAILED,
        retryable=False,
        output_paths=["/downloads/preserved.webp"],
        metadata={DOUYIN_PROFILE_REMOVED_ITEM_MARKER: True},
    )
    fresh = DownloadItem(
        id="fresh-item",
        media_id=media_id,
        source_url=f"https://www.douyin.com/video/{media_id}",
        status=ItemStatus.QUEUED,
        metadata={"profile_owner_verified": True},
    )

    merged = DownloadManager._merge_discovered_items([previous], [fresh])

    assert len(merged) == 1
    assert merged[0].id == "fresh-item"
    assert merged[0].status == ItemStatus.QUEUED
    assert merged[0].retryable is True
    assert merged[0].output_paths == previous.output_paths
    assert DOUYIN_PROFILE_REMOVED_ITEM_MARKER not in merged[0].metadata


def test_partial_douyin_refresh_blocks_generic_retryable_cached_queue(
    tmp_path,
) -> None:
    profile_url = "https://www.douyin.com/user/verified-profile"
    media_id = "7670000000000000001"
    job = DownloadJob(
        id="generic-failed-profile-refresh",
        source_url=profile_url,
        platform=Platform.DOUYIN,
        source_kind=SourceKind.PROFILE,
        output_root=str(tmp_path),
        status=JobStatus.FAILED,
        items=[
            DownloadItem(
                id="generic-failed",
                media_id=media_id,
                source_url=f"https://www.douyin.com/video/{media_id}",
                status=ItemStatus.FAILED,
                error="Generic media transfer failure",
                retryable=True,
                metadata=complete_douyin_profile_metadata(
                    profile_url,
                    media_id,
                    title="Generic failed item",
                ),
            )
        ],
    )
    partial_result = DiscoveryResult(
        author="Verified author",
        items=[
            DownloadItem(
                id="fresh-item",
                media_id=media_id,
                source_url=f"https://www.douyin.com/video/{media_id}",
                metadata=complete_douyin_profile_metadata(
                    profile_url,
                    media_id,
                    title="Fresh item",
                ),
            )
        ],
        discovery_complete=False,
    )

    with pytest.raises(TemporaryAccessError, match="partial author feed"):
        DownloadManager._validate_discovery_result(job, partial_result)


def test_completed_profile_refresh_uses_fresh_verified_display_metadata() -> None:
    media_id = "7670000000000000001"
    previous = DownloadItem(
        id="old-id",
        media_id=media_id,
        source_url=f"https://www.douyin.com/video/{media_id}",
        title=media_id,
        author="Old author",
        upload_date="2024-01-01",
        media_type=MediaType.VIDEO,
        status=ItemStatus.COMPLETED,
        output_paths=["/downloads/preserved.mp4"],
    )
    fresh = DownloadItem(
        id="fresh-id",
        media_id=media_id,
        source_url=f"https://www.douyin.com/video/{media_id}",
        title="Fresh verified title",
        author="Fresh author",
        upload_date="2025-08-27",
        media_type=MediaType.IMAGE,
        extractor_key="Douyin",
    )

    merged = DownloadManager._merge_discovered_items([previous], [fresh])[0]

    assert merged.status == ItemStatus.COMPLETED
    assert merged.output_paths == previous.output_paths
    assert merged.title == "Fresh verified title"
    assert merged.author == "Fresh author"
    assert merged.upload_date == "2025-08-27"
    assert merged.media_type == MediaType.IMAGE


def test_completed_non_douyin_refresh_preserves_saved_display_metadata() -> None:
    previous = DownloadItem(
        id="old-note",
        media_id="note-id",
        source_url="https://www.xiaohongshu.com/explore/note-id?token=old",
        title="Previously parsed title",
        author="Known author",
        upload_date="2025-01-02",
        status=ItemStatus.COMPLETED,
        output_paths=["/downloads/note.webp"],
    )
    fresh = DownloadItem(
        id="fresh-note",
        media_id="note-id",
        source_url="https://www.xiaohongshu.com/explore/note-id?token=fresh",
        title="note-id",
    )

    merged = DownloadManager._merge_discovered_items([previous], [fresh])[0]

    assert merged.title == "Previously parsed title"
    assert merged.author == "Known author"
    assert merged.upload_date == "2025-01-02"


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
    existing_media_id = "6411cf99000000001300b6d9"
    new_media_id = "6411cf99000000001300b6da"
    existing_url = f"https://www.xiaohongshu.com/explore/{existing_media_id}"
    new_url = f"https://www.xiaohongshu.com/explore/{new_media_id}"

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
                        media_id=existing_media_id,
                        source_url=existing_url,
                        title="Existing note",
                        media_type=MediaType.IMAGE,
                        metadata={
                            "xiaohongshu_profile_id": "example",
                            "profile_note_membership_verified": True,
                        },
                    ),
                    DownloadItem(
                        id="new-note",
                        media_id=new_media_id,
                        source_url=new_url,
                        title="New note",
                        media_type=MediaType.IMAGE,
                        metadata={
                            "xiaohongshu_profile_id": "example",
                            "profile_note_membership_verified": True,
                        },
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
                    media_id=existing_media_id,
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
