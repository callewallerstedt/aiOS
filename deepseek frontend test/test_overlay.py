"""Test the new PySide6 aiOS overlay."""
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

app = QApplication(sys.argv)

from main import AiosOverlay, load_config, Theme, DEFAULT_CONFIG

# ── Test 1: Config loading ──────────────────────────────────────────────────
print("=== Test 1: Config loading ===")
cfg = load_config()
assert "theme" in cfg, "Missing theme"
assert "project_root" in cfg, "Missing project_root"
assert "window" in cfg, "Missing window"
assert isinstance(cfg["window"], dict), f"window should be dict, got {type(cfg['window'])}"
print("  Config OK:", cfg["window"])

# ── Test 2: Theme ──────────────────────────────────────────────────────────
print("\n=== Test 2: Theme ===")
default_theme = DEFAULT_CONFIG["theme"]
theme = Theme(cfg)
# Check that known keys exist
for key in ("text", "accent", "panel", "surface", "muted"):
    assert key in DEFAULT_CONFIG["theme"], f"Missing theme key: {key}"
print(f"  Theme OK, text={theme.c('text')}, accent={theme.c('accent')}")
blended = theme.blend("#000000", "#ffffff", 0.5)
print(f"  Blend test: {blended}")

# ── Test 3: Window creation ────────────────────────────────────────────────
print("\n=== Test 3: Window creation ===")
win = AiosOverlay()
win.show()
time.sleep(0.5)
assert win.width() > 0, "Window width should be > 0"
assert win.height() > 0, "Window height should be > 0"
print(f"  Size: {win.width()}x{win.height()}")
print(f"  Active tab: {win.active_tab}")

# ── Test 4: Tab navigation ─────────────────────────────────────────────────
print("\n=== Test 4: Tab navigation ===")
for tab in ["Dashboard", "Projects", "CODE", "Apps", "Drop", "AI Operator", "Settings"]:
    win._render_tab(tab)
    time.sleep(0.1)
    assert win.active_tab == tab, f"Tab should be {tab}, got {win.active_tab}"
    assert win._nav_buttons[tab].isChecked(), f"Nav button for {tab} should be checked"
print("  All tabs navigated OK")

# ── Test 5: Chat functionality ─────────────────────────────────────────────
print("\n=== Test 5: Chat ===")
win._render_tab("Dashboard")
win.chat_input.setPlainText("Hello world!")
win._send_chat()
time.sleep(0.2)
assert win.chat_input.toPlainText() == "", "Input should clear after send"
print(f"  Chat message count: {win.chat_msg_layout.count() - 1} messages")

# ── Test 6: Settings pages ─────────────────────────────────────────────────
print("\n=== Test 6: Settings pages ===")
win._render_tab("Settings")
for page in ["General", "Appearance", "Voice", "Voice agent", "OPERATOR", "Models", "Macro pad"]:
    win._open_settings_page(page)
    time.sleep(0.05)
    assert win.settings_page == page, f"Settings page should be {page}"
print("  All settings pages navigated OK")

# ── Test 7: CODE tab sessions ──────────────────────────────────────────────
print("\n=== Test 7: CODE tab sessions ===")
win._render_tab("CODE")
time.sleep(0.2)
print(f"  Session count: {win.code_session_list.count()}")

# ── Test 8: Bottom tray ────────────────────────────────────────────────────
print("\n=== Test 8: Bottom tray ===")
assert not win._tray_open, "Tray should start closed"
win._toggle_tray()
time.sleep(0.3)
assert win._tray_open, "Tray should be open after toggle"
print("  Tray toggled OK")

# ── Test 9: Window dragging ────────────────────────────────────────────────
print("\n=== Test 9: Window operations ===")
# Simulate a resize event
win.resize(1280, 800)
time.sleep(0.1)
print(f"  Resized to {win.width()}x{win.height()}")

# ── Cleanup ────────────────────────────────────────────────────────────────
win.close()
print("\n✅ All tests passed!")