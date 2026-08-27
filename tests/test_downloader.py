from __future__ import annotations

import json
import sys
import threading
import time
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from yt_dlp import YoutubeDL
from yt_dlp.networking import Response
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.utils import DownloadCancelled, DownloadError

from app.downloader import (
    OUTPUT_TEMPLATE,
    DownloaderConfig,
    MediaDownloader,
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
    calls: list[tuple[MediaType, int, int, int, bool]] = []

    def download_asset(
        ydl,
        assets,
        *args,
        media_type,
        asset_index,
        progress_index,
        progress_count,
        verify_declared_dimensions=False,
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
        (MediaType.IMAGE, 1, 1, 3, True),
        (MediaType.IMAGE, 2, 2, 3, True),
        (MediaType.VIDEO, 1, 3, 3, True),
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


def test_douyin_item_discovery_creates_exactly_one_verified_item(
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

    result = engine.discover(source_url, Platform.DOUYIN, SourceKind.ITEM)

    assert result.author == "Verified author"
    assert len(result.items) == 1
    item = result.items[0]
    assert item.media_id == media_id
    assert item.source_url == source_url
    assert item.title == "Verified title"
    assert item.metadata["verification_url"] == source_url
    assert item.metadata["item_identity_verified"] is True
    assert item.metadata["douyin_item_media"] == {
        "media_id": media_id,
        "video_uri": video_uri,
        "title": "Verified title",
        "author": "Verified author",
        "minimum_width": 1080,
        "minimum_height": 1920,
        "owner_id": "verified-owner",
        "duration_ms": 23_400,
        "create_time": 1_756_656_000,
    }
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
    engine = MediaDownloader(DownloaderConfig(cookie_browser="chrome"))
    info = engine._douyin_raw_info_from_profile_metadata(
        {
            "profile_owner_verified": True,
            "douyin_profile_media": {
                "media_id": media_id,
                "owner_id": profile_id,
                "media_kind": "video",
                "video_uri": "v0200fg10000fixturevideoid",
                "duration_ms": 23_400,
            },
        },
        expected_id=media_id,
        expected_profile_id=profile_id,
        verification_url=f"https://www.douyin.com/user/{profile_id}",
        fallback_title="Video",
    )
    assert info is not None

    def probe(ydl, url, *, expected_duration, should_cancel):
        ratio = parse_qs(urlsplit(url).query)["ratio"][0]
        dimensions = {
            "default": (1440, 2560, 20_132_350, 59_093_472, "h265"),
            "4k": (1080, 1920, 4_000_000, 10_000_000, "h264"),
            "2k": (1080, 1920, 4_000_000, 10_000_000, "h264"),
            "1080p": (1080, 1920, 4_000_000, 10_000_000, "h264"),
            "720p": (720, 1280, 2_000_000, 5_000_000, "h264"),
        }
        width, height, bit_rate, filesize, codec = dimensions[ratio]
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


def test_douyin_expired_direct_mirror_is_optional_when_floor_is_met(
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
    assert engine._add_douyin_probe_formats(
        object(),
        info,
        expected_id="1111111111111111111",
        verification_url="https://www.douyin.com/video/1111111111111111111",
        should_cancel=lambda: False,
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
    assert "default" in info["_douyin_probe_failure"]


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

    with pytest.raises(MediaDownloadError, match="no lower-quality fallback"):
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
        (720, 1280),
        (1080, 1920),
        (1440, 2560),
    }
    retained_1080 = [value for value in added_formats if value["width"] == 1080]
    assert len(retained_1080) == 2
    assert {value["filesize"] for value in retained_1080} == {
        9_000_000,
        10_000_000,
    }
    urls_by_ratio = {
        parse_qs(urlsplit(value).query)["ratio"][0]: value for value in probed_urls
    }
    assert urlsplit(urls_by_ratio["default"]).hostname == "api-play-hl.amemv.com"
    assert all(
        urlsplit(urls_by_ratio[ratio]).hostname == "api-play.amemv.com"
        for ratio in ("4k", "2k", "1080p", "720p")
    )

    with YoutubeDL(
        {"quiet": True, **engine._download_format_options(Platform.DOUYIN)}
    ) as ydl:
        selected = ydl.process_ie_result(info, download=False)

    assert selected["format_id"].startswith("douyin-api-1440x2560")
    assert (selected["width"], selected["height"]) == (1440, 2560)
    assert selected["vcodec"] == "h265"


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

    assert [event.event for event in events] == ["probing"] * 5
    assert [event.message for event in events] == [
        "Checking Douyin quality 1/5: default",
        "Checking Douyin quality 2/5: 4k",
        "Checking Douyin quality 3/5: 2k",
        "Checking Douyin quality 4/5: 1080p",
        "Checking Douyin quality 5/5: 720p",
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
        ratio = parse_qs(urlsplit(url).query)["ratio"][0]
        dimensions = {
            "default": (1440, 2560, 2_000_000, 20_000_000, "h265"),
            "4k": (2160, 3840, 4_000_000, 40_000_000, "vvc"),
            "2k": (1440, 2560, 2_000_000, 20_000_000, "h265"),
            "1080p": (1080, 1920, 1_000_000, 10_000_000, "h264"),
            "720p": (720, 1280, 700_000, 7_000_000, "h264"),
        }
        width, height, bit_rate, filesize, codec = dimensions[ratio]
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
        ratio = parse_qs(urlsplit(url).query)["ratio"][0]
        if ratio == "720p":
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
    probed_urls = []

    def ffprobe(data, url, *, should_cancel):
        probed_urls.append(url)
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
    assert probed_urls == ["https://v26-web.douyinvod.com/verified-video.mp4"]
    assert "cookie" not in request_headers
    assert request_headers["range"] == "bytes=0-262143"
    assert ydl.request.extensions["timeout"] == 10.0
    assert ydl.response.offset == 256 * 1024
    assert ydl.response.closed is True

    monkeypatch.setattr(
        engine,
        "_ffprobe_douyin_media",
        lambda data, url, *, should_cancel: {
            "width": 1080,
            "height": 1920,
            "duration": 200,
        },
    )
    with pytest.raises(RuntimeError, match="duration did not match"):
        engine._probe_douyin_candidate(
            ProbeYoutubeDL(),
            "https://api-play.amemv.com/aweme/v1/play/" "?video_id=fixture&ratio=4k",
            expected_duration=72.8,
            should_cancel=lambda: False,
        )


def test_douyin_probe_rejects_untrusted_final_redirect_before_ffprobe(
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
    monkeypatch.setattr(
        engine,
        "_ffprobe_douyin_media",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Untrusted redirect must not reach FFprobe")
        ),
    )

    with pytest.raises(RuntimeError, match="trusted Douyin media hosts"):
        engine._probe_douyin_candidate(
            ydl,
            (
                "https://api-play.amemv.com/aweme/v1/play/"
                "?video_id=fixture&ratio=4k"
            ),
            expected_duration=72.8,
            should_cancel=lambda: False,
        )

    assert ydl.response.read_calls == 0
    assert ydl.response.closed is True


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

    result = engine._probe_douyin_candidate(
        ProbeYoutubeDL(),
        "https://api-play.amemv.com/aweme/v1/play/?video_id=fixture",
        expected_duration=10.0,
        should_cancel=lambda: False,
    )

    assert result is not None
    assert result["filesize"] == complete_size
    assert result["bit_rate"] == 8_000_000


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
        "default,4k,2k,1080p,720p: media endpoint network request failed"
    )
    assert "must-not-leak" not in info["_douyin_probe_failure"]


def test_douyin_official_probe_retries_transient_timeout(monkeypatch) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    info = _douyin_raw_info()
    attempts: dict[str, int] = {}
    delays = []
    messages = []

    def probe(ydl, url, *, expected_duration, should_cancel):
        ratio = parse_qs(urlsplit(url).query)["ratio"][0]
        attempts[ratio] = attempts.get(ratio, 0) + 1
        if ratio == "4k" and attempts[ratio] < 3:
            raise TimeoutError("temporary timeout")
        width, height = (720, 1280) if ratio == "720p" else (1080, 1920)
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
    assert attempts == {
        "default": 1,
        "4k": 3,
        "2k": 1,
        "1080p": 1,
        "720p": 1,
    }
    assert delays == [1.0, 2.0]
    assert sum(message.startswith("Retrying Douyin quality 4k") for message in messages) == 2


def test_douyin_probe_closes_retryable_http_error(monkeypatch) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    response = Response(
        BytesIO(),
        url="https://api-play.amemv.com/aweme/v1/play/",
        headers={},
        status=429,
        reason="Too Many Requests",
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
        }

    monkeypatch.setattr(engine, "_probe_douyin_ratio_with_retry", probe_ratio)

    selected = engine._select_highest_douyin_live_photo_asset(
        object(),
        asset,
        callback=None,
        should_cancel=lambda: False,
    )

    assert ratios == ["default", "4k", "2k", "1080p", "720p"]
    assert (selected.width, selected.height) == (1080, 1920)
    assert selected.size == 2_982_056
    assert selected.candidates[0].endswith("/default-final.mp4")
    assert parse_qs(urlsplit(selected.candidates[1]).query)["ratio"] == [
        "default"
    ]
    assert selected.format_id == (
        "douyin-highest-live-photo-default-1080x1920"
    )


def test_douyin_live_photo_fails_closed_when_one_ratio_cannot_be_verified(
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

    def probe_ratio(ydl, url, *, ratio, **kwargs):
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

    with pytest.raises(MediaDownloadError, match="highest quality"):
        engine._select_highest_douyin_live_photo_asset(
            object(),
            asset,
            callback=None,
            should_cancel=lambda: False,
        )


def test_douyin_partial_probe_success_never_allows_720p_fallback(monkeypatch) -> None:
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
    assert info["_douyin_probe_failure"].startswith(
        "default,4k,2k,1080p: media request or FFprobe timed out"
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
            "https://cdn.example.com/video.mp4",
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


def test_douyin_ffprobe_uses_short_independent_timeouts(monkeypatch) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    calls = []
    monkeypatch.setattr("app.downloader.shutil.which", lambda name: "/bin/ffprobe")

    def run(command, *, input_data=None, timeout_seconds, should_cancel):
        calls.append((command, input_data, timeout_seconds))
        return None

    monkeypatch.setattr(engine, "_run_ffprobe", run)

    assert (
        engine._ffprobe_douyin_media(
            b"fixture",
            "https://cdn.example.com/video.mp4",
            should_cancel=lambda: False,
        )
        is None
    )
    assert [call[2] for call in calls] == [3.0, 15.0]
    assert "15000000" in calls[1][0]


def test_douyin_ffprobe_prefix_timeout_continues_to_remote(monkeypatch) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    calls = []
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

    result = engine._ffprobe_douyin_media(
        b"fixture",
        "https://cdn.example.com/video.mp4",
        should_cancel=lambda: False,
    )

    assert result is not None
    assert (result["width"], result["height"]) == (1080, 1920)
    assert [call[2] for call in calls] == [3.0, 15.0]


def test_douyin_ffprobe_uses_remote_range_seek_when_moov_is_at_tail(
    monkeypatch,
) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    calls = []
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

    result = engine._ffprobe_douyin_media(
        b"prefix-without-tail-moov",
        "https://cdn.example.com/default-master.mp4",
        should_cancel=lambda: False,
    )

    assert result is not None
    assert (result["width"], result["height"]) == (1440, 2560)
    assert result["vcodec"] == "hevc"
    assert [call[2] for call in calls] == [3.0, 15.0]
    assert calls[0][1] == b"prefix-without-tail-moov"
    assert calls[1][1] is None
    assert "https://cdn.example.com/default-master.mp4" in calls[1][0]


def test_douyin_ffprobe_uses_remote_when_prefix_has_no_dimensions(
    monkeypatch,
) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    calls = []
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

    result = engine._ffprobe_douyin_media(
        b"prefix-without-dimensions",
        "https://cdn.example.com/default-master.mp4",
        should_cancel=lambda: False,
    )

    assert result is not None
    assert (result["width"], result["height"]) == (1440, 2560)
    assert [call[2] for call in calls] == [3.0, 15.0]


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
    final_url: str = "https://v26-web.douyinvod.com/final.mp4",
):
    class Headers:
        def get(self, name: str, default=None):
            values = {
                "Content-Type": "video/mp4",
                "Content-Length": str(len(payload)),
            }
            return values.get(name, default)

    class AssetResponse:
        headers = Headers()

        def __init__(self) -> None:
            self.url = final_url
            self.offset = 0
            self.closed = False

        def read(self, size: int) -> bytes:
            chunk = payload[self.offset : self.offset + size]
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
            response = AssetResponse()
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
                "url": "https://v26-web.douyinvod.com/verified.mp4",
                "width": 1440,
                "height": 2560,
                "tbr": 10_000,
                "filesize": len(payload),
            }
        )
        return True

    monkeypatch.setattr(engine, "_add_douyin_probe_formats", add_verified_format)
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
    ("width", "height", "bit_rate", "error_match"),
    [
        (720, 1280, 10_000_000, "below its declared"),
        (1440, 2560, 1_000_000, "bitrate was below"),
    ],
)
def test_douyin_verified_transfer_rejects_lower_final_media_without_overwrite(
    monkeypatch,
    tmp_path,
    width,
    height,
    bit_rate,
    error_match,
) -> None:
    media_id = "7664225419386607205"
    payload = b"\x00\x00\x00\x18ftypisom" + b"verified-original-bytes"
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
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

    with pytest.raises(MediaDownloadError, match=error_match):
        engine.download_item(item, Platform.DOUYIN, tmp_path)

    assert expected.read_bytes() == b"existing-good-file"
    assert direct_ydl.responses[0].closed is True
    assert not list(tmp_path.glob("*.part"))
    assert not (tmp_path / ".parts").exists()


def test_douyin_verified_transfer_preserves_exact_bytes_and_reports_resolution(
    monkeypatch,
    tmp_path,
) -> None:
    media_id = "7664225419386607205"
    payload = b"\x00\x00\x00\x18ftypisom" + b"verified-original-bytes"
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
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
        "https://v26-web.douyinvod.com/verified.mp4"
    )
    assert not list(tmp_path.glob("*.part"))
    assert not (tmp_path / ".parts").exists()


def test_douyin_verified_transfer_blocks_untrusted_final_redirect(
    monkeypatch,
    tmp_path,
) -> None:
    media_id = "7664225419386607205"
    payload = b"\x00\x00\x00\x18ftypisom" + b"private-response"
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
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

    with pytest.raises(MediaDownloadError, match="untrusted URL"):
        engine.download_item(item, Platform.DOUYIN, tmp_path)

    assert direct_ydl.responses[0].offset == 0
    assert direct_ydl.responses[0].closed is True
    assert not list(tmp_path.iterdir())


def test_douyin_verified_transfer_requires_size_or_bitrate_fingerprint(
    monkeypatch,
    tmp_path,
) -> None:
    media_id = "7664225419386607205"
    payload = b"\x00\x00\x00\x18ftypisom" + b"unfingerprinted-bytes"
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
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

    with pytest.raises(MediaDownloadError, match="duration did not match"):
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


def test_douyin_direct_transfer_does_not_follow_predictable_temp_symlink(
    monkeypatch,
    tmp_path,
) -> None:
    media_id = "7664225419386607205"
    payload = b"\x00\x00\x00\x18ftypisom" + b"verified-original-bytes"
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
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
            callback=None,
            should_cancel=lambda: False,
            asset_index=1,
        )

    assert ydl.response.read_calls == 0
    assert ydl.response.closed is True
    assert not list(tmp_path.iterdir())


def test_highest_asset_stream_preserves_bytes_and_reports_aggregate_progress(
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
        callback=None,
        should_cancel=lambda: False,
        asset_index=1,
        verify_declared_dimensions=True,
    )

    assert path.read_bytes() == payloads[1]
    assert (chosen.width, chosen.height) == (1080, 1920)


def test_highest_asset_stream_closes_http_error_response(tmp_path) -> None:
    response = Response(
        BytesIO(),
        url="https://v26-web.douyinvod.com/unavailable.mp4",
        headers={},
        status=503,
        reason="Service Unavailable",
    )
    error = HTTPError(response)

    class AssetYoutubeDL:
        def urlopen(self, request):
            raise error

    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    with pytest.raises(MediaDownloadError, match="highest-available"):
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
            callback=None,
            should_cancel=lambda: False,
            asset_index=1,
            verify_declared_dimensions=True,
        )

    assert response.closed is True
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    ("ffprobe_payload", "error_match"),
    [
        (b'{"streams": [], "format": {}}', "dimensions could not be verified"),
        (
            b'{"streams": [{"codec_type": "video", "width": 720, '
            b'"height": 1280}], "format": {"duration": "2.0"}}',
            "below its declared",
        ),
    ],
)
def test_live_photo_local_verification_rejects_invalid_or_lower_media(
    monkeypatch,
    tmp_path,
    ffprobe_payload,
    error_match,
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

    response = AssetResponse()

    class AssetYoutubeDL:
        def urlopen(self, request):
            return response

    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
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

    with pytest.raises(MediaDownloadError, match=error_match):
        engine._download_first_available_asset(
            AssetYoutubeDL(),
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
            callback=None,
            should_cancel=lambda: False,
            asset_index=1,
            verify_declared_dimensions=True,
        )

    assert response.closed is True
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
