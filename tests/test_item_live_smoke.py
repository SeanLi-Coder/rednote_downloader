from __future__ import annotations

import json
from http.cookiejar import CookieJar

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
