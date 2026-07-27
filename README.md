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

Tap **+** in the chat bar to send photos or text files with a message — you can also paste a screenshot straight into the box, or drag files onto the window on a desktop browser. Photos are resized on the phone, travel through the relay's object store, and land in front of OPERATOR before your task starts: photograph a bank statement, ask it to find the matching receipts, and it works from the image. Attaching mid-run hands the file to the task already in flight.

The conversation is yours to keep. **New chat** starts a fresh one that stays fresh — the tasks you cleared never come back, however many times the phone reopens the app. Otherwise, leave for an hour and the recent tasks come back as one thread, with everything OPERATOR did while you were gone still under the message that started it. Nothing in the chat is ever deleted to save room: older steps fold behind a **Show earlier steps** button, and a phone that is out of storage keeps the thread on screen and says so. Only **New chat** clears the feed.

**Settings → Notifications** wakes the phone when OPERATOR needs an answer, runs out of steps, or finishes — with the app closed. The relay sends real Web Push (VAPID + RFC 8291), so nothing depends on the app being awake to poll. On iPhone, add aiOS Remote to your Home Screen first: iOS only allows notifications for installed web apps. **Send a test notification** proves the round trip.

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
| `phone_site/` | Installable PWA and Sites worker relay. Run `npm run build` to produce its deployable `dist/`. |
| `aios_updater.py` | In-app updater. CLI: `python aios_updater.py check\|update\|restart`. |
| `install_aios.py` | Installer wizard. |

### Deploying aiOS Remote

The phone app and the relay it talks to ship separately, which is worth
knowing when a new feature only half works.

| Piece | Where it runs | How it updates |
| --- | --- | --- |
| The PWA (`phone_site/index.html`, `phone.js`, `phone.css`, `sw.js`) | GitHub Pages, `callewallerstedt.github.io` | Automatically. `.github/workflows/pages.yml` runs `npm run build` and publishes on every push to `main` that touches `phone_site/**`. |
| The relay API (`phone_site/worker/index.js`) | The OpenAI apps host in `.openai/hosting.json`, serving `aios-remote-control.contact-wallerstedt.chatgpt.site` with its D1 (`DB`) and R2 (`FILES`) bindings | Redeploy that hosting project. Pushing to `main` does **not** do it. |

So a change to the chat, the timeline, or anything else in the app is live as
soon as Pages finishes — bump `CACHE` in `sw.js` so phones fetch it. Anything
that needs a new API route is not, and the app is built to notice: photo
attachments fall back to travelling inside the message, notifications say the
relay is out of date instead of failing silently, and the rest carries on.

`wrangler.jsonc` is kept for `npm run dev`, which runs the worker locally
against isolated storage.

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
