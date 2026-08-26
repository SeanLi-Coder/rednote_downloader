from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.douyin_signing import (
    _SigningFailure,
    _build_signing_document,
    _extract_sdk_glue_tags,
    _is_douyin_url,
    _validate_detail_response,
    _validate_profile_response,
    fetch_signed_aweme_detail,
    fetch_signed_profile_awemes,
)
from app.errors import AuthenticationRequiredError, DownloadCancelledError


AWEME_ID = "7671259887394052209"
SEC_UID = "MS4wLjABAAAAexpected"
VIDEO_URL = f"https://www.douyin.com/video/{AWEME_ID}"
PROFILE_URL = f"https://www.douyin.com/user/{SEC_UID}"
GLUE_HTML = """
<!doctype html>
<html><head>
  <script src="https://www.douyin.com/unrelated.js"></script>
  <script data-sdk-glue-custom>window.customAnchor = true;</script>
  <script data-sdk-glue-default="pre-handler">window.preHandler = true;</script>
  <script
    data-sdk-glue-default="load"
    src="https://lf-c-flwb.bytetos.com/obj/security/glue/9.9.9/sdk-glue.js"
    onload="window.untrustedHandler = true"
  ></script>
  <script data-sdk-glue-default="init">window.initializeGlue();</script>
</head></html>
"""


def _detail_response(
    *,
    aweme_id: str = AWEME_ID,
    sec_uid: str | None = SEC_UID,
    status_code: int = 0,
    http_status: int = 200,
) -> dict:
    author = {} if sec_uid is None else {"sec_uid": sec_uid}
    return {
        "state": "done",
        "httpStatus": http_status,
        "payload": {
            "status_code": status_code,
            "aweme_detail": {
                "aweme_id": aweme_id,
                "author": author,
                "desc": "Test video",
            },
        },
    }


def _profile_aweme(aweme_id: str, sec_uid: str = SEC_UID) -> dict:
    return {
        "aweme_id": aweme_id,
        "author": {"sec_uid": sec_uid, "nickname": "Test Author"},
        "video": {"play_addr": {"uri": f"video-{aweme_id}"}},
        "desc": f"Video {aweme_id}",
    }


def _profile_response(
    aweme_ids: list[str],
    *,
    has_more: int = 0,
    max_cursor: str | None = None,
    sec_uid: str = SEC_UID,
) -> dict:
    payload = {
        "status_code": 0,
        "sec_uid": sec_uid,
        "has_more": has_more,
        "aweme_list": [_profile_aweme(value, sec_uid) for value in aweme_ids],
    }
    if max_cursor is not None:
        payload["max_cursor"] = max_cursor
    return {"state": "done", "httpStatus": 200, "payload": payload}


def test_extract_sdk_glue_tags_ignores_other_scripts_and_sanitizes_attributes() -> None:
    tags = _extract_sdk_glue_tags(GLUE_HTML)

    assert len(tags) == 4
    assert all("data-sdk-glue" in tag for tag in tags)
    assert "unrelated.js" not in "".join(tags)
    assert "onload" not in "".join(tags)
    assert "glue/9.9.9/sdk-glue.js" in tags[2]
    assert "window.initializeGlue();" in tags[3]


def test_build_signing_document_adds_only_required_fixed_runtimes() -> None:
    document = _build_signing_document(_extract_sdk_glue_tags(GLUE_HTML))

    assert "runtime_bundler_34.js" in document
    assert 'project-id="34"' in document
    assert "webmssdk.es5.js" in document
    assert "glue/9.9.9/sdk-glue.js" in document


@pytest.mark.parametrize(
    "url",
    [
        VIDEO_URL,
        PROFILE_URL,
        f"https://douyin.com/video/{AWEME_ID}",
        f"https://www.douyin.com:443/user/{SEC_UID}/",
    ],
)
def test_douyin_verification_url_accepts_only_expected_https_targets(
    url: str,
) -> None:
    assert _is_douyin_url(url)


@pytest.mark.parametrize(
    "url",
    [
        f"http://www.douyin.com/video/{AWEME_ID}",
        f"https://www.douyin.com.evil.example/video/{AWEME_ID}",
        f"https://evil.example/user/{SEC_UID}",
        "https://www.douyin.com/",
        "https://www.douyin.com/aweme/v1/web/aweme/detail/",
        "https://www.douyin.com/video/not-numeric",
        f"https://www.douyin.com:444/video/{AWEME_ID}",
        f"https://user:password@www.douyin.com/video/{AWEME_ID}",
    ],
)
def test_douyin_verification_url_rejects_unsafe_origin_or_path(url: str) -> None:
    assert not _is_douyin_url(url)


@pytest.mark.parametrize(
    "source_html",
    [
        "<html><script>window.noGlue = true;</script></html>",
        (
            '<script data-sdk-glue="load" '
            'src="https://malicious.example/sdk-glue.js"></script>'
        ),
        '<script data-sdk-glue="init">window.incomplete = true;',
    ],
)
def test_extract_sdk_glue_tags_rejects_missing_or_unsafe_markup(
    source_html: str,
) -> None:
    with pytest.raises(_SigningFailure):
        _extract_sdk_glue_tags(source_html)


def test_validate_detail_response_returns_only_verified_aweme_detail() -> None:
    detail = _validate_detail_response(
        _detail_response(),
        AWEME_ID,
        SEC_UID,
    )

    assert detail["aweme_id"] == AWEME_ID
    assert detail["author"]["sec_uid"] == SEC_UID


@pytest.mark.parametrize(
    "response,expected_sec_uid",
    [
        (_detail_response(http_status=403), SEC_UID),
        (_detail_response(status_code=4), SEC_UID),
        (_detail_response(aweme_id="7000000000000000000"), SEC_UID),
        (_detail_response(sec_uid=None), None),
        (_detail_response(sec_uid="MS4wLjABAAAAother"), SEC_UID),
    ],
)
def test_validate_detail_response_rejects_unverified_identity(
    response: dict,
    expected_sec_uid: str | None,
) -> None:
    with pytest.raises(_SigningFailure):
        _validate_detail_response(response, AWEME_ID, expected_sec_uid)


def test_validate_profile_response_keeps_videos_and_skips_owned_photo_posts() -> None:
    response = _profile_response(
        ["7000000000000000001"],
        has_more=0,
    )
    response["payload"]["aweme_list"].append(
        {
            "aweme_id": "7000000000000000002",
            "author": {"sec_uid": SEC_UID},
            "images": [{"uri": "photo-1"}],
        }
    )

    awemes, has_more, next_cursor = _validate_profile_response(
        response,
        SEC_UID,
    )

    assert [aweme["aweme_id"] for aweme in awemes] == ["7000000000000000001"]
    assert has_more is False
    assert next_cursor is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(sec_uid="MS4wLjABAAAAother"),
        lambda payload: payload["aweme_list"][0].update(aweme_id="not-numeric"),
        lambda payload: payload["aweme_list"][0]["author"].update(
            sec_uid="MS4wLjABAAAAother"
        ),
        lambda payload: payload.pop("max_cursor"),
    ],
)
def test_validate_profile_response_rejects_unverified_pages(mutate) -> None:
    response = _profile_response(
        ["7000000000000000001"],
        has_more=1,
        max_cursor="123",
    )
    mutate(response["payload"])

    with pytest.raises(_SigningFailure):
        _validate_profile_response(response, SEC_UID)


class FakeApiResponse:
    status = 200
    url = VIDEO_URL

    def text(self) -> str:
        return GLUE_HTML


class FakeRoute:
    def __init__(self) -> None:
        self.fulfilled = False
        self.body = ""

    def fulfill(self, **kwargs) -> None:
        self.fulfilled = True
        self.body = kwargs["body"]


class FakeRequest:
    def __init__(self) -> None:
        self.requested_url: str | None = None

    def get(self, url: str, timeout: int) -> FakeApiResponse:
        self.requested_url = url
        return FakeApiResponse()


class FakePage:
    def __init__(self, result: dict | list[dict] | None = None) -> None:
        self.closed = False
        self.route_handler = None
        self.routed_url: str | None = None
        self.goto_url: str | None = None
        self.fulfilled_route = FakeRoute()
        self.started = False
        self.waited = False
        if isinstance(result, list):
            self.results = result
        else:
            self.results = [result or _detail_response()]
        self.signed_requests: list[dict] = []

    def route(self, url: str, handler) -> None:
        self.routed_url = url
        self.route_handler = handler

    def goto(self, url: str, wait_until: str, timeout: int) -> None:
        self.goto_url = url
        self.route_handler(self.fulfilled_route)

    def evaluate(self, script: str, argument=None):
        if "typeof window.useWebSecsdkApi" in script:
            return True
        if argument is not None:
            self.started = True
            self.signed_requests.append(argument)
            return None
        if "const value = window.__originalMediaSignedDetail" in script:
            index = max(0, len(self.signed_requests) - 1)
            return self.results[index]
        return None

    def wait_for_timeout(self, timeout: int) -> None:
        self.waited = True

    def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.request = FakeRequest()
        self.closed = False
        self.cookies = None
        self.new_page_calls = 0

    def add_cookies(self, cookies) -> None:
        self.cookies = cookies

    def new_page(self) -> FakePage:
        self.new_page_calls += 1
        return self.page

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    version = "151.0.0.0"

    def __init__(self, context: FakeContext) -> None:
        self.context = context
        self.closed = False

    def new_context(self, **kwargs) -> FakeContext:
        return self.context

    def close(self) -> None:
        self.closed = True


class FakePlaywrightManager:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.resources_closed_on_exit = False
        self.playwright = SimpleNamespace(
            chromium=SimpleNamespace(launch=lambda **kwargs: browser)
        )

    def __enter__(self):
        return self.playwright

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.resources_closed_on_exit = (
            self.browser.context.page.closed
            and self.browser.context.closed
            and self.browser.closed
        )
        return None


def _install_fake_playwright(
    monkeypatch,
    result: dict | list[dict] | None = None,
):
    page = FakePage(result)
    context = FakeContext(page)
    browser = FakeBrowser(context)
    manager = FakePlaywrightManager(browser)
    context.cookie_jar = object()
    monkeypatch.setattr(
        "app.douyin_signing._load_chrome_cookie_jar",
        lambda profile: context.cookie_jar,
    )
    monkeypatch.setattr(
        "app.douyin_signing._cookie_jar_to_playwright",
        lambda cookie_jar: [
            {
                "name": "sessionid",
                "value": "not-logged",
                "domain": ".douyin.com",
                "path": "/",
            }
        ],
    )
    monkeypatch.setattr(
        "app.douyin_signing._fetch_source_html_with_urllib",
        lambda url, cookie_jar, user_agent, timeout_ms: GLUE_HTML,
    )
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: manager,
    )
    return page, context, browser, manager


def test_fetch_signed_aweme_detail_uses_same_origin_and_closes_resources(
    monkeypatch,
) -> None:
    page, context, browser, manager = _install_fake_playwright(monkeypatch)

    detail = fetch_signed_aweme_detail(
        AWEME_ID,
        verification_url=VIDEO_URL,
        expected_sec_uid=SEC_UID,
        signer_settle_ms=0,
    )

    assert detail["aweme_id"] == AWEME_ID
    assert context.request.requested_url is None
    assert page.routed_url == page.goto_url
    assert page.goto_url.startswith("https://www.douyin.com/")
    assert page.fulfilled_route.fulfilled is True
    assert "glue/9.9.9/sdk-glue.js" in page.fulfilled_route.body
    assert page.started is True
    assert page.signed_requests == [
        {
            "path": "/aweme/v1/web/aweme/detail/",
            "params": {"aweme_id": AWEME_ID},
            "timeoutMs": 45_000,
        }
    ]
    assert page.closed is True
    assert context.closed is True
    assert browser.closed is True
    assert manager.resources_closed_on_exit is True


def test_fetch_prefers_urllib_with_the_original_cookie_jar(
    monkeypatch,
) -> None:
    page, context, browser, manager = _install_fake_playwright(monkeypatch)
    fallback_calls = []

    def fetch_with_urllib(url, cookie_jar, user_agent, timeout_ms):
        fallback_calls.append((url, cookie_jar, user_agent, timeout_ms))
        return GLUE_HTML

    monkeypatch.setattr(
        "app.douyin_signing._fetch_source_html_with_urllib",
        fetch_with_urllib,
    )

    detail = fetch_signed_aweme_detail(
        AWEME_ID,
        verification_url=VIDEO_URL,
        expected_sec_uid=SEC_UID,
        signer_settle_ms=0,
    )

    assert detail["aweme_id"] == AWEME_ID
    assert len(fallback_calls) == 1
    assert fallback_calls[0][0] == VIDEO_URL
    assert fallback_calls[0][1] is context.cookie_jar
    assert "Chrome/151.0.0.0" in fallback_calls[0][2]
    assert context.request.requested_url is None
    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True


def test_fetch_falls_back_to_browser_context_when_urllib_fails(monkeypatch) -> None:
    page, context, browser, manager = _install_fake_playwright(monkeypatch)

    def fail_urllib(url, cookie_jar, user_agent, timeout_ms):
        raise OSError("Temporary network failure")

    monkeypatch.setattr(
        "app.douyin_signing._fetch_source_html_with_urllib",
        fail_urllib,
    )

    detail = fetch_signed_aweme_detail(
        AWEME_ID,
        verification_url=VIDEO_URL,
        expected_sec_uid=SEC_UID,
        signer_settle_ms=0,
    )

    assert detail["aweme_id"] == AWEME_ID
    assert context.request.requested_url == VIDEO_URL
    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True


def test_fetch_errors_are_mapped_without_exposing_cookie_details(monkeypatch) -> None:
    def fail_cookie_read(profile):
        raise OSError("cookie token=do-not-expose")

    monkeypatch.setattr(
        "app.douyin_signing._load_chrome_cookie_jar",
        fail_cookie_read,
    )

    with pytest.raises(AuthenticationRequiredError) as captured:
        fetch_signed_aweme_detail(
            AWEME_ID,
            verification_url=VIDEO_URL,
            signer_settle_ms=0,
        )

    assert captured.value.verification_url == VIDEO_URL
    assert "do-not-expose" not in str(captured.value)


@pytest.mark.parametrize(
    "unsafe_url",
    [
        f"http://www.douyin.com/video/{AWEME_ID}",
        f"https://www.douyin.com.evil.example/video/{AWEME_ID}",
        "https://www.douyin.com/aweme/v1/web/aweme/detail/",
    ],
)
def test_fetch_rejects_unsafe_verification_url_before_cookie_access(
    monkeypatch,
    unsafe_url: str,
) -> None:
    cookie_accessed = False

    def load_cookie_jar(profile):
        nonlocal cookie_accessed
        cookie_accessed = True
        raise AssertionError("Cookie access must not happen")

    monkeypatch.setattr(
        "app.douyin_signing._load_chrome_cookie_jar",
        load_cookie_jar,
    )

    with pytest.raises(AuthenticationRequiredError) as captured:
        fetch_signed_aweme_detail(AWEME_ID, verification_url=unsafe_url)

    assert captured.value.verification_url == unsafe_url
    assert cookie_accessed is False


@pytest.mark.parametrize(
    ("verification_url", "expected_sec_uid"),
    [
        ("https://www.douyin.com/video/7000000000000000000", SEC_UID),
        (PROFILE_URL, None),
        (PROFILE_URL, "MS4wLjABAAAAother"),
    ],
)
def test_detail_fetch_binds_verification_url_identity_before_cookie_access(
    monkeypatch,
    verification_url: str,
    expected_sec_uid: str | None,
) -> None:
    cookie_accessed = False

    def load_cookie_jar(profile):
        nonlocal cookie_accessed
        cookie_accessed = True
        raise AssertionError("Cookie access must not happen")

    monkeypatch.setattr(
        "app.douyin_signing._load_chrome_cookie_jar",
        load_cookie_jar,
    )

    with pytest.raises(AuthenticationRequiredError):
        fetch_signed_aweme_detail(
            AWEME_ID,
            verification_url=verification_url,
            expected_sec_uid=expected_sec_uid,
        )

    assert cookie_accessed is False


def test_invalid_signed_response_maps_to_auth_and_closes_resources(
    monkeypatch,
) -> None:
    page, context, browser, manager = _install_fake_playwright(
        monkeypatch,
        _detail_response(aweme_id="7000000000000000000"),
    )

    with pytest.raises(AuthenticationRequiredError) as captured:
        fetch_signed_aweme_detail(
            AWEME_ID,
            verification_url=VIDEO_URL,
            expected_sec_uid=SEC_UID,
            signer_settle_ms=0,
        )

    assert captured.value.verification_url == VIDEO_URL
    assert page.closed is True
    assert context.closed is True
    assert browser.closed is True
    assert manager.resources_closed_on_exit is True


def test_cancellation_during_signer_wait_closes_resources(monkeypatch) -> None:
    page, context, browser, manager = _install_fake_playwright(monkeypatch)
    cancelled = False

    def wait_and_cancel(timeout: int) -> None:
        nonlocal cancelled
        cancelled = True

    page.evaluate = lambda script, argument=None: (
        False if "typeof window.useWebSecsdkApi" in script else None
    )
    page.wait_for_timeout = wait_and_cancel

    with pytest.raises(DownloadCancelledError):
        fetch_signed_aweme_detail(
            AWEME_ID,
            verification_url=VIDEO_URL,
            should_cancel=lambda: cancelled,
            signer_settle_ms=0,
        )

    assert page.closed is True
    assert context.closed is True
    assert browser.closed is True
    assert manager.resources_closed_on_exit is True


def test_fetch_signed_profile_awemes_paginates_on_one_signer_page(
    monkeypatch,
) -> None:
    page, context, browser, manager = _install_fake_playwright(
        monkeypatch,
        [
            _profile_response(
                ["7000000000000000001", "7000000000000000002"],
                has_more=1,
                max_cursor="123",
            ),
            _profile_response(
                ["7000000000000000002", "7000000000000000003"],
                has_more=0,
                max_cursor="456",
            ),
        ],
    )

    awemes = fetch_signed_profile_awemes(
        PROFILE_URL,
        SEC_UID,
        signer_settle_ms=0,
    )

    assert [aweme["aweme_id"] for aweme in awemes] == [
        "7000000000000000001",
        "7000000000000000002",
        "7000000000000000003",
    ]
    assert context.new_page_calls == 1
    assert [request["path"] for request in page.signed_requests] == [
        "/aweme/v1/web/aweme/post/",
        "/aweme/v1/web/aweme/post/",
    ]
    assert [request["params"]["max_cursor"] for request in page.signed_requests] == [
        "0",
        "123",
    ]
    assert all(
        request["params"]["sec_user_id"] == SEC_UID
        and request["params"]["count"] == "50"
        and request["params"]["pc_client_type"] == "1"
        and request["params"]["update_version_code"] == "170400"
        for request in page.signed_requests
    )
    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True


def test_profile_fetch_retries_transient_empty_signed_response(monkeypatch) -> None:
    empty_response = {
        "state": "done",
        "httpStatus": 200,
        "payload": {"status_code": 0},
    }
    page, context, browser, manager = _install_fake_playwright(
        monkeypatch,
        [
            empty_response,
            _profile_response(
                ["7000000000000000001"],
                has_more=0,
            ),
        ],
    )

    awemes = fetch_signed_profile_awemes(
        PROFILE_URL,
        SEC_UID,
        signer_settle_ms=0,
    )

    assert [aweme["aweme_id"] for aweme in awemes] == ["7000000000000000001"]
    assert [request["params"]["max_cursor"] for request in page.signed_requests] == [
        "0",
        "0",
    ]
    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True


def test_profile_page_limit_maps_to_auth_and_closes_resources(monkeypatch) -> None:
    page, context, browser, manager = _install_fake_playwright(
        monkeypatch,
        [
            _profile_response(
                ["7000000000000000001"],
                has_more=1,
                max_cursor="123",
            )
        ],
    )

    with pytest.raises(AuthenticationRequiredError) as captured:
        fetch_signed_profile_awemes(
            PROFILE_URL,
            SEC_UID,
            max_pages=1,
            signer_settle_ms=0,
        )

    assert captured.value.verification_url == PROFILE_URL
    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True
