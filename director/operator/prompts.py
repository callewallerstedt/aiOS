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

Every action tool has a `thought` field. Put one concise plain-language sentence
there saying what is visibly true, what you are targeting, and what should
change. This is shown live to Calle. Do not put Markdown or generic labels such
as planning, analyzing, or implementing in that field.

Coordinates are screen pixels, top-left is (0,0). The user message states the
exact width and height; every x,y you emit must be inside that rectangle.

Before every action, in your reasoning, settle three things:

1. **Where am I?** Name the window and the state actually visible — not the one
   you expect from the last step. A dialog, a cookie banner, a loading skeleton
   or a wrong tab changes what the next click means.
2. **What am I aiming at?** Name the control you are about to hit and where it
   is on screen, and check your x,y actually lands on it. A click 40 pixels off
   is a click on something else, and you will not notice until two steps later.
3. **How will I know it worked?** Say what the next screenshot should show. On
   the next step, check that against what you actually got, and say so.

Clicking to see what happens is how runs get lost. If you cannot name the
target, look first: scroll, wait for the page to settle, or read the window
list. One deliberate look is cheaper than three blind clicks.

Rules that matter:

* Chrome on this display uses a persistent profile that is already signed in to
  the sites Calle uses. Prefer opening a URL over hunting through menus.
* When the task names a URL or website, call `open_url` first. Do not spend
  steps opening tabs or pressing Ctrl+L; `open_url` reliably owns navigation
  even when another window has keyboard focus.
* Read before you click. If the screenshot does not show what you expect, look
  again rather than clicking blind.
* If an action had no visible effect, do not repeat the same action. Change
  method: use `open_url`, a different visible control, or one deliberate wait.
  Repeated clicks, hotkeys, or waits on an unchanged screen waste the run.
* Try the existing authenticated session first. Select Calle's already signed-in
  Google account and continue through normal SSO/account-chooser screens;
  clicking an existing account or Continue is not entering a credential.
* Calle explicitly authorizes you to retrieve verification codes sent to his
  already signed-in Gmail. Open Gmail, read the newest matching code, return to
  the requesting site, type it, and continue without asking him to relay it.
  Do not stop after merely opening the email.
* NEVER type or ask for a password, card number or account-recovery secret. Use
  status "handoff" only when password entry, manual authenticator/push approval,
  a captcha, payment details, recovery secrets, or missing account access
  genuinely prevents progress. Say exactly what Calle must do on the same screen.
* Stop and answer "ask" when the task is ambiguous in a way that changes what
  you would do.
* You have been here before. The first step shows what you learned on this
  screen and how your recent runs went — read it before you plan, and do not
  walk back into a dead end you already recorded.
* When you learn something that would have saved this run — where a control
  actually lives, which account is the right one, a route that does not work —
  call `remember` with it. That is the only thing that survives to your next
  run, so write it as an instruction to yourself, not as a diary entry.
* Calle can add instructions while you work. They arrive as "Calle added:" in
  the history and as CALLE JUST SAID. Treat the newest one as the current
  brief: it corrects or replaces what you were doing. Act on it immediately
  rather than finishing your previous plan first.
* Nothing is done because you clicked the button that does it. Before `finish`
  with "done", the screenshot in front of you must show the result — the page
  loaded, the value saved, the message sent. If it does not, keep working or
  say plainly what you could not confirm.
* If Calle tells you to stop, call `finish` with status "stopped" immediately.
  Do not turn a stop instruction into a question or handoff.
* When the task is finished, call `finish` with status "done" and put the ANSWER in `message` — any
  value you were sent to find (a price, a name, a code, a status) must appear
  there literally, because that text is what gets reported back.
* Do not do anything the task did not ask for. No installing, no sending, no
  deleting, no purchases.
"""


def _tool(name: str, description: str, properties: dict, required: list[str],
          *, visible_thought: bool = False) -> dict:
    props = dict(properties)
    needed = list(required)
    if visible_thought:
        props = {
            "thought": {
                "type": "string",
                "description": "One concise plain sentence: visible state, target, and expected result. No Markdown or generic planning label.",
            },
            **props,
        }
        needed = ["thought", *needed]
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": props,
                           "required": needed}}


def _action_tool(name: str, description: str, properties: dict,
                 required: list[str]) -> dict:
    return _tool(name, description, properties, required, visible_thought=True)


COORD = {"type": "integer", "minimum": 0}
ACTION_TOOLS = [
    _action_tool("click", "Click a visible point on the current screenshot.",
          {"x": COORD, "y": COORD,
           "button": {"type": "string", "enum": ["left", "right", "middle"]},
           "clicks": {"type": "integer", "minimum": 1, "maximum": 2}}, ["x", "y"]),
    _action_tool("move_pointer", "Move the pointer without clicking, for hover menus, tooltips, previews, and revealing hidden controls.",
          {"x": COORD, "y": COORD}, ["x", "y"]),
    _action_tool("type_text", "Click the visible text control at x,y, focus it, then type text. Use the accessibility control center when one is listed.",
          {"x": COORD, "y": COORD, "text": {"type": "string"},
           "replace": {"type": "boolean", "description": "Replace the field's current text instead of inserting at the clicked position."}},
          ["x", "y", "text"]),
    _action_tool("key", "Press a keyboard key one or more times.",
          {"key": {"type": "string"},
           "presses": {"type": "integer", "minimum": 1, "maximum": 20}}, ["key"]),
    _action_tool("hotkey", "Press a keyboard shortcut such as ctrl+l.",
          {"keys": {"type": "array", "items": {"type": "string"}, "minItems": 1}}, ["keys"]),
    _action_tool("scroll", "Scroll at a visible point; positive dy scrolls down and negative dy scrolls up.",
          {"x": COORD, "y": COORD,
           "dy": {"type": "integer", "minimum": -25, "maximum": 25}}, ["x", "y", "dy"]),
    # The loop could always perform a drag; until now the model had no way to
    # ask for one, which put sliders, range pickers, reordering and canvas work
    # out of reach.
    _action_tool("drag", "Press at one point, move, and release at another. For sliders, "
                  "range handles, reordering lists and drawing.",
          {"from": {"type": "array", "items": COORD, "minItems": 2, "maxItems": 2},
           "to": {"type": "array", "items": COORD, "minItems": 2, "maxItems": 2},
           "button": {"type": "string", "enum": ["left", "right", "middle"]}},
          ["from", "to"]),
    _action_tool("draw_path", "Press and draw one continuous freehand path through the supplied screen points, then release.",
          {"points": {"type": "array", "items": {
               "type": "array", "items": COORD, "minItems": 2, "maxItems": 2},
               "minItems": 2, "maxItems": 80},
           "button": {"type": "string", "enum": ["left", "right", "middle"]}},
          ["points"]),
    _action_tool("select_all_text", "Select everything in the focused field, so the next "
                             "type_text replaces it instead of appending.",
          {}, []),
    _action_tool("open_url", "Open a URL in the persistent Chrome profile. This is the preferred, reliable way to navigate to any named site.",
          {"url": {"type": "string"}}, ["url"]),
    _action_tool("wait", "Wait briefly for the desktop or page to settle.",
          {"seconds": {"type": "number", "minimum": 0.1, "maximum": 10}}, ["seconds"]),
    _action_tool("wait_for_screen", "Wait until the visible desktop changes or remains stable. Use this after navigation, submission, app launch, or animation when readiness matters more than a guessed delay.",
          {"condition": {"type": "string", "enum": ["change", "stable"]},
           "timeout": {"type": "number", "minimum": 0.5, "maximum": 20},
           "stable_for": {"type": "number", "minimum": 0.2, "maximum": 3}},
          ["condition", "timeout"]),
    _action_tool("launch_app", "Launch a GUI application on the real desktop.",
          {"command": {"type": "string"}}, ["command"]),
    _action_tool("shell", "Run a bounded shell command on this Linux machine.",
          {"command": {"type": "string"}}, ["command"]),
    _action_tool("remember", "Keep something for your future runs on this screen: where a "
                      "control lives, which account is the right one, what did not "
                      "work. You are shown these at the start of every run.",
          {"key": {"type": "string",
                   "description": "Short stable slug, e.g. 'spotify-artist-switcher'."},
           "value": {"type": "string",
                     "description": "The lesson, in one or two sentences."}},
          ["key", "value"]),
    _action_tool("finish", "Finish, stop, ask Calle, hand off for credentials, or report failure.",
          {"status": {"type": "string", "enum": ["done", "stopped", "ask", "handoff", "fail"]},
           "message": {"type": "string"}}, ["status", "message"]),
]

PROGRESS_REVIEW_PROMPT = """You are reviewing your own aiOS OPERATOR run at a
periodic checkpoint. Do not interact with the computer. Compare the checkpoint
screen with the current screen and read the complete action trace for this
interval.

Call `continue_work` only when the task made meaningful, observable progress:
navigation toward the goal, a blocker removed, a required value entered, or a
result reached. Animation, waiting, refocusing, and repeated attempts on an
unchanged screen are not progress. If the run is stuck, call `stop_stuck` and
state the concrete visible issue plus what kept failing. Never continue merely
because another click might work.
"""

PROGRESS_REVIEW_TOOLS = [
    _tool("continue_work", "Meaningful task progress occurred during this interval.",
          {"summary": {"type": "string"},
           "next_approach": {"type": "string"}}, ["summary", "next_approach"]),
    _tool("stop_stuck", "No meaningful progress occurred; stop with the concrete issue.",
          {"issue": {"type": "string"},
           "attempts": {"type": "string"}}, ["issue", "attempts"]),
]


def task_message(task: str, width: int, height: int, history: str,
                 windows: list[str] | None = None, feedback: str = "",
                 notes: list[str] | None = None, step: int = 0,
                 background: str = "",
                 controls: list[dict] | None = None) -> str:
    lines = [f"TASK: {task}", "", f"SCREEN: {width}x{height} pixels."]
    if step:
        lines.append(f"STEP: {step}")
    if background:
        # Only on the first step: after that it is in the history, and repeating
        # it every step would push the screenshot out of the model's attention.
        lines += ["", background]
    if windows:
        lines += ["", "OPEN WINDOWS: " + " | ".join(windows[:8])]
    if controls:
        lines += ["", "VISIBLE ACCESSIBLE CONTROLS (use these real screen bounds when they match the screenshot):"]
        for row in controls[:60]:
            x = int(row.get("x") or 0)
            y = int(row.get("y") or 0)
            control_width = int(row.get("width") or 0)
            control_height = int(row.get("height") or 0)
            center_x = x + control_width // 2
            center_y = y + control_height // 2
            name = str(row.get("name") or "").replace("\n", " ")[:120]
            role = str(row.get("role") or "control")
            focused = " focused" if row.get("focused") else ""
            lines.append(
                f'- {role} {name!r}: center=({center_x},{center_y}), bounds=({x},{y},{control_width},{control_height}){focused}')
    if history:
        lines += ["", "HISTORY:", history]
    if feedback:
        lines += ["", "ACTION FEEDBACK:", feedback]
    if notes:
        # Last, so it is the freshest thing in the prompt, and marked as an
        # instruction rather than context.
        lines += ["", "CALLE JUST SAID — this updates your task, follow it now:"]
        lines += [f"- {note}" for note in notes[-5:]]
    lines += ["", "State where you are, what you are aiming at, and what the next "
                  "screenshot should show. Then call exactly one action tool."]
    return "\n".join(lines)


def progress_review_message(task: str, checkpoint_step: int, current_step: int,
                            history: str) -> str:
    return "\n".join([
        f"TASK: {task}",
        "",
        f"Review the work from step {checkpoint_step} through step {current_step}.",
        "The first attached image is the checkpoint screen. The second attached image is the current screen.",
        "",
        "ACTIONS DURING THIS INTERVAL:",
        history or "(no recorded actions)",
        "",
        "Decide whether meaningful, observable progress was made. Call exactly one review tool.",
    ])
