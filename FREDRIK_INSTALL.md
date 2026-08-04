# Fredrik install checklist

Short agent/human checklist to get aiOS dictation, transcription, and the voice agent working on a Windows PC.

## 1. Clone + install

```powershell
git clone https://github.com/callewallerstedt/aiOS.git
cd aiOS
.\install-aios.ps1
```

In the installer wizard, leave these **on**:

- Python dependencies
- Download Whisper `small`
- Voice defaults
- Sign in with Codex (needed for the voice / ChatGPT agent)
- AutoHotkey (if missing)
- Keep aiOS running after Windows sign-in (startup watchdog)

## 2. Confirm Whisper (transcription)

If the installer skipped the model download:

```powershell
python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"
```

No NVIDIA GPU (or CUDA fails) → open **Settings → Voice Dictation** and set:

- Device: **cpu**
- Compute: **int8**

## 3. Auto-startup

The installer should register the logon watchdog. If not:

```powershell
.\install-startup.ps1
```

Check that Task Scheduler has **aiOS Watchdog**, or that Startup has the watchdog shortcut. After a reboot, aiOS, hotkeys, and dictation should come back without a manual start.

## 4. Auth for the voice agent

Finish Codex login in the installer, **or** set `OPENAI_API_KEY` in `agent_clicker/.env` / Settings.

Without this, hold-to-talk dictation still works. The agent / ChatGPT button will not.

## 5. Two buttons (dictate + agent)

| Button | Example key | What to bind |
| --- | --- | --- |
| Hold-to-talk (dictate) | **End** | Settings → Voice Dictation → **Voice hotkey = End** |
| Agent / ChatGPT | **Page Down** | Run `voice_target_agent.bat` in the aiOS folder |

1. In Settings, set **Voice hotkey** to `End` (or `PageDown` if that key is preferred for PTT).
2. Bind the **other** key in macro software / AHK / Stream Deck to:

```text
C:\path\to\aiOS\voice_target_agent.bat
```

(Use the real clone path on that machine.)

### How to use it

1. Hold **End** (dictate)
2. Tap **Page Down** (agent) while still holding
3. Speak
4. Release **End** → transcript goes to the voice agent

Skip step 2 → transcript types into the focused window instead.

## 6. Smoke test

- Hold End → speak → release → text appears / types
- Hold End → tap Page Down → speak → release → agent overlay replies (spoken + chat)
- Reboot once → aiOS is still listening without a manual start

## 7. Notes

- Do not commit `helper_config.json` or `agent_clicker/.env` — they stay local.
- Plain dictation only needs Whisper + mic. The talking agent also needs Codex or an OpenAI API key.
- Optional macro bats live in the repo root: `voice_ptt_down.bat`, `voice_ptt_up.bat`, `voice_target_agent.bat`, `voice_stop_agent.bat`.
