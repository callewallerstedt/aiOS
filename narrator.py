"""Screen narration helpers for aiOS.

Captures a monitor, asks an OpenAI vision model for a short bilingual
description, displays it in a transparent overlay, and speaks the learning
language with OpenAI text-to-speech.
"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from difflib import SequenceMatcher
import io
import json
from pathlib import Path
import queue
import random
import struct
import threading
import tkinter as tk
import urllib.error
import urllib.request

import mss
from PIL import Image, ImageDraw, ImageFont


TRANSPARENT = "#010203"
DEFAULT_MODEL = "gpt-5-nano"
DEFAULT_TTS_MODEL = "gpt-4o-mini-tts"
DEFAULT_VOICE = "marin"

DIFFICULTY_LEVELS = (
    "Complete beginner",
    "Beginner",
    "Intermediate",
    "Advanced",
)

LENGTH_OPTIONS = (
    "Very short",
    "Short",
    "Medium",
)

LANGUAGES = (
    "English",
    "Swedish",
    "Spanish",
    "French",
    "German",
    "Italian",
    "Portuguese",
    "Dutch",
    "Norwegian",
    "Danish",
    "Finnish",
    "Polish",
    "Czech",
    "Romanian",
    "Greek",
    "Turkish",
    "Russian",
    "Ukrainian",
    "Arabic",
    "Hebrew",
    "Hindi",
    "Chinese",
    "Japanese",
    "Korean",
    "Vietnamese",
    "Thai",
    "Indonesian",
    "Swahili",
)

VOICES = (
    "marin",
    "cedar",
    "coral",
    "nova",
    "alloy",
    "ash",
    "ballad",
    "echo",
    "fable",
    "onyx",
    "sage",
    "shimmer",
    "verse",
)

FONT_FAMILIES = (
    "Segoe UI",
    "Arial",
    "Calibri",
    "Verdana",
    "Tahoma",
    "Times New Roman",
    "Consolas",
)


DEFAULT_CONFIG = {
    "model": DEFAULT_MODEL,
    "learning_language": "Spanish",
    "known_language": "English",
    "difficulty": "Complete beginner",
    "length": "Very short",
    "custom_prompt": "",
    "interval_seconds": 15,
    "monitor": 1,
    "tts": True,
    "voice": DEFAULT_VOICE,
    "overlay": True,
    "font_family": "Segoe UI",
    "learning_font_size": 30,
    "known_font_size": 21,
    "learning_color": "#FFFFFF",
    "known_color": "#61DAFB",
    "outline_color": "#000000",
    "outline_size": 4,
    "opacity": 100,
    "line_gap": 28,
    "bottom_offset": 46,
}


@dataclass(frozen=True)
class Monitor:
    index: int
    left: int
    top: int
    width: int
    height: int
    label: str


def list_monitors() -> list[Monitor]:
    monitors: list[Monitor] = []
    with mss.mss() as capture:
        for index, item in enumerate(capture.monitors):
            name = "All monitors" if index == 0 else f"Monitor {index}"
            label = f"{name} - {item['width']}x{item['height']} @ ({item['left']}, {item['top']})"
            monitors.append(
                Monitor(
                    index=index,
                    left=int(item["left"]),
                    top=int(item["top"]),
                    width=int(item["width"]),
                    height=int(item["height"]),
                    label=label,
                )
            )
    return monitors


def capture_monitor(monitor: Monitor) -> Image.Image:
    region = {
        "left": monitor.left,
        "top": monitor.top,
        "width": monitor.width,
        "height": monitor.height,
    }
    from agent_clicker.desktop_agent.screen import current_process_windows_hidden_from_agent_capture

    with current_process_windows_hidden_from_agent_capture():
        with mss.mss() as capture:
            raw = capture.grab(region)
    return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")


def image_data_url(image: Image.Image, max_dimension: int = 1280, quality: int = 72) -> str:
    image = image.convert("RGB")
    if max(image.size) > max_dimension:
        image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=quality, optimize=True)
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _api_json(url: str, api_key: str, payload: dict, timeout: int = 90) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(detail).get("error", {}).get("message", detail)
        except json.JSONDecodeError:
            message = detail or str(exc)
        raise RuntimeError(message) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error: {exc.reason}") from exc


def _parse_json_object(text: str) -> dict:
    value = (text or "").strip()
    if value.startswith("```"):
        value = value.strip("`").strip()
        if value.lower().startswith("json"):
            value = value[4:].strip()
    try:
        result = json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        if start < 0:
            raise ValueError("The narrator model did not return JSON.")
        result, _ = json.JSONDecoder().raw_decode(value[start:])
    if not isinstance(result, dict):
        raise ValueError("The narrator model returned an invalid response.")
    return result


def _clean_line(value, fallback: str = "") -> str:
    text = " ".join(str(value or fallback).split()).strip()
    return text[:360]


def request_narration(
    api_key: str,
    model: str,
    learning_language: str,
    known_language: str,
    difficulty: str,
    length: str,
    custom_prompt: str,
    screenshot_data_url: str,
    history: list[dict],
) -> dict:
    recent = [
        {
            "learning": _clean_line(item.get("learning")),
            "known": _clean_line(item.get("known")),
            "focus": _clean_line(item.get("focus")),
            "angle": _clean_line(item.get("angle")),
        }
        for item in history[-8:]
        if isinstance(item, dict)
    ]
    teaching_angles = (
        "Notice a different visible object and teach its useful everyday name.",
        "Focus on a useful action verb shown by what the user or screen is doing.",
        "Teach a simple position, direction, color, or spatial phrase from the scene.",
        "Make a brief friendly reaction or playful observation about a visible detail.",
        "Teach one practical phrase the learner could use in a similar situation.",
        "Point out a small background detail that has not been mentioned recently.",
        "Use a tiny friendly question or guess about what may happen next.",
    )
    recent_angles = {str(item.get("angle") or "") for item in history[-3:] if isinstance(item, dict)}
    available_angles = [item for item in teaching_angles if item not in recent_angles] or list(teaching_angles)
    angle = random.SystemRandom().choice(available_angles)
    length_guidance = {
        "Very short": "Use one short sentence, usually 4 to 10 words.",
        "Short": "Use one concise sentence, usually 8 to 16 words.",
        "Medium": "Use one or two concise sentences, no more than 28 words total.",
    }.get(length, "Use one short sentence, usually 4 to 10 words.")
    difficulty_guidance = {
        "Complete beginner": (
            "Use the most basic everyday vocabulary and simple present-tense grammar. "
            "Avoid idioms, slang, and complex sentence structures."
        ),
        "Beginner": "Use common everyday vocabulary and simple grammar with one clear idea.",
        "Intermediate": "Use natural everyday vocabulary with some variety, while staying easy to follow.",
        "Advanced": "Use natural fluent language, but remain concise and conversational.",
    }.get(difficulty, "Use the most basic everyday vocabulary and simple grammar.")
    system = (
        "You are a friendly live screen narrator and language-learning companion. React like a calm, "
        "supportive friend watching beside the user: warm, casual, observant, and never robotic. "
        "Comment on the scene instead of mechanically listing it. Use recent narration for continuity, "
        "but actively choose a different detail, object, verb, phrase, or teaching angle than the recent "
        "comments. Never repeat the same opening, main noun, or observation twice in a row. If little "
        "changed, look for a useful background detail or teach a related everyday phrase. Do not read passwords, "
        "API keys, payment details, private messages, or long identifiers aloud; describe sensitive "
        "areas generically. Return only a JSON object with exactly three string fields: learning_text, "
        "known_text, and focus. Both language fields must express the same meaning. focus is a short "
        "English label for the detail or teaching topic you chose. Do not use markdown."
    )
    custom_instruction = _clean_line(custom_prompt) or "No extra request."
    prompt = (
        f"Learning language: {learning_language}\n"
        f"Known/translation language: {known_language}\n"
        f"Learner difficulty: {difficulty}. {difficulty_guidance}\n"
        f"Response length: {length}. {length_guidance}\n"
        f"User's extra direction: {custom_instruction}\n"
        f"Teaching angle for this turn: {angle}\n"
        f"Recent narration: {json.dumps(recent, ensure_ascii=False)}\n"
        "Narrate what is happening now. Avoid the recent topics and wording. Keep it useful, lively, "
        "friendly, and easy to say aloud."
    )
    payload = {
        "model": model or DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": screenshot_data_url, "detail": "low"},
                    },
                ],
            },
        ],
        "response_format": {"type": "json_object"},
        "max_completion_tokens": 180,
    }
    if str(model).casefold().startswith("gpt-5"):
        payload["reasoning_effort"] = "minimal"
    def send(current_payload):
        try:
            return _api_json("https://api.openai.com/v1/chat/completions", api_key, current_payload)
        except RuntimeError as exc:
            if "reasoning_effort" not in str(exc):
                raise
            current_payload.pop("reasoning_effort", None)
            return _api_json("https://api.openai.com/v1/chat/completions", api_key, current_payload)

    response = send(payload)
    choices = response.get("choices") or []
    if not choices:
        raise RuntimeError("The narrator model returned no response.")
    content = (choices[0].get("message") or {}).get("content") or ""
    result = _parse_json_object(content)
    learning = _clean_line(result.get("learning_text"))
    known = _clean_line(result.get("known_text"))
    focus = _clean_line(result.get("focus"), "new detail")
    if not learning or not known:
        raise RuntimeError("The narrator response was missing one of the two languages.")
    if _too_similar(known, recent):
        retry_payload = json.loads(json.dumps(payload))
        retry_payload["messages"][1]["content"][0]["text"] += (
            f"\nYour first draft was too similar to recent narration: {known!r}. "
            "Try once more. Choose a clearly different visible detail or teaching topic and different wording."
        )
        retry_response = send(retry_payload)
        retry_choices = retry_response.get("choices") or []
        if retry_choices:
            retry_content = (retry_choices[0].get("message") or {}).get("content") or ""
            retry_result = _parse_json_object(retry_content)
            retry_learning = _clean_line(retry_result.get("learning_text"))
            retry_known = _clean_line(retry_result.get("known_text"))
            if retry_learning and retry_known:
                response = retry_response
                learning = retry_learning
                known = retry_known
                focus = _clean_line(retry_result.get("focus"), "different detail")
    usage = response.get("usage") or {}
    return {
        "learning": learning,
        "known": known,
        "focus": focus,
        "angle": angle,
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
    }


def _too_similar(text: str, recent: list[dict]) -> bool:
    normalized = " ".join(str(text or "").casefold().split())
    if not normalized:
        return False
    words = set(normalized.split())
    for item in recent[-6:]:
        previous = " ".join(str(item.get("known") or "").casefold().split())
        if not previous:
            continue
        ratio = SequenceMatcher(None, normalized, previous).ratio()
        previous_words = set(previous.split())
        union = words | previous_words
        overlap = len(words & previous_words) / len(union) if union else 0.0
        if ratio >= 0.68 or overlap >= 0.72:
            return True
    return False


def _pcm_to_wav(pcm: bytes) -> bytes:
    rate = 24000
    bits = 16
    channels = 1
    byte_rate = rate * channels * (bits // 8)
    block_align = channels * (bits // 8)
    header = (
        b"RIFF"
        + struct.pack("<I", 36 + len(pcm))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, channels, rate, byte_rate, block_align, bits)
        + b"data"
        + struct.pack("<I", len(pcm))
    )
    return header + pcm


class TTSPlayer:
    def __init__(self, on_error=None):
        self.api_key = ""
        self.model = DEFAULT_TTS_MODEL
        self.voice = DEFAULT_VOICE
        self.learning_language = "Spanish"
        self.enabled = False
        self.on_error = on_error or (lambda _message: None)
        self._queue: queue.Queue[tuple[str, object, int] | None] = queue.Queue()
        self._stop = threading.Event()
        self._generation = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def configure(self, api_key: str, voice: str, learning_language: str, enabled: bool):
        self.api_key = api_key
        self.voice = voice or DEFAULT_VOICE
        self.learning_language = learning_language or "the selected language"
        self.enabled = bool(enabled)
        if not self.enabled:
            self.clear()

    def speak(self, text: str, on_start=None):
        if not self.enabled or not self.api_key or not text.strip():
            return False
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        self._generation += 1
        self._queue.put((text.strip()[:500], on_start, self._generation))
        return True

    def clear(self):
        self._generation += 1
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        try:
            import winsound

            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass

    def shutdown(self):
        self._stop.set()
        self.clear()
        self._queue.put(None)

    def _run(self):
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None or self._stop.is_set():
                return
            if not self.enabled:
                continue
            text, on_start, generation = item
            try:
                self._stream_and_play(text, on_start, generation)
            except Exception as exc:
                self._call_start(on_start)
                self.on_error(str(exc))

    @staticmethod
    def _call_start(on_start):
        if on_start:
            try:
                on_start()
            except Exception:
                pass

    def _speech_request(self, text: str):
        payload = {
            "model": self.model,
            "voice": self.voice,
            "input": text,
            "instructions": (
                f"Speak clearly and slowly in {self.learning_language}. Use accurate pronunciation, "
                "a warm friendly tone, and gentle pauses for a language learner. Do not rush."
            ),
            "response_format": "pcm",
        }
        request = urllib.request.Request(
            "https://api.openai.com/v1/audio/speech",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            return urllib.request.urlopen(request, timeout=90)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                message = json.loads(detail).get("error", {}).get("message", detail)
            except json.JSONDecodeError:
                message = detail or str(exc)
            raise RuntimeError(message) from exc

    def _stream_and_play(self, text: str, on_start=None, generation: int = 0):
        """Play PCM as it arrives so text and speech can start together."""
        response = self._speech_request(text)
        started = False
        try:
            import sounddevice as sound

            remainder = b""
            with sound.RawOutputStream(samplerate=24000, channels=1, dtype="int16") as stream:
                while (
                    not self._stop.is_set()
                    and self.enabled
                    and generation == self._generation
                ):
                    chunk = response.read(8192)
                    if not chunk:
                        break
                    chunk = remainder + chunk
                    if len(chunk) % 2:
                        remainder = chunk[-1:]
                        chunk = chunk[:-1]
                    else:
                        remainder = b""
                    if not chunk:
                        continue
                    if not started:
                        self._call_start(on_start)
                        started = True
                    stream.write(chunk)
            if not started:
                self._call_start(on_start)
        except Exception:
            pcm = response.read()
            if not started:
                self._call_start(on_start)
            if generation != self._generation or not self.enabled:
                return
            import winsound

            winsound.PlaySound(_pcm_to_wav(pcm), winsound.SND_MEMORY)
        finally:
            response.close()


class _Point(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _Size(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class _BlendFunction(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class _BitmapInfoHeader(ctypes.Structure):
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


class _RgbQuad(ctypes.Structure):
    _fields_ = [
        ("rgbBlue", ctypes.c_ubyte),
        ("rgbGreen", ctypes.c_ubyte),
        ("rgbRed", ctypes.c_ubyte),
        ("rgbReserved", ctypes.c_ubyte),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", _BitmapInfoHeader), ("bmiColors", _RgbQuad * 1)]


class TextOverlay:
    """Per-pixel transparent, click-through Windows text overlay."""

    def __init__(self, root: tk.Misc):
        self.root = root
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.last_monitor: Monitor | None = None
        self.last_learning = ""
        self.last_known = ""
        self.enabled = True
        self._styled = False
        self._capture_excluded = False
        self._native_hwnd = None
        self._old_wndproc = None
        self._wndproc = None
        self.style = {
            key: DEFAULT_CONFIG[key]
            for key in (
                "font_family",
                "learning_font_size",
                "known_font_size",
                "learning_color",
                "known_color",
                "outline_color",
                "outline_size",
                "opacity",
                "line_gap",
                "bottom_offset",
            )
        }

    def _native_handle(self):
        if self._native_hwnd:
            return self._native_hwnd
        hwnd = self.window.winfo_id()
        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        self._native_hwnd = user32.GetAncestor(hwnd, 2) or hwnd
        return self._native_hwnd

    def _make_click_through(self):
        if self._styled:
            return
        self.window.update_idletasks()
        hwnd = self._native_handle()
        user32 = ctypes.windll.user32
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = wintypes.LONG
        user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
        user32.SetWindowLongW.restype = wintypes.LONG
        style = user32.GetWindowLongW(hwnd, -20)
        user32.SetWindowLongW(
            hwnd,
            -20,
            style | 0x00080000 | 0x00000020 | 0x00000080 | 0x00000008 | 0x08000000,
        )
        self._install_transparent_hit_test(hwnd)
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0020)
        self._styled = True

    def _install_transparent_hit_test(self, hwnd):
        if self._wndproc is not None:
            return
        user32 = ctypes.windll.user32
        result_type = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
        wndproc_type = ctypes.WINFUNCTYPE(
            result_type,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        set_wndproc = user32.SetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else user32.SetWindowLongW
        set_wndproc.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        set_wndproc.restype = ctypes.c_void_p if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
        user32.CallWindowProcW.argtypes = [
            ctypes.c_void_p,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.CallWindowProcW.restype = result_type

        def window_proc(proc_hwnd, message, wparam, lparam):
            if message == 0x0084:  # WM_NCHITTEST
                return -1  # HTTRANSPARENT: pass input to the window underneath.
            if message == 0x0021:  # WM_MOUSEACTIVATE
                return 3  # MA_NOACTIVATE
            if self._old_wndproc:
                return user32.CallWindowProcW(self._old_wndproc, proc_hwnd, message, wparam, lparam)
            return user32.DefWindowProcW(proc_hwnd, message, wparam, lparam)

        self._wndproc = wndproc_type(window_proc)
        self._old_wndproc = set_wndproc(hwnd, -4, self._wndproc)

    def _allow_normal_capture(self):
        """Keep narration visible to normal screenshots and screen sharing."""
        try:
            top_hwnd = self._native_handle()
            set_affinity = ctypes.windll.user32.SetWindowDisplayAffinity
            set_affinity.argtypes = [wintypes.HWND, wintypes.DWORD]
            set_affinity.restype = wintypes.BOOL
            set_affinity(top_hwnd, 0x00000000)
            self._capture_excluded = False
        except Exception:
            self._capture_excluded = False

    def set_enabled(self, enabled: bool):
        self.enabled = bool(enabled)
        if not self.enabled:
            self.hide()
        elif self.last_monitor and self.last_learning:
            self.show(self.last_monitor, self.last_learning, self.last_known)

    def configure_style(self, settings: dict):
        for key in self.style:
            if key in settings:
                self.style[key] = settings[key]

    def show(self, monitor: Monitor, learning: str, known: str):
        self.last_monitor = monitor
        self.last_learning = learning
        self.last_known = known
        if not self.enabled:
            return
        learning_size = self._int_style("learning_font_size", 30, 12, 72)
        known_size = self._int_style("known_font_size", 21, 10, 60)
        line_gap = self._int_style("line_gap", 28, 0, 120)
        bottom_offset = self._int_style("bottom_offset", 46, 0, max(0, monitor.height - 100))
        width = max(560, min(1400, monitor.width - 80))
        height = max(110, min(420, int(learning_size * 2.5 + known_size * 2.5 + line_gap + 24)))
        x = monitor.left + max(20, (monitor.width - width) // 2)
        y = monitor.top + monitor.height - height - bottom_offset
        self.window.geometry(f"{width}x{height}{x:+d}{y:+d}")
        self.window.update_idletasks()
        self._make_click_through()
        image = self._render_image(width, height, learning, known)
        self._update_layered_window(self._native_handle(), image, x, y)
        self.window.deiconify()
        ctypes.windll.user32.ShowWindow(self._native_handle(), 4)
        self._allow_normal_capture()

    def _render_image(self, width: int, height: int, learning: str, known: str) -> Image.Image:
        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        learning_size = self._int_style("learning_font_size", 30, 12, 72)
        known_size = self._int_style("known_font_size", 21, 10, 60)
        outline_size = self._int_style("outline_size", 4, 0, 12)
        opacity = self._int_style("opacity", 100, 10, 100) / 100.0
        line_gap = self._int_style("line_gap", 28, 0, 120)
        learning_font = self._font(learning_size, bold=True, text=learning)
        known_font = self._font(known_size, bold=False, text=known)
        max_width = width - 70
        learning_lines = self._wrap_text(draw, learning, learning_font, max_width, max_lines=2)
        known_lines = self._wrap_text(draw, known, known_font, max_width, max_lines=2)
        learning_box = draw.multiline_textbbox(
            (0, 0), learning_lines, font=learning_font, align="center", spacing=4, stroke_width=outline_size
        )
        known_box = draw.multiline_textbbox(
            (0, 0), known_lines, font=known_font, align="center", spacing=3, stroke_width=outline_size
        )
        learning_height = learning_box[3] - learning_box[1]
        known_height = known_box[3] - known_box[1]
        content_height = learning_height + line_gap + known_height
        learning_y = max(8, (height - content_height) // 2)
        known_y = learning_y + learning_height + line_gap
        draw.multiline_text(
            (width // 2, learning_y),
            learning_lines,
            font=learning_font,
            fill=self._rgba(self.style.get("learning_color"), opacity, "#FFFFFF"),
            anchor="ma",
            align="center",
            spacing=4,
            stroke_width=outline_size,
            stroke_fill=self._rgba(self.style.get("outline_color"), opacity, "#000000"),
        )
        draw.multiline_text(
            (width // 2, known_y),
            known_lines,
            font=known_font,
            fill=self._rgba(self.style.get("known_color"), opacity, "#61DAFB"),
            anchor="ma",
            align="center",
            spacing=3,
            stroke_width=max(0, outline_size - 1),
            stroke_fill=self._rgba(self.style.get("outline_color"), opacity, "#000000"),
        )
        return image

    def _font(self, size: int, bold: bool, text: str = ""):
        family = str(self.style.get("font_family") or "Segoe UI")
        font_files = {
            "Segoe UI": ("segoeui.ttf", "segoeuib.ttf"),
            "Arial": ("arial.ttf", "arialbd.ttf"),
            "Calibri": ("calibri.ttf", "calibrib.ttf"),
            "Verdana": ("verdana.ttf", "verdanab.ttf"),
            "Tahoma": ("tahoma.ttf", "tahomabd.ttf"),
            "Times New Roman": ("times.ttf", "timesbd.ttf"),
            "Consolas": ("consola.ttf", "consolab.ttf"),
        }
        if any("\u3040" <= char <= "\u30ff" for char in text):
            name = "meiryo.ttc"
        elif any("\uac00" <= char <= "\ud7af" for char in text):
            name = "malgun.ttf"
        elif any("\u3400" <= char <= "\u9fff" for char in text):
            name = "msyh.ttc"
        else:
            regular, bold_name = font_files.get(family, font_files["Segoe UI"])
            name = bold_name if bold else regular
        path = Path.home().drive + rf"\Windows\Fonts\{name}"
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            return ImageFont.load_default()

    def _int_style(self, key: str, default: int, low: int, high: int) -> int:
        try:
            value = int(self.style.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    @staticmethod
    def _rgba(value, opacity: float, fallback: str):
        color = str(value or fallback).strip().lstrip("#")
        if len(color) != 6:
            color = fallback.lstrip("#")
        try:
            red, green, blue = (int(color[index:index + 2], 16) for index in (0, 2, 4))
        except ValueError:
            red, green, blue = (255, 255, 255)
        return red, green, blue, max(0, min(255, int(255 * opacity)))

    @staticmethod
    def _wrap_text(draw, text: str, font, max_width: int, max_lines: int) -> str:
        source = str(text or "").strip()
        words = source.split()
        if not words:
            return ""
        if len(words) == 1 and draw.textbbox((0, 0), words[0], font=font, stroke_width=4)[2] > max_width:
            lines: list[str] = []
            current = ""
            for char in source:
                candidate = current + char
                if current and draw.textbbox((0, 0), candidate, font=font, stroke_width=4)[2] > max_width:
                    lines.append(current)
                    current = char
                    if len(lines) >= max_lines:
                        break
                else:
                    current = candidate
            if len(lines) < max_lines and current:
                lines.append(current)
            if "".join(lines) != source and lines:
                lines[-1] = lines[-1].rstrip(" .") + "..."
            return "\n".join(lines[:max_lines])
        lines: list[str] = []
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if draw.textbbox((0, 0), candidate, font=font, stroke_width=4)[2] <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
                if len(lines) >= max_lines:
                    break
        if len(lines) < max_lines:
            lines.append(current)
        if len(lines) == max_lines and " ".join(lines).strip() != " ".join(words).strip():
            lines[-1] = lines[-1].rstrip(" .") + "..."
        return "\n".join(lines[:max_lines])

    @staticmethod
    def _update_layered_window(hwnd, image: Image.Image, x: int, y: int):
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.GetDC.restype = wintypes.HDC
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.ReleaseDC.restype = ctypes.c_int
        user32.UpdateLayeredWindow.argtypes = [
            wintypes.HWND,
            wintypes.HDC,
            ctypes.POINTER(_Point),
            ctypes.POINTER(_Size),
            wintypes.HDC,
            ctypes.POINTER(_Point),
            wintypes.COLORREF,
            ctypes.POINTER(_BlendFunction),
            wintypes.DWORD,
        ]
        user32.UpdateLayeredWindow.restype = wintypes.BOOL
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
        gdi32.DeleteDC.argtypes = [wintypes.HDC]
        gdi32.DeleteDC.restype = wintypes.BOOL
        gdi32.CreateDIBSection.argtypes = [
            wintypes.HDC,
            ctypes.POINTER(_BitmapInfo),
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.HANDLE,
            wintypes.DWORD,
        ]
        gdi32.CreateDIBSection.restype = ctypes.c_void_p
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.SelectObject.restype = ctypes.c_void_p
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteObject.restype = wintypes.BOOL
        rgba = image.tobytes("raw", "RGBA")
        bgra = bytearray(len(rgba))
        for index in range(0, len(rgba), 4):
            red, green, blue, alpha = rgba[index:index + 4]
            bgra[index] = (blue * alpha) // 255
            bgra[index + 1] = (green * alpha) // 255
            bgra[index + 2] = (red * alpha) // 255
            bgra[index + 3] = alpha

        screen_dc = user32.GetDC(0)
        memory_dc = gdi32.CreateCompatibleDC(screen_dc)
        bits = ctypes.c_void_p()
        info = _BitmapInfo()
        info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
        info.bmiHeader.biWidth = image.width
        info.bmiHeader.biHeight = -image.height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        bitmap = gdi32.CreateDIBSection(screen_dc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
        if not bitmap or not bits:
            gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(0, screen_dc)
            raise OSError("Could not create narrator overlay bitmap")
        old_bitmap = gdi32.SelectObject(memory_dc, bitmap)
        try:
            ctypes.memmove(bits, bytes(bgra), len(bgra))
            destination = _Point(x, y)
            size = _Size(image.width, image.height)
            source = _Point(0, 0)
            blend = _BlendFunction(0, 0, 255, 1)
            updated = user32.UpdateLayeredWindow(
                hwnd,
                screen_dc,
                ctypes.byref(destination),
                ctypes.byref(size),
                memory_dc,
                ctypes.byref(source),
                0,
                ctypes.byref(blend),
                2,
            )
            if not updated:
                raise OSError("Could not update narrator overlay")
        finally:
            gdi32.SelectObject(memory_dc, old_bitmap)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(0, screen_dc)

    def hide(self):
        try:
            ctypes.windll.user32.ShowWindow(self._native_handle(), 0)
        except Exception:
            pass

    def restore(self):
        if self.enabled and self.last_monitor and self.last_learning:
            self.show(self.last_monitor, self.last_learning, self.last_known)

    def destroy(self):
        try:
            self.window.destroy()
        except tk.TclError:
            pass
