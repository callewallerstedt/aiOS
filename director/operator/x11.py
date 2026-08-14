"""Hands and eyes on the operator display: screenshots via ffmpeg/scrot, input
via xdotool.

Everything here takes the display from settings, so the same code works on
:99 (the virtual screen) or on a real seat if that is ever wanted.
"""
from __future__ import annotations

import asyncio
import base64
import io
import os
import shutil
import time
from typing import Any

from .. import config
from . import display as display_mod

try:
    from PIL import Image
except ImportError:  # pragma: no cover - Pillow is installed by the deploy script
    Image = None

# Keys the model may name, mapped to X keysyms xdotool understands.
KEY_ALIASES = {
    "enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape",
    "tab": "Tab", "space": "space", "backspace": "BackSpace", "delete": "Delete",
    "del": "Delete", "insert": "Insert", "home": "Home", "end": "End",
    "pageup": "Page_Up", "pagedown": "Page_Down", "up": "Up", "down": "Down",
    "left": "Left", "right": "Right", "ctrl": "ctrl", "control": "ctrl",
    "alt": "alt", "shift": "shift", "win": "super", "super": "super",
    "cmd": "super", "meta": "super", "capslock": "Caps_Lock",
    "printscreen": "Print", "menu": "Menu", "plus": "plus", "minus": "minus",
}
for _n in range(1, 25):
    KEY_ALIASES[f"f{_n}"] = f"F{_n}"

BUTTONS = {"left": 1, "middle": 2, "right": 3}


def keysym(name: str) -> str:
    raw = str(name or "").strip()
    return KEY_ALIASES.get(raw.lower(), raw)


async def run(argv: list[str], settings: dict[str, Any] | None = None,
              timeout: float = 20.0) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, env=display_mod.display_env(settings),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except (FileNotFoundError, NotImplementedError, OSError) as exc:
        return 127, f"{argv[0]} unavailable: {exc}"
    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "timed out"
    return proc.returncode or 0, raw.decode("utf-8", errors="replace").strip()


async def xdotool(*args: str, settings: dict[str, Any] | None = None) -> tuple[int, str]:
    return await run(["xdotool", *[str(a) for a in args]], settings)


async def screen_size(settings: dict[str, Any] | None = None) -> tuple[int, int]:
    code, out = await xdotool("getdisplaygeometry", settings=settings)
    if code == 0 and out:
        parts = out.split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            return int(parts[0]), int(parts[1])
    cfg = display_mod.operator_settings(settings)
    return int(cfg.get("width") or 1600), int(cfg.get("height") or 900)


async def capture(settings: dict[str, Any] | None = None) -> bytes:
    """Grab the display as PNG bytes."""
    stamp = f"shot-{int(time.time() * 1000)}.png"
    path = config.shots_dir() / stamp
    if shutil.which("scrot"):
        code, out = await run(["scrot", "-o", "-z", str(path)], settings, timeout=25)
    elif shutil.which("ffmpeg"):
        width, height = await screen_size(settings)
        code, out = await run(
            ["ffmpeg", "-loglevel", "error", "-y", "-f", "x11grab", "-video_size",
             f"{width}x{height}", "-i", display_mod.display_name(settings),
             "-frames:v", "1", str(path)], settings, timeout=30)
    else:
        raise RuntimeError("neither scrot nor ffmpeg is installed on the box")
    if code != 0 or not path.is_file():
        raise RuntimeError(f"screen capture failed: {out or 'no output file'}")
    data = path.read_bytes()
    try:
        os.unlink(path)
    except OSError:
        pass
    return data


def encode_jpeg(png_bytes: bytes, *, max_width: int = 1400, quality: int = 68) -> tuple[str, int, int]:
    """PNG -> data URL. Returns (data_url, width, height) of what the model sees.

    The model is told these dimensions and answers in the same pixel space, so
    the caller must scale coordinates back up when the image was shrunk.
    """
    if Image is None:
        encoded = base64.b64encode(png_bytes).decode("ascii")
        return f"data:image/png;base64,{encoded}", 0, 0
    with Image.open(io.BytesIO(png_bytes)) as img:
        img = img.convert("RGB")
        if img.width > max_width:
            ratio = max_width / float(img.width)
            img = img.resize((max_width, max(1, int(img.height * ratio))), Image.LANCZOS)
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=quality, optimize=True)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}", img.width, img.height


async def type_text(text: str, settings: dict[str, Any] | None = None) -> None:
    """Paste text without translating punctuation through the keyboard layout.

    ``xdotool type`` turns ``:`` into ``ö`` on Calle's Swedish X11 layout.
    Sending UTF-8 through the X clipboard keeps URLs and Unicode exact.
    """
    if not text:
        return
    if shutil.which("xclip"):
        proc = await asyncio.create_subprocess_exec(
            "xclip", "-selection", "clipboard", "-in", "-quiet",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=display_mod.display_env(settings))
        try:
            assert proc.stdin is not None
            proc.stdin.write(text.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            # xclip owns the selection until another X client requests it, so
            # Paste must happen before waiting for the process to exit.
            await asyncio.sleep(0.05)
            await hotkey(["ctrl", "v"], settings)
            await asyncio.sleep(0.05)
            return
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
    await xdotool("type", "--clearmodifiers", "--delay", "12", text, settings=settings)


async def press(key: str, presses: int = 1, settings: dict[str, Any] | None = None) -> None:
    sym = keysym(key)
    for _ in range(max(1, int(presses or 1))):
        await xdotool("key", "--clearmodifiers", sym, settings=settings)


async def hotkey(keys: list[str], settings: dict[str, Any] | None = None) -> None:
    combo = "+".join(keysym(k) for k in keys if str(k).strip())
    if combo:
        await xdotool("key", "--clearmodifiers", combo, settings=settings)


async def move(x: int, y: int, settings: dict[str, Any] | None = None) -> None:
    await xdotool("mousemove", int(x), int(y), settings=settings)


async def pointer_window(settings: dict[str, Any] | None = None) -> str:
    """Return the X11 window currently under the pointer, when available."""
    code, out = await xdotool("getmouselocation", "--shell", settings=settings)
    if code != 0:
        return ""
    for line in out.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "WINDOW" and value.strip().isdigit():
            return value.strip()
    return ""


async def _checked_xdotool(*args: str, settings: dict[str, Any] | None = None) -> None:
    code, out = await xdotool(*args, settings=settings)
    if code != 0:
        raise RuntimeError(f"xdotool {' '.join(args)} failed ({code}): {out or 'no output'}")


async def click(x: int | None, y: int | None, button: str = "left", clicks: int = 1,
                settings: dict[str, Any] | None = None) -> None:
    if x is not None and y is not None:
        await move(x, y, settings)
    button_code = str(BUTTONS.get(str(button or "left").lower(), 1))
    window = await pointer_window(settings)
    args = ["click"]
    # On the real GNOME desktop, XTEST can move the pointer correctly while a
    # bare click never reaches Chrome. Addressing the window under the pointer
    # delivers the same event reliably and still works for whichever app is at
    # the requested screen coordinate.
    if window:
        args += ["--window", window]
    args += ["--repeat", str(max(1, int(clicks or 1))), "--delay", "80", button_code]
    await _checked_xdotool(*args, settings=settings)


async def mouse_down(x: int | None, y: int | None, button: str = "left",
                     settings: dict[str, Any] | None = None) -> None:
    if x is not None and y is not None:
        await move(x, y, settings)
    await xdotool("mousedown", BUTTONS.get(str(button or "left").lower(), 1), settings=settings)


async def mouse_up(x: int | None, y: int | None, button: str = "left",
                   settings: dict[str, Any] | None = None) -> None:
    if x is not None and y is not None:
        await move(x, y, settings)
    await xdotool("mouseup", BUTTONS.get(str(button or "left").lower(), 1), settings=settings)


async def drag(start: tuple[int, int], end: tuple[int, int], button: str = "left",
               steps: int = 18, settings: dict[str, Any] | None = None) -> None:
    await mouse_down(start[0], start[1], button, settings)
    x0, y0 = start
    x1, y1 = end
    for i in range(1, max(2, int(steps)) + 1):
        ratio = i / float(steps)
        await move(int(x0 + (x1 - x0) * ratio), int(y0 + (y1 - y0) * ratio), settings)
        await asyncio.sleep(0.012)
    await mouse_up(x1, y1, button, settings)


async def stroke(points: list[list[int]], button: str = "left", step_delay: float = 0.02,
                 settings: dict[str, Any] | None = None) -> None:
    """One continuous press-glide-release through every point."""
    if len(points) < 2:
        return
    await mouse_down(int(points[0][0]), int(points[0][1]), button, settings)
    for point in points[1:]:
        await move(int(point[0]), int(point[1]), settings)
        await asyncio.sleep(max(0.0, float(step_delay)))
    last = points[-1]
    await mouse_up(int(last[0]), int(last[1]), button, settings)


async def scroll(x: int | None, y: int | None, dy: int = -3,
                 settings: dict[str, Any] | None = None) -> None:
    if x is not None and y is not None:
        await move(x, y, settings)
    button = 5 if int(dy) < 0 else 4          # 4 = wheel up, 5 = wheel down
    await xdotool("click", "--repeat", min(abs(int(dy)) or 1, 25), "--delay", "40",
                  button, settings=settings)


async def key_down(key: str, settings: dict[str, Any] | None = None) -> None:
    await xdotool("keydown", keysym(key), settings=settings)


async def key_up(key: str = "", settings: dict[str, Any] | None = None) -> None:
    if key:
        await xdotool("keyup", keysym(key), settings=settings)
    else:
        await xdotool("keyup", "--clearmodifiers", "shift", settings=settings)


async def active_window_title(settings: dict[str, Any] | None = None) -> str:
    code, out = await xdotool("getactivewindow", "getwindowname", settings=settings)
    return out.strip() if code == 0 else ""


async def window_list(limit: int = 25, settings: dict[str, Any] | None = None) -> list[str]:
    """Titles of the visible windows.

    Uses xdotool rather than wmctrl: wmctrl reads _NET_CLIENT_LIST, which only
    a window manager publishes, so it returns nothing on a bare display and
    made the operator believe the screen was empty.
    """
    code, out = await xdotool("search", "--onlyvisible", "--name", ".", settings=settings)
    if code != 0 or not out.strip():
        return []
    titles: list[str] = []
    for window_id in out.split()[:limit * 2]:
        if len(titles) >= limit:
            break
        named, title = await xdotool("getwindowname", window_id, settings=settings)
        title = title.strip()
        if named == 0 and title and title not in titles:
            titles.append(title)
    return titles
