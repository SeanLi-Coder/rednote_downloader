from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.downloader import (
    KUAISHOU_SAVED_ASSETS_KEY,
    DiscoveryResult,
    DownloadOutcome,
    EngineEvent,
    platform_output_directory,
)
from app.errors import (
    DiscoveryError,
    MediaDownloadError,
    SiteIssueCode,
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
)
from app.storage import JsonJobStore
from app.task_manager import (
    DownloadManager,
    _is_kuaishou_security_transfer_error,
    _public_kuaishou_saved_asset,
)


PROFILE = "https://www.kuaishou.com/profile/3xowner1"
SHARE = "https://v.kuaishou.com/share123"
ITEM = "https://www.kuaishou.com/short-video/3xvideo1"
MEDIA = "https://video.kwaicdn.com/video.mp4"


def _item(
    media_id: str,
    *,
    source_kind: str = "profile",
    source_id: str = "3xowner1",
    author_id: str = "3xowner1",
) -> DownloadItem:
    url = f"https://www.kuaishou.com/short-video/{media_id}"
    return DownloadItem(
        id=f"kuaishou-{media_id}",
        media_id=media_id,
        source_url=url,
        title=f"Video {media_id}",
        author="Fixture Author",
        extractor_key="Kuaishou",
        media_type=MediaType.VIDEO,
        metadata={
            "kuaishou_author_id": author_id,
            "kuaishou_source_kind": source_kind,
            "kuaishou_source_id": source_id,
            "kuaishou_refresh_url": url,
        },
    )


def _job(source_url: str, kind: SourceKind, root: Path) -> DownloadJob:
    return DownloadJob(
        id="fixture",
        source_url=source_url,
        platform=Platform.KUAISHOU,
        source_kind=kind,
        output_root=str(root),
        cookie_browser=None,
    )


@pytest.mark.parametrize(
    ("mutation", "source_url", "kind"),
    [
        (lambda item: setattr(item, "media_id", "other"), PROFILE, SourceKind.PROFILE),
        (
            lambda item: item.metadata.update(kuaishou_author_id="other"),
            PROFILE,
            SourceKind.PROFILE,
        ),
        (
            lambda item: item.metadata.update(kuaishou_source_id="other"),
            PROFILE,
            SourceKind.PROFILE,
        ),
        (
            lambda item: item.metadata.update(
                kuaishou_refresh_url="https://www.kuaishou.com/short-video/other"
            ),
            PROFILE,
            SourceKind.PROFILE,
        ),
        (
            lambda item: setattr(
                item,
                "source_url",
                "https://www.kuaishou.com.evil.test/short-video/3xvideo1",
            ),
            PROFILE,
            SourceKind.PROFILE,
        ),
        (
            lambda item: item.metadata.update(kuaishou_source_kind="profile"),
            ITEM,
            SourceKind.ITEM,
        ),
    ],
)
def test_untrusted_discovery_is_rejected_before_queue(
    tmp_path, mutation, source_url, kind
) -> None:
    item = _item(
        "3xvideo1",
        source_kind="item" if kind == SourceKind.ITEM else "profile",
        source_id="3xvideo1" if kind == SourceKind.ITEM else "3xowner1",
    )
    mutation(item)
    with pytest.raises(DiscoveryError):
        DownloadManager._validate_discovery_result(
            _job(source_url, kind, tmp_path),
            DiscoveryResult(author="Fixture Author", items=[item]),
        )


def test_short_link_retry_rejects_changed_resolved_profile(tmp_path) -> None:
    job = _job(SHARE, SourceKind.SHORT_LINK, tmp_path)
    job.resolved_source_kind = SourceKind.PROFILE
    job.resolved_source_id = "3xowner1"
    item = _item("3xvideo1", source_id="other")
    with pytest.raises(DiscoveryError, match="target changed"):
        DownloadManager._validate_discovery_result(
            job, DiscoveryResult(author="Fixture Author", items=[item])
        )


def test_duplicate_items_are_rejected_before_queue(tmp_path) -> None:
    item = _item("3xvideo1")
    with pytest.raises(DiscoveryError, match="duplicate"):
        DownloadManager._validate_discovery_result(
            _job(PROFILE, SourceKind.PROFILE, tmp_path),
            DiscoveryResult(author="Fixture Author", items=[item, item.model_copy(deep=True)]),
        )


class _PartialProfileEngine:
    def __init__(self, discovered: list[DownloadItem]) -> None:
        self.discovered = discovered
        self.downloaded: list[str] = []

    def discover(self, url, platform, kind, *, should_cancel):
        assert url == SHARE
        assert platform == Platform.KUAISHOU
        assert kind == SourceKind.SHORT_LINK
        return DiscoveryResult(
            author="Fixture Author",
            items=[item.model_copy(deep=True) for item in self.discovered],
            discovery_complete=False,
            warning="Kuaishou profile pagination was interrupted by rate limiting",
        )

    def download_item(self, item, platform, output_dir, *, callback, should_cancel):
        self.downloaded.append(item.media_id)
        output = str(Path(output_dir) / f"{item.media_id}.mp4")
        return DownloadOutcome(
            output_paths=[output],
            title=item.title,
            author=item.author,
            media_type=MediaType.VIDEO,
            selected_format="fixture-best",
            resolution="1920x1080",
        )


def test_short_profile_partial_retry_preserves_unseen_work_and_uses_author_folder(
    tmp_path, monkeypatch
) -> None:
    state = tmp_path / "state"
    root = tmp_path / "downloads"
    manager = DownloadManager(state_dir=state, default_output_root=root, max_workers=1)
    try:
        job = manager.create_job(SHARE, cookie_browser=None, auto_start=False)
        first = _item("3xvideo1")
        first.status = ItemStatus.COMPLETED
        first.output_paths = [str(root / "existing.mp4")]
        unseen = _item("3xvideo2")
        unseen.status = ItemStatus.QUEUED
        with manager._lock:
            stored = manager._require_job(job.id)
            stored.items = [first, unseen]
            stored.status = JobStatus.INTERRUPTED
            stored.discovery_complete = False
            stored.resolved_source_kind = SourceKind.PROFILE
            stored.resolved_source_id = "3xowner1"
            manager._commit_locked(stored)
        engine = _PartialProfileEngine([first])
        monkeypatch.setattr(manager, "_engine_for_job", lambda job: engine)
        manager.retry_failed(job.id)
        manager._futures[job.id].result(timeout=5)
        result = manager.get_job(job.id)
        assert result.status == JobStatus.PARTIAL
        assert result.discovery_complete is False
        assert result.resolved_source_kind == SourceKind.PROFILE
        assert [item.media_id for item in result.items] == ["3xvideo1", "3xvideo2"]
        assert [item.status for item in result.items] == [ItemStatus.COMPLETED] * 2
        assert engine.downloaded == ["3xvideo2"]
        assert Path(result.output_dir) == platform_output_directory(
            Platform.KUAISHOU, root, "Fixture Author"
        )
        assert Path(result.output_dir).is_dir()
    finally:
        manager.shutdown()

    restored = DownloadManager(state_dir=state, default_output_root=root, max_workers=1)
    try:
        assert [item.media_id for item in restored.get_job(job.id).items] == [
            "3xvideo1",
            "3xvideo2",
        ]
    finally:
        restored.shutdown()


def test_asset_receipts_persist_incrementally_without_signed_urls(tmp_path) -> None:
    state = tmp_path / "state"
    manager = DownloadManager(
        state_dir=state, default_output_root=tmp_path / "downloads", max_workers=1
    )
    try:
        job = manager.create_job(ITEM, cookie_browser=None, auto_start=False)
        item = _item("3xvideo1", source_kind="item", source_id="3xvideo1")
        with manager._lock:
            manager._require_job(job.id).items.append(item)
            manager._commit_locked(manager._require_job(job.id))
        paths = [tmp_path / "first.jpg", tmp_path / "third.jpg"]
        for path in paths:
            path.write_bytes(b"fixture")
        records = [
            {
                "media_id": "3xvideo1",
                "index": index,
                "media_kind": "image",
                "path": str(path),
                "width": 800,
                "height": 600,
                "size": path.stat().st_size,
                "local_sha256": "a" * 64,
                "source_sha256": "b" * 64,
                "candidates": [f"{MEDIA}?token=private"],
            }
            for index, path in ((1, paths[0]), (3, paths[1]))
        ]
        for record in records:
            manager._on_engine_event(
                job.id,
                item.id,
                EngineEvent(
                    event="asset_completed",
                    output_paths=[record["path"]],
                    asset_records=[record],
                ),
            )
        manager._on_engine_event(
            job.id,
            item.id,
            EngineEvent(
                event="asset_completed",
                output_paths=[records[0]["path"]],
                asset_records=[records[0]],
            ),
        )
    finally:
        manager.shutdown()

    persisted = JsonJobStore(state).get(job.id).items[0]
    saved = persisted.metadata[KUAISHOU_SAVED_ASSETS_KEY]
    assert [record["index"] for record in saved] == [1, 3]
    assert saved == [_public_kuaishou_saved_asset(record) for record in records]
    assert MEDIA not in json.dumps(persisted.model_dump(mode="json"))


def test_cookie_diagnostic_code_is_persisted_only_when_whitelisted(tmp_path) -> None:
    job = _job(ITEM, SourceKind.ITEM, tmp_path)
    item = _item("3xvideo1", source_kind="item", source_id="3xvideo1")
    job.items = [item]
    issue = TemporaryAccessError(
        "Kuaishou Chrome Cookie access failed. Diagnostic: cookie_database_locked.",
        issue_code=SiteIssueCode.COOKIE_UNAVAILABLE,
        diagnostic_code="cookie_database_locked",
    )
    DownloadManager._record_issue_locked(job, str(issue), item=item, cause=issue)
    assert job.diagnostic_code == "cookie_database_locked"
    assert item.diagnostic_code == "cookie_database_locked"

    unsafe = TemporaryAccessError(
        "Kuaishou Chrome Cookie access failed",
        issue_code=SiteIssueCode.COOKIE_UNAVAILABLE,
        diagnostic_code="private_token_value",
    )
    DownloadManager._record_issue_locked(job, str(unsafe), item=item, cause=unsafe)
    assert job.diagnostic_code is None
    assert item.diagnostic_code is None


def test_cookie_diagnostic_survives_worker_and_restart(tmp_path, monkeypatch) -> None:
    class CookieFailureEngine:
        def discover(self, url, platform, kind, *, should_cancel):
            return DiscoveryResult(
                author="Fixture Author",
                items=[_item("3xvideo1", source_kind="item", source_id="3xvideo1")],
            )

        def download_item(self, item, platform, output_dir, *, callback, should_cancel):
            raise TemporaryAccessError(
                "Kuaishou Chrome Cookie access failed. Diagnostic: cookie_database_locked.",
                issue_code=SiteIssueCode.COOKIE_UNAVAILABLE,
                diagnostic_code="cookie_database_locked",
            )

    state = tmp_path / "state"
    root = tmp_path / "downloads"
    manager = DownloadManager(state_dir=state, default_output_root=root, max_workers=1)
    try:
        monkeypatch.setattr(manager, "_engine_for_job", lambda job: CookieFailureEngine())
        job = manager.create_job(ITEM, cookie_browser=None, auto_start=True)
        manager._futures[job.id].result(timeout=5)
        assert manager.get_job(job.id).status == JobStatus.INTERRUPTED
    finally:
        manager.shutdown()

    persisted = JsonJobStore(state).get(job.id)
    assert persisted.issue_code == SiteIssueCode.COOKIE_UNAVAILABLE
    assert persisted.diagnostic_code == "cookie_database_locked"
    assert persisted.items[0].diagnostic_code == "cookie_database_locked"
    restored = DownloadManager(state_dir=state, default_output_root=root, max_workers=1)
    try:
        assert restored.get_job(job.id).diagnostic_code == "cookie_database_locked"
    finally:
        restored.shutdown()


def test_untrusted_media_redirect_pauses_profile_queue(tmp_path, monkeypatch) -> None:
    class RedirectFailureEngine:
        def discover(self, url, platform, kind, *, should_cancel):
            return DiscoveryResult(
                author="Fixture Author",
                items=[_item("3xvideo1"), _item("3xvideo2")],
            )

        def download_item(self, item, platform, output_dir, *, callback, should_cancel):
            raise MediaDownloadError(
                "All highest-available media URLs failed: Candidate 1: "
                "Kuaishou media redirect was blocked before requesting an "
                "untrusted target"
            )

    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    try:
        monkeypatch.setattr(manager, "_engine_for_job", lambda job: RedirectFailureEngine())
        job = manager.create_job(PROFILE, cookie_browser=None, auto_start=True)
        manager._futures[job.id].result(timeout=5)
        result = manager.get_job(job.id)
        assert result.status == JobStatus.INTERRUPTED
        assert result.issue_code == SiteIssueCode.SECURITY_BLOCKED
        assert [item.status for item in result.items] == [
            ItemStatus.FAILED,
            ItemStatus.QUEUED,
        ]
    finally:
        manager.shutdown()


@pytest.mark.parametrize(
    "detail",
    [
        "Untrusted Kuaishou media URL was blocked",
        "Kuaishou media redirect was blocked before requesting an untrusted target",
        "Kuaishou media request redirected to an untrusted URL",
    ],
)
def test_only_known_kuaishou_transfer_security_errors_are_classified(
    tmp_path, detail
) -> None:
    for message in (
        detail,
        f"All highest-available media URLs failed: Candidate 2: {detail}",
    ):
        job = _job(ITEM, SourceKind.ITEM, tmp_path)
        DownloadManager._record_issue_locked(
            job, message, cause=MediaDownloadError(message)
        )
        assert job.issue_code == SiteIssueCode.SECURITY_BLOCKED

    text = f"Unrelated site message: {detail}"
    assert not _is_kuaishou_security_transfer_error(MediaDownloadError(text), text)
    assert not _is_kuaishou_security_transfer_error(RuntimeError(detail), detail)
