"""Invoke the visible accessible control at a desktop coordinate.

GTK deliberately ignores app-directed synthetic X11 button events in some
windows. AT-SPI is the desktop-native control path for those windows: resolve
the foreground widget at the requested point and perform its advertised action.
This helper runs under the system Python because Ubuntu packages pyatspi there.
"""
from __future__ import annotations

import hashlib
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
    preferred = ("click", "press", "activate", "open", "jump", "toggle", "check", "select")
    choices = [row for row in choices
               if any(word in row[1] for word in preferred)]
    if not choices:
        return None
    choices.sort(key=lambda row: (next(
        (rank for rank, word in enumerate(preferred) if word in row[1]),
        len(preferred)), row[0]))
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
    interactive_roles = {
        pyatspi.ROLE_CHECK_BOX, pyatspi.ROLE_COMBO_BOX,
        pyatspi.ROLE_LINK, pyatspi.ROLE_LIST_BOX,
        pyatspi.ROLE_MENU_ITEM, pyatspi.ROLE_PAGE_TAB,
        pyatspi.ROLE_PUSH_BUTTON, pyatspi.ROLE_RADIO_BUTTON,
        pyatspi.ROLE_SLIDER, pyatspi.ROLE_SPIN_BUTTON,
        pyatspi.ROLE_TOGGLE_BUTTON,
    }
    try:
        role = node.getRole()
    except Exception:
        role = None
    if role in interactive_roles:
        action = _action(node)
        if action:
            return "action", node, *action
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


def _window_candidates(desktop: Any, pyatspi: Any, title: str = "") -> list[Any]:
    """Return only the foreground window named by X11."""
    wanted = str(title or "").strip().casefold()
    windows = _windows(desktop, pyatspi)
    if not wanted:
        return []
    exact = [row for row in windows if _label(row).strip().casefold() == wanted]
    partial = [row for row in windows if wanted in _label(row).casefold()
               or _label(row).casefold() in wanted]
    return exact or partial


def inspect_controls(title: str = "", limit: int = 60) -> dict[str, Any]:
    """List visible interactive controls and their real screen bounds."""
    import pyatspi  # system package; intentionally lazy for offline unit tests

    desktop = pyatspi.Registry.getDesktop(0)
    controls: list[dict[str, Any]] = []
    interactive_roles = {
        pyatspi.ROLE_CHECK_BOX, pyatspi.ROLE_COMBO_BOX,
        pyatspi.ROLE_ENTRY, pyatspi.ROLE_LINK, pyatspi.ROLE_LIST_BOX,
        pyatspi.ROLE_MENU_ITEM, pyatspi.ROLE_PAGE_TAB,
        pyatspi.ROLE_PASSWORD_TEXT, pyatspi.ROLE_PUSH_BUTTON,
        pyatspi.ROLE_RADIO_BUTTON, pyatspi.ROLE_SLIDER,
        pyatspi.ROLE_SPIN_BUTTON, pyatspi.ROLE_TOGGLE_BUTTON,
    }

    def visit(node: Any, depth: int = 0, path: tuple[int, ...] = ()) -> None:
        if len(controls) >= max(1, int(limit)) or depth > 24:
            return
        try:
            state = node.getState()
            showing = state.contains(pyatspi.STATE_SHOWING)
            visible = state.contains(pyatspi.STATE_VISIBLE)
            role = node.getRole()
            bounds = node.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
        except Exception:
            showing = visible = False
            role = None
            bounds = None
        if (showing and visible and role in interactive_roles and bounds
                and bounds.width > 1 and bounds.height > 1):
            label = _label(node)[:160]
            role_name = str(node.getRoleName() or "control")
            identity = "|".join([
                role_name, label, ".".join(str(value) for value in path),
                str(int(bounds.x)), str(int(bounds.y)),
                str(int(bounds.width)), str(int(bounds.height)),
            ])

            def has_state(name: str) -> bool:
                value = getattr(pyatspi, name, None)
                return bool(value is not None and state.contains(value))

            row = {
                "id": hashlib.sha256(identity.encode("utf-8", "replace")).hexdigest()[:12],
                "role": role_name,
                "name": label,
                "x": int(bounds.x), "y": int(bounds.y),
                "width": int(bounds.width), "height": int(bounds.height),
                "focused": has_state("STATE_FOCUSED"),
                "checked": has_state("STATE_CHECKED"),
                "selected": has_state("STATE_SELECTED"),
                "expanded": has_state("STATE_EXPANDED"),
                "pressed": has_state("STATE_PRESSED"),
                "enabled": has_state("STATE_ENABLED"),
            }
            if role in {pyatspi.ROLE_ENTRY, pyatspi.ROLE_PASSWORD_TEXT}:
                try:
                    row["text_length"] = int(node.queryText().characterCount)
                except Exception:
                    row["text_length"] = None
            controls.append(row)
        try:
            children = list(node)
        except Exception:
            children = []
        for index, child in enumerate(children):
            visit(child, depth + 1, (*path, index))
            if len(controls) >= max(1, int(limit)):
                break

    for window in _window_candidates(desktop, pyatspi, title):
        visit(window)
    return {"controls": controls}


def click_at(x: int, y: int, title: str = "") -> dict[str, Any]:
    import pyatspi  # system package; intentionally lazy for offline unit tests

    desktop = pyatspi.Registry.getDesktop(0)
    candidates = [row for row in _window_candidates(desktop, pyatspi, title)
                  if _contains(row, x, y, pyatspi.DESKTOP_COORDS)]
    for window in candidates:
        found = _deepest_action(window, x, y, pyatspi.DESKTOP_COORDS, pyatspi)
        if not found:
            continue
        kind, node, *details = found
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
        if len(argv) > 1 and argv[1] == "inspect":
            title = argv[2] if len(argv) > 2 else ""
            limit = int(argv[3]) if len(argv) > 3 else 60
            print(json.dumps(inspect_controls(title, limit), ensure_ascii=True))
            return 0
        x, y = int(argv[1]), int(argv[2])
        title = argv[3] if len(argv) > 3 else ""
        result = click_at(x, y, title)
    except Exception as exc:
        result = {"handled": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result.get("handled") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
