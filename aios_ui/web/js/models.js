// The Models window: which model does which job in a CODE session.
//
// It replaced four dropdowns in the composer that could only describe one
// model, because a session is not one model's work. The Coder leads, Scouts
// explore, and the Consultant thinks through difficult decisions when asked.
// A dropdown cannot show you that a model costs
// twenty times more per token than the one above it, which is the fact you
// actually need when the coder re-sends its context on every round.
//
// Every number rendered here is measured or reported: prices come from
// OpenRouter, throughput from this machine's own finished sessions. A model
// with no recorded session shows no speed rather than a guess.

import { api } from "./bridge.js";
import { escapeHtml } from "./markdown.js";

const PROVIDERS = [
  ["codex", "ChatGPT Codex"],
  ["claude", "Claude"],
  ["cursor", "Cursor"],
  ["ollama", "Ollama local"],
  ["openrouter", "OpenRouter"],
];

const REASONING_LABELS = {
  off: "Off",
  on: "On",
  low: "Low",
  medium: "Medium",
  high: "High",
  xhigh: "Max",
};

const STRATEGY_LABELS = {
  auto: "AUTO · CODER-LED",
};

/** Keep legacy saved role configs readable while presenting the new role name. */
function normaliseRoles(value) {
  const roles = JSON.parse(JSON.stringify(value || {}));
  if (!roles.consultant && roles.planner) roles.consultant = roles.planner;
  delete roles.planner;
  return roles;
}

function normaliseCatalogue(value) {
  const catalogue = value || [];
  const hasConsultant = catalogue.some((row) => String(row.role || "").toLowerCase() === "consultant");
  const seen = new Set();
  return catalogue.filter((row) => !(hasConsultant && String(row.role || "").toLowerCase() === "planner")).map((row) => {
    if (String(row.role || "").toLowerCase() !== "planner") return row;
    return {
      ...row,
      role: "consultant",
      label: "Consultant",
      tagline: String(row.tagline || "").replace(/planner/gi, "consultant"),
      detail: String(row.detail || "").replace(/planner/gi, "consultant"),
    };
  }).filter((row) => {
    const role = String(row.role || "");
    if (seen.has(role)) return false;
    seen.add(role);
    return true;
  });
}

function normaliseConfigs(value) {
  return (value || []).map((config) => ({
    ...config,
    strategy: "auto",
    roles: normaliseRoles(config.roles),
  }));
}

/** "$0.09" / "$2" / "—" — never "$0.00" for a price we simply do not have. */
function money(value) {
  if (value === null || value === undefined) return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  if (number === 0) return "free";
  if (number < 0.01) return `$${number.toFixed(4).replace(/0+$/, "")}`;
  if (number < 1) return `$${number.toFixed(2)}`;
  return `$${number.toFixed(number < 10 ? 2 : 0)}`;
}

function contextLabel(tokens) {
  const count = Number(tokens || 0);
  if (!count) return "—";
  if (count >= 1000000) return `${Math.round(count / 100000) / 10}M`;
  return `${Math.round(count / 1000)}K`;
}

/** Dollars for one round trip of a typical coding turn, so two rows compare. */
function blendedCost(model) {
  if (model.price_in === null || model.price_out === null) return null;
  return (Number(model.price_in) * 0.9) + (Number(model.price_out) * 0.1);
}

export class ModelsWindow {
  constructor(shell) {
    this.shell = shell;
    this.node = null;
    this.roles = {};
    this.catalogue = [];
    this.models = [];
    this.speed = {};
    this.savedConfigs = [];
    this.benchRuns = [];
    this.active = "coder";
    this.filter = "";
    this.naming = false;
    this.editingConfigId = null;
    this.configName = "";
    this.configDescription = "";
    this.configStrategy = "auto";
    this.configShowInComposer = true;
    this.rolesBeforeEdit = null;
    this.onSaved = null;
    this.onConfigsChanged = null;
    this.onApplied = null;
    this.provider = "openrouter";
    this.capabilities = [];
    this.openrouterModels = [];
    this.reviewFix = false;
    this.appliedConfig = null;
    this.loadId = 0;
    // Which saved configs are showing their details. Kept on the instance so a
    // background refresh does not collapse a row you just opened.
    this.expandedConfigs = new Set();
  }

  mapCapabilityModels(provider) {
    const info = (this.capabilities || []).find((row) => row.provider === provider) || {};
    const efforts = (info.reasoning || info.efforts || ["off", "low", "medium", "high"]).map(String);
    return (info.models || []).map((model) => ({
      id: String(model.id || ""),
      label: String(model.label || model.id || ""),
      price_in: null,
      price_out: null,
      context_length: 0,
      reasoning: (model.reasoning || efforts).map(String),
      fast: !!model.fast,
      enabled: true,
      default_reasoning: String(model.default_reasoning || efforts[0] || "medium"),
    })).filter((row) => row.id);
  }

  async syncProviderModels() {
    if (this.provider === "openrouter") {
      this.models = this.openrouterModels;
      return;
    }
    this.models = this.mapCapabilityModels(this.provider);
  }

  async open(role = "coder", {
    provider = "openrouter", reviewFix = false, roles = null, activeConfigId = "", strategy = "auto",
  } = {}) {
    const loadId = ++this.loadId;
    this.active = role;
    this.provider = provider;
    this.reviewFix = !!reviewFix;
    this.configStrategy = "auto";
    const hasSuppliedRoles = !!roles && Object.keys(roles).length > 0;
    this.roles = hasSuppliedRoles ? normaliseRoles(roles) : {};
    this.appliedConfig = null;
    // Paint immediately, then hydrate from local/cached endpoints only. Live
    // OpenRouter metrics, CLI model discovery, and bench history are slower
    // and must never hold the configuration editor hostage.
    this.render();
    const [rolesResult, modelsResult, configsResult] = await Promise.all([
      api("/api/code/roles"),
      api("/api/code/models"),
      api("/api/code/model-configs"),
    ]);
    if (loadId !== this.loadId) return;
    if (!rolesResult || rolesResult.ok === false) {
      this.shell.toast("Could not load the model roles.", "error");
      return;
    }
    if (!hasSuppliedRoles) this.roles = normaliseRoles(rolesResult.roles);
    this.catalogue = normaliseCatalogue(rolesResult.catalogue);
    this.openrouterModels = (modelsResult && modelsResult.models) || [];
    this.speed = (modelsResult && modelsResult.speed) || {};
    this.savedConfigs = normaliseConfigs(configsResult && configsResult.configs);
    this.appliedConfig = this.savedConfigs.find((row) => String(row.id) === String(activeConfigId)) || null;
    this.configStrategy = "auto";
    await this.syncProviderModels();
    if (this.node) this.render();
    void this.refreshBackgroundData(loadId);
  }

  async refreshBackgroundData(loadId) {
    const current = () => loadId === this.loadId;
    await Promise.all([
      api("/api/code/models?refresh=1").then(async (result) => {
        if (!current() || !result || result.ok === false) return;
        this.openrouterModels = result.models || this.openrouterModels;
        this.speed = result.speed || this.speed;
        await this.syncProviderModels();
        if (this.node) this.render();
      }),
      api("/api/bench/runs?limit=1000").then((result) => {
        if (!current() || !result || result.ok === false) return;
        this.benchRuns = result.runs || [];
        if (this.node) this.render();
      }),
      api("/api/code/capabilities").then(async (result) => {
        if (!current() || !result || result.ok === false) return;
        this.capabilities = result.providers || [];
        await this.syncProviderModels();
        if (this.node) this.render();
      }),
    ]);
  }

  savedConfigCard(cfg) {
    const roles = normaliseRoles(cfg.roles);
    const adaptiveReview = !!roles.reviewer && roles.reviewer.enabled !== false;
    const modelNames = Object.entries(roles)
      .filter(([, v]) => v && v.model && v.enabled !== false)
      .map(([k, v]) => `${k}: ${v.model}`)
      .join(" | ");
    const providerLabel = PROVIDERS.find(([id]) => id === (cfg.provider || "openrouter"))?.[1]
      || String(cfg.provider || "openrouter");
    const runs = this.benchRuns.filter((row) => String(row.saved_config_id || "") === String(cfg.id));
    const history = runs.length
      ? runs.map((run) => `<button class="saved-config-run" data-config-run="${escapeHtml(run.id)}">
          <span>${escapeHtml(run.label || run.id)}</span>
          <i>${escapeHtml(String(run.status || ""))}${run.score == null ? "" : ` · ${escapeHtml(String(Math.round(run.score)))}`}</i>
        </button>`).join("")
      : '<div class="saved-config-no-runs">No benchmark runs yet.</div>';
    // One line per config. With dozens saved, a card carrying the description,
    // every role's model and the full run history meant two fit on screen and
    // the list was unreadable; the detail moves behind a disclosure and the
    // row keeps only what you scan for: is it on, what is it, which model.
    const active = Object.entries(roles).filter(([, v]) => v && v.model && v.enabled !== false);
    const lead = (roles.coder && roles.coder.model) || (active[0] && active[0][1].model) || "—";
    const others = Math.max(0, active.length - 1);
    const open = this.expandedConfigs.has(String(cfg.id));
    const applied = this.appliedConfig && String(this.appliedConfig.id) === String(cfg.id);
    return `
      <div class="saved-config-card${open ? " open" : ""}${applied ? " applied" : ""}" data-config-id="${escapeHtml(cfg.id)}">
        <div class="saved-config-row">
          <button type="button" class="saved-config-composer-toggle${cfg.show_in_composer === false ? "" : " on"}"
                  data-config-action="visibility" role="switch" aria-checked="${cfg.show_in_composer === false ? "false" : "true"}"
                  title="Show this configuration above the chat input">
            <span></span>
          </button>
          <button type="button" class="saved-config-disclose" data-config-action="expand"
                  aria-expanded="${open ? "true" : "false"}" title="${open ? "Hide details" : "Show details"}">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>
          </button>
          <span class="saved-config-name" title="${escapeHtml(cfg.name || "Untitled")}">${escapeHtml(cfg.name || "Untitled")}</span>
          <span class="saved-config-lead" title="${escapeHtml(modelNames)}">${escapeHtml(lead)}${others ? `<i>+${others}</i>` : ""}</span>
          ${runs.length ? `<span class="saved-config-runcount">${runs.length}</span>` : ""}
          <span class="saved-config-actions">
            <button class="btn mini ghost" data-config-action="apply" title="Apply this config to all roles">Apply</button>
            <button class="btn mini ghost" data-config-action="edit" title="Edit this saved configuration">Edit</button>
            <button class="btn mini ghost" data-config-action="duplicate" title="Duplicate">Copy</button>
            <button class="btn mini ghost" data-config-action="bench" title="Open bench with this config">Bench</button>
            <button class="btn mini ghost danger" data-config-action="delete" title="Delete this config">Delete</button>
          </span>
        </div>
        ${open ? `<div class="saved-config-detail">
          ${cfg.description ? `<div class="saved-config-desc">${escapeHtml(cfg.description)}</div>` : ""}
          <div class="saved-config-models">${escapeHtml(providerLabel)} · ${escapeHtml(modelNames)}</div>
          <div class="saved-config-flags">
            <span class="saved-config-strategy">${STRATEGY_LABELS.auto}</span>
            <span class="saved-config-strategy" title="The Coder decides whether another model is useful.">${adaptiveReview ? "review available" : "review off"}</span>
          </div>
          <div class="saved-config-history"><b>${runs.length} run${runs.length === 1 ? "" : "s"}</b>${history}</div>
        </div>` : ""}
      </div>
    `;
  }

  close() {
    if (this.node) this.node.remove();
    this.node = null;
    document.removeEventListener("keydown", this.escape);
  }

  render() {
    if (!this.node) {
      this.node = document.createElement("div");
      this.node.className = "modal-backdrop models-backdrop";
      document.body.append(this.node);
      // The backdrop starts transparent so it can fade; without this it is
      // appended, styled opacity:0, and never shown.
      requestAnimationFrame(() => this.node && this.node.classList.add("show"));
      this.escape = (event) => { if (event.key === "Escape") this.close(); };
      document.addEventListener("keydown", this.escape);
      this.node.addEventListener("click", (event) => {
        if (event.target === this.node) this.close();
      });
    }

    // Rebuilding the DOM wipes every nested scrollbar back to the top, so toggling a
    // config's composer visibility (or a background refresh mid-scroll) would yank the
    // view. Capture positions first and restore them after the re-render.
    // Keyed off a stable data attribute rather than className: className has no
    // leading dot, so querySelector(className) looked for an element *type* of
    // that name, found nothing, and silently restored nothing.
    const scrollState = Array.from(this.node.querySelectorAll("[data-scroll]"))
      .map((el) => ({ key: el.dataset.scroll, top: el.scrollTop, left: el.scrollLeft }));

    this.renderContent();

    scrollState.forEach((state) => {
      const next = this.node.querySelector(`[data-scroll="${state.key}"]`);
      if (next) {
        next.scrollTop = state.top;
        next.scrollLeft = state.left;
      }
    });
  }

  renderContent() {
    this.node.innerHTML = `
      <div class="modal models-modal">
        <div class="models-head">
          <div>
            <div class="modal-title">Models</div>
            <div class="models-sub">Who does what in a session, and on which model.</div>
            <div class="models-provider">
              <label class="pick-field">
                <span>Provider</span>
                <select class="slim" data-models="provider">
                  ${PROVIDERS.map(([id, label]) => `<option value="${escapeHtml(id)}"${id === this.provider ? " selected" : ""}>${escapeHtml(label)}</option>`).join("")}
                </select>
              </label>
              <button class="btn compact ghost" data-models="setup-provider" title="Run provider setup (login, API key, etc.)">Setup</button>
            </div>
          </div>
          <div>
            <button class="btn compact ghost" data-models="save-config" title="Save this role/model setup as a named preset">Save config</button>
            <button class="btn compact ghost" data-models="close">Done</button>
          </div>
        </div>
        ${this.naming ? `<div class="models-save-form">
          <input class="models-save-name" type="text" maxlength="80" data-models="config-name" placeholder="Configuration name" value="${escapeHtml(this.configName)}" autofocus>
          <input class="models-save-description" type="text" maxlength="500" data-models="config-description" placeholder="What is this setup for? (optional)" value="${escapeHtml(this.configDescription)}">
          <span class="saved-config-strategy" title="The Coder leads and delegates only when useful.">${STRATEGY_LABELS.auto}</span>
          <label class="models-save-visibility" title="Show this configuration as a pill above the chat input">
            <input type="checkbox" data-models="config-visible"${this.configShowInComposer ? " checked" : ""}> Show in composer
          </label>
          <button class="btn compact accent" data-models="confirm-save">${this.editingConfigId ? "Update" : "Save"}</button>
          <button class="btn compact ghost" data-models="cancel-save">Cancel</button>
        </div>` : ""}
        <div class="models-body">
          <div class="models-roles" data-scroll="roles">${this.catalogue.map((row) => this.roleCard(row)).join("")}</div>
          <div class="models-vsplit" data-split="v" role="separator" aria-orientation="vertical" title="Drag to resize sidebar"></div>
          <div class="models-pick" data-scroll="pick">${this.picker()}</div>
        </div>
        <div class="models-ssplit" data-split="h" role="separator" aria-orientation="horizontal" title="Drag to resize configurations"></div>
        <div class="models-saved" data-scroll="saved">
          <div class="saved-title">Saved Model Configurations</div>
          ${this.savedConfigs.length === 0 ? '<div class="saved-empty">No saved configs yet. Configure your models and click "Save config" above.</div>' : ""}
          ${this.savedConfigs.map((cfg) => this.savedConfigCard(cfg)).join("")}
        </div>
      </div>
    `;
    this.bind();
  }

  roleCard(meta) {
    const role = this.roles[meta.role] || {};
    const model = this.models.find((row) => row.id === role.model);
    const on = role.enabled !== false;
    const selected = meta.role === this.active;
    const price = model ? blendedCost(model) : null;
    return `
      <button class="role-card${selected ? " selected" : ""}${on ? "" : " off"}" data-role="${escapeHtml(meta.role)}">
        <div class="role-top">
          <span class="role-name">${escapeHtml(meta.label)}</span>
          ${meta.optional
            ? `<span class="role-toggle${on ? " on" : ""}" data-toggle="${escapeHtml(meta.role)}" role="switch"
                     aria-checked="${on}" title="${on ? "Switch this stage off" : "Switch this stage on"}"></span>`
            : `<span class="role-always" title="Not optional: the session is this stage.">always</span>`}
        </div>
        <div class="role-tagline">${escapeHtml(meta.tagline)}</div>
        <div class="role-model">${escapeHtml(model ? model.label : (role.model || "—"))}</div>
        <div class="role-meta">
          <span title="How hard the model thinks before it answers.">think ${escapeHtml(REASONING_LABELS[role.reasoning] || role.reasoning || "off")}</span>
          ${role.fast ? "<span class=\"pill fast\">fast</span>" : ""}
          ${price === null ? "" : `<span class="role-price">${escapeHtml(money(price))}/M</span>`}
        </div>
      </button>
    `;
  }

  picker() {
    const meta = this.catalogue.find((row) => row.role === this.active) || {};
    const role = this.roles[this.active] || {};
    const model = this.models.find((row) => row.id === role.model);
    const efforts = (model && model.reasoning) || ["off", "low", "medium", "high"];
    const disabled = role.enabled === false;
    const term = this.filter.trim().toLowerCase();
    const rows = this.models.filter((row) => !term
      || row.id.toLowerCase().includes(term)
      || String(row.label || "").toLowerCase().includes(term));
    return `
      <div class="pick-head">
        <div>
          <div class="pick-title">${escapeHtml(meta.label || this.active)}</div>
          <div class="pick-detail">${escapeHtml(meta.detail || "")}</div>
        </div>
      </div>
      ${disabled ? `<div class="pick-off">This stage is switched off. Turn it on to choose its model.</div>` : `
      <div class="pick-controls">
        <label class="pick-field">
          <span>Intelligence</span>
          <select class="slim" data-models="reasoning">
            ${efforts.map((value) => `<option value="${escapeHtml(value)}"${value === role.reasoning ? " selected" : ""}>${escapeHtml(REASONING_LABELS[value] || value)}</option>`).join("")}
          </select>
        </label>
        ${model && model.fast ? `
        <label class="pick-field check" title="Fast mode routes OpenRouter models to the highest-throughput provider and disables extra reasoning.">
          <input type="checkbox" data-models="fast"${role.fast ? " checked" : ""}> Fast mode
        </label>` : `<span class="pick-note">Fast mode is not available on this model.</span>`}
        <input type="search" class="slim pick-search" data-models="search" placeholder="Filter ${this.models.length} models…" value="${escapeHtml(this.filter)}">
      </div>
      <div class="model-table-head">
        <span>Model</span><span>In /M</span><span>Out /M</span><span>Context</span><span>Speed</span>
      </div>
      <div class="model-list" data-models="list">
        ${rows.map((row) => this.modelRow(row, role)).join("") || `<div class="pick-off">No model matches that.</div>`}
      </div>`}
    `;
  }

  modelRow(model, role) {
    const localSpeed = this.speed[model.id];
    const networkSpeed = Number(model.openrouter_average_tps || 0);
    const speedLabel = localSpeed
      ? `${localSpeed.tokens_per_second} t/s`
      : (networkSpeed > 0 ? `${networkSpeed} t/s` : "—");
    const speedTitle = localSpeed
      ? `Local average from ${localSpeed.sessions} session${localSpeed.sessions === 1 ? "" : "s"}`
      : (networkSpeed > 0 ? "OpenRouter 30-minute average of provider p50 throughput" : "No throughput sample yet");
    const chosen = model.id === role.model;
    const suggested = (this.active === "scout" && model.scout)
      || (this.active === "consultant" && (model.consultant || model.planner));
    const popularity = Number(model.popularity_rank || 0);
    const goodFor = (model.good_for || []).slice(0, 3);
    return `
      <button class="model-row${chosen ? " chosen" : ""}" data-model="${escapeHtml(model.id)}">
        <span class="model-name">
          <b>${escapeHtml(model.label)}</b>
          ${suggested ? `<span class="pill good">good for ${escapeHtml(this.active)}</span>` : ""}
          ${popularity > 0 && popularity <= 50 ? `<span class="pill popular" title="OpenRouter rank by tokens processed in the last week">#${popularity} weekly</span>` : ""}
          ${goodFor.map((tag) => `<span class="pill strength">${escapeHtml(tag)}</span>`).join("")}
          ${model.enabled ? "" : `<span class="pill muted">not in Settings</span>`}
          <i>${escapeHtml(model.id)}</i>
        </span>
        <span class="num">${escapeHtml(money(model.price_in))}</span>
        <span class="num">${escapeHtml(money(model.price_out))}</span>
        <span class="num">${escapeHtml(contextLabel(model.context_length))}</span>
        <span class="num" title="${escapeHtml(speedTitle)}">${escapeHtml(speedLabel)}</span>
      </button>
    `;
  }

  bind() {
    const q = (name) => this.node.querySelector(`[data-models="${name}"]`);
    q("close").addEventListener("click", () => {
      if (this.onApplied) {
        this.onApplied({
          roles: this.roles,
          provider: this.provider,
          reviewFix: this.reviewFix,
          strategy: "auto",
          config: this.appliedConfig,
        });
      }
      this.close();
    });
    const saveBtn = q("save-config");
    if (saveBtn) {
      saveBtn.addEventListener("click", () => {
        this.editingConfigId = null;
        this.rolesBeforeEdit = null;
        this.configName = "";
        this.configDescription = "";
        this.configStrategy = "auto";
        this.configShowInComposer = true;
        this.naming = true;
        this.render();
        const input = q("config-name");
        if (input) input.focus();
      });
    }
    const providerSelect = q("provider");
    if (providerSelect) {
      providerSelect.addEventListener("change", async () => {
        this.provider = providerSelect.value;
        this.appliedConfig = null;
        await this.syncProviderModels();
        this.render();
      });
    }
    const setupProvider = q("setup-provider");
    if (setupProvider) {
      setupProvider.addEventListener("click", async () => {
        const result = await api(`/api/code/providers/${encodeURIComponent(this.provider)}/setup`, { method: "POST" });
        this.shell.toast(
          (result && (result.message || result.error)) || `${this.provider} is ready.`,
          result && result.ok === false ? "error" : "info",
        );
      });
    }
    const reviewFix = q("review-fix");
    if (reviewFix) {
      reviewFix.addEventListener("change", () => { this.reviewFix = reviewFix.checked; });
    }
    const cancelSave = q("cancel-save");
    if (cancelSave) cancelSave.addEventListener("click", () => {
      if (this.rolesBeforeEdit) this.roles = this.rolesBeforeEdit;
      this.rolesBeforeEdit = null;
      this.editingConfigId = null;
      this.naming = false;
      this.render();
    });
    const nameField = q("config-name");
    if (nameField) nameField.addEventListener("input", () => { this.configName = nameField.value; });
    const descriptionField = q("config-description");
    if (descriptionField) descriptionField.addEventListener("input", () => { this.configDescription = descriptionField.value; });
    const visibleField = q("config-visible");
    if (visibleField) visibleField.addEventListener("change", () => { this.configShowInComposer = visibleField.checked; });
    const confirmSave = q("confirm-save");
    if (confirmSave) confirmSave.addEventListener("click", () => {
      const name = (q("config-name").value || "").trim();
      if (!name) { q("config-name").focus(); return; }
      this.saveCurrentConfig(name, (q("config-description").value || "").trim());
    });

    this.node.querySelectorAll("[data-toggle]").forEach((node) => {
      node.addEventListener("click", (event) => {
        event.stopPropagation();
        const name = node.dataset.toggle;
        this.patch(name, { enabled: !(this.roles[name] || {}).enabled });
      });
    });
    this.node.querySelectorAll("[data-role]").forEach((node) => {
      node.addEventListener("click", () => {
        this.active = node.dataset.role;
        this.render();
      });
    });

    const reasoning = q("reasoning");
    if (reasoning) reasoning.addEventListener("change", () => this.patch(this.active, { reasoning: reasoning.value }));
    const fast = q("fast");
    if (fast) fast.addEventListener("change", () => this.patch(this.active, { fast: fast.checked }));

    const search = q("search");
    if (search) {
      search.addEventListener("input", () => {
        this.filter = search.value;
        const list = q("list");
        const role = this.roles[this.active] || {};
        const term = this.filter.trim().toLowerCase();
        const rows = this.models.filter((row) => !term
          || row.id.toLowerCase().includes(term)
          || String(row.label || "").toLowerCase().includes(term));
        list.innerHTML = rows.map((row) => this.modelRow(row, role)).join("")
          || `<div class="pick-off">No model matches that.</div>`;
        this.bindRows();
      });
    }
    this.bindRows();
    this.bindSavedConfigs();
    this.bindSplitters();
  }

  bindSplitters() {
    const modal = this.node.querySelector(".models-modal");
    if (!modal) return;
    this.node.querySelectorAll("[data-split]").forEach((handle) => {
      handle.addEventListener("pointerdown", (event) => {
        event.preventDefault();
        const dir = handle.dataset.split;
        const pointerId = event.pointerId;
        handle.setPointerCapture(pointerId);
        handle.classList.add("active");
        const onMove = (move) => {
          const rect = modal.getBoundingClientRect();
          if (dir === "v") {
            const width = Math.max(130, Math.min(420, move.clientX - rect.left));
            modal.style.setProperty("--models-roles-w", `${width}px`);
          } else {
            // Distance from the handle (cursor) down to the modal bottom == the
            // height of the saved-configurations panel (which sits below the handle).
            const height = Math.max(70, Math.min(rect.height - 120, rect.bottom - move.clientY));
            modal.style.setProperty("--models-saved-h", `${height}px`);
          }
        };
        const stop = () => {
          handle.classList.remove("active");
          handle.removeEventListener("pointermove", onMove);
          handle.removeEventListener("pointerup", stop);
          handle.removeEventListener("pointercancel", stop);
        };
        handle.addEventListener("pointermove", onMove);
        handle.addEventListener("pointerup", stop);
        handle.addEventListener("pointercancel", stop);
      });
    });
  }

  bindSavedConfigs() {
    this.node.querySelectorAll("[data-config-action]").forEach((btn) => {
      btn.addEventListener("click", (event) => {
        event.stopPropagation();
        const card = btn.closest("[data-config-id]");
        if (!card) return;
        const configId = card.dataset.configId;
        const action = btn.dataset.configAction;
        if (action === "expand") this.toggleConfigDetail(configId, card);
        else if (action === "apply") this.applySavedConfig(configId);
        else if (action === "edit") this.editSavedConfig(configId);
        else if (action === "duplicate") this.duplicateSavedConfig(configId);
        else if (action === "visibility") this.toggleConfigVisibility(configId);
        else if (action === "bench") this.benchmarkConfig(configId);
        else if (action === "delete") this.deleteSavedConfig(configId);
      });
    });
    this.node.querySelectorAll("[data-config-run]").forEach((btn) => {
      btn.addEventListener("click", (event) => {
        event.stopPropagation();
        this.close();
        this.shell.pendingBenchRunId = btn.dataset.configRun;
        this.shell.show("BENCH");
      });
    });
  }

  async saveCurrentConfig(name, description) {
    const snapshot = JSON.parse(JSON.stringify(this.roles));
    const result = await api("/api/code/model-configs", {
      method: "POST",
      body: {
        id: this.editingConfigId || undefined,
        name,
        description,
        provider: this.provider,
        strategy: "auto",
        review_fix: false,
        show_in_composer: this.configShowInComposer,
        roles: snapshot,
      },
    });
    if (result && result.ok) {
      this.shell.toast(`Saved config "${name}".`, "success");
      this.savedConfigs = normaliseConfigs(result.configs);
      if (this.onConfigsChanged) this.onConfigsChanged(this.savedConfigs);
      if (this.rolesBeforeEdit) this.roles = this.rolesBeforeEdit;
      this.rolesBeforeEdit = null;
      this.editingConfigId = null;
      this.naming = false;
      this.render();
    } else {
      this.shell.toast("Could not save config.", "error");
    }
  }

  async applySavedConfig(configId) {
    const cfg = this.savedConfigs.find((c) => c.id === configId);
    if (!cfg || !cfg.roles) return;
    this.roles = normaliseRoles(cfg.roles);
    const nextProvider = String(cfg.provider || this.provider || "openrouter").trim().toLowerCase();
    this.provider = PROVIDERS.some(([id]) => id === nextProvider) ? nextProvider : "openrouter";
    this.reviewFix = !!cfg.review_fix;
    this.configStrategy = "auto";
    this.appliedConfig = cfg;
    await this.syncProviderModels();
    this.shell.toast(`Selected config "${cfg.name}". Click Done to apply it.`, "success");
    this.render();
  }

  async duplicateSavedConfig(configId) {
    const cfg = this.savedConfigs.find((row) => String(row.id) === String(configId));
    if (!cfg) return;
    const result = await api("/api/code/model-configs", {
      method: "POST",
      body: {
        name: `${cfg.name || "Untitled"} copy`,
        description: cfg.description || "",
        provider: cfg.provider || "openrouter",
        strategy: "auto",
        review_fix: !!cfg.review_fix,
        show_in_composer: cfg.show_in_composer !== false,
        roles: normaliseRoles(cfg.roles),
      },
    });
    if (!result || result.ok === false) {
      this.shell.toast("Could not duplicate config.", "error");
      return;
    }
    this.savedConfigs = normaliseConfigs(result.configs);
    if (this.onConfigsChanged) this.onConfigsChanged(this.savedConfigs);
    this.shell.toast(`Duplicated "${cfg.name}".`, "success");
    this.render();
  }

  async toggleConfigVisibility(configId) {
    const cfg = this.savedConfigs.find((row) => String(row.id) === String(configId));
    if (!cfg) return;
    const next = cfg.show_in_composer === false;
    // Flipping one switch is a local change. Re-rendering the whole modal for
    // it rebuilds every row and drops the list back to the top, which is why
    // toggling a config halfway down the list used to lose your place. Paint
    // the switch itself and leave the rest of the DOM alone.
    cfg.show_in_composer = next;
    this.paintComposerToggle(cfg);
    const result = await api("/api/code/model-configs", {
      method: "POST",
      body: {
        id: cfg.id,
        name: cfg.name,
        description: cfg.description || "",
        provider: cfg.provider || "openrouter",
        strategy: "auto",
        review_fix: !!cfg.review_fix,
        show_in_composer: next,
        roles: normaliseRoles(cfg.roles),
      },
    });
    if (!result || result.ok === false) {
      cfg.show_in_composer = !next;
      this.shell.toast("Could not update composer visibility.", "error");
      this.paintComposerToggle(cfg);
      return;
    }
    // Keep the server's copy, but repaint only the switches: the list the user
    // is looking at has not otherwise changed.
    this.savedConfigs = normaliseConfigs(result.configs);
    if (this.onConfigsChanged) this.onConfigsChanged(this.savedConfigs);
    this.savedConfigs.forEach((row) => this.paintComposerToggle(row));
  }

  /** Open or close one config's detail without disturbing the list. */
  toggleConfigDetail(configId, card) {
    const key = String(configId);
    const open = !this.expandedConfigs.has(key);
    if (open) this.expandedConfigs.add(key);
    else this.expandedConfigs.delete(key);
    const cfg = this.savedConfigs.find((row) => String(row.id) === key);
    if (!card || !cfg) return;
    // Replace this row only. Rebuilding the modal would scroll the list home.
    card.outerHTML = this.savedConfigCard(cfg);
    const next = this.node.querySelector(`[data-config-id="${CSS.escape(key)}"]`);
    if (next) this.bindConfigCard(next);
  }

  /** Wire the actions inside a single config row. */
  bindConfigCard(card) {
    card.querySelectorAll("[data-config-action]").forEach((btn) => {
      btn.addEventListener("click", (event) => {
        event.stopPropagation();
        const configId = card.dataset.configId;
        const action = btn.dataset.configAction;
        if (action === "expand") this.toggleConfigDetail(configId, card);
        else if (action === "apply") this.applySavedConfig(configId);
        else if (action === "edit") this.editSavedConfig(configId);
        else if (action === "duplicate") this.duplicateSavedConfig(configId);
        else if (action === "visibility") this.toggleConfigVisibility(configId);
        else if (action === "bench") this.benchmarkConfig(configId);
        else if (action === "delete") this.deleteSavedConfig(configId);
      });
    });
    card.querySelectorAll("[data-config-run]").forEach((btn) => {
      btn.addEventListener("click", (event) => {
        event.stopPropagation();
        this.close();
        this.shell.pendingBenchRunId = btn.dataset.configRun;
        this.shell.show("BENCH");
      });
    });
  }

  /** Paint one config's composer switch in place, without a re-render. */
  paintComposerToggle(cfg) {
    if (!this.node) return;
    const card = this.node.querySelector(`[data-config-id="${CSS.escape(String(cfg.id))}"]`);
    const toggle = card && card.querySelector('[data-config-action="visibility"]');
    if (!toggle) return;
    const on = cfg.show_in_composer !== false;
    toggle.classList.toggle("on", on);
    toggle.setAttribute("aria-checked", String(on));
  }

  editSavedConfig(configId) {
    const cfg = this.savedConfigs.find((row) => String(row.id) === String(configId));
    if (!cfg) return;
    if (!this.editingConfigId) this.rolesBeforeEdit = JSON.parse(JSON.stringify(this.roles));
    this.editingConfigId = cfg.id;
    this.configName = cfg.name || "";
    this.configDescription = cfg.description || "";
    this.configStrategy = "auto";
    this.configShowInComposer = cfg.show_in_composer !== false;
    const nextProvider = String(cfg.provider || this.provider || "openrouter").trim().toLowerCase();
    this.provider = PROVIDERS.some(([id]) => id === nextProvider) ? nextProvider : "openrouter";
    this.reviewFix = false;
    this.roles = normaliseRoles(cfg.roles);
    this.naming = true;
    this.syncProviderModels().then(() => this.render());
  }

  async benchmarkConfig(configId) {
    const cfg = this.savedConfigs.find((c) => c.id === configId);
    if (!cfg) return;
    this.shell.pendingBenchConfig = JSON.parse(JSON.stringify({ ...cfg, review_fix: !!cfg.review_fix }));
    this.close();
    this.shell.show("BENCH");
  }

  async deleteSavedConfig(configId) {
    const cfg = this.savedConfigs.find((c) => c.id === configId);
    if (!cfg) return;
    const confirmed = await this.shell.confirm(
      `Delete config "${cfg.name}"?`,
      "Past benchmark runs stay available; only the reusable configuration is removed.",
    );
    if (!confirmed) return;
    const result = await api("/api/code/model-configs", {
      method: "DELETE",
      body: { id: configId },
    });
    if (result && result.ok) {
      this.shell.toast(`Deleted "${cfg.name}".`, "success");
      this.savedConfigs = normaliseConfigs(result.configs);
      if (this.onConfigsChanged) this.onConfigsChanged(this.savedConfigs);
      this.render();
    } else {
      this.shell.toast("Could not delete config.", "error");
    }
  }

  bindRows() {
    this.node.querySelectorAll("[data-model]").forEach((node) => {
      node.addEventListener("click", () => {
        const id = node.dataset.model;
        const model = this.models.find((row) => row.id === id) || {};
        const patch = { model: id };
        // Switching to a model that cannot do the current setting must not
        // leave the role holding a value that model will reject.
        const efforts = model.reasoning || ["off"];
        const current = (this.roles[this.active] || {}).reasoning;
        if (!efforts.includes(current)) {
          patch.reasoning = efforts.includes(model.default_reasoning) ? model.default_reasoning : efforts[0];
        }
        if (!model.fast) patch.fast = false;
        this.patch(this.active, patch);
      });
    });
  }

  async patch(role, fields) {
    const next = { ...(this.roles[role] || {}), ...fields };
    this.roles = { ...this.roles, [role]: next };
    if (!this.editingConfigId) this.appliedConfig = null;
    this.render();
    if (this.editingConfigId) return;
    const result = await api("/api/code/roles", { method: "POST", body: { roles: { [role]: fields } } });
    if (result && result.ok) {
      this.roles = result.roles;
      this.render();
      if (this.onSaved) this.onSaved(this.roles);
    } else {
      this.shell.toast("Could not save that model choice.", "error");
    }
  }
}
