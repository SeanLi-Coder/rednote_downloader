#!/usr/bin/env python3
"""Run bounded, anonymous production discovery and downloads for public items."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import multiprocessing
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Sequence
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app import douyin_signing
from app.downloader import (
    DownloaderConfig,
    MediaDownloader,
    safe_external_error_message,
)
from app.models import MediaType, Platform, SourceKind
from app.platforms import identify_url


def target_url(value: str) -> tuple[str, str]:
    info = identify_url(value)
    if info.platform != Platform.DOUYIN or info.kind != SourceKind.ITEM:
        raise ValueError("Only an unambiguous Douyin item URL is accepted")
    match = re.fullmatch(r"https://www\.douyin\.com/video/(\d{1,30})", info.url)
    if not match:
        raise ValueError("The normalized URL did not contain a safe item ID")
    return info.url, match.group(1)


def profile_observer_target(value: str) -> dict[str, str]:
    """Accept only an official profile URL with two explicit numeric item IDs."""
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.douyin.com"
        or not re.fullmatch(r"/user/(?:self|MS4w[A-Za-z0-9_-]{1,180})", parsed.path)
        or parsed.fragment
        or len(value) > 2048
        or any(ord(character) < 33 for character in value)
    ):
        raise ValueError("The observer requires a standard official Douyin profile URL")
    query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=12)
    if set(query) - {"modal_id", "vid", "from_tab_name", "showTab"}:
        raise ValueError("The observer URL contains unsupported query fields")
    ids = {}
    for key in ("modal_id", "vid"):
        values = query.get(key, [])
        if len(values) != 1 or not re.fullmatch(r"[0-9]{1,30}", values[0]):
            raise ValueError(
                "The observer requires exactly one numeric modal_id and vid"
            )
        ids[key] = values[0]
    return ids


def observed_page_route(value: str) -> dict[str, Any]:
    """Retain route shape and numeric IDs, not account names or arbitrary queries."""
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.netloc != "www.douyin.com":
            return {"path": "other"}
        path = "other"
        if re.fullmatch(r"/video/[0-9]{1,30}", parsed.path):
            path = parsed.path
        elif parsed.path == "/user/self":
            path = parsed.path
        elif re.fullmatch(r"/user/MS4w[A-Za-z0-9_-]{1,180}", parsed.path):
            path = "/user/<profile>"
        query = parse_qs(parsed.query, max_num_fields=32)
        ids = {
            key: [
                value
                for value in query.get(key, [])
                if re.fullmatch(r"[0-9]{1,30}", value)
            ][:4]
            for key in ("modal_id", "vid", "aweme_id")
            if any(re.fullmatch(r"[0-9]{1,30}", value) for value in query.get(key, []))
        }
        return {"path": path, "numeric_query_ids": ids}
    except ValueError:
        return {"path": "other"}


def observed_detail_request_id(value: str) -> str | None:
    """Observe request IDs only; recommendation response lists are not inspected."""
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "www.douyin.com"
            or parsed.path != "/aweme/v1/web/aweme/detail/"
        ):
            return None
        values = parse_qs(parsed.query, max_num_fields=150).get("aweme_id", [])
        if len(values) == 1 and re.fullmatch(r"[0-9]{1,30}", values[0]):
            return values[0]
    except ValueError:
        pass
    return None


def observer_request_allowed(resource_type: str, value: str) -> bool:
    """Permit page code and official JSON APIs, never media or image requests."""
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            return False
        if resource_type == "document":
            return parsed.netloc == "www.douyin.com" and bool(
                re.fullmatch(
                    r"/|/user/[A-Za-z0-9_-]{1,184}|/video/[0-9]{1,30}", parsed.path
                )
            )
        if resource_type in {"script", "stylesheet"}:
            return True
        return (
            resource_type in {"xhr", "fetch"}
            and parsed.netloc == "www.douyin.com"
            and parsed.path.startswith(("/aweme/v1/web/", "/api/"))
            and not re.search(r"(?:play|download|media)(?:/|$)", parsed.path)
        )
    except ValueError:
        return False


_PAGE_ID_OBSERVER = r"""() => {
    const numeric = value => typeof value === 'string' && /^[0-9]{1,30}$/.test(value);
    const dom = [];
    const attributes = ['data-e2e-vid', 'data-aweme-id', 'data-video-id'];
    const videos = [...document.querySelectorAll('video')].slice(0, 24);
    for (const video of videos) {
        let node = video;
        for (let depth = 0; node && depth < 8 && dom.length < 96; depth++, node = node.parentElement) {
            for (const attribute of attributes) {
                const value = node.getAttribute(attribute);
                if (numeric(value)) dom.push({source: depth === 0 ? 'video' : 'ancestor', attribute, id: value});
            }
        }
    }
    const state = [];
    const fields = new Set(['modal_id', 'modalId', 'vid', 'aweme_id', 'awemeId']);
    const seen = new WeakSet();
    let budget = 6000;
    const walk = (value, source, depth) => {
        if (!value || typeof value !== 'object' || depth > 14 || budget-- <= 0 || state.length >= 96 || seen.has(value)) return;
        seen.add(value);
        for (const [key, entry] of Object.entries(value).slice(0, 100)) {
            if (fields.has(key) && numeric(entry)) state.push({source, field: key, id: entry});
            if (state.length >= 96) return;
            walk(entry, source, depth + 1);
        }
    };
    for (const source of ['_ROUTER_DATA', '__ROUTER_DATA__', '__INITIAL_STATE__', '__NEXT_DATA__']) {
        try { walk(window[source], source, 0); } catch (_) {}
    }
    const render = document.getElementById('RENDER_DATA');
    if (render && render.textContent.length < 2000000) {
        try { walk(JSON.parse(decodeURIComponent(render.textContent)), 'RENDER_DATA', 0); } catch (_) {}
    }
    return {video_node_count: videos.length, video_node_ids: dom, script_state_ids: state};
}"""


def sanitized_page_ids(value: Any) -> dict[str, Any]:
    """Validate browser-returned fields again before writing the report."""
    value = value if isinstance(value, dict) else {}
    count = value.get("video_node_count")
    result = {
        "video_node_count": count if type(count) is int and 0 <= count <= 24 else 0
    }
    for collection, valid_fields in (
        (
            "video_node_ids",
            {
                "source": {"video", "ancestor"},
                "attribute": {"data-e2e-vid", "data-aweme-id", "data-video-id"},
            },
        ),
        (
            "script_state_ids",
            {
                "source": {
                    "_ROUTER_DATA",
                    "__ROUTER_DATA__",
                    "__INITIAL_STATE__",
                    "__NEXT_DATA__",
                    "RENDER_DATA",
                },
                "field": {"modal_id", "modalId", "vid", "aweme_id", "awemeId"},
            },
        ),
    ):
        entries = value.get(collection)
        result[collection] = []
        for entry in entries[:96] if isinstance(entries, list) else []:
            if (
                isinstance(entry, dict)
                and isinstance(entry.get("id"), str)
                and re.fullmatch(r"[0-9]{1,30}", entry["id"])
                and all(
                    isinstance(entry.get(key), str) and entry[key] in allowed
                    for key, allowed in valid_fields.items()
                )
            ):
                safe = {key: entry[key] for key in (*valid_fields, "id")}
                if safe not in result[collection]:
                    result[collection].append(safe)
    return result


def _profile_observer_worker(url: str, connection) -> None:
    if hasattr(os, "setsid"):
        os.setsid()
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    result: dict[str, Any] = {"status": "failed", "detail_request_ids": []}
    try:
        from playwright.sync_api import sync_playwright

        result["expected_ids"] = profile_observer_target(url)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                channel="chrome", headless=True, timeout=15_000
            )
            try:
                context = browser.new_context(
                    service_workers="block", accept_downloads=False
                )
                context.set_default_timeout(5_000)
                context.route(
                    "**/*",
                    lambda route: (
                        route.continue_()
                        if observer_request_allowed(
                            route.request.resource_type, route.request.url
                        )
                        else route.abort()
                    ),
                )
                page = context.new_page()

                def request_seen(request):
                    media_id = observed_detail_request_id(request.url)
                    if (
                        media_id
                        and media_id not in result["detail_request_ids"]
                        and len(result["detail_request_ids"]) < 96
                    ):
                        result["detail_request_ids"].append(media_id)

                page.on("request", request_seen)
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                except Exception as exc:
                    result["navigation_error_type"] = type(exc).__name__
                page.wait_for_timeout(8_000)
                result["final_route"] = observed_page_route(page.url)
                result.update(sanitized_page_ids(page.evaluate(_PAGE_ID_OBSERVER)))
                result["status"] = "observed"
            finally:
                browser.close()
    except Exception as exc:
        result["error_type"] = type(exc).__name__
    connection.send({"result": result})
    connection.close()


def run_profile_observer(url: str) -> dict[str, Any]:
    ids = profile_observer_target(url)
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_profile_observer_worker, args=(url, sender))
    started = time.monotonic()
    result = None
    process.start()
    sender.close()
    try:
        # Reserve six seconds for the existing bounded process-group cleanup.
        while time.monotonic() - started < 54:
            if receiver.poll(min(1, max(0, 54 - (time.monotonic() - started)))):
                try:
                    result = receiver.recv().get("result")
                except EOFError:
                    pass
                break
            if not process.is_alive():
                break
    finally:
        _stop_worker(process)
        receiver.close()
    if result is None:
        result = {
            "status": "failed",
            "expected_ids": ids,
            "error_type": "ObserverWorkerDidNotFinish",
        }
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


def _safe_scalar(value: Any) -> Any:
    if value is None or type(value) is bool:
        return value
    if type(value) is int and abs(value) < 10**30:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,30}", value):
        return value
    return {"type": type(value).__name__}


def _keys(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    return sorted(
        key
        for key in value
        if isinstance(key, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", key)
    )[:150]


def ssr_shape(wrapper: Any) -> dict[str, Any]:
    if not isinstance(wrapper, dict):
        return {"wrapper_type": type(wrapper).__name__}
    aweme = wrapper.get("aweme")
    aweme = aweme if isinstance(aweme, dict) else {}
    detail = aweme.get("detail")
    detail = detail if isinstance(detail, dict) else {}
    video = detail.get("video")
    video = video if isinstance(video, dict) else {}
    author = detail.get("authorInfo")
    bitrates = video.get("bitRateList")
    return {
        "wrapper": {
            key: _safe_scalar(wrapper.get(key))
            for key in ("awemeId", "statusCode", "redirect", "isSpider")
        },
        "aweme": {
            key: _safe_scalar(aweme.get(key))
            for key in ("statusCode", "isUnknownAweme")
        },
        "detail": {
            key: _safe_scalar(detail.get(key))
            for key in ("awemeId", "groupId", "awemeType", "mediaType")
        },
        "detail_keys": _keys(detail),
        "video_keys": _keys(video),
        "bitrate_keys": (
            [_keys(value) for value in bitrates[:30]]
            if isinstance(bitrates, list)
            else []
        ),
        "author_has_sec_uid": isinstance(author, dict) and bool(author.get("secUid")),
    }


def exception_chain(exc: BaseException) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen and len(result) < 8:
        seen.add(id(exc))
        entry: dict[str, Any] = {
            "type": type(exc).__name__,
            "message": safe_external_error_message(exc),
        }
        code = getattr(exc, "issue_code", None)
        if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", code):
            entry["issue_code"] = code
        result.append(entry)
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    return result


def quality_duration(value: Any) -> float | None:
    if type(value) not in (int, float, str):
        return None
    try:
        duration = float(value)
    except (ValueError, OverflowError):
        return None
    return duration if math.isfinite(duration) and 0 < duration <= 604_800 else None


def quality_metrics(value: Any) -> dict[str, int | float | None]:
    """Copy bounded numeric fields only; never serialize media response payloads."""
    value = value if isinstance(value, dict) else {}
    bounds = {
        "width": 16_384,
        "height": 16_384,
        "bit_rate": 10**12,
        "filesize": 10**15,
    }
    metrics = {
        field: (
            raw if type(raw := value.get(field)) is int and 0 <= raw <= bound else None
        )
        for field, bound in bounds.items()
    }
    metrics["duration_seconds"] = quality_duration(value.get("duration"))
    return metrics


@contextlib.contextmanager
def observe_quality_probes(engine: MediaDownloader, reports: list[dict[str, Any]]):
    """Observe real selectors and probes without replacing their decisions."""
    original_video = engine._add_douyin_probe_formats_scoped
    original_live_photo = engine._select_highest_douyin_live_photo_asset_scoped
    original_probe = engine._probe_douyin_ratio_with_retry
    original_unresolved = engine._unresolved_douyin_direct_failures
    active_scopes: list[dict[str, Any] | None] = []

    @contextlib.contextmanager
    def observed_scope(kind, candidates):
        record = None
        if len(reports) < 32:
            candidates = candidates if isinstance(candidates, (list, tuple)) else []
            record = {
                "kind": kind,
                "status": "running",
                "declared_candidates": [
                    quality_metrics(value) for value in candidates[:16]
                ],
                "probes": [],
            }
            reports.append(record)
        active_scopes.append(record)
        try:
            yield
        except BaseException:
            if record is not None:
                record["status"] = "raised"
            raise
        else:
            if record is not None:
                record["status"] = "returned"
        finally:
            active_scopes.pop()

    def observed_video(ydl, info, *args, **kwargs):
        candidates = (
            info.get("_douyin_direct_candidates") if isinstance(info, dict) else []
        )
        with observed_scope("video", candidates):
            return original_video(ydl, info, *args, **kwargs)

    def observed_live_photo(ydl, asset, *args, **kwargs):
        with observed_scope("live_photo", getattr(asset, "quality_candidates", None)):
            return original_live_photo(ydl, asset, *args, **kwargs)

    def observed_probe(ydl, candidate_url, *args, **kwargs):
        scope = active_scopes[-1] if active_scopes else None
        record = None
        if scope is not None and len(scope["probes"]) < 120:
            raw_label = kwargs.get("ratio")
            label = (
                raw_label
                if isinstance(raw_label, str)
                and re.fullmatch(r"default|author-feed-[1-9][0-9]{0,2}", raw_label)
                else "other"
            )
            record = {
                "label": label,
                "expected_duration_seconds": quality_duration(
                    kwargs.get("expected_duration")
                ),
                "endpoint_attempt": 1
                + sum(probe["label"] == label for probe in scope["probes"]),
                "status": "running",
            }
            scope["probes"].append(record)
        try:
            result = original_probe(ydl, candidate_url, *args, **kwargs)
        except BaseException:
            if record is not None:
                record["status"] = "raised"
            raise
        if record is not None:
            record["status"] = "returned" if result else "empty"
            record["measured"] = quality_metrics(result)
            if record["label"].startswith("author-feed-"):
                index = int(record["label"].rsplit("-", 1)[1]) - 1
                if index < len(scope["declared_candidates"]):
                    declared = scope["declared_candidates"][index]
                    measured = record["measured"]
                    dimensions = [
                        declared["width"],
                        declared["height"],
                        measured["width"],
                        measured["height"],
                    ]
                    if all(value is not None and value > 0 for value in dimensions):
                        record["meets_declared_dimensions"] = min(
                            dimensions[2:]
                        ) >= min(dimensions[:2]) and max(dimensions[2:]) >= max(
                            dimensions[:2]
                        )
        return result

    def observed_unresolved(*args, **kwargs):
        result = original_unresolved(*args, **kwargs)
        scope = active_scopes[-1] if active_scopes else None
        if scope is not None and isinstance(result, list):
            scope["unresolved_candidate_count"] = len(result)
        return result

    with (
        mock.patch.object(engine, "_add_douyin_probe_formats_scoped", observed_video),
        mock.patch.object(
            engine,
            "_select_highest_douyin_live_photo_asset_scoped",
            observed_live_photo,
        ),
        mock.patch.object(engine, "_probe_douyin_ratio_with_retry", observed_probe),
        mock.patch.object(
            engine, "_unresolved_douyin_direct_failures", observed_unresolved
        ),
    ):
        yield


@contextlib.contextmanager
def force_signed_detail_adapter(url: str, *, enabled: bool = False):
    """Inject only the exact item's primary extraction miss for fallback testing."""
    state = {"extract_count": 0}
    if not enabled:
        yield state
        return
    if target_url(url)[0] != url:
        raise ValueError("Forced signing requires an exact canonical item URL")
    original_extract = YoutubeDL.extract_info

    def extract_info(self, source_url, *args, **kwargs):
        download = kwargs.get("download", args[0] if args else True)
        process = kwargs.get("process", args[3] if len(args) > 3 else True)
        if source_url == url and download is False and process is False:
            state["extract_count"] += 1
            raise DownloadError("Douyin returned an empty aweme detail")
        return original_extract(self, source_url, *args, **kwargs)

    with mock.patch.object(YoutubeDL, "extract_info", extract_info):
        yield state


@contextlib.contextmanager
def force_ssr_detail_adapter(url: str, *, enabled: bool = False):
    """Suppress only validated target capture success to exercise real SSR parsing."""
    state = {
        "capture_suppressed_count": 0,
        "ssr_attempt_count": 0,
        "ssr_success_count": 0,
    }
    if not enabled:
        yield state
        return
    canonical_url, media_id = target_url(url)
    if canonical_url != url:
        raise ValueError("Forced SSR requires an exact canonical item URL")
    original_capture = douyin_signing._PageDetailCapture.handle_request_finished
    original_ssr = douyin_signing._read_ssr_aweme_detail_from_page

    def capture_finished(capture, request):
        # Always execute production response binding, authentication, identity,
        # content-size and network checks before suppressing a successful capture.
        result = original_capture(capture, request)
        if (
            capture.aweme_id == media_id
            and douyin_signing._is_target_page_detail_request(request, media_id)
            and capture.detail is not None
            and capture.authentication_failure is None
            and capture.identity_failure is None
            and capture.terminal_failure is None
        ):
            capture.detail = None
            state["capture_suppressed_count"] += 1
        return result

    def read_ssr(page, aweme_id, expected_sec_uid):
        if aweme_id == media_id:
            state["ssr_attempt_count"] += 1
        result = original_ssr(page, aweme_id, expected_sec_uid)
        if (
            aweme_id == media_id
            and isinstance(result, dict)
            and result.get("aweme_id") == media_id
        ):
            state["ssr_success_count"] += 1
        return result

    with (
        mock.patch.object(
            douyin_signing._PageDetailCapture,
            "handle_request_finished",
            capture_finished,
        ),
        mock.patch.object(douyin_signing, "_read_ssr_aweme_detail_from_page", read_ssr),
    ):
        yield state


@contextlib.contextmanager
def anonymous_browser_adapter(shapes: list[dict[str, Any]]):
    """Adapt only cookie access; preserve actual responses and validators."""
    empty_jar = CookieJar()
    original_cookie_converter = douyin_signing._cookie_jar_to_playwright
    original_options = MediaDownloader._douyin_ytdlp_options
    original_validator = douyin_signing._validated_ssr_wrapper_detail

    def empty_cookies(jar: CookieJar) -> list[dict[str, Any]]:
        if jar is not empty_jar:
            raise RuntimeError("The anonymous smoke received an unexpected cookie jar")
        return original_cookie_converter(jar) if list(jar) else []

    def anonymous_options(self, *args, **kwargs):
        options = original_options(self, *args, **kwargs)
        options.pop("cookiesfrombrowser", None)
        return options

    def observed_validator(wrapper, *args, **kwargs):
        if len(shapes) < 12:
            shapes.append(ssr_shape(wrapper))
        return original_validator(wrapper, *args, **kwargs)

    with (
        mock.patch.object(
            douyin_signing, "_load_chrome_cookie_jar", return_value=empty_jar
        ),
        mock.patch.object(
            douyin_signing, "_cookie_jar_to_playwright", side_effect=empty_cookies
        ),
        mock.patch.object(MediaDownloader, "_douyin_ytdlp_options", anonymous_options),
        mock.patch.object(
            douyin_signing, "_validated_ssr_wrapper_detail", observed_validator
        ),
    ):
        yield


def inspect_media(path: Path, output_root: Path, ffprobe: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("The output is not a regular media file")
    resolved = path.resolve(strict=True)
    if output_root.resolve() not in resolved.parents:
        raise RuntimeError("The output escaped the temporary media directory")
    size = path.stat().st_size
    if size <= 0:
        raise RuntimeError("The downloaded media file was empty")
    probe = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height,duration:format=duration,format_name",
            "-of",
            "json",
            str(resolved),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    payload = json.loads(probe.stdout)
    videos = [
        stream
        for stream in payload.get("streams", [])
        if stream.get("codec_type") == "video"
    ]
    if not videos:
        raise RuntimeError("FFprobe found no video or image stream")
    stream = videos[0]
    width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    if min(width, height) <= 0:
        raise RuntimeError("FFprobe returned invalid media dimensions")
    image_extensions = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".heic"}
    is_image = resolved.suffix.lower() in image_extensions
    raw_duration = payload.get("format", {}).get("duration") or stream.get("duration")
    duration = float(raw_duration) if raw_duration and raw_duration != "N/A" else None
    if not is_image and (duration is None or duration <= 0):
        raise RuntimeError("FFprobe returned no positive video duration")
    digest = hashlib.sha256()
    with resolved.open("rb") as media:
        while chunk := media.read(1024 * 1024):
            digest.update(chunk)
    return {
        "type": "image" if is_image else "video",
        "width": width,
        "height": height,
        "duration_seconds": duration,
        "size_bytes": size,
        "sha256": digest.hexdigest(),
    }


def _worker(
    url: str,
    media_id: str,
    output_dir: str,
    ffprobe: str,
    connection,
    force_signed_detail: bool = False,
    force_ssr_detail: bool = False,
) -> None:
    force_signed_detail = force_signed_detail or force_ssr_detail
    if hasattr(os, "setsid"):
        os.setsid()
    # Native libraries and browser subprocesses must never print raw network data.
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    stage = "identify"
    shapes: list[dict[str, Any]] = []
    quality_reports: list[dict[str, Any]] = []
    last_event = ""

    def event_callback(event):
        nonlocal last_event
        if event.event in {
            "probing",
            "downloading",
            "postprocessing",
            "asset_completed",
        }:
            if event.event != last_event:
                connection.send({"stage": stage, "event": event.event})
                last_event = event.event

    force_state = {"extract_count": 0}
    ssr_state = {
        "capture_suppressed_count": 0,
        "ssr_attempt_count": 0,
        "ssr_success_count": 0,
    }
    result: dict[str, Any] = {
        "media_id": media_id,
        "status": "failed",
        "media": [],
        "forced_signed_detail": force_signed_detail,
        "forced_ssr_detail": force_ssr_detail,
    }
    try:
        info = identify_url(url)
        engine = MediaDownloader(
            DownloaderConfig(cookie_browser="chrome", allow_cookie_fallback=False),
            discovery_callback=event_callback,
        )
        with (
            anonymous_browser_adapter(shapes),
            observe_quality_probes(engine, quality_reports),
            force_signed_detail_adapter(
                info.url, enabled=force_signed_detail
            ) as force_state,
            force_ssr_detail_adapter(info.url, enabled=force_ssr_detail) as ssr_state,
        ):
            stage = "discover"
            connection.send({"stage": stage})
            discovery = engine.discover(info.url, info.platform, info.kind)
            if not discovery.discovery_complete or len(discovery.items) != 1:
                raise RuntimeError("Discovery did not return exactly one complete item")
            item = discovery.items[0]
            if item.media_id != media_id or target_url(item.source_url)[1] != media_id:
                raise RuntimeError("Discovery returned a different item identity")
            if discovery.cookie_fallback_used:
                raise RuntimeError("Discovery unexpectedly used cookie fallback")
            if force_ssr_detail and ssr_state["ssr_success_count"] == 0:
                raise RuntimeError(
                    "Forced SSR discovery did not return verified exact-item SSR metadata"
                )
            result["discovered_type"] = item.media_type.value
            stage = "download"
            connection.send({"stage": stage})
            outcome = engine.download_item(
                item, info.platform, output_dir, callback=event_callback
            )
            if outcome.cookie_fallback_used or not outcome.output_paths:
                raise RuntimeError("Download returned no media or used cookie fallback")
            stage = "ffprobe"
            connection.send({"stage": stage})
            result["media"] = [
                inspect_media(Path(path), Path(output_dir), ffprobe)
                for path in outcome.output_paths
            ]
            if item.media_type == MediaType.VIDEO and not any(
                media["type"] == "video" for media in result["media"]
            ):
                raise RuntimeError("A video item produced only still images")
            if force_signed_detail and force_state["extract_count"] == 0:
                raise RuntimeError(
                    "Forced signed discovery did not exercise the primary extractor miss"
                )
        result["status"] = "passed"
    except Exception as exc:
        result["exception_chain"] = exception_chain(exc)
    result.update(
        stage=stage,
        ssr_shapes=shapes,
        quality_probes=quality_reports,
        forced_extract_count=force_state["extract_count"],
        forced_ssr_capture_suppressed_count=ssr_state["capture_suppressed_count"],
        forced_ssr_attempt_count=ssr_state["ssr_attempt_count"],
        forced_ssr_success_count=ssr_state["ssr_success_count"],
    )
    connection.send({"result": result})
    connection.close()


def _stop_worker(process) -> None:
    if process.pid and os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    elif process.is_alive():
        process.terminate()
    process.join(timeout=3)
    if process.pid and os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.is_alive():
        process.kill()
    process.join(timeout=3)


def run_item(
    url: str,
    media_id: str,
    ffprobe: str,
    timeout: int,
    *,
    force_signed_detail: bool = False,
    force_ssr_detail: bool = False,
) -> dict[str, Any]:
    force_signed_detail = force_signed_detail or force_ssr_detail
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    started = time.monotonic()
    stage = "starting"
    result = None
    with tempfile.TemporaryDirectory(prefix="douyin-item-smoke-") as directory:
        process = context.Process(
            target=_worker,
            args=(
                url,
                media_id,
                directory,
                ffprobe,
                sender,
                force_signed_detail,
                force_ssr_detail,
            ),
        )
        process.start()
        sender.close()
        try:
            while time.monotonic() - started < timeout:
                if receiver.poll(1):
                    try:
                        message = receiver.recv()
                    except EOFError:
                        break
                    if "result" in message:
                        result = message["result"]
                        break
                    stage = message["stage"]
                    print(
                        f"Item {media_id}: {stage} {message.get('event', '')}".rstrip(),
                        flush=True,
                    )
                elif not process.is_alive():
                    break
        finally:
            _stop_worker(process)
            receiver.close()
    elapsed = round(time.monotonic() - started, 3)
    if result is None:
        result = {
            "media_id": media_id,
            "status": "failed",
            "stage": stage,
            "forced_signed_detail": force_signed_detail,
            "forced_ssr_detail": force_ssr_detail,
            "exception_chain": [
                {
                    "type": (
                        "SmokeDeadlineExceeded"
                        if elapsed >= timeout
                        else "SmokeWorkerExited"
                    ),
                    "message": "The worker did not finish",
                }
            ],
            "media": [],
        }
    result["elapsed_seconds"] = elapsed
    print(f"Item {media_id}: {result['status']} after {elapsed}s", flush=True)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--url", action="append")
    modes.add_argument("--observe-profile-item-url")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--item-timeout", type=int, default=600)
    parser.add_argument(
        "--force-signed-detail",
        action="store_true",
        help="Inject an exact-item primary extraction miss to test real signed fallback",
    )
    parser.add_argument(
        "--force-ssr-detail",
        action="store_true",
        help="Force signed fallback and suppress validated page capture to test real SSR",
    )
    args = parser.parse_args(argv)
    args.force_signed_detail = args.force_signed_detail or args.force_ssr_detail
    if args.observe_profile_item_url and args.force_signed_detail:
        parser.error("Forced signed discovery requires item URL mode")
    if args.observe_profile_item_url:
        try:
            result = run_profile_observer(args.observe_profile_item_url)
        except Exception as exc:
            result = {"status": "failed", "error_type": type(exc).__name__}
        result["description"] = (
            "Anonymous official profile-page observation only; no user cookies or media downloads. "
            "Numeric IDs are observations, not proof of the selected item's identity."
        )
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"Anonymous profile observation: {result['status']}", flush=True)
        return 0 if result["status"] == "observed" else 1
    if not 30 <= args.item_timeout <= 900 or not 1 <= len(args.url) <= 8:
        parser.error("Use 1-8 item URLs and a 30-900 second per-item timeout")
    report: dict[str, Any] = {
        "description": "Anonymous production discovery and download; no user cookies. Temporary media is removed after validation; only sanitized results are retained.",
        "forced_signed_detail": args.force_signed_detail,
        "forced_ssr_detail": args.force_ssr_detail,
        "status": "running",
        "items": [],
    }
    if args.force_signed_detail:
        report["description"] += (
            " Diagnostic injection forces only the exact item's primary extractor "
            "to fail; signed discovery, identity guards and media downloads remain "
            "real. This is not evidence that the primary API naturally failed."
        )
    if args.force_ssr_detail:
        report["description"] += (
            " Forced SSR additionally suppresses only successful exact-item page API "
            "captures after their production validation; authentication, identity and "
            "network failures remain authoritative. A pass requires actual verified "
            "SSR metadata, not a later signed-API fallback."
        )

    def save():
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    save()
    try:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            raise RuntimeError("FFprobe was not found on PATH")
        targets = [target_url(url) for url in args.url]
        for url, media_id in targets:
            report["items"].append(
                run_item(
                    url,
                    media_id,
                    ffprobe,
                    args.item_timeout,
                    force_signed_detail=args.force_signed_detail,
                    force_ssr_detail=args.force_ssr_detail,
                )
            )
            save()
        report["status"] = (
            "passed"
            if all(item["status"] == "passed" for item in report["items"])
            else "failed"
        )
    except Exception as exc:
        report.update(status="failed", exception_chain=exception_chain(exc))
    save()
    print(f"Anonymous item smoke: {report['status']}", flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
