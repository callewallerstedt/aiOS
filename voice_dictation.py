"""Local live speech-to-text dictation, controlled by AutoHotkey.

Behavior
--------
- Long-running background process. Started automatically by aiOS on launch.
- AHK sends "start" / "stop" on a local TCP socket (port 48737) when the user
  holds or releases Insert. This script does NOT register a global keyboard hook.
- While active, audio from the default mic is transcribed locally with
  faster-whisper when the user releases Insert, and the text is typed into the
  focused window.
- A small mic overlay (bottom-center, always-on-top) shows status + level.

CLI
---
    python voice_dictation.py            # run the background server + overlay
    python voice_dictation.py --toggle   # ping the server to start/stop
    python voice_dictation.py --stop     # force-stop the dictation
    python voice_dictation.py --quit     # quit the background process

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
import socket
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

from voice_settings import load_voice_dictation_settings, resolve_transcribe_language

HOST = "127.0.0.1"
PORT = 48737

MISSING = []
keyboard = None
np = None
sd = None
WhisperModel = None


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
    try:
        from faster_whisper import WhisperModel as WhisperModelClass
    except ImportError as exc:
        raise RuntimeError("faster-whisper is not installed") from exc
    WhisperModel = WhisperModelClass


SAMPLE_RATE = 16000
MIN_AUDIO_SECONDS = 0.6
MAX_BUFFER_SECONDS = 12.0
CONFIG_PATH = Path(__file__).resolve().parent / "helper_config.json"
LOG_PATH = Path(__file__).resolve().parent / "voice-err.log"
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
LWA_COLORKEY = 0x00000001


def log_event(message):
    try:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with LOG_PATH.open("a", encoding="utf-8") as file:
            file.write(f"{timestamp} {message}\n")
    except OSError:
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


def die_missing():
    msg = (
        "Voice dictation needs these Python packages:\n  "
        + "\n  ".join(MISSING)
        + "\n\nInstall with:\n  pip install keyboard numpy sounddevice faster-whisper\n"
    )
    sys.stderr.write(msg)
    sys.exit(1)


def send_command(command):
    try:
        with socket.create_connection((HOST, PORT), timeout=0.2) as client:
            client.sendall(command.encode("utf-8"))
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
        self.text = theme["text"]
        self.muted = theme["muted"]
        self.success = theme["success"]
        self.width = 150
        self.height = 64
        self.root.configure(bg=self.transparent)
        self.canvas = tk.Canvas(
            self.root,
            width=self.width,
            height=self.height,
            bg=self.transparent,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self.level = 0.0
        self.status_text = "listening"
        self.pulse = 0.0
        self._after = None
        self._visible = False
        self._apply_transparency()
        self._apply_window_shape()

    def _place_bottom_center(self):
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - self.width) // 2
        y = max(40, sh - self.height - 140)
        self.root.geometry(f"{self.width}x{self.height}+{x}+{y}")
        self._apply_window_shape()

    def show(self):
        if self._visible:
            self.root.lift()
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
            self.root.lift()
            self.root.update()
        except tk.TclError:
            pass
        self._visible = True
        sys.stderr.write(f"[overlay] shown at geometry {self.root.geometry()}\n")
        sys.stderr.flush()
        self._animate()

    def hide(self):
        if not self._visible:
            return
        self._visible = False
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
        self.level = max(0.0, min(1.0, float(level)))

    def set_status(self, text):
        self.status_text = text

    def _apply_transparency(self):
        try:
            self.root.attributes("-transparentcolor", self.transparent)
        except tk.TclError:
            pass
        if not sys.platform.startswith("win"):
            return
        try:
            hwnd = self.root.winfo_id()
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)
            r, g, b = self.root.winfo_rgb(self.transparent)
            color_key = (r // 256) | ((g // 256) << 8) | ((b // 256) << 16)
            ctypes.windll.user32.SetLayeredWindowAttributes(hwnd, color_key, 0, LWA_COLORKEY)
        except (AttributeError, OSError, tk.TclError):
            pass

    def _apply_window_shape(self):
        if not sys.platform.startswith("win"):
            return
        try:
            hwnd = self.root.winfo_id()
            region = ctypes.windll.gdi32.CreateRoundRectRgn(
                0, 0, self.width + 1, self.height + 1, 42, 42
            )
            ctypes.windll.user32.SetWindowRgn(hwnd, region, True)
        except (AttributeError, OSError, tk.TclError):
            pass

    def _animate(self):
        if not self._visible:
            return
        self.pulse = (self.pulse + 0.18) % (math.pi * 2)
        self.canvas.delete("all")
        glow = 0.5 + 0.5 * math.sin(self.pulse)
        outline = self.blend_color(self.surface2, self.accent, 0.18 + self.level * 0.38)
        self._round_rect(
            1, 1, self.width - 2, self.height - 2, 22,
            fill=self.panel_bg, outline=outline, width=1,
        )
        cx, cy = 34, self.height // 2
        ring = 12 + int(glow * 3 + self.level * 8)
        self.canvas.create_oval(
            cx - ring, cy - ring, cx + ring, cy + ring,
            outline=self.blend_color(self.panel_bg, self.accent, 0.22 + self.level * 0.35),
            width=1,
        )
        self.canvas.create_oval(
            cx - 6, cy - 6, cx + 6, cy + 6,
            fill=self.accent, outline="",
        )
        x0 = 64
        max_bar = 26
        for index in range(8):
            phase = self.pulse + index * 0.7
            strength = 0.25 + self.level * 0.75
            height = 6 + abs(math.sin(phase)) * max_bar * strength
            x = x0 + index * 8
            color = self.blend_color(self.muted, self.accent, min(1.0, 0.2 + strength))
            self._round_rect(
                x, cy - height / 2, x + 4, cy + height / 2, 3,
                fill=color, outline="",
            )
        self._after = self.root.after(60, self._animate)

    def _round_rect(self, x1, y1, x2, y2, r, **kwargs):
        points = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return self.canvas.create_polygon(points, smooth=True, splinesteps=24, **kwargs)

    def blend_color(self, background, foreground, amount):
        amount = max(0.0, min(1.0, float(amount)))
        try:
            br, bg, bb = self.root.winfo_rgb(background)
            fr, fg, fb = self.root.winfo_rgb(foreground)
        except tk.TclError:
            return foreground
        values = []
        for base, top in ((br, fr), (bg, fg), (bb, fb)):
            values.append(int((base + (top - base) * amount) / 256))
        return f"#{values[0]:02x}{values[1]:02x}{values[2]:02x}"


class Dictation:
    def __init__(self):
        self.active = False
        self.settings = load_voice_dictation_settings()
        self.audio_queue: "queue.Queue[np.ndarray]" = queue.Queue()
        self.transcribe_queue: "queue.Queue[np.ndarray | None]" = queue.Queue()
        self.stream = None
        self.buffer = np.zeros(0, dtype=np.float32)
        self.last_voice_at = 0.0
        self.model = None
        self.model_lock = threading.Lock()
        self._loaded_model_name = None
        self.overlay = MicOverlay()
        self.worker_thread = None
        self.transcribe_thread = None
        self.stop_event = threading.Event()
        self.ui_queue: "queue.Queue[callable]" = queue.Queue()
        self.overlay.root.after(40, self._pump_ui)

    def _pump_ui(self):
        try:
            while True:
                fn = self.ui_queue.get_nowait()
                try:
                    fn()
                except Exception:
                    pass
        except queue.Empty:
            pass
        self.overlay.root.after(40, self._pump_ui)

    def ui(self, fn, *args, **kwargs):
        self.ui_queue.put(lambda: fn(*args, **kwargs))

    def reload_settings(self):
        self.settings = load_voice_dictation_settings()
        env_model = os.environ.get("VOICE_WHISPER_MODEL", "").strip()
        env_compute = os.environ.get("VOICE_WHISPER_COMPUTE", "").strip()
        if env_model:
            self.settings["whisper_model"] = env_model
        if env_compute:
            self.settings["compute_type"] = env_compute
        model_name = self.settings["whisper_model"]
        if self.model is not None and getattr(self, "_loaded_model_name", None) != model_name:
            self.model = None

    def _transcribe_kwargs(self):
        language_setting = str(self.settings.get("language", "auto") or "auto").strip().lower()
        language = resolve_transcribe_language(language_setting)
        kwargs = {
            "vad_filter": True,
            "vad_parameters": {"min_silence_duration_ms": 250},
            "beam_size": 1,
            "no_speech_threshold": 0.55,
            "condition_on_previous_text": False,
        }
        if language:
            kwargs["language"] = language
        else:
            kwargs["multilingual"] = True
            kwargs["language_detection_segments"] = 2
            kwargs["initial_prompt"] = (
                "Transcribe mixed Swedish and English exactly as spoken. "
                "Keep Swedish words in Swedish and English words in English."
            )
        return kwargs

    def ensure_model(self):
        with self.model_lock:
            if self.model is not None:
                return
            self.reload_settings()
            model_name = self.settings["whisper_model"]
            compute_type = self.settings["compute_type"]
            self.ui(self.overlay.set_status, "loading model...")
            log_event(f"loading whisper model={model_name} compute={compute_type}")
            load_whisper_dependency()
            self.model = WhisperModel(model_name, device="auto", compute_type=compute_type)
            self._loaded_model_name = model_name
            log_event("whisper model ready")
            self.ui(self.overlay.set_status, "listening")

    def _audio_callback(self, indata, frames, time_info, status):
        if status:
            pass
        chunk = indata[:, 0].astype(np.float32, copy=True)
        self.audio_queue.put(chunk)
        rms = float(np.sqrt(np.mean(chunk * chunk) + 1e-9))
        self.ui(self.overlay.set_level, min(rms * 8.0, 1.0))

    def toggle(self):
        sys.stderr.write(f"[toggle] active={self.active}\n"); sys.stderr.flush()
        if self.active:
            self.stop()
        else:
            self.start()

    def start(self):
        if self.active:
            return
        self.reload_settings()
        self.active = True
        self.stop_event.clear()
        self._drain_queue(self.audio_queue)
        self._drain_queue(self.transcribe_queue)
        self.buffer = np.zeros(0, dtype=np.float32)
        self.last_voice_at = time.monotonic()
        self.overlay.show()
        self.overlay.set_status("starting...")
        log_event("dictation start requested")
        self.worker_thread = threading.Thread(target=self._run, daemon=True)
        self.worker_thread.start()

    def stop(self):
        if not self.active:
            return
        log_event("dictation stop requested")
        self.active = False
        self.stop_event.set()
        self.ui(self.overlay.set_status, "transcribing...")
        try:
            if self.stream is not None:
                self.stream.stop()
                self.stream.close()
        except Exception:
            pass
        self.stream = None

    def _drain_queue(self, target):
        try:
            while True:
                target.get_nowait()
                target.task_done()
        except queue.Empty:
            pass

    def _run(self):
        try:
            self.stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                blocksize=int(SAMPLE_RATE * 0.1),
                callback=self._audio_callback,
            )
            self.stream.start()
            self.transcribe_thread = threading.Thread(target=self._transcribe_loop, daemon=True)
            self.transcribe_thread.start()
            log_event("microphone stream started")
        except Exception as exc:
            log_event(f"microphone stream failed: {exc}")
            self.ui(self.overlay.set_status, f"mic failed: {exc}")
            time.sleep(2.0)
            self.ui(self.stop)
            return

        self.ui(self.overlay.set_status, "listening")
        silence_rms = float(self.settings["silence_rms"])
        chunk_seconds = float(self.settings.get("chunk_seconds", 1.6))
        silence_flush_seconds = float(self.settings.get("silence_flush_seconds", 0.7))
        while not self.stop_event.is_set():
            try:
                chunk = self.audio_queue.get(timeout=0.2)
            except queue.Empty:
                chunk = None

            if chunk is not None:
                self.buffer = np.concatenate([self.buffer, chunk])
                rms = float(np.sqrt(np.mean(chunk * chunk) + 1e-9))
                if rms > silence_rms:
                    self.last_voice_at = time.monotonic()
                self.ui(self.overlay.set_level, min(rms * 8.0, 1.0))
            duration = self.buffer.shape[0] / SAMPLE_RATE
            quiet_for = time.monotonic() - self.last_voice_at
            if duration >= chunk_seconds and quiet_for >= silence_flush_seconds:
                self._queue_transcription(self.buffer)
                self.buffer = np.zeros(0, dtype=np.float32)
                self.last_voice_at = time.monotonic()
            elif duration >= MAX_BUFFER_SECONDS:
                self._queue_transcription(self.buffer)
                self.buffer = np.zeros(0, dtype=np.float32)
                self.last_voice_at = time.monotonic()

        if self.buffer.shape[0] / SAMPLE_RATE >= MIN_AUDIO_SECONDS:
            self._queue_transcription(self.buffer)
            self.buffer = np.zeros(0, dtype=np.float32)
        self.transcribe_queue.put(None)
        if self.transcribe_thread is not None:
            self.transcribe_thread.join()
            self.transcribe_thread = None
        self.ui(self.overlay.hide)

    def _queue_transcription(self, audio):
        if audio.shape[0] / SAMPLE_RATE < MIN_AUDIO_SECONDS:
            return
        self.transcribe_queue.put(audio.copy())

    def _transcribe_loop(self):
        while True:
            audio = self.transcribe_queue.get()
            try:
                if audio is None:
                    return
                self._transcribe_and_emit(audio)
            finally:
                self.transcribe_queue.task_done()

    def _transcribe_and_emit(self, audio):
        self.ui(self.overlay.set_status, "transcribing...")
        try:
            self.ensure_model()
            segments, _info = self.model.transcribe(audio, **self._transcribe_kwargs())
            text = "".join(seg.text for seg in segments).strip()
        except Exception as exc:
            log_event(f"transcription failed: {exc}")
            self.ui(self.overlay.set_status, f"asr error: {exc}")
            return
        self.ui(self.overlay.set_status, "listening")
        if not text:
            log_event("transcription returned empty text")
            return
        log_event(f"transcribed {len(text)} chars")
        try:
            delay = max(0, int(self.settings.get("typing_delay_ms", 0))) / 1000.0
            keyboard.write(text + " ", delay=delay)
        except Exception:
            log_event("keyboard.write failed")


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
                data = client.recv(64).decode("utf-8", errors="ignore").strip().lower()
            except OSError:
                data = ""
            finally:
                try:
                    client.close()
                except OSError:
                    pass
            if data == "toggle":
                dictation.ui(dictation.toggle)
            elif data == "start":
                dictation.ui(dictation.start)
            elif data == "stop":
                dictation.ui(dictation.stop)
            elif data == "quit":
                dictation.ui(dictation.stop)
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
    args = parser.parse_args()

    if args.quit:
        send_command("quit")
        return
    if args.stop:
        send_command("stop")
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
        sys.stderr.write("Could not bind voice control port; another instance may be running.\n")
        return

    # If launched with --toggle/--start and no server existed, kick off dictation now
    if args.toggle or args.start:
        dictation.overlay.root.after(50, dictation.start)

    # Pre-load the Whisper model in the background so the first real toggle is instant
    if os.environ.get("VOICE_PRELOAD", "1") != "0":
        def _preload():
            try:
                dictation.ensure_model()
            except Exception as exc:
                sys.stderr.write(f"[preload] {exc}\n"); sys.stderr.flush()
        threading.Thread(target=_preload, daemon=True).start()

    def quit_app():
        try:
            dictation.stop()
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
