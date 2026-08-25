"""aiOS Markdown reader — frameless WebView2 viewer for .md files."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import locale
import os
import subprocess
import sys
import webbrowser
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import pathname2url

# Isolate this viewer's WebView2 profile from the main aiOS shell, and from
# other reader windows opened at the same time via file association.
_PROFILE_ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "aiOS"
_PROFILE = _PROFILE_ROOT / f"md-reader-webview2-{os.getpid()}"
_PROFILE.mkdir(parents=True, exist_ok=True)
os.environ["WEBVIEW2_USER_DATA_FOLDER"] = str(_PROFILE)

import webview  # noqa: E402 - must follow the env var above

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "md_reader_web"
INDEX = WEB_DIR / "index.html"


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", locale.getpreferredencoding(False) or "utf-8", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _theme() -> dict[str, str]:
    defaults = {
        "accent": "#61dafb",
        "panel": "#101722",
        "panel2": "#151f2d",
        "surface": "#0b111b",
        "surface2": "#111a27",
        "text": "#f4f7fb",
        "muted": "#8b98aa",
        "danger": "#ff5f57",
    }
    try:
        from helper_overlay import load_config

        theme = load_config().get("theme") or {}
        for key in defaults:
            value = str(theme.get(key) or "")
            if value.startswith("#") and len(value) == 7:
                defaults[key] = value
    except Exception:
        pass
    return defaults


class ReaderApi:
    def __init__(self, initial_path: Path | None) -> None:
        self._window: webview.Window | None = None
        self._maximized = False
        self.path = initial_path
        self.content = ""
        if self.path and self.path.is_file():
            try:
                self.content = _read_text(self.path)
            except OSError:
                self.content = f"# Could not open file\n\n`{self.path}`"

    def attach(self, window: webview.Window) -> None:
        self._window = window

    def style_window(self) -> None:
        if os.name != "nt" or not self._window:
            return
        try:
            handle = int(self._window.native.Handle)
            preference = ctypes.c_int(2)  # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.wintypes.HWND(handle), 33, ctypes.byref(preference), ctypes.sizeof(preference)
            )
        except Exception:
            pass

    def _payload(self, *, ok: bool = True, error: str = "") -> dict:
        path = self.path if self.path and self.path.exists() else None
        return {
            "ok": ok,
            "error": error,
            "path": str(path) if path else "",
            "name": path.name if path else "Markdown",
            "content": self.content,
            "theme": _theme(),
        }

    def get_document(self) -> dict:
        return self._payload()

    def open_dialog(self) -> dict:
        if not self._window:
            return self._payload(ok=False, error="Window not ready")
        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=(
                "Markdown (*.md;*.markdown;*.mdown;*.mkd)",
                "Text (*.txt)",
                "All files (*.*)",
            ),
        )
        if not result:
            return {"ok": False, "cancelled": True}
        return self.open_path(str(result[0]))

    def open_path(self, target: str) -> dict:
        path = Path(str(target or "")).expanduser()
        if not path.is_file():
            return self._payload(ok=False, error="File not found")
        try:
            self.content = _read_text(path)
        except OSError as exc:
            return self._payload(ok=False, error=str(exc))
        self.path = path
        if self._window:
            try:
                self._window.set_title(f"{path.name} — aiOS")
            except Exception:
                pass
        return self._payload()

    def reload(self) -> dict:
        if not self.path:
            return self._payload(ok=False, error="No file open")
        return self.open_path(str(self.path))

    def reveal(self) -> bool:
        if not self.path or not self.path.exists():
            return False
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer", f"/select,{self.path}"])  # noqa: S603
            else:
                webbrowser.open(self.path.parent.as_uri())
            return True
        except Exception:
            return False

    def open_link(self, target: str) -> bool:
        value = str(target or "").strip()
        if not value:
            return False
        if value.startswith("#"):
            return True
        parsed = urlparse(value)
        if parsed.scheme in {"http", "https", "mailto"}:
            webbrowser.open(value)
            return True
        if parsed.scheme == "file":
            candidate = Path(unquote(parsed.path))
            if os.name == "nt" and str(candidate).startswith("/"):
                candidate = Path(str(candidate)[1:])
        elif parsed.scheme == "":
            rel = value.split("#", 1)[0].split("?", 1)[0]
            if not rel:
                return True
            base = self.path.parent if self.path else Path.cwd()
            candidate = (base / rel).resolve()
        else:
            return False
        if candidate.suffix.lower() in {".md", ".markdown", ".mdown", ".mkd", ".txt"} and candidate.is_file():
            payload = self.open_path(str(candidate))
            if payload.get("ok") and self._window:
                self._window.evaluate_js(
                    f"window.__mdReaderLoad && window.__mdReaderLoad({json.dumps(payload)})"
                )
            return bool(payload.get("ok"))
        if candidate.exists():
            try:
                os.startfile(str(candidate))  # noqa: S606
                return True
            except Exception:
                return False
        return False

    def minimize(self) -> None:
        if self._window:
            self._window.minimize()

    def toggle_maximize(self) -> None:
        if not self._window:
            return
        if self._maximized:
            self._window.restore()
            self._maximized = False
        else:
            self._window.maximize()
            self._maximized = True

    def close(self) -> None:
        if self._window:
            self._window.destroy()

    def resize_window(self, width: float, height: float) -> None:
        if self._window:
            self._window.resize(max(640, int(width)), max(420, int(height)))


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def main() -> None:
    enable_dpi_awareness()
    if not INDEX.is_file():
        raise SystemExit(f"Reader UI missing: {INDEX}")

    initial: Path | None = None
    if len(sys.argv) > 1:
        candidate = Path(sys.argv[1]).expanduser()
        if candidate.is_file():
            initial = candidate

    api = ReaderApi(initial)
    url = INDEX.resolve().as_uri() if hasattr(INDEX, "as_uri") else f"file:{pathname2url(str(INDEX.resolve()))}"
    title = f"{initial.name} — aiOS" if initial else "aiOS Markdown"

    window = webview.create_window(
        title,
        url,
        js_api=api,
        width=980,
        height=780,
        min_size=(640, 420),
        frameless=True,
        easy_drag=False,
        transparent=False,
        background_color=_theme()["panel"],
        text_select=True,
    )
    api.attach(window)
    window.events.shown += api.style_window
    webview.start(gui="edgechromium", debug=False, private_mode=False)


if __name__ == "__main__":
    main()
