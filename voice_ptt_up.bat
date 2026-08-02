@echo off
:: Macro Deck: bind this to Button UP (released).
powershell -NoProfile -WindowStyle Hidden -Command "$c=New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1',48737); $s=$c.GetStream(); $b=[Text.Encoding]::UTF8.GetBytes('ptt_up'); $s.Write($b,0,$b.Length); $c.Close()"
