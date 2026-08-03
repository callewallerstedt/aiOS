"""Local live speech-to-text dictation, controlled by AutoHotkey.

Behavior
--------
- Long-running background process. Started automatically by aiOS on launch.
- AHK sends "start" / "stop" on a local TCP socket (port 48737) when the user
  holds or releases Insert. This script does NOT register a global keyboard hook.
- While active, audio from the default mic is transcribed locally with
  faster-whisper. Nothing is typed while you speak: the words collect in a
  composer panel above the mic pill so you can see what was heard.
- On release the transcript is sent to the current target:
    cursor    — typed/pasted into the window you had focused (the default)
    clipboard — copied, nothing typed
    agent     — handed to voice_agent.py, which answers and can call tools
                (web search, open an app, PowerShell, aiOS OPERATOR)
  The macro keyboard picks the target mid-hold by sending "target:agent" etc.
  on the same socket; see voice_target_*.bat.

CLI
---
    python voice_dictation.py                  # run the background server + overlay
    python voice_dictation.py --toggle         # ping the server to start/stop
    python voice_dictation.py --stop           # force-stop the dictation
    python voice_dictation.py --target agent   # route this turn to the agent
    python voice_dictation.py --cancel         # drop what was said, send nothing
    python voice_dictation.py --stop-agent     # abort the agent / OPERATOR, stop talking
    python voice_dictation.py --quit           # quit the background process

If --toggle is sent and no server is running, this process becomes the server
and starts dictation immediately.

First run downloads the Whisper model (~470 MB) into the HF cache.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import queue
import re
import socket
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from collections import deque
from ctypes import wintypes
from pathlib import Path

from voice_settings import load_voice_dictation_settings, resolve_transcribe_language
from voice_text import (
    apply_replacements,
    build_initial_prompt,
    is_hallucination,
    is_non_speech_marker,
    join_chunks,
    tidy_transcript,
)

try:
    from minecraft_ai import MinecraftVoiceAssistant, foreground_window_rect, is_minecraft_foreground
except Exception:
    MinecraftVoiceAssistant = None
    foreground_window_rect = None

    def is_minecraft_foreground():
        return False

try:
    from minecraft_chat_bridge import start_minecraft_chat_bridge
except Exception:
    start_minecraft_chat_bridge = None

try:
    from voice_agent import send_to_helper
except Exception:  # the agent is optional; dictation still works without it

    def send_to_helper(action, text="", options=None):
        return False

HOST = "127.0.0.1"
PORT = 48737

MISSING = []
keyboard = None
np = None
sd = None
WhisperModel = None
CUDA_DLL_HANDLES = []


def load_runtime_dependencies():
    global MISSING, WhisperModel, keyboard, np, sd
    if keyboard is not None and np is not None and sd is not None:
        return []
    missing = []
    try:
        import keyboard as keyboard_module
    except ImportError:
        missing.append("keyboard")
    else:
        keyboard = keyboard_module
    try:
        import numpy as numpy_module
    except ImportError:
        missing.append("numpy")
    else:
        np = numpy_module
    try:
        import sounddevice as sounddevice_module
    except ImportError:
        missing.append("sounddevice")
    else:
        sd = sounddevice_module
    MISSING = missing
    return missing


def load_whisper_dependency():
    global WhisperModel
    if WhisperModel is not None:
        return
    prepare_cuda_dll_dirs()
    try:
        from faster_whisper import WhisperModel as WhisperModelClass
    except ImportError as exc:
        raise RuntimeError("faster-whisper is not installed") from exc
    WhisperModel = WhisperModelClass


def prepare_cuda_dll_dirs():
    if not sys.platform.startswith("win") or CUDA_DLL_HANDLES:
        return
    candidates = []
    for base in [Path(path) for path in sys.path if "site-packages" in str(path).lower()]:
        candidates.extend(
            [
                base / "nvidia" / "cublas" / "bin",
                base / "nvidia" / "cudnn" / "bin",
                base / "nvidia" / "cuda_nvrtc" / "bin",
            ]
        )
    added = []
    for path in candidates:
        if not path.exists():
            continue
        try:
            CUDA_DLL_HANDLES.append(os.add_dll_directory(str(path)))
            added.append(str(path))
        except (AttributeError, OSError):
            pass
    if added:
        os.environ["PATH"] = os.pathsep.join(added + [os.environ.get("PATH", "")])
        log_event("cuda dll dirs loaded: " + "; ".join(added))


SAMPLE_RATE = 16000
# Absolute floor for a clip worth sending to Whisper. Anything between this and
# the configured min_speech_seconds is padded with silence rather than dropped,
# so one-word commands ("yes", "stop", "nej") survive.
MIN_AUDIO_SECONDS = 0.12
# Whisper needs roughly a third of a second of context to decode reliably; short
# clips are padded up to this before transcription.
PAD_TO_SECONDS = 0.5
MAX_BUFFER_SECONDS = 10.0
# When the 10 s cap forces a flush mid-sentence, this much audio is carried into
# the next buffer so the cut does not fall inside a word.
FLUSH_OVERLAP_SECONDS = 0.25
PASTE_MIN_CHARS = 18
# A language guess made from a single short chunk is unreliable, so the session
# lock waits for this much audio and this much confidence.
LANGUAGE_LOCK_MIN_SECONDS = 1.6
LANGUAGE_LOCK_MIN_PROBABILITY = 0.65
# Waveform metering: the mic envelope is measured in dBFS so the bars follow
# perceived loudness instead of raw linear amplitude (which barely moves for
# normal speech). Anything under the floor reads as silence, the ceiling is
# roughly a loud syllable.
LEVEL_FLOOR_DB = -58.0
LEVEL_CEIL_DB = -8.0
LEVEL_SLICES = 3  # envelope samples taken per 100 ms audio block
RGN_OR = 2
# Where a finished transcript goes. The macro keyboard switches this mid-hold.
TARGETS = ("cursor", "clipboard", "agent")
TARGET_ALIASES = {
    "cursor": "cursor", "type": "cursor", "text": "cursor",
    "clipboard": "clipboard", "copy": "clipboard",
    "agent": "agent", "chatgpt": "agent", "gpt": "agent", "ai": "agent", "assistant": "agent",
}
# How long the panel stays up after a turn finishes.
COMPOSE_LINGER_MS = 9000
AGENT_LINGER_MS = 25000
# The agent transcript grows upward from the fixed mic pill. Once this viewport
# is full, older turns roll off the top so the newest answer remains visible.
COMPOSE_MAX_HEIGHT = 960
CONFIG_PATH = Path(__file__).resolve().parent / "helper_config.json"
LOG_PATH = Path(__file__).resolve().parent / "voice-err.log"
TRANSCRIPT_LOG_PATH = Path(__file__).resolve().parent / "voice-transcripts.jsonl"
# Everything the agent says, mirrored to disk so the phone PWA can follow the
# same conversation the desktop overlay shows. Same shape as the OPERATOR
# stream, so the phone reads it with the identical byte-offset SSE logic.
PHONE_VOICE_DIR = Path(__file__).resolve().parent / "phone_voice_events"
PHONE_VOICE_EVENTS = PHONE_VOICE_DIR / "events.jsonl"
PHONE_VOICE_MAX_BYTES = 512_000
GWL_EXSTYLE = -20
GWLP_WNDPROC = -4
GA_ROOT = 2
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WDA_MONITOR = 0x00000001
WDA_EXCLUDEFROMCAPTURE = 0x00000011
LWA_COLORKEY = 0x00000001
LWA_ALPHA = 0x00000002
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
WM_NCHITTEST = 0x0084
HTTRANSPARENT = -1
# Keep the HUD clear of the Windows taskbar / docked bars.
TASKBAR_PAD = 72


def log_event(message):
    try:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with LOG_PATH.open("a", encoding="utf-8") as file:
            file.write(f"{timestamp} {message}\n")
    except OSError:
        pass


def stderr_write(message):
    stream = getattr(sys, "stderr", None)
    if stream is None:
        return
    try:
        stream.write(message)
        stream.flush()
    except Exception:
        pass


def load_theme():
    defaults = {
        "panel": "#0d0d0d",
        "surface": "#111111",
        "surface2": "#3a3a3a",
        "accent": "#ffffff",
        "text": "#f4f7fb",
        "muted": "#9a9a9a",
        "success": "#38d996",
        "danger": "#ff5f57",
        "opacity": 0.95,
    }
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as file:
            theme = json.load(file).get("theme", {})
    except (OSError, json.JSONDecodeError):
        theme = {}
    if isinstance(theme, dict):
        defaults.update({key: value for key, value in theme.items() if key in defaults})
    return defaults


def clean_reply(text):
    """Flatten the odd bit of markdown the model emits so the panel reads clean."""
    text = str(text or "").strip()
    text = re.sub(r"```[\w+-]*\n?", "", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*]\s+", "· ", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\w)[*_](\S.*?)[*_](?!\w)", r"\1", text)
    return re.sub(r"\n{2,}", "\n", text).strip()


def die_missing():
    msg = (
        "Voice dictation needs these Python packages:\n  "
        + "\n  ".join(MISSING)
        + "\n\nInstall with:\n  pip install keyboard numpy sounddevice faster-whisper\n"
    )
    stderr_write(msg)
    sys.exit(1)


def send_command(command):
    try:
        with socket.create_connection((HOST, PORT), timeout=0.2) as client:
            if isinstance(command, (bytes, bytearray)):
                client.sendall(command)
            else:
                client.sendall(str(command).encode("utf-8"))
        return True
    except OSError:
        return False


def mirror_phone_event(kind, text="", extra=None):
    """Append one agent event to the phone's stream.

    Best-effort by design: the phone is a nice-to-have mirror and must never be
    able to break or slow down a spoken turn.
    """
    record = {"ts": time.time(), "type": str(kind), "text": str(text or "")}
    if isinstance(extra, dict):
        record.update(extra)
    try:
        PHONE_VOICE_DIR.mkdir(parents=True, exist_ok=True)
        # Trim before appending so the file cannot grow without bound. The
        # phone's reader treats a shrink as a reset and re-syncs.
        try:
            if PHONE_VOICE_EVENTS.stat().st_size > PHONE_VOICE_MAX_BYTES:
                lines = PHONE_VOICE_EVENTS.read_text(encoding="utf-8", errors="replace").splitlines()
                PHONE_VOICE_EVENTS.write_text("\n".join(lines[-400:]) + "\n", encoding="utf-8")
        except OSError:
            pass
        with PHONE_VOICE_EVENTS.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        log_event(f"phone mirror failed: {exc}")


def send_ask(text, echo_user=True):
    """Ask the running voice agent a typed question (JSON control message)."""
    payload = json.dumps({"cmd": "ask", "text": str(text or ""), "echo_user": bool(echo_user)})
    try:
        with socket.create_connection((HOST, PORT), timeout=1.5) as client:
            client.sendall(payload.encode("utf-8"))
        return True
    except OSError:
        return False


class MicOverlay:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Voice")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        theme = load_theme()
        self.transparent = "#010203"
        self.bg = theme["panel"]
        self.panel_bg = theme["surface"]
        self.surface2 = theme["surface2"]
        self.accent = theme["accent"]
        self.base_accent = self.accent
        self.text = theme["text"]
        self.muted = theme["muted"]
        self.success = theme["success"]
        self.danger = theme["danger"]
        # Pill + compose panel share the same soft translucency (Settings → Overlay opacity).
        self.base_opacity = 0.85
        self.panel_opacity = 0.85
        self.opacity = self.base_opacity
        try:
            self.apply_opacity_settings(load_voice_dictation_settings())
        except Exception:
            pass
        self.font_title = tkfont.Font(family="Segoe UI", size=11, weight="bold")
        self.font_label = tkfont.Font(family="Segoe UI", size=8, weight="bold")
        self.font_body = tkfont.Font(family="Segoe UI", size=9)
        self.font_count = tkfont.Font(family="Segoe UI", size=9, weight="bold")
        self.font_status = tkfont.Font(family="Segoe UI", size=9)
        self.font_goal = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        self.font_pill = tkfont.Font(family="Segoe UI", size=9)
        self.font_compose = tkfont.Font(family="Segoe UI", size=10)
        self.font_reply = tkfont.Font(family="Segoe UI", size=10)
        self.font_chip = tkfont.Font(family="Segoe UI", size=8, weight="bold")
        self.font_tool = tkfont.Font(family="Segoe UI", size=8)
        self.pill_size = (236, 52)
        self.compose_width = 470
        self.compose_gap = 10
        self.mc_width = 300
        self.width, self.height = self.pill_size
        self.mode = "dictation"
        self.title_text = "Voice"
        self.mc_user = ""
        self.mc_reply = "Ready."
        self.mc_goal = ""
        self.mc_items = []
        self.root.configure(bg=self.transparent)
        self.canvas = tk.Canvas(
            self.root,
            width=self.width,
            height=self.height,
            bg=self.transparent,
            highlightthickness=0,
            bd=0,
        )
        # No fill/expand — pack must not fight SetWindowPos when the compose
        # panel grows upward (that was shoving the pill down one frame).
        self.canvas.pack()
        self.level = 0.0
        self.status_text = ""
        self.recording = False
        self._last_mc_rect = None
        self.pulse = 0.0
        self._after = None
        self._visible = False
        self._blend_cache = {}
        self._clickthrough_wndproc = None
        self._clickthrough_old_wndproc = None
        self._clickthrough_hwnd = None
        self._capture_exclusion_ok = False
        # Scrolling mic envelope: newest sample on the right, one bar each.
        self.bar_pitch = 5
        self.bar_width = 3
        self.bar_count = 40
        self.levels = deque([0.0] * self.bar_count, maxlen=self.bar_count)
        self._last_level_at = 0.0
        # Composer panel: the conversation so far, what was heard just now,
        # where it is going, and what came back.
        self.compose_open = False
        self.compose_target = "cursor"
        self.compose_history = []
        self.compose_text = ""
        self.compose_note = ""
        self.compose_tools = []
        self.compose_reply = ""
        self.compose_error = ""
        self._compose_cache = (None, [], 0)
        # Screen position of the pill's bottom-center. Kept across panel open/
        # close so growing the compose HUD never nudges the mic bar.
        self._pill_anchor = None
        self._apply_transparency()
        self._apply_window_shape()

    # ------------------------------------------------------------- composer API

    def apply_opacity_settings(self, settings=None):
        """Load overlay opacity from voice settings (20–100%)."""
        settings = settings or load_voice_dictation_settings()
        try:
            percent = int(settings.get("overlay_opacity", 85))
        except (TypeError, ValueError):
            percent = 85
        amount = max(0.20, min(1.0, percent / 100.0))
        self.base_opacity = amount
        self.panel_opacity = amount
        self.opacity = amount
        if self._visible:
            self._apply_transparency()

    def set_target(self, target):
        target = target if target in TARGETS else "cursor"
        if self.compose_target == target:
            return
        self.compose_target = target
        # Bust the compose cache so the chip label redraws immediately.
        self._compose_cache = (None, [], 0)
        # Only the chip label / accent change — keep transcript & chat on screen.
        self._relayout()

    def set_history(self, rows):
        """Earlier turns of the running conversation: [(role, text), ...]."""
        self.compose_history = [
            (str(role), str(text)) for role, text in (rows or []) if str(text).strip()
        ]
        self._relayout()

    def set_transcript(self, text):
        text = str(text or "")
        self.compose_text = text
        if text:
            self.compose_open = True
            # Fresh utterance replaces the previous reply bubble in-place.
            if self.compose_target == "agent":
                self.compose_reply = ""
                self.compose_error = ""
                self.compose_tools = []
        self._relayout()

    def set_compose_note(self, note):
        self.compose_note = str(note or "")
        self._relayout()

    def push_tool(self, line):
        line = str(line or "").strip()
        if not line:
            return
        self.compose_tools.append(line)
        self.compose_tools = self.compose_tools[-4:]
        self.compose_open = True
        self._relayout()

    def set_reply(self, text):
        self.compose_reply = clean_reply(text)
        self.compose_open = True
        self._relayout()

    def start_reply_stream(self):
        self.compose_reply = ""
        self.compose_error = ""
        self.compose_open = True
        self._relayout()

    def append_reply_delta(self, delta):
        delta = str(delta or "")
        if not delta:
            return
        self.compose_reply += delta
        self.compose_open = True
        self._relayout()

    def set_compose_error(self, text):
        self.compose_error = str(text or "")
        if self.compose_error:
            self.compose_open = True
        self._relayout()

    def open_compose(self):
        # Never open an empty black slab — only expand when there is content.
        if not (
            str(self.compose_text or "").strip()
            or str(self.compose_reply or "").strip()
            or str(self.compose_error or "").strip()
            or self.compose_history
            or self.compose_tools
        ):
            return
        self.compose_open = True
        self._relayout()

    def clear_compose(self):
        self.compose_open = False
        self.compose_history = []
        self.compose_text = ""
        self.compose_note = ""
        self.compose_tools = []
        self.compose_reply = ""
        self.compose_error = ""
        self._compose_cache = (None, [], 0)
        self._relayout()

    def clear_turn(self):
        """New turn, same conversation: drop the current rows, keep the chat."""
        self.compose_text = ""
        self.compose_note = ""
        self.compose_tools = []
        self.compose_reply = ""
        self.compose_error = ""
        self._relayout()

    def has_compose_content(self):
        return self._panel_height() > 0

    def _compose_signature(self):
        return (
            self.compose_target,
            tuple(self.compose_history),
            self.compose_text,
            self.compose_note,
            tuple(self.compose_tools),
            self.compose_reply,
            self.compose_error,
        )

    def _compute_dictation_placement(self, width, height):
        """Pin the mic pill's bottom-center; grow/shrink the chat upward only.

        Once `_pill_anchor` is set, its bottom Y is frozen for the session so a
        TO CURSOR / transcript panel can never shove the pill downward.
        """
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        max_bottom = float(max(self.pill_size[1] + 8, sh - TASKBAR_PAD))
        if self._pill_anchor is None:
            pill_cx = sw / 2.0
            pill_bottom = max_bottom
        else:
            pill_cx, pill_bottom = self._pill_anchor
            pill_cx = float(pill_cx)
            # Clamp up only — never allow a lower (larger-Y) bottom than locked.
            pill_bottom = min(float(pill_bottom), max_bottom)

        x = int(round(pill_cx - width / 2.0))
        y = int(round(pill_bottom - height))
        x = max(0, min(x, max(0, sw - width)))
        if y < 8:
            # Not enough room above: slide the whole HUD up (pill moves up too).
            y = 8
            pill_bottom = float(y + height)
            if pill_bottom > max_bottom:
                pill_bottom = max_bottom
                y = max(8, int(round(pill_bottom - height)))
        return x, y, float(pill_cx), float(pill_bottom)

    def _apply_dictation_bounds(self, width, height):
        """Atomically resize+reposition so the pill bottom stays glued on screen."""
        width = int(width)
        height = int(height)
        locked = self._pill_anchor
        x, y, pill_cx, pill_bottom = self._compute_dictation_placement(width, height)
        if locked is not None:
            locked_cx, locked_bottom = float(locked[0]), float(locked[1])
            # Freeze the locked bottom. Only accept an upward correction (y clamp).
            if pill_bottom < locked_bottom - 0.5:
                self._pill_anchor = (locked_cx, pill_bottom)
            else:
                pill_cx, pill_bottom = locked_cx, locked_bottom
                x = int(round(pill_cx - width / 2.0))
                y = int(round(pill_bottom - height))
                sw = self.root.winfo_screenwidth()
                x = max(0, min(x, max(0, sw - width)))
                if y < 8:
                    y = 8
                    pill_bottom = float(y + height)
                    self._pill_anchor = (pill_cx, pill_bottom)
                else:
                    self._pill_anchor = (pill_cx, pill_bottom)
        else:
            self._pill_anchor = (pill_cx, pill_bottom)

        self.width, self.height = width, height
        # SetWindowPos first — Tk geometry alone often grows height downward
        # from the old top-left for one frame (pill appears to drop).
        if sys.platform.startswith("win"):
            try:
                hwnd = self._overlay_hwnd()
                if hwnd:
                    ctypes.windll.user32.SetWindowPos(
                        hwnd,
                        0,
                        int(x),
                        int(y),
                        int(width),
                        int(height),
                        SWP_NOZORDER | SWP_NOACTIVATE,
                    )
            except (AttributeError, OSError, tk.TclError):
                pass
        try:
            self.root.geometry(f"{width}x{height}+{x}+{y}")
        except tk.TclError:
            pass
        try:
            self.canvas.configure(width=width, height=height, bg=self.transparent)
        except tk.TclError:
            pass
        self._apply_window_shape()
        return x, y

    def _place_bottom_center(self):
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        if self.mode == "minecraft":
            self._pill_anchor = None
            rect = foreground_window_rect() if foreground_window_rect else None
            if rect and self._sane_window_rect(rect):
                self._last_mc_rect = rect
            else:
                rect = self._last_mc_rect
            if rect:
                left, top, right, _bottom = rect
                x = max(left + 24, right - self.width - 36)
                y = top + 76
            else:
                x = max(24, sw - self.width - 36)
                y = 76
            geometry = f"{self.width}x{self.height}+{x}+{y}"
            try:
                if self.root.geometry() != geometry:
                    self.root.geometry(geometry)
            except tk.TclError:
                pass
            self._apply_window_shape()
            return
        self._apply_dictation_bounds(self.width, self.height)

    @staticmethod
    def _sane_window_rect(rect):
        try:
            left, top, right, bottom = [int(value) for value in rect]
        except Exception:
            return False
        width = right - left
        height = bottom - top
        return width >= 320 and height >= 240 and all(-10000 < value < 10000 for value in (left, top, right, bottom))

    def show(self):
        if self._visible:
            self.root.lift()
            self._exclude_from_capture()
            return
        self._place_bottom_center()
        try:
            self.root.deiconify()
        except tk.TclError:
            pass
        try:
            self.root.update_idletasks()
            self.root.attributes("-topmost", True)
            self._apply_transparency()
            self._apply_window_shape()
            self._exclude_from_capture()
            self._force_topmost()
            self.root.update()
        except tk.TclError:
            pass
        self._visible = True
        self._install_clickthrough()
        self._exclude_from_capture()
        stderr_write(f"[overlay] shown at geometry {self.root.geometry()}\n")
        self._animate()

    def hide(self):
        # Always withdraw — _visible can desync if a queued show races a dismiss.
        self._visible = False
        # Keep _pill_anchor so the next show() lands in the same spot instead of
        # jumping back to the default bottom-center.
        if self._after:
            try:
                self.root.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None
        try:
            self.root.withdraw()
        except tk.TclError:
            pass

    def set_level(self, level):
        self.push_levels([level])

    def push_levels(self, values):
        """Append real mic envelope samples (0..1) to the scrolling waveform."""
        for value in values:
            try:
                value = max(0.0, min(1.0, float(value)))
            except (TypeError, ValueError):
                continue
            self.levels.append(value)
            self.level = value
        self._last_level_at = time.monotonic()

    def reset_levels(self):
        self.levels.extend([0.0] * self.bar_count)
        self.level = 0.0
        self._last_level_at = 0.0

    def set_status(self, text):
        self.status_text = text

    def set_recording(self, active):
        """Keep the idle waveform visually distinct from live microphone capture."""
        self.recording = bool(active)

    def set_mode(self, mode):
        mode = "minecraft" if mode == "minecraft" else "dictation"
        previous = self.mode
        self.mode = mode
        if mode == "minecraft":
            self.title_text = "MC AI"
            self.accent = self.success
            self.width = self.mc_width
            _, target_height = self._mc_build()
            if self.height != target_height or int(self.canvas["width"]) != self.width:
                self.height = target_height
                self.canvas.configure(width=self.width, height=self.height)
                self._apply_window_shape()
            if self._visible:
                self._place_bottom_center()
            return
        self.title_text = "Voice"
        self.accent = self.base_accent
        # Staying in dictation must not snap back to the bare pill — that flash
        # is what you see when re-holding during an open agent chat.
        if previous == "dictation":
            return
        self.width, self.height = self.pill_size
        self.canvas.configure(width=self.width, height=self.height)
        self._relayout()
        if self._visible:
            self._place_bottom_center()

    def commit_current_turn(self):
        """Move the live transcript/reply into history so a re-hold can't erase them."""
        user = str(self.compose_text or "").strip()
        if user:
            last = self.compose_history[-1] if self.compose_history else None
            if not last or last != ("user", user):
                self.compose_history.append(("user", user))
        for tool in self.compose_tools:
            line = str(tool or "").strip()
            if line:
                self.compose_history.append(("tool", line))
        reply = str(self.compose_reply or "").strip()
        error = str(self.compose_error or "").strip()
        if reply:
            last = self.compose_history[-1] if self.compose_history else None
            if not last or last != ("assistant", reply):
                self.compose_history.append(("assistant", reply))
        elif error:
            self.compose_history.append(("assistant", error))
        self.compose_text = ""
        self.compose_reply = ""
        self.compose_error = ""
        self.compose_tools = []

    def prepare_listen(self):
        """Keep the agent chat visible while a new hold starts — no wipe/flash."""
        self.commit_current_turn()
        self.compose_note = ""
        self.compose_open = True
        self._relayout()

    def show_finished_turn(self, history_rows, note=""):
        """After the agent answers: history is the source of truth, no ephemeral reply."""
        self.compose_history = [
            (str(role), str(text)) for role, text in (history_rows or []) if str(text).strip()
        ]
        self.compose_text = ""
        self.compose_reply = ""
        self.compose_error = ""
        self.compose_tools = []
        self.compose_note = str(note or "")
        self.compose_open = True
        self._relayout()

    def set_minecraft_state(self, state):
        if not isinstance(state, dict):
            return
        self.mc_user = str(state.get("user") or self.mc_user or "")[:160]
        self.mc_reply = str(state.get("reply") or self.mc_reply or "")[:260]
        self.mc_goal = str(state.get("goal") or "")[:80]
        items = state.get("shopping") or []
        self.mc_items = items[:8] if isinstance(items, list) else []
        self.set_mode("minecraft")

    def _overlay_hwnd(self):
        """Top-level HWND for Win32 style / region calls (not the Tk child)."""
        child = int(self.root.winfo_id())
        try:
            root = ctypes.windll.user32.GetAncestor(child, GA_ROOT)
            return int(root or child)
        except (AttributeError, OSError):
            return child

    def _overlay_hwnds(self):
        """Every HWND Tk may route mouse hits through for this overlay."""
        hwnds = []
        try:
            child = int(self.root.winfo_id())
            hwnds.append(child)
            user32 = ctypes.windll.user32
            parent = int(user32.GetParent(child) or 0)
            if parent:
                hwnds.append(parent)
            root = int(user32.GetAncestor(child, GA_ROOT) or 0)
            if root:
                hwnds.append(root)
        except (AttributeError, OSError, tk.TclError, TypeError, ValueError):
            pass
        out = []
        seen = set()
        for hwnd in hwnds:
            if hwnd and hwnd not in seen:
                seen.add(hwnd)
                out.append(hwnd)
        return out

    def _exclude_from_capture(self):
        """Keep the dictation/agent HUD out of OPERATOR desktop captures."""
        if not sys.platform.startswith("win"):
            self._capture_exclusion_ok = False
            return False
        try:
            self.root.update_idletasks()
            hwnd = self._overlay_hwnd()
            user32 = ctypes.windll.user32
            excluded = bool(user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE))
            if not excluded:
                excluded = bool(user32.SetWindowDisplayAffinity(hwnd, WDA_MONITOR))
            self._capture_exclusion_ok = excluded
            return excluded
        except (AttributeError, OSError, tk.TclError, TypeError, ValueError):
            self._capture_exclusion_ok = False
            return False

    def _install_clickthrough(self):
        """Force click-through via WS_EX_TRANSPARENT on every related HWND."""
        if not sys.platform.startswith("win"):
            return
        try:
            self.root.update_idletasks()
            user32 = ctypes.windll.user32
            hwnds = self._overlay_hwnds()
            if not hwnds:
                return
            for hwnd in hwnds:
                style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                user32.SetWindowLongW(
                    hwnd,
                    GWL_EXSTYLE,
                    style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                )
            if self._clickthrough_wndproc is not None:
                return
            hwnd = hwnds[-1]
            wndproc_type = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t,
                wintypes.HWND,
                wintypes.UINT,
                ctypes.c_size_t,
                ctypes.c_ssize_t,
            )
            user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.GetWindowLongPtrW.restype = ctypes.c_void_p
            user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
            user32.SetWindowLongPtrW.restype = ctypes.c_void_p
            user32.CallWindowProcW.argtypes = [
                ctypes.c_void_p,
                wintypes.HWND,
                wintypes.UINT,
                ctypes.c_size_t,
                ctypes.c_ssize_t,
            ]
            user32.CallWindowProcW.restype = ctypes.c_ssize_t
            original = user32.GetWindowLongPtrW(hwnd, GWLP_WNDPROC)

            def wndproc(window, message, wparam, lparam):
                if message == WM_NCHITTEST:
                    return HTTRANSPARENT
                return user32.CallWindowProcW(original, window, message, wparam, lparam)

            callback = wndproc_type(wndproc)
            if not user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, ctypes.cast(callback, ctypes.c_void_p)):
                return
            self._clickthrough_hwnd = hwnd
            self._clickthrough_old_wndproc = original
            self._clickthrough_wndproc = callback
        except (AttributeError, OSError, tk.TclError, ValueError, TypeError):
            self._clickthrough_wndproc = None

    def _apply_transparency(self):
        try:
            self.root.attributes("-transparentcolor", self.transparent)
        except tk.TclError:
            pass
        alpha = max(0, min(255, int(self.opacity * 255)))
        if not sys.platform.startswith("win"):
            try:
                self.root.attributes("-alpha", self.opacity)
            except tk.TclError:
                pass
            return
        try:
            user32 = ctypes.windll.user32
            hwnds = self._overlay_hwnds() or [self._overlay_hwnd()]
            r, g, b = self.root.winfo_rgb(self.transparent)
            color_key = (r // 256) | ((g // 256) << 8) | ((b // 256) << 16)
            for hwnd in hwnds:
                style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                # WS_EX_TRANSPARENT makes mouse hits fall through to apps below.
                user32.SetWindowLongW(
                    hwnd,
                    GWL_EXSTYLE,
                    style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                )
                user32.SetLayeredWindowAttributes(
                    hwnd, color_key, alpha, LWA_COLORKEY | LWA_ALPHA
                )
        except (AttributeError, OSError, tk.TclError):
            pass

    def _force_topmost(self):
        try:
            self.root.attributes("-topmost", True)
            if sys.platform.startswith("win"):
                user32 = ctypes.windll.user32
                hwnd = self._overlay_hwnd()
                user32.SetWindowPos(
                    hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
                )
                # Tk can clear EXSTYLE on resize/paint — keep click-through glued on.
                for handle in self._overlay_hwnds():
                    style = user32.GetWindowLongW(handle, GWL_EXSTYLE)
                    if not (style & WS_EX_TRANSPARENT):
                        user32.SetWindowLongW(
                            handle,
                            GWL_EXSTYLE,
                            style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                        )
            else:
                self.root.lift()
        except (AttributeError, OSError, tk.TclError):
            pass

    # ------------------------------------------------------------------ layout

    def _panel_height(self):
        if self.mode == "minecraft" or not self.compose_open:
            return 0
        _ops, height = self._compose_build()
        return int(height or 0)

    def _pill_origin(self):
        pill_w, pill_h = self.pill_size
        return (self.width - pill_w) / 2.0, self.height - pill_h

    def _relayout(self):
        if self.mode == "minecraft":
            return
        pill_w, pill_h = self.pill_size
        panel_h = self._panel_height()
        # Collapse empty compose so we never leave a hollow black rectangle.
        if self.compose_open and panel_h <= 0:
            self.compose_open = False
            panel_h = 0
        if panel_h:
            width = max(pill_w, self.compose_width)
            height = panel_h + self.compose_gap + pill_h
        else:
            width, height = pill_w, pill_h
        opacity = self.panel_opacity if panel_h else self.base_opacity
        size_changed = width != self.width or height != self.height
        # Keep both states at the configured translucency (panel used to go solid).
        if abs(opacity - self.opacity) > 0.001:
            self.opacity = opacity
            self._apply_transparency()
        if not size_changed:
            return
        # Grow/shrink upward from the frozen pill bottom (Win32 atomic move+size).
        x, y = self._apply_dictation_bounds(width, height)
        if self._visible:
            self._paint_frame()
            try:
                self.root.update_idletasks()
            except tk.TclError:
                pass
            # Re-assert after Tk settles — child canvas resize can nudge the HWND.
            if sys.platform.startswith("win"):
                try:
                    hwnd = self._overlay_hwnd()
                    if hwnd:
                        ctypes.windll.user32.SetWindowPos(
                            hwnd,
                            0,
                            int(x),
                            int(y),
                            int(width),
                            int(height),
                            SWP_NOZORDER | SWP_NOACTIVATE,
                        )
                except (AttributeError, OSError, tk.TclError):
                    pass
            try:
                self.root.geometry(f"{width}x{height}+{x}+{y}")
            except tk.TclError:
                pass
            self._exclude_from_capture()

    def _apply_window_shape(self):
        """Minecraft keeps a round region; dictation uses color-key only.

        Dictation used to SetWindowRgn(panel)+color-key at the same time, which
        stacked a transparent silhouette on top of the black chat panel.
        """
        if not sys.platform.startswith("win"):
            return
        try:
            hwnd = self._overlay_hwnd()
            gdi = ctypes.windll.gdi32
            if self.mode == "minecraft":
                region = gdi.CreateRoundRectRgn(0, 0, self.width + 1, self.height + 1, 44, 44)
                ctypes.windll.user32.SetWindowRgn(hwnd, region, True)
            else:
                # Clear any leftover region mask from older builds / mode switches.
                ctypes.windll.user32.SetWindowRgn(hwnd, 0, True)
            self._apply_transparency()
        except (AttributeError, OSError, tk.TclError):
            pass

    def _paint_frame(self):
        """One immediate canvas paint (also used after resize to avoid black flash)."""
        try:
            self.canvas.delete("all")
        except tk.TclError:
            return
        if self.mode == "minecraft":
            self._round_rect(0, 0, self.width, self.height, 22, fill=self.panel_bg, outline="")
            self._draw_minecraft_panel()
            return
        self._draw_compose()
        pill_w, pill_h = self.pill_size
        origin_x, origin_y = self._pill_origin()
        self._round_rect(
            origin_x, origin_y, origin_x + pill_w, origin_y + pill_h, pill_h / 2,
            fill=self.panel_bg, outline="",
        )
        self._draw_dictation(origin_x, origin_y, pill_w, pill_h)

    def _animate(self):
        if not self._visible:
            return
        self._force_topmost()
        self.pulse = (self.pulse + 0.13) % (math.pi * 2)
        # Nothing is feeding the meter (transcribing, model loading, stopped):
        # let the waveform drain out instead of freezing mid-word.
        if self._last_level_at and time.monotonic() - self._last_level_at > 0.25:
            self.levels.append(0.0)
            self.level = 0.0
        self._paint_frame()
        self._after = self.root.after(33, self._animate)

    # ---------------------------------------------------------------- composer

    def _target_style(self):
        return {
            "cursor": ("TO CURSOR", self.muted),
            "clipboard": ("TO CLIPBOARD", "#7aa2f7"),
            "agent": ("TO AGENT", self.accent),
        }.get(self.compose_target, ("TO CURSOR", self.muted))

    def _compose_build(self):
        """Lay the panel out once per content change; frames just replay the ops."""
        signature = self._compose_signature()
        if self._compose_cache[0] == signature:
            return self._compose_cache[1], self._compose_cache[2]

        pad = 16
        width = self.compose_width
        right = width - pad
        # Agent history stays in memory when the target changes, but it is only
        # painted in agent mode. TO CURSOR gets a clean transcription surface.
        chat = self.compose_target == "agent"
        dim = lambda color, amount: self.blend_color(self.panel_bg, color, amount)
        agent_bg = dim(self.text, 0.10)
        user_bg = dim(self.accent, 0.22)
        tool_bg = dim(self.accent, 0.12)
        max_bubble = width - pad * 2 - 40

        blocks = []
        if chat:
            for role, text in self.compose_history:
                if role == "tool":
                    blocks.append(
                        self._bubble_block("tool", text, self.font_tool, dim(self.accent, 0.85), tool_bg, 3, limit=2, prefix="› ")
                    )
                elif role == "assistant":
                    blocks.append(
                        self._bubble_block("assistant", text, self.font_body, self.text, agent_bg, 6, limit=6)
                    )
                else:
                    blocks.append(
                        self._bubble_block("user", text, self.font_body, self.text, user_bg, 6, limit=4)
                    )
            if self.compose_text:
                spoken = self.muted if (self.compose_reply or self.compose_error) else self.text
                blocks.append(
                    self._bubble_block("user", self.compose_text, self.font_compose, spoken, user_bg, 6, limit=None)
                )
            for tool in self.compose_tools:
                blocks.append(
                    self._bubble_block("tool", tool, self.font_tool, dim(self.accent, 0.9), tool_bg, 3, limit=2, prefix="› ")
                )
            if self.compose_reply or self.compose_error:
                body = self.compose_error or self.compose_reply
                fill = self.danger if self.compose_error else self.text
                blocks.append(
                    self._bubble_block("assistant", body, self.font_reply, fill, agent_bg, 6, limit=9)
                )
        else:
            if self.compose_text:
                spoken = self.muted if (self.compose_reply or self.compose_error) else self.text
                blocks.append(
                    self._bubble_block("left", self.compose_text, self.font_compose, spoken, agent_bg, 6, limit=None)
                )
            # With no preserved agent conversation these can be ordinary cursor
            # status/error rows. Once agent history exists, never leak its tool
            # or reply bubbles into TO CURSOR.
            if not self.compose_history:
                for tool in self.compose_tools:
                    blocks.append(
                        self._bubble_block("tool", tool, self.font_tool, dim(self.accent, 0.9), tool_bg, 3, limit=2, prefix="› ")
                    )
                if self.compose_reply or self.compose_error:
                    body = self.compose_error or self.compose_reply
                    fill = self.danger if self.compose_error else self.text
                    blocks.append(
                        self._bubble_block("left", body, self.font_reply, fill, agent_bg, 6, limit=9)
                    )

        # Clamp bubble text widths now that we know the panel.
        for block in blocks:
            if block.get("kind") != "bubble":
                continue
            lines = self._wrap_lines(block["raw"], block["font"], min(max_bubble, block["max_w"]), block["limit"])
            block["lines"] = lines
            text_w = max((block["font"].measure(line) for line in lines), default=40)
            block["bubble_w"] = min(max_bubble, max(48, text_w + 20))
            block["height"] = len(lines) * block["line_height"] + 14 + block["gap_after"]

        if not blocks:
            # Nothing to say yet — no empty box floating over the pill.
            self._compose_cache = (signature, [], 0)
            return [], 0

        header_h = 40
        budget = self._compose_height_limit() - header_h - pad
        while len(blocks) > 1 and sum(block["height"] for block in blocks) > budget:
            blocks.pop(0)

        label, color = self._target_style()
        ops = [("chip", pad, 12, label, color)]
        if self.compose_note:
            ops.append(
                ("text", right, 21, self.compose_note[:38], dim(self.muted, 0.75), self.font_tool, "e")
            )
        y = header_h
        for block in blocks:
            y += 2
            if block["side"] == "right":
                x = width - pad - block["bubble_w"]
            else:
                x = pad
            ops.append(
                (
                    "bubble",
                    x,
                    y,
                    block["bubble_w"],
                    block["height"] - block["gap_after"],
                    block["bg"],
                    block["lines"],
                    block["color"],
                    block["font"],
                    block["line_height"],
                )
            )
            y += block["height"]

        height = int(y + pad - 2)
        self._compose_cache = (signature, ops, height)
        return ops, height

    def _compose_height_limit(self):
        """Maximum upward growth that keeps the fixed mic pill on-screen."""
        try:
            screen_height = int(self.root.winfo_screenheight())
        except (tk.TclError, TypeError, ValueError):
            screen_height = 1080
        if self._pill_anchor is not None:
            pill_bottom = min(float(self._pill_anchor[1]), float(screen_height - TASKBAR_PAD))
        else:
            pill_bottom = float(screen_height - TASKBAR_PAD)
        available = int(pill_bottom - self.pill_size[1] - self.compose_gap - 8)
        return max(180, min(COMPOSE_MAX_HEIGHT, available))

    def _bubble_block(self, side, text, font, color, bg, gap_after, limit=4, prefix=""):
        raw = prefix + str(text or "")
        # Provisional width; finalized in _compose_build against the panel.
        max_w = self.compose_width - 72
        lines = self._wrap_lines(raw, font, max_w, limit)
        line_height = font.metrics("linespace") + 3
        text_w = max((font.measure(line) for line in lines), default=40)
        align = "right" if side == "user" else "left"
        return {
            "kind": "bubble",
            "side": align,
            "raw": raw,
            "lines": lines,
            "font": font,
            "color": color,
            "bg": bg,
            "line_height": line_height,
            "gap_after": gap_after,
            "limit": limit,
            "max_w": max_w,
            "bubble_w": min(max_w, max(48, text_w + 20)),
            "height": len(lines) * line_height + 14 + gap_after,
        }

    def _chat_block(self, label, text, font, color, gap_after, limit=4, prefix="", gap_before=0):
        # Kept for any older callers; new chat path uses _bubble_block.
        width = self.compose_width - 36
        lines = self._wrap_lines(prefix + str(text), font, width, limit)
        line_height = font.metrics("linespace") + 4
        return {
            "label": label,
            "lines": lines,
            "font": font,
            "color": color,
            "line_height": line_height,
            "gap_after": gap_after,
            "gap_before": gap_before,
            "height": len(lines) * line_height + gap_after + gap_before,
        }

    def _draw_compose(self):
        panel_h = self._panel_height()
        if not panel_h:
            return
        ops, _height = self._compose_build()
        self._round_rect(0, 0, self.width, panel_h, 16, fill=self.panel_bg, outline="")
        for op in ops:
            kind = op[0]
            if kind == "text":
                _, x, y, text, fill, font, anchor = op
                self.canvas.create_text(x, y, text=text, fill=fill, font=font, anchor=anchor)
            elif kind == "line":
                _, x1, y, x2, fill = op
                self.canvas.create_line(x1, y, x2, y, fill=fill)
            elif kind == "bubble":
                _, x, y, bw, bh, bg, lines, fill, font, line_height = op
                self._round_rect(x, y, x + bw, y + bh, 12, fill=bg, outline="")
                ty = y + 7
                for line in lines:
                    self.canvas.create_text(x + 10, ty, text=line, fill=fill, font=font, anchor="nw")
                    ty += line_height
            elif kind == "chip":
                _, x, y, text, color = op
                text_width = self.font_chip.measure(text)
                self._round_rect(
                    x, y, x + text_width + 20, y + 19, 9,
                    fill=self.blend_color(self.panel_bg, color, 0.18), outline="",
                )
                self.canvas.create_oval(x + 9, y + 8, x + 13, y + 12, fill=color, outline="")
                self.canvas.create_text(
                    x + 18, y + 10, text=text, fill=color, font=self.font_chip, anchor="w",
                )

    def _status_kind(self):
        status = str(self.status_text or "").lower()
        if any(word in status for word in ("error", "failed", "no microphone", "unavailable")):
            return "error"
        if any(word in status for word in ("transcribing", "loading", "thinking")):
            return "busy"
        return "live"

    def _draw_dictation(self, origin_x, origin_y, pill_w, pill_h):
        kind = self._status_kind()
        # The mic keeps running while a chunk transcribes, so trust the meter:
        # as long as audio is flowing the waveform stays, no flicker mid-sentence.
        mic_live = bool(self._last_level_at) and time.monotonic() - self._last_level_at < 0.4
        color = {"error": self.danger, "busy": self.muted, "live": self.accent}[kind]
        if kind == "busy" and mic_live:
            color = self.accent
        if self.compose_target != "cursor" and kind == "live":
            color = self._target_style()[1]
        # Audio being present recently is not enough to call the microphone
        # live: after release those final samples remain in the deque. The
        # explicit capture state makes every idle/transcribing waveform grey.
        if not self.recording and kind != "error":
            color = self.muted
        cy = origin_y + pill_h / 2
        cx = origin_x + 21.0
        glow = 0.5 + 0.5 * math.sin(self.pulse * (2.2 if kind == "busy" else 1.0))

        # Status dot with a soft halo that breathes, and swells with the voice.
        halo = 5.0 + 3.0 * glow + 5.0 * self.level
        self.canvas.create_oval(
            cx - halo, cy - halo, cx + halo, cy + halo,
            fill=self.blend_color(self.panel_bg, color, 0.07 + 0.08 * glow), outline="",
        )
        dot = 3.2
        self.canvas.create_oval(
            cx - dot, cy - dot, cx + dot, cy + dot,
            fill=color if kind != "busy" else self.blend_color(self.panel_bg, color, 0.35 + 0.55 * glow),
            outline="",
        )

        # Always show the waveform for normal dictation — never flash the word
        # "listening" while the first mic samples arrive.
        if kind != "error":
            self._draw_wave(origin_x + 34.0, origin_x + pill_w - 18.0, cy, color, pill_h)
            return
        label = str(self.status_text or "").strip()
        if label.endswith("..."):
            label = label[:-3] + "…"
        self.canvas.create_text(
            origin_x + 36, cy + 1, text=label[:26], fill=self.muted,
            font=self.font_pill, anchor="w",
        )

    def _draw_wave(self, x0, x1, cy, color, pill_h):
        """Draw the real mic envelope: one bar per captured sample, newest right."""
        span = max(0.0, x1 - x0)
        count = max(1, min(self.bar_count, int((span + self.bar_pitch - self.bar_width) // self.bar_pitch)))
        values = list(self.levels)[-count:]
        if len(values) < count:
            values = [0.0] * (count - len(values)) + values
        max_half = pill_h * 0.30
        # Right-align the bars so the newest sample always sits at the same edge.
        start = x1 - (count - 1) * self.bar_pitch - self.bar_width / 2
        for index, value in enumerate(values):
            x = start + index * self.bar_pitch
            half = max(1.0, value * max_half)
            fade = 0.45 + 0.55 * ((index + 1) / count)
            amount = round(min(1.0, (0.30 + 0.70 * value) * fade), 2)
            self.canvas.create_line(
                x, cy - half, x, cy + half,
                fill=self.blend_color(self.surface2, color, amount),
                width=self.bar_width, capstyle=tk.ROUND,
            )

    def _wrap_lines(self, text, font, avail, max_lines):
        words = str(text or "").split()
        if not words:
            return []
        lines, current, truncated = [], words[0], False
        for word in words[1:]:
            trial = current + " " + word
            if font.measure(trial) <= avail:
                current = trial
            else:
                lines.append(current)
                current = word
                if len(lines) == max_lines:
                    truncated = True  # words are still left over
                    break
        if not truncated:
            lines.append(current)
        if truncated or font.measure(lines[-1]) > avail:
            tail = lines[-1]
            while tail and font.measure(tail + "…") > avail:
                tail = tail[:-1]
            lines[-1] = tail.rstrip() + "…"
        return lines

    def _mc_build(self):
        pad = 18
        avail = self.width - pad * 2
        right = self.width - pad
        ops = []
        # Header: status dot + title, with the live status to the right.
        ops.append(("dot", pad + 4, 23, 4, self.accent))
        ops.append(("text", pad + 16, 14, "MC AI", self.text, self.font_title, "nw"))
        status = str(self.status_text or "").strip()[:18]
        if status:
            ops.append(("text", right, 18, status, self.muted, self.font_status, "ne"))
        y = 44
        ops.append(("line", pad, y, right, y, self.surface2))
        y += 14

        reply = (self.mc_reply or "").strip()
        has_reply = bool(reply) and reply.lower() != "ready."
        if has_reply:
            for line in self._wrap_lines(reply, self.font_body, avail, 3):
                ops.append(("text", pad, y, line, self.text, self.font_body, "nw"))
                y += 17
            y += 8

        if self.mc_items:
            ops.append(("text", pad, y, (self.mc_goal or "Shopping list")[:36], self.text, self.font_goal, "nw"))
            y += 24
            for item in self.mc_items[:6]:
                label = str(item.get("label") or item.get("item") or item.get("group") or "Item")
                have = int(item.get("have") or 0)
                needed = max(1, int(item.get("needed") or 1))
                done = have >= needed
                color = self.success if done else self.text
                ratio = max(0.0, min(1.0, have / needed))
                ops.append(("text", pad, y, label[:24], color, self.font_body, "nw"))
                ops.append(("text", right, y, f"{min(have, needed)}/{needed}", color, self.font_count, "ne"))
                ops.append(("bar", pad, y + 18, avail, ratio, color))
                y += 30
            y -= 4
        elif not has_reply:
            ops.append(("text", pad, y, "Look at Minecraft and ask:", self.muted, self.font_status, "nw"))
            y += 16
            ops.append(("text", pad, y, "“what do I need for a piston?”", self.muted, self.font_status, "nw"))
            y += 16

        return ops, y + pad

    def _draw_minecraft_panel(self):
        ops, _ = self._mc_build()
        for op in ops:
            kind = op[0]
            if kind == "text":
                _, x, y, text, fill, font, anchor = op
                self.canvas.create_text(x, y, text=text, fill=fill, font=font, anchor=anchor)
            elif kind == "dot":
                _, cx, cy, r, fill = op
                self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=fill, outline="")
            elif kind == "line":
                _, x1, y1, x2, y2, fill = op
                self.canvas.create_line(x1, y1, x2, y2, fill=fill)
            elif kind == "bar":
                _, x, y, width, ratio, fill = op
                self._round_rect(x, y, x + width, y + 5, 2, fill=self.surface2, outline="")
                if ratio > 0:
                    self._round_rect(x, y, x + max(4, width * ratio), y + 5, 2, fill=fill, outline="")

    def _round_rect(self, x1, y1, x2, y2, r, **kwargs):
        points = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return self.canvas.create_polygon(points, smooth=True, splinesteps=24, **kwargs)

    def blend_color(self, background, foreground, amount):
        amount = max(0.0, min(1.0, float(amount)))
        key = (background, foreground, round(amount, 3))
        cached = self._blend_cache.get(key)
        if cached is not None:
            return cached
        try:
            br, bg, bb = self.root.winfo_rgb(background)
            fr, fg, fb = self.root.winfo_rgb(foreground)
        except tk.TclError:
            return foreground
        values = []
        for base, top in ((br, fr), (bg, fg), (bb, fb)):
            values.append(min(255, int((base + (top - base) * amount) / 256)))
        color = f"#{values[0]:02x}{values[1]:02x}{values[2]:02x}"
        self._blend_cache[key] = color
        return color


class Dictation:
    def __init__(self):
        self.active = False
        self.settings = load_voice_dictation_settings()
        self.audio_queue: "queue.Queue[np.ndarray]" = queue.Queue()
        self.transcribe_queue: "queue.Queue[np.ndarray | None]" = queue.Queue()
        self.stream = None
        self._chunk_parts: list = []
        self._chunk_samples = 0
        self.last_voice_at = 0.0
        self.model = None
        self.model_lock = threading.Lock()
        self._loaded_model_name = None
        self._loaded_device = None
        self._session_language = None
        # Language guesses that were not confident enough to lock on their own.
        self._language_votes = {}
        # Per-chunk no-speech probabilities for the turn being assembled.
        self._chunk_no_speech: list = []
        self.overlay = MicOverlay()
        self.active_mode = "dictation"
        self.minecraft_assistant = MinecraftVoiceAssistant() if MinecraftVoiceAssistant else None
        self.minecraft_chat_bridge = None
        if start_minecraft_chat_bridge is not None:
            try:
                self.minecraft_chat_bridge = start_minecraft_chat_bridge()
                log_event("minecraft chat bridge started")
            except Exception as exc:
                log_event(f"minecraft chat bridge failed: {exc}")
        self.worker_thread = None
        self.transcribe_thread = None
        self.stop_event = threading.Event()
        self._pipeline_quit = threading.Event()
        self._pipeline_lock = threading.Lock()
        self.ui_queue: "queue.Queue[callable]" = queue.Queue()
        self.ptt_down_at = 0.0
        self.ptt_hold_seconds = 0.6
        # Transcript is collected while you speak and only leaves on release,
        # to whichever target the macro keyboard last selected.
        self.target = "cursor"
        self._target_set_at = 0.0
        self.transcript_parts: list = []
        self._transcribing = False
        # True from start() until capture has flushed leftover audio into Whisper.
        # Prevents early release from dispatching an empty transcript and hiding.
        self._session_flushing = False
        self._dispatch_pending = False
        self._dispatch_after = None
        self._linger_after = None
        self._agent_hide_after = None
        self._turn_id = 0
        # Bumped when cursor/clipboard dismisses so late UI callbacks can't
        # resurrect the overlay (set_transcript/show race after hide).
        self._overlay_gen = 0
        self.agent = None
        self.agent_tts = None
        self.overlay.root.after(16, self._pump_ui)
        self.overlay.root.after(450, self._watch_minecraft_overlay)

    def _pump_ui(self):
        try:
            while True:
                fn = self.ui_queue.get_nowait()
                try:
                    fn()
                except Exception as exc:
                    log_event(f"ui callback failed: {exc}")
        except queue.Empty:
            pass
        self.overlay.root.after(16, self._pump_ui)

    def ui(self, fn, *args, **kwargs):
        self.ui_queue.put(lambda: fn(*args, **kwargs))

    def _watch_minecraft_overlay(self):
        try:
            if not self.active and is_minecraft_foreground():
                if self.minecraft_assistant is not None:
                    self.overlay.set_minecraft_state(self.minecraft_assistant.state("ready"))
                else:
                    self.overlay.set_mode("minecraft")
                    self.overlay.set_status("mc ai unavailable")
                self.overlay.show()
                self.overlay.set_status("ready")
            elif not self.active and self.overlay.mode == "minecraft":
                self.overlay.hide()
                self.overlay.set_mode("dictation")
        except Exception as exc:
            log_event(f"minecraft overlay watcher failed: {exc}")
        self.overlay.root.after(450, self._watch_minecraft_overlay)

    def _show_start_failure(self, message):
        self.overlay.reset_levels()
        self.overlay.set_recording(False)
        self.overlay.set_status(message)
        self.overlay.root.after(1800, lambda: None if self.active else self.overlay.hide())

    def _set_status_if_active(self, message):
        if self.active:
            self.overlay.set_status(message)

    def reload_settings(self):
        self.settings = load_voice_dictation_settings()
        env_model = os.environ.get("VOICE_WHISPER_MODEL", "").strip()
        env_device = os.environ.get("VOICE_WHISPER_DEVICE", "").strip()
        env_compute = os.environ.get("VOICE_WHISPER_COMPUTE", "").strip()
        if env_model:
            self.settings["whisper_model"] = env_model
        if env_device:
            self.settings["device"] = env_device
        if env_compute:
            self.settings["compute_type"] = env_compute
        model_name = self.settings["whisper_model"]
        device = self._whisper_device()
        if self.model is not None and (
            getattr(self, "_loaded_model_name", None) != model_name
            or getattr(self, "_loaded_device", None) != device
        ):
            self.model = None
        try:
            self.ui(self.overlay.apply_opacity_settings, self.settings)
        except Exception:
            pass
        self._configure_agent_tts()

    def _configure_agent_tts(self):
        enabled = bool(self.settings.get("agent_tts_enabled", True))
        voice = str(self.settings.get("agent_tts_voice") or "nova")
        try:
            from voice_agent import agent_settings
            api_key = str(agent_settings().get("api_key") or "").strip()
        except Exception:
            api_key = ""
        if self.agent_tts is not None:
            try:
                self.agent_tts.set_voice(voice)
                self.agent_tts.set_api_key(api_key)
                self.agent_tts.enable(enabled)
            except Exception as exc:
                log_event(f"agent TTS configuration failed: {exc}")
            return self.agent_tts
        if not enabled:
            return None
        try:
            clicker_path = str(Path(__file__).resolve().parent / "agent_clicker")
            if clicker_path not in sys.path:
                sys.path.insert(0, clicker_path)
            from desktop_agent.tts import TTSPlayer
            self.agent_tts = TTSPlayer(
                voice=voice,
                api_key=api_key,
                max_chars=4000,
                on_log=lambda _level, message: log_event(f"agent TTS: {message}"),
            )
            self.agent_tts.enable(True)
        except Exception as exc:
            log_event(f"agent TTS unavailable: {exc}")
            self.agent_tts = None
        return self.agent_tts

    def _speak_agent_reply(self, text):
        """Say something out loud outside the normal streaming path (timers)."""
        player = self._configure_agent_tts()
        if player is None:
            return
        try:
            player.speak(clean_reply(text))
        except Exception as exc:
            log_event(f"agent TTS enqueue failed: {exc}")

    def _stop_speaking(self, reason=""):
        """Cut agent speech instantly — the user is talking over it.

        Without this the assistant kept narrating into an open microphone, so
        its own voice ended up in the next transcript.
        """
        if not self.settings.get("barge_in", True):
            return
        player = self.agent_tts
        if player is None:
            return
        try:
            player.clear()
            log_event(f"barge-in: agent speech stopped{f' ({reason})' if reason else ''}")
        except Exception as exc:
            log_event(f"barge-in failed: {exc}")

    def _whisper_device(self):
        device = str(self.settings.get("device") or "cpu").strip().lower()
        return device if device in {"cpu", "cuda", "auto"} else "cpu"

    def _whisper_compute_type(self):
        compute_type = str(self.settings.get("compute_type") or "int8").strip().lower()
        if compute_type not in {"int8", "float16", "float32"}:
            compute_type = "int8"
        if self._whisper_device() == "cpu" and compute_type == "float16":
            compute_type = "int8"
        return compute_type

    def _transcribe_kwargs(self):
        language_setting = str(self.settings.get("language", "auto") or "auto").strip().lower()
        language = resolve_transcribe_language(language_setting)
        kwargs = {
            "vad_filter": True,
            "vad_parameters": {"min_silence_duration_ms": 200},
            "beam_size": 1,
            "best_of": 1,
            "temperature": 0,
            "no_speech_threshold": 0.55,
            "condition_on_previous_text": False,
            "without_timestamps": True,
        }
        base_prompt = ""
        if language:
            kwargs["language"] = language
        elif self._session_language:
            # Reuse language detected earlier in this PTT session — skips detect cost.
            kwargs["language"] = self._session_language
        else:
            kwargs["multilingual"] = True
            kwargs["language_detection_segments"] = 2
            base_prompt = (
                "Transcribe mixed Swedish and English exactly as spoken. "
                "Keep Swedish words in Swedish and English words in English."
            )
        # Bias the decoder toward the names it has never heard — aiOS, OPERATOR,
        # whatever else the user added in settings.
        prompt = build_initial_prompt(self.settings.get("vocabulary"), base_prompt)
        if prompt:
            kwargs["initial_prompt"] = prompt
        return kwargs

    def ensure_model(self):
        with self.model_lock:
            if self.model is not None:
                return
            self.reload_settings()
            model_name = self.settings["whisper_model"]
            device = self._whisper_device()
            compute_type = self._whisper_compute_type()
            self.ui(self.overlay.set_status, "loading model...")
            log_event(f"loading whisper model={model_name} device={device} compute={compute_type}")
            load_whisper_dependency()
            self.model = WhisperModel(model_name, device=device, compute_type=compute_type)
            self._loaded_model_name = model_name
            self._loaded_device = device
            log_event("whisper model ready")
            self.ui(self._set_status_if_active, "")

    def warmup(self):
        """Warm agent client + Whisper so the first real turn isn't cold."""
        # Agent first — cheap, and typed GUI chat can fire before Whisper is ready.
        try:
            import socket as _socket

            agent = self._ensure_agent()
            agent.warmup()
            try:
                _socket.getaddrinfo("api.openai.com", 443)
            except OSError:
                pass
            log_event("agent client warmup complete")
        except Exception as exc:
            log_event(f"agent warmup skipped: {exc}")
        self.ensure_model()
        self._ensure_pipeline()
        try:
            silence = np.zeros(int(SAMPLE_RATE * 0.4), dtype=np.float32)
            segments, _info = self.model.transcribe(
                silence,
                language="en",
                beam_size=1,
                best_of=1,
                temperature=0,
                vad_filter=False,
                without_timestamps=True,
            )
            # Force engine work even if VAD would skip.
            _ = "".join(seg.text for seg in segments)
            log_event("whisper warmup complete")
        except Exception as exc:
            log_event(f"whisper warmup skipped: {exc}")

    def _audio_callback(self, indata, frames, time_info, status):
        if status:
            pass
        if not self.active:
            return
        chunk = indata[:, 0].astype(np.float32, copy=True)
        self.audio_queue.put(chunk)
        levels = self._envelope(chunk)
        if levels:
            self.ui(self.overlay.push_levels, levels)

    @staticmethod
    def _envelope(chunk, slices=LEVEL_SLICES):
        """Split a mic block into short windows and return their loudness in 0..1.

        Uses dBFS so the waveform tracks what the ear hears; the linear RMS of
        speech barely leaves the bottom of the scale.
        """
        usable = (chunk.shape[0] // slices) * slices
        if usable <= 0:
            return []
        blocks = chunk[:usable].reshape(slices, -1)
        rms = np.sqrt(np.mean(blocks * blocks, axis=1) + 1e-12)
        db = 20.0 * np.log10(rms + 1e-12)
        norm = (db - LEVEL_FLOOR_DB) / (LEVEL_CEIL_DB - LEVEL_FLOOR_DB)
        return np.clip(norm, 0.0, 1.0).astype(np.float32).tolist()

    def _input_device(self):
        # An explicitly chosen mic wins: plugging in a headset should not
        # silently move dictation to whatever Windows just made default.
        wanted = str(self.settings.get("input_device") or "").strip()
        if wanted:
            try:
                devices = list(sd.query_devices())
            except Exception as exc:
                log_event(f"device list failed while resolving {wanted!r}: {exc}")
                devices = []
            needle = wanted.casefold()
            for index, info in enumerate(devices):
                try:
                    if int(info.get("max_input_channels", 0)) <= 0:
                        continue
                except (TypeError, ValueError):
                    continue
                if needle in str(info.get("name") or "").casefold():
                    log_event(f"using configured microphone {index}: {info.get('name')}")
                    return index
            log_event(f"configured microphone {wanted!r} not found; falling back to default")

        try:
            default = sd.default.device
            default_input = default[0] if isinstance(default, (list, tuple)) else default
            if isinstance(default_input, int) and default_input >= 0:
                info = sd.query_devices(default_input)
                if int(info.get("max_input_channels", 0)) > 0:
                    return default_input
        except Exception as exc:
            log_event(f"default microphone lookup failed: {exc}")

        try:
            devices = list(sd.query_devices())
        except Exception as exc:
            raise RuntimeError(f"could not list audio devices: {exc}") from exc

        for index, info in enumerate(devices):
            try:
                if int(info.get("max_input_channels", 0)) > 0:
                    name = str(info.get("name") or f"device {index}")
                    log_event(f"using fallback microphone device {index}: {name}")
                    return index
            except (TypeError, ValueError):
                continue
        raise RuntimeError("no microphone input device found")

    def toggle(self):
        stderr_write(f"[toggle] active={self.active}\n")
        if self.active:
            self.stop()
        else:
            self.start()

    def ptt_down(self):
        """Macro Deck / hotkey press: start, or stop if already running past hold threshold."""
        now = time.monotonic()
        if self.active:
            elapsed = now - self.ptt_down_at
            if elapsed >= self.ptt_hold_seconds:
                log_event(f"ptt_down stop after {elapsed:.3f}s")
                self.stop()
            else:
                log_event(f"ptt_down ignored echo at {elapsed:.3f}s")
            return
        self.ptt_down_at = now
        log_event("ptt_down start")
        self.start()

    def ptt_up(self):
        """Macro Deck / hotkey release: stop only when held ≥ threshold; else stay in toggle mode."""
        if not self.active:
            return
        elapsed = time.monotonic() - self.ptt_down_at
        if elapsed >= self.ptt_hold_seconds:
            log_event(f"ptt_up hold-stop after {elapsed:.3f}s")
            self.stop()
        else:
            log_event(f"ptt_up toggle-stay after {elapsed:.3f}s")

    def _agent_session_active(self):
        """True only while the agent overlay chat is visibly open.

        Hold-to-talk then keeps targeting agent with no extra button press.
        Once the overlay closes, the next hold needs the agent button again.
        """
        ov = self.overlay
        if not getattr(ov, "_visible", False):
            return False
        if getattr(ov, "compose_target", "") != "agent":
            return False
        return bool(ov.compose_open)

    def _clear_agent_sticky(self):
        """Overlay went away — next dictation defaults to cursor again."""
        if self.target == "agent":
            self.target = "cursor"
            self._target_set_at = 0.0
            log_event("agent sticky cleared (overlay closed)")

    def start(self):
        if self.active:
            return
        # Kill any reply still being spoken before the mic opens, so the
        # assistant's own voice cannot land in this turn's audio.
        self._stop_speaking("new turn")
        self.active = True
        self._session_flushing = True
        if self.ptt_down_at <= 0:
            self.ptt_down_at = time.monotonic()
        self.active_mode = "minecraft" if is_minecraft_foreground() else "dictation"
        self.stop_event.clear()
        self._drain_queue(self.audio_queue)
        self._reset_buffer()
        self._session_language = None
        self._language_votes = {}
        self._chunk_no_speech = []
        self.last_voice_at = time.monotonic()
        self._cancel_linger()
        self._cancel_dispatch()
        self.transcript_parts = []
        self._turn_id += 1
        continuing_agent = self._agent_session_active()
        # A target picked just before the key went down still counts, so tapping
        # the agent button first and then talking works as well as the reverse.
        # If the agent chat is still on screen, keep routing there instead of
        # wiping the overlay and falling back to cursor.
        if continuing_agent:
            self.target = "agent"
            self._target_set_at = time.monotonic()
        elif time.monotonic() - self._target_set_at > 5.0:
            self.target = "cursor"
        # Show overlay immediately — settings reload can wait in the worker.
        self.overlay.reset_levels()
        self.overlay.set_recording(True)
        keep_panel = continuing_agent or (
            getattr(self.overlay, "_visible", False) and self.overlay.has_compose_content()
        )
        if keep_panel:
            # Keep the existing transcript/chat — never flash-clear mid-session.
            self.overlay.set_target(self.target)
            if continuing_agent:
                self.overlay.prepare_listen()
            else:
                self.overlay.set_compose_note("")
                self.overlay.open_compose()
        else:
            self.overlay.clear_compose()
            self.overlay.set_target(self.target)
        self.overlay.set_mode(self.active_mode)
        # Status stays "live" visually via the waveform — avoid a "listening" text flash.
        self.overlay.set_status("")
        self.overlay.show()
        log_event("dictation start requested" + (" (continue agent)" if continuing_agent else ""))
        try:
            self.reload_settings()
            self._ensure_pipeline()
        except Exception as exc:
            log_event(f"microphone stream failed: {exc}")
            self.active = False
            self.overlay.set_recording(False)
            self._session_flushing = False
            self.stop_event.set()
            message = "no microphone" if "no microphone" in str(exc).lower() else "mic failed"
            self.ui(self._show_start_failure, message)

    def stop(self):
        if not self.active:
            return
        log_event("dictation stop requested")
        self.active = False
        self.stop_event.set()
        self.overlay.set_recording(False)
        # Keep the overlay up and show progress until Whisper finishes, even if
        # the capture thread has not flushed the final buffer yet.
        self.ui(self.overlay.set_status, "transcribing...")
        if self.active_mode != "minecraft":
            self._dispatch_pending = True
            self.ui(self._wait_for_transcript, time.monotonic())

    # ------------------------------------------------------------------ routing

    def _agent_overlay_open(self):
        """True while the agent chat panel is still on screen (sticky session)."""
        ov = self.overlay
        if not getattr(ov, "_visible", False):
            return False
        if getattr(ov, "compose_target", "") != "agent":
            return False
        if not getattr(ov, "compose_open", False):
            return False
        if self.active or self._dispatch_pending or self.target == "agent":
            return True
        return bool(ov.has_compose_content())

    def set_target(self, target):
        """Macro keyboard picked a destination — usually mid-hold.

        The overlay stays up and an in-flight transcription still finishes and
        sends. Leaving agent mode hides its preserved conversation immediately;
        switching back restores it.
        """
        target = TARGET_ALIASES.get(str(target or "").strip().lower())
        if not target:
            return
        # Clipboard while in agent = back to default cursor routing (not copy).
        if target == "clipboard" and (
            self.target == "agent"
            or getattr(self.overlay, "compose_target", "") == "agent"
            or self._agent_overlay_open()
        ):
            target = "cursor"
        if target == self.target and getattr(self.overlay, "compose_target", "") == target:
            return
        previous = self.target
        self.target = target
        self._target_set_at = time.monotonic()
        if target != "agent":
            self._clear_agent_sticky()
        # Don't let an old linger timer hide the panel while the user is
        # flipping destinations — keep whatever is already on screen.
        self._cancel_linger()
        log_event(f"target -> {target}" + (f" (was {previous})" if previous != target else ""))

        def apply():
            self.overlay.set_target(target)
            # Only expand the compose panel when there is real content to show.
            # Forcing open_compose() here produced the empty black slab.
            if (
                str(getattr(self.overlay, "compose_text", "") or "").strip()
                or getattr(self.overlay, "compose_history", None)
                or str(getattr(self.overlay, "compose_reply", "") or "").strip()
            ):
                self.overlay.open_compose()
            if self.active:
                self.overlay.set_status("")
            self.overlay.show()

        self.ui(apply)
        # If the key already came up and we have text waiting, re-route it now.
        if (
            not self.active
            and not self._dispatch_pending
            and self.transcript_parts
            and target != previous
        ):
            self.ui(self._dispatch_transcript)

    def cancel(self):
        log_event("dictation cancelled")
        self._stop_speaking("cancel")
        self.active = False
        self._session_flushing = False
        self.stop_event.set()
        self.overlay.set_recording(False)
        self._dispatch_pending = False
        self.transcript_parts = []
        self._chunk_no_speech = []
        self._cancel_dispatch()
        self.ui(self._dismiss_overlay)

    def stop_agent(self):
        """Abort whatever the agent is doing and shut it up.

        Reachable from the macro keyboard and the aiOS window, so a runaway
        answer or a long OPERATOR job is always one button away from stopping.
        """
        self._stop_speaking("stop_agent")
        if self.agent is None:
            log_event("stop_agent: no agent running")
            return
        try:
            self.agent.cancel()
        except Exception as exc:
            log_event(f"stop_agent failed: {exc}")
            return
        self.ui(self.overlay.set_compose_note, "stopped")
        self.ui(self.overlay.set_status, "ready")

    def _cancel_dispatch(self):
        self._dispatch_pending = False
        if self._dispatch_after:
            try:
                self.overlay.root.after_cancel(self._dispatch_after)
            except tk.TclError:
                pass
            self._dispatch_after = None

    def _cancel_linger(self):
        if self._linger_after:
            try:
                self.overlay.root.after_cancel(self._linger_after)
            except tk.TclError:
                pass
            self._linger_after = None

    def _linger(self, milliseconds):
        """Keep the panel up for a beat after a turn, then fade the whole overlay."""
        self._cancel_linger()

        def done():
            self._linger_after = None
            if self.active or self._dispatch_pending:
                # Still listening / finishing Whisper — never tear down early.
                return
            self.overlay.clear_compose()
            self.overlay.hide()
            # Closed = no longer sticky; next hold needs the agent button again.
            self._clear_agent_sticky()

        self._linger_after = self.overlay.root.after(milliseconds, done)

    def _wait_for_transcript(self, started_at):
        """Hold the send until capture flush + Whisper are done."""
        if not self._dispatch_pending:
            return
        # Keep the status visible while we wait — early release used to race past
        # the capture flush and hide before any text arrived.
        if self._session_flushing or self._transcribing or not self.transcribe_queue.empty():
            self.overlay.set_status("transcribing...")
        drained = (
            not self._session_flushing
            and self.transcribe_queue.empty()
            and not self._transcribing
        )
        if drained or time.monotonic() - started_at > 25:
            self._dispatch_after = None
            self._dispatch_transcript()
            return
        self._dispatch_after = self.overlay.root.after(60, lambda: self._wait_for_transcript(started_at))

    def _dismiss_overlay(self):
        """Hard dismiss for cursor/clipboard — beats any queued set_transcript/show."""
        if self._agent_hide_after:
            try:
                self.overlay.root.after_cancel(self._agent_hide_after)
            except tk.TclError:
                pass
            self._agent_hide_after = None
        self._overlay_gen += 1
        gen = self._overlay_gen
        self._cancel_linger()
        self.overlay.clear_compose()
        self.overlay.hide()
        self._clear_agent_sticky()
        # Catch UI-queue items that were already enqueued before dismiss.
        self.overlay.root.after(50, lambda: self._hide_if_gen(gen))
        self.overlay.root.after(200, lambda: self._hide_if_gen(gen))

    def _hide_if_gen(self, gen):
        if self._overlay_gen != gen:
            return
        if self.active or self._dispatch_pending or self.target == "agent":
            return
        if self._agent_overlay_open():
            return
        try:
            self.overlay.clear_compose()
            self.overlay.hide()
        except Exception:
            pass

    def _log_transcript(self, text, target, dropped=""):
        """Append the finished turn to voice-transcripts.jsonl.

        The error log only ever recorded a character count, so there was no way
        to see what you actually dictated ten minutes ago.
        """
        if not self.settings.get("transcript_history", True):
            return
        record = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "target": target,
            "text": text,
            "language": self._session_language or "",
        }
        if dropped:
            record["dropped"] = dropped
        try:
            with TRANSCRIPT_LOG_PATH.open("a", encoding="utf-8") as file:
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            log_event(f"transcript history write failed: {exc}")

    def _dispatch_transcript(self):
        self._cancel_dispatch()
        self._dispatch_pending = False
        # join_chunks removes words duplicated across a flush seam without
        # touching repetition the user actually spoke.
        text = join_chunks(self.transcript_parts)
        probabilities = list(self._chunk_no_speech)
        self.transcript_parts = []
        self._chunk_no_speech = []
        # Now that the whole turn is assembled, decide whether it was speech at
        # all. Doing this per chunk would eat a genuine "thanks, bye".
        if text and self.settings.get("hallucination_filter", True):
            no_speech_prob = max(probabilities) if probabilities else None
            if is_hallucination(text, no_speech_prob=no_speech_prob):
                log_event(f"dropped hallucinated turn: {text[:60]!r} (p={no_speech_prob})")
                self._log_transcript(text, self.target, dropped="hallucination")
                text = ""
        if not text:
            # Empty hold: keep an agent chat that's still up; otherwise dismiss.
            if self._agent_overlay_open() or (
                self.target == "agent" and self.overlay.has_compose_content()
            ):
                self.overlay.set_status("ready")
            else:
                self._dismiss_overlay()
            return
        target = self.target
        log_event(f"dispatch {len(text)} chars -> {target}")
        self._log_transcript(text, target)
        self.overlay.set_target(target)
        if target == "agent":
            # Agent keeps the chat up while it thinks / answers.
            self.overlay.show()
            self._agent_echo_user = True
            self.overlay.commit_current_turn()
            self.overlay.set_history(self._agent_history())
            self.overlay.set_transcript(text)
            self.overlay.set_compose_note("thinking...")
            self.overlay.set_status("agent")
            send_to_helper(
                "voice_event",
                text,
                {"kind": "turn_start", "echo_user": True},
            )
            mirror_phone_event("turn_start", text, {"source": "voice"})
            threading.Thread(target=self._send_to_agent, args=(text, self._turn_id), daemon=True).start()
            return
        # Cursor / clipboard: send and dismiss immediately — no linger.
        # Capture gen so a late set_transcript from the worker can't reopen us.
        dismiss_gen = self._overlay_gen + 1
        if target == "clipboard":
            threading.Thread(target=self._send_to_clipboard, args=(text, dismiss_gen), daemon=True).start()
        else:
            threading.Thread(target=self._send_to_cursor, args=(text, dismiss_gen), daemon=True).start()
        self._dismiss_overlay()

    def _send_to_cursor(self, text, dismiss_gen=None):
        try:
            self._type_text(text)
        except Exception as exc:
            log_event(f"typing failed: {exc}")
            # Don't resurrect a dismissed cursor overlay for the error chip.
            if dismiss_gen is not None and self._overlay_gen != dismiss_gen:
                return
            if self.target == "agent" or self._agent_overlay_open():
                self.ui(self.overlay.set_compose_error, f"could not type: {exc}")

    def _send_to_clipboard(self, text, dismiss_gen=None):
        try:
            self._set_clipboard(text)
        except Exception as exc:
            log_event(f"clipboard failed: {exc}")
            if dismiss_gen is not None and self._overlay_gen != dismiss_gen:
                return
            if self.target == "agent" or self._agent_overlay_open():
                self.ui(self.overlay.set_compose_error, f"could not copy: {exc}")

    def _agent_history(self):
        """Earlier turns, if an agent conversation is already running."""
        if self.agent is None:
            return []
        try:
            return self.agent.history()
        except Exception as exc:
            log_event(f"agent history failed: {exc}")
            return []

    def _ensure_agent(self):
        if self.agent is not None:
            return self.agent
        from voice_agent import VoiceAgent

        self.agent = VoiceAgent(
            on_event=self._agent_event,
            type_text=self._type_text,
            copy_text=self._set_clipboard,
            hide_overlay=self._request_agent_overlay_hide,
            # Reminders fire outside any turn, so they need their own way to
            # reach the speakers.
            speak=self._speak_agent_reply,
        )
        return self.agent

    def _request_agent_overlay_hide(self, signoff=""):
        """Hide after the streamed sign-off has been visible and spoken."""
        signoff = clean_reply(str(signoff or ""))
        delay_ms = max(1200, min(4500, int(len(signoff) * 55 + 500)))
        log_event(f"agent hide_overlay scheduled after sign-off ({delay_ms}ms)")

        def schedule():
            if self._agent_hide_after:
                try:
                    self.overlay.root.after_cancel(self._agent_hide_after)
                except tk.TclError:
                    pass
            # The completed-turn UI schedules its normal linger immediately
            # afterward, so the goodbye timer needs an independent handle.
            self._agent_hide_after = self.overlay.root.after(delay_ms, self._dismiss_overlay)

        self.ui(schedule)

    def _ui_if_current(self, turn, fn, *args):
        """Drop UI updates from a turn the user has already spoken over."""
        def apply():
            if turn == self._turn_id:
                fn(*args)

        self.ui_queue.put(apply)

    def _agent_event(self, kind, payload):
        turn = self._turn_id
        echo_user = bool(getattr(self, "_agent_echo_user", True))
        # Mirror to the phone before the desktop work, so a slow UI never
        # delays what the phone sees.
        if kind in {"reply_start", "reply_delta", "reply_done", "status"}:
            mirror_phone_event(kind, payload if isinstance(payload, str) else "")
        elif kind in {"tool_start", "tool_done"} and isinstance(payload, dict):
            mirror_phone_event(
                kind,
                str(payload.get("label") or payload.get("name") or "tool"),
                {"tool": str(payload.get("name") or ""), "ok": bool(payload.get("ok", True))},
            )
        if kind == "tool":
            text = payload if isinstance(payload, str) else str((payload or {}).get("label") or "")
            if text:
                self._ui_if_current(turn, self.overlay.push_tool, text)
        elif kind == "status":
            text = payload if isinstance(payload, str) else str((payload or {}).get("text") or "thinking")
            self._ui_if_current(turn, self.overlay.set_compose_note, f"{text}...")
            # Only mirror meaningful status changes to the GUI (avoid flood).
            if text and text.casefold() not in {"thinking"}:
                send_to_helper(
                    "voice_event",
                    text,
                    {"kind": "status", "status": text, "echo_user": echo_user},
                )
        elif kind == "tool_start":
            detail = payload if isinstance(payload, dict) else {"name": str(payload or "tool")}
            label = str(detail.get("label") or detail.get("name") or "tool")
            self._ui_if_current(turn, self.overlay.set_compose_note, f"{label}...")
            send_to_helper(
                "voice_event",
                label,
                {"kind": "tool_start", "tool": detail, "echo_user": echo_user},
            )
        elif kind == "tool_done":
            detail = payload if isinstance(payload, dict) else {"name": str(payload or "tool")}
            label = str(detail.get("label") or detail.get("name") or "tool")
            self._ui_if_current(turn, self.overlay.push_tool, label)
            send_to_helper(
                "voice_event",
                label,
                {"kind": "tool_done", "tool": detail, "echo_user": echo_user},
            )
        elif kind == "reply_start":
            player = self._configure_agent_tts() if getattr(self, "_agent_speak_reply", True) else None
            if player is not None:
                try:
                    player.begin_stream()
                except Exception as exc:
                    log_event(f"agent TTS stream start failed: {exc}")
            self._ui_if_current(turn, self.overlay.start_reply_stream)
            send_to_helper(
                "voice_event",
                "",
                {"kind": "reply_start", "echo_user": echo_user},
            )
        elif kind == "reply_delta":
            delta = str(payload or "")
            if delta:
                self._ui_if_current(turn, self.overlay.append_reply_delta, delta)
                send_to_helper(
                    "voice_event",
                    delta,
                    {"kind": "reply_delta", "echo_user": echo_user},
                )
        elif kind == "reply_done":
            reply = str(payload or "")
            player = self._configure_agent_tts() if getattr(self, "_agent_speak_reply", True) else None
            if player is not None:
                try:
                    player.end_stream(clean_reply(reply))
                except Exception as exc:
                    log_event(f"agent TTS stream finish failed: {exc}")
            if reply:
                self._ui_if_current(turn, self.overlay.set_reply, reply)
            send_to_helper(
                "voice_event",
                reply,
                {"kind": "reply_done", "echo_user": echo_user},
            )

    def ask_text(self, text, echo_user=True, *, reasoning="", speak_reply=True):
        """Typed (or otherwise finished) text → same agent path as a spoken turn."""
        text = str(text or "").strip()
        if not text:
            return
        if self.active:
            # Don't interrupt a live hold; the release path owns that transcript.
            log_event("ask_text ignored while dictation is active")
            send_to_helper(
                "voice_log",
                text,
                {"error": "dictation is active — release the mic first", "echo_user": echo_user},
            )
            return
        self._cancel_linger()
        self._cancel_dispatch()
        self.target = "agent"
        self._target_set_at = time.monotonic()
        self._turn_id += 1
        turn = self._turn_id
        self._agent_echo_user = bool(echo_user)
        self._agent_speak_reply = bool(speak_reply)
        self.overlay.set_mode("dictation")
        self.overlay.set_history(self._agent_history())
        self.overlay.set_target("agent")
        self.overlay.set_transcript(text)
        self.overlay.set_compose_note("thinking...")
        self.overlay.set_status("agent")
        self.overlay.show()
        send_to_helper(
            "voice_event",
            text,
            {"kind": "turn_start", "echo_user": bool(echo_user)},
        )
        mirror_phone_event("turn_start", text, {"source": "typed"})
        threading.Thread(
            target=self._send_to_agent,
            args=(text, turn),
            kwargs={
                "echo_user": bool(echo_user),
                "reasoning": str(reasoning or "").strip().lower(),
            },
            daemon=True,
        ).start()

    def reset_agent(self):
        """Forget the voice-agent conversation (aiOS GUI Reset)."""
        self._cancel_linger()
        if self.agent is not None:
            try:
                self.agent.clear()
            except Exception as exc:
                log_event(f"agent reset failed: {exc}")
        self._clear_agent_sticky()
        self.ui(self.overlay.clear_compose)
        if not self.active:
            self.ui(self.overlay.hide)
        log_event("agent conversation reset")

    @staticmethod
    def _transport_tool_details(details):
        """Make tool details JSON-safe without truncating inputs or results."""
        transported = []
        for item in list(details or []):
            if not isinstance(item, dict):
                continue
            transported.append(json.loads(json.dumps(item, ensure_ascii=False, default=str)))
        return transported

    def _send_to_agent(self, text, turn, echo_user=True, reasoning=""):
        started = time.monotonic()
        self._agent_echo_user = bool(echo_user)
        reply = ""
        error = ""
        details = []
        tools = []
        try:
            overrides = {"agent_reasoning": reasoning} if reasoning else None
            result = self._ensure_agent().run(text, overrides=overrides)
        except Exception as exc:
            log_event(f"agent failed to start: {exc}")
            result = None
            error = str(exc)[:200]
        cancelled = False
        if result is not None:
            details = self._transport_tool_details(getattr(result, "tool_details", None) or [])
            tools = list(result.tools or [])[:12]
            cancelled = bool(getattr(result, "cancelled", False))
            if result.error:
                error = str(result.error)
            else:
                reply = (result.reply or "").strip()
        elapsed = time.monotonic() - started
        note = "stopped" if cancelled and not reply else f"{elapsed:.1f}s"
        def finish_overlay():
            if turn != self._turn_id:
                return
            if cancelled and not reply:
                self.overlay.set_compose_note("stopped")
                self.overlay.set_status("ready")
                self._linger(COMPOSE_LINGER_MS)
                return
            if error and not reply:
                self.overlay.set_compose_error(error)
                self.overlay.commit_current_turn()
            else:
                # Fold the finished turn into history from agent memory so the
                # next hold can't make the latest messages vanish.
                self.overlay.show_finished_turn(self._agent_history(), note=note)
            self.overlay.set_status("ready")
            self._linger(AGENT_LINGER_MS)

        self.ui(finish_overlay)
        payload = {
            "reply": reply,
            "error": error,
            "tools": tools,
            "tool_details": details,
            "echo_user": echo_user,
            "elapsed": elapsed if result is not None else elapsed,
        }
        mirror_phone_event(
            "turn_done",
            reply or error,
            {"error": bool(error), "tools": tools, "elapsed": round(elapsed, 2)},
        )
        ok = send_to_helper("voice_log", text, payload)
        log_event(
            f"voice_log {'sent' if ok else 'FAILED'} "
            f"reply={len(reply)} error={bool(error)} tools={len(details)} echo={echo_user}"
        )

    def shutdown_pipeline(self):
        """Close warm mic/workers on app quit."""
        self._pipeline_quit.set()
        self.active = False
        self.stop_event.set()
        self.overlay.set_recording(False)
        if self.agent_tts is not None:
            try:
                self.agent_tts.shutdown()
            except Exception:
                pass
            self.agent_tts = None
        try:
            self.transcribe_queue.put_nowait(None)
        except Exception:
            pass
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    def _drain_queue(self, target):
        try:
            while True:
                target.get_nowait()
                try:
                    target.task_done()
                except Exception:
                    pass
        except queue.Empty:
            pass

    def _reset_buffer(self):
        self._chunk_parts = []
        self._chunk_samples = 0

    def _append_audio(self, chunk):
        self._chunk_parts.append(chunk)
        self._chunk_samples += int(chunk.shape[0])

    def _take_buffer(self, overlap=0.0):
        """Drain the buffer, optionally leaving a tail behind for continuity.

        ``overlap`` seconds of the end are copied back into the fresh buffer so
        a forced mid-sentence flush does not cut a word in half.
        """
        if not self._chunk_parts:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate(self._chunk_parts)
        self._reset_buffer()
        keep = int(max(0.0, float(overlap)) * SAMPLE_RATE)
        if keep and audio.shape[0] > keep:
            self._append_audio(audio[-keep:].copy())
        return audio

    def _buffer_seconds(self):
        return self._chunk_samples / float(SAMPLE_RATE)

    def _ensure_pipeline(self):
        """Keep mic + workers warm between PTT sessions (big start-latency win)."""
        with self._pipeline_lock:
            if self.stream is None:
                input_device = self._input_device()
                self.stream = sd.InputStream(
                    device=input_device,
                    samplerate=SAMPLE_RATE,
                    channels=1,
                    dtype="float32",
                    blocksize=int(SAMPLE_RATE * 0.08),
                    callback=self._audio_callback,
                )
                self.stream.start()
                log_event("microphone stream started (warm)")
            if self.worker_thread is None or not self.worker_thread.is_alive():
                self._pipeline_quit.clear()
                self.worker_thread = threading.Thread(target=self._capture_loop, daemon=True)
                self.worker_thread.start()
            if self.transcribe_thread is None or not self.transcribe_thread.is_alive():
                self.transcribe_thread = threading.Thread(target=self._transcribe_loop, daemon=True)
                self.transcribe_thread.start()

    def _capture_loop(self):
        while not self._pipeline_quit.is_set():
            # Idle between sessions — discard any stray audio and wait for PTT.
            if not self.active and not self.stop_event.is_set():
                try:
                    self.audio_queue.get(timeout=0.05)
                except queue.Empty:
                    pass
                continue

            silence_rms = float(self.settings["silence_rms"])
            chunk_seconds = float(self.settings.get("chunk_seconds", 1.0))
            silence_flush_seconds = float(self.settings.get("silence_flush_seconds", 0.35))
            turn_id = self._turn_id
            self.ui(self.overlay.set_status, "")

            while self.active and not self.stop_event.is_set() and not self._pipeline_quit.is_set():
                try:
                    chunk = self.audio_queue.get(timeout=0.05)
                except queue.Empty:
                    chunk = None

                if chunk is not None:
                    self._append_audio(chunk)
                    rms = float(np.sqrt(np.mean(chunk * chunk) + 1e-9))
                    if rms > silence_rms:
                        self.last_voice_at = time.monotonic()

                duration = self._buffer_seconds()
                quiet_for = time.monotonic() - self.last_voice_at
                if duration >= chunk_seconds and quiet_for >= silence_flush_seconds:
                    # Flushed on a natural pause, so the cut is between words.
                    self._queue_transcription(self._take_buffer())
                    self.last_voice_at = time.monotonic()
                elif duration >= MAX_BUFFER_SECONDS:
                    # Forced flush mid-sentence: keep a short tail so the next
                    # buffer does not start halfway through a syllable.
                    self._queue_transcription(self._take_buffer(overlap=FLUSH_OVERLAP_SECONDS))
                    self.last_voice_at = time.monotonic()

            # End of PTT session: pull any leftover mic chunks, then flush.
            while True:
                try:
                    chunk = self.audio_queue.get_nowait()
                except queue.Empty:
                    break
                self._append_audio(chunk)
            if self._buffer_seconds() >= MIN_AUDIO_SECONDS:
                self._queue_transcription(self._take_buffer())
            else:
                self._reset_buffer()
            # Only clear if this is still the same turn (a quick re-press bumps turn_id).
            if self._turn_id == turn_id:
                self._session_flushing = False
            self.stop_event.clear()
            # Hide overlay once in-flight transcriptions drain (best-effort).
            self.overlay.root.after(120, self._hide_if_idle)

    def _hide_if_idle(self):
        """Only for turns that never produced a panel — otherwise the linger owns it."""
        if self.active or self._session_flushing or self._dispatch_pending or self._linger_after:
            return
        if not self.transcribe_queue.empty() or self._transcribing:
            self.overlay.root.after(120, self._hide_if_idle)
            return
        if self.overlay.has_compose_content():
            return
        try:
            self.overlay.hide()
        except Exception:
            pass

    def _queue_transcription(self, audio):
        """Send a clip to Whisper, padding short ones instead of dropping them.

        The old floor silently swallowed "yes", "stop" and "nej" — the shortest
        and most time-critical things anyone says to a voice agent.
        """
        seconds = audio.shape[0] / SAMPLE_RATE
        if seconds < MIN_AUDIO_SECONDS:
            return
        floor = float(self.settings.get("min_speech_seconds", 0.22) or 0.22)
        if seconds < floor:
            return
        if seconds < PAD_TO_SECONDS:
            padding = int((PAD_TO_SECONDS - seconds) * SAMPLE_RATE)
            audio = np.concatenate([audio, np.zeros(padding, dtype=np.float32)])
        self.transcribe_queue.put(audio)

    def _transcribe_loop(self):
        while not self._pipeline_quit.is_set():
            try:
                audio = self.transcribe_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if audio is None:
                    if self._pipeline_quit.is_set():
                        return
                    continue
                self._transcribe_and_emit(audio)
            finally:
                try:
                    self.transcribe_queue.task_done()
                except Exception:
                    pass

    def _transcribe_and_emit(self, audio):
        self._transcribing = True
        try:
            self._transcribe_and_collect(audio)
        finally:
            self._transcribing = False

    def _transcribe_and_collect(self, audio):
        # Show progress after release too — stop() clears active before Whisper runs.
        if self.active or self._dispatch_pending:
            self.ui(self.overlay.set_status, "transcribing...")
        try:
            self.ensure_model()
            text, no_speech_prob = self._transcribe_text(audio)
        except Exception as exc:
            if self._should_retry_on_cpu(exc):
                log_event(f"transcription failed on {getattr(self, '_loaded_device', 'auto')}: {exc}; retrying on cpu")
                try:
                    self.settings["device"] = "cpu"
                    self.settings["compute_type"] = "int8"
                    with self.model_lock:
                        self.model = None
                        self._loaded_device = None
                    self.ensure_model()
                    text, no_speech_prob = self._transcribe_text(audio)
                except Exception as retry_exc:
                    log_event(f"transcription failed on cpu: {retry_exc}")
                    self.ui(self.overlay.set_status, "asr error")
                    return
            else:
                log_event(f"transcription failed: {exc}")
                self.ui(self.overlay.set_status, "asr error")
                return
        if self.active:
            self.ui(self.overlay.set_status, "")
        elif self._dispatch_pending:
            self.ui(self.overlay.set_status, "transcribing...")
        if not text:
            log_event("transcription returned empty text")
            return
        # Sound-event markers ([BLANK_AUDIO], ♪) are never speech, so they can go
        # at the chunk level. The ambiguous silence artifacts are judged later,
        # against the whole turn, so a real "thanks" mid-sentence survives.
        if is_non_speech_marker(text):
            log_event(f"dropped non-speech chunk: {text[:40]!r}")
            return
        text = apply_replacements(tidy_transcript(text), self.settings.get("replacements"))
        if not text:
            return
        log_event(f"transcribed {len(text)} chars")
        if self.active_mode == "minecraft" or is_minecraft_foreground():
            self._emit_minecraft(text)
            return
        # Nothing is typed while you speak any more: the words land in the
        # composer panel and only leave when you release the key.
        self.transcript_parts.append(text)
        if no_speech_prob is not None:
            self._chunk_no_speech.append(float(no_speech_prob))
        # Same join the dispatch will use, so the panel shows exactly what gets sent.
        joined = join_chunks(self.transcript_parts)
        gen = self._overlay_gen

        def apply_transcript():
            # Cursor/clipboard may have already dismissed — don't reopen.
            if self._overlay_gen != gen and self.target != "agent":
                return
            if (
                not self.active
                and not self._dispatch_pending
                and self.target != "agent"
                and not self._agent_overlay_open()
            ):
                return
            self.overlay.set_transcript(joined)
            # Agent (and live hold) need the panel; cursor/clipboard dismiss on release.
            if self.active or self.target == "agent":
                self.overlay.show()

        self.ui(apply_transcript)

    def _type_text(self, text):
        payload = text + " "
        delay = max(0, int(self.settings.get("typing_delay_ms", 0))) / 1000.0
        if len(payload) >= PASTE_MIN_CHARS and delay <= 0:
            try:
                self._paste_text(payload)
                return
            except Exception as exc:
                log_event(f"clipboard paste failed, falling back to type: {exc}")
        keyboard.write(payload, delay=delay)

    def _set_clipboard(self, text):
        """Clipboard writes have to happen on the Tk thread; block until they land."""
        ready = threading.Event()
        errors = []

        def apply():
            try:
                root = self.overlay.root
                root.clipboard_clear()
                root.clipboard_append(text)
                root.update_idletasks()
            except Exception as exc:
                errors.append(exc)
            finally:
                ready.set()

        self.overlay.root.after(0, apply)
        if not ready.wait(0.8) or errors:
            raise RuntimeError(errors[0] if errors else "clipboard timeout")

    def _paste_text(self, text):
        self._set_clipboard(text)
        time.sleep(0.015)
        keyboard.send("ctrl+v")

    def _emit_minecraft(self, text):
        if self.minecraft_assistant is None:
            self.ui(self.overlay.set_status, "mc ai unavailable")
            log_event("minecraft ai unavailable")
            time.sleep(1.2)
            return
        self.ui(self.overlay.set_mode, "minecraft")
        self.ui(self.overlay.set_status, "mc thinking...")
        log_event(f"minecraft ai request: {text[:120]}")
        try:
            result = self.minecraft_assistant.handle(text)
        except Exception as exc:
            result = {
                "status": "error",
                "user": text,
                "reply": f"MC AI error: {exc}",
                "goal": "",
                "shopping": [],
            }
        status = str(result.get("status") or "done") if isinstance(result, dict) else str(result)
        reply = str(result.get("reply") or status) if isinstance(result, dict) else status
        log_event(f"minecraft ai response: {reply[:160]}")
        if isinstance(result, dict):
            self.ui(self.overlay.set_minecraft_state, result)
        self.ui(self.overlay.set_status, status[:24] or "done")
        time.sleep(1.4)

    def _transcribe_text(self, audio):
        """Decode one clip. Returns (text, no_speech_prob).

        The no-speech probability is what lets the caller tell a real "thank
        you" from Whisper's stock reply to a second of fan noise.
        """
        started = time.monotonic()
        seconds = audio.shape[0] / SAMPLE_RATE
        segments, info = self.model.transcribe(audio, **self._transcribe_kwargs())
        parts = []
        probabilities = []
        for segment in segments:
            parts.append(segment.text)
            probability = getattr(segment, "no_speech_prob", None)
            if probability is not None:
                try:
                    probabilities.append(float(probability))
                except (TypeError, ValueError):
                    pass
        text = "".join(parts).strip()
        no_speech_prob = min(probabilities) if probabilities else None
        self._maybe_lock_language(info, seconds)
        elapsed = time.monotonic() - started
        log_event(f"asr {seconds:.2f}s audio -> {elapsed:.3f}s")
        return text, no_speech_prob

    def _maybe_lock_language(self, info, seconds):
        """Pin the session language, but only once the guess is worth trusting.

        Locking off the first one-second chunk used to send a whole English
        sentence down the Swedish path because of a single filler word.
        """
        if self._session_language is not None or info is None:
            return
        detected = str(getattr(info, "language", "") or "").strip().lower()
        if detected not in {"en", "sv"}:
            return
        try:
            probability = float(getattr(info, "language_probability", 0.0) or 0.0)
        except (TypeError, ValueError):
            probability = 0.0
        if seconds < LANGUAGE_LOCK_MIN_SECONDS or probability < LANGUAGE_LOCK_MIN_PROBABILITY:
            # Remember the guess; two chunks agreeing is also good enough.
            if self._language_votes.get(detected, 0) + 1 >= 2:
                self._session_language = detected
                log_event(f"session language locked to {detected} (two agreeing chunks)")
                return
            self._language_votes[detected] = self._language_votes.get(detected, 0) + 1
            return
        self._session_language = detected
        log_event(f"session language locked to {detected} (p={probability:.2f}, {seconds:.1f}s)")

    def _should_retry_on_cpu(self, exc):
        message = str(exc).lower()
        return self._whisper_device() != "cpu" and any(token in message for token in ("cuda", "cublas", "cudnn"))


def run_command_server(dictation):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind((HOST, PORT))
    except OSError:
        return False
    server.listen(4)

    def loop():
        while True:
            try:
                client, _addr = server.accept()
            except OSError:
                return
            try:
                raw = client.recv(65536).decode("utf-8", errors="ignore").strip()
            except OSError:
                raw = ""
            finally:
                try:
                    client.close()
                except OSError:
                    pass
            if raw.startswith("{"):
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    payload = None
                if isinstance(payload, dict):
                    cmd = str(payload.get("cmd") or "").strip().lower()
                    if cmd == "ask":
                        text = str(payload.get("text") or "")
                        echo_user = bool(payload.get("echo_user", True))
                        reasoning = str(payload.get("reasoning") or "").strip().lower()
                        speak_reply = bool(payload.get("speak_reply", True))
                        log_event(f"voice command: ask ({len(text)} chars)")
                        dictation.overlay.root.after(
                            0,
                            lambda value=text, echo=echo_user, effort=reasoning, speak=speak_reply: dictation.ask_text(
                                value,
                                echo_user=echo,
                                reasoning=effort,
                                speak_reply=speak,
                            ),
                        )
                        continue
                    if cmd == "reset_agent":
                        log_event("voice command: reset_agent")
                        dictation.overlay.root.after(0, dictation.reset_agent)
                        continue
            data = raw.lower()
            if data == "toggle":
                log_event("voice command: toggle")
                dictation.overlay.root.after(0, dictation.toggle)
            elif data == "start":
                log_event("voice command: start")
                dictation.overlay.root.after(0, dictation.start)
            elif data == "stop":
                log_event("voice command: stop")
                dictation.overlay.root.after(0, dictation.stop)
            elif data == "ptt_down":
                log_event("voice command: ptt_down")
                dictation.overlay.root.after(0, dictation.ptt_down)
            elif data == "ptt_up":
                log_event("voice command: ptt_up")
                dictation.overlay.root.after(0, dictation.ptt_up)
            elif data.startswith("target"):
                # "target:agent" / "target agent" — the macro keyboard picking a
                # destination, usually while the dictate key is still held.
                name = data.replace("target", "", 1).strip(" :=")
                log_event(f"voice command: target {name}")
                dictation.overlay.root.after(0, lambda value=name: dictation.set_target(value))
            elif data in TARGET_ALIASES:
                log_event(f"voice command: {data}")
                dictation.overlay.root.after(0, lambda value=data: dictation.set_target(value))
            elif data == "cancel":
                log_event("voice command: cancel")
                dictation.overlay.root.after(0, dictation.cancel)
            elif data in {"stop_agent", "shush", "quiet"}:
                log_event("voice command: stop_agent")
                dictation.overlay.root.after(0, dictation.stop_agent)
            elif data == "reset_agent":
                log_event("voice command: reset_agent")
                dictation.overlay.root.after(0, dictation.reset_agent)
            elif data == "send":
                log_event("voice command: send")
                dictation.overlay.root.after(0, dictation.stop)
            elif data == "reload":
                log_event("voice command: reload")
                dictation.overlay.root.after(0, dictation.reload_settings)
            elif data == "quit":
                log_event("voice command: quit")
                dictation.overlay.root.after(0, dictation.stop)
                dictation.overlay.root.after(80, dictation.overlay.root.destroy)
                return

    threading.Thread(target=loop, daemon=True).start()
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--toggle", action="store_true")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--quit", action="store_true")
    parser.add_argument("--cancel", action="store_true")
    parser.add_argument(
        "--stop-agent",
        action="store_true",
        help="abort the running agent turn / OPERATOR job and stop speaking",
    )
    parser.add_argument("--target", default="", help="cursor | clipboard | agent")
    args = parser.parse_args()

    if args.quit:
        send_command("quit")
        return
    if args.stop:
        send_command("stop")
        return
    if args.cancel:
        send_command("cancel")
        return
    if args.stop_agent:
        send_command("stop_agent")
        return
    if args.target:
        send_command(f"target:{args.target.strip().lower()}")
        return

    # Client mode: try to wake an existing server first
    forwarded = False
    if args.toggle:
        forwarded = send_command("toggle")
    elif args.start:
        forwarded = send_command("start")
    if forwarded:
        return

    # No server running -> become one
    if load_runtime_dependencies():
        die_missing()

    dictation = Dictation()
    if not run_command_server(dictation):
        stderr_write("Could not bind voice control port; another instance may be running.\n")
        return

    # If launched with --toggle/--start and no server existed, kick off dictation now
    if args.toggle or args.start:
        dictation.overlay.root.after(50, dictation.start)

    # Pre-load model + warm mic/CUDA so the first real PTT is instant
    if os.environ.get("VOICE_PRELOAD", "1") != "0":
        def _preload():
            try:
                dictation.warmup()
            except Exception as exc:
                stderr_write(f"[preload] {exc}\n")
        threading.Thread(target=_preload, daemon=True).start()

    def quit_app():
        try:
            dictation.stop()
        except Exception:
            pass
        try:
            dictation.shutdown_pipeline()
        except Exception:
            pass
        try:
            dictation.overlay.root.destroy()
        except Exception:
            pass
        os._exit(0)

    try:
        keyboard.add_hotkey("ctrl+alt+shift+q", quit_app)
    except Exception:
        pass

    try:
        dictation.overlay.root.mainloop()
    except KeyboardInterrupt:
        quit_app()


if __name__ == "__main__":
    main()
