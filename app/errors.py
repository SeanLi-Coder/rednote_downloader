from __future__ import annotations

import re
from enum import Enum


class SiteIssueCode(str, Enum):
    """Stable, user-visible categories for remote-site and local runtime failures."""

    RATE_LIMITED = "rate_limited"
    VERIFICATION_REQUIRED = "verification_required"
    LOGIN_REQUIRED = "login_required"
    REQUEST_REJECTED = "request_rejected"
    SITE_PROCESSING = "site_processing"
    CONTENT_UNAVAILABLE = "content_unavailable"
    REGION_RESTRICTED = "region_restricted"
    SITE_RESPONSE_CHANGED = "site_response_changed"
    MEDIA_LINK_EXPIRED = "media_link_expired"
    SITE_UNAVAILABLE = "site_unavailable"
    NETWORK_ERROR = "network_error"
    COOKIE_UNAVAILABLE = "cookie_unavailable"
    SECURITY_BLOCKED = "security_blocked"
    LOCAL_CONFIGURATION = "local_configuration"
    UNKNOWN = "unknown"


_REASON_CATEGORY_RE = re.compile(r"Reason category:\s*([a-z0-9-]+)", re.I)


def is_explicit_rate_limit_message(value: str) -> bool:
    """Recognize rate-limit language without treating generic retries as limits."""

    lowered = str(value or "").lower()
    return bool(
        re.search(r"\b(?:too many requests|rate[- ]limit(?:ed)?)\b", lowered)
        or re.search(r"(?:访问|请求|操作)(?:过于|太)?频繁", lowered)
        or re.search(r"频繁操作", lowered)
        or re.search(r"\b(?:http(?: error)?|status code)\s*429\b", lowered)
    )


def classify_site_issue(
    value: BaseException | str | None,
    *,
    authentication_required: bool = False,
) -> SiteIssueCode:
    """Classify a sanitized runtime message without treating auth disclaimers as auth."""

    explicit_code = getattr(value, "issue_code", None)
    if explicit_code is not None:
        try:
            return SiteIssueCode(explicit_code)
        except ValueError:
            pass
    text = str(value or "")
    lowered = text.lower()
    reason_match = _REASON_CATEGORY_RE.search(text)
    reason = reason_match.group(1).lower() if reason_match else ""

    explicitly_not_auth = any(
        marker in lowered
        for marker in (
            "captcha verification is not required",
            "captcha is not required",
            "a captcha is not required",
            "chrome verification is not required",
            "verification page is not required",
            "does not require chrome verification",
            "without requesting chrome verification",
        )
    )
    if not explicitly_not_auth and any(
        marker in lowered
        for marker in (
            "captcha required",
            "captcha challenge",
            "complete the captcha",
            "complete captcha",
            "solve the captcha",
            "verify you are human",
            "请完成验证码",
            "请完成验证",
            "请拖动滑块",
        )
    ):
        return SiteIssueCode.VERIFICATION_REQUIRED

    if authentication_required:
        if not explicitly_not_auth and any(
            marker in lowered
            for marker in (
                "captcha",
                "verification challenge",
                "explicit verification",
                "verify you are human",
                "security verification",
                "验证码",
                "安全验证",
            )
        ):
            return SiteIssueCode.VERIFICATION_REQUIRED
        return SiteIssueCode.LOGIN_REQUIRED

    if reason == "http-429":
        return SiteIssueCode.RATE_LIMITED
    if reason in {"http-403", "signed-rejected"}:
        return SiteIssueCode.REQUEST_REJECTED
    if reason == "http-5xx":
        return SiteIssueCode.SITE_UNAVAILABLE
    if reason in {"network-timeout", "network-error", "signer-timeout"}:
        return SiteIssueCode.NETWORK_ERROR
    if reason.startswith("api-") or reason == "no-progress-timeout":
        return SiteIssueCode.SITE_RESPONSE_CHANGED

    if is_explicit_rate_limit_message(lowered) or any(
        marker in lowered
        for marker in (
            "http error 429",
            "http 429",
            "status code 429",
            "temporarily limited",
        )
    ):
        return SiteIssueCode.RATE_LIMITED

    if any(
        marker in lowered
        for marker in (
            "chrome cookies could not be read",
            "cookie is disabled for this task",
            "cookie was disabled when this task was created",
            "cookie database",
            "failed to load cookies",
            "failed to decrypt",
            "could not decrypt",
            "unsupported cookie-browser",
            "bound chrome profile",
        )
    ):
        return SiteIssueCode.COOKIE_UNAVAILABLE

    if not explicitly_not_auth and any(
        marker in lowered
        for marker in (
            "displayed an explicit verification challenge",
            "redirected to an explicit login or verification page",
            "requires a captcha",
            "requires verification",
            "verification required",
            "verify you are human",
            "验证码",
            "安全验证",
        )
    ):
        return SiteIssueCode.VERIFICATION_REQUIRED
    if not explicitly_not_auth and any(
        marker in lowered
        for marker in (
            "login required",
            "requires login",
            "please log in",
            "please sign in",
            "log into an account",
            "need to be logged in",
            "need be logged in",
            "must be logged in",
            "you are not logged in",
            "you're not logged in",
            "login expired",
            "session expired",
            "do not have permission to view this post",
            "user's account is private. log into",
            "no current authenticated",
            "did not accept the selected chrome profile's login session",
            "fresh browser cookies",
            "请先登录",
            "登录后",
        )
    ):
        return SiteIssueCode.LOGIN_REQUIRED

    if any(
        marker in lowered
        for marker in (
            "still processing",
            "being processed",
            "being transcoded",
            "processing this video",
            "under review",
            "正在处理",
            "审核中",
            "转码中",
        )
    ):
        return SiteIssueCode.SITE_PROCESSING
    if any(
        marker in lowered
        for marker in (
            "not available in your country",
            "not available in your region",
            "video is not available in your region",
            "content is not available in your region",
            "geo-restricted",
            "geo restricted",
            "region restricted",
            "http error 451",
        )
    ):
        return SiteIssueCode.REGION_RESTRICTED
    if any(
        marker in lowered
        for marker in (
            "media endpoint returned http 401",
            "media endpoint returned http 404",
            "media endpoint returned http 410",
            "media endpoint returned an empty response",
        )
    ):
        return SiteIssueCode.MEDIA_LINK_EXPIRED
    if any(
        marker in lowered
        for marker in (
            "media link expired",
            "media url expired",
            "signature expired",
            "url has expired",
            "access token invalid",
            "saved access token",
        )
    ):
        return SiteIssueCode.MEDIA_LINK_EXPIRED
    if any(
        marker in lowered
        for marker in (
            "http error 404",
            "http error 410",
            "http 404",
            "http 410",
            "video is unavailable",
            "video unavailable",
            "video not available",
            "content is unavailable",
            "private video",
            "this video is private",
            "video is private",
            "has been removed",
            "has been deleted",
            "no longer available",
            "不存在或当前不可见",
        )
    ):
        return SiteIssueCode.CONTENT_UNAVAILABLE
    if any(
        marker in lowered
        for marker in (
            "untrusted url",
            "untrusted douyin media url",
            "outside the trusted",
            "unrecognized douyin cdn",
            "redirect could not be trusted",
            "nonstandard-port",
            "blocked media route",
            "security check",
            "tls certificate validation failed",
        )
    ):
        return SiteIssueCode.SECURITY_BLOCKED
    if any(
        marker in lowered
        for marker in (
            "different video",
            "different author",
            "different note",
            "cross-wired",
            "identity or integrity",
            "identity changed",
            "integrity validation",
            "incomplete verified",
            "missing aweme",
            "no verified media identity",
            "response changed",
            "did not match the verified",
            "below its declared",
            "could not verify",
            "could not be verified",
            "returned no note data",
            "blank browser response",
            "no trusted notes",
            "did not return video data",
            "did not return an mp4 file",
            "returned text or metadata instead of",
            "returned an empty file",
            "returned no verified",
            "did not return a verified",
            "could not find the requested video in its verified author feed",
            "returned no complete video or image posts",
            "profile identity is missing",
            "url has no video identifier",
            "did not include a verified direct highest-quality rendition",
            "media size changed between the range probe and local probe",
            "media content changed between the range probe and local probe",
        )
    ):
        return SiteIssueCode.SITE_RESPONSE_CHANGED
    if any(
        marker in lowered
        for marker in (
            "media endpoint returned http 425",
            "http error 403",
            "http 403",
            "status code 403",
            "forbidden",
            "access denied",
            "request rejected",
            "temporarily rejected",
            "风控",
            "网络环境存在风险",
            "ip address is blocked",
            "your ip is blocked",
        )
    ):
        return SiteIssueCode.REQUEST_REJECTED
    if re.search(r"\b(?:http(?: error)?\s*)?5\d\d\b", lowered) or any(
        marker in lowered
        for marker in (
            "service unavailable",
            "server unavailable",
            "server error",
            "bad gateway",
        )
    ):
        return SiteIssueCode.SITE_UNAVAILABLE
    if any(
        marker in lowered
        for marker in (
            "media endpoint returned http 408",
            "secure media connection failed",
            "incomplete media response",
            "network error",
            "network request failed",
            "connection failed",
            "connection reset",
            "connection refused",
            "timed out",
            "timeout",
            "no media progress",
            "local dns or web filter blocked",
            "blocked.dnsfilter.com",
        )
    ):
        return SiteIssueCode.NETWORK_ERROR
    if any(
        marker in lowered
        for marker in (
            "ffprobe was not found",
            "ffprobe was found but could not be started",
            "ffmpeg was not found",
        )
    ):
        return SiteIssueCode.LOCAL_CONFIGURATION
    return SiteIssueCode.UNKNOWN


class DownloaderCoreError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        issue_code: SiteIssueCode | None = None,
        diagnostic_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.issue_code = issue_code
        self.diagnostic_code = diagnostic_code


class AuthenticationRequiredError(DownloaderCoreError):
    def __init__(
        self,
        message: str,
        verification_url: str | None = None,
        *,
        issue_code: SiteIssueCode | None = None,
    ) -> None:
        super().__init__(message, issue_code=issue_code)
        self.verification_url = verification_url


class DownloadCancelledError(DownloaderCoreError):
    pass


class DiscoveryError(DownloaderCoreError):
    pass


class TemporaryAccessError(DownloaderCoreError):
    pass


class DouyinMediaRefreshRequiredError(TemporaryAccessError):
    """A verified Douyin source must be rediscovered before one safe retry."""


class MediaDownloadError(DownloaderCoreError):
    pass
