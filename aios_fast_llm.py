"""An Ollama-compatible front end for llama.cpp, so aiOS CODE gets speculative decoding.

Ollama is the convenient way to store and serve GGUF models, but it does not
expose speculative decoding, and Qwen3.8's GGUF carries an MTP head that Ollama
loads and then ignores. Handing the same blob to the llama-server binary Ollama
already ships, with ``--spec-type draft-mtp``, roughly doubles generation.

Measured on a 16 GB RTX 5070 Ti with Qwen3.8-27B UD-Q2_K_XL at 32k context, both
figures taken from the server's own eval counters so the comparison is like for
like rather than wall clock against wall clock:

    ollama                                57.6 tok/s
    llama-server, --spec-type draft-mtp  108.8 tok/s   (99% of drafts accepted)

aiOS CODE speaks Ollama's ``/api/chat`` and llama-server speaks OpenAI ``/v1``,
so this translates between the two instead of changing the harness. Run it and
point aiOS at it:

    python aios_fast_llm.py
    setx AIOS_OLLAMA_HOST http://127.0.0.1:11435

Only the three endpoints aiOS actually calls are implemented: ``/api/version``,
``/api/tags`` and ``/api/chat``. This is a shim for one caller, not an Ollama
replacement.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

import aios_local_llm as backend

UPSTREAM_PORT = int(os.environ.get("AIOS_FAST_LLM_UPSTREAM", "11500"))
LISTEN_PORT = int(os.environ.get("AIOS_FAST_LLM_PORT", "11435"))

STATE: dict[str, str] = {"model": "", "upstream": "http://127.0.0.1:" + str(UPSTREAM_PORT)}


def post_stream(url: str, payload: dict, timeout: float = 1800):
    """Yield parsed objects from an OpenAI-style server-sent event stream."""
    request = Request(url, data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                return
            try:
                yield json.loads(body)
            except json.JSONDecodeError:
                continue


def finish_reason(value: str) -> str:
    """Map an OpenAI finish reason onto the two words the harness trusts.

    Anything it does not recognise is treated as an unsafe stop, so an unknown
    value must not be passed through verbatim.
    """
    return {"stop": "stop", "length": "length", "tool_calls": "stop"}.get(str(value or ""), "stop")


def to_openai_messages(messages) -> list:
    """Rewrite Ollama-shaped history into what llama-server will accept.

    Ollama emits a tool call as ``{"id", "function": {"name", "arguments": {...}}}``.
    llama-server requires ``"type": "function"`` and ``arguments`` as a JSON
    *string*, and rejects the entire request otherwise -- so the first round of
    a turn succeeds and every later round fails, because only later rounds carry
    a previous tool call in their history.
    """
    out = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        row = dict(message)
        calls = row.get("tool_calls")
        if isinstance(calls, list) and calls:
            rewritten = []
            for call in calls:
                if not isinstance(call, dict):
                    continue
                clean = dict(call)
                clean["type"] = clean.get("type") or "function"
                function = dict(clean.get("function") or {})
                function.pop("index", None)
                arguments = function.get("arguments")
                if not isinstance(arguments, str):
                    function["arguments"] = json.dumps(arguments or {}, ensure_ascii=False)
                clean["function"] = function
                rewritten.append(clean)
            row["tool_calls"] = rewritten
            # An assistant turn that only calls tools still needs a content key.
            row.setdefault("content", "")
        out.append(row)
    return out


def translate_chat(payload: dict):
    """Yield Ollama ``/api/chat`` chunks for one upstream OpenAI completion."""
    model = str(payload.get("model") or STATE["model"] or "local")
    options = payload.get("options") or {}
    upstream: dict[str, object] = {
        "model": "local",
        "messages": to_openai_messages(payload.get("messages")),
        "stream": True,
        "temperature": options.get("temperature", 0.35),
        "stream_options": {"include_usage": True},
        "timings_per_token": True,
    }
    if payload.get("tools"):
        upstream["tools"] = payload["tools"]
    if options.get("num_predict"):
        upstream["max_tokens"] = int(options["num_predict"])
    if not payload.get("think"):
        # The harness decides per turn whether the model may think. Pass that
        # through rather than leaving it to the template's default.
        upstream["chat_template_kwargs"] = {"enable_thinking": False}

    calls: dict[int, dict] = {}
    finish = ""
    timings: dict = {}
    for chunk in post_stream(STATE["upstream"] + "/v1/chat/completions", upstream):
        if chunk.get("timings"):
            timings = chunk["timings"]
        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0] or {}
        finish = choice.get("finish_reason") or finish
        delta = choice.get("delta") or {}
        for fragment in delta.get("tool_calls") or []:
            index = int(fragment.get("index") or 0)
            slot = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if fragment.get("id"):
                slot["id"] = fragment["id"]
            function = fragment.get("function") or {}
            if function.get("name"):
                slot["name"] = function["name"]
            if function.get("arguments"):
                slot["arguments"] += function["arguments"]
        text = delta.get("content") or ""
        thinking = delta.get("reasoning_content") or ""
        if text or thinking:
            message: dict[str, object] = {"role": "assistant", "content": text}
            if thinking:
                message["thinking"] = thinking
            yield {"model": model, "message": message, "done": False}

    final: dict[str, object] = {"role": "assistant", "content": ""}
    tool_calls = []
    for index in sorted(calls):
        slot = calls[index]
        if not slot["name"]:
            continue
        try:
            arguments = json.loads(slot["arguments"] or "{}")
        except json.JSONDecodeError:
            # Never hand the harness half an arguments object. Reporting the
            # truncation is what lets it retry instead of running a call the
            # model never finished asking for.
            finish = "length"
            continue
        tool_calls.append({
            "id": slot["id"] or "call-" + str(index),
            "function": {"index": index, "name": slot["name"], "arguments": arguments},
        })
    if tool_calls:
        final["tool_calls"] = tool_calls

    predicted_ms = float(timings.get("predicted_ms") or 0.0)
    prompt_ms = float(timings.get("prompt_ms") or 0.0)
    yield {
        "model": model,
        "message": final,
        "done": True,
        "done_reason": finish_reason(finish),
        "eval_count": int(timings.get("predicted_n") or 0),
        "eval_duration": int(predicted_ms * 1_000_000),
        "prompt_eval_count": int(timings.get("prompt_n") or 0),
        "prompt_eval_duration": int(prompt_ms * 1_000_000),
        "total_duration": int((predicted_ms + prompt_ms) * 1_000_000),
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:
        return

    def send_json(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        name = STATE["model"]
        if self.path.startswith("/api/version"):
            self.send_json(200, json.dumps({"version": "aios-fast-llm"}).encode())
        elif self.path.startswith("/api/tags"):
            self.send_json(200, json.dumps({"models": [{
                "name": name,
                "model": name,
                "size": 0,
                "digest": "",
                "details": {"family": "qwen35", "parameter_size": "27B"},
            }]}).encode())
        elif self.path.startswith("/api/ps"):
            self.send_json(200, json.dumps({"models": [{"name": name, "model": name}]}).encode())
        else:
            self.send_json(404, b'{"error":"not found"}')

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self.send_json(400, b'{"error":"invalid json"}')
            return
        if not self.path.startswith("/api/chat"):
            self.send_json(404, b'{"error":"not found"}')
            return

        if not payload.get("stream", True):
            try:
                chunks = list(translate_chat(payload))
            except Exception as exc:
                self.send_json(502, json.dumps({"error": str(exc)[:300]}).encode())
                return
            merged = chunks[-1] if chunks else {}
            text = "".join(
                str((chunk.get("message") or {}).get("content") or "")
                for chunk in chunks[:-1]
            )
            if text:
                merged.setdefault("message", {})["content"] = text
            self.send_json(200, json.dumps(merged).encode())
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for chunk in translate_chat(payload):
                line = (json.dumps(chunk) + "\n").encode("utf-8")
                self.wfile.write(format(len(line), "X").encode() + b"\r\n" + line + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        except Exception:
            # The response is already committed, so there is no status left to
            # send. Dropping the socket is the only honest signal available.
            pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=backend.DEFAULT_MODEL)
    parser.add_argument("--ctx", type=int, default=32768)
    parser.add_argument("--spec", default="draft-mtp")
    parser.add_argument("--port", type=int, default=LISTEN_PORT)
    parser.add_argument("--upstream-port", type=int, default=UPSTREAM_PORT)
    args = parser.parse_args()

    STATE["model"] = args.model
    STATE["upstream"] = "http://127.0.0.1:" + str(args.upstream_port)

    blob = backend.resolve_blob(args.model)
    environment = dict(os.environ)
    dll = backend.cuda_backend()
    if dll is not None:
        environment["GGML_BACKEND_PATH"] = str(dll)
        environment["PATH"] = str(dll.parent) + ";" + str(backend.OLLAMA_LIB) + ";" + environment.get("PATH", "")

    command = backend.build_command(blob, argparse.Namespace(
        port=args.upstream_port, host="127.0.0.1", ctx=args.ctx,
        spec=args.spec, gpu_layers=99,
    ))
    print("model    " + args.model)
    print("context  " + str(args.ctx) + "   speculation  " + args.spec)
    print("loading  llama-server on :" + str(args.upstream_port) + " ...", flush=True)

    process = subprocess.Popen(command, env=environment)
    try:
        if not backend.wait_until_ready(STATE["upstream"], process, 420.0):
            print("llama-server failed to become ready", file=sys.stderr)
            process.terminate()
            return 1
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print("ready    http://127.0.0.1:" + str(args.port) + "  (Ollama API)")
        print("         point aiOS at it:  AIOS_OLLAMA_HOST=http://127.0.0.1:" + str(args.port))
        print("         Ctrl-C to stop", flush=True)
        while process.poll() is None:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        process.terminate()
        try:
            process.wait(timeout=30)
        except Exception:
            process.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
