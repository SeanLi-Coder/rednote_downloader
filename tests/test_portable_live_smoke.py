from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import douyin_signing
from app.douyin import is_complete_profile_media_metadata
from app.downloader import (
    DownloadOutcome,
    DownloaderConfig,
    EngineEvent,
    MediaDownloader,
)
from app.models import MediaType
from scripts import douyin_portable_live_smoke as smoke
from scripts.douyin_portable_live_smoke import (
    LIVE_FIXTURES,
    MINIMUM_LIVE_PHOTO_BYTES,
    MINIMUM_MEDIA_BYTES,
    TARGET_LIVE_PHOTO,
    build_fixture_item,
    build_live_photo_item,
    discover_live_photo_metadata,
    run_live_smoke,
    validate_fixture_result,
    validate_live_photo_metadata,
    validate_live_photo_result,
)


@pytest.mark.parametrize("fixture", LIVE_FIXTURES, ids=lambda value: value.name)
def test_live_fixture_is_identity_bound_and_uses_no_cookie(fixture) -> None:
    engine = MediaDownloader(
        DownloaderConfig(cookie_browser=None, allow_cookie_fallback=False)
    )

    item = build_fixture_item(engine, fixture)

    cached = item.metadata["douyin_profile_media"]
    assert engine.config.cookie_browser is None
    assert item.metadata["profile_owner_verified"] is True
    assert cached["media_id"] == fixture.media_id
    assert cached["owner_id"] == fixture.profile_id
    assert cached["video_uri"] == fixture.video_uri
    assert is_complete_profile_media_metadata(
        cached,
        fixture.media_id,
        fixture.profile_id,
    )


def _valid_outcome(tmp_path: Path):
    fixture = LIVE_FIXTURES[0]
    media_path = tmp_path / "fixture.mp4"
    media_path.write_bytes(
        b"\x00\x00\x00\x18ftypisom" + b"x" * (MINIMUM_MEDIA_BYTES - 12)
    )
    outcome = DownloadOutcome(
        output_paths=[str(media_path)],
        media_type=MediaType.VIDEO,
        selected_format=(
            f"douyin-api-{fixture.expected_width}x{fixture.expected_height}-1"
        ),
        resolution=f"{fixture.expected_width}x{fixture.expected_height}",
        cookie_fallback_used=False,
    )
    media = {
        "width": fixture.expected_width,
        "height": fixture.expected_height,
        "video_codec": "hevc",
        "audio_codec": "aac",
        "duration_seconds": fixture.duration_ms / 1000,
        "size_bytes": media_path.stat().st_size,
        "bit_rate": 1_000_000,
    }
    return fixture, media_path, outcome, media


def test_validate_fixture_result_accepts_complete_highest_quality_file(
    tmp_path: Path,
) -> None:
    fixture, media_path, outcome, media = _valid_outcome(tmp_path)

    result = validate_fixture_result(fixture, outcome, tmp_path, media)

    assert result["status"] == "passed"
    assert result["resolution"] == "1080x1920"
    assert result["size_bytes"] == media_path.stat().st_size
    assert len(result["sha256"]) == 64
    assert result["output_file_count"] == 1
    assert result["cookie_browser"] is None


def test_validate_fixture_result_rejects_lower_resolution(tmp_path: Path) -> None:
    fixture, _media_path, outcome, media = _valid_outcome(tmp_path)
    outcome.resolution = "720x1280"

    with pytest.raises(RuntimeError, match="expected highest resolution"):
        validate_fixture_result(fixture, outcome, tmp_path, media)


def _live_photo_detail(*, media_id: str | None = None, owner_id: str | None = None):
    fixture = TARGET_LIVE_PHOTO
    return {
        "aweme_id": media_id or fixture.media_id,
        "aweme_type": 68,
        "desc": "Portable target Live Photo",
        "create_time": 1_788_000_000,
        "author": {
            "sec_uid": owner_id or fixture.profile_id,
            "nickname": "Public Live Photo fixture",
        },
        "images": [
            {
                "width": fixture.static_width,
                "height": fixture.static_height,
                "url_list": ["https://p3-pc-sign.douyinpic.com/portable-static.webp"],
                "video": {
                    "width": fixture.direct_width,
                    "height": fixture.direct_height,
                    "duration": fixture.duration_ms,
                    "play_addr": {
                        "uri": "v0200fg10000portablelivephoto",
                        "width": fixture.direct_width,
                        "height": fixture.direct_height,
                        "url_list": ["https://v26-web.douyinvod.com/portable-live.mp4"],
                    },
                },
            }
        ],
    }


def test_target_live_photo_discovery_forces_empty_cookie_jar_and_strict_ssr(
    monkeypatch,
) -> None:
    calls = []

    def fail_browser_cookie_read(*_args, **_kwargs):
        raise AssertionError("The smoke must never read real browser cookies")

    def fetch_detail(aweme_id, **kwargs):
        calls.append((aweme_id, kwargs))
        cookie_jar = douyin_signing._load_chrome_cookie_jar(None)
        assert list(cookie_jar) == []
        assert douyin_signing._cookie_jar_to_playwright(cookie_jar) == []
        with pytest.raises(RuntimeError, match="canonical /note SSR"):
            douyin_signing._start_signed_fetch(None, None, None, None)
        return _live_photo_detail()

    monkeypatch.setattr(
        douyin_signing,
        "extract_cookies_from_browser",
        fail_browser_cookie_read,
    )
    monkeypatch.setattr(douyin_signing, "fetch_signed_aweme_detail", fetch_detail)

    metadata = discover_live_photo_metadata(TARGET_LIVE_PHOTO)

    assert calls == [
        (
            TARGET_LIVE_PHOTO.media_id,
            {
                "verification_url": (
                    "https://www.douyin.com/video/" f"{TARGET_LIVE_PHOTO.media_id}"
                ),
                "expected_sec_uid": TARGET_LIVE_PHOTO.profile_id,
                "cookie_profile": None,
            },
        )
    ]
    assert metadata["media_kind"] == "image"
    assert metadata["media_id"] == TARGET_LIVE_PHOTO.media_id
    assert metadata["owner_id"] == TARGET_LIVE_PHOTO.profile_id
    assert metadata["image_assets"][0]["width"] == 2160
    assert metadata["image_assets"][0]["height"] == 2880
    assert metadata["live_photo_assets"][0]["width"] == 720
    assert metadata["live_photo_assets"][0]["height"] == 960
    assert metadata["live_photo_assets"][0]["duration_ms"] == 2942
    assert "live_photo_static_fallback_indexes" not in metadata
    assert TARGET_LIVE_PHOTO.minimum_download_width == 1308
    assert TARGET_LIVE_PHOTO.minimum_download_height == 1744


@pytest.mark.parametrize(
    "detail",
    [
        _live_photo_detail(media_id="7683074221437746171"),
        _live_photo_detail(owner_id="MS4wLjABAAAAwrongportableowner"),
    ],
    ids=["wrong-item", "wrong-owner"],
)
def test_target_live_photo_discovery_rejects_cross_wired_ssr(
    monkeypatch,
    detail,
) -> None:
    monkeypatch.setattr(
        douyin_signing,
        "fetch_signed_aweme_detail",
        lambda *_args, **_kwargs: detail,
    )

    with pytest.raises(RuntimeError, match="strict item/author validation"):
        discover_live_photo_metadata(TARGET_LIVE_PHOTO)


def test_target_live_photo_builds_bound_production_download_item(monkeypatch) -> None:
    monkeypatch.setattr(
        douyin_signing,
        "fetch_signed_aweme_detail",
        lambda *_args, **_kwargs: _live_photo_detail(),
    )
    metadata = discover_live_photo_metadata(TARGET_LIVE_PHOTO)

    item = build_live_photo_item(TARGET_LIVE_PHOTO, metadata)

    assert item.media_type == MediaType.IMAGE
    assert item.media_id == TARGET_LIVE_PHOTO.media_id
    assert item.source_url == (
        f"https://www.douyin.com/video/{TARGET_LIVE_PHOTO.media_id}"
    )
    assert item.metadata["item_identity_verified"] is True
    assert item.metadata["verification_url"] == item.source_url
    assert item.metadata["douyin_item_media"] is metadata
    assert is_complete_profile_media_metadata(
        metadata,
        TARGET_LIVE_PHOTO.media_id,
        TARGET_LIVE_PHOTO.profile_id,
    )


def test_target_live_photo_offline_smoke_downloads_only_motion_mp4(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fixture = TARGET_LIVE_PHOTO
    report_path = tmp_path / "report.json"

    monkeypatch.setattr(smoke.shutil, "which", lambda _name: "/fake/ffprobe")
    monkeypatch.setattr(
        smoke,
        "environment_report",
        lambda _ffprobe: {
            "os": "TestOS",
            "os_release": "1",
            "architecture": "test-arch",
            "python": "3.10.0",
            "ffprobe": "ffprobe test",
        },
    )
    monkeypatch.setattr(
        douyin_signing,
        "fetch_signed_aweme_detail",
        lambda *_args, **_kwargs: _live_photo_detail(),
    )

    downloaded_size = MINIMUM_LIVE_PHOTO_BYTES + 4096

    def download_item(
        engine,
        item,
        platform,
        output_dir,
        *,
        callback=None,
        should_cancel=None,
    ):
        del should_cancel
        assert engine.config.cookie_browser is None
        assert engine.config.allow_cookie_fallback is False
        assert platform.value == "douyin"
        assert item.media_type == MediaType.IMAGE
        validate_live_photo_metadata(fixture, item.metadata["douyin_item_media"])
        destination = Path(output_dir) / (
            f"2026-09-09-Live Photo [{fixture.media_id}]-001.mp4"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(
            b"\x00\x00\x00\x18ftypisom" + b"x" * (downloaded_size - 12)
        )
        selected_format = "douyin-highest-live-photo-default-1308x1744"
        if callback:
            callback(
                EngineEvent(
                    event="asset_completed",
                    selected_format=selected_format,
                    resolution="1308x1744",
                    output_paths=[str(destination)],
                )
            )
        return DownloadOutcome(
            output_paths=[str(destination)],
            title=item.title,
            author=item.author,
            media_type=MediaType.IMAGE,
            selected_format="douyin-highest-live-photos-or-images",
            resolution="1308x1744",
            cookie_fallback_used=False,
        )

    monkeypatch.setattr(MediaDownloader, "download_item", download_item)
    monkeypatch.setattr(
        smoke,
        "inspect_local_media",
        lambda path, _ffprobe: {
            "width": 1308,
            "height": 1744,
            "video_codec": "h264",
            "audio_codec": "aac",
            "duration_seconds": 2.942,
            "size_bytes": path.stat().st_size,
            "bit_rate": 2_000_000,
        },
    )

    result = run_live_smoke(
        [],
        live_photo_fixture=fixture,
        report_path=report_path,
    )

    assert result == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "passed"
    assert len(report["fixtures"]) == 1
    live_result = report["fixtures"][0]
    assert live_result["name"] == fixture.name
    assert live_result["discovery"] == "canonical-note-ssr"
    assert live_result["downloaded_media"] == "motion-mp4"
    assert live_result["resolution"] == "1308x1744"
    assert live_result["minimum_resolution"] == "1308x1744"
    assert live_result["selected_format"].startswith("douyin-highest-live-photo-")
    assert live_result["video_codec"] == "h264"
    assert live_result["duration_seconds"] == 2.942
    assert live_result["size_bytes"] == downloaded_size
    assert len(live_result["sha256"]) == 64
    serialized = json.dumps(report, ensure_ascii=False)
    assert "https://" not in serialized
    assert "portable-static.webp" not in serialized
    assert "portable-live.mp4" not in serialized


def test_target_live_photo_rejects_direct_only_720p_download(tmp_path: Path) -> None:
    fixture = TARGET_LIVE_PHOTO
    media_path = tmp_path / f"Live Photo [{fixture.media_id}]-001.mp4"
    downloaded_size = MINIMUM_LIVE_PHOTO_BYTES + 4096
    media_path.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"x" * (downloaded_size - 12))
    selected_format = "douyin-highest-live-photo-direct-720x960"
    outcome = DownloadOutcome(
        output_paths=[str(media_path)],
        media_type=MediaType.IMAGE,
        selected_format="douyin-highest-live-photos-or-images",
        resolution="720x960",
        cookie_fallback_used=False,
    )
    events = [
        EngineEvent(
            event="asset_completed",
            selected_format=selected_format,
            resolution="720x960",
            output_paths=[str(media_path)],
        )
    ]
    media = {
        "width": fixture.direct_width,
        "height": fixture.direct_height,
        "video_codec": "h264",
        "audio_codec": "aac",
        "duration_seconds": fixture.duration_ms / 1_000,
        "size_bytes": media_path.stat().st_size,
        "bit_rate": 1_000_000,
    }

    with pytest.raises(RuntimeError, match="required production floor"):
        validate_live_photo_result(
            fixture,
            outcome,
            events,
            tmp_path,
            media,
        )
