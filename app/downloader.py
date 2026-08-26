from __future__ import annotations

import contextlib
import hashlib
import mimetypes
import os
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import unquote, urlsplit

from yt_dlp import YoutubeDL
from yt_dlp.networking import Request
from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.utils import DownloadCancelled, DownloadError

from .douyin import discover_profile as discover_douyin_profile
from .errors import (
    AuthenticationRequiredError,
    DiscoveryError,
    DownloadCancelledError,
    MediaDownloadError,
)
from .models import DownloadItem, MediaType, Platform, SourceKind, TransferProgress
from .platforms import identify_url
from .xiaohongshu import RemoteAsset, discover_profile as discover_xhs_profile
from .xiaohongshu import parse_note as parse_xhs_note


MAX_TITLE_BYTES = 180
MAX_MEDIA_ID_BYTES = 64
MAX_FILENAME_COMPONENT_BYTES = 255
RESERVED_EXTENSION_BYTES = 16
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
_INVALID_FILENAME = re.compile(r"[\x00-\x1f<>:\"/\\|?*]+")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


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
            items = [
                DownloadItem(
                    id=_item_key(platform, self._media_id(entry), entry, index),
                    media_id=self._media_id(entry),
                    source_url=entry,
                    title=self._media_id(entry) or "Douyin video",
                    author=profile.author,
                    playlist_index=index,
                    extractor_key="Douyin",
                    media_type=MediaType.VIDEO,
                )
                for index, entry in enumerate(profile.video_urls, start=1)
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
        if _is_auth_error(message):
            raise AuthenticationRequiredError(
                "The site requires login, fresh browser cookies, or a CAPTCHA. "
                "Complete verification in Chrome and retry.",
                verification_url=url,
            ) from exc
        raise MediaDownloadError(message) from exc

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
                )
            )
        return DiscoveryResult(
            author=author,
            items=items,
            cookie_fallback_used=fallback,
            warning=COOKIE_FALLBACK_WARNING if fallback else None,
        )

    def _download_with_ytdlp(
        self,
        item: DownloadItem,
        output_dir: Path,
        *,
        callback: EventCallback | None,
        should_cancel: CancelCallback,
    ) -> DownloadOutcome:
        final_paths: list[str] = []

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
                "format": "bestvideo*+bestaudio/best",
                "paths": {"home": str(output_dir), "temp": str(parts_dir)},
                "outtmpl": {"default": OUTPUT_TEMPLATE},
                "windowsfilenames": True,
                "continuedl": True,
                "overwrites": True,
                "ignoreerrors": False,
                "noplaylist": True,
                "concurrent_fragment_downloads": self.config.concurrent_fragments,
                "progress_hooks": [progress_hook],
                "postprocessor_hooks": [postprocessor_hook],
                "post_hooks": [post_hook],
                "logger": logger,
            }
            with YoutubeDL(options) as ydl:
                ydl.add_post_processor(
                    _SafeFilenamePostProcessor(
                        ydl,
                        title_byte_limit=self.config.filename_limit,
                        fallback_media_id=item.media_id or item.id,
                    ),
                    when="pre_process",
                )
                result = ydl.extract_info(item.source_url, download=True)
                if not isinstance(result, dict):
                    raise DownloadError("The URL returned no downloadable media")
                candidates = self._paths_from_info(result, ydl)
            return result, [*final_paths, *candidates]

        (info, paths), fallback = self._run_with_cookie_fallback(
            operation, url=item.source_url
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
        for key in (
            "uploader",
            "channel",
            "playlist_uploader",
            "creator",
            "artist",
        ):
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
