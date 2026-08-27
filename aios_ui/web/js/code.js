// The CODE tab.
//
// Push, not poll: two SSE streams (session list + selected transcript) replace
// the 900ms/250ms Tk timers. The transcript itself lives in transcript.js,
// because the BENCH page renders benchmark sessions with the same engine.

import { api, stream, native } from "./bridge.js";
import { escapeHtml } from "./markdown.js";
import { ACTIVE, Transcript, compactTokens, formatDuration, relativeTime } from "./transcript.js";
import { autosizePromptShell, promptConfigRowMarkup, promptShellMarkup } from "./chat_components.js";
import { ModelsWindow } from "./models.js";
import { CodeSplit } from "./code_split.js";

const PROVIDERS = [
  ["codex", "ChatGPT Codex"],
  ["claude", "Claude"],
  ["cursor", "Cursor"],
  ["ollama", "Ollama local"],
  ["openrouter", "OpenRouter"],
];

// Terminal outcomes that can show an unread dot until the operator opens them.
const SESSION_UNREAD_ON_SETTLE = new Set([
  "completed", "incomplete", "stopped", "interrupted", "failed", "error",
]);

const HIDDEN_SESSIONS_KEY = "aios:code-hidden-sessions";

function loadHiddenSessionIds() {
  try {
    const stored = JSON.parse(localStorage.getItem(HIDDEN_SESSIONS_KEY) || "[]");
    return new Set(Array.isArray(stored) ? stored.map(String).filter(Boolean) : []);
  } catch {
    return new Set();
  }
}

function saveHiddenSessionIds(ids) {
  try {
    localStorage.setItem(HIDDEN_SESSIONS_KEY, JSON.stringify([...ids]));
  } catch {
    // Hiding still works for this run when WebView storage is unavailable.
  }
}

const HIDDEN_PROJECTS_KEY = "aios:code-hidden-projects";
const STANDING_PROMPT_LIMIT = 4000;

function loadHiddenProjectKeys() {
  try {
    const stored = JSON.parse(localStorage.getItem(HIDDEN_PROJECTS_KEY) || "[]");
    return new Set(Array.isArray(stored) ? stored.map(String).filter(Boolean) : []);
  } catch {
    return new Set();
  }
}

function saveHiddenProjectKeys(keys) {
  try {
    localStorage.setItem(HIDDEN_PROJECTS_KEY, JSON.stringify([...keys]));
  } catch {
    // Hiding still works for this run when WebView storage is unavailable.
  }
}

/**
 * localStorage is scoped to the page origin, and aiOS's UI server binds a
 * fresh ephemeral port on every launch -- so each restart is a new origin and
 * localStorage comes back empty. The backend config file is the durable copy;
 * localStorage stays as an instant cache only.
 */
function hydrateHiddenFromConfig(hiddenSets, shell) {
  const storedProjects = (shell.config || {}).code_hidden_projects;
  if (Array.isArray(storedProjects)) {
    hiddenSets.projects = new Set(storedProjects.map(String).filter(Boolean));
  }
  const storedSessions = (shell.config || {}).code_hidden_sessions;
  if (Array.isArray(storedSessions)) {
    hiddenSets.sessions = new Set(storedSessions.map(String).filter(Boolean));
  }
}

function sessionDotClass(status, isUnread) {
  if (status === "running" || status === "queued") return "running";
  if (status === "waiting_user") return "waiting";
  if (status === "incomplete") return "incomplete";
  if (status === "failed" || status === "error") return isUnread ? "unread" : "error";
  if (status === "completed" || status === "stopped" || status === "interrupted") {
    return isUnread ? "unread" : "completed";
  }
  return "";
}

function sessionTitleTextEl(row) {
  const title = row.querySelector(".title");
  if (!title) return null;
  let text = title.querySelector(".title-text");
  if (!text) {
    text = document.createElement("span");
    text.className = "title-text";
    text.textContent = title.textContent;
    title.replaceChildren(text);
  }
  return text;
}

function objectValue(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function hasOwn(value, key) {
  return Object.prototype.hasOwnProperty.call(objectValue(value), key);
}

function numberValue(...values) {
  for (const value of values) {
    if (value === null || value === undefined || value === "") continue;
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return Math.max(0, parsed);
  }
  return 0;
}

function positiveValue(...values) {
  for (const value of values) {
    const parsed = Number(value);
    if (Number.isFinite(parsed) && parsed > 0) return parsed;
  }
  return 0;
}

function shortText(value, limit = 84) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, Math.max(0, limit - 1))}…` : text;
}

const FINISHED_ACTIVITY_PHASES = new Set([
  "completed", "failed", "cancelled", "canceled", "stopped", "interrupted", "incomplete",
]);

/** The most recent pipeline stage, preferring one that is still in flight. */
function currentPipelineStage(job) {
  const stages = Object.values(objectValue(objectValue(job).pipeline_stages))
    .filter((row) => row && typeof row === "object")
    .sort((a, b) => numberValue(a.started_at, a.updated_at) - numberValue(b.started_at, b.updated_at));
  const active = stages.filter((row) => {
    const phase = String(row.phase || "").toLowerCase();
    return !FINISHED_ACTIVITY_PHASES.has(phase) && !row.completed_at;
  });
  return active[active.length - 1] || stages[stages.length - 1] || {};
}

/** A concise action from an actual activity event, never from model narration. */
function activityAction(activity) {
  const row = objectValue(activity);
  const title = String(row.title || row.text || "").replace(/\s+/g, " ").trim();
  const detail = String(row.detail || row.command || "").replace(/\s+/g, " ").trim();
  const corpus = `${title} ${detail}`.toLowerCase();
  if (/luaparse|\bluac\b|lua.{0,18}(parse|syntax)/.test(corpus)) return "Parsing Lua syntax";
  if (/py_compile|compileall/.test(corpus)) return "Compiling Python";
  if (/\b(pytest|vitest|jest|npm test|pnpm test|cargo test|go test|dotnet test)\b/.test(corpus)) {
    return "Running focused tests";
  }
  if (/\b(tsc|eslint|ruff|mypy|lint|typecheck|syntax check|diagnostic)\b/.test(corpus)) {
    return "Checking diagnostics";
  }
  const generic = /^(run|ran) command$|^(read|search|edit|write)( file| files)?$/i.test(title);
  return shortText((generic && detail) ? detail : (title || detail), 118);
}

function workingState(stage, activity) {
  const stageName = String(objectValue(stage).stage || "").toLowerCase();
  const row = objectValue(activity);
  const type = String(row.activity_type || row.type || row.tool || "").toLowerCase();
  const corpus = `${type} ${row.title || ""} ${row.detail || ""} ${row.command || ""}`.toLowerCase();
  if (stageName.includes("consultant") || stageName.includes("planner") || type.includes("consult")) {
    return { key: "consulting", label: "Consulting" };
  }
  if (type.includes("plan")) return { key: "planning", label: "Planning" };
  if (stageName.includes("scout") || /search|read|outline|symbol|glob|find|list/.test(type)) {
    return { key: "finding", label: "Finding files" };
  }
  if (/edit|write|patch|diff|files/.test(type)) return { key: "editing", label: "Editing" };
  if (/review|verify|test|diagnostic|lint|check/.test(`${stageName} ${corpus}`)
      || (type.includes("command") && /test|verify|check|lint|compile|parse|build|diagnostic/.test(corpus))) {
    return { key: "checking", label: "Checking" };
  }
  if (stageName.includes("review")) return { key: "checking", label: "Checking" };
  return { key: "working", label: "Working" };
}

/**
 * Turn backend state into the one plain-language truth shown above the chat.
 * Model prose can say "done" early; only the job lifecycle can return Done.
 */
export function deriveSessionState(job, activity = {}) {
  const source = objectValue(job);
  const status = String(source.status || "").toLowerCase();
  const activeStarted = source.turn_started_at || source.started_at;
  const elapsed = Math.max(0, Math.floor(numberValue(
    ACTIVE.has(status) && activeStarted ? Date.now() / 1000 - Number(activeStarted) : null,
    source.elapsed_seconds,
    source.started_at && source.completed_at ? Number(source.completed_at) - Number(source.started_at) : null,
  )));
  const queued = numberValue(source.queued);
  const fallback = shortText(source.last_error || source.last_summary || "", 150);

  if (status === "waiting_user") {
    return {
      key: "waiting", label: "Waiting for you",
      detail: shortText(source.pending_question || "Answer the agent to keep this session moving.", 150),
      elapsed, queued,
    };
  }
  if (status === "failed" || status === "error") {
    return { key: "failed", label: "Failed", detail: fallback || "The session ended with an error.", elapsed, queued };
  }
  if (status === "stopped" || status === "interrupted") {
    const detail = status === "interrupted"
      ? (queued ? "The active turn was interrupted; the follow-up is queued next." : "The active turn was interrupted safely.")
      : "Stopped by you. Review the transcript for any unfinished checks.";
    return { key: "stopped", label: status === "interrupted" ? "Interrupted" : "Stopped", detail, elapsed, queued };
  }
  if (status === "incomplete") {
    return {
      key: "warning", label: "Done with warning",
      detail: shortText(fallback || "The agent stopped before it produced a complete result.", 150),
      elapsed, queued,
    };
  }
  if (status === "completed") {
    const verification = objectValue(source.verification);
    const verificationState = String(verification.state || "").toLowerCase();
    if (verificationState && verificationState !== "verified") {
      return {
        key: "warning",
        label: "Done · unverified",
        detail: shortText(verification.reason || fallback || "The session completed, but its changes were not fully verified.", 150),
        elapsed, queued,
      };
    }
    return {
      key: "done",
      label: "Done",
      detail: shortText(fallback || "The session completed.", 150),
      elapsed, queued,
    };
  }

  const stage = currentPipelineStage(source);
  const row = objectValue(activity);
  const stageActive = !!Object.keys(stage).length
    && !FINISHED_ACTIVITY_PHASES.has(String(stage.phase || "").toLowerCase())
    && !stage.completed_at;
  if (status === "queued" && !stageActive && !Object.keys(row).length) {
    return {
      key: "queued", label: "Queued",
      detail: queued > 1 ? `${queued} follow-ups are waiting.` : "Waiting to start the next turn.",
      elapsed, queued,
    };
  }
  const state = workingState(stage, row);
  const action = activityAction(row) || shortText(stage.detail || "", 118);
  const phase = String(row.phase || "").toLowerCase();
  const provider = String(source.provider || "").toLowerCase();
  const waitingOnModel = !action && !Object.keys(row).length;
  return {
    ...(waitingOnModel ? { key: "generating", label: "Generating" } : state),
    detail: action
      ? `${FINISHED_ACTIVITY_PHASES.has(phase) ? "Latest: " : ""}${action}`
      : (provider === "ollama"
        ? "Ollama is generating. Native tool arguments arrive as one buffered block; each completed file section will appear here."
        : "Waiting for the model's next response."),
    elapsed,
    queued,
  };
}

/** Normalize old and new job payloads without inventing missing telemetry. */
export function normalizeContextTelemetry(job) {
  const source = objectValue(job);
  const context = objectValue(source.context);
  const budget = objectValue(source.context_budget);
  const profile = objectValue(source.model_profile);
  const usage = objectValue(source.usage);
  const artifacts = Array.isArray(source.artifacts) ? source.artifacts : [];
  const hasEstimate = hasOwn(context, "used_tokens_est") || hasOwn(source, "used_tokens_est");
  const hasActual = hasOwn(context, "actual_input_tokens") || hasOwn(source, "actual_input_tokens");
  const hasCached = hasOwn(context, "cached_input_tokens") || hasOwn(usage, "cached_input_tokens");
  const hasWindow = hasOwn(context, "window_tokens") || hasOwn(budget, "window_tokens") || hasOwn(profile, "context_tokens");
  const hasWorking = hasOwn(context, "working_tokens") || hasOwn(budget, "working_tokens") || hasOwn(context, "budget_tokens_est");
  const hasReserve = hasOwn(context, "output_reserve_tokens") || hasOwn(budget, "output_reserve_tokens");
  const hasCompactions = hasOwn(context, "compactions") || hasOwn(source, "context_compactions");
  const hasArtifacts = hasOwn(context, "artifact_count") || Array.isArray(source.artifacts);

  const estimated = numberValue(context.used_tokens_est, source.used_tokens_est);
  const actual = numberValue(context.actual_input_tokens, source.actual_input_tokens);
  const cached = numberValue(context.cached_input_tokens, usage.cached_input_tokens);
  const windowTokens = positiveValue(
    context.window_tokens,
    budget.window_tokens,
    profile.context_tokens,
  );
  const workingTokens = positiveValue(
    context.working_tokens,
    budget.working_tokens,
    context.budget_tokens_est,
  );
  const reserveTokens = positiveValue(
    context.output_reserve_tokens,
    budget.output_reserve_tokens,
  );
  const current = actual || estimated;
  const denominator = workingTokens || numberValue(context.budget_tokens_est);
  const computedPercent = denominator ? Math.round((current / denominator) * 100) : 0;
  const percent = Math.max(
    0,
    Math.min(100, denominator ? computedPercent : numberValue(context.percent)),
  );
  const rawCompactions = context.compactions ?? source.context_compactions ?? 0;
  const compactions = Array.isArray(rawCompactions)
    ? rawCompactions.length
    : numberValue(rawCompactions);
  const artifactCount = Math.max(numberValue(context.artifact_count), artifacts.length);

  return {
    ...context,
    estimated_tokens: estimated,
    actual_input_tokens: actual,
    cached_input_tokens: cached,
    window_tokens: windowTokens,
    working_tokens: workingTokens,
    output_reserve_tokens: reserveTokens,
    current_tokens: current,
    current_is_estimate: !actual && !!estimated,
    has_estimate: hasEstimate,
    has_actual: hasActual,
    has_cached: hasCached,
    has_window: hasWindow,
    has_working: hasWorking,
    has_reserve: hasReserve,
    has_compactions: hasCompactions,
    has_artifacts: hasArtifacts,
    available: Object.keys(context).length > 0 || Object.keys(budget).length > 0,
    percent,
    compactions,
    artifact_count: artifactCount,
    usable: !!context.usable || denominator > 0,
    managed: String(context.managed || ""),
  };
}

export function normalizeHarnessTelemetry(job) {
  const source = objectValue(job);
  const rawStrategy = source.task_strategy;
  const strategy = objectValue(rawStrategy);
  const rawReasons = strategy.reasons ?? strategy.reason ?? source.strategy_reason;
  const reasons = Array.isArray(rawReasons)
    ? rawReasons.map((value) => String(value || "").trim()).filter(Boolean)
    : (rawReasons ? [String(rawReasons).trim()] : []);
  const profile = objectValue(source.model_profile);
  const verification = objectValue(source.verification);
  const progress = objectValue(source.progress);
  const plan = objectValue(source.task_plan);
  const steps = Array.isArray(plan.steps) ? plan.steps : [];
  const completedSteps = steps.filter((step) => String(objectValue(step).status || "") === "completed").length;
  const evidence = Array.isArray(verification.evidence) ? verification.evidence : [];

  return {
    strategy: {
      available: typeof rawStrategy === "string" || Object.keys(strategy).length > 0,
      name: String(strategy.name || (typeof rawStrategy === "string" ? rawStrategy : "")),
      reasons,
      score: numberValue(strategy.score),
      use_scout: !!strategy.use_scout,
      use_planner: !!strategy.use_planner,
      allow_subagents: !!strategy.allow_subagents,
      working_context_tokens: numberValue(strategy.working_context_tokens),
    },
    profile: {
      available: Object.keys(profile).length > 0 || !!source.model,
      model: String(profile.model || source.model || ""),
      edit_mode: String(profile.edit_mode || ""),
      tool_schema_mode: String(profile.tool_schema_mode || ""),
      context_mode: String(profile.context_mode || ""),
      context_tokens: numberValue(profile.context_tokens),
      conservative: !!profile.conservative,
    },
    verification: {
      ...verification,
      available: Object.keys(verification).length > 0,
      state: String(verification.state || ""),
      generation: numberValue(verification.generation),
      passing_evidence_count: numberValue(verification.passing_evidence_count),
      failing_evidence_count: numberValue(verification.failing_evidence_count),
      completion_blocks: numberValue(verification.completion_blocks),
      evidence_count: evidence.length,
      carried_change_count: numberValue(verification.carried_change_count),
    },
    progress: {
      ...progress,
      available: Object.keys(progress).length > 0,
      state: String(progress.state || ""),
      no_progress_calls: numberValue(progress.no_progress_calls),
      productive_calls: numberValue(progress.productive_calls),
      tool_calls: numberValue(progress.tool_calls),
      redirects: numberValue(progress.redirects),
      plan_completed: completedSteps,
      plan_total: steps.length,
    },
    context: normalizeContextTelemetry(source),
  };
}

export class CodeTab {
  constructor(host, shell) {
    this.host = host;
    this.shell = shell;
    this.jobs = [];
    this.selectedId = null;
    this.capabilities = { providers: [] };
    this.projects = [];
    this.roles = {};
    this.defaultRoles = {};
    this.modelConfigs = [];
    this.launchProvider = "openrouter";
    this.launchReviewFix = false;
    this.turnStrategy = "auto";
    this.activeConfigId = "";
    this.configBoundJob = null;
    this.modelsWindow = null;
    this.standingPrompt = String((this.shell.config || {}).code_standing_prompt || "")
      .slice(0, STANDING_PROMPT_LIMIT);
    this.standingPromptSaveTimer = null;
    this.standingPromptSaveChain = Promise.resolve();

    this.jobStream = null;
    this.eventStream = null;
    this.since = 0;

    // The session list is written by its own small rAF writer; the transcript
    // brings its own (see transcript.js).
    this.frame = null;
    this.fallback = null;
    this.sessionEls = new Map();
    this.dirtySessions = false;
    this.collapsed = {};
    this.unread = new Set();
    this.hiddenSessions = loadHiddenSessionIds();
    this.hiddenProjects = loadHiddenProjectKeys();
    // The backend config is durable across restarts; localStorage is not
    // (the UI server's origin changes every launch). Config wins when set.
    const hiddenSets = { sessions: this.hiddenSessions, projects: this.hiddenProjects };
    hydrateHiddenFromConfig(hiddenSets, this.shell);
    this.hiddenSessions = hiddenSets.sessions;
    this.hiddenProjects = hiddenSets.projects;
    this.hideMode = false;
    this.booted = false;   // first job batch should not mark sessions unread
    this.phoneShowHistory = false;
    this.usageWindow = null;
    this.activityState = new Map();
    this.currentActivity = null;
    this.activitySequence = 0;
    this.deliveryReceipts = new Map();

    this.render();
    this.connect();
  }

  /**
   * Torn down. Every `await api(...)` below has to check this before it touches
   * the DOM again.
   *
   * `show()` empties the page the moment you navigate, so a reply that lands a
   * moment later finds `el()` returning null and throws on the first `.value`.
   * That took the whole UI down with one toast and no other sign. The HARNESS
   * and BENCH buttons in this header make the race a single click wide, which
   * is how it finally showed up.
   */
  destroy() {
    this.gone = true;
    if (this.standingPromptSaveTimer) this.saveStandingPrompt();
    if (this.abort) this.abort.abort();
    if (this.clock) clearInterval(this.clock);
    if (this.jobStream) this.jobStream.close();
    if (this.eventStream) this.eventStream.close();
    if (this.split) this.split.destroy();
    if (this.view) this.view.destroy();
    if (this.frame) cancelAnimationFrame(this.frame);
    this.frame = null;
    this.clearFallback();
  }

  // ------------------------------------------------------------------ layout

  render() {
    this.host.innerHTML = `
      <div class="code-head">
        <h1>CODE</h1>
        <span class="providers">Codex &middot; Claude &middot; Cursor &middot; Ollama &middot; OpenRouter</span>
        <span class="spacer"></span>
        <button class="btn compact" data-code="harness" title="What this agent is made of: tools, models and limits">HARNESS</button>
        <button class="btn compact" data-code="bench" title="Measure this harness against a fixed set of tasks">BENCH</button>
        <button class="btn compact" data-code="reload-ui" title="Reload the CSS and HTML only (no backend restart)">Reload UI</button>
        <button class="icon-btn" data-code="refresh" title="Refresh agents and sessions">&#x21bb;</button>
      </div>

      <div class="code-overview">
        <span class="code-pill active" data-code="count-active">0 active</span>
        <span class="code-pill waiting" data-code="count-waiting">0 need you</span>
        <span class="code-pill done" data-code="count-done">0 finished</span>
        <span class="code-pill usage" data-code="usage" title="Spend over the last 28 days">28d &mdash;</span>
        <span class="code-health" data-code="health">Checking local agent logins and models&hellip;</span>
        <label class="check"><input type="checkbox" data-code="speak"> Speak milestones</label>
        <button class="btn compact" data-code="setup">Set up agent</button>
      </div>

      <div class="code-split">
        <button class="phone-sessions-backdrop" data-code="phone-sessions-close"
                aria-label="Close live sessions"></button>
        <section class="card code-sessions">
          <div class="code-sessions-head">
            <span>SESSIONS</span>
            <button type="button" class="icon-btn session-hide-toggle" data-code="hide-toggle"
                    aria-label="Edit hidden sessions" aria-pressed="false"
                    title="Edit hidden sessions">&#x270e;</button>
            <span class="phone-session-filters">
              <button class="btn compact active" data-code="phone-live">Live</button>
              <button class="btn compact" data-code="phone-history">History</button>
            </span>
          </div>
          <div class="code-sessions-list" data-code="sessions"></div>
        </section>

        <div class="code-panes">
          <div class="code-pane-row" data-code="pane-row">
            <section class="card code-detail">
          <div class="code-detail-head">
            <div class="pane-title-block main-pane-title">
              <span class="pane-title" data-code="main-pane-title">New session</span>
              <button type="button" class="pane-raw-toggle" data-code="raw-output"
                      aria-pressed="false" title="Show plain provider output and tool events">Raw</button>
              <button type="button" class="pane-title-action" data-code="add-pane"
                      title="Add a split pane (up to 3 sessions side by side)"
                      aria-label="Add a split pane">+</button>
            </div>
            <button class="phone-sessions-toggle icon-btn" data-code="phone-sessions"
                    aria-label="Open live sessions" title="Live sessions">&#9776;</button>
            <button class="phone-agent-toggle icon-btn" data-code="phone-agent"
                    aria-label="Open agent chat" title="Agent chat">A</button>
            <button class="context-ring" data-code="context-ring" hidden title="Context usage — click for details">
              <svg viewBox="0 0 36 36" aria-hidden="true">
                <circle class="track" cx="18" cy="18" r="15"></circle>
                <circle class="fill" data-code="context-fill" cx="18" cy="18" r="15"></circle>
              </svg>
              <span class="pct" data-code="context-pct">0%</span>
            </button>
            <div class="actions" data-code="detail-actions"></div>
          </div>
          <div class="context-panel" data-code="context-panel" hidden>
            <div class="context-panel-head">
              <strong>Session context</strong>
              <button class="icon-btn" data-code="context-close" title="Close">&#x2715;</button>
            </div>
            <div class="context-panel-body" data-code="context-body">Select a session to see context usage.</div>
            <div class="context-panel-actions">
              <button class="btn compact accent" data-code="context-compact">Compact context</button>
            </div>
          </div>

          <div class="code-session-state" data-code="session-state" data-state="idle" hidden
               role="status" aria-live="polite" aria-atomic="true">
            <span class="session-state-mark" aria-hidden="true"></span>
            <div class="session-state-copy">
              <strong data-code="state-label">Idle</strong>
              <span data-code="state-action"></span>
            </div>
            <span class="session-state-queue" data-code="state-queue" hidden></span>
            <span class="session-state-time" data-code="state-time"></span>
          </div>

          <div class="code-stats" data-code="stats" hidden>
            <span data-stat="elapsed">TIME &mdash;</span>
            <span data-stat="speed">TOK/S &mdash;</span>
            <span data-stat="tokens">TOKENS &mdash;</span>
            <span data-stat="cost">COST &mdash;</span>
            <span data-stat="files">FILES 0</span>
            <span class="diff" data-stat="diff">+0 / -0</span>
          </div>

          <div class="code-telemetry" data-code="telemetry" hidden
               aria-label="Session harness telemetry"></div>

          <div class="code-transcript-wrap">
            <div class="code-transcript" data-code="transcript"></div>
            <button class="scroll-bottom" data-code="jump">Jump to latest &darr;</button>
          </div>

          <div class="code-composer">
            <div class="prompt-menu prompt-plus-menu" data-code="plus-menu" hidden>
              <button type="button" class="prompt-menu-row" data-code="attach">
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21.4 11.6 12 21a6 6 0 0 1-8.5-8.5l9.4-9.4a4 4 0 0 1 5.7 5.7l-9.4 9.4a2 2 0 0 1-2.8-2.8l8.7-8.7"></path></svg>
                <span><strong>Attach</strong><small>Add a file to this message</small></span>
              </button>
              <button type="button" class="prompt-menu-row" data-code="handoff">
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17 1l4 4-4 4M3 11V9a4 4 0 0 1 4-4h14M7 23l-4-4 4-4m14-2v2a4 4 0 0 1-4 4H3"></path></svg>
                <span><strong>Handoff</strong><small>Pull in the latest external coding session</small></span>
              </button>
              <button type="button" class="prompt-menu-row" data-code="add-folder" data-code-project-row>
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 5h6l2 2h10v12H3z"></path></svg>
                <span><strong>Project</strong><small data-project-name></small></span>
              </button>
              <button type="button" class="prompt-menu-row" data-code="steer-now" data-code-queue-row hidden>
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"></path></svg>
                <span><strong>Steer now</strong><small>Interrupt this round and apply immediately</small></span>
              </button>
            </div>

            <div class="prompt-menu prompt-config-menu" data-code="config-menu" hidden>
              <div class="prompt-menu-label">Saved configurations</div>
              <div data-code="config-menu-list"></div>
              <div class="prompt-menu-separator"></div>
              <button type="button" class="prompt-menu-row prompt-settings-row" data-code="models">
                <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3A1.7 1.7 0 0 0 10 3V2.8h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1z"></path></svg>
                <span><strong>Settings</strong><small>Models, roles, reasoning and saved configs</small></span>
              </button>
            </div>

            ${promptShellMarkup({
              shellAttr: 'data-code="prompt-shell"',
              plusAttr: 'data-code="prompt-plus"',
              inputAttr: 'data-code="brief"',
              configAttr: 'data-code="prompt-config"',
              configNameAttr: "data-config-name",
              dictateAttr: 'data-code="dictate"',
              sendAttr: 'data-code="send"',
              sendTitle: "Launch a new CODE session",
            })}
            <div class="code-standing-prompt${this.standingPrompt.trim() ? " active" : ""}" data-code="standing-wrap">
              <div class="standing-prompt-panel" data-code="standing-panel" hidden>
                <div class="standing-prompt-head">
                  <label for="code-standing-prompt">Always append</label>
                  <span>Saved automatically</span>
                </div>
                <textarea id="code-standing-prompt" data-code="standing-prompt" rows="2"
                          maxlength="${STANDING_PROMPT_LIMIT}"
                          placeholder="Small instructions for every CODE message&hellip;">${escapeHtml(this.standingPrompt)}</textarea>
                <div class="standing-prompt-note">Added to every new session, continuation, model and configuration.</div>
              </div>
              <button type="button" class="standing-prompt-toggle" data-code="standing-toggle"
                      aria-label="Edit the prompt appended to every CODE message"
                      aria-expanded="false" title="Always-appended prompt">^</button>
            </div>
            <input type="hidden" data-code="project" value="">
            <div class="composer-delivery" data-code="delivery-receipt" role="status" aria-live="polite" hidden></div>
          </div>
        </section>
          </div>
        </div>
      </div>
    `;

    this.el = (name) => this.host.querySelector(`[data-code="${name}"]`);
    this.transcript = this.el("transcript");
    this.sessionsList = this.el("sessions");
    this.jumpEl = this.el("jump");

    this.view = new Transcript(this.transcript, {
      jump: this.jumpEl,
      isActive: () => ACTIVE.has(String((this.jobMeta || {}).status || "")),
      // Tool paths are relative to the session's project root.
      onReveal: (raw) => {
        const root = String((this.jobMeta || {}).cwd || "");
        const absolute = /^[a-zA-Z]:[\\/]|^\\\\/.test(raw) ? raw : `${root}\\${raw}`.replace(/\//g, "\\");
        native("open_path", absolute).then((ok) => {
          if (!ok) this.shell.toast(`Could not open ${absolute}`, "error");
        });
      },
      onReviewSuggest: (text) => {
        const brief = this.el("brief");
        brief.value = String(text || "");
        brief.dispatchEvent(new Event("input", { bubbles: true }));
        brief.focus();
        autosizePromptShell(this.el("prompt-shell"), brief);
      },
      onAnswer: (answer) => this.answerQuestion(answer),
    });

    const defaultProvider = this.shell.config.code_default_provider || "openrouter";
    this.launchProvider = PROVIDERS.some(([id]) => id === defaultProvider) ? defaultProvider : "openrouter";
    this.launchReviewFix = !!this.shell.config.code_review_fix_enabled;

    this.bind();

    // Split view: extra live panes beside the main detail pane. Owns its own
    // SSE streams, transcripts and composers; see code_split.js.
    this.split = new CodeSplit(this);
  }

  setLaunchProvider(provider) {
    const next = String(provider || "").trim().toLowerCase();
    this.launchProvider = PROVIDERS.some(([id]) => id === next) ? next : "openrouter";
    this.syncTarget();
  }

  setLaunchReviewFix(enabled) {
    this.launchReviewFix = !!enabled;
  }

  bind() {
    // Every listener is filed under one AbortController.
    //
    // This is the duplicate-send bug: listeners were bound to this.host, but
    // host is the persistent page element -- switching away from CODE and back
    // replaced its innerHTML while leaving the old listener attached, so the
    // second visit fired send() twice, the third three times. Aborting on
    // destroy() guarantees exactly one live handler per element.
    this.abort = new AbortController();
    const on = (target, type, handler) =>
      target.addEventListener(type, handler, { signal: this.abort.signal });

    on(this.host, "click", (event) => {
      const configPill = event.target.closest("[data-config-pill]");
      if (configPill) {
        this.closePromptMenus();
        this.applyModelConfig(configPill.dataset.configPill);
        return;
      }
      const groupHead = event.target.closest(".session-group-head");
      if (groupHead) {
        const key = groupHead.dataset.group;
        if (event.target.closest(".proj-info")) {
          // "i" hides/unhides the whole project; sessions stay on disk.
          if (this.hiddenProjects.has(key)) {
            this.hiddenProjects.delete(key);
          } else {
            this.hiddenProjects.add(key);
          }
          this.saveHiddenState();
          this.dirtySessions = true;
          this.schedule();
          return;
        }
        if (event.target.closest(".plus")) {
          // "+" starts a fresh session already pointed at that project.
          this.select(null);
          const path = this.projectPathForGroup(key);
          this.el("project").value = path;
          this.updateProjectName();
          if (path && !path.startsWith("@")) {
            api("/api/config", { method: "POST", body: { code_last_project_path: path } });
          }
          const detailTitle = this.el("detail-title");
          const detailMeta = this.el("detail-meta");
          if (detailTitle) detailTitle.textContent = "New session";
          if (detailMeta) detailMeta.textContent = "Describe what you want built, then press Launch.";
          this.syncTarget();
          this.el("brief").focus();
        } else {
          const body = groupHead.nextElementSibling;
          const isCollapsed = !!body?.classList.contains("collapsed");
          this.collapsed[key] = !isCollapsed;
          this.dirtySessions = true;
          this.schedule();
        }
        return;
      }

      const row = event.target.closest(".session-row");
      if (row) {
        if (this.hideMode) {
          const id = row.dataset.id;
          if (this.hiddenSessions.has(id)) {
            this.hiddenSessions.delete(id);
          } else {
            this.hiddenSessions.add(id);
          }
          this.saveHiddenState();
          this.dirtySessions = true;
          this.schedule();
          return;
        }
        // Split view: a click lands in the focused pane; with no split panes
        // open (or focus on the main pane) this is the normal select().
        if (!(this.split && this.split.routeRowActivate(row.dataset.id))) {
          this.select(row.dataset.id);
        }
        if (this.shell.phoneMirror) this.host.classList.remove("phone-sessions-open");
      }

      const trigger = event.target.closest("[data-code]");
      if (!trigger) return;
      const action = trigger.dataset.code;
      if (action === "refresh") { this.refreshCapabilities(true); this.refreshUsage(); }
      else if (action === "reload-ui") this.reloadUI();
      else if (action === "hide-toggle") this.toggleHideMode();
      else if (action === "send") this.send();
      else if (action === "steer-now") this.send("steer_now");
      else if (action === "prompt-plus") this.togglePromptMenu("plus");
      else if (action === "prompt-config") this.togglePromptMenu("config");
      else if (action === "standing-toggle") this.toggleStandingPrompt();
      else if (action === "dictate") this.shell.toast("Use your configured aiOS dictation hotkey to dictate here.", "info");
      else if (action === "bench") this.shell.show("BENCH");
      else if (action === "harness") this.shell.show("HARNESS");
      else if (action === "jump") this.view.setFollow(true);
      else if (action === "setup") this.setupProvider();
      else if (action === "add-folder") { this.closePromptMenus(); this.addFolder(); }
      else if (action === "models") { this.closePromptMenus(); this.openModels(); }
      else if (action === "attach") { this.closePromptMenus(); this.shell.toast("File attachments are not wired up in this build yet.", "info"); }
      else if (action === "handoff") { this.closePromptMenus(); this.handoff(); }
      else if (action === "stop") this.stopJob();
      else if (action === "undo") this.undoJob();
      else if (action === "delete") this.deleteJob();
      else if (action === "fast-mode") this.toggleFastMode();
      else if (action === "self-review") this.openSelfReview();
      else if (action === "usage") this.showUsage();
      else if (action === "context-ring") this.toggleContextPanel();
      else if (action === "context-close") this.hideContextPanel();
      else if (action === "context-compact") this.compactContext();
      else if (action === "raw-output") {
        const enabled = this.view.toggleRawMode();
        trigger.classList.toggle("active", enabled);
        trigger.setAttribute("aria-pressed", String(enabled));
        trigger.title = enabled ? "Show formatted transcript" : "Show plain provider output and tool events";
      }
      else if (action === "phone-sessions") this.host.classList.toggle("phone-sessions-open");
      else if (action === "phone-sessions-close") this.host.classList.remove("phone-sessions-open");
      else if (action === "phone-live") {
        this.phoneShowHistory = false;
        this.renderSessions();
      }
      else if (action === "phone-history") {
        this.phoneShowHistory = true;
        this.renderSessions();
      }
      else if (action === "phone-agent") {
        this.host.classList.remove("phone-sessions-open");
        document.documentElement.classList.toggle("phone-agent-open");
      }
    });

    on(document, "keydown", (event) => {
      if (event.key === "Escape") {
        this.closePromptMenus();
        if (this.split) this.split.closeContextMenu();
      }
      if (event.key === "Escape" && this.shell.phoneMirror) {
        this.host.classList.remove("phone-sessions-open");
        document.documentElement.classList.remove("phone-agent-open");
      }
    });

    const brief = this.el("brief");
    on(brief, "keydown", (event) => {
      // Enter sends, Shift+Enter makes a new line -- chat convention.
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        this.send();
      }
    });
    const autosizeBrief = () => autosizePromptShell(this.el("prompt-shell"), brief);
    on(brief, "input", autosizeBrief);

    const standingPrompt = this.el("standing-prompt");
    on(standingPrompt, "input", () => {
      this.standingPrompt = String(standingPrompt.value || "").slice(0, STANDING_PROMPT_LIMIT);
      this.shell.config.code_standing_prompt = this.standingPrompt;
      this.el("standing-wrap")?.classList.toggle("active", !!this.standingPrompt.trim());
      this.scheduleStandingPromptSave();
    });
    on(standingPrompt, "blur", () => {
      if (this.standingPromptSaveTimer) this.saveStandingPrompt();
    });

    on(document, "pointerdown", (event) => {
      if (!event.target.closest(".code-composer")) this.closePromptMenus();
    });

    on(this.el("speak"), "change", (event) => {
      api("/api/config", { method: "POST", body: { code_speak_notifications: event.target.checked } });
    });

    // Right-click a session row for split-view actions.
    on(this.sessionsList, "contextmenu", (event) => {
      const row = event.target.closest(".session-row");
      if (!row || this.hideMode || !this.split) return;
      event.preventDefault();
      this.split.showContextMenu(event.clientX, event.clientY, row.dataset.id);
    });
  }

  // ----------------------------------------------------------------- streams

  connect() {
    this.jobStream = stream(() => "/sse/code/jobs?limit=250", {
      jobs: (payload) => {
        const incoming = payload.jobs || [];
        // First batch — mark nothing unread; the user asked to start fresh.
        if (this.booted) {
          // Detect newly-settled sessions to mark them unread. An incomplete
          // run is intentionally terminal, but it still needs the operator's eye.
          const oldMap = new Map(this.jobs.map((j) => [String(j.id), j]));
          for (const job of incoming) {
            const id = String(job.id || "");
            if (!id) continue;
            const old = oldMap.get(id);
            const oldStatus = String(old ? old.status || "" : "");
            const newStatus = String(job.status || "");
            if (SESSION_UNREAD_ON_SETTLE.has(newStatus)
                && newStatus !== oldStatus && !(this.split && this.split.isOpen(id))) {
              this.unread.add(id);
            }
            if (this.split ? this.split.isOpen(id) : id === this.selectedId) this.unread.delete(id);
          }
        }
        this.booted = true;
        this.jobs = incoming;
        if (this.split) this.split.restorePending();
        if (this.shell.phoneMirror && !this.selectedId) {
          const live = incoming.find((job) => ACTIVE.has(String(job.status || "")));
          if (live && live.id) this.select(String(live.id));
        }
        this.dirtySessions = true;
        this.schedule();
      },
    });
    this.refreshCapabilities(false);
    this.refreshRoles();
    this.refreshModelConfigs();
    this.refreshProjects();
    this.refreshUsage();
    api("/api/config").then((result) => {
      if (!result.ok) return;
      this.el("speak").checked = result.config.code_speak_notifications !== false;
    });
  }

  select(jobId) {
    if (jobId) this.unread.delete(jobId);
    if (jobId === this.selectedId) {
      this.dirtySessions = true;
      this.schedule();
      return;
    }
    this.selectedId = jobId;
    this.setTurnStrategy("auto");
    this.since = 0;
    if (this.eventStream) this.eventStream.close();
    this.eventStream = null;

    this.view.reset();
    this.view.pinToEnd();   // opening a session always lands on the latest message
    this.dirtySessions = true;
    this.schedule();

    this.jobMeta = null;
    this.configBoundJob = null;
    this.activityState.clear();
    this.currentActivity = null;
    this.activitySequence = 0;
    if (this.clock) { clearInterval(this.clock); this.clock = null; }

    if (!jobId) {
      this.resetLaunchConfiguration();
      const detailTitle = this.el("detail-title");
      const detailMeta = this.el("detail-meta");
      if (detailTitle) detailTitle.textContent = "Select a session";
      if (detailMeta) detailMeta.textContent = "Current conversation, questions, and tool outputs appear here.";
      this.el("detail-actions").innerHTML = "";
      this.el("stats").hidden = true;
      this.el("telemetry").hidden = true;
      this.el("session-state").hidden = true;
      this.hideContextPanel();
      const ring = this.el("context-ring");
      if (ring) ring.hidden = true;
      this.syncTarget();
      return;
    }
    this.eventStream = stream(
      () => `/sse/code/events?job=${encodeURIComponent(jobId)}&since=${this.since}`,
      {
        events: (payload) => {
          if (this.selectedId !== jobId) return;
          this.since = payload.size || this.since;
          if (payload.job) {
            this.jobMeta = payload.job;
            this.dirtyMeta = true;
            this.schedule();
          }
          this.absorbActivities(payload.events || []);
          if (this.jobMeta && (payload.events || []).length) {
            this.dirtyMeta = true;
            this.schedule();
          }
          this.view.push(payload.events);
        },
        reset: () => {
          if (this.selectedId !== jobId) return;
          this.since = 0;
          this.view.reset();
          this.activityState.clear();
          this.currentActivity = null;
          this.activitySequence = 0;
        },
        job: (payload) => {
          if (this.selectedId !== jobId) return;
          this.jobMeta = payload.job;
          this.dirtyMeta = true;
          this.schedule();
        },
      },
    );
  }

  /** Keep one bounded, coalesced view of the latest real tool activity. */
  absorbActivities(events) {
    for (const event of events || []) {
      const kind = String(event.kind || "").toLowerCase();
      if (kind !== "activity" && kind !== "tool") continue;
      const type = String(event.activity_type || event.type || event.tool || "").toLowerCase();
      if (type === "thinking" || type === "stage") continue;
      const id = String(event.activity_id || event.tool_call_id || event.call_id
        || `${kind}-${event.ts || this.activitySequence + 1}`);
      const previous = this.activityState.get(id) || {};
      this.activityState.set(id, { ...previous, ...event, _seen: ++this.activitySequence });
    }
    const rows = [...this.activityState.values()].sort((a, b) => numberValue(a._seen) - numberValue(b._seen));
    if (rows.length > 300) {
      this.activityState = new Map(rows.slice(-300).map((row) => [
        String(row.activity_id || row.tool_call_id || row.call_id || row._seen), row,
      ]));
    }
    const meaningful = rows.filter((row) => {
      const type = String(row.activity_type || row.type || row.tool || "").toLowerCase();
      return type !== "thinking" && type !== "stage";
    });
    const active = meaningful.filter((row) => !FINISHED_ACTIVITY_PHASES.has(String(row.phase || "").toLowerCase()));
    this.currentActivity = active[active.length - 1] || meaningful[meaningful.length - 1] || null;
  }

  // -------------------------------------------------------- the rAF writer

  /**
   * Ask for a flush of the session list.
   *
   * rAF is the right clock while the window is on screen, but aiOS is an
   * overlay -- it spends most of its life hidden, and a hidden window does not
   * composite, so rAF never fires. The timer keeps state moving while hidden
   * (DOM mutation is still legal, it just is not painted); whichever fires
   * first wins.
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
    if (this.dirtySessions) {
      this.dirtySessions = false;
      this.renderSessions();
      this.renderCounters();
    }
    if (this.dirtyMeta) {
      this.dirtyMeta = false;
      this.renderDetailMeta();
    }
    if (this.split) this.split.syncTabs();
  }

  // -------------------------------------------------------------- sessions

  /**
   * Sessions grouped by project, newest-created group first, each collapsible with a
   * "+" that starts a session in that project -- the Tk tree, rebuilt.
   */
  renderSessions() {
    const createdStamp = (job) => Number(job.created_at || job.updated_at || 0);
    const visibleJobs = this.shell.phoneMirror
      ? this.jobs.filter((job) => this.phoneShowHistory
        ? !ACTIVE.has(String(job.status || ""))
        : ACTIVE.has(String(job.status || "")))
      : this.jobs.filter((job) => this.hideMode || !this.hiddenSessions.has(String(job.id || "")));
    const liveButton = this.el("phone-live");
    const historyButton = this.el("phone-history");
    if (liveButton) liveButton.classList.toggle("active", !this.phoneShowHistory);
    if (historyButton) historyButton.classList.toggle("active", this.phoneShowHistory);
    if (!visibleJobs.length) {
      this.sessionsList.innerHTML =
        `<div class="placeholder" style="font-size:11px;padding:20px 8px">${
          this.shell.phoneMirror
            ? (this.phoneShowHistory ? "No session history yet." : "No live sessions right now.")
            : (this.hiddenSessions.size
              ? "All sessions are hidden.<br>Use the pen to edit hidden sessions."
              : "No sessions yet.<br>Launch one here or ask the voice agent.")
        }</div>`;
      this.sessionEls.clear();
      return;
    }

    const groups = new Map();
    for (const job of visibleJobs) {
      const groupLabel = String(job.sidebar_group || "").trim();
      const key = groupLabel ? `@${groupLabel}` : String(job.cwd || job.project_name || "Other");
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(job);
    }
    const names = new Map(
      (this.projects || []).map((project) => [String(project.path || "").toLowerCase(), String(project.name || "")]),
    );
    let ordered = [...groups.entries()].sort((a, b) => {
      const aReview = a[0].startsWith("@");
      const bReview = b[0].startsWith("@");
      if (aReview !== bReview) return aReview ? 1 : -1;
      return Math.max(...b[1].map(createdStamp)) - Math.max(...a[1].map(createdStamp));
    });
    // Hidden projects drop out of the rail entirely; the pen (hide mode) brings
    // every group back so they can be unhidden with the same "i" button.
    if (!this.hideMode) {
      ordered = ordered.filter(([key]) => !this.hiddenProjects.has(key));
    }
    if (!ordered.length) {
      this.sessionsList.innerHTML =
        `<div class="placeholder" style="font-size:11px;padding:20px 8px">All projects are hidden.<br>Use the pen to edit hidden projects.</div>`;
      this.sessionEls.clear();
      return;
    }

    const fragment = document.createDocumentFragment();
    const seen = new Set();

    for (const [groupIndex, [key, jobs]] of ordered.entries()) {
      const hasLiveSession = jobs.some((job) => ACTIVE.has(String(job.status || "")));
      const phoneDefaultOpen = this.shell.phoneMirror
        && (this.phoneShowHistory ? groupIndex === 0 : hasLiveSession);
      const collapsed = this.collapsed[key] == null
        ? !phoneDefaultOpen
        : this.collapsed[key] !== false;
      const virtualGroup = key.startsWith("@");
      const projectHidden = !virtualGroup && this.hiddenProjects.has(key);
      const label = virtualGroup ? key.slice(1) : (names.get(key.toLowerCase())
        || String(jobs[0].project_name || key.split(/[\\/]/).filter(Boolean).pop() || key));

      const head = document.createElement("div");
      head.className = `session-group-head${projectHidden ? " hidden-project" : ""}`;
      head.dataset.group = key;
      // The "i" (hide project) button only exists while pen/edit mode is on.
      const infoButton = this.hideMode && !virtualGroup
        ? `<span class="proj-info${projectHidden ? " active" : ""}" title="${projectHidden ? "Unhide this project" : "Hide this project"}">i</span>`
        : "";
      head.innerHTML = `<span class="arrow">${collapsed ? "\u25b6" : "\u25bc"}</span>`
        + `<span class="label" title="${escapeHtml(key)}">${escapeHtml(label.toUpperCase())}</span>`
        + (virtualGroup ? "" : '<span class="plus" title="New session in this project">+</span>')
        + infoButton;
      fragment.appendChild(head);

      const body = document.createElement("div");
      body.className = `session-group-body${collapsed ? " collapsed" : ""}`;
      for (const job of jobs.sort((a, b) => createdStamp(b) - createdStamp(a))) {
        const id = String(job.id || "");
        if (!id) continue;
        seen.add(id);
        let node = this.sessionEls.get(id);
        if (!node) {
          node = document.createElement("button");
          node.className = "session-row";
          node.dataset.id = id;
          node.innerHTML = '<span class="dot"></span><span class="text-block"><span class="title"><span class="title-text"></span></span><span class="meta"></span></span>';
          // Draggable into a split pane (or pane tab). The id rides on the
          // custom drag type that code_split.js drop targets read back.
          node.draggable = true;
          node.addEventListener("dragstart", (event) => {
            event.dataTransfer.setData("text/code-session", id);
            event.dataTransfer.effectAllowed = "copy";
            if (this.split) this.split.setDragSession(id);
          });
          node.addEventListener("dragend", () => {
            if (this.split) this.split.setDragSession(null);
          });
          this.sessionEls.set(id, node);
        }
        const status = String(job.status || "");
        const isUnread = this.unread.has(id);
        node.querySelector(".dot").className = `dot ${sessionDotClass(status, isUnread)}`;
        // Titles arrive as "OpenRouter \u00b7 do the thing". The provider is already
        // on the meta line below, so the headline keeps just the chat name.
        const title = String(job.title || job.brief || "Untitled session").replace(/^\s*[^\u00b7]{1,20}\u00b7\s*/, "");
        const titleEl = sessionTitleTextEl(node);
        if (titleEl) titleEl.textContent = title || "Untitled session";
        node.querySelector(".meta").textContent =
          `${String(job.provider || "").toUpperCase()} \u00b7 ${status || "idle"} \u00b7 ${relativeTime(job.updated_at)}`;
        node.classList.toggle("selected", id === this.selectedId);
        // Working sessions shimmer, so a glance at the rail shows what is live.
        node.classList.toggle("live", ACTIVE.has(status));
        node.classList.toggle("hidden-session", this.hiddenSessions.has(id));
        body.appendChild(node);
      }
      fragment.appendChild(body);
    }

    for (const [id, node] of this.sessionEls) {
      if (!seen.has(id)) {
        node.remove();
        this.sessionEls.delete(id);
      }
    }
    this.sessionsList.replaceChildren(fragment);
  }

  renderCounters() {
    const count = (predicate) => this.jobs.filter(predicate).length;
    const done = count((j) => j.status === "completed");
    const settled = count((j) => ["completed", "incomplete", "failed", "error", "stopped", "interrupted"].includes(j.status));
    this.el("count-active").textContent = `${count((j) => j.status === "queued" || j.status === "running")} active`;
    this.el("count-waiting").textContent = `${count((j) => j.status === "waiting_user" || j.status === "incomplete")} need you`;
    this.el("count-done").textContent = `${done} finished`;
    // usage_window() walks every job file, so recompute only when a session
    // actually settles -- never on the streaming path.
    if (this.lastDone !== settled) {
      this.lastDone = settled;
      this.refreshUsage();
    }
  }

  renderDetailMeta() {
    const job = this.jobMeta;
    if (!job) return;
    this.syncSelectedConfiguration(job);
    const detailTitle = this.el("detail-title");
    if (detailTitle) detailTitle.textContent = job.title || job.brief || "Session";
    const fast = job.fast ? " \u00b7 fast" : "";
    const verificationState = String(objectValue(job.verification).state || "").toLowerCase();
    const statusLabel = job.status === "completed" && verificationState && verificationState !== "verified"
      ? `completed · ${verificationState}`
      : job.status;
    const detailMeta = this.el("detail-meta");
    if (detailMeta) detailMeta.textContent =
      `${String(job.provider || "").replace(/^./, (c) => c.toUpperCase())} \u00b7 ${statusLabel} \u00b7 ${job.model} / ${job.reasoning}${fast}\n${job.cwd || ""}`;

    const active = ACTIVE.has(String(job.status || ""));
    const reviewable = !active && job.session_kind !== "review";
    const undoable = Number(job.undoable_files || 0) > 0;
    this.el("detail-actions").innerHTML = `
      ${reviewable ? '<button class="btn compact accent" data-code="self-review" title="Audit the complete session with another model configuration">Self review</button>' : ""}
      <button class="btn compact" data-code="undo"${(!active && undoable) ? "" : " disabled"} title="Restore every file this session changed back to how it was before the agent edited it">Undo</button>
      <button class="btn compact ghost" data-code="delete"${active ? " disabled" : ""}>Delete</button>
      <button class="btn compact" data-code="stop"${active ? "" : " disabled"}>Stop</button>
    `;

    this.renderSessionState(job);
    this.renderStats(job);
    this.renderHarnessTelemetry(job);
    this.renderContext(job);
    this.syncTarget();
  }

  /** One authoritative lifecycle line; streamed assistant prose never drives it. */
  renderSessionState(job) {
    const panel = this.el("session-state");
    if (!panel || !job) return;
    const state = deriveSessionState(job, this.currentActivity || {});
    const status = String(job.status || "").toLowerCase();
    this.view?.setWorking(
      status === "running" || status === "queued",
      state.detail || "The agent is working.",
      Number(job.turn_started_at || job.started_at || job.created_at || 0),
      state.label || "Generating",
    );
    panel.hidden = false;
    panel.dataset.state = state.key;
    this.el("state-label").textContent = state.label;
    this.el("state-action").textContent = state.detail || "";
    this.el("state-time").textContent = formatDuration(state.elapsed);
    const queue = this.el("state-queue");
    queue.hidden = !state.queued;
    queue.textContent = state.queued ? `${state.queued} queued` : "";
    const receipt = this.deliveryReceipts.get(String(job.id || ""));
    queue.title = receipt && receipt.preview ? `Next: ${receipt.preview}` : "Follow-ups waiting after the current turn";
  }

  syncSelectedConfiguration(job) {
    const id = String(job.id || "");
    if (!id || this.configBoundJob === id) return;
    this.configBoundJob = id;
    if (job.role_config && typeof job.role_config === "object") {
      this.roles = JSON.parse(JSON.stringify(job.role_config));
    }
    this.launchProvider = String(job.provider || this.launchProvider || "openrouter").toLowerCase();
    this.activeConfigId = String(job.config_id || "");
    this.refreshSelectors();
  }

  renderHarnessTelemetry(job) {
    const strip = this.el("telemetry");
    if (!strip) return;
    if (!job) {
      strip.hidden = true;
      strip.innerHTML = "";
      return;
    }

    const telemetry = normalizeHarnessTelemetry(job);
    const strategy = telemetry.strategy;
    const profile = telemetry.profile;
    const progress = telemetry.progress;
    const context = telemetry.context;
    const count = (value) => compactTokens(Math.round(numberValue(value)));
    const bits = (...values) => values.filter(Boolean).join(" · ");
    const cell = (key, label, value, detail, title, state = "") => `
      <div class="code-telemetry-cell" data-telemetry="${key}" data-state="${escapeHtml(state)}"
           title="${escapeHtml(title || detail || value)}">
        <span class="telemetry-key">${label}</span>
        <strong>${escapeHtml(value || "—")}</strong>
        <span class="telemetry-detail">${escapeHtml(detail || "No data")}</span>
      </div>
    `;

    const strategyFlags = [
      strategy.use_scout ? "scout" : "",
      strategy.use_planner ? "consultant" : "",
      strategy.allow_subagents ? "subagents" : "",
      strategy.score ? `score ${strategy.score.toFixed(2)}` : "",
      strategy.working_context_tokens ? `${count(strategy.working_context_tokens)} work tok` : "",
    ].filter(Boolean);
    const strategyReason = strategy.available
      ? (strategy.reasons[0] || strategyFlags.join(" · "))
      : "";
    const strategyTitle = bits(strategy.reasons.join("; "), strategyFlags.join(" · "));

    const profileModes = [
      profile.edit_mode ? `edit ${profile.edit_mode}` : "",
      profile.tool_schema_mode ? `schema ${profile.tool_schema_mode}` : "",
      profile.context_mode ? `ctx ${profile.context_mode}` : "",
      profile.conservative ? "conservative" : "",
    ].filter(Boolean);
    const profileTitle = bits(
      profileModes.join(" · "),
      profile.context_tokens ? `${count(profile.context_tokens)} context tok` : "",
    );

    const progressValue = progress.state
      ? progress.state.replaceAll("_", " ").toUpperCase()
      : (progress.no_progress_calls ? "NO PROGRESS" : "—");
    const progressDetail = progress.available
      ? bits(
          `${progress.productive_calls} useful`,
          `${progress.no_progress_calls} idle`,
          `${progress.tool_calls} tools`,
          progress.redirects ? `${progress.redirects} redirects` : "",
          progress.plan_total ? `plan ${progress.plan_completed}/${progress.plan_total}` : "",
        )
      : "";
    const progressTitle = bits(progress.blocked_reason, progressDetail);

    const contextValue = context.has_working && context.working_tokens
      ? `${context.percent}%`
      : (context.has_actual
          ? `${count(context.actual_input_tokens)} actual`
          : context.has_estimate ? `~${count(context.estimated_tokens)}` : "—");
    const contextDetail = bits(
      context.has_actual ? `${count(context.actual_input_tokens)} actual` : "",
      context.has_estimate ? `~${count(context.estimated_tokens)} est` : "",
      context.has_working ? `${count(context.working_tokens)} work` : "",
      context.has_cached ? `${count(context.cached_input_tokens)} cache` : "",
      context.has_reserve ? `${count(context.output_reserve_tokens)} reserve` : "",
      context.has_compactions ? `C${context.compactions}` : "",
      context.has_artifacts ? `A${context.artifact_count}` : "",
    );
    const contextTitle = bits(
      contextDetail,
      context.has_window ? `${count(context.window_tokens)} model window` : "",
      context.hint,
    );

    strip.innerHTML = [
      cell(
        "strategy",
        "TASK",
        strategy.name === "auto" ? "CODER-LED" : strategy.name.toUpperCase(),
        shortText(strategyReason),
        strategyTitle,
        strategy.name,
      ),
      cell("profile", "PROFILE", shortText(profile.model, 36), profileModes.join(" · "), profileTitle),
      cell("progress", "PROGRESS", progressValue, progressDetail, progressTitle, progress.state),
      cell("context", "CONTEXT", contextValue, contextDetail, contextTitle, context.percent >= 90 ? "hot" : context.percent >= 70 ? "warn" : ""),
    ].join("");
    strip.hidden = false;
  }

  /** Hollow ring that fills as the owned message history approaches its budget. */
  renderContext(job) {
    const ring = this.el("context-ring");
    const fill = this.el("context-fill");
    const pctEl = this.el("context-pct");
    if (!ring || !fill || !pctEl) return;
    const hasContext = !!(job && job.context && typeof job.context === "object");
    const hasBudget = !!(job && job.context_budget && typeof job.context_budget === "object");
    if (!job || (!hasContext && !hasBudget)) {
      ring.hidden = true;
      this.hideContextPanel();
      return;
    }
    const ctx = normalizeContextTelemetry(job);
    ring.hidden = false;
    const usable = !!ctx.usable;
    const percent = usable ? Math.max(0, Math.min(100, Number(ctx.percent) || 0)) : 0;
    const circumference = 2 * Math.PI * 15;
    fill.style.strokeDasharray = `${circumference}`;
    fill.style.strokeDashoffset = `${circumference * (1 - percent / 100)}`;
    ring.classList.toggle("warn", usable && percent >= 70 && percent < 90);
    ring.classList.toggle("hot", usable && percent >= 90);
    ring.classList.toggle("external", !usable);
    pctEl.textContent = usable ? `${percent}%` : "ext";
    ring.title = usable
      ? `Context ${percent}% full — click for details / compact`
      : (ctx.hint || "Provider manages its own context");
    if (!this.el("context-panel").hidden) this.renderContextPanel(ctx);
  }

  toggleContextPanel() {
    const panel = this.el("context-panel");
    if (!panel) return;
    if (!panel.hidden) {
      this.hideContextPanel();
      return;
    }
    const job = this.jobMeta;
    if (!job || (!job.context && !job.context_budget)) {
      this.shell.toast("No session context yet.");
      return;
    }
    const ctx = normalizeContextTelemetry(job);
    panel.hidden = false;
    this.renderContextPanel(ctx);
  }

  hideContextPanel() {
    const panel = this.el("context-panel");
    if (panel) panel.hidden = true;
  }

  renderContextPanel(ctx) {
    const body = this.el("context-body");
    const button = this.el("context-compact");
    if (!body || !button) return;
    const hasAccounting = !!(
      ctx.has_actual
      || ctx.has_estimate
      || ctx.has_cached
      || ctx.has_window
      || ctx.has_working
      || ctx.has_reserve
    );
    if (!ctx.usable && !hasAccounting) {
      body.innerHTML = `<p class="muted">${escapeHtml(ctx.hint || "This provider manages its own context window.")}</p>`;
      button.disabled = true;
      return;
    }
    const fmt = (n) => Number(n || 0).toLocaleString();
    const breakdown = ctx.breakdown || {};
    const rows = [
      ["Current actual", `${fmt(ctx.actual_input_tokens)} tok`, ctx.has_actual],
      ["Current estimate", `~${fmt(ctx.estimated_tokens)} tok`, ctx.has_estimate],
      ["Working budget", `${fmt(ctx.working_tokens)} tok · ${ctx.percent}% used`, ctx.has_working],
      ["Model window", `${fmt(ctx.window_tokens)} tok`, ctx.has_window],
      ["Output reserve", `${fmt(ctx.output_reserve_tokens)} tok`, ctx.has_reserve],
      ["Cached input", `${fmt(ctx.cached_input_tokens)} tok`, ctx.has_cached],
      ["Compactions", fmt(ctx.compactions), ctx.has_compactions],
      ["Artifacts", fmt(ctx.artifact_count), ctx.has_artifacts],
      ["Messages", fmt(ctx.messages), hasOwn(ctx, "messages")],
      ["Characters", ctx.budget_chars ? `${fmt(ctx.used_chars)} / ${fmt(ctx.budget_chars)}` : fmt(ctx.used_chars), hasOwn(ctx, "used_chars")],
      ["System / user", `${fmt(breakdown.system)} / ${fmt(breakdown.user)} chars`, !!(breakdown.system || breakdown.user)],
      ["Assistant / tools", `${fmt(breakdown.assistant)} / ${fmt(breakdown.tool)} chars`, !!(breakdown.assistant || breakdown.tool)],
    ].filter((row) => row[2]);
    body.innerHTML = `
      <p>Provider totals are exact. A leading ~ marks aiOS's current estimate.</p>
      <dl class="context-dl">
        ${rows.map(([k, v]) => `<div><dt>${escapeHtml(k)}</dt><dd>${escapeHtml(String(v))}</dd></div>`).join("")}
      </dl>
    `;
    button.disabled = ctx.managed !== "aios";
  }

  async compactContext() {
    const id = this.selectedId;
    if (!id) return;
    const button = this.el("context-compact");
    if (button) {
      button.disabled = true;
      button.textContent = "Compacting…";
    }
    const result = await api(`/api/code/jobs/${id}/compact`, { method: "POST", body: { force: true } });
    if (button) {
      button.disabled = false;
      button.textContent = "Compact context";
    }
    if (!result || result.ok === false) {
      this.shell.toast((result && result.error) || "Compact failed", "error");
      return;
    }
    if (result.skipped) {
      this.shell.toast(result.message || "Nothing to compact");
    } else {
      this.shell.toast(`Compacted — freed ${Number(result.saved_chars || 0).toLocaleString()} chars`);
    }
    if (result.context && this.jobMeta) {
      this.jobMeta.context = result.context;
      this.renderContext(this.jobMeta);
      this.renderHarnessTelemetry(this.jobMeta);
    }
    const detail = await api(`/api/code/jobs/${id}`);
    if (detail && detail.ok && detail.job) {
      this.jobMeta = detail.job;
      this.renderDetailMeta();
    }
  }

  /** TIME / TOK/S / TOKENS / COST / FILES / +added -deleted. */
  renderStats(job) {
    const strip = this.el("stats");
    strip.hidden = false;
    const active = ACTIVE.has(String(job.status || ""));
    const started = Number(job.started_at || job.created_at || 0);
    const elapsed = active && started
      ? Math.max(0, Math.floor(Date.now() / 1000 - started))
      : Math.max(0, Math.floor(Number(job.elapsed_seconds
          || (started ? Number(job.completed_at || 0) - started : 0)) || 0));

    const usage = (job.usage && typeof job.usage === "object") ? job.usage : {};
    const tokens = Number(usage.total_tokens || 0);
    const speed = job.tokens_per_second;
    const cost = Number(job.estimated_cost_usd || usage.cost_usd || 0);

    const set = (name, value) => { strip.querySelector(`[data-stat="${name}"]`).textContent = value; };
    set("elapsed", `TIME ${formatDuration(elapsed)}`);
    set("speed", speed !== null && speed !== undefined ? `TOK/S ${Number(speed).toFixed(1)}` : "TOK/S \u2014");
    set("tokens", tokens ? `TOKENS ${tokens.toLocaleString()}` : "TOKENS \u2014");
    set("cost", cost > 0 ? `COST $${cost.toFixed(4)}` : "COST \u2014");
    set("files", `FILES ${Number(job.files_edited || 0)}`);
    set("diff", `+${Number(job.lines_added || 0).toLocaleString()} / -${Number(job.lines_deleted || 0).toLocaleString()}`);
    this.renderSessionState(job);

    // A running job's clock has to tick between backend frames or TIME sits still.
    if (active && !this.clock) {
      this.clock = setInterval(() => {
        if (this.jobMeta && ACTIVE.has(String(this.jobMeta.status || ""))) this.renderStats(this.jobMeta);
        else { clearInterval(this.clock); this.clock = null; }
      }, 1000);
    }
  }


  /** Keep the composer strip describing exactly where a message will go. */
  syncTarget() {
    const job = this.selectedId ? this.jobMeta : null;
    const send = this.el("send");
    const queue = this.el("steer-now");
    const brief = this.el("brief");
    const projectRow = this.host.querySelector("[data-code-project-row]");
    if (this.selectedId) {
      if (projectRow) projectRow.hidden = true;
      const status = String((job || {}).status || "").toLowerCase();
      if (status === "waiting_user") {
        send.setAttribute("aria-label", "Answer now");
        send.title = "Answer the question the agent is waiting on";
        queue.hidden = true;
        brief.placeholder = "Answer the agent to continue this turn...";
      } else if (status === "running" || status === "queued") {
        send.setAttribute("aria-label", "Queue follow-up");
        send.title = "Add this message to the next safe model round without interrupting the response in progress";
        queue.hidden = false;
        brief.placeholder = "Add context or a follow-up while this run continues...";
      } else {
        send.setAttribute("aria-label", "Continue session");
        send.title = "Start a follow-up turn in this same session";
        queue.hidden = true;
        brief.placeholder = "Continue this session...";
      }
    } else {
      send.setAttribute("aria-label", "Launch session");
      send.title = "Launch a new CODE session";
      queue.hidden = true;
      brief.placeholder = "Describe what the agent should build...";
      if (projectRow) projectRow.hidden = false;
    }
    const receipt = this.selectedId ? this.deliveryReceipts.get(String(this.selectedId)) : null;
    const receiptEl = this.el("delivery-receipt");
    receiptEl.hidden = !receipt;
    receiptEl.dataset.state = receipt ? receipt.state : "";
    receiptEl.textContent = receipt ? receipt.text : "";
    this.renderModelConfigPills();
  }

  closePromptMenus() {
    const plus = this.el("plus-menu");
    const config = this.el("config-menu");
    if (plus) plus.hidden = true;
    if (config) config.hidden = true;
    this.el("prompt-plus")?.setAttribute("aria-expanded", "false");
    this.el("prompt-config")?.setAttribute("aria-expanded", "false");
  }

  togglePromptMenu(which) {
    const target = this.el(which === "config" ? "config-menu" : "plus-menu");
    const wasOpen = target ? !target.hidden : false;
    this.closePromptMenus();
    if (!target || wasOpen) return;
    target.hidden = false;
    this.el(which === "config" ? "prompt-config" : "prompt-plus")
      ?.setAttribute("aria-expanded", "true");
  }

  toggleStandingPrompt() {
    const panel = this.el("standing-panel");
    const toggle = this.el("standing-toggle");
    if (!panel || !toggle) return;
    const opening = panel.hidden;
    panel.hidden = !opening;
    toggle.setAttribute("aria-expanded", String(opening));
    this.el("standing-wrap")?.classList.toggle("expanded", opening);
    if (opening) this.el("standing-prompt")?.focus();
  }

  scheduleStandingPromptSave() {
    clearTimeout(this.standingPromptSaveTimer);
    this.standingPromptSaveTimer = setTimeout(() => this.saveStandingPrompt(), 250);
  }

  saveStandingPrompt() {
    clearTimeout(this.standingPromptSaveTimer);
    this.standingPromptSaveTimer = null;
    const value = String(this.standingPrompt || "").slice(0, STANDING_PROMPT_LIMIT);
    // Serialize writes so a slower older request can never overwrite newer text.
    this.standingPromptSaveChain = this.standingPromptSaveChain
      .catch(() => {})
      .then(() => api("/api/config", {
        method: "POST",
        body: { code_standing_prompt: value },
        timeout: 3000,
      }))
      .then((result) => {
        if (result?.ok) this.shell.config.code_standing_prompt = value;
      })
      .catch((error) => console.warn("Could not autosave the CODE standing prompt", error));
    return this.standingPromptSaveChain;
  }

  setDeliveryReceipt(jobId, text, state = "delivered", preview = "") {
    const id = String(jobId || "");
    if (!id) return;
    this.deliveryReceipts.set(id, { text: String(text || ""), state, preview: shortText(preview, 90) });
    if (this.selectedId === id) this.syncTarget();
  }

  /** Auto is always coder-led; delegation is a Coder decision, not prompt classification. */
  setTurnStrategy(_value) {
    this.turnStrategy = "auto";
  }

  showUsage() {
    const window_ = this.usageWindow;
    if (!window_ || window_.error) {
      this.shell.toast("28-day usage is not available yet.");
      return;
    }
    const usage = window_.usage || {};
    const n = (value) => Number(value || 0).toLocaleString();
    const rows = [
      ["Total tokens", n(usage.total_tokens)],
      ["  input", `${n(usage.input_tokens)}  (cached ${n(usage.cached_input_tokens)})`],
      ["  output", `${n(usage.output_tokens)}  (reasoning ${n(usage.reasoning_tokens)})`],
      ["Spend", `$${Number(usage.cost_usd || 0).toFixed(4)}`],
    ];
    for (const [provider, row] of Object.entries(window_.by_provider || {})) {
      const rowUsage = row.usage || {};
      rows.push([provider, `${n(rowUsage.total_tokens)} tok \u00b7 $${Number(rowUsage.cost_usd || 0).toFixed(4)} \u00b7 ${row.sessions || 0} sessions`]);
    }
    const missing = Number(window_.sessions_without_usage || 0);
    this.shell.sheet(
      `Last ${window_.days || 28} days \u00b7 ${window_.sessions || 0} sessions`,
      rows,
      missing ? `${missing} session${missing === 1 ? "" : "s"} reported no usage and are excluded.` : "",
    );
  }

  // ------------------------------------------------------------- capabilities

  async refreshCapabilities(force) {
    const result = await api(`/api/code/capabilities${force ? "?refresh=1" : ""}`);
    if (this.gone) return;
    if (result && result.providers) {
      this.capabilities = result;
      this.refreshSelectors();
      this.el("health").textContent = (result.providers || [])
        .map((row) => `${String(row.provider || "").replace(/^./, (c) => c.toUpperCase())} ${row.ready ? "ready" : "setup needed"}`)
        .join("  \u00b7  ") || "Loading agent capabilities\u2026";
    }
  }

  /** What the coder will actually run, for this provider.
   *
   * Roles describe the OpenRouter pipeline. The CLI providers each manage
   * their own model list, so there the launch falls back to whatever that
   * provider says its default is -- picking an OpenRouter model id for Codex
   * would just fail at launch.
   */
  coderChoice() {
    return this.configurationCoderChoice(this.launchProvider, this.roles);
  }

  configurationCoderChoice(provider, roles) {
    const info = (this.capabilities.providers || []).find((row) => row.provider === provider) || {};
    const models = info.models || [];
    const role = (roles || {}).coder || {};
    const configured = models.find((model) => String(model.id) === String(role.model || ""));
    // Provider discovery is asynchronous. A saved configuration is already an
    // explicit provider/model pair, so keep that pair while its catalogue is
    // still loading. The backend performs the authoritative live validation
    // before launch. Falling through here used the global default model, which
    // could send an Ollama or OpenRouter id to Codex/Claude/Cursor.
    if (role.model && !models.length) {
      return {
        model: role.model,
        reasoning: role.reasoning || "off",
        fast: !!role.fast,
      };
    }
    // Ollama belongs here too. A locally served model has no graded effort
    // scale -- its think flag is a boolean -- and the provider row carries no
    // model catalogue, so `efforts` below collapses to ["medium"] and silently
    // replaces an explicit "off" with medium. Honour what was configured.
    if (provider === "openrouter" || provider === "ollama") {
      if (role.model && configured) {
        return {
          model: role.model,
          reasoning: role.reasoning || "off",
          fast: !!configured.fast && !!role.fast,
        };
      }
    }
    const preferred = configured
      || models.find((model) => model.default) || models[0] || {};
    const efforts = (preferred.reasoning || info.reasoning || info.efforts || ["medium"]).map(String);
    // Turning reasoning off is always a valid choice; an incomplete catalogue
    // must not be able to veto it.
    if (String(role.reasoning || "") === "off" && !efforts.includes("off")) efforts.push("off");
    const fallback = {
      codex: "gpt-5.6-sol", claude: "sonnet", cursor: "composer-2.5",
      ollama: "qwen3:14b", openrouter: "deepseek/deepseek-v4-flash",
    }[provider] || "";
    return {
      model: String(preferred.id || fallback),
      reasoning: efforts.includes(String(role.reasoning || ""))
        ? String(role.reasoning)
        : String(preferred.default_reasoning || (efforts.includes("medium") ? "medium" : efforts[0])),
      fast: !!preferred.fast && !!role.fast,
    };
  }

  roleSupportsFast(name, role) {
    const provider = name === "coder" ? this.launchProvider : "openrouter";
    const info = (this.capabilities.providers || []).find((row) => row.provider === provider) || {};
    return !!(info.models || []).find((model) => String(model.id) === String((role || {}).model || "") && model.fast);
  }

  fastModeState() {
    const supported = Object.entries(this.roles || {})
      .filter(([, role]) => role && role.enabled !== false)
      .filter(([name, role]) => this.roleSupportsFast(name, role));
    return {
      count: supported.length,
      on: supported.length > 0 && supported.every(([, role]) => !!role.fast),
    };
  }

  refreshFastToggle() {
    const button = this.el("fast-mode");
    if (!button) return;
    const state = this.fastModeState();
    button.disabled = state.count === 0;
    button.classList.toggle("on", state.on);
    button.setAttribute("aria-pressed", state.on ? "true" : "false");
    button.title = state.count
      ? `${state.on ? "Disable" : "Enable"} fast mode on ${state.count} compatible configured model${state.count === 1 ? "" : "s"}`
      : "Fast mode is unavailable for the selected configuration";
  }

  async toggleFastMode() {
    const state = this.fastModeState();
    if (!state.count) return;
    const next = !state.on;
    this.roles = Object.fromEntries(Object.entries(this.roles || {}).map(([name, role]) => {
      const copy = { ...(role || {}) };
      if (this.roleSupportsFast(name, copy)) copy.fast = next;
      else copy.fast = false;
      return [name, copy];
    }));
    this.refreshSelectors();
    if (this.selectedId) await this.applyCurrentConfiguration();
  }

  /** Keep composer labels in sync after role or config changes. */
  refreshSelectors() {
    this.renderModelConfigPills();
    this.refreshFastToggle();
    this.syncTarget();
  }

  async refreshRoles() {
    const result = await api("/api/code/roles");
    if (this.gone || !result || result.ok === false) return;
    this.defaultRoles = JSON.parse(JSON.stringify(result.roles || {}));
    // A session's recorded configuration is authoritative while it is open.
    // The saved defaults seed only a fresh composer; otherwise this async
    // response can race the session detail stream and replace its model.
    if (!this.selectedId) this.roles = JSON.parse(JSON.stringify(this.defaultRoles));
    this.refreshSelectors();
    this.renderModelConfigPills();
  }

  resetLaunchConfiguration() {
    this.roles = JSON.parse(JSON.stringify(this.defaultRoles || {}));
    const provider = String(this.shell.config.code_default_provider || "openrouter").toLowerCase();
    this.launchProvider = PROVIDERS.some(([id]) => id === provider) ? provider : "openrouter";
    this.launchReviewFix = !!this.shell.config.code_review_fix_enabled;
    this.activeConfigId = "";
    this.refreshSelectors();
  }

  async refreshModelConfigs() {
    const result = await api("/api/code/model-configs");
    if (this.gone || !result || result.ok === false) return;
    this.modelConfigs = result.configs || [];
    this.renderModelConfigPills();
  }

  activeModelConfigId() {
    if (this.activeConfigId && this.modelConfigs.some((row) => String(row.id) === this.activeConfigId)) {
      return this.activeConfigId;
    }
    const comparable = (roles, provider, reviewFix, strategy) => JSON.stringify({
      provider: provider || "openrouter",
      review_fix: !!reviewFix,
      strategy: "auto",
      roles: Object.fromEntries(
        Object.entries(roles || {}).map(([name, role]) => [name, {
          enabled: role.enabled !== false,
          model: role.model || "",
          reasoning: role.reasoning || "off",
          fast: !!role.fast,
        }]),
      ),
    });
    const current = comparable(this.roles, this.launchProvider, this.launchReviewFix, this.turnStrategy);
    return (this.modelConfigs.find((config) => comparable(
      config.roles,
      config.provider || "openrouter",
      config.review_fix,
      "auto",
    ) === current) || {}).id || "";
  }

  renderModelConfigPills() {
    const host = this.el("config-menu-list");
    if (!host) return;
    const active = this.activeModelConfigId();
    const visible = this.modelConfigs.filter((config) => config.show_in_composer !== false);
    const selected = visible.find((config) => String(config.id) === String(active));
    // Never label an unmatched/custom role setup as the first saved preset.
    // That made the composer say "Qwen3.8 Local" while the actual coder could
    // still be Huihui (or any other previously selected model).
    const selectedName = selected?.name
      || `Custom · ${shortText(this.coderChoice().model || this.launchProvider || "Configuration", 34)}`;
    const name = this.host.querySelector("[data-config-name]");
    if (name) name.textContent = selectedName || "Configuration";
    host.innerHTML = visible.length ? visible.map((config) => {
      const coder = objectValue(objectValue(config.roles).coder);
      const meta = config.description || coder.model || config.provider || "Coder-led automatic delegation";
      const checked = String(config.id) === String(active);
      return promptConfigRowMarkup({
        label: escapeHtml(config.name || "Untitled"),
        hint: escapeHtml(shortText(meta, 58)),
        selected: checked,
        attrs: `data-config-pill="${escapeHtml(config.id)}"`,
      });
    }).join("") : `
      <button type="button" class="prompt-config-row empty" data-code="models">
        <span><strong>Save a configuration</strong><small>Choose models and reasoning for each role</small></span>
      </button>`;
  }

  async applyModelConfig(configId) {
    const config = this.modelConfigs.find((row) => String(row.id) === String(configId));
    if (!config) return;
    this.roles = JSON.parse(JSON.stringify(config.roles || {}));
    if (config.provider) this.setLaunchProvider(config.provider);
    this.setLaunchReviewFix(!!config.review_fix);
    this.setTurnStrategy("auto");
    this.activeConfigId = String(config.id || "");
    this.refreshSelectors();
    this.syncTarget();
    // Selecting a configuration only stages the roles/models for the NEXT
    // prompt. It must not auto-start a handoff or send any message, so do not
    // call applyCurrentConfiguration() here.
  }

  async applyCurrentConfiguration(config = null) {
    if (!this.selectedId) return { ok: true };
    const selected = config || this.modelConfigs.find((row) => String(row.id) === this.activeConfigId) || {};
    const choice = this.coderChoice();
    const result = await api(`/api/code/jobs/${encodeURIComponent(this.selectedId)}/configuration`, {
      method: "POST",
      body: {
        provider: this.launchProvider,
        model: choice.model,
        reasoning: choice.reasoning,
        fast: choice.fast,
        roles: this.roles,
        config_id: selected.id || this.activeConfigId || "",
        config_name: selected.name || "",
      },
    });
    if (!result || result.ok === false) {
      this.shell.toast((result && result.error) || "Could not apply that configuration.", "error");
      return result || { ok: false };
    }
    if (result.job) {
      this.jobMeta = result.job;
      this.configBoundJob = String(result.job.id || this.selectedId);
      this.renderDetailMeta();
    }
    this.shell.toast(result.handoff ? "Configuration applied - provider handoff started." : "Configuration applied.", "success");
    return result;
  }

  openModels() {
    if (!this.modelsWindow) {
      this.modelsWindow = new ModelsWindow(this.shell);
      this.modelsWindow.onSaved = (roles) => {
        this.roles = roles;
        this.refreshSelectors();
      };
      this.modelsWindow.onConfigsChanged = (configs) => {
        this.modelConfigs = configs || [];
        this.renderModelConfigPills();
      };
      this.modelsWindow.onApplied = async ({ roles, provider, reviewFix, strategy, config }) => {
        if (roles) this.roles = roles;
        if (provider) this.setLaunchProvider(provider);
        if (reviewFix !== undefined) this.setLaunchReviewFix(reviewFix);
        this.setTurnStrategy("auto");
        this.activeConfigId = config ? String(config.id || "") : "";
        this.refreshSelectors();
        if (config) {
          // Applying a configuration only stages roles/models for the next
          // prompt; it must not auto-start a handoff or send any message.
        } else {
          api("/api/config", {
            method: "POST",
            body: { code_default_provider: this.launchProvider, code_review_fix_enabled: this.launchReviewFix },
          });
          if (this.shell && this.shell.config) {
            this.shell.config.code_default_provider = this.launchProvider;
            this.shell.config.code_review_fix_enabled = this.launchReviewFix;
          }
        }
      };
    }
    this.modelsWindow.open("coder", {
      provider: this.launchProvider,
      reviewFix: this.launchReviewFix,
      roles: this.roles,
      activeConfigId: this.activeConfigId,
      strategy: "auto",
    });
  }

  async refreshProjects() {
    const result = await api("/api/code/projects");
    if (this.gone) return;
    this.projects = (result && result.projects) || [];
    const config = await api("/api/config");
    if (this.gone) return;
    const last = config.ok ? config.config.code_last_project_path : "";
    const input = this.el("project");
    if (!input.value) input.value = last || (this.projects[0] || {}).path || "";
    this.updateProjectName();
  }

  async refreshUsage() {
    const result = await api("/api/code/usage?days=28");
    if (this.gone) return;
    const window_ = (result && result.usage) || {};
    this.usageWindow = window_;
    if (window_.error) {
      this.el("usage").textContent = "28d unavailable";
      return;
    }
    const usage = window_.usage || {};
    const tokens = Number(usage.total_tokens || 0);
    const cost = Number(usage.cost_usd || 0);
    this.el("usage").textContent = (!tokens && !cost)
      ? "28d no usage yet"
      : `28d ${compactTokens(tokens)} tok \u00b7 $${cost.toFixed(2)}`;
  }

  // ------------------------------------------------------------------ actions

  /** Send an ask_user answer to the running session like a normal message. */
  async answerQuestion(response) {
    const targetId = this.selectedId;
    if (!targetId) return;
    const payload = response && typeof response === "object" ? response : { text: String(response || ""), answers: {} };
    const result = await api(`/api/code/jobs/${encodeURIComponent(targetId)}/messages`, {
      method: "POST",
      body: {
        text: String(payload.text || ""),
        standing_prompt: this.standingPrompt,
        question_answers: payload.answers || {},
        urgent: true,
        strategy: "auto",
      },
    });
    if (!result?.ok) throw new Error(result?.error || "Could not send answers.");
    return result;
  }

  async send(delivery = "") {
    const brief = this.el("brief");
    const text = brief.value.trim();
    if (!text) return;
    const button = this.el("send");
    const queueButton = this.el("steer-now");
    button.disabled = true;
    queueButton.disabled = true;
    if (this.standingPromptSaveTimer) this.saveStandingPrompt();
    await this.standingPromptSaveChain;

    let result;
    const targetId = this.selectedId;
    if (targetId) {
      this.deliveryReceipts.delete(String(targetId));
      this.syncTarget();
    }
    const targetStatus = String((this.jobMeta || {}).status || "").toLowerCase();
    const deliveryMode = delivery === "steer_now"
      ? "steer_now"
      : ((targetStatus === "running" || targetStatus === "queued") ? "queue_next" : "continue");
    if (targetId) {
      // Selecting a configuration only stages roles/models for the next prompt;
      // the send is what commits them. If the operator changed the model,
      // reasoning, speed tier, or provider for this session, apply it first so
      // the next turn actually runs on the new selection (handing off when the
      // coder changes, which feeds the session context to the new model).
      const choice = this.coderChoice();
      const current = this.jobMeta || {};
      const selectionChanged =
        String(choice.model || "") !== String(current.model || "")
        || String(choice.reasoning || "") !== String(current.reasoning || "")
        || !!choice.fast !== !!current.fast
        || String(this.launchProvider || "").toLowerCase() !== String(current.provider || "").toLowerCase();
      if (selectionChanged) {
        const applied = await this.applyCurrentConfiguration();
        if (!applied || applied.ok === false) {
          button.disabled = false;
          queueButton.disabled = false;
          return;
        }
      }
      result = await api(`/api/code/jobs/${encodeURIComponent(targetId)}/messages`, {
        method: "POST",
        body: {
          text,
          standing_prompt: this.standingPrompt,
          urgent: deliveryMode === "steer_now",
          strategy: "auto",
        },
      });
    } else {
      const cwd = this.el("project").value.trim();
      const choice = this.coderChoice();
      result = await api("/api/code/jobs", {
        method: "POST",
        body: {
          provider: this.launchProvider,
          cwd,
          brief: text,
          standing_prompt: this.standingPrompt,
          model: choice.model,
          reasoning: choice.reasoning,
          fast: choice.fast,
          review_fix: this.launchReviewFix,
          roles: this.roles,
          config_id: this.activeConfigId || "",
          config_name: (this.modelConfigs.find((row) => String(row.id) === this.activeConfigId) || {}).name || "",
          strategy: "auto",
        },
      });
      if (result && result.ok && result.job) {
        api("/api/config", { method: "POST", body: { code_last_project_path: cwd } });
        this.select(String(result.job.id || result.job));
      }
    }

    button.disabled = false;
    queueButton.disabled = false;
    if (!result || result.ok === false) {
      const error = (result && result.error) || "Could not deliver the CODE request.";
      if (targetId) this.setDeliveryReceipt(targetId, error, "failed");
      this.shell.toast(error, "error");
      return;
    }
    if (targetId) {
      const responseJob = objectValue(result.job);
      const queued = numberValue(responseJob.queued);
      let receipt = "";
      let state = "queued";
      let preview = "";
      if (deliveryMode === "steer_now" && String(responseJob.status || "") === "queued") {
        receipt = "Live steering was unavailable - the current turn was interrupted and this runs next.";
        preview = text;
      } else if (deliveryMode === "queue_next") {
        receipt = result.injected_next_round
          ? (queued > 1 ? `Queued as in-run follow-up ${queued}.` : "Queued for the next model round.")
          : (queued > 1 ? `Queued as follow-up ${queued}.` : "Queued after the current turn.");
        preview = text;
      }
      if (receipt) {
        this.setDeliveryReceipt(targetId, receipt, state, preview);
        this.shell.toast(receipt);
      }
      if (this.selectedId === targetId && Object.keys(responseJob).length) {
        this.jobMeta = responseJob;
        this.dirtyMeta = true;
        this.schedule();
      }
    }
    this.setTurnStrategy("auto");
    brief.value = "";
    brief.style.height = "auto";
    this.el("prompt-shell")?.classList.remove("expanded");
    this.view.setFollow(true);
  }

  /** Pull the latest Claude Code / Codex session into the brief as a handoff. */
  async handoff() {
    const list = await api("/api/handoff");
    if (this.gone) return;
    if (!list || typeof list !== "object") {
      this.shell.toast("Could not read handoff sessions.", "error");
      return;
    }
    const sessions = Object.values(list).filter((s) => s && s.path);
    if (!sessions.length) {
      this.shell.toast("No Claude Code or Codex sessions found yet.", "info");
      return;
    }
const rows = sessions.map((s) => [
      `${s.tool} · ${s.title || "untitled"}`,
      `${(s.user_count || 0) + (s.assistant_count || 0)} turns`,
    ]);
    const pick = await this.shell.pick(
      "Handoff session",
      rows,
      "Pick a session to paste into the brief.",
    );
    if (this.gone || pick == null) return;
    const chosen = sessions[pick];
    const result = await api("/api/handoff", {
      method: "POST",
      body: { tool: chosen.tool, path: chosen.path },
    });
    if (this.gone) return;
    if (!result || result.ok === false) {
      this.shell.toast((result && result.error) || "Could not build the handoff brief.", "error");
      return;
    }
    const brief = this.el("brief");
    brief.value = result.brief || "";
    brief.dispatchEvent(new Event("input", { bubbles: true }));
    brief.focus();
    this.shell.toast(`Handoff pasted from ${chosen.tool}.`, "success");
  }

  openSelfReview(source = this.jobMeta, onStarted = null) {
    if (!source || ACTIVE.has(String(source.status || ""))) return;
    const node = document.createElement("div");
    node.className = "modal-backdrop review-backdrop";
    const sourceConfig = {
      id: "__source__",
      name: source.config_name || "Source session configuration",
      provider: source.provider || "openrouter",
      roles: source.role_config || this.roles || {},
    };
    const configs = [sourceConfig, ...this.modelConfigs.filter((row) => String(row.id) !== String(source.config_id || ""))];
    node.innerHTML = `
      <div class="modal review-modal">
        <div class="modal-title">Self review this session</div>
        <div class="modal-detail">A separate, normal tool-capable session will audit the full timestamped loop and propose harness improvements.</div>
        <label class="review-field"><span>Model configuration</span><select class="slim" data-review="config">
          ${configs.map((config) => `<option value="${escapeHtml(config.id)}">${escapeHtml(config.name || "Untitled")}</option>`).join("")}
        </select></label>
        <label class="review-field"><span>Provider for the review agent</span><select class="slim" data-review="provider">
          ${PROVIDERS.map(([id, label]) => `<option value="${escapeHtml(id)}">${escapeHtml(label)}</option>`).join("")}
        </select></label>
        <div class="review-choice" data-review="choice"></div>
        <div class="review-note">The review gets job metadata, raw events, prompts, commands, tool calls/results, provider transcripts, handoffs, usage, diffs, timestamps, and a live description of the current harness. It is instructed to remain read-only.</div>
        <div class="modal-actions">
          <button class="btn compact ghost" data-review="cancel">Cancel</button>
          <button class="btn compact accent" data-review="run">Run self review</button>
        </div>
      </div>`;
    const configSelect = node.querySelector('[data-review="config"]');
    const providerSelect = node.querySelector('[data-review="provider"]');
    const choiceEl = node.querySelector('[data-review="choice"]');
    const selectedConfig = () => configs.find((row) => String(row.id) === String(configSelect.value)) || sourceConfig;
    const sync = (fromConfig = false) => {
      const config = selectedConfig();
      if (fromConfig) providerSelect.value = String(config.provider || "openrouter");
      const choice = this.configurationCoderChoice(providerSelect.value, config.roles || {});
      choiceEl.textContent = `${providerSelect.options[providerSelect.selectedIndex]?.text || providerSelect.value} - ${choice.model || "no model"} / ${choice.reasoning || "-"}${choice.fast ? " / fast" : ""}`;
    };
    configSelect.addEventListener("change", () => sync(true));
    providerSelect.addEventListener("change", () => sync(false));
    providerSelect.value = String(sourceConfig.provider || "openrouter");
    sync(false);
    const close = () => node.remove();
    node.addEventListener("click", async (event) => {
      if (event.target === node || event.target.closest('[data-review="cancel"]')) { close(); return; }
      const run = event.target.closest('[data-review="run"]');
      if (!run) return;
      const config = selectedConfig();
      const provider = providerSelect.value;
      const choice = this.configurationCoderChoice(provider, config.roles || {});
      if (!choice.model) {
        this.shell.toast("That provider has no available review model.", "error");
        return;
      }
      run.disabled = true;
      run.textContent = "Starting review...";
      const result = await api(`/api/code/jobs/${encodeURIComponent(source.id)}/review`, {
        method: "POST",
        body: {
          provider,
          model: choice.model,
          reasoning: choice.reasoning,
          fast: choice.fast,
          roles: config.roles || {},
          config_id: config.id === "__source__" ? String(source.config_id || "") : String(config.id || ""),
          config_name: config.name || "",
        },
      });
      if (!result || result.ok === false) {
        run.disabled = false;
        run.textContent = "Run self review";
        this.shell.toast((result && result.error) || "Could not start the self review.", "error");
        return;
      }
      close();
      const reviewId = String((result.job || {}).id || "");
      if (reviewId) {
        if (onStarted) onStarted(reviewId);
        else this.select(reviewId);
      }
      this.shell.toast("Self review started in Session Reviews.", "success");
    });
    document.body.appendChild(node);
    requestAnimationFrame(() => node.classList.add("show"));
  }

  async stopJob() {
    if (!this.selectedId) return;
    await api(`/api/code/jobs/${encodeURIComponent(this.selectedId)}/stop`, { method: "POST" });
  }

  async undoJob() {
    if (!this.selectedId) return;
    const job = this.jobMeta || {};
    const count = Number(job.undoable_files || 0);
    const detail = count > 0
      ? `This restores ${count} file${count === 1 ? "" : "s"} to how they were before this session changed them. Shell side effects without a file checkpoint are not reversed.`
      : "This restores every file checkpointed in this session. Shell side effects without a file checkpoint are not reversed.";
    const confirmed = await this.shell.confirm("Undo this session's file changes?", detail);
    if (!confirmed) return;
    const result = await api(`/api/code/jobs/${encodeURIComponent(this.selectedId)}/undo`, {
      method: "POST",
      body: { confirm: true },
    });
    if (result && result.ok === false) {
      this.shell.toast(result.error || "Could not undo session changes.", "error");
      return;
    }
    const restored = Number((result && result.restored_count) || 0);
    const failed = Number((result && result.error_count) || 0);
    if (result && result.job) this.jobMeta = result.job;
    this.renderDetailMeta();
    this.shell.toast(
      failed
        ? `Undid ${restored} file${restored === 1 ? "" : "s"}; ${failed} could not be restored.`
        : `Undid ${restored} file${restored === 1 ? "" : "s"}.`,
      failed ? "info" : "success",
    );
  }

  /** Persist hidden sessions + projects to localStorage cache AND backend config. */
  saveHiddenState() {
    saveHiddenSessionIds(this.hiddenSessions);
    saveHiddenProjectKeys(this.hiddenProjects);
    api("/api/config", {
      method: "POST",
      body: {
        code_hidden_sessions: [...this.hiddenSessions],
        code_hidden_projects: [...this.hiddenProjects],
      },
    });
  }

  toggleHideMode() {
    this.hideMode = !this.hideMode;
    const toggle = this.el("hide-toggle");
    if (toggle) {
      toggle.classList.toggle("active", this.hideMode);
      toggle.setAttribute("aria-pressed", String(this.hideMode));
      toggle.title = this.hideMode ? "Done editing hidden sessions" : "Edit hidden sessions";
    }
    this.host.classList.toggle("hide-mode", this.hideMode);
    this.dirtySessions = true;
    this.schedule();
  }

  async deleteJob() {
    if (!this.selectedId) return;
    const id = this.selectedId;
    const confirmed = await this.shell.confirm("Delete this session?", "Its transcript and logs are removed from disk.");
    if (!confirmed) return;
    const result = await api(`/api/code/jobs/${encodeURIComponent(id)}`, { method: "DELETE", body: { confirm: id } });
    if (result && result.ok === false) {
      this.shell.toast(result.error || "Could not delete the session.", "error");
      return;
    }
    this.select(null);
  }

  async setupProvider() {
    const provider = this.launchProvider;
    const result = await api(`/api/code/providers/${encodeURIComponent(provider)}/setup`, { method: "POST" });
    this.shell.toast((result && (result.message || result.error)) || `${provider} is ready.`, result && result.ok === false ? "error" : "info");
    this.refreshCapabilities(true);
  }

  /** Reload only the frontend CSS/HTML — no backend restart. */
  reloadUI() {
    const stamp = Date.now();
    for (const link of document.querySelectorAll('link[rel="stylesheet"]')) {
      const url = new URL(link.href, location.href);
      url.searchParams.set("v", stamp);
      link.href = url.pathname + url.search;
    }
    this.shell.toast("UI reloaded (CSS/HTML only)", "info");
  }

  /** Map a sessions-sidebar group key to the cwd stored on the hidden project field. */
  projectPathForGroup(key) {
    const raw = String(key || "").trim();
    if (!raw || raw.startsWith("@")) return raw;
    const lower = raw.toLowerCase();
    for (const project of this.projects || []) {
      const path = String(project.path || "");
      if (path.toLowerCase() === lower) return path;
    }
    return raw;
  }

  updateProjectName() {
    const el = this.host.querySelector("[data-project-name]");
    if (!el) return;
    const path = this.el("project").value || "";
    const lower = path.toLowerCase();
    const fromList = (this.projects || []).find((row) => String(row.path || "").toLowerCase() === lower);
    const name = fromList
      ? String(fromList.name || "")
      : (path.split(/[\\/]/).filter(Boolean).pop() || path || "No project");
    el.textContent = name || "No project";
    el.title = path;
  }

  async addFolder() {
    const picked = await this.shell.pickFolder();
    if (!picked) return;
    this.el("project").value = picked;
    await api("/api/code/projects", { method: "POST", body: { path: picked } });
    await this.refreshProjects();
    this.updateProjectName();
    this.syncTarget();
  }
}
