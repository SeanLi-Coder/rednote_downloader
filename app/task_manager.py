from __future__ import annotations

import re
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from .downloader import (
    DOUYIN_ITEM_EXPANSION_MESSAGE,
    DiscoveryResult,
    DownloaderConfig,
    EngineEvent,
    MediaDownloader,
    safe_component,
    safe_external_error_message,
)
from .errors import (
    AuthenticationRequiredError,
    DownloadCancelledError,
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
    Platform,
    SourceKind,
    utc_now,
)
from .platforms import identify_url
from .storage import JobNotFoundError, JsonJobStore


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
                job.platform == Platform.DOUYIN
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
            target = self._douyin_item_target(job)
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
            if self._douyin_item_has_invalid_expansion(job):
                target = self._douyin_item_target(job)
                now = utc_now()
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
                elif job.total_items and job.completed_items == job.total_items:
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
            if self._prepare_douyin_item_rediscovery_locked(job):
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
            if self._prepare_douyin_item_rediscovery_locked(job):
                self._submit_locked(job, None, rediscover=True)
                return self.get_job(job_id)
            item = self._find_item(job, item_id)
            if item.status not in RETRYABLE_ITEM_STATUSES or not item.retryable:
                raise ItemNotRetryableError(
                    f"Item {item_id} is not failed, cancelled, or waiting for authentication"
                )
            rediscover = self._should_rediscover_on_retry(job)
            if rediscover:
                self._submit_locked(job, [item.id], rediscover=True)
                return self.get_job(job_id)
            targets = [item_id]
            self._reset_item(item)
            self._submit_locked(job, targets, rediscover=False)
        return self.get_job(job_id)

    def retry_failed(self, job_id: str) -> DownloadJob:
        with self._lock:
            job = self._require_job(job_id)
            self._ensure_not_running(job_id)
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
            completed_profile_can_continue = rediscover and all(
                item.status == ItemStatus.COMPLETED for item in job.items
            )
            if job.items and not targets and not completed_profile_can_continue:
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
                    job.items = self._merge_discovered_items(
                        previous_items,
                        result.items,
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
                            raise AuthenticationRequiredError(
                                DOUYIN_ITEM_EXPANSION_MESSAGE,
                                verification_url=job.source_url,
                            )
                self._notify(self.get_job(job_id), "item_started", item_id)

                try:
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
                        item.retryable = True
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
            if persist or now - self._last_progress_save.get(job_id, 0.0) >= 0.5:
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

    @classmethod
    def _validate_discovery_result(
        cls,
        job: DownloadJob,
        result: DiscoveryResult,
    ) -> None:
        target = cls._douyin_item_target(job)
        if not target:
            return
        canonical_url, expected_id = target
        if len(result.items) != 1:
            raise AuthenticationRequiredError(
                DOUYIN_ITEM_EXPANSION_MESSAGE,
                verification_url=canonical_url,
            )
        item = result.items[0]
        if item.media_id != expected_id or not cls._is_bound_douyin_item_url(
            item.source_url,
            canonical_url,
            expected_id,
        ):
            raise AuthenticationRequiredError(
                DOUYIN_ITEM_EXPANSION_MESSAGE,
                verification_url=canonical_url,
            )
        item.source_url = canonical_url
        if cls._discard_invalid_douyin_item_cache(
            item,
            canonical_url,
            expected_id,
        ):
            raise AuthenticationRequiredError(
                DOUYIN_ITEM_EXPANSION_MESSAGE,
                verification_url=canonical_url,
            )
        cls._bind_douyin_item_metadata(job, item)

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
                or (
                    job.platform == Platform.DOUYIN
                    and any(
                        item.retryable and item.status in RETRYABLE_ITEM_STATUSES
                        for item in job.items
                    )
                )
            )
        )

    @staticmethod
    def _merge_discovered_items(
        previous: list[DownloadItem], discovered: list[DownloadItem]
    ) -> list[DownloadItem]:
        previous_by_media_id = {
            item.media_id: item for item in previous if item.media_id
        }
        previous_by_id = {item.id: item for item in previous}
        matched_previous_ids: set[str] = set()
        merged: list[DownloadItem] = []

        for fresh in discovered:
            old = (
                previous_by_media_id.get(fresh.media_id)
                if fresh.media_id
                else previous_by_id.get(fresh.id)
            )
            if old is None:
                merged.append(fresh)
                continue

            matched_previous_ids.add(old.id)
            if old.status == ItemStatus.COMPLETED:
                refreshed = old.model_copy(deep=True)
                refreshed.id = fresh.id
                refreshed.source_url = fresh.source_url
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
            fresh.metadata = {**old.metadata, **fresh.metadata}
            if old.title and old.title != old.media_id:
                fresh.title = old.title
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
            if retained.status != ItemStatus.COMPLETED and retained.retryable:
                retained.status = ItemStatus.FAILED
                retained.error = "Item was not found when the profile was refreshed"
                retained.updated_at = utc_now()
            merged.append(retained)

        return merged

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
