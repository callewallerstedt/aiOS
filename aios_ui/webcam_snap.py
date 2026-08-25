"""Webcam snap helpers for the Quick Tools tray.

Camera capture runs in Python via OpenCV/DirectShow so the WebView never
calls getUserMedia (no permission prompts, no stuck “Starting camera…”).
Frames are flipped 180° for the upside-down desk mount.

Device discovery uses ffmpeg's DirectShow list so we can prefer the Lenovo
RGB camera and skip Rift / Meta Quest virtual devices that steal index 0.
"""

from __future__ import annotations

import base64
import re
import struct
import subprocess
import threading
import time
from io import BytesIO
from pathlib import Path
from typing import Any

_DATA_URL_RE = re.compile(
    r"^data:image/(png|jpeg|jpg|webp);base64,(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_DSHOW_VIDEO_RE = re.compile(r'"([^"]+)"\s*\(video\)', re.IGNORECASE)

# Desk cam sits inverted — every preview/snap is rotated before use.
FLIP_UPSIDE_DOWN = True
# Keep the device open between Quick Tools uses so the next snap is a frame grab.
IDLE_COOL_SEC = 120.0

_SKIP_NAME_PARTS = (
    "meta quest",
    "rift",
    "obs virtual",
    "virtual camera",
    "ir camera",
    "infrared",
)


def decode_image_data_url(data_url: str):
    """Return a Pillow RGB image from a canvas data URL."""
    from PIL import Image

    raw = str(data_url or "").strip()
    match = _DATA_URL_RE.match(raw)
    if not match:
        raise ValueError("Expected a PNG/JPEG/WebP data URL.")
    payload = base64.b64decode(match.group(2), validate=False)
    image = Image.open(BytesIO(payload))
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
    if image.mode == "RGBA":
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.split()[3])
        return background
    return image.convert("RGB")


def _orient(image):
    """Flip upside-down desk-cam frames so the UI/clipboard look right-side up."""
    from PIL import Image

    if not FLIP_UPSIDE_DOWN:
        return image
    return image.transpose(Image.Transpose.ROTATE_180)


def _ffmpeg_exe() -> str:
    try:
        from .screen_recording import ffmpeg_path

        path = ffmpeg_path()
        if path:
            return path
    except Exception:
        pass
    import shutil

    which = shutil.which("ffmpeg")
    if which:
        return which
    winget = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    if winget.exists():
        matches = sorted(winget.glob("**/ffmpeg.exe"))
        if matches:
            return str(matches[0])
    return ""


def list_dshow_video_names() -> list[str]:
    """Return DirectShow video device names in index order (ffmpeg)."""
    exe = _ffmpeg_exe()
    if not exe:
        return []
    try:
        proc = subprocess.run(
            [exe, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    names: list[str] = []
    for line in (proc.stderr or "").splitlines():
        if "Alternative name" in line:
            continue
        match = _DSHOW_VIDEO_RE.search(line)
        if match:
            names.append(match.group(1).strip())
    return names


def preferred_device_index(names: list[str] | None = None) -> int:
    """Pick Lenovo RGB when present; otherwise the first non-virtual camera."""
    devices = list(names if names is not None else list_dshow_video_names())
    if not devices:
        return 0

    def rank(name: str) -> tuple[int, int]:
        low = name.casefold()
        if "lenovo" in low and "rgb" in low:
            return (0, 0)
        if "lenovo" in low and "ir" not in low:
            return (1, 0)
        if any(part in low for part in _SKIP_NAME_PARTS):
            return (9, 0)
        return (2, 0)

    best_i = 0
    best_rank = (99, 0)
    for index, name in enumerate(devices):
        current = rank(name)
        if current < best_rank:
            best_rank = current
            best_i = index
    if best_rank[0] >= 9:
        return 0
    return best_i


def _ffmpeg_still(device_name: str):
    """Grab one full-res JPEG from a named DirectShow camera via ffmpeg."""
    from PIL import Image

    exe = _ffmpeg_exe()
    if not exe:
        raise OSError("ffmpeg not found")
    name = str(device_name or "").strip()
    if not name:
        raise ValueError("No camera name")
    out = Path(__file__).resolve().parent.parent / ".tmp" / "webcam_still.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            exe,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "dshow",
            "-i",
            f"video={name}",
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    if proc.returncode != 0 or not out.exists() or out.stat().st_size < 32:
        detail = (proc.stderr or proc.stdout or "ffmpeg still failed").strip()
        raise OSError(detail[:240] or "ffmpeg still failed")
    return Image.open(out).convert("RGB")


def bgr_frame_to_dib(frame_bgr) -> bytes:
    """Pack an OpenCV BGR frame into a CF_DIB payload (fast, no Pillow/PNG)."""
    import numpy as np

    if frame_bgr is None or getattr(frame_bgr, "size", 0) == 0:
        raise ValueError("Empty camera frame")
    # Bottom-up BGR rows, 4-byte aligned — what CF_DIB expects.
    flipped = np.ascontiguousarray(frame_bgr[::-1])
    height, width = flipped.shape[:2]
    row_bytes = (width * 3 + 3) & ~3
    if row_bytes == width * 3:
        pixels = flipped.tobytes()
    else:
        padded = np.zeros((height, row_bytes), dtype=np.uint8)
        packed = flipped.reshape(height, width * 3)
        padded[:, : width * 3] = packed
        pixels = padded.tobytes()
    header = struct.pack(
        "<IiiHHIIiiII",
        40,
        int(width),
        int(height),
        1,
        24,
        0,
        len(pixels),
        0,
        0,
        0,
        0,
    )
    return header + pixels


def copy_dib_to_clipboard(dib: bytes) -> None:
    """Place a prebuilt CF_DIB blob on the clipboard (hot path)."""
    import ctypes
    from ctypes import wintypes

    if not dib:
        raise ValueError("Empty DIB")

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    CF_DIB = 8
    GMEM_MOVEABLE = 0x0002

    # 64-bit handles truncate if restype stays at the ctypes default (c_int).
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL

    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(dib))
    if not handle:
        raise OSError("GlobalAlloc failed for clipboard image.")
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle)
        raise OSError("GlobalLock failed for clipboard image.")
    try:
        ctypes.memmove(pointer, dib, len(dib))
    finally:
        kernel32.GlobalUnlock(handle)

    opened = False
    for _ in range(8):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.01)
    if not opened:
        kernel32.GlobalFree(handle)
        raise OSError("Could not open the clipboard.")
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_DIB, handle):
            raise OSError("SetClipboardData(CF_DIB) failed.")
        handle = 0
    finally:
        user32.CloseClipboard()
        if handle:
            kernel32.GlobalFree(handle)


def copy_image_to_clipboard(image) -> None:
    """Place an RGB Pillow image on the Windows clipboard as CF_DIB."""
    from PIL import Image

    if not isinstance(image, Image.Image):
        raise TypeError("image must be a Pillow Image")
    rgb = image.convert("RGB")
    bmp_buffer = BytesIO()
    rgb.save(bmp_buffer, format="BMP")
    copy_dib_to_clipboard(bmp_buffer.getvalue()[14:])  # strip BITMAPFILEHEADER


def paste_clipboard() -> None:
    """Send Ctrl+V to the focused window (after the UI has hidden aiOS)."""
    try:
        import keyboard

        keyboard.send("ctrl+v")
        return
    except Exception:
        pass
    _paste_with_sendinput()


def _paste_with_sendinput() -> None:
    import ctypes
    from ctypes import wintypes

    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    VK_CONTROL = 0x11
    VK_V = 0x56

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class INPUT(ctypes.Structure):
        class _I(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT)]

        _anonymous_ = ("i",)
        _fields_ = [("type", wintypes.DWORD), ("i", _I)]

    def press(vk: int, up: bool = False) -> INPUT:
        flags = KEYEVENTF_KEYUP if up else 0
        return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(vk, 0, flags, 0, None))

    events = (INPUT * 4)(
        press(VK_CONTROL),
        press(VK_V),
        press(VK_V, up=True),
        press(VK_CONTROL, up=True),
    )
    sent = ctypes.windll.user32.SendInput(4, events, ctypes.sizeof(INPUT))
    if sent != 4:
        raise OSError("SendInput could not paste the clipboard image.")


class CameraSession:
    """Process-wide OpenCV capture — no browser permission surface."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cap = None
        self._index = -1
        self._name = ""
        self._running = False
        self._thread: threading.Thread | None = None
        self._frame = None
        self._ready_dib: bytes | None = None
        self._frame_lock = threading.Lock()
        self._error = ""
        self._names_cache: list[str] | None = None
        self._idle_timer: threading.Timer | None = None
        self._last_used = 0.0

    @property
    def active(self) -> bool:
        return self._running and self._cap is not None

    def touch(self) -> None:
        """Keep the camera warm; release only after IDLE_COOL_SEC of quiet."""
        self._last_used = time.monotonic()
        timer = self._idle_timer
        if timer is not None:
            timer.cancel()
        nxt = threading.Timer(IDLE_COOL_SEC, self._idle_cool)
        nxt.daemon = True
        self._idle_timer = nxt
        nxt.start()

    def _idle_cool(self) -> None:
        if time.monotonic() - self._last_used < IDLE_COOL_SEC - 0.5:
            return
        self.stop()

    def refresh_names(self) -> list[str]:
        names = list_dshow_video_names()
        self._names_cache = names
        return names

    def list_devices(self) -> list[dict[str, str]]:
        names = self._names_cache if self._names_cache is not None else self.refresh_names()
        if not names:
            return [{"id": "auto", "label": "Auto (Lenovo / first real camera)"}]
        devices = [{"id": "auto", "label": "Auto (prefer Lenovo RGB)"}]
        for index, name in enumerate(names):
            devices.append({"id": str(index), "label": name})
        return devices

    def _probe_live_index(self) -> tuple[int, str]:
        """Find a non-black camera without spawning ffmpeg (index 1 first = Lenovo)."""
        import cv2

        # Rift often occupies 0 and stays black; Lenovo RGB is usually 1.
        for index in (1, 0, 2, 3):
            cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
            if not cap.isOpened():
                try:
                    cap.release()
                except Exception:
                    pass
                continue
            mean = 0.0
            ok_frame = False
            try:
                for _ in range(4):
                    ok, frame = cap.read()
                    if ok and frame is not None:
                        ok_frame = True
                        mean = float(frame.mean())
            finally:
                cap.release()
            if ok_frame and mean >= 8.0:
                return index, f"Camera {index + 1}"
        return 0, "Camera 1"

    def _resolve_device(self, device: int | str | None) -> tuple[int, str]:
        raw = "" if device is None else str(device).strip()
        if raw.casefold() in {"", "auto", "default", "none"}:
            # Reuse last good index — avoids rediscovery on every warm.
            if self._index >= 0:
                return self._index, self._name or f"Camera {self._index + 1}"
            # ffmpeg device list (~1s) beats opening every index (~15s+).
            names = self._names_cache if self._names_cache is not None else self.refresh_names()
            if names:
                index = preferred_device_index(names)
                name = names[index] if 0 <= index < len(names) else ""
                return index, name
            return self._probe_live_index()
        if not raw.isdigit():
            names = self._names_cache if self._names_cache is not None else self.refresh_names()
            needle = raw.casefold()
            for index, name in enumerate(names):
                if needle == name.casefold() or needle in name.casefold():
                    return index, name
            raise ValueError(f"No camera named {raw!r}.")
        index = int(raw)
        if index < 0:
            raise ValueError("Camera index must be >= 0.")
        names = self._names_cache or []
        name = names[index] if 0 <= index < len(names) else f"Camera {index + 1}"
        return index, name

    def start(self, device: int | str | None = "auto") -> None:
        index, name = self._resolve_device(device)
        with self._lock:
            if self._running and self._index == index and self._cap is not None:
                self.touch()
                return
            self.stop()
            import cv2

            cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
            if not cap.isOpened():
                cap.release()
                cap = cv2.VideoCapture(index)
            if not cap.isOpened():
                label = name or f"camera {index}"
                raise OSError(f"Could not open {label}.")
            try:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            # Warm a couple frames so the first ready DIB is not black/stale.
            for _ in range(3):
                cap.read()
            self._cap = cap
            self._index = index
            self._name = name
            self._error = ""
            self._running = True
            self._thread = threading.Thread(
                target=self._reader,
                name="aios-webcam",
                daemon=True,
            )
            self._thread.start()
            self.touch()
            # Block until a clipboard frame exists so the first pad press is ready.
            ready_deadline = time.monotonic() + 2.5
            while time.monotonic() < ready_deadline:
                with self._frame_lock:
                    if self._ready_dib:
                        break
                time.sleep(0.01)
            # Resolve friendly names in the background (ffmpeg is slow).
            if self._names_cache is None:
                threading.Thread(
                    target=self.refresh_names, daemon=True, name="aios-webcam-names"
                ).start()

    def stop(self) -> None:
        with self._lock:
            self._running = False
            thread = self._thread
            self._thread = None
            cap = self._cap
            self._cap = None
            timer = self._idle_timer
            self._idle_timer = None
        if timer is not None:
            timer.cancel()
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        with self._frame_lock:
            self._frame = None
            self._ready_dib = None

    def _reader(self) -> None:
        import cv2

        while self._running:
            cap = self._cap
            if cap is None:
                break
            try:
                ok, frame = cap.read()
            except Exception as exc:
                self._error = str(exc)
                time.sleep(0.02)
                continue
            if not ok or frame is None:
                time.sleep(0.01)
                continue
            oriented = (
                cv2.rotate(frame, cv2.ROTATE_180) if FLIP_UPSIDE_DOWN else frame
            )
            try:
                dib = bgr_frame_to_dib(oriented)
            except Exception as exc:
                self._error = str(exc)
                dib = None
            with self._frame_lock:
                self._frame = frame
                if dib is not None:
                    self._ready_dib = dib
            # Camera is ~30fps; don't spin the core harder than needed.
            time.sleep(0.005)

    def take_ready_dib(self, *, wait_s: float = 2.0) -> bytes:
        """Return the latest prebuilt clipboard DIB (hot path)."""
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            with self._frame_lock:
                dib = self._ready_dib
            if dib:
                return dib
            if not self._running:
                break
            time.sleep(0.005)
        raise OSError(self._error or "Camera has not produced a frame yet.")

    def _raw_frame(self, *, wait_s: float = 1.5):
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            with self._frame_lock:
                frame = None if self._frame is None else self._frame.copy()
            if frame is not None:
                return frame
            if not self._running:
                break
            time.sleep(0.01)
        raise OSError(self._error or "Camera has not produced a frame yet.")

    def _opencv_image(self):
        import cv2
        from PIL import Image

        if not self.active:
            self.start("auto" if self._index < 0 else self._index)
        frame = self._raw_frame()
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        return _orient(Image.fromarray(rgb))

    def capture_image(self):
        """Full-res snap when possible (ffmpeg by name), else the live OpenCV frame."""
        if not self.active:
            self.start("auto" if self._index < 0 else self._index)

        # Named DirectShow capture via ffmpeg gives the Lenovo's real 1080p still.
        if self._name and not self._name.startswith("Camera "):
            try:
                return _orient(_ffmpeg_still(self._name))
            except Exception:
                pass
        return self._opencv_image()

    def preview_data_url(self, *, quality: int = 72) -> str:
        # Preview must stay on the OpenCV stream — ffmpeg stills are too slow.
        image = self._opencv_image()
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=max(40, min(95, int(quality))))
        token = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{token}"


_SESSION = CameraSession()


def camera() -> CameraSession:
    return _SESSION


def warm_camera() -> None:
    """Open the preferred camera in the background so pad snaps are instant."""
    camera().start("auto")


def cool_camera() -> None:
    camera().stop()


def schedule_idle_cool() -> None:
    """Keep the device warm briefly after Quick Tools closes."""
    camera().touch()


def instant_snap_to_clipboard() -> dict[str, Any]:
    """Push the latest prebuilt frame to the clipboard (no encode on press)."""
    session = camera()
    if not session.active:
        session.start("auto")
    try:
        dib = session.take_ready_dib()
        copy_dib_to_clipboard(dib)
        session.touch()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "message": "Snapped",
        "bytes": len(dib),
        "name": session._name,
    }


def handle(data: dict | None) -> dict[str, Any]:
    """Dispatch a webcam_snap Quick Tool action."""
    payload = data or {}
    action = str(payload.get("action") or "copy").strip().lower()
    session = camera()

    if action == "devices":
        return {"ok": True, "devices": session.list_devices(), "active": session.active}

    if action == "warm":
        try:
            warm_camera()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "message": f"Camera warm: {session._name or session._index}",
            "device": str(session._index),
            "name": session._name,
        }

    if action == "start":
        try:
            session.start(payload.get("device", "auto"))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "message": f"Camera ready: {session._name or session._index}",
            "device": str(session._index),
            "name": session._name,
            "devices": session.list_devices(),
        }

    if action == "stop":
        session.stop()
        return {"ok": True, "message": "Camera stopped"}

    if action == "preview":
        try:
            if not session.active:
                session.start(payload.get("device", "auto"))
            image = session.preview_data_url()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "image": image, "name": session._name, "device": str(session._index)}

    if action == "paste":
        try:
            time.sleep(0.05)
            paste_clipboard()
        except Exception as exc:
            return {"ok": False, "error": f"Paste failed: {exc}"}
        return {"ok": True, "message": "Pasted"}

    # Default Quick Tools press: fast frame → clipboard (caller hides + pastes).
    if action in {"instant", "snap_paste"}:
        return instant_snap_to_clipboard()

    if action in {"copy", "copy_and_paste", "snap"}:
        try:
            raw = str(payload.get("image") or "").strip()
            if raw:
                image = decode_image_data_url(raw)
                # Client-supplied frames are already oriented by the preview path;
                # only re-orient when the payload came from an unflipped source.
                if payload.get("orient", False):
                    image = _orient(image)
            elif payload.get("fast") or action == "snap":
                if not session.active:
                    session.start(payload.get("device", "auto"))
                image = session._opencv_image()
            else:
                if not session.active:
                    session.start(payload.get("device", "auto"))
                image = session.capture_image()
        except Exception as exc:
            return {"ok": False, "error": f"Bad image: {exc}"}
        try:
            copy_image_to_clipboard(image)
        except Exception as exc:
            return {"ok": False, "error": f"Clipboard failed: {exc}"}
        if action == "copy" or action == "snap":
            return {
                "ok": True,
                "message": "Copied to clipboard",
                "width": image.width,
                "height": image.height,
            }
        try:
            time.sleep(0.05)
            paste_clipboard()
        except Exception as exc:
            return {
                "ok": False,
                "error": f"Copied, but paste failed: {exc}",
                "copied": True,
            }
        return {
            "ok": True,
            "message": "Snapped and pasted",
            "width": image.width,
            "height": image.height,
        }

    return {"ok": False, "error": f"unknown webcam action {action}"}
