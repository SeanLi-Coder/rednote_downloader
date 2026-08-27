from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Platform(str, Enum):
    XIAOHONGSHU = "xiaohongshu"
    DOUYIN = "douyin"
    BILIBILI = "bilibili"
    YOUTUBE = "youtube"


class SourceKind(str, Enum):
    PROFILE = "profile"
    PLAYLIST = "playlist"
    ITEM = "item"
    SHORT_LINK = "short_link"


class JobStatus(str, Enum):
    QUEUED = "queued"
    DISCOVERING = "discovering"
    DOWNLOADING = "downloading"
    NEEDS_AUTH = "needs_auth"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class ItemStatus(str, Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    POSTPROCESSING = "postprocessing"
    NEEDS_AUTH = "needs_auth"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class MediaType(str, Enum):
    VIDEO = "video"
    IMAGE = "image"
    UNKNOWN = "unknown"


ACTIVE_JOB_STATUSES = {
    JobStatus.QUEUED,
    JobStatus.DISCOVERING,
    JobStatus.DOWNLOADING,
}

RETRYABLE_ITEM_STATUSES = {
    ItemStatus.FAILED,
    ItemStatus.NEEDS_AUTH,
    ItemStatus.CANCELLED,
}


class TransferProgress(BaseModel):
    model_config = ConfigDict(extra="ignore")

    downloaded_bytes: int = 0
    total_bytes: int | None = None
    percent: float | None = None
    speed_bytes_per_second: float | None = None
    eta_seconds: float | None = None
    fragment_index: int | None = None
    fragment_count: int | None = None
    filename: str | None = None


class DownloadItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    media_id: str | None = None
    source_url: str
    title: str = "Untitled"
    upload_date: str | None = None
    author: str | None = None
    extractor_key: str | None = None
    playlist_index: int | None = None
    media_type: MediaType = MediaType.UNKNOWN
    status: ItemStatus = ItemStatus.QUEUED
    progress: TransferProgress = Field(default_factory=TransferProgress)
    attempts: int = 0
    output_paths: list[str] = Field(default_factory=list)
    selected_format: str | None = None
    resolution: str | None = None
    error: str | None = None
    auth_message: str | None = None
    retryable: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class DownloadJob(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    source_url: str
    platform: Platform
    source_kind: SourceKind
    output_root: str
    resolved_source_kind: SourceKind | None = None
    resolved_source_id: str | None = None
    status: JobStatus = JobStatus.QUEUED
    author: str | None = None
    output_dir: str | None = None
    items: list[DownloadItem] = Field(default_factory=list)
    total_items: int = 0
    completed_items: int = 0
    failed_items: int = 0
    active_item_id: str | None = None
    error: str | None = None
    warning: str | None = None
    auth_message: str | None = None
    verification_url: str | None = None
    cookie_browser: str | None = "chrome"
    cookie_profile: str | None = None
    cookie_fallback_used: bool = False
    discovery_complete: bool = True
    cancel_requested: bool = False
    retryable: bool = True
    revision: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)

    def refresh_counts(self) -> None:
        self.total_items = len(self.items)
        self.completed_items = sum(
            item.status == ItemStatus.COMPLETED for item in self.items
        )
        self.failed_items = sum(
            item.status in {ItemStatus.FAILED, ItemStatus.NEEDS_AUTH}
            for item in self.items
        )

    @property
    def output_path(self) -> Path:
        return Path(self.output_dir or self.output_root)


class UrlInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: str
    platform: Platform
    kind: SourceKind


class ManagerEvent(BaseModel):
    job_id: str
    event: str
    item_id: str | None = None
    revision: int
    timestamp: datetime = Field(default_factory=utc_now)
