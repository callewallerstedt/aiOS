@echo off
set OLLAMA_MODELS=C:\AI\OllamaModels
py -3 "%~dp0local_ai_chat.py"
if errorlevel 1 python "%~dp0local_ai_chat.py"
