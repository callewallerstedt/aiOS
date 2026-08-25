import ctypes
import json
import math
import threading
import time
import tkinter as tk
from datetime import datetime
from urllib.parse import quote
from urllib.request import urlopen

from tk_win_fixes import suppress_tk_monitor_windows


LOCATION = "Lerkil, Sweden"
DISPLAY_LOCATION = "Vallda Sando / Lerkil"
UPDATE_SECONDS = 30 * 60
WIDTH = 500
HEIGHT = 520
MARGIN_X = 28
MARGIN_Y = 72
TRANSPARENT_COLOR = "#010203"


WEATHER_LABELS = {
    0: "Clear",
    1: "Mostly clear",
    2: "Partly cloudy",
    3: "Cloudy",
    45: "Fog",
    48: "Rime fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    56: "Freezing drizzle",
    57: "Freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Light showers",
    81: "Showers",
    82: "Heavy showers",
    85: "Snow showers",
    86: "Snow showers",
    95: "Thunder",
    96: "Thunder hail",
    99: "Thunder hail",
}


def get_json(url):
    with urlopen(url, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def geocode(location):
    url = (
        "https://geocoding-api.open-meteo.com/v1/search"
        f"?name={quote(location)}&count=1&language=en&format=json"
    )
    data = get_json(url)
    results = data.get("results") or []
    if not results:
        raise RuntimeError(f"Location not found: {location}")
    result = results[0]
    label = result.get("name", location)
    country = result.get("country")
    if country:
        label = f"{label}, {country}"
    return result["latitude"], result["longitude"], label


def fetch_weather():
    latitude, longitude, label = geocode(LOCATION)
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={latitude}&longitude={longitude}"
        "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        "&forecast_days=10&timezone=auto"
    )
    data = get_json(url)
    return label, data


def fmt_temp(value):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "--"
    return f"{round(value):d}°"


def weather_text(code):
    return WEATHER_LABELS.get(int(code), "Weather")


def make_forecast_text(label, data):
    current = data["current"]
    daily = data["daily"]
    updated = datetime.now().strftime("%H:%M")
    wind = round(current.get("wind_speed_10m", 0))
    condition = weather_text(current.get("weather_code", 0))
    rows = []
    for idx, day in enumerate(daily["time"][:10]):
        date = datetime.fromisoformat(day)
        name = "Today" if idx == 0 else date.strftime("%a %d")
        high = fmt_temp(daily["temperature_2m_max"][idx])
        low = fmt_temp(daily["temperature_2m_min"][idx])
        rain = daily.get("precipitation_probability_max", [None] * 10)[idx]
        rain_text = "--%" if rain is None else f"{round(rain):d}%"
        summary = weather_text(daily["weather_code"][idx])
        rows.append((name, high, low, rain_text, summary))

    return {
        "location": DISPLAY_LOCATION or label,
        "temp": fmt_temp(current.get("temperature_2m")),
        "feels": fmt_temp(current.get("apparent_temperature")),
        "condition": condition,
        "wind": f"{wind} km/h",
        "rows": rows,
        "updated": updated,
    }


class WeatherOverlay:
    def __init__(self):
        self.root = tk.Tk()
        # Hide immediately — otherwise Windows flashes a blank "tk" frame for
        # hundreds of ms before transparentcolor / first paint kick in.
        self.root.withdraw()
        suppress_tk_monitor_windows()
        self.root.title("aiOS Weather")
        self.root.overrideredirect(True)
        self.root.configure(bg=TRANSPARENT_COLOR)
        try:
            self.root.attributes("-transparentcolor", TRANSPARENT_COLOR)
        except tk.TclError:
            pass
        self.root.attributes("-topmost", False)

        screen_w = self.root.winfo_screenwidth()
        x = screen_w - WIDTH - MARGIN_X
        y = MARGIN_Y
        self.root.geometry(f"{WIDTH}x{HEIGHT}+{x}+{y}")

        self.canvas = tk.Canvas(
            self.root,
            width=WIDTH,
            height=HEIGHT,
            bg=TRANSPARENT_COLOR,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)

        self._shown = False
        self.root.update_idletasks()
        self._apply_tool_window_styles()
        suppress_tk_monitor_windows()
        for delay in (0, 100, 400, 1200):
            self.root.after(delay, suppress_tk_monitor_windows)
        self.update_async()

    def _apply_tool_window_styles(self):
        try:
            hwnd = self.root.winfo_id()
            user32 = ctypes.windll.user32
            ex_style = user32.GetWindowLongW(hwnd, -20)
            ex_style |= 0x00000020  # WS_EX_TRANSPARENT (click-through)
            ex_style |= 0x00000080  # WS_EX_TOOLWINDOW (no taskbar entry)
            ex_style |= 0x08000000  # WS_EX_NOACTIVATE
            user32.SetWindowLongW(hwnd, -20, ex_style)
        except (AttributeError, OSError, tk.TclError):
            pass

    def make_click_through(self):
        self._apply_tool_window_styles()
        self.send_to_back()

    def send_to_back(self):
        try:
            hwnd = self.root.winfo_id()
            HWND_BOTTOM = 1
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOACTIVATE = 0x0010
            ctypes.windll.user32.SetWindowPos(
                hwnd, HWND_BOTTOM, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
            )
        except (AttributeError, OSError, tk.TclError):
            pass

    def update_async(self):
        thread = threading.Thread(target=self.update_weather, daemon=True)
        thread.start()
        self.root.after(UPDATE_SECONDS * 1000, self.update_async)

    def update_weather(self):
        try:
            label, data = fetch_weather()
            payload = make_forecast_text(label, data)
        except Exception as exc:
            payload = {
                "location": DISPLAY_LOCATION,
                "temp": "--",
                "feels": "--",
                "condition": "Weather unavailable",
                "wind": "--",
                "rows": [("Retrying", "--", "--", "--", str(exc)[:24])],
                "updated": datetime.now().strftime("%H:%M"),
            }
        self.root.after(0, lambda: self.draw(payload))

    def round_rect(self, x1, y1, x2, y2, radius, fill, outline=""):
        points = [
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1,
        ]
        return self.canvas.create_polygon(points, smooth=True, fill=fill, outline=outline)

    def draw(self, weather):
        c = self.canvas
        c.delete("all")
        self.round_rect(8, 8, WIDTH - 8, HEIGHT - 8, 22, "#11151A", "#2B3138")

        c.create_text(28, 30, anchor="nw", text="WEATHER", fill="#7E8792", font=("Segoe UI Semibold", 9))
        c.create_text(28, 48, anchor="nw", text=weather["location"], fill="#F3F0E8", font=("Segoe UI Semibold", 18))

        c.create_text(28, 88, anchor="nw", text=weather["temp"], fill="#FFFFFF", font=("Segoe UI Semibold", 46))
        c.create_text(144, 100, anchor="nw", text=weather["condition"], fill="#D7D3C9", font=("Segoe UI", 13), width=320)
        c.create_text(144, 126, anchor="nw", text=f"Feels {weather['feels']}   Wind {weather['wind']}", fill="#9DA5AE", font=("Segoe UI", 10))

        self.round_rect(24, 170, WIDTH - 24, 474, 14, "#171C22", "#303842")
        c.create_text(38, 188, anchor="nw", text="10 DAY FORECAST", fill="#87909B", font=("Segoe UI Semibold", 9))
        c.create_text(175, 188, anchor="nw", text="HIGH", fill="#87909B", font=("Segoe UI Semibold", 9))
        c.create_text(230, 188, anchor="nw", text="LOW", fill="#87909B", font=("Segoe UI Semibold", 9))
        c.create_text(282, 188, anchor="nw", text="RAIN", fill="#87909B", font=("Segoe UI Semibold", 9))

        y = 216
        for idx, (name, high, low, rain, summary) in enumerate(weather["rows"][:10]):
            if idx:
                c.create_line(38, y - 7, WIDTH - 38, y - 7, fill="#242B33")
            name_fill = "#F2EFE7" if idx == 0 else "#D9D5CC"
            c.create_text(38, y, anchor="nw", text=name, fill=name_fill, font=("Segoe UI Semibold", 11))
            c.create_text(198, y, anchor="ne", text=high, fill="#F2EFE7", font=("Segoe UI", 11))
            c.create_text(252, y, anchor="ne", text=low, fill="#B8C0C8", font=("Segoe UI", 11))
            c.create_text(322, y, anchor="ne", text=rain, fill="#8FC7FF", font=("Segoe UI", 11))
            c.create_text(342, y, anchor="nw", text=summary, fill="#B8C0C8", font=("Segoe UI", 10), width=126)
            y += 26

        c.create_text(28, HEIGHT - 34, anchor="nw", text=f"Updated {weather['updated']}", fill="#69727D", font=("Segoe UI", 9))
        if not self._shown:
            self._shown = True
            self._apply_tool_window_styles()
            self.root.deiconify()
            self.root.update_idletasks()
            self.make_click_through()
        else:
            self.root.after(50, self.send_to_back)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    WeatherOverlay().run()
