from __future__ import annotations
import base64
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import threading
import queue
import uuid

from flask import Flask, render_template, request, Response, jsonify
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent import config
from agent.orchestrator import run_task
from voice_settings import load_voice_dictation_settings, resolve_transcribe_language

# Make Windows screenshot capture see physical pixels on multi-monitor setups.
if os.name == "nt":
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    except Exception:
        pass


app = Flask(__name__, template_folder="templates", static_folder="static")

# In-memory job store: job_id -> queue of events (dicts). Last sentinel {"type":"end"}.
JOBS: dict[str, queue.Queue] = {}
IMAGES: dict[str, Image.Image] = {}  # job_id -> original image
HELPER_HOST = "127.0.0.1"
HELPER_PORT = 48736
SCREEN_LOCK = threading.Lock()
SCREEN_CACHE: dict[tuple, dict] = {}

OPERATOR_EVENTS_DIR = REPO_ROOT / "phone_operator_events"
OPERATOR_EVENTS_FILE = OPERATOR_EVENTS_DIR / "events.jsonl"
OPERATOR_FRAMES_DIR = OPERATOR_EVENTS_DIR / "frames"
OPERATOR_EVENTS_DIR.mkdir(exist_ok=True)
OPERATOR_FRAMES_DIR.mkdir(exist_ok=True)


class PhoneTranscriber:
    def __init__(self):
        self.lock = threading.Lock()
        self.model = None
        self.loaded_key = None

    def _settings(self):
        settings = load_voice_dictation_settings()
        env_model = os.environ.get("VOICE_WHISPER_MODEL", "").strip()
        env_compute = os.environ.get("VOICE_WHISPER_COMPUTE", "").strip()
        if env_model:
            settings["whisper_model"] = env_model
        if env_compute:
            settings["compute_type"] = env_compute
        return settings

    def _kwargs(self, settings):
        language_setting = str(settings.get("language", "auto") or "auto").strip().lower()
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

    def _ensure_model(self, settings):
        from faster_whisper import WhisperModel

        key = (settings["whisper_model"], settings["compute_type"])
        with self.lock:
            if self.model is None or self.loaded_key != key:
                self.model = WhisperModel(settings["whisper_model"], device="auto", compute_type=settings["compute_type"])
                self.loaded_key = key
            return self.model

    def transcribe(self, path):
        settings = self._settings()
        model = self._ensure_model(settings)
        with self.lock:
            segments, _info = model.transcribe(str(path), **self._kwargs(settings))
            return "".join(segment.text for segment in segments).strip()


PHONE_TRANSCRIBER = PhoneTranscriber()


def _preload_whisper():
    try:
        PHONE_TRANSCRIBER._ensure_model(PHONE_TRANSCRIBER._settings())
    except Exception:
        pass


threading.Thread(target=_preload_whisper, daemon=True).start()


@app.after_request
def add_phone_cors(response):
    if request.path.startswith("/api/phone/"):
        origin = request.headers.get("Origin") or "*"
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


HELPER_CONFIG_PATH = REPO_ROOT / "helper_config.json"
DEFAULT_OPERATOR_CONFIG = {
    "monitor": "",
    "model": "gpt-5.5",
    "reasoning": "medium",
    "steps": "25",
    "delay": "0.20",
    "tts": False,
    "voice": "nova",
    "shell": False,
    "codex_auth": False,
}


def forward_helper(action, text="", options=None):
    action = str(action or "").strip().lower()
    text = str(text or "").strip()
    if action not in {"chat", "operator", "phone_start", "phone_stop", "reload_operator_settings", "operator_stop"}:
        return {"ok": True, "sent": False}
    if action in {"chat", "operator"} and not text:
        return {"ok": True, "sent": False}
    body = {"action": action, "text": text}
    if isinstance(options, dict) and options:
        body["options"] = options
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        with socket.create_connection((HELPER_HOST, HELPER_PORT), timeout=0.35) as client:
            client.sendall(payload)
    except OSError as exc:
        return {"ok": False, "sent": False, "error": f"aiOS helper is not listening: {exc}"}
    return {"ok": True, "sent": True}


def _load_helper_config():
    if not HELPER_CONFIG_PATH.exists():
        return {}
    try:
        with HELPER_CONFIG_PATH.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_helper_config(config):
    tmp = HELPER_CONFIG_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2, ensure_ascii=False)
    tmp.replace(HELPER_CONFIG_PATH)


def _enumerate_monitors():
    try:
        import mss
    except Exception as exc:
        return [], str(exc)
    monitors = []
    try:
        with mss.mss() as sct:
            for i, m in enumerate(sct.monitors):
                label = ("All monitors" if i == 0 else f"Monitor {i}") + f"  {m['width']}x{m['height']} @ ({m['left']},{m['top']})"
                monitors.append({
                    "index": i,
                    "left": int(m["left"]),
                    "top": int(m["top"]),
                    "width": int(m["width"]),
                    "height": int(m["height"]),
                    "label": label,
                    "name": "All monitors" if i == 0 else f"Monitor {i}",
                })
    except Exception as exc:
        return [], str(exc)
    return monitors, ""


@app.route("/")
def index():
    return render_template("index.html",
                           default_model=config.MODEL,
                           max_rounds=config.MAX_ROUNDS)


@app.route("/phone")
def phone():
    return render_template("phone.html")


@app.route("/api/phone/status")
def api_phone_status():
    try:
        with socket.create_connection((HELPER_HOST, HELPER_PORT), timeout=0.2):
            helper = True
    except OSError:
        helper = False
    monitors, _ = _enumerate_monitors()
    cfg = _load_helper_config()
    operator = dict(DEFAULT_OPERATOR_CONFIG)
    operator.update(cfg.get("ai_operator") or {})
    return jsonify({
        "ok": True,
        "helper": helper,
        "monitor_count": len(monitors),
        "operator": operator,
    })


@app.route("/api/phone/start", methods=["POST", "OPTIONS"])
def api_phone_start():
    if request.method == "OPTIONS":
        return "", 204
    data = request.get_json(silent=True) or {}
    target = (data.get("target") or "").strip()
    result = forward_helper("phone_start", target)
    return jsonify(result), 200 if result.get("ok") else 503


@app.route("/api/phone/stop", methods=["POST", "OPTIONS"])
def api_phone_stop():
    if request.method == "OPTIONS":
        return "", 204
    result = forward_helper("phone_stop", "")
    return jsonify(result), 200 if result.get("ok") else 503


@app.route("/api/phone/send", methods=["POST", "OPTIONS"])
def api_phone_send():
    if request.method == "OPTIONS":
        return "", 204
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    target = (data.get("target") or "chat").strip().lower()
    options = data.get("options") if isinstance(data.get("options"), dict) else None
    if not text:
        return jsonify({"error": "text required"}), 400
    if target == "operator" and options:
        cfg = _load_helper_config()
        operator = dict(DEFAULT_OPERATOR_CONFIG)
        operator.update(cfg.get("ai_operator") or {})
        for key, value in options.items():
            if key not in DEFAULT_OPERATOR_CONFIG:
                continue
            default = DEFAULT_OPERATOR_CONFIG[key]
            if isinstance(default, bool):
                operator[key] = bool(value)
            else:
                operator[key] = "" if value is None else str(value)
        cfg["ai_operator"] = operator
        try:
            _save_helper_config(cfg)
        except OSError:
            pass
        result = forward_helper(target, text, operator)
    else:
        result = forward_helper(target, text)
    return jsonify(result), 200 if result.get("ok") else 503


@app.route("/api/phone/monitors")
def api_phone_monitors():
    monitors, err = _enumerate_monitors()
    return jsonify({"ok": not err, "monitors": monitors, "error": err})


@app.route("/api/phone/operator/events")
def api_phone_operator_events():
    last_pos = 0
    try:
        if request.args.get("since"):
            last_pos = max(0, int(request.args.get("since")))
    except (TypeError, ValueError):
        last_pos = 0

    def stream():
        nonlocal last_pos
        yield "retry: 2000\n\n"
        idle_ticks = 0
        while True:
            try:
                if OPERATOR_EVENTS_FILE.exists():
                    size = OPERATOR_EVENTS_FILE.stat().st_size
                    if size < last_pos:
                        last_pos = 0  # file truncated, new run
                        yield "event: reset\ndata: {}\n\n"
                    if size > last_pos:
                        with OPERATOR_EVENTS_FILE.open("rb") as fh:
                            fh.seek(last_pos)
                            chunk = fh.read(size - last_pos)
                            last_pos = size
                        for raw_line in chunk.splitlines():
                            line = raw_line.decode("utf-8", "ignore").strip()
                            if not line:
                                continue
                            try:
                                event = json.loads(line)
                                event["size"] = size
                                line = json.dumps(event, ensure_ascii=False)
                            except json.JSONDecodeError:
                                pass
                            yield f"data: {line}\n\n"
                        idle_ticks = 0
                    else:
                        idle_ticks += 1
                else:
                    idle_ticks += 1
                if idle_ticks % 10 == 0:
                    yield ": ping\n\n"
                time.sleep(0.35)
            except GeneratorExit:
                return
            except Exception:
                time.sleep(0.5)

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return Response(stream(), mimetype="text/event-stream", headers=headers)


@app.route("/api/phone/operator/log")
def api_phone_operator_log():
    events = []
    size = 0
    since = 0
    reset = False
    try:
        since = max(0, int(request.args.get("since") or 0))
    except (TypeError, ValueError):
        since = 0
    if OPERATOR_EVENTS_FILE.exists():
        try:
            size = OPERATOR_EVENTS_FILE.stat().st_size
            if since > size:
                since = 0
                reset = True
            with OPERATOR_EVENTS_FILE.open("rb") as fh:
                if since:
                    fh.seek(since)
                raw = fh.read()
            for line in raw.decode("utf-8", "ignore").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    event["size"] = size
                    events.append(event)
                except json.JSONDecodeError:
                    pass
        except OSError:
            pass
    return jsonify({"ok": True, "events": events, "size": size, "reset": reset})


@app.route("/api/phone/operator/frame/<int:frame_id>")
def api_phone_operator_frame(frame_id):
    path = OPERATOR_FRAMES_DIR / f"frame-{frame_id}.jpg"
    if not path.exists():
        return jsonify({"error": "not found"}), 404
    return Response(path.read_bytes(), mimetype="image/jpeg", headers={"Cache-Control": "public, max-age=3600"})


@app.route("/api/phone/screen")
def api_phone_screen():
    try:
        idx = int(request.args.get("monitor") or 1)
    except (TypeError, ValueError):
        idx = 1
    try:
        quality = max(20, min(95, int(request.args.get("q") or 78)))
    except (TypeError, ValueError):
        quality = 78
    try:
        max_dim = max(300, min(3840, int(request.args.get("max") or 1600)))
    except (TypeError, ValueError):
        max_dim = 1600
    now = time.monotonic()
    cache_key = (idx, quality, max_dim)
    with SCREEN_LOCK:
        cached = SCREEN_CACHE.get(cache_key)
        if cached and now - cached["at"] < 0.45:
            return Response(cached["data"], mimetype="image/jpeg")
        try:
            import mss

            with mss.mss() as sct:
                if idx < 0 or idx >= len(sct.monitors):
                    idx = 1 if len(sct.monitors) > 1 else 0
                monitor = sct.monitors[idx]
                shot = sct.grab(monitor)
                image = Image.frombytes("RGB", shot.size, shot.rgb)
        except Exception:
            try:
                from PIL import ImageGrab

                image = ImageGrab.grab().convert("RGB")
            except Exception as exc:
                return jsonify({"error": str(exc)}), 500
        image.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=quality, optimize=True)
        SCREEN_CACHE[cache_key] = {"data": out.getvalue(), "at": now}
        return Response(SCREEN_CACHE[cache_key]["data"], mimetype="image/jpeg")


@app.route("/api/phone/operator/config", methods=["GET", "POST", "OPTIONS"])
def api_phone_operator_config():
    if request.method == "OPTIONS":
        return "", 204
    cfg = _load_helper_config()
    operator = dict(DEFAULT_OPERATOR_CONFIG)
    operator.update(cfg.get("ai_operator") or {})
    if request.method == "GET":
        monitors, mon_err = _enumerate_monitors()
        return jsonify({"ok": True, "operator": operator, "monitors": monitors, "monitor_error": mon_err})
    data = request.get_json(silent=True) or {}
    incoming = data.get("operator") or {}
    if not isinstance(incoming, dict):
        return jsonify({"ok": False, "error": "operator must be object"}), 400
    for key, value in incoming.items():
        if key not in DEFAULT_OPERATOR_CONFIG:
            continue
        default = DEFAULT_OPERATOR_CONFIG[key]
        if isinstance(default, bool):
            operator[key] = bool(value)
        else:
            operator[key] = "" if value is None else str(value)
    cfg["ai_operator"] = operator
    try:
        _save_helper_config(cfg)
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    forward_helper("reload_operator_settings", "", operator)
    return jsonify({"ok": True, "operator": operator})


@app.route("/api/phone/operator/stop", methods=["POST", "OPTIONS"])
def api_phone_operator_stop():
    if request.method == "OPTIONS":
        return "", 204
    result = forward_helper("operator_stop", "")
    return jsonify(result), 200 if result.get("ok") else 503


@app.route("/api/phone/transcribe", methods=["POST"])
def api_phone_transcribe():
    audio = request.files.get("audio")
    target = (request.form.get("target") or "none").strip().lower()
    if not audio:
        return jsonify({"error": "audio required"}), 400
    suffix = Path(audio.filename or "").suffix or ".webm"
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp:
            temp_path = Path(temp.name)
            audio.save(temp)
        text = PHONE_TRANSCRIBER.transcribe(temp_path)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        if temp_path:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
    if target in {"chat", "operator"}:
        forward_helper("phone_start", "AIOS" if target == "chat" else "OPERATOR")
    result = forward_helper(target, text) if text else {"ok": True, "sent": False}
    return jsonify({"ok": result.get("ok", False), "text": text, "sent": result.get("sent", False), "error": result.get("error", "")})


@app.route("/api/run", methods=["POST"])
def api_run():
    f = request.files.get("image")
    task = (request.form.get("task") or "").strip()
    model = (request.form.get("model") or config.MODEL).strip() or config.MODEL
    max_rounds = int(request.form.get("max_rounds") or config.MAX_ROUNDS)
    mode = (request.form.get("mode") or "full").strip()
    allow_crop = (request.form.get("allow_crop") or "0") in ("1", "true", "on", "yes")
    if not f or not task:
        return jsonify({"error": "image and task required"}), 400
    try:
        img = Image.open(f.stream).convert("RGB")
    except Exception as e:
        return jsonify({"error": f"bad image: {e}"}), 400

    job_id = uuid.uuid4().hex
    q: queue.Queue = queue.Queue()
    JOBS[job_id] = q
    IMAGES[job_id] = img

    def on_event(ev: dict):
        q.put(ev)

    def worker():
        try:
            run_task(img, task, on_event=on_event, model=model, max_rounds=max_rounds,
                     mode=mode, allow_crop=allow_crop)
        except Exception as e:
            q.put({"type": "fatal", "error": str(e)})
        finally:
            q.put({"type": "end"})

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"job_id": job_id, "image_size": list(img.size)})


@app.route("/api/stream/<job_id>")
def api_stream(job_id):
    q = JOBS.get(job_id)
    if q is None:
        return jsonify({"error": "unknown job"}), 404

    def gen():
        while True:
            ev = q.get()
            yield f"data: {json.dumps(ev)}\n\n"
            if ev.get("type") == "end":
                JOBS.pop(job_id, None)
                IMAGES.pop(job_id, None)
                break
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
