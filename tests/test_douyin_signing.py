from __future__ import annotations

import json
from email.message import Message
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError as UrllibHTTPError

import pytest

import app.douyin_signing as douyin_signing
from app.douyin import verified_aweme_metadata
from app.douyin_signing import (
    _AuthenticationSigningFailure,
    _SigningFailure,
    _TransientSigningFailure,
    _build_signing_document,
    _explicit_auth_api_issue_code,
    _explicit_auth_html_issue_code,
    _explicit_auth_url_issue_code,
    _extract_sdk_glue_tags,
    _extract_glue_with_context_fallback,
    _fetch_source_html_with_urllib,
    _is_allowed_douyin_origin,
    _is_douyin_url,
    _is_explicit_auth_url,
    _raise_signing_error,
    _validate_detail_response,
    _validate_profile_response,
    _wait_for_signed_response,
    fetch_signed_aweme_detail,
    fetch_signed_profile_awemes,
)
from app.errors import (
    AuthenticationRequiredError,
    DiscoveryError,
    DownloadCancelledError,
    SiteIssueCode,
    TemporaryAccessError,
)


AWEME_ID = "7671259887394052209"
SEC_UID = "MS4wLjABAAAAexpected"
VIDEO_URL = f"https://www.douyin.com/video/{AWEME_ID}"
NOTE_URL = f"https://www.douyin.com/note/{AWEME_ID}"
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
DNS_FILTER_HTML = """
<!doctype html>
<html lang="en">
  <head>
    <title>Website Filtered</title>
    <link href="https://blocked.dnsfilter.com/main.css" rel="stylesheet">
  </head>
  <body>
    <div id="app"></div>
    <script src="https://blocked.dnsfilter.com/index.js"></script>
  </body>
</html>
"""


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def advance_ms(self, milliseconds: int) -> None:
        self.advance(milliseconds / 1_000)

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)


def test_no_progress_budget_clamps_and_refreshes_from_monotonic_clock(
    monkeypatch,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(
        douyin_signing,
        "time",
        SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep),
    )
    budget = douyin_signing._NoProgressBudget(120)

    clock.advance(119)
    assert budget.remaining_seconds() == pytest.approx(1)
    assert 999 <= budget.clamp_timeout_ms(45_000) <= 1_000

    budget.refresh()
    clock.advance(119)
    assert budget.remaining_seconds() == pytest.approx(1)
    budget.note_failure(
        _TransientSigningFailure(
            "missing profile page",
            category="api-missing-aweme-list",
        )
    )
    clock.advance(1)
    with pytest.raises(
        douyin_signing._SigningNoProgressTimeout,
        match="120-second safety window",
    ) as captured:
        budget.remaining_seconds()

    assert captured.value.category == "api-missing-aweme-list"


def test_signed_profile_honors_an_expired_shared_progress_budget(
    monkeypatch,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(
        douyin_signing,
        "time",
        SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep),
    )
    budget = douyin_signing._NoProgressBudget(120)
    clock.advance(120)
    monkeypatch.setattr(
        douyin_signing,
        "_run_with_signing_page",
        lambda *args, **kwargs: pytest.fail(
            "An expired shared budget must stop before opening another session"
        ),
    )

    with pytest.raises(
        TemporaryAccessError,
        match="120 seconds without verified progress",
    ):
        fetch_signed_profile_awemes(
            PROFILE_URL,
            SEC_UID,
            progress_budget=budget,
        )


def test_serialized_slot_wait_reports_once_and_expires_shared_budget(
    monkeypatch,
) -> None:
    clock = FakeClock()
    statuses: list[str] = []

    class BusyLock:
        def __init__(self) -> None:
            self.timeouts: list[float] = []
            self.release_calls = 0

        def acquire(self, *, timeout: float) -> bool:
            self.timeouts.append(timeout)
            clock.advance(timeout)
            return False

        def release(self) -> None:
            self.release_calls += 1

    lock = BusyLock()
    monkeypatch.setattr(
        douyin_signing,
        "time",
        SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep),
    )
    monkeypatch.setattr(douyin_signing, "_SIGNED_FETCH_LOCK", lock)
    entered = False

    with pytest.raises(douyin_signing._SigningNoProgressTimeout):
        with douyin_signing._serialized_signed_fetch(
            lambda: False,
            budget=douyin_signing._NoProgressBudget(120),
            status_callback=statuses.append,
        ):
            entered = True

    assert entered is False
    assert statuses == ["Waiting for the Douyin signed request slot"]
    assert lock.release_calls == 0
    assert lock.timeouts
    assert all(0 < timeout <= 0.2 for timeout in lock.timeouts)
    assert sum(lock.timeouts) == pytest.approx(120)


def test_chunked_urllib_drip_cannot_extend_shared_deadline(monkeypatch) -> None:
    clock = FakeClock()

    class DripResponse:
        status = 200

        def __init__(self) -> None:
            self.headers = Message()
            self.read_sizes: list[int] = []
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            self.closed = True

        def geturl(self) -> str:
            return VIDEO_URL

        def read(self, size: int) -> bytes:
            self.read_sizes.append(size)
            clock.advance(30)
            return b"x"

    response = DripResponse()

    class DripOpener:
        def open(self, request, timeout: float):
            assert timeout == pytest.approx(45)
            return response

    monkeypatch.setattr(
        douyin_signing,
        "time",
        SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep),
    )
    monkeypatch.setattr(
        douyin_signing,
        "build_opener",
        lambda *args, **kwargs: DripOpener(),
    )

    with pytest.raises(douyin_signing._SigningNoProgressTimeout):
        _fetch_source_html_with_urllib(
            VIDEO_URL,
            object(),
            "Test User Agent",
            45_000,
            budget=douyin_signing._NoProgressBudget(120),
            should_cancel=lambda: False,
        )

    assert len(response.read_sizes) == 4
    assert all(0 < size <= 64 * 1_024 for size in response.read_sizes)
    assert response.closed is True


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
        f"https://www.douyin.com/note/{AWEME_ID}",
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


def test_signing_source_origin_and_explicit_auth_redirect_are_distinct() -> None:
    assert _is_allowed_douyin_origin(VIDEO_URL)
    assert not _is_allowed_douyin_origin("https://sso.douyin.com/verify")
    assert not _is_allowed_douyin_origin("https://evil.example/login")
    assert _is_explicit_auth_url("https://www.douyin.com/passport/safe/verify")
    assert _is_explicit_auth_url("https://www.douyin.com/login")
    assert _is_explicit_auth_url("https://sso.douyin.com/verify")
    assert not _is_explicit_auth_url("https://evil.example/login")
    assert not _is_explicit_auth_url("http://captive.portal/passport/verify")
    assert not _is_explicit_auth_url("https://sso.douyin.com.evil.example/verify")
    assert not _is_explicit_auth_url("https://sso.douyin.com:444/verify")
    assert not _is_explicit_auth_url("https://user@sso.douyin.com/verify")
    assert not _is_explicit_auth_url("https://sso.douyin.com/home")
    assert not _is_explicit_auth_url(PROFILE_URL)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.douyin.com/login", SiteIssueCode.LOGIN_REQUIRED),
        (
            "https://www.douyin.com/passport/web/login",
            SiteIssueCode.LOGIN_REQUIRED,
        ),
        (
            "https://www.douyin.com/passport/safe/verify",
            SiteIssueCode.VERIFICATION_REQUIRED,
        ),
        ("https://sso.douyin.com/captcha", SiteIssueCode.VERIFICATION_REQUIRED),
        ("https://evil.example/login", None),
    ],
)
def test_explicit_auth_url_distinguishes_login_and_verification(
    url: str,
    expected: SiteIssueCode | None,
) -> None:
    assert _explicit_auth_url_issue_code(url) == expected


@pytest.mark.parametrize(
    ("source_html", "expected"),
    [
        (
            "<html><body>Please complete the CAPTCHA</body></html>",
            SiteIssueCode.VERIFICATION_REQUIRED,
        ),
        (
            "<html><body>登录状态已过期，请先登录</body></html>",
            SiteIssueCode.LOGIN_REQUIRED,
        ),
        (
            "<html><body>Normal post"
            "<script>const words = ['captcha', '请先登录'];</script>"
            "</body></html>",
            None,
        ),
    ],
)
def test_explicit_auth_html_uses_visible_text_only(
    source_html: str,
    expected: SiteIssueCode | None,
) -> None:
    assert _explicit_auth_html_issue_code(source_html) == expected


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


def test_validate_detail_response_classifies_images_base_filter_without_identity() -> (
    None
):
    response = {
        "state": "done",
        "httpStatus": 200,
        "payload": {
            "status_code": 0,
            "aweme_detail": None,
            "filter_detail": {"filter_reason": "images_base"},
        },
    }

    with pytest.raises(_TransientSigningFailure) as captured:
        _validate_detail_response(response, AWEME_ID, SEC_UID)

    assert captured.value.category == "api-filtered-images-base"
    assert "different aweme" not in str(captured.value).lower()


def test_validate_detail_response_marks_http_429_as_transient() -> None:
    with pytest.raises(_TransientSigningFailure, match="temporarily limited"):
        _validate_detail_response(
            _detail_response(http_status=429),
            AWEME_ID,
            SEC_UID,
        )


def test_validate_detail_response_marks_bare_http_403_as_transient() -> None:
    with pytest.raises(_TransientSigningFailure, match="temporarily rejected"):
        _validate_detail_response(
            _detail_response(http_status=403),
            AWEME_ID,
            SEC_UID,
        )


def test_validate_detail_response_requires_auth_for_explicit_403_login() -> None:
    response = _detail_response(http_status=403)
    response["payload"]["status_msg"] = "Please login to continue"

    with pytest.raises(
        _AuthenticationSigningFailure,
        match="authentication",
    ) as captured:
        _validate_detail_response(response, AWEME_ID, SEC_UID)

    assert captured.value.issue_code == SiteIssueCode.LOGIN_REQUIRED


@pytest.mark.parametrize(
    ("status_message", "expected"),
    [
        ("Please login to continue", SiteIssueCode.LOGIN_REQUIRED),
        ("Complete the CAPTCHA to continue", SiteIssueCode.VERIFICATION_REQUIRED),
        ("Temporary API response", None),
    ],
)
def test_explicit_auth_api_message_distinguishes_login_and_verification(
    status_message: str,
    expected: SiteIssueCode | None,
) -> None:
    assert _explicit_auth_api_issue_code({"status_msg": status_message}) == expected


@pytest.mark.parametrize(
    ("status_message", "expected_category"),
    [
        ("请求频繁，请稍后再试", "api-rate-limit"),
        ("Temporary API response", "api-status-nonzero"),
    ],
)
def test_nonzero_api_status_classifies_explicit_rate_limit_only(
    status_message: str,
    expected_category: str,
) -> None:
    response = _detail_response(status_code=4)
    response["payload"]["status_msg"] = status_message

    with pytest.raises(_TransientSigningFailure) as captured:
        _validate_detail_response(response, AWEME_ID, SEC_UID)

    assert captured.value.category == expected_category


def test_invalid_json_http_503_is_treated_as_transient() -> None:
    page = FakePage(
        {
            "state": "error",
            "reason": "invalid_json",
            "httpStatus": 503,
        }
    )

    with pytest.raises(_TransientSigningFailure, match="temporary HTTP status"):
        _wait_for_signed_response(page, 1_000, lambda: False)


def test_invalid_json_http_200_is_treated_as_transient_rejection() -> None:
    page = FakePage(
        {
            "state": "error",
            "reason": "invalid_json",
            "httpStatus": 200,
        }
    )

    with pytest.raises(_TransientSigningFailure, match="temporarily rejected"):
        _wait_for_signed_response(page, 1_000, lambda: False)


def test_invalid_json_http_403_is_treated_as_transient_rejection() -> None:
    page = FakePage(
        {
            "state": "error",
            "reason": "invalid_json",
            "httpStatus": 403,
        }
    )

    with pytest.raises(_TransientSigningFailure, match="temporarily rejected"):
        _wait_for_signed_response(page, 1_000, lambda: False)


@pytest.mark.parametrize(
    ("auth_kind", "expected"),
    [
        (SiteIssueCode.LOGIN_REQUIRED.value, SiteIssueCode.LOGIN_REQUIRED),
        (
            SiteIssueCode.VERIFICATION_REQUIRED.value,
            SiteIssueCode.VERIFICATION_REQUIRED,
        ),
    ],
)
def test_invalid_json_signed_response_preserves_auth_kind(
    auth_kind: str,
    expected: SiteIssueCode,
) -> None:
    page = FakePage(
        {
            "state": "error",
            "reason": "invalid_json",
            "httpStatus": 200,
            "authKind": auth_kind,
        }
    )

    with pytest.raises(_AuthenticationSigningFailure) as captured:
        _wait_for_signed_response(page, 1_000, lambda: False)

    assert captured.value.issue_code == expected


def test_missing_sdk_glue_is_transient_but_unsafe_glue_is_not() -> None:
    with pytest.raises(_TransientSigningFailure, match="not present"):
        _extract_sdk_glue_tags("<html><body>temporary shell</body></html>")

    with pytest.raises(_SigningFailure) as captured:
        _extract_sdk_glue_tags(
            '<script data-sdk-glue="load" '
            'src="https://malicious.example/sdk-glue.js"></script>'
        )
    assert not isinstance(captured.value, _TransientSigningFailure)


def test_dnsfilter_replacement_page_is_a_distinct_network_filter_failure() -> None:
    with pytest.raises(
        douyin_signing._NetworkFilterSigningFailure,
        match="local DNS or web filter",
    ) as captured:
        _extract_sdk_glue_tags(DNS_FILTER_HTML)

    assert not isinstance(captured.value, _TransientSigningFailure)
    assert not isinstance(captured.value, _AuthenticationSigningFailure)


@pytest.mark.parametrize(
    "source_html",
    [
        "<html><body>complete the captcha</body></html>",
        "<html><body>请完成下列验证 验证码</body></html>",
        (
            "<html><body>verify you are human"
            "<script>const text = 'temporary shell';</script></body></html>"
        ),
    ],
)
def test_visible_verification_without_sdk_glue_requires_authentication(
    source_html,
) -> None:
    with pytest.raises(_AuthenticationSigningFailure) as captured:
        _extract_sdk_glue_tags(source_html)

    assert captured.value.issue_code == SiteIssueCode.VERIFICATION_REQUIRED


def test_auth_words_inside_hidden_script_do_not_trigger_verification() -> None:
    with pytest.raises(_TransientSigningFailure, match="not present"):
        _extract_sdk_glue_tags(
            "<html><body>temporary shell"
            "<script>const messages = ['captcha', '验证码'];</script>"
            "</body></html>"
        )


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("http-429", SiteIssueCode.RATE_LIMITED),
        ("http-403", SiteIssueCode.REQUEST_REJECTED),
        ("http-5xx", SiteIssueCode.SITE_UNAVAILABLE),
        ("network-timeout", SiteIssueCode.NETWORK_ERROR),
    ],
)
def test_no_progress_timeout_preserves_last_site_category(
    category: str,
    expected: SiteIssueCode,
) -> None:
    failure = douyin_signing._SigningNoProgressTimeout(
        "bounded discovery expired",
        category=category,
    )

    with pytest.raises(TemporaryAccessError) as captured:
        _raise_signing_error(VIDEO_URL, failure)

    assert captured.value.issue_code == expected
    assert f"Reason category: {category}" in str(captured.value)


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (
            _AuthenticationSigningFailure(
                "login response",
                issue_code=SiteIssueCode.LOGIN_REQUIRED,
            ),
            SiteIssueCode.LOGIN_REQUIRED,
        ),
        (
            _AuthenticationSigningFailure(
                "captcha response",
                issue_code=SiteIssueCode.VERIFICATION_REQUIRED,
            ),
            SiteIssueCode.VERIFICATION_REQUIRED,
        ),
    ],
)
def test_raise_signing_error_preserves_authentication_kind(
    failure: _AuthenticationSigningFailure,
    expected: SiteIssueCode,
) -> None:
    with pytest.raises(AuthenticationRequiredError) as captured:
        _raise_signing_error(VIDEO_URL, failure)

    assert captured.value.issue_code == expected
    assert captured.value.verification_url == VIDEO_URL


def test_nonzero_api_status_is_transient_unless_auth_is_explicit() -> None:
    with pytest.raises(_TransientSigningFailure, match="temporarily rejected"):
        _validate_detail_response(
            _detail_response(status_code=4),
            AWEME_ID,
            SEC_UID,
        )

    response = _detail_response(status_code=4)
    response["payload"]["status_msg"] = "Please login to continue"
    with pytest.raises(_AuthenticationSigningFailure, match="authentication"):
        _validate_detail_response(response, AWEME_ID, SEC_UID)


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


def test_validate_profile_response_keeps_owned_video_and_photo_posts() -> None:
    response = _profile_response(
        ["7000000000000000001"],
        has_more=0,
    )
    response["payload"]["aweme_list"].append(
        {
            "aweme_id": "7000000000000000002",
            "author": {"sec_uid": SEC_UID},
            "aweme_type": 68,
            "video": {
                "play_addr": {
                    "uri": "https://lf9-music-east.douyinstatic.com/music.mp3"
                }
            },
            "images": [
                {
                    "uri": "photo-1",
                    "width": 1080,
                    "height": 1920,
                    "url_list": ["https://p3-pc-sign.douyinpic.com/photo-1.webp"],
                }
            ],
        }
    )

    awemes, has_more, next_cursor = _validate_profile_response(
        response,
        SEC_UID,
    )

    assert [aweme["aweme_id"] for aweme in awemes] == [
        "7000000000000000001",
        "7000000000000000002",
    ]
    assert has_more is False
    assert next_cursor is None


def test_validate_profile_response_retries_incomplete_owned_media() -> None:
    response = _profile_response([], has_more=0)
    response["payload"]["aweme_list"] = [
        {
            "aweme_id": "7000000000000000002",
            "author": {"sec_uid": SEC_UID},
        }
    ]

    with pytest.raises(
        _TransientSigningFailure,
        match="incomplete aweme media",
    ) as captured:
        _validate_profile_response(response, SEC_UID)

    assert captured.value.category == "api-incomplete-media"


@pytest.mark.parametrize("response_identity", [None, "", "MS4wLjABAAAAother"])
def test_validate_profile_response_retries_unbound_empty_terminal_page(
    response_identity: str | None,
) -> None:
    response = _profile_response([], has_more=0)
    if response_identity is None:
        response["payload"].pop("sec_uid")
    else:
        response["payload"]["sec_uid"] = response_identity

    with pytest.raises(
        _TransientSigningFailure,
        match="unbound empty terminal page",
    ) as captured:
        _validate_profile_response(response, SEC_UID)

    assert captured.value.category == "api-unbound-empty-page"


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
    def __init__(
        self,
        *,
        status: int = 200,
        url: str = VIDEO_URL,
        body: str = GLUE_HTML,
    ) -> None:
        self.status = status
        self.url = url
        self.body = body
        self.disposed = False

    def text(self) -> str:
        return self.body

    def dispose(self) -> None:
        self.disposed = True


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


class FakePageRequest:
    def __init__(self, url: str, method: str = "GET") -> None:
        self.url = url
        self.method = method
        self._response = None

    def response(self):
        return self._response


class FakePageResponse:
    def __init__(
        self,
        payload: dict,
        *,
        url: str | None = None,
        request_url: str | None = None,
        method: str = "GET",
        status: int = 200,
        body: bytes | None = None,
    ) -> None:
        self._payload = payload
        self.url = url or (
            "https://www.douyin.com/aweme/v1/web/aweme/detail/" f"?aweme_id={AWEME_ID}"
        )
        self.request = FakePageRequest(request_url or self.url, method)
        self.request._response = self
        self.status = status
        self._body = body if body is not None else json.dumps(payload).encode("utf-8")

    def json(self) -> dict:
        return self._payload

    def body(self) -> bytes:
        return self._body


class FakePage:
    def __init__(
        self,
        result: dict | list[dict] | None = None,
        *,
        page_responses: list[FakePageResponse] | None = None,
        page_final_url: str | None = None,
        page_html: str = "<html><body>Douyin item</body></html>",
        pace_snapshot: dict | None = None,
    ) -> None:
        self.closed = False
        self.route_handler = None
        self.routed_url: str | None = None
        self.goto_url: str | None = None
        self.goto_urls: list[str] = []
        self.url = "about:blank"
        self.fulfilled_route = FakeRoute()
        self.started = False
        self.waited = False
        self.page_responses = page_responses or []
        self.page_final_url = page_final_url
        self.page_html = page_html
        self.pace_snapshot = pace_snapshot or {"state": "missing"}
        self.response_handlers = []
        if isinstance(result, list):
            self.results = result
        else:
            self.results = [result or _detail_response()]
        self.signed_requests: list[dict] = []

    def route(self, url: str, handler) -> None:
        self.routed_url = url
        self.route_handler = handler

    def on(self, event: str, handler) -> None:
        assert event == "requestfinished"
        self.response_handlers.append(handler)

    def remove_listener(self, event: str, handler) -> None:
        assert event == "requestfinished"
        if handler in self.response_handlers:
            self.response_handlers.remove(handler)

    def goto(self, url: str, wait_until: str, timeout: int) -> None:
        self.goto_urls.append(url)
        self.goto_url = url
        if url == douyin_signing._SIGNING_PAGE_URL:
            self.url = url
            self.route_handler(self.fulfilled_route)
            return
        self.url = self.page_final_url or url
        for response in self.page_responses:
            for handler in list(self.response_handlers):
                handler(response.request)

    def content(self) -> str:
        return self.page_html

    def evaluate(self, script: str, argument=None):
        if "typeof window.useWebSecsdkApi" in script:
            return True
        if "const source = self.__pace_f" in script:
            return self.pace_snapshot
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
    *,
    page_responses: list[FakePageResponse] | None = None,
    page_final_url: str | None = None,
    page_html: str = "<html><body>Douyin item</body></html>",
    pace_snapshot: dict | None = None,
):
    page = FakePage(
        result,
        page_responses=page_responses,
        page_final_url=page_final_url,
        page_html=page_html,
        pace_snapshot=pace_snapshot,
    )
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
        lambda url, cookie_jar, user_agent, timeout_ms, **kwargs: GLUE_HTML,
    )
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: manager,
    )
    return page, context, browser, manager


def _ssr_live_photo_detail(
    *,
    aweme_id: str = AWEME_ID,
    sec_uid: str = SEC_UID,
    image_url: str = "https://p3-pc-sign.douyinpic.com/media/original.webp",
) -> dict:
    video_uri = "v1e00fgi0000testlivephoto00001"
    video_urls = [
        {"src": "https://v11-weba.douyinvod.com/media/live-photo.mp4"},
        {"src": "https://v26-web.douyinvod.com/media/live-photo.mp4"},
    ]
    return {
        "awemeId": aweme_id,
        "groupId": aweme_id,
        "awemeType": 68,
        "mediaType": 2,
        "createTime": 1_788_855_117,
        "desc": "Test Live Photo",
        "authorInfo": {"secUid": sec_uid, "nickname": "Test Author"},
        "images": [
            {
                "uri": "test-image-uri",
                "width": 2160,
                "height": 2880,
                "urlList": [image_url],
                "downloadUrlList": [image_url],
                "livePhotoType": 1,
                "video": {
                    "uri": video_uri,
                    "width": 720,
                    "height": 960,
                    "duration": 2942,
                    "dataSize": 292_561,
                    "playAddrSize": 292_561,
                    "playAddr": video_urls,
                    "playAddrH265": [],
                    "bitRateList": [
                        {
                            "uri": video_uri,
                            "width": 720,
                            "height": 960,
                            "dataSize": 292_561,
                            "bitRate": 795_543,
                            "realBitrate": 795_543,
                            "isH265": 0,
                            "format": "mp4",
                            "playAddr": video_urls,
                        }
                    ],
                },
            }
        ],
    }


def _ssr_wrapper(
    detail: dict | None = None,
    *,
    aweme_id: str = AWEME_ID,
) -> dict:
    return {
        "awemeId": aweme_id,
        "statusCode": 0,
        "redirect": False,
        "isSpider": False,
        "aweme": {
            "statusCode": 0,
            "isUnknownAweme": False,
            "detail": detail or _ssr_live_photo_detail(),
            "filterDetail": {},
        },
    }


def _ssr_flight_fragments(wrapper: dict | None = None) -> list[str]:
    text_value = "前置🙂Flight 文本"
    text_bytes = text_value.encode("utf-8")
    root = ["$", "div", None, wrapper or _ssr_wrapper()]
    stream = (
        f"8:T{len(text_bytes):x},"
        + text_value
        + "\n7:"
        + json.dumps(root, ensure_ascii=False, separators=(",", ":"))
        + "\n"
    )
    # Deliberately split inside the T header/payload boundary and JSON frame.
    return [stream[:5], stream[5:12], stream[12:37], stream[37:]]


def _pace_snapshot(wrapper: dict | None = None) -> dict:
    fragments = _ssr_flight_fragments(wrapper)
    return {
        "state": "done",
        "fragments": fragments,
        "totalBytes": len("".join(fragments).encode("utf-8")),
    }


def test_flight_parser_handles_segmented_utf8_text_frame_before_json() -> None:
    values = douyin_signing._parse_flight_json_records(_ssr_flight_fragments())

    assert len(values) == 1
    assert values[0][3]["awemeId"] == AWEME_ID
    assert values[0][3]["aweme"]["detail"]["desc"] == "Test Live Photo"


def test_direct_item_uses_identity_bound_live_photo_ssr_without_detail_api(
    monkeypatch,
) -> None:
    page, context, browser, manager = _install_fake_playwright(
        monkeypatch,
        _detail_response(aweme_id="7000000000000000000"),
        pace_snapshot=_pace_snapshot(),
    )

    def fail_if_glue_is_extracted(*args, **kwargs):
        raise AssertionError("SecSDK glue must not be extracted after SSR capture")

    monkeypatch.setattr(
        douyin_signing,
        "_extract_glue_with_context_fallback",
        fail_if_glue_is_extracted,
    )

    detail = fetch_signed_aweme_detail(
        AWEME_ID,
        verification_url=VIDEO_URL,
        expected_sec_uid=SEC_UID,
        signer_settle_ms=0,
    )
    metadata = verified_aweme_metadata(
        detail,
        AWEME_ID,
        expected_profile_id=SEC_UID,
    )

    assert detail["aweme_id"] == AWEME_ID
    assert detail["author"] == {
        "sec_uid": SEC_UID,
        "nickname": "Test Author",
    }
    assert metadata is not None
    assert metadata["media_kind"] == "image"
    assert [
        (asset["width"], asset["height"]) for asset in metadata["image_assets"]
    ] == [(2160, 2880)]
    assert [
        (asset["width"], asset["height"], asset["duration_ms"])
        for asset in metadata["live_photo_assets"]
    ] == [(720, 960, 2942)]
    assert metadata.get("live_photo_static_fallback_indexes") is None
    assert page.goto_urls == [NOTE_URL]
    assert page.started is False
    assert page.signed_requests == []
    assert page.response_handlers == []
    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True


SSR_DISTINCT_GROUP_AWEME_ID = "7649744769275263409"
SSR_DISTINCT_GROUP_ID = "7649743858876479333"


def _ssr_distinct_group_video_wrapper() -> dict:
    detail = _ssr_live_photo_detail(aweme_id=SSR_DISTINCT_GROUP_AWEME_ID)
    detail.update(
        groupId=SSR_DISTINCT_GROUP_ID,
        awemeType=0,
        mediaType=4,
        video=detail["images"][0]["video"],
        images=[],
    )
    return _ssr_wrapper(detail, aweme_id=SSR_DISTINCT_GROUP_AWEME_ID)


@pytest.mark.parametrize(
    "group_id", [SSR_DISTINCT_GROUP_ID, SSR_DISTINCT_GROUP_AWEME_ID, None]
)
def test_ssr_group_id_is_not_required_to_equal_explicit_item_id(group_id) -> None:
    wrapper = _ssr_distinct_group_video_wrapper()
    raw_detail = wrapper["aweme"]["detail"]
    if group_id is None:
        raw_detail.pop("groupId")
    else:
        raw_detail["groupId"] = group_id

    detail = douyin_signing._extract_ssr_aweme_detail(
        _ssr_flight_fragments(wrapper), SSR_DISTINCT_GROUP_AWEME_ID, SEC_UID
    )

    assert detail is not None
    assert detail["aweme_id"] == SSR_DISTINCT_GROUP_AWEME_ID
    assert "groupId" not in detail
    metadata = verified_aweme_metadata(
        detail, SSR_DISTINCT_GROUP_AWEME_ID, expected_profile_id=SEC_UID
    )
    assert metadata is not None
    assert metadata["media_id"] == SSR_DISTINCT_GROUP_AWEME_ID
    assert metadata["media_kind"] == "video"


@pytest.mark.parametrize("detail_id", [None, "7000000000000000000"])
def test_ssr_group_id_never_substitutes_for_missing_or_wrong_detail_id(
    detail_id,
) -> None:
    wrapper = _ssr_distinct_group_video_wrapper()
    detail = wrapper["aweme"]["detail"]
    detail["groupId"] = SSR_DISTINCT_GROUP_AWEME_ID
    if detail_id is None:
        detail.pop("awemeId")
    else:
        detail["awemeId"] = detail_id

    with pytest.raises(douyin_signing._IdentitySigningFailure, match="different aweme"):
        douyin_signing._extract_ssr_aweme_detail(
            _ssr_flight_fragments(wrapper), SSR_DISTINCT_GROUP_AWEME_ID, SEC_UID
        )


@pytest.mark.parametrize("wrapper_id", [None, "7000000000000000000"])
def test_ssr_group_id_never_substitutes_for_missing_or_wrong_wrapper_id(
    wrapper_id,
) -> None:
    wrapper = _ssr_distinct_group_video_wrapper()
    wrapper["aweme"]["detail"]["groupId"] = SSR_DISTINCT_GROUP_AWEME_ID
    if wrapper_id is None:
        wrapper.pop("awemeId")
    else:
        wrapper["awemeId"] = wrapper_id

    assert (
        douyin_signing._extract_ssr_aweme_detail(
            _ssr_flight_fragments(wrapper), SSR_DISTINCT_GROUP_AWEME_ID, SEC_UID
        )
        is None
    )
    with pytest.raises(douyin_signing._IdentitySigningFailure, match="different aweme"):
        douyin_signing._validated_ssr_wrapper_detail(
            wrapper, SSR_DISTINCT_GROUP_AWEME_ID, SEC_UID
        )


def test_ssr_distinct_group_keeps_author_identity_check() -> None:
    wrapper = _ssr_distinct_group_video_wrapper()
    wrapper["aweme"]["detail"]["authorInfo"]["secUid"] = "MS4wLjABAAAAwrongowner"

    with pytest.raises(
        douyin_signing._IdentitySigningFailure, match="different author"
    ):
        douyin_signing._extract_ssr_aweme_detail(
            _ssr_flight_fragments(wrapper), SSR_DISTINCT_GROUP_AWEME_ID, SEC_UID
        )


@pytest.mark.parametrize("change_media", [False, True])
def test_ssr_distinct_groups_do_not_hide_conflicting_media(change_media) -> None:
    first = _ssr_distinct_group_video_wrapper()
    second = _ssr_distinct_group_video_wrapper()
    second["aweme"]["detail"]["groupId"] = "7000000000000000001"
    if change_media:
        second["aweme"]["detail"]["video"]["duration"] += 1_000
    fragments = _ssr_flight_fragments(first) + _ssr_flight_fragments(second)

    if change_media:
        with pytest.raises(
            douyin_signing._IdentitySigningFailure, match="conflicting copies"
        ):
            douyin_signing._extract_ssr_aweme_detail(
                fragments, SSR_DISTINCT_GROUP_AWEME_ID, SEC_UID
            )
    else:
        detail = douyin_signing._extract_ssr_aweme_detail(
            fragments, SSR_DISTINCT_GROUP_AWEME_ID, SEC_UID
        )
        assert detail is not None
        assert detail["aweme_id"] == SSR_DISTINCT_GROUP_AWEME_ID


def test_signed_item_accepts_distinct_group_ssr_without_fallback(monkeypatch) -> None:
    wrapper = _ssr_distinct_group_video_wrapper()
    page, context, browser, manager = _install_fake_playwright(
        monkeypatch,
        _detail_response(aweme_id="7000000000000000000"),
        pace_snapshot=_pace_snapshot(wrapper),
    )

    def fail_if_glue_is_extracted(*args, **kwargs):
        pytest.fail("A verified exact SSR item must not fall back to signing")

    monkeypatch.setattr(
        douyin_signing, "_extract_glue_with_context_fallback", fail_if_glue_is_extracted
    )
    detail = fetch_signed_aweme_detail(
        SSR_DISTINCT_GROUP_AWEME_ID,
        verification_url=f"https://www.douyin.com/video/{SSR_DISTINCT_GROUP_AWEME_ID}",
        expected_sec_uid=SEC_UID,
        signer_settle_ms=0,
    )

    assert detail["aweme_id"] == SSR_DISTINCT_GROUP_AWEME_ID
    assert detail["author"]["sec_uid"] == SEC_UID
    assert page.goto_urls == [
        f"https://www.douyin.com/note/{SSR_DISTINCT_GROUP_AWEME_ID}"
    ]
    assert page.started is False
    assert page.signed_requests == []
    assert page.response_handlers == []
    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True


@pytest.mark.parametrize(
    ("detail_aweme_id", "detail_sec_uid", "failure_pattern"),
    [
        ("7000000000000000000", SEC_UID, "different aweme"),
        (AWEME_ID, "MS4wLjABAAAAwrongowner", "different author"),
    ],
)
def test_ssr_detail_fails_closed_on_item_or_author_mismatch(
    detail_aweme_id: str,
    detail_sec_uid: str,
    failure_pattern: str,
) -> None:
    detail = _ssr_live_photo_detail(
        aweme_id=detail_aweme_id,
        sec_uid=detail_sec_uid,
    )
    wrapper = _ssr_wrapper(detail)

    with pytest.raises(
        douyin_signing._IdentitySigningFailure,
        match=failure_pattern,
    ):
        douyin_signing._extract_ssr_aweme_detail(
            _ssr_flight_fragments(wrapper),
            AWEME_ID,
            SEC_UID,
        )


@pytest.mark.parametrize(
    ("mutation", "failure_type", "failure_pattern"),
    [
        ("redirect", douyin_signing._IdentitySigningFailure, "redirect"),
        ("unknown", douyin_signing._IdentitySigningFailure, "unknown aweme"),
        ("page-status", _TransientSigningFailure, "nonzero status"),
        ("item-status", _TransientSigningFailure, "nonzero status"),
    ],
)
def test_ssr_wrapper_rejects_redirect_unknown_and_nonzero_status(
    mutation: str,
    failure_type: type[Exception],
    failure_pattern: str,
) -> None:
    wrapper = _ssr_wrapper()
    if mutation == "redirect":
        wrapper["redirect"] = True
    elif mutation == "unknown":
        wrapper["aweme"]["isUnknownAweme"] = True
    elif mutation == "page-status":
        wrapper["statusCode"] = 1
    else:
        wrapper["aweme"]["statusCode"] = 1

    with pytest.raises(failure_type, match=failure_pattern):
        douyin_signing._extract_ssr_aweme_detail(
            _ssr_flight_fragments(wrapper),
            AWEME_ID,
            SEC_UID,
        )


@pytest.mark.parametrize(
    ("layer", "field"),
    [
        ("page", "statusMsg"),
        ("item", "statusMessage"),
    ],
)
def test_ssr_camelcase_status_fields_classify_explicit_rate_limit(
    layer: str,
    field: str,
) -> None:
    wrapper = _ssr_wrapper()
    payload = wrapper if layer == "page" else wrapper["aweme"]
    payload["statusCode"] = 4
    payload[field] = "请求频繁，请稍后再试"

    with pytest.raises(_TransientSigningFailure) as captured:
        douyin_signing._extract_ssr_aweme_detail(
            _ssr_flight_fragments(wrapper),
            AWEME_ID,
            SEC_UID,
        )

    assert captured.value.category == "api-rate-limit"


@pytest.mark.parametrize(
    ("layer", "field"),
    [
        ("page", "statusMessage"),
        ("item", "statusMsg"),
    ],
)
def test_ssr_camelcase_status_fields_classify_explicit_auth(
    layer: str,
    field: str,
) -> None:
    wrapper = _ssr_wrapper()
    payload = wrapper if layer == "page" else wrapper["aweme"]
    payload["statusCode"] = 1
    payload[field] = "请登录后继续"

    with pytest.raises(_AuthenticationSigningFailure) as captured:
        douyin_signing._extract_ssr_aweme_detail(
            _ssr_flight_fragments(wrapper),
            AWEME_ID,
            SEC_UID,
        )

    assert captured.value.issue_code == SiteIssueCode.LOGIN_REQUIRED


def test_ssr_flight_capture_limits_fail_closed(monkeypatch) -> None:
    monkeypatch.setattr(douyin_signing, "_MAX_PACE_FRAGMENT_BYTES", 8)

    with pytest.raises(_SigningFailure, match="unexpectedly large"):
        douyin_signing._parse_flight_json_records(['0:["too long"]\n'])

    page = SimpleNamespace(
        evaluate=lambda script, argument: {"state": "stream_too_large"}
    )
    with pytest.raises(_SigningFailure, match="bounded Flight capture limits"):
        douyin_signing._read_ssr_aweme_detail_from_page(page, AWEME_ID, SEC_UID)


@pytest.mark.parametrize(
    "pace_snapshot",
    [
        {"state": "too_many_entries"},
        {"state": "done", "fragments": ["not-a-flight-frame"]},
    ],
)
def test_optional_ssr_structure_miss_falls_back_to_exact_signed_api(
    monkeypatch,
    pace_snapshot: dict,
) -> None:
    page, context, browser, manager = _install_fake_playwright(
        monkeypatch,
        pace_snapshot=pace_snapshot,
    )

    detail = fetch_signed_aweme_detail(
        AWEME_ID,
        verification_url=VIDEO_URL,
        expected_sec_uid=SEC_UID,
        signer_settle_ms=0,
    )

    assert detail["aweme_id"] == AWEME_ID
    assert page.goto_urls == [NOTE_URL, douyin_signing._SIGNING_PAGE_URL]
    assert page.started is True
    assert len(page.signed_requests) == 1
    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True


def test_ssr_author_identity_mismatch_does_not_fall_back_to_signed_api(
    monkeypatch,
) -> None:
    wrapper = _ssr_wrapper(_ssr_live_photo_detail(sec_uid="MS4wLjABAAAAwrongowner"))
    page, context, browser, manager = _install_fake_playwright(
        monkeypatch,
        pace_snapshot=_pace_snapshot(wrapper),
    )

    with pytest.raises(DiscoveryError) as captured:
        fetch_signed_aweme_detail(
            AWEME_ID,
            verification_url=VIDEO_URL,
            expected_sec_uid=SEC_UID,
            signer_settle_ms=0,
        )

    assert isinstance(captured.value.__cause__, douyin_signing._IdentitySigningFailure)
    assert page.goto_urls == [NOTE_URL]
    assert page.started is False
    assert page.signed_requests == []
    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True


def test_ssr_explicit_auth_status_does_not_fall_back_to_signed_api(
    monkeypatch,
) -> None:
    wrapper = _ssr_wrapper()
    wrapper["statusCode"] = 1
    wrapper["statusMessage"] = "请登录后继续"
    page, context, browser, manager = _install_fake_playwright(
        monkeypatch,
        pace_snapshot=_pace_snapshot(wrapper),
    )

    with pytest.raises(AuthenticationRequiredError) as captured:
        fetch_signed_aweme_detail(
            AWEME_ID,
            verification_url=VIDEO_URL,
            expected_sec_uid=SEC_UID,
            signer_settle_ms=0,
        )

    assert captured.value.issue_code == SiteIssueCode.LOGIN_REQUIRED
    assert page.goto_urls == [NOTE_URL]
    assert page.started is False
    assert page.signed_requests == []
    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True


def test_ssr_flight_parser_enforces_all_collection_limits(monkeypatch) -> None:
    monkeypatch.setattr(douyin_signing, "_MAX_PACE_ENTRIES", 1)
    with pytest.raises(_SigningFailure, match="fragment list"):
        douyin_signing._parse_flight_json_records(["0:{}\n", "1:{}\n"])

    monkeypatch.setattr(douyin_signing, "_MAX_PACE_ENTRIES", 64)
    monkeypatch.setattr(douyin_signing, "_MAX_PACE_TOTAL_BYTES", 8)
    with pytest.raises(_SigningFailure, match="stream was unexpectedly large"):
        douyin_signing._parse_flight_json_records(["0:{}\n", "1:{}\n"])

    monkeypatch.setattr(douyin_signing, "_MAX_PACE_TOTAL_BYTES", 2_000_000)
    monkeypatch.setattr(douyin_signing, "_MAX_FLIGHT_FRAMES", 1)
    with pytest.raises(_SigningFailure, match="too many Flight frames"):
        douyin_signing._parse_flight_json_records(["0:{}\n1:{}\n"])

    monkeypatch.setattr(douyin_signing, "_MAX_FLIGHT_FRAMES", 256)
    monkeypatch.setattr(douyin_signing, "_MAX_FLIGHT_FRAME_BYTES", 3)
    with pytest.raises(_SigningFailure, match="text frame was unexpectedly large"):
        douyin_signing._parse_flight_json_records(["0:T4,test\n"])

    monkeypatch.setattr(douyin_signing, "_MAX_FLIGHT_FRAME_BYTES", 1_000_000)
    monkeypatch.setattr(douyin_signing, "_MAX_FLIGHT_JSON_RECORDS", 1)
    with pytest.raises(_SigningFailure, match="too many JSON Flight frames"):
        douyin_signing._parse_flight_json_records(["0:{}\n1:{}\n"])


def test_ssr_keeps_untrusted_media_url_for_existing_media_validator() -> None:
    evil_url = "https://evil.example/not-douyin-media.bin"
    wrapper = _ssr_wrapper(_ssr_live_photo_detail(image_url=evil_url))

    detail = douyin_signing._extract_ssr_aweme_detail(
        _ssr_flight_fragments(wrapper),
        AWEME_ID,
        SEC_UID,
    )

    assert detail is not None
    assert detail["images"][0]["url_list"] == [evil_url]
    assert (
        verified_aweme_metadata(
            detail,
            AWEME_ID,
            expected_profile_id=SEC_UID,
        )
        is None
    )


def test_direct_item_prefers_complete_detail_captured_from_its_page(
    monkeypatch,
) -> None:
    captured_payload = _detail_response()["payload"]
    page, context, browser, manager = _install_fake_playwright(
        monkeypatch,
        _detail_response(aweme_id="7000000000000000000"),
        page_responses=[FakePageResponse(captured_payload)],
    )

    def fail_if_glue_is_extracted(*args, **kwargs):
        raise AssertionError("SecSDK glue must not be extracted after page capture")

    monkeypatch.setattr(
        douyin_signing,
        "_extract_glue_with_context_fallback",
        fail_if_glue_is_extracted,
    )

    detail = fetch_signed_aweme_detail(
        AWEME_ID,
        verification_url=VIDEO_URL,
        expected_sec_uid=SEC_UID,
        signer_settle_ms=0,
    )

    assert detail["aweme_id"] == AWEME_ID
    assert page.goto_urls == [NOTE_URL]
    assert page.started is False
    assert page.signed_requests == []
    assert page.response_handlers == []
    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True


def test_page_detail_capture_reads_completed_request_body_not_response_json(
    monkeypatch,
) -> None:
    complete_payload = _detail_response()["payload"]
    misleading_payload = _detail_response(aweme_id="7000000000000000000")["payload"]
    page, context, browser, manager = _install_fake_playwright(
        monkeypatch,
        page_responses=[
            FakePageResponse(
                misleading_payload,
                body=json.dumps(complete_payload).encode("utf-8"),
            )
        ],
    )

    detail = fetch_signed_aweme_detail(
        AWEME_ID,
        verification_url=VIDEO_URL,
        expected_sec_uid=SEC_UID,
        signer_settle_ms=0,
    )

    assert detail["aweme_id"] == AWEME_ID
    assert page.goto_urls == [NOTE_URL]
    assert page.started is False
    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True


@pytest.mark.parametrize(
    ("body", "error_type", "issue_code"),
    [
        (
            b"<html><body>Please complete the verification</body></html>",
            AuthenticationRequiredError,
            SiteIssueCode.VERIFICATION_REQUIRED,
        ),
        (
            DNS_FILTER_HTML.encode("utf-8"),
            TemporaryAccessError,
            SiteIssueCode.NETWORK_ERROR,
        ),
    ],
)
def test_completed_page_detail_body_preserves_auth_and_dns_classification(
    monkeypatch,
    body: bytes,
    error_type: type[Exception],
    issue_code: SiteIssueCode,
) -> None:
    page, context, browser, manager = _install_fake_playwright(
        monkeypatch,
        page_responses=[FakePageResponse({}, body=body)],
    )

    with pytest.raises(error_type) as captured:
        fetch_signed_aweme_detail(
            AWEME_ID,
            verification_url=VIDEO_URL,
            expected_sec_uid=SEC_UID,
            signer_settle_ms=0,
        )

    assert captured.value.issue_code == issue_code
    assert page.goto_urls == [NOTE_URL]
    assert page.started is False
    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True


def test_page_detail_capture_rejects_response_redirect_outside_bound_endpoint(
    monkeypatch,
) -> None:
    exact_request_url = (
        "https://www.douyin.com/aweme/v1/web/aweme/detail/" f"?aweme_id={AWEME_ID}"
    )
    page, context, browser, manager = _install_fake_playwright(
        monkeypatch,
        page_responses=[
            FakePageResponse(
                _detail_response()["payload"],
                url="https://evil.example/redirected-detail",
                request_url=exact_request_url,
            )
        ],
    )

    with pytest.raises(DiscoveryError) as captured:
        fetch_signed_aweme_detail(
            AWEME_ID,
            verification_url=VIDEO_URL,
            expected_sec_uid=SEC_UID,
            signer_settle_ms=0,
        )

    assert isinstance(captured.value.__cause__, douyin_signing._IdentitySigningFailure)
    assert "bound endpoint" in str(captured.value.__cause__)
    assert page.goto_urls == [NOTE_URL]
    assert page.started is False
    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True


def test_page_detail_capture_accepts_trusted_douyin_subdomain(monkeypatch) -> None:
    detail_url = (
        "https://www-hj.douyin.com/aweme/v1/web/aweme/detail/" f"?aweme_id={AWEME_ID}"
    )
    page, context, browser, manager = _install_fake_playwright(
        monkeypatch,
        _detail_response(aweme_id="7000000000000000000"),
        page_responses=[
            FakePageResponse(_detail_response()["payload"], url=detail_url)
        ],
    )

    detail = fetch_signed_aweme_detail(
        AWEME_ID,
        verification_url=VIDEO_URL,
        expected_sec_uid=SEC_UID,
        signer_settle_ms=0,
    )

    assert detail["aweme_id"] == AWEME_ID
    assert page.goto_urls == [NOTE_URL]
    assert page.started is False
    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True


@pytest.mark.parametrize(
    ("candidate_url", "method"),
    [
        (
            "https://www.douyin.com/aweme/v1/web/aweme/detail/"
            "?aweme_id=7000000000000000000",
            "GET",
        ),
        (
            "https://evil.example/aweme/v1/web/aweme/detail/" f"?aweme_id={AWEME_ID}",
            "GET",
        ),
        (
            "https://www.douyin.com/aweme/v1/web/aweme/detail/" f"?aweme_id={AWEME_ID}",
            "POST",
        ),
        (
            "https://www.douyin.com:33443/aweme/v1/web/aweme/detail/"
            f"?aweme_id={AWEME_ID}",
            "GET",
        ),
        (
            "https://www.douyin.com/aweme/v1/web/aweme/detail/extra"
            f"?aweme_id={AWEME_ID}",
            "GET",
        ),
        (
            "https://www.douyin.com/aweme/v1/web/aweme/detail/"
            f"?aweme_id={AWEME_ID}&aweme_id={AWEME_ID}",
            "GET",
        ),
    ],
)
def test_page_detail_capture_rejects_unbound_request_candidates(
    monkeypatch,
    candidate_url: str,
    method: str,
) -> None:
    page, context, browser, manager = _install_fake_playwright(
        monkeypatch,
        page_responses=[
            FakePageResponse(
                _detail_response()["payload"],
                url=candidate_url,
                method=method,
            )
        ],
    )

    detail = fetch_signed_aweme_detail(
        AWEME_ID,
        verification_url=VIDEO_URL,
        expected_sec_uid=SEC_UID,
        signer_settle_ms=0,
    )

    assert detail["aweme_id"] == AWEME_ID
    assert page.goto_urls == [NOTE_URL, douyin_signing._SIGNING_PAGE_URL]
    assert page.started is True
    assert len(page.signed_requests) == 1
    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True


def test_page_detail_capture_allows_same_item_note_redirect(monkeypatch) -> None:
    note_url = f"https://www.douyin.com/note/{AWEME_ID}"
    page, context, browser, manager = _install_fake_playwright(
        monkeypatch,
        _detail_response(aweme_id="7000000000000000000"),
        page_responses=[FakePageResponse(_detail_response()["payload"])],
        page_final_url=note_url,
    )

    detail = fetch_signed_aweme_detail(
        AWEME_ID,
        verification_url=VIDEO_URL,
        expected_sec_uid=SEC_UID,
        signer_settle_ms=0,
    )

    assert detail["aweme_id"] == AWEME_ID
    assert page.goto_urls == [NOTE_URL]
    assert page.url == note_url
    assert page.started is False
    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True


@pytest.mark.parametrize(
    ("page_aweme_id", "page_sec_uid", "failure_pattern"),
    [
        ("7000000000000000000", SEC_UID, "different aweme"),
        (AWEME_ID, "wrong-owner", "different author"),
    ],
)
def test_bound_page_detail_fails_closed_on_explicit_identity_mismatch(
    monkeypatch, page_aweme_id: str, page_sec_uid: str, failure_pattern: str
) -> None:
    mismatched_payload = _detail_response(
        aweme_id=page_aweme_id,
        sec_uid=page_sec_uid,
    )["payload"]
    page, context, browser, manager = _install_fake_playwright(
        monkeypatch,
        page_responses=[FakePageResponse(mismatched_payload)],
    )

    with pytest.raises(DiscoveryError) as captured:
        fetch_signed_aweme_detail(
            AWEME_ID,
            verification_url=VIDEO_URL,
            expected_sec_uid=SEC_UID,
            signer_settle_ms=0,
        )

    assert isinstance(captured.value.__cause__, douyin_signing._IdentitySigningFailure)
    assert failure_pattern in str(captured.value.__cause__)
    assert page.goto_urls == [NOTE_URL]
    assert page.started is False
    assert page.signed_requests == []
    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True


def test_direct_note_url_enables_bound_page_detail_capture(monkeypatch) -> None:
    note_url = f"https://www.douyin.com/note/{AWEME_ID}"
    page, context, browser, manager = _install_fake_playwright(
        monkeypatch,
        page_responses=[FakePageResponse(_detail_response()["payload"])],
    )

    detail = fetch_signed_aweme_detail(
        AWEME_ID,
        verification_url=note_url,
        expected_sec_uid=SEC_UID,
        signer_settle_ms=0,
    )

    assert detail["aweme_id"] == AWEME_ID
    assert page.goto_urls == [NOTE_URL]
    assert page.started is False
    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True


def test_page_capture_miss_falls_back_to_synthetic_fetch(monkeypatch) -> None:
    page, context, browser, manager = _install_fake_playwright(monkeypatch)

    detail = fetch_signed_aweme_detail(
        AWEME_ID,
        verification_url=VIDEO_URL,
        expected_sec_uid=SEC_UID,
        signer_settle_ms=0,
    )

    assert detail["aweme_id"] == AWEME_ID
    assert page.goto_urls == [NOTE_URL, douyin_signing._SIGNING_PAGE_URL]
    assert page.started is True
    assert page.response_handlers == []
    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True


def test_images_base_page_candidate_falls_back_and_keeps_stable_reason(
    monkeypatch,
) -> None:
    filtered_payload = {
        "status_code": 0,
        "aweme_detail": None,
        "filter_detail": {"filter_reason": "images_base"},
    }
    page, context, browser, manager = _install_fake_playwright(
        monkeypatch,
        {
            "state": "done",
            "httpStatus": 200,
            "payload": filtered_payload,
        },
        page_responses=[FakePageResponse(filtered_payload)],
    )
    monkeypatch.setattr(douyin_signing, "_DETAIL_REQUEST_ATTEMPTS", 1)
    monkeypatch.setattr(douyin_signing, "_DETAIL_SIGNING_SESSION_ATTEMPTS", 1)

    with pytest.raises(TemporaryAccessError) as captured:
        fetch_signed_aweme_detail(
            AWEME_ID,
            verification_url=VIDEO_URL,
            expected_sec_uid=SEC_UID,
            signer_settle_ms=0,
        )

    assert captured.value.issue_code == SiteIssueCode.SITE_RESPONSE_CHANGED
    assert "Reason category: api-filtered-images-base" in str(captured.value)
    assert isinstance(captured.value.__cause__, _TransientSigningFailure)
    assert "different aweme" not in str(captured.value).lower()
    assert page.goto_urls == [NOTE_URL, douyin_signing._SIGNING_PAGE_URL]
    assert page.started is True
    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True


@pytest.mark.parametrize(
    ("page_final_url", "page_html", "expected_issue"),
    [
        (
            "https://www.douyin.com/login",
            "<html><body>Normal page</body></html>",
            SiteIssueCode.LOGIN_REQUIRED,
        ),
        (
            f"https://www.douyin.com/video/{AWEME_ID}",
            "<html><body>请完成验证</body></html>",
            SiteIssueCode.VERIFICATION_REQUIRED,
        ),
    ],
)
def test_item_page_capture_preserves_explicit_auth_classification(
    monkeypatch,
    page_final_url: str,
    page_html: str,
    expected_issue: SiteIssueCode,
) -> None:
    page, context, browser, manager = _install_fake_playwright(
        monkeypatch,
        page_final_url=page_final_url,
        page_html=page_html,
    )

    with pytest.raises(AuthenticationRequiredError) as captured:
        fetch_signed_aweme_detail(
            AWEME_ID,
            verification_url=VIDEO_URL,
            expected_sec_uid=SEC_UID,
            signer_settle_ms=0,
        )

    assert captured.value.issue_code == expected_issue
    assert captured.value.verification_url == VIDEO_URL
    assert page.started is False
    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True


def test_off_origin_page_body_cannot_manufacture_chrome_verification(
    monkeypatch,
) -> None:
    page, context, browser, manager = _install_fake_playwright(
        monkeypatch,
        page_final_url="https://evil.example/login",
        page_html="<html><body>请完成验证</body></html>",
    )

    detail = fetch_signed_aweme_detail(
        AWEME_ID,
        verification_url=VIDEO_URL,
        expected_sec_uid=SEC_UID,
        signer_settle_ms=0,
    )

    assert detail["aweme_id"] == AWEME_ID
    assert page.goto_urls == [NOTE_URL, douyin_signing._SIGNING_PAGE_URL]
    assert page.started is True
    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True


def test_item_page_detail_response_preserves_explicit_login_classification(
    monkeypatch,
) -> None:
    page, context, browser, manager = _install_fake_playwright(
        monkeypatch,
        page_responses=[
            FakePageResponse(
                {"status_code": 0, "aweme_detail": None},
                status=401,
            )
        ],
    )

    with pytest.raises(AuthenticationRequiredError) as captured:
        fetch_signed_aweme_detail(
            AWEME_ID,
            verification_url=VIDEO_URL,
            expected_sec_uid=SEC_UID,
            signer_settle_ms=0,
        )

    assert captured.value.issue_code == SiteIssueCode.LOGIN_REQUIRED
    assert captured.value.verification_url == VIDEO_URL
    assert page.goto_urls == [NOTE_URL]
    assert page.started is False
    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True


def test_profile_verification_url_does_not_enable_item_page_capture(
    monkeypatch,
) -> None:
    page, context, browser, manager = _install_fake_playwright(
        monkeypatch,
        page_responses=[FakePageResponse(_detail_response()["payload"])],
    )

    detail = fetch_signed_aweme_detail(
        AWEME_ID,
        verification_url=PROFILE_URL,
        expected_sec_uid=SEC_UID,
        signer_settle_ms=0,
    )

    assert detail["aweme_id"] == AWEME_ID
    assert page.goto_urls == [douyin_signing._SIGNING_PAGE_URL]
    assert page.started is True
    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True


def test_dnsfilter_replacement_page_maps_to_actionable_network_error(
    monkeypatch,
) -> None:
    page, context, browser, _manager = _install_fake_playwright(monkeypatch)
    source_fetches = 0

    def fetch_dnsfilter_page(*args, **kwargs):
        nonlocal source_fetches
        source_fetches += 1
        return DNS_FILTER_HTML

    monkeypatch.setattr(
        "app.douyin_signing._fetch_source_html_with_urllib",
        fetch_dnsfilter_page,
    )

    with pytest.raises(TemporaryAccessError) as captured:
        fetch_signed_aweme_detail(
            AWEME_ID,
            verification_url=VIDEO_URL,
            expected_sec_uid=SEC_UID,
            signer_settle_ms=0,
        )

    message = str(captured.value)
    assert captured.value.issue_code == SiteIssueCode.NETWORK_ERROR
    assert "Allow Douyin in the local filter" in message
    assert "disable DNS filtering" in message
    assert "switch networks" in message
    assert "retry the original link" in message
    assert "Chrome verification is not required" in message
    assert source_fetches == 1
    assert context.request.requested_url is None
    assert context.new_page_calls == 1
    assert page.started is False
    assert context.closed is True
    assert browser.closed is True


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


def test_pending_detail_retries_share_one_no_progress_budget(monkeypatch) -> None:
    pending_response = {"state": "pending"}
    page, context, browser, manager = _install_fake_playwright(
        monkeypatch,
        [pending_response, pending_response, pending_response],
    )
    clock = FakeClock()
    statuses: list[str] = []
    abort_calls = 0
    original_evaluate = page.evaluate

    def evaluate(script: str, argument=None):
        nonlocal abort_calls
        if "controller.abort" in script:
            abort_calls += 1
        return original_evaluate(script, argument)

    page.evaluate = evaluate
    page.wait_for_timeout = clock.advance_ms
    monkeypatch.setattr(
        douyin_signing,
        "time",
        SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep),
    )

    with pytest.raises(
        TemporaryAccessError,
        match="120 seconds without verified progress",
    ) as captured:
        fetch_signed_aweme_detail(
            AWEME_ID,
            verification_url=VIDEO_URL,
            expected_sec_uid=SEC_UID,
            signer_settle_ms=0,
            request_timeout_ms=45_000,
            status_callback=statuses.append,
        )

    assert isinstance(
        captured.value.__cause__, douyin_signing._SigningNoProgressTimeout
    )
    assert "Reason category: network-timeout" in str(captured.value)
    assert len(page.signed_requests) == 3
    assert [value["timeoutMs"] for value in page.signed_requests[:2]] == [
        45_000,
        45_000,
    ]
    assert 26_000 <= page.signed_requests[2]["timeoutMs"] <= 27_000
    assert context.new_page_calls == 1
    assert abort_calls >= 3
    assert not any("session 2/" in value for value in statuses)
    assert any("reason: network-timeout" in value for value in statuses)
    assert clock.now == pytest.approx(120, abs=0.01)
    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True


def test_fetch_prefers_urllib_with_the_original_cookie_jar(
    monkeypatch,
) -> None:
    page, context, browser, manager = _install_fake_playwright(monkeypatch)
    fallback_calls = []

    def fetch_with_urllib(url, cookie_jar, user_agent, timeout_ms, **kwargs):
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
    urllib_called = False

    def fail_urllib(url, cookie_jar, user_agent, timeout_ms, **kwargs):
        nonlocal urllib_called
        urllib_called = True
        assert kwargs["budget"] is not None
        assert kwargs["should_cancel"] is None
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
    assert urllib_called is True
    assert context.request.requested_url == VIDEO_URL
    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True


@pytest.mark.parametrize(
    ("transient_response", "reason"),
    [
        (_detail_response(http_status=429), "http-429"),
        (_detail_response(http_status=403), "http-403"),
        (_detail_response(http_status=503), "http-5xx"),
        (_detail_response(status_code=9), "api-status-nonzero"),
    ],
)
def test_detail_retry_status_reports_only_safe_reason_category(
    monkeypatch,
    transient_response: dict,
    reason: str,
) -> None:
    _install_fake_playwright(
        monkeypatch,
        [transient_response, _detail_response()],
    )
    statuses: list[str] = []

    detail = fetch_signed_aweme_detail(
        AWEME_ID,
        verification_url=VIDEO_URL,
        expected_sec_uid=SEC_UID,
        signer_settle_ms=0,
        status_callback=statuses.append,
    )

    assert detail["aweme_id"] == AWEME_ID
    retry_statuses = [value for value in statuses if "Retrying" in value]
    assert retry_statuses == [
        f"Retrying Douyin signed detail request 2/3 (reason: {reason})"
    ]
    assert all(
        "http" not in value.lower() or reason in value for value in retry_statuses
    )


def test_browser_html_429_is_transient_and_response_is_disposed(monkeypatch) -> None:
    response = FakeApiResponse(status=429)
    context = SimpleNamespace(
        request=SimpleNamespace(get=lambda url, timeout: response)
    )
    monkeypatch.setattr(
        "app.douyin_signing._fetch_source_html_with_urllib",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("temporary source request failure")
        ),
    )

    with pytest.raises(_TransientSigningFailure, match="temporarily limited"):
        _extract_glue_with_context_fallback(
            context,
            VIDEO_URL,
            object(),
            "Test User Agent",
            1_000,
            lambda: False,
        )

    assert response.disposed is True


def test_browser_html_bare_403_is_transient_and_response_is_disposed(
    monkeypatch,
) -> None:
    response = FakeApiResponse(status=403, body="temporarily unavailable")
    context = SimpleNamespace(
        request=SimpleNamespace(get=lambda url, timeout: response)
    )
    monkeypatch.setattr(
        "app.douyin_signing._fetch_source_html_with_urllib",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("temporary source request failure")
        ),
    )

    with pytest.raises(_TransientSigningFailure, match="temporarily rejected"):
        _extract_glue_with_context_fallback(
            context,
            VIDEO_URL,
            object(),
            "Test User Agent",
            1_000,
            lambda: False,
        )

    assert response.disposed is True


def test_browser_html_dnsfilter_replacement_page_is_not_authentication(
    monkeypatch,
) -> None:
    response = FakeApiResponse(body=DNS_FILTER_HTML)
    context = SimpleNamespace(
        request=SimpleNamespace(get=lambda url, timeout: response)
    )
    monkeypatch.setattr(
        "app.douyin_signing._fetch_source_html_with_urllib",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("temporary source request failure")
        ),
    )

    with pytest.raises(douyin_signing._NetworkFilterSigningFailure) as captured:
        _extract_glue_with_context_fallback(
            context,
            VIDEO_URL,
            object(),
            "Test User Agent",
            1_000,
            lambda: False,
        )

    assert not isinstance(captured.value, _AuthenticationSigningFailure)
    assert response.disposed is True


def test_browser_html_off_origin_401_is_not_treated_as_authentication(
    monkeypatch,
) -> None:
    response = FakeApiResponse(
        status=401,
        url="https://evil.example/login",
        body="login",
    )
    context = SimpleNamespace(
        request=SimpleNamespace(get=lambda url, timeout: response)
    )
    monkeypatch.setattr(
        "app.douyin_signing._fetch_source_html_with_urllib",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("temporary source request failure")
        ),
    )

    with pytest.raises(_SigningFailure, match="outside the trusted origin") as captured:
        _extract_glue_with_context_fallback(
            context,
            VIDEO_URL,
            object(),
            "Test User Agent",
            1_000,
            lambda: False,
        )

    assert not isinstance(captured.value, _AuthenticationSigningFailure)
    assert response.disposed is True


def test_browser_html_sso_verify_redirect_requires_authentication(
    monkeypatch,
) -> None:
    response = FakeApiResponse(
        status=200,
        url="https://sso.douyin.com/verify",
        body="verification",
    )
    context = SimpleNamespace(
        request=SimpleNamespace(get=lambda url, timeout: response)
    )
    monkeypatch.setattr(
        "app.douyin_signing._fetch_source_html_with_urllib",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("temporary source request failure")
        ),
    )

    with pytest.raises(_AuthenticationSigningFailure) as captured:
        _extract_glue_with_context_fallback(
            context,
            VIDEO_URL,
            object(),
            "Test User Agent",
            1_000,
            lambda: False,
        )

    assert captured.value.issue_code == SiteIssueCode.VERIFICATION_REQUIRED
    assert response.disposed is True


def test_browser_html_403_with_visible_captcha_requires_authentication(
    monkeypatch,
) -> None:
    response = FakeApiResponse(status=403, body="<body>请完成下列验证 验证码</body>")
    context = SimpleNamespace(
        request=SimpleNamespace(get=lambda url, timeout: response)
    )
    monkeypatch.setattr(
        "app.douyin_signing._fetch_source_html_with_urllib",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("temporary source request failure")
        ),
    )

    with pytest.raises(_AuthenticationSigningFailure) as captured:
        _extract_glue_with_context_fallback(
            context,
            VIDEO_URL,
            object(),
            "Test User Agent",
            1_000,
            lambda: False,
        )

    assert captured.value.issue_code == SiteIssueCode.VERIFICATION_REQUIRED
    assert response.disposed is True


def test_urllib_html_429_is_transient_and_response_is_closed(monkeypatch) -> None:
    payload = BytesIO(b"rate limited")
    error = UrllibHTTPError(
        VIDEO_URL,
        429,
        "Too Many Requests",
        {},
        payload,
    )

    class FailingOpener:
        def open(self, request, timeout):
            raise error

    monkeypatch.setattr(
        "app.douyin_signing.build_opener",
        lambda *args, **kwargs: FailingOpener(),
    )

    with pytest.raises(_TransientSigningFailure, match="temporarily limited"):
        _fetch_source_html_with_urllib(
            VIDEO_URL,
            object(),
            "Test User Agent",
            1_000,
        )

    assert payload.closed is True


def test_urllib_html_off_origin_401_is_not_treated_as_authentication(
    monkeypatch,
) -> None:
    payload = BytesIO(b"login")
    error = UrllibHTTPError(
        "https://evil.example/login",
        401,
        "Unauthorized",
        {},
        payload,
    )

    class FailingOpener:
        def open(self, request, timeout):
            raise error

    monkeypatch.setattr(
        "app.douyin_signing.build_opener",
        lambda *args, **kwargs: FailingOpener(),
    )

    with pytest.raises(_SigningFailure, match="outside the trusted origin") as captured:
        _fetch_source_html_with_urllib(
            VIDEO_URL,
            object(),
            "Test User Agent",
            1_000,
        )

    assert not isinstance(captured.value, _AuthenticationSigningFailure)
    assert payload.closed is True


def test_urllib_html_sso_verify_redirect_requires_authentication(
    monkeypatch,
) -> None:
    class RedirectedResponse:
        status = 200

        def __init__(self) -> None:
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            self.closed = True

        def geturl(self) -> str:
            return "https://sso.douyin.com/verify"

    response = RedirectedResponse()

    class RedirectingOpener:
        def open(self, request, timeout):
            return response

    monkeypatch.setattr(
        "app.douyin_signing.build_opener",
        lambda *args, **kwargs: RedirectingOpener(),
    )

    with pytest.raises(_AuthenticationSigningFailure) as captured:
        _fetch_source_html_with_urllib(
            VIDEO_URL,
            object(),
            "Test User Agent",
            1_000,
        )

    assert captured.value.issue_code == SiteIssueCode.VERIFICATION_REQUIRED
    assert response.closed is True


def test_fetch_errors_are_mapped_without_exposing_cookie_details(monkeypatch) -> None:
    def fail_cookie_read(profile):
        raise OSError("cookie token=do-not-expose")

    monkeypatch.setattr(
        "app.douyin_signing._load_chrome_cookie_jar",
        fail_cookie_read,
    )

    with pytest.raises(TemporaryAccessError, match="Fully quit Chrome") as captured:
        fetch_signed_aweme_detail(
            AWEME_ID,
            verification_url=VIDEO_URL,
            signer_settle_ms=0,
        )

    assert "verification page is not required" in str(captured.value)
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

    with pytest.raises(DiscoveryError, match="Chrome verification is not required"):
        fetch_signed_aweme_detail(AWEME_ID, verification_url=unsafe_url)

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

    with pytest.raises(DiscoveryError, match="Chrome verification is not required"):
        fetch_signed_aweme_detail(
            AWEME_ID,
            verification_url=verification_url,
            expected_sec_uid=expected_sec_uid,
        )

    assert cookie_accessed is False


def test_invalid_signed_response_does_not_request_auth_and_closes_resources(
    monkeypatch,
) -> None:
    page, context, browser, manager = _install_fake_playwright(
        monkeypatch,
        _detail_response(aweme_id="7000000000000000000"),
    )

    with pytest.raises(DiscoveryError, match="Chrome verification is not required"):
        fetch_signed_aweme_detail(
            AWEME_ID,
            verification_url=VIDEO_URL,
            expected_sec_uid=SEC_UID,
            signer_settle_ms=0,
        )

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


def test_distinct_verified_profile_pages_refresh_no_progress_budget(
    monkeypatch,
) -> None:
    clock = FakeClock()
    statuses: list[str] = []
    responses = [
        _profile_response(
            ["7000000000000000001"],
            has_more=1,
            max_cursor="123",
        ),
        _profile_response(
            ["7000000000000000002"],
            has_more=0,
            max_cursor="456",
        ),
    ]

    class TimedPage:
        def __init__(self) -> None:
            self.signed_requests: list[dict] = []
            self.started_at = 0.0

        def evaluate(self, script: str, argument=None):
            if argument is not None:
                self.signed_requests.append(argument)
                self.started_at = clock.monotonic()
                return None
            if "controller.abort" in script:
                return None
            if clock.monotonic() - self.started_at < 119:
                return {"state": "pending"}
            return responses[len(self.signed_requests) - 1]

        def wait_for_timeout(self, timeout: int) -> None:
            clock.advance_ms(timeout)

    page = TimedPage()

    def run(*args, **kwargs):
        return kwargs["operation"](page)

    monkeypatch.setattr(
        douyin_signing,
        "time",
        SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep),
    )
    monkeypatch.setattr(douyin_signing, "_run_with_signing_page", run)

    awemes = fetch_signed_profile_awemes(
        PROFILE_URL,
        SEC_UID,
        max_pages=2,
        signer_settle_ms=0,
        request_timeout_ms=119_500,
        status_callback=statuses.append,
    )

    assert [aweme["aweme_id"] for aweme in awemes] == [
        "7000000000000000001",
        "7000000000000000002",
    ]
    assert clock.now == pytest.approx(238, abs=0.25)
    assert clock.now > 120
    assert [value for value in statuses if value.startswith("Verified Douyin")] == [
        "Verified Douyin signed profile page 1 (1 items)",
        "Verified Douyin signed profile page 2 (1 items)",
    ]
    assert [request["params"]["max_cursor"] for request in page.signed_requests] == [
        "0",
        "123",
    ]


def test_duplicate_only_profile_page_does_not_refresh_no_progress_budget(
    monkeypatch,
) -> None:
    clock = FakeClock()
    first_page = _profile_response(
        ["7000000000000000001"],
        has_more=1,
        max_cursor="123",
    )
    duplicate_page = _profile_response(
        ["7000000000000000001"],
        has_more=1,
        max_cursor="456",
    )

    class TimedPage:
        def __init__(self) -> None:
            self.signed_requests: list[dict] = []
            self.started_at = 0.0

        def evaluate(self, script: str, argument=None):
            if argument is not None:
                self.signed_requests.append(argument)
                self.started_at = clock.monotonic()
                return None
            if "controller.abort" in script:
                return None
            page_index = len(self.signed_requests) - 1
            if page_index == 0:
                return first_page
            if page_index == 1:
                if clock.monotonic() - self.started_at < 60:
                    return {"state": "pending"}
                return duplicate_page
            return {"state": "pending"}

        def wait_for_timeout(self, timeout: int) -> None:
            clock.advance_ms(timeout)

    page = TimedPage()
    monkeypatch.setattr(
        douyin_signing,
        "time",
        SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep),
    )
    monkeypatch.setattr(
        douyin_signing,
        "_run_with_signing_page",
        lambda *args, **kwargs: kwargs["operation"](page),
    )

    with pytest.raises(
        TemporaryAccessError,
        match="120 seconds without verified progress",
    ):
        fetch_signed_profile_awemes(
            PROFILE_URL,
            SEC_UID,
            max_pages=3,
            signer_settle_ms=0,
            request_timeout_ms=119_500,
        )

    assert clock.now == pytest.approx(120, abs=0.01)
    assert [request["params"]["max_cursor"] for request in page.signed_requests] == [
        "0",
        "123",
        "456",
    ]


def test_fresh_signing_session_resumes_from_failed_profile_cursor(
    monkeypatch,
) -> None:
    clock = FakeClock()
    run_calls = 0
    refresh_times: list[float] = []
    requested_by_session: dict[int, list[str]] = {1: [], 2: []}
    statuses: list[str] = []
    original_refresh = douyin_signing._NoProgressBudget.refresh

    def record_refresh(self) -> None:
        refresh_times.append(clock.monotonic())
        original_refresh(self)

    first_page = _profile_response(
        ["7000000000000000001"],
        has_more=1,
        max_cursor="123",
    )
    second_page = _profile_response(
        ["7000000000000000002"],
        has_more=0,
        max_cursor="456",
    )

    class ReplayPage:
        def __init__(self, session_number: int) -> None:
            self.session_number = session_number
            self.current_cursor = ""
            self.started_at = 0.0

        def evaluate(self, script: str, argument=None):
            if argument is not None:
                self.current_cursor = argument["params"]["max_cursor"]
                requested_by_session[self.session_number].append(self.current_cursor)
                self.started_at = clock.monotonic()
                return None
            if "controller.abort" in script:
                return None
            if self.session_number == 1:
                if self.current_cursor == "0":
                    return first_page
                return {"state": "failed", "reason": "request_failed"}
            if self.current_cursor != "123":
                raise AssertionError(
                    "A fresh session replayed an already verified page"
                )
            return second_page

        def wait_for_timeout(self, timeout: int) -> None:
            clock.advance_ms(timeout)

    def run(*args, **kwargs):
        nonlocal run_calls
        run_calls += 1
        return kwargs["operation"](ReplayPage(run_calls))

    monkeypatch.setattr(
        douyin_signing,
        "time",
        SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep),
    )
    monkeypatch.setattr(
        douyin_signing._NoProgressBudget,
        "refresh",
        record_refresh,
    )
    monkeypatch.setattr(douyin_signing, "_run_with_signing_page", run)

    awemes = fetch_signed_profile_awemes(
        PROFILE_URL,
        SEC_UID,
        max_pages=2,
        signer_settle_ms=0,
        request_timeout_ms=119_500,
        status_callback=statuses.append,
    )

    assert [aweme["aweme_id"] for aweme in awemes] == [
        "7000000000000000001",
        "7000000000000000002",
    ]
    assert run_calls == 2
    assert requested_by_session == {
        1: ["0", "123", "123", "123"],
        2: ["123"],
    }
    assert refresh_times == [pytest.approx(0), pytest.approx(8)]
    assert clock.now == pytest.approx(8)
    assert "Resuming Douyin signed profile at page 2/2 (1 verified items)" in statuses


def test_profile_session_rebuild_does_not_refresh_resume_budget(monkeypatch) -> None:
    clock = FakeClock()
    run_calls = 0
    refresh_times: list[float] = []
    second_session_cursors: list[str] = []
    original_refresh = douyin_signing._NoProgressBudget.refresh
    first_page = _profile_response(
        ["7000000000000000001"],
        has_more=1,
        max_cursor="123",
    )

    def record_refresh(self) -> None:
        refresh_times.append(clock.monotonic())
        original_refresh(self)

    class ResumeTimeoutPage:
        def __init__(self, session_number: int) -> None:
            self.session_number = session_number
            self.current_cursor = ""

        def evaluate(self, script: str, argument=None):
            if argument is not None:
                self.current_cursor = argument["params"]["max_cursor"]
                if self.session_number == 2:
                    second_session_cursors.append(self.current_cursor)
                return None
            if "controller.abort" in script:
                return None
            if self.session_number == 1 and self.current_cursor == "0":
                return first_page
            if self.session_number == 1:
                return {"state": "failed", "reason": "request_failed"}
            return {"state": "pending"}

        def wait_for_timeout(self, timeout: int) -> None:
            clock.advance_ms(timeout)

    def run(*args, **kwargs):
        nonlocal run_calls
        run_calls += 1
        return kwargs["operation"](ResumeTimeoutPage(run_calls))

    monkeypatch.setattr(
        douyin_signing,
        "time",
        SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep),
    )
    monkeypatch.setattr(
        douyin_signing._NoProgressBudget,
        "refresh",
        record_refresh,
    )
    monkeypatch.setattr(douyin_signing, "_run_with_signing_page", run)

    with pytest.raises(
        TemporaryAccessError,
        match="120 seconds without verified progress",
    ) as captured:
        fetch_signed_profile_awemes(
            PROFILE_URL,
            SEC_UID,
            max_pages=2,
            signer_settle_ms=0,
            request_timeout_ms=119_500,
        )

    assert isinstance(
        captured.value.__cause__, douyin_signing._SigningNoProgressTimeout
    )
    assert "Reason category: network-error" in str(captured.value)
    assert run_calls == 2
    assert second_session_cursors and set(second_session_cursors) == {"123"}
    assert refresh_times == [pytest.approx(0)]
    assert clock.now == pytest.approx(120, abs=0.01)


def test_profile_target_stops_before_a_later_transient_page(monkeypatch) -> None:
    target_aweme_id = "7000000000000000002"
    empty_response = {
        "state": "done",
        "httpStatus": 200,
        "payload": {"status_code": 0},
    }
    page, context, browser, manager = _install_fake_playwright(
        monkeypatch,
        [
            _profile_response(
                ["7000000000000000001", target_aweme_id],
                has_more=1,
                max_cursor="123",
            ),
            empty_response,
        ],
    )

    awemes = fetch_signed_profile_awemes(
        PROFILE_URL,
        SEC_UID,
        target_aweme_id=target_aweme_id,
        signer_settle_ms=0,
    )

    assert [aweme["aweme_id"] for aweme in awemes] == [target_aweme_id]
    assert len(page.signed_requests) == 1
    assert page.signed_requests[0]["params"]["max_cursor"] == "0"
    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True


def test_profile_fetch_does_not_return_partial_result_after_unbound_empty_page(
    monkeypatch,
) -> None:
    first_page = _profile_response(
        ["7000000000000000001"],
        has_more=1,
        max_cursor="123",
    )
    unbound_empty_page = _profile_response([], has_more=0)
    unbound_empty_page["payload"].pop("sec_uid")
    requested_cursors: list[str] = []
    statuses: list[str] = []

    class RecordingBudget:
        def __init__(self) -> None:
            self.refresh_calls = 0

        def remaining_seconds(self) -> float:
            return 120.0

        def clamp_timeout_ms(self, requested_ms: int) -> int:
            return requested_ms

        def note_failure(self, exc: BaseException) -> None:
            del exc

        def refresh(self) -> None:
            self.refresh_calls += 1

    class Page:
        def __init__(self) -> None:
            self.cursor = ""

        def evaluate(self, script: str, argument=None):
            if argument is not None:
                self.cursor = argument["params"]["max_cursor"]
                requested_cursors.append(self.cursor)
                return None
            if "controller.abort" in script:
                return None
            return first_page if self.cursor == "0" else unbound_empty_page

    budget = RecordingBudget()
    run_calls = 0

    def run(*args, **kwargs):
        nonlocal run_calls
        del args
        run_calls += 1
        return kwargs["operation"](Page())

    monkeypatch.setattr(douyin_signing, "_run_with_signing_page", run)
    monkeypatch.setattr(
        douyin_signing,
        "_wait_with_cancel",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        douyin_signing,
        "_wait_without_page_with_cancel",
        lambda *args, **kwargs: None,
    )

    with pytest.raises(
        TemporaryAccessError,
        match="Reason category: api-unbound-empty-page",
    ):
        fetch_signed_profile_awemes(
            PROFILE_URL,
            SEC_UID,
            signer_settle_ms=0,
            status_callback=statuses.append,
            progress_budget=budget,
        )

    assert run_calls == 3
    assert requested_cursors[0] == "0"
    assert requested_cursors[1:] == ["123"] * 9
    assert budget.refresh_calls == 1
    assert any("reason: api-unbound-empty-page" in value for value in statuses)


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
    statuses: list[str] = []

    awemes = fetch_signed_profile_awemes(
        PROFILE_URL,
        SEC_UID,
        signer_settle_ms=0,
        status_callback=statuses.append,
    )

    assert [aweme["aweme_id"] for aweme in awemes] == ["7000000000000000001"]
    assert [request["params"]["max_cursor"] for request in page.signed_requests] == [
        "0",
        "0",
    ]
    assert [request["params"]["count"] for request in page.signed_requests] == [
        "50",
        "18",
    ]
    assert any("reason: api-missing-aweme-list" in value for value in statuses)
    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True


def test_profile_fetch_retries_with_a_fresh_signing_session(monkeypatch) -> None:
    calls = []
    delays = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            raise _TransientSigningFailure("temporary empty profile response")
        return [_profile_aweme(AWEME_ID)]

    monkeypatch.setattr("app.douyin_signing._run_with_signing_page", run)
    monkeypatch.setattr(
        "app.douyin_signing._wait_without_page_with_cancel",
        lambda duration_ms, should_cancel, **kwargs: delays.append(duration_ms),
    )

    awemes = fetch_signed_profile_awemes(
        PROFILE_URL,
        SEC_UID,
        target_aweme_id=AWEME_ID,
        signer_settle_ms=0,
    )

    assert [aweme["aweme_id"] for aweme in awemes] == [AWEME_ID]
    assert len(calls) == 2
    assert delays == [5_000]


def test_profile_transient_exhaustion_is_not_mislabeled_as_captcha(
    monkeypatch,
) -> None:
    delays = []

    monkeypatch.setattr(
        "app.douyin_signing._run_with_signing_page",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            _TransientSigningFailure(
                "temporary profile limit",
                category="http-429",
            )
        ),
    )
    monkeypatch.setattr(
        "app.douyin_signing._wait_without_page_with_cancel",
        lambda duration_ms, should_cancel, **kwargs: delays.append(duration_ms),
    )

    with pytest.raises(TemporaryAccessError, match="rate-limited") as captured:
        fetch_signed_profile_awemes(
            PROFILE_URL,
            SEC_UID,
            target_aweme_id=AWEME_ID,
            signer_settle_ms=0,
        )

    assert delays == [5_000, 10_000]
    assert captured.value.issue_code == SiteIssueCode.RATE_LIMITED
    assert "Reason category: http-429" in str(captured.value)


def test_profile_page_limit_does_not_request_auth_and_closes_resources(
    monkeypatch,
) -> None:
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

    with pytest.raises(DiscoveryError, match="Chrome verification is not required"):
        fetch_signed_profile_awemes(
            PROFILE_URL,
            SEC_UID,
            max_pages=1,
            signer_settle_ms=0,
        )

    assert page.closed and context.closed and browser.closed
    assert manager.resources_closed_on_exit is True
