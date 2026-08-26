from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadCancelled, DownloadError

from app.downloader import (
    OUTPUT_TEMPLATE,
    DownloaderConfig,
    MediaDownloader,
    _SafeFilenamePostProcessor,
    _item_key,
    safe_component,
)
from app.douyin import DouyinProfile
from app.errors import AuthenticationRequiredError, MediaDownloadError
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
                    "media_id": "1111111111111111111",
                    "owner_id": (
                        "MS4wLjABAAAAyjrP-yPP2JYTBFC6qw6lsg-7EU6jI-" "UJFhhJqludJSo"
                    ),
                    "video_uri": "v0200fg10000fixturevideoid",
                    "title": "Cached title",
                }
            },
        ),
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
    assert result.items[0].metadata["douyin_profile_media"]["video_uri"] == (
        "v0200fg10000fixturevideoid"
    )


def test_douyin_item_discovery_rejects_crosswired_extractor_id(
    monkeypatch,
) -> None:
    source_url = "https://www.douyin.com/video/1111111111111111111"

    class CrosswiredDiscoveryYoutubeDL(FakeYoutubeDL):
        def extract_info(self, url: str, download: bool):
            return {
                "id": "2222222222222222222",
                "title": "Wrong video",
                "formats": [],
            }

    monkeypatch.setattr("app.downloader.YoutubeDL", CrosswiredDiscoveryYoutubeDL)
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))

    with pytest.raises(AuthenticationRequiredError) as error:
        engine.discover(source_url, Platform.DOUYIN, SourceKind.ITEM)

    assert error.value.verification_url == source_url


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

    with pytest.raises(AuthenticationRequiredError) as error:
        engine.download_item(item, Platform.DOUYIN, tmp_path)

    assert error.value.verification_url == profile_url


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

    with pytest.raises(AuthenticationRequiredError) as error:
        engine.download_item(item, Platform.DOUYIN, tmp_path)

    assert error.value.verification_url == profile_url


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

    with pytest.raises(AuthenticationRequiredError) as error:
        engine.download_item(item, Platform.DOUYIN, tmp_path)

    assert error.value.verification_url == profile_url
    assert "different author" in str(error.value)


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
    assert parse_qs(urlsplit(result["formats"][0]["url"]).query)["video_id"] == [
        video_uri
    ]


def test_douyin_profile_metadata_rejects_wrong_cached_owner() -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser="chrome"))

    with pytest.raises(AuthenticationRequiredError, match="different author"):
        engine._douyin_raw_info_from_profile_metadata(
            {
                "profile_owner_verified": True,
                "douyin_profile_media": {
                    "media_id": "2222222222222222222",
                    "owner_id": "profile-b",
                    "video_uri": "v0200fg10000fixturevideoid",
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
        if ratio != "default":
            return None
        return {
            "url": url,
            "width": 1440,
            "height": 2560,
            "bit_rate": 20_132_350,
            "filesize": 59_093_472,
            "duration": expected_duration,
            "vcodec": "h265",
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


def test_douyin_unplayable_4k_probe_does_not_override_playable_2k(
    monkeypatch,
) -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    info = _douyin_raw_info()

    def probe(ydl, url, *, expected_duration, should_cancel):
        ratio = parse_qs(urlsplit(url).query)["ratio"][0]
        if ratio == "4k":
            return {
                "url": url,
                "width": 2160,
                "height": 3840,
                "bit_rate": 4_000_000,
                "filesize": 40_000_000,
                "duration": expected_duration,
                "vcodec": "vvc",
                "acodec": "aac",
            }
        if ratio == "2k":
            return {
                "url": url,
                "width": 1440,
                "height": 2560,
                "bit_rate": 2_000_000,
                "filesize": 20_000_000,
                "duration": expected_duration,
                "vcodec": "h265",
                "acodec": "aac",
            }
        return None

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

    assert selected["format_id"].startswith("douyin-api-1440x2560")
    assert selected["vcodec"] == "h265"


def test_douyin_uri_extraction_rejects_crosswired_or_ambiguous_media() -> None:
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    verification_url = "https://www.douyin.com/user/expected-profile"
    info = _douyin_raw_info()

    with pytest.raises(AuthenticationRequiredError):
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
    with pytest.raises(AuthenticationRequiredError, match="multiple media identities"):
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
        url = "https://cdn.example.com/verified-video.mp4"

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
    assert result["url"] == "https://cdn.example.com/verified-video.mp4"
    assert probed_urls == ["https://cdn.example.com/verified-video.mp4"]
    assert "cookie" not in request_headers
    assert request_headers["range"] == "bytes=0-262143"
    assert ydl.request.extensions["timeout"] == 6.0
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
        def extract_info(self, url: str, download: bool):
            if url == "https://space.bilibili.com/946974/video":
                return {
                    "id": "946974",
                    "entries": [
                        {
                            "id": "BV1rp4y1e745",
                            "title": "Demo video",
                            "url": "https://www.bilibili.com/video/BV1rp4y1e745",
                        }
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

        def process_ie_result(self, info, download: bool):
            output_file = (
                Path(self.options["paths"]["home"])
                / f"2025-11-14-Douyin video [{media_id}].mp4"
            )
            output_file.write_bytes(b"media")
            self.options["post_hooks"][0](str(output_file))
            return {
                **info,
                "requested_downloads": [{"filepath": str(output_file)}],
                "format_id": "douyin-api-1080x1920-1",
                "width": 1080,
                "height": 1920,
            }

    SuccessfulDouyinYoutubeDL.created_options.clear()
    monkeypatch.setattr("app.downloader.YoutubeDL", SuccessfulDouyinYoutubeDL)
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    monkeypatch.setattr(
        engine,
        "_add_douyin_probe_formats",
        lambda *args, **kwargs: False,
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

    with pytest.raises(AuthenticationRequiredError, match="Fully quit Chrome") as error:
        engine.discover(
            "https://www.youtube.com/@Example/videos",
            Platform.YOUTUBE,
            SourceKind.PROFILE,
        )

    assert error.value.verification_url == "https://www.youtube.com/@Example/videos"


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
        [RemoteAsset(candidates=["https://cdn.example/original"], index=1)],
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

    def download_asset(*args, media_type, **kwargs):
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
