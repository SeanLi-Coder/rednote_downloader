from __future__ import annotations

import hashlib
from pathlib import Path


APP_ID = "original-media-downloader"
APP_VERSION = "1.2.10"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def calculate_build_id(project_root: Path = PROJECT_ROOT) -> str:
    """Fingerprint the code and static assets loaded by the local web process."""

    candidates = [
        project_root / "run.py",
        project_root / "launcher.py",
        project_root / "stop.py",
        project_root / "start.command",
        project_root / "start.sh",
        project_root / "start.bat",
        project_root / "stop.command",
        project_root / "stop.sh",
        project_root / "stop.bat",
        project_root / "pyproject.toml",
        project_root / "requirements.txt",
    ]
    candidates.extend((project_root / "app").glob("*.py"))
    candidates.extend((project_root / "app" / "static").glob("*"))
    digest = hashlib.sha256()
    for path in sorted(
        (candidate for candidate in candidates if candidate.is_file()),
        key=lambda candidate: candidate.relative_to(project_root).as_posix(),
    ):
        relative = path.relative_to(project_root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:12]


BUILD_ID = calculate_build_id()
