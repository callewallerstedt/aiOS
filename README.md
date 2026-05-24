<div align="center">
  <p>
    <img src="assets/aios-logo.png" width="112" alt="aiOS logo">
  </p>
  <img src="assets/operator-logo.png" width="560" alt="OPERATOR logo">
  <h1>aiOS</h1>
  <p><strong>Windows desktop helper with OPERATOR (computer-use agent) built in.</strong></p>
  <p>
    <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white">
    <img alt="Windows" src="https://img.shields.io/badge/Windows-desktop-0078D4?style=flat-square&logo=windows&logoColor=white">
    <img alt="OPERATOR" src="https://img.shields.io/badge/OPERATOR-Agent%20Clicker-38D996?style=flat-square">
  </p>
</div>

## What it is

aiOS is a Windows desktop overlay that gives you:

- **OPERATOR** — a computer-use agent that drives your mouse + keyboard from natural language, with a phone companion ([phonesite-six.vercel.app](https://phonesite-six.vercel.app)) for follow-ups while it runs.
- **Voice dictation** — hold a hotkey, speak, release; Whisper transcribes locally.
- **Quick chat** — overlay chat hooked to OpenAI / Codex.
- **Project + todo dashboard** — a personal layer over a folder of markdown files.

The two surfaces talk to each other on `127.0.0.1:48736`, plus a Flask server in `agent_clicker/` for the phone.

## Quick start

```powershell
git clone https://github.com/callewallerstedt/aiOS.git
cd aiOS
.\install-aios.ps1
```

The installer is a Tk wizard. It:

1. Verifies you have Python 3.10+.
2. Installs `requirements.txt`.
3. Pre-downloads Whisper `small` (≈460 MB) and EasyOCR (`en` + `sv`).
4. Writes voice defaults.
5. Optionally signs you in with Codex.
6. Creates `agent_clicker/.env` from the example.
7. Optionally installs AutoHotkey via `winget` and registers the startup hotkey.
8. Verifies everything imports.

Optional steps that fail (e.g. no internet for whisper) won't kill the install — the wizard logs them and continues.

### Manual setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python helper_overlay.py
```

## OpenAI / Codex auth

OPERATOR can run in two modes:

- **API mode** — set `OPENAI_API_KEY` in `agent_clicker/.env`, the system environment, or aiOS **Settings → Models**.
- **Codex mode** — sign in once during install (`.\install-aios.ps1` opens the Codex login flow). The toggle is in the OPERATOR panel.

The phone UI uses the same backend, so once one side is configured the phone works.

## Updating

Open **Settings → Update aiOS** inside the running helper. It:

- Compares your local SHA with `origin/main` on GitHub.
- Pulls via `git` (or downloads the tarball if the install isn't a git clone).
- Reinstalls `requirements.txt`.
- Restarts the helper.

Sources (`owner` / `repo` / `branch`) live in `helper_config.json` under `update_source` and are editable in Settings or during the installer's first run. The default points at this repo's `main`.

## Developing

Clone and run as above. Useful surfaces:

| Path | What it is |
| --- | --- |
| `helper_overlay.py` | The Tk overlay process — chat, settings, OPERATOR panel, hotkeys. |
| `agent_clicker/app/server.py` | Flask server for the phone (`/api/phone/...`). |
| `agent_clicker/desktop_agent/loop.py` | OPERATOR agent loop — screenshot → reason → act. |
| `agent_clicker/desktop_agent/prompts.py` | OPERATOR system prompt. |
| `phone_site/` | Static phone UI deployed to Vercel. Run `npm run build` to refresh `public/`. |
| `aios_updater.py` | In-app updater. CLI: `python aios_updater.py check\|update\|restart`. |
| `install_aios.py` | Installer wizard. |

### Running the phone backend

The phone hits `/api/phone/...` on the same machine. Run the Flask server:

```powershell
python agent_clicker\app\server.py
```

Open the phone UI and point its **Backend URL** at your tunnel (Tailscale, ngrok, etc.) or `http://localhost:5000` if local.

### Local-only files

These stay on your machine and aren't committed:

- `helper_config.json`, `helper_config.local.json`
- `agent_clicker/.env`
- `phone_operator_events/` (live operator stream)
- `debug_runs/` (per-run dumps)
- `.venv/` / `venv/`

The updater preserves all of them when pulling.

### Commit style

One-line title, optional bullet body. Keep changes scoped. Branches: feature branches → PR into `main`.

## Troubleshooting

- **"GitHub could not be reached" in Updater** — check the repo is public (it is) and your network reaches `api.github.com`. The 404 path was the old private-repo case; it's gone now.
- **Whisper download failed during install** — re-run the installer; it'll resume from the failure. Or download manually: `python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"`.
- **`Module not found` after install** — re-open the installer and just check "Python dependencies" + "Verify install" runs.
- **OPERATOR can't see your monitor** — Settings → Run-on-monitor in the OPERATOR panel; the helper auto-detects via `mss`.

## Layout

```
aiOS/
├── helper_overlay.py        # Main Tk overlay
├── install_aios.py          # Install wizard
├── aios_updater.py          # In-app updater
├── agent_clicker/
│   ├── app/server.py        # Phone backend
│   └── desktop_agent/       # OPERATOR loop
├── phone_site/              # Phone UI (Vercel)
├── assets/                  # Logos, splash frames
└── requirements.txt
```
