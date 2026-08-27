from __future__ import annotations

import http.client
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

import app.launcher_control as launcher_control_module

from app.launcher_control import (
    CONTROL_HOST,
    InvalidLauncherRecordError,
    LauncherControl,
    LauncherRecord,
    launcher_record_path,
    read_launcher_record,
    remove_launcher_record,
    write_launcher_record,
)
from app.runtime import STOP_TOKEN_HEADER


INSTANCE_ID = "a" * 32
STOP_TOKEN = "safe_launcher_stop_token_" + "b" * 32


def launcher_record(tmp_path: Path, **changes) -> LauncherRecord:
    values = {
        "app_id": "original-media-downloader",
        "build_id": "abcdef123456",
        "instance_id": INSTANCE_ID,
        "stop_token": STOP_TOKEN,
        "pid": max(2, os.getpid()),
        "target_port": 8765,
        "control_port": 18_765,
        "project_root": str(tmp_path.resolve()),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    values.update(changes)
    return LauncherRecord(**values)


def start_control(tmp_path: Path) -> tuple[LauncherControl, threading.Event]:
    stop_requested = threading.Event()
    control = LauncherControl(
        runtime_dir=tmp_path / "runtime",
        app_id="original-media-downloader",
        build_id="abcdef123456",
        instance_id=INSTANCE_ID,
        stop_token=STOP_TOKEN,
        target_port=8765,
        project_root=tmp_path,
        stop_requested=stop_requested,
    )
    control.start()
    return control, stop_requested


def request_json(
    port: int,
    path: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
) -> tuple[int, dict]:
    request = Request(
        f"http://{CONTROL_HOST}:{port}{path}",
        data=b"" if method == "POST" else None,
        headers=headers or {},
        method=method,
    )
    try:
        with urlopen(request, timeout=2.0) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        payload = exc.read()
        return exc.code, json.loads(payload.decode("utf-8")) if payload else {}


def test_launcher_record_round_trip_is_atomic_private_and_strict(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "runtime"
    record = launcher_record(tmp_path)
    path = write_launcher_record(runtime_dir, record)

    assert path == launcher_record_path(runtime_dir)
    assert read_launcher_record(runtime_dir) == record
    assert not list(runtime_dir.glob("*.tmp"))
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0

    payload = record.to_dict()
    payload["unexpected"] = "unsafe"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(InvalidLauncherRecordError):
        read_launcher_record(runtime_dir)


def test_launcher_record_rejects_duplicate_fields_and_symlinks(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    path = launcher_record_path(runtime_dir)
    record = launcher_record(tmp_path)
    payload = json.dumps(record.to_dict())
    path.write_text(
        payload.replace(
            '"app_id": "original-media-downloader"',
            '"app_id": "original-media-downloader", "app_id": "other"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(InvalidLauncherRecordError, match="duplicate"):
        read_launcher_record(runtime_dir)

    path.unlink()
    target = tmp_path / "record.json"
    target.write_text(payload, encoding="utf-8")
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are not available")
    with pytest.raises(InvalidLauncherRecordError):
        read_launcher_record(runtime_dir)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("app_id", "bad app"),
        ("build_id", ""),
        ("instance_id", "A" * 32),
        ("stop_token", "short"),
        ("pid", True),
        ("pid", 1),
        ("target_port", 0),
        ("target_port", 65_536),
        ("control_port", 0),
        ("control_port", 65_536),
        ("project_root", "relative/path"),
        ("started_at", "2026-08-27T12:00:00"),
    ],
)
def test_launcher_record_strictly_validates_fields(
    tmp_path: Path,
    field: str,
    value,
) -> None:
    with pytest.raises(InvalidLauncherRecordError):
        launcher_record(tmp_path, **{field: value})


def test_stale_instance_cannot_remove_a_new_launcher_record(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    record = launcher_record(tmp_path)
    write_launcher_record(runtime_dir, record)

    assert (
        remove_launcher_record(runtime_dir, instance_id="c" * 32) is False
    )
    assert read_launcher_record(runtime_dir) == record
    assert remove_launcher_record(runtime_dir, instance_id=INSTANCE_ID) is True
    assert read_launcher_record(runtime_dir) is None


def test_control_rejects_an_explicit_unsafe_pid(tmp_path: Path) -> None:
    with pytest.raises(InvalidLauncherRecordError):
        LauncherControl(
            runtime_dir=tmp_path / "runtime",
            app_id="original-media-downloader",
            build_id="abcdef123456",
            instance_id=INSTANCE_ID,
            stop_token=STOP_TOKEN,
            target_port=8765,
            project_root=tmp_path,
            stop_requested=threading.Event(),
            pid=0,
        )


def test_control_health_has_exact_identity_without_stop_token(tmp_path: Path) -> None:
    control, _ = start_control(tmp_path)
    try:
        first = control.start()
        second = control.start()
        assert second == first
        status, payload = request_json(first.control_port, "/health")
        assert status == 200
        assert payload == {
            "status": "starting",
            "app_id": first.app_id,
            "build_id": first.build_id,
            "instance_id": first.instance_id,
            "pid": first.pid,
            "target_port": first.target_port,
            "control_port": first.control_port,
            "project_root": first.project_root,
        }
        assert first.stop_token not in json.dumps(payload)
        assert read_launcher_record(tmp_path / "runtime") == first
    finally:
        control.close()
        control.close()
    assert read_launcher_record(tmp_path / "runtime") is None


def test_control_stop_requires_exact_header_and_sets_event(tmp_path: Path) -> None:
    control, stop_requested = start_control(tmp_path)
    record = control.record
    assert record is not None
    try:
        status, payload = request_json(record.control_port, "/stop", method="POST")
        assert status == 403
        assert payload == {"detail": "Invalid launcher stop token"}
        assert not stop_requested.is_set()

        status, _ = request_json(
            record.control_port,
            "/stop",
            method="POST",
            headers={STOP_TOKEN_HEADER: "wrong_" + "c" * 32},
        )
        assert status == 403
        assert not stop_requested.is_set()

        status, payload = request_json(
            record.control_port,
            "/stop",
            method="POST",
            headers={STOP_TOKEN_HEADER: STOP_TOKEN},
        )
        assert status == 200
        assert payload == {
            "status": "stopping",
            "instance_id": INSTANCE_ID,
        }
        assert stop_requested.wait(timeout=1.0)
    finally:
        control.close()


def test_control_publish_failure_closes_started_server_and_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    servers = []
    threads: list[threading.Thread] = []
    original_server = launcher_control_module._LauncherControlServer
    original_thread = threading.Thread

    def capture_server(*args, **kwargs):
        server = original_server(*args, **kwargs)
        servers.append(server)
        return server

    def capture_thread(*args, **kwargs):
        thread = original_thread(*args, **kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(
        launcher_control_module,
        "_LauncherControlServer",
        capture_server,
    )
    monkeypatch.setattr(launcher_control_module.threading, "Thread", capture_thread)
    monkeypatch.setattr(
        launcher_control_module,
        "write_launcher_record",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("publish failed")),
    )
    control = LauncherControl(
        runtime_dir=tmp_path / "runtime",
        app_id="original-media-downloader",
        build_id="abcdef123456",
        instance_id=INSTANCE_ID,
        stop_token=STOP_TOKEN,
        target_port=8765,
        project_root=tmp_path,
        stop_requested=threading.Event(),
    )

    with pytest.raises(OSError, match="publish failed"):
        control.start()

    assert len(servers) == 1
    assert servers[0].fileno() == -1
    assert len(threads) == 1
    assert not threads[0].is_alive()
    assert control.record is None
    assert read_launcher_record(tmp_path / "runtime") is None
    control.close()


def test_control_rejects_wrong_host_method_and_path(tmp_path: Path) -> None:
    control, stop_requested = start_control(tmp_path)
    record = control.record
    assert record is not None
    try:
        connection = http.client.HTTPConnection(
            CONTROL_HOST,
            record.control_port,
            timeout=2.0,
        )
        connection.putrequest("GET", "/health", skip_host=True)
        connection.putheader("Host", "attacker.example")
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.close()

        status, _ = request_json(record.control_port, "/stop", method="GET")
        assert status == 405
        status, _ = request_json(record.control_port, "/health", method="POST")
        assert status == 405
        status, _ = request_json(record.control_port, "/missing")
        assert status == 404
        assert not stop_requested.is_set()
    finally:
        control.close()


def test_close_does_not_remove_a_replacement_record(tmp_path: Path) -> None:
    control, _ = start_control(tmp_path)
    record = control.record
    assert record is not None
    replacement = launcher_record(
        tmp_path,
        instance_id="d" * 32,
        control_port=record.control_port,
    )
    write_launcher_record(tmp_path / "runtime", replacement)

    control.close()
    control.close()

    assert read_launcher_record(tmp_path / "runtime") == replacement


def test_control_can_restart_after_idempotent_close(tmp_path: Path) -> None:
    control, _ = start_control(tmp_path)
    first = control.record
    assert first is not None
    control.close()
    control.close()

    second = control.start()
    try:
        assert second.instance_id == first.instance_id
        assert read_launcher_record(tmp_path / "runtime") == second
        status, payload = request_json(second.control_port, "/health")
        assert status == 200
        assert payload["instance_id"] == INSTANCE_ID
    finally:
        control.close()
