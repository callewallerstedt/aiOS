from __future__ import annotations
import base64
import io
import json
import threading
import queue
import uuid

from flask import Flask, render_template, request, Response, jsonify
from PIL import Image

from agent import config
from agent.orchestrator import run_task


app = Flask(__name__, template_folder="templates", static_folder="static")

# In-memory job store: job_id -> queue of events (dicts). Last sentinel {"type":"end"}.
JOBS: dict[str, queue.Queue] = {}
IMAGES: dict[str, Image.Image] = {}  # job_id -> original image


@app.route("/")
def index():
    return render_template("index.html",
                           default_model=config.MODEL,
                           max_rounds=config.MAX_ROUNDS)


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
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
