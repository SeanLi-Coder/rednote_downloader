from __future__ import annotations

import json
import queue
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .browser import open_chrome
from .build_info import APP_ID, APP_VERSION, BUILD_ID, calculate_build_id
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
PUBLIC_DOUYIN_MEDIA_FIELDS = frozenset(
    {
        "asset_count",
        "author",
        "create_time",
        "duration_ms",
        "image_count",
        "live_photo_count",
        "media_id",
        "media_kind",
        "media_type",
        "minimum_height",
        "minimum_width",
        "owner_id",
        "title",
    }
)
PUBLIC_SENSITIVE_QUERY_FIELDS = frozenset({"xsec_token"})


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
    version=APP_VERSION,
    docs_url="/api/docs",
    redoc_url=None,
    lifespan=lifespan,
)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["127.0.0.1", "localhost", "testserver"],
)


@app.middleware("http")
async def disable_runtime_asset_cache(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (JobNotFoundError, ItemNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (JobBusyError, ItemNotRetryableError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, UnsupportedUrlError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def _redact_public_url(value: str) -> str:
    if "xsec_token" not in value.lower():
        return value
    try:
        parsed = urlsplit(value)
        filtered_query = [
            (name, item)
            for name, item in parse_qsl(parsed.query, keep_blank_values=True)
            if name.lower() not in PUBLIC_SENSITIVE_QUERY_FIELDS
        ]
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                urlencode(filtered_query, doseq=True),
                parsed.fragment,
            )
        )
    except (TypeError, ValueError):
        return re.sub(
            r"([?&])xsec_token=[^&#]*&?",
            lambda match: match.group(1) if match.group(0).endswith("&") else "",
            value,
            flags=re.IGNORECASE,
        )


def _redact_public_value(value):
    if isinstance(value, str):
        return _redact_public_url(value)
    if isinstance(value, list):
        return [_redact_public_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_public_value(item) for item in value)
    if isinstance(value, dict):
        return {name: _redact_public_value(item) for name, item in value.items()}
    return value


def _public_job(job: DownloadJob) -> DownloadJob:
    public = job.model_copy(deep=True)
    public.source_url = _redact_public_url(public.source_url)
    if public.verification_url:
        public.verification_url = _redact_public_url(public.verification_url)
    for item in public.items:
        item.source_url = _redact_public_url(item.source_url)
        item.metadata = _redact_public_value(item.metadata)
        for key in ("douyin_item_media", "douyin_profile_media"):
            cached = item.metadata.get(key)
            if isinstance(cached, dict):
                item.metadata[key] = {
                    name: value
                    for name, value in cached.items()
                    if name in PUBLIC_DOUYIN_MEDIA_FIELDS
                }
            elif key in item.metadata:
                item.metadata.pop(key, None)
    return public


@app.get("/api/health")
def health() -> dict[str, str | bool]:
    source_build_id = calculate_build_id()
    return {
        "status": "ok",
        "app_id": APP_ID,
        "version": APP_VERSION,
        "build_id": BUILD_ID,
        "source_build_id": source_build_id,
        "restart_required": source_build_id != BUILD_ID,
    }


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


_open_chrome = open_chrome


@app.post("/api/jobs/{job_id}/verify")
def open_verification(job_id: str) -> dict[str, str]:
    try:
        job = manager.get_job(job_id)
        source = identify_url(job.source_url)
        if source.platform != job.platform or source.kind != job.source_kind:
            raise UnsupportedUrlError("The original task URL is no longer verifiable")
        url = source.url
        _open_chrome(url)
        return {"status": "opened", "url": _redact_public_url(url)}
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
def index() -> HTMLResponse:
    content = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    content = (
        content.replace("__APP_ID__", APP_ID)
        .replace("__APP_VERSION__", APP_VERSION)
        .replace("__BUILD_ID__", BUILD_ID)
    )
    return HTMLResponse(
        content,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
        },
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
