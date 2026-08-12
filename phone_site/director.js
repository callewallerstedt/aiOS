/* aiOS Director — phone client.

   One screen stack (pair -> agents -> chat), one WebSocket, one event
   reducer. Everything the transcript shows is an event from Director, and
   every event carries the id it was stored under, so a phone that slept
   through a run catches up by asking for everything after the last id it saw
   instead of refetching the conversation.

   No framework, no bundler: this is served as three static files. */

const STORE_KEY = "aios-director";

const state = {
  url: "",
  token: "",
  device: "",
  agents: [],
  filter: "",
  agentId: "",
  shotTimer: null,
  threadId: "",
  cursor: 0,
  socket: null,
  reconnect: 0,
  streaming: null,      // live assistant element
  thinking: null,       // live reasoning element
  tools: new Map(),     // call_id -> element
  jobs: new Map(),      // job_id -> element
  threads: new Map(),   // agentId -> last thread payload
  openGen: 0,
  shot: { image: "", at: 0 },
  working: null,
  lastStampAt: 0,
  busy: false,
  recorder: null,
  chunks: [],
  pending: [],          // files waiting to send with the next message
  appearance: null,
};

/* ---------------- storage ---------------- */

function load() {
  try {
    const raw = JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
    state.url = raw.url || "";
    state.token = raw.token || "";
    state.device = raw.device || "";
    state.agentId = raw.agentId || "";
    if (raw.appearance) applyAppearance(raw.appearance);
  } catch {
    /* first run */
  }
}

function save() {
  localStorage.setItem(STORE_KEY, JSON.stringify({
    url: state.url, token: state.token, device: state.device, agentId: state.agentId,
    appearance: state.appearance,
  }));
}

function applyAppearance(colors) {
  const next = {
    user_bubble: colors?.user_bubble || "#3a5a8c",
    user_text: colors?.user_text || "#f2f3f4",
    agent_bubble: colors?.agent_bubble || "#2b2c2f",
    agent_text: colors?.agent_text || "#f2f3f4",
  };
  state.appearance = next;
  const root = document.documentElement.style;
  root.setProperty("--bubble-user", next.user_bubble);
  root.setProperty("--bubble-user-ink", next.user_text);
  root.setProperty("--bubble-agent", next.agent_bubble);
  root.setProperty("--bubble-agent-ink", next.agent_text);
}

/* ---------------- helpers ---------------- */

const $ = (id) => document.getElementById(id);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function isWide() {
  return window.matchMedia("(min-width: 860px)").matches;
}

function hash32(text) {
  let h = 2166136261;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

const BLOB_COLORS = [
  "#6d8cff", "#5ec8a0", "#f0b45a", "#e56b7d",
  "#c084fc", "#67c3e0", "#f08a5a", "#8fd14f",
  "#f2f0df", "#4a4d55", "#ff7ab6", "#7ad7f0",
];
const BLOB_SHAPES = ["circle", "squircle", "pill", "diamond"];
const BLOB_EMOTIONS = [
  "happy", "sleepy", "thinking", "surprised",
  "wink", "curious", "content", "focused",
];

function blobSpec(seed) {
  const h = hash32(String(seed || "agent"));
  return {
    color: BLOB_COLORS[h % BLOB_COLORS.length],
    shape: BLOB_SHAPES[(h >>> 4) % BLOB_SHAPES.length],
    emotion: BLOB_EMOTIONS[(h >>> 8) % BLOB_EMOTIONS.length],
  };
}

function parseBlob(raw) {
  const text = String(raw || "");
  if (!text.startsWith("blob|")) return null;
  const [, shape, color, emotion] = text.split("|");
  if (!BLOB_SHAPES.includes(shape)) return null;
  if (!/^#[0-9a-fA-F]{6}$/.test(color || "")) return null;
  if (!BLOB_EMOTIONS.includes(emotion)) return null;
  return { shape, color, emotion };
}

function encodeBlob(spec) {
  if (!spec) return "";
  return `blob|${spec.shape}|${spec.color}|${spec.emotion}`;
}

function blobFor(agent) {
  return parseBlob(agent && agent.emoji) || blobSpec((agent && (agent.id || agent.name)) || "director");
}

function blobEyes(emotion) {
  const ink = "#17181a";
  const white = "#f7f7f4";
  const eye = (cx, cy, rx, ry, px = 0, py = 0) =>
    `<ellipse cx="${cx}" cy="${cy}" rx="${rx}" ry="${ry}" fill="${white}"/>`
    + `<circle cx="${cx + px}" cy="${cy + py}" r="${Math.max(1.6, rx * 0.42)}" fill="${ink}"/>`
    + `<circle cx="${cx + px - 1}" cy="${cy + py - 1.2}" r="0.9" fill="${white}" opacity=".85"/>`;
  const arc = (x, flip = 1) =>
    `<path d="M${x} 31c2.2 ${-4 * flip} 8.2 ${-4 * flip} 10.4 0" fill="none" stroke="${ink}" stroke-width="2.5" stroke-linecap="round"/>`;
  switch (emotion) {
    case "happy": return arc(18) + arc(36);
    case "sleepy":
      return `<path d="M18 31h10M36 31h10" fill="none" stroke="${ink}" stroke-width="2.4" stroke-linecap="round"/>`;
    case "thinking": return eye(23, 26, 5.2, 6.2, 1.4, -1.6) + eye(41, 28, 4.4, 5.2, 1.6, -1.2);
    case "surprised": return eye(23, 30, 6.2, 7.2) + eye(41, 30, 6.2, 7.2);
    case "wink": return arc(18) + eye(41, 30, 5.4, 6.2, 0.4, 0.4);
    case "curious": return eye(23, 30, 5.2, 6, -1.8, 0.4) + eye(41, 30, 5.2, 6, -1.8, 0.4);
    case "content":
      return `<circle cx="23" cy="31" r="2.4" fill="${ink}"/><circle cx="41" cy="31" r="2.4" fill="${ink}"/>`;
    default:
      return eye(23, 30, 4.2, 6.8) + eye(41, 30, 4.2, 6.8);
  }
}

function blobBody(spec) {
  const fill = spec.color;
  if (spec.shape === "circle") return `<circle cx="32" cy="32" r="26" fill="${fill}"/>`;
  if (spec.shape === "pill") return `<rect x="4" y="14" width="56" height="36" rx="18" fill="${fill}"/>`;
  if (spec.shape === "diamond") {
    return `<rect x="14" y="14" width="36" height="36" rx="8" fill="${fill}" transform="rotate(45 32 32)"/>`;
  }
  return `<rect x="6" y="6" width="52" height="52" rx="16" fill="${fill}"/>`;
}

function agentMood(agent) {
  if (!agent || agent.frozen) return "idle";
  if (agent.busy || agent.status === "running") return "working";
  if (agent.status === "waiting") return "waiting";
  if (agent.id && agent.id === state.agentId) return "idle";
  return "sleeping";
}

function blobMoodEyes(mood, emotion) {
  const ink = "#17181a";
  if (mood === "sleeping") {
    return `<path d="M18 31c2.4-3.2 8.4-3.2 10.8 0" fill="none" stroke="${ink}" stroke-width="2.6" stroke-linecap="round"/>`
      + `<path d="M35 31c2.4-3.2 8.4-3.2 10.8 0" fill="none" stroke="${ink}" stroke-width="2.6" stroke-linecap="round"/>`;
  }
  if (mood === "working") return blobEyes("focused");
  if (mood === "waiting") return blobEyes("surprised");
  return blobEyes(emotion);
}

function blobExtras(mood) {
  if (mood === "sleeping") {
    return `<g class="blob-zzz" fill="#f2f3f4" font-family="ui-sans-serif, system-ui, sans-serif" font-weight="700">`
      + `<text class="blob-z" x="44" y="16" font-size="8">z</text>`
      + `<text class="blob-z" x="50" y="8" font-size="10">z</text>`
      + `<text class="blob-z" x="56" y="0" font-size="13">Z</text>`
      + `</g>`;
  }
  if (mood === "working") {
    return `<circle class="blob-spin-ring" cx="32" cy="32" r="30" fill="none" stroke="#fff" stroke-opacity=".4" stroke-width="2.2" stroke-linecap="round" stroke-dasharray="16 72"/>`;
  }
  return "";
}

function blobSvg(specOrSeed, mood = "idle") {
  const spec = specOrSeed && specOrSeed.color ? specOrSeed : blobSpec(specOrSeed);
  const shine = `<ellipse cx="22" cy="18" rx="9" ry="5.5" fill="#fff" opacity=".22"/>`;
  const box = mood === "sleeping" ? "-4 -16 76 84" : "0 0 64 64";
  return `<svg viewBox="${box}" aria-hidden="true">${blobBody(spec)}${shine}${blobMoodEyes(mood, spec.emotion)}${blobExtras(mood)}</svg>`;
}

function fillAvatar(node, agent, mood) {
  if (!node) return;
  node.textContent = "";
  node.style.backgroundImage = "";
  node.classList.remove("blob", "photo", "mood-working", "mood-sleeping", "mood-waiting", "mood-idle");
  if (agent && agent.avatar) {
    node.classList.add("photo");
    node.style.backgroundImage = `url(${agent.avatar})`;
    return;
  }
  const resolved = mood || agentMood(agent);
  node.classList.add("blob", `mood-${resolved}`);
  node.innerHTML = blobSvg(blobFor(agent), resolved);
}

function escapeHtml(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/* Markdown-lite. Everything is escaped first, so nothing the model writes can
   inject markup — the tags below are the only ones that exist. */
function markdown(source) {
  const escaped = escapeHtml(source);
  const blocks = [];
  let text = escaped.replace(/```([\w-]*)\n?([\s\S]*?)```/g, (_m, _lang, code) => {
    blocks.push(code.replace(/\n$/, ""));
    return ` BLOCK${blocks.length - 1} `;
  });

  text = text
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
             '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');

  const lines = text.split("\n");
  const out = [];
  let list = null;
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };

  for (const line of lines) {
    const heading = line.match(/^(#{1,3})\s+(.*)$/);
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (heading) {
      closeList();
      out.push(`<h${heading[1].length}>${heading[2]}</h${heading[1].length}>`);
    } else if (bullet) {
      if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; }
      out.push(`<li>${bullet[1]}</li>`);
    } else if (numbered) {
      if (list !== "ol") { closeList(); out.push("<ol>"); list = "ol"; }
      out.push(`<li>${numbered[1]}</li>`);
    } else if (!line.trim()) {
      closeList();
    } else {
      closeList();
      out.push(`<p>${line}</p>`);
    }
  }
  closeList();

  return out.join("")
    .replace(/ BLOCK(\d+) /g, (_m, i) => `<pre><code>${blocks[Number(i)]}</code></pre>`);
}

function relativeTime(seconds) {
  if (!seconds) return "";
  const delta = Date.now() / 1000 - seconds;
  if (delta < 60) return "now";
  if (delta < 3600) return `${Math.floor(delta / 60)}m`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h`;
  if (delta < 604800) return `${Math.floor(delta / 86400)}d`;
  return new Date(seconds * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

const ICONS = {
  shell: '<path d="M4 6l5 6-5 6M12 18h8"/>',
  web: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a15 15 0 0 1 0 18a15 15 0 0 1 0-18"/>',
  search: '<circle cx="11" cy="11" r="6"/><path d="M20 20l-4-4"/>',
  read: '<path d="M6 3h9l4 4v14H6z"/><path d="M15 3v5h4"/>',
  write: '<path d="M4 20h16"/><path d="M14 4l6 6-9 9H5v-6z"/>',
  ls: '<path d="M4 6h16M4 12h16M4 18h10"/>',
  remember: '<path d="M12 3a7 7 0 0 0-4 12.7V19h8v-3.3A7 7 0 0 0 12 3z"/><path d="M9 22h6"/>',
  operator: '<rect x="2.5" y="4" width="19" height="13" rx="2"/><path d="M8 20h8"/>',
  code: '<path d="M9 8l-5 4 5 4M15 8l5 4-5 4"/>',
  screen: '<rect x="2.5" y="4" width="19" height="13" rx="2"/>',
  machines: '<rect x="3" y="5" width="18" height="11" rx="2"/><path d="M7 20h10"/>',
  default: '<circle cx="12" cy="12" r="8"/>',
};

function glyphFor(name) {
  const map = {
    shell: "shell", processes: "shell",
    web_fetch: "web", web_search: "search",
    read_file: "read", write_file: "write", list_dir: "ls",
    remember: "remember", recall: "remember", forget: "remember",
    operator: "operator", operator_screenshot: "screen", operator_takeover: "screen",
    code_session: "code", code_status: "code", machines: "machines",
  };
  return ICONS[map[name] || "default"] || ICONS.default;
}

function svg(paths, className) {
  return `<svg class="${className || ""}" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${paths}</svg>`;
}

/* ---------------- api ---------------- */

function apiUrl(path) {
  return `${state.url.replace(/\/$/, "")}${path}`;
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(apiUrl(path), {
      ...options,
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${state.token}`,
        ...(options.headers || {}),
      },
    });
  } catch {
    throw new Error("Can't reach Director right now.");
  }
  if (response.status === 401) {
    unpair(true);
    throw new Error("This device is no longer paired.");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return data;
}

/* ---------------- screens ---------------- */

function show(screen) {
  const pair = screen === "pair";
  $("screen-pair").classList.toggle("hidden", !pair);
  $("workspace").classList.toggle("hidden", pair);
  if (pair) return;
  const wide = isWide();
  $("screen-agents").classList.toggle("hidden", !wide && screen !== "agents");
  $("screen-chat").classList.toggle("hidden", !wide && screen !== "chat");
  const empty = $("chat-empty");
  if (empty) empty.classList.toggle("hidden", !!state.agentId);
  document.body.classList.toggle("has-chat", !!state.agentId);
}

/* ---------------- pairing ---------------- */

function defaultUrl() {
  // Same origin only makes sense when the page is served by Director itself;
  // on Vercel we cannot guess the tailnet name, so leave it to be typed once.
  const saved = state.url;
  if (saved) return saved;
  if (location.hostname.endsWith(".ts.net")) return `${location.origin}/director`;
  return "";
}

async function doPair(event) {
  event.preventDefault();
  const button = $("pair-submit");
  const errorBox = $("pair-error");
  errorBox.classList.add("hidden");
  button.disabled = true;
  button.textContent = "Connecting…";

  const url = $("pair-url").value.trim().replace(/\/$/, "");
  const code = $("pair-code").value.trim().toUpperCase();
  const name = $("pair-name").value.trim() || (isWide() ? "Computer" : "Phone");

  try {
    if (!url) throw new Error("Director address is required.");
    const response = await fetch(`${url}/api/pair`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code, name, kind: "phone" }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) throw new Error(data.error || `HTTP ${response.status}`);
    state.url = url;
    state.token = data.token;
    state.device = data.device?.name || name;
    save();
    await boot();
  } catch (error) {
    errorBox.textContent = String(error.message || error);
    errorBox.classList.remove("hidden");
  } finally {
    button.disabled = false;
    button.textContent = "Connect";
  }
}

function unpair(silent) {
  state.token = "";
  state.agents = [];
  if (state.socket) { try { state.socket.close(); } catch {} state.socket = null; }
  save();
  show("pair");
  $("pair-url").value = state.url || "";
  if (!silent) $("pair-code").focus();
}

/* ---------------- agent list ---------------- */

function avatarNode(agent, extra = "") {
  const node = el("div", `avatar${extra ? ` ${extra}` : ""}`);
  fillAvatar(node, agent);
  return node;
}

function renderAgents() {
  const list = $("agent-list");
  const query = (state.filter || "").trim().toLowerCase();
  const agents = state.agents.filter((agent) => {
    if (!query) return true;
    const hay = `${agent.name} ${agent.subtitle || ""} ${agent.preview || ""}`.toLowerCase();
    return hay.includes(query);
  });
  if (!agents.length) {
    list.textContent = "";
    list.append(el("div", "empty", state.agents.length ? "No matching chats." : "No agents yet."));
    return;
  }
  const keep = list.scrollTop;
  list.querySelector(".empty")?.remove();
  const existing = new Map();
  for (const row of list.querySelectorAll(".agent-row")) {
    existing.set(row.dataset.agentId, row);
  }
  const seen = new Set();
  agents.forEach((agent, index) => {
    seen.add(agent.id);
    let row = existing.get(agent.id);
    if (!row) {
      row = el("button", `agent-row${agent.id === state.agentId ? " active" : ""}`);
      row.dataset.agentId = agent.id;
      row.style.animationDelay = `${Math.min(index * 26, 200)}ms`;
      row.append(avatarNode(agent));
      const meta = el("div", "meta");
      const name = el("div", "name");
      name.append(document.createTextNode(agent.name));
      if (agent.busy || agent.status === "waiting") name.append(el("span", "dot busy"));
      meta.append(name);
      meta.append(el("div", "preview", agent.preview || agent.subtitle || ""));
      row.append(meta);
      row.append(el("div", "when", relativeTime(agent.updated_at)));
      row.addEventListener("click", () => openAgent(agent.id));
    } else {
      updateAgentRow(row, agent);
    }
    row.classList.toggle("active", agent.id === state.agentId);
    list.append(row);
  });
  for (const [id, row] of existing) {
    if (!seen.has(id)) row.remove();
  }
  list.scrollTop = keep;
  list.classList.add("is-ready");
}

function updateAgentRow(row, agent) {
  const name = row.querySelector(".name");
  if (name) {
    name.textContent = agent.name;
    if (agent.busy || agent.status === "waiting") name.append(el("span", "dot busy"));
  }
  const preview = row.querySelector(".preview");
  if (preview) preview.textContent = agent.preview || agent.subtitle || "";
  const when = row.querySelector(".when");
  if (when) when.textContent = relativeTime(agent.updated_at);
  const av = row.querySelector(".avatar");
  if (av) fillAvatar(av, agent);
}

function markActiveAgent(agentId) {
  const list = $("agent-list");
  if (!list) return;
  for (const row of list.querySelectorAll(".agent-row")) {
    row.classList.toggle("active", row.dataset.agentId === agentId);
  }
}

/* ---------------- transcript ---------------- */

function transcriptEl() { return $("transcript"); }

function appendTranscript(node) {
  const spacer = $("transcript-spacer");
  const host = transcriptEl();
  if (spacer && spacer.parentNode === host) host.insertBefore(node, spacer);
  else host.append(node);
}

function atBottom() {
  const node = transcriptEl();
  return node.scrollHeight - node.scrollTop - node.clientHeight < 90;
}

function scrollDown(force) {
  const node = transcriptEl();
  if (force || atBottom()) {
    requestAnimationFrame(() => { node.scrollTop = node.scrollHeight; });
  }
}

function attachThumbs(attachments) {
  if (!attachments || !attachments.length) return null;
  const wrap = el("div", "attach-thumbs");
  for (const item of attachments) {
    const url = item.url || item.data || "";
    if (String(item.type || "").startsWith("image") || String(url).startsWith("data:image")) {
      const img = document.createElement("img");
      img.src = url;
      img.alt = item.name || "image";
      wrap.append(img);
    } else {
      wrap.append(el("div", "bubble-file", item.name || "file"));
    }
  }
  return wrap;
}

const STAMP_GAP_MS = 10 * 60 * 1000;

function messageTime(value) {
  const n = Number(value);
  if (!n) return Date.now();
  return n > 1e12 ? n : n * 1000;
}

function formatChatStamp(ms) {
  const date = new Date(ms);
  const now = new Date();
  const time = date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  const start = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const dayDelta = Math.round((start(now) - start(date)) / 86400000);
  if (dayDelta === 0) return time;
  if (dayDelta === 1) return `Yesterday ${time}`;
  if (dayDelta > 1 && dayDelta < 7) {
    return `${date.toLocaleDateString(undefined, { weekday: "short" })} ${time}`;
  }
  const day = date.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: date.getFullYear() === now.getFullYear() ? undefined : "numeric",
  });
  return `${day} ${time}`;
}

function stampIfNeeded(at) {
  const ms = messageTime(at);
  if (!state.lastStampAt || ms - state.lastStampAt >= STAMP_GAP_MS) {
    appendTranscript(el("div", "chat-stamp", formatChatStamp(ms)));
  }
  if (!state.lastStampAt || ms >= state.lastStampAt) state.lastStampAt = ms;
}

function addUser(text, attachments, at) {
  stampIfNeeded(at);
  const row = el("div", "row-user");
  const node = el("div", "bubble-user");
  const thumbs = attachThumbs(attachments);
  if (thumbs) node.append(thumbs);
  if (text) node.append(document.createTextNode(text));
  row.append(node);
  appendTranscript(row);
  scrollDown(true);
  return node;
}

function addAssistant(text, at) {
  stampIfNeeded(at);
  const row = el("div", "row-agent");
  const node = el("div", "bubble-agent assistant");
  node.innerHTML = markdown(text);
  row.append(node);
  appendTranscript(row);
  scrollDown();
  return node;
}

function addStatus(text, isError) {
  const node = el("div", `status-line${isError ? " error" : ""}`, text);
  appendTranscript(node);
  scrollDown();
  return node;
}

function loadingPixels() {
  return `<span class="loading-pixels anim-wave" aria-hidden="true">${"<span></span>".repeat(9)}</span>`;
}

function churningRow(label) {
  const node = el("div", "working-sentinel live");
  node.innerHTML = `${loadingPixels()}<span class="working-label">${escapeHtml(label)}</span>`;
  return node;
}

function ensureWorking(label) {
  if (state.working) {
    const lab = state.working.querySelector(".working-label");
    if (lab && label) lab.textContent = label;
    return state.working;
  }
  const node = churningRow(label || "Churning");
  appendTranscript(node);
  state.working = node;
  scrollDown();
  return node;
}

function settleWorking() {
  if (!state.working) return;
  state.working.remove();
  state.working = null;
}

function ensureThinking() {
  settleWorking();
  if (state.thinking) return state.thinking;
  const wrap = el("div", "thinking live");
  const head = el("button", "thinking-head");
  head.innerHTML = `<span class="thinking-label">Thinking</span>${svg('<path d="M6 9l6 6 6-6"/>', "thinking-chevron")}`;
  const body = el("div", "thinking-body");
  head.addEventListener("click", () => wrap.classList.toggle("expanded"));
  wrap.append(head, body);
  appendTranscript(wrap);
  state.thinking = wrap;
  scrollDown();
  return wrap;
}

function settleThinking() {
  if (!state.thinking) return;
  state.thinking.classList.remove("live");
  const label = state.thinking.querySelector(".thinking-label");
  if (label) label.textContent = "Thought";
  state.thinking = null;
}

function toolCard({ callId, name, args, card, running }) {
  if (running) settleWorking();
  const wrap = el("div", `tool-card${running ? " running" : ""}`);
  const chip = el("button", "tool-chip");
  const detail = card?.preview || summariseArgs(args) || "";
  const meta = card?.meta || (running ? "…" : "");
  chip.innerHTML =
    `<span class="glyph">${running ? loadingPixels() : svg(glyphFor(name))}</span>` +
    `<span class="name">${escapeHtml(card?.title || name)}</span>` +
    `<span class="detail">${escapeHtml(detail)}</span>` +
    `<span class="meta">${escapeHtml(meta)}</span>` +
    svg('<path d="M6 9l6 6 6-6"/>', "card-chevron");
  const body = el("div", "tool-body", card?.body || "");
  chip.addEventListener("click", () => {
    if (!body.textContent) return;
    wrap.classList.toggle("expanded");
  });
  wrap.append(chip, body);
  if (card?.tone) wrap.classList.add(card.tone);
  if (callId) state.tools.set(callId, wrap);
  appendTranscript(wrap);
  scrollDown();
  return wrap;
}

function markPendingTool(text) {
  const running = [...state.tools.values()].pop();
  if (running) running.querySelector(".tool-chip .meta").textContent = text;
}

function summariseArgs(args) {
  if (!args || typeof args !== "object") return "";
  for (const key of ["command", "task", "url", "query", "path", "key", "question", "summary"]) {
    if (args[key]) return String(args[key]);
  }
  const first = Object.values(args)[0];
  return first === undefined ? "" : String(first);
}

function finishTool(callId, name, card) {
  const wrap = state.tools.get(callId);
  if (!wrap) { toolCard({ callId, name, card }); return; }
  state.tools.delete(callId);
  wrap.classList.remove("running");
  if (card?.tone) wrap.classList.add(card.tone);
  const chip = wrap.querySelector(".tool-chip");
  chip.querySelector(".name").textContent = card?.title || name;
  const glyph = chip.querySelector(".glyph");
  if (glyph) glyph.innerHTML = svg(glyphFor(name));
  if (card?.preview) chip.querySelector(".detail").textContent = card.preview;
  chip.querySelector(".meta").textContent = card?.meta || "";
  const body = wrap.querySelector(".tool-body");
  body.textContent = card?.body || "";
  if (card?.takeover) {
    const open = el("button", "btn ghost", "Open the screen");
    open.addEventListener("click", () => openTakeover(card.takeover));
    wrap.append(open);
  }
  scrollDown();
}

function approvalCard(payload) {
  const wrap = el("div", "action-card");
  wrap.dataset.approval = payload.id;
  wrap.append(kicker("Needs your approval",
    '<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9L2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>'));
  wrap.append(el("div", "summary", payload.summary || "Approve this?"));
  if (payload.detail) {
    wrap.append(el("pre", null, payload.detail));
  }
  const row = el("div", "row");
  const approve = el("button", "btn primary", "Approve");
  const decline = el("button", "btn danger", "Decline");
  approve.addEventListener("click", () => decide(payload.id, "approved", wrap));
  decline.addEventListener("click", () => decide(payload.id, "declined", wrap));
  row.append(approve, decline);
  wrap.append(row);

  // Widening the yes. "Everything" is a real permission change, so it says so
  // rather than hiding behind a checkbox.
  const more = el("div", "row scopes");
  const forRun = el("button", "btn ghost", "Approve the rest of this run");
  const forAgent = el("button", "btn ghost", "Always allow this agent");
  const forAll = el("button", "btn ghost", "Approve everything, always");
  forRun.addEventListener("click", () => decide(payload.id, "approved", wrap, "run"));
  forAgent.addEventListener("click", () => decide(payload.id, "approved", wrap, "agent"));
  forAll.addEventListener("click", () => {
    if (confirm("Approve every action from every agent from now on, without asking? "
                + "You can turn this off in Settings.")) {
      decide(payload.id, "approved", wrap, "all");
    }
  });
  more.append(forRun, forAgent, forAll);
  wrap.append(more);

  appendTranscript(wrap);
  scrollDown(true);
  return wrap;
}

function kicker(text, paths) {
  const node = el("div", "kicker");
  node.innerHTML = `${svg(paths)}<span>${escapeHtml(text)}</span>`;
  return node;
}

const SCOPE_WORDS = {
  run: "Approved for the rest of this run.",
  agent: "This agent is now always allowed.",
  all: "Everything is approved from now on. Turn it off in Settings.",
};

async function decide(id, status, wrap, scope = "") {
  wrap.classList.add("settled", status);
  wrap.append(el("div", "verdict",
    status === "approved" ? (SCOPE_WORDS[scope] || "You approved this.") : "You declined this."));
  try {
    await api(`/api/approvals/${id}`, {
      method: "POST", body: JSON.stringify({ status, scope }),
    });
  } catch (error) {
    addStatus(String(error.message || error), true);
  }
}

function questionCard(payload) {
  const wrap = el("div", "action-card");
  wrap.dataset.question = payload.id;
  const isHandoff = payload.kind === "handoff";
  wrap.append(kicker(isHandoff ? "Your turn on the screen" : "Director is asking",
    isHandoff
      ? '<rect x="2.5" y="4" width="19" height="13" rx="2"/><path d="M8 20h8"/>'
      : '<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.6 2.6 0 1 1 3.4 2.5c-.6.2-.9.8-.9 1.4v.6M12 17h.01"/>'));
  wrap.append(el("div", "summary", payload.question || ""));

  const row = el("div", "row");
  if (isHandoff || payload.takeover) {
    const open = el("button", "btn", "Open the screen");
    open.addEventListener("click", () => openTakeover(payload.path || "/vnc/vnc.html?autoconnect=1&resize=scale&path=vnc/ws"));
    row.append(open);
  }
  for (const option of payload.options || []) {
    const button = el("button", "btn" + (option === "Done" ? " primary" : ""), option);
    button.addEventListener("click", () => answer(payload.id, option, wrap));
    row.append(button);
  }
  if (!(payload.options || []).length) {
    const input = el("input");
    input.placeholder = "Your answer";
    input.className = "answer";
    const send = el("button", "btn primary", "Send");
    send.addEventListener("click", () => answer(payload.id, input.value, wrap));
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); answer(payload.id, input.value, wrap); }
    });
    const field = el("div", "field");
    field.append(input);
    wrap.append(field);
    row.append(send);
  }
  wrap.append(row);
  appendTranscript(wrap);
  scrollDown(true);
  return wrap;
}

async function answer(id, text, wrap) {
  wrap.classList.add("settled");
  wrap.append(el("div", "verdict", `You answered: ${text}`));
  try {
    await api(`/api/questions/${id}`, { method: "POST", body: JSON.stringify({ answer: text }) });
  } catch (error) {
    addStatus(String(error.message || error), true);
  }
}

function shotCard(payload) {
  if (!payload || !payload.image) return;
  let row = $("live-shot");
  if (!row) {
    row = el("div", "row-agent");
    row.id = "live-shot";
    const wrap = el("div", "shot live");
    const img = document.createElement("img");
    img.alt = "Operator screen";
    wrap.append(img);
    wrap.append(el("div", "cap", "Operator screen"));
    img.addEventListener("click", () => openTakeover("/vnc/view"));
    row.append(wrap);
    appendTranscript(row);
  }
  row.querySelector("img").src = payload.image;
  const preview = $("context-screen-img");
  if (preview) {
    preview.src = payload.image;
    $("context-screen")?.classList.add("has-image");
  }
  scrollDown();
}

function taskRow(jobId, label, stateText) {
  let wrap = state.jobs.get(jobId);
  if (!wrap) {
    wrap = el("div", "task-row");
    const pixels = el("div", "task-pixels");
    pixels.innerHTML = loadingPixels();
    wrap.append(pixels);
    wrap.append(el("div", "label", label));
    wrap.append(el("div", "state", stateText || "running"));
    state.jobs.set(jobId, wrap);
    appendTranscript(wrap);
    scrollDown();
  } else {
    wrap.querySelector(".label").textContent = label;
    wrap.querySelector(".state").textContent = stateText || "";
  }
  return wrap;
}

/* ---------------- rendering stored history ---------------- */

function renderMessages(messages) {
  const node = transcriptEl();
  node.textContent = "";
  const spacer = el("div", "transcript-spacer");
  spacer.id = "transcript-spacer";
  node.append(spacer);
  state.tools.clear();
  state.jobs.clear();
  state.streaming = null;
  state.thinking = null;
  state.working = null;
  state.lastStampAt = 0;

  const pendingTools = new Map();
  for (const message of messages) {
    if (message.role === "user") addUser(message.content, message.meta?.attachments, message.created_at);
    else if (message.role === "assistant") addAssistant(message.content, message.created_at);
    else if (message.role === "system") addStatus(message.content.split("\n")[0]);
    else if (message.role === "tool_call") {
      let args = {};
      try { args = JSON.parse(message.meta?.arguments || "{}"); } catch {}
      pendingTools.set(message.meta?.call_id, { name: message.meta?.name, args });
    } else if (message.role === "tool_result") {
      const pending = pendingTools.get(message.meta?.call_id) || {};
      const output = message.meta?.output || "";
      toolCard({
        callId: null,
        name: pending.name || message.meta?.name || "tool",
        args: pending.args,
        card: {
          title: pending.name || "tool",
          preview: summariseArgs(pending.args),
          meta: output.split("\n")[0].slice(0, 40),
          body: output,
        },
      });
    }
  }
  scrollDown(true);
}

/* ---------------- events ---------------- */

function handleEvent(event) {
  if (event.id) state.cursor = Math.max(state.cursor, event.id);
  const payload = event.payload || {};
  const mine = !event.thread_id || event.thread_id === state.threadId;

  if (!mine) {
    refreshAgentsSoon();
    return;
  }

  switch (event.kind) {
    case "message.user":
      // Echoed by the sender already; only render when it came from elsewhere.
      if (!document.querySelector(`[data-user-pending="${payload.id}"]`)) {
        if (!state.lastSentText || state.lastSentText !== payload.text) {
          addUser(payload.text, payload.attachments, event.created_at);
        }
      }
      state.lastSentText = "";
      break;

    case "reasoning.delta": {
      const wrap = ensureThinking();
      wrap.querySelector(".thinking-body").textContent += payload.text || "";
      break;
    }

    case "message.delta": {
      settleThinking();
      if (!state.streaming) {
        stampIfNeeded(event.created_at);
        const row = el("div", "row-agent");
        state.streaming = el("div", "bubble-agent assistant streaming");
        state.streaming.dataset.raw = "";
        row.append(state.streaming);
        appendTranscript(row);
      }
      state.streaming.dataset.raw += payload.text || "";
      state.streaming.innerHTML = markdown(state.streaming.dataset.raw);
      scrollDown();
      break;
    }

    case "message.assistant":
      settleThinking();
      if (state.streaming) {
        state.streaming.classList.remove("streaming");
        state.streaming.innerHTML = markdown(payload.text || state.streaming.dataset.raw || "");
        state.streaming = null;
      } else {
        addAssistant(payload.text || "", event.created_at);
      }
      scrollDown();
      break;

    case "tool.start":
      settleThinking();
      toolCard({ callId: payload.call_id, name: payload.name, args: payload.arguments, running: true });
      break;

    case "tool.done":
      finishTool(payload.call_id, payload.name, payload.card);
      break;

    case "approval":
      // The chip for this tool is already on screen, above the card asking to
      // allow it. Say so on the chip, or it reads as though the command ran
      // and was approved afterwards.
      markPendingTool("waiting for approval");
      approvalCard(payload);
      break;

    case "approval.decided": {
      const wrap = document.querySelector(`[data-approval="${payload.id}"]`);
      if (wrap && !wrap.classList.contains("settled")) {
        wrap.classList.add("settled", payload.status || "");
        wrap.append(el("div", "verdict", `Decided: ${payload.status}`));
      }
      break;
    }

    case "question":
      questionCard(payload);
      break;

    case "question.answered": {
      const wrap = document.querySelector(`[data-question="${payload.id}"]`);
      if (wrap && !wrap.classList.contains("settled")) {
        wrap.classList.add("settled");
        wrap.append(el("div", "verdict", `Answered: ${payload.answer}`));
      }
      break;
    }

    case "operator.screenshot":
      shotCard(payload);
      break;

    case "operator.started":
    case "operator.step":
    case "operator.actions":
    case "operator.done":
    case "operator.failed":
    case "operator.stopped":
      break;

    case "operator.takeover":
      openTakeover(payload.path);
      break;

    case "code.started":
      taskRow(payload.job_id, `CODE on ${payload.machine}: ${payload.task || ""}`, "running");
      break;

    case "code.progress":
      taskRow(payload.job_id, payload.title || "CODE session", payload.status || "running");
      break;

    case "job.finished": {
      const wrap = state.jobs.get(payload.id);
      if (wrap) {
        wrap.classList.add(payload.status === "done" ? "done" : "failed");
        wrap.querySelector(".state").textContent = payload.status || "";
      }
      break;
    }

    case "thread.status":
      setBusy(payload.status === "running" || payload.status === "waiting");
      if (payload.status === "idle") { settleThinking(); state.streaming = null; }
      break;

    case "thread.error":
      settleThinking();
      addStatus(payload.error || "Something went wrong.", true);
      break;

    case "thread.steered":
      addStatus("Sent — Director will pick this up in the current run.");
      break;

    case "routine.fired":
      addStatus(`Routine: ${payload.name}`);
      break;

    case "routine.created":
      addStatus(`Scheduled "${payload.name}" — ${payload.schedule}`);
      refreshAgentsSoon();
      break;

    default:
      break;
  }

  refreshAgentsSoon();
}

let refreshTimer = 0;
function refreshAgentsSoon() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => { loadAgents().catch(() => {}); }, 900);
}

/* ---------------- websocket ---------------- */

function connect() {
  const previous = state.socket;
  state.socket = null;
  if (previous) {
    try { previous.close(); } catch {}
  }
  const wsUrl = apiUrl(`/ws?token=${encodeURIComponent(state.token)}&since=${state.cursor}`)
    .replace(/^http/, "ws");
  const socket = new WebSocket(wsUrl);
  state.socket = socket;

  socket.addEventListener("open", () => {
    if (state.socket !== socket) return;
    state.reconnect = 0;
    setConnection(true);
  });
  socket.addEventListener("message", (message) => {
    if (state.socket !== socket) return;
    let event;
    try { event = JSON.parse(message.data); } catch { return; }
    if (event.kind === "ready") { state.cursor = event.payload?.cursor || state.cursor; return; }
    handleEvent(event);
  });
  socket.addEventListener("close", (event) => {
    if (state.socket !== socket) return;
    state.socket = null;
    state.reconnect = Math.min((state.reconnect || 0) + 1, 6);
    setConnection(false, !event.wasClean && state.reconnect >= 5);
    const wait = 400 * 2 ** state.reconnect;
    setTimeout(() => {
      if (state.token && !state.socket) connect();
    }, wait);
  });
  socket.addEventListener("error", () => {
    if (state.socket !== socket) return;
    setConnection(false, true);
    try { socket.close(); } catch {}
  });
}

function setConnection(online, issue) {
  const conn = issue ? "err" : (online ? "on" : "off");
  const label = issue ? "Connection issue" : (online ? "Connected" : "Offline");
  document.querySelectorAll(".conn-dot").forEach((dot) => {
    dot.dataset.conn = conn;
    dot.setAttribute("aria-label", label);
  });
  const deviceSub = $("device-sub");
  if (deviceSub) deviceSub.textContent = "Director";
  const deviceName = $("device-name");
  if (deviceName) deviceName.textContent = state.device || "This device";
}

function setBusy(busy) {
  state.busy = busy;
  const stop = $("btn-stop");
  const send = $("btn-send");
  if (stop) stop.classList.toggle("hidden", !busy);
  if (send) send.classList.toggle("hidden", false);
  $("chat-sub").textContent = busy ? "working..." : ($("chat-sub").dataset.idle || "");
  const agent = state.agents.find((row) => row.id === state.agentId);
  if (agent) {
    agent.busy = busy;
    if (busy && agent.status !== "waiting") agent.status = "running";
    if (!busy) agent.status = "idle";
    fillAvatar($("chat-avatar"), agent);
    paintAgentRow(agent);
  }
  if (busy) ensureWorking();
  else settleWorking();
}

function paintAgentRow(agent) {
  const row = $("agent-list")?.querySelector(`[data-agent-id="${agent.id}"]`);
  if (!row) return;
  const name = row.querySelector(".name");
  if (name) {
    const dot = name.querySelector(".dot.busy");
    const show = agent.busy || agent.status === "waiting";
    if (show && !dot) name.append(el("span", "dot busy"));
    if (!show && dot) dot.remove();
  }
  const av = row.querySelector(".avatar");
  if (av) fillAvatar(av, agent);
}

/* ---------------- actions ---------------- */

async function loadAgents() {
  const data = await api("/api/state");
  state.agents = data.agents || [];
  renderAgents();
  setConnection(state.socket?.readyState === WebSocket.OPEN);
  prefetchThreads();
  return data;
}

function prefetchThreads() {
  const ids = state.agents
    .filter((agent) => agent.id && agent.id !== state.agentId && !state.threads.has(agent.id))
    .slice(0, 5)
    .map((agent) => agent.id);
  for (const id of ids) {
    api(`/api/agents/${id}/thread`).then((data) => {
      if (!data?.thread || state.threads.has(id)) return;
      state.threads.set(id, {
        thread: data.thread,
        messages: data.messages || [],
        cursor: data.cursor || 0,
        at: Date.now(),
      });
    }).catch(() => {});
  }
}

function paintChatHeader(agent) {
  $("chat-name").textContent = agent.name || "Director";
  $("composer-input").placeholder = `Message ${agent.name || "Director"}…`;
  const sub = $("chat-sub");
  const bits = [agent.subtitle || ""];
  if (agent.routines) bits.push(`${agent.routines} scheduled`);
  if (agent.auto_approve) bits.push("auto-approved");
  const line = bits.filter(Boolean).join(" · ");
  sub.dataset.idle = line;
  sub.textContent = line;
  fillAvatar($("chat-avatar"), agent);
  const label = $("context-screen-label");
  if (label) label.textContent = `${agent.name || "Operator"}'s screen`;
}

async function openAgent(agentId) {
  const gen = ++state.openGen;
  if (state.threadId) markWatching(state.threadId, false);
  state.agentId = agentId;
  save();
  const agent = state.agents.find((row) => row.id === agentId) || {};
  paintChatHeader(agent);
  markActiveAgent(agentId);
  show("chat");
  refreshContext();

  const cached = state.threads.get(agentId);
  if (cached) {
    state.threadId = cached.thread.id;
    state.cursor = Math.max(state.cursor, cached.cursor || 0);
    renderMessages(cached.messages || []);
    setBusy(cached.thread.status === "running" || cached.thread.status === "waiting");
    markWatching(state.threadId, true);
  } else {
    transcriptEl().textContent = "";
    appendTranscript(churningRow("Loading"));
  }

  try {
    const data = await api(`/api/agents/${agentId}/thread`);
    if (gen !== state.openGen) return;
    state.threads.set(agentId, {
      thread: data.thread,
      messages: data.messages || [],
      cursor: data.cursor || 0,
      at: Date.now(),
    });
    state.threadId = data.thread.id;
    state.cursor = Math.max(state.cursor, data.cursor || 0);
    renderMessages(data.messages || []);
    setBusy(data.thread.status === "running" || data.thread.status === "waiting");
    markWatching(state.threadId, true);
    if (!(data.messages || []).length) {
      appendTranscript(el("div", "empty", `Say something to ${agent.name || "Director"}.`));
    }
  } catch (error) {
    if (gen !== state.openGen) return;
    if (!cached) addStatus(String(error.message || error), true);
  }
}

async function send() {
  const input = $("composer-input");
  const text = input.value.trim();
  const attachments = state.pending.slice();
  if (!text && !attachments.length) return;
  input.value = "";
  input.style.height = "auto";
  state.pending = [];
  renderAttachTray();
  const empty = transcriptEl().querySelector(".empty");
  if (empty) empty.remove();
  addUser(text, attachments);
  state.lastSentText = text;
  setBusy(true);
  try {
    await api(`/api/threads/${state.threadId}/messages`, {
      method: "POST", body: JSON.stringify({ text, attachments }),
    });
  } catch (error) {
    addStatus(String(error.message || error), true);
    setBusy(false);
  }
}

async function stopTurn() {
  if (!state.threadId) return;
  try {
    await api(`/api/threads/${state.threadId}/stop`, { method: "POST" });
  } catch (error) {
    addStatus(String(error.message || error), true);
  }
}

/* ---------------- voice ---------------- */

async function toggleMic() {
  const button = $("btn-mic");
  if (state.recorder && state.recorder.state === "recording") {
    state.recorder.stop();
    return;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    addStatus("Microphone permission was refused.", true);
    return;
  }
  const recorder = new MediaRecorder(stream);
  state.recorder = recorder;
  state.chunks = [];
  recorder.addEventListener("dataavailable", (event) => {
    if (event.data.size) state.chunks.push(event.data);
  });
  recorder.addEventListener("stop", async () => {
    button.classList.remove("recording");
    stream.getTracks().forEach((track) => track.stop());
    state.recorder = null;
    const blob = new Blob(state.chunks, { type: recorder.mimeType || "audio/webm" });
    if (blob.size < 1200) return;
    const form = new FormData();
    form.append("audio", blob, "clip.webm");
    try {
      const response = await fetch(apiUrl("/api/voice/transcribe"), {
        method: "POST",
        headers: { Authorization: `Bearer ${state.token}` },
        body: form,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.ok) throw new Error(data.error || `HTTP ${response.status}`);
      const input = $("composer-input");
      input.value = (input.value ? `${input.value} ` : "") + (data.text || "");
      autosize(input);
      if (data.text) await send();
    } catch (error) {
      addStatus(String(error.message || error), true);
    }
  });
  recorder.start();
  button.classList.add("recording");
}

/* ---------------- notifications ---------------- */

function urlBase64ToUint8Array(base64) {
  const padded = (base64 + "=".repeat((4 - (base64.length % 4)) % 4))
    .replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(padded);
  return Uint8Array.from([...raw].map((char) => char.charCodeAt(0)));
}

/* Ask once, when the user does something that implies they want alerts. iOS
   only grants this from a user gesture inside an installed PWA, so it is wired
   to the Settings button rather than fired on load. */
async function enableNotifications({ silent = false } = {}) {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
    if (!silent) alert("This browser cannot do push notifications. On iPhone, add "
                       + "Director to the Home Screen first.");
    return false;
  }
  try {
    const permission = await Notification.requestPermission();
    if (permission !== "granted") {
      if (!silent) alert("Notifications are turned off for this app.");
      return false;
    }
    const key = await api("/api/push/key");
    if (!key.public_key) {
      if (!silent) alert("Director has no push keys configured.");
      return false;
    }
    const registration = await navigator.serviceWorker.ready;
    const existing = await registration.pushManager.getSubscription();
    const subscription = existing || await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(key.public_key),
    });
    await api("/api/push/subscribe", {
      method: "POST", body: JSON.stringify({ subscription: subscription.toJSON() }),
    });
    state.pushOn = true;
    return true;
  } catch (error) {
    if (!silent) alert(`Could not turn on notifications: ${error.message || error}`);
    return false;
  }
}

async function disableNotifications() {
  try {
    const registration = await navigator.serviceWorker.ready;
    const subscription = await registration.pushManager.getSubscription();
    if (subscription) {
      await api("/api/push/unsubscribe", {
        method: "POST", body: JSON.stringify({ endpoint: subscription.endpoint }) });
      await subscription.unsubscribe();
    }
    state.pushOn = false;
  } catch { /* already gone */ }
}

async function pushIsOn() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) return false;
  if (Notification.permission !== "granted") return false;
  try {
    const registration = await navigator.serviceWorker.ready;
    return !!(await registration.pushManager.getSubscription());
  } catch {
    return false;
  }
}

/* Director skips a notification for a thread the phone is actually looking at. */
function markWatching(threadId, active) {
  if (!threadId || !state.token) return;
  api(`/api/threads/${threadId}/watching`, {
    method: "POST", body: JSON.stringify({ active: !!active }),
  }).catch(() => {});
}

/* ---------------- takeover ---------------- */

function takeoverSrc(path) {
  let base = path || "/vnc/view";
  if (base.includes("vnc.html")) base = "/vnc/view";
  const joiner = base.includes("?") ? "&" : "?";
  return apiUrl(`${base}${joiner}token=${encodeURIComponent(state.token)}&t=${Date.now()}`);
}

function takeoverOpen() {
  const wrap = $("takeover");
  return wrap && !wrap.classList.contains("hidden");
}

function closeTakeover() {
  const wrap = $("takeover");
  if (!wrap) return;
  wrap.classList.add("hidden");
  const frame = wrap.querySelector("iframe");
  if (frame) {
    frame.dataset.path = "";
    frame.src = "about:blank";
  }
}

function ensureTakeoverShell() {
  let wrap = $("takeover");
  if (wrap) return wrap;
  wrap = el("div");
  wrap.id = "takeover";
  wrap.classList.add("hidden");
  const bar = el("div", "bar");
  bar.append(el("div", "label", "Operator screen — you are in control"));
  const close = el("button", "icon-btn");
  close.innerHTML = svg('<path d="M6 6l12 12M18 6L6 18"/>');
  close.addEventListener("click", closeTakeover);
  bar.append(close);
  const frame = document.createElement("iframe");
  frame.src = "about:blank";
  frame.allow = "clipboard-read; clipboard-write";
  wrap.append(bar, frame);
  document.body.append(wrap);
  return wrap;
}

function openTakeover(path) {
  const wrap = ensureTakeoverShell();
  const frame = wrap.querySelector("iframe");
  const wanted = path || "/vnc/view";
  wrap.classList.remove("hidden");
  if (frame) {
    frame.src = takeoverSrc(wanted);
    frame.dataset.path = wanted;
  }
}

/* ---------------- settings ---------------- */

async function openSettings() {
  const { body, dismiss } = openSheet("Settings");

  body.append(el("div", "status-line", "Loading…"));
  let data;
  try {
    const [settings, modelInfo, health] = await Promise.all([
      api("/api/settings"), api("/api/models"), api("/api/state"),
    ]);
    data = { settings: settings.settings, models: modelInfo, health };
  } catch (error) {
    body.textContent = "";
    body.append(el("div", "notice", String(error.message || error)));
    return;
  }
  body.textContent = "";

  // notifications
  const alerts = el("div", "group");
  alerts.append(el("h3", null, "Notifications"));
  alerts.append(el("div", "hint",
    "Replies, approvals, questions and reminders arrive on this phone even when "
    + "the app is closed. On iPhone, add Director to the Home Screen first."));
  const pushOn = await pushIsOn();
  const pushRow = el("div", "row");
  const enable = el("button", `btn${pushOn ? "" : " primary"}`,
                    pushOn ? "Notifications are on" : "Turn on notifications");
  enable.addEventListener("click", async () => {
    if (await pushIsOn()) {
      await disableNotifications();
      enable.textContent = "Turn on notifications";
      enable.classList.add("primary");
    } else if (await enableNotifications()) {
      enable.textContent = "Notifications are on";
      enable.classList.remove("primary");
    }
  });
  const test = el("button", "btn ghost", "Send a test");
  test.addEventListener("click", async () => {
    test.textContent = "Sending…";
    try {
      const got = await api("/api/push/test", { method: "POST" });
      test.textContent = got.sent ? `Sent to ${got.sent}` : "No devices subscribed";
    } catch (error) {
      test.textContent = String(error.message || error).slice(0, 40);
    }
  });
  pushRow.append(enable, test);
  alerts.append(pushRow);
  body.append(alerts);

  // permissions
  const permissions = el("div", "group");
  permissions.append(el("h3", null, "Permissions"));
  const approveAll = toggleRow("Approve everything, always",
                               !!(data.settings.safety || {}).approve_all);
  permissions.append(approveAll.row);
  permissions.append(el("div", "hint",
    "With this on, Director never stops to ask before running a command, writing "
    + "a file, sending or deleting. It still tells you what it did."));
  const savePermissions = el("button", "btn", "Save permissions");
  savePermissions.addEventListener("click", async () => {
    await api("/api/settings", {
      method: "PATCH",
      body: JSON.stringify({ safety: { approve_all: approveAll.value() } }),
    });
    savePermissions.textContent = "Saved";
  });
  permissions.append(savePermissions);
  body.append(permissions);

  // backends
  const backends = el("div", "group");
  backends.append(el("h3", null, "Model backends"));
  for (const row of data.models.backends || []) {
    const line = el("div", "line");
    line.append(el("span", null, row.backend));
    line.append(el("span", "v", row.message));
    backends.append(line);
  }
  const keyField = el("div", "field");
  keyField.append(el("label", null, "OpenRouter API key"));
  const keyInput = el("input");
  keyInput.type = "password";
  keyInput.placeholder = data.settings.backends?.openrouter?.api_key || "sk-or-…";
  keyField.append(keyInput);
  backends.append(keyField);

  const voiceField = el("div", "field");
  voiceField.append(el("label", null, "OpenAI key (voice transcription)"));
  const voiceInput = el("input");
  voiceInput.type = "password";
  voiceInput.placeholder = data.settings.voice?.openai_api_key || "sk-…";
  voiceField.append(voiceInput);
  backends.append(voiceField);

  const saveKeys = el("button", "btn primary", "Save keys");
  saveKeys.addEventListener("click", async () => {
    const patch = { backends: {}, voice: {} };
    if (keyInput.value.trim()) patch.backends.openrouter = { api_key: keyInput.value.trim() };
    if (voiceInput.value.trim()) patch.voice.openai_api_key = voiceInput.value.trim();
    try {
      await api("/api/settings", { method: "PATCH", body: JSON.stringify(patch) });
      saveKeys.textContent = "Saved";
      keyInput.value = ""; voiceInput.value = "";
    } catch (error) {
      saveKeys.textContent = String(error.message || error);
    }
  });
  backends.append(saveKeys);
  body.append(backends);

  // default model
  const defaults = el("div", "group");
  defaults.append(el("h3", null, "Default model"));
  const modelField = el("div", "field");
  modelField.append(el("label", null, "Coordinator model"));
  const select = el("select");
  for (const model of data.models.codex_models || []) {
    const option = document.createElement("option");
    option.value = `codex:${model.id}`;
    option.textContent = `${model.label} (Codex)`;
    select.append(option);
  }
  const custom = document.createElement("option");
  custom.value = "openrouter:";
  custom.textContent = "OpenRouter model…";
  select.append(custom);
  select.value = `${data.settings.defaults?.backend || "codex"}:${data.settings.defaults?.model || ""}`;
  modelField.append(select);
  defaults.append(modelField);

  const orField = el("div", "field");
  orField.append(el("label", null, "OpenRouter model id (when chosen above)"));
  const orInput = el("input");
  orInput.placeholder = "anthropic/claude-sonnet-4.5";
  orField.append(orInput);
  defaults.append(orField);

  const reasoningField = el("div", "field");
  reasoningField.append(el("label", null, "Reasoning"));
  const reasoning = el("select");
  for (const level of ["none", "low", "medium", "high"]) {
    const option = document.createElement("option");
    option.value = level; option.textContent = level;
    reasoning.append(option);
  }
  reasoning.value = data.settings.defaults?.reasoning || "low";
  reasoningField.append(reasoning);
  defaults.append(reasoningField);

  const saveModel = el("button", "btn primary", "Save model");
  saveModel.addEventListener("click", async () => {
    const [backend, model] = select.value.split(":");
    const patch = {
      defaults: {
        backend,
        model: backend === "openrouter" ? orInput.value.trim() : model,
        reasoning: reasoning.value,
      },
    };
    try {
      await api("/api/settings", { method: "PATCH", body: JSON.stringify(patch) });
      saveModel.textContent = "Saved";
    } catch (error) {
      saveModel.textContent = String(error.message || error);
    }
  });
  defaults.append(saveModel);
  body.append(defaults);

  // operator
  const operator = el("div", "group");
  operator.append(el("h3", null, "Operator screen"));
  const op = data.health.operator || {};
  const opLine = el("div", "line");
  opLine.append(el("span", null, `display ${op.display || ":99"}`));
  opLine.append(el("span", "v", op.ready ? "running" : "stopped"));
  operator.append(opLine);
  const openScreen = el("button", "btn", "Open the screen");
  openScreen.addEventListener("click", () => { dismiss(); openTakeover(op.takeover_path); });
  operator.append(openScreen);
  body.append(operator);

  // routines across every agent
  const schedules = el("div", "group");
  schedules.append(el("h3", null, "Routines"));
  try {
    const all = (await api("/api/routines")).routines || [];
    if (!all.length) {
      schedules.append(el("div", "hint",
        "Nothing scheduled. Ask any agent: \"every weekday at 7:30, tell me what's "
        + "on today\"."));
    }
    for (const routine of all) {
      const line = el("div", "line");
      line.append(el("span", null, routine.name));
      line.append(el("span", "v", routine.enabled ? routine.next_human : "paused"));
      schedules.append(line);
    }
  } catch { /* shown empty */ }
  body.append(schedules);

  // machines
  const machines = el("div", "group");
  machines.append(el("h3", null, "Machines"));
  for (const machine of data.health.machines || []) {
    const line = el("div", "line");
    line.append(el("span", null, machine.name));
    line.append(el("span", "v", machine.online ? "online" : "offline"));
    machines.append(line);
  }
  if (!(data.health.machines || []).length) {
    machines.append(el("div", "line", "None paired yet."));
  }
  body.append(machines);

  // chat colors
  const look = el("div", "group");
  look.append(el("h3", null, "Chat colors"));
  const colors = data.settings.appearance || state.appearance || {};
  const fields = [
    ["Your bubble", "user_bubble", colors.user_bubble || "#3a5a8c"],
    ["Your text", "user_text", colors.user_text || "#f2f3f4"],
    ["Agent bubble", "agent_bubble", colors.agent_bubble || "#2b2c2f"],
    ["Agent text", "agent_text", colors.agent_text || "#f2f3f4"],
  ];
  const colorInputs = {};
  for (const [label, key, value] of fields) {
    const field = el("div", "field");
    field.append(el("label", null, label));
    const input = el("input");
    input.type = "color";
    input.value = /^#[0-9a-fA-F]{6}$/.test(value) ? value : "#2b2c2f";
    colorInputs[key] = input;
    field.append(input);
    look.append(field);
  }
  const saveColors = el("button", "btn primary", "Save colors");
  saveColors.addEventListener("click", async () => {
    const appearance = {
      user_bubble: colorInputs.user_bubble.value,
      user_text: colorInputs.user_text.value,
      agent_bubble: colorInputs.agent_bubble.value,
      agent_text: colorInputs.agent_text.value,
    };
    applyAppearance(appearance);
    save();
    try {
      await api("/api/settings", { method: "PATCH", body: JSON.stringify({ appearance }) });
      saveColors.textContent = "Saved";
    } catch (error) {
      saveColors.textContent = String(error.message || error);
    }
  });
  look.append(saveColors);
  body.append(look);

  // device
  const device = el("div", "group");
  device.append(el("h3", null, "This device"));
  const line = el("div", "line");
  line.append(el("span", null, state.device || "Phone"));
  line.append(el("span", "v", state.url));
  device.append(line);
  const unpairButton = el("button", "btn danger", "Unpair this device");
  unpairButton.addEventListener("click", () => { dismiss(); unpair(); });
  device.append(unpairButton);
  body.append(device);
}

/* ---------------- sheets: the shell every editor uses ---------------- */

function openSheet(title) {
  const backdrop = el("div", "sheet-backdrop");
  const sheet = el("div", isWide() ? "sheet dialog" : "sheet");
  const head = el("div", "sheet-head");
  head.append(el("h2", null, title));
  const close = el("button", "icon-btn");
  close.innerHTML = svg('<path d="M6 6l12 12M18 6L6 18"/>');
  const dismiss = () => { backdrop.remove(); sheet.remove(); };
  close.addEventListener("click", dismiss);
  backdrop.addEventListener("click", dismiss);
  head.append(close);
  const body = el("div", "sheet-body");
  sheet.append(head, body);
  document.body.append(backdrop, sheet);
  return { body, dismiss, head };
}

function field(label, node) {
  const wrap = el("div", "field");
  wrap.append(el("label", null, label));
  wrap.append(node);
  return wrap;
}

function textInput(value, placeholder) {
  const node = el("input");
  node.value = value || "";
  if (placeholder) node.placeholder = placeholder;
  return node;
}

/* Photos become data URLs so an avatar is just another field on the agent —
   no upload endpoint, no file store, and it survives a reinstall. */
function readImage(file, max = 256) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("could not read that image"));
    reader.onload = () => {
      const img = new Image();
      img.onerror = () => reject(new Error("that file is not an image"));
      img.onload = () => {
        const side = Math.min(img.width, img.height);
        const canvas = document.createElement("canvas");
        canvas.width = canvas.height = max;
        const ctx = canvas.getContext("2d");
        ctx.drawImage(img, (img.width - side) / 2, (img.height - side) / 2,
                      side, side, 0, 0, max, max);
        resolve(canvas.toDataURL("image/jpeg", 0.82));
      };
      img.src = reader.result;
    };
    reader.readAsDataURL(file);
  });
}

async function agentEditor(agent) {
  const creating = !agent;
  const draft = {
    name: agent?.name || "",
    blob: parseBlob(agent?.emoji) || blobSpec(agent?.id || agent?.name || `new-${Date.now()}`),
    avatar: agent?.avatar || "",
    subtitle: agent?.subtitle || "",
    system_prompt: agent?.system_prompt || "",
    backend: agent?.backend || "",
    model: agent?.model || "",
    reasoning: agent?.reasoning || "",
    auto_approve: !!agent?.auto_approve,
    notify: agent ? !!agent.notify : true,
  };
  const { body, dismiss } = openSheet(creating ? "New agent" : `Edit ${draft.name}`);

  // identity
  const identity = el("div", "group");
  identity.append(el("h3", null, "Identity"));

  const picker = el("div", "avatar-picker");
  const preview = el("div", "avatar big");
  const paintAvatar = () => {
    fillAvatar(preview, {
      id: agent?.id || draft.name || "new",
      avatar: draft.avatar,
      emoji: encodeBlob(draft.blob),
      frozen: true,
    }, "idle");
  };
  paintAvatar();
  const file = el("input");
  file.type = "file";
  file.accept = "image/*";
  file.className = "hidden";
  file.addEventListener("change", async () => {
    if (!file.files?.[0]) return;
    try {
      draft.avatar = await readImage(file.files[0]);
      paintAvatar();
    } catch (error) {
      alert(String(error.message || error));
    }
  });
  const choose = el("button", "btn ghost", "Choose a photo");
  choose.addEventListener("click", () => file.click());
  const clearPhoto = el("button", "btn ghost", "Use a blob");
  clearPhoto.addEventListener("click", () => { draft.avatar = ""; paintAvatar(); });
  const pickerButtons = el("div", "avatar-actions");
  pickerButtons.append(choose, clearPhoto);
  picker.append(preview, pickerButtons, file);
  identity.append(picker);

  const blobBox = el("div", "blob-customize");
  const paintChoices = () => {
    blobBox.querySelectorAll("[data-blob]").forEach((btn) => {
      const [kind, value] = btn.dataset.blob.split(":");
      btn.classList.toggle("on", String(draft.blob[kind]) === value);
    });
  };

  const shapeRow = el("div", "blob-row");
  shapeRow.append(el("div", "blob-label", "Shape"));
  const shapeGrid = el("div", "blob-grid");
  for (const shape of BLOB_SHAPES) {
    const btn = el("button", "blob-choice");
    btn.type = "button";
    btn.dataset.blob = `shape:${shape}`;
    btn.title = shape;
    btn.innerHTML = blobSvg({ ...draft.blob, shape, emotion: "content" });
    btn.addEventListener("click", () => {
      draft.avatar = "";
      draft.blob = { ...draft.blob, shape };
      paintAvatar();
      paintChoices();
      shapeGrid.querySelectorAll("button").forEach((node) => {
        const s = node.dataset.blob.split(":")[1];
        node.innerHTML = blobSvg({ ...draft.blob, shape: s, emotion: "content" });
      });
    });
    shapeGrid.append(btn);
  }
  shapeRow.append(shapeGrid);
  blobBox.append(shapeRow);

  const colorRow = el("div", "blob-row");
  colorRow.append(el("div", "blob-label", "Color"));
  const swatches = el("div", "blob-swatches");
  for (const color of BLOB_COLORS) {
    const btn = el("button", "blob-swatch");
    btn.type = "button";
    btn.dataset.blob = `color:${color}`;
    btn.style.background = color;
    btn.title = color;
    btn.addEventListener("click", () => {
      draft.avatar = "";
      draft.blob = { ...draft.blob, color };
      customColor.value = color;
      paintAvatar();
      paintChoices();
    });
    swatches.append(btn);
  }
  const customColor = el("input");
  customColor.type = "color";
  customColor.className = "blob-color";
  customColor.value = /^#[0-9a-fA-F]{6}$/.test(draft.blob.color) ? draft.blob.color : "#6d8cff";
  customColor.addEventListener("input", () => {
    draft.avatar = "";
    draft.blob = { ...draft.blob, color: customColor.value };
    paintAvatar();
    paintChoices();
  });
  swatches.append(customColor);
  colorRow.append(swatches);
  blobBox.append(colorRow);

  const eyeRow = el("div", "blob-row");
  eyeRow.append(el("div", "blob-label", "Eyes"));
  const eyeGrid = el("div", "blob-grid eyes");
  for (const emotion of BLOB_EMOTIONS) {
    const btn = el("button", "blob-choice");
    btn.type = "button";
    btn.dataset.blob = `emotion:${emotion}`;
    btn.title = emotion;
    btn.innerHTML = blobSvg({ ...draft.blob, emotion });
    btn.addEventListener("click", () => {
      draft.avatar = "";
      draft.blob = { ...draft.blob, emotion };
      paintAvatar();
      paintChoices();
    });
    eyeGrid.append(btn);
  }
  eyeRow.append(eyeGrid);
  blobBox.append(eyeRow);

  const shuffle = el("button", "btn ghost", "Shuffle this blob");
  shuffle.addEventListener("click", () => {
    draft.avatar = "";
    draft.blob = blobSpec(`${agent?.id || draft.name || "new"}-${Math.random()}`);
    customColor.value = draft.blob.color;
    paintAvatar();
    paintChoices();
    shapeGrid.querySelectorAll("button").forEach((node) => {
      const s = node.dataset.blob.split(":")[1];
      node.innerHTML = blobSvg({ ...draft.blob, shape: s, emotion: "content" });
    });
    eyeGrid.querySelectorAll("button").forEach((node) => {
      const e = node.dataset.blob.split(":")[1];
      node.innerHTML = blobSvg({ ...draft.blob, emotion: e });
    });
  });
  blobBox.append(shuffle);
  identity.append(blobBox);
  paintChoices();

  const nameInput = textInput(draft.name, "Coder, Home, Money…");
  identity.append(field("Name", nameInput));
  identity.append(el("div", "hint",
    "Pick a shape, color and eyes — or a photo, if you want a real picture."));
  const subtitleInput = textInput(draft.subtitle, "What this one is for");
  identity.append(field("Subtitle", subtitleInput));
  body.append(identity);

  // instructions
  const behaviour = el("div", "group");
  behaviour.append(el("h3", null, "Custom instructions"));
  const promptInput = el("textarea");
  promptInput.rows = 7;
  promptInput.value = draft.system_prompt;
  promptInput.placeholder = "How this agent should work, what it should care about, "
                          + "anything it should always do or never do.";
  behaviour.append(promptInput);
  behaviour.append(el("div", "hint",
    "The agent reads this as its own instructions and can quote it back to you. "
    + "Every agent is a full Director underneath — this shapes it, it does not limit it."));
  body.append(behaviour);

  // model
  const modelGroup = el("div", "group");
  modelGroup.append(el("h3", null, "Model"));
  const select = el("select");
  const inherit = document.createElement("option");
  inherit.value = "";
  inherit.textContent = "Use the default";
  select.append(inherit);
  let catalogue = { codex_models: [] };
  try { catalogue = await api("/api/models"); } catch { /* offline: default only */ }
  for (const model of catalogue.codex_models || []) {
    const option = document.createElement("option");
    option.value = `codex:${model.id}`;
    option.textContent = `${model.label} (Codex)`;
    select.append(option);
  }
  const custom = document.createElement("option");
  custom.value = "openrouter:";
  custom.textContent = "OpenRouter model…";
  select.append(custom);
  select.value = draft.backend ? `${draft.backend}:${draft.model}` : "";
  modelGroup.append(field("Which model", select));

  const orInput = textInput(draft.backend === "openrouter" ? draft.model : "",
                            "anthropic/claude-sonnet-4.5");
  modelGroup.append(field("OpenRouter model id", orInput));

  const reasoning = el("select");
  for (const level of ["", "none", "low", "medium", "high"]) {
    const option = document.createElement("option");
    option.value = level;
    option.textContent = level || "Use the default";
    reasoning.append(option);
  }
  reasoning.value = draft.reasoning;
  modelGroup.append(field("Reasoning", reasoning));
  body.append(modelGroup);

  // permissions and alerts
  const behaviourGroup = el("div", "group");
  behaviourGroup.append(el("h3", null, "Permissions and alerts"));
  const autoRow = toggleRow("Approve everything from this agent", draft.auto_approve);
  const notifyRow = toggleRow("Notify me about this agent", draft.notify);
  behaviourGroup.append(autoRow.row, notifyRow.row);
  body.append(behaviourGroup);

  // actions
  const actions = el("div", "group");
  const save = el("button", "btn primary", creating ? "Create agent" : "Save");
  save.addEventListener("click", async () => {
    const [backend, model] = (select.value || ":").split(":");
    const payload = {
      name: nameInput.value.trim(),
      emoji: encodeBlob(draft.blob),
      avatar: draft.avatar,
      subtitle: subtitleInput.value.trim(),
      system_prompt: promptInput.value,
      backend: backend || "",
      model: backend === "openrouter" ? orInput.value.trim() : (model || ""),
      reasoning: reasoning.value,
      auto_approve: autoRow.value(),
      notify: notifyRow.value(),
    };
    if (!payload.name) { alert("Give it a name."); return; }
    save.disabled = true;
    try {
      if (creating) {
        await api("/api/agents", { method: "POST", body: JSON.stringify(payload) });
      } else {
        await api(`/api/agents/${agent.id}`, { method: "PATCH", body: JSON.stringify(payload) });
      }
      dismiss();
      await loadAgents();
      if (!creating && state.agentId === agent.id) {
        const fresh = state.agents.find((row) => row.id === agent.id);
        if (fresh) paintChatHeader(fresh);
      }
    } catch (error) {
      alert(String(error.message || error));
      save.disabled = false;
    }
  });
  actions.append(save);
  if (!creating && agent.kind === "custom") {
    const remove = el("button", "btn danger", "Delete this agent");
    remove.addEventListener("click", async () => {
      if (!confirm(`Delete ${agent.name} and its conversation?`)) return;
      await api(`/api/agents/${agent.id}`, { method: "DELETE" });
      dismiss();
      if (state.agentId === agent.id) state.agentId = "";
      show("agents");
      await loadAgents();
    });
    actions.append(remove);
  }
  body.append(actions);
}

function toggleRow(label, value) {
  const row = el("div", "line toggle");
  row.append(el("span", null, label));
  const button = el("button", `switch${value ? " on" : ""}`);
  button.append(el("span", "knob"));
  let current = !!value;
  button.addEventListener("click", () => {
    current = !current;
    button.classList.toggle("on", current);
  });
  row.append(button);
  return { row, value: () => current };
}

async function newAgent() {
  await agentEditor(null);
}

function chatMenu() {
  const agent = state.agents.find((row) => row.id === state.agentId) || {};
  const { body, dismiss } = openSheet(agent.name || "Conversation");
  const group = el("div", "group");

  const options = [
    ["Edit this agent", () => { dismiss(); agentEditor(agent); }],
    ["Routines", () => { dismiss(); routinesSheet(agent); }],
    ["Open the operator screen", () => { dismiss(); openTakeover(); }],
    ["Start a fresh conversation", async () => {
      dismiss();
      await api(`/api/threads/${state.threadId}/clear`, { method: "POST" });
      await openAgent(state.agentId);
    }],
    ["Stop the current run", async () => {
      dismiss();
      await api(`/api/threads/${state.threadId}/stop`, { method: "POST" });
    }],
  ];
  for (const [label, action] of options) {
    const button = el("button", "btn", label);
    button.addEventListener("click", action);
    group.append(button);
  }
  body.append(group);
}

/* ---------------- routines ---------------- */

async function routinesSheet(agent) {
  const { body } = openSheet(`${agent.name} — routines`);
  body.append(el("div", "status-line", "Loading…"));
  let rows = [];
  try {
    rows = (await api(`/api/routines?agent_id=${agent.id}`)).routines || [];
  } catch (error) {
    body.textContent = "";
    body.append(el("div", "notice", String(error.message || error)));
    return;
  }
  body.textContent = "";

  const list = el("div", "group");
  list.append(el("h3", null, "Scheduled"));
  if (!rows.length) {
    list.append(el("div", "hint", "Nothing scheduled yet. You can also just ask in "
      + "chat — \"remind me every Friday at five to do the invoices\"."));
  }
  for (const routine of rows) {
    const item = el("div", "routine");
    const top = el("div", "routine-top");
    top.append(el("span", "routine-name", routine.name));
    top.append(el("span", "routine-when", routine.enabled ? routine.next_human : "paused"));
    item.append(top);
    item.append(el("div", "routine-schedule", routine.described));
    item.append(el("div", "routine-prompt", routine.prompt));
    const controls = el("div", "row");
    const toggle = el("button", "btn ghost", routine.enabled ? "Pause" : "Resume");
    toggle.addEventListener("click", async () => {
      await api(`/api/routines/${routine.id}`, {
        method: "PATCH", body: JSON.stringify({ enabled: !routine.enabled }) });
      toggle.textContent = routine.enabled ? "Resume" : "Pause";
      routine.enabled = !routine.enabled;
    });
    const runNow = el("button", "btn ghost", "Run now");
    runNow.addEventListener("click", async () => {
      runNow.textContent = "Running…";
      await api(`/api/routines/${routine.id}/run`, { method: "POST" });
      runNow.textContent = "Ran";
    });
    const remove = el("button", "btn ghost danger", "Delete");
    remove.addEventListener("click", async () => {
      await api(`/api/routines/${routine.id}`, { method: "DELETE" });
      item.remove();
    });
    controls.append(toggle, runNow, remove);
    item.append(controls);
    list.append(item);
  }
  body.append(list);

  // new routine
  const add = el("div", "group");
  add.append(el("h3", null, "New routine"));
  const name = textInput("", "morning digest");
  add.append(field("Name", name));
  const prompt = el("textarea");
  prompt.rows = 3;
  prompt.placeholder = "What should the agent do when this fires?";
  add.append(field("Prompt", prompt));

  const kind = el("select");
  for (const [value, label] of [["daily", "Every day"], ["weekdays", "Every weekday"],
                                ["weekly", "Every week"], ["interval", "Every N minutes"],
                                ["once", "Once, later"]]) {
    const option = document.createElement("option");
    option.value = value; option.textContent = label;
    kind.append(option);
  }
  add.append(field("How often", kind));

  const time = textInput("08:00", "HH:MM");
  const timeField = field("At", time);
  add.append(timeField);

  const weekday = el("select");
  ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    .forEach((day, index) => {
      const option = document.createElement("option");
      option.value = String(index); option.textContent = day;
      weekday.append(option);
    });
  const weekdayField = field("On", weekday);
  add.append(weekdayField);

  const minutes = textInput("30", "30");
  const minutesField = field("Every (minutes)", minutes);
  add.append(minutesField);

  const paint = () => {
    timeField.classList.toggle("hidden", kind.value === "interval");
    weekdayField.classList.toggle("hidden", kind.value !== "weekly");
    minutesField.classList.toggle("hidden", kind.value !== "interval");
  };
  kind.addEventListener("change", paint);
  paint();

  const create = el("button", "btn primary", "Schedule it");
  create.addEventListener("click", async () => {
    const schedule = { kind: kind.value };
    if (kind.value === "interval") {
      schedule.seconds = Math.max(60, Number(minutes.value || 30) * 60);
    } else if (kind.value === "once") {
      const [hh, mm] = (time.value || "08:00").split(":").map(Number);
      const when = new Date();
      when.setHours(hh || 0, mm || 0, 0, 0);
      if (when.getTime() < Date.now()) when.setDate(when.getDate() + 1);
      schedule.at = when.getTime() / 1000;
    } else {
      schedule.time = time.value || "08:00";
      if (kind.value === "weekly") schedule.weekday = Number(weekday.value);
    }
    try {
      await api("/api/routines", {
        method: "POST",
        body: JSON.stringify({ agent_id: agent.id, name: name.value.trim(),
                               prompt: prompt.value.trim(), schedule }),
      });
      await routinesSheet(agent);
    } catch (error) {
      alert(String(error.message || error));
    }
  });
  add.append(create);
  body.append(add);
}

/* ---------------- attachments ---------------- */

function renderAttachTray() {
  const tray = $("attach-tray");
  if (!tray) return;
  tray.textContent = "";
  tray.classList.toggle("hidden", !state.pending.length);
  state.pending.forEach((item, index) => {
    const chip = el("div", "attach-chip");
    if (String(item.type || "").startsWith("image") || String(item.url || "").startsWith("data:image")) {
      const img = document.createElement("img");
      img.src = item.url;
      img.alt = item.name || "image";
      chip.append(img);
    } else {
      chip.append(el("div", "name", item.name || "file"));
    }
    const x = el("button", "x", "x");
    x.addEventListener("click", () => {
      state.pending.splice(index, 1);
      renderAttachTray();
    });
    chip.append(x);
    tray.append(chip);
  });
}

function fileToAttachment(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("could not read file"));
    reader.onload = () => {
      const url = String(reader.result || "");
      if (String(file.type || "").startsWith("image/") && url.startsWith("data:image")) {
        const img = new Image();
        img.onload = () => {
          const max = 1600;
          let { width, height } = img;
          if (width > max || height > max) {
            const scale = Math.min(max / width, max / height);
            width = Math.round(width * scale);
            height = Math.round(height * scale);
          }
          const canvas = document.createElement("canvas");
          canvas.width = width;
          canvas.height = height;
          canvas.getContext("2d").drawImage(img, 0, 0, width, height);
          resolve({
            name: file.name || "image.jpg",
            type: "image/jpeg",
            url: canvas.toDataURL("image/jpeg", 0.82),
          });
        };
        img.onerror = () => resolve({ name: file.name || "image", type: file.type || "image", url });
        img.src = url;
        return;
      }
      resolve({ name: file.name || "file", type: file.type || "application/octet-stream", url });
    };
    reader.readAsDataURL(file);
  });
}

async function addFiles(fileList) {
  const files = Array.from(fileList || []);
  for (const file of files) {
    try {
      const item = await fileToAttachment(file);
      if (item.url && item.url.length > 12 * 1024 * 1024) {
        addStatus(`${item.name} is too large to send.`, true);
        continue;
      }
      state.pending.push(item);
    } catch (error) {
      addStatus(String(error.message || error), true);
    }
  }
  renderAttachTray();
}

function toggleAttachMenu() {
  $("attach-menu").classList.toggle("hidden");
}

function pickAttach(kind) {
  $("attach-menu").classList.add("hidden");
  const id = kind === "camera" ? "file-camera" : kind === "gallery" ? "file-gallery" : "file-files";
  $(id).click();
}

/* ---------------- composer plumbing ---------------- */

function autosize(node) {
  node.style.height = "auto";
  node.style.height = `${Math.min(node.scrollHeight, 148)}px`;
}

/* ---------------- desktop context pane ---------------- */

async function refreshContext() {
  paintRoutinesPanel();
  await refreshScreenPreview();
  if (state.shotTimer) clearInterval(state.shotTimer);
  state.shotTimer = setInterval(() => {
    if (document.visibilityState !== "visible") return;
    if ($("workspace").classList.contains("hidden")) return;
    if (takeoverOpen()) return;
    refreshScreenPreview().catch(() => {});
  }, 8000);
}

async function paintRoutinesPanel() {
  const host = $("context-routines");
  if (!host) return;
  host.textContent = "";
  if (!state.agentId) {
    host.append(el("div", "hint", "Open a chat to see its routines."));
    return;
  }
  try {
    const rows = (await api(`/api/routines?agent_id=${state.agentId}`)).routines || [];
    if (!rows.length) {
      host.append(el("div", "hint", "Nothing scheduled. Ask in chat, or tap +."));
      return;
    }
    for (const routine of rows) {
      const item = el("button", "routine-mini");
      item.append(el("div", "name", routine.name));
      item.append(el("div", "when", routine.enabled ? (routine.described || routine.next_human) : "paused"));
      item.addEventListener("click", () => {
        const agent = state.agents.find((row) => row.id === state.agentId);
        if (agent) routinesSheet(agent);
      });
      host.append(item);
    }
  } catch (error) {
    host.append(el("div", "hint", String(error.message || error)));
  }
}

async function refreshScreenPreview() {
  const card = $("context-screen");
  const img = $("context-screen-img");
  if (!card || !img || !state.token) return;
  if (takeoverOpen()) return;
  if (state.shot.image && Date.now() - state.shot.at < 2000) {
    if (img.getAttribute("src") !== state.shot.image) {
      img.src = state.shot.image;
      card.classList.add("has-image");
    }
    return;
  }
  try {
    const data = await api("/api/operator/screenshot?preview=1");
    if (!data.image || takeoverOpen()) return;
    if (data.image === state.shot.image) {
      state.shot.at = Date.now();
      return;
    }
    await new Promise((resolve) => {
      const probe = new Image();
      probe.onload = resolve;
      probe.onerror = resolve;
      probe.src = data.image;
    });
    if (takeoverOpen()) return;
    state.shot = { image: data.image, at: Date.now() };
    img.src = data.image;
    card.classList.add("has-image");
  } catch {
    if (!state.shot.image) card.classList.remove("has-image");
  }
}

/* ---------------- boot ---------------- */

async function boot() {
  fillAvatar($("pair-mark"), { id: "agt_director" });
  fillAvatar($("device-blob"), { id: state.device || "device" });
  fillAvatar($("empty-blob"), { id: "agt_director" });
  if ($("pair-name") && !$("pair-name").value) {
    $("pair-name").placeholder = isWide() ? "Computer" : "iPhone";
  }
  if (!state.token || !state.url) {
    $("pair-url").value = defaultUrl();
    show("pair");
    return;
  }
  show("agents");
  try {
    await loadAgents();
    const settings = await api("/api/settings").catch(() => null);
    if (settings?.settings?.appearance) {
      applyAppearance(settings.settings.appearance);
      save();
    }
  } catch (error) {
    const message = String(error.message || error);
    if (/no longer paired/i.test(message)) {
      show("pair");
      $("pair-url").value = state.url;
      const box = $("pair-error");
      box.textContent = message;
      box.classList.remove("hidden");
      return;
    }
    setConnection(false, true);
  }
  connect();
  api("/api/operator/start", { method: "POST" }).catch(() => {});

  // Deep link from a notification tap: ?agent=agt_x
  const wanted = new URLSearchParams(location.search).get("agent");
  const target = wanted && state.agents.some((agent) => agent.id === wanted)
    ? wanted : state.agentId;
  if (target && state.agents.some((agent) => agent.id === target)) {
    await openAgent(target).catch(() => show("agents"));
  }

  // Already-granted permission is re-subscribed silently; the subscription can
  // be rotated by the browser at any time and a stale one pushes into a void.
  if ("Notification" in window && Notification.permission === "granted") {
    enableNotifications({ silent: true }).catch(() => {});
  }
}

function wire() {
  $("pair-form").addEventListener("submit", doPair);
  $("btn-settings").addEventListener("click", openSettings);
  $("btn-settings-mobile")?.addEventListener("click", openSettings);
  $("btn-new-agent").addEventListener("click", newAgent);
  $("btn-search")?.addEventListener("click", () => {
    const box = $("sidebar-search");
    if (!box) return;
    const open = box.classList.toggle("hidden") === false;
    if (open) $("agent-search")?.focus();
  });
  $("agent-search")?.addEventListener("input", (event) => {
    state.filter = event.target.value || "";
    renderAgents();
  });
  $("btn-new-routine")?.addEventListener("click", () => {
    const agent = state.agents.find((row) => row.id === state.agentId);
    if (agent) routinesSheet(agent);
  });
  $("context-screen")?.addEventListener("click", () => openTakeover());
  $("btn-back").addEventListener("click", () => {
    state.agentId = "";
    show("agents");
    loadAgents().catch(() => {});
  });
  window.addEventListener("resize", () => {
    if (!state.token) return;
    show(state.agentId ? "chat" : "agents");
  });
  $("btn-chat-menu").addEventListener("click", chatMenu);
  $("btn-screen").addEventListener("click", () => openTakeover());
  $("btn-send").addEventListener("click", send);
  $("btn-stop").addEventListener("click", stopTurn);
  $("btn-mic").addEventListener("click", toggleMic);
  $("btn-attach").addEventListener("click", toggleAttachMenu);
  $("attach-menu").addEventListener("click", (event) => {
    const kind = event.target?.dataset?.kind;
    if (kind) pickAttach(kind);
  });
  $("file-camera").addEventListener("change", (event) => { addFiles(event.target.files); event.target.value = ""; });
  $("file-gallery").addEventListener("change", (event) => { addFiles(event.target.files); event.target.value = ""; });
  $("file-files").addEventListener("change", (event) => { addFiles(event.target.files); event.target.value = ""; });

  const input = $("composer-input");
  input.addEventListener("input", () => autosize(input));
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && window.matchMedia("(min-width: 760px)").matches) {
      event.preventDefault();
      send();
    }
  });
  input.addEventListener("paste", (event) => {
    const files = Array.from(event.clipboardData?.items || [])
      .filter((item) => item.kind === "file")
      .map((item) => item.getAsFile())
      .filter(Boolean);
    if (!files.length) return;
    event.preventDefault();
    addFiles(files);
  });
  document.addEventListener("click", (event) => {
    const menu = $("attach-menu");
    if (!menu || menu.classList.contains("hidden")) return;
    if (menu.contains(event.target) || $("btn-attach").contains(event.target)) return;
    menu.classList.add("hidden");
  });

  document.addEventListener("visibilitychange", () => {
    const visible = document.visibilityState === "visible";
    if (visible && state.token) {
      if (!state.socket || state.socket.readyState !== WebSocket.OPEN) connect();
      loadAgents().catch(() => {});
    }
    // Tell Director whether this conversation is actually on screen, so a
    // reply being read does not also buzz the phone.
    if (state.threadId) {
      const looking = visible && !!state.agentId && !$("workspace").classList.contains("hidden");
      markWatching(state.threadId, looking);
    }
  });

  window.addEventListener("pagehide", () => {
    if (state.threadId) markWatching(state.threadId, false);
  });

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
    navigator.serviceWorker.addEventListener("message", (event) => {
      // Opened from a notification: jump to the agent it was about.
      const url = String(event.data?.url || "");
      const match = url.match(/agent=([\w-]+)/);
      if (match && state.agents.some((agent) => agent.id === match[1])) {
        openAgent(match[1]).catch(() => {});
      }
    });
  }
}

load();
wire();
boot();
