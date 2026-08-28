from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit, urlunsplit

from yt_dlp import YoutubeDL
from yt_dlp.dependencies import requests, urllib3
from yt_dlp.extractor.tiktok import DouyinIE
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
from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.utils import DownloadCancelled, DownloadError

from .douyin import discover_profile as discover_douyin_profile
from .douyin import discover_item_metadata_from_profile
from .douyin import is_complete_profile_media_metadata
from .douyin import quality_floor_dimensions
from .douyin_signing import fetch_signed_aweme_detail
from .errors import (
    AuthenticationRequiredError,
    DiscoveryError,
    DownloadCancelledError,
    MediaDownloadError,
    TemporaryAccessError,
)
from .models import DownloadItem, MediaType, Platform, SourceKind, TransferProgress
from .platforms import identify_url
from .xiaohongshu import RemoteAsset, discover_profile as discover_xhs_profile
from .xiaohongshu import is_trusted_xiaohongshu_asset_url
from .xiaohongshu import parse_note as parse_xhs_note


MAX_TITLE_BYTES = 180
MAX_MEDIA_ID_BYTES = 64
MAX_FILENAME_COMPONENT_BYTES = 255
RESERVED_EXTENSION_BYTES = 16
DOUYIN_PROBE_RATIOS = ("default", "4k", "2k", "1080p", "720p")
DOUYIN_PROBE_BYTES = 256 * 1024
DOUYIN_DEFAULT_PROBE_HOST = "api-play-hl.amemv.com"
DOUYIN_RATIO_PROBE_HOST = "api-play.amemv.com"
DOUYIN_PROBE_HTTP_TIMEOUT_SECONDS = 10.0
DOUYIN_PROBE_ATTEMPTS = 3
DOUYIN_PROBE_RETRY_BASE_SECONDS = 1.0
DOUYIN_TRANSFER_ATTEMPTS = 3
DOUYIN_FFPROBE_PIPE_TIMEOUT_SECONDS = 3.0
DOUYIN_FFPROBE_FILE_TIMEOUT_SECONDS = 15.0
DOUYIN_MAX_PROBE_FILE_BYTES = 8 * 1024 * 1024 * 1024
DOUYIN_PROCESS_POLL_SECONDS = 0.1
DOUYIN_MEDIA_PROBE_LOCK = threading.Lock()
DOUYIN_MAX_MEDIA_REDIRECTS = 5
DOUYIN_MAX_DURATION_MS = 7 * 24 * 60 * 60 * 1_000
DOUYIN_DIRECT_MEDIA_DOMAINS = (
    "douyin.com",
    "douyinvod.com",
    "amemv.com",
    "zjcdn.com",
)
DOUYIN_REGIONAL_MEDIA_DOMAINS = (
    *DOUYIN_DIRECT_MEDIA_DOMAINS,
    "douyincdn.com",
    "idouyinvod.com",
    "pstatp.com",
)
FFPROBE_FALLBACK_PATHS = (
    "/opt/homebrew/bin/ffprobe",
    "/usr/local/bin/ffprobe",
)
DOUYIN_MEDIA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Mobile Safari/537.36"
    ),
    "Referer": "https://www.douyin.com/",
    "Accept": "*/*",
}
OUTPUT_TEMPLATE = (
    "%(upload_date>%Y-%m-%d,release_date>%Y-%m-%d|Unknown-Date)s-"
    "%(_filename_title,title|Untitled)s "
    "[%(_filename_media_id,id|unknown-id)s].%(ext)s"
)
AUTH_ERROR_MARKERS = (
    "captcha",
    "sign in",
    "login required",
    "log in",
    "authentication required",
    "confirm you're not a bot",
    "confirm you’re not a bot",
    "verify you are human",
    "fresh cookies",
    "cookies are needed",
    "cookies-from-browser",
    "please verify",
    "security verification",
    "验证码",
    "安全验证",
    "登录后",
    "请先登录",
)
TEMPORARY_ACCESS_MARKERS = (
    "访问频繁",
    "请求频繁",
    "风控",
    "too many requests",
    "rate limit",
    "rate-limit",
    "temporarily limited",
    "http error 429",
    "status code 429",
)
COOKIE_LOAD_ERROR_MARKERS = (
    "cookie database",
    "could not copy chrome",
    "could not find chrome cookies",
    "failed to load cookies",
    "failed to decrypt",
    "could not decrypt",
    "keyring",
)
COOKIE_FALLBACK_WARNING = (
    "Chrome cookies could not be read, so anonymous access was used. "
    "The profile may be incomplete and restricted high-quality formats may be missing."
)
COOKIE_ACCESS_MESSAGE = (
    "Chrome cookies could not be read. Fully quit Chrome and retry, approve any "
    "system cookie-access prompt, or disable Chrome Cookie in settings to continue "
    "explicitly without login and create a new task. Opening a verification page is "
    "not required."
)
DOUYIN_ITEM_EXPANSION_MESSAGE = (
    "Douyin returned an uploader profile instead of the requested video. "
    "The unexpected profile entries were blocked. Retry the original video to "
    "rediscover it; Chrome verification was not requested because no explicit "
    "CAPTCHA or login response was shown."
)
DOUYIN_EMPTY_DETAIL_MARKERS = (
    "aweme detail is empty",
    "empty aweme detail",
    "no aweme detail",
    "unable to extract aweme detail",
    "uploader profile instead of the requested video",
    "returned data for a different video while requesting",
)
_INVALID_FILENAME = re.compile(r"[\x00-\x1f<>:\"/\\|?*]+")
_ERROR_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_ERROR_SECRET_NAME_PATTERN = (
    r"authorization|proxy-authorization|set-cookie|cookies?|x-bogus|x-gorgon|"
    r"a[_-]?bogus|verifyfp|s_v_web_id|ttwid|odin_tt|sid_guard|sid_tt|"
    r"uid_tt(?:_ss)?|fp|expires|sign|auth|session[_-]?id|"
    r"[a-z0-9_-]*(?:token|signature|sessionid|csrf)[a-z0-9_-]*"
)
_ERROR_SECRET_FIELD_RE = re.compile(
    rf"(?<![\w-])['\"]?({_ERROR_SECRET_NAME_PATTERN})['\"]?\s*[:=]\s*"
    r"(?:\[redacted\]|'[^']*'|\"[^\"]*\"|[^\r\n,;}\]]+)",
    re.IGNORECASE,
)
_ERROR_SECRET_TUPLE_RE = re.compile(
    rf"(['\"]?)({_ERROR_SECRET_NAME_PATTERN})\1\s*,\s*(['\"])[^'\"]*\3",
    re.IGNORECASE,
)
_ERROR_COOKIE_CONTAINER_RE = re.compile(
    r"(?<![\w-])['\"]?(set-cookie|cookies?)['\"]?\s*[:=]\s*[^\r\n]*",
    re.IGNORECASE,
)
_ERROR_BEARER_RE = re.compile(
    r"\bbearer\s+(?:\[redacted\]|[^\s,;'\"}\]]+)",
    re.IGNORECASE,
)
_ERROR_HTTP_REASON_RE = re.compile(
    r"\b(HTTP Error\s+\d{3})(?::[^\r\n]*)",
    re.IGNORECASE,
)
_ERROR_IPV4_RE = re.compile(
    r"(?<![a-z0-9])(?:\d{1,3}\.){3}\d{1,3}(?![a-z0-9])",
    re.IGNORECASE,
)
_ERROR_BRACKETED_IP_RE = re.compile(r"\[[0-9a-f:.%]+\]", re.IGNORECASE)
_ERROR_HOST_FIELD_RE = re.compile(
    r"(?<![\w-])host\s*=\s*(?:\[[^\]\r\n]*\]|'[^']*'|\"[^\"]*\"|[^,\s)\]}]+)",
    re.IGNORECASE,
)
_DOUYIN_REDIRECT_ERROR_MARKERS = (
    "media endpoint redirected to an unrecognized Douyin CDN host",
    "Douyin media redirect could not be trusted",
)
_DOUYIN_REDIRECT_REASON_RE = re.compile(
    r"(?:Redirect reason:\s*|reason:\s*)([a-z0-9-]+)",
    re.IGNORECASE,
)
_DOUYIN_REDIRECT_HOST_PATTERNS = (
    re.compile(r"(\(host:\s*)([^;)\r\n]+)", re.IGNORECASE),
    re.compile(r"(Redirect host:\s*)([^;\r\n]+)", re.IGNORECASE),
)
_DOUYIN_REDIRECT_PORT_PATTERNS = (
    re.compile(r"(\bport:\s*)([^;)\r\n]+)", re.IGNORECASE),
    re.compile(r"(Redirect port:\s*)([^;\r\n]+)", re.IGNORECASE),
)
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}

FFPROBE_REQUIRED_MESSAGE = (
    "FFprobe was not found. Install FFmpeg with FFprobe, make sure its bin "
    "directory is on PATH, fully stop this app, and restart it. On macOS with "
    "Homebrew, run `brew install ffmpeg` and then `./start.command`."
)
FFPROBE_START_MESSAGE = (
    "FFprobe was found but could not be started. Reinstall FFmpeg with FFprobe, "
    "fully stop this app, and restart it."
)


class _DouyinProbeRejected(RuntimeError):
    pass


class _DouyinRedirectRejected(_DouyinProbeRejected):
    def __init__(
        self,
        message: str,
        *,
        redirect_host: str | None = None,
        redirect_host_fingerprint: str | None = None,
        redirect_port: int | None = None,
        redirect_reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.redirect_host = redirect_host
        self.redirect_host_fingerprint = redirect_host_fingerprint
        self.redirect_port = redirect_port
        self.redirect_reason = redirect_reason


class _DouyinProbeIntegrityChanged(_DouyinProbeRejected):
    pass


def safe_component(
    value: str | None, fallback: str = "Unknown Author", limit: int = 120
) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = _INVALID_FILENAME.sub("_", value)
    value = re.sub(r"\s+", " ", value).strip(" ._")
    encoded = value.encode("utf-8")
    if len(encoded) > limit:
        value = encoded[:limit].decode("utf-8", errors="ignore").rstrip(" .")
    value = value or fallback
    if value.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        value = f"_{value}"
    return value


def _safe_douyin_redirect_family(hostname: str) -> str | None:
    normalized = hostname.strip().lower().rstrip(".")
    if not normalized or len(normalized) > 253:
        return None
    labels = normalized.split(".")
    if any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in labels
    ):
        return None
    return next(
        (
            domain
            for domain in sorted(
                DOUYIN_REGIONAL_MEDIA_DOMAINS,
                key=len,
                reverse=True,
            )
            if normalized == domain or normalized.endswith(f".{domain}")
        ),
        None,
    )


def _sanitize_douyin_redirect_diagnostic(message: str) -> str:
    if not any(marker in message for marker in _DOUYIN_REDIRECT_ERROR_MARKERS):
        return message
    reason_match = _DOUYIN_REDIRECT_REASON_RE.search(message)
    reason = reason_match.group(1).lower() if reason_match else ""

    def safe_host(match: re.Match[str]) -> str:
        family = (
            _safe_douyin_redirect_family(match.group(2))
            if reason in {"unverified-source-binding", "nonstandard-port"}
            else None
        )
        return f"{match.group(1)}{family or 'unavailable'}"

    def safe_port(match: re.Match[str]) -> str:
        raw_port = match.group(2).strip()
        try:
            port = int(raw_port) if len(raw_port) <= 5 and raw_port.isdigit() else 0
        except ValueError:
            port = 0
        return f"{match.group(1)}{port if 1 <= port <= 65_535 else 'unavailable'}"

    for pattern in _DOUYIN_REDIRECT_HOST_PATTERNS:
        message = pattern.sub(safe_host, message)
    for pattern in _DOUYIN_REDIRECT_PORT_PATTERNS:
        message = pattern.sub(safe_port, message)
    return message


def safe_external_error_message(value: BaseException | str) -> str:
    message = _sanitize_douyin_redirect_diagnostic(str(value))
    message = _ERROR_URL_RE.sub("[redacted URL]", message)
    message = _ERROR_HTTP_REASON_RE.sub(r"\1", message)
    message = _ERROR_HOST_FIELD_RE.sub("host=[redacted]", message)

    def redact_ip_literal(match: re.Match[str]) -> str:
        candidate = match.group(0).strip("[]").split("%", 1)[0]
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            return match.group(0)
        return "[redacted IP]"

    message = _ERROR_IPV4_RE.sub(redact_ip_literal, message)
    message = _ERROR_BRACKETED_IP_RE.sub(redact_ip_literal, message)
    message = _ERROR_SECRET_TUPLE_RE.sub(
        lambda match: (
            f"{match.group(1)}{match.group(2)}{match.group(1)}, "
            f"{match.group(3)}[redacted]{match.group(3)}"
        ),
        message,
    )
    message = _ERROR_SECRET_FIELD_RE.sub(
        lambda match: f"{match.group(1)}=[redacted]",
        message,
    )
    message = _ERROR_COOKIE_CONTAINER_RE.sub(
        lambda match: f"{match.group(1)}=[redacted]",
        message,
    )
    message = _ERROR_BEARER_RE.sub("Bearer [redacted]", message)
    return message[:4_000]


def _safe_media_id(value: str | None, fallback: str = "unknown-id") -> str:
    original = str(value or fallback)
    normalized = unicodedata.normalize("NFKC", original)
    sanitized = safe_component(
        normalized,
        fallback=fallback,
        limit=max(MAX_MEDIA_ID_BYTES, len(normalized.encode("utf-8"))),
    )
    if sanitized == original and len(sanitized.encode("utf-8")) <= MAX_MEDIA_ID_BYTES:
        return sanitized

    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:12]
    hash_suffix = f"~{digest}"
    prefix_limit = MAX_MEDIA_ID_BYTES - len(hash_suffix.encode("ascii"))
    prefix = safe_component(sanitized, fallback="id", limit=prefix_limit)
    return f"{prefix}{hash_suffix}"


def _normalized_douyin_duration_ms(value: Any) -> int | None:
    if type(value) is int and 0 < value <= DOUYIN_MAX_DURATION_MS:
        return value
    return None


def _filename_title_limit(requested_limit: int, media_id: str) -> int:
    date_prefix_bytes = len("Unknown-Date-".encode("ascii"))
    separators_bytes = len(" [].".encode("ascii"))
    available = (
        MAX_FILENAME_COMPONENT_BYTES
        - date_prefix_bytes
        - separators_bytes
        - RESERVED_EXTENSION_BYTES
        - len(media_id.encode("utf-8"))
    )
    return max(1, min(int(requested_limit), MAX_TITLE_BYTES, available))


class _SafeFilenamePostProcessor(PostProcessor):
    def __init__(
        self,
        downloader: YoutubeDL,
        *,
        title_byte_limit: int,
        fallback_media_id: str,
    ) -> None:
        super().__init__(downloader)
        self.title_byte_limit = title_byte_limit
        self.fallback_media_id = fallback_media_id

    def run(self, information: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
        media_id = _safe_media_id(
            information.get("id"), fallback=self.fallback_media_id
        )
        title_limit = _filename_title_limit(self.title_byte_limit, media_id)
        raw_title = information.get("title") or media_id
        information["_filename_title"] = safe_component(
            str(raw_title), fallback="Untitled", limit=title_limit
        )
        information["_filename_media_id"] = media_id
        return [], information


def _item_key(platform: Platform, media_id: str | None, url: str, index: int) -> str:
    identity = media_id or f"{url}\0{index}"
    source = f"{platform.value}\0{identity}".encode("utf-8")
    return hashlib.sha256(source).hexdigest()[:20]


def _is_auth_error(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in AUTH_ERROR_MARKERS)


def _is_temporary_access_error(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in TEMPORARY_ACCESS_MARKERS)


def _is_cookie_load_error(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in COOKIE_LOAD_ERROR_MARKERS)


@dataclass(slots=True)
class DownloaderConfig:
    cookie_browser: str | None = "chrome"
    cookie_profile: str | None = None
    allow_cookie_fallback: bool = False
    socket_timeout_seconds: int = 30
    retries: int = 10
    fragment_retries: int = 10
    concurrent_fragments: int = 4
    filename_limit: int = 180


@dataclass(slots=True)
class EngineEvent:
    event: str
    progress: TransferProgress | None = None
    message: str | None = None
    title: str | None = None
    upload_date: str | None = None
    author: str | None = None
    media_type: MediaType | None = None
    selected_format: str | None = None
    resolution: str | None = None
    output_paths: list[str] = field(default_factory=list)
    cookie_fallback_used: bool = False


@dataclass(slots=True)
class DiscoveryResult:
    author: str
    items: list[DownloadItem]
    cookie_fallback_used: bool = False
    warning: str | None = None
    discovery_complete: bool = True


@dataclass(slots=True)
class DownloadOutcome:
    output_paths: list[str]
    title: str | None = None
    upload_date: str | None = None
    author: str | None = None
    media_type: MediaType | None = None
    selected_format: str | None = None
    resolution: str | None = None
    cookie_fallback_used: bool = False


EventCallback = Callable[[EngineEvent], None]
CancelCallback = Callable[[], bool]


class _YdlLogger:
    def __init__(self, callback: EventCallback | None = None) -> None:
        self.callback = callback
        self.errors: list[str] = []

    def debug(self, message: str) -> None:
        pass

    def info(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        if self.callback:
            self.callback(EngineEvent(event="warning", message=message))

    def error(self, message: str) -> None:
        self.errors.append(message)


class MediaDownloader:
    def __init__(self, config: DownloaderConfig | None = None) -> None:
        self.config = config or DownloaderConfig()

    def discover(
        self,
        url: str,
        platform: Platform,
        kind: SourceKind,
        *,
        should_cancel: CancelCallback | None = None,
    ) -> DiscoveryResult:
        should_cancel = should_cancel or (lambda: False)
        if should_cancel():
            raise DownloadCancelledError("Task cancelled")

        short_link_fallback = False
        if kind == SourceKind.SHORT_LINK:
            resolved_url, short_link_fallback = self._resolve_short_url(url)
            if resolved_url != url:
                resolved = identify_url(resolved_url)
                if resolved.platform != platform:
                    raise DiscoveryError(
                        "The short link redirected to a different platform and was "
                        "blocked"
                    )
                result = self.discover(
                    resolved.url,
                    resolved.platform,
                    resolved.kind,
                    should_cancel=should_cancel,
                )
                if platform == Platform.XIAOHONGSHU:
                    if resolved.kind not in {
                        SourceKind.ITEM,
                        SourceKind.PROFILE,
                    }:
                        raise DiscoveryError(
                            "The Xiaohongshu short link resolved to an unsupported "
                            "target"
                        )
                    parsed_resolved = urlsplit(resolved.url)
                    binding_url = urlunsplit(
                        parsed_resolved._replace(query="", fragment="")
                    )
                    for discovered_item in result.items:
                        discovered_item.metadata.update(
                            {
                                "xiaohongshu_resolved_source_url": binding_url,
                                "xiaohongshu_resolved_source_kind": (
                                    resolved.kind.value
                                ),
                            }
                        )
                result.cookie_fallback_used |= short_link_fallback
                if result.cookie_fallback_used and not result.warning:
                    result.warning = COOKIE_FALLBACK_WARNING
                return result

        if platform == Platform.XIAOHONGSHU and kind == SourceKind.PROFILE:
            profile = discover_xhs_profile(
                url,
                cookie_profile=self.config.cookie_profile,
                use_browser_cookies=bool(self.config.cookie_browser),
                allow_cookie_fallback=self.config.allow_cookie_fallback,
                should_cancel=should_cancel,
            )
            items = [
                DownloadItem(
                    id=_item_key(platform, self._media_id(entry), entry, index),
                    media_id=self._media_id(entry),
                    source_url=entry,
                    title="Untitled Xiaohongshu note",
                    author=profile.author,
                    playlist_index=index,
                    extractor_key="XiaoHongShu",
                    metadata={
                        "xiaohongshu_profile_id": profile.profile_id,
                        "profile_note_membership_verified": True,
                    },
                )
                for index, entry in enumerate(profile.note_urls, start=1)
            ]
            return DiscoveryResult(
                author=profile.author,
                items=items,
                cookie_fallback_used=profile.cookie_fallback_used | short_link_fallback,
                warning=(
                    " ".join(
                        value
                        for value in (
                            (
                                COOKIE_FALLBACK_WARNING
                                if profile.cookie_fallback_used or short_link_fallback
                                else None
                            ),
                            profile.warning,
                        )
                        if value
                    )
                    or None
                ),
                discovery_complete=profile.discovery_complete,
            )

        if platform == Platform.DOUYIN and kind == SourceKind.PROFILE:
            profile = discover_douyin_profile(
                url,
                cookie_profile=self.config.cookie_profile,
                use_browser_cookies=bool(self.config.cookie_browser),
                allow_cookie_fallback=self.config.allow_cookie_fallback,
                should_cancel=should_cancel,
            )
            profile_id = self._douyin_profile_id(url)
            if not profile_id:
                raise DiscoveryError("The Douyin profile identity is missing")
            items: list[DownloadItem] = []
            seen_media_ids: set[str] = set()
            for index, entry in enumerate(profile.video_urls, start=1):
                media_id = self._media_id(entry)
                cached_media = profile.media_metadata.get(media_id or "")
                if (
                    not media_id
                    or media_id in seen_media_ids
                    or not isinstance(cached_media, dict)
                    or not is_complete_profile_media_metadata(
                        cached_media,
                        media_id,
                        profile_id,
                    )
                ):
                    raise TemporaryAccessError(
                        "Douyin returned incomplete profile media metadata. "
                        "No numeric placeholder queue was created; retry the "
                        "original profile after the temporary response clears."
                    )
                seen_media_ids.add(media_id)
                media_kind = str(cached_media.get("media_kind") or "").strip()
                title = str(cached_media.get("title") or "").strip()
                if not title or title == media_id or title.isdigit():
                    title = f"Untitled Douyin {media_kind}"
                author = str(cached_media.get("author") or profile.author).strip()
                create_time = cached_media.get("create_time")
                if (
                    not author
                    or type(create_time) is not int
                    or create_time <= 0
                    or media_kind not in {"video", "image"}
                ):
                    raise TemporaryAccessError(
                        "Douyin returned incomplete profile titles or ownership "
                        "metadata. No placeholder items were created; retry after "
                        "a short wait."
                    )
                metadata: dict[str, Any] = {
                    "profile_url": url,
                    "profile_owner_verified": True,
                    "douyin_profile_media": dict(cached_media),
                }
                items.append(
                    DownloadItem(
                        id=_item_key(platform, media_id, entry, index),
                        media_id=media_id,
                        source_url=entry,
                        title=title,
                        upload_date=self._douyin_upload_date(create_time),
                        author=author,
                        playlist_index=index,
                        extractor_key="Douyin",
                        media_type=(
                            MediaType.IMAGE
                            if media_kind == "image"
                            else MediaType.VIDEO
                        ),
                        metadata=metadata,
                    )
                )
            return DiscoveryResult(
                author=profile.author,
                items=items,
                cookie_fallback_used=profile.cookie_fallback_used | short_link_fallback,
                warning=(
                    " ".join(
                        value
                        for value in (
                            (
                                COOKIE_FALLBACK_WARNING
                                if profile.cookie_fallback_used or short_link_fallback
                                else None
                            ),
                            profile.warning,
                        )
                        if value
                    )
                    or None
                ),
                discovery_complete=profile.discovery_complete,
            )

        if platform == Platform.DOUYIN and kind == SourceKind.ITEM:
            return self._discover_douyin_item(url, should_cancel)

        if platform == Platform.XIAOHONGSHU and kind in {
            SourceKind.ITEM,
            SourceKind.SHORT_LINK,
        }:
            note, fallback = parse_xhs_note(
                url,
                cookie_profile=self.config.cookie_profile,
                use_browser_cookies=bool(self.config.cookie_browser),
                allow_cookie_fallback=self.config.allow_cookie_fallback,
            )
            media_type = MediaType.VIDEO if note.videos else MediaType.IMAGE
            item = DownloadItem(
                id=_item_key(platform, note.note_id, url, 1),
                media_id=note.note_id,
                source_url=url,
                title=note.title,
                upload_date=note.upload_date,
                author=note.author,
                playlist_index=1,
                extractor_key="XiaoHongShu",
                media_type=media_type,
            )
            return DiscoveryResult(
                author=note.author or "Xiaohongshu Author",
                items=[item],
                cookie_fallback_used=fallback | short_link_fallback,
                warning=(
                    COOKIE_FALLBACK_WARNING if fallback or short_link_fallback else None
                ),
            )

        return self._discover_with_ytdlp(url, platform, kind, should_cancel)

    def _discover_douyin_item(
        self,
        url: str,
        should_cancel: CancelCallback,
    ) -> DiscoveryResult:
        expected_id = self._douyin_video_id(url)
        if not expected_id:
            raise DiscoveryError("The Douyin URL has no video identifier")

        def operation(use_cookies: bool) -> tuple[dict[str, Any], str]:
            options = {
                **self._base_options(use_cookies),
                "skip_download": True,
                "noplaylist": True,
                "ignoreerrors": False,
                "logger": _YdlLogger(),
            }
            with YoutubeDL(options) as ydl:
                info = self._extract_douyin_raw_info(
                    ydl,
                    url,
                    expected_id=expected_id,
                    expected_profile_id=None,
                    verification_url=url,
                    profile_metadata=None,
                    fallback_title="Untitled Douyin video",
                    should_cancel=should_cancel,
                )
                self._validate_douyin_info(info, expected_id, url)
                video_uri = self._douyin_video_uri(info, expected_id, url)
                if not video_uri:
                    raise TemporaryAccessError(
                        "Douyin did not return a verified media identity for the "
                        "requested video. Retry the original video; Chrome verification "
                        "is not required unless Douyin explicitly shows a CAPTCHA or "
                        "login page."
                    )
            return info, video_uri

        (info, video_uri), fallback = self._run_with_cookie_fallback(
            operation,
            url=url,
        )
        author = (
            self._author_from_info(info, fallback="Douyin Author") or "Douyin Author"
        )
        title = str(info.get("title") or "").strip()
        if not title or title == expected_id or title.isdigit():
            title = "Untitled Douyin video"
        cached_media: dict[str, Any] = {
            "media_id": expected_id,
            "video_uri": video_uri,
            "title": title,
            "author": author,
        }
        native_formats = info.get("formats")
        quality_candidates: list[Any] = [info]
        if isinstance(native_formats, list):
            quality_candidates.extend(
                value
                for value in native_formats
                if isinstance(value, dict)
                and str(value.get("vcodec") or "").lower() != "none"
            )
        quality_floor = quality_floor_dimensions(quality_candidates)
        if quality_floor:
            cached_media["minimum_width"], cached_media["minimum_height"] = (
                quality_floor
            )
        owner_id = str(info.get("channel_id") or "").strip()
        if not owner_id:
            raise TemporaryAccessError(
                "Douyin did not return a verified author identity, so the author-feed "
                "highest-quality renditions could not be checked. Retry the original "
                "video; no default-only fallback was downloaded."
            )
        cached_media["owner_id"] = owner_id
        if not self.config.cookie_browser:
            raise TemporaryAccessError(
                "Douyin highest-quality verification requires the verified author "
                "feed. Enable automatic Chrome Cookie reading and retry the original "
                "video; no default-only fallback was downloaded."
            )
        if owner_id:
            try:
                enriched_media = discover_item_metadata_from_profile(
                    owner_id,
                    expected_id,
                    cookie_profile=self.config.cookie_profile,
                    should_cancel=should_cancel,
                )
            except DownloadCancelledError:
                raise
            except TemporaryAccessError as exc:
                raise TemporaryAccessError(
                    "Douyin temporarily limited the verified author-feed request "
                    "after automatic retries. Wait a minute or two and retry the "
                    "original video; no lower-quality media was downloaded."
                ) from exc
            except AuthenticationRequiredError as exc:
                raise AuthenticationRequiredError(
                    "Douyin could not verify the author's highest-quality "
                    "renditions after automatic retries. Open the original video "
                    "in Chrome, finish verification, then retry.",
                    verification_url=url,
                ) from exc
            except DiscoveryError as exc:
                raise MediaDownloadError(
                    "Douyin author-feed data failed identity or integrity validation. "
                    "Retry the original video; Chrome verification is not required "
                    "unless Douyin explicitly shows a CAPTCHA or login page."
                ) from exc
            if not enriched_media:
                raise TemporaryAccessError(
                    "Douyin could not find the requested video in its verified "
                    "author feed, so the highest quality could not be confirmed. "
                    "Retry the original video; Chrome verification was not requested."
                )
            enriched_video_uri = str(
                enriched_media.get("video_uri") or ""
            ).strip()
            if enriched_video_uri != video_uri:
                raise MediaDownloadError(
                    "Douyin author-feed enrichment returned a different media "
                    "identity for the requested video. The cross-wired response was "
                    "blocked; Chrome verification was not requested."
                )
            if "direct_candidates" in enriched_media:
                cached_media["direct_candidates"] = enriched_media["direct_candidates"]
            enriched_duration_ms = _normalized_douyin_duration_ms(
                enriched_media.get("duration_ms")
            )
            if enriched_duration_ms is not None:
                cached_media["duration_ms"] = enriched_duration_ms
            if not self._douyin_direct_candidates_from_cache(cached_media):
                raise TemporaryAccessError(
                    "Douyin author-feed data did not include a verified direct "
                    "highest-quality rendition. Retry after a short wait; no "
                    "default-only fallback was downloaded."
                )
            combined_floor = quality_floor_dimensions(
                [cached_media, enriched_media],
                cap_full_hd=False,
            )
            if combined_floor:
                cached_media["minimum_width"], cached_media["minimum_height"] = (
                    combined_floor
                )
        if "duration_ms" not in cached_media:
            duration = self._float_or_none(info.get("duration"))
            if duration and 0 < duration <= DOUYIN_MAX_DURATION_MS / 1_000:
                cached_media["duration_ms"] = int(round(duration * 1_000))
        timestamp = self._float_or_none(info.get("timestamp"))
        if timestamp and timestamp > 0:
            cached_media["create_time"] = int(timestamp)

        item = DownloadItem(
            id=_item_key(Platform.DOUYIN, expected_id, url, 1),
            media_id=expected_id,
            source_url=url,
            title=title,
            upload_date=self._normalize_date(info.get("upload_date")),
            author=author,
            playlist_index=1,
            extractor_key="Douyin",
            media_type=MediaType.VIDEO,
            metadata={
                "verification_url": url,
                "item_identity_verified": True,
                "douyin_item_media": cached_media,
            },
        )
        return DiscoveryResult(
            author=author or "Douyin Author",
            items=[item],
            cookie_fallback_used=fallback,
            warning=COOKIE_FALLBACK_WARNING if fallback else None,
        )

    def _resolve_short_url(self, url: str) -> tuple[str, bool]:
        def operation(use_cookies: bool) -> str:
            try:
                with YoutubeDL(self._base_options(use_cookies)) as ydl:
                    response = ydl.urlopen(
                        Request(url, headers={"Accept": "text/html,*/*"})
                    )
                    try:
                        return str(response.url)
                    finally:
                        response.close()
            except DownloadError:
                raise
            except Exception as exc:
                raise DownloadError(str(exc)) from exc

        return self._run_with_cookie_fallback(operation, url=url)

    def download_item(
        self,
        item: DownloadItem,
        platform: Platform,
        output_dir: str | Path,
        *,
        callback: EventCallback | None = None,
        should_cancel: CancelCallback | None = None,
    ) -> DownloadOutcome:
        should_cancel = should_cancel or (lambda: False)
        output_path = Path(output_dir).expanduser().resolve()
        output_path.mkdir(parents=True, exist_ok=True)
        if platform == Platform.XIAOHONGSHU:
            return self._download_xhs_item(
                item,
                output_path,
                callback=callback,
                should_cancel=should_cancel,
            )
        if (
            platform == Platform.DOUYIN
            and (
                (
                    isinstance(item.metadata.get("douyin_profile_media"), dict)
                    and item.metadata["douyin_profile_media"].get("media_kind")
                    == "image"
                )
                or (
                    item.media_type == MediaType.IMAGE
                    and "profile_url" in item.metadata
                )
            )
        ):
            return self._download_douyin_profile_image(
                item,
                output_path,
                callback=callback,
                should_cancel=should_cancel,
            )
        return self._download_with_ytdlp(
            item,
            output_path,
            platform=platform,
            callback=callback,
            should_cancel=should_cancel,
        )

    @staticmethod
    def _douyin_upload_date(create_time: int) -> str:
        china_timezone = timezone(timedelta(hours=8))
        try:
            return (
                datetime.fromtimestamp(create_time, china_timezone)
                .date()
                .isoformat()
            )
        except (OverflowError, OSError, ValueError) as exc:
            raise DiscoveryError("The Douyin publish time is invalid") from exc

    def _cookie_options(self, use_cookies: bool) -> dict[str, Any]:
        if not use_cookies or not self.config.cookie_browser:
            return {}
        value: tuple[str, ...]
        if self.config.cookie_profile:
            value = (self.config.cookie_browser, self.config.cookie_profile)
        else:
            value = (self.config.cookie_browser,)
        return {"cookiesfrombrowser": value}

    def _base_options(self, use_cookies: bool = True) -> dict[str, Any]:
        return {
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": self.config.socket_timeout_seconds,
            "retries": self.config.retries,
            "fragment_retries": self.config.fragment_retries,
            "extractor_retries": 3,
            "file_access_retries": 3,
            "js_runtimes": {"deno": {}},
            **self._cookie_options(use_cookies),
        }

    def _run_with_cookie_fallback(
        self,
        operation: Callable[[bool], Any],
        *,
        url: str,
    ) -> tuple[Any, bool]:
        use_cookies = bool(self.config.cookie_browser)
        try:
            return operation(use_cookies), False
        except DownloadCancelled:
            raise DownloadCancelledError("Task cancelled")
        except DownloadError as exc:
            message = str(exc)
            if use_cookies and _is_cookie_load_error(message):
                if self.config.allow_cookie_fallback:
                    try:
                        return operation(False), True
                    except DownloadCancelled as cancelled:
                        raise DownloadCancelledError("Task cancelled") from cancelled
                    except DownloadError as fallback_error:
                        self._raise_download_error(fallback_error, url)
                raise TemporaryAccessError(COOKIE_ACCESS_MESSAGE) from exc
            self._raise_download_error(exc, url)
        raise AssertionError("Unreachable")

    @staticmethod
    def _raise_download_error(exc: BaseException, url: str) -> None:
        message = str(exc)
        normalized_message = message.lower()
        if "uploader profile instead of the requested video" in normalized_message:
            raise MediaDownloadError(DOUYIN_ITEM_EXPANSION_MESSAGE) from exc
        if "returned data for a different video while requesting" in normalized_message:
            raise MediaDownloadError(
                f"{safe_external_error_message(message)}. The mismatched response "
                "was blocked; Chrome verification was not requested."
            ) from exc
        if _is_temporary_access_error(message):
            raise TemporaryAccessError(
                "The site temporarily rate-limited the request. Retry after a short "
                "wait; Chrome verification is not required unless the page explicitly "
                "shows a CAPTCHA or login prompt."
            ) from exc
        if _is_auth_error(message):
            raise AuthenticationRequiredError(
                "The site requires login, fresh browser cookies, or a CAPTCHA. "
                "Complete verification in Chrome and retry.",
                verification_url=url,
            ) from exc
        raise MediaDownloadError(safe_external_error_message(message)) from exc

    def _discover_with_ytdlp(
        self,
        url: str,
        platform: Platform,
        kind: SourceKind,
        should_cancel: CancelCallback,
    ) -> DiscoveryResult:
        def operation(use_cookies: bool) -> tuple[dict[str, Any], str | None]:
            if should_cancel():
                raise DownloadCancelled("Task cancelled")
            expand_bilibili_item = (
                platform == Platform.BILIBILI and kind == SourceKind.ITEM
            )
            options = {
                **self._base_options(use_cookies),
                "skip_download": True,
                "extract_flat": False if expand_bilibili_item else "in_playlist",
                "lazy_playlist": False,
                "ignoreerrors": False,
                "logger": _YdlLogger(),
            }
            with YoutubeDL(options) as ydl:
                result = ydl.extract_info(url, download=False)
                if not isinstance(result, dict):
                    raise DownloadError("The URL returned no downloadable media")
                if platform == Platform.DOUYIN:
                    expected_id = self._douyin_video_id(url)
                    if not expected_id:
                        raise DownloadError("The Douyin URL has no video identifier")
                    self._validate_douyin_info(result, expected_id, url)

                probed_author: str | None = None
                raw_entries = result.get("entries")
                if raw_entries is not None and str(
                    self._author_from_info(result)
                ).startswith("Unknown"):
                    entries = [
                        entry for entry in list(raw_entries) if isinstance(entry, dict)
                    ]
                    result["entries"] = entries
                    for entry in entries[:5]:
                        probed_author = self._author_from_info(
                            entry,
                            fallback=None,
                        )
                        if probed_author:
                            break
                    for entry in entries[:5]:
                        if probed_author:
                            break
                        if should_cancel():
                            raise DownloadCancelled("Task cancelled")
                        entry_url = self._entry_url(entry, platform, original_url=url)
                        try:
                            details = ydl.extract_info(entry_url, download=False)
                        except DownloadError:
                            continue
                        if isinstance(details, dict):
                            probed_author = self._author_from_info(
                                details, fallback=None
                            )
                        if probed_author:
                            break
            return result, probed_author

        (info, probed_author), fallback = self._run_with_cookie_fallback(
            operation, url=url
        )
        raw_entries = info.get("entries")
        entries = list(raw_entries) if raw_entries is not None else [info]
        entries = [entry for entry in entries if isinstance(entry, dict)]
        if not entries:
            raise DiscoveryError("No downloadable items were found")

        author = self._author_from_info(info)
        if author.startswith("Unknown"):
            author = probed_author or self._author_from_info(entries[0])
        items: list[DownloadItem] = []
        for index, entry in enumerate(entries, start=1):
            if should_cancel():
                raise DownloadCancelledError("Task cancelled")
            media_id = str(entry.get("id") or "").strip() or None
            entry_url = self._entry_url(entry, platform, original_url=url)
            items.append(
                DownloadItem(
                    id=_item_key(platform, media_id, entry_url, index),
                    media_id=media_id,
                    source_url=entry_url,
                    title=str(entry.get("title") or media_id or f"Item {index}"),
                    upload_date=self._normalize_date(entry.get("upload_date")),
                    author=self._author_from_info(entry, fallback=None) or author,
                    extractor_key=str(entry.get("extractor_key") or "") or None,
                    playlist_index=index,
                    media_type=self._media_type(entry),
                    metadata=(
                        {"verification_url": url} if platform == Platform.DOUYIN else {}
                    ),
                )
            )
        return DiscoveryResult(
            author=author,
            items=items,
            cookie_fallback_used=fallback,
            warning=COOKIE_FALLBACK_WARNING if fallback else None,
        )

    @staticmethod
    def _download_format_options(platform: Platform) -> dict[str, Any]:
        options: dict[str, Any] = {"format": "bestvideo*+bestaudio/best"}
        if platform == Platform.DOUYIN:
            options["format_sort"] = ["res"]
        return options

    @staticmethod
    def _douyin_video_id(url: str) -> str | None:
        match = re.search(r"/video/(\d+)(?:[/?#]|$)", url)
        return match.group(1) if match else None

    @staticmethod
    def _douyin_profile_id(url: str) -> str | None:
        match = re.search(r"/user/([^/?#]+)(?:[/?#]|$)", url)
        return unquote(match.group(1)).strip() if match else None

    @staticmethod
    def _is_douyin_media_host(hostname: str | None) -> bool:
        normalized = (hostname or "").lower().rstrip(".")
        return normalized == "amemv.com" or normalized.endswith(".amemv.com")

    @staticmethod
    def _is_douyin_direct_media_host(hostname: str | None) -> bool:
        normalized = (hostname or "").lower().rstrip(".")
        return any(
            normalized == domain or normalized.endswith(f".{domain}")
            for domain in DOUYIN_DIRECT_MEDIA_DOMAINS
        )

    @staticmethod
    def _is_douyin_regional_media_host(hostname: str | None) -> bool:
        normalized = (hostname or "").lower().rstrip(".")
        return any(
            normalized == domain or normalized.endswith(f".{domain}")
            for domain in DOUYIN_REGIONAL_MEDIA_DOMAINS
        )

    @staticmethod
    def _douyin_regional_media_family(hostname: str | None) -> str | None:
        normalized = (hostname or "").lower().rstrip(".")
        return next(
            (
                domain
                for domain in sorted(
                    DOUYIN_REGIONAL_MEDIA_DOMAINS,
                    key=len,
                    reverse=True,
                )
                if normalized == domain or normalized.endswith(f".{domain}")
            ),
            None,
        )

    @staticmethod
    def _parse_legacy_ipv4_part(value: str) -> int | None:
        if not value:
            return None
        try:
            if value.lower().startswith("0x"):
                return int(value[2:], 16)
            if len(value) > 1 and value.startswith("0"):
                return int(value[1:], 8)
            return int(value, 10)
        except ValueError:
            return None

    @classmethod
    def _looks_like_ip_literal(cls, hostname: str) -> bool:
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            return True
        parts = hostname.split(".")
        if not 1 <= len(parts) <= 4:
            return False
        numbers = [cls._parse_legacy_ipv4_part(part) for part in parts]
        if any(number is None or number < 0 for number in numbers):
            return False
        limits = {
            1: (0xFFFFFFFF,),
            2: (0xFF, 0xFFFFFF),
            3: (0xFF, 0xFF, 0xFFFF),
            4: (0xFF, 0xFF, 0xFF, 0xFF),
        }[len(numbers)]
        return all(number <= limit for number, limit in zip(numbers, limits))

    @staticmethod
    def _is_special_use_hostname(hostname: str) -> bool:
        special_names = {
            "example.com",
            "example.net",
            "example.org",
            "home.arpa",
            "localhost",
        }
        special_suffixes = (
            ".home",
            ".home.arpa",
            ".internal",
            ".invalid",
            ".lan",
            ".local",
            ".localhost",
            ".alt",
            ".example",
            ".onion",
            ".test",
            ".example.com",
            ".example.net",
            ".example.org",
        )
        return hostname in special_names or hostname.endswith(special_suffixes)

    @classmethod
    def _parse_strict_https_url(
        cls,
        value: str,
    ) -> tuple[str | None, str | None]:
        raw_value = str(value or "")
        if (
            not raw_value
            or len(raw_value) > 8_192
            or "\\" in raw_value
            or any(character.isspace() for character in raw_value)
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in raw_value
            )
        ):
            return None, "malformed-url"
        try:
            parsed = urlsplit(raw_value)
            hostname = (parsed.hostname or "").lower().rstrip(".")
            username = parsed.username
            password = parsed.password
            port = parsed.port
        except (TypeError, ValueError):
            return None, "malformed-url"
        if "[" in parsed.netloc or "]" in parsed.netloc:
            return None, (
                "ip-literal"
                if cls._looks_like_ip_literal(hostname)
                else "malformed-url"
            )
        if username is not None or password is not None:
            return None, "embedded-credentials"
        if not hostname:
            return None, "missing-host"
        if len(hostname) > 253:
            return None, "hostname-too-long"
        try:
            hostname.encode("ascii")
        except UnicodeEncodeError:
            return None, "non-ascii-host"
        labels = hostname.split(".")
        is_ip_literal = cls._looks_like_ip_literal(hostname)
        is_dns_hostname = not is_ip_literal and len(labels) >= 2 and not any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in labels
        )
        if parsed.scheme.lower() != "https":
            return None, "non-https-scheme"
        if is_ip_literal:
            return None, "ip-literal"
        if len(labels) < 2:
            return None, "single-label-host"
        if not is_dns_hostname:
            return None, "invalid-hostname"
        if cls._is_special_use_hostname(hostname):
            return None, "local-or-special-use-host"
        if port not in {None, 443}:
            return None, "nonstandard-port"
        return hostname, None

    @classmethod
    def _strict_https_hostname(cls, value: str) -> str | None:
        hostname, reason = cls._parse_strict_https_url(value)
        return hostname if reason is None else None

    @classmethod
    def _is_verified_douyin_media_redirect(
        cls,
        source_url: str,
        final_url: str,
    ) -> bool:
        if not cls._is_trusted_douyin_asset_url(source_url, MediaType.VIDEO):
            return False
        hostname = cls._strict_https_hostname(final_url)
        return cls._is_douyin_regional_media_host(hostname)

    @classmethod
    def _douyin_redirect_diagnostic(
        cls,
        value: str,
    ) -> tuple[str | None, str]:
        hostname, reason = cls._parse_strict_https_url(value)
        return hostname, reason or "unrecognized-host"

    @classmethod
    def _douyin_media_redirect_rejection_reason(
        cls,
        value: str,
        media_type: MediaType,
        *,
        allow_verified_regional: bool,
    ) -> str | None:
        hostname, validation_reason = cls._parse_strict_https_url(value)
        if validation_reason is not None:
            return validation_reason
        if cls._is_trusted_douyin_asset_url(value, media_type):
            return None
        if (
            media_type == MediaType.VIDEO
            and cls._is_douyin_regional_media_host(hostname)
        ):
            return None if allow_verified_regional else "unverified-source-binding"
        return "unrecognized-host"

    @classmethod
    def _douyin_redirect_rejection(
        cls,
        value: str,
        reason: str,
    ) -> _DouyinRedirectRejected:
        hostname, _ = cls._douyin_redirect_diagnostic(value)
        if reason == "nonstandard-port":
            try:
                hostname = (
                    (urlsplit(value).hostname or "").lower().rstrip(".") or None
                )
            except (TypeError, ValueError):
                hostname = None
        visible_hostname = (
            cls._douyin_regional_media_family(hostname)
            if reason in {"unverified-source-binding", "nonstandard-port"}
            else None
        )
        host_fingerprint = None
        if hostname and (
            reason == "unrecognized-host"
            or (reason == "nonstandard-port" and not visible_hostname)
        ):
            host_fingerprint = hashlib.sha256(
                hostname.encode("ascii")
            ).hexdigest()[:12]
        redirect_port = None
        if reason == "nonstandard-port":
            try:
                parsed_port = urlsplit(value).port
            except (TypeError, ValueError):
                parsed_port = None
            if type(parsed_port) is int and 1 <= parsed_port <= 65_535:
                redirect_port = parsed_port
        return _DouyinRedirectRejected(
            "media endpoint redirected to an unrecognized Douyin CDN host "
            f"(host: {visible_hostname or 'unavailable'}; "
            f"host-fingerprint: {host_fingerprint or 'unavailable'}; "
            f"port: {redirect_port or 'unavailable'}; "
            f"reason: {reason})",
            redirect_host=visible_hostname,
            redirect_host_fingerprint=host_fingerprint,
            redirect_port=redirect_port,
            redirect_reason=reason,
        )

    @classmethod
    def _open_douyin_media_response(
        cls,
        ydl: YoutubeDL,
        request: Request,
        *,
        redirect_rejection_reason: Callable[[str], str | None],
        should_cancel: CancelCallback | None = None,
    ):
        should_cancel = should_cancel or (lambda: False)
        if should_cancel():
            raise DownloadCancelled("Task cancelled")
        initial_reason = redirect_rejection_reason(request.url)
        if initial_reason is not None:
            raise cls._douyin_redirect_rejection(request.url, initial_reason)
        director = getattr(ydl, "_request_director", None)
        if director is None:
            raise _DouyinProbeRejected(
                "secure Douyin redirect validation requires a yt-dlp request director"
            )
        handler = getattr(director, "handlers", {}).get("Requests")
        if not isinstance(handler, RequestsRH) or requests is None:
            raise _DouyinProbeRejected(
                "secure Douyin redirect validation requires the yt-dlp requests handler"
            )
        headers = handler._get_headers(request)
        isolated_cookie_jar = getattr(
            handler,
            "_douyin_isolated_media_cookie_jar",
            None,
        )
        if not isinstance(isolated_cookie_jar, CookieJar):
            isolated_cookie_jar = CookieJar()
            setattr(
                handler,
                "_douyin_isolated_media_cookie_jar",
                isolated_cookie_jar,
            )
        else:
            isolated_cookie_jar.clear()
        session = handler._get_instance(
            cookiejar=isolated_cookie_jar,
            legacy_ssl_support=request.extensions.get("legacy_ssl"),
        )
        current_url = request.url
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
        request_timeout = handler._calculate_timeout(request)
        redirect_deadline = time.monotonic() + request_timeout
        for redirect_count in range(DOUYIN_MAX_MEDIA_REDIRECTS + 1):
            if should_cancel():
                raise DownloadCancelled("Task cancelled")
            remaining_timeout = redirect_deadline - time.monotonic()
            if remaining_timeout <= 0:
                raise TransportError(
                    cause=TimeoutError("Douyin redirect chain timed out")
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

            if should_cancel():
                raw_response.close()
                raise DownloadCancelled("Task cancelled")

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
                    raise cls._douyin_redirect_rejection(
                        "",
                        "malformed-url",
                    )
                try:
                    target_url = urljoin(str(raw_response.url), location)
                except (TypeError, ValueError):
                    raw_response.close()
                    raise cls._douyin_redirect_rejection(
                        "",
                        "malformed-url",
                    )
                try:
                    reason = redirect_rejection_reason(target_url)
                except BaseException:
                    raw_response.close()
                    raise
                if reason is not None:
                    raw_response.close()
                    raise cls._douyin_redirect_rejection(target_url, reason)
                if redirect_count >= DOUYIN_MAX_MEDIA_REDIRECTS:
                    raw_response.close()
                    raise cls._douyin_redirect_rejection(
                        target_url,
                        "too-many-redirects",
                    )
                raw_response.close()
                if urlsplit(current_url).netloc.lower() != (
                    urlsplit(target_url).netloc.lower()
                ):
                    current_headers = {
                        key: value
                        for key, value in current_headers.items()
                        if key.lower()
                        not in {
                            "authorization",
                            "cookie",
                            "host",
                            "proxy-authorization",
                        }
                    }
                current_url = target_url
                continue

            response = RequestsResponseAdapter(raw_response)
            if not 200 <= response.status < 300:
                raise HTTPError(response)
            return response
        raise AssertionError("unreachable Douyin redirect loop state")

    @classmethod
    def _is_trusted_douyin_asset_url(
        cls,
        value: str,
        media_type: MediaType,
    ) -> bool:
        hostname = cls._strict_https_hostname(value)
        if not hostname:
            return False
        if media_type == MediaType.IMAGE:
            return hostname == "douyinpic.com" or hostname.endswith(
                ".douyinpic.com"
            )
        return cls._is_douyin_direct_media_host(hostname)

    @classmethod
    def _douyin_direct_candidates_from_cache(
        cls,
        cached: dict[str, Any],
    ) -> list[dict[str, Any]]:
        raw_candidates = cached.get("direct_candidates")
        if not isinstance(raw_candidates, list):
            return []
        expected_video_uri = str(cached.get("video_uri") or "").strip()
        result: list[dict[str, Any]] = []
        for value in raw_candidates[:4]:
            if not isinstance(value, dict):
                continue
            if (
                expected_video_uri
                and str(value.get("video_uri") or "").strip()
                != expected_video_uri
            ):
                continue
            try:
                width = int(value.get("width") or 0)
                height = int(value.get("height") or 0)
                bit_rate = int(value.get("bit_rate") or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if not (0 < width <= 16_384 and 0 < height <= 16_384):
                continue
            urls: list[str] = []
            for candidate_url in value.get("urls") or []:
                if not cls._is_trusted_douyin_asset_url(
                    candidate_url,
                    MediaType.VIDEO,
                ):
                    continue
                if candidate_url not in urls:
                    urls.append(candidate_url)
                if len(urls) >= 5:
                    break
            if not urls:
                continue
            candidate: dict[str, Any] = {
                "width": width,
                "height": height,
                "urls": urls,
            }
            if bit_rate > 0:
                candidate["bit_rate"] = bit_rate
            codec_hint = str(value.get("codec_hint") or "").strip().lower()
            if codec_hint in {"h264", "hevc", "h265", "vvc", "h266", "bytevc2"}:
                candidate["codec_hint"] = codec_hint
            result.append(candidate)
        return result

    @staticmethod
    def _raise_douyin_mismatch(
        expected_id: str, actual_id: str, verification_url: str
    ) -> None:
        raise MediaDownloadError(
            "Douyin returned data for a different video while requesting "
            f"{expected_id} (received {actual_id}). The mismatched response was "
            "blocked; Chrome verification was not requested."
        )

    @staticmethod
    def _is_douyin_playlist_result(info: dict[str, Any]) -> bool:
        result_type = str(info.get("_type") or "").lower()
        return info.get("entries") is not None or result_type in {
            "playlist",
            "multi_video",
        }

    def _validate_douyin_info(
        self,
        info: dict[str, Any],
        expected_id: str,
        verification_url: str,
        expected_profile_id: str | None = None,
    ) -> None:
        actual_id = str(info.get("id") or "").strip()
        if actual_id != expected_id:
            self._raise_douyin_mismatch(
                expected_id, actual_id or "missing", verification_url
            )
        if self._is_douyin_playlist_result(info):
            raise MediaDownloadError(DOUYIN_ITEM_EXPANSION_MESSAGE)
        if expected_profile_id:
            actual_profile_id = str(info.get("channel_id") or "").strip()
            if actual_profile_id != expected_profile_id:
                raise MediaDownloadError(
                    "Douyin returned data from a different author while requesting "
                    f"profile {expected_profile_id} (received "
                    f"{actual_profile_id or 'missing'}). The mismatched response was "
                    "blocked; Chrome verification was not requested."
                )

    def _douyin_video_uri(
        self,
        info: dict[str, Any],
        expected_id: str,
        verification_url: str,
        expected_profile_id: str | None = None,
    ) -> str | None:
        self._validate_douyin_info(
            info,
            expected_id,
            verification_url,
            expected_profile_id,
        )
        uris: set[str] = set()
        for value in info.get("formats") or []:
            if not isinstance(value, dict):
                continue
            media_url = value.get("url")
            if not isinstance(media_url, str):
                continue
            parsed = urlsplit(media_url)
            if (
                not self._is_douyin_media_host(parsed.hostname)
                or parsed.path.rstrip("/") != "/aweme/v1/play"
            ):
                continue
            for uri in parse_qs(parsed.query).get("video_id", []):
                if re.fullmatch(r"[A-Za-z0-9_-]{10,200}", uri):
                    uris.add(uri)
        if len(uris) > 1:
            raise MediaDownloadError(
                "Douyin returned multiple media identities for one video. The "
                "cross-wired response was blocked; Chrome verification was not "
                "requested."
            )
        return next(iter(uris), None)

    @staticmethod
    def _should_use_douyin_signed_detail(error: DownloadError) -> bool:
        message = str(error).lower()
        return _is_auth_error(message) or any(
            marker in message for marker in DOUYIN_EMPTY_DETAIL_MARKERS
        )

    def _extract_douyin_raw_info(
        self,
        ydl: YoutubeDL,
        source_url: str,
        *,
        expected_id: str,
        expected_profile_id: str | None,
        verification_url: str,
        profile_metadata: dict[str, Any] | None,
        fallback_title: str,
        should_cancel: CancelCallback,
    ) -> dict[str, Any]:
        cached_result = self._douyin_raw_info_from_item_metadata(
            profile_metadata,
            expected_id=expected_id,
            expected_profile_id=expected_profile_id,
            verification_url=verification_url,
            fallback_title=fallback_title,
        ) or self._douyin_raw_info_from_profile_metadata(
            profile_metadata,
            expected_id=expected_id,
            expected_profile_id=expected_profile_id,
            verification_url=verification_url,
            fallback_title=fallback_title,
        )
        if cached_result:
            return cached_result
        try:
            raw_result = ydl.extract_info(
                source_url,
                download=False,
                process=False,
            )
            if isinstance(raw_result, dict):
                if self._is_douyin_playlist_result(raw_result):
                    extraction_error = DownloadError(
                        "Douyin returned an uploader profile instead of the requested "
                        "video"
                    )
                else:
                    actual_id = str(raw_result.get("id") or "").strip()
                    if actual_id == expected_id:
                        return raw_result
                    extraction_error = DownloadError(
                        "Douyin returned data for a different video while requesting "
                        f"{expected_id} (received {actual_id or 'missing'})"
                    )
            else:
                extraction_error = DownloadError(
                    "Douyin returned an empty aweme detail"
                )
        except DownloadError as exc:
            extraction_error = exc

        if not self.config.cookie_browser or not self._should_use_douyin_signed_detail(
            extraction_error
        ):
            raise extraction_error
        if should_cancel():
            raise DownloadCancelled("Task cancelled")

        detail = fetch_signed_aweme_detail(
            expected_id,
            verification_url=verification_url,
            expected_sec_uid=expected_profile_id,
            cookie_profile=self.config.cookie_profile,
            should_cancel=should_cancel,
        )
        parsed = DouyinIE(ydl)._parse_aweme_video_app(detail)
        if not isinstance(parsed, dict):
            raise DownloadError("Douyin signed detail returned no downloadable media")
        parsed.setdefault("webpage_url", source_url)
        parsed.setdefault("original_url", source_url)
        parsed.setdefault("extractor", "Douyin")
        parsed.setdefault("extractor_key", "Douyin")
        return parsed

    def _douyin_raw_info_from_item_metadata(
        self,
        metadata: dict[str, Any] | None,
        *,
        expected_id: str,
        expected_profile_id: str | None,
        verification_url: str,
        fallback_title: str,
    ) -> dict[str, Any] | None:
        if not metadata or "douyin_item_media" not in metadata:
            return None
        cached = metadata.get("douyin_item_media")
        if (
            not isinstance(cached, dict)
            or metadata.get("item_identity_verified") is not True
            or self._douyin_video_id(verification_url) != expected_id
        ):
            raise MediaDownloadError(
                "Douyin item metadata could not be verified. Create a new task "
                "from the original video and retry; Chrome verification was not "
                "requested."
            )
        cached_id = str(cached.get("media_id") or "").strip()
        if cached_id != expected_id:
            self._raise_douyin_mismatch(
                expected_id,
                cached_id or "missing",
                verification_url,
            )
        cached_owner = str(cached.get("owner_id") or "").strip()
        if expected_profile_id and cached_owner != expected_profile_id:
            raise MediaDownloadError(
                "Douyin item metadata belongs to a different author. Create a new "
                "task from the original link and retry; Chrome verification was not "
                "requested."
            )
        video_uri = str(cached.get("video_uri") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", video_uri):
            raise MediaDownloadError(
                "Douyin item metadata has no verified media identity. Create a new "
                "task from the original video and retry; Chrome verification was not "
                "requested."
            )

        result: dict[str, Any] = {
            "id": expected_id,
            "channel": str(cached.get("author") or "Douyin Author"),
            "title": str(cached.get("title") or fallback_title or expected_id),
            "formats": [
                {
                    "format_id": "item-cached-play",
                    "url": self._douyin_ratio_url(video_uri, "720p"),
                    "ext": "mp4",
                    "preference": -1,
                    "source_preference": -2,
                }
            ],
            "_format_sort_fields": ("quality", "codec", "size", "br"),
            "_douyin_verified_cache_only": True,
        }
        if cached_owner:
            result["channel_id"] = cached_owner
        direct_candidates = self._douyin_direct_candidates_from_cache(cached)
        if direct_candidates:
            result["_douyin_direct_candidates"] = direct_candidates
        quality_floor = quality_floor_dimensions([cached], cap_full_hd=False)
        if quality_floor:
            result["_douyin_minimum_width"], result["_douyin_minimum_height"] = (
                quality_floor
            )
        duration_ms = _normalized_douyin_duration_ms(cached.get("duration_ms"))
        if duration_ms is not None:
            result["duration"] = duration_ms / 1_000
        create_time = cached.get("create_time")
        if isinstance(create_time, int) and create_time > 0:
            result["timestamp"] = create_time
            result["upload_date"] = self._douyin_upload_date(create_time).replace(
                "-", ""
            )
        self._validate_douyin_info(
            result,
            expected_id,
            verification_url,
            expected_profile_id,
        )
        return result

    def _douyin_raw_info_from_profile_metadata(
        self,
        metadata: dict[str, Any] | None,
        *,
        expected_id: str,
        expected_profile_id: str | None,
        verification_url: str,
        fallback_title: str,
    ) -> dict[str, Any] | None:
        if not metadata or "douyin_profile_media" not in metadata:
            return None
        cached = metadata.get("douyin_profile_media")
        if (
            not isinstance(cached, dict)
            or metadata.get("profile_owner_verified") is not True
            or not expected_profile_id
        ):
            raise MediaDownloadError(
                "Douyin profile media metadata is incomplete. Retry the original "
                "profile to refresh it; Chrome verification is not required for "
                "this local metadata error."
            )
        cached_id = str(cached.get("media_id") or "").strip()
        if cached_id != expected_id:
            self._raise_douyin_mismatch(
                expected_id,
                cached_id or "missing",
                verification_url,
            )
        cached_owner = str(cached.get("owner_id") or "").strip()
        if cached_owner != expected_profile_id:
            raise MediaDownloadError(
                "Douyin profile metadata belongs to a different author. Create a "
                "new task from the original profile and retry; Chrome verification "
                "was not requested."
            )
        raw_direct_candidates = cached.get("direct_candidates")
        if cached.get("media_kind") == "video" and raw_direct_candidates is None:
            raise TemporaryAccessError(
                "This saved Douyin task predates author-feed highest-quality "
                "verification. The task was paused instead of downloading a "
                "default-only fallback. Create a new task from the original profile."
            )
        if not is_complete_profile_media_metadata(
            cached,
            expected_id,
            expected_profile_id,
        ):
            raise MediaDownloadError(
                "Douyin profile media metadata is incomplete. Retry the original "
                "profile to refresh it; Chrome verification is not required for "
                "this local metadata error."
            )
        video_uri = str(cached.get("video_uri") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", video_uri):
            raise MediaDownloadError(
                "Douyin profile metadata has no verified media identity. Create a "
                "new task from the original profile and retry; Chrome verification "
                "was not requested."
            )

        result: dict[str, Any] = {
            "id": expected_id,
            "channel_id": expected_profile_id,
            "channel": str(cached.get("author") or "Douyin Author"),
            "title": str(cached.get("title") or fallback_title or expected_id),
            "formats": [
                {
                    "format_id": "profile-cached-play",
                    "url": self._douyin_ratio_url(video_uri, "720p"),
                    "ext": "mp4",
                    "preference": -1,
                    "source_preference": -2,
                }
            ],
            "_format_sort_fields": ("quality", "codec", "size", "br"),
            "_douyin_profile_cache_only": True,
            "_douyin_verified_cache_only": True,
        }
        direct_candidates = self._douyin_direct_candidates_from_cache(cached)
        if direct_candidates:
            result["_douyin_direct_candidates"] = direct_candidates
        quality_floor = quality_floor_dimensions([cached], cap_full_hd=False)
        if quality_floor:
            result["_douyin_minimum_width"], result["_douyin_minimum_height"] = (
                quality_floor
            )
        duration_ms = _normalized_douyin_duration_ms(cached.get("duration_ms"))
        if duration_ms is not None:
            result["duration"] = duration_ms / 1_000
        create_time = cached.get("create_time")
        if isinstance(create_time, int) and create_time > 0:
            result["timestamp"] = create_time
            result["upload_date"] = self._douyin_upload_date(create_time).replace(
                "-", ""
            )
        self._validate_douyin_info(
            result,
            expected_id,
            verification_url,
            expected_profile_id,
        )
        return result

    @staticmethod
    def _douyin_ratio_url(
        video_uri: str,
        ratio: str,
        *,
        hostname: str | None = None,
    ) -> str:
        hostname = hostname or (
            DOUYIN_DEFAULT_PROBE_HOST
            if ratio == "default"
            else DOUYIN_RATIO_PROBE_HOST
        )
        query = urlencode(
            {
                "video_id": video_uri,
                "ratio": ratio,
                "line": "0",
                "is_play_url": "1",
                "source": "PackSourceEnum_AWEME_DETAIL",
            }
        )
        return f"https://{hostname}/aweme/v1/play/?{query}"

    @classmethod
    def _douyin_default_probe_urls(cls, video_uri: str) -> list[str]:
        return [
            cls._douyin_ratio_url(
                video_uri,
                "default",
                hostname=hostname,
            )
            for hostname in (
                DOUYIN_DEFAULT_PROBE_HOST,
                DOUYIN_RATIO_PROBE_HOST,
            )
        ]

    def _probe_douyin_default_with_fallback(
        self,
        ydl: YoutubeDL,
        video_uri: str,
        *,
        expected_duration: float | None,
        callback: EventCallback | None,
        should_cancel: CancelCallback,
    ) -> dict[str, Any] | None:
        failures: list[Exception] = []
        candidate_urls = self._douyin_default_probe_urls(video_uri)
        for candidate_url in candidate_urls:
            try:
                probe = self._probe_douyin_ratio_with_retry(
                    ydl,
                    candidate_url,
                    ratio="default",
                    expected_duration=expected_duration,
                    callback=callback,
                    should_cancel=should_cancel,
                )
            except (DownloadCancelled, MediaDownloadError):
                raise
            except Exception as exc:
                failures.append(exc)
                continue
            if probe:
                probe["source_candidates"] = list(
                    dict.fromkeys([candidate_url, *candidate_urls])
                )
                return probe
        if failures:
            blocking = next(
                (
                    failure
                    for failure in failures
                    if isinstance(
                        failure,
                        (_DouyinRedirectRejected, _DouyinProbeIntegrityChanged),
                    )
                ),
                next(
                    (
                        failure
                        for failure in failures
                        if self._should_pause_douyin_probe_error(failure)
                    ),
                    failures[-1],
                ),
            )
            raise blocking
        return None

    def _add_douyin_probe_formats(
        self,
        ydl: YoutubeDL,
        info: dict[str, Any],
        *,
        expected_id: str,
        verification_url: str,
        expected_profile_id: str | None = None,
        callback: EventCallback | None = None,
        should_cancel: CancelCallback,
    ) -> bool:
        formats = [
            value
            for value in (info.get("formats") or [])
            if not isinstance(value, dict)
            or not str(value.get("format_id") or "").startswith("douyin-api-")
        ]
        info["formats"] = formats
        video_uri = self._douyin_video_uri(
            info,
            expected_id,
            verification_url,
            expected_profile_id,
        )
        if not video_uri:
            info["_douyin_probe_failure"] = "no verified media identity was available"
            return False
        expected_duration = self._float_or_none(info.get("duration"))
        probes: list[dict[str, Any]] = []
        failures: list[tuple[str, str]] = []
        direct_candidates = info.get("_douyin_direct_candidates") or []
        if not isinstance(direct_candidates, list):
            direct_candidates = []
        if not direct_candidates:
            raise TemporaryAccessError(
                "Douyin author-feed quality renditions were unavailable, so the "
                "highest quality could not be verified. The task was paused before "
                "downloading this or later items. Refresh the task from the original "
                "link with Chrome Cookie enabled; no default-only fallback was "
                "downloaded."
            )
        direct_count = len(direct_candidates)
        direct_failures: list[tuple[dict[str, Any], str, str, bool]] = []
        for index, candidate in enumerate(direct_candidates, start=1):
            if should_cancel():
                raise DownloadCancelled("Task cancelled")
            declared_width = int(candidate["width"])
            declared_height = int(candidate["height"])
            label = f"author-feed-{index}"
            if callback:
                callback(
                    EngineEvent(
                        event="probing",
                        message=(
                            "Checking Douyin author-feed quality "
                            f"{index}/{direct_count}: "
                            f"{declared_width}x{declared_height}"
                        ),
                    )
                )
            direct_probe: dict[str, Any] | None = None
            errors: list[tuple[str, bool]] = []
            for candidate_url in candidate.get("urls") or []:
                try:
                    direct_probe = self._probe_douyin_ratio_with_retry(
                        ydl,
                        candidate_url,
                        ratio=label,
                        expected_duration=expected_duration,
                        callback=callback,
                        should_cancel=should_cancel,
                    )
                except DownloadCancelled:
                    raise
                except MediaDownloadError:
                    raise
                except Exception as exc:
                    errors.append(
                        (
                            self._safe_douyin_probe_failure(exc),
                            self._should_pause_douyin_probe_error(exc),
                        )
                    )
                    continue
                if direct_probe:
                    break
                errors.append(("media metadata could not be parsed", False))
            if direct_probe:
                direct_probe["source_candidates"] = list(
                    dict.fromkeys(
                        [
                            str(direct_probe.get("source_url") or ""),
                            *(candidate.get("urls") or []),
                        ]
                    )
                )
                direct_probe["source_candidates"] = [
                    value
                    for value in direct_probe["source_candidates"]
                    if value
                    and self._is_trusted_douyin_asset_url(
                        value,
                        MediaType.VIDEO,
                    )
                ]
                actual_width = int(direct_probe.get("width") or 0)
                actual_height = int(direct_probe.get("height") or 0)
                if (
                    min(actual_width, actual_height)
                    < min(declared_width, declared_height)
                    or max(actual_width, actual_height)
                    < max(declared_width, declared_height)
                ):
                    direct_failures.append(
                        (
                            candidate,
                            label,
                            (
                                "verified media was below the author-feed "
                                f"{declared_width}x{declared_height} rendition"
                            ),
                            False,
                        )
                    )
                else:
                    direct_probe["requested_ratio"] = label
                    probes.append(direct_probe)
                continue
            direct_failures.append(
                (
                    candidate,
                    label,
                    errors[-1][0] if errors else "no media URL was available",
                    any(transient for _, transient in errors),
                )
            )

        if should_cancel():
            raise DownloadCancelled("Task cancelled")
        if callback:
            callback(
                EngineEvent(
                    event="probing",
                    message="Checking Douyin quality 1/1: default",
                )
            )
        try:
            default_probe = self._probe_douyin_default_with_fallback(
                ydl,
                video_uri,
                expected_duration=expected_duration,
                callback=callback,
                should_cancel=should_cancel,
            )
        except DownloadCancelled:
            raise
        except MediaDownloadError:
            raise
        except Exception as exc:
            reason = self._safe_douyin_probe_failure(exc)
            if self._should_pause_douyin_probe_error(exc):
                raise TemporaryAccessError(
                    "Douyin authoritative default quality source was temporarily "
                    "unavailable after automatic retries. The task was paused before "
                    "probing later items; wait briefly and continue the task. No "
                    "lower-quality fallback was downloaded. Probe details: "
                    f"default: {reason}"
                ) from exc
            failures.append(("default", reason))
        else:
            if default_probe:
                default_probe["requested_ratio"] = "default"
                probes.append(default_probe)
            else:
                failures.append(("default", "media metadata could not be parsed"))

        unresolved_direct = self._unresolved_douyin_direct_failures(
            direct_failures,
            probes,
        )
        if unresolved_direct:
            details = self._summarize_douyin_probe_failures(
                [(label, reason) for label, reason, _ in unresolved_direct]
            )
            if any(transient for _, _, transient in unresolved_direct):
                raise TemporaryAccessError(
                    "Douyin authoritative author-feed quality source was temporarily "
                    "unavailable after automatic retries. The task was paused before "
                    "probing later items; wait briefly and continue the task. No "
                    f"lower-quality fallback was downloaded. Probe details: {details}"
                )
            failures.extend(
                (label, reason) for label, reason, _ in unresolved_direct
            )

        unique_probes: dict[tuple[Any, ...], dict[str, Any]] = {}
        unsupported_probes: list[dict[str, Any]] = []
        for probe in probes:
            video_codec = str(probe.get("vcodec") or "").lower()
            if video_codec in {"bytevc2", "h266", "vvc"}:
                unsupported_probes.append(probe)
                continue
            signature = self._douyin_probe_media_identity_key(probe)
            existing_probe = unique_probes.get(signature)
            if existing_probe is None:
                unique_probes[signature] = probe
            else:
                self._merge_douyin_probe_source_candidates(
                    existing_probe,
                    probe,
                )

        if unique_probes:
            best_supported_rank = max(
                self._douyin_probe_quality_key(value)
                for value in unique_probes.values()
            )
            blocking_unsupported = [
                value
                for value in unsupported_probes
                if self._douyin_probe_quality_key(value) > best_supported_rank
            ]
        else:
            blocking_unsupported = unsupported_probes
        codec_failures: list[tuple[str, str]] = []
        for probe in blocking_unsupported:
            video_codec = str(probe.get("vcodec") or "").lower()
            codec_failures.append(
                (
                    str(probe.get("requested_ratio") or "unknown"),
                    f"highest candidate uses unsupported video codec {video_codec}",
                )
            )

        if codec_failures:
            info["_douyin_probe_failure"] = self._summarize_douyin_probe_failures(
                codec_failures
            )
            return False
        if not unique_probes:
            info["_douyin_probe_failure"] = (
                self._summarize_douyin_probe_failures(failures)
                if failures
                else "no playable candidate was returned"
            )
            return False

        quality_floor = quality_floor_dimensions(
            [
                {
                    "width": info.get("_douyin_minimum_width"),
                    "height": info.get("_douyin_minimum_height"),
                }
            ],
            cap_full_hd=False,
        )
        best_probe = max(
            unique_probes.values(),
            key=self._douyin_probe_quality_key,
        )
        best_dimensions = (int(best_probe["width"]), int(best_probe["height"]))
        if quality_floor:
            best_short, best_long = sorted(best_dimensions)
            minimum_short, minimum_long = sorted(quality_floor)
            if best_short < minimum_short or best_long < minimum_long:
                info["_douyin_probe_failure"] = (
                    "best verified candidate was "
                    f"{best_dimensions[0]}x{best_dimensions[1]}, below the "
                    "discovered minimum "
                    f"{quality_floor[0]}x{quality_floor[1]}"
                )
                return False
        if failures:
            info["_douyin_probe_failure"] = self._summarize_douyin_probe_failures(
                failures
            )
            return False

        for index, probe in enumerate(unique_probes.values(), start=1):
            width = int(probe["width"])
            height = int(probe["height"])
            bit_rate = int(probe.get("bit_rate") or 0)
            formats.append(
                {
                    "format_id": f"douyin-api-{width}x{height}-{index}",
                    "format_note": "Verified original-quality endpoint",
                    "url": probe["url"],
                    "ext": "mp4",
                    "vcodec": probe.get("vcodec") or "h264",
                    "acodec": probe.get("acodec") or "aac",
                    "width": width,
                    "height": height,
                    "tbr": bit_rate / 1000 if bit_rate else None,
                    "filesize": probe.get("filesize"),
                    "duration": probe.get("duration"),
                    "_douyin_probe_prefix_size": probe.get("probe_prefix_size"),
                    "_douyin_probe_prefix_sha256": probe.get(
                        "probe_prefix_sha256"
                    ),
                    "_douyin_probe_source_url": probe.get("source_url"),
                    "_douyin_probe_source_urls": list(
                        probe.get("source_candidates") or []
                    ),
                    "_douyin_requested_ratio": probe.get("requested_ratio"),
                    "preference": -1,
                    "http_headers": dict(DOUYIN_MEDIA_HEADERS),
                }
            )
        if unique_probes and info.get("_douyin_verified_cache_only"):
            info["formats"] = [
                value
                for value in formats
                if value.get("format_id")
                not in {"profile-cached-play", "item-cached-play"}
            ]
        info.pop("_douyin_probe_failure", None)
        return True

    def _probe_douyin_ratio_with_retry(
        self,
        ydl: YoutubeDL,
        candidate_url: str,
        *,
        ratio: str,
        expected_duration: float | None,
        callback: EventCallback | None,
        should_cancel: CancelCallback,
    ) -> dict[str, Any] | None:
        for attempt in range(1, DOUYIN_PROBE_ATTEMPTS + 1):
            try:
                while not DOUYIN_MEDIA_PROBE_LOCK.acquire(
                    timeout=DOUYIN_PROCESS_POLL_SECONDS
                ):
                    if should_cancel():
                        raise DownloadCancelled("Task cancelled")
                try:
                    return self._probe_douyin_candidate(
                        ydl,
                        candidate_url,
                        expected_duration=expected_duration,
                        should_cancel=should_cancel,
                    )
                finally:
                    DOUYIN_MEDIA_PROBE_LOCK.release()
            except (DownloadCancelled, MediaDownloadError):
                raise
            except Exception as exc:
                retryable = self._is_retryable_douyin_probe_error(exc)
                self._close_douyin_probe_error(exc)
                if (
                    attempt >= DOUYIN_PROBE_ATTEMPTS
                    or not retryable
                ):
                    raise
                if callback:
                    callback(
                        EngineEvent(
                            event="probing",
                            message=(
                                f"Retrying Douyin quality {ratio} after a "
                                f"temporary network error "
                                f"({attempt + 1}/{DOUYIN_PROBE_ATTEMPTS})"
                            ),
                        )
                    )
                self._wait_for_douyin_probe_retry(
                    DOUYIN_PROBE_RETRY_BASE_SECONDS * (2 ** (attempt - 1)),
                    should_cancel,
                )
        return None

    @staticmethod
    def _is_retryable_douyin_probe_error(exc: Exception) -> bool:
        if isinstance(exc, _DouyinProbeRejected):
            return False
        if isinstance(exc, HTTPError):
            return exc.status in {
                401,
                403,
                404,
                408,
                410,
                425,
                429,
                500,
                502,
                503,
                504,
            }
        if isinstance(exc, (TransportError, TimeoutError, ConnectionError, OSError)):
            return True
        return False

    @classmethod
    def _should_pause_douyin_probe_error(cls, exc: Exception) -> bool:
        return isinstance(
            exc,
            (_DouyinRedirectRejected, _DouyinProbeIntegrityChanged),
        ) or (
            cls._is_retryable_douyin_probe_error(exc)
        )

    @staticmethod
    def _close_douyin_probe_error(exc: Exception) -> None:
        if isinstance(exc, HTTPError):
            with contextlib.suppress(Exception):
                exc.close()

    @staticmethod
    def _wait_for_douyin_probe_retry(
        delay_seconds: float,
        should_cancel: CancelCallback,
    ) -> None:
        deadline = time.monotonic() + max(delay_seconds, 0.0)
        while True:
            if should_cancel():
                raise DownloadCancelled("Task cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(DOUYIN_PROCESS_POLL_SECONDS, remaining))

    @staticmethod
    def _safe_douyin_probe_failure(exc: Exception) -> str:
        if isinstance(exc, _DouyinProbeRejected):
            return str(exc)
        text = f"{type(exc).__name__} {exc}".lower()
        if "timeout" in text or "timed out" in text:
            return "media request or FFprobe timed out"
        if "ssl" in text or "certificate" in text:
            return "secure media connection failed"
        if any(
            marker in text
            for marker in ("connection", "network", "resolve", "remote end")
        ):
            return "media endpoint network request failed"
        if isinstance(exc, HTTPError):
            return f"media endpoint returned HTTP {exc.status}"
        if (
            type(exc).__name__.lower() == "httperror"
            or "http error" in text
            or "http status" in text
            or "status code" in text
        ):
            return "media endpoint returned an HTTP error"
        return f"media probe failed ({type(exc).__name__})"

    @staticmethod
    def _summarize_douyin_probe_failures(
        failures: list[tuple[str, str]],
    ) -> str:
        if not failures:
            return "no playable candidate was returned"
        grouped: dict[str, list[str]] = {}
        for ratio, reason in failures:
            grouped.setdefault(reason, []).append(ratio)
        return "; ".join(
            f"{','.join(ratios)}: {reason}" for reason, ratios in grouped.items()
        )

    def _probe_douyin_candidate(
        self,
        ydl: YoutubeDL,
        candidate_url: str,
        *,
        expected_duration: float | None,
        should_cancel: CancelCallback,
    ) -> dict[str, Any] | None:
        if should_cancel():
            raise DownloadCancelled("Task cancelled")
        if not self._is_trusted_douyin_asset_url(
            candidate_url,
            MediaType.VIDEO,
        ):
            raise _DouyinProbeRejected(
                "untrusted initial Douyin media endpoint was blocked"
            )
        headers = {
            **DOUYIN_MEDIA_HEADERS,
            "Range": f"bytes=0-{DOUYIN_PROBE_BYTES - 1}",
        }
        response = self._open_douyin_media_response(
            ydl,
            Request(
                candidate_url,
                headers=headers,
                extensions={"timeout": DOUYIN_PROBE_HTTP_TIMEOUT_SECONDS},
            ),
            redirect_rejection_reason=lambda value: (
                self._douyin_media_redirect_rejection_reason(
                    value,
                    MediaType.VIDEO,
                    allow_verified_regional=True,
                )
            ),
            should_cancel=should_cancel,
        )
        try:
            final_url = str(response.url)
            redirect_reason = self._douyin_media_redirect_rejection_reason(
                final_url,
                MediaType.VIDEO,
                allow_verified_regional=True,
            )
            if redirect_reason is not None:
                raise self._douyin_redirect_rejection(
                    final_url,
                    redirect_reason,
                )
            content_type = (
                str(response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
            )
            if content_type and not (
                content_type.startswith("video/")
                or content_type
                in {
                    "application/mp4",
                    "application/octet-stream",
                    "binary/octet-stream",
                }
            ):
                raise _DouyinProbeRejected("media endpoint did not return video data")
            payload = bytearray()
            while len(payload) < DOUYIN_PROBE_BYTES:
                if should_cancel():
                    raise DownloadCancelled("Task cancelled")
                chunk = response.read(DOUYIN_PROBE_BYTES - len(payload))
                if not chunk:
                    break
                payload.extend(chunk)
            if len(payload) < 12 or payload[4:8] != b"ftyp":
                raise _DouyinProbeRejected("media endpoint did not return an MP4 file")
            content_range = str(response.headers.get("Content-Range") or "")
            size_match = re.search(r"/(\d+)$", content_range)
            filesize = int(size_match.group(1)) if size_match else None
            content_length = str(response.headers.get("Content-Length") or "")
            try:
                response_status = int(getattr(response, "status", 200) or 200)
            except (TypeError, ValueError):
                response_status = 200
            if (
                filesize is None
                and response_status != 206
                and content_length.isdigit()
                and int(content_length) >= len(payload)
            ):
                filesize = int(content_length)
        finally:
            with contextlib.suppress(Exception):
                response.close()

        if should_cancel():
            raise DownloadCancelled("Task cancelled")
        media = self._ffprobe_douyin_media(
            bytes(payload),
            should_cancel=should_cancel,
        )
        if not self._douyin_probe_metadata_complete(media, filesize=filesize):
            with tempfile.TemporaryDirectory(
                prefix="original-media-douyin-probe-"
            ) as temporary_directory:
                local_path = Path(temporary_directory) / "candidate.mp4"
                self._download_douyin_probe_file(
                    ydl,
                    candidate_url,
                    local_path,
                    expected_prefix=bytes(payload),
                    expected_filesize=filesize,
                    should_cancel=should_cancel,
                )
                media = self._ffprobe_douyin_media(
                    bytes(payload),
                    local_path=local_path,
                    should_cancel=should_cancel,
                )
        if not media:
            raise _DouyinProbeRejected("FFprobe could not parse the media stream")
        width = int(media.get("width") or 0)
        height = int(media.get("height") or 0)
        if width <= 0 or height <= 0:
            raise _DouyinProbeRejected("FFprobe returned no video dimensions")
        duration = self._float_or_none(media.get("duration"))
        if duration is None or duration <= 0:
            raise _DouyinProbeRejected("FFprobe returned no media duration")
        if expected_duration is not None:
            tolerance = self._douyin_duration_tolerance(expected_duration)
            if abs(duration - expected_duration) > tolerance:
                raise _DouyinProbeRejected(
                    "media duration did not match the requested Douyin item"
                )
        bit_rate = int(media.get("bit_rate") or 0)
        if bit_rate <= 0 and filesize and duration and duration > 0:
            bit_rate = int(filesize * 8 / duration)
        if not filesize and bit_rate <= 0:
            raise _DouyinProbeRejected(
                "FFprobe returned no bitrate or complete media size"
            )
        return {
            **media,
            "url": final_url,
            "filesize": filesize,
            "bit_rate": bit_rate,
            "probe_prefix_size": len(payload),
            "probe_prefix_sha256": hashlib.sha256(payload).hexdigest(),
            "source_url": candidate_url,
        }

    @staticmethod
    def _douyin_duration_tolerance(duration: float) -> float:
        return max(0.5, min(2.0, duration * 0.01))

    def _download_douyin_probe_file(
        self,
        ydl: YoutubeDL,
        candidate_url: str,
        path: Path,
        *,
        expected_prefix: bytes,
        expected_filesize: int | None,
        should_cancel: CancelCallback,
    ) -> None:
        if should_cancel():
            raise DownloadCancelled("Task cancelled")
        if not self._is_trusted_douyin_asset_url(
            candidate_url,
            MediaType.VIDEO,
        ):
            raise _DouyinProbeRejected(
                "untrusted initial Douyin media endpoint was blocked"
            )
        if (
            expected_filesize is not None
            and expected_filesize > DOUYIN_MAX_PROBE_FILE_BYTES
        ):
            raise _DouyinProbeRejected(
                "media file exceeded the safe probe size limit"
            )
        response = self._open_douyin_media_response(
            ydl,
            Request(
                candidate_url,
                headers=dict(DOUYIN_MEDIA_HEADERS),
                extensions={"timeout": DOUYIN_PROBE_HTTP_TIMEOUT_SECONDS},
            ),
            redirect_rejection_reason=lambda value: (
                self._douyin_media_redirect_rejection_reason(
                    value,
                    MediaType.VIDEO,
                    allow_verified_regional=True,
                )
            ),
            should_cancel=should_cancel,
        )
        downloaded = 0
        prefix = bytearray()
        try:
            final_url = str(response.url)
            redirect_reason = self._douyin_media_redirect_rejection_reason(
                final_url,
                MediaType.VIDEO,
                allow_verified_regional=True,
            )
            if redirect_reason is not None:
                raise self._douyin_redirect_rejection(
                    final_url,
                    redirect_reason,
                )
            content_type = (
                str(response.headers.get("Content-Type") or "")
                .split(";", 1)[0]
                .lower()
            )
            if content_type and not (
                content_type.startswith("video/")
                or content_type
                in {
                    "application/mp4",
                    "application/octet-stream",
                    "binary/octet-stream",
                }
            ):
                raise _DouyinProbeRejected(
                    "media endpoint did not return video data"
                )
            declared_length = str(
                response.headers.get("Content-Length") or ""
            )
            if declared_length.isdigit():
                declared_size = int(declared_length)
                if declared_size > DOUYIN_MAX_PROBE_FILE_BYTES:
                    raise _DouyinProbeRejected(
                        "media file exceeded the safe probe size limit"
                    )
                if expected_filesize is not None and declared_size != expected_filesize:
                    raise _DouyinProbeIntegrityChanged(
                        "media size changed between the range probe and local probe"
                    )
            with path.open("wb") as output:
                while True:
                    if should_cancel():
                        raise DownloadCancelled("Task cancelled")
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > DOUYIN_MAX_PROBE_FILE_BYTES:
                        raise _DouyinProbeRejected(
                            "media file exceeded the safe probe size limit"
                        )
                    if expected_filesize is not None and downloaded > expected_filesize:
                        raise _DouyinProbeIntegrityChanged(
                            "media size changed between the range probe and local probe"
                        )
                    if len(prefix) < len(expected_prefix):
                        remaining = len(expected_prefix) - len(prefix)
                        prefix.extend(chunk[:remaining])
                    output.write(chunk)
        finally:
            with contextlib.suppress(Exception):
                response.close()
        if downloaded <= 0:
            raise _DouyinProbeRejected("media endpoint returned an empty file")
        if expected_filesize is not None and downloaded != expected_filesize:
            raise _DouyinProbeIntegrityChanged(
                "media size changed between the range probe and local probe"
            )
        if bytes(prefix) != expected_prefix:
            raise _DouyinProbeIntegrityChanged(
                "media content changed between the range probe and local probe"
            )

    def _ffprobe_douyin_media(
        self,
        initial_bytes: bytes,
        *,
        local_path: Path | None = None,
        should_cancel: CancelCallback,
    ) -> dict[str, Any] | None:
        executable = self._find_ffprobe_executable()
        if not executable:
            raise MediaDownloadError(FFPROBE_REQUIRED_MESSAGE)
        entries = (
            "stream=codec_type,codec_name,width,height,bit_rate,duration:"
            "format=duration,bit_rate,size"
        )
        base_command = [
            executable,
            "-v",
            "error",
            "-show_entries",
            entries,
            "-of",
            "json",
        ]
        if local_path is not None:
            payload = self._run_ffprobe(
                [*base_command, "-i", str(local_path)],
                timeout_seconds=DOUYIN_FFPROBE_FILE_TIMEOUT_SECONDS,
                should_cancel=should_cancel,
            )
            return self._parse_ffprobe_payload(payload)
        try:
            payload = self._run_ffprobe(
                [*base_command, "-i", "pipe:0"],
                input_data=initial_bytes,
                timeout_seconds=DOUYIN_FFPROBE_PIPE_TIMEOUT_SECONDS,
                should_cancel=should_cancel,
            )
        except TimeoutError:
            payload = None
        return self._parse_ffprobe_payload(payload)

    @classmethod
    def _douyin_probe_metadata_complete(
        cls,
        media: dict[str, Any] | None,
        *,
        filesize: int | None = None,
    ) -> bool:
        if not media:
            return False
        return (
            int(media.get("width") or 0) > 0
            and int(media.get("height") or 0) > 0
            and (cls._float_or_none(media.get("duration")) or 0) > 0
            and (int(media.get("bit_rate") or 0) > 0 or (filesize or 0) > 0)
        )

    @staticmethod
    def _find_ffprobe_executable() -> str | None:
        executable = shutil.which("ffprobe")
        if executable:
            return executable
        for value in FFPROBE_FALLBACK_PATHS:
            candidate = Path(value)
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        return None

    def _run_ffprobe(
        self,
        command: list[str],
        *,
        input_data: bytes | None = None,
        timeout_seconds: float,
        should_cancel: CancelCallback,
    ) -> bytes | None:
        try:
            process = subprocess.Popen(
                command,
                stdin=(
                    subprocess.PIPE if input_data is not None else subprocess.DEVNULL
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise MediaDownloadError(FFPROBE_START_MESSAGE) from exc

        deadline = time.monotonic() + max(0.1, timeout_seconds)
        pending_input = input_data
        while True:
            if should_cancel():
                self._terminate_process(process)
                raise DownloadCancelled("Task cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._terminate_process(process)
                raise TimeoutError("FFprobe timed out while reading media")
            try:
                stdout, _ = process.communicate(
                    input=pending_input,
                    timeout=min(DOUYIN_PROCESS_POLL_SECONDS, remaining),
                )
                return stdout or None
            except subprocess.TimeoutExpired:
                pending_input = None

    @staticmethod
    def _terminate_process(process: subprocess.Popen[bytes]) -> None:
        with contextlib.suppress(OSError):
            process.terminate()
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                process.kill()
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                process.wait(timeout=0.5)

    @staticmethod
    def _parse_ffprobe_payload(payload: bytes | None) -> dict[str, Any] | None:
        if not payload:
            return None
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        streams = data.get("streams") or []
        video = next(
            (
                stream
                for stream in streams
                if isinstance(stream, dict) and stream.get("codec_type") == "video"
            ),
            None,
        )
        if not video:
            return None
        audio = next(
            (
                stream
                for stream in streams
                if isinstance(stream, dict) and stream.get("codec_type") == "audio"
            ),
            None,
        )
        format_data = data.get("format") or {}

        def safe_int(value: Any) -> int:
            try:
                parsed = int(value or 0)
            except (TypeError, ValueError):
                return 0
            return parsed if parsed > 0 else 0

        def safe_number(*values: Any) -> Any | None:
            for value in values:
                try:
                    parsed = float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(parsed) and parsed > 0:
                    return value
            return None

        return {
            "width": safe_int(video.get("width")),
            "height": safe_int(video.get("height")),
            "vcodec": video.get("codec_name"),
            "acodec": audio.get("codec_name") if audio else "none",
            "bit_rate": safe_int(
                safe_number(video.get("bit_rate"), format_data.get("bit_rate"))
            ),
            "duration": safe_number(video.get("duration"), format_data.get("duration")),
        }

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        try:
            parsed = float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
        return parsed if parsed is not None and math.isfinite(parsed) else None

    @staticmethod
    def _cleanup_ytdlp_parts_dir(
        parts_dir: Path,
        output_dir: Path,
        *,
        remove_contents: bool,
    ) -> None:
        parts_root = output_dir / ".parts"
        try:
            relative = parts_dir.relative_to(parts_root)
        except ValueError:
            return
        if len(relative.parts) != 2:
            return
        scoped_directories = (parts_root, parts_dir.parent, parts_dir)
        try:
            if any(
                directory.is_symlink() or directory.resolve() != directory
                for directory in scoped_directories
            ):
                return
        except OSError:
            return
        if remove_contents:
            with contextlib.suppress(OSError):
                shutil.rmtree(parts_dir)
        else:
            with contextlib.suppress(OSError):
                parts_dir.rmdir()
        for directory in (parts_dir.parent, parts_root):
            with contextlib.suppress(OSError):
                directory.rmdir()

    @staticmethod
    def _prepare_ytdlp_parts_dir(parts_dir: Path, output_dir: Path) -> None:
        parts_root = output_dir / ".parts"
        try:
            relative = parts_dir.relative_to(parts_root)
        except ValueError as exc:
            raise MediaDownloadError(
                "The temporary download directory is outside the output directory"
            ) from exc
        if len(relative.parts) != 2:
            raise MediaDownloadError("The temporary download directory is invalid")
        for directory in (parts_root, parts_dir.parent, parts_dir):
            try:
                if directory.is_symlink():
                    raise MediaDownloadError(
                        "The temporary download directory contains a symbolic link"
                    )
                directory.mkdir(exist_ok=True)
                if (
                    directory.is_symlink()
                    or not directory.is_dir()
                    or directory.resolve() != directory
                ):
                    raise MediaDownloadError(
                        "The temporary download directory could not be verified"
                    )
            except MediaDownloadError:
                raise
            except OSError as exc:
                raise MediaDownloadError(
                    "The temporary download directory could not be created"
                ) from exc

    @staticmethod
    def _highest_verified_douyin_format(
        info: dict[str, Any],
    ) -> dict[str, Any] | None:
        verified = [
            value
            for value in info.get("formats") or []
            if isinstance(value, dict)
            and str(value.get("format_id") or "").startswith("douyin-api-")
            and isinstance(value.get("url"), str)
        ]
        return max(
            verified,
            key=lambda value: (
                int(value.get("width") or 0) * int(value.get("height") or 0),
                int(float(value.get("tbr") or 0) * 1_000),
                int(value.get("filesize") or 0),
                str(value.get("_douyin_requested_ratio") or "") == "default",
            ),
            default=None,
        )

    def _download_with_ytdlp(
        self,
        item: DownloadItem,
        output_dir: Path,
        *,
        platform: Platform,
        callback: EventCallback | None,
        should_cancel: CancelCallback,
    ) -> DownloadOutcome:
        final_paths: list[str] = []
        verification_url = str(
            item.metadata.get("profile_url")
            or item.metadata.get("verification_url")
            or item.source_url
        )
        expected_douyin_id: str | None = None
        expected_douyin_profile_id: str | None = None
        if platform == Platform.DOUYIN:
            expected_douyin_profile_id = self._douyin_profile_id(
                str(item.metadata.get("profile_url") or "")
            )
            expected_douyin_id = item.media_id or self._douyin_video_id(item.source_url)
            source_id = self._douyin_video_id(item.source_url)
            if not expected_douyin_id or source_id != expected_douyin_id:
                self._raise_douyin_mismatch(
                    expected_douyin_id or "unknown",
                    source_id or "missing",
                    verification_url,
                )

        def emit(event: EngineEvent) -> None:
            if callback:
                callback(event)

        def progress_hook(data: dict[str, Any]) -> None:
            if should_cancel():
                raise DownloadCancelled("Task cancelled")
            status = str(data.get("status") or "")
            info = data.get("info_dict") or {}
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = int(data.get("downloaded_bytes") or 0)
            percent = None
            if total:
                percent = min(100.0, downloaded * 100.0 / int(total))
            emit(
                EngineEvent(
                    event=status or "progress",
                    progress=TransferProgress(
                        downloaded_bytes=downloaded,
                        total_bytes=int(total) if total else None,
                        percent=percent,
                        speed_bytes_per_second=data.get("speed"),
                        eta_seconds=data.get("eta"),
                        fragment_index=data.get("fragment_index"),
                        fragment_count=data.get("fragment_count"),
                        filename=data.get("filename"),
                    ),
                    title=info.get("title"),
                    upload_date=self._normalize_date(info.get("upload_date")),
                    author=self._author_from_info(info, fallback=None),
                    media_type=self._media_type(info),
                )
            )

        def postprocessor_hook(data: dict[str, Any]) -> None:
            if should_cancel():
                raise DownloadCancelled("Task cancelled")
            info = data.get("info_dict") or {}
            emit(
                EngineEvent(
                    event="postprocessing",
                    title=info.get("title"),
                    selected_format=self._format_id(info),
                    resolution=self._resolution(info),
                )
            )

        def post_hook(filename: str) -> None:
            final_paths.append(str(Path(filename).resolve()))

        def match_filter(info: dict[str, Any], *, incomplete: bool = False) -> None:
            if not expected_douyin_id:
                return None
            if incomplete and not info.get("id"):
                return None
            self._validate_douyin_info(
                info,
                expected_douyin_id,
                verification_url,
                expected_douyin_profile_id,
            )
            return None

        job_scope = safe_component(
            str(item.metadata.get("_job_id") or "standalone"),
            fallback="standalone",
            limit=80,
        )
        item_scope = safe_component(item.id, fallback="item", limit=80)
        parts_dir = output_dir / ".parts" / job_scope / item_scope

        def operation(use_cookies: bool) -> tuple[dict[str, Any], list[str]]:
            final_paths.clear()
            self._prepare_ytdlp_parts_dir(parts_dir, output_dir)
            logger = _YdlLogger(callback)
            options = {
                **self._base_options(use_cookies),
                **self._download_format_options(platform),
                "paths": {
                    "home": str(output_dir),
                    "temp": str(parts_dir),
                },
                "outtmpl": {"default": OUTPUT_TEMPLATE},
                "windowsfilenames": True,
                "continuedl": platform != Platform.DOUYIN,
                "overwrites": True,
                "ignoreerrors": False,
                "noplaylist": True,
                "concurrent_fragment_downloads": self.config.concurrent_fragments,
                "progress_hooks": [progress_hook],
                "postprocessor_hooks": [postprocessor_hook],
                "post_hooks": [post_hook],
                "logger": logger,
            }
            if expected_douyin_id:
                options["match_filter"] = match_filter
            with YoutubeDL(options) as ydl:
                ydl.add_post_processor(
                    _SafeFilenamePostProcessor(
                        ydl,
                        title_byte_limit=self.config.filename_limit,
                        fallback_media_id=item.media_id or item.id,
                    ),
                    when="pre_process",
                )
                if expected_douyin_id:
                    raw_result = self._extract_douyin_raw_info(
                        ydl,
                        item.source_url,
                        expected_id=expected_douyin_id,
                        expected_profile_id=expected_douyin_profile_id,
                        verification_url=verification_url,
                        profile_metadata=item.metadata,
                        fallback_title=item.title,
                        should_cancel=should_cancel,
                    )
                    self._validate_douyin_info(
                        raw_result,
                        expected_douyin_id,
                        verification_url,
                        expected_douyin_profile_id,
                    )
                    probe_added = self._add_douyin_probe_formats(
                        ydl,
                        raw_result,
                        expected_id=expected_douyin_id,
                        verification_url=verification_url,
                        expected_profile_id=expected_douyin_profile_id,
                        callback=emit,
                        should_cancel=should_cancel,
                    )
                    if not probe_added:
                        probe_failure = str(
                            raw_result.get("_douyin_probe_failure")
                            or "no playable candidate was returned"
                        )
                        raise DownloadError(
                            "Douyin media was discovered, but its highest "
                            "quality could not be verified. Retry this item; no "
                            "lower-quality fallback was downloaded. Probe details: "
                            f"{probe_failure}."
                        )
                    selected = self._highest_verified_douyin_format(raw_result)
                    if not selected:
                        raise DownloadError(
                            "Douyin quality probes returned no verified downloadable "
                            "format"
                        )
                    selected_size = int(selected.get("filesize") or 0)
                    selected_bit_rate = int(
                        float(selected.get("tbr") or 0) * 1_000
                    )
                    redirect_source_url = str(
                        selected.get("_douyin_probe_source_url") or ""
                    ).strip()
                    selected_candidates = list(
                        dict.fromkeys(
                            value
                            for value in (
                                redirect_source_url,
                                str(selected["url"]),
                                *(
                                    selected.get("_douyin_probe_source_urls")
                                    or []
                                ),
                            )
                            if isinstance(value, str) and value
                        )
                    )
                    title = str(raw_result.get("title") or item.title or "").strip()
                    if (
                        not title
                        or title == expected_douyin_id
                        or title.isdigit()
                    ):
                        title = "Untitled Douyin video"
                    selected_asset = RemoteAsset(
                        candidates=selected_candidates,
                        index=1,
                        width=int(selected.get("width") or 0) or None,
                        height=int(selected.get("height") or 0) or None,
                        size=selected_size or None,
                        format_id=str(selected.get("format_id") or "verified"),
                        duration=(
                            self._float_or_none(raw_result.get("duration"))
                            or self._float_or_none(selected.get("duration"))
                        ),
                        bit_rate=selected_bit_rate or None,
                        video_codec=str(selected.get("vcodec") or "").lower()
                        or None,
                        audio_codec=str(selected.get("acodec") or "").lower()
                        or None,
                        probe_prefix_size=int(
                            selected.get("_douyin_probe_prefix_size") or 0
                        )
                        or None,
                        probe_prefix_sha256=str(
                            selected.get("_douyin_probe_prefix_sha256") or ""
                        )
                        or None,
                        redirect_source_url=redirect_source_url or None,
                    )
                    path, chosen = self._download_first_available_asset(
                        ydl,
                        [selected_asset],
                        output_dir,
                        self._normalize_date(raw_result.get("upload_date")),
                        title,
                        expected_douyin_id,
                        item.source_url,
                        platform=Platform.DOUYIN,
                        media_type=MediaType.VIDEO,
                        callback=callback,
                        should_cancel=should_cancel,
                        verify_declared_dimensions=True,
                        require_quality_fingerprint=True,
                    )
                    result = {
                        **raw_result,
                        "title": title,
                        "format_id": chosen.format_id,
                        "width": chosen.width,
                        "height": chosen.height,
                        "filepath": str(path),
                        "requested_formats": [dict(selected)],
                        "requested_downloads": [
                            {**dict(selected), "filepath": str(path)}
                        ],
                        "_verified_local_resolution": (
                            f"{chosen.width}x{chosen.height}"
                        ),
                    }
                    candidates = [str(path)]
                else:
                    result = ydl.extract_info(item.source_url, download=True)
                    if not isinstance(result, dict):
                        raise DownloadError("The URL returned no downloadable media")
                    candidates = self._paths_from_info(result, ydl)
                if not isinstance(result, dict):
                    raise DownloadError("The URL returned no downloadable media")
                if expected_douyin_id:
                    self._validate_douyin_info(
                        result,
                        expected_douyin_id,
                        verification_url,
                        expected_douyin_profile_id,
                    )
            paths = [*final_paths, *candidates]
            return result, paths

        operation_succeeded = False
        try:
            try:
                (info, paths), fallback = self._run_with_cookie_fallback(
                    operation, url=item.source_url
                )
                operation_succeeded = True
            except AuthenticationRequiredError as exc:
                if (
                    platform == Platform.DOUYIN
                    and exc.verification_url != verification_url
                ):
                    raise AuthenticationRequiredError(
                        str(exc), verification_url=verification_url
                    ) from exc
                raise
        finally:
            self._cleanup_ytdlp_parts_dir(
                parts_dir,
                output_dir,
                remove_contents=(
                    platform == Platform.DOUYIN or operation_succeeded
                ),
            )
        unique_paths = self._existing_unique_paths(paths)
        if not unique_paths:
            raise MediaDownloadError("yt-dlp finished but no output file was found")
        outcome = DownloadOutcome(
            output_paths=unique_paths,
            title=info.get("title"),
            upload_date=self._normalize_date(info.get("upload_date")),
            author=self._author_from_info(info, fallback=None),
            media_type=self._media_type(info),
            selected_format=self._format_id(info),
            resolution=(
                str(info.get("_verified_local_resolution"))
                if expected_douyin_id
                else self._resolution(info)
            ),
            cookie_fallback_used=fallback,
        )
        emit(
            EngineEvent(
                event="completed",
                title=outcome.title,
                upload_date=outcome.upload_date,
                author=outcome.author,
                media_type=outcome.media_type,
                selected_format=outcome.selected_format,
                resolution=outcome.resolution,
                output_paths=outcome.output_paths,
                cookie_fallback_used=fallback,
            )
        )
        return outcome

    def _download_douyin_profile_image(
        self,
        item: DownloadItem,
        output_dir: Path,
        *,
        callback: EventCallback | None,
        should_cancel: CancelCallback,
    ) -> DownloadOutcome:
        if should_cancel():
            raise DownloadCancelledError("Task cancelled")
        profile_url = str(item.metadata.get("profile_url") or "").strip()
        profile_id = self._douyin_profile_id(profile_url)
        media_id = str(item.media_id or "").strip()
        canonical_item_url = (
            f"https://www.douyin.com/video/{media_id}" if media_id else ""
        )
        try:
            profile_source = identify_url(profile_url)
            item_source = identify_url(item.source_url)
        except (TypeError, ValueError):
            profile_source = None
            item_source = None
        cached = item.metadata.get("douyin_profile_media")
        if (
            item.metadata.get("profile_owner_verified") is not True
            or not profile_id
            or not media_id
            or profile_source is None
            or profile_source.platform != Platform.DOUYIN
            or profile_source.kind != SourceKind.PROFILE
            or profile_source.url != profile_url
            or item_source is None
            or item_source.platform != Platform.DOUYIN
            or item_source.kind != SourceKind.ITEM
            or item_source.url != canonical_item_url
            or item.source_url != canonical_item_url
            or not isinstance(cached, dict)
            or cached.get("media_kind") != "image"
        ):
            raise MediaDownloadError(
                "Douyin profile image metadata is incomplete or belongs to a "
                "different item. Create a new task from the original profile."
            )
        if not is_complete_profile_media_metadata(cached, media_id, profile_id):
            live_assets = cached.get("live_photo_assets")
            if isinstance(live_assets, list) and any(
                isinstance(value, dict)
                and value.get("direct_candidates") is None
                for value in live_assets
            ):
                raise TemporaryAccessError(
                    "This saved Douyin Live Photo task predates author-feed "
                    "highest-quality verification. The task was paused instead of "
                    "downloading a default-only fallback. Create a new task from the "
                    "original profile."
                )
            raise MediaDownloadError(
                "Douyin profile image metadata is incomplete. Create a new task "
                "from the original profile."
            )

        title = str(cached.get("title") or item.title or "").strip()
        if not title or title == media_id or title.isdigit():
            title = "Untitled Douyin image"
        author = str(cached.get("author") or item.author or "Douyin Author").strip()
        create_time = cached.get("create_time")
        if type(create_time) is not int or create_time <= 0:
            raise MediaDownloadError("Douyin profile image publish time is invalid")
        upload_date = self._douyin_upload_date(create_time)
        image_assets = self._douyin_cached_assets(
            cached.get("image_assets"),
            format_prefix="douyin-highest-image",
        )
        live_photo_assets = self._douyin_cached_assets(
            cached.get("live_photo_assets") or [],
            format_prefix="douyin-highest-live-photo",
        )
        if not image_assets:
            raise MediaDownloadError(
                "Douyin profile image has no highest-available assets"
            )

        if callback:
            callback(
                EngineEvent(
                    event="metadata",
                    title=title,
                    upload_date=upload_date,
                    author=author,
                    media_type=MediaType.IMAGE,
                )
            )

        output_paths: list[str] = []
        completed_assets: list[RemoteAsset] = []
        total_assets = len(image_assets) + len(live_photo_assets)
        progress_index = 0
        with YoutubeDL(self._base_options(False)) as ydl:
            for asset in image_assets:
                progress_index += 1
                reused = self._existing_douyin_image_asset(
                    item,
                    output_dir,
                    media_id,
                    asset,
                )
                if reused:
                    path, chosen = reused
                    if callback:
                        callback(
                            EngineEvent(
                                event="downloading",
                                progress=TransferProgress(
                                    downloaded_bytes=int(chosen.size or 0),
                                    total_bytes=chosen.size,
                                    percent=progress_index * 100.0 / total_assets,
                                    fragment_index=progress_index,
                                    fragment_count=total_assets,
                                    filename=str(path),
                                ),
                            )
                        )
                else:
                    try:
                        path, chosen = self._download_first_available_asset(
                            ydl,
                            [asset],
                            output_dir,
                            upload_date,
                            title,
                            media_id,
                            profile_url,
                            platform=Platform.DOUYIN,
                            media_type=MediaType.IMAGE,
                            callback=callback,
                            should_cancel=should_cancel,
                            asset_index=asset.index,
                            progress_index=progress_index,
                            progress_count=total_assets,
                            verify_declared_dimensions=True,
                        )
                    except DownloadCancelledError:
                        raise
                    except TemporaryAccessError:
                        raise
                    except Exception as exc:
                        raise MediaDownloadError(
                            f"Image {asset.index} failed: "
                            f"{safe_external_error_message(exc)}"
                        ) from exc
                output_paths.append(str(path))
                completed_assets.append(chosen)
                if callback:
                    callback(
                        EngineEvent(
                            event="asset_completed",
                            output_paths=list(output_paths),
                            selected_format=chosen.format_id,
                            resolution=self._asset_resolution(chosen),
                        )
                    )

            for asset in live_photo_assets:
                progress_index += 1
                try:
                    asset = self._select_highest_douyin_live_photo_asset(
                        ydl,
                        asset,
                        callback=callback,
                        should_cancel=should_cancel,
                    )
                    path, chosen = self._download_first_available_asset(
                        ydl,
                        [asset],
                        output_dir,
                        upload_date,
                        title,
                        media_id,
                        profile_url,
                        platform=Platform.DOUYIN,
                        media_type=MediaType.VIDEO,
                        callback=callback,
                        should_cancel=should_cancel,
                        asset_index=asset.index,
                        progress_index=progress_index,
                        progress_count=total_assets,
                        verify_declared_dimensions=True,
                        require_quality_fingerprint=True,
                    )
                except DownloadCancelledError:
                    raise
                except TemporaryAccessError:
                    raise
                except Exception as exc:
                    raise MediaDownloadError(
                        f"Live Photo {asset.index} failed: "
                        f"{safe_external_error_message(exc)}"
                    ) from exc
                output_paths.append(str(path))
                completed_assets.append(chosen)
                if callback:
                    callback(
                        EngineEvent(
                            event="asset_completed",
                            output_paths=list(output_paths),
                            selected_format=chosen.format_id,
                            resolution=self._asset_resolution(chosen),
                        )
                    )

        largest = max(
            completed_assets,
            key=lambda value: int(value.width or 0) * int(value.height or 0),
        )
        selected_format = (
            "douyin-highest-images+live-photos"
            if live_photo_assets
            else "douyin-highest-images"
        )
        outcome = DownloadOutcome(
            output_paths=output_paths,
            title=title,
            upload_date=upload_date,
            author=author,
            media_type=MediaType.IMAGE,
            selected_format=selected_format,
            resolution=self._asset_resolution(largest),
        )
        if callback:
            callback(
                EngineEvent(
                    event="completed",
                    title=outcome.title,
                    upload_date=outcome.upload_date,
                    author=outcome.author,
                    media_type=outcome.media_type,
                    selected_format=outcome.selected_format,
                    resolution=outcome.resolution,
                    output_paths=outcome.output_paths,
                )
            )
        return outcome

    def _select_highest_douyin_live_photo_asset(
        self,
        ydl: YoutubeDL,
        asset: RemoteAsset,
        *,
        callback: EventCallback | None,
        should_cancel: CancelCallback,
    ) -> RemoteAsset:
        video_uri = str(asset.video_uri or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", video_uri):
            raise MediaDownloadError(
                "Douyin Live Photo has no verified media identity"
            )

        if not asset.quality_candidates:
            raise TemporaryAccessError(
                "Douyin Live Photo has no structured author-feed quality "
                "renditions. The task was paused instead of downloading a "
                "default-only fallback. Create a new task from the original profile."
            )
        renditions = asset.quality_candidates
        direct_probes: list[dict[str, Any]] = []
        direct_failures: list[tuple[dict[str, Any], str, str, bool]] = []
        probe_count = len(renditions) + 1
        for rendition_index, rendition in enumerate(renditions, start=1):
            if should_cancel():
                raise DownloadCancelledError("Task cancelled")
            label = f"author-feed-{rendition_index}"
            if callback:
                callback(
                    EngineEvent(
                        event="probing",
                        message=(
                            "Checking Douyin Live Photo quality "
                            f"{rendition_index}/{probe_count}: {label}"
                        ),
                    )
                )
            errors: list[tuple[str, bool]] = []
            direct_probe: dict[str, Any] | None = None
            for candidate_url in rendition.get("urls") or []:
                try:
                    direct_probe = self._probe_douyin_ratio_with_retry(
                        ydl,
                        candidate_url,
                        ratio=label,
                        expected_duration=asset.duration,
                        callback=callback,
                        should_cancel=should_cancel,
                    )
                except DownloadCancelled as exc:
                    raise DownloadCancelledError("Task cancelled") from exc
                except MediaDownloadError:
                    raise
                except Exception as exc:
                    errors.append(
                        (
                            self._safe_douyin_probe_failure(exc),
                            self._should_pause_douyin_probe_error(exc),
                        )
                    )
                    continue
                if direct_probe:
                    break
                errors.append(("media metadata could not be parsed", False))

            declared_width = int(rendition.get("width") or 0)
            declared_height = int(rendition.get("height") or 0)
            if direct_probe:
                direct_probe["source_candidates"] = list(
                    dict.fromkeys(
                        [
                            str(direct_probe.get("source_url") or ""),
                            *(rendition.get("urls") or []),
                        ]
                    )
                )
                direct_probe["source_candidates"] = [
                    value
                    for value in direct_probe["source_candidates"]
                    if value
                    and self._is_trusted_douyin_asset_url(
                        value,
                        MediaType.VIDEO,
                    )
                ]
                actual_width = int(direct_probe.get("width") or 0)
                actual_height = int(direct_probe.get("height") or 0)
                declared_too_high = bool(
                    declared_width
                    and declared_height
                    and (
                        min(actual_width, actual_height)
                        < min(declared_width, declared_height)
                        or max(actual_width, actual_height)
                        < max(declared_width, declared_height)
                    )
                )
                if actual_width <= 0 or actual_height <= 0 or declared_too_high:
                    direct_failures.append(
                        (
                            rendition,
                            label,
                            (
                                "verified media was below the author-feed "
                                f"{declared_width}x{declared_height} rendition"
                            ),
                            False,
                        )
                    )
                    continue
                direct_probe["requested_ratio"] = label
                direct_probes.append(direct_probe)
                continue

            reason = errors[-1][0] if errors else "no media URL was available"
            direct_failures.append(
                (
                    rendition,
                    label,
                    reason,
                    any(transient for _, transient in errors),
                )
            )

        if callback:
            callback(
                EngineEvent(
                    event="probing",
                    message=(
                        "Checking Douyin Live Photo quality "
                        f"{probe_count}/{probe_count}: default"
                    ),
                )
            )
        try:
            default_probe = self._probe_douyin_default_with_fallback(
                ydl,
                video_uri,
                expected_duration=asset.duration,
                callback=callback,
                should_cancel=should_cancel,
            )
        except DownloadCancelled as exc:
            raise DownloadCancelledError("Task cancelled") from exc
        except MediaDownloadError:
            raise
        except Exception as exc:
            reason = self._safe_douyin_probe_failure(exc)
            if self._should_pause_douyin_probe_error(exc):
                raise TemporaryAccessError(
                    "Douyin Live Photo authoritative quality source was temporarily "
                    "unavailable after automatic retries. The task was paused before "
                    "probing later items; wait briefly and continue the task. No "
                    "lower-quality fallback was downloaded. Probe details: "
                    f"default: {reason}"
                ) from exc
            raise MediaDownloadError(
                "Douyin Live Photo default original-quality source could not be "
                f"verified. Probe details: default: {reason}"
            ) from exc
        if not default_probe:
            raise MediaDownloadError(
                "Douyin Live Photo default original-quality source returned no "
                "playable media metadata"
            )
        default_probe["requested_ratio"] = "default"
        probes = [*direct_probes, default_probe]

        unresolved = self._unresolved_douyin_direct_failures(
            direct_failures,
            probes,
        )
        if unresolved:
            details = self._summarize_douyin_probe_failures(
                [(label, reason) for label, reason, _ in unresolved]
            )
            if any(transient for _, _, transient in unresolved):
                raise TemporaryAccessError(
                    "Douyin Live Photo authoritative quality source was temporarily "
                    "unavailable after automatic retries. The task was paused before "
                    "probing later items; wait briefly and continue the task. No "
                    f"lower-quality fallback was downloaded. Probe details: {details}"
                )
            raise MediaDownloadError(
                "Douyin Live Photo author-feed quality source could not be verified. "
                f"Probe details: {details}"
            )

        supported: list[dict[str, Any]] = []
        unsupported: list[dict[str, Any]] = []
        for probe in probes:
            if str(probe.get("vcodec") or "").lower() in {
                "bytevc2",
                "h266",
                "vvc",
            }:
                unsupported.append(probe)
            else:
                supported.append(probe)
        if not supported:
            raise MediaDownloadError(
                "Douyin Live Photo returned no supported playable candidate"
            )
        best = max(
            supported,
            key=lambda value: (
                *self._douyin_probe_quality_key(value),
                str(value.get("requested_ratio") or "") == "default",
            ),
        )
        best_identity = self._douyin_probe_media_identity_key(best)
        for probe in supported:
            if (
                probe is not best
                and self._douyin_probe_media_identity_key(probe) == best_identity
            ):
                self._merge_douyin_probe_source_candidates(best, probe)
        best_rank = self._douyin_probe_quality_key(best)
        if any(self._douyin_probe_quality_key(value) > best_rank for value in unsupported):
            raise MediaDownloadError(
                "Douyin Live Photo highest candidate uses an unsupported video codec"
            )

        actual_width = int(best.get("width") or 0)
        actual_height = int(best.get("height") or 0)
        declared_too_high = bool(
            asset.width
            and asset.height
            and (
                min(actual_width, actual_height) < min(asset.width, asset.height)
                or max(actual_width, actual_height) < max(asset.width, asset.height)
            )
        )
        if actual_width <= 0 or actual_height <= 0 or declared_too_high:
            raise MediaDownloadError(
                "Douyin Live Photo best verified candidate was below its declared "
                f"{asset.width}x{asset.height} resolution"
            )
        requested_ratio = safe_component(
            str(best.get("requested_ratio") or "verified"),
            fallback="verified",
        )
        redirect_source_url = str(best.get("source_url") or "").strip()
        best_urls = [
            value
            for value in (
                redirect_source_url,
                str(best["url"]),
                *(best.get("source_candidates") or []),
            )
            if isinstance(value, str) and value
        ]
        if requested_ratio in DOUYIN_PROBE_RATIOS:
            if requested_ratio == "default":
                best_urls.extend(self._douyin_default_probe_urls(video_uri))
            else:
                best_urls.append(
                    self._douyin_ratio_url(video_uri, requested_ratio)
                )
        elif requested_ratio.startswith("author-feed-"):
            with contextlib.suppress(ValueError, IndexError):
                rendition_index = int(requested_ratio.rsplit("-", 1)[1]) - 1
                best_urls.extend(renditions[rendition_index].get("urls") or [])
        best_urls = list(dict.fromkeys(best_urls))
        return RemoteAsset(
            candidates=best_urls,
            index=asset.index,
            width=actual_width,
            height=actual_height,
            size=(int(best["filesize"]) if best.get("filesize") else None),
            format_id=(
                "douyin-highest-live-photo-"
                f"{requested_ratio}-{actual_width}x{actual_height}"
            ),
            video_uri=video_uri,
            duration=self._float_or_none(best.get("duration")) or asset.duration,
            bit_rate=(int(best["bit_rate"]) if best.get("bit_rate") else None),
            video_codec=str(best.get("vcodec") or "").lower() or None,
            audio_codec=str(best.get("acodec") or "").lower() or None,
            probe_prefix_size=int(best.get("probe_prefix_size") or 0) or None,
            probe_prefix_sha256=str(best.get("probe_prefix_sha256") or "") or None,
            redirect_source_url=redirect_source_url or None,
        )

    @staticmethod
    def _douyin_probe_quality_key(value: dict[str, Any]) -> tuple[int, int, int]:
        return (
            int(value.get("width") or 0) * int(value.get("height") or 0),
            int(value.get("bit_rate") or 0),
            int(value.get("filesize") or 0),
        )

    @classmethod
    def _douyin_probe_media_identity_key(
        cls,
        value: dict[str, Any],
    ) -> tuple[Any, ...]:
        duration = cls._float_or_none(value.get("duration"))
        return (
            int(value.get("width") or 0),
            int(value.get("height") or 0),
            str(value.get("vcodec") or "").lower(),
            str(value.get("acodec") or "").lower(),
            int(value.get("bit_rate") or 0),
            int(value.get("filesize") or 0),
            round(duration, 6) if duration is not None else None,
            int(value.get("probe_prefix_size") or 0),
            str(value.get("probe_prefix_sha256") or "").lower(),
        )

    @classmethod
    def _merge_douyin_probe_source_candidates(
        cls,
        target: dict[str, Any],
        source: dict[str, Any],
    ) -> None:
        candidates = [
            value
            for value in (
                str(target.get("source_url") or ""),
                str(target.get("url") or ""),
                *(target.get("source_candidates") or []),
                str(source.get("source_url") or ""),
                str(source.get("url") or ""),
                *(source.get("source_candidates") or []),
            )
            if isinstance(value, str)
            and value
            and (
                cls._is_trusted_douyin_asset_url(value, MediaType.VIDEO)
                or cls._is_douyin_regional_media_host(
                    cls._strict_https_hostname(value)
                )
            )
        ]
        target["source_candidates"] = list(dict.fromkeys(candidates))

    @classmethod
    def _unresolved_douyin_direct_failures(
        cls,
        failures: list[tuple[dict[str, Any], str, str, bool]],
        verified_probes: list[dict[str, Any]],
    ) -> list[tuple[str, str, bool]]:
        best = max(
            verified_probes,
            key=cls._douyin_probe_quality_key,
            default=None,
        )
        best_pixels = (
            int(best.get("width") or 0) * int(best.get("height") or 0)
            if best
            else 0
        )
        best_bit_rate = int(best.get("bit_rate") or 0) if best else 0
        unresolved: list[tuple[str, str, bool]] = []
        for rendition, label, reason, transient in failures:
            declared_pixels = int(rendition.get("width") or 0) * int(
                rendition.get("height") or 0
            )
            declared_bit_rate = int(rendition.get("bit_rate") or 0)
            dominated = best_pixels > declared_pixels or (
                best_pixels == declared_pixels
                and declared_bit_rate > 0
                and best_bit_rate >= declared_bit_rate
            )
            if not dominated:
                unresolved.append((label, reason, transient))
        return unresolved

    def _existing_douyin_image_asset(
        self,
        item: DownloadItem,
        output_dir: Path,
        media_id: str,
        asset: RemoteAsset,
    ) -> tuple[Path, RemoteAsset] | None:
        expected_suffix = re.compile(
            rf"\[{re.escape(media_id)}\]-{asset.index:03d}\.[^.]+$"
        )
        for value in item.output_paths:
            candidate = Path(value)
            try:
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                resolved = candidate.resolve(strict=True)
                if resolved.parent != output_dir or not expected_suffix.search(
                    resolved.name
                ):
                    continue
                with resolved.open("rb") as handle:
                    dimensions = self._image_dimensions(handle.read(1024 * 1024))
                size = resolved.stat().st_size
            except OSError:
                continue
            if not dimensions:
                continue
            width, height = dimensions
            if asset.width and asset.height and (
                min(width, height) < min(asset.width, asset.height)
                or max(width, height) < max(asset.width, asset.height)
            ):
                continue
            return (
                resolved,
                RemoteAsset(
                    candidates=list(asset.candidates),
                    index=asset.index,
                    width=width,
                    height=height,
                    size=size,
                    format_id=asset.format_id,
                ),
            )
        return None

    @classmethod
    def _douyin_cached_assets(
        cls,
        values: Any,
        *,
        format_prefix: str,
    ) -> list[RemoteAsset]:
        if not isinstance(values, list):
            return []
        result: list[RemoteAsset] = []
        for value in values:
            if not isinstance(value, dict):
                continue
            try:
                index = int(value.get("index") or 0)
                width = int(value.get("width") or 0)
                height = int(value.get("height") or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            candidates = [
                candidate
                for candidate in value.get("candidates") or []
                if isinstance(candidate, str) and candidate
            ]
            if index <= 0 or width <= 0 or height <= 0 or not candidates:
                continue
            quality_candidates: list[dict[str, Any]] | None = None
            if format_prefix == "douyin-highest-live-photo":
                video_uri = str(value.get("video_uri") or "").strip()
                parsed_renditions: list[dict[str, Any]] = []
                for rendition in value.get("direct_candidates") or []:
                    if not isinstance(rendition, dict):
                        continue
                    try:
                        rendition_width = int(rendition.get("width") or 0)
                        rendition_height = int(rendition.get("height") or 0)
                        rendition_bit_rate = int(rendition.get("bit_rate") or 0)
                    except (TypeError, ValueError, OverflowError):
                        continue
                    rendition_urls = [
                        url
                        for url in rendition.get("urls") or []
                        if isinstance(url, str)
                        and cls._is_trusted_douyin_asset_url(
                            url, MediaType.VIDEO
                        )
                    ][:5]
                    if (
                        rendition_width <= 0
                        or rendition_height <= 0
                        or not rendition_urls
                        or str(rendition.get("video_uri") or "").strip()
                        != video_uri
                    ):
                        continue
                    parsed_rendition: dict[str, Any] = {
                        "width": rendition_width,
                        "height": rendition_height,
                        "urls": list(dict.fromkeys(rendition_urls)),
                    }
                    if rendition_bit_rate > 0:
                        parsed_rendition["bit_rate"] = rendition_bit_rate
                    codec_hint = str(
                        rendition.get("codec_hint") or ""
                    ).strip().lower()
                    if codec_hint in {
                        "h264",
                        "hevc",
                        "h265",
                        "vvc",
                        "h266",
                        "bytevc2",
                    }:
                        parsed_rendition["codec_hint"] = codec_hint
                    parsed_renditions.append(parsed_rendition)
                quality_candidates = parsed_renditions or [
                    {
                        "width": width,
                        "height": height,
                        "urls": list(candidates),
                    }
                ]
            duration_ms = _normalized_douyin_duration_ms(
                value.get("duration_ms")
            )
            result.append(
                RemoteAsset(
                    candidates=candidates,
                    index=index,
                    width=width,
                    height=height,
                    format_id=f"{format_prefix}-{width}x{height}",
                    video_uri=(
                        str(value.get("video_uri") or "").strip() or None
                    ),
                    duration=(
                        duration_ms / 1_000
                        if duration_ms is not None
                        else None
                    ),
                    quality_candidates=quality_candidates,
                )
            )
        return result

    @staticmethod
    def _asset_resolution(asset: RemoteAsset) -> str | None:
        if asset.width and asset.height:
            return f"{asset.width}x{asset.height}"
        return None

    def _download_xhs_item(
        self,
        item: DownloadItem,
        output_dir: Path,
        *,
        callback: EventCallback | None,
        should_cancel: CancelCallback,
    ) -> DownloadOutcome:
        if should_cancel():
            raise DownloadCancelledError("Task cancelled")
        note, fallback = parse_xhs_note(
            item.source_url,
            cookie_profile=self.config.cookie_profile,
            use_browser_cookies=bool(self.config.cookie_browser),
            allow_cookie_fallback=self.config.allow_cookie_fallback,
        )
        expected_note_id = str(item.media_id or "").strip().lower()
        if expected_note_id and note.note_id.lower() != expected_note_id:
            raise MediaDownloadError(
                "Xiaohongshu returned a different note from the requested item. "
                "The cross-wired response was blocked."
            )
        expected_profile_id = str(
            item.metadata.get("xiaohongshu_profile_id") or ""
        ).strip()
        membership_verified = item.metadata.get(
            "profile_note_membership_verified"
        )
        if expected_profile_id or membership_verified is not None:
            if (
                membership_verified is not True
                or not expected_profile_id
                or note.author_id != expected_profile_id
            ):
                raise MediaDownloadError(
                    "Xiaohongshu profile note belongs to a different or unverifiable "
                    "author. The cross-wired response was blocked before download."
                )
        media_type = MediaType.VIDEO if note.videos else MediaType.IMAGE
        if not note.videos and not note.images and not note.live_photos:
            raise MediaDownloadError(
                "The Xiaohongshu note contains no downloadable media"
            )
        if callback:
            callback(
                EngineEvent(
                    event="metadata",
                    title=note.title,
                    upload_date=note.upload_date,
                    author=note.author,
                    media_type=media_type,
                    cookie_fallback_used=fallback,
                )
            )

        options = self._base_options(not fallback)
        output_paths: list[str] = []
        selected_format: str | None = None
        resolution: str | None = None
        completed_assets: list[RemoteAsset] = []
        try:
            with YoutubeDL(options) as ydl:
                if note.videos:
                    declared_assets = [
                        asset
                        for asset in note.videos
                        if asset.width and asset.height
                    ]
                    declared_floor = (
                        max(
                            declared_assets,
                            key=lambda asset: int(asset.width or 0)
                            * int(asset.height or 0),
                        )
                        if declared_assets
                        else None
                    )
                    flattened = [
                        RemoteAsset(
                            candidates=list(asset.candidates),
                            index=asset.index,
                            width=(
                                asset.width
                                or (declared_floor.width if declared_floor else None)
                            ),
                            height=(
                                asset.height
                                or (declared_floor.height if declared_floor else None)
                            ),
                            size=asset.size,
                            format_id=asset.format_id,
                            video_uri=asset.video_uri,
                            duration=asset.duration,
                        )
                        for asset in note.videos
                    ]
                    try:
                        path, chosen = self._download_first_available_asset(
                            ydl,
                            flattened,
                            output_dir,
                            note.upload_date,
                            note.title,
                            note.note_id,
                            item.source_url,
                            platform=Platform.XIAOHONGSHU,
                            media_type=MediaType.VIDEO,
                            callback=callback,
                            should_cancel=should_cancel,
                            verify_declared_dimensions=True,
                        )
                    except DownloadCancelledError:
                        raise
                    except Exception as exc:
                        raise MediaDownloadError(f"Video failed: {exc}") from exc
                    output_paths.append(str(path))
                    if callback:
                        callback(
                            EngineEvent(
                                event="asset_completed",
                                output_paths=list(output_paths),
                            )
                        )
                    selected_format = chosen.format_id
                    if chosen.width and chosen.height:
                        resolution = f"{chosen.width}x{chosen.height}"
                else:
                    for asset in note.images:
                        if should_cancel():
                            raise DownloadCancelledError("Task cancelled")
                        try:
                            path, chosen = self._download_first_available_asset(
                                ydl,
                                [asset],
                                output_dir,
                                note.upload_date,
                                note.title,
                                note.note_id,
                                item.source_url,
                                platform=Platform.XIAOHONGSHU,
                                media_type=MediaType.IMAGE,
                                callback=callback,
                                should_cancel=should_cancel,
                                asset_index=asset.index,
                                verify_declared_dimensions=True,
                            )
                        except DownloadCancelledError:
                            raise
                        except Exception as exc:
                            raise MediaDownloadError(
                                f"Image {asset.index} failed: {exc}"
                            ) from exc
                        output_paths.append(str(path))
                        completed_assets.append(chosen)
                        if callback:
                            callback(
                                EngineEvent(
                                    event="asset_completed",
                                    output_paths=list(output_paths),
                                )
                            )
                    for asset in note.live_photos:
                        if should_cancel():
                            raise DownloadCancelledError("Task cancelled")
                        try:
                            path, chosen = self._download_first_available_asset(
                                ydl,
                                [asset],
                                output_dir,
                                note.upload_date,
                                note.title,
                                note.note_id,
                                item.source_url,
                                platform=Platform.XIAOHONGSHU,
                                media_type=MediaType.VIDEO,
                                callback=callback,
                                should_cancel=should_cancel,
                                asset_index=asset.index,
                                verify_declared_dimensions=True,
                            )
                        except DownloadCancelledError:
                            raise
                        except Exception as exc:
                            raise MediaDownloadError(
                                f"Live Photo video {asset.index} failed: {exc}"
                            ) from exc
                        output_paths.append(str(path))
                        completed_assets.append(chosen)
                        if callback:
                            callback(
                                EngineEvent(
                                    event="asset_completed",
                                    output_paths=list(output_paths),
                                )
                            )
                        selected_format = chosen.format_id
                    sized_assets = [
                        asset
                        for asset in completed_assets
                        if asset.width and asset.height
                    ]
                    if sized_assets:
                        largest = max(
                            sized_assets,
                            key=lambda asset: int(asset.width or 0)
                            * int(asset.height or 0),
                        )
                        resolution = f"{largest.width}x{largest.height}"
        except DownloadCancelledError:
            raise
        except Exception as exc:
            self._raise_download_error(exc, item.source_url)

        outcome = DownloadOutcome(
            output_paths=output_paths,
            title=note.title,
            upload_date=note.upload_date,
            author=note.author,
            media_type=media_type,
            selected_format=selected_format,
            resolution=resolution,
            cookie_fallback_used=fallback,
        )
        if callback:
            callback(
                EngineEvent(
                    event="completed",
                    title=outcome.title,
                    upload_date=outcome.upload_date,
                    author=outcome.author,
                    media_type=outcome.media_type,
                    selected_format=outcome.selected_format,
                    resolution=outcome.resolution,
                    output_paths=outcome.output_paths,
                    cookie_fallback_used=fallback,
                )
            )
        return outcome

    def _download_first_available_asset(
        self,
        ydl: YoutubeDL,
        assets: list[RemoteAsset],
        output_dir: Path,
        upload_date: str | None,
        title: str,
        media_id: str,
        source_url: str,
        *,
        platform: Platform,
        media_type: MediaType,
        callback: EventCallback | None,
        should_cancel: CancelCallback,
        asset_index: int | None = None,
        progress_index: int | None = None,
        progress_count: int | None = None,
        verify_declared_dimensions: bool = False,
        require_quality_fingerprint: bool = False,
        _douyin_transfer_attempt: int = 1,
    ) -> tuple[Path, RemoteAsset]:
        errors: list[str] = []
        douyin_transient_errors: list[str] = []
        douyin_redirect_errors: list[TemporaryAccessError] = []
        is_xiaohongshu_source = platform == Platform.XIAOHONGSHU
        is_douyin_source = platform == Platform.DOUYIN
        for asset in assets:
            allow_verified_douyin_redirect = bool(
                is_douyin_source
                and media_type == MediaType.VIDEO
                and require_quality_fingerprint
                and (asset.size is not None or asset.bit_rate is not None)
                and asset.duration is not None
                and asset.duration > 0
                and asset.video_codec
                and asset.probe_prefix_size is not None
                and 12 <= asset.probe_prefix_size <= DOUYIN_PROBE_BYTES
                and re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(asset.probe_prefix_sha256 or ""),
                )
                and asset.redirect_source_url is not None
                and self._is_trusted_douyin_asset_url(
                    asset.redirect_source_url,
                    MediaType.VIDEO,
                )
            )
            for candidate_index, candidate in enumerate(asset.candidates, start=1):
                if should_cancel():
                    raise DownloadCancelledError("Task cancelled")
                response = None
                try:
                    if is_xiaohongshu_source and not (
                        is_trusted_xiaohongshu_asset_url(candidate)
                    ):
                        raise MediaDownloadError(
                            "Untrusted Xiaohongshu media URL was blocked"
                        )
                    if is_douyin_source and not (
                        self._is_trusted_douyin_asset_url(candidate, media_type)
                        or (
                            allow_verified_douyin_redirect
                            and self._is_douyin_regional_media_host(
                                self._strict_https_hostname(candidate)
                            )
                        )
                    ):
                        raise MediaDownloadError(
                            "Untrusted Douyin media URL was blocked"
                        )
                    referer = source_url
                    if is_xiaohongshu_source:
                        referer = "https://www.xiaohongshu.com/"
                    elif is_douyin_source:
                        referer = DOUYIN_MEDIA_HEADERS["Referer"]
                    request_headers = {
                        "Referer": referer,
                        "Accept": "*/*",
                    }
                    if is_douyin_source:
                        request_headers["User-Agent"] = DOUYIN_MEDIA_HEADERS[
                            "User-Agent"
                        ]
                    media_request = Request(
                        candidate,
                        headers=request_headers,
                    )
                    if is_douyin_source:
                        try:
                            response = self._open_douyin_media_response(
                                ydl,
                                media_request,
                                redirect_rejection_reason=lambda value: (
                                    self._douyin_media_redirect_rejection_reason(
                                        value,
                                        media_type,
                                        allow_verified_regional=(
                                            allow_verified_douyin_redirect
                                        ),
                                    )
                                ),
                                should_cancel=should_cancel,
                            )
                        except DownloadCancelled as exc:
                            raise DownloadCancelledError("Task cancelled") from exc
                        except _DouyinRedirectRejected as exc:
                            raise TemporaryAccessError(
                                "Douyin media redirect could not be trusted. The task "
                                "was paused before downloading later items. Redirect "
                                f"host: {exc.redirect_host or 'unavailable'}; Redirect "
                                "host fingerprint: "
                                f"{exc.redirect_host_fingerprint or 'unavailable'}; "
                                f"Redirect port: {exc.redirect_port or 'unavailable'}; "
                                f"reason: {exc.redirect_reason or 'unrecognized-host'}"
                            ) from exc
                    else:
                        response = ydl.urlopen(media_request)
                    final_url = str(getattr(response, "url", None) or candidate)
                    if is_xiaohongshu_source and not (
                        is_trusted_xiaohongshu_asset_url(final_url)
                    ):
                        raise MediaDownloadError(
                            "Xiaohongshu media request redirected to an untrusted URL"
                        )
                    redirect_reason = (
                        self._douyin_media_redirect_rejection_reason(
                            final_url,
                            media_type,
                            allow_verified_regional=(
                                allow_verified_douyin_redirect
                            ),
                        )
                        if is_douyin_source
                        else None
                    )
                    if is_douyin_source and redirect_reason is not None:
                        redirect_error = self._douyin_redirect_rejection(
                            final_url,
                            redirect_reason,
                        )
                        raise TemporaryAccessError(
                            "Douyin media redirect could not be trusted. The task was "
                            "paused before downloading later items. Redirect host: "
                            f"{redirect_error.redirect_host or 'unavailable'}; "
                            "Redirect host fingerprint: "
                            f"{redirect_error.redirect_host_fingerprint or 'unavailable'}; "
                            "Redirect port: "
                            f"{redirect_error.redirect_port or 'unavailable'}; "
                            "Redirect reason: "
                            f"{redirect_error.redirect_reason}"
                        )
                    content_type = (
                        (response.headers.get("Content-Type") or "")
                        .split(";", 1)[0]
                        .lower()
                    )
                    if content_type.startswith("text/") or content_type in {
                        "application/json",
                        "application/problem+json",
                    }:
                        raise MediaDownloadError(
                            "Media server returned a text or metadata response "
                            "instead of a media file"
                        )
                    first_chunk = response.read(1024 * 1024)
                    if not first_chunk:
                        raise MediaDownloadError(
                            "Media server returned an empty response"
                        )
                    chosen = asset
                    if verify_declared_dimensions and media_type == MediaType.IMAGE:
                        actual_dimensions = self._image_dimensions(first_chunk)
                        if not actual_dimensions:
                            raise MediaDownloadError(
                                "Highest-available image dimensions could not be verified"
                            )
                        actual_width, actual_height = actual_dimensions
                        if asset.width and asset.height and (
                            min(actual_width, actual_height)
                            < min(asset.width, asset.height)
                            or max(actual_width, actual_height)
                            < max(asset.width, asset.height)
                        ):
                            raise MediaDownloadError(
                                "Media server returned an image below its declared "
                                f"{asset.width}x{asset.height} resolution"
                            )
                        chosen = RemoteAsset(
                            candidates=list(asset.candidates),
                            index=asset.index,
                            width=actual_width,
                            height=actual_height,
                            size=asset.size,
                            format_id=asset.format_id,
                        )
                    extension = self._asset_extension(
                        candidate, content_type, media_type, first_chunk
                    )
                    path = self._xhs_output_path(
                        output_dir,
                        upload_date,
                        title,
                        media_id,
                        extension,
                        asset_index,
                    )
                    total_header = response.headers.get("Content-Length")
                    total = (
                        int(total_header)
                        if total_header and total_header.isdigit()
                        else None
                    )
                    downloaded = len(first_chunk)

                    def progress_percent() -> float | None:
                        if not total:
                            return None
                        local_fraction = min(1.0, downloaded / total)
                        if (
                            progress_index is not None
                            and progress_count is not None
                            and 0 < progress_index <= progress_count
                        ):
                            return min(
                                100.0,
                                ((progress_index - 1) + local_fraction)
                                * 100.0
                                / progress_count,
                            )
                        return min(100.0, local_fraction * 100.0)

                    temporary_fd, temporary_name = tempfile.mkstemp(
                        prefix=".original-media-",
                        suffix=".part",
                        dir=path.parent,
                    )
                    temporary = Path(temporary_name)
                    try:
                        with os.fdopen(temporary_fd, "wb") as handle:
                            temporary_fd = -1
                            handle.write(first_chunk)
                            if callback:
                                callback(
                                    EngineEvent(
                                        event="downloading",
                                        progress=TransferProgress(
                                            downloaded_bytes=downloaded,
                                            total_bytes=total,
                                            percent=progress_percent(),
                                            fragment_index=progress_index,
                                            fragment_count=progress_count,
                                            filename=str(path),
                                        ),
                                    )
                                )
                            while True:
                                if should_cancel():
                                    raise DownloadCancelledError("Task cancelled")
                                chunk = response.read(1024 * 1024)
                                if not chunk:
                                    break
                                handle.write(chunk)
                                downloaded += len(chunk)
                                if callback:
                                    callback(
                                        EngineEvent(
                                            event="downloading",
                                            progress=TransferProgress(
                                                downloaded_bytes=downloaded,
                                                total_bytes=total,
                                                percent=progress_percent(),
                                                fragment_index=progress_index,
                                                fragment_count=progress_count,
                                                filename=str(path),
                                            ),
                                        )
                                    )
                            if total is not None and downloaded != total:
                                raise MediaDownloadError(
                                    f"Incomplete media response: expected {total} bytes, "
                                    f"received {downloaded}"
                                )
                            if asset.size is not None and downloaded != asset.size:
                                raise MediaDownloadError(
                                    "Media response changed after quality verification: "
                                    f"expected {asset.size} bytes, received {downloaded}"
                                )
                            handle.flush()
                            os.fsync(handle.fileno())
                        if (
                            verify_declared_dimensions
                            and media_type == MediaType.VIDEO
                        ):
                            chosen = self._verify_local_video_asset(
                                temporary,
                                asset,
                                should_cancel=should_cancel,
                                require_quality_fingerprint=(
                                    require_quality_fingerprint
                                ),
                            )
                        os.replace(temporary, path)
                    except BaseException:
                        if temporary_fd >= 0:
                            with contextlib.suppress(OSError):
                                os.close(temporary_fd)
                        temporary.unlink(missing_ok=True)
                        raise
                    if callback:
                        callback(
                            EngineEvent(
                                event="downloading",
                                progress=TransferProgress(
                                    downloaded_bytes=downloaded,
                                    total_bytes=total or downloaded,
                                    percent=(
                                        progress_index * 100.0 / progress_count
                                        if progress_index is not None
                                        and progress_count
                                        else 100.0
                                    ),
                                    fragment_index=progress_index,
                                    fragment_count=progress_count,
                                    filename=str(path),
                                ),
                            )
                        )
                    return path.resolve(), chosen
                except DownloadCancelledError:
                    raise
                except TemporaryAccessError as exc:
                    if (
                        is_douyin_source
                        and "Douyin media redirect could not be trusted" in str(exc)
                    ):
                        douyin_redirect_errors.append(exc)
                        errors.append(
                            f"Candidate {candidate_index}: "
                            f"{safe_external_error_message(exc)}"
                        )
                        continue
                    raise
                except Exception as exc:
                    self._close_douyin_probe_error(exc)
                    if is_douyin_source and self._is_retryable_douyin_transfer_error(
                        exc
                    ):
                        douyin_transient_errors.append(
                            self._safe_douyin_probe_failure(exc)
                        )
                    errors.append(
                        f"Candidate {candidate_index}: "
                        f"{safe_external_error_message(exc)}"
                    )
                finally:
                    if response is not None:
                        with contextlib.suppress(Exception):
                            response.close()
        if douyin_transient_errors:
            detail = douyin_transient_errors[-1]
            if _douyin_transfer_attempt < DOUYIN_TRANSFER_ATTEMPTS:
                if callback:
                    callback(
                        EngineEvent(
                            event="probing",
                            message=(
                                "Retrying Douyin media transfer after a temporary "
                                "network error "
                                f"({_douyin_transfer_attempt + 1}/"
                                f"{DOUYIN_TRANSFER_ATTEMPTS})"
                            ),
                        )
                    )
                try:
                    self._wait_for_douyin_probe_retry(
                        DOUYIN_PROBE_RETRY_BASE_SECONDS
                        * (2 ** (_douyin_transfer_attempt - 1)),
                        should_cancel,
                    )
                except DownloadCancelled as exc:
                    raise DownloadCancelledError("Task cancelled") from exc
                return self._download_first_available_asset(
                    ydl,
                    assets,
                    output_dir,
                    upload_date,
                    title,
                    media_id,
                    source_url,
                    platform=platform,
                    media_type=media_type,
                    callback=callback,
                    should_cancel=should_cancel,
                    asset_index=asset_index,
                    progress_index=progress_index,
                    progress_count=progress_count,
                    verify_declared_dimensions=verify_declared_dimensions,
                    require_quality_fingerprint=require_quality_fingerprint,
                    _douyin_transfer_attempt=_douyin_transfer_attempt + 1,
                )
            if douyin_redirect_errors:
                raise douyin_redirect_errors[-1]
            raise TemporaryAccessError(
                "Douyin media transfer was temporarily unavailable. The task was "
                "paused before downloading later items; wait briefly and continue "
                "the task. Completed files were preserved. Transfer details: "
                f"{detail}"
            )
        if douyin_redirect_errors:
            raise douyin_redirect_errors[-1]
        detail = errors[-1] if errors else "No asset URLs were available"
        raise MediaDownloadError(
            f"All highest-available media URLs failed: {detail}"
        )

    @staticmethod
    def _is_retryable_douyin_transfer_error(exc: Exception) -> bool:
        if isinstance(exc, HTTPError):
            return exc.status in {
                401,
                403,
                404,
                408,
                410,
                425,
                429,
                500,
                502,
                503,
                504,
            }
        if isinstance(exc, MediaDownloadError):
            return str(exc).startswith(
                (
                    "Incomplete media response:",
                    "Media response changed after quality verification:",
                    "Highest-available video dimensions could not be verified",
                    "Media server returned a video below its declared",
                    "Downloaded video content did not match",
                    "Downloaded video codec did not match",
                    "Downloaded audio codec did not match",
                    "Downloaded video size did not match",
                    "Downloaded video bitrate was below",
                    "Downloaded video duration did not match",
                )
            )
        return isinstance(exc, (TransportError, TimeoutError, ConnectionError))

    def _verify_local_video_asset(
        self,
        path: Path,
        asset: RemoteAsset,
        *,
        should_cancel: CancelCallback,
        require_quality_fingerprint: bool = False,
    ) -> RemoteAsset:
        if (
            require_quality_fingerprint
            and asset.size is None
            and asset.bit_rate is None
        ):
            raise MediaDownloadError(
                "The verified highest-quality video has no bitrate or complete "
                "size fingerprint"
            )
        if require_quality_fingerprint and (
            asset.duration is None or asset.duration <= 0
        ):
            raise MediaDownloadError(
                "The verified highest-quality video has no duration fingerprint"
            )
        if require_quality_fingerprint and (
            asset.probe_prefix_size is None
            or not (12 <= asset.probe_prefix_size <= DOUYIN_PROBE_BYTES)
            or not re.fullmatch(
                r"[0-9a-f]{64}",
                str(asset.probe_prefix_sha256 or ""),
            )
        ):
            raise MediaDownloadError(
                "The verified highest-quality video has no content fingerprint"
            )
        if require_quality_fingerprint and not asset.video_codec:
            raise MediaDownloadError(
                "The verified highest-quality video has no codec fingerprint"
            )
        if asset.probe_prefix_size and asset.probe_prefix_sha256:
            try:
                with path.open("rb") as handle:
                    prefix = handle.read(asset.probe_prefix_size)
            except OSError as exc:
                raise MediaDownloadError(
                    "Downloaded video content fingerprint could not be read"
                ) from exc
            if (
                len(prefix) != asset.probe_prefix_size
                or hashlib.sha256(prefix).hexdigest()
                != asset.probe_prefix_sha256
            ):
                raise MediaDownloadError(
                    "Downloaded video content did not match the verified Douyin "
                    "media endpoint"
                )
        executable = self._find_ffprobe_executable()
        if not executable:
            raise MediaDownloadError(FFPROBE_REQUIRED_MESSAGE)
        try:
            payload = self._run_ffprobe(
                [
                    executable,
                    "-v",
                    "error",
                    "-show_entries",
                    (
                        "stream=codec_type,codec_name,width,height,bit_rate,duration:"
                        "format=duration,bit_rate,size"
                    ),
                    "-of",
                    "json",
                    "-i",
                    str(path),
                ],
                timeout_seconds=DOUYIN_FFPROBE_FILE_TIMEOUT_SECONDS,
                should_cancel=should_cancel,
            )
        except DownloadCancelled as exc:
            raise DownloadCancelledError("Task cancelled") from exc
        media = self._parse_ffprobe_payload(payload)
        actual_width = int((media or {}).get("width") or 0)
        actual_height = int((media or {}).get("height") or 0)
        if actual_width <= 0 or actual_height <= 0:
            raise MediaDownloadError(
                "Highest-available video dimensions could not be verified"
            )
        if asset.width and asset.height and (
            min(actual_width, actual_height) < min(asset.width, asset.height)
            or max(actual_width, actual_height) < max(asset.width, asset.height)
        ):
            raise MediaDownloadError(
                "Media server returned a video below its declared "
                f"{asset.width}x{asset.height} resolution"
            )
        actual_video_codec = str((media or {}).get("vcodec") or "").lower()
        actual_audio_codec = str((media or {}).get("acodec") or "").lower()
        if asset.video_codec and actual_video_codec != asset.video_codec.lower():
            raise MediaDownloadError(
                "Downloaded video codec did not match its verified media endpoint"
            )
        if asset.audio_codec and actual_audio_codec != asset.audio_codec.lower():
            raise MediaDownloadError(
                "Downloaded audio codec did not match its verified media endpoint"
            )
        if asset.size is not None:
            actual_size = path.stat().st_size
            tolerance = max(64 * 1024, int(asset.size * 0.01))
            if abs(actual_size - asset.size) > tolerance:
                raise MediaDownloadError(
                    "Downloaded video size did not match its verified highest-quality "
                    "media endpoint"
                )
        if asset.bit_rate is not None:
            actual_bit_rate = int((media or {}).get("bit_rate") or 0)
            if actual_bit_rate <= 0 or actual_bit_rate < int(asset.bit_rate * 0.9):
                raise MediaDownloadError(
                    "Downloaded video bitrate was below its verified highest-quality "
                    "media endpoint"
                )
        if asset.duration is not None:
            actual_duration = self._float_or_none((media or {}).get("duration"))
            tolerance = self._douyin_duration_tolerance(asset.duration)
            if (
                actual_duration is None
                or abs(actual_duration - asset.duration) > tolerance
            ):
                raise MediaDownloadError(
                    "Downloaded video duration did not match its verified media "
                    "metadata"
                )
        return RemoteAsset(
            candidates=list(asset.candidates),
            index=asset.index,
            width=actual_width,
            height=actual_height,
            size=asset.size,
            format_id=asset.format_id,
            video_uri=asset.video_uri,
            duration=asset.duration,
            bit_rate=asset.bit_rate,
            quality_candidates=asset.quality_candidates,
            video_codec=asset.video_codec,
            audio_codec=asset.audio_codec,
            probe_prefix_size=asset.probe_prefix_size,
            probe_prefix_sha256=asset.probe_prefix_sha256,
            redirect_source_url=asset.redirect_source_url,
        )

    @staticmethod
    def _image_dimensions(data: bytes) -> tuple[int, int] | None:
        if len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n"):
            width = int.from_bytes(data[16:20], "big")
            height = int.from_bytes(data[20:24], "big")
            return (width, height) if width > 0 and height > 0 else None
        if len(data) >= 10 and data.startswith((b"GIF87a", b"GIF89a")):
            width = int.from_bytes(data[6:8], "little")
            height = int.from_bytes(data[8:10], "little")
            return (width, height) if width > 0 and height > 0 else None
        if len(data) >= 30 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8X":
                width = 1 + int.from_bytes(data[24:27], "little")
                height = 1 + int.from_bytes(data[27:30], "little")
                return width, height
            if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
                width = int.from_bytes(data[26:28], "little") & 0x3FFF
                height = int.from_bytes(data[28:30], "little") & 0x3FFF
                return (width, height) if width > 0 and height > 0 else None
            if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
                packed = int.from_bytes(data[21:25], "little")
                width = (packed & 0x3FFF) + 1
                height = ((packed >> 14) & 0x3FFF) + 1
                return width, height
        iso_dimensions = MediaDownloader._iso_bmff_image_dimensions(data)
        if iso_dimensions:
            return iso_dimensions
        if len(data) < 4 or not data.startswith(b"\xff\xd8"):
            return None
        offset = 2
        start_of_frame_markers = {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }
        while offset + 4 <= len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            while offset < len(data) and data[offset] == 0xFF:
                offset += 1
            if offset >= len(data):
                return None
            marker = data[offset]
            offset += 1
            if marker in {0x01, *range(0xD0, 0xDA)}:
                continue
            if offset + 2 > len(data):
                return None
            segment_length = int.from_bytes(data[offset : offset + 2], "big")
            if segment_length < 2 or offset + segment_length > len(data):
                return None
            if marker in start_of_frame_markers and segment_length >= 7:
                height = int.from_bytes(data[offset + 3 : offset + 5], "big")
                width = int.from_bytes(data[offset + 5 : offset + 7], "big")
                return (width, height) if width > 0 and height > 0 else None
            offset += segment_length
        return None

    @staticmethod
    def _iso_bmff_image_dimensions(data: bytes) -> tuple[int, int] | None:
        image_brands = {
            b"avif",
            b"avis",
            b"heic",
            b"heix",
            b"hevc",
            b"hevx",
            b"mif1",
            b"msf1",
        }

        def boxes(start: int, end: int):
            offset = start
            while offset + 8 <= end:
                size = int.from_bytes(data[offset : offset + 4], "big")
                box_type = data[offset + 4 : offset + 8]
                header_size = 8
                if size == 1:
                    if offset + 16 > end:
                        return
                    size = int.from_bytes(data[offset + 8 : offset + 16], "big")
                    header_size = 16
                elif size == 0:
                    size = end - offset
                if size < header_size:
                    return
                box_end = offset + size
                if box_end > end:
                    return
                yield box_type, offset + header_size, box_end
                offset = box_end

        top_level = list(boxes(0, len(data)))
        ftyp = next(
            (
                (payload_start, box_end)
                for box_type, payload_start, box_end in top_level
                if box_type == b"ftyp"
            ),
            None,
        )
        if not ftyp:
            return None
        ftyp_start, ftyp_end = ftyp
        if ftyp_end - ftyp_start < 8:
            return None
        brands = {data[ftyp_start : ftyp_start + 4]}
        brands.update(
            data[offset : offset + 4]
            for offset in range(ftyp_start + 8, ftyp_end, 4)
            if offset + 4 <= ftyp_end
        )
        if not brands.intersection(image_brands):
            return None

        dimensions: list[tuple[int, int]] = []
        container_offsets = {b"meta": 4, b"iprp": 0, b"ipco": 0}

        def collect(start: int, end: int, depth: int) -> None:
            if depth > 8:
                return
            for box_type, payload_start, box_end in boxes(start, end):
                if box_type == b"ispe":
                    if box_end - payload_start < 12:
                        continue
                    width = int.from_bytes(
                        data[payload_start + 4 : payload_start + 8], "big"
                    )
                    height = int.from_bytes(
                        data[payload_start + 8 : payload_start + 12], "big"
                    )
                    if width > 0 and height > 0:
                        dimensions.append((width, height))
                    continue
                child_offset = container_offsets.get(box_type)
                if child_offset is None or payload_start + child_offset > box_end:
                    continue
                collect(payload_start + child_offset, box_end, depth + 1)

        collect(0, len(data), 0)
        return max(dimensions, key=lambda value: value[0] * value[1]) if dimensions else None

    def _xhs_output_path(
        self,
        output_dir: Path,
        upload_date: str | None,
        title: str,
        media_id: str,
        extension: str,
        asset_index: int | None,
    ) -> Path:
        date_part = upload_date or "Unknown-Date"
        title_part = safe_component(
            title, fallback=media_id, limit=self.config.filename_limit
        )
        suffix = f"-{asset_index:03d}" if asset_index is not None else ""
        return output_dir / f"{date_part}-{title_part} [{media_id}]{suffix}.{extension}"

    @staticmethod
    def _asset_extension(
        url: str,
        content_type: str,
        media_type: MediaType,
        first_bytes: bytes = b"",
    ) -> str:
        ambiguous_iso_image = False
        if first_bytes.startswith(b"\xff\xd8\xff"):
            return "jpg"
        if first_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        if first_bytes.startswith((b"GIF87a", b"GIF89a")):
            return "gif"
        if first_bytes.startswith(b"RIFF") and first_bytes[8:12] == b"WEBP":
            return "webp"
        if len(first_bytes) >= 12 and first_bytes[4:8] == b"ftyp":
            box_size = int.from_bytes(first_bytes[:4], "big")
            box_end = min(len(first_bytes), box_size) if box_size >= 12 else 12
            brands = {first_bytes[8:12]}
            brands.update(
                first_bytes[offset : offset + 4]
                for offset in range(16, box_end - 3, 4)
            )
            if brands & {b"avif", b"avis"}:
                return "avif"
            if brands & {b"heic", b"heix", b"hevc", b"hevx"}:
                return "heic"
            if media_type == MediaType.VIDEO:
                return "mp4"
            ambiguous_iso_image = bool(brands & {b"mif1", b"msf1"})
        extensions = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
            "image/avif": "avif",
            "image/heic": "heic",
            "image/heif": "heic",
            "image/gif": "gif",
            "video/mp4": "mp4",
            "video/webm": "webm",
            "video/quicktime": "mov",
        }
        if content_type in extensions:
            return extensions[content_type]
        suffix = Path(unquote(urlsplit(url).path)).suffix.lower().lstrip(".")
        if ambiguous_iso_image:
            if suffix in {"avif", "avis"}:
                return "avif"
            if suffix in {"heic", "heif", "heix", "hevc", "hevx"}:
                return "heic"
            return "bin"
        if re.fullmatch(r"[a-z0-9]{2,5}", suffix):
            return suffix
        guessed = mimetypes.guess_extension(content_type or "")
        if guessed:
            return guessed.lstrip(".").replace("jpeg", "jpg")
        return "mp4" if media_type == MediaType.VIDEO else "jpg"

    @staticmethod
    def _media_id(url: str) -> str | None:
        path = urlsplit(url).path.rstrip("/")
        value = path.rsplit("/", 1)[-1]
        return value or None

    @staticmethod
    def _author_from_info(
        info: dict[str, Any], fallback: str | None = "Unknown Author"
    ) -> str | None:
        extractor = " ".join(
            str(info.get(key) or "") for key in ("extractor", "extractor_key")
        ).lower()
        keys = (
            ("channel", "uploader", "playlist_uploader", "creator", "artist")
            if "douyin" in extractor
            else ("uploader", "channel", "playlist_uploader", "creator", "artist")
        )
        for key in keys:
            value = info.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return fallback

    @staticmethod
    def _normalize_date(value: Any) -> str | None:
        if not value:
            return None
        text = re.sub(r"\D", "", str(value))
        if len(text) >= 8:
            return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
        return None

    @staticmethod
    def _media_type(info: dict[str, Any]) -> MediaType:
        if info.get("vcodec") not in (None, "none") or info.get("formats"):
            return MediaType.VIDEO
        if info.get("thumbnails") and not info.get("formats"):
            return MediaType.IMAGE
        return MediaType.UNKNOWN

    @staticmethod
    def _entry_url(entry: dict[str, Any], platform: Platform, original_url: str) -> str:
        for key in ("webpage_url", "original_url"):
            value = entry.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value
        value = entry.get("url")
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
        media_id = str(entry.get("id") or value or "").strip()
        if platform == Platform.YOUTUBE and media_id:
            return f"https://www.youtube.com/watch?v={media_id}"
        if platform == Platform.BILIBILI and media_id:
            return f"https://www.bilibili.com/video/{media_id}"
        if platform == Platform.DOUYIN and media_id.isdigit():
            return f"https://www.douyin.com/video/{media_id}"
        if media_id and len(media_id) > 5:
            return media_id
        return original_url

    @staticmethod
    def _format_id(info: dict[str, Any]) -> str | None:
        requested = info.get("requested_formats") or []
        values = [
            str(value.get("format_id")) for value in requested if value.get("format_id")
        ]
        if values:
            return "+".join(values)
        value = info.get("format_id")
        return str(value) if value else None

    @staticmethod
    def _resolution(info: dict[str, Any]) -> str | None:
        candidates = [info, *(info.get("requested_formats") or [])]
        dimensions = [
            (int(value.get("width") or 0), int(value.get("height") or 0))
            for value in candidates
            if isinstance(value, dict)
        ]
        width, height = max(
            dimensions, key=lambda pair: pair[0] * pair[1], default=(0, 0)
        )
        return f"{width}x{height}" if width and height else None

    @staticmethod
    def _paths_from_info(info: dict[str, Any], ydl: YoutubeDL) -> list[str]:
        paths: list[str] = []
        for key in ("filepath", "_filename"):
            if info.get(key):
                paths.append(str(info[key]))
        for requested in info.get("requested_downloads") or []:
            for key in ("filepath", "filename"):
                if requested.get(key):
                    paths.append(str(requested[key]))
        with contextlib.suppress(Exception):
            paths.append(ydl.prepare_filename(info))
        return paths

    @staticmethod
    def _existing_unique_paths(paths: Iterable[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in paths:
            path = Path(value).expanduser().resolve()
            text = str(path)
            if text not in seen and path.is_file() and path.stat().st_size > 0:
                seen.add(text)
                result.append(text)
        return result
