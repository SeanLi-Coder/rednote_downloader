from __future__ import annotations

import json
import hashlib
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from yt_dlp import YoutubeDL
from yt_dlp.dependencies import urllib3
from yt_dlp.networking import Request, Response
from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.utils import DownloadCancelled, DownloadError

from app.downloader import (
    DOUYIN_MAX_MEDIA_REDIRECTS,
    DOUYIN_MAX_PROBE_FILE_BYTES,
    DOUYIN_REGIONAL_MEDIA_DOMAINS,
    DOUYIN_TRANSFER_ATTEMPTS,
    OUTPUT_TEMPLATE,
    DownloaderConfig,
    MediaDownloader,
    _DouyinProbeIntegrityChanged,
    _DouyinProbeRejected,
    _DouyinRedirectRejected,
    _SafeFilenamePostProcessor,
    _item_key,
    safe_component,
    safe_external_error_message,
)
from app.douyin import DouyinProfile
from app.errors import (
    AuthenticationRequiredError,
    DownloadCancelledError,
    DiscoveryError,
    MediaDownloadError,
    TemporaryAccessError,
)
from app.models import DownloadItem, MediaType, Platform, SourceKind
from app.xiaohongshu import RemoteAsset, XiaohongshuNote


class FakeYoutubeDL:
    created_options: list[dict] = []

    def __init__(self, options: dict) -> None:
        self.options = options
        self.created_options.append(options)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def add_post_processor(self, post_processor, when: str) -> None:
        self.post_processor = post_processor
        self.post_processor_when = when

    def extract_info(self, url: str, download: bool):
        if not download:
            return {
                "id": "channel-id",
                "channel": "Test Channel",
                "entries": [
                    {
                        "id": "abcdefghijk",
                        "title": "First video",
                        "upload_date": "20251114",
                        "channel": "Test Channel",
                        "extractor_key": "Youtube",
                        "url": "abcdefghijk",
                    }
                ],
            }

        output_dir = Path(self.options["paths"]["home"])
        output_file = output_dir / "2025-11-14-First video [abcdefghijk].mp4"
        output_file.write_bytes(b"media")
        info = {
            "id": "abcdefghijk",
            "title": "First video",
            "upload_date": "20251114",
            "channel": "Test Channel",
            "vcodec": "vp9",
            "requested_formats": [
                {
                    "format_id": "313",
                    "width": 3840,
                    "height": 2160,
                    "vcodec": "vp9",
                },
                {"format_id": "251", "vcodec": "none"},
            ],
            "requested_downloads": [{"filepath": str(output_file)}],
        }
        self.options["progress_hooks"][0](
            {
                "status": "downloading",
                "downloaded_bytes": 5_000,
                "total_bytes": 10_000,
                "speed": 2_000,
                "eta": 3,
                "filename": str(output_file),
                "info_dict": info,
            }
        )
        self.options["postprocessor_hooks"][0]({"status": "started", "info_dict": info})
        self.options["post_hooks"][0](str(output_file))
        return info

    def prepare_filename(self, info: dict) -> str:
        return str(Path(self.options["paths"]["home"]) / "unused.mp4")


def _inject_fake_douyin_media_opener(
    monkeypatch: pytest.MonkeyPatch,
    engine: MediaDownloader,
) -> None:
    """Adapt lightweight fake YDLs without weakening the production fail-closed path."""

    def open_response(
        ydl,
        request,
        *,
        redirect_rejection_reason,
        should_cancel=None,
    ):
        if should_cancel is not None and should_cancel():
            raise DownloadCancelled("Task cancelled")
        initial_reason = redirect_rejection_reason(request.url)
        if initial_reason is not None:
            raise engine._douyin_redirect_rejection(request.url, initial_reason)
        response = ydl.urlopen(request)
        final_url = str(getattr(response, "url", request.url))
        final_reason = redirect_rejection_reason(final_url)
        if final_reason is not None:
            response.close()
            raise engine._douyin_redirect_rejection(final_url, final_reason)
        return response

    monkeypatch.setattr(engine, "_open_douyin_media_response", open_response)


class _FakeRequestsRaw(BytesIO):
    def read(self, size: int = -1, decode_content: bool = False) -> bytes:
        return super().read(size)


class _FakeRequestsResponse:
    def __init__(
        self,
        url: str,
        *,
        location: str | None = None,
        payload: bytes = b"",
    ) -> None:
        self.url = url
        self.status_code = 302 if location is not None else 200
        self.reason = "Found" if location is not None else "OK"
        self.headers = {} if location is None else {"Location": location}
        self.is_redirect = location is not None
        self.raw = _FakeRequestsRaw(payload)
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self.raw.close()


class _QueuedRequestsSession:
    def __init__(self, responses: list[_FakeRequestsResponse]) -> None:
        self.responses = responses
        self.requests: list[dict] = []

    def request(self, **kwargs):
        self.requests.append(kwargs)
        if len(self.requests) > len(self.responses):
            raise AssertionError("Unexpected request after the queued redirect chain")
        return self.responses[len(self.requests) - 1]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("  测试 作者  ", "测试 作者"),
        ('A/B:C*D?E"F<G>H|I', "A_B_C_D_E_F_G_H_I"),
        ("CON", "_CON"),
        ("CON.txt", "_CON.txt"),
        ("COM1.log", "_COM1.log"),
        ("..", "Unknown Author"),
    ],
)
def test_safe_component_removes_unsafe_path_characters(
    value: str, expected: str
) -> None:
    assert safe_component(value) == expected


def test_safe_component_honors_length_limit() -> None:
    assert safe_component("a" * 200, limit=24) == "a" * 24


def test_douyin_author_prefers_display_name_over_numeric_uploader() -> None:
    assert (
        MediaDownloader._author_from_info(
            {
                "extractor_key": "Douyin",
                "uploader": "30509580784",
                "channel": "温柚柚",
            }
        )
        == "温柚柚"
    )
    assert (
        MediaDownloader._author_from_info(
            {
                "extractor_key": "Generic",
                "uploader": "Uploader Name",
                "channel": "Channel Name",
            }
        )
        == "Uploader Name"
    )


def test_item_key_is_stable_when_profile_tokens_change() -> None:
    first = _item_key(
        Platform.XIAOHONGSHU,
        "note-id",
        "https://www.xiaohongshu.com/explore/note-id?xsec_token=old",
        1,
    )
    refreshed = _item_key(
        Platform.XIAOHONGSHU,
        "note-id",
        "https://www.xiaohongshu.com/explore/note-id?xsec_token=new",
        999,
    )

    assert first == refreshed


def test_output_template_uses_date_prefix_and_explicit_unknown_fallback() -> None:
    ydl = YoutubeDL({"quiet": True, "outtmpl": OUTPUT_TEMPLATE})

    missing_date = Path(
        ydl.prepare_filename({"id": "item-id", "title": "Title", "ext": "mp4"})
    ).name
    dated = Path(
        ydl.prepare_filename(
            {
                "id": "item-id",
                "title": "Title",
                "ext": "mp4",
                "upload_date": "20251114",
            }
        )
    ).name

    assert missing_date == "Unknown-Date-Title [item-id].mp4"
    assert dated == "2025-11-14-Title [item-id].mp4"


@pytest.mark.parametrize(
    "title",
    [
        ":" * 500,
        "😀" * 500,
        "超长中文标题:/?*<>|" * 100,
    ],
)
def test_output_template_truncates_long_utf8_title_and_preserves_media_id(
    title: str,
) -> None:
    filenames = []

    with YoutubeDL(
        {"quiet": True, "windowsfilenames": True, "outtmpl": OUTPUT_TEMPLATE}
    ) as ydl:
        ydl.add_post_processor(
            _SafeFilenamePostProcessor(
                ydl,
                title_byte_limit=180,
                fallback_media_id="fallback-id",
            ),
            when="pre_process",
        )
        for media_id in ("different-id-one", "different-id-two"):
            info = {
                "id": media_id,
                "title": title,
                "ext": "mp4",
                "upload_date": "20251114",
            }
            info, _ = ydl.pre_process(info)
            filenames.append(Path(ydl.prepare_filename(info)).name)

    assert filenames[0].startswith("2025-11-14-")
    assert filenames[0].endswith(" [different-id-one].mp4")
    assert filenames[1].endswith(" [different-id-two].mp4")
    assert filenames[0] != filenames[1]
    assert all(len(filename.encode("utf-8")) <= 255 for filename in filenames)
    assert all("\ufffd" not in filename for filename in filenames)


def test_output_template_hashes_overlong_media_ids_to_prevent_collisions() -> None:
    shared_prefix = "media-id-" * 20
    filenames = []

    with YoutubeDL(
        {"quiet": True, "windowsfilenames": True, "outtmpl": OUTPUT_TEMPLATE}
    ) as ydl:
        for media_id in (f"{shared_prefix}one", f"{shared_prefix}two"):
            info = {
                "id": media_id,
                "title": "Title",
                "ext": "mp4",
                "upload_date": "20251114",
            }
            _, info = _SafeFilenamePostProcessor(
                ydl,
                title_byte_limit=180,
                fallback_media_id=media_id,
            ).run(info)
            filenames.append(Path(ydl.prepare_filename(info)).name)

    assert filenames[0] != filenames[1]
    assert all("~" in filename for filename in filenames)
    assert all(len(filename.encode("utf-8")) <= 255 for filename in filenames)


def test_ytdlp_deno_runtime_is_available_for_complete_youtube_formats() -> None:
    with YoutubeDL({"quiet": True, "js_runtimes": {"deno": {}}}) as ydl:
        runtime = ydl._js_runtimes["deno"]

    assert runtime is not None
    assert runtime.info is not None
    assert runtime.info.supported is True


def test_ytdlp_discovery_builds_stable_item_urls(monkeypatch) -> None:
    FakeYoutubeDL.created_options.clear()
    monkeypatch.setattr("app.downloader.YoutubeDL", FakeYoutubeDL)
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    result = engine.discover(
        "https://www.youtube.com/@Example/videos",
        Platform.YOUTUBE,
        SourceKind.PROFILE,
    )

    assert result.author == "Test Channel"
    assert len(result.items) == 1
    assert result.items[0].source_url == "https://www.youtube.com/watch?v=abcdefghijk"
    assert result.items[0].upload_date == "2025-11-14"
    assert FakeYoutubeDL.created_options[0]["skip_download"] is True
    assert FakeYoutubeDL.created_options[0]["extract_flat"] == "in_playlist"


def test_bilibili_item_discovery_expands_parts_with_real_metadata(
    monkeypatch,
) -> None:
    source_url = "https://www.bilibili.com/video/BV1rp4y1e745"

    class BilibiliItemYoutubeDL:
        created_options: list[dict] = []
        extracted_urls: list[str] = []

        def __init__(self, options: dict) -> None:
            self.options = options
            self.created_options.append(options)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

        def extract_info(self, url: str, download: bool):
            self.extracted_urls.append(url)
            assert download is False
            return {
                "_type": "playlist",
                "id": "BV1rp4y1e745",
                "title": "Multipart video",
                "entries": [
                    {
                        "id": "BV1rp4y1e745_p1",
                        "title": "Multipart video p01 First part",
                        "uploader": "Test Uploader",
                        "webpage_url": f"{source_url}?p=1",
                        "extractor_key": "BiliBili",
                        "upload_date": "20251114",
                        "vcodec": "hevc",
                    },
                    {
                        "id": "BV1rp4y1e745_p2",
                        "title": "Multipart video p02 Second part",
                        "uploader": "Test Uploader",
                        "webpage_url": f"{source_url}?p=2",
                        "extractor_key": "BiliBili",
                        "upload_date": "20251115",
                        "vcodec": "hevc",
                    },
                ],
            }

    monkeypatch.setattr("app.downloader.YoutubeDL", BilibiliItemYoutubeDL)
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    result = engine.discover(
        source_url,
        Platform.BILIBILI,
        SourceKind.ITEM,
    )

    assert BilibiliItemYoutubeDL.created_options[0]["extract_flat"] is False
    assert BilibiliItemYoutubeDL.extracted_urls == [source_url]
    assert result.author == "Test Uploader"
    assert [item.media_id for item in result.items] == [
        "BV1rp4y1e745_p1",
        "BV1rp4y1e745_p2",
    ]
    assert [item.title for item in result.items] == [
        "Multipart video p01 First part",
        "Multipart video p02 Second part",
    ]
    assert [item.source_url for item in result.items] == [
        f"{source_url}?p=1",
        f"{source_url}?p=2",
    ]
    assert len({item.id for item in result.items}) == 2
    assert all(item.author == "Test Uploader" for item in result.items)


def test_douyin_profile_items_keep_canonical_ids_and_profile_verification_url(
    monkeypatch,
) -> None:
    profile_url = (
        "https://www.douyin.com/user/"
        "MS4wLjABAAAAyjrP-yPP2JYTBFC6qw6lsg-7EU6jI-UJFhhJqludJSo"
    )
    video_urls = [
        "https://www.douyin.com/video/1111111111111111111",
        "https://www.douyin.com/video/2222222222222222222",
    ]
    monkeypatch.setattr(
        "app.downloader.discover_douyin_profile",
        lambda *args, **kwargs: DouyinProfile(
            "Test Author",
            video_urls,
            media_metadata={
                "1111111111111111111": {
                    "media_kind": "video",
                    "media_id": "1111111111111111111",
                    "owner_id": (
                        "MS4wLjABAAAAyjrP-yPP2JYTBFC6qw6lsg-7EU6jI-" "UJFhhJqludJSo"
                    ),
                    "video_uri": "v0200fg10000fixturevideoid",
                    "title": "Cached title",
                    "author": "Test Author",
                    "create_time": 1_756_656_000,
                },
                "2222222222222222222": {
                    "media_kind": "image",
                    "media_id": "2222222222222222222",
                    "owner_id": (
                        "MS4wLjABAAAAyjrP-yPP2JYTBFC6qw6lsg-7EU6jI-" "UJFhhJqludJSo"
                    ),
                    "title": "",
                    "author": "Test Author",
                    "create_time": 1_756_656_000,
                    "image_assets": [
                        {
                            "index": 1,
                            "width": 1440,
                            "height": 2560,
                            "candidates": [
                                "https://p3-pc-sign.douyinpic.com/original"
                            ],
                        }
                    ],
                }
            },
        ),
    )
    validated: list[tuple[str, str]] = []

    def complete(cached, media_id, owner_id):
        validated.append((media_id, owner_id))
        return cached.get("media_id") == media_id and cached.get("owner_id") == owner_id

    monkeypatch.setattr(
        "app.downloader.is_complete_profile_media_metadata",
        complete,
    )
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    result = engine.discover(profile_url, Platform.DOUYIN, SourceKind.PROFILE)

    assert [item.media_id for item in result.items] == [
        "1111111111111111111",
        "2222222222222222222",
    ]
    assert [item.source_url for item in result.items] == video_urls
    assert all(item.metadata["profile_url"] == profile_url for item in result.items)
    assert all(item.metadata["profile_owner_verified"] is True for item in result.items)
    assert result.items[0].title == "Cached title"
    assert result.items[0].upload_date == "2025-09-01"
    assert result.items[0].media_type == MediaType.VIDEO
    assert result.items[1].title == "Untitled Douyin image"
    assert result.items[1].upload_date == "2025-09-01"
    assert result.items[1].media_type == MediaType.IMAGE
    assert result.items[0].metadata["douyin_profile_media"]["video_uri"] == (
        "v0200fg10000fixturevideoid"
    )
    assert validated == [
        (
            "1111111111111111111",
            "MS4wLjABAAAAyjrP-yPP2JYTBFC6qw6lsg-7EU6jI-UJFhhJqludJSo",
        ),
        (
            "2222222222222222222",
            "MS4wLjABAAAAyjrP-yPP2JYTBFC6qw6lsg-7EU6jI-UJFhhJqludJSo",
        ),
    ]


def test_douyin_profile_discovery_rejects_incomplete_cache_without_placeholders(
    monkeypatch,
) -> None:
    profile_url = "https://www.douyin.com/user/verified-profile"
    video_url = "https://www.douyin.com/video/1111111111111111111"
    monkeypatch.setattr(
        "app.downloader.discover_douyin_profile",
        lambda *args, **kwargs: DouyinProfile(
            "Test Author",
            [video_url],
            media_metadata={},
        ),
    )
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    with pytest.raises(TemporaryAccessError, match="No numeric placeholder queue"):
        engine.discover(profile_url, Platform.DOUYIN, SourceKind.PROFILE)


def test_douyin_profile_image_downloads_highest_images_and_live_photos(
    monkeypatch,
    tmp_path,
) -> None:
    profile_id = "verified-profile"
    media_id = "1111111111111111111"
    profile_url = f"https://www.douyin.com/user/{profile_id}"
    cached = {
        "media_kind": "image",
        "media_id": media_id,
        "owner_id": profile_id,
        "title": "Verified image post",
        "author": "Verified Author",
        "create_time": 1_756_656_000,
        "image_assets": [
            {
                "index": 1,
                "width": 1440,
                "height": 2560,
                "candidates": ["https://p3-pc-sign.douyinpic.com/image-1"],
            },
            {
                "index": 2,
                "width": 1080,
                "height": 1920,
                "candidates": ["https://p9-pc-sign.douyinpic.com/image-2"],
            },
        ],
        "live_photo_assets": [
            {
                "index": 1,
                "width": 2160,
                "height": 3840,
                "candidates": ["https://v26-web.douyinvod.com/live-1.mp4"],
                "video_uri": "v0200fg10000verifiedlivephoto",
                "duration_ms": 2_000,
            }
        ],
    }
    item = DownloadItem(
        id="item-id",
        media_id=media_id,
        source_url=f"https://www.douyin.com/video/{media_id}",
        title=media_id,
        media_type=MediaType.IMAGE,
        metadata={
            "profile_url": profile_url,
            "profile_owner_verified": True,
            "douyin_profile_media": cached,
        },
    )
    monkeypatch.setattr(
        "app.downloader.is_complete_profile_media_metadata",
        lambda value, expected_id, expected_owner: (
            value is cached
            and expected_id == media_id
            and expected_owner == profile_id
        ),
    )
    monkeypatch.setattr("app.downloader.YoutubeDL", FakeYoutubeDL)
    engine = MediaDownloader(DownloaderConfig(cookie_browser="chrome"))
    monkeypatch.setattr(
        engine,
        "_download_with_ytdlp",
        lambda *args, **kwargs: pytest.fail("Douyin image must not use yt-dlp"),
    )
    calls: list[tuple[MediaType, int, int, int, bool, bool]] = []

    def download_asset(
        ydl,
        assets,
        *args,
        media_type,
        asset_index,
        progress_index,
        progress_count,
        verify_declared_dimensions=False,
        require_quality_fingerprint=False,
        **kwargs,
    ):
        asset = assets[0]
        calls.append(
            (
                media_type,
                asset_index,
                progress_index,
                progress_count,
                verify_declared_dimensions,
                require_quality_fingerprint,
            )
        )
        extension = "jpg" if media_type == MediaType.IMAGE else "mp4"
        return tmp_path / f"asset-{progress_index:03d}.{extension}", asset

    monkeypatch.setattr(engine, "_download_first_available_asset", download_asset)
    monkeypatch.setattr(
        engine,
        "_select_highest_douyin_live_photo_asset",
        lambda ydl, asset, **kwargs: asset,
    )
    events = []

    outcome = engine.download_item(
        item,
        Platform.DOUYIN,
        tmp_path,
        callback=events.append,
    )

    assert calls == [
        (MediaType.IMAGE, 1, 1, 3, True, False),
        (MediaType.IMAGE, 2, 2, 3, True, False),
        (MediaType.VIDEO, 1, 3, 3, True, True),
    ]
    assert outcome.output_paths == [
        str(tmp_path / "asset-001.jpg"),
        str(tmp_path / "asset-002.jpg"),
        str(tmp_path / "asset-003.mp4"),
    ]
    assert outcome.title == "Verified image post"
    assert outcome.author == "Verified Author"
    assert outcome.upload_date == "2025-09-01"
    assert outcome.media_type == MediaType.IMAGE
    assert outcome.selected_format == "douyin-highest-images+live-photos"
    assert outcome.resolution == "2160x3840"
    completed = [event for event in events if event.event == "asset_completed"]
    assert len(completed) == 3
    assert completed[-1].output_paths == outcome.output_paths


def test_douyin_profile_image_failure_keeps_completed_asset_paths(
    monkeypatch,
    tmp_path,
) -> None:
    profile_id = "verified-profile"
    media_id = "1111111111111111111"
    cached = {
        "media_kind": "image",
        "media_id": media_id,
        "owner_id": profile_id,
        "title": "Two images",
        "author": "Verified Author",
        "create_time": 1_756_656_000,
        "image_assets": [
            {
                "index": index,
                "width": 1080,
                "height": 1920,
                "candidates": [f"https://p3-pc-sign.douyinpic.com/image-{index}"],
            }
            for index in (1, 2)
        ],
    }
    item = DownloadItem(
        id="item-id",
        media_id=media_id,
        source_url=f"https://www.douyin.com/video/{media_id}",
        media_type=MediaType.IMAGE,
        metadata={
            "profile_url": f"https://www.douyin.com/user/{profile_id}",
            "profile_owner_verified": True,
            "douyin_profile_media": cached,
        },
    )
    monkeypatch.setattr(
        "app.downloader.is_complete_profile_media_metadata",
        lambda *args: True,
    )
    monkeypatch.setattr("app.downloader.YoutubeDL", FakeYoutubeDL)
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    def download_asset(*args, asset_index, **kwargs):
        if asset_index == 2:
            raise MediaDownloadError("temporary CDN error")
        return tmp_path / "first.jpg", args[1][0]

    monkeypatch.setattr(engine, "_download_first_available_asset", download_asset)
    events = []

    with pytest.raises(MediaDownloadError, match="Image 2 failed"):
        engine.download_item(
            item,
            Platform.DOUYIN,
            tmp_path,
            callback=events.append,
        )

    completed = [event for event in events if event.event == "asset_completed"]
    assert completed[-1].output_paths == [str(tmp_path / "first.jpg")]


def test_douyin_profile_retry_reuses_verified_saved_static_image(tmp_path) -> None:
    media_id = "1111111111111111111"
    saved = tmp_path / f"2025-09-01-Photo [{media_id}]-001.png"
    saved.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + (1440).to_bytes(4, "big")
        + (2560).to_bytes(4, "big")
        + b"saved-original-image"
    )
    item = DownloadItem(
        id="item-id",
        media_id=media_id,
        source_url=f"https://www.douyin.com/video/{media_id}",
        output_paths=[str(saved)],
    )
    asset = RemoteAsset(
        candidates=["https://p3-pc-sign.douyinpic.com/original.webp"],
        index=1,
        width=1440,
        height=2560,
        format_id="douyin-highest-image-1440x2560",
    )
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    reused = engine._existing_douyin_image_asset(
        item,
        tmp_path,
        media_id,
        asset,
    )

    assert reused is not None
    path, chosen = reused
    assert path == saved.resolve()
    assert (chosen.width, chosen.height) == (1440, 2560)
    assert chosen.size == saved.stat().st_size


def test_douyin_profile_image_rejects_unverified_cache_without_network(
    monkeypatch,
    tmp_path,
) -> None:
    media_id = "1111111111111111111"
    item = DownloadItem(
        id="item-id",
        media_id=media_id,
        source_url=f"https://www.douyin.com/video/{media_id}",
        media_type=MediaType.IMAGE,
        metadata={
            "profile_url": "https://www.douyin.com/user/verified-profile",
            "profile_owner_verified": False,
            "douyin_profile_media": {
                "media_kind": "image",
                "media_id": media_id,
                "owner_id": "different-profile",
            },
        },
    )
    monkeypatch.setattr(
        "app.downloader.is_complete_profile_media_metadata",
        lambda *args: False,
    )
    monkeypatch.setattr(
        "app.downloader.YoutubeDL",
        lambda *args, **kwargs: pytest.fail("Invalid cache must not access network"),
    )
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    with pytest.raises(MediaDownloadError, match="different item"):
        engine.download_item(item, Platform.DOUYIN, tmp_path)


def test_douyin_item_discovery_rejects_crosswired_extractor_id(
    monkeypatch,
) -> None:
    source_url = "https://www.douyin.com/video/1111111111111111111"

    class CrosswiredDiscoveryYoutubeDL(FakeYoutubeDL):
        def extract_info(self, url: str, download: bool, process: bool = True):
            return {
                "id": "2222222222222222222",
                "title": "Wrong video",
                "formats": [],
            }

    monkeypatch.setattr("app.downloader.YoutubeDL", CrosswiredDiscoveryYoutubeDL)
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    with pytest.raises(MediaDownloadError, match="different video") as error:
        engine.discover(source_url, Platform.DOUYIN, SourceKind.ITEM)

    assert "Chrome verification was not requested" in str(error.value)


def test_douyin_item_discovery_without_chrome_cookie_fails_closed(
    monkeypatch,
) -> None:
    media_id = "7664225419386607205"
    source_url = f"https://www.douyin.com/video/{media_id}"
    video_uri = "v0200fg10000fixturevideoid"

    class DirectItemYoutubeDL(FakeYoutubeDL):
        def extract_info(self, url: str, download: bool, process: bool = True):
            assert url == source_url
            assert download is False
            assert process is False
            return {
                "id": media_id,
                "title": "Verified title",
                "channel": "Verified author",
                "channel_id": "verified-owner",
                "duration": 23.4,
                "timestamp": 1_756_656_000,
                "extractor_key": "Douyin",
                "formats": [
                    {
                        "url": (
                            "https://api-play.amemv.com/aweme/v1/play/"
                            f"?video_id={video_uri}&ratio=720p"
                        ),
                        "width": 1440,
                        "height": 2560,
                    }
                ],
            }

    DirectItemYoutubeDL.created_options.clear()
    monkeypatch.setattr("app.downloader.YoutubeDL", DirectItemYoutubeDL)
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    with pytest.raises(TemporaryAccessError, match="Enable automatic Chrome Cookie"):
        engine.discover(source_url, Platform.DOUYIN, SourceKind.ITEM)

    assert DirectItemYoutubeDL.created_options[0]["noplaylist"] is True


def test_douyin_item_discovery_enriches_quality_from_bound_author_profile(
    monkeypatch,
) -> None:
    media_id = "7638230489560727931"
    owner_id = "MS4wLjABAAAAexpected-owner"
    source_url = f"https://www.douyin.com/video/{media_id}"
    video_uri = "v0200fg10000fixturevideoid"
    direct_url = "https://v26-web.douyinvod.com/verified-1440.mp4"

    class DirectItemYoutubeDL(FakeYoutubeDL):
        def extract_info(self, url: str, download: bool, process: bool = True):
            return {
                "id": media_id,
                "title": "Verified title",
                "channel": "Verified author",
                "channel_id": owner_id,
                "duration": 27.5,
                "formats": [
                    {
                        "url": (
                            "https://api-play.amemv.com/aweme/v1/play/"
                            f"?video_id={video_uri}&ratio=720p"
                        ),
                        "width": 720,
                        "height": 1280,
                    }
                ],
            }

    calls = []

    def enrich(profile_id, target_id, **kwargs):
        calls.append((profile_id, target_id))
        return {
            "video_uri": video_uri,
            "minimum_width": 1440,
            "minimum_height": 2560,
            "direct_candidates": [
                {
                    "width": 1440,
                    "height": 2560,
                    "codec_hint": "hevc",
                    "video_uri": video_uri,
                    "urls": [direct_url],
                }
            ],
        }

    monkeypatch.setattr("app.downloader.YoutubeDL", DirectItemYoutubeDL)
    monkeypatch.setattr(
        "app.downloader.discover_item_metadata_from_profile",
        enrich,
    )
    engine = MediaDownloader(DownloaderConfig(cookie_browser="chrome"))

    result = engine.discover(source_url, Platform.DOUYIN, SourceKind.ITEM)

    assert calls == [(owner_id, media_id)]
    cached = result.items[0].metadata["douyin_item_media"]
    assert cached["minimum_width"] == 1440
    assert cached["minimum_height"] == 2560
    assert cached["direct_candidates"][0]["urls"] == [direct_url]


def test_douyin_item_discovery_fails_closed_when_author_feed_is_unverified(
    monkeypatch,
) -> None:
    media_id = "7638230489560727931"
    source_url = f"https://www.douyin.com/video/{media_id}"
    video_uri = "v0200fg10000fixturevideoid"

    class DirectItemYoutubeDL(FakeYoutubeDL):
        def extract_info(self, url: str, download: bool, process: bool = True):
            return {
                "id": media_id,
                "title": "Verified title",
                "channel": "Verified author",
                "channel_id": "MS4wLjABAAAAexpected-owner",
                "formats": [
                    {
                        "url": (
                            "https://api-play.amemv.com/aweme/v1/play/"
                            f"?video_id={video_uri}&ratio=720p"
                        )
                    }
                ],
            }

    monkeypatch.setattr("app.downloader.YoutubeDL", DirectItemYoutubeDL)
    monkeypatch.setattr(
        "app.downloader.discover_item_metadata_from_profile",
        lambda *args, **kwargs: None,
    )
    engine = MediaDownloader(DownloaderConfig(cookie_browser="chrome"))

    with pytest.raises(TemporaryAccessError, match="highest quality") as error:
        engine.discover(source_url, Platform.DOUYIN, SourceKind.ITEM)

    assert "Chrome verification was not requested" in str(error.value)


def test_douyin_item_discovery_rejects_crosswired_enrichment_media_identity(
    monkeypatch,
) -> None:
    media_id = "7638230489560727931"
    source_url = f"https://www.douyin.com/video/{media_id}"
    native_video_uri = "v0200fg10000nativeidentity"

    class DirectItemYoutubeDL(FakeYoutubeDL):
        def extract_info(self, url: str, download: bool, process: bool = True):
            return {
                "id": media_id,
                "title": "Verified title",
                "channel": "Verified author",
                "channel_id": "MS4wLjABAAAAexpected-owner",
                "formats": [
                    {
                        "url": (
                            "https://api-play.amemv.com/aweme/v1/play/"
                            f"?video_id={native_video_uri}&ratio=720p"
                        )
                    }
                ],
            }

    monkeypatch.setattr("app.downloader.YoutubeDL", DirectItemYoutubeDL)
    monkeypatch.setattr(
        "app.downloader.discover_item_metadata_from_profile",
        lambda *args, **kwargs: {
            "video_uri": "v0200fg10000differentidentity",
            "minimum_width": 1440,
            "minimum_height": 2560,
            "direct_candidates": [
                {
                    "width": 1440,
                    "height": 2560,
                    "video_uri": "v0200fg10000differentidentity",
                    "urls": [
                        "https://v26-web.douyinvod.com/crosswired.mp4"
                    ],
                }
            ],
        },
    )
    engine = MediaDownloader(DownloaderConfig(cookie_browser="chrome"))

    with pytest.raises(MediaDownloadError, match="different media identity"):
        engine.discover(source_url, Platform.DOUYIN, SourceKind.ITEM)


def test_douyin_item_discovery_reports_transient_profile_limiting(
    monkeypatch,
) -> None:
    media_id = "7638230489560727931"
    source_url = f"https://www.douyin.com/video/{media_id}"
    video_uri = "v0200fg10000fixturevideoid"

    class DirectItemYoutubeDL(FakeYoutubeDL):
        def extract_info(self, url: str, download: bool, process: bool = True):
            return {
                "id": media_id,
                "title": "Verified title",
                "channel": "Verified author",
                "channel_id": "MS4wLjABAAAAexpected-owner",
                "formats": [
                    {
                        "url": (
                            "https://api-play.amemv.com/aweme/v1/play/"
                            f"?video_id={video_uri}&ratio=720p"
                        )
                    }
                ],
            }

    monkeypatch.setattr("app.downloader.YoutubeDL", DirectItemYoutubeDL)
    monkeypatch.setattr(
        "app.downloader.discover_item_metadata_from_profile",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            TemporaryAccessError("temporary profile limiting")
        ),
    )
    engine = MediaDownloader(DownloaderConfig(cookie_browser="chrome"))

    with pytest.raises(TemporaryAccessError, match="no lower-quality"):
        engine.discover(source_url, Platform.DOUYIN, SourceKind.ITEM)


def test_douyin_item_enrichment_never_lowers_native_quality_floor(
    monkeypatch,
) -> None:
    media_id = "7638230489560727931"
    source_url = f"https://www.douyin.com/video/{media_id}"
    video_uri = "v0200fg10000fixturevideoid"

    class DirectItemYoutubeDL(FakeYoutubeDL):
        def extract_info(self, url: str, download: bool, process: bool = True):
            return {
                "id": media_id,
                "title": "Verified title",
                "channel": "Verified author",
                "channel_id": "MS4wLjABAAAAexpected-owner",
                "formats": [
                    {
                        "url": (
                            "https://api-play.amemv.com/aweme/v1/play/"
                            f"?video_id={video_uri}&ratio=1080p"
                        ),
                        "width": 1080,
                        "height": 1920,
                    }
                ],
            }

    monkeypatch.setattr("app.downloader.YoutubeDL", DirectItemYoutubeDL)
    monkeypatch.setattr(
        "app.downloader.discover_item_metadata_from_profile",
        lambda *args, **kwargs: {
            "video_uri": video_uri,
            "minimum_width": 720,
            "minimum_height": 1280,
            "direct_candidates": [
                {
                    "width": 720,
                    "height": 1280,
                    "video_uri": video_uri,
                    "urls": ["https://v26-web.douyinvod.com/verified-720.mp4"],
                }
            ],
        },
    )
    engine = MediaDownloader(DownloaderConfig(cookie_browser="chrome"))

    result = engine.discover(source_url, Platform.DOUYIN, SourceKind.ITEM)

    cached = result.items[0].metadata["douyin_item_media"]
    assert cached["minimum_width"] == 1080
    assert cached["minimum_height"] == 1920


def test_douyin_item_discovery_rejects_playlist_shaped_result(
    monkeypatch,
) -> None:
    media_id = "7664225419386607205"
    source_url = f"https://www.douyin.com/video/{media_id}"

    class UnexpectedPlaylistYoutubeDL(FakeYoutubeDL):
        def extract_info(self, url: str, download: bool, process: bool = True):
            return {
                "_type": "playlist",
                "id": media_id,
                "channel": "Wrong profile",
                "entries": [
                    {"id": "7677923079457231738"},
                    {"id": "7677554129950241521"},
                ],
            }

    monkeypatch.setattr("app.downloader.YoutubeDL", UnexpectedPlaylistYoutubeDL)
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    with pytest.raises(MediaDownloadError, match="uploader profile") as error:
        engine.discover(source_url, Platform.DOUYIN, SourceKind.ITEM)

    assert "Chrome verification was not requested" in str(error.value)


def test_douyin_item_playlist_shape_uses_bound_signed_detail_fallback(
    monkeypatch,
) -> None:
    media_id = "7664225419386607205"
    source_url = f"https://www.douyin.com/video/{media_id}"
    calls = []

    class UnexpectedPlaylistYoutubeDL:
        def extract_info(self, url: str, download: bool, process: bool):
            return {
                "_type": "playlist",
                "id": media_id,
                "entries": [{"id": "7677923079457231738"}],
            }

    class FakeDouyinIE:
        def __init__(self, ydl) -> None:
            self.ydl = ydl

        def _parse_aweme_video_app(self, detail):
            return {"id": media_id, "title": "Signed item", "formats": []}

    def fetch(aweme_id, **kwargs):
        calls.append((aweme_id, kwargs))
        return {"aweme_id": media_id}

    monkeypatch.setattr("app.downloader.DouyinIE", FakeDouyinIE)
    monkeypatch.setattr("app.downloader.fetch_signed_aweme_detail", fetch)
    engine = MediaDownloader(DownloaderConfig(cookie_browser="chrome"))

    result = engine._extract_douyin_raw_info(
        UnexpectedPlaylistYoutubeDL(),
        source_url,
        expected_id=media_id,
        expected_profile_id=None,
        verification_url=source_url,
        profile_metadata=None,
        fallback_title=media_id,
        should_cancel=lambda: False,
    )

    assert result["id"] == media_id
    assert len(calls) == 1
    assert calls[0][0] == media_id
    assert calls[0][1]["verification_url"] == source_url
    assert calls[0][1]["expected_sec_uid"] is None
    assert calls[0][1]["cookie_profile"] is None


def test_douyin_item_crosswired_id_uses_bound_signed_detail_fallback(
    monkeypatch,
) -> None:
    media_id = "7664225419386607205"
    source_url = f"https://www.douyin.com/video/{media_id}"
    calls = []

    class CrosswiredYoutubeDL:
        def extract_info(self, url: str, download: bool, process: bool):
            return {
                "id": "7677923079457231738",
                "title": "Wrong item",
                "formats": [],
            }

    class FakeDouyinIE:
        def __init__(self, ydl) -> None:
            self.ydl = ydl

        def _parse_aweme_video_app(self, detail):
            return {"id": media_id, "title": "Signed item", "formats": []}

    def fetch(aweme_id, **kwargs):
        calls.append((aweme_id, kwargs))
        return {"aweme_id": media_id}

    monkeypatch.setattr("app.downloader.DouyinIE", FakeDouyinIE)
    monkeypatch.setattr("app.downloader.fetch_signed_aweme_detail", fetch)
    engine = MediaDownloader(DownloaderConfig(cookie_browser="chrome"))

    result = engine._extract_douyin_raw_info(
        CrosswiredYoutubeDL(),
        source_url,
        expected_id=media_id,
        expected_profile_id=None,
        verification_url=source_url,
        profile_metadata=None,
        fallback_title=media_id,
        should_cancel=lambda: False,
    )

    assert result["id"] == media_id
    assert len(calls) == 1
    assert calls[0][0] == media_id
    assert calls[0][1]["verification_url"] == source_url
    assert calls[0][1]["expected_sec_uid"] is None
    assert calls[0][1]["cookie_profile"] is None
    assert callable(calls[0][1]["should_cancel"])


def test_douyin_crosswired_source_is_rejected_before_ytdlp(
    monkeypatch, tmp_path
) -> None:
    profile_url = "https://www.douyin.com/user/expected-profile"

    class UnexpectedYoutubeDL:
        def __init__(self, options):
            raise AssertionError("yt-dlp must not run for a cross-wired item")

    monkeypatch.setattr("app.downloader.YoutubeDL", UnexpectedYoutubeDL)
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    item = DownloadItem(
        id="item-id",
        media_id="1111111111111111111",
        source_url="https://www.douyin.com/video/2222222222222222222",
        title="Expected video",
        media_type=MediaType.VIDEO,
        metadata={"profile_url": profile_url},
    )

    with pytest.raises(MediaDownloadError) as error:
        engine.download_item(item, Platform.DOUYIN, tmp_path)

    assert "Chrome verification was not requested" in str(error.value)


def test_douyin_extracted_id_mismatch_is_rejected_before_media_transfer(
    monkeypatch, tmp_path
) -> None:
    expected_id = "1111111111111111111"
    profile_url = "https://www.douyin.com/user/expected-profile"

    class CrosswiredYoutubeDL(FakeYoutubeDL):
        def extract_info(self, url: str, download: bool, process: bool = True):
            self.options["match_filter"](
                {"id": "2222222222222222222", "title": "Wrong video"},
                incomplete=False,
            )
            raise AssertionError("a mismatched item must be rejected before download")

    monkeypatch.setattr("app.downloader.YoutubeDL", CrosswiredYoutubeDL)
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    item = DownloadItem(
        id="item-id",
        media_id=expected_id,
        source_url=f"https://www.douyin.com/video/{expected_id}",
        title="Expected video",
        media_type=MediaType.VIDEO,
        metadata={"profile_url": profile_url},
    )

    with pytest.raises(MediaDownloadError) as error:
        engine.download_item(item, Platform.DOUYIN, tmp_path)

    assert "Chrome verification was not requested" in str(error.value)


def test_douyin_download_never_processes_playlist_shaped_item(
    monkeypatch, tmp_path
) -> None:
    media_id = "7664225419386607205"
    source_url = f"https://www.douyin.com/video/{media_id}"

    class PlaylistYoutubeDL(FakeYoutubeDL):
        def extract_info(self, url: str, download: bool, process: bool = True):
            return {
                "_type": "playlist",
                "id": media_id,
                "entries": [{"id": "7677923079457231738"}],
            }

        def process_ie_result(self, info, download: bool):
            raise AssertionError("playlist-shaped item must never be processed")

    monkeypatch.setattr("app.downloader.YoutubeDL", PlaylistYoutubeDL)
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    item = DownloadItem(
        id="direct-item",
        media_id=media_id,
        source_url=source_url,
        title="Direct item",
        media_type=MediaType.VIDEO,
        metadata={"verification_url": source_url},
    )

    with pytest.raises(MediaDownloadError, match="uploader profile") as error:
        engine.download_item(item, Platform.DOUYIN, tmp_path)

    assert "Chrome verification was not requested" in str(error.value)


@pytest.mark.parametrize("channel_id", ["profile-b", None])
def test_douyin_profile_rejects_self_consistent_item_from_wrong_author(
    monkeypatch, tmp_path, channel_id
) -> None:
    media_id = "2222222222222222222"
    profile_url = "https://www.douyin.com/user/profile-a"

    class WrongAuthorYoutubeDL(FakeYoutubeDL):
        def extract_info(self, url: str, download: bool, process: bool = True):
            return {
                "id": media_id,
                "channel_id": channel_id,
                "title": "Wrong author's video",
                "formats": [],
            }

        def process_ie_result(self, info, download: bool):
            raise AssertionError("wrong-author media must not reach processing")

    monkeypatch.setattr("app.downloader.YoutubeDL", WrongAuthorYoutubeDL)
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    item = DownloadItem(
        id="legacy-item",
        media_id=media_id,
        source_url=f"https://www.douyin.com/video/{media_id}",
        title="Contaminated legacy item",
        media_type=MediaType.VIDEO,
        metadata={"profile_url": profile_url},
    )

    with pytest.raises(MediaDownloadError) as error:
        engine.download_item(item, Platform.DOUYIN, tmp_path)

    assert "different author" in str(error.value)
    assert "Chrome verification was not requested" in str(error.value)


def test_douyin_signed_detail_fallback_parses_verified_raw_info(monkeypatch) -> None:
    media_id = "2222222222222222222"
    profile_id = "profile-a"
    profile_url = f"https://www.douyin.com/user/{profile_id}"
    source_url = f"https://www.douyin.com/video/{media_id}"
    calls = []

    class FreshCookieYoutubeDL:
        def extract_info(self, url: str, download: bool, process: bool):
            raise DownloadError("Fresh cookies are needed")

    class FakeDouyinIE:
        def __init__(self, ydl) -> None:
            self.ydl = ydl

        def _parse_aweme_video_app(self, detail):
            assert detail["aweme_id"] == media_id
            return {
                "id": media_id,
                "channel_id": profile_id,
                "title": "Signed detail",
                "formats": [],
            }

    def fetch(aweme_id, **kwargs):
        calls.append((aweme_id, kwargs))
        return {
            "aweme_id": media_id,
            "author": {"sec_uid": profile_id},
            "video": {},
        }

    monkeypatch.setattr("app.downloader.DouyinIE", FakeDouyinIE)
    monkeypatch.setattr("app.downloader.fetch_signed_aweme_detail", fetch)
    engine = MediaDownloader(DownloaderConfig(cookie_browser="chrome"))

    result = engine._extract_douyin_raw_info(
        FreshCookieYoutubeDL(),
        source_url,
        expected_id=media_id,
        expected_profile_id=profile_id,
        verification_url=profile_url,
        profile_metadata=None,
        fallback_title="Signed detail",
        should_cancel=lambda: False,
    )

    assert result["id"] == media_id
    assert result["channel_id"] == profile_id
    assert result["webpage_url"] == source_url
    assert calls[0][0] == media_id
    assert calls[0][1]["expected_sec_uid"] == profile_id
    assert calls[0][1]["verification_url"] == profile_url


def test_douyin_signed_detail_fallback_does_not_hide_unrelated_errors(
    monkeypatch,
) -> None:
    class NetworkFailureYoutubeDL:
        def extract_info(self, url: str, download: bool, process: bool):
            raise DownloadError("Connection reset by peer")

    monkeypatch.setattr(
        "app.downloader.fetch_signed_aweme_detail",
        lambda *args, **kwargs: pytest.fail("signed fallback must not run"),
    )
    engine = MediaDownloader(DownloaderConfig(cookie_browser="chrome"))

    with pytest.raises(DownloadError, match="Connection reset by peer"):
        engine._extract_douyin_raw_info(
            NetworkFailureYoutubeDL(),
            "https://www.douyin.com/video/2222222222222222222",
            expected_id="2222222222222222222",
            expected_profile_id="profile-a",
            verification_url="https://www.douyin.com/user/profile-a",
            profile_metadata=None,
            fallback_title="Douyin video",
            should_cancel=lambda: False,
        )


def test_douyin_verified_profile_metadata_skips_per_item_detail() -> None:
    media_id = "2222222222222222222"
    profile_id = "profile-a"
    video_uri = "v0200fg10000fixturevideoid"

    class UnexpectedYoutubeDL:
        def extract_info(self, *args, **kwargs):
            raise AssertionError("verified profile metadata must skip item detail")

    engine = MediaDownloader(DownloaderConfig(cookie_browser="chrome"))
    result = engine._extract_douyin_raw_info(
        UnexpectedYoutubeDL(),
        f"https://www.douyin.com/video/{media_id}",
        expected_id=media_id,
        expected_profile_id=profile_id,
        verification_url=f"https://www.douyin.com/user/{profile_id}",
        profile_metadata={
            "profile_owner_verified": True,
            "douyin_profile_media": {
                "media_id": media_id,
                "owner_id": profile_id,
                "media_kind": "video",
                "video_uri": video_uri,
                "direct_candidates": [
                    {
                        "width": 1080,
                        "height": 1920,
                        "video_uri": video_uri,
                        "urls": [
                            "https://v26-web.douyinvod.com/profile-direct.mp4"
                        ],
                    }
                ],
                "duration_ms": 72_800,
                "create_time": 1_756_656_000,
                "title": "Cached profile video",
                "author": "Profile A",
            },
        },
        fallback_title="Fallback title",
        should_cancel=lambda: False,
    )

    assert result["id"] == media_id
    assert result["channel_id"] == profile_id
    assert result["duration"] == 72.8
    assert result["timestamp"] == 1_756_656_000
    assert result["upload_date"] == "20250901"
    assert parse_qs(urlsplit(result["formats"][0]["url"]).query)["video_id"] == [
        video_uri
    ]
    with YoutubeDL({"quiet": True, "outtmpl": OUTPUT_TEMPLATE}) as ydl:
        assert Path(ydl.prepare_filename(result)).name.startswith("2025-09-01-")


def test_douyin_verified_item_metadata_skips_second_signed_detail() -> None:
    media_id = "7664225419386607205"
    source_url = f"https://www.douyin.com/video/{media_id}"
    video_uri = "v0200fg10000fixturevideoid"

    class UnexpectedYoutubeDL:
        def extract_info(self, *args, **kwargs):
            raise AssertionError("verified item metadata must skip item detail")

    engine = MediaDownloader(DownloaderConfig(cookie_browser="chrome"))
    result = engine._extract_douyin_raw_info(
        UnexpectedYoutubeDL(),
        source_url,
        expected_id=media_id,
        expected_profile_id=None,
        verification_url=source_url,
        profile_metadata={
            "verification_url": source_url,
            "item_identity_verified": True,
            "douyin_item_media": {
                "media_id": media_id,
                "owner_id": "verified-owner",
                "video_uri": video_uri,
                "minimum_width": 1080,
                "minimum_height": 1920,
                "direct_candidates": [
                    {
                        "width": 1080,
                        "height": 1920,
                        "video_uri": video_uri,
                        "urls": [
                            "https://v26-web.douyinvod.com/item-direct.mp4"
                        ],
                    }
                ],
                "duration_ms": 23_400,
                "create_time": 1_756_656_000,
                "title": "Cached item video",
                "author": "Verified author",
            },
        },
        fallback_title="Fallback title",
        should_cancel=lambda: False,
    )

    assert result["id"] == media_id
    assert result["channel_id"] == "verified-owner"
    assert result["duration"] == 23.4
    assert result["timestamp"] == 1_756_656_000
    assert result["upload_date"] == "20250901"
    assert result["_douyin_verified_cache_only"] is True
    assert result["_douyin_minimum_width"] == 1080
    assert result["_douyin_minimum_height"] == 1920
    assert parse_qs(urlsplit(result["formats"][0]["url"]).query)["video_id"] == [
        video_uri
    ]


def test_douyin_profile_metadata_rejects_wrong_cached_owner() -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser="chrome"))

    with pytest.raises(MediaDownloadError, match="different author"):
        engine._douyin_raw_info_from_profile_metadata(
            {
                "profile_owner_verified": True,
                "douyin_profile_media": {
                    "media_id": "2222222222222222222",
                    "owner_id": "profile-b",
                    "media_kind": "video",
                    "video_uri": "v0200fg10000fixturevideoid",
                },
            },
            expected_id="2222222222222222222",
            expected_profile_id="profile-a",
            verification_url="https://www.douyin.com/user/profile-a",
            fallback_title="Video",
        )


@pytest.mark.parametrize(
    "cached_fields",
    [
        {"video_uri": "v0200fg10000fixturevideoid"},
        {
            "media_kind": "video",
            "video_uri": "v0200fg10000fixturevideoid",
            "direct_candidates": [
                {
                    "width": 1440,
                    "height": 2560,
                    "video_uri": "v0200fg10000differentvideoid",
                    "urls": ["https://v26-web.douyinvod.com/mismatched.mp4"],
                }
            ],
        },
    ],
)
def test_douyin_incomplete_profile_cache_does_not_request_chrome_verification(
    cached_fields,
) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser="chrome"))

    with pytest.raises(MediaDownloadError, match="Chrome verification is not required"):
        engine._douyin_raw_info_from_profile_metadata(
            {
                "profile_owner_verified": True,
                "douyin_profile_media": {
                    "media_id": "2222222222222222222",
                    "owner_id": "profile-a",
                    **cached_fields,
                },
            },
            expected_id="2222222222222222222",
            expected_profile_id="profile-a",
            verification_url="https://www.douyin.com/user/profile-a",
            fallback_title="Video",
        )


def test_douyin_cached_profile_item_can_recover_default_2k_master(
    monkeypatch,
) -> None:
    media_id = "2222222222222222222"
    profile_id = "profile-a"
    video_uri = "v0200fg10000fixturevideoid"
    direct_url = "https://v26-web.douyinvod.com/verified-1080.mp4"
    engine = MediaDownloader(DownloaderConfig(cookie_browser="chrome"))
    info = engine._douyin_raw_info_from_profile_metadata(
        {
            "profile_owner_verified": True,
            "douyin_profile_media": {
                "media_id": media_id,
                "owner_id": profile_id,
                "media_kind": "video",
                "video_uri": video_uri,
                "duration_ms": 23_400,
                "direct_candidates": [
                    {
                        "width": 1080,
                        "height": 1920,
                        "bit_rate": 4_000_000,
                        "video_uri": video_uri,
                        "urls": [direct_url],
                    }
                ],
            },
        },
        expected_id=media_id,
        expected_profile_id=profile_id,
        verification_url=f"https://www.douyin.com/user/{profile_id}",
        fallback_title="Video",
    )
    assert info is not None

    def probe(ydl, url, *, expected_duration, should_cancel):
        if url == direct_url:
            width, height, bit_rate, filesize, codec = (
                1080,
                1920,
                4_000_000,
                10_000_000,
                "h264",
            )
        else:
            assert parse_qs(urlsplit(url).query)["ratio"] == ["default"]
            width, height, bit_rate, filesize, codec = (
                1440,
                2560,
                20_132_350,
                59_093_472,
                "h265",
            )
        return {
            "url": url,
            "width": width,
            "height": height,
            "bit_rate": bit_rate,
            "filesize": filesize,
            "duration": expected_duration,
            "vcodec": codec,
            "acodec": "aac",
        }

    monkeypatch.setattr(engine, "_probe_douyin_candidate", probe)
    assert engine._add_douyin_probe_formats(
        object(),
        info,
        expected_id=media_id,
        expected_profile_id=profile_id,
        verification_url=f"https://www.douyin.com/user/{profile_id}",
        should_cancel=lambda: False,
    )
    assert all(value["format_id"] != "profile-cached-play" for value in info["formats"])

    with YoutubeDL(
        {"quiet": True, **engine._download_format_options(Platform.DOUYIN)}
    ) as ydl:
        selected = ydl.process_ie_result(info, download=False)

    assert selected["format_id"].startswith("douyin-api-1440x2560")
    assert (selected["width"], selected["height"]) == (1440, 2560)


def test_douyin_profile_direct_rendition_beats_throttled_ratio_endpoints(
    monkeypatch,
) -> None:
    media_id = "2222222222222222222"
    profile_id = "profile-a"
    direct_url = "https://v26-web.douyinvod.com/verified-1440.mp4"
    engine = MediaDownloader(DownloaderConfig(cookie_browser="chrome"))
    info = engine._douyin_raw_info_from_profile_metadata(
        {
            "profile_owner_verified": True,
            "douyin_profile_media": {
                "media_id": media_id,
                "owner_id": profile_id,
                "media_kind": "video",
                "video_uri": "v0200fg10000fixturevideoid",
                "minimum_width": 1440,
                "minimum_height": 2560,
                "direct_candidates": [
                    {
                            "width": 1440,
                            "height": 2560,
                            "video_uri": "v0200fg10000fixturevideoid",
                            "bit_rate": 1_320_511,
                        "codec_hint": "hevc",
                        "urls": [direct_url],
                    }
                ],
            },
        },
        expected_id=media_id,
        expected_profile_id=profile_id,
        verification_url=f"https://www.douyin.com/user/{profile_id}",
        fallback_title="Video",
    )
    assert info is not None
    assert info["_douyin_minimum_width"] == 1440
    assert info["_douyin_minimum_height"] == 2560

    def probe(ydl, url, *, expected_duration, should_cancel):
        if url == direct_url:
            width, height, bit_rate, codec = 1440, 2560, 1_320_511, "hevc"
        else:
            ratio = parse_qs(urlsplit(url).query)["ratio"][0]
            if ratio == "720p":
                width, height, bit_rate, codec = 720, 1280, 700_000, "h264"
            else:
                width, height, bit_rate, codec = 1080, 1920, 1_000_000, "h264"
        return {
            "url": url,
            "width": width,
            "height": height,
            "bit_rate": bit_rate,
            "filesize": bit_rate * 3,
            "duration": expected_duration,
            "vcodec": codec,
            "acodec": "aac",
        }

    monkeypatch.setattr(engine, "_probe_douyin_candidate", probe)
    assert engine._add_douyin_probe_formats(
        object(),
        info,
        expected_id=media_id,
        expected_profile_id=profile_id,
        verification_url=f"https://www.douyin.com/user/{profile_id}",
        should_cancel=lambda: False,
    )
    assert any(
        value["format_id"].startswith("douyin-api-1440x2560")
        for value in info["formats"]
    )


def test_douyin_expired_author_feed_source_pauses_instead_of_downgrading(
    monkeypatch,
) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    info = _douyin_raw_info()
    info["_douyin_minimum_width"] = 1080
    info["_douyin_minimum_height"] = 1920
    info["_douyin_direct_candidates"] = [
        {
            "width": 1080,
            "height": 1920,
            "urls": ["https://v26-web.douyinvod.com/expired.mp4"],
        }
    ]

    def probe(ydl, url, *, expected_duration, should_cancel):
        if "expired.mp4" in url:
            raise TimeoutError("expired direct URL")
        return {
            "url": url,
            "width": 1080,
            "height": 1920,
            "bit_rate": 1_000_000,
            "filesize": 3_000_000,
            "duration": expected_duration,
            "vcodec": "h264",
            "acodec": "aac",
        }

    monkeypatch.setattr(engine, "_probe_douyin_candidate", probe)
    with pytest.raises(TemporaryAccessError, match="author-feed-1"):
        engine._add_douyin_probe_formats(
            object(),
            info,
            expected_id="1111111111111111111",
            verification_url="https://www.douyin.com/video/1111111111111111111",
            should_cancel=lambda: False,
        )


def test_douyin_higher_default_dominates_expired_lower_author_feed(
    monkeypatch,
) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    info = _douyin_raw_info()
    info["_douyin_direct_candidates"] = [
        {
            "width": 720,
            "height": 1280,
            "bit_rate": 700_000,
            "urls": ["https://v26-web.douyinvod.com/expired-720.mp4"],
        }
    ]

    def probe(ydl, url, *, expected_duration, should_cancel):
        if "expired-720.mp4" in url:
            raise TimeoutError("expired lower author-feed URL")
        return {
            "url": url,
            "width": 1080,
            "height": 1920,
            "bit_rate": 2_000_000,
            "filesize": 20_000_000,
            "duration": expected_duration,
            "vcodec": "h264",
            "acodec": "aac",
        }

    monkeypatch.setattr(engine, "_probe_douyin_candidate", probe)
    monkeypatch.setattr(
        engine,
        "_wait_for_douyin_probe_retry",
        lambda delay, should_cancel: None,
    )

    assert engine._add_douyin_probe_formats(
        object(),
        info,
        expected_id="1111111111111111111",
        verification_url="https://www.douyin.com/video/1111111111111111111",
        should_cancel=lambda: False,
    )
    assert any(
        value["format_id"].startswith("douyin-api-1080x1920")
        for value in info["formats"]
    )


def test_douyin_direct_floor_never_hides_ratio_probe_failure(monkeypatch) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    direct_url = "https://v26-web.douyinvod.com/verified-1080.mp4"
    info = _douyin_raw_info()
    info["_douyin_minimum_width"] = 1080
    info["_douyin_minimum_height"] = 1920
    info["_douyin_direct_candidates"] = [
        {"width": 1080, "height": 1920, "urls": [direct_url]}
    ]

    def probe(ydl, url, *, expected_duration, should_cancel):
        if url != direct_url and parse_qs(urlsplit(url).query)["ratio"][0] == "default":
            raise TimeoutError("default endpoint timed out")
        return {
            "url": url,
            "width": 1080,
            "height": 1920,
            "bit_rate": 1_000_000,
            "filesize": 3_000_000,
            "duration": expected_duration,
            "vcodec": "h264",
            "acodec": "aac",
        }

    monkeypatch.setattr(engine, "_probe_douyin_candidate", probe)
    with pytest.raises(TemporaryAccessError, match="default"):
        engine._add_douyin_probe_formats(
            object(),
            info,
            expected_id="1111111111111111111",
            verification_url="https://www.douyin.com/video/1111111111111111111",
            should_cancel=lambda: False,
        )


def test_douyin_cached_profile_item_never_falls_back_to_unverified_720(
    monkeypatch, tmp_path
) -> None:
    media_id = "2222222222222222222"
    profile_id = "profile-a"

    class NoProcessYoutubeDL(FakeYoutubeDL):
        def extract_info(self, *args, **kwargs):
            raise AssertionError("cached profile item must skip detail extraction")

        def process_ie_result(self, info, download: bool):
            raise AssertionError("unverified cached 720p must not be downloaded")

    monkeypatch.setattr("app.downloader.YoutubeDL", NoProcessYoutubeDL)
    engine = MediaDownloader(DownloaderConfig(cookie_browser="chrome"))
    monkeypatch.setattr(
        engine,
        "_add_douyin_probe_formats",
        lambda *args, **kwargs: False,
    )
    item = DownloadItem(
        id="cached-item",
        media_id=media_id,
        source_url=f"https://www.douyin.com/video/{media_id}",
        title="Cached video",
        media_type=MediaType.VIDEO,
        metadata={
            "profile_url": f"https://www.douyin.com/user/{profile_id}",
            "profile_owner_verified": True,
            "douyin_profile_media": {
                "media_id": media_id,
                "owner_id": profile_id,
                "media_kind": "video",
                "video_uri": "v0200fg10000fixturevideoid",
            },
        },
    )

    with pytest.raises(TemporaryAccessError, match="predates author-feed"):
        engine.download_item(item, Platform.DOUYIN, tmp_path)


def test_douyin_format_sort_prefers_highest_playable_resolution() -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    options = {
        "quiet": True,
        **engine._download_format_options(Platform.DOUYIN),
    }
    formats = [
        {
            "format_id": "720p-high-quality-rank",
            "url": "https://media.example/720.mp4",
            "ext": "mp4",
            "vcodec": "h264",
            "acodec": "aac",
            "width": 720,
            "height": 1080,
            "quality": 100,
            "preference": -1,
            "tbr": 3_000,
        },
        {
            "format_id": "1080p",
            "url": "https://media.example/1080.mp4",
            "ext": "mp4",
            "vcodec": "h264",
            "acodec": "aac",
            "width": 1080,
            "height": 1920,
            "quality": 50,
            "preference": -1,
            "tbr": 2_800,
        },
        {
            "format_id": "2k-h265",
            "url": "https://media.example/2k.mp4",
            "ext": "mp4",
            "vcodec": "h265",
            "acodec": "aac",
            "width": 1440,
            "height": 2560,
            "quality": 1,
            "preference": -1,
            "tbr": 2_500,
        },
        {
            "format_id": "2k-unplayable-vvc",
            "url": "https://media.example/2k-vvc.mp4",
            "ext": "mp4",
            "vcodec": "vvc",
            "acodec": "aac",
            "width": 1440,
            "height": 2560,
            "quality": 2,
            "preference": -100,
            "tbr": 4_000,
        },
    ]

    with YoutubeDL(options) as ydl:
        selected = ydl.process_ie_result(
            {
                "id": "1111111111111111111",
                "title": "Format ordering fixture",
                "formats": formats,
                "_format_sort_fields": ("quality", "codec", "size", "br"),
            },
            download=False,
        )

    assert selected["format_id"] == "2k-h265"
    assert (selected["width"], selected["height"]) == (1440, 2560)


def test_douyin_format_sort_prefers_1080x1920_over_720x1080() -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    formats = [
        {
            "format_id": "720p",
            "url": "https://media.example/720.mp4",
            "ext": "mp4",
            "vcodec": "h265",
            "acodec": "aac",
            "width": 720,
            "height": 1080,
            "quality": 100,
            "preference": -1,
        },
        {
            "format_id": "1080p",
            "url": "https://media.example/1080.mp4",
            "ext": "mp4",
            "vcodec": "h264",
            "acodec": "aac",
            "width": 1080,
            "height": 1920,
            "quality": 1,
            "preference": -1,
        },
    ]

    with YoutubeDL(
        {"quiet": True, **engine._download_format_options(Platform.DOUYIN)}
    ) as ydl:
        selected = ydl.process_ie_result(
            {
                "id": "1111111111111111111",
                "title": "Portrait fixture",
                "formats": formats,
                "_format_sort_fields": ("quality", "codec", "size", "br"),
            },
            download=False,
        )

    assert selected["format_id"] == "1080p"
    assert (selected["width"], selected["height"]) == (1080, 1920)


def _douyin_raw_info() -> dict:
    video_uri = "v0200fg10000fixturevideoid"
    direct_url = "https://v26-web.douyinvod.com/fixture-direct.mp4"
    return {
        "id": "1111111111111111111",
        "title": "Probe fixture",
        "duration": 100,
        "formats": [
            {
                "format_id": "native-1080",
                "url": (
                    "https://api-play.amemv.com/aweme/v1/play/"
                    f"?video_id={video_uri}&ratio=1080p"
                ),
                "ext": "mp4",
                "vcodec": "h264",
                "acodec": "aac",
                "width": 1080,
                "height": 1920,
                "quality": 1,
                "preference": -1,
            }
        ],
        "_format_sort_fields": ("quality", "codec", "size", "br"),
        "_douyin_direct_candidates": [
            {
                "width": 1080,
                "height": 1920,
                "bit_rate": 1_000_000,
                "urls": [direct_url],
            }
        ],
    }


def test_douyin_ratio_candidates_use_probed_resolution_not_requested_label(
    monkeypatch,
) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    info = _douyin_raw_info()
    actual = {
        "default": (1440, 2560, 20_132_350, 59_093_472),
        "4k": (1080, 1920, 1_100_000, 10_000_000),
        "2k": (1080, 1920, 1_100_000, 10_000_000),
        "1080p": (1080, 1920, 1_000_000, 9_000_000),
        "720p": (720, 1280, 700_000, 7_000_000),
    }
    probed_urls = []

    def probe(ydl, url, *, expected_duration, should_cancel):
        probed_urls.append(url)
        if "fixture-direct.mp4" in url:
            width, height, bit_rate, filesize = (1080, 1920, 1_000_000, 9_000_000)
            ratio = "author-feed"
        else:
            ratio = parse_qs(urlsplit(url).query)["ratio"][0]
            width, height, bit_rate, filesize = actual[ratio]
        return {
            "url": url,
            "width": width,
            "height": height,
            "bit_rate": bit_rate,
            "filesize": filesize,
            "duration": expected_duration,
            "vcodec": "h265" if ratio == "default" else "h264",
            "acodec": "aac",
        }

    monkeypatch.setattr(engine, "_probe_douyin_candidate", probe)

    added = engine._add_douyin_probe_formats(
        object(),
        info,
        expected_id="1111111111111111111",
        verification_url="https://www.douyin.com/user/expected-profile",
        should_cancel=lambda: False,
    )

    added_formats = [
        value
        for value in info["formats"]
        if value["format_id"].startswith("douyin-api-")
    ]
    assert added is True
    assert {(value["width"], value["height"]) for value in added_formats} == {
        (1080, 1920),
        (1440, 2560),
    }
    urls_by_ratio = {
        parse_qs(urlsplit(value).query)["ratio"][0]: value
        for value in probed_urls
        if parse_qs(urlsplit(value).query).get("ratio")
    }
    assert urlsplit(urls_by_ratio["default"]).hostname == "api-play-hl.amemv.com"
    assert set(urls_by_ratio) == {"default"}
    assert any("fixture-direct.mp4" in value for value in probed_urls)

    with YoutubeDL(
        {"quiet": True, **engine._download_format_options(Platform.DOUYIN)}
    ) as ydl:
        selected = ydl.process_ie_result(info, download=False)

    assert selected["format_id"].startswith("douyin-api-1440x2560")
    assert (selected["width"], selected["height"]) == (1440, 2560)
    assert selected["vcodec"] == "h265"


def test_douyin_verified_cache_without_direct_never_probes_default(
    monkeypatch,
) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    info = _douyin_raw_info()
    info.pop("_douyin_direct_candidates")
    info["_douyin_verified_cache_only"] = True
    monkeypatch.setattr(
        engine,
        "_probe_douyin_ratio_with_retry",
        lambda *args, **kwargs: pytest.fail("default must not be probed"),
    )

    with pytest.raises(TemporaryAccessError, match="default-only fallback"):
        engine._add_douyin_probe_formats(
            object(),
            info,
            expected_id="1111111111111111111",
            verification_url="https://www.douyin.com/video/1111111111111111111",
            should_cancel=lambda: False,
        )


def test_douyin_probe_reports_each_ratio_and_propagates_cancellation(
    monkeypatch,
) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    info = _douyin_raw_info()
    events = []

    monkeypatch.setattr(
        engine,
        "_probe_douyin_candidate",
        lambda ydl, url, *, expected_duration, should_cancel: None,
    )

    engine._add_douyin_probe_formats(
        object(),
        info,
        expected_id="1111111111111111111",
        verification_url="https://www.douyin.com/user/profile-a",
        callback=events.append,
        should_cancel=lambda: False,
    )

    assert [event.event for event in events] == ["probing", "probing"]
    assert [event.message for event in events] == [
        "Checking Douyin author-feed quality 1/1: 1080x1920",
        "Checking Douyin quality 1/1: default",
    ]

    def cancel_probe(ydl, url, *, expected_duration, should_cancel):
        raise DownloadCancelled("Task cancelled")

    monkeypatch.setattr(engine, "_probe_douyin_candidate", cancel_probe)
    with pytest.raises(DownloadCancelled):
        engine._add_douyin_probe_formats(
            object(),
            _douyin_raw_info(),
            expected_id="1111111111111111111",
            verification_url="https://www.douyin.com/user/profile-a",
            callback=events.append,
            should_cancel=lambda: False,
        )


def test_douyin_720_probe_does_not_override_native_1080(monkeypatch) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    info = _douyin_raw_info()

    def probe(ydl, url, *, expected_duration, should_cancel):
        return {
            "url": url,
            "width": 720,
            "height": 1280,
            "bit_rate": 700_000,
            "filesize": 7_000_000,
            "duration": expected_duration,
            "vcodec": "h264",
            "acodec": "aac",
        }

    monkeypatch.setattr(engine, "_probe_douyin_candidate", probe)
    engine._add_douyin_probe_formats(
        object(),
        info,
        expected_id="1111111111111111111",
        verification_url="https://www.douyin.com/user/expected-profile",
        should_cancel=lambda: False,
    )

    with YoutubeDL(
        {"quiet": True, **engine._download_format_options(Platform.DOUYIN)}
    ) as ydl:
        selected = ydl.process_ie_result(info, download=False)

    assert selected["format_id"] == "native-1080"
    assert (selected["width"], selected["height"]) == (1080, 1920)


def test_douyin_cached_floor_blocks_silently_throttled_720_probes(
    monkeypatch,
) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    info = _douyin_raw_info()
    info["formats"][0]["format_id"] = "item-cached-play"
    info["_douyin_verified_cache_only"] = True
    info["_douyin_minimum_width"] = 1080
    info["_douyin_minimum_height"] = 1920

    def probe(ydl, url, *, expected_duration, should_cancel):
        return {
            "url": url,
            "width": 720,
            "height": 1280,
            "bit_rate": 700_000,
            "filesize": 7_000_000,
            "duration": expected_duration,
            "vcodec": "h264",
            "acodec": "aac",
        }

    monkeypatch.setattr(engine, "_probe_douyin_candidate", probe)

    assert (
        engine._add_douyin_probe_formats(
            object(),
            info,
            expected_id="1111111111111111111",
            verification_url="https://www.douyin.com/video/1111111111111111111",
            should_cancel=lambda: False,
        )
        is False
    )
    assert not any(
        value["format_id"].startswith("douyin-api-") for value in info["formats"]
    )
    assert "720x1280" in info["_douyin_probe_failure"]
    assert "minimum 1080x1920" in info["_douyin_probe_failure"]


def test_douyin_native_720_floor_accepts_verified_720_probes(monkeypatch) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    info = _douyin_raw_info()
    info["formats"][0]["format_id"] = "item-cached-play"
    info["_douyin_verified_cache_only"] = True
    info["_douyin_minimum_width"] = 720
    info["_douyin_minimum_height"] = 1280
    info["_douyin_direct_candidates"][0].update(
        {"width": 720, "height": 1280, "bit_rate": 700_000}
    )

    def probe(ydl, url, *, expected_duration, should_cancel):
        return {
            "url": url,
            "width": 720,
            "height": 1280,
            "bit_rate": 700_000,
            "filesize": 7_000_000,
            "duration": expected_duration,
            "vcodec": "h264",
            "acodec": "aac",
        }

    monkeypatch.setattr(engine, "_probe_douyin_candidate", probe)

    assert engine._add_douyin_probe_formats(
        object(),
        info,
        expected_id="1111111111111111111",
        verification_url="https://www.douyin.com/video/1111111111111111111",
        should_cancel=lambda: False,
    )
    assert any(
        value["format_id"].startswith("douyin-api-720x1280")
        for value in info["formats"]
    )


def test_douyin_unsupported_top_codec_blocks_lower_quality(
    monkeypatch,
) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    info = _douyin_raw_info()

    def probe(ydl, url, *, expected_duration, should_cancel):
        if "fixture-direct.mp4" in url:
            width, height, bit_rate, filesize, codec = (
                1080,
                1920,
                1_000_000,
                10_000_000,
                "h264",
            )
        else:
            width, height, bit_rate, filesize, codec = (
                2160,
                3840,
                4_000_000,
                40_000_000,
                "vvc",
            )
        return {
            "url": url,
            "width": width,
            "height": height,
            "bit_rate": bit_rate,
            "filesize": filesize,
            "duration": expected_duration,
            "vcodec": codec,
            "acodec": "aac",
        }

    monkeypatch.setattr(engine, "_probe_douyin_candidate", probe)
    assert (
        engine._add_douyin_probe_formats(
            object(),
            info,
            expected_id="1111111111111111111",
            verification_url="https://www.douyin.com/user/expected-profile",
            should_cancel=lambda: False,
        )
        is False
    )
    assert not any(
        value["format_id"].startswith("douyin-api-") for value in info["formats"]
    )
    assert "unsupported video codec vvc" in info["_douyin_probe_failure"]


def test_douyin_lower_resolution_unsupported_codec_does_not_block_best(
    monkeypatch,
) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    info = _douyin_raw_info()

    def probe(ydl, url, *, expected_duration, should_cancel):
        if "fixture-direct.mp4" in url:
            width, height, codec = 720, 1280, "vvc"
        else:
            width, height, codec = 1440, 2560, "hevc"
        return {
            "url": url,
            "width": width,
            "height": height,
            "bit_rate": 2_000_000,
            "filesize": 20_000_000,
            "duration": expected_duration,
            "vcodec": codec,
            "acodec": "aac",
        }

    monkeypatch.setattr(engine, "_probe_douyin_candidate", probe)
    assert engine._add_douyin_probe_formats(
        object(),
        info,
        expected_id="1111111111111111111",
        verification_url="https://www.douyin.com/video/1111111111111111111",
        should_cancel=lambda: False,
    )
    assert any(
        value["format_id"].startswith("douyin-api-1440x2560")
        for value in info["formats"]
    )


def test_douyin_uri_extraction_rejects_crosswired_or_ambiguous_media() -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    verification_url = "https://www.douyin.com/user/expected-profile"
    info = _douyin_raw_info()

    with pytest.raises(MediaDownloadError, match="different video"):
        engine._douyin_video_uri(
            {**info, "id": "2222222222222222222"},
            "1111111111111111111",
            verification_url,
        )

    info["formats"].append(
        {
            "url": (
                "https://api-play.amemv.com/aweme/v1/play/"
                "?video_id=v0200fg10000differentvideoid&ratio=720p"
            )
        }
    )
    with pytest.raises(MediaDownloadError, match="multiple media identities"):
        engine._douyin_video_uri(info, "1111111111111111111", verification_url)


def test_douyin_uri_accepts_amemv_subdomains_but_rejects_lookalikes() -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    expected_id = "1111111111111111111"
    video_uri = "v0200fg10000fixturevideoid"
    base_info = {"id": expected_id}

    accepted = {
        **base_info,
        "formats": [
            {
                "url": (
                    "https://api-play-hl.amemv.com/aweme/v1/play/"
                    f"?video_id={video_uri}&ratio=720p"
                )
            }
        ],
    }
    rejected = {
        **base_info,
        "formats": [
            {
                "url": (
                    "https://evilamemv.com/aweme/v1/play/"
                    f"?video_id={video_uri}&ratio=720p"
                )
            }
        ],
    }

    assert (
        engine._douyin_video_uri(
            accepted, expected_id, "https://www.douyin.com/user/profile-a"
        )
        == video_uri
    )
    assert (
        engine._douyin_video_uri(
            rejected, expected_id, "https://www.douyin.com/user/profile-a"
        )
        is None
    )


@pytest.mark.parametrize("content_type", ["video/mp4", "application/octet-stream"])
def test_douyin_probe_caps_range_omits_cookie_and_validates_duration(
    monkeypatch, content_type: str
) -> None:
    payload = b"\x00\x00\x00\x18ftypisom" + b"x" * (400_000 - 12)

    class Headers:
        def get(self, name: str, default=None):
            values = {
                "Content-Type": content_type,
                "Content-Range": "bytes 0-262143/10425019",
            }
            return values.get(name, default)

    class Response:
        headers = Headers()
        url = "https://v26-web.douyinvod.com/verified-video.mp4"

        def __init__(self):
            self.offset = 0
            self.closed = False

        def read(self, size: int) -> bytes:
            chunk = payload[self.offset : self.offset + size]
            self.offset += len(chunk)
            return chunk

        def close(self) -> None:
            self.closed = True

    class ProbeYoutubeDL:
        def urlopen(self, request):
            self.request = request
            self.response = Response()
            return self.response

    ydl = ProbeYoutubeDL()
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    _inject_fake_douyin_media_opener(monkeypatch, engine)
    ffprobe_calls = []

    def ffprobe(data, *, local_path=None, should_cancel):
        ffprobe_calls.append((data, local_path))
        return {
            "width": 1080,
            "height": 1920,
            "vcodec": "h264",
            "acodec": "aac",
            "bit_rate": 1_088_835,
            "duration": 72.81,
        }

    monkeypatch.setattr(
        engine,
        "_ffprobe_douyin_media",
        ffprobe,
    )

    result = engine._probe_douyin_candidate(
        ydl,
        "https://api-play.amemv.com/aweme/v1/play/?video_id=fixture&ratio=4k",
        expected_duration=72.8,
        should_cancel=lambda: False,
    )

    request_headers = {key.lower(): value for key, value in ydl.request.headers.items()}
    assert result is not None
    assert result["width"] == 1080
    assert result["filesize"] == 10_425_019
    assert result["url"] == "https://v26-web.douyinvod.com/verified-video.mp4"
    assert result["probe_prefix_size"] == 256 * 1024
    assert result["probe_prefix_sha256"] == hashlib.sha256(
        payload[: 256 * 1024]
    ).hexdigest()
    assert ffprobe_calls == [(payload[: 256 * 1024], None)]
    assert "cookie" not in request_headers
    assert request_headers["range"] == "bytes=0-262143"
    assert ydl.request.extensions["timeout"] == 10.0
    assert ydl.response.offset == 256 * 1024
    assert ydl.response.closed is True

    monkeypatch.setattr(
        engine,
        "_ffprobe_douyin_media",
        lambda data, *, local_path=None, should_cancel: {
            "width": 1080,
            "height": 1920,
            "duration": 200,
            "bit_rate": 1_000_000,
        },
    )
    with pytest.raises(RuntimeError, match="duration did not match"):
        engine._probe_douyin_candidate(
            ProbeYoutubeDL(),
            "https://api-play.amemv.com/aweme/v1/play/" "?video_id=fixture&ratio=4k",
            expected_duration=72.8,
            should_cancel=lambda: False,
        )


def test_douyin_probe_rejects_unrecognized_final_redirect_before_ffprobe(
    monkeypatch,
) -> None:
    class Headers:
        def get(self, name: str, default=None):
            return "video/mp4" if name.lower() == "content-type" else default

    class Response:
        headers = Headers()
        url = "http://127.0.0.1/private.mp4"

        def __init__(self) -> None:
            self.closed = False
            self.read_calls = 0

        def read(self, size: int) -> bytes:
            self.read_calls += 1
            return b"\x00\x00\x00\x18ftypisom"

        def close(self) -> None:
            self.closed = True

    class ProbeYoutubeDL:
        def __init__(self) -> None:
            self.response = Response()

        def urlopen(self, request):
            return self.response

    ydl = ProbeYoutubeDL()
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    _inject_fake_douyin_media_opener(monkeypatch, engine)
    monkeypatch.setattr(
        engine,
        "_ffprobe_douyin_media",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Untrusted redirect must not reach FFprobe")
        ),
    )

    with pytest.raises(
        _DouyinRedirectRejected,
        match="unrecognized Douyin CDN host",
    ) as error:
        engine._probe_douyin_candidate(
            ydl,
            (
                "https://api-play.amemv.com/aweme/v1/play/"
                "?video_id=fixture&ratio=4k"
            ),
            expected_duration=72.8,
            should_cancel=lambda: False,
        )

    message = str(error.value)
    assert "host: unavailable" in message
    assert "reason: non-https-scheme" in message
    assert "127.0.0.1" not in message
    assert "private.mp4" not in message
    assert ydl.response.read_calls == 0
    assert ydl.response.closed is True


def test_douyin_regional_media_allowlist_covers_every_verified_domain_family() -> None:
    expected_domains = (
        "douyin.com",
        "douyinvod.com",
        "amemv.com",
        "zjcdn.com",
        "douyincdn.com",
        "idouyinvod.com",
        "pstatp.com",
    )
    source_url = (
        "https://api-play.amemv.com/aweme/v1/play/?video_id=verified-fixture"
    )
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    assert DOUYIN_REGIONAL_MEDIA_DOMAINS == expected_domains
    for domain in expected_domains:
        assert engine._is_verified_douyin_media_redirect(
            source_url,
            f"https://{domain}/original.mp4",
        )
        assert engine._is_verified_douyin_media_redirect(
            source_url,
            f"https://edge.video.{domain}/original.mp4",
        )


def test_douyin_regional_media_allowlist_rejects_lookalikes_and_invalid_urls() -> None:
    source_url = (
        "https://api-play.amemv.com/aweme/v1/play/?video_id=verified-fixture"
    )
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    assert engine._is_verified_douyin_media_redirect(
        source_url,
        "https://EDGE.VIDEO.PSTATP.COM.:443/original.mp4",
    )
    for domain in DOUYIN_REGIONAL_MEDIA_DOMAINS:
        assert not engine._is_verified_douyin_media_redirect(
            source_url,
            f"https://{domain}.evil.example/original.mp4",
        )
        assert not engine._is_verified_douyin_media_redirect(
            source_url,
            f"https://evil{domain}/original.mp4",
        )

    for value in (
        "http://v5.pstatp.com/original.mp4",
        "https://user:password@v5.pstatp.com/original.mp4",
        "https://v5.pstatp.com:8443/original.mp4",
        "https://v5.pstatp.com:invalid/original.mp4",
        "https://127.0.0.1/original.mp4",
        "https://[::1]/original.mp4",
        "https://localhost/original.mp4",
        "https://例子.pstatp.com/original.mp4",
        "https://edge.video.pstatp.com\\@evil.example/original.mp4",
        "https://edge.video.pstatp.com%0d.evil.example/original.mp4",
    ):
        assert not engine._is_verified_douyin_media_redirect(source_url, value)


@pytest.mark.parametrize(
    ("value", "expected_host", "expected_reason"),
    [
        (
            "http://media.vendor-cdn.net/original.mp4?token=secret-query",
            None,
            "non-https-scheme",
        ),
        (
            "https://user:secret-password@media.vendor-cdn.net/original.mp4",
            None,
            "embedded-credentials",
        ),
        (
            "https://media.vendor-cdn.net:8443/original.mp4",
            None,
            "nonstandard-port",
        ),
        (
            "https://media.vendor-cdn.net:invalid/original.mp4",
            None,
            "malformed-url",
        ),
        ("https:///missing-host", None, "missing-host"),
        ("http://127.0.0.1/private.mp4", None, "non-https-scheme"),
        ("https://127.0.0.1/private.mp4", None, "ip-literal"),
        ("https://010.000.000.001/private.mp4", None, "ip-literal"),
        ("https://0x7f.0.0.1/private.mp4", None, "ip-literal"),
        ("https://2130706433/private.mp4", None, "ip-literal"),
        ("https://[::1]/private.mp4", None, "ip-literal"),
        ("https://[pstatp.com]/private.mp4", None, "malformed-url"),
        ("https://[edge.video.pstatp.com]/private.mp4", None, "malformed-url"),
        ("https://localhost/private.mp4", None, "single-label-host"),
        (
            "https://media.example.com/private.mp4",
            None,
            "local-or-special-use-host",
        ),
        ("https://media.internal/private.mp4", None, "local-or-special-use-host"),
        ("https://media.example/private.mp4", None, "local-or-special-use-host"),
        ("https://media.onion/private.mp4", None, "local-or-special-use-host"),
        ("https://media.alt/private.mp4", None, "local-or-special-use-host"),
        ("https://例子.example/private.mp4", None, "non-ascii-host"),
        (
            "https://bad_host.example/private.mp4",
            None,
            "invalid-hostname",
        ),
        (
            "https://unrecognized-cdn.vendor-cdn.net/original.mp4?token=secret-query",
            "unrecognized-cdn.vendor-cdn.net",
            "unrecognized-host",
        ),
        (
            "https://media.vendor-cdn.net\\@evil.example/private.mp4",
            None,
            "malformed-url",
        ),
        ("https://media.vendor-cdn.net/private.mp4\nsecret", None, "malformed-url"),
        (" https://media.vendor-cdn.net/private.mp4", None, "malformed-url"),
        ("", None, "malformed-url"),
    ],
)
def test_douyin_redirect_diagnostic_is_specific_and_never_persists_url_secrets(
    value,
    expected_host,
    expected_reason,
) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    host, reason = engine._douyin_redirect_diagnostic(value)
    rejection = engine._douyin_redirect_rejection(value, reason)
    message = str(rejection)

    assert host == expected_host
    assert reason == expected_reason
    assert rejection.redirect_reason == expected_reason
    if expected_reason == "unrecognized-host":
        expected_fingerprint = hashlib.sha256(
            expected_host.encode("ascii")
        ).hexdigest()[:12]
        assert rejection.redirect_host is None
        assert rejection.redirect_host_fingerprint == expected_fingerprint
        assert "host: unavailable" in message
        assert f"host-fingerprint: {expected_fingerprint}" in message
        assert expected_host not in message
    else:
        assert rejection.redirect_host is None
        assert rejection.redirect_host_fingerprint is None
        assert "host: unavailable" in message
    assert f"reason: {expected_reason}" in message
    for secret in (
        "secret-query",
        "secret-password",
        "user:",
        "original.mp4",
        "private.mp4",
        "127.0.0.1",
        "0x7f.0.0.1",
        "2130706433",
        "::1",
    ):
        assert secret not in message


def test_douyin_unknown_redirect_redacts_token_subdomain_and_adds_fingerprint() -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    hostname = "secret-token.unrecognized.vendor-cdn.net"

    rejection = engine._douyin_redirect_rejection(
        f"https://{hostname}/original.mp4?token=secret-query",
        "unrecognized-host",
    )

    message = str(rejection)
    fingerprint = hashlib.sha256(hostname.encode("ascii")).hexdigest()[:12]
    assert rejection.redirect_host is None
    assert rejection.redirect_host_fingerprint == fingerprint
    assert "secret-token" not in message
    assert "unrecognized.vendor-cdn.net" not in message
    assert "secret-query" not in message
    assert "vendor-cdn.net" not in message
    assert fingerprint in message


def test_douyin_unknown_redirect_never_persists_token_in_two_label_host() -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    hostname = "secret-token.com"

    rejection = engine._douyin_redirect_rejection(
        f"https://{hostname}/original.mp4",
        "unrecognized-host",
    )

    message = str(rejection)
    assert rejection.redirect_host is None
    assert rejection.redirect_host_fingerprint == hashlib.sha256(
        hostname.encode("ascii")
    ).hexdigest()[:12]
    assert hostname not in message
    assert "secret-token" not in message


def test_douyin_known_unbound_redirect_only_persists_allowlist_family() -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    hostname = "secret-token.edge.pstatp.com"

    rejection = engine._douyin_redirect_rejection(
        f"https://{hostname}/original.mp4",
        "unverified-source-binding",
    )

    message = str(rejection)
    assert rejection.redirect_host == "pstatp.com"
    assert rejection.redirect_host_fingerprint is None
    assert "host: pstatp.com" in message
    assert hostname not in message
    assert "secret-token" not in message


def test_douyin_redirect_policy_distinguishes_known_unbound_and_unknown_hosts() -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    assert engine._douyin_media_redirect_rejection_reason(
        "https://v26-web.douyinvod.com/original.mp4",
        MediaType.VIDEO,
        allow_verified_regional=False,
    ) is None
    assert engine._douyin_media_redirect_rejection_reason(
        "https://edge.video.pstatp.com/original.mp4",
        MediaType.VIDEO,
        allow_verified_regional=False,
    ) == "unverified-source-binding"
    assert engine._douyin_media_redirect_rejection_reason(
        "https://edge.video.pstatp.com/original.mp4",
        MediaType.VIDEO,
        allow_verified_regional=True,
    ) is None
    assert engine._douyin_media_redirect_rejection_reason(
        "https://unrecognized-cdn.vendor-cdn.net/original.mp4",
        MediaType.VIDEO,
        allow_verified_regional=True,
    ) == "unrecognized-host"


def test_douyin_media_open_fails_closed_without_request_director() -> None:
    class LegacyYoutubeDL:
        def __init__(self) -> None:
            self.calls = 0

        def urlopen(self, request):
            self.calls += 1
            raise AssertionError("Fail-closed media open must not use urlopen fallback")

    ydl = LegacyYoutubeDL()
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    with pytest.raises(
        _DouyinProbeRejected,
        match="requires a yt-dlp request director",
    ):
        engine._open_douyin_media_response(
            ydl,
            Request("https://v26-web.douyinvod.com/original.mp4"),
            redirect_rejection_reason=lambda value: None,
        )

    assert ydl.calls == 0


def test_douyin_media_open_cancellation_prevents_validation_and_request() -> None:
    class LegacyYoutubeDL:
        def __init__(self) -> None:
            self.calls = 0

        def urlopen(self, request):
            self.calls += 1
            raise AssertionError("Cancelled media open must not perform a request")

    ydl = LegacyYoutubeDL()
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    with pytest.raises(DownloadCancelled, match="Task cancelled"):
        engine._open_douyin_media_response(
            ydl,
            Request("https://v26-web.douyinvod.com/original.mp4"),
            redirect_rejection_reason=lambda value: pytest.fail(
                "Cancellation must be checked before redirect validation"
            ),
            should_cancel=lambda: True,
        )

    assert ydl.calls == 0


def test_douyin_media_open_cancellation_after_request_closes_response(
    monkeypatch,
) -> None:
    class FinalResponse:
        is_redirect = False

        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class CancellingSession:
        def __init__(self) -> None:
            self.calls = 0
            self.response = FinalResponse()

        def request(self, **kwargs):
            self.calls += 1
            return self.response

    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    cancellation_checks = iter((False, False, True))
    session = CancellingSession()

    with YoutubeDL({"quiet": True, "proxy": ""}) as ydl:
        handler = ydl._request_director.handlers["Requests"]
        monkeypatch.setattr(handler, "_get_instance", lambda **kwargs: session)
        with pytest.raises(DownloadCancelled, match="Task cancelled"):
            engine._open_douyin_media_response(
                ydl,
                Request("https://v26-web.douyinvod.com/original.mp4"),
                redirect_rejection_reason=lambda value: None,
                should_cancel=lambda: next(cancellation_checks),
            )

    assert session.calls == 1
    assert session.response.closed is True


def test_douyin_redirect_limit_never_requests_sixth_target(monkeypatch) -> None:
    assert DOUYIN_MAX_MEDIA_REDIRECTS == 5
    urls = [
        f"https://v26-web.douyinvod.com/hop-{index}.mp4"
        for index in range(DOUYIN_MAX_MEDIA_REDIRECTS + 2)
    ]
    redirect_responses = [
        _FakeRequestsResponse(urls[index], location=urls[index + 1])
        for index in range(DOUYIN_MAX_MEDIA_REDIRECTS + 1)
    ]
    session = _QueuedRequestsSession(redirect_responses)
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    with YoutubeDL({"quiet": True, "proxy": ""}) as ydl:
        handler = ydl._request_director.handlers["Requests"]
        monkeypatch.setattr(handler, "_get_instance", lambda **kwargs: session)
        with pytest.raises(
            _DouyinRedirectRejected,
            match="reason: too-many-redirects",
        ):
            engine._open_douyin_media_response(
                ydl,
                Request(urls[0]),
                redirect_rejection_reason=lambda value: None,
            )

    requested_urls = [request["url"] for request in session.requests]
    assert requested_urls == urls[: DOUYIN_MAX_MEDIA_REDIRECTS + 1]
    assert urls[DOUYIN_MAX_MEDIA_REDIRECTS + 1] not in requested_urls
    assert len(redirect_responses) == 6
    assert all(response.closed for response in redirect_responses)


def test_douyin_relative_multi_hop_redirect_chain_succeeds(monkeypatch) -> None:
    source_url = "https://v26-web.douyinvod.com/media/start.mp4"
    first_hop_url = "https://v26-web.douyinvod.com/step/one.mp4"
    final_url = "https://v26-web.douyinvod.com/step/final.mp4"
    responses = [
        _FakeRequestsResponse(source_url, location="../step/one.mp4"),
        _FakeRequestsResponse(first_hop_url, location="final.mp4"),
        _FakeRequestsResponse(final_url, payload=b"verified-media"),
    ]
    session = _QueuedRequestsSession(responses)
    validated_urls = []
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    with YoutubeDL({"quiet": True, "proxy": ""}) as ydl:
        handler = ydl._request_director.handlers["Requests"]
        monkeypatch.setattr(handler, "_get_instance", lambda **kwargs: session)
        response = engine._open_douyin_media_response(
            ydl,
            Request(source_url),
            redirect_rejection_reason=lambda value: (
                validated_urls.append(value) or None
            ),
        )
        try:
            assert response.url == final_url
            assert response.read() == b"verified-media"
        finally:
            response.close()

    assert [request["url"] for request in session.requests] == [
        source_url,
        first_hop_url,
        final_url,
    ]
    assert validated_urls == [source_url, first_hop_url, final_url]
    assert responses[0].closed is True
    assert responses[1].closed is True
    assert responses[2].raw.closed is True


@pytest.mark.parametrize(
    "location",
    [
        "",
        "https://v26-web.douyinvod.com/bad path.mp4",
        "https://v26-web.douyinvod.com\\@evil.example/media.mp4",
        "https://v26-web.douyinvod.com/media.mp4\x1f",
    ],
)
def test_douyin_malformed_redirect_location_closes_current_response(
    monkeypatch,
    location,
) -> None:
    source_url = "https://v26-web.douyinvod.com/source.mp4"
    redirect_response = _FakeRequestsResponse(
        source_url,
        location="placeholder",
    )
    redirect_response.headers["Location"] = location
    if not location:
        redirect_response.is_redirect = False
    session = _QueuedRequestsSession([redirect_response])
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    with YoutubeDL({"quiet": True, "proxy": ""}) as ydl:
        handler = ydl._request_director.handlers["Requests"]
        monkeypatch.setattr(handler, "_get_instance", lambda **kwargs: session)
        with pytest.raises(
            _DouyinRedirectRejected,
            match="reason: malformed-url",
        ):
            engine._open_douyin_media_response(
                ydl,
                Request(source_url),
                redirect_rejection_reason=lambda value: None,
            )

    assert len(session.requests) == 1
    assert redirect_response.closed is True


def test_douyin_redirect_policy_exception_closes_current_response(
    monkeypatch,
) -> None:
    source_url = "https://v26-web.douyinvod.com/source.mp4"
    target_url = "https://v26-web.douyinvod.com/target.mp4"
    redirect_response = _FakeRequestsResponse(source_url, location=target_url)
    session = _QueuedRequestsSession([redirect_response])
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    def policy(value: str) -> None:
        if value == source_url:
            return None
        raise RuntimeError("policy callback failed")

    with YoutubeDL({"quiet": True, "proxy": ""}) as ydl:
        handler = ydl._request_director.handlers["Requests"]
        monkeypatch.setattr(handler, "_get_instance", lambda **kwargs: session)
        with pytest.raises(RuntimeError, match="policy callback failed"):
            engine._open_douyin_media_response(
                ydl,
                Request(source_url),
                redirect_rejection_reason=policy,
            )

    assert len(session.requests) == 1
    assert redirect_response.closed is True


def test_douyin_cross_origin_redirect_strips_sensitive_headers_and_keeps_range(
    monkeypatch,
) -> None:
    source_url = "https://v26-web.douyinvod.com/source.mp4"
    target_url = "https://v5-dy-ov-experiment.zjcdn.com/final.mp4"
    responses = [
        _FakeRequestsResponse(source_url, location=target_url),
        _FakeRequestsResponse(target_url, payload=b"verified-media"),
    ]
    session = _QueuedRequestsSession(responses)
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    with YoutubeDL({"quiet": True, "proxy": ""}) as ydl:
        handler = ydl._request_director.handlers["Requests"]
        monkeypatch.setattr(handler, "_get_instance", lambda **kwargs: session)
        response = engine._open_douyin_media_response(
            ydl,
            Request(
                source_url,
                headers={
                    "Authorization": "Bearer secret",
                    "Cookie": "session=secret",
                    "Proxy-Authorization": "Basic secret",
                    "Range": "bytes=0-262143",
                },
            ),
            redirect_rejection_reason=lambda value: None,
        )
        response.close()

    assert len(session.requests) == 2
    first_headers = {
        key.lower(): value for key, value in session.requests[0]["headers"].items()
    }
    second_headers = {
        key.lower(): value for key, value in session.requests[1]["headers"].items()
    }
    assert first_headers["authorization"] == "Bearer secret"
    assert first_headers["cookie"] == "session=secret"
    assert first_headers["proxy-authorization"] == "Basic secret"
    assert first_headers["range"] == "bytes=0-262143"
    assert {
        "authorization",
        "cookie",
        "proxy-authorization",
    }.isdisjoint(second_headers)
    assert second_headers["range"] == "bytes=0-262143"
    assert responses[0].closed is True
    assert responses[1].raw.closed is True


def test_douyin_safe_redirect_does_not_request_rejected_target() -> None:
    counts = {"source": 0, "target": 0}
    observed = {"authorization": None, "range": None}

    class TargetHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            counts["target"] += 1
            observed["authorization"] = self.headers.get("Authorization")
            observed["range"] = self.headers.get("Range")
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.end_headers()
            self.wfile.write(b"must-not-be-requested")

        def log_message(self, format, *args):
            return

    target_server = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
    target_url = (
        f"http://127.0.0.1:{target_server.server_port}/private.mp4"
        "?token=must-not-persist"
    )

    class SourceHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            counts["source"] += 1
            self.send_response(302)
            self.send_header("Location", target_url)
            self.end_headers()

        def log_message(self, format, *args):
            return

    source_server = ThreadingHTTPServer(("127.0.0.1", 0), SourceHandler)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (source_server, target_server)
    ]
    for thread in threads:
        thread.start()

    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    source_url = f"http://127.0.0.1:{source_server.server_port}/redirect"
    try:
        with YoutubeDL({"quiet": True, "proxy": ""}) as ydl:
            with pytest.raises(_DouyinRedirectRejected) as error:
                engine._open_douyin_media_response(
                    ydl,
                    Request(
                        source_url,
                        headers={
                            "Authorization": "Bearer must-not-forward",
                            "Range": "bytes=0-1023",
                        },
                    ),
                    redirect_rejection_reason=lambda value: (
                        None
                        if value == source_url
                        else engine._douyin_media_redirect_rejection_reason(
                            value,
                            MediaType.VIDEO,
                            allow_verified_regional=True,
                        )
                    ),
                )
            assert counts == {"source": 1, "target": 0}
            response = engine._open_douyin_media_response(
                ydl,
                Request(
                    source_url,
                    headers={
                        "Authorization": "Bearer must-not-forward",
                        "Range": "bytes=0-1023",
                    },
                ),
                redirect_rejection_reason=lambda value: None,
            )
            try:
                assert response.url == target_url
                assert response.read() == b"must-not-be-requested"
            finally:
                response.close()
    finally:
        for server in (source_server, target_server):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)

    message = str(error.value)
    assert counts == {"source": 2, "target": 1}
    assert observed["authorization"] is None
    assert observed["range"] == "bytes=0-1023"
    assert "reason: non-https-scheme" in message
    assert "127.0.0.1" not in message
    assert "private.mp4" not in message
    assert "must-not-persist" not in message


def test_douyin_redirect_policy_runs_without_buffering_redirect_body(
    monkeypatch,
) -> None:
    counts = {"source": 0, "target": 0}
    redirect_body = b"x" * (2 * 1024 * 1024)

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/redirect":
                counts["source"] += 1
                target_url = (
                    f"http://127.0.0.1:{self.server.server_port}/blocked"
                )
                self.send_response(302)
                self.send_header("Location", target_url)
                self.send_header("Content-Length", str(len(redirect_body)))
                self.end_headers()
                try:
                    self.wfile.write(redirect_body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            counts["target"] += 1
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    source_url = f"http://127.0.0.1:{server.server_port}/redirect"
    media_request = Request(source_url, extensions={"timeout": 5.0})
    captured_responses = []
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    try:
        with YoutubeDL({"quiet": True, "proxy": ""}) as ydl:
            handler = ydl._request_director.handlers["Requests"]
            session = handler._get_instance(
                cookiejar=handler._get_cookiejar(media_request),
                legacy_ssl_support=None,
            )
            original_request = session.request

            def observe_request(**kwargs):
                response = original_request(**kwargs)
                captured_responses.append(response)
                return response

            monkeypatch.setattr(session, "request", observe_request)
            monkeypatch.setattr(handler, "_get_instance", lambda **kwargs: session)
            with pytest.raises(
                _DouyinRedirectRejected,
                match="reason: non-https-scheme",
            ):
                engine._open_douyin_media_response(
                    ydl,
                    media_request,
                    redirect_rejection_reason=lambda value: (
                        None
                        if value == source_url
                        else engine._douyin_media_redirect_rejection_reason(
                            value,
                            MediaType.VIDEO,
                            allow_verified_regional=True,
                        )
                    ),
                )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert counts == {"source": 1, "target": 0}
    assert len(captured_responses) == 1
    redirect_response = captured_responses[0]
    assert redirect_response._content_consumed is False
    assert redirect_response._content is False
    assert redirect_response.raw.closed is True


def test_douyin_safe_redirect_maps_low_level_protocol_errors_for_retry(
    monkeypatch,
) -> None:
    class BrokenSession:
        def request(self, **kwargs):
            raise urllib3.exceptions.ProtocolError("temporary connection reset")

    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    with YoutubeDL({"quiet": True, "proxy": ""}) as ydl:
        handler = ydl._request_director.handlers["Requests"]
        monkeypatch.setattr(
            handler,
            "_get_instance",
            lambda **kwargs: BrokenSession(),
        )
        with pytest.raises(TransportError) as error:
            engine._open_douyin_media_response(
                ydl,
                Request("https://api-play.amemv.com/aweme/v1/play/"),
                redirect_rejection_reason=lambda value: None,
            )

    assert "temporary connection reset" in str(error.value)
    assert engine._is_retryable_douyin_probe_error(error.value) is True


def test_douyin_redirect_chain_budget_stops_before_next_request(monkeypatch) -> None:
    source_url = "https://v26-web.douyinvod.com/source.mp4"

    class RedirectResponse:
        is_redirect = True
        url = source_url
        headers = {"Location": "https://v26-web.douyinvod.com/next.mp4"}

        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class SlowRedirectSession:
        def __init__(self) -> None:
            self.calls = 0
            self.response = RedirectResponse()

        def request(self, **kwargs):
            self.calls += 1
            assert kwargs["timeout"] == 10.0
            return self.response

    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    clock = iter((100.0, 100.0, 111.0))
    monkeypatch.setattr("app.downloader.time.monotonic", lambda: next(clock))
    session = SlowRedirectSession()

    with YoutubeDL({"quiet": True, "proxy": ""}) as ydl:
        handler = ydl._request_director.handlers["Requests"]
        monkeypatch.setattr(handler, "_get_instance", lambda **kwargs: session)
        with pytest.raises(TransportError, match="redirect chain timed out"):
            engine._open_douyin_media_response(
                ydl,
                Request(source_url, extensions={"timeout": 10.0}),
                redirect_rejection_reason=lambda value: None,
            )

    assert session.calls == 1
    assert session.response.closed is True


def test_douyin_probe_accepts_regional_redirect_and_skips_full_complete_prefix(
    monkeypatch,
) -> None:
    payload = b"\x00\x00\x00\x18ftypisom" + b"verified-regional-cdn"
    final_url = "https://edge.video.pstatp.com/original.mp4"

    class Headers:
        def get(self, name: str, default=None):
            values = {
                "Content-Type": "video/mp4",
                "Content-Range": f"bytes 0-{len(payload) - 1}/{len(payload)}",
            }
            return values.get(name, default)

    class Response:
        headers = Headers()
        url = final_url
        status = 206

        def __init__(self) -> None:
            self.offset = 0
            self.closed = False

        def read(self, size: int) -> bytes:
            chunk = payload[self.offset : self.offset + size]
            self.offset += len(chunk)
            return chunk

        def close(self) -> None:
            self.closed = True

    class ProbeYoutubeDL:
        def __init__(self) -> None:
            self.response = Response()
            self.calls = 0

        def urlopen(self, request):
            self.calls += 1
            if self.calls > 1:
                raise AssertionError("A complete prefix must not trigger a full download")
            self.request = request
            return self.response

    ydl = ProbeYoutubeDL()
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    _inject_fake_douyin_media_opener(monkeypatch, engine)
    ffprobe_calls = []

    def ffprobe(data, *, local_path=None, should_cancel):
        ffprobe_calls.append((data, local_path))
        return {
            "width": 1080,
            "height": 1920,
            "vcodec": "h264",
            "acodec": "aac",
            "bit_rate": 2_000_000,
            "duration": 2.0,
        }

    monkeypatch.setattr(engine, "_ffprobe_douyin_media", ffprobe)

    result = engine._probe_douyin_candidate(
        ydl,
        "https://api-play.amemv.com/aweme/v1/play/?video_id=fixture",
        expected_duration=2.0,
        should_cancel=lambda: False,
    )

    assert result is not None
    assert result["url"] == final_url
    assert result["source_url"] == (
        "https://api-play.amemv.com/aweme/v1/play/?video_id=fixture"
    )
    assert result["filesize"] == len(payload)
    assert result["probe_prefix_sha256"] == hashlib.sha256(payload).hexdigest()
    assert ffprobe_calls == [(payload, None)]
    assert ydl.calls == 1
    assert ydl.response.offset == len(payload)
    assert ydl.response.closed is True


def test_douyin_incomplete_prefix_downloads_identical_full_file_and_cleans_temp(
    monkeypatch,
) -> None:
    prefix = b"\x00\x00\x00\x18ftypisom-prefix"
    full_payload = prefix + b"-middle-moov-tail"
    candidate_url = (
        "https://api-play.amemv.com/aweme/v1/play/?video_id=full-probe-fixture"
    )
    final_url = "https://edge.video.pstatp.com/original.mp4"

    class Response:
        def __init__(self, payload: bytes, headers: dict[str, str]) -> None:
            self.url = final_url
            self.headers = headers
            self.status = 206 if "Content-Range" in headers else 200
            self.payload = payload
            self.offset = 0
            self.closed = False

        def read(self, size: int) -> bytes:
            chunk_size = min(size, 3)
            chunk = self.payload[self.offset : self.offset + chunk_size]
            self.offset += len(chunk)
            return chunk

        def close(self) -> None:
            self.closed = True

    class ProbeYoutubeDL:
        def __init__(self) -> None:
            self.requests = []
            self.responses = []

        def urlopen(self, request):
            self.requests.append(request)
            request_headers = {
                key.lower(): value for key, value in request.headers.items()
            }
            if "range" in request_headers:
                response = Response(
                    prefix,
                    {
                        "Content-Type": "video/mp4",
                        "Content-Range": (
                            f"bytes 0-{len(prefix) - 1}/{len(full_payload)}"
                        ),
                    },
                )
            else:
                response = Response(
                    full_payload,
                    {
                        "Content-Type": "video/mp4",
                        "Content-Length": str(len(full_payload)),
                    },
                )
            self.responses.append(response)
            return response

    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    _inject_fake_douyin_media_opener(monkeypatch, engine)
    ffprobe_calls = []
    temporary_paths = []

    def ffprobe(data, *, local_path=None, should_cancel):
        snapshot = local_path.read_bytes() if local_path is not None else None
        ffprobe_calls.append((data, local_path, snapshot))
        if local_path is None:
            return {
                "width": 0,
                "height": 0,
                "duration": None,
                "bit_rate": 0,
            }
        temporary_paths.append(local_path)
        return {
            "width": 1440,
            "height": 2560,
            "vcodec": "hevc",
            "acodec": "aac",
            "bit_rate": 20_000_000,
            "duration": 2.0,
        }

    monkeypatch.setattr(engine, "_ffprobe_douyin_media", ffprobe)
    ydl = ProbeYoutubeDL()
    result = engine._probe_douyin_candidate(
        ydl,
        candidate_url,
        expected_duration=2.0,
        should_cancel=lambda: False,
    )

    assert result is not None
    assert result["url"] == final_url
    assert result["filesize"] == len(full_payload)
    assert result["probe_prefix_size"] == len(prefix)
    assert result["probe_prefix_sha256"] == hashlib.sha256(prefix).hexdigest()
    assert len(ydl.requests) == 2
    assert [response.offset for response in ydl.responses] == [
        len(prefix),
        len(full_payload),
    ]
    assert all(response.closed for response in ydl.responses)
    assert ffprobe_calls[0] == (prefix, None, None)
    assert ffprobe_calls[1][0] == prefix
    assert ffprobe_calls[1][2] == full_payload
    assert len(temporary_paths) == 1
    assert not temporary_paths[0].exists()
    assert not temporary_paths[0].parent.exists()


@pytest.mark.parametrize(
    ("failure_mode", "exception_type", "message"),
    [
        (
            "prefix_changed",
            _DouyinProbeIntegrityChanged,
            "media content changed",
        ),
        ("size_changed", _DouyinProbeIntegrityChanged, "media size changed"),
        ("unknown_host", _DouyinRedirectRejected, "unrecognized Douyin CDN host"),
        ("oversized", _DouyinProbeRejected, "safe probe size limit"),
        ("cancelled", DownloadCancelled, "Task cancelled"),
    ],
)
def test_douyin_full_probe_rejects_changed_or_unsafe_media_and_cleans_temp(
    monkeypatch,
    failure_mode,
    exception_type,
    message,
) -> None:
    prefix = b"\x00\x00\x00\x18ftypisom-PREFIX"
    changed_prefix = b"\x00\x00\x00\x18ftypisom-BROKEN"
    full_payload = prefix + b"-complete-tail"
    candidate_url = (
        "https://api-play.amemv.com/aweme/v1/play/?video_id=failure-fixture"
    )
    trusted_final_url = "https://edge.video.pstatp.com/original.mp4"
    cancellation = {"active": False}

    class Response:
        def __init__(
            self,
            payload: bytes,
            headers: dict[str, str],
            final_url: str,
        ) -> None:
            self.url = final_url
            self.headers = headers
            self.status = 206 if "Content-Range" in headers else 200
            self.payload = payload
            self.offset = 0
            self.closed = False

        def read(self, size: int) -> bytes:
            chunk = self.payload[self.offset : self.offset + min(size, 5)]
            self.offset += len(chunk)
            return chunk

        def close(self) -> None:
            self.closed = True

    class ProbeYoutubeDL:
        def __init__(self) -> None:
            self.responses = []

        def urlopen(self, request):
            if not self.responses:
                expected_size = (
                    DOUYIN_MAX_PROBE_FILE_BYTES + 1
                    if failure_mode == "oversized"
                    else len(full_payload)
                )
                response = Response(
                    prefix,
                    {
                        "Content-Type": "video/mp4",
                        "Content-Range": (
                            f"bytes 0-{len(prefix) - 1}/{expected_size}"
                        ),
                    },
                    trusted_final_url,
                )
            else:
                response_payload = (
                    changed_prefix + full_payload[len(prefix) :]
                    if failure_mode == "prefix_changed"
                    else full_payload
                )
                declared_size = (
                    DOUYIN_MAX_PROBE_FILE_BYTES + 1
                    if failure_mode == "oversized"
                    else len(full_payload)
                    + (1 if failure_mode == "size_changed" else 0)
                )
                response = Response(
                    response_payload,
                    {
                        "Content-Type": "video/mp4",
                        "Content-Length": str(declared_size),
                    },
                    (
                        "https://unrecognized-cdn.vendor-cdn.net/original.mp4"
                        if failure_mode == "unknown_host"
                        else trusted_final_url
                    ),
                )
                if failure_mode == "cancelled":
                    cancellation["active"] = True
            self.responses.append(response)
            return response

    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    _inject_fake_douyin_media_opener(monkeypatch, engine)
    monkeypatch.setattr(
        engine,
        "_ffprobe_douyin_media",
        lambda data, *, local_path=None, should_cancel: None,
    )
    original_download = engine._download_douyin_probe_file
    temporary_paths = []

    def track_download(ydl, candidate, path, **kwargs):
        temporary_paths.append(path)
        return original_download(ydl, candidate, path, **kwargs)

    monkeypatch.setattr(engine, "_download_douyin_probe_file", track_download)
    ydl = ProbeYoutubeDL()
    with pytest.raises(exception_type, match=message):
        engine._probe_douyin_candidate(
            ydl,
            candidate_url,
            expected_duration=2.0,
            should_cancel=lambda: cancellation["active"],
        )

    expected_response_count = 1 if failure_mode == "oversized" else 2
    assert len(ydl.responses) == expected_response_count
    assert all(response.closed for response in ydl.responses)
    assert len(temporary_paths) == 1
    assert not temporary_paths[0].exists()
    assert not temporary_paths[0].parent.exists()
    if failure_mode in {"prefix_changed", "size_changed"}:
        assert engine._should_pause_douyin_probe_error(
            exception_type(message)
        )
    if failure_mode in {"unknown_host", "size_changed", "cancelled"}:
        assert ydl.responses[1].offset == 0


@pytest.mark.parametrize(
    "initial_url",
    [
        "https://edge.video.pstatp.com/original.mp4",
        "https://regional-video.cdn.example/original.mp4",
        "http://v26-web.douyinvod.com/original.mp4",
        "https://user:password@v26-web.douyinvod.com/original.mp4",
        "https://v26-web.douyinvod.com:8443/original.mp4",
        "https://127.0.0.1/original.mp4",
        "https://[::1]/original.mp4",
    ],
)
def test_douyin_probe_rejects_non_allowlisted_initial_url_before_network(
    initial_url,
) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    class ProbeYoutubeDL:
        def __init__(self) -> None:
            self.calls = 0

        def urlopen(self, request):
            self.calls += 1
            raise AssertionError("A non-allowlisted initial URL must not be opened")

    ydl = ProbeYoutubeDL()
    with pytest.raises(RuntimeError, match="untrusted initial Douyin"):
        engine._probe_douyin_candidate(
            ydl,
            initial_url,
            expected_duration=2.0,
            should_cancel=lambda: False,
        )

    assert ydl.calls == 0


def test_douyin_redirect_rejection_skips_internal_retries_and_pauses_upstream(
    monkeypatch,
) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    attempts = []
    delays = []

    def reject(ydl, url, *, expected_duration, should_cancel):
        attempts.append(url)
        raise _DouyinRedirectRejected(
            "media endpoint redirected to an unrecognized Douyin CDN host"
        )

    monkeypatch.setattr(engine, "_probe_douyin_candidate", reject)
    monkeypatch.setattr(
        engine,
        "_wait_for_douyin_probe_retry",
        lambda delay, should_cancel: delays.append(delay),
    )
    asset = RemoteAsset(
        candidates=["https://v26-web.douyinvod.com/direct.mp4"],
        index=1,
        width=1080,
        height=1920,
        video_uri="v0200fg10000redirectfixture",
        duration=2.0,
        quality_candidates=[
            {
                "width": 1080,
                "height": 1920,
                "bit_rate": 2_000_000,
                "urls": ["https://v26-web.douyinvod.com/direct.mp4"],
            }
        ],
    )

    assert not engine._is_retryable_douyin_probe_error(
        _DouyinRedirectRejected("rejected")
    )
    assert engine._should_pause_douyin_probe_error(
        _DouyinRedirectRejected("rejected")
    )
    with pytest.raises(TemporaryAccessError, match="task was paused"):
        engine._select_highest_douyin_live_photo_asset(
            object(),
            asset,
            callback=None,
            should_cancel=lambda: False,
        )

    assert len(attempts) == 2
    assert attempts[0] == "https://v26-web.douyinvod.com/direct.mp4"
    assert "api-play-hl.amemv.com" in attempts[1]
    assert delays == []


def test_douyin_live_photo_tries_later_mirror_after_unrecognized_redirect(
    monkeypatch,
) -> None:
    payload = b"\x00\x00\x00\x18ftypisom" + b"verified-live-photo"
    first_mirror = "https://v26-web.douyinvod.com/mirror-1.mp4"
    second_mirror = "https://v26-web.douyinvod.com/mirror-2.mp4"
    regional_final = "https://edge.video.pstatp.com/original.mp4"

    class Headers:
        def get(self, name: str, default=None):
            values = {
                "Content-Type": "video/mp4",
                "Content-Range": f"bytes 0-{len(payload) - 1}/{len(payload)}",
            }
            return values.get(name, default)

    class Response:
        headers = Headers()
        status = 206

        def __init__(self, url: str) -> None:
            self.url = url
            self.offset = 0
            self.closed = False

        def read(self, size: int) -> bytes:
            chunk = payload[self.offset : self.offset + size]
            self.offset += len(chunk)
            return chunk

        def close(self) -> None:
            self.closed = True

    class ProbeYoutubeDL:
        def __init__(self) -> None:
            self.requests = []
            self.responses = []

        def urlopen(self, request):
            self.requests.append(request.url)
            if request.url == first_mirror:
                final_url = "https://unrecognized-cdn.vendor-cdn.net/original.mp4"
            elif request.url == second_mirror:
                final_url = regional_final
            else:
                final_url = "https://v5-dy-ov-experiment.zjcdn.com/default.mp4"
            response = Response(final_url)
            self.responses.append(response)
            return response

    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    _inject_fake_douyin_media_opener(monkeypatch, engine)

    ffprobe_count = 0

    def ffprobe(data, *, local_path=None, should_cancel):
        nonlocal ffprobe_count
        ffprobe_count += 1
        assert data == payload
        assert local_path is None
        if ffprobe_count == 1:
            return {
                "width": 1080,
                "height": 1920,
                "vcodec": "h264",
                "acodec": "aac",
                "bit_rate": 2_000_000,
                "duration": 2.0,
            }
        return {
            "width": 720,
            "height": 1280,
            "vcodec": "h264",
            "acodec": "aac",
            "bit_rate": 1_000_000,
            "duration": 2.0,
        }

    monkeypatch.setattr(engine, "_ffprobe_douyin_media", ffprobe)
    ydl = ProbeYoutubeDL()
    selected = engine._select_highest_douyin_live_photo_asset(
        ydl,
        RemoteAsset(
            candidates=[first_mirror, second_mirror],
            index=1,
            width=1080,
            height=1920,
            video_uri="v0200fg10000redirectfixture",
            duration=2.0,
            quality_candidates=[
                {
                    "width": 1080,
                    "height": 1920,
                    "bit_rate": 2_000_000,
                    "urls": [first_mirror, second_mirror],
                }
            ],
        ),
        callback=None,
        should_cancel=lambda: False,
    )

    assert ydl.requests[:2] == [first_mirror, second_mirror]
    assert "api-play-hl.amemv.com" in ydl.requests[2]
    assert ydl.responses[0].offset == 0
    assert ydl.responses[0].closed is True
    assert all(response.closed for response in ydl.responses)
    assert ffprobe_count == 2
    assert selected.candidates[:2] == [second_mirror, regional_final]
    assert selected.redirect_source_url == second_mirror
    assert (selected.width, selected.height) == (1080, 1920)


def test_douyin_probe_rejects_candidate_without_size_or_bitrate(
    monkeypatch,
) -> None:
    payload = b"\x00\x00\x00\x18ftypisom"

    class Headers:
        def get(self, name: str, default=None):
            return "video/mp4" if name.lower() == "content-type" else default

    class Response:
        headers = Headers()
        url = "https://v26-web.douyinvod.com/verified.mp4"
        status = 206

        def __init__(self) -> None:
            self.finished = False

        def read(self, size: int) -> bytes:
            if self.finished:
                return b""
            self.finished = True
            return payload

        def close(self) -> None:
            return None

    class ProbeYoutubeDL:
        def urlopen(self, request):
            return Response()

    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    _inject_fake_douyin_media_opener(monkeypatch, engine)
    monkeypatch.setattr(
        engine,
        "_ffprobe_douyin_media",
        lambda *args, **kwargs: {
            "width": 1440,
            "height": 2560,
            "duration": 10.0,
            "bit_rate": 0,
            "vcodec": "hevc",
            "acodec": "aac",
        },
    )

    with pytest.raises(RuntimeError, match="no bitrate or complete media size"):
        engine._probe_douyin_candidate(
            ProbeYoutubeDL(),
            "https://api-play.amemv.com/aweme/v1/play/?video_id=fixture",
            expected_duration=10.0,
            should_cancel=lambda: False,
        )


def test_douyin_probe_derives_bitrate_from_complete_content_length(
    monkeypatch,
) -> None:
    payload = b"\x00\x00\x00\x18ftypisom"
    complete_size = 10_000_000

    class Headers:
        def get(self, name: str, default=None):
            values = {
                "Content-Type": "video/mp4",
                "Content-Length": str(complete_size),
            }
            return values.get(name, default)

    class Response:
        headers = Headers()
        url = "https://v26-web.douyinvod.com/verified.mp4"
        status = 200

        def __init__(self) -> None:
            self.finished = False

        def read(self, size: int) -> bytes:
            if self.finished:
                return b""
            self.finished = True
            return payload

        def close(self) -> None:
            return None

    class ProbeYoutubeDL:
        def urlopen(self, request):
            return Response()

    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    _inject_fake_douyin_media_opener(monkeypatch, engine)
    monkeypatch.setattr(
        engine,
        "_ffprobe_douyin_media",
        lambda *args, **kwargs: {
            "width": 1440,
            "height": 2560,
            "duration": 10.0,
            "bit_rate": 0,
            "vcodec": "hevc",
            "acodec": "aac",
        },
    )
    full_probe_calls = []
    monkeypatch.setattr(
        engine,
        "_download_douyin_probe_file",
        lambda *args, **kwargs: full_probe_calls.append((args, kwargs)),
    )

    result = engine._probe_douyin_candidate(
        ProbeYoutubeDL(),
        "https://api-play.amemv.com/aweme/v1/play/?video_id=fixture",
        expected_duration=10.0,
        should_cancel=lambda: False,
    )

    assert result is not None
    assert result["filesize"] == complete_size
    assert result["bit_rate"] == 8_000_000
    assert full_probe_calls == []


def test_douyin_probe_requires_duration_even_without_item_duration(
    monkeypatch,
) -> None:
    payload = b"\x00\x00\x00\x18ftypisom"

    class Headers:
        def get(self, name: str, default=None):
            values = {
                "Content-Type": "video/mp4",
                "Content-Range": "bytes 0-11/10000000",
            }
            return values.get(name, default)

    class Response:
        headers = Headers()
        url = "https://v26-web.douyinvod.com/verified.mp4"
        status = 206

        def __init__(self) -> None:
            self.finished = False

        def read(self, size: int) -> bytes:
            if self.finished:
                return b""
            self.finished = True
            return payload

        def close(self) -> None:
            return None

    class ProbeYoutubeDL:
        def urlopen(self, request):
            return Response()

    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    _inject_fake_douyin_media_opener(monkeypatch, engine)
    monkeypatch.setattr(
        engine,
        "_ffprobe_douyin_media",
        lambda *args, **kwargs: {
            "width": 1440,
            "height": 2560,
            "duration": None,
            "bit_rate": 10_000_000,
            "vcodec": "hevc",
            "acodec": "aac",
        },
    )
    monkeypatch.setattr(
        engine,
        "_download_douyin_probe_file",
        lambda *args, **kwargs: None,
    )

    with pytest.raises(RuntimeError, match="no media duration"):
        engine._probe_douyin_candidate(
            ProbeYoutubeDL(),
            "https://api-play.amemv.com/aweme/v1/play/?video_id=fixture",
            expected_duration=None,
            should_cancel=lambda: False,
        )


def test_douyin_video_asset_allowlist_accepts_official_zjcdn_redirects_only() -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    assert engine._is_trusted_douyin_asset_url(
        "https://v5-dy-ov-experiment.zjcdn.com/video.mp4",
        MediaType.VIDEO,
    )
    assert not engine._is_trusted_douyin_asset_url(
        "https://zjcdn.com.evil.example/video.mp4",
        MediaType.VIDEO,
    )
    assert not engine._is_trusted_douyin_asset_url(
        "http://v5-dy-ov-experiment.zjcdn.com/video.mp4",
        MediaType.VIDEO,
    )
    assert not engine._is_trusted_douyin_asset_url(
        "https://v5-dy-ov-experiment.zjcdn.com:8443/video.mp4",
        MediaType.VIDEO,
    )
    assert not engine._is_trusted_douyin_asset_url(
        "https://v5-dy-ov-experiment.zjcdn.com/video.mp4",
        MediaType.IMAGE,
    )


def test_douyin_cached_direct_candidates_reuse_strict_asset_allowlist() -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    video_uri = "v0200fg10000verifiedfixture"
    result = engine._douyin_direct_candidates_from_cache(
        {
            "video_uri": video_uri,
            "direct_candidates": [
                {
                    "width": 1440,
                    "height": 2560,
                    "video_uri": video_uri,
                    "urls": [
                        (
                            "https://user:pass@"
                            "v5-dy-ov-experiment.zjcdn.com:8443/private.mp4"
                        ),
                        "https://zjcdn.com.evil.example/private.mp4",
                        "https://v5-dy-ov-experiment.zjcdn.com/original.mp4",
                    ],
                }
            ],
        }
    )

    assert result == [
        {
            "width": 1440,
            "height": 2560,
            "urls": [
                "https://v5-dy-ov-experiment.zjcdn.com/original.mp4"
            ],
        }
    ]


def test_douyin_probe_failures_are_safe_and_actionable(monkeypatch) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    info = _douyin_raw_info()

    def fail(ydl, url, *, expected_duration, should_cancel):
        raise RuntimeError(
            "connection reset for https://cdn.example/video?token=must-not-leak"
        )

    monkeypatch.setattr(engine, "_probe_douyin_candidate", fail)

    assert (
        engine._add_douyin_probe_formats(
            object(),
            info,
            expected_id="1111111111111111111",
            verification_url="https://www.douyin.com/user/profile-a",
            should_cancel=lambda: False,
        )
        is False
    )
    assert info["_douyin_probe_failure"] == (
        "default,author-feed-1: media endpoint network request failed"
    )
    assert "must-not-leak" not in info["_douyin_probe_failure"]


def test_douyin_official_probe_retries_transient_timeout(monkeypatch) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    info = _douyin_raw_info()
    attempts: dict[str, int] = {}
    delays = []
    messages = []

    def probe(ydl, url, *, expected_duration, should_cancel):
        ratio = (
            "author-feed"
            if "fixture-direct.mp4" in url
            else parse_qs(urlsplit(url).query)["ratio"][0]
        )
        attempts[ratio] = attempts.get(ratio, 0) + 1
        if ratio == "default" and attempts[ratio] < 3:
            raise TimeoutError("temporary timeout")
        width, height = (1080, 1920)
        return {
            "url": url,
            "width": width,
            "height": height,
            "bit_rate": 1_000_000,
            "filesize": 9_000_000,
            "duration": expected_duration,
            "vcodec": "h264",
            "acodec": "aac",
        }

    monkeypatch.setattr(engine, "_probe_douyin_candidate", probe)
    monkeypatch.setattr(
        engine,
        "_wait_for_douyin_probe_retry",
        lambda delay, should_cancel: delays.append(delay),
    )

    assert engine._add_douyin_probe_formats(
        object(),
        info,
        expected_id="1111111111111111111",
        verification_url="https://www.douyin.com/video/1111111111111111111",
        callback=lambda event: messages.append(event.message),
        should_cancel=lambda: False,
    )
    assert attempts == {"author-feed": 1, "default": 3}
    assert delays == [1.0, 2.0]
    assert sum(
        message.startswith("Retrying Douyin quality default")
        for message in messages
    ) == 2


@pytest.mark.parametrize("status", [401, 403, 404, 410, 429, 503])
def test_douyin_probe_closes_retryable_http_error(monkeypatch, status) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    response = Response(
        BytesIO(),
        url="https://api-play.amemv.com/aweme/v1/play/",
        headers={},
        status=status,
        reason="Temporary media failure",
    )
    http_error = HTTPError(response)
    attempts = 0

    def probe(ydl, url, *, expected_duration, should_cancel):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise http_error
        return {
            "url": url,
            "width": 1080,
            "height": 1920,
            "duration": expected_duration,
            "vcodec": "h264",
            "acodec": "aac",
        }

    monkeypatch.setattr(engine, "_probe_douyin_candidate", probe)
    monkeypatch.setattr(
        engine,
        "_wait_for_douyin_probe_retry",
        lambda delay, should_cancel: None,
    )

    result = engine._probe_douyin_ratio_with_retry(
        object(),
        "https://api-play.amemv.com/aweme/v1/play/",
        ratio="4k",
        expected_duration=23.0,
        callback=None,
        should_cancel=lambda: False,
    )

    assert result and result["width"] == 1080
    assert attempts == 2
    assert response.closed is True


def test_douyin_live_photo_selects_verified_default_over_lower_direct(
    monkeypatch,
) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    asset = RemoteAsset(
        candidates=["https://v26-web.douyinvod.com/direct-low.mp4"],
        index=1,
        width=720,
        height=1280,
        video_uri="v0200fg10000verifiedlivephoto",
        duration=1.834,
        quality_candidates=[
            {
                "width": 720,
                "height": 1280,
                "bit_rate": 1_500_000,
                "urls": ["https://v26-web.douyinvod.com/direct-low.mp4"],
            }
        ],
    )
    ratios: list[str] = []

    monkeypatch.setattr(
        engine,
        "_probe_douyin_candidate",
        lambda *args, **kwargs: {
            "url": "https://v26-web.douyinvod.com/direct-low-final.mp4",
            "width": 720,
            "height": 1280,
            "bit_rate": 1_500_000,
            "filesize": 378_678,
            "duration": 1.834,
            "vcodec": "h264",
            "acodec": "none",
        },
    )

    def probe_ratio(ydl, url, *, ratio, **kwargs):
        ratios.append(ratio)
        width, height, bit_rate, filesize = (
            (1080, 1920, 12_990_000, 2_982_056)
            if ratio == "default"
            else (720, 1280, 1_500_000, 378_678)
        )
        return {
            "url": f"https://v26-web.douyinvod.com/{ratio}-final.mp4",
            "width": width,
            "height": height,
            "bit_rate": bit_rate,
            "filesize": filesize,
            "duration": 1.834,
            "vcodec": "hevc" if ratio == "default" else "h264",
            "acodec": "none",
            "probe_prefix_size": 32,
            "probe_prefix_sha256": ("a" if ratio == "default" else "b") * 64,
        }

    monkeypatch.setattr(engine, "_probe_douyin_ratio_with_retry", probe_ratio)

    selected = engine._select_highest_douyin_live_photo_asset(
        object(),
        asset,
        callback=None,
        should_cancel=lambda: False,
    )

    assert ratios == ["author-feed-1", "default"]
    assert (selected.width, selected.height) == (1080, 1920)
    assert selected.size == 2_982_056
    assert selected.bit_rate == 12_990_000
    assert selected.duration == 1.834
    assert selected.video_codec == "hevc"
    assert selected.audio_codec == "none"
    assert selected.probe_prefix_size == 32
    assert selected.probe_prefix_sha256 == "a" * 64
    assert selected.candidates[0].endswith("/default-final.mp4")
    assert parse_qs(urlsplit(selected.candidates[1]).query)["ratio"] == [
        "default"
    ]
    assert selected.format_id == (
        "douyin-highest-live-photo-default-1080x1920"
    )


def test_douyin_live_photo_higher_default_dominates_expired_lower_direct(
    monkeypatch,
) -> None:
    direct_url = "https://v26-web.douyinvod.com/expired-live-720.mp4"
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    asset = RemoteAsset(
        candidates=[direct_url],
        index=1,
        width=720,
        height=1280,
        video_uri="v0200fg10000verifiedlivephoto",
        duration=2.0,
        quality_candidates=[
            {
                "width": 720,
                "height": 1280,
                "bit_rate": 700_000,
                "urls": [direct_url],
            }
        ],
    )

    def probe_ratio(ydl, url, *, ratio, **kwargs):
        if ratio == "author-feed-1":
            raise TimeoutError("expired lower direct URL")
        return {
            "url": "https://v26-web.douyinvod.com/default-1080.mp4",
            "width": 1080,
            "height": 1920,
            "bit_rate": 2_000_000,
            "filesize": 2_000_000,
            "duration": 2.0,
            "vcodec": "hevc",
            "acodec": "none",
            "probe_prefix_size": 32,
            "probe_prefix_sha256": "a" * 64,
        }

    monkeypatch.setattr(engine, "_probe_douyin_ratio_with_retry", probe_ratio)

    selected = engine._select_highest_douyin_live_photo_asset(
        object(),
        asset,
        callback=None,
        should_cancel=lambda: False,
    )

    assert (selected.width, selected.height) == (1080, 1920)
    assert selected.format_id.endswith("default-1080x1920")


def test_douyin_cached_live_photo_preserves_distinct_direct_renditions() -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    video_uri = "v0200fg10000verifiedlivephoto"
    high_url = "https://v26-web.douyinvod.com/direct-high.mp4"
    compatible_url = "https://v11-web.douyinvod.com/direct-compatible.mp4"

    assets = engine._douyin_cached_assets(
        [
            {
                "index": 1,
                "width": 1080,
                "height": 1920,
                "candidates": [high_url, compatible_url],
                "video_uri": video_uri,
                "direct_candidates": [
                    {
                        "width": 1080,
                        "height": 1920,
                        "bit_rate": 5_000_000,
                        "codec_hint": "hevc",
                        "urls": [high_url],
                        "video_uri": video_uri,
                    },
                    {
                        "width": 1080,
                        "height": 1920,
                        "bit_rate": 2_000_000,
                        "codec_hint": "h264",
                        "urls": [compatible_url],
                        "video_uri": video_uri,
                    },
                ],
            }
        ],
        format_prefix="douyin-highest-live-photo",
    )

    assert len(assets) == 1
    assert assets[0].quality_candidates == [
        {
            "width": 1080,
            "height": 1920,
            "bit_rate": 5_000_000,
            "codec_hint": "hevc",
            "urls": [high_url],
        },
        {
            "width": 1080,
            "height": 1920,
            "bit_rate": 2_000_000,
            "codec_hint": "h264",
            "urls": [compatible_url],
        },
    ]


def test_douyin_live_photo_ignores_unneeded_derivative_ratio_failure(
    monkeypatch,
) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    asset = RemoteAsset(
        candidates=["https://v26-web.douyinvod.com/direct.mp4"],
        index=1,
        width=720,
        height=1280,
        video_uri="v0200fg10000verifiedlivephoto",
        duration=2.0,
        quality_candidates=[
            {
                "width": 720,
                "height": 1280,
                "urls": ["https://v26-web.douyinvod.com/direct.mp4"],
            }
        ],
    )
    monkeypatch.setattr(
        engine,
        "_probe_douyin_candidate",
        lambda *args, **kwargs: {
            "url": asset.candidates[0],
            "width": 720,
            "height": 1280,
            "duration": 2.0,
            "vcodec": "h264",
            "acodec": "none",
        },
    )

    ratios: list[str] = []

    def probe_ratio(ydl, url, *, ratio, **kwargs):
        ratios.append(ratio)
        if ratio == "2k":
            raise TimeoutError("temporary endpoint timeout")
        return {
            "url": url,
            "width": 1080,
            "height": 1920,
            "duration": 2.0,
            "vcodec": "h264",
            "acodec": "none",
        }

    monkeypatch.setattr(engine, "_probe_douyin_ratio_with_retry", probe_ratio)

    selected = engine._select_highest_douyin_live_photo_asset(
        object(),
        asset,
        callback=None,
        should_cancel=lambda: False,
    )

    assert ratios == ["author-feed-1", "default"]
    assert (selected.width, selected.height) == (1080, 1920)


def test_douyin_live_photo_pauses_when_default_is_temporarily_unavailable(
    monkeypatch,
) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    asset = RemoteAsset(
        candidates=["https://v26-web.douyinvod.com/direct.mp4"],
        index=1,
        width=720,
        height=1280,
        video_uri="v0200fg10000verifiedlivephoto",
        duration=2.0,
        quality_candidates=[
            {
                "width": 720,
                "height": 1280,
                "bit_rate": 1_500_000,
                "urls": ["https://v26-web.douyinvod.com/direct.mp4"],
            }
        ],
    )

    def probe_ratio(ydl, url, *, ratio, **kwargs):
        if ratio == "default":
            raise TimeoutError("temporary endpoint timeout")
        return {
            "url": url,
            "width": 720,
            "height": 1280,
            "bit_rate": 1_500_000,
            "filesize": 378_678,
            "duration": 2.0,
            "vcodec": "h264",
            "acodec": "none",
        }

    monkeypatch.setattr(engine, "_probe_douyin_ratio_with_retry", probe_ratio)

    with pytest.raises(TemporaryAccessError, match="task was paused"):
        engine._select_highest_douyin_live_photo_asset(
            object(),
            asset,
            callback=None,
            should_cancel=lambda: False,
        )


def test_douyin_live_photo_does_not_replace_failed_higher_bitrate_direct(
    monkeypatch,
) -> None:
    high_url = "https://v26-web.douyinvod.com/direct-high.mp4"
    low_url = "https://v11-web.douyinvod.com/direct-low.mp4"
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    asset = RemoteAsset(
        candidates=[high_url, low_url],
        index=1,
        width=1080,
        height=1920,
        video_uri="v0200fg10000verifiedlivephoto",
        duration=2.0,
        quality_candidates=[
            {
                "width": 1080,
                "height": 1920,
                "bit_rate": 5_000_000,
                "urls": [high_url],
            },
            {
                "width": 1080,
                "height": 1920,
                "bit_rate": 4_500_000,
                "urls": [low_url],
            },
        ],
    )

    def probe_ratio(ydl, url, *, ratio, **kwargs):
        if ratio == "author-feed-1":
            raise TimeoutError("higher rendition temporarily unavailable")
        return {
            "url": url,
            "width": 1080,
            "height": 1920,
            "bit_rate": 4_500_000,
            "filesize": 378_678,
            "duration": 2.0,
            "vcodec": "h264",
            "acodec": "none",
        }

    monkeypatch.setattr(engine, "_probe_douyin_ratio_with_retry", probe_ratio)

    with pytest.raises(TemporaryAccessError, match="author-feed-1"):
        engine._select_highest_douyin_live_photo_asset(
            object(),
            asset,
            callback=None,
            should_cancel=lambda: False,
        )


def test_douyin_default_timeout_never_allows_720p_fallback(monkeypatch) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    info = _douyin_raw_info()
    info["_douyin_verified_cache_only"] = True

    def probe(ydl, url, *, expected_duration, should_cancel):
        ratio = parse_qs(urlsplit(url).query)["ratio"][0]
        if ratio != "720p":
            raise TimeoutError(f"{ratio} timed out")
        return {
            "url": url,
            "width": 720,
            "height": 1280,
            "bit_rate": 700_000,
            "filesize": 7_000_000,
            "duration": expected_duration,
            "vcodec": "h264",
            "acodec": "aac",
        }

    monkeypatch.setattr(engine, "_probe_douyin_candidate", probe)

    with pytest.raises(TemporaryAccessError, match="default"):
        engine._add_douyin_probe_formats(
            object(),
            info,
            expected_id="1111111111111111111",
            verification_url="https://www.douyin.com/video/1111111111111111111",
            should_cancel=lambda: False,
        )
    assert not any(
        value["format_id"].startswith("douyin-api-") for value in info["formats"]
    )


def test_download_error_redacts_external_urls_headers_and_tokens() -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    error = DownloadError(
        "transfer failed for https://cdn.example/video?token=must-not-leak "
        "Authorization: Bearer top-secret sessionid=also-secret "
        "X-Bogus: bogus-secret signature: signature-secret "
        "headers={'Cookie': 'dict-cookie-secret', "
        "'Authorization': 'Bearer dict-bearer-secret'} "
        "Bearer standalone-secret"
    )

    with pytest.raises(MediaDownloadError) as raised:
        engine._raise_download_error(
            error,
            "https://www.douyin.com/video/1111111111111111111",
        )

    message = str(raised.value)
    assert "[redacted URL]" in message
    assert "must-not-leak" not in message
    assert "top-secret" not in message
    assert "also-secret" not in message
    assert "bogus-secret" not in message
    assert "signature-secret" not in message
    assert "dict-cookie-secret" not in message
    assert "dict-bearer-secret" not in message
    assert "standalone-secret" not in message


@pytest.mark.parametrize(
    ("raw_message", "expected"),
    [
        (
            "media endpoint redirected to an unrecognized Douyin CDN host "
            "(host: secret-token.vendor-cdn.net)",
            "(host: unavailable)",
        ),
        (
            "Douyin media redirect could not be trusted. Redirect host: "
            "secret-token.vendor-cdn.net; Redirect reason: unrecognized-host",
            "Redirect host: unavailable",
        ),
        (
            "Douyin media redirect could not be trusted. Redirect host: "
            "secret-token.edge.pstatp.com; Redirect reason: "
            "unverified-source-binding",
            "Redirect host: pstatp.com",
        ),
    ],
)
def test_external_error_redaction_sanitizes_saved_douyin_redirect_hosts(
    raw_message: str,
    expected: str,
) -> None:
    sanitized = safe_external_error_message(raw_message)

    assert expected in sanitized
    assert "secret-token" not in sanitized
    assert safe_external_error_message(sanitized) == sanitized


def test_external_error_redaction_removes_http_reason_and_ip_literals() -> None:
    assert safe_external_error_message(
        "HTTP Error 400: token=SECRET 127.0.0.1 [::1]"
    ) == "HTTP Error 400"
    sanitized = safe_external_error_message(
        "Media transport failed at 127.0.0.1 and [::1]"
    )
    assert sanitized == (
        "Media transport failed at [redacted IP] and [redacted IP]"
    )
    for host_value in (
        "::1",
        "2130706433",
        "010.000.000.001",
        "secret-token.vendor-cdn.net",
    ):
        sanitized_host = safe_external_error_message(
            f"HTTPSConnectionPool(host='{host_value}', port=443)"
        )
        assert host_value not in sanitized_host
        assert "host=[redacted]" in sanitized_host
        assert safe_external_error_message(sanitized_host) == sanitized_host


@pytest.mark.parametrize(
    ("raw_message", "secret"),
    [
        ("X-Bogus: bogus-secret", "bogus-secret"),
        ("signature: signature-secret", "signature-secret"),
        ("Bearer standalone-secret", "standalone-secret"),
        ("headers={'Cookie': 'dict-cookie-secret'}", "dict-cookie-secret"),
        (
            "headers={'Authorization': 'Bearer dict-bearer-secret'}",
            "dict-bearer-secret",
        ),
        ("params={'msToken': 'MS_SECRET'}", "MS_SECRET"),
        ("params={'a_bogus': 'AB_SECRET'}", "AB_SECRET"),
        ("params={'verifyFp': 'FP_SECRET'}", "FP_SECRET"),
        ("params={'passport_csrf_token': 'CSRF_SECRET'}", "CSRF_SECRET"),
        ("cookies={'odin_tt': 'ODIN_SECRET'}", "ODIN_SECRET"),
        ("sid_tt=SID_SECRET; sessionid_ss=SESSION_SECRET", "SID_SECRET"),
        ("sid_tt=SID_SECRET; sessionid_ss=SESSION_SECRET", "SESSION_SECRET"),
        ("headers=[('Cookie', 'sid=COOKIESECRET')]", "COOKIESECRET"),
        ("headers=[('X-Bogus', 'BOGUSSECRET')]", "BOGUSSECRET"),
        (
            "headers=(('Authorization', 'Basic BASICSECRET'),)",
            "BASICSECRET",
        ),
        ("cookies={'sid': 'COOKIESECRET'}", "COOKIESECRET"),
        ("session_id=SESSIONSECRET", "SESSIONSECRET"),
        (
            "Cookie: foo=bar; arbitrary_name=LEAKME; sid_tt=SENSITIVE",
            "LEAKME",
        ),
        (
            "cookies={'sid':'SECRET1','arbitrary':'SECRET2'}",
            "SECRET2",
        ),
        ("Set-Cookie=sid=SECRET1; foo=SECRET2", "SECRET2"),
    ],
)
def test_external_error_redaction_covers_header_representations(
    raw_message: str,
    secret: str,
) -> None:
    sanitized = safe_external_error_message(raw_message)
    assert secret not in sanitized
    assert safe_external_error_message(sanitized) == sanitized


def test_douyin_ffprobe_missing_is_explicit(monkeypatch) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    monkeypatch.setattr("app.downloader.shutil.which", lambda name: None)
    monkeypatch.setattr("app.downloader.FFPROBE_FALLBACK_PATHS", ())

    with pytest.raises(MediaDownloadError, match="FFprobe was not found"):
        engine._ffprobe_douyin_media(
            b"fixture",
            should_cancel=lambda: False,
        )


def test_douyin_ffprobe_start_failure_is_explicit(monkeypatch) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    def fail(*args, **kwargs):
        raise OSError("invalid executable")

    monkeypatch.setattr("app.downloader.subprocess.Popen", fail)

    with pytest.raises(MediaDownloadError, match="could not be started"):
        engine._run_ffprobe(
            ["ffprobe"],
            timeout_seconds=1,
            should_cancel=lambda: False,
        )


def test_douyin_ffprobe_finds_apple_silicon_homebrew_path(
    monkeypatch, tmp_path
) -> None:
    executable = tmp_path / "ffprobe"
    executable.write_bytes(b"fixture")
    executable.chmod(0o755)
    monkeypatch.setattr("app.downloader.shutil.which", lambda name: None)
    monkeypatch.setattr("app.downloader.FFPROBE_FALLBACK_PATHS", (str(executable),))

    assert MediaDownloader._find_ffprobe_executable() == str(executable)


def test_douyin_ffprobe_payload_parses_video_and_audio_streams() -> None:
    payload = json.dumps(
        {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1080,
                    "height": 1920,
                    "bit_rate": "1088835",
                },
                {"codec_type": "audio", "codec_name": "aac"},
            ],
            "format": {"duration": "72.809002"},
        }
    ).encode()

    parsed = MediaDownloader._parse_ffprobe_payload(payload)

    assert parsed == {
        "width": 1080,
        "height": 1920,
        "vcodec": "h264",
        "acodec": "aac",
        "bit_rate": 1_088_835,
        "duration": "72.809002",
    }


def test_douyin_ffprobe_payload_tolerates_unknown_numeric_values() -> None:
    payload = json.dumps(
        {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": "N/A",
                    "height": None,
                    "bit_rate": "N/A",
                }
            ],
            "format": {"duration": "N/A", "bit_rate": "N/A"},
        }
    ).encode()

    parsed = MediaDownloader._parse_ffprobe_payload(payload)

    assert parsed is not None
    assert parsed["width"] == 0
    assert parsed["height"] == 0
    assert parsed["bit_rate"] == 0
    assert parsed["duration"] is None


def test_douyin_ffprobe_payload_uses_valid_format_numeric_values() -> None:
    payload = json.dumps(
        {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "width": 1440,
                    "height": 2560,
                    "duration": "N/A",
                    "bit_rate": "N/A",
                }
            ],
            "format": {"duration": "23.357823", "bit_rate": "20132350"},
        }
    ).encode()

    parsed = MediaDownloader._parse_ffprobe_payload(payload)

    assert parsed is not None
    assert parsed["duration"] == "23.357823"
    assert parsed["bit_rate"] == 20_132_350


def test_douyin_ffprobe_process_can_be_cancelled_promptly() -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    cancellation_checks = 0

    def should_cancel() -> bool:
        nonlocal cancellation_checks
        cancellation_checks += 1
        return cancellation_checks >= 2

    started = time.monotonic()
    with pytest.raises(DownloadCancelled):
        engine._run_ffprobe(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            timeout_seconds=5,
            should_cancel=should_cancel,
        )

    assert time.monotonic() - started < 2
    assert cancellation_checks >= 2


def test_douyin_ffprobe_uses_short_timeouts_and_never_passes_a_url(
    monkeypatch,
    tmp_path,
) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    calls = []
    local_path = tmp_path / "candidate.mp4"
    local_path.write_bytes(b"complete-local-file")
    monkeypatch.setattr("app.downloader.shutil.which", lambda name: "/bin/ffprobe")

    def run(command, *, input_data=None, timeout_seconds, should_cancel):
        calls.append((command, input_data, timeout_seconds))
        return None

    monkeypatch.setattr(engine, "_run_ffprobe", run)

    assert engine._ffprobe_douyin_media(
        b"fixture",
        should_cancel=lambda: False,
    ) is None
    assert engine._ffprobe_douyin_media(
        b"fixture",
        local_path=local_path,
        should_cancel=lambda: False,
    ) is None
    assert [call[2] for call in calls] == [3.0, 15.0]
    assert [call[0][call[0].index("-i") + 1] for call in calls] == [
        "pipe:0",
        str(local_path),
    ]
    assert calls[0][1] == b"fixture"
    assert calls[1][1] is None
    assert all(
        not argument.startswith(("http://", "https://"))
        for command, _, _ in calls
        for argument in command
    )


def test_douyin_ffprobe_prefix_timeout_continues_to_local_file(
    monkeypatch,
    tmp_path,
) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    calls = []
    local_path = tmp_path / "candidate.mp4"
    local_path.write_bytes(b"complete-local-file")
    monkeypatch.setattr("app.downloader.shutil.which", lambda name: "/bin/ffprobe")

    def run(command, *, input_data=None, timeout_seconds, should_cancel):
        calls.append((command, input_data, timeout_seconds))
        if input_data is not None:
            raise TimeoutError("prefix timed out")
        return json.dumps(
            {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1080,
                        "height": 1920,
                        "duration": "20.0",
                    }
                ]
            }
        ).encode()

    monkeypatch.setattr(engine, "_run_ffprobe", run)

    prefix_result = engine._ffprobe_douyin_media(
        b"fixture",
        should_cancel=lambda: False,
    )
    result = engine._ffprobe_douyin_media(
        b"fixture",
        local_path=local_path,
        should_cancel=lambda: False,
    )

    assert prefix_result is None
    assert result is not None
    assert (result["width"], result["height"]) == (1080, 1920)
    assert [call[2] for call in calls] == [3.0, 15.0]
    assert calls[1][0][calls[1][0].index("-i") + 1] == str(local_path)


def test_douyin_ffprobe_uses_local_file_when_moov_is_at_tail(
    monkeypatch,
    tmp_path,
) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    calls = []
    local_path = tmp_path / "candidate.mp4"
    local_path.write_bytes(b"complete-local-file-with-tail-moov")
    monkeypatch.setattr("app.downloader.shutil.which", lambda name: "/bin/ffprobe")

    def run(command, *, input_data=None, timeout_seconds, should_cancel):
        calls.append((command, input_data, timeout_seconds))
        if input_data is not None:
            return json.dumps(
                {
                    "streams": [
                        {
                            "codec_type": "video",
                            "codec_name": "hevc",
                            "width": 1440,
                            "height": 2560,
                            "duration": "N/A",
                        }
                    ],
                    "format": {"duration": "N/A"},
                }
            ).encode()
        return json.dumps(
            {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "hevc",
                        "width": 1440,
                        "height": 2560,
                        "duration": "23.353333",
                        "bit_rate": "20132350",
                    },
                    {
                        "codec_type": "audio",
                        "codec_name": "aac",
                    },
                ],
                "format": {
                    "duration": "23.357823",
                    "size": "59093472",
                },
            }
        ).encode()

    monkeypatch.setattr(engine, "_run_ffprobe", run)

    prefix_result = engine._ffprobe_douyin_media(
        b"prefix-without-tail-moov",
        should_cancel=lambda: False,
    )
    result = engine._ffprobe_douyin_media(
        b"prefix-without-tail-moov",
        local_path=local_path,
        should_cancel=lambda: False,
    )

    assert prefix_result is not None
    assert prefix_result["duration"] is None
    assert result is not None
    assert (result["width"], result["height"]) == (1440, 2560)
    assert result["vcodec"] == "hevc"
    assert [call[2] for call in calls] == [3.0, 15.0]
    assert calls[0][1] == b"prefix-without-tail-moov"
    assert calls[1][1] is None
    assert calls[1][0][calls[1][0].index("-i") + 1] == str(local_path)
    assert all(
        not argument.startswith(("http://", "https://"))
        for command, _, _ in calls
        for argument in command
    )


def test_douyin_ffprobe_uses_local_file_when_prefix_has_no_dimensions(
    monkeypatch,
    tmp_path,
) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    calls = []
    local_path = tmp_path / "candidate.mp4"
    local_path.write_bytes(b"complete-local-file")
    monkeypatch.setattr("app.downloader.shutil.which", lambda name: "/bin/ffprobe")

    def run(command, *, input_data=None, timeout_seconds, should_cancel):
        calls.append((command, input_data, timeout_seconds))
        if input_data is not None:
            return json.dumps(
                {
                    "streams": [
                        {
                            "codec_type": "video",
                            "codec_name": "hevc",
                            "width": 0,
                            "height": 0,
                            "duration": "23.357823",
                        }
                    ]
                }
            ).encode()
        return json.dumps(
            {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "hevc",
                        "width": 1440,
                        "height": 2560,
                        "duration": "23.357823",
                    }
                ]
            }
        ).encode()

    monkeypatch.setattr(engine, "_run_ffprobe", run)

    prefix_result = engine._ffprobe_douyin_media(
        b"prefix-without-dimensions",
        should_cancel=lambda: False,
    )
    result = engine._ffprobe_douyin_media(
        b"prefix-without-dimensions",
        local_path=local_path,
        should_cancel=lambda: False,
    )

    assert prefix_result is not None
    assert (prefix_result["width"], prefix_result["height"]) == (0, 0)
    assert result is not None
    assert (result["width"], result["height"]) == (1440, 2560)
    assert [call[2] for call in calls] == [3.0, 15.0]
    assert calls[1][0][calls[1][0].index("-i") + 1] == str(local_path)


def test_bilibili_profile_probes_first_video_when_playlist_has_no_author(
    monkeypatch,
) -> None:
    class BilibiliProfileYoutubeDL(FakeYoutubeDL):
        created_options: list[dict] = []
        extracted_urls: list[str] = []

        def extract_info(self, url: str, download: bool):
            self.extracted_urls.append(url)
            if url == "https://space.bilibili.com/946974/video":
                return {
                    "id": "946974",
                    "entries": [
                        {
                            "id": "BV1rp4y1e745",
                            "title": "Demo video",
                            "url": "https://www.bilibili.com/video/BV1rp4y1e745",
                        },
                        {
                            "id": "BV1xx411c7mD",
                            "title": "Second demo video",
                            "url": "https://www.bilibili.com/video/BV1xx411c7mD",
                        },
                    ],
                }
            return {
                "id": "BV1rp4y1e745",
                "title": "Demo video",
                "uploader": "Blender",
                "upload_date": "20251114",
            }

    monkeypatch.setattr("app.downloader.YoutubeDL", BilibiliProfileYoutubeDL)
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    result = engine.discover(
        "https://space.bilibili.com/946974/video",
        Platform.BILIBILI,
        SourceKind.PROFILE,
    )

    assert result.author == "Blender"
    assert result.items[0].author == "Blender"
    assert len(result.items) == 2
    assert BilibiliProfileYoutubeDL.created_options[0]["extract_flat"] == (
        "in_playlist"
    )
    assert BilibiliProfileYoutubeDL.extracted_urls == [
        "https://space.bilibili.com/946974/video",
        "https://www.bilibili.com/video/BV1rp4y1e745",
    ]


def test_ytdlp_download_uses_uncapped_best_format_and_reports_progress(
    monkeypatch, tmp_path
) -> None:
    FakeYoutubeDL.created_options.clear()
    monkeypatch.setattr("app.downloader.YoutubeDL", FakeYoutubeDL)
    engine = MediaDownloader()
    events = []
    item = DownloadItem(
        id="item-1",
        media_id="abcdefghijk",
        source_url="https://www.youtube.com/watch?v=abcdefghijk",
        title="First video",
        media_type=MediaType.VIDEO,
    )

    outcome = engine.download_item(
        item,
        Platform.YOUTUBE,
        tmp_path,
        callback=events.append,
    )

    options = FakeYoutubeDL.created_options[0]
    assert options["format"] == "bestvideo*+bestaudio/best"
    assert options["cookiesfrombrowser"] == ("chrome",)
    assert options["noplaylist"] is True
    assert options["overwrites"] is True
    assert "trim_file_name" not in options
    assert options["outtmpl"]["default"] == OUTPUT_TEMPLATE
    assert options["js_runtimes"] == {"deno": {}}
    assert Path(options["paths"]["temp"]).parts[-3:] == (
        ".parts",
        "standalone",
        "item-1",
    )
    assert "height" not in options["format"]
    progress = next(event.progress for event in events if event.event == "downloading")
    assert progress is not None
    assert progress.percent == 50.0
    assert progress.downloaded_bytes == 5_000
    assert progress.total_bytes == 10_000
    assert outcome.selected_format == "313+251"
    assert outcome.resolution == "3840x2160"
    assert len(outcome.output_paths) == 1
    assert not Path(options["paths"]["temp"]).exists()
    assert not (tmp_path / ".parts").exists()


def test_douyin_download_disables_partial_resume_between_quality_retries(
    monkeypatch, tmp_path
) -> None:
    media_id = "2222222222222222222"
    profile_id = "profile-a"

    class SuccessfulDouyinYoutubeDL(FakeYoutubeDL):
        def extract_info(
            self,
            url: str,
            download: bool,
            process: bool = True,
        ):
            return {
                "id": media_id,
                "channel_id": profile_id,
                "title": "Douyin video",
                "formats": [],
            }

    SuccessfulDouyinYoutubeDL.created_options.clear()
    monkeypatch.setattr("app.downloader.YoutubeDL", SuccessfulDouyinYoutubeDL)
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    def add_verified_format(_ydl, info, **kwargs):
        info["formats"].append(
            {
                "format_id": "douyin-api-1080x1920-1",
                "url": "https://v26-web.douyinvod.com/verified.mp4",
                "width": 1080,
                "height": 1920,
                "tbr": 5_000,
                "filesize": 5,
            }
        )
        return True

    monkeypatch.setattr(
        engine,
        "_add_douyin_probe_formats",
        add_verified_format,
    )

    def download_asset(_ydl, assets, output_dir, *args, **kwargs):
        output_file = output_dir / f"2025-11-14-Douyin video [{media_id}].mp4"
        output_file.write_bytes(b"media")
        return output_file, assets[0]

    monkeypatch.setattr(
        engine,
        "_download_first_available_asset",
        download_asset,
    )
    item = DownloadItem(
        id="douyin-item",
        media_id=media_id,
        source_url=f"https://www.douyin.com/video/{media_id}",
        title="Douyin video",
        media_type=MediaType.VIDEO,
        metadata={"profile_url": f"https://www.douyin.com/user/{profile_id}"},
    )

    engine.download_item(item, Platform.DOUYIN, tmp_path)

    assert SuccessfulDouyinYoutubeDL.created_options[0]["continuedl"] is False


def _configure_verified_douyin_transfer(
    monkeypatch,
    engine: MediaDownloader,
    *,
    media_id: str,
    payload: bytes,
    ffprobe_width: int = 1440,
    ffprobe_height: int = 2560,
    ffprobe_bit_rate: int = 10_000_000,
    selected_url: str = "https://v26-web.douyinvod.com/verified.mp4",
    final_url: str = "https://v26-web.douyinvod.com/final.mp4",
    source_payload: bytes | None = None,
):
    class Headers:
        def __init__(self, response_payload: bytes) -> None:
            self.response_payload = response_payload

        def get(self, name: str, default=None):
            values = {
                "Content-Type": "video/mp4",
                "Content-Length": str(len(self.response_payload)),
            }
            return values.get(name, default)

    class AssetResponse:
        def __init__(self, response_payload: bytes) -> None:
            self.headers = Headers(response_payload)
            self.payload = response_payload
            self.url = final_url
            self.offset = 0
            self.closed = False

        def read(self, size: int) -> bytes:
            chunk = self.payload[self.offset : self.offset + size]
            self.offset += len(chunk)
            return chunk

        def close(self) -> None:
            self.closed = True

    class DirectTransferYoutubeDL(FakeYoutubeDL):
        created_options: list[dict] = []
        responses: list[AssetResponse] = []
        requests = []

        def urlopen(self, request):
            self.requests.append(request)
            response_payload = (
                source_payload
                if source_payload is not None
                and request.url.startswith("https://api-play.amemv.com/")
                else payload
            )
            response = AssetResponse(response_payload)
            self.responses.append(response)
            return response

    monkeypatch.setattr("app.downloader.YoutubeDL", DirectTransferYoutubeDL)
    monkeypatch.setattr(
        engine,
        "_extract_douyin_raw_info",
        lambda *args, **kwargs: {
            "id": media_id,
            "title": "Douyin video",
            "upload_date": "20251114",
            "duration": 10.0,
            "formats": [],
        },
    )

    def add_verified_format(_ydl, info, **kwargs):
        info["formats"].append(
            {
                "format_id": "douyin-api-1440x2560-1",
                "url": selected_url,
                "width": 1440,
                "height": 2560,
                "tbr": 10_000,
                "filesize": len(payload),
                "vcodec": "hevc",
                "acodec": "none",
                "_douyin_probe_prefix_size": len(payload),
                "_douyin_probe_prefix_sha256": hashlib.sha256(payload).hexdigest(),
                "_douyin_probe_source_url": (
                    "https://api-play.amemv.com/aweme/v1/play/"
                    "?video_id=fixture"
                ),
            }
        )
        return True

    monkeypatch.setattr(engine, "_add_douyin_probe_formats", add_verified_format)
    monkeypatch.setattr(
        engine,
        "_wait_for_douyin_probe_retry",
        lambda delay, should_cancel: None,
    )
    monkeypatch.setattr(engine, "_find_ffprobe_executable", lambda: "/fake/ffprobe")
    monkeypatch.setattr(
        engine,
        "_run_ffprobe",
        lambda *args, **kwargs: json.dumps(
            {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "hevc",
                        "width": ffprobe_width,
                        "height": ffprobe_height,
                        "bit_rate": str(ffprobe_bit_rate),
                        "duration": "10.0",
                    }
                ],
                "format": {
                    "duration": "10.0",
                    "bit_rate": str(ffprobe_bit_rate),
                    "size": str(len(payload)),
                },
            }
        ).encode(),
    )
    return DirectTransferYoutubeDL


@pytest.mark.parametrize(
    ("width", "height", "bit_rate"),
    [
        (720, 1280, 10_000_000),
        (1440, 2560, 1_000_000),
    ],
)
def test_douyin_verified_transfer_rejects_lower_final_media_without_overwrite(
    monkeypatch,
    tmp_path,
    width,
    height,
    bit_rate,
) -> None:
    media_id = "7664225419386607205"
    payload = b"\x00\x00\x00\x18ftypisom" + b"verified-original-bytes"
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    _inject_fake_douyin_media_opener(monkeypatch, engine)
    direct_ydl = _configure_verified_douyin_transfer(
        monkeypatch,
        engine,
        media_id=media_id,
        payload=payload,
        ffprobe_width=width,
        ffprobe_height=height,
        ffprobe_bit_rate=bit_rate,
    )
    expected = tmp_path / f"2025-11-14-Douyin video [{media_id}].mp4"
    expected.write_bytes(b"existing-good-file")
    item = DownloadItem(
        id="douyin-item",
        media_id=media_id,
        source_url=f"https://www.douyin.com/video/{media_id}",
        title="Douyin video",
        media_type=MediaType.VIDEO,
        metadata={"_job_id": "douyin-job"},
    )

    with pytest.raises(TemporaryAccessError, match="media transfer"):
        engine.download_item(item, Platform.DOUYIN, tmp_path)

    assert expected.read_bytes() == b"existing-good-file"
    assert len(direct_ydl.responses) == DOUYIN_TRANSFER_ATTEMPTS * 2
    assert all(response.closed for response in direct_ydl.responses)
    assert not list(tmp_path.glob("*.part"))
    assert not (tmp_path / ".parts").exists()


def test_douyin_verified_transfer_preserves_exact_bytes_and_reports_resolution(
    monkeypatch,
    tmp_path,
) -> None:
    media_id = "7664225419386607205"
    payload = b"\x00\x00\x00\x18ftypisom" + b"verified-original-bytes"
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    _inject_fake_douyin_media_opener(monkeypatch, engine)
    direct_ydl = _configure_verified_douyin_transfer(
        monkeypatch,
        engine,
        media_id=media_id,
        payload=payload,
    )
    item = DownloadItem(
        id="douyin-item",
        media_id=media_id,
        source_url=f"https://www.douyin.com/video/{media_id}",
        title="Douyin video",
        media_type=MediaType.VIDEO,
        metadata={"_job_id": "douyin-job"},
    )

    outcome = engine.download_item(item, Platform.DOUYIN, tmp_path)

    path = Path(outcome.output_paths[0])
    assert path.read_bytes() == payload
    assert path.name == f"2025-11-14-Douyin video [{media_id}].mp4"
    assert outcome.selected_format == "douyin-api-1440x2560-1"
    assert outcome.resolution == "1440x2560"
    assert direct_ydl.created_options[0]["continuedl"] is False
    assert direct_ydl.responses[0].closed is True
    assert direct_ydl.requests[0].url == (
        "https://api-play.amemv.com/aweme/v1/play/?video_id=fixture"
    )
    assert not list(tmp_path.glob("*.part"))
    assert not (tmp_path / ".parts").exists()


def test_douyin_verified_transfer_falls_back_to_probed_final_after_source_changes(
    monkeypatch,
    tmp_path,
) -> None:
    media_id = "7664225419386607205"
    payload = b"\x00\x00\x00\x18ftypisom" + b"verified-original-bytes"
    changed_payload = b"\x00\x00\x00\x18ftypisom" + b"changed-source-response"
    assert len(changed_payload) == len(payload)
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    _inject_fake_douyin_media_opener(monkeypatch, engine)
    direct_ydl = _configure_verified_douyin_transfer(
        monkeypatch,
        engine,
        media_id=media_id,
        payload=payload,
        source_payload=changed_payload,
    )
    item = DownloadItem(
        id="douyin-item",
        media_id=media_id,
        source_url=f"https://www.douyin.com/video/{media_id}",
        title="Douyin video",
        media_type=MediaType.VIDEO,
        metadata={"_job_id": "douyin-job"},
    )

    outcome = engine.download_item(item, Platform.DOUYIN, tmp_path)

    path = Path(outcome.output_paths[0])
    assert path.read_bytes() == payload
    assert [request.url for request in direct_ydl.requests] == [
        "https://api-play.amemv.com/aweme/v1/play/?video_id=fixture",
        "https://v26-web.douyinvod.com/verified.mp4",
    ]
    assert len(direct_ydl.responses) == 2
    assert all(response.closed for response in direct_ydl.responses)
    assert not list(tmp_path.glob("*.part"))


def test_douyin_verified_transfer_accepts_bound_regional_cdn_and_preserves_bytes(
    monkeypatch,
    tmp_path,
) -> None:
    media_id = "7664225419386607205"
    payload = b"\x00\x00\x00\x18ftypisom" + b"regional-original-bytes"
    final_url = "https://edge.video.pstatp.com/original.mp4"
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    _inject_fake_douyin_media_opener(monkeypatch, engine)
    direct_ydl = _configure_verified_douyin_transfer(
        monkeypatch,
        engine,
        media_id=media_id,
        payload=payload,
        selected_url=final_url,
        final_url=final_url,
    )
    item = DownloadItem(
        id="douyin-item",
        media_id=media_id,
        source_url=f"https://www.douyin.com/video/{media_id}",
        title="Douyin video",
        media_type=MediaType.VIDEO,
        metadata={"_job_id": "douyin-job"},
    )

    outcome = engine.download_item(item, Platform.DOUYIN, tmp_path)

    path = Path(outcome.output_paths[0])
    assert path.read_bytes() == payload
    assert outcome.resolution == "1440x2560"
    assert direct_ydl.requests[0].url == (
        "https://api-play.amemv.com/aweme/v1/play/?video_id=fixture"
    )
    assert direct_ydl.responses[0].offset == len(payload)
    assert direct_ydl.responses[0].closed is True
    assert not list(tmp_path.glob("*.part"))


def test_douyin_verified_transfer_blocks_untrusted_final_redirect(
    monkeypatch,
    tmp_path,
) -> None:
    media_id = "7664225419386607205"
    payload = b"\x00\x00\x00\x18ftypisom" + b"private-response"
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    _inject_fake_douyin_media_opener(monkeypatch, engine)
    direct_ydl = _configure_verified_douyin_transfer(
        monkeypatch,
        engine,
        media_id=media_id,
        payload=payload,
        final_url="http://127.0.0.1:8080/private.mp4",
    )
    item = DownloadItem(
        id="douyin-item",
        media_id=media_id,
        source_url=f"https://www.douyin.com/video/{media_id}",
        title="Douyin video",
        media_type=MediaType.VIDEO,
        metadata={"_job_id": "douyin-job"},
    )

    with pytest.raises(
        TemporaryAccessError,
        match="redirect could not be trusted",
    ) as error:
        engine.download_item(item, Platform.DOUYIN, tmp_path)

    message = str(error.value)
    assert "Redirect host: unavailable" in message
    assert "reason: non-https-scheme" in message
    assert "127.0.0.1" not in message
    assert "private.mp4" not in message
    assert direct_ydl.responses[0].offset == 0
    assert direct_ydl.responses[0].closed is True
    assert not list(tmp_path.iterdir())


def test_douyin_verified_transfer_pauses_on_unknown_redirect_without_read_or_file(
    monkeypatch,
    tmp_path,
) -> None:
    media_id = "7664225419386607205"
    payload = b"\x00\x00\x00\x18ftypisom" + b"unknown-cdn-response"
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    _inject_fake_douyin_media_opener(monkeypatch, engine)
    direct_ydl = _configure_verified_douyin_transfer(
        monkeypatch,
        engine,
        media_id=media_id,
        payload=payload,
        final_url=(
            "https://unrecognized-cdn.vendor-cdn.net/original.mp4"
            "?token=must-not-persist"
        ),
    )
    item = DownloadItem(
        id="douyin-item",
        media_id=media_id,
        source_url=f"https://www.douyin.com/video/{media_id}",
        title="Douyin video",
        media_type=MediaType.VIDEO,
        metadata={"_job_id": "douyin-job"},
    )

    with pytest.raises(
        TemporaryAccessError,
        match="redirect could not be trusted",
    ) as error:
        engine.download_item(item, Platform.DOUYIN, tmp_path)

    message = str(error.value)
    hostname = "unrecognized-cdn.vendor-cdn.net"
    fingerprint = hashlib.sha256(hostname.encode("ascii")).hexdigest()[:12]
    assert "Redirect host: unavailable" in message
    assert f"Redirect host fingerprint: {fingerprint}" in message
    assert "unrecognized-cdn" not in message
    assert "reason: unrecognized-host" in message
    assert "original.mp4" not in message
    assert "must-not-persist" not in message
    assert len(direct_ydl.responses) == 1
    assert direct_ydl.requests[0].url == (
        "https://api-play.amemv.com/aweme/v1/play/?video_id=fixture"
    )
    assert direct_ydl.responses[0].offset == 0
    assert direct_ydl.responses[0].closed is True
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "redirect_source_url",
    [
        None,
        "https://edge.video.pstatp.com/untrusted-source.mp4",
    ],
)
def test_douyin_regional_transfer_requires_strict_probe_source_binding(
    tmp_path,
    redirect_source_url,
) -> None:
    payload = b"\x00\x00\x00\x18ftypisom" + b"bound-original"
    public_url = "https://edge.video.pstatp.com/original.mp4"

    class AssetYoutubeDL:
        calls = 0

        def urlopen(self, request):
            self.calls += 1
            raise AssertionError("Unbound regional CDN candidate must not be opened")

    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    ydl = AssetYoutubeDL()
    with pytest.raises(MediaDownloadError, match="Untrusted Douyin media URL"):
        engine._download_first_available_asset(
            ydl,
            [
                RemoteAsset(
                    candidates=[public_url],
                    index=1,
                    width=1080,
                    height=1920,
                    size=len(payload),
                    duration=2.0,
                    bit_rate=2_000_000,
                    video_codec="h264",
                    audio_codec="none",
                    probe_prefix_size=len(payload),
                    probe_prefix_sha256=hashlib.sha256(payload).hexdigest(),
                    redirect_source_url=redirect_source_url,
                )
            ],
            tmp_path,
            "2025-09-01",
            "Live Photo",
            "1111111111111111111",
            "https://www.douyin.com/user/verified-profile",
            media_type=MediaType.VIDEO,
            platform=Platform.DOUYIN,
            callback=None,
            should_cancel=lambda: False,
            asset_index=1,
            verify_declared_dimensions=True,
            require_quality_fingerprint=True,
        )

    assert ydl.calls == 0
    assert not list(tmp_path.iterdir())


def test_douyin_redirect_to_known_regional_cdn_reports_missing_source_binding(
    monkeypatch,
    tmp_path,
) -> None:
    payload = b"\x00\x00\x00\x18ftypisom" + b"must-not-be-read"

    class Response:
        url = "https://edge.video.pstatp.com/original.mp4"
        headers = {"Content-Type": "video/mp4"}

        def __init__(self) -> None:
            self.read_calls = 0
            self.closed = False

        def read(self, size: int) -> bytes:
            self.read_calls += 1
            return payload[:size]

        def close(self) -> None:
            self.closed = True

    class AssetYoutubeDL:
        def __init__(self) -> None:
            self.response = Response()

        def urlopen(self, request):
            return self.response

    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    _inject_fake_douyin_media_opener(monkeypatch, engine)
    ydl = AssetYoutubeDL()
    with pytest.raises(
        TemporaryAccessError,
        match="reason: unverified-source-binding",
    ) as error:
        engine._download_first_available_asset(
            ydl,
            [
                RemoteAsset(
                    candidates=["https://v26-web.douyinvod.com/original.mp4"],
                    index=1,
                    width=1080,
                    height=1920,
                    size=len(payload),
                    duration=2.0,
                    bit_rate=2_000_000,
                    video_codec="h264",
                    audio_codec="none",
                    probe_prefix_size=len(payload),
                    probe_prefix_sha256=hashlib.sha256(payload).hexdigest(),
                    redirect_source_url=None,
                )
            ],
            tmp_path,
            "2025-09-01",
            "Live Photo",
            "1111111111111111111",
            "https://www.douyin.com/user/verified-profile",
            media_type=MediaType.VIDEO,
            platform=Platform.DOUYIN,
            callback=None,
            should_cancel=lambda: False,
            asset_index=1,
            verify_declared_dimensions=True,
            require_quality_fingerprint=True,
        )

    message = str(error.value)
    assert "Redirect host: pstatp.com" in message
    assert ydl.response.read_calls == 0
    assert ydl.response.closed is True
    assert not list(tmp_path.iterdir())


def test_douyin_verified_transfer_requires_size_or_bitrate_fingerprint(
    monkeypatch,
    tmp_path,
) -> None:
    media_id = "7664225419386607205"
    payload = b"\x00\x00\x00\x18ftypisom" + b"unfingerprinted-bytes"
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    _inject_fake_douyin_media_opener(monkeypatch, engine)
    _configure_verified_douyin_transfer(
        monkeypatch,
        engine,
        media_id=media_id,
        payload=payload,
    )

    def add_unfingerprinted_format(_ydl, info, **kwargs):
        info["formats"].append(
            {
                "format_id": "douyin-api-1440x2560-unfingerprinted",
                "url": "https://v26-web.douyinvod.com/verified.mp4",
                "width": 1440,
                "height": 2560,
            }
        )
        return True

    monkeypatch.setattr(
        engine,
        "_add_douyin_probe_formats",
        add_unfingerprinted_format,
    )
    expected = tmp_path / f"2025-11-14-Douyin video [{media_id}].mp4"
    expected.write_bytes(b"existing-good-file")
    item = DownloadItem(
        id="douyin-item",
        media_id=media_id,
        source_url=f"https://www.douyin.com/video/{media_id}",
        title="Douyin video",
        media_type=MediaType.VIDEO,
        metadata={"_job_id": "douyin-job"},
    )

    with pytest.raises(MediaDownloadError, match="no bitrate or complete size"):
        engine.download_item(item, Platform.DOUYIN, tmp_path)

    assert expected.read_bytes() == b"existing-good-file"
    assert not list(tmp_path.glob(".original-media-*.part"))
    assert not (tmp_path / ".parts").exists()


def test_douyin_verified_transfer_uses_probe_duration_when_item_omits_it(
    monkeypatch,
    tmp_path,
) -> None:
    media_id = "7664225419386607205"
    payload = b"\x00\x00\x00\x18ftypisom" + b"wrong-duration-bytes"
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    _inject_fake_douyin_media_opener(monkeypatch, engine)
    _configure_verified_douyin_transfer(
        monkeypatch,
        engine,
        media_id=media_id,
        payload=payload,
        ffprobe_bit_rate=10_000_000,
    )
    monkeypatch.setattr(
        engine,
        "_extract_douyin_raw_info",
        lambda *args, **kwargs: {
            "id": media_id,
            "title": "Douyin video",
            "upload_date": "20251114",
            "formats": [],
        },
    )

    def add_verified_format(_ydl, info, **kwargs):
        info["formats"].append(
            {
                "format_id": "douyin-api-1440x2560-duration",
                "url": "https://v26-web.douyinvod.com/verified.mp4",
                "width": 1440,
                "height": 2560,
                "tbr": 10_000,
                "filesize": len(payload),
                "duration": 10.0,
                "vcodec": "hevc",
                "acodec": "none",
                "_douyin_probe_prefix_size": len(payload),
                "_douyin_probe_prefix_sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
        return True

    monkeypatch.setattr(engine, "_add_douyin_probe_formats", add_verified_format)
    monkeypatch.setattr(
        engine,
        "_run_ffprobe",
        lambda *args, **kwargs: json.dumps(
            {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "hevc",
                        "width": 1440,
                        "height": 2560,
                        "bit_rate": "10000000",
                        "duration": "20.0",
                    }
                ],
                "format": {
                    "duration": "20.0",
                    "bit_rate": "10000000",
                    "size": str(len(payload)),
                },
            }
        ).encode(),
    )
    expected = tmp_path / f"2025-11-14-Douyin video [{media_id}].mp4"
    expected.write_bytes(b"existing-good-file")
    item = DownloadItem(
        id="douyin-item",
        media_id=media_id,
        source_url=f"https://www.douyin.com/video/{media_id}",
        title="Douyin video",
        media_type=MediaType.VIDEO,
        metadata={"_job_id": "douyin-job"},
    )

    with pytest.raises(TemporaryAccessError, match="media transfer"):
        engine.download_item(item, Platform.DOUYIN, tmp_path)

    assert expected.read_bytes() == b"existing-good-file"
    assert not list(tmp_path.glob(".original-media-*.part"))


def test_douyin_final_verifier_requires_duration_fingerprint(tmp_path) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    path = tmp_path / "candidate.mp4"
    path.write_bytes(b"candidate")

    with pytest.raises(MediaDownloadError, match="no duration fingerprint"):
        engine._verify_local_video_asset(
            path,
            RemoteAsset(
                candidates=["https://v26-web.douyinvod.com/candidate.mp4"],
                index=1,
                width=1440,
                height=2560,
                size=path.stat().st_size,
                bit_rate=10_000_000,
            ),
            should_cancel=lambda: False,
            require_quality_fingerprint=True,
        )


def test_douyin_final_verifier_rejects_changed_content_prefix(tmp_path) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    path = tmp_path / "candidate.mp4"
    payload = b"\x00\x00\x00\x18ftypisom" + b"different-video"
    path.write_bytes(payload)

    with pytest.raises(MediaDownloadError, match="content did not match"):
        engine._verify_local_video_asset(
            path,
            RemoteAsset(
                candidates=["https://v26-web.douyinvod.com/candidate.mp4"],
                index=1,
                width=1080,
                height=1920,
                size=len(payload),
                duration=2.0,
                bit_rate=1_000_000,
                video_codec="hevc",
                audio_codec="none",
                probe_prefix_size=len(payload),
                probe_prefix_sha256=hashlib.sha256(b"expected-video").hexdigest(),
            ),
            should_cancel=lambda: False,
            require_quality_fingerprint=True,
        )


def test_douyin_final_verifier_rejects_codec_and_short_duration_changes(
    monkeypatch,
    tmp_path,
) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    path = tmp_path / "candidate.mp4"
    payload = b"\x00\x00\x00\x18ftypisom" + b"verified-video"
    path.write_bytes(payload)
    monkeypatch.setattr(engine, "_find_ffprobe_executable", lambda: "/fake/ffprobe")

    def verify(*, video_codec: str, duration: float, error_match: str) -> None:
        monkeypatch.setattr(
            engine,
            "_run_ffprobe",
            lambda *args, **kwargs: json.dumps(
                {
                    "streams": [
                        {
                            "codec_type": "video",
                            "codec_name": video_codec,
                            "width": 1080,
                            "height": 1920,
                            "bit_rate": "1000000",
                            "duration": str(duration),
                        }
                    ],
                    "format": {
                        "duration": str(duration),
                        "bit_rate": "1000000",
                        "size": str(len(payload)),
                    },
                }
            ).encode(),
        )
        with pytest.raises(MediaDownloadError, match=error_match):
            engine._verify_local_video_asset(
                path,
                RemoteAsset(
                    candidates=["https://v26-web.douyinvod.com/candidate.mp4"],
                    index=1,
                    width=1080,
                    height=1920,
                    size=len(payload),
                    duration=2.0,
                    bit_rate=1_000_000,
                    video_codec="hevc",
                    audio_codec="none",
                    probe_prefix_size=len(payload),
                    probe_prefix_sha256=hashlib.sha256(payload).hexdigest(),
                ),
                should_cancel=lambda: False,
                require_quality_fingerprint=True,
            )

    verify(video_codec="h264", duration=2.0, error_match="video codec")
    verify(video_codec="hevc", duration=2.6, error_match="duration did not match")
    assert engine._douyin_duration_tolerance(2.0) == 0.5


def test_douyin_direct_transfer_does_not_follow_predictable_temp_symlink(
    monkeypatch,
    tmp_path,
) -> None:
    media_id = "7664225419386607205"
    payload = b"\x00\x00\x00\x18ftypisom" + b"verified-original-bytes"
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    _inject_fake_douyin_media_opener(monkeypatch, engine)
    _configure_verified_douyin_transfer(
        monkeypatch,
        engine,
        media_id=media_id,
        payload=payload,
    )
    expected = tmp_path / f"2025-11-14-Douyin video [{media_id}].mp4"
    outside = tmp_path / "outside-sentinel.bin"
    outside.write_bytes(b"must-not-change")
    predictable_temp = expected.with_name(
        f"{expected.name}.{threading.get_ident()}.part"
    )
    predictable_temp.symlink_to(outside)
    item = DownloadItem(
        id="douyin-item",
        media_id=media_id,
        source_url=f"https://www.douyin.com/video/{media_id}",
        title="Douyin video",
        media_type=MediaType.VIDEO,
        metadata={"_job_id": "douyin-job"},
    )

    outcome = engine.download_item(item, Platform.DOUYIN, tmp_path)

    path = Path(outcome.output_paths[0])
    assert outside.read_bytes() == b"must-not-change"
    assert predictable_temp.is_symlink()
    assert not path.is_symlink()
    assert path.read_bytes() == payload
    assert not list(tmp_path.glob(".original-media-*.part"))


def test_ytdlp_temp_paths_are_isolated_between_jobs(monkeypatch, tmp_path) -> None:
    FakeYoutubeDL.created_options.clear()
    monkeypatch.setattr("app.downloader.YoutubeDL", FakeYoutubeDL)
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    for job_id in ("job-a", "job-b"):
        item = DownloadItem(
            id="same-item",
            media_id="abcdefghijk",
            source_url="https://www.youtube.com/watch?v=abcdefghijk",
            title="First video",
            media_type=MediaType.VIDEO,
            metadata={"_job_id": job_id},
        )
        engine.download_item(item, Platform.YOUTUBE, tmp_path)

    first_temp = FakeYoutubeDL.created_options[0]["paths"]["temp"]
    second_temp = FakeYoutubeDL.created_options[1]["paths"]["temp"]
    assert first_temp != second_temp
    assert Path(first_temp).parts[-3:] == (".parts", "job-a", "same-item")
    assert Path(second_temp).parts[-3:] == (".parts", "job-b", "same-item")


def test_ytdlp_partial_file_is_preserved_for_resumable_download_failure(
    monkeypatch,
    tmp_path,
) -> None:
    class PartialFailureYoutubeDL(FakeYoutubeDL):
        def extract_info(self, url: str, download: bool):
            parts_dir = Path(self.options["paths"]["temp"])
            (parts_dir / "fixture.mp4.part").write_bytes(b"partial-media")
            raise DownloadError("fixture transfer failed")

    monkeypatch.setattr("app.downloader.YoutubeDL", PartialFailureYoutubeDL)
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    item = DownloadItem(
        id="failed-item",
        media_id="abcdefghijk",
        source_url="https://www.youtube.com/watch?v=abcdefghijk",
        title="Failed video",
        media_type=MediaType.VIDEO,
        metadata={"_job_id": "failed-job"},
    )

    with pytest.raises(MediaDownloadError, match="fixture transfer failed"):
        engine.download_item(item, Platform.YOUTUBE, tmp_path)

    partial_path = tmp_path / ".parts" / "failed-job" / "failed-item" / "fixture.mp4.part"
    assert partial_path.read_bytes() == b"partial-media"


def test_ytdlp_partial_file_is_preserved_for_resumable_cancellation(
    monkeypatch,
    tmp_path,
) -> None:
    class PartialCancellationYoutubeDL(FakeYoutubeDL):
        def extract_info(self, url: str, download: bool):
            parts_dir = Path(self.options["paths"]["temp"])
            partial_path = parts_dir / "fixture.mp4.part"
            partial_path.write_bytes(b"partial-media")
            self.options["progress_hooks"][0](
                {
                    "status": "downloading",
                    "downloaded_bytes": partial_path.stat().st_size,
                    "total_bytes": 5 * 1024 * 1024,
                }
            )
            raise AssertionError("cancellation must interrupt the progress hook")

    monkeypatch.setattr("app.downloader.YoutubeDL", PartialCancellationYoutubeDL)
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    item = DownloadItem(
        id="cancelled-item",
        media_id="abcdefghijk",
        source_url="https://www.youtube.com/watch?v=abcdefghijk",
        title="Cancelled video",
        media_type=MediaType.VIDEO,
        metadata={"_job_id": "cancelled-job"},
    )

    with pytest.raises(DownloadCancelledError, match="Task cancelled"):
        engine.download_item(
            item,
            Platform.YOUTUBE,
            tmp_path,
            should_cancel=lambda: True,
        )

    partial_path = (
        tmp_path
        / ".parts"
        / "cancelled-job"
        / "cancelled-item"
        / "fixture.mp4.part"
    )
    assert partial_path.read_bytes() == b"partial-media"


@pytest.mark.parametrize("cancelled", [False, True])
def test_douyin_partial_file_is_removed_after_failure_or_cancellation(
    monkeypatch,
    tmp_path,
    cancelled,
) -> None:
    media_id = "2222222222222222222"

    monkeypatch.setattr("app.downloader.YoutubeDL", FakeYoutubeDL)
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    monkeypatch.setattr(
        engine,
        "_extract_douyin_raw_info",
        lambda *args, **kwargs: {
            "id": media_id,
            "title": "Douyin video",
            "formats": [],
        },
    )
    monkeypatch.setattr(
        engine,
        "_add_douyin_probe_formats",
        lambda _ydl, info, **kwargs: (
            info["formats"].append(
                {
                    "format_id": "douyin-api-1080x1920-1",
                    "url": "https://v26-web.douyinvod.com/verified.mp4",
                    "width": 1080,
                    "height": 1920,
                    "tbr": 5_000,
                    "filesize": len(b"partial-media"),
                }
            )
            or True
        ),
    )

    def download_asset(_ydl, assets, output_dir, *args, **kwargs):
        parts_dir = output_dir / ".parts" / "douyin-job" / "douyin-item"
        (parts_dir / "fixture.mp4.part").write_bytes(b"partial-media")
        if cancelled:
            raise DownloadCancelledError("Task cancelled")
        raise MediaDownloadError("fixture Douyin transfer failed")

    monkeypatch.setattr(
        engine,
        "_download_first_available_asset",
        download_asset,
    )
    item = DownloadItem(
        id="douyin-item",
        media_id=media_id,
        source_url=f"https://www.douyin.com/video/{media_id}",
        title="Douyin video",
        media_type=MediaType.VIDEO,
        metadata={"_job_id": "douyin-job"},
    )

    expected_error = DownloadCancelledError if cancelled else MediaDownloadError
    with pytest.raises(expected_error):
        engine.download_item(
            item,
            Platform.DOUYIN,
            tmp_path,
            should_cancel=lambda: cancelled,
        )

    assert not (tmp_path / ".parts").exists()


def test_ytdlp_parts_cleanup_does_not_follow_parent_symlink(tmp_path) -> None:
    output_dir = tmp_path / "output"
    outside_dir = tmp_path / "outside"
    scoped_outside_dir = outside_dir / "job" / "item"
    output_dir.mkdir()
    scoped_outside_dir.mkdir(parents=True)
    sentinel = scoped_outside_dir / "do-not-delete.txt"
    sentinel.write_text("preserved", encoding="utf-8")
    (output_dir / ".parts").symlink_to(outside_dir, target_is_directory=True)
    parts_dir = output_dir / ".parts" / "job" / "item"

    MediaDownloader._cleanup_ytdlp_parts_dir(
        parts_dir,
        output_dir,
        remove_contents=True,
    )

    assert sentinel.read_text(encoding="utf-8") == "preserved"
    with pytest.raises(MediaDownloadError, match="symbolic link"):
        MediaDownloader._prepare_ytdlp_parts_dir(parts_dir, output_dir)


def test_cookie_database_error_retries_without_browser_cookies(monkeypatch) -> None:
    class CookieFallbackYoutubeDL(FakeYoutubeDL):
        created_options: list[dict] = []

        def extract_info(self, url: str, download: bool):
            if "cookiesfrombrowser" in self.options:
                raise DownloadError("Could not copy Chrome cookie database")
            return super().extract_info(url, download)

    monkeypatch.setattr("app.downloader.YoutubeDL", CookieFallbackYoutubeDL)
    engine = MediaDownloader(DownloaderConfig(allow_cookie_fallback=True))

    result = engine.discover(
        "https://www.youtube.com/@Example/videos",
        Platform.YOUTUBE,
        SourceKind.PROFILE,
    )

    assert result.cookie_fallback_used is True
    assert "cookiesfrombrowser" in CookieFallbackYoutubeDL.created_options[0]
    assert "cookiesfrombrowser" not in CookieFallbackYoutubeDL.created_options[1]


def test_cookie_database_error_requires_user_action_by_default(monkeypatch) -> None:
    class CookieErrorYoutubeDL(FakeYoutubeDL):
        def extract_info(self, url: str, download: bool):
            raise DownloadError("Could not copy Chrome cookie database")

    monkeypatch.setattr("app.downloader.YoutubeDL", CookieErrorYoutubeDL)
    engine = MediaDownloader()

    with pytest.raises(TemporaryAccessError, match="Fully quit Chrome") as error:
        engine.discover(
            "https://www.youtube.com/@Example/videos",
            Platform.YOUTUBE,
            SourceKind.PROFILE,
        )

    assert "verification page is not required" in str(error.value)


def test_authentication_errors_are_exposed_as_actionable_state(monkeypatch) -> None:
    class AuthErrorYoutubeDL(FakeYoutubeDL):
        def extract_info(self, url: str, download: bool):
            raise DownloadError("Fresh cookies are needed to confirm you're not a bot")

    monkeypatch.setattr("app.downloader.YoutubeDL", AuthErrorYoutubeDL)
    engine = MediaDownloader()

    with pytest.raises(AuthenticationRequiredError) as error:
        engine.discover(
            "https://www.youtube.com/@Example/videos",
            Platform.YOUTUBE,
            SourceKind.PROFILE,
        )

    assert error.value.verification_url == "https://www.youtube.com/@Example/videos"


def test_rate_limit_errors_do_not_request_chrome_verification() -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser="chrome"))

    with pytest.raises(
        TemporaryAccessError,
        match="Chrome verification is not required",
    ):
        engine._raise_download_error(
            DownloadError("访问频繁，请稍后重试"),
            "https://www.douyin.com/video/7664225419386607205",
        )


def test_xhs_output_path_starts_with_date_and_sanitizes_title(tmp_path) -> None:
    engine = MediaDownloader()

    path = engine._xhs_output_path(
        tmp_path,
        "2025-11-14",
        "A/B: title",
        "note-id",
        "jpg",
        2,
    )

    assert path.name == "2025-11-14-A_B_ title [note-id]-002.jpg"
    assert path.parent == tmp_path


@pytest.mark.parametrize(
    ("first_bytes", "content_type", "expected"),
    [
        (b"\xff\xd8\xff\xe0", "application/octet-stream", "jpg"),
        (b"\x89PNG\r\n\x1a\n", "image/jpeg", "png"),
        (b"RIFF\x00\x00\x00\x00WEBP", "image/jpeg", "webp"),
        (b"\x00\x00\x00\x18ftypavif", "application/octet-stream", "avif"),
        (b"\x00\x00\x00\x28ftypheic", "image/jpeg", "heic"),
    ],
)
def test_asset_extension_uses_media_signature_before_server_header(
    first_bytes: bytes,
    content_type: str,
    expected: str,
) -> None:
    assert (
        MediaDownloader._asset_extension(
            "https://cdn.example/asset",
            content_type,
            MediaType.IMAGE,
            first_bytes,
        )
        == expected
    )


def _iso_bmff_image(brand: bytes, width: int, height: int) -> bytes:
    def box(box_type: bytes, payload: bytes) -> bytes:
        return (len(payload) + 8).to_bytes(4, "big") + box_type + payload

    ftyp = box(b"ftyp", brand + b"\x00\x00\x00\x00" + brand)
    ispe = box(
        b"ispe",
        b"\x00\x00\x00\x00"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big"),
    )
    ipco = box(b"ipco", ispe)
    iprp = box(b"iprp", ipco)
    meta = box(b"meta", b"\x00\x00\x00\x00" + iprp)
    return ftyp + meta


@pytest.mark.parametrize("brand", [b"avif", b"avis", b"heic", b"heix"])
def test_iso_bmff_image_dimensions_parse_avif_and_heic(brand: bytes) -> None:
    assert MediaDownloader._image_dimensions(
        _iso_bmff_image(brand, 1920, 2560)
    ) == (1920, 2560)


@pytest.mark.parametrize(
    ("compatible_brand", "content_type", "expected"),
    [
        (b"avif", "application/octet-stream", "avif"),
        (b"heic", "application/octet-stream", "heic"),
        (b"mif1", "image/avif", "avif"),
        (b"mif1", "application/octet-stream", "bin"),
    ],
)
def test_iso_bmff_extension_uses_compatible_brands_and_content_type(
    compatible_brand: bytes,
    content_type: str,
    expected: str,
) -> None:
    ftyp_payload = b"mif1" + b"\x00\x00\x00\x00" + compatible_brand + b"mif1"
    first_bytes = (
        (len(ftyp_payload) + 8).to_bytes(4, "big") + b"ftyp" + ftyp_payload
    )

    assert (
        MediaDownloader._asset_extension(
            "https://sns-img-bd.xhscdn.com/original",
            content_type,
            MediaType.IMAGE,
            first_bytes,
        )
        == expected
    )


def test_iso_bmff_image_below_declared_resolution_fails_closed(tmp_path) -> None:
    payload = _iso_bmff_image(b"avif", 1, 1)

    class Headers:
        def get(self, name: str, default=None):
            values = {
                "Content-Type": "image/avif",
                "Content-Length": str(len(payload)),
            }
            return values.get(name, default)

    class Response:
        headers = Headers()

        def __init__(self) -> None:
            self.finished = False

        def read(self, size: int) -> bytes:
            if self.finished:
                return b""
            self.finished = True
            return payload

        def close(self) -> None:
            return None

    class AssetYoutubeDL:
        def urlopen(self, request):
            return Response()

    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    with pytest.raises(MediaDownloadError, match="below its declared"):
        engine._download_first_available_asset(
            AssetYoutubeDL(),
            [
                RemoteAsset(
                    candidates=["https://sns-img-bd.xhscdn.com/original.avif"],
                    index=1,
                    width=1920,
                    height=2560,
                )
            ],
            tmp_path,
            "2025-11-14",
            "Image post",
            "note-id",
            "https://www.xiaohongshu.com/explore/note-id",
            media_type=MediaType.IMAGE,
            platform=Platform.XIAOHONGSHU,
            callback=None,
            should_cancel=lambda: False,
            asset_index=1,
            verify_declared_dimensions=True,
        )

    assert not list(tmp_path.iterdir())


def test_xhs_asset_download_replaces_existing_file_with_current_candidate(
    tmp_path,
) -> None:
    payload = b"\xff\xd8\xffnew-original"

    class Headers:
        def get(self, name: str, default=None):
            values = {
                "Content-Type": "image/jpeg",
                "Content-Length": str(len(payload)),
            }
            return values.get(name, default)

    class Response:
        headers = Headers()

        def __init__(self) -> None:
            self.finished = False

        def read(self, size: int) -> bytes:
            if self.finished:
                return b""
            self.finished = True
            return payload

        def close(self) -> None:
            pass

    class AssetYoutubeDL:
        def urlopen(self, request):
            return Response()

    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    expected = engine._xhs_output_path(
        tmp_path,
        "2025-11-14",
        "Title",
        "note-id",
        "jpg",
        1,
    )
    expected.write_bytes(b"old-lower-quality")

    path, _ = engine._download_first_available_asset(
        AssetYoutubeDL(),
        [
            RemoteAsset(
                candidates=["https://sns-img-bd.xhscdn.com/original"],
                index=1,
            )
        ],
        tmp_path,
        "2025-11-14",
        "Title",
        "note-id",
        "https://www.xiaohongshu.com/explore/note-id",
        media_type=MediaType.IMAGE,
        platform=Platform.XIAOHONGSHU,
        callback=None,
        should_cancel=lambda: False,
        asset_index=1,
    )

    assert path == expected.resolve()
    assert expected.read_bytes() == payload


def test_xhs_asset_request_blocks_external_host_and_redacts_referer(
    tmp_path,
) -> None:
    payload = b"\xff\xd8\xfforiginal"

    class Headers:
        def get(self, name: str, default=None):
            values = {
                "Content-Type": "image/jpeg",
                "Content-Length": str(len(payload)),
            }
            return values.get(name, default)

    class Response:
        headers = Headers()

        def __init__(self) -> None:
            self.finished = False

        def read(self, size: int) -> bytes:
            if self.finished:
                return b""
            self.finished = True
            return payload

        def close(self) -> None:
            return None

    class AssetYoutubeDL:
        def __init__(self) -> None:
            self.requests = []

        def urlopen(self, request):
            self.requests.append(request)
            return Response()

    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    source_url = (
        "https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9"
        "?xsec_token=TOP_SECRET&xsec_source=pc_user"
    )
    ydl = AssetYoutubeDL()

    with pytest.raises(MediaDownloadError, match="Untrusted Xiaohongshu"):
        engine._download_first_available_asset(
            ydl,
            [RemoteAsset(candidates=["https://evil.example/asset"], index=1)],
            tmp_path,
            "2025-11-14",
            "Title",
            "6411cf99000000001300b6d9",
            source_url,
            media_type=MediaType.IMAGE,
            platform=Platform.XIAOHONGSHU,
            callback=None,
            should_cancel=lambda: False,
            asset_index=1,
        )

    assert ydl.requests == []

    path, _ = engine._download_first_available_asset(
        ydl,
        [
            RemoteAsset(
                candidates=["https://sns-img-bd.xhscdn.com/original"],
                index=1,
            )
        ],
        tmp_path,
        "2025-11-14",
        "Title",
        "6411cf99000000001300b6d9",
        source_url,
        media_type=MediaType.IMAGE,
        platform=Platform.XIAOHONGSHU,
        callback=None,
        should_cancel=lambda: False,
        asset_index=1,
    )

    assert path.read_bytes() == payload
    assert len(ydl.requests) == 1
    assert ydl.requests[0].headers["Referer"] == "https://www.xiaohongshu.com/"
    assert "TOP_SECRET" not in str(ydl.requests[0].headers)


def test_xhs_asset_request_blocks_untrusted_final_redirect_before_read(
    tmp_path,
) -> None:
    class Headers:
        def get(self, name: str, default=None):
            values = {
                "Content-Type": "image/jpeg",
                "Content-Length": "16",
            }
            return values.get(name, default)

    class RedirectedResponse:
        headers = Headers()
        url = "http://127.0.0.1:8080/private.jpg"

        def __init__(self) -> None:
            self.read_calls = 0
            self.closed = False

        def read(self, size: int) -> bytes:
            self.read_calls += 1
            return b"\xff\xd8\xffprivate-data"

        def close(self) -> None:
            self.closed = True

    class AssetYoutubeDL:
        def __init__(self) -> None:
            self.response = RedirectedResponse()

        def urlopen(self, request):
            return self.response

    ydl = AssetYoutubeDL()
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    with pytest.raises(MediaDownloadError, match="untrusted URL"):
        engine._download_first_available_asset(
            ydl,
            [
                RemoteAsset(
                    candidates=[
                        "https://sns-img-bd.xhscdn.com/redirectable-original"
                    ],
                    index=1,
                )
            ],
            tmp_path,
            "2025-11-14",
            "Title",
            "6411cf99000000001300b6d9",
            "https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9",
            media_type=MediaType.IMAGE,
            platform=Platform.XIAOHONGSHU,
            callback=None,
            should_cancel=lambda: False,
            asset_index=1,
        )

    assert ydl.response.read_calls == 0
    assert ydl.response.closed is True
    assert not list(tmp_path.iterdir())


def test_highest_asset_stream_preserves_bytes_and_reports_aggregate_progress(
    monkeypatch,
    tmp_path,
) -> None:
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + (1440).to_bytes(4, "big")
        + (2560).to_bytes(4, "big")
        + b"original-image-payload"
    )

    class Headers:
        def get(self, name: str, default=None):
            values = {
                "Content-Type": "image/png",
                "Content-Length": str(len(payload)),
            }
            return values.get(name, default)

    class Response:
        headers = Headers()

        def __init__(self) -> None:
            self.finished = False

        def read(self, size: int) -> bytes:
            if self.finished:
                return b""
            self.finished = True
            return payload

        def close(self) -> None:
            pass

    class AssetYoutubeDL:
        def urlopen(self, request):
            return Response()

    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    _inject_fake_douyin_media_opener(monkeypatch, engine)
    events = []
    path, chosen = engine._download_first_available_asset(
        AssetYoutubeDL(),
        [
            RemoteAsset(
                candidates=["https://p3-pc-sign.douyinpic.com/original"],
                index=1,
                width=1080,
                height=1920,
                format_id="douyin-highest-image-1080x1920",
            )
        ],
        tmp_path,
        "2025-09-01",
        "Image post",
        "1111111111111111111",
        "https://www.douyin.com/user/verified-profile",
        media_type=MediaType.IMAGE,
        platform=Platform.DOUYIN,
        callback=events.append,
        should_cancel=lambda: False,
        asset_index=1,
        progress_index=2,
        progress_count=4,
        verify_declared_dimensions=True,
    )

    assert path.read_bytes() == payload
    assert path.name.endswith("[1111111111111111111]-001.png")
    assert (chosen.width, chosen.height) == (1440, 2560)
    assert events[-1].progress is not None
    assert events[-1].progress.percent == 50.0
    assert events[-1].progress.fragment_index == 2
    assert events[-1].progress.fragment_count == 4


def test_highest_asset_stream_retries_candidate_below_declared_resolution(
    monkeypatch,
    tmp_path,
) -> None:
    def png(width: int, height: int) -> bytes:
        return (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + width.to_bytes(4, "big")
            + height.to_bytes(4, "big")
            + b"payload"
        )

    payloads = [png(720, 1280), png(1080, 1920)]

    class Headers:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def get(self, name: str, default=None):
            values = {
                "Content-Type": "image/png",
                "Content-Length": str(len(self.payload)),
            }
            return values.get(name, default)

    class Response:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload
            self.headers = Headers(payload)
            self.finished = False
            self.closed = False

        def read(self, size: int) -> bytes:
            if self.finished:
                return b""
            self.finished = True
            return self.payload

        def close(self) -> None:
            self.closed = True

    class AssetYoutubeDL:
        def __init__(self) -> None:
            self.responses = [Response(value) for value in payloads]

        def urlopen(self, request):
            return self.responses.pop(0)

    ydl = AssetYoutubeDL()
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    _inject_fake_douyin_media_opener(monkeypatch, engine)
    path, chosen = engine._download_first_available_asset(
        ydl,
        [
            RemoteAsset(
                candidates=[
                    "https://p3-pc-sign.douyinpic.com/lower",
                    "https://p9-pc-sign.douyinpic.com/original",
                ],
                index=1,
                width=1080,
                height=1920,
            )
        ],
        tmp_path,
        "2025-09-01",
        "Image post",
        "1111111111111111111",
        "https://www.douyin.com/user/verified-profile",
        media_type=MediaType.IMAGE,
        platform=Platform.DOUYIN,
        callback=None,
        should_cancel=lambda: False,
        asset_index=1,
        verify_declared_dimensions=True,
    )

    assert path.read_bytes() == payloads[1]
    assert (chosen.width, chosen.height) == (1080, 1920)


@pytest.mark.parametrize("status", [401, 403, 404, 410, 429, 503])
def test_highest_asset_stream_closes_http_error_response(
    monkeypatch,
    tmp_path,
    status,
) -> None:
    response = Response(
        BytesIO(),
        url="https://v26-web.douyinvod.com/unavailable.mp4",
        headers={},
        status=status,
        reason="Temporary media failure",
    )
    error = HTTPError(response)

    class AssetYoutubeDL:
        def urlopen(self, request):
            raise error

    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    _inject_fake_douyin_media_opener(monkeypatch, engine)
    monkeypatch.setattr(
        engine,
        "_wait_for_douyin_probe_retry",
        lambda delay, should_cancel: None,
    )

    with pytest.raises(TemporaryAccessError, match="task was paused"):
        engine._download_first_available_asset(
            AssetYoutubeDL(),
            [
                RemoteAsset(
                    candidates=[response.url],
                    index=1,
                    width=1080,
                    height=1920,
                )
            ],
            tmp_path,
            "2025-09-01",
            "Live Photo",
            "1111111111111111111",
            "https://www.douyin.com/user/verified-profile",
            media_type=MediaType.VIDEO,
            platform=Platform.DOUYIN,
            callback=None,
            should_cancel=lambda: False,
            asset_index=1,
            verify_declared_dimensions=True,
        )

    assert response.closed is True
    assert not list(tmp_path.iterdir())


def test_douyin_transfer_retries_transport_error_then_succeeds(
    monkeypatch,
    tmp_path,
) -> None:
    payload = b"\x00\x00\x00\x18ftypisom" + b"verified-media"

    class Headers:
        def get(self, name: str, default=None):
            return {
                "Content-Type": "video/mp4",
                "Content-Length": str(len(payload)),
            }.get(name, default)

    class SuccessfulResponse:
        url = "https://v26-web.douyinvod.com/retry.mp4"
        headers = Headers()

        def __init__(self) -> None:
            self.offset = 0

        def read(self, size: int) -> bytes:
            chunk = payload[self.offset : self.offset + size]
            self.offset += len(chunk)
            return chunk

        def close(self) -> None:
            return None

    class AssetYoutubeDL:
        def __init__(self) -> None:
            self.calls = 0

        def urlopen(self, request):
            self.calls += 1
            if self.calls < 3:
                raise TransportError("temporary CDN disconnect")
            return SuccessfulResponse()

    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    _inject_fake_douyin_media_opener(monkeypatch, engine)
    delays = []
    monkeypatch.setattr(
        engine,
        "_wait_for_douyin_probe_retry",
        lambda delay, should_cancel: delays.append(delay),
    )
    ydl = AssetYoutubeDL()

    path, _ = engine._download_first_available_asset(
        ydl,
        [
            RemoteAsset(
                candidates=["https://v26-web.douyinvod.com/retry.mp4"],
                index=1,
            )
        ],
        tmp_path,
        "2025-09-01",
        "Video",
        "1111111111111111111",
        "https://www.douyin.com/video/1111111111111111111",
        media_type=MediaType.VIDEO,
        platform=Platform.DOUYIN,
        callback=None,
        should_cancel=lambda: False,
    )

    assert path.read_bytes() == payload
    assert ydl.calls == 3
    assert delays == [1.0, 2.0]


def test_douyin_transfer_read_interruption_pauses_after_finite_retries(
    monkeypatch,
    tmp_path,
) -> None:
    payload = b"\x00\x00\x00\x18ftypisom" + b"partial-media"

    class Headers:
        def get(self, name: str, default=None):
            return {
                "Content-Type": "video/mp4",
                "Content-Length": str(len(payload) + 100),
            }.get(name, default)

    class InterruptedResponse:
        url = "https://v26-web.douyinvod.com/interrupted.mp4"
        headers = Headers()

        def __init__(self) -> None:
            self.reads = 0
            self.closed = False

        def read(self, size: int) -> bytes:
            self.reads += 1
            if self.reads == 1:
                return payload
            raise TransportError("connection reset while reading")

        def close(self) -> None:
            self.closed = True

    class AssetYoutubeDL:
        def __init__(self) -> None:
            self.responses = []

        def urlopen(self, request):
            response = InterruptedResponse()
            self.responses.append(response)
            return response

    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    _inject_fake_douyin_media_opener(monkeypatch, engine)
    monkeypatch.setattr(
        engine,
        "_wait_for_douyin_probe_retry",
        lambda delay, should_cancel: None,
    )
    ydl = AssetYoutubeDL()

    with pytest.raises(TemporaryAccessError, match="task was paused"):
        engine._download_first_available_asset(
            ydl,
            [
                RemoteAsset(
                    candidates=[
                        "https://v26-web.douyinvod.com/interrupted.mp4"
                    ],
                    index=1,
                )
            ],
            tmp_path,
            "2025-09-01",
            "Video",
            "1111111111111111111",
            "https://www.douyin.com/video/1111111111111111111",
            media_type=MediaType.VIDEO,
            platform=Platform.DOUYIN,
            callback=None,
            should_cancel=lambda: False,
        )

    assert len(ydl.responses) == 3
    assert all(response.closed for response in ydl.responses)
    assert not list(tmp_path.glob(".original-media-*.part"))


@pytest.mark.parametrize(
    "ffprobe_payload",
    [
        b'{"streams": [], "format": {}}',
        (
            b'{"streams": [{"codec_type": "video", "width": 720, '
            b'"height": 1280}], "format": {"duration": "2.0"}}'
        ),
    ],
)
def test_live_photo_local_verification_retries_and_pauses_invalid_or_lower_media(
    monkeypatch,
    tmp_path,
    ffprobe_payload,
) -> None:
    payload = b"not-an-mp4-media-stream"

    class Headers:
        def get(self, name: str, default=None):
            values = {
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(payload)),
            }
            return values.get(name, default)

    class AssetResponse:
        headers = Headers()

        def __init__(self) -> None:
            self.finished = False
            self.closed = False

        def read(self, size: int) -> bytes:
            if self.finished:
                return b""
            self.finished = True
            return payload

        def close(self) -> None:
            self.closed = True

    class AssetYoutubeDL:
        def __init__(self) -> None:
            self.responses = []

        def urlopen(self, request):
            response = AssetResponse()
            self.responses.append(response)
            return response

    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    _inject_fake_douyin_media_opener(monkeypatch, engine)
    monkeypatch.setattr(
        engine,
        "_wait_for_douyin_probe_retry",
        lambda delay, should_cancel: None,
    )
    monkeypatch.setattr(
        engine,
        "_find_ffprobe_executable",
        lambda: "/fake/ffprobe",
    )
    monkeypatch.setattr(
        engine,
        "_run_ffprobe",
        lambda *args, **kwargs: ffprobe_payload,
    )

    ydl = AssetYoutubeDL()
    with pytest.raises(TemporaryAccessError, match="task was paused"):
        engine._download_first_available_asset(
            ydl,
            [
                RemoteAsset(
                    candidates=["https://v26-web.douyinvod.com/live.mp4"],
                    index=1,
                    width=1080,
                    height=1920,
                    duration=2.0,
                )
            ],
            tmp_path,
            "2025-09-01",
            "Live Photo",
            "1111111111111111111",
            "https://www.douyin.com/user/verified-profile",
            media_type=MediaType.VIDEO,
            platform=Platform.DOUYIN,
            callback=None,
            should_cancel=lambda: False,
            asset_index=1,
            verify_declared_dimensions=True,
        )

    assert len(ydl.responses) == DOUYIN_TRANSFER_ATTEMPTS
    assert all(response.closed for response in ydl.responses)
    assert not list(tmp_path.iterdir())


def test_xhs_multi_image_failure_reports_asset_and_keeps_completed_paths(
    monkeypatch, tmp_path
) -> None:
    note = XiaohongshuNote(
        note_id="note-id",
        title="Multi image note",
        author="Test Author",
        upload_date="2025-11-14",
        images=[
            RemoteAsset(candidates=["https://cdn.example/1"], index=1),
            RemoteAsset(candidates=["https://cdn.example/2"], index=2),
        ],
    )
    monkeypatch.setattr(
        "app.downloader.parse_xhs_note", lambda *args, **kwargs: (note, False)
    )
    monkeypatch.setattr("app.downloader.YoutubeDL", FakeYoutubeDL)
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    events = []

    def download_asset(*args, asset_index=None, **kwargs):
        if asset_index == 2:
            raise MediaDownloadError("temporary CDN error")
        return tmp_path / "first.webp", note.images[0]

    monkeypatch.setattr(engine, "_download_first_available_asset", download_asset)
    item = DownloadItem(
        id="note-id",
        media_id="note-id",
        source_url="https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9",
        title=note.title,
        media_type=MediaType.IMAGE,
    )

    with pytest.raises(MediaDownloadError, match="Image 2 failed"):
        engine.download_item(
            item,
            Platform.XIAOHONGSHU,
            tmp_path,
            callback=events.append,
        )

    completed = [event for event in events if event.event == "asset_completed"]
    assert completed[-1].output_paths == [str(tmp_path / "first.webp")]


def test_xhs_profile_download_blocks_note_from_different_author(
    monkeypatch,
    tmp_path,
) -> None:
    note_id = "6411cf99000000001300b6d9"
    note = XiaohongshuNote(
        note_id=note_id,
        title="Cross-wired note",
        author="Author B",
        author_id="profile-b",
        upload_date="2025-11-14",
        images=[
            RemoteAsset(
                candidates=["https://sns-img-bd.xhscdn.com/original"],
                index=1,
            )
        ],
    )
    monkeypatch.setattr(
        "app.downloader.parse_xhs_note",
        lambda *args, **kwargs: (note, False),
    )

    class UnexpectedYoutubeDL:
        def __init__(self, options):
            raise AssertionError("Cross-wired note must be blocked before download")

    monkeypatch.setattr("app.downloader.YoutubeDL", UnexpectedYoutubeDL)
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    item = DownloadItem(
        id="profile-note",
        media_id=note_id,
        source_url=f"https://www.xiaohongshu.com/explore/{note_id}",
        title="Queued note",
        media_type=MediaType.IMAGE,
        metadata={
            "xiaohongshu_profile_id": "profile-a",
            "profile_note_membership_verified": True,
        },
    )

    with pytest.raises(MediaDownloadError, match="cross-wired"):
        engine.download_item(item, Platform.XIAOHONGSHU, tmp_path)

    assert not list(tmp_path.iterdir())


def test_xhs_image_and_live_photo_reports_highest_asset_resolution(
    monkeypatch, tmp_path
) -> None:
    image = RemoteAsset(
        candidates=["https://cdn.example/image"],
        index=1,
        width=1920,
        height=2560,
    )
    live_photo = RemoteAsset(
        candidates=["https://cdn.example/live"],
        index=1,
        width=2160,
        height=3840,
        format_id="live-4k",
    )
    note = XiaohongshuNote(
        note_id="note-id",
        title="Live Photo note",
        author="Test Author",
        upload_date="2025-11-14",
        images=[image],
        live_photos=[live_photo],
    )
    monkeypatch.setattr(
        "app.downloader.parse_xhs_note", lambda *args, **kwargs: (note, False)
    )
    monkeypatch.setattr("app.downloader.YoutubeDL", FakeYoutubeDL)
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    verification_calls = []

    def download_asset(
        *args,
        media_type,
        verify_declared_dimensions=False,
        **kwargs,
    ):
        verification_calls.append((media_type, verify_declared_dimensions))
        asset = image if media_type == MediaType.IMAGE else live_photo
        return tmp_path / f"{media_type.value}.bin", asset

    monkeypatch.setattr(engine, "_download_first_available_asset", download_asset)
    item = DownloadItem(
        id="note-id",
        media_id="note-id",
        source_url="https://www.xiaohongshu.com/explore/note-id",
        title=note.title,
        media_type=MediaType.IMAGE,
    )

    outcome = engine.download_item(item, Platform.XIAOHONGSHU, tmp_path)

    assert outcome.resolution == "2160x3840"
    assert outcome.selected_format == "live-4k"
    assert verification_calls == [
        (MediaType.IMAGE, True),
        (MediaType.VIDEO, True),
    ]


def test_xhs_original_video_inherits_best_declared_floor_and_is_verified(
    monkeypatch,
    tmp_path,
) -> None:
    original = RemoteAsset(
        candidates=["https://cdn.example/original"],
        index=1,
        format_id="original",
    )
    highest_stream = RemoteAsset(
        candidates=["https://cdn.example/1080"],
        index=1,
        width=1920,
        height=1080,
        format_id="HD",
    )
    note = XiaohongshuNote(
        note_id="note-id",
        title="Video note",
        author="Test Author",
        upload_date="2025-11-14",
        videos=[original, highest_stream],
    )
    monkeypatch.setattr(
        "app.downloader.parse_xhs_note",
        lambda *args, **kwargs: (note, False),
    )
    monkeypatch.setattr("app.downloader.YoutubeDL", FakeYoutubeDL)
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    captured = {}

    def download_asset(
        ydl,
        assets,
        *args,
        verify_declared_dimensions=False,
        **kwargs,
    ):
        captured["assets"] = assets
        captured["verify"] = verify_declared_dimensions
        return tmp_path / "video.mp4", assets[0]

    monkeypatch.setattr(engine, "_download_first_available_asset", download_asset)
    item = DownloadItem(
        id="note-id",
        media_id="note-id",
        source_url="https://www.xiaohongshu.com/explore/note-id",
        title=note.title,
        media_type=MediaType.VIDEO,
    )

    outcome = engine.download_item(item, Platform.XIAOHONGSHU, tmp_path)

    assert captured["verify"] is True
    assert (captured["assets"][0].width, captured["assets"][0].height) == (
        1920,
        1080,
    )
    assert outcome.selected_format == "original"
    assert outcome.resolution == "1920x1080"
