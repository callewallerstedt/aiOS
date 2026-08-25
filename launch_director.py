"""Desktop launcher for the aiOS Director WebView2 shell.

Usage:
    pythonw launch_director.py
"""

from __future__ import annotations

from director_desktop import install_shortcuts, spawn_director


def main() -> int:
    install_shortcuts()
    spawn_director()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
