from __future__ import annotations

import os
import shutil
import subprocess
import sys
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


def open_chrome(url: str) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(
            ["/usr/bin/open", "-a", "Google Chrome", url],
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
                subprocess.Popen(
                    [candidate, url],
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
            subprocess.Popen(
                [executable, url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return
        except FileNotFoundError:
            continue
    raise RuntimeError("Google Chrome was not found")
