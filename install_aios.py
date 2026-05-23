from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

from voice_settings import DEFAULT_VOICE_DICTATION, merge_voice_dictation


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "helper_config.json"
REQUIREMENTS_PATH = BASE_DIR / "requirements.txt"
AGENT_ENV_EXAMPLE = BASE_DIR / "agent_clicker" / ".env.example"
AGENT_ENV = BASE_DIR / "agent_clicker" / ".env"
AUTOHOTKEY_CANDIDATES = (
    Path(r"C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe"),
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "AutoHotkey" / "v2" / "AutoHotkey64.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "AutoHotkey" / "v2" / "AutoHotkey32.exe",
)
CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"
CODEX_CANDIDATES = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Microsoft"
    / "WindowsApps"
    / "codex.exe",
    Path.home() / "AppData/Local/OpenAI/Codex/bin/codex.exe",
)


class Installer(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("aiOS Installer")
        self.geometry("760x560")
        self.minsize(700, 500)
        self.configure(bg="#080b0f")
        self.queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.codex_login_event = threading.Event()
        self.stop_requested = False

        self.install_deps = tk.BooleanVar(value=True)
        self.download_whisper = tk.BooleanVar(value=True)
        self.download_ocr = tk.BooleanVar(value=True)
        self.configure_voice = tk.BooleanVar(value=True)
        self.codex_auth = tk.BooleanVar(value=True)
        self.create_agent_env = tk.BooleanVar(value=True)
        self.install_autohotkey = tk.BooleanVar(value=False)
        self.add_startup = tk.BooleanVar(value=True)
        self.start_now = tk.BooleanVar(value=False)

        self.total_steps = 0
        self.done_steps = 0
        self._build()
        self.after(80, self._pump)

    def _build(self) -> None:
        header = tk.Frame(self, bg="#080b0f")
        header.pack(fill="x", padx=28, pady=(24, 14))
        logo = tk.Label(header, text="aiOS", bg="#080b0f", fg="#ffffff", font=("Segoe UI", 28, "bold"))
        logo.pack(anchor="w")
        sub = tk.Label(
            header,
            text="Install dependencies, configure OPERATOR, and prepare local voice transcription.",
            bg="#080b0f",
            fg="#9fb0c2",
            font=("Segoe UI", 10),
        )
        sub.pack(anchor="w", pady=(4, 0))

        body = tk.Frame(self, bg="#0d1218", highlightbackground="#243244", highlightthickness=1)
        body.pack(fill="x", padx=28, pady=(0, 16))

        self._option(body, self.install_deps, "Python dependencies", "Recommended")
        self._option(body, self.download_whisper, "Download Whisper small model", "Recommended, SV + EN")
        self._option(body, self.download_ocr, "Download OPERATOR OCR models", "Recommended")
        self._option(body, self.configure_voice, "Voice defaults: small, Auto (SV + EN), int8", "Recommended")
        self._option(body, self.codex_auth, "Sign in with Codex for chat + OPERATOR", "Recommended")
        self._option(body, self.create_agent_env, "Create Agent Clicker .env if missing", "Recommended")
        self._option(body, self.install_autohotkey, "Install AutoHotkey v2 with winget", "Only if missing")
        self._option(body, self.add_startup, "Add aiOS hotkey launcher to Windows startup", "Asks before changing startup")
        self._option(body, self.start_now, "Start aiOS after install", "Optional")

        progress_box = tk.Frame(self, bg="#080b0f")
        progress_box.pack(fill="x", padx=28)
        self.status = tk.Label(progress_box, text="Ready", bg="#080b0f", fg="#dbe7f3", anchor="w")
        self.status.pack(fill="x")
        self.progress = tk.Canvas(progress_box, height=12, bg="#101820", highlightthickness=0)
        self.progress.pack(fill="x", pady=(7, 12))
        self.progress_fill = self.progress.create_rectangle(0, 0, 0, 12, fill="#38d996", width=0)
        self.progress.bind("<Configure>", lambda _event: self._draw_progress())

        self.log = tk.Text(
            self,
            height=12,
            bg="#05070a",
            fg="#d7e4ee",
            insertbackground="#ffffff",
            relief="flat",
            padx=12,
            pady=10,
            font=("Consolas", 9),
        )
        self.log.pack(fill="both", expand=True, padx=28, pady=(0, 18))
        self.log.configure(state="disabled")

        actions = tk.Frame(self, bg="#080b0f")
        actions.pack(fill="x", padx=28, pady=(0, 24))
        self.install_btn = tk.Button(
            actions,
            text="Install",
            command=self.start_install,
            bg="#ffffff",
            fg="#05070a",
            activebackground="#dbe7f3",
            relief="flat",
            padx=18,
            pady=8,
            font=("Segoe UI", 10, "bold"),
        )
        self.install_btn.pack(side="left")
        tk.Button(
            actions,
            text="Close",
            command=self.destroy,
            bg="#182333",
            fg="#dbe7f3",
            activebackground="#26384e",
            activeforeground="#ffffff",
            relief="flat",
            padx=18,
            pady=8,
        ).pack(side="left", padx=(10, 0))

    def _option(self, parent: tk.Widget, var: tk.BooleanVar, text: str, hint: str) -> None:
        row = tk.Frame(parent, bg="#0d1218")
        row.pack(fill="x", padx=16, pady=(14, 0))
        cb = tk.Checkbutton(
            row,
            variable=var,
            text=text,
            bg="#0d1218",
            fg="#edf5ff",
            selectcolor="#0d1218",
            activebackground="#0d1218",
            activeforeground="#ffffff",
            font=("Segoe UI", 10, "bold"),
            anchor="w",
        )
        cb.pack(side="left")
        tk.Label(row, text=hint, bg="#0d1218", fg="#8ea0b5", font=("Segoe UI", 9)).pack(side="right")

    def start_install(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if self.add_startup.get():
            ok = messagebox.askyesno(
                "Startup permission",
                "Allow aiOS to add its AutoHotkey launcher to your Windows Startup folder?",
            )
            if not ok:
                self.add_startup.set(False)
                self._append("Startup skipped by user.\n")
        if self.add_startup.get() and not self.install_autohotkey.get() and not self._find_autohotkey():
            ok = messagebox.askyesno(
                "AutoHotkey missing",
                "Startup hotkeys need AutoHotkey v2. Install AutoHotkey with winget?",
            )
            if ok:
                self.install_autohotkey.set(True)
            else:
                self.add_startup.set(False)
                self._append("Startup skipped because AutoHotkey is missing.\n")
        self.install_btn.configure(state="disabled")
        self.done_steps = 0
        self.total_steps = sum(
            bool(v.get())
            for v in (
                self.install_deps,
                self.download_whisper,
                self.download_ocr,
                self.configure_voice,
                self.codex_auth,
                self.create_agent_env,
                self.install_autohotkey,
                self.add_startup,
                self.start_now,
            )
        )
        self._set_status("Installing...")
        self._draw_progress()
        self.worker = threading.Thread(target=self._run_install, daemon=True)
        self.worker.start()

    def _run_install(self) -> None:
        try:
            if self.install_deps.get():
                self._step("Installing Python dependencies", self._install_deps)
            if self.download_whisper.get():
                self._step("Downloading Whisper small model", self._download_whisper)
            if self.download_ocr.get():
                self._step("Downloading OPERATOR OCR models", self._download_ocr)
            if self.configure_voice.get():
                self._step("Writing voice defaults", self._configure_voice_defaults)
            if self.codex_auth.get():
                self._step("Signing in with Codex", self._codex_auth_flow)
            if self.create_agent_env.get():
                self._step("Preparing Agent Clicker .env", self._create_agent_env)
            if self.install_autohotkey.get():
                self._step("Installing AutoHotkey", self._install_autohotkey)
            if self.add_startup.get():
                self._step("Adding startup launcher", self._add_startup)
            if self.start_now.get():
                self._step("Starting aiOS", self._start_aios)
            self.queue.put(("done", None))
        except Exception as exc:
            self.queue.put(("error", str(exc)))

    def _step(self, label: str, fn) -> None:
        self.queue.put(("status", label))
        self.queue.put(("log", f"\n== {label} ==\n"))
        fn()
        self.queue.put(("progress", None))

    def _install_deps(self) -> None:
        self._run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"])
        self._run([sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS_PATH)])

    def _download_whisper(self) -> None:
        code = (
            "from faster_whisper import WhisperModel\n"
            "WhisperModel('small', device='cpu', compute_type='int8')\n"
            "print('Whisper small ready for Auto (SV + EN).')\n"
        )
        self._run([sys.executable, "-c", code])

    def _download_ocr(self) -> None:
        code = (
            "import easyocr\n"
            "try:\n"
            "    easyocr.Reader(['en', 'sv'], gpu=False)\n"
            "    print('EasyOCR ready for EN + SV.')\n"
            "except Exception as exc:\n"
            "    print(f'EN + SV OCR preload failed: {exc}')\n"
            "    easyocr.Reader(['en'], gpu=False)\n"
            "    print('EasyOCR ready for EN.')\n"
        )
        self._run([sys.executable, "-c", code])

    def _configure_voice_defaults(self) -> None:
        config = self._load_config()
        voice = dict(DEFAULT_VOICE_DICTATION)
        voice.update(config.get("voice_dictation") or {})
        voice.update({"whisper_model": "small", "language": "auto", "compute_type": "int8"})
        config["voice_dictation"] = merge_voice_dictation(voice)
        self._save_config(config)
        self.queue.put(("log", "Voice defaults set to small / Auto (SV + EN) / int8.\n"))

    def _codex_auth_flow(self) -> None:
        codex = self._find_codex()
        if not codex:
            raise RuntimeError("codex.exe was not found. Install Codex first, then run this installer again.")
        ok, message = self._codex_auth_available()
        if not ok:
            self.queue.put(("log", f"{message}\nOpening Codex login in a new terminal...\n"))
            self._open_codex_login(codex)
            self.codex_login_event.clear()
            self.queue.put(("ask_codex_done", None))
            self.codex_login_event.wait()
            ok, message = self._codex_auth_available()
        if not ok:
            raise RuntimeError(f"Codex auth was not completed: {message}")
        config = self._load_config()
        ai_operator = dict(config.get("ai_operator") or {})
        ai_operator["codex_auth"] = True
        config["ai_operator"] = ai_operator
        self._save_config(config)
        self.queue.put(("log", "Codex auth verified. OPERATOR Codex mode enabled.\n"))

    def _create_agent_env(self) -> None:
        if AGENT_ENV.exists():
            self.queue.put(("log", "agent_clicker\\.env already exists.\n"))
            return
        if AGENT_ENV_EXAMPLE.exists():
            shutil.copy2(AGENT_ENV_EXAMPLE, AGENT_ENV)
            self.queue.put(("log", "Created agent_clicker\\.env from example.\n"))
        else:
            AGENT_ENV.write_text("OPENAI_API_KEY=\nAGENT_MODEL=gpt-5.5\nAGENT_MAX_ROUNDS=8\nAGENT_OCR=easyocr\n", encoding="utf-8")
            self.queue.put(("log", "Created agent_clicker\\.env.\n"))

    def _install_autohotkey(self) -> None:
        if self._find_autohotkey():
            self.queue.put(("log", "AutoHotkey v2 already found.\n"))
            return
        if shutil.which("winget") is None:
            raise RuntimeError("winget was not found. Install AutoHotkey v2 manually or uncheck this option.")
        self._run(["winget", "install", "--id", "AutoHotkey.AutoHotkey", "-e", "--accept-package-agreements", "--accept-source-agreements"])
        if not self._find_autohotkey():
            self.queue.put(("log", "AutoHotkey install finished, but the executable was not found yet. Restart the installer if startup fails.\n"))

    def _add_startup(self) -> None:
        if not self._find_autohotkey():
            raise RuntimeError("AutoHotkey v2 is required before startup can be added.")
        self._run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(BASE_DIR / "install-startup.ps1")])

    def _start_aios(self) -> None:
        subprocess.Popen(
            [sys.executable, str(BASE_DIR / "helper_overlay.py")],
            cwd=str(BASE_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        self.queue.put(("log", "aiOS started.\n"))

    def _run(self, cmd: list[str]) -> None:
        self.queue.put(("log", f"> {' '.join(cmd)}\n"))
        proc = subprocess.Popen(
            cmd,
            cwd=str(BASE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            self.queue.put(("log", line))
        rc = proc.wait()
        if rc != 0:
            raise RuntimeError(f"Command failed with exit code {rc}: {' '.join(cmd)}")

    def _find_autohotkey(self) -> Path | None:
        for path in AUTOHOTKEY_CANDIDATES:
            if path and path.exists():
                return path
        found = shutil.which("AutoHotkey.exe")
        return Path(found) if found else None

    def _find_codex(self) -> Path | None:
        found = shutil.which("codex") or shutil.which("codex.exe")
        if found:
            return Path(found)
        for path in CODEX_CANDIDATES:
            if path and path.exists():
                return path
        windows_apps = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WindowsApps"
        try:
            matches = sorted(windows_apps.glob("OpenAI.Codex_*/*/resources/codex.exe"), reverse=True)
        except OSError:
            matches = []
        return matches[0] if matches else None

    def _codex_auth_available(self) -> tuple[bool, str]:
        if not CODEX_AUTH_PATH.exists():
            return False, f"No Codex auth file at {CODEX_AUTH_PATH}."
        try:
            data = json.loads(CODEX_AUTH_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return False, f"Could not read Codex auth file: {exc}"
        token = (data.get("tokens") or {}).get("access_token")
        account_id = (data.get("tokens") or {}).get("account_id")
        if not token:
            return False, "Codex auth file has no access token."
        if not account_id:
            return False, "Codex auth file has no account id."
        return True, "ok"

    def _open_codex_login(self, codex: Path) -> None:
        subprocess.Popen(
            ["cmd.exe", "/c", "start", "", "cmd.exe", "/k", f'"{codex}" login'],
            cwd=str(BASE_DIR),
        )

    def _load_config(self) -> dict:
        if CONFIG_PATH.exists():
            try:
                return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}
        return {}

    def _save_config(self, config: dict) -> None:
        CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")

    def _pump(self) -> None:
        while True:
            try:
                kind, value = self.queue.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._append(str(value))
            elif kind == "status":
                self._set_status(str(value))
            elif kind == "progress":
                self.done_steps += 1
                self._draw_progress()
            elif kind == "ask_codex_done":
                messagebox.showinfo(
                    "Codex login",
                    "Finish the Codex browser login in the terminal window, then click OK here.",
                )
                self.codex_login_event.set()
            elif kind == "done":
                self._set_status("Done")
                self.install_btn.configure(state="normal")
                self._append("\nInstall complete.\n")
                messagebox.showinfo("aiOS Installer", "Install complete.")
            elif kind == "error":
                self._set_status("Failed")
                self.install_btn.configure(state="normal")
                self._append(f"\nERROR: {value}\n")
                messagebox.showerror("aiOS Installer", str(value))
        self.after(80, self._pump)

    def _append(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_status(self, text: str) -> None:
        self.status.configure(text=text)

    def _draw_progress(self) -> None:
        width = max(1, self.progress.winfo_width())
        total = max(1, self.total_steps)
        fraction = min(1.0, self.done_steps / total)
        self.progress.coords(self.progress_fill, 0, 0, int(width * fraction), 12)


if __name__ == "__main__":
    Installer().mainloop()
