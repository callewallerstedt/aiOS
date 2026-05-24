from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sys
import time
import tkinter as tk
import traceback

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageTk


BASE_DIR = Path(__file__).resolve().parent
FRAME_DIR = BASE_DIR / "assets" / "startup" / "aios-logo-reveal-frames"
LOG_PATH = BASE_DIR / "startup-splash.log"

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
BI_RGB = 0
TRANSPARENT = "#010203"

PAD_X = 64
PAD_Y = 52
CORNER_RADIUS = 22
LOGO_DARK_THRESHOLD = 26
PANEL_TOP = (14, 18, 26)
PANEL_BOTTOM = (9, 12, 18)
PANEL_ALPHA = 255
ACCENT = (110, 231, 200)
CHROMA_KEY = (1, 2, 3)


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class RGBQUAD(ctypes.Structure):
    _fields_ = [
        ("rgbBlue", ctypes.c_ubyte),
        ("rgbGreen", ctypes.c_ubyte),
        ("rgbRed", ctypes.c_ubyte),
        ("rgbReserved", ctypes.c_ubyte),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", RGBQUAD * 1)]


def log(message):
    try:
        with LOG_PATH.open("a", encoding="utf-8") as file:
            file.write(message + "\n")
    except OSError:
        pass


def _lerp(a, b, t):
    return int(a + (b - a) * t)


def _rounded_rect_alpha(width, height, radius):
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, fill=255)
    return mask.point(lambda v: 255 if v >= 128 else 0)


def build_splash_panel(logo_width, logo_height):
    width = logo_width + PAD_X * 2
    height = logo_height + PAD_Y * 2
    panel = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    pixels = panel.load()

    for y in range(height):
        vertical = y / max(height - 1, 1)
        r = _lerp(PANEL_TOP[0], PANEL_BOTTOM[0], vertical)
        g = _lerp(PANEL_TOP[1], PANEL_BOTTOM[1], vertical)
        b = _lerp(PANEL_TOP[2], PANEL_BOTTOM[2], vertical)
        for x in range(width):
            pixels[x, y] = (r, g, b, PANEL_ALPHA)

    rounded = _rounded_rect_alpha(width, height, CORNER_RADIUS)
    panel.putalpha(rounded)

    draw = ImageDraw.Draw(panel)
    draw.rounded_rectangle(
        (0, 0, width - 1, height - 1),
        radius=CORNER_RADIUS,
        outline=(255, 255, 255, 28),
        width=1,
    )

    return panel, (PAD_X, PAD_Y)


def _logo_mask(logo):
    red, green, blue, alpha = logo.split()
    bright = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    mask = bright.point(lambda value: 255 if value > LOGO_DARK_THRESHOLD else 0)
    return ImageChops.multiply(mask, alpha.point(lambda value: 255 if value > 0 else 0))


def compose_splash_frame(logo, panel, offset):
    composed = panel.copy()
    composed.paste(logo, offset, _logo_mask(logo))
    return composed


def flatten_for_display(frame, bg=CHROMA_KEY):
    base = Image.new("RGBA", frame.size, (*bg, 255))
    return Image.alpha_composite(base, frame).convert("RGB")


REVEAL_FRAMES = 48
REVEAL_FEATHER = 80


def _wipe_mask(width, height, progress, feather=REVEAL_FEATHER):
    edge = progress * (width + feather)
    row = Image.new("L", (width, 1), 0)
    pixels = row.load()
    for x in range(width):
        if x <= edge - feather:
            pixels[x, 0] = 255
        elif x >= edge:
            pixels[x, 0] = 0
        else:
            t = (edge - x) / feather
            pixels[x, 0] = max(0, min(255, int(255 * t)))
    return row.resize((width, height))


def prepare_splash_frames(frame_paths):
    logos = [Image.open(path).convert("RGBA") for path in frame_paths]
    if not logos:
        return []
    final_logo = max(logos, key=lambda im: im.convert("L").getextrema()[1])
    panel, offset = build_splash_panel(*final_logo.size)
    base_logo_mask = _logo_mask(final_logo)
    frames = []
    for i in range(REVEAL_FRAMES):
        progress = (i + 1) / REVEAL_FRAMES
        eased = 1 - (1 - progress) ** 2
        wipe = _wipe_mask(final_logo.size[0], final_logo.size[1], eased)
        combined = ImageChops.multiply(base_logo_mask, wipe)
        composed = panel.copy()
        composed.paste(final_logo, offset, combined)
        frames.append(composed)
    return frames


def update_layered_window(hwnd, image, x, y):
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    user32.GetDC.argtypes = [ctypes.c_void_p]
    user32.GetDC.restype = ctypes.c_void_p
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.UpdateLayeredWindow.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(POINT),
        ctypes.POINTER(SIZE),
        ctypes.c_void_p,
        ctypes.POINTER(POINT),
        wintypes.COLORREF,
        ctypes.POINTER(BLENDFUNCTION),
        wintypes.DWORD,
    ]
    user32.UpdateLayeredWindow.restype = wintypes.BOOL
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.restype = wintypes.BOOL
    gdi32.CreateDIBSection.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(BITMAPINFO),
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    gdi32.CreateDIBSection.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteObject.restype = wintypes.BOOL

    width, height = image.size
    rgba = image.tobytes("raw", "RGBA")
    bgra = bytearray(len(rgba))
    for index in range(0, len(rgba), 4):
        r, g, b, a = rgba[index:index + 4]
        bgra[index] = (b * a) // 255
        bgra[index + 1] = (g * a) // 255
        bgra[index + 2] = (r * a) // 255
        bgra[index + 3] = a

    hwnd_ptr = ctypes.c_void_p(hwnd)
    screen_dc = user32.GetDC(None)
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    bits = ctypes.c_void_p()
    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = width
    info.bmiHeader.biHeight = -height
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = BI_RGB
    bitmap = gdi32.CreateDIBSection(screen_dc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
    if not bitmap or not bits:
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(0, screen_dc)
        raise OSError("CreateDIBSection failed")
    old_bitmap = gdi32.SelectObject(mem_dc, bitmap)
    try:
        ctypes.memmove(bits, bytes(bgra), len(bgra))
        ok = user32.UpdateLayeredWindow(
            hwnd_ptr,
            screen_dc,
            ctypes.byref(POINT(x, y)),
            ctypes.byref(SIZE(width, height)),
            mem_dc,
            ctypes.byref(POINT(0, 0)),
            0,
            ctypes.byref(BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)),
            ULW_ALPHA,
        )
        if not ok:
            raise OSError("UpdateLayeredWindow failed")
    finally:
        gdi32.SelectObject(mem_dc, old_bitmap)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(0, screen_dc)


def run_layered(frames):
    root = tk.Tk()
    root.withdraw()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    width, height = frames[0].size
    x = (root.winfo_screenwidth() - width) // 2
    y = (root.winfo_screenheight() - height) // 2
    root.geometry(f"{width}x{height}+{x}+{y}")
    root.update_idletasks()
    hwnd = root.winfo_id()
    user32 = ctypes.windll.user32
    user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_void_p
    user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    user32.SetWindowLongPtrW.restype = ctypes.c_void_p
    hwnd_ptr = ctypes.c_void_p(hwnd)
    style = user32.GetWindowLongPtrW(hwnd_ptr, GWL_EXSTYLE) or 0
    user32.SetWindowLongPtrW(hwnd_ptr, GWL_EXSTYLE, ctypes.c_void_p(style | WS_EX_LAYERED))
    update_layered_window(hwnd, frames[0], x, y)
    root.deiconify()
    root.lift()

    def step(index=1):
        if index >= len(frames):
            def finish():
                root.quit()
                root.destroy()
            root.after(260, finish)
            return
        update_layered_window(hwnd, frames[index], x, y)
        root.after(34, lambda: step(index + 1))

    root.after(80, step)
    root.after(4800, lambda: os._exit(0))
    root.mainloop()


def run_tk_fallback(frames):
    root = tk.Tk()
    root.withdraw()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    chroma = "#{:02x}{:02x}{:02x}".format(*CHROMA_KEY)
    root.configure(bg=chroma)
    try:
        root.attributes("-transparentcolor", chroma)
    except tk.TclError:
        pass
    images = [ImageTk.PhotoImage(flatten_for_display(frame), master=root) for frame in frames]
    label = tk.Label(root, image=images[0], bg=chroma, bd=0, highlightthickness=0)
    label.pack()
    width = images[0].width()
    height = images[0].height()
    root.geometry(f"{width}x{height}+{(root.winfo_screenwidth() - width) // 2}+{(root.winfo_screenheight() - height) // 2}")
    root.deiconify()
    root.lift()
    root._images = images

    def step(index=1):
        if index >= len(images):
            def finish():
                root.quit()
                root.destroy()
            root.after(260, finish)
            return
        label.configure(image=images[index])
        root.after(34, lambda: step(index + 1))

    root.after(80, step)
    root.after(4800, lambda: os._exit(0))
    root.mainloop()


def main():
    try:
        LOG_PATH.write_text("", encoding="utf-8")
    except OSError:
        pass
    frame_paths = sorted(FRAME_DIR.glob("frame_*.png"))
    if not frame_paths:
        log("no frames")
        return
    try:
        frames = prepare_splash_frames(frame_paths)
        if not frames:
            log("no composed frames")
            return
        run_tk_fallback(frames)
        log("ok")
    except Exception as exc:
        log(f"failed: {exc}")
        log(traceback.format_exc())


if __name__ == "__main__":
    try:
        main()
    finally:
        os._exit(0)
