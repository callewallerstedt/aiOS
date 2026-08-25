// Tiny 3×3 Quick Tools palette for the macropad hold gesture.

import { api, native } from "./bridge.js";

const TOOLS = [
  { id: "webcam_snap", glyph: "◉", label: "Webcam", kind: "snap" },
  { id: "phone_photos", glyph: "⇄", label: "Phone", kind: "http" },
  { id: "paste_image", glyph: "V", label: "Paste", kind: "http" },
  { id: "record_screen", glyph: "●", label: "Record", kind: "ui" },
  { id: "open_aios", glyph: "ai", label: "aiOS", kind: "main", primary: true },
  { id: "recordings", glyph: "▶", label: "Clips", kind: "http" },
  { id: "downloads", glyph: "↓", label: "Down", kind: "http" },
  { id: "close", glyph: "×", label: "Close", kind: "close" },
  { id: "open_code", glyph: "</>", label: "CODE", kind: "main" },
];

const grid = document.getElementById("qt-grid");
const status = document.getElementById("qt-status");
let busy = false;

function applyTheme(theme, config) {
  const root = document.documentElement.style;
  for (const key of ["accent", "panel", "panel2", "surface", "surface2", "text", "muted", "danger", "success"]) {
    if (theme[key]) root.setProperty(`--${key}`, theme[key]);
  }
  if (theme.radius != null) {
    const radius = Math.max(0, Number(theme.radius) || 0);
    root.setProperty("--radius", `${radius}px`);
    root.setProperty("--global-radius", `${radius}px`);
  }
  if (theme.font_size) root.setProperty("--font-size", `${theme.font_size}px`);
}

async function bootTheme() {
  const result = await api("/api/config", { timeout: 4000 });
  if (result && result.config) {
    applyTheme(result.config.theme || {}, result.config);
  }
}

function setStatus(text, kind = "") {
  status.textContent = text || "";
  status.dataset.kind = kind;
}

function render() {
  grid.innerHTML = TOOLS.map((tool) => `
    <button class="qt-cell${tool.kind === "close" ? " is-close" : ""}${tool.primary ? " is-primary" : ""}"
            type="button" data-tool="${tool.id}" title="${tool.label}">
      <span class="glyph">${tool.glyph}</span>
      <span class="label">${tool.label}</span>
    </button>
  `).join("");
}

async function runTool(id) {
  if (busy) return;
  const tool = TOOLS.find((entry) => entry.id === id);
  if (!tool) return;

  if (tool.kind === "close") {
    await native("close");
    return;
  }
  if (tool.id === "open_aios") {
    await native("open_main", "");
    return;
  }
  if (tool.id === "open_code") {
    await native("open_main", "CODE");
    return;
  }
  // One-shot: grab frame → clipboard → close → paste. No preview modal.
  if (tool.id === "webcam_snap" || tool.kind === "snap") {
    busy = true;
    setStatus("Snap…");
    try {
      await native("webcam_snap_now");
    } catch (error) {
      setStatus(String(error.message || error), "error");
    } finally {
      busy = false;
    }
    return;
  }
  if (tool.kind === "ui") {
    await native("open_tool", tool.id);
    return;
  }

  busy = true;
  grid.querySelectorAll(".qt-cell").forEach((node) => node.classList.add("is-busy"));
  setStatus("Working…");
  try {
    const result = await api(`/api/tools/${tool.id}`, { method: "POST", body: {}, timeout: 12000 });
    if (!result || result.ok === false) {
      setStatus((result && result.error) || "That did not work.", "error");
      return;
    }
    if (tool.id === "phone_photos") {
      const session = result.session || {};
      const url = session.url || session.link || "";
      if (url) {
        try { await navigator.clipboard.writeText(url); } catch { /* optional */ }
        setStatus("Phone link copied", "ok");
      } else {
        setStatus(result.message || "Phone session ready", "ok");
      }
      window.setTimeout(() => native("close"), 450);
      return;
    }
    setStatus(result.message || "Done", "ok");
    window.setTimeout(() => native("close"), 280);
  } finally {
    busy = false;
    grid.querySelectorAll(".qt-cell").forEach((node) => node.classList.remove("is-busy"));
  }
}

function flashTool(id) {
  const button = grid.querySelector(`[data-tool="${id}"]`);
  if (!button) return;
  button.classList.add("is-pressed");
  window.setTimeout(() => button.classList.remove("is-pressed"), 140);
}

grid.addEventListener("click", (event) => {
  const button = event.target.closest("[data-tool]");
  if (!button) return;
  runTool(button.dataset.tool);
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.preventDefault();
    native("close");
    return;
  }
  // Numpad / top-row 1–9 match the 3×3 grid (same as the physical pad).
  if (event.key >= "1" && event.key <= "9" && !event.ctrlKey && !event.altKey && !event.metaKey) {
    event.preventDefault();
    const tool = TOOLS[Number(event.key) - 1];
    if (tool) runTool(tool.id);
  }
});

// Macropad → TCP `qt:<id>` → Python evaluate_js → here.
window.aiosQt = {
  run(id) {
    const key = String(id || "").trim();
    if (!key) return false;
    flashTool(key);
    runTool(key);
    return true;
  },
};

render();
setStatus("");
bootTheme();
