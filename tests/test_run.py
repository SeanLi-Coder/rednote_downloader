from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

import pytest

import run
from app.build_info import APP_ID, APP_VERSION, BUILD_ID
from app.runtime import DEFAULT_SERVER_PORT


@pytest.fixture(autouse=True)
def _isolated_project_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(run, "PROJECT_ROOT", tmp_path / "project")


def current_health() -> dict[str, object]:
    return {
        "status": "ok",
        "app_id": APP_ID,
        "version": APP_VERSION,
        "build_id": BUILD_ID,
        "source_build_id": BUILD_ID,
        "restart_required": False,
    }


def test_current_build_requires_complete_identity() -> None:
    assert run._is_current_build(current_health()) is True
    assert run._is_current_build({"status": "ok"}) is False
    assert run._is_current_build({**current_health(), "build_id": "old"}) is False
    assert run._is_current_build({**current_health(), "restart_required": True}) is False


def test_launcher_parent_uses_direct_identity_on_posix(monkeypatch) -> None:
    monkeypatch.setattr(run.os, "getppid", lambda: 12_345)

    assert run._launcher_parent_matches(12_345, platform_name="posix") is True
    assert run._launcher_parent_matches(54_321, platform_name="posix") is False


def test_launcher_parent_uses_stable_monitor_on_windows(monkeypatch) -> None:
    monkeypatch.setattr(
        run.os,
        "getppid",
        lambda: (_ for _ in ()).throw(AssertionError("getppid must not be used")),
    )

    assert run._launcher_parent_matches(12_345, platform_name="nt") is True


def test_main_uses_the_shared_default_port(monkeypatch, tmp_path) -> None:
    captured: list[int] = []

    def reject_bind(port: int):
        captured.append(port)
        raise OSError("test occupied port")

    monkeypatch.setattr(run, "_bind_listener", reject_bind)
    monkeypatch.setattr(
        run,
        "_handle_occupied_port",
        lambda port, *, no_browser: 0,
    )

    assert (
        run.main(["--runtime-dir", str(tmp_path / "runtime"), "--no-browser"])
        == 0
    )
    assert captured == [DEFAULT_SERVER_PORT]


def test_occupied_old_backend_never_opens_browser(monkeypatch, tmp_path) -> None:
    opened: list[str] = []
    monkeypatch.setattr(
        run,
        "_bind_listener",
        lambda port: (_ for _ in ()).throw(OSError()),
    )
    monkeypatch.setattr(run, "_fetch_health", lambda port: {"status": "ok"})
    monkeypatch.setattr(run, "open_chrome", opened.append)
    monkeypatch.setattr(
        run.uvicorn,
        "Config",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("the ASGI app must not load while the port is occupied")
        ),
    )
    result = run.main(
        ["--port", "18765", "--runtime-dir", str(tmp_path / "runtime")]
    )

    assert result == 2
    assert opened == []


def test_occupied_current_backend_opens_existing_instance(
    monkeypatch,
    tmp_path,
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(
        run,
        "_bind_listener",
        lambda port: (_ for _ in ()).throw(OSError()),
    )
    monkeypatch.setattr(run, "_fetch_health", lambda port: current_health())
    monkeypatch.setattr(run, "open_chrome", opened.append)

    result = run.main(
        ["--port", "18766", "--runtime-dir", str(tmp_path / "runtime")]
    )

    assert result == 0
    assert opened == ["http://127.0.0.1:18766"]


def test_no_browser_never_opens_matching_existing_instance(
    monkeypatch,
    tmp_path,
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(
        run,
        "_bind_listener",
        lambda port: (_ for _ in ()).throw(OSError()),
    )
    monkeypatch.setattr(run, "_fetch_health", lambda port: current_health())
    monkeypatch.setattr(run, "open_chrome", opened.append)

    result = run.main(
        [
            "--port",
            "18767",
            "--no-browser",
            "--runtime-dir",
            str(tmp_path / "runtime"),
        ]
    )

    assert result == 0
    assert opened == []


def test_listener_exclusively_reserves_port_before_app_import() -> None:
    listener = run._bind_listener(0)
    port = int(listener.getsockname()[1])
    contender = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    contender.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        assert listener.get_inheritable() is False
        try:
            contender.bind((run.HOST, port))
        except OSError:
            pass
        else:
            raise AssertionError("a second process unexpectedly acquired the port")
    finally:
        contender.close()
        listener.close()


def test_windows_listener_requests_exclusive_address_use(monkeypatch) -> None:
    options: list[tuple[int, int, int]] = []

    class FakeSocket:
        def setsockopt(self, level, option, value):
            options.append((level, option, value))

    monkeypatch.setattr(run.socket, "SO_EXCLUSIVEADDRUSE", 0x4, raising=False)

    run._configure_listener(FakeSocket(), "win32")

    assert options == [(socket.SOL_SOCKET, 0x4, 1)]


def test_launcher_import_does_not_load_downloader_or_task_manager() -> None:
    project_root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import run; "
                "print('app.downloader' in sys.modules); "
                "print('app.task_manager' in sys.modules); "
                "print('app.main' in sys.modules)"
            ),
        ],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.splitlines() == ["False", "False", "False"]


def test_free_port_start_supports_uvicorn_without_load_app(
    monkeypatch,
    tmp_path,
) -> None:
    class CompatibleConfig:
        def __init__(self, app, **kwargs):
            self.app = app

    class CompatibleServer:
        def __init__(self, *, config):
            self.config = config
            self.started = False

        def run(self, *, sockets):
            assert len(sockets) == 1
            self.started = True

    monkeypatch.setattr(run.uvicorn, "Config", CompatibleConfig)
    monkeypatch.setattr(run.uvicorn, "Server", CompatibleServer)

    assert (
        run.main(
            [
                "--port",
                "0",
                "--no-browser",
                "--runtime-dir",
                str(tmp_path / "runtime"),
            ]
        )
        == 0
    )


def test_browser_waits_for_exact_build(monkeypatch) -> None:
    responses = iter([None, {"status": "ok"}, current_health()])
    opened: list[str] = []
    monkeypatch.setattr(run, "_fetch_health", lambda port, timeout=0.5: next(responses))
    monkeypatch.setattr(run.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(run, "open_chrome", opened.append)

    run._open_when_ready(18768)

    assert opened == ["http://127.0.0.1:18768"]


def test_parent_watchdog_forces_managed_tree_after_shutdown_timeout(
    monkeypatch,
) -> None:
    class FakeServer:
        should_exit = False

    class MissingParent:
        @staticmethod
        def disappeared() -> bool:
            return True

    class NeverFinished:
        def __init__(self) -> None:
            self.waits: list[float] = []

        def wait(self, timeout: float) -> bool:
            self.waits.append(timeout)
            return False

    server = FakeServer()
    finished = NeverFinished()
    forced: list[bool] = []
    monkeypatch.setattr(
        run,
        "_force_exit_managed_process_tree",
        lambda: forced.append(True),
    )

    run._watch_parent(server, MissingParent(), finished)

    assert server.should_exit is True
    assert finished.waits == [
        run.PARENT_POLL_SECONDS,
        run.PARENT_FORCE_STOP_SECONDS,
    ]
    assert forced == [True]
