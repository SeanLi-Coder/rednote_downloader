from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .build_info import PROJECT_ROOT


ENV_INSTANCE_ID = "OMD_INSTANCE_ID"
ENV_STOP_TOKEN = "OMD_STOP_TOKEN"
ENV_SERVER_PORT = "OMD_SERVER_PORT"
ENV_PROJECT_LOCK_FD = "OMD_PROJECT_LOCK_FD"
ENV_RUNTIME_DIR = "OMD_RUNTIME_DIR"

STOP_TOKEN_HEADER = "X-Original-Media-Stop-Token"
DEFAULT_SERVER_PORT = 8766
DEFAULT_RUNTIME_DIR = PROJECT_ROOT / "data" / "runtime"
DEFAULT_PROJECT_LOCK_PATH = DEFAULT_RUNTIME_DIR / "project.lock"
RUNTIME_RECORD_MAX_BYTES = 64 * 1024
RUNTIME_STOP_EVENT = threading.Event()
_RUNTIME_IDENTITY_LOCK = threading.Lock()
_runtime_instance_id = "unmanaged"
_runtime_stop_token = ""
_runtime_server_port = DEFAULT_SERVER_PORT

_WINDOWS_ERROR_ALREADY_EXISTS = 183
_WINDOWS_HANDLE_FLAG_INHERIT = 0x00000001
_WINDOWS_EVENT_ACCESS = 0x00100002
_WINDOWS_SYNCHRONIZE = 0x00100000
_WINDOWS_WAIT_TIMEOUT = 0x00000102

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
        "port",
        "project_root",
        "started_at",
    }
)


class RuntimeErrorBase(RuntimeError):
    """Base class for safe local runtime coordination failures."""


class ProjectLockHeldError(RuntimeErrorBase):
    """Raised when another process currently owns the project lock."""


class InvalidRuntimeRecordError(RuntimeErrorBase):
    """Raised when a runtime record exists but cannot be trusted."""


def configure_runtime_identity(
    *,
    instance_id: str,
    stop_token: str,
    server_port: int,
) -> None:
    if not _INSTANCE_ID.fullmatch(instance_id):
        raise RuntimeErrorBase("The runtime instance ID is invalid")
    if not _STOP_TOKEN.fullmatch(stop_token):
        raise RuntimeErrorBase("The runtime stop token is invalid")
    _validated_port(server_port)
    global _runtime_instance_id, _runtime_stop_token, _runtime_server_port
    with _RUNTIME_IDENTITY_LOCK:
        _runtime_instance_id = instance_id
        _runtime_stop_token = stop_token
        _runtime_server_port = server_port
        RUNTIME_STOP_EVENT.clear()


def current_runtime_identity() -> tuple[str, str, int]:
    with _RUNTIME_IDENTITY_LOCK:
        return (
            _runtime_instance_id,
            _runtime_stop_token,
            _runtime_server_port,
        )


def clear_runtime_identity(*, instance_id: str) -> bool:
    global _runtime_instance_id, _runtime_stop_token, _runtime_server_port
    with _RUNTIME_IDENTITY_LOCK:
        if _runtime_instance_id != instance_id:
            return False
        _runtime_instance_id = "unmanaged"
        _runtime_stop_token = ""
        _runtime_server_port = DEFAULT_SERVER_PORT
        RUNTIME_STOP_EVENT.clear()
        return True


def _require_plain_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise InvalidRuntimeRecordError(f"Runtime record {name} is invalid")
    return value


def _require_identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _SAFE_IDENTIFIER.fullmatch(value):
        raise InvalidRuntimeRecordError(f"Runtime record {name} is invalid")
    return value


def _require_project_root(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        raise InvalidRuntimeRecordError("Runtime record project_root is invalid")
    path = Path(value)
    if not path.is_absolute():
        raise InvalidRuntimeRecordError("Runtime record project_root is not absolute")
    canonical = str(path.resolve(strict=False))
    if canonical != value or path == Path(path.anchor):
        raise InvalidRuntimeRecordError("Runtime record project_root is not canonical")
    return value


def _require_started_at(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise InvalidRuntimeRecordError("Runtime record started_at is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidRuntimeRecordError(
            "Runtime record started_at is invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidRuntimeRecordError(
            "Runtime record started_at must include a timezone"
        )
    return value


@dataclass(frozen=True, slots=True)
class RuntimeRecord:
    app_id: str
    build_id: str
    instance_id: str
    stop_token: str
    pid: int
    port: int
    project_root: str
    started_at: str

    def __post_init__(self) -> None:
        _require_identifier(self.app_id, "app_id")
        _require_identifier(self.build_id, "build_id")
        if not isinstance(self.instance_id, str) or not _INSTANCE_ID.fullmatch(
            self.instance_id
        ):
            raise InvalidRuntimeRecordError("Runtime record instance_id is invalid")
        if not isinstance(self.stop_token, str) or not _STOP_TOKEN.fullmatch(
            self.stop_token
        ):
            raise InvalidRuntimeRecordError("Runtime record stop_token is invalid")
        _require_plain_int(self.pid, "pid", 1, 2**31 - 1)
        _require_plain_int(self.port, "port", 1, 65_535)
        _require_project_root(self.project_root)
        _require_started_at(self.started_at)

    @classmethod
    def from_dict(cls, value: Any) -> RuntimeRecord:
        if not isinstance(value, dict) or set(value) != _EXPECTED_RECORD_FIELDS:
            raise InvalidRuntimeRecordError(
                "Runtime record fields are missing or unexpected"
            )
        return cls(**value)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _validated_port(port: int) -> int:
    if type(port) is not int or not 1 <= port <= 65_535:
        raise ValueError("port must be between 1 and 65535")
    return port


def record_path(runtime_dir: str | Path, port: int) -> Path:
    return Path(runtime_dir).expanduser().resolve() / f"runtime-{_validated_port(port)}.json"


def _lock_posix(fd: int, *, blocking: bool) -> None:
    import fcntl

    flags = fcntl.LOCK_EX
    if not blocking:
        flags |= fcntl.LOCK_NB
    try:
        fcntl.flock(fd, flags)
    except BlockingIOError as exc:
        raise ProjectLockHeldError("Another launcher owns the project lock") from exc


def _unlock_posix(fd: int) -> None:
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


def _windows_kernel32():
    import ctypes

    return ctypes.WinDLL("kernel32", use_last_error=True)


def _windows_event_name(path: Path) -> str:
    canonical = str(path).replace("/", "\\").casefold().encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    return f"Local\\OriginalMediaDownloader-{digest}"


def _create_windows_lock_event(path: Path) -> int:
    import ctypes
    from ctypes import wintypes

    kernel32 = _windows_kernel32()
    kernel32.CreateEventW.argtypes = [
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    ctypes.set_last_error(0)
    handle = kernel32.CreateEventW(
        None,
        True,
        False,
        _windows_event_name(path),
    )
    if not handle:
        raise RuntimeErrorBase(
            f"Could not create the Windows project lock ({ctypes.get_last_error()})"
        )
    if ctypes.get_last_error() == _WINDOWS_ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        raise ProjectLockHeldError("Another launcher owns the project lock")
    return int(handle)


def _adopt_windows_lock_event(path: Path, inherited_handle: int) -> int:
    import ctypes
    from ctypes import wintypes

    kernel32 = _windows_kernel32()
    kernel32.GetHandleInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetHandleInformation.restype = wintypes.BOOL
    kernel32.OpenEventW.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    flags = wintypes.DWORD()
    if not kernel32.GetHandleInformation(
        wintypes.HANDLE(inherited_handle),
        ctypes.byref(flags),
    ):
        raise RuntimeErrorBase("The inherited Windows project lock is invalid")
    handle = kernel32.OpenEventW(
        _WINDOWS_EVENT_ACCESS,
        False,
        _windows_event_name(path),
    )
    if not handle:
        raise RuntimeErrorBase(
            "The inherited Windows project lock object no longer exists"
        )
    kernel32.CloseHandle(wintypes.HANDLE(inherited_handle))
    return int(handle)


def _close_windows_handle(handle: int) -> None:
    from ctypes import wintypes

    kernel32 = _windows_kernel32()
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle(wintypes.HANDLE(handle))


def set_project_lock_inheritable(handle: int, inheritable: bool) -> None:
    if os.name != "nt":
        os.set_inheritable(handle, inheritable)
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = _windows_kernel32()
    kernel32.SetHandleInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.SetHandleInformation.restype = wintypes.BOOL
    flags = _WINDOWS_HANDLE_FLAG_INHERIT if inheritable else 0
    if not kernel32.SetHandleInformation(
        wintypes.HANDLE(handle),
        _WINDOWS_HANDLE_FLAG_INHERIT,
        flags,
    ):
        raise RuntimeErrorBase(
            f"Could not update Windows project lock inheritance "
            f"({ctypes.get_last_error()})"
        )


class ParentProcessMonitor:
    """Keep an identity-stable watch on the process that launched this one."""

    def __init__(self, pid: int) -> None:
        if type(pid) is not int or pid <= 1:
            raise ValueError("parent PID must be greater than 1")
        self.pid = pid
        self._handle: int | None = None
        self._missing = False
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel32 = _windows_kernel32()
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            handle = kernel32.OpenProcess(_WINDOWS_SYNCHRONIZE, False, pid)
            if handle:
                self._handle = int(handle)
            else:
                self._missing = True

    def disappeared(self) -> bool:
        if os.name != "nt":
            return os.getppid() != self.pid
        if self._missing or self._handle is None:
            return True
        from ctypes import wintypes

        kernel32 = _windows_kernel32()
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        result = kernel32.WaitForSingleObject(
            wintypes.HANDLE(self._handle),
            0,
        )
        return result != _WINDOWS_WAIT_TIMEOUT

    def close(self) -> None:
        if self._handle is not None:
            _close_windows_handle(self._handle)
            self._handle = None

    def __enter__(self) -> ParentProcessMonitor:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class ProjectLock:
    """A kernel-owned lock that never trusts lock-file existence by itself."""

    def __init__(self, path: str | Path) -> None:
        expanded = Path(path).expanduser()
        if not expanded.is_absolute():
            expanded = Path.cwd() / expanded
        self.path = expanded.parent.resolve() / expanded.name
        self.fd: int | None = None
        self._unlock_on_release = True
        self._windows_named_event = False

    @classmethod
    def from_inherited_fd(cls, path: str | Path, fd: int) -> ProjectLock:
        if type(fd) is not int or fd < 0:
            raise RuntimeErrorBase("The inherited project lock FD is invalid")
        lock = cls(path)
        if os.name == "nt":
            try:
                metadata = os.stat(lock.path, follow_symlinks=False)
            except OSError as exc:
                raise RuntimeErrorBase(
                    f"The inherited project lock could not be inspected: {exc}"
                ) from exc
            if not stat.S_ISREG(metadata.st_mode):
                raise RuntimeErrorBase(
                    "The inherited project lock is not a regular file"
                )
            lock.fd = _adopt_windows_lock_event(lock.path, fd)
            lock._windows_named_event = True
            lock._unlock_on_release = False
            return lock
        try:
            descriptor_metadata = os.fstat(fd)
            path_metadata = os.stat(lock.path, follow_symlinks=False)
        except OSError as exc:
            raise RuntimeErrorBase(
                f"The inherited project lock could not be inspected: {exc}"
            ) from exc
        if not stat.S_ISREG(descriptor_metadata.st_mode) or not stat.S_ISREG(
            path_metadata.st_mode
        ):
            raise RuntimeErrorBase("The inherited project lock is not a regular file")
        if (descriptor_metadata.st_dev, descriptor_metadata.st_ino) != (
            path_metadata.st_dev,
            path_metadata.st_ino,
        ):
            raise RuntimeErrorBase(
                "The inherited project lock FD does not match the lock path"
            )
        if os.name != "nt":
            # flock is tied to the inherited open-file description. Reapplying the
            # same non-blocking lock both verifies usability and preserves ownership.
            _lock_posix(fd, blocking=False)
        os.set_inheritable(fd, False)
        lock.fd = fd
        # Explicit LOCK_UN on an inherited open-file description could also release
        # the launcher's ownership. Closing this reference is sufficient instead.
        lock._unlock_on_release = False
        return lock

    def acquire(self, blocking: bool = False) -> ProjectLock:
        if self.fd is not None:
            raise RuntimeErrorBase("The project lock is already acquired")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing_metadata = os.lstat(self.path)
        except FileNotFoundError:
            existing_metadata = None
        except OSError as exc:
            raise RuntimeErrorBase(f"Could not inspect project lock: {exc}") from exc
        if existing_metadata is not None and not stat.S_ISREG(
            existing_metadata.st_mode
        ):
            raise RuntimeErrorBase("The project lock is not a regular file")
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise RuntimeErrorBase(f"Could not open project lock: {exc}") from exc
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise RuntimeErrorBase("The project lock is not a regular file")
            if existing_metadata is not None and (
                existing_metadata.st_dev,
                existing_metadata.st_ino,
            ) != (metadata.st_dev, metadata.st_ino):
                raise RuntimeErrorBase("The project lock changed while it was opened")
            if metadata.st_size == 0:
                os.write(fd, b"\0")
                os.fsync(fd)
            if os.name == "nt":
                os.close(fd)
                fd = -1
                self.fd = _create_windows_lock_event(self.path)
                self._windows_named_event = True
                self._unlock_on_release = False
                return self
            os.lseek(fd, 0, os.SEEK_SET)
            _lock_posix(fd, blocking=blocking)
            os.set_inheritable(fd, False)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            raise
        self.fd = fd
        self._unlock_on_release = True
        return self

    def release(self) -> None:
        fd = self.fd
        if fd is None:
            return
        self.fd = None
        if self._windows_named_event:
            _close_windows_handle(fd)
            return
        try:
            if self._unlock_on_release:
                _unlock_posix(fd)
        finally:
            os.close(fd)

    def __enter__(self) -> ProjectLock:
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()


def _runtime_path(runtime_dir: str | Path, port: int | None) -> Path:
    if port is not None:
        return record_path(runtime_dir, port)
    path = Path(runtime_dir).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.parent.resolve() / path.name
    if path.suffix.lower() != ".json":
        raise ValueError("port is required when a runtime directory is provided")
    return path


def _read_regular_file(path: Path) -> bytes | None:
    try:
        path_metadata = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InvalidRuntimeRecordError(
            f"Runtime record could not be inspected: {exc}"
        ) from exc
    if not stat.S_ISREG(path_metadata.st_mode):
        raise InvalidRuntimeRecordError("Runtime record is not a regular file")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InvalidRuntimeRecordError(
            f"Runtime record could not be opened: {exc}"
        ) from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise InvalidRuntimeRecordError("Runtime record is not a regular file")
        if (path_metadata.st_dev, path_metadata.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise InvalidRuntimeRecordError(
                "Runtime record changed while it was opened"
            )
        if metadata.st_size > RUNTIME_RECORD_MAX_BYTES:
            raise InvalidRuntimeRecordError("Runtime record is too large")
        chunks: list[bytes] = []
        remaining = RUNTIME_RECORD_MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(16_384, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > RUNTIME_RECORD_MAX_BYTES:
            raise InvalidRuntimeRecordError("Runtime record is too large")
        return payload
    finally:
        os.close(fd)


def read_runtime_record(
    runtime_dir: str | Path,
    port: int | None = None,
) -> RuntimeRecord | None:
    path = _runtime_path(runtime_dir, port)
    payload = _read_regular_file(path)
    if payload is None:
        return None
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidRuntimeRecordError("Runtime record is not valid JSON") from exc
    return RuntimeRecord.from_dict(value)


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


def write_runtime_record(
    runtime_dir: str | Path,
    record: RuntimeRecord,
) -> Path:
    # Revalidate instances created through unusual dataclass construction paths.
    validated = RuntimeRecord.from_dict(record.to_dict())
    path = record_path(runtime_dir, validated.port)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
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


def remove_runtime_record(
    runtime_dir: str | Path,
    port: int,
    *,
    instance_id: str,
) -> bool:
    if not isinstance(instance_id, str) or not _INSTANCE_ID.fullmatch(instance_id):
        raise ValueError("instance_id must be 32 lowercase hexadecimal characters")
    path = record_path(runtime_dir, port)
    record = read_runtime_record(runtime_dir, port)
    if record is None or record.instance_id != instance_id:
        return False
    try:
        current = read_runtime_record(runtime_dir, port)
        if current is None or current.instance_id != instance_id:
            return False
        path.unlink()
    except FileNotFoundError:
        return False
    _fsync_directory(path.parent)
    return True


__all__ = [
    "DEFAULT_RUNTIME_DIR",
    "ENV_INSTANCE_ID",
    "ENV_PROJECT_LOCK_FD",
    "ENV_RUNTIME_DIR",
    "ENV_SERVER_PORT",
    "ENV_STOP_TOKEN",
    "InvalidRuntimeRecordError",
    "ProjectLock",
    "ProjectLockHeldError",
    "RUNTIME_RECORD_MAX_BYTES",
    "RUNTIME_STOP_EVENT",
    "RuntimeErrorBase",
    "RuntimeRecord",
    "STOP_TOKEN_HEADER",
    "ParentProcessMonitor",
    "clear_runtime_identity",
    "configure_runtime_identity",
    "current_runtime_identity",
    "read_runtime_record",
    "record_path",
    "remove_runtime_record",
    "set_project_lock_inheritable",
    "write_runtime_record",
]
