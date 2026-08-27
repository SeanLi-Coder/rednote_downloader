from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import sysconfig
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

from app.runtime import ProjectLock, ProjectLockHeldError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WAIT_SECONDS = 15.0


def _isolated_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    shutil.copytree(
        PROJECT_ROOT / "app",
        project / "app",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for name in (
        "run.py",
        "launcher.py",
        "stop.py",
        "start.command",
        "pyproject.toml",
        "requirements.txt",
    ):
        shutil.copy2(PROJECT_ROOT / name, project / name)
    return project


def _free_port() -> int:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])
    finally:
        listener.close()


def _clean_environment() -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("OMD_")
    }
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def _environment_with_current_packages() -> dict[str, str]:
    environment = _clean_environment()
    package_paths = {
        path
        for name in ("purelib", "platlib")
        if (path := sysconfig.get_path(name))
    }
    environment["PYTHONPATH"] = os.pathsep.join(sorted(package_paths))
    return environment


def _fetch_health(port: int, *, timeout: float = 0.25) -> dict[str, Any] | None:
    request = Request(
        f"http://127.0.0.1:{port}/api/health",
        headers={"Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
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


def _wait_until(predicate, *, timeout: float = WAIT_SECONDS) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _start_backend(
    project: Path,
    *,
    port: int,
    runtime_dir: Path,
) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [
            sys.executable,
            str(project / "run.py"),
            "--port",
            str(port),
            "--runtime-dir",
            str(runtime_dir),
            "--no-browser",
        ],
        cwd=project,
        env=_clean_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if not _wait_until(lambda: _fetch_health(port) is not None):
        output, _ = process.communicate(timeout=5)
        raise AssertionError(f"backend did not become healthy:\n{output}")
    return process


def _finish_owned_process(process: subprocess.Popen[str]) -> str:
    if process.poll() is None:
        process.terminate()
    try:
        output, _ = process.communicate(timeout=WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        output, _ = process.communicate(timeout=5)
    return output


def _port_is_free(port: int) -> bool:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if not sys.platform.startswith("win"):
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        listener.close()
    return True


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    completed = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "state="],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0 and not completed.stdout.strip().startswith("Z")


def _project_lock_is_free(path: Path) -> bool:
    lock = ProjectLock(path)
    try:
        lock.acquire(blocking=False)
    except ProjectLockHeldError:
        return False
    finally:
        lock.release()
    return True


@pytest.mark.parametrize(
    "shutdown_signal",
    [
        pytest.param(signal.SIGTERM, id="sigterm"),
        pytest.param(
            getattr(signal, "SIGHUP", signal.SIGTERM),
            id="sighup",
            marks=pytest.mark.skipif(
                not hasattr(signal, "SIGHUP"),
                reason="SIGHUP is not available on this platform",
            ),
        ),
    ],
)
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows graceful shutdown is covered through authenticated control",
)
def test_direct_backend_signals_shutdown_cleanly(
    tmp_path: Path,
    shutdown_signal: signal.Signals,
) -> None:
    project = _isolated_project(tmp_path)
    runtime_dir = project / "data" / "runtime"
    port = _free_port()
    process = _start_backend(project, port=port, runtime_dir=runtime_dir)
    try:
        process.send_signal(shutdown_signal)
        output, _ = process.communicate(timeout=WAIT_SECONDS)
        assert process.returncode == 0
        assert "Application shutdown complete" in output
        assert _wait_until(lambda: _port_is_free(port))
        assert not (runtime_dir / f"runtime-{port}.json").exists()
    finally:
        if process.poll() is None:
            _finish_owned_process(process)


def test_duplicate_port_choice_reuses_one_project_instance_and_stop_is_safe(
    tmp_path: Path,
) -> None:
    project = _isolated_project(tmp_path)
    runtime_dir = project / "data" / "runtime"
    first_port = _free_port()
    second_port = _free_port()
    first = _start_backend(project, port=first_port, runtime_dir=runtime_dir)
    try:
        second = subprocess.run(
            [
                sys.executable,
                str(project / "run.py"),
                "--port",
                str(second_port),
                "--runtime-dir",
                str(runtime_dir),
                "--no-browser",
            ],
            cwd=project,
            env=_clean_environment(),
            check=False,
            capture_output=True,
            text=True,
            timeout=WAIT_SECONDS,
        )
        assert second.returncode == 0
        assert "already running" in second.stdout
        assert _fetch_health(second_port) is None

        different_runtime_port = _free_port()
        different_runtime = subprocess.run(
            [
                sys.executable,
                str(project / "run.py"),
                "--port",
                str(different_runtime_port),
                "--runtime-dir",
                str(project / "other-runtime"),
                "--no-browser",
            ],
            cwd=project,
            env=_clean_environment(),
            check=False,
            capture_output=True,
            text=True,
            timeout=WAIT_SECONDS,
        )
        assert different_runtime.returncode == 2
        assert "already starting" in different_runtime.stdout
        assert _fetch_health(different_runtime_port) is None

        stopped = subprocess.run(
            [
                sys.executable,
                str(project / "stop.py"),
                "--port",
                str(first_port),
                "--runtime-dir",
                str(runtime_dir),
                "--timeout",
                "10",
            ],
            cwd=project,
            env=_clean_environment(),
            check=False,
            capture_output=True,
            text=True,
            timeout=WAIT_SECONDS,
        )
        output, _ = first.communicate(timeout=WAIT_SECONDS)
        assert stopped.returncode == 0, stopped.stderr
        assert "stopped" in stopped.stdout
        assert first.returncode == 0
        assert "Application shutdown complete" in output
        assert _port_is_free(first_port)
        assert not (runtime_dir / f"runtime-{first_port}.json").exists()
    finally:
        if first.poll() is None:
            _finish_owned_process(first)


def test_launcher_and_stop_tool_complete_managed_lifecycle(tmp_path: Path) -> None:
    project = _isolated_project(tmp_path)
    runtime_dir = project / "data" / "runtime"
    port = _free_port()
    (project / "requirements.txt").write_text("", encoding="utf-8")
    created = subprocess.run(
        [
            sys.executable,
            "-m",
            "venv",
            "--system-site-packages",
            str(project / ".venv"),
        ],
        cwd=project,
        env=_clean_environment(),
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert created.returncode == 0, created.stderr
    launcher_process = subprocess.Popen(
        [
            sys.executable,
            str(project / "launcher.py"),
            "--port",
            str(port),
            "--runtime-dir",
            str(runtime_dir),
            "--no-browser",
        ],
        cwd=project,
        env=_environment_with_current_packages(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        if not _wait_until(lambda: _fetch_health(port) is not None, timeout=30):
            output = _finish_owned_process(launcher_process)
            raise AssertionError(f"launcher did not become healthy:\n{output}")
        assert (runtime_dir / "launcher.json").is_file()
        assert (runtime_dir / f"runtime-{port}.json").is_file()

        stopped = subprocess.run(
            [
                sys.executable,
                str(project / "stop.py"),
                "--port",
                str(port),
                "--runtime-dir",
                str(runtime_dir),
                "--timeout",
                "30",
            ],
            cwd=project,
            env=_clean_environment(),
            check=False,
            capture_output=True,
            text=True,
            timeout=40,
        )
        output, _ = launcher_process.communicate(timeout=40)
        assert stopped.returncode == 0, stopped.stderr
        assert launcher_process.returncode == 0, output
        assert "Application shutdown complete" in output
        assert _wait_until(lambda: _port_is_free(port))
        assert not (runtime_dir / "launcher.json").exists()
        assert not (runtime_dir / f"runtime-{port}.json").exists()
        assert _project_lock_is_free(project / "data" / "runtime" / "project.lock")
    finally:
        if launcher_process.poll() is None:
            _finish_owned_process(launcher_process)


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX fake venv command")
def test_stop_tool_cancels_bootstrap_and_its_owned_subprocess_tree(
    tmp_path: Path,
) -> None:
    project = _isolated_project(tmp_path)
    runtime_dir = project / "data" / "runtime"
    fake_python = project / ".venv" / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    bootstrap_pid = tmp_path / "bootstrap.pid"
    child_pid = tmp_path / "bootstrap-child.pid"
    fake_python.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                'if [ "${1:-}" = "-I" ]; then exit 0; fi',
                f"echo $$ > {bootstrap_pid}",
                "sleep 60 &",
                "child=$!",
                f"echo $child > {child_pid}",
                'wait "$child"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o700)
    port = _free_port()
    launcher_process = subprocess.Popen(
        [
            sys.executable,
            str(project / "launcher.py"),
            "--port",
            str(port),
            "--runtime-dir",
            str(runtime_dir),
            "--no-browser",
        ],
        cwd=project,
        env=_clean_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert _wait_until(lambda: (runtime_dir / "launcher.json").is_file())
        assert _wait_until(bootstrap_pid.is_file)
        assert _wait_until(child_pid.is_file)
        bootstrap_process_pid = int(bootstrap_pid.read_text(encoding="utf-8"))
        child_process_pid = int(child_pid.read_text(encoding="utf-8"))

        stopped = subprocess.run(
            [
                sys.executable,
                str(project / "stop.py"),
                "--port",
                str(port),
                "--runtime-dir",
                str(runtime_dir),
                "--timeout",
                "15",
            ],
            cwd=project,
            env=_clean_environment(),
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        output, _ = launcher_process.communicate(timeout=20)
        assert stopped.returncode == 0, stopped.stderr
        assert launcher_process.returncode == 0, output
        assert _wait_until(lambda: not _pid_is_alive(bootstrap_process_pid))
        assert _wait_until(lambda: not _pid_is_alive(child_process_pid))
        assert not (runtime_dir / "launcher.json").exists()
        assert _port_is_free(port)
    finally:
        if launcher_process.poll() is None:
            _finish_owned_process(launcher_process)


@pytest.mark.skipif(sys.platform == "win32", reason="tests the POSIX parent pipe")
def test_launcher_sigkill_during_bootstrap_releases_tree_lock_and_restarts(
    tmp_path: Path,
) -> None:
    project = _isolated_project(tmp_path)
    runtime_dir = project / "data" / "runtime"
    lock_path = runtime_dir / "project.lock"
    fake_python = project / ".venv" / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    bootstrap_pid = tmp_path / "sigkill-bootstrap.pid"
    child_pid = tmp_path / "sigkill-bootstrap-child.pid"
    fake_python.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                'if [ "${1:-}" = "-I" ]; then exit 0; fi',
                f"echo $$ > {bootstrap_pid}",
                "sleep 60 &",
                "child=$!",
                f"echo $child > {child_pid}",
                'wait "$child"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o700)
    port = _free_port()

    def start_launcher() -> subprocess.Popen[str]:
        return subprocess.Popen(
            [
                sys.executable,
                str(project / "launcher.py"),
                "--port",
                str(port),
                "--runtime-dir",
                str(runtime_dir),
                "--no-browser",
            ],
            cwd=project,
            env=_clean_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    first = start_launcher()
    second: subprocess.Popen[str] | None = None
    first_bootstrap_pid: int | None = None
    first_child_pid: int | None = None
    try:
        assert _wait_until(lambda: (runtime_dir / "launcher.json").is_file())
        assert _wait_until(bootstrap_pid.is_file)
        assert _wait_until(child_pid.is_file)
        first_bootstrap_pid = int(bootstrap_pid.read_text(encoding="utf-8"))
        first_child_pid = int(child_pid.read_text(encoding="utf-8"))

        first.kill()
        first.wait(timeout=5)
        assert _wait_until(lambda: not _pid_is_alive(first_bootstrap_pid))
        assert _wait_until(lambda: not _pid_is_alive(first_child_pid))
        assert _wait_until(lambda: _project_lock_is_free(lock_path))

        stopped = subprocess.run(
            [
                sys.executable,
                str(project / "stop.py"),
                "--port",
                str(port),
                "--runtime-dir",
                str(runtime_dir),
                "--timeout",
                "10",
            ],
            cwd=project,
            env=_clean_environment(),
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert stopped.returncode == 0, stopped.stderr
        assert "not running" in stopped.stdout
        assert not (runtime_dir / "launcher.json").exists()

        bootstrap_pid.unlink(missing_ok=True)
        child_pid.unlink(missing_ok=True)
        second = start_launcher()
        assert _wait_until(lambda: (runtime_dir / "launcher.json").is_file())
        assert _wait_until(bootstrap_pid.is_file)
        assert _wait_until(child_pid.is_file)
        assert second.poll() is None

        stopped_again = subprocess.run(
            [
                sys.executable,
                str(project / "stop.py"),
                "--port",
                str(port),
                "--runtime-dir",
                str(runtime_dir),
                "--timeout",
                "15",
            ],
            cwd=project,
            env=_clean_environment(),
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        output, _ = second.communicate(timeout=20)
        assert stopped_again.returncode == 0, stopped_again.stderr
        assert second.returncode == 0, output
        assert _wait_until(lambda: _project_lock_is_free(lock_path))
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                _finish_owned_process(process)
        for pid in (first_bootstrap_pid, first_child_pid):
            if pid is not None and _pid_is_alive(pid):
                os.kill(pid, signal.SIGKILL)


@pytest.mark.skipif(sys.platform == "win32", reason="tests the macOS shell launcher")
def test_start_command_shell_sigkill_stops_bootstrap_tree_and_releases_lock(
    tmp_path: Path,
) -> None:
    project = _isolated_project(tmp_path)
    runtime_dir = project / "data" / "runtime"
    lock_path = runtime_dir / "project.lock"
    fake_python = project / ".venv" / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    bootstrap_pid = tmp_path / "shell-bootstrap.pid"
    child_pid = tmp_path / "shell-bootstrap-child.pid"
    fake_python.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                'if [ "${1:-}" = "-I" ]; then exit 0; fi',
                f"echo $$ > {bootstrap_pid}",
                "sleep 60 &",
                "child=$!",
                f"echo $child > {child_pid}",
                'wait "$child"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o700)
    shell = subprocess.Popen(
        [str(project / "start.command")],
        cwd=project,
        env=_clean_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    bootstrap_process_pid: int | None = None
    child_process_pid: int | None = None
    try:
        assert _wait_until(lambda: (runtime_dir / "launcher.json").is_file())
        assert _wait_until(bootstrap_pid.is_file)
        assert _wait_until(child_pid.is_file)
        bootstrap_process_pid = int(bootstrap_pid.read_text(encoding="utf-8"))
        child_process_pid = int(child_pid.read_text(encoding="utf-8"))

        shell.kill()
        output, _ = shell.communicate(timeout=20)

        assert shell.returncode == -signal.SIGKILL, output
        assert _wait_until(lambda: not _pid_is_alive(bootstrap_process_pid))
        assert _wait_until(lambda: not _pid_is_alive(child_process_pid))
        assert _wait_until(lambda: _project_lock_is_free(lock_path))
        assert _wait_until(lambda: not (runtime_dir / "launcher.json").exists())
    finally:
        if shell.poll() is None:
            shell.kill()
            shell.wait(timeout=5)
        for pid in (bootstrap_process_pid, child_process_pid):
            if pid is not None and _pid_is_alive(pid):
                os.kill(pid, signal.SIGKILL)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX parent reaping semantics")
def test_backend_stops_when_its_launcher_is_killed(tmp_path: Path) -> None:
    project = _isolated_project(tmp_path)
    runtime_dir = project / "data" / "runtime"
    port = _free_port()
    pid_file = tmp_path / "backend.pid"
    log_file = tmp_path / "backend.log"
    wrapper_code = "\n".join(
        [
            "import os, pathlib, subprocess, sys, time",
            "project = pathlib.Path(sys.argv[1])",
            "port, runtime_dir = sys.argv[2], sys.argv[3]",
            "pid_file, log_file = pathlib.Path(sys.argv[4]), pathlib.Path(sys.argv[5])",
            "with log_file.open('w', encoding='utf-8') as log:",
            "    child = subprocess.Popen([sys.executable, str(project / 'run.py'), '--port', port, '--runtime-dir', runtime_dir, '--exit-with-parent', str(os.getpid()), '--no-browser'], cwd=project, stdout=log, stderr=subprocess.STDOUT, text=True)",
            "    pid_file.write_text(str(child.pid), encoding='utf-8')",
            "    while True:",
            "        time.sleep(1)",
        ]
    )
    wrapper = subprocess.Popen(
        [
            sys.executable,
            "-c",
            wrapper_code,
            str(project),
            str(port),
            str(runtime_dir),
            str(pid_file),
            str(log_file),
        ],
        cwd=project,
        env=_clean_environment(),
    )
    backend_pid: int | None = None
    try:
        assert _wait_until(pid_file.is_file)
        backend_pid = int(pid_file.read_text(encoding="utf-8"))
        assert _wait_until(lambda: _fetch_health(port) is not None)
        wrapper.kill()
        wrapper.wait(timeout=5)

        assert _wait_until(lambda: _fetch_health(port) is None)
        assert _wait_until(lambda: _port_is_free(port))
        assert _wait_until(
            lambda: not (runtime_dir / f"runtime-{port}.json").exists()
        )
        assert "Application shutdown complete" in log_file.read_text(
            encoding="utf-8"
        )
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
            wrapper.wait(timeout=5)
        if backend_pid is not None and _fetch_health(port) is not None:
            os.kill(backend_pid, signal.SIGTERM)
            _wait_until(lambda: _fetch_health(port) is None)


@pytest.mark.skipif(sys.platform == "win32", reason="tests POSIX killpg watchdog")
def test_parent_death_watchdog_kills_stuck_backend_and_descendant(
    tmp_path: Path,
) -> None:
    project = _isolated_project(tmp_path)
    runtime_dir = project / "data" / "runtime"
    port = _free_port()
    backend_pid_path = tmp_path / "stuck-backend.pid"
    child_pid_path = tmp_path / "stuck-child.pid"
    log_file = tmp_path / "stuck-backend.log"

    run_path = project / "run.py"
    run_source = run_path.read_text(encoding="utf-8")
    updated_source = run_source.replace(
        "PARENT_FORCE_STOP_SECONDS = 135.0",
        "PARENT_FORCE_STOP_SECONDS = 1.0",
    )
    assert updated_source != run_source
    run_path.write_text(updated_source, encoding="utf-8")

    child_code = "\n".join(
        [
            "import os, pathlib, signal, time",
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
            f"pathlib.Path({str(child_pid_path)!r}).write_text(str(os.getpid()))",
            "time.sleep(60)",
        ]
    )
    (project / "app" / "main.py").write_text(
        "\n".join(
            [
                "import asyncio, subprocess, sys",
                "from contextlib import asynccontextmanager",
                "from fastapi import FastAPI",
                "@asynccontextmanager",
                "async def lifespan(_):",
                f"    child = subprocess.Popen([sys.executable, '-c', {child_code!r}])",
                "    yield",
                "    await asyncio.sleep(60)",
                "app = FastAPI(lifespan=lifespan)",
                "@app.get('/api/health')",
                "def health(): return {'status': 'ok'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    wrapper_code = "\n".join(
        [
            "import os, pathlib, subprocess, sys, time",
            "project = pathlib.Path(sys.argv[1])",
            "port, runtime_dir = sys.argv[2], sys.argv[3]",
            "pid_path, log_path = pathlib.Path(sys.argv[4]), pathlib.Path(sys.argv[5])",
            "with log_path.open('w', encoding='utf-8') as log:",
            "    child = subprocess.Popen([sys.executable, str(project / 'run.py'), '--port', port, '--runtime-dir', runtime_dir, '--exit-with-parent', str(os.getpid()), '--no-browser'], cwd=project, stdout=log, stderr=subprocess.STDOUT, text=True, start_new_session=True)",
            "    pid_path.write_text(str(child.pid), encoding='utf-8')",
            "    while True: time.sleep(1)",
        ]
    )
    wrapper = subprocess.Popen(
        [
            sys.executable,
            "-c",
            wrapper_code,
            str(project),
            str(port),
            str(runtime_dir),
            str(backend_pid_path),
            str(log_file),
        ],
        cwd=project,
        env=_clean_environment(),
    )
    backend_pid: int | None = None
    child_pid: int | None = None
    try:
        assert _wait_until(backend_pid_path.is_file)
        backend_pid = int(backend_pid_path.read_text(encoding="utf-8"))
        assert _wait_until(lambda: _fetch_health(port) is not None)
        assert _wait_until(child_pid_path.is_file)
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))

        wrapper.kill()
        wrapper.wait(timeout=5)

        assert _wait_until(lambda: not _pid_is_alive(backend_pid))
        assert _wait_until(lambda: not _pid_is_alive(child_pid))
        assert _wait_until(lambda: _port_is_free(port))
        assert _wait_until(
            lambda: _project_lock_is_free(runtime_dir / "project.lock")
        )
        assert "Backend shutdown timed out" in log_file.read_text(encoding="utf-8")
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
            wrapper.wait(timeout=5)
        if backend_pid is not None and _pid_is_alive(backend_pid):
            try:
                os.killpg(backend_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if child_pid is not None and _pid_is_alive(child_pid):
            os.kill(child_pid, signal.SIGKILL)
