from __future__ import annotations

import contextlib
import html
import json
import re
import threading
import time
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from typing import Any, Callable
from urllib.error import HTTPError as UrllibHTTPError
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

from yt_dlp.cookies import extract_cookies_from_browser

from .browser import chrome_user_agent
from .errors import (
    AuthenticationRequiredError,
    DiscoveryError,
    DownloadCancelledError,
    SiteIssueCode,
    TemporaryAccessError,
    is_explicit_rate_limit_message,
)


CancelCallback = Callable[[], bool]
StatusCallback = Callable[[str], None]

_SIGNING_PAGE_URL = "https://www.douyin.com/__original_media_signing__"
_DETAIL_API_PATH = "/aweme/v1/web/aweme/detail/"
_PROFILE_API_PATH = "/aweme/v1/web/aweme/post/"
_DETAIL_REQUEST_ATTEMPTS = 3
_DETAIL_RETRY_BASE_MS = 1_000
_DETAIL_SIGNING_SESSION_ATTEMPTS = 3
_DETAIL_SIGNING_RETRY_BASE_MS = 5_000
# Douyin currently returns an empty status-only second page for this profile when
# count=18, while count=50 returns all 26 records and remains cursor-paginated for
# larger profiles.
_PROFILE_PAGE_SIZE = 50
_PROFILE_FALLBACK_PAGE_SIZE = 18
_PROFILE_REQUEST_ATTEMPTS = 3
_PROFILE_RETRY_BASE_MS = 1_000
_PROFILE_SIGNING_SESSION_ATTEMPTS = 3
_PROFILE_SIGNING_RETRY_BASE_MS = 5_000
_POLL_INTERVAL_MS = 200
_SIGNED_NO_PROGRESS_TIMEOUT_SECONDS = 120.0
_MAX_GLUE_TAGS = 16
_MAX_GLUE_BYTES = 1_000_000
_MAX_SOURCE_HTML_BYTES = 5_000_000
_MAX_PAGE_DETAIL_BODY_BYTES = 2_000_000
_MAX_PACE_ENTRIES = 64
_MAX_PACE_FRAGMENT_BYTES = 1_000_000
_MAX_PACE_TOTAL_BYTES = 2_000_000
_MAX_FLIGHT_FRAMES = 256
_MAX_FLIGHT_FRAME_BYTES = 1_000_000
_MAX_FLIGHT_JSON_RECORDS = 128
_MAX_SSR_IMAGES = 100
_MAX_SSR_MEDIA_URLS = 5
_MAX_SSR_MEDIA_URL_BYTES = 8_192
_SIGNED_FETCH_LOCK = threading.Lock()
_TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_TRUSTED_DOUYIN_AUTH_HOSTS = frozenset(
    {"douyin.com", "www.douyin.com", "sso.douyin.com"}
)
_EXPLICIT_AUTH_PATH_MARKERS = (
    "/captcha",
    "/login",
    "/passport/",
    "/safe/",
    "/verify",
)
_EXPLICIT_AUTH_API_MARKERS = (
    "captcha",
    "verify you are human",
    "complete the verification",
    "security verification",
    "login required",
    "please login",
    "please log in",
    "sign in required",
    "验证码",
    "安全验证",
    "请登录",
    "登录后",
    "当前未登录",
    "登录状态已过期",
    "session expired",
    "not logged in",
)
_DNS_FILTER_BLOCK_HOST = "blocked.dnsfilter.com"

# These two official runtimes are required by the current SecSDK glue bootstrap.
# The glue script itself is always taken from the current Douyin HTML instead of
# pinning a version here.
_SECURITY_RUNTIME_SCRIPTS = (
    (
        "https://lf-security.bytegoofy.com/obj/security-secsdk-gray/"
        "runtime_bundler_34.js",
        ' project-id="34"',
    ),
    (
        "https://lf-c-flwb.bytetos.com/obj/rc-client-security/"
        "c-webmssdk/1.0.0.20/webmssdk.es5.js",
        "",
    ),
)

_TRUSTED_SCRIPT_HOST_SUFFIXES = (
    ".bytegoofy.com",
    ".bytedance.com",
    ".byted-static.com",
    ".bytetos.com",
    ".douyin.com",
    ".douyinstatic.com",
)

_ALLOWED_GLUE_ATTRIBUTES = {
    "crossorigin",
    "id",
    "integrity",
    "referrerpolicy",
    "src",
    "type",
}

_START_SIGNED_FETCH_SCRIPT = r"""
({ path, params, timeoutMs }) => {
  const stateKey = "__originalMediaSignedDetail";
  const previous = window[stateKey];
  if (previous && previous.controller) previous.controller.abort();

  const controller = new AbortController();
  const state = { state: "pending", controller };
  window[stateKey] = state;
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const query = new URLSearchParams();
  for (const [name, value] of Object.entries(params)) {
    query.append(name, value);
  }

  fetch(`${path}?${query.toString()}`, {
    credentials: "include",
    signal: controller.signal,
  })
    .then(async (response) => {
      const responseText = await response.text();
      let payload = null;
      try {
        payload = JSON.parse(responseText);
      } catch (_) {
        let authKind = null;
        try {
          const responseUrl = new URL(response.url);
          const host = responseUrl.hostname.toLowerCase().replace(/[.]$/, "");
          const trustedHost = host === "douyin.com" || host.endsWith(".douyin.com");
          const standardPort = !responseUrl.port || responseUrl.port === "443";
          const authPath = decodeURIComponent(responseUrl.pathname).toLowerCase();
          if (responseUrl.protocol === "https:" && trustedHost && standardPort) {
            if (["/captcha", "/verify", "/safe/"].some((value) => authPath.includes(value))) {
              authKind = "verification_required";
            } else if (authPath.includes("/login") || authPath.includes("/passport/")) {
              authKind = "login_required";
            }
          }
        } catch (_) {}
        if (!authKind) {
          try {
            const documentValue = new DOMParser().parseFromString(responseText, "text/html");
            documentValue.querySelectorAll("script,style,template,noscript").forEach((node) => node.remove());
            const visibleText = (documentValue.body?.textContent || "").replace(/\s+/g, " ").toLowerCase();
            if ([
              "verify you are human",
              "complete the verification",
              "complete the captcha",
              "请完成下列验证",
              "请完成验证",
              "请拖动滑块",
            ].some((value) => visibleText.includes(value))) {
              authKind = "verification_required";
            } else if ([
              "登录后继续",
              "登录后查看",
              "请先登录",
              "当前未登录",
              "登录状态已过期",
              "session expired",
              "not logged in",
            ].some((value) => visibleText.includes(value))) {
              authKind = "login_required";
            }
          } catch (_) {}
        }
        window[stateKey] = {
          state: "error",
          reason: "invalid_json",
          httpStatus: response.status,
          authKind,
        };
        return;
      }
      window[stateKey] = {
        state: "done",
        httpStatus: response.status,
        payload,
      };
    })
    .catch(() => {
      window[stateKey] = { state: "error", reason: "request_failed" };
    })
    .finally(() => clearTimeout(timer));
}
"""

_READ_SIGNED_FETCH_SCRIPT = """
() => {
  const value = window.__originalMediaSignedDetail;
  if (!value) return { state: "missing" };
  if (value.state === "pending") return { state: "pending" };
  return value;
}
"""

_ABORT_SIGNED_FETCH_SCRIPT = """
() => {
  const value = window.__originalMediaSignedDetail;
  if (value && value.controller) value.controller.abort();
}
"""


class _SigningFailure(RuntimeError):
    pass


class _IdentitySigningFailure(_SigningFailure):
    pass


class _TransientSigningFailure(_SigningFailure):
    def __init__(self, message: str, *, category: str = "transient") -> None:
        super().__init__(message)
        self.category = category


class _NetworkFilterSigningFailure(_SigningFailure):
    pass


class _AuthenticationSigningFailure(_SigningFailure):
    def __init__(
        self,
        message: str,
        *,
        issue_code: SiteIssueCode = SiteIssueCode.LOGIN_REQUIRED,
    ) -> None:
        super().__init__(message)
        self.issue_code = issue_code


class _CookieAccessSigningFailure(_SigningFailure):
    pass


class _SigningNoProgressTimeout(_TransientSigningFailure):
    pass


class _NoProgressBudget:
    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = max(float(timeout_seconds), 0.001)
        self._deadline = time.monotonic() + self.timeout_seconds
        self._last_failure_category: str | None = None

    def _raise_timeout(self) -> None:
        raise _SigningNoProgressTimeout(
            "Douyin signed discovery made no verified progress within the "
            f"{self.timeout_seconds:g}-second safety window",
            category=self._last_failure_category or "no-progress-timeout",
        )

    def remaining_seconds(self) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            self._raise_timeout()
        return remaining

    def clamp_timeout_ms(self, requested_ms: int) -> int:
        remaining = self.remaining_seconds()
        requested = max(int(requested_ms), 1)
        remaining_ms = int(remaining * 1_000)
        if remaining_ms < 1:
            self._raise_timeout()
        return min(requested, remaining_ms)

    def note_failure(self, exc: BaseException) -> None:
        if not isinstance(exc, _SigningNoProgressTimeout):
            self._last_failure_category = _transient_reason_category(exc)

    def refresh(self) -> None:
        self._deadline = time.monotonic() + self.timeout_seconds
        self._last_failure_category = None


def new_signed_discovery_budget() -> _NoProgressBudget:
    return _NoProgressBudget(_SIGNED_NO_PROGRESS_TIMEOUT_SECONDS)


def _emit_status(status_callback: StatusCallback | None, message: str) -> None:
    if status_callback:
        status_callback(message)


def _http_retry_category(status: int) -> str:
    if status == 403:
        return "http-403"
    if status == 429:
        return "http-429"
    if status >= 500:
        return "http-5xx"
    return f"http-{status}"


def _transient_reason_category(exc: BaseException) -> str:
    if isinstance(exc, _TransientSigningFailure):
        return exc.category
    if isinstance(exc, _SigningFailure):
        return "source-validation"
    message = str(exc).lower()
    if any(value in message for value in ("timeout", "timed out", "network")):
        return "network-timeout"
    return "network-error"


def _transient_site_issue_code(category: str) -> SiteIssueCode:
    return {
        "api-rate-limit": SiteIssueCode.RATE_LIMITED,
        "http-429": SiteIssueCode.RATE_LIMITED,
        "http-403": SiteIssueCode.REQUEST_REJECTED,
        "signed-rejected": SiteIssueCode.REQUEST_REJECTED,
        "http-5xx": SiteIssueCode.SITE_UNAVAILABLE,
        "network-timeout": SiteIssueCode.NETWORK_ERROR,
        "network-error": SiteIssueCode.NETWORK_ERROR,
        "signer-timeout": SiteIssueCode.NETWORK_ERROR,
    }.get(category, SiteIssueCode.SITE_RESPONSE_CHANGED)


class _QuietCookieLogger:
    def debug(self, message: str) -> None:
        pass

    def info(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass


def _is_trusted_script_url(value: str) -> bool:
    parsed = urlsplit(value)
    if not parsed.scheme and not parsed.netloc:
        return value.startswith("/") and not value.startswith("//")
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    hostname = parsed.hostname.lower()
    return any(
        hostname == suffix[1:] or hostname.endswith(suffix)
        for suffix in _TRUSTED_SCRIPT_HOST_SUFFIXES
    )


def _douyin_target(value: str) -> tuple[str, str] | None:
    try:
        parsed = urlsplit(value)
    except (TypeError, ValueError):
        return None
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or hostname not in {"douyin.com", "www.douyin.com"}:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    try:
        if parsed.port not in {None, 443}:
            return None
    except ValueError:
        return None
    path = unquote(parsed.path)
    profile_match = re.fullmatch(r"/user/([^/]+)/?", path)
    if profile_match:
        return "user", profile_match.group(1)
    video_match = re.fullmatch(r"/video/([0-9]+)/?", path)
    if video_match:
        return "video", video_match.group(1)
    note_match = re.fullmatch(r"/note/([0-9]+)/?", path)
    if note_match:
        return "video", note_match.group(1)
    return None


def _is_douyin_url(value: str) -> bool:
    return _douyin_target(value) is not None


def _is_sdk_glue_attribute(name: str) -> bool:
    normalized = name.lower()
    return normalized == "data-sdk-glue" or normalized.startswith("data-sdk-glue-")


def _render_script_start_tag(attributes: list[tuple[str, str | None]]) -> str:
    rendered: list[str] = []
    has_glue_marker = False
    for raw_name, value in attributes:
        name = raw_name.lower()
        if _is_sdk_glue_attribute(name):
            has_glue_marker = True
        is_glue_attribute = _is_sdk_glue_attribute(name)
        if name not in _ALLOWED_GLUE_ATTRIBUTES and not is_glue_attribute:
            continue
        if name == "src" and (not value or not _is_trusted_script_url(value)):
            raise _SigningFailure("Douyin returned an untrusted SecSDK script URL")
        if value is None:
            rendered.append(name)
        else:
            rendered.append(f'{name}="{html.escape(value, quote=True)}"')
    if not has_glue_marker:
        raise _SigningFailure("Douyin SecSDK marker is missing")
    return "<script " + " ".join(rendered) + ">"


class _SdkGlueParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.tags: list[str] = []
        self._current: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        if not any(_is_sdk_glue_attribute(name) for name, _ in attrs):
            return
        if self._current is not None:
            raise _SigningFailure("Douyin returned nested SecSDK script tags")
        self._current = [_render_script_start_tag(attrs)]

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "script" and any(
            _is_sdk_glue_attribute(name) for name, _ in attrs
        ):
            raise _SigningFailure("Douyin returned an incomplete SecSDK script tag")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "script" or self._current is None:
            return
        self._current.append("</script>")
        self.tags.append("".join(self._current))
        self._current = None

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current.append(data)

    def handle_entityref(self, name: str) -> None:
        if self._current is not None:
            self._current.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._current is not None:
            self._current.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        if self._current is not None:
            self._current.append(f"<!--{data}-->")

    def close(self) -> None:
        super().close()
        if self._current is not None:
            raise _SigningFailure("Douyin returned an incomplete SecSDK script tag")


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._hidden_depth = 0
        self.values: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.lower() in {"script", "style", "template", "noscript"}:
            self._hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if (
            tag.lower() in {"script", "style", "template", "noscript"}
            and self._hidden_depth
        ):
            self._hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden_depth and data.strip():
            self.values.append(data)


def _explicit_auth_html_issue_code(source_html: str) -> SiteIssueCode | None:
    parser = _VisibleTextParser()
    try:
        parser.feed(source_html)
        parser.close()
    except Exception:
        return None
    visible_text = re.sub(r"\s+", " ", " ".join(parser.values)).lower()
    if any(
        marker in visible_text
        for marker in (
            "verify you are human",
            "complete the verification",
            "complete the captcha",
            "请完成下列验证",
            "请完成验证",
            "请拖动滑块",
        )
    ):
        return SiteIssueCode.VERIFICATION_REQUIRED
    if any(
        marker in visible_text
        for marker in (
            "登录后继续",
            "登录后查看",
            "请先登录",
            "当前未登录",
            "登录状态已过期",
            "session expired",
            "not logged in",
        )
    ):
        return SiteIssueCode.LOGIN_REQUIRED
    return None


def _has_explicit_auth_html(source_html: str) -> bool:
    return _explicit_auth_html_issue_code(source_html) is not None


def _has_explicit_rate_limit_api_message(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    message = str(
        payload.get("status_msg")
        or payload.get("status_message")
        or payload.get("statusMsg")
        or payload.get("statusMessage")
        or payload.get("message")
        or ""
    ).lower()
    return is_explicit_rate_limit_message(message)


def _explicit_auth_api_issue_code(payload: Any) -> SiteIssueCode | None:
    if not isinstance(payload, dict):
        return None
    message = str(
        payload.get("status_msg")
        or payload.get("status_message")
        or payload.get("statusMsg")
        or payload.get("statusMessage")
        or payload.get("message")
        or ""
    ).lower()
    if not any(marker in message for marker in _EXPLICIT_AUTH_API_MARKERS):
        return None
    if any(
        marker in message
        for marker in (
            "captcha",
            "verification",
            "verify you are human",
            "验证码",
            "安全验证",
        )
    ):
        return SiteIssueCode.VERIFICATION_REQUIRED
    return SiteIssueCode.LOGIN_REQUIRED


def _has_explicit_auth_api_message(payload: Any) -> bool:
    return _explicit_auth_api_issue_code(payload) is not None


def _extract_sdk_glue_tags(source_html: str) -> tuple[str, ...]:
    if not isinstance(source_html, str) or not source_html.strip():
        raise _TransientSigningFailure(
            "Douyin returned an empty HTML response",
            category="signer-html",
        )
    if len(source_html.encode("utf-8")) > _MAX_SOURCE_HTML_BYTES:
        raise _SigningFailure("Douyin HTML response was unexpectedly large")
    _raise_if_dns_filter_block_page(source_html)
    parser = _SdkGlueParser()
    try:
        parser.feed(source_html)
        parser.close()
    except _SigningFailure:
        raise
    except Exception as exc:
        raise _SigningFailure("Douyin SecSDK HTML could not be parsed") from exc
    if not parser.tags:
        auth_issue = _explicit_auth_html_issue_code(source_html)
        if auth_issue is not None:
            raise _AuthenticationSigningFailure(
                "Douyin HTML displayed an explicit authentication request",
                issue_code=auth_issue,
            )
        raise _TransientSigningFailure(
            "Douyin SecSDK glue was not present in the HTML",
            category="signer-html",
        )
    if len(parser.tags) > _MAX_GLUE_TAGS:
        raise _SigningFailure("Douyin returned too many SecSDK glue tags")
    if sum(len(tag.encode("utf-8")) for tag in parser.tags) > _MAX_GLUE_BYTES:
        raise _SigningFailure("Douyin SecSDK glue was unexpectedly large")
    return tuple(parser.tags)


def _build_signing_document(glue_tags: tuple[str, ...]) -> str:
    joined_glue = "\n".join(glue_tags)
    runtime_tags = []
    for source, extra_attributes in _SECURITY_RUNTIME_SCRIPTS:
        if source not in joined_glue:
            escaped_source = html.escape(source, quote=True)
            runtime_tags.append(
                f'<script src="{escaped_source}"{extra_attributes}></script>'
            )
    scripts = "\n".join([*runtime_tags, joined_glue])
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f"{scripts}</head><body></body></html>"
    )


def _load_chrome_cookie_jar(cookie_profile: str | None) -> CookieJar:
    try:
        return extract_cookies_from_browser(
            "chrome", profile=cookie_profile, logger=_QuietCookieLogger()
        )
    except Exception as exc:
        raise _CookieAccessSigningFailure("Chrome cookies could not be read") from exc


def _cookie_jar_to_playwright(cookie_jar: CookieJar) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    now = time.time()
    for cookie in cookie_jar:
        domain = cookie.domain.lower().lstrip(".")
        if domain != "douyin.com" and not domain.endswith(".douyin.com"):
            continue
        item: dict[str, Any] = {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path or "/",
            "secure": bool(cookie.secure),
        }
        if cookie.expires:
            expires = float(cookie.expires)
            if expires > 10_000_000_000_000:
                expires = expires / 1_000_000 - 11_644_473_600
            if expires <= now:
                continue
            if now < expires <= 253_402_300_799:
                item["expires"] = int(expires)
        if cookie.has_nonstandard_attr("HttpOnly"):
            item["httpOnly"] = True
        result.append(item)
    if not result:
        raise _AuthenticationSigningFailure(
            "No current Douyin Chrome cookies were available"
        )
    return result


def _is_allowed_douyin_origin(value: str) -> bool:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in {
        "douyin.com",
        "www.douyin.com",
    }:
        return False
    try:
        return parsed.port in {None, 443}
    except ValueError:
        return False


def _is_dns_filter_block_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and (parsed.hostname or "").lower() == _DNS_FILTER_BLOCK_HOST
            and parsed.username is None
            and parsed.password is None
            and parsed.port in {None, 443}
        )
    except (TypeError, ValueError):
        return False


def _is_dns_filter_block_page(source_html: str) -> bool:
    if not isinstance(source_html, str):
        return False
    return bool(
        re.search(
            r"<title\b[^>]*>\s*website\s+filtered\s*</title>",
            source_html,
            re.IGNORECASE,
        )
        and re.search(
            r"https://blocked[.]dnsfilter[.]com(?:[/:?#]|[\"'])",
            source_html,
            re.IGNORECASE,
        )
    )


def _raise_if_dns_filter_block_page(source_html: str) -> None:
    if _is_dns_filter_block_page(source_html):
        raise _NetworkFilterSigningFailure(
            "A local DNS or web filter blocked Douyin before the site loaded"
        )


def _explicit_auth_url_issue_code(value: str) -> SiteIssueCode | None:
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or hostname not in _TRUSTED_DOUYIN_AUTH_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
        ):
            return None
        path = unquote(parsed.path).lower()
    except (TypeError, ValueError):
        return None
    if not any(marker in path for marker in _EXPLICIT_AUTH_PATH_MARKERS):
        return None
    if "/captcha" in path or "/verify" in path or "/safe/" in path:
        return SiteIssueCode.VERIFICATION_REQUIRED
    return SiteIssueCode.LOGIN_REQUIRED


def _is_explicit_auth_url(value: str) -> bool:
    return _explicit_auth_url_issue_code(value) is not None


def _fetch_source_html_with_urllib(
    verification_url: str,
    cookie_jar: CookieJar,
    user_agent: str,
    timeout_ms: int,
    *,
    budget: _NoProgressBudget | None = None,
    should_cancel: CancelCallback | None = None,
) -> str:
    request = Request(
        verification_url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
        method="GET",
    )
    opener = build_opener(HTTPCookieProcessor(cookie_jar))
    try:
        _raise_if_cancelled(should_cancel)
        if budget is not None:
            budget.remaining_seconds()
        with opener.open(request, timeout=max(timeout_ms / 1_000, 0.001)) as response:
            _raise_if_cancelled(should_cancel)
            if budget is not None:
                budget.remaining_seconds()
            status = getattr(response, "status", None)
            redirect_issue = _explicit_auth_url_issue_code(response.geturl())
            if redirect_issue is not None:
                raise _AuthenticationSigningFailure(
                    "Douyin HTML redirected to an explicit login or verification page",
                    issue_code=redirect_issue,
                )
            if _is_dns_filter_block_url(response.geturl()):
                raise _NetworkFilterSigningFailure(
                    "A local DNS or web filter redirected the Douyin request"
                )
            if not _is_allowed_douyin_origin(response.geturl()):
                raise _SigningFailure(
                    "Douyin HTML redirected outside the trusted origin"
                )
            if type(status) is int and status in _TRANSIENT_HTTP_STATUSES:
                raise _TransientSigningFailure(
                    "Douyin HTML request was temporarily limited",
                    category=_http_retry_category(status),
                )
            if type(status) is int and status == 401:
                raise _AuthenticationSigningFailure(
                    "Douyin HTML request requires authentication"
                )
            if type(status) is int and status == 403:
                raise _TransientSigningFailure(
                    "Douyin HTML request was temporarily rejected",
                    category="http-403",
                )
            if type(status) is not int or not 200 <= status < 300:
                raise _SigningFailure("Douyin HTML returned an invalid HTTP status")
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > _MAX_SOURCE_HTML_BYTES:
                        raise _SigningFailure(
                            "Douyin HTML response was unexpectedly large"
                        )
                except ValueError as exc:
                    raise _SigningFailure(
                        "Douyin HTML returned an invalid content length"
                    ) from exc
            body = _read_limited_source_body(
                response,
                budget=budget,
                should_cancel=should_cancel,
            )
            charset = response.headers.get_content_charset() or "utf-8"
            try:
                return body.decode(charset)
            except (LookupError, UnicodeDecodeError) as exc:
                raise _SigningFailure(
                    "Douyin HTML response could not be decoded"
                ) from exc
    except UrllibHTTPError as exc:
        try:
            _raise_if_cancelled(should_cancel)
            if budget is not None:
                budget.remaining_seconds()
            redirect_issue = _explicit_auth_url_issue_code(exc.geturl())
            if redirect_issue is not None:
                raise _AuthenticationSigningFailure(
                    "Douyin HTML redirected to an explicit login or verification page",
                    issue_code=redirect_issue,
                ) from exc
            if _is_dns_filter_block_url(exc.geturl()):
                raise _NetworkFilterSigningFailure(
                    "A local DNS or web filter redirected the Douyin request"
                ) from exc
            if not _is_allowed_douyin_origin(exc.geturl()):
                raise _SigningFailure(
                    "Douyin HTML redirected outside the trusted origin"
                ) from exc
            if exc.code in _TRANSIENT_HTTP_STATUSES:
                raise _TransientSigningFailure(
                    "Douyin HTML request was temporarily limited",
                    category=_http_retry_category(exc.code),
                ) from exc
            if exc.code == 401:
                raise _AuthenticationSigningFailure(
                    "Douyin HTML request requires authentication"
                ) from exc
            if exc.code == 403:
                try:
                    body = _read_limited_source_body(
                        exc,
                        budget=budget,
                        should_cancel=should_cancel,
                    )
                    charset = exc.headers.get_content_charset() or "utf-8"
                    source_html = body.decode(charset, errors="replace")
                except Exception:
                    source_html = ""
                _raise_if_dns_filter_block_page(source_html)
                auth_issue = _explicit_auth_html_issue_code(source_html)
                if auth_issue is not None:
                    raise _AuthenticationSigningFailure(
                        "Douyin HTML displayed an explicit authentication request",
                        issue_code=auth_issue,
                    ) from exc
                raise _TransientSigningFailure(
                    "Douyin HTML request was temporarily rejected",
                    category="http-403",
                ) from exc
            raise _SigningFailure(
                "Douyin HTML returned an invalid HTTP status"
            ) from exc
        finally:
            with contextlib.suppress(Exception):
                exc.close()


def _read_limited_source_body(
    response: Any,
    *,
    budget: _NoProgressBudget | None,
    should_cancel: CancelCallback | None,
) -> bytes:
    body = bytearray()
    reader = getattr(response, "read1", None)
    if not callable(reader):
        reader = response.read
    while True:
        _raise_if_cancelled(should_cancel)
        if budget is not None:
            budget.remaining_seconds()
        remaining = _MAX_SOURCE_HTML_BYTES + 1 - len(body)
        chunk = reader(min(64 * 1_024, remaining))
        _raise_if_cancelled(should_cancel)
        if budget is not None:
            budget.remaining_seconds()
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > _MAX_SOURCE_HTML_BYTES:
            raise _SigningFailure("Douyin HTML response was unexpectedly large")


def _extract_glue_with_context_fallback(
    context: Any,
    verification_url: str,
    cookie_jar: CookieJar,
    user_agent: str,
    timeout_ms: int,
    should_cancel: CancelCallback | None,
    *,
    budget: _NoProgressBudget | None = None,
    status_callback: StatusCallback | None = None,
) -> tuple[tuple[str, ...], bool]:
    budget = budget or _NoProgressBudget(_SIGNED_NO_PROGRESS_TIMEOUT_SECONDS)
    _emit_status(status_callback, "Fetching Douyin signing HTML")
    try:
        source_html = _fetch_source_html_with_urllib(
            verification_url,
            cookie_jar,
            user_agent,
            budget.clamp_timeout_ms(timeout_ms),
            budget=budget,
            should_cancel=should_cancel,
        )
        budget.remaining_seconds()
        return _extract_sdk_glue_tags(source_html), True
    except DownloadCancelledError:
        raise
    except _AuthenticationSigningFailure:
        raise
    except _NetworkFilterSigningFailure:
        raise
    except Exception as exc:
        _raise_if_cancelled(should_cancel)
        budget.note_failure(exc)
        budget.remaining_seconds()
        _emit_status(
            status_callback,
            "Retrying Douyin signing HTML in the browser context "
            f"(reason: {_transient_reason_category(exc)})",
        )
        try:
            response = context.request.get(
                verification_url,
                timeout=budget.clamp_timeout_ms(timeout_ms),
            )
        except Exception as exc:
            budget.remaining_seconds()
            raise _TransientSigningFailure(
                "Douyin browser HTML request temporarily failed",
                category=_transient_reason_category(exc),
            ) from exc
        try:
            budget.remaining_seconds()
            status = response.status
            redirect_issue = _explicit_auth_url_issue_code(response.url)
            if redirect_issue is not None:
                raise _AuthenticationSigningFailure(
                    "Douyin HTML redirected to an explicit login or verification page",
                    issue_code=redirect_issue,
                )
            if _is_dns_filter_block_url(response.url):
                raise _NetworkFilterSigningFailure(
                    "A local DNS or web filter redirected the Douyin request"
                )
            if not _is_allowed_douyin_origin(response.url):
                raise _SigningFailure(
                    "Douyin HTML redirected outside the trusted origin"
                )
            if type(status) is int and status in _TRANSIENT_HTTP_STATUSES:
                raise _TransientSigningFailure(
                    "Douyin browser HTML request was temporarily limited",
                    category=_http_retry_category(status),
                )
            if type(status) is int and status == 401:
                raise _AuthenticationSigningFailure(
                    "Douyin browser HTML request requires authentication"
                )
            if type(status) is int and status == 403:
                source_html = response.text()
                budget.remaining_seconds()
                _raise_if_dns_filter_block_page(source_html)
                auth_issue = _explicit_auth_html_issue_code(source_html)
                if auth_issue is not None:
                    raise _AuthenticationSigningFailure(
                        "Douyin HTML displayed an explicit authentication request",
                        issue_code=auth_issue,
                    )
                raise _TransientSigningFailure(
                    "Douyin browser HTML request was temporarily rejected",
                    category="http-403",
                )
            if type(status) is not int or not 200 <= status < 300:
                raise _SigningFailure(
                    "Douyin HTML returned an invalid browser response"
                )
            source_html = response.text()
            budget.remaining_seconds()
            return _extract_sdk_glue_tags(source_html), False
        finally:
            with contextlib.suppress(Exception):
                response.dispose()


def _raise_if_cancelled(should_cancel: CancelCallback | None) -> None:
    if should_cancel and should_cancel():
        raise DownloadCancelledError("Task cancelled")


@contextlib.contextmanager
def _serialized_signed_fetch(
    should_cancel: CancelCallback | None,
    *,
    budget: _NoProgressBudget | None = None,
    status_callback: StatusCallback | None = None,
):
    budget = budget or _NoProgressBudget(_SIGNED_NO_PROGRESS_TIMEOUT_SECONDS)
    waiting_reported = False
    acquired = False
    while not acquired:
        _raise_if_cancelled(should_cancel)
        remaining = budget.remaining_seconds()
        acquired = _SIGNED_FETCH_LOCK.acquire(
            timeout=min(_POLL_INTERVAL_MS / 1_000, remaining)
        )
        if not acquired and not waiting_reported:
            _emit_status(
                status_callback,
                "Waiting for the Douyin signed request slot",
            )
            waiting_reported = True
    try:
        _raise_if_cancelled(should_cancel)
        budget.remaining_seconds()
        yield
    finally:
        if acquired:
            _SIGNED_FETCH_LOCK.release()


def _wait_with_cancel(
    page: Any,
    duration_ms: int,
    should_cancel: CancelCallback | None,
    *,
    budget: _NoProgressBudget | None = None,
) -> None:
    remaining = max(0, duration_ms)
    while remaining:
        _raise_if_cancelled(should_cancel)
        interval = min(_POLL_INTERVAL_MS, remaining)
        if budget is not None:
            interval = min(interval, budget.clamp_timeout_ms(interval))
        page.wait_for_timeout(interval)
        remaining -= interval
    _raise_if_cancelled(should_cancel)
    if budget is not None:
        budget.remaining_seconds()


def _wait_without_page_with_cancel(
    duration_ms: int,
    should_cancel: CancelCallback | None,
    *,
    budget: _NoProgressBudget | None = None,
) -> None:
    deadline = time.monotonic() + max(duration_ms, 0) / 1_000
    while True:
        _raise_if_cancelled(should_cancel)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if budget is not None:
                budget.remaining_seconds()
            return
        interval = min(_POLL_INTERVAL_MS / 1_000, remaining)
        if budget is not None:
            interval = min(interval, budget.remaining_seconds())
        time.sleep(interval)


def _wait_for_signer(
    page: Any,
    timeout_ms: int,
    should_cancel: CancelCallback | None,
    *,
    budget: _NoProgressBudget | None = None,
) -> None:
    effective_timeout_ms = (
        budget.clamp_timeout_ms(timeout_ms) if budget is not None else timeout_ms
    )
    deadline = time.monotonic() + effective_timeout_ms / 1_000
    while time.monotonic() < deadline:
        _raise_if_cancelled(should_cancel)
        if budget is not None:
            budget.remaining_seconds()
        if page.evaluate("() => typeof window.useWebSecsdkApi === 'function'"):
            return
        interval = min(
            _POLL_INTERVAL_MS,
            max(1, int((deadline - time.monotonic()) * 1_000)),
        )
        if budget is not None:
            interval = min(interval, budget.clamp_timeout_ms(interval))
        page.wait_for_timeout(interval)
    _raise_if_cancelled(should_cancel)
    if budget is not None:
        budget.remaining_seconds()
    raise _TransientSigningFailure(
        "Douyin SecSDK did not become ready",
        category="signer-timeout",
    )


def _start_signed_fetch(
    page: Any,
    path: str,
    params: dict[str, str],
    timeout_ms: int,
) -> None:
    page.evaluate(
        _START_SIGNED_FETCH_SCRIPT,
        {"path": path, "params": params, "timeoutMs": timeout_ms},
    )


def _wait_for_signed_response(
    page: Any,
    timeout_ms: int,
    should_cancel: CancelCallback | None,
    *,
    budget: _NoProgressBudget | None = None,
) -> dict[str, Any]:
    effective_timeout_ms = (
        budget.clamp_timeout_ms(timeout_ms) if budget is not None else timeout_ms
    )
    deadline = time.monotonic() + effective_timeout_ms / 1_000
    while time.monotonic() < deadline:
        if should_cancel and should_cancel():
            with contextlib.suppress(Exception):
                page.evaluate(_ABORT_SIGNED_FETCH_SCRIPT)
            raise DownloadCancelledError("Task cancelled")
        if budget is not None:
            budget.remaining_seconds()
        result = page.evaluate(_READ_SIGNED_FETCH_SCRIPT)
        if not isinstance(result, dict):
            raise _SigningFailure("Douyin returned an invalid signed response")
        state = result.get("state")
        if state == "done":
            return result
        if state not in {"missing", "pending"}:
            http_status = result.get("httpStatus")
            auth_kind = result.get("authKind")
            if auth_kind == SiteIssueCode.VERIFICATION_REQUIRED.value:
                raise _AuthenticationSigningFailure(
                    "Douyin signed response displayed an explicit verification page",
                    issue_code=SiteIssueCode.VERIFICATION_REQUIRED,
                )
            if auth_kind == SiteIssueCode.LOGIN_REQUIRED.value:
                raise _AuthenticationSigningFailure(
                    "Douyin signed response displayed an explicit login page",
                    issue_code=SiteIssueCode.LOGIN_REQUIRED,
                )
            if type(http_status) is int and http_status in _TRANSIENT_HTTP_STATUSES:
                raise _TransientSigningFailure(
                    "Douyin signed request returned a temporary HTTP status",
                    category=_http_retry_category(http_status),
                )
            if result.get("reason") == "request_failed":
                raise _TransientSigningFailure(
                    "Douyin signed network request temporarily failed",
                    category="network-error",
                )
            if type(http_status) is int and http_status == 401:
                raise _AuthenticationSigningFailure(
                    "Douyin signed request requires authentication"
                )
            if type(http_status) is int and http_status == 403:
                raise _TransientSigningFailure(
                    "Douyin signed request was temporarily rejected",
                    category="http-403",
                )
            raise _TransientSigningFailure(
                "Douyin temporarily rejected the signed request",
                category="signed-rejected",
            )
        interval = min(
            _POLL_INTERVAL_MS,
            max(1, int((deadline - time.monotonic()) * 1_000)),
        )
        if budget is not None:
            interval = min(interval, budget.clamp_timeout_ms(interval))
        page.wait_for_timeout(interval)
    with contextlib.suppress(Exception):
        page.evaluate(_ABORT_SIGNED_FETCH_SCRIPT)
    _raise_if_cancelled(should_cancel)
    if budget is not None:
        budget.remaining_seconds()
    raise _TransientSigningFailure(
        "Douyin signed request timed out",
        category="network-timeout",
    )


def _validated_payload(
    response: dict[str, Any],
    request_name: str,
) -> dict[str, Any]:
    http_status = response.get("httpStatus")
    if type(http_status) is int and http_status in _TRANSIENT_HTTP_STATUSES:
        raise _TransientSigningFailure(
            f"Douyin {request_name} request was temporarily limited",
            category=_http_retry_category(http_status),
        )
    payload = response.get("payload")
    if type(http_status) is int and http_status == 401:
        raise _AuthenticationSigningFailure(
            f"Douyin {request_name} request requires authentication"
        )
    if type(http_status) is int and http_status == 403:
        auth_issue = _explicit_auth_api_issue_code(payload)
        if auth_issue is not None:
            raise _AuthenticationSigningFailure(
                f"Douyin {request_name} API requires authentication",
                issue_code=auth_issue,
            )
        raise _TransientSigningFailure(
            f"Douyin {request_name} request was temporarily rejected",
            category="http-403",
        )
    if type(http_status) is not int or not 200 <= http_status < 300:
        raise _SigningFailure(
            f"Douyin {request_name} request returned an invalid HTTP status"
        )
    if not isinstance(payload, dict):
        raise _SigningFailure(f"Douyin {request_name} request returned no JSON object")
    status_code = payload.get("status_code")
    if type(status_code) is not int or status_code != 0:
        auth_issue = _explicit_auth_api_issue_code(payload)
        if auth_issue is not None:
            raise _AuthenticationSigningFailure(
                f"Douyin {request_name} API requires authentication",
                issue_code=auth_issue,
            )
        if _has_explicit_rate_limit_api_message(payload):
            raise _TransientSigningFailure(
                f"Douyin {request_name} API explicitly rate-limited the request",
                category="api-rate-limit",
            )
        raise _TransientSigningFailure(
            f"Douyin {request_name} API temporarily rejected the request",
            category="api-status-nonzero",
        )
    return payload


def _validate_detail_response(
    response: dict[str, Any],
    aweme_id: str,
    expected_sec_uid: str | None,
) -> dict[str, Any]:
    payload = _validated_payload(response, "detail")
    detail = payload.get("aweme_detail")
    filter_details = [payload.get("filter_detail")]
    if isinstance(detail, dict):
        filter_details.append(detail.get("filter_detail"))
    if any(
        isinstance(value, dict)
        and str(value.get("filter_reason") or "").strip().lower() == "images_base"
        for value in filter_details
    ):
        raise _TransientSigningFailure(
            "Douyin detail API returned an images_base-filtered minimal detail",
            category="api-filtered-images-base",
        )
    if not isinstance(detail, dict):
        raise _SigningFailure("Douyin detail API returned no aweme detail")
    actual_aweme_id = str(detail.get("aweme_id") or "").strip()
    if actual_aweme_id != aweme_id:
        raise _IdentitySigningFailure("Douyin detail API returned a different aweme")
    author = detail.get("author")
    if not isinstance(author, dict):
        raise _SigningFailure("Douyin detail API returned no author")
    actual_sec_uid = str(author.get("sec_uid") or "").strip()
    if not actual_sec_uid:
        raise _SigningFailure("Douyin detail API returned no author identity")
    if expected_sec_uid and actual_sec_uid != expected_sec_uid:
        raise _IdentitySigningFailure("Douyin detail API returned a different author")
    return detail


def _validate_profile_response(
    response: dict[str, Any],
    profile_id: str,
) -> tuple[list[dict[str, Any]], bool, str | None]:
    payload = _validated_payload(response, "profile")
    response_identity = str(
        payload.get("sec_uid") or payload.get("sec_user_id") or ""
    ).strip()
    raw_awemes = payload.get("aweme_list")
    if not isinstance(raw_awemes, list):
        raise _TransientSigningFailure(
            "Douyin profile API returned no aweme list",
            category="api-missing-aweme-list",
        )
    awemes: list[dict[str, Any]] = []
    for aweme in raw_awemes:
        if not isinstance(aweme, dict):
            raise _SigningFailure("Douyin profile API returned an invalid aweme")
        aweme_id = str(aweme.get("aweme_id") or "").strip()
        if not aweme_id.isdigit():
            raise _SigningFailure("Douyin profile API returned an invalid aweme ID")
        author = aweme.get("author")
        if not isinstance(author, dict):
            raise _SigningFailure("Douyin profile API returned no aweme author")
        sec_uid = str(author.get("sec_uid") or "").strip()
        if sec_uid != profile_id:
            raise _SigningFailure("Douyin profile API returned another author's aweme")
        video = aweme.get("video")
        images = aweme.get("images")
        has_video = isinstance(video, dict) and bool(video)
        has_images = isinstance(images, list) and bool(images)
        if not has_video and not has_images:
            raise _TransientSigningFailure(
                "Douyin profile API returned incomplete aweme media",
                category="api-incomplete-media",
            )
        awemes.append(aweme)

    has_more_value = payload.get("has_more")
    if type(has_more_value) is bool:
        has_more = has_more_value
    elif type(has_more_value) is int and has_more_value in {0, 1}:
        has_more = bool(has_more_value)
    else:
        raise _SigningFailure("Douyin profile API returned invalid pagination state")

    if not raw_awemes and not has_more and response_identity != profile_id:
        raise _TransientSigningFailure(
            "Douyin profile API returned an unbound empty terminal page",
            category="api-unbound-empty-page",
        )
    if response_identity and response_identity != profile_id:
        raise _SigningFailure("Douyin profile API returned a different profile")

    next_cursor_value = payload.get("max_cursor")
    next_cursor = (
        str(next_cursor_value).strip() if next_cursor_value is not None else None
    )
    if has_more and not next_cursor:
        raise _SigningFailure("Douyin profile API returned no next cursor")
    return awemes, has_more, next_cursor


def _close_resources(*resources: Any | None) -> None:
    for resource in resources:
        if resource is not None:
            with contextlib.suppress(Exception):
                resource.close()


def _is_target_page_detail_url(value: Any, aweme_id: str) -> bool:
    try:
        parsed = urlsplit(str(value))
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme != "https"
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or not (hostname == "douyin.com" or hostname.endswith(".douyin.com"))
            or parsed.port not in {None, 443}
            or parsed.path != _DETAIL_API_PATH
        ):
            return False
        return parse_qs(parsed.query, keep_blank_values=True).get("aweme_id") == [
            aweme_id
        ]
    except (TypeError, ValueError):
        return False


def _is_target_page_detail_request(request: Any, aweme_id: str) -> bool:
    return str(getattr(request, "method", "")) == "GET" and (
        _is_target_page_detail_url(getattr(request, "url", ""), aweme_id)
    )


_READ_PACE_FLIGHT_SCRIPT = r"""
({ maxEntries, maxFragmentBytes, maxTotalBytes }) => {
  const source = self.__pace_f;
  if (!Array.isArray(source)) return { state: "missing" };
  if (source.length > maxEntries) return { state: "too_many_entries" };
  const encoder = new TextEncoder();
  const fragments = [];
  let totalBytes = 0;
  for (const entry of source) {
    if (!Array.isArray(entry) || entry[0] !== 1) continue;
    const value = entry[1];
    if (typeof value !== "string") return { state: "invalid_fragment" };
    // Reject huge UTF-16 strings before asking TextEncoder to allocate a second
    // equally large buffer. The byte check below remains authoritative.
    if (value.length > maxFragmentBytes) return { state: "fragment_too_large" };
    const byteLength = encoder.encode(value).byteLength;
    if (byteLength > maxFragmentBytes) return { state: "fragment_too_large" };
    totalBytes += byteLength;
    if (totalBytes > maxTotalBytes) return { state: "stream_too_large" };
    fragments.push(value);
  }
  return { state: "done", fragments, totalBytes };
}
"""


def _parse_flight_json_records(fragments: Any) -> list[Any]:
    if not isinstance(fragments, list) or len(fragments) > _MAX_PACE_ENTRIES:
        raise _SigningFailure("Douyin SSR returned an invalid Flight fragment list")
    encoded_fragments: list[bytes] = []
    total_bytes = 0
    for fragment in fragments:
        if not isinstance(fragment, str):
            raise _SigningFailure("Douyin SSR returned a non-text Flight fragment")
        encoded = fragment.encode("utf-8")
        if len(encoded) > _MAX_PACE_FRAGMENT_BYTES:
            raise _SigningFailure("Douyin SSR Flight fragment was unexpectedly large")
        total_bytes += len(encoded)
        if total_bytes > _MAX_PACE_TOTAL_BYTES:
            raise _SigningFailure("Douyin SSR Flight stream was unexpectedly large")
        encoded_fragments.append(encoded)

    stream = b"".join(encoded_fragments)
    offset = 0
    frame_count = 0
    json_values: list[Any] = []
    while offset < len(stream):
        frame_count += 1
        if frame_count > _MAX_FLIGHT_FRAMES:
            raise _SigningFailure("Douyin SSR returned too many Flight frames")
        colon = stream.find(b":", offset, min(len(stream), offset + 18))
        if colon < 0 or not re.fullmatch(rb"[0-9A-Fa-f]+", stream[offset:colon]):
            raise _SigningFailure("Douyin SSR returned an invalid Flight frame header")
        payload_offset = colon + 1
        if stream[payload_offset : payload_offset + 1] == b"T":
            comma = stream.find(
                b",",
                payload_offset + 1,
                min(len(stream), payload_offset + 18),
            )
            length_text = stream[payload_offset + 1 : comma] if comma >= 0 else b""
            if comma < 0 or not re.fullmatch(rb"[0-9A-Fa-f]+", length_text):
                raise _SigningFailure(
                    "Douyin SSR returned an invalid Flight text frame length"
                )
            byte_length = int(length_text, 16)
            if byte_length > _MAX_FLIGHT_FRAME_BYTES:
                raise _SigningFailure(
                    "Douyin SSR Flight text frame was unexpectedly large"
                )
            frame_end = comma + 1 + byte_length
            if frame_end > len(stream):
                raise _SigningFailure(
                    "Douyin SSR returned a truncated Flight text frame"
                )
            offset = frame_end
            if stream[offset : offset + 1] == b"\n":
                offset += 1
            continue

        newline = stream.find(b"\n", payload_offset)
        if newline < 0:
            newline = len(stream)
        payload = stream[payload_offset:newline]
        if len(payload) > _MAX_FLIGHT_FRAME_BYTES:
            raise _SigningFailure("Douyin SSR Flight frame was unexpectedly large")
        offset = newline + 1 if newline < len(stream) else newline
        if not payload or payload[:1] not in b'[{"-0123456789tfn':
            continue
        if len(json_values) >= _MAX_FLIGHT_JSON_RECORDS:
            raise _SigningFailure("Douyin SSR returned too many JSON Flight frames")
        try:
            json_values.append(json.loads(payload.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            # React Flight also has non-JSON record kinds. A record that merely
            # begins like JSON is not authoritative unless it fully decodes.
            continue
    return json_values


def _ssr_media_urls(value: Any, *, nested_src: bool) -> list[str]:
    if not isinstance(value, list) or not value:
        return []
    if len(value) > _MAX_SSR_MEDIA_URLS:
        raise _SigningFailure("Douyin SSR returned too many media URL candidates")
    result: list[str] = []
    for candidate in value:
        candidate = (
            candidate.get("src")
            if nested_src and isinstance(candidate, dict)
            else candidate
        )
        if (
            not isinstance(candidate, str)
            or not candidate
            or len(candidate.encode("utf-8")) > _MAX_SSR_MEDIA_URL_BYTES
        ):
            return []
        if candidate not in result:
            result.append(candidate)
    return result


def _ssr_video_address(
    source: dict[str, Any],
    address_key: str,
    *,
    data_size_key: str,
) -> dict[str, Any] | None:
    urls = _ssr_media_urls(source.get(address_key), nested_src=True)
    if not urls:
        return None
    return {
        "uri": source.get("uri"),
        "url_list": urls,
        "width": source.get("width"),
        "height": source.get("height"),
        "data_size": source.get(data_size_key) or source.get("dataSize"),
    }


def _adapt_ssr_video(source: Any) -> dict[str, Any] | None:
    if not isinstance(source, dict) or not source:
        return None
    result: dict[str, Any] = {
        "uri": source.get("uri"),
        "width": source.get("width"),
        "height": source.get("height"),
        "duration": source.get("duration"),
        "data_size": source.get("dataSize"),
    }
    address_fields = (
        ("playAddr", "play_addr", "playAddrSize"),
        ("playAddrH264", "play_addr_h264", "playAddrH264Size"),
        ("playAddrH265", "play_addr_265", "playAddrH265Size"),
        ("playAddrBytevc1", "play_addr_bytevc1", "playAddrBytevc1Size"),
    )
    for source_key, result_key, size_key in address_fields:
        address = _ssr_video_address(source, source_key, data_size_key=size_key)
        if address is not None:
            result[result_key] = address

    raw_bit_rates = source.get("bitRateList")
    if raw_bit_rates is not None:
        if not isinstance(raw_bit_rates, list) or len(raw_bit_rates) > 64:
            raise _SigningFailure("Douyin SSR returned invalid video bitrates")
        bit_rates: list[dict[str, Any]] = []
        for value in raw_bit_rates:
            if not isinstance(value, dict):
                raise _SigningFailure("Douyin SSR returned an invalid video bitrate")
            address = _ssr_video_address(
                value,
                "playAddr",
                data_size_key="dataSize",
            )
            if address is None:
                continue
            bit_rates.append(
                {
                    "bit_rate": value.get("bitRate"),
                    "real_bit_rate": value.get("realBitrate"),
                    "is_h265": value.get("isH265"),
                    "is_bytevc1": value.get("isBytevc1"),
                    "width": value.get("width"),
                    "height": value.get("height"),
                    "format": value.get("format"),
                    "play_addr": address,
                }
            )
        if bit_rates:
            result["bit_rate"] = bit_rates
    return result


def _adapt_ssr_aweme_detail(detail: dict[str, Any]) -> dict[str, Any]:
    author_info = detail.get("authorInfo")
    if not isinstance(author_info, dict):
        raise _SigningFailure("Douyin SSR item returned no author")
    author = {
        "sec_uid": author_info.get("secUid"),
        "nickname": author_info.get("nickname"),
    }
    result: dict[str, Any] = {
        "aweme_id": detail.get("awemeId"),
        "aweme_type": detail.get("awemeType"),
        "media_type": detail.get("mediaType"),
        "create_time": detail.get("createTime"),
        "desc": detail.get("desc"),
        "item_title": detail.get("itemTitle"),
        "author": author,
    }
    raw_images = detail.get("images")
    if raw_images is not None:
        if not isinstance(raw_images, list) or len(raw_images) > _MAX_SSR_IMAGES:
            raise _SigningFailure("Douyin SSR returned an invalid image list")
        images: list[dict[str, Any]] = []
        for value in raw_images:
            if not isinstance(value, dict):
                raise _SigningFailure("Douyin SSR returned an invalid image")
            image: dict[str, Any] = {
                "uri": value.get("uri"),
                "width": value.get("width"),
                "height": value.get("height"),
                "url_list": _ssr_media_urls(
                    value.get("urlList"),
                    nested_src=False,
                ),
                "download_url_list": _ssr_media_urls(
                    value.get("downloadUrlList"),
                    nested_src=False,
                ),
            }
            video = _adapt_ssr_video(value.get("video"))
            if video is not None:
                image["video"] = video
            images.append(image)
        result["images"] = images
    video = _adapt_ssr_video(detail.get("video"))
    if video is not None:
        result["video"] = video
    return result


def _validated_ssr_wrapper_detail(
    wrapper: dict[str, Any],
    aweme_id: str,
    expected_sec_uid: str | None,
) -> dict[str, Any]:
    for payload, label in ((wrapper, "page"), (wrapper.get("aweme"), "item")):
        if not isinstance(payload, dict):
            raise _SigningFailure(f"Douyin SSR returned no {label} data")
        status_code = payload.get("statusCode")
        if type(status_code) is not int or status_code != 0:
            auth_issue = _explicit_auth_api_issue_code(payload)
            if auth_issue is not None:
                raise _AuthenticationSigningFailure(
                    f"Douyin SSR {label} requires authentication",
                    issue_code=auth_issue,
                )
            if _has_explicit_rate_limit_api_message(payload):
                raise _TransientSigningFailure(
                    f"Douyin SSR {label} was explicitly rate-limited",
                    category="api-rate-limit",
                )
            raise _TransientSigningFailure(
                f"Douyin SSR {label} returned a nonzero status",
                category="ssr-status-nonzero",
            )
    if wrapper.get("redirect") is not False:
        raise _IdentitySigningFailure("Douyin SSR item requested a redirect")
    if wrapper.get("isSpider") is not False:
        raise _TransientSigningFailure(
            "Douyin SSR returned a spider response",
            category="ssr-spider-response",
        )
    aweme = wrapper["aweme"]
    if aweme.get("isUnknownAweme") is not False:
        raise _IdentitySigningFailure("Douyin SSR returned an unknown aweme")
    detail = aweme.get("detail")
    if not isinstance(detail, dict):
        raise _SigningFailure("Douyin SSR returned no aweme detail")
    wrapper_aweme_id = str(wrapper.get("awemeId") or "").strip()
    actual_aweme_id = str(detail.get("awemeId") or "").strip()
    # A real SSR response can have a different groupId. It is not an alias for
    # the item ID and must never replace either explicit awemeId binding.
    if wrapper_aweme_id != aweme_id or actual_aweme_id != aweme_id:
        raise _IdentitySigningFailure("Douyin SSR returned a different aweme")
    author = detail.get("authorInfo")
    if not isinstance(author, dict):
        raise _SigningFailure("Douyin SSR item returned no author")
    actual_sec_uid = str(author.get("secUid") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", actual_sec_uid):
        raise _SigningFailure("Douyin SSR returned no valid author identity")
    if expected_sec_uid and actual_sec_uid != expected_sec_uid:
        raise _IdentitySigningFailure("Douyin SSR returned a different author")
    return _adapt_ssr_aweme_detail(detail)


def _extract_ssr_aweme_detail(
    fragments: Any,
    aweme_id: str,
    expected_sec_uid: str | None,
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for value in _parse_flight_json_records(fragments):
        if not isinstance(value, list) or len(value) < 4:
            continue
        wrapper = value[3]
        if not isinstance(wrapper, dict) or "awemeId" not in wrapper:
            continue
        if str(wrapper.get("awemeId") or "").strip() != aweme_id:
            continue
        candidates.append(
            _validated_ssr_wrapper_detail(wrapper, aweme_id, expected_sec_uid)
        )
    if not candidates:
        return None
    first = candidates[0]
    if any(value != first for value in candidates[1:]):
        raise _IdentitySigningFailure(
            "Douyin SSR returned conflicting copies of the requested aweme"
        )
    return first


def _read_ssr_aweme_detail_from_page(
    page: Any,
    aweme_id: str,
    expected_sec_uid: str | None,
) -> dict[str, Any] | None:
    try:
        snapshot = page.evaluate(
            _READ_PACE_FLIGHT_SCRIPT,
            {
                "maxEntries": _MAX_PACE_ENTRIES,
                "maxFragmentBytes": _MAX_PACE_FRAGMENT_BYTES,
                "maxTotalBytes": _MAX_PACE_TOTAL_BYTES,
            },
        )
    except Exception:
        # A normal-video redirect can briefly destroy the note execution context.
        # The completed response capture or existing signed-request path remains
        # authoritative, and page authentication/DNS state is checked separately.
        return None
    if snapshot is None or (
        isinstance(snapshot, dict) and snapshot.get("state") == "missing"
    ):
        return None
    if not isinstance(snapshot, dict):
        raise _SigningFailure("Douyin SSR returned an invalid Flight capture")
    if snapshot.get("state") != "done":
        raise _SigningFailure("Douyin SSR exceeded its bounded Flight capture limits")
    return _extract_ssr_aweme_detail(
        snapshot.get("fragments"),
        aweme_id,
        expected_sec_uid,
    )


class _PageDetailCapture:
    def __init__(self, aweme_id: str, expected_sec_uid: str | None) -> None:
        self.aweme_id = aweme_id
        self.expected_sec_uid = expected_sec_uid
        self.detail: dict[str, Any] | None = None
        self.authentication_failure: _AuthenticationSigningFailure | None = None
        self.identity_failure: _IdentitySigningFailure | None = None
        self.terminal_failure: _SigningFailure | None = None

    def handle_request_finished(self, request: Any) -> None:
        if (
            self.detail is not None
            or self.authentication_failure is not None
            or self.identity_failure is not None
            or self.terminal_failure is not None
            or not _is_target_page_detail_request(request, self.aweme_id)
        ):
            return
        try:
            response = request.response()
            if response is None:
                return
            response_url = str(getattr(response, "url", "") or "")
            if _is_dns_filter_block_url(response_url):
                raise _NetworkFilterSigningFailure(
                    "A local DNS or web filter redirected the Douyin detail response"
                )
            auth_issue = _explicit_auth_url_issue_code(response_url)
            if auth_issue is not None:
                raise _AuthenticationSigningFailure(
                    "Douyin detail response redirected to authentication",
                    issue_code=auth_issue,
                )
            if not _is_target_page_detail_url(response_url, self.aweme_id):
                raise _IdentitySigningFailure(
                    "Douyin detail response redirected outside its bound endpoint"
                )
            body = response.body()
            if not isinstance(body, (bytes, bytearray)):
                raise _SigningFailure(
                    "Douyin detail response returned no complete body"
                )
            if len(body) > _MAX_PAGE_DETAIL_BODY_BYTES:
                raise _SigningFailure(
                    "Douyin detail response body was unexpectedly large"
                )
            try:
                body_text = bytes(body).decode("utf-8")
                payload = json.loads(body_text)
            except (UnicodeDecodeError, json.JSONDecodeError):
                body_text = bytes(body).decode("utf-8", errors="replace")
                _raise_if_dns_filter_block_page(body_text)
                html_auth_issue = _explicit_auth_html_issue_code(body_text)
                if html_auth_issue is not None:
                    raise _AuthenticationSigningFailure(
                        "Douyin detail response displayed authentication",
                        issue_code=html_auth_issue,
                    )
                return
            detail = _validate_detail_response(
                {
                    "httpStatus": getattr(response, "status", None),
                    "payload": payload,
                },
                self.aweme_id,
                self.expected_sec_uid,
            )
        except _AuthenticationSigningFailure as exc:
            self.authentication_failure = exc
            return
        except _IdentitySigningFailure as exc:
            self.identity_failure = exc
            return
        except _NetworkFilterSigningFailure as exc:
            self.terminal_failure = exc
            return
        except _SigningFailure as exc:
            if "unexpectedly large" in str(exc):
                self.terminal_failure = exc
            return
        except Exception:
            # Page startup can issue incomplete detail requests before the complete
            # item response. Only a fully validated response is authoritative; all
            # other candidates safely fall back to the synthetic signed request.
            return
        self.detail = detail


def _wait_for_page_detail_capture(
    page: Any,
    capture: _PageDetailCapture,
    timeout_ms: int,
    should_cancel: CancelCallback | None,
    *,
    budget: _NoProgressBudget,
) -> None:
    effective_timeout_ms = budget.clamp_timeout_ms(max(1, timeout_ms))
    deadline = time.monotonic() + effective_timeout_ms / 1_000
    while (
        capture.detail is None
        and capture.authentication_failure is None
        and capture.identity_failure is None
        and capture.terminal_failure is None
        and time.monotonic() < deadline
    ):
        _raise_if_cancelled(should_cancel)
        budget.remaining_seconds()
        interval = min(
            _POLL_INTERVAL_MS,
            max(1, int((deadline - time.monotonic()) * 1_000)),
        )
        page.wait_for_timeout(min(interval, budget.clamp_timeout_ms(interval)))


def _capture_detail_from_item_page(
    page: Any,
    verification_url: str,
    aweme_id: str,
    expected_sec_uid: str | None,
    navigation_timeout_ms: int,
    settle_timeout_ms: int,
    should_cancel: CancelCallback | None,
    *,
    budget: _NoProgressBudget,
    status_callback: StatusCallback | None,
) -> dict[str, Any] | None:
    del verification_url
    capture = _PageDetailCapture(aweme_id, expected_sec_uid)
    handler_registered = False
    navigation_url = f"https://www.douyin.com/note/{aweme_id}"

    def validate_page_state() -> str:
        final_url = str(getattr(page, "url", "") or "")
        if _is_dns_filter_block_url(final_url):
            raise _NetworkFilterSigningFailure(
                "A local DNS or web filter redirected the Douyin item page"
            )
        redirect_issue = _explicit_auth_url_issue_code(final_url)
        if redirect_issue is not None:
            raise _AuthenticationSigningFailure(
                "Douyin item page redirected to an explicit authentication page",
                issue_code=redirect_issue,
            )
        if _douyin_target(final_url) != ("video", aweme_id):
            # Off-origin and unrelated pages are never allowed to manufacture an
            # authentication prompt from arbitrary body text. Trusted explicit
            # auth URLs were handled above; inline auth text is authoritative only
            # on the exact bound item page.
            return final_url
        try:
            source_html = page.content()
        except Exception:
            source_html = ""
        if source_html:
            _raise_if_dns_filter_block_page(source_html)
            html_auth_issue = _explicit_auth_html_issue_code(source_html)
            if html_auth_issue is not None:
                raise _AuthenticationSigningFailure(
                    "Douyin item page displayed an explicit authentication request",
                    issue_code=html_auth_issue,
                )
        return final_url

    def captured_or_ssr_detail(final_url: str) -> dict[str, Any] | None:
        if capture.authentication_failure is not None:
            raise capture.authentication_failure
        if capture.identity_failure is not None:
            raise capture.identity_failure
        if capture.terminal_failure is not None:
            raise capture.terminal_failure
        if _douyin_target(final_url) != ("video", aweme_id):
            return None
        if capture.detail is not None:
            return capture.detail
        try:
            return _read_ssr_aweme_detail_from_page(
                page,
                aweme_id,
                expected_sec_uid,
            )
        except (
            _AuthenticationSigningFailure,
            _IdentitySigningFailure,
            _NetworkFilterSigningFailure,
            _TransientSigningFailure,
        ):
            raise
        except _SigningFailure:
            # SSR is an optional exact-item candidate. A bounded parser miss or a
            # future Flight layout must not prevent the existing exact signed API
            # from producing independently verified metadata.
            return None

    try:
        page.on("requestfinished", capture.handle_request_finished)
        handler_registered = True
        _emit_status(status_callback, "Opening the original Douyin item page")
        try:
            page.goto(
                navigation_url,
                wait_until="domcontentloaded",
                timeout=budget.clamp_timeout_ms(navigation_timeout_ms),
            )
        except (DownloadCancelledError, _SigningNoProgressTimeout):
            raise
        except Exception:
            # A committed item page can emit the complete detail response before
            # Playwright reports a later navigation timeout. The final page binding
            # and the validated response remain authoritative in that case.
            pass

        _raise_if_cancelled(should_cancel)
        budget.remaining_seconds()
        final_url = validate_page_state()
        detail = captured_or_ssr_detail(final_url)
        if detail is not None:
            return detail

        if settle_timeout_ms > 0:
            _wait_for_page_detail_capture(
                page,
                capture,
                settle_timeout_ms,
                should_cancel,
                budget=budget,
            )
        _raise_if_cancelled(should_cancel)
        budget.remaining_seconds()
        final_url = validate_page_state()
        return captured_or_ssr_detail(final_url)
    finally:
        if handler_registered:
            remover = getattr(page, "remove_listener", None)
            if callable(remover):
                with contextlib.suppress(Exception):
                    remover("requestfinished", capture.handle_request_finished)


def _run_with_signing_page(
    verification_url: str,
    *,
    cookie_profile: str | None,
    should_cancel: CancelCallback | None,
    navigation_timeout_ms: int,
    signer_timeout_ms: int,
    signer_settle_ms: int,
    budget: _NoProgressBudget,
    status_callback: StatusCallback | None,
    operation: Callable[[Any], Any],
    page_detail_aweme_id: str | None = None,
    page_detail_expected_sec_uid: str | None = None,
) -> Any:
    browser: Any | None = None
    context: Any | None = None
    page: Any | None = None
    try:
        _raise_if_cancelled(should_cancel)
        budget.remaining_seconds()
        _emit_status(status_callback, "Loading Douyin Chrome cookies")
        try:
            cookie_jar = _load_chrome_cookie_jar(cookie_profile)
        except (DownloadCancelledError, _SigningFailure):
            raise
        except Exception as exc:
            raise _CookieAccessSigningFailure(
                "Chrome cookies could not be read"
            ) from exc
        budget.remaining_seconds()
        browser_cookies = _cookie_jar_to_playwright(cookie_jar)
        _raise_if_cancelled(should_cancel)
        budget.remaining_seconds()
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            try:
                budget.remaining_seconds()
                _emit_status(status_callback, "Starting the Douyin signing browser")
                browser = playwright.chromium.launch(
                    channel="chrome",
                    headless=True,
                    timeout=budget.clamp_timeout_ms(navigation_timeout_ms),
                )
                budget.remaining_seconds()
                context = browser.new_context(
                    locale="zh-CN",
                    viewport={"width": 1440, "height": 1100},
                    user_agent=chrome_user_agent(browser.version),
                )
                context.add_cookies(browser_cookies)
                _raise_if_cancelled(should_cancel)
                budget.remaining_seconds()

                page = context.new_page()

                if page_detail_aweme_id is not None:
                    captured_detail = _capture_detail_from_item_page(
                        page,
                        verification_url,
                        page_detail_aweme_id,
                        page_detail_expected_sec_uid,
                        navigation_timeout_ms,
                        signer_settle_ms,
                        should_cancel,
                        budget=budget,
                        status_callback=status_callback,
                    )
                    if captured_detail is not None:
                        return captured_detail

                user_agent = chrome_user_agent(browser.version)
                glue_tags, cookie_jar_updated = _extract_glue_with_context_fallback(
                    context,
                    verification_url,
                    cookie_jar,
                    user_agent,
                    navigation_timeout_ms,
                    should_cancel,
                    budget=budget,
                    status_callback=status_callback,
                )
                if cookie_jar_updated:
                    context.add_cookies(_cookie_jar_to_playwright(cookie_jar))
                signing_document = _build_signing_document(glue_tags)
                _raise_if_cancelled(should_cancel)
                budget.remaining_seconds()

                def serve_signing_page(route: Any) -> None:
                    route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body=signing_document,
                    )

                page.route(_SIGNING_PAGE_URL, serve_signing_page)
                _emit_status(status_callback, "Starting the Douyin signing session")
                page.goto(
                    _SIGNING_PAGE_URL,
                    wait_until="commit",
                    timeout=budget.clamp_timeout_ms(navigation_timeout_ms),
                )
                budget.remaining_seconds()
                _wait_for_signer(
                    page,
                    signer_timeout_ms,
                    should_cancel,
                    budget=budget,
                )
                _wait_with_cancel(
                    page,
                    signer_settle_ms,
                    should_cancel,
                    budget=budget,
                )
                _raise_if_cancelled(should_cancel)
                budget.remaining_seconds()
                return operation(page)
            finally:
                _close_resources(page, context, browser)
                page = context = browser = None
    finally:
        _close_resources(page, context, browser)


_PUBLIC_SIGNING_DIAGNOSTIC_CODES = frozenset(
    {
        "ssr-redirect",
        "ssr-unknown-item",
        "ssr-item-mismatch",
        "ssr-author-mismatch",
        "ssr-conflicting-items",
        "detail-item-mismatch",
        "detail-author-mismatch",
        "detail-response-redirect",
        "detail-metadata-incomplete",
        "ssr-metadata-incomplete",
        "signer-html-invalid",
        "signer-script-invalid",
        "signing-validation-failed",
        "signing-runtime-error",
    }
)


def public_signing_diagnostic_code(error: Exception) -> str:
    """Carry only structured, fixed codes across exception boundaries."""
    code = getattr(error, "diagnostic_code", None)
    if isinstance(code, str) and code in _PUBLIC_SIGNING_DIAGNOSTIC_CODES:
        return code
    return "signing-validation-failed"


def _signing_diagnostic_code(cause: Exception) -> str:
    """Expose fixed local reason codes, never exception text or response data."""
    if not isinstance(cause, _SigningFailure):
        return "signing-runtime-error"
    reasons = {
        "ssr-redirect": ("Douyin SSR item requested a redirect",),
        "ssr-unknown-item": ("Douyin SSR returned an unknown aweme",),
        "ssr-item-mismatch": ("Douyin SSR returned a different aweme",),
        "ssr-author-mismatch": ("Douyin SSR returned a different author",),
        "ssr-conflicting-items": (
            "Douyin SSR returned conflicting copies of the requested aweme",
        ),
        "detail-item-mismatch": ("Douyin detail API returned a different aweme",),
        "detail-author-mismatch": ("Douyin detail API returned a different author",),
        "detail-response-redirect": (
            "Douyin detail response redirected outside its bound endpoint",
        ),
        "detail-metadata-incomplete": (
            "Douyin detail API returned no aweme detail",
            "Douyin detail API returned no author",
            "Douyin detail API returned no author identity",
            "Douyin detail response returned no complete body",
        ),
        "ssr-metadata-incomplete": (
            "Douyin SSR returned no page data",
            "Douyin SSR returned no item data",
            "Douyin SSR returned no aweme detail",
            "Douyin SSR item returned no author",
            "Douyin SSR returned no valid author identity",
        ),
        "signer-html-invalid": (
            "Douyin HTML redirected outside the trusted origin",
            "Douyin HTML returned an invalid HTTP status",
            "Douyin HTML returned an invalid browser response",
            "Douyin HTML returned an invalid content length",
            "Douyin HTML response could not be decoded",
            "Douyin HTML response was unexpectedly large",
            "Douyin SecSDK HTML could not be parsed",
        ),
        "signer-script-invalid": (
            "Douyin returned an untrusted SecSDK script URL",
            "Douyin SecSDK marker is missing",
            "Douyin returned nested SecSDK script tags",
            "Douyin returned an incomplete SecSDK script tag",
            "Douyin returned too many SecSDK glue tags",
            "Douyin SecSDK glue was unexpectedly large",
        ),
    }
    message = str(cause)
    for code, messages in reasons.items():
        if message in messages:
            return code
    return "signing-validation-failed"


def _raise_signing_error(
    verification_url: str,
    cause: Exception,
) -> None:
    if isinstance(cause, _NetworkFilterSigningFailure):
        raise TemporaryAccessError(
            "A local DNS or web filter blocked Douyin before the site loaded. "
            "Allow Douyin in the local filter, disable DNS filtering, or switch "
            "networks, then retry the original link. Chrome verification is not "
            "required.",
            issue_code=SiteIssueCode.NETWORK_ERROR,
        ) from cause
    if isinstance(cause, _SigningNoProgressTimeout):
        code = _transient_site_issue_code(cause.category)
        raise TemporaryAccessError(
            "Douyin signed discovery stopped after 120 seconds without verified "
            "progress. Retry after a short wait; Chrome verification is not "
            f"required. Reason category: {cause.category}.",
            issue_code=code,
        ) from cause
    if isinstance(cause, _TransientSigningFailure):
        code = _transient_site_issue_code(cause.category)
        message = {
            SiteIssueCode.RATE_LIMITED: (
                "Douyin explicitly rate-limited a signed request after automatic "
                "retries. Wait a minute or two and retry; no lower-quality media "
                "was downloaded."
            ),
            SiteIssueCode.REQUEST_REJECTED: (
                "Douyin temporarily rejected a signed request after automatic "
                "retries, without displaying a CAPTCHA or login page. Wait briefly "
                "and retry."
            ),
            SiteIssueCode.SITE_UNAVAILABLE: (
                "Douyin returned a temporary server error after automatic retries. "
                "The queue was paused; wait briefly and retry."
            ),
            SiteIssueCode.NETWORK_ERROR: (
                "The Douyin signed request encountered a network or timeout error "
                "after automatic retries. Check the connection and retry."
            ),
            SiteIssueCode.SITE_RESPONSE_CHANGED: (
                "Douyin returned incomplete or changed signed response data after "
                "automatic retries. The response could not be verified, so the task "
                "was paused without downloading a fallback."
            ),
        }[code]
        raise TemporaryAccessError(
            f"{message} Reason category: {cause.category}.",
            issue_code=code,
        ) from cause
    if isinstance(cause, _CookieAccessSigningFailure):
        raise TemporaryAccessError(
            "Chrome cookies could not be read. Fully quit Chrome and retry, approve "
            "any system cookie-access prompt, or disable Chrome Cookie in settings "
            "to continue explicitly without login and create a new task. Opening a "
            "verification page is not required.",
            issue_code=SiteIssueCode.COOKIE_UNAVAILABLE,
        ) from cause
    if isinstance(cause, _AuthenticationSigningFailure):
        if cause.issue_code == SiteIssueCode.LOGIN_REQUIRED:
            if "no current douyin chrome cookies" in str(cause).lower():
                login_message = (
                    "No current Douyin session cookies were found in the selected "
                    "Chrome profile. Open the original task URL in that profile to "
                    "establish a site session; sign in only if Douyin asks, then retry."
                )
            else:
                login_message = (
                    "Douyin explicitly requested a current login session. Sign in to "
                    "Douyin in Chrome, return to the original link, then retry."
                )
            raise AuthenticationRequiredError(
                login_message,
                verification_url=verification_url,
                issue_code=SiteIssueCode.LOGIN_REQUIRED,
            ) from cause
        raise AuthenticationRequiredError(
            "Douyin displayed an explicit CAPTCHA or verification page. Open the "
            "provided URL in Chrome, finish the visible verification, then retry.",
            verification_url=verification_url,
            issue_code=SiteIssueCode.VERIFICATION_REQUIRED,
        ) from cause
    if isinstance(cause, _SigningFailure):
        diagnostic_code = _signing_diagnostic_code(cause)
        raise DiscoveryError(
            "Douyin signed data failed identity or integrity validation. Retry the "
            "original link; Chrome verification is not required unless Douyin "
            "explicitly shows a CAPTCHA or login page. "
            f"Diagnostic code: {diagnostic_code}.",
            issue_code=SiteIssueCode.SITE_RESPONSE_CHANGED,
            diagnostic_code=diagnostic_code,
        ) from cause
    message = str(cause).lower()
    if isinstance(cause, ImportError) or any(
        marker in message
        for marker in (
            "no module named 'playwright'",
            "executable doesn't exist",
            "executable does not exist",
            "browser was not found",
        )
    ):
        raise DiscoveryError(
            "The Playwright browser runtime required for Douyin signing is not "
            "installed. Run the project setup command and restart it.",
            issue_code=SiteIssueCode.LOCAL_CONFIGURATION,
        ) from cause
    if any(marker in message for marker in ("timeout", "timed out", "network")):
        raise TemporaryAccessError(
            "Douyin signed discovery temporarily failed before a verified response "
            "was available. Retry after a short wait; Chrome verification is not "
            "required. Reason category: network-timeout.",
            issue_code=SiteIssueCode.NETWORK_ERROR,
        ) from cause
    diagnostic_code = _signing_diagnostic_code(cause)
    raise DiscoveryError(
        "Douyin signed discovery failed before a verified response was available. "
        "Retry the original link; Chrome verification is not required unless Douyin "
        "explicitly shows a CAPTCHA or login page. "
        f"Diagnostic code: {diagnostic_code}.",
        issue_code=SiteIssueCode.SITE_RESPONSE_CHANGED,
        diagnostic_code=diagnostic_code,
    ) from cause


def fetch_signed_aweme_detail(
    aweme_id: str,
    *,
    verification_url: str,
    expected_sec_uid: str | None = None,
    cookie_profile: str | None = None,
    should_cancel: CancelCallback | None = None,
    navigation_timeout_ms: int = 45_000,
    signer_timeout_ms: int = 20_000,
    signer_settle_ms: int = 8_000,
    request_timeout_ms: int = 45_000,
    status_callback: StatusCallback | None = None,
    progress_budget: _NoProgressBudget | None = None,
) -> dict[str, Any]:
    """Fetch one Douyin aweme detail through the site's current official SecSDK."""

    budget = progress_budget or new_signed_discovery_budget()
    try:
        if not isinstance(aweme_id, str) or not aweme_id.isdigit():
            raise _SigningFailure("The Douyin aweme identifier is invalid")
        if not isinstance(verification_url, str) or not _is_douyin_url(
            verification_url
        ):
            raise _SigningFailure("The Douyin verification URL is invalid")
        if expected_sec_uid is not None:
            if not isinstance(expected_sec_uid, str) or not expected_sec_uid.strip():
                raise _SigningFailure("The expected Douyin author identity is invalid")
            expected_sec_uid = expected_sec_uid.strip()
        verification_kind, verification_id = _douyin_target(verification_url) or (
            "",
            "",
        )
        if verification_kind == "video" and verification_id != aweme_id:
            raise _SigningFailure(
                "The Douyin verification URL identifies a different aweme"
            )
        if verification_kind == "user" and expected_sec_uid != verification_id:
            raise _SigningFailure(
                "The Douyin verification URL identifies a different author"
            )

        def fetch_detail(page: Any) -> dict[str, Any]:
            for attempt in range(_DETAIL_REQUEST_ATTEMPTS):
                try:
                    if not attempt:
                        _emit_status(status_callback, "Fetching Douyin signed detail")
                    effective_timeout_ms = budget.clamp_timeout_ms(request_timeout_ms)
                    _start_signed_fetch(
                        page,
                        _DETAIL_API_PATH,
                        {"aweme_id": aweme_id},
                        effective_timeout_ms,
                    )
                    response = _wait_for_signed_response(
                        page,
                        effective_timeout_ms,
                        should_cancel,
                        budget=budget,
                    )
                    detail = _validate_detail_response(
                        response,
                        aweme_id,
                        expected_sec_uid,
                    )
                except _SigningNoProgressTimeout:
                    raise
                except _TransientSigningFailure as exc:
                    budget.note_failure(exc)
                    if attempt + 1 >= _DETAIL_REQUEST_ATTEMPTS:
                        raise
                    _emit_status(
                        status_callback,
                        "Retrying Douyin signed detail request "
                        f"{attempt + 2}/{_DETAIL_REQUEST_ATTEMPTS} "
                        f"(reason: {_transient_reason_category(exc)})",
                    )
                    _wait_with_cancel(
                        page,
                        _DETAIL_RETRY_BASE_MS * (2**attempt),
                        should_cancel,
                        budget=budget,
                    )
                    continue
                _raise_if_cancelled(should_cancel)
                return detail
            raise _SigningFailure("Douyin detail request attempts were exhausted")

        with _serialized_signed_fetch(
            should_cancel,
            budget=budget,
            status_callback=status_callback,
        ):
            for session_attempt in range(_DETAIL_SIGNING_SESSION_ATTEMPTS):
                try:
                    _emit_status(
                        status_callback,
                        "Preparing Douyin signed detail session "
                        f"{session_attempt + 1}/{_DETAIL_SIGNING_SESSION_ATTEMPTS}",
                    )
                    return _run_with_signing_page(
                        verification_url,
                        cookie_profile=cookie_profile,
                        should_cancel=should_cancel,
                        navigation_timeout_ms=navigation_timeout_ms,
                        signer_timeout_ms=signer_timeout_ms,
                        signer_settle_ms=signer_settle_ms,
                        budget=budget,
                        status_callback=status_callback,
                        operation=fetch_detail,
                        page_detail_aweme_id=(
                            aweme_id if verification_kind == "video" else None
                        ),
                        page_detail_expected_sec_uid=expected_sec_uid,
                    )
                except _SigningNoProgressTimeout:
                    raise
                except _TransientSigningFailure as exc:
                    budget.note_failure(exc)
                    if session_attempt + 1 >= _DETAIL_SIGNING_SESSION_ATTEMPTS:
                        raise
                    _emit_status(
                        status_callback,
                        "Retrying Douyin signed detail with a fresh signing session "
                        f"(reason: {_transient_reason_category(exc)})",
                    )
                    _wait_without_page_with_cancel(
                        _DETAIL_SIGNING_RETRY_BASE_MS * (2**session_attempt),
                        should_cancel,
                        budget=budget,
                    )
        raise _SigningFailure("Douyin detail signing attempts were exhausted")
    except DownloadCancelledError:
        raise
    except Exception as exc:
        _raise_signing_error(verification_url, exc)


def _profile_request_params(profile_id: str, cursor: str) -> dict[str, str]:
    return {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "sec_user_id": profile_id,
        "max_cursor": cursor,
        "count": str(_PROFILE_PAGE_SIZE),
        "publish_video_strategy_type": "2",
        "locate_query": "false",
        "show_live_replay_strategy": "1",
        "need_time_list": "1",
        "time_list_query": "0",
        "whale_cut_token": "",
        "cut_version": "1",
        "from_user_page": "1",
        "update_version_code": "170400",
        "pc_client_type": "1",
    }


def fetch_signed_profile_awemes(
    profile_url: str,
    profile_id: str,
    *,
    target_aweme_id: str | None = None,
    cookie_profile: str | None = None,
    should_cancel: CancelCallback | None = None,
    max_pages: int = 300,
    max_awemes: int = 5_000,
    navigation_timeout_ms: int = 45_000,
    signer_timeout_ms: int = 20_000,
    signer_settle_ms: int = 8_000,
    request_timeout_ms: int = 45_000,
    status_callback: StatusCallback | None = None,
    progress_budget: _NoProgressBudget | None = None,
) -> list[dict[str, Any]]:
    """Fetch verified raw awemes from one Douyin profile with SecSDK."""

    budget = progress_budget or new_signed_discovery_budget()
    try:
        if not isinstance(profile_url, str) or not _is_douyin_url(profile_url):
            raise _SigningFailure("The Douyin profile URL is invalid")
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise _SigningFailure("The Douyin profile identity is invalid")
        if type(max_pages) is not int or max_pages <= 0:
            raise _SigningFailure("The Douyin page limit is invalid")
        if type(max_awemes) is not int or max_awemes <= 0:
            raise _SigningFailure("The Douyin aweme limit is invalid")
        if target_aweme_id is not None and (
            not isinstance(target_aweme_id, str) or not target_aweme_id.isdigit()
        ):
            raise _SigningFailure("The target Douyin aweme identifier is invalid")
        profile_id = profile_id.strip()
        if _douyin_target(profile_url) != ("user", profile_id):
            raise _SigningFailure(
                "The Douyin profile URL identifies a different author"
            )

        collected: dict[str, dict[str, Any]] = {}
        cursor = "0"
        verified_page_cursors: set[str] = set()

        def fetch_pages(page: Any) -> list[dict[str, Any]]:
            nonlocal cursor
            while len(verified_page_cursors) < max_pages:
                page_number = len(verified_page_cursors) + 1
                _raise_if_cancelled(should_cancel)
                budget.remaining_seconds()
                if cursor in verified_page_cursors:
                    raise _SigningFailure("Douyin profile pagination repeated a cursor")
                for attempt in range(_PROFILE_REQUEST_ATTEMPTS):
                    try:
                        if not attempt:
                            _emit_status(
                                status_callback,
                                "Fetching Douyin signed profile page "
                                f"{page_number}/{max_pages}",
                            )
                        params = _profile_request_params(profile_id, cursor)
                        if attempt % 2:
                            params["count"] = str(_PROFILE_FALLBACK_PAGE_SIZE)
                        effective_timeout_ms = budget.clamp_timeout_ms(
                            request_timeout_ms
                        )
                        _start_signed_fetch(
                            page,
                            _PROFILE_API_PATH,
                            params,
                            effective_timeout_ms,
                        )
                        response = _wait_for_signed_response(
                            page,
                            effective_timeout_ms,
                            should_cancel,
                            budget=budget,
                        )
                        awemes, has_more, next_cursor = _validate_profile_response(
                            response,
                            profile_id,
                        )
                        break
                    except _SigningNoProgressTimeout:
                        raise
                    except _TransientSigningFailure as exc:
                        budget.note_failure(exc)
                        if attempt + 1 >= _PROFILE_REQUEST_ATTEMPTS:
                            raise
                        _emit_status(
                            status_callback,
                            "Retrying Douyin signed profile page "
                            f"{page_number}/{max_pages} request "
                            f"{attempt + 2}/{_PROFILE_REQUEST_ATTEMPTS} "
                            f"(reason: {_transient_reason_category(exc)})",
                        )
                        _wait_with_cancel(
                            page,
                            _PROFILE_RETRY_BASE_MS * (2**attempt),
                            should_cancel,
                            budget=budget,
                        )
                if has_more and (
                    next_cursor == cursor or str(next_cursor) in verified_page_cursors
                ):
                    raise _SigningFailure("Douyin profile pagination did not advance")
                collected_before_page = len(collected)
                for aweme in awemes:
                    aweme_id = str(aweme["aweme_id"])
                    if aweme_id in collected:
                        continue
                    if len(collected) >= max_awemes:
                        raise _SigningFailure(
                            "Douyin profile reached the aweme safety limit"
                        )
                    collected[aweme_id] = aweme
                verified_page_cursors.add(cursor)
                if len(collected) > collected_before_page:
                    budget.refresh()
                _emit_status(
                    status_callback,
                    "Verified Douyin signed profile page "
                    f"{page_number} ({len(awemes)} items)",
                )
                if target_aweme_id and target_aweme_id in collected:
                    _raise_if_cancelled(should_cancel)
                    return [collected[target_aweme_id]]
                if not has_more:
                    _raise_if_cancelled(should_cancel)
                    return list(collected.values())
                cursor = str(next_cursor)
            raise _SigningFailure("Douyin profile reached the page safety limit")

        with _serialized_signed_fetch(
            should_cancel,
            budget=budget,
            status_callback=status_callback,
        ):
            for session_attempt in range(_PROFILE_SIGNING_SESSION_ATTEMPTS):
                try:
                    _emit_status(
                        status_callback,
                        "Preparing Douyin signed profile session "
                        f"{session_attempt + 1}/{_PROFILE_SIGNING_SESSION_ATTEMPTS}",
                    )
                    if session_attempt and verified_page_cursors:
                        _emit_status(
                            status_callback,
                            "Resuming Douyin signed profile at page "
                            f"{len(verified_page_cursors) + 1}/{max_pages} "
                            f"({len(collected)} verified items)",
                        )
                    return _run_with_signing_page(
                        profile_url,
                        cookie_profile=cookie_profile,
                        should_cancel=should_cancel,
                        navigation_timeout_ms=navigation_timeout_ms,
                        signer_timeout_ms=signer_timeout_ms,
                        signer_settle_ms=signer_settle_ms,
                        budget=budget,
                        status_callback=status_callback,
                        operation=fetch_pages,
                    )
                except _SigningNoProgressTimeout:
                    raise
                except _TransientSigningFailure as exc:
                    budget.note_failure(exc)
                    if session_attempt + 1 >= _PROFILE_SIGNING_SESSION_ATTEMPTS:
                        raise
                    _emit_status(
                        status_callback,
                        "Retrying Douyin signed profile with a fresh signing session "
                        f"(reason: {_transient_reason_category(exc)})",
                    )
                    _wait_without_page_with_cancel(
                        _PROFILE_SIGNING_RETRY_BASE_MS * (2**session_attempt),
                        should_cancel,
                        budget=budget,
                    )
        raise _SigningFailure("Douyin profile signing attempts were exhausted")
    except DownloadCancelledError:
        raise
    except Exception as exc:
        _raise_signing_error(profile_url, exc)
