from __future__ import annotations

from http.cookiejar import CookieJar

import pytest

from app.douyin import (
    _auth_page_issue_code,
    _explicit_douyin_auth_url_issue_code,
    _is_explicit_douyin_auth_url,
    _is_target_post_response,
    _looks_like_auth_page,
    _looks_like_request_rejected,
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
    DownloadCancelledError,
    SiteIssueCode,
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


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeProgressBudget:
    def __init__(self, clock: FakeClock, timeout_seconds: float = 120) -> None:
        self.clock = clock
        self.timeout_seconds = timeout_seconds
        self.deadline = clock.now + timeout_seconds
        self.refresh_calls = 0

    def remaining_seconds(self) -> float:
        remaining = self.deadline - self.clock.now
        if remaining <= 0:
            raise RuntimeError("PRIVATE_FAKE_BUDGET_TIMEOUT")
        return remaining

    def refresh(self) -> None:
        self.refresh_calls += 1
        self.deadline = self.clock.now + self.timeout_seconds


def test_douyin_rate_limit_text_is_not_treated_as_captcha() -> None:
    assert _looks_like_transient_limit("当前访问频繁，请稍后再试")
    assert not _looks_like_auth_page("当前访问频繁，请稍后再试")
    assert _looks_like_auth_page("请完成下列验证码")
    assert not _looks_like_auth_page(
        '<script>const route = "captcha";</script><main>正常主页</main>'
    )
    assert not _looks_like_transient_limit("网络环境存在风险，请稍后再试")
    assert _looks_like_request_rejected("网络环境存在风险，请稍后再试")
    assert not _looks_like_auth_page("网络环境存在风险，请稍后再试")


@pytest.mark.parametrize(
    "visible_text",
    [
        "普通作品标题：验证码背后的设计",
        "本期讨论 CAPTCHA 与安全验证的历史",
        "登录后有哪些新功能",
    ],
)
def test_douyin_auth_words_in_normal_visible_content_are_not_actionable(
    visible_text: str,
) -> None:
    assert _auth_page_issue_code(f"<main>{visible_text}</main>") is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("<main>请先登录后继续</main>", SiteIssueCode.LOGIN_REQUIRED),
        (
            "<main>Please complete the CAPTCHA</main>",
            SiteIssueCode.VERIFICATION_REQUIRED,
        ),
        (
            "<script>const route='captcha';</script><main>正常主页</main>",
            None,
        ),
    ],
)
def test_douyin_visible_auth_page_distinguishes_login_and_verification(
    value: str,
    expected: SiteIssueCode | None,
) -> None:
    assert _auth_page_issue_code(value) == expected


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
    ("url", "expected"),
    [
        ("https://www.douyin.com/login", SiteIssueCode.LOGIN_REQUIRED),
        (
            "https://www.douyin.com/passport/web/login",
            SiteIssueCode.LOGIN_REQUIRED,
        ),
        (
            "https://sso.douyin.com/verify",
            SiteIssueCode.VERIFICATION_REQUIRED,
        ),
        (
            "https://www.douyin.com/captcha/?from=profile",
            SiteIssueCode.VERIFICATION_REQUIRED,
        ),
        ("https://evil.example/login", None),
    ],
)
def test_douyin_auth_url_distinguishes_login_and_verification(
    url: str,
    expected: SiteIssueCode | None,
) -> None:
    assert _explicit_douyin_auth_url_issue_code(url) == expected


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
    statuses: list[str] = []

    def fetch_detail(requested_media_id, **kwargs):
        calls.append((requested_media_id, kwargs))
        return {
            "aweme_id": media_id,
            "author": {"sec_uid": profile_id, "nickname": "Test Author"},
            "video": {
                "play_addr": {
                    "uri": video_uri,
                    "width": 1440,
                    "height": 2560,
                    "url_list": ["https://v26-web.douyinvod.com/item-target-1440.mp4"],
                }
            },
        }

    monkeypatch.setattr("app.douyin.fetch_signed_aweme_detail", fetch_detail)
    monkeypatch.setattr(
        "app.douyin.fetch_signed_profile_awemes",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError(
                "The profile feed must not be requested after detail success"
            )
        ),
    )

    result = discover_item_metadata_from_profile(
        profile_id,
        media_id,
        prefer_exact_detail=True,
        status_callback=statuses.append,
    )

    assert result and result["media_id"] == media_id
    assert result["owner_id"] == profile_id
    assert result["video_uri"] == video_uri
    assert result["minimum_width"] == 1440
    assert result["minimum_height"] == 2560
    assert result["direct_candidates"][0]["urls"] == [
        "https://v26-web.douyinvod.com/item-target-1440.mp4"
    ]
    assert len(calls) == 1
    assert calls[0][0] == media_id
    assert calls[0][1]["expected_sec_uid"] == profile_id
    assert calls[0][1]["verification_url"] == (
        f"https://www.douyin.com/video/{media_id}"
    )
    assert calls[0][1]["status_callback"] == statuses.append
    assert calls[0][1]["progress_budget"] is not None


def test_item_metadata_profile_lookup_uses_targeted_feed_by_default(
    monkeypatch,
) -> None:
    profile_id = "MS4wLjABAAAAexpected"
    media_id = "1111111111111111111"
    video_uri = "v0200fg10000fixturevideoid"
    feed_calls = []
    statuses: list[str] = []

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
                        "url_list": ["https://v26-web.douyinvod.com/item-target.mp4"],
                    }
                },
            }
        ]

    monkeypatch.setattr("app.douyin.fetch_signed_profile_awemes", fetch_feed)

    result = discover_item_metadata_from_profile(
        profile_id,
        media_id,
        status_callback=statuses.append,
    )

    assert result and result["media_id"] == media_id
    assert len(feed_calls) == 1
    assert feed_calls[0][2]["target_aweme_id"] == media_id
    assert feed_calls[0][2]["status_callback"] == statuses.append
    assert feed_calls[0][2]["progress_budget"] is not None


def test_item_metadata_preferred_detail_falls_back_to_targeted_feed(
    monkeypatch,
) -> None:
    profile_id = "MS4wLjABAAAAexpected"
    media_id = "1111111111111111111"
    video_uri = "v0200fg10000fixturevideoid"
    feed_calls = []

    detail_budgets = []

    def fetch_detail(*args, **kwargs):
        detail_budgets.append(kwargs["progress_budget"])
        raise TemporaryAccessError("detail endpoint was temporarily limited")

    monkeypatch.setattr("app.douyin.fetch_signed_aweme_detail", fetch_detail)

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
                        "url_list": ["https://v26-web.douyinvod.com/feed-target.mp4"],
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
    assert feed_calls[0][2]["progress_budget"] is detail_budgets[0]


@pytest.mark.parametrize(
    ("response_media_id", "response_profile_id"),
    [
        ("2222222222222222222", "MS4wLjABAAAAexpected"),
        ("1111111111111111111", "MS4wLjABAAAAdifferent"),
    ],
)
def test_item_metadata_preferred_detail_rejects_crosswired_identity_without_feed(
    monkeypatch,
    response_media_id: str,
    response_profile_id: str,
) -> None:
    profile_id = "MS4wLjABAAAAexpected"
    media_id = "1111111111111111111"

    monkeypatch.setattr(
        "app.douyin.fetch_signed_aweme_detail",
        lambda *args, **kwargs: {
            "aweme_id": response_media_id,
            "author": {
                "sec_uid": response_profile_id,
                "nickname": "Cross-wired Author",
            },
            "video": {
                "play_addr": {
                    "uri": "v0200fg10000crosswiredvideoid",
                    "width": 1440,
                    "height": 2560,
                    "url_list": ["https://v26-web.douyinvod.com/cross-wired.mp4"],
                }
            },
        },
    )
    monkeypatch.setattr(
        "app.douyin.fetch_signed_profile_awemes",
        lambda *args, **kwargs: pytest.fail(
            "Cross-wired exact detail must fail closed without a feed fallback"
        ),
    )

    with pytest.raises(DiscoveryError, match="without complete, verified") as error:
        discover_item_metadata_from_profile(
            profile_id,
            media_id,
            prefer_exact_detail=True,
        )

    assert error.value.issue_code == SiteIssueCode.SITE_RESPONSE_CHANGED


def test_item_metadata_preferred_detail_integrity_failure_does_not_use_feed(
    monkeypatch,
) -> None:
    profile_id = "MS4wLjABAAAAexpected"
    media_id = "1111111111111111111"
    expected_error = DiscoveryError(
        "Douyin detail API returned a different aweme",
        issue_code=SiteIssueCode.SITE_RESPONSE_CHANGED,
    )

    monkeypatch.setattr(
        "app.douyin.fetch_signed_aweme_detail",
        lambda *args, **kwargs: (_ for _ in ()).throw(expected_error),
    )
    monkeypatch.setattr(
        "app.douyin.fetch_signed_profile_awemes",
        lambda *args, **kwargs: pytest.fail(
            "An exact-detail integrity failure must not use the profile feed"
        ),
    )

    with pytest.raises(DiscoveryError) as captured:
        discover_item_metadata_from_profile(
            profile_id,
            media_id,
            prefer_exact_detail=True,
        )

    assert captured.value is expected_error


def test_douyin_signed_profile_discovery_returns_verified_complete_metadata(
    monkeypatch,
) -> None:
    profile_id = "MS4wLjABAAAAexpected"
    profile_url = f"https://www.douyin.com/user/{profile_id}"
    aweme_id = "1111111111111111111"
    video_uri = "v0200fg10000fixturevideoid"
    video_url = "https://v26-web.douyinvod.com/signed-profile-1440.mp4"
    signed_calls = []

    def fetch_signed(*args, **kwargs):
        signed_calls.append((args, kwargs))
        return [
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
        ]

    monkeypatch.setattr(
        "app.douyin.fetch_signed_profile_awemes",
        fetch_signed,
    )
    statuses: list[str] = []

    result = discover_profile(
        profile_url,
        use_browser_cookies=True,
        status_callback=statuses.append,
    )

    assert result.author == "Signed Author"
    assert result.video_urls == [f"https://www.douyin.com/video/{aweme_id}"]
    assert result.discovery_complete is True
    assert len(signed_calls) == 1
    assert signed_calls[0][1]["status_callback"] == statuses.append
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

    with pytest.raises(TemporaryAccessError) as captured:
        discover_profile(profile_url, use_browser_cookies=True)

    assert captured.value.issue_code == SiteIssueCode.NETWORK_ERROR
    assert "no CAPTCHA or rate-limit response" in str(captured.value)


@pytest.mark.parametrize(
    ("browser_error", "expected_error", "expected_code"),
    [
        (
            "当前访问频繁，请稍后再试",
            TemporaryAccessError,
            SiteIssueCode.RATE_LIMITED,
        ),
        (
            "网络环境存在风险，请稍后再试",
            TemporaryAccessError,
            SiteIssueCode.REQUEST_REJECTED,
        ),
        (
            "Executable doesn't exist at /missing/chrome",
            DiscoveryError,
            SiteIssueCode.LOCAL_CONFIGURATION,
        ),
    ],
)
def test_douyin_playwright_failure_has_structured_site_classification(
    monkeypatch,
    browser_error: str,
    expected_error: type[Exception],
    expected_code: SiteIssueCode,
) -> None:
    from playwright.sync_api import Error as PlaywrightError

    profile_id = "MS4wLjABAAAAexpected"
    profile_url = f"https://www.douyin.com/user/{profile_id}"

    def signed_profile_failure(*args, **kwargs):
        raise AuthenticationRequiredError(
            "Signed profile unavailable",
            verification_url=profile_url,
        )

    class FailingPlaywrightContext:
        def __enter__(self):
            raise PlaywrightError(browser_error)

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

    monkeypatch.setattr(
        "app.douyin.fetch_signed_profile_awemes",
        signed_profile_failure,
    )
    monkeypatch.setattr("app.douyin._extract_cookies", lambda profile: CookieJar())
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: FailingPlaywrightContext(),
    )

    with pytest.raises(expected_error) as captured:
        discover_profile(profile_url, use_browser_cookies=True)

    assert captured.value.issue_code == expected_code


def test_douyin_browser_fallback_shares_budget_and_stops_without_progress(
    monkeypatch,
) -> None:
    profile_id = "MS4wLjABAAAAexpected"
    profile_url = f"https://www.douyin.com/user/{profile_id}"
    clock = FakeClock()
    budget = FakeProgressBudget(clock)
    statuses: list[str] = []
    signed_budgets = []

    class FakeBody:
        @property
        def first(self):
            return self

        def count(self) -> int:
            return 1

        def inner_text(self, timeout: int) -> str:
            return "Normal profile page"

    class FakePage:
        url = profile_url

        def __init__(self) -> None:
            self.mouse = self
            self.events: list[str] = []
            self.waits: list[int] = []
            self.goto_timeout = 0
            self.scrolls = 0
            self.header_response_received = False
            self.response_json_calls = 0

        def on(self, event: str, callback) -> None:
            self.events.append(event)

        def goto(self, url: str, wait_until: str, timeout: int) -> None:
            self.url = url
            self.goto_timeout = timeout
            self.header_response_received = True

        def wait_for_timeout(self, timeout: int) -> None:
            self.waits.append(timeout)
            clock.advance(timeout / 1_000)

        def locator(self, selector: str) -> FakeBody:
            return FakeBody()

        def content(self) -> str:
            return "<html><body>Normal profile page</body></html>"

        def title(self) -> str:
            return "Normal profile - Douyin"

        def wheel(self, x: int, y: int) -> None:
            self.scrolls += 1

        def evaluate(self, script: str) -> None:
            return None

    page = FakePage()
    _install_fake_douyin_browser(monkeypatch, page)

    def signed_failure(*args, **kwargs):
        signed_budgets.append(kwargs["progress_budget"])
        clock.advance(100)
        raise DiscoveryError("Signed integrity failure")

    monkeypatch.setattr("app.douyin.new_signed_discovery_budget", lambda: budget)
    monkeypatch.setattr(
        "app.douyin.fetch_signed_profile_awemes",
        signed_failure,
    )

    with pytest.raises(
        TemporaryAccessError,
        match="120 seconds without new verified profile media",
    ) as captured:
        discover_profile(
            profile_url,
            use_browser_cookies=True,
            max_scrolls=300,
            stable_rounds=1_000,
            status_callback=statuses.append,
        )

    assert signed_budgets == [budget]
    assert clock.now == pytest.approx(120, abs=0.01)
    assert page.goto_timeout <= 20_000
    assert page.scrolls < 300
    assert page.events == ["requestfinished"]
    assert page.header_response_received is True
    assert page.response_json_calls == 0
    assert page.waits and all(0 < value <= 4_000 for value in page.waits)
    assert any(
        "Starting bounded Douyin browser profile fallback" in s for s in statuses
    )
    assert any("Scanning Douyin browser fallback round" in s for s in statuses)
    assert all(profile_id not in status for status in statuses)
    assert "PRIVATE_FAKE_BUDGET_TIMEOUT" not in str(captured.value)


def test_douyin_browser_budget_refreshes_only_for_new_verified_items(
    monkeypatch,
) -> None:
    profile_id = "MS4wLjABAAAAexpected"
    profile_url = f"https://www.douyin.com/user/{profile_id}"
    media_ids = [
        "1111111111111111111",
        "2222222222222222222",
        "3333333333333333333",
    ]
    clock = FakeClock()
    budget = FakeProgressBudget(clock)
    statuses: list[str] = []

    def response_data(
        media_id: str,
        has_more: bool,
        owner_id: str = profile_id,
    ) -> dict:
        return {
            "has_more": int(has_more),
            "aweme_list": [
                {
                    "aweme_id": media_id,
                    "desc": f"Video {media_id}",
                    "author": {
                        "sec_uid": owner_id,
                        "nickname": "Verified Author",
                    },
                    "video": {
                        "play_addr": {
                            "uri": f"video-{media_id}",
                            "width": 1080,
                            "height": 1920,
                            "url_list": [
                                f"https://v26-web.douyinvod.com/{media_id}.mp4"
                            ],
                        }
                    },
                }
            ],
        }

    class FakeResponse:
        def __init__(self, data: dict) -> None:
            self.url = (
                "https://www.douyin.com/aweme/v1/web/aweme/post/"
                f"?sec_user_id={profile_id}"
            )
            self.data = data

        def json(self) -> dict:
            return self.data

    class FakeRequest:
        def __init__(self, data: dict) -> None:
            self.value = FakeResponse(data)

        def response(self) -> FakeResponse:
            return self.value

    class FakeBody:
        @property
        def first(self):
            return self

        def count(self) -> int:
            return 1

        def inner_text(self, timeout: int) -> str:
            return "Normal profile page"

    class FakePage:
        url = profile_url

        def __init__(self) -> None:
            self.mouse = self
            self.callback = None
            self.wheel_index = 0

        def on(self, event: str, callback) -> None:
            assert event == "requestfinished"
            self.callback = callback

        def emit(
            self,
            media_id: str,
            has_more: bool,
            owner_id: str = profile_id,
        ) -> None:
            assert self.callback is not None
            self.callback(FakeRequest(response_data(media_id, has_more, owner_id)))

        def goto(self, url: str, wait_until: str, timeout: int) -> None:
            self.url = url
            self.emit(media_ids[0], True)

        def wait_for_timeout(self, timeout: int) -> None:
            clock.advance(timeout / 1_000)

        def locator(self, selector: str) -> FakeBody:
            return FakeBody()

        def content(self) -> str:
            return "<html><body>Normal profile page</body></html>"

        def wheel(self, x: int, y: int) -> None:
            delays = [50, 50, 100]
            emitted_ids = [media_ids[0], media_ids[1], media_ids[2]]
            has_more_values = [False, True, False]
            owner_ids = ["MS4wLjABAAAAother", profile_id, profile_id]
            clock.advance(delays[self.wheel_index])
            if self.wheel_index == 0:
                assert self.callback is not None
                self.callback(FakeRequest({"has_more": 0, "aweme_list": []}))
            self.emit(
                emitted_ids[self.wheel_index],
                has_more_values[self.wheel_index],
                owner_ids[self.wheel_index],
            )
            self.wheel_index += 1

        def evaluate(self, script: str) -> None:
            return None

    page = FakePage()
    _install_fake_douyin_browser(monkeypatch, page)
    monkeypatch.setattr("app.douyin.new_signed_discovery_budget", lambda: budget)

    result = discover_profile(
        profile_url,
        max_scrolls=4,
        stable_rounds=4,
        status_callback=statuses.append,
    )

    assert clock.now > 200
    assert budget.refresh_calls == 3
    assert page.wheel_index == 3
    assert result.video_urls == [
        f"https://www.douyin.com/video/{media_id}" for media_id in media_ids
    ]
    added_statuses = [status for status in statuses if "added" in status]
    assert len(added_statuses) == 3
    assert "1 total" in added_statuses[0]
    assert "2 total" in added_statuses[1]
    assert "3 total" in added_statuses[2]


@pytest.mark.parametrize(
    ("redirect_url", "cancel_after_deadline", "expected_error"),
    [
        ("https://www.douyin.com/login", False, AuthenticationRequiredError),
        (None, True, DownloadCancelledError),
    ],
)
def test_douyin_browser_auth_and_cancel_take_priority_over_budget_expiry(
    monkeypatch,
    redirect_url: str | None,
    cancel_after_deadline: bool,
    expected_error: type[Exception],
) -> None:
    profile_id = "MS4wLjABAAAAexpected"
    profile_url = f"https://www.douyin.com/user/{profile_id}"
    clock = FakeClock()
    budget = FakeProgressBudget(clock)

    class FakePage:
        url = profile_url

        def on(self, event: str, callback) -> None:
            assert event == "requestfinished"

        def goto(self, url: str, wait_until: str, timeout: int) -> None:
            clock.advance(2)
            self.url = redirect_url or url

    page = FakePage()
    _install_fake_douyin_browser(monkeypatch, page)

    def signed_failure(*args, **kwargs):
        clock.advance(119)
        if redirect_url:
            raise AuthenticationRequiredError(
                "Explicit signed authentication",
                verification_url=profile_url,
            )
        raise DiscoveryError("Signed integrity failure")

    monkeypatch.setattr("app.douyin.new_signed_discovery_budget", lambda: budget)
    monkeypatch.setattr(
        "app.douyin.fetch_signed_profile_awemes",
        signed_failure,
    )

    with pytest.raises(expected_error) as captured:
        discover_profile(
            profile_url,
            should_cancel=(
                (lambda: clock.now >= 120) if cancel_after_deadline else None
            ),
        )

    assert "PRIVATE_FAKE_BUDGET_TIMEOUT" not in str(captured.value)
    if isinstance(captured.value, AuthenticationRequiredError):
        assert captured.value.verification_url == profile_url


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
    ("scroll_text", "expected_issue"),
    [
        (None, None),
        (
            "网络环境存在风险，请稍后再试",
            SiteIssueCode.REQUEST_REJECTED,
        ),
    ],
)
def test_douyin_scroll_non_auth_content_does_not_request_verification(
    monkeypatch,
    scroll_text: str | None,
    expected_issue: SiteIssueCode | None,
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

    class FakeRequest:
        def response(self) -> FakeResponse:
            return FakeResponse()

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
            if event == "requestfinished":
                self.response_callback = callback

        def goto(self, url: str, wait_until: str, timeout: int) -> None:
            self.url = url
            assert self.response_callback is not None
            self.response_callback(FakeRequest())

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

    if expected_issue is not None:
        with pytest.raises(TemporaryAccessError) as captured:
            discover_profile(profile_url, max_scrolls=1)
        assert captured.value.issue_code == expected_issue
        return

    result = discover_profile(profile_url, max_scrolls=1)
    assert result.author == "Verified Author"
    assert result.video_urls == [f"https://www.douyin.com/video/{media_id}"]
    assert result.discovery_complete is False


@pytest.mark.parametrize(
    ("redirect_url", "expected_error", "expected_code"),
    [
        (
            "https://www.douyin.com/login",
            AuthenticationRequiredError,
            SiteIssueCode.LOGIN_REQUIRED,
        ),
        (
            "https://www.douyin.com/captcha",
            AuthenticationRequiredError,
            SiteIssueCode.VERIFICATION_REQUIRED,
        ),
        ("https://evil.example/login", DiscoveryError, None),
    ],
)
def test_douyin_browser_redirect_requires_trusted_explicit_auth_url(
    monkeypatch,
    redirect_url: str,
    expected_error: type[Exception],
    expected_code: SiteIssueCode | None,
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
        assert captured.value.issue_code == expected_code


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
                    "url_list": ["https://v26-web.douyinvod.com/verified-265.mp4"],
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


def test_douyin_live_photo_uses_outer_video_dimensions_when_address_omits_them() -> (
    None
):
    profile_id = "MS4wLjABAAAAexpected"
    media_id = "7683074221437746170"
    result = _minimal_aweme_metadata(
        {
            "aweme_id": media_id,
            "aweme_type": 68,
            "author": {"sec_uid": profile_id},
            "images": [
                {
                    "width": 1440,
                    "height": 2560,
                    "url_list": ["https://p3-pc-sign.douyinpic.com/live-cover.webp"],
                    "video": {
                        "width": 1080,
                        "height": 1920,
                        "play_addr": {
                            "uri": "v0200fg10000outerdimensions",
                            "url_list": ["https://v26-web.douyinvod.com/live-play.mp4"],
                        },
                    },
                }
            ],
        },
        profile_id,
    )

    assert result is not None
    live = result[1]["live_photo_assets"][0]
    assert (live["width"], live["height"]) == (1080, 1920)
    assert (
        live["direct_candidates"][0]["width"],
        live["direct_candidates"][0]["height"],
    ) == (
        1080,
        1920,
    )


def test_douyin_live_photo_uses_unwatermarked_download_addr_as_last_resort() -> None:
    profile_id = "MS4wLjABAAAAexpected"
    media_id = "7683074221437746170"
    result = _minimal_aweme_metadata(
        {
            "aweme_id": media_id,
            "aweme_type": 68,
            "author": {"sec_uid": profile_id},
            "images": [
                {
                    "width": 1440,
                    "height": 2560,
                    "url_list": ["https://p3-pc-sign.douyinpic.com/live-cover.webp"],
                    "video": {
                        "width": 1080,
                        "height": 1920,
                        "has_watermark": 0,
                        "download_addr": {
                            "uri": "v0200fg10000downloadaddress",
                            "width": 720,
                            "height": 720,
                            "url_list": [
                                "https://v26-web.douyinvod.com/live-download.mp4"
                            ],
                        },
                    },
                }
            ],
        },
        profile_id,
    )

    assert result is not None
    live = result[1]["live_photo_assets"][0]
    assert (live["width"], live["height"]) == (1080, 1920)
    assert (
        live["direct_candidates"][0]["width"],
        live["direct_candidates"][0]["height"],
    ) == (720, 1280)
    assert live["video_uri"] == "v0200fg10000downloadaddress"
    assert live["candidates"] == ["https://v26-web.douyinvod.com/live-download.mp4"]


def test_douyin_live_photo_never_uses_watermarked_download_addr() -> None:
    profile_id = "MS4wLjABAAAAexpected"
    media_id = "7683074221437746170"
    result = _minimal_aweme_metadata(
        {
            "aweme_id": media_id,
            "aweme_type": 68,
            "author": {"sec_uid": profile_id},
            "images": [
                {
                    "width": 1440,
                    "height": 2560,
                    "url_list": ["https://p3-pc-sign.douyinpic.com/live-cover.webp"],
                    "video": {
                        "width": 1080,
                        "height": 1920,
                        "has_watermark": 1,
                        "download_addr": {
                            "uri": "v0200fg10000watermarkeddownload",
                            "width": 1080,
                            "height": 1920,
                            "url_list": [
                                "https://v26-web.douyinvod.com/watermarked.mp4"
                            ],
                        },
                    },
                }
            ],
        },
        profile_id,
    )

    assert result is not None
    assert "live_photo_assets" not in result[1]
    assert result[1]["live_photo_static_fallback_indexes"] == [1]


@pytest.mark.parametrize("watermark_value", [None, 0.0, "0"])
def test_douyin_live_photo_requires_explicit_unwatermarked_download_addr(
    watermark_value,
) -> None:
    profile_id = "MS4wLjABAAAAexpected"
    media_id = "7683074221437746170"
    result = _minimal_aweme_metadata(
        {
            "aweme_id": media_id,
            "aweme_type": 68,
            "author": {"sec_uid": profile_id},
            "images": [
                {
                    "width": 1440,
                    "height": 2560,
                    "url_list": ["https://p3-pc-sign.douyinpic.com/live-cover.webp"],
                    "video": {
                        "width": 1080,
                        "height": 1920,
                        "has_watermark": watermark_value,
                        "download_addr": {
                            "uri": "v0200fg10000unknownwatermark",
                            "width": 1080,
                            "height": 1920,
                            "url_list": ["https://v26-web.douyinvod.com/unknown.mp4"],
                        },
                    },
                }
            ],
        },
        profile_id,
    )

    assert result is not None
    assert "live_photo_assets" not in result[1]
    assert result[1]["live_photo_static_fallback_indexes"] == [1]


def test_douyin_live_photo_rejects_conflicting_visible_play_and_download_ids() -> None:
    profile_id = "MS4wLjABAAAAexpected"
    media_id = "7683074221437746170"
    result = _minimal_aweme_metadata(
        {
            "aweme_id": media_id,
            "aweme_type": 68,
            "author": {"sec_uid": profile_id},
            "images": [
                {
                    "width": 1440,
                    "height": 2560,
                    "url_list": ["https://p3-pc-sign.douyinpic.com/live-cover.webp"],
                    "video": {
                        "width": 1080,
                        "height": 1920,
                        "has_watermark": 0,
                        "play_addr": {
                            "uri": "v0200fg10000visibleplayidentity",
                            "url_list": ["https://evil.example/play.mp4"],
                        },
                        "download_addr": {
                            "uri": "v0200fg10000differentdownload",
                            "width": 1080,
                            "height": 1920,
                            "url_list": ["https://v26-web.douyinvod.com/download.mp4"],
                        },
                    },
                }
            ],
        },
        profile_id,
    )

    assert result is not None
    assert "live_photo_assets" not in result[1]
    assert result[1]["live_photo_static_fallback_indexes"] == [1]


def test_douyin_live_photo_does_not_drop_untrusted_higher_rendition() -> None:
    profile_id = "MS4wLjABAAAAexpected"
    media_id = "7683074221437746170"
    live_uri = "v0200fg10000sameverifiedidentity"
    result = _minimal_aweme_metadata(
        {
            "aweme_id": media_id,
            "aweme_type": 68,
            "author": {"sec_uid": profile_id},
            "images": [
                {
                    "width": 1440,
                    "height": 2560,
                    "url_list": ["https://p3-pc-sign.douyinpic.com/live-cover.webp"],
                    "video": {
                        "play_addr": {
                            "uri": live_uri,
                            "width": 1080,
                            "height": 1920,
                            "url_list": [
                                "https://unrecognized.example/high-original.mp4"
                            ],
                        },
                        "play_addr_h264": {
                            "uri": live_uri,
                            "width": 720,
                            "height": 1280,
                            "url_list": [
                                "https://v11-web.douyinvod.com/trusted-low.mp4"
                            ],
                        },
                    },
                }
            ],
        },
        profile_id,
    )

    assert result is not None
    assert "live_photo_assets" not in result[1]
    assert result[1]["live_photo_static_fallback_indexes"] == [1]


def test_douyin_live_photo_does_not_drop_unidentified_higher_rendition() -> None:
    profile_id = "MS4wLjABAAAAexpected"
    media_id = "7683074221437746170"
    result = _minimal_aweme_metadata(
        {
            "aweme_id": media_id,
            "aweme_type": 68,
            "author": {"sec_uid": profile_id},
            "images": [
                {
                    "width": 1440,
                    "height": 2560,
                    "url_list": ["https://p3-pc-sign.douyinpic.com/live-cover.webp"],
                    "video": {
                        "play_addr": {
                            "width": 1080,
                            "height": 1920,
                            "url_list": [
                                "https://v26-web.douyinvod.com/high-without-id.mp4"
                            ],
                        },
                        "play_addr_h264": {
                            "uri": "v0200fg10000identifiedlower",
                            "width": 720,
                            "height": 1280,
                            "url_list": [
                                "https://v11-web.douyinvod.com/identified-low.mp4"
                            ],
                        },
                    },
                }
            ],
        },
        profile_id,
    )

    assert result is not None
    assert "live_photo_assets" not in result[1]
    assert result[1]["live_photo_static_fallback_indexes"] == [1]


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
                "candidates": ["https://p3-pc-sign.douyinpic.com/static-only.webp"],
            }
        ],
        "live_photo_static_fallback_indexes": [1],
    }

    assert is_complete_profile_media_metadata(metadata, media_id, profile_id)


@pytest.mark.parametrize("fallback_indexes", [[], [2], [1, 1], ["1"]])
def test_douyin_static_fallback_indexes_must_bind_unique_image_positions(
    fallback_indexes,
) -> None:
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
                "candidates": ["https://p3-pc-sign.douyinpic.com/static-only.webp"],
            }
        ],
        "live_photo_static_fallback_indexes": fallback_indexes,
    }

    assert not is_complete_profile_media_metadata(metadata, media_id, profile_id)


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
                "candidates": ["https://p3-pc-sign.douyinpic.com/live-cover.webp"],
            }
        ],
        "live_photo_assets": [
            {
                "index": 1,
                "width": 1080,
                "height": 1920,
                "candidates": ["https://v26-web.douyinvod.com/live-without-direct.mp4"],
                "video_uri": "v0200fg10000livephotoasset",
            }
        ],
    }

    assert not is_complete_profile_media_metadata(metadata, media_id, profile_id)


def test_douyin_live_photo_uses_static_when_media_identities_conflict() -> None:
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
                    "url_list": ["https://p3-pc-sign.douyinpic.com/photo.webp"],
                    "video": {
                        "play_addr": {
                            "uri": "v0200fg10000liveidentityA",
                            "width": 1080,
                            "height": 1920,
                            "url_list": ["https://v26-web.douyinvod.com/live-a.mp4"],
                        },
                        "play_addr_h264": {
                            "uri": "v0200fg10000liveidentityB",
                            "width": 1080,
                            "height": 1920,
                            "url_list": ["https://v11-web.douyinvod.com/live-b.mp4"],
                        },
                    },
                }
            ],
        },
        profile_id,
    )

    assert result is not None
    assert result[1]["media_kind"] == "image"
    assert result[1]["live_photo_static_fallback_indexes"] == [1]
    assert "live_photo_assets" not in result[1]


def test_douyin_live_photo_uses_static_when_rendition_identity_conflicts() -> None:
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
                    "url_list": ["https://p3-pc-sign.douyinpic.com/photo.webp"],
                    "video": {
                        "play_addr": {
                            "uri": "v0200fg10000liveidentityA",
                            "width": 720,
                            "height": 1280,
                            "url_list": ["https://v26-web.douyinvod.com/live-a.mp4"],
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

    assert result is not None
    assert result[1]["media_kind"] == "image"
    assert result[1]["live_photo_static_fallback_indexes"] == [1]
    assert "live_photo_assets" not in result[1]


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
                    "url_list": ["https://v26-web.douyinvod.com/profile-a.mp4"],
                },
                "bit_rate": [
                    {
                        "bit_rate": 2_000_000,
                        "play_addr": {
                            "uri": "v0200fg10000profileidentityB",
                            "width": 1080,
                            "height": 1920,
                            "url_list": ["https://v11-web.douyinvod.com/profile-b.mp4"],
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
                    "url_list": ["https://p3-pc-sign.douyinpic.com/photo.webp"],
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
            "urls": ["https://v11-weba.douyinvod.com/verified-1440-landscape.mp4"],
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

    class FakeRequest:
        def __init__(self, data: dict):
            self._response = FakeResponse(data)

        def response(self) -> FakeResponse:
            return self._response

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
            if event == "requestfinished":
                self.callback = callback

        def goto(self, url: str, wait_until: str, timeout: int):
            self.url = url
            self.callback(FakeRequest(response_data("1111111111111111111", True)))

        def wait_for_timeout(self, timeout: int) -> None:
            if self.scrolled and not self.sent_second_page:
                self.sent_second_page = True
                self.callback(FakeRequest(response_data("2222222222222222222", False)))

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
