@echo off
:: Macro Deck: bind to a button you tap WHILE holding the dictate key.
:: Sends the transcript to: clipboard
powershell -NoProfile -WindowStyle Hidden -Command "$c=New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1',48737); $s=$c.GetStream(); $b=[Text.Encoding]::UTF8.GetBytes('target:clipboard'); $s.Write($b,0,$b.Length); $c.Close()"
