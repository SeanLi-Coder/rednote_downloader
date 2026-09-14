from __future__ import annotations

import contextlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from typing import Any, Callable, Iterable
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

from yt_dlp import YoutubeDL
from yt_dlp.cookies import extract_cookies_from_browser
from yt_dlp.dependencies import requests, urllib3
from yt_dlp.networking import Request
from yt_dlp.networking._requests import RequestsRH, RequestsResponseAdapter
from yt_dlp.networking.exceptions import (
    CertificateVerifyError,
    HTTPError,
    ProxyError,
    RequestError,
    SSLError,
    TransportError,
)
from yt_dlp.utils import DownloadError, js_to_json

from .browser import chrome_user_agent
from .errors import (
    AuthenticationRequiredError,
    DiscoveryError,
    DownloadCancelledError,
    SiteIssueCode,
    TemporaryAccessError,
)


AUTH_TEXT_PATTERNS = (
    "captcha",
    "verify you are human",
    "security verification",
    "安全验证",
    "验证码",
    "请完成验证",
    "登录后查看",
    "请登录",
    "登录后继续",
)
VERIFICATION_CHALLENGE_TEXT_PATTERNS = (
    "captcha",
    "verify you are human",
    "security verification",
    "安全验证",
    "请完成验证",
    "滑块验证",
    "行为验证",
)
TRANSIENT_TEXT_PATTERNS = (
    "访问频繁",
    "请求频繁",
    "too many requests",
    "try again later",
    "网络环境存在风险",
)
EXPLICIT_AUTH_PATH_MARKERS = ("/captcha", "/login", "/passport/", "/verify")
EXPLICIT_VERIFICATION_PATH_MARKERS = ("/captcha", "/verify")
MAX_MEDIA_V2_JSON_CHARS = 1_000_000
XIAOHONGSHU_MAX_REDIRECTS = 5


@dataclass(slots=True)
class XiaohongshuProfile:
    author: str
    note_urls: list[str]
    profile_id: str
    cookie_fallback_used: bool = False
    warning: str | None = None
    discovery_complete: bool = True

    def __iter__(self):
        yield self.author
        yield self.note_urls


@dataclass(slots=True)
class RemoteAsset:
    candidates: list[str]
    index: int
    width: int | None = None
    height: int | None = None
    size: int | None = None
    format_id: str | None = None
    video_uri: str | None = None
    duration: float | None = None
    bit_rate: int | None = None
    quality_candidates: list[dict[str, Any]] | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    probe_prefix_size: int | None = None
    probe_prefix_sha256: str | None = None
    redirect_source_url: str | None = None


@dataclass(slots=True)
class XiaohongshuNote:
    note_id: str
    title: str
    author: str | None
    upload_date: str | None
    author_id: str | None = None
    images: list[RemoteAsset] = field(default_factory=list)
    videos: list[RemoteAsset] = field(default_factory=list)
    live_photos: list[RemoteAsset] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


class _XiaohongshuRedirectRejected(RuntimeError):
    def __init__(
        self,
        reason: str,
        *,
        target_url: str | None = None,
    ) -> None:
        super().__init__(f"Xiaohongshu redirect was blocked ({reason})")
        self.reason = reason
        self.target_url = target_url


class _XiaohongshuNoteIdentityError(DiscoveryError):
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


def _visible_text(value: str) -> str:
    parser = _VisibleTextParser()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        return ""
    return re.sub(r"\s+", " ", " ".join(parser.values)).lower()


def _looks_like_auth_page(text: str) -> bool:
    lowered = _visible_text(text)
    return any(pattern in lowered for pattern in AUTH_TEXT_PATTERNS)


def _looks_like_verification_challenge(text: str) -> bool:
    lowered = _visible_text(text)
    return any(
        pattern in lowered for pattern in VERIFICATION_CHALLENGE_TEXT_PATTERNS
    )


def _page_has_verification_challenge(page: Any) -> bool:
    if _is_explicit_xiaohongshu_verification_url(str(page.url)):
        return True
    try:
        return bool(
            page.evaluate(
                r"""
                () => {
                  const selectors = [
                    'iframe[src*="captcha" i]',
                    '[id*="captcha" i]',
                    '[class*="captcha" i]',
                    '[data-testid*="captcha" i]',
                    '[role="dialog"]',
                    '[class*="modal" i]',
                    '[class*="dialog" i]'
                  ];
                  const challengeText = /(请完成验证|安全验证|行为验证|滑块验证|verify you are human|security verification)/i;
                  const challengeHint = /(captcha|geetest|yidun|tcaptcha|verify[-_ ]?slider)/i;
                  for (const element of document.querySelectorAll(selectors.join(','))) {
                    const style = window.getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    if (style.display === 'none' || style.visibility === 'hidden' || rect.width <= 0 || rect.height <= 0) continue;
                    const hint = [
                      element.id,
                      element.className,
                      element.getAttribute('src'),
                      element.getAttribute('data-testid')
                    ].filter((value) => typeof value === 'string').join(' ');
                    if (challengeHint.test(hint) || challengeText.test(element.innerText || '')) return true;
                  }
                  return false;
                }
                """
            )
        )
    except Exception:
        return False


def _looks_like_transient_limit(text: str) -> bool:
    lowered = _visible_text(text)
    return any(pattern in lowered for pattern in TRANSIENT_TEXT_PATTERNS)


def _is_trusted_xiaohongshu_page_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower()
        return (
            parsed.scheme == "https"
            and parsed.username is None
            and parsed.password is None
            and (
                hostname == "xiaohongshu.com"
                or hostname.endswith(".xiaohongshu.com")
            )
            and parsed.port in {None, 443}
        )
    except (TypeError, ValueError):
        return False


def _is_explicit_xiaohongshu_auth_url(value: str) -> bool:
    if not _is_trusted_xiaohongshu_page_url(value):
        return False
    try:
        path = unquote(urlsplit(value).path).lower()
    except (TypeError, ValueError):
        return False
    return any(marker in path for marker in EXPLICIT_AUTH_PATH_MARKERS)


def _is_explicit_xiaohongshu_verification_url(value: str) -> bool:
    if not _is_trusted_xiaohongshu_page_url(value):
        return False
    try:
        path = unquote(urlsplit(value).path).lower()
    except (TypeError, ValueError):
        return False
    return any(marker in path for marker in EXPLICIT_VERIFICATION_PATH_MARKERS)


def is_trusted_xiaohongshu_note_url(value: str) -> bool:
    if not _is_trusted_xiaohongshu_page_url(value):
        return False
    try:
        path = urlsplit(value).path.rstrip("/")
    except (TypeError, ValueError):
        return False
    return bool(
        re.fullmatch(
            r"/(?:explore|discovery/item)/[0-9a-f]+",
            path,
            re.IGNORECASE,
        )
    )


def xiaohongshu_note_id(value: str) -> str | None:
    if not is_trusted_xiaohongshu_note_url(value):
        return None
    try:
        match = re.fullmatch(
            r"/(?:explore|discovery/item)/([0-9a-f]+)",
            urlsplit(value).path.rstrip("/"),
            re.IGNORECASE,
        )
    except (TypeError, ValueError):
        return None
    return match.group(1).lower() if match else None


def is_trusted_xiaohongshu_profile_url(value: str) -> bool:
    if not _is_trusted_xiaohongshu_page_url(value):
        return False
    try:
        path = urlsplit(value).path.rstrip("/")
    except (TypeError, ValueError):
        return False
    return bool(re.fullmatch(r"/user/profile/[^/]+", path))


def xiaohongshu_profile_id(value: str) -> str | None:
    if not is_trusted_xiaohongshu_profile_url(value):
        return None
    try:
        match = re.fullmatch(
            r"/user/profile/([^/]+)",
            urlsplit(value).path.rstrip("/"),
        )
    except (TypeError, ValueError):
        return None
    return unquote(match.group(1)).strip() if match else None


def is_trusted_xiaohongshu_asset_url(value: str) -> bool:
    if not isinstance(value, str) or not value or len(value) > 8_192:
        return False
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or not hostname
            or not (
                hostname == "xhscdn.com"
                or hostname.endswith(".xhscdn.com")
                or hostname == "xiaohongshu.com"
                or hostname.endswith(".xiaohongshu.com")
            )
        ):
            return False
        return parsed.port in {None, 443}
    except (TypeError, ValueError):
        return False


def _normalize_xiaohongshu_asset_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 8_192:
        return None
    if is_trusted_xiaohongshu_asset_url(value):
        return value
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme != "http"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 80}
            or not (
                hostname == "xhscdn.com"
                or hostname.endswith(".xhscdn.com")
            )
        ):
            return None
        upgraded = urlunsplit(
            parsed._replace(scheme="https", netloc=hostname)
        )
    except (TypeError, ValueError):
        return None
    return upgraded if is_trusted_xiaohongshu_asset_url(upgraded) else None


def _without_xsec_query(value: str) -> str:
    parsed = urlsplit(value)
    filtered = [
        (name, item)
        for name, item in parse_qsl(parsed.query, keep_blank_values=True)
        if name.lower() not in {"xsec_token", "xsec_source"}
    ]
    return urlunsplit(parsed._replace(query=urlencode(filtered, doseq=True)))


def _cookie_jar_to_playwright(cookie_jar: CookieJar) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    now = time.time()
    for cookie in cookie_jar:
        if not cookie.domain.endswith("xiaohongshu.com"):
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
    return result


def _extract_chrome_cookies(profile: str | None) -> CookieJar:
    return extract_cookies_from_browser(
        "chrome", profile=profile, logger=_QuietCookieLogger()
    )


def _pick_author(page: Any) -> str | None:
    with contextlib.suppress(Exception):
        author = page.evaluate(
            """
            () => {
              const root = window.__INITIAL_STATE__ || {};
              const pageRef = root.user?.userPageData;
              const pageData = pageRef?.value || pageRef?._rawValue || pageRef || {};
              const infoRef = root.user?.userInfo;
              const userInfo = infoRef?.value || infoRef?._rawValue || infoRef || {};
              const values = [
                pageData?.basicInfo?.nickname,
                pageData?.basicInfo?.nickName,
                pageData?.user?.nickname,
                pageData?.user?.nickName,
                userInfo?.nickname,
                userInfo?.nickName
              ];
              return values.find((value) => typeof value === 'string' && value.trim()) || null;
            }
            """
        )
        if author:
            return str(author).strip()

    selectors = (
        ".user-name",
        ".username",
        "[class*='user-name']",
        "[class*='username']",
        "h1",
    )
    for selector in selectors:
        with contextlib.suppress(Exception):
            value = page.locator(selector).first.inner_text(timeout=1_000).strip()
            if value:
                return value

    with contextlib.suppress(Exception):
        title = page.title().strip()
        title = re.sub(r"\s*[-_]\s*小红书.*$", "", title).strip()
        if title and title != "小红书":
            return title
    return None


def _profile_note_tokens(page: Any) -> list[tuple[str, str | None]]:
    try:
        values = page.evaluate(
            """
            () => {
              const notesRef = window.__INITIAL_STATE__?.user?.notes;
              const pages = notesRef?.value || notesRef?._rawValue || [];
              const result = [];
              for (const pageItems of pages) {
                if (!Array.isArray(pageItems)) continue;
                for (const item of pageItems) {
                  const card = item?.noteCard || {};
                  const id = item?.id || card?.noteId;
                  const token = item?.xsecToken || card?.xsecToken || null;
                  if (typeof id === 'string') result.push({id, token});
                }
              }
              return result;
            }
            """
        )
    except Exception:
        return []
    result: list[tuple[str, str | None]] = []
    for value in values or []:
        if not isinstance(value, dict):
            continue
        note_id = value.get("id")
        token = value.get("token")
        if isinstance(note_id, str) and re.fullmatch(
            r"[0-9a-f]+", note_id, re.IGNORECASE
        ):
            result.append(
                (
                    note_id.lower(),
                    token if isinstance(token, str) and token else None,
                )
            )
    return result


def _profile_has_more(page: Any) -> bool | None:
    try:
        value = page.evaluate(
            """
            () => {
              const ref = window.__INITIAL_STATE__?.user?.noteQueries;
              const queries = ref?.value || ref?._rawValue || [];
              return typeof queries?.[0]?.hasMore === 'boolean' ? queries[0].hasMore : null;
            }
            """
        )
        return value if isinstance(value, bool) else None
    except Exception:
        return None


def _collect_profile_note_urls(
    page: Any,
    discovered: dict[str, str],
) -> None:
    for note_id, token in _profile_note_tokens(page):
        note_url = f"https://www.xiaohongshu.com/explore/{note_id}"
        if token:
            note_url = (
                f"{note_url}?"
                f"{urlencode({'xsec_token': token, 'xsec_source': 'pc_user'})}"
            )
            discovered[note_id] = note_url
        else:
            discovered.setdefault(note_id, note_url)

    try:
        hrefs: Iterable[str] = page.locator(
            "a[href*='/explore/'], a[href*='/discovery/item/']"
        ).evaluate_all("elements => elements.map(element => element.href)")
    except Exception:
        hrefs = []
    for href in hrefs:
        absolute = urljoin(page.url, href)
        if not is_trusted_xiaohongshu_note_url(absolute):
            continue
        match = re.search(
            r"/(?:explore|discovery/item)/([0-9a-f]+)",
            urlsplit(absolute).path,
            re.IGNORECASE,
        )
        note_id = match.group(1).lower() if match else ""
        # Only INITIAL_STATE.user.notes establishes profile membership. DOM links
        # can include recommendations or navigation outside the author's feed.
        if not note_id or note_id not in discovered:
            continue
        if "xsec_token=" in urlsplit(absolute).query:
            discovered[note_id] = absolute
        else:
            discovered.setdefault(note_id, absolute)


def _xiaohongshu_auth_required(
    verification_url: str,
    *,
    challenge: bool,
) -> AuthenticationRequiredError:
    if challenge:
        message = (
            "Xiaohongshu displayed an explicit verification challenge. Open the "
            "same page in the bound Chrome profile, complete it, and retry."
        )
    else:
        message = (
            "Xiaohongshu did not accept the selected Chrome profile's login session. "
            "Sign in or refresh Xiaohongshu in that same Chrome profile, then retry. "
            "This is a login-session issue; a CAPTCHA is not required unless Chrome "
            "actually displays one."
        )
    return AuthenticationRequiredError(message, verification_url=verification_url)


def discover_profile(
    url: str,
    *,
    cookie_profile: str | None = None,
    use_browser_cookies: bool = True,
    allow_cookie_fallback: bool = False,
    should_cancel: Callable[[], bool] | None = None,
    max_scrolls: int = 300,
    stable_rounds: int = 15,
    navigation_timeout_ms: int = 45_000,
) -> XiaohongshuProfile:
    """Discover all currently visible notes from a Xiaohongshu profile."""

    profile_id = xiaohongshu_profile_id(url)
    if not profile_id:
        raise DiscoveryError("Invalid or untrusted Xiaohongshu profile URL")

    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise DiscoveryError(
            "Playwright is required for Xiaohongshu profile discovery"
        ) from exc

    cookie_fallback_used = False
    browser_cookies: list[dict[str, Any]] = []
    if use_browser_cookies:
        try:
            browser_cookies = _cookie_jar_to_playwright(
                _extract_chrome_cookies(cookie_profile)
            )
        except Exception as exc:
            if not allow_cookie_fallback:
                raise TemporaryAccessError(
                    "Chrome cookies could not be read. Fully quit Chrome and retry, "
                    "approve any system cookie-access prompt, or disable Chrome Cookie "
                    "in settings to continue explicitly without login and create a new "
                    "task. Opening a verification page is not required.",
                ) from exc
            cookie_fallback_used = True

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            try:
                browser_version = browser.version
                context = browser.new_context(
                    locale="zh-CN",
                    viewport={"width": 1440, "height": 1100},
                    user_agent=chrome_user_agent(browser_version),
                )
                if browser_cookies:
                    context.add_cookies(browser_cookies)
                page = context.new_page()
                page.goto(
                    url, wait_until="domcontentloaded", timeout=navigation_timeout_ms
                )
                page.wait_for_timeout(2_000)

                if _is_explicit_xiaohongshu_auth_url(page.url):
                    raise _xiaohongshu_auth_required(
                        url,
                        challenge=_is_explicit_xiaohongshu_verification_url(page.url),
                    )
                if not _is_trusted_xiaohongshu_page_url(page.url):
                    raise DiscoveryError(
                        "Xiaohongshu profile discovery redirected outside the trusted "
                        "Xiaohongshu origin"
                    )
                if page.locator("body").count() == 0:
                    raise TemporaryAccessError(
                        "Xiaohongshu profile discovery temporarily returned a blank "
                        "browser response. Retry after a short wait; Chrome "
                        "verification is not required."
                    )
                try:
                    body_text = page.locator("body").inner_text(timeout=5_000)
                except Exception:
                    body_text = page.content()
                discovered: dict[str, str] = {}
                _collect_profile_note_urls(page, discovered)
                if _looks_like_transient_limit(body_text):
                    raise TemporaryAccessError(
                        "Xiaohongshu profile discovery was temporarily rate-limited. "
                        "Retry after a short wait; Chrome verification is not required."
                    )
                if _page_has_verification_challenge(page):
                    raise _xiaohongshu_auth_required(url, challenge=True)
                if not discovered and _looks_like_auth_page(body_text):
                    raise _xiaohongshu_auth_required(url, challenge=False)

                author = _pick_author(page) or "Xiaohongshu Author"
                unchanged_rounds = 0
                discovery_complete = False
                discovery_warning: str | None = None

                for _ in range(max_scrolls):
                    if should_cancel and should_cancel():
                        raise DownloadCancelledError("Task cancelled")
                    before = len(discovered)
                    _collect_profile_note_urls(page, discovered)
                    unchanged_rounds = (
                        unchanged_rounds + 1 if len(discovered) == before else 0
                    )
                    has_more = _profile_has_more(page)
                    if has_more is False and unchanged_rounds >= 2:
                        discovery_complete = True
                        break
                    if unchanged_rounds >= stable_rounds:
                        discovery_warning = (
                            "Xiaohongshu stopped returning new notes before the profile "
                            "reported completion. The discovered list may be incomplete; "
                            "retry discovery after a short wait."
                        )
                        break
                    page.mouse.wheel(0, 5_000)
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(1_000)

                    if _is_explicit_xiaohongshu_auth_url(page.url):
                        raise _xiaohongshu_auth_required(
                            url,
                            challenge=_is_explicit_xiaohongshu_verification_url(
                                page.url
                            ),
                        )
                    if not _is_trusted_xiaohongshu_page_url(page.url):
                        raise DiscoveryError(
                            "Xiaohongshu profile discovery redirected outside the "
                            "trusted Xiaohongshu origin"
                        )
                    try:
                        updated_body = page.locator("body").inner_text(timeout=2_000)
                    except Exception:
                        updated_body = ""
                    if _looks_like_transient_limit(updated_body):
                        raise TemporaryAccessError(
                            "Xiaohongshu profile discovery was temporarily rate-limited. "
                            "Retry after a short wait; Chrome verification is not "
                            "required."
                        )
                    if _page_has_verification_challenge(page):
                        raise _xiaohongshu_auth_required(url, challenge=True)
                    if not discovered and _looks_like_auth_page(updated_body):
                        raise _xiaohongshu_auth_required(url, challenge=False)
                if not discovery_complete and not discovery_warning:
                    discovery_warning = (
                        "Xiaohongshu reached the discovery safety limit before confirming "
                        "the end of the profile. Retry to continue discovering notes."
                    )

                if not discovered:
                    raise DiscoveryError(
                        "No Xiaohongshu notes were found. The profile may be empty, "
                        "private, or verification may be required."
                    )
                return XiaohongshuProfile(
                    author=author,
                    note_urls=list(discovered.values()),
                    profile_id=profile_id,
                    cookie_fallback_used=cookie_fallback_used,
                    warning=discovery_warning,
                    discovery_complete=discovery_complete,
                )
            finally:
                browser.close()
    except (
        AuthenticationRequiredError,
        DownloadCancelledError,
        TemporaryAccessError,
    ):
        raise
    except PlaywrightError as exc:
        message = str(exc)
        if "timeout" in message.lower() or _looks_like_transient_limit(message):
            raise TemporaryAccessError(
                "Xiaohongshu profile discovery temporarily timed out or was "
                "rate-limited. Retry after a short wait; Chrome verification is not "
                "required."
            ) from exc
        raise DiscoveryError(
            f"Xiaohongshu browser discovery failed: {message}"
        ) from exc


def _extract_balanced_object(source: str, offset: int) -> str:
    start = source.find("{", offset)
    if start < 0:
        raise ValueError("Object start not found")
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(start, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'", "`"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise ValueError("Object end not found")


def _open_xiaohongshu_response(
    ydl: YoutubeDL,
    request: Request,
    *,
    is_trusted_url: Callable[[str], bool],
    max_redirects: int = XIAOHONGSHU_MAX_REDIRECTS,
):
    if not is_trusted_url(request.url):
        raise _XiaohongshuRedirectRejected(
            "untrusted-url",
            target_url=request.url,
        )
    director = getattr(ydl, "_request_director", None)
    handler = getattr(director, "handlers", {}).get("Requests")
    if not isinstance(handler, RequestsRH) or requests is None:
        raise _XiaohongshuRedirectRejected("secure-handler-unavailable")

    headers = handler._get_headers(request)
    current_headers = {
        key: value
        for key, value in headers.items()
        if key.lower()
        not in {
            "authorization",
            "cookie",
            "host",
            "proxy-authorization",
        }
    }
    session = handler._get_instance(
        cookiejar=handler._get_cookiejar(request),
        legacy_ssl_support=request.extensions.get("legacy_ssl"),
    )
    current_url = request.url
    request_timeout = handler._calculate_timeout(request)
    redirect_deadline = time.monotonic() + request_timeout

    for redirect_count in range(max_redirects + 1):
        remaining_timeout = redirect_deadline - time.monotonic()
        if remaining_timeout <= 0:
            raise TransportError(
                cause=TimeoutError("Xiaohongshu redirect chain timed out")
            )
        redirect_location_present = False
        redirect_location: str | None = None

        def suppress_requests_redirect_preparation(response, *args, **kwargs):
            nonlocal redirect_location_present, redirect_location
            redirect_location_present = "Location" in response.headers
            redirect_location = response.headers.pop("Location", None)
            return response

        try:
            raw_response = session.request(
                method=request.method,
                url=current_url,
                data=request.data,
                headers=current_headers,
                timeout=max(0.1, min(request_timeout, remaining_timeout)),
                proxies=handler._get_proxies(request),
                allow_redirects=False,
                stream=True,
                hooks={"response": suppress_requests_redirect_preparation},
            )
        except requests.exceptions.SSLError as exc:
            if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                raise CertificateVerifyError(cause=exc) from exc
            raise SSLError(cause=exc) from exc
        except requests.exceptions.ProxyError as exc:
            raise ProxyError(cause=exc) from exc
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        ) as exc:
            raise TransportError(cause=exc) from exc
        except urllib3.exceptions.HTTPError as exc:
            raise TransportError(cause=exc) from exc
        except requests.exceptions.RequestException as exc:
            raise RequestError(cause=exc) from exc

        if redirect_location_present:
            raw_response.headers["Location"] = redirect_location or ""

        response_url = str(getattr(raw_response, "url", None) or current_url)
        try:
            response_is_trusted = is_trusted_url(response_url)
        except BaseException:
            raw_response.close()
            raise
        if not response_is_trusted:
            raw_response.close()
            raise _XiaohongshuRedirectRejected(
                "untrusted-response-url",
                target_url=response_url,
            )

        is_redirect_response = bool(raw_response.is_redirect) or getattr(
            raw_response,
            "status_code",
            None,
        ) in {301, 302, 303, 307, 308}
        if is_redirect_response:
            location = str(raw_response.headers.get("Location") or "")
            if (
                not location
                or len(location) > 8_192
                or "\\" in location
                or any(character.isspace() for character in location)
                or any(
                    ord(character) < 32 or ord(character) == 127
                    for character in location
                )
            ):
                raw_response.close()
                raise _XiaohongshuRedirectRejected("malformed-location")
            try:
                target_url = urljoin(response_url, location)
            except (TypeError, ValueError):
                raw_response.close()
                raise _XiaohongshuRedirectRejected("malformed-location")
            try:
                target_is_trusted = is_trusted_url(target_url)
            except BaseException:
                raw_response.close()
                raise
            if not target_is_trusted:
                raw_response.close()
                raise _XiaohongshuRedirectRejected(
                    "untrusted-target",
                    target_url=target_url,
                )
            if redirect_count >= max_redirects:
                raw_response.close()
                raise _XiaohongshuRedirectRejected(
                    "too-many-redirects",
                    target_url=target_url,
                )
            raw_response.close()
            current_url = target_url
            continue

        response = RequestsResponseAdapter(raw_response)
        if not 200 <= response.status < 300:
            raise HTTPError(response)
        return response
    raise AssertionError("unreachable Xiaohongshu redirect loop state")


def _read_page(ydl: YoutubeDL, url: str) -> str:
    expected_note_id = xiaohongshu_note_id(url)

    def is_trusted_note_target(value: str) -> bool:
        return bool(
            expected_note_id
            and xiaohongshu_note_id(value) == expected_note_id
        )

    try:
        response = _open_xiaohongshu_response(
            ydl,
            Request(
                url,
                headers={
                    "Referer": "https://www.xiaohongshu.com/",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
            ),
            is_trusted_url=is_trusted_note_target,
        )
    except _XiaohongshuRedirectRejected as exc:
        if exc.target_url and _is_explicit_xiaohongshu_auth_url(exc.target_url):
            raise _xiaohongshu_auth_required(
                url,
                challenge=_is_explicit_xiaohongshu_verification_url(
                    exc.target_url
                ),
            ) from exc
        raise DiscoveryError(
            "Xiaohongshu note redirect was blocked before requesting an "
            "untrusted or mismatched target"
        ) from exc
    try:
        final_url = str(getattr(response, "url", None) or url)
        if _is_explicit_xiaohongshu_auth_url(final_url):
            raise _xiaohongshu_auth_required(
                url,
                challenge=_is_explicit_xiaohongshu_verification_url(final_url),
            )
        if not is_trusted_xiaohongshu_note_url(final_url):
            raise DiscoveryError(
                "Xiaohongshu note request redirected outside the trusted note origin"
            )
        data = response.read()
        content_type = response.headers.get("Content-Type") or ""
        encoding_match = re.search(r"charset=([^;\s]+)", content_type, re.IGNORECASE)
        encoding = encoding_match.group(1).strip("\"'") if encoding_match else "utf-8"
        return data.decode(encoding, errors="replace")
    finally:
        response.close()


def _initial_state_from_html(html: str) -> dict[str, Any]:
    marker = re.search(r"window\.__INITIAL_STATE__\s*=", html)
    if not marker:
        raise ValueError("Initial state marker not found")
    source = _extract_balanced_object(html, marker.end())
    return json.loads(js_to_json(source))


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _unique_urls(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _normalize_xiaohongshu_asset_url(value)
        if not normalized:
            continue
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _untransformed_xhs_url(url: str) -> str | None:
    parsed = urlsplit(url)
    if "!" not in parsed.path:
        return None
    path = parsed.path.split("!", 1)[0]
    return urlunsplit(parsed._replace(path=path))


def _direct_xhs_image_urls(url: str) -> list[str]:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not is_trusted_xiaohongshu_asset_url(url) or not (
        hostname == "xhscdn.com" or hostname.endswith(".xhscdn.com")
    ):
        return []
    parts = parsed.path.lstrip("/").split("/")
    if len(parts) < 3:
        return []
    asset_path = "/".join(parts[2:]).split("!", 1)[0]
    if not asset_path:
        return []
    return [
        f"https://sns-img-bd.xhscdn.com/{asset_path}",
        f"https://ci.xiaohongshu.com/{asset_path}",
        f"https://sns-img-qc.xhscdn.com/{asset_path}",
        f"https://sns-img-hw.xhscdn.com/{asset_path}",
    ]


def _image_candidates(image: dict[str, Any]) -> list[str]:
    scored: list[tuple[int, str]] = []
    file_id = image.get("fileId")
    if isinstance(file_id, str) and file_id:
        scored.extend(
            (
                (1_100, f"https://sns-img-bd.xhscdn.com/{file_id}"),
                (1_090, f"https://ci.xiaohongshu.com/{file_id}"),
                (1_080, f"https://sns-img-qc.xhscdn.com/{file_id}"),
            )
        )
    if isinstance(image.get("url"), str) and image["url"]:
        scored.append((900, image["url"]))
    for info in image.get("infoList") or []:
        if not isinstance(info, dict):
            continue
        url = info.get("url")
        scene = str(info.get("imageScene") or "").upper()
        if isinstance(url, str):
            score = 500 if any(key in scene for key in ("ORG", "ORIGINAL")) else 300
            if "DFT" in scene:
                score = max(score, 400)
            if "PRV" in scene:
                score = min(score, 100)
            scored.append((score, url))
    if isinstance(image.get("urlDefault"), str):
        scored.append((450, image["urlDefault"]))
    if isinstance(image.get("urlPre"), str):
        scored.append((50, image["urlPre"]))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    candidates: list[str] = []
    for _, url in scored:
        candidates.extend(_direct_xhs_image_urls(url))
        original = _untransformed_xhs_url(url)
        if original:
            candidates.append(original)
        candidates.append(url)
    return _unique_urls(candidates)


def _walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _video_media_roots(video: dict[str, Any]) -> list[dict[str, Any]]:
    roots = [video]
    media_v2 = video.get("mediaV2")
    if isinstance(media_v2, str):
        if len(media_v2) > MAX_MEDIA_V2_JSON_CHARS:
            media_v2 = None
        else:
            try:
                media_v2 = json.loads(media_v2)
            except (TypeError, ValueError, json.JSONDecodeError):
                media_v2 = None
    if isinstance(media_v2, dict):
        roots.append(media_v2)
    return roots


def _url_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str)]
    return []


def _video_assets(note: dict[str, Any]) -> list[RemoteAsset]:
    video = note.get("video") or {}
    if not isinstance(video, dict):
        return []
    assets: list[RemoteAsset] = []
    origin_key = (
        ((video.get("consumer") or {}).get("originVideoKey"))
        if isinstance(video, dict)
        else None
    )
    if isinstance(origin_key, str) and origin_key:
        assets.append(
            RemoteAsset(
                candidates=[
                    f"https://sns-video-bd.xhscdn.com/{origin_key.lstrip('/')}"
                ],
                index=1,
                format_id="original",
            )
        )

    candidates: list[tuple[tuple[int, int, int], RemoteAsset]] = []
    for root in _video_media_roots(video):
        for data in _walk_dicts(root):
            master_url = data.get("masterUrl") or data.get("master_url")
            backup_urls = _url_values(
                data.get("backupUrls") or data.get("backup_urls")
            )
            urls = _unique_urls([master_url, *backup_urls])
            if not urls:
                continue
            width = _as_int(data.get("width"))
            height = _as_int(data.get("height"))
            bitrate = _as_int(
                data.get("avgBitrate")
                or data.get("avg_bitrate")
                or data.get("videoBitrate")
                or data.get("video_bitrate")
            )
            size = _as_int(data.get("size"))
            score = ((width or 0) * (height or 0), bitrate or 0, size or 0)
            candidates.append(
                (
                    score,
                    RemoteAsset(
                        candidates=urls,
                        index=1,
                        width=width,
                        height=height,
                        size=size,
                        format_id=str(
                            data.get("qualityType")
                            or data.get("quality_type")
                            or "stream"
                        ),
                    ),
                )
            )
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    for _, asset in candidates:
        if not any(asset.candidates[0] in existing.candidates for existing in assets):
            assets.append(asset)
    return assets


def _live_photo_asset(image: dict[str, Any], index: int) -> RemoteAsset | None:
    candidates: list[tuple[tuple[int, int, int], RemoteAsset]] = []
    for data in _walk_dicts(image.get("stream") or {}):
        master_url = data.get("masterUrl") or data.get("master_url")
        backup_urls = _url_values(
            data.get("backupUrls") or data.get("backup_urls")
        )
        urls = _unique_urls([master_url, *backup_urls])
        if not urls:
            continue
        # A Live Photo's still image and motion clip are separate media files.
        # The image dimensions are not evidence of the clip dimensions.
        width = _as_int(data.get("width"))
        height = _as_int(data.get("height"))
        bitrate = _as_int(
            data.get("avgBitrate")
            or data.get("avg_bitrate")
            or data.get("videoBitrate")
            or data.get("video_bitrate")
        )
        size = _as_int(data.get("size"))
        score = ((width or 0) * (height or 0), bitrate or 0, size or 0)
        candidates.append(
            (
                score,
                RemoteAsset(
                    candidates=urls,
                    index=index,
                    width=width,
                    height=height,
                    size=size,
                    bit_rate=bitrate,
                    format_id=str(
                        data.get("qualityType")
                        or data.get("quality_type")
                        or "live-photo"
                    ),
                ),
            )
        )
    return max(candidates, key=lambda pair: pair[0])[1] if candidates else None


def _upload_date(note: dict[str, Any]) -> str | None:
    china_time = timezone(timedelta(hours=8))
    for key in ("time", "createTime", "lastUpdateTime", "timestamp"):
        value = _as_int(note.get(key))
        if not value:
            continue
        if value > 10_000_000_000:
            value //= 1000
        with contextlib.suppress(ValueError, OSError, OverflowError):
            return datetime.fromtimestamp(value, tz=china_time).strftime("%Y-%m-%d")
    return None


def parse_note(
    url: str,
    *,
    cookie_profile: str | None = None,
    use_browser_cookies: bool = True,
    allow_cookie_fallback: bool = False,
) -> tuple[XiaohongshuNote, bool]:
    if not is_trusted_xiaohongshu_note_url(url):
        raise DiscoveryError("Invalid or untrusted Xiaohongshu note URL")
    match = re.search(
        r"/(?:explore|discovery/item)/([0-9a-f]+)", urlsplit(url).path, re.IGNORECASE
    )
    if not match:
        raise DiscoveryError("Invalid Xiaohongshu note URL")
    note_id = match.group(1).lower()
    cookie_fallback_used = False

    def load(use_cookies: bool) -> str:
        options: dict[str, Any] = {"quiet": True, "no_warnings": True}
        if use_cookies:
            options["cookiesfrombrowser"] = (
                ("chrome", cookie_profile) if cookie_profile else ("chrome",)
            )
        with YoutubeDL(options) as ydl:
            return _read_page(ydl, url)

    try:
        html = load(use_browser_cookies)
    except (DownloadError, OSError) as exc:
        message = str(exc).lower()
        cookie_error = any(
            marker in message
            for marker in (
                "cookie database",
                "could not copy chrome",
                "decrypt",
                "keyring",
                "failed to load cookies",
                "could not find chrome cookies",
            )
        )
        if use_browser_cookies and cookie_error and not allow_cookie_fallback:
            raise TemporaryAccessError(
                "Chrome cookies could not be read. Fully quit Chrome and retry, "
                "approve any system cookie-access prompt, or disable Chrome Cookie "
                "in settings to continue explicitly without login and create a new "
                "task. Opening a verification page is not required.",
            ) from exc
        if not use_browser_cookies or not cookie_error:
            raise
        html = load(False)
        cookie_fallback_used = True

    if _looks_like_transient_limit(html):
        raise TemporaryAccessError(
            "Xiaohongshu temporarily rate-limited the note request. Retry after a "
            "short wait; Chrome verification is not required."
        )
    state_error: Exception | None = None
    note: Any = None
    try:
        state = _initial_state_from_html(html)
        note = (
            ((state.get("note") or {}).get("noteDetailMap") or {}).get(note_id) or {}
        ).get("note")
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        state_error = exc
    if not isinstance(note, dict) or not note:
        if _looks_like_auth_page(html):
            raise _xiaohongshu_auth_required(
                url,
                challenge=_looks_like_verification_challenge(html),
            )
        if state_error is not None:
            raise DiscoveryError(
                f"Could not parse Xiaohongshu note data: {state_error}"
            ) from state_error
        if "xsec_token=" in urlsplit(url).query:
            canonical_url = _without_xsec_query(url)
            if canonical_url != url:
                try:
                    refreshed_note, refreshed_fallback = parse_note(
                        canonical_url,
                        cookie_profile=cookie_profile,
                        use_browser_cookies=use_browser_cookies,
                        allow_cookie_fallback=allow_cookie_fallback,
                    )
                except _XiaohongshuNoteIdentityError:
                    raise
                except DiscoveryError as exc:
                    raise TemporaryAccessError(
                        "Xiaohongshu returned no note data for the saved access token "
                        "or the canonical note URL. Retry or create a new task from a "
                        "fresh link; Chrome verification is not required unless "
                        "Xiaohongshu explicitly shows a CAPTCHA or login page."
                    ) from exc
                return refreshed_note, cookie_fallback_used or refreshed_fallback
        raise DiscoveryError(
            "Xiaohongshu returned no note data. The note may be private, deleted, "
            "or require verification."
        )

    # The map key binds legacy payloads; an explicit inner ID must agree with it.
    for identity_key in ("noteId", "note_id", "id"):
        if identity_key not in note:
            continue
        value = note[identity_key]
        if not isinstance(value, str) or value.strip().lower() != note_id:
            raise _XiaohongshuNoteIdentityError(
                "Xiaohongshu returned a mismatched or invalid note identity. "
                "The response was blocked before downloading media; check the "
                "original note link. Chrome verification is not required.",
                issue_code=SiteIssueCode.SITE_RESPONSE_CHANGED,
            )

    user = note.get("user") if isinstance(note.get("user"), dict) else {}
    author_id = ""
    for key in ("userId", "user_id", "userid", "id"):
        value = user.get(key)
        if isinstance(value, str) and value.strip():
            author_id = value.strip()
            break
    title = str(note.get("title") or note.get("desc") or "").strip()
    title = re.sub(r"\s+", " ", title)[:240]
    if not title or title == note_id or title.isdigit():
        title = "Untitled Xiaohongshu note"
    images: list[RemoteAsset] = []
    live_photos: list[RemoteAsset] = []
    for index, image in enumerate(note.get("imageList") or [], start=1):
        if not isinstance(image, dict):
            continue
        candidates = _image_candidates(image)
        if candidates:
            images.append(
                RemoteAsset(
                    candidates=candidates,
                    index=index,
                    width=_as_int(image.get("width")),
                    height=_as_int(image.get("height")),
                    size=_as_int(image.get("fileSize") or image.get("size")),
                    format_id="original-image",
                )
            )
        if live_photo := _live_photo_asset(image, index):
            live_photos.append(live_photo)

    return (
        XiaohongshuNote(
            note_id=note_id,
            title=title,
            author=str(user.get("nickname") or user.get("nickName") or "").strip()
            or None,
            upload_date=_upload_date(note),
            author_id=author_id or None,
            images=images,
            videos=_video_assets(note),
            live_photos=live_photos,
            raw=note,
        ),
        cookie_fallback_used,
    )
