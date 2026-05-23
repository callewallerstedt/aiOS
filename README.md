# aiOS

Windows desktop helper with project tools, voice dictation, local chat, startup visuals, and bundled OPERATOR support through Agent Clicker.

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python helper_overlay.py
```

Optional startup/hotkey install:

```powershell
.\install-startup.ps1
```

Voice dictation dependencies can also be installed with:

```powershell
.\install-voice.ps1
```

## OPERATOR

OPERATOR loads the bundled `agent_clicker` folder from this repo. Configure an OpenAI key either in aiOS Settings, as `OPENAI_API_KEY`, or in:

```powershell
copy agent_clicker\.env.example agent_clicker\.env
```

Then edit `agent_clicker\.env` and run `python helper_overlay.py`.

## Local Files

`helper_config.json`, logs, virtual environments, Agent Clicker debug runs, and `.env` files are ignored on purpose. Use `helper_config.example.json` as a clean starting point if needed.
