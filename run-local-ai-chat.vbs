Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = scriptDir
shell.Environment("PROCESS")("OLLAMA_MODELS") = "D:\AI\OllamaModels"
shell.Run "pythonw.exe """ & scriptDir & "\local_ai_chat.py""", 0, False
