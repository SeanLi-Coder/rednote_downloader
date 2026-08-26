from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Iterator

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .downloader import DownloaderConfig
from .models import DownloadJob
from .platforms import UnsupportedUrlError, identify_url
from .storage import JobNotFoundError
from .task_manager import (
    DownloadManager,
    ItemNotFoundError,
    ItemNotRetryableError,
    JobBusyError,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_PATH = DATA_DIR / "config.json"
DEFAULT_DOWNLOAD_DIR = PROJECT_ROOT / "downloads"


class AppConfig(BaseModel):
    download_dir: str = str(DEFAULT_DOWNLOAD_DIR)
    use_chrome_cookies: bool = True
    chrome_profile: str | None = None

    @field_validator("download_dir")
    @classmethod
    def validate_download_dir(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Download directory cannot be empty")
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return str(path.resolve())


class CreateJobRequest(BaseModel):
    url: str = Field(min_length=8, max_length=4096)


class RetryRequest(BaseModel):
    item_id: str | None = None


def _load_config() -> AppConfig:
    if not CONFIG_PATH.is_file():
        return AppConfig()
    try:
        return AppConfig.model_validate_json(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return AppConfig()


def _save_config(config: AppConfig) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_PATH.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(config.model_dump(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(CONFIG_PATH)


config = _load_config()
manager = DownloadManager(
    state_dir=DATA_DIR / "state",
    default_output_root=config.download_dir,
    downloader_config=DownloaderConfig(
        cookie_browser="chrome" if config.use_chrome_cookies else None,
        cookie_profile=config.chrome_profile,
    ),
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    manager.shutdown(wait=True, cancel_running=True)


app = FastAPI(
    title="Original Media Downloader",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url=None,
    lifespan=lifespan,
)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["127.0.0.1", "localhost", "testserver"],
)


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (JobNotFoundError, ItemNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (JobBusyError, ItemNotRetryableError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, UnsupportedUrlError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def _public_job(job: DownloadJob) -> DownloadJob:
    public = job.model_copy(deep=True)
    for item in public.items:
        for key in ("douyin_item_media", "douyin_profile_media"):
            cached = item.metadata.get(key)
            if isinstance(cached, dict):
                cached.pop("video_uri", None)
                cached.pop("direct_candidates", None)
    return public


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config", response_model=AppConfig)
def get_config() -> AppConfig:
    return config.model_copy(deep=True)


@app.put("/api/config", response_model=AppConfig)
def update_config(request: AppConfig) -> AppConfig:
    global config
    output_dir = Path(request.download_dir)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Cannot create download directory: {exc}",
        ) from exc
    config = request
    manager.default_output_root = output_dir
    manager.downloader_config.cookie_browser = (
        "chrome" if request.use_chrome_cookies else None
    )
    manager.downloader_config.cookie_profile = request.chrome_profile
    _save_config(config)
    return config.model_copy(deep=True)


@app.post("/api/jobs", status_code=201)
def create_job(request: CreateJobRequest):
    try:
        return _public_job(
            manager.create_job(
                request.url,
                output_root=config.download_dir,
                cookie_browser="chrome" if config.use_chrome_cookies else None,
                cookie_profile=config.chrome_profile,
            )
        )
    except Exception as exc:
        raise _http_error(exc) from exc


@app.get("/api/jobs")
def list_jobs():
    return [_public_job(job) for job in manager.list_jobs()]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    try:
        return _public_job(manager.get_job(job_id))
    except Exception as exc:
        raise _http_error(exc) from exc


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str, request: Annotated[RetryRequest, Body()]):
    try:
        if request.item_id:
            return _public_job(manager.retry_item(job_id, request.item_id))
        return _public_job(manager.retry_failed(job_id))
    except Exception as exc:
        raise _http_error(exc) from exc


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    try:
        return _public_job(manager.cancel_job(job_id))
    except Exception as exc:
        raise _http_error(exc) from exc


def _open_chrome(url: str) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(
            ["/usr/bin/open", "-a", "Google Chrome", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    if sys.platform.startswith("win"):
        candidates = [
            shutil.which("chrome.exe"),
            shutil.which("chrome"),
            str(
                Path(os.environ.get("LOCALAPPDATA", ""))
                / "Google/Chrome/Application/chrome.exe"
            ),
            str(
                Path(os.environ.get("PROGRAMFILES", ""))
                / "Google/Chrome/Application/chrome.exe"
            ),
            str(
                Path(os.environ.get("PROGRAMFILES(X86)", ""))
                / "Google/Chrome/Application/chrome.exe"
            ),
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                subprocess.Popen(
                    [candidate, url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
        raise RuntimeError("Google Chrome was not found")
    for executable in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    ):
        try:
            subprocess.Popen(
                [executable, url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except FileNotFoundError:
            continue
    raise RuntimeError("Google Chrome was not found")


@app.post("/api/jobs/{job_id}/verify")
def open_verification(job_id: str) -> dict[str, str]:
    try:
        job = manager.get_job(job_id)
        source = identify_url(job.source_url)
        if source.platform != job.platform or source.kind != job.source_kind:
            raise UnsupportedUrlError("The original task URL is no longer verifiable")
        url = source.url
        _open_chrome(url)
        return {"status": "opened", "url": url}
    except Exception as exc:
        raise _http_error(exc) from exc


@app.get("/api/events")
def events() -> StreamingResponse:
    messages: queue.Queue[str] = queue.Queue(maxsize=1)

    def listener(_, job) -> None:
        payload = json.dumps(
            _public_job(job).model_dump(mode="json"),
            ensure_ascii=False,
        )
        try:
            messages.put_nowait(payload)
        except queue.Full:
            try:
                messages.get_nowait()
                messages.put_nowait(payload)
            except (queue.Empty, queue.Full):
                pass

    def stream() -> Iterator[str]:
        manager.add_listener(listener)
        try:
            yield ": connected\n\n"
            while True:
                try:
                    payload = messages.get(timeout=15)
                    yield f"event: job\ndata: {payload}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            manager.remove_listener(listener)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
