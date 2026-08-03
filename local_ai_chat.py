import ast
import base64
import io
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk
from urllib.error import URLError
from urllib.request import Request, urlopen

import mss
from PIL import Image, ImageGrab, ImageTk


APP_TITLE = "Local AI"
API_BASE = "http://localhost:11434"
DEFAULT_MODEL = "qwen3.6-agent:27b"
VISION_MODEL = "qwen2.5vl:7b"
MODEL_ROOT = r"C:\AI\OllamaModels"
KEEP_ALIVE = "1h"
WORKSPACE = Path(__file__).resolve().parent
DATASET_ROOT = WORKSPACE / "training_data" / "gui_clicks"
SYSTEM_PROMPT = (
    "You are a local assistant running on this computer. Be concise, practical, "
    "and precise. Use available tools when they help. Ask before destructive actions."
)

SAFE_TEXT_EXTENSIONS = {
    ".ahk",
    ".bat",
    ".cmd",
    ".css",
    ".csv",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".log",
    ".md",
    ".ps1",
    ".py",
    ".txt",
    ".ts",
    ".tsx",
    ".xml",
    ".yaml",
    ".yml",
}


def request_json(path, payload=None, timeout=60):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(API_BASE + path, data=data, headers=headers)
    with urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def stream_chat(payload):
    req = Request(
        API_BASE + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(req, timeout=900) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if line:
                yield json.loads(line)


def image_to_b64_png(image):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def extract_json_object(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError(f"Model did not return JSON: {text[:300]}")


def think_setting(model, enabled):
    if model.lower().startswith("gpt-oss"):
        return "medium" if enabled else "low"
    return bool(enabled)


def ollama_exe():
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    if local.exists():
        return str(local)
    return "ollama"


def ensure_ollama():
    os.environ.setdefault("OLLAMA_MODELS", MODEL_ROOT)
    try:
        request_json("/api/version", timeout=3)
        return True
    except Exception:
        pass

    creationflags = 0
    if sys.platform.startswith("win"):
        creationflags = subprocess.CREATE_NO_WINDOW
    try:
        subprocess.Popen(
            [ollama_exe(), "serve"],
            env=os.environ.copy(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except Exception:
        return False

    for _ in range(30):
        try:
            request_json("/api/version", timeout=2)
            return True
        except Exception:
            time.sleep(1)
    return False


def resolve_workspace_path(relative_path):
    relative = relative_path or "."
    resolved = (WORKSPACE / relative).resolve()
    if resolved != WORKSPACE and WORKSPACE not in resolved.parents:
        raise ValueError("Path is outside the workspace.")
    return resolved


def list_workspace_files(relative_path=".", max_items=80):
    target = resolve_workspace_path(relative_path)
    if not target.exists():
        return {"error": "Path does not exist."}
    if not target.is_dir():
        return {"error": "Path is not a directory."}

    rows = []
    for item in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if len(rows) >= max(1, min(int(max_items or 80), 200)):
            break
        rel = item.relative_to(WORKSPACE).as_posix()
        rows.append(
            {
                "path": rel,
                "type": "dir" if item.is_dir() else "file",
                "size": None if item.is_dir() else item.stat().st_size,
            }
        )
    return {"workspace": str(WORKSPACE), "items": rows}


def read_text_file(relative_path, max_chars=6000):
    target = resolve_workspace_path(relative_path)
    if not target.exists() or not target.is_file():
        return {"error": "File does not exist."}
    if target.suffix.lower() not in SAFE_TEXT_EXTENSIONS:
        return {"error": "Only text-like workspace files can be read."}
    limit = max(200, min(int(max_chars or 6000), 20000))
    with target.open("r", encoding="utf-8", errors="replace") as handle:
        text = handle.read(limit + 1)
    return {
        "path": target.relative_to(WORKSPACE).as_posix(),
        "content": text[:limit],
        "truncated": len(text) > limit,
    }


ALLOWED_AST_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Call,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
)
ALLOWED_MATH_NAMES = {
    name: getattr(math, name)
    for name in dir(math)
    if not name.startswith("_") and callable(getattr(math, name))
}
ALLOWED_MATH_NAMES.update({"pi": math.pi, "e": math.e, "tau": math.tau, "abs": abs, "round": round})


def calculate_expression(expression):
    tree = ast.parse(expression, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED_AST_NODES):
            raise ValueError(f"Unsupported expression: {type(node).__name__}")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
            raise ValueError("Only numeric constants are allowed.")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in ALLOWED_MATH_NAMES:
                raise ValueError("Only simple math functions are allowed.")
        if isinstance(node, ast.Name) and node.id not in ALLOWED_MATH_NAMES:
            raise ValueError(f"Unknown name: {node.id}")
    return eval(compile(tree, "<calculator>", "eval"), {"__builtins__": {}}, ALLOWED_MATH_NAMES)


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "Get the current local date and time on this computer.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Evaluate a safe math expression.",
            "parameters": {
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_workspace_files",
            "description": "List files and folders under this app workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string"},
                    "max_items": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_text_file",
            "description": "Read a text-like file under this app workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string"},
                    "max_chars": {"type": "integer"},
                },
                "required": ["relative_path"],
            },
        },
    },
]


def run_tool(name, args):
    try:
        if name == "get_current_time":
            result = {"local_time": datetime.now().astimezone().isoformat(timespec="seconds")}
        elif name == "calculate":
            result = {"result": calculate_expression(args.get("expression", ""))}
        elif name == "list_workspace_files":
            result = list_workspace_files(args.get("relative_path", "."), args.get("max_items", 80))
        elif name == "read_text_file":
            result = read_text_file(args.get("relative_path", ""), args.get("max_chars", 6000))
        else:
            result = {"error": f"Unknown tool: {name}"}
    except Exception as exc:
        result = {"error": str(exc)}
    return json.dumps(result, ensure_ascii=False)


class LocalAIApp:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.current_assistant_open = False
        self.assistant_stream_start = None
        self.assistant_placeholder_range = None
        self.assistant_stream_has_content = False
        self.sending = False

        self.model_var = tk.StringVar(value=DEFAULT_MODEL)
        self.eval_model_var = tk.StringVar(value=VISION_MODEL)
        self.eval_monitor_var = tk.StringVar(value="")
        self.gen_monitor_var = tk.StringVar(value="")
        self.gen_suggest_count_var = tk.StringVar(value="4")
        self.gen_pending_suggest_count = 4
        self.eval_mark_mode_var = tk.BooleanVar(value=False)
        self.think_var = tk.BooleanVar(value=False)
        self.tools_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Starting")
        self.eval_image = None
        self.eval_photo = None
        self.eval_scale = 1.0
        self.eval_offset = (0, 0)
        self.eval_user_view = False
        self.eval_pan_start = None
        self.eval_prediction = None
        self.eval_correct_point = None
        self.eval_raw_response = ""
        self.eval_monitors = []
        self.gen_image = None
        self.gen_photo = None
        self.gen_scale = 1.0
        self.gen_offset = (0, 0)
        self.gen_user_view = False
        self.gen_pan_start = None
        self.gen_points = {}
        self.gen_selected_index = 0
        self.gen_suggest_btn = None

        root.title(APP_TITLE)
        root.geometry("980x720")
        root.minsize(760, 540)
        root.configure(bg="#0d0f14")

        self.build_ui()
        self.root.after(100, self.process_events)
        threading.Thread(target=self.startup, daemon=True).start()

    def build_ui(self):
        self.colors = {
            "bg": "#0d0f14",
            "panel": "#141821",
            "panel2": "#10131a",
            "text": "#edf1f7",
            "muted": "#8d96a8",
            "accent": "#6ee7c8",
            "accent2": "#7aa2ff",
            "line": "#242b38",
            "danger": "#ff7b8a",
        }

        top = tk.Frame(self.root, bg=self.colors["bg"])
        top.pack(fill=tk.X, padx=18, pady=(16, 10))

        title = tk.Label(
            top,
            text="Local AI",
            bg=self.colors["bg"],
            fg=self.colors["text"],
            font=("Segoe UI", 22, "bold"),
        )
        title.pack(side=tk.LEFT)

        controls = tk.Frame(top, bg=self.colors["bg"])
        controls.pack(side=tk.RIGHT)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Dark.TCombobox",
            fieldbackground=self.colors["panel"],
            background=self.colors["panel"],
            foreground=self.colors["text"],
            arrowcolor=self.colors["text"],
            bordercolor=self.colors["line"],
            lightcolor=self.colors["line"],
            darkcolor=self.colors["line"],
        )

        self.model_combo = ttk.Combobox(
            controls,
            textvariable=self.model_var,
            values=[DEFAULT_MODEL],
            width=24,
            style="Dark.TCombobox",
            state="normal",
        )
        self.model_combo.pack(side=tk.LEFT, padx=(0, 8), ipady=2)

        self.think_check = self.checkbutton(controls, "Think", self.think_var)
        self.think_check.pack(side=tk.LEFT, padx=4)
        self.tools_check = self.checkbutton(controls, "Tools", self.tools_var)
        self.tools_check.pack(side=tk.LEFT, padx=4)
        self.button(controls, "Refresh", self.refresh_models).pack(side=tk.LEFT, padx=(8, 4))
        self.button(controls, "Clear", self.clear_chat).pack(side=tk.LEFT, padx=4)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 12))

        chat_tab = tk.Frame(self.notebook, bg=self.colors["bg"])
        eval_tab = tk.Frame(self.notebook, bg=self.colors["bg"])
        gen_tab = tk.Frame(self.notebook, bg=self.colors["bg"])
        self.notebook.add(chat_tab, text="Chat")
        self.notebook.add(eval_tab, text="Evaluation")
        self.notebook.add(gen_tab, text="Generate Training Data")

        body = tk.Frame(chat_tab, bg=self.colors["panel"])
        body.pack(fill=tk.BOTH, expand=True, pady=(0, 12))
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)

        self.chat = tk.Text(
            body,
            wrap=tk.WORD,
            bg=self.colors["panel"],
            fg=self.colors["text"],
            insertbackground=self.colors["text"],
            relief=tk.FLAT,
            bd=0,
            padx=18,
            pady=16,
            font=("Segoe UI", 11),
            spacing1=3,
            spacing2=2,
            spacing3=10,
        )
        self.chat.grid(row=0, column=0, sticky="nsew")
        self.chat.configure(state=tk.DISABLED)
        self.chat.tag_configure("user_name", foreground=self.colors["accent"], font=("Segoe UI", 10, "bold"))
        self.chat.tag_configure("assistant_name", foreground=self.colors["accent2"], font=("Segoe UI", 10, "bold"))
        self.chat.tag_configure("tool_name", foreground=self.colors["accent"], font=("Segoe UI", 10, "bold"))
        self.chat.tag_configure("thinking", foreground=self.colors["muted"], font=("Segoe UI", 10, "italic"))
        self.chat.tag_configure("error", foreground=self.colors["danger"])
        self.chat.tag_configure("body", foreground=self.colors["text"])
        self.chat.tag_configure("heading", foreground=self.colors["accent2"], font=("Segoe UI", 12, "bold"), spacing1=5, spacing3=5)
        self.chat.tag_configure("bold", foreground=self.colors["text"], font=("Segoe UI", 11, "bold"))
        self.chat.tag_configure("italic", foreground=self.colors["text"], font=("Segoe UI", 11, "italic"))
        self.chat.tag_configure("inline_code", foreground="#d6e2ff", background="#101827", font=("Consolas", 10))
        self.chat.tag_configure("code", foreground="#d6e2ff", background="#080d14", font=("Consolas", 10))
        self.chat.tag_configure("quote", foreground=self.colors["muted"], font=("Segoe UI", 11, "italic"), lmargin1=24, lmargin2=24)
        self.chat.tag_configure("link", foreground="#8fd3ff", underline=True)

        scrollbar = tk.Scrollbar(body, command=self.chat.yview, bg=self.colors["panel"], troughcolor=self.colors["panel"])
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.chat.configure(yscrollcommand=scrollbar.set)

        bottom = tk.Frame(chat_tab, bg=self.colors["bg"])
        bottom.pack(fill=tk.X, pady=(0, 2))
        bottom.grid_columnconfigure(0, weight=1)

        input_wrap = tk.Frame(bottom, bg=self.colors["panel2"], highlightthickness=1, highlightbackground=self.colors["line"])
        input_wrap.grid(row=0, column=0, sticky="ew")
        input_wrap.grid_columnconfigure(0, weight=1)

        self.prompt = tk.Text(
            input_wrap,
            height=3,
            wrap=tk.WORD,
            bg=self.colors["panel2"],
            fg=self.colors["text"],
            insertbackground=self.colors["text"],
            relief=tk.FLAT,
            bd=0,
            padx=12,
            pady=10,
            font=("Segoe UI", 11),
        )
        self.prompt.grid(row=0, column=0, sticky="ew")
        self.prompt.bind("<Control-Return>", self.send_message)
        self.prompt.bind("<Return>", self.enter_to_send)

        self.send_btn = self.button(bottom, "Send", self.send_message, primary=True)
        self.send_btn.grid(row=0, column=1, sticky="ns", padx=(10, 0), ipadx=14)

        self.build_evaluation_tab(eval_tab)
        self.build_generate_training_tab(gen_tab)

        status = tk.Label(
            self.root,
            textvariable=self.status_var,
            bg=self.colors["bg"],
            fg=self.colors["muted"],
            anchor="w",
            font=("Segoe UI", 9),
        )
        status.pack(fill=tk.X, padx=18, pady=(0, 12))

    def build_evaluation_tab(self, parent):
        parent.grid_rowconfigure(1, weight=1)
        parent.grid_columnconfigure(0, weight=1)

        eval_controls = tk.Frame(parent, bg=self.colors["bg"])
        eval_controls.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        eval_controls.grid_columnconfigure(1, weight=1)

        self.button(eval_controls, "Paste Image", self.eval_paste_image, primary=True).grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        self.button(eval_controls, "Capture Screen", self.eval_capture_screen).grid(
            row=0, column=1, sticky="w", padx=(0, 8)
        )
        self.button(eval_controls, "Reset View", self.reset_eval_view).grid(
            row=0, column=2, sticky="w", padx=(0, 8)
        )

        tk.Label(
            eval_controls,
            text="Screen",
            bg=self.colors["bg"],
            fg=self.colors["muted"],
            font=("Segoe UI", 9, "bold"),
        ).grid(row=0, column=3, sticky="e", padx=(8, 8))
        self.eval_monitor_combo = ttk.Combobox(
            eval_controls,
            textvariable=self.eval_monitor_var,
            values=[],
            width=28,
            style="Dark.TCombobox",
            state="readonly",
        )
        self.eval_monitor_combo.grid(row=0, column=4, sticky="e", padx=(0, 8))
        self.button(eval_controls, "Refresh Screens", self.refresh_eval_monitors).grid(
            row=0, column=5, sticky="e", padx=(0, 8)
        )

        tk.Label(
            eval_controls,
            text="Vision model",
            bg=self.colors["bg"],
            fg=self.colors["muted"],
            font=("Segoe UI", 9, "bold"),
        ).grid(row=0, column=6, sticky="e", padx=(8, 8))
        self.eval_model_combo = ttk.Combobox(
            eval_controls,
            textvariable=self.eval_model_var,
            values=[VISION_MODEL],
            width=22,
            style="Dark.TCombobox",
            state="normal",
        )
        self.eval_model_combo.grid(row=0, column=7, sticky="e")
        self.refresh_eval_monitors()

        workspace = tk.Frame(parent, bg=self.colors["bg"])
        workspace.grid(row=1, column=0, sticky="nsew")
        workspace.grid_rowconfigure(0, weight=1)
        workspace.grid_columnconfigure(0, weight=3)
        workspace.grid_columnconfigure(1, weight=2)

        canvas_wrap = tk.Frame(
            workspace,
            bg=self.colors["panel"],
            highlightthickness=1,
            highlightbackground=self.colors["line"],
        )
        canvas_wrap.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        canvas_wrap.grid_rowconfigure(0, weight=1)
        canvas_wrap.grid_columnconfigure(0, weight=1)

        self.eval_canvas = tk.Canvas(
            canvas_wrap,
            bg=self.colors["panel"],
            highlightthickness=0,
        )
        self.eval_canvas.grid(row=0, column=0, sticky="nsew")
        self.eval_canvas.bind("<Configure>", lambda _event: self.redraw_eval_canvas())
        self.eval_canvas.bind("<MouseWheel>", self.eval_zoom)
        self.eval_canvas.bind("<Button-4>", self.eval_zoom)
        self.eval_canvas.bind("<Button-5>", self.eval_zoom)
        self.eval_canvas.bind("<ButtonPress-1>", self.eval_pan_begin)
        self.eval_canvas.bind("<B1-Motion>", self.eval_pan_move)
        self.eval_canvas.bind("<ButtonPress-3>", self.eval_mark_correct)
        self.eval_canvas.bind("<Double-Button-1>", lambda _event: self.reset_eval_view())

        side = tk.Frame(
            workspace,
            bg=self.colors["panel2"],
            highlightthickness=1,
            highlightbackground=self.colors["line"],
        )
        side.grid(row=0, column=1, sticky="nsew")
        side.grid_columnconfigure(0, weight=1)
        side.grid_rowconfigure(4, weight=1)

        tk.Label(
            side,
            text="Target prompt",
            bg=self.colors["panel2"],
            fg=self.colors["muted"],
            anchor="w",
            font=("Segoe UI", 9, "bold"),
        ).grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 4))
        self.eval_prompt = tk.Text(
            side,
            height=4,
            wrap=tk.WORD,
            bg=self.colors["panel"],
            fg=self.colors["text"],
            insertbackground=self.colors["text"],
            relief=tk.FLAT,
            bd=0,
            padx=10,
            pady=8,
            font=("Segoe UI", 10),
        )
        self.eval_prompt.grid(row=1, column=0, sticky="ew", padx=12)
        self.eval_prompt.insert("1.0", "click the search box")
        self.eval_prompt.bind("<Control-Return>", self.evaluate_image)

        self.eval_run_btn = self.button(side, "Evaluate", self.evaluate_image, primary=True)
        self.eval_run_btn.grid(row=2, column=0, sticky="ew", padx=12, pady=(12, 6))

        label_tools = tk.Frame(side, bg=self.colors["panel2"])
        label_tools.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 8))
        label_tools.grid_columnconfigure(1, weight=1)
        self.checkbutton(label_tools, "Mark Mode", self.eval_mark_mode_var).grid(row=0, column=0, sticky="w")
        self.button(label_tools, "Save Example", self.save_eval_example).grid(row=0, column=1, sticky="e")
        self.button(label_tools, "Clear Label", self.clear_eval_label).grid(row=0, column=2, sticky="e", padx=(8, 0))

        self.eval_output = tk.Text(
            side,
            wrap=tk.WORD,
            bg=self.colors["panel2"],
            fg=self.colors["text"],
            relief=tk.FLAT,
            bd=0,
            padx=12,
            pady=8,
            font=("Consolas", 9),
        )
        self.eval_output.grid(row=4, column=0, sticky="nsew")
        self.eval_output.insert(
            "1.0",
            "Paste an image, describe the target, then Evaluate.\n"
            "The model should return original-image x/y coordinates.\n",
        )
        self.eval_output.configure(state=tk.DISABLED)

    def build_generate_training_tab(self, parent):
        parent.grid_rowconfigure(1, weight=1)
        parent.grid_columnconfigure(0, weight=1)

        controls = tk.Frame(parent, bg=self.colors["bg"])
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        controls.grid_columnconfigure(4, weight=1)

        tk.Label(
            controls,
            text="Screen",
            bg=self.colors["bg"],
            fg=self.colors["muted"],
            font=("Segoe UI", 9, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.gen_monitor_combo = ttk.Combobox(
            controls,
            textvariable=self.gen_monitor_var,
            values=[],
            width=30,
            style="Dark.TCombobox",
            state="readonly",
        )
        self.gen_monitor_combo.grid(row=0, column=1, sticky="w", padx=(0, 8))
        self.button(controls, "Capture", self.gen_capture_screen, primary=True).grid(
            row=0, column=2, sticky="w", padx=(0, 8)
        )
        self.button(controls, "Paste Image", self.gen_paste_image).grid(
            row=0, column=3, sticky="w", padx=(0, 8)
        )
        self.button(controls, "Reset View", self.reset_gen_view).grid(
            row=0, column=5, sticky="e", padx=(8, 0)
        )

        workspace = tk.Frame(parent, bg=self.colors["bg"])
        workspace.grid(row=1, column=0, sticky="nsew")
        workspace.grid_rowconfigure(0, weight=1)
        workspace.grid_columnconfigure(0, weight=3)
        workspace.grid_columnconfigure(1, weight=2)

        canvas_wrap = tk.Frame(
            workspace,
            bg=self.colors["panel"],
            highlightthickness=1,
            highlightbackground=self.colors["line"],
        )
        canvas_wrap.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        canvas_wrap.grid_rowconfigure(0, weight=1)
        canvas_wrap.grid_columnconfigure(0, weight=1)
        self.gen_canvas = tk.Canvas(canvas_wrap, bg=self.colors["panel"], highlightthickness=0)
        self.gen_canvas.grid(row=0, column=0, sticky="nsew")
        self.gen_canvas.bind("<Configure>", lambda _event: self.redraw_gen_canvas())
        self.gen_canvas.bind("<MouseWheel>", self.gen_zoom)
        self.gen_canvas.bind("<Button-4>", self.gen_zoom)
        self.gen_canvas.bind("<Button-5>", self.gen_zoom)
        self.gen_canvas.bind("<ButtonPress-1>", self.gen_mark_or_pan_begin)
        self.gen_canvas.bind("<B1-Motion>", self.gen_pan_move)
        self.gen_canvas.bind("<ButtonPress-3>", self.gen_mark_point)
        self.gen_canvas.bind("<Double-Button-1>", lambda _event: self.reset_gen_view())

        side = tk.Frame(
            workspace,
            bg=self.colors["panel2"],
            highlightthickness=1,
            highlightbackground=self.colors["line"],
        )
        side.grid(row=0, column=1, sticky="nsew")
        side.grid_columnconfigure(0, weight=1)
        side.grid_rowconfigure(2, weight=1)
        side.grid_rowconfigure(5, weight=1)

        tk.Label(
            side,
            text="Prompts, one per line",
            bg=self.colors["panel2"],
            fg=self.colors["muted"],
            anchor="w",
            font=("Segoe UI", 9, "bold"),
        ).grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 4))
        self.gen_prompts = tk.Text(
            side,
            height=7,
            wrap=tk.WORD,
            bg=self.colors["panel"],
            fg=self.colors["text"],
            insertbackground=self.colors["text"],
            relief=tk.FLAT,
            bd=0,
            padx=10,
            pady=8,
            font=("Segoe UI", 10),
        )
        self.gen_prompts.grid(row=1, column=0, sticky="ew", padx=12)
        self.gen_prompts.insert("1.0", "")

        self.gen_prompt_list = tk.Listbox(
            side,
            bg=self.colors["panel"],
            fg=self.colors["text"],
            selectbackground=self.colors["accent2"],
            selectforeground="#06110e",
            relief=tk.FLAT,
            bd=0,
            font=("Segoe UI", 10),
            height=7,
        )
        self.gen_prompt_list.grid(row=2, column=0, sticky="nsew", padx=12, pady=(8, 8))
        self.gen_prompt_list.bind("<<ListboxSelect>>", self.gen_select_prompt)

        row = tk.Frame(side, bg=self.colors["panel2"])
        row.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 8))
        row.grid_columnconfigure(4, weight=1)
        self.button(row, "Load Prompt List", self.gen_load_prompts).grid(row=0, column=0, sticky="w", padx=(0, 8))
        tk.Label(
            row,
            text="Count",
            bg=self.colors["panel2"],
            fg=self.colors["muted"],
            font=("Segoe UI", 9, "bold"),
        ).grid(row=0, column=1, sticky="w", padx=(0, 4))
        tk.Entry(
            row,
            textvariable=self.gen_suggest_count_var,
            width=6,
            bg=self.colors["panel"],
            fg=self.colors["text"],
            insertbackground=self.colors["text"],
            relief=tk.FLAT,
        ).grid(row=0, column=2, sticky="w", padx=(0, 8))
        self.gen_suggest_btn = self.button(row, "Suggest More with GPT", self.gen_suggest_with_gpt)
        self.gen_suggest_btn.grid(row=0, column=3, sticky="w", padx=(0, 8))
        self.button(row, "Clear Point", self.gen_clear_point).grid(row=0, column=5, sticky="e")

        self.button(side, "Save Labeled Batch", self.save_gen_batch, primary=True).grid(
            row=4, column=0, sticky="ew", padx=12, pady=(0, 8)
        )

        self.gen_output = tk.Text(
            side,
            wrap=tk.WORD,
            bg=self.colors["panel2"],
            fg=self.colors["text"],
            relief=tk.FLAT,
            bd=0,
            padx=12,
            pady=8,
            font=("Consolas", 9),
        )
        self.gen_output.grid(row=5, column=0, sticky="nsew")
        self.gen_output.insert(
            "1.0",
            "Load prompts, select one, then right-click the correct point on the image.\n"
            "Each saved point becomes one training example.\n",
        )
        self.gen_output.configure(state=tk.DISABLED)
        self.gen_load_prompts()
        self.refresh_eval_monitors()

    def button(self, parent, text, command, primary=False):
        bg = self.colors["accent"] if primary else self.colors["panel"]
        fg = "#06110e" if primary else self.colors["text"]
        active = "#8ff0d7" if primary else "#1d2330"
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=active,
            activeforeground=fg,
            relief=tk.FLAT,
            bd=0,
            padx=12,
            pady=7,
            font=("Segoe UI", 10, "bold"),
            cursor="hand2",
        )

    def checkbutton(self, parent, text, variable):
        return tk.Checkbutton(
            parent,
            text=text,
            variable=variable,
            bg=self.colors["bg"],
            fg=self.colors["text"],
            activebackground=self.colors["bg"],
            activeforeground=self.colors["text"],
            selectcolor=self.colors["panel"],
            relief=tk.FLAT,
            bd=0,
            font=("Segoe UI", 10),
            cursor="hand2",
        )

    def set_eval_image(self, image, source="image"):
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGB")
        self.eval_image = image.copy()
        self.eval_prediction = None
        self.eval_correct_point = None
        self.eval_raw_response = ""
        self.eval_user_view = False
        self.eval_fit_image_to_canvas()
        self.eval_write(
            f"Loaded {source}: {self.eval_image.width} x {self.eval_image.height} px\n"
        )
        self.redraw_eval_canvas()

    def reset_eval_view(self):
        self.eval_user_view = False
        self.eval_fit_image_to_canvas()
        self.redraw_eval_canvas()

    def eval_fit_image_to_canvas(self):
        if self.eval_image is None or not hasattr(self, "eval_canvas"):
            return
        width = max(1, self.eval_canvas.winfo_width())
        height = max(1, self.eval_canvas.winfo_height())
        img_w, img_h = self.eval_image.size
        scale = min(width / img_w, height / img_h, 1.0)
        display_w = max(1, int(img_w * scale))
        display_h = max(1, int(img_h * scale))
        self.eval_scale = scale
        self.eval_offset = ((width - display_w) // 2, (height - display_h) // 2)

    def eval_zoom(self, event):
        if self.eval_image is None:
            return "break"
        old_scale = self.eval_scale
        if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0:
            factor = 1.18
        else:
            factor = 1 / 1.18
        new_scale = max(0.08, min(8.0, old_scale * factor))
        if abs(new_scale - old_scale) < 0.0001:
            return "break"

        offset_x, offset_y = self.eval_offset
        image_x = (event.x - offset_x) / old_scale
        image_y = (event.y - offset_y) / old_scale
        self.eval_scale = new_scale
        self.eval_offset = (
            int(event.x - image_x * new_scale),
            int(event.y - image_y * new_scale),
        )
        self.eval_user_view = True
        self.redraw_eval_canvas()
        return "break"

    def eval_pan_begin(self, event):
        if self.eval_mark_mode_var.get():
            return self.eval_mark_correct(event)
        self.eval_pan_start = (event.x, event.y, self.eval_offset[0], self.eval_offset[1])
        return "break"

    def eval_pan_move(self, event):
        if self.eval_image is None or not self.eval_pan_start:
            return "break"
        start_x, start_y, offset_x, offset_y = self.eval_pan_start
        self.eval_offset = (offset_x + event.x - start_x, offset_y + event.y - start_y)
        self.eval_user_view = True
        self.redraw_eval_canvas()
        return "break"

    def canvas_to_image_point(self, event):
        if self.eval_image is None:
            return None
        offset_x, offset_y = self.eval_offset
        if self.eval_scale <= 0:
            return None
        x = (event.x - offset_x) / self.eval_scale
        y = (event.y - offset_y) / self.eval_scale
        width, height = self.eval_image.size
        if x < 0 or y < 0 or x >= width or y >= height:
            return None
        return int(round(x)), int(round(y))

    def eval_mark_correct(self, event):
        point = self.canvas_to_image_point(event)
        if point is None:
            self.eval_write("Correct point ignored: click inside the image.\n")
            return "break"
        self.eval_correct_point = {"x": point[0], "y": point[1]}
        self.eval_write(f"Correct point: ({point[0]}, {point[1]})\n")
        self.redraw_eval_canvas()
        return "break"

    def clear_eval_label(self):
        self.eval_correct_point = None
        self.redraw_eval_canvas()
        self.eval_write("Correct point cleared.\n")

    def save_eval_example(self):
        if self.eval_image is None:
            self.eval_write("Save failed: load an image first.\n")
            return
        prompt = self.eval_prompt.get("1.0", tk.END).strip()
        if not prompt:
            self.eval_write("Save failed: enter a target prompt first.\n")
            return
        if not self.eval_correct_point:
            self.eval_write("Save failed: right-click the correct target first.\n")
            return

        DATASET_ROOT.mkdir(parents=True, exist_ok=True)
        screenshots_dir = DATASET_ROOT / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)

        sample_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        image_name = f"{sample_id}.png"
        image_path = screenshots_dir / image_name
        self.eval_image.convert("RGB").save(image_path, format="PNG", optimize=True)

        width, height = self.eval_image.size
        prediction = self.eval_prediction or None
        error_px = None
        if prediction:
            dx = prediction["x"] - self.eval_correct_point["x"]
            dy = prediction["y"] - self.eval_correct_point["y"]
            error_px = round(math.sqrt(dx * dx + dy * dy), 2)

        row = {
            "schema": "aios.gui_click.v1",
            "id": sample_id,
            "image": str(image_path.relative_to(DATASET_ROOT)).replace("\\", "/"),
            "prompt": prompt,
            "action": {
                "type": "click",
                "x": self.eval_correct_point["x"],
                "y": self.eval_correct_point["y"],
                "button": "left",
                "double": False,
                "clicks": 1,
            },
            "metadata": {
                "width": width,
                "height": height,
                "source": "human_corrected",
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            },
            "prediction": prediction,
            "prediction_error_px": error_px,
            "raw_model_response": self.eval_raw_response,
        }

        dataset_path = DATASET_ROOT / "dataset.jsonl"
        with dataset_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

        message = f"Saved example {sample_id}"
        if error_px is not None:
            message += f" | model error {error_px}px"
        self.eval_write(message + f"\nDataset: {dataset_path}\n")

    def eval_paste_image(self):
        try:
            grabbed = ImageGrab.grabclipboard()
        except Exception as exc:
            self.eval_write(f"Clipboard read failed: {exc}\n")
            return

        if isinstance(grabbed, Image.Image):
            self.set_eval_image(grabbed, "clipboard image")
            return
        if isinstance(grabbed, list) and grabbed:
            for item in grabbed:
                path = Path(item)
                if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp", ".webp"}:
                    try:
                        with Image.open(path) as image:
                            self.set_eval_image(image, path.name)
                        return
                    except Exception:
                        continue
        self.eval_write("Clipboard does not contain an image or image file.\n")

    def refresh_eval_monitors(self):
        monitors = []
        try:
            with mss.mss() as sct:
                for i, monitor in enumerate(sct.monitors):
                    left = int(monitor["left"])
                    top = int(monitor["top"])
                    width = int(monitor["width"])
                    height = int(monitor["height"])
                    primary = bool(monitor.get("is_primary"))
                    label = ("All screens" if i == 0 else f"Monitor {i}") + f": {width}x{height} @ {left},{top}"
                    if primary:
                        label += " primary"
                    monitors.append(
                        {
                            "label": label,
                            "mss_index": i,
                            "region": {"left": left, "top": top, "width": width, "height": height},
                            "width": width,
                            "height": height,
                            "primary": primary,
                        }
                    )
        except Exception as exc:
            self.eval_write(f"Monitor refresh failed: {exc}\n")

        if not monitors:
            try:
                img = ImageGrab.grab(all_screens=True)
                monitors.append(
                    {
                        "label": f"1: {img.width}x{img.height} all screens",
                        "region": None,
                        "width": img.width,
                        "height": img.height,
                        "primary": True,
                    }
                )
            except Exception:
                pass

        self.eval_monitors = monitors
        labels = [m["label"] for m in monitors]
        if hasattr(self, "eval_monitor_combo"):
            self.eval_monitor_combo.configure(values=labels)
        if hasattr(self, "gen_monitor_combo"):
            self.gen_monitor_combo.configure(values=labels)
        if labels and self.eval_monitor_var.get() not in labels:
            primary = next((m["label"] for m in monitors if m.get("primary")), labels[0])
            self.eval_monitor_var.set(primary)
        if labels and self.gen_monitor_var.get() not in labels:
            primary = next((m["label"] for m in monitors if m.get("primary")), labels[0])
            self.gen_monitor_var.set(primary)

    def eval_capture_screen(self):
        try:
            selected = self.eval_monitor_var.get()
            monitor = next((m for m in self.eval_monitors if m["label"] == selected), None)
            if monitor is None:
                self.refresh_eval_monitors()
                monitor = next((m for m in self.eval_monitors if m["label"] == self.eval_monitor_var.get()), None)
            if monitor and monitor.get("region"):
                with mss.mss() as sct:
                    raw = sct.grab(monitor["region"])
                    image = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
                self.set_eval_image(image, f"screen capture {monitor['label']}")
            else:
                self.set_eval_image(ImageGrab.grab(all_screens=True), "screen capture all screens")
        except Exception as exc:
            self.eval_write(f"Screen capture failed: {exc}\n")

    def eval_write(self, text):
        self.eval_output.configure(state=tk.NORMAL)
        self.eval_output.insert(tk.END, text)
        self.eval_output.see(tk.END)
        self.eval_output.configure(state=tk.DISABLED)

    def redraw_eval_canvas(self):
        self.eval_canvas.delete("all")
        width = max(1, self.eval_canvas.winfo_width())
        height = max(1, self.eval_canvas.winfo_height())
        if self.eval_image is None:
            self.eval_canvas.create_text(
                width // 2,
                height // 2,
                text="Paste an image or capture the screen",
                fill=self.colors["muted"],
                font=("Segoe UI", 13, "bold"),
            )
            return

        img_w, img_h = self.eval_image.size
        if not self.eval_user_view:
            self.eval_fit_image_to_canvas()
        scale = self.eval_scale
        display_w = max(1, int(img_w * scale))
        display_h = max(1, int(img_h * scale))
        offset_x, offset_y = self.eval_offset

        resampling = getattr(Image, "Resampling", Image).LANCZOS
        display = self.eval_image.resize((display_w, display_h), resampling)
        self.eval_photo = ImageTk.PhotoImage(display)
        self.eval_canvas.create_image(offset_x, offset_y, anchor="nw", image=self.eval_photo)

        if self.eval_prediction:
            x = self.eval_prediction["x"] * scale + offset_x
            y = self.eval_prediction["y"] * scale + offset_y
            color = "#ff4d6d"
            radius = 9
            self.eval_canvas.create_oval(
                x - radius,
                y - radius,
                x + radius,
                y + radius,
                outline=color,
                width=3,
            )
            self.eval_canvas.create_line(x - 16, y, x + 16, y, fill=color, width=2)
            self.eval_canvas.create_line(x, y - 16, x, y + 16, fill=color, width=2)
            label = f"{int(self.eval_prediction['x'])}, {int(self.eval_prediction['y'])}"
            self.eval_canvas.create_text(
                x + 12,
                y - 18,
                text=label,
                fill=color,
                anchor="w",
                font=("Segoe UI", 10, "bold"),
            )

        if self.eval_correct_point:
            x = self.eval_correct_point["x"] * scale + offset_x
            y = self.eval_correct_point["y"] * scale + offset_y
            color = "#39e58c"
            radius = 10
            self.eval_canvas.create_oval(
                x - radius,
                y - radius,
                x + radius,
                y + radius,
                outline=color,
                width=3,
            )
            self.eval_canvas.create_line(x - 18, y, x + 18, y, fill=color, width=2)
            self.eval_canvas.create_line(x, y - 18, x, y + 18, fill=color, width=2)
            label = f"correct {int(self.eval_correct_point['x'])}, {int(self.eval_correct_point['y'])}"
            self.eval_canvas.create_text(
                x + 12,
                y + 18,
                text=label,
                fill=color,
                anchor="w",
                font=("Segoe UI", 10, "bold"),
            )

    def evaluate_image(self, event=None):
        if self.eval_image is None:
            self.eval_write("Load an image first.\n")
            return "break"
        prompt = self.eval_prompt.get("1.0", tk.END).strip()
        if not prompt:
            self.eval_write("Enter a target prompt first.\n")
            return "break"
        model = self.eval_model_var.get().strip() or VISION_MODEL
        image = self.eval_image.copy()
        self.eval_run_btn.configure(state=tk.DISABLED)
        self.status_var.set("Evaluating image")
        self.eval_write(f"\nEvaluating with {model}: {prompt}\n")
        threading.Thread(
            target=self.evaluate_image_worker,
            args=(image, prompt, model),
            daemon=True,
        ).start()
        return "break"

    def evaluate_image_worker(self, image, prompt, model):
        raw = ""
        try:
            width, height = image.size
            instruction = (
                "You are a desktop computer-use agent in evaluation mode.\n\n"
                "You drive a real mouse and keyboard on the user's monitor. Each "
                "step you receive the user's high-level TASK and the current "
                "SCREENSHOT.\n\n"
                "Reply with strict JSON only, no markdown:\n"
                "{\n"
                '  "thought": "what you see and why this point is right",\n'
                '  "status": "continue",\n'
                '  "say": "",\n'
                '  "message": "one-line status",\n'
                '  "actions": [\n'
                '    {"type":"click","x":<int>,"y":<int>,"button":"left",'
                '"double":false,"clicks":1}\n'
                "  ]\n"
                "}\n\n"
                "Coordinates: the screenshot shown to you is MONITOR-LOCAL pixels. "
                "Top-left = (0,0). All x,y you output must be inside "
                f"[0..{width - 1}] x [0..{height - 1}]. Use the center of the "
                "clickable target, not a bounding-box corner.\n\n"
                f"STEP 1/1\nTASK: {prompt}\n"
                f"Monitor screenshot is {width}x{height} px (top-left = 0,0). "
                "Output coordinates in this space.\nNow think and reply with JSON."
            )
            payload = {
                "model": model,
                "prompt": instruction,
                "images": [image_to_b64_png(image)],
                "stream": False,
                "options": {"temperature": 0.0, "num_ctx": 4096},
            }
            response = request_json("/api/generate", payload, timeout=900)
            raw = response.get("response", "")
            data = extract_json_object(raw)
            prediction = self.normalize_prediction(data, width, height)
            self.events.put(("eval_result", prediction, raw))
        except Exception as exc:
            self.events.put(("eval_error", str(exc), raw))

    def normalize_prediction(self, data, width, height):
        if not isinstance(data, dict):
            raise ValueError("Prediction JSON must be an object.")

        source = data
        actions = data.get("actions")
        if isinstance(actions, list):
            click_action = self.first_click_action(actions)
            if click_action:
                source = click_action

        if "x" in source and "y" in source:
            x, y = self.coerce_xy(source["x"], source["y"])
        elif "x" in source and isinstance(source["x"], list):
            x, y = self.coerce_point(source["x"])
        elif "point" in source:
            x, y = self.coerce_point(source["point"])
        elif "coordinate" in source:
            x, y = self.coerce_point(source["coordinate"])
        elif "coordinates" in source:
            x, y = self.coerce_point(source["coordinates"])
        elif "position" in source:
            x, y = self.coerce_point(source["position"])
        elif "click" in source:
            x, y = self.coerce_point(source["click"])
        elif "bbox" in source and isinstance(source["bbox"], list) and len(source["bbox"]) >= 4:
            x = (float(source["bbox"][0]) + float(source["bbox"][2])) / 2
            y = (float(source["bbox"][1]) + float(source["bbox"][3])) / 2
        else:
            raise ValueError(f"Prediction JSON has no x/y point: {data}")

        x = max(0, min(width - 1, int(round(float(x)))))
        y = max(0, min(height - 1, int(round(float(y)))))
        return {
            "x": x,
            "y": y,
            "confidence": source.get("confidence", data.get("confidence")),
            "description": source.get("description", data.get("message", "")),
            "action": source,
        }

    def first_click_action(self, actions):
        for action in actions:
            if not isinstance(action, dict):
                continue
            action_type = str(action.get("type", "")).lower()
            if action_type in {"click", "left_click", "right_click", "double_click", "move"}:
                return action
        return None

    def coerce_xy(self, x, y):
        if isinstance(x, list) and not isinstance(y, list):
            return self.coerce_point(x)
        if isinstance(y, list) and not isinstance(x, list):
            return self.coerce_point(y)
        if isinstance(x, list) and isinstance(y, list):
            if len(x) >= 2:
                return self.coerce_point(x)
            if len(y) >= 2:
                return self.coerce_point(y)
        return x, y

    def coerce_point(self, point):
        if isinstance(point, dict):
            if "x" in point and "y" in point:
                return point["x"], point["y"]
            if "point" in point:
                return self.coerce_point(point["point"])
        if isinstance(point, list):
            flat = self.flatten_numbers(point)
            if len(flat) >= 2:
                return flat[0], flat[1]
        raise ValueError(f"Could not read point from {point!r}")

    def flatten_numbers(self, value):
        out = []
        if isinstance(value, (int, float, str)):
            try:
                out.append(float(value))
            except ValueError:
                pass
        elif isinstance(value, list):
            for item in value:
                out.extend(self.flatten_numbers(item))
        return out

    def gen_write(self, text):
        self.gen_output.configure(state=tk.NORMAL)
        self.gen_output.insert(tk.END, text)
        self.gen_output.see(tk.END)
        self.gen_output.configure(state=tk.DISABLED)

    def gen_prompt_lines(self):
        return [line.strip() for line in self.gen_prompts.get("1.0", tk.END).splitlines() if line.strip()]

    def gen_load_prompts(self):
        prompts = self.gen_prompt_lines()
        self.gen_prompt_list.delete(0, tk.END)
        for i, prompt in enumerate(prompts, 1):
            point = self.gen_points.get(i - 1)
            prefix = "[x]" if point else "[ ]"
            self.gen_prompt_list.insert(tk.END, f"{prefix} {i}. {prompt}")
        if prompts:
            index = min(self.gen_selected_index, len(prompts) - 1)
            self.gen_prompt_list.selection_clear(0, tk.END)
            self.gen_prompt_list.selection_set(index)
            self.gen_prompt_list.activate(index)
            self.gen_selected_index = index
        self.redraw_gen_canvas()

    def gen_select_prompt(self, _event=None):
        selection = self.gen_prompt_list.curselection()
        if selection:
            self.gen_selected_index = int(selection[0])
            self.redraw_gen_canvas()

    def set_gen_image(self, image, source="image"):
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGB")
        self.gen_image = image.copy()
        self.gen_points = {}
        self.gen_selected_index = 0
        self.gen_prompts.delete("1.0", tk.END)
        self.gen_prompt_list.delete(0, tk.END)
        self.gen_user_view = False
        self.gen_fit_image_to_canvas()
        self.gen_write(f"Loaded {source}: {self.gen_image.width} x {self.gen_image.height} px\n")
        self.gen_load_prompts()
        self.redraw_gen_canvas()

    def gen_paste_image(self):
        try:
            grabbed = ImageGrab.grabclipboard()
        except Exception as exc:
            self.gen_write(f"Clipboard read failed: {exc}\n")
            return
        if isinstance(grabbed, Image.Image):
            self.set_gen_image(grabbed, "clipboard image")
            return
        if isinstance(grabbed, list) and grabbed:
            for item in grabbed:
                path = Path(item)
                if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp", ".webp"}:
                    try:
                        with Image.open(path) as image:
                            self.set_gen_image(image, path.name)
                        return
                    except Exception:
                        continue
        self.gen_write("Clipboard does not contain an image or image file.\n")

    def gen_capture_screen(self):
        try:
            selected = self.gen_monitor_var.get()
            monitor = next((m for m in self.eval_monitors if m["label"] == selected), None)
            if monitor is None:
                self.refresh_eval_monitors()
                monitor = next((m for m in self.eval_monitors if m["label"] == self.gen_monitor_var.get()), None)
            if monitor and monitor.get("region"):
                with mss.mss() as sct:
                    raw = sct.grab(monitor["region"])
                    image = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
                self.set_gen_image(image, f"screen capture {monitor['label']}")
            else:
                self.set_gen_image(ImageGrab.grab(all_screens=True), "screen capture all screens")
        except Exception as exc:
            self.gen_write(f"Screen capture failed: {exc}\n")

    def reset_gen_view(self):
        self.gen_user_view = False
        self.gen_fit_image_to_canvas()
        self.redraw_gen_canvas()

    def gen_fit_image_to_canvas(self):
        if self.gen_image is None or not hasattr(self, "gen_canvas"):
            return
        width = max(1, self.gen_canvas.winfo_width())
        height = max(1, self.gen_canvas.winfo_height())
        img_w, img_h = self.gen_image.size
        scale = min(width / img_w, height / img_h, 1.0)
        display_w = max(1, int(img_w * scale))
        display_h = max(1, int(img_h * scale))
        self.gen_scale = scale
        self.gen_offset = ((width - display_w) // 2, (height - display_h) // 2)

    def gen_zoom(self, event):
        if self.gen_image is None:
            return "break"
        old_scale = self.gen_scale
        factor = 1.18 if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0 else 1 / 1.18
        new_scale = max(0.08, min(8.0, old_scale * factor))
        offset_x, offset_y = self.gen_offset
        image_x = (event.x - offset_x) / old_scale
        image_y = (event.y - offset_y) / old_scale
        self.gen_scale = new_scale
        self.gen_offset = (int(event.x - image_x * new_scale), int(event.y - image_y * new_scale))
        self.gen_user_view = True
        self.redraw_gen_canvas()
        return "break"

    def gen_mark_or_pan_begin(self, event):
        if event.state & 0x0004:
            return self.gen_mark_point(event)
        self.gen_pan_start = (event.x, event.y, self.gen_offset[0], self.gen_offset[1])
        return "break"

    def gen_pan_move(self, event):
        if self.gen_image is None or not self.gen_pan_start:
            return "break"
        start_x, start_y, offset_x, offset_y = self.gen_pan_start
        self.gen_offset = (offset_x + event.x - start_x, offset_y + event.y - start_y)
        self.gen_user_view = True
        self.redraw_gen_canvas()
        return "break"

    def gen_canvas_to_image_point(self, event):
        if self.gen_image is None or self.gen_scale <= 0:
            return None
        offset_x, offset_y = self.gen_offset
        x = (event.x - offset_x) / self.gen_scale
        y = (event.y - offset_y) / self.gen_scale
        width, height = self.gen_image.size
        if x < 0 or y < 0 or x >= width or y >= height:
            return None
        return int(round(x)), int(round(y))

    def gen_mark_point(self, event):
        prompts = self.gen_prompt_lines()
        if not prompts:
            self.gen_write("Load prompts first.\n")
            return "break"
        point = self.gen_canvas_to_image_point(event)
        if point is None:
            self.gen_write("Point ignored: click inside the image.\n")
            return "break"
        index = min(self.gen_selected_index, len(prompts) - 1)
        self.gen_points[index] = {"x": point[0], "y": point[1]}
        self.gen_write(f"Prompt {index + 1} point: ({point[0]}, {point[1]})\n")
        if index + 1 < len(prompts):
            self.gen_selected_index = index + 1
        self.gen_load_prompts()
        return "break"

    def gen_clear_point(self):
        self.gen_points.pop(self.gen_selected_index, None)
        self.gen_load_prompts()
        self.gen_write(f"Cleared point for prompt {self.gen_selected_index + 1}.\n")

    def gen_suggest_with_gpt(self):
        if self.gen_image is None:
            self.gen_write("Suggest failed: capture or paste an image first.\n")
            return
        try:
            count = max(1, int(self.gen_suggest_count_var.get().strip()))
        except ValueError:
            count = 4
            self.gen_suggest_count_var.set("4")
        image = self.gen_image.copy()
        existing = self.current_gen_annotations()
        self.gen_pending_suggest_count = count
        self.gen_suggest_btn.configure(state=tk.DISABLED)
        self.status_var.set("GPT suggesting targets")
        self.gen_write(f"Requesting {count} more clickable target suggestion(s) from GPT...\n")
        threading.Thread(
            target=self.gen_suggest_with_gpt_worker,
            args=(image, count, existing),
            daemon=True,
        ).start()

    def current_gen_annotations(self):
        prompts = self.gen_prompt_lines()
        rows = []
        for i, prompt in enumerate(prompts):
            point = self.gen_points.get(i)
            row = {"label": prompt}
            if point:
                row["x"] = point["x"]
                row["y"] = point["y"]
            rows.append(row)
        return rows

    def gen_suggest_with_gpt_worker(self, image, count, existing):
        raw = ""
        try:
            agent_path = WORKSPACE / "agent_clicker"
            if str(agent_path) not in sys.path:
                sys.path.insert(0, str(agent_path))
            from agent import codex_backend

            width, height = image.size
            system = (
                "You are a GUI annotation assistant. Identify useful clickable UI targets "
                "for training a computer-use click model. Return JSON only."
            )
            prompt = (
                f"Find exactly {count} additional distinct clickable things visible in this screenshot. "
                "Choose a diverse, semi-random set across the whole visible UI, not just the "
                "largest or most obvious controls. Include a mix when visible: text fields, "
                "buttons, tabs, icons, menu items, links, chips/filters, list rows, cards, "
                "tiles, thumbnails, sidebar items, toolbar controls, window controls, and "
                "small clickable affordances. Spread targets across top/middle/bottom and "
                "left/center/right areas when possible. Avoid duplicate targets and avoid "
                "ambiguous background areas.\n\n"
                f"Screenshot size: {width}x{height}. Coordinates are original-image pixels "
                "with top-left (0,0). Use the center of the clickable target.\n\n"
                "Labels must be specific enough for a training example. Include the app/site/context "
                "and screen position when useful, e.g. 'click the YouTube search field at the top "
                "middle of the browser window', not just 'click the search field'. If the target is "
                "wide, still give one click point near the functional center of the field.\n\n"
                "Already selected targets to avoid duplicating:\n"
                f"{json.dumps(existing, ensure_ascii=False, indent=2)}\n\n"
                "Return exactly this JSON shape, no markdown:\n"
                "{\n"
                '  "targets": [\n'
                '    {"label": "click the ...", "x": 123, "y": 456, "reason": "..."},\n'
                '    {"label": "click the ...", "x": 123, "y": 456, "reason": "..."}\n'
                "  ]\n"
                "}"
            )
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64," + image_to_b64_png(image),
                                "detail": "high",
                            },
                        },
                    ],
                }
            ]
            raw = codex_backend.chat_raw(system, messages, model="gpt-5.5", timeout=240)
            data = extract_json_object(raw)
            suggestions = self.normalize_gpt_suggestions(data, width, height, count)
            self.events.put(("gen_suggestions", suggestions, raw))
        except Exception as exc:
            self.events.put(("gen_suggest_error", str(exc), raw))

    def normalize_gpt_suggestions(self, data, width, height, count=None):
        targets = data.get("targets") if isinstance(data, dict) else None
        if not isinstance(targets, list):
            raise ValueError(f"GPT response has no targets list: {data}")
        out = []
        limit = count or len(targets)
        for target in targets[:limit]:
            if not isinstance(target, dict):
                continue
            label = str(target.get("label") or target.get("prompt") or target.get("description") or "").strip()
            if not label:
                label = "click the target"
            x, y = self.coerce_xy(target.get("x"), target.get("y"))
            x = max(0, min(width - 1, int(round(float(x)))))
            y = max(0, min(height - 1, int(round(float(y)))))
            out.append(
                {
                    "label": label,
                    "x": x,
                    "y": y,
                    "reason": str(target.get("reason", "")),
                }
            )
        if not out:
            raise ValueError(f"GPT returned no usable target points: {data}")
        return out

    def apply_gpt_suggestions(self, suggestions):
        existing_prompts = self.gen_prompt_lines()
        prompt_lines = existing_prompts + [item["label"] for item in suggestions]
        self.gen_prompts.delete("1.0", tk.END)
        self.gen_prompts.insert("1.0", "\n".join(prompt_lines))
        start_index = len(existing_prompts)
        old_points = dict(self.gen_points)
        self.gen_points = old_points
        for offset, item in enumerate(suggestions):
            self.gen_points[start_index + offset] = {
                "x": item["x"],
                "y": item["y"],
                "suggested_by": "gpt-5.5",
                "reason": item.get("reason", ""),
            }
        self.gen_selected_index = start_index if suggestions else self.gen_selected_index
        self.gen_load_prompts()
        self.gen_write(
            f"Added {len(suggestions)} GPT suggestion(s). Review points, right-click to adjust, then save.\n"
        )

    def redraw_gen_canvas(self):
        self.gen_canvas.delete("all")
        width = max(1, self.gen_canvas.winfo_width())
        height = max(1, self.gen_canvas.winfo_height())
        if self.gen_image is None:
            self.gen_canvas.create_text(
                width // 2,
                height // 2,
                text="Capture or paste an image",
                fill=self.colors["muted"],
                font=("Segoe UI", 13, "bold"),
            )
            return
        img_w, img_h = self.gen_image.size
        if not self.gen_user_view:
            self.gen_fit_image_to_canvas()
        scale = self.gen_scale
        offset_x, offset_y = self.gen_offset
        display_w = max(1, int(img_w * scale))
        display_h = max(1, int(img_h * scale))
        resampling = getattr(Image, "Resampling", Image).LANCZOS
        display = self.gen_image.resize((display_w, display_h), resampling)
        self.gen_photo = ImageTk.PhotoImage(display)
        self.gen_canvas.create_image(offset_x, offset_y, anchor="nw", image=self.gen_photo)

        for index, point in self.gen_points.items():
            x = point["x"] * scale + offset_x
            y = point["y"] * scale + offset_y
            color = "#39e58c" if index == self.gen_selected_index else "#7aa2ff"
            radius = 9
            self.gen_canvas.create_oval(x - radius, y - radius, x + radius, y + radius, outline=color, width=3)
            self.gen_canvas.create_line(x - 15, y, x + 15, y, fill=color, width=2)
            self.gen_canvas.create_line(x, y - 15, x, y + 15, fill=color, width=2)
            self.gen_canvas.create_text(
                x + 12,
                y - 16,
                text=str(index + 1),
                fill=color,
                anchor="w",
                font=("Segoe UI", 11, "bold"),
            )

    def save_gen_batch(self):
        if self.gen_image is None:
            self.gen_write("Save failed: capture or paste an image first.\n")
            return
        prompts = self.gen_prompt_lines()
        if not prompts:
            self.gen_write("Save failed: load prompt lines first.\n")
            return
        labeled = [(i, prompt, self.gen_points.get(i)) for i, prompt in enumerate(prompts) if self.gen_points.get(i)]
        if not labeled:
            self.gen_write("Save failed: label at least one prompt.\n")
            return

        DATASET_ROOT.mkdir(parents=True, exist_ok=True)
        screenshots_dir = DATASET_ROOT / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        dataset_path = DATASET_ROOT / "dataset.jsonl"
        batch_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        image_name = f"{batch_id}.png"
        image_path = screenshots_dir / image_name
        self.gen_image.convert("RGB").save(image_path, format="PNG", optimize=True)
        width, height = self.gen_image.size

        with dataset_path.open("a", encoding="utf-8") as handle:
            for index, prompt, point in labeled:
                row = {
                    "schema": "aios.gui_click.v1",
                    "id": f"{batch_id}_{index + 1:02d}",
                    "batch_id": batch_id,
                    "image": str(image_path.relative_to(DATASET_ROOT)).replace("\\", "/"),
                    "prompt": prompt,
                    "action": {
                        "type": "click",
                        "x": point["x"],
                        "y": point["y"],
                        "button": "left",
                        "double": False,
                        "clicks": 1,
                    },
                    "metadata": {
                        "width": width,
                        "height": height,
                        "source": "human_verified_batch",
                        "suggested_by": point.get("suggested_by"),
                        "suggestion_reason": point.get("reason", ""),
                        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    },
                }
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.gen_write(f"Saved {len(labeled)} examples from batch {batch_id}\nDataset: {dataset_path}\n")

    def startup(self):
        self.events.put(("status", "Starting Ollama"))
        if not ensure_ollama():
            self.events.put(("error", "Ollama is not reachable. Start Ollama and press Refresh."))
            self.events.put(("status", "Offline"))
            return
        self.events.put(("status", "Ready"))
        self.refresh_models_async()

    def refresh_models(self):
        threading.Thread(target=self.refresh_models_async, daemon=True).start()

    def refresh_models_async(self):
        try:
            tags = request_json("/api/tags", timeout=10)
            names = [item["name"] for item in tags.get("models", [])]
            if DEFAULT_MODEL not in names:
                names.insert(0, DEFAULT_MODEL)
            if VISION_MODEL not in names:
                names.append(VISION_MODEL)
            self.events.put(("models", names))
            self.events.put(("status", "Ready"))
        except Exception as exc:
            self.events.put(("error", f"Model refresh failed: {exc}"))

    def clear_chat(self):
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.current_assistant_open = False
        self.assistant_stream_start = None
        self.assistant_placeholder_range = None
        self.assistant_stream_has_content = False
        self.chat.configure(state=tk.NORMAL)
        self.chat.delete("1.0", tk.END)
        self.chat.configure(state=tk.DISABLED)
        self.status_var.set("Ready")

    def enter_to_send(self, event):
        if event.state & 0x0001:
            return None
        return self.send_message(event)

    def send_message(self, event=None):
        if self.sending:
            return "break"
        prompt = self.prompt.get("1.0", tk.END).strip()
        if not prompt:
            return "break"

        self.prompt.delete("1.0", tk.END)
        self.append_block("You", prompt, "user_name")
        self.open_assistant_block()
        self.sending = True
        self.send_btn.configure(state=tk.DISABLED)
        self.status_var.set("Running")

        history = list(self.messages)
        history.append({"role": "user", "content": prompt})
        model = self.model_var.get().strip() or DEFAULT_MODEL
        think = self.think_var.get()
        tools = self.tools_var.get()
        show_thinking = self.think_var.get()

        threading.Thread(
            target=self.worker,
            args=(history, model, think, tools, show_thinking),
            daemon=True,
        ).start()
        return "break"

    def worker(self, history, model, think, tools, show_thinking):
        try:
            if tools:
                final = self.chat_with_tools(history, model, think, show_thinking)
            else:
                final = self.chat_stream(history, model, think, show_thinking)
            new_history = history + [{"role": "assistant", "content": final}]
            self.events.put(("done", new_history))
        except URLError as exc:
            self.events.put(("error", f"Ollama request failed: {exc}"))
            self.events.put(("done", self.messages))
        except Exception as exc:
            self.events.put(("error", str(exc)))
            self.events.put(("done", self.messages))

    def chat_stream(self, history, model, think, show_thinking):
        final_parts = []
        payload = {
            "model": model,
            "messages": history,
            "stream": True,
            "keep_alive": KEEP_ALIVE,
            "think": think_setting(model, think),
            "options": {"num_ctx": 4096, "temperature": 0.55},
        }
        for chunk in stream_chat(payload):
            message = chunk.get("message", {})
            thinking = message.get("thinking")
            content = message.get("content")
            if thinking and show_thinking:
                self.events.put(("token", thinking, "thinking"))
            if content:
                final_parts.append(content)
                self.events.put(("token", content, "body"))
            if chunk.get("done"):
                break
        return "".join(final_parts).strip()

    def chat_with_tools(self, history, model, think, show_thinking):
        working = [dict(item) for item in history]
        final_text = ""
        for _ in range(5):
            payload = {
                "model": model,
                "messages": working,
                "stream": False,
                "keep_alive": KEEP_ALIVE,
                "think": think_setting(model, think),
                "tools": TOOLS,
                "options": {"num_ctx": 4096, "temperature": 0.45},
            }
            response = request_json("/api/chat", payload, timeout=900)
            message = response.get("message", {})
            if message.get("thinking") and show_thinking:
                self.events.put(("token", message["thinking"] + "\n", "thinking"))

            content = message.get("content") or ""
            tool_calls = message.get("tool_calls") or []
            if content:
                final_text += content
                self.events.put(("token", content, "body"))

            if not tool_calls:
                return final_text.strip()

            working.append(message)
            for call in tool_calls:
                function = call.get("function", {})
                name = function.get("name", "")
                args = function.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                self.events.put(("tool", name))
                result = run_tool(name, args)
                working.append({"role": "tool", "content": result, "tool_name": name})

        return final_text.strip() or "Stopped after too many tool calls."

    def append_block(self, speaker, text, name_tag):
        self.chat.configure(state=tk.NORMAL)
        self.chat.insert(tk.END, f"{speaker}\n", name_tag)
        self.insert_markdown_text(text.strip(), "body")
        self.chat.insert(tk.END, "\n", "body")
        self.chat.configure(state=tk.DISABLED)
        self.chat.see(tk.END)

    def insert_markdown_text(self, text, default_tag="body"):
        in_code = False
        for line in text.splitlines() or [""]:
            stripped = line.strip()
            if stripped.startswith("```"):
                in_code = not in_code
                continue
            if in_code:
                self.chat.insert(tk.END, line + "\n", "code")
            else:
                self.insert_markdown_line(line, default_tag)

    def insert_markdown_line(self, line, default_tag="body"):
        stripped = line.strip()
        if not stripped:
            self.chat.insert(tk.END, "\n", default_tag)
            return
        heading = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        if heading:
            self.insert_inline_markdown(heading.group(2), "heading")
            self.chat.insert(tk.END, "\n", "heading")
            return
        quote = re.match(r"^>\s?(.*)$", stripped)
        if quote:
            self.insert_inline_markdown(quote.group(1), "quote")
            self.chat.insert(tk.END, "\n", "quote")
            return
        bullet = re.match(r"^([-*+]|\d+[.)])\s+(.+)$", stripped)
        if bullet:
            marker = bullet.group(1)
            marker = "•" if marker in {"-", "*", "+"} else marker
            self.chat.insert(tk.END, f"{marker} ", default_tag)
            self.insert_inline_markdown(bullet.group(2), default_tag)
            self.chat.insert(tk.END, "\n", default_tag)
            return
        self.insert_inline_markdown(line, default_tag)
        self.chat.insert(tk.END, "\n", default_tag)

    def insert_inline_markdown(self, text, default_tag="body"):
        pattern = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`|\*[^*\n]+\*|\[[^\]]+\]\([^)]+\))")
        pos = 0
        for match in pattern.finditer(text):
            if match.start() > pos:
                self.chat.insert(tk.END, text[pos:match.start()], default_tag)
            token = match.group(0)
            if token.startswith("**") and token.endswith("**"):
                self.chat.insert(tk.END, token[2:-2], "bold")
            elif token.startswith("`") and token.endswith("`"):
                self.chat.insert(tk.END, token[1:-1], "inline_code")
            elif token.startswith("*") and token.endswith("*"):
                self.chat.insert(tk.END, token[1:-1], "italic")
            else:
                link_match = re.match(r"\[([^\]]+)\]\(([^)]+)\)", token)
                if link_match:
                    self.chat.insert(tk.END, link_match.group(1), "link")
                    self.chat.insert(tk.END, f" ({link_match.group(2)})", default_tag)
                else:
                    self.chat.insert(tk.END, token, default_tag)
            pos = match.end()
        if pos < len(text):
            self.chat.insert(tk.END, text[pos:], default_tag)

    def clear_assistant_placeholder(self):
        if not self.assistant_placeholder_range:
            return
        start, end = self.assistant_placeholder_range
        try:
            self.chat.delete(start, end)
        except tk.TclError:
            pass
        self.assistant_placeholder_range = None

    def open_assistant_block(self):
        self.chat.configure(state=tk.NORMAL)
        self.chat.insert(tk.END, "AI\n", "assistant_name")
        self.assistant_stream_start = self.chat.index(tk.END + "-1c")
        self.assistant_stream_has_content = False
        placeholder_start = self.chat.index(tk.END + "-1c")
        self.chat.insert(tk.END, "Working...\n", "thinking")
        self.assistant_placeholder_range = (placeholder_start, self.chat.index(tk.END + "-1c"))
        self.chat.configure(state=tk.DISABLED)
        self.chat.see(tk.END)
        self.current_assistant_open = True

    def append_token(self, text, tag):
        self.chat.configure(state=tk.NORMAL)
        self.clear_assistant_placeholder()
        if tag == "body" and not self.assistant_stream_has_content:
            self.assistant_stream_start = self.chat.index(tk.END + "-1c")
            self.assistant_stream_has_content = True
        self.chat.insert(tk.END, text, tag)
        self.chat.configure(state=tk.DISABLED)
        self.chat.see(tk.END)

    def render_current_assistant_markdown(self, text):
        final_text = str(text or "").strip()
        if not final_text or not self.assistant_stream_has_content or not self.assistant_stream_start:
            return
        self.chat.configure(state=tk.NORMAL)
        self.clear_assistant_placeholder()
        try:
            self.chat.delete(self.assistant_stream_start, tk.END + "-1c")
            self.insert_markdown_text(final_text, "body")
        except tk.TclError:
            pass
        self.chat.configure(state=tk.DISABLED)
        self.chat.see(tk.END)

    def append_tool(self, name):
        self.chat.configure(state=tk.NORMAL)
        self.clear_assistant_placeholder()
        self.chat.insert(tk.END, f"\n{name}\n", "tool_name")
        self.chat.configure(state=tk.DISABLED)
        self.chat.see(tk.END)

    def process_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "status":
                    self.status_var.set(event[1])
                elif kind == "models":
                    names = event[1]
                    self.model_combo.configure(values=names)
                    self.eval_model_combo.configure(values=names)
                    if self.model_var.get() not in names and names:
                        self.model_var.set(names[0])
                    if self.eval_model_var.get() not in names:
                        self.eval_model_var.set(VISION_MODEL if VISION_MODEL in names else names[0])
                elif kind == "token":
                    _, text, tag = event
                    self.append_token(text, tag)
                elif kind == "tool":
                    self.append_tool(event[1])
                elif kind == "error":
                    self.chat.configure(state=tk.NORMAL)
                    self.clear_assistant_placeholder()
                    self.chat.configure(state=tk.DISABLED)
                    self.append_token("\n" + event[1] + "\n", "error")
                elif kind == "done":
                    self.messages = event[1]
                    final = ""
                    if self.messages and self.messages[-1].get("role") == "assistant":
                        final = self.messages[-1].get("content") or ""
                    self.render_current_assistant_markdown(final)
                    self.append_token("\n", "body")
                    self.assistant_stream_start = None
                    self.assistant_placeholder_range = None
                    self.assistant_stream_has_content = False
                    self.sending = False
                    self.send_btn.configure(state=tk.NORMAL)
                    self.status_var.set("Ready")
                elif kind == "eval_result":
                    _, prediction, raw = event
                    self.eval_prediction = prediction
                    self.eval_raw_response = raw
                    self.redraw_eval_canvas()
                    self.eval_write(
                        json.dumps(prediction, ensure_ascii=False, indent=2)
                        + "\nRaw response:\n"
                        + raw.strip()
                        + "\n"
                    )
                    self.eval_run_btn.configure(state=tk.NORMAL)
                    self.status_var.set("Ready")
                elif kind == "eval_error":
                    raw = event[2] if len(event) > 2 else ""
                    self.eval_raw_response = raw
                    self.eval_write(f"Evaluation failed: {event[1]}\n")
                    if raw:
                        self.eval_write("Raw response:\n" + raw.strip() + "\n")
                    self.eval_run_btn.configure(state=tk.NORMAL)
                    self.status_var.set("Ready")
                elif kind == "gen_suggestions":
                    _, suggestions, raw = event
                    self.apply_gpt_suggestions(suggestions)
                    self.gen_write("Raw GPT response:\n" + raw.strip() + "\n")
                    if self.gen_suggest_btn:
                        self.gen_suggest_btn.configure(state=tk.NORMAL)
                    self.status_var.set("Ready")
                elif kind == "gen_suggest_error":
                    raw = event[2] if len(event) > 2 else ""
                    self.gen_write(f"GPT suggestion failed: {event[1]}\n")
                    if raw:
                        self.gen_write("Raw GPT response:\n" + raw.strip() + "\n")
                    if self.gen_suggest_btn:
                        self.gen_suggest_btn.configure(state=tk.NORMAL)
                    self.status_var.set("Ready")
        except queue.Empty:
            pass
        self.root.after(50, self.process_events)


def main():
    root = tk.Tk()
    app = LocalAIApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
