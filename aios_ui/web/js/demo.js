// Demo page for the aiOS agent-chat transcript.
//
// Feeds a scripted sequence of fake events through the REAL Transcript
// renderer (transcript.js) so every component the agent chat can draw is
// visible in one place for demo and debugging. Nothing here talks to the
// backend; it is pure front-end.

import { Transcript } from "./transcript.js";

const root = document.getElementById("demo-transcript");
const runBtn = document.getElementById("demo-run");
const resetBtn = document.getElementById("demo-reset");

let transcript = null;
let timer = null;
let since = 0;

function makeTranscript() {
  if (transcript) transcript.destroy();
  transcript = new Transcript(root, {
    isActive: () => false,
    onReveal: (path) => console.log("[demo] reveal", path),
    onReviewSuggest: (prompt) => console.log("[demo] review suggest", prompt),
    onReviewFix: (state) => console.log("[demo] review fix", state),
    onAnswer: (answers) => console.log("[demo] answer", answers),
  });
}

// ------------------------------------------------------------------ events

const now = Math.floor(Date.now() / 1000);

// A unified diff for the "files" tool card.
const SAMPLE_DIFF = [
  "--- a/aios_ui/web/js/demo.js",
  "+++ b/aios_ui/web/js/demo.js",
  "@@ -1,6 +1,8 @@",
  " import { Transcript } from \"./transcript.js\";",
  "+// A new comment line.",
  "+const DEMO = true;",
  " const root = document.getElementById(\"demo-transcript\");",
  " const runBtn = document.getElementById(\"demo-run\");",
  " const resetBtn = document.getElementById(\"demo-reset\");",
].join("\n");

// The full script, in the order it should appear.
const SCRIPT = [
  { kind: "user", text: "Show me every component you can render, please." },
  { kind: "status", text: "thinking" },
  {
    kind: "activity",
    activity_type: "thinking",
    phase: "started",
    title: "Thought through the approach",
    summary: "The user wants a visual inventory of every UI element the agent chat can draw. I will walk through the transcript renderer and show each component type with realistic sample content.",
    ts: now,
  },
  {
    kind: "activity",
    activity_type: "stage",
    phase: "started",
    title: "Plan",
    detail: "1. Render a user bubble\n2. Show a thinking block\n3. Draw a pipeline stage\n4. Emit tool cards (command, files, search)\n5. Ask a question\n6. Reply in markdown",
    ts: now,
  },
  {
    kind: "activity",
    activity_type: "tool",
    phase: "started",
    title: "Reading transcript.js",
    detail: "Reading the transcript renderer to inventory its components.",
    ts: now,
  },
  {
    kind: "activity",
    activity_type: "tool",
    phase: "completed",
    title: "Read transcript.js",
    detail: "Found the Transcript class and every component it renders.",
    ts: now,
  },
  {
    kind: "activity",
    activity_type: "command",
    phase: "completed",
    title: "Ran command",
    command: "python -m py_compile aios_ui/web/js/transcript.js",
    detail: "",
    ts: now,
  },
  {
    kind: "activity",
    activity_type: "files",
    phase: "completed",
    title: "Edited demo.js",
    detail: "Added a demo event script.",
    diff: SAMPLE_DIFF,
    ts: now,
  },
  {
    kind: "activity",
    activity_type: "tool",
    phase: "completed",
    title: "Searched the codebase",
    detail: "Found 3 matches for `loading-pixels`.",
    ts: now,
  },
  {
    kind: "question",
    question_id: "demo-q",
    question: "Which components should I highlight?",
    questions: [
      {
        id: "q1",
        q: "Which components should I highlight?",
        type: "radio",
        options: [
          { label: "Everything", description: "Show all cards, inputs and loaders." },
          { label: "Just tool calls", description: "Only the tool-card variants." },
          { label: "Questions only", description: "Only the approval cards." },
        ],
      },
      {
        id: "q2",
        q: "Any preference on the loading animation?",
        type: "radio",
        options: [
          { label: "Wave", description: "The default left-to-right wave." },
          { label: "Orbit", description: "A circular orbit." },
        ],
      },
    ],
    options: [],
    ts: now,
  },
  {
    kind: "assistant",
    text: "Here is everything the agent chat can render.\n\n- **User bubbles** and **assistant markdown** replies\n- **Thinking blocks** that expand on click\n- **Pipeline stages** for plans\n- **Tool cards** for commands, file edits and searches\n- **Approval cards** for questions\n- **Loading pixels** in five animation variants\n\n```js\nconst demo = true;\n```",
  },
  { kind: "provider_switch", text: "Handed off to a subagent for a second opinion." },
  {
    kind: "activity",
    activity_type: "tool",
    phase: "completed",
    title: "Subagent finished",
    detail: "The subagent reviewed the component list and confirmed nothing is missing.",
    ts: now,
  },
  { kind: "status", text: "All done." },
  { kind: "result", text: "Here is everything the agent chat can render.\n\n- **User bubbles** and **assistant markdown** replies\n- **Thinking blocks** that expand on click\n- **Pipeline stages** for plans\n- **Tool cards** for commands, file edits and searches\n- **Approval cards** for questions\n- **Loading pixels** in five animation variants\n\n```js\nconst demo = true;\n```" },
];

function feedScript() {
  clearTimeout(timer);
  since = 0;
  const step = () => {
    if (since >= SCRIPT.length) return;
    const batch = SCRIPT.slice(since, since + 1);
    since += 1;
    transcript.push(batch);
    timer = setTimeout(step, 350);
  };
  step();
}

// ------------------------------------------------------------------ wiring

runBtn.addEventListener("click", () => {
  makeTranscript();
  feedScript();
});

resetBtn.addEventListener("click", () => {
  clearTimeout(timer);
  makeTranscript();
});

// Kick off once on load.
makeTranscript();
feedScript();