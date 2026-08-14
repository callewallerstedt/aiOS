/* aiOS Director — phone client.

   One screen stack (pair -> agents -> chat), one WebSocket, one event
   reducer. Everything the transcript shows is an event from Director, and
   every event carries the id it was stored under, so a phone that slept
   through a run catches up by asking for everything after the last id it saw
   instead of refetching the conversation.

   CODE job cards open the real aiOS CODE transcript renderer
   (aios_ui/web/js/transcript.js), fed by events proxied through Director. */

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
  reasoningRendered: false,
  tools: new Map(),     // call_id -> element
  jobs: new Map(),      // job_id -> element
  jobMeta: new Map(),   // job_id -> { session_id, title, ... }
  operatorJobs: new Map(), // job_id -> inline Operator event viewer
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
  codeView: null,       // { jobId, sessionId, since, view }
  historyExpanded: false,
  currentThread: null,
  currentMessages: [],
  screen: "agents",
  groupWorking: new Map(),
  machines: [],
  wake: null,
  pinnedAgentId: "agt_director",
  homeRecording: false,
  phoneMouse: null,
};

/* ---------------- storage ---------------- */

function load() {
  try {
    const raw = JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
    state.url = raw.url || "";
    state.token = raw.token || "";
    state.device = raw.device || "";
    state.agentId = raw.agentId || "";
    state.pinnedAgentId = raw.pinnedAgentId || "agt_director";
    if (raw.appearance) applyAppearance(raw.appearance);
  } catch {
    /* first run */
  }
}

function save() {
  localStorage.setItem(STORE_KEY, JSON.stringify({
    url: state.url, token: state.token, device: state.device, agentId: state.agentId,
    appearance: state.appearance,
    pinnedAgentId: state.pinnedAgentId,
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
  "frustrated", "annoyed", "thinking", "focused", "sleepy",
  "confused", "skeptical", "worried", "mischievous",
  "happy", "wink", "curious", "content", "surprised",
];
const ACTIVE_EYE_ROTATION = [
  "frustrated", "annoyed", "thinking",
  "focused", "sleepy", "confused",
  "skeptical", "worried", "mischievous",
];
const ACTIVE_EYE_INTERVAL = 4500;
const BLOB_EMOTION_LABELS = {
  frustrated: "Frustrated / overwhelmed",
  annoyed: "Annoyed / unimpressed",
  thinking: "Thinking / recalling",
  focused: "Focused / determined",
  sleepy: "Sleepy / calm",
  confused: "Confused / unsure",
  skeptical: "Skeptical / suspicious",
  worried: "Worried / concerned",
  mischievous: "Mischievous / plotting",
};

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
  const brow = (x1, y1, x2, y2, width = 3.6) =>
    `<path d="M${x1} ${y1}L${x2} ${y2}" fill="none" stroke="${ink}" stroke-width="${width}" stroke-linecap="round"/>`;
  switch (emotion) {
    case "happy": return arc(18) + arc(36);
    case "frustrated":
      return `<path d="M18 25l9 6-9 6M46 25l-9 6 9 6" fill="none" stroke="${ink}" stroke-width="4.3" stroke-linecap="round" stroke-linejoin="round"/>`;
    case "annoyed":
      return eye(23, 31, 6.4, 7.5, 0, -0.5) + eye(41, 31, 6.4, 7.5, 0, -0.5)
        + `<path d="M16 25h14M34 25h14" stroke="${ink}" stroke-width="4.4" stroke-linecap="round"/>`;
    case "sleepy":
      return `<path d="M17 29q6 7 12 0M35 29q6 7 12 0" fill="none" stroke="${ink}" stroke-width="3.2" stroke-linecap="round"/>`;
    case "thinking":
      return eye(23, 30, 6.3, 8, 1, -3) + eye(41, 30, 6.3, 8, 1, -3);
    case "focused":
      return eye(23, 32, 6.3, 7.4, 0, 0) + eye(41, 32, 6.3, 7.4, 0, 0)
        + brow(16, 23, 28, 29) + brow(48, 23, 36, 29);
    case "confused":
      return eye(22.5, 30, 6.4, 8, 1.2, -2.2) + eye(42, 29, 5.7, 7.3, 1, -2.4);
    case "skeptical":
      return eye(23, 32, 7, 5.8, 3, 0) + eye(41, 32, 7, 5.8, 3, 0)
        + brow(16, 25, 29, 25, 3.8)
        + `<path d="M35 25q6-6 13 0" fill="none" stroke="${ink}" stroke-width="3.5" stroke-linecap="round"/>`;
    case "worried":
      return eye(23, 32, 6.2, 8, 0, 1) + eye(41, 32, 6.2, 8, 0, 1)
        + `<path d="M16 25q7-6 13-1M35 24q6-5 13 1" fill="none" stroke="${ink}" stroke-width="3.4" stroke-linecap="round"/>`;
    case "mischievous":
      return eye(23, 32, 6.5, 7.2, 0, -0.3) + eye(41, 32, 6.5, 7.2, 0, -0.3)
        + `<path d="M15 24l15 5M49 24l-15 5" stroke="${ink}" stroke-width="4.6" stroke-linecap="round"/>`;
    case "surprised": return eye(23, 30, 6.2, 7.2) + eye(41, 30, 6.2, 7.2);
    case "wink": return arc(18) + eye(41, 30, 5.4, 6.2, 0.4, 0.4);
    case "curious": return eye(23, 30, 5.2, 6, -1.8, 0.4) + eye(41, 30, 5.2, 6, -1.8, 0.4);
    case "content":
      return `<circle cx="23" cy="31" r="2.4" fill="${ink}"/><circle cx="41" cy="31" r="2.4" fill="${ink}"/>`;
    default:
      return eye(23, 30, 4.2, 6.8) + eye(41, 30, 4.2, 6.8);
  }
}

function activeBlobEmotion(agent, mood, now = Date.now()) {
  if (mood !== "working" && mood !== "waiting") return blobFor(agent).emotion;
  const seed = hash32(String(agent?.id || agent?.name || "agent"));
  const turn = Math.floor(now / ACTIVE_EYE_INTERVAL);
  return ACTIVE_EYE_ROTATION[(seed + turn) % ACTIVE_EYE_ROTATION.length];
}

function blobBody(spec) {
  const fill = spec.color;
  if (spec.shape === "circle") return `<circle cx="32" cy="32" r="26" fill="${fill}"/>`;
  if (spec.shape === "pill") return `<rect x="4" y="14" width="56" height="36" rx="18" fill="${fill}"/>`;
  if (spec.shape === "diamond") {
    // 15% larger than the other blob bodies so the rotated square reads as
    // the same visual weight, then a bit more as requested.
    return `<rect x="11.3" y="11.3" width="41.4" height="41.4" rx="9.2" fill="${fill}" transform="rotate(45 32 32)"/>`;
  }
  return `<rect x="6" y="6" width="52" height="52" rx="16" fill="${fill}"/>`;
}

function sleepWindow(agent) {
  const h = hash32(String((agent && (agent.id || agent.name)) || "agent"));
  // Stable per agent: fall asleep some minute between 21:00 and 01:59,
  // wake some minute between 08:00 and 11:59. Never during the day.
  return {
    sleepMins: (21 * 60 + (h % (5 * 60))) % (24 * 60),
    wakeMins: 8 * 60 + ((h >>> 11) % (4 * 60)),
  };
}

function isAsleep(agent, now) {
  if (!agent || String(agent.kind || "") === "group") return false;
  const at = now || new Date();
  const { sleepMins, wakeMins } = sleepWindow(agent);
  const mins = at.getHours() * 60 + at.getMinutes();
  if (sleepMins < wakeMins) return mins >= sleepMins && mins < wakeMins;
  return mins >= sleepMins || mins < wakeMins;
}

function agentMood(agent) {
  if (!agent || agent.frozen) return "idle";
  if (agent.busy || agent.status === "running") return "working";
  if (agent.status === "waiting") return "waiting";
  if (String(agent.kind || "") === "group") return "idle";
  if (agent.id && agent.id === state.agentId) return "idle";
  if (isAsleep(agent)) return "sleeping";
  return "idle";
}

function blobMoodEyes(mood, emotion) {
  const ink = "#17181a";
  if (mood === "sleeping") {
    return `<path d="M18 31c2.4-3.2 8.4-3.2 10.8 0" fill="none" stroke="${ink}" stroke-width="2.6" stroke-linecap="round"/>`
      + `<path d="M35 31c2.4-3.2 8.4-3.2 10.8 0" fill="none" stroke="${ink}" stroke-width="2.6" stroke-linecap="round"/>`;
  }
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
  // Keep a fixed viewport across selection changes. The old sleeping viewport
  // made the chat icon visibly shrink when returning to the list.
  return `<svg viewBox="0 0 64 64" aria-hidden="true">${blobBody(spec)}${shine}${blobMoodEyes(mood, spec.emotion)}${blobExtras(mood)}</svg>`;
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
  const spec = blobFor(agent);
  spec.emotion = activeBlobEmotion(agent, resolved);
  node.classList.add("blob", `mood-${resolved}`);
  node.innerHTML = blobSvg(spec, resolved);
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
    return `AIOS_CODE_BLOCK_SENTINEL_${blocks.length - 1}_END`;
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
  const splitRow = (line) => line.trim().replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim());
  const isDivider = (line) => {
    const cells = splitRow(line);
    return cells.length >= 2 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
  };
  const looksRow = (line) => line.trim().includes("|");
  const skipBlank = (from) => {
    let i = from;
    while (i < lines.length && !lines[i].trim()) i += 1;
    return i;
  };
  const alignOf = (cell) => {
    const left = cell.startsWith(":");
    const right = cell.endsWith(":");
    if (left && right) return "center";
    if (right) return "right";
    if (left) return "left";
    return "";
  };

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (line.trim() && looksRow(line)) {
      const dividerAt = skipBlank(i + 1);
      if (dividerAt < lines.length && isDivider(lines[dividerAt])) {
        const header = splitRow(line);
        const aligns = splitRow(lines[dividerAt]).map(alignOf);
        if (header.length >= 2 && aligns.length === header.length) {
          closeList();
          const rows = [];
          let j = skipBlank(dividerAt + 1);
          while (j < lines.length) {
            if (!lines[j].trim()) { j += 1; continue; }
            if (!looksRow(lines[j])) break;
            const row = splitRow(lines[j]);
            while (row.length < header.length) row.push("");
            rows.push(row.slice(0, header.length));
            j += 1;
          }
          const attr = (n) => (aligns[n] ? ` style="text-align:${aligns[n]}"` : "");
          const head = header.map((cell, n) => `<th${attr(n)}>${cell}</th>`).join("");
          const body = rows.map((row) =>
            `<tr>${row.map((cell, n) => `<td${attr(n)}>${cell}</td>`).join("")}</tr>`).join("");
          out.push(`<div class="md-table"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`);
          i = j - 1;
          continue;
        }
      }
    }
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
    .replace(/AIOS_CODE_BLOCK_SENTINEL_(\d+)_END/g,
             (_m, i) => `<pre><code>${blocks[Number(i)]}</code></pre>`);
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
    code_session: "code", code_status: "code", code_configs: "code",
    machines: "machines",
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
  state.screen = screen;
  const pair = screen === "pair";
  $("screen-pair").classList.toggle("hidden", !pair);
  $("workspace").classList.toggle("hidden", pair);
  if (pair) return;
  const wide = isWide();
  const code = screen === "code";
  $("screen-agents").classList.toggle("hidden", !wide && screen !== "agents");
  $("screen-chat").classList.toggle("hidden", code || (!wide && screen !== "chat"));
  $("screen-code")?.classList.toggle("hidden", !code);
  document.body.classList.toggle("code-open", code);
  const empty = $("chat-empty");
  if (empty) empty.classList.toggle("hidden", !!state.agentId || code);
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

function avatarNode(agent, extra = "", mood) {
  const node = el("div", `avatar${extra ? ` ${extra}` : ""}`);
  fillAvatar(node, agent, mood);
  return node;
}

function isGroup(agent) {
  return String(agent?.kind || "") === "group";
}

function agentById(id) {
  return state.agents.find((row) => row.id === id) || null;
}

function groupMemberAgents(agent) {
  return (agent?.members || []).map((id) => agentById(id)).filter(Boolean);
}

function avatarStack(agent, extra = "") {
  if (!isGroup(agent)) return avatarNode(agent, extra);
  const members = groupMemberAgents(agent);
  const stack = el("div", `avatar-stack${extra ? ` ${extra}` : ""}`);
  const shown = members.slice(0, 3);
  stack.dataset.count = String(shown.length || 1);
  if (!shown.length) {
    stack.append(avatarNode(agent, extra, "idle"));
    return stack;
  }
  for (const member of shown) stack.append(avatarNode(member, "tiny", "idle"));
  return stack;
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
      row.append(avatarStack(agent));
      const meta = el("div", "meta");
      const name = el("div", "name");
      paintAgentName(name, agent);
      meta.append(name);
      meta.append(el("div", "preview", agent.preview || ""));
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

function paintAgentName(node, agent) {
  if (!node) return;
  node.textContent = "";
  node.append(el("span", "name-text", agent.name || "Agent"));
  const sub = String(agent.subtitle || "").trim();
  if (sub) node.append(subtitlePill(sub));
  if (agent.busy || agent.status === "waiting") node.append(el("span", "dot busy"));
}

function subtitlePill(text) {
  const pill = el("span", "sub-pill");
  const track = el("span", "sub-pill-track");
  const original = el("span", "sub-pill-copy", text);
  const duplicate = el("span", "sub-pill-copy", text);
  duplicate.setAttribute("aria-hidden", "true");
  track.append(original, duplicate);
  pill.append(track);
  requestAnimationFrame(() => {
    const width = original.getBoundingClientRect().width;
    const overflowing = width > pill.clientWidth;
    pill.classList.toggle("scrolling", overflowing);
    if (overflowing) {
      pill.style.setProperty("--subtitle-roll",
        `${Math.max(6, width / 18).toFixed(2)}s`);
    }
  });
  return pill;
}

function updateAgentRow(row, agent) {
  paintAgentName(row.querySelector(".name"), agent);
  const preview = row.querySelector(".preview");
  if (preview) preview.textContent = agent.preview || "";
  const when = row.querySelector(".when");
  if (when) when.textContent = relativeTime(agent.updated_at);
  const face = row.querySelector(".avatar-stack") || row.querySelector(".avatar");
  if (face) face.replaceWith(avatarStack(agent));
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

function addUser(text, attachments, at, extra = {}) {
  stampIfNeeded(at);
  if (extra.kind === "agent_message") {
    const row = el("div", "row-agent relay-message");
    if (extra.id) row.dataset.msgId = extra.id;
    const sender = agentById(extra.sender_id) || {
      id: extra.sender_id || "agent-relay",
      name: extra.sender_name || "Agent",
    };
    row.append(avatarNode(sender, "tiny", "idle"));
    const col = el("div", "speech");
    col.append(el("div", "speaker-name", `From ${sender.name || "Agent"}`));
    const node = el("div", "bubble-agent relay-bubble");
    node.innerHTML = markdown(text);
    col.append(node);
    row.append(col);
    appendTranscript(row);
    scrollDown(true);
    return node;
  }
  const row = el("div", "row-user");
  if (extra.id) row.dataset.msgId = extra.id;
  const node = el("div", "bubble-user");
  // Keep the selected appearance on the actual bubble as well as in CSS.
  // Mobile WebKit can briefly lose the custom-property paint when the PWA
  // restores or incrementally inserts a message, leaving only its text.
  node.style.backgroundColor = state.appearance?.user_bubble || "#3a5a8c";
  node.style.color = state.appearance?.user_text || "#f2f3f4";
  const thumbs = attachThumbs(attachments);
  if (thumbs) node.append(thumbs);
  if (text) node.append(document.createTextNode(text));
  row.append(node);
  appendTranscript(row);
  scrollDown(true);
  return node;
}

function addAssistant(text, at, extra = {}) {
  if (extra?.kind === "work_done") {
    addWorkDoneCard(extra, text, at);
    return;
  }
  stampIfNeeded(at);
  const row = el("div", "row-agent");
  if (extra?.id) row.dataset.msgId = extra.id;
  const node = el("div", "bubble-agent assistant");
  node.innerHTML = markdown(text);
  const speakerId = extra?.speaker_id || "";
  if (speakerId) {
    const speaker = agentById(speakerId) || {
      id: speakerId,
      name: extra.speaker_name || "Agent",
      emoji: extra.emoji || "",
    };
    row.classList.add("named");
    row.append(avatarNode(speaker, "tiny", "idle"));
    const col = el("div", "speech");
    col.append(el("div", "speaker-name", speaker.name || extra.speaker_name || "Agent"));
    col.append(node);
    row.append(col);
  } else {
    row.append(node);
  }
  appendTranscript(row);
  scrollDown();
  return node;
}

function addThinking(reasoning, at) {
  const text = String(reasoning || "").trim();
  if (!text) return null;
  stampIfNeeded(at);
  const wrap = el("div", "thinking");
  const head = el("button", "thinking-head");
  head.type = "button";
  head.innerHTML = `<span class="thinking-label">Thought</span>${svg('<path d="M6 9l6 6 6-6"/>', "thinking-chevron")}`;
  const body = el("div", "thinking-body", text);
  head.addEventListener("click", () => wrap.classList.toggle("expanded"));
  wrap.append(head, body);
  appendTranscript(wrap);
  return wrap;
}

function addWorkDoneCard(extra, text, at) {
  stampIfNeeded(at);
  const speaker = agentById(extra.speaker_id) || {
    id: extra.speaker_id,
    name: extra.speaker_name || "Agent",
  };
  const card = el("button", "work-done");
  card.type = "button";
  card.append(avatarNode(speaker, "tiny", "idle"));
  const meta = el("div", "work-done-meta");
  meta.append(el("div", "work-done-name", `${speaker.name || "Agent"} finished`));
  meta.append(el("div", "work-done-preview", String(text || "Done.").slice(0, 90)));
  card.append(meta);
  card.addEventListener("click", () => {
    if (speaker.id) openAgent(speaker.id);
  });
  appendTranscript(card);
  scrollDown();
  return card;
}

function lastMessageHost() {
  const node = transcriptEl();
  if (!node) return null;
  const rows = [...node.querySelectorAll(".row-agent.named, .row-user")];
  return rows.length ? rows[rows.length - 1] : null;
}

function messageHost(targetId) {
  const node = transcriptEl();
  if (!node) return null;
  if (targetId) {
    const match = node.querySelector(`[data-msg-id="${targetId}"]`);
    if (match) return match;
  }
  return lastMessageHost();
}

function addReaction(payload, at) {
  const emoji = payload?.emoji || payload?.text || "👍";
  const speakerId = String(payload?.speaker_id || "");
  const speakerName = payload?.speaker_name || "Agent";
  const host = messageHost(payload?.target_id);
  if (!host) {
    stampIfNeeded(at);
    const row = el("div", "row-agent named");
    const bar = el("div", "react-bar");
    row.append(bar);
    appendTranscript(row);
    return paintReactChip(bar, emoji, speakerId, speakerName);
  }
  const bubble = host.querySelector(".bubble-user, .bubble-agent") || host;
  let bar = bubble.querySelector(":scope > .react-bar");
  if (!bar) {
    bar = el("div", "react-bar");
    bubble.append(bar);
  }
  return paintReactChip(bar, emoji, speakerId, speakerName);
}

function paintReactChip(bar, emoji, speakerId, speakerName) {
  const existing = [...bar.querySelectorAll(".react-chip")].find(
    (chip) => chip.dataset.emoji === emoji
  );
  if (existing) {
    const who = new Set((existing.dataset.speakers || "").split(",").filter(Boolean));
    if (speakerId && who.has(speakerId)) return existing;
    if (speakerId) who.add(speakerId);
    existing.dataset.speakers = [...who].join(",");
    const count = existing.querySelector(".react-count");
    if (count) count.textContent = who.size > 1 ? String(who.size) : "";
    existing.title = [existing.title, speakerName].filter(Boolean).join(", ");
    return existing;
  }
  const chip = el("span", "react-chip");
  chip.dataset.emoji = emoji;
  chip.dataset.speakers = speakerId;
  chip.title = speakerName;
  chip.append(el("span", "react-emoji", emoji));
  chip.append(el("span", "react-count"));
  bar.append(chip);
  scrollDown();
  return chip;
}

function paintGroupWorking() {
  const node = transcriptEl();
  if (!node) return;
  let cluster = node.querySelector("#group-working");
  if (!state.groupWorking.size) {
    cluster?.remove();
    return;
  }
  if (!cluster) {
    cluster = el("button", "work-cluster");
    cluster.id = "group-working";
    cluster.type = "button";
    cluster.addEventListener("click", workingSheet);
    appendTranscript(cluster);
  }
  cluster.textContent = "";
  for (const [id, row] of state.groupWorking) {
    const chip = el("span", "work-chip");
    const agent = agentById(id) || { id, name: row.name || "Agent" };
    chip.append(avatarNode(agent, "tiny", "idle"));
    chip.append(el("span", "work-chip-label", `${agent.name || row.name || "Agent"} working`));
    cluster.append(chip);
  }
  const spacer = $("transcript-spacer");
  if (spacer && spacer.parentNode === node) node.insertBefore(cluster, spacer);
  scrollDown();
}

function workingSheet() {
  const { body, dismiss } = openSheet("Working now");
  const list = el("div", "group");
  if (!state.groupWorking.size) {
    list.append(el("div", "hint", "Nobody is working right now."));
    body.append(list);
    return;
  }
  for (const [id, row] of state.groupWorking) {
    const agent = agentById(id) || { id, name: row.name || "Agent" };
    const btn = el("button", "btn member-pick on");
    btn.type = "button";
    btn.append(avatarNode(agent, "tiny", "idle"));
    const label = el("span");
    label.append(document.createTextNode(agent.name || row.name || "Agent"));
    btn.append(label);
    if (row.task) btn.append(el("span", "sub", row.task));
    btn.addEventListener("click", () => {
      dismiss();
      if (agent.id) openAgent(agent.id);
    });
    list.append(btn);
  }
  list.append(el("div", "hint", "Opens that agent's private chat — the work lives there, not in this group."));
  body.append(list);
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
  if (state.thinking) return state.thinking;
  const wrap = el("div", "thinking live");
  const head = el("button", "thinking-head");
  head.type = "button";
  head.innerHTML = `${loadingPixels()}<span class="thinking-label">Thinking</span>${svg('<path d="M6 9l6 6 6-6"/>', "thinking-chevron")}`;
  const body = el("div", "thinking-body");
  head.addEventListener("click", () => wrap.classList.toggle("expanded"));
  wrap.append(head, body);
  settleWorking();
  appendTranscript(wrap);
  state.thinking = wrap;
  state.reasoningRendered = true;
  scrollDown();
  return wrap;
}

function settleThinking() {
  if (!state.thinking) return;
  const body = state.thinking.querySelector(".thinking-body");
  if (!String(body?.textContent || "").trim()) {
    state.thinking.remove();
    state.thinking = null;
    return;
  }
  state.thinking.classList.remove("live");
  state.thinking.querySelector(".loading-pixels")?.remove();
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
  chip.addEventListener("click", (event) => {
    if (wrap.dataset.operatorJobId) {
      event.preventDefault();
      wrap.classList.toggle("expanded");
      if (wrap.classList.contains("expanded")) loadOperatorJob(wrap.dataset.operatorJobId);
      return;
    }
    if (card?.agent_id) {
      event.preventDefault();
      openAgent(card.agent_id);
      return;
    }
    if (wrap.dataset.jobId) {
      event.preventDefault();
      openCodeSession(wrap.dataset.jobId, wrap.dataset.sessionId || "",
                      wrap.dataset.jobTitle || detail);
      return;
    }
    if (!body.textContent) return;
    wrap.classList.toggle("expanded");
  });
  wrap.append(chip, body);
  if (card?.tone) wrap.classList.add(card.tone);
  if (card?.agent_id) wrap.classList.add("agent-link");
  if (card?.job_id && (card?.job_kind === "operator" || name === "operator")) {
    configureOperatorJob(wrap, card.job_id);
  } else if (card?.job_id) {
    wrap.classList.add("code-job");
    wrap.dataset.jobId = card.job_id;
    if (card.session_id) wrap.dataset.sessionId = card.session_id;
    wrap.dataset.jobTitle = card.preview || detail;
  }
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
  if (name === "start_work" || name === "react") return;
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
  if (card?.job_id && (card?.job_kind === "operator" || name === "operator")) {
    configureOperatorJob(wrap, card.job_id);
  } else if (card?.job_id) {
    wrap.classList.add("code-job");
    wrap.dataset.jobId = card.job_id;
    if (card.session_id) wrap.dataset.sessionId = card.session_id;
    wrap.dataset.jobTitle = card.preview || "";
  }
  if (card?.takeover) {
    const open = el("button", "btn ghost", "Open the screen");
    open.addEventListener("click", () => openTakeover(card.takeover));
    wrap.append(open);
  }
  scrollDown();
}

function configureOperatorJob(wrap, jobId) {
  const id = String(jobId || "");
  if (!id) return;
  wrap.classList.add("operator-job");
  wrap.dataset.operatorJobId = id;
  const body = wrap.querySelector(".tool-body");
  body.classList.add("operator-timeline");
  if (!body.childNodes.length) body.append(el("div", "operator-empty", "Tap to view this run"));
  const existing = state.operatorJobs.get(id);
  state.operatorJobs.set(id, existing
    ? { ...existing, wrap, body }
    : { wrap, body, seen: new Set(), loaded: false, loading: false });
}

function operatorEventNode(event) {
  const payload = event.payload || {};
  const step = Number(payload.step || payload.steps || 0);
  const label = step ? `Step ${step}` : "Operator";
  if (event.kind === "operator.screenshot" && payload.image) {
    const node = el("figure", "operator-frame");
    const image = document.createElement("img");
    image.src = payload.image;
    image.alt = `${label} screenshot`;
    image.loading = "lazy";
    node.append(image, el("figcaption", null,
      `${label} · ${payload.width || "?"}×${payload.height || "?"}`));
    return node;
  }
  const node = el("div", `operator-event ${event.kind.replaceAll(".", "-")}`);
  let title = label;
  let text = "";
  if (event.kind === "operator.started") {
    title = "Task";
    text = payload.task || "Operator started";
  } else if (event.kind === "operator.step") {
    title = `${label} · Thinking`;
    text = payload.thought || payload.message || payload.native_tool || "Choosing the next action";
  } else if (event.kind === "operator.actions") {
    title = `${label} · Action`;
    text = (payload.performed || []).join("\n") || "Action completed";
  } else if (event.kind === "operator.progress_review") {
    title = `${label} · Progress review`;
    text = payload.progress
      ? (payload.summary || "Progress confirmed")
      : (payload.issue || "No progress detected");
  } else if (event.kind === "operator.done") {
    title = "Finished";
    text = payload.summary || "Done";
  } else if (event.kind === "operator.stuck") {
    title = "Stopped · no progress";
    text = payload.issue || "The run was stuck";
  } else if (event.kind === "operator.failed") {
    title = "Failed";
    text = payload.error || "Operator failed";
  } else if (event.kind === "operator.stopped") {
    title = "Stopped";
    text = payload.reason || "Stopped";
  } else {
    return null;
  }
  node.append(el("div", "operator-event-title", title));
  node.append(el("div", "operator-event-text", text));
  return node;
}

function recordOperatorEvent(event) {
  const jobId = String(event.payload?.job_id || "");
  const view = state.operatorJobs.get(jobId);
  if (!jobId || !view || view.seen.has(event.id)) return;
  view.seen.add(event.id);
  view.body.querySelector(".operator-empty")?.remove();
  const node = operatorEventNode(event);
  if (node) view.body.append(node);
  const meta = view.wrap.querySelector(".tool-chip .meta");
  if (event.kind === "operator.done") meta.textContent = "done";
  else if (event.kind === "operator.failed") meta.textContent = "failed";
  else if (event.kind === "operator.stuck") meta.textContent = "stopped";
  else if (event.kind === "operator.step") meta.textContent = `step ${event.payload?.step || ""}`.trim();
}

async function loadOperatorJob(jobId) {
  const view = state.operatorJobs.get(String(jobId || ""));
  if (!view || view.loaded || view.loading) return;
  view.loading = true;
  const empty = view.body.querySelector(".operator-empty");
  if (empty) empty.textContent = "Loading run…";
  try {
    const data = await api(`/api/jobs/${encodeURIComponent(jobId)}/events?since=0`);
    for (const event of data.events || []) recordOperatorEvent(event);
    view.loaded = true;
    if (!(data.events || []).length) {
      view.body.querySelector(".operator-empty")?.remove();
      view.body.append(el("div", "operator-empty", "No Operator events were recorded."));
    }
  } catch (error) {
    if (empty) empty.textContent = String(error.message || error);
  } finally {
    view.loading = false;
  }
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

function shotCard(payload, persistent = false) {
  if (!payload || !payload.image) return;
  let row = $("live-shot");
  if (persistent && row) {
    row.removeAttribute("id");
    row.querySelector(".shot")?.classList.remove("live");
  }
  if (!row) {
    row = el("div", "row-agent");
    if (!persistent) row.id = "live-shot";
    const wrap = el("div", `shot${persistent ? "" : " live"}`);
    const img = document.createElement("img");
    img.alt = payload.caption || "Operator screen";
    wrap.append(img);
    wrap.append(el("div", "cap", payload.caption || "Operator screen"));
    img.addEventListener("click", () => openTakeover("/vnc/view"));
    row.append(wrap);
    appendTranscript(row);
  }
  row.querySelector("img").src = payload.image;
  const caption = row.querySelector(".cap");
  if (caption && payload.caption) caption.textContent = payload.caption;
  const preview = $("context-screen-img");
  if (preview) {
    preview.src = payload.image;
    $("context-screen")?.classList.add("has-image");
  }
  scrollDown();
}

function taskRow(jobId, label, stateText, sessionId = "") {
  let wrap = state.jobs.get(jobId);
  const meta = state.jobMeta.get(jobId) || {};
  if (sessionId) meta.session_id = sessionId;
  if (label) meta.title = label;
  state.jobMeta.set(jobId, meta);
  if (!wrap) {
    wrap = el("button", "task-row code-job");
    wrap.type = "button";
    wrap.dataset.jobId = jobId;
    if (meta.session_id) wrap.dataset.sessionId = meta.session_id;
    const pixels = el("div", "task-pixels");
    pixels.innerHTML = loadingPixels();
    wrap.append(pixels);
    wrap.append(el("div", "label", label));
    wrap.append(el("div", "state", stateText || "running"));
    wrap.append(el("div", "open-hint", "Open"));
    wrap.addEventListener("click", () => {
      openCodeSession(jobId, wrap.dataset.sessionId || meta.session_id || "", label);
    });
    state.jobs.set(jobId, wrap);
    appendTranscript(wrap);
    scrollDown();
  } else {
    wrap.querySelector(".label").textContent = label;
    wrap.querySelector(".state").textContent = stateText || "";
    if (meta.session_id) wrap.dataset.sessionId = meta.session_id;
  }
  return wrap;
}

/* ---------------- rendering stored history ---------------- */

function ensureHistoryToggle() {
  let button = $("btn-history-toggle");
  if (!button) {
    button = el("button", "history-toggle hidden");
    button.id = "btn-history-toggle";
    button.type = "button";
    button.title = "Previous messages";
    button.setAttribute("aria-label", "Show previous messages");
    button.setAttribute("aria-expanded", "false");
    button.innerHTML = `<span>Older chats</span>${svg(
      '<path d="M7 9l5 5 5-5"/>', "history-chevron")}`;
  }
  if (!button.dataset.bound) {
    button.addEventListener("click", togglePreviousMessages);
    button.dataset.bound = "true";
  }
  return button;
}

function renderMessages(messages, thread = state.currentThread) {
  state.currentThread = thread || null;
  state.currentMessages = Array.isArray(messages) ? messages : [];
  const through = Number(thread?.compacted_through || 0);
  const derivedHidden = through
    ? state.currentMessages.filter((message) => Number(message.sequence || 0) <= through).length
    : 0;
  const hidden = Math.max(Number(thread?.hidden_count || 0), derivedHidden);
  const historyButton = ensureHistoryToggle();
  historyButton?.classList.toggle("hidden", hidden === 0);
  historyButton?.setAttribute("aria-expanded", state.historyExpanded ? "true" : "false");
  historyButton?.setAttribute("aria-label", state.historyExpanded
    ? "Hide previous messages" : `Show ${hidden} previous messages`);
  const shown = hidden && !state.historyExpanded
    ? state.currentMessages.filter((message) => Number(message.sequence || 0) > through)
    : state.currentMessages;
  const node = transcriptEl();
  node.textContent = "";
  if (historyButton) node.append(historyButton);
  const spacer = el("div", "transcript-spacer");
  spacer.id = "transcript-spacer";
  node.append(spacer);
  state.tools.clear();
  state.jobs.clear();
  state.jobMeta.clear();
  state.operatorJobs.clear();
  state.streaming = null;
  state.thinking = null;
  state.reasoningRendered = false;
  state.working = null;
  state.lastStampAt = 0;
  state.groupWorking = new Map();
  for (const row of thread?.working || []) {
    if (row.agent_id) state.groupWorking.set(row.agent_id, row);
  }

  const pendingTools = new Map();
  for (const message of shown) {
    if (message.role === "user") addUser(message.content, message.meta?.attachments, message.created_at, { ...(message.meta || {}), id: message.id });
    else if (message.role === "assistant") {
      addThinking(message.meta?.reasoning, message.created_at);
      addAssistant(message.content, message.created_at, { ...(message.meta || {}), id: message.id });
    }
    else if (message.role === "reaction") addReaction({
      emoji: message.content || message.meta?.emoji,
      speaker_id: message.meta?.speaker_id,
      speaker_name: message.meta?.speaker_name,
      target_id: message.meta?.target_id,
    }, message.created_at);
    else if (message.role === "system") addStatus(message.content.split("\n")[0]);
    else if (message.role === "tool_call") {
      if (message.meta?.name === "start_work" || message.meta?.name === "react") continue;
      let args = {};
      try { args = JSON.parse(message.meta?.arguments || "{}"); } catch {}
      pendingTools.set(message.meta?.call_id, { name: message.meta?.name, args });
    } else if (message.role === "tool_result") {
      if (message.meta?.name === "start_work" || message.meta?.name === "react") continue;
      const pending = pendingTools.get(message.meta?.call_id) || {};
      const output = message.meta?.output || "";
      const card = message.meta?.card || {
        title: pending.name || message.meta?.name || "tool",
        preview: summariseArgs(pending.args),
        meta: output.split("\n")[0].slice(0, 40),
        body: output,
      };
      toolCard({
        callId: null,
        name: pending.name || message.meta?.name || "tool",
        args: pending.args,
        card,
      });
      if (message.meta?.image) {
        shotCard({ image: message.meta.image, caption: card.title || "Image" }, true);
      }
    }
  }
  paintGroupWorking();
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
      if (payload.id) {
        const rows = [...(transcriptEl()?.querySelectorAll(".row-user") || [])];
        const last = rows[rows.length - 1];
        if (last && !last.dataset.msgId) last.dataset.msgId = payload.id;
      }
      if (!document.querySelector(`[data-user-pending="${payload.id}"]`)) {
        if (!state.lastSentText || state.lastSentText !== payload.text) {
          addUser(payload.text, payload.attachments, event.created_at,
                  { ...payload, id: payload.id });
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

    case "message.assistant": {
      const hadLiveThinking = state.reasoningRendered;
      settleThinking();
      if (!hadLiveThinking && payload.reasoning) addThinking(payload.reasoning, event.created_at);
      state.reasoningRendered = false;
      if (state.streaming) {
        state.streaming.classList.remove("streaming");
        state.streaming.innerHTML = markdown(payload.text || state.streaming.dataset.raw || "");
        state.streaming = null;
      } else {
        addAssistant(payload.text || "", event.created_at, payload);
      }
      scrollDown();
      break;
    }

    case "message.reaction":
      addReaction(payload, event.created_at);
      break;

    case "tool.start":
      if (payload.name === "start_work" || payload.name === "react") break;
      settleThinking();
      toolCard({ callId: payload.call_id, name: payload.name, args: payload.arguments, running: true });
      break;

    case "tool.done":
      if (payload.name === "start_work" || payload.name === "react") break;
      finishTool(payload.call_id, payload.name, payload.card);
      if (payload.image) {
        shotCard({ image: payload.image, caption: payload.card?.title || "Image" }, true);
      }
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
      if (payload.job_id) recordOperatorEvent(event);
      else shotCard(payload);
      break;

    case "operator.stuck":
      recordOperatorEvent(event);
      addStatus(`Operator stopped: ${payload.issue || "no meaningful progress"}`, true);
      break;

    case "operator.started":
    case "operator.step":
    case "operator.actions":
    case "operator.progress_review":
    case "operator.done":
    case "operator.failed":
    case "operator.stopped":
      recordOperatorEvent(event);
      break;

    case "operator.takeover":
      openTakeover(payload.path);
      break;

    case "code.started":
      taskRow(payload.job_id,
              `CODE on ${payload.machine}: ${payload.task || ""}`,
              "running",
              payload.session_id || "");
      break;

    case "code.progress":
      taskRow(payload.job_id, payload.title || "CODE session",
              payload.status || "running", payload.session_id || "");
      break;

    case "code.events":
      ingestCodeEvents(payload);
      break;

    case "job.finished": {
      const wrap = state.jobs.get(payload.id);
      if (wrap) {
        wrap.classList.add(payload.status === "done" ? "done" : "failed");
        wrap.querySelector(".state").textContent = payload.status || "";
      }
      if (payload.session_id) {
        const meta = state.jobMeta.get(payload.id) || {};
        meta.session_id = payload.session_id;
        state.jobMeta.set(payload.id, meta);
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
      addStatus("Sent — they'll pick this up without stopping.");
      break;

    case "group.working":
      if (payload.agent_id) {
        state.groupWorking.set(payload.agent_id, payload);
        paintGroupWorking();
      }
      break;

    case "group.idle":
      if (payload.agent_id) state.groupWorking.delete(payload.agent_id);
      paintGroupWorking();
      break;

    case "group.considering":
    case "group.quiet":
    case "group.round_done":
      break;

    case "thread.compacted":
      refreshOpenThread();
      break;

    case "routine.fired":
      addStatus(`Routine: ${payload.name}`);
      break;

    case "routine.created":
      addStatus(`Scheduled "${payload.name}" — ${payload.schedule}`);
      refreshAgentsSoon();
      break;

    case "machine.online":
      markMachine(payload, true);
      break;

    case "machine.offline":
      markMachine(payload, false);
      break;

    case "mouse.status":
      updatePhoneMouseStatus(payload);
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
  const agent = state.agents.find((row) => row.id === state.agentId);
  const group = isGroup(agent);
  $("chat-sub").textContent = busy
    ? (group ? (state.groupWorking.size ? "working…" : "listening…") : "working...")
    : ($("chat-sub").dataset.idle || "");
  if (agent) {
    agent.busy = busy;
    if (busy && agent.status !== "waiting") agent.status = "running";
    if (!busy) agent.status = "idle";
    paintChatHeader(agent);
    paintAgentRow(agent);
  }
  if (busy && !group) ensureWorking();
  else if (!busy) settleWorking();
}

function paintAgentRow(agent) {
  const row = $("agent-list")?.querySelector(`[data-agent-id="${agent.id}"]`);
  if (!row) return;
  paintAgentName(row.querySelector(".name"), agent);
  const face = row.querySelector(".avatar-stack") || row.querySelector(".avatar");
  if (face) face.replaceWith(avatarStack(agent));
}

function rotateActiveBlobEyes() {
  if (document.hidden) return;
  const active = state.agents.filter((agent) => {
    const mood = agentMood(agent);
    return mood === "working" || mood === "waiting";
  });
  for (const agent of active) paintAgentRow(agent);
  const current = active.find((agent) => agent.id === state.agentId);
  if (current && state.screen === "chat") paintChatHeader(current);
}

/* ---------------- actions ---------------- */

async function loadAgents() {
  const data = await api("/api/state");
  state.agents = data.agents || [];
  state.machines = data.machines || [];
  state.wake = data.wake || null;
  state.pinnedAgentId = data.phone?.pinned_agent_id || state.pinnedAgentId || "agt_director";
  renderAgents();
  paintWakeButton();
  paintHomeVoice();
  setConnection(state.socket?.readyState === WebSocket.OPEN);
  prefetchThreads();
  return data;
}

function paintWakeButton() {
  const btn = $("btn-wake-pc");
  const screen = $("screen-agents");
  if (!btn) return;
  const wake = state.wake || {};
  const windows = (state.machines || []).find((item) =>
    /windows/i.test(`${item.platform || ""} ${item.name || ""}`));
  // A disconnected bridge is not proof that the PC is off. Older Director
  // servers only returned online=false, which made a live PC show "Wake PC".
  // Wake is offered only when a current server explicitly confirms "off".
  const connected = wake.connected === true || windows?.online === true;
  const isOn = connected || wake.power_state === "on" || wake.reachable === true;
  // Power-off needs the outbound Windows bridge. Reachability alone proves
  // the PC is on but cannot safely run shutdown.exe.
  const canPowerOff = connected && wake.can_power_off !== false;
  const canWake = !!wake.available && wake.power_state === "off";
  const show = canWake || isOn;
  btn.classList.toggle("hidden", !show);
  btn.classList.toggle("is-on", isOn);
  btn.classList.toggle("is-unavailable", isOn && !canPowerOff);
  // Keep status visible when an older/unpaired desktop bridge is the problem,
  // but do not let that stale state leave the control permanently disabled.
  btn.disabled = false;
  screen?.classList.toggle("pc-asleep", canWake);
  if (isOn && !canPowerOff) {
    const message = "Windows PC is on · tap to reconnect power control";
    const label = btn.querySelector("span");
    if (label) label.textContent = message;
    btn.title = message;
    btn.setAttribute("aria-label", message);
  } else {
    const label = btn.querySelector("span");
    const action = canPowerOff ? "Turn Windows PC off" : "Wake Windows PC";
    if (label) label.textContent = action;
    btn.title = action;
    btn.setAttribute("aria-label", action);
  }
}

function pinnedAgent() {
  return state.agents.find((agent) => agent.id === state.pinnedAgentId)
    || state.agents.find((agent) => agent.id === "agt_director")
    || state.agents.find((agent) => !isGroup(agent));
}

function paintHomeVoice(text = "") {
  const button = $("btn-home-voice");
  if (!button) return;
  const agent = pinnedAgent();
  const fallback = agent ? `Tap to talk to ${agent.name}` : "Choose a pinned agent in Settings";
  button.disabled = !agent;
  button.title = text || fallback;
  button.setAttribute("aria-label", text || fallback);
}

function markMachine(payload, online) {
  const machines = state.machines || [];
  let row = machines.find((item) => item.id === payload?.id);
  if (row) row.online = online;
  else if (payload?.id) {
    machines.push({ id: payload.id, name: payload.name || "PC", online });
  }
  state.machines = machines;
  if (!state.wake) state.wake = { available: true, online: false, name: "PC" };
  const windows = machines.find((item) =>
    /windows/i.test(`${item.platform || ""} ${item.name || ""}`)) || machines[0];
  if (online) {
    state.wake.online = true;
    state.wake.connected = true;
    state.wake.can_power_off = true;
    state.wake.power_state = "on";
  } else {
    state.wake.connected = false;
    state.wake.can_power_off = false;
    state.wake.power_state = "unknown";
    setTimeout(() => refreshPowerStatus(), 250);
  }
  if (windows?.name) state.wake.name = windows.name;
  paintWakeButton();
  refreshPhoneMouseMachines();
}

async function refreshPowerStatus() {
  try {
    const data = await api("/api/machines");
    state.machines = data.machines || [];
    state.wake = data.wake || state.wake;
    paintWakeButton();
  } catch { /* the websocket/reconnect path will retry */ }
}

async function togglePcPower() {
  const btn = $("btn-wake-pc");
  if (!btn || btn.disabled) return;
  const knownOn = state.wake?.power_state === "on" || state.wake?.reachable === true;
  let turningOff = !!state.wake?.online && !!state.wake?.can_power_off;
  if (knownOn && !turningOff) {
    await refreshPowerStatus();
    if (!(state.wake?.online && state.wake?.can_power_off)) {
      alert("The PC is on, but its Director bridge is not connected yet. "
            + "aiOS will keep restarting that bridge automatically; try again in a moment.");
      return;
    }
    turningOff = true;
  }
  if (turningOff && !window.confirm(`Turn off ${state.wake?.name || "the PC"}?`)) return;
  const label = btn.querySelector("span");
  btn.disabled = true;
  if (label) label.textContent = turningOff ? "Turning off…" : "Waking…";
  try {
    await api(turningOff ? "/api/power/off" : "/api/wake", { method: "POST" });
  } catch (error) {
    const message = String(error.message || error).slice(0, 120);
    if (label) label.textContent = message;
    btn.title = message;
    alert(message);
    btn.disabled = false;
    return;
  }
  const start = Date.now();
  while (Date.now() - start < (turningOff ? 45000 : 90000)) {
    await new Promise((resolve) => setTimeout(resolve, 2000));
    try {
      const data = await api("/api/state");
      state.machines = data.machines || [];
      state.wake = data.wake || state.wake;
      if (turningOff ? !data.wake?.online : data.wake?.online) break;
    } catch { /* keep waiting */ }
  }
  btn.disabled = false;
  paintWakeButton();
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
  const title = $("chat-name");
  title.textContent = "";
  title.append(el("span", "name-text", agent.name || "Director"));
  const subLabel = String(agent.subtitle || "").trim();
  if (subLabel) title.append(subtitlePill(subLabel));
  $("composer-input").placeholder = isGroup(agent)
    ? `Message ${agent.name || "the group"}…`
    : `Message ${agent.name || "Director"}…`;
  const sub = $("chat-sub");
  const bits = [];
  if (isGroup(agent)) {
    const n = (agent.members || []).length;
    bits.push(n ? `${n} agents` : "group");
  }
  if (agent.routines) bits.push(`${agent.routines} scheduled`);
  if (agent.auto_approve) bits.push("auto-approved");
  const line = bits.filter(Boolean).join(" · ");
  sub.dataset.idle = line;
  if (!state.busy) sub.textContent = line;
  const slot = $("chat-avatar");
  if (slot) {
    const next = avatarStack(agent, "small");
    next.id = "chat-avatar";
    slot.replaceWith(next);
  }
  const label = $("context-screen-label");
  if (label) label.textContent = `${agent.name || "Operator"}'s screen`;
}

async function openAgent(agentId, { pushHistory = true } = {}) {
  const gen = ++state.openGen;
  if (state.threadId) markWatching(state.threadId, false);
  state.agentId = agentId;
  state.historyExpanded = false;
  save();
  const agent = state.agents.find((row) => row.id === agentId) || {};
  paintChatHeader(agent);
  markActiveAgent(agentId);
  show("chat");
  if (pushHistory && history.state?.directorScreen !== "chat") {
    history.pushState({ directorScreen: "chat", agentId }, "");
  } else if (pushHistory && history.state?.agentId !== agentId) {
    history.replaceState({ directorScreen: "chat", agentId }, "");
  }
  refreshContext();

  const cached = state.threads.get(agentId);
  if (cached) {
    state.threadId = cached.thread.id;
    state.cursor = Math.max(state.cursor, cached.cursor || 0);
    cached.thread.working = cached.working || cached.thread.working || [];
    renderMessages(cached.messages || [], cached.thread);
    setBusy(cached.thread.status === "running" || cached.thread.status === "waiting"
      || (cached.thread.working || []).length > 0);
    markWatching(state.threadId, true);
  } else {
    transcriptEl().textContent = "";
    appendTranscript(churningRow("Loading"));
  }

  try {
    const data = await api(`/api/agents/${agentId}/thread`);
    if (gen !== state.openGen) return;
    data.thread.working = data.working || [];
    state.threads.set(agentId, {
      thread: data.thread,
      messages: data.messages || [],
      working: data.working || [],
      cursor: data.cursor || 0,
      at: Date.now(),
    });
    state.threadId = data.thread.id;
    state.cursor = Math.max(state.cursor, data.cursor || 0);
    renderMessages(data.messages || [], data.thread);
    setBusy(data.thread.status === "running" || data.thread.status === "waiting"
      || (data.working || []).length > 0);
    markWatching(state.threadId, true);
    if (!(data.messages || []).length) {
      appendTranscript(el("div", "empty", `Say something to ${agent.name || "Director"}.`));
    }
  } catch (error) {
    if (gen !== state.openGen) return;
    if (!cached) addStatus(String(error.message || error), true);
  }
}

async function refreshOpenThread() {
  const threadId = state.threadId;
  const agentId = state.agentId;
  if (!threadId || !agentId) return;
  try {
    const data = await api(`/api/threads/${threadId}`);
    if (state.threadId !== threadId) return;
    data.thread.working = data.working || [];
    state.threads.set(agentId, {
      thread: data.thread,
      messages: data.messages || [],
      working: data.working || [],
      cursor: data.cursor || state.cursor,
      at: Date.now(),
    });
    renderMessages(data.messages || [], data.thread);
  } catch {
    /* The next normal refresh will retry. */
  }
}

function leaveChat() {
  if (state.threadId) markWatching(state.threadId, false);
  state.openGen += 1;
  state.agentId = "";
  state.threadId = "";
  state.historyExpanded = false;
  state.currentThread = null;
  state.currentMessages = [];
  save();
  markActiveAgent("");
  $("btn-history-toggle")?.classList.add("hidden");
  show("agents");
}

function navigateBack() {
  if (state.screen === "code") {
    if (history.state?.directorScreen === "code") history.back();
    else closeCodeSession();
    return;
  }
  if (state.screen === "chat") {
    if (history.state?.directorScreen === "chat") history.back();
    else leaveChat();
  }
}

async function togglePreviousMessages() {
  state.historyExpanded = !state.historyExpanded;
  const button = $("btn-history-toggle");
  button?.setAttribute("aria-expanded", state.historyExpanded ? "true" : "false");
  await refreshOpenThread();
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
  const stop = $("btn-stop");
  if (stop) stop.disabled = true;
  try {
    const result = await api(`/api/threads/${state.threadId}/stop`, { method: "POST" });
    if (!result.hard_cancel) {
      addStatus("Stop requested — Director needs the latest backend to force it.", true);
      return;
    }
    state.groupWorking.clear();
    paintGroupWorking();
    settleThinking();
    state.streaming = null;
    setBusy(false);
    addStatus("Stopped.");
  } catch (error) {
    addStatus(String(error.message || error), true);
  } finally {
    if (stop) stop.disabled = false;
  }
}

/* ---------------- voice ---------------- */

async function transcribeAudio(blob) {
  const form = new FormData();
  form.append("audio", blob, "clip.webm");
  const response = await fetch(apiUrl("/api/voice/transcribe"), {
    method: "POST",
    headers: { Authorization: `Bearer ${state.token}` },
    body: form,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || !data.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return String(data.text || "").trim();
}

async function beginRecording(button, onText, onError) {
  if (state.recorder?.state === "recording") return false;
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (error) {
    onError?.("Microphone permission was refused.");
    return false;
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
    if (blob.size < 1200) {
      onError?.("Recording was too short.");
      return;
    }
    try {
      button.classList.add("transcribing");
      const text = await transcribeAudio(blob);
      if (text) await onText(text);
    } catch (error) {
      onError?.(String(error.message || error));
    } finally {
      button.classList.remove("transcribing");
    }
  });
  recorder.start();
  button.classList.add("recording");
  return true;
}

function stopRecording() {
  if (state.recorder?.state === "recording") state.recorder.stop();
}

async function toggleMic() {
  const button = $("btn-mic");
  if (state.recorder?.state === "recording") {
    stopRecording();
    return;
  }
  await beginRecording(button, async (text) => {
    const input = $("composer-input");
    input.value = (input.value ? `${input.value} ` : "") + text;
    autosize(input);
    await send();
  }, (message) => addStatus(message, true));
}

async function toggleHomeVoice(event) {
  event.preventDefault();
  if (state.homeRecording) {
    state.homeRecording = false;
    event.currentTarget.setAttribute("aria-pressed", "false");
    paintHomeVoice("Transcribing…");
    stopRecording();
    return;
  }
  if (state.recorder?.state === "recording") return;
  const agent = pinnedAgent();
  if (!agent) {
    paintHomeVoice("Choose a pinned agent in Settings");
    return;
  }
  const button = $("btn-home-voice");
  state.homeRecording = await beginRecording(button, async (text) => {
    paintHomeVoice(`Sending to ${agent.name}…`);
    const threadData = await api(`/api/agents/${agent.id}/thread`);
    await api(`/api/threads/${threadData.thread.id}/messages`, {
      method: "POST", body: JSON.stringify({ text, attachments: [] }),
    });
    paintHomeVoice(`Sent to ${agent.name}`);
    setTimeout(() => paintHomeVoice(), 1800);
  }, (message) => {
    paintHomeVoice(message);
    setTimeout(() => paintHomeVoice(), 2600);
  });
  if (state.homeRecording) {
    button.setAttribute("aria-pressed", "true");
    paintHomeVoice(`Listening — tap again to send to ${agent.name}`);
  }
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
  wrap.querySelector(".takeover-keyboard-input")?.blur();
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
  const keyboard = el("button", "icon-btn keyboard-btn");
  keyboard.type = "button";
  keyboard.title = "Open keyboard";
  keyboard.setAttribute("aria-label", "Open keyboard");
  keyboard.innerHTML = svg(
    '<rect x="2.5" y="5" width="19" height="14" rx="2"/>'
    + '<path d="M6 9h.01M9 9h.01M12 9h.01M15 9h.01M18 9h.01M6 12h.01M9 12h.01M12 12h.01M15 12h.01M18 12h.01M7 15h10"/>');
  const keyboardInput = el("textarea", "takeover-keyboard-input");
  keyboardInput.rows = 1;
  keyboardInput.inputMode = "text";
  keyboardInput.setAttribute("enterkeyhint", "enter");
  keyboardInput.setAttribute("autocomplete", "off");
  keyboardInput.setAttribute("autocorrect", "on");
  keyboardInput.setAttribute("autocapitalize", "sentences");
  keyboardInput.setAttribute("aria-label", "Type on operator screen");
  const postKeyboard = (payload) => {
    const frame = wrap.querySelector("iframe");
    frame?.contentWindow?.postMessage(payload, "*");
  };
  keyboard.addEventListener("click", () => {
    // iOS only opens its software keyboard when focus happens synchronously
    // inside the user's tap. Focusing an iframe input later via postMessage
    // loses that activation, so this parent-owned input captures the typing.
    keyboardInput.focus();
    keyboard.classList.add("active");
  });
  keyboardInput.addEventListener("blur", () => keyboard.classList.remove("active"));
  keyboardInput.addEventListener("input", (event) => {
    postKeyboard({
      type: "director.keyboard-input",
      inputType: event.inputType || "",
      data: event.data == null ? keyboardInput.value : event.data,
    });
    keyboardInput.value = "";
  });
  keyboardInput.addEventListener("keydown", (event) => {
    const special = { Backspace: 0xff08, Delete: 0xffff, Enter: 0xff0d,
                      Tab: 0xff09, Escape: 0xff1b };
    if (!special[event.key]) return;
    event.preventDefault();
    postKeyboard({ type: "director.keyboard-key", keysym: special[event.key] });
  });
  const close = el("button", "icon-btn");
  close.innerHTML = svg('<path d="M6 6l12 12M18 6L6 18"/>');
  close.addEventListener("click", closeTakeover);
  bar.append(keyboardInput, keyboard, close);
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
    const [settings, modelInfo, health, balance] = await Promise.all([
      api("/api/settings"), api("/api/models"), api("/api/state"),
      api("/api/openrouter/balance").catch((error) => ({
        ok: false, error: String(error.message || error),
      })),
    ]);
    data = { settings: settings.settings, models: modelInfo, health, balance };
  } catch (error) {
    body.textContent = "";
    body.append(el("div", "notice", String(error.message || error)));
    return;
  }
  body.textContent = "";

  const pinned = el("div", "group");
  pinned.append(el("h3", null, "Homepage voice"));
  pinned.append(el("div", "hint",
    "Hold the microphone on the home page, speak, then release. The recording is "
    + "transcribed and sent straight to this private agent chat."));
  const pinnedField = el("div", "field");
  pinnedField.append(el("label", null, "Pinned agent"));
  const pinnedSelect = el("select");
  for (const agent of state.agents.filter((row) => !isGroup(row))) {
    const option = document.createElement("option");
    option.value = agent.id;
    option.textContent = agent.name;
    pinnedSelect.append(option);
  }
  pinnedSelect.value = data.settings.phone?.pinned_agent_id
    || state.pinnedAgentId || "agt_director";
  pinnedField.append(pinnedSelect);
  pinned.append(pinnedField);
  const savePinned = el("button", "btn primary", "Save pinned agent");
  savePinned.addEventListener("click", async () => {
    await api("/api/settings", {
      method: "PATCH",
      body: JSON.stringify({ phone: { pinned_agent_id: pinnedSelect.value } }),
    });
    state.pinnedAgentId = pinnedSelect.value;
    save();
    paintHomeVoice();
    savePinned.textContent = "Saved";
  });
  pinned.append(savePinned);
  body.append(pinned);

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

  const house = el("div", "group");
  house.append(el("h3", null, "Instructions"));
  house.append(el("div", "hint",
    "Every agent sees this on every turn — Director, Operator, Coder, and any "
    + "chat you add. Standing rules: language, tone, what never to do."));
  const houseField = el("div", "field");
  const houseInput = el("textarea");
  houseInput.rows = 6;
  houseInput.placeholder = "e.g. Always reply in Swedish. Never buy anything.";
  houseInput.value = data.settings.instructions || "";
  houseField.append(houseInput);
  house.append(houseField);
  const saveHouse = el("button", "btn primary", "Save instructions");
  saveHouse.addEventListener("click", async () => {
    try {
      await api("/api/settings", {
        method: "PATCH",
        body: JSON.stringify({ instructions: houseInput.value }),
      });
      saveHouse.textContent = "Saved";
    } catch (error) {
      saveHouse.textContent = String(error.message || error);
    }
  });
  house.append(saveHouse);
  body.append(house);

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
  const money = (value) => new Intl.NumberFormat("en-US", {
    style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2,
  }).format(Number(value));
  const balanceLine = el("div", "line");
  balanceLine.append(el("span", null, "OpenRouter balance"));
  const balanceValue = el("span", "v");
  const balanceDetail = el("div", "hint");
  const paintBalance = (result) => {
    if (!result || result.ok === false || !Number.isFinite(Number(result.balance))) {
      balanceValue.textContent = "unavailable";
      balanceDetail.textContent = (result && result.error) || "Could not read the balance.";
      return;
    }
    balanceValue.textContent = `${money(result.balance)} remaining`;
    balanceDetail.textContent = `${money(result.total_credits)} purchased / ${money(result.total_usage)} used`;
  };
  balanceLine.append(balanceValue);
  backends.append(balanceLine, balanceDetail);
  paintBalance(data.balance);
  const refreshBalance = el("button", "btn", "Refresh balance");
  refreshBalance.addEventListener("click", async () => {
    refreshBalance.disabled = true;
    balanceValue.textContent = "refreshing...";
    try {
      paintBalance(await api("/api/openrouter/balance?refresh=1"));
    } catch (error) {
      paintBalance({ ok: false, error: String(error.message || error) });
    } finally {
      refreshBalance.disabled = false;
    }
  });
  backends.append(refreshBalance);
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
  for (const model of data.models.openrouter_models || []) {
    const option = document.createElement("option");
    option.value = `openrouter:${model.id}`;
    option.textContent = `${model.label} (OpenRouter)`;
    select.append(option);
  }
  const custom = document.createElement("option");
  custom.value = "openrouter:";
  custom.textContent = "OpenRouter model…";
  select.append(custom);
  const currentBackend = data.settings.defaults?.backend || "codex";
  const currentModel = data.settings.defaults?.model || "";
  const currentChoice = `${currentBackend}:${currentModel}`;
  select.value = [...select.options].some((option) => option.value === currentChoice)
    ? currentChoice : (currentBackend === "openrouter" ? "openrouter:" : currentChoice);
  modelField.append(select);
  defaults.append(modelField);

  const orField = el("div", "field");
  orField.append(el("label", null, "OpenRouter model id (when chosen above)"));
  const orInput = el("input");
  orInput.placeholder = "anthropic/claude-sonnet-4.5";
  if (currentBackend === "openrouter"
      && !(data.models.openrouter_models || []).some((row) => row.id === currentModel)) {
    orInput.value = currentModel;
  }
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
    const separator = select.value.indexOf(":");
    const backend = select.value.slice(0, separator);
    const model = select.value.slice(separator + 1);
    const patch = {
      defaults: {
        backend,
        model: backend === "openrouter" ? (model || orInput.value.trim()) : model,
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

function previewBlock(title, content, page) {
  const block = el("section", "dev-preview-block");
  block.dataset.previewPage = page;
  block.append(el("h2", null, title));
  const stage = el("div", "dev-stage");
  stage.innerHTML = content;
  block.append(stage);
  return block;
}

function developerGallery() {
  document.querySelector(".dev-gallery")?.remove();
  const gallery = el("div", "dev-gallery");
  const head = el("header", "dev-gallery-head");
  const close = el("button", "icon-btn");
  close.type = "button";
  close.setAttribute("aria-label", "Close component gallery");
  close.innerHTML = svg('<path d="M15 18l-6-6 6-6"/>');
  close.addEventListener("click", () => gallery.remove());
  const title = el("div", "title");
  title.append(el("h1", null, "Director components"));
  title.append(el("div", "sub", "Live UI states · development only"));
  head.append(close, title);

  const tabs = el("nav", "dev-tabs");
  for (const [id, label] of [["all", "All"], ["chat", "Chat"],
                             ["actions", "Actions"], ["operator", "Operator"]]) {
    const tab = el("button", `dev-tab${id === "all" ? " on" : ""}`, label);
    tab.type = "button";
    tab.addEventListener("click", () => {
      tabs.querySelectorAll(".dev-tab").forEach((node) => node.classList.toggle("on", node === tab));
      gallery.querySelectorAll(".dev-preview-block").forEach((node) => {
        node.classList.toggle("hidden", id !== "all" && node.dataset.previewPage !== id);
      });
    });
    tabs.append(tab);
  }

  const body = el("main", "dev-gallery-body");
  body.append(previewBlock("Churning / thinking",
    `<div class="working-sentinel live">${loadingPixels()}<span class="working-label">Churning</span></div>`
    + `<div class="thinking live expanded"><button class="thinking-head" type="button">`
    + `${loadingPixels()}<span class="thinking-label">Thinking</span>`
    + `${svg('<path d="M6 9l6 6 6-6"/>', "thinking-chevron")}</button>`
    + `<div class="thinking-body">I’m checking the live machine state and comparing it with the latest bridge heartbeat.</div></div>`, "chat"));
  body.append(previewBlock("Chat bubbles",
    `<div class="row-agent"><div class="bubble-agent assistant"><p>Here’s the concise answer from your agent.</p></div></div>`
    + `<div class="row-user"><div class="bubble-user">Can you check the latest version?</div></div>`
    + `<div class="row-agent named"><div class="speech"><div class="speaker-name">Luna</div>`
    + `<div class="bubble-agent assistant"><p>I found the issue and sent it to Director.</p></div></div></div>`
    + `<div class="row-agent relay-message"><div class="speech"><div class="speaker-name">From Coder</div>`
    + `<div class="bubble-agent relay-bubble">The tests are green.</div></div></div>`, "chat"));
  body.append(previewBlock("Scrolling subtitle",
    `<div class="title"><h1><span class="name-text">Researcher</span>`
    + `<span class="sub-pill scrolling" style="--subtitle-roll:8s;max-width:140px"><span class="sub-pill-track">`
    + `<span class="sub-pill-copy">Product discovery and long-range planning</span>`
    + `<span class="sub-pill-copy" aria-hidden="true">Product discovery and long-range planning</span>`
    + `</span></span></h1></div>`, "chat"));
  body.append(previewBlock("Homepage voice",
    `<div class="home-voice-wrap"><button class="home-voice" type="button" aria-label="Hold to talk">`
    + `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round">`
    + `<rect x="9" y="2.5" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3.5"/>`
    + `</svg></button></div>`, "actions"));
  body.append(previewBlock("Tool states",
    `<div class="tool-card running"><button class="tool-chip"><span class="glyph">${loadingPixels()}</span>`
    + `<span class="name">Searching chats</span><span class="detail">Windows power state</span><span class="meta">running</span></button></div>`
    + `<div class="tool-card ok"><button class="tool-chip"><span class="glyph">✓</span>`
    + `<span class="name">Sent to Luna</span><span class="detail">Please check this</span><span class="meta">done</span></button></div>`, "actions"));
  body.append(previewBlock("Approval",
    `<div class="action-card"><div class="kicker"><span>Needs your approval</span></div>`
    + `<div class="summary">Turn off the Windows PC?</div><pre>shutdown.exe /s /t 5</pre>`
    + `<div class="row"><button class="btn primary">Approve</button><button class="btn danger">Decline</button></div></div>`, "actions"));
  body.append(previewBlock("Question",
    `<div class="action-card"><div class="kicker"><span>Director is asking</span></div>`
    + `<div class="summary">Which agent should receive the recording?</div>`
    + `<div class="row"><button class="btn primary">Director</button><button class="btn">Luna</button></div></div>`, "actions"));
  body.append(previewBlock("Operator screen",
    `<div class="shot live dev-operator-shot"><div class="dev-screen-grid"><span></span><span></span><span></span></div>`
    + `<div class="cap">Operator screen · live preview</div></div>`
    + `<div class="action-card"><div class="kicker"><span>Your turn on the screen</span></div>`
    + `<div class="summary">Director needs you to finish the sign-in.</div>`
    + `<div class="row"><button class="btn primary">Open the screen</button><button class="btn">Done</button></div></div>`, "operator"));
  body.append(previewBlock("PC power",
    `<div class="dev-power-row"><button class="wake-pc" aria-label="Wake Windows PC">`
    + `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 3v8"/><path d="M8.5 6.2a7 7 0 1 0 7 0"/></svg><span>Wake PC</span></button>`
    + `<button class="wake-pc is-on" aria-label="Turn Windows PC off"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 3v8"/><path d="M8.5 6.2a7 7 0 1 0 7 0"/></svg><span>Turn PC off</span></button>`
    + `<small>off / on</small></div>`, "actions"));

  gallery.append(head, tabs, body);
  document.body.append(gallery);
}

function phoneMouseMachines() {
  return (state.machines || []).filter((machine) => machine.online);
}

function setPhoneMouseStatus(text, tone = "") {
  const control = state.phoneMouse;
  if (!control?.status) return;
  control.status.textContent = text;
  control.status.dataset.tone = tone;
}

function refreshPhoneMouseMachines() {
  const control = state.phoneMouse;
  if (!control?.select) return;
  const previous = control.select.value || control.machineId;
  const machines = phoneMouseMachines();
  control.select.replaceChildren();
  for (const machine of machines) {
    const option = document.createElement("option");
    option.value = machine.id;
    option.textContent = machine.name || "Computer";
    control.select.append(option);
  }
  const preferred = machines.find((machine) => machine.id === previous)
    || machines.find((machine) => /windows/i.test(`${machine.platform || ""} ${machine.name || ""}`))
    || machines[0];
  if (preferred) control.select.value = preferred.id;
  control.start.disabled = !preferred || control.starting;
  if (!preferred) setPhoneMouseStatus("No computers are online.", "error");
  else if (!control.active && !control.starting) setPhoneMouseStatus("Ready to connect.");
  if (control.machineId && !machines.some((machine) => machine.id === control.machineId)) {
    stopPhoneMouse(false);
    setPhoneMouseStatus("Computer went offline.", "error");
  }
}

function sendPhoneMouse(action, payload = {}, machineId = "") {
  const control = state.phoneMouse;
  const target = machineId || control?.machineId || control?.select?.value;
  if (!target) throw new Error("Choose an online computer.");
  if (!state.socket || state.socket.readyState !== WebSocket.OPEN) {
    throw new Error("Director is offline.");
  }
  state.socket.send(JSON.stringify({
    type: "mouse", machine_id: target, action, payload,
  }));
}

function orientedAcceleration(x, y) {
  const angle = Number(screen.orientation?.angle ?? window.orientation ?? 0);
  if (angle === 90 || angle === -270) return { x: -y, y: x };
  if (angle === 270 || angle === -90) return { x: y, y: -x };
  if (Math.abs(angle) === 180) return { x: -x, y: -y };
  return { x, y };
}

function mouseVelocity(value, sensitivity) {
  const deadzone = 0.24;
  const magnitude = Math.abs(value);
  if (magnitude <= deadzone) return 0;
  return Math.sign(value) * Math.min(90,
    Math.pow(magnitude - deadzone, 1.42) * 6.2 * sensitivity);
}

function calibratePhoneMouse() {
  const control = state.phoneMouse;
  if (!control?.active) return;
  control.zero = null;
  control.samples = [];
  control.dot.style.transform = "translate3d(0, 0, 0)";
  setPhoneMouseStatus("Hold the phone still to calibrateâ€¦", "working");
}

function handlePhoneMotion(event) {
  const control = state.phoneMouse;
  const acceleration = event.accelerationIncludingGravity;
  if (!control?.active || !acceleration
      || !Number.isFinite(acceleration.x) || !Number.isFinite(acceleration.y)) return;
  const point = orientedAcceleration(acceleration.x, acceleration.y);
  if (!control.zero) {
    control.samples.push(point);
    if (control.samples.length < 12) return;
    control.zero = {
      x: control.samples.reduce((sum, sample) => sum + sample.x, 0) / control.samples.length,
      y: control.samples.reduce((sum, sample) => sum + sample.y, 0) / control.samples.length,
    };
    control.samples = [];
    setPhoneMouseStatus("Connected Â· tilt to move", "ready");
    return;
  }
  const x = point.x - control.zero.x;
  const y = point.y - control.zero.y;
  const visualX = Math.max(-42, Math.min(42, x * 12));
  const visualY = Math.max(-42, Math.min(42, -y * 12));
  control.dot.style.transform = `translate3d(${visualX}px, ${visualY}px, 0)`;
  const now = performance.now();
  if (now - control.lastMove < 32) return;
  control.lastMove = now;
  const sensitivity = Number(control.sensitivity.value || 1);
  const dx = Math.round(mouseVelocity(x, sensitivity));
  const dy = Math.round(mouseVelocity(-y, sensitivity));
  if (!dx && !dy) return;
  try {
    sendPhoneMouse("move", { dx, dy });
  } catch (error) {
    stopPhoneMouse(false);
    setPhoneMouseStatus(error.message || String(error), "error");
  }
}

async function requestMotionPermission() {
  if (typeof DeviceMotionEvent === "undefined") {
    throw new Error("Motion sensors are not available on this device.");
  }
  if (typeof DeviceMotionEvent.requestPermission === "function") {
    const result = await DeviceMotionEvent.requestPermission();
    if (result !== "granted") throw new Error("Motion access was not allowed.");
  }
}

async function startPhoneMouse() {
  const control = state.phoneMouse;
  if (!control || control.starting || control.active) return;
  const machineId = control.select.value;
  if (!machineId) return setPhoneMouseStatus("No computers are online.", "error");
  control.starting = true;
  control.start.disabled = true;
  setPhoneMouseStatus("Requesting motion accessâ€¦", "working");
  try {
    await requestMotionPermission();
    control.machineId = machineId;
    sendPhoneMouse("start", {}, machineId);
    setPhoneMouseStatus("Connecting to the computerâ€¦", "working");
  } catch (error) {
    control.starting = false;
    control.start.disabled = false;
    setPhoneMouseStatus(error.message || String(error), "error");
  }
}

async function activatePhoneMouse() {
  const control = state.phoneMouse;
  if (!control || control.active) return;
  control.starting = false;
  control.active = true;
  control.start.textContent = "Mouse active";
  control.start.disabled = true;
  control.select.disabled = true;
  control.calibrate.disabled = false;
  window.addEventListener("devicemotion", handlePhoneMotion, { passive: true });
  calibratePhoneMouse();
  try {
    control.wakeLock = await navigator.wakeLock?.request("screen");
  } catch { /* motion still works without a wake lock */ }
}

function stopPhoneMouse(close = false) {
  const control = state.phoneMouse;
  if (!control) return;
  if (control.machineId && (control.active || control.starting)) {
    try { sendPhoneMouse("stop", {}, control.machineId); } catch {}
  }
  window.removeEventListener("devicemotion", handlePhoneMotion);
  control.wakeLock?.release().catch(() => {});
  control.wakeLock = null;
  control.active = false;
  control.starting = false;
  control.zero = null;
  control.samples = [];
  control.machineId = "";
  if (close) {
    control.page.remove();
    state.phoneMouse = null;
    return;
  }
  control.start.textContent = "Enable phone mouse";
  control.select.disabled = false;
  control.calibrate.disabled = true;
  refreshPhoneMouseMachines();
}

function updatePhoneMouseStatus(payload) {
  const control = state.phoneMouse;
  if (!control) return;
  if (payload?.machine_id && control.machineId
      && payload.machine_id !== control.machineId) return;
  if (payload?.status === "ready") {
    activatePhoneMouse();
  } else if (payload?.status === "stopped") {
    if (control.active || control.starting) stopPhoneMouse(false);
  } else if (payload?.status === "error") {
    const message = payload.error || "Phone mouse could not connect.";
    control.active = false;
    control.starting = false;
    stopPhoneMouse(false);
    setPhoneMouseStatus(message, "error");
  }
}

function bindPhoneMouseButton(button, name) {
  const release = () => {
    button.classList.remove("pressed");
    if (state.phoneMouse?.active) {
      try { sendPhoneMouse("button", { button: name, pressed: false }); } catch {}
    }
  };
  button.addEventListener("pointerdown", (event) => {
    if (!state.phoneMouse?.active) return;
    event.preventDefault();
    button.setPointerCapture?.(event.pointerId);
    button.classList.add("pressed");
    try { sendPhoneMouse("button", { button: name, pressed: true }); } catch {}
  });
  button.addEventListener("pointerup", release);
  button.addEventListener("pointercancel", release);
  button.addEventListener("lostpointercapture", release);
  button.addEventListener("contextmenu", (event) => event.preventDefault());
}

function openPhoneMouse() {
  if (state.phoneMouse) stopPhoneMouse(true);
  document.querySelector(".phone-mouse-page")?.remove();
  const page = el("div", "phone-mouse-page");
  page.innerHTML = `
    <header class="phone-mouse-head">
      <button class="icon-btn phone-mouse-close" type="button" aria-label="Back">${svg('<path d="M15 18l-6-6 6-6"/>')}</button>
      <div><h1>Phone mouse</h1><p>Accelerometer remote</p></div>
      <span class="conn-dot" data-conn="${state.socket?.readyState === WebSocket.OPEN ? "on" : "off"}" aria-label="Director connection"></span>
    </header>
    <main class="phone-mouse-body">
      <label class="phone-mouse-machine"><span>Computer</span><select></select></label>
      <div class="phone-mouse-status" role="status" aria-live="polite">Ready to connect.</div>
      <div class="phone-mouse-field" aria-hidden="true">
        <div class="phone-mouse-rings"></div><div class="phone-mouse-dot"></div>
        <span>Tilt to steer</span>
      </div>
      <label class="phone-mouse-sensitivity"><span>Sensitivity</span><input type="range" min="0.55" max="2.2" step="0.05" value="1"></label>
      <div class="phone-mouse-actions">
        <button class="btn phone-mouse-start primary" type="button">Enable phone mouse</button>
        <button class="btn phone-mouse-calibrate" type="button" disabled>Recalibrate</button>
      </div>
    </main>
    <footer class="phone-mouse-buttons">
      <button type="button" data-button="left"><span>Left</span><small>hold to drag</small></button>
      <button type="button" data-button="right"><span>Right</span><small>click</small></button>
    </footer>`;
  const control = {
    page,
    select: page.querySelector("select"),
    status: page.querySelector(".phone-mouse-status"),
    dot: page.querySelector(".phone-mouse-dot"),
    sensitivity: page.querySelector("input[type=range]"),
    start: page.querySelector(".phone-mouse-start"),
    calibrate: page.querySelector(".phone-mouse-calibrate"),
    active: false, starting: false, machineId: "", zero: null,
    samples: [], lastMove: 0, wakeLock: null,
  };
  state.phoneMouse = control;
  control.start.addEventListener("click", startPhoneMouse);
  control.calibrate.addEventListener("click", calibratePhoneMouse);
  control.select.addEventListener("change", () => {
    if (control.active || control.starting) stopPhoneMouse(false);
  });
  page.querySelector(".phone-mouse-close").addEventListener("click", () => stopPhoneMouse(true));
  page.querySelectorAll("[data-button]").forEach((button) => {
    bindPhoneMouseButton(button, button.dataset.button);
  });
  document.body.append(page);
  refreshPhoneMouseMachines();
}

function openDeveloperMenu() {
  const { body, dismiss } = openSheet("Developer");
  const group = el("div", "group");
  group.append(el("div", "hint", "Preview Director’s real interface states without waiting for an agent run."));
  const components = el("button", "btn primary", "Open component gallery");
  components.addEventListener("click", () => { dismiss(); developerGallery(); });
  group.append(components);
  const phoneMouse = el("button", "btn", "Open phone mouse");
  phoneMouse.addEventListener("click", () => { dismiss(); openPhoneMouse(); });
  group.append(phoneMouse);
  const diagnostics = el("div", "line");
  diagnostics.append(el("span", null, "Schedule timezone"));
  diagnostics.append(el("span", "v", "Europe/Stockholm"));
  group.append(diagnostics);
  body.append(group);
}

function installDirectorDoubleTap() {
  const logo = $("director-logo");
  if (!logo) return;
  let lastTap = 0;
  logo.addEventListener("click", (event) => {
    const now = performance.now();
    if (now - lastTap < 420) {
      event.preventDefault();
      lastTap = 0;
      openDeveloperMenu();
    } else {
      lastTap = now;
    }
  });
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
    btn.title = BLOB_EMOTION_LABELS[emotion] || emotion;
    btn.setAttribute("aria-label", btn.title);
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
  for (const model of catalogue.openrouter_models || []) {
    const option = document.createElement("option");
    option.value = `openrouter:${model.id}`;
    option.textContent = `${model.label} (OpenRouter)`;
    select.append(option);
  }
  const custom = document.createElement("option");
  custom.value = "openrouter:";
  custom.textContent = "OpenRouter model…";
  select.append(custom);
  const draftChoice = draft.backend ? `${draft.backend}:${draft.model}` : "";
  select.value = [...select.options].some((option) => option.value === draftChoice)
    ? draftChoice : (draft.backend === "openrouter" ? "openrouter:" : "");
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
    const choice = select.value || ":";
    const separator = choice.indexOf(":");
    const backend = choice.slice(0, separator);
    const model = choice.slice(separator + 1);
    const payload = {
      name: nameInput.value.trim(),
      emoji: encodeBlob(draft.blob),
      avatar: draft.avatar,
      subtitle: subtitleInput.value.trim(),
      system_prompt: promptInput.value,
      backend: backend || "",
      model: backend === "openrouter" ? (model || orInput.value.trim()) : (model || ""),
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
  if (!creating && !isGroup(agent)) {
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

function newChatChooser() {
  const { body, dismiss } = openSheet("New chat");
  const box = el("div", "group");
  const agentBtn = el("button", "btn", "Agent");
  agentBtn.addEventListener("click", () => { dismiss(); agentEditor(null); });
  const groupBtn = el("button", "btn primary", "Group chat");
  groupBtn.addEventListener("click", () => { dismiss(); groupEditor(null); });
  box.append(agentBtn, groupBtn);
  box.append(el("div", "hint",
    "A group is several agents in one room. They decide who speaks. "
    + "If you ask someone to do a task, they take it to their private chat with you."));
  body.append(box);
}

async function groupEditor(group) {
  const creating = !group;
  const draft = {
    name: group?.name || "",
    blob: parseBlob(group?.emoji) || blobSpec(group?.id || group?.name || `group-${Date.now()}`),
    avatar: group?.avatar || "",
    subtitle: group?.subtitle || "",
    members: new Set(group?.members || []),
    rules: group?.rules || "",
  };
  const { body, dismiss } = openSheet(creating ? "New group chat" : `Edit ${draft.name || "group"}`);

  const identity = el("div", "group");
  identity.append(el("h3", null, "Group"));
  const picker = el("div", "avatar-picker");
  const preview = el("div", "avatar big");
  const paintAvatar = () => {
    fillAvatar(preview, {
      id: group?.id || draft.name || "new-group",
      avatar: draft.avatar,
      emoji: encodeBlob(draft.blob),
      frozen: true,
    }, "idle");
  };
  paintAvatar();
  picker.append(preview);
  identity.append(picker);
  const shuffle = el("button", "btn ghost", "Shuffle this blob");
  shuffle.addEventListener("click", () => {
    draft.avatar = "";
    draft.blob = blobSpec(`${group?.id || draft.name || "group"}-${Math.random()}`);
    paintAvatar();
  });
  identity.append(shuffle);
  const nameInput = textInput(draft.name, "Ops, Home, Studio…");
  identity.append(field("Name", nameInput));
  const subtitleInput = textInput(draft.subtitle, "What this room is for");
  identity.append(field("Subtitle", subtitleInput));
  body.append(identity);

  const people = el("div", "group");
  people.append(el("h3", null, "Agents in this chat"));
  people.append(el("div", "hint", "Everyone here sees every message. They speak when it is for them."));
  const picks = el("div", "member-picks");
  const candidates = state.agents.filter((row) => !isGroup(row));
  if (!candidates.length) {
    people.append(el("div", "hint", "Create an agent first, then add them here."));
  }
  for (const agent of candidates) {
    const row = el("button", `member-pick${draft.members.has(agent.id) ? " on" : ""}`);
    row.type = "button";
    row.append(avatarNode(agent, "tiny"));
    const label = el("span");
    label.append(document.createTextNode(agent.name));
    row.append(label);
    if (agent.subtitle) row.append(el("span", "sub", agent.subtitle));
    row.addEventListener("click", () => {
      if (draft.members.has(agent.id)) draft.members.delete(agent.id);
      else draft.members.add(agent.id);
      row.classList.toggle("on", draft.members.has(agent.id));
    });
    picks.append(row);
  }
  people.append(picks);
  body.append(people);

  const rulesBox = el("div", "group");
  rulesBox.append(el("h3", null, "Group rules"));
  const rulesInput = el("textarea");
  rulesInput.rows = 6;
  rulesInput.value = draft.rules;
  rulesInput.placeholder = "How this room works. Everyone in the group sees these.";
  rulesBox.append(rulesInput);
  rulesBox.append(el("div", "hint",
    "Examples: keep it short, one person owns each task, don't pile on."));
  body.append(rulesBox);

  const actions = el("div", "group");
  const save = el("button", "btn primary", creating ? "Create group" : "Save");
  save.addEventListener("click", async () => {
    const members = [...draft.members];
    const payload = {
      name: nameInput.value.trim(),
      emoji: encodeBlob(draft.blob),
      avatar: draft.avatar,
      subtitle: subtitleInput.value.trim(),
      kind: "group",
      members,
      rules: rulesInput.value,
    };
    if (!payload.name) { alert("Give the group a name."); return; }
    if (members.length < 2) { alert("Pick at least two agents."); return; }
    save.disabled = true;
    try {
      if (creating) {
        const created = await api("/api/agents", { method: "POST", body: JSON.stringify(payload) });
        dismiss();
        await loadAgents();
        if (created?.agent?.id) await openAgent(created.agent.id);
      } else {
        await api(`/api/agents/${group.id}`, { method: "PATCH", body: JSON.stringify(payload) });
        dismiss();
        await loadAgents();
        if (state.agentId === group.id) {
          const fresh = state.agents.find((row) => row.id === group.id);
          if (fresh) paintChatHeader(fresh);
        }
      }
    } catch (error) {
      alert(String(error.message || error));
      save.disabled = false;
    }
  });
  actions.append(save);
  if (!creating) {
    const remove = el("button", "btn danger", "Delete this group");
    remove.addEventListener("click", async () => {
      if (!confirm(`Delete ${group.name} ? The agents themselves stay.`)) return;
      await api(`/api/agents/${group.id}`, { method: "DELETE" });
      dismiss();
      if (state.agentId === group.id) state.agentId = "";
      show("agents");
      await loadAgents();
    });
    actions.append(remove);
  }
  body.append(actions);
}

function chatMenu() {
  const agent = state.agents.find((row) => row.id === state.agentId) || {};
  const { body, dismiss } = openSheet(agent.name || "Conversation");
  const group = el("div", "group");
  const groupChat = isGroup(agent);

  const options = [
    [groupChat ? "Edit this group" : "Edit this agent",
     () => { dismiss(); groupChat ? groupEditor(agent) : agentEditor(agent); }],
  ];
  if (!groupChat) {
    options.push([agent.id === state.pinnedAgentId
      ? "Pinned for homepage voice"
      : "Pin for homepage voice", async () => {
        state.pinnedAgentId = agent.id;
        save();
        paintHomeVoice();
        await api("/api/settings", {
          method: "PATCH", body: JSON.stringify({ phone: { pinned_agent_id: agent.id } }),
        });
        dismiss();
      }]);
    options.push(["Routines", () => { dismiss(); routinesSheet(agent); }]);
    options.push(["Open the operator screen", () => { dismiss(); openTakeover(); }]);
  }
  options.push(
    ["Start a fresh conversation", async () => {
      dismiss();
      await api(`/api/threads/${state.threadId}/clear`, { method: "POST" });
      await openAgent(state.agentId);
    }],
    ["Stop the current run", async () => {
      dismiss();
      await stopTurn();
    }],
  );
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

function syncKeyboardViewport() {
  const viewport = window.visualViewport;
  if (!viewport) return;
  // iOS sometimes leaves the layout viewport at full screen while moving and
  // shrinking the visual viewport for the keyboard. Pin the app to what is
  // actually visible; the composer then stays exactly 8px above its bottom.
  document.documentElement.style.setProperty("--visual-height", `${viewport.height}px`);
  document.documentElement.style.setProperty("--visual-top", `${viewport.offsetTop}px`);
  const keyboardOpen = window.innerHeight - viewport.height > 100;
  document.documentElement.style.setProperty(
    "--composer-bottom",
    keyboardOpen ? "8px" : "max(8px, env(safe-area-inset-bottom, 0px))",
  );
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

/* ---------------- CODE session viewer ---------------- */

function ingestCodeEvents(payload) {
  const jobId = String(payload.job_id || "");
  if (!jobId) return;
  if (payload.session_id) {
    const meta = state.jobMeta.get(jobId) || {};
    meta.session_id = payload.session_id;
    state.jobMeta.set(jobId, meta);
  }
  const view = state.codeView;
  if (!view || view.jobId !== jobId || !view.view) return;
  if (payload.reset) {
    view.view.reset();
    view.since = 0;
  }
  const events = payload.events || [];
  if (events.length) view.view.push(events);
  if (payload.size != null) view.since = Number(payload.size) || view.since;
}

async function openCodeSession(jobId, sessionId = "", title = "", { pushHistory = true } = {}) {
  if (!jobId) return;
  const screen = $("screen-code");
  const mount = $("code-session-transcript");
  const jump = $("code-session-jump");
  if (!screen || !mount) return;

  const meta = state.jobMeta.get(jobId) || {};
  if (sessionId) meta.session_id = sessionId;
  if (title) meta.title = title;
  state.jobMeta.set(jobId, meta);

  $("code-session-title").textContent = meta.title || title || "CODE session";
  $("code-session-sub").textContent = meta.session_id
    ? `session ${meta.session_id}`
    : jobId;

  if (state.codeView?.view) {
    try { state.codeView.view.reset(); } catch {}
  }
  mount.textContent = "";
  let Transcript;
  try {
    ({ Transcript } = await import("/code/transcript.js"));
  } catch (error) {
    mount.textContent = String(error.message || error);
    show("code");
    return;
  }
  const view = new Transcript(mount, {
    jump,
    isActive: () => !$("screen-code")?.classList.contains("hidden"),
    onReveal: null,
    onAnswer: null,
  });
  state.codeView = { jobId, sessionId: meta.session_id || "", since: 0, view };
  show("code");
  if (pushHistory) {
    history.pushState({ directorScreen: "code", agentId: state.agentId,
                        jobId, sessionId: meta.session_id || "", title: meta.title || title }, "");
  }

  let data = null;
  let lastError = "";
  for (let attempt = 0; attempt < 10; attempt += 1) {
    try {
      data = await api(`/api/jobs/${encodeURIComponent(jobId)}/code-events?since=0`);
      break;
    } catch (error) {
      lastError = String(error.message || error);
      if (!/no CODE session yet|409/.test(lastError) || attempt === 9) break;
      await new Promise((resolve) => setTimeout(resolve, 400));
      if (state.codeView?.jobId !== jobId) return;
    }
  }
  if (state.codeView?.jobId !== jobId) return;
  if (!data) {
    const note = el("div", "status-row",
                    `Could not load CODE chat: ${lastError || "unknown error"}`);
    mount.append(note);
    return;
  }
  if (data.session_id) {
    state.codeView.sessionId = data.session_id;
    meta.session_id = data.session_id;
    state.jobMeta.set(jobId, meta);
    $("code-session-sub").textContent = `session ${data.session_id}`;
  }
  if (data.reset) view.reset();
  if (data.events?.length) view.push(data.events);
  state.codeView.since = Number(data.size || 0);
}

function closeCodeSession() {
  if (state.codeView?.view) {
    try { state.codeView.view.reset(); } catch {}
  }
  state.codeView = null;
  if (state.agentId) show("chat");
  else show("agents");
}

function installEdgeSwipe() {
  let gesture = null;
  document.addEventListener("touchstart", (event) => {
    if (event.touches.length !== 1 || event.touches[0].clientX > 24) {
      gesture = null;
      return;
    }
    const touch = event.touches[0];
    gesture = { x: touch.clientX, y: touch.clientY, done: false };
  }, { passive: true });
  document.addEventListener("touchmove", (event) => {
    if (!gesture || gesture.done || event.touches.length !== 1) return;
    const touch = event.touches[0];
    const dx = touch.clientX - gesture.x;
    const dy = Math.abs(touch.clientY - gesture.y);
    if (dx > 72 && dx > dy * 1.35) {
      gesture.done = true;
      navigateBack();
    } else if (dy > 44 || dx < -16) {
      gesture = null;
    }
  }, { passive: true });
  document.addEventListener("touchend", () => { gesture = null; }, { passive: true });
  document.addEventListener("touchcancel", () => { gesture = null; }, { passive: true });
}

/* ---------------- boot ---------------- */

async function boot() {
  syncKeyboardViewport();
  fillAvatar($("pair-mark"), { id: "agt_director" });
  fillAvatar($("device-blob"), { id: state.device || "device" });
  fillAvatar($("empty-blob"), { id: "agt_director" });
  if (["localhost", "127.0.0.1"].includes(location.hostname)
      && new URLSearchParams(location.search).get("components") === "1") {
    developerGallery();
  }
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
    history.replaceState({ directorScreen: "agents" }, "");
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
  setInterval(() => {
    if (document.hidden || !state.agents.length) return;
    for (const agent of state.agents) paintAgentRow(agent);
  }, 60 * 1000);
  setInterval(rotateActiveBlobEyes, ACTIVE_EYE_INTERVAL);
  setInterval(() => {
    if (!document.hidden) refreshPowerStatus();
  }, 10000);

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
  $("btn-wake-pc")?.addEventListener("click", togglePcPower);
  installDirectorDoubleTap();
  const homeVoice = $("btn-home-voice");
  homeVoice?.addEventListener("click", toggleHomeVoice);
  homeVoice?.addEventListener("contextmenu", (event) => event.preventDefault());
  $("btn-new-agent").addEventListener("click", newChatChooser);
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
  $("btn-back").addEventListener("click", navigateBack);
  $("btn-code-back")?.addEventListener("click", navigateBack);
  ensureHistoryToggle();
  installEdgeSwipe();
  window.addEventListener("popstate", (event) => {
    const target = event.state || { directorScreen: "agents" };
    if (target.directorScreen === "code" && target.jobId) {
      openCodeSession(target.jobId, target.sessionId || "", target.title || "",
                      { pushHistory: false }).catch(() => {});
      return;
    }
    if (state.codeView?.view) {
      try { state.codeView.view.reset(); } catch {}
      state.codeView = null;
    }
    if (target.directorScreen === "chat" && target.agentId) {
      if (state.agentId === target.agentId) show("chat");
      else openAgent(target.agentId, { pushHistory: false }).catch(() => leaveChat());
      return;
    }
    leaveChat();
  });
  window.addEventListener("resize", () => {
    if (!state.token) return;
    if (state.codeView) {
      show("code");
      return;
    }
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
  window.visualViewport?.addEventListener("resize", syncKeyboardViewport);
  window.visualViewport?.addEventListener("scroll", syncKeyboardViewport);
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
    if (!visible && (state.phoneMouse?.active || state.phoneMouse?.starting)) {
      stopPhoneMouse(false);
    }
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
    if (state.phoneMouse?.active || state.phoneMouse?.starting) stopPhoneMouse(false);
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
