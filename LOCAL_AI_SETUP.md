# Local AI Setup

Hardware found:

- CPU: AMD Ryzen 7 3700X, 8 cores / 16 threads
- GPU: NVIDIA GeForce RTX 2070 SUPER, 8 GB VRAM
- RAM: 16 GB
- Model cache: `D:\AI\OllamaModels`

Downloaded models:

- `gpt-oss:20b` - default, stronger reasoning/agentic model
- `qwen3:14b` - alternate Qwen reasoning model

Measured smoke tests:

- `gpt-oss:20b`: first load about 79s, warmed-up medium reasoning prompt about 14s
- `qwen3:14b`: first load about 105s, warmed-up thinking prompt about 80s

Notes:

- The desktop shortcut is `Local AI Chat` on the Desktop.
- The GUI talks to Ollama locally at `http://localhost:11434`.
- The `Tools` toggle enables safe local tools: calculator, current time, workspace file list, and workspace text-file read.
- For GPT-OSS, the `Think` toggle uses medium reasoning when enabled and low reasoning when disabled.

Run:

```powershell
.\run-local-ai-chat.ps1
```

or double-click:

```text
run-local-ai-chat.bat
```
