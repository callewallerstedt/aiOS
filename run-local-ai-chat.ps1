$env:OLLAMA_MODELS = 'C:\AI\OllamaModels'
$python = Get-Command py -ErrorAction SilentlyContinue
if ($python) {
    py -3 "$PSScriptRoot\local_ai_chat.py"
} else {
    python "$PSScriptRoot\local_ai_chat.py"
}
