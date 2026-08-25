"""Launcher for the WebView2 aiOS shell.

Runs alongside helper_overlay.py during the port: `python aios_shell.py` opens
the new frontend, `python helper_overlay.py` still opens the Tk one.

When launched as the main entry point (the desktop shortcut or macropad button),
this script immediately hides its console window so the user only sees the
WebView2 panel.
"""

import sys


def _hide_console() -> None:
    """Hide the terminal window on Windows so aiOS lives solely as a GUI panel."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.user32.ShowWindow(
            ctypes.windll.kernel32.GetConsoleWindow(), 0  # SW_HIDE
        )
    except Exception:
        pass


from aios_ui.app import main

if __name__ == "__main__":
    _hide_console()
    sys.exit(main())
