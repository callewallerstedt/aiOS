"""Text-to-speech via OpenAI gpt-4o-mini-tts + Windows winsound.

Queue-based: enqueue speak(text), worker thread fetches WAV from OpenAI and
plays it blocking (so phrases never overlap). New requests queue up; pause /
clear stops the queue cleanly.
"""
from __future__ import annotations
import queue
import struct
import threading
import time
import winsound

from agent.vlm import client as oai_client


# OpenAI's PCM format spec: 24 kHz, 16-bit, mono, little-endian, signed.
_PCM_RATE = 24000
_PCM_BITS = 16
_PCM_CHANNELS = 1


def _pcm_to_wav(pcm: bytes) -> bytes:
    """Wrap raw PCM bytes with a valid RIFF/WAVE header winsound can play."""
    byte_rate = _PCM_RATE * _PCM_CHANNELS * (_PCM_BITS // 8)
    block_align = _PCM_CHANNELS * (_PCM_BITS // 8)
    data_len = len(pcm)
    riff_size = 36 + data_len
    header = (
        b"RIFF" + struct.pack("<I", riff_size) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH",
                                16, 1, _PCM_CHANNELS, _PCM_RATE,
                                byte_rate, block_align, _PCM_BITS)
        + b"data" + struct.pack("<I", data_len)
    )
    return header + pcm

# Latest TTS model with steerable voice; very smooth, low latency.
DEFAULT_MODEL = "gpt-4o-mini-tts"
DEFAULT_VOICE = "nova"   # nova, coral, shimmer, sage, alloy, echo, fable, onyx, ash, ballad, verse
DEFAULT_INSTRUCTIONS = ("Speak calmly and naturally in a warm, conversational tone. "
                        "Keep a brisk pace — you are narrating what an assistant is "
                        "doing right now, not reading a story.")


class TTSPlayer:
    def __init__(self, model: str = DEFAULT_MODEL, voice: str = DEFAULT_VOICE,
                 instructions: str = DEFAULT_INSTRUCTIONS,
                 on_log=None):
        self.model = model
        self.voice = voice
        self.instructions = instructions
        self.on_log = on_log or (lambda level, msg: None)
        self._q: "queue.Queue[str | None]" = queue.Queue()
        self._stop = threading.Event()
        self._enabled = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # --------- public ---------

    def set_voice(self, voice: str):
        self.voice = voice

    def set_model(self, model: str):
        self.model = model

    def enable(self, on: bool):
        self._enabled = on
        if not on:
            self.clear()

    def is_enabled(self) -> bool:
        return self._enabled

    def speak(self, text: str):
        text = (text or "").strip()
        if not text or not self._enabled:
            return
        # Trim to keep narration snappy.
        if len(text) > 220:
            text = text[:217].rstrip() + "..."
        self._q.put(text)

    def clear(self):
        """Drop any queued utterances and stop the current sound (best-effort)."""
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass

    def shutdown(self):
        self._stop.set()
        self._q.put(None)

    # --------- worker ---------

    def _run(self):
        while not self._stop.is_set():
            try:
                text = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if text is None or self._stop.is_set():
                return
            if not self._enabled:
                continue
            try:
                wav = self._synthesize(text)
                # Blocking play so subsequent items don't overlap.
                winsound.PlaySound(wav, winsound.SND_MEMORY)
            except Exception as e:
                self.on_log("err", f"TTS failed: {e}")

    def _synthesize(self, text: str) -> bytes:
        # Ask for raw PCM and wrap with a clean WAV header. OpenAI's "wav"
        # format streams with placeholder chunk sizes that winsound rejects.
        kwargs = dict(
            model=self.model,
            voice=self.voice,
            input=text,
            response_format="pcm",
        )
        if self.model.startswith("gpt-4o"):
            kwargs["instructions"] = self.instructions
        resp = oai_client().audio.speech.create(**kwargs)
        pcm = resp.read() if hasattr(resp, "read") else resp.content
        return _pcm_to_wav(pcm)
