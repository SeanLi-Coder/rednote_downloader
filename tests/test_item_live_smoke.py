from __future__ import annotations

import json
from http.cookiejar import Cookie, CookieJar
from types import SimpleNamespace

import pytest

from app import douyin_signing
from app.downloader import DownloaderConfig, MediaDownloader
from scripts import douyin_item_live_smoke as smoke


@pytest.mark.parametrize("media_id", ["7684132989608949594", "7677863372330774922"])
def test_smoke_normalizes_user_modal_to_exact_single_item(media_id):
    url = f"https://www.douyin.com/user/self?modal_id={media_id}&showTab=favorite_collection"
    assert smoke.target_url(url) == (
        f"https://www.douyin.com/video/{media_id}",
        media_id,
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://www.douyin.com/user/self",
        "https://www.douyin.com/user/self?modal_id=1&vid=2",
        "https://www.youtube.com/watch?v=123",
    ],
)
def test_smoke_rejects_non_item_or_ambiguous_targets(url):
    with pytest.raises(ValueError):
        smoke.target_url(url)


def test_ssr_shape_keeps_structure_without_payload_secrets():
    wrapper = {
        "awemeId": "7684132989608949594",
        "statusCode": 0,
        "redirect": "https://secret.test/?token=secret",
        "isSpider": False,
        "aweme": {
            "statusCode": 0,
            "isUnknownAweme": False,
            "detail": {
                "awemeId": "7684132989608949594",
                "groupId": "7677863372330774922",
                "desc": "private caption",
                "awemeType": 0,
                "mediaType": "secret",
                "authorInfo": {"secUid": "secret author", "nickname": "private name"},
                "video": {"bitRateList": [{"playAddr": {"urlList": ["secret URL"]}}]},
            },
        },
    }
    shape = smoke.ssr_shape(wrapper)
    encoded = json.dumps(shape)
    assert "secret" not in encoded and "private" not in encoded
    assert shape["detail"]["groupId"] == "7677863372330774922"
    assert shape["author_has_sec_uid"] is True
    assert shape["bitrate_keys"] == [["playAddr"]]
    assert shape["wrapper"]["redirect"] == {"type": "str"}


def test_anonymous_adapter_preserves_validator_and_non_cookie_options(monkeypatch):
    marker = object()
    seen = []

    def validator(wrapper, aweme_id, expected_sec_uid):
        seen.append((wrapper, aweme_id, expected_sec_uid))
        return marker

    monkeypatch.setattr(douyin_signing, "_validated_ssr_wrapper_detail", validator)
    engine = MediaDownloader(DownloaderConfig(cookie_browser="chrome"))
    original = engine._douyin_ytdlp_options()
    wrapper = {"statusCode": 0}
    shapes = []
    with smoke.anonymous_browser_adapter(shapes):
        jar = douyin_signing._load_chrome_cookie_jar(None)
        assert list(jar) == []
        assert douyin_signing._cookie_jar_to_playwright(jar) == []
        with pytest.raises(RuntimeError, match="unexpected cookie jar"):
            douyin_signing._cookie_jar_to_playwright(CookieJar())
        assert engine._douyin_ytdlp_options() == {
            key: value for key, value in original.items() if key != "cookiesfrombrowser"
        }
        assert (
            douyin_signing._validated_ssr_wrapper_detail(wrapper, "123", None) is marker
        )
    assert seen == [(wrapper, "123", None)]
    assert len(shapes) == 1
    assert engine._douyin_ytdlp_options() == original


def test_exception_chain_redacts_signed_urls_and_cookie_values():
    try:
        try:
            raise ValueError(
                "media https://v.test/x?signature=TOPSECRET Cookie: sessionid=COOKIESECRET"
            )
        except ValueError as inner:
            raise RuntimeError("The production operation failed") from inner
    except RuntimeError as error:
        chain = smoke.exception_chain(error)
    text = json.dumps(chain)
    assert len(chain) == 2
    assert "TOPSECRET" not in text and "COOKIESECRET" not in text
    assert "https://" not in text


def test_anonymous_adapter_allows_only_its_own_server_issued_session(monkeypatch):
    def reject_user_cookie_access(*args, **kwargs):
        pytest.fail("The smoke must not read user Chrome cookies")

    monkeypatch.setattr(
        douyin_signing, "_load_chrome_cookie_jar", reject_user_cookie_access
    )
    with smoke.anonymous_browser_adapter([]):
        jar = douyin_signing._load_chrome_cookie_jar(None)
        jar.set_cookie(
            Cookie(
                0,
                "ttwid",
                "synthetic-server-session",
                None,
                False,
                ".douyin.com",
                True,
                True,
                "/",
                True,
                True,
                None,
                True,
                None,
                None,
                {},
            )
        )
        converted = douyin_signing._cookie_jar_to_playwright(jar)
        assert len(converted) == 1
        assert converted[0]["name"] == "ttwid"
        assert converted[0]["value"] == "synthetic-server-session"
        with pytest.raises(RuntimeError, match="unexpected cookie jar"):
            douyin_signing._cookie_jar_to_playwright(CookieJar())


def test_missing_ffprobe_writes_failed_report_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke.shutil, "which", lambda name: None)
    report = tmp_path / "report.json"
    assert (
        smoke.main(
            ["--url", "https://www.douyin.com/video/123", "--report", str(report)]
        )
        == 1
    )
    payload = json.loads(report.read_text())
    assert payload["status"] == "failed"
    assert payload["items"] == []


def test_quality_metrics_only_accepts_bounded_numeric_fields():
    assert smoke.quality_metrics(
        {
            "width": 1080,
            "height": "https://secret.test/?token=secret",
            "bit_rate": True,
            "filesize": 10**16,
            "author": "private author",
            "url": "private URL",
        }
    ) == {"width": 1080, "height": None, "bit_rate": None, "filesize": None}
    assert all(value is None for value in smoke.quality_metrics(None).values())


def test_quality_observer_records_dimensions_without_changing_results(monkeypatch):
    engine = MediaDownloader(DownloaderConfig(cookie_browser="chrome"))
    marker = object()
    original_probe_results = [
        {"width": 720, "height": 1280, "bit_rate": 800_000, "filesize": 1000},
        {"width": 1080, "height": 1920, "bit_rate": 1_500_000, "filesize": 2000},
    ]
    calls = []
    results = []
    unresolved_result = []

    def original_probe(ydl, url, **kwargs):
        calls.append((ydl, url, kwargs))
        return original_probe_results[len(calls) - 1]

    def original_video(ydl, info, **kwargs):
        for url in info["_douyin_direct_candidates"][0]["urls"]:
            results.append(
                engine._probe_douyin_ratio_with_retry(
                    ydl, url, ratio="author-feed-1", **kwargs
                )
            )
        assert (
            engine._unresolved_douyin_direct_failures([object()], results)
            is unresolved_result
        )
        return marker

    monkeypatch.setattr(engine, "_probe_douyin_ratio_with_retry", original_probe)
    monkeypatch.setattr(engine, "_add_douyin_probe_formats_scoped", original_video)
    monkeypatch.setattr(
        engine, "_unresolved_douyin_direct_failures", lambda *args: unresolved_result
    )
    info = {
        "_douyin_direct_candidates": [
            {
                "width": 1080,
                "height": 1920,
                "bit_rate": 1_600_000,
                "filesize": 2100,
                "urls": [
                    "https://secret.test/?token=ONE",
                    "https://secret.test/?token=TWO",
                ],
                "author": "private author",
                "headers": {"Cookie": "private cookie"},
            }
        ]
    }
    reports = []
    with smoke.observe_quality_probes(engine, reports):
        assert (
            engine._add_douyin_probe_formats_scoped(marker, info, callback=marker)
            is marker
        )
    assert engine._probe_douyin_ratio_with_retry is original_probe
    assert all(
        got is expected for got, expected in zip(results, original_probe_results)
    )
    assert calls[0] == (
        marker,
        info["_douyin_direct_candidates"][0]["urls"][0],
        {"ratio": "author-feed-1", "callback": marker},
    )
    assert reports[0]["kind"] == "video"
    assert reports[0]["status"] == "returned"
    assert reports[0]["unresolved_candidate_count"] == 0
    assert reports[0]["declared_candidates"] == [
        {"width": 1080, "height": 1920, "bit_rate": 1_600_000, "filesize": 2100}
    ]
    assert [probe["meets_declared_dimensions"] for probe in reports[0]["probes"]] == [
        False,
        True,
    ]
    assert [probe["endpoint_attempt"] for probe in reports[0]["probes"]] == [1, 2]
    encoded = json.dumps(reports)
    assert (
        "private" not in encoded
        and "secret" not in encoded
        and "https://" not in encoded
    )


def test_quality_observer_live_photo_rethrows_identical_exception(monkeypatch):
    engine = MediaDownloader(DownloaderConfig(cookie_browser="chrome"))
    error = ValueError("private response https://secret.test/?token=SECRET")

    def original_probe(*args, **kwargs):
        raise error

    def original_live_photo(ydl, asset, **kwargs):
        return engine._probe_douyin_ratio_with_retry(
            ydl, "private media URL", ratio="author-feed-1"
        )

    monkeypatch.setattr(engine, "_probe_douyin_ratio_with_retry", original_probe)
    monkeypatch.setattr(
        engine, "_select_highest_douyin_live_photo_asset_scoped", original_live_photo
    )
    asset = SimpleNamespace(quality_candidates=[{"width": 1440, "height": 2560}])
    reports = []
    with (
        smoke.observe_quality_probes(engine, reports),
        pytest.raises(ValueError) as caught,
    ):
        engine._select_highest_douyin_live_photo_asset_scoped(None, asset)
    assert caught.value is error
    assert reports[0]["kind"] == "live_photo"
    assert reports[0]["status"] == "raised"
    assert reports[0]["probes"][0]["status"] == "raised"
    assert "private" not in json.dumps(reports) and "SECRET" not in json.dumps(reports)


def test_quality_observer_redacts_unrecognized_labels_and_empty_response(monkeypatch):
    engine = MediaDownloader(DownloaderConfig(cookie_browser="chrome"))
    monkeypatch.setattr(
        engine, "_probe_douyin_ratio_with_retry", lambda *args, **kwargs: None
    )

    def original_video(ydl, info):
        return engine._probe_douyin_ratio_with_retry(
            ydl, "private URL", ratio="private response"
        )

    monkeypatch.setattr(engine, "_add_douyin_probe_formats_scoped", original_video)
    reports = []
    with smoke.observe_quality_probes(engine, reports):
        assert engine._add_douyin_probe_formats_scoped(None, {}) is None
    assert reports[0]["probes"] == [
        {
            "label": "other",
            "endpoint_attempt": 1,
            "status": "empty",
            "measured": {
                "width": None,
                "height": None,
                "bit_rate": None,
                "filesize": None,
            },
        }
    ]
    assert "private" not in json.dumps(reports)


def test_quality_observer_limits_report_size_without_skipping_calls(monkeypatch):
    engine = MediaDownloader(DownloaderConfig(cookie_browser="chrome"))
    calls = []

    def original_probe(*args, **kwargs):
        calls.append(None)
        return {"width": 1920, "height": 1080}

    def original_video(ydl, info):
        for _ in range(121):
            engine._probe_douyin_ratio_with_retry(ydl, "private URL", ratio="default")
        return False

    monkeypatch.setattr(engine, "_probe_douyin_ratio_with_retry", original_probe)
    monkeypatch.setattr(engine, "_add_douyin_probe_formats_scoped", original_video)
    reports = []
    with smoke.observe_quality_probes(engine, reports):
        for _ in range(33):
            assert engine._add_douyin_probe_formats_scoped(None, {}) is False
    assert len(calls) == 33 * 121
    assert len(reports) == 32
    assert all(len(report["probes"]) == 120 for report in reports)
