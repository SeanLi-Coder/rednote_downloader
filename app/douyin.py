from __future__ import annotations

import contextlib
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, unquote, urlsplit

from yt_dlp.cookies import extract_cookies_from_browser

from .browser import chrome_user_agent
from .douyin_signing import (
    fetch_signed_aweme_detail,
    fetch_signed_profile_awemes,
    new_signed_discovery_budget,
)
from .errors import (
    AuthenticationRequiredError,
    DiscoveryError,
    DownloadCancelledError,
    SiteIssueCode,
    TemporaryAccessError,
    is_explicit_rate_limit_message,
)


AUTH_TEXT_PATTERNS = (
    "verify you are human",
    "complete the verification",
    "complete the captcha",
    "请完成下列验证",
    "请完成验证",
    "请拖动滑块",
    "登录后查看",
    "登录后继续",
    "请先登录",
    "当前未登录",
    "登录状态已过期",
    "session expired",
    "not logged in",
)
TRANSIENT_TEXT_PATTERNS = (
    re.compile(r"(?:^|\s)(?:当前)?访问(?:过于)?频繁[，,\s]*(?:请)?稍后"),
    re.compile(r"(?:^|\s)(?:当前)?请求(?:过于)?频繁[，,\s]*(?:请)?稍后"),
    re.compile(r"\btoo many requests\b"),
)
REJECTED_TEXT_PATTERNS = (
    re.compile(r"网络环境存在风险[，,\s]*(?:请)?稍后"),
    re.compile(r"\btry again later\b"),
)
EXPLICIT_AUTH_PATH_MARKERS = ("/captcha", "/login", "/passport/", "/verify")

DOUYIN_QUALITY_FLOOR_SHORT_EDGE = 1080
DOUYIN_QUALITY_FLOOR_LONG_EDGE = 1920
_DOUYIN_VIDEO_DOMAINS = (
    "douyin.com",
    "douyinvod.com",
    "amemv.com",
    "zjcdn.com",
)
_DOUYIN_IMAGE_DOMAINS = ("douyinpic.com",)


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


def _auth_page_issue_code(text: str) -> SiteIssueCode | None:
    lowered = _visible_text(text)
    if any(
        pattern in lowered
        for pattern in (
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
        marker in lowered
        for marker in (
            "登录后查看",
            "登录后继续",
            "请先登录",
            "当前未登录",
            "登录状态已过期",
            "session expired",
            "not logged in",
        )
    ):
        return SiteIssueCode.LOGIN_REQUIRED
    return None


def _looks_like_auth_page(text: str) -> bool:
    return _auth_page_issue_code(text) is not None


def _looks_like_transient_limit(text: str) -> bool:
    lowered = _visible_text(text)
    return is_explicit_rate_limit_message(lowered) or any(
        pattern.search(lowered) for pattern in TRANSIENT_TEXT_PATTERNS
    )


def _looks_like_request_rejected(text: str) -> bool:
    lowered = _visible_text(text)
    return any(pattern.search(lowered) for pattern in REJECTED_TEXT_PATTERNS)


def _is_trusted_douyin_page_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower()
        return (
            parsed.scheme == "https"
            and (hostname == "douyin.com" or hostname.endswith(".douyin.com"))
            and parsed.port in {None, 443}
        )
    except (TypeError, ValueError):
        return False


def _explicit_douyin_auth_url_issue_code(value: str) -> SiteIssueCode | None:
    if not _is_trusted_douyin_page_url(value):
        return None
    try:
        path = unquote(urlsplit(value).path).lower()
    except (TypeError, ValueError):
        return None
    if any(marker in path for marker in ("/captcha", "/verify", "/safe/")):
        return SiteIssueCode.VERIFICATION_REQUIRED
    if "/login" in path or "/passport/" in path:
        return SiteIssueCode.LOGIN_REQUIRED
    return None


def _is_explicit_douyin_auth_url(value: str) -> bool:
    return _explicit_douyin_auth_url_issue_code(value) is not None


def _douyin_authentication_error(
    issue_code: SiteIssueCode,
    verification_url: str,
) -> AuthenticationRequiredError:
    if issue_code == SiteIssueCode.LOGIN_REQUIRED:
        message = (
            "Douyin requires a current Chrome login session. Open the original "
            "task URL in Chrome, log in, then retry. No CAPTCHA was detected."
        )
    else:
        message = (
            "Douyin displayed an explicit CAPTCHA or verification page. Open the "
            "original task URL in Chrome, complete the visible challenge, then retry."
        )
    return AuthenticationRequiredError(
        message,
        verification_url=verification_url,
        issue_code=issue_code,
    )


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


def _is_trusted_https_url(value: str, domains: tuple[str, ...]) -> bool:
    if not isinstance(value, str) or not value or len(value) > 8_192:
        return False
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme != "https"
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or not any(
                hostname == domain or hostname.endswith(f".{domain}")
                for domain in domains
            )
        ):
            return False
        return parsed.port in {None, 443}
    except (TypeError, ValueError):
        return False


def _safe_direct_media_urls(address: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for value in address.get("url_list") or []:
        if not _is_trusted_https_url(value, _DOUYIN_VIDEO_DOMAINS):
            continue
        if value not in result:
            result.append(value)
        if len(result) >= 5:
            break
    return result


def _safe_image_urls(image: dict[str, Any]) -> list[str]:
    """Return only the high-pixel image renditions exposed in ``url_list``.

    ``download_url_list`` is intentionally excluded: Douyin currently returns a
    separate, visibly transformed 1080p download rendition there even when the
    post contains a larger image.
    """

    result: list[str] = []
    for value in image.get("url_list") or []:
        if not _is_trusted_https_url(value, _DOUYIN_IMAGE_DOMAINS):
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
    fallback_dimensions: tuple[int, int] | None = None,
    prefer_fallback_dimensions: bool = False,
) -> dict[str, Any] | None:
    if not isinstance(address, dict):
        return None
    dimensions = (
        fallback_dimensions
        if prefer_fallback_dimensions and fallback_dimensions
        else quality_floor_dimensions([address], cap_full_hd=False)
        or fallback_dimensions
    )
    urls = _safe_direct_media_urls(address)
    if not dimensions or not urls:
        return None
    candidate: dict[str, Any] = {
        "width": dimensions[0],
        "height": dimensions[1],
        "urls": urls,
    }
    video_uri = str(address.get("uri") or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{10,200}", video_uri):
        candidate["video_uri"] = video_uri
    try:
        normalized_bit_rate = int(bit_rate or address.get("bit_rate") or 0)
    except (TypeError, ValueError, OverflowError):
        normalized_bit_rate = 0
    if normalized_bit_rate > 0:
        candidate["bit_rate"] = normalized_bit_rate
    if codec_hint:
        candidate["codec_hint"] = codec_hint
    return candidate


def _highest_live_photo_asset(
    video: Any,
    *,
    index: int,
) -> dict[str, Any] | None:
    if not isinstance(video, dict) or not video:
        return None
    outer_dimensions = quality_floor_dimensions([video], cap_full_hd=False)
    address_values = [
        (video.get("play_addr"), None, None, outer_dimensions),
        (video.get("play_addr_h264"), None, "h264", outer_dimensions),
        (video.get("play_addr_265"), None, "hevc", outer_dimensions),
        (video.get("play_addr_bytevc1"), None, "hevc", outer_dimensions),
    ]
    bit_rates = video.get("bit_rate")
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
            address_values.append(
                (
                    value.get("play_addr"),
                    value.get("bit_rate"),
                    codec_hint,
                    quality_floor_dimensions([value], cap_full_hd=False)
                    or outer_dimensions,
                )
            )
    play_candidates: list[dict[str, Any]] = []
    unusable_play_renditions: list[tuple[tuple[int, int] | None, str, int]] = []
    for address, bit_rate, codec_hint, fallback_dimensions in address_values:
        if not isinstance(address, dict) or not address:
            continue
        candidate = _direct_quality_candidate(
            address,
            bit_rate=bit_rate,
            codec_hint=codec_hint,
            fallback_dimensions=fallback_dimensions,
        )
        if candidate:
            play_candidates.append(candidate)
            continue
        dimensions = (
            quality_floor_dimensions([address], cap_full_hd=False)
            or fallback_dimensions
        )
        raw_uri = str(address.get("uri") or "").strip()
        raw_urls = address.get("url_list")
        advertised = bool(dimensions or raw_uri) or (
            isinstance(raw_urls, list) and bool(raw_urls)
        )
        if not advertised:
            continue
        try:
            declared_bit_rate = int(bit_rate or address.get("bit_rate") or 0)
        except (TypeError, ValueError, OverflowError):
            declared_bit_rate = 0
        unusable_play_renditions.append(
            (
                dimensions,
                raw_uri if re.fullmatch(r"[A-Za-z0-9_-]{10,200}", raw_uri) else "",
                max(0, declared_bit_rate),
            )
        )
    if play_candidates and any(
        not candidate.get("video_uri") for candidate in play_candidates
    ):
        return None
    candidates = list(play_candidates)
    visible_play_uris = {
        value
        for address, _, _, _ in address_values
        if isinstance(address, dict)
        and re.fullmatch(
            r"[A-Za-z0-9_-]{10,200}",
            value := str(address.get("uri") or "").strip(),
        )
    }
    has_watermark = video.get("has_watermark")
    explicitly_unwatermarked = has_watermark is False or (
        type(has_watermark) is int and has_watermark == 0
    )
    if not candidates and outer_dimensions and explicitly_unwatermarked:
        download_addr = video.get("download_addr")
        download_dimensions = outer_dimensions
        if isinstance(download_addr, dict):
            try:
                download_width = int(download_addr.get("width") or 0)
            except (TypeError, ValueError, OverflowError):
                download_width = 0
            if download_width > 0 and outer_dimensions[0] > 0:
                download_dimensions = (
                    download_width,
                    max(
                        1,
                        round(
                            download_width * outer_dimensions[1] / outer_dimensions[0]
                        ),
                    ),
                )
        download_candidate = _direct_quality_candidate(
            download_addr,
            codec_hint="h264",
            fallback_dimensions=download_dimensions,
            prefer_fallback_dimensions=True,
        )
        download_uri = (
            str(download_candidate.get("video_uri") or "").strip()
            if download_candidate
            else ""
        )
        if (
            download_candidate
            and download_uri
            and (not visible_play_uris or visible_play_uris == {download_uri})
        ):
            candidates = [download_candidate]
    if not candidates:
        return None
    candidate_uris = {
        str(candidate.get("video_uri") or "").strip()
        for candidate in candidates
        if candidate.get("video_uri")
    }
    if len(candidate_uris) != 1:
        return None
    video_uri = next(iter(candidate_uris))
    if visible_play_uris and visible_play_uris != {video_uri}:
        return None
    best_candidate = max(
        candidates,
        key=lambda value: (
            value["width"] * value["height"],
            int(value.get("bit_rate") or 0),
        ),
    )
    best_pixels = best_candidate["width"] * best_candidate["height"]
    best_bit_rate = int(best_candidate.get("bit_rate") or 0)
    for dimensions, declared_uri, declared_bit_rate in unusable_play_renditions:
        if not dimensions or declared_uri != video_uri:
            return None
        declared_pixels = dimensions[0] * dimensions[1]
        if declared_pixels > best_pixels or (
            declared_pixels == best_pixels and declared_bit_rate > best_bit_rate
        ):
            return None
    candidates.sort(
        key=lambda value: (
            value["width"] * value["height"],
            int(value.get("bit_rate") or 0),
        ),
        reverse=True,
    )
    highest_pixels = candidates[0]["width"] * candidates[0]["height"]
    highest_candidates = [
        candidate
        for candidate in candidates
        if candidate["width"] * candidate["height"] == highest_pixels
    ]
    unique_highest_candidates: list[dict[str, Any]] = []
    seen_renditions: set[tuple[int, int, tuple[str, ...]]] = set()
    for candidate in highest_candidates:
        signature = (
            candidate["width"],
            candidate["height"],
            tuple(candidate["urls"]),
        )
        if signature in seen_renditions:
            continue
        seen_renditions.add(signature)
        unique_highest_candidates.append(candidate)
        if len(unique_highest_candidates) >= 4:
            break
    urls: list[str] = []
    for candidate in unique_highest_candidates:
        if candidate.get("video_uri") != video_uri:
            continue
        for value in candidate["urls"]:
            if value not in urls:
                urls.append(value)
            if len(urls) >= 5:
                break
        if len(urls) >= 5:
            break
    if not urls:
        return None
    asset_width = candidates[0]["width"]
    asset_height = candidates[0]["height"]
    if (
        outer_dimensions
        and outer_dimensions[0] * outer_dimensions[1] > asset_width * asset_height
    ):
        asset_width, asset_height = outer_dimensions
    result: dict[str, Any] = {
        "index": index,
        "width": asset_width,
        "height": asset_height,
        "candidates": urls,
        "video_uri": video_uri,
        "direct_candidates": unique_highest_candidates,
    }
    duration_ms = video.get("duration")
    if isinstance(duration_ms, int) and duration_ms > 0:
        result["duration_ms"] = duration_ms
    return result


def _image_asset(image: Any, *, index: int) -> dict[str, Any] | None:
    if not isinstance(image, dict):
        return None
    dimensions = quality_floor_dimensions([image], cap_full_hd=False)
    candidates = _safe_image_urls(image)
    if not dimensions or not candidates:
        return None
    return {
        "index": index,
        "width": dimensions[0],
        "height": dimensions[1],
        "candidates": candidates,
    }


def _metadata_title(
    aweme: dict[str, Any],
    media_kind: str,
    media_id: str,
) -> str:
    for key in ("desc", "item_title", "preview_title"):
        value = aweme.get(key)
        if isinstance(value, str) and value.strip():
            title = re.sub(r"\s+", " ", value).strip()[:240]
            if title != media_id and not title.isdigit():
                return title
    return f"Untitled Douyin {media_kind}"


def _valid_cached_asset(asset: Any, *, image: bool) -> bool:
    if not isinstance(asset, dict) or type(asset.get("index")) is not int:
        return False
    if asset["index"] <= 0:
        return False
    try:
        width = int(asset.get("width") or 0)
        height = int(asset.get("height") or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    if not (0 < width <= 16_384 and 0 < height <= 16_384):
        return False
    candidates = asset.get("candidates")
    if not isinstance(candidates, list) or not candidates or len(candidates) > 5:
        return False
    domains = _DOUYIN_IMAGE_DOMAINS if image else _DOUYIN_VIDEO_DOMAINS
    if not all(_is_trusted_https_url(value, domains) for value in candidates):
        return False
    if not image:
        video_uri = str(asset.get("video_uri") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", video_uri):
            return False
        duration_ms = asset.get("duration_ms")
        if duration_ms is not None and (
            type(duration_ms) is not int or duration_ms <= 0
        ):
            return False
        direct_candidates = asset.get("direct_candidates")
        if not (
            isinstance(direct_candidates, list)
            and 0 < len(direct_candidates) <= 4
            and all(
                _valid_direct_quality_candidate(value, video_uri)
                for value in direct_candidates
            )
        ):
            return False
    return True


def _valid_direct_quality_candidate(value: Any, video_uri: str) -> bool:
    if not isinstance(value, dict) or value.get("video_uri") != video_uri:
        return False
    try:
        width = int(value.get("width") or 0)
        height = int(value.get("height") or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    urls = value.get("urls")
    bit_rate = value.get("bit_rate")
    if bit_rate is not None and (type(bit_rate) is not int or bit_rate <= 0):
        return False
    codec_hint = value.get("codec_hint")
    if codec_hint is not None and codec_hint not in {
        "h264",
        "hevc",
        "h265",
        "vvc",
        "h266",
        "bytevc2",
    }:
        return False
    return (
        0 < width <= 16_384
        and 0 < height <= 16_384
        and isinstance(urls, list)
        and 0 < len(urls) <= 5
        and all(_is_trusted_https_url(url, _DOUYIN_VIDEO_DOMAINS) for url in urls)
    )


def is_complete_profile_media_metadata(
    cached: Any,
    media_id: str,
    owner_id: str,
) -> bool:
    """Return whether cached profile metadata is identity-bound and downloadable."""

    if (
        not isinstance(cached, dict)
        or not isinstance(media_id, str)
        or not isinstance(owner_id, str)
        or not media_id.isdigit()
        or not owner_id
        or str(cached.get("media_id") or "").strip() != media_id
        or str(cached.get("owner_id") or "").strip() != owner_id
    ):
        return False
    media_kind = cached.get("media_kind")
    if media_kind == "video":
        video_uri = str(cached.get("video_uri") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", video_uri):
            return False
        direct_candidates = cached.get("direct_candidates")
        return (
            isinstance(direct_candidates, list)
            and 0 < len(direct_candidates) <= 4
            and all(
                _valid_direct_quality_candidate(value, video_uri)
                for value in direct_candidates
            )
        )
    if media_kind != "image":
        return False
    image_assets = cached.get("image_assets")
    if not isinstance(image_assets, list) or not image_assets:
        return False
    if not all(_valid_cached_asset(value, image=True) for value in image_assets):
        return False
    image_indexes = [value["index"] for value in image_assets]
    if len(set(image_indexes)) != len(image_indexes):
        return False
    live_assets = cached.get("live_photo_assets")
    fallback_indexes = cached.get("live_photo_static_fallback_indexes")
    if fallback_indexes is None:
        normalized_fallback_indexes: list[int] = []
    elif (
        not isinstance(fallback_indexes, list)
        or not fallback_indexes
        or any(type(value) is not int or value <= 0 for value in fallback_indexes)
        or len(set(fallback_indexes)) != len(fallback_indexes)
        or not set(fallback_indexes).issubset(set(image_indexes))
    ):
        return False
    else:
        normalized_fallback_indexes = fallback_indexes
    if live_assets is None:
        return True
    if not isinstance(live_assets, list) or not live_assets:
        return False
    if not all(_valid_cached_asset(value, image=False) for value in live_assets):
        return False
    live_indexes = [value["index"] for value in live_assets]
    return (
        len(set(live_indexes)) == len(live_indexes)
        and set(live_indexes).issubset(set(image_indexes))
        and set(live_indexes).isdisjoint(normalized_fallback_indexes)
    )


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
    images = aweme.get("images")
    is_image_post = str(aweme.get("aweme_type") or "") == "68" or (
        isinstance(images, list) and bool(images)
    )
    if is_image_post:
        if not isinstance(images, list) or not images:
            return None
        image_assets: list[dict[str, Any]] = []
        live_photo_assets: list[dict[str, Any]] = []
        static_fallback_indexes: list[int] = []
        for index, image in enumerate(images, start=1):
            asset = _image_asset(image, index=index)
            if not asset:
                return None
            image_assets.append(asset)
            nested_video = image.get("video") if isinstance(image, dict) else None
            if isinstance(nested_video, dict) and nested_video:
                live_asset = _highest_live_photo_asset(nested_video, index=index)
                if live_asset:
                    live_photo_assets.append(live_asset)
                else:
                    static_fallback_indexes.append(index)
        metadata: dict[str, Any] = {
            "media_id": aweme_id,
            "owner_id": owner_id,
            "media_kind": "image",
            "image_assets": image_assets,
            "title": _metadata_title(aweme, "image", aweme_id),
        }
        if live_photo_assets:
            metadata["live_photo_assets"] = live_photo_assets
        if static_fallback_indexes:
            metadata["live_photo_static_fallback_indexes"] = static_fallback_indexes
        create_time = aweme.get("create_time")
        if isinstance(create_time, int) and create_time > 0:
            metadata["create_time"] = create_time
        nickname = author.get("nickname")
        if isinstance(nickname, str) and nickname.strip():
            metadata["author"] = nickname.strip()
        if not is_complete_profile_media_metadata(metadata, aweme_id, profile_id):
            return None
        return aweme_id, metadata

    video = aweme.get("video")
    if not isinstance(video, dict):
        return None
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
    video_uris = {
        candidate
        for address in addresses
        if isinstance(address, dict)
        and re.fullmatch(
            r"[A-Za-z0-9_-]{10,200}",
            candidate := str(address.get("uri") or "").strip(),
        )
    }
    if len(video_uris) != 1:
        return None
    video_uri = next(iter(video_uris))

    metadata: dict[str, Any] = {
        "media_id": aweme_id,
        "owner_id": owner_id,
        "media_kind": "video",
        "video_uri": video_uri,
        "title": _metadata_title(aweme, "video", aweme_id),
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
    if any(candidate.get("video_uri") != video_uri for candidate in direct_candidates):
        return None
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
    nickname = author.get("nickname")
    if isinstance(nickname, str) and nickname.strip():
        metadata["author"] = nickname.strip()
    if not is_complete_profile_media_metadata(metadata, aweme_id, profile_id):
        return None
    return aweme_id, metadata


def verified_aweme_metadata(
    aweme: dict[str, Any],
    media_id: str,
    *,
    expected_profile_id: str | None = None,
) -> dict[str, Any] | None:
    """Return complete metadata only when an aweme is bound to the target item."""
    if not media_id.isdigit():
        return None
    aweme_id = str(aweme.get("aweme_id") or aweme.get("awemeId") or "").strip()
    author = aweme.get("author")
    if aweme_id != media_id or not isinstance(author, dict):
        return None
    owner_id = str(author.get("sec_uid") or author.get("secUid") or "").strip()
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", owner_id)
        or expected_profile_id is not None
        and owner_id != expected_profile_id
    ):
        return None
    parsed = _minimal_aweme_metadata(aweme, owner_id)
    if not parsed or parsed[0] != media_id:
        return None
    metadata = parsed[1]
    if not is_complete_profile_media_metadata(metadata, media_id, owner_id):
        return None
    return metadata


def discover_item_metadata_from_profile(
    profile_id: str,
    media_id: str,
    *,
    cookie_profile: str | None = None,
    prefer_exact_detail: bool = False,
    should_cancel: Callable[[], bool] | None = None,
    status_callback: Callable[[str], None] | None = None,
    progress_budget: Any | None = None,
) -> dict[str, Any] | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", profile_id):
        return None
    if not media_id.isdigit():
        return None
    profile_url = f"https://www.douyin.com/user/{profile_id}"
    item_url = f"https://www.douyin.com/video/{media_id}"
    progress_budget = progress_budget or new_signed_discovery_budget()
    detail_error: Exception | None = None
    if prefer_exact_detail:
        try:
            detail = fetch_signed_aweme_detail(
                media_id,
                verification_url=item_url,
                expected_sec_uid=profile_id,
                cookie_profile=cookie_profile,
                should_cancel=should_cancel,
                status_callback=status_callback,
                progress_budget=progress_budget,
            )
        except (AuthenticationRequiredError, DownloadCancelledError):
            raise
        except TemporaryAccessError as exc:
            detail_error = exc
        else:
            metadata = verified_aweme_metadata(
                detail,
                media_id,
                expected_profile_id=profile_id,
            )
            if metadata:
                return metadata
            raise DiscoveryError(
                "Douyin returned the requested item detail without complete, "
                "verified media metadata. Diagnostic code: detail-metadata-incomplete.",
                issue_code=SiteIssueCode.SITE_RESPONSE_CHANGED,
                diagnostic_code="detail-metadata-incomplete",
            )

    awemes = fetch_signed_profile_awemes(
        profile_url,
        profile_id,
        target_aweme_id=media_id,
        cookie_profile=cookie_profile,
        should_cancel=should_cancel,
        status_callback=status_callback,
        progress_budget=progress_budget,
    )
    for aweme in awemes:
        if str(aweme.get("aweme_id") or "").strip() != media_id:
            continue
        metadata = verified_aweme_metadata(
            aweme,
            media_id,
            expected_profile_id=profile_id,
        )
        if not metadata:
            raise TemporaryAccessError(
                "Douyin returned the requested profile item without complete, "
                "verified media metadata. Retry after a short wait.",
                issue_code=SiteIssueCode.SITE_RESPONSE_CHANGED,
            )
        return metadata
    if detail_error is not None:
        raise detail_error
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


def _raise_if_profile_discovery_cancelled(
    should_cancel: Callable[[], bool] | None,
) -> None:
    if should_cancel and should_cancel():
        raise DownloadCancelledError("Task cancelled")


def _profile_discovery_budget_remaining(
    progress_budget: Any,
    should_cancel: Callable[[], bool] | None,
) -> float:
    _raise_if_profile_discovery_cancelled(should_cancel)
    try:
        remaining = float(progress_budget.remaining_seconds())
    except Exception as exc:
        _raise_if_profile_discovery_cancelled(should_cancel)
        raise TemporaryAccessError(
            "Douyin profile discovery stopped after 120 seconds without new "
            "verified profile media. Retry after a short wait; Chrome verification "
            "is not required.",
            issue_code=SiteIssueCode.NETWORK_ERROR,
        ) from exc
    _raise_if_profile_discovery_cancelled(should_cancel)
    return remaining


def _profile_discovery_timeout_ms(
    progress_budget: Any,
    requested_ms: int,
    should_cancel: Callable[[], bool] | None,
) -> int:
    remaining = _profile_discovery_budget_remaining(
        progress_budget,
        should_cancel,
    )
    remaining_ms = int(remaining * 1_000)
    if remaining_ms < 1:
        raise TemporaryAccessError(
            "Douyin profile discovery stopped after 120 seconds without new "
            "verified profile media. Retry after a short wait; Chrome verification "
            "is not required.",
            issue_code=SiteIssueCode.NETWORK_ERROR,
        )
    return min(max(int(requested_ms), 1), remaining_ms)


def _profile_discovery_status(
    status_callback: Callable[[str], None] | None,
    message: str,
) -> None:
    if status_callback:
        status_callback(message)


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
    status_callback: Callable[[str], None] | None = None,
) -> DouyinProfile:
    profile_id = _profile_id(url)
    if not profile_id:
        raise DiscoveryError("The Douyin profile URL has no profile identifier")
    progress_budget = new_signed_discovery_budget()
    signed_fallback_reason = "direct-browser-mode"
    if use_browser_cookies:
        try:
            signed_awemes = fetch_signed_profile_awemes(
                url,
                profile_id,
                cookie_profile=cookie_profile,
                should_cancel=should_cancel,
                status_callback=status_callback,
                progress_budget=progress_budget,
            )
        except AuthenticationRequiredError:
            signed_fallback_reason = "authentication-confirmation"
        except DiscoveryError:
            signed_fallback_reason = "signed-integrity"
        else:
            signed_data = {"aweme_list": signed_awemes, "has_more": False}
            entries, authors, _ = _parse_profile_awemes(signed_data, profile_id)
            metadata: dict[str, dict[str, Any]] = {}
            entry_ids = {aweme_id for aweme_id, _ in entries}
            incomplete_ids: list[str] = []
            for aweme in signed_awemes:
                aweme_id = str(aweme.get("aweme_id") or "").strip()
                if aweme_id not in entry_ids:
                    continue
                value = _minimal_aweme_metadata(aweme, profile_id)
                if not value or not is_complete_profile_media_metadata(
                    value[1], aweme_id, profile_id
                ):
                    incomplete_ids.append(aweme_id)
                    continue
                metadata[value[0]] = value[1]
            if incomplete_ids or len(metadata) != len(entries):
                first_id = (incomplete_ids or sorted(entry_ids - metadata.keys()))[0]
                raise TemporaryAccessError(
                    "Douyin returned profile media without complete verified metadata "
                    f"(first incomplete item: {first_id}). Retry after a short wait."
                )
            if not entries:
                raise DiscoveryError(
                    "Douyin returned no complete video or image posts for this profile"
                )
            return DouyinProfile(
                author=authors[0] if authors else "Douyin Author",
                video_urls=[video_url for _, video_url in entries],
                discovery_complete=True,
                media_metadata=metadata,
            )
    remaining = _profile_discovery_budget_remaining(progress_budget, should_cancel)
    _profile_discovery_status(
        status_callback,
        "Starting bounded Douyin browser profile fallback "
        f"(reason: {signed_fallback_reason}; {max(1, int(remaining))}s remaining)",
    )
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise DiscoveryError(
            "Playwright is required for Douyin profile discovery",
            issue_code=SiteIssueCode.LOCAL_CONFIGURATION,
        ) from exc

    browser_cookies: list[dict[str, Any]] = []
    cookie_fallback_used = False
    if use_browser_cookies:
        _profile_discovery_budget_remaining(progress_budget, should_cancel)
        try:
            browser_cookies = _cookie_jar_to_playwright(
                _extract_cookies(cookie_profile)
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
        _profile_discovery_budget_remaining(progress_budget, should_cancel)

    try:
        with sync_playwright() as playwright:
            _profile_discovery_status(
                status_callback,
                "Launching Douyin browser fallback",
            )
            browser = playwright.chromium.launch(
                channel="chrome",
                headless=True,
                timeout=_profile_discovery_timeout_ms(
                    progress_budget,
                    navigation_timeout_ms,
                    should_cancel,
                ),
            )
            try:
                _profile_discovery_budget_remaining(
                    progress_budget,
                    should_cancel,
                )
                browser_version = browser.version
                context = browser.new_context(
                    locale="zh-CN",
                    viewport={"width": 1440, "height": 1100},
                    user_agent=chrome_user_agent(browser_version),
                )
                _profile_discovery_budget_remaining(
                    progress_budget,
                    should_cancel,
                )
                if browser_cookies:
                    context.add_cookies(browser_cookies)
                    _profile_discovery_budget_remaining(
                        progress_budget,
                        should_cancel,
                    )
                discovered: dict[str, str] = {}
                media_metadata: dict[str, dict[str, Any]] = {}
                incomplete_media_ids: set[str] = set()
                api_authors: list[str] = []
                api_has_more: bool | None = None
                callback_budget_error: Exception | None = None

                def check_browser_budget() -> float:
                    _raise_if_profile_discovery_cancelled(should_cancel)
                    if callback_budget_error is not None:
                        raise callback_budget_error
                    return _profile_discovery_budget_remaining(
                        progress_budget,
                        should_cancel,
                    )

                def capture_finished_post_request(request: Any) -> None:
                    nonlocal api_has_more, callback_budget_error
                    try:
                        check_browser_budget()
                    except (DownloadCancelledError, TemporaryAccessError) as exc:
                        callback_budget_error = exc
                        return
                    try:
                        response = request.response()
                    except Exception:
                        return
                    if response is None:
                        return
                    if not _is_target_post_response(response.url, profile_id):
                        return
                    try:
                        check_browser_budget()
                    except (DownloadCancelledError, TemporaryAccessError) as exc:
                        callback_budget_error = exc
                        return
                    try:
                        data = response.json()
                    except Exception:
                        return
                    raw_values = data.get("aweme_list")
                    if raw_values is None:
                        raw_values = data.get("awemeList")
                    if not isinstance(raw_values, list):
                        return
                    response_identity = str(
                        data.get("sec_uid") or data.get("sec_user_id") or ""
                    ).strip()
                    if response_identity and response_identity != profile_id:
                        return
                    entries, authors, has_more = _parse_profile_awemes(data, profile_id)
                    values = raw_values
                    if values:
                        for aweme in values:
                            if not isinstance(aweme, dict):
                                return
                            author = aweme.get("author")
                            if not isinstance(author, dict):
                                return
                            owner_id = str(
                                author.get("sec_uid") or author.get("secUid") or ""
                            ).strip()
                            if owner_id != profile_id:
                                return
                        if not entries:
                            return
                    elif has_more is not False or response_identity != profile_id:
                        return
                    entries_by_id = dict(entries)
                    verified_metadata: dict[str, dict[str, Any]] = {}
                    for aweme in values:
                        if not isinstance(aweme, dict):
                            continue
                        aweme_id = str(
                            aweme.get("aweme_id") or aweme.get("awemeId") or ""
                        ).strip()
                        if aweme_id not in entries_by_id:
                            continue
                        metadata = _minimal_aweme_metadata(aweme, profile_id)
                        if not metadata or not is_complete_profile_media_metadata(
                            metadata[1], aweme_id, profile_id
                        ):
                            incomplete_media_ids.add(aweme_id)
                            continue
                        verified_metadata.setdefault(*metadata)
                        incomplete_media_ids.discard(aweme_id)
                    try:
                        check_browser_budget()
                    except (DownloadCancelledError, TemporaryAccessError) as exc:
                        callback_budget_error = exc
                        return
                    media_metadata.update(
                        {
                            aweme_id: metadata
                            for aweme_id, metadata in verified_metadata.items()
                            if aweme_id not in media_metadata
                        }
                    )
                    api_authors.extend(authors)
                    before = len(discovered)
                    for aweme_id, video_url in entries:
                        if aweme_id in media_metadata:
                            discovered.setdefault(aweme_id, video_url)
                    if has_more is not None:
                        api_has_more = has_more
                    added = len(discovered) - before
                    if added:
                        try:
                            progress_budget.refresh()
                            remaining = check_browser_budget()
                        except (
                            DownloadCancelledError,
                            TemporaryAccessError,
                        ) as exc:
                            callback_budget_error = exc
                            return
                        except Exception as exc:
                            callback_budget_error = TemporaryAccessError(
                                "Douyin browser fallback could not update its bounded "
                                "progress state. Retry after a short wait; Chrome "
                                "verification is not required."
                            )
                            callback_budget_error.__cause__ = exc
                            return
                        _profile_discovery_status(
                            status_callback,
                            "Douyin browser fallback added "
                            f"{added} verified item(s) ({len(discovered)} total; "
                            f"{max(1, int(remaining))}s remaining)",
                        )

                page = context.new_page()
                check_browser_budget()
                page.on("requestfinished", capture_finished_post_request)
                _profile_discovery_status(
                    status_callback,
                    "Opening the Douyin profile in the bounded browser fallback",
                )
                page.goto(
                    url,
                    wait_until="commit",
                    timeout=_profile_discovery_timeout_ms(
                        progress_budget,
                        navigation_timeout_ms,
                        should_cancel,
                    ),
                )
                _raise_if_profile_discovery_cancelled(should_cancel)
                auth_issue = _explicit_douyin_auth_url_issue_code(page.url)
                if auth_issue is not None:
                    raise _douyin_authentication_error(auth_issue, url)
                if not _is_trusted_douyin_page_url(page.url):
                    raise DiscoveryError(
                        "Douyin profile discovery redirected outside the trusted "
                        "Douyin origin"
                    )
                check_browser_budget()
                page.wait_for_timeout(
                    _profile_discovery_timeout_ms(
                        progress_budget,
                        4_000,
                        should_cancel,
                    )
                )
                _raise_if_profile_discovery_cancelled(should_cancel)

                auth_issue = _explicit_douyin_auth_url_issue_code(page.url)
                if auth_issue is not None:
                    raise _douyin_authentication_error(auth_issue, url)
                if not _is_trusted_douyin_page_url(page.url):
                    raise DiscoveryError(
                        "Douyin profile discovery redirected outside the trusted "
                        "Douyin origin"
                    )
                check_browser_budget()
                if page.locator("body").count() == 0 and not discovered:
                    raise TemporaryAccessError(
                        "Douyin profile discovery temporarily returned a blank browser "
                        "response. Retry after a short wait; Chrome verification was "
                        "not requested."
                    )
                body_timeout_ms = _profile_discovery_timeout_ms(
                    progress_budget,
                    5_000,
                    should_cancel,
                )
                try:
                    body_text = page.locator("body").inner_text(timeout=body_timeout_ms)
                except Exception:
                    check_browser_budget()
                    body_text = page.content()
                _raise_if_profile_discovery_cancelled(should_cancel)
                auth_issue = _auth_page_issue_code(body_text)
                if auth_issue is not None:
                    raise _douyin_authentication_error(auth_issue, url)
                if _looks_like_transient_limit(body_text):
                    raise TemporaryAccessError(
                        "Douyin profile discovery was temporarily rate-limited. Retry "
                        "after a short wait; Chrome verification is not required."
                    )
                if _looks_like_request_rejected(body_text):
                    raise TemporaryAccessError(
                        "Douyin temporarily rejected the profile request without "
                        "showing a CAPTCHA or login page. Retry after a short wait.",
                        issue_code=SiteIssueCode.REQUEST_REJECTED,
                    )
                check_browser_budget()

                author = (
                    (api_authors[0] if api_authors else None)
                    or _pick_author(page)
                    or "Douyin Author"
                )
                check_browser_budget()
                unchanged_rounds = 0
                discovery_complete = False
                discovery_warning: str | None = None
                for scroll_offset in range(max_scrolls):
                    remaining = check_browser_budget()
                    _profile_discovery_status(
                        status_callback,
                        "Scanning Douyin browser fallback "
                        f"round {scroll_offset + 1}/{max_scrolls} "
                        f"({len(discovered)} verified item(s); "
                        f"{max(1, int(remaining))}s remaining)",
                    )
                    if api_has_more is False:
                        discovery_complete = True
                        break
                    before = len(discovered)
                    page.mouse.wheel(0, 5_000)
                    _raise_if_profile_discovery_cancelled(should_cancel)
                    auth_issue = _explicit_douyin_auth_url_issue_code(page.url)
                    if auth_issue is not None:
                        raise _douyin_authentication_error(auth_issue, url)
                    if not _is_trusted_douyin_page_url(page.url):
                        raise DiscoveryError(
                            "Douyin profile discovery redirected outside the trusted "
                            "Douyin origin"
                        )
                    check_browser_budget()
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
                    _raise_if_profile_discovery_cancelled(should_cancel)
                    auth_issue = _explicit_douyin_auth_url_issue_code(page.url)
                    if auth_issue is not None:
                        raise _douyin_authentication_error(auth_issue, url)
                    if not _is_trusted_douyin_page_url(page.url):
                        raise DiscoveryError(
                            "Douyin profile discovery redirected outside the trusted "
                            "Douyin origin"
                        )
                    check_browser_budget()
                    page.wait_for_timeout(
                        _profile_discovery_timeout_ms(
                            progress_budget,
                            1_200,
                            should_cancel,
                        )
                    )
                    _raise_if_profile_discovery_cancelled(should_cancel)
                    auth_issue = _explicit_douyin_auth_url_issue_code(page.url)
                    if auth_issue is not None:
                        raise _douyin_authentication_error(auth_issue, url)
                    if not _is_trusted_douyin_page_url(page.url):
                        raise DiscoveryError(
                            "Douyin profile discovery redirected outside the trusted "
                            "Douyin origin"
                        )
                    updated_body_timeout_ms = _profile_discovery_timeout_ms(
                        progress_budget,
                        2_000,
                        should_cancel,
                    )
                    try:
                        updated_body = page.locator("body").inner_text(
                            timeout=updated_body_timeout_ms
                        )
                    except Exception:
                        check_browser_budget()
                        updated_body = page.content()
                    _raise_if_profile_discovery_cancelled(should_cancel)
                    auth_issue = _auth_page_issue_code(updated_body)
                    if auth_issue is not None:
                        raise _douyin_authentication_error(auth_issue, url)
                    if _looks_like_transient_limit(updated_body):
                        raise TemporaryAccessError(
                            "Douyin profile discovery was temporarily rate-limited. "
                            "Retry after a short wait; Chrome verification is not "
                            "required."
                        )
                    if _looks_like_request_rejected(updated_body):
                        raise TemporaryAccessError(
                            "Douyin temporarily rejected the profile request without "
                            "showing a CAPTCHA or login page. Retry after a short wait.",
                            issue_code=SiteIssueCode.REQUEST_REJECTED,
                        )
                    check_browser_budget()
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
                            "retry after a short wait."
                        )
                        break
                if not discovery_complete and not discovery_warning:
                    discovery_warning = (
                        "Douyin reached the discovery safety limit before confirming the "
                        "end of the profile. Retry to continue discovering videos."
                    )

                check_browser_budget()
                if incomplete_media_ids:
                    raise TemporaryAccessError(
                        "Douyin browser discovery returned media without complete "
                        "verified metadata (first incomplete item: "
                        f"{sorted(incomplete_media_ids)[0]}). Retry after a short wait."
                    )
                if not discovered:
                    raise TemporaryAccessError(
                        "Douyin profile discovery temporarily returned no verified "
                        "profile-owned media. Retry after a short wait; Chrome "
                        "verification was not requested."
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
    except (
        AuthenticationRequiredError,
        DownloadCancelledError,
        TemporaryAccessError,
    ):
        raise
    except PlaywrightError as exc:
        message = str(exc)
        lowered_message = message.lower()
        if _looks_like_transient_limit(message):
            raise TemporaryAccessError(
                "Douyin profile discovery was explicitly rate-limited. Retry after "
                "a short wait; Chrome verification is not required.",
                issue_code=SiteIssueCode.RATE_LIMITED,
            ) from exc
        if _looks_like_request_rejected(message):
            raise TemporaryAccessError(
                "Douyin temporarily rejected browser profile discovery without "
                "showing a CAPTCHA or login page. Retry after a short wait.",
                issue_code=SiteIssueCode.REQUEST_REJECTED,
            ) from exc
        if "timeout" in lowered_message or "timed out" in lowered_message:
            raise TemporaryAccessError(
                "Douyin browser profile discovery timed out. Check the network and "
                "retry; no CAPTCHA or rate-limit response was detected.",
                issue_code=SiteIssueCode.NETWORK_ERROR,
            ) from exc
        if any(
            marker in lowered_message
            for marker in (
                "executable doesn't exist",
                "executable does not exist",
                "browser was not found",
            )
        ):
            raise DiscoveryError(
                "The Playwright browser executable required for Douyin discovery "
                "is not installed. Run the project setup command and restart it.",
                issue_code=SiteIssueCode.LOCAL_CONFIGURATION,
            ) from exc
        raise DiscoveryError(
            f"Douyin browser discovery failed: {message}",
            issue_code=SiteIssueCode.SITE_RESPONSE_CHANGED,
        ) from exc
