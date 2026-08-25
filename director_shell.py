"""Desktop entry point for the aiOS Director WebView2 shell.

Double-click the ``aiOS Director`` shortcut (or run ``pythonw director_shell.py``)
to open Director as its own taskbar app with the aiOS icon.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _crash_log() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "aiOS"
    root.mkdir(parents=True, exist_ok=True)
    return root / "director-shell.log"


def _run() -> int:
    from director_desktop import _hide_console, main

    _hide_console()
    return int(main() or 0)


if __name__ == "__main__":
    try:
        raise SystemExit(_run())
    except Exception:
        try:
            _crash_log().write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        raise
