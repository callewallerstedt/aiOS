"""Director desktop shell wiring."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import director_desktop  # noqa: E402


def test_director_icon_exists():
    path = director_desktop.icon_path()
    assert path is not None
    assert path.is_file()


def test_director_url_is_the_public_pwa():
    assert director_desktop.default_url().startswith("https://phonesite")


def test_director_app_id_is_unique_from_aios_helper():
    assert director_desktop.APP_USER_MODEL_ID == "aiOS.Director.Desktop"
    assert director_desktop.APP_USER_MODEL_ID != "aiOS.Desktop.Helper"


def test_launcher_exe_is_under_assets():
    path = director_desktop.LAUNCHER_EXE
    assert path.parent.name == "assets"
    assert path.name == "aios-director.exe"


def test_launcher_command_uses_branded_exe_on_windows(monkeypatch):
    monkeypatch.setattr(director_desktop.os, "name", "nt")
    exe = director_desktop.ROOT / "assets" / "aios-director.exe"
    monkeypatch.setattr(director_desktop, "ensure_launcher_exe", lambda: exe)
    target, arguments = director_desktop.launcher_command()
    assert target == exe
    assert "director_shell.py" in arguments


def test_launcher_pth_includes_site_packages():
    text = director_desktop._launcher_pth_contents()
    assert "import site" in text
    assert "site-packages" in text.replace("\\", "/")
    assert str(director_desktop.ROOT) in text
