from __future__ import annotations

from pathlib import Path

import pytest
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from app.downloader import (
    OUTPUT_TEMPLATE,
    DownloaderConfig,
    MediaDownloader,
    _SafeFilenamePostProcessor,
    _item_key,
    safe_component,
)
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
