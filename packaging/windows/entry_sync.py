"""PyInstaller entry point: the hidden background auto-sync (`chatbridge watch` without a console window)."""

import os
import sys

from chatbridge.cli import main

if __name__ == "__main__":
    log = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "ChatBridge", "watch.log")
    sys.exit(main(["watch", "--interval", "20", "--log-file", log, *sys.argv[1:]]))
