from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "DownloadItem": (".models", "DownloadItem"),
    "DownloadJob": (".models", "DownloadJob"),
    "DownloadManager": (".task_manager", "DownloadManager"),
    "DownloaderConfig": (".downloader", "DownloaderConfig"),
    "ItemStatus": (".models", "ItemStatus"),
    "JobStatus": (".models", "JobStatus"),
    "MediaDownloader": (".downloader", "MediaDownloader"),
    "MediaType": (".models", "MediaType"),
    "Platform": (".models", "Platform"),
    "SourceKind": (".models", "SourceKind"),
    "UnsupportedUrlError": (".platforms", "UnsupportedUrlError"),
    "identify_url": (".platforms", "identify_url"),
}
__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value
