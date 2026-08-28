from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

import stop
from app.build_info import APP_ID
from app.launcher_control import (
    LauncherRecord,
    read_launcher_record,
    write_launcher_record,
)
from app.runtime import RuntimeRecord, STOP_TOKEN_HEADER, write_runtime_record


INSTANCE_ID = "a" * 32
STOP_TOKEN = "safe_stop_token_abcdefghijklmnopqrstuvwxyz"
TEST_UID = 502


def runtime_record(
    *,
    project_root: Path = stop.PROJECT_ROOT,
    pid: int = 42_424,
    port: int = 18_765,
) -> RuntimeRecord:
    return RuntimeRecord(
        app_id=APP_ID,
        build_id="test-build",
        instance_id=INSTANCE_ID,
        stop_token=STOP_TOKEN,
        pid=pid,
        port=port,
        project_root=str(project_root.resolve()),
        started_at="2026-08-27T12:00:00+00:00",
    )


def matching_health(record: RuntimeRecord) -> dict[str, Any]:
    return {
        "status": "ok",
        "app_id": record.app_id,
        "build_id": record.build_id,
        "instance_id": record.instance_id,
        "server_pid": record.pid,
        "server_port": record.port,
    }


def launcher_record(
    tmp_path: Path,
    *,
    runtime: RuntimeRecord | None = None,
    pid: int = 43_434,
    target_port: int = 18_765,
) -> LauncherRecord:
    return LauncherRecord(
        app_id=APP_ID,
        build_id=runtime.build_id if runtime is not None else "test-build",
        instance_id=runtime.instance_id if runtime is not None else INSTANCE_ID,
        stop_token=runtime.stop_token if runtime is not None else STOP_TOKEN,
        pid=pid,
        target_port=target_port,
        control_port=19_876,
        project_root=(
            runtime.project_root
            if runtime is not None
            else str(stop.PROJECT_ROOT.resolve())
        ),
        started_at="2026-08-27T12:00:00+00:00",
    )


def matching_launcher_health(record: LauncherRecord) -> dict[str, Any]:
    return {
        "status": "starting",
        "app_id": record.app_id,
        "build_id": record.build_id,
        "instance_id": record.instance_id,
        "pid": record.pid,
        "target_port": record.target_port,
        "control_port": record.control_port,
        "project_root": record.project_root,
    }


def test_main_uses_the_new_default_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_stop_backend(**kwargs):
        captured.update(kwargs)
        return "not_running"

    monkeypatch.setattr(stop, "stop_backend", fake_stop_backend)

    assert stop.main(["--runtime-dir", str(tmp_path)]) == 0
    assert captured["port"] == stop.DEFAULT_SERVER_PORT == 8766


def test_tokenized_runtime_record_stops_only_matching_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = runtime_record()
    runtime_dir = tmp_path / "runtime"
    write_runtime_record(runtime_dir, record)
    posted: list[tuple[int, str, str, float]] = []

    monkeypatch.setattr(
        stop,
        "_fetch_health",
        lambda port, timeout: matching_health(record),
    )
    monkeypatch.setattr(
        stop,
        "_post_runtime_stop",
        lambda port, *, stop_token, instance_id, timeout: posted.append(
            (port, stop_token, instance_id, timeout)
        )
        or {"status": "stopping", "instance_id": instance_id},
    )
    monkeypatch.setattr(stop, "_wait_for_pid_exit", lambda pid, timeout: True)
    monkeypatch.setattr(
        stop,
        "_stop_legacy_macos_backend",
        lambda *args, **kwargs: pytest.fail("legacy fallback must not run"),
    )

    assert (
        stop.stop_backend(
            port=record.port,
            runtime_dir=runtime_dir,
            timeout=4.0,
        )
        == "stopped"
    )
    assert posted == [(record.port, STOP_TOKEN, INSTANCE_ID, 2.0)]


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [
        ("app_id", "different-app"),
        ("build_id", "different-build"),
        ("instance_id", "b" * 32),
        ("server_pid", 42_425),
        ("server_pid", True),
        ("server_port", 18_766),
        ("server_port", True),
    ],
)
def test_runtime_health_mismatch_never_posts_stop_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    wrong_value: Any,
) -> None:
    record = runtime_record()
    runtime_dir = tmp_path / "runtime"
    write_runtime_record(runtime_dir, record)
    health = matching_health(record)
    health[field] = wrong_value
    monkeypatch.setattr(stop, "_fetch_health", lambda port, timeout: health)
    monkeypatch.setattr(
        stop,
        "_post_runtime_stop",
        lambda *args, **kwargs: pytest.fail("mismatched runtime must not be stopped"),
    )

    with pytest.raises(stop.StopRefusedError, match="does not match"):
        stop.stop_backend(port=record.port, runtime_dir=runtime_dir)


def test_runtime_record_for_different_project_is_refused_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other_project = tmp_path / "other-project"
    other_project.mkdir()
    record = runtime_record(project_root=other_project)
    runtime_dir = tmp_path / "runtime"
    write_runtime_record(runtime_dir, record)
    monkeypatch.setattr(
        stop,
        "_fetch_health",
        lambda *args, **kwargs: pytest.fail("network must not be contacted"),
    )

    with pytest.raises(stop.StopRefusedError, match="different project"):
        stop.stop_backend(port=record.port, runtime_dir=runtime_dir)


def test_invalid_record_never_falls_back_to_pid_signaling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / f"runtime-{stop.DEFAULT_SERVER_PORT}.json").write_text(
        "not json",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        stop,
        "_stop_legacy_macos_backend",
        lambda *args, **kwargs: pytest.fail("invalid records must not use fallback"),
    )

    with pytest.raises(stop.StopRefusedError, match="invalid or unreadable"):
        stop.stop_backend(runtime_dir=runtime_dir, platform_name="darwin")


def test_invalid_runtime_record_does_not_block_valid_launcher_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / "runtime-18765.json").write_text("not json", encoding="utf-8")
    control = launcher_record(tmp_path, target_port=18_765)
    write_launcher_record(runtime_dir, control)
    stopped: list[int] = []
    monkeypatch.setattr(
        stop,
        "_stop_launcher",
        lambda candidate, *, port, project_root, timeout: stopped.append(port)
        or "stopped",
    )

    assert stop.stop_backend(port=18_765, runtime_dir=runtime_dir) == "stopped"
    assert stopped == [18_765]


def test_invalid_launcher_record_does_not_block_valid_runtime_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = runtime_record()
    runtime_dir = tmp_path / "runtime"
    write_runtime_record(runtime_dir, record)
    (runtime_dir / "launcher.json").write_text("not json", encoding="utf-8")
    posted: list[int] = []
    monkeypatch.setattr(
        stop,
        "_fetch_health",
        lambda port, timeout: matching_health(record),
    )
    monkeypatch.setattr(
        stop,
        "_post_runtime_stop",
        lambda port, **kwargs: posted.append(port)
        or {"status": "stopping", "instance_id": record.instance_id},
    )
    monkeypatch.setattr(stop, "_wait_for_pid_exit", lambda pid, timeout: True)

    assert stop.stop_backend(port=record.port, runtime_dir=runtime_dir) == "stopped"
    assert posted == [record.port]


def test_authenticated_stop_timeout_does_not_escalate_to_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = runtime_record()
    runtime_dir = tmp_path / "runtime"
    write_runtime_record(runtime_dir, record)
    monkeypatch.setattr(
        stop,
        "_fetch_health",
        lambda port, timeout: matching_health(record),
    )
    monkeypatch.setattr(
        stop,
        "_post_runtime_stop",
        lambda port, **kwargs: {
            "status": "stopping",
            "instance_id": record.instance_id,
        },
    )
    monkeypatch.setattr(stop, "_wait_for_pid_exit", lambda pid, timeout: False)
    monkeypatch.setattr(
        stop.os,
        "kill",
        lambda *args: pytest.fail("tokenized stop must not escalate to os.kill"),
    )

    with pytest.raises(stop.StopTimeoutError, match="did not exit"):
        stop.stop_backend(port=record.port, runtime_dir=runtime_dir)


def test_matching_runtime_prefers_launcher_and_waits_for_full_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = runtime_record()
    control = launcher_record(tmp_path, runtime=record, target_port=record.port)
    runtime_dir = tmp_path / "runtime"
    write_runtime_record(runtime_dir, record)
    write_launcher_record(runtime_dir, control)
    calls: list[tuple[int, int, float]] = []
    monkeypatch.setattr(stop, "_fetch_health", lambda port, timeout: matching_health(record))
    monkeypatch.setattr(
        stop,
        "_fetch_launcher_health",
        lambda control_port, timeout: matching_launcher_health(control),
    )
    monkeypatch.setattr(
        stop,
        "_stop_launcher",
        lambda candidate, *, port, project_root, timeout: calls.append(
            (candidate.pid, port, timeout)
        )
        or "stopped",
    )
    monkeypatch.setattr(
        stop,
        "_post_runtime_stop",
        lambda *args, **kwargs: pytest.fail("the launcher must supervise shutdown"),
    )

    assert stop.stop_backend(port=record.port, runtime_dir=runtime_dir) == "stopped"
    assert calls == [(control.pid, record.port, stop.DEFAULT_STOP_TIMEOUT_SECONDS)]


def test_launcher_for_different_target_port_is_not_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_dir = tmp_path / "runtime"
    control = launcher_record(tmp_path, target_port=8765)
    write_launcher_record(runtime_dir, control)
    monkeypatch.setattr(stop, "_fetch_health", lambda port, timeout: None)
    monkeypatch.setattr(
        stop,
        "_stop_launcher",
        lambda *args, **kwargs: pytest.fail("a different target must not be stopped"),
    )

    assert (
        stop.stop_backend(port=18_765, runtime_dir=runtime_dir)
        == "not_running"
    )
    assert read_launcher_record(runtime_dir) == control


def test_launcher_health_mismatch_with_live_pid_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = launcher_record(tmp_path)
    monkeypatch.setattr(
        stop,
        "_fetch_launcher_health",
        lambda control_port, timeout: {
            **matching_launcher_health(control),
            "instance_id": "b" * 32,
        },
    )
    monkeypatch.setattr(stop, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(
        stop,
        "_post_launcher_stop",
        lambda *args, **kwargs: pytest.fail("mismatched launcher must not be stopped"),
    )

    with pytest.raises(stop.StopRefusedError, match="does not match"):
        stop._stop_launcher(
            control,
            port=control.target_port,
            project_root=stop.PROJECT_ROOT,
            timeout=2.0,
        )


def test_dead_launcher_record_is_removed_without_pid_signaling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_dir = tmp_path / "runtime"
    control = launcher_record(tmp_path)
    write_launcher_record(runtime_dir, control)
    monkeypatch.setattr(stop, "_fetch_launcher_health", lambda *args, **kwargs: None)
    monkeypatch.setattr(stop, "_fetch_health", lambda *args, **kwargs: None)
    monkeypatch.setattr(stop, "_pid_exists", lambda pid: False)
    monkeypatch.setattr(
        stop.os,
        "kill",
        lambda *args: pytest.fail("a dead launcher must not be signaled"),
    )

    assert (
        stop.stop_backend(port=control.target_port, runtime_dir=runtime_dir)
        == "not_running"
    )
    assert read_launcher_record(runtime_dir) is None


def test_launcher_stop_timeout_never_escalates_to_pid_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = launcher_record(tmp_path)
    monkeypatch.setattr(
        stop,
        "_fetch_launcher_health",
        lambda *args, **kwargs: matching_launcher_health(control),
    )
    monkeypatch.setattr(
        stop,
        "_post_launcher_stop",
        lambda *args, **kwargs: {
            "status": "stopping",
            "instance_id": control.instance_id,
        },
    )
    monkeypatch.setattr(stop, "_wait_for_pid_exit", lambda pid, timeout: False)
    monkeypatch.setattr(
        stop.os,
        "kill",
        lambda *args: pytest.fail("authenticated launcher stop must not signal a PID"),
    )

    with pytest.raises(stop.StopTimeoutError, match="did not exit"):
        stop._stop_launcher(
            control,
            port=control.target_port,
            project_root=stop.PROJECT_ROOT,
            timeout=2.0,
        )


def test_dead_stale_runtime_record_is_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = runtime_record()
    runtime_dir = tmp_path / "runtime"
    path = write_runtime_record(runtime_dir, record)
    monkeypatch.setattr(stop, "_fetch_health", lambda *args, **kwargs: None)
    monkeypatch.setattr(stop, "_pid_exists", lambda pid: False)

    assert stop.stop_backend(port=record.port, runtime_dir=runtime_dir) == "not_running"
    assert not path.exists()


def test_zombie_process_counts_as_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stop.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(
        stop.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="Z+\n",
            stderr="",
        ),
    )

    assert stop._pid_exists(42_424) is False


class FakeResponse:
    def __init__(self, payload: dict[str, Any], *, status: int = 200) -> None:
        self.status = status
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.payload[:limit]


def test_stop_request_uses_header_not_url_or_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = []

    def fake_urlopen(request, *, timeout):
        requests.append((request, timeout))
        return FakeResponse(
            {"status": "stopping", "instance_id": INSTANCE_ID}
        )

    monkeypatch.setattr(stop, "urlopen", fake_urlopen)

    assert stop._post_runtime_stop(
        18_765,
        stop_token=STOP_TOKEN,
        instance_id=INSTANCE_ID,
        timeout=1.5,
    ) == {"status": "stopping", "instance_id": INSTANCE_ID}
    request, timeout = requests[0]
    assert request.full_url == "http://127.0.0.1:18765/api/runtime/stop"
    headers = {name.lower(): value for name, value in request.header_items()}
    assert headers[STOP_TOKEN_HEADER.lower()] == STOP_TOKEN
    assert STOP_TOKEN.encode() not in (request.data or b"")
    assert STOP_TOKEN not in request.full_url
    assert timeout == 1.5


def test_rejected_stop_request_does_not_disclose_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(request, *, timeout):
        raise HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(stop, "urlopen", fake_urlopen)

    with pytest.raises(stop.StopRefusedError) as caught:
        stop._post_runtime_stop(
            18_765,
            stop_token=STOP_TOKEN,
            instance_id=INSTANCE_ID,
            timeout=1.0,
        )
    assert STOP_TOKEN not in str(caught.value)


def configure_safe_legacy_backend(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pid: int = 51_515,
) -> None:
    monkeypatch.setattr(stop.os, "getuid", lambda: TEST_UID, raising=False)
    listener_results = iter([[pid], [pid], []])
    monkeypatch.setattr(
        stop,
        "_macos_listener_pids",
        lambda port: next(listener_results),
    )
    monkeypatch.setattr(
        stop,
        "_fetch_health",
        lambda port, timeout: {
            "status": "ok",
            "app_id": APP_ID,
            "build_id": "legacy-build",
        },
    )
    monkeypatch.setattr(stop, "_macos_process_uid", lambda candidate: TEST_UID)
    monkeypatch.setattr(
        stop,
        "_macos_process_start_time",
        lambda candidate: "Thu Aug 27 12:00:00 2026",
    )
    monkeypatch.setattr(
        stop,
        "_macos_process_cwd",
        lambda candidate: stop.PROJECT_ROOT.resolve(),
    )
    monkeypatch.setattr(
        stop,
        "_macos_process_command",
        lambda candidate: (
            f"{shlex.quote(sys.executable)} "
            f"{shlex.quote(str(stop.PROJECT_ROOT / 'run.py'))}"
        ),
    )


def test_macos_legacy_fallback_signals_only_twice_verified_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid = 51_515
    configure_safe_legacy_backend(monkeypatch, pid=pid)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(stop.os, "kill", lambda target, sig: signals.append((target, sig)))
    monkeypatch.setattr(stop, "_wait_for_pid_exit", lambda target, timeout: True)

    assert (
        stop.stop_backend(
            port=stop.LEGACY_PORT,
            runtime_dir=tmp_path / "missing-runtime",
            platform_name="darwin",
        )
        == "stopped"
    )
    assert signals == [(pid, signal.SIGTERM)]


@pytest.mark.parametrize(
    "unsafe_field",
    ["multiple_listeners", "wrong_health", "wrong_uid", "wrong_cwd", "wrong_command"],
)
def test_macos_legacy_fallback_refuses_any_failed_identity_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_field: str,
) -> None:
    pid = 52_525
    monkeypatch.setattr(stop.os, "getuid", lambda: TEST_UID, raising=False)
    monkeypatch.setattr(stop, "_macos_listener_pids", lambda port: [pid])
    monkeypatch.setattr(
        stop,
        "_fetch_health",
        lambda port, timeout: {
            "status": "ok",
            "app_id": APP_ID,
            "build_id": "legacy-build",
        },
    )
    monkeypatch.setattr(stop, "_macos_process_uid", lambda candidate: TEST_UID)
    monkeypatch.setattr(
        stop,
        "_macos_process_start_time",
        lambda candidate: "Thu Aug 27 12:00:00 2026",
    )
    monkeypatch.setattr(
        stop,
        "_macos_process_cwd",
        lambda candidate: stop.PROJECT_ROOT.resolve(),
    )
    monkeypatch.setattr(
        stop,
        "_macos_process_command",
        lambda candidate: f"{sys.executable} {stop.PROJECT_ROOT / 'run.py'}",
    )
    if unsafe_field == "multiple_listeners":
        monkeypatch.setattr(stop, "_macos_listener_pids", lambda port: [pid, pid + 1])
    elif unsafe_field == "wrong_health":
        monkeypatch.setattr(
            stop,
            "_fetch_health",
            lambda port, timeout: {"status": "ok", "app_id": "other-app"},
        )
    elif unsafe_field == "wrong_uid":
        monkeypatch.setattr(stop, "_macos_process_uid", lambda candidate: TEST_UID + 1)
    elif unsafe_field == "wrong_cwd":
        monkeypatch.setattr(
            stop,
            "_macos_process_cwd",
            lambda candidate: tmp_path.resolve(),
        )
    else:
        monkeypatch.setattr(
            stop,
            "_macos_process_command",
            lambda candidate: f"{sys.executable} unrelated.py",
        )
    monkeypatch.setattr(
        stop.os,
        "kill",
        lambda *args: pytest.fail("an unverified process must never be signaled"),
    )

    with pytest.raises(stop.StopRefusedError):
        stop.stop_backend(
            port=stop.LEGACY_PORT,
            runtime_dir=tmp_path / "missing-runtime",
            platform_name="darwin",
        )


def test_legacy_fallback_rechecks_identity_immediately_before_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = stop.LegacyCandidate(
        pid=61_616,
        uid=TEST_UID,
        started_at="Thu Aug 27 12:00:00 2026",
        command=f"{sys.executable} run.py",
        cwd=stop.PROJECT_ROOT,
        build_id="old",
    )
    second = stop.LegacyCandidate(
        pid=61_617,
        uid=TEST_UID,
        started_at="Thu Aug 27 12:00:00 2026",
        command=f"{sys.executable} run.py",
        cwd=stop.PROJECT_ROOT,
        build_id="old",
    )
    inspections = iter([first, second])
    monkeypatch.setattr(
        stop,
        "_inspect_legacy_macos_backend",
        lambda *args, **kwargs: next(inspections),
    )
    monkeypatch.setattr(
        stop,
        "_fetch_health",
        lambda *args, **kwargs: {"status": "ok", "app_id": APP_ID},
    )
    monkeypatch.setattr(
        stop.os,
        "kill",
        lambda *args: pytest.fail("a replaced listener must never be signaled"),
    )

    with pytest.raises(stop.StopRefusedError, match="changed during verification"):
        stop.stop_backend(
            port=stop.LEGACY_PORT,
            runtime_dir=tmp_path / "missing-runtime",
            platform_name="darwin",
        )


def test_legacy_fallback_is_limited_to_macos_legacy_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        stop,
        "_inspect_legacy_macos_backend",
        lambda *args, **kwargs: pytest.fail("unsafe fallback must not inspect processes"),
    )
    monkeypatch.setattr(
        stop,
        "_fetch_health",
        lambda *args, **kwargs: {"status": "ok", "app_id": APP_ID},
    )

    with pytest.raises(stop.StopRefusedError, match="only available on macOS"):
        stop.stop_backend(
            runtime_dir=tmp_path / "missing-runtime",
            platform_name="linux",
        )
    with pytest.raises(stop.StopRefusedError, match="legacy localhost port 8765"):
        stop.stop_backend(
            port=18_765,
            runtime_dir=tmp_path / "missing-runtime",
            platform_name="darwin",
        )


def test_no_record_and_no_legacy_listener_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stop, "_macos_listener_pids", lambda port: [])
    monkeypatch.setattr(
        stop.os,
        "kill",
        lambda *args: pytest.fail("no listener means no signal"),
    )

    assert (
        stop.stop_backend(
            runtime_dir=tmp_path / "missing-runtime",
            platform_name="darwin",
        )
        == "not_running"
    )
