from __future__ import annotations

import pytest

from app.models import Platform, SourceKind
from app.platforms import UnsupportedUrlError, extract_url, identify_url


@pytest.mark.parametrize(
    ("value", "platform", "kind"),
    [
        (
            "https://www.xiaohongshu.com/user/profile/5c99d4b30000000011015e6d",
            Platform.XIAOHONGSHU,
            SourceKind.PROFILE,
        ),
        (
            "https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9",
            Platform.XIAOHONGSHU,
            SourceKind.ITEM,
        ),
        ("https://xhslink.com/a/example", Platform.XIAOHONGSHU, SourceKind.SHORT_LINK),
        (
            "https://www.douyin.com/user/MS4wLjABAAAATEST",
            Platform.DOUYIN,
            SourceKind.PROFILE,
        ),
        (
            "https://www.douyin.com/video/7628957913016552758",
            Platform.DOUYIN,
            SourceKind.ITEM,
        ),
        ("https://v.douyin.com/example/", Platform.DOUYIN, SourceKind.SHORT_LINK),
        (
            "https://space.bilibili.com/946974/video",
            Platform.BILIBILI,
            SourceKind.PROFILE,
        ),
        (
            "https://www.bilibili.com/video/BV13x41117TL",
            Platform.BILIBILI,
            SourceKind.ITEM,
        ),
        ("https://b23.tv/example", Platform.BILIBILI, SourceKind.SHORT_LINK),
        (
            "https://www.youtube.com/@BlenderOfficial",
            Platform.YOUTUBE,
            SourceKind.PROFILE,
        ),
        (
            "https://www.youtube.com/@BlenderOfficial/videos",
            Platform.YOUTUBE,
            SourceKind.PROFILE,
        ),
        (
            "https://www.youtube.com/channel/UCSMOQeBJ2RAnuFungnQOxLg/shorts",
            Platform.YOUTUBE,
            SourceKind.PROFILE,
        ),
        (
            "https://www.youtube.com/watch?v=LXb3EKWsInQ",
            Platform.YOUTUBE,
            SourceKind.ITEM,
        ),
        (
            "https://www.youtube.com/playlist?list=PLexample",
            Platform.YOUTUBE,
            SourceKind.PLAYLIST,
        ),
        ("https://youtu.be/LXb3EKWsInQ", Platform.YOUTUBE, SourceKind.ITEM),
    ],
)
def test_identify_supported_urls(
    value: str, platform: Platform, kind: SourceKind
) -> None:
    info = identify_url(value)

    assert info.platform == platform
    assert info.kind == kind


def test_extract_url_accepts_share_text_and_removes_fragment() -> None:
    value = "Copy this: HTTPS://WWW.XIAOHONGSHU.COM/explore/6411cf99000000001300b6d9#comments。"

    assert (
        extract_url(value)
        == "https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9"
    )


@pytest.mark.parametrize(
    "value",
    [
        "not a URL",
        "ftp://www.youtube.com/video",
        "https://user:password@www.youtube.com/watch?v=LXb3EKWsInQ",
        "https://youtube.com.example.org/watch?v=LXb3EKWsInQ",
        "https://example.com/video/123",
        "https://www.xiaohongshu.com/search_result",
        "https://www.douyin.com/search/example",
        "https://www.douyin.com/note/7628957913016552758",
    ],
)
def test_rejects_invalid_or_unsupported_urls(value: str) -> None:
    with pytest.raises((UnsupportedUrlError, ValueError)):
        identify_url(value)
