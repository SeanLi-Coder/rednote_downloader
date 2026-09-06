from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


def chrome_user_agent(
    chrome_version: str,
    platform_name: str | None = None,
) -> str:
    platform_name = platform_name or sys.platform
    if platform_name == "darwin":
        platform_token = "Macintosh; Intel Mac OS X 10_15_7"
    elif platform_name.startswith("win"):
        platform_token = "Windows NT 10.0; Win64; x64"
    else:
        platform_token = "X11; Linux x86_64"
    return (
        f"Mozilla/5.0 ({platform_token}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{chrome_version} Safari/537.36"
    )


_CHROME_PROFILE_DIRECTORY_RE = re.compile(r"(?:Default|Profile [1-9][0-9]*)")
_CHROME_EPOCH_OFFSET_SECONDS = 11_644_473_600


def chrome_user_data_directory(platform_name: str | None = None) -> Path | None:
    platform_name = platform_name or sys.platform
    if platform_name == "darwin":
        return Path.home() / "Library/Application Support/Google/Chrome"
    if platform_name.startswith("win"):
        local_app_data = os.environ.get("LOCALAPPDATA")
        return (
            Path(local_app_data) / "Google/Chrome/User Data"
            if local_app_data
            else None
        )
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home) if config_home else Path.home() / ".config"
    return root / "google-chrome"


def _chrome_profile_order(user_data_dir: Path) -> list[str]:
    info_cache: dict[str, object] = {}
    last_used = ""
    local_state = user_data_dir / "Local State"
    try:
        state = json.loads(local_state.read_text(encoding="utf-8"))
        profile_state = state.get("profile") if isinstance(state, dict) else None
        if isinstance(profile_state, dict):
            cached = profile_state.get("info_cache")
            if isinstance(cached, dict):
                info_cache = cached
            value = profile_state.get("last_used")
            if isinstance(value, str):
                last_used = value
    except (OSError, TypeError, ValueError):
        pass

    candidates: set[str] = set()
    try:
        if user_data_dir.is_dir():
            candidates = {
                path.name
                for path in user_data_dir.iterdir()
                if path.is_dir()
                and _CHROME_PROFILE_DIRECTORY_RE.fullmatch(path.name)
            }
    except OSError:
        return []
    candidates.update(
        name
        for name in info_cache
        if isinstance(name, str)
        and _CHROME_PROFILE_DIRECTORY_RE.fullmatch(name)
        and (user_data_dir / name).is_dir()
    )

    def sort_key(name: str) -> tuple[int, float, str]:
        metadata = info_cache.get(name)
        active_time = 0.0
        if isinstance(metadata, dict):
            try:
                active_time = float(metadata.get("active_time") or 0)
            except (TypeError, ValueError, OverflowError):
                active_time = 0.0
        return (0 if name == last_used else 1, -active_time, name)

    return sorted(candidates, key=sort_key)


def _profile_has_unexpired_cookie(
    profile_dir: Path,
    domain: str,
    cookie_name: str,
    *,
    now: float,
) -> bool:
    databases = [
        path
        for path in (profile_dir / "Network/Cookies", profile_dir / "Cookies")
        if path.is_file()
    ]
    if not databases:
        return False
    try:
        databases.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
    except OSError:
        pass
    chrome_now = int((now + _CHROME_EPOCH_OFFSET_SECONDS) * 1_000_000)
    normalized_domain = domain.lower().lstrip(".")
    for database in databases:
        try:
            connection = sqlite3.connect(
                f"{database.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=1,
            )
            try:
                rows = connection.execute(
                    "SELECT host_key, value, encrypted_value, expires_utc "
                    "FROM cookies WHERE name = ?",
                    (cookie_name,),
                )
                for host, value, encrypted_value, expires in rows:
                    normalized_host = str(host or "").lower().lstrip(".")
                    if not (
                        normalized_host == normalized_domain
                        or normalized_host.endswith(f".{normalized_domain}")
                    ):
                        continue
                    if not value and not encrypted_value:
                        continue
                    try:
                        expires_value = int(expires or 0)
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if expires_value <= 0 or expires_value > chrome_now:
                        return True
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            continue
    return False


def select_chrome_profile_with_cookie(
    domain: str,
    cookie_name: str,
    *,
    user_data_dir: str | Path | None = None,
    now: float | None = None,
) -> str | None:
    root = (
        Path(user_data_dir).expanduser()
        if user_data_dir is not None
        else chrome_user_data_directory()
    )
    if root is None or not root.is_dir():
        return None
    timestamp = time.time() if now is None else now
    for profile in _chrome_profile_order(root):
        if _profile_has_unexpired_cookie(
            root / profile,
            domain,
            cookie_name,
            now=timestamp,
        ):
            return profile
    return None


def select_chrome_profile_with_cookies(
    domain: str,
    cookie_names: tuple[str, ...],
    *,
    user_data_dir: str | Path | None = None,
    now: float | None = None,
) -> str | None:
    if not cookie_names or any(not name for name in cookie_names):
        return None
    root = (
        Path(user_data_dir).expanduser()
        if user_data_dir is not None
        else chrome_user_data_directory()
    )
    if root is None or not root.is_dir():
        return None
    timestamp = time.time() if now is None else now
    for profile in _chrome_profile_order(root):
        if all(
            _profile_has_unexpired_cookie(
                root / profile,
                domain,
                cookie_name,
                now=timestamp,
            )
            for cookie_name in cookie_names
        ):
            return profile
    return None


def chrome_profile_has_cookie(
    profile_directory: str,
    domain: str,
    cookie_name: str,
    *,
    user_data_dir: str | Path | None = None,
    now: float | None = None,
) -> bool:
    value = profile_directory.strip()
    if not _CHROME_PROFILE_DIRECTORY_RE.fullmatch(value):
        return False
    root = (
        Path(user_data_dir).expanduser()
        if user_data_dir is not None
        else chrome_user_data_directory()
    )
    if root is None or not root.is_dir():
        return False
    return _profile_has_unexpired_cookie(
        root / value,
        domain,
        cookie_name,
        now=time.time() if now is None else now,
    )


def chrome_profile_has_cookies(
    profile_directory: str,
    domain: str,
    cookie_names: tuple[str, ...],
    *,
    user_data_dir: str | Path | None = None,
    now: float | None = None,
) -> bool:
    if not cookie_names or any(not name for name in cookie_names):
        return False
    return all(
        chrome_profile_has_cookie(
            profile_directory,
            domain,
            cookie_name,
            user_data_dir=user_data_dir,
            now=now,
        )
        for cookie_name in cookie_names
    )


def _chrome_profile_argument(profile_directory: str | None) -> str | None:
    if profile_directory is None:
        return None
    value = profile_directory.strip()
    if not _CHROME_PROFILE_DIRECTORY_RE.fullmatch(value):
        raise RuntimeError("The Chrome profile directory is invalid")
    return f"--profile-directory={value}"


def _macos_chrome_executable() -> Path | None:
    candidates = (
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )
    return next((path for path in candidates if path.is_file()), None)


def open_chrome(url: str, profile_directory: str | None = None) -> None:
    profile_argument = _chrome_profile_argument(profile_directory)
    if sys.platform == "darwin":
        if profile_argument:
            executable = _macos_chrome_executable()
            if executable is None:
                raise RuntimeError("Google Chrome was not found")
            command = [str(executable), profile_argument, url]
        else:
            command = ["/usr/bin/open", "-a", "Google Chrome", url]
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return
    if sys.platform.startswith("win"):
        candidates = [
            shutil.which("chrome.exe"),
            shutil.which("chrome"),
            str(
                Path(os.environ.get("LOCALAPPDATA", ""))
                / "Google/Chrome/Application/chrome.exe"
            ),
            str(
                Path(os.environ.get("PROGRAMFILES", ""))
                / "Google/Chrome/Application/chrome.exe"
            ),
            str(
                Path(os.environ.get("PROGRAMFILES(X86)", ""))
                / "Google/Chrome/Application/chrome.exe"
            ),
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                command = [candidate]
                if profile_argument:
                    command.append(profile_argument)
                command.append(url)
                subprocess.Popen(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=(
                        subprocess.CREATE_NEW_PROCESS_GROUP
                        | subprocess.DETACHED_PROCESS
                        | subprocess.CREATE_BREAKAWAY_FROM_JOB
                    ),
                )
                return
        raise RuntimeError("Google Chrome was not found")
    for executable in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    ):
        try:
            command = [executable]
            if profile_argument:
                command.append(profile_argument)
            command.append(url)
            subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return
        except FileNotFoundError:
            continue
    raise RuntimeError("Google Chrome was not found")
