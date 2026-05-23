$ErrorActionPreference = "Stop"
Write-Host "Installing voice dictation dependencies..."
python -m pip install --upgrade pip
python -m pip install keyboard numpy sounddevice faster-whisper
Write-Host ""
Write-Host "Done. Launch with:  pythonw voice_dictation.py"
Write-Host "Or use run-voice.bat."
