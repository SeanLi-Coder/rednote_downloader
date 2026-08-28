from __future__ import annotations

from pathlib import Path

import pytest

from app.douyin import is_complete_profile_media_metadata
from app.downloader import DownloadOutcome, DownloaderConfig, MediaDownloader
from app.models import MediaType
from scripts.douyin_portable_live_smoke import (
    LIVE_FIXTURES,
    MINIMUM_MEDIA_BYTES,
    build_fixture_item,
    validate_fixture_result,
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
