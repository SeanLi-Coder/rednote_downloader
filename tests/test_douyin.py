from __future__ import annotations

from http.cookiejar import CookieJar

import pytest

from app.douyin import (
    _is_explicit_douyin_auth_url,
    _is_target_post_response,
    _looks_like_auth_page,
    _looks_like_transient_limit,
    _minimal_aweme_metadata,
    _parse_profile_awemes,
    discover_item_metadata_from_profile,
    discover_profile,
    is_complete_profile_media_metadata,
    quality_floor_dimensions,
)
from app.errors import (
    AuthenticationRequiredError,
    DiscoveryError,
    TemporaryAccessError,
)


def _install_fake_douyin_browser(monkeypatch, page) -> None:
    class FakeContext:
        def new_page(self):
            return page

    class FakeBrowser:
        version = "151.0.0.0"

        def new_context(self, **kwargs):
            return FakeContext()

        def close(self) -> None:
            return None

    class FakeChromium:
        def launch(self, **kwargs):
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

    class FakePlaywrightContext:
        def __enter__(self):
            return FakePlaywright()

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

    def signed_profile_failure(*args, **kwargs):
        raise DiscoveryError("Force browser discovery")

    monkeypatch.setattr(
        "app.douyin.fetch_signed_profile_awemes",
        signed_profile_failure,
    )
    monkeypatch.setattr("app.douyin._extract_cookies", lambda profile: CookieJar())
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: FakePlaywrightContext(),
    )


def test_douyin_rate_limit_text_is_not_treated_as_captcha() -> None:
    assert _looks_like_transient_limit("当前访问频繁，请稍后再试")
    assert not _looks_like_auth_page("当前访问频繁，请稍后再试")
    assert _looks_like_auth_page("请完成下列验证码")
    assert not _looks_like_auth_page(
        '<script>const route = "captcha";</script><main>正常主页</main>'
    )
    assert _looks_like_transient_limit("网络环境存在风险，请稍后再试")
    assert not _looks_like_auth_page("网络环境存在风险，请稍后再试")


@pytest.mark.parametrize(
    "url",
    [
        "https://www.douyin.com/login",
        "https://www.douyin.com/passport/web/login",
        "https://sso.douyin.com/verify",
        "https://www.douyin.com/captcha/?from=profile",
    ],
)
def test_douyin_explicit_auth_urls_are_trusted_and_actionable(url: str) -> None:
    assert _is_explicit_douyin_auth_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.douyin.com.evil.example/login",
        "https://evil.example/passport/login",
        "http://www.douyin.com/login",
    ],
)
def test_douyin_lookalike_auth_urls_are_not_actionable(url: str) -> None:
    assert not _is_explicit_douyin_auth_url(url)


def test_item_metadata_profile_lookup_uses_exact_detail_when_preferred(
    monkeypatch,
) -> None:
    profile_id = "MS4wLjABAAAAexpected"
    media_id = "1111111111111111111"
    video_uri = "v0200fg10000fixturevideoid"
    calls = []

    def fetch_detail(requested_media_id, **kwargs):
        calls.append((requested_media_id, kwargs))
        return {
            "aweme_id": media_id,
            "author": {"sec_uid": profile_id, "nickname": "Test Author"},
            "video": {
                "play_addr": {
                    "uri": video_uri,
                    "width": 1080,
                    "height": 1920,
                    "url_list": [
                        "https://v26-web.douyinvod.com/item-target.mp4"
                    ],
                }
            },
        }

    monkeypatch.setattr("app.douyin.fetch_signed_aweme_detail", fetch_detail)
    monkeypatch.setattr(
        "app.douyin.fetch_signed_profile_awemes",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("The profile feed must not be requested after detail success")
        ),
    )

    result = discover_item_metadata_from_profile(
        profile_id,
        media_id,
        prefer_exact_detail=True,
    )

    assert result and result["media_id"] == media_id
    assert len(calls) == 1
    assert calls[0][0] == media_id
    assert calls[0][1]["expected_sec_uid"] == profile_id
    assert calls[0][1]["verification_url"] == (
        f"https://www.douyin.com/user/{profile_id}"
    )


def test_item_metadata_profile_lookup_uses_targeted_feed_by_default(
    monkeypatch,
) -> None:
    profile_id = "MS4wLjABAAAAexpected"
    media_id = "1111111111111111111"
    video_uri = "v0200fg10000fixturevideoid"
    feed_calls = []

    monkeypatch.setattr(
        "app.douyin.fetch_signed_aweme_detail",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Exact detail must not replace author-feed enrichment")
        ),
    )

    def fetch_feed(profile_url, requested_profile_id, **kwargs):
        feed_calls.append((profile_url, requested_profile_id, kwargs))
        return [
            {
                "aweme_id": media_id,
                "author": {"sec_uid": profile_id, "nickname": "Test Author"},
                "video": {
                    "play_addr": {
                        "uri": video_uri,
                        "width": 1080,
                        "height": 1920,
                        "url_list": [
                            "https://v26-web.douyinvod.com/item-target.mp4"
                        ],
                    }
                },
            }
        ]

    monkeypatch.setattr("app.douyin.fetch_signed_profile_awemes", fetch_feed)

    result = discover_item_metadata_from_profile(profile_id, media_id)

    assert result and result["media_id"] == media_id
    assert len(feed_calls) == 1
    assert feed_calls[0][2]["target_aweme_id"] == media_id


def test_item_metadata_preferred_detail_falls_back_to_targeted_feed(
    monkeypatch,
) -> None:
    profile_id = "MS4wLjABAAAAexpected"
    media_id = "1111111111111111111"
    video_uri = "v0200fg10000fixturevideoid"
    feed_calls = []

    monkeypatch.setattr(
        "app.douyin.fetch_signed_aweme_detail",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            TemporaryAccessError("detail endpoint was temporarily limited")
        ),
    )

    def fetch_feed(profile_url, requested_profile_id, **kwargs):
        feed_calls.append((profile_url, requested_profile_id, kwargs))
        return [
            {
                "aweme_id": media_id,
                "author": {"sec_uid": profile_id, "nickname": "Test Author"},
                "video": {
                    "play_addr": {
                        "uri": video_uri,
                        "width": 1440,
                        "height": 2560,
                        "url_list": [
                            "https://v26-web.douyinvod.com/feed-target.mp4"
                        ],
                    }
                },
            }
        ]

    monkeypatch.setattr("app.douyin.fetch_signed_profile_awemes", fetch_feed)

    result = discover_item_metadata_from_profile(
        profile_id,
        media_id,
        prefer_exact_detail=True,
    )

    assert result and result["media_id"] == media_id
    assert result["minimum_width"] == 1440
    assert result["minimum_height"] == 2560
    assert len(feed_calls) == 1
    assert feed_calls[0][2]["target_aweme_id"] == media_id


def test_douyin_signed_profile_discovery_returns_verified_complete_metadata(
    monkeypatch,
) -> None:
    profile_id = "MS4wLjABAAAAexpected"
    profile_url = f"https://www.douyin.com/user/{profile_id}"
    aweme_id = "1111111111111111111"
    video_uri = "v0200fg10000fixturevideoid"
    video_url = "https://v26-web.douyinvod.com/signed-profile-1440.mp4"
    monkeypatch.setattr(
        "app.douyin.fetch_signed_profile_awemes",
        lambda *args, **kwargs: [
            {
                "aweme_id": aweme_id,
                "desc": "Signed profile video",
                "create_time": 1_756_656_000,
                "author": {
                    "sec_uid": profile_id,
                    "nickname": "Signed Author",
                },
                "video": {
                    "duration": 72_800,
                    "width": 1440,
                    "height": 2560,
                    "play_addr": {
                        "uri": video_uri,
                        "width": 1440,
                        "height": 2560,
                        "url_list": [video_url],
                    },
                },
            }
        ],
    )

    result = discover_profile(profile_url, use_browser_cookies=True)

    assert result.author == "Signed Author"
    assert result.video_urls == [f"https://www.douyin.com/video/{aweme_id}"]
    assert result.discovery_complete is True
    assert result.media_metadata[aweme_id] == {
        "media_id": aweme_id,
        "owner_id": profile_id,
        "media_kind": "video",
        "video_uri": video_uri,
        "direct_candidates": [
            {
                "width": 1440,
                "height": 2560,
                "urls": [video_url],
                "video_uri": video_uri,
            }
        ],
        "minimum_width": 1440,
        "minimum_height": 2560,
        "duration_ms": 72_800,
        "create_time": 1_756_656_000,
        "title": "Signed profile video",
        "author": "Signed Author",
    }


def test_douyin_browser_timeout_does_not_request_chrome_verification(
    monkeypatch,
) -> None:
    from playwright.sync_api import Error as PlaywrightError

    profile_id = "MS4wLjABAAAAexpected"
    profile_url = f"https://www.douyin.com/user/{profile_id}"

    def signed_profile_failure(*args, **kwargs):
        raise AuthenticationRequiredError(
            "Signed profile unavailable",
            verification_url=profile_url,
        )

    class TimeoutPlaywrightContext:
        def __enter__(self):
            raise PlaywrightError("Timeout 45000ms exceeded while navigating")

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

    monkeypatch.setattr(
        "app.douyin.fetch_signed_profile_awemes",
        signed_profile_failure,
    )
    monkeypatch.setattr("app.douyin._extract_cookies", lambda profile: CookieJar())
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: TimeoutPlaywrightContext(),
    )

    with pytest.raises(TemporaryAccessError, match="Chrome verification is not required"):
        discover_profile(profile_url, use_browser_cookies=True)


def test_douyin_browser_mixed_login_and_rate_limit_is_temporary(
    monkeypatch,
) -> None:
    profile_id = "MS4wLjABAAAAexpected"
    profile_url = f"https://www.douyin.com/user/{profile_id}"

    def signed_profile_failure(*args, **kwargs):
        raise DiscoveryError("Force browser discovery")

    class FakeBody:
        def count(self) -> int:
            return 1

        def inner_text(self, timeout: int) -> str:
            return "请登录 当前访问频繁，请稍后再试"

    class FakePage:
        url = profile_url

        def on(self, event: str, callback) -> None:
            return None

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
        "app.douyin.fetch_signed_profile_awemes",
        signed_profile_failure,
    )
    monkeypatch.setattr("app.douyin._extract_cookies", lambda profile: CookieJar())
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: FakePlaywrightContext(),
    )

    with pytest.raises(
        TemporaryAccessError,
        match="Chrome verification is not required",
    ):
        discover_profile(profile_url, use_browser_cookies=True)


def test_douyin_initial_html_hidden_auth_word_does_not_request_verification(
    monkeypatch,
) -> None:
    profile_id = "MS4wLjABAAAAexpected"
    profile_url = f"https://www.douyin.com/user/{profile_id}"

    class FakeBody:
        def count(self) -> int:
            return 1

        def inner_text(self, timeout: int) -> str:
            raise RuntimeError("DOM text unavailable")

    class FakePage:
        url = profile_url

        def on(self, event: str, callback) -> None:
            return None

        def goto(self, url: str, wait_until: str, timeout: int) -> None:
            self.url = url

        def wait_for_timeout(self, timeout: int) -> None:
            return None

        def locator(self, selector: str) -> FakeBody:
            return FakeBody()

        def content(self) -> str:
            return '<script>const route="captcha";</script><main>正常主页</main>'

    _install_fake_douyin_browser(monkeypatch, FakePage())

    with pytest.raises(
        TemporaryAccessError,
        match="no verified profile-owned media",
    ):
        discover_profile(profile_url, max_scrolls=0)


@pytest.mark.parametrize(
    ("scroll_text", "expect_rate_limit"),
    [
        (None, False),
        ("网络环境存在风险，请稍后再试", True),
    ],
)
def test_douyin_scroll_non_auth_content_does_not_request_verification(
    monkeypatch,
    scroll_text: str | None,
    expect_rate_limit: bool,
) -> None:
    profile_id = "MS4wLjABAAAAexpected"
    profile_url = f"https://www.douyin.com/user/{profile_id}"
    media_id = "1111111111111111111"

    class FakeResponse:
        url = (
            "https://www.douyin.com/aweme/v1/web/aweme/post/"
            f"?sec_user_id={profile_id}"
        )

        def json(self):
            return {
                "aweme_list": [
                    {
                        "aweme_id": media_id,
                        "desc": "Verified title",
                        "create_time": 1_756_656_000,
                        "author": {
                            "sec_uid": profile_id,
                            "nickname": "Verified Author",
                        },
                        "video": {
                            "duration": 12_000,
                            "width": 1080,
                            "height": 1920,
                            "play_addr": {
                                "uri": "v0200fg10000fixturevideoid",
                                "width": 1080,
                                "height": 1920,
                                "url_list": [
                                    "https://v26-web.douyinvod.com/browser-verified.mp4"
                                ],
                            },
                        },
                    }
                ],
                "has_more": True,
            }

    class FakeBody:
        def __init__(self) -> None:
            self.calls = 0

        def count(self) -> int:
            return 1

        def inner_text(self, timeout: int) -> str:
            self.calls += 1
            if self.calls == 1:
                return "正常主页"
            if scroll_text is not None:
                return scroll_text
            raise RuntimeError("DOM text unavailable after scroll")

    class FakePage:
        url = profile_url

        def __init__(self) -> None:
            self.body = FakeBody()
            self.response_callback = None
            self.mouse = self

        def on(self, event: str, callback) -> None:
            if event == "response":
                self.response_callback = callback

        def goto(self, url: str, wait_until: str, timeout: int) -> None:
            self.url = url
            assert self.response_callback is not None
            self.response_callback(FakeResponse())

        def wait_for_timeout(self, timeout: int) -> None:
            return None

        def locator(self, selector: str) -> FakeBody:
            return self.body

        def content(self) -> str:
            return '<script>const route="captcha";</script><main>正常主页</main>'

        def wheel(self, x: int, y: int) -> None:
            return None

        def evaluate(self, script: str) -> None:
            return None

    _install_fake_douyin_browser(monkeypatch, FakePage())

    if expect_rate_limit:
        with pytest.raises(
            TemporaryAccessError,
            match="temporarily rate-limited",
        ):
            discover_profile(profile_url, max_scrolls=1)
        return

    result = discover_profile(profile_url, max_scrolls=1)
    assert result.author == "Verified Author"
    assert result.video_urls == [f"https://www.douyin.com/video/{media_id}"]
    assert result.discovery_complete is False


@pytest.mark.parametrize(
    ("redirect_url", "expected_error"),
    [
        ("https://www.douyin.com/login", AuthenticationRequiredError),
        ("https://evil.example/login", DiscoveryError),
    ],
)
def test_douyin_browser_redirect_requires_trusted_explicit_auth_url(
    monkeypatch,
    redirect_url: str,
    expected_error: type[Exception],
) -> None:
    profile_id = "MS4wLjABAAAAexpected"
    profile_url = f"https://www.douyin.com/user/{profile_id}"

    def signed_profile_failure(*args, **kwargs):
        raise AuthenticationRequiredError(
            "Signed profile unavailable",
            verification_url=profile_url,
        )

    class FakeLocator:
        def count(self) -> int:
            return 1

        def inner_text(self, timeout: int) -> str:
            return "Neutral client-side shell"

    class FakePage:
        def __init__(self) -> None:
            self.url = profile_url

        def on(self, event: str, callback) -> None:
            return None

        def goto(self, url: str, wait_until: str, timeout: int) -> None:
            self.url = redirect_url

        def wait_for_timeout(self, timeout: int) -> None:
            return None

        def locator(self, selector: str) -> FakeLocator:
            return FakeLocator()

        def content(self) -> str:
            return "<html><body>Neutral client-side shell</body></html>"

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
        "app.douyin.fetch_signed_profile_awemes",
        signed_profile_failure,
    )
    monkeypatch.setattr("app.douyin._extract_cookies", lambda profile: CookieJar())
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: FakePlaywrightContext(),
    )

    with pytest.raises(expected_error) as captured:
        discover_profile(profile_url, use_browser_cookies=True)

    if isinstance(captured.value, AuthenticationRequiredError):
        assert captured.value.verification_url == profile_url


def test_douyin_minimal_metadata_accepts_265_and_bitrate_uris() -> None:
    profile_id = "MS4wLjABAAAAexpected"
    base = {
        "aweme_id": "1111111111111111111",
        "author": {"sec_uid": profile_id},
    }

    from_265 = _minimal_aweme_metadata(
        {
            **base,
            "video": {
                "play_addr_265": {
                    "uri": "v0200fg10000265fixtureid",
                    "width": 1080,
                    "height": 1920,
                    "url_list": [
                        "https://v26-web.douyinvod.com/verified-265.mp4"
                    ],
                }
            },
        },
        profile_id,
    )
    from_bitrate = _minimal_aweme_metadata(
        {
            **base,
            "video": {
                "bit_rate": [
                    {
                        "bit_rate": 2_000_000,
                        "play_addr": {
                            "uri": "v0200fg10000bitratefixtureid",
                            "width": 1440,
                            "height": 2560,
                            "url_list": [
                                "https://v11-weba.douyinvod.com/verified-bitrate.mp4"
                            ],
                        },
                    }
                ]
            },
        },
        profile_id,
    )

    assert from_265 and from_265[1]["video_uri"] == "v0200fg10000265fixtureid"
    assert from_265[1]["media_kind"] == "video"
    assert from_265[1]["direct_candidates"] == [
        {
            "width": 1080,
            "height": 1920,
            "urls": ["https://v26-web.douyinvod.com/verified-265.mp4"],
            "video_uri": "v0200fg10000265fixtureid",
            "codec_hint": "hevc",
        }
    ]
    assert from_bitrate and from_bitrate[1]["video_uri"] == (
        "v0200fg10000bitratefixtureid"
    )
    assert from_bitrate[1]["direct_candidates"] == [
        {
            "width": 1440,
            "height": 2560,
            "urls": ["https://v11-weba.douyinvod.com/verified-bitrate.mp4"],
            "video_uri": "v0200fg10000bitratefixtureid",
            "bit_rate": 2_000_000,
            "codec_hint": "h264",
        }
    ]


def test_douyin_realistic_photo_post_uses_images_not_top_level_music_video() -> None:
    profile_id = "MS4wLjABAAAAexpected"
    media_id = "7676078420824775161"
    image_url = (
        "https://p3-pc-sign.douyinpic.com/tos-cn-i-0813c000-ce/photo-1"
        "~tplv-dy-aweme-images:q75.webp?x-signature=verified"
    )
    live_720 = "https://v11-weba.douyinvod.com/live-720.mp4"
    live_1080 = "https://v26-web.douyinvod.com/live-1080.mp4"

    result = _minimal_aweme_metadata(
        {
            "aweme_id": media_id,
            "aweme_type": 68,
            "desc": "Photo post title",
            "create_time": 1_756_656_000,
            "author": {"sec_uid": profile_id, "nickname": "Photo Author"},
            "video": {
                "play_addr": {
                    "uri": "https://lf9-music-east.douyinstatic.com/music.mp3",
                    "width": 720,
                    "height": 720,
                }
            },
            "images": [
                {
                    "width": 1440,
                    "height": 2560,
                    "url_list": [image_url, "https://evil.example/photo.webp"],
                    "download_url_list": [
                        "https://p3-pc-sign.douyinpic.com/lower-1080.webp"
                    ],
                    "video": {
                        "play_addr": {
                            "uri": "v0200fg10000livephotoasset",
                            "width": 720,
                            "height": 1280,
                            "url_list": [live_720],
                        },
                        "bit_rate": [
                            {
                                "bit_rate": 2_000_000,
                                "is_h265": 1,
                                "play_addr": {
                                    "uri": "v0200fg10000livephotoasset",
                                    "width": 1080,
                                    "height": 1920,
                                    "url_list": [live_1080],
                                },
                            }
                        ],
                    },
                }
            ],
        },
        profile_id,
    )

    assert result is not None
    metadata = result[1]
    assert metadata["media_kind"] == "image"
    assert metadata["title"] == "Photo post title"
    assert metadata["image_assets"] == [
        {
            "index": 1,
            "width": 1440,
            "height": 2560,
            "candidates": [image_url],
        }
    ]
    assert metadata["live_photo_assets"] == [
        {
            "index": 1,
            "width": 1080,
            "height": 1920,
            "candidates": [live_1080],
            "video_uri": "v0200fg10000livephotoasset",
            "direct_candidates": [
                {
                    "width": 1080,
                    "height": 1920,
                    "urls": [live_1080],
                    "video_uri": "v0200fg10000livephotoasset",
                    "bit_rate": 2_000_000,
                    "codec_hint": "hevc",
                }
            ],
        }
    ]
    assert "lower-1080" not in str(metadata)
    assert is_complete_profile_media_metadata(metadata, media_id, profile_id)


def test_douyin_image_metadata_requires_trusted_complete_assets() -> None:
    profile_id = "MS4wLjABAAAAexpected"
    media_id = "7676078420824775161"
    base = {
        "media_id": media_id,
        "owner_id": profile_id,
        "media_kind": "image",
        "title": "Photo title",
        "image_assets": [
            {
                "index": 1,
                "width": 1080,
                "height": 1920,
                "candidates": ["https://p3-pc-sign.douyinpic.com/photo.webp"],
            }
        ],
    }

    assert is_complete_profile_media_metadata(base, media_id, profile_id)
    assert is_complete_profile_media_metadata(
        {key: value for key, value in base.items() if key != "title"},
        media_id,
        profile_id,
    )
    assert not is_complete_profile_media_metadata(
        {**base, "owner_id": "MS4wLjABAAAAother"}, media_id, profile_id
    )
    assert not is_complete_profile_media_metadata(
        {
            **base,
            "image_assets": [
                {
                    "index": 1,
                    "width": 1080,
                    "height": 1920,
                    "candidates": ["https://evil.example/photo.webp"],
                }
            ],
        },
        media_id,
        profile_id,
    )


@pytest.mark.parametrize("direct_candidates", [None, []])
def test_douyin_video_metadata_requires_nonempty_direct_candidates(
    direct_candidates,
) -> None:
    profile_id = "MS4wLjABAAAAexpected"
    media_id = "1111111111111111111"
    metadata = {
        "media_id": media_id,
        "owner_id": profile_id,
        "media_kind": "video",
        "video_uri": "v0200fg10000fixturevideoid",
    }
    if direct_candidates is not None:
        metadata["direct_candidates"] = direct_candidates

    assert not is_complete_profile_media_metadata(metadata, media_id, profile_id)


def test_douyin_static_only_image_metadata_is_complete() -> None:
    profile_id = "MS4wLjABAAAAexpected"
    media_id = "7676078420824775161"
    metadata = {
        "media_id": media_id,
        "owner_id": profile_id,
        "media_kind": "image",
        "image_assets": [
            {
                "index": 1,
                "width": 1440,
                "height": 2560,
                "candidates": [
                    "https://p3-pc-sign.douyinpic.com/static-only.webp"
                ],
            }
        ],
    }

    assert is_complete_profile_media_metadata(metadata, media_id, profile_id)


def test_douyin_live_photo_metadata_requires_direct_candidates() -> None:
    profile_id = "MS4wLjABAAAAexpected"
    media_id = "7676078420824775161"
    metadata = {
        "media_id": media_id,
        "owner_id": profile_id,
        "media_kind": "image",
        "image_assets": [
            {
                "index": 1,
                "width": 1440,
                "height": 2560,
                "candidates": [
                    "https://p3-pc-sign.douyinpic.com/live-cover.webp"
                ],
            }
        ],
        "live_photo_assets": [
            {
                "index": 1,
                "width": 1080,
                "height": 1920,
                "candidates": [
                    "https://v26-web.douyinvod.com/live-without-direct.mp4"
                ],
                "video_uri": "v0200fg10000livephotoasset",
            }
        ],
    }

    assert not is_complete_profile_media_metadata(metadata, media_id, profile_id)


def test_douyin_live_photo_rejects_multiple_highest_media_identities() -> None:
    profile_id = "MS4wLjABAAAAexpected"
    media_id = "7676078420824775161"
    result = _minimal_aweme_metadata(
        {
            "aweme_id": media_id,
            "aweme_type": 68,
            "author": {"sec_uid": profile_id},
            "images": [
                {
                    "width": 1080,
                    "height": 1920,
                    "url_list": [
                        "https://p3-pc-sign.douyinpic.com/photo.webp"
                    ],
                    "video": {
                        "play_addr": {
                            "uri": "v0200fg10000liveidentityA",
                            "width": 1080,
                            "height": 1920,
                            "url_list": [
                                "https://v26-web.douyinvod.com/live-a.mp4"
                            ],
                        },
                        "play_addr_h264": {
                            "uri": "v0200fg10000liveidentityB",
                            "width": 1080,
                            "height": 1920,
                            "url_list": [
                                "https://v11-web.douyinvod.com/live-b.mp4"
                            ],
                        },
                    },
                }
            ],
        },
        profile_id,
    )

    assert result is None


def test_douyin_live_photo_rejects_different_lower_rendition_identity() -> None:
    profile_id = "MS4wLjABAAAAexpected"
    media_id = "7676078420824775161"
    result = _minimal_aweme_metadata(
        {
            "aweme_id": media_id,
            "aweme_type": 68,
            "author": {"sec_uid": profile_id},
            "images": [
                {
                    "width": 1080,
                    "height": 1920,
                    "url_list": [
                        "https://p3-pc-sign.douyinpic.com/photo.webp"
                    ],
                    "video": {
                        "play_addr": {
                            "uri": "v0200fg10000liveidentityA",
                            "width": 720,
                            "height": 1280,
                            "url_list": [
                                "https://v26-web.douyinvod.com/live-a.mp4"
                            ],
                        },
                        "bit_rate": [
                            {
                                "bit_rate": 2_000_000,
                                "play_addr": {
                                    "uri": "v0200fg10000liveidentityB",
                                    "width": 1080,
                                    "height": 1920,
                                    "url_list": [
                                        "https://v11-web.douyinvod.com/live-b.mp4"
                                    ],
                                },
                            }
                        ],
                    },
                }
            ],
        },
        profile_id,
    )

    assert result is None


def test_douyin_profile_video_rejects_multiple_media_identities() -> None:
    profile_id = "MS4wLjABAAAAexpected"
    result = _minimal_aweme_metadata(
        {
            "aweme_id": "7676078420824775161",
            "author": {"sec_uid": profile_id},
            "video": {
                "play_addr": {
                    "uri": "v0200fg10000profileidentityA",
                    "width": 720,
                    "height": 1280,
                    "url_list": [
                        "https://v26-web.douyinvod.com/profile-a.mp4"
                    ],
                },
                "bit_rate": [
                    {
                        "bit_rate": 2_000_000,
                        "play_addr": {
                            "uri": "v0200fg10000profileidentityB",
                            "width": 1080,
                            "height": 1920,
                            "url_list": [
                                "https://v11-web.douyinvod.com/profile-b.mp4"
                            ],
                        },
                    }
                ],
            },
        },
        profile_id,
    )

    assert result is None


def test_douyin_missing_description_gets_non_numeric_display_title() -> None:
    profile_id = "MS4wLjABAAAAexpected"
    media_id = "7676078420824775161"
    result = _minimal_aweme_metadata(
        {
            "aweme_id": media_id,
            "aweme_type": 68,
            "author": {"sec_uid": profile_id},
            "images": [
                {
                    "width": 1080,
                    "height": 1920,
                    "url_list": [
                        "https://p3-pc-sign.douyinpic.com/photo.webp"
                    ],
                }
            ],
        },
        profile_id,
    )

    assert result is not None
    assert result[1]["title"] == "Untitled Douyin image"
    assert "live_photo_assets" not in result[1]


def test_douyin_signed_profile_fails_closed_on_incomplete_media(monkeypatch) -> None:
    profile_id = "MS4wLjABAAAAexpected"
    profile_url = f"https://www.douyin.com/user/{profile_id}"
    media_id = "7676078420824775161"
    monkeypatch.setattr(
        "app.douyin.fetch_signed_profile_awemes",
        lambda *args, **kwargs: [
            {
                "aweme_id": media_id,
                "aweme_type": 68,
                "desc": "Incomplete photo",
                "author": {"sec_uid": profile_id, "nickname": "Author"},
                "video": {"play_addr": {"uri": "music.mp3"}},
                "images": [
                    {
                        "width": 1080,
                        "height": 1920,
                        "url_list": ["https://evil.example/photo.webp"],
                    }
                ],
            }
        ],
    )

    with pytest.raises(TemporaryAccessError, match="complete verified metadata"):
        discover_profile(profile_url, use_browser_cookies=True)


def test_douyin_minimal_metadata_preserves_verified_quality_floor() -> None:
    profile_id = "MS4wLjABAAAAexpected"
    video_uri = "v0200fg10000fixturevideoid"
    result = _minimal_aweme_metadata(
        {
            "aweme_id": "1111111111111111111",
            "author": {"sec_uid": profile_id},
            "video": {
                "play_addr": {
                    "uri": video_uri,
                    "width": 1920,
                    "height": 1080,
                    "url_list": [
                        "https://v26-web.douyinvod.com/verified-1080-landscape.mp4"
                    ],
                },
                "bit_rate": [
                    {
                        "bit_rate": 3_000_000,
                        "width": 2560,
                        "height": 1440,
                        "play_addr": {
                            "uri": video_uri,
                            "width": 2560,
                            "height": 1440,
                            "url_list": [
                                "https://v11-weba.douyinvod.com/verified-1440-landscape.mp4"
                            ],
                        },
                    }
                ],
            },
        },
        profile_id,
    )

    assert result is not None
    assert result[1]["minimum_width"] == 2560
    assert result[1]["minimum_height"] == 1440
    assert result[1]["direct_candidates"] == [
        {
            "width": 2560,
            "height": 1440,
            "urls": [
                "https://v11-weba.douyinvod.com/verified-1440-landscape.mp4"
            ],
            "video_uri": video_uri,
            "bit_rate": 3_000_000,
            "codec_hint": "h264",
        }
    ]


def test_douyin_quality_floor_does_not_inflate_native_720() -> None:
    assert quality_floor_dimensions([{"width": 720, "height": 1280}]) == (
        720,
        1280,
    )


def test_douyin_metadata_keeps_highest_verified_direct_rendition() -> None:
    profile_id = "MS4wLjABAAAAexpected"
    shared_uri = "v0200fg10000fixturevideoid"
    result = _minimal_aweme_metadata(
        {
            "aweme_id": "1111111111111111111",
            "author": {"sec_uid": profile_id},
            "video": {
                "width": 1440,
                "height": 2560,
                "play_addr": {
                    "uri": shared_uri,
                    "width": 1080,
                    "height": 1920,
                    "url_list": ["https://v26-web.douyinvod.com/verified-1080.mp4"],
                },
                "bit_rate": [
                    {
                        "bit_rate": 1_320_511,
                        "is_bytevc1": 1,
                        "play_addr": {
                            "uri": shared_uri,
                            "width": 1440,
                            "height": 2560,
                            "url_list": [
                                "https://v11-weba.douyinvod.com/verified-1440.mp4",
                                "https://evil.example/untrusted.mp4",
                            ],
                        },
                    }
                ],
            },
        },
        profile_id,
    )

    assert result is not None
    metadata = result[1]
    assert metadata["minimum_width"] == 1440
    assert metadata["minimum_height"] == 2560
    assert metadata["direct_candidates"] == [
        {
            "width": 1440,
            "height": 2560,
            "urls": ["https://v11-weba.douyinvod.com/verified-1440.mp4"],
            "video_uri": shared_uri,
            "bit_rate": 1_320_511,
            "codec_hint": "hevc",
        }
    ]


def test_douyin_explicit_bitrate_candidate_precedes_root_fallbacks() -> None:
    profile_id = "MS4wLjABAAAAexpected"
    video = {
        "play_addr": {"uri": "v0200fg10000fixturevideoid"},
        "bit_rate": [
            {
                "bit_rate": 2_000_000,
                "is_h265": 1,
                "play_addr": {
                    "uri": "v0200fg10000fixturevideoid",
                    "width": 1440,
                    "height": 2560,
                    "url_list": ["https://v26-web.douyinvod.com/explicit-high.mp4"],
                },
            }
        ],
    }
    for index, key in enumerate(
        ("play_addr", "play_addr_h264", "play_addr_265", "play_addr_bytevc1")
    ):
        video[key] = {
            "uri": "v0200fg10000fixturevideoid",
            "width": 1440,
            "height": 2560,
            "url_list": [f"https://v26-web.douyinvod.com/root-{index}.mp4"],
        }
    result = _minimal_aweme_metadata(
        {
            "aweme_id": "1111111111111111111",
            "author": {"sec_uid": profile_id},
            "video": video,
        },
        profile_id,
    )

    assert result is not None
    candidates = result[1]["direct_candidates"]
    assert len(candidates) == 4
    assert candidates[0]["bit_rate"] == 2_000_000
    assert candidates[0]["urls"] == ["https://v26-web.douyinvod.com/explicit-high.mp4"]


def test_douyin_post_response_must_match_requested_profile() -> None:
    profile_id = "MS4wLjABAAAAexpected"

    assert _is_target_post_response(
        "https://www.douyin.com/aweme/v1/web/aweme/post/"
        f"?sec_user_id={profile_id}&max_cursor=0",
        profile_id,
    )
    assert not _is_target_post_response(
        "https://www.douyin.com/aweme/v1/web/aweme/post/"
        "?sec_user_id=MS4wLjABAAAAother&max_cursor=0",
        profile_id,
    )
    assert not _is_target_post_response(
        "https://www.douyin.com/aweme/v1/web/aweme/detail/"
        f"?sec_user_id={profile_id}",
        profile_id,
    )
    assert not _is_target_post_response(
        "https://evil-douyin.example/aweme/v1/web/aweme/post/"
        f"?sec_user_id={profile_id}",
        profile_id,
    )


def test_douyin_profile_awemes_require_matching_author_identity() -> None:
    profile_id = "MS4wLjABAAAAexpected"
    data = {
        "has_more": 1,
        "aweme_list": [
            {
                "aweme_id": "1111111111111111111",
                "author": {"sec_uid": profile_id, "nickname": "Expected Author"},
                "video": {"play_addr": {}},
            },
            {
                "aweme_id": "2222222222222222222",
                "author": {
                    "sec_uid": "MS4wLjABAAAAother",
                    "nickname": "Other Author",
                },
                "video": {"play_addr": {}},
            },
            {
                "aweme_id": "3333333333333333333",
                "author": {"nickname": "Unknown Owner"},
                "video": {"play_addr": {}},
            },
        ],
    }

    entries, authors, has_more = _parse_profile_awemes(data, profile_id)

    assert entries == [
        (
            "1111111111111111111",
            "https://www.douyin.com/video/1111111111111111111",
        )
    ]
    assert authors == ["Expected Author"]
    assert has_more is True


def test_douyin_discovery_waits_for_scrolled_api_page_before_stability_stop(
    monkeypatch,
) -> None:
    profile_id = "MS4wLjABAAAAexpected"
    profile_url = f"https://www.douyin.com/user/{profile_id}"

    def response_data(aweme_id: str, has_more: bool) -> dict:
        video_uri = f"video-{aweme_id}"
        return {
            "has_more": int(has_more),
            "aweme_list": [
                {
                    "aweme_id": aweme_id,
                    "desc": f"Video {aweme_id}",
                    "author": {"sec_uid": profile_id, "nickname": "Author"},
                    "video": {
                        "play_addr": {
                            "uri": video_uri,
                            "width": 1080,
                            "height": 1920,
                            "url_list": [
                                f"https://v26-web.douyinvod.com/{aweme_id}.mp4"
                            ],
                        }
                    },
                }
            ],
        }

    class FakeResponse:
        def __init__(self, data: dict):
            self.url = (
                "https://www.douyin.com/aweme/v1/web/aweme/post/"
                f"?sec_user_id={profile_id}"
            )
            self._data = data

        def json(self):
            return self._data

    class FakeLocator:
        def __init__(self, selector: str):
            self.selector = selector

        @property
        def first(self):
            return self

        def count(self) -> int:
            return 1

        def inner_text(self, timeout: int) -> str:
            return "" if self.selector == "body" else "Author"

    class FakeMouse:
        def __init__(self, page):
            self.page = page

        def wheel(self, x: int, y: int) -> None:
            self.page.scrolled = True

    class FakePage:
        def __init__(self):
            self.url = profile_url
            self.callback = None
            self.scrolled = False
            self.sent_second_page = False
            self.mouse = FakeMouse(self)

        def on(self, event: str, callback) -> None:
            if event == "response":
                self.callback = callback

        def goto(self, url: str, wait_until: str, timeout: int):
            self.url = url
            self.callback(FakeResponse(response_data("1111111111111111111", True)))

        def wait_for_timeout(self, timeout: int) -> None:
            if self.scrolled and not self.sent_second_page:
                self.sent_second_page = True
                self.callback(FakeResponse(response_data("2222222222222222222", False)))

        def locator(self, selector: str) -> FakeLocator:
            return FakeLocator(selector)

        def evaluate(self, script: str) -> None:
            return None

        def content(self) -> str:
            return "<html><body></body></html>"

        def title(self) -> str:
            return "Author - 抖音"

    class FakeContext:
        def __init__(self):
            self.page = FakePage()

        def new_page(self) -> FakePage:
            return self.page

    class FakeBrowser:
        version = "151.0.0.0"

        def __init__(self):
            self.context = FakeContext()

        def new_context(self, **kwargs) -> FakeContext:
            return self.context

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
        "playwright.sync_api.sync_playwright", lambda: FakePlaywrightContext()
    )

    def signed_profile_failure(*args, **kwargs):
        raise AuthenticationRequiredError(
            "Signed profile unavailable",
            verification_url=profile_url,
        )

    monkeypatch.setattr(
        "app.douyin.fetch_signed_profile_awemes", signed_profile_failure
    )
    monkeypatch.setattr("app.douyin._extract_cookies", lambda profile: CookieJar())

    result = discover_profile(
        profile_url,
        use_browser_cookies=True,
        max_scrolls=3,
        stable_rounds=1,
    )

    assert result.video_urls == [
        "https://www.douyin.com/video/1111111111111111111",
        "https://www.douyin.com/video/2222222222222222222",
    ]
    assert result.discovery_complete is True
