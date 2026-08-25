"""Launch a tuned llama.cpp server for a model already pulled through Ollama.

Ollama is the convenient way to *fetch* and store GGUF models, but its server
does not expose speculative decoding. Qwen3.8's GGUF ships an MTP head
(`blk.64.nextn.*`) that Ollama loads and then ignores; handing the same blob to
the llama-server binary Ollama already bundles, with `--spec-type draft-mtp`,
roughly doubles generation speed on editing work at identical output quality --
speculation only accepts tokens the full model would have produced anyway.

Measured on a 16 GB RTX 5070 Ti with Qwen3.8-27B UD-Q2_K_XL at 32k context:

    ollama, fp16 KV, no flash attention      14 tok/s
    ollama, flash attention + q8_0 KV        56 tok/s
    llama-server, same + --spec-type draft-mtp   82 tok/s writing new code
                                                109 tok/s rewriting existing code

The server speaks the OpenAI chat-completions API on /v1 and parses native tool
calls with --jinja, so it is a drop-in for any OpenAI-compatible client.

    python aios_local_llm.py                     # the default model, port 11500
    python aios_local_llm.py --model qwen3:14b   # any installed Ollama tag
    python aios_local_llm.py --ctx 65536         # 64k still runs at full speed
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

OLLAMA_LIB = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "lib" / "ollama"
# The desktop app records its store in its own settings database and ignores
# OLLAMA_MODELS, so a stale variable points at a directory the app never fills.
# Try what the environment asks for, then where the app actually keeps models.
MODEL_STORES = [
    Path(directory) for directory in (
        os.environ.get("OLLAMA_MODELS"),
        Path.home() / ".ollama" / "models",
    ) if directory
]
DEFAULT_MODEL = "hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q2_K_XL"
DEFAULT_PORT = int(os.environ.get("AIOS_LOCAL_LLM_PORT", "11500"))


def resolve_blob(tag: str) -> Path:
    """Map an Ollama tag to the GGUF blob on disk."""
    name, _, version = tag.partition(":")
    version = version or "latest"
    if "/" not in name:
        name = f"registry.ollama.ai/library/{name}"
    elif name.startswith("hf.co/"):
        pass
    elif name.count("/") == 1:
        name = f"registry.ollama.ai/{name}"
    tried = []
    for store in MODEL_STORES:
        manifest = store / "manifests" / Path(name) / version
        tried.append(manifest)
        if not manifest.exists():
            continue
        data = json.loads(manifest.read_text(encoding="utf-8"))
        for layer in data.get("layers", []):
            if layer.get("mediaType", "").endswith(".model"):
                blob = store / "blobs" / layer["digest"].replace(":", "-")
                if blob.exists():
                    return blob
    separator = chr(10) + "  "
    listed = separator.join(str(path) for path in tried)
    raise SystemExit(f"no GGUF for {tag!r}; looked in:{separator}{listed}")


def cuda_backend() -> Path | None:
    """The ggml CUDA backend Ollama ships, newest CUDA major first.

    llama-server only discovers backends next to its own executable, so running
    it straight out of Ollama's lib directory finds the CPU backends and reports
    "no usable GPU found". GGML_BACKEND_PATH points it at the right DLL.
    """
    for directory in sorted(OLLAMA_LIB.glob("cuda_v*"), reverse=True):
        dll = directory / "ggml-cuda.dll"
        if dll.exists():
            return dll
    return None


def free_vram_mib() -> int | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total,memory.used",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip().splitlines()[0]
        total, used = (int(value) for value in out.split(","))
        return total - used
    except Exception:
        return None


def build_command(blob: Path, args: argparse.Namespace) -> list[str]:
    server = OLLAMA_LIB / "llama-server.exe"
    if not server.exists():
        raise SystemExit(f"llama-server not found at {server}; is Ollama installed?")
    command = [
        str(server), "-m", str(blob),
        "--port", str(args.port),
        "--host", args.host,
        "-c", str(args.ctx),
        "-fa", "on",
        # An fp16 KV cache is what pushes a 27B out of 16 GB, not the weights.
        "-ctk", "q8_0", "-ctv", "q8_0",
        "--no-webui",
        # One slot: llama-server sizes the KV cache per slot, and four idle
        # slots are what tips a 27B over the edge of a 16 GB card.
        "--parallel", "1",
        # --jinja uses the template embedded in the GGUF, which is what makes
        # tool calls parse into native tool_calls instead of leaking as text.
        "--jinja",
    ]
    if args.spec != "none":
        command += ["--spec-type", args.spec]
    # llama.cpp logs "failed to fit params ... abort" when -ngl is set
    # explicitly. That is the auto-fitter standing down, not an error: it then
    # honours the requested count. Letting it fit instead strands layers on the
    # CPU and costs about three quarters of the speed.
    command += ["-ngl", str(args.gpu_layers)]
    return command


def wait_until_ready(base: str, process: subprocess.Popen, timeout: float) -> bool:
    """/health answers before the weights are in, so probe a real completion."""
    body = json.dumps({"model": "local", "max_tokens": 1,
                       "messages": [{"role": "user", "content": "hi"}]}).encode()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        try:
            request = Request(f"{base}/v1/chat/completions", data=body,
                              headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=10) as response:
                if json.loads(response.read()).get("choices"):
                    return True
        except Exception:
            time.sleep(3)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=DEFAULT_MODEL, help="an installed Ollama tag")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--ctx", type=int, default=32768)
    parser.add_argument("--spec", default="draft-mtp",
                        help="draft-mtp (default), ngram-map-k, ngram-cache, or none")
    parser.add_argument("--gpu-layers", type=int, default=99)
    parser.add_argument("--timeout", type=float, default=420.0)
    args = parser.parse_args()

    blob = resolve_blob(args.model)
    environment = dict(os.environ)
    backend = cuda_backend()
    if backend is not None:
        environment["GGML_BACKEND_PATH"] = str(backend)
        environment["PATH"] = f"{backend.parent};{OLLAMA_LIB};{environment.get('PATH', '')}"
    else:
        print("warning: no CUDA backend found next to Ollama; this will run on the CPU",
              file=sys.stderr)

    command = build_command(blob, args)
    base = f"http://{args.host}:{args.port}"
    free = free_vram_mib()
    if free is not None and free < 12000:
        print(f"warning: only {free} MiB of VRAM free. Orphaned llama-server "
              f"processes outlive a killed parent and hold their weights; "
              f"check for them before blaming the model.", file=sys.stderr)
    print(f"model    {args.model}")
    print(f"blob     {blob.name[:23]}...  ({blob.stat().st_size / 2**30:.1f} GB)")
    print(f"context  {args.ctx}   speculation  {args.spec}")
    print(f"loading  {base}/v1 ...", flush=True)

    process = subprocess.Popen(command, env=environment)
    try:
        if not wait_until_ready(base, process, args.timeout):
            print("server failed to become ready", file=sys.stderr)
            process.terminate()
            return 1
        print(f"ready    {base}/v1  (OpenAI-compatible, native tool calls)")
        print("         Ctrl-C to stop")
        process.wait()
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
