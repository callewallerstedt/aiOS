@echo off
:: Macro Deck: panic button for the voice agent.
:: Stops the spoken reply immediately, aborts the turn the agent is running,
:: and cancels an OPERATOR job if one is in progress. Safe to press any time.
powershell -NoProfile -WindowStyle Hidden -Command "$c=New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1',48737); $s=$c.GetStream(); $b=[Text.Encoding]::UTF8.GetBytes('stop_agent'); $s.Write($b,0,$b.Length); $c.Close()"
