// The Settings tab -- a full port of the Tk build's SETTINGS_PAGES.
//
// Every field the Tk window had is here, on the same page, in the same group,
// with the same hint text. Writes go to the endpoints in aios_ui/settings_api.py
// so the clamping rules (voice_settings.merge_voice_dictation, the operator
// defaults, the theme ranges) stay in one place instead of being re-guessed in
// JavaScript.
//
// Commit timing matches the Tk build on purpose: text commits on blur/Enter so
// a half-typed path is never saved, sliders commit when you stop dragging, and
// toggles/selects commit immediately.

import { api, native } from "./bridge.js";

const SETTLE_MS = 220; // how long a slider must rest before it is written

// Browser key names -> the AHK names in voice_settings.SAFE_HOTKEYS.
const CAPTURE_KEYS = {
  Insert: "Insert", Home: "Home", End: "End", PageUp: "PageUp", PageDown: "PageDown",
  Delete: "Delete", ScrollLock: "ScrollLock", Pause: "Pause", ContextMenu: "AppsKey",
};

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = String(text ?? "");
  return div.innerHTML;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

export class SettingsTab {
  constructor(host, shell) {
    this.host = host;
    this.shell = shell;
    this.activePage = localStorage.getItem("aios:settings-page") || "General";
    this.config = {};
    this.meta = null;
    this._timers = new Map();
    this._statusTimer = null;

    this.render();
    this.boot();
  }

  async boot() {
    // Shell already loaded config at startup — paint from that while meta arrives
    // so the page is never a multi-second blank panel.
    if (this.shell && this.shell.config) this.config = this.shell.config;
    const [config, meta] = await Promise.all([api("/api/config"), api("/api/settings/meta")]);
    if (config && config.ok) this.config = config.config || {};
    if (meta && meta.ok) this.meta = meta;
    if (!this.meta) {
      this.host.querySelector("#settings-body").innerHTML =
        `<div class="placeholder"><strong>Settings unavailable</strong>The backend did not answer.</div>`;
      return;
    }
    this.renderNav();
    this.renderPage();
    // Ollama models are intentionally omitted from the first meta if the probe
    // would have stalled the tab; pick them up once the cache has warmed.
    this.refreshLocalModels();
  }

  async refreshLocalModels() {
    await new Promise((resolve) => setTimeout(resolve, 700));
    if (this._dead || !this.meta) return;
    const next = await api("/api/settings/meta");
    if (this._dead || !next || !next.ok || !next.agent_models) return;
    const before = (this.meta.agent_models || []).map((row) => row.id).join("\0");
    const after = next.agent_models.map((row) => row.id).join("\0");
    if (before === after) return;
    this.meta.agent_models = next.agent_models;
    if (this.activePage === "Voice agent") this.renderPage();
  }

  destroy() {
    this._dead = true;
    for (const timer of this._timers.values()) clearTimeout(timer);
    this._timers.clear();
    if (this._statusTimer) clearTimeout(this._statusTimer);
    if (this._updatePoll) clearInterval(this._updatePoll);
  }

  // ------------------------------------------------------------- config access

  get theme() { return this.config.theme || {}; }
  get voice() { return this.config.voice_dictation || {}; }
  get operator() { return this.config.ai_operator || {}; }
  get relay() { return this.config.phone_relay || {}; }

  /** Voice default for a key, so an unset field shows what the agent will use. */
  vd(key, fallback) {
    const value = this.voice[key];
    if (value !== undefined && value !== null) return value;
    const fromDefaults = (this.meta && this.meta.voice_defaults) ? this.meta.voice_defaults[key] : undefined;
    return fromDefaults !== undefined ? fromDefaults : fallback;
  }

  // ------------------------------------------------------------------- saving

  saved(label) {
    const node = this.host.querySelector("#settings-status");
    if (!node) return;
    node.textContent = label ? `Saved · ${label}` : "Saved";
    node.classList.remove("problem");
    if (this._statusTimer) clearTimeout(this._statusTimer);
    this._statusTimer = setTimeout(() => { node.textContent = ""; }, 2200);
  }

  problem(message) {
    const node = this.host.querySelector("#settings-status");
    if (!node) return;
    node.textContent = message;
    node.classList.add("problem");
  }

  async post(path, body, label) {
    const result = await api(path, { method: "POST", body });
    if (!result || result.ok === false) {
      this.problem(result && result.error ? result.error : "Could not save.");
      return null;
    }
    if (label) this.saved(label);
    return result;
  }

  /** Top-level helper_config keys. */
  async setConfig(patch, label) {
    Object.assign(this.config, patch);
    const result = await this.post("/api/config", patch, label);
    if (result && result.config) this.config = result.config;
    this.shell.config = this.config;
    this.shell.renderSubtitle();
    return result;
  }

  async setVoice(patch, label) {
    Object.assign(this.config.voice_dictation || (this.config.voice_dictation = {}), patch);
    const result = await this.post("/api/settings/voice", { patch }, label);
    if (result && result.voice_dictation) this.config.voice_dictation = result.voice_dictation;
    return result;
  }

  async setTheme(patch, label) {
    Object.assign(this.config.theme || (this.config.theme = {}), patch);
    const result = await this.post("/api/settings/theme", { patch }, label);
    if (result && result.theme) this.config.theme = result.theme;
    this.shell.config = this.config;
    this.shell.applyTheme(this.config.theme, this.config);
    return result;
  }

  async setOperator(patch, label) {
    Object.assign(this.config.ai_operator || (this.config.ai_operator = {}), patch);
    const result = await this.post("/api/settings/operator", { patch }, label);
    if (result && result.ai_operator) this.config.ai_operator = result.ai_operator;
    return result;
  }

  async setRelay(patch, label) {
    Object.assign(this.config.phone_relay || (this.config.phone_relay = {}), patch);
    const result = await this.post("/api/settings/relay", { patch }, label);
    if (result && result.phone_relay) this.config.phone_relay = result.phone_relay;
    return result;
  }

  /** Debounce per control, so dragging a slider writes once when it settles. */
  settle(key, fn) {
    const pending = this._timers.get(key);
    if (pending) clearTimeout(pending);
    this._timers.set(key, setTimeout(() => {
      this._timers.delete(key);
      fn();
    }, SETTLE_MS));
  }

  // --------------------------------------------------------------- the shell

  render() {
    this.host.innerHTML = `
      <div class="settings-root">
        <div class="settings-rail" id="settings-rail"></div>
        <div class="settings-main">
          <div class="settings-head">
            <span class="settings-hint" id="settings-hint"></span>
            <span class="settings-status" id="settings-status"></span>
          </div>
          <div class="settings-body" id="settings-body">
            <div class="placeholder"><strong>Loading settings…</strong></div>
          </div>
        </div>
      </div>
    `;
    this.host.querySelector("#settings-rail").addEventListener("click", (event) => {
      const button = event.target.closest("[data-page]");
      if (!button) return;
      this.activePage = button.dataset.page;
      localStorage.setItem("aios:settings-page", this.activePage);
      this.renderNav();
      this.renderPage();
    });
  }

  renderNav() {
    const pages = this.meta.pages || [];
    if (!pages.some(([name]) => name === this.activePage)) this.activePage = pages[0][0];
    this.host.querySelector("#settings-rail").innerHTML = pages.map(([name, hint]) => `
      <button class="settings-nav-btn${name === this.activePage ? " active" : ""}"
              data-page="${escapeHtml(name)}" title="${escapeHtml(hint)}">${escapeHtml(name)}</button>
    `).join("");
    const hint = (pages.find(([name]) => name === this.activePage) || [, ""])[1];
    this.host.querySelector("#settings-hint").textContent = hint || "";
  }

  renderPage() {
    const body = this.host.querySelector("#settings-body");
    body.innerHTML = "";
    body.scrollTop = 0;
    if (this._updatePoll) { clearInterval(this._updatePoll); this._updatePoll = null; }
    const render = {
      "General": this.pageGeneral,
      "Appearance": this.pageAppearance,
      "Voice": this.pageVoice,
      "Voice agent": this.pageVoiceAgent,
      "OPERATOR": this.pageOperator,
      "Models": this.pageModels,
      "Macro pad": this.pageMacroPad,
    }[this.activePage] || this.pageGeneral;
    render.call(this, body);
  }

  // -------------------------------------------------------- building blocks

  /** A titled card. Returns the frame to drop fields into. */
  group(parent, title, hint = "") {
    const card = el("div", "settings-group");
    card.appendChild(el("div", "settings-group-title", title));
    if (hint) card.appendChild(el("div", "settings-group-hint", hint));
    const body = el("div", "settings-group-body");
    card.appendChild(body);
    parent.appendChild(card);
    return body;
  }

  /** One labelled row. Returns the frame the control goes in. */
  field(parent, label, hint = "") {
    const wrap = el("div", "settings-row");
    const head = el("div", "settings-row-head");
    head.appendChild(el("label", "settings-label", label));
    const control = el("div", "settings-field");
    head.appendChild(control);
    wrap.appendChild(head);
    if (hint) wrap.appendChild(el("div", "settings-row-hint", hint));
    parent.appendChild(wrap);
    return control;
  }

  /** Text that saves on blur or Enter -- never on every keystroke. */
  text(control, value, onCommit, { placeholder = "", mono = false, mask = false } = {}) {
    const input = document.createElement("input");
    input.type = mask ? "password" : "text";
    input.className = `settings-input${mono ? " mono" : ""}`;
    input.value = value == null ? "" : String(value);
    input.placeholder = placeholder;
    let last = input.value;
    const commit = () => {
      if (input.value === last) return;
      last = input.value;
      onCommit(input.value);
    };
    input.addEventListener("blur", commit);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); commit(); input.blur(); }
    });
    control.appendChild(input);
    return input;
  }

  toggle(control, value, onChange) {
    const label = el("label", "settings-toggle");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = !!value;
    box.addEventListener("change", () => onChange(box.checked));
    label.appendChild(box);
    label.appendChild(el("span", "settings-toggle-track"));
    control.appendChild(label);
    return box;
  }

  /**
   * @param options string[] | {id,label,hint}[] -- an unknown current value is
   * prepended so a hand-edited config never silently switches to something else.
   */
  select(control, value, options, onChange) {
    const node = document.createElement("select");
    node.className = "settings-select";
    const rows = (options || []).map((item) =>
      typeof item === "string" ? { id: item, label: item } : item);
    if (value != null && String(value) !== "" && !rows.some((row) => String(row.id) === String(value))) {
      rows.unshift({ id: value, label: String(value) });
    }
    for (const row of rows) {
      const option = document.createElement("option");
      option.value = String(row.id);
      option.textContent = row.hint ? `${row.label}  ·  ${row.hint}` : String(row.label);
      node.appendChild(option);
    }
    node.value = String(value ?? "");
    node.addEventListener("change", () => onChange(node.value));
    control.appendChild(node);
    return node;
  }

  slider(control, { value, min, max, step = 1, suffix = "", decimals = 0, onChange, key }) {
    const row = el("div", "settings-slider-row");
    const input = document.createElement("input");
    input.type = "range";
    input.min = String(min);
    input.max = String(max);
    input.step = String(step);
    input.value = String(value ?? min);
    const readout = el("span", "settings-slider-value");
    const format = (raw) => `${decimals ? Number(raw).toFixed(decimals) : Math.round(Number(raw))}${suffix}`;
    readout.textContent = format(input.value);
    input.addEventListener("input", () => {
      readout.textContent = format(input.value);
      // Dragging fires per pixel; only the settled value is written.
      this.settle(key || String(min) + max + suffix, () => onChange(parseFloat(input.value)));
    });
    row.appendChild(input);
    row.appendChild(readout);
    control.appendChild(row);
    return input;
  }

  radius(control, value, onChange) {
    const row = el("div", "settings-slider-row settings-radius-row");
    const range = document.createElement("input");
    range.type = "range";
    range.min = "0";
    range.max = "100";
    range.step = "1";
    range.value = String(Math.min(100, Math.max(0, Number(value) || 0)));
    const exact = document.createElement("input");
    exact.type = "number";
    exact.className = "settings-radius-value";
    exact.min = "0";
    exact.step = "any";
    exact.value = String(Math.max(0, Number(value) || 0));
    exact.setAttribute("aria-label", "Exact corner radius in pixels");

    const apply = (raw, settled) => {
      const parsed = Number(raw);
      if (!Number.isFinite(parsed) || parsed < 0) {
        if (settled) this.problem("Corner radius must be zero or a positive number.");
        return;
      }
      const next = Math.round(parsed * 1000) / 1000;
      exact.value = String(next);
      range.value = String(Math.min(100, next));
      if (settled) onChange(next);
      else this.settle("radius", () => onChange(next));
    };
    range.addEventListener("input", () => apply(range.value, false));
    exact.addEventListener("blur", () => apply(exact.value, true));
    exact.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); apply(exact.value, true); exact.blur(); }
    });
    row.append(range, exact);
    control.appendChild(row);
    return { range, exact };
  }

  /** Swatch + native picker + hex field, all three driving the same key. */
  color(parent, key, label, value, onChange) {
    const row = el("div", "settings-color-row");
    row.appendChild(el("span", "settings-color-label", label));

    const picker = document.createElement("input");
    picker.type = "color";
    picker.className = "settings-color-swatch";
    const normalise = (raw) => (/^#[0-9a-fA-F]{6}$/.test(String(raw || "")) ? String(raw) : "#000000");
    picker.value = normalise(value);

    const hex = document.createElement("input");
    hex.type = "text";
    hex.className = "settings-input mono settings-color-hex";
    hex.value = value == null ? "" : String(value);

    const apply = (next) => {
      if (!/^#[0-9a-fA-F]{6}$/.test(next)) {
        this.problem(`${label}: use colors like #61dafb.`);
        return;
      }
      picker.value = next;
      hex.value = next;
      onChange(next);
    };
    // input fires while the OS picker is open, so the window recolours live.
    picker.addEventListener("input", () => { hex.value = picker.value; onChange(picker.value); });
    hex.addEventListener("blur", () => apply(hex.value.trim()));
    hex.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); apply(hex.value.trim()); hex.blur(); }
    });

    row.appendChild(picker);
    row.appendChild(hex);

    const reset = el("button", "btn compact ghost", "Reset");
    reset.addEventListener("click", () => {
      const fallback = (this.meta.theme_defaults || {})[key];
      if (fallback) apply(String(fallback));
    });
    row.appendChild(reset);
    parent.appendChild(row);
  }

  button(control, label, onClick, className = "btn compact") {
    const node = el("button", className, label);
    node.addEventListener("click", () => onClick(node));
    control.appendChild(node);
    return node;
  }

  actions(parent) {
    const row = el("div", "settings-actions");
    parent.appendChild(row);
    return row;
  }

  // ---------------------------------------------------------- page: General

  pageGeneral(body) {
    const cfg = this.config;

    let group = this.group(body, "Project folder", "Where aiOS looks for your markdown projects.");
    let control = this.field(group, "Location",
      "Saved when you leave the field or press Enter. The folder is created if missing.");
    const rootInput = this.text(control, cfg.project_root, async (value) => {
      const result = await this.post("/api/settings/project-root", { path: value }, "Location");
      if (result && result.project_root) {
        this.config.project_root = result.project_root;
        this.shell.config = this.config;
        this.shell.renderSubtitle();
      }
    });
    this.button(control, "Browse…", async () => {
      const picked = await native("pick_folder");
      if (!picked) return;
      rootInput.value = picked;
      const result = await this.post("/api/settings/project-root", { path: picked }, "Location");
      if (result) { this.config.project_root = picked; this.shell.renderSubtitle(); }
    });

    // --- mobile remote
    const relay = this.relay;
    group = this.group(body, "Mobile remote",
      "Control OPERATOR from the aiOS phone app. Use the same private code on every PC.");
    control = this.field(group, "Remote URL");
    const urlInput = this.text(control, relay.url || "", (value) => this.setRelay({ url: value }, "Remote URL"));
    control = this.field(group, "Private code",
      "Typed once to pair. Not stored in settings — press Connect after entering it.");
    const codeInput = this.text(control, "", () => {}, { mask: true });
    control = this.field(group, "Computer name");
    const nameInput = this.text(control, relay.machine_name || this.meta.computer_name,
      (value) => this.setRelay({ machine_name: value }, "Computer name"));

    const relayRow = this.actions(group);
    const relayStatus = el("span", "settings-inline-status",
      this.meta.relay_paired ? `Connected as ${relay.machine_name || "this PC"}` : "Not connected");
    this.button(relayRow, "Connect", async (node) => {
      node.disabled = true;
      relayStatus.textContent = "Connecting securely…";
      const result = await api("/api/settings/relay/pair", {
        method: "POST",
        body: { url: urlInput.value.trim(), code: codeInput.value.trim(), name: nameInput.value.trim() },
      });
      node.disabled = false;
      if (!result || result.ok === false) {
        relayStatus.textContent = (result && result.error) || "Could not connect.";
        return;
      }
      codeInput.value = "";
      relayStatus.textContent = `Connected as ${result.machine_name || "this PC"}`;
    }, "btn compact accent");
    this.button(relayRow, "Open remote", () => {
      const url = urlInput.value.trim();
      if (!url) { relayStatus.textContent = "Enter your remote URL first"; return; }
      api("/api/tools/open_url", { method: "POST", body: { url } });
    });
    relayRow.appendChild(relayStatus);

    this.updateCard(body);
  }

  /** The "Update aiOS" card: source, check, update & restart, live log. */
  updateCard(body) {
    const meta = this.meta.updater || {};
    const group = this.group(body, "Update aiOS",
      meta.ok ? `current ${meta.current}  ·  press Check to compare with GitHub`
              : `updater unavailable: ${meta.error || "unknown error"}`);
    if (!meta.ok) return;

    let control = this.field(group, "Owner", "GitHub user/org");
    const owner = this.text(control, meta.owner, () => {});
    control = this.field(group, "Repo");
    const repo = this.text(control, meta.repo, () => {});
    control = this.field(group, "Branch", "usually main");
    const branch = this.text(control, meta.branch, () => {});

    const status = el("div", "settings-update-status", "");
    const log = el("pre", "settings-update-log", "");
    const row = this.actions(group);

    let canUpdate = false;
    const updateBtn = this.button(row, "Update & restart", async () => {
      if (!canUpdate) return;
      updateBtn.disabled = true;
      checkBtn.disabled = true;
      status.textContent = "updating…";
      log.textContent = "";
      const started = await api("/api/update/run", { method: "POST", body: {} });
      if (!started || started.ok === false) {
        status.textContent = (started && started.error) || "Could not start the update.";
        updateBtn.disabled = false;
        checkBtn.disabled = false;
        return;
      }
      this._updatePoll = setInterval(async () => {
        const result = await api("/api/update/log");
        if (!result || result.ok === false) return;
        log.textContent = (result.lines || []).join("\n");
        if (!result.running) {
          clearInterval(this._updatePoll);
          this._updatePoll = null;
          checkBtn.disabled = false;
          status.textContent = "done";
        }
      }, 700);
    }, "btn compact accent");
    updateBtn.disabled = true;

    const check = async (silent) => {
      checkBtn.disabled = true;
      if (!silent) status.textContent = "checking…";
      const result = await api("/api/update/check", { method: "POST", body: {} });
      checkBtn.disabled = false;
      if (!result || result.ok === false) {
        status.textContent = (result && result.error) || "could not reach GitHub";
        return;
      }
      canUpdate = !!result.behind;
      updateBtn.disabled = !canUpdate;
      status.textContent = canUpdate
        ? `update available: ${String(result.latest || "").slice(0, 8)} on ${result.branch} — ${result.message || ""}`
        : `up to date (${String(result.current || "").slice(0, 8)} on ${result.branch})`;
    };

    const saveBtn = this.button(row, "Save source", async () => {
      saveBtn.disabled = true;
      const result = await this.post("/api/update/source", {
        owner: owner.value.trim(), repo: repo.value.trim(), branch: branch.value.trim() || "main",
      }, "Update source");
      saveBtn.disabled = false;
      if (result) check(false);
    });
    const checkBtn = this.button(row, "Check for updates", () => check(false));
    // Move the buttons into the Tk order: save, check, update.
    row.insertBefore(saveBtn, row.firstChild);
    row.insertBefore(checkBtn, updateBtn);

    group.appendChild(status);
    group.appendChild(log);
    // Do not hit GitHub on every General open — that raced the page paint and
    // made Settings feel stuck. Check is one click away.
  }

  // ------------------------------------------------------- page: Appearance

  pageAppearance(body) {
    const theme = this.theme;

    let group = this.group(body, "Window", "Applies as you drag.");
    let control = this.field(group, "Opacity");
    this.slider(control, {
      key: "opacity", value: Math.round((Number(theme.opacity ?? 0.94)) * 100),
      min: 75, max: 100, suffix: "%",
      onChange: (value) => { native("set_opacity", value); this.setTheme({ opacity: value / 100 }, "Opacity"); },
    });
    control = this.field(group, "Text size");
    this.slider(control, {
      key: "font_size", value: Number(theme.font_size ?? 10), min: 8, max: 15,
      onChange: (value) => this.setTheme({ font_size: value }, "Text size"),
    });
    control = this.field(group, "Corner radius");
    this.radius(control, Number(theme.radius ?? 28),
      (value) => this.setTheme({ radius: value }, "Corner radius"));
    control = this.field(group, "Always on top", "Keeps aiOS above other windows.");
    this.toggle(control, theme.always_on_top !== false, (value) => {
      native("set_always_on_top", value);
      this.setTheme({ always_on_top: value }, "Always on top");
    });

    group = this.group(body, "Thinking dots", "The pulse shown while a model is working.");
    control = this.field(group, "Base opacity");
    this.slider(control, {
      key: "thinking_base_opacity", value: Number(theme.thinking_base_opacity ?? 45),
      min: 0, max: 100, suffix: "%",
      onChange: (value) => this.setTheme({ thinking_base_opacity: value }, "Base opacity"),
    });
    control = this.field(group, "Pulse opacity");
    this.slider(control, {
      key: "thinking_pulse_opacity", value: Number(theme.thinking_pulse_opacity ?? 100),
      min: 0, max: 100, suffix: "%",
      onChange: (value) => this.setTheme({ thinking_pulse_opacity: value }, "Pulse opacity"),
    });

    group = this.group(body, "Colors", "Click a swatch to change it — the window recolours live.");
    for (const [key, label] of this.meta.theme_colors || []) {
      const fallback = (this.meta.theme_defaults || {})[key];
      this.color(group, key, label, theme[key] ?? fallback, (value) => this.setTheme({ [key]: value }, label));
    }
    const row = this.actions(group);
    this.button(row, "Reset all colours", async () => {
      const ok = await this.shell.confirm("Reset colours?", "Every colour goes back to the aiOS defaults.");
      if (!ok) return;
      const patch = {};
      for (const [key] of this.meta.theme_colors || []) patch[key] = (this.meta.theme_defaults || {})[key];
      await this.setTheme(patch, "Colors");
      this.renderPage();
    });
  }

  // ------------------------------------------------------------- page: Voice

  pageVoice(body) {
    const separate = !!this.vd("separate_hotkeys", false);
    const voiceKey = String(this.vd("voice_hotkey", "Insert"));
    const aiosKey = String(this.vd("aios_hotkey", "Insert"));
    const summary = separate
      ? `Quick press ${voiceKey} toggles dictation; holding it ≥0.6 s stops on release. ${aiosKey} opens and closes aiOS.`
      : `Short press ${voiceKey} opens aiOS. Hold ${voiceKey} to dictate — release to stop and type.`;

    let group = this.group(body, "Keys", summary);
    let control = this.field(group, "Separate keys",
      "One key for dictation, another for opening aiOS. Recommended with a macro pad.");
    this.toggle(control, separate, async (value) => {
      await this.setVoice({ separate_hotkeys: value }, "Separate keys");
      this.renderPage(); // the rest of the group changes shape
    });

    control = this.field(group, separate ? "Dictation key" : "Shared key",
      "F13–F24 are macro-pad keys. Side mouse buttons work too. AutoHotkey picks changes up within ~2 s.");
    this.select(control, voiceKey, this.meta.hotkeys, async (value) => {
      await this.setVoice({ voice_hotkey: value }, "Dictation key");
      this.renderPage();
    });
    this.button(control, "Capture…", () => this.captureHotkey("voice_hotkey"));

    if (separate) {
      control = this.field(group, "Open aiOS key", "Immediate open/close — no hold wait.");
      this.select(control, aiosKey, this.meta.hotkeys, async (value) => {
        await this.setVoice({ aios_hotkey: value }, "Open aiOS key");
        this.renderPage();
      });
      this.button(control, "Capture…", () => this.captureHotkey("aios_hotkey"));
    } else {
      control = this.field(group, "Hold threshold",
        "Held longer than this means dictate; shorter means open aiOS.");
      this.slider(control, {
        key: "hold_ms", value: Number(this.vd("hold_ms", 280)), min: 150, max: 800, step: 10, suffix: " ms",
        onChange: (value) => this.setVoice({ hold_ms: value }, "Hold threshold"),
      });
    }

    // --- microphone
    group = this.group(body, "Microphone", "What aiOS listens to, and how hard it listens.");
    control = this.field(group, "Device",
      "Part of the device name, e.g. 'Yeti'. Leave empty to follow the Windows default.");
    this.text(control, this.vd("input_device", ""), (value) => this.setVoice({ input_device: value }, "Device"));
    control = this.field(group, "Sensitivity",
      "Lower picks up quieter speech but also more room noise.");
    this.slider(control, {
      key: "silence_rms",
      value: Math.max(1, Math.min(50, Math.round(Number(this.vd("silence_rms", 0.006)) * 10000))),
      min: 1, max: 50,
      // Stored as an RMS floor; the slider is the 1..50 scale the Tk build used.
      onChange: (value) => this.setVoice({ silence_rms: Math.round(value) / 10000 }, "Sensitivity"),
    });

    // --- transcription
    group = this.group(body, "Transcription",
      "Whisper runs locally. Bigger models are slower but hear you better.");
    control = this.field(group, "Model",
      "large-v3-turbo is the best pick on a GPU: near-large accuracy at small speed. "
      + "Changes preload in the background; large models can take about a minute to become ready.");
    this.select(control, this.vd("whisper_model", "small"), this.meta.whisper_models,
      (value) => this.setVoice({ whisper_model: value }, "Model"));
    control = this.field(group, "Language",
      "Auto and Swedish need a multilingual model (small, not small.en).");
    this.select(control, this.vd("language", "auto"), this.meta.whisper_languages,
      (value) => this.setVoice({ language: value }, "Language"));
    control = this.field(group, "Device", "cuda uses the GPU; cpu always works.");
    this.select(control, this.vd("device", "cuda"), this.meta.whisper_devices,
      (value) => this.setVoice({ device: value }, "Whisper device"));
    control = this.field(group, "Compute", "float16 on a GPU, int8 on CPU.");
    this.select(control, this.vd("compute_type", "int8"), this.meta.compute_types,
      (value) => this.setVoice({ compute_type: value }, "Compute"));
    control = this.field(group, "Vocabulary",
      "Names Whisper has never heard, comma separated. Biases the decoder toward them.");
    this.text(control, (this.vd("vocabulary", []) || []).join(", "),
      (value) => this.setVoice({ vocabulary: value }, "Vocabulary"));
    control = this.field(group, "Fix words",
      "wrong=right pairs applied to every transcript, e.g. ayos=aiOS, operator=OPERATOR.");
    this.text(control,
      Object.entries(this.vd("replacements", {}) || {}).map(([from, to]) => `${from}=${to}`).join(", "),
      (value) => this.setVoice({ replacements: value }, "Fix words"), { mono: true });
    control = this.field(group, "Filter silence junk",
      "Drops the stock phrases Whisper invents on silence instead of typing them.");
    this.toggle(control, this.vd("hallucination_filter", true),
      (value) => this.setVoice({ hallucination_filter: value }, "Filter silence junk"));
    control = this.field(group, "Keep transcript log",
      "Appends every finished turn to voice-transcripts.jsonl (git-ignored).");
    this.toggle(control, this.vd("transcript_history", true),
      (value) => this.setVoice({ transcript_history: value }, "Keep transcript log"));

    // --- output
    group = this.group(body, "Output", "What happens when a turn finishes.");
    control = this.field(group, "Overlay opacity", "Background of the dictation pill and the agent chat panel.");
    this.slider(control, {
      key: "overlay_opacity", value: Number(this.vd("overlay_opacity", 85)), min: 20, max: 100, suffix: "%",
      onChange: (value) => this.setVoice({ overlay_opacity: value }, "Overlay opacity"),
    });
    control = this.field(group, "Typing delay",
      "Raise this only if an app drops characters when text is typed quickly.");
    this.slider(control, {
      key: "typing_delay_ms", value: Number(this.vd("typing_delay_ms", 0)), min: 0, max: 50, suffix: " ms",
      onChange: (value) => this.setVoice({ typing_delay_ms: value }, "Typing delay"),
    });
    control = this.field(group, "Speak replies", "Read the agent's answers out loud.");
    this.toggle(control, this.vd("agent_tts_enabled", true),
      (value) => this.setVoice({ agent_tts_enabled: value }, "Speak replies"));
    control = this.field(group, "Stop on new speech",
      "Cuts the spoken reply the moment you press to talk, so it never talks into your mic.");
    this.toggle(control, this.vd("barge_in", true), (value) => this.setVoice({ barge_in: value }, "Stop on new speech"));

    // --- discord
    group = this.group(body, "Discord", "Mute yourself in Discord while dictating.");
    control = this.field(group, "Mute while dictating");
    this.toggle(control, this.vd("discord_mute_enabled", false),
      (value) => this.setVoice({ discord_mute_enabled: value }, "Mute while dictating"));
    control = this.field(group, "Mute key",
      "Match Discord → Keybinds → Toggle Mute. Combos work: Alt+M, Ctrl+Shift+M, F8. Applies after AutoHotkey reloads.");
    this.text(control, this.vd("discord_mute_hotkey", ""),
      (value) => this.setVoice({ discord_mute_hotkey: value }, "Mute key"));
  }

  /** Grab the next key press and store it as the voice or open-aiOS hotkey. */
  captureHotkey(field) {
    const title = field === "voice_hotkey" ? "Set dictation key" : "Set open aiOS key";
    const node = el("div", "modal-backdrop");
    node.innerHTML = `
      <div class="modal">
        <div class="modal-title">${escapeHtml(title)}</div>
        <div class="modal-detail">Press the key you want to use. Side mouse buttons work too.<br>
          Esc cancels.</div>
        <div class="capture-key" id="capture-key">waiting…</div>
        <div class="modal-actions"><button class="btn compact ghost" data-choice="no">Cancel</button></div>
      </div>
    `;
    document.body.appendChild(node);
    requestAnimationFrame(() => node.classList.add("show"));

    const readout = node.querySelector("#capture-key");
    const close = () => {
      window.removeEventListener("keydown", onKey, true);
      window.removeEventListener("mousedown", onMouse, true);
      node.remove();
    };
    const commit = async (name) => {
      close();
      await this.setVoice({ [field]: name }, title);
      this.renderPage();
    };
    const onKey = (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (event.key === "Escape") { close(); return; }
      const name = CAPTURE_KEYS[event.key] || (/^F([1-9]|1\d|2[0-4])$/.test(event.key) ? event.key : "");
      if (!name) {
        readout.textContent = `${event.key} cannot be used — pick F13–F24, Insert, Home, End, PageUp/Down, Delete, Pause or ScrollLock.`;
        return;
      }
      readout.textContent = name;
      commit(name);
    };
    const onMouse = (event) => {
      if (event.button !== 3 && event.button !== 4) return;
      event.preventDefault();
      event.stopPropagation();
      commit(event.button === 3 ? "Mouse 4" : "Mouse 5");
    };
    window.addEventListener("keydown", onKey, true);
    window.addEventListener("mousedown", onMouse, true);
    node.addEventListener("click", (event) => {
      if (event.target.closest("[data-choice]") || event.target === node) close();
    });
  }

  // ------------------------------------------------------- page: Voice agent

  pageVoiceAgent(body) {
    let group = this.group(body, "Model",
      "What answers the Agent chat and spoken turns. Cloud models need an OpenAI key; "
      + "Ollama models run locally and stay free.");
    let control = this.field(group, "Agent model",
      "Luna is the cheap cloud default. Pick any ollama:… entry to run the Agent chat locally.");
    this.select(control, this.vd("agent_model", "gpt-5.6-luna"), this.meta.agent_models,
      (value) => this.setVoice({ agent_model: value }, "Agent model"));
    control = this.field(group, "Thinking",
      "For local Ollama models, off/low skips thinking (much faster). Medium/high enable it.");
    this.select(control, this.vd("agent_reasoning", "low"), this.meta.agent_reasoning,
      (value) => this.setVoice({ agent_reasoning: value }, "Thinking"));
    control = this.field(group, "Voice", "Which TTS voice reads the agent's answers.");
    this.select(control, this.vd("agent_tts_voice", "m3"), this.meta.agent_tts_voices,
      (value) => this.setVoice({ agent_tts_voice: value }, "Voice"));

    group = this.group(body, "Tools",
      "Each switch adds or removes a capability. Off means the tool is never offered to the model.");
    for (const [key, label, hint] of this.meta.voice_tools || []) {
      control = this.field(group, label, hint);
      this.toggle(control, this.vd(key, true), (value) => this.setVoice({ [key]: value }, label));
    }

    group = this.group(body, "Shell safety",
      "The agent's input is dictated speech, so a mis-heard sentence must not be able to do damage.");
    control = this.field(group, "Block destructive",
      "Refuses recursive deletes, formats, shutdowns and download-and-run outright.");
    this.toggle(control, this.vd("agent_shell_guard", true),
      (value) => this.setVoice({ agent_shell_guard: value }, "Block destructive"));
    control = this.field(group, "Confirm changes",
      "Anything that changes state is read back to you and waits for a spoken yes.");
    this.toggle(control, this.vd("agent_shell_confirm", true),
      (value) => this.setVoice({ agent_shell_confirm: value }, "Confirm changes"));

    group = this.group(body, "Conversation", "How much the agent carries between turns.");
    control = this.field(group, "Remember chat",
      "Keeps the conversation across a restart of the dictation process.");
    this.toggle(control, this.vd("agent_persist_memory", true),
      (value) => this.setVoice({ agent_persist_memory: value }, "Remember chat"));
    control = this.field(group, "Forget after",
      "Minutes of silence before the conversation resets. 0 never forgets.");
    this.slider(control, {
      key: "agent_memory_minutes", value: Number(this.vd("agent_memory_minutes", 10)),
      min: 0, max: 120, suffix: " min",
      onChange: (value) => this.setVoice({ agent_memory_minutes: value }, "Forget after"),
    });
    control = this.field(group, "Tool rounds",
      "How many times the agent may call tools before it must answer.");
    this.slider(control, {
      key: "agent_max_rounds", value: Number(this.vd("agent_max_rounds", 6)), min: 1, max: 12,
      onChange: (value) => this.setVoice({ agent_max_rounds: value }, "Tool rounds"),
    });

    group = this.group(body, "File access",
      "Folders the file tools may touch. One per line. Empty means the built-in set: "
      + "the project root plus Documents, Desktop and Downloads.");
    const roots = document.createElement("textarea");
    roots.className = "settings-textarea";
    roots.value = (this.vd("agent_file_roots", []) || []).join("\n");
    roots.placeholder = "C:\\Users\\you\\Documents";
    roots.addEventListener("blur", () => {
      const list = roots.value.split("\n").map((line) => line.trim()).filter(Boolean);
      this.setVoice({ agent_file_roots: list }, "File access");
    });
    this.field(group, "Allowed folders").appendChild(roots);
  }

  // -------------------------------------------------------- page: OPERATOR

  pageOperator(body) {
    const op = this.operator;

    let group = this.group(body, "Models", "Which models plan and drive the run.");
    let control = this.field(group, "Acting model", "Looks at the screen and decides the next click.");
    this.select(control, op.model || this.meta.operator_models[0], this.meta.operator_models,
      (value) => this.setOperator({ model: value }, "Acting model"));
    control = this.field(group, "Planning model",
      "Optional second model that writes the plan before OPERATOR starts.");
    this.select(control, op.planner_model || "off", ["off", ...this.meta.operator_models],
      (value) => this.setOperator({ planner_model: value }, "Planning model"));
    control = this.field(group, "Reasoning", "Higher thinks longer per step. Costs more and runs slower.");
    this.select(control, op.reasoning || "low", this.meta.operator_reasoning,
      (value) => this.setOperator({ reasoning: value }, "Reasoning"));

    group = this.group(body, "Run limits", "Guard rails for a run that goes wrong.");
    control = this.field(group, "Max steps", "OPERATOR stops and asks once it has used this many actions.");
    this.slider(control, {
      key: "op_steps", value: parseInt(op.steps, 10) || 25, min: 1, max: 200,
      onChange: (value) => this.setOperator({ steps: value }, "Max steps"),
    });
    control = this.field(group, "Step delay", "Pause between actions. Raise it if apps cannot keep up.");
    this.slider(control, {
      key: "op_delay", value: parseFloat(op.delay) || 0.2, min: 0, max: 3, step: 0.05,
      suffix: " s", decimals: 2,
      onChange: (value) => this.setOperator({ delay: value }, "Step delay"),
    });

    group = this.group(body, "Behaviour", "What OPERATOR may do and how it reports back.");
    control = this.field(group, "Speak progress", "Narrates what it is doing out loud.");
    this.toggle(control, !!op.tts, (value) => this.setOperator({ tts: value }, "Speak progress"));
    control = this.field(group, "Voice", "Which TTS voice narrates the run.");
    this.select(control, op.voice || "nova", this.meta.operator_voices,
      (value) => this.setOperator({ voice: value }, "Voice"));
    control = this.field(group, "Allow shell", "Lets OPERATOR run commands instead of only clicking.");
    this.toggle(control, !!op.shell, (value) => this.setOperator({ shell: value }, "Allow shell"));
    control = this.field(group, "Use Codex auth", "Bills through your Codex sign-in instead of the API key.");
    this.toggle(control, !!op.codex_auth, (value) => this.setOperator({ codex_auth: value }, "Use Codex auth"));

    group.appendChild(el("div", "settings-note",
      "Monitor choice, preview and cursor test live on the OPERATOR tab, next to the run controls."));
  }

  // ---------------------------------------------------------- page: Models

  pageModels(body) {
    const cfg = this.config;

    let group = this.group(body, "Codex", "The coding agent behind the Codex tab.");
    let control = this.field(group, "Model");
    this.text(control, cfg.codex_model, (value) => this.setConfig({ codex_model: value }, "Codex model"));
    control = this.field(group, "Thinking");
    this.select(control, cfg.codex_reasoning || "none", this.meta.codex_reasoning,
      (value) => this.setConfig({ codex_reasoning: value }, "Codex thinking"));

    group = this.group(body, "Quick chat", "The faster model used for short questions.");
    control = this.field(group, "Model");
    this.text(control, cfg.quick_codex_model, (value) => this.setConfig({ quick_codex_model: value }, "Quick model"));
    control = this.field(group, "Thinking");
    this.select(control, cfg.quick_codex_reasoning || "none", this.meta.codex_reasoning,
      (value) => this.setConfig({ quick_codex_reasoning: value }, "Quick thinking"));

    const storedOpenai = String(cfg.openai_api_key || "");
    group = this.group(body, "OpenAI", "Used by the side chat, the voice agent and OPERATOR.");
    control = this.field(group, "Chat model");
    this.text(control, cfg.chat_model, (value) => this.setConfig({ chat_model: value }, "Chat model"));
    control = this.field(group, "API key",
      this.meta.env_openai && !storedOpenai
        ? "Currently using the OPENAI_API_KEY environment variable — leave empty to keep it."
        : "Stored in helper_config.json, which is git-ignored.");
    this.text(control, storedOpenai, (value) => {
      this.config.openai_api_key = value;
      this.post("/api/settings/openai-key", { key: value }, "OpenAI key");
    }, { mask: true, mono: true });

    group = this.group(body, "CODE default", "Default provider and model for new CODE sessions.");
    control = this.field(group, "Provider");
    this.text(control, cfg.code_default_provider || "openrouter",
      (value) => this.setConfig({ code_default_provider: value }, "CODE default provider"),
      { placeholder: "codex, claude, cursor, ollama, openrouter" });
    control = this.field(group, "Model");
    this.text(control, cfg.code_default_model || "deepseek/deepseek-v4-flash",
      (value) => this.setConfig({ code_default_model: value }, "CODE default model"),
      { placeholder: "Full model ID as shown in the CODE model picker" });

    this.openrouterGroup(body);
  }

  openrouterGroup(body) {
    const cfg = this.config;
    const storedKey = String(cfg.openrouter_api_key || "");
    const group = this.group(body, "OpenRouter",
      "CODE provider for hosted models (DeepSeek and more). Enable which models appear in the CODE picker.");
    const control = this.field(group, "API key",
      this.meta.env_openrouter && !storedKey
        ? "Currently using the OPENROUTER_API_KEY environment variable — leave empty to keep it."
        : "Stored in helper_config.json, which is git-ignored.");
    this.text(control, storedKey, (value) => {
      this.config.openrouter_api_key = value;
      this.post("/api/settings/openrouter-key", { key: value }, "OpenRouter key");
    }, { mask: true, mono: true });

    const head = el("div", "settings-list-head");
    head.appendChild(el("span", "settings-list-title", "Models in CODE"));
    const refresh = el("button", "btn compact", "Refresh tool models");
    head.appendChild(refresh);
    group.appendChild(head);

    const list = el("div", "settings-model-list");
    group.appendChild(list);

    const enabled = new Set(this.meta.openrouter_enabled || []);
    const draw = (models) => {
      list.innerHTML = "";
      if (!models.length) {
        list.appendChild(el("div", "settings-note", "No OpenRouter models cached. Press Refresh tool models."));
        return;
      }
      for (const model of models) {
        const row = el("label", "settings-model-row");
        const box = document.createElement("input");
        box.type = "checkbox";
        box.checked = enabled.has(model.id);
        box.addEventListener("change", () => {
          if (box.checked) enabled.add(model.id); else enabled.delete(model.id);
          this.post("/api/settings/openrouter/models", { enabled: [...enabled] }, "OpenRouter models")
            .then((result) => {
              // The backend refuses an empty list; mirror what it actually saved.
              if (!result || !result.openrouter_enabled_models) return;
              enabled.clear();
              for (const id of result.openrouter_enabled_models) enabled.add(id);
              box.checked = enabled.has(model.id);
            });
        });
        row.appendChild(box);
        const copy = el("div", "settings-model-copy");
        copy.appendChild(el("b", null, model.label));
        copy.appendChild(el("i", null, `${model.id}${model.description ? ` · ${model.description}` : ""}`));
        row.appendChild(copy);
        list.appendChild(row);
      }
    };
    draw(this.meta.openrouter_models || []);

    refresh.addEventListener("click", async () => {
      refresh.disabled = true;
      this.saved("Refreshing OpenRouter tool models…");
      const result = await api("/api/settings/openrouter/refresh", { method: "POST", body: {} });
      refresh.disabled = false;
      if (!result || result.ok === false) {
        this.problem((result && result.error) || "Could not refresh models.");
        return;
      }
      this.meta.openrouter_models = result.models || [];
      draw(this.meta.openrouter_models);
      this.saved(`Loaded ${result.count || 0} tool-capable OpenRouter models`);
    });
  }

  // -------------------------------------------------------- page: Macro pad

  pageMacroPad(body) {
    let group = this.group(body, "How it works today",
      "Macro-pad buttons talk to aiOS over a local socket by running one of these files. "
      + "Bind each one in your macro software.");
    for (const file of this.meta.macro_files || []) {
      const row = el("div", `macro-row${file.present ? "" : " missing"}`);
      row.appendChild(el("code", null, file.name));
      row.appendChild(el("span", "macro-hint", file.present ? file.hint : `${file.hint}  (missing)`));
      row.appendChild(el("span", "macro-status", file.present ? "present" : "missing"));
      group.appendChild(row);
    }
    const row = this.actions(group);
    this.button(row, "Open folder", () => api("/api/tools/open_base_dir", { method: "POST", body: {} }));

    group = this.group(body, "Planned",
      "Macro-pad configuration is moving into aiOS so buttons can be bound here instead of "
      + "through .bat files and external macro software.");
    for (const line of this.meta.macro_planned || []) {
      group.appendChild(el("div", "settings-bullet", `·  ${line}`));
    }
  }
}
