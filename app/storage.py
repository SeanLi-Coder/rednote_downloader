from __future__ import annotations

import json
import os
import re
import threading
import uuid
from pathlib import Path

from .models import DownloadJob


class JobNotFoundError(KeyError):
    pass


class JsonJobStore:
    """Persist each job separately so one interrupted write cannot corrupt all jobs."""

    _SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")

    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _job_path(self, job_id: str) -> Path:
        if not self._SAFE_ID.fullmatch(job_id):
            raise ValueError("Invalid job ID")
        return self.state_dir / f"{job_id}.json"

    def save(self, job: DownloadJob) -> None:
        payload = job.model_dump(mode="json")
        target = self._job_path(job.id)
        temporary = self.state_dir / f".{job.id}.{uuid.uuid4().hex}.tmp"
        with self._lock:
            try:
                with temporary.open("w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)

    def get(self, job_id: str) -> DownloadJob:
        path = self._job_path(job_id)
        with self._lock:
            if not path.is_file():
                raise JobNotFoundError(job_id)
            return DownloadJob.model_validate_json(path.read_text(encoding="utf-8"))

    def load_all(self) -> tuple[list[DownloadJob], list[str]]:
        jobs: list[DownloadJob] = []
        warnings: list[str] = []
        with self._lock:
            for path in sorted(self.state_dir.glob("*.json")):
                try:
                    jobs.append(
                        DownloadJob.model_validate_json(
                            path.read_text(encoding="utf-8")
                        )
                    )
                except Exception as exc:
                    warnings.append(f"Could not load {path.name}: {exc}")
        jobs.sort(key=lambda job: job.created_at, reverse=True)
        return jobs, warnings
