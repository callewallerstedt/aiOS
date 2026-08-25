"""aiOS-button short/hold gesture for the macropad.

Nothing opens on key-down.
  • Release under HOLD_MS → toggle the full aiOS shell (or close Quick Tools)
  • Still down at HOLD_MS → Quick Tools overlay only (main stays hidden)

One-shot ``toggle`` (Macro Deck single action) is treated as a short tap → aiOS.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .api import NativeApi, QuickToolsApi

HOLD_MS = 200


class PadGesture:
    def __init__(
        self,
        main: NativeApi,
        quick_tools: QuickToolsApi,
        *,
        hold_ms: int = HOLD_MS,
    ) -> None:
        self._main = main
        self._qt = quick_tools
        self._hold_ms = max(50, int(hold_ms))
        self._lock = threading.Lock()
        self._down = False
        self._long = False
        self._generation = 0
        self._timer: threading.Timer | None = None

    def toggle_main(self) -> dict:
        """Short press / one-shot toggle — full aiOS, not the palette."""
        if self._qt.is_open():
            self._qt.hide()
            return {"ok": True, "phase": "closed_quick_tools"}
        self._main.toggle()
        return {"ok": True, "phase": "tap"}

    def open_quick_tools(self) -> dict:
        try:
            self._main.hide()
        except Exception:
            pass
        self._qt.show()
        return {"ok": True, "phase": "quick_tools"}

    def down(self) -> dict:
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._down = True
            self._long = False
            if self._timer is not None:
                self._timer.cancel()
            timer = threading.Timer(self._hold_ms / 1000.0, self._fire_long, args=(generation,))
            timer.daemon = True
            self._timer = timer
            timer.start()
        # Start the Lenovo while the hold is still being decided so Webcam
        # is already streaming by the time Quick Tools appears.
        try:
            self._qt.warm_webcam()
        except Exception:
            pass
        return {"ok": True, "phase": "down"}

    def up(self) -> dict:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            was_down = self._down
            was_long = self._long
            self._down = False
            self._long = False
            self._generation += 1
        if not was_down:
            return {"ok": True, "phase": "idle"}
        if was_long:
            return {"ok": True, "phase": "hold"}
        return self.toggle_main()

    def cancel(self) -> dict:
        """Abort a press (e.g. dictate stole the hold)."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._down = False
            self._long = False
            self._generation += 1
        self._qt.hide()
        return {"ok": True, "phase": "cancelled"}

    def _fire_long(self, generation: int) -> None:
        with self._lock:
            if generation != self._generation or not self._down or self._long:
                return
            self._long = True
        # Hold: Quick Tools only — never raise the full shell.
        self.open_quick_tools()
