from __future__ import annotations

import json

import pytest
from yt_dlp.utils import DownloadError

from app.errors import AuthenticationRequiredError
from app.xiaohongshu import _image_candidates, parse_note


NOTE_ID = "6411cf99000000001300b6d9"
NOTE_URL = f"https://www.xiaohongshu.com/explore/{NOTE_ID}"


def make_html() -> str:
    note = {
        "title": "Original title",
        "time": 1_700_000_000_000,
        "user": {"nickname": "Test Author"},
        "imageList": [
            {
                "width": 3000,
                "height": 4000,
                "infoList": [
                    {
                        "imageScene": "WB_PRV",
                        "url": "https://sns-webpic.example/preview.jpg!preview",
                    },
                    {
                        "imageScene": "WB_ORIGINAL",
                        "url": "https://sns-webpic.example/original.jpg!transform",
                    },
                ],
                "stream": {
                    "h265": [
                        {
                            "masterUrl": "https://video.example/live-photo.mp4",
                            "width": 3000,
                            "height": 4000,
                            "avgBitrate": 6_000_000,
                            "size": 12_000_000,
                            "qualityType": "LIVE_HD",
                        }
                    ]
                },
            }
        ],
        "video": {
            "consumer": {"originVideoKey": "original/video.mp4"},
            "media": {
                "stream": {
                    "h264": [
                        {
                            "masterUrl": "https://video.example/1080.mp4",
                            "width": 1920,
                            "height": 1080,
                            "avgBitrate": 8_000_000,
                            "size": 20_000_000,
                            "qualityType": "HD",
                        },
                        {
                            "masterUrl": "https://video.example/720.mp4",
                            "width": 1280,
                            "height": 720,
                            "avgBitrate": 4_000_000,
                            "size": 10_000_000,
                            "qualityType": "SD",
                        },
                    ]
                }
            },
        },
    }
    state = {"note": {"noteDetailMap": {NOTE_ID: {"note": note}}}}
    return f"<script>window.__INITIAL_STATE__ = {json.dumps(state)};</script>"


class FakeHeaders:
    def get(self, name: str, default=None):
        if name.lower() == "content-type":
            return "text/html; charset=utf-8"
        return default

    def get_content_charset(self) -> str:
        return "utf-8"


class FakeResponse:
    def __init__(self, body: str) -> None:
        self.body = body
        self.headers = FakeHeaders()

    def read(self) -> bytes:
        return self.body.encode("utf-8")

    def close(self) -> None:
        pass


class FakeYoutubeDL:
    created_options: list[dict] = []
    html = make_html()

    def __init__(self, options: dict) -> None:
        self.options = options
        self.created_options.append(options)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def urlopen(self, request) -> FakeResponse:
        return FakeResponse(self.html)


def test_parse_note_uses_browser_cookies_and_keeps_original_assets(monkeypatch) -> None:
    FakeYoutubeDL.created_options.clear()
    monkeypatch.setattr("app.xiaohongshu.YoutubeDL", FakeYoutubeDL)

    note, fallback_used = parse_note(NOTE_URL)

    assert fallback_used is False
    assert FakeYoutubeDL.created_options[0]["cookiesfrombrowser"] == ("chrome",)
    assert note.note_id == NOTE_ID
    assert note.author == "Test Author"
    assert note.upload_date == "2023-11-15"
    assert note.images[0].candidates[0] == "https://sns-webpic.example/original.jpg"
    assert note.images[0].candidates[-1].endswith("preview.jpg!preview")
    assert note.videos[0].format_id == "original"
    assert note.videos[0].candidates == [
        "https://sns-video-bd.xhscdn.com/original/video.mp4"
    ]
    assert note.videos[1].height == 1080
    assert note.videos[2].height == 720
    assert note.live_photos[0].candidates == ["https://video.example/live-photo.mp4"]
    assert note.live_photos[0].format_id == "LIVE_HD"


def test_parse_note_respects_disabled_browser_cookies(monkeypatch) -> None:
    FakeYoutubeDL.created_options.clear()
    monkeypatch.setattr("app.xiaohongshu.YoutubeDL", FakeYoutubeDL)

    note, fallback_used = parse_note(NOTE_URL, use_browser_cookies=False)

    assert note.note_id == NOTE_ID
    assert fallback_used is False
    assert "cookiesfrombrowser" not in FakeYoutubeDL.created_options[0]


def test_parse_note_retries_without_cookies_when_browser_database_is_unavailable(
    monkeypatch,
) -> None:
    class CookieFallbackYoutubeDL(FakeYoutubeDL):
        created_options: list[dict] = []

        def __enter__(self):
            if "cookiesfrombrowser" in self.options:
                raise DownloadError("Could not copy Chrome cookie database")
            return self

    monkeypatch.setattr("app.xiaohongshu.YoutubeDL", CookieFallbackYoutubeDL)

    note, fallback_used = parse_note(NOTE_URL, allow_cookie_fallback=True)

    assert note.note_id == NOTE_ID
    assert fallback_used is True
    assert "cookiesfrombrowser" in CookieFallbackYoutubeDL.created_options[0]
    assert "cookiesfrombrowser" not in CookieFallbackYoutubeDL.created_options[1]


def test_parse_note_cookie_failure_requires_user_action_by_default(
    monkeypatch,
) -> None:
    class CookieErrorYoutubeDL(FakeYoutubeDL):
        def __enter__(self):
            raise DownloadError("Could not copy Chrome cookie database")

    monkeypatch.setattr("app.xiaohongshu.YoutubeDL", CookieErrorYoutubeDL)

    with pytest.raises(AuthenticationRequiredError, match="Fully quit Chrome"):
        parse_note(NOTE_URL)


def test_parse_note_reports_verification_page(monkeypatch) -> None:
    class AuthPageYoutubeDL(FakeYoutubeDL):
        html = "<html><body>请完成验证</body></html>"

    monkeypatch.setattr("app.xiaohongshu.YoutubeDL", AuthPageYoutubeDL)

    with pytest.raises(AuthenticationRequiredError, match="CAPTCHA"):
        parse_note(NOTE_URL)


def test_image_candidates_prioritize_direct_original_cdn() -> None:
    transformed = (
        "https://sns-webpic-qc.xhscdn.com/202511142359/abcdef/"
        "notes_pre_post/1040asset!nd_dft_wlteh_jpg_3"
    )

    candidates = _image_candidates(
        {
            "urlDefault": transformed,
            "infoList": [{"imageScene": "WB_DFT", "url": transformed}],
        }
    )

    assert candidates[:2] == [
        "https://sns-img-bd.xhscdn.com/notes_pre_post/1040asset",
        "https://ci.xiaohongshu.com/notes_pre_post/1040asset",
    ]
