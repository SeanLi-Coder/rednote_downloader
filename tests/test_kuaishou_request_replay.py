"""Real browser requests to loopback fixtures; never user profiles or sites."""

from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import kuaishou as ks
from app.errors import DiscoveryError, DownloadCancelledError, TemporaryAccessError

@pytest.fixture(scope="module")
def chrome_channel_available():
    """Run loopback browser tests whenever Playwright can launch Chrome."""
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(
                channel="chrome", headless=True, args=["--no-proxy-server"]
            )
        except PlaywrightError as exc:
            message = str(exc)
            if (
                "Executable doesn't exist" in message
                or "Chromium distribution 'chrome' is not found" in message
            ):
                pytest.skip("The optional Chrome channel is not installed")
            raise
        else:
            browser.close()


@pytest.mark.parametrize(
    ("method", "redirect", "expected_method", "expected_body"),
    [
        ("POST", 301, "GET", b""),
        ("POST", 302, "GET", b""),
        ("POST", 303, "GET", b""),
        ("HEAD", 303, "HEAD", b""),
        ("POST", 307, "POST", b"fixture-body"),
        ("POST", 308, "POST", b"fixture-body"),
    ],
)
def test_redirect_replay_preserves_method_and_exact_body(
    method, redirect, expected_method, expected_body, chrome_channel_available
):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_request(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            received.append((self.path, self.command, body, dict(self.headers)))
            if self.path == "/start":
                self.send_response(redirect)
                self.send_header("Location", "/finish")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            content = b"<!doctype html><title>Fixture</title>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(content)

        do_GET = do_POST = do_HEAD = handle_request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    route_errors = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            try:
                context = browser.new_context(service_workers="block")

                def checked(route):
                    try:
                        ks.fulfill_checked_route(
                            route,
                            allowed=lambda url: url.startswith(base + "/"),
                            should_cancel=lambda: False,
                        )
                    except (
                        DiscoveryError, DownloadCancelledError, TemporaryAccessError, PlaywrightError
                    ) as exc:
                        route_errors.append(type(exc).__name__)
                        route.abort()

                context.route("**/*", checked)
                page = context.new_page()
                page.set_default_timeout(5000)
                page.goto(base + "/page")
                received.clear()
                options = {"method": method}
                if method == "POST":
                    options.update(
                        body="fixture-body",
                        headers={"content-type": "application/x-fixture"},
                    )
                page.evaluate(
                    "async options => { const response = await fetch('/start', options); "
                    "await response.arrayBuffer(); }",
                    options,
                )
                assert not route_errors
                requests = [entry for entry in received if entry[0] in {"/start", "/finish"}]
                assert len(requests) == 2
                assert requests[0][1:3] == (
                    method, b"fixture-body" if method == "POST" else b""
                )
                assert requests[1][1:3] == (expected_method, expected_body)
                if expected_method == "GET":
                    assert not any(
                        name.lower() in {"content-type", "content-length"}
                        for name in requests[1][3]
                    )
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_empty_sanitized_headers_never_restore_authorization_across_hosts(
    chrome_channel_available,
):
    """Exercise the real request API's empty-header fallback with synthetic auth."""
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            received.append((self.path, self.headers.get("Authorization")))
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{self.server.server_port}/finish")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            content = b"<!doctype html><title>Fixture</title>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origins = [f"http://{host}:{server.server_port}/" for host in ("127.0.0.1", "localhost")]
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            try:
                context = browser.new_context(service_workers="block")

                def checked(route):
                    # Chrome normally adds User-Agent and Accept headers. Strip
                    # those at this test boundary to exercise the legal empty
                    # sanitized case while route.fetch still uses its ORIGINAL
                    # real request (and would inherit the synthetic auth header).
                    request = route.request
                    wrapper = SimpleNamespace(
                        request=SimpleNamespace(
                            url=request.url,
                            method=request.method,
                            post_data_buffer=request.post_data_buffer,
                            headers={"authorization": "Bearer fixture-only"},
                        ),
                        fetch=route.fetch,
                        fulfill=route.fulfill,
                    )
                    ks.fulfill_checked_route(
                        wrapper,
                        allowed=lambda url: any(url.startswith(origin) for origin in origins),
                        should_cancel=lambda: False,
                    )

                context.route("**/*", checked)
                page = context.new_page()
                page.set_default_timeout(5000)
                page.goto(origins[0] + "page")
                received.clear()
                page.evaluate(
                    "async () => { const response = await fetch('/start', "
                    "{headers: {authorization: 'Bearer fixture-only'}}); "
                    "await response.arrayBuffer(); }"
                )
                requests = [entry for entry in received if entry[0] in {"/start", "/finish"}]
                assert requests == [("/start", None), ("/finish", None)]
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
