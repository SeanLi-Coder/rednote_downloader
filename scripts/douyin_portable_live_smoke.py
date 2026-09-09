#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import platform as system_platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Sequence
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app import douyin_signing
from app.douyin import (
    is_complete_profile_media_metadata,
    verified_aweme_metadata,
)
from app.downloader import (
    DownloadOutcome,
    DownloaderConfig,
    EngineEvent,
    MediaDownloader,
    safe_external_error_message,
)
from app.models import DownloadItem, MediaType, Platform


MINIMUM_MEDIA_BYTES = 1024 * 1024
MINIMUM_LIVE_PHOTO_BYTES = 128 * 1024
MAXIMUM_DURATION_DELTA_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class LiveFixture:
    name: str
    media_id: str
    profile_id: str
    video_uri: str
    expected_width: int
    expected_height: int
    duration_ms: int


@dataclass(frozen=True, slots=True)
class LivePhotoFixture:
    name: str
    media_id: str
    profile_id: str
    static_width: int
    static_height: int
    direct_width: int
    direct_height: int
    duration_ms: int
    minimum_download_width: int
    minimum_download_height: int


LIVE_FIXTURES = (
    LiveFixture(
        name="public-1080p",
        media_id="7649279395044040154",
        profile_id=(
            "MS4wLjABAAAArpmD1ptinZVMBeuah9WXt8cQiuOm71RjunHN4wQYZR7-"
            "krAtvhvHQ8JCgvjGyuWc"
        ),
        video_uri="v0d00fg10000d8jr5rfog65nosu05tv0",
        expected_width=1080,
        expected_height=1920,
        duration_ms=4573,
    ),
    LiveFixture(
        name="public-1440p",
        media_id="7677165606521581157",
        profile_id=(
            "MS4wLjABAAAACtq2kRhidImbdwKxHUlU71QM0xeFVHUORqPWbAFQ09_"
            "KrOKlqoW-gwFRhpdm2H01"
        ),
        video_uri="v1e00fgi0000da5ca9fog65sr50kj6fg",
        expected_width=1440,
        expected_height=2560,
        duration_ms=11034,
    ),
)


TARGET_LIVE_PHOTO = LivePhotoFixture(
    name="target-live-photo-7683074221437746170",
    media_id="7683074221437746170",
    profile_id=(
        "MS4wLjABAAAAvLgZS-O6Oc9diWWZ-jctzlhanUBoN7a5oJLdsTkx6F9TVD9kehAq"
        "FqdrpG3uPlmz"
    ),
    static_width=2160,
    static_height=2880,
    direct_width=720,
    direct_height=960,
    duration_ms=2942,
    minimum_download_width=1308,
    minimum_download_height=1744,
)


def build_fixture_item(
    engine: MediaDownloader,
    fixture: LiveFixture,
) -> DownloadItem:
    profile_url = f"https://www.douyin.com/user/{fixture.profile_id}"
    source_url = f"https://www.douyin.com/video/{fixture.media_id}"
    cached_media: dict[str, Any] = {
        "media_kind": "video",
        "media_id": fixture.media_id,
        "owner_id": fixture.profile_id,
        "video_uri": fixture.video_uri,
        "title": f"Portable live smoke {fixture.media_id}",
        "author": "Public fixture",
        "duration_ms": fixture.duration_ms,
        "minimum_width": 720,
        "minimum_height": 1280,
        "direct_candidates": [
            {
                "video_uri": fixture.video_uri,
                "width": 720,
                "height": 1280,
                "urls": [engine._douyin_ratio_url(fixture.video_uri, "720p")],
            }
        ],
    }
    if not is_complete_profile_media_metadata(
        cached_media,
        fixture.media_id,
        fixture.profile_id,
    ):
        raise RuntimeError(f"Invalid live fixture metadata: {fixture.name}")
    return DownloadItem(
        id=f"portable-live-{fixture.media_id}",
        media_id=fixture.media_id,
        source_url=source_url,
        title=str(cached_media["title"]),
        author=str(cached_media["author"]),
        media_type=MediaType.VIDEO,
        metadata={
            "_job_id": "portable-live-smoke",
            "profile_url": profile_url,
            "profile_owner_verified": True,
            "douyin_profile_media": cached_media,
        },
    )


def discover_live_photo_metadata(
    fixture: LivePhotoFixture,
) -> dict[str, Any]:
    """Discover one public Live Photo through the production SSR entry point.

    The smoke deliberately replaces Chrome cookie loading with one empty jar. The
    Playwright conversion shim exists only because the production helper normally
    treats an empty browser jar as a user-facing login condition. It rejects every
    non-empty or substituted jar, so this path cannot consume local user cookies.
    The signed-request guard makes a passing smoke prove that canonical ``/note``
    SSR supplied the detail instead of the later API fallback.
    """

    verification_url = f"https://www.douyin.com/video/{fixture.media_id}"
    empty_cookie_jar = CookieJar()

    def empty_playwright_cookies(cookie_jar: CookieJar) -> list[dict[str, Any]]:
        if cookie_jar is not empty_cookie_jar or list(cookie_jar):
            raise RuntimeError("The Live Photo smoke received a non-empty cookie jar")
        return []

    def reject_signed_request_fallback(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError(
            "The target Live Photo was not present in canonical /note SSR"
        )

    with (
        mock.patch.object(
            douyin_signing,
            "_load_chrome_cookie_jar",
            return_value=empty_cookie_jar,
        ),
        mock.patch.object(
            douyin_signing,
            "_cookie_jar_to_playwright",
            side_effect=empty_playwright_cookies,
        ),
        mock.patch.object(
            douyin_signing,
            "_start_signed_fetch",
            side_effect=reject_signed_request_fallback,
        ),
    ):
        detail = douyin_signing.fetch_signed_aweme_detail(
            fixture.media_id,
            verification_url=verification_url,
            expected_sec_uid=fixture.profile_id,
            cookie_profile=None,
        )

    if list(empty_cookie_jar):
        raise RuntimeError("The Live Photo smoke unexpectedly retained cookies")
    metadata = verified_aweme_metadata(
        detail,
        fixture.media_id,
        expected_profile_id=fixture.profile_id,
    )
    if metadata is None:
        raise RuntimeError(
            "The target Live Photo detail failed strict item/author validation"
        )
    validate_live_photo_metadata(fixture, metadata)
    return metadata


def validate_live_photo_metadata(
    fixture: LivePhotoFixture,
    metadata: dict[str, Any],
) -> None:
    if not is_complete_profile_media_metadata(
        metadata,
        fixture.media_id,
        fixture.profile_id,
    ):
        raise RuntimeError("The target Live Photo metadata was incomplete")
    if metadata.get("media_kind") != "image":
        raise RuntimeError("The target Live Photo was not classified as an image post")
    if metadata.get("live_photo_static_fallback_indexes"):
        raise RuntimeError(
            "The target Live Photo metadata requested an unsafe static fallback"
        )

    image_assets = metadata.get("image_assets")
    live_assets = metadata.get("live_photo_assets")
    if not isinstance(image_assets, list) or len(image_assets) != 1:
        raise RuntimeError("The target Live Photo did not expose exactly one image")
    if not isinstance(live_assets, list) or len(live_assets) != 1:
        raise RuntimeError(
            "The target Live Photo did not expose exactly one motion asset"
        )

    image_asset = image_assets[0]
    live_asset = live_assets[0]
    if image_asset.get("index") != 1 or (
        int(image_asset.get("width") or 0),
        int(image_asset.get("height") or 0),
    ) != (fixture.static_width, fixture.static_height):
        raise RuntimeError(
            "The target Live Photo static asset did not match the expected "
            f"{fixture.static_width}x{fixture.static_height} dimensions"
        )
    if live_asset.get("index") != 1 or (
        int(live_asset.get("width") or 0),
        int(live_asset.get("height") or 0),
    ) != (fixture.direct_width, fixture.direct_height):
        raise RuntimeError(
            "The target Live Photo motion asset did not match the expected "
            f"{fixture.direct_width}x{fixture.direct_height} dimensions"
        )
    if int(live_asset.get("duration_ms") or 0) != fixture.duration_ms:
        raise RuntimeError(
            "The target Live Photo motion duration did not match the expected "
            f"{fixture.duration_ms}ms"
        )

    direct_candidates = live_asset.get("direct_candidates")
    if not isinstance(direct_candidates, list) or not direct_candidates:
        raise RuntimeError("The target Live Photo had no verified direct rendition")
    best_direct = max(
        direct_candidates,
        key=lambda value: int(value.get("width") or 0) * int(value.get("height") or 0),
    )
    if (
        int(best_direct.get("width") or 0),
        int(best_direct.get("height") or 0),
    ) != (fixture.direct_width, fixture.direct_height):
        raise RuntimeError(
            "The target Live Photo direct rendition dimensions changed unexpectedly"
        )


def build_live_photo_item(
    fixture: LivePhotoFixture,
    metadata: dict[str, Any],
) -> DownloadItem:
    validate_live_photo_metadata(fixture, metadata)
    source_url = f"https://www.douyin.com/video/{fixture.media_id}"
    return DownloadItem(
        id=f"portable-live-photo-{fixture.media_id}",
        media_id=fixture.media_id,
        source_url=source_url,
        title=str(metadata.get("title") or "Portable Live Photo smoke"),
        author=str(metadata.get("author") or "Public fixture"),
        media_type=MediaType.IMAGE,
        metadata={
            "_job_id": "portable-live-smoke",
            "profile_url": f"https://www.douyin.com/user/{fixture.profile_id}",
            "profile_owner_verified": True,
            "item_identity_verified": True,
            "verification_url": source_url,
            "douyin_item_media": metadata,
        },
    )


def inspect_local_media(path: Path, ffprobe: str) -> dict[str, Any]:
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height:format=duration,size,bit_rate",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(completed.stdout)
    streams = payload.get("streams") or []
    video = next(
        (
            value
            for value in streams
            if isinstance(value, dict) and value.get("codec_type") == "video"
        ),
        None,
    )
    if not isinstance(video, dict):
        raise RuntimeError("FFprobe found no video stream in the downloaded file")
    audio = next(
        (
            value
            for value in streams
            if isinstance(value, dict) and value.get("codec_type") == "audio"
        ),
        None,
    )
    media_format = payload.get("format") or {}
    return {
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "video_codec": str(video.get("codec_name") or ""),
        "audio_codec": (
            str(audio.get("codec_name") or "") if isinstance(audio, dict) else None
        ),
        "duration_seconds": float(media_format.get("duration") or 0),
        "size_bytes": int(media_format.get("size") or path.stat().st_size),
        "bit_rate": int(media_format.get("bit_rate") or 0),
    }


def validate_fixture_result(
    fixture: LiveFixture,
    outcome: DownloadOutcome,
    output_dir: Path,
    media: dict[str, Any],
) -> dict[str, Any]:
    if outcome.cookie_fallback_used:
        raise RuntimeError("The no-cookie live smoke unexpectedly used cookie fallback")
    if outcome.media_type != MediaType.VIDEO:
        raise RuntimeError("The live smoke result was not classified as video")
    expected_resolution = f"{fixture.expected_width}x{fixture.expected_height}"
    if outcome.resolution != expected_resolution:
        raise RuntimeError(
            "The production downloader did not select the expected highest "
            f"resolution: expected {expected_resolution}, got {outcome.resolution}"
        )
    expected_format_prefix = f"douyin-api-{expected_resolution}-"
    if not str(outcome.selected_format or "").startswith(expected_format_prefix):
        raise RuntimeError(
            "The production downloader did not report a verified Douyin API format"
        )
    if len(outcome.output_paths) != 1:
        raise RuntimeError(
            f"Expected one output path, received {len(outcome.output_paths)}"
        )

    output_root = output_dir.resolve()
    path = Path(outcome.output_paths[0]).resolve()
    if output_root not in path.parents:
        raise RuntimeError("The downloaded file escaped the smoke output directory")
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("The downloaded output is not a regular file")
    size_bytes = path.stat().st_size
    if size_bytes < MINIMUM_MEDIA_BYTES:
        raise RuntimeError(
            f"The downloaded media was unexpectedly small: {size_bytes} bytes"
        )
    with path.open("rb") as media_file:
        prefix = media_file.read(12)
    if len(prefix) < 12 or prefix[4:8] != b"ftyp":
        raise RuntimeError("The downloaded output did not contain an MP4 file header")
    output_files = sorted(
        candidate.resolve()
        for candidate in output_dir.rglob("*")
        if candidate.is_file()
    )
    if output_files != [path]:
        raise RuntimeError(
            f"Expected one downloaded file and no residue, found {len(output_files)}"
        )
    if (output_dir / ".parts").exists():
        raise RuntimeError("The production downloader left a .parts directory")

    actual_width = int(media.get("width") or 0)
    actual_height = int(media.get("height") or 0)
    if (actual_width, actual_height) != (
        fixture.expected_width,
        fixture.expected_height,
    ):
        raise RuntimeError(
            "Independent FFprobe did not confirm the expected highest resolution: "
            f"expected {expected_resolution}, got {actual_width}x{actual_height}"
        )
    duration_seconds = float(media.get("duration_seconds") or 0)
    expected_duration = fixture.duration_ms / 1000
    if abs(duration_seconds - expected_duration) > MAXIMUM_DURATION_DELTA_SECONDS:
        raise RuntimeError(
            "Independent FFprobe found a mismatched duration: "
            f"expected about {expected_duration:.3f}s, got {duration_seconds:.3f}s"
        )
    if not str(media.get("video_codec") or ""):
        raise RuntimeError("Independent FFprobe returned no video codec")
    if int(media.get("size_bytes") or 0) != size_bytes:
        raise RuntimeError("FFprobe size did not match the downloaded file size")

    digest = hashlib.sha256()
    with path.open("rb") as media_file:
        while chunk := media_file.read(1024 * 1024):
            digest.update(chunk)
    sha256 = digest.hexdigest()
    return {
        "name": fixture.name,
        "media_id": fixture.media_id,
        "status": "passed",
        "resolution": expected_resolution,
        "selected_format": outcome.selected_format,
        "video_codec": media["video_codec"],
        "audio_codec": media.get("audio_codec"),
        "duration_seconds": duration_seconds,
        "size_bytes": size_bytes,
        "bit_rate": int(media.get("bit_rate") or 0),
        "sha256": sha256,
        "output_file_count": len(output_files),
        "cookie_browser": None,
        "cookie_fallback_used": False,
    }


def validate_live_photo_result(
    fixture: LivePhotoFixture,
    outcome: DownloadOutcome,
    events: Sequence[EngineEvent],
    output_dir: Path,
    media: dict[str, Any],
) -> dict[str, Any]:
    if outcome.cookie_fallback_used:
        raise RuntimeError("The Live Photo smoke unexpectedly used cookie fallback")
    if outcome.media_type != MediaType.IMAGE:
        raise RuntimeError("The Live Photo post lost its image-post classification")
    if outcome.selected_format != "douyin-highest-live-photos-or-images":
        raise RuntimeError("The Live Photo outcome did not report motion-first media")
    if len(outcome.output_paths) != 1:
        raise RuntimeError(
            f"Expected one Live Photo output, received {len(outcome.output_paths)}"
        )

    completed_events = [event for event in events if event.event == "asset_completed"]
    if len(completed_events) != 1:
        raise RuntimeError(
            "Expected exactly one completed Live Photo asset event, received "
            f"{len(completed_events)}"
        )
    asset_event = completed_events[0]
    if not str(asset_event.selected_format or "").startswith(
        "douyin-highest-live-photo-"
    ):
        raise RuntimeError(
            "The downloaded Live Photo asset was not the verified motion rendition"
        )
    if asset_event.output_paths != outcome.output_paths:
        raise RuntimeError(
            "The Live Photo asset event and final outcome paths differed"
        )

    output_root = output_dir.resolve()
    path = Path(outcome.output_paths[0]).resolve()
    if output_root not in path.parents:
        raise RuntimeError("The Live Photo output escaped the smoke output directory")
    if path.suffix.lower() != ".mp4" or path.is_symlink() or not path.is_file():
        raise RuntimeError("The Live Photo output was not one regular MP4 file")
    size_bytes = path.stat().st_size
    if size_bytes < MINIMUM_LIVE_PHOTO_BYTES:
        raise RuntimeError(
            f"The Live Photo media was unexpectedly small: {size_bytes} bytes"
        )
    with path.open("rb") as media_file:
        prefix = media_file.read(12)
    if len(prefix) < 12 or prefix[4:8] != b"ftyp":
        raise RuntimeError("The Live Photo output did not contain an MP4 header")
    output_files = sorted(
        candidate.resolve()
        for candidate in output_dir.rglob("*")
        if candidate.is_file()
    )
    if output_files != [path]:
        raise RuntimeError(
            "The Live Photo smoke saved a static image or transfer residue; "
            f"found {len(output_files)} files"
        )
    if (output_dir / ".parts").exists():
        raise RuntimeError("The Live Photo smoke left a .parts directory")

    actual_width = int(media.get("width") or 0)
    actual_height = int(media.get("height") or 0)
    actual_resolution = f"{actual_width}x{actual_height}"
    if outcome.resolution != actual_resolution:
        raise RuntimeError(
            "The Live Photo outcome and independent FFprobe resolution differed: "
            f"outcome {outcome.resolution}, FFprobe {actual_resolution}"
        )
    if asset_event.resolution != actual_resolution:
        raise RuntimeError(
            "The Live Photo asset event and independent FFprobe resolution differed"
        )
    if min(actual_width, actual_height) < min(
        fixture.minimum_download_width,
        fixture.minimum_download_height,
    ) or max(actual_width, actual_height) < max(
        fixture.minimum_download_width,
        fixture.minimum_download_height,
    ):
        raise RuntimeError(
            "The downloaded Live Photo was below its required production floor: "
            f"expected at least {fixture.minimum_download_width}x"
            f"{fixture.minimum_download_height}, got {actual_resolution}"
        )
    duration_seconds = float(media.get("duration_seconds") or 0)
    expected_duration = fixture.duration_ms / 1_000
    if abs(duration_seconds - expected_duration) > MAXIMUM_DURATION_DELTA_SECONDS:
        raise RuntimeError(
            "Independent FFprobe found a mismatched Live Photo duration: "
            f"expected about {expected_duration:.3f}s, got {duration_seconds:.3f}s"
        )
    if not str(media.get("video_codec") or ""):
        raise RuntimeError("Independent FFprobe returned no Live Photo video codec")
    if int(media.get("size_bytes") or 0) != size_bytes:
        raise RuntimeError("FFprobe size did not match the Live Photo file size")

    digest = hashlib.sha256()
    with path.open("rb") as media_file:
        while chunk := media_file.read(1024 * 1024):
            digest.update(chunk)
    return {
        "name": fixture.name,
        "media_id": fixture.media_id,
        "status": "passed",
        "discovery": "canonical-note-ssr",
        "downloaded_media": "motion-mp4",
        "resolution": actual_resolution,
        "minimum_resolution": (
            f"{fixture.minimum_download_width}x{fixture.minimum_download_height}"
        ),
        "selected_format": asset_event.selected_format,
        "video_codec": media["video_codec"],
        "audio_codec": media.get("audio_codec"),
        "duration_seconds": duration_seconds,
        "size_bytes": size_bytes,
        "bit_rate": int(media.get("bit_rate") or 0),
        "sha256": digest.hexdigest(),
        "output_file_count": len(output_files),
        "cookie_browser": None,
        "cookie_fallback_used": False,
    }


def environment_report(ffprobe: str) -> dict[str, Any]:
    version = subprocess.run(
        [ffprobe, "-version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.splitlines()[0]
    return {
        "os": system_platform.system(),
        "os_release": system_platform.release(),
        "architecture": system_platform.machine(),
        "python": system_platform.python_version(),
        "ffprobe": version,
    }


def write_report(path: Path | None, report: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def run_live_smoke(
    fixtures: Sequence[LiveFixture],
    *,
    live_photo_fixture: LivePhotoFixture | None = TARGET_LIVE_PHOTO,
    report_path: Path | None = None,
) -> int:
    ffprobe = shutil.which("ffprobe")
    report: dict[str, Any] = {
        "status": "running",
        "environment": {},
        "fixtures": [],
    }
    write_report(report_path, report)
    if not ffprobe:
        report.update(
            status="failed",
            error="FFprobe was not found on PATH",
        )
        write_report(report_path, report)
        print(json.dumps(report, indent=2), file=sys.stderr)
        return 1

    try:
        report["environment"] = environment_report(ffprobe)
        engine = MediaDownloader(
            DownloaderConfig(
                cookie_browser=None,
                allow_cookie_fallback=False,
            )
        )
        if engine.config.cookie_browser is not None:
            raise RuntimeError("The portable live smoke must not read browser cookies")
        with tempfile.TemporaryDirectory(
            prefix="original-media-portable-live-"
        ) as temporary_directory:
            output_root = Path(temporary_directory)
            for fixture in fixtures:
                output_dir = output_root / fixture.name
                item = build_fixture_item(engine, fixture)
                outcome = engine.download_item(
                    item,
                    Platform.DOUYIN,
                    output_dir,
                )
                path = Path(outcome.output_paths[0]).resolve()
                media = inspect_local_media(path, ffprobe)
                result = validate_fixture_result(
                    fixture,
                    outcome,
                    output_dir,
                    media,
                )
                report["fixtures"].append(result)
                write_report(report_path, report)
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            if live_photo_fixture is not None:
                output_dir = output_root / live_photo_fixture.name
                metadata = discover_live_photo_metadata(live_photo_fixture)
                item = build_live_photo_item(live_photo_fixture, metadata)
                events: list[EngineEvent] = []
                outcome = engine.download_item(
                    item,
                    Platform.DOUYIN,
                    output_dir,
                    callback=events.append,
                )
                path = Path(outcome.output_paths[0]).resolve()
                media = inspect_local_media(path, ffprobe)
                result = validate_live_photo_result(
                    live_photo_fixture,
                    outcome,
                    events,
                    output_dir,
                    media,
                )
                report["fixtures"].append(result)
                write_report(report_path, report)
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    except Exception as exc:
        message = safe_external_error_message(exc)
        report.update(status="failed", error=message)
        write_report(report_path, report)
        print(f"Portable live smoke failed: {message}", file=sys.stderr)
        return 1

    report["status"] = "passed"
    write_report(report_path, report)
    print("Portable Douyin live smoke passed without browser cookies.")
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a no-cookie, full-file Douyin portability smoke test through the "
            "production downloader."
        )
    )
    parser.add_argument(
        "--fixture",
        action="append",
        choices=[
            *(fixture.name for fixture in LIVE_FIXTURES),
            TARGET_LIVE_PHOTO.name,
        ],
        help="Run only the named public fixture. Repeat to select multiple fixtures.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Write a JSON result report to this path.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    requested = set(args.fixture or [])
    fixtures = [
        fixture
        for fixture in LIVE_FIXTURES
        if not requested or fixture.name in requested
    ]
    live_photo_fixture = (
        TARGET_LIVE_PHOTO
        if not requested or TARGET_LIVE_PHOTO.name in requested
        else None
    )
    return run_live_smoke(
        fixtures,
        live_photo_fixture=live_photo_fixture,
        report_path=args.report,
    )


if __name__ == "__main__":
    raise SystemExit(main())
