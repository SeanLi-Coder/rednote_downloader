from __future__ import annotations

import contextlib
import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from yt_dlp import YoutubeDL
from yt_dlp.extractor.tiktok import DouyinIE
from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import HTTPError, TransportError
from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.utils import DownloadCancelled, DownloadError

from .douyin import discover_profile as discover_douyin_profile
from .douyin import discover_item_metadata_from_profile
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
DOUYIN_FFPROBE_PIPE_TIMEOUT_SECONDS = 3.0
DOUYIN_FFPROBE_REMOTE_TIMEOUT_SECONDS = 15.0
DOUYIN_PROCESS_POLL_SECONDS = 0.1
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
    "访问频繁",
    "风控",
    "uploader profile instead of the requested video",
    "different video while requesting",
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
    "explicitly without login and create a new task."
)
DOUYIN_ITEM_EXPANSION_MESSAGE = (
    "Douyin returned an uploader profile instead of the requested video. "
    "The unexpected profile entries were blocked. Open the original video in "
    "Chrome, finish verification, then retry."
)
DOUYIN_EMPTY_DETAIL_MARKERS = (
    "aweme detail is empty",
    "empty aweme detail",
    "no aweme detail",
    "unable to extract aweme detail",
    "uploader profile instead of the requested video",
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


def safe_external_error_message(value: BaseException | str) -> str:
    message = str(value)
    message = _ERROR_URL_RE.sub("[redacted URL]", message)
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
                result = self.discover(
                    resolved.url,
                    resolved.platform,
                    resolved.kind,
                    should_cancel=should_cancel,
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
                    title=self._media_id(entry) or "Xiaohongshu note",
                    author=profile.author,
                    playlist_index=index,
                    extractor_key="XiaoHongShu",
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
            items: list[DownloadItem] = []
            for index, entry in enumerate(profile.video_urls, start=1):
                media_id = self._media_id(entry)
                cached_media = profile.media_metadata.get(media_id or "")
                metadata: dict[str, Any] = {
                    "profile_url": url,
                    "profile_owner_verified": True,
                }
                if cached_media:
                    metadata["douyin_profile_media"] = dict(cached_media)
                items.append(
                    DownloadItem(
                        id=_item_key(platform, media_id, entry, index),
                        media_id=media_id,
                        source_url=entry,
                        title=str(
                            (cached_media or {}).get("title")
                            or media_id
                            or "Douyin video"
                        ),
                        author=str(
                            (cached_media or {}).get("author") or profile.author
                        ),
                        playlist_index=index,
                        extractor_key="Douyin",
                        media_type=MediaType.VIDEO,
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

        return self._discover_with_ytdlp(url, platform, should_cancel)

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
                    fallback_title=expected_id,
                    should_cancel=should_cancel,
                )
                self._validate_douyin_info(info, expected_id, url)
                video_uri = self._douyin_video_uri(info, expected_id, url)
                if not video_uri:
                    raise AuthenticationRequiredError(
                        "Douyin did not return a verified media identity for the "
                        "requested video. Open the original video in Chrome, finish "
                        "verification, then retry.",
                        verification_url=url,
                    )
            return info, video_uri

        (info, video_uri), fallback = self._run_with_cookie_fallback(
            operation,
            url=url,
        )
        author = (
            self._author_from_info(info, fallback="Douyin Author") or "Douyin Author"
        )
        title = str(info.get("title") or expected_id)
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
        if owner_id:
            cached_media["owner_id"] = owner_id
        if owner_id and self.config.cookie_browser:
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
            except (AuthenticationRequiredError, DiscoveryError) as exc:
                raise AuthenticationRequiredError(
                    "Douyin could not verify the author's highest-quality "
                    "renditions after automatic retries. Open the original video "
                    "in Chrome, finish verification, then retry.",
                    verification_url=url,
                ) from exc
            if not enriched_media:
                raise AuthenticationRequiredError(
                    "Douyin could not find the requested video in its verified "
                    "author feed, so the highest quality could not be confirmed. "
                    "Open the original video in Chrome, finish verification, "
                    "then retry.",
                    verification_url=url,
                )
            if "direct_candidates" in enriched_media:
                cached_media["direct_candidates"] = enriched_media["direct_candidates"]
            combined_floor = quality_floor_dimensions(
                [cached_media, enriched_media],
                cap_full_hd=False,
            )
            if combined_floor:
                cached_media["minimum_width"], cached_media["minimum_height"] = (
                    combined_floor
                )
        duration = self._float_or_none(info.get("duration"))
        if duration and duration > 0:
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
        return self._download_with_ytdlp(
            item,
            output_path,
            platform=platform,
            callback=callback,
            should_cancel=should_cancel,
        )

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
                raise AuthenticationRequiredError(
                    COOKIE_ACCESS_MESSAGE,
                    verification_url=url,
                ) from exc
            self._raise_download_error(exc, url)
        raise AssertionError("Unreachable")

    @staticmethod
    def _raise_download_error(exc: BaseException, url: str) -> None:
        message = str(exc)
        if "uploader profile instead of the requested video" in message.lower():
            raise AuthenticationRequiredError(
                DOUYIN_ITEM_EXPANSION_MESSAGE,
                verification_url=url,
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
        should_cancel: CancelCallback,
    ) -> DiscoveryResult:
        def operation(use_cookies: bool) -> tuple[dict[str, Any], str | None]:
            if should_cancel():
                raise DownloadCancelled("Task cancelled")
            options = {
                **self._base_options(use_cookies),
                "skip_download": True,
                "extract_flat": "in_playlist",
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
            for domain in ("douyin.com", "douyinvod.com", "amemv.com")
        )

    @classmethod
    def _douyin_direct_candidates_from_cache(
        cls,
        cached: dict[str, Any],
    ) -> list[dict[str, Any]]:
        raw_candidates = cached.get("direct_candidates")
        if not isinstance(raw_candidates, list):
            return []
        result: list[dict[str, Any]] = []
        for value in raw_candidates[:4]:
            if not isinstance(value, dict):
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
                if not isinstance(candidate_url, str) or len(candidate_url) > 8_192:
                    continue
                parsed = urlsplit(candidate_url)
                if parsed.scheme != "https" or not cls._is_douyin_direct_media_host(
                    parsed.hostname
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
        raise AuthenticationRequiredError(
            "Douyin returned data for a different video while requesting "
            f"{expected_id} (received {actual_id}). Open the original link in "
            "Chrome, finish verification, then retry.",
            verification_url=verification_url,
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
            raise AuthenticationRequiredError(
                DOUYIN_ITEM_EXPANSION_MESSAGE,
                verification_url=verification_url,
            )
        if expected_profile_id:
            actual_profile_id = str(info.get("channel_id") or "").strip()
            if actual_profile_id != expected_profile_id:
                raise AuthenticationRequiredError(
                    "Douyin returned data from a different author while requesting "
                    f"profile {expected_profile_id} (received "
                    f"{actual_profile_id or 'missing'}). Open the original profile "
                    "in Chrome, finish verification, then retry.",
                    verification_url=verification_url,
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
            raise AuthenticationRequiredError(
                "Douyin returned multiple media identities for one video. Open the "
                "original profile in Chrome, finish verification, then retry.",
                verification_url=verification_url,
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
            raise AuthenticationRequiredError(
                "Douyin item metadata could not be verified. Create a new task "
                "from the original video and retry.",
                verification_url=verification_url,
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
            raise AuthenticationRequiredError(
                "Douyin item metadata belongs to a different author. Create a new "
                "task from the original link and retry.",
                verification_url=verification_url,
            )
        video_uri = str(cached.get("video_uri") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", video_uri):
            raise AuthenticationRequiredError(
                "Douyin item metadata has no verified media identity. Create a new "
                "task from the original video and retry.",
                verification_url=verification_url,
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
        duration_ms = cached.get("duration_ms")
        if isinstance(duration_ms, int) and duration_ms > 0:
            result["duration"] = duration_ms / 1_000
        create_time = cached.get("create_time")
        if isinstance(create_time, int) and create_time > 0:
            result["timestamp"] = create_time
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
            raise AuthenticationRequiredError(
                "Douyin profile metadata could not be verified. Create a new task "
                "from the original profile and retry.",
                verification_url=verification_url,
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
            raise AuthenticationRequiredError(
                "Douyin profile metadata belongs to a different author. Create a "
                "new task from the original profile and retry.",
                verification_url=verification_url,
            )
        video_uri = str(cached.get("video_uri") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", video_uri):
            raise AuthenticationRequiredError(
                "Douyin profile metadata has no verified media identity. Create a "
                "new task from the original profile and retry.",
                verification_url=verification_url,
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
        duration_ms = cached.get("duration_ms")
        if isinstance(duration_ms, int) and duration_ms > 0:
            result["duration"] = duration_ms / 1_000
        create_time = cached.get("create_time")
        if isinstance(create_time, int) and create_time > 0:
            result["timestamp"] = create_time
        self._validate_douyin_info(
            result,
            expected_id,
            verification_url,
            expected_profile_id,
        )
        return result

    @staticmethod
    def _douyin_ratio_url(video_uri: str, ratio: str) -> str:
        hostname = (
            DOUYIN_DEFAULT_PROBE_HOST if ratio == "default" else DOUYIN_RATIO_PROBE_HOST
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
        direct_count = len(direct_candidates)
        for index, candidate in enumerate(direct_candidates, start=1):
            if should_cancel():
                raise DownloadCancelled("Task cancelled")
            declared_width = int(candidate["width"])
            declared_height = int(candidate["height"])
            label = f"direct-{declared_width}x{declared_height}"
            if callback:
                callback(
                    EngineEvent(
                        event="probing",
                        message=(
                            "Checking Douyin direct quality "
                            f"{index}/{direct_count}: "
                            f"{declared_width}x{declared_height}"
                        ),
                    )
                )
            direct_probe: dict[str, Any] | None = None
            for candidate_url in candidate.get("urls") or []:
                try:
                    direct_probe = self._probe_douyin_candidate(
                        ydl,
                        candidate_url,
                        expected_duration=expected_duration,
                        should_cancel=should_cancel,
                    )
                except DownloadCancelled:
                    raise
                except MediaDownloadError:
                    raise
                except Exception as exc:
                    self._close_douyin_probe_error(exc)
                    continue
                if direct_probe:
                    break
            if direct_probe:
                direct_probe["requested_ratio"] = label
                probes.append(direct_probe)

        ratio_count = len(DOUYIN_PROBE_RATIOS)
        for index, ratio in enumerate(DOUYIN_PROBE_RATIOS, start=1):
            if should_cancel():
                raise DownloadCancelled("Task cancelled")
            if callback:
                callback(
                    EngineEvent(
                        event="probing",
                        message=(
                            f"Checking Douyin quality {index}/{ratio_count}: {ratio}"
                        ),
                    )
                )
            candidate_url = self._douyin_ratio_url(video_uri, ratio)
            try:
                probe = self._probe_douyin_ratio_with_retry(
                    ydl,
                    candidate_url,
                    ratio=ratio,
                    expected_duration=expected_duration,
                    callback=callback,
                    should_cancel=should_cancel,
                )
                if probe:
                    probe["requested_ratio"] = ratio
                    probes.append(probe)
                else:
                    failures.append((ratio, "media metadata could not be parsed"))
            except DownloadCancelled:
                raise
            except MediaDownloadError:
                raise
            except Exception as exc:
                failures.append((ratio, self._safe_douyin_probe_failure(exc)))
                continue
            if should_cancel():
                raise DownloadCancelled("Task cancelled")

        unique_probes: dict[tuple[int, int, str, str, int, int], dict[str, Any]] = {}
        unsupported_probes: list[dict[str, Any]] = []
        for probe in probes:
            video_codec = str(probe.get("vcodec") or "").lower()
            if video_codec in {"bytevc2", "h266", "vvc"}:
                unsupported_probes.append(probe)
                continue
            signature = (
                int(probe["width"]),
                int(probe["height"]),
                video_codec,
                str(probe.get("acodec") or ""),
                int(probe.get("bit_rate") or 0),
                int(probe.get("filesize") or 0),
            )
            unique_probes.setdefault(signature, probe)

        if unique_probes:
            best_supported_pixels = max(
                int(value["width"]) * int(value["height"])
                for value in unique_probes.values()
            )
            blocking_unsupported = [
                value
                for value in unsupported_probes
                if int(value["width"]) * int(value["height"]) > best_supported_pixels
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
            key=lambda value: int(value["width"]) * int(value["height"]),
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
                return self._probe_douyin_candidate(
                    ydl,
                    candidate_url,
                    expected_duration=expected_duration,
                    should_cancel=should_cancel,
                )
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
            return exc.status in {408, 425, 429, 500, 502, 503, 504}
        if isinstance(exc, (TransportError, TimeoutError, ConnectionError, OSError)):
            return True
        return False

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
        headers = {
            **DOUYIN_MEDIA_HEADERS,
            "Range": f"bytes=0-{DOUYIN_PROBE_BYTES - 1}",
        }
        response = ydl.urlopen(
            Request(
                candidate_url,
                headers=headers,
                extensions={"timeout": DOUYIN_PROBE_HTTP_TIMEOUT_SECONDS},
            )
        )
        try:
            final_url = str(response.url)
            final_url_parts = urlsplit(final_url)
            if (
                final_url_parts.scheme not in {"http", "https"}
                or not final_url_parts.hostname
            ):
                raise _DouyinProbeRejected(
                    "media endpoint returned an invalid redirect"
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
        finally:
            response.close()

        if should_cancel():
            raise DownloadCancelled("Task cancelled")
        media = self._ffprobe_douyin_media(
            bytes(payload),
            final_url,
            should_cancel=should_cancel,
        )
        if not media:
            raise _DouyinProbeRejected("FFprobe could not parse the media stream")
        width = int(media.get("width") or 0)
        height = int(media.get("height") or 0)
        if width <= 0 or height <= 0:
            raise _DouyinProbeRejected("FFprobe returned no video dimensions")
        duration = self._float_or_none(media.get("duration"))
        if expected_duration is not None:
            if duration is None:
                raise _DouyinProbeRejected("FFprobe returned no media duration")
            tolerance = max(3.0, expected_duration * 0.05)
            if abs(duration - expected_duration) > tolerance:
                raise _DouyinProbeRejected(
                    "media duration did not match the requested Douyin item"
                )
        return {
            **media,
            "url": final_url,
            "filesize": filesize,
        }

    def _ffprobe_douyin_media(
        self,
        initial_bytes: bytes,
        candidate_url: str,
        *,
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
        try:
            payload = self._run_ffprobe(
                [*base_command, "-i", "pipe:0"],
                input_data=initial_bytes,
                timeout_seconds=DOUYIN_FFPROBE_PIPE_TIMEOUT_SECONDS,
                should_cancel=should_cancel,
            )
        except TimeoutError:
            payload = None
        prefix_media = self._parse_ffprobe_payload(payload)
        if self._douyin_probe_metadata_complete(prefix_media):
            return prefix_media

        header_blob = "".join(
            f"{key}: {value}\r\n" for key, value in DOUYIN_MEDIA_HEADERS.items()
        )
        payload = self._run_ffprobe(
            [
                *base_command,
                "-rw_timeout",
                str(int(DOUYIN_FFPROBE_REMOTE_TIMEOUT_SECONDS * 1_000_000)),
                "-headers",
                header_blob,
                "-i",
                candidate_url,
            ],
            timeout_seconds=DOUYIN_FFPROBE_REMOTE_TIMEOUT_SECONDS,
            should_cancel=should_cancel,
        )
        remote_media = self._parse_ffprobe_payload(payload)
        return self._merge_douyin_probe_metadata(prefix_media, remote_media)

    @classmethod
    def _douyin_probe_metadata_complete(cls, media: dict[str, Any] | None) -> bool:
        if not media:
            return False
        return (
            int(media.get("width") or 0) > 0
            and int(media.get("height") or 0) > 0
            and (cls._float_or_none(media.get("duration")) or 0) > 0
        )

    @classmethod
    def _merge_douyin_probe_metadata(
        cls,
        prefix_media: dict[str, Any] | None,
        remote_media: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not prefix_media:
            return remote_media
        if not remote_media:
            return prefix_media
        merged = dict(prefix_media)
        for key, value in remote_media.items():
            if key in {"width", "height", "bit_rate"}:
                if int(value or 0) > 0:
                    merged[key] = value
            elif key == "duration":
                if (cls._float_or_none(value) or 0) > 0:
                    merged[key] = value
            elif value not in {None, "", "none"}:
                merged[key] = value
        return merged

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

        def operation(use_cookies: bool) -> tuple[dict[str, Any], list[str]]:
            final_paths.clear()
            job_scope = safe_component(
                str(item.metadata.get("_job_id") or "standalone"),
                fallback="standalone",
                limit=80,
            )
            item_scope = safe_component(item.id, fallback="item", limit=80)
            parts_dir = output_dir / ".parts" / job_scope / item_scope
            parts_dir.mkdir(parents=True, exist_ok=True)
            logger = _YdlLogger(callback)
            options = {
                **self._base_options(use_cookies),
                **self._download_format_options(platform),
                "paths": {"home": str(output_dir), "temp": str(parts_dir)},
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
                    result = ydl.process_ie_result(raw_result, download=True)
                else:
                    result = ydl.extract_info(item.source_url, download=True)
                if not isinstance(result, dict):
                    raise DownloadError("The URL returned no downloadable media")
                if expected_douyin_id:
                    self._validate_douyin_info(
                        result,
                        expected_douyin_id,
                        verification_url,
                        expected_douyin_profile_id,
                    )
                candidates = self._paths_from_info(result, ydl)
            return result, [*final_paths, *candidates]

        try:
            (info, paths), fallback = self._run_with_cookie_fallback(
                operation, url=item.source_url
            )
        except AuthenticationRequiredError as exc:
            if platform == Platform.DOUYIN and exc.verification_url != verification_url:
                raise AuthenticationRequiredError(
                    str(exc), verification_url=verification_url
                ) from exc
            raise
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
            resolution=self._resolution(info),
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
                    flattened: list[RemoteAsset] = []
                    for asset in note.videos:
                        flattened.append(asset)
                    try:
                        path, chosen = self._download_first_available_asset(
                            ydl,
                            flattened,
                            output_dir,
                            note.upload_date,
                            note.title,
                            note.note_id,
                            item.source_url,
                            media_type=MediaType.VIDEO,
                            callback=callback,
                            should_cancel=should_cancel,
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
                                media_type=MediaType.IMAGE,
                                callback=callback,
                                should_cancel=should_cancel,
                                asset_index=asset.index,
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
                                media_type=MediaType.VIDEO,
                                callback=callback,
                                should_cancel=should_cancel,
                                asset_index=asset.index,
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
        media_type: MediaType,
        callback: EventCallback | None,
        should_cancel: CancelCallback,
        asset_index: int | None = None,
    ) -> tuple[Path, RemoteAsset]:
        errors: list[str] = []
        for asset in assets:
            for candidate in asset.candidates:
                if should_cancel():
                    raise DownloadCancelledError("Task cancelled")
                response = None
                try:
                    response = ydl.urlopen(
                        Request(
                            candidate,
                            headers={
                                "Referer": source_url,
                                "Accept": "*/*",
                            },
                        )
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
                            f"Media server returned {content_type} instead of a media file"
                        )
                    first_chunk = response.read(1024 * 1024)
                    if not first_chunk:
                        raise MediaDownloadError(
                            "Media server returned an empty response"
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
                    temporary = path.with_name(
                        f"{path.name}.{threading.get_ident()}.part"
                    )
                    total_header = response.headers.get("Content-Length")
                    total = (
                        int(total_header)
                        if total_header and total_header.isdigit()
                        else None
                    )
                    downloaded = len(first_chunk)
                    try:
                        with temporary.open("wb") as handle:
                            handle.write(first_chunk)
                            if callback:
                                callback(
                                    EngineEvent(
                                        event="downloading",
                                        progress=TransferProgress(
                                            downloaded_bytes=downloaded,
                                            total_bytes=total,
                                            percent=(
                                                min(100.0, downloaded * 100.0 / total)
                                                if total
                                                else None
                                            ),
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
                                                percent=(
                                                    min(
                                                        100.0,
                                                        downloaded * 100.0 / total,
                                                    )
                                                    if total
                                                    else None
                                                ),
                                                filename=str(path),
                                            ),
                                        )
                                    )
                            if total is not None and downloaded != total:
                                raise MediaDownloadError(
                                    f"Incomplete media response: expected {total} bytes, "
                                    f"received {downloaded}"
                                )
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.replace(temporary, path)
                    except BaseException:
                        temporary.unlink(missing_ok=True)
                        raise
                    return path.resolve(), asset
                except DownloadCancelledError:
                    raise
                except Exception as exc:
                    errors.append(f"{candidate}: {exc}")
                finally:
                    if response is not None:
                        response.close()
        detail = errors[-1] if errors else "No asset URLs were available"
        raise MediaDownloadError(f"All original media URLs failed: {detail}")

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
        if first_bytes.startswith(b"\xff\xd8\xff"):
            return "jpg"
        if first_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        if first_bytes.startswith((b"GIF87a", b"GIF89a")):
            return "gif"
        if first_bytes.startswith(b"RIFF") and first_bytes[8:12] == b"WEBP":
            return "webp"
        if len(first_bytes) >= 12 and first_bytes[4:8] == b"ftyp":
            brand = first_bytes[8:12]
            if brand in {b"avif", b"avis"}:
                return "avif"
            if brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
                return "heic"
            if media_type == MediaType.VIDEO:
                return "mp4"
        extensions = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
            "image/avif": "avif",
            "image/gif": "gif",
            "video/mp4": "mp4",
            "video/webm": "webm",
            "video/quicktime": "mov",
        }
        if content_type in extensions:
            return extensions[content_type]
        suffix = Path(unquote(urlsplit(url).path)).suffix.lower().lstrip(".")
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
