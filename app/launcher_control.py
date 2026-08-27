from __future__ import annotations

import json
import os
import re
import secrets
import stat
import tempfile
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .runtime import STOP_TOKEN_HEADER


CONTROL_HOST = "127.0.0.1"
LAUNCHER_RECORD_FILENAME = "launcher.json"
LAUNCHER_RECORD_MAX_BYTES = 64 * 1024

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_INSTANCE_ID = re.compile(r"^[a-f0-9]{32}$")
_STOP_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
_EXPECTED_RECORD_FIELDS = frozenset(
    {
        "app_id",
        "build_id",
        "instance_id",
        "stop_token",
        "pid",
        "target_port",
        "control_port",
        "project_root",
        "started_at",
    }
)


class LauncherControlError(RuntimeError):
    """Base class for launcher control failures."""


class InvalidLauncherRecordError(LauncherControlError):
    """Raised when launcher identity data cannot be trusted."""


def _require_plain_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise InvalidLauncherRecordError(f"Launcher record {name} is invalid")
    return value


def _require_identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _SAFE_IDENTIFIER.fullmatch(value):
        raise InvalidLauncherRecordError(f"Launcher record {name} is invalid")
    return value


def _require_project_root(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        raise InvalidLauncherRecordError("Launcher record project_root is invalid")
    path = Path(value)
    if not path.is_absolute():
        raise InvalidLauncherRecordError(
            "Launcher record project_root is not absolute"
        )
    canonical = str(path.resolve(strict=False))
    if canonical != value or path == Path(path.anchor):
        raise InvalidLauncherRecordError(
            "Launcher record project_root is not canonical"
        )
    return value


def _require_started_at(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise InvalidLauncherRecordError("Launcher record started_at is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidLauncherRecordError(
            "Launcher record started_at is invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidLauncherRecordError(
            "Launcher record started_at must include a timezone"
        )
    return value


@dataclass(frozen=True, slots=True)
class LauncherRecord:
    app_id: str
    build_id: str
    instance_id: str
    stop_token: str
    pid: int
    target_port: int
    control_port: int
    project_root: str
    started_at: str

    def __post_init__(self) -> None:
        _require_identifier(self.app_id, "app_id")
        _require_identifier(self.build_id, "build_id")
        if not isinstance(self.instance_id, str) or not _INSTANCE_ID.fullmatch(
            self.instance_id
        ):
            raise InvalidLauncherRecordError(
                "Launcher record instance_id is invalid"
            )
        if not isinstance(self.stop_token, str) or not _STOP_TOKEN.fullmatch(
            self.stop_token
        ):
            raise InvalidLauncherRecordError(
                "Launcher record stop_token is invalid"
            )
        _require_plain_int(self.pid, "pid", 2, 2**31 - 1)
        _require_plain_int(self.target_port, "target_port", 1, 65_535)
        _require_plain_int(self.control_port, "control_port", 1, 65_535)
        _require_project_root(self.project_root)
        _require_started_at(self.started_at)

    @classmethod
    def from_dict(cls, value: Any) -> LauncherRecord:
        if not isinstance(value, dict) or set(value) != _EXPECTED_RECORD_FIELDS:
            raise InvalidLauncherRecordError(
                "Launcher record fields are missing or unexpected"
            )
        return cls(**value)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def launcher_record_path(runtime_dir: str | Path) -> Path:
    return Path(runtime_dir).expanduser().resolve() / LAUNCHER_RECORD_FILENAME


def _read_regular_file(path: Path) -> bytes | None:
    try:
        path_metadata = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InvalidLauncherRecordError(
            f"Launcher record could not be inspected: {exc}"
        ) from exc
    if not stat.S_ISREG(path_metadata.st_mode):
        raise InvalidLauncherRecordError("Launcher record is not a regular file")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InvalidLauncherRecordError(
            f"Launcher record could not be opened: {exc}"
        ) from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise InvalidLauncherRecordError(
                "Launcher record is not a regular file"
            )
        if (path_metadata.st_dev, path_metadata.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise InvalidLauncherRecordError(
                "Launcher record changed while it was opened"
            )
        if metadata.st_size > LAUNCHER_RECORD_MAX_BYTES:
            raise InvalidLauncherRecordError("Launcher record is too large")
        payload = bytearray()
        while len(payload) <= LAUNCHER_RECORD_MAX_BYTES:
            chunk = os.read(
                fd,
                min(16_384, LAUNCHER_RECORD_MAX_BYTES + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > LAUNCHER_RECORD_MAX_BYTES:
            raise InvalidLauncherRecordError("Launcher record is too large")
        return bytes(payload)
    finally:
        os.close(fd)


def _reject_duplicate_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for name, item in pairs:
        if name in value:
            raise InvalidLauncherRecordError(
                "Launcher record contains duplicate fields"
            )
        value[name] = item
    return value


def read_launcher_record(runtime_dir: str | Path) -> LauncherRecord | None:
    payload = _read_regular_file(launcher_record_path(runtime_dir))
    if payload is None:
        return None
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_fields,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidLauncherRecordError(
            "Launcher record is not valid JSON"
        ) from exc
    return LauncherRecord.from_dict(value)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_launcher_record(
    runtime_dir: str | Path,
    record: LauncherRecord,
) -> Path:
    validated = LauncherRecord.from_dict(record.to_dict())
    path = launcher_record_path(runtime_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(temporary_fd, 0o600)
        else:
            os.chmod(temporary, 0o600)
        with os.fdopen(temporary_fd, "w", encoding="utf-8") as handle:
            temporary_fd = -1
            json.dump(validated.to_dict(), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        temporary.unlink(missing_ok=True)
    return path


def remove_launcher_record(
    runtime_dir: str | Path,
    *,
    instance_id: str,
) -> bool:
    if not isinstance(instance_id, str) or not _INSTANCE_ID.fullmatch(instance_id):
        raise ValueError("instance_id must be 32 lowercase hexadecimal characters")
    path = launcher_record_path(runtime_dir)
    record = read_launcher_record(runtime_dir)
    if record is None or record.instance_id != instance_id:
        return False
    try:
        current = read_launcher_record(runtime_dir)
        if current is None or current.instance_id != instance_id:
            return False
        path.unlink()
    except FileNotFoundError:
        return False
    _fsync_directory(path.parent)
    return True


class _LauncherControlServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        server_address: tuple[str, int],
        record: LauncherRecord,
        stop_requested: threading.Event,
    ) -> None:
        self.record = record
        self.stop_requested = stop_requested
        super().__init__(server_address, _LauncherControlHandler)


class _LauncherControlHandler(BaseHTTPRequestHandler):
    server: _LauncherControlServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _valid_local_request(self) -> bool:
        if not self.client_address or self.client_address[0] != CONTROL_HOST:
            self._send_json(403, {"detail": "Launcher control is local-only"})
            return False
        expected_host = f"{CONTROL_HOST}:{self.server.record.control_port}"
        if self.headers.get("Host", "") != expected_host:
            self._send_json(403, {"detail": "Invalid launcher control host"})
            return False
        return True

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(encoded)
        self.close_connection = True

    def _method_not_allowed(self, allowed: str) -> None:
        if not self._valid_local_request():
            return
        self.send_response(405)
        self.send_header("Allow", allowed)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def do_GET(self) -> None:
        if not self._valid_local_request():
            return
        if self.path == "/health":
            record = self.server.record
            self._send_json(
                200,
                {
                    "status": "starting",
                    "app_id": record.app_id,
                    "build_id": record.build_id,
                    "instance_id": record.instance_id,
                    "pid": record.pid,
                    "target_port": record.target_port,
                    "control_port": record.control_port,
                    "project_root": record.project_root,
                },
            )
            return
        if self.path == "/stop":
            self._method_not_allowed("POST")
            return
        self._send_json(404, {"detail": "Not found"})

    def do_POST(self) -> None:
        if not self._valid_local_request():
            return
        if self.path == "/health":
            self._method_not_allowed("GET")
            return
        if self.path != "/stop":
            self._send_json(404, {"detail": "Not found"})
            return
        expected_token = self.server.record.stop_token
        supplied_token = self.headers.get(STOP_TOKEN_HEADER, "")
        if not supplied_token or not secrets.compare_digest(
            supplied_token,
            expected_token,
        ):
            self._send_json(403, {"detail": "Invalid launcher stop token"})
            return
        self.server.stop_requested.set()
        self._send_json(
            200,
            {
                "status": "stopping",
                "instance_id": self.server.record.instance_id,
            },
        )

    def do_HEAD(self) -> None:
        self._method_not_allowed("GET, POST")

    def do_PUT(self) -> None:
        self._method_not_allowed("GET, POST")

    def do_DELETE(self) -> None:
        self._method_not_allowed("GET, POST")

    def do_PATCH(self) -> None:
        self._method_not_allowed("GET, POST")

    def do_OPTIONS(self) -> None:
        self._method_not_allowed("GET, POST")


class LauncherControl:
    """Own a token-authenticated loopback control server for the launcher."""

    def __init__(
        self,
        *,
        runtime_dir: str | Path,
        app_id: str,
        build_id: str,
        instance_id: str,
        stop_token: str,
        target_port: int,
        project_root: str | Path,
        stop_requested: threading.Event,
        pid: int | None = None,
        started_at: str | None = None,
    ) -> None:
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self.app_id = _require_identifier(app_id, "app_id")
        self.build_id = _require_identifier(build_id, "build_id")
        if not isinstance(instance_id, str) or not _INSTANCE_ID.fullmatch(instance_id):
            raise InvalidLauncherRecordError(
                "Launcher record instance_id is invalid"
            )
        if not isinstance(stop_token, str) or not _STOP_TOKEN.fullmatch(stop_token):
            raise InvalidLauncherRecordError("Launcher record stop_token is invalid")
        self.instance_id = instance_id
        self.stop_token = stop_token
        self.target_port = _require_plain_int(
            target_port,
            "target_port",
            1,
            65_535,
        )
        self.project_root = _require_project_root(
            str(Path(project_root).expanduser().resolve())
        )
        self.stop_requested = stop_requested
        self.pid = _require_plain_int(
            os.getpid() if pid is None else pid,
            "pid",
            2,
            2**31 - 1,
        )
        self._specified_started_at = (
            _require_started_at(started_at) if started_at is not None else None
        )
        self._lock = threading.RLock()
        self._server: _LauncherControlServer | None = None
        self._thread: threading.Thread | None = None
        self._record: LauncherRecord | None = None

    @property
    def record(self) -> LauncherRecord | None:
        with self._lock:
            return self._record

    @property
    def control_port(self) -> int | None:
        record = self.record
        return record.control_port if record is not None else None

    def start(self) -> LauncherRecord:
        with self._lock:
            if self._record is not None:
                return self._record
            started_at = self._specified_started_at or datetime.now(
                timezone.utc
            ).isoformat()
            provisional = LauncherRecord(
                app_id=self.app_id,
                build_id=self.build_id,
                instance_id=self.instance_id,
                stop_token=self.stop_token,
                pid=self.pid,
                target_port=self.target_port,
                control_port=1,
                project_root=self.project_root,
                started_at=started_at,
            )
            server = _LauncherControlServer(
                (CONTROL_HOST, 0),
                provisional,
                self.stop_requested,
            )
            port = int(server.server_address[1])
            record = LauncherRecord(
                **{
                    **provisional.to_dict(),
                    "control_port": port,
                }
            )
            server.record = record
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.05},
                name="launcher-control",
                daemon=True,
            )
            try:
                thread.start()
                write_launcher_record(self.runtime_dir, record)
            except BaseException:
                if thread.is_alive():
                    server.shutdown()
                    thread.join(timeout=2.0)
                server.server_close()
                try:
                    remove_launcher_record(
                        self.runtime_dir,
                        instance_id=self.instance_id,
                    )
                except (InvalidLauncherRecordError, OSError):
                    pass
                raise
            self._server = server
            self._thread = thread
            self._record = record
            return record

    def close(self) -> None:
        with self._lock:
            server = self._server
            thread = self._thread
            record = self._record
            if server is None and thread is None and record is None:
                return
            self._server = None
            self._thread = None
            self._record = None
            try:
                if server is not None:
                    try:
                        server.shutdown()
                    finally:
                        server.server_close()
                if thread is not None and thread is not threading.current_thread():
                    thread.join(timeout=2.0)
            finally:
                if record is not None:
                    try:
                        remove_launcher_record(
                            self.runtime_dir,
                            instance_id=record.instance_id,
                        )
                    except (InvalidLauncherRecordError, OSError):
                        pass

    def __enter__(self) -> LauncherControl:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


__all__ = [
    "CONTROL_HOST",
    "InvalidLauncherRecordError",
    "LAUNCHER_RECORD_FILENAME",
    "LAUNCHER_RECORD_MAX_BYTES",
    "LauncherControl",
    "LauncherControlError",
    "LauncherRecord",
    "launcher_record_path",
    "read_launcher_record",
    "remove_launcher_record",
    "write_launcher_record",
]
