"""Desktop launcher for the aiOS WebView2 shell.

Double-click this file (or its shortcut) to completely restart aiOS. The same
detached coordinator is used by the WebView2 header and tray menu.

Usage:
    pythonw launch_aios.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000


def _find_pythonw() -> str:
    base = Path(__file__).resolve().parent
    candidates = [
        base / ".venv" / "Scripts" / "pythonw.exe",
        Path(sys.executable).with_name("pythonw.exe"),
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    return sys.executable


def _create_desktop_shortcut() -> str | None:
    try:
        desktop = Path(os.environ.get("USERPROFILE", "")) / "Desktop"
        if not desktop.is_dir():
            return None

        shortcut_path = desktop / "aiOS.lnk"
        script_path = Path(__file__).resolve()
        pythonw = _find_pythonw()
        icon_path = script_path.parent / "assets" / "aios-logo.ico"
        icon_line = f"$s.IconLocation = '{icon_path}', 0\n" if icon_path.exists() else ""

        ps_script = f"""
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut('{shortcut_path}')
$s.TargetPath = '{pythonw}'
$s.Arguments = '"{script_path}"'
$s.WorkingDirectory = '{script_path.parent}'
$s.Description = 'aiOS Desktop Shell'
$s.WindowStyle = 7
{icon_line}$s.Save()
"""
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, timeout=10,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if shortcut_path.exists():
            return str(shortcut_path)
    except Exception:
        pass
    return None


def main() -> int:
    _create_desktop_shortcut()
    import aios_updater

    launched = aios_updater.spawn_relaunch(parent_pid=0, extra_args=["--fast-start"])
    return 0 if launched else 1


if __name__ == "__main__":
    raise SystemExit(main())
