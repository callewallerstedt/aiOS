"""Monitor enumeration + screenshot capture via mss."""
from __future__ import annotations
from dataclasses import dataclass
import mss
from PIL import Image


@dataclass
class Monitor:
    index: int            # 1-based; 0 = "all monitors" virtual screen
    left: int
    top: int
    width: int
    height: int
    label: str

    def contains(self, gx: int, gy: int) -> bool:
        return self.left <= gx < self.left + self.width and self.top <= gy < self.top + self.height


def list_monitors() -> list[Monitor]:
    out: list[Monitor] = []
    with mss.mss() as sct:
        for i, m in enumerate(sct.monitors):
            label = ("All monitors" if i == 0 else f"Monitor {i}") + f"  {m['width']}x{m['height']} @ ({m['left']},{m['top']})"
            out.append(Monitor(index=i, left=m["left"], top=m["top"],
                               width=m["width"], height=m["height"], label=label))
    return out


def capture(mon: Monitor) -> Image.Image:
    with mss.mss() as sct:
        raw = sct.grab({"left": mon.left, "top": mon.top, "width": mon.width, "height": mon.height})
        return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
