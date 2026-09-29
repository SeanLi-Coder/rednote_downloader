"""Offline Kuaishou integration tests; no user profiles or external network."""

from __future__ import annotations

import ast
import io
import json
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit, urlunsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import browser as chrome_browser
from app import downloader as engine
from app import kuaishou as ks
from app.downloader import KUAISHOU_SAVED_ASSETS_KEY, DownloadItem, EngineEvent
from app.errors import (
    AuthenticationRequiredError,
    DiscoveryError,
    DownloadCancelledError,
    MediaDownloadError,
    SiteIssueCode,
    TemporaryAccessError,
)
from app.models import (
    DownloadJob,
    ItemStatus,
    JobStatus,
    MediaType,
    Platform,
    SourceKind,
)
from app.platforms import UnsupportedUrlError, identify_url
from app.storage import JsonJobStore
from app.task_manager import DownloadManager, _public_kuaishou_saved_asset
from app.xiaohongshu import RemoteAsset, _XiaohongshuRedirectRejected

VIDEO = "https://www.kuaishou.com/short-video/3xvideo1"
PROFILE = "https://www.kuaishou.com/profile/3xowner1"
MEDIA = "https://v1.kwaicdn.com/upic/video.mp4"


def feed(media_id="3xvideo1", author="3xowner1", **photo_values):
    return {
        "photo": {
            "id": media_id,
            "caption": "Fixture video",
            "duration": 5000,
            "timestamp": 1_720_000_000_000,
            "photoUrl": MEDIA,
            **photo_values,
        },
        "author": {"id": author, "name": "Fixture Author"},
    }


def apollo(value=None, media_id="3xvideo1"):
    value = value or feed(media_id)
    return {
        "ROOT_QUERY": {
            f'visionVideoDetail({{"photoId":"{media_id}","page":"profile"}})': {
                "__ref": "detail"
            }
        },
        "detail": {
            "result": 1,
            "photo": {"__ref": "photo"},
            "author": {"type": "id", "id": "author"},
        },
        "photo": value["photo"],
        "author": value["author"],
    }


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        (VIDEO, "item"),
        (PROFILE, "profile"),
        ("https://v.kuaishou.com/AbCdEF", "short_link"),
        ("https://www.kuaishou.com/f/AbCdEF", "short_link"),
        ("https://m.gifshow.com/fw/photo/3xvideo1", "item"),
        ("https://m.gifshow.com/fw/user/3xowner1", "profile"),
        ("share text https://v.kuaishou.com/AbCdEF。", "short_link"),
    ],
)
def test_source_routes(url, kind):
    identified = identify_url(url)
    assert identified.platform == Platform.KUAISHOU
    assert identified.kind.value == kind


@pytest.mark.parametrize(
    "url",
    [
        "http://www.kuaishou.com/short-video/id",
        "https://www.kuaishou.com:444/short-video/id",
        "https://www.kuaishou.com.evil.test/short-video/id",
        "https://127.0.0.1/short-video/id",
        "https://user:secret@www.kuaishou.com/short-video/id",
        "https://www.kuaishou.com/short-video/../id",
        "https://www.kuaishou.com/short-video/a%2Fb",
        "https://www.kuaishou.com/profile/",
        "https://v.kuaishou.com/id/extra",
        "https://m.gifshow.com/other/id",
    ],
)
def test_reject_unsafe_source(url):
    with pytest.raises((UnsupportedUrlError, ValueError)):
        identify_url(url)


@pytest.mark.parametrize(
    "url", [MEDIA, "https://v1.kwaicdn.com:443/a", "https://v1.yximgs.com/a"]
)
def test_media_allowlist(url):
    assert ks.is_media_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://v1.kwaicdn.com/a",
        "https://v1.kwaicdn.com.evil.test/a",
        "https://127.0.0.1/a",
        "https://v1.kwaicdn.com:80/a",
        "https://a:b@v1.kwaicdn.com/a",
        "https://v1.kwaicdn.com./a",
        "https://v1.kwaicdn.com\\@evil.test/a",
        "https://v1.kwaicdn.com/a\nheader",
    ],
)
def test_media_blocklist(url):
    assert not ks.is_media_url(url)


@pytest.mark.parametrize(
    "host",
    [
        "p5-plat.wskwai.com",
        "p66-plat.wskwai.com",
        "p4-plat.wsbkwai.com",
    ],
)
def test_page_static_asset_hosts_are_routable_but_never_media_hosts(host):
    """The real profile page loads its JS/CSS from these hosts.

    Blocking them stops the page script from ever running, so the site never
    issues its author-feed request and discovery reports "no verified data" even
    though the author has works. They may only serve page subresources: widening
    the trusted MEDIA allowlist with them would let an unrelated host supply
    downloadable bytes.
    """
    assert host not in ks.MEDIA_DOMAINS
    assert not ks.is_media_url(f"https://{host}/a.js")
    assert any(
        host == root or host.endswith("." + root) for root in ks.BROWSER_DOMAINS
    )


def test_static_asset_domains_do_not_widen_page_or_media_allowlists():
    """Adding page asset hosts must not weaken navigation or media checks."""
    for host in ("www.wskwai.com", "evil.wsbkwai.com"):
        assert not ks.is_page_url(f"https://{host}/profile/3xowner1")
        assert not ks.is_media_url(f"https://{host}/upic/video.mp4")
    # Navigation stays bound to the real page hosts.
    assert ks.is_page_url("https://www.kuaishou.com/profile/3xowner1")
    # Media stays bound to the media CDN allowlist.
    assert ks.is_media_url(MEDIA)
    for domain in ks.STATIC_ASSET_DOMAINS:
        assert domain in ks.BROWSER_DOMAINS
        assert domain not in ks.MEDIA_DOMAINS
        assert domain not in ks.PAGE_HOSTS


@pytest.mark.parametrize(
    "host",
    [
        "wskwai.com.evil.test",
        "evilwskwai.com",
        "not-wsbkwai.com",
        "p5-plat.wskwai.com.evil.test",
    ],
)
def test_lookalike_static_asset_hosts_are_still_blocked(host):
    """Suffix matching must not admit a lookalike attacker domain."""
    assert not any(
        host == root or host.endswith("." + root) for root in ks.BROWSER_DOMAINS
    )
    assert not ks.is_media_url(f"https://{host}/a.js")


def test_exact_apollo_binding_and_references():
    state = apollo()
    state["ROOT_QUERY"]['visionVideoDetail({"photoId":"unrelated"})'] = feed(
        "unrelated"
    )
    values = ks.apollo_operations(state, "visionVideoDetail", "photoId", "3xvideo1")
    assert len(values) == 1
    assert ks.parse_video(values[0][1], expected_id="3xvideo1").media_id == "3xvideo1"
    assert ks.apollo_operations(state, "visionVideoDetail", "photoId", "missing") == []


def test_apollo_old_reference_layout_and_cycle():
    state = apollo()
    state['$ROOT_QUERY.visionVideoDetail({"photoId":"3xvideo1"})'] = state.pop("detail")
    state["ROOT_QUERY"] = {}
    state["author"]["parent"] = {"__ref": "author"}
    data = ks.apollo_operations(
        {"defaultClient": state}, "visionVideoDetail", "photoId", "3xvideo1"
    )[0][1]
    assert data["author"]["parent"] is None
    assert ks.parse_video(data).author_id == "3xowner1"


def test_parse_image_post_selects_highest_sized_variant():
    value = feed(
        photoUrl=[
            {"url": MEDIA + "?small", "width": 720, "height": 720},
            {"url": MEDIA + "?large", "width": 1440, "height": 1440},
        ],
        manifest=None,
        photoH265Url=None,
        photoUrls=None,
    )
    value["photo"].pop("photoUrl", None)
    value["photo"]["photoUrl"] = [
        {"url": MEDIA + "?small", "width": 720, "height": 720},
        {"url": MEDIA + "?large", "width": 1440, "height": 1440},
    ]
    parsed = ks.parse_video(value)
    assert parsed.media_type == "image"
    assert [(asset.width, asset.height) for asset in parsed.assets] == [(1440, 1440)]
    assert parsed.assets[0].candidates == [MEDIA + "?large"]


def test_image_post_without_declared_dimensions_is_not_claimed_highest_quality():
    value = feed(photoUrl=[{"url": MEDIA + "?unknown"}], manifest=None)
    value["photo"].pop("photoUrl", None)
    value["photo"]["photoUrl"] = [{"url": MEDIA + "?unknown"}]
    parsed = ks.parse_video(value)
    assert parsed.media_type == "image"
    assert not parsed.assets


def test_highest_video_size_rendition_is_selected_and_low_rendition_excluded():
    """R7: restored as a real test; these assertions previously never ran."""
    manifest = {
        "adaptationSet": [
            {
                "representation": [
                    {"id": "low", "url": MEDIA + "?low", "width": 640, "height": 360},
                    {
                        "id": "high",
                        "url": MEDIA + "?high",
                        "backupUrl": [MEDIA + "?backup"],
                        "width": 1920,
                        "height": 1080,
                    },
                ]
            }
        ]
    }
    video = ks.parse_video(feed(manifest=json.dumps(manifest)))
    assert video.media_type == "video"
    assert all((asset.width, asset.height) == (1920, 1080) for asset in video.assets)
    assert not any("?low" in url for asset in video.assets for url in asset.candidates)
    assert video.assets[0].candidates == [MEDIA + "?high", MEDIA + "?backup"]
    assert all(asset.duration == 5 for asset in video.assets)


def test_valid_video_with_object_photo_urls_stays_a_video():
    """R1: an object-list photoUrls must not turn a real video into an image."""
    manifest = {
        "adaptationSet": [
            {
                "representation": [
                    {
                        "id": "high",
                        "url": MEDIA + "?high",
                        "width": 1920,
                        "height": 1080,
                        "videoCodec": "avc",
                        "avgBitrate": 8000,
                    }
                ]
            }
        ]
    }
    parsed = ks.parse_video(
        feed(
            manifest=manifest,
            photoUrls=[{"url": MEDIA + "?still", "width": 800, "height": 600}],
        )
    )
    assert parsed.media_type == "video"
    assert [(asset.width, asset.height) for asset in parsed.assets] == [(1920, 1080)]
    assert parsed.assets[0].candidates == [MEDIA + "?high"]


def test_sized_cover_only_post_is_not_an_image_work():
    """R1: a declared duration with only a sized coverUrl must not become an image."""
    parsed = ks.parse_video(
        {
            "photo": {
                "id": "3xvideo1",
                "caption": "Cover only",
                "duration": 5000,
                "manifest": None,
                "coverUrl": [{"url": MEDIA + "?cover", "width": 1080, "height": 1920}],
            },
            "author": {"id": "3xowner1", "name": "Fixture Author"},
        }
    )
    assert parsed.media_type == "video"
    assert parsed.assets == []


def test_plain_string_photo_url_is_a_video_file_not_an_image():
    """R1: a bare string photoUrl is the legacy video file, never image evidence."""
    parsed = ks.parse_video(feed())
    assert parsed.media_type == "video"
    assert parsed.assets[0].candidates == [MEDIA]
    assert parsed.assets[0].format_id == "kuaishou-photoUrl"


def test_cover_cdn_backups_are_never_video_candidates():
    """Live shape: photoUrls holds cover backups ({cdn,url}, unsized), not video.

    On the real site every video work carries a manifest, and its photoUrls is a
    list of cover-image CDN backups with no declared dimensions. Treating those as
    video candidates would download a cover thumbnail as if it were the work, so a
    video whose manifest is missing must fail closed. Because a duration is
    declared, this is a VIDEO work with no usable media, never an image work.
    """
    parsed = ks.parse_video(
        {
            "photo": {
                "id": "3xvideo1",
                "caption": "Cover backups only",
                "duration": 5000,
                "manifest": None,
                "photoUrls": [
                    {"cdn": "ali", "url": MEDIA + "?cover-a"},
                    {"cdn": "tx", "url": MEDIA + "?cover-b"},
                ],
            },
            "author": {"id": "3xowner1", "name": "Fixture Author"},
        }
    )
    assert parsed.media_type == "video"
    assert parsed.assets == []


def test_unsized_cover_backups_without_duration_are_still_not_an_image():
    """A sizeless photoUrls entry never becomes an image, with or without duration."""
    parsed = ks.parse_video(
        {
            "photo": {
                "id": "3xvideo1",
                "caption": "Cover backups only",
                "width": 720,
                "height": 1280,
                "photoUrls": [
                    {"cdn": "ali", "url": MEDIA + "?cover-a"},
                    {"cdn": "tx", "url": MEDIA + "?cover-b"},
                ],
            },
            "author": {"id": "3xowner1", "name": "Fixture Author"},
        }
    )
    assert parsed.media_type == "video"
    assert parsed.assets == []


def test_speculative_cover_fields_are_not_read_as_video_candidates():
    """A field the site has never been observed to return must not be consulted."""
    parsed = ks.parse_video(
        feed(
            photoUrl=None,
            manifest=None,
            photoH265Url=MEDIA + "?h265-cover",
            photoH265Urls=[{"cdn": "ali", "url": MEDIA + "?h265-backup"}],
        )
    )
    assert parsed.media_type == "video"
    assert parsed.assets == []
    assert not any(
        "?h265" in url for asset in parsed.assets for url in asset.candidates
    )


def test_manifest_video_with_cover_backups_keeps_only_the_video_stream():
    """A real video work keeps its manifest renditions and ignores cover backups."""
    manifest = {
        "adaptationSet": [
            {
                "representation": [
                    {
                        "id": "high",
                        "url": MEDIA + "?high",
                        "width": 720,
                        "height": 1280,
                        "videoCodec": "avc",
                    }
                ]
            }
        ]
    }
    parsed = ks.parse_video(
        {
            "photo": {
                "id": "3xvideo1",
                "caption": "Real video shape",
                "duration": 5000,
                "width": 720,
                "height": 1280,
                "manifest": manifest,
                "photoUrls": [
                    {"cdn": "ali", "url": MEDIA + "?cover-a"},
                    {"cdn": "tx", "url": MEDIA + "?cover-b"},
                ],
                "photoH265Urls": [{"cdn": "ali", "url": MEDIA + "?h265-cover"}],
                "coverUrl": MEDIA + "?cover",
            },
            "author": {"id": "3xowner1", "name": "Fixture Author"},
        }
    )
    assert parsed.media_type == "video"
    assert len(parsed.assets) == 1
    assert parsed.assets[0].candidates == [MEDIA + "?high"]
    assert (parsed.assets[0].width, parsed.assets[0].height) == (720, 1280)
    assert not any(
        "cover" in url for asset in parsed.assets for url in asset.candidates
    )


def album_feed(media_id="3xalbum1", photo_urls=None):
    return {
        "photo": {
            "id": media_id,
            "caption": "Fixture album",
            # 2026-09-19 UTC, so date-prefixed names are deterministic.
            "timestamp": 1_789_819_200_000,
            "photoUrls": photo_urls,
        },
        "author": {"id": "3xowner1", "name": "Fixture Author"},
    }


def test_album_keeps_every_distinct_image_in_declared_order():
    """R2: a smaller album member must survive a global max-pixel filter."""
    parsed = ks.parse_video(
        album_feed(
            photo_urls=[
                {"url": MEDIA + "?first", "width": 800, "height": 600},
                {"url": MEDIA + "?second", "width": 1600, "height": 1200},
            ]
        )
    )
    assert parsed.media_type == "image"
    assert [(asset.index, asset.width, asset.height) for asset in parsed.assets] == [
        (1, 800, 600),
        (2, 1600, 1200),
    ]
    assert parsed.assets[0].candidates == [MEDIA + "?first"]
    assert parsed.assets[1].candidates == [MEDIA + "?second"]


def test_album_selects_highest_variant_within_each_image():
    """R2: variants of ONE image pick the highest; distinct images both survive."""
    parsed = ks.parse_video(
        album_feed(
            photo_urls=[
                [
                    {"url": MEDIA + "?a-low", "width": 400, "height": 300},
                    {"url": MEDIA + "?a-high", "width": 800, "height": 600},
                ],
                [{"url": MEDIA + "?b", "width": 1600, "height": 1200}],
            ]
        )
    )
    assert parsed.media_type == "image"
    assert [(asset.index, asset.width, asset.height) for asset in parsed.assets] == [
        (1, 800, 600),
        (2, 1600, 1200),
    ]
    assert parsed.assets[0].candidates == [MEDIA + "?a-high"]
    assert parsed.assets[1].candidates == [MEDIA + "?b"]


def test_album_duplicate_records_are_preserved_by_position():
    """R2: identity comes from position, not from guessing equal URLs."""
    parsed = ks.parse_video(
        album_feed(
            photo_urls=[
                {"url": MEDIA + "?same", "width": 800, "height": 600},
                {"url": MEDIA + "?same", "width": 800, "height": 600},
            ]
        )
    )
    assert parsed.media_type == "image"
    assert [asset.index for asset in parsed.assets] == [1, 2]
    assert [asset.format_id for asset in parsed.assets] == [
        "kuaishou-image-1",
        "kuaishou-image-2",
    ]


def test_album_member_without_dimensions_makes_album_unsupported():
    """R2: one unsized member cannot be dropped while the group claims complete."""
    parsed = ks.parse_video(
        album_feed(
            photo_urls=[
                {"url": MEDIA + "?first", "width": 800, "height": 600},
                {"url": MEDIA + "?unsized"},
            ]
        )
    )
    assert parsed.media_type == "image"
    assert parsed.assets == []


def test_album_cover_entry_is_not_image_evidence():
    """R1/R2: a cover thumbnail among images must not stand in for the work."""
    parsed = ks.parse_video(
        {
            "photo": {
                "id": "3xvideo1",
                "caption": "Cover only",
                "duration": 5000,
                "manifest": None,
                "photoUrl": None,
                "photoUrls": None,
                "coverUrl": [
                    {"url": MEDIA + "?cover", "width": 1080, "height": 1920},
                    {"url": MEDIA + "?cover-large", "width": 2160, "height": 3840},
                ],
            },
            "author": {"id": "3xowner1", "name": "Fixture Author"},
        }
    )
    assert parsed.media_type == "video"
    assert parsed.assets == []


def test_profile_with_unsized_album_member_is_not_complete():
    """R2: an album missing dimensions must not let the profile report complete."""
    collector = ks.ProfileCollector("3xowner1", PROFILE)
    collector.accept(
        {
            "feeds": [
                feed(),
                album_feed(
                    photo_urls=[
                        {"url": MEDIA + "?first", "width": 800, "height": 600},
                        {"url": MEDIA + "?unsized"},
                    ]
                ),
            ],
            "pcursor": "no_more",
        },
        owner_id="3xowner1",
        cursor="",
    )
    assert not collector.complete
    assert collector.unsupported
    assert list(collector.videos) == ["3xvideo1"]


def test_profile_with_verified_album_and_video_completes():
    """R2: a verified album and a normal video coexist and the page can complete."""
    collector = ks.ProfileCollector("3xowner1", PROFILE)
    collector.accept(
        {
            "feeds": [
                feed(),
                album_feed(
                    photo_urls=[
                        {"url": MEDIA + "?first", "width": 800, "height": 600},
                        {"url": MEDIA + "?second", "width": 1600, "height": 1200},
                    ]
                ),
            ],
            "pcursor": "no_more",
        },
        owner_id="3xowner1",
        cursor="",
    )
    assert collector.complete
    assert list(collector.videos) == ["3xvideo1", "3xalbum1"]
    assert collector.videos["3xalbum1"].media_type == "image"
    assert len(collector.videos["3xalbum1"].assets) == 2


def test_profile_problem_details_name_works_and_fixed_reasons():
    """2c: the user can see which works were skipped and why."""
    collector = ks.ProfileCollector("3xowner1", PROFILE)
    collector.accept(
        {
            "feeds": [
                feed(),
                feed("3xcoveronly", photoUrl=None, manifest=None),
                album_feed(
                    "3xunsized",
                    [{"url": MEDIA + "?a", "width": 800, "height": 600},
                     {"url": MEDIA + "?b"}],
                ),
            ],
            "pcursor": "no_more",
        },
        owner_id="3xowner1",
        cursor="",
    )
    assert not collector.complete
    assert collector.problem_count == 2
    assert [problem["position"] for problem in collector.problems] == [2, 3]
    assert [problem["media_id"] for problem in collector.problems] == [
        "3xcoveronly",
        "3xunsized",
    ]
    assert [problem["reason"] for problem in collector.problems] == [
        ks.PROFILE_PROBLEM_NO_MEDIA,
        ks.PROFILE_PROBLEM_UNSUPPORTED,
    ]

    summary = ks._profile_problem_summary(collector)
    assert "2 work(s) could not be verified" in summary
    assert ks.PROFILE_PROBLEM_NO_MEDIA in summary
    assert ks.PROFILE_PROBLEM_UNSUPPORTED in summary
    assert "#2:3xcoveronly" in summary and "#3:3xunsized" in summary


def test_profile_problem_summary_is_none_when_everything_verified():
    collector = ks.ProfileCollector("3xowner1", PROFILE)
    collector.accept(
        {"feeds": [feed()], "pcursor": "no_more"}, owner_id="3xowner1", cursor=""
    )
    assert collector.complete
    assert collector.problem_count == 0 and collector.problems == []
    assert ks._profile_problem_summary(collector) is None


def test_profile_problem_details_are_bounded(monkeypatch):
    """2c: a large profile must not inflate persisted state with unbounded detail."""
    monkeypatch.setattr(ks, "MAX_PROFILE_PROBLEM_DETAILS", 3)
    collector = ks.ProfileCollector("3xowner1", PROFILE)
    collector.accept(
        {
            "feeds": [
                feed(f"3xcover{i}", photoUrl=None, manifest=None) for i in range(7)
            ],
            "pcursor": "no_more",
        },
        owner_id="3xowner1",
        cursor="",
    )
    assert collector.problem_count == 7
    assert len(collector.problems) == 3
    assert collector.unsupported
    summary = ks._profile_problem_summary(collector)
    assert "7 work(s) could not be verified" in summary
    assert "A further 4 were only counted" in summary


def test_profile_problem_summary_lists_at_most_ten_works(monkeypatch):
    """2c: the affected-work list stays readable even with many skips."""
    monkeypatch.setattr(ks, "MAX_PROFILE_PROBLEM_DETAILS", 40)
    collector = ks.ProfileCollector("3xowner1", PROFILE)
    collector.accept(
        {
            "feeds": [
                feed(f"3xcover{i}", photoUrl=None, manifest=None) for i in range(15)
            ],
            "pcursor": "no_more",
        },
        owner_id="3xowner1",
        cursor="",
    )
    summary = ks._profile_problem_summary(collector)
    assert summary.count("#") == 10
    assert "(+5 more listed in task details)" in summary


def test_profile_problem_summary_never_echoes_site_text():
    """2c: captions, cookies and media URLs must not reach the warning text."""
    collector = ks.ProfileCollector("3xowner1", PROFILE)
    collector.accept(
        {
            "feeds": [
                {
                    "photo": {
                        "id": "3xcoveronly",
                        "caption": "私密内容 sessionid=cookie-secret",
                        "duration": 5000,
                        "manifest": None,
                        "photoUrl": None,
                        "coverUrl": MEDIA + "?signed=token-secret",
                    },
                    "author": {"id": "3xowner1", "name": "Fixture Author"},
                }
            ],
            "pcursor": "no_more",
        },
        owner_id="3xowner1",
        cursor="",
    )
    summary = ks._profile_problem_summary(collector)
    for secret in ("私密内容", "sessionid", "cookie-secret", "signed", "token-secret", MEDIA):
        assert secret not in summary


def test_queue_limit_is_reported_as_its_own_reason(monkeypatch):
    """2c: exceeding the protected queue limit is reported, never silently dropped."""
    monkeypatch.setattr(ks, "MAX_PROFILE_ITEMS", 1)
    collector = ks.ProfileCollector("3xowner1", PROFILE)
    assert collector.accept(
        {"feeds": [feed()], "pcursor": "next"}, owner_id="3xowner1", cursor=""
    )
    assert collector.problem_count == 0
    # A second page still returns an in-limit position, but the queue is full,
    # so the work cannot be queued and must be reported with its own reason.
    collector.accept(
        {"feeds": [feed("3xvideo2")], "pcursor": "no_more"},
        owner_id="3xowner1",
        cursor="next",
    )
    assert collector.problem_count == 1
    assert collector.problems[0]["reason"] == ks.PROFILE_PROBLEM_QUEUE_LIMIT
    assert collector.problems[0]["media_id"] == "3xvideo2"
    assert ks.PROFILE_PROBLEM_QUEUE_LIMIT in ks._profile_problem_summary(collector)
    assert not collector.complete


def test_items_beyond_the_page_limit_are_counted_not_dropped(monkeypatch):
    """2c: an oversized page must report its skipped items instead of hiding them."""
    monkeypatch.setattr(ks, "MAX_PROFILE_ITEMS", 1)
    collector = ks.ProfileCollector("3xowner1", PROFILE)
    collector.accept(
        {"feeds": [feed(), feed("3xvideo2"), feed("3xvideo3")], "pcursor": "no_more"},
        owner_id="3xowner1",
        cursor="",
    )
    assert len(collector.videos) == 1
    assert collector.problem_count == 2
    assert [problem["position"] for problem in collector.problems] == [2, 3]
    assert {problem["reason"] for problem in collector.problems} == {
        ks.PROFILE_PROBLEM_PAGE_LIMIT
    }
    assert not collector.complete
    summary = ks._profile_problem_summary(collector)
    assert "2 work(s) could not be verified" in summary
    # No verified identity exists for unparsed items, so none may be invented.
    assert "#" not in summary


def test_profile_warning_carries_the_problem_summary(monkeypatch):
    """2c: the summary reaches the discovery result the task layer displays."""
    monkeypatch.setattr(ks, "PROFILE_RETRY_BASE_SECONDS", 0.0)
    browser = BrowserFixture(
        final_url=PROFILE,
        state={},
        responses=[
            api_response(
                {
                    "result": 1,
                    "feeds": [feed(), feed("3xcoveronly", photoUrl=None, manifest=None)],
                    "pcursor": "next",
                }
            ),
            api_response({"result": 2}, cursor="next"),
        ],
    )
    monkeypatch.setattr(ks, "sync_playwright", browser.playwright)
    result = ks.discover(PROFILE)
    assert len(result.videos) == 1 and not result.complete
    assert result.warning.startswith(ks.PROFILE_INTERRUPTED)
    assert "Reason category: request_rejected." in result.warning
    assert "1 work(s) could not be verified" in result.warning
    assert "#2:3xcoveronly" in result.warning



def test_highest_bitrate_within_same_codec_without_cross_codec_comparison():
    representations = [
        {
            "id": "low",
            "url": MEDIA + "?low",
            "width": 1920,
            "height": 1080,
            "videoCodec": "avc",
            "avgBitrate": 1000,
        },
        {
            "id": "high",
            "url": MEDIA + "?high",
            "width": 1920,
            "height": 1080,
            "videoCodec": "avc",
            "avgBitrate": 8000,
            "fileSize": 5_000_000,
        },
        {
            "id": "hevc",
            "url": MEDIA + "?hevc",
            "width": 1920,
            "height": 1080,
            "videoCodec": "hevc",
            "avgBitrate": 3000,
        },
    ]
    video = ks.parse_video(
        feed(manifest={"adaptationSet": [{"representation": representations}]})
    )
    assert [asset.format_id for asset in video.assets] == [
        "kuaishou-high",
        "kuaishou-hevc",
    ]
    assert video.assets[0].video_codec == "h264"
    assert video.assets[0].bit_rate is None
    assert video.assets[0].size == 5_000_000


def test_unknown_codec_is_not_ranked_against_another_unknown_codec():
    low = {
        "id": "unknown-hevc",
        "url": MEDIA + "?hevc",
        "width": 1920,
        "height": 1080,
        "avgBitrate": 2000,
    }
    high = {
        "id": "unknown-avc",
        "url": MEDIA + "?avc",
        "width": 1920,
        "height": 1080,
        "avgBitrate": 8000,
    }
    video = ks.parse_video(
        feed(
            manifest={"adaptationSet": [{"representation": [high]}]},
            manifestH265={"adaptationSet": [{"representation": [low]}]},
        )
    )
    assert len(video.assets) == 2
    assert all(asset.video_codec is None for asset in video.assets)


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="The optional local FFmpeg runtime is not installed",
)
def test_real_video_with_high_audio_bitrate_passes_quality_verification(
    monkeypatch, tmp_path
):
    path = tmp_path / "audio-heavy.mp4"
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x90:r=10:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2",
            "-c:v",
            "libx264",
            "-crf",
            "28",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    probe = json.loads(
        subprocess.run(
            [
                shutil.which("ffprobe"),
                "-v",
                "error",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            timeout=10,
        ).stdout
    )
    rates = {
        stream["codec_type"]: int(stream["bit_rate"]) for stream in probe["streams"]
    }
    assert rates["audio"] > rates["video"]
    video = ks.parse_video(
        feed(
            duration=2000,
            photoUrl=None,
            manifest={
                "adaptationSet": [
                    {
                        "representation": [
                            {
                                "id": "original",
                                "url": MEDIA,
                                "width": 160,
                                "height": 90,
                                "videoCodec": "avc",
                                "fileSize": path.stat().st_size,
                                "avgBitrate": 192,
                            }
                        ]
                    }
                ]
            },
        )
    )
    asset = video.assets[0]
    assert asset.bit_rate is None and asset.size == path.stat().st_size
    downloader = engine.MediaDownloader(engine.DownloaderConfig(cookie_browser=None))
    monkeypatch.setattr(
        downloader, "_find_ffprobe_executable", lambda: shutil.which("ffprobe")
    )
    verified = downloader._verify_local_video_asset(
        path, asset, should_cancel=lambda: False
    )
    assert (verified.width, verified.height) == (160, 90)


def test_video_resource_and_foreign_author_blocked():
    value = feed(
        videoResource={
            "h264": {
                "adaptationSet": [
                    {
                        "representation": [
                            {"id": "4k", "url": MEDIA, "width": 3840, "height": 2160}
                        ]
                    }
                ]
            }
        },
        photoUrl=None,
    )
    assert ks.parse_video(value).assets[0].width == 3840
    with pytest.raises(DiscoveryError, match="different author"):
        ks.parse_video(value, owner_id="someone-else")
    with pytest.raises(DiscoveryError, match="different video"):
        ks.parse_video(value, expected_id="someone-else")


def test_profile_cursor_chain_dedup_and_completion():
    collector = ks.ProfileCollector("3xowner1", PROFILE)
    assert not collector.accept(
        {"feeds": [feed("foreign")], "pcursor": "no_more"}, owner_id="other", cursor=""
    )
    assert not collector.accept(
        {"feeds": [feed("out-of-order")], "pcursor": "no_more"},
        owner_id="3xowner1",
        cursor="next",
    )
    assert collector.accept(
        {"result": 1, "feeds": [feed()], "pcursor": "next"},
        owner_id="3xowner1",
        cursor="",
    )
    assert not collector.complete
    assert not collector.accept(
        {"feeds": [feed("duplicate-page")], "pcursor": "no_more"},
        owner_id="3xowner1",
        cursor="",
    )
    assert collector.accept(
        {"result": 1, "feeds": [feed(), feed("3xvideo2")], "pcursor": "no_more"},
        owner_id="3xowner1",
        cursor="next",
    )
    assert collector.complete
    assert list(collector.videos) == ["3xvideo1", "3xvideo2"]


@pytest.mark.parametrize("cursor", [None, "", "same"])
def test_profile_without_proven_end_remains_incomplete(cursor):
    collector = ks.ProfileCollector("3xowner1", PROFILE)
    collector.accept(
        {"result": 1, "feeds": [feed()], "pcursor": cursor},
        owner_id="3xowner1",
        cursor="",
    )
    assert not collector.complete


def test_profile_foreign_owner_is_not_downloaded():
    collector = ks.ProfileCollector("3xowner1", PROFILE)
    with pytest.raises(DiscoveryError):
        collector.accept(
            {"feeds": [feed(author="foreign")], "pcursor": "no_more"},
            owner_id="3xowner1",
            cursor="",
        )
    assert not collector.videos


def test_profile_budget_and_unsupported_media_not_false_complete(monkeypatch):
    monkeypatch.setattr(ks, "MAX_PROFILE_ITEMS", 1)
    collector = ks.ProfileCollector("3xowner1", PROFILE)
    collector.accept(
        {"feeds": [feed(), feed("second")], "pcursor": "no_more"},
        owner_id="3xowner1",
        cursor="",
    )
    assert len(collector.videos) == 1 and not collector.complete
    collector = ks.ProfileCollector("3xowner1", PROFILE)
    collector.accept(
        {"feeds": [feed(photoUrl=None)], "pcursor": "no_more"},
        owner_id="3xowner1",
        cursor="",
    )
    assert not collector.complete and not collector.videos


@pytest.mark.parametrize(
    ("payload", "error_type", "issue"),
    [
        ({"result": 109}, AuthenticationRequiredError, SiteIssueCode.LOGIN_REQUIRED),
        (
            {"result": 2, "message": "captcha required"},
            AuthenticationRequiredError,
            SiteIssueCode.VERIFICATION_REQUIRED,
        ),
        ({"result": 429}, TemporaryAccessError, SiteIssueCode.RATE_LIMITED),
        (
            {"result": 2, "message": "deleted"},
            TemporaryAccessError,
            SiteIssueCode.CONTENT_UNAVAILABLE,
        ),
        ({"result": 2}, TemporaryAccessError, SiteIssueCode.REQUEST_REJECTED),
    ],
)
def test_explicit_site_errors(payload, error_type, issue):
    with pytest.raises(error_type) as error:
        ks.response_error(payload, VIDEO)
    assert error.value.issue_code == issue
    if isinstance(error.value, AuthenticationRequiredError):
        assert error.value.verification_url == VIDEO


class BrowserFixture:
    def __init__(self, *, final_url=VIDEO, state=None, responses=()):
        self.final_url = final_url
        self.state = state if state is not None else apollo()
        self.responses = list(responses)
        self.page = self
        self.main_frame = object()
        self.version = "140.0.0.0"
        self.url = VIDEO
        self.listener = None
        self.handler = None
        self.closed = False
        self.added_cookies = []
        self.launch_options = None
        self.scrolls = 0

    def launch(self, **kwargs):
        self.launch_options = kwargs
        return self

    def new_context(self, **kwargs):
        assert kwargs["service_workers"] == "block"
        return self

    def new_page(self):
        return self

    def add_cookies(self, cookies):
        self.added_cookies.extend(cookies)

    def set_default_timeout(self, value):
        pass

    def route(self, pattern, handler):
        self.handler = handler

    def on(self, event, listener):
        self.listener = listener

    def goto(self, url, **kwargs):
        request = SimpleNamespace(
            url=self.final_url,
            is_navigation_request=lambda: True,
            frame=self.main_frame,
            resource_type="document",
            method="GET",
            post_data_buffer=None,
            headers={},
        )
        response = SimpleNamespace(
            url=self.final_url, headers={}, status=200, dispose=Mock()
        )
        route = SimpleNamespace(
            request=request,
            abort=Mock(),
            continue_=Mock(),
            fetch=Mock(return_value=response),
            fulfill=Mock(),
        )
        self.handler(route)
        if route.abort.called:
            raise RuntimeError("Navigation aborted")
        self.url = self.final_url
        if self.responses:
            self.listener(self.responses.pop(0))

    def reload(self, **kwargs):
        self.goto(self.url)

    def title(self):
        return "Kuaishou fixture"

    def locator(self, selector):
        assert selector != "body"
        return SimpleNamespace(all_inner_texts=list)

    def evaluate(self, script):
        if "APOLLO_STATE" in script:
            return self.state
        self.scrolls += 1
        if self.responses:
            self.listener(self.responses.pop(0))

    def wait_for_timeout(self, value):
        pass

    def close(self):
        self.closed = True

    @contextmanager
    def playwright(self):
        yield SimpleNamespace(chromium=self)


def api_response(payload, *, owner="3xowner1", cursor="", path="/rest/v/profile/feed"):
    raw = json.dumps(payload).encode()
    return SimpleNamespace(
        url="https://www.kuaishou.com" + path,
        request=SimpleNamespace(post_data_json={"user_id": owner, "pcursor": cursor}),
        headers={"content-length": str(len(raw))},
        body=lambda: raw,
    )


def test_browser_single_item_no_user_cookies(monkeypatch):
    browser = BrowserFixture()
    monkeypatch.setattr(ks, "sync_playwright", browser.playwright)
    monkeypatch.setattr(
        ks,
        "_extract_chrome_cookies",
        Mock(side_effect=AssertionError("Must not read cookies")),
    )
    result = ks.discover(VIDEO)
    assert result.videos[0].media_id == "3xvideo1"
    assert result.complete and browser.closed
    assert browser.launch_options == {"channel": "chrome", "headless": True}


def test_browser_profile_fixture_pagination_observation(monkeypatch):
    responses = [
        api_response({"result": 1, "feeds": [feed()], "pcursor": "next"}),
        api_response(
            {"result": 1, "feeds": [feed(), feed("second")], "pcursor": "no_more"},
            cursor="next",
        ),
    ]
    browser = BrowserFixture(final_url=PROFILE, state={}, responses=responses)
    monkeypatch.setattr(ks, "sync_playwright", browser.playwright)
    messages = []
    result = ks.discover(PROFILE, status_callback=messages.append)
    assert result.complete and len(result.videos) == 2
    assert browser.scrolls >= 1 and any("2 videos" in message for message in messages)


def test_browser_profile_stall_explicit_partial(monkeypatch):
    browser = BrowserFixture(
        final_url=PROFILE,
        state={},
        responses=[api_response({"result": 1, "feeds": [feed()], "pcursor": "next"})],
    )
    monkeypatch.setattr(ks, "sync_playwright", browser.playwright)
    result = ks.discover(PROFILE)
    assert not result.complete and result.warning == ks.PROFILE_INCOMPLETE
    assert len(result.videos) == 1 and browser.closed


@pytest.mark.parametrize(
    ("payload", "category", "retry_text"),
    [
        # Kuaishou's `result` is a site code, not an HTTP status, so an unknown
        # non-1 value is a rejection; only explicit limit language is rate_limited.
        ({"result": 2}, "request_rejected", "rejected a profile page"),
        ({"result": 2, "error_msg": "操作频繁请稍后再试"}, "rate_limited", "rate limited the profile"),
        ({"result": 429}, "rate_limited", "rate limited the profile"),
    ],
)
def test_profile_interruption_keeps_verified_works_and_reports_reason(
    monkeypatch, payload, category, retry_text
):
    """A rate-limited page must not discard the works already verified."""
    monkeypatch.setattr(ks, "PROFILE_RETRY_BASE_SECONDS", 0.0)
    browser = BrowserFixture(
        final_url=PROFILE,
        state={},
        responses=[
            api_response(
                {
                    "result": 1,
                    "feeds": [feed(), feed("3xvideo2")],
                    "pcursor": "next",
                }
            ),
            api_response(payload, cursor="next"),
        ],
    )
    monkeypatch.setattr(ks, "sync_playwright", browser.playwright)
    statuses = []
    result = ks.discover(PROFILE, status_callback=statuses.append)
    assert not result.complete
    assert sorted(video.media_id for video in result.videos) == [
        "3xvideo1",
        "3xvideo2",
    ]
    assert result.warning.startswith(ks.PROFILE_INTERRUPTED)
    assert f"Reason category: {category}." in result.warning
    assert any(retry_text in status for status in statuses)
    if category != "rate_limited":
        assert not any("rate limited" in status for status in statuses)
    assert browser.closed


@pytest.mark.parametrize(
    ("issue_code", "retry_text"),
    [
        (SiteIssueCode.NETWORK_ERROR, "network request failed"),
        (SiteIssueCode.SITE_UNAVAILABLE, "service is unavailable"),
    ],
)
def test_profile_retry_status_reports_transient_cause(
    monkeypatch, issue_code, retry_text
):
    monkeypatch.setattr(ks, "PROFILE_RETRY_BASE_SECONDS", 0.0)
    original_response_error = ks.response_error

    def fail_after_first_page(payload, source_url):
        if payload.get("result") == 2:
            raise TemporaryAccessError("Synthetic interruption", issue_code=issue_code)
        return original_response_error(payload, source_url)

    monkeypatch.setattr(ks, "response_error", fail_after_first_page)
    browser = BrowserFixture(
        final_url=PROFILE,
        state={},
        responses=[
            api_response({"result": 1, "feeds": [feed()], "pcursor": "next"}),
            api_response({"result": 2}, cursor="next"),
        ],
    )
    monkeypatch.setattr(ks, "sync_playwright", browser.playwright)
    statuses = []
    result = ks.discover(PROFILE, status_callback=statuses.append)
    assert len(result.videos) == 1 and not result.complete
    assert f"Reason category: {issue_code.value}." in result.warning
    assert any(retry_text in status for status in statuses)
    assert not any("rate limited" in status for status in statuses)
    assert browser.closed


def test_profile_interruption_without_any_work_raises_the_real_error(monkeypatch):
    """An interruption with nothing verified is a failure, never an empty profile."""
    monkeypatch.setattr(ks, "PROFILE_RETRY_BASE_SECONDS", 0.0)
    browser = BrowserFixture(
        final_url=PROFILE,
        state={},
        responses=[api_response({"result": 2})],
    )
    monkeypatch.setattr(ks, "sync_playwright", browser.playwright)
    with pytest.raises(TemporaryAccessError) as error:
        ks.discover(PROFILE)
    assert error.value.issue_code == SiteIssueCode.REQUEST_REJECTED
    assert "Reason category: request_rejected." in str(error.value)
    assert browser.closed


def test_profile_interruption_retries_are_bounded(monkeypatch):
    """Retries stop at the configured limit instead of waiting indefinitely."""
    monkeypatch.setattr(ks, "PROFILE_RETRY_BASE_SECONDS", 0.0)
    monkeypatch.setattr(ks, "PROFILE_RETRY_ATTEMPTS", 2)
    browser = BrowserFixture(
        final_url=PROFILE,
        state={},
        responses=[
            api_response({"result": 1, "feeds": [feed()], "pcursor": "next"}),
            api_response({"result": 2}, cursor="next"),
            api_response({"result": 2}, cursor="next"),
        ],
    )
    monkeypatch.setattr(ks, "sync_playwright", browser.playwright)
    statuses = []
    result = ks.discover(PROFILE, status_callback=statuses.append)
    assert len(result.videos) == 1 and not result.complete
    retries = [text for text in statuses if "rejected a profile page" in text]
    assert len(retries) == 2
    assert "(1/2)" in retries[0] and "(2/2)" in retries[1]
    assert result.warning.startswith(ks.PROFILE_INTERRUPTED)


def test_profile_interruption_recovers_when_a_retry_succeeds(monkeypatch):
    """A successful retry resumes pagination from the interrupted cursor."""
    monkeypatch.setattr(ks, "PROFILE_RETRY_BASE_SECONDS", 0.0)
    browser = BrowserFixture(
        final_url=PROFILE,
        state={},
        responses=[
            api_response({"result": 1, "feeds": [feed()], "pcursor": "next"}),
            api_response({"result": 2}, cursor="next"),
            api_response(
                {
                    "result": 1,
                    "feeds": [feed("3xvideo2")],
                    "pcursor": "no_more",
                },
                cursor="next",
            ),
        ],
    )
    monkeypatch.setattr(ks, "sync_playwright", browser.playwright)
    result = ks.discover(PROFILE)
    assert result.complete
    assert result.warning is None
    assert sorted(video.media_id for video in result.videos) == [
        "3xvideo1",
        "3xvideo2",
    ]


def test_profile_cancel_during_retry_wait_raises_without_partial_result(monkeypatch):
    """Cancellation is honored while waiting out a rate limit."""
    monkeypatch.setattr(ks, "PROFILE_RETRY_BASE_SECONDS", 30.0)
    browser = BrowserFixture(
        final_url=PROFILE,
        state={},
        responses=[
            api_response({"result": 1, "feeds": [feed()], "pcursor": "next"}),
            api_response({"result": 2}, cursor="next"),
        ],
    )
    ticks = []
    original_wait = browser.wait_for_timeout

    def counting_wait(value):
        ticks.append(value)
        # The first five ticks belong to the normal poll loop; the retry wait
        # starts afterwards, so arm cancellation once that wait is running.
        if len(ticks) > 5:
            armed[0] = True
        return original_wait(value)

    armed = [False]
    browser.wait_for_timeout = counting_wait
    monkeypatch.setattr(ks, "sync_playwright", browser.playwright)
    with pytest.raises(DownloadCancelledError):
        ks.discover(PROFILE, should_cancel=lambda: armed[0])
    assert browser.closed


def test_profile_retry_resumes_from_the_interrupted_cursor():
    """The rejected page must not consume or advance the pagination cursor."""
    collector = ks.ProfileCollector("3xowner1", PROFILE)
    collector.accept(
        {"result": 1, "feeds": [feed()], "pcursor": "next"},
        owner_id="3xowner1",
        cursor="",
    )
    assert collector.next_cursor == "next"
    with pytest.raises(TemporaryAccessError):
        collector.accept(
            {"result": 2}, owner_id="3xowner1", cursor="next"
        )
    # A rejected page leaves the chain intact, so the same cursor can be retried.
    assert collector.next_cursor == "next"
    assert collector.seen_cursors == {""}
    assert collector.accept(
        {"result": 1, "feeds": [feed("3xvideo2")], "pcursor": "no_more"},
        owner_id="3xowner1",
        cursor="next",
    )
    assert collector.complete
    assert sorted(collector.videos) == ["3xvideo1", "3xvideo2"]


@pytest.mark.parametrize(
    ("payload", "error_type", "issue"),
    [
        ({"result": 109}, AuthenticationRequiredError, SiteIssueCode.LOGIN_REQUIRED),
        (
            {"result": 2, "message": "captcha required"},
            AuthenticationRequiredError,
            SiteIssueCode.VERIFICATION_REQUIRED,
        ),
    ],
)
def test_profile_login_and_verification_stay_fatal(monkeypatch, payload, error_type, issue):
    """Login and verification problems are never degraded into a partial result."""
    monkeypatch.setattr(ks, "PROFILE_RETRY_BASE_SECONDS", 0.0)
    browser = BrowserFixture(
        final_url=PROFILE,
        state={},
        responses=[
            api_response({"result": 1, "feeds": [feed()], "pcursor": "next"}),
            api_response(payload, cursor="next"),
        ],
    )
    monkeypatch.setattr(ks, "sync_playwright", browser.playwright)
    with pytest.raises(error_type) as error:
        ks.discover(PROFILE)
    assert error.value.issue_code == issue
    assert browser.closed


def test_profile_foreign_author_stays_fatal(monkeypatch):
    """An identity mismatch is never treated as a recoverable interruption."""
    monkeypatch.setattr(ks, "PROFILE_RETRY_BASE_SECONDS", 0.0)
    browser = BrowserFixture(
        final_url=PROFILE,
        state={},
        responses=[api_response({"feeds": [feed(author="foreign")], "pcursor": "next"})],
    )
    monkeypatch.setattr(ks, "sync_playwright", browser.playwright)
    with pytest.raises(DiscoveryError):
        ks.discover(PROFILE)


def test_profile_unsupported_response_change_stays_fatal(monkeypatch):
    """A structural response change must stay loud instead of looking transient."""
    monkeypatch.setattr(ks, "PROFILE_RETRY_BASE_SECONDS", 0.0)
    browser = BrowserFixture(
        final_url=PROFILE,
        state={},
        responses=[api_response({"result": 1, "pcursor": "next"})],
    )
    monkeypatch.setattr(ks, "sync_playwright", browser.playwright)
    with pytest.raises(TemporaryAccessError) as error:
        ks.discover(PROFILE)
    assert error.value.issue_code == SiteIssueCode.SITE_RESPONSE_CHANGED


@pytest.mark.parametrize(
    ("issue", "paginating", "expected"),
    [
        (SiteIssueCode.RATE_LIMITED, True, True),
        (SiteIssueCode.REQUEST_REJECTED, True, True),
        (SiteIssueCode.SITE_UNAVAILABLE, True, True),
        (SiteIssueCode.NETWORK_ERROR, True, True),
        (SiteIssueCode.RATE_LIMITED, False, False),
        (SiteIssueCode.LOGIN_REQUIRED, True, False),
        (SiteIssueCode.VERIFICATION_REQUIRED, True, False),
        (SiteIssueCode.SITE_RESPONSE_CHANGED, True, False),
        (SiteIssueCode.SECURITY_BLOCKED, True, False),
        (SiteIssueCode.CONTENT_UNAVAILABLE, True, False),
        (SiteIssueCode.UNKNOWN, True, False),
        (None, True, False),
    ],
)
def test_recoverable_interruption_classification(issue, paginating, expected):
    exc = TemporaryAccessError("fixture", issue_code=issue)
    assert ks.is_recoverable_profile_interruption(exc, paginating=paginating) is expected


@pytest.mark.parametrize(
    "exc",
    [
        AuthenticationRequiredError("login", issue_code=SiteIssueCode.LOGIN_REQUIRED),
        DiscoveryError("identity changed"),
    ],
)
def test_authentication_and_identity_failures_are_never_recoverable(exc):
    assert ks.is_recoverable_profile_interruption(exc, paginating=True) is False


def test_item_rate_limit_is_not_a_recoverable_profile_interruption():
    """A single video has no partial result to preserve, so it must fail loudly."""
    exc = TemporaryAccessError("limited", issue_code=SiteIssueCode.RATE_LIMITED)
    assert ks.is_recoverable_profile_interruption(exc, paginating=False) is False


@pytest.mark.parametrize(
    ("issue", "expected"),
    [
        (SiteIssueCode.RATE_LIMITED, "rate_limited"),
        (SiteIssueCode.REQUEST_REJECTED, "request_rejected"),
        (SiteIssueCode.NETWORK_ERROR, "network_error"),
        (None, "unknown"),
    ],
)
def test_interruption_category_is_a_fixed_code(issue, expected):
    exc = TemporaryAccessError("fixture", issue_code=issue) if issue else RuntimeError("x")
    assert ks._public_interruption_category(exc) == expected


def test_interruption_category_never_echoes_site_text():
    hostile = TemporaryAccessError(
        "频繁操作 secret=/Users/someone/Default token=abc",
        issue_code=SiteIssueCode.RATE_LIMITED,
    )
    assert ks._public_interruption_category(hostile) == "rate_limited"
    assert "someone" not in ks._public_interruption_category(hostile)
    assert ks._public_interruption_category(None) == "unknown"



def test_unsupported_terminal_page_stops_immediately(monkeypatch):
    browser = BrowserFixture(
        final_url=PROFILE,
        state={},
        responses=[
            api_response(
                {
                    "result": 1,
                    "feeds": [feed(), feed("image", photoUrl=None)],
                    "pcursor": "no_more",
                }
            )
        ],
    )
    monkeypatch.setattr(ks, "sync_playwright", browser.playwright)
    result = ks.discover(PROFILE)
    assert not result.complete and result.warning
    assert browser.scrolls == 0


def test_short_item_rest_response_reobserved_after_identity_known(monkeypatch):
    raw = json.dumps({"data": {"visionVideoDetail": {"result": 1, **feed()}}}).encode()
    response = SimpleNamespace(
        url="https://www.kuaishou.com/graphql",
        headers={},
        body=lambda: raw,
        request=SimpleNamespace(
            post_data_json={
                "operationName": "visionVideoDetail",
                "variables": {"photoId": "3xvideo1"},
            }
        ),
    )
    browser = BrowserFixture(state={}, responses=[response, response])
    monkeypatch.setattr(ks, "sync_playwright", browser.playwright)
    result = ks.discover("https://v.kuaishou.com/share123")
    assert result.videos[0].media_id == "3xvideo1" and not browser.responses


@pytest.mark.parametrize("title", ["Domain Blocked", "Website Filtered"])
def test_local_web_filter_has_distinct_network_error(monkeypatch, title):
    browser = BrowserFixture(state={})
    browser.title = lambda: title
    monkeypatch.setattr(ks, "sync_playwright", browser.playwright)
    with pytest.raises(TemporaryAccessError) as error:
        ks.discover(VIDEO)
    assert error.value.issue_code == SiteIssueCode.NETWORK_ERROR
    assert "verification is not required" in str(error.value)
    assert "proxy or network policy" in str(error.value)


def test_short_redirect_resolves_item(monkeypatch):
    browser = BrowserFixture()
    monkeypatch.setattr(ks, "sync_playwright", browser.playwright)
    result = ks.discover("https://v.kuaishou.com/share123")
    assert result.source_kind == "item" and result.source_id == "3xvideo1"


def test_redirect_to_private_or_other_site_aborts_before_navigation(monkeypatch):
    browser = BrowserFixture(final_url="http://127.0.0.1/private")
    monkeypatch.setattr(ks, "sync_playwright", browser.playwright)
    with pytest.raises(DiscoveryError, match="outside trusted"):
        ks.discover("https://v.kuaishou.com/share123")
    assert browser.closed


def test_direct_video_redirect_to_another_identity_blocked(monkeypatch):
    browser = BrowserFixture(final_url="https://www.kuaishou.com/short-video/foreign")
    monkeypatch.setattr(ks, "sync_playwright", browser.playwright)
    with pytest.raises(DiscoveryError, match="different video"):
        ks.discover(VIDEO)


def test_cancel_before_cookie_or_browser_access(monkeypatch):
    monkeypatch.setattr(
        ks, "_browser_cookies", Mock(side_effect=AssertionError("No cookie read"))
    )
    monkeypatch.setattr(
        ks, "sync_playwright", Mock(side_effect=AssertionError("No launch"))
    )
    with pytest.raises(DownloadCancelledError):
        ks.discover(VIDEO, use_browser_cookies=True, should_cancel=lambda: True)


def test_cancel_during_profile_closes_browser(monkeypatch):
    browser = BrowserFixture(final_url=PROFILE, state={})
    monkeypatch.setattr(ks, "sync_playwright", browser.playwright)
    with pytest.raises(DownloadCancelledError):
        ks.discover(PROFILE, should_cancel=lambda: browser.scrolls > 0)
    assert browser.closed


def test_cookie_read_error_never_falls_back_to_anonymous(monkeypatch):
    monkeypatch.setattr(
        ks,
        "_extract_chrome_cookies",
        Mock(side_effect=RuntimeError("fixture unavailable")),
    )
    monkeypatch.setattr(
        ks, "sync_playwright", Mock(side_effect=AssertionError("No anonymous launch"))
    )
    with pytest.raises(TemporaryAccessError) as error:
        ks.discover(VIDEO, use_browser_cookies=True)
    assert error.value.issue_code == SiteIssueCode.COOKIE_UNAVAILABLE


COOKIE_FAILURE_CASES = [
    (PermissionError("operation not permitted on the cookie store"), "cookie_permission_denied"),
    (OSError("cookie database is locked by another process"), "cookie_database_locked"),
    (RuntimeError("failed to decrypt the cookie value with keychain"), "cookie_decryption_failed"),
    (RuntimeError("some unclassified failure"), "cookie_access_unknown"),
]


@pytest.mark.parametrize(("cause", "expected"), COOKIE_FAILURE_CASES)
def test_kuaishou_cookie_failure_carries_a_safe_diagnostic(
    monkeypatch, tmp_path, cause, expected
):
    """R5: the real Kuaishou path classifies the failure and never leaks details.

    The diagnostic helper falls back to a filesystem probe when the error text
    carries no marker. Pin that probe to a complete temporary profile so the
    category depends only on the injected error, never on whether the host (for
    example a bare CI runner) happens to have Chrome installed.
    """
    profile_dir = tmp_path / "Default"
    (profile_dir / "Network").mkdir(parents=True)
    (profile_dir / "Network" / "Cookies").write_bytes(b"")
    monkeypatch.setattr(
        chrome_browser, "chrome_user_data_directory", lambda *a, **k: tmp_path
    )
    monkeypatch.setattr(ks, "_extract_chrome_cookies", Mock(side_effect=cause))
    monkeypatch.setattr(
        ks, "sync_playwright", Mock(side_effect=AssertionError("No anonymous launch"))
    )
    with pytest.raises(TemporaryAccessError) as error:
        ks.discover(VIDEO, use_browser_cookies=True, cookie_profile="Default")
    failure = error.value
    assert failure.issue_code == SiteIssueCode.COOKIE_UNAVAILABLE
    assert failure.diagnostic_code == expected
    assert f"Diagnostic: {expected}." in str(failure)
    assert "Quit Chrome" not in str(failure)
    # The original exception text is never echoed; only the fixed category is.
    assert str(cause) not in str(failure)
    assert failure.__cause__ is cause


@pytest.mark.parametrize(
    "signal", [DownloadCancelledError, KeyboardInterrupt, SystemExit]
)
def test_kuaishou_cookie_failure_does_not_relabel_cancellation(monkeypatch, signal):
    """R5: a cancellation is never rewritten into a cookie-unavailable failure."""
    raised = signal("Task cancelled") if signal is DownloadCancelledError else signal()
    monkeypatch.setattr(ks, "_extract_chrome_cookies", Mock(side_effect=raised))
    monkeypatch.setattr(
        ks, "sync_playwright", Mock(side_effect=AssertionError("No anonymous launch"))
    )
    with pytest.raises(signal) as error:
        ks.discover(VIDEO, use_browser_cookies=True)
    assert error.value is raised
    assert not isinstance(error.value, TemporaryAccessError) or (
        error.value.issue_code != SiteIssueCode.COOKIE_UNAVAILABLE
    )


def test_kuaishou_cookie_diagnostic_never_leaks_profile_or_cookie_data(monkeypatch):
    """R5: neither the profile path nor any cookie value reaches the message."""
    secret_path = "/Users/someone/Library/Application Support/Google/Chrome/Default"
    monkeypatch.setattr(
        ks,
        "_extract_chrome_cookies",
        Mock(
            side_effect=RuntimeError(
                f"cannot open {secret_path} sessionid=cookie-secret token=abc123"
            )
        ),
    )
    monkeypatch.setattr(
        ks, "sync_playwright", Mock(side_effect=AssertionError("No anonymous launch"))
    )
    with pytest.raises(TemporaryAccessError) as error:
        ks.discover(VIDEO, use_browser_cookies=True)
    message = str(error.value)
    for secret in ("someone", "Application Support", "sessionid", "cookie-secret", "abc123"):
        assert secret not in message
    assert error.value.diagnostic_code in {
        "cookie_decryption_failed",
        "cookie_permission_denied",
        "cookie_database_locked",
        "chrome_data_directory_missing",
        "chrome_profile_invalid",
        "chrome_profile_missing",
        "cookie_database_missing",
        "cookie_access_unknown",
    }


def test_task_state_records_the_kuaishou_cookie_diagnostic(tmp_path):
    """R5: the category reaches persisted task state, not only the exception."""
    from app.task_manager import _recover_cookie_diagnostic_code

    message = (
        "Kuaishou Chrome cookies could not be read. Quit Chrome and retry, or "
        "disable Chrome Cookie explicitly. Diagnostic: cookie_database_locked."
    )
    assert _recover_cookie_diagnostic_code(message) == "cookie_database_locked"
    assert _recover_cookie_diagnostic_code("no marker here") is None
    assert _recover_cookie_diagnostic_code(
        "Diagnostic: /Users/someone/Default"
    ) is None

    job = DownloadJob(
        id="fixture",
        source_url=VIDEO,
        platform=Platform.KUAISHOU,
        source_kind=SourceKind.ITEM,
        output_root=str(tmp_path),
        status=JobStatus.FAILED,
    )
    item = DownloadItem(
        id="item",
        media_id="3xvideo1",
        source_url=VIDEO,
        status=ItemStatus.FAILED,
        error=message,
    )
    job.items.append(item)
    DownloadManager._record_issue_locked(job, message, item=item, cause=RuntimeError(message))
    assert item.issue_code == SiteIssueCode.COOKIE_UNAVAILABLE
    assert item.diagnostic_code is None
    assert DownloadManager._backfill_job_issue_locked(job) is True
    assert item.diagnostic_code == "cookie_database_locked"


def test_signing_diagnostic_is_not_mistaken_for_a_cookie_diagnostic(tmp_path):
    """R5: a Douyin signing code must never become a cookie category."""
    from app.task_manager import DownloadManager as Manager

    job = DownloadJob(
        id="fixture",
        source_url="https://www.douyin.com/video/1",
        platform=Platform.DOUYIN,
        source_kind=SourceKind.ITEM,
        output_root=str(tmp_path),
        status=JobStatus.FAILED,
    )
    item = DownloadItem(
        id="item",
        media_id="1",
        source_url="https://www.douyin.com/video/1",
        status=ItemStatus.FAILED,
        error="Douyin automatic media refresh did not pass identity or integrity "
        "validation. Diagnostic code: signing-validation-failed.",
    )
    job.items.append(item)

    class SigningOnly(RuntimeError):
        diagnostic_code = "signing-validation-failed"

    Manager._record_issue_locked(job, item.error, item=item, cause=SigningOnly(item.error))
    assert item.diagnostic_code is None
    Manager._backfill_job_issue_locked(job)
    assert item.diagnostic_code is None


def test_engine_discovery_persists_only_identity_not_expiring_urls(monkeypatch):
    result = ks.Result(
        [ks.parse_video(feed())], "profile", "3xowner1", False, ks.PROFILE_INCOMPLETE
    )
    monkeypatch.setattr(engine, "discover_kuaishou", lambda *args, **kwargs: result)
    downloader = engine.MediaDownloader(engine.DownloaderConfig(cookie_browser=None))
    first = downloader.discover(PROFILE, Platform.KUAISHOU, SourceKind.PROFILE)
    second = downloader.discover(PROFILE, Platform.KUAISHOU, SourceKind.PROFILE)
    assert first.items[0].id == second.items[0].id
    assert first.items[0].metadata["kuaishou_author_id"] == "3xowner1"
    assert MEDIA not in first.items[0].model_dump_json()
    assert not first.discovery_complete and first.warning


def discovered_item(monkeypatch):
    result = ks.Result([ks.parse_video(feed())], "item", "3xvideo1")
    monkeypatch.setattr(engine, "discover_kuaishou", lambda *args, **kwargs: result)
    downloader = engine.MediaDownloader(engine.DownloaderConfig(cookie_browser=None))
    item = downloader.discover(VIDEO, Platform.KUAISHOU, SourceKind.ITEM).items[0]
    return downloader, item


def test_download_refreshes_links_and_reuses_ffprobe_pipeline(monkeypatch, tmp_path):
    downloader, item = discovered_item(monkeypatch)
    refreshed = ks.Result(
        [ks.parse_video(feed(photoUrl=MEDIA + "?fresh"))], "item", "3xvideo1"
    )
    refresh = Mock(return_value=refreshed)
    monkeypatch.setattr(engine, "discover_kuaishou", refresh)
    captured = {}

    def transfer(ydl, assets, *args, **kwargs):
        captured.update(kwargs)
        assert assets[0].candidates == [MEDIA + "?fresh"]
        (tmp_path / "result.mp4").write_bytes(b"verified transfer fixture")
        return tmp_path / "result.mp4", RemoteAsset([MEDIA], 1, width=1920, height=1080)

    monkeypatch.setattr(downloader, "_download_first_available_asset", transfer)
    outcome = downloader.download_item(item, Platform.KUAISHOU, tmp_path)
    assert refresh.call_count == 1
    assert captured["verify_declared_dimensions"] is True
    assert captured["platform"] == Platform.KUAISHOU
    assert outcome.resolution == "1920x1080"


def test_download_changed_author_blocked(monkeypatch, tmp_path):
    downloader, item = discovered_item(monkeypatch)
    monkeypatch.setattr(
        engine,
        "discover_kuaishou",
        lambda *args, **kwargs: ks.Result(
            [ks.parse_video(feed(author="foreign"))], "item", "3xvideo1"
        ),
    )
    with pytest.raises(MediaDownloadError, match="author identity"):
        downloader.download_item(item, Platform.KUAISHOU, tmp_path)
    assert not list(tmp_path.iterdir())


def make_real_jpeg(tmp_path, width, height, *, color="red", name="fixture.jpg"):
    """Create a real decodable JPEG so image checks are not header-only guesses."""
    target = tmp_path / name
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s={width}x{height}",
            "-frames:v",
            "1",
            str(target),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return target


def make_real_mp4(
    tmp_path, width, height, *, color="blue", seconds=1.0, name="fixture.mp4"
):
    """Create a real FFprobe-verifiable H.264 video for reuse regressions."""
    target = tmp_path / name
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s={width}x{height}:r=10:d={seconds}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "32",
            str(target),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return target


def two_rendition_video():
    """A video whose H.264 and HEVC renditions share the highest resolution."""
    return ks.parse_video(
        feed(
            manifest={
                "adaptationSet": [
                    {
                        "representation": [
                            {
                                "id": "h264-high",
                                "url": MEDIA + "?h264",
                                "width": 1920,
                                "height": 1080,
                                "videoCodec": "avc",
                                "avgBitrate": 8000,
                            },
                            {
                                "id": "hevc-high",
                                "url": MEDIA + "?hevc",
                                "width": 1920,
                                "height": 1080,
                                "videoCodec": "hevc",
                                "avgBitrate": 3000,
                            },
                        ]
                    }
                ]
            }
        )
    )


def test_video_renditions_are_one_candidate_set_producing_one_file(
    monkeypatch, tmp_path
):
    """R3: two available highest renditions must yield ONE video, not two files."""
    video = two_rendition_video()
    assert len(video.assets) == 2
    result = ks.Result([video], "item", "3xvideo1")
    monkeypatch.setattr(engine, "discover_kuaishou", lambda *a, **k: result)
    downloader = engine.MediaDownloader(engine.DownloaderConfig(cookie_browser=None))
    item = downloader.discover(VIDEO, Platform.KUAISHOU, SourceKind.ITEM).items[0]

    calls = []

    def transfer(ydl, assets, *args, **kwargs):
        calls.append((list(assets), kwargs))
        (tmp_path / "one.mp4").write_bytes(b"verified transfer fixture")
        return tmp_path / "one.mp4", RemoteAsset(
            [MEDIA], 1, width=1920, height=1080
        )

    monkeypatch.setattr(downloader, "_download_first_available_asset", transfer)
    outcome = downloader.download_item(item, Platform.KUAISHOU, tmp_path)
    assert len(calls) == 1
    assert len(calls[0][0]) == 2
    assert calls[0][1]["media_type"] == MediaType.VIDEO
    # A video is one work, so it carries no album position at all.
    assert calls[0][1].get("asset_index") is None
    assert outcome.output_paths == [str(tmp_path / "one.mp4")]
    assert outcome.media_type == MediaType.VIDEO


def test_video_second_rendition_is_tried_after_first_transfer_failure(
    monkeypatch, tmp_path
):
    """R3: one failed rendition must not abandon the remaining candidates."""
    downloader = engine.MediaDownloader(engine.DownloaderConfig(cookie_browser=None))
    payload = b"\0\0\0\x18ftypmp42" + b"\0" * 4096
    attempts = []

    def opener(ydl, request, *, is_trusted_url):
        attempts.append(request.url)
        if request.url.endswith("?h264"):
            raise urllib.error.HTTPError(
                request.url, 503, "unavailable", {}, None
            )
        response = io.BytesIO(payload)
        response.headers = {
            "Content-Type": "video/mp4",
            "Content-Length": str(len(payload)),
        }
        response.url = request.url
        return response

    monkeypatch.setattr(engine, "_open_xiaohongshu_response", opener)
    monkeypatch.setattr(
        downloader,
        "_verify_local_video_asset",
        lambda path, asset, **kwargs: RemoteAsset(
            list(asset.candidates), asset.index, width=1920, height=1080
        ),
    )
    video = two_rendition_video()
    path, chosen = downloader._download_first_available_asset(
        SimpleNamespace(),
        list(video.assets),
        tmp_path,
        "2026-09-19",
        "Fixture",
        "3xvideo1",
        VIDEO,
        platform=Platform.KUAISHOU,
        media_type=MediaType.VIDEO,
        callback=None,
        should_cancel=lambda: False,
        verify_declared_dimensions=True,
    )
    assert attempts == [MEDIA + "?h264", MEDIA + "?hevc"]
    assert path.is_file()
    assert (chosen.width, chosen.height) == (1920, 1080)


def test_video_all_renditions_failing_reports_failure_without_lower_quality(
    monkeypatch, tmp_path
):
    """R3: when every highest candidate fails, report failure; never downgrade."""
    downloader = engine.MediaDownloader(engine.DownloaderConfig(cookie_browser=None))
    attempts = []

    def opener(ydl, request, *, is_trusted_url):
        attempts.append(request.url)
        raise urllib.error.HTTPError(request.url, 503, "unavailable", {}, None)

    monkeypatch.setattr(engine, "_open_xiaohongshu_response", opener)
    video = two_rendition_video()
    with pytest.raises(MediaDownloadError, match="All highest-available media URLs"):
        downloader._download_first_available_asset(
            SimpleNamespace(),
            list(video.assets),
            tmp_path,
            "2026-09-19",
            "Fixture",
            "3xvideo1",
            VIDEO,
            platform=Platform.KUAISHOU,
            media_type=MediaType.VIDEO,
            callback=None,
            should_cancel=lambda: False,
            verify_declared_dimensions=True,
        )
    assert attempts == [MEDIA + "?h264", MEDIA + "?hevc"]
    assert not list(tmp_path.iterdir())


def test_video_cancel_leaves_no_partial_file(monkeypatch, tmp_path):
    """R3: cancellation stops immediately and leaves no partial output."""
    downloader = engine.MediaDownloader(engine.DownloaderConfig(cookie_browser=None))
    payload = b"\0\0\0\x18ftypmp42" + b"\0" * 4096
    cancelled = False

    def opener(ydl, request, *, is_trusted_url):
        response = io.BytesIO(payload)
        response.headers = {
            "Content-Type": "video/mp4",
            "Content-Length": str(len(payload)),
        }
        response.url = request.url
        return response

    def callback(event):
        nonlocal cancelled
        if event.event == "downloading":
            cancelled = True

    monkeypatch.setattr(engine, "_open_xiaohongshu_response", opener)
    video = two_rendition_video()
    with pytest.raises(DownloadCancelledError):
        downloader._download_first_available_asset(
            SimpleNamespace(),
            list(video.assets),
            tmp_path,
            "2026-09-19",
            "Fixture",
            "3xvideo1",
            VIDEO,
            platform=Platform.KUAISHOU,
            media_type=MediaType.VIDEO,
            callback=callback,
            should_cancel=lambda: cancelled,
            verify_declared_dimensions=True,
        )
    assert not list(tmp_path.iterdir())


@contextmanager
def local_media_server(payload, content_type):
    """Serve real bytes over a real local HTTP socket for transfer regressions."""
    served = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            served.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", served
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


@pytest.mark.skipif(
    not shutil.which("ffmpeg"),
    reason="The optional local FFmpeg runtime is not installed",
)
def test_album_image_transfer_over_local_http_writes_and_verifies_real_bytes(
    monkeypatch, tmp_path
):
    """R3: a real local HTTP transfer writes and verifies a real image file."""
    source = make_real_jpeg(tmp_path, 800, 600, color="red", name="source.jpg")
    payload = source.read_bytes()
    downloader = engine.MediaDownloader(engine.DownloaderConfig(cookie_browser=None))
    with local_media_server(payload, "image/jpeg") as (base, served):

        def opener(ydl, request, *, is_trusted_url):
            local = urllib.request.urlopen(base + "/image.jpg", timeout=10)
            response = io.BytesIO(local.read())
            local.close()
            response.headers = {
                "Content-Type": "image/jpeg",
                "Content-Length": str(len(payload)),
            }
            response.url = request.url
            return response

        monkeypatch.setattr(engine, "_open_xiaohongshu_response", opener)
        path, chosen = downloader._download_first_available_asset(
            SimpleNamespace(),
            [
                RemoteAsset(
                    [MEDIA + "?first"], index=1, width=800, height=600,
                    format_id="kuaishou-image-1",
                )
            ],
            tmp_path,
            "2026-09-19",
            "Fixture album",
            "3xalbum1",
            "https://www.kuaishou.com/short-video/3xalbum1",
            platform=Platform.KUAISHOU,
            media_type=MediaType.IMAGE,
            callback=None,
            should_cancel=lambda: False,
            asset_index=1,
            verify_declared_dimensions=True,
        )
    assert served == ["/image.jpg"]
    assert path.is_file()
    assert path.name == "2026-09-19-Fixture album-001.jpg"
    assert (chosen.width, chosen.height) == (800, 600)
    assert path.read_bytes() == payload


@pytest.mark.skipif(
    not shutil.which("ffmpeg"),
    reason="The optional local FFmpeg runtime is not installed",
)
def test_saved_album_image_reuse_requires_a_full_decode(tmp_path):
    """R4: a reused album image must fully decode, not just pass a header check."""
    downloader = engine.MediaDownloader(engine.DownloaderConfig(cookie_browser=None))
    good = make_real_jpeg(tmp_path, 800, 600, color="blue", name="good.jpg")
    downloader._decode_local_image(good, should_cancel=lambda: False)

    truncated = make_real_jpeg(tmp_path, 1600, 1200, color="green", name="full.jpg")
    truncated.write_bytes(truncated.read_bytes()[:3000])
    # The truncated file still reports plausible header dimensions.
    assert downloader._image_dimensions(truncated.read_bytes())
    with pytest.raises(MediaDownloadError, match="full decode verification"):
        downloader._decode_local_image(truncated, should_cancel=lambda: False)


@pytest.mark.skipif(
    not shutil.which("ffmpeg"),
    reason="The optional local FFmpeg runtime is not installed",
)
def test_existing_album_image_is_reused_only_when_verified(tmp_path):
    """R4: reuse needs identity, position and a verified file; otherwise redownload."""
    downloader = engine.MediaDownloader(engine.DownloaderConfig(cookie_browser=None))
    saved = make_real_jpeg(tmp_path, 800, 600, color="blue", name="saved.jpg")
    asset = RemoteAsset(
        [MEDIA + "?first"], index=1, width=800, height=600,
        format_id="kuaishou-image-1",
    )
    reused = downloader._existing_kuaishou_image_asset(
        downloader._kuaishou_completion_record(
            saved, asset, "3xalbum1", "image", 1, should_cancel=lambda: False,
        ),
        tmp_path,
        asset,
        should_cancel=lambda: False,
    )
    assert reused is not None
    assert reused[0] == saved.resolve()
    assert (reused[1].width, reused[1].height) == (800, 600)

    truncated = make_real_jpeg(tmp_path, 1600, 1200, color="green", name="broken.jpg")
    truncated.write_bytes(truncated.read_bytes()[:3000])
    assert downloader._existing_kuaishou_image_asset(
        downloader._kuaishou_completion_record(
            truncated, asset, "3xalbum1", "image", 1, should_cancel=lambda: False,
        ),
        tmp_path,
        asset,
        should_cancel=lambda: False,
    ) is None

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    moved = outside / "saved.jpg"
    shutil.copy(saved, moved)
    assert downloader._existing_kuaishou_image_asset(
        downloader._kuaishou_completion_record(
            moved, asset, "3xalbum1", "image", 1, should_cancel=lambda: False,
        ),
        tmp_path,
        asset,
        should_cancel=lambda: False,
    ) is None

    assert downloader._existing_kuaishou_image_asset(
        {"media_id": "3xalbum1", "index": 1, "path": str(tmp_path / "missing.jpg")},
        tmp_path,
        asset,
        should_cancel=lambda: False,
    ) is None
    assert downloader._existing_kuaishou_image_asset(
        None, tmp_path, asset, should_cancel=lambda: False
    ) is None


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="The optional local FFmpeg runtime is not installed",
)
def test_existing_video_is_reused_only_after_ffprobe_verification(tmp_path):
    """A completed video is reused only when FFprobe still verifies its quality."""
    downloader = engine.MediaDownloader(engine.DownloaderConfig(cookie_browser=None))
    saved = make_real_mp4(tmp_path, 720, 1280, color="blue", name="saved.mp4")
    asset = RemoteAsset(
        [MEDIA + "?high"], index=1, width=720, height=1280,
        format_id="kuaishou-high", video_codec="h264",
    )
    record = downloader._kuaishou_completion_record(
        saved, asset, "3xvideo1", "video", 1, should_cancel=lambda: False,
    )
    reused = downloader._existing_kuaishou_video_asset(
        record, tmp_path, asset, should_cancel=lambda: False
    )
    assert reused is not None
    assert reused[0] == saved.resolve()
    assert (reused[1].width, reused[1].height) == (720, 1280)

    # A truncated file must not be trusted even though it still exists. Cut it to
    # a third of its real size so the container is genuinely corrupt.
    truncated = make_real_mp4(tmp_path, 720, 1280, color="green", name="broken.mp4")
    truncated.write_bytes(truncated.read_bytes()[: truncated.stat().st_size // 3])
    assert downloader._existing_kuaishou_video_asset(
        downloader._kuaishou_completion_record(
            truncated, asset, "3xvideo1", "video", 1, should_cancel=lambda: False,
        ),
        tmp_path,
        asset,
        should_cancel=lambda: False,
    ) is None

    # A file below the declared resolution must be re-downloaded, never reused.
    lower = make_real_mp4(tmp_path, 360, 640, color="red", name="lower.mp4")
    assert downloader._existing_kuaishou_video_asset(
        downloader._kuaishou_completion_record(
            lower, asset, "3xvideo1", "video", 1, should_cancel=lambda: False,
        ),
        tmp_path,
        asset,
        should_cancel=lambda: False,
    ) is None

    # A file outside the output directory, a missing file and no record at all
    # must each fall back to a fresh download.
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    moved = outside / "saved.mp4"
    shutil.copy(saved, moved)
    for candidate in (
        {**record, "path": str(moved)},
        {**record, "path": str(tmp_path / "missing.mp4")},
        {**record, "path": ""},
        None,
    ):
        assert downloader._existing_kuaishou_video_asset(
            candidate, tmp_path, asset, should_cancel=lambda: False
        ) is None


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="The optional local FFmpeg runtime is not installed",
)
def test_completed_video_is_not_downloaded_again_after_restart(
    monkeypatch, tmp_path
):
    """A crash after the video landed must not re-download the same work."""
    # feed() declares duration=5000ms, and reuse re-runs the same FFprobe gate a
    # fresh download must pass, so the saved file has to match that duration.
    saved = make_real_mp4(
        tmp_path, 720, 1280, color="blue", seconds=5.0, name="saved.mp4"
    )
    saved = saved.rename(tmp_path / "2026-09-19-Fixture video.mp4")

    downloader, item = discovered_item(monkeypatch)
    item.metadata[KUAISHOU_SAVED_ASSETS_KEY] = [
        downloader._kuaishou_completion_record(
            saved, ks.parse_video(feed()).assets[0], "3xvideo1", "video", 1,
            should_cancel=lambda: False,
        )
    ]
    requests = []

    def opener(ydl, request, *, is_trusted_url):
        requests.append(request.url)
        raise AssertionError("A completed video must not be fetched again")

    monkeypatch.setattr(engine, "_open_xiaohongshu_response", opener)
    outcome = downloader.download_item(
        item,
        Platform.KUAISHOU,
        tmp_path,
        callback=None,
        should_cancel=lambda: False,
    )
    assert requests == []
    assert outcome.output_paths == [str(saved)]
    assert outcome.media_type == MediaType.VIDEO
    assert outcome.resolution == "720x1280"
    # Reusing the file must not create a second copy under a new name.
    assert [path.name for path in tmp_path.glob("*.mp4")] == [
        "2026-09-19-Fixture video.mp4"
    ]


def test_video_completion_record_is_reported_with_its_media_kind(
    monkeypatch, tmp_path
):
    """The video path must emit a persisted record, not only an output path."""
    downloader, item = discovered_item(monkeypatch)
    events = []

    def transfer(ydl, assets, *args, **kwargs):
        (tmp_path / "one.mp4").write_bytes(b"x" * 4321)
        return tmp_path / "one.mp4", RemoteAsset(
            [MEDIA], 1, width=720, height=1280, format_id="kuaishou-high",
            size=4321,
        )

    monkeypatch.setattr(downloader, "_download_first_available_asset", transfer)
    downloader.download_item(
        item,
        Platform.KUAISHOU,
        tmp_path,
        callback=events.append,
        should_cancel=lambda: False,
    )
    completed = [event for event in events if event.event == "asset_completed"]
    assert len(completed) == 1
    records = completed[0].asset_records
    assert len(records) == 1
    assert records[0]["media_kind"] == "video"
    assert records[0]["index"] == 1
    assert records[0]["media_id"] == "3xvideo1"
    assert records[0]["size"] == 4321
    assert MEDIA not in json.dumps(records)


def test_video_record_survives_persistence_round_trip(tmp_path):
    """A video record must stay whitelisted and readable after a restart."""
    state_dir = tmp_path / "state"
    manager = DownloadManager(
        state_dir=state_dir, default_output_root=tmp_path / "downloads", max_workers=1
    )
    try:
        job = manager.create_job(VIDEO, cookie_browser=None, auto_start=False)
        item = DownloadItem(
            id="kuaishou-3xvideo1",
            media_id="3xvideo1",
            source_url=VIDEO,
            title="Fixture video",
            media_type=MediaType.VIDEO,
            metadata={"kuaishou_author_id": "3xowner1"},
        )
        with manager._lock:
            job = manager._require_job(job.id)
            job.items.append(item)
            manager.store.save(job)
        manager._on_engine_event(
            job.id,
            item.id,
            EngineEvent(
                event="asset_completed",
                output_paths=[str(tmp_path / "2026-09-19-Fixture video.mp4")],
                asset_records=[
                    {
                        "media_id": "3xvideo1",
                        "index": 1,
                        "media_kind": "video",
                        "path": str(tmp_path / "2026-09-19-Fixture video.mp4"),
                        "width": 720,
                        "height": 1280,
                        "candidates": [MEDIA + "?high"],
                    }
                ],
            ),
        )
        job_id = job.id
    finally:
        manager.shutdown()

    persisted = JsonJobStore(state_dir).get(job_id)
    records = persisted.items[0].metadata[KUAISHOU_SAVED_ASSETS_KEY]
    assert len(records) == 1
    assert records[0]["media_kind"] == "video"
    assert "candidates" not in records[0]
    assert MEDIA not in json.dumps(records)


def test_image_record_is_not_reused_by_the_video_path(monkeypatch, tmp_path):
    """A media-kind mismatch must force a fresh download, never a wrong reuse."""
    downloader = engine.MediaDownloader(engine.DownloaderConfig(cookie_browser=None))
    asset = RemoteAsset(
        [MEDIA + "?high"], index=1, width=720, height=1280, format_id="kuaishou-high"
    )
    item = DownloadItem(
        id="fixture",
        media_id="3xvideo1",
        source_url=VIDEO,
        metadata={
            KUAISHOU_SAVED_ASSETS_KEY: [
                {
                    "media_id": "3xvideo1",
                    "index": 1,
                    "media_kind": "image",
                    "path": str(tmp_path / "not-a-video.jpg"),
                }
            ]
        },
    )
    assert downloader._kuaishou_saved_asset_records(
        item, "3xvideo1", "video"
    ) == {}
    assert downloader._existing_kuaishou_video_asset(
        None, tmp_path, asset, should_cancel=lambda: False
    ) is None


def test_album_records_are_rejected_when_identity_or_position_is_missing():
    """R4: a record must bind the work, position and media kind, never a bare name."""
    valid = {
        "media_id": "3xalbum1",
        "index": 2,
        "media_kind": "image",
        "path": "/tmp/album-002.jpg",
        "width": 800,
        "height": 600,
    }
    assert _public_kuaishou_saved_asset(valid) == valid
    assert _public_kuaishou_saved_asset({**valid, "media_id": ""}) is None
    assert _public_kuaishou_saved_asset({**valid, "index": 0}) is None
    assert _public_kuaishou_saved_asset({**valid, "path": ""}) is None
    assert _public_kuaishou_saved_asset({**valid, "index": True}) is None
    assert _public_kuaishou_saved_asset("not-a-record") is None
    assert _public_kuaishou_saved_asset({**valid, "candidates": [MEDIA]}) == valid


def test_saved_asset_records_require_a_known_media_kind():
    """A record must declare video or image; anything else is rejected outright."""
    valid = {
        "media_id": "3xwork1",
        "index": 1,
        "media_kind": "image",
        "path": "/tmp/a.jpg",
    }
    assert _public_kuaishou_saved_asset(valid) == valid
    assert _public_kuaishou_saved_asset({**valid, "media_kind": "video"}) is not None
    assert _public_kuaishou_saved_asset({**valid, "media_kind": "gif"}) is None
    assert _public_kuaishou_saved_asset({**valid, "media_kind": ""}) is None
    # A missing media_kind is rejected too, so a record without one is never
    # treated as valid.
    without_kind = {k: v for k, v in valid.items() if k != "media_kind"}
    assert _public_kuaishou_saved_asset(without_kind) is None
    assert _public_kuaishou_saved_asset({**valid, "media_kind": 123}) is None


def discovered_album_item(monkeypatch, photo_urls, *, media_id="3xalbum1"):
    result = ks.Result(
        [ks.parse_video(album_feed(media_id, photo_urls))], "item", media_id
    )
    monkeypatch.setattr(engine, "discover_kuaishou", lambda *a, **k: result)
    downloader = engine.MediaDownloader(engine.DownloaderConfig(cookie_browser=None))
    url = f"https://www.kuaishou.com/short-video/{media_id}"
    item = downloader.discover(url, Platform.KUAISHOU, SourceKind.ITEM).items[0]
    return downloader, item


@pytest.mark.skipif(
    not shutil.which("ffmpeg"),
    reason="The optional local FFmpeg runtime is not installed",
)
def test_album_commits_each_image_before_the_next_one_starts(
    monkeypatch, tmp_path
):
    """R4: image one is recorded even though image two fails."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    first = make_real_jpeg(source_dir, 800, 600, color="red", name="first.jpg")
    first_payload = first.read_bytes()
    downloader, item = discovered_album_item(
        monkeypatch,
        [
            {"url": MEDIA + "?first", "width": 800, "height": 600},
            {"url": MEDIA + "?second", "width": 1600, "height": 1200},
        ],
    )
    assert item.media_type == MediaType.IMAGE
    events = []

    def opener(ydl, request, *, is_trusted_url):
        if request.url.endswith("?second"):
            raise urllib.error.HTTPError(
                request.url, 503, "unavailable", {}, None
            )
        response = io.BytesIO(first_payload)
        response.headers = {
            "Content-Type": "image/jpeg",
            "Content-Length": str(len(first_payload)),
        }
        response.url = request.url
        return response

    monkeypatch.setattr(engine, "_open_xiaohongshu_response", opener)
    with pytest.raises(MediaDownloadError, match=r"Image 2 failed|highest-available"):
        downloader.download_item(
            item,
            Platform.KUAISHOU,
            output_dir,
            callback=events.append,
            should_cancel=lambda: False,
        )
    completed = [event for event in events if event.event == "asset_completed"]
    assert len(completed) == 1
    assert completed[0].output_paths == [
        str(output_dir / "2026-09-19-Fixture album-001.jpg")
    ]
    assert [record["index"] for record in completed[0].asset_records] == [1]
    assert completed[0].asset_records[0]["media_id"] == "3xalbum1"
    assert MEDIA not in json.dumps(completed[0].asset_records)
    saved = list(output_dir.glob("*.jpg"))
    assert [path.name for path in saved] == ["2026-09-19-Fixture album-001.jpg"]
    assert not list(output_dir.glob("*.part"))


@pytest.mark.skipif(
    not shutil.which("ffmpeg"),
    reason="The optional local FFmpeg runtime is not installed",
)
def test_album_cancel_before_second_image_keeps_first_and_leaves_no_partial(
    monkeypatch, tmp_path
):
    """R4: cancelling before image two keeps image one and leaves no partial file."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    first = make_real_jpeg(source_dir, 800, 600, color="red", name="first.jpg")
    first_payload = first.read_bytes()
    downloader, item = discovered_album_item(
        monkeypatch,
        [
            {"url": MEDIA + "?first", "width": 800, "height": 600},
            {"url": MEDIA + "?second", "width": 1600, "height": 1200},
        ],
    )
    events = []
    started_second = threading.Event()

    def opener(ydl, request, *, is_trusted_url):
        if request.url.endswith("?second"):
            started_second.set()
            raise DownloadCancelledError("Task cancelled")
        response = io.BytesIO(first_payload)
        response.headers = {
            "Content-Type": "image/jpeg",
            "Content-Length": str(len(first_payload)),
        }
        response.url = request.url
        return response

    monkeypatch.setattr(engine, "_open_xiaohongshu_response", opener)
    with pytest.raises(DownloadCancelledError):
        downloader.download_item(
            item,
            Platform.KUAISHOU,
            output_dir,
            callback=events.append,
            should_cancel=lambda: False,
        )
    assert started_second.is_set()
    completed = [event for event in events if event.event == "asset_completed"]
    assert len(completed) == 1
    assert [record["index"] for record in completed[0].asset_records] == [1]
    assert [path.name for path in output_dir.glob("*.jpg")] == [
        "2026-09-19-Fixture album-001.jpg"
    ]
    assert not list(output_dir.glob("*.part"))
    assert not list(output_dir.glob(".*"))


@pytest.mark.skipif(
    not shutil.which("ffmpeg"),
    reason="The optional local FFmpeg runtime is not installed",
)
def test_album_records_survive_restart_and_are_reused_not_renamed(
    monkeypatch, tmp_path
):
    """R4: persisted records let a restart reuse image one without a new suffix."""
    output_dir = tmp_path / "Kuaishou" / "Fixture Author"
    output_dir.mkdir(parents=True)
    saved = make_real_jpeg(output_dir, 800, 600, color="red", name="saved.jpg")
    saved = saved.rename(output_dir / "2026-09-19-Fixture album-001.jpg")

    state_dir = tmp_path / "state"
    manager = DownloadManager(
        state_dir=state_dir, default_output_root=tmp_path / "downloads", max_workers=1
    )
    try:
        job = manager.create_job(
            "https://www.kuaishou.com/short-video/3xalbum1",
            cookie_browser=None,
            auto_start=False,
        )
        item = DownloadItem(
            id="kuaishou-3xalbum1",
            media_id="3xalbum1",
            source_url="https://www.kuaishou.com/short-video/3xalbum1",
            title="Fixture album",
            upload_date="2026-09-19",
            author="Fixture Author",
            media_type=MediaType.IMAGE,
            metadata={"kuaishou_author_id": "3xowner1"},
        )
        with manager._lock:
            job = manager._require_job(job.id)
            job.items.append(item)
            manager.store.save(job)
        manager._on_engine_event(
            job.id,
            item.id,
            EngineEvent(
                event="asset_completed",
                output_paths=[str(saved)],
                asset_records=[
                    {
                        "media_id": "3xalbum1",
                        "index": 1,
                        "media_kind": "image",
                        "path": str(saved),
                        "width": 800,
                        "height": 600,
                        "candidates": [MEDIA + "?first"],
                        **engine.MediaDownloader._kuaishou_completion_record(
                            saved, RemoteAsset([MEDIA + "?first"], 1, width=800, height=600),
                            "3xalbum1", "image", 1, should_cancel=lambda: False,
                        ),
                    }
                ],
            ),
        )
    finally:
        manager.shutdown()

    persisted = JsonJobStore(state_dir).get(job.id)
    records = persisted.items[0].metadata[KUAISHOU_SAVED_ASSETS_KEY]
    assert [record["index"] for record in records] == [1]
    assert records[0]["path"] == str(saved)
    assert records[0]["media_kind"] == "image"
    assert "candidates" not in records[0]
    assert MEDIA not in json.dumps(records)

    restarted = DownloadManager(
        state_dir=state_dir, default_output_root=tmp_path / "downloads", max_workers=1
    )
    try:
        restored = restarted.get_job(job.id)
        assert restored.items[0].output_paths == [str(saved)]
        restored_records = restored.items[0].metadata[KUAISHOU_SAVED_ASSETS_KEY]
        assert restored_records[0]["index"] == 1
    finally:
        restarted.shutdown()

    second = make_real_jpeg(tmp_path, 1600, 1200, color="blue", name="second.jpg")
    second_payload = second.read_bytes()
    downloader, item = discovered_album_item(
        monkeypatch,
        [
            {"url": MEDIA + "?first", "width": 800, "height": 600},
            {"url": MEDIA + "?second", "width": 1600, "height": 1200},
        ],
    )
    item.metadata[KUAISHOU_SAVED_ASSETS_KEY] = restored_records
    requests = []

    def opener(ydl, request, *, is_trusted_url):
        requests.append(request.url)
        response = io.BytesIO(second_payload)
        response.headers = {
            "Content-Type": "image/jpeg",
            "Content-Length": str(len(second_payload)),
        }
        response.url = request.url
        return response

    monkeypatch.setattr(engine, "_open_xiaohongshu_response", opener)
    outcome = downloader.download_item(
        item,
        Platform.KUAISHOU,
        output_dir,
        callback=None,
        should_cancel=lambda: False,
    )
    assert requests == [MEDIA + "?second"]
    assert outcome.output_paths[0] == str(saved)
    assert sorted(path.name for path in output_dir.glob("*.jpg")) == [
        "2026-09-19-Fixture album-001.jpg",
        "2026-09-19-Fixture album-002.jpg",
    ]


def test_album_records_are_not_reused_for_a_different_work(monkeypatch, tmp_path):
    """R4: a record from another work or position must not be reused."""
    downloader = engine.MediaDownloader(engine.DownloaderConfig(cookie_browser=None))
    item = DownloadItem(
        id="fixture",
        media_id="3xalbum1",
        source_url="https://www.kuaishou.com/short-video/3xalbum1",
        metadata={
            KUAISHOU_SAVED_ASSETS_KEY: [
                {
                    "media_id": "3xother",
                    "index": 1,
                    "media_kind": "image",
                    "path": "/tmp/other.jpg",
                },
                {
                    "media_id": "3xalbum1",
                    "index": 2,
                    "media_kind": "image",
                    "path": "/tmp/second.jpg",
                },
                "corrupt",
            ]
        },
    )
    records = downloader._kuaishou_saved_asset_records(item, "3xalbum1", "image")
    # Another work's record and a corrupt entry must never leak into this work.
    assert list(records) == [2]
    assert records[2]["path"] == "/tmp/second.jpg"
    assert 1 not in records
    assert downloader._kuaishou_saved_asset_records(item, "3xunknown", "image") == {}
    assert downloader._kuaishou_saved_asset_records(
        DownloadItem(
            id="bare",
            media_id="3xalbum1",
            source_url="https://www.kuaishou.com/short-video/3xalbum1",
            metadata={},
        ),
        "3xalbum1",
        "image",
    ) == {}


def test_saved_asset_records_do_not_mix_video_and_image_kinds():
    """A video record must never be reused as an album image, or vice versa."""
    downloader = engine.MediaDownloader(engine.DownloaderConfig(cookie_browser=None))
    item = DownloadItem(
        id="fixture",
        media_id="3xwork1",
        source_url="https://www.kuaishou.com/short-video/3xwork1",
        metadata={
            KUAISHOU_SAVED_ASSETS_KEY: [
                {
                    "media_id": "3xwork1",
                    "index": 1,
                    "media_kind": "video",
                    "path": "/tmp/work.mp4",
                },
                {
                    "media_id": "3xwork1",
                    "index": 1,
                    "media_kind": "image",
                    "path": "/tmp/work-001.jpg",
                },
            ]
        },
    )
    videos = downloader._kuaishou_saved_asset_records(item, "3xwork1", "video")
    images = downloader._kuaishou_saved_asset_records(item, "3xwork1", "image")
    assert videos[1]["path"] == "/tmp/work.mp4"
    assert images[1]["path"] == "/tmp/work-001.jpg"
    assert downloader._kuaishou_saved_asset_records(item, "3xwork1", "gif") == {}


def kuaishou_item(media_id, *, status=ItemStatus.QUEUED, **kwargs):
    metadata = {"kuaishou_author_id": "3xowner1", **kwargs.pop("metadata", {})}
    return DownloadItem(
        id=f"kuaishou-{media_id}",
        media_id=media_id,
        source_url=f"https://www.kuaishou.com/short-video/{media_id}",
        title="Fixture video",
        author="Fixture Author",
        status=status,
        metadata=metadata,
        **kwargs,
    )


def test_incomplete_rediscovery_keeps_queued_kuaishou_works():
    """A rate-limited pass must not discard works the site simply did not return."""
    previous = [
        kuaishou_item("3xvideo1", status=ItemStatus.QUEUED),
        kuaishou_item("3xvideo2", status=ItemStatus.QUEUED),
    ]
    # The interrupted pass only reached the first work again.
    discovered = [kuaishou_item("3xvideo1")]
    merged = DownloadManager._merge_discovered_items(
        previous, discovered, preserve_unmatched_items=True
    )
    assert [item.media_id for item in merged] == ["3xvideo1", "3xvideo2"]
    unmatched = next(item for item in merged if item.media_id == "3xvideo2")
    assert unmatched.status == ItemStatus.QUEUED
    assert unmatched.error is None
    assert unmatched.retryable is True


def test_complete_rediscovery_still_retires_missing_kuaishou_works():
    """Once the site confirms the end, a missing work is genuinely gone."""
    previous = [kuaishou_item("3xvideo1", status=ItemStatus.QUEUED)]
    merged = DownloadManager._merge_discovered_items(
        previous, [], preserve_unmatched_items=False
    )
    assert len(merged) == 1
    assert merged[0].status == ItemStatus.FAILED
    assert merged[0].error == "Item was not found when the profile was refreshed"


def test_completed_kuaishou_work_is_kept_and_not_redownloaded():
    """A finished work survives rediscovery with its output intact."""
    previous = [
        kuaishou_item(
            "3xvideo1",
            status=ItemStatus.COMPLETED,
            output_paths=["/downloads/Kuaishou/Author/2026-09-19-Fixture.mp4"],
            selected_format="kuaishou-high",
            resolution="720x1280",
        )
    ]
    discovered = [kuaishou_item("3xvideo1")]
    merged = DownloadManager._merge_discovered_items(
        previous, discovered, preserve_unmatched_items=True
    )
    assert len(merged) == 1
    assert merged[0].status == ItemStatus.COMPLETED
    assert merged[0].output_paths == [
        "/downloads/Kuaishou/Author/2026-09-19-Fixture.mp4"
    ]
    assert merged[0].resolution == "720x1280"


def test_failed_kuaishou_work_stays_retryable_after_rediscovery():
    """A failed work keeps its retryable state so the pass can try it again."""
    previous = [
        kuaishou_item(
            "3xvideo1",
            status=ItemStatus.FAILED,
            error="All highest-available media URLs failed",
        )
    ]
    discovered = [kuaishou_item("3xvideo1")]
    merged = DownloadManager._merge_discovered_items(
        previous, discovered, preserve_unmatched_items=True
    )
    assert len(merged) == 1
    assert merged[0].retryable is True


def test_album_records_survive_rediscovery_through_metadata_merge():
    """Persisted album records must not be lost when the profile is rewalked."""
    previous = [
        kuaishou_item(
            "3xalbum1",
            status=ItemStatus.FAILED,
            media_type=MediaType.IMAGE,
            metadata={
                "kuaishou_author_id": "3xowner1",
                KUAISHOU_SAVED_ASSETS_KEY: [
                    {
                        "media_id": "3xalbum1",
                        "index": 1,
                        "media_kind": "image",
                        "path": "/downloads/Kuaishou/Author/2026-09-19-Fixture-001.jpg",
                        "width": 800,
                        "height": 600,
                    }
                ],
            },
        )
    ]
    discovered = [
        DownloadItem(
            id="kuaishou-3xalbum1",
            media_id="3xalbum1",
            source_url="https://www.kuaishou.com/short-video/3xalbum1",
            media_type=MediaType.IMAGE,
            metadata={"kuaishou_author_id": "3xowner1"},
        )
    ]
    merged = DownloadManager._merge_discovered_items(
        previous, discovered, preserve_unmatched_items=True
    )
    records = merged[0].metadata[KUAISHOU_SAVED_ASSETS_KEY]
    assert [record["index"] for record in records] == [1]
    assert records[0]["media_kind"] == "image"
    assert records[0]["path"].endswith("2026-09-19-Fixture-001.jpg")


def test_preserve_unmatched_defaults_to_the_previous_behaviour():
    """Other platforms are unaffected: the new flag defaults to off."""
    previous = [
        DownloadItem(
            id="xhs-1",
            media_id="note1",
            source_url="https://www.xiaohongshu.com/explore/note1",
            status=ItemStatus.QUEUED,
        )
    ]
    merged = DownloadManager._merge_discovered_items(previous, [])
    assert merged[0].status == ItemStatus.FAILED


def test_incomplete_profile_job_is_rediscovered_on_retry(tmp_path):
    """An interrupted profile task resumes through rediscovery, not a fresh walk."""
    job = DownloadJob(
        id="fixture",
        source_url=PROFILE,
        platform=Platform.KUAISHOU,
        source_kind=SourceKind.PROFILE,
        output_root=str(tmp_path),
        status=JobStatus.INTERRUPTED,
        discovery_complete=False,
    )
    assert DownloadManager._should_rediscover_on_retry(job)

    complete_job = DownloadJob(
        id="fixture2",
        source_url=PROFILE,
        platform=Platform.KUAISHOU,
        source_kind=SourceKind.PROFILE,
        output_root=str(tmp_path),
        status=JobStatus.COMPLETED,
        discovery_complete=True,
    )
    complete_job.items.append(kuaishou_item("3xvideo1", status=ItemStatus.COMPLETED))
    assert not DownloadManager._should_rediscover_on_retry(complete_job)


def test_media_transfer_uses_guarded_redirect_not_unchecked_urlopen(
    monkeypatch, tmp_path
):
    downloader = engine.MediaDownloader(engine.DownloaderConfig(cookie_browser=None))
    calls = []

    def guarded(ydl, request, *, is_trusted_url):
        assert is_trusted_url(MEDIA)
        assert not is_trusted_url("https://127.0.0.1/private")
        calls.append(request.url)
        raise _XiaohongshuRedirectRejected("untrusted-url")

    monkeypatch.setattr(engine, "_open_xiaohongshu_response", guarded)
    ydl = SimpleNamespace(
        urlopen=Mock(side_effect=AssertionError("Unchecked transfer"))
    )
    with pytest.raises(TemporaryAccessError, match="redirect was blocked") as captured:
        downloader._download_first_available_asset(
            ydl,
            [RemoteAsset([MEDIA, "https://v2.kwaicdn.com/backup.mp4"], 1)],
            tmp_path,
            None,
            "Fixture",
            "3xvideo1",
            VIDEO,
            platform=Platform.KUAISHOU,
            media_type=MediaType.VIDEO,
            callback=None,
            should_cancel=lambda: False,
            verify_declared_dimensions=True,
        )
    assert captured.value.issue_code == SiteIssueCode.SECURITY_BLOCKED
    assert calls == [MEDIA]
    assert not list(tmp_path.iterdir())


def test_media_transfer_cancel_preserves_no_partial_file(monkeypatch, tmp_path):
    downloader = engine.MediaDownloader(engine.DownloaderConfig(cookie_browser=None))
    response = io.BytesIO(b"\0\0\0\x18ftypmp42" + b"\0" * 100)
    response.headers = {"Content-Type": "video/mp4"}
    response.url = MEDIA
    monkeypatch.setattr(
        engine, "_open_xiaohongshu_response", lambda *args, **kwargs: response
    )
    cancelled = False

    def callback(event):
        nonlocal cancelled
        if event.event == "downloading":
            cancelled = True

    with pytest.raises(DownloadCancelledError):
        downloader._download_first_available_asset(
            SimpleNamespace(),
            [RemoteAsset([MEDIA], 1)],
            tmp_path,
            None,
            "Fixture",
            "3xvideo1",
            VIDEO,
            platform=Platform.KUAISHOU,
            media_type=MediaType.VIDEO,
            callback=callback,
            should_cancel=lambda: cancelled,
            verify_declared_dimensions=True,
        )
    assert response.closed and not list(tmp_path.iterdir())


def test_kuaishou_output_path_uses_date_title_and_collision_id(tmp_path):
    downloader = engine.MediaDownloader(engine.DownloaderConfig(cookie_browser=None))
    first = downloader._xhs_output_path(
        tmp_path,
        "2026-09-19",
        "#平底鞋给不了高跟鞋的优雅 #家纺人聊睡眠",
        "3xvideo1",
        "mp4",
        None,
        Platform.KUAISHOU,
    )
    assert first.name == "2026-09-19-#平底鞋给不了高跟鞋的优雅 #家纺人聊睡眠.mp4"
    first.write_bytes(b"existing")
    second = downloader._xhs_output_path(
        tmp_path,
        "2026-09-19",
        "#平底鞋给不了高跟鞋的优雅 #家纺人聊睡眠",
        "3xvideo1",
        "mp4",
        None,
        Platform.KUAISHOU,
    )
    assert second.name == "2026-09-19-#平底鞋给不了高跟鞋的优雅 #家纺人聊睡眠 [3xvideo1].mp4"


def test_kuaishou_output_directory_is_separate(tmp_path):
    """R7: verify the real production path calculation, not two hand-written paths."""
    root = tmp_path / "downloads"
    kuaishou = engine.platform_output_directory(
        Platform.KUAISHOU, root, "Fixture Author"
    )
    other = engine.platform_output_directory(Platform.DOUYIN, root, "Fixture Author")
    assert kuaishou == root / "Kuaishou" / "Fixture Author"
    assert other == root / "Fixture Author"
    assert kuaishou != other


@pytest.mark.parametrize(
    "author", ["../../escape", "/absolute/name", "..", "a/b\\c", "  "]
)
def test_output_directory_cannot_escape_the_author_folder(tmp_path, author):
    """R7: a crafted author name must stay inside the platform folder."""
    root = tmp_path / "downloads"
    resolved = engine.platform_output_directory(
        Platform.KUAISHOU, root, author
    ).resolve()
    assert (root / "Kuaishou").resolve() in resolved.parents
    assert not str(resolved).startswith(str(tmp_path / "escape"))


def test_task_manager_persists_the_separate_kuaishou_directory(
    monkeypatch, tmp_path
):
    """R7: the real manager computes, creates and persists the separate folder."""
    result = ks.Result([ks.parse_video(feed())], "item", "3xvideo1")
    monkeypatch.setattr(engine, "discover_kuaishou", lambda *a, **k: result)
    monkeypatch.setattr(
        engine.MediaDownloader,
        "download_item",
        lambda self, item, platform, output_dir, **kwargs: engine.DownloadOutcome(
            output_paths=[str(Path(output_dir) / "done.mp4")],
            title="Fixture video",
            author="Fixture Author",
            media_type=MediaType.VIDEO,
        ),
    )

    state_dir = tmp_path / "state"
    output_root = tmp_path / "downloads"
    manager = DownloadManager(
        state_dir=state_dir, default_output_root=output_root, max_workers=1
    )
    # Run the submitted work inline so the real discovery path is exercised.
    monkeypatch.setattr(
        manager._executor, "submit", lambda fn, *a, **k: (fn(*a, **k), object())[1]
    )
    try:
        job = manager.create_job(
            VIDEO, cookie_browser=None, output_root=output_root
        )
        expected = str(output_root / "Kuaishou" / "Fixture Author")
        assert job.output_dir == expected
        assert Path(expected).is_dir()
        assert not (output_root / "Fixture Author").exists()
        assert job.items[0].output_paths == [
            str(Path(expected) / "done.mp4")
        ]
        persisted = JsonJobStore(state_dir).get(job.id)
        assert persisted.output_dir == expected
    finally:
        manager.shutdown()

    # An older persisted job keeps its separate directory after a restart.
    restarted = DownloadManager(
        state_dir=state_dir, default_output_root=output_root, max_workers=1
    )
    try:
        assert restarted.get_job(job.id).output_dir == expected
    finally:
        restarted.shutdown()


def test_non_kuaishou_platforms_keep_their_original_directory(monkeypatch, tmp_path):
    """R7: only Kuaishou gains the extra folder; other platforms are unchanged."""
    output_root = tmp_path / "downloads"
    assert engine.platform_output_directory(
        Platform.XIAOHONGSHU, output_root, "Fixture Author"
    ) == output_root / "Fixture Author"
    assert engine.platform_output_directory(
        Platform.BILIBILI, output_root, "Fixture Author"
    ) == output_root / "Fixture Author"
    assert engine.platform_output_directory(
        Platform.YOUTUBE, output_root, "Fixture Author"
    ) == output_root / "Fixture Author"


def test_incomplete_short_profile_retries_discovery():
    job = DownloadJob(
        id="fixture",
        source_url="https://v.kuaishou.com/share123",
        platform=Platform.KUAISHOU,
        source_kind=SourceKind.SHORT_LINK,
        output_root="/tmp",
        status=JobStatus.PARTIAL,
        discovery_complete=False,
    )
    assert DownloadManager._should_rediscover_on_retry(job)


def test_public_status_strips_share_tokens_without_importing_user_state():
    source = (ROOT / "app/main.py").read_text()
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_redact_public_url"
    )
    module = ast.Module(body=[function], type_ignores=[])
    namespace = {"urlsplit": urlsplit, "urlunsplit": urlunsplit}
    # Compile only this trusted repository function, without importing main's user state.
    exec(compile(module, "redaction-test", "exec"), namespace)  # noqa: S102
    assert (
        namespace["_redact_public_url"](VIDEO + "?shareToken=private&authorId=owner")
        == VIDEO
    )


@pytest.fixture(scope="module")
def chrome_channel_available():
    """Run the local browser fixture on any host with a Chrome channel."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(
                channel="chrome", headless=True, args=["--no-proxy-server"]
            )
        except PlaywrightError as exc:
            message = str(exc)
            if (
                "Executable doesn't exist" in message
                or "Chromium distribution 'chrome' is not found" in message
            ):
                pytest.skip("The optional Chrome channel is not installed")
            raise
        else:
            browser.close()


def test_checked_route_real_chrome_blocks_redirect_before_target_request(
    chrome_channel_available,
):
    from playwright.sync_api import sync_playwright

    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            received.append(self.path)
            if self.path in {"/start", "/safe-start", "/defer-start"}:
                self.send_response(302)
                self.send_header(
                    "Location",
                    {
                        "/start": "/blocked",
                        "/safe-start": "/allowed",
                        "/defer-start": "/allowed/landing",
                    }[self.path],
                )
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                body = b"<!doctype html><title>Fixture</title>Allowed"
                if self.path == "/allowed/landing":
                    body += b"<script>window.executed=true;fetch('relative-api').then(()=>window.relativeDone=true)</script>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    errors, resolved = [], []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                channel="chrome", headless=True, args=["--no-proxy-server"]
            )
            try:
                context = browser.new_context(service_workers="block")
                page = context.new_page()

                def handler(route):
                    try:
                        final = ks.fulfill_checked_route(
                            route,
                            allowed=lambda value: (
                                value.startswith(base + "/")
                                and urlsplit(value).path != "/blocked"
                            ),
                            should_cancel=lambda: False,
                            defer_navigation_redirects=route.request.url.endswith(
                                "/defer-start"
                            ),
                        )
                        resolved.append(final)
                    except DiscoveryError as error:
                        errors.append(error)
                        route.abort()

                context.route("**/*", handler)
                page.goto(
                    base + "/safe-start", wait_until="domcontentloaded", timeout=5000
                )
                assert page.title() == "Fixture"
                assert resolved[-1] == base + "/allowed"
                assert "/allowed" in received
                page.goto(
                    base + "/defer-start", wait_until="domcontentloaded", timeout=5000
                )
                assert page.title() == "Loading"
                assert page.evaluate("() => window.executed === true") is False
                assert (
                    "/relative-api" not in received
                    and "/allowed/relative-api" not in received
                )
                page.goto(resolved[-1], wait_until="domcontentloaded", timeout=5000)
                page.wait_for_function("window.relativeDone === true", timeout=5000)
                assert "/allowed/relative-api" in received
                assert "/relative-api" not in received
                with pytest.raises(Exception, match="net::ERR_FAILED"):
                    page.goto(base + "/start", timeout=5000)
                assert errors and "/blocked" not in received
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
