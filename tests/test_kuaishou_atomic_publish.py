"""Kuaishou files must not replace another task's completed media."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.downloader import MediaDownloader
from app.errors import SiteIssueCode, TemporaryAccessError


def test_concurrent_kuaishou_publication_never_overwrites(tmp_path):
    engine = MediaDownloader()
    ready = Barrier(2)

    def publish(media_id: str, payload: bytes):
        temporary = tmp_path / f".{media_id}.part"
        temporary.write_bytes(payload)
        ready.wait(timeout=5)
        return engine._publish_kuaishou_asset(
            temporary,
            tmp_path,
            "2026-09-30",
            "Same title",
            media_id,
            "mp4",
            None,
            should_cancel=lambda: False,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(publish, "video-one", b"first media")
        second = pool.submit(publish, "video-two", b"second media")
        first_path = first.result(timeout=10)
        second_path = second.result(timeout=10)

    assert first_path != second_path
    assert first_path.read_bytes() == b"first media"
    assert second_path.read_bytes() == b"second media"
    assert not list(tmp_path.glob("*.part"))


def test_kuaishou_publication_preserves_existing_user_file(tmp_path):
    existing = tmp_path / "2026-09-30-Same title.mp4"
    existing.write_bytes(b"user media")
    temporary = tmp_path / ".verified.part"
    temporary.write_bytes(b"new media")

    path = MediaDownloader()._publish_kuaishou_asset(
        temporary,
        tmp_path,
        "2026-09-30",
        "Same title",
        "video-id",
        "mp4",
        None,
        should_cancel=lambda: False,
    )

    assert path.name == "2026-09-30-Same title [video-id].mp4"
    assert path.read_bytes() == b"new media"
    assert existing.read_bytes() == b"user media"


def test_kuaishou_publication_requires_safe_filesystem(tmp_path, monkeypatch):
    temporary = tmp_path / ".verified.part"
    temporary.write_bytes(b"verified media")

    def unavailable_link(*args):
        raise OSError("hard links unsupported")

    monkeypatch.setattr("app.downloader.os.link", unavailable_link)
    with pytest.raises(TemporaryAccessError) as captured:
        MediaDownloader()._publish_kuaishou_asset(
            temporary,
            tmp_path,
            "2026-09-30",
            "Same title",
            "video-id",
            "mp4",
            None,
            should_cancel=lambda: False,
        )

    assert captured.value.issue_code == SiteIssueCode.LOCAL_CONFIGURATION
    assert temporary.read_bytes() == b"verified media"
    assert not (tmp_path / "2026-09-30-Same title.mp4").exists()
