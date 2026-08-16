/* Offline playground for the live Director PWA.
   Intercepts fetch + WebSocket so the real UI boots without the Linux box. */
(function () {
  const now = () => Date.now() / 1000;
  let seq = 40;
  const nid = (prefix) => `${prefix}_${Math.random().toString(36).slice(2, 10)}`;

  const settings = {
    backends: {
      codex: { enabled: true, codex_home: "" },
      openrouter: { enabled: true, api_key: "••••fake" },
    },
    defaults: { backend: "codex", model: "gpt-5.6-luna", reasoning: "low" },
    operator: {
      backend: "codex", model: "gpt-5.6-luna", reasoning: "medium",
      mode: "real", width: 1280, height: 720,
    },
    voice: { transcribe_backend: "openai", openai_api_key: "", model: "whisper-1" },
    appearance: {
      user_bubble: "#3a5a8c", user_text: "#f2f3f4",
      agent_bubble: "#2b2c2f", agent_text: "#f2f3f4",
    },
    phone: { pinned_agent_id: "agt_director" },
    safety: { confirm_destructive: true, approve_all: false },
    instructions: "Always reply in a sharp, short Slack voice. Never sugarcoat.",
    prompts: { base: "", coordinator: "", group: "" },
    wake: { mac: "30:C5:99:D0:0D:4A", ip: "192.168.0.83" },
    push: { enabled: false, public_key: "" },
  };

  const agents = [
    {
      id: "agt_director", name: "Director",
      emoji: "blob|squircle|#6d8cff|focused", avatar: "", kind: "director",
      subtitle: "Runs the whole thing", system_prompt: "You are Director.",
      backend: "codex", model: "gpt-5.6-luna", reasoning: "low",
      auto_approve: false, notify: true, members: [], rules: "",
      routines: 1, thread_id: "thr_director",
      preview: "PC is on. Chrome is up.", updated_at: now() - 90,
      status: "idle", busy: false, working: [],
    },
    {
      id: "agt_luna", name: "Luna",
      emoji: "blob|circle|#c084fc|thinking", avatar: "", kind: "custom",
      subtitle: "Product discovery and long-range planning",
      system_prompt: "You are Luna.",
      backend: "openrouter", model: "openai/gpt-5.6-luna", reasoning: "low",
      auto_approve: false, notify: true, members: [], rules: "",
      routines: 0, thread_id: "thr_luna",
      preview: "Checking the live machine state…", updated_at: now() - 20,
      status: "running", busy: true, working: [],
    },
    {
      id: "agt_researcher", name: "Researcher",
      emoji: "blob|pill|#5ec8a0|curious", avatar: "", kind: "custom",
      subtitle: "Looks things up", system_prompt: "You are Researcher.",
      backend: "codex", model: "gpt-5.6-luna", reasoning: "medium",
      auto_approve: true, notify: true, members: [], rules: "",
      routines: 0, thread_id: "thr_research",
      preview: "The tests are green.", updated_at: now() - 3600,
      status: "idle", busy: false, working: [],
    },
    {
      id: "agt_group", name: "House chat",
      emoji: "blob|diamond|#f0b45a|happy", avatar: "", kind: "group",
      subtitle: "", system_prompt: "",
      backend: "", model: "", reasoning: "",
      auto_approve: false, notify: true,
      members: ["agt_director", "agt_luna", "agt_researcher"],
      rules: "Stay short. Ping the right specialist.",
      routines: 0, thread_id: "thr_group",
      preview: "From Coder: The tests are green.", updated_at: now() - 7200,
      status: "idle", busy: false, working: [],
    },
  ];

  const machines = [{
    id: "mac_windows", name: "calle-windows", platform: "windows",
    online: true, caps: ["code", "shell", "screen"],
  }];

  const wake = {
    available: true, connected: true, can_power_off: true,
    power_state: "on", reachable: true, online: true, name: "calle-windows",
  };

  const codeEvents = [
    { ts: now() - 68, kind: "user", role: "user", text: "Polish the Director phone stream." },
    { ts: now() - 67, kind: "activity", activity_id: "think-1", activity_type: "thinking", phase: "started", title: "Thinking" },
    { ts: now() - 66, kind: "activity", activity_id: "think-1", activity_type: "thinking", phase: "update", title: "Thinking", delta: "Tracing the compact phone layout and its event flow.", stream: "summary" },
    { ts: now() - 65, kind: "activity", activity_id: "think-1", activity_type: "thinking", phase: "completed", title: "Thought through the approach" },
    { ts: now() - 64, kind: "activity", activity_id: "read-1", activity_type: "read", phase: "completed", title: "Read file", detail: "phone_site/director.js" },
    { ts: now() - 63, kind: "activity", activity_id: "think-2", activity_type: "thinking", phase: "started", title: "Thinking" },
    { ts: now() - 62, kind: "activity", activity_id: "think-2", activity_type: "thinking", phase: "update", title: "Thinking", delta: "Keeping every reasoning round inside one Activity disclosure.", stream: "summary" },
    { ts: now() - 61, kind: "activity", activity_id: "think-2", activity_type: "thinking", phase: "completed", title: "Thought through the approach" },
    { ts: now() - 60, kind: "assistant", role: "assistant", text: "The phone stream is compact and the controls now reflect their state." },
  ];

  function msg(threadId, role, content, meta, ageSec, sequence) {
    return {
      id: nid("msg"), thread_id: threadId, role, content,
      meta: meta || {}, created_at: now() - ageSec, sequence,
    };
  }

  const threads = {
    thr_director: {
      thread: {
        id: "thr_director", agent_id: "agt_director", title: "",
        preview: "PC is on. Chrome is up.", status: "idle",
        archived: 0, created_at: now() - 86400, updated_at: now() - 90,
        compacted_through: 0, hidden_count: 0,
      },
      messages: [
        msg("thr_director", "user", "Is the PC on? Open Chrome if it is.", {}, 400, 1),
        msg("thr_director", "tool_call", "", {
          call_id: "c1", name: "machine_dirs", arguments: JSON.stringify({ path: "C:\\\\" }),
        }, 390, 2),
        msg("thr_director", "tool_result", "ok", {
          call_id: "c1", name: "machine_dirs",
          card: { title: "Listed C:\\", preview: "Windows power state", meta: "done", body: "Desktop, Documents, aiOS" },
          output: "Desktop\nDocuments\naiOS",
        }, 380, 3),
        msg("thr_director", "assistant",
          "PC is on. Chrome is up on the virtual display — not black this time.",
          { reasoning: "Checking the live machine state and comparing it with the latest bridge heartbeat." },
          90, 4),
        msg("thr_director", "user", "Polish the Director phone stream.", {}, 78, 5),
        msg("thr_director", "tool_call", "", {
          call_id: "c2", name: "code_session", arguments: JSON.stringify({ task: "Polish the Director phone stream." }),
        }, 76, 6),
        msg("thr_director", "tool_result", "ok", {
          call_id: "c2", name: "code_session",
          card: { title: "code", preview: "Polish the Director phone stream", meta: "calle-windows", tone: "accent", job_id: "job_phone_ui", session_id: "session_phone_ui" },
          output: "CODE session running.",
        }, 74, 7),
        msg("thr_director", "assistant", "I started the focused aiOS CODE session.", {}, 72, 8),
      ],
      working: [], questions: [],
    },
    thr_luna: {
      thread: {
        id: "thr_luna", agent_id: "agt_luna", title: "",
        preview: "Checking the live machine state…", status: "running",
        archived: 0, created_at: now() - 8000, updated_at: now() - 20,
        compacted_through: 0, hidden_count: 0,
      },
      messages: [
        msg("thr_luna", "user", "Can you check the latest version?", {}, 120, 1),
        msg("thr_luna", "assistant", "Here’s the concise answer from your agent.", {}, 80, 2),
      ],
      working: [], questions: [],
    },
    thr_research: {
      thread: {
        id: "thr_research", agent_id: "agt_researcher", title: "",
        preview: "The tests are green.", status: "idle",
        archived: 0, created_at: now() - 20000, updated_at: now() - 3600,
        compacted_through: 0, hidden_count: 0,
      },
      messages: [
        msg("thr_research", "user", "Are the tests green?", {}, 3700, 1),
        msg("thr_research", "assistant", "Yes — the focused suite passed.", {}, 3600, 2),
      ],
      working: [], questions: [],
    },
    thr_group: {
      thread: {
        id: "thr_group", agent_id: "agt_group", title: "",
        preview: "From Coder: The tests are green.", status: "idle",
        archived: 0, created_at: now() - 40000, updated_at: now() - 7200,
        compacted_through: 0, hidden_count: 0,
      },
      messages: [
        msg("thr_group", "user", "Who should look at the failing job?", {}, 7300, 1),
        msg("thr_group", "assistant", "I found the issue and sent it to Director.",
          { speaker_id: "agt_luna", speaker_name: "Luna" }, 7250, 2),
        msg("thr_group", "user", "The tests are green.", {
          kind: "agent_message", sender_id: "agt_researcher", sender_name: "Researcher",
        }, 7200, 3),
      ],
      working: [], questions: [],
    },
  };

  const routines = [{
    id: "rtn_morning", agent_id: "agt_director", name: "Morning brief",
    prompt: "Summarise overnight mail and PC status.",
    schedule: { kind: "daily", time: "08:00" },
    next_run: now() + 3600, enabled: true,
  }];

  function statePayload() {
    return {
      ok: true,
      agents,
      machines,
      wake,
      cursor: seq,
      defaults: settings.defaults,
      phone: settings.phone,
      timezone: "Europe/Stockholm",
      operator: { running: true, display: ":0" },
      pending_approvals: [],
    };
  }

  function threadPayload(threadId) {
    const row = threads[threadId];
    if (!row) return null;
    return { ok: true, cursor: seq, ...row };
  }

  function agentThread(agentId) {
    const agent = agents.find((row) => row.id === agentId);
    if (!agent) return null;
    return threadPayload(agent.thread_id);
  }

  function merge(base, patch) {
    const out = { ...base };
    for (const [key, value] of Object.entries(patch || {})) {
      if (value && typeof value === "object" && !Array.isArray(value)
          && value && typeof out[key] === "object" && !Array.isArray(out[key])) {
        out[key] = merge(out[key], value);
      } else {
        out[key] = value;
      }
    }
    return out;
  }

  function json(data, status = 200) {
    return new Response(JSON.stringify(data), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  }

  class FakeWebSocket {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;
    constructor(url) {
      this.url = url;
      this.readyState = FakeWebSocket.CONNECTING;
      this.bufferedAmount = 0;
      this.extensions = "";
      this.protocol = "";
      this._listeners = { open: [], message: [], close: [], error: [] };
      window.__offlineSocket = this;
      queueMicrotask(() => {
        this.readyState = FakeWebSocket.OPEN;
        this._fire("open", {});
        this.push({ kind: "ready", payload: { cursor: seq } });
      });
    }
    addEventListener(type, fn) {
      (this._listeners[type] ||= []).push(fn);
    }
    removeEventListener(type, fn) {
      const list = this._listeners[type] || [];
      this._listeners[type] = list.filter((item) => item !== fn);
    }
    send() {}
    close() {
      this.readyState = FakeWebSocket.CLOSED;
      this._fire("close", { wasClean: true });
    }
    _fire(type, event) {
      for (const fn of this._listeners[type] || []) fn(event);
    }
    push(data) {
      if (this.readyState !== FakeWebSocket.OPEN) return;
      this._fire("message", { data: JSON.stringify(data) });
    }
  }

  function emit(kind, payload, threadId) {
    seq += 1;
    window.__offlineSocket?.push({
      id: seq, kind, thread_id: threadId, payload, created_at: now(),
    });
  }

  async function replyTo(threadId, text) {
    const row = threads[threadId];
    if (!row) return;
    row.thread.status = "running";
    emit("thread.status", { status: "running" }, threadId);
    emit("reasoning.delta", { text: "Looking at what you just said. " }, threadId);
    await new Promise((resolve) => setTimeout(resolve, 350));
    const chunks = [
      "Got it — this is the offline playground, so nothing hits the Linux box. ",
      "Edit `director.css` / `director.js` / `index.html` here and refresh.",
    ];
    let full = "";
    for (const chunk of chunks) {
      full += chunk;
      emit("message.delta", { text: chunk }, threadId);
      await new Promise((resolve) => setTimeout(resolve, 180));
    }
    const assistant = msg(threadId, "assistant", full, {}, 0, row.messages.length + 1);
    row.messages.push(assistant);
    row.thread.status = "idle";
    row.thread.preview = full.slice(0, 80);
    row.thread.updated_at = now();
    const agent = agents.find((item) => item.thread_id === threadId);
    if (agent) {
      agent.preview = row.thread.preview;
      agent.updated_at = row.thread.updated_at;
      agent.status = "idle";
      agent.busy = false;
    }
    emit("message.assistant", { text: full, id: assistant.id }, threadId);
    emit("thread.status", { status: "idle" }, threadId);
  }

  async function route(url, options = {}) {
    const parsed = new URL(url, location.origin);
    const path = parsed.pathname;
    const method = String(options.method || "GET").toUpperCase();
    let body = {};
    if (options.body && typeof options.body === "string") {
      try { body = JSON.parse(options.body); } catch { body = {}; }
    }

    if (path === "/api/health") {
      return json({ ok: true, service: "aios-director-offline", time: now() });
    }
    if (path === "/api/pair" && method === "POST") {
      return json({ ok: true, token: "offline", device_id: "dev_local", name: body.name || "Local" });
    }
    if (path === "/api/state") return json(statePayload());
    if (path === "/api/agents" && method === "GET") return json({ ok: true, agents });
    if (path === "/api/agents" && method === "POST") {
      const id = nid("agt");
      const tid = nid("thr");
      const agent = {
        id, name: body.name || "New agent",
        emoji: body.emoji || "blob|circle|#f08a5a|happy",
        avatar: body.avatar || "", kind: body.kind || "custom",
        subtitle: body.subtitle || "", system_prompt: body.system_prompt || "",
        backend: body.backend || settings.defaults.backend,
        model: body.model || settings.defaults.model,
        reasoning: body.reasoning || settings.defaults.reasoning,
        auto_approve: false, notify: true,
        members: body.members || [], rules: body.rules || "",
        routines: 0, thread_id: tid, preview: "", updated_at: now(),
        status: "idle", busy: false, working: [],
      };
      agents.unshift(agent);
      threads[tid] = {
        thread: {
          id: tid, agent_id: id, title: "", preview: "", status: "idle",
          archived: 0, created_at: now(), updated_at: now(),
          compacted_through: 0, hidden_count: 0,
        },
        messages: [], working: [], questions: [],
      };
      return json({ ok: true, agent });
    }

    const agentPatch = path.match(/^\/api\/agents\/([^/]+)$/);
    if (agentPatch && method === "PATCH") {
      const agent = agents.find((row) => row.id === agentPatch[1]);
      if (!agent) return json({ ok: false, error: "no such agent" }, 404);
      Object.assign(agent, body);
      return json({ ok: true, agent });
    }
    if (agentPatch && method === "DELETE") {
      const idx = agents.findIndex((row) => row.id === agentPatch[1]);
      if (idx >= 0) agents.splice(idx, 1);
      return json({ ok: true });
    }

    const agentThr = path.match(/^\/api\/agents\/([^/]+)\/thread$/);
    if (agentThr) {
      const payload = agentThread(agentThr[1]);
      return payload ? json(payload) : json({ ok: false, error: "no such agent" }, 404);
    }
    const newThr = path.match(/^\/api\/agents\/([^/]+)\/threads$/);
    if (newThr && method === "POST") {
      const agent = agents.find((row) => row.id === newThr[1]);
      if (!agent) return json({ ok: false, error: "no such agent" }, 404);
      const tid = nid("thr");
      agent.thread_id = tid;
      threads[tid] = {
        thread: {
          id: tid, agent_id: agent.id, title: "", preview: "", status: "idle",
          archived: 0, created_at: now(), updated_at: now(),
          compacted_through: 0, hidden_count: 0,
        },
        messages: [], working: [], questions: [],
      };
      return json({ ok: true, thread: threads[tid].thread });
    }

    const getThr = path.match(/^\/api\/threads\/([^/]+)$/);
    if (getThr && method === "GET") {
      const payload = threadPayload(getThr[1]);
      return payload ? json(payload) : json({ ok: false, error: "no such thread" }, 404);
    }
    const postMsg = path.match(/^\/api\/threads\/([^/]+)\/messages$/);
    if (postMsg && method === "POST") {
      const row = threads[postMsg[1]];
      if (!row) return json({ ok: false, error: "no such thread" }, 404);
      const message = msg(postMsg[1], "user", body.text || "", { attachments: body.attachments || [] }, 0, row.messages.length + 1);
      row.messages.push(message);
      row.thread.preview = body.text || "";
      row.thread.updated_at = now();
      setTimeout(() => { replyTo(postMsg[1], body.text || "").catch(() => {}); }, 80);
      return json({ ok: true, message });
    }
    const stopThr = path.match(/^\/api\/threads\/([^/]+)\/stop$/);
    if (stopThr && method === "POST") {
      const row = threads[stopThr[1]];
      if (row) row.thread.status = "idle";
      emit("thread.status", { status: "idle" }, stopThr[1]);
      return json({ ok: true, stopped: true, hard_cancel: true });
    }
    const clearThr = path.match(/^\/api\/threads\/([^/]+)\/clear$/);
    if (clearThr && method === "POST") {
      const row = threads[clearThr[1]];
      if (!row) return json({ ok: false, error: "no such thread" }, 404);
      row.messages = [];
      row.thread.preview = "";
      return json({ ok: true, thread: row.thread });
    }
    const watch = path.match(/^\/api\/threads\/([^/]+)\/watching$/);
    if (watch) return json({ ok: true });

    if (path === "/api/settings" && method === "GET") return json({ ok: true, settings });
    if (path === "/api/settings" && method === "PATCH") {
      Object.assign(settings, merge(settings, body));
      return json({ ok: true });
    }
    if (path === "/api/models") {
      return json({
        ok: true,
        backends: [
          { backend: "codex", ready: true, message: "offline mock" },
          { backend: "openrouter", ready: true, message: "offline mock" },
        ],
        codex_models: [
          { id: "gpt-5.6-luna", label: "GPT-5.6 Luna", reasoning: ["none", "low", "medium"], default_reasoning: "low" },
          { id: "gpt-5.6-terra", label: "GPT-5.6 Terra", reasoning: ["low", "medium", "high"], default_reasoning: "medium" },
          { id: "gpt-5.6-sol", label: "GPT-5.6 Sol", reasoning: ["low", "medium", "high", "xhigh"], default_reasoning: "medium" },
        ],
        openrouter_models: [
          { id: "openai/gpt-5.6-luna", label: "GPT-5.6 Luna", reasoning: ["none", "low", "medium"], default_reasoning: "low" },
        ],
      });
    }
    if (path === "/api/openrouter/balance") {
      return json({ ok: true, currency: "USD", balance: 12.34, total_credits: 20, total_usage: 7.66 });
    }
    if (path === "/api/prompt") {
      const wanted = parsed.searchParams.get("agent") || "agt_director";
      const agent = agents.find((row) => row.id === wanted) || agents[0];
      const sections = [
        { key: "base", label: "Base", text: "You are part of aiOS Director.", editable: "settings" },
        { key: "identity", label: "Identity", text: agent.system_prompt || `You are ${agent.name}.`, editable: "agent" },
        { key: "live", label: "Live state", text: "Windows PC is on. Operator display is :0.", editable: "live" },
      ];
      return json({
        ok: true,
        agent: { id: agent.id, name: agent.name, emoji: agent.emoji, kind: agent.kind },
        agents: agents.filter((row) => row.kind !== "group").map((row) => ({
          id: row.id, name: row.name, emoji: row.emoji,
        })),
        sections,
        prompt: sections.map((item) => item.text).join("\n\n"),
        blocks: {
          base: { label: "Base", default: "You are part of aiOS Director." },
          coordinator: { label: "Coordinator", default: "" },
          group: { label: "Group", default: "" },
        },
        overrides: settings.prompts,
        instructions: settings.instructions,
        tools: ["web_search", "shell", "operator", "code_session"],
      });
    }
    if (path === "/api/routines") {
      const agentId = parsed.searchParams.get("agent_id") || "";
      const rows = agentId ? routines.filter((row) => row.agent_id === agentId) : routines;
      return json({ ok: true, routines: rows });
    }
    if (path === "/api/push/key") return json({ ok: true, public_key: "" });
    if (path === "/api/push/test") return json({ ok: true, sent: 0 });
    if (path === "/api/push/subscribe" || path === "/api/push/unsubscribe") return json({ ok: true });
    if (path === "/api/jobs") return json({ ok: true, jobs: [] });
    if (path === "/api/events") return json({ ok: true, events: [] });
    if (path === "/api/machines") return json({ ok: true, machines });
    if (path === "/api/operator/status") return json({ ok: true, running: true });
    if (path === "/api/operator/screenshot") return json({ ok: true, image: "" });
    if (path === "/api/wake" || path === "/api/power/off") return json({ ok: true });
    if (path === "/api/voice/transcribe") return json({ ok: true, text: "offline voice note" });
    if (path.startsWith("/api/questions/")) return json({ ok: true });
    if (path.startsWith("/api/approvals/")) return json({ ok: true });
    const codeEventJob = path.match(/^\/api\/jobs\/([^/]+)\/code-events$/);
    if (codeEventJob) {
      return json({
        ok: true, job_id: codeEventJob[1], session_id: "session_phone_ui",
        reset: true, events: codeEvents, size: codeEvents.length,
      });
    }
    if (path.startsWith("/api/jobs/")) return json({ ok: true, job: { id: "job_x", status: "done" } });
    if (path.startsWith("/api/avatar/")) return json({ ok: false, error: "offline — no image gen" }, 400);

    return json({ ok: false, error: `offline mock has no ${method} ${path}` }, 404);
  }

  const realFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    const url = String(typeof input === "string" ? input : input.url || "");
    let path = url;
    try { path = new URL(url, location.origin).pathname; } catch {}
    if (path.startsWith("/api/") || url.includes("/api/")) {
      return route(url, init || {});
    }
    return realFetch(input, init);
  };

  window.WebSocket = FakeWebSocket;

  try {
    const existing = JSON.parse(localStorage.getItem("aios-director") || "{}");
    localStorage.setItem("aios-director", JSON.stringify({
      url: location.origin,
      token: existing.token || "offline",
      device: existing.device || "Local playground",
      agentId: existing.agentId || "agt_director",
      pinnedAgentId: existing.pinnedAgentId || "agt_director",
      appearance: existing.appearance || settings.appearance,
    }));
  } catch {
    localStorage.setItem("aios-director", JSON.stringify({
      url: location.origin, token: "offline", device: "Local playground",
      agentId: "agt_director", pinnedAgentId: "agt_director",
    }));
  }

  if (navigator.serviceWorker) {
    navigator.serviceWorker.getRegistrations?.().then((regs) => {
      regs.forEach((reg) => reg.unregister());
    }).catch(() => {});
    navigator.serviceWorker.register = async () => ({
      unregister: async () => true,
      pushManager: {
        getSubscription: async () => null,
        subscribe: async () => { throw new Error("offline playground"); },
      },
    });
  }

  const style = document.createElement("style");
  style.textContent = `
    .offline-tag {
      font-size: 10px; letter-spacing: .08em; text-transform: uppercase;
      color: #1c1d1f; background: #f0b45a; border-radius: 999px;
      padding: 2px 8px; margin-left: 8px; font-weight: 700;
    }
  `;
  document.head.append(style);
  const mark = () => {
    const brand = document.querySelector("#screen-agents .brand-row");
    if (!brand || brand.querySelector(".offline-tag")) return;
    const tag = document.createElement("span");
    tag.className = "offline-tag";
    tag.textContent = "offline";
    tag.title = "Local playground — not talking to Director";
    brand.append(tag);
  };
  document.addEventListener("DOMContentLoaded", mark);
  setTimeout(mark, 200);
  setTimeout(mark, 800);
})();
