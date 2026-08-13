"""Run the web portal locally (no Docker) and open it in the default browser."""

from __future__ import annotations

import argparse
import threading
import webbrowser

import uvicorn


def _open_when_ready(url: str) -> None:
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="notchgen-web",
        description="Start the notchgen portal and open it in your browser.",
    )
    p.add_argument("--host", default="127.0.0.1", help="interface to bind (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8000, help="port to serve on (default 8000)")
    p.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    args = p.parse_args(argv)

    url = f"http://{args.host}:{args.port}"
    print(f"notchgen portal starting at {url}  (Ctrl-C to stop)")
    if not args.no_browser:
        _open_when_ready(url if args.host != "0.0.0.0" else f"http://127.0.0.1:{args.port}")

    uvicorn.run("notchgen.api:app", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
