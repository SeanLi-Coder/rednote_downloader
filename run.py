from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import signal
import socket
import sys
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import uvicorn

from app.browser import open_chrome
from app.build_info import APP_ID, APP_VERSION, BUILD_ID
from app.runtime import (
    DEFAULT_RUNTIME_DIR,
    DEFAULT_SERVER_PORT,
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
    RUNTIME_STOP_EVENT,
    clear_runtime_identity,
    configure_runtime_identity,
    read_runtime_record,
    remove_runtime_record,
    write_runtime_record,
)


HOST = "127.0.0.1"
PROJECT_ROOT = Path(__file__).resolve().parent
PARENT_POLL_SECONDS = 0.25
PARENT_FORCE_STOP_SECONDS = 135.0


def _server_url(port: int) -> str:
    return f"http://{HOST}:{port}"


def _launcher_parent_matches(
    expected_pid: int,
    *,
    platform_name: str | None = None,
) -> bool:
    # A Windows venv interpreter can pass through an executable redirector, so
    # getppid() is not a stable launcher identity there. ParentProcessMonitor
    # validates the supplied PID with a process handle before serving requests.
    if (platform_name or os.name) == "nt":
        return True
    return expected_pid == os.getppid()


def _fetch_health(port: int, *, timeout: float = 1.0) -> dict[str, Any] | None:
    request = Request(
        f"{_server_url(port)}/api/health",
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


def _is_current_build(health: dict[str, Any] | None) -> bool:
    return bool(
        health
        and health.get("status") == "ok"
        and health.get("app_id") == APP_ID
        and health.get("version") == APP_VERSION
        and health.get("build_id") == BUILD_ID
        and health.get("source_build_id") == BUILD_ID
        and health.get("restart_required") is False
    )


def _bind_listener(port: int) -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _configure_listener(listener)
    try:
        listener.bind((HOST, port))
        # Reserve the endpoint immediately on platforms where two unlistened
        # SO_REUSEADDR sockets may otherwise bind the same address.
        listener.listen()
    except BaseException:
        listener.close()
        raise
    listener.set_inheritable(False)
    return listener


def _configure_listener(
    listener: socket.socket,
    platform_name: str | None = None,
) -> None:
    platform_name = platform_name or sys.platform
    if platform_name.startswith("win"):
        exclusive_address_use = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive_address_use is not None:
            listener.setsockopt(socket.SOL_SOCKET, exclusive_address_use, 1)
        return
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


def _wait_for_current_build(
    port: int,
    *,
    timeout: float = 20.0,
    poll_interval: float = 0.1,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_current_build(_fetch_health(port, timeout=0.5)):
            return True
        time.sleep(poll_interval)
    return False


def _open_when_ready(port: int) -> None:
    if not _wait_for_current_build(port):
        print("The web UI was not opened because the expected backend did not start.")
        return
    try:
        open_chrome(_server_url(port))
    except RuntimeError as exc:
        print(f"Could not open Chrome automatically: {exc}")


def _handle_occupied_port(port: int, *, no_browser: bool) -> int:
    health = _fetch_health(port)
    if _is_current_build(health):
        print(
            f"Original Media Downloader {APP_VERSION} ({BUILD_ID}) is already "
            f"running at {_server_url(port)}."
        )
        if not no_browser:
            try:
                open_chrome(_server_url(port))
            except RuntimeError as exc:
                print(f"Could not open Chrome automatically: {exc}")
        return 0

    print("")
    print(f"Port {port} is already used by an older or different backend.")
    print("The new backend was not loaded and Chrome will not be opened.")
    print(
        "Stop the previous downloader Terminal with Ctrl+C, or run "
        "stop.command, then start again."
    )
    print(
        f"To inspect the listener on macOS, run: "
        f"lsof -nP -iTCP:{port} -sTCP:LISTEN"
    )
    return 2


def _record_matches_health(
    record: RuntimeRecord,
    health: dict[str, Any] | None,
) -> bool:
    return bool(
        health
        and health.get("status") == "ok"
        and health.get("app_id") == APP_ID
        and health.get("instance_id") == record.instance_id
        and health.get("server_pid") == record.pid
        and health.get("server_port") == record.port
    )


def _handle_occupied_project(
    runtime_dir: Path,
    *,
    no_browser: bool,
) -> int:
    matching: list[tuple[RuntimeRecord, dict[str, Any]]] = []
    if runtime_dir.is_dir():
        for path in sorted(runtime_dir.glob("runtime-*.json")):
            try:
                record = read_runtime_record(path)
            except (InvalidRuntimeRecordError, OSError):
                continue
            if (
                record is None
                or record.app_id != APP_ID
                or record.project_root != str(PROJECT_ROOT.resolve())
            ):
                continue
            health = _fetch_health(record.port)
            if _record_matches_health(record, health):
                matching.append((record, health or {}))

    if len(matching) == 1:
        record, health = matching[0]
        url = _server_url(record.port)
        if _is_current_build(health):
            print(
                f"Original Media Downloader {APP_VERSION} ({BUILD_ID}) is "
                f"already running at {url}."
            )
            if not no_browser:
                try:
                    open_chrome(url)
                except RuntimeError as exc:
                    print(f"Could not open Chrome automatically: {exc}")
            return 0
        print("A different build of Original Media Downloader is still running.")
        print(f"Run stop.command (port {record.port}), then start this build again.")
        return 2

    print(
        "Original Media Downloader is already starting, installing, running, "
        "or shutting down."
    )
    print("Wait a moment or run stop.command if the previous Terminal was lost.")
    return 2


def _project_lock() -> ProjectLock:
    lock_path = PROJECT_ROOT / "data" / "runtime" / "project.lock"
    inherited = os.environ.get(ENV_PROJECT_LOCK_FD)
    if inherited is None:
        return ProjectLock(lock_path).acquire(blocking=False)
    try:
        fd = int(inherited)
    except ValueError as exc:
        raise RuntimeErrorBase("The inherited project lock FD is invalid") from exc
    return ProjectLock.from_inherited_fd(lock_path, fd)


def _managed_identity() -> tuple[str, str]:
    instance_id = os.environ.get(ENV_INSTANCE_ID)
    stop_token = os.environ.get(ENV_STOP_TOKEN)
    if instance_id is None and stop_token is None:
        return secrets.token_hex(16), secrets.token_urlsafe(32)
    if not instance_id or not stop_token:
        raise RuntimeErrorBase("The inherited runtime identity is incomplete")
    return instance_id, stop_token


@contextlib.contextmanager
def _runtime_environment(
    *,
    instance_id: str,
    stop_token: str,
    port: int,
    runtime_dir: Path,
) -> Iterator[None]:
    values = {
        ENV_INSTANCE_ID: instance_id,
        ENV_SERVER_PORT: str(port),
        ENV_RUNTIME_DIR: str(runtime_dir),
    }
    managed_names = {*values, ENV_STOP_TOKEN, ENV_PROJECT_LOCK_FD}
    previous = {name: os.environ.get(name) for name in managed_names}
    configure_runtime_identity(
        instance_id=instance_id,
        stop_token=stop_token,
        server_port=port,
    )
    os.environ.update(values)
    os.environ.pop(ENV_STOP_TOKEN, None)
    os.environ.pop(ENV_PROJECT_LOCK_FD, None)
    try:
        yield
    finally:
        clear_runtime_identity(instance_id=instance_id)
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _watch_parent(
    server: uvicorn.Server,
    parent_monitor: ParentProcessMonitor,
    finished: threading.Event,
) -> None:
    while not finished.wait(PARENT_POLL_SECONDS):
        if parent_monitor.disappeared():
            print("The launcher exited; stopping the backend safely.")
            server.should_exit = True
            if not finished.wait(PARENT_FORCE_STOP_SECONDS):
                print("Backend shutdown timed out; stopping its managed process tree.")
                _force_exit_managed_process_tree()
            return


def _force_exit_managed_process_tree() -> None:
    if os.name != "nt" and os.getpgrp() == os.getpid():
        os.killpg(os.getpid(), signal.SIGKILL)
    os._exit(1)


def _watch_runtime_stop(
    server: uvicorn.Server,
    finished: threading.Event,
) -> None:
    while not finished.is_set():
        if RUNTIME_STOP_EVENT.wait(PARENT_POLL_SECONDS):
            server.should_exit = True
            return


@contextlib.contextmanager
def _graceful_signal_handlers(server: uvicorn.Server) -> Iterator[None]:
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
        server.should_exit = True

    try:
        for current in handled:
            previous[current] = signal.getsignal(current)
            signal.signal(current, request_stop)
        yield
    finally:
        for current, handler in previous.items():
            signal.signal(current, handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start the media downloader web app")
    parser.add_argument("--port", default=DEFAULT_SERVER_PORT, type=int)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--exit-with-parent", type=int)
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=None,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    if not 0 <= args.port <= 65_535:
        parser.error("port must be between 0 and 65535")
    if args.exit_with_parent is not None:
        if args.exit_with_parent <= 1:
            parser.error("parent PID must be greater than 1")
        if not _launcher_parent_matches(args.exit_with_parent):
            print("The expected launcher is no longer this process's parent.")
            return 1

    runtime_dir_value = args.runtime_dir or os.environ.get(ENV_RUNTIME_DIR)
    runtime_dir = Path(runtime_dir_value or DEFAULT_RUNTIME_DIR).expanduser().resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    try:
        project_lock = _project_lock()
    except ProjectLockHeldError:
        return _handle_occupied_project(runtime_dir, no_browser=args.no_browser)
    except (OSError, RuntimeErrorBase) as exc:
        print(f"Could not establish the project runtime lock: {exc}")
        return 2

    try:
        try:
            listener = _bind_listener(args.port)
        except OSError:
            return _handle_occupied_port(args.port, no_browser=args.no_browser)

        actual_port = int(listener.getsockname()[1])
        try:
            instance_id, stop_token = _managed_identity()
            record = RuntimeRecord(
                app_id=APP_ID,
                build_id=BUILD_ID,
                instance_id=instance_id,
                stop_token=stop_token,
                pid=os.getpid(),
                port=actual_port,
                project_root=str(PROJECT_ROOT.resolve()),
                started_at=datetime.now(timezone.utc).isoformat(),
            )
        except (InvalidRuntimeRecordError, RuntimeErrorBase) as exc:
            print(f"Could not establish the managed runtime identity: {exc}")
            listener.close()
            return 2

        config = uvicorn.Config(
            "app.main:app",
            host=HOST,
            port=actual_port,
            log_level="info",
        )
        # Uvicorn imports the ASGI app only after this process owns the port.
        server = uvicorn.Server(config=config)
        watchers_finished = threading.Event()
        parent_watcher: threading.Thread | None = None
        parent_monitor: ParentProcessMonitor | None = None
        runtime_watcher: threading.Thread | None = None
        try:
            with _runtime_environment(
                instance_id=instance_id,
                stop_token=stop_token,
                port=actual_port,
                runtime_dir=runtime_dir,
            ), _graceful_signal_handlers(server):
                write_runtime_record(runtime_dir, record)
                if args.exit_with_parent is not None:
                    parent_monitor = ParentProcessMonitor(args.exit_with_parent)
                    if parent_monitor.disappeared():
                        print("The expected launcher is no longer running.")
                        return 1
                    parent_watcher = threading.Thread(
                        target=_watch_parent,
                        args=(server, parent_monitor, watchers_finished),
                        name="launcher-watch",
                        daemon=True,
                    )
                    parent_watcher.start()
                runtime_watcher = threading.Thread(
                    target=_watch_runtime_stop,
                    args=(server, watchers_finished),
                    name="runtime-stop-watch",
                    daemon=True,
                )
                runtime_watcher.start()
                if not args.no_browser:
                    threading.Thread(
                        target=_open_when_ready,
                        args=(actual_port,),
                        name="open-web-ui",
                        daemon=True,
                    ).start()
                print(
                    f"Starting Original Media Downloader {APP_VERSION} ({BUILD_ID}) "
                    f"at {_server_url(actual_port)}"
                )
                try:
                    server.run(sockets=[listener])
                except KeyboardInterrupt:
                    return 0
                return 0 if server.started else 3
        finally:
            watchers_finished.set()
            if parent_watcher is not None:
                parent_watcher.join(timeout=1.0)
            if runtime_watcher is not None:
                runtime_watcher.join(timeout=1.0)
            if parent_monitor is not None:
                parent_monitor.close()
            with contextlib.suppress(InvalidRuntimeRecordError, OSError):
                remove_runtime_record(
                    runtime_dir,
                    actual_port,
                    instance_id=instance_id,
                )
            listener.close()
    finally:
        project_lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
