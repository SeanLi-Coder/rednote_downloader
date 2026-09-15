from __future__ import annotations

import pytest

import app.douyin_signing as signing
from app.errors import (
    AuthenticationRequiredError,
    DiscoveryError,
    SiteIssueCode,
    TemporaryAccessError,
)


VIDEO_URL = "https://www.douyin.com/video/7649744769275263409"
SECRET_MARKERS = (
    "attacker.invalid",
    "private-path",
    "signature-secret",
    "sessionid=cookie-secret",
)
UNTRUSTED_DETAIL = (
    "https://attacker.invalid/private-path?signature=signature-secret "
    "Cookie: sessionid=cookie-secret"
)


IDENTITY_CASES = [
    ("Douyin SSR item requested a redirect", "ssr-redirect"),
    ("Douyin SSR returned an unknown aweme", "ssr-unknown-item"),
    ("Douyin SSR returned a different aweme", "ssr-item-mismatch"),
    ("Douyin SSR returned a different author", "ssr-author-mismatch"),
    (
        "Douyin SSR returned conflicting copies of the requested aweme",
        "ssr-conflicting-items",
    ),
    ("Douyin detail API returned a different aweme", "detail-item-mismatch"),
    ("Douyin detail API returned a different author", "detail-author-mismatch"),
    (
        "Douyin detail response redirected outside its bound endpoint",
        "detail-response-redirect",
    ),
]


@pytest.mark.parametrize(
    ("message", "expected_code"),
    [
        (message, "detail-metadata-incomplete")
        for message in (
            "Douyin detail API returned no aweme detail",
            "Douyin detail API returned no author",
            "Douyin detail API returned no author identity",
            "Douyin detail response returned no complete body",
        )
    ]
    + [
        (message, "ssr-metadata-incomplete")
        for message in (
            "Douyin SSR returned no page data",
            "Douyin SSR returned no item data",
            "Douyin SSR returned no aweme detail",
            "Douyin SSR item returned no author",
            "Douyin SSR returned no valid author identity",
        )
    ]
    + [
        (message, "signer-html-invalid")
        for message in (
            "Douyin HTML redirected outside the trusted origin",
            "Douyin HTML returned an invalid HTTP status",
            "Douyin HTML returned an invalid browser response",
            "Douyin HTML returned an invalid content length",
            "Douyin HTML response could not be decoded",
            "Douyin HTML response was unexpectedly large",
            "Douyin SecSDK HTML could not be parsed",
        )
    ]
    + [
        (message, "signer-script-invalid")
        for message in (
            "Douyin returned an untrusted SecSDK script URL",
            "Douyin SecSDK marker is missing",
            "Douyin returned nested SecSDK script tags",
            "Douyin returned an incomplete SecSDK script tag",
            "Douyin returned too many SecSDK glue tags",
            "Douyin SecSDK glue was unexpectedly large",
        )
    ],
)
def test_fixed_metadata_and_signer_failures_have_specific_codes(
    message: str, expected_code: str
) -> None:
    cause = signing._SigningFailure(message)
    cause.__cause__ = RuntimeError(UNTRUSTED_DETAIL)

    assert signing._signing_diagnostic_code(cause) == expected_code
    with pytest.raises(DiscoveryError) as captured:
        signing._raise_signing_error(VIDEO_URL, cause)

    error = captured.value
    assert type(error) is DiscoveryError
    assert error.issue_code == SiteIssueCode.SITE_RESPONSE_CHANGED
    assert error.__cause__ is cause
    assert f"Diagnostic code: {expected_code}." in str(error)
    assert error.diagnostic_code == expected_code
    assert signing.public_signing_diagnostic_code(error) == expected_code
    assert message not in str(error)
    for secret in SECRET_MARKERS:
        assert secret not in str(error)


@pytest.mark.parametrize(("message", "expected_code"), IDENTITY_CASES)
def test_identity_failure_has_a_stable_public_diagnostic(
    message: str, expected_code: str
) -> None:
    cause = signing._IdentitySigningFailure(message)

    assert signing._signing_diagnostic_code(cause) == expected_code
    with pytest.raises(DiscoveryError) as captured:
        signing._raise_signing_error(VIDEO_URL, cause)

    error = captured.value
    assert type(error) is DiscoveryError
    assert error.issue_code == SiteIssueCode.SITE_RESPONSE_CHANGED
    assert error.__cause__ is cause
    assert f"Diagnostic code: {expected_code}." in str(error)
    assert error.diagnostic_code == expected_code
    assert signing.public_signing_diagnostic_code(error) == expected_code
    assert message not in str(error)


@pytest.mark.parametrize(("message", "expected_code"), IDENTITY_CASES)
def test_diagnostics_require_exact_internal_messages(
    message: str, expected_code: str
) -> None:
    cause = signing._IdentitySigningFailure(f"{message}: {UNTRUSTED_DETAIL}")

    assert signing._signing_diagnostic_code(cause) == "signing-validation-failed"
    with pytest.raises(DiscoveryError) as captured:
        signing._raise_signing_error(VIDEO_URL, cause)

    error = captured.value
    assert error.__cause__ is cause
    assert error.issue_code == SiteIssueCode.SITE_RESPONSE_CHANGED
    assert "Diagnostic code: signing-validation-failed." in str(error)
    assert error.diagnostic_code == "signing-validation-failed"
    assert signing.public_signing_diagnostic_code(error) == "signing-validation-failed"
    assert f"Diagnostic code: {expected_code}." not in str(error)
    for secret in SECRET_MARKERS:
        assert secret not in str(error)


@pytest.mark.parametrize(
    ("exception_type", "expected_code"),
    [
        (signing._SigningFailure, "signing-validation-failed"),
        (signing._IdentitySigningFailure, "signing-validation-failed"),
        (RuntimeError, "signing-runtime-error"),
        (ValueError, "signing-runtime-error"),
    ],
)
def test_unknown_failures_do_not_expose_untrusted_exception_text(
    exception_type: type[Exception], expected_code: str
) -> None:
    cause = exception_type(UNTRUSTED_DETAIL)

    assert signing._signing_diagnostic_code(cause) == expected_code
    with pytest.raises(DiscoveryError) as captured:
        signing._raise_signing_error(VIDEO_URL, cause)

    error = captured.value
    assert type(error) is DiscoveryError
    assert error.issue_code == SiteIssueCode.SITE_RESPONSE_CHANGED
    assert error.__cause__ is cause
    assert f"Diagnostic code: {expected_code}." in str(error)
    assert error.diagnostic_code == expected_code
    assert signing.public_signing_diagnostic_code(error) == expected_code
    for secret in SECRET_MARKERS:
        assert secret not in str(error)


@pytest.mark.parametrize(("message", "unused_expected_code"), IDENTITY_CASES)
def test_unknown_exception_cannot_impersonate_a_known_internal_failure(
    message: str, unused_expected_code: str
) -> None:
    cause = RuntimeError(message)

    assert signing._signing_diagnostic_code(cause) == "signing-runtime-error"
    with pytest.raises(DiscoveryError) as captured:
        signing._raise_signing_error(VIDEO_URL, cause)

    assert captured.value.__cause__ is cause
    assert captured.value.issue_code == SiteIssueCode.SITE_RESPONSE_CHANGED
    assert "Diagnostic code: signing-runtime-error." in str(captured.value)
    assert captured.value.diagnostic_code == "signing-runtime-error"
    assert (
        signing.public_signing_diagnostic_code(captured.value)
        == "signing-runtime-error"
    )


@pytest.mark.parametrize(
    ("detail", "expected_code"),
    [
        (
            {"aweme_id": "7649744769275263408", "author": {"sec_uid": "expected"}},
            "detail-item-mismatch",
        ),
        (
            {
                "aweme_id": "7649744769275263409",
                "author": {"sec_uid": "another-author"},
            },
            "detail-author-mismatch",
        ),
    ],
)
def test_real_detail_identity_validation_preserves_diagnostic(
    detail: dict, expected_code: str
) -> None:
    response = {
        "httpStatus": 200,
        "payload": {"status_code": 0, "aweme_detail": detail},
    }

    with pytest.raises(signing._IdentitySigningFailure) as internal:
        signing._validate_detail_response(response, "7649744769275263409", "expected")
    with pytest.raises(DiscoveryError) as public:
        signing._raise_signing_error(VIDEO_URL, internal.value)

    assert public.value.__cause__ is internal.value
    assert public.value.issue_code == SiteIssueCode.SITE_RESPONSE_CHANGED
    assert f"Diagnostic code: {expected_code}." in str(public.value)
    assert public.value.diagnostic_code == expected_code
    assert signing.public_signing_diagnostic_code(public.value) == expected_code


@pytest.mark.parametrize(
    ("category", "expected_issue"),
    [
        ("http-429", SiteIssueCode.RATE_LIMITED),
        ("api-rate-limit", SiteIssueCode.RATE_LIMITED),
        ("http-403", SiteIssueCode.REQUEST_REJECTED),
        ("http-5xx", SiteIssueCode.SITE_UNAVAILABLE),
        ("network-timeout", SiteIssueCode.NETWORK_ERROR),
        ("network-error", SiteIssueCode.NETWORK_ERROR),
        ("signer-html", SiteIssueCode.SITE_RESPONSE_CHANGED),
    ],
)
def test_transient_failures_keep_their_existing_classification(
    category: str, expected_issue: SiteIssueCode
) -> None:
    cause = signing._TransientSigningFailure(UNTRUSTED_DETAIL, category=category)

    with pytest.raises(TemporaryAccessError) as captured:
        signing._raise_signing_error(VIDEO_URL, cause)

    error = captured.value
    assert error.issue_code == expected_issue
    assert error.__cause__ is cause
    assert f"Reason category: {category}." in str(error)
    assert "Diagnostic code:" not in str(error)
    for secret in SECRET_MARKERS:
        assert secret not in str(error)


@pytest.mark.parametrize(
    "issue_code", [SiteIssueCode.LOGIN_REQUIRED, SiteIssueCode.VERIFICATION_REQUIRED]
)
def test_authentication_failures_keep_verification_url_and_classification(
    issue_code: SiteIssueCode,
) -> None:
    cause = signing._AuthenticationSigningFailure(
        UNTRUSTED_DETAIL, issue_code=issue_code
    )

    with pytest.raises(AuthenticationRequiredError) as captured:
        signing._raise_signing_error(VIDEO_URL, cause)

    error = captured.value
    assert error.issue_code == issue_code
    assert error.__cause__ is cause
    assert error.verification_url == VIDEO_URL
    assert "Diagnostic code:" not in str(error)
    for secret in SECRET_MARKERS:
        assert secret not in str(error)


@pytest.mark.parametrize(
    ("cause", "expected_type", "expected_issue"),
    [
        (
            signing._NetworkFilterSigningFailure(UNTRUSTED_DETAIL),
            TemporaryAccessError,
            SiteIssueCode.NETWORK_ERROR,
        ),
        (
            signing._CookieAccessSigningFailure(UNTRUSTED_DETAIL),
            TemporaryAccessError,
            SiteIssueCode.COOKIE_UNAVAILABLE,
        ),
        (
            RuntimeError(f"network request timed out: {UNTRUSTED_DETAIL}"),
            TemporaryAccessError,
            SiteIssueCode.NETWORK_ERROR,
        ),
        (
            ImportError(f"Missing browser module: {UNTRUSTED_DETAIL}"),
            DiscoveryError,
            SiteIssueCode.LOCAL_CONFIGURATION,
        ),
    ],
)
def test_operational_errors_do_not_become_generic_integrity_failures(
    cause: Exception,
    expected_type: type[Exception],
    expected_issue: SiteIssueCode,
) -> None:
    with pytest.raises(expected_type) as captured:
        signing._raise_signing_error(VIDEO_URL, cause)

    error = captured.value
    assert type(error) is expected_type
    assert error.issue_code == expected_issue
    assert error.__cause__ is cause
    assert "Diagnostic code:" not in str(error)
    for secret in SECRET_MARKERS:
        assert secret not in str(error)
