"""Low-latency, local Supertonic text-to-speech for streamed Agent replies.

Supertonic stays warm on a worker thread and synthesizes each completed Agent
reply as one continuous waveform. It has no API charge. Windows SAPI is retained
only as an emergency fallback if the local model cannot load.
"""
from __future__ import annotations

import queue
import re
import threading


DEFAULT_VOICE = "M3"
DEFAULT_SPEED = 1.12
DEFAULT_STEPS = 8


class TTSPlayer:
    def __init__(self, model: str = "supertonic-3", voice: str = DEFAULT_VOICE,
                 instructions: str = "", on_log=None, api_key: str | None = None,
                 max_chars: int = 4000, rate: int = 2, speed: float = DEFAULT_SPEED,
                 total_steps: int = DEFAULT_STEPS):
        del instructions, api_key  # Kept for compatibility with the former API TTS.
        self.model = str(model or "supertonic-3")
        self.voice = voice
        self.rate = max(-10, min(10, int(rate)))
        self.speed = max(0.7, min(2.0, float(speed)))
        self.total_steps = max(5, min(12, int(total_steps)))
        self.on_log = on_log or (lambda level, msg: None)
        self.max_chars = max(0, int(max_chars or 0))
        self._q: "queue.Queue[tuple[int, str] | None]" = queue.Queue()
        self._stop = threading.Event()
        self._enabled = True
        self._generation = 0
        self._stream_buffer = ""
        self._stream_spoken = ""
        self._stream_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True, name="aios-local-tts")
        self._thread.start()

    def set_voice(self, voice: str):
        self.voice = str(voice or DEFAULT_VOICE)

    def set_model(self, model: str):
        self.model = str(model or "supertonic-3")

    def set_api_key(self, _api_key: str | None):
        return None

    def enable(self, on: bool):
        self._enabled = bool(on)
        if not self._enabled:
            self.clear()

    def is_enabled(self) -> bool:
        return self._enabled

    def speak(self, text: str):
        """Queue a complete utterance (also used outside the streaming path)."""
        text = self._clean(text)
        if not text or not self._enabled:
            return
        if self.max_chars and len(text) > self.max_chars:
            text = text[: max(1, self.max_chars - 3)].rstrip() + "..."
        self._q.put((self._generation, text))

    def begin_stream(self):
        """Start one reply, dropping speech left over from an older reply."""
        self.clear()
        with self._stream_lock:
            self._stream_buffer = ""
            self._stream_spoken = ""

    def feed(self, delta: str):
        """Feed streamed model text and speak complete short clauses promptly."""
        if not delta or not self._enabled:
            return
        with self._stream_lock:
            self._stream_buffer += str(delta)
            for chunk in self._take_ready_chunks():
                self._stream_spoken += chunk
                self.speak(chunk)

    def end_stream(self, final_text: str = ""):
        """Flush the last partial clause, with final_text as a no-delta fallback."""
        if not self._enabled:
            return
        with self._stream_lock:
            remainder = self._stream_buffer.strip()
            self._stream_buffer = ""
            if remainder:
                self._stream_spoken += remainder
                self.speak(remainder)
            elif not self._stream_spoken.strip() and final_text:
                self.speak(final_text)

    def clear(self):
        """Drop queued utterances and interrupt current playback."""
        self._generation += 1
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass

    def shutdown(self):
        self._stop.set()
        self._q.put(None)

    def _take_ready_chunks(self) -> list[str]:
        ready = []
        while True:
            text = self._stream_buffer
            # Prefer a real sentence boundary, including fairly short sign-offs.
            match = re.search(r"[.!?](?:[\"']?)(?=\s|$)", text)
            cut = match.end() if match else 0
            # A longer comma/semicolon clause starts audio while the rest streams.
            if not cut and len(text) >= 48:
                marks = [text.rfind(mark, 0, min(len(text), 96)) for mark in (",", ";", ":")]
                cut = max(marks) + 1
                if cut < 32:
                    cut = 0
            # Never wait indefinitely for punctuation from dictated-style prose.
            if not cut and len(text) >= 88:
                cut = text.rfind(" ", 48, 88)
                if cut < 0:
                    cut = 88
            if not cut:
                break
            chunk = text[:cut].strip()
            self._stream_buffer = text[cut:].lstrip()
            if chunk:
                ready.append(chunk)
        return ready

    @staticmethod
    def _clean(text: str) -> str:
        text = str(text or "").strip()
        text = re.sub(r"[`*_#]", "", text)
        return re.sub(r"\s+", " ", text)

    @staticmethod
    def _language_for(text: str) -> str:
        lowered = f" {str(text or '').casefold()} "
        if re.search(r"[åäö]", lowered):
            return "sv"
        swedish_words = (" och ", " jag ", " det ", " är ", " inte ", " tack ", " klart ")
        return "sv" if sum(word in lowered for word in swedish_words) >= 2 else "en"

    def _run(self):
        try:
            from supertonic import TTS
            import sounddevice as sd

            model = TTS(
                model=self.model,
                auto_download=True,
                intra_op_num_threads=8,
                inter_op_num_threads=1,
            )
            styles = {}
            self.on_log("info", f"Supertonic ready: {self.voice} at {self.speed:.2f}x")
            while not self._stop.is_set():
                try:
                    item = self._q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if item is None or self._stop.is_set():
                    break
                generation, text = item
                if not self._enabled or generation != self._generation:
                    continue
                try:
                    voice = str(self.voice or DEFAULT_VOICE).upper()
                    if voice not in {f"F{i}" for i in range(1, 6)} | {f"M{i}" for i in range(1, 6)}:
                        voice = DEFAULT_VOICE
                    style = styles.get(voice)
                    if style is None:
                        style = model.get_voice_style(voice_name=voice)
                        styles[voice] = style
                    audio, _duration = model.synthesize(
                        text,
                        voice_style=style,
                        lang=self._language_for(text),
                        total_steps=self.total_steps,
                        speed=self.speed,
                    )
                    if generation != self._generation or not self._enabled:
                        continue
                    sd.play(audio.squeeze(), samplerate=44100, blocking=True)
                except Exception as exc:
                    self.on_log("err", f"Supertonic speech failed: {exc}")
        except Exception as exc:
            self.on_log("err", f"Supertonic unavailable, using Windows fallback: {exc}")
            self._run_sapi_fallback()

    def _run_sapi_fallback(self):
        try:
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            speaker = win32com.client.Dispatch("SAPI.SpVoice")
            speaker.Rate = self.rate
            self.on_log("info", "fallback TTS ready: Microsoft Zira")
            while not self._stop.is_set():
                try:
                    item = self._q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if item is None or self._stop.is_set():
                    break
                generation, text = item
                if self._enabled and generation == self._generation:
                    speaker.Speak(text)
        except Exception as exc:
            self.on_log("err", f"fallback TTS unavailable: {exc}")
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
