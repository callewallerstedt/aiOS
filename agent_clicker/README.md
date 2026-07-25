# Agent Clicker

A VLM-driven UI click-targeting agent. Input: a screenshot + a task like
"click cell B12" / "click the Chrome icon" / "click the second video".
Output: precise (x, y) pixel coordinates in the original image — reached via
multi-round reasoning over a toolkit of CV primitives.

The agent's job is to **think then click**: each round it either calls a tool
(OCR, set-of-marks, grid, crop, color mask, icon detector, describe) or commits
a final coordinate. Everything streams to a Flask debug UI so you can see every
round, every tool output, every annotated intermediate image.

## Why this works

- **Set-of-marks**: every OCR text and detected icon is numbered on the image,
  then the VLM just picks a number. This is the technique that makes
  GPT-4o-class models reliable at UI clicking, instead of asking them to
  guess pixel coordinates directly.
- **Crop & retry**: when ambiguous, the agent zooms in and re-runs tools on
  the smaller region. Excel cell B12 = crop to spreadsheet → OCR column
  header "B" → OCR row label "12" → click their intersection.
- **Multi-tool**: text targets use OCR; icons use edge/contour detection;
  spatial targets use grid; known-color targets use color masks.

## Setup (Windows / PowerShell)

```powershell
# 1. clone / cd into folder
cd "C:\Claude Code\agent clicker"

# 2. (recommended) a venv
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 3. deps (PyTorch is the heaviest — CPU wheels are fine)
pip install -r requirements.txt

# 4. configure
copy .env.example .env
# edit .env -> paste OPENAI_API_KEY, pick AGENT_MODEL (gpt-5.6-luna by default)

# 5. run
python run.py
# open http://127.0.0.1:5000
```

First run downloads EasyOCR weights (~80 MB). They cache in `%USERPROFILE%\.EasyOCR\`.

## Using the UI

1. Pick a screenshot file.
2. Type a task: any of —
   - `click the Chrome icon`
   - `click cell B12`
   - `click the search input`
   - `click the second video in the list`
   - `click the red close button`
3. Pick a model (`gpt-5.6-luna` is the default fast OPERATOR model).
4. Press **Run**. Each round appears live with the VLM's thought, the tool
   call, an annotated image, and a text summary. The final card shows a green
   crosshair at the click point.

## Available tools

| Tool | What it does |
| --- | --- |
| `ocr` | EasyOCR with 2× upscaling; returns numbered text boxes + centers |
| `set_of_marks` | OCR + icon detection combined, all numbered for picking |
| `grid` | Overlays a labeled (A1, B2…) grid for spatial reasoning |
| `crop` | Zooms into a region (subsequent tools run there) |
| `color_mask` | Finds pixel clusters matching an RGB color |
| `find_icons` | Heuristic edge/contour-based icon candidates |
| `describe` | Falls back to a second VLM pass for a natural-language read |
| `commit` / `commit_mark` | Finalize (x,y) — either raw coords or by mark ID |

## Optional heavier models

Switch to a richer detector by editing `.env`:

- `OMNIPARSER_PATH`: path to a Microsoft OmniParser-V2 checkpoint dir. Will
  replace `find_icons` with a structured UI parse. (Plug-in slot — not bundled.)
- `SAM_CHECKPOINT`: path to a Segment-Anything ViT checkpoint. Enables
  segment-based proposals.

These are heavy (multi-GB, GPU recommended) and currently wired as plug-in
slots — the lean stack (EasyOCR + OpenCV + VLM set-of-marks) is what runs by
default and what's used in the tester.

## Repo layout

```
agent/
  config.py        env config
  vlm.py           OpenAI vision client
  prompts.py       system prompt
  orchestrator.py  multi-round loop
  tools/
    ocr.py marks.py grid.py crop.py color.py icons.py describe.py
app/
  server.py        Flask + SSE
  templates/index.html
  static/main.js style.css
run.py             launcher
```

## Notes on model names

The default clicking model is `gpt-5.6-luna`. The model selector also exposes
`gpt-5.6-terra` and `gpt-5.6-sol` for accounts that have access to them.

## Troubleshooting

- **`OPENAI_API_KEY missing`**: `.env` not loaded — make sure you copied
  `.env.example` and filled it in, and that you launched `python run.py` from
  the project root.
- **EasyOCR slow on first run**: it's downloading weights. Subsequent runs
  are fast (CPU is fine for screenshot-sized images).
- **OCR misses small text**: the agent should `crop` to that region and
  re-OCR. If it doesn't, lower `AGENT_MAX_ROUNDS` and re-prompt with a more
  specific task ("click cell B12 in the visible Excel sheet").
- **VLM returns invalid JSON**: the orchestrator surfaces the error to the
  model and lets it retry within `AGENT_MAX_ROUNDS`.
