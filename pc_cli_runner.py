"""Locate Codex / Claude CLI binaries and project folders for the phone bridge."""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

DEFAULT_PROJECTS_ROOT = os.environ.get("AIOS_PROJECTS_ROOT", r"C:\1 - Projects")
DEFAULT_CODEX_MODEL = os.environ.get("AIOS_CODEX_MODEL", "gpt-5.6-sol")
DEFAULT_CLAUDE_MODEL = os.environ.get("AIOS_CLAUDE_MODEL", "sonnet")
CONFIG_PATH = Path(__file__).resolve().parent / "helper_config.json"


def _load_paths() -> dict[str, str]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    paths = data.get("pc_cli") if isinstance(data.get("pc_cli"), dict) else {}
    return {str(k): str(v).strip() for k, v in paths.items() if v}


def find_codex() -> str:
    configured = os.environ.get("AIOS_CODEX_PATH", "").strip() or _load_paths().get("codex_path", "")
    if configured and Path(configured).exists():
        return configured
    candidates: list[Path] = [
        Path.home() / "AppData/Local/OpenAI/Codex/bin/codex.exe",
    ]
    codex_bin = Path.home() / "AppData/Local/OpenAI/Codex/bin"
    if codex_bin.is_dir():
        try:
            candidates.extend(sorted(codex_bin.glob("*/codex.exe"), reverse=True))
        except OSError:
            pass
    # The Microsoft Store command discovered by shutil.which can be readable
    # but not directly executable from a child Python process. Prefer the real
    # per-user Codex binary that the desktop app installs above it.
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    found = shutil.which("codex") or shutil.which("codex.exe")
    if found and "\\WindowsApps\\" not in found:
        return found
    candidates = [Path.home() / "AppData/Local/Microsoft/WindowsApps/codex.exe"]
    windows_apps = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WindowsApps"
    try:
        candidates.extend(sorted(windows_apps.glob("OpenAI.Codex_*/*/resources/codex.exe"), reverse=True))
    except OSError:
        pass
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return ""


def find_claude() -> str:
    configured = os.environ.get("AIOS_CLAUDE_PATH", "").strip() or _load_paths().get("claude_path", "")
    if configured and Path(configured).exists():
        return configured
    found = shutil.which("claude") or shutil.which("claude.exe")
    if found:
        return found
    candidates = [
        Path.home() / "AppData/Roaming/npm/claude.cmd",
        Path.home() / "AppData/Roaming/npm/claude.ps1",
        Path.home() / "AppData/Roaming/npm/claude",
        Path.home() / "AppData/Local/Programs/Anthropic/Claude/claude.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return ""


def resolve_project(projects_root: str, project: str = "") -> Path:
    root = Path(projects_root or DEFAULT_PROJECTS_ROOT)
    project = str(project or "").strip()
    if not project:
        return root
    candidate = Path(project)
    if candidate.is_absolute():
        return candidate
    safe = re.sub(r'[<>:"|?*]', "", project).strip("\\/")
    return root / safe


def cli_status() -> dict:
    codex = find_codex()
    claude = find_claude()
    return {
        "ok": True,
        "codex": codex or None,
        "claude": claude or None,
        "projects_root": DEFAULT_PROJECTS_ROOT,
    }
