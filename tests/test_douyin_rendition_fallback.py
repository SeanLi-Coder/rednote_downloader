from __future__ import annotations

from typing import Any

import pytest
from yt_dlp.utils import DownloadCancelled

from app.downloader import DownloaderConfig, MediaDownloader
from app.errors import (
    DownloadCancelledError,
    MediaDownloadError,
    TemporaryAccessError,
)
from app.xiaohongshu import RemoteAsset


ITEM_ID = "7684132989608949594"
VIDEO_URI = "v0200fg10000renditionfixture"
SOURCE_URL = f"https://www.douyin.com/video/{ITEM_ID}"
FIRST_URL = "https://v26-web.douyinvod.com/first-rendition.mp4"
BACKUP_URL = "https://v11-web.douyinvod.com/backup-rendition.mp4"
VERIFIED_URL = "https://v26-web.douyinvod.com/verified-rendition.mp4"
FULL_HD = (1080, 1920)
LOW_HD = (720, 1280)


def _rendition(
    urls: list[str],
    *,
    dimensions: tuple[int, int] = FULL_HD,
    bit_rate: int | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "width": dimensions[0],
        "height": dimensions[1],
        "urls": urls,
    }
    if bit_rate is not None:
        result["bit_rate"] = bit_rate
    return result


class RenditionSelection:
    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        media_kind: str,
        candidates: list[dict[str, Any]],
        outcomes: dict[str, tuple[int, int] | Exception | None],
        *,
        default_dimensions: tuple[int, int] = FULL_HD,
    ) -> None:
        self.engine = MediaDownloader(DownloaderConfig(cookie_browser=None))
        self.media_kind = media_kind
        self.calls: list[tuple[str, str]] = []
        self.should_cancel = lambda: False
        self.info = {
            "id": ITEM_ID,
            "title": "Rendition fallback fixture",
            "duration": 2.0,
            "formats": [
                {
                    "format_id": "native-1080",
                    "url": (
                        "https://api-play.amemv.com/aweme/v1/play/"
                        f"?video_id={VIDEO_URI}&ratio=1080p"
                    ),
                    "width": 1080,
                    "height": 1920,
                    "vcodec": "h264",
                    "acodec": "aac",
                    "ext": "mp4",
                }
            ],
            "_douyin_direct_candidates": candidates,
            "_douyin_minimum_width": 1080,
            "_douyin_minimum_height": 1920,
        }
        self.asset = RemoteAsset(
            candidates=[url for candidate in candidates for url in candidate["urls"]],
            index=1,
            width=1080,
            height=1920,
            video_uri=VIDEO_URI,
            duration=2.0,
            quality_candidates=candidates,
        )

        def probe(ydl, url, *, ratio, **kwargs):
            self.calls.append((ratio, url))
            outcome = default_dimensions if ratio == "default" else outcomes[url]
            if isinstance(outcome, Exception):
                raise outcome
            if outcome is None:
                return None
            width, height = outcome
            full_hd = width * height >= 1080 * 1920
            return {
                "url": url,
                "source_url": url,
                "width": width,
                "height": height,
                "bit_rate": 2_000_000 if full_hd else 500_000,
                "filesize": 500_000 if full_hd else 125_000,
                "duration": 2.0,
                "vcodec": "h264",
                "acodec": "none" if media_kind == "live_photo" else "aac",
                "probe_prefix_size": 32,
                "probe_prefix_sha256": ("a" if full_hd else "b") * 64,
            }

        monkeypatch.setattr(self.engine, "_probe_douyin_ratio_with_retry", probe)

    def run(self) -> bool | RemoteAsset:
        if self.media_kind == "video":
            return self.engine._add_douyin_probe_formats(
                object(),
                self.info,
                expected_id=ITEM_ID,
                verification_url=SOURCE_URL,
                should_cancel=self.should_cancel,
            )
        return self.engine._select_highest_douyin_live_photo_asset(
            object(),
            self.asset,
            callback=None,
            should_cancel=self.should_cancel,
        )

    def assert_full_hd_selected(self) -> None:
        selected = self.run()
        if self.media_kind == "video":
            assert selected is True, self.info.get("_douyin_probe_failure")
            verified_formats = [
                value
                for value in self.info["formats"]
                if value["format_id"].startswith("douyin-api-")
            ]
            assert verified_formats
            assert (
                max(value["width"] * value["height"] for value in verified_formats)
                == 1080 * 1920
            )
        else:
            assert isinstance(selected, RemoteAsset)
            assert (selected.width, selected.height) == FULL_HD

    def assert_blocked(self) -> str:
        if self.media_kind == "live_photo":
            with pytest.raises(MediaDownloadError) as caught:
                self.run()
            return str(caught.value)
        assert self.run() is False
        assert not any(
            value["format_id"].startswith("douyin-api-")
            for value in self.info["formats"]
        )
        return self.info["_douyin_probe_failure"]


@pytest.fixture(params=["video", "live_photo"])
def media_kind(request) -> str:
    return request.param


def test_rendition_tries_backup_after_first_url_returns_lower_resolution(
    monkeypatch, media_kind
) -> None:
    selection = RenditionSelection(
        monkeypatch,
        media_kind,
        [_rendition([FIRST_URL, BACKUP_URL], bit_rate=2_000_000)],
        {FIRST_URL: LOW_HD, BACKUP_URL: FULL_HD},
        default_dimensions=LOW_HD,
    )

    selection.assert_full_hd_selected()

    assert selection.calls[:2] == [
        ("author-feed-1", FIRST_URL),
        ("author-feed-1", BACKUP_URL),
    ]


def test_fully_measured_lower_variant_without_bitrate_does_not_veto_full_hd(
    monkeypatch, media_kind
) -> None:
    selection = RenditionSelection(
        monkeypatch,
        media_kind,
        [
            _rendition([VERIFIED_URL], bit_rate=2_000_000),
            _rendition([FIRST_URL, BACKUP_URL]),
        ],
        {VERIFIED_URL: FULL_HD, FIRST_URL: LOW_HD, BACKUP_URL: LOW_HD},
    )

    selection.assert_full_hd_selected()

    assert ("author-feed-2", FIRST_URL) in selection.calls
    assert ("author-feed-2", BACKUP_URL) in selection.calls


@pytest.mark.parametrize("unverified_outcome", ["timeout", "unparseable"])
@pytest.mark.parametrize(
    "reverse_urls", [False, True], ids=["lower-first", "unknown-first"]
)
def test_partially_measured_variant_without_bitrate_remains_blocking(
    monkeypatch, media_kind, unverified_outcome, reverse_urls
) -> None:
    unresolved = (
        TimeoutError("rendition backup temporarily unavailable")
        if unverified_outcome == "timeout"
        else None
    )
    urls = [BACKUP_URL, FIRST_URL] if reverse_urls else [FIRST_URL, BACKUP_URL]
    selection = RenditionSelection(
        monkeypatch,
        media_kind,
        [
            _rendition([VERIFIED_URL], bit_rate=2_000_000),
            _rendition(urls),
        ],
        {VERIFIED_URL: FULL_HD, FIRST_URL: LOW_HD, BACKUP_URL: unresolved},
    )

    if unverified_outcome == "timeout":
        with pytest.raises(TemporaryAccessError, match="author-feed-2") as caught:
            selection.run()
        details = str(caught.value)
        assert "media request or FFprobe timed out" in details
    else:
        details = selection.assert_blocked()
        assert "author-feed-2" in details
        assert "could not be parsed" in details
    assert "below" not in details
    assert [url for ratio, url in selection.calls if ratio == "author-feed-2"] == urls


def test_declared_2k_variant_cannot_be_replaced_with_measured_full_hd(
    monkeypatch, media_kind
) -> None:
    selection = RenditionSelection(
        monkeypatch,
        media_kind,
        [
            _rendition([VERIFIED_URL], bit_rate=2_000_000),
            _rendition([FIRST_URL, BACKUP_URL], dimensions=(1440, 2560)),
        ],
        {VERIFIED_URL: FULL_HD, FIRST_URL: LOW_HD, BACKUP_URL: LOW_HD},
    )

    details = selection.assert_blocked()

    assert "author-feed-2" in details
    assert "1440x2560" in details


def test_higher_declared_bitrate_stays_blocking_after_lower_resolution_responses(
    monkeypatch, media_kind
) -> None:
    selection = RenditionSelection(
        monkeypatch,
        media_kind,
        [
            _rendition([VERIFIED_URL], bit_rate=2_000_000),
            _rendition([FIRST_URL, BACKUP_URL], bit_rate=5_000_000),
        ],
        {VERIFIED_URL: FULL_HD, FIRST_URL: LOW_HD, BACKUP_URL: LOW_HD},
    )

    assert "author-feed-2" in selection.assert_blocked()


def test_dimension_mismatch_diagnostic_includes_measured_and_declared_sizes(
    monkeypatch, media_kind
) -> None:
    selection = RenditionSelection(
        monkeypatch,
        media_kind,
        [
            _rendition([VERIFIED_URL], bit_rate=2_000_000),
            _rendition([FIRST_URL, BACKUP_URL], bit_rate=5_000_000),
        ],
        {VERIFIED_URL: FULL_HD, FIRST_URL: LOW_HD, BACKUP_URL: LOW_HD},
    )

    details = selection.assert_blocked()

    assert "720x1280" in details
    assert "1080x1920" in details


def test_cancellation_after_lower_probe_stops_before_backup_or_default(
    monkeypatch, media_kind
) -> None:
    selection = RenditionSelection(
        monkeypatch,
        media_kind,
        [_rendition([FIRST_URL, BACKUP_URL], bit_rate=2_000_000)],
        {FIRST_URL: LOW_HD, BACKUP_URL: FULL_HD},
    )
    selection.should_cancel = lambda: bool(selection.calls)
    error_type = DownloadCancelled if media_kind == "video" else DownloadCancelledError

    with pytest.raises(error_type):
        selection.run()

    assert selection.calls == [("author-feed-1", FIRST_URL)]


def test_lower_and_empty_dimensions_do_not_prove_every_mirror_is_lower(
    monkeypatch, media_kind
) -> None:
    selection = RenditionSelection(
        monkeypatch,
        media_kind,
        [
            _rendition([VERIFIED_URL], bit_rate=2_000_000),
            _rendition([FIRST_URL, BACKUP_URL]),
        ],
        {VERIFIED_URL: FULL_HD, FIRST_URL: LOW_HD, BACKUP_URL: (0, 0)},
    )

    assert "author-feed-2" in selection.assert_blocked()
    assert ("author-feed-2", BACKUP_URL) in selection.calls


@pytest.mark.parametrize("measured_dimensions", [(1080, 3840), (2000, 2000)])
def test_more_pixels_do_not_cover_a_short_or_long_edge_below_declaration(
    monkeypatch, media_kind, measured_dimensions
) -> None:
    selection = RenditionSelection(
        monkeypatch,
        media_kind,
        [
            _rendition(
                [VERIFIED_URL], dimensions=measured_dimensions, bit_rate=2_000_000
            ),
            _rendition([FIRST_URL, BACKUP_URL], dimensions=(1440, 2560)),
        ],
        {VERIFIED_URL: measured_dimensions, FIRST_URL: LOW_HD, BACKUP_URL: LOW_HD},
        default_dimensions=measured_dimensions,
    )

    details = selection.assert_blocked()

    assert "author-feed-2" in details
    assert "1440x2560" in details


def test_known_lower_mirror_is_excluded_from_selected_transfer_sources(
    monkeypatch, media_kind
) -> None:
    selection = RenditionSelection(
        monkeypatch,
        media_kind,
        [_rendition([FIRST_URL, BACKUP_URL], bit_rate=2_000_000)],
        {FIRST_URL: LOW_HD, BACKUP_URL: FULL_HD},
        default_dimensions=LOW_HD,
    )

    selected = selection.run()

    if media_kind == "live_photo":
        assert isinstance(selected, RemoteAsset)
        assert BACKUP_URL in selected.candidates
        assert FIRST_URL not in selected.candidates
    else:
        assert selected is True
        selected_formats = [
            value
            for value in selection.info["formats"]
            if value["format_id"].startswith("douyin-api-")
            and (value["width"], value["height"]) == FULL_HD
        ]
        assert selected_formats
        for verified_format in selected_formats:
            assert verified_format["_douyin_probe_source_url"] == BACKUP_URL
            assert BACKUP_URL in verified_format["_douyin_probe_source_urls"]
            assert FIRST_URL not in verified_format["_douyin_probe_source_urls"]
