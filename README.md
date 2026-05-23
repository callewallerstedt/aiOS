<div align="center">
  <p>
    <img src="assets/aios-logo.png" width="112" alt="aiOS logo">
  </p>
  <img src="assets/operator-logo.png" width="560" alt="OPERATOR logo">
  <h1>aiOS</h1>
  <p><strong>Windows desktop helper with OPERATOR built in.</strong></p>
  <p>
    <img alt="Python" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white">
    <img alt="Windows" src="https://img.shields.io/badge/Windows-desktop-0078D4?style=flat-square&logo=windows&logoColor=white">
    <img alt="OPERATOR" src="https://img.shields.io/badge/OPERATOR-Agent%20Clicker-38D996?style=flat-square">
  </p>
</div>

## Run

```powershell
.\install-aios.ps1
```

or double-click `install-aios.bat`.

or manually:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python helper_overlay.py
```

## Install

```powershell
.\install-aios.ps1
.\install-aios.bat
.\install-startup.ps1
.\install-voice.ps1
```

## OPERATOR

OPERATOR loads the bundled `agent_clicker` folder from this repo.

```powershell
copy agent_clicker\.env.example agent_clicker\.env
```

Use `.\install-aios.ps1` to sign in with Codex, enable OPERATOR Codex mode, and pre-download the Whisper `small` and OPERATOR OCR models. You can also set `OPENAI_API_KEY` in aiOS Settings, your environment, or `agent_clicker\.env`.

## Local Files

`helper_config.json`, logs, virtual environments, `.env` files, and Agent Clicker debug runs stay local.
