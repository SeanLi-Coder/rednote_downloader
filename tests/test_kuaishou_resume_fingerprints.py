"""Isolated Kuaishou receipt regressions; synthetic media and no network."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.task_manager import _public_kuaishou_saved_asset
from test_kuaishou import (
    KUAISHOU_SAVED_ASSETS_KEY,
    MEDIA,
    VIDEO,
    DownloadCancelledError,
    DownloadItem,
    DownloadManager,
    EngineEvent,
    JsonJobStore,
    MediaDownloadError,
    MediaType,
    Platform,
    RemoteAsset,
    engine,
    make_real_jpeg,
    make_real_mp4,
)

HAS_FFMPEG = pytest.mark.skipif(
    not shutil.which("ffmpeg"), reason="Synthetic media tests require local FFmpeg",
)
HAS_VIDEO_TOOLS = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="Synthetic video tests require local FFmpeg and FFprobe",
)


def downloader():
    return engine.MediaDownloader(engine.DownloaderConfig(cookie_browser=None))


def receipt(path, asset, kind="image", index=1):
    return engine.MediaDownloader._kuaishou_completion_record(
        path, asset, "3xvideo1", kind, index, should_cancel=lambda: False,
    )


@HAS_FFMPEG
def test_same_size_decodable_image_replacement_is_not_reused(tmp_path):
    saved = make_real_jpeg(tmp_path, 320, 180, color="red", name="saved.jpg")
    other = make_real_jpeg(tmp_path, 320, 180, color="blue", name="other.jpg")
    size = max(saved.stat().st_size, other.stat().st_size)
    saved.write_bytes(saved.read_bytes().ljust(size, b"\0"))
    replacement = other.read_bytes().ljust(size, b"\0")
    asset = RemoteAsset([MEDIA + "?image=first&token=one"], 1, width=320, height=180)
    record = receipt(saved, asset)
    instance = downloader()
    assert instance._existing_kuaishou_image_asset(
        record, tmp_path, asset, should_cancel=lambda: False,
    ) is not None
    saved.write_bytes(replacement)
    instance._decode_local_image(saved, should_cancel=lambda: False)
    assert saved.stat().st_size == record["size"]
    assert instance._existing_kuaishou_image_asset(
        record, tmp_path, asset, should_cancel=lambda: False,
    ) is None
    assert saved.read_bytes() == replacement


@HAS_VIDEO_TOOLS
def test_same_size_valid_video_replacement_is_not_reused(tmp_path):
    saved = make_real_mp4(tmp_path, 320, 180, color="red", name="saved.mp4")
    other = make_real_mp4(tmp_path, 320, 180, color="blue", name="other.mp4")
    size = max(saved.stat().st_size, other.stat().st_size)
    saved.write_bytes(saved.read_bytes().ljust(size, b"\0"))
    replacement = other.read_bytes().ljust(size, b"\0")
    asset = RemoteAsset([MEDIA], 1, width=320, height=180, video_codec="h264", duration=1)
    record = receipt(saved, asset, "video")
    instance = downloader()
    assert instance._existing_kuaishou_video_asset(
        record, tmp_path, asset, should_cancel=lambda: False,
    ) is not None
    saved.write_bytes(replacement)
    instance._verify_local_video_asset(saved, asset, should_cancel=lambda: False)
    assert instance._existing_kuaishou_video_asset(
        record, tmp_path, asset, should_cancel=lambda: False,
    ) is None
    assert saved.read_bytes() == replacement


def test_source_fingerprint_preserves_all_query_values_but_not_candidate_order():
    first = MEDIA + "?image=first&token=one"
    backup = MEDIA + "?image=first&token=two"
    fingerprint = engine.MediaDownloader._kuaishou_source_fingerprint
    original = fingerprint(RemoteAsset([first, backup], 1), "image")
    assert original == fingerprint(RemoteAsset([backup, first, first], 9), "image")
    assert original != fingerprint(RemoteAsset([first.replace("first", "second"), backup], 1), "image")
    assert original != fingerprint(RemoteAsset([first.replace("one", "new"), backup], 1), "image")
    assert original != fingerprint(RemoteAsset([first, backup], 1), "video")
    with pytest.raises(MediaDownloadError, match="trusted source"):
        fingerprint(RemoteAsset(["https://untrusted.invalid/image.jpg"], 1), "image")


@HAS_FFMPEG
def test_reordered_same_dimension_image_and_legacy_receipt_fail_closed(tmp_path):
    saved = make_real_jpeg(tmp_path, 320, 180)
    original = RemoteAsset([MEDIA + "?image=first"], 1, width=320, height=180)
    changed = RemoteAsset([MEDIA + "?image=second"], 1, width=320, height=180)
    record = receipt(saved, original)
    instance = downloader()
    for current, evidence in (
        (changed, record),
        (original, {key: value for key, value in record.items() if key != "local_sha256"}),
        (original, {key: value for key, value in record.items() if key != "source_sha256"}),
        (original, {**record, "size": record["size"] + 1}),
    ):
        assert instance._existing_kuaishou_image_asset(
            evidence, tmp_path, current, should_cancel=lambda: False,
        ) is None


@HAS_VIDEO_TOOLS
def test_second_codec_rendition_survives_restart_without_transfer(monkeypatch, tmp_path):
    saved = make_real_mp4(tmp_path, 320, 180, name="saved.mp4")
    first = RemoteAsset([MEDIA + "?hevc"], 1, width=320, height=180, video_codec="hevc")
    second = RemoteAsset([MEDIA + "?h264"], 1, width=320, height=180, video_codec="h264")
    item = DownloadItem(
        id="fixture", media_id="3xvideo1", source_url=VIDEO,
        metadata={KUAISHOU_SAVED_ASSETS_KEY: [receipt(saved, second, "video")]},
    )
    instance = downloader()
    monkeypatch.setattr(instance, "_download_first_available_asset", lambda *a, **k: pytest.fail("A verified saved rendition must not transfer"))
    video = SimpleNamespace(
        media_id="3xvideo1", assets=[first, second], title="Fixture", author="Fixture",
        upload_date="2026-09-26", url=VIDEO,
    )
    events = []
    outcome = instance._download_kuaishou_video(
        item, video, tmp_path, callback=events.append, should_cancel=lambda: False,
    )
    assert outcome.output_paths == [str(saved)]
    assert events[0].asset_records[0]["source_sha256"] == receipt(saved, second, "video")["source_sha256"]
    assert list(tmp_path.glob("*.mp4")) == [saved]


@HAS_FFMPEG
@pytest.mark.parametrize("with_backup", [False, True])
def test_fresh_image_requires_full_decode_before_publish(monkeypatch, tmp_path, with_backup):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    good = make_real_jpeg(source_dir, 1600, 1200).read_bytes()
    broken = good[:3000]
    instance = downloader()
    assert instance._image_dimensions(broken) == (1600, 1200)
    urls = [MEDIA + "?broken"] + ([MEDIA + "?good"] if with_backup else [])
    requested = []

    def open_response(ydl, request, *, is_trusted_url):
        requested.append(request.url)
        payload = good if request.url.endswith("?good") else broken
        response = io.BytesIO(payload)
        response.url = request.url
        response.headers = {"Content-Type": "image/jpeg", "Content-Length": str(len(payload))}
        return response

    monkeypatch.setattr(engine, "_open_xiaohongshu_response", open_response)

    def transfer():
        return instance._download_first_available_asset(
            None, [RemoteAsset(urls, 1, width=1600, height=1200)], output,
            "2026-09-26", "Fixture", "3xvideo1", VIDEO,
            platform=Platform.KUAISHOU, media_type=MediaType.IMAGE,
            callback=None, should_cancel=lambda: False, asset_index=1,
            verify_declared_dimensions=True,
        )

    if with_backup:
        path, selected = transfer()
        assert path.read_bytes() == good
        assert selected.size == len(good)
        assert list(output.iterdir()) == [path]
    else:
        with pytest.raises(MediaDownloadError, match="decode"):
            transfer()
        assert list(output.iterdir()) == []
    assert requested == urls


def test_hash_is_streamed_and_cancellable_without_touching_file(tmp_path):
    path = tmp_path / "large.fixture"
    path.write_bytes(b"x" * (5 * 1024 * 1024))
    checks = 0

    def should_cancel():
        nonlocal checks
        checks += 1
        return checks == 4

    with pytest.raises(DownloadCancelledError):
        engine.MediaDownloader._kuaishou_local_fingerprint(path, should_cancel=should_cancel)
    assert path.stat().st_size == 5 * 1024 * 1024
    assert checks == 4


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO checks require POSIX")
def test_hash_rejects_fifo_without_waiting_for_a_writer(tmp_path):
    path = tmp_path / "not-media.fifo"
    os.mkfifo(path)
    result = subprocess.run(
        [sys.executable, "-c", """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from app.downloader import MediaDownloader
from app.errors import MediaDownloadError
try:
    MediaDownloader._kuaishou_local_fingerprint(Path(sys.argv[1]), should_cancel=lambda: False)
except MediaDownloadError as exc:
    assert "regular file" in str(exc)
else:
    raise AssertionError("A FIFO must not be accepted as media")
""", str(path), str(Path(engine.__file__).resolve().parents[1])],
        capture_output=True, timeout=5, check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")


@HAS_FFMPEG
def test_file_changed_during_decode_is_not_reused(monkeypatch, tmp_path):
    saved = make_real_jpeg(tmp_path, 320, 180)
    asset = RemoteAsset([MEDIA], 1, width=320, height=180)
    record = receipt(saved, asset)
    instance = downloader()

    def mutate(path, *, should_cancel):
        path.write_bytes(path.read_bytes() + b"changed")

    monkeypatch.setattr(instance, "_decode_local_image", mutate)
    assert instance._existing_kuaishou_image_asset(record, tmp_path, asset, should_cancel=lambda: False) is None


def test_retry_keeps_later_receipts_and_hashes_on_disk(tmp_path):
    state = tmp_path / "state"
    first = tmp_path / "first.fixture"
    third = tmp_path / "third.fixture"
    first.write_bytes(b"first")
    third.write_bytes(b"third")
    first_record = receipt(first, RemoteAsset([MEDIA + "?first"], 1), index=1)
    third_record = receipt(third, RemoteAsset([MEDIA + "?third"], 3), index=3)
    manager = DownloadManager(state_dir=state, default_output_root=tmp_path / "downloads", max_workers=1)
    try:
        job = manager.create_job(VIDEO, cookie_browser=None, auto_start=False)
        item = DownloadItem(
            id="fixture", media_id="3xvideo1", source_url=VIDEO,
            metadata={KUAISHOU_SAVED_ASSETS_KEY: [first_record, third_record]},
        )
        with manager._lock:
            manager._require_job(job.id).items.append(item)
        manager._on_engine_event(job.id, item.id, EngineEvent(
            event="asset_completed", output_paths=[str(first)], asset_records=[first_record],
        ))
    finally:
        manager.shutdown()
    stored = JsonJobStore(state).get(job.id).items[0].metadata[KUAISHOU_SAVED_ASSETS_KEY]
    assert [record["index"] for record in stored] == [1, 3]
    assert stored == [_public_kuaishou_saved_asset(first_record), _public_kuaishou_saved_asset(third_record)]
    assert MEDIA not in json.dumps(stored)


@pytest.mark.parametrize("field", ["source_sha256", "local_sha256"])
def test_receipt_digest_whitelist_never_persists_a_url(tmp_path, field):
    path = tmp_path / "fixture"
    path.write_bytes(b"fixture")
    record = receipt(path, RemoteAsset([MEDIA], 1))
    cleaned = _public_kuaishou_saved_asset({**record, field: MEDIA + "?token=private"})
    assert field not in cleaned
    assert MEDIA not in json.dumps(cleaned)
