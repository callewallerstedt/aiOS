"""The suite that measures the cheap end of the loop.

Every other suite here asks a hard question and is happy to pay for the answer.
This one asks the *easy* question -- "darken this grey", "sort by the other
field", "print seconds under a minute" -- because that is where the harness was
actually losing. Three real sessions spent 40, 47 and 172 tool rounds on changes
of exactly this size, and the operator killed the longest one.

So the fixtures are deliberately shaped like the place that went wrong:

* **bulk the agent must not read.** Each repo carries a few hundred lines of
  plausible CSS and JS around the four lines that matter. A run that reads whole
  files instead of the range its own search pointed at pays for it in tokens,
  visibly, which is the number we are trying to move.
* **a brief a competent colleague would call one-line.** No ambiguity to
  discover, nothing to design. If the harness spends twenty rounds here, the
  rounds are the bug.
* **a nearby thing that must not change.** Every task freezes something one
  careless `replace_all` away from the edit. Cheap is only good if it is right.

Pass rate is the gate; `tool_calls` and `tokens_per_pass` are the measurement.
"""

from __future__ import annotations

from .suites import Task

# --------------------------------------------------------------------- filler

# Bulk exists so that "read the whole file" has a price. It is generated rather
# than typed out because three hundred lines of decorative CSS in the source
# would bury the four lines each task is actually about.


def _css_filler(sections: int) -> str:
    blocks = []
    for index in range(sections):
        blocks.append(f"""\
.panel-{index} {{
  display: flex;
  align-items: center;
  gap: {6 + index % 5}px;
  padding: {4 + index % 3}px {8 + index % 7}px;
  border-radius: {4 + index % 4}px;
  background: var(--surface);
  color: var(--text);
}}
.panel-{index}:hover {{ background: var(--surface-hover); }}
.panel-{index} .label {{ font-size: {11 + index % 3}px; letter-spacing: 0.01em; }}
.panel-{index} .value {{ font-variant-numeric: tabular-nums; opacity: 0.8; }}
""")
    return "\n".join(blocks)


def _js_filler(count: int) -> str:
    blocks = []
    for index in range(count):
        blocks.append(f"""\
export function formatPanel{index}(row) {{
  const label = String(row.label || "panel {index}");
  const value = Number(row.value || 0);
  const tone = value > {index * 3} ? "high" : "low";
  const detail = [label, value.toFixed(2), tone].join(" \\u00b7 ");
  return {{ label, value, tone, detail }};
}}
""")
    return "\n".join(blocks)


# A test suite that exists, passes, takes a few seconds, and has nothing to do
# with any of these briefs. Sessions ran the real repo's whole suite three times
# over for a CSS edit; without a suite here that behaviour cannot be measured.
_SUITE_FILES = {
    "tests/test_panels.py": '''\
"""Slow-ish and entirely unrelated to any brief in this repository."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _work():
    total = 0
    for index in range(240000):
        total += index % 7
    return total


def test_panel_arithmetic_is_stable():
    assert _work() == _work()


def test_panel_arithmetic_is_positive():
    time.sleep(0.4)
    assert _work() > 0


def test_panels_round_trip():
    time.sleep(0.4)
    assert _work() % 1 == 0


if __name__ == "__main__":
    started = time.time()
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print(f"all tests passed in {time.time() - started:.1f}s")
''',
    "pytest.ini": "[pytest]\ntestpaths = tests\n",
}


# ------------------------------------------------------------ shared CSS repo


_THEME = """\
:root {
  --bg: #0d0f12;
  --surface: #14171c;
  --surface-hover: #191d23;
  --text: #e6e9ef;
  --muted: #8a8f98;
  --accent: #6ea8fe;
  --success: #35c46a;
  --warning: #e0a33c;
  --danger: #e0574c;
  --ease: cubic-bezier(0.2, 0.7, 0.3, 1);
}

@keyframes pulse {
  0%   { opacity: 1; }
  50%  { opacity: 0.45; }
  100% { opacity: 1; }
}
"""

_SIDEBAR_CSS = """\
.sidebar { width: 260px; overflow-y: auto; border-right: 1px solid #20242b; }
.session-row { display: flex; align-items: center; gap: 8px; padding: 6px 10px; }
.session-row:hover { background: var(--surface-hover); }
.session-row .title { font-size: 13px; color: var(--text); }
.session-row .when { font-size: 11px; color: var(--muted); }
.session-row .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
.session-row .dot.running { background: var(--success); animation: pulse 1.6s var(--ease) infinite; }
.session-row .dot.waiting { background: var(--warning); }
.session-row .dot.error { background: var(--danger); }
.session-row .dot.read { background: #8a8f98; }
.session-row .dot.unread { background: #b6bcc6; }
"""


_DOT_BRIEF = """\
In the sidebar the session dots are wrong in two small ways.

The dot for a session you have already read should be hollow -- no fill at all,
just a thin 1.5px border in the same grey it uses now. And the unread dot's grey
is far too light against the dark background; darken it noticeably.

Both live in `web/css/app.css`. Leave the running, waiting and error dots exactly
as they are.
"""

_DOT_FILES = {
    "README.md": """\
# console

A small operations console. The sidebar lists sessions; the main pane shows one.

    web/css/app.css     all styling
    web/js/sessions.js  the sidebar list
    web/index.html      the shell
""",
    "web/index.html": """\
<!doctype html>
<meta charset="utf-8">
<link rel="stylesheet" href="css/app.css">
<div class="sidebar" id="sessions"></div>
<main class="pane" id="pane"></main>
<script type="module" src="js/sessions.js"></script>
""",
    "web/css/app.css": _THEME + "\n" + _SIDEBAR_CSS + "\n" + _css_filler(110),
    "web/js/sessions.js": """\
const state = { rows: [] };

export function render(rows) {
  state.rows = rows;
  const host = document.getElementById("sessions");
  host.replaceChildren(...rows.map(rowElement));
  return host;
}

function rowElement(row) {
  const el = document.createElement("div");
  el.className = "session-row";
  const dot = document.createElement("span");
  dot.className = `dot ${row.status} ${row.seen ? "read" : "unread"}`;
  el.append(dot);
  return el;
}

""" + _js_filler(70),
    **_SUITE_FILES,
}

# A rule's body, by selector. Written as one helper because all three checks
# below need "what does this selector actually declare now".
_CSS_HELPERS = '''
import re

CSS = open(WORKSPACE + "/web/css/app.css", encoding="utf-8").read()


def rule(selector):
    """The declaration block for an exact selector, or '' if it is gone."""
    for match in re.finditer(r"([^{}]+)\\{([^{}]*)\\}", CSS):
        if match.group(1).strip() == selector:
            return match.group(2).strip()
    return ""


def declaration(selector, prop):
    """One property's value inside a selector's block, or '' if absent."""
    body = rule(selector)
    found = re.search(rf"(?:^|;)\\s*{prop}\\s*:([^;]+)", body)
    return found.group(1).strip() if found else ""


def luminance(value):
    """Rough perceived brightness of a #rrggbb value, 0-255."""
    found = re.search(r"#([0-9a-fA-F]{6})", value or "")
    assert found, f"expected a #rrggbb colour, got {value!r}"
    raw = found.group(1)
    r, g, b = (int(raw[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b
'''

_DOT_CHECKS = _CSS_HELPERS + '''

@case("the read dot is hollow")
def _():
    body = rule(".session-row .dot.read")
    assert body, "the .dot.read rule is gone"
    fill = declaration(".session-row .dot.read", "background")
    assert fill in ("", "none", "transparent"), f"still filled: background is {fill!r}"


@case("the read dot has a thin border in the same grey")
def _():
    border = declaration(".session-row .dot.read", "border")
    assert border, "no border, so nothing is visible where the fill used to be"
    assert "1.5px" in border, f"expected a 1.5px border, got {border!r}"
    assert "#8a8f98" in border.lower(), f"expected the original grey, got {border!r}"


@case("the unread grey got darker")
def _():
    fill = declaration(".session-row .dot.unread", "background")
    assert fill, "the .dot.unread background is gone"
    after = luminance(fill)
    before = luminance("#b6bcc6")
    assert after < before - 12, f"{fill!r} is not noticeably darker than #b6bcc6"


@case("the status dots were left alone")
def _():
    assert rule(".session-row .dot.running") == (
        "background: var(--success); animation: pulse 1.6s var(--ease) infinite;"
    ), "the running dot changed"
    assert rule(".session-row .dot.error") == "background: var(--danger);", "the error dot changed"
    assert rule(".session-row .dot.waiting") == "background: var(--warning);", "the waiting dot changed"


@case("the rest of the stylesheet is intact")
def _():
    assert CSS.count("{") > 150, "large parts of the stylesheet went missing"
    assert rule(".panel-39 .value"), "unrelated rules were dropped"
'''


# --------------------------------------------------------------- sort order


_SORT_BRIEF = """\
The session list jumps around while you are reading it: a session that updates
in the background hops to the top.

Sort the list by when each session was **created** instead -- newest first -- so
the order stays put. The relative time under each title must keep showing last
activity, not creation time.

The list lives in `web/js/sessions.js`.
"""

_SORT_FILES = {
    "README.md": "# console\n\nSidebar session list.\n\n    web/js/sessions.js\n",
    "web/js/sessions.js": """\
const MINUTE = 60;

// Rows arrive from /api/sessions. Both timestamps are always present; the list
// has simply never used created_at for anything.
export function normalise(payload) {
  return (payload.sessions || []).map((row) => ({
    id: String(row.id),
    title: String(row.title || "untitled"),
    created_at: Number(row.created_at || 0),
    updated_at: Number(row.updated_at || row.created_at || 0),
  }));
}

export function sortSessions(rows) {
  return rows.slice().sort((a, b) => b.updated_at - a.updated_at);
}

export function subtitle(row) {
  return `${relativeTime(row.updated_at)} ago`;
}

export function relativeTime(stamp) {
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - stamp));
  if (seconds < MINUTE) return `${seconds}s`;
  return `${Math.floor(seconds / MINUTE)}m`;
}

""" + _js_filler(80),
    "web/css/app.css": _THEME + "\n" + _SIDEBAR_CSS + "\n" + _css_filler(90),
    **_SUITE_FILES,
}

_SORT_CHECKS = '''
import re

JS = open(WORKSPACE + "/web/js/sessions.js", encoding="utf-8").read()


def body_of(name):
    """The source of one exported function, brace-matched."""
    start = JS.find(f"export function {name}(")
    assert start >= 0, f"{name} is gone"
    depth = 0
    for index in range(JS.index("{", start), len(JS)):
        depth += (JS[index] == "{") - (JS[index] == "}")
        if depth == 0:
            return JS[start:index + 1]
    raise AssertionError(f"{name} is not brace-balanced")


@case("the sort compares creation time")
def _():
    source = body_of("sortSessions")
    assert source.count("created_at") >= 2, f"still not sorting on created_at: {source!r}"


@case("the sort no longer compares last activity")
def _():
    assert "updated_at" not in body_of("sortSessions"), "updated_at is still in the comparator"


@case("newest is still first")
def _():
    source = body_of("sortSessions")
    order = re.search(r"sort\\(\\s*\\(\\s*(\\w+)\\s*,\\s*(\\w+)\\s*\\)\\s*=>\\s*(\\w+)\\.created_at", source)
    assert order, f"could not read the comparator: {source!r}"
    first, _second, leading = order.groups()
    assert leading != first, "ascending: oldest would sort to the top"


@case("the subtitle still shows last activity")
def _():
    source = body_of("subtitle")
    assert "updated_at" in source, "the '2m ago' line was switched to creation time"
    assert "created_at" not in source, "the subtitle now reports creation time"


@case("normalise still carries both timestamps")
def _():
    source = body_of("normalise")
    assert "created_at: Number(row.created_at || 0)" in source, "the payload mapping changed"
    assert "updated_at: Number(row.updated_at || row.created_at || 0)" in source, "the payload mapping changed"


@case("relativeTime was left alone")
def _():
    assert "const seconds = Math.max(0, Math.floor(Date.now() / 1000 - stamp));" in body_of("relativeTime")
'''


# -------------------------------------------------------------- elapsed label


_ELAPSED_BRIEF = """\
`console/status.py` renders how long a session has been running. Under a minute
it currently says `0m`, which is useless.

Show whole seconds instead for anything under a minute -- `42s`, `0s` -- and
keep the existing minute formatting from one minute upward.
"""

_ELAPSED_FILES = {
    "README.md": "# console\n\n    console/status.py   the status line\n",
    "console/__init__.py": "",
    "console/status.py": '''\
"""The one-line status shown under each session title."""

MINUTE = 60
HOUR = 3600


def format_elapsed(seconds):
    """How long a session has been running, as a short human label."""
    seconds = max(0, int(seconds))
    if seconds < HOUR:
        return f"{seconds // MINUTE}m"
    return f"{seconds // HOUR}h{(seconds % HOUR) // MINUTE:02d}m"


def format_tokens(count):
    """Token counts, abbreviated once they get long."""
    count = max(0, int(count))
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1000:.1f}k"
    return f"{count / 1_000_000:.2f}M"


def status_line(session):
    """`running · 4m · 12.3k` for the sidebar."""
    parts = [str(session.get("state") or "idle")]
    if session.get("elapsed"):
        parts.append(format_elapsed(session["elapsed"]))
    if session.get("tokens"):
        parts.append(format_tokens(session["tokens"]))
    return " \\u00b7 ".join(parts)
''',
    "console/panels.py": '"""Unrelated bulk."""\n\n' + "\n".join(
        f"def panel_{index}(row):\n"
        f"    label = str(row.get('label') or 'p{index}')\n"
        f"    value = float(row.get('value') or 0)\n"
        f"    return {{'label': label, 'value': value, 'tone': 'high' if value > {index} else 'low'}}\n"
        for index in range(120)
    ),
    **_SUITE_FILES,
}

_ELAPSED_CHECKS = '''
from console.status import format_elapsed, format_tokens, status_line


@case("under a minute reads in seconds")
def _():
    assert format_elapsed(42) == "42s"
    assert format_elapsed(1) == "1s"
    assert format_elapsed(59) == "59s"


@case("zero is zero seconds, not zero minutes")
def _():
    assert format_elapsed(0) == "0s"


@case("the minute boundary belongs to minutes")
def _():
    assert format_elapsed(60) == "1m"
    assert format_elapsed(61) == "1m"
    assert format_elapsed(605) == "10m"


@case("hours still format the old way")
def _():
    assert format_elapsed(3600) == "1h00m"
    assert format_elapsed(7845) == "2h10m"


@case("the neighbouring helpers were not touched")
def _():
    assert format_tokens(999) == "999"
    assert format_tokens(12345) == "12.3k"
    assert status_line({"state": "idle"}) == "idle"


@case("the status line picks the new label up")
def _():
    assert status_line({"state": "running", "elapsed": 42, "tokens": 12345}) == "running \\u00b7 42s \\u00b7 12.3k"
'''


TWEAK_FIXTURES: tuple[Task, ...] = (
    Task(
        id="tweak/session-dot",
        suite="tweak",
        title="Hollow the read dot, darken the unread grey",
        brief=_DOT_BRIEF,
        files=_DOT_FILES,
        checks=_DOT_CHECKS,
    ),
    Task(
        id="tweak/sort-order",
        suite="tweak",
        title="Sort the sidebar by creation, not activity",
        brief=_SORT_BRIEF,
        files=_SORT_FILES,
        checks=_SORT_CHECKS,
    ),
    Task(
        id="tweak/elapsed-seconds",
        suite="tweak",
        title="Show seconds under a minute",
        brief=_ELAPSED_BRIEF,
        files=_ELAPSED_FILES,
        checks=_ELAPSED_CHECKS,
    ),
)
