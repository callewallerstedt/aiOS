"""Desktop computer-use agent — Tkinter UI.

Run:  python desktop_app.py
"""
from __future__ import annotations

# CRITICAL: enable per-monitor DPI awareness BEFORE Tk / pyautogui /
# mss touch the screen. Without this, on Windows with any monitor at
# >100% scaling, coordinates the model sees (physical pixels from mss)
# don't match the coordinates pyautogui clicks at (logical pixels),
# so clicks land in the wrong place — often visibly nowhere near
# the target on a secondary monitor.
import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import json
import os
import queue
import time
import tkinter as tk
from datetime import datetime
from tkinter import ttk, scrolledtext, messagebox, filedialog

from PIL import Image, ImageDraw, ImageTk, ImageGrab

from agent.config import MODEL as DEFAULT_MODEL
from desktop_agent.screen import list_monitors, Monitor, capture
from desktop_agent.loop import AgentLoop
from desktop_agent.tts import TTSPlayer, DEFAULT_VOICE
from desktop_agent.actions import execute as exec_action


# ----------------------- click overlay (transparent topmost window) -----------------

class ClickOverlay:
    """Brief red ring at (screen_x, screen_y) on the real display."""

    def __init__(self, root: tk.Tk):
        self.root = root

    def flash(self, x: int, y: int, button: str = "left"):
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        try:
            win.attributes("-transparentcolor", "white")
        except tk.TclError:
            pass
        R = 36
        win.geometry(f"{R*2}x{R*2}+{x - R}+{y - R}")
        c = tk.Canvas(win, width=R*2, height=R*2, bg="white", highlightthickness=0)
        c.pack()
        color = {"left": "#ff3030", "right": "#3060ff", "middle": "#30ff60"}.get(button, "#ff3030")
        c.create_oval(4, 4, R*2 - 4, R*2 - 4, outline=color, width=4)
        c.create_oval(R - 4, R - 4, R + 4, R + 4, fill=color, outline=color)
        # fade-out via after
        def step(i=0):
            try:
                win.attributes("-alpha", max(0.0, 1.0 - i * 0.08))
            except tk.TclError:
                return
            if i < 14:
                win.after(50, lambda: step(i + 1))
            else:
                try: win.destroy()
                except tk.TclError: pass
        step()


# ----------------------- main app ----------------------------------------------------

DARK_BG = "#14171d"
DARKER  = "#0f1115"
PANEL   = "#1a1e26"
ACCENT  = "#2c7be5"
GOOD    = "#74e08a"
WARN    = "#f0c14b"
BAD     = "#ff6b6b"
TEXT    = "#e6e6e6"
DIM     = "#8a8f99"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Desktop Agent")
        root.geometry("1320x820")
        root.configure(bg=DARK_BG)

        self.event_q: "queue.Queue[dict]" = queue.Queue()
        self.loop = AgentLoop(self._enqueue)
        self.overlay = ClickOverlay(root)
        self.tts = TTSPlayer(on_log=lambda level, msg: self._enqueue({"type": "log", "msg": msg}))
        self.monitors: list[Monitor] = list_monitors()
        self.current_image: Image.Image | None = None
        self.tk_preview = None
        self.preview_scale = 1.0
        self.preview_origin = (0, 0)
        self.last_clicks: list[tuple[int, int, str]] = []  # in monitor-local pixels
        # Task-input attachments. Each: {"kind": "image"|"text",
        #   "image": PIL.Image|None, "text": str|None, "name": str,
        #   "thumb": PhotoImage (for UI), "chip": tk.Widget (for removal)}
        self.attachments: list[dict] = []
        self.last_debug_dir: str | None = None

        self._build_styles()
        self._build_ui()
        self._tick()

    # -----------------------------------------------------------------------

    def _build_styles(self):
        s = ttk.Style()
        try: s.theme_use("clam")
        except tk.TclError: pass
        s.configure(".", background=DARK_BG, foreground=TEXT, fieldbackground=PANEL,
                    bordercolor="#2a2f3a", lightcolor=PANEL, darkcolor=PANEL,
                    troughcolor=PANEL)
        s.configure("TLabel", background=DARK_BG, foreground=TEXT)
        s.configure("TFrame", background=DARK_BG)
        s.configure("Panel.TFrame", background=PANEL)
        s.configure("TButton", background="#243042", foreground=TEXT, padding=6)
        s.map("TButton", background=[("active", "#2c7be5")])
        s.configure("Run.TButton", background=ACCENT, foreground="white", padding=8)
        s.configure("Stop.TButton", background="#a32a2a", foreground="white", padding=6)
        s.configure("TCombobox", fieldbackground=PANEL, background=PANEL, foreground=TEXT)
        s.configure("TEntry",    fieldbackground=PANEL, foreground=TEXT, insertcolor=TEXT)
        s.configure("Big.TLabel", font=("Segoe UI", 11, "bold"))
        s.configure("Status.TLabel", font=("Segoe UI", 10))

    def _build_ui(self):
        # ---------- top control bar ----------
        top = ttk.Frame(self.root, padding=10)
        top.pack(side="top", fill="x")

        ttk.Label(top, text="Monitor").grid(row=0, column=0, sticky="w", padx=(0, 4))
        self.var_mon = tk.StringVar()
        self.cb_mon = ttk.Combobox(top, textvariable=self.var_mon, width=42,
                                   values=[m.label for m in self.monitors], state="readonly")
        primary_idx = 1 if len(self.monitors) >= 2 else 0
        self.cb_mon.current(primary_idx)
        self.cb_mon.grid(row=0, column=1, padx=4, sticky="w")
        mon_btns = ttk.Frame(top)
        mon_btns.grid(row=0, column=2, padx=4)
        ttk.Button(mon_btns, text="Preview",     command=self._preview_monitor).pack(side="left", padx=2)
        ttk.Button(mon_btns, text="Test cursor", command=self._test_cursor   ).pack(side="left", padx=2)

        ttk.Label(top, text="Model").grid(row=0, column=3, padx=(14, 4))
        self.var_model = tk.StringVar(value=DEFAULT_MODEL)
        self.cb_model = ttk.Combobox(top, textvariable=self.var_model, width=14, values=[
            "gpt-5.5", "gpt-5.5-pro", "gpt-5.2", "gpt-5", "gpt-4o", "gpt-4o-mini"
        ])
        self.cb_model.grid(row=0, column=4)

        ttk.Label(top, text="Reasoning").grid(row=0, column=17, padx=(14, 4))
        self.var_reason = tk.StringVar(value="medium")
        ttk.Combobox(top, textvariable=self.var_reason, width=8, state="readonly",
                     values=["minimal", "low", "medium", "high"]).grid(row=0, column=18)

        ttk.Label(top, text="Max steps").grid(row=0, column=5, padx=(14, 4))
        self.var_steps = tk.IntVar(value=25)
        ttk.Spinbox(top, from_=1, to=200, textvariable=self.var_steps, width=5).grid(row=0, column=6)

        ttk.Label(top, text="Action delay (s)").grid(row=0, column=7, padx=(14, 4))
        self.var_delay = tk.DoubleVar(value=0.20)
        ttk.Spinbox(top, from_=0.0, to=3.0, increment=0.05, textvariable=self.var_delay,
                    width=5, format="%.2f").grid(row=0, column=8)

        ttk.Label(top, text="🔊 TTS").grid(row=0, column=9, padx=(14, 2))
        self.var_tts = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, variable=self.var_tts, command=self._toggle_tts)\
            .grid(row=0, column=10)
        ttk.Label(top, text="voice").grid(row=0, column=11, padx=(8, 2))
        self.var_voice = tk.StringVar(value=DEFAULT_VOICE)
        ttk.Combobox(top, textvariable=self.var_voice, width=9, values=[
            "nova", "coral", "shimmer", "sage", "alloy", "echo", "fable", "onyx",
            "ash", "ballad", "verse",
        ], state="readonly").grid(row=0, column=12)
        self.var_voice.trace_add("write", lambda *_: self.tts.set_voice(self.var_voice.get()))

        ttk.Label(top, text="🖥 Shell").grid(row=0, column=13, padx=(14, 2))
        self.var_shell = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, variable=self.var_shell,
                        command=lambda: self.var_status.set(
                            f"PowerShell {'ENABLED' if self.var_shell.get() else 'disabled'}")
                       ).grid(row=0, column=14)

        from agent.codex_backend import auth_available as _codex_ok
        ttk.Label(top, text="🔑 Codex auth").grid(row=0, column=15, padx=(14, 2))
        self.var_codex = tk.BooleanVar(value=False)
        codex_chk = ttk.Checkbutton(top, variable=self.var_codex,
                                    command=self._toggle_codex)
        codex_chk.grid(row=0, column=16)
        ok, _msg = _codex_ok()
        if not ok:
            codex_chk.state(["disabled"])

        top.columnconfigure(1, weight=1)

        # ---------- task input panel (multi-line + attachments) ----------
        task_panel = tk.Frame(self.root, bg=PANEL, bd=0,
                              highlightbackground="#2a2f3a", highlightthickness=1)
        task_panel.pack(side="top", fill="x", padx=10, pady=(8, 4))

        # header row inside the panel: label + tip + attach + run/pause/stop/clear
        head = tk.Frame(task_panel, bg=PANEL)
        head.pack(side="top", fill="x", padx=8, pady=(6, 0))
        ttk.Label(head, text="Task", style="Big.TLabel", background=PANEL)\
            .pack(side="left")
        ttk.Label(head, text="  (Enter = run · Shift+Enter = newline · "
                              "Ctrl+V pastes images)",
                  background=PANEL, foreground=DIM).pack(side="left")

        self.btn_stop  = ttk.Button(head, text="⏹ Stop",  style="Stop.TButton",
                                    command=self._stop, state="disabled")
        self.btn_pause = ttk.Button(head, text="⏸ Pause", command=self._toggle_pause,
                                    state="disabled")
        self.btn_run   = ttk.Button(head, text="▶ Run",   style="Run.TButton",
                                    command=self._run)
        self.btn_stop.pack(side="right", padx=(4, 0))
        self.btn_pause.pack(side="right", padx=(4, 0))
        self.btn_run.pack(side="right", padx=(4, 0))
        ttk.Button(head, text="Clear log", command=self._clear_log)\
            .pack(side="right", padx=(4, 8))
        self.btn_debug = ttk.Button(head, text="📁 Open last run",
                                    command=self._open_last_debug, state="disabled")
        self.btn_debug.pack(side="right", padx=(4, 0))
        ttk.Button(head, text="📎 Attach", command=self._attach_files)\
            .pack(side="right", padx=(4, 0))

        # multi-line text input
        self.txt_task = tk.Text(task_panel, height=4, wrap="word",
                                bg=DARKER, fg=TEXT, insertbackground=TEXT,
                                borderwidth=0, highlightthickness=0,
                                font=("Segoe UI", 11), padx=8, pady=6,
                                undo=True)
        self.txt_task.pack(side="top", fill="x", padx=8, pady=(6, 4))
        self.txt_task.bind("<Return>",       self._on_task_enter)
        self.txt_task.bind("<Shift-Return>", lambda e: None)  # default: insert newline
        self.txt_task.bind("<Control-Return>", self._on_task_enter)
        self.txt_task.bind("<Control-v>",    self._on_task_paste)
        self.txt_task.bind("<Control-V>",    self._on_task_paste)

        # attachments strip (chips). Hidden until something is attached.
        self.att_strip = tk.Frame(task_panel, bg=PANEL)
        self.att_strip.pack(side="top", fill="x", padx=6, pady=(0, 6))
        self.att_placeholder = tk.Label(self.att_strip,
            text="No attachments. Use 📎 Attach or paste an image (Ctrl+V).",
            bg=PANEL, fg=DIM, font=("Segoe UI", 9))
        self.att_placeholder.pack(side="left", padx=4, pady=4)

        # ---------- main split ----------
        mid = ttk.Frame(self.root)
        mid.pack(side="top", fill="both", expand=True, padx=10, pady=(0, 4))

        # left — preview
        left = ttk.Frame(mid, style="Panel.TFrame")
        left.pack(side="left", fill="both", expand=True, padx=(0, 6))
        ttk.Label(left, text="What the agent sees", style="Big.TLabel",
                  background=PANEL).pack(anchor="w", padx=8, pady=(8, 4))
        self.canvas = tk.Canvas(left, bg=DARKER, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.canvas.bind("<Configure>", lambda e: self._redraw_preview())

        # right — log
        right = ttk.Frame(mid, style="Panel.TFrame", width=560)
        right.pack(side="right", fill="both", padx=(6, 0))
        right.pack_propagate(False)
        ttk.Label(right, text="Activity log", style="Big.TLabel",
                  background=PANEL).pack(anchor="w", padx=8, pady=(8, 4))
        self.log = scrolledtext.ScrolledText(right, bg=DARKER, fg=TEXT, font=("Cascadia Code", 9),
                                             wrap="word", insertbackground=TEXT,
                                             borderwidth=0, highlightthickness=0)
        self.log.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        for tag, color in [("ts", DIM), ("thought", "#9ec3ff"), ("action", "#c8e69a"),
                           ("ok", GOOD), ("err", BAD), ("status", WARN), ("step", "#ffd479"),
                           ("done", GOOD), ("dim", DIM)]:
            self.log.tag_configure(tag, foreground=color)
        self.log.tag_configure("bold", font=("Cascadia Code", 9, "bold"))

        # ---------- status bar ----------
        bot = ttk.Frame(self.root, padding=(10, 6))
        bot.pack(side="bottom", fill="x")
        self.var_status = tk.StringVar(value="Idle.")
        ttk.Label(bot, textvariable=self.var_status, style="Status.TLabel").pack(side="left")
        self.var_step_info = tk.StringVar(value="")
        ttk.Label(bot, textvariable=self.var_step_info, style="Status.TLabel",
                  foreground=DIM).pack(side="right")

    # -----------------------------------------------------------------------

    def _selected_monitor(self) -> Monitor:
        i = self.cb_mon.current()
        if i < 0: i = 0
        return self.monitors[i]

    def _test_cursor(self):
        """Diagnostic: slide cursor to monitor center and draw a small X with
        path. Verifies DPI awareness, monitor offsets, and pen-down behavior
        without needing the model."""
        mon = self._selected_monitor()
        try:
            img = capture(mon)
        except Exception as e:
            messagebox.showerror("Capture failed", str(e))
            return
        self.current_image = img
        self.last_clicks.clear()
        self._redraw_preview()
        cx, cy = mon.width // 2, mon.height // 2
        self._log("step", f"[{_ts()}] === TEST CURSOR on {mon.label} ===\n")
        self._log("dim",  f"  local center=({cx},{cy}) -> virtual=({mon.left+cx},{mon.top+cy})\n")
        def cb(x, y, btn):
            self.overlay.flash(x, y, btn)
            self.last_clicks.append((x - mon.left, y - mon.top, btn))
        # 1) slide to center (no click), then 2) draw an X (path) to verify drag-hold
        import threading
        def worker():
            from desktop_agent.actions import execute as ex
            r1 = ex({"type": "move", "x": cx, "y": cy, "duration": 0.6}, mon, on_click=cb)
            self._enqueue({"type": "log", "msg": f"  move -> {r1.detail} ({r1.elapsed_ms}ms)"})
            self._enqueue({"type": "log",
                "msg": "  (no draw — open Paint and re-run if you want to see strokes)"})
        threading.Thread(target=worker, daemon=True).start()

    def _preview_monitor(self):
        mon = self._selected_monitor()
        try:
            img = capture(mon)
        except Exception as e:
            messagebox.showerror("Capture failed", str(e))
            return
        self.last_clicks.clear()
        self.current_image = img
        self._redraw_preview()
        self._log("ts", f"[{_ts()}] ")
        self._log("dim", f"preview {mon.label}\n")

    # -----------------------------------------------------------------------

    # ----------------------- task input helpers ----------------------------

    def _on_task_enter(self, event):
        """Plain Enter submits; Shift+Enter inserts newline (handled separately)."""
        # If Shift is held, fall through to default newline insertion.
        if event.state & 0x0001:  # Shift mask
            return None
        self._run()
        return "break"

    def _on_task_paste(self, event):
        """Ctrl+V: if clipboard has an image, attach it. Otherwise default text paste."""
        try:
            grabbed = ImageGrab.grabclipboard()
        except Exception:
            grabbed = None
        if isinstance(grabbed, Image.Image):
            ts = datetime.now().strftime("%H%M%S")
            self._add_image_attachment(grabbed.copy(), name=f"pasted_{ts}.png")
            return "break"
        if isinstance(grabbed, list):  # list of file paths on Windows
            any_img = False
            for p in grabbed:
                if isinstance(p, str) and os.path.isfile(p):
                    self._add_file_attachment(p)
                    any_img = True
            if any_img:
                return "break"
        return None  # let Tk paste text normally

    def _attach_files(self):
        paths = filedialog.askopenfilenames(
            title="Attach files",
            filetypes=[
                ("Images", "*.png *.jpg *.jpeg *.gif *.bmp *.webp"),
                ("Text",   "*.txt *.md *.json *.csv *.log *.py *.js *.html *.xml *.yaml *.yml"),
                ("All",    "*.*"),
            ])
        for p in paths:
            self._add_file_attachment(p)

    def _add_file_attachment(self, path: str):
        name = os.path.basename(path)
        ext = os.path.splitext(path)[1].lower()
        img_exts = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
        try:
            if ext in img_exts:
                img = Image.open(path).convert("RGB")
                self._add_image_attachment(img, name=name)
            else:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
                self._add_text_attachment(text, name=name)
        except Exception as e:
            messagebox.showerror("Attach failed", f"{name}: {e}")

    def _add_image_attachment(self, img: Image.Image, name: str):
        att = {"kind": "image", "image": img, "text": None, "name": name}
        self.attachments.append(att)
        self._render_chip(att, is_image=True)

    def _add_text_attachment(self, text: str, name: str):
        att = {"kind": "text", "image": None, "text": text, "name": name}
        self.attachments.append(att)
        self._render_chip(att, is_image=False)

    def _render_chip(self, att: dict, is_image: bool):
        # Hide the placeholder once we have at least one attachment.
        try:
            self.att_placeholder.pack_forget()
        except Exception:
            pass

        chip = tk.Frame(self.att_strip, bg="#243042",
                        highlightbackground="#3a4253", highlightthickness=1)
        chip.pack(side="left", padx=4, pady=4)

        if is_image:
            thumb = att["image"].copy()
            thumb.thumbnail((56, 56), Image.LANCZOS)
            ph = ImageTk.PhotoImage(thumb)
            att["thumb"] = ph  # keep ref alive
            tk.Label(chip, image=ph, bg="#243042", borderwidth=0).pack(side="left", padx=(4, 4), pady=4)
            sub = f"{att['image'].width}×{att['image'].height}"
        else:
            tk.Label(chip, text="📄", bg="#243042", fg=TEXT,
                     font=("Segoe UI", 18)).pack(side="left", padx=(6, 4), pady=4)
            n_lines = (att.get("text") or "").count("\n") + 1
            sub = f"{n_lines} lines · {len(att.get('text') or '')} chars"

        meta = tk.Frame(chip, bg="#243042")
        meta.pack(side="left", padx=(0, 6), pady=4)
        name = att["name"]
        disp_name = name if len(name) <= 28 else name[:25] + "…"
        tk.Label(meta, text=disp_name, bg="#243042", fg=TEXT,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w")
        tk.Label(meta, text=sub, bg="#243042", fg=DIM,
                 font=("Segoe UI", 8)).pack(anchor="w")

        rm = tk.Label(chip, text="✕", bg="#243042", fg="#ff8080",
                      font=("Segoe UI", 11, "bold"), cursor="hand2",
                      padx=8)
        rm.pack(side="right", fill="y")
        rm.bind("<Button-1>", lambda e, a=att, c=chip: self._remove_attachment(a, c))
        rm.bind("<Enter>", lambda e: rm.configure(fg="#ff3030", bg="#2e3a4d"))
        rm.bind("<Leave>", lambda e: rm.configure(fg="#ff8080", bg="#243042"))

        att["chip"] = chip

    def _remove_attachment(self, att: dict, chip: tk.Widget):
        try:
            chip.destroy()
        except Exception:
            pass
        if att in self.attachments:
            self.attachments.remove(att)
        if not self.attachments:
            self.att_placeholder.pack(side="left", padx=4, pady=4)

    def _clear_attachments(self):
        for att in list(self.attachments):
            chip = att.get("chip")
            if chip is not None:
                try: chip.destroy()
                except Exception: pass
        self.attachments.clear()
        self.att_placeholder.pack(side="left", padx=4, pady=4)

    # -----------------------------------------------------------------------

    def _run(self):
        task = self.txt_task.get("1.0", "end").strip()
        if not task:
            messagebox.showwarning("No task", "Type a task first.")
            return
        if self.loop.is_running():
            messagebox.showinfo("Running", "An agent is already running.")
            return
        mon = self._selected_monitor()
        model = self.var_model.get().strip() or DEFAULT_MODEL
        steps = int(self.var_steps.get())
        delay = float(self.var_delay.get())
        self._clear_log()
        self.last_clicks.clear()
        self._log("step", f"[{_ts()}] === START ===\n")
        shell_on = self.var_shell.get()
        self._log("dim", f"task={task!r} monitor={mon.label} model={model} max_steps={steps} "
                          f"shell={'on' if shell_on else 'off'}\n\n")
        self.btn_run.state(["disabled"])
        self.btn_pause.state(["!disabled"])
        self.btn_stop.state(["!disabled"])
        self.var_status.set("Running…")
        backend = "codex" if self.var_codex.get() else "api"
        self._log("dim", f"backend={backend}\n")
        reason = (self.var_reason.get() or "medium").strip().lower() or None
        # Snapshot attachments so user can keep editing the input while running.
        attachments_snap = [
            {"kind": a["kind"], "image": a.get("image"),
             "text": a.get("text"), "name": a.get("name", "?")}
            for a in self.attachments
        ]
        self._log("dim", f"reasoning_effort={reason} attachments={len(attachments_snap)}\n")
        self.loop.start(task, mon, model=model, max_steps=steps, action_delay=delay,
                        shell_enabled=shell_on, backend=backend,
                        reasoning_effort=reason, mid_screenshots="key",
                        attachments=attachments_snap)

    def _stop(self):
        self._log("err", f"[{_ts()}] SAFETY STOP confirmed. Further agent inputs are blocked.\n")
        self.loop.stop()
        self.tts.clear()
        self.var_status.set("Stopping…")

    def _toggle_pause(self):
        if self.loop.is_paused():
            self.loop.resume()
            self.btn_pause.configure(text="⏸ Pause")
            self.var_status.set("Resumed.")
        else:
            self.loop.pause()
            self.btn_pause.configure(text="▶ Resume")
            self.var_status.set("Paused.")

    def _clear_log(self):
        self.log.delete("1.0", "end")

    def _toggle_codex(self):
        on = self.var_codex.get()
        if on:
            from agent.codex_backend import auth_available
            ok, msg = auth_available()
            if not ok:
                self.var_codex.set(False)
                messagebox.showerror("Codex auth", f"Not available: {msg}")
                return
        self.var_status.set(
            f"Backend: {'Codex (ChatGPT subscription)' if on else 'OpenAI API key'}")

    def _toggle_tts(self):
        on = self.var_tts.get()
        self.tts.enable(on)
        self.var_status.set(f"TTS {'on' if on else 'off'} ({self.var_voice.get()})")
        if on:
            self.tts.speak("text to speech is on")

    # ----------------------- event handling --------------------------------

    def _enqueue(self, ev: dict):
        self.event_q.put(ev)

    def _tick(self):
        try:
            while True:
                ev = self.event_q.get_nowait()
                self._handle_event(ev)
        except queue.Empty:
            pass
        self.root.after(40, self._tick)

    def _handle_event(self, ev: dict):
        t = ev.get("type")
        if t == "step_begin":
            self.var_step_info.set(f"step {ev['n']}")
            self._log("step", f"\n[{_ts()}] ── Step {ev['n']} ──\n")
            self.last_clicks.clear()
        elif t == "screenshot":
            self.current_image = ev["image"]
            self._redraw_preview()
        elif t == "thought":
            self._log("ts", f"[{_ts()}] ")
            self._log("thought", "thought: ")
            self._log("thought", (ev["thought"] or "").rstrip() + "\n")
            say = (ev.get("say") or "").strip()
            if say:
                self._log("ts", f"[{_ts()}] ")
                self._log("status", f"🔊 {say}\n")
                if self.tts.is_enabled():
                    self.tts.speak(say)
            if ev.get("message"):
                self._log("ts", f"[{_ts()}] ")
                self._log("status", f"msg: {ev['message']}\n")
            n_actions = len(ev.get("actions") or [])
            self._log("dim", f"            plan: {n_actions} action(s), status={ev.get('status')}, "
                              f"think={ev.get('elapsed_ms')}ms\n")
        elif t == "action_done":
            r = ev["result"]
            tag = "ok" if r["ok"] else "err"
            sym = "✓" if r["ok"] else "✗"
            atype = (r["action"].get("type") or "?")
            self._log("ts", f"[{_ts()}] ")
            self._log(tag, f"{sym} ")
            self._log("action", f"{atype:<12}")
            self._log("dim", f" {r['detail']}  ({r['elapsed_ms']}ms)\n")
            out = r.get("output") or ""
            if out:
                # indent + clip for readability; full text still goes to the model
                clipped = out if len(out) <= 1200 else out[:1200] + "\n…[clipped]"
                for line in clipped.splitlines():
                    self._log("dim", f"            │ {line}\n")
        elif t == "click_fx":
            # red ring at virtual screen coords
            self.overlay.flash(ev["x"], ev["y"], ev.get("button", "left"))
            # store local-screen-space click for the preview overlay
            mon = self._selected_monitor()
            lx, ly = ev["x"] - mon.left, ev["y"] - mon.top
            self.last_clicks.append((lx, ly, ev.get("button", "left")))
            self._redraw_preview()
        elif t == "step_end":
            r = ev["record"]
            self._log("dim", f"            step {r['n']} totals: think {r['think_ms']}ms, "
                              f"act {r['act_ms']}ms, {len(r['results'])} actions\n")
        elif t == "done":
            ok = ev.get("ok")
            tag = "done" if ok else "err"
            self._log("ts", f"\n[{_ts()}] ")
            self._log(tag, f"=== DONE  ok={ok}  steps={ev.get('steps')}  message={ev.get('message','')} ===\n")
            self.var_status.set(f"Done. {ev.get('message','')}")
            if self.tts.is_enabled():
                self.tts.speak("done" if ok else ("stopped" if "stop" in (ev.get('message','').lower()) else "failed"))
            self.btn_run.state(["!disabled"])
            self.btn_pause.state(["disabled"])
            self.btn_stop.state(["disabled"])
            self.btn_pause.configure(text="⏸ Pause")
        elif t == "ask":
            self.var_status.set("Agent is asking: " + ev.get("message", ""))
            self._log("status", f"ASK: {ev.get('message','')}\n")
        elif t == "log":
            self._log("dim", ev["msg"] + "\n")
        elif t == "debug_dir":
            self.last_debug_dir = ev.get("path")
            if self.last_debug_dir:
                try: self.btn_debug.state(["!disabled"])
                except Exception: pass
                self._log("dim", f"debug: {self.last_debug_dir}\n")

    def _open_last_debug(self):
        if not self.last_debug_dir or not os.path.isdir(self.last_debug_dir):
            messagebox.showinfo("No run yet", "Run the agent first — debug folders "
                                              "are created when a run starts.")
            return
        try:
            os.startfile(self.last_debug_dir)  # Windows
        except Exception as e:
            messagebox.showerror("Could not open folder", str(e))

    def _log(self, tag: str, text: str):
        self.log.insert("end", text, tag)
        self.log.see("end")

    # ----------------------- preview rendering -----------------------------

    def _redraw_preview(self):
        if self.current_image is None:
            return
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw < 10 or ch < 10:
            return
        iw, ih = self.current_image.size
        scale = min(cw / iw, ch / ih)
        nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
        img = self.current_image.resize((nw, nh), Image.LANCZOS).copy()
        d = ImageDraw.Draw(img, "RGBA")
        for (lx, ly, b) in self.last_clicks:
            cx, cy = int(lx * scale), int(ly * scale)
            color = {"left": (255, 60, 60, 255), "right": (60, 100, 255, 255),
                     "middle": (60, 255, 100, 255)}.get(b, (255, 60, 60, 255))
            R = 14
            d.ellipse([cx - R, cy - R, cx + R, cy + R], outline=color, width=3)
            d.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=color)
        self.tk_preview = ImageTk.PhotoImage(img)
        self.canvas.delete("all")
        ox, oy = (cw - nw) // 2, (ch - nh) // 2
        self.preview_scale = scale
        self.preview_origin = (ox, oy)
        self.canvas.create_image(ox, oy, anchor="nw", image=self.tk_preview)


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
