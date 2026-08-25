"""USB-C companion for displaying the real aiOS WebView UI on Android.

The Android APK is deliberately only a WebView.  This process serves the same
``aios_ui/web`` files and CODE APIs as the desktop shell, keeps a stable local
port available, and maintains ``adb reverse`` whenever an authorised phone is
attached.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time

from .server import start_mirror_server


BASE_DIR = Path(__file__).resolve().parent.parent
ADB = BASE_DIR / ".tools" / "platform-tools" / "adb.exe"
MIRROR_PORT = 48738
TRANSCRIBE_PORT = 5000
MIRROR_COMPONENT = "com.aios.mirror/.MainActivity"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _adb(*args: str, timeout: float = 8.0) -> subprocess.CompletedProcess[str] | None:
    if not ADB.is_file():
        return None
    try:
        return subprocess.run(
            [str(ADB), *args],
            cwd=str(BASE_DIR),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def connected_devices() -> set[str]:
    """Return authorised ADB device serials, excluding offline/RSA-pending ones."""
    result = _adb("devices")
    if result is None or result.returncode != 0:
        return set()
    devices: set[str] = set()
    for raw in (result.stdout or "").splitlines()[1:]:
        fields = raw.strip().split()
        if len(fields) >= 2 and fields[1] == "device":
            devices.add(fields[0])
    return devices


def forward_and_open(serial: str, *, launch: bool) -> bool:
    """Refresh one phone's reverse tunnel and optionally bring mirror forward."""
    for port in (MIRROR_PORT, TRANSCRIBE_PORT):
        mapping = f"tcp:{port}"
        forwarded = _adb("-s", serial, "reverse", mapping, mapping)
        if forwarded is None or forwarded.returncode != 0:
            return False
    if not launch:
        return True
    started = _adb(
        "-s", serial, "shell", "am", "start",
        "-a", "android.intent.action.MAIN",
        "-c", "android.intent.category.LAUNCHER",
        "-n", MIRROR_COMPONENT,
    )
    return started is not None and started.returncode == 0


def run() -> None:
    server = start_mirror_server(MIRROR_PORT)
    opened: set[str] = set()
    try:
        while True:
            attached = connected_devices()
            opened.intersection_update(attached)
            for serial in attached:
                if forward_and_open(serial, launch=serial not in opened):
                    opened.add(serial)
            time.sleep(5.0)
    finally:
        server.stop()


def main() -> int:
    try:
        run()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
