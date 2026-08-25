@echo off
:: Macro Deck one-shot: toggle full aiOS (same as a short tap).
:: For hold → Quick Tools, bind aios_pad_down.bat / aios_pad_up.bat instead.
powershell -NoProfile -WindowStyle Hidden -Command "$ports=@(48739,48736); foreach($p in $ports){ try { $c=New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1',$p); $s=$c.GetStream(); $b=[Text.Encoding]::UTF8.GetBytes('toggle'); $s.Write($b,0,$b.Length); $c.Close(); exit 0 } catch {} }; Start-Process -FilePath 'C:\aiOS\.venv\Scripts\pythonw.exe' -ArgumentList 'C:\aiOS\aios_shell.py' -WorkingDirectory 'C:\aiOS' -WindowStyle Hidden -ErrorAction SilentlyContinue; exit 1"
