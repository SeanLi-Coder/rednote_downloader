from __future__ import annotations

import contextlib
import re
import time
from dataclasses import dataclass
from http.cookiejar import CookieJar
from typing import Any, Callable, Iterable
from urllib.parse import urljoin, urlsplit

from yt_dlp.cookies import extract_cookies_from_browser

from .browser import chrome_user_agent
from .errors import AuthenticationRequiredError, DiscoveryError, DownloadCancelledError


AUTH_TEXT_PATTERNS = (
    "captcha",
    "verify you are human",
    "security verification",
    "安全验证",
    "验证码",
    "请完成下列验证",
    "访问频繁",
    "网络环境存在风险",
    "登录后查看",
)


@dataclass(slots=True)
class DouyinProfile:
    author: str
    video_urls: list[str]
    cookie_fallback_used: bool = False
    warning: str | None = None
    discovery_complete: bool = True

    def __iter__(self):
        yield self.author
        yield self.video_urls


class _QuietCookieLogger:
    def debug(self, message: str) -> None:
        pass

    def info(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass


def _looks_like_auth_page(text: str) -> bool:
    lowered = text.lower()
    return any(pattern in lowered for pattern in AUTH_TEXT_PATTERNS)


def _cookie_jar_to_playwright(cookie_jar: CookieJar) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    now = time.time()
    for cookie in cookie_jar:
        if not cookie.domain.endswith("douyin.com"):
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


def _extract_cookies(profile: str | None) -> CookieJar:
    return extract_cookies_from_browser(
        "chrome", profile=profile, logger=_QuietCookieLogger()
    )


def _pick_author(page: Any) -> str | None:
    for selector in (
        "h1",
        "[data-e2e='user-title']",
        "[class*='user-info-name']",
        "[class*='nickname']",
    ):
        with contextlib.suppress(Exception):
            text = page.locator(selector).first.inner_text(timeout=1_000).strip()
            if text:
                return text
    with contextlib.suppress(Exception):
        title = page.title().strip()
        title = re.sub(r"\s*[-_]\s*抖音.*$", "", title).strip()
        if title and title != "抖音":
            return title
    return None


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
) -> DouyinProfile:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise DiscoveryError(
            "Playwright is required for Douyin profile discovery"
        ) from exc

    browser_cookies: list[dict[str, Any]] = []
    cookie_fallback_used = False
    if use_browser_cookies:
        try:
            browser_cookies = _cookie_jar_to_playwright(
                _extract_cookies(cookie_profile)
            )
        except Exception as exc:
            if not allow_cookie_fallback:
                raise AuthenticationRequiredError(
                    "Chrome cookies could not be read. Fully quit Chrome and retry, "
                    "approve any system cookie-access prompt, or disable Chrome Cookie "
                    "in settings to continue explicitly without login and create a new task.",
                    verification_url=url,
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
                discovered: dict[str, str] = {}
                api_authors: list[str] = []
                api_has_more: bool | None = None

                def capture_post_response(response: Any) -> None:
                    nonlocal api_has_more
                    if "/aweme/v1/web/aweme/post/" not in response.url:
                        return
                    try:
                        data = response.json()
                    except Exception:
                        return
                    values = data.get("aweme_list") or data.get("awemeList") or []
                    for aweme in values:
                        if not isinstance(aweme, dict):
                            continue
                        author_data = aweme.get("author") or {}
                        nickname = author_data.get("nickname")
                        if isinstance(nickname, str) and nickname.strip():
                            api_authors.append(nickname.strip())
                        video_data = aweme.get("video")
                        if not isinstance(video_data, dict) or not video_data:
                            continue
                        aweme_id = str(
                            aweme.get("aweme_id") or aweme.get("awemeId") or ""
                        )
                        if aweme_id.isdigit():
                            discovered.setdefault(
                                aweme_id, f"https://www.douyin.com/video/{aweme_id}"
                            )
                    has_more = data.get("has_more")
                    if has_more is not None:
                        api_has_more = bool(has_more)

                page = context.new_page()
                page.on("response", capture_post_response)
                page.goto(url, wait_until="commit", timeout=navigation_timeout_ms)
                page.wait_for_timeout(4_000)

                if page.locator("body").count() == 0 and not discovered:
                    raise AuthenticationRequiredError(
                        "Douyin returned a blank verification response. Open this profile "
                        "in Chrome, finish any CAPTCHA or login, then retry the task.",
                        verification_url=url,
                    )
                try:
                    body_text = page.locator("body").inner_text(timeout=5_000)
                except Exception:
                    body_text = page.content()
                if _looks_like_auth_page(body_text) or "captcha" in page.url.lower():
                    raise AuthenticationRequiredError(
                        "Douyin requires verification. Open this profile in Chrome, "
                        "finish the CAPTCHA or login, then retry the task.",
                        verification_url=url,
                    )

                author = (
                    (api_authors[0] if api_authors else None)
                    or _pick_author(page)
                    or "Douyin Author"
                )
                unchanged_rounds = 0
                discovery_complete = False
                discovery_warning: str | None = None
                for _ in range(max_scrolls):
                    if should_cancel and should_cancel():
                        raise DownloadCancelledError("Task cancelled")
                    try:
                        hrefs: Iterable[str] = page.locator(
                            "a[href*='/video/']"
                        ).evaluate_all(
                            "elements => elements.map(element => element.href)"
                        )
                    except Exception:
                        hrefs = []
                    before = len(discovered)
                    for href in hrefs:
                        absolute = urljoin(page.url, href)
                        match = re.search(r"/video/(\d+)", urlsplit(absolute).path)
                        if match:
                            discovered.setdefault(match.group(1), absolute)
                    unchanged_rounds = (
                        unchanged_rounds + 1 if len(discovered) == before else 0
                    )
                    if api_has_more is False and unchanged_rounds >= 2:
                        discovery_complete = True
                        break
                    if unchanged_rounds >= stable_rounds:
                        discovery_warning = (
                            "Douyin stopped returning new videos before the profile "
                            "reported completion. The discovered list may be incomplete; "
                            "retry after checking Chrome verification."
                        )
                        break
                    page.mouse.wheel(0, 5_000)
                    with contextlib.suppress(Exception):
                        page.evaluate(
                            """
                            () => {
                              const candidates = [document.scrollingElement, ...document.querySelectorAll('*')]
                                .filter(Boolean)
                                .filter((element) => element.scrollHeight > element.clientHeight + 100)
                                .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
                              const target = candidates[0];
                              if (target) target.scrollTop = target.scrollHeight;
                            }
                            """
                        )
                    page.wait_for_timeout(1_200)
                    try:
                        updated_body = page.locator("body").inner_text(timeout=2_000)
                    except Exception:
                        updated_body = page.content()
                    if _looks_like_auth_page(updated_body):
                        raise AuthenticationRequiredError(
                            "Douyin interrupted discovery with a verification challenge. "
                            "Complete it in Chrome and retry.",
                            verification_url=url,
                        )
                if not discovery_complete and not discovery_warning:
                    discovery_warning = (
                        "Douyin reached the discovery safety limit before confirming the "
                        "end of the profile. Retry to continue discovering videos."
                    )

                if not discovered:
                    raise DiscoveryError(
                        "No Douyin videos were found. The profile may be empty, private, "
                        "or verification may be required."
                    )
                return DouyinProfile(
                    author=(api_authors[0] if api_authors else author),
                    video_urls=list(discovered.values()),
                    cookie_fallback_used=cookie_fallback_used,
                    warning=discovery_warning,
                    discovery_complete=discovery_complete,
                )
            finally:
                browser.close()
    except (AuthenticationRequiredError, DownloadCancelledError):
        raise
    except PlaywrightError as exc:
        message = str(exc)
        if _looks_like_auth_page(message) or "timeout" in message.lower():
            raise AuthenticationRequiredError(
                "Douyin requires verification in Chrome before this task can continue.",
                verification_url=url,
            ) from exc
        raise DiscoveryError(f"Douyin browser discovery failed: {message}") from exc
