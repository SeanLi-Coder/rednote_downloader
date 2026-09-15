from __future__ import annotations

import contextlib
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


def test_smoke_normalizes_reported_mixed_profile_link_to_active_modal():
    url = (
        "https://www.douyin.com/user/MS4wLjABAAAAUbSbP1q7W3AILSzSn3AsSsvgm3vmw"
        "PTdsgPyJXwZPg6vl51ORWgOUYrQ4HLw6YWb?from_tab_name=main"
        "&modal_id=7650852719788025187&vid=7683316000586315369"
    )

    assert smoke.target_url(url) == (
        "https://www.douyin.com/video/7650852719788025187",
        "7650852719788025187",
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://www.douyin.com/user/self",
        "https://www.douyin.com/user/self?modal_id=1&modal_id=2&vid=2",
        "https://www.douyin.com/user/self?modal_id=1&vid=2&vid=3",
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


@pytest.mark.parametrize("enabled", [False, True])
def test_forced_signed_adapter_only_injects_exact_unprocessed_metadata_miss(
    monkeypatch, enabled
):
    calls = []
    marker = object()

    def original(self, url, *args, **kwargs):
        calls.append((url, args, kwargs))
        return marker

    monkeypatch.setattr(smoke.YoutubeDL, "extract_info", original)
    url = "https://www.douyin.com/video/7649744769275263409"
    ydl = object.__new__(smoke.YoutubeDL)
    with smoke.force_signed_detail_adapter(url, enabled=enabled) as state:
        if enabled:
            with pytest.raises(smoke.DownloadError, match="empty aweme detail"):
                ydl.extract_info(url, download=False, process=False)
            assert calls == []
        else:
            assert ydl.extract_info(url, download=False, process=False) is marker
        assert state["extract_count"] == int(enabled)
    assert smoke.YoutubeDL.extract_info is original
    assert ydl.extract_info(url, download=False, process=False) is marker


@pytest.mark.parametrize(
    ("url", "kwargs"),
    [
        ("https://www.douyin.com/video/999", {"download": False, "process": False}),
        (
            "https://www.douyin.com/user/self?modal_id=123",
            {"download": False, "process": False},
        ),
        (
            "https://www.douyin.com/video/123?other=1",
            {"download": False, "process": False},
        ),
        ("https://www.youtube.com/watch?v=123", {"download": False, "process": False}),
        ("https://www.douyin.com/video/123", {"download": True, "process": False}),
        ("https://www.douyin.com/video/123", {"download": False, "process": True}),
        ("https://www.douyin.com/video/123", {"download": False}),
        ("https://www.douyin.com/video/123", {"process": False}),
        ("https://www.douyin.com/video/123", {}),
    ],
)
def test_forced_signed_adapter_preserves_other_urls_and_processing(
    monkeypatch, url, kwargs
):
    calls = []
    marker = object()

    def original(self, source_url, *args, **options):
        calls.append((source_url, args, options))
        return marker

    monkeypatch.setattr(smoke.YoutubeDL, "extract_info", original)
    ydl = object.__new__(smoke.YoutubeDL)
    with smoke.force_signed_detail_adapter(
        "https://www.douyin.com/video/123", enabled=True
    ) as state:
        assert ydl.extract_info(url, **kwargs) is marker
        assert state["extract_count"] == 0
    assert calls == [(url, (), kwargs)]


def test_forced_signed_adapter_restores_after_error_and_accepts_positional_flags(
    monkeypatch,
):
    def original(*args, **kwargs):
        pytest.fail("An injected metadata request must not make a network request")

    monkeypatch.setattr(smoke.YoutubeDL, "extract_info", original)
    url = "https://www.douyin.com/video/123"
    ydl = object.__new__(smoke.YoutubeDL)
    with pytest.raises(smoke.DownloadError, match="empty aweme detail"):
        with smoke.force_signed_detail_adapter(url, enabled=True) as state:
            ydl.extract_info(url, False, None, None, False)
    assert state["extract_count"] == 1
    assert smoke.YoutubeDL.extract_info is original


def test_forced_signed_adapter_rejects_noncanonical_targets_before_patch():
    original = smoke.YoutubeDL.extract_info
    with pytest.raises(ValueError, match="canonical"):
        with smoke.force_signed_detail_adapter(
            "https://www.douyin.com/user/self?modal_id=123", enabled=True
        ):
            pytest.fail("The invalid target must not enter the adapter")
    assert smoke.YoutubeDL.extract_info is original


def _page_detail_request(media_id="123", *, response_url=None, payload=None):
    url = f"https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={media_id}"
    response = SimpleNamespace(
        url=response_url or url,
        status=200,
        body=lambda: json.dumps(
            payload
            if payload is not None
            else {
                "status_code": 0,
                "aweme_detail": {"aweme_id": media_id, "author": {"sec_uid": "owner"}},
            }
        ).encode(),
    )
    return SimpleNamespace(method="GET", url=url, response=lambda: response)


@pytest.mark.parametrize("enabled", [False, True])
def test_forced_ssr_only_suppresses_exact_target_after_real_capture_validation(enabled):
    original_capture = douyin_signing._PageDetailCapture.handle_request_finished
    original_ssr = douyin_signing._read_ssr_aweme_detail_from_page
    target_capture = douyin_signing._PageDetailCapture("123", "owner")
    other_capture = douyin_signing._PageDetailCapture("456", "owner")
    with smoke.force_ssr_detail_adapter(
        "https://www.douyin.com/video/123", enabled=enabled
    ) as state:
        target_capture.handle_request_finished(_page_detail_request("456"))
        assert target_capture.detail is None
        assert state["capture_suppressed_count"] == 0
        target_capture.handle_request_finished(_page_detail_request())
        assert (target_capture.detail is None) is enabled
        other_capture.handle_request_finished(_page_detail_request("456"))
        assert other_capture.detail["aweme_id"] == "456"
        assert state["capture_suppressed_count"] == int(enabled)
    assert douyin_signing._PageDetailCapture.handle_request_finished is original_capture
    assert douyin_signing._read_ssr_aweme_detail_from_page is original_ssr


@pytest.mark.parametrize(
    ("response_url", "payload", "field", "error_type"),
    [
        (
            "https://www.douyin.com/passport/web/login/",
            None,
            "authentication_failure",
            douyin_signing._AuthenticationSigningFailure,
        ),
        (
            None,
            {"status_code": 1, "status_msg": "Please complete CAPTCHA"},
            "authentication_failure",
            douyin_signing._AuthenticationSigningFailure,
        ),
        (
            "https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=999",
            None,
            "identity_failure",
            douyin_signing._IdentitySigningFailure,
        ),
        (
            None,
            {
                "status_code": 0,
                "aweme_detail": {
                    "aweme_id": "999",
                    "author": {"sec_uid": "owner"},
                },
            },
            "identity_failure",
            douyin_signing._IdentitySigningFailure,
        ),
        (
            "https://blocked.dnsfilter.com/?reason=blocked",
            None,
            "terminal_failure",
            douyin_signing._NetworkFilterSigningFailure,
        ),
    ],
)
def test_forced_ssr_preserves_real_capture_auth_identity_and_network_failures(
    response_url,
    payload,
    field,
    error_type,
):
    capture = douyin_signing._PageDetailCapture("123", "owner")
    with smoke.force_ssr_detail_adapter(
        "https://www.douyin.com/video/123", enabled=True
    ) as state:
        capture.handle_request_finished(
            _page_detail_request(response_url=response_url, payload=payload)
        )
        assert isinstance(getattr(capture, field), error_type)
        failure = getattr(capture, field)
        capture.handle_request_finished(_page_detail_request())
        assert getattr(capture, field) is failure
        assert capture.detail is None
        assert state["capture_suppressed_count"] == 0


@pytest.mark.parametrize("enabled", [False, True])
def test_forced_ssr_observes_actual_reader_results_without_replacing_them(
    monkeypatch,
    enabled,
):
    calls = []
    detail = {"aweme_id": "123", "author": {"sec_uid": "owner"}}

    def original(page, aweme_id, expected_sec_uid):
        calls.append((page, aweme_id, expected_sec_uid))
        return detail

    monkeypatch.setattr(douyin_signing, "_read_ssr_aweme_detail_from_page", original)
    marker = object()
    with smoke.force_ssr_detail_adapter(
        "https://www.douyin.com/video/123", enabled=enabled
    ) as state:
        assert (
            douyin_signing._read_ssr_aweme_detail_from_page(marker, "123", "owner")
            is detail
        )
        assert (
            douyin_signing._read_ssr_aweme_detail_from_page(marker, "456", "other")
            is detail
        )
        assert state["ssr_attempt_count"] == int(enabled)
        assert state["ssr_success_count"] == int(enabled)
    assert calls == [(marker, "123", "owner"), (marker, "456", "other")]
    assert douyin_signing._read_ssr_aweme_detail_from_page is original


@pytest.mark.parametrize("result", [None, {}, {"aweme_id": "999"}, {"aweme_id": 123}])
def test_forced_ssr_never_counts_missing_or_unbound_metadata(monkeypatch, result):
    monkeypatch.setattr(
        douyin_signing, "_read_ssr_aweme_detail_from_page", lambda *args: result
    )
    with smoke.force_ssr_detail_adapter(
        "https://www.douyin.com/video/123", enabled=True
    ) as state:
        assert (
            douyin_signing._read_ssr_aweme_detail_from_page(None, "123", None) is result
        )
        assert state["ssr_attempt_count"] == 1
        assert state["ssr_success_count"] == 0


def test_forced_ssr_rethrows_identical_failure_and_restores_adapters(monkeypatch):
    error = douyin_signing._IdentitySigningFailure(
        "Douyin SSR returned a different aweme"
    )

    def original(*args):
        raise error

    monkeypatch.setattr(douyin_signing, "_read_ssr_aweme_detail_from_page", original)
    original_capture = douyin_signing._PageDetailCapture.handle_request_finished
    with pytest.raises(douyin_signing._IdentitySigningFailure) as caught:
        with smoke.force_ssr_detail_adapter(
            "https://www.douyin.com/video/123", enabled=True
        ) as state:
            douyin_signing._read_ssr_aweme_detail_from_page(None, "123", None)
    assert caught.value is error
    assert state["ssr_attempt_count"] == 1 and state["ssr_success_count"] == 0
    assert douyin_signing._PageDetailCapture.handle_request_finished is original_capture
    assert douyin_signing._read_ssr_aweme_detail_from_page is original


def test_forced_ssr_rejects_noncanonical_targets_before_patching():
    original_capture = douyin_signing._PageDetailCapture.handle_request_finished
    original_ssr = douyin_signing._read_ssr_aweme_detail_from_page
    with pytest.raises(ValueError, match="canonical"):
        with smoke.force_ssr_detail_adapter(
            "https://www.douyin.com/user/self?modal_id=123", enabled=True
        ):
            pytest.fail("The invalid target must not enter the adapter")
    assert douyin_signing._PageDetailCapture.handle_request_finished is original_capture
    assert douyin_signing._read_ssr_aweme_detail_from_page is original_ssr


@pytest.mark.parametrize(
    ("enabled", "ssr_enabled"),
    [(False, False), (True, False), (False, True), (True, True)],
)
def test_item_cli_passes_diagnostic_flag_and_labels_report(
    monkeypatch, tmp_path, enabled, ssr_enabled
):
    calls = []

    def run_item(
        url,
        media_id,
        ffprobe,
        timeout,
        *,
        force_signed_detail=False,
        force_ssr_detail=False,
    ):
        calls.append(
            (url, media_id, ffprobe, timeout, force_signed_detail, force_ssr_detail)
        )
        return {"status": "passed", "forced_signed_detail": force_signed_detail}

    monkeypatch.setattr(smoke.shutil, "which", lambda name: "ffprobe")
    monkeypatch.setattr(smoke, "run_item", run_item)
    path = tmp_path / "report.json"
    args = [
        "--url",
        "https://www.douyin.com/user/self?modal_id=123",
        "--report",
        str(path),
    ]
    if enabled:
        args.append("--force-signed-detail")
    if ssr_enabled:
        args.append("--force-ssr-detail")
    assert smoke.main(args) == 0
    assert calls == [
        (
            "https://www.douyin.com/video/123",
            "123",
            "ffprobe",
            600,
            enabled or ssr_enabled,
            ssr_enabled,
        )
    ]
    report = json.loads(path.read_text())
    assert report["forced_signed_detail"] is (enabled or ssr_enabled)
    assert report["forced_ssr_detail"] is ssr_enabled
    assert ("not evidence" in report["description"]) is (enabled or ssr_enabled)
    assert (
        "requires actual verified SSR metadata" in report["description"]
    ) is ssr_enabled


@pytest.mark.parametrize(
    ("enabled", "ssr_enabled"), [(False, False), (True, False), (False, True)]
)
def test_run_item_passes_forcing_flag_to_worker_and_preserves_cleanup(
    monkeypatch, enabled, ssr_enabled
):
    created = []
    closed = []
    stopped = []
    receiver = SimpleNamespace(
        poll=lambda timeout: True,
        recv=lambda: {
            "result": {
                "status": "passed",
                "forced_signed_detail": enabled or ssr_enabled,
                "forced_ssr_detail": ssr_enabled,
            }
        },
        close=lambda: closed.append("receiver"),
    )
    sender = SimpleNamespace(close=lambda: closed.append("sender"))
    process = SimpleNamespace(start=lambda: None)

    def create_process(*, target, args):
        assert target is smoke._worker
        created.append(args)
        return process

    context = SimpleNamespace(
        Pipe=lambda duplex: (receiver, sender), Process=create_process
    )
    monkeypatch.setattr(smoke.multiprocessing, "get_context", lambda kind: context)
    monkeypatch.setattr(smoke, "_stop_worker", lambda value: stopped.append(value))
    result = smoke.run_item(
        "https://www.douyin.com/video/123",
        "123",
        "ffprobe",
        30,
        force_signed_detail=enabled,
        force_ssr_detail=ssr_enabled,
    )
    assert result["status"] == "passed"
    assert result["forced_signed_detail"] is (enabled or ssr_enabled)
    assert result["forced_ssr_detail"] is ssr_enabled
    assert created[0][:2] == ("https://www.douyin.com/video/123", "123")
    assert created[0][3:] == ("ffprobe", sender, enabled or ssr_enabled, ssr_enabled)
    assert not smoke.Path(created[0][2]).exists()
    assert stopped == [process]
    assert closed == ["sender", "receiver"]


@pytest.mark.parametrize("flag", ["--force-signed-detail", "--force-ssr-detail"])
def test_forced_signed_cli_rejects_page_observer_combination(tmp_path, flag):
    with pytest.raises(SystemExit) as error:
        smoke.main(
            [
                "--observe-profile-item-url",
                "https://www.douyin.com/user/self?modal_id=123&vid=456",
                flag,
                "--report",
                str(tmp_path / "report.json"),
            ]
        )
    assert error.value.code == 2


@pytest.mark.parametrize(
    ("enabled", "call_extractor", "expected_status"),
    [(False, False, "passed"), (True, False, "failed"), (True, True, "passed")],
)
def test_worker_requires_forced_extraction_before_reporting_passed(
    monkeypatch, tmp_path, enabled, call_extractor, expected_status
):
    url = "https://www.douyin.com/video/123"
    results = []
    item = SimpleNamespace(
        media_id="123", source_url=url, media_type=smoke.MediaType.VIDEO
    )

    class Engine:
        def __init__(self, *args, **kwargs):
            pass

        def discover(self, source_url, platform, kind):
            assert source_url == url
            if call_extractor:
                ydl = object.__new__(smoke.YoutubeDL)
                with pytest.raises(smoke.DownloadError, match="empty aweme detail"):
                    ydl.extract_info(url, download=False, process=False)
            return SimpleNamespace(
                discovery_complete=True, items=[item], cookie_fallback_used=False
            )

        def download_item(self, *args, **kwargs):
            return SimpleNamespace(
                cookie_fallback_used=False, output_paths=["video.mp4"]
            )

    monkeypatch.setattr(smoke.os, "dup2", lambda *args: None)
    if hasattr(smoke.os, "setsid"):
        monkeypatch.setattr(smoke.os, "setsid", lambda: None)
    monkeypatch.setattr(smoke, "MediaDownloader", Engine)
    monkeypatch.setattr(
        smoke, "anonymous_browser_adapter", lambda shapes: contextlib.nullcontext()
    )
    monkeypatch.setattr(
        smoke, "observe_quality_probes", lambda *args: contextlib.nullcontext()
    )
    monkeypatch.setattr(smoke, "inspect_media", lambda *args: {"type": "video"})
    connection = SimpleNamespace(send=results.append, close=lambda: None)
    smoke._worker(url, "123", str(tmp_path), "ffprobe", connection, enabled)
    result = results[-1]["result"]
    assert result["status"] == expected_status
    assert result["forced_signed_detail"] is enabled
    assert result["forced_extract_count"] == int(call_extractor)
    if expected_status == "failed":
        assert "did not exercise" in result["exception_chain"][0]["message"]


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


@pytest.mark.parametrize("ssr_result", [None, {"aweme_id": "123"}])
def test_worker_requires_real_ssr_result_even_if_signed_api_discovery_succeeds(
    monkeypatch,
    tmp_path,
    ssr_result,
):
    url = "https://www.douyin.com/video/123"
    results = []
    downloads = []
    item = SimpleNamespace(
        media_id="123", source_url=url, media_type=smoke.MediaType.VIDEO
    )

    class Engine:
        def __init__(self, *args, **kwargs):
            pass

        def discover(self, source_url, platform, kind):
            ydl = object.__new__(smoke.YoutubeDL)
            with pytest.raises(smoke.DownloadError, match="empty aweme detail"):
                ydl.extract_info(url, download=False, process=False)
            douyin_signing._read_ssr_aweme_detail_from_page(None, "123", "owner")
            # Simulate a subsequent signed API success when SSR returns no detail.
            return SimpleNamespace(
                discovery_complete=True, items=[item], cookie_fallback_used=False
            )

        def download_item(self, *args, **kwargs):
            downloads.append(item)
            return SimpleNamespace(
                cookie_fallback_used=False, output_paths=["video.mp4"]
            )

    monkeypatch.setattr(smoke.os, "dup2", lambda *args: None)
    if hasattr(smoke.os, "setsid"):
        monkeypatch.setattr(smoke.os, "setsid", lambda: None)
    monkeypatch.setattr(smoke, "MediaDownloader", Engine)
    monkeypatch.setattr(
        smoke, "anonymous_browser_adapter", lambda *args: contextlib.nullcontext()
    )
    monkeypatch.setattr(
        smoke, "observe_quality_probes", lambda *args: contextlib.nullcontext()
    )
    monkeypatch.setattr(smoke, "inspect_media", lambda *args: {"type": "video"})
    monkeypatch.setattr(
        douyin_signing, "_read_ssr_aweme_detail_from_page", lambda *args: ssr_result
    )
    connection = SimpleNamespace(send=results.append, close=lambda: None)
    smoke._worker(url, "123", str(tmp_path), "ffprobe", connection, False, True)
    result = results[-1]["result"]
    success = ssr_result is not None
    assert result["status"] == ("passed" if success else "failed")
    assert result["forced_signed_detail"] is True
    assert result["forced_ssr_detail"] is True
    assert result["forced_extract_count"] == 1
    assert result["forced_ssr_attempt_count"] == 1
    assert result["forced_ssr_success_count"] == int(success)
    assert len(downloads) == int(success)
    if not success:
        assert (
            "verified exact-item SSR metadata"
            in result["exception_chain"][0]["message"]
        )


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
    ) == {
        "width": 1080,
        "height": None,
        "bit_rate": None,
        "filesize": None,
        "duration_seconds": None,
    }
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
        {
            "width": 1080,
            "height": 1920,
            "bit_rate": 1_600_000,
            "filesize": 2100,
            "duration_seconds": None,
        }
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
            "expected_duration_seconds": None,
            "endpoint_attempt": 1,
            "status": "empty",
            "measured": {
                "width": None,
                "height": None,
                "bit_rate": None,
                "filesize": None,
                "duration_seconds": None,
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


@pytest.mark.parametrize(
    "value", [True, "NaN", float("inf"), -1, 604_801, "private media URL", {}]
)
def test_quality_duration_rejects_invalid_or_unbounded_values(value):
    assert smoke.quality_duration(value) is None


def test_quality_metrics_preserves_ffprobe_numeric_string_duration():
    assert smoke.quality_metrics({"duration": "12.000000"})["duration_seconds"] == 12.0


PROFILE_OBSERVER_URL = "https://www.douyin.com/user/MS4wExample?from_tab_name=main&modal_id=7650852719788025187&vid=7683316000586315369"


def test_profile_observer_preserves_conflicting_ids_without_using_production_parser():
    assert smoke.profile_observer_target(PROFILE_OBSERVER_URL) == {
        "modal_id": "7650852719788025187",
        "vid": "7683316000586315369",
    }
    assert smoke.observed_page_route(PROFILE_OBSERVER_URL) == {
        "path": "/user/<profile>",
        "numeric_query_ids": {
            "modal_id": ["7650852719788025187"],
            "vid": ["7683316000586315369"],
        },
    }


@pytest.mark.parametrize(
    "url",
    [
        PROFILE_OBSERVER_URL.replace("https://", "http://"),
        PROFILE_OBSERVER_URL.replace("www.douyin.com", "www.douyin.com.evil.test"),
        PROFILE_OBSERVER_URL.replace("www.douyin.com", "user@www.douyin.com"),
        PROFILE_OBSERVER_URL.replace("www.douyin.com", "www.douyin.com:444"),
        PROFILE_OBSERVER_URL + "&modal_id=123",
        PROFILE_OBSERVER_URL + "&signature=secret",
        PROFILE_OBSERVER_URL + "#private",
        "https://www.douyin.com/user/self?modal_id=123&vid=private",
        "https://www.douyin.com/user/self?modal_id=123",
    ],
)
def test_profile_observer_rejects_unsafe_or_ambiguous_observation_input(url):
    with pytest.raises(ValueError):
        smoke.profile_observer_target(url)


def test_profile_observer_records_only_detail_request_id_not_recommendations():
    assert (
        smoke.observed_detail_request_id(
            "https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=123&signature=private"
        )
        == "123"
    )
    for value in (
        "https://www.douyin.com/aweme/v1/web/aweme/post/?aweme_id=123",
        "https://evil.test/aweme/v1/web/aweme/detail/?aweme_id=123",
        "https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=123&aweme_id=456",
        "https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=private",
    ):
        assert smoke.observed_detail_request_id(value) is None


@pytest.mark.parametrize("resource_type", ["media", "image", "font", "websocket"])
def test_profile_observer_never_allows_media_resource_types(resource_type):
    assert not smoke.observer_request_allowed(resource_type, PROFILE_OBSERVER_URL)


def test_profile_observer_blocks_media_fetches_and_preserves_official_page_requests():
    assert smoke.observer_request_allowed("document", PROFILE_OBSERVER_URL)
    assert smoke.observer_request_allowed("script", "https://static.douyin.com/app.js")
    assert smoke.observer_request_allowed(
        "fetch", "https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=123"
    )
    assert not smoke.observer_request_allowed(
        "fetch", "https://media.example.test/video.mp4"
    )
    assert not smoke.observer_request_allowed(
        "fetch", "https://www.douyin.com/aweme/v1/web/play/?video_id=123"
    )
    assert not smoke.observer_request_allowed("document", "https://evil.test/private")


def test_profile_observer_sanitizes_browser_result_again():
    result = smoke.sanitized_page_ids(
        {
            "video_node_count": 2,
            "video_node_ids": [
                {
                    "source": "ancestor",
                    "attribute": "data-e2e-vid",
                    "id": "123",
                    "cookie": "private",
                },
                {"source": "video", "attribute": "private", "id": "456"},
            ],
            "script_state_ids": [
                {
                    "source": "_ROUTER_DATA",
                    "field": "modal_id",
                    "id": "123",
                    "url": "private",
                },
                {"source": "__INITIAL_STATE__", "field": "vid", "id": "private"},
            ],
            "html": "private",
        }
    )
    assert result["video_node_ids"] == [
        {"source": "ancestor", "attribute": "data-e2e-vid", "id": "123"}
    ]
    assert result["script_state_ids"] == [
        {"source": "_ROUTER_DATA", "field": "modal_id", "id": "123"}
    ]
    assert "private" not in json.dumps(result)


def test_profile_observer_cli_is_independent_of_downloads_and_ffprobe(
    tmp_path, monkeypatch
):
    calls = []

    def observer(url):
        calls.append(url)
        return {
            "status": "observed",
            "expected_ids": smoke.profile_observer_target(url),
        }

    monkeypatch.setattr(smoke, "run_profile_observer", observer)
    monkeypatch.setattr(
        smoke.shutil,
        "which",
        lambda name: pytest.fail("The observer must not require FFprobe"),
    )
    report = tmp_path / "observation.json"
    assert (
        smoke.main(
            [
                "--observe-profile-item-url",
                PROFILE_OBSERVER_URL,
                "--report",
                str(report),
            ]
        )
        == 0
    )
    assert calls == [PROFILE_OBSERVER_URL]
    assert json.loads(report.read_text())["status"] == "observed"
    assert "MS4wExample" not in report.read_text()


def test_profile_observer_invalid_input_writes_only_sanitized_failure(tmp_path):
    report = tmp_path / "observation.json"
    assert (
        smoke.main(
            [
                "--observe-profile-item-url",
                "https://private.test/?cookie=secret",
                "--report",
                str(report),
            ]
        )
        == 1
    )
    payload = json.loads(report.read_text())
    assert payload["status"] == "failed"
    assert payload["error_type"] == "ValueError"
    assert "private" not in report.read_text() and "secret" not in report.read_text()
