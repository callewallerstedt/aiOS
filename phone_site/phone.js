/* aiOS Remote — phone client. */

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const MODELS = [
  { id: "luna", name: "Luna", note: "Fast and precise — the default" },
  { id: "terra", name: "Terra", note: "Steadier on dense screens" },
  { id: "sol", name: "Sol", note: "Strongest reasoning, slower" }
];

const PLANNER_MODELS = [
  { id: "off", name: "Off", note: "Start clicking immediately" },
  { id: "luna", name: "Luna", note: "Fast, inexpensive planning" },
  { id: "terra", name: "Terra", note: "Stronger planning at moderate speed" },
  { id: "sol", name: "Sol", note: "Strongest planning before execution" }
];

const EFFORTS = [
  { id: "low", name: "Low", note: "Acts quickly" },
  { id: "medium", name: "Medium", note: "Balanced" },
  { id: "high", name: "High", note: "Thinks longer before each move" }
];

const AI_PROVIDERS = [
  { id: "codex", name: "Codex", note: "Use the signed-in ChatGPT Codex account" },
  { id: "api", name: "OpenAI API", note: "Always use the API key saved on this PC" },
  { id: "codex_api_fallback", name: "Codex + API fallback", note: "Use Codex first; switch to the API key if needed" }
];

const RUNNING_STATES = new Set(["running", "starting", "thinking", "acting", "waiting"]);
const MAX_CLARIFIER_QUESTIONS = 10;

/* History + session hygiene. Coming back to the app should feel like opening
   a fresh page, with everything that happened parked in History. */
const HISTORY_KEY = "aios_history_v1";
const MAX_HISTORY_RUNS = 25;
const MAX_RUN_EVENTS = 400;
const HISTORY_BUDGET = 1_200_000;      // characters of JSON, well inside the quota
const IDLE_RUN_CLOSE_MS = 3 * 60 * 1000;
const EVENT_PAGE = 200;                // the relay's page size
const MAX_DRAIN_PAGES = 40;
const SERVER_PRUNE_EVENTS = 600;       // archived backlog worth deleting server-side
const MAX_ATTACHMENTS = 6;
const MAX_FILE_BYTES = 15 * 1024 * 1024;
const MAX_IMAGE_EDGE = 2000;           // keeps receipt and statement text legible
const IMAGE_QUALITY = 0.82;
const KEEP_ORIGINAL_IMAGE_BYTES = 400 * 1024;
const MAX_TEXT_BYTES = 256 * 1024;
const TEXT_FILE_PATTERN = /\.(txt|md|markdown|csv|tsv|json|log|ya?ml|xml|html?|py|js|ts|css|ini|cfg|conf|sql)$/i;
// Relays that predate the upload endpoint still carry small files inline in
// the command payload. That row lives in D1, so keep the budget tight.
const INLINE_ATTACHMENT_BUDGET = 700 * 1024;
const ATTACHMENT_ONLY_PROMPT = "Have a look at the attached file(s) and help me with this.";
const REMOTE_API_ORIGIN = location.hostname.endsWith("github.io")
  ? "https://aios-remote-control.contact-wallerstedt.chatgpt.site"
  : "";

function apiUrl(path) {
  return `${REMOTE_API_ORIGIN}${path}`;
}

const prefs = {
  get(key, fallback) { const value = localStorage.getItem(key); return value === null ? fallback : value; },
  set(key, value) { localStorage.setItem(key, String(value)); },
  bool(key, fallback) { const value = localStorage.getItem(key); return value === null ? fallback : value === "1"; }
};

const state = {
  token: prefs.get("aios_remote_token", ""),
  privateCode: prefs.get("aios_private_code", ""),
  machines: [],
  machineId: prefs.get("aios_machine_id", ""),
  monitorId: "",
  cursor: 0,
  feedVersion: 0,
  model: prefs.get("aios_model", "luna"),
  plannerModel: prefs.get("aios_planner_model", "sol"),
  lastPlannerModel: prefs.get("aios_planner_last_model", "sol"),
  effort: prefs.get("aios_effort", "low"),
  steps: Number(prefs.get("aios_steps", 30)),
  shell: prefs.bool("aios_shell", true),
  detailed: prefs.bool("aios_detailed", true),
  haptics: prefs.bool("aios_haptics", true),
  keepAwake: prefs.bool("aios_awake", false),
  notify: prefs.bool("aios_notify", false),
  pushReady: false,
  background: prefs.get("aios_background", "black"),
  providerMode: "codex",
  hasOpenAIKey: false,
  codexAvailable: false,
  transportPublicKey: "",
  screenOpen: prefs.bool("aios_screen_open", true),
  timers: [],
  frameTimer: null,
  streamLeaseTimer: null,
  frameBusy: false,
  frameTimes: [],
  displayFps: 0,
  busy: false,
  loading: false,
  running: false,
  forceNewPrompt: false,
  frameUrl: "",
  frameStamp: "",
  frameSeq: 0,
  frameUpdatedAt: 0,
  frameAt: 0,
  installPrompt: null,
  wakeLock: null,
  stickBottom: true,
  lastStep: null,
  history: [],
  liveRun: null,
  sessionMachineId: "",
  viewingRunId: "",
  replaying: false,
  draining: false,
  clarifierTimer: null,
  clarifierTimeout: null,
  clarifierSequence: 0,
  clarifierRequestId: "",
  clarifierDraft: "",
  clarifierQuestions: [],
  clarifierLoading: false,
  attachments: []
};

function applyAppearance(background = state.background) {
  state.background = background === "red" ? "red" : "black";
  prefs.set("aios_background", state.background);
  document.documentElement.dataset.background = state.background;
  const color = state.background === "red" ? "#f0243a" : "#08080a";
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", color);
  const label = state.background === "red" ? "Red" : "Black";
  const value = $("#appearanceValue");
  if (value) value.textContent = label;
  $$(".theme-option").forEach((button) => {
    button.classList.toggle("on", button.dataset.background === state.background);
  });
}

applyAppearance();

/* ── helpers ──────────────────────────────────────────── */

function buzz(pattern = 8) {
  if (state.haptics && navigator.vibrate) navigator.vibrate(pattern);
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.remove("show"), 2600);
}

async function copyText(value) {
  if (!value) return;
  try {
    await navigator.clipboard.writeText(value);
  } catch {
    const area = document.createElement("textarea");
    area.value = value;
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  buzz();
  toast("Copied to clipboard");
}

function bytesFromBase64Url(value) {
  const normalized = String(value || "").replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(normalized + "=".repeat((4 - normalized.length % 4) % 4));
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

function bytesToBase64Url(value) {
  const bytes = value instanceof Uint8Array ? value : new Uint8Array(value);
  let binary = "";
  for (let index = 0; index < bytes.length; index += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function encryptSecretForMachine(secret) {
  if (!window.crypto?.subtle || !state.transportPublicKey) {
    throw new Error("Update aiOS on this computer before saving an API key.");
  }
  const encoder = new TextEncoder();
  const remoteKey = await crypto.subtle.importKey(
    "raw",
    bytesFromBase64Url(state.transportPublicKey),
    { name: "ECDH", namedCurve: "P-256" },
    false,
    []
  );
  const ephemeral = await crypto.subtle.generateKey(
    { name: "ECDH", namedCurve: "P-256" },
    true,
    ["deriveBits"]
  );
  const shared = await crypto.subtle.deriveBits(
    { name: "ECDH", public: remoteKey },
    ephemeral.privateKey,
    256
  );
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const material = await crypto.subtle.importKey("raw", shared, "HKDF", false, ["deriveKey"]);
  const key = await crypto.subtle.deriveKey(
    {
      name: "HKDF",
      hash: "SHA-256",
      salt: iv,
      info: encoder.encode("aiOS Phone API Key v1")
    },
    material,
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt"]
  );
  const ciphertext = await crypto.subtle.encrypt(
    {
      name: "AES-GCM",
      iv,
      additionalData: encoder.encode("aiOS Phone API Key")
    },
    key,
    encoder.encode(secret)
  );
  const ephemeralPublicKey = await crypto.subtle.exportKey("raw", ephemeral.publicKey);
  return {
    version: 1,
    ephemeral_public_key: bytesToBase64Url(ephemeralPublicKey),
    iv: bytesToBase64Url(iv),
    ciphertext: bytesToBase64Url(ciphertext)
  };
}

function ago(timestamp) {
  const seconds = Math.max(0, Math.floor((Date.now() - Number(timestamp || 0)) / 1000));
  if (seconds < 10) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  return hours < 24 ? `${hours}h ago` : `${Math.floor(hours / 24)}d ago`;
}

function clockTime(value) {
  return new Date(Number(value || Date.now())).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function icon(name, className = "ic") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", className);
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.appendChild(use);
  return svg;
}

function labelOf(list, id) {
  return (list.find((item) => item.id === id) || list[0]).name;
}

/* ── session ──────────────────────────────────────────── */

function saveSession(token, code = "") {
  state.token = token;
  prefs.set("aios_remote_token", token);
  if (code) {
    state.privateCode = code;
    prefs.set("aios_private_code", code);
  }
}

function clearSession() {
  // Locking the phone must also stop the relay waking it.
  unsubscribePush(state.token).catch(() => {});
  state.token = "";
  state.privateCode = "";
  localStorage.removeItem("aios_remote_token");
  localStorage.removeItem("aios_private_code");
  localStorage.removeItem("aios_machine_id");
  stopPolling();
  closeSheets();
  showAuth();
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
  if (options.body && !(options.body instanceof Blob)) headers.set("Content-Type", "application/json");
  const { timeoutMs = 15_000, ...fetchOptions } = options;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(apiUrl(path), {
      ...fetchOptions,
      headers,
      signal: fetchOptions.signal || controller.signal
    });
    const data = response.headers.get("content-type")?.includes("application/json") ? await response.json() : null;
    if (response.status === 401 && state.token && !path.includes("/account/")) clearSession();
    if (!response.ok) {
      // Callers need the status, not just the copy: an older relay answers
      // "Not found." to endpoints it has never heard of.
      const failure = new Error(data?.error || `Request failed (${response.status})`);
      failure.status = response.status;
      throw failure;
    }
    return data;
  } catch (error) {
    if (error?.name === "AbortError") throw new Error("Request timed out.");
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function showAuth() {
  $("#authView").classList.remove("hidden");
  $("#appView").classList.add("hidden");
  $("#welcomePane").classList.remove("hidden");
  $("#loginForm").classList.add("hidden");
  $("#createdPane").classList.add("hidden");
}

function showApp() {
  $("#authView").classList.add("hidden");
  $("#appView").classList.remove("hidden");
  $("#pairCode").textContent = state.privateCode || "Unlock with your private code first";
  startPolling();
  refreshPushSubscription();
}

/* ── overlays (sheets + immersive viewer) ─────────────── */

let overlayCloser = null;

function openOverlay(close) {
  if (overlayCloser) overlayCloser();
  overlayCloser = close;
  history.pushState({ aiosOverlay: true }, "");
}

function dismissOverlay() {
  if (!overlayCloser) return;
  const close = overlayCloser;
  overlayCloser = null;
  close();
  if (history.state?.aiosOverlay) history.back();
}

window.addEventListener("popstate", () => {
  if (!overlayCloser) return;
  const close = overlayCloser;
  overlayCloser = null;
  close();
});

const SHEET_CLOSE_MS = 300;
const sheetUi = { closingTimer: null, drag: null, scrollY: 0, docListeners: null };

function sheetMobile() {
  return window.matchMedia("(max-width: 859px)").matches;
}

function sheetOpen() {
  return Boolean($(".sheet.open"));
}

/** iOS keeps scrolling the chat behind a fixed sheet unless the page is locked. */
function setSheetScrollLock(locked) {
  const root = document.documentElement;
  if (locked) {
    if (!sheetUi.scrollY) sheetUi.scrollY = window.scrollY || 0;
    root.classList.add("sheet-open");
    document.body.style.top = `-${sheetUi.scrollY}px`;
    return;
  }
  root.classList.remove("sheet-open");
  document.body.style.top = "";
  const y = sheetUi.scrollY || 0;
  sheetUi.scrollY = 0;
  window.scrollTo(0, y);
}

function clearSheetInline(sheet) {
  if (!sheet) return;
  sheet.style.transform = "";
  sheet.style.transition = "";
  sheet.classList.remove("dragging");
}

function setBackdropOpen(open) {
  const backdrop = $("#backdrop");
  if (!backdrop) return;
  backdrop.style.opacity = "";
  if (open) {
    backdrop.classList.remove("hidden");
    // Next frame so the opacity transition actually runs.
    requestAnimationFrame(() => backdrop.classList.add("open"));
    return;
  }
  backdrop.classList.remove("open");
  clearTimeout(sheetUi.closingTimer);
  sheetUi.closingTimer = setTimeout(() => {
    if (!$(".sheet.open")) backdrop.classList.add("hidden");
  }, SHEET_CLOSE_MS);
}

function openSheet(id) {
  const replacingSheet = overlayCloser === hideSheets;
  clearTimeout(sheetUi.closingTimer);
  $$(".sheet").forEach((sheet) => {
    const want = sheet.id === id;
    clearSheetInline(sheet);
    sheet.classList.toggle("open", want);
    sheet.setAttribute("aria-hidden", want ? "false" : "true");
    if (want) {
      const body = sheet.querySelector(".sheet-body");
      if (body) body.scrollTop = 0;
    }
  });
  setBackdropOpen(true);
  setSheetScrollLock(true);
  if (sheetMobile()) attachSheetDocListeners();
  buzz();
  if (!replacingSheet) openOverlay(hideSheets);
}

function hideSheets() {
  sheetUi.drag = null;
  detachSheetDocListeners();
  $$(".sheet").forEach((sheet) => {
    clearSheetInline(sheet);
    sheet.classList.remove("open");
    sheet.setAttribute("aria-hidden", "true");
  });
  setBackdropOpen(false);
  if (!sheetOpen()) setSheetScrollLock(false);
}

function closeSheets() {
  if (overlayCloser === hideSheets) dismissOverlay();
  else hideSheets();
}

function sheetDragInteractive(target) {
  return Boolean(target?.closest?.(
    "button, input, textarea, select, label, a, .option, .row, .choice-chip, .theme-option, .history-row, .code-chip, .text-input, .switch"
  ));
}

function sheetPointY(event) {
  if (event.touches?.length) return event.touches[0].clientY;
  if (event.changedTouches?.length) return event.changedTouches[0].clientY;
  return event.clientY;
}

function sheetApplyDrag(offset) {
  const drag = sheetUi.drag;
  if (!drag) return;
  const sheet = drag.sheet;
  const y = Math.max(0, offset);
  drag.offset = y;
  sheet.style.transform = `translate3d(0, ${y}px, 0)`;
  const backdrop = $("#backdrop");
  if (backdrop) backdrop.style.opacity = String(Math.max(0.12, 1 - y / 420));
}

function sheetBodyScrollable(body) {
  return Boolean(body && body.scrollHeight > body.clientHeight + 2);
}

function sheetCancelDrag() {
  const drag = sheetUi.drag;
  if (!drag) return;
  sheetUi.drag = null;
  drag.sheet.classList.remove("dragging");
  document.documentElement.classList.remove("sheet-dragging");
}

function sheetFinishDrag() {
  const drag = sheetUi.drag;
  if (!drag) return;
  if (!drag.active) {
    sheetCancelDrag();
    return;
  }
  const sheet = drag.sheet;
  sheetUi.drag = null;
  sheet.classList.remove("dragging");
  document.documentElement.classList.remove("sheet-dragging");
  const backdrop = $("#backdrop");
  const threshold = Math.min(140, Math.max(64, sheet.offsetHeight * 0.16));
  const flicked = drag.offset > threshold || (drag.offset > 36 && drag.velocity > 0.65);
  if (flicked) {
    sheet.style.transition = "transform .24s cubic-bezier(.2, .9, .3, 1)";
    sheet.style.transform = "translate3d(0, 100%, 0)";
    if (backdrop) {
      backdrop.style.transition = "opacity .24s ease";
      backdrop.style.opacity = "0";
    }
    setTimeout(() => {
      clearSheetInline(sheet);
      if (backdrop) {
        backdrop.style.opacity = "";
        backdrop.style.transition = "";
      }
      closeSheets();
    }, 240);
    return;
  }
  sheet.style.transition = "transform .22s cubic-bezier(.2, .9, .3, 1)";
  sheet.style.transform = "translate3d(0, 0, 0)";
  if (backdrop) {
    backdrop.style.transition = "opacity .22s ease";
    backdrop.style.opacity = "";
  }
  setTimeout(() => {
    if (sheetUi.drag) return;
    sheet.style.transition = "";
    sheet.style.transform = "";
    if (backdrop) backdrop.style.transition = "";
  }, 230);
}

function sheetMoveDrag(event) {
  const drag = sheetUi.drag;
  if (!drag) return;
  const sheet = drag.sheet;
  const y = sheetPointY(event);
  const delta = y - drag.startY;
  if (drag.armed && !drag.active) {
    if (delta < 8) return;
    drag.active = true;
    drag.armed = false;
    drag.startY = y;
  }
  if (!drag.active) return;
  event.preventDefault();
  const now = performance.now();
  const dt = Math.max(1, now - drag.lastT);
  drag.velocity = (y - drag.lastY) / dt;
  drag.lastY = y;
  drag.lastT = now;
  sheetApplyDrag(y - drag.startY);
}

function sheetTouchGuard(event) {
  if (!sheetOpen()) return;
  if (sheetUi.drag) {
    sheetMoveDrag(event);
    return;
  }
  // Let the menu body scroll; block everything else (chat timeline, backdrop, etc.).
  if (event.target.closest?.(".sheet-body")) return;
  if (event.cancelable) event.preventDefault();
}

function attachSheetDocListeners() {
  if (sheetUi.docListeners) return;
  sheetUi.docListeners = { guard: sheetTouchGuard, finish: sheetFinishDrag };
  document.addEventListener("touchmove", sheetTouchGuard, { passive: false, capture: true });
  document.addEventListener("touchend", sheetFinishDrag, { capture: true });
  document.addEventListener("touchcancel", sheetFinishDrag, { capture: true });
}

function ensureSheetDocListeners() {
  if (sheetOpen() && sheetMobile()) attachSheetDocListeners();
}

function detachSheetDocListeners() {
  if (!sheetUi.docListeners) return;
  document.removeEventListener("touchmove", sheetTouchGuard, { capture: true });
  document.removeEventListener("touchend", sheetFinishDrag, { capture: true });
  document.removeEventListener("touchcancel", sheetFinishDrag, { capture: true });
  sheetUi.docListeners = null;
}

function sheetStartDrag(sheet, body, event, { fromHandle }) {
  if (!sheetMobile() || !sheet.classList.contains("open")) return false;
  if (sheetUi.drag) return false;
  if (!fromHandle) {
    if (sheetDragInteractive(event.target)) return false;
    if ((body.scrollTop || 0) > 0) return false;
    // Scrollable menus should scroll, not fight the dismiss gesture.
    if (sheetBodyScrollable(body)) return false;
  }
  const y = sheetPointY(event);
  sheetUi.drag = {
    sheet, fromHandle, armed: !fromHandle, active: fromHandle,
    startY: y, offset: 0, lastY: y, lastT: performance.now(), velocity: 0
  };
  sheet.classList.add("dragging");
  document.documentElement.classList.add("sheet-dragging");
  return true;
}

/** Split every bottom sheet into a sticky drag handle + scrollable body. */
function prepareSheets() {
  $$(".sheet").forEach((sheet) => {
    sheet.classList.remove("hidden");
    sheet.setAttribute("aria-hidden", "true");

    let handle = sheet.querySelector(":scope > .sheet-handle");
    if (!handle) {
      const grabber = sheet.querySelector(":scope > .grabber") || document.createElement("div");
      grabber.className = "grabber";
      handle = document.createElement("div");
      handle.className = "sheet-handle";
      handle.setAttribute("aria-hidden", "true");
      handle.appendChild(grabber);
      sheet.insertBefore(handle, sheet.firstChild);
    }

    let body = sheet.querySelector(":scope > .sheet-body");
    if (!body) {
      body = document.createElement("div");
      body.className = "sheet-body";
      while (handle.nextSibling) body.appendChild(handle.nextSibling);
      sheet.appendChild(body);
    }

    bindSheetGestures(sheet, handle, body);
  });
}

function bindSheetGestures(sheet, handle, body) {
  handle.addEventListener("touchstart", (event) => {
    if (event.touches.length !== 1) return;
    if (!sheetStartDrag(sheet, body, event, { fromHandle: true })) return;
    event.preventDefault();
    event.stopPropagation();
  }, { passive: false });

  body.addEventListener("touchstart", (event) => {
    if (event.touches.length !== 1) return;
    ensureSheetDocListeners();
    sheetStartDrag(sheet, body, event, { fromHandle: false });
  }, { passive: true });

  handle.addEventListener("mousedown", (event) => {
    if (event.button !== 0) return;
    if (!sheetStartDrag(sheet, body, event, { fromHandle: true })) return;
    event.preventDefault();
    const onMove = (moveEvent) => sheetMoveDrag(moveEvent);
    const onUp = () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      sheetFinishDrag();
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  });
}

prepareSheets();

const backdropEl = $("#backdrop");
backdropEl.addEventListener("click", closeSheets);
backdropEl.addEventListener("touchmove", (event) => event.preventDefault(), { passive: false });

/** Renders a bottom-sheet picker and returns the choice through `onPick`. */
function renderOptions(container, items, activeId, onPick) {
  container.replaceChildren();
  for (const item of items) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `option${item.id === activeId ? " on" : ""}`;
    if (item.icon) {
      const badge = document.createElement("span");
      badge.className = "dot" + (item.online ? " online" : "");
      button.appendChild(badge);
    }
    const text = document.createElement("span");
    text.className = "option-text";
    const name = document.createElement("strong");
    name.textContent = item.name;
    text.appendChild(name);
    if (item.note) {
      const note = document.createElement("small");
      note.textContent = item.note;
      text.appendChild(note);
    }
    button.appendChild(text);
    if (item.id === activeId) button.appendChild(icon("check", "ic tick"));
    button.addEventListener("click", () => { buzz(); onPick(item.id); });
    container.appendChild(button);
  }
}

function renderChoiceChips(container, items, activeId, onPick) {
  container.replaceChildren();
  for (const item of items) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `choice-chip${item.id === activeId ? " on" : ""}`;
    button.textContent = item.name;
    button.setAttribute("aria-pressed", item.id === activeId ? "true" : "false");
    button.addEventListener("click", () => { buzz(); onPick(item.id); });
    container.appendChild(button);
  }
}

/* ── machines ─────────────────────────────────────────── */

function currentMachine() {
  return state.machines.find((machine) => machine.id === state.machineId);
}

function monitorsOf(machine) {
  const list = machine?.status?.monitors;
  return Array.isArray(list) && list.length ? list : [{ id: "primary", name: "Main display" }];
}

function currentMonitor() {
  const monitors = monitorsOf(currentMachine());
  return monitors.find((monitor) => String(monitor.id) === String(state.monitorId)) || monitors[0];
}

/** The bridge sends labels like "Monitor 1  2560x1440 @ (0,0)" — keep the short part. */
function monitorName(monitor, index = 0) {
  const raw = String(monitor?.name || "").split(/\s{2,}/)[0].trim();
  return raw || `Display ${index + 1}`;
}

function selectMachine(id) {
  closeSheets();
  if (state.machineId === id) return;
  closeRun("ended");
  state.machineId = id;
  state.cursor = 0;
  state.feedVersion += 1;
  state.viewingRunId = "";
  $("#historyBanner").classList.add("hidden");
  state.monitorId = "";
  state.lastStep = null;
  state.frameStamp = "";
  state.frameSeq = 0;
  state.frameUpdatedAt = 0;
  prefs.set("aios_machine_id", id);
  resetTimeline("Loading this computer’s activity…");
  clearMarkers();
  clearClarifier();
  $("#screenImage").classList.remove("loaded");
  $("#screenPlaceholder").classList.remove("hidden");
  renderMachine();
  beginSession();
  requestStream();
  refreshFrame();
}

function renderMachine() {
  const machine = currentMachine();
  const hasMachines = state.machines.length > 0;
  $("#emptyState").classList.toggle("hidden", hasMachines);
  $("#workspace").classList.toggle("hidden", !hasMachines);
  $("#promptForm").classList.toggle("hidden", !hasMachines);
  if (!machine) return;

  const status = machine.status || {};
  const operator = status.operator || {};
  const runState = String(operator.state || status.state || "idle").toLowerCase();
  const running = machine.online && RUNNING_STATES.has(runState);
  const asking = Boolean(operator.asking);
  state.running = running;

  $("#machineName").textContent = machine.name;
  $("#machineDot").className = `dot${machine.online ? (running ? " busy" : " online") : ""}`;
  $("#machineMeta").textContent = machine.online
    ? (running ? (asking ? "Needs your answer" : "OPERATOR is working") : `${machine.platform || "Windows"} · ready`)
    : `Offline · last seen ${ago(machine.last_seen)}`;

  $("#runTitle").textContent = asking
    ? "Waiting for your answer"
    : running ? (operator.task || "Working on your task") : "Ready for a task";
  $("#runMeta").textContent = `${labelOf(MODELS, state.model)} · ${running ? "working" : "idle"}`;
  $("#runDot").classList.toggle("on", running);
  $("#stopBtn").classList.toggle("hidden", !running);
  $("#promptInput").placeholder = treatAsFollowUp() ? "Add a follow-up…" : "Ask your computer…";

  const monitors = monitorsOf(machine);
  if (!monitors.some((monitor) => String(monitor.id) === String(state.monitorId))) {
    state.monitorId = String(monitors[0].id);
  }
  $("#monitorLabel").textContent = monitorName(currentMonitor());
  const accounts = Array.isArray(status.codex_accounts) ? status.codex_accounts : [];
  const activeAccount = accounts.find((account) => account.active);
  $("#codexAccountValue").textContent = activeAccount?.label || "Not signed in";
  const ai = status.ai || {};
  state.providerMode = String(ai.provider_mode || operator.provider_mode || state.providerMode || "codex");
  if (!AI_PROVIDERS.some((provider) => provider.id === state.providerMode)) state.providerMode = "codex";
  state.hasOpenAIKey = Boolean(ai.has_openai_api_key);
  state.codexAvailable = Boolean(ai.codex_available || accounts.some((account) => account.logged_in));
  state.transportPublicKey = String(ai.transport_public_key || "");
  renderAIStatus();
  const update = status.update || {};
  $("#updateValue").textContent = update.message || "Auto-update on";

  applyWakeLock();
}

async function loadMachines() {
  if (state.loading) return;
  state.loading = true;
  try {
    const data = await api("/api/machines");
    state.machines = data.machines || [];
    if (!state.machines.some((machine) => machine.id === state.machineId)) {
      state.machineId = state.machines[0]?.id || "";
      if (state.machineId) prefs.set("aios_machine_id", state.machineId);
    }
    renderMachine();
    // First sight of a computer (boot, or one that just finished pairing)
    // opens its session: archive whatever is old, start on a clean feed.
    if (state.machineId && state.sessionMachineId !== state.machineId) beginSession();
  } catch (error) {
    if (state.token) console.debug(error);
  } finally {
    state.loading = false;
  }
}

/* ── live screen: zoom, pan, click markers ────────────── */

const viewer = {
  zoom: 1,
  fitWidth: 0,
  pinch: null,
  lastTap: 0,
  tapTimer: null,
  markers: []
};

const scrollBox = () => $("#viewerScroll");

function fitViewer() {
  const image = $("#screenImage");
  const box = scrollBox();
  if (!image.naturalWidth || !box.clientWidth) return;
  const ratio = image.naturalWidth / image.naturalHeight;
  let width = box.clientWidth;
  if (width / ratio > box.clientHeight) width = box.clientHeight * ratio;
  viewer.fitWidth = width;
  applyZoom();
}

function applyZoom() {
  $("#viewerCanvas").style.width = `${Math.round(viewer.fitWidth * viewer.zoom)}px`;
  const zoomed = viewer.zoom > 1.01;
  $("#zoomChip").textContent = `${Math.round(viewer.zoom * 100)}%`;
  $("#zoomChip").classList.toggle("hidden", !zoomed);
  $("#zoomResetBtn").classList.toggle("hidden", !zoomed);
}

function setZoom(next, focalX, focalY) {
  const box = scrollBox();
  const clamped = Math.min(6, Math.max(1, next));
  if (Math.abs(clamped - viewer.zoom) < 0.001) return;
  const rect = box.getBoundingClientRect();
  const originX = focalX === undefined ? rect.width / 2 : focalX - rect.left;
  const originY = focalY === undefined ? rect.height / 2 : focalY - rect.top;
  const contentX = box.scrollLeft + originX;
  const contentY = box.scrollTop + originY;
  const factor = clamped / viewer.zoom;
  viewer.zoom = clamped;
  applyZoom();
  box.scrollLeft = contentX * factor - originX;
  box.scrollTop = contentY * factor - originY;
}

function resetZoom() {
  viewer.zoom = 1;
  applyZoom();
  scrollBox().scrollTo({ left: 0, top: 0, behavior: "smooth" });
}

function bindViewerGestures() {
  const box = scrollBox();

  box.addEventListener("touchstart", (event) => {
    if (event.touches.length === 2) {
      const [a, b] = event.touches;
      viewer.pinch = {
        distance: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY),
        zoom: viewer.zoom
      };
      event.preventDefault();
    }
  }, { passive: false });

  box.addEventListener("touchmove", (event) => {
    if (!viewer.pinch || event.touches.length !== 2) return;
    event.preventDefault();
    const [a, b] = event.touches;
    const distance = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
    setZoom(viewer.pinch.zoom * (distance / viewer.pinch.distance), (a.clientX + b.clientX) / 2, (a.clientY + b.clientY) / 2);
  }, { passive: false });

  box.addEventListener("touchend", (event) => {
    if (event.touches.length < 2) viewer.pinch = null;
    if (event.touches.length || event.changedTouches.length !== 1) return;
    const now = Date.now();
    const touch = event.changedTouches[0];
    if (now - viewer.lastTap < 300) {
      viewer.lastTap = 0;
      clearTimeout(viewer.tapTimer);
      buzz();
      if (viewer.zoom > 1.01) resetZoom();
      else setZoom(2.6, touch.clientX, touch.clientY);
    } else {
      viewer.lastTap = now;
      // A single tap in fullscreen hides the commentary so you can watch the
      // bare screen. Delayed, so it never steals the double-tap zoom.
      clearTimeout(viewer.tapTimer);
      viewer.tapTimer = setTimeout(() => {
        if (cinema.on) cinemaMute(!$("#viewer").classList.contains("cinema-muted"));
      }, 310);
    }
  });

  box.addEventListener("wheel", (event) => {
    if (!event.ctrlKey && !event.metaKey) return;
    event.preventDefault();
    setZoom(viewer.zoom * (event.deltaY < 0 ? 1.12 : 0.89), event.clientX, event.clientY);
  }, { passive: false });

  box.addEventListener("dblclick", (event) => {
    if (viewer.zoom > 1.01) resetZoom();
    else setZoom(2.6, event.clientX, event.clientY);
  });

  new ResizeObserver(() => fitViewer()).observe(box);
}

function clearMarkers() {
  $("#markerLayer").replaceChildren();
  viewer.markers = [];
  $("#lastClickPill").classList.add("hidden");
}

/** Places a marker using screen-space coordinates from the desktop agent. */
function markClick(payload) {
  const x = Number(payload?.x);
  const y = Number(payload?.y);
  if (!Number.isFinite(x) || !Number.isFinite(y)) return;
  const monitor = currentMonitor();
  const image = $("#screenImage");
  const width = Number(monitor.width) || image.naturalWidth;
  const height = Number(monitor.height) || image.naturalHeight;
  if (!width || !height) return;

  const left = ((x - Number(monitor.left || 0)) / width) * 100;
  const top = ((y - Number(monitor.top || 0)) / height) * 100;
  if (left < -5 || left > 105 || top < -5 || top > 105) return;

  const marker = document.createElement("div");
  marker.className = "marker";
  marker.style.left = `${left}%`;
  marker.style.top = `${top}%`;
  marker.appendChild(document.createElement("i"));
  $("#markerLayer").appendChild(marker);
  viewer.markers.push(marker);
  // Only ever one or two on screen: a marker is "where it just pressed", not a
  // history. It shows the ripple, then clears itself out of the way.
  while (viewer.markers.length > 2) viewer.markers.shift().remove();
  setTimeout(() => marker.classList.add("faded"), MARKER_HOLD_MS);
  setTimeout(() => {
    marker.remove();
    viewer.markers = viewer.markers.filter((item) => item !== marker);
  }, MARKER_HOLD_MS + 360);

  const pill = $("#lastClickPill");
  pill.textContent = `${payload.button || "left"} click · ${Math.round(x)}, ${Math.round(y)}`;
  pill.classList.remove("hidden");
  clearTimeout(markClick.timer);
  markClick.timer = setTimeout(() => pill.classList.add("hidden"), 2600);
}

/* ── cinema: fullscreen live view with commentary ─────────
   The timeline answers "what happened". This answers "what is it doing right
   now", at a glance, without covering the screen it is talking about. */

const MARKER_HOLD_MS = 620;
const CINEMA_MAX_LINES = 7;
const cinema = { on: false, lines: [], seq: 0 };

/** `describe` keeps its own private copy of this; the overlay needs one too. */
const say = (value) => String(value || "").trim();

function cinemaMute(muted) {
  $("#viewer").classList.toggle("cinema-muted", muted);
}

function cinemaSet(on) {
  cinema.on = on;
  $("#cinema").hidden = !on;
  if (!on) {
    cinemaMute(false);
    return;
  }
  // Opening mid-run must not show an empty rail — replay the recent tail so
  // the overlay starts already caught up.
  const run = state.liveRun;
  cinema.lines = [];
  $("#cinemaTask").textContent = run && run.status === "open" ? run.task || "" : "";
  $("#cinemaStep").textContent = run && run.steps ? `step ${run.steps}` : "";
  cinemaStatus(state.running ? "working" : "idle");
  for (const event of (run?.events || []).slice(-30)) {
    if (String(event.type) === "run_start") continue;  // would wipe what we just seeded
    cinemaEvent(String(event.type || "").toLowerCase(), event.payload || {});
  }
  cinemaRender();
}

function cinemaStatus(kind, label) {
  if (!cinema.on) return;
  const node = $("#cinemaState");
  const words = { idle: "Idle", thinking: "Thinking", working: "Working",
                  waiting: "Needs you", done: "Finished", failed: "Stopped" };
  node.textContent = label || words[kind] || kind;
  node.className = `cinema-state ${kind === "thinking" ? "thinking" : ""} ${kind === "idle" ? "idle" : ""}`.trim();
}

function cinemaPush(tone, label, body) {
  const text_ = String(body || "").trim();
  if (!text_) return;
  cinema.lines.push({ id: ++cinema.seq, tone, label, body: text_.slice(0, 260) });
  if (cinema.lines.length > CINEMA_MAX_LINES) cinema.lines.shift();
  cinemaRender();
}

function cinemaRender() {
  if (!cinema.on) return;
  const feed = $("#cinemaFeed");
  feed.replaceChildren();
  const total = cinema.lines.length;
  cinema.lines.forEach((line, index) => {
    // Older lines dim as they rise, so the newest always reads first.
    const fromEnd = total - 1 - index;
    const age = fromEnd === 0 ? "" : fromEnd <= 1 ? "age1" : fromEnd <= 3 ? "age2" : "age3";
    const row = document.createElement("div");
    row.className = `cinema-line ${line.tone} ${age}`.trim();
    const tag = document.createElement("b");
    tag.textContent = line.label;
    row.append(tag, document.createTextNode(line.body));
    feed.appendChild(row);
  });
}

/** Fold one run event into the overlay. Mirrors the timeline, kept terse. */
function cinemaEvent(type, payload) {
  if (!cinema.on) return;
  if (type === "run_start" || type === "command") {
    cinema.lines = [];
    $("#cinemaTask").textContent = say(payload.task) || $("#cinemaTask").textContent;
    cinemaStatus("thinking");
    cinemaRender();
    return;
  }
  if (type === "step_begin") {
    $("#cinemaStep").textContent = payload.n ? `step ${payload.n}` : "";
    cinemaStatus("thinking");
    return;
  }
  if (type === "planning_begin") return cinemaStatus("thinking", "Planning");
  if (type === "plan") return cinemaPush("think", "Plan", say(payload.plan));
  if (type === "thought") {
    const spoken = say(payload.say);
    const thought = say(payload.thought) || say(payload.message);
    cinemaStatus("working");
    return cinemaPush(spoken ? "" : "think", spoken ? "OPERATOR" : "Thinking", spoken || thought);
  }
  if (type === "click_fx") {
    const button = say(payload.button) || "left";
    return cinemaPush("", `${button} click`,
      `${Math.round(Number(payload.x))}, ${Math.round(Number(payload.y))}`);
  }
  if (type === "action_done") {
    const result = payload.result || payload;
    const action = result.action || {};
    const name = say(action.type || payload.action) || "action";
    if (result.ok !== false && /^click|^double_click|^right_click/.test(name)) return;
    return cinemaPush(result.ok === false ? "err" : "", name.replace(/_/g, " "),
      say(result.detail) || say(result.output).slice(0, 200));
  }
  if (type === "verify_begin") return cinemaStatus("thinking", "Checking");
  if (type === "verified") {
    return cinemaPush(String(payload.verdict) === "pass" ? "ok" : "err",
      String(payload.verdict) === "pass" ? "Checked" : "Not done yet",
      say(payload.reason));
  }
  if (type === "ask") {
    cinemaStatus("waiting");
    return cinemaPush("err", "Needs you", say(payload.message));
  }
  if (type === "max_steps") {
    cinemaStatus("waiting");
    return cinemaPush("err", "Out of steps", say(payload.message));
  }
  if (type === "done") {
    cinemaStatus(payload.ok ? "done" : "failed");
    $("#cinemaStep").textContent = payload.steps ? `${payload.steps} steps` : "";
    return cinemaPush(payload.ok ? "ok" : "err", payload.ok ? "Finished" : "Ended",
      say(payload.message));
  }
  if (type === "error") return cinemaPush("err", "Error", say(payload.message || payload.detail));
}

async function refreshFrame() {
  const machine = currentMachine();
  if (!machine || state.frameBusy || document.hidden || !state.screenOpen) return;
  state.frameBusy = true;
  const monitor = encodeURIComponent(state.monitorId || "primary");
  try {
    const response = await fetch(apiUrl(`/api/machines/${encodeURIComponent(machine.id)}/frame/${monitor}?t=${Date.now()}`), {
      headers: { Authorization: `Bearer ${state.token}` },
      cache: "no-store"
    });
    if (!response.ok) { updateLive(); return; }
    const updatedAt = Number(response.headers.get("x-aios-updated-at") || 0);
    const sequence = Number(response.headers.get("x-aios-frame-seq") || 0);
    // Skip work only when the relay actually tells us the frame is unchanged.
    // A relay that reports neither header must never freeze the picture.
    if (updatedAt || sequence) {
      const stamp = `${updatedAt || ""}:${sequence}`;
      if (stamp === state.frameStamp) { updateLive(); return; }
      if (sequence && sequence <= state.frameSeq) { updateLive(); return; }
      if (!sequence && updatedAt <= state.frameUpdatedAt) { updateLive(); return; }
      state.frameStamp = stamp;
    }
    const blob = await response.blob();
    if (machine.id !== state.machineId) return;
    if (sequence) state.frameSeq = sequence;
    if (updatedAt) state.frameUpdatedAt = updatedAt;
    state.frameAt = Date.now();
    state.frameTimes.push(performance.now());
    const cutoff = performance.now() - 2000;
    state.frameTimes = state.frameTimes.filter((value) => value >= cutoff);
    if (state.frameTimes.length > 1) {
      const span = state.frameTimes[state.frameTimes.length - 1] - state.frameTimes[0];
      state.displayFps = span > 0 ? ((state.frameTimes.length - 1) * 1000) / span : 0;
    }
    const next = URL.createObjectURL(blob);
    const image = $("#screenImage");
    image.onload = () => {
      if (state.frameUrl) URL.revokeObjectURL(state.frameUrl);
      state.frameUrl = next;
      image.classList.add("loaded");
      $("#screenPlaceholder").classList.add("hidden");
      if (!viewer.fitWidth) fitViewer();
    };
    image.src = next;
    updateLive();
  } catch {
    /* A sleeping computer simply has no frame yet. */
  } finally {
    state.frameBusy = false;
  }
}

function updateLive() {
  const machine = currentMachine();
  const fresh = Date.now() - state.frameAt < 2500 && Boolean(machine?.online);
  $("#livePill").classList.toggle("on", fresh);
  const uplink = Number(machine?.status?.stream?.fps || 0);
  const fps = state.displayFps || uplink;
  const error = String(machine?.status?.stream?.error || "").trim();
  $("#liveText").textContent = fresh
    ? `${fps.toFixed(1)} FPS`
    : !machine?.online ? "OFFLINE" : error ? "NO SIGNAL" : "CONNECTING";
  if (!fresh) {
    // Say why the picture is missing instead of spinning forever.
    $("#screenPlaceholder").querySelector("span").textContent = !machine ? "Choose a computer"
      : !machine.online ? "That computer is offline"
      : error ? `Screen capture failed — ${error}`
      : "Waiting for the first screenshot";
  }
}

/* ── operator screenshots ─────────────────────────────── */

/* Every step screenshot the model was given is published by the bridge and
   pulled in here, so the timeline shows the real input and the exact spot
   OPERATOR clicked — not just a line of text about it. */
const shots = { cache: new Map(), order: [], observer: null };

function shotObserver() {
  if (shots.observer || !("IntersectionObserver" in window)) return shots.observer;
  shots.observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      shots.observer.unobserve(entry.target);
      loadShot(entry.target);
    }
  }, { root: $("#timeline"), rootMargin: "800px 0px" });
  return shots.observer;
}

function dropShot(cacheKey) {
  const record = shots.cache.get(cacheKey);
  if (!record) return;
  shots.cache.delete(cacheKey);
  for (const node of record.nodes) {
    if (!node.isConnected) continue;
    node.removeAttribute("src");
    node.classList.remove("ready");
    // Timeline images reload when scrolled back to; an open fullscreen view
    // has no observer root, so it refetches straight away.
    if (node.closest("#timeline")) armShot(node);
    else if (!$("#lightbox").classList.contains("hidden")) loadShot(node);
  }
  URL.revokeObjectURL(record.url);
}

async function shotUrl(key, machineId, node) {
  const cacheKey = `${machineId}:${key}`;
  const cached = shots.cache.get(cacheKey);
  if (cached) {
    if (node) cached.nodes.add(node);
    return cached.url;
  }
  const path = `/api/machines/${encodeURIComponent(machineId)}/frame/${encodeURIComponent(key)}`;
  const response = await fetch(apiUrl(path), { headers: { Authorization: `Bearer ${state.token}` } });
  if (!response.ok) throw new Error(`Screenshot unavailable (${response.status})`);
  const url = URL.createObjectURL(await response.blob());
  const record = { url, nodes: new Set(node ? [node] : []) };
  shots.cache.set(cacheKey, record);
  shots.order = shots.order.filter((item) => item !== cacheKey);
  shots.order.push(cacheKey);
  while (shots.order.length > 60) dropShot(shots.order.shift());
  return url;
}

function armShot(img) {
  const observer = shotObserver();
  if (observer) observer.observe(img);
  else loadShot(img);
}

async function loadShot(img, attempt = 0) {
  const key = img.dataset.shot;
  const machineId = img.dataset.machine;
  if (!key || !machineId) return;
  try {
    img.src = await shotUrl(key, machineId, img);
    img.classList.add("ready");
    img.closest(".shot")?.classList.remove("missing");
  } catch (error) {
    // The bridge uploads a screenshot while the event is already on its way,
    // so a first miss usually just means "not there yet".
    if (attempt < 4) {
      setTimeout(() => loadShot(img, attempt + 1), 900 * (attempt + 1));
      return;
    }
    img.closest(".shot")?.classList.add("missing");
  }
}

/** Reserve the right box before the screenshot bytes arrive. */
function shotRatio(payload) {
  const width = Number(payload.shot_w || payload.width || 0);
  const height = Number(payload.shot_h || payload.height || 0);
  return width > 0 && height > 0 ? `${width} / ${height}` : "";
}

/** Where a click landed inside its screenshot, as a percentage of the frame. */
function clickPoint(payload) {
  const x = Number(payload.x);
  const y = Number(payload.y);
  const width = Number(payload.width);
  const height = Number(payload.height);
  if (!Number.isFinite(x) || !Number.isFinite(y) || !width || !height) return null;
  const left = ((x - Number(payload.left || 0)) / width) * 100;
  const top = ((y - Number(payload.top || 0)) / height) * 100;
  if (left < 0 || left > 100 || top < 0 || top > 100) return null;
  return { left, top };
}

function shotCard(info) {
  const figure = document.createElement("figure");
  figure.className = "shot";
  const img = document.createElement("img");
  img.alt = info.point ? "Screenshot with the click marked" : "Screenshot given to OPERATOR";
  img.decoding = "async";
  img.dataset.shot = info.shot;
  img.dataset.machine = state.machineId;
  if (info.ratio) img.style.aspectRatio = info.ratio;
  figure.appendChild(img);
  if (info.point) {
    const dot = document.createElement("span");
    dot.className = "shot-dot";
    dot.style.left = `${info.point.left}%`;
    dot.style.top = `${info.point.top}%`;
    figure.appendChild(dot);
  }
  figure.addEventListener("click", (event) => {
    event.stopPropagation();
    openLightbox(info);
    buzz();
  });
  armShot(img);
  return figure;
}

function openLightbox(info) {
  const box = $("#lightbox");
  const image = $("#lightboxImage");
  const dot = $("#lightboxDot");
  image.removeAttribute("src");
  image.classList.remove("ready");
  image.dataset.shot = info.shot || "";
  image.dataset.machine = state.machineId;
  image.style.aspectRatio = info.ratio || "";
  $("#lightboxLabel").textContent = info.shotLabel || info.label || info.title || "Screenshot";
  dot.classList.toggle("hidden", !info.point);
  if (info.point) {
    dot.style.left = `${info.point.left}%`;
    dot.style.top = `${info.point.top}%`;
  }
  box.classList.remove("hidden");
  // A file you attached is already on this phone — no relay round trip.
  if (info.url) {
    image.src = info.url;
    image.classList.add("ready");
    return;
  }
  loadShot(image);
}

function closeLightbox() {
  $("#lightbox").classList.add("hidden");
  $("#lightboxImage").removeAttribute("src");
}

/* ── timeline ─────────────────────────────────────────── */

function treatAsFollowUp() {
  // A follow-up only makes sense while OPERATOR is actively working on the
  // open live run. An old/finished thread on screen (or History) must start
  // a fresh prompt — otherwise the phone sends followup into a dead run and
  // the chat never shows AI activity again.
  return Boolean(
    state.running
    && !state.forceNewPrompt
    && !state.viewingRunId
    && state.liveRun
    && state.liveRun.status === "open"
  );
}

/** Leave History so live events paint again. Keep the transcript when the
 *  user is sending a new task in the thread they are already looking at. */
function resumeLiveFeed({ keepTranscript = false } = {}) {
  const wasViewing = Boolean(state.viewingRunId);
  state.viewingRunId = "";
  $("#historyBanner")?.classList.add("hidden");
  if (!wasViewing || keepTranscript) return;
  const events = state.liveRun?.events || [];
  if (events.length) replayEvents(events);
  else resetTimeline("Ready. Ask your computer to do something.");
}

function resetTimeline(message) {
  const feed = $("#timeline");
  feed.replaceChildren();
  const empty = document.createElement("div");
  empty.className = "timeline-empty";
  empty.appendChild(icon("spark"));
  const text = document.createElement("p");
  text.textContent = message;
  empty.appendChild(text);
  feed.appendChild(empty);
  state.lastStep = null;
  hideJump();
}

function nearBottom() {
  const feed = $("#timeline");
  return feed.scrollHeight - feed.scrollTop - feed.clientHeight < 90;
}

function hideJump() {
  state.stickBottom = true;
  $("#jumpBtn").classList.add("hidden");
}

function showJump() {
  state.stickBottom = false;
  $("#jumpBtn").classList.remove("hidden");
}

function scrollFeed(force = false) {
  const feed = $("#timeline");
  if (force || state.stickBottom) {
    feed.scrollTop = feed.scrollHeight;
    hideJump();
  } else {
    showJump();
  }
}

async function ensureNotifyPermission() {
  if (!("Notification" in window)) return false;
  if (Notification.permission === "granted") return true;
  if (Notification.permission === "denied") return false;
  try {
    return (await Notification.requestPermission()) === "granted";
  } catch {
    return false;
  }
}

const isIOS = /iP(hone|ad|od)/.test(navigator.userAgent)
  || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

function installedToHomeScreen() {
  return window.matchMedia?.("(display-mode: standalone)").matches || navigator.standalone === true;
}

/** Why this phone cannot be woken, in words the user can act on. */
function pushBlocker() {
  if (!("serviceWorker" in navigator)) return "This browser can't run background alerts.";
  if (!("Notification" in window) || !("PushManager" in window)) {
    return isIOS
      ? "Add aiOS Remote to your Home Screen first — iPhone only allows alerts for installed apps. Tap Share, then Add to Home Screen."
      : "This browser can't show notifications.";
  }
  if (isIOS && !installedToHomeScreen()) {
    return "Open aiOS Remote from your Home Screen icon to turn alerts on.";
  }
  return "";
}

/** Register this phone with the relay so it can be woken while closed.
 *
 *  A sleeping phone runs no JavaScript: the in-page alert below only ever
 *  fires while the app is open, which is exactly when you don't need it.
 *  The relay pushes instead, and that needs a subscription bound to its
 *  application server key.
 */
async function subscribePush() {
  const blocker = pushBlocker();
  if (blocker) throw new Error(blocker);
  const registration = await navigator.serviceWorker.ready;
  const { key } = await api("/api/push/key");
  if (!key) throw new Error("The relay has no push key yet — try again in a moment.");
  const applicationServerKey = bytesFromBase64Url(key);
  let subscription = await registration.pushManager.getSubscription();
  if (subscription) {
    // A subscription made against a different key can never be decrypted.
    const current = subscription.options?.applicationServerKey;
    const same = current && bytesToBase64Url(new Uint8Array(current)) === key;
    if (!same) {
      await subscription.unsubscribe().catch(() => {});
      subscription = null;
    }
  }
  subscription = subscription || await registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey
  });
  const raw = subscription.toJSON();
  await api("/api/push/subscribe", {
    method: "POST",
    body: JSON.stringify({ endpoint: raw.endpoint, keys: raw.keys })
  });
  state.pushReady = true;
  return subscription;
}

/** `token` is passed explicitly when locking the phone: by the time this
 *  reaches the relay the session it needs is already gone from state. */
async function unsubscribePush(token = state.token) {
  state.pushReady = false;
  if (!("serviceWorker" in navigator)) return;
  try {
    const registration = await navigator.serviceWorker.ready;
    const subscription = await registration.pushManager?.getSubscription();
    if (!subscription) return;
    const endpoint = subscription.endpoint;
    await subscription.unsubscribe().catch(() => {});
    await api("/api/push/subscribe", {
      method: "DELETE",
      body: JSON.stringify({ endpoint }),
      headers: token ? { Authorization: `Bearer ${token}` } : {}
    }).catch(() => {});
  } catch { /* nothing subscribed */ }
}

/** Subscriptions expire and rotate; re-register quietly on every open. */
async function refreshPushSubscription() {
  if (!state.notify || !state.token) return;
  try {
    await subscribePush();
  } catch (error) {
    console.debug(error);
    state.pushReady = false;
  }
}

/** Rich PWA alerts when OPERATOR needs you or finishes — only if enabled. */
async function pushNotify(title, body, {
  tag = "aios-remote",
  requireInteraction = false,
  actions = []
} = {}) {
  if (!state.notify || state.replaying) return;
  // The relay already pushed this one to the phone itself — showing it again
  // from the page would double every alert.
  if (state.pushReady) return;
  if (document.visibilityState === "visible" && document.hasFocus()) return;
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  const machine = currentMachine();
  const machineName = machine?.name || machine?.hostname || "Your PC";
  const task = String(state.liveRun?.prompt || state.liveRun?.task || "").trim();
  const detail = String(body || "").trim();
  const lines = [detail, task ? `Task: ${task.slice(0, 100)}` : "", `On ${machineName}`]
    .filter(Boolean);
  const options = {
    body: lines.join("\n").slice(0, 280),
    tag,
    renotify: true,
    requireInteraction,
    icon: "icons/aios-icon-192.png",
    badge: "icons/aios-icon-192.png",
    data: { url: "./", tag, machineId: machine?.id || "", title },
  };
  if (actions.length) options.actions = actions.slice(0, 2);
  try {
    const ready = await navigator.serviceWorker?.ready;
    if (ready?.showNotification) {
      await ready.showNotification(title, options);
      return;
    }
  } catch { /* fall through */ }
  try { new Notification(title, options); } catch { /* unsupported */ }
}

async function continueRun(extraSteps = state.steps) {
  const steps = Math.max(1, Math.min(200, Number(extraSteps) || state.steps || 30));
  backToLive();
  buzz(12);
  notePrompt(`Continue (+${steps} steps)`);
  addEvent({ type: "prompt", payload: { message: `Continue (+${steps} steps)` } });
  showThinkingSoon();
  try {
    await sendCommand("followup", {
      prompt: "Continue — keep going from where you left off.",
      model: state.model,
      planner_model: state.plannerModel,
      max_steps: steps,
      reasoning_effort: state.effort,
      shell: state.shell
    });
  } catch (error) {
    clearThinkingPlaceholder();
    toast(error.message);
  }
}

/** What the finished run cost, as priced by the PC that ran it. */
function costLine(cost) {
  if (!cost || typeof cost !== "object") return "";
  const usd = Number(cost.usd || 0);
  const parts = [];
  if (cost.priced) parts.push(`≈ ${usd < 1 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(2)}`}`);
  if (Number(cost.plan_requests || 0)) {
    if (cost.plan_usage_measured) {
      const percent = Number(cost.plan_usage_percent || 0);
      parts.push(`${percent > 0 ? `≈ ${percent.toLocaleString()}%` : "<1%"} of your ChatGPT plan this run`);
    } else {
      parts.push(`${Number(cost.plan_tokens || 0).toLocaleString()} tokens on your ChatGPT plan`);
    }
    if (cost.plan_window_used_percent !== undefined && cost.plan_window_used_percent !== null) {
      parts.push(`${Number(cost.plan_window_used_percent).toLocaleString()}% of the current window used`);
    }
  }
  const unpriced = Array.isArray(cost.unpriced) ? cost.unpriced : [];
  if (unpriced.length) parts.push(`no price set for ${unpriced.join(", ")}`);
  return parts.join(" · ");
}

/** Turns a raw agent event into the shape the timeline renders. */
function describe(event) {
  const payload = event.payload || {};
  const type = String(event.type || "log").toLowerCase();
  const text = (value) => String(value || "").trim();

  if (type === "prompt" || /follow.?up/.test(type)) {
    return {
      kind: "user",
      body: text(payload.message || payload.text || payload.prompt),
      files: Array.isArray(payload.files) ? payload.files : []
    };
  }
  if (type === "step_begin") return { kind: "step", step: payload.n };
  if (type === "screenshot") {
    const shot = text(payload.shot);
    const step = payload.n ? ` · step ${payload.n}` : "";
    if (!shot) return { kind: "entry", tone: "muted", glyph: "monitor", title: "Looked at the screen" };
    return {
      kind: "entry", tone: "shot", glyph: "monitor",
      title: `Screen sent to the AI${step}`,
      shot, ratio: shotRatio(payload), shotLabel: `Input screenshot${step}`
    };
  }
  if (type === "command") return { kind: "entry", tone: "muted", glyph: "bolt", title: "Task received" };
  if (type === "run_start") return { kind: "entry", tone: "muted", glyph: "bolt", title: text(payload.task) || "Task started" };
  if (type === "planning_begin") return { kind: "entry", tone: "think", glyph: "spark", title: `Planning with ${text(payload.model) || "planner"}` };
  if (type === "plan") {
    const todo = Array.isArray(payload.todo) ? payload.todo : [];
    const checks = Array.isArray(payload.done_when) ? payload.done_when : [];
    return {
      kind: "entry", tone: "think", glyph: "spark",
      title: todo.length ? `Plan ready · ${todo.length} steps` : "Plan ready",
      body: text(payload.plan),
      list: todo.map((item, index) => `${index + 1}. ${item}`),
      extra: checks.length ? `Done when: ${checks.join(" · ")}` : ""
    };
  }
  if (type === "verify_begin") {
    return { kind: "entry", tone: "think muted", glyph: "check", title: "Checking it really did the task" };
  }
  if (type === "verified") {
    const passed = String(payload.verdict) === "pass";
    const missing = Array.isArray(payload.missing) ? payload.missing : [];
    return {
      kind: "entry", tone: passed ? "ok" : "err", glyph: passed ? "check" : "warn",
      title: passed ? "Checked — the task is done" : "Not done yet",
      body: text(payload.reason),
      list: missing.map((item) => `Still missing: ${item}`)
    };
  }
  if (type === "debug_dir" || type === "step_end") return null;

  if (type === "thought") {
    const say = text(payload.say);
    const thought = text(payload.thought);
    const message = text(payload.message);
    if (say) return { kind: "entry", tone: "say", glyph: "spark", title: "OPERATOR", body: say, extra: state.detailed ? thought : "" };
    return { kind: "entry", tone: "think muted", glyph: "spark", title: "Thinking", body: thought || message };
  }
  if (type === "click_fx") {
    const button = text(payload.button) || "left";
    const coords = `${Math.round(Number(payload.x))}, ${Math.round(Number(payload.y))}`;
    return {
      kind: "entry", tone: "click", glyph: "cursor",
      title: `${button} click`,
      mono: coords,
      click: payload,
      shot: text(payload.shot),
      ratio: shotRatio(payload),
      point: clickPoint(payload),
      shotLabel: `${button} click at ${coords}`
    };
  }
  if (type === "action_done") {
    const result = payload.result || payload;
    const action = result.action || {};
    const name = text(action.type || payload.action) || "action";
    const ok = result.ok !== false;
    // A successful click already has its own entry with coordinates.
    if (ok && /^click|^double_click|^right_click/.test(name)) return null;
    return {
      kind: "entry", tone: ok ? "act" : "err", glyph: ok ? "bolt" : "warn",
      title: name.replace(/_/g, " "),
      body: text(result.detail),
      mono: text(result.output).slice(0, 400),
      hint: result.elapsed_ms ? `${result.elapsed_ms} ms` : ""
    };
  }
  if (type === "ask") {
    return { kind: "entry", tone: "ask", glyph: "warn", title: "OPERATOR needs you", body: text(payload.message) };
  }
  if (type === "max_steps") {
    return {
      kind: "entry", tone: "ask continue-cta", glyph: "warn",
      title: "Out of steps",
      body: text(payload.message) || "Used the step budget before finishing.",
      continueSteps: Number(payload.steps) || state.steps || 30,
      hint: payload.steps ? `${payload.steps} steps used` : ""
    };
  }
  if (type === "done") {
    const ok = Boolean(payload.ok);
    const stopped = /stop/i.test(text(payload.message));
    const usage = payload.usage || {};
    const usageText = Number(usage.requests || 0)
      ? `${Number(usage.input_tokens || 0).toLocaleString()} input + ${Number(usage.output_tokens || 0).toLocaleString()} output tokens · ${Number(usage.requests || 0)} calls`
      : "";
    return {
      kind: "entry", tone: ok ? "ok" : "err", glyph: ok ? "check" : "stop",
      title: ok ? (payload.verified ? "Finished · checked" : "Finished")
        : stopped ? "Stopped" : "Run ended",
      body: text(payload.message),
      cost: costLine(payload.cost),
      mono: usageText,
      hint: payload.steps ? `${payload.steps} steps` : ""
    };
  }
  if (type === "error") {
    return { kind: "entry", tone: "err", glyph: "warn", title: text(payload.title) || "Error", body: text(payload.message || payload.detail) };
  }
  const body = text(payload.msg || payload.message || payload.text || payload.detail);
  if (!body) return null;
  return { kind: "entry", tone: "muted", glyph: "bolt", title: body.slice(0, 120) };
}

/** Text of a sent bubble, ignoring any attachment chips it carries. */
function bubbleText(node) {
  return (node.querySelector(".msg-text") || node.querySelector(".msg-bubble"))?.textContent ?? "";
}

/** Thumbnails inside a sent bubble. Previews are local object URLs, so a
 *  replayed run from storage shows a named chip instead of a broken image. */
function attachmentStrip(files) {
  const strip = document.createElement("div");
  strip.className = "msg-shots";
  for (const file of files) {
    const name = String(file.name || "attachment");
    if (file.kind === "image" && file.url) {
      const image = document.createElement("img");
      image.src = file.url;
      image.alt = name;
      image.loading = "lazy";
      image.addEventListener("click", (event) => {
        event.stopPropagation();
        openLightbox({ url: file.url, label: name });
      });
      strip.appendChild(image);
      continue;
    }
    const chip = document.createElement("span");
    chip.className = "msg-file";
    chip.appendChild(icon(file.kind === "image" ? "image" : "file"));
    const label = document.createElement("span");
    label.textContent = name;
    chip.appendChild(label);
    strip.appendChild(chip);
  }
  return strip;
}

function clearThinkingPlaceholder() {
  $("#timeline")?.querySelectorAll(".entry.optimistic-think").forEach((node) => node.remove());
}

/** Instant feedback after you hit send — full-colour bubble + Thinking, no "sending". */
function showThinkingSoon() {
  clearThinkingPlaceholder();
  const feed = $("#timeline");
  if (!feed) return;
  feed.querySelector(".timeline-empty")?.remove();
  const entry = document.createElement("article");
  entry.className = "entry think muted optimistic-think";
  const badge = document.createElement("div");
  badge.className = "entry-icon";
  badge.appendChild(icon("spark"));
  const body = document.createElement("div");
  body.className = "entry-body";
  const head = document.createElement("div");
  head.className = "entry-title";
  const title = document.createElement("strong");
  title.textContent = "Thinking";
  const time = document.createElement("time");
  time.textContent = clockTime(Date.now());
  head.append(title, time);
  body.appendChild(head);
  entry.append(badge, body);
  feed.appendChild(entry);
  state.stickBottom = true;
  scrollFeed();
  $("#runTitle").textContent = "OPERATOR is thinking";
  $("#runMeta").textContent = `${labelOf(MODELS, state.model)} · thinking`;
  $("#runDot").classList.add("on");
  if (cinema.on) cinemaStatus("thinking");
}

function addEvent(event) {
  // Replaying history must not move the live screen or buzz the phone.
  if (!state.replaying) {
    if (event.type === "click_fx") markClick(event.payload || {});
    cinemaEvent(String(event.type || "").toLowerCase(), event.payload || {});
    if (event.type === "step_begin") $("#runMeta").textContent = `${labelOf(MODELS, state.model)} · step ${(event.payload || {}).n || ""}`;
    if (event.type === "ask") {
      buzz([16, 60, 16]);
      pushNotify("OPERATOR needs your input", (event.payload || {}).message || "Waiting for your answer", {
        tag: "aios-ask",
        requireInteraction: true,
        actions: [
          { action: "open", title: "Open chat" },
          { action: "dismiss", title: "Dismiss" }
        ]
      });
    }
    if (event.type === "max_steps") {
      buzz([16, 60, 16]);
      pushNotify("OPERATOR needs more steps", (event.payload || {}).message || "Continue the run?", {
        tag: "aios-max-steps",
        requireInteraction: true,
        actions: [
          { action: "open", title: "Continue in chat" },
          { action: "dismiss", title: "Dismiss" }
        ]
      });
    }
    if (event.type === "done") {
      buzz([12, 40, 12]);
      const payload = event.payload || {};
      const steps = payload.steps ? `${payload.steps} steps` : "";
      pushNotify(
        payload.ok ? "OPERATOR finished" : "OPERATOR run ended",
        [payload.message || (payload.ok ? "Task complete" : "The run stopped"), steps]
          .filter(Boolean).join(" · "),
        {
          tag: "aios-done",
          actions: [{ action: "open", title: "Open chat" }]
        }
      );
    }
  }

  const info = describe(event);
  if (!info) return;
  const feed = $("#timeline");
  feed.querySelector(".timeline-empty")?.remove();
  const wasBottom = nearBottom();
  const stamp = Number(event.created_at || Date.now());

  // Real agent activity replaces the optimistic Thinking row.
  if (!state.replaying && info.kind !== "user") clearThinkingPlaceholder();

  if (info.kind === "step") {
    if (state.lastStep === info.step) return;
    state.lastStep = info.step;
    const rule = document.createElement("div");
    rule.className = "step-rule";
    rule.textContent = `Step ${info.step ?? ""}`.trim();
    feed.appendChild(rule);
  } else if (info.kind === "user") {
    const files = info.files || [];
    if (!info.body && !files.length) return;
    // Already painted optimistically when you hit send — keep it full colour,
    // keep its thumbnails, and just refresh the clock when the PC echoes back.
    const existing = [...feed.querySelectorAll(".msg")]
      .find((node) => bubbleText(node) === info.body);
    if (existing) {
      existing.classList.remove("pending");
      const clock = existing.querySelector("time");
      if (clock) clock.textContent = clockTime(stamp);
      return;
    }
    const row = document.createElement("div");
    row.className = "msg";
    const bubble = document.createElement("div");
    bubble.className = "msg-bubble";
    if (files.length) bubble.appendChild(attachmentStrip(files));
    const message = document.createElement("span");
    message.className = "msg-text";
    message.textContent = info.body;
    bubble.appendChild(message);
    const time = document.createElement("time");
    time.textContent = clockTime(stamp);
    row.append(bubble, time);
    row.addEventListener("click", () => {
      $("#promptInput").value = info.body;
      autoGrow();
      $("#promptInput").focus();
    });
    feed.appendChild(row);
  } else {
    const entry = document.createElement("article");
    entry.className = `entry ${info.tone}`;
    const badge = document.createElement("div");
    badge.className = "entry-icon";
    badge.appendChild(icon(info.glyph));
    const body = document.createElement("div");
    body.className = "entry-body";
    const head = document.createElement("div");
    head.className = "entry-title";
    const title = document.createElement("strong");
    title.textContent = info.title;
    head.appendChild(title);
    const time = document.createElement("time");
    time.textContent = info.hint ? `${clockTime(stamp)} · ${info.hint}` : clockTime(stamp);
    head.appendChild(time);
    body.appendChild(head);
    if (info.body) {
      const paragraph = document.createElement("p");
      paragraph.textContent = info.body;
      body.appendChild(paragraph);
    }
    if (info.cost) {
      const cost = document.createElement("p");
      cost.className = "entry-cost";
      cost.textContent = info.cost;
      body.appendChild(cost);
    }
    if (info.list?.length) {
      const list = document.createElement("ul");
      list.className = "entry-list";
      for (const item of info.list) {
        const row = document.createElement("li");
        row.textContent = item;
        list.appendChild(row);
      }
      body.appendChild(list);
    }
    if (info.mono) {
      const mono = document.createElement("p");
      mono.className = "mono";
      mono.textContent = info.mono;
      body.appendChild(mono);
    }
    if (info.extra) {
      const extra = document.createElement("p");
      extra.className = "mono";
      extra.textContent = info.extra;
      body.appendChild(extra);
    }
    if (info.shot) body.appendChild(shotCard(info));
    if (info.continueSteps) {
      const actions = document.createElement("div");
      actions.className = "entry-actions";
      const button = document.createElement("button");
      button.type = "button";
      button.className = "continue-btn";
      const extra = Math.max(1, Number(state.steps) || Number(info.continueSteps) || 30);
      button.textContent = `Continue · +${extra} steps`;
      button.addEventListener("click", async (clickEvent) => {
        clickEvent.stopPropagation();
        if (button.disabled) return;
        button.disabled = true;
        button.textContent = "Continuing…";
        try {
          await continueRun(extra);
        } catch {
          button.disabled = false;
          button.textContent = `Continue · +${extra} steps`;
        }
      });
      actions.appendChild(button);
      body.appendChild(actions);
    }
    entry.append(badge, body);
    if (info.click) {
      entry.addEventListener("click", () => {
        openScreen();
        markClick(info.click);
        buzz();
      });
    }
    feed.appendChild(entry);
  }

  while (feed.children.length > 250) feed.firstElementChild.remove();
  state.stickBottom = wasBottom || info.kind === "user";
  scrollFeed();
}

/* ── history ──────────────────────────────────────────── */

function loadHistory() {
  try {
    const saved = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
    state.history = Array.isArray(saved) ? saved : [];
  } catch {
    state.history = [];
  }
  state.liveRun = null;
}

/** Persist, shedding the oldest runs until the browser is happy to store it. */
function saveHistory() {
  let attempt = state.history.slice(0, MAX_HISTORY_RUNS);
  while (attempt.length) {
    const payload = JSON.stringify(attempt);
    if (payload.length <= HISTORY_BUDGET) {
      try {
        localStorage.setItem(HISTORY_KEY, payload);
        state.history = attempt;
        return;
      } catch {
        /* Quota — drop the oldest run and try again. */
      }
    }
    attempt = attempt.slice(0, -1);
  }
  try { localStorage.removeItem(HISTORY_KEY); } catch { /* private mode */ }
  state.history = attempt;
}

function machineHistory() {
  return state.history.filter((run) => run.machineId === state.machineId);
}

function closeRun(status = "ended", stamp = Date.now()) {
  const run = state.liveRun;
  state.liveRun = null;
  if (!run || run.status !== "open") return;
  run.status = status;
  run.endedAt = stamp;
  saveHistory();
}

function openRun(task, stamp) {
  closeRun("ended", stamp);
  const run = {
    id: `r${stamp.toString(36)}${Math.random().toString(36).slice(2, 6)}`,
    machineId: state.machineId,
    task: String(task || "").slice(0, 400),
    startedAt: stamp,
    endedAt: 0,
    status: "open",
    steps: 0,
    cost: "",
    events: [],
  };
  state.liveRun = run;
  state.history.unshift(run);
  saveHistory();
  return run;
}

/** Remember what you typed, even if the PC never manages to start the run. */
function notePrompt(text, { asFollowUp = false, files = [] } = {}) {
  const stamp = Date.now();
  // Follow-ups stay on the open live run. A new task always opens a fresh
  // run — reusing a stale "open" row after the PC went idle is what made the
  // next message in an old thread go quiet (events attached nowhere useful,
  // or run_start split the bookkeeping while the feed stayed frozen).
  const reuse = asFollowUp && state.liveRun && state.liveRun.status === "open";
  const run = reuse ? state.liveRun : openRun(text, stamp);
  if (!run.task) run.task = String(text || "").slice(0, 400);
  // Object URLs die with the page, so history keeps the names only.
  const saved = files.map((file) => ({ name: file.name, kind: file.kind }));
  run.events.push({ type: "prompt", payload: { message: text, files: saved }, created_at: stamp });
  saveHistory();
}

function recordEvent(event) {
  const type = String(event.type || "").toLowerCase();
  if (type === "clarification") return;
  const payload = event.payload || {};
  const stamp = Number(event.created_at || Date.now());
  if (type === "run_start") {
    // The task you typed already opened a run here. Let the PC's run_start
    // adopt it instead of splitting one task across two history rows.
    const pending = state.liveRun;
    // Same words means same task, however long the PC took to pick it up —
    // otherwise a queued run files its replies away from your message.
    const started = String(payload.task || "").trim();
    const sameTask = Boolean(started) && started === String(pending?.task || "").trim();
    const adoptable = pending && pending.status === "open" && !pending.started
      && (sameTask || stamp - pending.startedAt < 120_000);
    if (adoptable) pending.started = true;
    else openRun(payload.task, stamp).started = true;
  }
  let run = state.liveRun;
  if (!run || run.status !== "open") run = openRun(payload.task || "", stamp);
  if (type === "run_start" && payload.task) run.task = String(payload.task).slice(0, 400);
  run.events.push({ type, payload, created_at: stamp });
  if (run.events.length > MAX_RUN_EVENTS) run.events.splice(0, run.events.length - MAX_RUN_EVENTS);
  if (type === "step_begin") run.steps = Math.max(run.steps, Number(payload.n) || 0);
  if (type === "done") {
    run.steps = Number(payload.steps) || run.steps;
    run.cost = costLine(payload.cost);
    closeRun(payload.ok ? "done" : /stop/i.test(String(payload.message || "")) ? "stopped" : "failed", stamp);
    return;
  }
  saveHistory();
}

/** A run whose PC went idle without a done event is over, whatever it thinks. */
function closeStaleRun() {
  const run = state.liveRun;
  if (!run || run.status !== "open" || state.running) return;
  const last = run.events.length ? Number(run.events[run.events.length - 1].created_at || 0) : run.startedAt;
  if (Date.now() - Math.max(last, run.startedAt) > IDLE_RUN_CLOSE_MS) closeRun("ended");
}

function runLabel(run) {
  return run.task || (run.status === "open" ? "Running…" : "Untitled run");
}

function runMetaLine(run) {
  const parts = [ago(run.endedAt || run.startedAt)];
  if (run.steps) parts.push(`${run.steps} step${run.steps === 1 ? "" : "s"}`);
  if (run.cost) parts.push(run.cost.split(" · ")[0]);
  return parts.join(" · ");
}

function renderHistory() {
  const list = $("#historyList");
  const runs = machineHistory();
  list.replaceChildren();
  if (!runs.length) {
    const empty = document.createElement("p");
    empty.className = "sheet-copy";
    empty.textContent = "Nothing yet. Finished runs land here automatically.";
    list.appendChild(empty);
    return;
  }
  for (const run of runs) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `history-row${run.id === state.viewingRunId ? " on" : ""}`;
    const dot = document.createElement("span");
    dot.className = `history-dot ${run.status}`;
    const text = document.createElement("span");
    text.className = "history-text";
    const title = document.createElement("strong");
    title.textContent = runLabel(run);
    const meta = document.createElement("small");
    meta.textContent = runMetaLine(run);
    text.append(title, meta);
    button.append(dot, text);
    button.addEventListener("click", () => openHistoryRun(run.id));
    list.appendChild(button);
  }
}

function replayEvents(events) {
  state.replaying = true;
  const feed = $("#timeline");
  feed.replaceChildren();
  state.lastStep = null;
  for (const event of events) addEvent(event);
  state.replaying = false;
  scrollFeed(true);
}

function openHistoryRun(id) {
  const run = state.history.find((item) => item.id === id);
  if (!run) return;
  closeSheets();
  state.viewingRunId = run.id === state.liveRun?.id ? "" : run.id;
  if (!state.viewingRunId) {
    backToLive();
    return;
  }
  $("#historyBannerText").textContent = `${runLabel(run)} · ${runMetaLine(run)}`;
  $("#historyBanner").classList.remove("hidden");
  replayEvents(run.events || []);
  buzz();
}

function backToLive() {
  resumeLiveFeed({ keepTranscript: false });
}

/* ── polling ──────────────────────────────────────────── */

/** Pull everything new, archive it, and show it unless we are reading history.
 *  `silent` catches up without redrawing — how a fresh session starts clean. */
async function drainEvents(silent = false) {
  const machine = currentMachine();
  if (!machine || state.draining) return 0;
  const id = machine.id;
  const version = state.feedVersion;
  state.draining = true;
  let seen = 0;
  try {
    for (let page = 0; page < MAX_DRAIN_PAGES; page += 1) {
      const data = await api(`/api/machines/${encodeURIComponent(id)}/events?since=${state.cursor}`);
      if (id !== state.machineId || version !== state.feedVersion) return seen;
      const events = data.events || [];
      for (const event of events) {
        if (String(event.type || "").toLowerCase() === "clarification") {
          if (!silent) applyClarification(event.payload || {});
          continue;
        }
        recordEvent(event);
        if (!silent && !state.viewingRunId) addEvent(event);
      }
      seen += events.length;
      state.cursor = Number(data.cursor || state.cursor);
      saveCursor();
      if (events.length < EVENT_PAGE) break;
    }
  } catch (error) {
    console.debug(error);
  } finally {
    state.draining = false;
  }
  return seen;
}

async function pollEvents() {
  await drainEvents(false);
  closeStaleRun();
}

function cursorKey() {
  return `aios_cursor_${state.machineId}`;
}

function loadCursor() {
  state.cursor = Number(prefs.get(cursorKey(), 0)) || 0;
}

function saveCursor() {
  if (state.machineId) prefs.set(cursorKey(), state.cursor);
}

/** Reattach to the thread this phone was last watching.
 *
 *  A reload — or an hour in the background, which on iOS means the same
 *  thing — leaves state.liveRun empty while storage still holds an open run.
 *  Without this, everything OPERATOR did while you were away is filed under a
 *  brand new history row and your message sits alone in the old one.
 */
function adoptLiveRun() {
  const recent = machineHistory()[0];
  state.liveRun = recent && recent.status === "open" ? recent : null;
  return recent || null;
}

/** Put the conversation back on screen, including everything OPERATOR did
 *  while the phone was away. `reset` (the New chat button) is the only way to
 *  an empty feed. */
async function beginSession(reset = false) {
  if (!state.machineId) return;
  state.sessionMachineId = state.machineId;
  loadCursor();
  state.viewingRunId = "";
  $("#historyBanner").classList.add("hidden");
  // Nothing half-finished survives a reopen: no stuck send, no stale draft
  // questions, no pending bubble waiting on a reply that never came.
  state.busy = false;
  state.draining = false;
  $("#sendBtn").disabled = false;
  clearClarifier();
  clearMarkers();
  state.feedVersion += 1;

  if (reset) {
    const archived = await drainEvents(true);
    closeRun("ended");
    state.liveRun = null;
    state.forceNewPrompt = true;
    resetTimeline("Ready. Ask your computer to do something.");
    if (archived >= SERVER_PRUNE_EVENTS && !state.running) await pruneServerEvents();
    await pollEvents();
    return;
  }

  // Paint what we already have first — reopening the app should never stare
  // back with an empty feed while the relay is still answering.
  const restored = adoptLiveRun();
  if (restored?.events?.length) replayEvents(restored.events);
  else resetTimeline("Ready. Ask your computer to do something.");

  // Then catch up. Silent, because the backlog belongs to the thread we just
  // painted and recordEvent is what stitches it back together.
  const arrived = await drainEvents(true);
  const current = machineHistory()[0];
  if (arrived && current?.events?.length) {
    // OPERATOR may have started something else entirely while we were away.
    if (current.id !== restored?.id && current.status === "open") state.liveRun = current;
    replayEvents(current.events);
  }
  await pollEvents();
}

/** The backlog is safely in History, so let the relay forget it. */
async function pruneServerEvents() {
  const machine = currentMachine();
  if (!machine || state.running) return;
  try {
    await api(`/api/machines/${encodeURIComponent(machine.id)}/events`, { method: "DELETE" });
    state.cursor = 0;
    saveCursor();
  } catch (error) {
    console.debug(error);
  }
}

function startPolling() {
  stopPolling();
  loadHistory();
  state.sessionMachineId = "";
  loadMachines();
  requestStream();
  startFrameLoop();
  state.timers.push(setInterval(loadMachines, 4000));
  state.timers.push(setInterval(pollEvents, 1400));
  state.streamLeaseTimer = setInterval(requestStream, 8000);
  state.timers.push(setInterval(updateLive, 1000));
}

function stopPolling() {
  state.timers.forEach(clearInterval);
  state.timers = [];
  clearInterval(state.streamLeaseTimer);
  state.streamLeaseTimer = null;
  clearTimeout(state.frameTimer);
  state.frameTimer = null;
}

function startFrameLoop() {
  clearTimeout(state.frameTimer);
  const tick = async () => {
    await refreshFrame();
    // Poll a little faster than the 12 FPS sender so network jitter does not
    // reduce the visible stream below ten fresh frames per second.
    state.frameTimer = setTimeout(tick, document.hidden || !state.screenOpen ? 500 : 70);
  };
  tick();
}

function requestStream() {
  const machine = currentMachine();
  if (!machine || !machine.online || document.hidden || !state.screenOpen) return;
  sendCommand("stream", { monitor_id: state.monitorId || "1", lease_seconds: 15 }).catch(() => {});
}

async function applyWakeLock() {
  if (!("wakeLock" in navigator)) return;
  const wanted = state.keepAwake && state.running && !document.hidden;
  try {
    if (wanted && !state.wakeLock) {
      state.wakeLock = await navigator.wakeLock.request("screen");
      state.wakeLock.addEventListener("release", () => { state.wakeLock = null; });
    } else if (!wanted && state.wakeLock) {
      await state.wakeLock.release();
      state.wakeLock = null;
    }
  } catch {
    /* Wake lock is a nicety; ignore refusals. */
  }
}

/* ── commands ─────────────────────────────────────────── */

async function sendCommand(type, payload = {}) {
  const machine = currentMachine();
  if (!machine) throw new Error("Choose a computer first.");
  // The production relay can lag behind the PWA. Tunnel newer command types
  // through its long-supported config envelope so intent questions keep
  // working while the PC and static client update independently.
  const tunneled = ["clarify", "stream", "update", "codex_switch", "ai_settings"].includes(type);
  return api(`/api/machines/${encodeURIComponent(machine.id)}/commands`, {
    method: "POST",
    body: JSON.stringify({
      type: tunneled ? "config" : type,
      payload: tunneled ? { ...payload, _aios_command: type } : payload
    })
  });
}

async function loadVersion() {
  const value = $("#versionValue");
  if (!value) return;
  try {
    const response = await fetch(`version.json?v=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const version = await response.json();
    const stamp = new Date(version.built_at || version.committed_at || "");
    const when = Number.isNaN(stamp.getTime())
      ? "Unknown build time"
      : new Intl.DateTimeFormat(undefined, {
          dateStyle: "medium",
          timeStyle: "short"
        }).format(stamp);
    const commit = String(version.commit || "").slice(0, 8);
    value.textContent = commit ? `${when} · ${commit}` : when;
    value.title = String(version.commit || "");
  } catch {
    value.textContent = "Local development build";
  }
}

/* ── attachments ──────────────────────────────────────── */

function humanSize(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function attachmentKind(file) {
  const type = String(file.type || "").toLowerCase();
  if (type.startsWith("image/")) return "image";
  if (type.startsWith("text/") || type === "application/json") return "text";
  return TEXT_FILE_PATTERN.test(file.name || "") ? "text" : "";
}

/** Decode a picked image, EXIF rotation included, without leaking object URLs. */
async function decodeImage(file) {
  if (window.createImageBitmap) {
    try { return await createImageBitmap(file, { imageOrientation: "from-image" }); } catch { /* Safari fallback */ }
  }
  const url = URL.createObjectURL(file);
  try {
    const image = new Image();
    image.decoding = "async";
    await new Promise((resolve, reject) => {
      image.onload = resolve;
      image.onerror = () => reject(new Error("That image could not be opened."));
      image.src = url;
    });
    return image;
  } finally {
    URL.revokeObjectURL(url);
  }
}

/** Shrink a phone photo to something the model reads well and the relay
 *  carries quickly. Small images are passed through untouched. */
async function prepareImage(file) {
  const source = await decodeImage(file);
  const width = source.width || source.naturalWidth;
  const height = source.height || source.naturalHeight;
  if (!width || !height) throw new Error("That image could not be opened.");
  const scale = Math.min(1, MAX_IMAGE_EDGE / Math.max(width, height));
  // Only pass a file through untouched when the PC can certainly open it —
  // an iPhone HEIC or an SVG has to become a JPEG on this side of the wire.
  const portable = /^image\/(png|jpeg|webp|gif|bmp)$/.test(String(file.type || "").toLowerCase());
  if (scale === 1 && portable && file.size <= KEEP_ORIGINAL_IMAGE_BYTES) {
    source.close?.();
    return { blob: file, type: file.type || "image/png", name: file.name || "image.png" };
  }
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(width * scale));
  canvas.height = Math.max(1, Math.round(height * scale));
  const context = canvas.getContext("2d");
  context.drawImage(source, 0, 0, canvas.width, canvas.height);
  source.close?.();
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", IMAGE_QUALITY));
  if (!blob) throw new Error("That image could not be prepared.");
  const base = String(file.name || "image").replace(/\.[^.]+$/, "") || "image";
  return { blob, type: "image/jpeg", name: `${base}.jpg` };
}

function renderAttachments() {
  const tray = $("#attachTray");
  if (!tray) return;
  tray.textContent = "";
  tray.classList.toggle("hidden", !state.attachments.length);
  $("#attachBtn")?.classList.toggle("on", Boolean(state.attachments.length));
  for (const item of state.attachments) {
    const chip = document.createElement("div");
    chip.className = `attach-chip ${item.kind === "image" ? "image" : "file"}${item.busy ? " busy" : ""}`;
    if (item.kind === "image") {
      const thumb = document.createElement("img");
      thumb.src = item.preview;
      thumb.alt = item.name;
      chip.appendChild(thumb);
    } else {
      chip.appendChild(icon("file"));
      const meta = document.createElement("div");
      meta.className = "attach-meta";
      const name = document.createElement("strong");
      name.textContent = item.name;
      const size = document.createElement("small");
      size.textContent = humanSize(item.size);
      meta.append(name, size);
      chip.appendChild(meta);
    }
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "attach-remove";
    remove.setAttribute("aria-label", `Remove ${item.name}`);
    remove.appendChild(icon("close"));
    remove.addEventListener("click", () => removeAttachment(item.id));
    chip.appendChild(remove);
    tray.appendChild(chip);
  }
  autoGrow();
}

function removeAttachment(id) {
  const item = state.attachments.find((entry) => entry.id === id);
  if (!item) return;
  if (item.preview) URL.revokeObjectURL(item.preview);
  state.attachments = state.attachments.filter((entry) => entry.id !== id);
  renderAttachments();
  buzz();
}

function clearAttachments() {
  // Previews are left alive on purpose: the bubble you just sent shows them.
  state.attachments = [];
  renderAttachments();
}

function markAttachmentsBusy(busy) {
  state.attachments.forEach((item) => { item.busy = busy; });
  renderAttachments();
}

async function addFiles(fileList) {
  const files = [...(fileList || [])].filter(Boolean);
  if (!files.length) return;
  if (!currentMachine()) { toast("Connect a computer first."); return; }
  let skipped = "";
  for (const file of files) {
    if (state.attachments.length >= MAX_ATTACHMENTS) {
      toast(`Up to ${MAX_ATTACHMENTS} files per message`);
      break;
    }
    const kind = attachmentKind(file);
    if (!kind) {
      skipped = /\.pdf$/i.test(file.name || "")
        ? "PDFs aren't supported yet — send a screenshot of the page."
        : `${file.name || "That file"} isn't a photo or a text file.`;
      continue;
    }
    if (file.size > MAX_FILE_BYTES) { skipped = `${file.name || "That file"} is over 15 MB.`; continue; }
    if (kind === "text" && file.size > MAX_TEXT_BYTES) { skipped = `${file.name || "That file"} is too long to send.`; continue; }
    try {
      const prepared = kind === "image"
        ? await prepareImage(file)
        : { blob: file, type: file.type || "text/plain", name: file.name || "note.txt" };
      state.attachments.push({
        id: `a${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`,
        kind,
        name: prepared.name,
        type: prepared.type,
        size: prepared.blob.size,
        blob: prepared.blob,
        preview: kind === "image" ? URL.createObjectURL(prepared.blob) : "",
        busy: false
      });
    } catch (error) {
      skipped = error.message || "That file could not be read.";
    }
  }
  renderAttachments();
  if (skipped) toast(skipped);
  else buzz();
}

async function blobToBase64(blob) {
  const buffer = new Uint8Array(await blob.arrayBuffer());
  let binary = "";
  for (let index = 0; index < buffer.length; index += 0x8000) {
    binary += String.fromCharCode.apply(null, buffer.subarray(index, index + 0x8000));
  }
  return btoa(binary);
}

async function uploadAttachment(item) {
  const machine = currentMachine();
  if (!machine) throw new Error("Choose a computer first.");
  const path = `/api/machines/${encodeURIComponent(machine.id)}/uploads?name=${encodeURIComponent(item.name)}`;
  const data = await api(path, {
    method: "POST",
    body: item.blob,
    headers: { "Content-Type": item.type || "application/octet-stream" },
    timeoutMs: 90_000
  });
  return { key: data.key, name: item.name, kind: item.kind, type: item.type, size: item.size };
}

/** Hand the picked files to the relay, newest transport first.
 *  A relay too old to know about uploads still gets small files inline. */
async function packAttachments(items) {
  if (!items.length) return [];
  const packed = [];
  let inlineOnly = false;
  for (const item of items) {
    if (!inlineOnly) {
      try {
        packed.push(await uploadAttachment(item));
        continue;
      } catch (error) {
        // Only a relay that has never heard of uploads earns the fallback.
        if (![404, 405, 501].includes(error.status)) throw error;
        inlineOnly = true;
      }
    }
    packed.push({
      name: item.name, kind: item.kind, type: item.type, size: item.size,
      data: await blobToBase64(item.blob)
    });
  }
  const inlineBytes = packed.reduce((total, item) => total + (item.data ? item.data.length : 0), 0);
  if (inlineBytes > INLINE_ATTACHMENT_BUDGET) {
    throw new Error("Your relay needs updating before it can carry files this big.");
  }
  return packed;
}

function attachmentSummary(items) {
  return items.map((item) => ({ name: item.name, kind: item.kind, url: item.preview || "" }));
}

function autoGrow() {
  const input = $("#promptInput");
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, window.innerHeight * 0.34)}px`;
  const busy = Boolean(input.value.trim()) || state.attachments.length > 0 || treatAsFollowUp();
  $("#suggestions").classList.toggle("hidden", busy);
}

function clearClarifier(clearQuestions = true) {
  clearTimeout(state.clarifierTimer);
  clearTimeout(state.clarifierTimeout);
  state.clarifierTimer = null;
  state.clarifierTimeout = null;
  state.clarifierLoading = false;
  state.clarifierRequestId = "";
  state.clarifierDraft = "";
  if (clearQuestions) state.clarifierQuestions = [];
  $("#clarifier").classList.add("hidden");
}

function renderClarifier() {
  const panel = $("#clarifier");
  const list = $("#clarifierQuestions");
  const questions = state.clarifierQuestions || [];
  if (!state.clarifierLoading && !questions.length) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");
  panel.classList.toggle("loading", state.clarifierLoading);
  list.replaceChildren();

  const openCount = questions.filter((question) => !question.answered).length;
  $(".clarifier-title").innerHTML = `<span class="clarifier-spark">✦</span> ${treatAsFollowUp() ? "Follow-up check" : "Intent check"}`;
  $("#clarifierStatus").textContent = state.clarifierLoading
    ? (questions.length ? "Updating…" : "Reading your draft…")
    : openCount ? `${openCount} ${openCount === 1 ? "detail" : "details"} to decide` : "Covered";

  for (const question of questions) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = `clarifier-question${question.answered ? " answered" : ""}`;
    row.setAttribute("aria-label", `${question.answered ? "Answered" : "Clarify"}: ${question.question}`);
    const check = document.createElement("span");
    check.className = "clarifier-check";
    check.appendChild(icon("check"));
    const text = document.createElement("p");
    text.textContent = question.question;
    const answer = document.createElement("span");
    answer.className = "clarifier-answer";
    answer.textContent = question.answered ? "covered" : "";
    row.append(check, text, answer);
    row.addEventListener("click", () => {
      const input = $("#promptInput");
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
      buzz();
    });
    list.appendChild(row);
  }
}

function scheduleClarification() {
  clearTimeout(state.clarifierTimer);
  const draft = $("#promptInput").value.trim();
  const machine = currentMachine();
  if (draft.length < 10 || !machine?.online || !state.token) {
    clearClarifier();
    return;
  }
  state.clarifierTimer = setTimeout(() => requestClarification(draft), 350);
}

async function waitForClarification(requestId, timeoutMs = 10_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline && state.clarifierRequestId === requestId && state.clarifierLoading) {
    await drainEvents(false);
    if (state.clarifierRequestId !== requestId || !state.clarifierLoading) return;
    await new Promise((resolve) => setTimeout(resolve, 180));
  }
}

async function requestClarification(draft) {
  if ($("#promptInput").value.trim() !== draft) return;
  const requestId = `${state.machineId}:${Date.now()}:${++state.clarifierSequence}`;
  state.clarifierRequestId = requestId;
  state.clarifierDraft = draft;
  state.clarifierLoading = true;
  renderClarifier();
  clearTimeout(state.clarifierTimeout);
  state.clarifierTimeout = setTimeout(() => {
    if (state.clarifierRequestId !== requestId) return;
    state.clarifierLoading = false;
    renderClarifier();
  }, 10_000);
  try {
    await sendCommand("clarify", {
      draft,
      request_id: requestId,
      previous: state.clarifierQuestions.map(({ id, question, answered }) => ({ id, question, answered }))
    });
    await waitForClarification(requestId);
  } catch {
    if (state.clarifierRequestId !== requestId) return;
    state.clarifierLoading = false;
    renderClarifier();
  }
}

function applyClarification(payload) {
  if (!payload || payload.request_id !== state.clarifierRequestId) return;
  if ($("#promptInput").value.trim() !== state.clarifierDraft) return;
  clearTimeout(state.clarifierTimeout);
  state.clarifierLoading = false;
  if (payload.ok !== false && Array.isArray(payload.questions)) {
    state.clarifierQuestions = payload.questions.slice(0, MAX_CLARIFIER_QUESTIONS)
      .filter((question) => question?.question)
      .map((question) => ({
        id: String(question.id || "question"),
        question: String(question.question || "").trim(),
        answered: Boolean(question.answered)
      }));
  }
  renderClarifier();
}

function openScreen() {
  if (!state.screenOpen) toggleScreen(true);
}

function toggleScreen(open) {
  state.screenOpen = open;
  prefs.set("aios_screen_open", open ? 1 : 0);
  $("#viewer").classList.toggle("collapsed", !open);
  $("#screenToggle").classList.toggle("up", !open);
  $("#screenToggle").querySelector("span").textContent = open ? "Hide screen" : "Show screen";
  if (open) setTimeout(fitViewer, 300);
}

function toggleImmersive(on) {
  const view = $("#viewer");
  view.classList.toggle("immersive", on);
  $("#expandBtn").replaceChildren(icon(on ? "shrink" : "expand"));
  cinemaSet(on);
  // Real fullscreen where the browser allows it — iOS Safari does not, so the
  // fixed-inset layout above is what actually carries the view there.
  try {
    if (on && !document.fullscreenElement && view.requestFullscreen) {
      view.requestFullscreen({ navigationUI: "hide" }).catch(() => {});
    } else if (!on && document.fullscreenElement && document.exitFullscreen) {
      document.exitFullscreen().catch(() => {});
    }
  } catch { /* fullscreen is a nicety, never a requirement */ }
  // Orientation lock only resolves on Android/desktop; iOS rejects it and the
  // rotate hint in the overlay covers that case.
  try {
    if (on) screen.orientation?.lock?.("landscape").catch(() => {});
    else screen.orientation?.unlock?.();
  } catch { /* not supported */ }
  setTimeout(fitViewer, 60);
  if (on) openOverlay(() => toggleImmersive(false));
  else if (overlayCloser) dismissOverlay();
}

/* ── dictation ────────────────────────────────────────── */

const SpeechRecognitionClass = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognition = null;
let recognitionBase = "";

function stopDictation() {
  if (recognition) {
    recognition.onend = null;
    recognition.stop();
    recognition = null;
  }
  $("#micBtn").classList.remove("rec");
  $("#listening").classList.add("hidden");
  $(".composer").classList.remove("rec");
}

function startDictation() {
  if (!SpeechRecognitionClass) {
    toast("Dictation needs Chrome or Safari — try the mic on your keyboard.");
    return;
  }
  recognition = new SpeechRecognitionClass();
  recognition.lang = navigator.language || "en-US";
  recognition.continuous = true;
  recognition.interimResults = true;
  recognitionBase = $("#promptInput").value.replace(/\s+$/, "");

  recognition.onresult = (event) => {
    let final = "";
    let interim = "";
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      const result = event.results[index];
      if (result.isFinal) final += result[0].transcript;
      else interim += result[0].transcript;
    }
    if (final) recognitionBase = `${recognitionBase} ${final.trim()}`.trim();
    $("#promptInput").value = interim ? `${recognitionBase} ${interim.trim()}`.trim() : recognitionBase;
    $("#listeningText").textContent = interim || "Listening…";
    autoGrow();
    scheduleClarification();
  };
  recognition.onerror = (event) => {
    stopDictation();
    if (event.error === "not-allowed") toast("Microphone access was blocked.");
    else if (event.error !== "aborted") toast("Dictation stopped.");
  };
  recognition.onend = () => stopDictation();

  try {
    recognition.start();
  } catch {
    stopDictation();
    return;
  }
  buzz(12);
  $("#micBtn").classList.add("rec");
  $(".composer").classList.add("rec");
  $("#listeningText").textContent = "Listening…";
  $("#listening").classList.remove("hidden");
}

/* ── wiring: auth ─────────────────────────────────────── */

$("#createAccountBtn").addEventListener("click", async () => {
  const button = $("#createAccountBtn");
  button.disabled = true;
  button.textContent = "Creating…";
  try {
    const data = await api("/api/account/create", { method: "POST", body: "{}" });
    saveSession(data.token, data.code);
    $("#privateCode").textContent = data.code;
    $("#welcomePane").classList.add("hidden");
    $("#createdPane").classList.remove("hidden");
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Create my remote";
  }
});

$("#showLoginBtn").addEventListener("click", () => {
  $("#welcomePane").classList.add("hidden");
  $("#loginForm").classList.remove("hidden");
  $("#loginCode").focus();
});
$("#loginBackBtn").addEventListener("click", () => {
  $("#loginForm").classList.add("hidden");
  $("#welcomePane").classList.remove("hidden");
});
$("#loginForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const code = $("#loginCode").value.trim();
  $("#loginError").textContent = "";
  try {
    const data = await api("/api/account/login", { method: "POST", body: JSON.stringify({ code }) });
    saveSession(data.token, code.toUpperCase());
    showApp();
  } catch (error) {
    $("#loginError").textContent = error.message;
  }
});
$("#privateCode").addEventListener("click", () => copyText(state.privateCode));
$("#copyCodeBtn").addEventListener("click", () => copyText(state.privateCode));
$("#enterRemoteBtn").addEventListener("click", showApp);

/* ── wiring: sheets ───────────────────────────────────── */

$("#machineBtn").addEventListener("click", () => {
  renderOptions(
    $("#machineOptions"),
    state.machines.map((machine) => ({
      id: machine.id,
      name: machine.name,
      note: machine.online ? "Online" : `Last seen ${ago(machine.last_seen)}`,
      icon: true,
      online: machine.online
    })),
    state.machineId,
    selectMachine
  );
  openSheet("machineSheet");
});

$("#monitorBtn").addEventListener("click", () => {
  const monitors = monitorsOf(currentMachine());
  renderOptions(
    $("#monitorOptions"),
    monitors.map((monitor, index) => ({
      id: String(monitor.id ?? index),
      name: monitorName(monitor, index),
      note: monitor.width ? `${monitor.width} × ${monitor.height}` : ""
    })),
    String(state.monitorId),
    (id) => {
      state.monitorId = id;
      state.frameStamp = "";
      state.frameSeq = 0;
      state.frameUpdatedAt = 0;
      $("#monitorLabel").textContent = monitorName(currentMonitor());
      clearMarkers();
      closeSheets();
      requestStream();
      refreshFrame();
    }
  );
  openSheet("monitorSheet");
});

function runSettingsSummary() {
  const clicker = labelOf(MODELS, state.model);
  const planner = state.plannerModel === "off" ? "No plan" : `Plan ${labelOf(PLANNER_MODELS, state.plannerModel)}`;
  return `${clicker} · ${planner}`;
}

function updateRunSettingsLabels() {
  const summary = runSettingsSummary();
  $("#runSettingsLabel").textContent = summary;
  $("#settingsRunValue").textContent = summary;
  $("#runModelValue").textContent = labelOf(MODELS, state.model);
  $("#runEffortValue").textContent = labelOf(EFFORTS, state.effort);
  $("#runPlannerValue").textContent = labelOf(PLANNER_MODELS, state.plannerModel === "off" ? state.lastPlannerModel : state.plannerModel);
  $("#plannerToggle").checked = state.plannerModel !== "off";
  $(".planner-section").classList.toggle("off", state.plannerModel === "off");
}

function renderRunSettings() {
  renderChoiceChips($("#runModelOptions"), MODELS, state.model, (id) => {
    state.model = id;
    prefs.set("aios_model", id);
    renderRunSettings();
    renderMachine();
  });
  renderChoiceChips($("#runEffortOptions"), EFFORTS, state.effort, (id) => {
    state.effort = id;
    prefs.set("aios_effort", id);
    renderRunSettings();
  });
  const plannerChoices = PLANNER_MODELS.filter((item) => item.id !== "off");
  const activePlanner = state.plannerModel === "off" ? state.lastPlannerModel : state.plannerModel;
  renderChoiceChips($("#runPlannerOptions"), plannerChoices, activePlanner, (id) => {
    state.plannerModel = id;
    state.lastPlannerModel = id;
    prefs.set("aios_planner_model", id);
    prefs.set("aios_planner_last_model", id);
    renderRunSettings();
  });
  updateRunSettingsLabels();
}

function openRunSettings() {
  renderRunSettings();
  openSheet("runSettingsSheet");
}

function providerLabel(id = state.providerMode) {
  return AI_PROVIDERS.find((provider) => provider.id === id)?.name || "Codex";
}

function renderAIStatus() {
  const providerValue = $("#aiProviderValue");
  if (providerValue) providerValue.textContent = providerLabel();
  const keyStatus = $("#apiKeyStatus");
  if (keyStatus) {
    keyStatus.textContent = state.hasOpenAIKey
      ? "Saved on this PC"
      : state.transportPublicKey ? "Not saved on this PC" : "Update this PC to enable secure key sync";
  }
  $("#clearApiKeyBtn")?.classList.toggle("hidden", !state.hasOpenAIKey);
}

function renderAIProviderOptions() {
  renderOptions($("#aiProviderOptions"), AI_PROVIDERS, state.providerMode, async (id) => {
    if (id === state.providerMode) return;
    state.providerMode = id;
    renderAIStatus();
    renderAIProviderOptions();
    try {
      await sendCommand("ai_settings", { provider_mode: id });
      toast(`${providerLabel(id)} selected`);
      if (id !== "codex" && !state.hasOpenAIKey) $("#apiKeyInput").focus();
      setTimeout(loadMachines, 900);
    } catch (error) {
      toast(error.message);
      setTimeout(loadMachines, 300);
    }
  });
  renderAIStatus();
}

$("#plannerToggle").addEventListener("change", (event) => {
  if (event.target.checked) {
    state.plannerModel = state.lastPlannerModel || "sol";
  } else {
    if (state.plannerModel !== "off") state.lastPlannerModel = state.plannerModel;
    state.plannerModel = "off";
  }
  prefs.set("aios_planner_model", state.plannerModel);
  prefs.set("aios_planner_last_model", state.lastPlannerModel);
  renderRunSettings();
});

$("#shellToggle").addEventListener("change", (event) => {
  state.shell = event.target.checked;
  prefs.set("aios_shell", state.shell ? "1" : "");
});

$("#runSettingsBtn").addEventListener("click", openRunSettings);
$("#settingsRunRow").addEventListener("click", openRunSettings);
$("#appearanceRow").addEventListener("click", () => {
  applyAppearance();
  openSheet("appearanceSheet");
});
$$(".theme-option").forEach((button) => button.addEventListener("click", () => {
  applyAppearance(button.dataset.background);
  buzz();
}));
$("#aiProviderRow").addEventListener("click", () => {
  renderAIProviderOptions();
  $("#apiKeyInput").value = "";
  openSheet("aiProviderSheet");
});
$("#apiKeyForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("#apiKeyInput");
  const apiKey = input.value.trim();
  if (!apiKey) {
    toast("Paste an OpenAI API key first.");
    input.focus();
    return;
  }
  const button = $("#saveApiKeyBtn");
  button.disabled = true;
  button.textContent = "Saving…";
  try {
    const encryptedOpenAIKey = await encryptSecretForMachine(apiKey);
    await sendCommand("ai_settings", {
      provider_mode: state.providerMode,
      encrypted_openai_api_key: encryptedOpenAIKey
    });
    input.value = "";
    state.hasOpenAIKey = true;
    renderAIStatus();
    toast("API key sent securely to this PC");
    setTimeout(loadMachines, 900);
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Save";
  }
});
$("#clearApiKeyBtn").addEventListener("click", async () => {
  if (!window.confirm("Remove the OpenAI API key from this computer?")) return;
  const button = $("#clearApiKeyBtn");
  button.disabled = true;
  try {
    await sendCommand("ai_settings", {
      provider_mode: state.providerMode,
      clear_openai_api_key: true
    });
    state.hasOpenAIKey = false;
    renderAIStatus();
    toast("API key removal requested");
    setTimeout(loadMachines, 900);
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
});
$("#codexAccountRow").addEventListener("click", () => {
  const accounts = Array.isArray(currentMachine()?.status?.codex_accounts)
    ? currentMachine().status.codex_accounts.filter((account) => account.logged_in)
    : [];
  if (!accounts.length) {
    toast("Sign in to Codex once from aiOS on this PC first.");
    return;
  }
  renderOptions(
    $("#codexAccountOptions"),
    accounts.map((account) => ({ id: account.id, name: account.label, note: account.active ? "Active" : "Ready" })),
    accounts.find((account) => account.active)?.id || "",
    async (id) => {
      try {
        await sendCommand("codex_switch", { account_id: id });
        closeSheets();
        toast("Codex account switched");
        setTimeout(loadMachines, 900);
      } catch (error) {
        toast(error.message);
      }
    }
  );
  openSheet("codexAccountSheet");
});
$("#settingsBtn").addEventListener("click", () => openSheet("settingsSheet"));
$("#addMachineBtn").addEventListener("click", () => openSheet("pairSheet"));
$("#emptyAddBtn").addEventListener("click", () => openSheet("pairSheet"));
$("#pairCode").addEventListener("click", () => copyText(state.privateCode));
$("#pairCopyBtn").addEventListener("click", () => {
  if (state.privateCode) copyText(state.privateCode);
  else toast("Unlock with your private code first.");
});

$("#renameBtn").addEventListener("click", () => {
  $("#renameInput").value = currentMachine()?.name || "";
  openSheet("renameSheet");
});
$("#renameForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const machine = currentMachine();
  const name = $("#renameInput").value.trim();
  if (!machine || !name) return;
  try {
    await api(`/api/machines/${encodeURIComponent(machine.id)}`, { method: "PATCH", body: JSON.stringify({ name }) });
    machine.name = name;
    renderMachine();
    closeSheets();
    toast("Renamed");
  } catch (error) {
    toast(error.message);
  }
});

$("#stepsInput").addEventListener("change", (event) => {
  const value = Math.min(100, Math.max(1, Number(event.target.value) || 30));
  state.steps = value;
  event.target.value = value;
  prefs.set("aios_steps", value);
});
$("#detailsToggle").addEventListener("change", (event) => {
  state.detailed = event.target.checked;
  prefs.set("aios_detailed", state.detailed ? 1 : 0);
  applyFilter();
});
$("#wakeToggle").addEventListener("change", (event) => {
  state.keepAwake = event.target.checked;
  prefs.set("aios_awake", state.keepAwake ? 1 : 0);
  applyWakeLock();
});
$("#notifyToggle").addEventListener("change", async (event) => {
  const turnOff = () => {
    event.target.checked = false;
    state.notify = false;
    state.pushReady = false;
    prefs.set("aios_notify", "");
  };
  if (!event.target.checked) {
    state.notify = false;
    prefs.set("aios_notify", "");
    $("#notifyTestBtn").classList.add("hidden");
    await unsubscribePush();
    return;
  }
  const blocker = pushBlocker();
  if (blocker) { turnOff(); toast(blocker); return; }
  if (!(await ensureNotifyPermission())) {
    turnOff();
    toast("Notifications were blocked — turn them back on in your phone's settings for aiOS Remote");
    return;
  }
  state.notify = true;
  prefs.set("aios_notify", "1");
  try {
    await subscribePush();
    $("#notifyTestBtn").classList.remove("hidden");
    toast("Notifications on — this phone now gets woken even when the app is closed");
  } catch (error) {
    // Permission is granted, so keep the in-app alerts; just be honest that
    // the closed-app ones are not working.
    state.pushReady = false;
    toast(error.message);
  }
});

$("#notifyTestBtn").addEventListener("click", async () => {
  const button = $("#notifyTestBtn");
  button.disabled = true;
  try {
    if (!state.pushReady) await subscribePush();
    await api("/api/push/test", { method: "POST" });
    toast("Sent — it should arrive in a second, even if you close the app");
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
});
$("#hapticToggle").addEventListener("change", (event) => {
  state.haptics = event.target.checked;
  prefs.set("aios_haptics", state.haptics ? 1 : 0);
  buzz();
});
$("#updateBtn").addEventListener("click", async () => {
  const button = $("#updateBtn");
  button.disabled = true;
  try {
    await sendCommand("update");
    closeSheets();
    toast("Update requested. aiOS will restart itself when safe.");
    setTimeout(loadMachines, 1200);
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
});
$("#logoutBtn").addEventListener("click", clearSession);

/* ── wiring: workspace ────────────────────────────────── */

function applyFilter() {
  $("#timeline").classList.toggle("lean", !state.detailed);
  $("#filterBtn").textContent = state.detailed ? "All" : "Key";
  $("#detailsToggle").checked = state.detailed;
}

$("#filterBtn").addEventListener("click", () => {
  state.detailed = !state.detailed;
  prefs.set("aios_detailed", state.detailed ? 1 : 0);
  applyFilter();
  buzz();
});

$("#screenToggle").addEventListener("click", () => {
  buzz();
  toggleScreen(!state.screenOpen);
  if (state.screenOpen) {
    requestStream();
    startFrameLoop();
  }
});
$("#expandBtn").addEventListener("click", () => toggleImmersive(!$("#viewer").classList.contains("immersive")));
$("#zoomResetBtn").addEventListener("click", resetZoom);
$("#jumpBtn").addEventListener("click", () => {
  scrollFeed(true);
  buzz(8);
});
$("#timeline").addEventListener("scroll", () => {
  if (nearBottom()) hideJump();
  else showJump();
}, { passive: true });

$("#stopBtn").addEventListener("click", async () => {
  buzz(20);
  try {
    await sendCommand("stop");
    toast("Stop requested");
  } catch (error) {
    toast(error.message);
  }
});

$("#clearChatBtn").addEventListener("click", async () => {
  if (!currentMachine()) return;
  buzz();
  if (state.running) sendCommand("stop").catch(() => {});
  await beginSession(true);
  renderMachine();
});

$("#historyBtn").addEventListener("click", () => {
  renderHistory();
  openSheet("historySheet");
});

$("#backToLiveBtn").addEventListener("click", () => {
  backToLive();
  buzz();
});

$("#clearHistoryBtn").addEventListener("click", () => {
  if (!window.confirm("Delete the saved history on this phone?")) return;
  const keep = state.history.filter((run) => run.machineId !== state.machineId);
  state.history = keep;
  state.liveRun = null;
  state.viewingRunId = "";
  $("#historyBanner").classList.add("hidden");
  saveHistory();
  renderHistory();
  toast("History cleared");
});

$("#promptInput").addEventListener("input", () => {
  autoGrow();
  scheduleClarification();
});
$("#promptInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
    event.preventDefault();
    $("#promptForm").requestSubmit();
  }
});
$$(".sugg").forEach((button) => button.addEventListener("click", () => {
  $("#promptInput").value = button.textContent;
  autoGrow();
  scheduleClarification();
  $("#promptInput").focus();
}));

$("#lightbox").addEventListener("click", (event) => {
  if (event.target.closest(".lightbox-shot")) return;
  closeLightbox();
});
$("#lightboxClose").addEventListener("click", closeLightbox);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("#lightbox").classList.contains("hidden")) closeLightbox();
});

$("#micBtn").addEventListener("click", () => (recognition ? stopDictation() : startDictation()));
$("#micStopBtn").addEventListener("click", stopDictation);

/* ── wiring: attachments ──────────────────────────────── */

$("#attachBtn").addEventListener("click", () => {
  buzz();
  $("#fileInput").click();
});
$("#fileInput").addEventListener("change", async (event) => {
  await addFiles(event.target.files);
  event.target.value = "";   // picking the same photo twice must still work
});

// Paste a screenshot straight into the chat. Ignore pastes aimed at the
// private-code and API-key fields, which are plain text by design.
document.addEventListener("paste", (event) => {
  if (!state.token || $("#appView").classList.contains("hidden")) return;
  if (event.target instanceof HTMLElement && event.target.closest("input")) return;
  const clipboard = event.clipboardData;
  let files = [...(clipboard?.files || [])];
  if (!files.length) {
    // Some browsers only expose a pasted screenshot through the item list.
    files = [...(clipboard?.items || [])]
      .filter((item) => item.kind === "file")
      .map((item) => item.getAsFile())
      .filter(Boolean);
  }
  if (!files.length) return;
  event.preventDefault();
  addFiles(files);
});

let dragDepth = 0;
const showDropHint = (on) => $("#dropHint").classList.toggle("hidden", !on);
const draggingFiles = (event) => [...(event.dataTransfer?.types || [])].includes("Files");

window.addEventListener("dragenter", (event) => {
  if (!state.token || !draggingFiles(event)) return;
  event.preventDefault();
  dragDepth += 1;
  showDropHint(true);
});
window.addEventListener("dragover", (event) => {
  if (!draggingFiles(event)) return;
  event.preventDefault();
});
window.addEventListener("dragleave", () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) showDropHint(false);
});
window.addEventListener("drop", (event) => {
  if (!state.token || !draggingFiles(event)) return;
  event.preventDefault();
  dragDepth = 0;
  showDropHint(false);
  addFiles(event.dataTransfer.files);
});

$("#promptForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  stopDictation();
  const typed = $("#promptInput").value.trim();
  const files = state.attachments.slice();
  const prompt = typed || (files.length ? ATTACHMENT_ONLY_PROMPT : "");
  if (!prompt || state.busy) return;
  const machine = currentMachine();
  // Decide follow-up BEFORE clearing History view — viewing an old thread
  // must never be treated as a follow-up to a live run.
  const asFollowUp = treatAsFollowUp();
  const type = asFollowUp ? "followup" : "prompt";
  state.forceNewPrompt = false;
  // Keep the thread the user is reading; just unlock the live event gate.
  resumeLiveFeed({ keepTranscript: true });
  if (!asFollowUp && state.liveRun?.status === "open") closeRun("ended");
  // New task while a previous run still looks busy: stop it so intent:new
  // can take over instead of bouncing off "Already running".
  if (type === "prompt" && state.running) sendCommand("stop").catch(() => {});
  state.busy = true;
  $("#sendBtn").disabled = true;
  buzz(12);
  const summary = attachmentSummary(files);
  notePrompt(prompt, { asFollowUp, files: summary });
  // Full colour immediately — never a grey "sending" bubble. Thinking shows
  // right away so the feed feels live before the first PC event arrives.
  addEvent({ type: "prompt", payload: { message: prompt, files: summary } });
  showThinkingSoon();
  try {
    if (files.length) markAttachmentsBusy(true);
    const attachments = await packAttachments(files);
    await sendCommand(type, {
      prompt,
      model: state.model,
      planner_model: state.plannerModel,
      max_steps: state.steps,
      reasoning_effort: state.effort,
      shell: state.shell,
      ...(attachments.length ? { attachments } : {})
    });
    $("#promptInput").value = "";
    clearAttachments();
    clearClarifier();
    autoGrow();
    if (!machine?.online) toast("Queued until that computer reconnects");
    // Pull the first AI events immediately — don't wait for the poll timer.
    pollEvents().catch(() => {});
  } catch (error) {
    clearThinkingPlaceholder();
    // Keep the files in the tray so a failed send is one tap from a retry.
    markAttachmentsBusy(false);
    toast(error.message);
  } finally {
    state.busy = false;
    $("#sendBtn").disabled = false;
  }
});

/* ── wiring: platform ─────────────────────────────────── */

window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();
  state.installPrompt = event;
  $("#installBtn").classList.remove("hidden");
});
$("#installBtn").addEventListener("click", async () => {
  if (!state.installPrompt) return;
  state.installPrompt.prompt();
  await state.installPrompt.userChoice;
  state.installPrompt = null;
  $("#installBtn").classList.add("hidden");
});
window.addEventListener("appinstalled", () => toast("aiOS Remote installed"));
window.addEventListener("online", () => { toast("Back online"); loadMachines(); });
window.addEventListener("resize", () => fitViewer());
document.addEventListener("visibilitychange", () => {
  if (!state.token) return;
  if (document.hidden) return;
  // However long you were away, the thread comes back with you: catch up on
  // this machine's backlog instead of wiping the feed.
  loadMachines().then(pollEvents);
  requestStream();
  startFrameLoop();
  refreshFrame();
  applyWakeLock();
});

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("sw.js").catch(() => {});
  // The browser can rotate a push subscription at any time; only the page
  // holds the session token needed to register the new one.
  navigator.serviceWorker.addEventListener("message", (event) => {
    if (event.data?.type === "push-subscription-changed") refreshPushSubscription();
  });
}

/* ── boot ─────────────────────────────────────────────── */

updateRunSettingsLabels();
$("#stepsInput").value = state.steps;
$("#shellToggle").checked = state.shell;
$("#wakeToggle").checked = state.keepAwake;
$("#hapticToggle").checked = state.haptics;
$("#notifyToggle").checked = state.notify;
$("#notifyTestBtn").classList.toggle("hidden", !state.notify);
applyFilter();
toggleScreen(state.screenOpen);
bindViewerGestures();
autoGrow();
loadVersion();

if (new URLSearchParams(location.search).has("compose")) {
  setTimeout(() => $("#promptInput").focus(), 400);
}

state.token ? showApp() : showAuth();
