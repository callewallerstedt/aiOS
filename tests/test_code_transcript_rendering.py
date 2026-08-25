import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# The native CODE chat has its own suite: tests/test_code_chat_ui.py.


def test_web_code_output_joins_consecutive_agent_deltas():
    script = r"""
const fs = require('fs');
const vm = require('vm');
class Node {
  constructor() { this.children = []; this._className = ''; this.scrollTop = 0; this.dataset = {}; this.open = false; this.textContent = ''; }
  set className(value) { this._className = value; }
  get className() { return this._className; }
  get classList() { return { contains: (name) => this._className.split(/\s+/).includes(name) }; }
  appendChild(child) { this.children.push(child); return child; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = [...children]; }
  setAttribute() {}
  addEventListener() {}
  scrollTo() {}
  get lastElementChild() { return [...this.children].reverse().find((child) => child instanceof Node) || null; }
  get scrollHeight() { return this.children.length; }
  querySelector(selector) {
    if (!selector.startsWith('.')) return null;
    const name = selector.slice(1);
    for (const child of this.children) {
      if (!(child instanceof Node)) continue;
      if (child.classList.contains(name)) return child;
      const nested = child.querySelector(selector);
      if (nested) return nested;
    }
    return null;
  }
}
const timeline = new Node();
global.location = { search: '', hostname: 'localhost', origin: 'http://localhost', href: 'http://localhost/code' };
global.localStorage = { getItem() { return null; }, setItem() {} };
global.document = {
  addEventListener() {},
  getElementById(id) { return id === 'timeline' ? timeline : new Node(); },
  createElement() { return new Node(); },
  createTextNode(text) { return { textContent: String(text) }; },
};
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
appendEvent({ kind: 'assistant_delta', delta: 'Hello' }, false);
appendEvent({ kind: 'assistant_delta', delta: ' there' }, false);
appendEvent({ kind: 'assistant_delta', delta: '.' }, false);
appendEvent({ kind: 'tool', text: '$ test' }, false);
appendEvent({ kind: 'assistant', text: 'Next' }, false);
appendEvent({ kind: 'assistant', text: ' message' }, false);
appendEvent({ kind: 'result', text: 'Hello there. Next message', notify: true }, false);
const text = (node) => node.children.map((child) => child instanceof Node ? text(child) : child.textContent).join('');
if (timeline.children.length !== 3) throw new Error(`expected 3 rows, got ${timeline.children.length}`);
if (text(timeline.children[0]) !== 'Hello there.') throw new Error(text(timeline.children[0]));
if (text(timeline.children[2]) !== 'Next message') throw new Error(text(timeline.children[2]));
"""
    result = subprocess.run(
        ["node", "-e", script, str(ROOT / "agent_clicker" / "app" / "static" / "coding.js")],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_web_provider_switch_event_is_a_clear_handoff_card():
    script = r"""
const fs = require('fs');
const vm = require('vm');
class Node {
  constructor() { this.children = []; this._className = ''; this.scrollTop = 0; this.clientHeight = 100; this.dataset = {}; this.textContent = ''; }
  set className(value) { this._className = value; }
  get className() { return this._className; }
  get classList() { return { contains: (name) => this._className.split(/\s+/).includes(name) }; }
  appendChild(child) { this.children.push(child); return child; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = [...children]; }
  setAttribute() {}
  addEventListener() {}
  scrollTo() {}
  get lastElementChild() { return [...this.children].reverse().find((child) => child instanceof Node) || null; }
  get scrollHeight() { return this.children.length; }
  querySelector() { return null; }
}
const timeline = new Node();
global.location = { search: '', hostname: 'localhost', origin: 'http://localhost', href: 'http://localhost/code' };
global.localStorage = { getItem() { return null; }, setItem() {} };
global.document = {
  addEventListener() {}, hidden: false,
  getElementById(id) { return id === 'timeline' ? timeline : new Node(); },
  createElement() { return new Node(); },
  createTextNode(text) { return { textContent: String(text) }; },
};
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
appendEvent({
  kind: 'provider_switch',
  text: 'Switched from Claude · sonnet to Codex · gpt-5.6-sol',
  from_provider: 'claude', from_model: 'sonnet',
  to_provider: 'codex', to_model: 'gpt-5.6-sol',
  native_continuation: false,
}, false);
const card = timeline.children[0];
if (!card.classList.contains('provider-switch')) throw new Error(card.className);
if (card.children[0].textContent !== 'HANDOFF') throw new Error(card.children[0].textContent);
if (card.children[1].children[0].textContent.indexOf('Claude') < 0) throw new Error('missing source');
if (card.children[1].children[1].textContent.indexOf('New native provider session') < 0) throw new Error('missing limitation');
"""
    result = subprocess.run(
        ["node", "-e", script, str(ROOT / "agent_clicker" / "app" / "static" / "coding.js")],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_web_command_phases_collapse_into_one_activity_card():
    script = r"""
const fs = require('fs');
const vm = require('vm');
class Node {
  constructor() { this.children = []; this._className = ''; this.scrollTop = 0; this.dataset = {}; this.open = false; this.textContent = ''; }
  set className(value) { this._className = value; }
  get className() { return this._className; }
  get classList() { return { contains: (name) => this._className.split(/\s+/).includes(name) }; }
  appendChild(child) { this.children.push(child); return child; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = [...children]; }
  setAttribute() {}
  addEventListener() {}
  scrollTo() {}
  get lastElementChild() { return [...this.children].reverse().find((child) => child instanceof Node) || null; }
  get scrollHeight() { return this.children.length; }
  querySelector(selector) {
    if (!selector.startsWith('.')) return null;
    const name = selector.slice(1);
    for (const child of this.children) {
      if (!(child instanceof Node)) continue;
      if (child.classList.contains(name)) return child;
      const nested = child.querySelector(selector);
      if (nested) return nested;
    }
    return null;
  }
}
const timeline = new Node();
global.location = { search: '', hostname: 'localhost', origin: 'http://localhost', href: 'http://localhost/code' };
global.localStorage = { getItem() { return null; }, setItem() {} };
global.document = {
  addEventListener() {},
  getElementById(id) { return id === 'timeline' ? timeline : new Node(); },
  createElement() { return new Node(); },
  createTextNode(text) { return { textContent: String(text) }; },
};
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
appendEvent({ kind: 'tool', text: '$ pytest -q' }, false);
appendEvent({ kind: 'tool', text: 'Running pytest -q' }, false);
appendEvent({ kind: 'tool', text: 'Ran pytest -q' }, false);
appendEvent({ kind: 'tool', text: '$ npm run build' }, false);
if (timeline.children.length !== 2) throw new Error(`expected 2 cards, got ${timeline.children.length}`);
"""
    result = subprocess.run(
        ["node", "-e", script, str(ROOT / "agent_clicker" / "app" / "static" / "coding.js")],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_code_dashboard_has_expandable_live_activity_and_diff_surfaces():
    html = (ROOT / "agent_clicker" / "app" / "templates" / "coding.html").read_text(encoding="utf-8")
    script = (ROOT / "agent_clicker" / "app" / "static" / "coding.js").read_text(encoding="utf-8")
    styles = (ROOT / "agent_clicker" / "app" / "static" / "coding.css").read_text(encoding="utf-8")

    assert 'id="run-strip"' in html
    assert 'id="activity-summary"' in html
    assert "function upsertActivity" in script
    assert "function renderDiff" in script
    assert "function renderMarkdown" in script
    assert "function timelineNearBottom" in script
    assert "FINAL REPORT" not in script
    assert "activity-card" in styles
    assert "activity-output" in styles
    assert "diff-line.add" in styles
    assert "markdown-table-wrap" in styles
    # Scroll anchoring stays on. Disabling it and restoring scrollTop by hand
    # was what made expanding a card jump the transcript.
    assert "overflow-anchor: none" not in styles
    assert "overscroll-behavior: contain" in styles
    # Tail-following is one piece of state the user's own scrolling owns, not a
    # per-event measurement that live re-renders could flip mid-turn.
    assert "function pinTimelineToTail" in script
    assert "function scrollTimelineToTail" in script
    assert "followTail" in script
    assert ".activity-card { flex: 0 0 auto" in styles
    assert ".event { flex: 0 0 auto" in styles
    assert "max-height: 330px" not in styles
    assert "max-height: 460px" not in styles


def test_turn_summary_row_is_short_and_reports_generation_rate():
    """The row named a provider and a 60-character tag and no speed at all."""
    from pathlib import Path as _Path

    js = (_Path(__file__).resolve().parent.parent
          / "aios_ui" / "web" / "js" / "transcript.js").read_text(encoding="utf-8")
    row = js.split("paintTurnUsage() {", 1)[1].split("finishTurnUsage", 1)[0]

    # Short labels, and the configuration's own name.
    assert "} in`" in row and "} out`" in row
    assert "} input`" not in row and "} output`" not in row
    assert "shortConfigName(row)" in row
    assert "row.provider || \"\"," not in row, "the provider prefix should be gone"
    assert "turn-usage-role" not in row, "the Coder label should be gone"

    # Measured rate, with the turn average beside the round when they differ.
    assert "rateLabel(roundRate, turnRate)" in row
    label = js.split("function rateLabel(", 1)[1].split("function shortConfigName", 1)[0]
    assert "tok/s" in label
    assert "(avg " in label
    assert "row.roundRate" in js and "row.turnRate" in js
    assert "state.round_tokens_per_second" in js and "state.turn_tokens_per_second" in js
