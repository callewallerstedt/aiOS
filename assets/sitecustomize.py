"""Boot Director when the branded exe is started with no script.

Windows taskbar pins often drop shortcut arguments, so a pythonw copy
named aios-director.exe would otherwise start and exit immediately.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


def _boot() -> None:
    exe = Path(sys.executable).name.lower()
    if exe not in {"aios-director.exe", "aios-director-runtime.exe"}:
        return
    if any(str(arg).lower().endswith(".py") for arg in sys.argv):
        return
    if sys.argv and sys.argv[0] in {"-c", "-m"}:
        return
    script = Path(__file__).resolve().parent.parent / "director_shell.py"
    if not script.is_file():
        return
    root = str(script.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    sys.argv = [str(script), *sys.argv[1:]]
    runpy.run_path(str(script), run_name="__main__")
    raise SystemExit(0)


_boot()
