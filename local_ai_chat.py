import ast
import json
import math
import os
import queue
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


APP_TITLE = "Local AI"
API_BASE = "http://localhost:11434"
DEFAULT_MODEL = "gpt-oss:20b"
MODEL_ROOT = r"D:\AI\OllamaModels"
WORKSPACE = Path(__file__).resolve().parent
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
        self.sending = False

        self.model_var = tk.StringVar(value=DEFAULT_MODEL)
        self.think_var = tk.BooleanVar(value=True)
        self.tools_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Starting")

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

        body = tk.Frame(self.root, bg=self.colors["panel"])
        body.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 12))
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

        scrollbar = tk.Scrollbar(body, command=self.chat.yview, bg=self.colors["panel"], troughcolor=self.colors["panel"])
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.chat.configure(yscrollcommand=scrollbar.set)

        bottom = tk.Frame(self.root, bg=self.colors["bg"])
        bottom.pack(fill=tk.X, padx=18, pady=(0, 14))
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

        status = tk.Label(
            self.root,
            textvariable=self.status_var,
            bg=self.colors["bg"],
            fg=self.colors["muted"],
            anchor="w",
            font=("Segoe UI", 9),
        )
        status.pack(fill=tk.X, padx=18, pady=(0, 12))

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
            self.events.put(("models", names))
            self.events.put(("status", "Ready"))
        except Exception as exc:
            self.events.put(("error", f"Model refresh failed: {exc}"))

    def clear_chat(self):
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.current_assistant_open = False
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
        self.chat.insert(tk.END, text.strip() + "\n\n", "body")
        self.chat.configure(state=tk.DISABLED)
        self.chat.see(tk.END)

    def open_assistant_block(self):
        self.chat.configure(state=tk.NORMAL)
        self.chat.insert(tk.END, "AI\n", "assistant_name")
        self.chat.configure(state=tk.DISABLED)
        self.chat.see(tk.END)
        self.current_assistant_open = True

    def append_token(self, text, tag):
        self.chat.configure(state=tk.NORMAL)
        self.chat.insert(tk.END, text, tag)
        self.chat.configure(state=tk.DISABLED)
        self.chat.see(tk.END)

    def append_tool(self, name):
        self.chat.configure(state=tk.NORMAL)
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
                    if self.model_var.get() not in names and names:
                        self.model_var.set(names[0])
                elif kind == "token":
                    _, text, tag = event
                    self.append_token(text, tag)
                elif kind == "tool":
                    self.append_tool(event[1])
                elif kind == "error":
                    self.append_token("\n" + event[1] + "\n", "error")
                elif kind == "done":
                    self.messages = event[1]
                    self.append_token("\n", "body")
                    self.sending = False
                    self.send_btn.configure(state=tk.NORMAL)
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
