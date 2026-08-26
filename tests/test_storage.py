from __future__ import annotations

import json

import pytest

from app.models import DownloadJob, Platform, SourceKind
from app.storage import JobNotFoundError, JsonJobStore


def make_job(job_id: str = "job-1") -> DownloadJob:
    return DownloadJob(
        id=job_id,
        source_url="https://www.youtube.com/@BlenderOfficial",
        platform=Platform.YOUTUBE,
        source_kind=SourceKind.PROFILE,
        output_root="downloads",
    )


def test_store_round_trip(tmp_path) -> None:
    store = JsonJobStore(tmp_path)
    job = make_job()

    store.save(job)

    assert store.get(job.id) == job
    assert store.load_all() == ([job], [])


def test_store_rejects_unsafe_job_ids(tmp_path) -> None:
    store = JsonJobStore(tmp_path)

    with pytest.raises(ValueError, match="Invalid job ID"):
        store.get("../outside")


def test_store_reports_missing_job(tmp_path) -> None:
    store = JsonJobStore(tmp_path)

    with pytest.raises(JobNotFoundError):
        store.get("missing")


def test_store_skips_corrupt_state_and_returns_warning(tmp_path) -> None:
    store = JsonJobStore(tmp_path)
    store.save(make_job("healthy"))
    (tmp_path / "broken.json").write_text("{not-json", encoding="utf-8")

    jobs, warnings = store.load_all()

    assert [job.id for job in jobs] == ["healthy"]
    assert len(warnings) == 1
    assert "broken.json" in warnings[0]


def test_save_replaces_existing_state_atomically(tmp_path) -> None:
    store = JsonJobStore(tmp_path)
    job = make_job()
    store.save(job)
    job.revision = 2

    store.save(job)

    payload = json.loads((tmp_path / "job-1.json").read_text(encoding="utf-8"))
    assert payload["revision"] == 2
    assert list(tmp_path.glob("*.tmp")) == []
