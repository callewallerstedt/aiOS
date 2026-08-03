# Local AI Setup

Hardware found on 2026-05-25:

- CPU: AMD Ryzen 7 9800X3D, 8 cores / 16 threads
- GPU: NVIDIA GeForce RTX 5070 Ti, 16 GB VRAM
- Integrated GPU: AMD Radeon(TM) Graphics
- RAM: 32 GB
- Model cache: `C:\AI\OllamaModels`

Installed runtime:

- Ollama 0.30.5
- Local API: `http://localhost:11434`

Downloaded models:

- `qwen3.6-agent:27b` - default local coding/text chat model, backed by `unsloth/Qwen3.6-27B-GGUF:UD-Q3_K_XL`
- `hf.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:UD-Q4_K_XL` - Qwen3 Coder 30B A3B Instruct GGUF, Unsloth dynamic Q4 XL quant, fallback coding/text model
- `qwen3-vl:30b` - official Ollama Qwen3-VL 30B vision model, default for OPERATOR `limit clicks`
- `qwen3-vl:8b` - official Ollama Qwen3-VL 8B vision model, tested working
- `hf.co/Qwen/Qwen3-VL-8B-Instruct-GGUF:Q4_K_M` - downloaded, but not selected by default because the HF GGUF import crashed during Ollama image requests
- `hf.co/Qwen/Qwen3-VL-30B-A3B-Instruct-GGUF:Q4_K_M` - downloaded, but not selected by default because the HF GGUF import crashed during Ollama image requests
- `qwen3:14b` - alternate local chat / reasoning model
- `qwen2.5vl:7b` - local vision model for screenshot experiments

Smoke tests:

- `qwen3:14b` responded successfully through `ollama run`.
- `qwen3.6-agent:27b` responded successfully through aiOS's `ollama_stream_chat(..., reasoning="off")` path.
- `hf.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:UD-Q4_K_XL` downloaded successfully through `ollama pull`.
- `hf.co/Qwen/Qwen3-VL-30B-A3B-Instruct-GGUF:Q4_K_M` downloaded successfully through `ollama pull`.
- `hf.co/Qwen/Qwen3-VL-8B-Instruct-GGUF:Q4_K_M` downloaded successfully through `ollama pull`.
- The HF Qwen3-VL GGUF imports crashed during Ollama vision requests even after unloading other models.
- Official `qwen3-vl:8b` and `qwen3-vl:30b` both completed Ollama image smoke tests successfully with `"think": false`.
- `qwen3:14b` loaded onto the RTX 5070 Ti and used about 11 GB of 16 GB VRAM.
- `qwen2.5vl:7b` described a live desktop screenshot through Ollama's `/api/generate` endpoint.
- During the vision test, GPU utilization reached about 98% and VRAM use was about 13.9 GB.

Run:

```powershell
.\run-local-ai-chat.ps1
```

or double-click:

```text
run-local-ai-chat.bat
```

Useful commands:

```powershell
ollama list
ollama run qwen3.6-agent:27b --think=false
ollama run hf.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:UD-Q4_K_XL
ollama run qwen3:14b
ollama run qwen3-vl:30b
ollama run qwen3-vl:8b
ollama run qwen2.5vl:7b
```

aiOS integration:

1. Run `python helper_overlay.py` or restart aiOS.
2. Open the `Local AI` tab.
3. Pick a local Ollama model from the dropdown. The tab uses `qwen3.6-agent:27b` by default and streams through Ollama at `http://localhost:11434`.
4. Use `Paste Image` or `Attach Image` with Qwen3-VL/Qwen2.5-VL models to send screenshots or image files.
5. Use the `Reasoning` dropdown to choose `off`, `low`, `medium`, or `high`. Keep `off` for Qwen3.6 Agent when you want direct answers; aiOS maps this to Ollama's thinking flag plus a generation budget, and streams reasoning/live output when the model exposes it.
6. Qwen3-VL may stream a `thinking` field before final text; aiOS now shows that muted stream so the chat does not look frozen.
7. Press `Pull` only if the model is missing or you change the model field to another Ollama/Hugging Face GGUF tag.

OPERATOR local mode:

1. Open `OPERATOR`.
2. Local OPERATOR models default to `limit clicks`.
3. This runs OPERATOR planning through the local `qwen3-vl:30b` Ollama vision model, sends screenshots to the local model, keeps shell/file actions available, and blocks direct visual clicks.
4. Before execution, aiOS asks the local text planner model for a minimal-click strategy using shell/hotkeys/direct URLs where possible; the vision executor treats this plan as guidance and adapts to the live screenshot.
5. The local OPERATOR prompt is compact for faster first tokens and tells the model to prefer hotkeys/shell over clicking. Reasoning streams into the live output panel when the model exposes it.
6. While the local model is generating, the OPERATOR Activity panel shows a live stream of the raw local output.
7. When the local model needs a click it proposes named targets. Approve with `Yes`; aiOS sends the screenshot and target list to `gpt-5.5` to resolve coordinates, then the local model clicks approved IDs with `click_target`.

GUI evaluation flow:

1. Run `.\run-local-ai-chat.ps1`.
2. Open the `Evaluation` tab.
3. Use `Paste Image` after copying a screenshot, or `Capture Screen`.
4. Type a target prompt such as `click the search box`.
5. Press `Evaluate`.
6. The app asks `qwen2.5vl:7b` for original-image coordinates and draws the predicted point on the image.
7. Right-click the correct target point, or enable `Mark Mode` and left-click it.
8. Press `Save Example` to append a training row to `training_data/gui_clicks/dataset.jsonl` and save the PNG under `training_data/gui_clicks/screenshots/`.

Batch training-data flow:

1. Open the `Generate Training Data` tab.
2. Pick a screen and press `Capture`, or press `Paste Image`.
3. Either enter one click instruction per line and press `Load Prompt List`, or set `Count` to any positive integer and press `Suggest More with GPT`.
4. Review the labels and plotted points.
5. Select a prompt and right-click the correct point on the image to adjust it. The app advances to the next prompt automatically.
6. Press `Save Labeled Batch`.

`Suggest More with GPT` uses Codex auth (`~/.codex/auth.json`) and asks `gpt-5.5` for additional diverse clickable targets across the current screenshot. Existing targets are included in the request so GPT avoids duplicates. Saved rows are still marked as human-reviewed because you review/adjust before saving.
