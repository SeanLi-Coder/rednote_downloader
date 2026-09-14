#!/usr/bin/env python3
"""Run bounded, anonymous production discovery and downloads for public items."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
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


@contextlib.contextmanager
def anonymous_browser_adapter(shapes: list[dict[str, Any]]):
    """Adapt only cookie access; preserve actual responses and validators."""
    empty_jar = CookieJar()
    original_options = MediaDownloader._douyin_ytdlp_options
    original_validator = douyin_signing._validated_ssr_wrapper_detail

    def empty_cookies(jar: CookieJar) -> list[dict[str, Any]]:
        if jar is not empty_jar or list(jar):
            raise RuntimeError("The anonymous smoke received an unexpected cookie jar")
        return []

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
    if list(empty_jar):
        raise RuntimeError("The anonymous smoke retained browser cookies")


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


def _worker(url: str, media_id: str, output_dir: str, ffprobe: str, connection) -> None:
    if hasattr(os, "setsid"):
        os.setsid()
    # Native libraries and browser subprocesses must never print raw network data.
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    stage = "identify"
    shapes: list[dict[str, Any]] = []
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

    result: dict[str, Any] = {"media_id": media_id, "status": "failed", "media": []}
    try:
        info = identify_url(url)
        engine = MediaDownloader(
            DownloaderConfig(cookie_browser="chrome", allow_cookie_fallback=False),
            discovery_callback=event_callback,
        )
        with anonymous_browser_adapter(shapes):
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
        result["status"] = "passed"
    except Exception as exc:
        result["exception_chain"] = exception_chain(exc)
    result.update(stage=stage, ssr_shapes=shapes)
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


def run_item(url: str, media_id: str, ffprobe: str, timeout: int) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    started = time.monotonic()
    stage = "starting"
    result = None
    with tempfile.TemporaryDirectory(prefix="douyin-item-smoke-") as directory:
        process = context.Process(
            target=_worker, args=(url, media_id, directory, ffprobe, sender)
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
    parser.add_argument("--url", action="append", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--item-timeout", type=int, default=600)
    args = parser.parse_args(argv)
    if not 30 <= args.item_timeout <= 900 or not 1 <= len(args.url) <= 8:
        parser.error("Use 1-8 item URLs and a 30-900 second per-item timeout")
    report: dict[str, Any] = {
        "description": "Anonymous production discovery and download; no user cookies. Temporary media is removed after validation; only sanitized results are retained.",
        "status": "running",
        "items": [],
    }

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
            report["items"].append(run_item(url, media_id, ffprobe, args.item_timeout))
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
