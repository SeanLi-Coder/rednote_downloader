from __future__ import annotations

import contextlib
import html
import re
import threading
import time
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from typing import Any, Callable
from urllib.error import HTTPError as UrllibHTTPError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

from yt_dlp.cookies import extract_cookies_from_browser

from .browser import chrome_user_agent
from .errors import (
    AuthenticationRequiredError,
    DiscoveryError,
    DownloadCancelledError,
    TemporaryAccessError,
)


CancelCallback = Callable[[], bool]

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
_MAX_GLUE_TAGS = 16
_MAX_GLUE_BYTES = 1_000_000
_MAX_SOURCE_HTML_BYTES = 5_000_000
_SIGNED_FETCH_LOCK = threading.Lock()
_TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_TRUSTED_DOUYIN_AUTH_HOSTS = frozenset(
    {"douyin.com", "www.douyin.com", "sso.douyin.com"}
)
_EXPLICIT_AUTH_PATH_MARKERS = ("/captcha", "/login", "/passport/", "/verify")
_EXPLICIT_AUTH_TEXT_MARKERS = (
    "captcha",
    "verify you are human",
    "complete the verification",
    "security verification",
    "验证码",
    "安全验证",
    "请完成下列验证",
    "请完成验证",
    "登录后继续",
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
)

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

_START_SIGNED_FETCH_SCRIPT = """
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
      let payload = null;
      try {
        payload = JSON.parse(await response.text());
      } catch (_) {
        window[stateKey] = {
          state: "error",
          reason: "invalid_json",
          httpStatus: response.status,
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


class _TransientSigningFailure(_SigningFailure):
    pass


class _AuthenticationSigningFailure(_SigningFailure):
    pass


class _CookieAccessSigningFailure(_SigningFailure):
    pass


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


def _has_explicit_auth_html(source_html: str) -> bool:
    parser = _VisibleTextParser()
    try:
        parser.feed(source_html)
        parser.close()
    except Exception:
        return False
    visible_text = re.sub(r"\s+", " ", " ".join(parser.values)).lower()
    return any(marker in visible_text for marker in _EXPLICIT_AUTH_TEXT_MARKERS)


def _has_explicit_auth_api_message(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    message = str(
        payload.get("status_msg")
        or payload.get("status_message")
        or payload.get("message")
        or ""
    ).lower()
    return any(marker in message for marker in _EXPLICIT_AUTH_API_MARKERS)


def _extract_sdk_glue_tags(source_html: str) -> tuple[str, ...]:
    if not isinstance(source_html, str) or not source_html.strip():
        raise _TransientSigningFailure("Douyin returned an empty HTML response")
    if len(source_html.encode("utf-8")) > _MAX_SOURCE_HTML_BYTES:
        raise _SigningFailure("Douyin HTML response was unexpectedly large")
    parser = _SdkGlueParser()
    try:
        parser.feed(source_html)
        parser.close()
    except _SigningFailure:
        raise
    except Exception as exc:
        raise _SigningFailure("Douyin SecSDK HTML could not be parsed") from exc
    if not parser.tags:
        if _has_explicit_auth_html(source_html):
            raise _AuthenticationSigningFailure(
                "Douyin HTML displayed an explicit verification challenge"
            )
        raise _TransientSigningFailure(
            "Douyin SecSDK glue was not present in the HTML"
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
        raise _CookieAccessSigningFailure(
            "Chrome cookies could not be read"
        ) from exc


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


def _is_explicit_auth_url(value: str) -> bool:
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
            return False
        path = unquote(parsed.path).lower()
    except (TypeError, ValueError):
        return False
    return any(marker in path for marker in _EXPLICIT_AUTH_PATH_MARKERS)


def _fetch_source_html_with_urllib(
    verification_url: str,
    cookie_jar: CookieJar,
    user_agent: str,
    timeout_ms: int,
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
        with opener.open(
            request, timeout=max(timeout_ms / 1_000, 0.001)
        ) as response:
            status = getattr(response, "status", None)
            if _is_explicit_auth_url(response.geturl()):
                raise _AuthenticationSigningFailure(
                    "Douyin HTML redirected to an explicit verification page"
                )
            if not _is_allowed_douyin_origin(response.geturl()):
                raise _SigningFailure(
                    "Douyin HTML redirected outside the trusted origin"
                )
            if type(status) is int and status in _TRANSIENT_HTTP_STATUSES:
                raise _TransientSigningFailure(
                    "Douyin HTML request was temporarily limited"
                )
            if type(status) is int and status == 401:
                raise _AuthenticationSigningFailure(
                    "Douyin HTML request requires authentication"
                )
            if type(status) is int and status == 403:
                raise _TransientSigningFailure(
                    "Douyin HTML request was temporarily rejected"
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
            body = response.read(_MAX_SOURCE_HTML_BYTES + 1)
            if len(body) > _MAX_SOURCE_HTML_BYTES:
                raise _SigningFailure("Douyin HTML response was unexpectedly large")
            charset = response.headers.get_content_charset() or "utf-8"
            try:
                return body.decode(charset)
            except (LookupError, UnicodeDecodeError) as exc:
                raise _SigningFailure(
                    "Douyin HTML response could not be decoded"
                ) from exc
    except UrllibHTTPError as exc:
        try:
            if _is_explicit_auth_url(exc.geturl()):
                raise _AuthenticationSigningFailure(
                    "Douyin HTML redirected to an explicit verification page"
                ) from exc
            if not _is_allowed_douyin_origin(exc.geturl()):
                raise _SigningFailure(
                    "Douyin HTML redirected outside the trusted origin"
                ) from exc
            if exc.code in _TRANSIENT_HTTP_STATUSES:
                raise _TransientSigningFailure(
                    "Douyin HTML request was temporarily limited"
                ) from exc
            if exc.code == 401:
                raise _AuthenticationSigningFailure(
                    "Douyin HTML request requires authentication"
                ) from exc
            if exc.code == 403:
                try:
                    body = exc.read(_MAX_SOURCE_HTML_BYTES + 1)
                    charset = exc.headers.get_content_charset() or "utf-8"
                    source_html = body.decode(charset, errors="replace")
                except Exception:
                    source_html = ""
                if _has_explicit_auth_html(source_html):
                    raise _AuthenticationSigningFailure(
                        "Douyin HTML displayed an explicit verification challenge"
                    ) from exc
                raise _TransientSigningFailure(
                    "Douyin HTML request was temporarily rejected"
                ) from exc
            raise _SigningFailure(
                "Douyin HTML returned an invalid HTTP status"
            ) from exc
        finally:
            with contextlib.suppress(Exception):
                exc.close()


def _extract_glue_with_context_fallback(
    context: Any,
    verification_url: str,
    cookie_jar: CookieJar,
    user_agent: str,
    timeout_ms: int,
    should_cancel: CancelCallback | None,
) -> tuple[tuple[str, ...], bool]:
    try:
        source_html = _fetch_source_html_with_urllib(
            verification_url,
            cookie_jar,
            user_agent,
            timeout_ms,
        )
        return _extract_sdk_glue_tags(source_html), True
    except DownloadCancelledError:
        raise
    except _AuthenticationSigningFailure:
        raise
    except Exception:
        _raise_if_cancelled(should_cancel)
        try:
            response = context.request.get(
                verification_url,
                timeout=timeout_ms,
            )
        except Exception as exc:
            raise _TransientSigningFailure(
                "Douyin browser HTML request temporarily failed"
            ) from exc
        try:
            status = response.status
            if _is_explicit_auth_url(response.url):
                raise _AuthenticationSigningFailure(
                    "Douyin HTML redirected to an explicit verification page"
                )
            if not _is_allowed_douyin_origin(response.url):
                raise _SigningFailure(
                    "Douyin HTML redirected outside the trusted origin"
                )
            if type(status) is int and status in _TRANSIENT_HTTP_STATUSES:
                raise _TransientSigningFailure(
                    "Douyin browser HTML request was temporarily limited"
                )
            if type(status) is int and status == 401:
                raise _AuthenticationSigningFailure(
                    "Douyin browser HTML request requires authentication"
                )
            if type(status) is int and status == 403:
                source_html = response.text()
                if _has_explicit_auth_html(source_html):
                    raise _AuthenticationSigningFailure(
                        "Douyin HTML displayed an explicit verification challenge"
                    )
                raise _TransientSigningFailure(
                    "Douyin browser HTML request was temporarily rejected"
                )
            if (
                type(status) is not int
                or not 200 <= status < 300
            ):
                raise _SigningFailure(
                    "Douyin HTML returned an invalid browser response"
                )
            return _extract_sdk_glue_tags(response.text()), False
        finally:
            with contextlib.suppress(Exception):
                response.dispose()


def _raise_if_cancelled(should_cancel: CancelCallback | None) -> None:
    if should_cancel and should_cancel():
        raise DownloadCancelledError("Task cancelled")


@contextlib.contextmanager
def _serialized_signed_fetch(should_cancel: CancelCallback | None):
    while not _SIGNED_FETCH_LOCK.acquire(timeout=_POLL_INTERVAL_MS / 1_000):
        _raise_if_cancelled(should_cancel)
    try:
        _raise_if_cancelled(should_cancel)
        yield
    finally:
        _SIGNED_FETCH_LOCK.release()


def _wait_with_cancel(
    page: Any,
    duration_ms: int,
    should_cancel: CancelCallback | None,
) -> None:
    remaining = max(0, duration_ms)
    while remaining:
        _raise_if_cancelled(should_cancel)
        interval = min(_POLL_INTERVAL_MS, remaining)
        page.wait_for_timeout(interval)
        remaining -= interval
    _raise_if_cancelled(should_cancel)


def _wait_without_page_with_cancel(
    duration_ms: int,
    should_cancel: CancelCallback | None,
) -> None:
    deadline = time.monotonic() + max(duration_ms, 0) / 1_000
    while True:
        _raise_if_cancelled(should_cancel)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(_POLL_INTERVAL_MS / 1_000, remaining))


def _wait_for_signer(
    page: Any,
    timeout_ms: int,
    should_cancel: CancelCallback | None,
) -> None:
    deadline = time.monotonic() + timeout_ms / 1_000
    while time.monotonic() < deadline:
        _raise_if_cancelled(should_cancel)
        if page.evaluate("() => typeof window.useWebSecsdkApi === 'function'"):
            return
        page.wait_for_timeout(_POLL_INTERVAL_MS)
    raise _TransientSigningFailure("Douyin SecSDK did not become ready")


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
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_ms / 1_000
    while time.monotonic() < deadline:
        if should_cancel and should_cancel():
            with contextlib.suppress(Exception):
                page.evaluate(_ABORT_SIGNED_FETCH_SCRIPT)
            raise DownloadCancelledError("Task cancelled")
        result = page.evaluate(_READ_SIGNED_FETCH_SCRIPT)
        if not isinstance(result, dict):
            raise _SigningFailure("Douyin returned an invalid signed response")
        state = result.get("state")
        if state == "done":
            return result
        if state not in {"missing", "pending"}:
            http_status = result.get("httpStatus")
            if type(http_status) is int and http_status in _TRANSIENT_HTTP_STATUSES:
                raise _TransientSigningFailure(
                    "Douyin signed request returned a temporary HTTP status"
                )
            if result.get("reason") == "request_failed":
                raise _TransientSigningFailure(
                    "Douyin signed network request temporarily failed"
                )
            if type(http_status) is int and http_status == 401:
                raise _AuthenticationSigningFailure(
                    "Douyin signed request requires authentication"
                )
            if type(http_status) is int and http_status == 403:
                raise _TransientSigningFailure(
                    "Douyin signed request was temporarily rejected"
                )
            raise _TransientSigningFailure(
                "Douyin temporarily rejected the signed request"
            )
        page.wait_for_timeout(_POLL_INTERVAL_MS)
    with contextlib.suppress(Exception):
        page.evaluate(_ABORT_SIGNED_FETCH_SCRIPT)
    raise _TransientSigningFailure("Douyin signed request timed out")


def _validated_payload(
    response: dict[str, Any],
    request_name: str,
) -> dict[str, Any]:
    http_status = response.get("httpStatus")
    if type(http_status) is int and http_status in _TRANSIENT_HTTP_STATUSES:
        raise _TransientSigningFailure(
            f"Douyin {request_name} request was temporarily limited"
        )
    payload = response.get("payload")
    if type(http_status) is int and http_status == 401:
        raise _AuthenticationSigningFailure(
            f"Douyin {request_name} request requires authentication"
        )
    if type(http_status) is int and http_status == 403:
        if _has_explicit_auth_api_message(payload):
            raise _AuthenticationSigningFailure(
                f"Douyin {request_name} API requires authentication"
            )
        raise _TransientSigningFailure(
            f"Douyin {request_name} request was temporarily rejected"
        )
    if type(http_status) is not int or not 200 <= http_status < 300:
        raise _SigningFailure(
            f"Douyin {request_name} request returned an invalid HTTP status"
        )
    if not isinstance(payload, dict):
        raise _SigningFailure(f"Douyin {request_name} request returned no JSON object")
    status_code = payload.get("status_code")
    if type(status_code) is not int or status_code != 0:
        if _has_explicit_auth_api_message(payload):
            raise _AuthenticationSigningFailure(
                f"Douyin {request_name} API requires authentication"
            )
        raise _TransientSigningFailure(
            f"Douyin {request_name} API temporarily rejected the request"
        )
    return payload


def _validate_detail_response(
    response: dict[str, Any],
    aweme_id: str,
    expected_sec_uid: str | None,
) -> dict[str, Any]:
    payload = _validated_payload(response, "detail")
    detail = payload.get("aweme_detail")
    if not isinstance(detail, dict):
        raise _SigningFailure("Douyin detail API returned no aweme detail")
    actual_aweme_id = str(detail.get("aweme_id") or "").strip()
    if actual_aweme_id != aweme_id:
        raise _SigningFailure("Douyin detail API returned a different aweme")
    author = detail.get("author")
    if not isinstance(author, dict):
        raise _SigningFailure("Douyin detail API returned no author")
    actual_sec_uid = str(author.get("sec_uid") or "").strip()
    if not actual_sec_uid:
        raise _SigningFailure("Douyin detail API returned no author identity")
    if expected_sec_uid and actual_sec_uid != expected_sec_uid:
        raise _SigningFailure("Douyin detail API returned a different author")
    return detail


def _validate_profile_response(
    response: dict[str, Any],
    profile_id: str,
) -> tuple[list[dict[str, Any]], bool, str | None]:
    payload = _validated_payload(response, "profile")
    response_identity = str(
        payload.get("sec_uid") or payload.get("sec_user_id") or ""
    ).strip()
    if response_identity and response_identity != profile_id:
        raise _SigningFailure("Douyin profile API returned a different profile")

    raw_awemes = payload.get("aweme_list")
    if not isinstance(raw_awemes, list):
        raise _TransientSigningFailure("Douyin profile API returned no aweme list")
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
                "Douyin profile API returned incomplete aweme media"
            )
        awemes.append(aweme)

    has_more_value = payload.get("has_more")
    if type(has_more_value) is bool:
        has_more = has_more_value
    elif type(has_more_value) is int and has_more_value in {0, 1}:
        has_more = bool(has_more_value)
    else:
        raise _SigningFailure("Douyin profile API returned invalid pagination state")

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


def _run_with_signing_page(
    verification_url: str,
    *,
    cookie_profile: str | None,
    should_cancel: CancelCallback | None,
    navigation_timeout_ms: int,
    signer_timeout_ms: int,
    signer_settle_ms: int,
    operation: Callable[[Any], Any],
) -> Any:
    browser: Any | None = None
    context: Any | None = None
    page: Any | None = None
    try:
        _raise_if_cancelled(should_cancel)
        try:
            cookie_jar = _load_chrome_cookie_jar(cookie_profile)
        except (DownloadCancelledError, _SigningFailure):
            raise
        except Exception as exc:
            raise _CookieAccessSigningFailure(
                "Chrome cookies could not be read"
            ) from exc
        browser_cookies = _cookie_jar_to_playwright(cookie_jar)
        _raise_if_cancelled(should_cancel)
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(
                    channel="chrome",
                    headless=True,
                )
                context = browser.new_context(
                    locale="zh-CN",
                    viewport={"width": 1440, "height": 1100},
                    user_agent=chrome_user_agent(browser.version),
                )
                context.add_cookies(browser_cookies)
                _raise_if_cancelled(should_cancel)
                user_agent = chrome_user_agent(browser.version)
                glue_tags, cookie_jar_updated = _extract_glue_with_context_fallback(
                    context,
                    verification_url,
                    cookie_jar,
                    user_agent,
                    navigation_timeout_ms,
                    should_cancel,
                )
                if cookie_jar_updated:
                    context.add_cookies(_cookie_jar_to_playwright(cookie_jar))
                signing_document = _build_signing_document(glue_tags)
                _raise_if_cancelled(should_cancel)

                page = context.new_page()

                def serve_signing_page(route: Any) -> None:
                    route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body=signing_document,
                    )

                page.route(_SIGNING_PAGE_URL, serve_signing_page)
                page.goto(
                    _SIGNING_PAGE_URL,
                    wait_until="commit",
                    timeout=navigation_timeout_ms,
                )
                _wait_for_signer(page, signer_timeout_ms, should_cancel)
                _wait_with_cancel(page, signer_settle_ms, should_cancel)
                _raise_if_cancelled(should_cancel)
                return operation(page)
            finally:
                _close_resources(page, context, browser)
                page = context = browser = None
    finally:
        _close_resources(page, context, browser)


def _raise_signing_error(
    verification_url: str,
    cause: Exception,
) -> None:
    if isinstance(cause, _TransientSigningFailure):
        raise TemporaryAccessError(
            "Douyin temporarily limited a signed request after automatic retries. "
            "Wait a minute or two and retry; no lower-quality media was downloaded."
        ) from cause
    if isinstance(cause, _CookieAccessSigningFailure):
        raise TemporaryAccessError(
            "Chrome cookies could not be read. Fully quit Chrome and retry, approve "
            "any system cookie-access prompt, or disable Chrome Cookie in settings "
            "to continue explicitly without login and create a new task. Opening a "
            "verification page is not required."
        ) from cause
    if isinstance(cause, _AuthenticationSigningFailure):
        raise AuthenticationRequiredError(
            "Douyin requires current Chrome cookies or an explicit verification. "
            "Open the provided URL in Chrome, finish any CAPTCHA or login, then retry.",
            verification_url=verification_url,
        ) from cause
    if isinstance(cause, _SigningFailure):
        raise DiscoveryError(
            "Douyin signed data failed identity or integrity validation. Retry the "
            "original link; Chrome verification is not required unless Douyin "
            "explicitly shows a CAPTCHA or login page."
        ) from cause
    message = str(cause).lower()
    if any(marker in message for marker in ("timeout", "timed out", "network")):
        raise TemporaryAccessError(
            "Douyin signed discovery temporarily failed before a verified response "
            "was available. Retry after a short wait; Chrome verification is not "
            "required."
        ) from cause
    raise DiscoveryError(
        "Douyin signed discovery failed before a verified response was available. "
        "Retry the original link; Chrome verification is not required unless Douyin "
        "explicitly shows a CAPTCHA or login page."
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
) -> dict[str, Any]:
    """Fetch one Douyin aweme detail through the site's current official SecSDK."""

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
                    _start_signed_fetch(
                        page,
                        _DETAIL_API_PATH,
                        {"aweme_id": aweme_id},
                        request_timeout_ms,
                    )
                    response = _wait_for_signed_response(
                        page,
                        request_timeout_ms,
                        should_cancel,
                    )
                    detail = _validate_detail_response(
                        response,
                        aweme_id,
                        expected_sec_uid,
                    )
                except _TransientSigningFailure:
                    if attempt + 1 >= _DETAIL_REQUEST_ATTEMPTS:
                        raise
                    _wait_with_cancel(
                        page,
                        _DETAIL_RETRY_BASE_MS * (2**attempt),
                        should_cancel,
                    )
                    continue
                _raise_if_cancelled(should_cancel)
                return detail
            raise _SigningFailure("Douyin detail request attempts were exhausted")

        with _serialized_signed_fetch(should_cancel):
            for session_attempt in range(_DETAIL_SIGNING_SESSION_ATTEMPTS):
                try:
                    return _run_with_signing_page(
                        verification_url,
                        cookie_profile=cookie_profile,
                        should_cancel=should_cancel,
                        navigation_timeout_ms=navigation_timeout_ms,
                        signer_timeout_ms=signer_timeout_ms,
                        signer_settle_ms=signer_settle_ms,
                        operation=fetch_detail,
                    )
                except _TransientSigningFailure:
                    if session_attempt + 1 >= _DETAIL_SIGNING_SESSION_ATTEMPTS:
                        raise
                    _wait_without_page_with_cancel(
                        _DETAIL_SIGNING_RETRY_BASE_MS * (2**session_attempt),
                        should_cancel,
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
) -> list[dict[str, Any]]:
    """Fetch verified raw awemes from one Douyin profile with SecSDK."""

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

        def fetch_pages(page: Any) -> list[dict[str, Any]]:
            collected: dict[str, dict[str, Any]] = {}
            cursor = "0"
            used_cursors: set[str] = set()
            for _ in range(max_pages):
                _raise_if_cancelled(should_cancel)
                if cursor in used_cursors:
                    raise _SigningFailure("Douyin profile pagination repeated a cursor")
                used_cursors.add(cursor)
                for attempt in range(_PROFILE_REQUEST_ATTEMPTS):
                    try:
                        params = _profile_request_params(profile_id, cursor)
                        if attempt % 2:
                            params["count"] = str(_PROFILE_FALLBACK_PAGE_SIZE)
                        _start_signed_fetch(
                            page,
                            _PROFILE_API_PATH,
                            params,
                            request_timeout_ms,
                        )
                        response = _wait_for_signed_response(
                            page,
                            request_timeout_ms,
                            should_cancel,
                        )
                        awemes, has_more, next_cursor = _validate_profile_response(
                            response,
                            profile_id,
                        )
                        break
                    except _TransientSigningFailure:
                        if attempt + 1 >= _PROFILE_REQUEST_ATTEMPTS:
                            raise
                        _wait_with_cancel(
                            page,
                            _PROFILE_RETRY_BASE_MS * (2**attempt),
                            should_cancel,
                        )
                for aweme in awemes:
                    aweme_id = str(aweme["aweme_id"])
                    if aweme_id in collected:
                        continue
                    if len(collected) >= max_awemes:
                        raise _SigningFailure(
                            "Douyin profile reached the aweme safety limit"
                        )
                    collected[aweme_id] = aweme
                if target_aweme_id and target_aweme_id in collected:
                    _raise_if_cancelled(should_cancel)
                    return [collected[target_aweme_id]]
                if not has_more:
                    _raise_if_cancelled(should_cancel)
                    return list(collected.values())
                if next_cursor == cursor:
                    raise _SigningFailure("Douyin profile pagination did not advance")
                cursor = str(next_cursor)
            raise _SigningFailure("Douyin profile reached the page safety limit")

        with _serialized_signed_fetch(should_cancel):
            for session_attempt in range(_PROFILE_SIGNING_SESSION_ATTEMPTS):
                try:
                    return _run_with_signing_page(
                        profile_url,
                        cookie_profile=cookie_profile,
                        should_cancel=should_cancel,
                        navigation_timeout_ms=navigation_timeout_ms,
                        signer_timeout_ms=signer_timeout_ms,
                        signer_settle_ms=signer_settle_ms,
                        operation=fetch_pages,
                    )
                except _TransientSigningFailure:
                    if session_attempt + 1 >= _PROFILE_SIGNING_SESSION_ATTEMPTS:
                        raise
                    _wait_without_page_with_cancel(
                        _PROFILE_SIGNING_RETRY_BASE_MS * (2**session_attempt),
                        should_cancel,
                    )
        raise _SigningFailure("Douyin profile signing attempts were exhausted")
    except DownloadCancelledError:
        raise
    except Exception as exc:
        _raise_signing_error(profile_url, exc)
