from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import douyin_signing
from app.downloader import DiscoveryResult, DownloaderConfig, MediaDownloader
from app.errors import (
    DiscoveryError,
    DouyinMediaRefreshRequiredError,
    MediaDownloadError,
    SiteIssueCode,
    TemporaryAccessError,
)
from app.models import (
    DownloadItem,
    ItemStatus,
    JobStatus,
    MediaType,
    Platform,
    SourceKind,
)
from app.storage import JsonJobStore
from app.task_manager import DownloadManager


MEDIA_ID = "7649744769275263409"
OWNER_ID = "MS4wLjABAAAAverified-diagnostic-owner"
SOURCE_URL = f"https://www.douyin.com/video/{MEDIA_ID}"
SECRET = "private-response-cookie-and-signed-url-token"
DIAGNOSTIC_CODES = (
    "ssr-redirect",
    "ssr-unknown-item",
    "ssr-item-mismatch",
    "ssr-author-mismatch",
    "ssr-conflicting-items",
    "detail-item-mismatch",
    "detail-author-mismatch",
    "detail-response-redirect",
    "detail-metadata-incomplete",
    "ssr-metadata-incomplete",
    "signer-html-invalid",
    "signer-script-invalid",
    "signing-validation-failed",
    "signing-runtime-error",
)


@pytest.fixture
def native_item_metadata(monkeypatch):
    class NativeMetadataYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def extract_info(self, url, download, process=True):
            assert url == SOURCE_URL
            assert download is False
            assert process is False
            return {
                "id": MEDIA_ID,
                "title": "Verified item title",
                "channel": "Verified author",
                "channel_id": OWNER_ID,
                "formats": [
                    {
                        "url": (
                            "https://api-play.amemv.com/aweme/v1/play/"
                            "?video_id=v0200diagnosticmedia&ratio=720p"
                        ),
                        "width": 720,
                        "height": 1280,
                    }
                ],
            }

        def urlopen(self, *args, **kwargs):
            pytest.fail("A diagnostic failure must not request any media")

    monkeypatch.setattr("app.downloader.YoutubeDL", NativeMetadataYoutubeDL)
    return MediaDownloader(DownloaderConfig(cookie_browser="chrome"))


def fail_author_enrichment(monkeypatch, error):
    def enrich(owner_id, media_id, **kwargs):
        assert owner_id == OWNER_ID
        assert media_id == MEDIA_ID
        assert kwargs["prefer_exact_detail"] is True
        raise error

    monkeypatch.setattr("app.downloader.discover_item_metadata_from_profile", enrich)


@pytest.mark.parametrize("code", DIAGNOSTIC_CODES)
def test_author_enrichment_preserves_fixed_diagnostic_without_raw_response(
    monkeypatch, native_item_metadata, code
):
    original = DiscoveryError(
        f"Internal response: {SECRET}",
        issue_code=SiteIssueCode.SITE_RESPONSE_CHANGED,
    )
    original.diagnostic_code = code
    fail_author_enrichment(monkeypatch, original)

    with pytest.raises(MediaDownloadError) as captured:
        native_item_metadata.discover(SOURCE_URL, Platform.DOUYIN, SourceKind.ITEM)

    error = captured.value
    assert str(error).endswith(f"Diagnostic code: {code}.")
    assert error.diagnostic_code == code
    assert error.issue_code == SiteIssueCode.SITE_RESPONSE_CHANGED
    assert error.__cause__ is original
    assert SECRET not in str(error)


@pytest.mark.parametrize(
    "invalid_code",
    [None, "unknown-reason", f"ssr-redirect {SECRET}", {"secret": SECRET}, 123],
)
def test_author_enrichment_uses_safe_code_when_attribute_is_missing_or_invalid(
    monkeypatch, native_item_metadata, invalid_code
):
    original = DiscoveryError(
        f"Diagnostic code: ssr-redirect. {SECRET}",
        issue_code=SiteIssueCode.SITE_RESPONSE_CHANGED,
    )
    if invalid_code is not None:
        original.diagnostic_code = invalid_code
    fail_author_enrichment(monkeypatch, original)

    with pytest.raises(MediaDownloadError) as captured:
        native_item_metadata.discover(SOURCE_URL, Platform.DOUYIN, SourceKind.ITEM)

    assert str(captured.value).endswith("Diagnostic code: signing-validation-failed.")
    assert captured.value.diagnostic_code == "signing-validation-failed"
    assert captured.value.__cause__ is original
    assert SECRET not in str(captured.value)
    assert "ssr-redirect" not in str(captured.value)


def test_signing_failure_code_survives_real_author_enrichment_wrapper(
    monkeypatch, native_item_metadata
):
    cause = douyin_signing._SigningFailure(
        "Douyin detail API returned a different author"
    )

    def fail_signed_request(*args, **kwargs):
        douyin_signing._raise_signing_error(SOURCE_URL, cause)

    monkeypatch.setattr("app.douyin.fetch_signed_aweme_detail", fail_signed_request)

    with pytest.raises(MediaDownloadError) as captured:
        native_item_metadata.discover(SOURCE_URL, Platform.DOUYIN, SourceKind.ITEM)

    assert str(captured.value).endswith("Diagnostic code: detail-author-mismatch.")
    assert captured.value.diagnostic_code == "detail-author-mismatch"
    assert captured.value.__cause__.__cause__ is cause
    assert captured.value.issue_code == SiteIssueCode.SITE_RESPONSE_CHANGED


def test_fresh_incomplete_exact_detail_is_not_reported_as_legacy_failure(
    monkeypatch, native_item_metadata
):
    def fetch_detail(media_id, **kwargs):
        assert media_id == MEDIA_ID
        assert kwargs["expected_sec_uid"] == OWNER_ID
        return {
            "aweme_id": MEDIA_ID,
            "author": {"sec_uid": OWNER_ID, "nickname": "Verified author"},
            "desc": SECRET,
        }

    monkeypatch.setattr("app.douyin.fetch_signed_aweme_detail", fetch_detail)
    monkeypatch.setattr(
        "app.douyin.fetch_signed_profile_awemes",
        lambda *args, **kwargs: pytest.fail(
            "Incomplete exact metadata must fail without scanning a profile"
        ),
    )

    with pytest.raises(MediaDownloadError) as captured:
        native_item_metadata.discover(SOURCE_URL, Platform.DOUYIN, SourceKind.ITEM)

    assert str(captured.value).endswith("Diagnostic code: detail-metadata-incomplete.")
    assert captured.value.diagnostic_code == "detail-metadata-incomplete"
    assert captured.value.__cause__.diagnostic_code == "detail-metadata-incomplete"
    assert captured.value.issue_code == SiteIssueCode.SITE_RESPONSE_CHANGED
    assert SECRET not in str(captured.value)


@pytest.mark.parametrize(
    "source_url",
    [
        f"https://www.douyin.com/user/self?modal_id={MEDIA_ID}&showTab=favorite_collection",
        f"https://www.douyin.com/user/{OWNER_ID}?from_tab_name=main&vid={MEDIA_ID}",
    ],
)
def test_new_job_persists_and_exposes_author_enrichment_diagnostic(
    monkeypatch, native_item_metadata, tmp_path, source_url
):
    from app import main as main_module

    original = DiscoveryError(
        f"Internal response: {SECRET}",
        issue_code=SiteIssueCode.SITE_RESPONSE_CHANGED,
    )
    original.diagnostic_code = "ssr-author-mismatch"
    fail_author_enrichment(monkeypatch, original)
    state_dir = tmp_path / "state"
    manager = DownloadManager(
        state_dir=state_dir,
        default_output_root=tmp_path / "downloads",
        downloader_config=DownloaderConfig(cookie_browser="chrome"),
    )
    monkeypatch.setattr(main_module, "manager", manager)
    client = TestClient(main_module.app, base_url="http://localhost")
    try:
        created = manager.create_job(source_url)
        manager._futures[created.id].result(timeout=5)
        job = manager.get_job(created.id)
        assert job.source_url == SOURCE_URL
        assert job.status == JobStatus.FAILED
        assert job.issue_code == SiteIssueCode.SITE_RESPONSE_CHANGED
        assert job.items == []
        assert job.error.endswith("Diagnostic code: ssr-author-mismatch.")
        assert job.verification_url is None
        stored = JsonJobStore(state_dir).get(job.id)
        assert stored.error == job.error
        assert stored.issue_code == job.issue_code
        response = client.get(f"/api/jobs/{job.id}")
        assert response.status_code == 200
        assert response.json()["error"] == job.error
        assert response.json()["issue_code"] == "site_response_changed"
        assert SECRET not in response.text
        assert SECRET not in (state_dir / f"{job.id}.json").read_text()
    finally:
        client.close()
        manager.shutdown(wait=True)


@pytest.mark.parametrize("source_kind", [SourceKind.PROFILE, SourceKind.ITEM])
@pytest.mark.parametrize(
    ("raw_code", "expected_code"),
    [
        ("ssr-author-mismatch", "ssr-author-mismatch"),
        ({"secret": SECRET}, "signing-validation-failed"),
    ],
)
def test_download_auto_refresh_preserves_diagnostic_in_paused_job_and_item(
    monkeypatch, tmp_path, source_kind, raw_code, expected_code
):
    profile_url = f"https://www.douyin.com/user/{OWNER_ID}"
    source_url = profile_url if source_kind == SourceKind.PROFILE else SOURCE_URL
    media_ids = [MEDIA_ID]
    if source_kind == SourceKind.PROFILE:
        media_ids.append("7649744769275263410")
    original = DiscoveryError(
        f"Internal signed response: {SECRET}",
        issue_code=SiteIssueCode.SITE_RESPONSE_CHANGED,
    )
    original.diagnostic_code = raw_code

    class RefreshFailureEngine:
        config = DownloaderConfig(cookie_browser="chrome")

        def __init__(self):
            self.discovery_calls = 0
            self.download_calls = []

        def discover(self, url, platform, kind, *, should_cancel):
            assert url == source_url
            self.discovery_calls += 1
            if self.discovery_calls > 1:
                assert source_kind == SourceKind.ITEM
                raise original
            items = []
            for media_id in media_ids:
                item_url = f"https://www.douyin.com/video/{media_id}"
                video_uri = f"verified-diagnostic-media-{media_id}"
                bound_media = {
                    "media_id": media_id,
                    "owner_id": OWNER_ID,
                    "media_kind": "video",
                    "video_uri": video_uri,
                    "minimum_width": 1080,
                    "minimum_height": 1920,
                    "direct_candidates": [
                        {
                            "video_uri": video_uri,
                            "width": 1080,
                            "height": 1920,
                            "bit_rate": 10_000_000,
                            "codec_hint": "hevc",
                            "urls": [f"https://v26-web.douyinvod.com/{media_id}.mp4"],
                        }
                    ],
                }
                metadata = (
                    {
                        "profile_url": profile_url,
                        "profile_owner_verified": True,
                        "douyin_profile_media": bound_media,
                    }
                    if source_kind == SourceKind.PROFILE
                    else {
                        "verification_url": item_url,
                        "item_identity_verified": True,
                        "douyin_item_media": bound_media,
                    }
                )
                items.append(
                    DownloadItem(
                        id=media_id,
                        media_id=media_id,
                        source_url=item_url,
                        author="Verified author",
                        title="Verified item",
                        media_type=MediaType.VIDEO,
                        metadata=metadata,
                    )
                )
            return DiscoveryResult(author="Verified author", items=items)

        def download_item(self, item, *args, **kwargs):
            self.download_calls.append(item.media_id)
            raise DouyinMediaRefreshRequiredError(
                "The verified media address requires a refresh"
            )

    refresh_calls = []

    def refresh_profile(owner_id, media_id, **kwargs):
        assert source_kind == SourceKind.PROFILE
        assert owner_id == OWNER_ID
        assert media_id == MEDIA_ID
        refresh_calls.append(media_id)
        raise original

    engine = RefreshFailureEngine()
    manager = DownloadManager(
        state_dir=tmp_path / "state",
        default_output_root=tmp_path / "downloads",
        max_workers=1,
    )
    monkeypatch.setattr(manager, "_engine_for_job", lambda job: engine)
    monkeypatch.setattr(
        "app.task_manager.discover_item_metadata_from_profile", refresh_profile
    )
    production_refresh = manager._refresh_douyin_media_during_run
    wrapped_errors = []

    def capture_refresh_error(*args, **kwargs):
        try:
            return production_refresh(*args, **kwargs)
        except TemporaryAccessError as error:
            wrapped_errors.append(error)
            raise

    monkeypatch.setattr(
        manager, "_refresh_douyin_media_during_run", capture_refresh_error
    )
    try:
        created = manager.create_job(source_url)
        manager._futures[created.id].result(timeout=5)
        job = manager.get_job(created.id)
        assert job.status == JobStatus.INTERRUPTED
        assert job.issue_code == SiteIssueCode.SITE_RESPONSE_CHANGED
        assert job.error.endswith(f"Diagnostic code: {expected_code}.")
        assert job.verification_url is None
        assert job.auth_message is None
        assert engine.download_calls == [MEDIA_ID]
        assert job.items[0].status == ItemStatus.FAILED
        assert job.items[0].error == job.error
        assert job.items[0].issue_code == job.issue_code
        assert all(item.output_paths == [] for item in job.items)
        assert len(wrapped_errors) == 1
        assert wrapped_errors[0].diagnostic_code == expected_code
        assert wrapped_errors[0].__cause__ is original
        assert SECRET not in job.model_dump_json()
        if source_kind == SourceKind.PROFILE:
            assert job.items[1].status == ItemStatus.QUEUED
            assert refresh_calls == [MEDIA_ID]
            assert engine.discovery_calls == 1
        else:
            assert refresh_calls == []
            assert engine.discovery_calls == 2
        stored = JsonJobStore(tmp_path / "state").get(job.id)
        assert stored.error == job.error
        assert stored.items[0].error == job.items[0].error
        assert stored.items[0].issue_code == job.issue_code
        assert SECRET not in stored.model_dump_json()
    finally:
        manager.shutdown(wait=True)
