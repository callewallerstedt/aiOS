"""Detached coordinator for a fast, complete aiOS restart.

Every restart entry point hands off here. The coordinator waits for its caller
to exit, stops the old managed process trees in one pass, then launches the
watchdog in fast-start mode. This avoids WebView2 profile-lock races while
reloading the GUI, HTTP backends, helper services, voice and hotkeys.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SHELL_PATH = BASE_DIR / "aios_shell.py"
WATCHDOG_PATH = BASE_DIR / "aios_watchdog.py"
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259

MANAGED_COMMAND_PATTERNS = (
    r"(?:^|[\\/\s\"])(?:aios_watchdog|helper_overlay|aios_shell|voice_dictation|phone_relay)\.py(?:[\"\s]|$)",
    r"agent_clicker[\\/]run\.py(?:[\"\s]|$)",
    r"(?:^|[\s\"])-m\s+aios_ui\.mirror(?:[\"\s]|$)",
    r"autocorrect\.ahk(?:[\"\s]|$)",
)


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            if not ok:
                return False
            return int(exit_code.value) == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def wait_for_exit(pid: int, timeout: float = 30.0) -> bool:
    deadline = time.perf_counter() + max(0.5, float(timeout))
    while time.perf_counter() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(0.04)
    return not process_alive(pid)


def find_pythonw() -> str:
    candidates = [
        BASE_DIR / ".venv" / "Scripts" / "pythonw.exe",
        Path(sys.executable).with_name("pythonw.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return shutil.which("pythonw.exe") or sys.executable


def managed_process_pids(*, exclude: set[int] | None = None) -> list[int]:
    """Find only aiOS-owned runtime processes, never broad Python matches."""
    if os.name != "nt":
        return []
    excluded = {int(value) for value in (exclude or set()) if int(value) > 0}
    pattern = "|".join(MANAGED_COMMAND_PATTERNS).replace("'", "''")
    skip = ",".join(str(pid) for pid in sorted(excluded)) or "0"
    command = (
        f"$pattern = '{pattern}'; $skip = @({skip}); "
        "Get-CimInstance Win32_Process | Where-Object { "
        "$_.ProcessId -notin $skip -and "
        "$_.Name -match '^(pythonw?\\.exe|AutoHotkey.*\\.exe)$' -and "
        "$_.CommandLine -match $pattern "
        "} | Select-Object -ExpandProperty ProcessId"
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
        return []
    return sorted({int(line.strip()) for line in result.stdout.splitlines() if line.strip().isdigit()})


def stop_managed_processes(*, exclude: set[int] | None = None) -> list[int]:
    """Stop all managed trees with one taskkill invocation."""
    pids = managed_process_pids(exclude=exclude)
    if not pids:
        return []
    command = ["taskkill", "/T", "/F"]
    for pid in pids:
        command.extend(["/PID", str(pid)])
    try:
        subprocess.run(
            command,
            cwd=str(BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    return pids


def clear_stale_heartbeats() -> None:
    for name in (".aios-helper-heartbeat", ".aios-ahk-heartbeat", ".aios-phone-relay-heartbeat"):
        try:
            (BASE_DIR / name).unlink(missing_ok=True)
        except OSError:
            pass


def launch_stack(_extra_args: list[str] | None = None) -> None:
    """Launch the single watchdog owner; --fast-start spawns the stack now."""
    target = WATCHDOG_PATH if WATCHDOG_PATH.exists() else SHELL_PATH
    args = [find_pythonw(), str(target)]
    if target == WATCHDOG_PATH:
        args.append("--fast-start")
    kwargs: dict = {"cwd": str(BASE_DIR), "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        kwargs["stdin"] = subprocess.DEVNULL
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    subprocess.Popen(args, **kwargs)


def launch_helper(extra_args: list[str] | None = None) -> None:
    """Compatibility name retained for the updater; now launches all aiOS."""
    launch_stack(extra_args)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 1:
        return 2
    try:
        parent_pid = int(argv[0])
    except ValueError:
        return 2
    extra_args = argv[1:]
    wait_for_exit(parent_pid, timeout=30.0)
    stop_managed_processes(exclude={os.getpid()})
    clear_stale_heartbeats()
    # taskkill /T already waits for teardown; only a tiny mutex settle remains.
    time.sleep(0.06)
    if not SHELL_PATH.exists():
        return 1
    launch_helper(extra_args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
