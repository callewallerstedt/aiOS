// The BENCH page.
//
// Reached from the BENCH button in CODE, not from the rail, because it is a
// tool for the CODE harness rather than a place you live in.
//
// What it is for: pointing the harness you actually use at a fixed set of
// tasks and finding out what it costs. Correctness comes from hidden checks the
// agent never sees, tokens and money come from what the provider reported, and
// the score is arithmetic over those two -- nothing here is a vibe.
//
// aiOS attempts are ordinary CODE sessions; raw native adapters write the same
// isolated job/event protocol. Clicking either kind uses the real transcript
// renderer from CODE (transcript.js).

import { api, native, stream } from "./bridge.js";
import { escapeHtml } from "./markdown.js";
import { Transcript, compactTokens, formatDuration, relativeTime } from "./transcript.js";
import { ModelsWindow } from "./models.js";

const PROVIDERS = [
  ["codex", "ChatGPT Codex"],
  ["claude", "Claude"],
  ["cursor", "Cursor"],
  ["ollama", "Ollama local"],
  ["openrouter", "OpenRouter"],
];

const FALLBACK_MODELS = {
  codex: ["gpt-5.6-sol"], claude: ["sonnet"], cursor: ["composer-2.5"],
  ollama: ["qwen3:14b"], openrouter: ["deepseek/deepseek-v4-flash", "qwen/qwen3.8-max", "qwen/qwen3-coder"],
};

const RUN_ACTIVE = new Set(["starting", "running", "stopping"]);
const TASK_ACTIVE = new Set(["pending", "running", "verifying"]);
// A benchmark task is finished when the run says so; the underlying session
// statuses are CODE's, and transcript.js only needs to know whether to keep the
// streaming caret alive.
const JOB_ACTIVE = new Set(["queued", "running", "waiting_user"]);

const SETTINGS_KEY = "aios:bench:setup";
const CUSTOM_SETTINGS_KEY = "aios:bench:custom-setup";
const CAMPAIGN_SETTINGS_KEY = "aios:bench:campaign-setup";
const PROJECT_SETTINGS_KEY = "aios:bench:project-setup";

function money(value) {
  if (value === null || value === undefined || value === "") return "n/a";
  const amount = Number(value || 0);
  if (!amount) return "$0";
  return amount < 0.01 ? `$${amount.toFixed(4)}` : `$${amount.toFixed(2)}`;
}

function number(value) {
  return Number(value || 0).toLocaleString();
}

function shortModel(id) {
  const text = String(id || "");
  const slash = text.lastIndexOf("/");
  return slash >= 0 ? text.slice(slash + 1) : text;
}

function humanize(value) {
  return String(value || "").trim().replaceAll("_", " ");
}

/**
 * Produce the identity copy for one comparison lane.
 *
 * `lane_type` is assigned by campaignHarnesses() after backend/saved data is
 * spread into the row.  Keeping that discriminator separate from provider and
 * model prevents a raw Codex/Claude/OMP/Hermes lane from accidentally inheriting
 * aiOS strategy or reviewer copy.
 */
export function benchLaneIdentity(row = {}) {
  const nativeHarness = row.lane_type === "native";
  const ready = row.ready !== false;
  const version = String(row.version || "").trim();
  if (nativeHarness) {
    const engine = String(row.engine || row.id || "native").trim().toLowerCase();
    const engineLabel = String(row.label || row.name || engine).trim();
    const reasoning = String(row.default_reasoning || row.reasoning || "default").trim();
    const tools = String(row.tool_profile || "adapter defaults").trim();
    const cost = humanize(row.cost_provenance || "unknown cost");
    return {
      nativeHarness, ready, engine, laneType: "native",
      model: String(row.default_model || row.model || (ready ? "native configured model" : "not installed")),
      headline: `${engineLabel} · reasoning ${reasoning}`,
      facts: `${version || "version unavailable"} · tools: ${tools} · ${cost}`,
      detailTitle: String(row.sandbox_note || "isolated benchmark workspace"),
    };
  }

  const adaptiveReview = !!(row.roles || {}).reviewer
    && (row.roles || {}).reviewer.enabled !== false;
  return {
    nativeHarness, ready, engine: "aios", laneType: "aios",
    model: String(row.model || ""),
    headline: `aiOS · ${row.strategy || "auto"} · ${adaptiveReview ? "review adaptive" : "review off"}`,
    facts: version,
    detailTitle: "",
  };
}

/** Shared by Fair comparison and Project test so lane identity cannot drift. */
export function renderBenchLaneChoice(row, checked, scope = "campaign") {
  const identity = benchLaneIdentity(row);
  const active = !!checked && identity.ready;
  const inputMarker = scope === "project" ? "data-project-harness" : "data-campaign-harness";
  const name = row.name || row.label || row.id;
  return `<label class="bench-campaign-harness${active ? " selected" : ""}${identity.ready ? "" : " unavailable"}" data-lane-type="${identity.laneType}"${identity.ready ? "" : ' aria-disabled="true"'}>
    <input type="checkbox" ${inputMarker}="1" data-harness-id="${escapeHtml(row.id)}" data-harness-kind="${escapeHtml(row.kind || identity.laneType)}" data-harness-engine="${escapeHtml(identity.engine)}"${active ? " checked" : ""}${identity.ready ? "" : " disabled"}>
    <span class="bench-campaign-harness-copy">
      <b>${escapeHtml(name)}</b>
      <i>${escapeHtml(identity.model || (identity.ready ? "configured model" : "not installed"))}</i>
      <em>${escapeHtml(identity.headline)}</em>
      ${identity.facts ? `<span class="bench-lane-facts"${identity.detailTitle ? ` title="${escapeHtml(identity.detailTitle)}"` : ""}>${escapeHtml(identity.facts)}</span>` : ""}
      ${identity.ready ? "" : `<small>${escapeHtml(row.reason || "not available on this machine")}</small>`}
    </span>
  </label>`;
}

export class BenchTab {
  constructor(host, shell) {
    this.host = host;
    this.shell = shell;
    this.runs = [];
    this.run = null;
    this.runId = null;
    this.group = null;
    this.groupId = null;
    this.taskId = null;
    this.mode = "setup";        // setup | campaign | custom | project | group | run | task | compare
    this.setupKind = "suite";   // suite | campaign | custom | project
    this.customs = [];
    this.savedConfigs = [];
    this.modelsWindow = null;
    this.customId = null;
    this.applyPreview = null;
    this.applyReceipt = null;
    this.capabilities = { providers: [] };
    this.meta = null;
    this.stateStream = null;
    this.eventStream = null;
    this.destroyed = false;
    this.since = 0;
    this.savedConfig = shell.pendingBenchConfig || null;
    this.initialRunId = shell.pendingBenchRunId || null;
    shell.pendingBenchConfig = null;
    shell.pendingBenchRunId = null;

    this.render();
    this.boot();
  }

  destroy() {
    this.destroyed = true;
    if (this.abort) this.abort.abort();
    if (this.stateStream) this.stateStream.close();
    if (this.eventStream) this.eventStream.close();
    if (this.view) this.view.destroy();
    if (this.clock) clearInterval(this.clock);
    if (this.modelsWindow) this.modelsWindow.close();
  }

  // ------------------------------------------------------------------ layout

  render() {
    this.host.innerHTML = `
      <div class="bench-head">
        <button class="btn compact ghost" data-bench="back">&#8592; CODE</button>
        <h1>BENCH</h1>
        <span class="tagline">Point the harness at a fixed set of tasks and find out what it costs.</span>
        <span class="spacer"></span>
        <button class="btn compact" data-bench="harness" title="What this agent is made of">HARNESS</button>
        <button class="btn compact" data-bench="compare">Compare runs</button>
        <button class="btn compact" data-bench="campaign">Quick compare</button>
        <button class="btn compact" data-bench="project">Project test</button>
        <button class="btn compact" data-bench="custom">Custom tests</button>
        <button class="btn compact accent" data-bench="new">+ New run</button>
      </div>

      <div class="bench-split">
        <section class="card bench-runs">
          <div class="bench-runs-head">RUNS</div>
          <div class="bench-runs-list" data-bench="runs"></div>
        </section>
        <section class="card bench-main" data-bench="main"></section>
      </div>
    `;
    this.el = (name) => this.host.querySelector(`[data-bench="${name}"]`);
    this.main = this.el("main");
    this.bind();
  }

  bind() {
    this.abort = new AbortController();
    this.host.addEventListener("click", (event) => {
      const configPill = event.target.closest("[data-bench-config]");
      if (configPill) { this.selectBenchConfig(configPill.dataset.benchConfig); return; }
      const groupRow = event.target.closest("[data-run-group]");
      if (groupRow) { this.openGroup(groupRow.dataset.runGroup); return; }
      const runRow = event.target.closest("[data-run]");
      if (runRow) { this.openRun(runRow.dataset.run); return; }
      const taskRow = event.target.closest("[data-task]");
      if (taskRow) { this.openTask(taskRow.dataset.task); return; }

      const trigger = event.target.closest("[data-bench]");
      if (!trigger) return;
      const action = trigger.dataset.bench;
      if (action === "back") this.shell.show("CODE");
      else if (action === "harness") this.shell.show("HARNESS");
      else if (action === "new") { this.savedConfig = null; this.showSetup(); }
      else if (action === "custom") this.showCustom();
      else if (action === "campaign") this.showCampaign();
      else if (action === "project") this.showProject();
      else if (action === "compare") this.showCompare();
      else if (action === "start") this.start();
      else if (action === "start-campaign") this.startCampaign();
      else if (action === "start-project") this.startProject();
      else if (action === "pick-project-folder") this.pickProjectFolder();
      else if (action === "preview-project-apply") this.previewProjectApply();
      else if (action === "confirm-project-apply") this.confirmProjectApply();
      else if (action === "start-custom") this.startCustom();
      else if (action === "save-custom") this.saveCustom();
      else if (action === "new-custom") this.newCustom();
      else if (action === "delete-custom") this.deleteCustom();
      else if (action === "edit-configs") this.openConfigManager();
      else if (action === "stop") this.stop();
      else if (action === "parallel-continue") this.parallelContinue();
      else if (action === "delete") this.remove();
      else if (action === "to-run") this.showRun();
      else if (action === "to-group") this.showGroup();
      else if (action === "open-workspace") this.openWorkspace();
      else if (action === "jump") this.view.setFollow(true);
      else if (action === "score-help") this.explainScore();
      else if (action === "setup-suite") { this.savedConfig = null; this.showSetup(); }
      else if (action === "setup-campaign") this.showCampaign();
      else if (action === "setup-project") this.showProject();
      else if (action === "setup-custom") this.showCustom();
    }, { signal: this.abort.signal });

    this.host.addEventListener("click", (event) => {
      const customRow = event.target.closest("[data-custom]");
      if (!customRow) return;
      if (event.target.closest("[data-bench]")) return;
      this.openCustom(customRow.dataset.custom);
    }, { signal: this.abort.signal });

    this.host.addEventListener("change", (event) => {
      const trigger = event.target.closest("[data-setup]");
      if (trigger) {
        if (trigger.dataset.setup === "provider") this.refreshModels();
        else if (trigger.dataset.setup === "model") this.refreshReasoning();
        this.saveSetup();
        this.updateSetupSummary();
        return;
      }
      const modelField = event.target.closest("[data-model-field]");
      if (modelField) {
        if (modelField.dataset.modelField === "provider") this.refreshModelRow(modelField.closest("[data-model-row]"));
        else if (modelField.dataset.modelField === "model") this.refreshModelRowReasoning(modelField.closest("[data-model-row]"));
        this.saveCustomSetup();
        this.updateCustomSummary();
      }
      if (event.target.closest("[data-custom-config]")) {
        event.target.closest(".bench-config-choice")?.classList.toggle("selected", event.target.checked);
        this.saveCustomSetup();
        this.updateCustomSummary();
      }
      if (event.target.closest("[data-campaign-harness]")) {
        event.target.closest(".bench-campaign-harness")?.classList.toggle("selected", event.target.checked);
        this.saveCampaignSetup();
        this.updateCampaignSummary();
      }
      if (event.target.closest("[data-campaign-field]")) {
        this.saveCampaignSetup();
        this.updateCampaignSummary();
      }
      if (event.target.closest("[data-project-harness]")) {
        event.target.closest(".bench-campaign-harness")?.classList.toggle("selected", event.target.checked);
        this.saveProjectSetup();
        this.updateProjectSummary();
      }
      if (event.target.closest("[data-project-field]")) {
        this.saveProjectSetup();
        this.updateProjectSummary();
      }
    }, { signal: this.abort.signal });

    this.host.addEventListener("input", (event) => {
      if (event.target.closest("[data-setup]")) this.updateSetupSummary();
      if (event.target.closest("[data-custom-field]") || event.target.closest("[data-model-row]")) {
        this.updateCustomSummary();
      }
      if (event.target.closest("[data-campaign-field]")) this.updateCampaignSummary();
      if (event.target.closest("[data-project-field]")) this.updateProjectSummary();
    }, { signal: this.abort.signal });
  }

  async boot() {
    const [meta, capabilities, customs, modelConfigs] = await Promise.all([
      api("/api/bench/meta"),
      api("/api/code/capabilities"),
      api("/api/bench/custom"),
      api("/api/code/model-configs"),
    ]);
    this.meta = meta && meta.ok ? meta : null;
    this.capabilities = capabilities && capabilities.providers ? capabilities : { providers: [] };
    this.customs = (customs && customs.definitions) || [];
    this.savedConfigs = (modelConfigs && modelConfigs.configs) || [];

    const listing = await api("/api/bench/groups");
    this.runs = (listing && listing.groups) || [];
    this.renderRuns();
    // Land on the last run if there is one -- you almost always want to see how
    // the thing you just started is doing, not fill the form in again.
    if (this.initialRunId) await this.openRun(this.initialRunId);
    else if (this.savedConfig) this.showSetup();
    else if (this.runs.length) {
      if (this.runs[0].is_group) await this.openGroup(this.runs[0].id);
      else await this.openRun(this.runs[0].id);
    }
    else {
      this.showSetup();
      this.connect();
    }
  }

  connect() {
    if (this.destroyed) return;
    if (this.stateStream) this.stateStream.close();
    this.stateStream = stream(
      () => `/sse/bench/state?run=${encodeURIComponent(this.runId || "")}&group=${encodeURIComponent(this.groupId || "")}`,
      {
        state: (payload) => {
          this.runs = payload.runs || [];
          if (payload.run && String(payload.run.id) === String(this.runId)) this.run = payload.run;
          if (payload.group && String(payload.group.id) === String(this.groupId)) this.group = payload.group;
          this.renderRuns();
          if (this.mode === "run") this.renderRun();
          else if (this.mode === "group") this.renderGroup();
          else if (this.mode === "task") this.renderTaskMeta();
          else if (this.mode === "compare") this.renderCompare();
        },
        error: (payload) => this.shell.toast(payload.error || "The bench stream dropped.", "error"),
      },
    );
  }

  async request(path, options = {}) {
    // EventSource occupies one of Chromium's small HTTP/1.1 per-origin socket
    // pool. With two aiOS tabs open, the long-lived CODE/BENCH streams can use
    // every slot and leave an ordinary fetch queued forever. Release BENCH's
    // state socket around user-initiated API calls, then reconnect immediately.
    if (this.stateStream) this.stateStream.close();
    this.stateStream = null;
    try {
      return await api(path, options);
    } finally {
      if (!this.destroyed) this.connect();
    }
  }

  // ------------------------------------------------------------- runs rail

  renderRuns() {
    const list = this.el("runs");
    if (!this.runs.length) {
      list.innerHTML = '<div class="placeholder" style="font-size:11px;padding:24px 10px">No runs yet.<br>Start one to measure the harness.</div>';
      return;
    }
    list.innerHTML = this.runs.map((run) => {
      const active = RUN_ACTIVE.has(String(run.status));
      const custom = String(run.kind) === "custom";
      const project = String(run.kind) === "project";
      const manual = custom || project;
      const grouped = !!run.is_group;
      const score = manual
        ? (run.total_tokens ? compactTokens(run.total_tokens) : "\u2014")
        : this.scoreText(run.score, run.finished);
      const meta = manual
        ? `${project ? "PROJECT" : "CUSTOM"} \u00b7 ${grouped ? `${(run.configurations || []).length} configurations` : (escapeHtml(String(run.model || "").split(",")[0] || "configuration"))} \u00b7 ${relativeTime(run.created_at)}`
        : `${escapeHtml(String(run.provider || "").toUpperCase())} \u00b7 ${run.passed || 0}/${run.tasks || 0} passed \u00b7 ${relativeTime(run.created_at)}`;
      return `
        <button class="bench-run-row${String(run.id) === String(this.groupId || this.runId) ? " selected" : ""}${active ? " live" : ""}"
                ${grouped ? `data-run-group="${escapeHtml(run.id)}"` : `data-run="${escapeHtml(run.id)}"`}>
          <span class="score ${manual ? "custom" : this.scoreClass(run.score, run.status)}">${score}</span>
          <span class="text-block">
            <span class="title">${escapeHtml(run.label || run.model || run.id)}</span>
            <span class="meta">${meta}</span>
          </span>
        </button>
      `;
    }).join("");
  }

  /**
   * A run with nothing finished has no score, and saying "0" would read as
   * "this harness scored zero" rather than "ask me again in a minute".
   */
  scoreText(score, finished) {
    if (!Number(finished || 0) || score === null || score === undefined) return "\u2014";
    return String(Math.round(score));
  }

  scoreClass(score, status) {
    if (RUN_ACTIVE.has(String(status))) return "pending";
    const value = Number(score || 0);
    if (value >= 90) return "excellent";
    if (value >= 75) return "strong";
    if (value >= 60) return "fair";
    return "weak";
  }

  // ------------------------------------------------------------------ setup

  showSetup() {
    this.mode = "setup";
    this.setupKind = "suite";
    this.stopEvents();
    const stored = this.savedSetup();
    const linked = this.savedConfig;
    const coder = ((linked || {}).roles || {}).coder || {};
    const saved = linked ? {
      ...stored,
      label: linked.name || stored.label,
      provider: "openrouter",
      model: coder.model || stored.model,
      reasoning: coder.reasoning || stored.reasoning,
      fast: coder.fast,
    } : stored;
    const suites = (this.meta && this.meta.suites) || [];
    const defaults = (this.meta && this.meta.defaults) || { concurrency: 3, timeout: 600 };

    this.main.innerHTML = `
      <div class="bench-pane">
        <div class="bench-pane-head">
          <div class="title">New run</div>
          <div class="meta">Every task is a real git repository. Hidden checks decide whether it worked &mdash; the agent's own word is never taken for it.</div>
        </div>

        <div class="bench-kind-tabs">
          <button class="bench-kind active" data-bench="setup-suite">Fixed suites</button>
          <button class="bench-kind" data-bench="setup-campaign">Fair comparison</button>
          <button class="bench-kind" data-bench="setup-project">Your project</button>
          <button class="bench-kind" data-bench="setup-custom">Custom prompt</button>
        </div>

        <div class="bench-config-picker">
          <span>MODEL CONFIGURATION</span>
          <div class="bench-config-pills">
            <button class="config-pill${linked ? "" : " active"}" data-bench-config="">Custom setup</button>
            ${this.savedConfigs.map((config) => `<button class="config-pill${linked && String(linked.id) === String(config.id) ? " active" : ""}"
              data-bench-config="${escapeHtml(config.id)}" title="${escapeHtml(config.description || config.name || "")}">${escapeHtml(config.name || "Untitled")}</button>`).join("")}
          </div>
        </div>

        ${linked ? `<div class="bench-linked-config">
          <div><b>Saved configuration: ${escapeHtml(linked.name || "Untitled")}</b>${linked.description ? `<i>${escapeHtml(linked.description)}</i>` : ""}</div>
          <div class="bench-linked-roles">${Object.entries(linked.roles || {}).map(([name, role]) =>
            `<span><b>${escapeHtml(name)}</b> ${escapeHtml(role.model || "—")} · ${escapeHtml(role.reasoning || "off")}${role.enabled === false ? " · off" : ""}</span>`
          ).join("")}</div>
        </div>` : ""}

        <div class="bench-form">
          <div class="field wide">
            <label>Label</label>
            <input type="text" data-setup="label" placeholder="what are you testing? e.g. flash vs pro" value="${escapeHtml(saved.label || "")}">
          </div>

          <div class="bench-section">HARNESS</div>
          <div class="field">
            <label>Agent</label>
            <select data-setup="provider">${PROVIDERS.map(([id, label]) => `<option value="${id}">${label}</option>`).join("")}</select>
          </div>
          <div class="field">
            <label>Model</label>
            <select data-setup="model"></select>
          </div>
          <div class="field">
            <label>Reasoning</label>
            <select data-setup="reasoning"></select>
          </div>
          <div class="field">
            <label>Fast mode</label>
            <label class="check"><input type="checkbox" data-setup="fast"> Use the fast variant</label>
          </div>

          <div class="bench-section">TASKS</div>
          <div class="bench-suites">
            ${suites.map((suite) => `
              <div class="bench-suite">
                <div class="copy">
                  <b>${escapeHtml(suite.label)}</b>
                  <i>${escapeHtml(suite.detail)}</i>
                </div>
                <input type="number" min="0" max="${suite.max}" data-setup="count" data-suite="${escapeHtml(suite.id)}"
                       value="${Number((saved.counts || {})[suite.id] ?? suite.default)}">
                <span class="of">of ${suite.max}</span>
              </div>
            `).join("")}
          </div>

          <div class="bench-section">HOW</div>
          <div class="field">
            <label title="Maximum number of different benchmark tasks running simultaneously. It never duplicates a task.">Parallel tasks</label>
            <input type="number" min="1" max="${((this.meta || {}).limits || {}).concurrency || 8}" data-setup="concurrency"
                   value="${Number(saved.concurrency || defaults.concurrency)}">
            <div class="field-help">Runs up to this many different selected tasks simultaneously. It does not create duplicates.</div>
          </div>
          <div class="field">
            <label>Timeout per task</label>
            <input type="number" min="60" max="${((this.meta || {}).limits || {}).timeout || 3600}" step="30" data-setup="timeout"
                   value="${Number(saved.timeout || defaults.timeout)}">
          </div>
        </div>

        <div class="bench-actions">
          <span class="summary" data-bench="setup-summary"></span>
          <button class="btn accent" data-bench="start">Run benchmark &#9656;</button>
        </div>
      </div>
    `;

    this.setup("provider").value = saved.provider || "openrouter";
    this.refreshModels(saved.model);
    this.refreshReasoning(saved.reasoning);
    this.setup("fast").checked = !!saved.fast;
    this.updateSetupSummary();
  }

  setup(name) {
    return this.main.querySelector(`[data-setup="${name}"]`);
  }

  savedSetup() {
    try {
      return JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}") || {};
    } catch {
      return {};
    }
  }

  saveSetup() {
    if (this.mode !== "setup") return;
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(this.collectSetup()));
  }

  collectSetup() {
    const counts = {};
    for (const input of this.main.querySelectorAll('[data-setup="count"]')) {
      counts[input.dataset.suite] = Math.max(0, Number(input.value) || 0);
    }
    return {
      label: this.setup("label").value.trim(),
      provider: this.setup("provider").value,
      model: this.setup("model").value,
      reasoning: this.setup("reasoning").value,
      fast: this.setup("fast").checked,
      review_fix: false,
      counts,
      concurrency: Math.max(1, Number(this.setup("concurrency").value) || 3),
      timeout: Math.max(60, Number(this.setup("timeout").value) || 600),
    };
  }

  updateSetupSummary() {
    const node = this.el("setup-summary");
    if (!node) return;
    const config = this.collectSetup();
    const total = Object.values(config.counts).reduce((sum, value) => sum + value, 0);
    const waves = Math.ceil(total / config.concurrency) || 0;
    node.textContent = total
      ? `${total} task${total === 1 ? "" : "s"} \u00b7 ${config.concurrency} at a time \u00b7 ${waves} wave${waves === 1 ? "" : "s"}, up to ${formatDuration(waves * config.timeout)}`
      : "Pick at least one task.";
  }

  providerInfo(provider) {
    return (this.capabilities.providers || []).find((row) => row.provider === provider) || {};
  }

  refreshModels(preferred = "") {
    const select = this.setup("model");
    if (!select) return;
    const info = this.providerInfo(this.setup("provider").value);
    const models = info.models || [];
    const options = models.length
      ? models.map((model) => [String(model.id), String(model.short_label || model.label || model.id)])
      : (FALLBACK_MODELS[this.setup("provider").value] || []).map((id) => [id, id]);
    select.innerHTML = options
      .map(([id, label]) => `<option value="${escapeHtml(id)}">${escapeHtml(label)}</option>`).join("");
    const wanted = preferred || select.value;
    select.value = options.some(([id]) => id === wanted)
      ? wanted
      : String((models.find((model) => model.default) || {}).id || (options[0] || [""])[0]);
    this.refreshReasoning();
  }

  refreshReasoning(preferred = "") {
    const select = this.setup("reasoning");
    if (!select) return;
    const info = this.providerInfo(this.setup("provider").value);
    const model = (info.models || []).find((row) => String(row.id) === this.setup("model").value) || {};
    const efforts = (model.reasoning || info.reasoning || ["low", "medium", "high"]).map(String);
    const prior = preferred || select.value;
    select.innerHTML = efforts
      .map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("");
    select.value = efforts.includes(prior)
      ? prior
      : String(efforts.includes(String(model.default_reasoning || "medium")) ? (model.default_reasoning || "medium") : efforts[0]);
    const fast = this.setup("fast");
    if (fast) {
      fast.disabled = !model.fast;
      if (!model.fast) fast.checked = false;
    }
  }

  async start() {
    const config = this.collectSetup();
    this.saveSetup();
    const button = this.main.querySelector('[data-bench="start"]');
    button.disabled = true;
    const body = { config, label: config.label };
    if (this.savedConfig) {
      const roles = JSON.parse(JSON.stringify(this.savedConfig.roles || {}));
      roles.coder = {
        ...(roles.coder || {}), enabled: true, model: config.model,
        reasoning: config.reasoning, fast: config.fast,
      };
      body.saved_config_id = this.savedConfig.id;
      body.saved_config_name = this.savedConfig.name;
      body.saved_config_roles = roles;
    }
    const result = await this.request("/api/bench/runs", { method: "POST", body });
    button.disabled = false;
    if (!result || result.ok === false) {
      this.shell.toast((result && result.error) || "Could not start the run.", "error");
      return;
    }
    this.runs = [{ ...result.run, tasks: (result.run.tasks || []).length }, ...this.runs];
    this.openRun(result.run.id);
  }

  // ------------------------------------------------------- fair comparison

  campaignHarnesses() {
    const catalogue = ((this.meta || {}).harnesses || (this.meta || {}).adapters || []);
    const aios = catalogue.find((row) => row.id === "aios") || {};
    const natives = catalogue
      .filter((row) => row.id !== "aios");
    return [
      ...this.savedConfigs.map((row) => ({
        id: row.id, kind: "config", lane_type: "aios", name: row.name || "Saved aiOS config",
        ready: true, engine: "aios", strategy: row.strategy || "auto",
        model: (((row.roles || {}).coder || {}).model || ""),
        detail: row.description || "Full saved aiOS role configuration", version: aios.version || "",
        roles: row.roles || {}, provider: row.provider || "openrouter", review_fix: !!row.review_fix,
      })),
      ...natives.map((row) => ({ ...row, kind: "native", lane_type: "native", engine: row.id })),
    ];
  }

  savedCampaignSetup() {
    try {
      return JSON.parse(localStorage.getItem(CAMPAIGN_SETTINGS_KEY) || "{}") || {};
    } catch {
      return {};
    }
  }

  showCampaign() {
    this.mode = "campaign";
    this.setupKind = "campaign";
    this.stopEvents();
    const saved = this.savedCampaignSetup();
    const suites = (this.meta && this.meta.suites) || [];
    const defaults = ((this.meta || {}).campaign_defaults || {});
    const counts = saved.counts || defaults.counts || { tweak: 1, bugfix: 1, humaneval: 1, aider_polyglot: 1 };
    const harnesses = this.campaignHarnesses();
    let selected = Array.isArray(saved.harness_ids) ? saved.harness_ids.map(String) : [];
    if (!selected.length) {
      selected = ["harness-balanced-engineering", "codex", "claude"]
        .filter((id) => harnesses.some((row) => String(row.id) === id && row.ready !== false));
    }

    this.main.innerHTML = `
      <div class="bench-pane bench-campaign-pane">
        <div class="bench-pane-head">
          <div>
            <div class="title">Fair comparison</div>
            <div class="meta">Fresh identical repositories, identical prompts, external graders, one attempt per cell.</div>
          </div>
        </div>
        <div class="bench-kind-tabs">
          <button class="bench-kind" data-bench="setup-suite">Fixed suites</button>
          <button class="bench-kind active" data-bench="setup-campaign">Fair comparison</button>
          <button class="bench-kind" data-bench="setup-project">Your project</button>
          <button class="bench-kind" data-bench="setup-custom">Custom prompt</button>
        </div>

        <div class="bench-campaign-note">
          <b>Two honest lanes</b>
          <span>Saved aiOS configurations compare harness strategy. Raw Codex, Claude, OMP, and Hermes compare the whole product (harness + model), so their result is labeled separately.</span>
        </div>

        <div class="bench-section">HARNESSES</div>
        <div class="bench-campaign-harnesses">
          ${harnesses.map((row) => {
            const checked = selected.includes(String(row.id));
            return renderBenchLaneChoice(row, checked, "campaign");
          }).join("")}
        </div>

        <div class="bench-section">TASK MATRIX</div>
        <div class="bench-suites bench-campaign-suites">
          ${suites.map((suite) => `
            <div class="bench-suite${suite.official ? " official" : ""}">
              <div class="copy">
                <b>${escapeHtml(suite.label)}${suite.official ? '<span class="bench-origin">PUBLIC SUBSET</span>' : ""}</b>
                <i>${escapeHtml(suite.detail)}</i>
                ${suite.comparability_note ? `<small>${escapeHtml(suite.comparability_note)}</small>` : ""}
              </div>
              <input type="number" min="0" max="${suite.max}" data-campaign-field="count" data-suite="${escapeHtml(suite.id)}"
                     value="${Number(counts[suite.id] || 0)}">
              <span class="of">of ${suite.max}</span>
            </div>
          `).join("")}
        </div>

        <div class="bench-section">GUARDRAILS</div>
        <div class="bench-form bench-campaign-guardrails">
          <div class="field wide">
            <label>Campaign label</label>
            <input type="text" data-campaign-field="label" value="${escapeHtml(saved.label || "Harness proof run")}">
          </div>
          <div class="field">
            <label>OpenRouter ceiling</label>
            <input type="number" min="0.05" max="5" step="0.05" data-campaign-field="max_cost_usd" value="${Number(saved.max_cost_usd || defaults.max_cost_usd || 0.75)}">
            <div class="field-help">Shared across every selected OpenRouter-backed harness, including aiOS, OMP, and Hermes.</div>
          </div>
          <div class="field">
            <label>Non-OpenRouter native ceiling</label>
            <input type="number" min="0" max="2" step="0.05" data-campaign-field="native_max_cost_usd" value="${Number(saved.native_max_cost_usd || defaults.native_max_cost_usd || 0.45)}">
            <div class="field-help">Per repetition for reportable native lanes outside OpenRouter, such as Claude. Codex subscription cost is unavailable.</div>
          </div>
          <div class="field">
            <label>Timeout per task</label>
            <input type="number" min="60" max="1800" step="30" data-campaign-field="timeout" value="${Number(saved.timeout || defaults.timeout || 600)}">
          </div>
          <div class="field">
            <label>Parallel tasks per harness</label>
            <input type="number" min="1" max="${((this.meta || {}).limits || {}).concurrency || 8}" step="1" data-campaign-field="concurrency" value="${Number(saved.concurrency || defaults.concurrency || 4)}">
            <div class="field-help">Each task still gets a fresh isolated repository; this only controls how many run at once.</div>
          </div>
          <div class="field">
            <label>Attempts</label>
            <input type="number" min="1" max="3" step="1" data-campaign-field="repetitions" value="${Number(saved.repetitions || 1)}">
            <div class="field-help">Use 3 for a defensible median; use 1 for the fast proof run.</div>
          </div>
        </div>
        <div class="bench-budget-warning">Cost enforcement uses live provider-reported or API-equivalent spend. It stops new work at the ceiling, but parallel in-flight requests can overshoot. The OpenRouter default stays far below your wallet balance.</div>
        <div class="bench-actions">
          <span class="summary" data-bench="campaign-summary"></span>
          <button class="btn accent" data-bench="start-campaign">Start live comparison &#9656;</button>
        </div>
      </div>
    `;
    this.updateCampaignSummary();
  }

  collectCampaignSetup() {
    const counts = {};
    const suiteMax = new Map(((this.meta || {}).suites || []).map((suite) => [
      String(suite.id), Math.max(0, Math.trunc(Number(suite.max) || 0)),
    ]));
    for (const input of this.main.querySelectorAll('[data-campaign-field="count"]')) {
      const suiteId = String(input.dataset.suite || "");
      const inputMax = Number(input.max);
      const fallbackMax = input.max !== "" && Number.isFinite(inputMax)
        ? Math.max(0, Math.trunc(inputMax))
        : Number.MAX_SAFE_INTEGER;
      const limit = suiteMax.has(suiteId) ? suiteMax.get(suiteId) : fallbackMax;
      const count = Math.max(0, Math.min(limit, Math.trunc(Number(input.value) || 0)));
      counts[suiteId] = count;
      input.value = String(count);
    }
    const harness_ids = [...this.main.querySelectorAll('[data-campaign-harness]:checked')]
      .map((input) => String(input.dataset.harnessId));
    const value = (name, fallback = 0) => {
      const node = this.main.querySelector(`[data-campaign-field="${name}"]`);
      return node ? node.value : fallback;
    };
    return {
      label: String(value("label", "Harness proof run")).trim(), counts, harness_ids,
      max_cost_usd: Math.max(0, Number(value("max_cost_usd", 0.75)) || 0),
      native_max_cost_usd: Math.max(0, Number(value("native_max_cost_usd", 0.45)) || 0),
      timeout: Math.max(60, Number(value("timeout", 600)) || 600),
      concurrency: Math.max(1, Math.min(
        Number((((this.meta || {}).limits || {}).concurrency) || 8),
        Math.trunc(Number(value("concurrency", 4)) || 4),
      )),
      repetitions: Math.max(1, Math.min(3, Number(value("repetitions", 1)) || 1)),
    };
  }

  saveCampaignSetup() {
    if (this.mode !== "campaign") return;
    localStorage.setItem(CAMPAIGN_SETTINGS_KEY, JSON.stringify(this.collectCampaignSetup()));
  }

  updateCampaignSummary() {
    const node = this.el("campaign-summary");
    if (!node || this.mode !== "campaign") return;
    const config = this.collectCampaignSetup();
    const tasks = Object.values(config.counts).reduce((sum, value) => sum + Number(value || 0), 0);
    const attempts = tasks * config.harness_ids.length * config.repetitions;
    node.textContent = config.harness_ids.length && tasks
      ? `${config.harness_ids.length} harnesses · ${tasks} tasks · ${attempts} isolated attempts · ${config.concurrency} parallel per harness · OpenRouter ceiling ${money(config.max_cost_usd)}`
      : "Pick at least one harness and one task.";
  }

  async startCampaign() {
    const config = this.collectCampaignSetup();
    const catalogue = this.campaignHarnesses();
    const configurations = config.harness_ids
      .map((id) => catalogue.find((row) => String(row.id) === String(id)))
      .filter((row) => row && row.ready !== false)
      .map((row) => row.lane_type === "aios" ? {
        id: row.id, name: row.name, engine: "aios", provider: row.provider || "openrouter",
        strategy: row.strategy || "auto", roles: row.roles || {}, cost_provenance: "provider_reported",
        harness_version: row.version || "", review_fix: !!row.review_fix,
      } : {
        id: row.id, name: row.label || row.name || row.id, engine: row.id,
        provider: row.default_provider || row.provider || row.id,
        model: row.default_model || row.model || "",
        reasoning: row.default_reasoning || row.reasoning || "medium",
        harness_version: row.version || "", cost_provenance: row.cost_provenance || "unavailable",
      });
    if (configurations.length < 2 || !Object.values(config.counts).some(Number)) {
      this.shell.toast("Pick at least two harnesses and one task.", "error");
      return;
    }
    this.saveCampaignSetup();
    const button = this.main.querySelector('[data-bench="start-campaign"]');
    if (button) button.disabled = true;
    const result = await this.request("/api/bench/groups", {
      method: "POST",
      body: {
        label: config.label || "Harness proof run", configurations,
        config: {
          kind: "suite", counts: config.counts, concurrency: config.concurrency, timeout: config.timeout,
          repetitions: config.repetitions, max_cost_usd: config.max_cost_usd,
          native_max_cost_usd: config.native_max_cost_usd, profile: "lean",
        },
      },
    });
    if (button) button.disabled = false;
    if (!result || result.ok === false || !result.group) {
      this.shell.toast((result && result.error) || "Could not start the comparison.", "error");
      return;
    }
    this.runs = [result.group, ...this.runs];
    if ((result.errors || []).length) this.shell.toast(`Started with ${result.errors.length} unavailable harness attempt(s).`, "error");
    else this.shell.toast(`${configurations.length} harnesses are live in BENCH.`);
    this.openGroup(result.group.id);
  }

  // ----------------------------------------------------- real project test

  savedProjectSetup() {
    try {
      return JSON.parse(localStorage.getItem(PROJECT_SETTINGS_KEY) || "{}") || {};
    } catch {
      return {};
    }
  }

  showProject() {
    this.mode = "project";
    this.setupKind = "project";
    this.stopEvents();
    const saved = this.savedProjectSetup();
    const defaults = ((this.meta || {}).campaign_defaults || {});
    const harnesses = this.campaignHarnesses();
    let selected = Array.isArray(saved.harness_ids) ? saved.harness_ids.map(String) : [];
    if (!selected.length) {
      selected = ["harness-balanced-engineering", "omp"]
        .filter((id) => harnesses.some((row) => String(row.id) === id && row.ready !== false));
      if (!selected.length && harnesses[0]) selected = [String(harnesses[0].id)];
    }
    this.main.innerHTML = `
      <div class="bench-pane bench-project-pane">
        <div class="bench-pane-head">
          <div>
            <div class="title">Benchmark on your project</div>
            <div class="meta">The source is frozen once. Every harness edits its own copy; your real folder stays untouched.</div>
          </div>
        </div>
        <div class="bench-kind-tabs">
          <button class="bench-kind" data-bench="setup-suite">Fixed suites</button>
          <button class="bench-kind" data-bench="setup-campaign">Fair comparison</button>
          <button class="bench-kind active" data-bench="setup-project">Your project</button>
          <button class="bench-kind" data-bench="setup-custom">Custom prompt</button>
        </div>

        <div class="bench-project-safety">
          <b>Real-project safety boundary</b>
          <span>Git-tracked and non-ignored source is copied into immutable BENCH storage. Generated dependencies stay out. Copying a winning result back requires a conflict preview, explicit confirmation, and a rollback checkpoint.</span>
        </div>

        <div class="bench-section">PROJECT + TASK</div>
        <div class="bench-project-fields">
          <div class="field wide">
            <label>Project folder</label>
            <div class="bench-project-picker">
              <input type="text" data-project-field="source_path" value="${escapeHtml(saved.source_path || "")}" placeholder="C:\\Projects\\my-app" spellcheck="false">
              <button class="btn compact" data-bench="pick-project-folder">Browse</button>
            </div>
            <div class="field-help">The benchmark runner receives only an isolated copy, never this path as its workspace.</div>
          </div>
          <div class="field wide">
            <label>What should every harness do?</label>
            <textarea data-project-field="prompt" spellcheck="false" placeholder="Describe the real engineering task, acceptance criteria, and checks to run.">${escapeHtml(saved.prompt || "")}</textarea>
          </div>
        </div>

        <div class="bench-section">LANES</div>
        <div class="bench-campaign-harnesses">
          ${harnesses.map((row) => {
            const checked = selected.includes(String(row.id));
            return renderBenchLaneChoice(row, checked, "project");
          }).join("")}
        </div>

        <div class="bench-section">GUARDRAILS</div>
        <div class="bench-form bench-campaign-guardrails">
          <div class="field wide">
            <label>Campaign label</label>
            <input type="text" data-project-field="label" value="${escapeHtml(saved.label || "Real project comparison")}">
          </div>
          <div class="field">
            <label>OpenRouter ceiling</label>
            <input type="number" min="0.05" max="5" step="0.05" data-project-field="max_cost_usd" value="${Number(saved.max_cost_usd || defaults.max_cost_usd || 0.75)}">
            <div class="field-help">Shared across selected OpenRouter lanes.</div>
          </div>
          <div class="field">
            <label>Native ceiling</label>
            <input type="number" min="0" max="5" step="0.05" data-project-field="native_max_cost_usd" value="${Number(saved.native_max_cost_usd || defaults.native_max_cost_usd || 0.45)}">
          </div>
          <div class="field">
            <label>Timeout per lane</label>
            <input type="number" min="60" max="${((this.meta || {}).limits || {}).custom_timeout || 7200}" step="60" data-project-field="timeout" value="${Number(saved.timeout || 3600)}">
          </div>
          <div class="field">
            <label>Attempts per harness</label>
            <input type="number" min="1" max="3" step="1" data-project-field="repetitions" value="${Number(saved.repetitions || 1)}">
          </div>
        </div>
        <div class="bench-actions">
          <span class="summary" data-bench="project-summary"></span>
          <button class="btn accent" data-bench="start-project">Freeze + start lanes &#9656;</button>
        </div>
      </div>
    `;
    this.updateProjectSummary();
  }

  collectProjectSetup() {
    const value = (name, fallback = "") => {
      const node = this.main.querySelector(`[data-project-field="${name}"]`);
      return node ? node.value : fallback;
    };
    return {
      source_path: String(value("source_path")).trim(),
      prompt: String(value("prompt")).trim(),
      label: String(value("label", "Real project comparison")).trim(),
      harness_ids: [...this.main.querySelectorAll("[data-project-harness]:checked")]
        .map((node) => String(node.dataset.harnessId)),
      max_cost_usd: Math.max(0, Number(value("max_cost_usd", 0.75)) || 0),
      native_max_cost_usd: Math.max(0, Number(value("native_max_cost_usd", 0.45)) || 0),
      timeout: Math.max(60, Number(value("timeout", 3600)) || 3600),
      repetitions: Math.max(1, Math.min(3, Math.trunc(Number(value("repetitions", 1)) || 1))),
    };
  }

  saveProjectSetup() {
    if (this.mode !== "project") return;
    localStorage.setItem(PROJECT_SETTINGS_KEY, JSON.stringify(this.collectProjectSetup()));
  }

  updateProjectSummary() {
    const node = this.el("project-summary");
    if (!node || this.mode !== "project") return;
    const config = this.collectProjectSetup();
    const lanes = config.harness_ids.length * config.repetitions;
    node.textContent = config.source_path && config.prompt && lanes
      ? `${config.harness_ids.length} harnesses · ${lanes} isolated copies · source frozen once · ${money(config.max_cost_usd)} OpenRouter ceiling`
      : "Choose a folder, write the task, and select at least one lane.";
  }

  async pickProjectFolder() {
    const picked = await this.shell.pickFolder();
    if (!picked || this.mode !== "project") return;
    const input = this.main.querySelector('[data-project-field="source_path"]');
    if (input) input.value = picked;
    this.saveProjectSetup();
    this.updateProjectSummary();
  }

  projectConfigurations(ids) {
    const catalogue = this.campaignHarnesses();
    return ids
      .map((id) => catalogue.find((row) => String(row.id) === String(id)))
      .filter((row) => row && row.ready !== false)
      .map((row) => row.lane_type === "aios" ? {
        id: row.id, name: row.name, engine: "aios", provider: row.provider || "openrouter",
        strategy: row.strategy || "auto", roles: row.roles || {}, cost_provenance: "provider_reported",
        harness_version: row.version || "", review_fix: !!row.review_fix,
      } : {
        id: row.id, name: row.label || row.name || row.id, engine: row.id,
        provider: row.default_provider || row.provider || row.id,
        model: row.default_model || row.model || "",
        reasoning: row.default_reasoning || row.reasoning || "medium",
        harness_version: row.version || "", cost_provenance: row.cost_provenance || "unavailable",
      });
  }

  async startProject() {
    const config = this.collectProjectSetup();
    const configurations = this.projectConfigurations(config.harness_ids);
    if (!config.source_path) {
      this.shell.toast("Choose a project folder first.", "error");
      return;
    }
    if (!config.prompt) {
      this.shell.toast("Write the engineering task first.", "error");
      return;
    }
    if (!configurations.length) {
      this.shell.toast("Select at least one ready harness.", "error");
      return;
    }
    this.saveProjectSetup();
    const button = this.main.querySelector('[data-bench="start-project"]');
    if (button) button.disabled = true;
    const result = await this.request("/api/bench/project-campaign", {
      method: "POST",
      body: {
        label: config.label || "Real project comparison", configurations,
        config: {
          source_path: config.source_path, prompt: config.prompt, timeout: config.timeout,
          repetitions: config.repetitions, max_cost_usd: config.max_cost_usd,
          native_max_cost_usd: config.native_max_cost_usd, profile: "lean",
        },
      },
    });
    if (button) button.disabled = false;
    if (!result || result.ok === false || !result.group) {
      this.shell.toast((result && result.error) || "Could not snapshot and start the project benchmark.", "error");
      return;
    }
    this.runs = [result.group, ...this.runs];
    this.shell.toast(`${configurations.length} isolated project lane${configurations.length === 1 ? " is" : "s are"} live.`);
    this.openGroup(result.group.id);
  }

  // ------------------------------------------------------------- custom

  async refreshCustoms() {
    const listing = await this.request("/api/bench/custom");
    this.customs = (listing && listing.definitions) || [];
  }

  async showCustom(preferredId) {
    this.mode = "custom";
    this.setupKind = "custom";
    this.stopEvents();
    await this.refreshCustoms();
    const saved = this.savedCustomSetup();
    const defaults = (this.meta && this.meta.defaults) || { concurrency: 3, custom_timeout: 3600 };
    const wanted = preferredId !== undefined
      ? preferredId
      : (this.customId || saved.custom_id || (this.customs[0] && this.customs[0].id) || "");
    const current = this.customs.find((row) => String(row.id) === String(wanted)) || null;
    this.customId = current ? current.id : null;
    const chars = ((current && current.prompt) || "").length;
    const maxCustomTimeout = ((this.meta || {}).limits || {}).custom_timeout || 7200;

    this.main.innerHTML = `
      <div class="bench-pane bench-custom-pane">
        <div class="bench-pane-head">
          <div style="min-width:0">
            <div class="title">Custom tests</div>
            <div class="meta">One prompt, any number of saved agent configurations. Each configuration gets its own measured run.</div>
          </div>
        </div>

        <div class="bench-kind-tabs">
          <button class="bench-kind" data-bench="setup-suite">Fixed suites</button>
          <button class="bench-kind" data-bench="setup-campaign">Fair comparison</button>
          <button class="bench-kind" data-bench="setup-project">Your project</button>
          <button class="bench-kind active" data-bench="setup-custom">Custom prompt</button>
        </div>

        <div class="bench-custom-layout">
          <aside class="bench-custom-list">
            <div class="bench-runs-head">SAVED</div>
            <div class="bench-custom-rows" data-bench="custom-list">
              ${this.customs.length ? this.customs.map((row) => `
                <button class="bench-custom-row${String(row.id) === String(this.customId) ? " selected" : ""}" data-custom="${escapeHtml(row.id)}">
                  <b>${escapeHtml(row.name)}</b>
                  <i>${escapeHtml(row.prompt_preview || "empty prompt")}</i>
                </button>
              `).join("") : '<div class="placeholder" style="font-size:11px;padding:18px 10px">No saved tests yet.<br>Write a prompt and save it.</div>'}
            </div>
            <button class="btn compact bench-custom-new" data-bench="new-custom">+ New test</button>
          </aside>

          <div class="bench-custom-editor">
            <div class="bench-custom-top">
              <label class="bench-stack">
                <span>Name</span>
                <input type="text" data-custom-field="name" placeholder="e.g. Cycloidal gearbox designer"
                       value="${escapeHtml((current && current.name) || "")}">
              </label>
              <label class="bench-stack bench-stack-grow">
                <span>Notes <em>optional</em></span>
                <input type="text" data-custom-field="notes" placeholder="what you are comparing this run against"
                       value="${escapeHtml((current && current.notes) || "")}">
              </label>
            </div>

            <div class="bench-custom-top">
              <label class="bench-stack">
                <span>Display title <em>optional</em></span>
                <input type="text" data-custom-field="title" placeholder="Title shown on benchmark runs"
                       value="${escapeHtml((current && current.title) || "")}">
              </label>
              <label class="bench-stack bench-stack-grow">
                <span>Info <em>optional</em></span>
                <input type="text" data-custom-field="info" placeholder="What this benchmark measures"
                       value="${escapeHtml((current && current.info) || "")}">
              </label>
            </div>

            <label class="bench-stack bench-prompt-stack">
              <span class="bench-prompt-label">
                <span>Prompt</span>
                <i data-bench="prompt-chars">${number(chars)} chars</i>
              </span>
              <textarea data-custom-field="prompt" spellcheck="false"
                        placeholder="The brief the agent gets in an empty git repo. Be specific about what done looks like.">${escapeHtml((current && current.prompt) || "")}</textarea>
            </label>

            <div class="bench-custom-bottom">
              <div class="bench-custom-block bench-config-select">
                <div class="bench-config-select-head">
                  <div><div class="bench-section tight">CONFIGURATIONS</div><i>Pick the saved configs to compare.</i></div>
                  <button class="btn compact ghost" data-bench="edit-configs">View / edit configurations</button>
                </div>
                <div class="bench-custom-configs" data-bench="custom-configs"></div>
              </div>

              <div class="bench-custom-block bench-custom-how">
                <div class="bench-section tight">RUN</div>
                <div class="bench-how-row">
                  <label class="bench-stack slim">
                    <span>Timeout (s)</span>
                    <input type="number" min="60" max="${maxCustomTimeout}" step="30" data-custom-field="timeout"
                           value="${Number(saved.timeout || defaults.custom_timeout || 3600)}">
                  </label>
                  <span class="bench-run-help">Reviewers stop after reporting. Use the review card's FIX button to continue manually.</span>
                </div>
                <div class="bench-run-help">Each selected configuration runs this prompt once. Selected configurations start together; nothing is duplicated.</div>
              </div>
            </div>

            <div class="bench-actions">
              <span class="summary" data-bench="custom-summary"></span>
              <button class="btn compact ghost" data-bench="delete-custom"${current ? "" : " disabled"}>Delete</button>
              <button class="btn compact" data-bench="save-custom">Save</button>
              <button class="btn accent" data-bench="start-custom">Run &#9656;</button>
            </div>
          </div>
        </div>
      </div>
    `;

    this.renderCustomConfigChoices(saved.config_ids);
    this.updateCustomSummary();
  }

  newCustom() {
    this.customId = null;
    const saved = this.savedCustomSetup();
    localStorage.setItem(CUSTOM_SETTINGS_KEY, JSON.stringify({ ...saved, custom_id: "" }));
    this.showCustom("");
  }

  openCustom(customId) {
    this.showCustom(customId);
  }

  customField(name) {
    return this.main.querySelector(`[data-custom-field="${name}"]`);
  }

  savedCustomSetup() {
    try {
      return JSON.parse(localStorage.getItem(CUSTOM_SETTINGS_KEY) || "{}") || {};
    } catch {
      return {};
    }
  }

  saveCustomSetup() {
    if (this.mode !== "custom") return;
    localStorage.setItem(CUSTOM_SETTINGS_KEY, JSON.stringify(this.collectCustomSetup()));
  }

  collectCustomSetup() {
    return {
      custom_id: this.customId || "",
      name: (this.customField("name") || {}).value || "",
      prompt: (this.customField("prompt") || {}).value || "",
      notes: (this.customField("notes") || {}).value || "",
      title: (this.customField("title") || {}).value || "",
      info: (this.customField("info") || {}).value || "",
      config_ids: [...this.main.querySelectorAll("[data-custom-config]:checked")].map((node) => node.value),
      timeout: Math.max(60, Number((this.customField("timeout") || {}).value) || 3600),
      review_fix: false,
    };
  }

  updateCustomSummary() {
    const node = this.el("custom-summary");
    if (!node) return;
    const config = this.collectCustomSetup();
    const count = config.config_ids.length;
    node.textContent = count
      ? `${count} configuration${count === 1 ? "" : "s"} · ${count} separate run${count === 1 ? "" : "s"}`
      : "Select at least one configuration.";
    const chars = this.el("prompt-chars");
    if (chars) chars.textContent = `${number((config.prompt || "").length)} chars`;
  }

  renderCustomConfigChoices(preferredIds) {
    const host = this.el("custom-configs");
    if (!host) return;
    const currentlySelected = [...host.querySelectorAll("[data-custom-config]:checked")].map((node) => node.value);
    const requested = Array.isArray(preferredIds) ? preferredIds : currentlySelected;
    const selected = new Set(requested.length ? requested.map(String) : (
      this.savedConfigs[0] ? [String(this.savedConfigs[0].id)] : []
    ));
    host.innerHTML = this.savedConfigs.length ? this.savedConfigs.map((config) => {
      const name = config.name || "Untitled";
      const tip = config.description || name;
      return `<label class="bench-config-choice${selected.has(String(config.id)) ? " selected" : ""}" title="${escapeHtml(tip)}">
        <input type="checkbox" data-custom-config value="${escapeHtml(config.id)}"${selected.has(String(config.id)) ? " checked" : ""}>
        <span>${escapeHtml(name)}</span>
      </label>`;
    }).join("") : `
      <div class="bench-config-empty">No saved configurations yet. Create one in the Models window, then select it here.</div>`;
  }

  openConfigManager() {
    if (!this.modelsWindow) {
      this.modelsWindow = new ModelsWindow(this.shell);
      this.modelsWindow.onConfigsChanged = (configs) => {
        this.savedConfigs = configs || [];
        this.renderCustomConfigChoices();
        this.updateCustomSummary();
      };
    }
    this.modelsWindow.open("coder");
  }

  selectBenchConfig(configId) {
    this.savedConfig = this.savedConfigs.find((row) => String(row.id) === String(configId)) || null;
    this.showSetup();
  }

  addTaskRow(seed = null) {
    const host = this.el("task-rows");
    if (!host || host.children.length >= 24) return;
    const task = seed || {};
    const row = document.createElement("div");
    row.className = "bench-task-row";
    row.dataset.taskRow = "1";
    row.dataset.taskId = task.id || `task-${host.children.length + 1}`;
    row.innerHTML = `
      <div class="bench-task-row-head">
        <input type="text" data-task-field="title" placeholder="Task title" value="${escapeHtml(task.title || "")}">
        <input type="text" data-task-field="info" placeholder="What should this prove? (optional)" value="${escapeHtml(task.info || "")}">
        <button class="btn compact ghost" data-bench="remove-task" title="Remove">×</button>
      </div>
      <textarea data-task-field="prompt" spellcheck="false" placeholder="Prompt for this task">${escapeHtml(task.prompt || "")}</textarea>
    `;
    host.appendChild(row);
    this.updateCustomSummary();
  }

  removeTaskRow(trigger) {
    const row = trigger.closest("[data-task-row]");
    if (!row) return;
    row.remove();
    this.saveCustomSetup();
    this.updateCustomSummary();
  }

  addModelRow(seed = null) {
    const host = this.el("model-rows");
    if (!host) return;
    const limit = ((this.meta || {}).limits || {}).custom_models || 8;
    if (host.children.length >= limit) {
      this.shell.toast(`At most ${limit} models per run.`);
      return;
    }
    const row = document.createElement("div");
    row.className = "bench-model-row";
    row.dataset.modelRow = "1";
    row.innerHTML = `
      <select data-model-field="provider">${PROVIDERS.map(([id, label]) => `<option value="${id}">${label}</option>`).join("")}</select>
      <select data-model-field="model"></select>
      <select data-model-field="reasoning"></select>
      <label class="check"><input type="checkbox" data-model-field="fast"> fast</label>
      <button class="btn compact ghost" data-bench="remove-model" title="Remove">\u00d7</button>
    `;
    host.appendChild(row);
    const preferred = seed || {};
    row.querySelector('[data-model-field="provider"]').value = preferred.provider || "openrouter";
    this.refreshModelRow(row, preferred.model, preferred.reasoning);
    row.querySelector('[data-model-field="fast"]').checked = !!preferred.fast;
    this.updateCustomSummary();
  }

  removeModelRow(trigger) {
    const row = trigger.closest("[data-model-row]");
    const host = this.el("model-rows");
    if (!row || !host) return;
    if (host.children.length <= 1) {
      this.shell.toast("Keep at least one model.");
      return;
    }
    row.remove();
    this.saveCustomSetup();
    this.updateCustomSummary();
  }

  refreshModelRow(row, preferredModel = "", preferredReasoning = "") {
    if (!row) return;
    const provider = row.querySelector('[data-model-field="provider"]').value;
    const modelSelect = row.querySelector('[data-model-field="model"]');
    const info = this.providerInfo(provider);
    const models = info.models || [];
    const options = models.length
      ? models.map((model) => [String(model.id), String(model.short_label || model.label || model.id)])
      : (FALLBACK_MODELS[provider] || []).map((id) => [id, id]);
    modelSelect.innerHTML = options
      .map(([id, label]) => `<option value="${escapeHtml(id)}">${escapeHtml(label)}</option>`).join("");
    const wanted = preferredModel || modelSelect.value;
    modelSelect.value = options.some(([id]) => id === wanted)
      ? wanted
      : String((models.find((model) => model.default) || {}).id || (options[0] || [""])[0]);
    this.refreshModelRowReasoning(row, preferredReasoning);
  }

  refreshModelRowReasoning(row, preferred = "") {
    if (!row) return;
    const provider = row.querySelector('[data-model-field="provider"]').value;
    const modelId = row.querySelector('[data-model-field="model"]').value;
    const select = row.querySelector('[data-model-field="reasoning"]');
    const fast = row.querySelector('[data-model-field="fast"]');
    const info = this.providerInfo(provider);
    const model = (info.models || []).find((entry) => String(entry.id) === modelId) || {};
    const efforts = (model.reasoning || info.reasoning || ["low", "medium", "high"]).map(String);
    const prior = preferred || select.value;
    select.innerHTML = efforts
      .map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("");
    select.value = efforts.includes(prior)
      ? prior
      : String(efforts.includes(String(model.default_reasoning || "medium")) ? (model.default_reasoning || "medium") : efforts[0]);
    if (fast) {
      fast.disabled = !model.fast;
      if (!model.fast) fast.checked = false;
    }
  }

  async saveCustom() {
    const config = this.collectCustomSetup();
    const body = {
      name: config.name, prompt: config.prompt, notes: config.notes,
      title: config.title, info: config.info,
    };
    const result = this.customId
      ? await this.request("/api/bench/custom/update", { method: "POST", body: { id: this.customId, ...body } })
      : await this.request("/api/bench/custom", { method: "POST", body });
    if (!result || result.ok === false) {
      this.shell.toast((result && result.error) || "Could not save the test.", "error");
      return;
    }
    this.customId = result.definition.id;
    this.saveCustomSetup();
    this.shell.toast("Custom test saved.");
    await this.showCustom(this.customId);
  }

  async deleteCustom() {
    if (!this.customId) return;
    const confirmed = await this.shell.confirm("Delete this custom test?", "Past runs stay on disk; only the saved prompt is removed.");
    if (!confirmed) return;
    const result = await this.request("/api/bench/custom/delete", { method: "POST", body: { id: this.customId } });
    if (!result || result.ok === false) {
      this.shell.toast((result && result.error) || "Could not delete it.", "error");
      return;
    }
    this.customId = null;
    this.shell.toast("Custom test deleted.");
    await this.showCustom();
  }

  async startCustom() {
    let config = this.collectCustomSetup();
    if (!config.prompt.trim()) {
      this.shell.toast("Write a prompt first.", "error");
      return;
    }
    const selectedConfigs = config.config_ids
      .map((id) => this.savedConfigs.find((row) => String(row.id) === String(id)))
      .filter(Boolean);
    if (!selectedConfigs.length) {
      this.shell.toast("Select at least one saved configuration.", "error");
      return;
    }
    // Always save before running so re-runs keep the same definition id.
    const saved = this.customId
      ? await this.request("/api/bench/custom/update", {
          method: "POST",
          body: {
            id: this.customId, name: config.name, prompt: config.prompt, notes: config.notes,
            title: config.title, info: config.info,
          },
        })
      : await this.request("/api/bench/custom", {
          method: "POST",
          body: {
            name: config.name || "Untitled custom", prompt: config.prompt, notes: config.notes,
            title: config.title, info: config.info,
          },
        });
    if (!saved || saved.ok === false) {
      this.shell.toast((saved && saved.error) || "Could not save the test.", "error");
      return;
    }
    this.customId = saved.definition.id;
    config = this.collectCustomSetup();
    this.saveCustomSetup();

    const button = this.main.querySelector('[data-bench="start-custom"]');
    if (button) button.disabled = true;
    const result = await this.request("/api/bench/groups", {
      method: "POST",
      body: {
        label: config.name || saved.definition.name,
        configurations: selectedConfigs,
        config: {
          kind: "custom",
          custom_id: this.customId,
          timeout: config.timeout,
          review_fix: config.review_fix,
        },
      },
    });
    if (button) button.disabled = false;
    if (!result || result.ok === false || !result.group) {
      this.shell.toast((result && result.error) || "Could not start the run.", "error");
      return;
    }
    this.runs = [result.group, ...this.runs];
    if ((result.errors || []).length) this.shell.toast(`Run started, but ${result.errors.length} configuration failed to launch.`, "error");
    else this.shell.toast(`${selectedConfigs.length} configurations started in one run.`);
    this.openGroup(result.group.id);
  }

  summariseCreated(run) {
    const tasks = run.tasks || [];
    return {
      ...run,
      kind: (run.config || {}).kind || "suite",
      custom_id: (run.config || {}).custom_id || "",
      tasks: tasks.length,
      finished: 0,
      passed: 0,
      total_tokens: 0,
    };
  }

  // ------------------------------------------------------------ run group

  async openGroup(groupId) {
    this.groupId = String(groupId);
    this.runId = null;
    this.run = null;
    this.taskId = null;
    this.stopEvents();
    this.renderRuns();
    const result = await this.request(`/api/bench/group?id=${encodeURIComponent(this.groupId)}`);
    if (!result || !result.ok) {
      this.shell.toast("That benchmark group is gone.", "error");
      this.showSetup();
      return;
    }
    this.group = result.group;
    this.showGroup();
  }

  showGroup() {
    this.mode = "group";
    this.runId = null;
    this.run = null;
    this.taskId = null;
    this.stopEvents();
    this.main.innerHTML = `<div class="bench-pane" data-bench="group-pane"></div>`;
    this.renderGroup();
  }

  renderGroup() {
    const pane = this.el("group-pane");
    const group = this.group;
    if (!pane || !group) return;
    const active = RUN_ACTIVE.has(String(group.status));
    const elapsed = active
      ? Math.max(0, (Date.now() / 1000) - Number(group.created_at || 0))
      : Number(group.total_seconds || 0);
    const children = group.runs || [];
    const isCustom = String(group.kind) === "custom";
    const isProject = String(group.kind) === "project";
    const stopping = String(group.status) === "stopping" || !!group.stop_requested;
    const total = Number(group.tasks || children.length || 0);
    const finished = Number(group.finished || 0);
    const passed = Number(group.passed || 0);
    const unfinished = Math.max(0, total - passed);
    const canParallelContinue = !active && !isCustom && !isProject && unfinished > 0;
    const progress = total ? Math.max(0, Math.min(100, (finished / total) * 100)) : 0;
    pane.innerHTML = `
      <div class="bench-pane-head">
        <div style="min-width:0">
          <div class="title">${escapeHtml(group.label || "Custom benchmark")}
            <span class="status-pill ${escapeHtml(String(group.status))}">${escapeHtml(String(group.status))}</span>
            <span class="status-pill custom">${children.length} attempts</span>
            ${isProject ? '<span class="status-pill custom">isolated project</span>' : isCustom ? '<span class="status-pill custom">manual quality</span>' : '<span class="status-pill passed">external graders</span>'}
          </div>
          <div class="meta">${isProject ? `Snapshot ${escapeHtml(String(group.project_snapshot_hash || "").slice(0, 12))} · one isolated copy per lane` : isCustom ? "One saved prompt" : "Identical fresh task repositories"} · ${group.comparable === false ? "mixed task sets" : "matched task set"} · ${escapeHtml(String(group.id))}</div>
        </div>
        <div class="actions">
          ${canParallelContinue ? `<button class="btn compact accent" data-bench="parallel-continue">Run ${unfinished} unfinished in parallel</button>` : ""}
          <button class="btn compact" data-bench="stop"${active && !stopping ? "" : " disabled"}>
            ${stopping ? "Stopping all\u2026" : `Stop all ${children.length}`}
          </button>
        </div>
      </div>
      ${group.continued_from_group ? `<div class="bench-campaign-budget">
        <span><b>${number(group.seeded_task_count || 0)}</b> passing results carried forward from ${escapeHtml(String(group.continued_from_group))}</span>
        <small>Only unfinished tasks received fresh workspaces; the original campaign remains unchanged.</small>
      </div>` : ""}
      <div class="bench-stats">
        <span><b>${number(group.total_tokens)}</b>tokens</span>
        <span><b>${money(group.cost_usd)}</b>${group.cost_comparable === false ? "reported + API-eq. cost" : "reported cost"}${group.cost_available === false ? " (partial)" : ""}</span>
        <span><b>${formatDuration(elapsed)}</b>elapsed</span>
        <span><b>${number(group.tool_calls)}</b>tool calls</span>
        <span><b>${number(group.files_edited)}</b>files <i>+${number(group.lines_added)} / -${number(group.lines_deleted)}</i></span>
        <span><b>${group.finished || 0}/${group.tasks || 0}</b>finished</span>
      </div>
      <div class="bench-group-overview" aria-live="polite">
        <div>
          <b>${finished} of ${total} task attempts finished</b>
          <span>${stopping ? "Stopping every active agent and preserving completed results." : "Every card below updates live. Open one for its transcript and workspace."}</span>
        </div>
        <div class="bench-group-progress"><i style="width:${progress.toFixed(1)}%"></i></div>
      </div>
      ${(group.budget || {}).cap_usd ? `<div class="bench-campaign-budget">
        <span><b>${money((group.budget || {}).spent_usd)}</b> of ${money((group.budget || {}).cap_usd)} observed-cost ceilings</span>
        <i><em style="width:${Math.min(100, (Number((group.budget || {}).spent_usd || 0) / Number((group.budget || {}).cap_usd || 1)) * 100)}%"></em></i>
        <small>Observed-cost stop; an in-flight provider request can overshoot.</small>
      </div>` : ""}
      <div class="bench-group-configs">
        ${children.map((run) => {
          const summary = run.summary || {};
          const usage = summary.usage || {};
          const runTasks = run.tasks || [];
          const task = runTasks.find((row) => TASK_ACTIVE.has(String(row.status))) || runTasks[0] || {};
          const taskElapsed = task.seconds || (TASK_ACTIVE.has(String(task.status)) && task.started_at
            ? (Date.now() / 1000) - Number(task.started_at) : 0);
          const stages = task.pipeline_stages || {};
          const current = ["reviewer", "coder", "consultant", "planner", "scout"]
            .find((name) => String((stages[name] || {}).phase) === "started");
          const runFinished = Number(summary.finished || 0);
          const runTotal = Number(summary.tasks || runTasks.length || 0);
          const taskFinished = task.passed !== null && task.passed !== undefined;
          const cardProgress = runTotal ? Math.min(100, (runFinished / runTotal) * 100) : (taskFinished ? 100 : 3);
          const currentLabel = current === "planner" ? "consultant" : current;
          const stageLabel = currentLabel ? `${currentLabel} active` : (runTotal ? `${runFinished}/${runTotal} finished` : String(task.status || run.status));
          const agentId = Number(run.agent_id || task.agent_id || 0);
          const previewPort = Number(run.preview_port || task.preview_port || 0);
          const primaryModel = String(task.native_primary_model || task.model || "");
          const auxiliaryModels = Array.isArray(task.native_models_used)
            ? task.native_models_used.filter((value) => String(value) && String(value) !== primaryModel)
            : [];
          const auxiliaryLabel = auxiliaryModels.length ? ` · aux ${auxiliaryModels.join(", ")}` : "";
          const projectResult = task.project_result || {};
          return `<button class="bench-group-config ${escapeHtml(String(run.status))}" data-run="${escapeHtml(run.id)}">
            <span class="bench-group-config-head"><b>${escapeHtml(run.saved_config_name || run.label || run.id)}</b><i><em></em>${escapeHtml(String(run.status))}</i></span>
            ${agentId ? `<span class="bench-group-identity">Agent #${String(agentId).padStart(3, "0")}<i>127.0.0.1:${previewPort}</i></span>` : ""}
            <span class="bench-group-stage"><b>${escapeHtml(stageLabel)}</b><i><em style="width:${cardProgress}%"></em></i></span>
            <span class="bench-group-model">${escapeHtml(String((run.config || {}).harness_label || run.saved_config_name || run.engine || "aiOS"))} · ${escapeHtml(primaryModel)}${escapeHtml(auxiliaryLabel)} · ${escapeHtml(String(task.reasoning || "off"))}${task.fast ? " · fast" : ""}</span>
            <span class="bench-group-metrics">
              <span><b>${number(summary.passed)}/${number(summary.finished)}</b> ${isProject ? "completed" : "passed"}</span>
              <span><b>${number(usage.total_tokens)}</b> tokens</span>
              <span><b>${money(summary.cost_usd)}</b> cost</span>
              <span><b>${taskElapsed ? formatDuration(taskElapsed) : "—"}</b> elapsed</span>
              <span><b>${number(summary.tool_calls)}</b> tools</span>
              ${task.model_request_count !== null && task.model_request_count !== undefined ? `<span><b>${number(task.model_request_count)}</b> model requests</span>` : ""}
              <span><b>${number(task.files_edited)}</b> files</span>
              <span><b>+${number(task.lines_added)} / -${number(task.lines_deleted)}</b> diff</span>
            </span>
            ${isProject && !projectResult.error ? `<span class="bench-project-card-diff"><b>${number(projectResult.changed_files)}</b> source files changed · ${number(projectResult.added)} added · ${number(projectResult.modified)} modified · ${number(projectResult.deleted)} deleted</span>` : ""}
            ${this.roleMetrics(task, run.saved_config_roles || {})}
          </button>`;
        }).join("")}
      </div>
      ${this.groupReport(group)}
    `;
  }

  groupReport(group) {
    const report = (group || {}).report || {};
    if (report.error) return `<div class="bench-error">${escapeHtml(report.error)}</div>`;
    const harnesses = report.harnesses || [];
    if (!harnesses.length) return "";
    const suites = report.by_suite || [];
    const recommendations = report.recommendations || [];
    return `<section class="bench-report">
      <div class="bench-report-head">
        <div><b>BENCHMARK REPORT</b><span>${escapeHtml(report.lane === "harness-only" ? "Harness-only lane" : "Harness + model lane")}</span></div>
        <i class="${report.comparable ? "ok" : "warn"}">${report.comparable ? "matched task set" : escapeHtml((report.comparability_reasons || ["not comparable"])[0])}</i>
      </div>
      <div class="bench-report-table" style="--bench-cols:${Math.max(1, suites.length)}">
        <div class="bench-report-row head">
          <span>Harness</span>
          ${suites.map((row) => `<span>${escapeHtml(row.suite)}</span>`).join("")}
          <span>Median tokens</span><span>Median requests</span><span>Median time</span><span>Median cost</span>
        </div>
        ${harnesses.map((harness) => {
          const metricsReady = Number(harness.attempt_count || 0) > 0
            && Number(harness.pending_count || 0) === 0;
          return `<div class="bench-report-row">
            <span><b>${escapeHtml(harness.name || harness.id)}</b><i>${escapeHtml((harness.models || []).join(", "))}${(harness.cost_provenances || []).length ? ` · ${escapeHtml(harness.cost_provenances.join(", "))}` : ""}</i></span>
            ${suites.map((suite) => {
              const cell = ((suite.harnesses || {})[harness.id] || {});
              const evaluated = Number(cell.evaluated_count || 0);
              const passed = Number(cell.passed_count || 0);
              return `<span class="bench-report-cell ${evaluated && passed === evaluated ? "pass" : evaluated ? "fail" : "pending"}">${evaluated ? `${passed}/${evaluated}` : "—"}</span>`;
            }).join("")}
            <span>${!metricsReady || harness.median_tokens === null || harness.median_tokens === undefined ? "—" : number(harness.median_tokens)}</span>
            <span title="${number(harness.model_request_count_available)} exact · ${number(harness.model_request_count_unavailable)} unavailable · ${escapeHtml((harness.model_request_count_sources || []).join(", "))}">${!metricsReady || harness.median_model_request_count === null || harness.median_model_request_count === undefined ? "—" : number(harness.median_model_request_count)}${Number(harness.model_request_count_unavailable || 0) ? "*" : ""}</span>
            <span>${!metricsReady || harness.median_seconds === null || harness.median_seconds === undefined ? "—" : formatDuration(harness.median_seconds)}</span>
            <span>${!metricsReady ? "—" : harness.cost_status === "available" ? money(harness.median_cost_usd) : "n/a"}</span>
          </div>`;
        }).join("")}
      </div>
      ${recommendations.length ? `<div class="bench-recommendations">
        <b>What to improve next</b>
        ${recommendations.slice(0, 5).map((row) => `<div><em>P${number(row.priority)}</em><span><b>${escapeHtml(row.signal)}</b>${escapeHtml(row.action)}</span></div>`).join("")}
      </div>` : '<div class="bench-report-clean">No failure signal yet. Finish the campaign before drawing conclusions.</div>'}
      <div class="bench-report-foot">${escapeHtml(report.lane_reason || "")} · Task set <code>${escapeHtml(String(report.task_set_hash || "").slice(0, 12))}</code></div>
    </section>`;
  }

  // -------------------------------------------------------------- open run

  async openRun(runId) {
    const fromGroup = this.mode === "group" ? this.groupId : null;
    this.runId = String(runId);
    if (!fromGroup) this.groupId = null;
    this.taskId = null;
    this.applyPreview = null;
    this.applyReceipt = null;
    this.stopEvents();
    this.renderRuns();
    const result = await this.request(`/api/bench/run?id=${encodeURIComponent(this.runId)}`);
    if (!result || !result.ok) {
      this.shell.toast("That run is gone.", "error");
      this.showSetup();
      return;
    }
    this.run = result.run;
    if (fromGroup) this.groupId = fromGroup;
    this.showRun();
  }

  showRun() {
    this.mode = "run";
    this.taskId = null;
    this.stopEvents();
    this.main.innerHTML = `<div class="bench-pane" data-bench="run-pane"></div>`;
    this.renderRun();
  }

  renderRun() {
    const pane = this.el("run-pane");
    const run = this.run;
    if (!pane || !run) return;

    const config = run.config || {};
    const summary = run.summary || {};
    const tasks = run.tasks || [];
    const finished = tasks.filter((task) => task.passed !== null && task.passed !== undefined).length;
    const active = RUN_ACTIVE.has(String(run.status));
    const usage = summary.usage || {};
    const isCustom = String(config.kind) === "custom";
    const isProject = String(config.kind) === "project";
    const isManual = isCustom || isProject;
    const projectTask = tasks[0] || {};
    const projectResult = projectTask.project_result || {};
    const agentId = Number(run.agent_id || (tasks[0] || {}).agent_id || 0);
    const previewPort = Number(run.preview_port || (tasks[0] || {}).preview_port || 0);
    const modelLine = isCustom
      ? `${(config.models || []).length} model${(config.models || []).length === 1 ? "" : "s"} \u00b7 custom`
      : `${escapeHtml(String(config.harness_label || config.provider || "").toUpperCase())} \u00b7 ${escapeHtml(String(config.model || "native configured model"))} \u00b7 ${escapeHtml(String(config.reasoning || ""))}${config.fast ? " \u00b7 fast" : ""}`;

    pane.innerHTML = `
      <div class="bench-pane-head">
        <div style="min-width:0">
          <div class="title">${escapeHtml(run.label || "Benchmark run")}
            <span class="status-pill ${escapeHtml(String(run.status))}">${escapeHtml(String(run.status))}</span>
            ${isCustom ? '<span class="status-pill custom">custom</span>' : ""}
            ${isProject ? '<span class="status-pill custom">isolated project</span>' : ""}
          </div>
          <div class="meta">${modelLine}
            \u00b7 ${config.concurrency} at a time \u00b7 ${escapeHtml(run.id)}</div>
          ${run.saved_config_name ? `<div class="meta">Saved configuration · ${escapeHtml(run.saved_config_name)}</div>` : ""}
          ${isProject ? `<div class="meta">Source snapshot · ${escapeHtml(config.project_source_name || "project")} · <code>${escapeHtml(String(config.project_snapshot_hash || "").slice(0, 16))}</code></div>` : ""}
          <div class="meta">Cost provenance · ${escapeHtml(String(config.cost_provenance || "provider_reported"))}${run.task_set_hash ? ` · task set ${escapeHtml(String(run.task_set_hash).slice(0, 12))}` : ""}</div>
          ${agentId ? `<div class="bench-run-identity">Agent #${String(agentId).padStart(3, "0")}<span>Preview port ${previewPort}</span><code>http://127.0.0.1:${previewPort}</code></div>` : ""}
        </div>
        <div class="actions">
          ${this.groupId ? '<button class="btn compact ghost" data-bench="to-group">← Comparison</button>' : ""}
          ${isProject && !active ? '<button class="btn compact accent" data-bench="preview-project-apply">Preview copy to real</button>' : ""}
          <button class="btn compact ghost" data-bench="delete"${active ? " disabled" : ""}>Delete</button>
          <button class="btn compact" data-bench="stop"${active && !run.stop_requested ? "" : " disabled"}>${run.stop_requested ? "Stopping\u2026" : "Stop"}</button>
        </div>
      </div>

      ${run.error ? `<div class="bench-error">${escapeHtml(run.error)}</div>` : ""}
      ${(run.budget || {}).cap_usd ? `<div class="bench-campaign-budget">
        <span><b>${money((run.budget || {}).spent_usd)}</b> of ${money((run.budget || {}).cap_usd)} observed-cost ceiling</span>
        <i><em style="width:${Math.min(100, (Number((run.budget || {}).spent_usd || 0) / Number((run.budget || {}).cap_usd || 1)) * 100)}%"></em></i>
        <small>${escapeHtml((run.budget || {}).note || "One in-flight provider request can overshoot.")}</small>
      </div>` : ""}
      ${isManual && config.prompt ? `<div class="bench-prompt"><div class="head">PROMPT</div><pre>${escapeHtml(config.prompt)}</pre></div>` : ""}
      ${isProject ? this.projectResultPanel(projectResult) : ""}
      ${isProject ? this.projectApplyPanel() : ""}

      ${isManual ? `
      <div class="bench-stats">
        <span><b>${number(usage.total_tokens)}</b>tokens</span>
        <span><b>${money(summary.cost_usd)}</b>spent</span>
        <span><b>${summary.total_seconds ? formatDuration(summary.total_seconds) : "\u2014"}</b>elapsed</span>
        <span><b>${number(usage.input_tokens)}</b>in <i>(${number(usage.cached_input_tokens)} cached)</i></span>
        <span><b>${number(usage.output_tokens)}</b>out <i>(${number(usage.reasoning_tokens)} reasoning)</i></span>
        <span><b>${number(summary.tool_calls)}</b>tool calls</span>
        ${projectTask.model_request_count !== null && projectTask.model_request_count !== undefined ? `<span><b>${number(projectTask.model_request_count)}</b>model requests</span>` : ""}
        <span><b>${finished}/${tasks.length}</b>finished</span>
      </div>
      ` : `
      <div class="bench-score">
        <div class="dial ${this.scoreClass(summary.score, run.status)}">
          <b>${this.scoreText(summary.score, summary.finished)}</b>
          <i>${escapeHtml(summary.finished ? String(summary.grade || "") : (active ? "running" : "no result"))}</i>
        </div>
        <div class="parts">
          ${this.scorePart("Correctness", summary, "correctness", `${summary.passed || 0} of ${summary.finished || 0} solved`)}
          ${this.scorePart("Efficiency", summary, "efficiency", summary.tokens_per_pass ? `${number(summary.tokens_per_pass)} tokens per solved task` : "no solved task yet")}
          ${this.scorePart("Speed", summary, "speed", summary.seconds_per_pass ? `${summary.seconds_per_pass}s per solved task` : "no solved task yet")}
          <button class="btn compact ghost how" data-bench="score-help">How is this scored?</button>
        </div>
      </div>

      <div class="bench-stats">
        <span><b>${number(usage.total_tokens)}</b>tokens</span>
        <span><b>${money(summary.cost_usd)}</b>spent</span>
        <span><b>${number(usage.input_tokens)}</b>in <i>(${number(usage.cached_input_tokens)} cached)</i></span>
        <span><b>${number(usage.output_tokens)}</b>out <i>(${number(usage.reasoning_tokens)} reasoning)</i></span>
        <span><b>${summary.cost_per_pass ? money(summary.cost_per_pass) : "\u2014"}</b>per solved task</span>
        <span><b>${number(summary.tool_calls)}</b>tool calls</span>
        ${projectTask.model_request_count !== null && projectTask.model_request_count !== undefined ? `<span><b>${number(projectTask.model_request_count)}</b>model requests</span>` : ""}
        <span><b>${summary.cache_hit_rate === null || summary.cache_hit_rate === undefined ? "\u2014" : `${Math.round(summary.cache_hit_rate * 100)}%`}</b>cache hits</span>
      </div>
      `}

      <div class="bench-progress">
        <div class="bar"><i style="width:${tasks.length ? Math.round((finished / tasks.length) * 100) : 0}%"></i></div>
        <span>${finished} of ${tasks.length} finished</span>
      </div>

      <div class="bench-tasks">
        ${tasks.map((task) => this.taskCard(task, isManual)).join("")}
      </div>
    `;
  }

  projectResultPanel(result) {
    if (!result || !Object.keys(result).length) return '<div class="bench-project-result pending">Result diff will appear when this lane finishes.</div>';
    if (result.error) return `<div class="bench-error">${escapeHtml(result.error)}</div>`;
    const changes = Array.isArray(result.changes) ? result.changes : [];
    return `<details class="bench-project-result"${changes.length <= 12 ? " open" : ""}>
      <summary><b>${number(result.changed_files)} source files changed</b><span>${number(result.added)} added · ${number(result.modified)} modified · ${number(result.deleted)} deleted</span></summary>
      <div class="bench-project-change-list">
        ${changes.length ? changes.map((row) => `<div class="${escapeHtml(row.status)}"><i>${escapeHtml(row.status)}</i><code>${escapeHtml(row.path)}</code><span>${number(row.before_size)} → ${number(row.after_size)} bytes</span></div>`).join("") : '<div class="empty">The lane made no source changes.</div>'}
      </div>
      <footer>Result <code>${escapeHtml(String(result.result_hash || "").slice(0, 16))}</code> · diff <code>${escapeHtml(String(result.diff_hash || "").slice(0, 16))}</code></footer>
    </details>`;
  }

  projectApplyPanel() {
    if (this.applyReceipt) return `<div class="bench-project-apply success"><b>Copied to the real project</b><span>${number((this.applyReceipt.applied || []).length)} files applied. Rollback checkpoint:</span><code>${escapeHtml(this.applyReceipt.checkpoint || "")}</code></div>`;
    const preview = this.applyPreview;
    if (!preview) return "";
    const rows = Array.isArray(preview.changes) ? preview.changes : [];
    return `<section class="bench-project-apply${preview.conflicts ? " conflict" : ""}">
      <div class="head"><b>Copy preview</b><span>${number(preview.ready)} ready · ${number(preview.already_applied)} already identical · ${number(preview.conflicts)} conflicts</span></div>
      <div class="bench-project-change-list">
        ${rows.map((row) => `<div class="${escapeHtml(row.disposition)}"><i>${escapeHtml(row.disposition)}</i><code>${escapeHtml(row.path)}</code><span>${escapeHtml(row.status)}</span></div>`).join("") || '<div class="empty">Nothing to copy.</div>'}
      </div>
      ${preview.conflicts ? '<p>The real project differs from the source snapshot on these paths. Nothing can be applied until you resolve that drift and preview again.</p>' : '<p>Confirming rechecks every file, creates a rollback checkpoint, then applies only this lane’s changed files.</p>'}
      <div class="actions"><button class="btn accent" data-bench="confirm-project-apply"${preview.conflicts || !preview.ready ? " disabled" : ""}>Copy ${number(preview.ready)} files to real project</button></div>
    </section>`;
  }

  async previewProjectApply() {
    if (!this.runId || String(((this.run || {}).config || {}).kind) !== "project") return;
    const task = ((this.run || {}).tasks || [])[0] || {};
    const result = await this.request("/api/bench/project/apply-preview", {
      method: "POST", body: { run_id: this.runId, task_id: task.id || "" },
    });
    if (!result || result.ok === false) {
      this.shell.toast((result && result.error) || "Could not compare this lane with the real project.", "error");
      return;
    }
    this.applyPreview = result.preview;
    this.applyReceipt = null;
    this.renderRun();
  }

  async confirmProjectApply() {
    const preview = this.applyPreview;
    if (!preview || preview.conflicts || !preview.ready) return;
    const deletionText = preview.deletions
      ? ` This will explicitly delete ${preview.deletions} source file${preview.deletions === 1 ? "" : "s"}.`
      : "";
    const confirmed = await this.shell.confirm(
      `Copy this lane into the real project?`,
      `${preview.ready} changed file${preview.ready === 1 ? "" : "s"} will be applied after one final drift check. A rollback checkpoint is created first.${deletionText}`,
    );
    if (!confirmed) return;
    const result = await this.request("/api/bench/project/apply-confirm", {
      method: "POST",
      body: { preview_id: preview.id, allow_deletions: !!preview.deletions },
    });
    if (!result || result.ok === false) {
      this.shell.toast((result && result.error) || "The real project changed; nothing was copied.", "error");
      await this.previewProjectApply();
      return;
    }
    this.applyReceipt = result;
    this.applyPreview = null;
    this.shell.toast(`${(result.applied || []).length} files copied. Rollback checkpoint kept.`, "success");
    this.renderRun();
  }

  scorePart(label, summary, key, detail) {
    const parts = summary.parts || {};
    const weights = summary.weights || {};
    const earned = Number(parts[key] || 0);
    const max = Number(weights[key] || 0);
    const share = max ? Math.round((earned / max) * 100) : 0;
    return `
      <div class="part">
        <span class="label">${escapeHtml(label)}</span>
        <span class="bar"><i style="width:${share}%"></i></span>
        <span class="value">${earned.toFixed(1)}<em>/${max}</em></span>
        <span class="detail">${escapeHtml(detail)}</span>
      </div>
    `;
  }

  roleMetrics(task, configuredRoles = null) {
    const rows = (task && task.role_usage && typeof task.role_usage === "object") ? task.role_usage : {};
    const configured = configuredRoles || ((this.run || {}).saved_config_roles || {});
    const order = [
      { stage: "scout", keys: ["scout"] },
      { stage: "consultant", keys: ["consultant", "planner"] },
      { stage: "coder", keys: ["coder"] },
      { stage: "reviewer", keys: ["reviewer"] },
    ];
    const visible = order.map((entry) => ({
      ...entry,
      key: entry.keys.find((key) => rows[key] || ((configured[key] || {}).enabled)),
    })).filter((entry) => entry.key);
    if (!visible.length) return "";
    return `<span class="bench-role-metrics">${visible.map(({ stage, key }) => {
      const row = rows[key] || {};
      const usage = row.usage || {};
      const phase = String(row.phase || (TASK_ACTIVE.has(String((task || {}).status || "")) ? "pending" : "not-run"));
      const total = Number(usage.total_tokens || 0);
      const cost = Number(usage.cost_usd || 0);
      const seconds = Number(row.seconds || 0);
      const model = row.model || ((configured[key] || {}).model) || "";
      const attempts = Number(row.attempts || 0);
      const tip = [
        model,
        `${number(usage.input_tokens)} input`,
        `${number(usage.cached_input_tokens)} cached`,
        `${number(usage.output_tokens)} output`,
        `${number(usage.reasoning_tokens)} reasoning`,
        `$${cost.toFixed(10)}`,
      ].filter(Boolean).join(" · ");
      return `<span class="bench-role-metric ${escapeHtml(phase)}" title="${escapeHtml(tip)}">
        <b>${escapeHtml(stage)}${attempts > 1 ? ` ×${attempts}` : ""}</b>
        <i>${escapeHtml(shortModel(model))}</i>
        <em>${total.toLocaleString()} tok</em>
        <em>${money(cost)}</em>
        <em>${seconds || !TASK_ACTIVE.has(String((task || {}).status || "")) ? formatDuration(seconds) : "—"}</em>
      </span>`;
    }).join("")}</span>`;
  }

  efficiencyTrace(task) {
    const storedTrace = (task && task.efficiency_trace && typeof task.efficiency_trace === "object")
      ? task.efficiency_trace
      : null;
    const requestCount = task && task.model_request_count !== null && task.model_request_count !== undefined
      && String(task.model_request_count_source || "unavailable") !== "unavailable"
      ? Number(task.model_request_count) : null;
    if ((!storedTrace || !Number(storedTrace.total_calls || 0)) && requestCount === null) return "";
    const trace = storedTrace || {};
    const total = Number(trace.total_calls || 0);
    const firstEdit = trace.first_edit_call === null || trace.first_edit_call === undefined
      ? "no edit"
      : `${formatDuration(Number(trace.time_to_first_edit_seconds || 0))} to edit`;
    const beforeAfter = trace.first_edit_call === null || trace.first_edit_call === undefined
      ? ""
      : `${number(trace.calls_before_first_edit)} before / ${number(trace.calls_after_first_edit)} after`;
    const waste = [
      Number(trace.failed_calls || 0) ? `${number(trace.failed_calls)} failed` : "",
      Number(trace.duplicate_calls || 0) ? `${number(trace.duplicate_calls)} duplicate` : "",
      Number(trace.retry_calls || 0) ? `${number(trace.retry_calls)} retry` : "",
      Number(trace.overlapping_read_calls || 0) ? `${number(trace.overlapping_read_calls)} overlapping read` : "",
      Number(trace.post_edit_inspection_calls || 0) ? `${number(trace.post_edit_inspection_calls)} post-edit inspection` : "",
    ].filter(Boolean);
    const buckets = (label, values) => {
      const rows = values && typeof values === "object" ? Object.entries(values) : [];
      if (!rows.length) return "";
      return `<span><b>${escapeHtml(label)}</b>${rows.map(([name, count]) =>
        `<i>${escapeHtml(name)} ${number(count)}</i>`).join("")}</span>`;
    };
    const sequence = Array.isArray(trace.sequence) ? trace.sequence : [];
    const modelRounds = Array.isArray(task.model_request_rounds) ? task.model_request_rounds : [];
    const modelRequestLabel = requestCount === null ? "" : `${number(requestCount)} model request${requestCount === 1 ? "" : "s"}`;
    return `<details class="bench-efficiency-trace">
      <summary>
        <b>Efficiency trace</b>
        <span>${number(total)} tool calls${modelRequestLabel ? ` · ${escapeHtml(modelRequestLabel)}` : ""} · ${escapeHtml(firstEdit)}${beforeAfter ? ` · ${escapeHtml(beforeAfter)}` : ""}${waste.length ? ` · ${escapeHtml(waste.join(" · "))}` : ""}</span>
      </summary>
      <div class="bench-efficiency-body">
        ${requestCount === null ? "" : `<div class="bench-model-rounds">
          <div class="head"><b>${escapeHtml(modelRequestLabel)}</b><span>${escapeHtml(String(task.model_request_count_source || "exact local telemetry"))}${Number(task.model_request_rounds_omitted || 0) ? ` · ${number(task.model_request_rounds_omitted)} older rows omitted` : ""}</span></div>
          ${modelRounds.length ? `<div class="rows">${modelRounds.map((round) => {
            const usage = round.usage || {};
            return `<span class="${escapeHtml(String(round.status || "completed"))}"><b>#${number(round.sequence)}</b><i>${escapeHtml(round.role || round.provider || "model")}</i><code>${escapeHtml(shortModel(round.model || ""))}</code><em>${number(usage.total_tokens)} tok</em><em>${escapeHtml(round.stop_reason || round.status || "")}</em></span>`;
          }).join("")}</div>` : '<div class="unavailable">Per-request usage is not exposed by this harness.</div>'}
        </div>`}
        <div class="bench-efficiency-buckets">
          ${buckets("Roles", trace.tools_by_role)}
          ${buckets("Types", trace.tools_by_type)}
          ${buckets("Tools", trace.tools_by_name)}
        </div>
        <ol class="bench-tool-sequence">
          ${sequence.map((call) => {
            const flags = [
              String(call.outcome || "") === "failed" ? "failed" : "",
              call.duplicate_of ? `duplicate of #${number(call.duplicate_of)}` : "",
              call.retry_of ? `retry of #${number(call.retry_of)}` : "",
              Array.isArray(call.overlaps_with) && call.overlaps_with.length
                ? `overlaps #${call.overlaps_with.map(number).join(", #")}` : "",
              call.post_edit_inspection ? "after edit" : "",
            ].filter(Boolean);
            const timing = call.elapsed_seconds === null || call.elapsed_seconds === undefined
              ? "" : `+${Number(call.elapsed_seconds).toFixed(3)}s`;
            const preview = String(call.argument_preview || call.target || "");
            return `<li class="${escapeHtml(String(call.outcome || "incomplete"))}">
              <span class="bench-tool-index">#${number(call.index)}</span>
              <span class="bench-tool-main">
                <b>${escapeHtml(call.tool || "tool")}</b>
                <code title="${escapeHtml(preview)}">${escapeHtml(call.target || preview || "—")}</code>
                ${flags.length ? `<em>${escapeHtml(flags.join(" · "))}</em>` : ""}
              </span>
              <span class="bench-tool-meta">${escapeHtml(call.role || "unattributed")} · ${escapeHtml(call.type || "tool")}${timing ? ` · ${escapeHtml(timing)}` : ""}</span>
            </li>`;
          }).join("")}
        </ol>
        ${Number(trace.omitted_calls || 0)
          ? `<div class="bench-efficiency-omitted">${number(trace.omitted_calls)} later calls omitted from this bounded trace; totals above include them.</div>`
          : ""}
      </div>
    </details>`;
  }

  taskCard(task, isCustom = false) {
    const usage = task.usage || {};
    const status = String(task.status || "pending");
    const live = TASK_ACTIVE.has(status);
    const suiteLabel = isCustom
      ? shortModel(task.model || task.title || task.id)
      : String(task.suite || "");
    const name = isCustom
      ? `${escapeHtml(String(task.provider || "").toUpperCase())} \u00b7 ${escapeHtml(String(task.reasoning || ""))}${task.fast ? " \u00b7 fast" : ""}`
      : escapeHtml(task.title || task.id);
    return `
      <button class="bench-task ${escapeHtml(status)}${String(task.id) === String(this.taskId) ? " selected" : ""}" data-task="${escapeHtml(task.id)}">
        <span class="dot"></span>
        <span class="body">
          <span class="line">
            <span class="suite">${escapeHtml(suiteLabel)}${task.official ? '<i class="bench-origin">PUBLIC</i>' : ""}</span>
            <span class="name">${name}</span>
          </span>
          <span class="line stats">
            ${live ? `<span class="working">${escapeHtml(status)}\u2026</span>` : `<span class="verdict">${task.passed ? (isCustom ? "done" : "passed") : escapeHtml(status)}</span>`}
            <span>${usage.total_tokens ? `${compactTokens(usage.total_tokens)} tok` : "\u2014"}</span>
            <span>${task.seconds ? `${Math.round(task.seconds)}s` : "\u2014"}</span>
            ${task.model_request_count !== null && task.model_request_count !== undefined ? `<span>${number(task.model_request_count)} req</span>` : ""}
            <span>${usage.cost_usd ? money(usage.cost_usd) : ""}</span>
            ${task.error ? `<span class="why" title="${escapeHtml(task.error)}">${escapeHtml(task.error)}</span>` : ""}
          </span>
          ${this.roleMetrics(task)}
        </span>
      </button>
    `;
  }

  explainScore() {
    const scoring = (this.meta && this.meta.scoring) || {};
    const weights = scoring.weights || {};
    const reference = scoring.reference || {};
    this.shell.sheet("How the score works", [
      ["Correctness", `${weights.correctness} points \u00b7 share of tasks whose hidden checks all passed`],
      ["Efficiency", `${weights.efficiency} points \u00b7 ${number(reference.tokens_per_pass)} tokens per solved task scores full marks`],
      ["Speed", `${weights.speed} points \u00b7 ${reference.seconds_per_pass}s per solved task scores full marks`],
    ], "Tokens and seconds are counted per solved task, so a run that burns budget failing is penalised twice. Neither budget scores above full marks, so read tokens per solved task for the rest of the story.");
  }

  async stop() {
    const target = this.runId || this.groupId;
    if (!target) return;
    const stoppingGroup = !this.runId && !!this.groupId;
    const result = await this.request("/api/bench/stop", { method: "POST", body: { id: target } });
    if (!result || result.ok === false) {
      this.shell.toast((result && result.error) || "Could not stop the benchmark.", "error");
      return;
    }
    if (stoppingGroup && this.group) {
      this.group = { ...this.group, status: "stopping", stop_requested: true };
      this.renderGroup();
    } else if (this.run) {
      this.run = { ...this.run, stop_requested: true };
      this.renderRun();
    }
    this.shell.toast(stoppingGroup
      ? `Stopping all ${(result.run_ids || []).length} configurations; completed results will be kept.`
      : "Winding the run down; the agents are being stopped.");
  }

  async parallelContinue() {
    if (!this.groupId || !this.group) return;
    const children = Math.max(1, Number((this.group.runs || []).length || 1));
    const unfinished = Math.max(1, Number(this.group.tasks || 0) - Number(this.group.passed || 0));
    const limit = Number((((this.meta || {}).limits || {}).concurrency) || 8);
    const concurrency = Math.max(1, Math.min(limit, Math.ceil(unfinished / children)));
    const button = this.main.querySelector('[data-bench="parallel-continue"]');
    if (button) button.disabled = true;
    const result = await this.request("/api/bench/parallel-continue", {
      method: "POST",
      body: { id: this.groupId, concurrency },
    });
    if (!result || result.ok === false || !result.group) {
      if (button) button.disabled = false;
      this.shell.toast((result && result.error) || "Could not continue the comparison.", "error");
      return;
    }
    this.runs = [result.group, ...this.runs];
    this.shell.toast(
      `${number(result.seeded_results || 0)} completed results merged; ${number(result.remaining_tasks || 0)} unfinished attempts are live at ${number(result.concurrency || concurrency)} per harness.`,
      "success",
    );
    await this.openGroup(result.group.id);
  }

  async remove() {
    if (!this.runId) return;
    const confirmed = await this.shell.confirm("Delete this run?", "Its sessions, workspaces and results are removed from disk.");
    if (!confirmed) return;
    const result = await this.request("/api/bench/delete", { method: "POST", body: { id: this.runId } });
    if (!result || result.ok === false) {
      this.shell.toast((result && result.error) || "Could not delete the run.", "error");
      return;
    }
    this.runs = this.runs.filter((run) => String(run.id) !== String(this.runId));
    this.runId = null;
    this.run = null;
    this.renderRuns();
    if (this.runs.length) this.openRun(this.runs[0].id);
    else this.showSetup();
  }

  // ------------------------------------------------------------- open task

  openTask(taskId) {
    this.stopEvents();
    this.mode = "task";
    this.taskId = String(taskId);
    this.main.innerHTML = `
      <div class="bench-pane bench-task-pane">
        <div class="bench-pane-head">
          <div style="min-width:0">
            <div class="title" data-bench="task-title">Task</div>
            <div class="meta" data-bench="task-meta"></div>
          </div>
          <div class="actions">
            <button class="btn compact ghost" data-bench="open-workspace">Open repo</button>
            <button class="btn compact" data-bench="to-run">&#8592; Run</button>
          </div>
        </div>
        <div class="bench-stats" data-bench="task-stats"></div>
        <div data-bench="role-stats"></div>
        <div data-bench="task-efficiency"></div>
        <div class="bench-checks" data-bench="task-checks"></div>
        <div class="code-transcript-wrap">
          <div class="code-transcript" data-bench="transcript"></div>
          <button class="scroll-bottom" data-bench="jump">Jump to latest &darr;</button>
        </div>
      </div>
    `;

    this.view = new Transcript(this.el("transcript"), {
      jump: this.el("jump"),
      isActive: () => JOB_ACTIVE.has(String((this.jobMeta || {}).status || "")),
      onReveal: (raw) => {
        const root = String((this.jobMeta || {}).cwd || "");
        const absolute = /^[a-zA-Z]:[\\/]|^\\\\/.test(raw) ? raw : `${root}\\${raw}`.replace(/\//g, "\\");
        native("open_path", absolute);
      },
      onReviewSuggest: (prompt) => this.runReviewFix(null, prompt),
      onReviewFix: (review) => this.runReviewFix(review),
    });
    this.view.pinToEnd();

    this.since = 0;
    this.jobMeta = null;
    this.renderTaskMeta();
    this.streamTask();
  }

  async runReviewFix(review, suggestion = "") {
    if (this.reviewFixPending || !this.runId || !this.taskId) return;
    const report = String((review || {}).output || (review || {}).summary || "").trim();
    const instruction = String(suggestion || "").trim() || (
      "The user explicitly clicked FIX after reading the automated review. " +
      "Judge every point below, fix what is genuinely wrong, explain any mistaken finding, " +
      "verify the result, and stop. Do not broaden the task.\n\nREVIEW REPORT\n" + report
    );
    if (!instruction.trim()) return;
    this.reviewFixPending = true;
    try {
      const result = await this.request("/api/bench/continue", {
        method: "POST",
        body: { id: this.runId, task: this.taskId, instruction },
      });
      if (!result || result.ok === false) {
        this.shell.toast((result && result.error) || "Could not start the manual fix.", "error");
        return;
      }
      this.run = result.run || this.run;
      this.shell.toast("Manual fix started in the same harness and workspace.", "success");
      this.renderTaskMeta();
      this.streamTask();
    } finally {
      this.reviewFixPending = false;
    }
  }

  streamTask() {
    // Only the socket. The view was built moments ago by openTask and tearing
    // it down here would leave push() with nothing to write into.
    if (this.eventStream) this.eventStream.close();
    const runId = this.runId;
    const taskId = this.taskId;
    this.eventStream = stream(
      () => `/sse/bench/events?run=${encodeURIComponent(runId)}&task=${encodeURIComponent(taskId)}&since=${this.since}`,
      {
        events: (payload) => {
          if (this.taskId !== taskId) return;
          this.since = payload.size || this.since;
          if (payload.job) this.jobMeta = payload.job;
          if (payload.task && this.run) {
            const tasks = (this.run.tasks || []).map((row) =>
              String(row.id) === String(payload.task.id) ? { ...row, ...payload.task } : row);
            this.run = { ...this.run, tasks };
            this.renderTaskMeta();
          }
          this.view.push(payload.events);
        },
        reset: () => {
          if (this.taskId !== taskId) return;
          this.since = 0;
          this.view.reset();
        },
        task: (payload) => {
          if (this.taskId !== taskId) return;
          this.jobMeta = payload.job || this.jobMeta;
          if (payload.task && this.run) {
            const tasks = (this.run.tasks || []).map((row) =>
              String(row.id) === String(payload.task.id) ? { ...row, ...payload.task } : row);
            this.run = { ...this.run, tasks };
          }
          this.renderTaskMeta();
        },
      },
    );
  }

  stopEvents() {
    if (this.eventStream) this.eventStream.close();
    this.eventStream = null;
    if (this.view) { this.view.destroy(); this.view = null; }
  }

  currentTask() {
    return ((this.run || {}).tasks || []).find((task) => String(task.id) === String(this.taskId)) || null;
  }

  renderTaskMeta() {
    const task = this.currentTask();
    const title = this.el("task-title");
    if (!task || !title) return;

    const usage = task.usage || {};
    const status = String(task.status || "");
    title.innerHTML = `${escapeHtml(task.title || task.id)}
      <span class="status-pill ${escapeHtml(status)}">${escapeHtml(status)}</span>`;
    const origin = task.benchmark_origin && task.benchmark_origin !== "aiOS"
      ? ` · ${escapeHtml(task.benchmark_origin)}${task.leaderboard_comparable === false ? " subset/adaptation" : ""}`
      : "";
    const source = task.source_url
      ? ` · <a href="${escapeHtml(task.source_url)}" target="_blank" rel="noreferrer">source</a>`
      : "";
    const primaryModel = String(task.native_primary_model || task.model || "");
    const auxiliaryModels = Array.isArray(task.native_models_used)
      ? task.native_models_used.filter((value) => String(value) && String(value) !== primaryModel)
      : [];
    const modelAttribution = primaryModel
      ? ` · model: ${escapeHtml(primaryModel)}${auxiliaryModels.length ? ` · auxiliary: ${escapeHtml(auxiliaryModels.join(", "))}` : ""}`
      : "";
    this.el("task-meta").innerHTML =
      `${escapeHtml(task.suite)} · ${escapeHtml(task.id)}${origin}${source}${modelAttribution}${task.review ? ` · reviewer: ${escapeHtml(task.review)}` : ""}`;

    this.el("task-stats").innerHTML = `
      <span><b>${number(usage.total_tokens)}</b>tokens</span>
      <span><b>${number(usage.input_tokens)}</b>in <i>(${number(usage.cached_input_tokens)} cached)</i></span>
      <span><b>${number(usage.output_tokens)}</b>out <i>(${number(usage.reasoning_tokens)} reasoning)</i></span>
      <span><b>${task.cost_provenance === "unavailable" ? "n/a" : money(usage.cost_usd)}</b>${task.cost_provenance === "api_equivalent" ? "API-eq. cost" : "cost"}</span>
      <span><b>${task.seconds ? formatDuration(task.seconds) : "\u2014"}</b>elapsed</span>
      <span><b>${number(task.tool_calls)}</b>tool calls</span>
      <span><b>${number(task.files_edited)}</b>files <i>+${number(task.lines_added)} / -${number(task.lines_deleted)}</i></span>
    `;
    this.el("role-stats").innerHTML = this.roleMetrics(task);
    this.el("task-efficiency").innerHTML = this.efficiencyTrace(task);

    const checks = task.checks || [];
    const isCustom = String(task.suite) === "custom";
    this.el("task-checks").innerHTML = isCustom
      ? '<div class="head">CUSTOM &mdash; no hidden checks. Open the repo and judge the result yourself.</div>'
      : (checks.length
        ? `<div class="head">CHECKS &mdash; the agent never sees these</div>` + checks.map((check) => `
            <div class="check-row ${check.passed ? "passed" : "failed"}">
              <span class="mark">${check.passed ? "\u2713" : "\u2717"}</span>
              <span class="name">${escapeHtml(check.name)}</span>
              <span class="detail">${escapeHtml(check.detail || "")}</span>
            </div>
          `).join("")
        : (TASK_ACTIVE.has(status)
            ? '<div class="head">CHECKS run when the agent stops.</div>'
            : (task.error ? `<div class="bench-error">${escapeHtml(task.error)}</div>` : "")));
  }

  openWorkspace() {
    const task = this.currentTask();
    if (!task || !task.workspace) {
      this.shell.toast("That task has no workspace on disk yet.");
      return;
    }
    native("open_path", task.workspace);
  }

  // --------------------------------------------------------------- compare

  showCompare() {
    this.mode = "compare";
    this.stopEvents();
    this.main.innerHTML = `<div class="bench-pane" data-bench="compare-pane"></div>`;
    this.renderCompare();
  }

  renderCompare() {
    const pane = this.el("compare-pane");
    if (!pane) return;
    const runs = this.runs.filter((run) => run.score !== null && run.score !== undefined);
    if (!runs.length) {
      pane.innerHTML = '<div class="placeholder"><strong>Nothing to compare yet</strong>Finish a run and it appears here next to the others.</div>';
      return;
    }
    const best = Math.max(...runs.map((run) => Number(run.score || 0)), 1);
    const taskHashes = new Set(runs.map((run) => String(run.task_set_hash || "")).filter(Boolean));
    const matched = taskHashes.size <= 1 && !runs.some((run) => !run.task_set_hash);
    pane.innerHTML = `
      <div class="bench-pane-head">
        <div style="min-width:0">
          <div class="title">Compare</div>
          <div class="meta">${matched ? "Matched task-set fingerprints. What changed is the harness/model configuration." : "Mixed task sets are shown here. Compare only rows with the same task-set fingerprint; campaigns enforce this automatically."}</div>
        </div>
      </div>
      <div class="bench-compare">
        <div class="row head">
          <span>Run</span><span>Model</span><span class="num">Score</span><span class="num">Passed</span>
          <span class="num">Tokens / pass</span><span class="num">Cost</span><span class="num">Sec / pass</span>
        </div>
        ${runs.map((run) => `
          <div class="row" data-run="${escapeHtml(run.id)}">
            <span class="label">
              <i class="bar" style="width:${Math.round((Number(run.score || 0) / best) * 100)}%"></i>
              <b>${escapeHtml(run.label || run.id)}</b>
              <em>${relativeTime(run.created_at)} · task set ${escapeHtml(String(run.task_set_hash || "unknown").slice(0, 8))}</em>
            </span>
            <span class="model">${escapeHtml(String(run.model || ""))}${run.fast ? " \u00b7 fast" : ""}<em>${escapeHtml(String(run.reasoning || ""))}</em></span>
            <span class="num score ${this.scoreClass(run.score, run.status)}">${Math.round(run.score)}</span>
            <span class="num">${run.passed || 0}/${run.tasks || 0}</span>
            <span class="num">${run.tokens_per_pass ? number(run.tokens_per_pass) : "\u2014"}</span>
            <span class="num">${money(run.cost_usd)}</span>
            <span class="num">${run.seconds_per_pass ? `${run.seconds_per_pass}s` : "\u2014"}</span>
          </div>
        `).join("")}
      </div>
    `;
  }
}
