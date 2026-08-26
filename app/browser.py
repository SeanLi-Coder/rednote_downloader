from __future__ import annotations

import sys


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
