// Shell: theme, nav, tab lifecycle, and the small native affordances (window
// controls, folder picker, confirm, toast) the Tk build got from tkinter.

import { api, authenticatedUrl, native } from "./bridge.js";
import { BenchTab } from "./bench.js";
import { ChatPanel, bindChatResize } from "./chat.js";
import { CodeTab } from "./code.js";
import { HarnessTab } from "./harness.js";
import { SettingsTab } from "./settings.js";

const TABS = [
  ["Dashboard", "Dashboard"],
  ["Projects", "Projects"],
  ["CODE", "CODE"],
  ["Apps", "Apps"],
  ["Drop", "Drop"],
  ["AI Operator", "OPERATOR"],
  ["Settings", "Settings"],
];

// Pages reached from inside CODE rather than from the rail: BENCH measures the
// harness, HARNESS explains it. Neither has its own nav button, and neither is
// somewhere aiOS should reopen on.
const CODE_PAGES = new Set(["BENCH", "HARNESS"]);
const PHONE_MIRROR = new URLSearchParams(location.search).get("phone") === "1";

// Tabs still on the Tk build. Listed explicitly so the nav never silently
// drops one -- each is wired in a later step of the rebuild.
const PENDING = {
  Dashboard: "Notes, weather, markets, GPU and usage tiles.",
  Projects: "Project list, detail, settings and the file tree.",
  Apps: "Start-menu discovery, search and launching.",
  Drop: "Drag-and-drop import into the active project.",
  "AI Operator": "The agent that drives your mouse and keyboard.",
};

class Shell {
  constructor() {
    this.page = document.getElementById("page");
    this.nav = document.getElementById("nav");
    this.active = null;
    this.tab = null;
    this.config = {};
    this.phoneMirror = PHONE_MIRROR;
    this.pageHistory = [];
    this.pageHistoryIndex = -1;
    this.uiPreferences = {};
    this.uiPreferencesSaveTimer = null;
  }

  readUiPreferences() {
    const saved = this.config.ui_preferences && typeof this.config.ui_preferences === "object"
      ? this.config.ui_preferences : {};
    const localZoom = Number(localStorage.getItem("aios:zoom"));
    const savedZoom = Number(saved.zoom);
    const zoom = Number.isFinite(savedZoom) ? savedZoom : localZoom;
    const readBool = (key, localKey) => saved[key] == null
      ? localStorage.getItem(localKey) === "true" : saved[key] === true;
    this.uiPreferences = {
      zoom: Number.isFinite(zoom) ? Math.max(0.4, Math.min(2.0, zoom)) : 1.0,
      nav_collapsed: readBool("nav_collapsed", "aios:nav-collapsed"),
      chat_collapsed: readBool("chat_collapsed", "aios:chat-collapsed"),
    };
  }

  saveUiPreferences() {
    localStorage.setItem("aios:zoom", String(this.uiPreferences.zoom));
    localStorage.setItem("aios:nav-collapsed", String(this.uiPreferences.nav_collapsed));
    localStorage.setItem("aios:chat-collapsed", String(this.uiPreferences.chat_collapsed));
    clearTimeout(this.uiPreferencesSaveTimer);
    this.uiPreferencesSaveTimer = setTimeout(() => {
      api("/api/config", {
        method: "POST",
        body: { patch: { ui_preferences: this.uiPreferences } },
        timeout: 3000,
      }).catch((error) => console.warn("Could not save aiOS UI preferences", error));
    }, 250);
  }

  updateCollapseButton(button, collapsed, expandedLabel, collapsedLabel, expandedGlyph, collapsedGlyph) {
    const label = collapsed ? collapsedLabel : expandedLabel;
    button.setAttribute("aria-expanded", String(!collapsed));
    button.title = label;
    button.querySelector(".sr-only").textContent = label;
    button.querySelector(".collapse-glyph").textContent = collapsed ? collapsedGlyph : expandedGlyph;
  }

  /**
   * Make breakage loud.
   *
   * Twice now a single null lookup has taken out a whole feature with no
   * visible sign -- the sessions just looked empty. An uncaught error should
   * announce itself rather than leave the UI quietly wrong.
   */
  watchForErrors() {
    const report = (what) => {
      console.error(what);
      this.toast(`UI error: ${String(what && what.message || what)}`, "error");
    };
    window.addEventListener("error", (event) => report(event.error || event.message));
    window.addEventListener("unhandledrejection", (event) => report(event.reason));
  }

  async boot() {
    this.watchForErrors();
    document.documentElement.classList.toggle("phone-mirror", this.phoneMirror);
    // The chrome and active tab must render even when config recovery or a
    // first-import backend call is unhealthy.  Before this timeout, one stuck
    // request left a perfectly good WebView as an empty panel forever.
    const result = await api("/api/config", { timeout: 5000 });
    if (result.ok) {
      this.config = result.config;
      this.applyTheme(result.config.theme || {}, result.config);
    } else {
      console.error("aiOS config unavailable during boot", result && result.error);
    }
    // Chrome is decoration; the tab is the app. One missing button must never
    // stop the content from rendering -- that failure mode cost a whole
    // session list once already.
    for (const step of [this.buildNav, this.bindChrome, this.mountChat, this.bindNativeMirrors, this.renderSubtitle, this.applyRadius, this.applyWindow]) {
      try {
        step.call(this);
      } catch (error) {
        console.error(`aiOS chrome step failed: ${step.name}`, error);
      }
    }
    this.show(this.phoneMirror ? "CODE" : (this.config.active_tab || "CODE"));
  }

  /**
   * The agent chat lives outside the tab lifecycle on purpose.
   *
   * It is a view of one long-running conversation; tearing it down on every tab
   * switch would drop the stream and the scroll position mid-answer. It stays
   * mounted, and only the page area swaps.
   */
  mountChat() {
    this.chat = new ChatPanel(document.getElementById("chat-inner"), this);
    if (!this.phoneMirror) {
      bindChatResize(document.getElementById("chat-panel"), document.getElementById("chat-resize"));
    }
  }

  /** Window-level theme settings CSS cannot reach: alpha and always-on-top. */
  applyWindow() {
    if (this.phoneMirror) return;
    const theme = this.config.theme || {};
    if (theme.opacity != null) native("set_opacity", Math.round(Number(theme.opacity) * 100));
    native("set_always_on_top", theme.always_on_top === true);
  }

  applyRadius() {
    const radius = Math.max(0, Number((this.config.theme || {}).radius ?? 28));
    document.documentElement.style.setProperty("--radius", `${radius}px`);
    document.documentElement.style.setProperty("--global-radius", `${radius}px`);
  }

  /** Settings -> Appearance still drives every colour, straight into CSS vars. */
  applyTheme(theme, config) {
    const root = document.documentElement.style;
    const cssNames = {
      app_background: "app-background",
      code_chat_background: "code-chat-background",
      code_sidebar_background: "code-sidebar-background",
    };
    for (const key of [
      "accent", "panel", "panel2", "surface", "surface2", "text", "muted", "danger", "success",
      "app_background", "code_chat_background", "code_sidebar_background", "chat_link",
    ]) {
      if (theme[key]) root.setProperty(`--${cssNames[key] || key}`, theme[key]);
    }
    if (theme.radius != null) {
      const radius = Math.max(0, Number(theme.radius) || 0);
      root.setProperty("--radius", `${radius}px`);
      root.setProperty("--global-radius", `${radius}px`);
    }
    if (theme.font_size) root.setProperty("--font-size", `${theme.font_size}px`);
  }

  /** The header breadcrumb: project root, chat model, codex model. */
  renderSubtitle() {
    const config = this.config;
    const parts = [
      config.project_root,
      config.chat_model ? `Chat ${config.chat_model}` : "",
      config.codex_model ? `Codex ${config.codex_model}` : "",
    ].filter(Boolean);
    document.getElementById("subtitle").textContent = parts.join("   |   ");
  }

  buildNav() {
    // Replace ONLY the tab buttons.
    //
    // The nav also holds the collapse button, and blowing away innerHTML took
    // it with them -- bindChrome then hit a null and boot() died before show()
    // ever ran, which is why the whole app came up empty. Anything else living
    // in the rail survives now.
    for (const stale of this.nav.querySelectorAll("[data-tab]")) stale.remove();
    const fragment = document.createDocumentFragment();
    for (const [id, label] of TABS) {
      const button = document.createElement("button");
      button.className = `nav-btn${id === "AI Operator" ? " brand-font" : ""}`;
      button.dataset.tab = id;
      button.textContent = label;
      fragment.appendChild(button);
    }
    // Before the spacer, so Quick tools stays pinned to the bottom of the rail.
    this.nav.insertBefore(fragment, document.getElementById("nav-spacer"));

    if (this.navBound) return;
    this.navBound = true;
    this.nav.addEventListener("click", (event) => {
      const button = event.target.closest("[data-tab]");
      if (button) this.show(button.dataset.tab);
    });
  }

  bindChrome() {
    document.querySelector(".header-tools").addEventListener("click", (event) => {
      const button = event.target.closest("[data-action]");
      if (!button) return;
      native(button.dataset.action);
    });

    const balance = document.getElementById("openrouter-balance");
    balance.addEventListener("click", () => this.syncOpenRouterBalance(true));
    this.syncOpenRouterBalance();
    this.balanceTimer = window.setInterval(() => this.syncOpenRouterBalance(), 60000);

    // WebView2's document history does not know about aiOS's in-page tabs.
    // Capture mouse X1/X2 and drive the shell's own navigation history so the
    // buttons work between every tab, including CODE, BENCH, and HARNESS.
    const mouseNavigation = (event) => {
      if (event.button !== 3 && event.button !== 4) return;
      event.preventDefault();
      event.stopPropagation();
      if (event.type === "mouseup") this.goPageHistory(event.button === 3 ? -1 : 1);
    };
    for (const type of ["mousedown", "mouseup", "auxclick"]) {
      window.addEventListener(type, mouseNavigation, { capture: true });
    }

    // Ctrl+Scroll to zoom (like Chrome).
    this.readUiPreferences();
    this.zoomLevel = this.uiPreferences.zoom;
    document.querySelector(".panel").style.zoom = String(this.zoomLevel);
    document.querySelector(".panel").addEventListener("wheel", (event) => {
      if (!event.ctrlKey) return;
      event.preventDefault();
      const delta = event.deltaY > 0 ? -0.08 : 0.08;
      this.zoomLevel = Math.max(0.4, Math.min(2.0, this.zoomLevel + delta));
      document.querySelector(".panel").style.zoom = String(this.zoomLevel);
      this.uiPreferences.zoom = this.zoomLevel;
      this.saveUiPreferences();
    }, { passive: false });

    this.buildQuickTools();

    // Collapse sidebar
    const nav = document.getElementById("nav");
    document.getElementById("nav-collapse").addEventListener("click", () => {
      nav.classList.toggle("collapsed");
      const collapsed = nav.classList.contains("collapsed");
      this.uiPreferences.nav_collapsed = collapsed;
      this.updateCollapseButton(document.getElementById("nav-collapse"), collapsed,
        "Collapse navigation", "Expand navigation", "‹", "›");
      this.saveUiPreferences();
    });
    if (this.uiPreferences.nav_collapsed) {
      nav.classList.add("collapsed");
    }
    this.updateCollapseButton(document.getElementById("nav-collapse"), this.uiPreferences.nav_collapsed,
      "Collapse navigation", "Expand navigation", "‹", "›");

    // Collapse chat panel
    const chatPanel = document.getElementById("chat-panel");
    const chatCollapse = document.getElementById("chat-collapse");
    if (this.phoneMirror) {
      chatCollapse.title = "Close agent chat";
      chatCollapse.querySelector(".collapse-glyph").textContent = "×";
      chatCollapse.querySelector(".sr-only").textContent = "Close agent chat";
    }
    chatCollapse.addEventListener("click", () => {
      if (this.phoneMirror) {
        document.documentElement.classList.remove("phone-agent-open");
        return;
      }
      chatPanel.classList.toggle("collapsed");
      const collapsed = chatPanel.classList.contains("collapsed");
      this.uiPreferences.chat_collapsed = collapsed;
      this.updateCollapseButton(chatCollapse, collapsed, "Collapse agent panel", "Expand agent panel", "›", "‹");
      this.saveUiPreferences();
    });
    if (this.uiPreferences.chat_collapsed) {
      chatPanel.classList.add("collapsed");
    }
    this.updateCollapseButton(chatCollapse, this.uiPreferences.chat_collapsed,
      "Collapse agent panel", "Expand agent panel", "›", "‹");
    document.getElementById("phone-agent-backdrop")?.addEventListener("click", () => {
      document.documentElement.classList.remove("phone-agent-open");
    });
    // A frameless window has no OS resize border, so the grip drives
    // window.resize() directly. Deltas come from screenX/Y because the page
    // itself is being resized underneath the cursor.
    document.getElementById("resize-grip").addEventListener("mousedown", (event) => {
      event.preventDefault();
      const origin = { x: event.screenX, y: event.screenY, w: window.outerWidth, h: window.outerHeight };
      let pending = null;

      const onMove = (move) => {
        pending = { w: origin.w + (move.screenX - origin.x), h: origin.h + (move.screenY - origin.y) };
        if (this.resizeFrame) return;
        // Coalesce to one native call per frame; mousemove fires far faster.
        this.resizeFrame = requestAnimationFrame(() => {
          this.resizeFrame = null;
          native("resize_window", pending.w, pending.h);
        });
      };
      const onUp = () => {
        window.removeEventListener("mousemove", onMove);
        window.removeEventListener("mouseup", onUp);
      };
      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    });
  }

  async syncOpenRouterBalance(force = false) {
    const node = document.getElementById("openrouter-balance");
    if (!node || node.classList.contains("loading")) return;
    node.classList.add("loading");
    node.setAttribute("aria-busy", "true");
    const result = await api(`/api/openrouter/balance${force ? "?refresh=1" : ""}`, { timeout: 15000 });
    node.classList.remove("loading");
    node.removeAttribute("aria-busy");
    if (!result || result.ok === false || !Number.isFinite(Number(result.balance))) {
      node.textContent = "OpenRouter --";
      node.classList.add("unavailable");
      node.title = `Balance unavailable: ${(result && result.error) || "unknown error"}. Click to retry.`;
      return;
    }
    const money = (value) => new Intl.NumberFormat("en-US", {
      style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2,
    }).format(Number(value));
    node.textContent = `OpenRouter ${money(result.balance)}`;
    node.title = `${money(result.balance)} remaining · ${money(result.total_credits)} purchased · ${money(result.total_usage)} used. Click to refresh.`;
    node.classList.remove("unavailable");
  }

  goPageHistory(delta) {
    const targetIndex = this.pageHistoryIndex + delta;
    if (targetIndex < 0 || targetIndex >= this.pageHistory.length) return;
    this.pageHistoryIndex = targetIndex;
    this.show(this.pageHistory[targetIndex], { history: false });
  }

  bindNativeMirrors() {
    if (!this.phoneMirror) return;
    const rail = document.getElementById("phone-native-rail");
    const viewer = document.getElementById("phone-native-viewer");
    const frame = document.getElementById("phone-native-frame");
    const name = document.getElementById("phone-native-name");
    const title = document.getElementById("phone-native-title");
    const message = document.getElementById("phone-native-message");
    let selected = "aios";
    let frameTimer = 0;
    let statusTimer = 0;

    const stopFrames = () => {
      clearTimeout(frameTimer);
      frameTimer = 0;
      frame.removeAttribute("src");
    };
    const nextFrame = () => {
      clearTimeout(frameTimer);
      if (selected === "aios") return;
      frameTimer = window.setTimeout(() => {
        frame.src = authenticatedUrl(`/native/frame/${selected}?frame=${Date.now()}`);
      }, 650);
    };
    frame.addEventListener("load", () => {
      frame.hidden = false;
      message.hidden = true;
      nextFrame();
    });
    frame.addEventListener("error", () => {
      frame.hidden = true;
      message.hidden = false;
      message.textContent = `Open ${name.textContent} on the PC to monitor it.`;
      nextFrame();
    });

    const select = (app) => {
      selected = app;
      for (const button of rail.querySelectorAll("[data-native-app]")) {
        button.classList.toggle("active", button.dataset.nativeApp === app);
      }
      document.documentElement.classList.toggle("phone-native-open", app !== "aios");
      stopFrames();
      if (app === "aios") return;
      const button = rail.querySelector(`[data-native-app="${app}"]`);
      name.textContent = button ? button.dataset.nativeLabel : app;
      title.textContent = "Native PC window";
      message.textContent = "Connecting to the PC window…";
      message.hidden = false;
      frame.hidden = true;
      frame.src = authenticatedUrl(`/native/frame/${app}?frame=${Date.now()}`);
    };
    rail.addEventListener("click", (event) => {
      const button = event.target.closest("[data-native-app]");
      if (button) select(button.dataset.nativeApp);
    });

    const refreshAvailability = async () => {
      const result = await api("/api/native/apps", { timeout: 4000 });
      if (result.ok) {
        for (const app of result.apps || []) {
          const button = rail.querySelector(`[data-native-app="${app.id}"]`);
          if (!button) continue;
          button.classList.toggle("unavailable", !app.available);
          button.title = app.available && app.title ? `${app.label}: ${app.title}` : app.label;
          if (selected === app.id && app.title) title.textContent = app.title;
        }
      }
      clearTimeout(statusTimer);
      statusTimer = window.setTimeout(refreshAvailability, 3500);
    };
    viewer.addEventListener("contextmenu", (event) => event.preventDefault());
    refreshAvailability();
  }

  /**
   * Quick Tools, as a nav entry rather than the old full-width tray.
   *
   * The tray took a strip of every screen and a click to peek at; the same five
   * tools now sit behind one rail button at the bottom of the nav, alongside
   * CODE / Apps / Drop / OPERATOR, and open in a popover next to it.
   */
  buildQuickTools() {
    const tools = [
      ["⇄", "Phone to PC", "Scan QR, shoot, auto-send", "phone_photos"],
      ["◉", "Webcam snap", "Snap, copy, paste at cursor", "webcam_snap"],
      ["V", "Paste image", "Save the clipboard image", "paste_image"],
      ["●", "Record screen", "Area, window, or monitor", "record_screen"],
      ["▶", "Recordings", "Browse your saved videos", "recordings"],
      ["↓", "Downloads", "Open your downloads folder", "downloads"],
    ];
    const panel = document.getElementById("quick-tools");
    const button = document.getElementById("nav-tools");
    const status = document.getElementById("quick-tools-status");
    document.getElementById("quick-tools-list").innerHTML = tools.map(([glyph, label, detail, action]) => `
      <button class="quick-tool" data-tool="${action}">
        <span class="glyph">${glyph}</span>
        <span class="copy"><b>${label}</b><i>${detail}</i></span>
      </button>
    `).join("");
    this.recordTool = document.querySelector('[data-tool="record_screen"]');

    const place = () => {
      // Anchored to the rail button, so it follows the collapsed rail too.
      const rect = button.getBoundingClientRect();
      panel.style.left = `${Math.round(rect.right + 8)}px`;
      panel.style.bottom = `${Math.round(window.innerHeight - rect.bottom)}px`;
    };
    const close = () => {
      panel.hidden = true;
      button.classList.remove("active");
    };
    this.closeQuickTools = close;
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      if (!panel.hidden) { close(); return; }
      status.textContent = "Ready";
      panel.hidden = false;
      button.classList.add("active");
      place();
    });
    window.addEventListener("resize", () => { if (!panel.hidden) place(); });
    document.addEventListener("click", (event) => {
      if (!panel.hidden && !panel.contains(event.target)) close();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") close();
    });

    panel.addEventListener("click", async (event) => {
      const tool = event.target.closest("[data-tool]");
      if (!tool) return;
      if (tool.dataset.tool === "record_screen") {
        await this.openScreenRecorder();
        return;
      }
      if (tool.dataset.tool === "webcam_snap") {
        close();
        this.openWebcamSnap();
        return;
      }
      status.textContent = "Working…";
      const result = await api(`/api/tools/${tool.dataset.tool}`, { method: "POST", body: {} });
      if (!result || result.ok === false) {
        status.textContent = (result && result.error) || "That did not work.";
        return;
      }
      if (tool.dataset.tool === "phone_photos") {
        close();
        this.showPhoneSession(result.session || {});
        return;
      }
      status.textContent = result.message || "Done";
    });
    this.syncScreenRecording();
    this.recordingPoll = window.setInterval(() => this.syncScreenRecording(), 1000);

    const demoBtn = document.getElementById("nav-demo");
    if (demoBtn) {
      demoBtn.addEventListener("click", () => {
        window.location.href = "demo.html";
      });
    }
  }

  recordingTime(seconds) {
    const elapsed = Math.max(0, Number(seconds) || 0);
    const minutes = Math.floor(elapsed / 60);
    return `${String(minutes).padStart(2, "0")}:${String(elapsed % 60).padStart(2, "0")}`;
  }

  applyRecordingState(result) {
    if (!result || !this.recordTool) return;
    const glyph = this.recordTool.querySelector(".glyph");
    const title = this.recordTool.querySelector("b");
    const detail = this.recordTool.querySelector("i");
    this.recordTool.classList.toggle("recording", Boolean(result.active));
    if (result.active) {
      glyph.textContent = "■";
      title.textContent = "Stop recording";
      detail.textContent = `REC ${this.recordingTime(result.elapsed)} • ${result.label || "screen"}`;
    } else {
      glyph.textContent = "●";
      title.textContent = "Record screen";
      detail.textContent = "Area, window, or monitor";
    }
  }

  async syncScreenRecording() {
    const result = await api("/api/tools/record_screen", {
      method: "POST",
      body: { action: "status" },
    });
    if (result && result.ok) this.applyRecordingState(result);
  }

  async openScreenRecorder() {
    const status = document.getElementById("quick-tools-status");
    status.textContent = "Checking recorder…";
    const result = await api("/api/tools/record_screen", {
      method: "POST",
      body: { action: "options" },
    });
    if (!result || result.ok === false) {
      status.textContent = (result && result.error) || "Recorder unavailable.";
      return;
    }
    this.applyRecordingState(result);
    if (result.active) {
      status.textContent = "Stopping recording…";
      const stopped = await api("/api/tools/record_screen", {
        method: "POST",
        body: { action: "stop" },
      });
      if (!stopped || stopped.ok === false) {
        status.textContent = (stopped && stopped.error) || "Could not stop recording.";
        return;
      }
      this.applyRecordingState(stopped);
      status.textContent = stopped.message || "Recording stopped";
      this.toast(stopped.message || "Recording stopped");
      return;
    }
    if (this.closeQuickTools) this.closeQuickTools();
    this.showScreenRecorder(result);
  }

  showScreenRecorder(options) {
    const node = document.createElement("div");
    node.className = "modal-backdrop recorder-backdrop";
    node.innerHTML = `
      <div class="modal recorder-modal">
        <div class="modal-title">Record screen</div>
        <div class="modal-detail">Choose exactly what aiOS should capture.</div>
        <button class="recorder-area" data-record-source="area">
          <b>Select an area</b><span>Drag anywhere across your screens</span>
        </button>
        <label class="recorder-target"><span>Monitor</span><select data-record-monitor></select></label>
        <button class="btn compact" data-record-source="monitor">Record monitor</button>
        <label class="recorder-target"><span>Window</span><select data-record-window></select></label>
        <button class="btn compact" data-record-source="window">Record window</button>
        <div class="recorder-note"></div>
        <div class="modal-actions">
          <button class="btn compact ghost" data-record-folder>Open recordings</button>
          <button class="btn compact ghost" data-record-cancel>Cancel</button>
        </div>
      </div>
    `;
    const monitorSelect = node.querySelector("[data-record-monitor]");
    const windowSelect = node.querySelector("[data-record-window]");
    const addOptions = (select, entries, empty) => {
      if (!entries.length) {
        const option = document.createElement("option");
        option.textContent = empty;
        option.value = "";
        select.appendChild(option);
        select.disabled = true;
        return;
      }
      for (const entry of entries) {
        const option = document.createElement("option");
        option.value = entry.id;
        option.textContent = entry.label;
        select.appendChild(option);
      }
    };
    addOptions(monitorSelect, options.monitors || [], "No monitors found");
    addOptions(windowSelect, options.windows || [], "No windows found");
    const note = node.querySelector(".recorder-note");
    if (!options.available) {
      note.textContent = "FFmpeg was not found on this PC.";
      node.querySelectorAll("[data-record-source]").forEach((button) => { button.disabled = true; });
    } else {
      note.textContent = "Recording saves as MP4 in Videos\\aiOS recordings.";
    }

    const close = () => node.remove();
    node.addEventListener("click", async (event) => {
      if (event.target === node || event.target.closest("[data-record-cancel]")) {
        close();
        return;
      }
      if (event.target.closest("[data-record-folder]")) {
        await api("/api/tools/recordings", { method: "POST", body: {} });
        return;
      }
      const button = event.target.closest("[data-record-source]");
      if (!button) return;
      button.disabled = true;
      const source = button.dataset.recordSource;
      const payload = { action: "start", source };
      if (source === "area") {
        note.textContent = "Drag the area to record. Press Esc to cancel.";
        const bounds = await native("pick_screen_area");
        if (!bounds || !bounds.width) {
          button.disabled = false;
          note.textContent = "Area selection cancelled.";
          return;
        }
        payload.bounds = bounds;
      } else if (source === "monitor") {
        payload.id = monitorSelect.value;
      } else {
        payload.id = windowSelect.value;
      }
      note.textContent = "Starting recorder…";
      const started = await api("/api/tools/record_screen", { method: "POST", body: payload });
      if (!started || started.ok === false) {
        button.disabled = false;
        note.textContent = (started && started.error) || "Could not start recording.";
        return;
      }
      this.applyRecordingState(started);
      close();
      this.toast(started.message || "Screen recording started");
    });
    document.body.appendChild(node);
    requestAnimationFrame(() => node.classList.add("show"));
  }

  /**
   * Live webcam → clipboard → paste-at-cursor.
   *
   * Camera runs in Python (OpenCV/DirectShow) — no browser permission prompt.
   * Stays open for multi-snap; Space / Snap & paste hides aiOS briefly so
   * Ctrl+V lands in the app that had focus before.
   */
  openWebcamSnap() {
    if (this._webcamCloser) {
      this._webcamCloser();
    }
    const node = document.createElement("div");
    node.className = "modal-backdrop webcam-backdrop";
    node.innerHTML = `
      <div class="modal webcam-modal">
        <div class="modal-title">Webcam snap</div>
        <div class="modal-detail">Point at your desk, snap, and it pastes where your cursor was.</div>
        <div class="webcam-stage">
          <img class="webcam-preview" alt="Camera preview" draggable="false">
          <div class="webcam-empty">Starting camera…</div>
        </div>
        <div class="webcam-meta">
          <label class="webcam-device"><span>Camera</span><select data-webcam-device></select></label>
          <div class="webcam-note" data-webcam-note>Space = snap &amp; paste</div>
        </div>
        <div class="webcam-shots" data-webcam-shots></div>
        <div class="modal-actions webcam-actions">
          <button class="btn compact ghost" data-webcam="close">Close</button>
          <button class="btn compact ghost" data-webcam="copy" disabled>Copy only</button>
          <button class="btn compact accent" data-webcam="paste" disabled>Snap &amp; paste</button>
        </div>
      </div>
    `;
    const preview = node.querySelector(".webcam-preview");
    const empty = node.querySelector(".webcam-empty");
    const deviceSelect = node.querySelector("[data-webcam-device]");
    const note = node.querySelector("[data-webcam-note]");
    const shots = node.querySelector("[data-webcam-shots]");
    const copyBtn = node.querySelector('[data-webcam="copy"]');
    const pasteBtn = node.querySelector('[data-webcam="paste"]');
    let busy = false;
    let closed = false;
    let ready = false;
    let pollTimer = 0;
    let lastPreview = "";

    const setNote = (text, kind = "") => {
      note.textContent = text;
      note.dataset.kind = kind;
    };
    const setReady = (value) => {
      ready = value;
      copyBtn.disabled = !value;
      pasteBtn.disabled = !value;
    };
    const stopPoll = () => {
      if (pollTimer) {
        window.clearTimeout(pollTimer);
        pollTimer = 0;
      }
    };
    const close = () => {
      if (closed) return;
      closed = true;
      stopPoll();
      document.removeEventListener("keydown", onKey, true);
      if (this._webcamCloser === close) this._webcamCloser = null;
      node.remove();
      api("/api/tools/webcam_snap", { method: "POST", body: { action: "stop" }, timeout: 8000 })
        .catch(() => {});
    };
    this._webcamCloser = close;

    const fillDevices = (devices, selectedId = "auto") => {
      const list = Array.isArray(devices) && devices.length
        ? devices
        : [{ id: "auto", label: "Auto (prefer Lenovo RGB)" }];
      deviceSelect.innerHTML = "";
      deviceSelect.disabled = false;
      for (const entry of list) {
        const option = document.createElement("option");
        option.value = String(entry.id);
        option.textContent = entry.label || `Camera ${entry.id}`;
        deviceSelect.appendChild(option);
      }
      const wanted = String(selectedId ?? "auto");
      if ([...deviceSelect.options].some((opt) => opt.value === wanted)) {
        deviceSelect.value = wanted;
      } else {
        deviceSelect.value = "auto";
      }
    };

    const rememberShot = (dataUrl) => {
      if (!dataUrl) return;
      const img = document.createElement("img");
      img.src = dataUrl;
      img.alt = "Snap";
      img.title = "Copied";
      shots.prepend(img);
      while (shots.children.length > 8) shots.lastElementChild.remove();
    };

    const pollPreview = async () => {
      if (closed || busy) {
        pollTimer = window.setTimeout(pollPreview, 180);
        return;
      }
      try {
        const result = await api("/api/tools/webcam_snap", {
          method: "POST",
          body: { action: "preview", device: deviceSelect.value || "auto" },
          timeout: 8000,
        });
        if (closed) return;
        if (result && result.ok && result.image) {
          lastPreview = result.image;
          preview.src = result.image;
          preview.classList.add("live");
          empty.hidden = true;
          if (!ready) {
            setReady(true);
            setNote("Space = snap & paste");
          }
        } else if (!ready) {
          empty.hidden = false;
          empty.textContent = (result && result.error) || "Waiting for camera…";
        }
      } catch (error) {
        if (!closed && !ready) {
          empty.hidden = false;
          empty.textContent = String(error.message || error);
        }
      }
      if (!closed) pollTimer = window.setTimeout(pollPreview, 120);
    };

    const runSnap = async (paste) => {
      if (busy || closed || !ready) return;
      busy = true;
      setReady(false);
      setNote(paste ? "Snapping & pasting…" : "Copying…");
      try {
        // Server grabs the OpenCV frame (already flipped). No browser capture.
        const copied = await api("/api/tools/webcam_snap", {
          method: "POST",
          body: { action: "copy", device: deviceSelect.value || "auto" },
          timeout: 20000,
        });
        if (!copied || copied.ok === false) {
          setNote((copied && copied.error) || "Could not copy the snap.", "error");
          return;
        }
        rememberShot(lastPreview);
        if (!paste) {
          setNote("Copied — Ctrl+V wherever you want it.");
          this.toast("Webcam snap copied");
          return;
        }
        await native("hide");
        await new Promise((resolve) => window.setTimeout(resolve, 140));
        const pasted = await api("/api/tools/webcam_snap", {
          method: "POST",
          body: { action: "paste" },
        });
        await new Promise((resolve) => window.setTimeout(resolve, 90));
        await native("show");
        if (!pasted || pasted.ok === false) {
          setNote((pasted && pasted.error) || "Copied, but paste failed.", "error");
          this.toast("Snap copied (paste failed)", "error");
          return;
        }
        setNote("Pasted. Snap again whenever you are ready.");
        this.toast("Webcam snap pasted");
      } catch (error) {
        setNote(String(error.message || error), "error");
        try { await native("show"); } catch { /* still hidden? leave note */ }
      } finally {
        busy = false;
        if (!closed) setReady(true);
      }
    };

    const startCamera = async (deviceId = "auto") => {
      stopPoll();
      setReady(false);
      preview.classList.remove("live");
      preview.removeAttribute("src");
      empty.hidden = false;
      empty.textContent = "Starting camera…";
      setNote("Looking for Lenovo RGB…");
      const result = await api("/api/tools/webcam_snap", {
        method: "POST",
        body: { action: "start", device: deviceId || "auto" },
        timeout: 30000,
      });
      if (closed) return;
      if (!result || result.ok === false) {
        empty.textContent = (result && result.error) || "Could not open the camera.";
        setNote(empty.textContent, "error");
        fillDevices(result && result.devices, deviceId);
        return;
      }
      fillDevices(result.devices, result.device != null ? String(result.device) : deviceId);
      setNote(result.name ? `Live: ${result.name}` : "Warming up…");
      pollPreview();
    };

    const onKey = (event) => {
      if (closed) return;
      if (event.key === "Escape") {
        event.preventDefault();
        close();
        return;
      }
      if (event.key === " " || event.code === "Space") {
        if (event.target && /^(INPUT|SELECT|TEXTAREA|BUTTON)$/.test(event.target.tagName)) return;
        event.preventDefault();
        runSnap(true);
      }
    };

    deviceSelect.addEventListener("change", () => startCamera(deviceSelect.value));
    node.addEventListener("click", (event) => {
      if (event.target === node) {
        close();
        return;
      }
      const action = event.target.closest("[data-webcam]");
      if (!action) return;
      if (action.dataset.webcam === "close") close();
      else if (action.dataset.webcam === "copy") runSnap(false);
      else if (action.dataset.webcam === "paste") runSnap(true);
    });
    document.addEventListener("keydown", onKey, true);
    document.body.appendChild(node);
    requestAnimationFrame(() => node.classList.add("show"));
    fillDevices(null, "auto");
    startCamera("auto");
  }

  /** The phone photo-drop link the local bridge just minted. */
  showPhoneSession(session) {
    const url = session.url || session.link || "";
    this.sheet("Phone to PC", [
      ["Open on phone", url || "(no link returned)"],
      ["Session", String(session.id || session.session_id || "—")],
    ], "Open that link on your phone; photos you shoot land in this PC's Pictures folder.");
  }

  show(name) {
    const { history = true } = arguments[1] || {};
    if (this.active === name) return;
    if (history) {
      this.pageHistory = this.pageHistory.slice(0, this.pageHistoryIndex + 1);
      this.pageHistory.push(name);
      this.pageHistoryIndex = this.pageHistory.length - 1;
    }
    if (this.tab && this.tab.destroy) this.tab.destroy();
    this.tab = null;
    this.active = name;

    for (const button of this.nav.querySelectorAll("[data-tab]")) {
      // BENCH and HARNESS are opened from inside CODE, so CODE stays lit while
      // you are in either of them.
      button.classList.toggle("active", button.dataset.tab === (CODE_PAGES.has(name) ? "CODE" : name));
    }
    // They are tools for the CODE harness rather than places you live in, so
    // neither is ever the tab aiOS reopens on.
    if (!this.phoneMirror && !CODE_PAGES.has(name)) {
      api("/api/config", { method: "POST", body: { active_tab: name } });
    }

    this.page.innerHTML = "";
    if (name === "CODE") {
      this.tab = new CodeTab(this.page, this);
      return;
    }
    if (name === "BENCH") {
      this.tab = new BenchTab(this.page, this);
      return;
    }
    if (name === "HARNESS") {
      this.tab = new HarnessTab(this.page, this);
      return;
    }
    if (name === "Settings") {
      this.tab = new SettingsTab(this.page, this);
      return;
    }
    this.page.innerHTML = `
      <div class="placeholder">
        <strong>${name}</strong>
        ${PENDING[name] || ""}<br>
        <span style="opacity:.65">Still served by the Tk build &mdash; being ported next.</span>
      </div>
    `;
  }

  // ------------------------------------------------------------ affordances

  toast(message, kind = "info") {
    const node = document.createElement("div");
    node.className = `toast ${kind}`;
    node.textContent = message;
    document.body.appendChild(node);
    requestAnimationFrame(() => node.classList.add("show"));
    setTimeout(() => {
      node.classList.remove("show");
      node.addEventListener("transitionend", () => node.remove(), { once: true });
    }, 4200);
  }

  confirm(title, detail = "") {
    return new Promise((resolve) => {
      const node = document.createElement("div");
      node.className = "modal-backdrop";
      node.innerHTML = `
        <div class="modal">
          <div class="modal-title"></div>
          <div class="modal-detail"></div>
          <div class="modal-actions">
            <button class="btn compact ghost" data-choice="no">Cancel</button>
            <button class="btn compact accent" data-choice="yes">Confirm</button>
          </div>
        </div>
      `;
      node.querySelector(".modal-title").textContent = title;
      node.querySelector(".modal-detail").textContent = detail;
      node.addEventListener("click", (event) => {
        const button = event.target.closest("[data-choice]");
        if (!button && event.target !== node) return;
        node.remove();
        resolve(button ? button.dataset.choice === "yes" : false);
      });
      document.body.appendChild(node);
      requestAnimationFrame(() => node.classList.add("show"));
    });
  }

  /** A read-only detail sheet -- the web equivalent of messagebox.showinfo. */
  sheet(title, rows, footnote = "") {
    const node = document.createElement("div");
    node.className = "modal-backdrop";
    node.innerHTML = `
      <div class="modal">
        <div class="modal-title"></div>
        <div class="sheet-rows"></div>
        <div class="modal-detail footnote"></div>
        <div class="modal-actions"><button class="btn compact accent" data-choice="ok">Close</button></div>
      </div>
    `;
    node.querySelector(".modal-title").textContent = title;
    node.querySelector(".sheet-rows").innerHTML = rows
      .map(([label, value]) => `<div class="sheet-row"><span></span><b></b></div>`)
      .join("");
    node.querySelectorAll(".sheet-row").forEach((row, index) => {
      row.querySelector("span").textContent = rows[index][0];
      row.querySelector("b").textContent = rows[index][1];
    });
    node.querySelector(".footnote").textContent = footnote;
    node.addEventListener("click", (event) => {
      if (event.target.closest("[data-choice]") || event.target === node) node.remove();
    });
    document.body.appendChild(node);
    requestAnimationFrame(() => node.classList.add("show"));
  }

  async pickFolder() {
    const picked = await native("pick_folder");
    return picked || null;
  }

  /** A single-select chooser. rows is [[label, value], ...]; resolves to the
   *  chosen index, or null when dismissed. */
  pick(title, rows, detail = "") {
    return new Promise((resolve) => {
      const node = document.createElement("div");
      node.className = "modal-backdrop";
      node.innerHTML = `
        <div class="modal">
          <div class="modal-title"></div>
          <div class="modal-detail"></div>
          <div class="pick-rows"></div>
          <div class="modal-actions"><button class="btn compact ghost" data-choice="cancel">Cancel</button></div>
        </div>
      `;
      node.querySelector(".modal-title").textContent = title;
      node.querySelector(".modal-detail").textContent = detail;
      const box = node.querySelector(".pick-rows");
      rows.forEach(([label, value], index) => {
        const row = document.createElement("button");
        row.type = "button";
        row.className = "pick-row";
        row.innerHTML = `<span></span><b></b>`;
        row.querySelector("span").textContent = label;
        row.querySelector("b").textContent = value;
        row.addEventListener("click", () => {
          node.remove();
          resolve(index);
        });
        box.appendChild(row);
      });
      node.addEventListener("click", (event) => {
        if (event.target.closest("[data-choice=cancel]") || event.target === node) {
          node.remove();
          resolve(null);
        }
      });
      document.body.appendChild(node);
      requestAnimationFrame(() => node.classList.add("show"));
    });
  }
}

const shell = new Shell();
window.aios = shell;

// pywebview injects its bridge asynchronously; the UI must not wait on it for
// anything except the native calls themselves.
shell.boot();

// Live reload is off on purpose: CODE editing aiOS mid-session was reloading
// the running window and crashing it. Re-enable via startLiveReload() only
// when deliberately iterating on the GUI.
