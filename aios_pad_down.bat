@echo off
:: Macro Deck: bind to aiOS button DOWN (pressed). Nothing opens yet.
:: Pair with aios_pad_up.bat on release. Hold ≥200ms → Quick Tools.
powershell -NoProfile -WindowStyle Hidden -Command "$ports=@(48739,48736); foreach($p in $ports){ try { $c=New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1',$p); $s=$c.GetStream(); $b=[Text.Encoding]::UTF8.GetBytes('pad_down'); $s.Write($b,0,$b.Length); $c.Close(); exit 0 } catch {} }; exit 1"
