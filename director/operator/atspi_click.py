"""Invoke the visible accessible control at a desktop coordinate.

GTK deliberately ignores app-directed synthetic X11 button events in some
windows. AT-SPI is the desktop-native control path for those windows: resolve
the foreground widget at the requested point and perform its advertised action.
This helper runs under the system Python because Ubuntu packages pyatspi there.
"""
from __future__ import annotations

import json
import sys
from typing import Any


def _contains(node: Any, x: int, y: int, coords: int) -> bool:
    try:
        return bool(node.queryComponent().contains(x, y, coords))
    except Exception:
        return False


def _label(node: Any) -> str:
    try:
        return str(node.name or "")
    except Exception:
        return ""


def _action(node: Any) -> tuple[Any, int, str] | None:
    try:
        actions = node.queryAction()
    except Exception:
        return None
    choices = []
    for index in range(int(actions.nActions)):
        try:
            name = str(actions.getName(index) or "").lower()
        except Exception:
            name = ""
        choices.append((index, name))
    if not choices:
        return None
    preferred = ("click", "press", "activate", "open", "jump")
    choices.sort(key=lambda row: (row[1] not in preferred, row[0]))
    index, name = choices[0]
    return actions, index, name or "default"


def _focus(node: Any, pyatspi: Any) -> bool:
    try:
        if not node.getState().contains(pyatspi.STATE_FOCUSABLE):
            return False
        return bool(node.queryComponent().grabFocus())
    except Exception:
        return False


def _deepest_action(node: Any, x: int, y: int, coords: int, pyatspi: Any,
                    depth: int = 0, inside_dialog: bool = False
                    ) -> tuple[str, Any, Any, int, str] | tuple[str, Any] | None:
    if depth and not _contains(node, x, y, coords):
        return None
    try:
        inside_dialog = inside_dialog or node.getRole() in {
            pyatspi.ROLE_ALERT, pyatspi.ROLE_DIALOG,
        }
    except Exception:
        pass
    try:
        children = list(node)
    except Exception:
        children = []
    for child in reversed(children):
        found = _deepest_action(
            child, x, y, coords, pyatspi, depth + 1, inside_dialog)
        if found:
            return found
    # Normal application windows keep the existing coordinate mouse path.
    # Accessibility is the reliable fallback specifically while a desktop
    # dialog owns the foreground input grab.
    if not inside_dialog:
        return None
    action = _action(node)
    if action:
        return "action", node, *action
    if _focus(node, pyatspi):
        return "focus", node
    return None


def _windows(desktop: Any, pyatspi: Any) -> list[Any]:
    roles = {
        pyatspi.ROLE_ALERT, pyatspi.ROLE_DIALOG, pyatspi.ROLE_FRAME,
        pyatspi.ROLE_WINDOW,
    }
    rows = []
    for app in desktop:
        try:
            children = list(app)
        except Exception:
            continue
        for child in children:
            try:
                if child.getRole() in roles:
                    rows.append(child)
            except Exception:
                continue
    return rows


def click_at(x: int, y: int, title: str = "") -> dict[str, Any]:
    import pyatspi  # system package; intentionally lazy for offline unit tests

    desktop = pyatspi.Registry.getDesktop(0)
    wanted = str(title or "").strip().casefold()
    windows = _windows(desktop, pyatspi)
    candidates = [row for row in windows if _contains(row, x, y, pyatspi.DESKTOP_COORDS)]
    if wanted:
        exact = [row for row in candidates if _label(row).strip().casefold() == wanted]
        partial = [row for row in candidates if wanted in _label(row).casefold()
                   or _label(row).casefold() in wanted]
        candidates = exact or partial
    else:
        # The caller identifies the front X11 window by title. Without that
        # identity, do not risk activating a covered window at the same point.
        candidates = []
    for window in candidates:
        found = _deepest_action(window, x, y, pyatspi.DESKTOP_COORDS, pyatspi)
        if not found:
            continue
        kind, node, *details = found
        if kind == "focus":
            return {"handled": True, "target": _label(node) or "control", "action": "focus"}
        actions, index, action_name = details
        try:
            handled = bool(actions.doAction(index))
        except Exception as exc:
            return {"handled": False, "error": f"{type(exc).__name__}: {exc}"}
        if handled:
            return {
                "handled": True,
                "target": _label(node) or "control",
                "action": action_name,
            }
    return {"handled": False, "error": "no actionable accessible control at coordinate"}


def main(argv: list[str]) -> int:
    try:
        x, y = int(argv[1]), int(argv[2])
        title = argv[3] if len(argv) > 3 else ""
        result = click_at(x, y, title)
    except Exception as exc:
        result = {"handled": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result.get("handled") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
