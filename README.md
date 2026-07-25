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

- **OPERATOR** — a computer-use agent that drives your mouse + keyboard from natural language, with an installable aiOS Remote PWA for controlling multiple paired PCs, sending follow-ups, and watching live activity and screenshots.
- **Voice dictation** — hold a hotkey, speak, release; Whisper transcribes locally.
- **Quick chat** — overlay chat hooked to OpenAI / Codex.
- **Project + todo dashboard** — a personal layer over a folder of markdown files.

The two surfaces talk to each other on `127.0.0.1:48736`, plus a Flask server in `agent_clicker/` for the phone.

Press **Ctrl+Space** to open or hide aiOS. The recommended startup watchdog keeps aiOS, its hotkeys, and a paired phone remote running in the background after sign-in.

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
7. Optionally installs AutoHotkey via `winget` and registers the resilient per-user startup watchdog.
8. Verifies everything imports.

The watchdog starts aiOS silently at Windows sign-in, checks each component every few seconds, and repairs the desktop helper, hotkeys, local OPERATOR bridge, or paired phone relay if one stops. The visual splash remains available for manual launches, but startup audio is disabled.

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
- **Codex mode** — click **Sign in with Codex** in the OPERATOR panel. aiOS opens the official Codex OAuth flow, detects completion automatically, and enables Codex mode. Use **Switch account** there whenever you want to add or change the active login.

OPERATOR defaults to `gpt-5.6-luna`, including when it uses your Codex account.
You can choose a separate pre-run planning model (Sol by default) while keeping Luna as the less expensive clicking model. Set Planning model to Off to begin execution immediately.

### aiOS Remote on your phone

1. Open [aiOS Remote](https://aios-remote-control.contact-wallerstedt.chatgpt.site) and choose **Create my private remote**.
2. Save the private code; it is the recovery and pairing secret for your remote.
3. On each PC, open aiOS **Settings → Mobile remote**.
4. Paste the website URL and private code, give the computer a friendly name, and press **Connect**.
5. Install the PWA from the browser menu or the install button.

Each PC gets its own machine credential. The phone can switch computers, send a new task or follow-up, stop a run, choose a monitor, and watch live OPERATOR activity and screenshots. No inbound port or temporary tunnel is required.

## Updating

Open **Settings → Update aiOS** inside the running helper and press **Update & restart**. That one press does everything:

- Checks the latest commit on GitHub for the configured `owner/repo@branch` (no need to press Check first).
- Pulls via `git fetch` + `git reset --hard` onto that commit — straight from the configured source, even if the clone's `origin` points somewhere stale.
- Falls back to downloading the tarball automatically if `git` is missing or the fetch fails.
- Reinstalls `requirements.txt`.
- Restarts the helper (tarball updates are swapped in by a small applier after the helper exits, because Windows holds the running files open).

If you're already on the latest commit it says so and skips the restart.

Same thing from a terminal:

```powershell
python aios_updater.py auto           # check, pull, install, relaunch
python aios_updater.py auto --force   # reinstall even if already current
python aios_updater.py check          # just report current vs latest
```

Sources (`owner` / `repo` / `branch`) live in `helper_config.json` under `update_source` and are editable in Settings or during the installer's first run. The default points at this repo's `main`. Local edits to tracked files are discarded by the update; untracked files (your config, logs, models) are left alone.

## Developing

Clone and run as above. Useful surfaces:

| Path | What it is |
| --- | --- |
| `helper_overlay.py` | The Tk overlay process — chat, settings, OPERATOR panel, hotkeys. |
| `agent_clicker/app/server.py` | Flask server for the phone (`/api/phone/...`). |
| `agent_clicker/desktop_agent/loop.py` | OPERATOR agent loop — screenshot → reason → act. |
| `agent_clicker/desktop_agent/prompts.py` | OPERATOR system prompt. |
| `phone_site/` | Installable PWA and Sites worker relay. Run `npm run build` to produce its deployable `dist/`. |
| `aios_updater.py` | In-app updater. CLI: `python aios_updater.py check\|update\|auto\|restart`. |
| `install_aios.py` | Installer wizard. |

### Running the mobile bridge

After pairing in **Settings → Mobile remote**, aiOS starts both the local Flask bridge and the secure outbound relay automatically. To start it manually:

```powershell
.\start-phone-bridge.ps1
```


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
- **Update fails with a git error** — the updater retries the fetch three times and then falls back to downloading the tarball on its own, so this should self-heal. Run `python aios_updater.py auto` in the install folder to see the full log; `update-apply.log` and `update-failures.log` hold the post-restart half.
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
