"""Hotkey control socket for the WebView2 aiOS shell.



AutoHotkey (and the legacy helper_overlay.py --toggle entry) talk to

127.0.0.1:48736 — the same port the Tk build used. When the background Tk

helper still owns that port, WebView also listens on WEBVIEW_PORT so pad /

quick-tools commands have a reliable home.

"""



from __future__ import annotations



import socket

import threading

from typing import Callable



HOST = "127.0.0.1"

PORT = 48736

WEBVIEW_PORT = 48739





def send_command(command: str, timeout: float = 0.2) -> bool:

    """Fire-and-forget a control word at the running shell. True if delivered."""

    payload = str(command or "").encode("utf-8")

    if not payload:

        return False

    for port in (WEBVIEW_PORT, PORT):

        try:

            with socket.create_connection((HOST, port), timeout=timeout) as client:

                client.sendall(payload)

            return True

        except OSError:

            continue

    return False





class ControlServer:

    """Tiny TCP server that turns hotkey words into window actions."""



    def __init__(self, handler: Callable[[str], None], port: int = WEBVIEW_PORT) -> None:

        self._handler = handler

        self._port = int(port)

        self._sock: socket.socket | None = None

        self._thread: threading.Thread | None = None

        self._stopping = False



    def start(self) -> bool:

        """Bind the hotkey port. False if something else already owns it."""

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        try:

            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            sock.bind((HOST, self._port))

            sock.listen(8)

            sock.settimeout(1.0)

        except OSError:

            sock.close()

            return False

        self._sock = sock

        self._stopping = False

        self._thread = threading.Thread(

            target=self._serve, daemon=True, name=f"aios-control-{self._port}"

        )

        self._thread.start()

        return True



    def stop(self) -> None:

        self._stopping = True

        sock = self._sock

        self._sock = None

        if sock is not None:

            try:

                sock.close()

            except OSError:

                pass



    def _serve(self) -> None:

        sock = self._sock

        if sock is None:

            return

        while not self._stopping:

            try:

                conn, _addr = sock.accept()

            except socket.timeout:

                continue

            except OSError:

                break

            try:

                data = conn.recv(64)

            except OSError:

                data = b""

            finally:

                try:

                    conn.close()

                except OSError:

                    pass

            command = (data or b"").decode("utf-8", errors="replace").strip().lower()

            if not command:

                continue

            try:

                self._handler(command)

            except Exception:

                pass


