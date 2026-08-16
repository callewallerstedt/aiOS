// The transcript engine, shared by the CODE tab and the BENCH page.
//
// It was born inside code.js and moved out whole when BENCH needed to show a
// benchmark session. A benchmark run *is* a CODE session -- same providers,
// same tools, same event stream -- so showing it any other way would have meant
// a second renderer drifting away from the first one.
//
// Three ideas carry the smoothness, all of them still true here:
//
//  1. Push, not poll. The caller feeds events in from an SSE stream; nothing
//     asks the backend whether anything happened.
//  2. One rAF writer. Incoming frames mutate state only; the DOM is touched
//     once per animation frame, so a provider dumping 200 events in a burst
//     still costs exactly one layout.
//  3. Bounded re-parse. Streaming assistant text only re-renders the unsettled
//     tail; everything above the last blank line is parsed once and kept.

import { renderMarkdown, escapeHtml } from "./markdown.js";

/** Session statuses that mean something is still happening. */
export const ACTIVE = new Set(["queued", "running", "waiting_user"]);

const PHASE_MAP = {
  complete: "completed", success: "completed", succeeded: "completed", done: "completed",
  error: "failed", declined: "failed", cancelled: "failed", canceled: "failed",
  in_progress: "started", running: "started", pending: "started",
};

// These are the same inline primitives used by the reference components:
// stroked SVG controls, circular task badges, and the compact tool glyphs.
const CHEVRON_ICON = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6" /></svg>';
const CHECK_ICON = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5" /></svg>';
const X_ICON = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12" /></svg>';
const SPARK_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l2.4 7.2L22 12l-7.6 2.8L12 22l-2.4-7.2L2 12l7.6-2.8z" /></svg>';
// Exact inline SVGs from aicss.dev/components/task-list and /file-diff.
const TODO_LIST_ICON = '<svg class="todoListIcon" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M8.25 6.75h12M8.25 12h12m-12 5.25h12M3.75 6.75h.007v.008H3.75V6.75Zm.375 0a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0ZM3.75 12h.007v.008H3.75V12Zm.375 0a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Zm-.375 5.25h.007v.008H3.75v-.008Zm.375 0a.375.375 0 1 1-.75 0 .375.375 0 0 1 .75 0Z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" /></svg>';
const TODO_CHEVRON_ICON = '<svg class="todoChevron" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="m19.5 8.25-7.5 7.5-7.5-7.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" /></svg>';
const TODO_HEAD_CHECK_ICON = '<svg class="todoHeadCheck" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path fill-rule="evenodd" clip-rule="evenodd" d="M2.25 12c0-5.385 4.365-9.75 9.75-9.75s9.75 4.365 9.75 9.75-4.365 9.75-9.75 9.75S2.25 17.385 2.25 12Zm13.36-1.814a.75.75 0 1 0-1.22-.872l-3.236 4.53L9.53 12.22a.75.75 0 0 0-1.06 1.06l2.25 2.25a.75.75 0 0 0 1.14-.094l3.75-5.25Z" fill="currentColor" /></svg>';
const TODO_CHECK_ICON = '<svg class="todoIcon" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M9 12.75 11.25 15 15 9.75M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" /></svg>';
const TODO_ARROW_ICON = '<svg class="todoIcon strong" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="m12.75 15 3-3m0 0-3-3m3 3h-7.5M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" /></svg>';
const TODO_DASHED_ICON = '<svg class="todoIcon" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="1.8" stroke-dasharray="1.8 3.6" stroke-linecap="round" /></svg>';
const CODE_FILE_ICON = '<svg class="diffIcon" viewBox="0 0 24 24" width="15" height="15" aria-hidden="true"><path d="M17.25 6.75 22.5 12l-5.25 5.25m-10.5 0L1.5 12l5.25-5.25m7.5-3-4.5 16.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" /></svg>';

const TOOL_ICONS = {
  think: '<path d="M12 2l2.4 7.2L22 12l-7.6 2.8L12 22l-2.4-7.2L2 12l7.6-2.8z" />',
  write: '<g fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z" /></g>',
  run: '<g fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 17l6-5-6-5M12 19h8" /></g>',
  read: '<g fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" /></g>',
  search: '<g fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7" /><path d="M21 21l-4.3-4.3" /></g>',
};

function toolIconMarkup(type) {
  const key = type === "command" ? "run" : type === "files" ? "write" : type === "read" ? "read" : type === "thinking" ? "think" : type === "search" ? "search" : "run";
  return `<svg width="13" height="13" viewBox="0 0 24 24" fill="${key === "think" ? "currentColor" : "none"}" stroke="currentColor">${TOOL_ICONS[key]}</svg>`;
}

export function fileDiffSummary(change) {
  const path = String(change?.path || "");
  const diff = String(change?.diff || "");
  const lines = diff.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n");
  const kindValue = change?.change_kind;
  const kind = String(kindValue && typeof kindValue === "object" ? kindValue.type : kindValue || "").toLowerCase();
  let add = lines.filter((line) => line.startsWith("+") && !line.startsWith("+++")).length;
  let del = lines.filter((line) => line.startsWith("-") && !line.startsWith("---")).length;
  // Added/deleted files are sometimes delivered as raw file contents rather
  // than a unified diff. Count those lines according to the structured change
  // kind so a real deletion never becomes a misleading +0/-0 chip.
  const unified = lines.some((line) => line.startsWith("@@"))
    || (lines.some((line) => line.startsWith("--- ")) && lines.some((line) => line.startsWith("+++ ")));
  const contentLines = diff ? lines.length - (lines.at(-1) === "" ? 1 : 0) : 0;
  if (!unified && kind.includes("add")) {
    add = contentLines;
    del = 0;
  }
  if (!unified && (kind.includes("delete") || kind.includes("remove"))) {
    add = 0;
    del = contentLines;
  }
  return {
    path,
    name: path.split(/[\\/]/).pop() || path,
    add,
    del,
    diff: String(change?.diff || ""),
  };
}

function normalizeReasoningText(value) {
  return String(value || "")
    .replace(/\r\n?/g, "\n")
    // Old sessions can contain a provider transport separator between almost
    // every token. Keep normal paragraphs, but turn 3+ blank lines into one
    // space so the archived trace is readable on phone and desktop.
    .replace(/\n(?:[ \t]*\n){2,}[ \t]*/g, " ");
}

function diffRows(change) {
  const source = String(change?.diff || "").replace(/\r\n?/g, "\n");
  const lines = source.split("\n");
  const rows = [];
  let oldLine = 1;
  let newLine = 1;
  let sawHunk = false;
  for (const line of lines) {
    const hunk = line.match(/^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@/);
    if (hunk) {
      oldLine = Number(hunk[1]);
      newLine = Number(hunk[2]);
      sawHunk = true;
      continue;
    }
    if (/^(?:---|\+\+\+)\s/.test(line) || line === "\\ No newline at end of file") continue;
    if (!sawHunk && !line) continue;
    if (line.startsWith("+")) {
      rows.push({ old: null, cur: newLine++, type: "add", text: line.slice(1) });
    } else if (line.startsWith("-")) {
      rows.push({ old: oldLine++, cur: null, type: "del", text: line.slice(1) });
    } else {
      rows.push({ old: oldLine++, cur: newLine++, type: "ctx", text: line.startsWith(" ") ? line.slice(1) : line });
    }
  }
  return rows.slice(0, 240);
}

function fileDiffMarkup(change) {
  const rows = diffRows(change);
  const added = rows.filter((row) => row.type === "add").length;
  const removed = rows.filter((row) => row.type === "del").length;
  return `<div class="aicss-diff diff" data-reveal="${escapeHtml(change.path)}" title="${escapeHtml(change.path)}">
    <div class="diffHead">
      <span class="diffFileWrap">${CODE_FILE_ICON}<span class="diffFile">${escapeHtml(change.name)}</span></span>
      <span class="diffStat"><span class="add">+${added.toLocaleString()}</span><span class="del">-${removed.toLocaleString()}</span></span>
    </div>
    <div class="diffBody">${rows.map((row) => `<div class="diffRow ${row.type}"><span class="ln old">${row.old ?? ""}</span><span class="ln new">${row.cur ?? ""}</span><span class="sign">${row.type === "add" ? "+" : row.type === "del" ? "-" : ""}</span><code>${escapeHtml(row.text)}</code></div>`).join("")}</div>
  </div>`;
}

function updateRollingCount(node, value) {
  const next = String(value || "0/0");
  const previous = String(node.dataset.value || next);
  node.dataset.value = next;
  if (!node.firstChild) {
    node.innerHTML = `<span class="rollCount">${[...next].map((char) => `<span class="rollDigit">${escapeHtml(char)}</span>`).join("")}</span>`;
    return;
  }
  if (previous === next) return;
  const width = Math.max(previous.length, next.length);
  node.innerHTML = `<span class="rollCount">${Array.from({ length: width }, (_, index) => {
    const from = previous[index] || "";
    const to = next[index] || "";
    if (from === to) return `<span class="rollDigit">${escapeHtml(to)}</span>`;
    return `<span class="rollDigit"><span class="rollInner"><span>${escapeHtml(from)}</span><span>${escapeHtml(to)}</span></span></span>`;
  }).join("")}</span>`;
  requestAnimationFrame(() => requestAnimationFrame(() =>
    node.querySelectorAll(".rollInner").forEach((inner) => inner.classList.add("on"))));
  setTimeout(() => {
    if (node.dataset.value !== next) return;
    node.innerHTML = `<span class="rollCount">${[...next].map((char) => `<span class="rollDigit">${escapeHtml(char)}</span>`).join("")}</span>`;
  }, 380);
}

// 3x3 pixel animations. Timing lives in CSS rather than inline delays so a
// variant can retime every cell, and so the count can never drift from the
// nine grid cells -- the delay list had grown a tenth entry, which rendered a
// stray dot outside the 3x3 grid. `wave` is the default because the working
// row drives its label and clock off the same period, making the highlight
// read as one wave travelling left to right through the whole row.
export const PIXEL_ANIMATIONS = ["wave", "pulse", "orbit", "rain", "bloom"];

export function pixelAnimation() {
  let choice = "";
  try {
    choice = String(window.localStorage.getItem("aiosPixelAnim") || "");
  } catch (err) {
    choice = "";
  }
  return PIXEL_ANIMATIONS.includes(choice) ? choice : "wave";
}

function loadingPixelsMarkup(variant = "") {
  const anim = PIXEL_ANIMATIONS.includes(variant) ? variant : pixelAnimation();
  return `<span class="loading-pixels anim-${anim}" aria-hidden="true">${"<span></span>".repeat(9)}</span>`;
}

function taskBadgeMarkup(phase, role = "") {
  const normalized = String(phase || "").toLowerCase();
  if (normalized === "started" || normalized === "update") {
    return `<span class="task-ring active"><svg width="24" height="24" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="11" fill="none" stroke="var(--line)" stroke-width="2" /><circle cx="12" cy="12" r="11" fill="none" stroke="var(--ink-3)" stroke-width="2" stroke-linecap="round" stroke-dasharray="8 23" /></svg><span>${escapeHtml(role)}</span></span>`;
  }
  if (normalized === "failed" || normalized === "incomplete") {
    return `<span class="task-badge-icon failed">${X_ICON}</span>`;
  }
  return `<span class="task-badge-icon done">${CHECK_ICON}</span>`;
}

/** Port of code_activity_key() -- one stable id per tool call. */
export function activityKey(event) {
  const id = String(event.activity_id || "").trim();
  if (id) return id;
  for (const field of ["command", "detail", "title", "text"]) {
    const value = String(event[field] || "").trim();
    if (!value) continue;
    const normalized = value
      .replace(/^\$\s*/, "")
      .replace(/^(?:run|ran|running|check|checked|checking)\s+/i, "")
      .replace(/\s+/g, " ")
      .toLowerCase()
      .slice(0, 160);
    return `${event.kind || "tool"}:${normalized}`;
  }
  return `${event.kind || "tool"}:${event.ts || ""}`;
}

/** Port of _code_activity_from_event() -- legacy rows become card-shaped. */
function normaliseActivity(event) {
  const kind = String(event.kind || "tool");
  if (kind === "activity") return { ...event };
  const raw = String(event.text || "Working").trim();
  let type = kind === "thinking" ? "thinking" : "tool";
  if (raw.startsWith("$ ") || event.tool === "command") type = "command";
  else if (/^Edited\b/i.test(raw) || event.tool === "files") type = "files";
  const titles = {
    thinking: "Thought through the approach",
    command: "Ran command",
    files: raw.split("\n")[0],
  };
  return {
    kind: "activity",
    activity_id: activityKey(event),
    activity_type: type,
    phase: "completed",
    title: titles[type] || (kind === "approval" ? "Approved permission" : raw.split(":")[0].slice(0, 80)),
    detail: type === "command" ? "" : raw,
    command: type === "command" ? raw.replace(/^\$\s*/, "") : "",
    summary: type === "thinking" ? raw : "",
    ts: event.ts,
  };
}

/** Port of format_duration() -- compact h/m/s. */
export function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  if (total < 60) return `${total}s`;
  if (total < 3600) return `${Math.floor(total / 60)}m ${total % 60}s`;
  return `${Math.floor(total / 3600)}h ${Math.floor((total % 3600) / 60)}m`;
}

/** Port of _compact_tokens() -- 13.2M, 940K, 812. */
export function compactTokens(value) {
  const count = Number(value || 0);
  if (count >= 1e9) return `${(count / 1e9).toFixed(1)}B`;
  if (count >= 1e6) return `${(count / 1e6).toFixed(1)}M`;
  if (count >= 1e3) return `${(count / 1e3).toFixed(1)}K`;
  return String(count);
}

export function relativeTime(value) {
  const seconds = Math.max(0, Date.now() / 1000 - Number(value || 0));
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export class Transcript {
  /**
   * @param root       the scrolling element the rows are written into
   * @param options.jump      the floating "jump to latest" button, if there is one
   * @param options.isActive  () => is the session still running? Used to settle
   *                          the caret on a turn that ended without a closing event.
   * @param options.onReveal  (path) => open a file the transcript mentions
   */
  constructor(root, options = {}) {
    this.transcript = root;
    this.jumpEl = options.jump || null;
    this.isActive = options.isActive || (() => false);
    this.onReveal = options.onReveal || null;
    this.onReviewSuggest = options.onReviewSuggest || null;
    this.onReviewFix = options.onReviewFix || null;
    this.onAnswer = options.onAnswer || null;

    this.queue = [];
    this.frame = null;
    this.fallback = null;
    this.follow = true;
    // Scroll bookkeeping: what we last wrote, and whether the user has the
    // scrollbar in hand. See scrollToEnd().
    this.wroteTop = -1;
    this.wroteHeight = -1;
    this.scrollHeld = false;
    this.scrollHoldUntil = 0;
    this.cards = new Map();
    this.stages = new Map();
    this.assistant = null;
    this.turnText = "";
    this.turnFileDiffs = new Map();
    this.turnDiffParts = new Map();
    this.turnUsage = new Map();
    this.suppressHarnessOutput = false;
    this.thinkingEl = null;
    this.thinkingKey = null;
    this.thinkingText = "";
    this.thinkingStartedAt = 0;
    this.toolRun = null;
    this.questionCards = new Map();
    this.questionSerial = 0;
    this.questionTimers = new Set();
    this.workingEl = null;
    this.workingTimer = null;
    this.workingStartedAt = 0;
    this.currentTurnStartedAt = 0;
    this.showThinking = true;
    this.showReasoning = true;
    try {
      this.showThinking = window.localStorage.getItem("aiosShowThinking") !== "0";
      this.showReasoning = window.localStorage.getItem("aiosShowReasoning") !== "0";
    } catch (_) { /* storage can be unavailable in a locked-down WebView */ }

    this.abort = new AbortController();
    this.mountControls();
    this.bind();
  }

  mountControls() {
    const node = document.createElement("div");
    node.className = "transcript-controls";
    node.innerHTML = `
      <span class="transcript-controls-label">Show</span>
      <button type="button" data-transcript-toggle="thinking" aria-pressed="${this.showThinking}">Thinking</button>
      <button type="button" data-transcript-toggle="reasoning" aria-pressed="${this.showReasoning}">Reasoning</button>
    `;
    this.transcript.prepend(node);
    this.transcript.classList.toggle("hide-live-thinking", !this.showThinking);
    this.transcript.classList.toggle("hide-reasoning", !this.showReasoning);
    this.controlsEl = node;
  }

  destroy() {
    this.abort.abort();
    if (this.frame !== null) cancelAnimationFrame(this.frame);
    this.frame = null;
    for (const timer of this.questionTimers) clearTimeout(timer);
    this.questionTimers.clear();
    this.clearFallback();
    this.clearWorking();
  }

  bind() {
    // Every listener is filed under one AbortController, including the two on
    // window: a page that swapped its transcript out would otherwise keep the
    // old one alive through them.
    const on = (target, type, handler, extra) =>
      target.addEventListener(type, handler, { signal: this.abort.signal, ...extra });

    on(this.transcript, "click", (event) => {
      const displayToggle = event.target.closest("[data-transcript-toggle]");
      if (displayToggle) {
        const option = displayToggle.dataset.transcriptToggle;
        if (option === "thinking") this.showThinking = !this.showThinking;
        if (option === "reasoning") this.showReasoning = !this.showReasoning;
        try {
          window.localStorage.setItem("aiosShowThinking", this.showThinking ? "1" : "0");
          window.localStorage.setItem("aiosShowReasoning", this.showReasoning ? "1" : "0");
        } catch (_) { /* keep the in-memory choice */ }
        this.transcript.classList.toggle("hide-live-thinking", !this.showThinking);
        this.transcript.classList.toggle("hide-reasoning", !this.showReasoning);
        this.controlsEl?.querySelector('[data-transcript-toggle="thinking"]')
          ?.setAttribute("aria-pressed", String(this.showThinking));
        this.controlsEl?.querySelector('[data-transcript-toggle="reasoning"]')
          ?.setAttribute("aria-pressed", String(this.showReasoning));
        return;
      }
      const fix = event.target.closest("[data-review-fix]");
      if (fix) {
        const cardNode = fix.closest(".tool-card");
        const key = cardNode?.dataset?.activityKey;
        const state = key ? this.cards.get(key)?.state : null;
        if (state && this.onReviewFix) this.onReviewFix(state);
        return;
      }
      const suggest = event.target.closest("[data-review-index]");
      if (suggest) {
        const cardNode = suggest.closest(".tool-card");
        const key = cardNode?.dataset?.activityKey;
        const row = key ? this.cards.get(key)?.state?.suggestions?.[Number(suggest.dataset.reviewIndex)] : null;
        const prompt = String(row?.prompt || "").trim();
        if (prompt && this.onReviewSuggest) this.onReviewSuggest(prompt);
        return;
      }
      const reveal = event.target.closest("[data-reveal]");
      if (reveal) {
        if (this.onReveal) this.onReveal(reveal.dataset.reveal);
        return;  // never also toggle the card
      }
      const questionAction = event.target.closest("[data-question-action]");
      if (questionAction) {
        const node = questionAction.closest(".question-card");
        const card = node ? this.questionCards.get(node.dataset.questionKey) : null;
        if (card) this.handleQuestionAction(card, questionAction);
        return;
      }
      // Native <details> owns its own disclosure. Do not let clicking a nested
      // Scout tool receipt also collapse the entire Scout card.
      if (event.target.closest(".agent-tool-call")) return;
      const toolRunHead = event.target.closest(".tool-run-head");
      if (toolRunHead) {
        const run = toolRunHead.closest(".tool-run");
        run?.classList.toggle("expanded");
        toolRunHead.setAttribute("aria-expanded", String(!!run?.classList.contains("expanded")));
        return;
      }
      const card = event.target.closest(".tool-card");
      if (card && card.classList.contains("expandable")) {
        const beforeTop = card.getBoundingClientRect().top;
        const beforeScroll = this.transcript.scrollTop;
        card.classList.toggle("expanded");
        card.dataset.manual = "true";
        this.setFollow(false, true);
        card.querySelector(".card-chevron")?.setAttribute("aria-expanded", String(card.classList.contains("expanded")));
        card.querySelector(".todoHead")?.setAttribute("aria-expanded", String(card.classList.contains("expanded")));
        requestAnimationFrame(() => {
          if (!card.isConnected) return;
          this.transcript.scrollTop = beforeScroll + (card.getBoundingClientRect().top - beforeTop);
        });
        return;
      }
      const stage = event.target.closest(".pipeline-stage");
      if (stage && stage.classList.contains("expandable")) {
        const beforeTop = stage.getBoundingClientRect().top;
        const beforeScroll = this.transcript.scrollTop;
        stage.classList.toggle("expanded");
        this.setFollow(false, true);
        stage.querySelector(".stage-head")?.setAttribute("aria-expanded", String(stage.classList.contains("expanded")));
        requestAnimationFrame(() => {
          if (!stage.isConnected) return;
          this.transcript.scrollTop = beforeScroll + (stage.getBoundingClientRect().top - beforeTop);
        });
        return;
      }
      const thinking = event.target.closest(".thinking-head");
      if (thinking) {
        const block = thinking.closest(".thinking");
        block.dataset.manual = "true";
        block.classList.toggle("expanded");
        thinking.setAttribute("aria-expanded", String(block.classList.contains("expanded")));
      }
    });

    on(this.transcript, "input", (event) => {
      const input = event.target.closest("[data-question-custom]");
      if (!input) return;
      const node = input.closest(".question-card");
      const card = node ? this.questionCards.get(node.dataset.questionKey) : null;
      if (card) this.updateQuestionCustom(card, input);
    });

    // Auto-follow releases the moment you scroll away, and re-arms when you
    // come back to the bottom -- the same contract as _code_set_auto_follow.
    on(this.transcript, "scroll", () => {
      // Ignore the scroll our own follow produced; only a move we did not make
      // is the user speaking. Compared by exact offset because during a busy
      // turn we write one per frame, and a time window that wide would swallow
      // the drag entirely.
      if (Math.abs(this.transcript.scrollTop - this.wroteTop) <= 1) return;
      const distance = this.transcript.scrollHeight - this.transcript.scrollTop - this.transcript.clientHeight;
      const atBottom = distance < 40;
      if (this.follow !== atBottom) this.setFollow(atBottom, true);
    });

    // While you are working the scrollbar, nothing writes scrollTop.
    //
    // A scrollbar drag is handled on the compositor and its scroll event
    // reaches this thread late, so the flush loop could put the thumb back
    // before we ever heard that you had moved it -- that is the jitter. The
    // pointer press arrives first, and holds the whole loop off.
    on(this.transcript, "pointerdown", () => { this.scrollHeld = true; });
    for (const release of ["pointerup", "pointercancel"]) {
      on(window, release, () => {
        if (!this.scrollHeld) return;
        this.scrollHeld = false;
        const distance = this.transcript.scrollHeight - this.transcript.scrollTop - this.transcript.clientHeight;
        this.setFollow(distance < 40, true);
      }, { passive: true });
    }
    // Wheel and touch have momentum and no release to wait for.
    for (const gesture of ["wheel", "touchstart", "touchmove"]) {
      on(this.transcript, gesture, () => { this.scrollHoldUntil = performance.now() + 350; });
    }
  }

  // ------------------------------------------------------------------ input

  /** Feed a batch of events in. One call per SSE frame. */
  push(events) {
    for (const event of events || []) this.queue.push(event);
    this.schedule();
  }

  reset() {
    // The jump button is a sibling of the transcript, not a child, so emptying
    // the transcript no longer takes it with it.
    this.transcript.innerHTML = "";
    this.mountControls();
    this.wroteTop = -1;
    this.wroteHeight = -1;
    this.cards.clear();
    this.stages.clear();
    this.assistant = null;
    this.turnText = "";
    this.turnFileDiffs.clear();
    this.turnDiffParts.clear();
    this.turnUsage.clear();
    this.suppressHarnessOutput = false;
    this.thinkingEl = null;
    this.thinkingKey = null;
    this.thinkingText = "";
    this.thinkingStartedAt = 0;
    this.toolRun = null;
    this.clearWorking();
    this.currentTurnStartedAt = 0;
    this.queue = [];
    this.follow = true;
    if (this.jumpEl) this.jumpEl.classList.remove("show");
  }

  // ----------------------------------------------------------------- scroll

  setFollow(value, fromScroll = false) {
    this.follow = value;
    if (this.jumpEl) this.jumpEl.classList.toggle("show", !value);
    if (value && !fromScroll) this.scrollToEnd(true);
  }

  /** Land on the latest message and keep landing while the layout settles. */
  pinToEnd(settleMs = 600) {
    this.follow = true;
    const deadline = performance.now() + settleMs;
    const step = () => {
      // The user taking over ends the settle immediately.
      if (!this.follow || this.scrollHeld || performance.now() < (this.scrollHoldUntil || 0)) return;
      this.scrollToEnd(true);
      if (performance.now() < deadline) setTimeout(step, 50);
    };
    step();
  }

  scrollToEnd(force = false) {
    const el = this.transcript;
    if (!force && (this.scrollHeld || performance.now() < (this.scrollHoldUntil || 0))) return;
    // Nothing new to follow. Re-writing the same offset every flush is what
    // fought the drag on a finished session, where no content arrives at all.
    if (!force && el.scrollHeight === this.wroteHeight && Math.abs(el.scrollTop - this.wroteTop) > 1) return;
    el.scrollTop = el.scrollHeight;
    this.wroteTop = el.scrollTop;
    this.wroteHeight = el.scrollHeight;
  }

  // -------------------------------------------------------- the rAF writer

  /**
   * Ask for a flush.
   *
   * rAF is the right clock while the window is on screen, but aiOS is an
   * overlay -- it spends most of its life hidden, and a hidden window does not
   * composite, so rAF never fires. Without a fallback the event queue would
   * grow for as long as you had the overlay tucked away and then land in one
   * enormous batch. The timer keeps state moving while hidden (DOM mutation is
   * still legal, it just is not painted); whichever fires first wins.
   */
  schedule() {
    if (this.frame !== null || this.fallback !== null) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = null;
      this.clearFallback();
      this.flush();
    });
    this.fallback = setTimeout(() => {
      this.fallback = null;
      if (this.frame !== null) {
        cancelAnimationFrame(this.frame);
        this.frame = null;
      }
      this.flush();
    }, 250);
  }

  clearFallback() {
    if (this.fallback === null) return;
    clearTimeout(this.fallback);
    this.fallback = null;
  }

  flush() {
    // Drain the whole queue into the DOM, then settle the scroll once.
    if (this.queue.length) {
      const batch = this.queue;
      this.queue = [];
      for (const event of batch) this.applyEvent(event);
    }

    // Advance the streaming reveal; keep the frame loop alive while it catches up.
    const streaming = this.advanceAssistant();

    // A finished turn often ends on an assistant event with nothing after it to
    // close the block. Left alone the caret would blink forever on a session
    // that stopped hours ago, so settle it once the job is no longer active.
    if (!streaming && this.assistant && !this.isActive()) this.closeAssistant();

    if (this.follow) this.scrollToEnd();
    if (streaming || this.queue.length) this.schedule();
  }

  // ------------------------------------------------------------- transcript

  applyEvent(event) {
    let kind = String(event.kind || "status");
    const terminalResult = kind === "result";
    const text = String(event.text || kind);
    const harnessAction = String(event.harness_action || "");

    // Older harnesses asked the model to answer a completion gate, then wrote
    // that answer as ordinary assistant text. The adjacent status event is the
    // protocol marker: suppress that generated gate answer without matching
    // prose, and reopen the stream when a recovered real final is published.
    if (kind === "status" && harnessAction === "recovered_from_completion_gate") {
      this.suppressHarnessOutput = false;
    } else if (kind === "status" && (event.verification_state || harnessAction.includes("verification"))) {
      this.suppressHarnessOutput = true;
    } else if (kind === "user") {
      this.suppressHarnessOutput = false;
      if (event.answer_to_question) {
        this.settleQuestion(String(event.answer_to_question), event.question_answers || {});
      } else {
        // Legacy question events had no stable id on the answering user row.
        // A following user message still answered the one pending question in
        // the protocol, so settle that card during replay instead of leaving a
        // stale control that can accidentally continue an old session.
        this.settleLatestQuestion({ legacy_answer: [text] });
      }
      const rawStartedAt = Number(event.ts || 0);
      this.currentTurnStartedAt = rawStartedAt > 0
        ? (rawStartedAt > 1e12 ? rawStartedAt : rawStartedAt * 1000)
        : Date.now();
      // Session state can arrive before transcript replay. Re-anchor an
      // existing sentinel when the user event identifies this round's start.
      if (this.workingEl && this.currentTurnStartedAt > this.workingStartedAt) {
        this.workingStartedAt = this.currentTurnStartedAt;
        this.paintWorking();
        this.scheduleWorkingClock();
      }
    }
    if (kind === "user") {
      this.finishTurnUsage();
      this.finishTurnDiffs();
    }
    if (this.suppressHarnessOutput && (kind === "assistant" || kind === "assistant_delta" || kind === "result")) {
      return;
    }

    if (kind === "result") {
      // The duplicate ending.
      //
      // A turn can emit several assistant events, and the final `result` is the
      // WHOLE turn concatenated -- not just the last block. Comparing against
      // only the most recent assistant block therefore never matched, so the
      // entire reply got printed a second time. Compare against everything
      // streamed since the last user message instead.
      const turn = this.turnText.replace(/\s+/g, "");
      const settled = text.replace(/\s+/g, "");
      if (turn && settled && (turn === settled || turn.includes(settled) || settled.includes(turn))) {
        this.closeAssistant();
        this.finishTurnUsage();
        this.finishTurnDiffs();
        return;
      }
      kind = "assistant";
    }

    if (kind === "activity" || kind === "tool" || kind === "thinking" || kind === "approval") {
      const activity = normaliseActivity(event);
      if (String(activity.activity_type || "") === "thinking") {
        this.streamThinking(activity, event);
        return;
      }
      this.closeAssistant();
      this.closeThinking();
      if (String(activity.activity_type || "") === "stage") {
        this.upsertStage(activity);
        return;
      }
      this.upsertActivity(activity);
      return;
    }
    if (kind === "provider_switch") {
      this.closeAssistant();
      this.closeToolRun();
      this.addRow("handoff", escapeHtml(text));
      return;
    }
    if (kind === "question") {
      this.closeAssistant();
      this.closeToolRun();
      this.addQuestion(event);
      return;
    }
    if (kind === "assistant" || kind === "assistant_delta") {
      this.closeToolRun();
      this.streamAssistant(String(event.delta || text));
      if (terminalResult) {
        this.finishTurnUsage();
        this.finishTurnDiffs();
      }
      return;
    }

    this.closeAssistant();
    this.closeThinking();
    this.closeToolRun();
    if (kind === "user") {
      this.turnText = "";  // a new turn starts here; result de-dupe resets with it
      // An ask_user answer is delivered to the agent but must not appear as a
      // visible chat bubble -- the question card already showed the choices.
      if (!event.answer_to_question) this.addRow("bubble-user", escapeHtml(text));
    } else if (kind === "status") {
      // The run strip already says "working"/"queued"; no row for those.
      const quiet = ["working", "queued"].includes(text.trim().toLowerCase());
      // Verification is harness bookkeeping, not conversation. Older jobs may
      // still contain these structured status events even though verification
      // no longer vetoes or replaces the Coder's final answer.
      const harnessOnly = !!event.verification_state
        || harnessAction.includes("verification")
        || harnessAction === "recovered_from_completion_gate";
      // Strategy routing is harness state, not a chat message. This removes
      // rows such as "Coder leading..." by protocol fields rather than prose.
      const orchestrationOnly = !!event.strategy || !!event.strategy_override;
      if (!quiet && !harnessOnly && !orchestrationOnly) this.addRow("status-row", escapeHtml(text));
    } else {
      this.addRow("assistant", renderMarkdown(text));
    }
  }

  addRow(className, html, parent = this.transcript) {
    const node = document.createElement("div");
    node.className = `${className} row-new`;
    node.innerHTML = html;
    if (parent === this.transcript && this.workingEl?.isConnected) {
      this.transcript.insertBefore(node, this.workingEl);
    } else {
      parent.appendChild(node);
    }
    // Drop the animation class after it plays so the node stops being composited.
    node.addEventListener("animationend", () => node.classList.remove("row-new"), { once: true });
    return node;
  }

  normaliseQuestionEvent(event) {
    const supplied = Array.isArray(event.questions) && event.questions.length
      ? event.questions
      : [{ id: "question_1", q: event.question || event.text || "Question", type: "radio", options: event.options || [] }];
    return supplied.slice(0, 3).map((row, index) => ({
      id: String(row?.id || `question_${index + 1}`),
      q: String(row?.q || row?.question || row?.prompt || "Question").trim(),
      type: ["check", "checkbox", "multi", "multiple"].includes(String(row?.type || "").toLowerCase()) ? "check" : "radio",
      options: (Array.isArray(row?.options) ? row.options : []).map((option) => ({
        label: String(typeof option === "object" ? (option?.label || option?.value || "") : option).trim(),
        description: String(typeof option === "object" ? (option?.description || "") : "").trim(),
      })).filter((option) => option.label),
    })).filter((row) => row.q);
  }

  addQuestion(event) {
    const questions = this.normaliseQuestionEvent(event);
    if (!questions.length) return;
    const key = String(event.question_id || `question-${++this.questionSerial}`);
    let card = this.questionCards.get(key);
    if (!card) {
      const node = this.addRow("question question-card", "");
      node.dataset.questionKey = key;
      card = { key, node, questions, qi: 0, answers: {}, custom: {}, sent: false, open: true, sending: false, error: "" };
      this.questionCards.set(key, card);
    }
    this.paintQuestion(card);
  }

  questionValues(card, index) {
    const question = card.questions[index];
    const selected = card.answers[index] || [];
    const values = selected.map((optionIndex) => question.options[optionIndex]?.label).filter(Boolean);
    const custom = String(card.custom[index] || "").trim();
    if (custom) values.push(custom);
    return values;
  }

  questionHasAnswer(card, index = card.qi) {
    return this.questionValues(card, index).length > 0;
  }

  paintQuestion(card) {
    if (!card.open) {
      card.node.innerHTML = `<button type="button" class="approval-open" data-question-action="open">Open approval</button>`;
      return;
    }
    const question = card.questions[card.qi];
    const last = card.qi === card.questions.length - 1;
    const selected = card.answers[card.qi] || [];
    const hasAnswer = this.questionHasAnswer(card);
    const options = question.options.map((option, index) => {
      const on = selected.includes(index);
      const control = question.type === "radio"
        ? `<span class="approval-radio-dot"></span>`
        : `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5" /></svg>`;
      return `
        <button type="button" class="approval-option ${on ? "selected" : ""}" aria-pressed="${on}" data-question-action="pick" data-option-index="${index}">
          <span class="approval-choice ${question.type === "radio" ? "radio" : "check"}">${control}</span>
          <span class="approval-option-copy"><span>${escapeHtml(option.label)}</span>${option.description ? `<small>${escapeHtml(option.description)}</small>` : ""}</span>
        </button>`;
    }).join("");
    const progress = card.questions.map((_, index) => {
      const mode = !card.sent && index === card.qi ? "current" : (card.sent || index < card.qi ? "done" : "pending");
      return `<button type="button" class="approval-dot ${mode}" aria-label="Go to question ${index + 1}" ${card.sent ? "disabled" : ""} data-question-action="goto" data-question-index="${index}"></button>`;
    }).join("");

    card.node.innerHTML = `
      <div class="approval-shell">
        <div class="approval-card">
          ${card.sent ? `
            <div class="approval-sent">
              <span class="approval-success"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5" /></svg></span>
              <span class="approval-sent-label">Answers sent</span>
              <button type="button" class="approval-reset" data-question-action="reset">Start over</button>
            </div>` : `
            <div class="approval-content" key="${card.qi}">
              <div class="approval-heading">
                <span>${escapeHtml(question.q)}</span>
                <button type="button" class="approval-icon" aria-label="Dismiss" data-question-action="dismiss">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12" /></svg>
                </button>
              </div>
              <div class="approval-options">${options}
                <label class="approval-custom-row">
                  <span aria-hidden="true"></span>
                  <input value="${escapeHtml(String(card.custom[card.qi] || ""))}" data-question-custom placeholder="Type something&hellip;" aria-label="Custom answer">
                </label>
              </div>
              ${card.error ? `<div class="approval-error">${escapeHtml(card.error)}</div>` : ""}
            </div>`}
          <div class="approval-footer">
            <span class="approval-pager">
              <button type="button" class="approval-icon" aria-label="Previous" ${card.qi === 0 || card.sent ? "disabled" : ""} data-question-action="prev">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6" /></svg>
              </button>
              <span class="approval-dots">${progress}</span>
              <button type="button" class="approval-icon" aria-label="Next" ${last || card.sent ? "disabled" : ""} data-question-action="next">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6" /></svg>
              </button>
            </span>
            ${card.sent ? "" : `
              <button type="button" class="approval-send ${hasAnswer ? "ready" : ""}" aria-label="${last ? "Send answers" : "Next question"}" ${hasAnswer && !card.sending ? "" : "disabled"} data-question-action="advance">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7" /></svg>
              </button>`}
          </div>
        </div>
      </div>`;
  }

  handleQuestionAction(card, control) {
    const action = String(control.dataset.questionAction || "");
    if (action === "dismiss" || action === "open") card.open = action === "open";
    else if (action === "reset") Object.assign(card, { qi: 0, answers: {}, custom: {}, sent: false, open: true, sending: false, error: "" });
    else if (action === "prev") card.qi = Math.max(0, card.qi - 1);
    else if (action === "next") card.qi = Math.min(card.questions.length - 1, card.qi + 1);
    else if (action === "goto") card.qi = Math.max(0, Math.min(card.questions.length - 1, Number(control.dataset.questionIndex) || 0));
    else if (action === "pick") {
      const index = Number(control.dataset.optionIndex);
      const question = card.questions[card.qi];
      const picked = card.answers[card.qi] || [];
      card.answers[card.qi] = question.type === "radio"
        ? [index]
        : (picked.includes(index) ? picked.filter((item) => item !== index) : [...picked, index]);
      if (question.type === "radio") {
        card.custom[card.qi] = "";
        const activeIndex = card.qi;
        this.paintQuestion(card);
        const timer = setTimeout(() => {
          this.questionTimers.delete(timer);
          if (card.qi !== activeIndex || card.sent) return;
          if (activeIndex === card.questions.length - 1) this.submitQuestion(card);
          else {
            card.qi = activeIndex + 1;
            this.paintQuestion(card);
          }
        }, 480);
        this.questionTimers.add(timer);
        return;
      }
    } else if (action === "advance" && this.questionHasAnswer(card)) {
      if (card.qi === card.questions.length - 1) this.submitQuestion(card);
      else card.qi += 1;
    }
    this.paintQuestion(card);
  }

  updateQuestionCustom(card, input) {
    card.custom[card.qi] = input.value;
    if (card.questions[card.qi].type === "radio") {
      card.answers[card.qi] = [];
      card.node.querySelectorAll(".approval-option.selected").forEach((option) => {
        option.classList.remove("selected");
        option.setAttribute("aria-pressed", "false");
      });
    }
    const send = card.node.querySelector('[data-question-action="advance"]');
    const ready = this.questionHasAnswer(card);
    if (send) {
      send.disabled = !ready || card.sending;
      send.classList.toggle("ready", ready);
    }
  }

  async submitQuestion(card) {
    if (card.sent || card.sending || !this.questionHasAnswer(card)) return;
    const answers = {};
    const lines = card.questions.map((question, index) => {
      const values = this.questionValues(card, index);
      answers[question.id] = values;
      return `${question.q}: ${values.join(", ")}`;
    });
    const text = card.questions.length === 1 ? (answers[card.questions[0].id] || []).join(", ") : lines.join("\n");
    card.sending = true;
    card.sent = true;
    card.error = "";
    this.paintQuestion(card);
    try {
      if (this.onAnswer) await this.onAnswer({ text, answers, questionId: card.key });
    } catch (error) {
      card.sent = false;
      card.error = String(error?.message || error || "Could not send answers.");
    } finally {
      card.sending = false;
      this.paintQuestion(card);
    }
  }

  settleQuestion(key, answers = {}) {
    const card = this.questionCards.get(String(key));
    if (!card) return;
    if (answers && typeof answers === "object") card.submittedAnswers = answers;
    card.sent = true;
    card.sending = false;
    card.error = "";
    this.paintQuestion(card);
  }

  settleLatestQuestion(answers = {}) {
    const pending = [...this.questionCards.values()].filter((card) => !card.sent);
    const card = pending[pending.length - 1];
    if (!card) return;
    this.settleQuestion(card.key, answers);
  }

  closeToolRun() {
    this.toolRun = null;
  }

  ensureToolRun() {
    if (this.toolRun?.node?.isConnected) return this.toolRun;
    const node = this.addRow("tool-run", `
      <button type="button" class="tool-run-head" aria-expanded="false">
        <span class="tool-run-chevron" aria-hidden="true">${CHEVRON_ICON}</span>
        <span class="tool-run-summary">Activity</span>
        <span class="tool-run-latest" hidden><span class="tool-run-live-mark" aria-hidden="true"></span><span class="tool-run-latest-text"></span></span>
      </button>
      <div class="tool-run-expand">
        <div class="tool-run-clip">
          <section class="tool-run-reasoning" hidden>
            <div class="tool-run-reasoning-head"><span aria-hidden="true">${SPARK_ICON}</span><span>Reasoning</span><span class="tool-run-reasoning-count"></span></div>
            <div class="tool-run-reasoning-content"></div>
          </section>
          <div class="tool-run-rows"></div>
          <div class="tool-run-diffs" hidden></div>
        </div>
      </div>
    `);
    this.toolRun = {
      node,
      body: node.querySelector(".tool-run-rows"),
      cards: new Set(),
      fileDiffs: new Map(),
      reasoningKeys: new Set(),
      reasoningParts: new Map(),
      reasoningActive: false,
      reasoningTitle: "Thinking",
      reasoningOrder: 0,
      sequence: 0,
    };
    return this.toolRun;
  }

  updateToolRun(run) {
    const count = run.cards.size;
    const files = run.fileDiffs.size;
    const reasoningCount = run.reasoningKeys?.size || 0;
    run.node.querySelector(".tool-run-summary").textContent = [
      count ? `${count} tool call${count === 1 ? "" : "s"}` : "Activity",
      reasoningCount ? `${reasoningCount} thought${reasoningCount === 1 ? "" : "s"}` : "",
      files ? `${files} file${files === 1 ? "" : "s"} changed` : "",
    ].filter(Boolean).join(" · ");
    const cards = [...run.cards].map((key) => this.cards.get(key)).filter(Boolean);
    const activeTool = cards.filter((card) => ["started", "update"].includes(String(card.state.phase || "")))
      .sort((a, b) => Number(b.updatedOrder || 0) - Number(a.updatedOrder || 0))[0];
    const latestTool = activeTool || cards.sort((a, b) => Number(b.updatedOrder || 0) - Number(a.updatedOrder || 0))[0];
    const reasoningWins = !!run.reasoningActive
      && (!activeTool || Number(run.reasoningOrder || 0) >= Number(activeTool.updatedOrder || 0));
    const latest = reasoningWins
      ? { state: { title: run.reasoningTitle || "Thinking", phase: "started" }, updatedOrder: run.reasoningOrder }
      : latestTool;
    const active = reasoningWins || !!activeTool;
    const preview = run.node.querySelector(".tool-run-latest");
    if (latest) {
      const state = latest.state || {};
      const label = String(state.title || state.activity_type || "Tool").trim();
      const rawDetail = String(state.command || state.detail || "").trim();
      const detail = rawDetail.split(/\r?\n/)[0];
      preview.querySelector(".tool-run-latest-text").textContent = `${active ? "Active" : "Latest"}: ${label}${detail && detail !== label ? ` \u00b7 ${detail}` : ""}`;
      const mark = preview.querySelector(".tool-run-live-mark");
      if (mark) mark.innerHTML = active ? loadingPixelsMarkup("orbit") : "";
      preview.classList.toggle("active", !!active);
      preview.classList.toggle("failed", !active && String(state.phase || "") === "failed");
      preview.hidden = false;
    } else {
      preview.hidden = true;
    }
    const diffs = run.node.querySelector(".tool-run-diffs");
    const rows = [...run.fileDiffs.values()];
    diffs.hidden = !rows.length;
    diffs.innerHTML = rows.map(fileDiffMarkup).join("");
  }

  recordTurnDiff(change, source = "") {
    const path = String(change?.path || "").trim();
    if (!path) return;
    const row = {
      path,
      name: String(change.name || path.split(/[\\/]/).pop() || path),
      add: Number(change.add || 0),
      del: Number(change.del || 0),
      diff: String(change.diff || ""),
    };
    this.turnDiffParts.set(`${String(source || "change")}\u0000${path}`, row);
    const total = { ...row, add: 0, del: 0 };
    for (const part of this.turnDiffParts.values()) {
      if (part.path !== path) continue;
      total.add += Number(part.add || 0);
      total.del += Number(part.del || 0);
      total.diff = part.diff || total.diff;
    }
    this.turnFileDiffs.set(path, total);
  }

  finishTurnDiffs() {
    if (!this.turnFileDiffs.size) return;
    // The exact file-diff cards already live in the turn's compact Activity
    // disclosure. A second summary row after the answer duplicated every file.
    this.turnFileDiffs.clear();
    this.turnDiffParts.clear();
  }

  finishTurnUsage() {
    if (!this.turnUsage.size) return;
    const rows = [...this.turnUsage.values()];
    this.turnUsage.clear();
    this.addRow("turn-usage", rows.map((row) => {
      const usage = row.usage || {};
      const bits = [
        row.provider || "",
        row.model || "",
        `${Number(usage.input_tokens || 0).toLocaleString()} input`,
        `${Number(usage.cached_input_tokens || 0).toLocaleString()} cached`,
        `${Number(usage.output_tokens || 0).toLocaleString()} output`,
        `${Number(usage.reasoning_tokens || 0).toLocaleString()} reasoning`,
        `${Number(usage.total_tokens || 0).toLocaleString()} total`,
        `$${Number(usage.cost_usd || 0).toFixed(6)}`,
        formatDuration(row.seconds || 0),
      ].filter(Boolean);
      return `
        <div class="turn-usage-row">
          <span class="turn-usage-role">${escapeHtml(row.label || "Coder")}</span>
          <span class="turn-usage-detail">${escapeHtml(bits.join(" · "))}</span>
        </div>
      `;
    }).join(""));
  }

  setWorking(active, detail = "", startedAt = 0) {
    if (!active) {
      this.clearWorking();
      return;
    }
    if (!this.workingEl) {
      const rawStartedAt = Number(startedAt || 0);
      const backendTurnStartedAt = rawStartedAt > 0
        ? (rawStartedAt > 1e12 ? rawStartedAt : rawStartedAt * 1000)
        : 0;
      this.workingStartedAt = Math.max(this.currentTurnStartedAt, backendTurnStartedAt) || Date.now();
      this.workingEl = document.createElement("div");
      this.workingEl.className = "working-sentinel live row-new";
      this.workingEl.innerHTML = `
        <span class="working-pixels" aria-hidden="true">${loadingPixelsMarkup()}</span>
        <span class="working-label">Churning</span>
        <span class="working-detail"></span>
        <span class="working-time"></span>
      `;
      this.transcript.appendChild(this.workingEl);
      this.scheduleWorkingClock();
    }
    this.workingEl.querySelector(".working-detail").textContent = String(detail || "The agent is working.");
    // appendChild on an already-attached node removes and re-inserts it,
    // restarting the CSS animation. Only append when it is not in the tree.
    if (this.workingEl.parentNode !== this.transcript) this.transcript.appendChild(this.workingEl);
    this.paintWorking();
    if (this.follow) this.scrollToEnd();
  }

  paintWorking() {
    if (!this.workingEl) return;
    const elapsed = Math.max(0, (Date.now() - this.workingStartedAt) / 1000);
    this.workingEl.querySelector(".working-time").textContent = formatDuration(elapsed);
  }

  scheduleWorkingClock() {
    if (this.workingTimer) clearTimeout(this.workingTimer);
    if (!this.workingEl) return;
    // The label has one-second precision. Align its DOM write to the next
    // second so the churning pixels remain a smooth compositor animation.
    const elapsedMs = Math.max(0, Date.now() - this.workingStartedAt);
    const untilNextSecond = Math.max(80, 1000 - (elapsedMs % 1000));
    this.workingTimer = setTimeout(() => {
      this.paintWorking();
      this.scheduleWorkingClock();
    }, untilNextSecond);
  }

  clearWorking() {
    if (this.workingTimer) clearTimeout(this.workingTimer);
    this.workingTimer = null;
    this.workingEl?.remove();
    this.workingEl = null;
    this.workingStartedAt = 0;
  }

  streamAssistant(delta) {
    if (!this.assistant) {
      const node = this.addRow("assistant streaming", "");
      this.assistant = { node, text: "", shown: 0, settledHtml: "", settledUpto: 0 };
    }
    this.assistant.text += delta;
    // Accumulates across every assistant block in the turn so the trailing
    // `result` event can be recognised as a repeat of the whole turn.
    this.turnText += delta;
  }

  /**
   * Reveal buffered text on the frame clock.
   *
   * Providers emit in lumps -- 300 characters, then nothing for 200ms. Painting
   * lumps looks like stutter even though it is fast. Revealing a fraction of the
   * outstanding backlog each frame turns any arrival pattern into an even flow,
   * and the fraction means it always catches up rather than lagging behind.
   */
  advanceAssistant() {
    const state = this.assistant;
    if (!state) return false;
    const outstanding = state.text.length - state.shown;
    if (outstanding <= 0) return false;

    const step = Math.max(2, Math.ceil(outstanding / 60));
    state.shown = Math.min(state.text.length, state.shown + step);
    this.paintAssistant(state);
    return state.shown < state.text.length;
  }

  /**
   * Re-parse only the unsettled tail.
   *
   * Everything before the last blank line (outside a code fence) can never
   * change, so it is parsed once into settledHtml. Without this, a 40KB reply
   * would re-parse 40KB every frame.
   */
  paintAssistant(state) {
    const visible = state.text.slice(0, state.shown);
    const tailStart = state.settledUpto;
    const boundary = visible.lastIndexOf("\n\n");
    if (boundary > tailStart + 1024) {
      const candidate = visible.slice(0, boundary);
      // Only settle outside a fence, or the block would be split in half.
      if ((candidate.match(/```/g) || []).length % 2 === 0) {
        state.settledHtml += renderMarkdown(visible.slice(tailStart, boundary));
        state.settledUpto = boundary;
      }
    }
    state.node.innerHTML = state.settledHtml + renderMarkdown(visible.slice(state.settledUpto));
  }

  closeAssistant() {
    if (!this.assistant) return;
    const state = this.assistant;
    state.shown = state.text.length;
    this.paintAssistant(state);
    state.node.classList.remove("streaming");
    this.assistant = null;
  }

  /**
   * Reasoning, shown for real.
   *
   * Providers stream the actual thought in `delta` across many `phase:update`
   * events, while `text`/`title` only ever say "Thinking" then "Thought through
   * the approach". Reading the title was why you saw the label instead of the
   * content -- the deltas have to be accumulated per activity_id.
   */
  streamThinking(activity, raw = {}) {
    const key = String(activity.activity_id || activityKey(activity));
    this.closeAssistant();
    const run = this.ensureToolRun();
    const reasoning = run.node.querySelector(".tool-run-reasoning");

    if (this.thinkingKey !== key) {
      this.thinkingKey = key;
      this.thinkingText = "";
      this.thinkingStartedAt = Date.now();
      run.reasoningKeys.add(key);
      run.reasoningParts.set(key, "");
    }
    if (raw.delta) this.thinkingText += normalizeReasoningText(raw.delta);
    else if (activity.summary) this.thinkingText = normalizeReasoningText(activity.summary);
    run.reasoningParts.set(key, this.thinkingText);

    const phase = String(raw.phase || activity.phase || "").toLowerCase();
    const done = phase === "completed" || phase === "failed";
    run.reasoningActive = !done;
    run.reasoningTitle = String(activity.title || "Thinking");
    run.reasoningOrder = ++run.sequence;
    reasoning.hidden = false;
    reasoning.classList.toggle("live", !done);
    reasoning.querySelector(".tool-run-reasoning-count").textContent =
      `${run.reasoningKeys.size} thought${run.reasoningKeys.size === 1 ? "" : "s"}`;
    reasoning.querySelector(".tool-run-reasoning-content").innerHTML = [...run.reasoningParts.values()]
      .map((part) => String(part || "").trim())
      .filter(Boolean)
      .map((part) => `<div class="tool-run-thought">${renderMarkdown(part)}</div>`)
      .join("");
    this.thinkingEl = reasoning;
    this.updateToolRun(run);
    if (done) this.closeThinking();
  }

  closeThinking() {
    if (this.thinkingEl) this.thinkingEl.classList.remove("live");
    if (this.toolRun) {
      this.toolRun.reasoningActive = false;
      this.updateToolRun(this.toolRun);
    }
    this.thinkingEl = null;
    this.thinkingKey = null;
    this.thinkingText = "";
    this.thinkingStartedAt = 0;
  }

  upsertStage(event) {
    const key = activityKey(event);
    let row = this.stages.get(key);
    if (!row) {
      this.closeToolRun();
      const node = this.addRow("pipeline-stage task-row", `
        <button type="button" class="stage-head" aria-expanded="false">
          <span class="stage-state task-badge"></span>
          <span class="stage-copy">
            <span class="stage-label task-label"></span>
            <span class="stage-detail task-amount"></span>
          </span>
          <span class="stage-metrics"></span>
          <span class="stage-pill task-pill"></span>
          <span class="stage-chevron task-chevron" aria-hidden="true">${CHEVRON_ICON}</span>
        </button>
        <div class="stage-expand">
          <div class="stage-expand-inner">
            <span class="stage-track" aria-hidden="true"></span>
            <div class="stage-detail-items">
              <div class="stage-context"></div>
              <div class="stage-provider"></div>
            </div>
          </div>
        </div>
      `);
      row = { node, state: {} };
      this.stages.set(key, row);
    }

    const state = row.state;
    for (const field of ["stage", "title", "detail", "phase"]) {
      if (event[field] !== null && event[field] !== undefined && event[field] !== "") {
        state[field] = String(event[field]);
      }
    }
    if (event.usage && typeof event.usage === "object") state.usage = { ...event.usage };
    if (event.seconds !== null && event.seconds !== undefined) state.seconds = Number(event.seconds || 0);
    if (event.model) state.model = String(event.model);
    if (event.provider) state.provider = String(event.provider);
    const rawPhase = String(state.phase || "started").toLowerCase();
    const phase = PHASE_MAP[rawPhase]
      || (["started", "update", "completed", "failed", "incomplete"].includes(rawPhase) ? rawPhase : "completed");
    const stage = String(state.stage || "work").toLowerCase();
    const meta = {
      scout: "Scout", planner: "Consultant", consultant: "Consultant",
      coder: "Coder", reviewer: "Reviewer",
    }[stage] || state.title || "Work";
    const stageTitle = String(state.title || meta).replace(/planner/gi, "Consultant");
    const visibleTitle = stage === "planner" && !/consultant/i.test(stageTitle)
      ? `Consultant · ${stageTitle}`
      : stageTitle;

    const wasExpanded = row.node.classList.contains("expanded");
    row.node.className = `pipeline-stage task-row ${phase}${wasExpanded ? " expanded" : ""}`;
    // Coder lifecycle is already represented by the always-present Churning
    // sentinel while active and the usage footer when settled. Rendering a
    // second spinning Coder card adds no information.
    row.node.hidden = stage === "coder";
    row.node.dataset.activityKey = key;
    row.node.querySelector(".stage-label").textContent = visibleTitle;
    row.node.querySelector(".stage-detail").textContent = stage === "planner"
      ? String(state.detail || "").replace(/planner/gi, "consultant")
      : (state.detail || "");
    const usage = state.usage || {};
    const total = Number(usage.total_tokens || 0);
    const cost = Number(usage.cost_usd || 0);
    const settled = ["completed", "failed", "incomplete"].includes(phase);
    const metrics = [];
    if (total || settled) metrics.push(`${total.toLocaleString()} tok`);
    if (cost || settled) metrics.push(`$${cost.toFixed(6)}`);
    if (Number(state.seconds || 0) || settled) metrics.push(formatDuration(state.seconds || 0));
    const metricsNode = row.node.querySelector(".stage-metrics");
    metricsNode.textContent = metrics.join(" · ");
    metricsNode.title = [
      state.model || "",
      `${Number(usage.input_tokens || 0).toLocaleString()} input`,
      `${Number(usage.cached_input_tokens || 0).toLocaleString()} cached`,
      `${Number(usage.output_tokens || 0).toLocaleString()} output`,
      `${Number(usage.reasoning_tokens || 0).toLocaleString()} reasoning`,
      `$${cost.toFixed(10)}`,
    ].filter(Boolean).join(" · ");
    row.node.querySelector(".stage-state").innerHTML = taskBadgeMarkup(phase, meta.slice(0, 1));
    row.node.querySelector(".stage-pill").textContent = {
      started: "Running", update: "Running", completed: "Done", failed: "Failed", incomplete: "Needs input",
    }[phase] || "Done";
    row.node.querySelector(".stage-context").textContent = state.detail || `${meta} activity`;
    row.node.querySelector(".stage-provider").textContent = [
      state.provider || "",
      state.model || "",
      `${Number(usage.input_tokens || 0).toLocaleString()} input`,
      `${Number(usage.cached_input_tokens || 0).toLocaleString()} cached`,
      `${Number(usage.output_tokens || 0).toLocaleString()} output`,
      `${Number(usage.reasoning_tokens || 0).toLocaleString()} reasoning`,
      `$${cost.toFixed(10)}`,
    ].filter(Boolean).join(" · ");
    const expandable = !!(state.detail || state.provider || state.model || metrics.length);
    row.node.classList.toggle("expandable", expandable);
    row.node.querySelector(".stage-head")
      .setAttribute("aria-expanded", String(row.node.classList.contains("expanded")));
    if (stage === "coder" && phase === "completed") {
      this.turnUsage.set(key, {
        label: meta,
        provider: state.provider || "",
        model: state.model || "",
        usage: { ...(state.usage || {}) },
        seconds: Number(state.seconds || 0),
      });
      // Settled Coder data belongs in the single end-of-turn footer.
    } else {
      if (stage === "coder") this.turnUsage.delete(key);
    }
  }

  upsertActivity(event) {
    const key = activityKey(event);
    let card = this.cards.get(key);
    if (!card) {
      const rawType = String(event.activity_type || "");
      const type = rawType === "planner" ? "consultant" : rawType;
      const taskActivity = ["plan", "consultant", "subagent", "review"].includes(type);
      if (taskActivity) this.closeToolRun();
      const run = taskActivity ? null : this.ensureToolRun();
      const markup = type === "plan" ? `
        <button type="button" class="todoHead" aria-expanded="true" aria-label="Toggle to-dos">
          <span class="todoHeadIcon">
            ${TODO_LIST_ICON}
            <span class="todoHeadPie" aria-hidden="true"><svg class="todoHeadPieRing" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10.5" fill="none" stroke="currentColor" stroke-width="2.2" stroke-dasharray="2.2 4.4" stroke-linecap="round" /></svg></span>
            ${TODO_HEAD_CHECK_ICON}
            ${TODO_CHEVRON_ICON}
          </span>
          <span class="todoTitle">To-dos</span>
          <span class="todoCount"></span>
        </button>
        <div class="todoCollapsible"><div class="todoInner"><ul class="todoList"></ul></div></div>
      ` : `
        <button type="button" class="line tool-chip-row">
          <span class="glyph tool-icon task-badge"></span>
          <span class="name"></span>
          <span class="detail"></span>
          <span class="timing"></span>
          <span class="filename"></span>
          <span class="agent-status"></span>
          <span class="card-chevron" aria-hidden="true">${CHEVRON_ICON}</span>
        </button>
        <div class="steps"></div>
        <div class="expand">
          <div class="expand-inner">
            <div class="fullpath"></div>
            <div class="agent-tools"></div>
            <div class="output"></div>
            <div class="review-actions"></div>
          </div>
        </div>
      `;
      const node = this.addRow(type === "plan" ? "tool-card aicss-todo todo expandable expanded" : "tool-card", markup, run?.body || this.transcript);
      node.dataset.activityKey = key;
      card = { node, state: {}, run };
      this.cards.set(key, card);
      if (run) {
        run.cards.add(key);
        this.updateToolRun(run);
      }
    }

    const state = card.state;
    const rawPhase = String(event.phase || state.phase || "started").toLowerCase();
    state.phase = PHASE_MAP[rawPhase]
      || (["started", "update", "completed", "failed"].includes(rawPhase) ? rawPhase : "completed");

    for (const field of [
      "activity_type", "title", "detail", "command", "cwd", "output", "summary", "diff", "error",
      "agent_name", "objective", "question", "context", "constraints", "attempts", "report", "advice",
    ]) {
      const value = event[field];
      if (value !== null && value !== undefined && value !== "") state[field] = String(value);
    }
    for (const field of ["lines_added", "lines_deleted", "exit_code", "duration_ms"]) {
      if (event[field] !== null && event[field] !== undefined) state[field] = event[field];
    }
    if (event.delta) {
      const target = { summary: "summary", plan: "detail" }[String(event.stream || "output")] || "output";
      state[target] = `${state[target] || ""}${event.delta}`;
    }
    if (Array.isArray(event.changes) && event.changes.length) {
      state.files = event.changes.map((change) => String(change.path || "")).filter(Boolean);
      state.diff = event.changes.map((change) => String(change.diff || "")).filter(Boolean).join("\n");
      state.fileDiffs = event.changes.map(fileDiffSummary).filter((change) => change.path);
    }
    for (const field of ["files", "steps", "findings", "unmet", "suggestions", "tool_calls", "transcript"]) {
      if (Array.isArray(event[field]) && event[field].length) state[field] = event[field];
    }
    if (event.verdict) state.verdict = String(event.verdict);
    if (card.run) {
      card.updatedOrder = ++card.run.sequence;
      this.updateToolRun(card.run);
    }

    if (card.run && Array.isArray(state.fileDiffs)) {
      for (const change of state.fileDiffs) {
        card.run.fileDiffs.set(change.path, change);
        this.recordTurnDiff(change, key);
      }
      this.updateToolRun(card.run);
    } else if (Array.isArray(state.fileDiffs)) {
      for (const change of state.fileDiffs) this.recordTurnDiff(change, key);
    }

    this.paintActivity(card);
  }

  paintPlan(card) {
    const { node, state } = card;
    const steps = Array.isArray(state.steps) ? state.steps : [];
    const completed = steps.filter((step) => String(step.status || "") === "completed").length;
    const activeIndex = steps.findIndex((step) => String(step.status || "") === "in_progress");
    const allDone = steps.length > 0 && completed === steps.length;
    const running = activeIndex >= 0 && !allDone;
    const pct = Math.round((Math.min(Math.max(completed, 0), steps.length || 1) / (steps.length || 1)) * 100);
    node.classList.toggle("plan-active", running);
    node.classList.toggle("plan-complete", allDone);
    node.style.setProperty("--todo-pie", `${pct}%`);
    node.querySelector(".todoHead")?.setAttribute("aria-expanded", String(node.classList.contains("expanded")));
    updateRollingCount(node.querySelector(".todoCount"), `${completed}/${steps.length}`);
    const icon = (markup, on) => on ? markup.replace('class="todoIcon', 'class="todoIcon on') : markup;
    node.querySelector(".todoList").innerHTML = steps.map((step, index) => {
      const status = String(step.status || "pending");
      const done = status === "completed";
      const active = status === "in_progress";
      const label = String(step.step || "");
      return `<li class="todoItem${done ? " done" : active ? " active" : ""}" style="--i:${index}">
        <span class="todoIconWrap">${icon(TODO_DASHED_ICON, !done && !active)}${icon(TODO_ARROW_ICON, active)}${icon(TODO_CHECK_ICON, done)}</span>
        <span class="todoLabel" data-label="${escapeHtml(label)}">${escapeHtml(label)}</span>
      </li>`;
    }).join("");
  }

  paintActivity(card) {
    const { node, state } = card;
    const rawType = String(state.activity_type || "");
    const type = rawType === "planner" ? "consultant" : rawType;
    if (type === "plan" && node.classList.contains("aicss-todo")) {
      this.paintPlan(card);
      return;
    }
    const busy = state.phase === "started" || state.phase === "update";
    node.classList.toggle("running", busy);
    node.classList.toggle("failed", state.phase === "failed");
    node.classList.toggle("is-consultant", type === "consultant");
    node.classList.toggle("is-subagent", type === "subagent");
    node.classList.toggle("is-review", type === "review");
    node.classList.toggle("is-file", type === "files" || type === "read");
    node.classList.toggle("task-row", type === "consultant" || type === "subagent" || type === "review");
    // Scouts and Consultants are live work streams: open them while work is
    // happening so their receipts are visible, then close them when the final
    // report arrives. The user can still reopen a settled card manually.
    const autoOpen = (type === "subagent" || type === "consultant") && busy;
    if (autoOpen) node.classList.add("expanded");
    else if ((type === "subagent" || type === "consultant") && !busy) node.classList.remove("expanded");
    if (type === "review" && busy) node.classList.add("expanded");
    if (type === "review" && state.verdict === "pass") node.classList.remove("failed");

    const roleType = type === "consultant" ? "C" : type === "subagent" ? "S" : type === "review" ? "R" : "";
    const glyph = node.querySelector(".glyph");
    const glyphMode = `${roleType || type}:${busy ? "busy" : state.phase}`;
    // Do not recreate animated glyph markup for every streamed tool receipt.
    // Replacing the spans resets their CSS animation phase and causes visible
    // churning stutter; only change the glyph when its semantic mode changes.
    if (glyph.dataset.mode !== glyphMode) {
      glyph.innerHTML = roleType
        ? taskBadgeMarkup(busy ? "started" : state.phase, roleType)
        : busy ? loadingPixelsMarkup() : toolIconMarkup(type);
      glyph.dataset.mode = glyphMode;
    }
    const consultantTitle = String(state.title || "Consultant").replace(/planner/gi, "Consultant");
    const visibleName = type === "consultant" && !/consultant/i.test(consultantTitle)
      ? `Consultant · ${consultantTitle}`
      : type === "consultant"
        ? consultantTitle
        : (state.title || state.agent_name || type || "Working");
    node.querySelector(".name").textContent = visibleName;

    // Files show only the basename in the collapsed line; the full path lives
    // in the expanded body next to an Explorer button.
    const paths = (state.files && state.files.length ? state.files : [state.detail]).filter(Boolean);
    const isFile = node.classList.contains("is-file");
    const detailText = isFile ? "" : type === "consultant"
      ? String(state.question || state.detail || (busy ? "Thinking through the hard part\u2026" : "Advice below"))
        .replace(/planner/gi, "consultant")
      : type === "subagent"
        ? (state.objective || state.detail || (busy ? "Exploring\u2026" : "Scout report below"))
        : (state.command || state.detail || (type === "review" ? state.summary : "") || "");
    node.querySelector(".detail").textContent = detailText;
    node.querySelector(".filename").textContent = isFile
      ? String(paths[0] || "").split(/[\\/]/).pop() || ""
      : "";

    // +added / -deleted as its own badge: bigger, and green/red rather than
    // buried in the grey timing text.
    const added = Number(state.lines_added || 0);
    const deleted = Number(state.lines_deleted || 0);
    // Sessions recorded before edits carried `changes` still have files, diff
    // and line counts on the event, so rebuild the row from those. Keyed on
    // this card rather than on the run being empty -- that older test skipped
    // every edit after the first, which is why a multi-file turn only ever
    // listed one file.
    if (card.run && type === "files" && !Array.isArray(state.fileDiffs) && (added || deleted)) {
      const path = String(paths[0] || "");
      if (path) {
        const change = {
          path,
          name: path.split(/[\\/]/).pop() || path,
          add: added,
          del: deleted,
          diff: String(state.diff || ""),
        };
        card.run.fileDiffs.set(path, change);
        this.recordTurnDiff(change, node.dataset.activityKey || "");
        this.updateToolRun(card.run);
      }
    }

    const bits = [];
    if (state.exit_code !== undefined && state.exit_code !== null && state.exit_code !== 0) bits.push(`exit ${state.exit_code}`);
    if (state.duration_ms) bits.push(`${(Number(state.duration_ms) / 1000).toFixed(1)}s`);
    node.querySelector(".timing").textContent = bits.join(" \u00b7 ");
    const agentStatus = node.querySelector(".agent-status");
    agentStatus.textContent = busy ? "Running" : state.phase === "failed" ? "Failed" : "Done";
    node.querySelector(".steps").innerHTML = "";

    const fullPath = isFile ? String(paths[0] || "") : "";
    node.querySelector(".fullpath").innerHTML = fullPath
      ? `<code>${escapeHtml(fullPath)}</code><button class="btn compact" data-reveal="${escapeHtml(fullPath)}">Open in Explorer</button>`
      : "";

    const sections = [];
    const listText = (rows) => (Array.isArray(rows) ? rows : []).map((row) => {
      if (typeof row === "string") return row;
      if (!row || typeof row !== "object") return String(row || "");
      return row.text || row.summary || row.title || row.name || JSON.stringify(row, null, 2);
    }).filter(Boolean).join("\n");
    const addSection = (label, value) => {
      const text = String(value || "").trim();
      if (!text || sections.some((row) => row.text === text)) return;
      sections.push({ label, text });
    };
    if (type === "subagent") {
      addSection("OBJECTIVE", state.objective);
      addSection("ACTIVITY", state.output);
      addSection("FINDINGS", listText(state.findings));
      addSection("REPORT", state.report || state.summary);
    } else if (type === "consultant") {
      addSection("QUESTION", state.question || state.objective);
      addSection("CONTEXT", state.context);
      addSection("CONSTRAINTS", state.constraints);
      addSection("ATTEMPTS", state.attempts);
      addSection("ACTIVITY", state.output);
      addSection("ADVICE", state.advice || state.report || state.summary);
    } else {
      addSection("", state.output);
    }
    addSection("DIFF", state.diff);
    addSection("ERROR", state.error);
    const body = sections.map(({ label, text }) => label ? `${label}\n${text}` : text).join("\n\n");
    node.querySelector(".output").textContent = body;
    const agentTools = node.querySelector(".agent-tools");
    const rawAgentTools = (type === "subagent" || type === "consultant")
      ? (Array.isArray(state.steps) && state.steps.length
        ? state.steps
        : Array.isArray(state.tool_calls) && state.tool_calls.length
          ? state.tool_calls
          : Array.isArray(state.transcript) ? state.transcript : [])
      : [];
    agentTools.innerHTML = rawAgentTools.map((row, index) => {
      const item = row && typeof row === "object" ? row : { result: String(row || "") };
      const tool = String(item.tool || item.name || item.title || `Tool ${index + 1}`);
      const argsValue = item.arguments ?? item.args ?? item.input ?? "";
      const resultValue = item.result ?? item.output ?? item.summary ?? "";
      const stringify = (value, pretty = false) => {
        if (typeof value === "string") return value;
        try { return JSON.stringify(value, null, pretty ? 2 : 0); }
        catch (_) { return String(value || ""); }
      };
      const args = stringify(argsValue).replace(/\s+/g, " ").trim();
      const result = stringify(resultValue, true).trim();
      const failed = item.failed === true || item.error;
      return `
        <details class="agent-tool-call${failed ? " failed" : ""}">
          <summary>
            <span class="agent-tool-chevron" aria-hidden="true">${CHEVRON_ICON}</span>
            <span class="agent-tool-label">${escapeHtml(tool)}</span>
            <span class="agent-tool-args">${escapeHtml(args)}</span>
          </summary>
          ${result ? `<pre class="agent-tool-result">${escapeHtml(result)}</pre>` : ""}
        </details>
      `;
    }).join("");
    agentTools.hidden = !rawAgentTools.length;
    const actions = node.querySelector(".review-actions");
    if (type === "review") {
      const suggestions = Array.isArray(state.suggestions) ? state.suggestions : [];
      const suggestionButtons = suggestions.map((row, index) => {
        const prompt = String(row?.prompt || "").trim();
        const label = String(row?.label || prompt).trim();
        if (!prompt) return "";
        return `<button type="button" class="btn compact review-suggest" data-review-index="${index}">${escapeHtml(label)}</button>`;
      }).join("");
      const fixButton = this.onReviewFix && String(state.verdict || "") === "concerns"
        ? '<button type="button" class="btn compact accent review-fix" data-review-fix>FIX</button>'
        : "";
      actions.innerHTML = suggestionButtons + fixButton;
      actions.hidden = !actions.innerHTML;
    } else {
      actions.innerHTML = "";
      actions.hidden = true;
    }
    node.classList.toggle("expandable", !!(body || fullPath || rawAgentTools.length || (type === "review" && !actions.hidden)));
    node.querySelector(".card-chevron")
      .setAttribute("aria-expanded", String(node.classList.contains("expanded")));
  }
}
