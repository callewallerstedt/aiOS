"""WebView2 desktop shell for the aiOS Director PWA.

Edge ``--app=`` shortcuts group under the browser in the taskbar and pin as
Edge, not as Director. This wrapper gives Director its own AppUserModelID,
window icon, and Start Menu / desktop shortcuts you can pin like any other app.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_URL = "https://phonesite-six.vercel.app"
APP_USER_MODEL_ID = "aiOS.Director.Desktop"
_MUTEX_NAME = "Local\\aiOS.Director.Desktop.Singleton"
_PROFILE_ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "aiOS"
WINDOW_TITLE = "aiOS Director"
LAUNCHER_EXE = ROOT / "assets" / "aios-director.exe"
PATCH_ICON_SCRIPT = ROOT / "phone_site" / "scripts" / "patch-exe-icon.cjs"
CREATE_NO_WINDOW = 0x08000000
ICON_CANDIDATES = (
    ROOT / "phone_site" / "icons" / "aios-icon.ico",
    ROOT / "assets" / "aios-logo.ico",
)
_MUTEX_HANDLE = None


def icon_path() -> Path | None:
    for candidate in ICON_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def default_url() -> str:
    return DEFAULT_URL


def set_windows_app_id() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except (AttributeError, OSError):
        pass


def _hide_console() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.user32.ShowWindow(
            ctypes.windll.kernel32.GetConsoleWindow(), 0  # SW_HIDE
        )
    except Exception:
        pass


def _claim_single_instance() -> bool:
    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    try:
        handle = ctypes.windll.kernel32.CreateMutexW(None, True, _MUTEX_NAME)
        if not handle:
            return True
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            ctypes.windll.kernel32.CloseHandle(handle)
            return False
        _MUTEX_HANDLE = handle
        return True
    except Exception:
        return True


def _profile_in_use(profile: Path) -> bool:
    """Return True when WebView2's lockfile is held by another live process."""
    lock = profile / "EBWebView" / "lockfile"
    if not lock.exists() or os.name != "nt":
        return False
    GENERIC_READ = 0x80000000
    OPEN_EXISTING = 3
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    handle = create_file(str(lock), GENERIC_READ, 0, None, OPEN_EXISTING, 0, None)
    if handle == INVALID_HANDLE_VALUE:
        return True
    close_handle = ctypes.windll.kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    close_handle(handle)
    return False


def _profile_dir() -> Path:
    """Pick a WebView2 profile we can actually open."""
    primary = _PROFILE_ROOT / "director-webview2"
    fallback = _PROFILE_ROOT / "director-webview2-recover"
    for candidate in (primary, fallback):
        candidate.mkdir(parents=True, exist_ok=True)
        if not _profile_in_use(candidate):
            return candidate
    emergency = _PROFILE_ROOT / f"director-webview2-recover-{os.getpid()}"
    emergency.mkdir(parents=True, exist_ok=True)
    return emergency


def _set_window_icon(window) -> None:
    """Backup icon path for backends that ignore ``webview.start(icon=...)``."""
    path = icon_path()
    if os.name != "nt" or not path or not getattr(window, "native", None):
        return
    try:
        handle = int(window.native.Handle)
        load_image = ctypes.windll.user32.LoadImageW
        load_image.argtypes = [
            ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
            ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        ]
        load_image.restype = ctypes.c_void_p
        image_icon = 1
        load_from_file = 0x0010 | 0x0040  # LR_LOADFROMFILE | LR_DEFAULTSIZE
        for icon_kind in (1, 0):  # ICON_BIG, ICON_SMALL
            icon = load_image(None, str(path), image_icon, 0, 0, load_from_file)
            if icon:
                ctypes.windll.user32.SendMessageW(handle, 0x0080, icon_kind, icon)
    except Exception:
        pass


def _find_pythonw() -> Path:
    candidates = [
        ROOT / ".venv" / "Scripts" / "pythonw.exe",
        Path(sys.executable).with_name("pythonw.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return Path(sys.executable)


def _patch_exe_icon(exe: Path, icon: Path) -> None:
    node = shutil.which("node")
    if not node or not PATCH_ICON_SCRIPT.is_file():
        return
    rcedit_module = ROOT / "phone_site" / "node_modules" / "rcedit"
    if not rcedit_module.is_dir():
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        if not npm:
            return
        subprocess.run(
            [npm, "install", "--no-save", "rcedit@5.0.2"],
            cwd=str(ROOT / "phone_site"),
            capture_output=True,
            timeout=120,
            creationflags=CREATE_NO_WINDOW,
        )
    subprocess.run(
        [node, str(PATCH_ICON_SCRIPT), str(exe), str(icon)],
        cwd=str(ROOT / "phone_site"),
        capture_output=True,
        timeout=30,
        creationflags=CREATE_NO_WINDOW,
        check=False,
    )


def _launcher_pth_contents() -> str:
    """Paths the branded pythonw copy needs, including user site-packages."""
    import site

    prefix = Path(sys.base_prefix)
    entries = [
        str(prefix),
        str(prefix / "DLLs"),
        str(prefix / "Lib"),
        str(prefix / "Lib" / "site-packages"),
    ]
    try:
        entries.extend(site.getsitepackages())
    except Exception:
        pass
    try:
        entries.append(site.getusersitepackages())
    except Exception:
        pass
    entries.append(str(ROOT))
    entries.append(".")
    seen: set[str] = set()
    lines: list[str] = []
    for item in entries:
        if not item or item in seen:
            continue
        seen.add(item)
        lines.append(item)
    lines.append("import site")
    return "\n".join(lines) + "\n"


def ensure_launcher_exe() -> Path:
    """Build a pythonw copy whose PE icon is aiOS, so pinning is not Python."""
    pythonw = _find_pythonw()
    icon = icon_path()
    LAUNCHER_EXE.parent.mkdir(parents=True, exist_ok=True)
    source_mtime = max(pythonw.stat().st_mtime, *(c.stat().st_mtime for c in ICON_CANDIDATES if c.is_file()))
    if not LAUNCHER_EXE.exists() or LAUNCHER_EXE.stat().st_mtime < source_mtime:
        shutil.copy2(pythonw, LAUNCHER_EXE)
        if icon:
            _patch_exe_icon(LAUNCHER_EXE, icon)
    pth = LAUNCHER_EXE.with_name(LAUNCHER_EXE.stem + "._pth")
    pth.write_text(_launcher_pth_contents(), encoding="utf-8")
    return LAUNCHER_EXE


def launcher_command() -> tuple[Path, str]:
    """Return the executable Windows should pin plus its arguments."""
    if os.name == "nt":
        exe = ensure_launcher_exe()
        script = ROOT / "director_shell.py"
        return exe, f'"{script}"'
    pythonw = _find_pythonw()
    script = ROOT / "director_shell.py"
    return pythonw, f'"{script}"'


def install_shortcuts() -> list[str]:
    """Write Desktop + Start Menu shortcuts that launch this shell, not Edge."""
    if os.name != "nt":
        return []
    created: list[str] = []
    target, arguments = launcher_command()
    icon = ensure_launcher_exe() if os.name == "nt" else icon_path()
    icon_line = f'$s.IconLocation = "{icon},0"\n' if icon else ""
    targets = (
        Path(os.environ.get("USERPROFILE", "")) / "Desktop",
        Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    )
    taskbar = (
        Path(os.environ.get("APPDATA", ""))
        / "Microsoft" / "Internet Explorer" / "Quick Launch" / "User Pinned" / "TaskBar"
    )
    for folder in targets:
        if not folder.is_dir():
            continue
        shortcut = folder / "aiOS Director.lnk"
        ps_script = f"""
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut('{shortcut}')
$s.TargetPath = '{target}'
$s.Arguments = '{arguments}'
$s.WorkingDirectory = '{ROOT}'
        $s.Description = 'aiOS Director'
$s.WindowStyle = 1
{icon_line}$s.Save()
"""
        try:
            import subprocess

            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True,
                timeout=15,
                creationflags=CREATE_NO_WINDOW,
            )
            if shortcut.exists():
                created.append(str(shortcut))
        except Exception:
            continue
    if taskbar.is_dir():
        _repair_taskbar_pins(taskbar, target, arguments, icon if isinstance(icon, Path) else None)
    return created


def _repair_taskbar_pins(folder: Path, target: Path, arguments: str, icon: Path | None) -> None:
    """Taskbar pins often keep the Python name and drop arguments."""
    try:
        raw = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"$ws = New-Object -ComObject WScript.Shell; "
             f"Get-ChildItem -LiteralPath '{folder}' -Filter *.lnk | ForEach-Object {{ "
             f"$s = $ws.CreateShortcut($_.FullName); "
             f"[pscustomobject]@{{ Path=$_.FullName; Target=$s.TargetPath; Args=$s.Arguments }} "
             f"}} | ConvertTo-Json -Compress"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
        ).stdout.strip() or "[]"
        rows = json.loads(raw)
        if isinstance(rows, dict):
            rows = [rows]
        wanted = str(target).lower()
        for row in rows:
            path = str(row.get("Path") or "")
            current = str(row.get("Target") or "").lower()
            if wanted not in current and "aios-director.exe" not in current:
                continue
            name = Path(path).name.lower()
            if name in {"python.lnk", "aios director.lnk", "aios-director.lnk"}:
                icon_line = f'$s.IconLocation = "{icon},0"\n' if icon else ""
                ps_script = f"""
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut('{path}')
$s.TargetPath = '{target}'
$s.Arguments = '{arguments}'
$s.WorkingDirectory = '{ROOT}'
$s.Description = 'aiOS Director'
$s.WindowStyle = 1
{icon_line}$s.Save()
"""
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_script],
                    capture_output=True,
                    timeout=15,
                    creationflags=CREATE_NO_WINDOW,
                )
    except Exception:
        pass


def _find_director_hwnd() -> int:
    if os.name != "nt":
        return 0
    try:
        return int(ctypes.windll.user32.FindWindowW(None, WINDOW_TITLE))
    except Exception:
        return 0


def _window_is_responding(hwnd: int) -> bool:
    if os.name != "nt" or not hwnd:
        return False
    try:
        return not bool(ctypes.windll.user32.IsHungAppWindow(hwnd))
    except Exception:
        return True


def _director_process_ids() -> list[int]:
    if os.name != "nt":
        return []
    ids: list[int] = []
    try:
        for proc in subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='aios-director.exe'\" | "
             "Select-Object -ExpandProperty ProcessId"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        ).stdout.splitlines():
            proc_id = proc.strip()
            if proc_id.isdigit():
                ids.append(int(proc_id))
    except Exception:
        pass
    return ids


def _kill_stale_director() -> None:
    """Stop a hung Director shell and orphaned WebView2 children."""
    if os.name != "nt":
        return
    for proc_id in _director_process_ids():
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc_id), "/T", "/F"],
                capture_output=True,
                timeout=10,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception:
            pass
    profile_hint = str(_PROFILE_ROOT / "director-webview2").lower()
    try:
        raw = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='msedgewebview2.exe'\" | "
             "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
        ).stdout.strip() or "[]"
        rows = json.loads(raw)
        if isinstance(rows, dict):
            rows = [rows]
        for row in rows:
            command = str(row.get("CommandLine") or "").lower()
            if profile_hint in command:
                proc_id = row.get("ProcessId")
                if proc_id:
                    subprocess.run(
                        ["taskkill", "/PID", str(proc_id), "/T", "/F"],
                        capture_output=True,
                        timeout=10,
                        creationflags=CREATE_NO_WINDOW,
                    )
    except Exception:
        pass


def _focus_existing_window() -> bool:
    if os.name != "nt":
        return False
    try:
        hwnd = _find_director_hwnd()
        if not hwnd or not _window_is_responding(hwnd):
            return False
        user32 = ctypes.windll.user32
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def _director_is_running() -> bool:
    hwnd = _find_director_hwnd()
    return bool(hwnd and _window_is_responding(hwnd))


def run(*, url: str | None = None, debug: bool = False) -> None:
    if not _claim_single_instance():
        if _focus_existing_window():
            return
        _kill_stale_director()
        time.sleep(0.6)
        if _focus_existing_window():
            return
        _claim_single_instance()

    set_windows_app_id()
    os.environ.setdefault("WEBVIEW2_USER_DATA_FOLDER", str(_profile_dir()))

    import webview  # noqa: E402 - must follow WEBVIEW2_USER_DATA_FOLDER

    start_url = (url or default_url()).strip() or DEFAULT_URL
    icon = icon_path()
    window = webview.create_window(
        WINDOW_TITLE,
        start_url,
        width=1280,
        height=860,
        min_size=(860, 640),
        background_color="#1c1d1f",
    )
    window.events.shown += lambda: _set_window_icon(window)
    webview.start(
        gui="edgechromium",
        debug=debug,
        private_mode=False,
        icon=str(icon) if icon else None,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="director-shell", description="aiOS Director desktop app")
    parser.add_argument("--url", help=f"PWA URL (default: {DEFAULT_URL})")
    parser.add_argument("--debug", action="store_true", help="open devtools")
    parser.add_argument(
        "--install-shortcuts",
        action="store_true",
        help="refresh Desktop and Start Menu shortcuts, then exit",
    )
    args = parser.parse_args(argv)
    if args.install_shortcuts:
        paths = install_shortcuts()
        if paths:
            print("Installed shortcuts:")
            for path in paths:
                print(f"  {path}")
        else:
            print("No shortcuts were written.", file=sys.stderr)
            return 1
        return 0
    run(url=args.url, debug=args.debug)
    return 0


def spawn_director() -> None:
    """Start Director detached, or bring the live window forward."""
    if _director_is_running():
        _focus_existing_window()
        return
    hwnd = _find_director_hwnd()
    if hwnd and not _window_is_responding(hwnd):
        _kill_stale_director()
    target, arguments = launcher_command()
    subprocess.Popen(
        [str(target), arguments.strip('"')],
        cwd=str(ROOT),
        creationflags=CREATE_NO_WINDOW,
    )


if __name__ == "__main__":
    _hide_console()
    raise SystemExit(main())
