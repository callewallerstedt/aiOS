// The HARNESS page: what the CODE agent is made of.
//
// Reached from CODE, like BENCH, because it describes that harness. BENCH tells
// you how well it performs; this tells you what it is doing.
//
// Nothing on this page is written down in the frontend. Every tool, limit and
// model comes from /api/harness/meta, which reads code_jobs at request time --
// so the tool list here is the tool list the model is handed, and a limit shown
// here is the constant the loop reads. A page that described the harness from
// memory would be wrong the first time someone added a tool.

import { api } from "./bridge.js";
import { escapeHtml } from "./markdown.js";

const SECTIONS = [
  ["flow", "How a turn runs"],
  ["providers", "Agents"],
  ["tools", "Tools"],
  ["models", "The other models"],
  ["limits", "Limits"],
  ["context", "What it is told"],
  ["telemetry", "Session telemetry"],
  ["lifecycle", "Session states"],
];

export class HarnessTab {
  constructor(host, shell) {
    this.host = host;
    this.shell = shell;
    this.meta = null;
    this.status = {};
    this.section = "flow";

    this.render();
    this.boot();
  }

  // `api()` resolves rather than rejecting, and never honours an abort signal,
  // so the only reliable way not to write into a page that has been swapped out
  // is to check on the way back.
  destroy() {
    this.gone = true;
  }

  // ------------------------------------------------------------------ layout

  render() {
    this.host.innerHTML = `
      <div class="harness-head">
        <button class="btn compact ghost" data-harness="back">&#8592; CODE</button>
        <h1>HARNESS</h1>
        <span class="tagline">Every part of the CODE agent, read from the code that runs it.</span>
        <span class="spacer"></span>
        <button class="btn compact" data-harness="bench" title="Measure this harness">BENCH</button>
      </div>

      <div class="harness-split">
        <nav class="card harness-rail" data-harness="rail"></nav>
        <section class="card harness-body" data-harness="body">
          <div class="harness-empty">Reading the harness&hellip;</div>
        </section>
      </div>
    `;

    this.rail = this.host.querySelector('[data-harness="rail"]');
    this.body = this.host.querySelector('[data-harness="body"]');

    this.host.addEventListener("click", (event) => {
      const trigger = event.target.closest("[data-harness]");
      if (!trigger) return;
      const action = trigger.dataset.harness;
      if (action === "back") this.shell.show("CODE");
      else if (action === "bench") this.shell.show("BENCH");
      else if (action === "section") this.select(trigger.dataset.section);
    });
  }

  async boot() {
    const meta = await api("/api/harness/meta");
    if (this.gone) return;
    if (!meta || !meta.ok) {
      const why = (meta && meta.error) || "no answer from the harness";
      this.body.innerHTML = `<div class="harness-empty">Could not read the harness: ${escapeHtml(String(why))}</div>`;
      return;
    }
    this.meta = meta;
    this.renderRail();
    this.renderBody();

    // Readiness costs a subprocess per CLI provider, so it arrives after the
    // page is already usable rather than holding it up. It is a nicety: if it
    // never comes back, the page still describes the harness.
    const status = await api("/api/harness/status");
    if (this.gone) return;
    for (const row of (status && status.providers) || []) this.status[row.id] = row;
    if (this.section === "providers") this.renderBody();
  }

  renderRail() {
    this.rail.innerHTML = SECTIONS.map(([id, label]) => `
      <button class="harness-rail-btn${id === this.section ? " selected" : ""}"
              data-harness="section" data-section="${id}">
        <span class="label">${label}</span>
        <span class="count">${this.countFor(id)}</span>
      </button>
    `).join("");
  }

  countFor(id) {
    const rows = this.meta && this.meta[id];
    return Array.isArray(rows) ? rows.length : "";
  }

  select(id) {
    if (!id || id === this.section) return;
    this.section = id;
    this.renderRail();
    this.renderBody();
    this.body.scrollTop = 0;
  }

  renderBody() {
    if (!this.meta) return;
    const render = {
      flow: () => this.renderFlow(),
      providers: () => this.renderProviders(),
      tools: () => this.renderTools(),
      models: () => this.renderModels(),
      limits: () => this.renderLimits(),
      context: () => this.renderContext(),
      telemetry: () => this.renderTelemetry(),
      lifecycle: () => this.renderLifecycle(),
    }[this.section];
    this.body.innerHTML = render ? render() : "";
  }

  // ----------------------------------------------------------------- sections

  intro(text) {
    return `<p class="harness-intro">${text}</p>`;
  }

  renderFlow() {
    const steps = this.meta.flow.map((row, index) => `
      <li class="harness-step">
        <span class="n">${index + 1}</span>
        <div>
          <strong>${escapeHtml(row.step)}</strong>
          <p>${escapeHtml(row.detail)}</p>
        </div>
      </li>
    `).join("");
    return `
      ${this.intro(`One message to a CODE session becomes one <em>turn</em>. This is what happens
        between pressing Launch and the session going quiet.`)}
      <ol class="harness-flow">${steps}</ol>
      <p class="harness-note">Sessions are stored in
        <code>${escapeHtml(this.meta.jobs_dir)}</code>. A benchmark run points that
        somewhere else, which is why benchmark sessions never reach your list.</p>
    `;
  }

  renderProviders() {
    const cards = this.meta.providers.map((row) => {
      const status = this.status[row.id];
      const badge = status
        ? `<span class="harness-status ${status.ready ? "ready" : "cold"}">${status.ready ? "ready" : "not ready"}</span>`
        : `<span class="harness-status pending">checking&hellip;</span>`;
      const owner = row.tools === "aios"
        ? `<span class="harness-tag accent">aiOS tools</span>`
        : `<span class="harness-tag">brings its own tools</span>`;
      return `
        <article class="harness-card">
          <header>
            <strong>${escapeHtml(row.label)}</strong>
            ${owner}
            <span class="spacer"></span>
            ${badge}
          </header>
          <p class="lead">${escapeHtml(row.runs)}</p>
          <p>${escapeHtml(row.detail)}</p>
          ${row.default_model ? `<p class="harness-kv"><span>Default model</span><code>${escapeHtml(row.default_model)}</code></p>` : ""}
          ${status && !status.ready ? `<p class="harness-kv warn"><span>Why not</span>${escapeHtml(status.message)}</p>` : ""}
        </article>
      `;
    }).join("");
    return `
      ${this.intro(`Five agents can do the work. The split that matters: two of them run
        <em>inside</em> aiOS and use the tools below, and three are external CLIs that arrive
        with their own. Everything else on this page describes the first kind.`)}
      <div class="harness-cards">${cards}</div>
    `;
  }

  renderTools() {
    const byGroup = new Map();
    for (const tool of this.meta.tools) {
      if (!byGroup.has(tool.group)) byGroup.set(tool.group, []);
      byGroup.get(tool.group).push(tool);
    }
    const groups = this.meta.groups
      .filter((group) => byGroup.has(group.id))
      .map((group) => {
        const rows = byGroup.get(group.id).map((tool) => `
          <li class="harness-tool">
            <div class="top">
              <code class="name">${escapeHtml(tool.name)}</code>
              ${tool.parallel ? `<span class="harness-tag">parallel</span>` : `<span class="harness-tag warn">one at a time</span>`}
              ${tool.subagent ? `<span class="harness-tag">subagents too</span>` : ""}
            </div>
            <p>${escapeHtml(tool.description)}</p>
            ${tool.arguments.length ? `<div class="args">${tool.arguments.map((arg) => `
              <span class="arg${arg.required ? " required" : ""}">${escapeHtml(arg.name)}<i>${escapeHtml(arg.type)}</i></span>
            `).join("")}</div>` : ""}
          </li>
        `).join("");
        return `<section class="harness-group"><h3>${escapeHtml(group.label)}</h3><ul>${rows}</ul></section>`;
      }).join("");
    return `
      ${this.intro(`The ${this.meta.tools.length} tools the agent can call, with the exact
        descriptions the model is given. Read-only calls in the same round run together, up to
        ${escapeHtml(String(this.limitValue("Parallel tools")))} at once; anything that can change
        the repository runs one at a time so two edits can never interleave.`)}
      <div class="harness-tools">${groups}</div>
    `;
  }

  limitValue(name) {
    const row = (this.meta.limits || []).find((entry) => entry.name === name);
    return row ? row.value : "";
  }

  renderModels() {
    const cards = this.meta.models.map((row) => `
      <article class="harness-card">
        <header>
          <strong>${escapeHtml(row.name)}</strong>
          ${row.enabled ? "" : `<span class="harness-tag warn">off</span>`}
          <span class="spacer"></span>
          <code>${escapeHtml(row.model)}</code>
        </header>
        <p class="harness-kv"><span>Runs</span>${escapeHtml(row.when)}</p>
        <p class="harness-kv"><span>Sees</span>${escapeHtml(row.sees)}</p>
        <p class="harness-kv"><span>Does</span>${escapeHtml(row.does)}</p>
        <p class="harness-kv"><span>Affects</span>${escapeHtml(row.affects)}</p>
        <p class="harness-kv"><span>Limit</span>${escapeHtml(row.limit)}</p>
      </article>
    `).join("");
    return `
      ${this.intro(`The coding agent is not the only model in a session. Three others run, and
        their tokens are counted against the session's cost, which is why a cheap-looking model
        can still produce an expensive run.`)}
      <div class="harness-cards">${cards}</div>
    `;
  }

  renderLimits() {
    const rows = this.meta.limits.map((row) => `
      <li class="harness-limit">
        <div class="top"><strong>${escapeHtml(row.name)}</strong><code>${escapeHtml(row.value)}</code></div>
        <p>${escapeHtml(row.detail)}</p>
      </li>
    `).join("");
    return `
      ${this.intro(`The numbers the loop actually reads. Every one of them is an environment
        variable, so none of this is baked in.`)}
      <ul class="harness-limits">${rows}</ul>
    `;
  }

  renderContext() {
    const rows = this.meta.context.map((row) => `
      <li class="harness-limit">
        <div class="top"><strong>${escapeHtml(row.name)}</strong></div>
        <p>${escapeHtml(row.detail)}</p>
      </li>
    `).join("");
    return `
      ${this.intro(`What goes into the model's context before your brief does, and what happens
        to it when the conversation outgrows the budget.`)}
      <ul class="harness-limits">${rows}</ul>
    `;
  }

  renderTelemetry() {
    const rows = this.meta.telemetry.map((row) => `
      <article class="harness-telemetry-card">
        <header>
          <strong>${escapeHtml(row.name)}</strong>
          <code>${escapeHtml(row.fields)}</code>
        </header>
        <p>${escapeHtml(row.detail)}</p>
      </article>
    `).join("");
    return `
      ${this.intro(`The compact strip above each CODE transcript. Missing values stay blank;
        estimates are never presented as provider-reported usage.`)}
      <div class="harness-telemetry-grid">${rows}</div>
    `;
  }

  renderLifecycle() {
    const rows = this.meta.lifecycle.map((row) => `
      <li class="harness-state ${row.kind}">
        <code>${escapeHtml(row.name)}</code>
        <p>${escapeHtml(row.detail)}</p>
      </li>
    `).join("");
    return `
      ${this.intro(`Every session is in exactly one of these. The first three mean something is
        still owed to you; the rest are final.`)}
      <ul class="harness-states">${rows}</ul>
    `;
  }
}
