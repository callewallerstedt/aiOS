import { renderMarkdown, escapeHtml } from "./markdown.js";

const shell = document.getElementById("shell");
const article = document.getElementById("article");
const raw = document.getElementById("raw");
const empty = document.getElementById("empty");
const stage = document.getElementById("stage");
const tocPane = document.getElementById("toc");
const tocList = document.getElementById("toc-list");
const searchBox = document.getElementById("search");
const searchInput = document.getElementById("search-input");
const searchCount = document.getElementById("search-count");
const docTitle = document.getElementById("doc-title");
const docPath = document.getElementById("doc-path");
const docStats = document.getElementById("doc-stats");
const progress = document.getElementById("progress").firstElementChild;
const btnRaw = document.getElementById("btn-raw");
const btnToc = document.getElementById("btn-toc");

const state = {
  path: "",
  name: "",
  content: "",
  html: "",
  toc: [],
  renderedFor: null,
  rawMode: false,
  tocOpen: true,
  zoom: 1,
  hits: [],
  hitIndex: -1,
};

function api() {
  return window.pywebview?.api || null;
}

async function call(name, ...args) {
  const bridge = api();
  if (!bridge || typeof bridge[name] !== "function") return null;
  return bridge[name](...args);
}

function applyTheme(theme) {
  if (!theme || typeof theme !== "object") return;
  for (const key of ["accent", "panel", "panel2", "surface", "surface2", "text", "muted", "danger"]) {
    if (typeof theme[key] === "string" && theme[key].startsWith("#")) {
      document.documentElement.style.setProperty(`--${key}`, theme[key]);
    }
  }
}

function setZoom(next) {
  state.zoom = Math.min(1.8, Math.max(0.8, Number(next) || 1));
  document.documentElement.style.setProperty("--zoom", String(state.zoom));
}

function wordCount(text) {
  const words = String(text || "").trim().match(/\S+/g);
  return words ? words.length : 0;
}

function formatStats(content) {
  const lines = String(content || "").split(/\r\n|\r|\n/).length;
  const words = wordCount(content);
  return `${lines.toLocaleString()} lines · ${words.toLocaleString()} words`;
}

function showDocument(payload) {
  if (!payload) return;
  applyTheme(payload.theme);
  state.path = String(payload.path || "");
  state.name = String(payload.name || (state.path ? state.path.split(/[/\\]/).pop() : "Markdown"));
  state.content = String(payload.content || "");
  state.renderedFor = null;
  state.html = "";
  state.toc = [];
  state.rawMode = false;

  document.title = state.path ? `${state.name} — aiOS` : "aiOS Markdown";
  docTitle.textContent = state.name || "Markdown";
  docPath.textContent = state.path || "No file open";
  docPath.title = state.path ? "Reveal in Explorer" : "No file open";
  docStats.textContent = state.path || state.content ? formatStats(state.content) : "—";

  const hasDoc = Boolean(state.content || state.path);
  empty.hidden = hasDoc;
  ensureRendered();
  applyChrome({ resetScroll: true });
}

/** Parse markdown once per content change — never on Contents/Raw toggles. */
function ensureRendered() {
  if (state.renderedFor === state.content) return;
  clearSearchMarks(true);
  if (!state.content && !state.path) {
    state.html = "";
    state.toc = [];
    article.innerHTML = "";
    raw.textContent = "";
    tocList.innerHTML = "";
    state.renderedFor = state.content;
    return;
  }
  const rendered = renderMarkdown(state.content);
  state.html = rendered.html || "<p><em>Empty document.</em></p>";
  state.toc = rendered.toc || [];
  article.innerHTML = state.html;
  raw.textContent = state.content;
  buildToc();
  state.renderedFor = state.content;
}

function buildToc() {
  tocList.innerHTML = "";
  for (const item of state.toc) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `toc-item l${Math.min(6, item.level)}`;
    button.textContent = item.text || item.id;
    button.addEventListener("click", () => {
      if (state.rawMode) {
        state.rawMode = false;
        applyChrome();
      }
      const target = document.getElementById(item.id);
      if (!target) return;
      target.scrollIntoView({ behavior: "smooth", block: "start" });
      for (const node of tocList.querySelectorAll(".toc-item")) node.classList.remove("active");
      button.classList.add("active");
    });
    tocList.appendChild(button);
  }
}

function applyChrome({ resetScroll = false } = {}) {
  const hasDoc = Boolean(state.content || state.path);
  if (!hasDoc) {
    article.hidden = true;
    raw.hidden = true;
    tocPane.hidden = true;
    btnRaw.classList.remove("active");
    btnToc.classList.remove("active");
    return;
  }

  article.hidden = state.rawMode;
  raw.hidden = !state.rawMode;
  btnRaw.classList.toggle("active", state.rawMode);

  const tocVisible = !state.rawMode && state.tocOpen && state.toc.length > 0;
  tocPane.hidden = !tocVisible;
  // Keep the Contents button reflecting the user's preference, even in Raw,
  // so toggling never feels "stuck" when modes interact.
  btnToc.classList.toggle("active", state.tocOpen && state.toc.length > 0);
  btnToc.disabled = state.toc.length === 0;
  btnToc.title = state.toc.length
    ? "Table of contents (Ctrl+\\)"
    : "No headings in this document";

  if (resetScroll) stage.scrollTop = 0;
  updateProgress();
}

function toggleToc() {
  ensureRendered();
  if (state.rawMode) {
    // Contents while in Raw means "leave Raw and show the outline".
    state.rawMode = false;
    state.tocOpen = true;
  } else {
    state.tocOpen = !state.tocOpen;
  }
  applyChrome();
}

function toggleRaw() {
  ensureRendered();
  state.rawMode = !state.rawMode;
  applyChrome();
  if (!state.rawMode && !searchBox.hidden && searchInput.value) {
    runSearch(searchInput.value, false);
  }
}

function updateProgress() {
  const max = stage.scrollHeight - stage.clientHeight;
  const ratio = max > 0 ? stage.scrollTop / max : 0;
  progress.style.width = `${Math.max(0, Math.min(1, ratio)) * 100}%`;
}

function openSearch() {
  searchBox.hidden = false;
  searchInput.focus();
  searchInput.select();
  if (searchInput.value) runSearch(searchInput.value, false);
}

function closeSearch() {
  searchBox.hidden = true;
  clearSearchMarks(true);
  searchCount.textContent = "0/0";
}

function clearSearchMarks(resetIndex) {
  for (const mark of article.querySelectorAll("mark.search-hit")) {
    const text = document.createTextNode(mark.textContent || "");
    mark.replaceWith(text);
  }
  article.normalize();
  state.hits = [];
  if (resetIndex) state.hitIndex = -1;
}

function runSearch(query, moveToFirst = true) {
  clearSearchMarks(true);
  const needle = String(query || "");
  if (!needle || state.rawMode) {
    searchCount.textContent = "0/0";
    return;
  }

  const walker = document.createTreeWalker(article, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);

  const lowerNeedle = needle.toLowerCase();
  for (const node of nodes) {
    const value = node.nodeValue || "";
    const lower = value.toLowerCase();
    let start = 0;
    let index = lower.indexOf(lowerNeedle, start);
    if (index < 0) continue;

    const frag = document.createDocumentFragment();
    while (index >= 0) {
      if (index > start) frag.appendChild(document.createTextNode(value.slice(start, index)));
      const mark = document.createElement("mark");
      mark.className = "search-hit";
      mark.textContent = value.slice(index, index + needle.length);
      frag.appendChild(mark);
      state.hits.push(mark);
      start = index + needle.length;
      index = lower.indexOf(lowerNeedle, start);
    }
    if (start < value.length) frag.appendChild(document.createTextNode(value.slice(start)));
    node.parentNode?.replaceChild(frag, node);
  }

  searchCount.textContent = state.hits.length ? `1/${state.hits.length}` : "0/0";
  if (state.hits.length && moveToFirst) jumpHit(0);
}

function jumpHit(index) {
  if (!state.hits.length) return;
  const next = ((index % state.hits.length) + state.hits.length) % state.hits.length;
  state.hitIndex = next;
  for (const [i, mark] of state.hits.entries()) {
    mark.classList.toggle("current", i === next);
  }
  state.hits[next].scrollIntoView({ behavior: "smooth", block: "center" });
  searchCount.textContent = `${next + 1}/${state.hits.length}`;
}

async function bootstrap() {
  const payload = await call("get_document");
  if (payload) showDocument(payload);
  else empty.hidden = false;
}

async function openFile() {
  const payload = await call("open_dialog");
  if (payload?.ok) showDocument(payload);
}

async function reloadFile() {
  const payload = await call("reload");
  if (payload?.ok) showDocument(payload);
}

function wireUi() {
  document.getElementById("btn-open").addEventListener("click", () => void openFile());
  document.getElementById("btn-reload").addEventListener("click", () => void reloadFile());
  document.getElementById("btn-search").addEventListener("click", () => {
    if (searchBox.hidden) openSearch();
    else searchInput.focus();
  });
  document.getElementById("search-close").addEventListener("click", closeSearch);
  document.getElementById("search-prev").addEventListener("click", () => jumpHit(state.hitIndex - 1));
  document.getElementById("search-next").addEventListener("click", () => jumpHit(state.hitIndex + 1));
  document.getElementById("btn-toc").addEventListener("click", () => toggleToc());
  document.getElementById("btn-raw").addEventListener("click", () => toggleRaw());
  document.getElementById("btn-min").addEventListener("click", () => void call("minimize"));
  document.getElementById("btn-max").addEventListener("click", () => void call("toggle_maximize"));
  document.getElementById("btn-close").addEventListener("click", () => void call("close"));
  document.getElementById("btn-zoom-in").addEventListener("click", () => setZoom(state.zoom + 0.1));
  document.getElementById("btn-zoom-out").addEventListener("click", () => setZoom(state.zoom - 0.1));
  docPath.addEventListener("click", () => void call("reveal"));

  searchInput.addEventListener("input", () => runSearch(searchInput.value, true));
  searchInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      jumpHit(event.shiftKey ? state.hitIndex - 1 : state.hitIndex + 1);
    } else if (event.key === "Escape") {
      event.preventDefault();
      closeSearch();
    }
  });

  stage.addEventListener("scroll", updateProgress, { passive: true });

  article.addEventListener("click", (event) => {
    const link = event.target.closest("a[data-link]");
    if (!link) return;
    event.preventDefault();
    void call("open_link", link.getAttribute("data-link") || "");
  });

  const grip = document.getElementById("resize-grip");
  let resizing = null;
  grip.addEventListener("mousedown", (event) => {
    resizing = { x: event.screenX, y: event.screenY, w: window.outerWidth, h: window.outerHeight };
    event.preventDefault();
  });
  window.addEventListener("mousemove", (event) => {
    if (!resizing) return;
    const width = Math.max(640, resizing.w + (event.screenX - resizing.x));
    const height = Math.max(420, resizing.h + (event.screenY - resizing.y));
    void call("resize_window", width, height);
  });
  window.addEventListener("mouseup", () => { resizing = null; });

  window.addEventListener("keydown", (event) => {
    const mod = event.ctrlKey || event.metaKey;
    const key = event.key.toLowerCase();
    if (mod && key === "f") {
      event.preventDefault();
      openSearch();
    } else if (mod && key === "o") {
      event.preventDefault();
      void openFile();
    } else if (mod && key === "r") {
      event.preventDefault();
      void reloadFile();
    } else if (mod && key === "u") {
      event.preventDefault();
      toggleRaw();
    } else if (mod && (event.key === "\\" || event.code === "Backslash")) {
      event.preventDefault();
      toggleToc();
    } else if (mod && (key === "=" || key === "+")) {
      event.preventDefault();
      setZoom(state.zoom + 0.1);
    } else if (mod && key === "-") {
      event.preventDefault();
      setZoom(state.zoom - 0.1);
    } else if (mod && key === "0") {
      event.preventDefault();
      setZoom(1);
    } else if (event.key === "F5") {
      event.preventDefault();
      void reloadFile();
    } else if (event.key === "Escape") {
      if (!searchBox.hidden) {
        event.preventDefault();
        closeSearch();
      } else {
        void call("close");
      }
    }
  });
}

function ready(fn) {
  if (window.pywebview?.api) {
    fn();
    return;
  }
  window.addEventListener("pywebviewready", fn, { once: true });
  // File opened outside pywebview (browser smoke test).
  setTimeout(() => {
    if (!window.pywebview?.api) {
      empty.hidden = false;
      empty.querySelector("p").textContent = "Waiting for the aiOS reader shell…";
    }
  }, 800);
}

window.__mdReaderLoad = showDocument;

wireUi();
ready(() => { void bootstrap(); });

// Keep helpers referenced for the module graph / future hooks.
void shell;
void escapeHtml;
