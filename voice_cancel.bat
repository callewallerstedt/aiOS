@echo off
:: Macro Deck: throw away what you just said without sending it anywhere.
powershell -NoProfile -WindowStyle Hidden -Command "$c=New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1',48737); $s=$c.GetStream(); $b=[Text.Encoding]::UTF8.GetBytes('cancel'); $s.Write($b,0,$b.Length); $c.Close()"
