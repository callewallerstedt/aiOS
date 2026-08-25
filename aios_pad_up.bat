@echo off
:: Macro Deck: bind to aiOS button UP (released).
:: Release <200ms → full aiOS. Hold already opened Quick Tools.
powershell -NoProfile -WindowStyle Hidden -Command "$ports=@(48739,48736); foreach($p in $ports){ try { $c=New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1',$p); $s=$c.GetStream(); $b=[Text.Encoding]::UTF8.GetBytes('pad_up'); $s.Write($b,0,$b.Length); $c.Close(); exit 0 } catch {} }; exit 1"
