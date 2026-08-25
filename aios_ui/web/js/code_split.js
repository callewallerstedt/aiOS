// CODE split view.
//
// The main detail pane stays exactly what it was -- full composer, context
// ring, telemetry, actions. This module adds up to two extra panes beside it
// so three sessions can render side by side, each with its own live SSE
// transcript and its own composer. One mental model:
//
//   [sessions rail] | [pane 0 = main detail] | [splitter] | [pane 1] ...
//
// Interaction rules:
//   - Clicking a session row loads it into the FOCUSED pane (the one last
//     clicked; initially the leftmost/main pane).
//   - Right-clicking a row offers "open in focused pane" / "open in a new
//     split pane" / "close all split panes".
//   - Session rows drag onto any pane to load there.
//   - Split-pane title blocks drag to reorder panes; "+" in the main card
//     header adds an empty pane.
//   - Splitters between panes resize them; layout persists in localStorage.

import { api, native, stream } from "./bridge.js";
import { escapeHtml } from "./markdown.js";
import { ACTIVE, Transcript, relativeTime } from "./transcript.js";
import { autosizePromptShell, promptConfigRowMarkup, promptShellMarkup } from "./chat_components.js";

const MAX_EXTRA_PANES = 2; // + the main pane = 3 side by side
const LAYOUT_KEY = "aios:code-split-layout";
const DRAG_TYPE = "text/code-session";
const PREFERRED_MIN_PANE_WIDTH = 150;

function cssPixels(value) {
  const parsed = Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

/** Measure the row in layout pixels (not zoom-scaled viewport pixels). */
function paneRowMetrics(rowEl) {
  const children = [...rowEl.children];
  const cards = children.filter((child) => child.classList.contains("code-detail"));
  const style = getComputedStyle(rowEl);
  const gap = cssPixels(style.columnGap || style.gap);
  const padding = cssPixels(style.paddingLeft) + cssPixels(style.paddingRight);
  const splitterWidth = children
    .filter((child) => child.classList.contains("code-pane-splitter"))
    .reduce((sum, child) => sum + child.offsetWidth, 0);
  return {
    children,
    cards,
    available: Math.max(0, rowEl.clientWidth - padding - splitterWidth - gap * Math.max(0, children.length - 1)),
  };
}

function shortText(value, limit = 84) {
  value = String(value || "").trim();
  return value.length > limit ? `${value.slice(0, limit - 1)}\u2026` : value;
}

function shortTitle(job) {
  const raw = String((job || {}).title || (job || {}).brief || "");
  return raw.replace(/^\s*[^\u00b7]{1,20}\u00b7\s*/, "") || "Untitled session";
}

function statusText(job) {
  if (!job) return "";
  return `${String(job.provider || "").toUpperCase()} \u00b7 ${job.status || "idle"} \u00b7 ${relativeTime(job.updated_at)}`;
}

function dotClass(status) {
  status = String(status || "").toLowerCase();
  if (status === "running") return "run";
  if (status === "queued") return "queue";
  if (status === "waiting_user" || status === "incomplete") return "wait";
  if (status === "completed") return "done";
  if (["failed", "error", "stopped", "interrupted"].includes(status)) return "err";
  return "";
}

export class CodeSplit {
  constructor(codeTab) {
    this.code = codeTab;
    this.panes = [];          // extra panes only; pane index 0 is the main detail
    this.activeIndex = 0;
    this.dragSessionId = null;
    this.restored = false;
    this.gone = false;

    this.rowEl = codeTab.el("pane-row");
    this.mainEl = codeTab.host.querySelector(".code-detail");
    this.mainTitleEl = codeTab.el("main-pane-title");
    this.addPaneEl = codeTab.el("add-pane");

    // Restore what was open last time once the job list arrives.
    try {
      const saved = JSON.parse(localStorage.getItem(LAYOUT_KEY) || "{}");
      this.pendingIds = Array.isArray(saved.ids)
        ? saved.ids.map((id) => (id == null ? null : String(id))).slice(0, MAX_EXTRA_PANES)
        : [];
      this.pendingWidths = Array.isArray(saved.widths) ? saved.widths.map(Number) : [];
    } catch {
      this.pendingIds = [];
      this.pendingWidths = [];
    }

    this.abort = new AbortController();
    const on = (target, type, handler, opts) =>
      target.addEventListener(type, handler, { signal: this.abort.signal, ...(opts || {}) });

    // Clicking anywhere inside a pane focuses it (so sidebar clicks land there).
    on(codeTab.host, "pointerdown", (event) => {
      const splitPane = event.target.closest("[data-split-pane]");
      if (splitPane) {
        this.setActive(Number(splitPane.dataset.splitPane) + 1);
        return;
      }
      if (event.target.closest(".code-detail")) this.setActive(0);
    });

    // Drop a dragged session onto the main pane -> select it there.
    this.bindDropTarget(this.mainEl, (id) => {
      if (this.activeIndex !== 0) this.setActive(0);
      this.code.select(id);
    });
    if (this.addPaneEl) on(this.addPaneEl, "click", () => {
      const pane = this.addEmptyPane();
      if (pane) this.setActive(this.panes.indexOf(pane) + 1);
    });

    on(document, "keydown", (event) => {
      if (event.key === "Escape") this.closeContextMenu();
    });
    on(document, "pointerdown", (event) => {
      if (!event.target.closest(".code-split-menu")) this.closeContextMenu();
      if (!event.target.closest(".code-composer")) this.closePaneMenus();
    });
    on(window, "resize", () => {
      this.fitPanes(); // keep everything inside the viewport when it shrinks
      this.syncTabs(); // keep integrated titles and controls in sync
      this.saveLayout();
    });

    this.renderTabs();
  }

  destroy() {
    this.gone = true;
    this.abort.abort();
    for (const pane of this.panes) this.teardownPane(pane);
    this.closeContextMenu();
  }

  // ------------------------------------------------------------- row routing

  /** Returns true when the sidebar click was consumed by a split pane. */
  routeRowActivate(jobId) {
    const id = String(jobId || "");
    if (!id) return false;
    const openIndex = this.panes.findIndex((pane) => String(pane.jobId) === id);
    if (openIndex >= 0) {
      // Already on screen -- focus that pane instead of duplicating it.
      this.setActive(openIndex + 1);
      return true;
    }
    if (this.activeIndex === 0) return false;   // leftmost pane: normal select()
    const pane = this.panes[this.activeIndex - 1];
    if (!pane) return false;
    this.loadInto(pane, id);
    return true;
  }

  setDragSession(id) {
    this.dragSessionId = id ? String(id) : null;
  }

  /** True when a session is rendered anywhere (main pane included). */
  isOpen(jobId) {
    const id = String(jobId || "");
    return String(this.code.selectedId) === id
      || this.panes.some((pane) => String(pane.jobId) === id);
  }

  // ------------------------------------------------------------------- panes

  addEmptyPane() {
    if (this.panes.length >= MAX_EXTRA_PANES) {
      this.code.shell.toast("Three sessions is the split-view limit.", "info");
      return null;
    }
    return this.createPane(null);
  }

  openInNewPane(jobId) {
    let pane = this.panes.find((candidate) => !candidate.jobId);
    if (!pane) pane = this.addEmptyPane();
    if (!pane) return;
    this.setActive(this.panes.indexOf(pane) + 1);
    this.loadInto(pane, jobId);
  }

  createPane(jobId) {
    const pane = {
      jobId: jobId ? String(jobId) : null,
      meta: null,
      since: 0,
      stream: null,
      view: null,
    };
    const el = document.createElement("section");
    el.className = "card code-detail code-split-pane empty";
    el.dataset.splitPane = String(this.panes.length);
    el.innerHTML = `
      <div class="code-detail-head split-head">
        <span class="pane-dot" hidden></span>
        <div class="title-block" draggable="true" title="Drag to reorder this pane">
          <span class="title-row">
            <span class="title">Split pane</span>
            <button class="pane-title-close" data-pane-act="close" title="Close this pane"
                    aria-label="Close this pane">&#x2715;</button>
            <button type="button" class="pane-raw-toggle" data-pane-act="raw"
                    aria-pressed="false" title="Show plain provider output and tool events">Raw</button>
          </span>
          <span class="meta">Drag a session here, or right-click one in the rail.</span>
        </div>
        <div class="actions" data-pane-actions></div>
      </div>
      <div class="code-transcript-wrap">
        <div class="code-transcript"></div>
        <button class="scroll-bottom" data-pane-act="jump">Jump to latest &darr;</button>
      </div>
      <div class="pane-placeholder">Pick a session<br><small>Click a session in the rail, or drag one here.</small></div>
      <div class="code-composer">
        <div class="prompt-menu prompt-plus-menu" data-pane-plus-menu hidden>
          <button type="button" class="prompt-menu-row" data-pmenu="attach">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21.4 11.6 12 21a6 6 0 0 1-8.5-8.5l9.4-9.4a4 4 0 0 1 5.7 5.7l-9.4 9.4a2 2 0 0 1-2.8-2.8l8.7-8.7"></path></svg>
            <span><strong>Attach</strong><small>Add a file to this message</small></span>
          </button>
          <button type="button" class="prompt-menu-row" data-pmenu="handoff">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17 1l4 4-4 4M3 11V9a4 4 0 0 1 4-4h14M7 23l-4-4 4-4m14-2v2a4 4 0 0 1-4 4H3"></path></svg>
            <span><strong>Handoff</strong><small>Pull in the latest external coding session</small></span>
          </button>
          <button type="button" class="prompt-menu-row" data-pmenu="queue-next">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"></path></svg>
            <span><strong>Queue next</strong><small>Run after the current turn</small></span>
          </button>
        </div>
        <div class="prompt-menu prompt-config-menu" data-pane-config-menu hidden>
          <div class="prompt-menu-label">Saved configurations</div>
          <div data-pane-config-list></div>
        </div>
        ${promptShellMarkup({
          shellAttr: 'data-pane-shell',
          plusAttr: 'data-pane-plus',
          inputAttr: 'data-pane-brief',
          configAttr: 'data-pane-config',
          configNameAttr: 'data-pane-config-name',
          dictateAttr: 'data-pane-dictate',
          sendAttr: 'data-pane-send',
          sendTitle: "Send to this session",
          placeholder: "Continue this session&hellip;",
        })}
      </div>
    `;
    const splitter = document.createElement("div");
    splitter.className = "code-pane-splitter";
    splitter.title = "Drag to resize";

    pane.el = el;
    pane.splitter = splitter;
    pane.brief = el.querySelector("[data-pane-brief]");
    pane.shell = el.querySelector("[data-pane-shell]");
    pane.titleBlockEl = el.querySelector(".title-block");
    pane.titleEl = el.querySelector(".title-block .title");
    pane.rawButton = el.querySelector('[data-pane-act="raw"]');
    pane.metaEl = el.querySelector(".title-block .meta");
    pane.dotEl = el.querySelector(".pane-dot");
    pane.actionsEl = el.querySelector("[data-pane-actions]");
    pane.plusMenu = el.querySelector("[data-pane-plus-menu]");
    pane.configMenu = el.querySelector("[data-pane-config-menu]");
    pane.configList = el.querySelector("[data-pane-config-list]");

    pane.view = new Transcript(el.querySelector(".code-transcript"), {
      jump: el.querySelector('[data-pane-act="jump"]'),
      isActive: () => ACTIVE.has(String((pane.meta || {}).status || "")),
      onReveal: (raw) => {
        const root = String((pane.meta || {}).cwd || "");
        const absolute = /^[a-zA-Z]:[\\/]|^\\\\/.test(raw) ? raw : `${root}\\${raw}`.replace(/\//g, "\\");
        native("open_path", absolute).then((ok) => {
          if (!ok) this.code.shell.toast(`Could not open ${absolute}`, "error");
        });
      },
      onReviewSuggest: (text) => {
        pane.brief.value = String(text || "");
        pane.brief.dispatchEvent(new Event("input", { bubbles: true }));
        pane.brief.focus();
        autosizePromptShell(pane.shell, pane.brief);
      },
      onAnswer: (answer) => this.answerPaneQuestion(pane, answer),
    });

    this.bindDropTarget(el, (id) => this.loadInto(pane, id));
    pane.titleBlockEl.addEventListener("dragstart", (event) => {
      const index = this.panes.indexOf(pane) + 1;
      if (index <= 0) return;
      event.dataTransfer.setData(DRAG_TYPE, `tab:${index}`);
      event.dataTransfer.effectAllowed = "move";
      pane.el.classList.add("dragging-pane");
    });
    pane.titleBlockEl.addEventListener("dragend", () => pane.el.classList.remove("dragging-pane"));
    pane.titleBlockEl.addEventListener("dragover", (event) => {
      if (event.dataTransfer.types.includes(DRAG_TYPE)) event.preventDefault();
    });
    pane.titleBlockEl.addEventListener("drop", (event) => {
      const payload = String(event.dataTransfer.getData(DRAG_TYPE) || "").trim();
      if (!payload.startsWith("tab:")) return;
      event.preventDefault();
      event.stopPropagation();
      this.movePane(Number(payload.slice(4)), this.panes.indexOf(pane) + 1);
    });

    el.addEventListener("click", (event) => {
      const pmenu = event.target.closest("[data-pmenu]");
      if (pmenu) {
        const action = pmenu.dataset.pmenu;
        this.closePaneMenus();
        if (action === "attach") this.code.shell.toast("File attachments are not wired up in this build yet.", "info");
        else if (action === "handoff") this.handoffToBrief(pane);
        else if (action === "queue-next") this.sendToPane(pane, "queue_next");
        return;
      }
      const act = event.target.closest("[data-pane-act]")?.dataset.paneAct;
      if (act === "close") this.closePane(pane);
      else if (act === "raw") {
        const enabled = pane.view.toggleRawMode();
        pane.rawButton.classList.toggle("active", enabled);
        pane.rawButton.setAttribute("aria-pressed", String(enabled));
        pane.rawButton.title = enabled ? "Show formatted transcript" : "Show plain provider output and tool events";
      }
      else if (act === "stop") this.stopPane(pane);
      else if (act === "jump") pane.view.setFollow(true);
      else if (act === "undo") this.undoPane(pane);
      else if (act === "delete") this.deletePane(pane);
      else if (act === "review") this.code.openSelfReview(pane.meta, (id) => this.loadInto(pane, id));
      else if (event.target.closest("[data-pane-send]")) this.sendToPane(pane);
      else if (event.target.closest("[data-pane-plus]")) this.togglePaneMenu(pane, "plus");
      else if (event.target.closest("[data-pane-config]")) this.togglePaneMenu(pane, "config");
      else if (event.target.closest('[data-code="models"]')) {
        this.closePaneMenus();
        this.code.openModels();
      }
      else if (event.target.closest("[data-pane-config-pill]")) {
        const pill = event.target.closest("[data-pane-config-pill]");
        this.closePaneMenus();
        this.applyPaneConfig(pane, pill.dataset.paneConfigPill);
      }
    });
    el.addEventListener("keydown", (event) => {
      if (event.target === pane.brief && event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        this.sendToPane(pane);
      }
    });
    pane.brief.addEventListener("input", () => autosizePromptShell(pane.shell, pane.brief));

    this.rowEl.appendChild(splitter);
    this.rowEl.appendChild(el);
    this.bindSplitter(splitter, pane);
    this.panes.push(pane);
    this.fitPanes(el); // shrink the others so the newcomer is fully visible

    if (jobId) this.connectPaneStream(pane);
    this.renumberPanes();
    this.renderTabs();
    this.saveLayout();
    return pane;
  }

  loadInto(pane, jobId) {
    const id = String(jobId || "");
    if (!id) return;
    this.code.unread.delete(id);
    this.code.dirtySessions = true;
    this.code.schedule();

    if (String(pane.jobId) === id) return;
    if (pane.stream) { pane.stream.close(); pane.stream = null; }
    pane.jobId = id;
    pane.meta = (this.code.jobs || []).find((job) => String(job.id) === id) || null;
    pane.since = 0;

    pane.el.classList.remove("empty");
    pane.view.reset();
    pane.view.pinToEnd();   // opening a session always lands on the latest message
    this.syncPaneHeader(pane);

    this.connectPaneStream(pane);
    this.renderTabs();
    this.saveLayout();
  }

  connectPaneStream(pane) {
    const id = String(pane.jobId);
    pane.stream = stream(
      () => `/sse/code/events?job=${encodeURIComponent(id)}&since=${pane.since}`,
      {
        events: (payload) => {
          if (this.gone || String(pane.jobId) !== id) return;
          pane.since = payload.size || pane.since;
          if (payload.job) { pane.meta = payload.job; this.syncPaneHeader(pane); }
          pane.view.push(payload.events);
        },
        reset: () => {
          if (this.gone || String(pane.jobId) !== id) return;
          pane.since = 0;
          pane.view.reset();
        },
        job: (payload) => {
          if (this.gone || String(pane.jobId) !== id) return;
          pane.meta = payload.job;
          this.syncPaneHeader(pane);
        },
      },
    );
  }

  closePane(pane) {
    const index = this.panes.indexOf(pane);
    if (index < 0) return;
    this.teardownPane(pane);
    pane.splitter.remove();
    pane.el.remove();
    this.panes.splice(index, 1);
    this.renumberPanes();
    this.fitPanes(); // let the survivors grow back into the freed space
    if (this.activeIndex > this.panes.length) this.activeIndex = this.panes.length;
    this.setActive(this.activeIndex);
    this.renderTabs();
    this.saveLayout();
  }

  closeAll() {
    for (const pane of [...this.panes]) this.closePane(pane);
  }

  teardownPane(pane) {
    if (pane.stream) { pane.stream.close(); pane.stream = null; }
    if (pane.view) { pane.view.destroy(); pane.view = null; }
  }

  renumberPanes() {
    this.panes.forEach((pane, index) => { pane.el.dataset.splitPane = String(index); });
  }

  /**
   * Re-fit every pane card into the available row width. Weights follow the
   * current widths (a freshly added card gets the average slice), so adding
   * the third pane shrinks the others instead of pushing it off-screen, and
   * closing one lets the rest grow back. Nothing ends up below MINW.
   */
  fitPanes(newCard = null) {
    if (this.gone || !this.rowEl || !this.rowEl.isConnected) return;
    const { cards, available } = paneRowMetrics(this.rowEl);
    if (!cards.length) return;

    // Back to a lone main pane: clear the frozen pixel width so it returns to
    // its natural fluid layout instead of a hardcoded size.
    if (!newCard && cards.length === 1) {
      cards[0].style.flex = "";
      return;
    }

    // offsetWidth and clientWidth use the same layout-pixel coordinate system.
    // getBoundingClientRect() includes the shell's user zoom, which made resize
    // math drift whenever aiOS was not at exactly 100% zoom.
    let weights = cards.map((card) => Math.max(card.offsetWidth, 1));
    if (newCard) {
      const existing = weights.filter((_, i) => cards[i] !== newCard);
      const avg = existing.length
        ? existing.reduce((sum, width) => sum + width, 0) / existing.length
        : 1;
      weights = weights.map((width, i) => (cards[i] === newCard ? avg : width));
    }
    let total = weights.reduce((sum, w) => sum + w, 0);
    weights = weights.map((w) => w * (available / (total || 1)));

    // Prefer readable panes, but never enforce a minimum wider than the actual
    // row can hold. All panes must remain visible even in a narrow window.
    const minimum = Math.min(PREFERRED_MIN_PANE_WIDTH, available / cards.length);
    let deficit = 0;
    let surplus = 0;
    weights.forEach((w) => {
      if (w < minimum) deficit += minimum - w;
      else surplus += w - minimum;
    });
    if (deficit > 0 && surplus > 0) {
      const take = Math.min(deficit, surplus);
      weights = weights.map((w) => {
        if (w <= minimum) return minimum;
        return w - take * ((w - minimum) / surplus);
      });
    } else if (deficit > 0) {
      weights = weights.map((w) => Math.max(minimum, w));
    }
    cards.forEach((card, i) => { card.style.flex = `0 1 ${weights[i].toFixed(3)}px`; });
    // Heal any sideways scroll drift (e.g. from a focus scroll before this
    // ran) so the first window's left border is never left clipped.
    if (this.rowEl.scrollLeft) this.rowEl.scrollLeft = 0;
    const panesEl = this.rowEl.parentElement;
    if (panesEl && panesEl.scrollLeft) panesEl.scrollLeft = 0;
  }

  syncPaneHeader(pane) {
    const job = pane.meta || (this.code.jobs || []).find((row) => String(row.id) === String(pane.jobId));
    if (!job) return;
    pane.titleEl.textContent = shortTitle(job);
    pane.titleEl.title = `${job.cwd || ""}`;
    pane.metaEl.textContent = statusText(job);
    const dot = dotClass(job.status);
    pane.dotEl.hidden = !dot;
    pane.dotEl.className = `pane-dot ${dot}`;
    this.renderPaneActions(pane, job);
  }

  /** Mirror of the main pane's header action row: review / undo / delete / stop. */
  renderPaneActions(pane, job) {
    const host = pane.actionsEl;
    if (!host) return;
    const active = ACTIVE.has(String((job || {}).status || ""));
    const reviewable = !active && job.session_kind !== "review";
    const undoable = Number(job.undoable_files || 0) > 0;
    host.innerHTML = `
      ${reviewable ? '<button class="btn compact accent" data-pane-act="review" title="Audit the complete session with another model configuration">Self review</button>' : ""}
      <button class="btn compact" data-pane-act="undo"${(!active && undoable) ? "" : " disabled"} title="Restore every file this session changed back to how it was before the agent edited it">Undo</button>
      <button class="btn compact ghost" data-pane-act="delete"${active ? " disabled" : ""}>Delete</button>
      <button class="btn compact" data-pane-act="stop"${active ? "" : " disabled"}>Stop</button>
    `;
  }

  /** Called by CodeTab whenever jobs or the selection changed. */
  syncTabs() {
    if (this.gone) return;
    const selectedJob = (this.code.jobs || []).find(
      (job) => String(job.id) === String(this.code.selectedId),
    );
    if (this.mainTitleEl) {
      this.mainTitleEl.textContent = selectedJob ? shortTitle(selectedJob) : "New session";
      this.mainTitleEl.title = String((selectedJob || {}).cwd || "");
    }
    if (this.addPaneEl) this.addPaneEl.hidden = this.panes.length >= MAX_EXTRA_PANES;
    for (const pane of this.panes) {
      this.syncPaneHeader(pane);
    }
  }

  // -------------------------------------------------------------------- tabs

  setActive(index) {
    this.activeIndex = Math.max(0, Math.min(index, this.panes.length));
    this.mainEl.classList.toggle("active-pane", this.activeIndex === 0);
    this.panes.forEach((pane, i) => pane.el.classList.toggle("active-pane", this.activeIndex === i + 1));
  }

  renderTabs() {
    this.syncTabs();
  }

  /** Move an extra pane from one visual position to another (main stays left). */
  movePane(from, to) {
    if (from === to || from <= 0 || to <= 0) return;
    const fromIdx = from - 1;
    const toIdx = to - 1;
    if (fromIdx >= this.panes.length || toIdx >= this.panes.length) return;
    const [pane] = this.panes.splice(fromIdx, 1);
    this.panes.splice(toIdx, 0, pane);
    // Re-append in order; each pane owns the splitter that sits before it.
    for (const p of this.panes) {
      this.rowEl.appendChild(p.splitter);
      this.rowEl.appendChild(p.el);
    }
    this.renumberPanes();
    this.setActive(to);
    this.renderTabs();
    this.saveLayout();
  }

  // --------------------------------------------------------------- splitters

  bindSplitter(splitter, pane) {
    splitter.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      splitter.setPointerCapture(event.pointerId);
      const leftEl = splitter.previousElementSibling;
      const rightEl = pane.el;
      const leftStart = leftEl.offsetWidth;
      const rightStart = rightEl.offsetWidth;
      const startX = event.clientX;
      const rowRectWidth = this.rowEl.getBoundingClientRect().width;
      const scaleX = this.rowEl.offsetWidth > 0 ? rowRectWidth / this.rowEl.offsetWidth : 1;
      const pairTotal = leftStart + rightStart;
      const minimum = Math.min(PREFERRED_MIN_PANE_WIDTH, pairTotal / 2);
      const move = (moveEvent) => {
        const dx = (moveEvent.clientX - startX) / (scaleX || 1);
        const leftW = Math.min(Math.max(minimum, leftStart + dx), pairTotal - minimum);
        const rightW = pairTotal - leftW;
        leftEl.style.flex = `0 1 ${leftW.toFixed(3)}px`;
        rightEl.style.flex = `0 1 ${rightW.toFixed(3)}px`;
      };
      const up = () => {
        splitter.removeEventListener("pointermove", move);
        splitter.removeEventListener("pointerup", up);
        splitter.removeEventListener("pointercancel", up);
        this.syncTabs(); // tabs must keep sitting over their windows
        this.saveLayout();
      };
      splitter.addEventListener("pointermove", move);
      splitter.addEventListener("pointerup", up);
      splitter.addEventListener("pointercancel", up);
    });
  }

  bindDropTarget(el, apply) {
    if (!el) return;
    el.addEventListener("dragover", (event) => {
      if (event.dataTransfer.types.includes(DRAG_TYPE)) {
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
        el.classList.add("drop-target");
      }
    });
    el.addEventListener("dragleave", () => el.classList.remove("drop-target"));
    el.addEventListener("drop", (event) => {
      el.classList.remove("drop-target");
      const payload = String(event.dataTransfer.getData(DRAG_TYPE) || "").trim();
      if (!payload || payload.startsWith("tab:")) return;
      event.preventDefault();
      apply(payload);
    });
  }

  // ------------------------------------------------------------ pane actions

  async sendToPane(pane, delivery = "") {
    const text = String(pane.brief.value || "").trim();
    if (!text || !pane.jobId) return;
    const sendButton = pane.el.querySelector("[data-pane-send]");
    if (sendButton) sendButton.disabled = true;
    try {
      const status = String((pane.meta || {}).status || "").toLowerCase();
      const deliveryMode = delivery === "queue_next"
        ? "queue_next"
        : ((status === "running" || status === "queued") ? "steer_now" : "continue");
      const result = await api(`/api/code/jobs/${encodeURIComponent(pane.jobId)}/messages`, {
        method: "POST",
        body: {
          text,
          standing_prompt: this.code.standingPrompt,
          urgent: deliveryMode === "steer_now",
          strategy: "auto",
        },
      });
      if (!result || result.ok === false) {
        this.code.shell.toast((result && result.error) || "Could not deliver the message.", "error");
        return;
      }
      if (result.job) { pane.meta = result.job; this.syncPaneHeader(pane); }
      pane.brief.value = "";
      pane.brief.style.height = "auto";
      pane.shell?.classList.remove("expanded");
      pane.view.setFollow(true);
    } finally {
      if (sendButton) sendButton.disabled = false;
    }
  }

  async stopPane(pane) {
    if (!pane.jobId) return;
    await api(`/api/code/jobs/${encodeURIComponent(pane.jobId)}/stop`, { method: "POST" });
  }

  /** Undo every file change this pane's session made (mirrors CodeTab.undoJob). */
  async undoPane(pane) {
    if (!pane.jobId || this.gone) return;
    const job = pane.meta || {};
    const count = Number(job.undoable_files || 0);
    const detail = count > 0
      ? `This restores ${count} file${count === 1 ? "" : "s"} to how ${count === 1 ? "it was" : "they were"} before the agent edited ${count === 1 ? "it" : "them"}.`
      : "No file changes were recorded for this session, so nothing should change.";
    const confirmed = await this.code.shell.confirm("Undo this session's file changes?", detail);
    if (this.gone || !confirmed) return;
    const result = await api(`/api/code/jobs/${encodeURIComponent(pane.jobId)}/undo`, {
      method: "POST",
      body: { confirm: true },
    });
    if (this.gone) return;
    if (!result || result.ok === false) {
      this.code.shell.toast((result && result.error) || "Undo failed.", "error");
      return;
    }
    if (result.job) { pane.meta = result.job; this.syncPaneHeader(pane); }
    const restored = Number((result.restored_files != null) ? result.restored_files : count);
    this.code.shell.toast(`Restored ${restored} file${restored === 1 ? "" : "s"} in this split pane.`, "success");
  }

  /** Delete the pane's session and close the pane (mirrors CodeTab.deleteJob). */
  async deletePane(pane) {
    if (!pane.jobId || this.gone) return;
    const job = pane.meta || {};
    const confirmed = await this.code.shell.confirm(
      "Delete this session?",
      `${shortTitle(job)}\n\nThis removes the transcript and marks the workspace clean. The files stay as they are.`,
    );
    if (this.gone || !confirmed) return;
    const result = await api(`/api/code/jobs/${encodeURIComponent(pane.jobId)}`, { method: "DELETE" });
    if (this.gone) return;
    if (!result || result.ok === false) {
      this.code.shell.toast((result && result.error) || "Delete failed.", "error");
      return;
    }
    this.closePane(pane);
    this.code.dirtySessions = true;
    this.code.schedule();
  }

  // ------------------------------------------------------- per-pane composer

  closePaneMenus() {
    for (const pane of this.panes) {
      if (pane.plusMenu) pane.plusMenu.hidden = true;
      if (pane.configMenu) pane.configMenu.hidden = true;
      pane.el.querySelector("[data-pane-plus]")?.setAttribute("aria-expanded", "false");
      pane.el.querySelector("[data-pane-config]")?.setAttribute("aria-expanded", "false");
    }
  }

  togglePaneMenu(pane, which) {
    const target = which === "config" ? pane.configMenu : pane.plusMenu;
    const wasOpen = target ? !target.hidden : false;
    this.closePaneMenus();
    if (!target || wasOpen) return;
    if (which === "config") this.renderPaneConfigMenu(pane);
    target.hidden = false;
    pane.el.querySelector(which === "config" ? "[data-pane-config]" : "[data-pane-plus]")
      ?.setAttribute("aria-expanded", "true");
  }

  renderPaneConfigMenu(pane) {
    if (!pane.configList) return;
    const configs = (this.code.modelConfigs || []).filter((config) => config.show_in_composer !== false);
    const activeId = String((pane.meta || {}).config_id || "");
    const selected = configs.find((config) => String(config.id) === activeId);
    const nameEl = pane.el.querySelector("[data-pane-config-name]");
    if (nameEl) nameEl.textContent = (selected || configs[0] || {}).name || "Configuration";
    pane.configList.innerHTML = configs.length
      ? configs.map((config) => {
          const coder = (config.roles || {}).coder || {};
          const meta = config.description || coder.model || config.provider || "Coder-led automatic delegation";
          return promptConfigRowMarkup({
            label: escapeHtml(config.name || "Untitled"),
            hint: escapeHtml(shortText(meta, 58)),
            selected: String(config.id) === activeId,
            attrs: `data-pane-config-pill="${escapeHtml(config.id)}"`,
          });
        }).join("")
      : '<button type="button" class="prompt-config-row empty" data-code="models"><span><strong>Save a configuration</strong><small>Choose models and reasoning for each role</small></span></button>';
  }

  /** Apply a saved configuration to this pane's session only. */
  async applyPaneConfig(pane, configId) {
    if (!pane.jobId || this.gone) return;
    const config = (this.code.modelConfigs || []).find((row) => String(row.id) === String(configId));
    if (!config) return;
    const provider = String(config.provider || "openrouter");
    const choice = this.code.configurationCoderChoice(provider, config.roles || {});
    try {
      const result = await api(`/api/code/jobs/${encodeURIComponent(pane.jobId)}/configuration`, {
        method: "POST",
        body: {
          provider,
          model: choice.model,
          reasoning: choice.reasoning,
          fast: choice.fast,
          roles: config.roles || {},
          config_id: String(config.id || ""),
          config_name: config.name || "",
        },
      });
      if (this.gone) return;
      if (!result || result.ok === false) {
        this.code.shell.toast((result && result.error) || "Could not apply the configuration.", "error");
        return;
      }
      if (result.job) { pane.meta = result.job; this.syncPaneHeader(pane); }
      this.code.shell.toast(`Applied "${config.name}" to this split pane.`, "success");
    } catch (error) {
      if (!this.gone) this.code.shell.toast(error.message || "Could not apply the configuration.", "error");
    }
  }

  /** Paste a Claude Code / Codex handoff brief into this pane's composer. */
  async handoffToBrief(pane) {
    const list = await api("/api/handoff");
    if (this.gone) return;
    if (!list || typeof list !== "object") {
      this.code.shell.toast("Could not read handoff sessions.", "error");
      return;
    }
    const sessions = Object.values(list).filter((session) => session && session.path);
    if (!sessions.length) {
      this.code.shell.toast("No Claude Code or Codex sessions found yet.", "info");
      return;
    }
    const rows = sessions.map((session) => [
      `${session.tool}\u00b7 ${session.title || "untitled"}`,
      `${(session.user_count || 0) + (session.assistant_count || 0)} turns`,
    ]);
    const pick = await this.code.shell.pick("Handoff session", rows, "Pick a session to paste into the brief.");
    if (this.gone || pick == null) return;
    const chosen = sessions[pick];
    const result = await api("/api/handoff", { method: "POST", body: { tool: chosen.tool, path: chosen.path } });
    if (this.gone) return;
    if (!result || result.ok === false) {
      this.code.shell.toast((result && result.error) || "Could not build the handoff brief.", "error");
      return;
    }
    pane.brief.value = result.brief || "";
    pane.brief.dispatchEvent(new Event("input", { bubbles: true }));
    pane.brief.focus();
    this.code.shell.toast(`Handoff pasted from ${chosen.tool}.`, "success");
  }

  async answerPaneQuestion(pane, response) {
    if (!pane.jobId) return;
    const payload = response && typeof response === "object" ? response : { text: String(response || ""), answers: {} };
    const result = await api(`/api/code/jobs/${encodeURIComponent(pane.jobId)}/messages`, {
      method: "POST",
      body: {
        text: String(payload.text || ""),
        standing_prompt: this.code.standingPrompt,
        question_answers: payload.answers || {},
        urgent: true,
        strategy: "auto",
      },
    });
    if (!result?.ok) throw new Error(result?.error || "Could not send answers.");
    return result;
  }

  // ------------------------------------------------------------ context menu

  showContextMenu(x, y, jobId) {
    this.closeContextMenu();
    const maxed = this.panes.length >= MAX_EXTRA_PANES;
    const menu = document.createElement("div");
    menu.className = "code-split-menu";
    menu.innerHTML = `
      <button type="button" data-menu="focus">Open in focused pane</button>
      <button type="button" data-menu="new"${maxed ? " disabled" : ""}>Open in new split pane${maxed ? " (limit reached)" : ""}</button>
      <div class="menu-separator"></div>
      <button type="button" data-menu="close-all"${this.panes.length ? "" : " disabled"}>Close all split panes</button>
    `;
    menu.style.left = `${Math.min(x, window.innerWidth - 230)}px`;
    menu.style.top = `${Math.min(y, window.innerHeight - 130)}px`;
    menu.addEventListener("click", (event) => {
      const action = event.target.closest("[data-menu]")?.dataset.menu;
      if (!action) return;
      this.closeContextMenu();
      if (action === "focus") {
        if (!this.routeRowActivate(jobId)) this.code.select(jobId);
      } else if (action === "new") {
        this.openInNewPane(jobId);
      } else if (action === "close-all") {
        this.closeAll();
      }
    });
    document.body.appendChild(menu);
    this.menuEl = menu;
  }

  closeContextMenu() {
    if (this.menuEl) { this.menuEl.remove(); this.menuEl = null; }
    this.closePaneMenus();
  }

  // ------------------------------------------------------------- persistence

  restorePending() {
    if (this.restored) return;
    this.restored = true;
    const jobs = this.code.jobs || [];
    // Null slots are intentional empty panes. Keeping them preserves both the
    // pane count and width-to-pane alignment across UI reloads.
    const ids = this.pendingIds.filter(
      (id) => id == null || jobs.some((job) => String(job.id) === id),
    );
    for (const id of ids) {
      const pane = this.createPane(id);
      pane.meta = jobs.find((job) => String(job.id) === id) || null;
      this.syncPaneHeader(pane);
    }
    if (ids.length) this.renderTabs();
    // Restore the stored shape, then let fitPanes clamp/scale it to whatever
    // fits the current window so a smaller viewport never overflows.
    if (this.pendingWidths.length >= ids.length + 1 && ids.length) {
      const mainW = Math.round(this.pendingWidths[0]);
      if (mainW > 100) this.mainEl.style.flex = `0 0 ${mainW}px`;
      this.panes.forEach((pane, i) => {
        const w = Math.round(this.pendingWidths[i + 1]);
        if (w > 100) pane.el.style.flex = `0 0 ${w}px`;
      });
      requestAnimationFrame(() => {
        this.fitPanes();
        this.syncTabs();
      });
    }
    this.pendingIds = [];
    this.pendingWidths = [];
  }

  saveLayout() {
    if (this.gone || !this.mainEl.isConnected) return;
    try {
      const widths = [
        this.mainEl.getBoundingClientRect().width,
        ...this.panes.map((pane) => pane.el.getBoundingClientRect().width),
      ];
      localStorage.setItem(LAYOUT_KEY, JSON.stringify({
        ids: this.panes.map((pane) => pane.jobId),
        widths,
      }));
    } catch { /* storage unavailable -- layout just will not persist */ }
  }
}
