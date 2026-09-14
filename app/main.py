from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, AsyncIterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .browser import open_chrome
from .build_info import APP_ID, APP_VERSION, BUILD_ID, calculate_build_id
from .downloader import DownloaderConfig
from .errors import SiteIssueCode
from .event_stream import JobEventBuffer
from .models import DownloadJob, ItemStatus, JobStatus, Platform, SourceKind
from .platforms import UnsupportedUrlError, identify_url
from .runtime import (
    RUNTIME_STOP_EVENT,
    STOP_TOKEN_HEADER,
    current_runtime_identity,
)
from .storage import JobNotFoundError
from .task_manager import (
    DownloadManager,
    ItemNotFoundError,
    ItemNotRetryableError,
    JobBusyError,
)
from .xiaohongshu import (
    is_trusted_xiaohongshu_note_url,
    is_trusted_xiaohongshu_profile_url,
    xiaohongshu_note_id,
    xiaohongshu_profile_id,
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
_CONFIG_LOCK = threading.RLock()


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
    # Windows can reject concurrent replacements even with distinct source files.
    with _CONFIG_LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=".config-", suffix=".tmp", dir=CONFIG_PATH.parent
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(config.model_dump(), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(CONFIG_PATH)
        finally:
            temporary.unlink(missing_ok=True)


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
def health() -> dict[str, str | bool | int]:
    source_build_id = calculate_build_id()
    instance_id, _, server_port = current_runtime_identity()
    return {
        "status": "ok",
        "app_id": APP_ID,
        "version": APP_VERSION,
        "build_id": BUILD_ID,
        "source_build_id": source_build_id,
        "restart_required": source_build_id != BUILD_ID,
        "instance_id": instance_id,
        "server_pid": os.getpid(),
        "server_port": server_port,
    }


def _request_process_stop() -> None:
    # Let the HTTP response leave the socket before Uvicorn begins shutdown.
    time.sleep(0.1)
    RUNTIME_STOP_EVENT.set()


@app.post("/api/runtime/stop")
def stop_runtime(request: Request) -> dict[str, str]:
    client_host = request.client.host if request.client is not None else ""
    if client_host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(status_code=403, detail="Runtime stop is local-only")

    instance_id, expected_token, _ = current_runtime_identity()
    supplied_token = request.headers.get(STOP_TOKEN_HEADER, "")
    if not expected_token:
        raise HTTPException(
            status_code=503,
            detail="This runtime was not started with a stop token",
        )
    if not supplied_token or not secrets.compare_digest(
        supplied_token,
        expected_token,
    ):
        raise HTTPException(status_code=403, detail="Invalid runtime stop token")

    threading.Thread(
        target=_request_process_stop,
        name="runtime-stop",
        daemon=True,
    ).start()
    return {
        "status": "stopping",
        "instance_id": instance_id,
    }


@app.get("/api/config", response_model=AppConfig)
def get_config() -> AppConfig:
    with _CONFIG_LOCK:
        return config.model_copy(deep=True)


@app.put("/api/config", response_model=AppConfig)
def update_config(request: AppConfig) -> AppConfig:
    global config
    request = request.model_copy(deep=True)
    output_dir = Path(request.download_dir)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Cannot create download directory: {exc}",
        ) from exc
    with _CONFIG_LOCK:
        try:
            _save_config(request)
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Could not save settings. Check free disk space and write "
                    "permissions for the project's data folder, then save again. "
                    "The previous settings are still in use."
                ),
            ) from exc
        config = request
        manager.default_output_root = output_dir
        manager.downloader_config.cookie_browser = (
            "chrome" if request.use_chrome_cookies else None
        )
        manager.downloader_config.cookie_profile = request.chrome_profile
        return config.model_copy(deep=True)


@app.post("/api/jobs", status_code=201)
def create_job(request: CreateJobRequest):
    snapshot = get_config()
    try:
        return _public_job(
            manager.create_job(
                request.url,
                output_root=snapshot.download_dir,
                cookie_browser="chrome" if snapshot.use_chrome_cookies else None,
                cookie_profile=snapshot.chrome_profile,
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


def _canonical_xiaohongshu_verification_note_url(
    value: str,
    expected_note_id: str,
) -> str | None:
    if (
        not is_trusted_xiaohongshu_note_url(value)
        or xiaohongshu_note_id(value) != expected_note_id
    ):
        return None
    try:
        parsed = urlsplit(value)
        allowed: dict[str, str] = {}
        for name, item in parse_qsl(parsed.query, keep_blank_values=True):
            normalized_name = name.lower()
            if normalized_name not in {"xsec_token", "xsec_source"}:
                continue
            if normalized_name in allowed:
                return None
            if not item or len(item) > 2_048 or any(ord(char) < 32 for char in item):
                return None
            if normalized_name == "xsec_source" and not re.fullmatch(
                r"[A-Za-z0-9_-]{1,64}", item
            ):
                return None
            allowed[normalized_name] = item
    except (TypeError, ValueError):
        return None
    query = urlencode(
        [
            (name, allowed[name])
            for name in ("xsec_token", "xsec_source")
            if name in allowed
        ]
    )
    return urlunsplit(
        ("https", "www.xiaohongshu.com", f"/explore/{expected_note_id}", query, "")
    )


def _xiaohongshu_verification_target(job: DownloadJob) -> str | None:
    if (
        job.platform != Platform.XIAOHONGSHU
        or job.status != JobStatus.NEEDS_AUTH
        or job.verification_url is None
    ):
        return None
    verification_note_id = xiaohongshu_note_id(job.verification_url)
    if not verification_note_id:
        return None
    candidates = [item for item in job.items if item.status == ItemStatus.NEEDS_AUTH]
    if len(candidates) != 1:
        return None
    item = candidates[0]
    if str(item.media_id or "").lower() != verification_note_id:
        return None
    item_url = _canonical_xiaohongshu_verification_note_url(
        item.source_url,
        verification_note_id,
    )
    verification_url = _canonical_xiaohongshu_verification_note_url(
        job.verification_url,
        verification_note_id,
    )
    if not item_url or not verification_url:
        return None

    if job.source_kind == SourceKind.ITEM:
        bound = xiaohongshu_note_id(job.source_url) == verification_note_id
    elif job.source_kind == SourceKind.PROFILE:
        profile_id = xiaohongshu_profile_id(job.source_url)
        bound = bool(
            profile_id
            and item.metadata.get("profile_note_membership_verified") is True
            and item.metadata.get("xiaohongshu_profile_id") == profile_id
        )
    elif job.source_kind == SourceKind.SHORT_LINK:
        resolved_kind = job.resolved_source_kind
        resolved_id = str(job.resolved_source_id or "").lower()
        metadata_kind = str(
            item.metadata.get("xiaohongshu_resolved_source_kind") or ""
        )
        metadata_url = str(
            item.metadata.get("xiaohongshu_resolved_source_url") or ""
        )
        if resolved_kind == SourceKind.ITEM:
            bound = bool(
                resolved_id == verification_note_id
                and metadata_kind == SourceKind.ITEM.value
                and xiaohongshu_note_id(metadata_url) == verification_note_id
            )
        elif resolved_kind == SourceKind.PROFILE:
            metadata_profile_id = xiaohongshu_profile_id(metadata_url)
            bound = bool(
                resolved_id
                and metadata_kind == SourceKind.PROFILE.value
                and metadata_profile_id == resolved_id
                and item.metadata.get("profile_note_membership_verified") is True
                and item.metadata.get("xiaohongshu_profile_id") == resolved_id
            )
        else:
            bound = False
    else:
        bound = False
    return verification_url if bound else None


def _is_trusted_xiaohongshu_verification_source(
    source_url: str,
    source_kind: SourceKind,
) -> bool:
    if source_kind == SourceKind.PROFILE:
        return is_trusted_xiaohongshu_profile_url(source_url)
    if source_kind == SourceKind.ITEM:
        return is_trusted_xiaohongshu_note_url(source_url)
    if source_kind != SourceKind.SHORT_LINK:
        return False
    try:
        parsed = urlsplit(source_url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        return (
            parsed.scheme == "https"
            and parsed.username is None
            and parsed.password is None
            and parsed.port in {None, 443}
            and (hostname == "xhslink.com" or hostname.endswith(".xhslink.com"))
        )
    except (TypeError, ValueError):
        return False


@app.post("/api/jobs/{job_id}/verify")
def open_verification(job_id: str) -> dict[str, str]:
    try:
        job = manager.get_job(job_id)
        if job.status != JobStatus.NEEDS_AUTH or job.issue_code not in {
            None,
            SiteIssueCode.UNKNOWN,
            SiteIssueCode.LOGIN_REQUIRED,
            SiteIssueCode.VERIFICATION_REQUIRED,
        }:
            raise ItemNotRetryableError(
                "This task does not currently require Chrome login or verification"
            )
        source = identify_url(job.source_url)
        if source.platform != job.platform or source.kind != job.source_kind:
            raise UnsupportedUrlError("The original task URL is no longer verifiable")
        if (
            job.platform == Platform.XIAOHONGSHU
            and not _is_trusted_xiaohongshu_verification_source(
                source.url,
                source.kind,
            )
        ):
            raise UnsupportedUrlError("The original task URL is no longer trusted")
        if (
            job.platform == Platform.XIAOHONGSHU
            and job.status == JobStatus.NEEDS_AUTH
            and job.cookie_browser == "chrome"
            and (
                job.cookie_profile is None
                or job.cookie_profile_auto_selected
            )
        ):
            job = manager.bind_xiaohongshu_verification_profile(job_id)
        url = _xiaohongshu_verification_target(job) or source.url
        profile = job.cookie_profile if job.cookie_browser == "chrome" else None
        _open_chrome(url, profile)
        return {"status": "opened", "url": _redact_public_url(url)}
    except Exception as exc:
        raise _http_error(exc) from exc


@app.get("/api/events")
async def events() -> StreamingResponse:
    async def stream() -> AsyncIterator[str]:
        messages = JobEventBuffer()
        manager.add_listener(messages.publish)
        try:
            yield ": connected\n\n"
            while True:
                try:
                    jobs = await messages.take()
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                for job in jobs:
                    payload = json.dumps(
                        _public_job(job).model_dump(mode="json"),
                        ensure_ascii=False,
                    )
                    yield f"event: job\ndata: {payload}\n\n"
        finally:
            messages.close()
            manager.remove_listener(messages.publish)

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
