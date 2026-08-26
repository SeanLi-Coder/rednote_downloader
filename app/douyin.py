from __future__ import annotations

import contextlib
import re
import time
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, unquote, urlsplit

from yt_dlp.cookies import extract_cookies_from_browser

from .browser import chrome_user_agent
from .douyin_signing import fetch_signed_profile_awemes
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

DOUYIN_QUALITY_FLOOR_SHORT_EDGE = 1080
DOUYIN_QUALITY_FLOOR_LONG_EDGE = 1920


@dataclass(slots=True)
class DouyinProfile:
    author: str
    video_urls: list[str]
    cookie_fallback_used: bool = False
    warning: str | None = None
    discovery_complete: bool = True
    media_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)

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


def _profile_id(url: str) -> str | None:
    match = re.search(r"/user/([^/?#]+)", urlsplit(url).path)
    return unquote(match.group(1)) if match else None


def _is_target_post_response(response_url: str, profile_id: str) -> bool:
    parsed = urlsplit(response_url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not (hostname == "douyin.com" or hostname.endswith(".douyin.com"))
        or parsed.path.rstrip("/") != "/aweme/v1/web/aweme/post"
    ):
        return False
    return profile_id in parse_qs(parsed.query).get("sec_user_id", [])


def _parse_profile_awemes(
    data: dict[str, Any], profile_id: str
) -> tuple[list[tuple[str, str]], list[str], bool | None]:
    entries: list[tuple[str, str]] = []
    authors: list[str] = []
    values = data.get("aweme_list") or data.get("awemeList") or []
    if not isinstance(values, list):
        return entries, authors, None
    for aweme in values:
        if not isinstance(aweme, dict):
            continue
        author_data = aweme.get("author") or {}
        if not isinstance(author_data, dict):
            continue
        owner_id = str(
            author_data.get("sec_uid") or author_data.get("secUid") or ""
        ).strip()
        if owner_id != profile_id:
            continue
        nickname = author_data.get("nickname")
        if isinstance(nickname, str) and nickname.strip():
            authors.append(nickname.strip())
        video_data = aweme.get("video")
        if not isinstance(video_data, dict) or not video_data:
            continue
        aweme_id = str(aweme.get("aweme_id") or aweme.get("awemeId") or "")
        if aweme_id.isdigit():
            entries.append((aweme_id, f"https://www.douyin.com/video/{aweme_id}"))
    has_more = data.get("has_more")
    return entries, authors, bool(has_more) if has_more is not None else None


def quality_floor_dimensions(
    candidates: Iterable[Any],
    *,
    cap_full_hd: bool = True,
) -> tuple[int, int] | None:
    """Return the largest valid dimensions, optionally capped at Full HD."""
    dimensions: list[tuple[int, int]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        try:
            width = int(candidate.get("width") or candidate.get("minimum_width") or 0)
            height = int(
                candidate.get("height") or candidate.get("minimum_height") or 0
            )
        except (TypeError, ValueError, OverflowError):
            continue
        if not (0 < width <= 16_384 and 0 < height <= 16_384):
            continue
        dimensions.append((width, height))
    if not dimensions:
        return None

    width, height = max(dimensions, key=lambda value: value[0] * value[1])
    if not cap_full_hd:
        return width, height
    if width <= height:
        return (
            min(width, DOUYIN_QUALITY_FLOOR_SHORT_EDGE),
            min(height, DOUYIN_QUALITY_FLOOR_LONG_EDGE),
        )
    return (
        min(width, DOUYIN_QUALITY_FLOOR_LONG_EDGE),
        min(height, DOUYIN_QUALITY_FLOOR_SHORT_EDGE),
    )


def _safe_direct_media_urls(address: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for value in address.get("url_list") or []:
        if not isinstance(value, str) or len(value) > 8_192:
            continue
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in ("douyin.com", "douyinvod.com", "amemv.com")
        ):
            continue
        if value not in result:
            result.append(value)
        if len(result) >= 5:
            break
    return result


def _direct_quality_candidate(
    address: Any,
    *,
    bit_rate: Any = None,
    codec_hint: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(address, dict):
        return None
    dimensions = quality_floor_dimensions([address], cap_full_hd=False)
    urls = _safe_direct_media_urls(address)
    if not dimensions or not urls:
        return None
    candidate: dict[str, Any] = {
        "width": dimensions[0],
        "height": dimensions[1],
        "urls": urls,
    }
    try:
        normalized_bit_rate = int(bit_rate or address.get("bit_rate") or 0)
    except (TypeError, ValueError, OverflowError):
        normalized_bit_rate = 0
    if normalized_bit_rate > 0:
        candidate["bit_rate"] = normalized_bit_rate
    if codec_hint:
        candidate["codec_hint"] = codec_hint
    return candidate


def _minimal_aweme_metadata(
    aweme: dict[str, Any], profile_id: str
) -> tuple[str, dict[str, Any]] | None:
    aweme_id = str(aweme.get("aweme_id") or aweme.get("awemeId") or "").strip()
    author = aweme.get("author")
    if not aweme_id.isdigit() or not isinstance(author, dict):
        return None
    owner_id = str(author.get("sec_uid") or author.get("secUid") or "").strip()
    if owner_id != profile_id:
        return None
    video = aweme.get("video")
    if not isinstance(video, dict):
        return None
    video_uri = ""
    address_values = [
        ("play_addr", video.get("play_addr"), None),
        ("play_addr_h264", video.get("play_addr_h264"), "h264"),
        ("play_addr_265", video.get("play_addr_265"), "hevc"),
        ("play_addr_bytevc1", video.get("play_addr_bytevc1"), "hevc"),
    ]
    addresses = [value for _, value, _ in address_values]
    bit_rates = video.get("bit_rate")
    if isinstance(bit_rates, list):
        addresses.extend(
            value.get("play_addr") for value in bit_rates if isinstance(value, dict)
        )
    for address in addresses:
        if not isinstance(address, dict):
            continue
        candidate = str(address.get("uri") or "").strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{10,200}", candidate):
            video_uri = candidate
            break
    if not video_uri:
        return None

    metadata: dict[str, Any] = {
        "media_id": aweme_id,
        "owner_id": owner_id,
        "video_uri": video_uri,
    }
    direct_candidates = [
        candidate
        for _, address, codec_hint in address_values
        if (
            candidate := _direct_quality_candidate(
                address,
                codec_hint=codec_hint,
            )
        )
    ]
    if isinstance(bit_rates, list):
        for value in bit_rates:
            if not isinstance(value, dict):
                continue
            codec_hint = (
                "hevc"
                if value.get("is_h265") in {1, True}
                or value.get("is_bytevc1") in {1, True}
                else "h264"
            )
            candidate = _direct_quality_candidate(
                value.get("play_addr"),
                bit_rate=value.get("bit_rate"),
                codec_hint=codec_hint,
            )
            if candidate:
                direct_candidates.append(candidate)
    if direct_candidates:
        direct_candidates.sort(
            key=lambda value: (
                value["width"] * value["height"],
                int(value.get("bit_rate") or 0),
            ),
            reverse=True,
        )
        highest_pixels = max(
            value["width"] * value["height"] for value in direct_candidates
        )
        unique_candidates: list[dict[str, Any]] = []
        seen_candidates: set[tuple[int, int, tuple[str, ...]]] = set()
        for candidate in direct_candidates:
            if candidate["width"] * candidate["height"] != highest_pixels:
                continue
            signature = (
                candidate["width"],
                candidate["height"],
                tuple(candidate["urls"]),
            )
            if signature in seen_candidates:
                continue
            seen_candidates.add(signature)
            unique_candidates.append(candidate)
        metadata["direct_candidates"] = unique_candidates[:4]
    quality_candidates: list[Any] = [video, *addresses]
    if isinstance(bit_rates, list):
        quality_candidates.extend(bit_rates)
    quality_floor = quality_floor_dimensions(
        metadata.get("direct_candidates") or quality_candidates,
        cap_full_hd=not bool(metadata.get("direct_candidates")),
    )
    if quality_floor:
        metadata["minimum_width"], metadata["minimum_height"] = quality_floor
    duration_ms = video.get("duration")
    if isinstance(duration_ms, int) and duration_ms > 0:
        metadata["duration_ms"] = duration_ms
    create_time = aweme.get("create_time")
    if isinstance(create_time, int) and create_time > 0:
        metadata["create_time"] = create_time
    description = aweme.get("desc")
    if isinstance(description, str) and description.strip():
        metadata["title"] = description.strip()
    nickname = author.get("nickname")
    if isinstance(nickname, str) and nickname.strip():
        metadata["author"] = nickname.strip()
    return aweme_id, metadata


def discover_item_metadata_from_profile(
    profile_id: str,
    media_id: str,
    *,
    cookie_profile: str | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any] | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", profile_id):
        return None
    if not media_id.isdigit():
        return None
    profile_url = f"https://www.douyin.com/user/{profile_id}"
    awemes = fetch_signed_profile_awemes(
        profile_url,
        profile_id,
        target_aweme_id=media_id,
        cookie_profile=cookie_profile,
        should_cancel=should_cancel,
    )
    for aweme in awemes:
        if str(aweme.get("aweme_id") or "").strip() != media_id:
            continue
        parsed = _minimal_aweme_metadata(aweme, profile_id)
        return parsed[1] if parsed else None
    return None


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
    profile_id = _profile_id(url)
    if not profile_id:
        raise DiscoveryError("The Douyin profile URL has no profile identifier")
    if use_browser_cookies:
        try:
            signed_awemes = fetch_signed_profile_awemes(
                url,
                profile_id,
                cookie_profile=cookie_profile,
                should_cancel=should_cancel,
            )
        except AuthenticationRequiredError:
            pass
        else:
            signed_data = {"aweme_list": signed_awemes, "has_more": False}
            entries, authors, _ = _parse_profile_awemes(signed_data, profile_id)
            metadata = dict(
                value
                for aweme in signed_awemes
                if (value := _minimal_aweme_metadata(aweme, profile_id))
            )
            return DouyinProfile(
                author=authors[0] if authors else "Douyin Author",
                video_urls=[video_url for _, video_url in entries],
                discovery_complete=True,
                media_metadata=metadata,
            )
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
                media_metadata: dict[str, dict[str, Any]] = {}
                api_authors: list[str] = []
                api_has_more: bool | None = None

                def capture_post_response(response: Any) -> None:
                    nonlocal api_has_more
                    if not _is_target_post_response(response.url, profile_id):
                        return
                    try:
                        data = response.json()
                    except Exception:
                        return
                    entries, authors, has_more = _parse_profile_awemes(data, profile_id)
                    values = data.get("aweme_list") or data.get("awemeList") or []
                    for aweme in values:
                        if not isinstance(aweme, dict):
                            continue
                        metadata = _minimal_aweme_metadata(aweme, profile_id)
                        if metadata:
                            media_metadata.setdefault(*metadata)
                    api_authors.extend(authors)
                    for aweme_id, video_url in entries:
                        discovered.setdefault(aweme_id, video_url)
                    if has_more is not None:
                        api_has_more = has_more

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
                    if api_has_more is False:
                        discovery_complete = True
                        break
                    before = len(discovered)
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
                    unchanged_rounds = (
                        unchanged_rounds + 1 if len(discovered) == before else 0
                    )
                    if api_has_more is False:
                        discovery_complete = True
                        break
                    if unchanged_rounds >= stable_rounds:
                        discovery_warning = (
                            "Douyin stopped returning new videos before the profile "
                            "reported completion. The discovered list may be incomplete; "
                            "retry after checking Chrome verification."
                        )
                        break
                if not discovery_complete and not discovery_warning:
                    discovery_warning = (
                        "Douyin reached the discovery safety limit before confirming the "
                        "end of the profile. Retry to continue discovering videos."
                    )

                if not discovered:
                    raise AuthenticationRequiredError(
                        "Douyin did not return verifiable profile-owned video data. Open "
                        "this profile in Chrome, finish verification, then retry.",
                        verification_url=url,
                    )
                return DouyinProfile(
                    author=(api_authors[0] if api_authors else author),
                    video_urls=list(discovered.values()),
                    cookie_fallback_used=cookie_fallback_used,
                    warning=discovery_warning,
                    discovery_complete=discovery_complete,
                    media_metadata=media_metadata,
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
