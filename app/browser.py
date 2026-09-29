from __future__ import annotations

import errno
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from .errors import DownloadCancelledError


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


COOKIE_DIAGNOSTIC_CODES = frozenset(
    {
        "cookie_decryption_failed",
        "cookie_permission_denied",
        "cookie_database_locked",
        "cookie_database_invalid",
        "cookie_storage_failed",
        "cookie_reader_failed",
        "chrome_data_directory_missing",
        "chrome_profile_invalid",
        "chrome_profile_missing",
        "cookie_database_missing",
        "cookie_access_unknown",
    }
)


def public_cookie_diagnostic_code(value: object) -> str:
    """Accept only known safe codes; never echo an arbitrary exception suffix."""
    code = str.strip(value).lower() if isinstance(value, str) else ""
    return code if code in COOKIE_DIAGNOSTIC_CODES else "cookie_access_unknown"


def _cookie_message_diagnostic(message: str) -> str | None:
    text = message.lower()
    if any(marker in text for marker in (
        "decrypt", "keychain", "secretbox", "encryption", "find-generic-password",
    )):
        return "cookie_decryption_failed"
    if any(marker in text for marker in (
        "permission denied", "access denied", "operation not permitted",
    )):
        return "cookie_permission_denied"
    if any(marker in text for marker in ("locked", "database is busy", "resource busy")):
        return "cookie_database_locked"
    return None


def _cookie_exception_attribute(error: BaseException, name: str) -> object:
    try:
        return getattr(error, name, None)
    except DownloadCancelledError:
        raise
    except Exception:
        return None


def _cookie_exception_chain(error: BaseException | None) -> list[BaseException]:
    """Bound traversal of causes, contexts and yt-dlp's retained exc_info."""
    pending = [error] if error is not None else []
    result: list[BaseException] = []
    seen: set[int] = set()
    while pending and len(result) < 16:
        current = pending.pop(0)
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (DownloadCancelledError, KeyboardInterrupt, SystemExit)):
            raise current
        result.append(current)
        pending.extend((current.__cause__, current.__context__))
        retained = _cookie_exception_attribute(current, "exc_info")
        if isinstance(retained, tuple) and len(retained) == 3:
            pending.append(retained[1])
    return result


def _cookie_system_diagnostic(error: BaseException) -> str | None:
    """Use numeric system errors before inspecting potentially private text."""
    if isinstance(error, OSError):
        if error.errno in {errno.EACCES, errno.EPERM}:
            return "cookie_permission_denied"
        if error.errno in {
            errno.ENOSPC, errno.EDQUOT, errno.EIO, errno.EROFS,
            errno.EMFILE, errno.ENFILE, errno.ENOMEM,
        }:
            return "cookie_storage_failed"
    if isinstance(error, sqlite3.DatabaseError):
        number = _cookie_exception_attribute(error, "sqlite_errorcode")
        if not isinstance(number, int):
            return None
        number &= 0xFF  # SQLite extended codes retain the base code in this byte.
        if number in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            return "cookie_database_locked"
        if number in {sqlite3.SQLITE_PERM, sqlite3.SQLITE_AUTH}:
            return "cookie_permission_denied"
        if number in {
            sqlite3.SQLITE_FULL, sqlite3.SQLITE_IOERR, sqlite3.SQLITE_CANTOPEN,
            sqlite3.SQLITE_READONLY, sqlite3.SQLITE_NOMEM,
        }:
            return "cookie_storage_failed"
        if number in {
            sqlite3.SQLITE_ERROR, sqlite3.SQLITE_SCHEMA, sqlite3.SQLITE_CORRUPT,
            sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_FORMAT,
        }:
            return "cookie_database_invalid"
    return None


def chrome_cookie_diagnostic(
    profile: str | None, error: BaseException | None = None
) -> str:
    """Return a safe, actionable reason without exposing paths or cookie data.

    The diagnostic probe itself must never escape: an ``OSError`` while checking
    directories or databases falls back to ``cookie_access_unknown`` so the
    caller's real business error category (``cookie_unavailable``) is preserved
    instead of being reclassified as a site response change. Only ``OSError`` is
    caught, so cancellation and interpreter-exit signals still propagate.
    """
    chain = _cookie_exception_chain(error)
    for current in chain:
        structured = public_cookie_diagnostic_code(
            _cookie_exception_attribute(current, "diagnostic_code")
        )
        if structured != "cookie_access_unknown":
            return structured
    for current in chain:
        system_code = _cookie_system_diagnostic(current)
        if system_code is not None:
            return system_code
    messages: list[str] = []
    for current in chain:
        try:
            messages.append(str(current)[:2048].lower())
        except DownloadCancelledError:
            raise
        except Exception:
            # Exception formatting is not part of the trusted public protocol.
            continue
    text = " ".join(messages)
    message_code = _cookie_message_diagnostic(text)
    if message_code is not None:
        return message_code
    for current in chain:
        if isinstance(current, PermissionError):
            return "cookie_permission_denied"
        if isinstance(current, sqlite3.DatabaseError):
            return "cookie_database_invalid"
        if isinstance(current, (ImportError, AttributeError, TypeError)):
            return "cookie_reader_failed"
    try:
        root = chrome_user_data_directory()
        if root is None or not root.is_dir():
            return "chrome_data_directory_missing"
        if profile is None:
            # yt-dlp chooses the newest Cookies database across this root, not
            # necessarily Default or the last foreground Chrome profile.
            profiles = [
                path for path in root.iterdir()
                if _CHROME_PROFILE_DIRECTORY_RE.fullmatch(path.name)
                and not path.is_symlink() and path.is_dir()
            ]
            databases = (
                path / relative for path in [root, *profiles]
                for relative in ("Network/Cookies", "Cookies")
            )
            if any(path.is_file() for path in databases):
                return "cookie_access_unknown"
            if not profiles:
                return "chrome_profile_missing"
            return "cookie_database_missing"
        selected = profile
        if not _CHROME_PROFILE_DIRECTORY_RE.fullmatch(selected):
            return "chrome_profile_invalid"
        profile_dir = root / selected
        if not profile_dir.is_dir():
            return "chrome_profile_missing"
        databases = (
            profile_dir / "Network/Cookies",
            profile_dir / "Cookies",
        )
        if not any(path.is_file() for path in databases):
            return "cookie_database_missing"
    except OSError:
        return "cookie_access_unknown"
    return "cookie_access_unknown"


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
