import ctypes
import json
import queue
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path

import psutil


ROOT = Path(r"C:\aiOS")
CONFIG = ROOT / "quick_launch.json"
TRANSPARENT_COLOR = "#010203"
HEALTH_WIDTH = 520
HEALTH_HEIGHT = 430
HEALTH_X = 28
HEALTH_Y = 96
STRIP_HEIGHT = 76
UPDATE_MS = 2000
GPU_UPDATE_MS = 5000
DISK_UPDATE_MS = 30000


class History:
    def __init__(self, size=60):
        self.size = size
        self.values = []

    def add(self, value):
        self.values.append(float(value))
        del self.values[:-self.size]


def set_bottom(hwnd):
    ctypes.windll.user32.SetWindowPos(hwnd, 1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)


def make_toolwindow(hwnd, click_through=False):
    ex_style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
    ex_style |= 0x00000080 | 0x08000000
    if click_through:
        ex_style |= 0x00000020
    ctypes.windll.user32.SetWindowLongW(hwnd, -20, ex_style)
    set_bottom(hwnd)


def run_command(command):
    kwargs = {"cwd": str(ROOT), "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    subprocess.Popen(command, shell=not isinstance(command, list), **kwargs)


def query_gpu():
    try:
        command = [
            "nvidia-smi",
            "--query-gpu=temperature.gpu,utilization.gpu,utilization.memory,memory.used,memory.total,power.draw,power.limit,fan.speed",
            "--format=csv,noheader,nounits",
        ]
        values = [part.strip() for part in subprocess.check_output(command, text=True, timeout=2).strip().split(",")]
        return {
            "temp": float(values[0]), "util": float(values[1]), "mem_util": float(values[2]),
            "mem_used": float(values[3]), "mem_total": float(values[4]), "power": float(values[5]),
            "power_limit": float(values[6]), "fan": float(values[7]) if values[7].replace(".", "", 1).isdigit() else None,
        }
    except Exception:
        return None


def disk_summary(path):
    usage = psutil.disk_usage(path)
    return usage.percent, usage.free / (1024 ** 3), usage.total / (1024 ** 3)


def round_rect(canvas, x1, y1, x2, y2, radius, fill, outline=""):
    points = [
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, fill=fill, outline=outline)


class HealthWidget:
    GRAPH_SPECS = {
        "cpu": (116, 88, 160, 38, "#75D1FF", 100),
        "ram": (116, 144, 160, 38, "#9FE870", 100),
        "gpu": (116, 200, 160, 38, "#F2C572", 100),
        "net_down": (116, 304, 160, 30, "#67E8C9", 500),
        "net_up": (116, 338, 160, 30, "#F488B8", 500),
    }

    def __init__(self, root):
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-transparentcolor", TRANSPARENT_COLOR)
        self.win.configure(bg=TRANSPARENT_COLOR)
        self.win.geometry(f"{HEALTH_WIDTH}x{HEALTH_HEIGHT}+{HEALTH_X}+{HEALTH_Y}")
        self.canvas = tk.Canvas(
            self.win, width=HEALTH_WIDTH, height=HEALTH_HEIGHT,
            bg=TRANSPARENT_COLOR, highlightthickness=0, bd=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self.histories = {name: History() for name in self.GRAPH_SPECS}
        self.last_net = psutil.net_io_counters()
        self.last_net_t = time.monotonic()
        self.gpu_data = None
        self.gpu_results = queue.Queue(maxsize=1)
        self.gpu_query_running = False
        self.last_gpu_t = 0.0
        self.disk_data = disk_summary("C:\\")
        self.last_disk_t = time.monotonic()
        self.text_items = {}
        self.graph_items = {}
        self.core_bars = []
        self._build_static_canvas()
        self._start_gpu_query()
        self.win.after(500, make_toolwindow, self.win.winfo_id(), True)
        self.update()

    def _label(self, x, y, text, size=10, color="#D9D5CC", family="Segoe UI"):
        return self.canvas.create_text(x, y, anchor="nw", text=text, fill=color, font=(family, size))

    def _dynamic_label(self, key, x, y, size=10, color="#D9D5CC", family="Segoe UI"):
        self.text_items[key] = self._label(x, y, "", size, color, family)

    def _build_static_canvas(self):
        c = self.canvas
        round_rect(c, 8, 8, HEALTH_WIDTH - 8, HEALTH_HEIGHT - 8, 22, "#11151A", "#2B3138")
        self._label(28, 28, "SYSTEM HEALTH", 9, "#7E8792", "Segoe UI Semibold")
        self._label(28, 48, "Ryzen 7 9800X3D  \u2022  RTX 5070 Ti", 16, "#F3F0E8", "Segoe UI Semibold")
        self._label(292, 92, "CPU temp: sensor unavailable", 10, "#8F98A3")
        self._label(28, 314, "NET", 13, "#F2EFE7", "Segoe UI Semibold")
        for key, (x, y, width, height, color, _maximum) in self.GRAPH_SPECS.items():
            round_rect(c, x, y, x + width, y + height, 7, "#10151A", "#28303A")
            self.graph_items[key] = c.create_line(0, 0, 0, 0, fill=color, width=2, smooth=True, state="hidden")
        self._dynamic_label("cpu", 28, 92, 13, "#F2EFE7", "Segoe UI Semibold")
        self._dynamic_label("ram", 28, 148, 13, "#F2EFE7", "Segoe UI Semibold")
        self._dynamic_label("ram_detail", 292, 148, 10, "#B8C0C8")
        self._dynamic_label("gpu", 28, 204, 13, "#F2EFE7", "Segoe UI Semibold")
        self._dynamic_label("vram", 292, 204, 10, "#B8C0C8")
        self._dynamic_label("gpu_power", 292, 224, 10, "#8F98A3")
        self._dynamic_label("disk", 28, 260, 13, "#F2EFE7", "Segoe UI Semibold")
        self._dynamic_label("disk_detail", 292, 258, 10, "#B8C0C8")
        self._dynamic_label("down", 292, 306, 10, "#B8C0C8")
        self._dynamic_label("up", 292, 338, 10, "#B8C0C8")
        round_rect(c, 116, 265, 276, 277, 6, "#10151A", "#28303A")
        self.disk_bar = c.create_rectangle(118, 267, 118, 275, fill="#C895FF", outline="")
        for index in range(16):
            x = 28 + (index % 8) * 56
            y = 382 + (index // 8) * 20
            round_rect(c, x, y, x + 42, y + 10, 5, "#10151A", "#28303A")
            self.core_bars.append(c.create_rectangle(x + 1, y + 1, x + 1, y + 9, fill="#75D1FF", outline=""))

    def _start_gpu_query(self):
        if self.gpu_query_running:
            return
        self.gpu_query_running = True

        def worker():
            result = query_gpu()
            try:
                self.gpu_results.put_nowait(result)
            except queue.Full:
                pass

        threading.Thread(target=worker, daemon=True, name="aios-widget-gpu").start()

    def _poll_gpu(self):
        try:
            self.gpu_data = self.gpu_results.get_nowait()
        except queue.Empty:
            return
        self.gpu_query_running = False
        self.last_gpu_t = time.monotonic()

    def _update_graph(self, key):
        x, y, width, height, _color, maximum = self.GRAPH_SPECS[key]
        history = self.histories[key]
        item = self.graph_items[key]
        if len(history.values) < 2:
            return
        step = width / max(1, history.size - 1)
        points = []
        for index, value in enumerate(history.values):
            points.extend((x + index * step, y + height - max(0, min(value, maximum)) / maximum * (height - 8) - 4))
        self.canvas.coords(item, *points)
        self.canvas.itemconfigure(item, state="normal")

    def update(self):
        now = time.monotonic()
        per_core = psutil.cpu_percent(interval=None, percpu=True)
        cpu = sum(per_core) / len(per_core) if per_core else 0.0
        memory = psutil.virtual_memory()
        if (now - self.last_disk_t) * 1000 >= DISK_UPDATE_MS:
            self.disk_data = disk_summary("C:\\")
            self.last_disk_t = now
        network = psutil.net_io_counters()
        elapsed = max(0.1, now - self.last_net_t)
        down = (network.bytes_recv - self.last_net.bytes_recv) * 8 / elapsed / 1_000_000
        up = (network.bytes_sent - self.last_net.bytes_sent) * 8 / elapsed / 1_000_000
        self.last_net, self.last_net_t = network, now
        self._poll_gpu()
        if (now - self.last_gpu_t) * 1000 >= GPU_UPDATE_MS:
            self._start_gpu_query()
        self.histories["cpu"].add(cpu)
        self.histories["ram"].add(memory.percent)
        self.histories["net_down"].add(min(down, 500))
        self.histories["net_up"].add(min(up, 500))
        if self.gpu_data:
            self.histories["gpu"].add(self.gpu_data["util"])
            gpu = self.gpu_data
            gpu_text = f"GPU {gpu['util']:4.0f}%  {gpu['temp']:.0f}C"
            vram = f"VRAM {gpu['mem_used'] / 1024:.1f}/{gpu['mem_total'] / 1024:.1f} GB"
            fan = "fan --" if gpu["fan"] is None else f"fan {gpu['fan']:.0f}%"
            gpu_power = f"{gpu['power']:.0f}/{gpu['power_limit']:.0f} W  {fan}"
        else:
            gpu_text, vram, gpu_power = "GPU --", "VRAM --", "power --  fan --"
        disk = self.disk_data
        values = {
            "cpu": f"CPU {cpu:4.0f}%", "ram": f"RAM {memory.percent:4.0f}%",
            "ram_detail": f"{memory.used / (1024 ** 3):.1f} / {memory.total / (1024 ** 3):.0f} GB",
            "gpu": gpu_text, "vram": vram, "gpu_power": gpu_power,
            "disk": f"C: {disk[0]:.0f}%", "disk_detail": f"{disk[1]:.0f} GB free / {disk[2]:.0f} GB",
            "down": f"down {down:.1f} Mbps", "up": f"up   {up:.1f} Mbps",
        }
        for key, text in values.items():
            self.canvas.itemconfigure(self.text_items[key], text=text)
        for key in self.GRAPH_SPECS:
            self._update_graph(key)
        self.canvas.coords(self.disk_bar, 118, 267, 118 + int(156 * disk[0] / 100), 275)
        for index, value in enumerate(per_core[:16]):
            x = 28 + (index % 8) * 56
            y = 382 + (index // 8) * 20
            self.canvas.coords(self.core_bars[index], x + 1, y + 1, x + 1 + int(40 * value / 100), y + 9)
        self.win.after(UPDATE_MS, self.update)


class LaunchStrip:
    def __init__(self, root):
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-transparentcolor", TRANSPARENT_COLOR)
        self.win.configure(bg=TRANSPARENT_COLOR)
        screen_width, screen_height = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        self.width = min(920, screen_width - 80)
        x = int((screen_width - self.width) / 2)
        y = screen_height - STRIP_HEIGHT - 58
        self.win.geometry(f"{self.width}x{STRIP_HEIGHT}+{x}+{y}")
        self.canvas = tk.Canvas(self.win, width=self.width, height=STRIP_HEIGHT, bg=TRANSPARENT_COLOR, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.icon_cache = {}
        self.config_stamp = None
        self.win.after(500, make_toolwindow, self.win.winfo_id(), False)
        self.draw(force=True)

    def load_items(self):
        if not CONFIG.exists():
            return []
        with CONFIG.open("r", encoding="utf-8") as handle:
            return json.load(handle).get("apps", [])

    def _icon(self, path):
        key = str(path or "")
        if key in self.icon_cache:
            return self.icon_cache[key]
        if not key or not Path(key).exists():
            return None
        try:
            self.icon_cache[key] = tk.PhotoImage(file=key).subsample(2, 2)
        except Exception:
            self.icon_cache[key] = None
        return self.icon_cache[key]

    def draw(self, force=False):
        try:
            stamp = CONFIG.stat().st_mtime_ns
        except OSError:
            stamp = 0
        if not force and stamp == self.config_stamp:
            self.win.after(10000, self.draw)
            return
        self.config_stamp = stamp
        canvas = self.canvas
        canvas.delete("all")
        round_rect(canvas, 8, 8, self.width - 8, STRIP_HEIGHT - 8, 18, "#11151A", "#2B3138")
        x = 24
        for app in self.load_items()[:12]:
            name, command = app.get("name", "App"), app.get("command")
            round_rect(canvas, x, 18, x + 54, 62, 12, "#171C22", "#303842")
            image = self._icon(app.get("icon"))
            if image:
                canvas.create_image(x + 27, 40, image=image)
            else:
                canvas.create_text(x + 27, 31, text=name[:2].upper(), fill="#F3F0E8", font=("Segoe UI Semibold", 13))
            canvas.create_text(x + 27, 55, text=name[:8], fill="#98A2AD", font=("Segoe UI", 7), anchor="n")
            hit = canvas.create_rectangle(x, 18, x + 54, 62, fill="", outline="")
            canvas.tag_bind(hit, "<Button-1>", lambda _event, cmd=command: run_command(cmd))
            x += 68
        self.win.after(10000, self.draw)


def main():
    root = tk.Tk()
    root.withdraw()
    HealthWidget(root)
    LaunchStrip(root)
    root.mainloop()


if __name__ == "__main__":
    main()
