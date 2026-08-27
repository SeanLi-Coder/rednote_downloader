from __future__ import annotations

import pytest

from app.browser import chrome_user_agent
from app.douyin import discover_profile as discover_douyin_profile
from app.errors import AuthenticationRequiredError, TemporaryAccessError
from app.douyin import _cookie_jar_to_playwright as douyin_cookies
from app.xiaohongshu import discover_profile as discover_xhs_profile
from app.xiaohongshu import _cookie_jar_to_playwright as xhs_cookies


class FakeCookie:
    def __init__(self, domain: str, expires: int | float | None) -> None:
        self.name = "session"
        self.value = "secret"
        self.domain = domain
        self.path = "/"
        self.secure = True
        self.expires = expires

    def has_nonstandard_attr(self, name: str) -> bool:
        return name == "HttpOnly"


@pytest.mark.parametrize(
    ("converter", "domain"),
    [
        (xhs_cookies, ".xiaohongshu.com"),
        (douyin_cookies, ".douyin.com"),
    ],
)
def test_chrome_epoch_microseconds_are_converted_to_integer_unix_seconds(
    converter,
    domain: str,
) -> None:
    expected_unix_seconds = 2_000_000_000
    chrome_epoch_microseconds = (expected_unix_seconds + 11_644_473_600) * 1_000_000

    result = converter([FakeCookie(domain, chrome_epoch_microseconds)])

    assert result[0]["expires"] == expected_unix_seconds
    assert isinstance(result[0]["expires"], int)
    assert result[0]["httpOnly"] is True


@pytest.mark.parametrize(
    ("converter", "domain"),
    [
        (xhs_cookies, ".example.com"),
        (douyin_cookies, ".example.com"),
    ],
)
def test_cookie_conversion_filters_unrelated_domains(converter, domain: str) -> None:
    assert converter([FakeCookie(domain, 2_000_000_000)]) == []


@pytest.mark.parametrize(
    ("platform_name", "expected_token"),
    [
        ("darwin", "Macintosh; Intel Mac OS X"),
        ("win32", "Windows NT 10.0; Win64; x64"),
        ("linux", "X11; Linux x86_64"),
    ],
)
def test_chrome_user_agent_matches_host_platform(
    platform_name: str, expected_token: str
) -> None:
    value = chrome_user_agent("140.0.0.0", platform_name)

    assert expected_token in value
    assert "Chrome/140.0.0.0" in value


@pytest.mark.parametrize(
    ("extractor_path", "discover", "url"),
    [
        (
            "app.xiaohongshu._extract_chrome_cookies",
            discover_xhs_profile,
            "https://www.xiaohongshu.com/user/profile/example",
        ),
        (
            "app.douyin._extract_cookies",
            discover_douyin_profile,
            "https://www.douyin.com/user/example",
        ),
    ],
)
def test_profile_cookie_access_failure_never_silently_falls_back(
    monkeypatch, extractor_path, discover, url
) -> None:
    def fail_cookie_read(profile):
        raise OSError("Could not copy Chrome cookie database")

    if extractor_path == "app.douyin._extract_cookies":
        def require_browser_fallback(*args, **kwargs):
            raise AuthenticationRequiredError(
                "Signed profile unavailable",
                verification_url=url,
            )

        monkeypatch.setattr(
            "app.douyin.fetch_signed_profile_awemes",
            require_browser_fallback,
        )
    monkeypatch.setattr(extractor_path, fail_cookie_read)

    with pytest.raises(TemporaryAccessError, match="Fully quit Chrome") as error:
        discover(url)

    assert "verification page is not required" in str(error.value)
