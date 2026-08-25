"""Quick audio recorder for aiOS.

Records computer (system) audio and/or microphone, then opens a small editor
where you can trim/crop the clip before saving it to Downloads as a WAV file.

Toggles
-------
- System audio : capture whatever is playing out of the default output device
                 (WASAPI loopback).
- Microphone   : capture the default input device and mix it in.
- Mute Discord : mute Discord's audio session while recording so it is not
                 captured in the system audio (needs `pycaw`). Discord is
                 restored to its previous state when recording stops.

After you press Stop, a waveform editor appears with two draggable handles so
you can crop the start/end, preview the selection, and save.

Run directly:
    pythonw audio_recorder.pyw

First use needs:  pip install sounddevice numpy scipy   (pycaw is optional)
"""

from __future__ import annotations

import sys
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import messagebox

# --- Optional / heavy dependencies are loaded lazily so the window can still
#     report a friendly message instead of crashing on a bare import error. ----
np = None
sd = None
sc = None
resample_poly = None  # optional; numpy fallback is used when unavailable


def load_dependencies():
    """Import audio libraries on demand. Returns a list of missing module names."""
    global np, sd, sc, resample_poly
    if np is not None and sd is not None and sc is not None:
        return []
    missing = []
    try:
        import numpy as numpy_module

        np = numpy_module
    except Exception:  # noqa: BLE001
        missing.append("numpy")
    try:
        import sounddevice as sounddevice_module  # microphone capture

        sd = sounddevice_module
    except Exception:  # noqa: BLE001
        missing.append("sounddevice")
    try:
        import soundcard as soundcard_module  # system (loopback) capture

        sc = soundcard_module
    except Exception:  # noqa: BLE001
        missing.append("soundcard")
    try:
        from scipy.signal import resample_poly as resample_poly_fn

        resample_poly = resample_poly_fn
    except Exception:  # noqa: BLE001
        resample_poly = None  # scipy is optional
    return missing


# --- Theme (matches the aiOS dark overlay) ----------------------------------
COLORS = {
    "bg": "#14161c",
    "panel": "#1b1e26",
    "surface": "#232734",
    "text": "#e7e9ef",
    "muted": "#8a90a2",
    "accent": "#5b8cff",
    "danger": "#ff5d6c",
    "ok": "#46d68a",
    "wave": "#5b8cff",
    "wave_dim": "#3a4258",
}

FONT = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 10, "bold")
FONT_BIG = ("Segoe UI", 16, "bold")


def downloads_dir() -> Path:
    path = Path.home() / "Downloads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def unique_download_path(stem: str, suffix: str) -> Path:
    folder = downloads_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = folder / f"{stem}-{stamp}{suffix}"
    if not candidate.exists():
        return candidate
    for index in range(2, 1000):
        candidate = folder / f"{stem}-{stamp}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise FileExistsError("Could not allocate a Downloads filename.")


# --- Discord muting via pycaw (optional) ------------------------------------
class ProcessMuter:
    """Mute audio sessions for the given process names while active."""

    def __init__(self, name_fragments):
        self.name_fragments = [n.lower() for n in name_fragments]
        self._restored = []  # (SimpleAudioVolume, previous_mute_state)

    @staticmethod
    def available() -> bool:
        try:
            import pycaw  # noqa: F401

            return True
        except Exception:  # noqa: BLE001
            return False

    def mute(self):
        try:
            from pycaw.pycaw import AudioUtilities
        except Exception:  # noqa: BLE001
            return
        try:
            sessions = AudioUtilities.GetAllSessions()
        except Exception:  # noqa: BLE001
            return
        for session in sessions:
            proc = session.Process
            if not proc:
                continue
            try:
                name = (proc.name() or "").lower()
            except Exception:  # noqa: BLE001
                continue
            if not any(frag in name for frag in self.name_fragments):
                continue
            volume = session.SimpleAudioVolume
            if volume is None:
                continue
            try:
                previous = volume.GetMute()
                volume.SetMute(1, None)
                self._restored.append((volume, previous))
            except Exception:  # noqa: BLE001
                continue

    def restore(self):
        for volume, previous in self._restored:
            try:
                volume.SetMute(previous, None)
            except Exception:  # noqa: BLE001
                pass
        self._restored = []


# --- Audio capture -----------------------------------------------------------
def _wasapi_devices():
    """Return (output_index, input_index) for the WASAPI host API, or (None, None)."""
    try:
        host_apis = sd.query_hostapis()
    except Exception:  # noqa: BLE001
        return None, None
    for host in host_apis:
        if "WASAPI" in host.get("name", ""):
            out = host.get("default_output_device", -1)
            inp = host.get("default_input_device", -1)
            return (out if out >= 0 else None, inp if inp >= 0 else None)
    return None, None


TARGET_RATE = 48000


def _to_stereo(buffer):
    """Coerce an (n, channels) float32 array to (n, 2)."""
    if buffer.ndim == 1:
        buffer = buffer[:, None]
    channels = buffer.shape[1]
    if channels == 1:
        return np.repeat(buffer, 2, axis=1)
    if channels == 2:
        return buffer
    return buffer[:, :2]


def _resample(buffer, src_rate, dst_rate):
    """Resample (n, channels) to dst_rate. Uses scipy if present, else numpy."""
    if src_rate == dst_rate or buffer.shape[0] == 0:
        return buffer
    if resample_poly is not None:
        return resample_poly(buffer, dst_rate, src_rate, axis=0).astype(np.float32)
    n_out = int(round(buffer.shape[0] * dst_rate / src_rate))
    src_idx = np.arange(buffer.shape[0])
    out_idx = np.linspace(0, buffer.shape[0] - 1, n_out)
    out = np.empty((n_out, buffer.shape[1]), dtype=np.float32)
    for channel in range(buffer.shape[1]):
        out[:, channel] = np.interp(out_idx, src_idx, buffer[:, channel])
    return out


class Recorder:
    """Captures system audio via soundcard (loopback) and the mic via sounddevice."""

    def __init__(self, record_system: bool, record_mic: bool):
        self.record_system = record_system
        self.record_mic = record_mic
        self._mic_stream = None
        self._sys_thread = None
        self._running = False
        self._sys_frames = []
        self._mic_frames = []
        self.mic_rate = TARGET_RATE
        self.peak = 0.0  # most recent block peak (0..1) for the level meter
        self.error = None  # set if the loopback thread dies

    def start(self):
        self._running = True

        if self.record_system:
            self._sys_thread = threading.Thread(target=self._system_loop, daemon=True)
            self._sys_thread.start()

        if self.record_mic:
            _, in_dev = _wasapi_devices()
            device = in_dev if in_dev is not None else sd.default.device[0]
            info = sd.query_devices(device)
            self.mic_rate = int(info["default_samplerate"]) or TARGET_RATE
            channels = max(1, min(2, int(info["max_input_channels"]) or 1))
            self._mic_stream = sd.InputStream(
                samplerate=self.mic_rate,
                device=device,
                channels=channels,
                dtype="float32",
                callback=self._mic_callback,
            )
            self._mic_stream.start()

        if not self.record_system and not self.record_mic:
            raise RuntimeError("Nothing selected to record.")

    def _system_loop(self):
        # soundcard talks to WASAPI through COM, which must be initialized on
        # this worker thread (otherwise CO_E_NOTINITIALIZED / 0x800401f0).
        com_ready = False
        try:
            import comtypes

            comtypes.CoInitialize()
            com_ready = True
        except Exception:  # noqa: BLE001
            pass
        try:
            speaker = sc.default_speaker()
            loopback = sc.get_microphone(speaker.name, include_loopback=True)
            with loopback.recorder(samplerate=TARGET_RATE, channels=2) as recorder:
                while self._running:
                    block = recorder.record(numframes=2048)
                    self._sys_frames.append(block)
                    self.peak = float(np.abs(block).max())
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
        finally:
            if com_ready:
                try:
                    comtypes.CoUninitialize()
                except Exception:  # noqa: BLE001
                    pass

    def _mic_callback(self, indata, _frames, _time, _status):
        block = indata.copy()
        self._mic_frames.append(block)
        if not self.record_system:  # let the mic drive the meter when alone
            try:
                self.peak = float(np.abs(block).max())
            except ValueError:
                pass

    def stop(self):
        self._running = False
        if self._sys_thread is not None:
            self._sys_thread.join(timeout=2.0)
            self._sys_thread = None
        if self._mic_stream is not None:
            try:
                self._mic_stream.stop()
                self._mic_stream.close()
            except Exception:  # noqa: BLE001
                pass
            self._mic_stream = None

    def finalize(self):
        """Mix the captured streams into (samples float32 [-1,1] stereo, rate)."""
        sys_audio = (
            np.concatenate(self._sys_frames, axis=0) if self._sys_frames else None
        )
        mic_audio = (
            np.concatenate(self._mic_frames, axis=0) if self._mic_frames else None
        )

        tracks = []
        if sys_audio is not None:
            tracks.append(_to_stereo(sys_audio))  # already at TARGET_RATE
        if mic_audio is not None:
            mic_stereo = _to_stereo(mic_audio)
            mic_stereo = _resample(mic_stereo, self.mic_rate, TARGET_RATE)
            tracks.append(mic_stereo)

        if not tracks:
            return np.zeros((0, 2), dtype=np.float32), TARGET_RATE

        length = max(track.shape[0] for track in tracks)
        mix = np.zeros((length, 2), dtype=np.float32)
        for track in tracks:
            mix[: track.shape[0]] += track
        np.clip(mix, -1.0, 1.0, out=mix)
        return mix, TARGET_RATE


def write_wav(path: Path, samples, rate: int):
    """Write float32 [-1,1] stereo samples to a 16-bit PCM WAV file."""
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(int(rate))
        wav.writeframes(pcm.tobytes())


# --- GUI ---------------------------------------------------------------------
class AudioRecorderApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("aiOS Audio Recorder")
        self.root.configure(bg=COLORS["bg"])
        self.root.attributes("-topmost", True)
        self.root.resizable(False, False)

        self.recorder = None
        self.muter = None
        self.started_at = 0.0
        self.timer_job = None
        self.samples = None
        self.rate = 48000

        # selection bounds (sample indices) for the editor
        self.sel_start = 0
        self.sel_end = 0
        self._drag_handle = None

        self.record_system = tk.BooleanVar(value=True)
        self.record_mic = tk.BooleanVar(value=False)
        self.mute_discord = tk.BooleanVar(value=False)

        self.container = tk.Frame(self.root, bg=COLORS["bg"], padx=18, pady=16)
        self.container.pack(fill="both", expand=True)

        missing = load_dependencies()
        if missing:
            self._build_missing_view(missing)
        else:
            self._build_setup_view()

        self._center()

    # -- helpers --
    def _center(self):
        self.root.update_idletasks()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"+{(sw - w) // 2}+{(sh - h) // 3}")

    def _clear(self):
        for child in self.container.winfo_children():
            child.destroy()

    def _chip(self, parent, text, command, kind="default"):
        bg = COLORS["surface"]
        fg = COLORS["text"]
        if kind == "accent":
            bg, fg = COLORS["accent"], "#0b1020"
        elif kind == "danger":
            bg, fg = COLORS["danger"], "#ffffff"
        elif kind == "ok":
            bg, fg = COLORS["ok"], "#06231a"
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=bg,
            activeforeground=fg,
            relief="flat",
            bd=0,
            padx=16,
            pady=8,
            cursor="hand2",
            font=FONT_BOLD,
        )

    def _check(self, parent, text, var, state="normal"):
        return tk.Checkbutton(
            parent,
            text=text,
            variable=var,
            onvalue=True,
            offvalue=False,
            state=state,
            bg=COLORS["bg"],
            fg=COLORS["text"] if state == "normal" else COLORS["muted"],
            selectcolor=COLORS["surface"],
            activebackground=COLORS["bg"],
            activeforeground=COLORS["text"],
            font=FONT,
            anchor="w",
        )

    # -- views --
    def _build_missing_view(self, missing):
        self._clear()
        tk.Label(
            self.container,
            text="Missing dependencies",
            bg=COLORS["bg"],
            fg=COLORS["danger"],
            font=FONT_BIG,
        ).pack(anchor="w")
        tk.Label(
            self.container,
            text="Install them, then reopen the recorder:\n\n"
            f"pip install {' '.join(missing)}",
            bg=COLORS["bg"],
            fg=COLORS["text"],
            font=FONT,
            justify="left",
        ).pack(anchor="w", pady=(8, 14))
        self._chip(self.container, "Close", self.root.destroy).pack(anchor="e")

    def _build_setup_view(self):
        self._clear()
        tk.Label(
            self.container,
            text="Record audio",
            bg=COLORS["bg"],
            fg=COLORS["text"],
            font=FONT_BIG,
        ).pack(anchor="w")
        tk.Label(
            self.container,
            text="Choose what to capture, then start.",
            bg=COLORS["bg"],
            fg=COLORS["muted"],
            font=FONT,
        ).pack(anchor="w", pady=(2, 12))

        self._check(self.container, "System audio (computer sound)", self.record_system).pack(
            anchor="w", fill="x"
        )
        self._check(self.container, "Microphone", self.record_mic).pack(anchor="w", fill="x")

        if ProcessMuter.available():
            self._check(
                self.container,
                "Mute Discord while recording",
                self.mute_discord,
            ).pack(anchor="w", fill="x")
        else:
            self.mute_discord.set(False)
            row = tk.Frame(self.container, bg=COLORS["bg"])
            row.pack(anchor="w", fill="x")
            self._check(row, "Mute Discord while recording", self.mute_discord, state="disabled").pack(
                side="left"
            )
            tk.Label(
                row,
                text="(needs: pip install pycaw)",
                bg=COLORS["bg"],
                fg=COLORS["muted"],
                font=("Segoe UI", 8),
            ).pack(side="left", padx=(4, 0))

        buttons = tk.Frame(self.container, bg=COLORS["bg"])
        buttons.pack(fill="x", pady=(16, 0))
        self._chip(buttons, "● Record", self.start_recording, kind="danger").pack(side="left")
        self._chip(buttons, "Cancel", self.root.destroy).pack(side="right")

    def _build_recording_view(self):
        self._clear()
        self.time_label = tk.Label(
            self.container,
            text="● REC  00:00",
            bg=COLORS["bg"],
            fg=COLORS["danger"],
            font=FONT_BIG,
        )
        self.time_label.pack(anchor="w")

        sources = []
        if self.record_system.get():
            sources.append("system")
        if self.record_mic.get():
            sources.append("mic")
        if self.mute_discord.get():
            sources.append("Discord muted")
        tk.Label(
            self.container,
            text="Recording " + " + ".join(sources),
            bg=COLORS["bg"],
            fg=COLORS["muted"],
            font=FONT,
        ).pack(anchor="w", pady=(2, 10))

        self.level_canvas = tk.Canvas(
            self.container,
            width=300,
            height=14,
            bg=COLORS["surface"],
            highlightthickness=0,
        )
        self.level_canvas.pack(anchor="w", pady=(0, 14))
        self._level_rect = self.level_canvas.create_rectangle(
            0, 0, 0, 14, fill=COLORS["ok"], width=0
        )

        self._chip(self.container, "■ Stop", self.stop_recording, kind="accent").pack(anchor="w")

    def _build_editor_view(self):
        self._clear()
        duration = len(self.samples) / self.rate if self.rate else 0
        tk.Label(
            self.container,
            text="Crop & save",
            bg=COLORS["bg"],
            fg=COLORS["text"],
            font=FONT_BIG,
        ).pack(anchor="w")
        self.editor_hint = tk.Label(
            self.container,
            text=f"Drag the handles to trim.  Length: {duration:5.1f}s",
            bg=COLORS["bg"],
            fg=COLORS["muted"],
            font=FONT,
        )
        self.editor_hint.pack(anchor="w", pady=(2, 10))

        self.wave_w = 460
        self.wave_h = 120
        self.wave_canvas = tk.Canvas(
            self.container,
            width=self.wave_w,
            height=self.wave_h,
            bg=COLORS["panel"],
            highlightthickness=1,
            highlightbackground=COLORS["surface"],
            cursor="sb_h_double_arrow",
        )
        self.wave_canvas.pack(anchor="w")
        self.wave_canvas.bind("<ButtonPress-1>", self._editor_press)
        self.wave_canvas.bind("<B1-Motion>", self._editor_drag)
        self.wave_canvas.bind("<ButtonRelease-1>", lambda _e: setattr(self, "_drag_handle", None))

        self.sel_start = 0
        self.sel_end = len(self.samples)
        self._draw_waveform()
        self._draw_selection()

        buttons = tk.Frame(self.container, bg=COLORS["bg"])
        buttons.pack(fill="x", pady=(14, 0))
        self._chip(buttons, "▶ Play", self.play_selection).pack(side="left")
        self._chip(buttons, "■", self.stop_playback).pack(side="left", padx=(6, 0))
        self._chip(buttons, "↺ Re-record", self.reset_to_setup).pack(side="left", padx=(6, 0))
        self._chip(buttons, "Save to Downloads", self.save_selection, kind="ok").pack(side="right")

    # -- recording flow --
    def start_recording(self):
        if not self.record_system.get() and not self.record_mic.get():
            messagebox.showinfo(
                "Nothing selected",
                "Pick system audio, microphone, or both.",
                parent=self.root,
            )
            return

        if self.mute_discord.get():
            self.muter = ProcessMuter(["discord"])
            self.muter.mute()

        self.recorder = Recorder(self.record_system.get(), self.record_mic.get())
        try:
            self.recorder.start()
        except Exception as exc:  # noqa: BLE001
            if self.muter:
                self.muter.restore()
                self.muter = None
            messagebox.showerror("Recording failed", str(exc), parent=self.root)
            return

        self.started_at = time.perf_counter()
        self._build_recording_view()
        self._tick()

    def _tick(self):
        if self.recorder is None:
            return
        elapsed = int(time.perf_counter() - self.started_at)
        minutes, seconds = divmod(elapsed, 60)
        dot = "●" if (elapsed % 2 == 0) else " "
        try:
            self.time_label.configure(text=f"{dot} REC  {minutes:02d}:{seconds:02d}")
            width = int(min(1.0, self.recorder.peak * 1.4) * 300)
            self.level_canvas.coords(self._level_rect, 0, 0, width, 14)
            color = COLORS["danger"] if self.recorder.peak > 0.92 else COLORS["ok"]
            self.level_canvas.itemconfigure(self._level_rect, fill=color)
        except tk.TclError:
            return
        self.timer_job = self.root.after(120, self._tick)

    def stop_recording(self):
        if self.timer_job is not None:
            self.root.after_cancel(self.timer_job)
            self.timer_job = None
        if self.recorder is None:
            return
        self.recorder.stop()
        if self.muter:
            self.muter.restore()
            self.muter = None
        error = self.recorder.error
        self.samples, self.rate = self.recorder.finalize()
        self.recorder = None

        if error and (self.samples is None or len(self.samples) == 0):
            messagebox.showerror("Recording failed", error, parent=self.root)
            self._build_setup_view()
            return

        if self.samples is None or len(self.samples) == 0:
            messagebox.showwarning(
                "Empty recording",
                "No audio was captured. If you recorded system audio, make sure "
                "something was actually playing.",
                parent=self.root,
            )
            self._build_setup_view()
            return
        self._build_editor_view()

    def reset_to_setup(self):
        self.stop_playback()
        self.samples = None
        self._build_setup_view()

    # -- editor --
    def _x_to_index(self, x):
        x = max(0, min(self.wave_w, x))
        return int(x / self.wave_w * len(self.samples))

    def _index_to_x(self, index):
        if not len(self.samples):
            return 0
        return index / len(self.samples) * self.wave_w

    def _draw_waveform(self):
        canvas = self.wave_canvas
        canvas.delete("wave")
        mono = self.samples.mean(axis=1)
        total = len(mono)
        if total == 0:
            return
        mid = self.wave_h / 2
        per_px = max(1, total // self.wave_w)
        for x in range(self.wave_w):
            start = x * per_px
            end = min(total, start + per_px)
            if start >= end:
                break
            chunk = mono[start:end]
            hi = float(chunk.max()) * mid
            lo = float(chunk.min()) * mid
            canvas.create_line(
                x, mid - hi, x, mid - lo, fill=COLORS["wave"], tags="wave"
            )

    def _draw_selection(self):
        canvas = self.wave_canvas
        canvas.delete("sel")
        x0 = self._index_to_x(self.sel_start)
        x1 = self._index_to_x(self.sel_end)
        # dim the trimmed-away regions
        if x0 > 0:
            canvas.create_rectangle(
                0, 0, x0, self.wave_h, fill=COLORS["bg"], stipple="gray50", width=0, tags="sel"
            )
        if x1 < self.wave_w:
            canvas.create_rectangle(
                x1, 0, self.wave_w, self.wave_h, fill=COLORS["bg"], stipple="gray50", width=0, tags="sel"
            )
        for x in (x0, x1):
            canvas.create_line(x, 0, x, self.wave_h, fill=COLORS["accent"], width=3, tags="sel")
        canvas.tag_raise("sel")

    def _editor_press(self, event):
        x0 = self._index_to_x(self.sel_start)
        x1 = self._index_to_x(self.sel_end)
        self._drag_handle = "start" if abs(event.x - x0) <= abs(event.x - x1) else "end"
        self._editor_drag(event)

    def _editor_drag(self, event):
        if self._drag_handle is None:
            return
        index = self._x_to_index(event.x)
        if self._drag_handle == "start":
            self.sel_start = min(index, self.sel_end - 1)
            self.sel_start = max(0, self.sel_start)
        else:
            self.sel_end = max(index, self.sel_start + 1)
            self.sel_end = min(len(self.samples), self.sel_end)
        self._draw_selection()
        sel_seconds = (self.sel_end - self.sel_start) / self.rate
        self.editor_hint.configure(
            text=f"Drag the handles to trim.  Selection: {sel_seconds:5.1f}s"
        )

    def _selection_samples(self):
        return self.samples[self.sel_start : self.sel_end]

    def play_selection(self):
        try:
            sd.stop()
            sd.play(self._selection_samples(), self.rate)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Playback failed", str(exc), parent=self.root)

    def stop_playback(self):
        try:
            sd.stop()
        except Exception:  # noqa: BLE001
            pass

    def save_selection(self):
        self.stop_playback()
        selection = self._selection_samples()
        if len(selection) == 0:
            messagebox.showwarning("Empty selection", "Nothing to save.", parent=self.root)
            return
        try:
            path = unique_download_path("aios-audio", ".wav")
            write_wav(path, selection, self.rate)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Save failed", str(exc), parent=self.root)
            return
        self.editor_hint.configure(text=f"Saved → {path.name}", fg=COLORS["ok"])
        try:
            import os

            os.startfile(str(path.parent))
        except Exception:  # noqa: BLE001
            pass
        self.root.after(900, self.root.destroy)

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _on_close(self):
        if self.recorder is not None:
            self.recorder.stop()
        if self.muter:
            self.muter.restore()
        self.stop_playback()
        self.root.destroy()


def main():
    app = AudioRecorderApp()
    app.run()


if __name__ == "__main__":
    main()
