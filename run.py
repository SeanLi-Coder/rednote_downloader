from __future__ import annotations

import argparse
import threading

import uvicorn

from app.main import _open_chrome


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the media downloader web app")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if not args.no_browser:
        url = f"http://127.0.0.1:{args.port}"

        def open_ui() -> None:
            try:
                _open_chrome(url)
            except RuntimeError as exc:
                print(f"Could not open Chrome automatically: {exc}")

        threading.Timer(1.25, open_ui).start()

    uvicorn.run("app.main:app", host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
