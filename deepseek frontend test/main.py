#!/usr/bin/env python3
"""
aiOS Overlay — PySide6 Frontend
Rebuilt from helper_overlay.py using Qt for smooth rendering, animations,
and efficient scrolling.

Layout (mirrors Tkinter original):
  ┌─────────────────────────────────────────────┐
  │  Header (drag bar: aiOS title + toolbar)     │
  ├──────┬──────────────────────┬───────────────┤
  │ Nav  │     Page             │  Chat Panel   │
  │(158px)│   (fills space)     │  (330px def)  │
  │      │                     │               │
  ├──────┴──────────────────────┴───────────────┤
  │           Bottom Tray (animated)            │
  └─────────────────────────────────────────────┘
"""

import sys, os, json, time, math, queue, threading, ctypes, io, uuid, hashlib
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Callable

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt, Signal, Slot, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPixmap, QPainter, QBrush, QPen
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFrame, QScrollArea, QTextEdit, QStackedWidget,
    QListWidget, QListWidgetItem, QSizePolicy, QSplitter, QMenu, QCheckBox,
    QLineEdit, QButtonGroup, QComboBox, QProgressBar, QToolButton, QSlider,
)

# ── Project root helpers ────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _read_json_object(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_json_atomic(path: Path, value: dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# ── Config ──────────────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "theme": {
        "accent": "#4f9fff",
        "panel": "#0f0f0f",
        "panel2": "#1a1a1a",
        "surface": "#141414",
        "surface2": "#1e1e1e",
        "text": "#e0e0e0",
        "muted": "#808080",
        "danger": "#f04747",
        "success": "#43b581",
        "warning": "#faa61a",
    },
    "project_root": str(PROJECT_ROOT),
    "opacity": 0.96,
    "always_on_top": True,
    "window": {"width": 1280, "height": 800},
    "chat_width": 330,
    "code_speak_notifications": True,
}

CONFIG_PATH = PROJECT_ROOT / "helper_config.json"


def load_config() -> dict:
    cfg = _read_json_object(CONFIG_PATH)
    # Deep copy defaults
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    if cfg is not None:
        merged.update(cfg)
    # The config file stores window geometry as "WxH+X+Y" string; parse it
    win = merged.get("window")
    if isinstance(win, str):
        import re
        m = re.match(r'(\d+)x(\d+)', win)
        if m:
            merged["window"] = {"width": int(m.group(1)), "height": int(m.group(2))}
        else:
            merged["window"] = {"width": 1280, "height": 800}
    return merged


_theme_colors = None

class Theme:
    """Centralized theme lookup, matching the Tkinter `self.c()` pattern."""
    def __init__(self, config: dict):
        self._colors = config.get("theme", DEFAULT_CONFIG["theme"])

    def c(self, key: str) -> str:
        return self._colors.get(key, self._colors.get("text", "#e0e0e0"))

    def blend(self, a: str, b: str, ratio: float) -> str:
        ca = QColor(a)
        cb = QColor(b)
        r = ca.red() * (1 - ratio) + cb.red() * ratio
        g = ca.green() * (1 - ratio) + cb.green() * ratio
        b_ = ca.blue() * (1 - ratio) + cb.blue() * ratio
        return QColor(int(r), int(g), int(b_)).name()


# ── Helpers ─────────────────────────────────────────────────────────────────
def get_setting(name: str, default: str = "") -> str:
    cfg = load_config()
    return str(cfg.get(name, default))


def clean_project_name(raw: str) -> str:
    return raw.strip().rstrip("/\\")


def safe_join_project(project_root: str, _project_path: str, rel_path: str) -> str:
    base = Path(project_root) / rel_path
    return str(base)


def code_activity_key(event: dict) -> str:
    return event.get("activity", "") or event.get("id", "")


# ── Main Overlay Window ─────────────────────────────────────────────────────
class AiosOverlay(QMainWindow):
    """PySide6 implementation of the aiOS desktop overlay."""

    def __init__(self, background: bool = False):
        super().__init__()
        self.setWindowTitle("aiOS")
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        self.config = load_config()
        self.theme_obj = Theme(self.config)
        self.theme = self.theme_obj  # convenience alias
        self.project_root = Path(self.config["project_root"])

        # State
        self.active_tab = "Dashboard"
        self.settings_page = "General"
        self.chat_run_id = 0
        self.chat_busy_since = 0.0
        self.drag_pos = None
        self._ui_queue: queue.Queue = queue.Queue()
        self._code_poll_timer: QTimer | None = None
        self._chat_embeds = []
        self._live_tool_count = 0
        self._code_sessions_signature: str | None = None
        self.code_view_token = 0
        self.code_provider_var = "codex"
        self.code_model_var = "gpt-5.6-sol"
        self.code_reasoning_var = "medium"
        self.code_fast_var = False
        self.code_project_var = str(self.config.get("code_last_project_path", ""))
        self.code_brief_text = ""
        self.code_selected_id = ""
        self.code_jobs = []
        self.code_projects = []
        self.code_capabilities = {"providers": []}

        # Window geometry
        win = self.config.get("window", DEFAULT_CONFIG["window"])
        w, h = win.get("width", 1280), win.get("height", 800)
        self.resize(w, h)
        opacity = self.config.get("opacity", 0.96)
        self.setWindowOpacity(opacity)

        self._build_ui()

        # Poll queue for thread-safe updates
        self._queue_timer = QTimer(self)
        self._queue_timer.timeout.connect(self._process_queue)
        self._queue_timer.start(50)

        # Start code polling
        self._start_code_poll()

    # ── Color shortcuts ─────────────────────────────────────────────────────
    def c(self, key: str) -> str:
        return self.theme_obj.c(key)

    def blend(self, a: str, b: str, ratio: float) -> str:
        return self.theme_obj.blend(a, b, ratio)

    # ── UI Building ─────────────────────────────────────────────────────────
    def _build_ui(self):
        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)

        outer = QVBoxLayout(central)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(0)

        # Panel (the main window area)
        panel = QFrame()
        panel.setObjectName("panel")
        outer.addWidget(panel)

        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(0)

        # Header
        self._build_header(panel_layout)

        # Body (nav + page + chat)
        body = QWidget()
        body.setObjectName("body")
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(18, 8, 18, 18)
        body_layout.setSpacing(12)

        self.nav_widget = self._build_nav()
        body_layout.addWidget(self.nav_widget)

        self.stacked_pages = QStackedWidget()
        self.stacked_pages.setObjectName("page")
        body_layout.addWidget(self.stacked_pages, 1)

        self.chat_panel = self._build_chat()
        body_layout.addWidget(self.chat_panel)

        panel_layout.addWidget(body, 1)

        # Bottom tray
        self._build_bottom_tray(panel_layout)

        # Resize grip
        self._resize_grip = QLabel(panel)
        self._resize_grip.setObjectName("resizeGrip")
        self._resize_grip.setFixedSize(12, 12)
        self._resize_grip.setCursor(Qt.SizeFDiagCursor)
        self._resize_grip.move(panel.width() - 24, panel.height() - 24)
        self._resize_grip.mousePressEvent = self._start_resize
        self._resize_grip.mouseMoveEvent = self._drag_resize

        # Stylesheet
        self._apply_theme()

        # Render initial tab
        self._render_tab("Dashboard")

    def _build_header(self, parent_layout: QVBoxLayout):
        header = QFrame()
        header.setObjectName("header")
        header.setFixedHeight(56)
        header.mousePressEvent = self._start_move
        header.mouseMoveEvent = self._drag_move
        parent_layout.addWidget(header)

        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(18, 10, 18, 10)

        title = QLabel("aiOS")
        title.setObjectName("brandTitle")
        h_layout.addWidget(title)

        subtitle = QLabel(self._status_subtitle())
        subtitle.setObjectName("subtitle")
        h_layout.addWidget(subtitle)

        h_layout.addStretch()

        # Header toolbar
        self._build_header_toolbar(h_layout)

    def _status_subtitle(self) -> str:
        return "v2.0 · PySide6"

    def _build_header_toolbar(self, layout: QHBoxLayout):
        btns = [
            ("⚙ Settings", lambda: self._render_tab("Settings")),
            ("🗕 Minimize", self.showMinimized),
            ("✕ Close", self.close),
        ]
        for text, cb in btns:
            btn = QPushButton(text)
            btn.setObjectName("headerBtn")
            btn.clicked.connect(cb)
            layout.addWidget(btn)

    def _build_nav(self) -> QWidget:
        nav = QWidget()
        nav.setObjectName("nav")
        nav.setFixedWidth(158)
        nav_layout = QVBoxLayout(nav)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(4)

        tabs = [
            ("Dashboard", "Dashboard"),
            ("Projects", "Projects"),
            ("CODE", "CODE"),
            ("Apps", "Apps"),
            ("Drop", "Drop"),
            ("OPERATOR", "AI Operator"),
            ("Settings", "Settings"),
        ]
        self._nav_buttons = {}
        for label, value in tabs:
            btn = QPushButton(label)
            btn.setObjectName("navBtn")
            btn.setCheckable(True)
            btn.setChecked(value == self.active_tab)
            btn.clicked.connect(lambda checked=False, v=value: self._render_tab(v))
            btn.setCursor(Qt.PointingHandCursor)
            nav_layout.addWidget(btn)
            self._nav_buttons[value] = btn

        nav_layout.addStretch()
        return nav

    def _build_chat(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("chatPanel")
        panel.setFixedWidth(int(self.config.get("chat_width", 330)))
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Resize handle
        handle = QFrame()
        handle.setObjectName("chatResizeHandle")
        handle.setFixedWidth(8)
        handle.setCursor(Qt.SplitHCursor)
        handle.mousePressEvent = self._start_chat_resize
        handle.mouseMoveEvent = self._drag_chat_resize
        # We overlay this on the left side
        handle.setParent(panel)
        handle.move(0, 0)
        handle.setFixedHeight(panel.height())

        chat_content = QWidget()
        chat_content.setObjectName("chatContent")
        layout.addWidget(chat_content)

        chat_vert = QVBoxLayout(chat_content)
        chat_vert.setContentsMargins(12, 10, 12, 12)
        chat_vert.setSpacing(4)

        # Header
        head_left = QWidget()
        head_left.setObjectName("chatHeadLeft")
        head_left_layout = QVBoxLayout(head_left)
        head_left_layout.setContentsMargins(0, 0, 0, 0)
        head_left_layout.setSpacing(2)

        agent_label = QLabel("Agent")
        agent_label.setObjectName("agentLabel")
        head_left_layout.addWidget(agent_label)

        # Model info label
        self.chat_account_label = QLabel("gpt-5.6-luna · API key ready")
        self.chat_account_label.setObjectName("chatAccountLabel")
        self.chat_account_label.setCursor(Qt.PointingHandCursor)
        self.chat_account_label.mousePressEvent = lambda e: None  # placeholder
        head_left_layout.addWidget(self.chat_account_label)

        head_row = QWidget()
        head_row_layout = QHBoxLayout(head_row)
        head_row_layout.setContentsMargins(0, 0, 0, 0)
        head_row_layout.addWidget(head_left)
        head_row_layout.addStretch()

        for text, cb, hint in [
            ("Model", lambda: None, "Pick a model"),
            ("Reset", self._reset_chat, "Clear chat"),
            ("Settings", lambda: self._render_tab("Settings"), "Settings"),
        ]:
            chip = QPushButton(text)
            chip.setObjectName("headerChip")
            chip.setCursor(Qt.PointingHandCursor)
            chip.clicked.connect(cb)
            head_row_layout.addWidget(chip)

        chat_vert.addWidget(head_row)

        # Message area (scrollable)
        scroll = QScrollArea()
        scroll.setObjectName("chatScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.chat_scroll = scroll

        self.chat_inner = QWidget()
        self.chat_inner.setObjectName("chatInner")
        self.chat_msg_layout = QVBoxLayout(self.chat_inner)
        self.chat_msg_layout.setContentsMargins(2, 4, 2, 4)
        self.chat_msg_layout.setSpacing(6)
        self.chat_msg_layout.addStretch()

        scroll.setWidget(self.chat_inner)
        chat_vert.addWidget(scroll, 1)

        # Bottom input area
        bottom = QWidget()
        bottom.setObjectName("chatBottom")
        bottom_layout = QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(8)

        input_wrap = QFrame()
        input_wrap.setObjectName("chatInputWrap")
        input_wrap_layout = QVBoxLayout(input_wrap)
        input_wrap_layout.setContentsMargins(0, 0, 0, 0)

        self.chat_input = QTextEdit()
        self.chat_input.setObjectName("chatInput")
        self.chat_input.setPlaceholderText("Type a message...")
        self.chat_input.setFixedHeight(72)
        self.chat_input.setAcceptRichText(False)

        # Enter sends, Shift+Enter newline
        self.chat_input.keyPressEvent = lambda e: self._chat_keypress(e)
        input_wrap_layout.addWidget(self.chat_input)
        bottom_layout.addWidget(input_wrap, 1)

        send_btn = QPushButton("Send")
        send_btn.setObjectName("sendBtn")
        send_btn.setCursor(Qt.PointingHandCursor)
        send_btn.clicked.connect(self._send_chat)
        bottom_layout.addWidget(send_btn)

        chat_vert.addWidget(bottom)

        # Store handle reference for resize
        self._chat_resize_handle = handle

        return panel

    def _build_bottom_tray(self, parent_layout: QVBoxLayout):
        self._tray_open = False
        self._tray_anim_progress = 0.0

        tray = QFrame()
        tray.setObjectName("bottomTray")
        tray.setFixedHeight(24)
        self._bottom_tray = tray

        tray_layout = QHBoxLayout(tray)
        tray_layout.setContentsMargins(8, 2, 8, 2)
        tray_layout.setSpacing(6)

        # Handle button
        handle = QPushButton("⊞")
        handle.setObjectName("trayHandle")
        handle.setFixedWidth(24)
        handle.setCursor(Qt.PointingHandCursor)
        handle.clicked.connect(self._toggle_tray)
        tray_layout.addWidget(handle)

        # Quick status items
        for sym in ["🔋", "📶", "🔊", "🖥", "⏰"]:
            lbl = QLabel(sym)
            lbl.setObjectName("trayIcon")
            tray_layout.addWidget(lbl)

        tray_layout.addStretch()

        # Now playing & clock
        self._now_playing_label = QLabel("NOW PLAYING —")
        self._now_playing_label.setObjectName("trayNowPlaying")
        tray_layout.addWidget(self._now_playing_label)

        self._tray_clock = QLabel(datetime.now().strftime("%H:%M"))
        self._tray_clock.setObjectName("trayClock")
        tray_layout.addWidget(self._tray_clock)

        # Animated expand/collapse
        self._tray_animation = QPropertyAnimation(tray, b"maximumHeight")
        self._tray_animation.setDuration(250)
        self._tray_animation.setEasingCurve(QEasingCurve.OutCubic)
        self._tray_animation.valueChanged.connect(lambda h: tray.setFixedHeight(h))

        parent_layout.addWidget(tray)

        # Update tray clock every 30 seconds
        self._tray_clock_timer = QTimer(self)
        self._tray_clock_timer.timeout.connect(lambda: self._tray_clock.setText(datetime.now().strftime("%H:%M")))
        self._tray_clock_timer.start(30000)

    def _toggle_tray(self):
        self._tray_open = not self._tray_open
        target = 208 if self._tray_open else 24
        self._tray_animation.stop()
        self._tray_animation.setStartValue(self._bottom_tray.height())
        self._tray_animation.setEndValue(target)
        self._tray_animation.start()

    # ── Window dragging ─────────────────────────────────────────────────────
    def _start_move(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_pos = event.globalPosition().toPoint()

    def _drag_move(self, event):
        if self.drag_pos is not None and event.buttons() == Qt.LeftButton:
            self.move(self.pos() + event.globalPosition().toPoint() - self.drag_pos)
            self.drag_pos = event.globalPosition().toPoint()

    def _start_resize(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_pos = event.globalPosition().toPoint()
            self._resize_start_size = self.size()

    def _drag_resize(self, event):
        if self.drag_pos is not None and event.buttons() == Qt.LeftButton:
            delta = event.globalPosition().toPoint() - self.drag_pos
            new_w = max(860, self._resize_start_size.width() + delta.x())
            new_h = max(560, self._resize_start_size.height() + delta.y())
            self.resize(new_w, new_h)

    def _start_chat_resize(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_pos = event.globalPosition().toPoint()
            self._chat_resize_start_w = self.chat_panel.width()

    def _drag_chat_resize(self, event):
        if self.drag_pos is not None and event.buttons() == Qt.LeftButton:
            delta = self.drag_pos.x() - event.globalPosition().toPoint().x()
            new_w = max(200, min(600, self._chat_resize_start_w + delta))
            self.chat_panel.setFixedWidth(new_w)
            self.config["chat_width"] = new_w

    # ── Tab rendering ───────────────────────────────────────────────────────
    def _render_tab(self, tab: str):
        self.active_tab = tab
        # Update nav highlighting
        for val, btn in self._nav_buttons.items():
            btn.setChecked(val == tab)

        # Remove old pages
        while self.stacked_pages.count():
            w = self.stacked_pages.widget(0)
            self.stacked_pages.removeWidget(w)
            w.deleteLater()

        page = QWidget()
        page.setObjectName("tabPage")

        if tab == "Dashboard":
            self._render_dashboard(page)
        elif tab == "Projects":
            self._render_projects(page)
        elif tab == "CODE":
            self._render_code(page)
        elif tab == "Apps":
            self._render_apps(page)
        elif tab == "Drop":
            self._render_drop(page)
        elif tab == "AI Operator":
            self._render_operator(page)
        elif tab == "Settings":
            self._render_settings(page)

        self.stacked_pages.addWidget(page)
        self.stacked_pages.setCurrentWidget(page)

    def _render_dashboard(self, page: QWidget):
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        # Header
        head = QWidget()
        head_layout = QHBoxLayout(head)
        head_layout.setContentsMargins(0, 0, 0, 0)

        title = QLabel("Dashboard")
        title.setObjectName("pageTitle")
        head_layout.addWidget(title)

        head_layout.addStretch()

        create_btn = QPushButton("+ New")
        create_btn.setObjectName("actionBtn")
        create_btn.setCursor(Qt.PointingHandCursor)
        head_layout.addWidget(create_btn)

        refresh_btn = QPushButton("↻")
        refresh_btn.setObjectName("actionBtn")
        refresh_btn.setCursor(Qt.PointingHandCursor)
        head_layout.addWidget(refresh_btn)

        layout.addWidget(head)

        # Cards grid
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setObjectName("dashScroll")

        cards_widget = QWidget()
        cards_widget.setObjectName("dashCards")
        cards_grid = QHBoxLayout(cards_widget)
        cards_grid.setSpacing(12)

        # Left column
        left_col = QWidget()
        left_layout = QVBoxLayout(left_col)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(12)

        # Hero card
        hero = self._dash_card(left_layout, "WELCOME")
        hero_label = QLabel("Good day! Select an action to get started.")
        hero_label.setObjectName("dashHeroText")
        hero_label.setWordWrap(True)
        hero[1].addWidget(hero_label)

        # Weather card
        weather = self._dash_card(left_layout, "WEATHER")
        w_lbl = QLabel("Loading weather...")
        w_lbl.setObjectName("dashWeather")
        weather[1].addWidget(w_lbl)

        # Clock card with live updates
        clock_card = self._dash_card(left_layout, "CLOCK")
        self._dash_clock_label = QLabel(datetime.now().strftime("%H:%M:%S"))
        self._dash_clock_label.setObjectName("dashClock")
        clock_card[1].addWidget(self._dash_clock_label)
        self._dash_date_label = QLabel(datetime.now().strftime("%A, %B %d, %Y"))
        self._dash_date_label.setObjectName("dashDate")
        clock_card[1].addWidget(self._dash_date_label)

        # Start clock timer
        self._dash_clock_timer = QTimer(self)
        self._dash_clock_timer.timeout.connect(self._update_dash_clock)
        self._dash_clock_timer.start(1000)

        cards_grid.addWidget(left_col, 1)

        # Right column
        right_col = QWidget()
        right_layout = QVBoxLayout(right_col)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(12)

        # Markets
        markets = self._dash_card(right_layout, "MARKETS")
        m_lbl = QLabel("NASDAQ · DOW · S&P 500")
        m_lbl.setObjectName("dashMarkets")
        markets[1].addWidget(m_lbl)

        # GPU
        gpu_card = self._dash_card(right_layout, "GPU")
        gpu_lbl = QLabel("NVIDIA GeForce RTX 5070 Ti")
        gpu_lbl.setObjectName("dashGpu")
        gpu_card[1].addWidget(gpu_lbl)

        # Notes
        notes = self._dash_card(right_layout, "NOTES")
        notes_edit = QTextEdit()
        notes_edit.setObjectName("dashNotes")
        notes_edit.setPlaceholderText("Quick notes...")
        notes_edit.setFixedHeight(100)
        notes[1].addWidget(notes_edit)

        # TODO
        todo_c = self._dash_card(right_layout, "TODO")
        todo_lbl = QLabel("No pending tasks")
        todo_lbl.setObjectName("dashTodo")
        todo_c[1].addWidget(todo_lbl)

        cards_grid.addWidget(right_col, 1)
        scroll.setWidget(cards_widget)
        layout.addWidget(scroll, 1)

    def _dash_card(self, parent_layout: QVBoxLayout, title: str) -> tuple:
        card = QFrame()
        card.setObjectName("dashCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 10, 14, 10)
        card_layout.setSpacing(6)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        t = QLabel(title)
        t.setObjectName("dashCardTitle")
        header_row.addWidget(t)
        header_row.addStretch()
        card_layout.addLayout(header_row)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.addWidget(body)

        parent_layout.addWidget(card)
        return card, body_layout

    def _update_dash_clock(self):
        """Update the dashboard clock label every second."""
        if hasattr(self, "_dash_clock_label"):
            self._dash_clock_label.setText(datetime.now().strftime("%H:%M:%S"))
        if hasattr(self, "_dash_date_label"):
            self._dash_date_label.setText(datetime.now().strftime("%A, %B %d, %Y"))

    def _render_projects(self, page: QWidget):
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        head = QWidget()
        hl = QHBoxLayout(head)
        hl.setContentsMargins(0, 0, 0, 0)
        QLabel("Projects").setObjectName("pageTitle")
        title = QLabel("Projects")
        title.setObjectName("pageTitle")
        hl.addWidget(title)
        hl.addStretch()
        layout.addWidget(head)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setObjectName("projectScroll")
        inner = QLabel("Project list loads here...")
        inner.setObjectName("projectInner")
        scroll.setWidget(inner)
        layout.addWidget(scroll, 1)

    def _render_code(self, page: QWidget):
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # Header
        head = QWidget()
        hl = QHBoxLayout(head)
        hl.setContentsMargins(0, 0, 0, 0)
        title = QLabel("CODE")
        title.setObjectName("pageTitle")
        hl.addWidget(title)

        subtitle = QLabel("Codex · Claude · Cursor · Ollama · OpenRouter")
        subtitle.setObjectName("codeSubtitle")
        hl.addWidget(subtitle)

        hl.addStretch()
        refresh = QPushButton("↻")
        refresh.setObjectName("actionBtn")
        refresh.setCursor(Qt.PointingHandCursor)
        hl.addWidget(refresh)
        layout.addWidget(head)

        # Overview stats
        overview = QWidget()
        ol = QHBoxLayout(overview)
        ol.setContentsMargins(0, 0, 0, 0)
        ol.setSpacing(6)
        self.code_active_label = QLabel("0 active")
        self.code_active_label.setObjectName("codeStatActive")
        ol.addWidget(self.code_active_label)

        self.code_waiting_label = QLabel("0 need you")
        self.code_waiting_label.setObjectName("codeStatWaiting")
        ol.addWidget(self.code_waiting_label)

        self.code_done_label = QLabel("0 finished")
        self.code_done_label.setObjectName("codeStatDone")
        ol.addWidget(self.code_done_label)

        self.code_usage_label = QLabel("28d —")
        self.code_usage_label.setObjectName("codeUsage")
        ol.addWidget(self.code_usage_label)

        ol.addStretch()
        self.code_health_label = QLabel("Checking agents...")
        self.code_health_label.setObjectName("codeHealth")
        ol.addWidget(self.code_health_label)
        layout.addWidget(overview)

        # Split area: sessions list + detail
        splitter = QSplitter(Qt.Horizontal)
        splitter.setObjectName("codeSplitter")

        # Left: sessions list
        left_panel = QFrame()
        left_panel.setObjectName("codeSessionsPanel")
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(4, 4, 4, 4)

        sess_head = QLabel("SESSIONS")
        sess_head.setObjectName("codeSectionHead")
        left_layout.addWidget(sess_head)

        self.code_session_list = QListWidget()
        self.code_session_list.setObjectName("codeSessionList")
        self.code_session_list.setCursor(Qt.PointingHandCursor)
        self.code_session_list.itemClicked.connect(self._on_session_clicked)
        left_layout.addWidget(self.code_session_list, 1)

        splitter.addWidget(left_panel)

        # Right: detail + chat
        right_panel = QWidget()
        right_panel.setObjectName("codeDetailPanel")
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(8, 8, 8, 8)

        self.code_detail_title = QLabel("Select a session")
        self.code_detail_title.setObjectName("codeDetailTitle")
        right_layout.addWidget(self.code_detail_title)

        self.code_detail_meta = QLabel("Current conversation appears here.")
        self.code_detail_meta.setObjectName("codeDetailMeta")
        right_layout.addWidget(self.code_detail_meta)

        # Telemetry bar
        tele = QWidget()
        tele_layout = QHBoxLayout(tele)
        tele_layout.setContentsMargins(0, 0, 0, 0)
        tele_layout.setSpacing(4)
        self.code_telemetry = {}
        for key in ("elapsed", "speed", "tokens", "cost", "files", "diff"):
            lbl = QLabel(f"{key.upper()} —")
            lbl.setObjectName("codeTelemetry")
            self.code_telemetry[key] = lbl
            tele_layout.addWidget(lbl)
        tele_layout.addStretch()

        # Action buttons
        self.code_stop_btn = QPushButton("Stop")
        self.code_stop_btn.setObjectName("codeActionBtn")
        self.code_stop_btn.setCursor(Qt.PointingHandCursor)
        tele_layout.addWidget(self.code_stop_btn)

        self.code_delete_btn = QPushButton("Delete")
        self.code_delete_btn.setObjectName("codeActionBtn")
        self.code_delete_btn.setCursor(Qt.PointingHandCursor)
        tele_layout.addWidget(self.code_delete_btn)

        right_layout.addWidget(tele)

        # Activity scroll
        self.code_activity_scroll = QScrollArea()
        self.code_activity_scroll.setWidgetResizable(True)
        self.code_activity_scroll.setObjectName("codeActivityScroll")
        # Smooth scrolling with wheel
        self.code_activity_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.code_activity_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.code_activity_scroll.setProperty("smoothMode", True)
        self.code_activity_inner = QWidget()
        self.code_activity_layout = QVBoxLayout(self.code_activity_inner)
        self.code_activity_layout.setContentsMargins(4, 4, 4, 4)
        self.code_activity_layout.addStretch()
        self.code_activity_scroll.setWidget(self.code_activity_inner)
        right_layout.addWidget(self.code_activity_scroll, 1)

        splitter.addWidget(right_panel)
        splitter.setSizes([220, 500])
        layout.addWidget(splitter, 1)

        # Composer (starts collapsed, shown on launch)
        self.code_composer = QFrame()
        self.code_composer.setObjectName("codeComposer")
        composer_layout = QVBoxLayout(self.code_composer)
        composer_layout.setContentsMargins(10, 8, 10, 8)

        # Provider row
        prov_row = QHBoxLayout()
        prov_label = QLabel("Agent:")
        prov_label.setObjectName("codeFieldLabel")
        prov_row.addWidget(prov_label)

        self.code_provider_combo = QComboBox()
        self.code_provider_combo.setObjectName("codeCombo")
        self.code_provider_combo.addItems(["Codex", "Claude", "Cursor", "Ollama", "OpenRouter"])
        prov_row.addWidget(self.code_provider_combo)

        model_label = QLabel("Model:")
        model_label.setObjectName("codeFieldLabel")
        prov_row.addWidget(model_label)

        self.code_model_combo = QComboBox()
        self.code_model_combo.setObjectName("codeCombo")
        self.code_model_combo.addItems(["gpt-5.6-sol"])
        prov_row.addWidget(self.code_model_combo)

        int_label = QLabel("Intelligence:")
        int_label.setObjectName("codeFieldLabel")
        prov_row.addWidget(int_label)

        self.code_reasoning_combo = QComboBox()
        self.code_reasoning_combo.setObjectName("codeCombo")
        self.code_reasoning_combo.addItems(["medium"])
        prov_row.addWidget(self.code_reasoning_combo)

        self.code_fast_check = QCheckBox("Fast")
        self.code_fast_check.setObjectName("codeFastCheck")
        prov_row.addWidget(self.code_fast_check)

        prov_row.addStretch()
        composer_layout.addLayout(prov_row)

        # Project row
        proj_row = QHBoxLayout()
        proj_label = QLabel("Project:")
        proj_label.setObjectName("codeFieldLabel")
        proj_row.addWidget(proj_label)

        self.code_project_combo = QComboBox()
        self.code_project_combo.setObjectName("codeCombo")
        self.code_project_combo.setEditable(True)
        proj_row.addWidget(self.code_project_combo, 1)

        browse_btn = QPushButton("+ Add folder")
        browse_btn.setObjectName("codeActionBtn")
        browse_btn.setCursor(Qt.PointingHandCursor)
        proj_row.addWidget(browse_btn)
        composer_layout.addLayout(proj_row)

        # Brief
        brief_label = QLabel("Brief:")
        brief_label.setObjectName("codeFieldLabel")
        composer_layout.addWidget(brief_label)

        self.code_brief_edit = QTextEdit()
        self.code_brief_edit.setObjectName("codeBrief")
        self.code_brief_edit.setPlaceholderText("Describe the task...")
        self.code_brief_edit.setFixedHeight(60)
        composer_layout.addWidget(self.code_brief_edit)

        # Launch
        launch_row = QHBoxLayout()
        launch_row.addStretch()
        attach_btn = QPushButton("Attach")
        attach_btn.setObjectName("codeActionBtn")
        attach_btn.setCursor(Qt.PointingHandCursor)
        launch_row.addWidget(attach_btn)
        launch_btn = QPushButton("Launch")
        launch_btn.setObjectName("codeLaunchBtn")
        launch_btn.setCursor(Qt.PointingHandCursor)
        launch_row.addWidget(launch_btn)
        composer_layout.addLayout(launch_row)

        # Populate sessions
        self._refresh_code_sessions()

    def _refresh_code_sessions(self):
        try:
            from code_jobs import list_jobs
            jobs = list_jobs(limit=50)
            self.code_session_list.clear()
            for job in jobs:
                item = QListWidgetItem(job.get("title", job.get("job_id", "Unknown")))
                item.setData(Qt.UserRole, job.get("job_id", ""))
                self.code_session_list.addItem(item)
        except Exception:
            pass

    def _on_session_clicked(self, item: QListWidgetItem):
        job_id = item.data(Qt.UserRole)
        if job_id:
            self.code_selected_id = job_id
            self.code_detail_title.setText(item.text())
            self.code_detail_meta.setText(f"Job ID: {job_id}")

    def _render_apps(self, page: QWidget):
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        head = QWidget()
        hl = QHBoxLayout(head)
        hl.setContentsMargins(0, 0, 0, 0)
        QLabel("Apps").setObjectName("pageTitle")
        title = QLabel("Apps")
        title.setObjectName("pageTitle")
        hl.addWidget(title)
        hl.addStretch()
        layout.addWidget(head)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QLabel("Installed apps appear here...")
        inner.setObjectName("appInner")
        scroll.setWidget(inner)
        layout.addWidget(scroll, 1)

    def _render_drop(self, page: QWidget):
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Drop")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        zone = QFrame()
        zone.setObjectName("dropZone")
        zone_layout = QVBoxLayout(zone)
        zone_layout.addStretch()
        dz_label = QLabel("Drop files here\nor click to import")
        dz_label.setAlignment(Qt.AlignCenter)
        dz_label.setObjectName("dropZoneLabel")
        zone_layout.addWidget(dz_label)
        zone_layout.addStretch()
        layout.addWidget(zone, 1)

    def _render_operator(self, page: QWidget):
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel("AI Operator")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        info = QLabel("Operator mode — controls, automation, and remote access.")
        info.setObjectName("operatorInfo")
        info.setWordWrap(True)
        layout.addWidget(info)
        layout.addStretch()

    def _render_settings(self, page: QWidget):
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        head = QWidget()
        hl = QHBoxLayout(head)
        hl.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Settings")
        title.setObjectName("pageTitle")
        hl.addWidget(title)
        hl.addStretch()
        layout.addWidget(head)

        # Subpage rail
        pages = ["General", "Appearance", "Voice", "Voice agent", "OPERATOR", "Models", "Macro pad"]
        rail = QWidget()
        rail_layout = QHBoxLayout(rail)
        rail_layout.setContentsMargins(0, 0, 0, 0)
        rail_layout.setSpacing(4)

        self._settings_btns = {}
        for p in pages:
            btn = QPushButton(p)
            btn.setObjectName("settingsRailBtn")
            btn.setCheckable(True)
            btn.setChecked(p == self.settings_page)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, page=p: self._open_settings_page(page))
            rail_layout.addWidget(btn)
            self._settings_btns[p] = btn
        rail_layout.addStretch()
        layout.addWidget(rail)

        self._settings_content = QScrollArea()
        self._settings_content.setWidgetResizable(True)
        self._settings_content.setObjectName("settingsContent")
        content_widget = QWidget()
        self._settings_content_layout = QVBoxLayout(content_widget)
        self._settings_content_layout.setContentsMargins(0, 0, 0, 0)
        self._settings_content.setWidget(content_widget)
        layout.addWidget(self._settings_content, 1)

        self._build_settings_general()

    def _open_settings_page(self, page: str):
        self.settings_page = page
        for p, btn in self._settings_btns.items():
            btn.setChecked(p == page)
        # Clear and rebuild
        while self._settings_content_layout.count():
            item = self._settings_content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if page == "General":
            self._build_settings_general()
        elif page == "Appearance":
            self._build_settings_appearance()
        else:
            placeholder = QLabel(f"{page} settings (coming soon)")
            placeholder.setObjectName("settingsPlaceholder")
            self._settings_content_layout.addWidget(placeholder)
        self._settings_content_layout.addStretch()

    def _build_settings_general(self):
        # Project folder
        card = QFrame()
        card.setObjectName("settingsCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 10, 14, 10)

        t = QLabel("Project Root")
        t.setObjectName("settingsCardTitle")
        card_layout.addWidget(t)
        path_edit = QLineEdit(self.config.get("project_root", ""))
        path_edit.setObjectName("settingsInput")
        card_layout.addWidget(path_edit)
        self._settings_content_layout.addWidget(card)

        # Update card
        card2 = QFrame()
        card2.setObjectName("settingsCard")
        card2_layout = QVBoxLayout(card2)
        card2_layout.setContentsMargins(14, 10, 14, 10)
        t2 = QLabel("Update")
        t2.setObjectName("settingsCardTitle")
        card2_layout.addWidget(t2)
        up_btn = QPushButton("Check for Updates")
        up_btn.setObjectName("actionBtn")
        up_btn.setCursor(Qt.PointingHandCursor)
        card2_layout.addWidget(up_btn)
        self._settings_content_layout.addWidget(card2)

    def _build_settings_appearance(self):
        card = QFrame()
        card.setObjectName("settingsCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 10, 14, 10)

        t = QLabel("Window")
        t.setObjectName("settingsCardTitle")
        card_layout.addWidget(t)

        # Opacity slider
        op_row = QHBoxLayout()
        op_row.addWidget(QLabel("Opacity:"))
        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(50, 100)
        self.opacity_slider.setValue(int(self.config.get("opacity", 0.96) * 100))
        self.opacity_slider.valueChanged.connect(
            lambda v: self.setWindowOpacity(v / 100.0)
        )
        op_row.addWidget(self.opacity_slider)
        op_row.addWidget(QLabel("100%"))
        card_layout.addLayout(op_row)

        self._settings_content_layout.addWidget(card)

    # ── Chat ────────────────────────────────────────────────────────────────
    def _chat_keypress(self, event):
        """Handle Enter to send, Shift+Enter for newline."""
        from PySide6.QtGui import QKeyEvent
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and event.modifiers() == Qt.NoModifier:
            self._send_chat()
        else:
            # Call original keyPressEvent
            QTextEdit.keyPressEvent(self.chat_input, event)

    def _send_chat(self):
        text = self.chat_input.toPlainText().strip()
        if not text:
            return
        self.chat_input.clear()
        self._add_chat_bubble("user", text)

        # Simulate agent response with streaming
        self._stream_chat_response(text)

    def _stream_chat_response(self, user_text: str):
        """Stream a chat response word by word for smooth token effect."""
        words = f"I received your message: \"{user_text}\"".split()
        self._chat_stream_buffer = ""
        self._chat_stream_index = 0
        self._chat_stream_words = words

        # Create streaming bubble
        self._chat_stream_bubble = QLabel("")
        self._chat_stream_bubble.setObjectName("chatBubbleAssistant")
        self._chat_stream_bubble.setWordWrap(True)
        self._chat_stream_bubble.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.chat_msg_layout.insertWidget(self.chat_msg_layout.count() - 1, self._chat_stream_bubble)

        self._chat_stream_timer = QTimer(self)
        self._chat_stream_timer.timeout.connect(self._stream_chat_tick)
        self._chat_stream_timer.start(30)

    def _stream_chat_tick(self):
        """Add one word to the streaming bubble."""
        if self._chat_stream_index < len(self._chat_stream_words):
            if self._chat_stream_buffer:
                self._chat_stream_buffer += " "
            self._chat_stream_buffer += self._chat_stream_words[self._chat_stream_index]
            self._chat_stream_bubble.setText(self._chat_stream_buffer)
            self._chat_stream_index += 1
            # Auto-scroll
            sb = self.chat_scroll.verticalScrollBar()
            sb.setValue(sb.maximum())
        else:
            self._chat_stream_timer.stop()
            self._chat_stream_timer.deleteLater()

    def _add_chat_bubble(self, role: str, text: str):
        bubble = QLabel(text)
        bubble.setObjectName(f"chatBubble{'User' if role == 'user' else 'Assistant'}")
        bubble.setWordWrap(True)
        bubble.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        # Insert before stretch
        self.chat_msg_layout.insertWidget(self.chat_msg_layout.count() - 1, bubble)
        # Auto-scroll
        self.chat_scroll.verticalScrollBar().setValue(
            self.chat_scroll.verticalScrollBar().maximum()
        )

    def _reset_chat(self):
        # Clear all messages
        while self.chat_msg_layout.count() > 1:
            item = self.chat_msg_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._chat_embeds = []

    def load_chat_history(self) -> list:
        try:
            path = PROJECT_ROOT / "chat_history.json"
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return []

    def render_chat_history(self):
        pass  # Placeholder

    # ── Code polling ─────────────────────────────────────────────────────────
    def _start_code_poll(self):
        self._code_poll_timer = QTimer(self)
        self._code_poll_timer.timeout.connect(self._poll_code)
        self._code_poll_timer.start(5000)

    def _poll_code(self):
        try:
            from code_jobs import list_jobs, provider_status
            jobs = list_jobs(limit=50)
            self.code_jobs = jobs
            active = sum(1 for j in jobs if j.get("status") == "active")
            waiting = sum(1 for j in jobs if j.get("status") == "waiting")
            done = sum(1 for j in jobs if j.get("status") in ("done", "error"))
            self.code_active_label.setText(f"{active} active")
            self.code_waiting_label.setText(f"{waiting} need you")
            self.code_done_label.setText(f"{done} finished")
        except Exception:
            pass

    # ── Queue processing ────────────────────────────────────────────────────
    def _process_queue(self):
        while not self._ui_queue.empty():
            try:
                fn = self._ui_queue.get_nowait()
                fn()
            except queue.Empty:
                break

    @Slot()
    def _ui_call(self, fn: Callable):
        self._ui_queue.put(fn)

    # ── Theme ────────────────────────────────────────────────────────────────
    def _apply_theme(self):
        c = self.theme_obj
        border = c.blend(c.c("panel2"), c.c("accent"), 0.18)

        style = f"""
        QWidget#central {{
            background-color: transparent;
        }}
        QFrame#panel {{
            background-color: {c.c("panel")};
            border-radius: 12px;
        }}
        QFrame#header {{
            background-color: {c.c("panel")};
            border-top-left-radius: 12px;
            border-top-right-radius: 12px;
        }}
        QLabel#brandTitle {{
            color: {c.c("text")};
            font-size: 18px;
            font-weight: bold;
        }}
        QLabel#subtitle {{
            color: {c.c("muted")};
            font-size: 9px;
            padding-left: 12px;
        }}
        QPushButton#headerBtn {{
            background: transparent;
            color: {c.c("muted")};
            border: none;
            padding: 4px 8px;
            font-size: 12px;
        }}
        QPushButton#headerBtn:hover {{
            color: {c.c("text")};
            background: {c.c("surface2")};
            border-radius: 4px;
        }}
        QWidget#nav {{
            background-color: {c.c("surface")};
            border-radius: 8px;
        }}
        QPushButton#navBtn {{
            background: transparent;
            color: {c.c("muted")};
            border: none;
            text-align: left;
            padding: 8px 16px;
            font-size: 12px;
            border-radius: 4px;
            margin: 0 10px;
        }}
        QPushButton#navBtn:hover {{
            background: {c.c("surface2")};
            color: {c.c("text")};
        }}
        QPushButton#navBtn:checked {{
            background: {c.c("accent")};
            color: #ffffff;
        }}
        QWidget#page {{
            background-color: {c.c("panel")};
        }}
        QWidget#chatPanel {{
            background-color: {c.c("surface")};
            border-radius: 8px;
        }}
        QFrame#chatResizeHandle {{
            background-color: {c.c("surface")};
        }}
        QLabel#agentLabel {{
            color: {c.c("text")};
            font-size: 10px;
            font-weight: bold;
        }}
        QLabel#chatAccountLabel {{
            color: {c.c("success")};
            font-size: 8px;
        }}
        QPushButton#headerChip {{
            background: transparent;
            color: {c.c("muted")};
            border: 1px solid {c.c("surface2")};
            padding: 2px 8px;
            font-size: 9px;
            border-radius: 4px;
        }}
        QPushButton#headerChip:hover {{
            background: {c.c("surface2")};
            color: {c.c("text")};
        }}
        QScrollArea#chatScroll {{
            background-color: {c.c("surface")};
            border: none;
        }}
        QWidget#chatInner {{
            background-color: {c.c("surface")};
        }}
        QLabel#chatBubbleUser {{
            background-color: {c.blend(c.c("surface"), c.c("accent"), 0.22)};
            color: {c.c("text")};
            padding: 8px 12px;
            border-radius: 8px;
            font-size: 9px;
        }}
        QLabel#chatBubbleAssistant {{
            background-color: {c.blend(c.c("surface"), c.c("text"), 0.08)};
            color: {c.c("text")};
            padding: 8px 12px;
            border-radius: 8px;
            font-size: 9px;
        }}
        QFrame#chatInputWrap {{
            background-color: {c.c("panel2")};
            border: 1px solid {border};
            border-radius: 6px;
        }}
        QTextEdit#chatInput {{
            background: transparent;
            color: {c.c("text")};
            border: none;
            font-size: 9px;
            padding: 8px 10px;
        }}
        QPushButton#sendBtn {{
            background: {c.c("accent")};
            color: #ffffff;
            border: none;
            padding: 6px 16px;
            font-size: 10px;
            font-weight: bold;
            border-radius: 4px;
        }}
        QPushButton#sendBtn:hover {{
            background: {c.blend(c.c("accent"), "#ffffff", 0.15)};
        }}
        QFrame#bottomTray {{
            background-color: {c.c("surface")};
            border-bottom-left-radius: 12px;
            border-bottom-right-radius: 12px;
        }}
        QPushButton#trayHandle {{
            background: transparent;
            color: {c.c("muted")};
            border: none;
            font-size: 14px;
        }}
        QPushButton#trayHandle:hover {{
            color: {c.c("text")};
        }}
        QLabel#trayIcon {{
            color: {c.c("muted")};
            font-size: 12px;
        }}
        QLabel#trayNowPlaying {{
            color: {c.c("muted")};
            font-size: 9px;
        }}
        QLabel#trayClock {{
            color: {c.c("accent")};
            font-size: 11px;
            font-weight: bold;
            padding-right: 8px;
        }}
        QLabel#pageTitle {{
            color: {c.c("text")};
            font-size: 18px;
            font-weight: bold;
        }}
        QPushButton#actionBtn {{
            background: {c.c("surface")};
            color: {c.c("muted")};
            border: none;
            padding: 6px 14px;
            font-size: 10px;
            border-radius: 4px;
        }}
        QPushButton#actionBtn:hover {{
            background: {c.c("surface2")};
            color: {c.c("text")};
        }}
        QFrame#dashCard {{
            background-color: {c.c("surface")};
            border-radius: 8px;
        }}
        QLabel#dashCardTitle {{
            color: {c.c("muted")};
            font-size: 8px;
            font-weight: bold;
        }}
        QLabel#dashHeroText {{
            color: {c.c("text")};
            font-size: 13px;
        }}
        QLabel#dashWeather, QLabel#dashClock, QLabel#dashMarkets,
        QLabel#dashGpu, QLabel#dashTodo {{
            color: {c.c("text")};
            font-size: 11px;
        }}
        QLabel#dashDate {{
            color: {c.c("muted")};
            font-size: 9px;
            padding-top: 2px;
        }}
        QTextEdit#dashNotes {{
            background: {c.c("panel2")};
            color: {c.c("text")};
            border: none;
            border-radius: 4px;
            font-size: 9px;
        }}
        QLabel#codeSubtitle {{
            color: {c.c("muted")};
            font-size: 9px;
            padding-left: 10px;
        }}
        QLabel#codeStatActive {{
            background: {c.c("surface")};
            color: {c.c("success")};
            padding: 6px 10px;
            font-size: 9px;
            font-weight: bold;
            border-radius: 4px;
        }}
        QLabel#codeStatWaiting {{
            background: {c.c("surface")};
            color: #f0c85f;
            padding: 6px 10px;
            font-size: 9px;
            font-weight: bold;
            border-radius: 4px;
        }}
        QLabel#codeStatDone {{
            background: {c.c("surface")};
            color: {c.c("muted")};
            padding: 6px 10px;
            font-size: 9px;
            font-weight: bold;
            border-radius: 4px;
        }}
        QLabel#codeUsage, QLabel#codeHealth {{
            color: {c.c("muted")};
            font-size: 9px;
            font-weight: bold;
            padding: 6px 10px;
        }}
        QSplitter::handle {{
            background: {c.c("panel")};
            width: 5px;
        }}
        QFrame#codeSessionsPanel {{
            background: {c.c("surface")};
            border-radius: 8px;
        }}
        QLabel#codeSectionHead {{
            color: {c.c("muted")};
            font-size: 8px;
            font-weight: bold;
            padding: 8px 9px 4px;
        }}
        QListWidget#codeSessionList {{
            background: transparent;
            color: {c.c("text")};
            border: none;
            font-size: 10px;
            outline: none;
        }}
        QListWidget::item {{
            padding: 6px 10px;
            border-radius: 4px;
        }}
        QListWidget::item:selected {{
            background: {c.c("surface2")};
        }}
        QListWidget::item:hover {{
            background: {c.c("surface2")};
        }}
        QWidget#codeDetailPanel {{
            background: {c.c("surface")};
            border-radius: 8px;
        }}
        QLabel#codeDetailTitle {{
            color: {c.c("text")};
            font-size: 10px;
            font-weight: bold;
        }}
        QLabel#codeDetailMeta {{
            color: {c.c("muted")};
            font-size: 7px;
        }}
        QLabel#codeTelemetry {{
            background: {c.c("panel2")};
            color: {c.c("muted")};
            padding: 4px 7px;
            font-size: 7px;
            font-weight: bold;
            border-radius: 3px;
        }}
        QPushButton#codeActionBtn {{
            background: {c.c("panel2")};
            color: {c.c("muted")};
            border: none;
            padding: 4px 10px;
            font-size: 9px;
            border-radius: 3px;
        }}
        QPushButton#codeActionBtn:hover {{
            background: {c.c("surface2")};
            color: {c.c("text")};
        }}
        QScrollArea#codeActivityScroll {{
            background: transparent;
            border: none;
        }}
        QFrame#codeComposer {{
            background: {c.c("surface")};
            border-radius: 8px;
        }}
        QLabel#codeFieldLabel {{
            color: {c.c("muted")};
            font-size: 8px;
            font-weight: bold;
        }}
        QComboBox#codeCombo {{
            background: {c.c("panel2")};
            color: {c.c("text")};
            border: none;
            padding: 4px 8px;
            font-size: 9px;
            border-radius: 3px;
        }}
        QComboBox#codeCombo::drop-down {{
            border: none;
        }}
        QCheckBox#codeFastCheck {{
            color: {c.c("text")};
            font-size: 8px;
            font-weight: bold;
        }}
        QTextEdit#codeBrief {{
            background: {c.c("panel2")};
            color: {c.c("text")};
            border: none;
            border-radius: 4px;
            font-size: 9px;
            padding: 7px 9px;
        }}
        QPushButton#codeLaunchBtn {{
            background: {c.c("accent")};
            color: #ffffff;
            border: none;
            padding: 6px 16px;
            font-size: 10px;
            font-weight: bold;
            border-radius: 4px;
        }}
        QPushButton#codeLaunchBtn:hover {{
            background: {c.blend(c.c("accent"), "#ffffff", 0.15)};
        }}
        QLabel#operatorInfo {{
            color: {c.c("text")};
            font-size: 11px;
            padding: 12px 0;
        }}
        QFrame#dropZone {{
            background: {c.c("surface")};
            border: 2px dashed {c.c("surface2")};
            border-radius: 12px;
            min-height: 200px;
        }}
        QLabel#dropZoneLabel {{
            color: {c.c("muted")};
            font-size: 14px;
        }}
        QPushButton#settingsRailBtn {{
            background: transparent;
            color: {c.c("muted")};
            border: none;
            padding: 6px 14px;
            font-size: 10px;
            border-radius: 4px;
        }}
        QPushButton#settingsRailBtn:hover {{
            background: {c.c("surface2")};
            color: {c.c("text")};
        }}
        QPushButton#settingsRailBtn:checked {{
            background: {c.c("accent")};
            color: #ffffff;
        }}
        QScrollArea#settingsContent {{
            background: transparent;
            border: none;
        }}
        QFrame#settingsCard {{
            background: {c.c("surface")};
            border-radius: 8px;
        }}
        QLabel#settingsCardTitle {{
            color: {c.c("muted")};
            font-size: 8px;
            font-weight: bold;
        }}
        QLineEdit#settingsInput {{
            background: {c.c("panel2")};
            color: {c.c("text")};
            border: none;
            padding: 6px 10px;
            font-size: 9px;
            border-radius: 4px;
        }}
        QLabel#settingsPlaceholder {{
            color: {c.c("muted")};
            font-size: 11px;
            padding: 20px 0;
        }}
        QSlider::groove:horizontal {{
            background: {c.c("surface2")};
            height: 6px;
            border-radius: 3px;
        }}
        QSlider::handle:horizontal {{
            background: {c.c("accent")};
            width: 14px;
            height: 14px;
            margin: -4px 0;
            border-radius: 7px;
        }}
        /* Scrollbar styling */
        QScrollBar:vertical {{
            background: {c.c("surface")};
            width: 8px;
            margin: 0;
        }}
        QScrollBar::handle:vertical {{
            background: {c.blend(c.c("panel2"), c.c("text"), 0.15)};
            min-height: 30px;
            border-radius: 4px;
        }}
        QScrollBar::handle:vertical:hover {{
            background: {c.blend(c.c("panel2"), c.c("text"), 0.25)};
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            height: 0;
        }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
            background: none;
        }}
        QScrollBar:horizontal {{
            background: {c.c("surface")};
            height: 8px;
            margin: 0;
        }}
        QScrollBar::handle:horizontal {{
            background: {c.blend(c.c("panel2"), c.c("text"), 0.15)};
            min-width: 30px;
            border-radius: 4px;
        }}
        QScrollBar::handle:horizontal:hover {{
            background: {c.blend(c.c("panel2"), c.c("text"), 0.25)};
        }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
            width: 0;
        }}
        QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
            background: none;
        }}
        """
        self.setStyleSheet(style)

    # ── Override resize for grip repositioning ──────────────────────────────
    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_resize_grip"):
            panel = self.centralWidget().findChild(QFrame, "panel")
            if panel:
                self._resize_grip.move(panel.width() - 24, panel.height() - 24)


# ── Entry point ─────────────────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setApplicationName("aiOS")
    app.setOrganizationName("aiOS")

    # Font
    font = QFont("Segoe UI", 9)
    app.setFont(font)

    window = AiosOverlay()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()