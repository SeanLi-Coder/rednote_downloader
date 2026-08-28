from __future__ import annotations

import re
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .downloader import (
    DOUYIN_ITEM_EXPANSION_MESSAGE,
    DiscoveryResult,
    DownloaderConfig,
    EngineEvent,
    MediaDownloader,
    safe_component,
    safe_external_error_message,
)
from .douyin import is_complete_profile_media_metadata
from .errors import (
    AuthenticationRequiredError,
    DiscoveryError,
    DownloadCancelledError,
    MediaDownloadError,
    TemporaryAccessError,
)
from .models import (
    ACTIVE_JOB_STATUSES,
    RETRYABLE_ITEM_STATUSES,
    DownloadItem,
    DownloadJob,
    ItemStatus,
    JobStatus,
    ManagerEvent,
    MediaType,
    Platform,
    SourceKind,
    utc_now,
)
from .platforms import identify_url
from .storage import JobNotFoundError, JsonJobStore
from .xiaohongshu import (
    xiaohongshu_note_id,
    xiaohongshu_profile_id,
)


class JobBusyError(RuntimeError):
    pass


class ItemNotFoundError(KeyError):
    pass


class ItemNotRetryableError(RuntimeError):
    pass


Listener = Callable[[ManagerEvent, DownloadJob], None]
LEGACY_DOUYIN_RESULT_ERROR = (
    "This legacy Douyin profile result must be manually reviewed; "
    "create a new task before downloading again."
)
DOUYIN_ITEM_SOURCE_ERROR = (
    "This legacy Douyin item task no longer has a verifiable original video URL. "
    "Its queued entries will not be retried; create a new task from the original "
    "video link."
)
DOUYIN_ITEM_MIGRATION_MESSAGE = (
    "This legacy Douyin item task must be rediscovered from its original video "
    "link before downloading. Existing files were preserved."
)
DOUYIN_UNVERIFIABLE_QUEUE_ERROR = (
    "This legacy Douyin task contains an unverified numeric queue and no longer "
    "has enough source metadata to continue safely. Create a new task from the "
    "original profile or video link. Existing files were preserved."
)
DOUYIN_PROFILE_REDISCOVERY_MESSAGE = (
    "This Douyin profile task contains legacy entries without complete verified "
    "author and media metadata. Unsaved placeholders were removed; "
    "retry the original profile to discover it again. Existing files were preserved."
)
DOUYIN_PROFILE_REDISCOVERY_ITEM_MARKER = "_douyin_profile_rediscovery_pending"
DOUYIN_PROFILE_REFRESH_REQUIRED_MARKER = "_douyin_profile_refresh_required"
DOUYIN_PROFILE_REMOVED_ITEM_MARKER = "_douyin_profile_removed_after_refresh"
DOUYIN_PROFILE_RETIRED_ITEM_MESSAGE = (
    "This preserved legacy entry was not returned by the refreshed Douyin profile. "
    "It was skipped and will not be downloaded again; existing files were preserved."
)
DOUYIN_PROFILE_REMOVED_PARTIAL_ITEM_MESSAGE = (
    "This partially downloaded Douyin profile entry was not returned by a complete "
    "verified profile refresh. It is no longer available for automatic retry; "
    "existing files were preserved."
)
DOUYIN_PROFILE_METADATA_ERROR = (
    "Douyin profile discovery temporarily returned incomplete verified media "
    "metadata. Retry after a short wait. No numeric placeholders were queued and "
    "Chrome verification is not required for this temporary response."
)
XIAOHONGSHU_ITEM_BINDING_ERROR = (
    "Xiaohongshu item identity or profile membership could not be verified. "
    "The untrusted or cross-wired entry was blocked before download; retry the "
    "original link to rediscover it."
)
XIAOHONGSHU_BINDING_REDISCOVERY_MARKER = (
    "_xiaohongshu_binding_rediscovery_pending"
)
LEGACY_DOUYIN_GENERIC_SIGNING_MARKER = (
    "Douyin could not create a verified signed request"
)
LEGACY_DOUYIN_GENERIC_SIGNING_MESSAGE = (
    "This task was paused by an older version after a generic Douyin signing "
    "failure. Retry the original link; Chrome verification is not required unless "
    "Douyin explicitly shows a CAPTCHA or login page."
)
LEGACY_DOUYIN_MEDIA_REDIRECT_MARKER = (
    "Douyin media request redirected to an untrusted URL"
)
LEGACY_DOUYIN_MEDIA_REDIRECT_MARKERS = (
    LEGACY_DOUYIN_MEDIA_REDIRECT_MARKER,
    "media endpoint redirected to an unrecognized Douyin CDN host",
    "Douyin media redirect could not be trusted",
)
LEGACY_DOUYIN_MEDIA_REDIRECT_MESSAGE = (
    "This task contains a Douyin media redirect failure recorded by an older "
    "version, which did not preserve the redirect hostname. Retry the original "
    "link to rediscover and verify the media with the current version. Existing "
    "files were preserved."
)
LEGACY_DOUYIN_SHORT_REDIRECT_MESSAGE = (
    "This saved Douyin short-link task contains a media redirect failure from an "
    "older version, but its original resolved target was not preserved. Create a "
    "new task from the original short link so the current version can bind and "
    "verify its target. Existing files were preserved."
)
_LEGACY_DOUYIN_MARKDOWN_ITEM_RE = re.compile(
    r"(https://www\.douyin\.com/video/(\d+))\]\("
    r"(https://www\.douyin\.com/video/(\d+))"
)


class DownloadManager:
    """Own download workers and persist every user-visible task transition."""

    def __init__(
        self,
        *,
        state_dir: str | Path = "data/state",
        default_output_root: str | Path = "downloads",
        max_workers: int = 2,
        downloader_config: DownloaderConfig | None = None,
    ) -> None:
        self.store = JsonJobStore(state_dir)
        self.default_output_root = Path(default_output_root).expanduser().resolve()
        self.default_output_root.mkdir(parents=True, exist_ok=True)
        self.downloader_config = downloader_config or DownloaderConfig()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="media-download"
        )
        self._lock = threading.RLock()
        self._jobs: dict[str, DownloadJob] = {}
        self._futures: dict[str, Future[None]] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._listeners: list[Listener] = []
        self._last_progress_save: dict[str, float] = {}
        self.load_warnings: list[str] = []
        self._restore_jobs()

    def _restore_jobs(self) -> None:
        jobs, warnings = self.store.load_all()
        self.load_warnings = warnings
        for job in jobs:
            changed = False
            legacy_douyin_result = False
            migrated_douyin_item_source = False
            migrated_incomplete_douyin_profile_queue = False
            was_quarantined_douyin_profile_queue = (
                self._is_quarantined_legacy_douyin_profile_queue(job)
            )
            recovered_legacy_source = self._legacy_douyin_markdown_item_source(
                job.source_url
            )
            try:
                normalized_source = identify_url(
                    recovered_legacy_source or job.source_url
                )
            except (TypeError, ValueError):
                normalized_source = None
            if (
                not was_quarantined_douyin_profile_queue
                and job.platform == Platform.DOUYIN
                and normalized_source
                and normalized_source.platform == Platform.DOUYIN
                and normalized_source.kind == SourceKind.ITEM
            ):
                normalized_media_id = MediaDownloader._douyin_video_id(
                    normalized_source.url
                )
                requires_item_migration = (
                    recovered_legacy_source is not None
                    or job.source_kind != SourceKind.ITEM
                    or MediaDownloader._douyin_video_id(job.source_url)
                    != normalized_media_id
                )
                source_changed = (
                    job.source_url != normalized_source.url
                    or job.source_kind != SourceKind.ITEM
                )
                if source_changed:
                    job.source_url = normalized_source.url
                    job.source_kind = SourceKind.ITEM
                    migrated_douyin_item_source = requires_item_migration
                    changed = True
                if (
                    job.verification_url is not None
                    and job.verification_url != normalized_source.url
                ):
                    job.verification_url = normalized_source.url
                    changed = True
            if self._is_xiaohongshu_direct_job(job):
                if job.source_kind == SourceKind.SHORT_LINK:
                    short_binding = self._xiaohongshu_expected_short_binding(job)
                    if short_binding and (
                        job.resolved_source_kind != short_binding[0]
                        or job.resolved_source_id != short_binding[1]
                    ):
                        job.resolved_source_kind = short_binding[0]
                        job.resolved_source_id = short_binding[1]
                        changed = True
                binding_rediscovery_pending = False
                for item in job.items:
                    if not (
                        item.metadata.get(
                            XIAOHONGSHU_BINDING_REDISCOVERY_MARKER
                        )
                        is True
                        or item.error == XIAOHONGSHU_ITEM_BINDING_ERROR
                    ):
                        continue
                    binding_rediscovery_pending = True
                    if (
                        item.metadata.get(
                            XIAOHONGSHU_BINDING_REDISCOVERY_MARKER
                        )
                        is not True
                    ):
                        item.metadata[
                            XIAOHONGSHU_BINDING_REDISCOVERY_MARKER
                        ] = True
                        changed = True
                    if not item.retryable:
                        item.retryable = True
                        changed = True
                if binding_rediscovery_pending:
                    if job.discovery_complete:
                        job.discovery_complete = False
                        changed = True
                    if not job.retryable:
                        job.retryable = True
                        changed = True
            if job.platform == Platform.DOUYIN and job.source_kind == SourceKind.PROFILE:
                for item in job.items:
                    changed |= self._refresh_douyin_profile_item_from_cache(job, item)
            legacy_media_redirect = self._has_legacy_douyin_media_redirect_error(job)
            if legacy_media_redirect and job.platform == Platform.DOUYIN:
                now = utc_now()
                if job.source_kind == SourceKind.PROFILE:
                    self._migrate_legacy_douyin_redirect_profile(job, now)
                    migrated_incomplete_douyin_profile_queue = True
                elif job.source_kind == SourceKind.ITEM:
                    job.status = JobStatus.INTERRUPTED
                    job.error = LEGACY_DOUYIN_MEDIA_REDIRECT_MESSAGE
                    job.warning = LEGACY_DOUYIN_MEDIA_REDIRECT_MESSAGE
                    job.auth_message = None
                    job.verification_url = None
                    job.active_item_id = None
                    job.cancel_requested = False
                    job.retryable = True
                    job.discovery_complete = False
                    job.finished_at = now
                    for item in job.items:
                        if not self._message_has_legacy_douyin_media_redirect(
                            item.error
                        ) and not self._message_has_legacy_douyin_media_redirect(
                            item.auth_message
                        ):
                            continue
                        item.status = ItemStatus.FAILED
                        item.error = LEGACY_DOUYIN_MEDIA_REDIRECT_MESSAGE
                        item.auth_message = None
                        item.retryable = True
                        item.metadata.pop("douyin_item_media", None)
                        item.metadata.pop("item_identity_verified", None)
                        item.updated_at = now
                elif job.source_kind == SourceKind.SHORT_LINK:
                    self._retire_legacy_douyin_redirect_short_link(job, now)
                changed = job.source_kind in {
                    SourceKind.PROFILE,
                    SourceKind.ITEM,
                    SourceKind.SHORT_LINK,
                }
            elif (
                job.platform == Platform.DOUYIN
                and job.source_kind == SourceKind.PROFILE
            ):
                changed |= self._mark_douyin_profile_redirect_refresh_required(job)
            legacy_signing_messages = (job.error or "", job.auth_message or "")
            if (
                job.platform == Platform.DOUYIN
                and job.status == JobStatus.NEEDS_AUTH
                and any(
                    LEGACY_DOUYIN_GENERIC_SIGNING_MARKER in message
                    for message in legacy_signing_messages
                )
            ):
                now = utc_now()
                job.status = JobStatus.FAILED
                job.error = LEGACY_DOUYIN_GENERIC_SIGNING_MESSAGE
                job.auth_message = None
                job.verification_url = None
                job.active_item_id = None
                job.cancel_requested = False
                job.retryable = True
                job.discovery_complete = False
                job.finished_at = now
                for item in job.items:
                    if item.status != ItemStatus.NEEDS_AUTH:
                        continue
                    item.status = ItemStatus.FAILED
                    item.error = LEGACY_DOUYIN_GENERIC_SIGNING_MESSAGE
                    item.auth_message = None
                    item.retryable = True
                    item.updated_at = now
                changed = True
            if migrated_douyin_item_source:
                now = utc_now()
                job.status = JobStatus.INTERRUPTED
                job.error = DOUYIN_ITEM_MIGRATION_MESSAGE
                job.auth_message = None
                job.verification_url = normalized_source.url
                job.active_item_id = None
                job.cancel_requested = False
                job.retryable = True
                job.discovery_complete = False
                job.finished_at = now
                for item in job.items:
                    bound, metadata_changed = self._bind_douyin_item_metadata(job, item)
                    changed |= metadata_changed
                    if not bound:
                        continue
                    item.status = ItemStatus.FAILED
                    item.error = DOUYIN_ITEM_MIGRATION_MESSAGE
                    item.auth_message = None
                    item.retryable = True
                    item.updated_at = now
                changed = True
            if self._is_incomplete_verified_douyin_profile_queue(job):
                now = utc_now()
                self._migrate_douyin_profile_for_rediscovery(job, now)
                migrated_incomplete_douyin_profile_queue = True
                changed = True
            unverified_numeric_queue = (
                self._is_unverifiable_legacy_douyin_queue(job)
            )
            previously_quarantined_numeric_queue = (
                was_quarantined_douyin_profile_queue
                or self._is_quarantined_legacy_douyin_profile_queue(job)
            )
            if unverified_numeric_queue or previously_quarantined_numeric_queue:
                now = utc_now()
                recoverable_profile_source = (
                    self._recoverable_douyin_profile_source(job)
                )
                if recoverable_profile_source is not None:
                    job.source_url = recoverable_profile_source
                    self._migrate_douyin_profile_for_rediscovery(job, now)
                    migrated_incomplete_douyin_profile_queue = True
                    changed = True
                elif unverified_numeric_queue:
                    preserved_items = [
                        item for item in job.items if item.output_paths
                    ]
                    for item in preserved_items:
                        item.status = ItemStatus.FAILED
                        item.error = DOUYIN_UNVERIFIABLE_QUEUE_ERROR
                        item.auth_message = None
                        item.retryable = False
                        item.updated_at = now
                    job.items = preserved_items
                    job.status = JobStatus.FAILED
                    job.error = DOUYIN_UNVERIFIABLE_QUEUE_ERROR
                    job.warning = DOUYIN_UNVERIFIABLE_QUEUE_ERROR
                    job.auth_message = None
                    job.verification_url = None
                    job.active_item_id = None
                    job.cancel_requested = False
                    job.retryable = False
                    job.discovery_complete = False
                    job.finished_at = now
                    changed = True
            if (
                job.platform == Platform.DOUYIN
                and job.source_kind == SourceKind.ITEM
                and (
                    normalized_source is None
                    or normalized_source.platform != Platform.DOUYIN
                    or normalized_source.kind != SourceKind.ITEM
                )
            ):
                now = utc_now()
                job.status = JobStatus.FAILED
                job.error = DOUYIN_ITEM_SOURCE_ERROR
                job.auth_message = None
                job.verification_url = None
                job.active_item_id = None
                job.cancel_requested = False
                job.retryable = False
                job.finished_at = now
                for item in job.items:
                    item.status = ItemStatus.FAILED
                    item.error = DOUYIN_ITEM_SOURCE_ERROR
                    item.retryable = False
                    item.updated_at = now
                changed = True
            target = (
                None
                if was_quarantined_douyin_profile_queue
                else self._douyin_item_target(job)
            )
            if target:
                requires_direct_rediscovery = False
                now = utc_now()
                for item in job.items:
                    had_profile_metadata = any(
                        key in item.metadata
                        for key in (
                            "profile_url",
                            "profile_owner_verified",
                            "douyin_profile_media",
                        )
                    )
                    cache_was_invalid = (
                        item.media_id == target[1]
                        and self._is_bound_douyin_item_url(
                            item.source_url,
                            target[0],
                            target[1],
                        )
                        and self._discard_invalid_douyin_item_cache(
                            item,
                            target[0],
                            target[1],
                        )
                    )
                    bound, metadata_changed = self._bind_douyin_item_metadata(job, item)
                    changed |= metadata_changed or cache_was_invalid
                    if bound and had_profile_metadata:
                        requires_direct_rediscovery = True
                    if bound and cache_was_invalid:
                        requires_direct_rediscovery = True
                    if bound and requires_direct_rediscovery:
                        item.status = ItemStatus.FAILED
                        item.error = DOUYIN_ITEM_MIGRATION_MESSAGE
                        item.auth_message = None
                        item.retryable = True
                        item.updated_at = now
                if requires_direct_rediscovery:
                    job.status = JobStatus.INTERRUPTED
                    job.error = DOUYIN_ITEM_MIGRATION_MESSAGE
                    job.auth_message = None
                    job.verification_url = target[0]
                    job.active_item_id = None
                    job.cancel_requested = False
                    job.retryable = True
                    job.discovery_complete = False
                    job.finished_at = now
                    changed = True
            if job.status in ACTIVE_JOB_STATUSES:
                job.status = JobStatus.INTERRUPTED
                job.error = "The application stopped before this task finished. Retry to continue."
                job.active_item_id = None
                job.cancel_requested = False
                job.finished_at = utc_now()
                changed = True
            for item in job.items:
                if (
                    job.platform == Platform.DOUYIN
                    and job.source_kind == SourceKind.PROFILE
                    and item.status == ItemStatus.COMPLETED
                    and not item.metadata.get("profile_owner_verified")
                ):
                    item.status = ItemStatus.FAILED
                    item.error = LEGACY_DOUYIN_RESULT_ERROR
                    item.retryable = False
                    item.updated_at = utc_now()
                    legacy_douyin_result = True
                    changed = True
                if item.status in {ItemStatus.DOWNLOADING, ItemStatus.POSTPROCESSING}:
                    item.status = ItemStatus.FAILED
                    item.error = "Interrupted when the application stopped"
                    item.retryable = True
                    item.updated_at = utc_now()
                    changed = True
            if (
                not was_quarantined_douyin_profile_queue
                and self._douyin_item_has_invalid_expansion(job)
            ):
                target = self._douyin_item_target(job)
                now = utc_now()
                if target:
                    canonical_url, expected_id = target
                    target_items = [
                        item
                        for item in job.items
                        if item.media_id == expected_id
                        and self._is_bound_douyin_item_url(
                            item.source_url,
                            canonical_url,
                            expected_id,
                        )
                        and (
                            item.output_paths
                            or not self._is_numeric_legacy_item(item)
                        )
                    ]
                    target_item_ids = {item.id for item in target_items}
                    preserved_files = [
                        item
                        for item in job.items
                        if item.output_paths and item.id not in target_item_ids
                    ]
                    job.items = target_items[:1] + preserved_files
                for item in job.items:
                    item.status = ItemStatus.FAILED
                    item.error = DOUYIN_ITEM_EXPANSION_MESSAGE
                    item.auth_message = None
                    item.retryable = True
                    item.updated_at = now
                job.status = JobStatus.INTERRUPTED
                job.error = DOUYIN_ITEM_EXPANSION_MESSAGE
                job.auth_message = None
                job.verification_url = target[0] if target else job.source_url
                job.active_item_id = None
                job.cancel_requested = False
                job.retryable = True
                job.discovery_complete = False
                job.finished_at = now
                changed = True
            changed |= self._sanitize_persisted_errors(job)
            if changed:
                job.refresh_counts()
                if legacy_douyin_result:
                    job.status = (
                        JobStatus.PARTIAL if job.completed_items else JobStatus.FAILED
                    )
                    job.retryable = False
                    job.error = LEGACY_DOUYIN_RESULT_ERROR
                    job.finished_at = utc_now()
                elif (
                    not migrated_incomplete_douyin_profile_queue
                    and job.total_items
                    and job.completed_items == job.total_items
                ):
                    if job.discovery_complete:
                        job.status = JobStatus.COMPLETED
                        job.error = None
                    else:
                        job.status = JobStatus.PARTIAL
                        job.error = job.warning or "Profile discovery may be incomplete"
                job.updated_at = utc_now()
                job.revision += 1
                self.store.save(job)
            self._jobs[job.id] = job

    def create_job(
        self,
        url: str,
        *,
        output_root: str | Path | None = None,
        cookie_browser: str | None = "chrome",
        cookie_profile: str | None = None,
        auto_start: bool = True,
    ) -> DownloadJob:
        url_info = identify_url(url)
        root = Path(output_root or self.default_output_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        job = DownloadJob(
            id=uuid.uuid4().hex,
            source_url=url_info.url,
            platform=url_info.platform,
            source_kind=url_info.kind,
            output_root=str(root),
            cookie_browser=cookie_browser,
            cookie_profile=cookie_profile,
        )
        with self._lock:
            self._jobs[job.id] = job
            self.store.save(job)
        self._notify(job, "created")
        if auto_start:
            self.start_job(job.id)
        return self.get_job(job.id)

    def start_job(self, job_id: str) -> DownloadJob:
        with self._lock:
            job = self._require_job(job_id)
            self._ensure_not_running(job_id)
            if not job.retryable:
                raise ItemNotRetryableError(job.error or "This task cannot be retried")
            if self._prepare_douyin_item_rediscovery_locked(job):
                targets = None
                rediscover = True
            elif self._has_douyin_profile_rediscovery_pending(job):
                targets = None
                rediscover = True
            elif self._has_douyin_profile_refresh_required(job):
                targets = None
                rediscover = True
            elif (
                job.platform == Platform.DOUYIN
                and job.source_kind == SourceKind.PROFILE
                and self._should_rediscover_on_retry(job)
            ):
                targets = None
                rediscover = True
            elif self._has_xiaohongshu_binding_rediscovery_pending(job):
                targets = None
                rediscover = True
            elif job.items:
                targets = [
                    item.id
                    for item in job.items
                    if item.retryable
                    and item.status
                    in {
                        ItemStatus.QUEUED,
                        ItemStatus.FAILED,
                        ItemStatus.NEEDS_AUTH,
                        ItemStatus.CANCELLED,
                    }
                ]
                for item in job.items:
                    if item.id in targets:
                        self._reset_item(item)
                rediscover = False
            else:
                targets = None
                rediscover = True
            self._submit_locked(job, targets, rediscover=rediscover)
        return self.get_job(job_id)

    def cancel_job(self, job_id: str) -> DownloadJob:
        with self._lock:
            job = self._require_job(job_id)
            if job.status in {
                JobStatus.COMPLETED,
                JobStatus.PARTIAL,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            }:
                return job.model_copy(deep=True)
            job.cancel_requested = True
            job.updated_at = utc_now()
            event = self._cancel_events.setdefault(job_id, threading.Event())
            event.set()
            future = self._futures.get(job_id)
            future_cancelled = bool(future and future.cancel())
            finalized = (
                job.status == JobStatus.NEEDS_AUTH
                or future is None
                or future.done()
                or future_cancelled
            )
            if finalized:
                self._mark_cancelled_locked(job)
                self._commit_locked(job)
            else:
                self._commit_locked(job)
        self._notify(
            self.get_job(job_id), "cancelled" if finalized else "cancel_requested"
        )
        return self.get_job(job_id)

    def retry_item(self, job_id: str, item_id: str) -> DownloadJob:
        with self._lock:
            job = self._require_job(job_id)
            self._ensure_not_running(job_id)
            if not job.retryable:
                raise ItemNotRetryableError(job.error or "This task cannot be retried")
            if self._prepare_douyin_item_rediscovery_locked(job):
                self._submit_locked(job, None, rediscover=True)
                return self.get_job(job_id)
            if self._has_douyin_profile_rediscovery_pending(job):
                self._find_item(job, item_id)
                self._submit_locked(job, None, rediscover=True)
                return self.get_job(job_id)
            if self._has_douyin_profile_refresh_required(job):
                self._find_item(job, item_id)
                self._submit_locked(job, None, rediscover=True)
                return self.get_job(job_id)
            item = self._find_item(job, item_id)
            if item.status not in RETRYABLE_ITEM_STATUSES or not item.retryable:
                raise ItemNotRetryableError(
                    f"Item {item_id} is not failed, cancelled, or waiting for authentication"
                )
            rediscover = self._should_rediscover_on_retry(job)
            if rediscover:
                requested_items = (
                    None
                    if self._has_xiaohongshu_binding_rediscovery_pending(job)
                    else [item.id]
                )
                self._submit_locked(job, requested_items, rediscover=True)
                return self.get_job(job_id)
            targets = [item_id]
            self._reset_item(item)
            self._submit_locked(job, targets, rediscover=False)
        return self.get_job(job_id)

    def retry_failed(self, job_id: str) -> DownloadJob:
        with self._lock:
            job = self._require_job(job_id)
            self._ensure_not_running(job_id)
            if not job.retryable:
                raise ItemNotRetryableError(job.error or "This task cannot be retried")
            if self._prepare_douyin_item_rediscovery_locked(job):
                self._submit_locked(job, None, rediscover=True)
                return self.get_job(job_id)
            resume_queued = job.status in {
                JobStatus.NEEDS_AUTH,
                JobStatus.INTERRUPTED,
                JobStatus.PARTIAL,
            }
            targets = [
                item.id
                for item in job.items
                if item.retryable
                and (
                    item.status in RETRYABLE_ITEM_STATUSES
                    or (resume_queued and item.status == ItemStatus.QUEUED)
                )
            ]
            rediscover = self._should_rediscover_on_retry(job)
            profile_can_continue = rediscover and all(
                item.status == ItemStatus.COMPLETED
                or item.metadata.get(DOUYIN_PROFILE_REDISCOVERY_ITEM_MARKER) is True
                or item.metadata.get(DOUYIN_PROFILE_REFRESH_REQUIRED_MARKER) is True
                for item in job.items
            )
            if job.items and not targets and not profile_can_continue:
                raise ItemNotRetryableError("This task has no failed items to retry")
            if rediscover:
                self._submit_locked(job, None, rediscover=True)
                return self.get_job(job_id)
            if not job.items and job.status in {
                JobStatus.FAILED,
                JobStatus.NEEDS_AUTH,
                JobStatus.INTERRUPTED,
                JobStatus.CANCELLED,
            }:
                self._submit_locked(job, None, rediscover=True)
            elif not targets:
                raise ItemNotRetryableError("This task has no failed items to retry")
            else:
                for item in job.items:
                    if item.id in targets:
                        self._reset_item(item)
                self._submit_locked(job, targets, rediscover=False)
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> DownloadJob:
        with self._lock:
            return self._require_job(job_id).model_copy(deep=True)

    def list_jobs(self) -> list[DownloadJob]:
        with self._lock:
            return [
                job.model_copy(deep=True)
                for job in sorted(
                    self._jobs.values(),
                    key=lambda value: value.created_at,
                    reverse=True,
                )
            ]

    def add_listener(self, listener: Listener) -> None:
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def remove_listener(self, listener: Listener) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def shutdown(self, wait: bool = True, cancel_running: bool = False) -> None:
        if cancel_running:
            with self._lock:
                job_ids = [
                    job_id
                    for job_id, future in self._futures.items()
                    if not future.done()
                ]
            for job_id in job_ids:
                self.cancel_job(job_id)
        self._executor.shutdown(wait=wait, cancel_futures=False)

    def _submit_locked(
        self,
        job: DownloadJob,
        item_ids: list[str] | None,
        *,
        rediscover: bool,
    ) -> None:
        cancel_event = threading.Event()
        self._cancel_events[job.id] = cancel_event
        job.cancel_requested = False
        job.status = JobStatus.QUEUED
        job.error = None
        job.auth_message = None
        job.verification_url = None
        job.finished_at = None
        job.updated_at = utc_now()
        if rediscover:
            job.discovery_complete = False
        self._commit_locked(job)
        future = self._executor.submit(
            self._run_job, job.id, item_ids, rediscover, cancel_event
        )
        self._futures[job.id] = future

    def _run_job(
        self,
        job_id: str,
        item_ids: list[str] | None,
        rediscover: bool,
        cancel_event: threading.Event,
    ) -> None:
        try:
            requested_item_ids = set(item_ids) if item_ids is not None else None
            requested_media_ids: set[str] = set()
            with self._lock:
                job = self._require_job(job_id)
                if requested_item_ids is not None:
                    requested_media_ids = {
                        item.media_id
                        for item in job.items
                        if item.id in requested_item_ids and item.media_id
                    }
                job.started_at = job.started_at or utc_now()
                job.finished_at = None
                job.status = (
                    JobStatus.DISCOVERING if rediscover else JobStatus.DOWNLOADING
                )
                self._commit_locked(job)
            self._notify(self.get_job(job_id), "started")

            engine = self._engine_for_job(self.get_job(job_id))
            if rediscover:
                job_snapshot = self.get_job(job_id)
                result = engine.discover(
                    job_snapshot.source_url,
                    job_snapshot.platform,
                    job_snapshot.source_kind,
                    should_cancel=cancel_event.is_set,
                )
                self._validate_discovery_result(job_snapshot, result)
                if cancel_event.is_set():
                    raise DownloadCancelledError("Task cancelled")
                with self._lock:
                    job = self._require_job(job_id)
                    job.author = result.author
                    author_folder = safe_component(
                        result.author, fallback=f"{job.platform.value}-author"
                    )
                    job.output_dir = str(Path(job.output_root) / author_folder)
                    Path(job.output_dir).mkdir(parents=True, exist_ok=True)
                    previous_items = job.items
                    if job.platform == Platform.XIAOHONGSHU:
                        for xhs_item in [*previous_items, *result.items]:
                            self._normalize_xiaohongshu_item_identity(xhs_item)
                    if (
                        job.platform == Platform.DOUYIN
                        and job.source_kind == SourceKind.ITEM
                    ):
                        expected_id = MediaDownloader._douyin_video_id(job.source_url)
                        previous_items = [
                            item
                            for item in previous_items
                            if item.media_id == expected_id
                            and self._is_bound_douyin_item_url(
                                item.source_url,
                                job.source_url,
                                expected_id,
                            )
                        ][:1]
                    elif self._has_xiaohongshu_binding_rediscovery_pending(job):
                        previous_items = self._trusted_xiaohongshu_previous_items(
                            job,
                            previous_items,
                            result.items,
                        )
                        for discovered_item in result.items:
                            discovered_item.metadata.pop(
                                XIAOHONGSHU_BINDING_REDISCOVERY_MARKER,
                                None,
                            )
                        requested_item_ids = None
                        requested_media_ids.clear()
                    if (
                        job.platform == Platform.XIAOHONGSHU
                        and job.source_kind == SourceKind.SHORT_LINK
                    ):
                        short_binding = self._xiaohongshu_short_binding_from_items(
                            result.items
                        )
                        if short_binding:
                            job.resolved_source_kind = short_binding[0]
                            job.resolved_source_id = short_binding[1]
                    job.items = self._merge_discovered_items(
                        previous_items,
                        result.items,
                        retire_missing_douyin_profile_items=(
                            job.platform == Platform.DOUYIN
                            and job.source_kind == SourceKind.PROFILE
                            and result.discovery_complete
                        ),
                    )
                    if (
                        job.platform == Platform.DOUYIN
                        and job.source_kind == SourceKind.ITEM
                    ):
                        for item in job.items:
                            self._bind_douyin_item_metadata(job, item)
                    job.cookie_fallback_used = result.cookie_fallback_used
                    job.discovery_complete = result.discovery_complete
                    job.warning = result.warning
                    job.status = JobStatus.DOWNLOADING
                    job.refresh_counts()
                    if requested_item_ids is None:
                        target_items = [
                            item
                            for item in job.items
                            if item.retryable
                            and (
                                item.status == ItemStatus.QUEUED
                                or item.status in RETRYABLE_ITEM_STATUSES
                            )
                        ]
                    else:
                        target_items = [
                            item
                            for item in job.items
                            if (
                                item.id in requested_item_ids
                                or (
                                    item.media_id
                                    and item.media_id in requested_media_ids
                                )
                            )
                            and item.retryable
                            and (
                                item.status == ItemStatus.QUEUED
                                or item.status in RETRYABLE_ITEM_STATUSES
                            )
                        ]
                    for item in target_items:
                        self._reset_item(item)
                    self._commit_locked(job)
                    item_ids = [item.id for item in target_items]
                self._notify(self.get_job(job_id), "discovered")

            job_snapshot = self.get_job(job_id)
            if not job_snapshot.output_dir:
                author_folder = safe_component(
                    job_snapshot.author,
                    fallback=f"{job_snapshot.platform.value}-author",
                )
                with self._lock:
                    job = self._require_job(job_id)
                    job.output_dir = str(Path(job.output_root) / author_folder)
                    Path(job.output_dir).mkdir(parents=True, exist_ok=True)
                    self._commit_locked(job)

            targets = set(item_ids or [])
            for item_id in list(item_ids or []):
                if cancel_event.is_set():
                    raise DownloadCancelledError("Task cancelled")
                binding_error: str | None = None
                with self._lock:
                    job = self._require_job(job_id)
                    item = self._find_item(job, item_id)
                    if item.id not in targets or item.status != ItemStatus.QUEUED:
                        continue
                    item.status = ItemStatus.DOWNLOADING
                    item.attempts += 1
                    item.error = None
                    item.auth_message = None
                    item.updated_at = utc_now()
                    job.active_item_id = item.id
                    job.status = JobStatus.DOWNLOADING
                    self._commit_locked(job)
                    item_snapshot = item.model_copy(deep=True)
                    item_snapshot.metadata["_job_id"] = job_id
                    output_dir = job.output_path
                    platform = job.platform
                    if (
                        platform == Platform.DOUYIN
                        and job.source_kind == SourceKind.PROFILE
                    ):
                        item_snapshot.metadata["profile_url"] = job.source_url
                    elif (
                        platform == Platform.DOUYIN
                        and job.source_kind == SourceKind.ITEM
                    ):
                        bound, _ = self._bind_douyin_item_metadata(
                            job,
                            item_snapshot,
                        )
                        if not bound:
                            binding_error = DOUYIN_ITEM_EXPANSION_MESSAGE
                    elif platform == Platform.XIAOHONGSHU and not (
                        self._is_bound_xiaohongshu_item(job, item_snapshot)
                    ):
                        binding_error = XIAOHONGSHU_ITEM_BINDING_ERROR
                self._notify(self.get_job(job_id), "item_started", item_id)

                try:
                    if binding_error:
                        raise MediaDownloadError(binding_error)
                    outcome = engine.download_item(
                        item_snapshot,
                        platform,
                        output_dir,
                        callback=lambda event, current=item_id: self._on_engine_event(
                            job_id, current, event
                        ),
                        should_cancel=cancel_event.is_set,
                    )
                    with self._lock:
                        job = self._require_job(job_id)
                        item = self._find_item(job, item_id)
                        item.status = ItemStatus.COMPLETED
                        item.output_paths = outcome.output_paths
                        item.title = outcome.title or item.title
                        item.upload_date = outcome.upload_date or item.upload_date
                        item.author = outcome.author or item.author
                        item.media_type = outcome.media_type or item.media_type
                        item.selected_format = outcome.selected_format
                        item.resolution = outcome.resolution
                        if (
                            job.platform == Platform.DOUYIN
                            and job.source_kind == SourceKind.PROFILE
                        ):
                            item.metadata["profile_url"] = job.source_url
                            item.metadata["profile_owner_verified"] = True
                        item.progress.percent = 100.0
                        item.error = None
                        item.updated_at = utc_now()
                        job.cookie_fallback_used |= outcome.cookie_fallback_used
                        job.active_item_id = None
                        job.refresh_counts()
                        self._commit_locked(job)
                    self._notify(self.get_job(job_id), "item_completed", item_id)
                except TemporaryAccessError as exc:
                    safe_message = safe_external_error_message(exc)
                    with self._lock:
                        job = self._require_job(job_id)
                        item = self._find_item(job, item_id)
                        cancelled = cancel_event.is_set() or job.cancel_requested
                        if cancelled:
                            self._mark_cancelled_locked(job)
                        else:
                            item.status = ItemStatus.FAILED
                            item.error = safe_message
                            item.auth_message = None
                            item.retryable = True
                            if (
                                job.platform == Platform.DOUYIN
                                and job.source_kind == SourceKind.PROFILE
                            ):
                                item.metadata[
                                    DOUYIN_PROFILE_REFRESH_REQUIRED_MARKER
                                ] = True
                                job.discovery_complete = False
                            item.updated_at = utc_now()
                            job.status = JobStatus.INTERRUPTED
                            job.error = safe_message
                            job.auth_message = None
                            job.verification_url = None
                            job.active_item_id = None
                            job.finished_at = utc_now()
                            job.retryable = True
                            job.refresh_counts()
                        self._commit_locked(job)
                    self._notify(
                        self.get_job(job_id),
                        "cancelled" if cancelled else "interrupted",
                        item_id,
                    )
                    return
                except AuthenticationRequiredError as exc:
                    with self._lock:
                        job = self._require_job(job_id)
                        item = self._find_item(job, item_id)
                        cancelled = cancel_event.is_set() or job.cancel_requested
                        if cancelled:
                            self._mark_cancelled_locked(job)
                        else:
                            safe_message = safe_external_error_message(exc)
                            item.status = ItemStatus.NEEDS_AUTH
                            item.error = safe_message
                            item.auth_message = safe_message
                            item.updated_at = utc_now()
                            job.active_item_id = None
                            job.status = JobStatus.NEEDS_AUTH
                            job.auth_message = safe_message
                            job.verification_url = self._verification_url(
                                job,
                                exc.verification_url or item.source_url,
                            )
                            job.error = safe_message
                            job.finished_at = utc_now()
                            job.refresh_counts()
                        self._commit_locked(job)
                    self._notify(
                        self.get_job(job_id),
                        "cancelled" if cancelled else "needs_auth",
                        item_id,
                    )
                    return
                except DownloadCancelledError:
                    with self._lock:
                        job = self._require_job(job_id)
                        item = self._find_item(job, item_id)
                        item.status = ItemStatus.CANCELLED
                        item.error = "Cancelled by user"
                        item.updated_at = utc_now()
                        job.active_item_id = None
                        job.refresh_counts()
                        self._commit_locked(job)
                    raise
                except Exception as exc:
                    safe_message = safe_external_error_message(exc)
                    with self._lock:
                        job = self._require_job(job_id)
                        item = self._find_item(job, item_id)
                        item.status = ItemStatus.FAILED
                        item.error = safe_message
                        item.auth_message = None
                        item.retryable = True
                        if (
                            binding_error == XIAOHONGSHU_ITEM_BINDING_ERROR
                            and self._is_xiaohongshu_direct_job(job)
                        ):
                            item.metadata[
                                XIAOHONGSHU_BINDING_REDISCOVERY_MARKER
                            ] = True
                            job.discovery_complete = False
                        item.updated_at = utc_now()
                        job.active_item_id = None
                        job.refresh_counts()
                        self._commit_locked(job)
                    self._notify(self.get_job(job_id), "item_failed", item_id)

            self._finish_job(job_id)
        except TemporaryAccessError as exc:
            safe_message = safe_external_error_message(exc)
            with self._lock:
                job = self._require_job(job_id)
                cancelled = cancel_event.is_set() or job.cancel_requested
                if cancelled:
                    self._mark_cancelled_locked(job)
                else:
                    job.status = JobStatus.FAILED
                    job.error = safe_message
                    job.auth_message = None
                    job.verification_url = None
                    job.active_item_id = None
                    job.finished_at = utc_now()
                    for item in job.items:
                        if item.status != ItemStatus.NEEDS_AUTH:
                            continue
                        item.status = ItemStatus.FAILED
                        item.error = safe_message
                        item.auth_message = None
                        item.retryable = True
                        item.updated_at = utc_now()
                    job.refresh_counts()
                self._commit_locked(job)
            self._notify(
                self.get_job(job_id), "cancelled" if cancelled else "failed"
            )
        except AuthenticationRequiredError as exc:
            with self._lock:
                job = self._require_job(job_id)
                cancelled = cancel_event.is_set() or job.cancel_requested
                if cancelled:
                    self._mark_cancelled_locked(job)
                else:
                    safe_message = safe_external_error_message(exc)
                    job.status = JobStatus.NEEDS_AUTH
                    job.error = safe_message
                    job.auth_message = safe_message
                    job.verification_url = self._verification_url(
                        job,
                        exc.verification_url or job.source_url,
                    )
                    job.active_item_id = None
                    job.finished_at = utc_now()
                self._commit_locked(job)
            self._notify(
                self.get_job(job_id), "cancelled" if cancelled else "needs_auth"
            )
        except DownloadCancelledError:
            with self._lock:
                job = self._require_job(job_id)
                self._mark_cancelled_locked(job)
                self._commit_locked(job)
            self._notify(self.get_job(job_id), "cancelled")
        except Exception as exc:
            safe_message = safe_external_error_message(exc)
            with self._lock:
                job = self._require_job(job_id)
                job.status = JobStatus.FAILED
                job.error = safe_message
                job.auth_message = None
                job.verification_url = None
                job.active_item_id = None
                job.finished_at = utc_now()
                job.refresh_counts()
                self._commit_locked(job)
            self._notify(self.get_job(job_id), "failed")

    def _on_engine_event(self, job_id: str, item_id: str, event: EngineEvent) -> None:
        persist = event.event in {
            "asset_completed",
            "completed",
            "postprocessing",
            "metadata",
            "warning",
        }
        with self._lock:
            job = self._require_job(job_id)
            item = self._find_item(job, item_id)
            if event.progress:
                item.progress = event.progress
            if event.title:
                item.title = event.title
            if event.upload_date:
                item.upload_date = event.upload_date
            if event.author:
                item.author = event.author
            if event.media_type:
                item.media_type = event.media_type
            if event.selected_format:
                item.selected_format = event.selected_format
            if event.resolution:
                item.resolution = event.resolution
            if event.output_paths:
                if event.event == "asset_completed":
                    item.output_paths = list(
                        dict.fromkeys([*item.output_paths, *event.output_paths])
                    )
                else:
                    item.output_paths = event.output_paths
            if event.event == "postprocessing":
                item.status = ItemStatus.POSTPROCESSING
            elif event.event == "downloading":
                item.status = ItemStatus.DOWNLOADING
            if event.event == "probing" and event.message:
                item.progress.filename = safe_external_error_message(event.message)
            elif event.message:
                job.warning = safe_external_error_message(event.message)
            elif event.cookie_fallback_used and not job.warning:
                job.warning = (
                    "Chrome cookies could not be read. Anonymous access was used, "
                    "so the result may be incomplete or below the highest quality."
                )
            job.cookie_fallback_used |= event.cookie_fallback_used
            item.updated_at = utc_now()
            job.updated_at = utc_now()
            job.revision += 1
            now = time.monotonic()
            progress_save_interval = (
                5.0
                if job.platform == Platform.DOUYIN
                and job.source_kind == SourceKind.PROFILE
                and len(job.items) >= 100
                else 0.5
            )
            if (
                persist
                or now - self._last_progress_save.get(job_id, 0.0)
                >= progress_save_interval
            ):
                self.store.save(job)
                self._last_progress_save[job_id] = now
            snapshot = job.model_copy(deep=True)
            listeners = list(self._listeners)
            manager_event = ManagerEvent(
                job_id=job_id,
                item_id=item_id,
                event=event.event,
                revision=job.revision,
            )
        for listener in listeners:
            try:
                listener(manager_event, snapshot)
            except Exception:
                continue

    def _finish_job(self, job_id: str) -> None:
        with self._lock:
            job = self._require_job(job_id)
            job.active_item_id = None
            job.cancel_requested = False
            job.refresh_counts()
            if (
                job.total_items
                and job.completed_items == job.total_items
                and not job.discovery_complete
            ):
                job.status = JobStatus.PARTIAL
                job.error = job.warning or "Profile discovery may be incomplete"
            elif job.total_items and job.completed_items == job.total_items:
                job.status = JobStatus.COMPLETED
                job.error = None
            elif job.completed_items and job.failed_items:
                job.status = JobStatus.PARTIAL
                job.error = f"{job.failed_items} item(s) failed"
            elif job.failed_items:
                job.status = JobStatus.FAILED
                job.error = f"{job.failed_items} item(s) failed"
            elif not job.total_items:
                job.status = JobStatus.FAILED
                job.error = "No downloadable items were found"
            else:
                job.status = JobStatus.PARTIAL
                job.error = "Some items were not processed"
            job.finished_at = utc_now()
            self._commit_locked(job)
        self._notify(self.get_job(job_id), "finished")

    @staticmethod
    def _mark_cancelled_locked(job: DownloadJob) -> None:
        now = utc_now()
        job.status = JobStatus.CANCELLED
        job.error = "Cancelled by user"
        job.auth_message = None
        job.verification_url = None
        job.active_item_id = None
        job.cancel_requested = False
        job.finished_at = now
        for item in job.items:
            if item.status in {
                ItemStatus.QUEUED,
                ItemStatus.DOWNLOADING,
                ItemStatus.POSTPROCESSING,
                ItemStatus.NEEDS_AUTH,
            }:
                item.status = ItemStatus.CANCELLED
                item.error = "Cancelled by user"
                item.auth_message = None
                item.updated_at = now
        job.refresh_counts()

    def _engine_for_job(self, job: DownloadJob) -> MediaDownloader:
        config = replace(
            self.downloader_config,
            cookie_browser=job.cookie_browser,
            cookie_profile=job.cookie_profile,
        )
        return MediaDownloader(config)

    @staticmethod
    def _legacy_douyin_markdown_item_source(value: str) -> str | None:
        match = _LEGACY_DOUYIN_MARKDOWN_ITEM_RE.fullmatch(value)
        if not match or match.group(2) != match.group(4):
            return None
        try:
            label_source = identify_url(match.group(1))
            target_source = identify_url(match.group(3))
        except (TypeError, ValueError):
            return None
        if (
            label_source.platform != Platform.DOUYIN
            or target_source.platform != Platform.DOUYIN
            or label_source.kind != SourceKind.ITEM
            or target_source.kind != SourceKind.ITEM
            or label_source.url != target_source.url
        ):
            return None
        return target_source.url

    @staticmethod
    def _douyin_item_target(job: DownloadJob) -> tuple[str, str] | None:
        if job.platform != Platform.DOUYIN:
            return None
        try:
            source = identify_url(job.source_url)
        except (TypeError, ValueError):
            return None
        if source.platform != Platform.DOUYIN or source.kind != SourceKind.ITEM:
            return None
        media_id = MediaDownloader._douyin_video_id(source.url)
        return (source.url, media_id) if media_id else None

    def _prepare_douyin_item_rediscovery_locked(self, job: DownloadJob) -> bool:
        target = self._douyin_item_target(job)
        if not target:
            if job.platform == Platform.DOUYIN and job.source_kind == SourceKind.ITEM:
                raise ItemNotRetryableError(DOUYIN_ITEM_SOURCE_ERROR)
            return False
        canonical_url, expected_id = target
        source_changed = (
            job.source_url != canonical_url or job.source_kind != SourceKind.ITEM
        )
        job.source_url = canonical_url
        job.source_kind = SourceKind.ITEM
        valid_items = [
            item
            for item in job.items
            if item.media_id == expected_id
            and self._is_bound_douyin_item_url(
                item.source_url,
                canonical_url,
                expected_id,
            )
        ]
        requires_identity_refresh = False
        for item in valid_items:
            had_profile_metadata = any(
                key in item.metadata
                for key in (
                    "profile_url",
                    "profile_owner_verified",
                    "douyin_profile_media",
                )
            )
            invalid_cache = self._discard_invalid_douyin_item_cache(
                item,
                canonical_url,
                expected_id,
            )
            self._bind_douyin_item_metadata(job, item)
            requires_identity_refresh |= had_profile_metadata or invalid_cache
        if requires_identity_refresh:
            job.discovery_complete = False
        invalid_expansion = self._douyin_item_has_invalid_expansion(job)
        if (
            not source_changed
            and not invalid_expansion
            and not requires_identity_refresh
            and job.discovery_complete
        ):
            return False
        job.items = valid_items[:1]
        for item in job.items:
            self._bind_douyin_item_metadata(job, item)
            if source_changed or invalid_expansion or requires_identity_refresh:
                item.status = ItemStatus.FAILED
                item.error = (
                    DOUYIN_ITEM_EXPANSION_MESSAGE
                    if invalid_expansion
                    else DOUYIN_ITEM_MIGRATION_MESSAGE
                )
                item.auth_message = None
                item.retryable = True
                item.updated_at = utc_now()
        job.active_item_id = None
        job.refresh_counts()
        return True

    @classmethod
    def _douyin_item_has_invalid_expansion(cls, job: DownloadJob) -> bool:
        target = cls._douyin_item_target(job)
        if not target or not job.items:
            return False
        canonical_url, expected_id = target
        valid_items = [
            item
            for item in job.items
            if item.media_id == expected_id
            and cls._is_bound_douyin_item_url(
                item.source_url,
                canonical_url,
                expected_id,
            )
        ]
        return len(job.items) != 1 or len(valid_items) != 1

    @staticmethod
    def _migrate_douyin_profile_for_rediscovery(
        job: DownloadJob,
        now: datetime,
    ) -> None:
        preserved_items = [item for item in job.items if item.output_paths]
        for item in preserved_items:
            item.auth_message = None
            item.status = ItemStatus.FAILED
            item.error = DOUYIN_PROFILE_REDISCOVERY_MESSAGE
            item.retryable = False
            item.metadata[DOUYIN_PROFILE_REDISCOVERY_ITEM_MARKER] = True
            media_id = str(item.media_id or "").strip()
            if not item.title or item.title == media_id or item.title.isdigit():
                item.title = f"Recovered Douyin files {media_id or item.id}"
            item.updated_at = now
        job.items = preserved_items
        job.status = JobStatus.INTERRUPTED
        job.error = DOUYIN_PROFILE_REDISCOVERY_MESSAGE
        job.warning = DOUYIN_PROFILE_REDISCOVERY_MESSAGE
        job.auth_message = None
        job.verification_url = job.source_url
        job.active_item_id = None
        job.cancel_requested = False
        job.retryable = True
        job.discovery_complete = False
        job.finished_at = now

    @staticmethod
    def _migrate_legacy_douyin_redirect_profile(
        job: DownloadJob,
        now: datetime,
    ) -> None:
        preserved_items: list[DownloadItem] = []
        for item in job.items:
            if item.status == ItemStatus.COMPLETED and item.output_paths:
                item.error = None
                item.auth_message = None
                item.metadata.pop(DOUYIN_PROFILE_REDISCOVERY_ITEM_MARKER, None)
                preserved_items.append(item)
                continue
            if not item.output_paths:
                continue
            item.status = ItemStatus.FAILED
            item.error = LEGACY_DOUYIN_MEDIA_REDIRECT_MESSAGE
            item.auth_message = None
            item.retryable = False
            item.metadata[DOUYIN_PROFILE_REDISCOVERY_ITEM_MARKER] = True
            item.updated_at = now
            preserved_items.append(item)
        job.items = preserved_items
        job.status = JobStatus.INTERRUPTED
        job.error = LEGACY_DOUYIN_MEDIA_REDIRECT_MESSAGE
        job.warning = LEGACY_DOUYIN_MEDIA_REDIRECT_MESSAGE
        job.auth_message = None
        job.verification_url = None
        job.active_item_id = None
        job.cancel_requested = False
        job.retryable = True
        job.discovery_complete = False
        job.finished_at = now

    @staticmethod
    def _retire_legacy_douyin_redirect_short_link(
        job: DownloadJob,
        now: datetime,
    ) -> None:
        for item in job.items:
            item.auth_message = None
            if item.status == ItemStatus.COMPLETED and item.output_paths:
                item.error = None
                continue
            item.status = ItemStatus.FAILED
            item.error = LEGACY_DOUYIN_SHORT_REDIRECT_MESSAGE
            item.retryable = False
            item.updated_at = now
        job.refresh_counts()
        job.status = JobStatus.PARTIAL if job.completed_items else JobStatus.FAILED
        job.error = LEGACY_DOUYIN_SHORT_REDIRECT_MESSAGE
        job.warning = LEGACY_DOUYIN_SHORT_REDIRECT_MESSAGE
        job.auth_message = None
        job.verification_url = None
        job.active_item_id = None
        job.cancel_requested = False
        job.retryable = False
        job.discovery_complete = False
        job.finished_at = now

    @staticmethod
    def _is_numeric_legacy_item(item: DownloadItem) -> bool:
        media_id = item.media_id or item.id
        if not media_id.isdigit():
            return False
        title = item.title.strip()
        return title in {"", "Untitled", media_id, item.id}

    @classmethod
    def _is_unverifiable_legacy_douyin_queue(cls, job: DownloadJob) -> bool:
        if (
            job.retryable is False
            and job.error == DOUYIN_UNVERIFIABLE_QUEUE_ERROR
        ):
            return False
        if (
            job.platform != Platform.DOUYIN
            or job.source_kind != SourceKind.PROFILE
            or len(job.items) < 2
        ):
            return False
        unverified_numeric_items = [
            item
            for item in job.items
            if cls._is_numeric_legacy_item(item)
            and item.metadata.get("profile_owner_verified") is not True
        ]
        return (
            len(unverified_numeric_items) >= 2
            and len(unverified_numeric_items) * 2 >= len(job.items)
        )

    @staticmethod
    def _is_quarantined_legacy_douyin_profile_queue(job: DownloadJob) -> bool:
        return (
            job.platform == Platform.DOUYIN
            and job.source_kind == SourceKind.PROFILE
            and job.retryable is False
            and job.error == DOUYIN_UNVERIFIABLE_QUEUE_ERROR
            and job.discovery_complete is False
        )

    @staticmethod
    def _recoverable_douyin_profile_source(job: DownloadJob) -> str | None:
        if job.platform != Platform.DOUYIN or job.source_kind != SourceKind.PROFILE:
            return None
        try:
            parsed = urlsplit(job.source_url.strip())
            port = parsed.port
        except (TypeError, ValueError):
            return None
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme.lower() != "https"
            or hostname not in {"douyin.com", "www.douyin.com"}
            or parsed.username
            or parsed.password
            or port is not None
            or parsed.fragment
            or "modal_id" in parse_qs(parsed.query, keep_blank_values=True)
        ):
            return None
        match = re.fullmatch(r"/user/([A-Za-z0-9_-]{8,200})/?", parsed.path)
        if match is None:
            return None
        return f"https://www.douyin.com/user/{match.group(1)}"

    @classmethod
    def _is_complete_douyin_profile_item(
        cls,
        job: DownloadJob,
        item: DownloadItem,
    ) -> bool:
        if job.platform != Platform.DOUYIN or job.source_kind != SourceKind.PROFILE:
            return False
        owner_id = MediaDownloader._douyin_profile_id(job.source_url)
        media_id = str(item.media_id or "").strip()
        if not owner_id or not media_id.isdigit():
            return False
        canonical_url = f"https://www.douyin.com/video/{media_id}"
        try:
            source = identify_url(item.source_url)
        except (TypeError, ValueError):
            return False
        if (
            source.platform != Platform.DOUYIN
            or source.kind != SourceKind.ITEM
            or source.url != canonical_url
            or item.source_url != canonical_url
            or item.metadata.get("profile_url") != job.source_url
            or item.metadata.get("profile_owner_verified") is not True
        ):
            return False
        return is_complete_profile_media_metadata(
            item.metadata.get("douyin_profile_media"),
            media_id,
            owner_id,
        )

    @classmethod
    def _refresh_douyin_profile_item_from_cache(
        cls,
        job: DownloadJob,
        item: DownloadItem,
    ) -> bool:
        if not cls._is_complete_douyin_profile_item(job, item):
            return False
        cached = item.metadata["douyin_profile_media"]
        changed = False
        title = str(cached.get("title") or "").strip()
        if title and not title.isdigit() and item.title != title:
            item.title = title
            changed = True
        author = str(cached.get("author") or "").strip()
        if author and item.author != author:
            item.author = author
            changed = True
        create_time = cached.get("create_time")
        if type(create_time) is int and create_time > 0:
            upload_date = MediaDownloader._douyin_upload_date(create_time)
            if item.upload_date != upload_date:
                item.upload_date = upload_date
                changed = True
        media_type = (
            MediaType.IMAGE
            if cached.get("media_kind") == "image"
            else MediaType.VIDEO
        )
        if item.media_type != media_type:
            item.media_type = media_type
            changed = True
        return changed

    @classmethod
    def _is_incomplete_verified_douyin_profile_queue(
        cls,
        job: DownloadJob,
    ) -> bool:
        if (
            job.retryable is False
            and job.error == DOUYIN_UNVERIFIABLE_QUEUE_ERROR
        ):
            return False
        if (
            job.warning == DOUYIN_PROFILE_REDISCOVERY_MESSAGE
            and job.discovery_complete is False
        ):
            return False
        if cls._is_unverifiable_legacy_douyin_queue(job):
            return False
        if (
            job.platform != Platform.DOUYIN
            or job.source_kind != SourceKind.PROFILE
            or not job.items
        ):
            return False
        return any(
            item.metadata.get("profile_owner_verified") is True
            and item.metadata.get(DOUYIN_PROFILE_REMOVED_ITEM_MARKER) is not True
            and not cls._is_complete_douyin_profile_item(job, item)
            for item in job.items
        )

    @classmethod
    def _validate_discovery_result(
        cls,
        job: DownloadJob,
        result: DiscoveryResult,
    ) -> None:
        if job.platform == Platform.XIAOHONGSHU:
            if not result.items:
                raise TemporaryAccessError(
                    "Xiaohongshu discovery temporarily returned no trusted notes. "
                    "Retry after a short wait; no placeholders were queued."
                )
            if job.source_kind == SourceKind.ITEM and len(result.items) != 1:
                raise DiscoveryError(XIAOHONGSHU_ITEM_BINDING_ERROR)
            if job.source_kind == SourceKind.SHORT_LINK:
                resolved_binding = cls._xiaohongshu_short_binding_from_items(
                    result.items
                )
                expected_binding = cls._xiaohongshu_expected_short_binding(job)
                if resolved_binding is None:
                    raise DiscoveryError(XIAOHONGSHU_ITEM_BINDING_ERROR)
                if (
                    cls._has_xiaohongshu_binding_rediscovery_pending(job)
                    and expected_binding is None
                ):
                    raise DiscoveryError(
                        "Xiaohongshu short-link retry could not verify the original "
                        "resolved target. Create a new task from the original link."
                    )
                if expected_binding and resolved_binding != expected_binding:
                    raise DiscoveryError(
                        "Xiaohongshu short-link retry resolved to a different note "
                        "or profile. The changed target was blocked before download."
                    )
                if (
                    resolved_binding[0] == SourceKind.ITEM
                    and len(result.items) != 1
                ):
                    raise DiscoveryError(XIAOHONGSHU_ITEM_BINDING_ERROR)
            seen_media_ids: set[str] = set()
            for item in result.items:
                media_id = str(item.media_id or "").strip()
                if (
                    not cls._is_bound_xiaohongshu_item(job, item)
                    or media_id.lower() in seen_media_ids
                ):
                    raise DiscoveryError(
                        "Xiaohongshu discovery returned an untrusted, duplicate, or "
                        "cross-wired note URL. The unexpected item was blocked."
                    )
                seen_media_ids.add(media_id.lower())
            return

        if job.platform == Platform.DOUYIN and job.source_kind == SourceKind.PROFILE:
            has_retryable_unfinished_item = any(
                item.retryable
                and item.status not in {ItemStatus.COMPLETED, ItemStatus.SKIPPED}
                for item in job.items
            )
            if (
                (
                    cls._has_douyin_profile_rediscovery_pending(job)
                    or any(
                        item.metadata.get(
                            DOUYIN_PROFILE_REFRESH_REQUIRED_MARKER
                        )
                        is True
                        for item in job.items
                    )
                    or has_retryable_unfinished_item
                )
                and not result.discovery_complete
            ):
                raise TemporaryAccessError(
                    "Douyin profile retry returned only a partial author feed. "
                    "Previously queued media entries were not reused; retry after a "
                    "short wait before downloading any item."
                )
            seen_media_ids: set[str] = set()
            invalid_profile_item = False
            for item in result.items:
                media_id = str(item.media_id or "").strip()
                if (
                    not cls._is_complete_douyin_profile_item(job, item)
                    or media_id in seen_media_ids
                ):
                    invalid_profile_item = True
                    break
                seen_media_ids.add(media_id)
            if invalid_profile_item:
                raise TemporaryAccessError(DOUYIN_PROFILE_METADATA_ERROR)
            if not result.items:
                raise TemporaryAccessError(
                    "Douyin profile discovery temporarily returned no verified media "
                    "items. Retry after a short wait; no placeholders were queued."
                )
            return

        target = cls._douyin_item_target(job)
        if not target:
            return
        canonical_url, expected_id = target
        if len(result.items) != 1:
            raise DiscoveryError(DOUYIN_ITEM_EXPANSION_MESSAGE)
        item = result.items[0]
        if item.media_id != expected_id or not cls._is_bound_douyin_item_url(
            item.source_url,
            canonical_url,
            expected_id,
        ):
            raise DiscoveryError(DOUYIN_ITEM_EXPANSION_MESSAGE)
        item.source_url = canonical_url
        cached = item.metadata.get("douyin_item_media")
        cache_identity_is_bound = (
            item.metadata.get("item_identity_verified") is True
            and item.metadata.get("verification_url") == canonical_url
            and isinstance(cached, dict)
            and str(cached.get("media_id") or "").strip() == expected_id
            and re.fullmatch(
                r"[A-Za-z0-9_-]{10,200}",
                str(cached.get("video_uri") or "").strip(),
            )
            is not None
        )
        if cache_identity_is_bound and not (
            MediaDownloader._douyin_direct_candidates_from_cache(cached)
        ):
            raise TemporaryAccessError(
                "Douyin item discovery returned no verified author-feed direct "
                "rendition. Retry after a short wait; no default-only fallback was "
                "queued."
            )
        if cls._discard_invalid_douyin_item_cache(
            item,
            canonical_url,
            expected_id,
        ):
            raise DiscoveryError(DOUYIN_ITEM_EXPANSION_MESSAGE)
        cls._bind_douyin_item_metadata(job, item)

    @staticmethod
    def _is_bound_xiaohongshu_item(
        job: DownloadJob,
        item: DownloadItem,
    ) -> bool:
        if job.platform != Platform.XIAOHONGSHU:
            return False
        media_id = str(item.media_id or "").strip()
        source_note_id = xiaohongshu_note_id(item.source_url)
        if (
            not source_note_id
            or not media_id
            or media_id.lower() != source_note_id
        ):
            return False
        binding_kind = job.source_kind
        binding_url = job.source_url
        if binding_kind == SourceKind.SHORT_LINK:
            binding_url = str(
                item.metadata.get("xiaohongshu_resolved_source_url") or ""
            )
            try:
                binding_kind = SourceKind(
                    str(
                        item.metadata.get("xiaohongshu_resolved_source_kind")
                        or ""
                    )
                )
            except ValueError:
                return False
        if binding_kind == SourceKind.ITEM:
            return xiaohongshu_note_id(binding_url) == source_note_id
        if binding_kind != SourceKind.PROFILE:
            return False
        expected_profile_id = xiaohongshu_profile_id(binding_url)
        return bool(
            expected_profile_id
            and item.metadata.get("profile_note_membership_verified") is True
            and item.metadata.get("xiaohongshu_profile_id")
            == expected_profile_id
        )

    @staticmethod
    def _xiaohongshu_short_item_binding(
        item: DownloadItem,
    ) -> tuple[SourceKind, str] | None:
        try:
            kind = SourceKind(
                str(item.metadata.get("xiaohongshu_resolved_source_kind") or "")
            )
        except ValueError:
            return None
        resolved_url = str(
            item.metadata.get("xiaohongshu_resolved_source_url") or ""
        )
        if kind == SourceKind.ITEM:
            resolved_id = xiaohongshu_note_id(resolved_url)
        elif kind == SourceKind.PROFILE:
            resolved_id = xiaohongshu_profile_id(resolved_url)
        else:
            return None
        return (kind, resolved_id) if resolved_id else None

    @classmethod
    def _xiaohongshu_short_binding_from_items(
        cls,
        items: list[DownloadItem],
    ) -> tuple[SourceKind, str] | None:
        bindings = [cls._xiaohongshu_short_item_binding(item) for item in items]
        if not bindings or any(binding is None for binding in bindings):
            return None
        unique = set(bindings)
        return unique.pop() if len(unique) == 1 else None

    @classmethod
    def _xiaohongshu_expected_short_binding(
        cls,
        job: DownloadJob,
    ) -> tuple[SourceKind, str] | None:
        if job.source_kind != SourceKind.SHORT_LINK:
            return None
        if (
            job.resolved_source_kind in {SourceKind.ITEM, SourceKind.PROFILE}
            and str(job.resolved_source_id or "").strip()
        ):
            return job.resolved_source_kind, str(job.resolved_source_id).strip()
        return cls._xiaohongshu_short_binding_from_items(job.items)

    @staticmethod
    def _normalize_xiaohongshu_item_identity(item: DownloadItem) -> None:
        note_id = xiaohongshu_note_id(item.source_url)
        media_id = str(item.media_id or "").strip()
        if note_id and media_id.lower() == note_id:
            item.media_id = note_id

    @staticmethod
    def _is_xiaohongshu_direct_job(job: DownloadJob) -> bool:
        return (
            job.platform == Platform.XIAOHONGSHU
            and job.source_kind in {SourceKind.ITEM, SourceKind.SHORT_LINK}
        )

    @classmethod
    def _has_xiaohongshu_binding_rediscovery_pending(
        cls,
        job: DownloadJob,
    ) -> bool:
        return cls._is_xiaohongshu_direct_job(job) and any(
            item.metadata.get(XIAOHONGSHU_BINDING_REDISCOVERY_MARKER) is True
            or item.error == XIAOHONGSHU_ITEM_BINDING_ERROR
            for item in job.items
        )

    @classmethod
    def _trusted_xiaohongshu_previous_items(
        cls,
        job: DownloadJob,
        previous: list[DownloadItem],
        discovered: list[DownloadItem],
    ) -> list[DownloadItem]:
        discovered_media_ids = {
            str(item.media_id or "").strip().lower()
            for item in discovered
            if str(item.media_id or "").strip()
        }
        trusted: list[DownloadItem] = []
        seen_media_ids: set[str] = set()
        for item in previous:
            media_id = str(item.media_id or "").strip().lower()
            if item.status == ItemStatus.COMPLETED and (
                not media_id or media_id not in discovered_media_ids
            ):
                completed_key = media_id or f"item:{item.id}"
                if completed_key not in seen_media_ids:
                    seen_media_ids.add(completed_key)
                    trusted.append(item)
                continue
            if (
                not media_id
                or media_id not in discovered_media_ids
                or media_id in seen_media_ids
                or item.metadata.get(XIAOHONGSHU_BINDING_REDISCOVERY_MARKER)
                is True
                or not cls._is_bound_xiaohongshu_item(job, item)
            ):
                continue
            seen_media_ids.add(media_id)
            trusted.append(item)
        return trusted

    @classmethod
    def _bind_douyin_item_metadata(
        cls,
        job: DownloadJob,
        item: DownloadItem,
    ) -> tuple[bool, bool]:
        target = cls._douyin_item_target(job)
        if not target:
            return False, False
        canonical_url, expected_id = target
        if item.media_id != expected_id or not cls._is_bound_douyin_item_url(
            item.source_url,
            canonical_url,
            expected_id,
        ):
            return False, False

        changed = item.source_url != canonical_url
        item.source_url = canonical_url
        for key in (
            "profile_url",
            "profile_owner_verified",
            "douyin_profile_media",
        ):
            if key in item.metadata:
                item.metadata.pop(key, None)
                changed = True
        if item.metadata.get("verification_url") != canonical_url:
            item.metadata["verification_url"] = canonical_url
            changed = True
        return True, changed

    @staticmethod
    def _discard_invalid_douyin_item_cache(
        item: DownloadItem,
        canonical_url: str,
        expected_id: str,
    ) -> bool:
        if "douyin_item_media" not in item.metadata:
            return False
        cached = item.metadata.get("douyin_item_media")
        valid = (
            item.metadata.get("item_identity_verified") is True
            and item.metadata.get("verification_url") == canonical_url
            and isinstance(cached, dict)
            and str(cached.get("media_id") or "").strip() == expected_id
            and re.fullmatch(
                r"[A-Za-z0-9_-]{10,200}",
                str(cached.get("video_uri") or "").strip(),
            )
            is not None
            and bool(MediaDownloader._douyin_direct_candidates_from_cache(cached))
        )
        if valid:
            return False
        item.metadata.pop("douyin_item_media", None)
        item.metadata.pop("item_identity_verified", None)
        return True

    @staticmethod
    def _is_bound_douyin_item_url(
        value: str,
        canonical_url: str,
        expected_id: str,
    ) -> bool:
        try:
            source = identify_url(value)
        except (TypeError, ValueError):
            return False
        return (
            source.platform == Platform.DOUYIN
            and source.kind == SourceKind.ITEM
            and source.url == canonical_url
            and MediaDownloader._douyin_video_id(source.url) == expected_id
        )

    @staticmethod
    def _sanitize_persisted_errors(job: DownloadJob) -> bool:
        changed = False
        for attribute in ("error", "warning", "auth_message"):
            value = getattr(job, attribute)
            if value is None:
                continue
            safe_value = safe_external_error_message(value)
            if safe_value != value:
                setattr(job, attribute, safe_value)
                changed = True
        for item in job.items:
            for attribute in ("error", "auth_message"):
                value = getattr(item, attribute)
                if value is None:
                    continue
                safe_value = safe_external_error_message(value)
                if safe_value != value:
                    setattr(item, attribute, safe_value)
                    changed = True
        return changed

    @staticmethod
    def _message_has_legacy_douyin_media_redirect(value: str | None) -> bool:
        if not value:
            return False
        if LEGACY_DOUYIN_MEDIA_REDIRECT_MARKER in value:
            return True
        has_reason = bool(
            re.search(
                r"(?:Redirect reason:\s*|reason:\s*)[a-z0-9-]+",
                value,
                re.IGNORECASE,
            )
        )
        return not has_reason and any(
            marker in value
            for marker in LEGACY_DOUYIN_MEDIA_REDIRECT_MARKERS[1:]
        )

    @staticmethod
    def _message_has_douyin_media_redirect(value: str | None) -> bool:
        return bool(
            value
            and any(
                marker in value
                for marker in LEGACY_DOUYIN_MEDIA_REDIRECT_MARKERS
            )
        )

    @classmethod
    def _has_legacy_douyin_media_redirect_error(cls, job: DownloadJob) -> bool:
        messages = [job.error, job.warning, job.auth_message]
        for item in job.items:
            messages.extend((item.error, item.auth_message))
        return any(
            cls._message_has_legacy_douyin_media_redirect(value)
            for value in messages
        )

    @classmethod
    def _verification_url(cls, job: DownloadJob, fallback: str) -> str:
        target = cls._douyin_item_target(job)
        return target[0] if target else fallback

    def _ensure_not_running(self, job_id: str) -> None:
        future = self._futures.get(job_id)
        if future is not None and not future.done():
            raise JobBusyError(f"Job {job_id} is already running")

    @staticmethod
    def _should_rediscover_on_retry(job: DownloadJob) -> bool:
        if DownloadManager._has_xiaohongshu_binding_rediscovery_pending(job):
            return True
        if (
            job.platform == Platform.XIAOHONGSHU
            and job.source_kind in {SourceKind.ITEM, SourceKind.SHORT_LINK}
        ):
            return job.status in {
                JobStatus.FAILED,
                JobStatus.NEEDS_AUTH,
                JobStatus.INTERRUPTED,
                JobStatus.PARTIAL,
                JobStatus.CANCELLED,
            } or any(
                item.retryable and item.status in RETRYABLE_ITEM_STATUSES
                for item in job.items
            )
        if job.platform == Platform.DOUYIN and job.source_kind == SourceKind.ITEM:
            return job.status in {
                JobStatus.FAILED,
                JobStatus.NEEDS_AUTH,
                JobStatus.INTERRUPTED,
                JobStatus.PARTIAL,
                JobStatus.CANCELLED,
            } or any(
                item.retryable and item.status in RETRYABLE_ITEM_STATUSES
                for item in job.items
            )
        return (
            job.source_kind == SourceKind.PROFILE
            and job.platform in {Platform.XIAOHONGSHU, Platform.DOUYIN}
            and (
                job.status in {JobStatus.NEEDS_AUTH, JobStatus.INTERRUPTED}
                or any(item.status == ItemStatus.NEEDS_AUTH for item in job.items)
                or not job.discovery_complete
                or any(
                    item.retryable and item.status in RETRYABLE_ITEM_STATUSES
                    for item in job.items
                )
            )
        )

    @staticmethod
    def _has_douyin_profile_rediscovery_pending(job: DownloadJob) -> bool:
        return (
            job.platform == Platform.DOUYIN
            and job.source_kind == SourceKind.PROFILE
            and any(
                item.metadata.get(DOUYIN_PROFILE_REDISCOVERY_ITEM_MARKER) is True
                for item in job.items
            )
        )

    @staticmethod
    def _has_douyin_profile_refresh_required(job: DownloadJob) -> bool:
        return (
            job.platform == Platform.DOUYIN
            and job.source_kind == SourceKind.PROFILE
            and any(
                item.metadata.get(DOUYIN_PROFILE_REFRESH_REQUIRED_MARKER) is True
                for item in job.items
            )
        )

    @staticmethod
    def _merge_discovered_items(
        previous: list[DownloadItem],
        discovered: list[DownloadItem],
        *,
        retire_missing_douyin_profile_items: bool = False,
    ) -> list[DownloadItem]:
        previous_by_media_id = {
            item.media_id: item for item in previous if item.media_id
        }
        previous_by_id = {item.id: item for item in previous}
        matched_previous_ids: set[str] = set()
        merged: list[DownloadItem] = []

        for fresh in discovered:
            fresh_is_douyin = (
                fresh.extractor_key == "Douyin"
                or fresh.metadata.get("profile_owner_verified") is True
                or "douyin_item_media" in fresh.metadata
            )
            old = (
                previous_by_media_id.get(fresh.media_id)
                if fresh.media_id
                else previous_by_id.get(fresh.id)
            )
            if old is None:
                merged.append(fresh)
                continue

            matched_previous_ids.add(old.id)
            recovery_pending = (
                old.metadata.get(DOUYIN_PROFILE_REDISCOVERY_ITEM_MARKER) is True
            )
            removed_after_refresh = (
                old.metadata.get(DOUYIN_PROFILE_REMOVED_ITEM_MARKER) is True
            )
            if recovery_pending or removed_after_refresh:
                fresh.attempts = old.attempts
                fresh.created_at = old.created_at
                fresh.output_paths = list(old.output_paths)
                fresh.status = ItemStatus.QUEUED
                fresh.error = None
                fresh.auth_message = None
                fresh.retryable = True
                old_metadata = dict(old.metadata)
                old_metadata.pop(DOUYIN_PROFILE_REDISCOVERY_ITEM_MARKER, None)
                old_metadata.pop(DOUYIN_PROFILE_REFRESH_REQUIRED_MARKER, None)
                old_metadata.pop(DOUYIN_PROFILE_REMOVED_ITEM_MARKER, None)
                fresh.metadata = {**old_metadata, **fresh.metadata}
                merged.append(fresh)
                continue
            if old.status == ItemStatus.COMPLETED:
                refreshed = old.model_copy(deep=True)
                refreshed.id = fresh.id
                refreshed.source_url = fresh.source_url
                if fresh_is_douyin:
                    refreshed.title = fresh.title
                    refreshed.upload_date = fresh.upload_date
                    refreshed.author = fresh.author
                    refreshed.media_type = fresh.media_type
                refreshed.playlist_index = fresh.playlist_index
                refreshed.extractor_key = fresh.extractor_key
                refreshed.metadata.update(fresh.metadata)
                merged.append(refreshed)
                continue

            fresh.attempts = old.attempts
            fresh.created_at = old.created_at
            fresh.status = old.status
            fresh.progress = old.progress.model_copy(deep=True)
            fresh.output_paths = list(old.output_paths)
            fresh.selected_format = old.selected_format
            fresh.resolution = old.resolution
            fresh.error = old.error
            fresh.auth_message = old.auth_message
            fresh.retryable = old.retryable
            old_metadata = dict(old.metadata)
            old_metadata.pop(DOUYIN_PROFILE_REFRESH_REQUIRED_MARKER, None)
            fresh.metadata = {**old_metadata, **fresh.metadata}
            if (
                not fresh_is_douyin
                and old.title
                and old.title != old.media_id
            ):
                fresh.title = old.title
            if not fresh_is_douyin:
                fresh.upload_date = old.upload_date or fresh.upload_date
                fresh.author = old.author or fresh.author
                fresh.media_type = (
                    old.media_type
                    if old.media_type.value != "unknown"
                    else fresh.media_type
                )
            merged.append(fresh)

        for old in previous:
            if old.id in matched_previous_ids:
                continue
            retained = old.model_copy(deep=True)
            if retained.metadata.pop(
                DOUYIN_PROFILE_REDISCOVERY_ITEM_MARKER, None
            ) is True:
                retained.status = ItemStatus.SKIPPED
                retained.error = DOUYIN_PROFILE_RETIRED_ITEM_MESSAGE
                retained.auth_message = None
                retained.retryable = False
                media_id = str(retained.media_id or "").strip()
                if (
                    not retained.title
                    or retained.title == media_id
                    or retained.title.isdigit()
                ):
                    retained.title = (
                        f"Recovered Douyin files {media_id or retained.id}"
                    )
                retained.updated_at = utc_now()
                merged.append(retained)
                continue
            if (
                retire_missing_douyin_profile_items
                and retained.status
                not in {ItemStatus.COMPLETED, ItemStatus.SKIPPED}
            ):
                if not retained.output_paths:
                    continue
                retained.metadata.pop(
                    DOUYIN_PROFILE_REFRESH_REQUIRED_MARKER,
                    None,
                )
                retained.metadata.pop("douyin_profile_media", None)
                retained.metadata[DOUYIN_PROFILE_REMOVED_ITEM_MARKER] = True
                retained.status = ItemStatus.FAILED
                retained.error = DOUYIN_PROFILE_REMOVED_PARTIAL_ITEM_MESSAGE
                retained.auth_message = None
                retained.retryable = False
                retained.updated_at = utc_now()
                merged.append(retained)
                continue
            if retained.status != ItemStatus.COMPLETED and retained.retryable:
                retained.status = ItemStatus.FAILED
                retained.error = "Item was not found when the profile was refreshed"
                retained.updated_at = utc_now()
            merged.append(retained)

        return merged

    @classmethod
    def _mark_douyin_profile_redirect_refresh_required(
        cls,
        job: DownloadJob,
    ) -> bool:
        redirect_items = [
            item
            for item in job.items
            if item.status not in {ItemStatus.COMPLETED, ItemStatus.SKIPPED}
            and (
                cls._message_has_douyin_media_redirect(item.error)
                or cls._message_has_douyin_media_redirect(item.auth_message)
            )
        ]
        if not redirect_items and any(
            cls._message_has_douyin_media_redirect(value)
            for value in (job.error, job.warning, job.auth_message)
        ):
            active_item = next(
                (
                    item
                    for item in job.items
                    if item.id == job.active_item_id
                    and item.status
                    not in {ItemStatus.COMPLETED, ItemStatus.SKIPPED}
                ),
                None,
            )
            fallback_item = next(
                (
                    item
                    for item in job.items
                    if item.status
                    not in {ItemStatus.COMPLETED, ItemStatus.SKIPPED}
                ),
                None,
            )
            if active_item or fallback_item:
                redirect_items = [active_item or fallback_item]
        if not redirect_items:
            return False
        changed = False
        for item in redirect_items:
            if item.metadata.get(DOUYIN_PROFILE_REFRESH_REQUIRED_MARKER) is not True:
                item.metadata[DOUYIN_PROFILE_REFRESH_REQUIRED_MARKER] = True
                changed = True
            if not item.retryable:
                item.retryable = True
                changed = True
        if not job.retryable:
            job.retryable = True
            changed = True
        if job.discovery_complete:
            job.discovery_complete = False
            changed = True
        return changed

    def _require_job(self, job_id: str) -> DownloadJob:
        try:
            return self._jobs[job_id]
        except KeyError as exc:
            raise JobNotFoundError(job_id) from exc

    @staticmethod
    def _find_item(job: DownloadJob, item_id: str) -> DownloadItem:
        for item in job.items:
            if item.id == item_id:
                return item
        raise ItemNotFoundError(item_id)

    @staticmethod
    def _reset_item(item: DownloadItem) -> None:
        item.status = ItemStatus.QUEUED
        item.error = None
        item.auth_message = None
        item.progress = item.progress.model_copy(
            update={
                "downloaded_bytes": 0,
                "total_bytes": None,
                "percent": None,
                "speed_bytes_per_second": None,
                "eta_seconds": None,
            }
        )
        item.updated_at = utc_now()

    def _commit_locked(self, job: DownloadJob) -> None:
        job.refresh_counts()
        job.updated_at = utc_now()
        job.revision += 1
        self.store.save(job)

    def _notify(self, job: DownloadJob, event: str, item_id: str | None = None) -> None:
        with self._lock:
            listeners = list(self._listeners)
        manager_event = ManagerEvent(
            job_id=job.id,
            item_id=item_id,
            event=event,
            revision=job.revision,
        )
        for listener in listeners:
            try:
                listener(manager_event, job.model_copy(deep=True))
            except Exception:
                continue
