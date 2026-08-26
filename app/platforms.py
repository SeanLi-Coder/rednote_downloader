from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit, urlunsplit

from .models import Platform, SourceKind, UrlInfo


class UnsupportedUrlError(ValueError):
    pass


_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_MARKDOWN_URL_RE = re.compile(r"\[[^\]]*\]\((https?://[^\s)]+)\)", re.IGNORECASE)
_TRAILING_SHARE_PUNCTUATION = ").,;!?]}\u3002\uff0c\uff1b\uff01\uff1f\u3011\uff09"


def _is_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def extract_url(value: str) -> str:
    value = value.strip()
    markdown_match = _MARKDOWN_URL_RE.search(value)
    plain_match = _URL_RE.search(value)
    use_markdown = bool(
        markdown_match
        and (plain_match is None or markdown_match.start() <= plain_match.start())
    )
    match = markdown_match if use_markdown else plain_match
    if match:
        value = match.group(1 if use_markdown else 0).rstrip(
            _TRAILING_SHARE_PUNCTUATION
        )
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise UnsupportedUrlError("Please enter a complete http:// or https:// URL")
    if parsed.username or parsed.password:
        raise UnsupportedUrlError("URLs containing credentials are not accepted")
    normalized = parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=(parsed.hostname or "").lower()
        + (f":{parsed.port}" if parsed.port else ""),
        fragment="",
    )
    return urlunsplit(normalized)


def identify_url(value: str) -> UrlInfo:
    url = extract_url(value)
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path.rstrip("/") or "/"
    query = parse_qs(parsed.query)
    query_with_blanks = parse_qs(parsed.query, keep_blank_values=True)

    if _is_domain(host, "xhslink.com"):
        return UrlInfo(
            url=url, platform=Platform.XIAOHONGSHU, kind=SourceKind.SHORT_LINK
        )
    if _is_domain(host, "xiaohongshu.com"):
        if re.search(r"/user/profile/[^/]+", path):
            kind = SourceKind.PROFILE
        elif re.search(r"/(?:explore|discovery/item)/[0-9a-f]+", path, re.IGNORECASE):
            kind = SourceKind.ITEM
        else:
            raise UnsupportedUrlError("Unsupported Xiaohongshu URL")
        return UrlInfo(url=url, platform=Platform.XIAOHONGSHU, kind=kind)

    if host == "v.douyin.com":
        return UrlInfo(url=url, platform=Platform.DOUYIN, kind=SourceKind.SHORT_LINK)
    if _is_domain(host, "douyin.com"):
        user_match = re.fullmatch(r"/user/[^/]+", path)
        modal_values = query_with_blanks.get("modal_id", [])
        modal_ids = set(modal_values)
        video_match = re.fullmatch(r"/video/(\d+)", path)
        if video_match:
            media_id = video_match.group(1)
            url = f"https://www.douyin.com/video/{media_id}"
            kind = SourceKind.ITEM
        elif (
            user_match
            and len(modal_ids) == 1
            and all(re.fullmatch(r"\d+", value) for value in modal_values)
        ):
            media_id = next(iter(modal_ids))
            url = f"https://www.douyin.com/video/{media_id}"
            kind = SourceKind.ITEM
        elif user_match and "modal_id" in query_with_blanks:
            raise UnsupportedUrlError("Invalid or ambiguous Douyin modal video URL")
        elif user_match:
            kind = SourceKind.PROFILE
        elif re.search(r"/note/\d+", path):
            raise UnsupportedUrlError("Douyin image posts are not supported yet")
        else:
            raise UnsupportedUrlError("Unsupported Douyin URL")
        return UrlInfo(url=url, platform=Platform.DOUYIN, kind=kind)

    if host == "b23.tv":
        return UrlInfo(url=url, platform=Platform.BILIBILI, kind=SourceKind.SHORT_LINK)
    if _is_domain(host, "bilibili.com"):
        if host == "space.bilibili.com" and re.match(r"/\d+", path):
            kind = SourceKind.PROFILE
        elif any(
            token in path
            for token in ("/lists/", "/favlist", "/medialist/", "/channel/")
        ):
            kind = SourceKind.PLAYLIST
        else:
            kind = SourceKind.ITEM
        return UrlInfo(url=url, platform=Platform.BILIBILI, kind=kind)

    if host == "youtu.be" or _is_domain(host, "youtube.com"):
        if "list" in query or "/playlist" in path:
            kind = SourceKind.PLAYLIST
        elif re.match(
            r"/(?:@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)(?:/(?:videos|shorts|streams))?$",
            path,
        ):
            kind = SourceKind.PROFILE
        else:
            kind = SourceKind.ITEM
        return UrlInfo(url=url, platform=Platform.YOUTUBE, kind=kind)

    raise UnsupportedUrlError(
        "Only Xiaohongshu, Douyin, Bilibili, and YouTube URLs are supported"
    )
