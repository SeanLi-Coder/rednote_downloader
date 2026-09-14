from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import main
from app.downloader import DownloaderConfig


@pytest.fixture
def config_environment(monkeypatch, tmp_path):
    data = tmp_path / "data"
    original = main.AppConfig(
        download_dir=str(tmp_path / "original"), chrome_profile="Profile 1"
    )
    manager = SimpleNamespace(
        default_output_root=Path(original.download_dir),
        downloader_config=DownloaderConfig(
            cookie_browser="chrome", cookie_profile="Profile 1"
        ),
    )
    monkeypatch.setattr(main, "DATA_DIR", data)
    monkeypatch.setattr(main, "CONFIG_PATH", data / "config.json")
    monkeypatch.setattr(main, "config", original)
    monkeypatch.setattr(main, "manager", manager)
    main._save_config(original)
    return original, manager


def test_failed_config_save_keeps_disk_and_running_settings(
    config_environment, monkeypatch, tmp_path
):
    original, manager = config_environment
    saved = main.CONFIG_PATH.read_bytes()

    def fail_replace(self, target):
        raise OSError("Disk full")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(HTTPException) as failure:
        main.update_config(
            main.AppConfig(
                download_dir=str(tmp_path / "changed"),
                use_chrome_cookies=False,
                chrome_profile=None,
            )
        )

    assert failure.value.status_code == 500
    assert "previous settings are still in use" in failure.value.detail
    assert main.get_config() == original
    assert main.CONFIG_PATH.read_bytes() == saved
    assert manager.default_output_root == Path(original.download_dir)
    assert manager.downloader_config.cookie_browser == "chrome"
    assert manager.downloader_config.cookie_profile == "Profile 1"
    assert list(main.DATA_DIR.glob("*.tmp")) == []


def test_concurrent_config_writes_use_independent_temporary_files(
    config_environment, monkeypatch, tmp_path
):
    barrier = threading.Barrier(2)
    replacing = threading.Lock()
    replace = Path.replace
    temporary_paths = []

    def recorded_replace(self, target):
        assert replacing.acquire(blocking=False), "Configuration replacements overlapped"
        try:
            temporary_paths.append(self)
            return replace(self, target)
        finally:
            replacing.release()

    def concurrent_save(candidate):
        barrier.wait(timeout=5)
        main._save_config(candidate)

    monkeypatch.setattr(Path, "replace", recorded_replace)
    candidates = [
        main.AppConfig(download_dir=str(tmp_path / name), use_chrome_cookies=value)
        for name, value in [("one", True), ("two", False)]
    ]
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(concurrent_save, value) for value in candidates]
        for future in futures:
            future.result(timeout=10)

    assert len(set(temporary_paths)) == 2
    assert main._load_config() in candidates
    assert list(main.DATA_DIR.glob("*.tmp")) == []


def test_config_updates_publish_only_after_persistence(
    config_environment, monkeypatch, tmp_path
):
    original, manager = config_environment
    saving = threading.Event()
    release_save = threading.Event()
    save = main._save_config
    first = main.AppConfig(download_dir=str(tmp_path / "first"))
    second = main.AppConfig(
        download_dir=str(tmp_path / "second"), use_chrome_cookies=False
    )

    def delayed_save(candidate):
        if candidate == first:
            saving.set()
            assert release_save.wait(timeout=5)
        save(candidate)

    monkeypatch.setattr(main, "_save_config", delayed_save)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_save = executor.submit(main.update_config, first)
        try:
            assert saving.wait(timeout=5)
            assert main.config == original
            assert manager.default_output_root == Path(original.download_dir)
            second_save = executor.submit(main.update_config, second)
        finally:
            release_save.set()
        assert first_save.result(timeout=10) == first
        assert second_save.result(timeout=10) == second

    assert main.get_config() == second == main._load_config()
    assert manager.default_output_root == Path(second.download_dir)
    assert manager.downloader_config.cookie_browser is None


def test_create_job_uses_one_config_snapshot(config_environment, monkeypatch, tmp_path):
    original, manager = config_environment
    changed = main.AppConfig(
        download_dir=str(tmp_path / "changed"),
        use_chrome_cookies=False,
        chrome_profile="Profile 2",
    )
    calls = []

    def snapshot_then_update():
        main.config = changed
        return original.model_copy(deep=True)

    def create(url, **options):
        calls.append(options)
        return {"id": "created"}

    monkeypatch.setattr(main, "get_config", snapshot_then_update)
    monkeypatch.setattr(main, "_public_job", lambda job: job)
    manager.create_job = create

    assert main.create_job(main.CreateJobRequest(url="https://youtu.be/test")) == {
        "id": "created"
    }
    assert calls == [
        {
            "output_root": original.download_dir,
            "cookie_browser": "chrome",
            "cookie_profile": "Profile 1",
        }
    ]
