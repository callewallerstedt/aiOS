# aiOS UI (the real GUI)

This folder is **aiOS** — the live WebView2 overlay.

- Shell entry: `../aios_shell.py`
- Frontend: `web/` (`js/`, `css/`, `index.html`)
- Backend API: `server.py`, `api.py`, `*.py` modules here

`../helper_overlay.py` is the deprecated Tkinter UI. Do not edit it for GUI
work. Put CODE / tabs / layout / session-list changes in this package.
