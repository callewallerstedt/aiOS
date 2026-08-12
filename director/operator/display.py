"""The operator's screen.

Director drives a virtual X display (Xvfb :99) rather than the laptop's own
panel, for three reasons: the box is a laptop that is usually closed, a
virtual screen never fights the human for the mouse, and it can be handed over
from anywhere — x11vnc exports it and noVNC puts it in the phone's browser, so
"you do the login yourself" works on a train.

Xvfb, x11vnc and websockify run as systemd --user units so they outlive a
Director restart. This module starts them if they are not already up and
answers questions about their state.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from typing import Any

from .. import config

UNITS = {
    "xvfb": "aios-director-xvfb.service",
    "wm": "aios-director-wm.service",
    "vnc": "aios-director-x11vnc.service",
    "chrome": "aios-director-chrome.service",
}

# noVNC's static files ship in Ubuntu's `novnc` package. Director serves them
# itself and bridges the WebSocket to x11vnc's TCP port, so takeover rides the
# same authenticated origin as the rest of the API and no extra port is public.
NOVNC_ROOTS = ("/usr/share/novnc", "/usr/local/share/novnc")


def operator_settings(settings: dict[str, Any] | None = None) -> dict:
    cfg = settings if settings is not None else config.load_settings()
    return cfg.get("operator", {}) or {}


def display_name(settings: dict[str, Any] | None = None) -> str:
    return str(operator_settings(settings).get("display") or ":99")


def display_env(settings: dict[str, Any] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["DISPLAY"] = display_name(settings)
    env.setdefault("XAUTHORITY", "")
    return env


async def _run(argv: list[str], timeout: float = 20.0,
               env: dict[str, str] | None = None) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=env)
    except (FileNotFoundError, NotImplementedError, OSError) as exc:
        # No systemd here (a dev box, or Windows). Report it rather than raising
        # so /api/state still answers.
        return 127, f"{argv[0]} unavailable: {exc}"
    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "timed out"
    return proc.returncode or 0, raw.decode("utf-8", errors="replace").strip()


async def unit_active(unit: str) -> bool:
    code, out = await _run(["systemctl", "--user", "is-active", unit], timeout=10)
    return code == 0 and out.strip() == "active"


async def status(settings: dict[str, Any] | None = None) -> dict:
    cfg = operator_settings(settings)
    states = {}
    for key, unit in UNITS.items():
        states[key] = await unit_active(unit)
    return {
        "display": display_name(settings),
        "units": states,
        "ready": all(states.get(key) for key in ("xvfb", "wm", "vnc")),
        "vnc_port": int(cfg.get("vnc_port") or 5999),
        "novnc_port": int(cfg.get("novnc_port") or 6080),
        "width": int(cfg.get("width") or 1600),
        "height": int(cfg.get("height") or 900),
    }


async def ensure_running(settings: dict[str, Any] | None = None) -> dict:
    """Start any unit that is not active. Idempotent."""
    for unit in UNITS.values():
        if not await unit_active(unit):
            await _run(["systemctl", "--user", "start", unit], timeout=25)
    # Xvfb needs a moment before X clients can connect.
    for _ in range(20):
        if await unit_active(UNITS["xvfb"]):
            break
        await asyncio.sleep(0.25)
    await _run(["xsetroot", "-solid", "#2b2c2f"], timeout=5, env=display_env(settings))
    if not await chrome_running(settings):
        await launch_chrome("", settings)
    return await status(settings)


async def restart(settings: dict[str, Any] | None = None) -> dict:
    for unit in UNITS.values():
        await _run(["systemctl", "--user", "restart", unit], timeout=25)
    return await status(settings)


def chrome_binary() -> str:
    for name in ("google-chrome-stable", "google-chrome", "chromium-browser", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def chrome_argv(url: str = "", settings: dict[str, Any] | None = None) -> list[str]:
    """Flags that make Chrome paint on Xvfb instead of a black window."""
    binary = chrome_binary()
    cfg = operator_settings(settings)
    profile = str(config.chrome_profile_dir())
    width = int(cfg.get("width") or 1600)
    height = int(cfg.get("height") or 900)
    argv = [
        binary,
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--password-store=basic",
        "--disable-gpu",
        "--use-gl=angle",
        "--use-angle=swiftshader",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--ozone-platform=x11",
        f"--window-size={width},{height}",
        "--window-position=0,0",
        "--start-maximized",
    ]
    if url:
        argv.append(url)
    return argv


async def chrome_running(settings: dict[str, Any] | None = None) -> bool:
    profile = str(config.chrome_profile_dir())
    code, out = await _run(["pgrep", "-f", f"user-data-dir={profile}"], timeout=10)
    return code == 0 and bool(out.strip())


async def launch_chrome(url: str = "", settings: dict[str, Any] | None = None) -> str:
    """Open the persistent Chrome profile on the operator display.

    The profile lives in Director's data directory so web logins survive
    reboots. If Chrome is already up, this opens the URL in a new tab.

    Chrome's own output goes to chrome.log rather than /dev/null. Discarding it
    once cost a whole operator run: the browser never came up, every screenshot
    was black, and the only clue was the model saying so.
    """
    if not chrome_binary():
        return "no Chrome or Chromium found on the box"
    already = await chrome_running(settings)
    argv = chrome_argv(url, settings)

    log_path = config.home() / "chrome.log"
    try:
        handle = open(log_path, "ab", buffering=0)
    except OSError:
        handle = subprocess.DEVNULL
    process = subprocess.Popen(argv, env=display_env(settings),
                               stdout=handle, stderr=handle,
                               start_new_session=True)
    await asyncio.sleep(1.0 if already else 3.0)

    if process.poll() is not None and not await chrome_running(settings):
        tail = ""
        try:
            with open(log_path, "rb") as reader:
                tail = reader.read()[-600:].decode("utf-8", "replace")
        except OSError:
            pass
        return (f"Chrome exited immediately (code {process.returncode}). "
                f"Last output: {tail.strip()[-400:] or 'none'}")
    return f"opened {url or 'Chrome'}"


def novnc_root() -> str:
    """Where noVNC's static files live, or '' if the package is not installed."""
    for root in NOVNC_ROOTS:
        if os.path.isfile(os.path.join(root, "vnc.html")):
            return root
    return ""


def takeover_path() -> str:
    """Path the PWA opens for takeover, relative to the Director public URL.

    A thin viewer (no noVNC chrome) talks to `/vnc/ws` on the same origin.
    The token is added by the phone; the viewer forwards it onto the socket.
    """
    return "/vnc/view"
