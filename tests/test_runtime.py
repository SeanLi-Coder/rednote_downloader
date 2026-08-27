from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.runtime import (
    ENV_INSTANCE_ID,
    ENV_PROJECT_LOCK_FD,
    ENV_RUNTIME_DIR,
    ENV_SERVER_PORT,
    ENV_STOP_TOKEN,
    InvalidRuntimeRecordError,
    ProjectLock,
    ProjectLockHeldError,
    RuntimeRecord,
    read_runtime_record,
    record_path,
    remove_runtime_record,
    write_runtime_record,
)


def runtime_record(tmp_path: Path, **changes) -> RuntimeRecord:
    values = {
        "app_id": "original-media-downloader",
        "build_id": "abcdef123456",
        "instance_id": "a" * 32,
        "stop_token": "safe_stop_token_" + "b" * 32,
        "pid": 12345,
        "port": 18765,
        "project_root": str(tmp_path.resolve()),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    values.update(changes)
    return RuntimeRecord(**values)


def test_environment_variable_names_are_stable() -> None:
    assert ENV_INSTANCE_ID == "OMD_INSTANCE_ID"
    assert ENV_STOP_TOKEN == "OMD_STOP_TOKEN"
    assert ENV_SERVER_PORT == "OMD_SERVER_PORT"
    assert ENV_PROJECT_LOCK_FD == "OMD_PROJECT_LOCK_FD"
    assert ENV_RUNTIME_DIR == "OMD_RUNTIME_DIR"


def test_project_lock_is_exclusive_and_stale_file_is_harmless(tmp_path) -> None:
    path = tmp_path / "project.lock"
    path.write_bytes(b"stale metadata is not ownership")
    first = ProjectLock(path).acquire()
    second = ProjectLock(path)
    try:
        assert first.fd is not None
        if os.name == "nt":
            assert os.get_handle_inheritable(first.fd) is False
        else:
            assert os.get_inheritable(first.fd) is False
        with pytest.raises(ProjectLockHeldError):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


def test_project_lock_release_is_idempotent(tmp_path) -> None:
    lock = ProjectLock(tmp_path / "project.lock").acquire()
    lock.release()
    lock.release()
    assert lock.fd is None


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX descriptor duplication")
def test_inherited_lock_release_only_closes_its_descriptor(tmp_path) -> None:
    path = tmp_path / "project.lock"
    launcher_lock = ProjectLock(path).acquire()
    assert launcher_lock.fd is not None
    inherited_fd = os.dup(launcher_lock.fd)
    os.set_inheritable(inherited_fd, True)
    inherited = ProjectLock.from_inherited_fd(path, inherited_fd)
    inherited.release()

    contender = ProjectLock(path)
    try:
        with pytest.raises(ProjectLockHeldError):
            contender.acquire()
    finally:
        launcher_lock.release()

    contender.acquire()
    contender.release()


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX descriptor duplication")
def test_inherited_lock_fd_must_match_the_lock_path(tmp_path) -> None:
    expected = tmp_path / "expected.lock"
    other = ProjectLock(tmp_path / "other.lock").acquire()
    assert other.fd is not None
    duplicate = os.dup(other.fd)
    try:
        expected.write_bytes(b"\0")
        with pytest.raises(Exception):
            ProjectLock.from_inherited_fd(expected, duplicate)
    finally:
        os.close(duplicate)
        other.release()


def test_project_lock_refuses_a_symbolic_link(tmp_path) -> None:
    target = tmp_path / "target"
    target.write_text("target", encoding="utf-8")
    link = tmp_path / "project.lock"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are not available")

    with pytest.raises(Exception):
        ProjectLock(link).acquire()


def test_runtime_record_round_trip_is_atomic_and_private(tmp_path) -> None:
    runtime_dir = tmp_path / "runtime"
    record = runtime_record(tmp_path)
    path = write_runtime_record(runtime_dir, record)

    assert path == record_path(runtime_dir, record.port)
    assert read_runtime_record(runtime_dir, record.port) == record
    assert read_runtime_record(path) == record
    assert not list(runtime_dir.glob("*.tmp"))
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0


def test_missing_runtime_record_returns_none(tmp_path) -> None:
    assert read_runtime_record(tmp_path / "runtime", 18765) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("app_id", "bad app"),
        ("build_id", ""),
        ("instance_id", "A" * 32),
        ("stop_token", "short"),
        ("pid", True),
        ("pid", 0),
        ("port", 65_536),
        ("project_root", "relative/path"),
        ("started_at", "2026-08-27T12:00:00"),
    ],
)
def test_runtime_record_strictly_validates_fields(tmp_path, field, value) -> None:
    with pytest.raises(InvalidRuntimeRecordError):
        runtime_record(tmp_path, **{field: value})


def test_runtime_record_rejects_unknown_fields(tmp_path) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    path = record_path(runtime_dir, 18765)
    payload = runtime_record(tmp_path).to_dict()
    payload["unexpected"] = "unsafe"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(InvalidRuntimeRecordError):
        read_runtime_record(runtime_dir, 18765)


def test_runtime_record_rejects_symlink(tmp_path) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    target = tmp_path / "record.json"
    target.write_text(json.dumps(runtime_record(tmp_path).to_dict()), encoding="utf-8")
    path = record_path(runtime_dir, 18765)
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are not available")

    with pytest.raises(InvalidRuntimeRecordError):
        read_runtime_record(runtime_dir, 18765)


def test_remove_runtime_record_requires_exact_instance(tmp_path) -> None:
    runtime_dir = tmp_path / "runtime"
    record = runtime_record(tmp_path)
    path = write_runtime_record(runtime_dir, record)

    assert (
        remove_runtime_record(runtime_dir, record.port, instance_id="c" * 32)
        is False
    )
    assert path.is_file()
    assert (
        remove_runtime_record(
            runtime_dir,
            record.port,
            instance_id=record.instance_id,
        )
        is True
    )
    assert not path.exists()
    assert (
        remove_runtime_record(
            runtime_dir,
            record.port,
            instance_id=record.instance_id,
        )
        is False
    )


def test_record_path_rejects_unsafe_ports(tmp_path) -> None:
    for value in (0, 65_536, True):
        with pytest.raises(ValueError):
            record_path(tmp_path, value)
