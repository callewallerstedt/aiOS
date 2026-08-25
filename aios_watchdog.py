"""Keep the local aiOS desktop stack healthy after Windows sign-in."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import urllib.request


BASE_DIR = Path(__file__).resolve().parent
HELPER_HEARTBEAT = BASE_DIR / ".aios-helper-heartbeat"
AHK_HEARTBEAT = BASE_DIR / ".aios-ahk-heartbeat"
RELAY_HEARTBEAT = BASE_DIR / ".aios-phone-relay-heartbeat"
DIRECTOR_HEARTBEAT = BASE_DIR / ".aios-director-client-heartbeat"
HEALTH_PATH = BASE_DIR / ".aios-health.json"
UPDATE_HEALTH_PATH = BASE_DIR / ".aios-update-health.json"
UPDATE_REQUEST_PATH = BASE_DIR / ".aios-update-request"
OPERATOR_STATUS_PATH = BASE_DIR / "phone_operator_events" / "status.json"
LOG_PATH = BASE_DIR / "aios-watchdog.log"
BRIDGE_START_LOG = BASE_DIR / "phone-bridge-start.log"
CONFIG_PATH = BASE_DIR / "helper_config.json"
MUTEX_NAME = "Local\\aiOS.Desktop.Watchdog.Singleton"
CHECK_INTERVAL = 10
HELPER_TIMEOUT = 40
AHK_TIMEOUT = 35
RELAY_TIMEOUT = 90
DIRECTOR_TIMEOUT = 90
AUTO_UPDATE_INTERVAL = 60
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DETACHED_PROCESS = 0x00000008 if os.name == "nt" else 0
_MUTEX_HANDLE = None
_UPDATE_LOCK = threading.Lock()
_UPDATE_RESULT = {"state": "idle", "message": "Auto-update ready", "updated_at": 0}


def log(message: str) -> None:
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 512 * 1024:
            LOG_PATH.replace(LOG_PATH.with_suffix(".log.old"))
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


def claim_single_instance() -> bool:
    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return True
    if kernel32.GetLastError() == 183:
        kernel32.CloseHandle(handle)
        return False
    _MUTEX_HANDLE = handle
    return True


def is_fresh(path: Path, timeout: float, now: float | None = None) -> bool:
    try:
        age = (time.time() if now is None else now) - path.stat().st_mtime
        return age <= timeout
    except OSError:
        return False


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def phone_enabled(config: dict) -> bool:
    relay = config.get("phone_relay") if isinstance(config, dict) else {}
    return bool(isinstance(relay, dict) and relay.get("enabled") and relay.get("machine_token"))


def director_enabled() -> bool:
    try:
        data = json.loads((BASE_DIR / "aios_director_client.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(str(data.get("url") or "").strip() and str(data.get("token") or "").strip())


def ensure_director_client(pythonw: str, now: float,
                           grace_until: float) -> tuple[str, float, bool]:
    """Keep the outbound Director machine link alive and self-healing."""
    if not director_enabled():
        return "not paired", grace_until, False
    if is_fresh(DIRECTOR_HEARTBEAT, DIRECTOR_TIMEOUT, now):
        return "healthy", grace_until, False
    if now < grace_until:
        return "starting", grace_until, False
    stop_python_script("director_client.py")
    spawn([pythonw, str(BASE_DIR / "director_client.py")])
    return "restarting", now + 45, True


def ensure_phone_bridge(config: dict, now: float, grace_until: float) -> tuple[str, float, bool]:
    """Keep the local CODE/OPERATOR API alive, with or without phone pairing."""
    paired = phone_enabled(config)
    backend_ok = local_bridge_healthy()
    relay_ok = is_fresh(RELAY_HEARTBEAT, RELAY_TIMEOUT, now) if paired else False

    if paired:
        status = "healthy" if backend_ok and relay_ok else "restarting"
        needs_start = not backend_ok or not relay_ok
    else:
        status = "not paired" if backend_ok else "local bridge restarting"
        needs_start = not backend_ok

    started = False
    if needs_start and now >= grace_until:
        if paired and not relay_ok:
            stop_python_script("phone_relay.py")
        start_phone_bridge()
        grace_until = now + 45
        started = True
    return status, grace_until, started


def find_pythonw() -> str:
    candidates = [
        BASE_DIR / ".venv" / "Scripts" / "pythonw.exe",
        Path(sys.executable).with_name("pythonw.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return shutil.which("pythonw.exe") or shutil.which("python.exe") or sys.executable


def find_autohotkey() -> str:
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates = [
        Path(r"C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe"),
        local / "Programs" / "AutoHotkey" / "v2" / "AutoHotkey64.exe",
        local / "Programs" / "AutoHotkey" / "v2" / "AutoHotkey32.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return shutil.which("AutoHotkey.exe") or ""


def spawn(command: list[str], *, output_path: Path | None = None) -> None:
    output = None
    try:
        output = output_path.open("a", encoding="utf-8") if output_path else None
        subprocess.Popen(
            command,
            cwd=str(BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=output or subprocess.DEVNULL,
            stderr=subprocess.STDOUT if output else subprocess.DEVNULL,
            # A detached Windows process silently drops redirected standard
            # handles. CREATE_NO_WINDOW is enough for logged recovery helpers;
            # ordinary long-lived desktop children remain fully detached.
            creationflags=CREATE_NO_WINDOW if output else CREATE_NO_WINDOW | DETACHED_PROCESS,
            close_fds=True,
        )
    finally:
        if output:
            output.close()


def stop_python_script(script_name: str) -> None:
    if os.name != "nt":
        return
    escaped = script_name.replace("'", "''")
    command = (
        f"$needle = '{escaped}'; "
        "Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -match '^pythonw?\\.exe$' -and $_.CommandLine -like ('*' + $needle + '*') "
        "} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=str(BASE_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=12,
        creationflags=CREATE_NO_WINDOW,
    )


def script_running(script_name: str) -> bool:
    if os.name != "nt":
        return False
    escaped = script_name.replace("'", "''")
    command = (
        f"$needle = '{escaped}'; "
        "(Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -match '^pythonw?\\.exe$' -and $_.CommandLine -like ('*' + $needle + '*') "
        "} | Measure-Object).Count"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=12,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return (result.stdout or "").strip().isdigit() and int((result.stdout or "0").strip()) > 0


def local_bridge_healthy() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:5000/api/phone/status", timeout=3) as response:
            return response.status == 200
    except Exception:
        return False


def mirror_healthy() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:48738/mirror-health", timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def start_phone_bridge() -> None:
    spawn([
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(BASE_DIR / "start-phone-bridge.ps1"),
        "-PythonExe", find_pythonw(),
    ], output_path=BRIDGE_START_LOG)


def write_health(status: dict) -> None:
    payload = {"updated_at": int(time.time()), "watchdog_pid": os.getpid(), **status}
    temp = HEALTH_PATH.with_suffix(".json.tmp")
    try:
        temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp.replace(HEALTH_PATH)
    except OSError:
        pass


def _write_update_health(payload: dict) -> None:
    global _UPDATE_RESULT
    _UPDATE_RESULT = {**payload, "updated_at": int(time.time())}
    temp = UPDATE_HEALTH_PATH.with_suffix(".json.tmp")
    try:
        temp.write_text(json.dumps(_UPDATE_RESULT, indent=2), encoding="utf-8")
        temp.replace(UPDATE_HEALTH_PATH)
    except OSError:
        pass


def operator_is_running() -> bool:
    try:
        return bool(json.loads(OPERATOR_STATUS_PATH.read_text(encoding="utf-8")).get("running"))
    except (OSError, json.JSONDecodeError):
        return False


def auto_update_enabled(config: dict) -> bool:
    settings = config.get("auto_update") if isinstance(config.get("auto_update"), dict) else {}
    return settings.get("enabled", True) is not False


def _auto_update_worker(pythonw: str) -> None:
    if not _UPDATE_LOCK.acquire(blocking=False):
        return
    try:
        import aios_updater

        _write_update_health({"state": "checking", "message": "Checking GitHub main"})
        check = aios_updater.check_for_update()
        if not check.get("ok"):
            _write_update_health({"state": "error", "message": check.get("error") or "Update check failed"})
            return
        if not check.get("behind"):
            _write_update_health({
                "state": "current", "message": "Latest version installed",
                "current": check.get("current"), "latest": check.get("latest"),
            })
            return
        if operator_is_running():
            _write_update_health({
                "state": "waiting", "message": "Update waiting for OPERATOR to finish",
                "current": check.get("current"), "latest": check.get("latest"),
            })
            return
        result = aios_updater.perform_update(progress=lambda value: log(f"update: {value}"))
        if not result.get("ok"):
            _write_update_health({
                "state": "paused", "message": result.get("message") or "Update paused safely",
                "current": check.get("current"), "latest": check.get("latest"),
            })
            return
        _write_update_health({
            "state": "restarting", "message": "Updated; restarting aiOS services",
            "current": result.get("current") or check.get("latest"), "latest": check.get("latest"),
        })
        stop_python_script("helper_overlay.py")
        stop_python_script("aios_shell.py")
        stop_python_script("phone_relay.py")
        stop_python_script("agent_clicker/run.py")
        stop_python_script("aios_ui.mirror")
        if result.get("staged"):
            if not aios_updater.spawn_staged_apply(parent_pid=0):
                raise RuntimeError("could not launch staged update applier")
            return
        time.sleep(1.0)
        spawn([pythonw, str(BASE_DIR / "helper_overlay.py"), "--background", "--tk"])
        spawn([pythonw, str(BASE_DIR / "aios_shell.py")])
        spawn([pythonw, "-m", "aios_ui.mirror"])
        start_phone_bridge()
        _write_update_health({
            "state": "current", "message": "Update installed and services restarted",
            "current": result.get("current") or check.get("latest"), "latest": check.get("latest"),
        })
    except Exception as exc:
        _write_update_health({"state": "error", "message": f"Auto-update failed: {exc}"})
        log(f"auto-update failed: {exc!r}")
    finally:
        try:
            UPDATE_REQUEST_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        _UPDATE_LOCK.release()


def schedule_auto_update(pythonw: str) -> None:
    threading.Thread(
        target=_auto_update_worker,
        args=(pythonw,),
        name="aios-auto-update",
        daemon=True,
    ).start()


def fast_start_stack(pythonw: str) -> tuple[float, float, float, float]:
    """Launch every user-facing component immediately after a full restart."""
    now = time.time()
    spawn([pythonw, str(BASE_DIR / "helper_overlay.py"), "--background", "--tk"])
    spawn([pythonw, str(BASE_DIR / "aios_shell.py")])
    spawn([pythonw, "-m", "aios_ui.mirror"])
    ahk = find_autohotkey()
    if ahk:
        spawn([ahk, str(BASE_DIR / "autocorrect.ahk")])
    start_phone_bridge()
    if director_enabled():
        spawn([pythonw, str(BASE_DIR / "director_client.py")])
    log("fast-start launched helper, WebView2 shell, USB mirror, hotkeys, phone backend and Director client")
    return now + 55, now + 45, now + 35, now + 45


def run(*, fast_start: bool = False) -> None:
    pythonw = find_pythonw()
    if fast_start:
        (helper_grace_until, bridge_grace_until, ahk_grace_until,
         director_grace_until) = fast_start_stack(pythonw)
    else:
        helper_grace_until = 0.0
        bridge_grace_until = 0.0
        ahk_grace_until = 0.0
        director_grace_until = 0.0
    next_update_check = time.time() + 12
    log("watchdog started")
    while True:
        now = time.time()
        status = {
            "helper": "healthy",
            "hotkeys": "healthy",
            "phone": "not paired",
            "usb_mirror": "healthy",
            "director": "not paired",
        }

        updating = (BASE_DIR / ".aios_update_staging").exists()
        if not is_fresh(HELPER_HEARTBEAT, HELPER_TIMEOUT, now) and now >= helper_grace_until:
            if updating:
                status["helper"] = "update in progress"
            else:
                stop_python_script("helper_overlay.py")
                spawn([pythonw, str(BASE_DIR / "helper_overlay.py"), "--background", "--tk"])
                helper_grace_until = now + 55
                status["helper"] = "restarted"
                log("helper restarted")

        if not script_running("aios_shell.py") and not updating:
            spawn([pythonw, str(BASE_DIR / "aios_shell.py")])
            log("aios_shell started")

        if not mirror_healthy() and not updating:
            if not script_running("aios_ui.mirror"):
                spawn([pythonw, "-m", "aios_ui.mirror"])
                status["usb_mirror"] = "restarting"
                log("USB mirror started")
            else:
                status["usb_mirror"] = "starting"

        ahk = find_autohotkey()
        if not is_fresh(AHK_HEARTBEAT, AHK_TIMEOUT, now) and now >= ahk_grace_until:
            if ahk:
                spawn([ahk, str(BASE_DIR / "autocorrect.ahk")])
                ahk_grace_until = now + 35
                status["hotkeys"] = "restarted"
                log("AutoHotkey launcher restarted")
            else:
                status["hotkeys"] = "AutoHotkey missing"

        config = load_config()
        status["phone"], bridge_grace_until, bridge_started = ensure_phone_bridge(
            config, now, bridge_grace_until)
        if bridge_started:
            log("phone bridge recovery started")

        (status["director"], director_grace_until,
         director_started) = ensure_director_client(
            pythonw, now, director_grace_until)
        if director_started:
            log("Director Windows bridge restarted")

        requested = UPDATE_REQUEST_PATH.exists()
        if auto_update_enabled(config) and (requested or now >= next_update_check):
            schedule_auto_update(pythonw)
            next_update_check = now + AUTO_UPDATE_INTERVAL

        status["update"] = dict(_UPDATE_RESULT)
        write_health(status)
        time.sleep(CHECK_INTERVAL)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--fast-start", action="store_true")
    args, _unknown = parser.parse_known_args(argv)
    if os.name != "nt":
        print("aiOS watchdog is intended for Windows.")
        return 1
    if not claim_single_instance():
        return 0
    try:
        run(fast_start=args.fast_start)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        log(f"watchdog crashed: {exc!r}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
