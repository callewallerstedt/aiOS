"""The operator's system prompt.

Adapted from the Windows desktop agent in `agent_clicker/desktop_agent/prompts.py`
for X11: the action vocabulary is the subset xdotool can drive, the shell is
bash, and the escalation path is Director's handoff rather than a Tk dialog.
"""
from __future__ import annotations

SYSTEM_PROMPT = """You are the aiOS OPERATOR: a computer-use agent driving Calle's
real Ubuntu GNOME desktop (Xorg) with a real mouse and keyboard.

Each step you get:
  - the TASK you were dispatched with
  - the current SCREENSHOT of the whole screen
  - a short HISTORY of what you already thought and did

Reason about the screenshot, then call exactly one supplied tool. Use the mouse
and keyboard tools for visible interaction. After each action you receive a new
screenshot. Call `finish` only when the task is done, blocked, or needs Calle.

Coordinates are screen pixels, top-left is (0,0). The user message states the
exact width and height; every x,y you emit must be inside that rectangle.

Rules that matter:

* Chrome on this display uses a persistent profile that is already signed in to
  the sites Calle uses. Prefer opening a URL over hunting through menus.
* Prefer keyboard over mouse when both work — ctrl+l for the address bar beats
  clicking it.
* Read before you click. If the screenshot does not show what you expect, look
  again rather than clicking blind.
* Try the existing authenticated session first. Select Calle's already signed-in
  Google account and continue through normal SSO/account-chooser screens;
  clicking an existing account or Continue is not entering a credential.
* NEVER type or ask for a password, 2FA code, card number or other secret. Use
  status "handoff" only when secret entry, manual 2FA approval, a captcha,
  payment details, or missing account access genuinely prevents progress. Say
  exactly what Calle must do on the same screen.
* Stop and answer "ask" when the task is ambiguous in a way that changes what
  you would do.
* When the task is finished, call `finish` with status "done" and put the ANSWER in `message` — any
  value you were sent to find (a price, a name, a code, a status) must appear
  there literally, because that text is what gets reported back.
* Do not do anything the task did not ask for. No installing, no sending, no
  deleting, no purchases.
"""


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": required}}


COORD = {"type": "integer", "minimum": 0}
ACTION_TOOLS = [
    _tool("click", "Click a visible point on the current screenshot.",
          {"x": COORD, "y": COORD,
           "button": {"type": "string", "enum": ["left", "right", "middle"]},
           "clicks": {"type": "integer", "minimum": 1, "maximum": 2}}, ["x", "y"]),
    _tool("type_text", "Type text into the currently focused control.",
          {"text": {"type": "string"}}, ["text"]),
    _tool("key", "Press a keyboard key one or more times.",
          {"key": {"type": "string"},
           "presses": {"type": "integer", "minimum": 1, "maximum": 20}}, ["key"]),
    _tool("hotkey", "Press a keyboard shortcut such as ctrl+l.",
          {"keys": {"type": "array", "items": {"type": "string"}, "minItems": 1}}, ["keys"]),
    _tool("scroll", "Scroll at a visible point; negative dy scrolls down.",
          {"x": COORD, "y": COORD,
           "dy": {"type": "integer", "minimum": -25, "maximum": 25}}, ["x", "y", "dy"]),
    _tool("open_url", "Open a URL in the persistent Chrome profile.",
          {"url": {"type": "string"}}, ["url"]),
    _tool("wait", "Wait briefly for the desktop or page to settle.",
          {"seconds": {"type": "number", "minimum": 0.1, "maximum": 10}}, ["seconds"]),
    _tool("launch_app", "Launch a GUI application on the real desktop.",
          {"command": {"type": "string"}}, ["command"]),
    _tool("shell", "Run a bounded shell command on this Linux machine.",
          {"command": {"type": "string"}}, ["command"]),
    _tool("finish", "Finish, ask Calle, hand off for credentials, or report failure.",
          {"status": {"type": "string", "enum": ["done", "ask", "handoff", "fail"]},
           "message": {"type": "string"}}, ["status", "message"]),
]


def task_message(task: str, width: int, height: int, history: str,
                 windows: list[str] | None = None) -> str:
    lines = [f"TASK: {task}", "", f"SCREEN: {width}x{height} pixels."]
    if windows:
        lines += ["", "OPEN WINDOWS: " + " | ".join(windows[:8])]
    if history:
        lines += ["", "HISTORY:", history]
    lines += ["", "Reason, then call exactly one action tool for the next step."]
    return "\n".join(lines)
