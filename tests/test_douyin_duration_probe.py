from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from yt_dlp.utils import DownloadCancelled

from app.downloader import (
    DOUYIN_PROBE_BYTES,
    DownloaderConfig,
    MediaDownloader,
    _DouyinProbeIntegrityChanged,
    _DouyinProbeRejected,
)


CANDIDATE_URL = "https://api-play.amemv.com/aweme/v1/play/?video_id=durationfixture"
FINAL_URL = "https://v26-web.douyinvod.com/duration-fixture.mp4"
PREFIX = b"\x00\x00\x00\x18ftypisom" + b"a" * (DOUYIN_PROBE_BYTES - 12)
FULL_PAYLOAD = PREFIX + b"complete-final-fragment"


class ProbeResponse:
    def __init__(self, payload: bytes, headers: dict[str, str]) -> None:
        self.payload = payload
        self.headers = headers
        self.url = FINAL_URL
        self.status = 206 if "Content-Range" in headers else 200
        self.offset = 0
        self.closed = False

    def read(self, size: int) -> bytes:
        chunk = self.payload[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class FixtureOrigin:
    def __init__(
        self,
        *,
        prefix: bytes = PREFIX,
        full_payload: bytes = FULL_PAYLOAD,
        known_size: bool = True,
        failure: str | None = None,
    ) -> None:
        self.prefix = prefix
        self.full_payload = full_payload
        self.known_size = known_size
        self.failure = failure
        self.requests: list[tuple[str, bool]] = []
        self.responses: list[ProbeResponse] = []
        self.cancelled = False

    def urlopen(self, request):
        is_range = any(key.lower() == "range" for key in request.headers)
        self.requests.append((request.url, is_range))
        headers = {"Content-Type": "video/mp4"}
        if is_range:
            payload = self.prefix
            total = str(len(self.full_payload)) if self.known_size else "*"
            headers["Content-Range"] = f"bytes 0-{len(payload) - 1}/{total}"
        else:
            payload = self.full_payload
            size = len(payload)
            if self.failure == "content":
                payload = b"different-file" + payload[len(b"different-file") :]
            elif self.failure == "declared_size":
                size += 1
            elif self.failure == "truncated":
                payload = payload[:-1]
            elif self.failure == "cancelled":
                self.cancelled = True
            headers["Content-Length"] = str(size)
        response = ProbeResponse(payload, headers)
        self.responses.append(response)
        return response


def install_origin(monkeypatch, engine: MediaDownloader, origin: FixtureOrigin):
    paths: list[Path] = []
    original_download = engine._download_douyin_probe_file

    def open_response(ydl, request, *, redirect_rejection_reason, should_cancel):
        assert ydl is origin
        if should_cancel():
            raise DownloadCancelled("Task cancelled")
        assert redirect_rejection_reason(request.url) is None
        response = origin.urlopen(request)
        assert redirect_rejection_reason(response.url) is None
        return response

    def download(ydl, url, path, **kwargs):
        paths.append(path)
        return original_download(ydl, url, path, **kwargs)

    monkeypatch.setattr(engine, "_open_douyin_media_response", open_response)
    monkeypatch.setattr(engine, "_download_douyin_probe_file", download)
    return paths


def install_ffprobe(monkeypatch, engine, *, prefix_duration=1.0, full_duration=12.0):
    calls: list[tuple[bytes, Path | None]] = []

    def ffprobe(data, *, local_path=None, should_cancel):
        assert not should_cancel()
        calls.append((data, local_path))
        if local_path is not None:
            assert local_path.read_bytes().startswith(data)
        return {
            "width": 1080,
            "height": 1920,
            "vcodec": "h264",
            "acodec": "aac",
            "bit_rate": 2_000_000,
            "duration": prefix_duration if local_path is None else full_duration,
        }

    monkeypatch.setattr(engine, "_ffprobe_douyin_media", ffprobe)
    return calls


def probe(engine, origin, *, callback=None):
    return engine._probe_douyin_candidate(
        origin,
        CANDIDATE_URL,
        expected_duration=12.0,
        callback=callback,
        should_cancel=lambda: origin.cancelled,
    )


@pytest.mark.parametrize("known_size", [True, False])
@pytest.mark.parametrize("prefix_duration", [1.0, 11.49, 12.51])
def test_positive_but_conflicting_prefix_duration_requires_complete_file(
    monkeypatch, known_size, prefix_duration
) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    origin = FixtureOrigin(known_size=known_size)
    paths = install_origin(monkeypatch, engine, origin)
    calls = install_ffprobe(monkeypatch, engine, prefix_duration=prefix_duration)
    events = []

    result = probe(engine, origin, callback=events.append)

    assert result and result["duration"] == 12.0
    assert (result["width"], result["height"]) == (1080, 1920)
    assert result["probe_prefix_size"] == DOUYIN_PROBE_BYTES
    assert result["probe_prefix_sha256"] == hashlib.sha256(PREFIX).hexdigest()
    assert [is_range for _, is_range in origin.requests] == [True, False]
    assert [local_path is not None for _, local_path in calls] == [False, True]
    assert all(response.closed for response in origin.responses)
    assert len(paths) == 1 and not paths[0].exists()
    assert any("original file" in (event.message or "") for event in events)


@pytest.mark.parametrize("full_duration", [1.0, 11.49, 12.51])
def test_complete_file_duration_must_still_match_without_wider_tolerance(
    monkeypatch, full_duration
) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    origin = FixtureOrigin()
    paths = install_origin(monkeypatch, engine, origin)
    calls = install_ffprobe(monkeypatch, engine, full_duration=full_duration)

    with pytest.raises(_DouyinProbeRejected, match="duration did not match"):
        probe(engine, origin)

    assert len(origin.requests) == 2
    assert len(calls) == 2 and calls[1][1] is not None
    assert all(response.closed for response in origin.responses)
    assert len(paths) == 1 and not paths[0].exists()


@pytest.mark.parametrize("full_duration", [11.5, 12.5])
def test_complete_file_preserves_existing_duration_tolerance(
    monkeypatch, full_duration
) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    origin = FixtureOrigin()
    install_origin(monkeypatch, engine, origin)
    install_ffprobe(monkeypatch, engine, full_duration=full_duration)

    result = probe(engine, origin)

    assert result and result["duration"] == full_duration
    assert len(origin.requests) == 2


@pytest.mark.parametrize("prefix_duration", [1.0, 12.0])
def test_already_complete_range_body_never_downloads_same_file_again(
    monkeypatch, prefix_duration
) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    entire_file = b"\x00\x00\x00\x18ftypisom-complete-file"
    origin = FixtureOrigin(prefix=entire_file, full_payload=entire_file)
    paths = install_origin(monkeypatch, engine, origin)
    calls = install_ffprobe(monkeypatch, engine, prefix_duration=prefix_duration)

    if prefix_duration == 12.0:
        assert probe(engine, origin)["duration"] == 12.0
    else:
        with pytest.raises(_DouyinProbeRejected, match="duration did not match"):
            probe(engine, origin)

    assert len(origin.requests) == 1
    assert len(calls) == 1
    assert not paths
    assert origin.responses[0].closed


@pytest.mark.parametrize(
    ("failure", "exception", "message"),
    [
        ("content", _DouyinProbeIntegrityChanged, "media content changed"),
        ("declared_size", _DouyinProbeIntegrityChanged, "media size changed"),
        ("truncated", _DouyinProbeIntegrityChanged, "media size changed"),
        ("cancelled", DownloadCancelled, "Task cancelled"),
    ],
)
def test_duration_reprobe_preserves_identity_size_and_cancellation_guards(
    monkeypatch, failure, exception, message
) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    origin = FixtureOrigin(failure=failure)
    paths = install_origin(monkeypatch, engine, origin)
    calls = install_ffprobe(monkeypatch, engine)

    with pytest.raises(exception, match=message):
        probe(engine, origin)

    assert len(origin.requests) == 2
    assert all(response.closed for response in origin.responses)
    assert len(calls) == 1
    assert len(paths) == 1 and not paths[0].exists()
    if failure in {"declared_size", "cancelled"}:
        assert origin.responses[1].offset == 0


def test_default_route_fallback_can_recover_with_full_duration_verified_media(
    monkeypatch,
) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    origin = FixtureOrigin()
    paths = install_origin(monkeypatch, engine, origin)
    calls: list[Path | None] = []

    def ffprobe(data, *, local_path=None, should_cancel):
        calls.append(local_path)
        return {
            "width": 1080,
            "height": 1920,
            "vcodec": "h264",
            "acodec": "aac",
            "bit_rate": 2_000_000,
            "duration": 12.0 if local_path and len(origin.requests) == 4 else 1.0,
        }

    monkeypatch.setattr(engine, "_ffprobe_douyin_media", ffprobe)

    result = engine._probe_douyin_default_with_fallback(
        origin,
        "durationfixture",
        expected_duration=12.0,
        callback=None,
        should_cancel=lambda: origin.cancelled,
    )

    assert result and result["duration"] == 12.0
    assert [is_range for _, is_range in origin.requests] == [True, False, True, False]
    routes = [urlsplit(url) for url, is_range in origin.requests if is_range]
    assert [(route.hostname, parse_qs(route.query)["line"][0]) for route in routes] == [
        ("api-play-hl.amemv.com", "0"),
        ("api-play.amemv.com", "1"),
    ]
    assert [path is not None for path in calls] == [False, True, False, True]
    assert len(paths) == 2 and all(not path.exists() for path in paths)
    assert all(response.closed for response in origin.responses)


def test_fragmented_mp4_uses_full_file_duration_instead_of_first_fragment(
    monkeypatch, tmp_path
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    if not ffmpeg or not engine._find_ffprobe_executable():
        pytest.skip("FFmpeg and FFprobe are required for the fragmented MP4 fixture")
    source = tmp_path / "fragmented.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30",
            "-t",
            "12",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-g",
            "30",
            "-movflags",
            "frag_keyframe+empty_moov+default_base_moof",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=45,
    )
    full_payload = source.read_bytes()
    prefix = full_payload[:DOUYIN_PROBE_BYTES]
    prefix_media = engine._ffprobe_douyin_media(prefix, should_cancel=lambda: False)
    assert len(full_payload) > DOUYIN_PROBE_BYTES
    assert engine._douyin_probe_metadata_complete(
        prefix_media, filesize=len(full_payload)
    )
    assert abs(
        float(prefix_media["duration"]) - 12.0
    ) > engine._douyin_duration_tolerance(12.0)
    origin = FixtureOrigin(prefix=prefix, full_payload=full_payload)
    paths = install_origin(monkeypatch, engine, origin)

    result = probe(engine, origin)

    assert result and float(result["duration"]) == pytest.approx(12.0, abs=0.05)
    assert (result["width"], result["height"]) == (640, 360)
    assert result["filesize"] == len(full_payload)
    assert len(origin.requests) == 2
    assert len(paths) == 1 and not paths[0].exists()
