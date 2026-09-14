from __future__ import annotations

import contextlib
import copy
import json
from http.cookiejar import CookieJar
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import douyin, douyin_signing
from app.downloader import DiscoveryResult, DownloadOutcome, TemporaryAccessError
from app.errors import SiteIssueCode
from app.models import DownloadItem, MediaType, Platform, SourceKind
from scripts import douyin_profile_live_smoke as smoke

PROFILE = "https://www.douyin.com/user/MS4wLjABAAAAUbSbP1q7W3AILSzSn3AsSsvgm3vmwPTdsgPyJXwZPg6vl51ORWgOUYrQ4HLw6YWb"


def test_profile_target_accepts_only_public_profile_and_removes_query():
    assert smoke.profile_url(PROFILE + "?from_tab_name=main&token=SECRET") == PROFILE


@pytest.mark.parametrize(
    "url",
    [
        "https://www.douyin.com/user/self",
        PROFILE + "?modal_id=123",
        "https://www.douyin.com/video/123",
        "https://www.youtube.com/@example",
        "https://v.douyin.com/example/",
        "https://www.douyin.com/user/private-author",
    ],
)
def test_profile_target_rejects_non_profile_urls(url):
    with pytest.raises(ValueError):
        smoke.profile_url(url)


def test_anonymous_profile_adapter_covers_signed_and_browser_fallback_cookie_paths(
    monkeypatch,
):
    def reject_user_cookie_access(*args, **kwargs):
        pytest.fail("The profile smoke must never access user Chrome cookies")

    for module, name in (
        (douyin_signing, "_load_chrome_cookie_jar"),
        (douyin, "_extract_cookies"),
        (douyin, "extract_cookies_from_browser"),
    ):
        monkeypatch.setattr(module, name, reject_user_cookie_access)
    with smoke.anonymous_profile_adapter():
        jar = douyin_signing._load_chrome_cookie_jar(None)
        assert list(jar) == []
        assert douyin._extract_cookies(None) is jar
        assert douyin._cookie_jar_to_playwright(jar) == []
        with pytest.raises(RuntimeError, match="unexpected cookie jar"):
            douyin._cookie_jar_to_playwright(CookieJar())


def test_profile_discovery_fallback_does_not_call_real_cookie_loader(monkeypatch):
    import playwright.sync_api
    from app.errors import DiscoveryError

    marker = RuntimeError("Reached anonymous browser launch after cookie conversion")
    calls = []

    def fail_signed(*args, **kwargs):
        calls.append("signed")
        raise DiscoveryError("Signed discovery returned no complete profile")

    def launch_marker():
        calls.append("browser")
        raise marker

    def reject_user_cookie_access(*args, **kwargs):
        pytest.fail("The actual browser cookie extractor must not run")

    monkeypatch.setattr(douyin, "fetch_signed_profile_awemes", fail_signed)
    monkeypatch.setattr(
        douyin, "extract_cookies_from_browser", reject_user_cookie_access
    )
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", launch_marker)
    with smoke.anonymous_profile_adapter(), pytest.raises(RuntimeError) as caught:
        douyin.discover_profile(
            PROFILE, use_browser_cookies=True, allow_cookie_fallback=False
        )
    assert caught.value is marker
    assert calls == ["signed", "browser"]


@pytest.mark.parametrize(
    ("total", "expected"),
    [
        (0, []),
        (1, [0]),
        (3, [0, 1, 2]),
        (5, [0, 1, 2, 3, 4]),
        (10, [5, 6, 7, 8, 9]),
        (100, [77, 78, 79, 80, 81]),
    ],
)
def test_profile_sampling_is_bounded_and_centered_at_eighty_percent(total, expected):
    assert smoke.sample_indices(total) == expected


def fake_items(count):
    return [
        DownloadItem(
            id=str(index + 1),
            media_id=str(index + 1),
            source_url=f"https://www.douyin.com/video/{index + 1}",
            title="private title",
            author="private author",
            media_type=MediaType.VIDEO,
            metadata={
                "douyin_profile_media": {
                    "duration_ms": 8429,
                    "url": "https://media.test/?token=SECRET",
                }
            },
        )
        for index in range(count)
    ]


def setup_exercise(monkeypatch, items, download=None, complete=True):
    discovered = []
    downloaded = []
    snapshots = []

    def discover(*args):
        discovered.append(args)
        return DiscoveryResult("private author", items, discovery_complete=complete)

    def download_item(item, platform, output_dir, **kwargs):
        downloaded.append(item)
        if download:
            return download(item)
        return DownloadOutcome(
            output_paths=[str(Path(output_dir) / "private title.mp4")]
        )

    engine = SimpleNamespace(discover=discover, download_item=download_item)
    monkeypatch.setattr(
        smoke, "observe_quality_probes", lambda *args: contextlib.nullcontext()
    )
    monkeypatch.setattr(
        smoke,
        "inspect_media",
        lambda *args: {
            "type": "video",
            "width": 1080,
            "height": 1920,
            "sha256": "a" * 64,
        },
    )
    emit = lambda event: snapshots.append(copy.deepcopy(event))
    return engine, discovered, downloaded, snapshots, emit


def test_profile_smoke_passes_original_profile_items_and_metadata_to_download(
    monkeypatch, tmp_path
):
    items = fake_items(100)
    original_metadata = items[79].metadata
    engine, discovered, downloaded, snapshots, emit = setup_exercise(monkeypatch, items)
    result = smoke.exercise_profile(engine, PROFILE, str(tmp_path), "ffprobe", emit)
    assert discovered == [(PROFILE, Platform.DOUYIN, SourceKind.PROFILE)]
    assert len(downloaded) == 5
    assert all(
        actual is items[index]
        for actual, index in zip(downloaded, [77, 78, 79, 80, 81])
    )
    assert downloaded[2].metadata is original_metadata
    assert result["status"] == "passed"
    assert result["selected_ordinals"] == [78, 79, 80, 81, 82]
    assert result["items"][2]["target_duration_ms"] == 8429
    assert result["discovered_count"] == 100
    assert snapshots[-1]["snapshot"]["items"][-1]["status"] == "passed"
    encoded = json.dumps(result)
    assert not any(
        secret in encoded
        for secret in ("private title", "private author", "SECRET", "https://")
    )


def test_profile_smoke_preserves_sanitized_duration_error_and_checks_other_samples(
    monkeypatch, tmp_path
):
    def download(item):
        raise ValueError(
            "Media duration did not match target private title by private author https://media.test/?token=SECRET Cookie: sessionid=COOKIESECRET"
        )

    engine, _, downloaded, _, emit = setup_exercise(
        monkeypatch, fake_items(10), download
    )
    result = smoke.exercise_profile(engine, PROFILE, str(tmp_path), "ffprobe", emit)
    assert result["status"] == "failed"
    assert len(downloaded) == 5
    encoded = json.dumps(result)
    assert "Media duration did not match target" in encoded
    assert not any(
        secret in encoded
        for secret in (
            "private title",
            "private author",
            "SECRET",
            "COOKIESECRET",
            "https://",
        )
    )


def test_profile_smoke_stops_remaining_samples_when_rate_limited(monkeypatch, tmp_path):
    def download(item):
        raise TemporaryAccessError("HTTP 429", issue_code=SiteIssueCode.RATE_LIMITED)

    engine, _, downloaded, _, emit = setup_exercise(
        monkeypatch, fake_items(10), download
    )
    result = smoke.exercise_profile(engine, PROFILE, str(tmp_path), "ffprobe", emit)
    assert result["status"] == "failed"
    assert result["stopped_for_site_issue"] == "rate_limited"
    assert len(downloaded) == 1


def test_profile_smoke_does_not_claim_eighty_percent_from_incomplete_discovery(
    monkeypatch, tmp_path
):
    engine, _, downloaded, _, emit = setup_exercise(
        monkeypatch, fake_items(10), complete=False
    )
    with pytest.raises(RuntimeError, match="complete nonempty profile"):
        smoke.exercise_profile(engine, PROFILE, str(tmp_path), "ffprobe", emit)
    assert downloaded == []


def test_profile_smoke_rejects_cross_item_identity_before_download(
    monkeypatch, tmp_path
):
    items = fake_items(1)
    items[0].source_url = "https://www.douyin.com/video/999"
    engine, _, downloaded, _, emit = setup_exercise(monkeypatch, items)
    with pytest.raises(RuntimeError, match="different item identity"):
        smoke.exercise_profile(engine, PROFILE, str(tmp_path), "ffprobe", emit)
    assert downloaded == []


@pytest.mark.parametrize("value", [True, "8429", 0, -1, 10**50, float("inf")])
def test_profile_duration_reports_bounded_numeric_metadata_only(value):
    item = fake_items(1)[0]
    item.metadata["douyin_profile_media"]["duration_ms"] = value
    assert smoke.target_duration_ms(item) is None


def test_profile_timeout_preserves_partial_results_and_cleans_worker_and_media(
    monkeypatch,
):
    receiver = SimpleNamespace(
        poll=lambda timeout: True,
        recv=lambda: {
            "snapshot": {
                "stage": "download",
                "items": [{"media_id": "123", "status": "passed"}],
            }
        },
        close=lambda: closed.append("receiver"),
    )
    sender = SimpleNamespace(close=lambda: closed.append("sender"))
    closed = []
    created = []
    stopped = []
    process = SimpleNamespace(start=lambda: None)

    def create_process(*, target, args):
        created.append(args)
        assert target is smoke._worker
        return process

    context = SimpleNamespace(
        Pipe=lambda **kwargs: (receiver, sender), Process=create_process
    )
    times = iter([0, 1, 901, 901])
    monkeypatch.setattr(smoke.multiprocessing, "get_context", lambda value: context)
    monkeypatch.setattr(smoke.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(smoke, "_stop_worker", lambda value: stopped.append(value))
    result = smoke.run_profile(PROFILE, "ffprobe", 900)
    assert result["status"] == "failed"
    assert result["exception_chain"][0]["type"] == "SmokeDeadlineExceeded"
    assert result["items"] == [{"media_id": "123", "status": "passed"}]
    assert stopped == [process]
    assert set(closed) == {"receiver", "sender"}
    assert not Path(created[0][1]).exists()


def test_profile_smoke_missing_ffprobe_writes_failed_report_without_network(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(smoke.shutil, "which", lambda name: None)
    report = tmp_path / "report.json"
    assert smoke.main(["--url", PROFILE, "--report", str(report)]) == 1
    payload = json.loads(report.read_text())
    assert payload["status"] == "failed"
    assert payload["items"] == []
    assert PROFILE not in report.read_text()


@pytest.mark.parametrize("timeout", [0, 29, 901, 100000])
def test_profile_smoke_rejects_unbounded_timeouts(timeout, tmp_path):
    with pytest.raises(SystemExit):
        smoke.main(
            [
                "--url",
                PROFILE,
                "--report",
                str(tmp_path / "report.json"),
                "--timeout",
                str(timeout),
            ]
        )
