"""Launch the TeraBox TUI file manager."""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="terabox-tui",
        description="Terminal file manager for TeraBox (dm.terabox.com web API).",
    )
    parser.add_argument(
        "--start",
        default="/",
        metavar="DIR",
        help="remote directory to open first (default: /)",
    )
    parser.add_argument(
        "--version", action="store_true", help="print version and exit"
    )
    args = parser.parse_args(argv)

    from terabox_tui import __version__

    if args.version:
        print(f"terabox-tui {__version__}")
        return 0

    from terabox_tui.app import TeraBoxApp

    app = TeraBoxApp()
    start = args.start
    if start and start != "/":
        app._cwd = start if start.startswith("/") else "/" + start
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
