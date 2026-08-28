from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.build_info import APP_ID
from app.launcher_control import (
    InvalidLauncherRecordError,
    LauncherRecord,
    read_launcher_record,
    remove_launcher_record,
)
from app.runtime import (
    DEFAULT_SERVER_PORT,
    STOP_TOKEN_HEADER,
    InvalidRuntimeRecordError,
    RuntimeRecord,
    read_runtime_record,
    record_path,
    remove_runtime_record,
)


HOST = "127.0.0.1"
LEGACY_PORT = 8765
DEFAULT_STOP_TIMEOUT_SECONDS = 150.0
PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_RUNTIME_DIR = PROJECT_ROOT / "data" / "runtime"
_MAX_RESPONSE_BYTES = 16_384
_RUN_PY_TOKEN = re.compile(r"(?:^|[\\/])run\.py$")
_WINDOWS_ERROR_INVALID_PARAMETER = 87
_WINDOWS_SYNCHRONIZE = 0x00100000
_WINDOWS_WAIT_OBJECT_0 = 0x00000000
_WINDOWS_WAIT_TIMEOUT = 0x00000102


class StopRefusedError(RuntimeError):
    """Raised when the target cannot be proven to be this project."""


class StopTimeoutError(RuntimeError):
    """Raised when a verified target does not exit before the deadline."""


@dataclass(frozen=True)
class LegacyCandidate:
    pid: int
    uid: int
    started_at: str
    command: str
    cwd: Path
    build_id: str | None


def _server_url(port: int, path: str) -> str:
    return f"http://{HOST}:{port}{path}"


def _launcher_url(port: int, path: str) -> str:
    return f"http://{HOST}:{port}{path}"


def _read_json_response(response: Any) -> dict[str, Any] | None:
    try:
        raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            return None
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _fetch_health(port: int, *, timeout: float) -> dict[str, Any] | None:
    request = Request(
        _server_url(port, "/api/health"),
        headers={"Accept": "application/json", "Cache-Control": "no-cache"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            return _read_json_response(response)
    except (HTTPError, URLError, OSError, TimeoutError):
        return None


def _post_runtime_stop(
    port: int,
    *,
    stop_token: str,
    instance_id: str,
    timeout: float,
) -> dict[str, Any]:
    request = Request(
        _server_url(port, "/api/runtime/stop"),
        data=b"{}",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            STOP_TOKEN_HEADER: stop_token,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise StopRefusedError(
                    f"The runtime stop endpoint returned HTTP {response.status}."
                )
            payload = _read_json_response(response)
    except HTTPError as exc:
        raise StopRefusedError(
            f"The runtime stop endpoint rejected the request (HTTP {exc.code})."
        ) from exc
    except (URLError, OSError, TimeoutError) as exc:
        raise StopRefusedError(
            "The verified backend did not accept the authenticated stop request."
        ) from exc
    if (
        payload is None
        or payload.get("status") != "stopping"
        or payload.get("instance_id") != instance_id
    ):
        raise StopRefusedError(
            "The runtime stop endpoint returned an unexpected response."
        )
    return payload


def _fetch_launcher_health(
    control_port: int,
    *,
    timeout: float,
) -> dict[str, Any] | None:
    request = Request(
        _launcher_url(control_port, "/health"),
        headers={"Accept": "application/json", "Cache-Control": "no-cache"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            return _read_json_response(response)
    except (HTTPError, URLError, OSError, TimeoutError):
        return None


def _post_launcher_stop(
    record: LauncherRecord,
    *,
    timeout: float,
) -> dict[str, Any]:
    request = Request(
        _launcher_url(record.control_port, "/stop"),
        data=b"{}",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            STOP_TOKEN_HEADER: record.stop_token,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise StopRefusedError(
                    f"The launcher stop endpoint returned HTTP {response.status}."
                )
            payload = _read_json_response(response)
    except HTTPError as exc:
        raise StopRefusedError(
            f"The launcher stop endpoint rejected the request (HTTP {exc.code})."
        ) from exc
    except (URLError, OSError, TimeoutError) as exc:
        raise StopRefusedError(
            "The verified launcher did not accept the authenticated stop request."
        ) from exc
    if (
        payload is None
        or payload.get("status") != "stopping"
        or payload.get("instance_id") != record.instance_id
    ):
        raise StopRefusedError(
            "The launcher stop endpoint returned an unexpected response."
        )
    return payload


def _validate_launcher_record(
    record: LauncherRecord,
    *,
    port: int,
    project_root: Path,
) -> None:
    if record.app_id != APP_ID:
        raise StopRefusedError("The launcher record belongs to a different app.")
    if record.pid <= 1 or record.pid == os.getpid():
        raise StopRefusedError("The launcher record contains an unsafe process ID.")
    if record.target_port != port:
        raise StopRefusedError("The launcher record targets a different port.")
    if not _same_project_root(record.project_root, project_root):
        raise StopRefusedError("The launcher record belongs to a different project.")


def _launcher_health_matches_record(
    health: dict[str, Any] | None,
    record: LauncherRecord,
) -> bool:
    return bool(
        health
        and health.get("status") == "starting"
        and health.get("app_id") == record.app_id
        and health.get("build_id") == record.build_id
        and health.get("instance_id") == record.instance_id
        and health.get("pid") == record.pid
        and health.get("target_port") == record.target_port
        and health.get("control_port") == record.control_port
        and health.get("project_root") == record.project_root
    )


def _same_project_root(value: str, expected: Path) -> bool:
    try:
        return Path(value).resolve(strict=True) == expected.resolve(strict=True)
    except (OSError, RuntimeError):
        return False


def _validate_runtime_record(
    record: RuntimeRecord,
    *,
    port: int,
    project_root: Path,
) -> None:
    if record.app_id != APP_ID:
        raise StopRefusedError("The runtime record belongs to a different app.")
    if record.port != port:
        raise StopRefusedError("The runtime record port does not match the request.")
    if record.pid <= 1 or record.pid == os.getpid():
        raise StopRefusedError("The runtime record contains an unsafe process ID.")
    if not _same_project_root(record.project_root, project_root):
        raise StopRefusedError("The runtime record belongs to a different project.")


def _health_matches_record(
    health: dict[str, Any] | None,
    record: RuntimeRecord,
) -> bool:
    if not health or health.get("status") != "ok":
        return False
    return bool(
        health.get("app_id") == APP_ID
        and health.get("build_id") == record.build_id
        and health.get("instance_id") == record.instance_id
        and type(health.get("server_pid")) is int
        and health.get("server_pid") == record.pid
        and type(health.get("server_port")) is int
        and health.get("server_port") == record.port
    )


def _windows_process_handle(pid: int) -> tuple[Any | None, int]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    ctypes.set_last_error(0)
    handle = kernel32.OpenProcess(_WINDOWS_SYNCHRONIZE, False, pid)
    return handle or None, ctypes.get_last_error()


def _close_windows_process_handle(handle: Any) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle(handle)


def _wait_windows_process_handle(handle: Any, timeout_ms: int) -> int:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    return int(kernel32.WaitForSingleObject(handle, timeout_ms))


def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        handle, error = _windows_process_handle(pid)
        if handle is None:
            return error != _WINDOWS_ERROR_INVALID_PARAMETER
        try:
            return _wait_windows_process_handle(handle, 0) != _WINDOWS_WAIT_OBJECT_0
        finally:
            _close_windows_process_handle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if os.name != "nt":
        try:
            completed = subprocess.run(
                ["/bin/ps", "-p", str(pid), "-o", "state="],
                check=False,
                capture_output=True,
                text=True,
                timeout=1.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            return True
        if completed.returncode != 0:
            return False
        state = completed.stdout.strip()
        if state.startswith("Z"):
            return False
    return True


def _wait_for_pid_exit(pid: int, *, timeout: float) -> bool:
    if os.name == "nt":
        handle, error = _windows_process_handle(pid)
        if handle is None:
            return error == _WINDOWS_ERROR_INVALID_PARAMETER
        try:
            timeout_ms = max(0, min(round(timeout * 1000), 0xFFFFFFFE))
            return (
                _wait_windows_process_handle(handle, timeout_ms)
                == _WINDOWS_WAIT_OBJECT_0
            )
        finally:
            _close_windows_process_handle(handle)
    deadline = time.monotonic() + max(0.0, timeout)
    while _pid_exists(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def _run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StopRefusedError(
            f"Could not safely inspect the legacy backend with {command[0]}."
        ) from exc


def _macos_listener_pids(port: int) -> list[int]:
    completed = _run_command(
        [
            "/usr/sbin/lsof",
            "-nP",
            "-a",
            f"-iTCP:{port}",
            "-sTCP:LISTEN",
            "-t",
        ]
    )
    if completed.stderr.strip():
        raise StopRefusedError("lsof could not safely identify the listener.")
    if completed.returncode not in (0, 1):
        raise StopRefusedError("lsof could not safely identify the listener.")
    values: set[int] = set()
    for line in completed.stdout.splitlines():
        value = line.strip()
        if not value:
            continue
        if not value.isascii() or not value.isdecimal():
            raise StopRefusedError("lsof returned an invalid listener process ID.")
        pid = int(value)
        if pid <= 1:
            raise StopRefusedError("lsof returned an unsafe listener process ID.")
        values.add(pid)
    return sorted(values)


def _macos_process_uid(pid: int) -> int:
    completed = _run_command(["/bin/ps", "-p", str(pid), "-o", "uid="])
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value.isascii() or not value.isdecimal():
        raise StopRefusedError("Could not verify the legacy backend owner.")
    return int(value)


def _macos_process_command(pid: int) -> str:
    completed = _run_command(
        ["/bin/ps", "-ww", "-p", str(pid), "-o", "command="]
    )
    command = completed.stdout.strip()
    if completed.returncode != 0 or not command or "\n" in command:
        raise StopRefusedError("Could not verify the legacy backend command.")
    return command


def _macos_process_start_time(pid: int) -> str:
    completed = _run_command(
        ["/bin/ps", "-p", str(pid), "-o", "lstart="]
    )
    started_at = completed.stdout.strip()
    if (
        completed.returncode != 0
        or not started_at
        or "\n" in started_at
        or len(started_at) > 128
    ):
        raise StopRefusedError("Could not verify the legacy backend start time.")
    return started_at


def _macos_process_cwd(pid: int) -> Path:
    completed = _run_command(
        ["/usr/sbin/lsof", "-nP", "-a", "-p", str(pid), "-d", "cwd", "-Fn"]
    )
    if completed.returncode != 0 or completed.stderr.strip():
        raise StopRefusedError("Could not verify the legacy backend directory.")
    paths = [line[1:] for line in completed.stdout.splitlines() if line.startswith("n")]
    if len(paths) != 1 or not paths[0]:
        raise StopRefusedError("Could not uniquely verify the legacy backend directory.")
    try:
        return Path(paths[0]).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise StopRefusedError(
            "Could not resolve the legacy backend directory."
        ) from exc


def _command_runs_project(command: str, *, cwd: Path, project_root: Path) -> bool:
    try:
        arguments = shlex.split(command)
    except ValueError:
        return False
    expected = project_root / "run.py"
    try:
        expected = expected.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    for argument in arguments:
        if not _RUN_PY_TOKEN.search(argument):
            continue
        candidate = Path(argument)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        try:
            if candidate.resolve(strict=True) == expected:
                return True
        except (OSError, RuntimeError):
            continue
    return False


def _inspect_legacy_macos_backend(
    port: int,
    *,
    project_root: Path,
    request_timeout: float,
) -> LegacyCandidate | None:
    listener_pids = _macos_listener_pids(port)
    if not listener_pids:
        return None
    if len(listener_pids) != 1:
        raise StopRefusedError(
            "The legacy port does not have exactly one listener process."
        )
    pid = listener_pids[0]
    health = _fetch_health(port, timeout=request_timeout)
    if not health or health.get("status") != "ok" or health.get("app_id") != APP_ID:
        raise StopRefusedError(
            "The listener did not identify itself as Original Media Downloader."
        )
    if "instance_id" in health or "server_pid" in health or "server_port" in health:
        raise StopRefusedError(
            "A token-aware backend is missing its runtime record; refusing legacy fallback."
        )
    uid = _macos_process_uid(pid)
    if uid != os.getuid():
        raise StopRefusedError("The legacy backend belongs to a different user.")
    cwd = _macos_process_cwd(pid)
    try:
        expected_root = project_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise StopRefusedError("Could not resolve this project directory.") from exc
    if cwd != expected_root:
        raise StopRefusedError("The legacy backend belongs to a different project.")
    command = _macos_process_command(pid)
    if not _command_runs_project(command, cwd=cwd, project_root=expected_root):
        raise StopRefusedError("The legacy listener command is not this project's run.py.")
    build_id = health.get("build_id")
    return LegacyCandidate(
        pid=pid,
        uid=uid,
        started_at=_macos_process_start_time(pid),
        command=command,
        cwd=cwd,
        build_id=build_id if isinstance(build_id, str) else None,
    )


def _stop_legacy_macos_backend(
    port: int,
    *,
    project_root: Path,
    timeout: float,
) -> str:
    if port != LEGACY_PORT:
        raise StopRefusedError(
            "Legacy fallback is only allowed for the legacy localhost port 8765."
        )
    request_timeout = min(2.0, max(0.1, timeout))
    first = _inspect_legacy_macos_backend(
        port,
        project_root=project_root,
        request_timeout=request_timeout,
    )
    if first is None:
        return "not_running"
    second = _inspect_legacy_macos_backend(
        port,
        project_root=project_root,
        request_timeout=request_timeout,
    )
    if second is None or second != first:
        raise StopRefusedError(
            "The legacy listener changed during verification; it was not signaled."
        )
    if _macos_process_start_time(first.pid) != first.started_at:
        raise StopRefusedError(
            "The legacy listener changed immediately before signaling."
        )
    try:
        os.kill(first.pid, signal.SIGTERM)
    except ProcessLookupError:
        return "stopped"
    except (PermissionError, OSError) as exc:
        raise StopRefusedError("Could not signal the verified legacy backend.") from exc
    if not _wait_for_pid_exit(first.pid, timeout=timeout):
        raise StopTimeoutError(
            "The verified legacy backend did not exit before the timeout."
        )
    remaining = _macos_listener_pids(port)
    if remaining:
        raise StopRefusedError(
            "The verified legacy backend exited, but another process now owns legacy port 8765."
        )
    return "stopped"


def _stop_launcher(
    record: LauncherRecord,
    *,
    port: int,
    project_root: Path,
    timeout: float,
) -> str:
    _validate_launcher_record(record, port=port, project_root=project_root)
    request_timeout = min(2.0, max(0.1, timeout))
    health = _fetch_launcher_health(
        record.control_port,
        timeout=request_timeout,
    )
    if not _launcher_health_matches_record(health, record):
        if not _pid_exists(record.pid):
            return "not_running"
        raise StopRefusedError(
            "The launcher record does not match its local control server."
        )
    _post_launcher_stop(record, timeout=request_timeout)
    if not _wait_for_pid_exit(record.pid, timeout=timeout):
        raise StopTimeoutError(
            "The authenticated launcher did not exit before the timeout."
        )
    return "stopped"


def stop_backend(
    *,
    port: int = DEFAULT_SERVER_PORT,
    runtime_dir: str | Path = DEFAULT_RUNTIME_DIR,
    timeout: float = DEFAULT_STOP_TIMEOUT_SECONDS,
    project_root: Path = PROJECT_ROOT,
    platform_name: str | None = None,
) -> str:
    if not 1 <= port <= 65_535:
        raise ValueError("port must be between 1 and 65535")
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    path = record_path(Path(runtime_dir), port)
    runtime_record_error: Exception | None = None
    try:
        record = read_runtime_record(path)
    except (InvalidRuntimeRecordError, OSError) as exc:
        record = None
        runtime_record_error = exc
    launcher_record_error: Exception | None = None
    try:
        launcher_record = read_launcher_record(runtime_dir)
    except (InvalidLauncherRecordError, OSError) as exc:
        launcher_record = None
        launcher_record_error = exc

    if record is not None:
        _validate_runtime_record(record, port=port, project_root=project_root)
        request_timeout = min(2.0, max(0.1, timeout))
        health = _fetch_health(port, timeout=request_timeout)
        if _health_matches_record(health, record):
            if (
                launcher_record is not None
                and launcher_record.target_port == port
                and launcher_record.instance_id == record.instance_id
                and launcher_record.build_id == record.build_id
                and launcher_record.project_root == record.project_root
                and _launcher_health_matches_record(
                    _fetch_launcher_health(
                        launcher_record.control_port,
                        timeout=request_timeout,
                    ),
                    launcher_record,
                )
            ):
                return _stop_launcher(
                    launcher_record,
                    port=port,
                    project_root=project_root,
                    timeout=timeout,
                )
            _post_runtime_stop(
                port,
                stop_token=record.stop_token,
                instance_id=record.instance_id,
                timeout=request_timeout,
            )
            if not _wait_for_pid_exit(record.pid, timeout=timeout):
                raise StopTimeoutError(
                    "The authenticated backend did not exit before the timeout."
                )
            return "stopped"

        if launcher_record is not None and launcher_record.target_port == port:
            outcome = _stop_launcher(
                launcher_record,
                port=port,
                project_root=project_root,
                timeout=timeout,
            )
            if outcome == "stopped":
                return outcome
            with contextlib.suppress(InvalidLauncherRecordError, OSError):
                remove_launcher_record(
                    runtime_dir,
                    instance_id=launcher_record.instance_id,
                )

        if health is None and not _pid_exists(record.pid):
            with contextlib.suppress(InvalidRuntimeRecordError, OSError):
                remove_runtime_record(
                    runtime_dir,
                    port,
                    instance_id=record.instance_id,
                )
            return "not_running"
        raise StopRefusedError(
            "The runtime record does not match the backend currently on the port."
        )

    if launcher_record is not None and launcher_record.target_port == port:
        outcome = _stop_launcher(
            launcher_record,
            port=port,
            project_root=project_root,
            timeout=timeout,
        )
        if outcome == "not_running":
            with contextlib.suppress(InvalidLauncherRecordError, OSError):
                remove_launcher_record(
                    runtime_dir,
                    instance_id=launcher_record.instance_id,
                )
        else:
            return outcome

    if runtime_record_error is not None:
        raise StopRefusedError(
            "The runtime record is invalid or unreadable; refusing unsafe fallback."
        ) from runtime_record_error
    if launcher_record_error is not None:
        raise StopRefusedError(
            "The launcher record is invalid or unreadable; refusing unsafe fallback."
        ) from launcher_record_error

    probe = _fetch_health(port, timeout=min(1.0, max(0.1, timeout)))
    if probe is None:
        return "not_running"
    platform_name = platform_name or sys.platform
    if platform_name != "darwin":
        raise StopRefusedError(
            "No runtime record exists; legacy fallback is only available on macOS."
        )
    return _stop_legacy_macos_backend(
        port,
        project_root=project_root,
        timeout=timeout,
    )


def _port_argument(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _timeout_argument(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number") from exc
    if timeout <= 0:
        raise argparse.ArgumentTypeError("timeout must be greater than zero")
    return timeout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Safely stop Original Media Downloader"
    )
    parser.add_argument("--port", default=DEFAULT_SERVER_PORT, type=_port_argument)
    parser.add_argument(
        "--runtime-dir",
        default=str(DEFAULT_RUNTIME_DIR),
        help="Directory containing tokenized runtime records",
    )
    parser.add_argument(
        "--timeout",
        default=DEFAULT_STOP_TIMEOUT_SECONDS,
        type=_timeout_argument,
    )
    args = parser.parse_args(argv)
    try:
        outcome = stop_backend(
            port=args.port,
            runtime_dir=args.runtime_dir,
            timeout=args.timeout,
        )
    except (StopRefusedError, StopTimeoutError, ValueError) as exc:
        print(f"Stop refused: {exc}", file=sys.stderr)
        return 2
    if outcome == "not_running":
        print(f"Original Media Downloader is not running on port {args.port}.")
    else:
        print(f"Original Media Downloader stopped on port {args.port}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
