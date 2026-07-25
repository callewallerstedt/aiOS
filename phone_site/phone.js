/* aiOS Remote — phone client. */

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const MODELS = [
  { id: "luna", name: "Luna", note: "Fast and precise — the default" },
  { id: "terra", name: "Terra", note: "Steadier on dense screens" },
  { id: "sol", name: "Sol", note: "Strongest reasoning, slower" }
];

const EFFORTS = [
  { id: "low", name: "Low", note: "Acts quickly" },
  { id: "medium", name: "Medium", note: "Balanced" },
  { id: "high", name: "High", note: "Thinks longer before each move" }
];

const RUNNING_STATES = new Set(["running", "starting", "thinking", "acting", "waiting"]);

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
  effort: prefs.get("aios_effort", "low"),
  steps: Number(prefs.get("aios_steps", 30)),
  detailed: prefs.bool("aios_detailed", true),
  haptics: prefs.bool("aios_haptics", true),
  keepAwake: prefs.bool("aios_awake", false),
  screenOpen: prefs.bool("aios_screen_open", true),
  timers: [],
  busy: false,
  loading: false,
  running: false,
  frameUrl: "",
  frameStamp: "",
  frameAt: 0,
  installPrompt: null,
  wakeLock: null,
  stickBottom: true,
  lastStep: null
};

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
  const response = await fetch(path, { ...options, headers });
  const data = response.headers.get("content-type")?.includes("application/json") ? await response.json() : null;
  if (response.status === 401 && state.token && !path.includes("/account/")) clearSession();
  if (!response.ok) throw new Error(data?.error || `Request failed (${response.status})`);
  return data;
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

function openSheet(id) {
  $$(".sheet").forEach((sheet) => sheet.classList.toggle("hidden", sheet.id !== id));
  $("#backdrop").classList.remove("hidden");
  buzz();
  openOverlay(hideSheets);
}

function hideSheets() {
  $$(".sheet").forEach((sheet) => sheet.classList.add("hidden"));
  $("#backdrop").classList.add("hidden");
}

function closeSheets() {
  if (overlayCloser === hideSheets) dismissOverlay();
  else hideSheets();
}

$("#backdrop").addEventListener("click", closeSheets);

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
  state.machineId = id;
  state.cursor = 0;
  state.feedVersion += 1;
  state.monitorId = "";
  state.lastStep = null;
  state.frameStamp = "";
  prefs.set("aios_machine_id", id);
  resetTimeline("Loading this computer’s activity…");
  clearMarkers();
  $("#screenImage").classList.remove("loaded");
  $("#screenPlaceholder").classList.remove("hidden");
  renderMachine();
  pollEvents();
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
  $("#promptInput").placeholder = running ? "Add a follow-up…" : "Ask your computer…";

  const monitors = monitorsOf(machine);
  if (!monitors.some((monitor) => String(monitor.id) === String(state.monitorId))) {
    state.monitorId = String(monitors[0].id);
  }
  $("#monitorLabel").textContent = monitorName(currentMonitor());

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
      buzz();
      if (viewer.zoom > 1.01) resetZoom();
      else setZoom(2.6, touch.clientX, touch.clientY);
    } else {
      viewer.lastTap = now;
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
  while (viewer.markers.length > 4) viewer.markers.shift().remove();
  setTimeout(() => marker.classList.add("faded"), 2000);
  setTimeout(() => { marker.remove(); viewer.markers = viewer.markers.filter((item) => item !== marker); }, 9000);

  const pill = $("#lastClickPill");
  pill.textContent = `${payload.button || "left"} click · ${Math.round(x)}, ${Math.round(y)}`;
  pill.classList.remove("hidden");
  clearTimeout(markClick.timer);
  markClick.timer = setTimeout(() => pill.classList.add("hidden"), 6000);
}

async function refreshFrame() {
  const machine = currentMachine();
  if (!machine) return;
  const monitor = encodeURIComponent(state.monitorId || "primary");
  try {
    const response = await fetch(`/api/machines/${encodeURIComponent(machine.id)}/frame/${monitor}?t=${Date.now()}`, {
      headers: { Authorization: `Bearer ${state.token}` },
      cache: "no-store"
    });
    if (!response.ok) { updateLive(); return; }
    const stamp = response.headers.get("x-aios-updated-at") || "";
    if (stamp && stamp === state.frameStamp) { updateLive(); return; }
    const blob = await response.blob();
    if (machine.id !== state.machineId) return;
    state.frameStamp = stamp;
    state.frameAt = Date.now();
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
  }
}

function updateLive() {
  const fresh = Date.now() - state.frameAt < 9000 && Boolean(currentMachine()?.online);
  $("#livePill").classList.toggle("on", fresh);
}

/* ── timeline ─────────────────────────────────────────── */

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

function scrollFeed(force = false) {
  const feed = $("#timeline");
  if (force || state.stickBottom) {
    feed.scrollTop = feed.scrollHeight;
    hideJump();
  } else {
    $("#jumpBtn").classList.remove("hidden");
  }
}

/** Turns a raw agent event into the shape the timeline renders. */
function describe(event) {
  const payload = event.payload || {};
  const type = String(event.type || "log").toLowerCase();
  const text = (value) => String(value || "").trim();

  if (type === "prompt" || /follow.?up/.test(type)) {
    return { kind: "user", body: text(payload.message || payload.text || payload.prompt) };
  }
  if (type === "step_begin") return { kind: "step", step: payload.n };
  if (type === "screenshot") return { kind: "entry", tone: "muted", glyph: "monitor", title: "Looked at the screen" };
  if (type === "command") return { kind: "entry", tone: "muted", glyph: "bolt", title: "Task received" };
  if (type === "run_start") return { kind: "entry", tone: "muted", glyph: "bolt", title: text(payload.task) || "Task started" };
  if (type === "debug_dir" || type === "step_end") return null;

  if (type === "thought") {
    const say = text(payload.say);
    const thought = text(payload.thought);
    const message = text(payload.message);
    if (say) return { kind: "entry", tone: "say", glyph: "spark", title: "OPERATOR", body: say, extra: state.detailed ? thought : "" };
    return { kind: "entry", tone: "think muted", glyph: "spark", title: "Thinking", body: thought || message };
  }
  if (type === "click_fx") {
    return {
      kind: "entry", tone: "click", glyph: "cursor",
      title: `${text(payload.button) || "left"} click`,
      mono: `${Math.round(Number(payload.x))}, ${Math.round(Number(payload.y))}`,
      click: payload
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
  if (type === "done") {
    const ok = Boolean(payload.ok);
    const stopped = /stop/i.test(text(payload.message));
    return {
      kind: "entry", tone: ok ? "ok" : "err", glyph: ok ? "check" : "stop",
      title: ok ? "Finished" : stopped ? "Stopped" : "Run ended",
      body: text(payload.message),
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

function addEvent(event, pending = false) {
  if (event.type === "click_fx") markClick(event.payload || {});
  if (event.type === "step_begin") $("#runMeta").textContent = `${labelOf(MODELS, state.model)} · step ${(event.payload || {}).n || ""}`;
  if (event.type === "ask") buzz([16, 60, 16]);
  if (event.type === "done") buzz([12, 40, 12]);

  const info = describe(event);
  if (!info) return;
  const feed = $("#timeline");
  feed.querySelector(".timeline-empty")?.remove();
  const wasBottom = nearBottom();
  const stamp = Number(event.created_at || Date.now());

  if (info.kind === "step") {
    if (state.lastStep === info.step) return;
    state.lastStep = info.step;
    const rule = document.createElement("div");
    rule.className = "step-rule";
    rule.textContent = `Step ${info.step ?? ""}`.trim();
    feed.appendChild(rule);
  } else if (info.kind === "user") {
    if (!info.body) return;
    // The optimistic bubble becomes the real one once the agent echoes it back.
    if (!pending) {
      const waiting = [...feed.querySelectorAll(".msg.pending")]
        .find((node) => node.querySelector(".msg-bubble").textContent === info.body);
      if (waiting) {
        waiting.classList.remove("pending");
        waiting.querySelector("time").textContent = clockTime(stamp);
        return;
      }
    }
    const row = document.createElement("div");
    row.className = `msg${pending ? " pending" : ""}`;
    const bubble = document.createElement("div");
    bubble.className = "msg-bubble";
    bubble.textContent = info.body;
    const time = document.createElement("time");
    time.textContent = pending ? "sending" : clockTime(stamp);
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
  state.stickBottom = wasBottom || pending;
  scrollFeed();
}

async function pollEvents() {
  const machine = currentMachine();
  if (!machine) return;
  const id = machine.id;
  const version = state.feedVersion;
  try {
    const data = await api(`/api/machines/${encodeURIComponent(id)}/events?since=${state.cursor}`);
    if (id !== state.machineId || version !== state.feedVersion) return;
    for (const event of data.events || []) addEvent(event);
    state.cursor = Number(data.cursor || state.cursor);
  } catch (error) {
    console.debug(error);
  }
}

/* ── polling ──────────────────────────────────────────── */

function startPolling() {
  stopPolling();
  loadMachines();
  pollEvents();
  refreshFrame();
  state.timers.push(setInterval(loadMachines, 4000));
  state.timers.push(setInterval(pollEvents, 1400));
  state.timers.push(setInterval(refreshFrame, 2200));
  state.timers.push(setInterval(updateLive, 3000));
}

function stopPolling() {
  state.timers.forEach(clearInterval);
  state.timers = [];
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
  return api(`/api/machines/${encodeURIComponent(machine.id)}/commands`, {
    method: "POST",
    body: JSON.stringify({ type, payload })
  });
}

function autoGrow() {
  const input = $("#promptInput");
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, window.innerHeight * 0.34)}px`;
  $("#suggestions").classList.toggle("hidden", Boolean(input.value.trim()) || state.running);
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
      $("#monitorLabel").textContent = monitorName(currentMonitor());
      clearMarkers();
      closeSheets();
      refreshFrame();
    }
  );
  openSheet("monitorSheet");
});

function openModelSheet() {
  renderOptions($("#modelOptions"), MODELS, state.model, (id) => {
    state.model = id;
    prefs.set("aios_model", id);
    $("#modelLabel").textContent = labelOf(MODELS, id);
    $("#settingsModelValue").textContent = labelOf(MODELS, id);
    closeSheets();
    renderMachine();
  });
  openSheet("modelSheet");
}

function openEffortSheet() {
  renderOptions($("#effortOptions"), EFFORTS, state.effort, (id) => {
    state.effort = id;
    prefs.set("aios_effort", id);
    $("#effortLabel").textContent = labelOf(EFFORTS, id);
    $("#settingsEffortValue").textContent = labelOf(EFFORTS, id);
    closeSheets();
  });
  openSheet("effortSheet");
}

$("#modelBtn").addEventListener("click", openModelSheet);
$("#effortBtn").addEventListener("click", openEffortSheet);
$("#settingsModelRow").addEventListener("click", openModelSheet);
$("#settingsEffortRow").addEventListener("click", openEffortSheet);
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
$("#hapticToggle").addEventListener("change", (event) => {
  state.haptics = event.target.checked;
  prefs.set("aios_haptics", state.haptics ? 1 : 0);
  buzz();
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

$("#screenToggle").addEventListener("click", () => { buzz(); toggleScreen(!state.screenOpen); });
$("#expandBtn").addEventListener("click", () => toggleImmersive(!$("#viewer").classList.contains("immersive")));
$("#zoomResetBtn").addEventListener("click", resetZoom);
$("#jumpBtn").addEventListener("click", () => scrollFeed(true));
$("#timeline").addEventListener("scroll", () => { if (nearBottom()) hideJump(); });

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
  const machine = currentMachine();
  if (!machine || !window.confirm("Clear this computer's activity?")) return;
  const button = $("#clearChatBtn");
  button.disabled = true;
  try {
    await api(`/api/machines/${encodeURIComponent(machine.id)}/events`, { method: "DELETE" });
    state.feedVersion += 1;
    state.cursor = 0;
    resetTimeline("Cleared. Ask your computer to do something.");
    toast("Timeline cleared");
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
});

$("#promptInput").addEventListener("input", autoGrow);
$("#promptInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
    event.preventDefault();
    $("#promptForm").requestSubmit();
  }
});
$$(".sugg").forEach((button) => button.addEventListener("click", () => {
  $("#promptInput").value = button.textContent;
  autoGrow();
  $("#promptInput").focus();
}));

$("#micBtn").addEventListener("click", () => (recognition ? stopDictation() : startDictation()));
$("#micStopBtn").addEventListener("click", stopDictation);

$("#promptForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  stopDictation();
  const prompt = $("#promptInput").value.trim();
  if (!prompt || state.busy) return;
  const machine = currentMachine();
  const type = state.running ? "followup" : "prompt";
  state.busy = true;
  $("#sendBtn").disabled = true;
  buzz(12);
  addEvent({ type: "prompt", payload: { message: prompt } }, true);
  try {
    await sendCommand(type, {
      prompt,
      model: state.model,
      max_steps: state.steps,
      reasoning_effort: state.effort
    });
    $("#promptInput").value = "";
    autoGrow();
    if (!machine?.online) toast("Queued until that computer reconnects");
  } catch (error) {
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
  if (document.hidden || !state.token) return;
  loadMachines();
  pollEvents();
  refreshFrame();
  applyWakeLock();
});

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});

/* ── boot ─────────────────────────────────────────────── */

$("#modelLabel").textContent = labelOf(MODELS, state.model);
$("#effortLabel").textContent = labelOf(EFFORTS, state.effort);
$("#settingsModelValue").textContent = labelOf(MODELS, state.model);
$("#settingsEffortValue").textContent = labelOf(EFFORTS, state.effort);
$("#stepsInput").value = state.steps;
$("#wakeToggle").checked = state.keepAwake;
$("#hapticToggle").checked = state.haptics;
applyFilter();
toggleScreen(state.screenOpen);
bindViewerGestures();
autoGrow();

if (new URLSearchParams(location.search).has("compose")) {
  setTimeout(() => $("#promptInput").focus(), 400);
}

state.token ? showApp() : showAuth();
