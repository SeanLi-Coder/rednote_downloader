from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import launcher
from app.runtime import (
    DEFAULT_SERVER_PORT,
    ENV_INSTANCE_ID,
    ENV_PROJECT_LOCK_FD,
    ENV_RUNTIME_DIR,
    ENV_SERVER_PORT,
    ENV_STOP_TOKEN,
    ProjectLockHeldError,
)


class FakeProcess:
    def __init__(self, *, pid: int = 43210, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls: list[float | None] = []

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = -signal.SIGTERM

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -signal.SIGKILL

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return self.returncode


def test_backend_environment_contains_only_explicit_runtime_values(tmp_path) -> None:
    environment = launcher._backend_environment(
        {"PATH": "/usr/bin"},
        instance_id="a" * 32,
        stop_token="token_" + "b" * 32,
        port=18765,
        lock_fd=9,
        runtime_dir=tmp_path,
    )

    assert environment["PATH"] == "/usr/bin"
    assert environment[ENV_INSTANCE_ID] == "a" * 32
    assert environment[ENV_STOP_TOKEN] == "token_" + "b" * 32
    assert environment[ENV_SERVER_PORT] == "18765"
    assert environment[ENV_PROJECT_LOCK_FD] == "9"
    assert environment[ENV_RUNTIME_DIR] == str(tmp_path)


def test_backend_pid_requires_exact_match_on_posix() -> None:
    assert (
        launcher._backend_pid_matches_process(
            12_345,
            12_345,
            platform_name="posix",
        )
        is True
    )
    assert (
        launcher._backend_pid_matches_process(
            54_321,
            12_345,
            platform_name="posix",
        )
        is False
    )


@pytest.mark.parametrize("server_pid", [None, True, 0, 1, "12345"])
def test_backend_pid_rejects_invalid_windows_identity(server_pid) -> None:
    assert (
        launcher._backend_pid_matches_process(
            server_pid,
            12_345,
            platform_name="nt",
        )
        is False
    )


def test_backend_pid_allows_windows_venv_redirector() -> None:
    assert (
        launcher._backend_pid_matches_process(
            54_321,
            12_345,
            platform_name="nt",
        )
        is True
    )


def test_stop_request_terminates_only_the_owned_popen(monkeypatch) -> None:
    process = FakeProcess()
    stop_requested = threading.Event()
    stop_requested.set()
    killed_pids: list[tuple] = []
    monkeypatch.setattr(launcher.os, "kill", lambda *args: killed_pids.append(args))

    returncode, stopped = launcher._wait_for_owned_process(
        process,
        stop_requested=stop_requested,
        initial_parent_pid=os.getppid(),
        poll_seconds=0,
    )

    assert stopped is True
    assert returncode == -signal.SIGTERM
    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert killed_pids == []


def test_parent_disappearance_stops_the_owned_backend(monkeypatch) -> None:
    process = FakeProcess()
    monkeypatch.setattr(launcher.os, "getppid", lambda: 1)

    returncode, stopped = launcher._wait_for_owned_process(
        process,
        stop_requested=threading.Event(),
        initial_parent_pid=9876,
        poll_seconds=0,
    )

    assert stopped is True
    assert returncode == -signal.SIGTERM
    assert process.terminate_calls == 1


def test_normal_backend_exit_is_propagated_without_termination() -> None:
    process = FakeProcess(returncode=7)

    returncode, stopped = launcher._wait_for_owned_process(
        process,
        stop_requested=threading.Event(),
        initial_parent_pid=os.getppid(),
        poll_seconds=0,
    )

    assert (returncode, stopped) == (7, False)
    assert process.terminate_calls == 0


def test_signal_handlers_only_set_the_shutdown_event() -> None:
    stop_requested = threading.Event()
    previous = signal.getsignal(signal.SIGTERM)
    with launcher._shutdown_signal_handlers(stop_requested):
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        assert stop_requested.is_set()
    assert signal.getsignal(signal.SIGTERM) == previous


def test_spawn_backend_passes_lock_and_launcher_parent(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}
    process = FakeProcess()
    assigned: list[FakeProcess] = []
    inheritance: list[tuple[int, bool]] = []

    def fake_popen(command, **options):
        captured["command"] = command
        captured["options"] = options
        return process

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        launcher,
        "set_project_lock_inheritable",
        lambda handle, inheritable: inheritance.append((handle, inheritable)),
    )
    monkeypatch.setattr(
        launcher,
        "_assign_windows_backend_job",
        lambda candidate: assigned.append(candidate),
    )
    result = launcher._spawn_backend(
        Path("/python"),
        project_root=tmp_path,
        port=18765,
        no_browser=True,
        environment={"SAFE": "1"},
        lock_fd=9,
    )

    assert result is process
    command = captured["command"]
    assert command == [
        str(Path("/python")),
        str(tmp_path / "run.py"),
        "--port",
        "18765",
        "--exit-with-parent",
        str(os.getpid()),
        "--no-browser",
    ]
    options = captured["options"]
    assert options["cwd"] == tmp_path
    assert options["env"] == {"SAFE": "1"}
    assert assigned == [process]
    if os.name == "nt":
        assert options["close_fds"] is True
        assert options["startupinfo"].lpAttributeList == {"handle_list": [9]}
        assert inheritance == [(9, True), (9, False)]
    else:
        assert options["pass_fds"] == (9,)


def test_prepare_environment_installs_after_validation(monkeypatch, tmp_path) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("example==1\n", encoding="utf-8")
    python = launcher._venv_python(tmp_path)
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    python.chmod(0o700)
    commands: list[list[str]] = []
    monkeypatch.setattr(launcher, "_python_is_supported", lambda executable: True)

    result = launcher._prepare_virtual_environment(
        tmp_path,
        base_python=Path("/base-python"),
        run_command=lambda command: commands.append(list(command)),
    )

    assert result == python
    assert len(commands) == 1
    assert commands[0][:4] == [str(python), "-m", "pip", "install"]
    assert commands[0][-2:] == ["-r", str(requirements)]


def test_prepare_environment_rejects_unsupported_existing_python(
    monkeypatch, tmp_path
) -> None:
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    python = launcher._venv_python(tmp_path)
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    python.chmod(0o700)
    commands: list[list[str]] = []
    monkeypatch.setattr(launcher, "_python_is_supported", lambda executable: False)

    with pytest.raises(launcher.LauncherError, match="unsupported Python"):
        launcher._prepare_virtual_environment(
            tmp_path,
            base_python=Path("/base-python"),
            run_command=lambda command: commands.append(list(command)),
        )
    assert commands == []


def test_occupied_project_lock_never_spawns_or_stops_a_process(
    monkeypatch, tmp_path
) -> None:
    class HeldLock:
        def __init__(self, path) -> None:
            self.fd = None

        def acquire(self, blocking=False):
            raise ProjectLockHeldError("held")

    reported: list[tuple[Path, Path]] = []
    monkeypatch.setattr(launcher, "ProjectLock", HeldLock)
    monkeypatch.setattr(
        launcher,
        "_report_lock_owner",
        lambda runtime_dir, project_root: reported.append(
            (runtime_dir, project_root)
        ),
    )
    monkeypatch.setattr(
        launcher,
        "_spawn_backend",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("an occupied launcher must not spawn a backend")
        ),
    )

    result = launcher._launch(
        project_root=tmp_path,
        runtime_dir=tmp_path / "runtime",
        port=18765,
        no_browser=True,
        base_python=Path("/python"),
        stop_requested=threading.Event(),
        initial_parent_pid=os.getppid(),
    )

    assert result == 2
    assert reported == [(tmp_path / "runtime", tmp_path)]


def test_launch_writes_and_removes_only_its_runtime_record(
    monkeypatch, tmp_path
) -> None:
    class FakeLock:
        instances: list["FakeLock"] = []

        def __init__(self, path) -> None:
            self.path = path
            self.fd = None
            self.released = False
            self.instances.append(self)

        def acquire(self, blocking=False):
            self.fd = 9
            return self

        def release(self):
            self.released = True
            self.fd = None

    process = FakeProcess(returncode=0)
    captured_environment: dict[str, str] = {}
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr(launcher, "ProjectLock", FakeLock)
    monkeypatch.setattr(
        launcher,
        "_prepare_virtual_environment",
        lambda *args, **kwargs: Path("/venv/python"),
    )

    def fake_spawn(*args, environment, **kwargs):
        captured_environment.update(environment)
        return process

    monkeypatch.setattr(launcher, "_spawn_backend", fake_spawn)

    result = launcher._launch(
        project_root=tmp_path,
        runtime_dir=runtime_dir,
        port=18765,
        no_browser=True,
        base_python=Path("/python"),
        stop_requested=threading.Event(),
        initial_parent_pid=os.getppid(),
    )

    assert result == 0
    assert captured_environment[ENV_INSTANCE_ID]
    assert captured_environment[ENV_STOP_TOKEN]
    assert not (runtime_dir / "runtime-18765.json").exists()
    assert FakeLock.instances[0].released is True


def test_launch_cleanup_failure_still_releases_every_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class FakeLock:
        def __init__(self, path) -> None:
            self.fd = None

        def acquire(self, blocking=False):
            self.fd = 9
            return self

        def release(self) -> None:
            events.append("lock.release")
            self.fd = None

    class FakeControl:
        def __init__(self, **kwargs) -> None:
            pass

        def start(self) -> None:
            events.append("control.start")

        def close(self) -> None:
            events.append("control.close")

    process = FakeProcess(returncode=0)
    monkeypatch.setattr(launcher, "ProjectLock", FakeLock)
    monkeypatch.setattr(launcher, "LauncherControl", FakeControl)
    monkeypatch.setattr(
        launcher,
        "_prepare_virtual_environment",
        lambda *args, **kwargs: Path("/venv/python"),
    )
    monkeypatch.setattr(launcher, "_spawn_backend", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        launcher,
        "_wait_for_owned_process",
        lambda *args, **kwargs: (0, False),
    )
    monkeypatch.setattr(
        launcher,
        "_cleanup_finished_process_group",
        lambda candidate: (_ for _ in ()).throw(
            launcher.LauncherError("cleanup failed")
        ),
    )
    monkeypatch.setattr(
        launcher,
        "_close_windows_backend_job",
        lambda candidate: events.append("job.close"),
    )

    with pytest.raises(launcher.LauncherError, match="cleanup failed"):
        launcher._launch(
            project_root=tmp_path,
            runtime_dir=tmp_path / "runtime",
            port=18_765,
            no_browser=True,
            base_python=Path("/python"),
            stop_requested=threading.Event(),
            initial_parent_pid=os.getppid(),
        )

    assert events == [
        "control.start",
        "control.close",
        "job.close",
        "lock.release",
    ]


def test_main_honors_explicit_parent_pid(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    def fake_launch(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(launcher, "_launch", fake_launch)

    assert (
        launcher.main(
            [
                "--port",
                "18765",
                "--parent-pid",
                "54321",
                "--runtime-dir",
                str(tmp_path),
                "--no-browser",
            ]
        )
        == 0
    )
    assert captured["initial_parent_pid"] == 54321
    assert captured["port"] == 18765
    assert captured["no_browser"] is True


def test_main_uses_the_shared_default_port(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    def fake_launch(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(launcher, "_launch", fake_launch)

    assert (
        launcher.main(
            [
                "--parent-pid",
                "54321",
                "--runtime-dir",
                str(tmp_path),
                "--no-browser",
            ]
        )
        == 0
    )
    assert captured["port"] == DEFAULT_SERVER_PORT


@pytest.mark.skipif(os.name == "nt", reason="tests POSIX process-group cleanup")
def test_force_stop_cleans_descendant_after_group_leader_exits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    child_pid_path = tmp_path / "stubborn-child.pid"
    child_code = "\n".join(
        [
            "import os, pathlib, signal, time",
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
            f"pathlib.Path({str(child_pid_path)!r}).write_text(str(os.getpid()))",
            "time.sleep(60)",
        ]
    )
    leader_code = "\n".join(
        [
            "import pathlib, subprocess, sys, time",
            f"path = pathlib.Path({str(child_pid_path)!r})",
            f"subprocess.Popen([sys.executable, '-c', {child_code!r}])",
            "deadline = time.monotonic() + 5",
            "while not path.is_file() and time.monotonic() < deadline: time.sleep(0.01)",
        ]
    )
    process = subprocess.Popen(
        [sys.executable, "-c", leader_code],
        start_new_session=True,
    )
    process._omd_process_group = True
    child_pid: int | None = None
    try:
        deadline = time.monotonic() + 5
        while not child_pid_path.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert child_pid_path.is_file()
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        process.wait(timeout=5)
        monkeypatch.setattr(
            launcher,
            "BACKEND_FORCE_STOP_TIMEOUT_SECONDS",
            0.5,
        )

        launcher._force_stop_backend_tree(process)

        completed = subprocess.run(
            ["/bin/ps", "-p", str(child_pid), "-o", "state="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        assert completed.returncode != 0 or completed.stdout.strip().startswith("Z")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
