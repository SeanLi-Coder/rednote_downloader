from __future__ import annotations

import argparse
import contextlib
import os
import select
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence


POLL_SECONDS = 0.1
TERM_GRACE_SECONDS = 2.0
KILL_GRACE_SECONDS = 2.0


def _live_process_group_members(
    process: subprocess.Popen[bytes],
) -> list[int] | None:
    process.poll()
    try:
        helper = subprocess.Popen(
            ["/bin/ps", "-axo", "pid=,pgid=,state=,uid="],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, _ = helper.communicate(timeout=2.0)
    except OSError:
        return None
    except subprocess.TimeoutExpired:
        helper.kill()
        helper.communicate()
        return None
    if helper.returncode != 0:
        return None
    members: list[int] = []
    current_uid = os.getuid()
    for line in stdout.splitlines():
        fields = line.split()
        if len(fields) != 4:
            continue
        try:
            pid, process_group, state, uid = (
                int(fields[0]),
                int(fields[1]),
                fields[2],
                int(fields[3]),
            )
        except ValueError:
            continue
        if process_group != process.pid or state.startswith("Z"):
            continue
        if pid == helper.pid:
            continue
        # macOS can transiently report the ps helper itself in a dying process
        # group with uid 0. Only same-user members can belong to this user-owned
        # command tree and are safe to signal individually.
        if uid != current_uid:
            continue
        members.append(pid)
    return members


def _process_group_exists(process: subprocess.Popen[bytes]) -> bool:
    # Reap the direct child before probing its group. On macOS an unreaped
    # session leader can otherwise make killpg report EPERM for a zombie-only
    # group even though no live process remains.
    leader_running = process.poll() is None
    members = _live_process_group_members(process)
    if members is not None:
        return bool(members)
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return leader_running
    return True


def _signal_process_group(
    process: subprocess.Popen[bytes],
    shutdown_signal: signal.Signals,
) -> None:
    try:
        os.killpg(process.pid, shutdown_signal)
        return
    except ProcessLookupError:
        return
    except PermissionError:
        members = _live_process_group_members(process)
        if members is None:
            raise
    for pid in members:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, shutdown_signal)


def _wait_for_process_group(
    process: subprocess.Popen[bytes],
    timeout: float,
) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while _process_group_exists(process):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def stop_process_group(
    process: subprocess.Popen[bytes],
    *,
    term_grace_seconds: float = TERM_GRACE_SECONDS,
    kill_grace_seconds: float = KILL_GRACE_SECONDS,
) -> None:
    _signal_process_group(process, signal.SIGTERM)
    if not _wait_for_process_group(process, term_grace_seconds):
        _signal_process_group(process, signal.SIGKILL)
        if not _wait_for_process_group(process, kill_grace_seconds):
            raise RuntimeError("The guarded process group did not stop")
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=kill_grace_seconds)


def _parent_pipe_closed(fd: int) -> bool:
    readable, _, _ = select.select([fd], [], [], 0)
    if not readable:
        return False
    try:
        return os.read(fd, 1) == b""
    except BlockingIOError:
        return False


def _command_exit_code(returncode: int) -> int:
    return returncode if returncode >= 0 else 128 + abs(returncode)


def run_guarded_command(
    command: Sequence[str],
    *,
    parent_pipe_fd: int,
) -> int:
    if os.name == "nt":
        raise RuntimeError("The POSIX process guard cannot run on Windows")
    if parent_pipe_fd < 0:
        raise ValueError("parent pipe FD must be non-negative")
    if not command or not command[0]:
        raise ValueError("a child command is required")

    stop_requested = threading.Event()
    handled = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        handled.append(signal.SIGHUP)
    previous: dict[signal.Signals, object] = {}
    process: subprocess.Popen[bytes] | None = None

    def request_stop(_: int, __: object) -> None:
        stop_requested.set()

    try:
        os.set_blocking(parent_pipe_fd, False)
        if _parent_pipe_closed(parent_pipe_fd):
            return 0
        for current in handled:
            previous[current] = signal.getsignal(current)
            signal.signal(current, request_stop)
        try:
            process = subprocess.Popen(
                list(command),
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            print(f"Could not start guarded command: {exc}", file=sys.stderr)
            return 127

        while True:
            returncode = process.poll()
            if returncode is not None:
                stop_process_group(process)
                return _command_exit_code(returncode)
            if stop_requested.is_set() or _parent_pipe_closed(parent_pipe_fd):
                stop_process_group(process)
                return 0
            stop_requested.wait(POLL_SECONDS)
    finally:
        try:
            if process is not None:
                stop_process_group(process)
        finally:
            for current, handler in previous.items():
                signal.signal(current, handler)
            with contextlib.suppress(OSError):
                os.close(parent_pipe_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=argparse.SUPPRESS)
    parser.add_argument("--parent-pipe-fd", required=True, type=int)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    try:
        return run_guarded_command(command, parent_pipe_fd=args.parent_pipe_fd)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Process guard stopped safely: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
