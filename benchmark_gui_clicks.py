import argparse
import base64
import json
import math
import random
import re
import statistics
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen

from PIL import Image


ROOT = Path(__file__).resolve().parent
DATASET_ROOT = ROOT / "training_data" / "gui_clicks"
DATASET_PATH = DATASET_ROOT / "dataset.jsonl"
BENCHMARK_ROOT = DATASET_ROOT / "benchmarks"
API_BASE = "http://localhost:11434"


def request_json(path, payload=None, timeout=900):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(API_BASE + path, data=data, headers=headers)
    with urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def image_to_b64_png(image):
    buffer = BytesIO()
    image.convert("RGB").save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def extract_json_object(text):
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    if start == -1:
        raise ValueError(f"no JSON object in model output: {text[:300]!r}")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(obj, dict):
        raise ValueError("top-level JSON is not an object")
    return obj


def flatten_numbers(value):
    out = []
    if isinstance(value, (int, float, str)):
        try:
            out.append(float(value))
        except ValueError:
            pass
    elif isinstance(value, list):
        for item in value:
            out.extend(flatten_numbers(item))
    return out


def coerce_point(value):
    if isinstance(value, dict):
        if "x" in value and "y" in value:
            return value["x"], value["y"]
        for key in ("point", "coordinate", "coordinates", "position", "click"):
            if key in value:
                return coerce_point(value[key])
    if isinstance(value, list):
        flat = flatten_numbers(value)
        if len(flat) >= 2:
            return flat[0], flat[1]
    raise ValueError(f"cannot read point from {value!r}")


def normalize_prediction(data, width, height):
    source = data
    actions = data.get("actions")
    if isinstance(actions, list):
        for action in actions:
            if isinstance(action, dict) and str(action.get("type", "")).lower() in {
                "click",
                "left_click",
                "right_click",
                "double_click",
                "move",
                "commit",
            }:
                source = action
                break

    if "x" in source and "y" in source:
        if isinstance(source["x"], list):
            x, y = coerce_point(source["x"])
        elif isinstance(source["y"], list):
            x, y = coerce_point(source["y"])
        else:
            x, y = source["x"], source["y"]
    elif "x" in source and isinstance(source["x"], list):
        x, y = coerce_point(source["x"])
    elif "args" in source and isinstance(source["args"], dict):
        return normalize_prediction(source["args"], width, height)
    elif "point" in source:
        x, y = coerce_point(source["point"])
    elif "coordinate" in source:
        x, y = coerce_point(source["coordinate"])
    elif "coordinates" in source:
        x, y = coerce_point(source["coordinates"])
    elif "position" in source:
        x, y = coerce_point(source["position"])
    elif "click" in source:
        x, y = coerce_point(source["click"])
    elif "bbox" in source and isinstance(source["bbox"], list) and len(source["bbox"]) >= 4:
        x = (float(source["bbox"][0]) + float(source["bbox"][2])) / 2
        y = (float(source["bbox"][1]) + float(source["bbox"][3])) / 2
    else:
        raise ValueError(f"no coordinate in prediction: {data}")

    return {
        "x": max(0, min(width - 1, int(round(float(x))))),
        "y": max(0, min(height - 1, int(round(float(y))))),
    }


def load_rows():
    rows = []
    with DATASET_PATH.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            row["_line_no"] = line_no
            rows.append(row)
    return rows


def build_prompt(task, width, height):
    return (
        "You are a desktop computer-use agent in evaluation mode.\n\n"
        "Reply with strict JSON only, no markdown:\n"
        "{\n"
        '  "thought": "what you see and why this point is right",\n'
        '  "actions": [\n'
        '    {"type":"click","x":<int>,"y":<int>,"button":"left","double":false,"clicks":1}\n'
        "  ]\n"
        "}\n\n"
        "Coordinates are original screenshot pixels. Top-left = (0,0). "
        f"All x,y must be inside [0..{width - 1}] x [0..{height - 1}]. "
        "Use the center of the clickable target, not a bounding-box corner.\n\n"
        f"TASK: {task}\n"
        f"Monitor screenshot is {width}x{height} px.\n"
    )


def benchmark(rows, model):
    results = []
    for i, row in enumerate(rows, 1):
        image_path = DATASET_ROOT / row["image"]
        target = row["action"]
        started = time.time()
        result = {
            "id": row.get("id"),
            "line_no": row.get("_line_no"),
            "image": row.get("image"),
            "prompt": row.get("prompt"),
            "target": {"x": target.get("x"), "y": target.get("y")},
            "model": model,
        }
        try:
            with Image.open(image_path) as image:
                image = image.convert("RGB")
                width, height = image.size
                payload = {
                    "model": model,
                    "prompt": build_prompt(row["prompt"], width, height),
                    "images": [image_to_b64_png(image)],
                    "stream": False,
                    "options": {"temperature": 0.0, "num_ctx": 4096},
                }
                response = request_json("/api/generate", payload, timeout=900)
            raw = response.get("response", "")
            parsed = extract_json_object(raw)
            pred = normalize_prediction(parsed, width, height)
            dx = pred["x"] - target["x"]
            dy = pred["y"] - target["y"]
            error_px = math.sqrt(dx * dx + dy * dy)
            result.update(
                {
                    "ok": True,
                    "prediction": pred,
                    "error_px": round(error_px, 2),
                    "latency_s": round(time.time() - started, 2),
                    "raw": raw,
                }
            )
        except Exception as exc:
            result.update(
                {
                    "ok": False,
                    "error": str(exc),
                    "latency_s": round(time.time() - started, 2),
                }
            )
        print(
            f"{i}/{len(rows)} {result['id']} "
            + (f"err={result['error_px']}px" if result.get("ok") else f"FAIL {result.get('error')}")
        )
        results.append(result)
    return results


def summarize(results):
    ok = [r for r in results if r.get("ok")]
    errors = [r["error_px"] for r in ok]
    summary = {
        "count": len(results),
        "ok": len(ok),
        "failed": len(results) - len(ok),
    }
    if errors:
        summary.update(
            {
                "mean_error_px": round(statistics.mean(errors), 2),
                "median_error_px": round(statistics.median(errors), 2),
                "min_error_px": round(min(errors), 2),
                "max_error_px": round(max(errors), 2),
                "hit_rate_10px": round(sum(e <= 10 for e in errors) / len(errors), 3),
                "hit_rate_25px": round(sum(e <= 25 for e in errors) / len(errors), 3),
                "hit_rate_50px": round(sum(e <= 50 for e in errors) / len(errors), 3),
                "hit_rate_100px": round(sum(e <= 100 for e in errors) / len(errors), 3),
                "mean_latency_s": round(statistics.mean(r["latency_s"] for r in ok), 2),
            }
        )
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="qwen2.5vl:7b")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    rows = load_rows()
    if not args.all:
        rng = random.Random(args.seed)
        rows = rows[:]
        rng.shuffle(rows)
        rows = rows[: args.limit]

    BENCHMARK_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result_path = BENCHMARK_ROOT / f"benchmark_{stamp}.jsonl"
    summary_path = BENCHMARK_ROOT / f"benchmark_{stamp}_summary.json"

    results = benchmark(rows, args.model)
    summary = summarize(results)
    summary.update(
        {
            "model": args.model,
            "dataset": str(DATASET_PATH),
            "result_path": str(result_path),
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    )

    with result_path.open("w", encoding="utf-8") as handle:
        for row in results:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\nSUMMARY")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
