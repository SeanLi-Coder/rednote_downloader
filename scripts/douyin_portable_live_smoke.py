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
from pathlib import Path
from typing import Any, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.douyin import is_complete_profile_media_metadata
from app.downloader import (
    DownloadOutcome,
    DownloaderConfig,
    MediaDownloader,
    safe_external_error_message,
)
from app.models import DownloadItem, MediaType, Platform


MINIMUM_MEDIA_BYTES = 1024 * 1024
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
        choices=[fixture.name for fixture in LIVE_FIXTURES],
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
    return run_live_smoke(fixtures, report_path=args.report)


if __name__ == "__main__":
    raise SystemExit(main())
