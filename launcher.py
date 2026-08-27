from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.build_info import APP_ID, APP_VERSION, BUILD_ID, PROJECT_ROOT
from app.launcher_control import LauncherControl, LauncherControlError
from app.process_guard import stop_process_group
from app.runtime import (
    DEFAULT_RUNTIME_DIR,
    ENV_INSTANCE_ID,
    ENV_PROJECT_LOCK_FD,
    ENV_RUNTIME_DIR,
    ENV_SERVER_PORT,
    ENV_STOP_TOKEN,
    InvalidRuntimeRecordError,
    ParentProcessMonitor,
    ProjectLock,
    ProjectLockHeldError,
    RuntimeErrorBase,
    RuntimeRecord,
    STOP_TOKEN_HEADER,
    read_runtime_record,
    remove_runtime_record,
    set_project_lock_inheritable,
)


MINIMUM_PYTHON = (3, 10)
PARENT_POLL_SECONDS = 0.25
PROCESS_STOP_TIMEOUT_SECONDS = 20.0
BACKEND_STARTUP_STOP_GRACE_SECONDS = 10.0
BACKEND_GRACEFUL_STOP_TIMEOUT_SECONDS = 120.0
BACKEND_FORCE_STOP_TIMEOUT_SECONDS = 5.0
PROCESS_GUARD_PATH = Path(__file__).resolve().parent / "app" / "process_guard.py"
_WINDOWS_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_WINDOWS_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
_WINDOWS_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class LauncherError(RuntimeError):
    pass


class LauncherCancelled(LauncherError):
    pass


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _positive_pid(value: str) -> int:
    try:
        pid = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("parent PID must be an integer") from exc
    if not 1 <= pid <= 2**31 - 1:
        raise argparse.ArgumentTypeError("parent PID is outside the supported range")
    return pid


def _venv_python(project_root: Path) -> Path:
    if os.name == "nt":
        return project_root / ".venv" / "Scripts" / "python.exe"
    return project_root / ".venv" / "bin" / "python"


def _command_exit_code(returncode: int) -> int:
    return returncode if returncode >= 0 else 128 + abs(returncode)


def _popen_with_project_lock(
    command: Sequence[str],
    *,
    lock_fd: int,
    extra_inherited_handles: Sequence[int] = (),
    **options: Any,
) -> subprocess.Popen[Any]:
    inherited_handles = tuple(dict.fromkeys((lock_fd, *extra_inherited_handles)))
    if os.name != "nt":
        return subprocess.Popen(
            list(command),
            pass_fds=inherited_handles,
            **options,
        )

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.lpAttributeList = {"handle_list": list(inherited_handles)}
    for handle in inherited_handles:
        set_project_lock_inheritable(handle, True)
    try:
        return subprocess.Popen(
            list(command),
            close_fds=True,
            startupinfo=startupinfo,
            **options,
        )
    finally:
        for handle in reversed(inherited_handles):
            set_project_lock_inheritable(handle, False)


def _assign_windows_backend_job(process: subprocess.Popen[Any]) -> None:
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    class JobObjectBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JobObjectExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JobObjectBasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise LauncherError(
            f"Could not create the backend job ({ctypes.get_last_error()})"
        )
    information = JobObjectExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = (
        _WINDOWS_JOB_OBJECT_LIMIT_BREAKAWAY_OK
        | _WINDOWS_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    configured = kernel32.SetInformationJobObject(
        job,
        _WINDOWS_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(information),
        ctypes.sizeof(information),
    )
    assigned = configured and kernel32.AssignProcessToJobObject(
        job,
        wintypes.HANDLE(process._handle),
    )
    if not assigned:
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise LauncherError(f"Could not assign the backend job ({error})")
    process._omd_job_handle = int(job)


def _close_windows_backend_job(process: subprocess.Popen[Any]) -> None:
    handle = getattr(process, "_omd_job_handle", None)
    if handle is None:
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle(wintypes.HANDLE(handle))
    process._omd_job_handle = None


def _force_stop_backend_tree(process: subprocess.Popen[Any]) -> None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        handle = getattr(process, "_omd_job_handle", None)
        if handle is not None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel32.TerminateJobObject.restype = wintypes.BOOL
            kernel32.TerminateJobObject(wintypes.HANDLE(handle), 1)
        elif process.poll() is None:
            process.kill()
        if process.poll() is None:
            try:
                process.wait(timeout=BACKEND_FORCE_STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as exc:
                raise LauncherError("The backend process tree did not stop") from exc
        return

    try:
        stop_process_group(
            process,
            term_grace_seconds=BACKEND_FORCE_STOP_TIMEOUT_SECONDS,
            kill_grace_seconds=BACKEND_FORCE_STOP_TIMEOUT_SECONDS,
        )
    except (OSError, RuntimeError) as exc:
        raise LauncherError("The backend process tree did not stop") from exc


def _cleanup_finished_process_group(process: subprocess.Popen[Any]) -> None:
    if os.name == "nt" or not getattr(process, "_omd_process_group", False):
        return
    _force_stop_backend_tree(process)


def _wait_for_managed_backend_exit(process: subprocess.Popen[Any]) -> None:
    try:
        process.wait(timeout=BACKEND_GRACEFUL_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _force_stop_backend_tree(process)
    finally:
        _cleanup_finished_process_group(process)


def _terminate_owned_process(
    process: subprocess.Popen[Any],
    *,
    timeout: float = PROCESS_STOP_TIMEOUT_SECONDS,
) -> None:
    if getattr(process, "_omd_process_group", False):
        _force_stop_backend_tree(process)
        _close_windows_backend_job(process)
        return
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    # The Popen object still owns an unreaped child here. No PID or name lookup is used.
    process.kill()
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired as exc:
        raise LauncherError("The owned child process did not stop") from exc


def _wait_for_owned_process(
    process: subprocess.Popen[Any],
    *,
    stop_requested: threading.Event,
    initial_parent_pid: int,
    parent_monitor: ParentProcessMonitor | None = None,
    graceful_stop: Callable[[], bool] | None = None,
    poll_seconds: float = PARENT_POLL_SECONDS,
) -> tuple[int, bool]:
    owns_monitor = parent_monitor is None
    monitor = parent_monitor or ParentProcessMonitor(initial_parent_pid)
    try:
        while True:
            returncode = process.poll()
            if returncode is not None:
                _cleanup_finished_process_group(process)
                return returncode, False
            if stop_requested.is_set() or monitor.disappeared():
                if graceful_stop is not None and graceful_stop():
                    _wait_for_managed_backend_exit(process)
                else:
                    _terminate_owned_process(process)
                return int(process.returncode or 0), True
            stop_requested.wait(poll_seconds)
    finally:
        if owns_monitor:
            monitor.close()


@contextlib.contextmanager
def _shutdown_signal_handlers(stop_requested: threading.Event) -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    handled = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        handled.append(signal.SIGHUP)
    if hasattr(signal, "SIGBREAK"):
        handled.append(signal.SIGBREAK)
    previous: dict[signal.Signals, Any] = {}

    def request_stop(_: int, __: Any) -> None:
        stop_requested.set()

    try:
        for current in handled:
            previous[current] = signal.getsignal(current)
            signal.signal(current, request_stop)
        yield
    finally:
        for current, handler in previous.items():
            signal.signal(current, handler)


def _run_owned_command(
    command: Sequence[str],
    *,
    cwd: Path,
    stop_requested: threading.Event,
    initial_parent_pid: int,
    parent_monitor: ParentProcessMonitor | None = None,
    lock_fd: int | None = None,
) -> None:
    if stop_requested.is_set() or (
        parent_monitor is not None and parent_monitor.disappeared()
    ):
        raise LauncherCancelled("Startup was cancelled")
    parent_pipe_read: int | None = None
    parent_pipe_write: int | None = None
    process: subprocess.Popen[Any] | None = None
    try:
        if lock_fd is None:
            process = subprocess.Popen(list(command), cwd=cwd)
        else:
            options: dict[str, Any] = {}
            owned_command = list(command)
            extra_inherited_handles: tuple[int, ...] = ()
            if os.name == "nt":
                options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                options["start_new_session"] = True
                parent_pipe_read, parent_pipe_write = os.pipe()
                owned_command = [
                    sys.executable,
                    str(PROCESS_GUARD_PATH),
                    "--parent-pipe-fd",
                    str(parent_pipe_read),
                    "--",
                    *command,
                ]
                extra_inherited_handles = (parent_pipe_read,)
            process = _popen_with_project_lock(
                owned_command,
                cwd=cwd,
                lock_fd=lock_fd,
                extra_inherited_handles=extra_inherited_handles,
                **options,
            )
            process._omd_process_group = True
            try:
                _assign_windows_backend_job(process)
            except BaseException:
                _force_stop_backend_tree(process)
                raise
    except OSError as exc:
        raise LauncherError(f"Could not start {command[0]}: {exc}") from exc
    finally:
        if parent_pipe_read is not None:
            with contextlib.suppress(OSError):
                os.close(parent_pipe_read)
        if process is None and parent_pipe_write is not None:
            with contextlib.suppress(OSError):
                os.close(parent_pipe_write)
    if process is None:
        raise LauncherError(f"Could not start {command[0]}")
    try:
        returncode, stopped = _wait_for_owned_process(
            process,
            stop_requested=stop_requested,
            initial_parent_pid=initial_parent_pid,
            parent_monitor=parent_monitor,
        )
    finally:
        if parent_pipe_write is not None:
            with contextlib.suppress(OSError):
                os.close(parent_pipe_write)
        _close_windows_backend_job(process)
    if stopped:
        raise LauncherCancelled("Startup was cancelled")
    if returncode != 0:
        raise LauncherError(
            f"Command failed with exit code {_command_exit_code(returncode)}: "
            f"{Path(command[0]).name}"
        )


def _python_is_supported(
    executable: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    try:
        completed = runner(
            [
                str(executable),
                "-I",
                "-c",
                (
                    "import sys; raise SystemExit("
                    "sys.version_info < (3, 10))"
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _prepare_virtual_environment(
    project_root: Path,
    *,
    base_python: Path,
    run_command: Callable[[Sequence[str]], None],
) -> Path:
    python = _venv_python(project_root)
    if not python.is_file():
        print("Creating the local Python environment...")
        run_command([str(base_python), "-m", "venv", str(project_root / ".venv")])
    if not python.is_file() or not os.access(python, os.X_OK):
        raise LauncherError("The local Python environment could not be created")
    if not _python_is_supported(python):
        raise LauncherError(
            "The existing .venv uses an unsupported Python version. "
            "Delete .venv and run again."
        )
    requirements = project_root / "requirements.txt"
    if not requirements.is_file():
        raise LauncherError("requirements.txt was not found")
    print("Checking dependencies...")
    run_command(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-q",
            "-r",
            str(requirements),
        ]
    )
    return python


def _backend_environment(
    base: dict[str, str],
    *,
    instance_id: str,
    stop_token: str,
    port: int,
    lock_fd: int,
    runtime_dir: Path,
) -> dict[str, str]:
    environment = dict(base)
    environment.update(
        {
            ENV_INSTANCE_ID: instance_id,
            ENV_STOP_TOKEN: stop_token,
            ENV_SERVER_PORT: str(port),
            ENV_PROJECT_LOCK_FD: str(lock_fd),
            ENV_RUNTIME_DIR: str(runtime_dir),
        }
    )
    return environment


def _spawn_backend(
    python: Path,
    *,
    project_root: Path,
    port: int,
    no_browser: bool,
    environment: dict[str, str],
    lock_fd: int,
) -> subprocess.Popen[Any]:
    command = [
        str(python),
        str(project_root / "run.py"),
        "--port",
        str(port),
        "--exit-with-parent",
        str(os.getpid()),
    ]
    if no_browser:
        command.append("--no-browser")
    options: dict[str, Any] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    try:
        process = _popen_with_project_lock(
            command,
            cwd=project_root,
            env=environment,
            lock_fd=lock_fd,
            **options,
        )
        try:
            _assign_windows_backend_job(process)
        except BaseException:
            _force_stop_backend_tree(process)
            raise
        process._omd_process_group = True
        return process
    except (OSError, LauncherError) as exc:
        raise LauncherError(f"Could not start the backend: {exc}") from exc


def _candidate_records(runtime_dir: Path) -> list[RuntimeRecord]:
    records: list[RuntimeRecord] = []
    if not runtime_dir.is_dir():
        return records
    for path in sorted(runtime_dir.glob("runtime-*.json")):
        try:
            record = read_runtime_record(path)
        except InvalidRuntimeRecordError:
            continue
        if record is not None:
            records.append(record)
    return records


def _fetch_health(port: int, *, timeout: float = 1.0) -> dict[str, Any] | None:
    request = Request(
        f"http://127.0.0.1:{port}/api/health",
        headers={"Accept": "application/json", "Cache-Control": "no-cache"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            payload = json.loads(response.read(16_384).decode("utf-8"))
    except (
        HTTPError,
        URLError,
        OSError,
        TimeoutError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return None
    return payload if isinstance(payload, dict) else None


def _post_runtime_stop(
    port: int,
    *,
    instance_id: str,
    stop_token: str,
    timeout: float = 1.0,
) -> bool:
    request = Request(
        f"http://127.0.0.1:{port}/api/runtime/stop",
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
                return False
            payload = json.loads(response.read(16_384).decode("utf-8"))
    except (
        HTTPError,
        URLError,
        OSError,
        TimeoutError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return False
    return bool(
        isinstance(payload, dict)
        and payload.get("status") == "stopping"
        and payload.get("instance_id") == instance_id
    )


def _backend_pid_matches_process(
    server_pid: object,
    process_pid: int,
    *,
    platform_name: str | None = None,
) -> bool:
    if type(server_pid) is not int or server_pid <= 1:
        return False
    # A Windows venv executable may be a redirector whose PID differs from the
    # Python process serving requests. The random instance ID and stop token
    # still bind the authenticated endpoint to this launch operation.
    if (platform_name or os.name) == "nt":
        return True
    return server_pid == process_pid


def _request_managed_backend_stop(
    process: subprocess.Popen[Any],
    *,
    port: int,
    instance_id: str,
    stop_token: str,
) -> bool:
    deadline = time.monotonic() + BACKEND_STARTUP_STOP_GRACE_SECONDS
    while process.poll() is None and time.monotonic() < deadline:
        health = _fetch_health(port, timeout=0.5)
        if (
            health
            and health.get("status") == "ok"
            and health.get("app_id") == APP_ID
            and health.get("instance_id") == instance_id
            and _backend_pid_matches_process(
                health.get("server_pid"),
                process.pid,
            )
            and health.get("server_port") == port
            and _post_runtime_stop(
                port,
                instance_id=instance_id,
                stop_token=stop_token,
                timeout=1.0,
            )
        ):
            return True
        time.sleep(0.1)
    if process.poll() is not None:
        return True
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.terminate()
        return True
    except (OSError, ValueError):
        return False


def _report_lock_owner(runtime_dir: Path, project_root: Path) -> None:
    expected_root = str(project_root.resolve())
    matching = [
        record
        for record in _candidate_records(runtime_dir)
        if record.app_id == APP_ID and record.project_root == expected_root
    ]
    for record in matching:
        health = _fetch_health(record.port)
        if health and health.get("app_id") == APP_ID:
            print(
                f"Original Media Downloader is already running at "
                f"http://127.0.0.1:{record.port}."
            )
            return
    print(
        "Original Media Downloader is already starting, installing, running, "
        "or shutting down. No process was stopped."
    )


def _launch(
    *,
    project_root: Path,
    runtime_dir: Path,
    port: int,
    no_browser: bool,
    base_python: Path,
    stop_requested: threading.Event,
    initial_parent_pid: int,
) -> int:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    lock = ProjectLock(project_root / "data" / "runtime" / "project.lock")
    try:
        lock.acquire(blocking=False)
    except ProjectLockHeldError:
        _report_lock_owner(runtime_dir, project_root)
        return 2

    instance_id = secrets.token_hex(16)
    stop_token = secrets.token_urlsafe(32)
    backend: subprocess.Popen[Any] | None = None
    parent_monitor: ParentProcessMonitor | None = None
    control: LauncherControl | None = None
    try:
        parent_monitor = ParentProcessMonitor(initial_parent_pid)
        control = LauncherControl(
            runtime_dir=runtime_dir,
            app_id=APP_ID,
            build_id=BUILD_ID,
            instance_id=instance_id,
            stop_token=stop_token,
            target_port=port,
            project_root=project_root,
            stop_requested=stop_requested,
        )
        control.start()
        if lock.fd is None:
            raise LauncherError("The project lock did not provide a file descriptor")

        def run_command(command: Sequence[str]) -> None:
            _run_owned_command(
                command,
                cwd=project_root,
                stop_requested=stop_requested,
                initial_parent_pid=initial_parent_pid,
                parent_monitor=parent_monitor,
                lock_fd=lock.fd,
            )

        python = _prepare_virtual_environment(
            project_root,
            base_python=base_python,
            run_command=run_command,
        )
        if stop_requested.is_set() or parent_monitor.disappeared():
            raise LauncherCancelled("Startup was cancelled")
        environment = _backend_environment(
            os.environ,
            instance_id=instance_id,
            stop_token=stop_token,
            port=port,
            lock_fd=lock.fd,
            runtime_dir=runtime_dir,
        )
        backend = _spawn_backend(
            python,
            project_root=project_root,
            port=port,
            no_browser=no_browser,
            environment=environment,
            lock_fd=lock.fd,
        )
        print(
            f"Starting Original Media Downloader {APP_VERSION} ({BUILD_ID}) "
            f"at http://127.0.0.1:{port}"
        )
        returncode, stopped = _wait_for_owned_process(
            backend,
            stop_requested=stop_requested,
            initial_parent_pid=initial_parent_pid,
            parent_monitor=parent_monitor,
            graceful_stop=lambda: _request_managed_backend_stop(
                backend,
                port=port,
                instance_id=instance_id,
                stop_token=stop_token,
            ),
        )
        return 0 if stopped else _command_exit_code(returncode)
    finally:
        original_error_active = sys.exc_info()[0] is not None
        cleanup_errors: list[BaseException] = []
        if backend is not None:
            try:
                if backend.poll() is None:
                    if _request_managed_backend_stop(
                        backend,
                        port=port,
                        instance_id=instance_id,
                        stop_token=stop_token,
                    ):
                        _wait_for_managed_backend_exit(backend)
                    else:
                        _force_stop_backend_tree(backend)
                _cleanup_finished_process_group(backend)
            except BaseException as exc:
                cleanup_errors.append(exc)
        with contextlib.suppress(InvalidRuntimeRecordError, OSError):
            remove_runtime_record(
                runtime_dir,
                port,
                instance_id=instance_id,
            )
        if control is not None:
            try:
                control.close()
            except BaseException as exc:
                cleanup_errors.append(exc)
        if parent_monitor is not None:
            try:
                parent_monitor.close()
            except BaseException as exc:
                cleanup_errors.append(exc)
        if backend is not None:
            try:
                _close_windows_backend_job(backend)
            except BaseException as exc:
                cleanup_errors.append(exc)
        try:
            lock.release()
        except BaseException as exc:
            cleanup_errors.append(exc)
        if cleanup_errors and not original_error_active:
            raise cleanup_errors[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare and supervise the Original Media Downloader"
    )
    parser.add_argument("--port", default=8765, type=_port)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--parent-pid", type=_positive_pid)
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=DEFAULT_RUNTIME_DIR,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    project_root = PROJECT_ROOT.resolve()
    runtime_dir = args.runtime_dir.expanduser().resolve()
    stop_requested = threading.Event()
    initial_parent_pid = args.parent_pid or os.getppid()
    if sys.version_info < MINIMUM_PYTHON:
        print("Python 3.10 or newer is required.")
        return 1
    try:
        with _shutdown_signal_handlers(stop_requested):
            return _launch(
                project_root=project_root,
                runtime_dir=runtime_dir,
                port=args.port,
                no_browser=args.no_browser,
                base_python=Path(sys.executable).resolve(),
                stop_requested=stop_requested,
                initial_parent_pid=initial_parent_pid,
            )
    except LauncherCancelled:
        return 0
    except LauncherError as exc:
        print(str(exc))
        return 1
    except (
        OSError,
        InvalidRuntimeRecordError,
        LauncherControlError,
        RuntimeErrorBase,
    ) as exc:
        print(f"Launcher stopped safely: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
