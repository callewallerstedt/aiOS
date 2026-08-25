import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TRANSCRIPT = ROOT / "aios_ui" / "web" / "js" / "transcript.js"


def test_structured_question_state_and_tool_preview_behave_in_module():
    script = r"""
import { pathToFileURL } from 'node:url';
const { Transcript } = await import(pathToFileURL(process.argv[1]).href);

const questions = Transcript.prototype.normaliseQuestionEvent({
  questions: [
    { id: 'flavors', q: 'How many flavors?', type: 'radio', options: ['Three', 'Five'] },
    { id: 'mixins', q: 'Which mix-ins?', type: 'check', options: ['Chips', 'Sprinkles'] },
  ],
});
if (questions.length !== 2 || questions[1].type !== 'check') throw new Error('question normalization failed');
const card = { questions, qi: 1, answers: { 1: [0, 1] }, custom: { 1: 'Waffle bits' } };
const values = Transcript.prototype.questionValues.call({}, card, 1);
if (values.join('|') !== 'Chips|Sprinkles|Waffle bits') throw new Error(values.join('|'));

const replayCards = new Map([
  ['answered', { key: 'answered', sent: true }],
  ['legacy-pending', { key: 'legacy-pending', sent: false }],
]);
let settledKey = '';
Transcript.prototype.settleLatestQuestion.call({
  questionCards: replayCards,
  settleQuestion(key) { settledKey = key; },
}, { legacy_answer: ['yes'] });
if (settledKey !== 'legacy-pending') throw new Error(`legacy question stayed actionable: ${settledKey}`);

const summary = { textContent: '' };
const latestText = { textContent: '' };
const classes = new Set();
const preview = {
  hidden: true,
  classList: { toggle(name, on) { if (on) classes.add(name); else classes.delete(name); } },
  querySelector(selector) { return selector === '.tool-run-latest-text' ? latestText : null; },
};
const diffs = { hidden: true, innerHTML: '' };
const run = {
  cards: new Set(['one', 'two']), fileDiffs: new Map(),
  node: { querySelector(selector) {
    if (selector === '.tool-run-summary') return summary;
    if (selector === '.tool-run-latest') return preview;
    if (selector === '.tool-run-diffs') return diffs;
    throw new Error(selector);
  } },
};
const context = { cards: new Map([
  ['one', { updatedOrder: 1, state: { phase: 'completed', title: 'Read file', detail: 'old.py' } }],
  ['two', { updatedOrder: 2, state: { phase: 'started', title: 'Run command', command: 'pytest -q' } }],
]) };
Transcript.prototype.updateToolRun.call(context, run);
if (summary.textContent !== '2 tool calls') throw new Error(summary.textContent);
if (!latestText.textContent.startsWith('Active: Run command')) throw new Error(latestText.textContent);
if (!classes.has('active') || preview.hidden) throw new Error('active preview missing');
context.cards.get('two').state.phase = 'completed';
Transcript.prototype.updateToolRun.call(context, run);
if (!latestText.textContent.startsWith('Latest: Run command')) throw new Error(latestText.textContent);
if (classes.has('active')) throw new Error('settled preview still active');
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(TRANSCRIPT)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_question_card_and_collapsed_tool_preview_are_styled_and_wired():
    transcript = TRANSCRIPT.read_text(encoding="utf-8")
    code = (ROOT / "aios_ui" / "web" / "js" / "code.js").read_text(encoding="utf-8")
    styles = (ROOT / "aios_ui" / "web" / "css" / "code.css").read_text(encoding="utf-8")
    server = (ROOT / "aios_ui" / "server.py").read_text(encoding="utf-8")

    assert "normaliseQuestionEvent" in transcript
    assert "setTimeout(() =>" in transcript and "}, 480)" in transcript
    assert "approval-custom-row" in transcript
    assert "question_answers: payload.answers" in code
    assert 'question_answers=data.get("question_answers")' in server
    assert ".approval-card" in styles
    assert ".approval-dot.current" in styles
    assert ".tool-run-latest.active" in styles
