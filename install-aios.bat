@echo off
setlocal
cd /d "%~dp0"
python install_aios.py
if errorlevel 1 pause
