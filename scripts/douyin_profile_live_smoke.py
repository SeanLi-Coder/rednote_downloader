#!/usr/bin/env python3
"""Run bounded anonymous profile discovery and sample real downloads near 80%."""
from __future__ import annotations

import argparse
import contextlib
import math
import multiprocessing
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Sequence
from unittest import mock
from urllib.parse import urlsplit

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app import douyin, douyin_signing
from app.downloader import DownloaderConfig, MediaDownloader
from app.errors import SiteIssueCode, classify_site_issue
from app.models import MediaType, Platform, SourceKind
from app.platforms import identify_url
from scripts.douyin_item_live_smoke import (
    _stop_worker,
    anonymous_browser_adapter,
    exception_chain,
    inspect_media,
    observe_quality_probes,
    target_url,
)


def profile_url(value: str) -> str:
    info = identify_url(value)
    if info.platform != Platform.DOUYIN or info.kind != SourceKind.PROFILE:
        raise ValueError("Only a public Douyin profile URL is accepted")
    path = urlsplit(info.url).path.rstrip("/")
    if not re.fullmatch(r"/user/MS4w[A-Za-z0-9_-]{8,200}", path):
        raise ValueError("The profile must contain a public author identifier")
    return f"https://www.douyin.com{path}"


def sample_indices(total: int) -> list[int]:
    """Return at most five consecutive zero-based indices centered near 80%."""
    if total <= 0:
        return []
    count = min(total, 5)
    center = math.ceil(total * 0.8) - 1
    start = max(0, min(center - count // 2, total - count))
    return list(range(start, start + count))


def target_duration_ms(item) -> int | None:
    metadata = item.metadata.get("douyin_profile_media")
    raw = metadata.get("duration_ms") if isinstance(metadata, dict) else None
    return raw if type(raw) is int and 0 < raw <= 86_400_000 else None


@contextlib.contextmanager
def anonymous_profile_adapter():
    """Cover both signing and profile-browser fallback cookie-loading paths."""
    original_converter = douyin._cookie_jar_to_playwright
    with anonymous_browser_adapter([]):
        jar = douyin_signing._load_chrome_cookie_jar(None)

        def convert_profile_cookies(candidate):
            if candidate is not jar:
                raise RuntimeError(
                    "The profile smoke received an unexpected cookie jar"
                )
            return original_converter(candidate)

        with (
            mock.patch.object(douyin, "_extract_cookies", return_value=jar),
            mock.patch.object(
                douyin, "_cookie_jar_to_playwright", convert_profile_cookies
            ),
        ):
            yield


def _safe_errors(exc: BaseException, sensitive_values=()) -> list[dict[str, Any]]:
    errors = exception_chain(exc)
    for error in errors:
        for value in sensitive_values:
            if isinstance(value, str) and value:
                error["message"] = error["message"].replace(value, "[redacted]")
    return errors


def exercise_profile(
    engine: MediaDownloader,
    url: str,
    output_dir: str,
    ffprobe: str,
    emit: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    """Preserve the exact profile-discovered objects and metadata during download."""
    result: dict[str, Any] = {"status": "running", "items": [], "stage": "discover"}
    emit({"snapshot": result})
    discovery = engine.discover(url, Platform.DOUYIN, SourceKind.PROFILE)
    result["discovered_count"] = len(discovery.items)
    result["discovery_complete"] = bool(discovery.discovery_complete)
    if discovery.cookie_fallback_used:
        raise RuntimeError("Discovery unexpectedly used cookie fallback")
    if not discovery.discovery_complete or not discovery.items:
        raise RuntimeError(
            "A complete nonempty profile is required to sample its 80% position"
        )
    indices = sample_indices(len(discovery.items))
    result["selected_ordinals"] = [index + 1 for index in indices]
    emit({"snapshot": result})
    for index in indices:
        item = discovery.items[index]
        media_id = item.media_id
        if not isinstance(media_id, str) or not re.fullmatch(r"[0-9]{1,30}", media_id):
            raise RuntimeError("Profile discovery returned an unsafe item identifier")
        if target_url(item.source_url)[1] != media_id:
            raise RuntimeError("Profile discovery returned a different item identity")
        entry: dict[str, Any] = {
            "media_id": media_id,
            "ordinal": index + 1,
            "target_duration_ms": target_duration_ms(item),
            "discovered_type": item.media_type.value,
            "status": "running",
            "media": [],
            "quality_probes": [],
        }
        result["items"].append(entry)
        result["stage"] = "download"
        emit({"snapshot": result})

        def event_callback(event):
            if event.event in {
                "probing",
                "downloading",
                "postprocessing",
                "asset_completed",
            }:
                emit(
                    {
                        "stage": result["stage"],
                        "ordinal": index + 1,
                        "event": event.event,
                    }
                )

        try:
            with observe_quality_probes(engine, entry["quality_probes"]):
                outcome = engine.download_item(
                    item, Platform.DOUYIN, output_dir, callback=event_callback
                )
            if outcome.cookie_fallback_used or not outcome.output_paths:
                raise RuntimeError("Download returned no media or used cookie fallback")
            result["stage"] = "ffprobe"
            entry["media"] = [
                inspect_media(Path(path), Path(output_dir), ffprobe)
                for path in outcome.output_paths
            ]
            if item.media_type == MediaType.VIDEO and not any(
                media["type"] == "video" for media in entry["media"]
            ):
                raise RuntimeError("A video item produced only still images")
            entry["status"] = "passed"
        except Exception as exc:
            entry["status"] = "failed"
            entry["exception_chain"] = _safe_errors(
                exc, (item.title, item.author, discovery.author)
            )
            code = classify_site_issue(exc)
            entry["issue_code"] = code.value
            if code in {
                SiteIssueCode.RATE_LIMITED,
                SiteIssueCode.LOGIN_REQUIRED,
                SiteIssueCode.VERIFICATION_REQUIRED,
                SiteIssueCode.SECURITY_BLOCKED,
                SiteIssueCode.REQUEST_REJECTED,
            }:
                result["stopped_for_site_issue"] = code.value
                emit({"snapshot": result})
                break
        emit({"snapshot": result})
    result["status"] = (
        "passed"
        if len(result["items"]) == len(indices)
        and all(item["status"] == "passed" for item in result["items"])
        else "failed"
    )
    return result


def _worker(url: str, output_dir: str, ffprobe: str, connection) -> None:
    if hasattr(os, "setsid"):
        os.setsid()
    # Browser and native-library logs can contain signed URLs; suppress them.
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    result = {"status": "failed", "stage": "discover", "items": []}

    def emit(message):
        nonlocal result
        if "snapshot" in message:
            result = message["snapshot"]
        connection.send(message)

    try:
        engine = MediaDownloader(
            DownloaderConfig(cookie_browser="chrome", allow_cookie_fallback=False)
        )
        with anonymous_profile_adapter():
            result = exercise_profile(
                engine, profile_url(url), output_dir, ffprobe, emit
            )
    except Exception as exc:
        result.update(status="failed", exception_chain=_safe_errors(exc))
    connection.send({"result": result})
    connection.close()


def run_profile(url: str, ffprobe: str, timeout: int) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    started = time.monotonic()
    snapshot: dict[str, Any] = {"stage": "starting", "items": []}
    result = None
    last_progress = None
    with tempfile.TemporaryDirectory(prefix="douyin-profile-smoke-") as directory:
        process = context.Process(
            target=_worker, args=(url, directory, ffprobe, sender)
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
                    if "snapshot" in message:
                        snapshot = message["snapshot"]
                        progress = (snapshot["stage"], len(snapshot["items"]))
                    else:
                        progress = (
                            message["stage"],
                            message.get("ordinal"),
                            message.get("event"),
                        )
                    if progress != last_progress:
                        print(f"Profile smoke: {progress}", flush=True)
                        last_progress = progress
                elif not process.is_alive():
                    break
        finally:
            _stop_worker(process)
            receiver.close()
    elapsed = round(time.monotonic() - started, 3)
    if result is None:
        result = snapshot
        result.update(
            status="failed",
            exception_chain=[
                {
                    "type": (
                        "SmokeDeadlineExceeded"
                        if elapsed >= timeout
                        else "SmokeWorkerExited"
                    ),
                    "message": "The worker did not finish; completed diagnostic results were retained",
                }
            ],
        )
    result["elapsed_seconds"] = elapsed
    return result


def main(argv: Sequence[str] | None = None) -> int:
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args(argv)
    if not 30 <= args.timeout <= 900:
        parser.error("Use a 30-900 second total timeout")
    report: dict[str, Any] = {
        "description": "Anonymous full-profile discovery, then at most five downloads near the 80% list position using unchanged profile-discovered items. No user cookies. Temporary media is removed; only sanitized diagnostic results are retained. This does not download the full profile or reproduce elapsed time from earlier downloads.",
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
        url = profile_url(args.url)
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            raise RuntimeError("FFprobe was not found on PATH")
        report.update(run_profile(url, ffprobe, args.timeout))
    except Exception as exc:
        report.update(status="failed", exception_chain=_safe_errors(exc))
    save()
    print(f"Anonymous profile smoke: {report['status']}", flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
