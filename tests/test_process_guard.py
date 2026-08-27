from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GUARD = PROJECT_ROOT / "app" / "process_guard.py"


def _wait_until(predicate, *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


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
        timeout=2,
    )
    return completed.returncode == 0 and not completed.stdout.strip().startswith("Z")


def _start_guard(command: list[str]) -> tuple[subprocess.Popen[str], int]:
    parent_pipe_read, parent_pipe_write = os.pipe()
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                str(GUARD),
                "--parent-pipe-fd",
                str(parent_pipe_read),
                "--",
                *command,
            ],
            pass_fds=(parent_pipe_read,),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    finally:
        os.close(parent_pipe_read)
    return process, parent_pipe_write


@pytest.mark.skipif(os.name == "nt", reason="the parent-pipe guard is POSIX-only")
def test_parent_pipe_eof_stops_guarded_command_tree(tmp_path: Path) -> None:
    leader_pid_path = tmp_path / "leader.pid"
    child_pid_path = tmp_path / "child.pid"
    script = tmp_path / "tree.py"
    script.write_text(
        "\n".join(
            [
                "import os, pathlib, subprocess, sys",
                f"pathlib.Path({str(leader_pid_path)!r}).write_text(str(os.getpid()))",
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])",
                f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid))",
                "raise SystemExit(child.wait())",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    guard, parent_pipe_write = _start_guard([sys.executable, str(script)])
    leader_pid: int | None = None
    child_pid: int | None = None
    try:
        assert _wait_until(leader_pid_path.is_file)
        assert _wait_until(child_pid_path.is_file)
        leader_pid = int(leader_pid_path.read_text(encoding="utf-8"))
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        os.close(parent_pipe_write)
        parent_pipe_write = -1
        output, _ = guard.communicate(timeout=10)
        assert guard.returncode == 0, output
        assert _wait_until(lambda: not _pid_is_alive(leader_pid))
        assert _wait_until(lambda: not _pid_is_alive(child_pid))
    finally:
        if parent_pipe_write >= 0:
            os.close(parent_pipe_write)
        if guard.poll() is None:
            guard.kill()
            guard.wait(timeout=5)
        for pid in (leader_pid, child_pid):
            if pid is not None and _pid_is_alive(pid):
                os.kill(pid, signal.SIGKILL)


@pytest.mark.skipif(os.name == "nt", reason="the parent-pipe guard is POSIX-only")
def test_normal_leader_exit_kills_stubborn_descendant(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "stubborn.pid"
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
    guard, parent_pipe_write = _start_guard(
        [sys.executable, "-c", leader_code]
    )
    child_pid: int | None = None
    try:
        assert _wait_until(child_pid_path.is_file)
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        output, _ = guard.communicate(timeout=10)
        assert guard.returncode == 0, output
        assert _wait_until(lambda: not _pid_is_alive(child_pid))
    finally:
        os.close(parent_pipe_write)
        if guard.poll() is None:
            guard.kill()
            guard.wait(timeout=5)
        if child_pid is not None and _pid_is_alive(child_pid):
            os.kill(child_pid, signal.SIGKILL)
