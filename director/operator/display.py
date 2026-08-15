"""The operator's Linux desktop.

Director normally drives Calle's real GNOME/Xorg session on display ``:0``.
x11vnc exports that same session so the phone handoff and the physical laptop
show exactly what the agent sees. The old Xvfb path remains available only as
an explicit legacy mode.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from typing import Any

from .. import config

# Skip the systemd / xsetroot / chrome probe when we already know the
# display is up. Screenshot polls and VNC connects used to pay that cost
# on every call, which is why the operator screen felt slow to open.
_READY_TTL = 20.0
_ready_until = 0.0
_ready_status: dict[str, Any] | None = None
_recovery_lock: asyncio.Lock | None = None
_recovery_loop: asyncio.AbstractEventLoop | None = None

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
    return str(operator_settings(settings).get("display") or ":0")


def real_desktop(settings: dict[str, Any] | None = None) -> bool:
    return str(operator_settings(settings).get("mode") or "real").lower() != "virtual"


def xauthority(settings: dict[str, Any] | None = None) -> str:
    cfg = operator_settings(settings)
    configured = str(cfg.get("xauthority") or "").strip()
    if configured:
        return configured
    if not real_desktop(settings):
        return ""
    # No getuid on Windows, where this module is only ever imported by tests.
    uid = getattr(os, "getuid", lambda: 1000)()
    return f"/run/user/{uid}/gdm/Xauthority"


def display_env(settings: dict[str, Any] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["DISPLAY"] = display_name(settings)
    env["XAUTHORITY"] = xauthority(settings)
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


async def display_alive(settings: dict[str, Any] | None = None) -> bool:
    """True when something is already serving the operator DISPLAY."""
    code, _ = await _run(["xdpyinfo"], timeout=3, env=display_env(settings))
    return code == 0


async def unit_active(unit: str) -> bool:
    code, out = await _run(["systemctl", "--user", "is-active", unit], timeout=10)
    return code == 0 and out.strip() == "active"


async def status(settings: dict[str, Any] | None = None) -> dict:
    cfg = operator_settings(settings)
    active = await asyncio.gather(*(unit_active(unit) for unit in UNITS.values()))
    states = dict(zip(UNITS, active))
    desktop = await display_alive(settings)
    ready = desktop and states.get("vnc", False) if real_desktop(settings) else (
        desktop and all(states.get(key) for key in ("xvfb", "wm", "vnc")))
    return {
        "mode": "real" if real_desktop(settings) else "virtual",
        "display": display_name(settings),
        "desktop": desktop,
        "units": states,
        "ready": ready,
        "vnc_port": int(cfg.get("vnc_port") or 5999),
        "novnc_port": int(cfg.get("novnc_port") or 6080),
        "width": int(cfg.get("width") or 1600),
        "height": int(cfg.get("height") or 900),
    }


def reset_ready_cache() -> None:
    global _ready_until, _ready_status
    _ready_until = 0.0
    _ready_status = None


def _cached_ready() -> dict[str, Any] | None:
    if _ready_status and _ready_status.get("ready") and time.monotonic() < _ready_until:
        return _ready_status
    return None


def _remember_ready(state: dict[str, Any]) -> dict[str, Any]:
    global _ready_until, _ready_status
    if state.get("ready"):
        _ready_status = state
        _ready_until = time.monotonic() + _READY_TTL
    else:
        reset_ready_cache()
    return state


DISPLAY_KEYS = ("xvfb", "wm", "vnc")


def _get_recovery_lock() -> asyncio.Lock:
    """One repair at a time, without binding tests to an old event loop."""
    global _recovery_lock, _recovery_loop
    loop = asyncio.get_running_loop()
    if _recovery_lock is None or _recovery_loop is not loop:
        _recovery_lock = asyncio.Lock()
        _recovery_loop = loop
    return _recovery_lock


# Desktop nags that open on top of everything and take the keyboard. They are
# not part of any task, and on a box that gets restarted by deploys they pile
# up: five of them once sat behind Chrome while the operator typed a 2FA code
# into nothing for sixty steps. Clicks still landed on the page — only the
# keystrokes went to the modal — so nothing about the screenshot looked wrong.
NAG_TITLES = (
    "software updater",
    "upgrade available",
    "system program problem",
    "has experienced an internal error",
    "closed unexpectedly",
    "report problem",
    "crash report",
)


async def stray_dialogs(settings: dict[str, Any] | None = None) -> list[str]:
    """Titles of nag windows currently open on the operator display."""
    code, out = await _run(["wmctrl", "-l"], timeout=10, env=display_env(settings))
    if code != 0:
        return []
    found = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        window_id, title = parts[0], parts[3]
        if any(nag in title.casefold() for nag in NAG_TITLES):
            found.append(f"{window_id} {title}")
    return found


async def dismiss_stray_dialogs(settings: dict[str, Any] | None = None) -> list[str]:
    """Close those nags. Returns what was closed, for the run log."""
    closed = []
    for row in await stray_dialogs(settings):
        window_id, title = row.split(" ", 1)
        code, _ = await _run(["wmctrl", "-ic", window_id], timeout=10, env=display_env(settings))
        if code == 0:
            closed.append(title)
    return closed


async def ensure_running(settings: dict[str, Any] | None = None, *,
                         with_chrome: bool = False) -> dict:
    """Start the virtual display if it is down. Idempotent.

    Screenshot polls and VNC connects must not launch Chrome or paint
    xsetroot over a session that is already on screen — that is how the
    operator view used to jump between windows. Chrome is only started
    when a caller asks for it (an operator run that needs a browser).
    """
    cached = _cached_ready()
    if cached is not None and not with_chrome:
        return cached
    if real_desktop(settings):
        states = {"xvfb": False, "wm": False,
                  "vnc": await unit_active(UNITS["vnc"]), "chrome": False}
        desktop_up = await display_alive(settings)
        if desktop_up and not states["vnc"]:
            await _run(["systemctl", "--user", "start", UNITS["vnc"]], timeout=25)
            states["vnc"] = await unit_active(UNITS["vnc"])
        if with_chrome and desktop_up and not await chrome_running(settings):
            await launch_chrome("", settings)
            states["chrome"] = True
        cfg = operator_settings(settings)
        return _remember_ready({
            "mode": "real", "display": display_name(settings),
            "desktop": desktop_up, "units": states,
            "ready": desktop_up and states["vnc"],
            "vnc_port": int(cfg.get("vnc_port") or 5999),
            "novnc_port": int(cfg.get("novnc_port") or 6080),
            "width": int(cfg.get("width") or 1280),
            "height": int(cfg.get("height") or 720),
        })
    started_xvfb = False
    states: dict[str, bool] = {}
    display_up = await display_alive(settings)
    for key, unit in UNITS.items():
        if key == "chrome" and not with_chrome:
            continue
        if key == "xvfb" and display_up:
            states[key] = True
            continue
        active = await unit_active(unit)
        states[key] = active
        if not active:
            await _run(["systemctl", "--user", "start", unit], timeout=25)
            if key == "xvfb":
                started_xvfb = True
    if started_xvfb:
        for _ in range(20):
            if await unit_active(UNITS["xvfb"]):
                break
            await asyncio.sleep(0.25)
        await _run(["xsetroot", "-solid", "#2b2c2f"], timeout=5, env=display_env(settings))
    if with_chrome and not await chrome_running(settings):
        await launch_chrome("", settings)
    if started_xvfb:
        return _remember_ready(await status(settings))
    cfg = operator_settings(settings)
    return _remember_ready({
        "display": display_name(settings),
        "units": states,
        "ready": all(states.get(key) for key in DISPLAY_KEYS),
        "vnc_port": int(cfg.get("vnc_port") or 5999),
        "novnc_port": int(cfg.get("novnc_port") or 6080),
        "width": int(cfg.get("width") or 1600),
        "height": int(cfg.get("height") or 900),
    })


async def open_viewer_connection(settings: dict[str, Any] | None = None):
    """Connect to x11vnc without touching a healthy operator session.

    Viewing is a consumer of the display, not an operator startup. The common
    path is therefore one local TCP connection and no systemd/Chrome probes.
    Only a failed connection enters the serialized display-service recovery
    path, and that path explicitly never launches or restarts Chrome.
    """
    cfg = operator_settings(settings)
    port = int(cfg.get("vnc_port") or 5999)
    try:
        return await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=0.75)
    except (OSError, asyncio.TimeoutError):
        pass

    async with _get_recovery_lock():
        # Another viewer may have repaired the service while we waited.
        try:
            return await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), timeout=0.75)
        except (OSError, asyncio.TimeoutError):
            reset_ready_cache()
            await ensure_running(settings, with_chrome=False)

        last_error: BaseException | None = None
        for _ in range(12):
            try:
                return await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", port), timeout=0.75)
            except (OSError, asyncio.TimeoutError) as exc:
                last_error = exc
                await asyncio.sleep(0.15)
        raise ConnectionError(f"operator viewer is unavailable on port {port}") from last_error


async def restart_viewer(settings: dict[str, Any] | None = None) -> dict:
    """Repair only the VNC exporter; preserve Xvfb, WM and Chrome processes."""
    reset_ready_cache()
    await _run(["systemctl", "--user", "restart", UNITS["vnc"]], timeout=25)
    return _remember_ready(await status(settings))


async def restart(settings: dict[str, Any] | None = None) -> dict:
    """Backward-compatible safe restart used by the operator API."""
    return await restart_viewer(settings)


def chrome_binary() -> str:
    for name in ("google-chrome-stable", "google-chrome", "chromium-browser", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def chrome_argv(url: str = "", settings: dict[str, Any] | None = None) -> list[str]:
    """Build Chrome flags for the real desktop or legacy Xvfb mode."""
    binary = chrome_binary()
    cfg = operator_settings(settings)
    profile = str(config.chrome_profile_dir())
    width = int(cfg.get("width") or 1280)
    height = int(cfg.get("height") or 720)
    argv = [
        binary,
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        # Two different things. The infobar flag is the old one; current Chrome
        # shows a "Restore pages?" *bubble* instead, and that bubble takes the
        # keyboard. With only the infobar flag set, a restarted Chrome sat with
        # the bubble open and swallowed every keystroke the operator sent.
        "--disable-session-crashed-bubble",
        "--hide-crash-restore-bubble",
        "--restore-last-session",
        "--password-store=basic",
        "--disable-dev-shm-usage",
        "--ozone-platform=x11",
        f"--window-size={width},{height}",
        "--window-position=0,0",
        "--start-maximized",
    ]
    if not real_desktop(settings):
        argv[7:7] = ["--disable-gpu", "--use-gl=angle", "--use-angle=swiftshader"]
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
