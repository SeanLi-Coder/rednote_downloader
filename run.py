from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import uvicorn

from app.browser import open_chrome
from app.build_info import APP_ID, APP_VERSION, BUILD_ID


HOST = "127.0.0.1"


def _server_url(port: int) -> str:
    return f"http://{HOST}:{port}"


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
    except BaseException:
        listener.close()
        raise
    listener.set_inheritable(True)
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
        "Stop the previous downloader Terminal with Ctrl+C, then run "
        "start.command again."
    )
    print(
        f"To inspect the listener on macOS, run: "
        f"lsof -nP -iTCP:{port} -sTCP:LISTEN"
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start the media downloader web app")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    try:
        listener = _bind_listener(args.port)
    except OSError:
        return _handle_occupied_port(args.port, no_browser=args.no_browser)

    actual_port = int(listener.getsockname()[1])
    config = uvicorn.Config(
        "app.main:app",
        host=HOST,
        port=actual_port,
        log_level="info",
    )
    try:
        # Uvicorn imports the ASGI app only after this process owns the port.
        server = uvicorn.Server(config=config)
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
        listener.close()


if __name__ == "__main__":
    raise SystemExit(main())
