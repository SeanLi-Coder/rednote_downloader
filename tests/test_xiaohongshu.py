from __future__ import annotations

import json
from datetime import datetime, timezone
from http.cookiejar import CookieJar

import pytest
from yt_dlp.utils import DownloadError

from app.errors import (
    AuthenticationRequiredError,
    DiscoveryError,
    TemporaryAccessError,
)
from app.xiaohongshu import (
    _image_candidates,
    _is_explicit_xiaohongshu_auth_url,
    _live_photo_asset,
    _looks_like_auth_page,
    _looks_like_transient_limit,
    _upload_date,
    _video_assets,
    discover_profile,
    is_trusted_xiaohongshu_asset_url,
    is_trusted_xiaohongshu_note_url,
    parse_note,
)


NOTE_ID = "6411cf99000000001300b6d9"
NOTE_URL = f"https://www.xiaohongshu.com/explore/{NOTE_ID}"


def make_html() -> str:
    note = {
        "title": "Original title",
        "time": 1_700_000_000_000,
        "user": {
            "nickname": "Test Author",
            "userId": "5c99d4b30000000011015e6d",
        },
        "imageList": [
            {
                "width": 3000,
                "height": 4000,
                "infoList": [
                    {
                        "imageScene": "WB_PRV",
                        "url": "https://sns-webpic-qc.xhscdn.com/preview.jpg!preview",
                    },
                    {
                        "imageScene": "WB_ORIGINAL",
                        "url": "https://sns-webpic-qc.xhscdn.com/original.jpg!transform",
                    },
                ],
                "stream": {
                    "h265": [
                        {
                            "masterUrl": "https://sns-video-bd.xhscdn.com/live-photo.mp4",
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
                            "masterUrl": "https://sns-video-bd.xhscdn.com/1080.mp4",
                            "width": 1920,
                            "height": 1080,
                            "avgBitrate": 8_000_000,
                            "size": 20_000_000,
                            "qualityType": "HD",
                        },
                        {
                            "masterUrl": "https://sns-video-bd.xhscdn.com/720.mp4",
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
    def __init__(self, body: str, *, url: str | None = None) -> None:
        self.body = body
        self.url = url
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
    assert note.author_id == "5c99d4b30000000011015e6d"
    assert note.upload_date == "2023-11-15"
    assert note.images[0].candidates[0] == (
        "https://sns-webpic-qc.xhscdn.com/original.jpg"
    )
    assert note.images[0].candidates[-1].endswith("preview.jpg!preview")
    assert note.videos[0].format_id == "original"
    assert note.videos[0].candidates == [
        "https://sns-video-bd.xhscdn.com/original/video.mp4"
    ]
    assert note.videos[1].height == 1080
    assert note.videos[2].height == 720
    assert note.live_photos[0].candidates == [
        "https://sns-video-bd.xhscdn.com/live-photo.mp4"
    ]
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

    with pytest.raises(TemporaryAccessError, match="Fully quit Chrome"):
        parse_note(NOTE_URL)


def test_parse_note_reports_verification_page(monkeypatch) -> None:
    class AuthPageYoutubeDL(FakeYoutubeDL):
        html = "<html><body>请完成验证</body></html>"

    monkeypatch.setattr("app.xiaohongshu.YoutubeDL", AuthPageYoutubeDL)

    with pytest.raises(AuthenticationRequiredError, match="CAPTCHA"):
        parse_note(NOTE_URL)


def test_parse_note_rejects_untrusted_final_redirect_before_read(monkeypatch) -> None:
    class RedirectedResponse(FakeResponse):
        def __init__(self) -> None:
            super().__init__(make_html(), url="http://127.0.0.1/private")
            self.read_calls = 0
            self.closed = False

        def read(self) -> bytes:
            self.read_calls += 1
            return super().read()

        def close(self) -> None:
            self.closed = True

    class RedirectingYoutubeDL(FakeYoutubeDL):
        response = RedirectedResponse()

        def urlopen(self, request) -> FakeResponse:
            return self.response

    monkeypatch.setattr("app.xiaohongshu.YoutubeDL", RedirectingYoutubeDL)

    with pytest.raises(DiscoveryError, match="redirected outside"):
        parse_note(NOTE_URL)

    assert RedirectingYoutubeDL.response.read_calls == 0
    assert RedirectingYoutubeDL.response.closed is True


def test_xiaohongshu_rate_limit_and_hidden_script_are_not_authentication() -> None:
    assert _looks_like_transient_limit("当前访问频繁，请稍后再试")
    assert not _looks_like_auth_page("当前访问频繁，请稍后再试")
    assert not _looks_like_auth_page(
        '<script>const route = "captcha";</script><main>正常主页</main>'
    )
    assert _looks_like_auth_page("<main>请完成验证</main>")
    mixed_page = "<button>请登录</button><main>当前访问频繁，请稍后再试</main>"
    assert _looks_like_auth_page(mixed_page)
    assert _looks_like_transient_limit(mixed_page)


def test_xiaohongshu_auth_url_requires_trusted_origin() -> None:
    assert _is_explicit_xiaohongshu_auth_url(
        "https://www.xiaohongshu.com/login"
    )
    assert not _is_explicit_xiaohongshu_auth_url(
        "https://www.xiaohongshu.com.evil.example/login"
    )


def test_xiaohongshu_note_and_asset_urls_require_trusted_https_origins() -> None:
    assert is_trusted_xiaohongshu_note_url(NOTE_URL)
    assert not is_trusted_xiaohongshu_note_url(
        f"https://evil.example/explore/{NOTE_ID}"
    )
    assert not is_trusted_xiaohongshu_note_url(
        f"http://www.xiaohongshu.com/explore/{NOTE_ID}"
    )
    assert is_trusted_xiaohongshu_asset_url(
        "https://sns-video-bd.xhscdn.com/original.mp4"
    )
    assert is_trusted_xiaohongshu_asset_url(
        "https://ci.xiaohongshu.com/original.jpg"
    )
    assert not is_trusted_xiaohongshu_asset_url(
        "https://xhscdn.com.evil.example/original.mp4"
    )
    assert not is_trusted_xiaohongshu_asset_url(
        "https://127.0.0.1/private"
    )


def test_parse_note_blocks_untrusted_origin_before_network(monkeypatch) -> None:
    class UnexpectedYoutubeDL:
        def __init__(self, options: dict) -> None:
            raise AssertionError("Untrusted URL must not reach the network")

    monkeypatch.setattr("app.xiaohongshu.YoutubeDL", UnexpectedYoutubeDL)

    with pytest.raises(DiscoveryError, match="untrusted"):
        parse_note(f"https://evil.example/explore/{NOTE_ID}")


def test_xiaohongshu_asset_extractors_drop_untrusted_urls() -> None:
    assert _image_candidates(
        {
            "url": "https://evil.example/image.jpg",
            "infoList": [
                {
                    "imageScene": "WB_ORIGINAL",
                    "url": "https://127.0.0.1/private.jpg",
                }
            ],
        }
    ) == []
    assert _video_assets(
        {
            "media": {
                "stream": {
                    "h264": [
                        {"masterUrl": "https://evil.example/video.mp4"}
                    ]
                }
            }
        }
    ) == []
    assert _live_photo_asset(
        {
            "stream": {
                "h265": [
                    {"masterUrl": "https://127.0.0.1/live-photo.mp4"}
                ]
            }
        },
        1,
    ) is None


def test_parse_note_ignores_auth_words_inside_hidden_script(monkeypatch) -> None:
    class HiddenAuthScriptYoutubeDL(FakeYoutubeDL):
        html = '<script>const route = "captcha";</script>' + make_html()

    monkeypatch.setattr(
        "app.xiaohongshu.YoutubeDL",
        HiddenAuthScriptYoutubeDL,
    )

    note, _ = parse_note(NOTE_URL)

    assert note.note_id == NOTE_ID


def test_parse_note_rate_limit_is_temporary_not_authentication(monkeypatch) -> None:
    class LimitedYoutubeDL(FakeYoutubeDL):
        html = "<html><body>当前访问频繁，请稍后再试</body></html>"

    monkeypatch.setattr("app.xiaohongshu.YoutubeDL", LimitedYoutubeDL)

    with pytest.raises(
        TemporaryAccessError,
        match="Chrome verification is not required",
    ):
        parse_note(NOTE_URL)


def test_parse_note_mixed_login_navigation_and_rate_limit_is_temporary(
    monkeypatch,
) -> None:
    class MixedLimitedYoutubeDL(FakeYoutubeDL):
        html = (
            "<html><body><button>请登录</button>"
            "<main>当前访问频繁，请稍后再试</main></body></html>"
        )

    monkeypatch.setattr("app.xiaohongshu.YoutubeDL", MixedLimitedYoutubeDL)

    with pytest.raises(
        TemporaryAccessError,
        match="Chrome verification is not required",
    ):
        parse_note(NOTE_URL)


def test_parse_note_stale_xsec_token_requires_rediscovery_not_authentication(
    monkeypatch,
) -> None:
    class StaleTokenYoutubeDL(FakeYoutubeDL):
        html = (
            "<script>window.__INITIAL_STATE__ = "
            + json.dumps({"note": {"noteDetailMap": {}}})
            + ";</script>"
        )

    monkeypatch.setattr("app.xiaohongshu.YoutubeDL", StaleTokenYoutubeDL)

    with pytest.raises(
        TemporaryAccessError,
        match="Chrome verification is not required",
    ):
        parse_note(f"{NOTE_URL}?xsec_token=stale&xsec_source=pc_user")


def test_parse_note_stale_xsec_token_retries_canonical_note_url(
    monkeypatch,
) -> None:
    empty_state = (
        "<script>window.__INITIAL_STATE__ = "
        + json.dumps({"note": {"noteDetailMap": {}}})
        + ";</script>"
    )

    class RefreshingTokenYoutubeDL(FakeYoutubeDL):
        requested_urls: list[str] = []

        def urlopen(self, request) -> FakeResponse:
            self.requested_urls.append(request.url)
            return FakeResponse(
                empty_state if "xsec_token=" in request.url else make_html()
            )

    monkeypatch.setattr("app.xiaohongshu.YoutubeDL", RefreshingTokenYoutubeDL)

    note, _ = parse_note(
        f"{NOTE_URL}?xsec_token=stale&xsec_source=pc_user"
    )

    assert note.note_id == NOTE_ID
    assert len(RefreshingTokenYoutubeDL.requested_urls) == 2
    assert "xsec_token=stale" in RefreshingTokenYoutubeDL.requested_urls[0]
    assert RefreshingTokenYoutubeDL.requested_urls[1] == NOTE_URL


def test_xiaohongshu_upload_date_uses_fixed_utc_plus_eight_boundary() -> None:
    before_boundary = int(
        datetime(2025, 8, 31, 15, 59, 59, tzinfo=timezone.utc).timestamp()
    )
    after_boundary = int(
        datetime(2025, 8, 31, 16, 0, 0, tzinfo=timezone.utc).timestamp()
    )

    assert _upload_date({"time": before_boundary * 1_000}) == "2025-08-31"
    assert _upload_date({"time": after_boundary * 1_000}) == "2025-09-01"


def test_xiaohongshu_browser_timeout_is_temporary_not_authentication(
    monkeypatch,
) -> None:
    from playwright.sync_api import Error as PlaywrightError

    profile_url = "https://www.xiaohongshu.com/user/profile/expected"

    class TimeoutPlaywrightContext:
        def __enter__(self):
            raise PlaywrightError("Timeout 45000ms exceeded while navigating")

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

    monkeypatch.setattr(
        "app.xiaohongshu._extract_chrome_cookies",
        lambda profile: CookieJar(),
    )
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: TimeoutPlaywrightContext(),
    )

    with pytest.raises(
        TemporaryAccessError,
        match="Chrome verification is not required",
    ):
        discover_profile(profile_url)


def test_xiaohongshu_profile_mixed_login_and_rate_limit_is_temporary(
    monkeypatch,
) -> None:
    profile_url = "https://www.xiaohongshu.com/user/profile/expected"

    class FakeBody:
        def count(self) -> int:
            return 1

        def inner_text(self, timeout: int) -> str:
            return "请登录 当前访问频繁，请稍后再试"

    class FakePage:
        url = profile_url

        def goto(self, url: str, wait_until: str, timeout: int) -> None:
            self.url = url

        def wait_for_timeout(self, timeout: int) -> None:
            return None

        def locator(self, selector: str) -> FakeBody:
            return FakeBody()

    class FakeContext:
        def new_page(self) -> FakePage:
            return FakePage()

    class FakeBrowser:
        version = "151.0.0.0"

        def new_context(self, **kwargs) -> FakeContext:
            return FakeContext()

        def close(self) -> None:
            return None

    class FakeChromium:
        def launch(self, **kwargs) -> FakeBrowser:
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

    class FakePlaywrightContext:
        def __enter__(self) -> FakePlaywright:
            return FakePlaywright()

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

    monkeypatch.setattr(
        "app.xiaohongshu._extract_chrome_cookies",
        lambda profile: CookieJar(),
    )
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: FakePlaywrightContext(),
    )

    with pytest.raises(
        TemporaryAccessError,
        match="Chrome verification is not required",
    ):
        discover_profile(profile_url)


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
