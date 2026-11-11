"""Launcher that works from ANY current directory.

``python -m app`` only works when the *current directory* is the project folder.
Windows Task Scheduler / NSSM start processes in ``C:\\Windows\\System32``, which
would make the ``app`` package unimportable. This script puts its own folder on
``sys.path`` first, so a shortcut/service can start the app with an absolute path
from anywhere:

    C:\\vision-automation\\.venv\\Scripts\\pythonw.exe C:\\vision-automation\\run.py ^
        --config C:\\vision-automation\\config.yaml --no-webcam

It is a thin wrapper: every CLI flag of ``python -m app`` works identically.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from app.main import main as app_main

    return int(app_main())


if __name__ == "__main__":
    raise SystemExit(main())
