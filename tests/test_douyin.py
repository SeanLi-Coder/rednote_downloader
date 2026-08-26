from __future__ import annotations

from http.cookiejar import CookieJar

from app.douyin import (
    _is_target_post_response,
    _minimal_aweme_metadata,
    _parse_profile_awemes,
    discover_item_metadata_from_profile,
    discover_profile,
    quality_floor_dimensions,
)
from app.errors import AuthenticationRequiredError


def test_item_metadata_profile_lookup_stops_at_target(monkeypatch) -> None:
    profile_id = "MS4wLjABAAAAexpected"
    media_id = "1111111111111111111"
    calls = []

    def fetch(profile_url, requested_profile_id, **kwargs):
        calls.append((profile_url, requested_profile_id, kwargs))
        return [
            {
                "aweme_id": media_id,
                "author": {"sec_uid": profile_id, "nickname": "Test Author"},
                "video": {
                    "play_addr": {"uri": "v0200fg10000fixturevideoid"}
                },
            }
        ]

    monkeypatch.setattr("app.douyin.fetch_signed_profile_awemes", fetch)

    result = discover_item_metadata_from_profile(profile_id, media_id)

    assert result and result["media_id"] == media_id
    assert len(calls) == 1
    assert calls[0][2]["target_aweme_id"] == media_id


def test_douyin_signed_profile_discovery_returns_verified_complete_metadata(
    monkeypatch,
) -> None:
    profile_id = "MS4wLjABAAAAexpected"
    profile_url = f"https://www.douyin.com/user/{profile_id}"
    aweme_id = "1111111111111111111"
    video_uri = "v0200fg10000fixturevideoid"
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
                    "play_addr": {"uri": video_uri},
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
        "video_uri": video_uri,
        "minimum_width": 1080,
        "minimum_height": 1920,
        "duration_ms": 72_800,
        "create_time": 1_756_656_000,
        "title": "Signed profile video",
        "author": "Signed Author",
    }


def test_douyin_minimal_metadata_accepts_265_and_bitrate_uris() -> None:
    profile_id = "MS4wLjABAAAAexpected"
    base = {
        "aweme_id": "1111111111111111111",
        "author": {"sec_uid": profile_id},
    }

    from_265 = _minimal_aweme_metadata(
        {
            **base,
            "video": {"play_addr_265": {"uri": "v0200fg10000265fixtureid"}},
        },
        profile_id,
    )
    from_bitrate = _minimal_aweme_metadata(
        {
            **base,
            "video": {
                "bit_rate": [{"play_addr": {"uri": "v0200fg10000bitratefixtureid"}}]
            },
        },
        profile_id,
    )

    assert from_265 and from_265[1]["video_uri"] == "v0200fg10000265fixtureid"
    assert from_bitrate and from_bitrate[1]["video_uri"] == (
        "v0200fg10000bitratefixtureid"
    )


def test_douyin_minimal_metadata_caches_conservative_quality_floor() -> None:
    profile_id = "MS4wLjABAAAAexpected"
    result = _minimal_aweme_metadata(
        {
            "aweme_id": "1111111111111111111",
            "author": {"sec_uid": profile_id},
            "video": {
                "play_addr": {"uri": "v0200fg10000fixturevideoid"},
                "bit_rate": [
                    {
                        "width": 2560,
                        "height": 1440,
                        "play_addr": {"uri": "v0200fg10000highqualityid"},
                    }
                ],
            },
        },
        profile_id,
    )

    assert result is not None
    assert result[1]["minimum_width"] == 1920
    assert result[1]["minimum_height"] == 1080


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
        return {
            "has_more": int(has_more),
            "aweme_list": [
                {
                    "aweme_id": aweme_id,
                    "author": {"sec_uid": profile_id, "nickname": "Author"},
                    "video": {"play_addr": {}},
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
