from __future__ import annotations

import json
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from io import BytesIO

import pytest
from yt_dlp import YoutubeDL
from yt_dlp.networking import Request
from yt_dlp.utils import DownloadError

from app.errors import (
    AuthenticationRequiredError,
    DiscoveryError,
    SiteIssueCode,
    TemporaryAccessError,
)
from app.xiaohongshu import (
    XIAOHONGSHU_MAX_REDIRECTS,
    _XiaohongshuRedirectRejected,
    _collect_profile_note_urls,
    _image_candidates,
    _is_explicit_xiaohongshu_auth_url,
    _is_explicit_xiaohongshu_verification_url,
    _live_photo_asset,
    _looks_like_auth_page,
    _page_has_verification_challenge,
    _looks_like_transient_limit,
    _looks_like_verification_challenge,
    _open_xiaohongshu_response,
    _read_page,
    _upload_date,
    _video_assets,
    discover_profile,
    is_trusted_xiaohongshu_asset_url,
    is_trusted_xiaohongshu_note_url,
    parse_note,
    xiaohongshu_note_id,
)


NOTE_ID = "6411cf99000000001300b6d9"
NOTE_URL = f"https://www.xiaohongshu.com/explore/{NOTE_ID}"
_REAL_OPEN_XIAOHONGSHU_RESPONSE = _open_xiaohongshu_response


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


@pytest.fixture(autouse=True)
def _adapt_lightweight_xiaohongshu_ytdl_fakes(monkeypatch) -> None:
    """Keep legacy parser fakes while preserving fail-closed redirect semantics."""

    def open_response(
        ydl,
        request,
        *,
        is_trusted_url,
        max_redirects=XIAOHONGSHU_MAX_REDIRECTS,
    ):
        del max_redirects
        if not is_trusted_url(request.url):
            raise _XiaohongshuRedirectRejected(
                "untrusted-url",
                target_url=request.url,
            )
        response = ydl.urlopen(request)
        final_url = str(getattr(response, "url", None) or request.url)
        if not is_trusted_url(final_url):
            response.close()
            raise _XiaohongshuRedirectRejected(
                "untrusted-response-url",
                target_url=final_url,
            )
        return response

    monkeypatch.setattr(
        "app.xiaohongshu._open_xiaohongshu_response",
        open_response,
    )


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
        response = self.responses[len(self.requests) - 1]
        hook = (kwargs.get("hooks") or {}).get("response")
        if callable(hook):
            response = hook(response)
        return response


def _youtube_dl_with_requests_session(
    monkeypatch,
    session: _QueuedRequestsSession,
) -> YoutubeDL:
    ydl = YoutubeDL({"quiet": True, "proxy": ""})
    handler = ydl._request_director.handlers["Requests"]
    monkeypatch.setattr(
        handler,
        "_get_instance",
        lambda **kwargs: session,
    )
    return ydl


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


def _note_identity_html(identity: dict) -> str:
    note = {
        "title": "Identity fixture",
        "type": "video",
        "user": {"userId": "5c99d4b30000000011015e6d"},
        "video": {
            "media": {
                "stream": {
                    "h264": [
                        {
                            "masterUrl": "https://sns-video-bd.xhscdn.com/fixture.mp4",
                            "width": 1920,
                            "height": 1080,
                        }
                    ]
                }
            }
        },
        **identity,
    }
    state = {"note": {"noteDetailMap": {NOTE_ID: {"note": note}}}}
    return f"<script>window.__INITIAL_STATE__ = {json.dumps(state)};</script>"


@pytest.mark.parametrize("identity_key", ["noteId", "note_id", "id"])
@pytest.mark.parametrize(
    "payload_id",
    ["6411cf99000000001300b6d8", "", None, 123, {"id": NOTE_ID}],
)
def test_parse_note_rejects_conflicting_or_invalid_inner_identity(
    monkeypatch,
    identity_key,
    payload_id,
) -> None:
    monkeypatch.setattr(
        "app.xiaohongshu._read_page",
        lambda *args: _note_identity_html({identity_key: payload_id}),
    )

    with pytest.raises(DiscoveryError, match="note identity") as failure:
        parse_note(NOTE_URL, use_browser_cookies=False)

    assert failure.value.issue_code == SiteIssueCode.SITE_RESPONSE_CHANGED
    assert "Chrome verification is not required" in str(failure.value)


@pytest.mark.parametrize(
    "identity",
    [
        {},
        {"noteId": NOTE_ID},
        {"note_id": NOTE_ID.upper()},
        {"id": NOTE_ID},
        {"noteId": NOTE_ID, "note_id": NOTE_ID, "id": NOTE_ID},
    ],
)
def test_parse_note_keeps_matching_and_legacy_key_bound_payloads(
    monkeypatch,
    identity,
) -> None:
    monkeypatch.setattr(
        "app.xiaohongshu._read_page",
        lambda *args: _note_identity_html(identity),
    )

    note, _ = parse_note(
        f"https://www.xiaohongshu.com/explore/{NOTE_ID.upper()}",
        use_browser_cookies=False,
    )

    assert note.note_id == NOTE_ID
    assert note.videos


def test_parse_note_rejects_conflicting_alias_despite_matching_primary_id(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.xiaohongshu._read_page",
        lambda *args: _note_identity_html(
            {
                "noteId": NOTE_ID,
                "note_id": "6411cf99000000001300b6d8",
            }
        ),
    )

    with pytest.raises(DiscoveryError, match="note identity"):
        parse_note(NOTE_URL, use_browser_cookies=False)


def test_parse_note_token_refresh_preserves_identity_failure(monkeypatch) -> None:
    requested_urls = []

    def read_page(ydl, url):
        requested_urls.append(url)
        if "xsec_token=" in url:
            return (
                "<script>window.__INITIAL_STATE__ = "
                '{"note":{"noteDetailMap":{}}};</script>'
            )
        return _note_identity_html({"noteId": "6411cf99000000001300b6d8"})

    monkeypatch.setattr("app.xiaohongshu._read_page", read_page)

    with pytest.raises(DiscoveryError, match="note identity") as failure:
        parse_note(f"{NOTE_URL}?xsec_token=stale", use_browser_cookies=False)

    assert failure.value.issue_code == SiteIssueCode.SITE_RESPONSE_CHANGED
    assert len(requested_urls) == 2
    assert requested_urls[-1] == NOTE_URL


@pytest.mark.parametrize("stage", ["discover", "download"])
def test_downloader_blocks_inner_identity_mismatch_before_media_transfer(
    monkeypatch,
    tmp_path,
    stage,
) -> None:
    from app.downloader import DownloaderConfig, MediaDownloader
    from app.models import DownloadItem, MediaType
    from app.platforms import Platform, SourceKind

    monkeypatch.setattr(
        "app.xiaohongshu._read_page",
        lambda *args: _note_identity_html({"noteId": "6411cf99000000001300b6d8"}),
    )
    engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
    transfers = []
    monkeypatch.setattr(
        engine,
        "_download_first_available_asset",
        lambda *args, **kwargs: transfers.append(True),
    )
    item = DownloadItem(
        id=NOTE_ID,
        media_id=NOTE_ID,
        source_url=NOTE_URL,
        title="Expected work",
        media_type=MediaType.VIDEO,
        metadata={
            "xiaohongshu_profile_id": "5c99d4b30000000011015e6d",
            "profile_note_membership_verified": True,
        },
    )

    with pytest.raises(DiscoveryError, match="note identity"):
        if stage == "discover":
            engine.discover(NOTE_URL, Platform.XIAOHONGSHU, SourceKind.ITEM)
        else:
            engine.download_item(item, Platform.XIAOHONGSHU, tmp_path)

    assert not transfers
    assert not list(tmp_path.iterdir())


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

    with pytest.raises(AuthenticationRequiredError, match="verification challenge"):
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

    with pytest.raises(DiscoveryError, match="blocked before requesting"):
        parse_note(NOTE_URL)

    assert RedirectingYoutubeDL.response.read_calls == 0
    assert RedirectingYoutubeDL.response.closed is True


@pytest.mark.parametrize(
    "target_url",
    [
        "http://127.0.0.1:8080/private",
        "https://evil.example/private",
        f"https://www.xiaohongshu.com:8443/explore/{NOTE_ID}",
    ],
)
def test_note_redirect_rejects_untrusted_next_hop_before_request(
    monkeypatch,
    target_url: str,
) -> None:
    first_response = _FakeRequestsResponse(NOTE_URL, location=target_url)
    session = _QueuedRequestsSession([first_response])
    ydl = _youtube_dl_with_requests_session(monkeypatch, session)

    with ydl, pytest.raises(
        _XiaohongshuRedirectRejected,
        match="untrusted-target",
    ):
        _REAL_OPEN_XIAOHONGSHU_RESPONSE(
            ydl,
            Request(NOTE_URL),
            is_trusted_url=lambda value: xiaohongshu_note_id(value) == NOTE_ID,
        )

    assert [request["url"] for request in session.requests] == [NOTE_URL]
    assert target_url not in [request["url"] for request in session.requests]
    assert first_response.closed is True


def test_note_redirect_follows_only_trusted_https_hops(monkeypatch) -> None:
    redirected_url = (
        f"https://www.xiaohongshu.com/discovery/item/{NOTE_ID}?from=redirect"
    )
    first_response = _FakeRequestsResponse(NOTE_URL, location=redirected_url)
    final_response = _FakeRequestsResponse(
        redirected_url,
        payload=make_html().encode("utf-8"),
    )
    session = _QueuedRequestsSession([first_response, final_response])
    ydl = _youtube_dl_with_requests_session(monkeypatch, session)

    with ydl:
        response = _REAL_OPEN_XIAOHONGSHU_RESPONSE(
            ydl,
            Request(NOTE_URL),
            is_trusted_url=lambda value: xiaohongshu_note_id(value) == NOTE_ID,
        )
        try:
            assert response.read() == make_html().encode("utf-8")
        finally:
            response.close()

    assert [request["url"] for request in session.requests] == [
        NOTE_URL,
        redirected_url,
    ]
    assert all(request["allow_redirects"] is False for request in session.requests)
    assert first_response.closed is True


def test_note_redirect_chain_is_bounded_before_extra_request(monkeypatch) -> None:
    max_redirects = 2
    urls = [f"{NOTE_URL}?hop={index}" for index in range(max_redirects + 2)]
    responses = [
        _FakeRequestsResponse(urls[index], location=urls[index + 1])
        for index in range(max_redirects + 1)
    ]
    session = _QueuedRequestsSession(responses)
    ydl = _youtube_dl_with_requests_session(monkeypatch, session)

    with ydl, pytest.raises(
        _XiaohongshuRedirectRejected,
        match="too-many-redirects",
    ):
        _REAL_OPEN_XIAOHONGSHU_RESPONSE(
            ydl,
            Request(urls[0]),
            is_trusted_url=lambda value: xiaohongshu_note_id(value) == NOTE_ID,
            max_redirects=max_redirects,
        )

    assert [request["url"] for request in session.requests] == urls[:-1]
    assert urls[-1] not in [request["url"] for request in session.requests]
    assert all(response.closed for response in responses)


def test_note_captcha_redirect_is_classified_without_requesting_challenge(
    monkeypatch,
) -> None:
    captcha_url = (
        "https://www.xiaohongshu.com/website-login/captcha"
        f"?redirectPath=%2Fexplore%2F{NOTE_ID}"
    )
    first_response = _FakeRequestsResponse(NOTE_URL, location=captcha_url)
    session = _QueuedRequestsSession([first_response])
    ydl = _youtube_dl_with_requests_session(monkeypatch, session)
    monkeypatch.setattr(
        "app.xiaohongshu._open_xiaohongshu_response",
        _REAL_OPEN_XIAOHONGSHU_RESPONSE,
    )

    with ydl, pytest.raises(AuthenticationRequiredError) as exc_info:
        _read_page(ydl, NOTE_URL)

    assert exc_info.value.verification_url == NOTE_URL
    assert [request["url"] for request in session.requests] == [NOTE_URL]
    assert captcha_url not in [request["url"] for request in session.requests]
    assert first_response.closed is True


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
    assert not _looks_like_verification_challenge(
        "<aside>手机号登录 获取验证码</aside>"
    )
    assert _looks_like_verification_challenge("<main>请完成验证</main>")


def test_structured_challenge_detection_does_not_scan_normal_page_text() -> None:
    class FakePage:
        url = "https://www.xiaohongshu.com/user/profile/expected"

        def evaluate(self, script: str) -> bool:
            assert "document.querySelectorAll" in script
            return False

    assert not _page_has_verification_challenge(FakePage())


def test_profile_dom_recommendation_does_not_establish_membership() -> None:
    class FakeLocator:
        def evaluate_all(self, script: str) -> list[str]:
            return [NOTE_URL]

    class FakePage:
        url = "https://www.xiaohongshu.com/user/profile/expected"

        def evaluate(self, script: str) -> list[dict[str, str]]:
            return []

        def locator(self, selector: str) -> FakeLocator:
            return FakeLocator()

    discovered: dict[str, str] = {}
    _collect_profile_note_urls(FakePage(), discovered)

    assert discovered == {}


def test_xiaohongshu_auth_url_requires_trusted_origin() -> None:
    assert _is_explicit_xiaohongshu_auth_url(
        "https://www.xiaohongshu.com/login"
    )
    assert not _is_explicit_xiaohongshu_auth_url(
        "https://www.xiaohongshu.com.evil.example/login"
    )
    assert _is_explicit_xiaohongshu_verification_url(
        "https://www.xiaohongshu.com/website-login/captcha?redirectPath=%2Fexplore"
    )
    assert not _is_explicit_xiaohongshu_verification_url(
        "https://www.xiaohongshu.com/login"
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
    assert not is_trusted_xiaohongshu_asset_url(
        "http://sns-video-bd.xhscdn.com/original.mp4"
    )
    assert not is_trusted_xiaohongshu_asset_url(
        "https://sns-video-bd.xhscdn.com:8443/original.mp4"
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


def test_live_photo_stream_does_not_inherit_still_image_dimensions() -> None:
    asset = _live_photo_asset(
        {
            "width": 4283,
            "height": 5711,
            "stream": {
                "h264": [
                    {
                        "masterUrl": (
                            "http://sns-video-bd.xhscdn.com/live-photo.mp4"
                        ),
                        "backupUrls": (
                            "http://sns-video-qc.xhscdn.com/live-photo.mp4"
                        ),
                        "avgBitrate": 2_000_000,
                        "qualityType": "LIVE_HD",
                    }
                ]
            },
        },
        1,
    )

    assert asset is not None
    assert asset.width is None
    assert asset.height is None
    assert asset.bit_rate == 2_000_000
    assert asset.candidates == [
        "https://sns-video-bd.xhscdn.com/live-photo.mp4",
        "https://sns-video-qc.xhscdn.com/live-photo.mp4",
    ]


def test_video_assets_upgrade_only_trusted_standard_http_xhscdn_urls() -> None:
    note = {
        "video": {
            "media": {
                "stream": {
                    "h264": [
                        {
                            "masterUrl": (
                                "http://sns-video-zl.xhscdn.com/video.mp4?token=test"
                            ),
                            "backupUrls": [
                                "http://sns-bak-v8.xhscdn.com:80/video.mp4",
                                "http://user:pass@sns-bak-v8.xhscdn.com/private.mp4",
                                "http://sns-bak-v8.xhscdn.com:81/private.mp4",
                                "http://xhscdn.com.evil.example/private.mp4",
                                "http://www.xiaohongshu.com/private.mp4",
                            ],
                            "width": 720,
                            "height": 1608,
                        }
                    ]
                }
            }
        }
    }

    assets = _video_assets(note)

    assert len(assets) == 1
    assert assets[0].candidates == [
        "https://sns-video-zl.xhscdn.com/video.mp4?token=test",
        "https://sns-bak-v8.xhscdn.com/video.mp4",
    ]
    assert all(
        is_trusted_xiaohongshu_asset_url(candidate)
        for candidate in assets[0].candidates
    )


def test_video_assets_support_media_v2_json_snake_case_streams() -> None:
    media_v2 = {
        "stream": {
            "EF5": [
                {
                    "master_url": "http://sns-video-zl.xhscdn.com/hevc.mp4",
                    "backup_urls": [
                        "http://sns-bak-v10.xhscdn.com/hevc.mp4"
                    ],
                    "width": 1440,
                    "height": 2560,
                    "avg_bitrate": 8_000_000,
                    "size": 12_000_000,
                    "quality_type": "HD",
                    "video_codec": "EF5",
                    "audio_codec": "aac",
                }
            ]
        }
    }
    note = {
        "video": {
            "media": {
                "stream": {
                    "EF4": [
                        {
                            "masterUrl": (
                                "http://sns-video-zl.xhscdn.com/h264.mp4"
                            ),
                            "width": 720,
                            "height": 1280,
                            "avgBitrate": 4_000_000,
                            "qualityType": "SD",
                        }
                    ]
                }
            },
            "mediaV2": json.dumps(media_v2),
        }
    }

    assets = _video_assets(note)

    assert [(asset.width, asset.height) for asset in assets] == [
        (1440, 2560),
        (720, 1280),
    ]
    assert assets[0].format_id == "HD"
    assert assets[0].candidates == [
        "https://sns-video-zl.xhscdn.com/hevc.mp4",
        "https://sns-bak-v10.xhscdn.com/hevc.mp4",
    ]
    assert assets[1].candidates == [
        "https://sns-video-zl.xhscdn.com/h264.mp4"
    ]


def test_video_assets_ignore_invalid_or_oversized_media_v2_payloads() -> None:
    assert _video_assets({"video": "not-an-object"}) == []
    assert _video_assets(
        {"video": {"mediaV2": "{" + (" " * 1_000_001)}}
    ) == []


def test_parse_note_ignores_auth_words_inside_hidden_script(monkeypatch) -> None:
    class HiddenAuthScriptYoutubeDL(FakeYoutubeDL):
        html = '<script>const route = "captcha";</script>' + make_html()

    monkeypatch.setattr(
        "app.xiaohongshu.YoutubeDL",
        HiddenAuthScriptYoutubeDL,
    )

    note, _ = parse_note(NOTE_URL)

    assert note.note_id == NOTE_ID


def test_parse_note_accepts_matching_state_with_generic_login_overlay(
    monkeypatch,
) -> None:
    class LoginOverlayYoutubeDL(FakeYoutubeDL):
        html = (
            "<html><body><aside>登录即可查看 Ta 的笔记 "
            "手机号登录 获取验证码</aside></body></html>"
            + make_html()
        )

    monkeypatch.setattr("app.xiaohongshu.YoutubeDL", LoginOverlayYoutubeDL)

    note, _ = parse_note(NOTE_URL)

    assert note.note_id == NOTE_ID
    assert note.title == "Original title"


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


def test_profile_discovers_state_note_despite_generic_login_overlay(
    monkeypatch,
) -> None:
    profile_url = "https://www.xiaohongshu.com/user/profile/expected"
    note_url = (
        f"{NOTE_URL}?xsec_token=fresh-token&xsec_source=pc_user"
    )

    class FakeMouse:
        def wheel(self, x: int, y: int) -> None:
            return None

    class FakeLocator:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        @property
        def first(self):
            return self

        def count(self) -> int:
            return 1

        def inner_text(self, timeout: int) -> str:
            if self.selector == "body":
                return (
                    "登录即可查看 Ta 的笔记 手机号登录 +86 "
                    "获取验证码 登录"
                )
            return "Test Author"

        def evaluate_all(self, script: str) -> list[str]:
            return [note_url]

    class FakePage:
        url = profile_url
        mouse = FakeMouse()

        def goto(self, url: str, wait_until: str, timeout: int) -> None:
            self.url = url

        def wait_for_timeout(self, timeout: int) -> None:
            return None

        def locator(self, selector: str) -> FakeLocator:
            return FakeLocator(selector)

        def evaluate(self, script: str):
            if "noteQueries" in script:
                return False
            if "const notesRef" in script:
                return [{"id": NOTE_ID, "token": "fresh-token"}]
            return None

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

    result = discover_profile(profile_url)

    assert result.author == "Test Author"
    assert result.note_urls == [note_url]
    assert result.discovery_complete is True
    assert result.warning is None


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
