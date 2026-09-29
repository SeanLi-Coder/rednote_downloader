"""Synthetic album completeness regressions; no network or browser profiles."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import kuaishou as ks
from app.errors import DiscoveryError


def image(name="first", width=800, height=600):
    return {
        "url": f"https://v1.kwaicdn.com/fixture/{name}.jpg",
        "width": width,
        "height": height,
    }


def work(album):
    return {
        "photo": {"id": "fixture-work", "photoUrls": album},
        "author": {"id": "fixture-owner"},
    }


@pytest.mark.parametrize(
    "missing_member",
    [
        None,
        {},
        [],
        "https://v1.kwaicdn.com/fixture/unknown.jpg",
        {"url": "https://untrusted.invalid/image.jpg", "width": 800, "height": 600},
        [{"url": "https://untrusted.invalid/image.jpg", "width": 800, "height": 600}],
        {"width": 800, "height": 600},
        [{"width": 800, "height": 600}],
    ],
)
def test_unverifiable_album_member_never_disappears_into_complete_profile(missing_member):
    entry = work([image(), missing_member, image("last")])
    parsed = ks.parse_video(entry)
    assert parsed.assets == []

    collector = ks.ProfileCollector(
        "fixture-owner", "https://www.kuaishou.com/profile/fixture-owner"
    )
    collector.accept(
        {"result": 1, "feeds": [entry], "pcursor": "no_more"},
        owner_id="fixture-owner",
        cursor="",
    )
    assert collector.terminal
    assert not collector.complete
    assert collector.problem_count == 1
    assert not collector.videos


def test_album_limit_does_not_publish_a_truncated_group():
    parsed = ks.parse_video(work([image(f"image-{index}") for index in range(101)]))
    assert parsed.assets == []


def test_album_at_the_existing_limit_keeps_every_member_in_order():
    members = [image(f"image-{index}") for index in range(100)]
    parsed = ks.parse_video(work(members))
    assert [asset.candidates for asset in parsed.assets] == [
        [entry["url"]] for entry in members
    ]
    assert [asset.index for asset in parsed.assets] == list(range(1, 101))


@pytest.mark.parametrize("field", ["photoUrl", "photoUrls"])
def test_over_budget_variant_group_is_not_partially_selected(field):
    variants = [image(f"variant-{index}") for index in range(101)]
    entry = work([])
    entry["photo"][field] = variants if field == "photoUrl" else [variants]
    assert ks.parse_video(entry).assets == []


def test_same_image_highest_dimension_backups_remain_available():
    low = image("low", 400, 300)
    high = image("high", 1600, 1200)
    backup = image("backup", 1600, 1200)
    parsed = ks.parse_video(work([[low, high, backup, high]]))
    assert len(parsed.assets) == 1
    asset = parsed.assets[0]
    assert (asset.width, asset.height) == (1600, 1200)
    assert asset.candidates == [high["url"], backup["url"]]


def test_equal_pixel_counts_do_not_merge_incompatible_image_shapes():
    first = image("first", 800, 600)
    other_shape = image("other-shape", 1200, 400)
    backup = image("backup", 800, 600)
    parsed = ks.parse_video(work([[first, other_shape, backup]]))
    assert len(parsed.assets) == 1
    assert parsed.assets[0].candidates == [first["url"], backup["url"]]


def test_album_validation_does_not_block_a_verified_video_rendition():
    entry = work([image(), None])
    entry["photo"]["manifest"] = {
        "adaptationSet": [{"representation": [image("video", 1920, 1080)]}]
    }
    parsed = ks.parse_video(entry)
    assert parsed.media_type == "video"
    assert len(parsed.assets) == 1
    assert (parsed.assets[0].width, parsed.assets[0].height) == (1920, 1080)


def test_oversized_apollo_list_is_rejected_instead_of_becoming_a_complete_prefix(monkeypatch):
    monkeypatch.setattr(ks, "MAX_PROFILE_ITEMS", 2)
    payload = {
        "result": 1,
        "feeds": [work([image(str(index))]) for index in range(3)],
        "pcursor": "no_more",
    }
    state = {
        "ROOT_QUERY": {
            'visionProfilePhotoList({"userId":"fixture-owner","pcursor":""})': payload
        }
    }
    with pytest.raises(DiscoveryError, match="limit"):
        ks.apollo_operations(state, "visionProfilePhotoList", "userId", "fixture-owner")
