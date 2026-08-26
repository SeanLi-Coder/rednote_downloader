from __future__ import annotations

from app.models import (
    DownloadItem,
    DownloadJob,
    ItemStatus,
    JobStatus,
    Platform,
    SourceKind,
)


def make_job() -> DownloadJob:
    return DownloadJob(
        id="job-1",
        source_url="https://www.youtube.com/@BlenderOfficial",
        platform=Platform.YOUTUBE,
        source_kind=SourceKind.PROFILE,
        output_root="downloads",
        items=[
            DownloadItem(
                id="done",
                source_url="https://example.com/1",
                status=ItemStatus.COMPLETED,
            ),
            DownloadItem(
                id="failed",
                source_url="https://example.com/2",
                status=ItemStatus.FAILED,
            ),
            DownloadItem(
                id="auth",
                source_url="https://example.com/3",
                status=ItemStatus.NEEDS_AUTH,
            ),
            DownloadItem(
                id="queued",
                source_url="https://example.com/4",
                status=ItemStatus.QUEUED,
            ),
        ],
    )


def test_refresh_counts_tracks_success_and_failures() -> None:
    job = make_job()

    job.refresh_counts()

    assert job.total_items == 4
    assert job.completed_items == 1
    assert job.failed_items == 2


def test_model_round_trip_preserves_progress_and_status() -> None:
    job = make_job()
    job.status = JobStatus.PARTIAL
    job.items[0].progress.downloaded_bytes = 4_096
    job.items[0].progress.total_bytes = 8_192
    job.items[0].progress.percent = 50.0

    restored = DownloadJob.model_validate_json(job.model_dump_json())

    assert restored.status == JobStatus.PARTIAL
    assert restored.items[0].progress.downloaded_bytes == 4_096
    assert restored.items[0].progress.total_bytes == 8_192
    assert restored.items[0].progress.percent == 50.0
