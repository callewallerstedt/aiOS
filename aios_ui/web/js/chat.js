// The right-hand agent chat.
//
// This is a *view* of the agent that voice_dictation already runs, not a second
// brain. Type here, speak to the overlay, or message from the phone -- all three
// reach the same VoiceAgent with the same tools, so the conversation is
// continuous across them. The compact model controls below configure that one
// resident controller; the projects it delegates to CODE keep their own saved
// provider/model configurations.
//
// Rendering follows the CODE tab's rules: state in, one rAF writer out, so a
// burst of streamed deltas costs one layout instead of one per token.

import { api, stream } from "./bridge.js";
import { escapeHtml } from "./markdown.js";
import { autosizePromptShell, promptConfigRowMarkup, promptShellMarkup } from "./chat_components.js";
import { Transcript } from "./transcript.js";

const DRAFT_KEY = "aios:chat-draft";
const WIDTH_KEY = "aios:chat-width";

/** Tool labels the agent emits are sentence-shaped; keep them to one line. */
function toolLabel(event) {
  const label = String(event.text || event.tool || "tool").trim();
  return label.length > 90 ? `${label.slice(0, 87)}…` : label;
}

export class ChatPanel {
  constructor(root, shell) {
    this.root = root;
    this.shell = shell;
    this.since = 0;
    this.running = false;
    this.status = "";
    this.micState = "";
    this.pendingText = "";
    this.toolSerial = 0;
    this.openTools = new Map();
    this.recorder = null;
    this.micStream = null;
    this.audioChunks = [];
    // Saved dropdown preferences: [{id, label, show}]. Empty means "show all".
    this.chatPrefs = [];
    this.build();
    this.hydrate();
  }

  // --------------------------------------------------------------- structure

  build() {
    this.root.innerHTML = `
      <header class="chat-head">
        <span class="chat-title">AGENT</span>
        <span class="chat-state" id="chat-state"></span>
        <button class="chat-act" id="chat-stop" title="Stop this turn" hidden>Stop</button>
        <button class="chat-act" id="chat-reset" title="Forget the conversation">Reset</button>
      </header>
      <div class="code-transcript-wrap chat-log-wrap">
        <div class="code-transcript chat-log" id="chat-log"></div>
        <button class="scroll-bottom" id="chat-jump">Jump to latest &darr;</button>
      </div>
      <div class="code-composer chat-composer">
        <div class="prompt-menu prompt-plus-menu chat-plus-menu" id="chat-plus-menu" hidden>
          <button type="button" class="prompt-menu-row" id="chat-attach">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21.4 11.6 12 21a6 6 0 0 1-8.5-8.5l9.4-9.4a4 4 0 0 1 5.7 5.7l-9.4 9.4a2 2 0 0 1-2.8-2.8l8.7-8.7"></path></svg>
            <span><strong>Attach</strong><small>Add a file to this message</small></span>
          </button>
        </div>
        <div class="prompt-menu prompt-config-menu chat-model-menu" id="chat-model-menu" hidden>
          <div class="prompt-menu-label">Brain</div>
          <div id="chat-model-list"></div>
          <div class="prompt-menu-separator"></div>
          <div class="prompt-menu-label">Reasoning</div>
          <div id="chat-reasoning-list"></div>
          <div class="prompt-menu-separator"></div>
          <button type="button" class="prompt-menu-row" id="chat-models-edit">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"></path></svg>
            <span><strong>Edit models&hellip;</strong><small>Choose which models show here and their names</small></span>
          </button>
        </div>
        ${promptShellMarkup({
          shellAttr: 'id="chat-prompt-shell"',
          plusAttr: 'id="chat-plus"',
          inputAttr: 'id="chat-input" spellcheck="false"',
          configAttr: 'id="chat-model-btn" title="Model and reasoning for the resident voice and sidebar controller"',
          configNameAttr: 'id="chat-model-name"',
          dictateAttr: 'id="chat-mic"',
          sendAttr: 'id="chat-send"',
          placeholder: "Write a message&hellip;",
          configName: "Model",
        })}
      </div>
    `;
    this.log = this.root.querySelector("#chat-log");
    this.input = this.root.querySelector("#chat-input");
    this.stateEl = this.root.querySelector("#chat-state");
    this.stopBtn = this.root.querySelector("#chat-stop");
    this.micBtn = this.root.querySelector("#chat-mic");
    this.modelMenu = this.root.querySelector("#chat-model-menu");
    this.modelList = this.root.querySelector("#chat-model-list");
    this.reasoningList = this.root.querySelector("#chat-reasoning-list");
    this.modelBtn = this.root.querySelector("#chat-model-btn");
    this.modelName = this.root.querySelector("#chat-model-name");
    this.plusBtn = this.root.querySelector("#chat-plus");
    this.plusMenu = this.root.querySelector("#chat-plus-menu");
    this.jumpBtn = this.root.querySelector("#chat-jump");
    this.view = new Transcript(this.log, {
      jump: this.jumpBtn,
      isActive: () => this.running,
    });
    this.jumpBtn.addEventListener("click", () => this.view.setFollow(true));

    this.root.querySelector("#chat-send").addEventListener("click", () => this.send());
    this.input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        this.send();
      }
    });
    this.input.addEventListener("input", () => {
      this.grow();
      localStorage.setItem(DRAFT_KEY, this.input.value);
    });
    this.stopBtn.addEventListener("click", () => api("/api/agent/stop", { method: "POST", body: {} }));
    this.root.querySelector("#chat-reset").addEventListener("click", () => this.reset());
    this.micBtn.addEventListener("click", () => this.toggleRecording());
    this.plusBtn.addEventListener("click", () => this.togglePlusMenu());
    this.root.querySelector("#chat-attach").addEventListener("click", () => {
      this.togglePlusMenu(false);
      this.shell.toast("File attachments are not wired up in this build yet.", "info");
    });

    // Model + reasoning live in one dropdown, built like the CODE composer's
    // configuration menu.
    this.modelBtn.addEventListener("click", () => this.toggleModelMenu());
    this.root.querySelector("#chat-models-edit").addEventListener("click", () => {
      this.toggleModelMenu(false);
      this.openModelEditor();
    });
    this.modelMenu.addEventListener("click", (event) => {
      const row = event.target.closest("[data-value]");
      if (!row) return;
      const patch = row.dataset.kind === "reasoning"
        ? { agent_reasoning: row.dataset.value }
        : { agent_model: row.dataset.value };
      this.saveAgentConfig(patch);
      this.toggleModelMenu(false);
    });
    document.addEventListener("click", (event) => {
      if ((!this.modelMenu.hidden || !this.plusMenu.hidden) && !event.target.closest(".chat-composer")) {
        this.toggleModelMenu(false);
        this.togglePlusMenu(false);
      }
    });

    // A half-typed message must survive a tab switch or a collapse.
    this.input.value = localStorage.getItem(DRAFT_KEY) || "";
    this.grow();

  }

  togglePlusMenu(force) {
    const show = force === undefined ? this.plusMenu.hidden : force;
    this.plusMenu.hidden = !show;
    this.plusBtn.setAttribute("aria-expanded", String(show));
    if (show) this.toggleModelMenu(false);
  }

  toggleModelMenu(force) {
    const show = force === undefined ? this.modelMenu.hidden : force;
    this.modelMenu.hidden = !show;
    this.modelBtn.setAttribute("aria-expanded", String(show));
    if (show) this.togglePlusMenu(false);
  }

  /** One dropdown listing both knobs, checked like the CODE config menu. */
  renderModelMenu() {
    const asItem = (raw) =>
      (typeof raw === "string" || typeof raw === "number")
        ? { id: String(raw), label: String(raw) }
        : (raw || {});
    const row = (item, kind, current) => {
      const value = String(item.id ?? item.value ?? "");
      const selected = value === current;
      return promptConfigRowMarkup({
        label: escapeHtml(String(item.label ?? item.id ?? item.value ?? "")),
        hint: item.hint ? escapeHtml(String(item.hint)) : "",
        selected,
        attrs: `data-kind="${kind}" data-value="${escapeHtml(value)}"`,
      });
    };
    const labelOf = (rows, value) => {
      for (const raw of rows || []) {
        const item = asItem(raw);
        if (String(item.id ?? item.value ?? "") === String(value)) return String(item.label ?? item.id ?? item.value ?? "");
      }
      return String(value);
    };
    // Saved preferences win when present: only ticked models, under their
    // custom names. With no preferences every available model shows as-is.
    const prefs = (this.chatPrefs || []).filter((pref) => pref && pref.show !== false);
    const source = prefs.length ? prefs : (this.agentModels || []);
    this.modelList.innerHTML = source.map((raw) => row(asItem(raw), "model", this.agentModel)).join("");
    this.reasoningList.innerHTML = (this.agentReasoning || []).map((raw) => row(asItem(raw), "reasoning", this.agentReasoningLevel)).join("");
    this.modelName.textContent = labelOf(source, this.agentModel);
  }

  /** Modal like CODE's model manager: pick what shows, name each entry. */
  async openModelEditor() {
    const got = await api("/api/settings/agent-chat-models");
    if (!got || !got.ok) {
      this.shell.toast((got && got.error) || "Could not load the model list.", "error");
      return;
    }
    const available = Array.isArray(got.available) ? got.available : [];
    const saved = new Map((got.models || []).map((pref) => [String(pref.id), pref]));
    const overlay = document.createElement("div");
    overlay.className = "chat-model-editor";
    overlay.innerHTML = `
      <div class="chat-model-editor-card" role="dialog" aria-modal="true" aria-label="Edit agent models">
        <header>
          <strong>Agent models</strong>
          <button type="button" class="chat-model-close" aria-label="Close">&times;</button>
        </header>
        <p class="chat-model-hint">Tick what shows in the dropdown and give each one a short name. The full id stays underneath.</p>
        <div class="chat-model-rows">
          ${available.map((item) => {
            const id = String(item.id ?? "");
            const pref = saved.get(id);
            const show = pref ? pref.show !== false : true;
            const label = pref ? pref.label : String(item.label ?? id);
            return `
            <label class="chat-model-row">
              <input type="checkbox" data-field="show" data-id="${escapeHtml(id)}"${show ? " checked" : ""}>
              <input type="text" data-field="label" data-id="${escapeHtml(id)}" value="${escapeHtml(label)}">
              <small>${escapeHtml(id)}</small>
            </label>`;
          }).join("")}
        </div>
        <footer>
          <button type="button" data-action="reset">Show all</button>
          <button type="button" data-action="save" class="chat-model-save">Save</button>
        </footer>
      </div>`;

    const close = () => overlay.remove();
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay || event.target.closest(".chat-model-close")) close();
    });
    overlay.querySelector('[data-action="reset"]').addEventListener("click", async () => {
      const result = await api("/api/settings/agent-chat-models", { method: "POST", body: { models: [] } });
      if (result && result.ok) {
        this.chatPrefs = [];
        this.renderModelMenu();
      }
      close();
    });
    overlay.querySelector('[data-action="save"]').addEventListener("click", async () => {
      const models = available.map((item) => {
        const id = String(item.id ?? "");
        const key = `[data-field="label"][data-id="${CSS.escape(id)}"]`;
        const tick = `[data-field="show"][data-id="${CSS.escape(id)}"]`;
        return {
          id,
          label: (overlay.querySelector(key) || {}).value?.trim() || id,
          show: Boolean(overlay.querySelector(tick) && overlay.querySelector(tick).checked),
        };
      });
      const result = await api("/api/settings/agent-chat-models", { method: "POST", body: { models } });
      if (!result || !result.ok) {
        this.shell.toast((result && result.error) || "Could not save the model list.", "error");
        return;
      }
      this.chatPrefs = result.models || [];
      this.renderModelMenu();
      close();
    });

    document.body.appendChild(overlay);
  }

  grow() {
    autosizePromptShell(this.root.querySelector("#chat-prompt-shell"), this.input);
  }

  // ------------------------------------------------------------------ wiring

  async hydrate() {
    const [history, prefs] = await Promise.all([
      api("/api/agent/log?since=0"),
      api("/api/settings/agent-chat-models"),
      this.loadAgentConfig(),
    ]);
    if (prefs && prefs.ok) {
      this.chatPrefs = Array.isArray(prefs.models) ? prefs.models : [];
      this.renderModelMenu();
    }
    if (history.ok) {
      for (const event of history.events || []) this.absorb(event);
      this.since = history.size || 0;
      this.running = Boolean(history.running);
    }
    this.setRunning(this.running, this.status || "The agent is working.");
    this.view.pinToEnd();
    this.connect();
  }

  async loadAgentConfig() {
    const meta = await api("/api/settings/meta");
    if (!meta || !meta.ok || this.destroyed) return;
    const voice = (this.shell.config && this.shell.config.voice_dictation) || {};
    const defaults = meta.voice_defaults || {};
    this.agentModels = meta.agent_models || [];
    this.agentReasoning = meta.agent_reasoning || [];
    this.agentModel = String(voice.agent_model || defaults.agent_model || "gpt-5.6-luna");
    this.agentReasoningLevel = String(voice.agent_reasoning || defaults.agent_reasoning || "low");
    this.renderModelMenu();
  }

  async saveAgentConfig(patch) {
    this.modelBtn.disabled = true;
    this.modelName.textContent = "Saving\u2026";
    const result = await api("/api/settings/voice", { method: "POST", body: { patch } });
    this.modelBtn.disabled = false;
    if (!result || result.ok === false) {
      this.shell.toast((result && result.error) || "Could not change the agent model.", "error");
      this.renderModelMenu();
      return;
    }
    const voice = result.voice_dictation || {};
    this.shell.config.voice_dictation = voice;
    if (patch.agent_model !== undefined) {
      this.agentModel = String(voice.agent_model || patch.agent_model);
    }
    if (patch.agent_reasoning !== undefined) {
      this.agentReasoningLevel = String(voice.agent_reasoning || patch.agent_reasoning);
    }
    this.renderModelMenu();
  }

  connect() {
    this.close();
    this.source = stream(() => `/sse/agent/events?since=${this.since}`, {
      reset: () => {
        this.since = 0;
        this.resetTranscript();
      },
      events: (payload) => {
        for (const event of payload.events || []) this.absorb(event);
        this.since = payload.size || this.since;
      },
      state: (payload) => {
        this.running = Boolean(payload.running);
        this.setRunning(this.running, this.status || "The agent is working.");
      },
    });
  }

  close() {
    if (this.source) this.source.close();
    this.source = null;
  }

  destroy() {
    this.destroyed = true;
    this.stopMicrophone();
    this.close();
    this.view?.destroy();
  }

  // ------------------------------------------------------------------- state

  /** Translate resident-agent events into the main CODE transcript protocol. */
  absorb(event) {
    const kind = String(event.type || "");
    const text = String(event.text || "");
    if (kind === "turn_start") {
      if (this.pendingText === text) this.pendingText = "";
      else if (text) this.view.push([{ kind: "user", text, ts: event.ts }]);
      this.status = "thinking";
      this.setRunning(true, this.status);
      return;
    }
    if (kind === "status") {
      this.status = text || "thinking";
      this.setRunning(true, this.status);
      return;
    }
    if (kind === "tool_start") {
      this.status = text || "working";
      const tool = String(event.tool || "tool");
      const id = `agent-${tool}-${++this.toolSerial}`;
      const pending = this.openTools.get(tool) || [];
      pending.push(id);
      this.openTools.set(tool, pending);
      this.view.push([this.toolEvent(event, id, "started")]);
      this.setRunning(true, this.status);
      return;
    }
    if (kind === "tool_done") {
      const tool = String(event.tool || "tool");
      const pending = this.openTools.get(tool) || [];
      const id = pending.shift() || `agent-${tool}-${++this.toolSerial}`;
      if (pending.length) this.openTools.set(tool, pending);
      else this.openTools.delete(tool);
      this.view.push([this.toolEvent(event, id, event.ok === false ? "failed" : "completed")]);
      return;
    }
    if (kind === "reply_delta") {
      if (text) this.view.push([{ kind: "assistant_delta", delta: text, text }]);
      return;
    }
    if (kind === "turn_done") {
      if (text) this.view.push([{ kind: "result", text }]);
      this.status = "";
      this.setRunning(false);
    }
  }

  toolEvent(event, id, phase) {
    const tool = String(event.tool || "tool");
    const type = /update_plan|\bplan\b/i.test(tool) ? "plan"
      : /powershell|command|shell/i.test(tool) ? "command"
      : /code_start|delegate|agent/i.test(tool) ? "subagent"
        : /read|find|search|project|config|capabilit/i.test(tool) ? "search"
          : "tool";
    return {
      kind: "activity",
      activity_id: id,
      activity_type: type,
      phase,
      title: toolLabel(event),
      detail: String(event.text || ""),
      tool,
      ts: event.ts,
      error: phase === "failed" ? String(event.text || "Tool failed") : "",
      steps: Array.isArray(event.steps) ? event.steps : [],
    };
  }

  setRunning(active, detail = "") {
    this.running = Boolean(active);
    this.view.setWorking(this.running, detail || "The agent is working.");
    this.stopBtn.hidden = !this.running;
    this.stateEl.textContent = this.running ? "" : this.micState;
  }

  resetTranscript() {
    this.pendingText = "";
    this.openTools.clear();
    this.view.reset();
  }

  // ------------------------------------------------------------------ actions

  async send() {
    const text = this.input.value.trim();
    if (!text) return;
    this.input.value = "";
    localStorage.removeItem(DRAFT_KEY);
    this.grow();
    // Use the real transcript for the optimistic turn, then suppress its echo.
    this.pendingText = text;
    this.view.push([{ kind: "user", text, ts: Date.now() / 1000 }]);
    this.status = "thinking";
    this.setRunning(true, this.status);
    this.view.pinToEnd(300);
    // The selected model and reasoning ride along on every turn: in Director
    // mode the Linux brain never reads this PC's config, so without this the
    // sidebar dropdown is decorative.
    const result = await api("/api/agent/send", {
      method: "POST",
      body: { text, model: this.agentModel, reasoning: this.agentReasoningLevel },
    });
    if (!result.ok) {
      this.pendingText = "";
      this.setRunning(false);
      this.view.push([{ kind: "status", text: result.error || "Could not reach the agent." }]);
    }
  }

  chooseAudioType() {
    const choices = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm", "audio/aac"];
    return choices.find((type) => window.MediaRecorder?.isTypeSupported?.(type)) || "";
  }

  async toggleRecording() {
    if (this.recorder && this.recorder.state === "recording") {
      this.recorder.stop();
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      this.view.push([{ kind: "status", text: "Microphone recording is unavailable." }]);
      return;
    }
    try {
      this.audioChunks = [];
      this.micStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      const mimeType = this.chooseAudioType();
      this.recorder = new MediaRecorder(this.micStream, mimeType ? { mimeType } : undefined);
      this.recorder.addEventListener("dataavailable", (event) => {
        if (event.data && event.data.size) this.audioChunks.push(event.data);
      });
      this.recorder.addEventListener("stop", () => this.transcribeRecording(mimeType), { once: true });
      this.recorder.start();
      this.micBtn.classList.add("recording");
      this.micState = "listening";
      this.stateEl.textContent = this.micState;
    } catch (_error) {
      this.stopMicrophone();
      this.view.push([{ kind: "status", text: "Microphone permission was not granted." }]);
    }
  }

  stopMicrophone() {
    if (this.recorder?.state === "recording") this.recorder.stop();
    if (this.micStream) this.micStream.getTracks().forEach((track) => track.stop());
    this.micStream = null;
    this.micBtn?.classList.remove("recording", "transcribing");
  }

  async transcribeRecording(mimeType) {
    if (this.micStream) this.micStream.getTracks().forEach((track) => track.stop());
    this.micStream = null;
    this.recorder = null;
    this.micBtn.classList.remove("recording");
    if (this.destroyed) {
      this.audioChunks = [];
      return;
    }
    if (!this.audioChunks.length) return;
    const blob = new Blob(this.audioChunks, { type: mimeType || "audio/webm" });
    this.audioChunks = [];
    const form = new FormData();
    form.append("audio", blob, mimeType.includes("webm") ? "phone.webm" : "phone.mp4");
    form.append("target", "none");
    this.micBtn.classList.add("transcribing");
    this.micState = "transcribing";
    this.stateEl.textContent = this.micState;
    try {
      const response = await fetch("http://127.0.0.1:5000/api/phone/transcribe", {
        method: "POST",
        body: form,
      });
      const result = await response.json();
      if (!response.ok || !result.text) throw new Error(result.error || "No speech heard");
      this.input.value = result.text;
      this.grow();
      await this.send();
    } catch (error) {
      this.view.push([{ kind: "status", text: `Transcription failed: ${String(error.message || error)}` }]);
    } finally {
      this.micBtn.classList.remove("transcribing");
      this.micState = "";
      this.stateEl.textContent = "";
    }
  }

  async reset() {
    const sure = await this.shell.confirm(
      "Forget this conversation?",
      "The agent starts fresh and the shared event log is cleared, so the old chat will not reappear after a restart. Running CODE sessions are not affected.",
    );
    if (!sure) return;
    const result = await api("/api/agent/reset", { method: "POST", body: {} });
    // Reset the cursor along with the transcript: the backend truncated the
    // shared log, so anything we remembered by byte offset is gone anyway.
    this.since = 0;
    this.resetTranscript();
    this.status = "";
    this.setRunning(false);
    if (!result || result.ok === false) {
      this.shell.toast((result && result.error) || "Could not reset the agent.", "error");
    }
  }
}

/**
 * Drag the panel's left edge to resize it.
 *
 * The markup has had a .chat-resize handle since the shell was written; nothing
 * was ever bound to it.
 */
export function bindChatResize(panel, handle) {
  const stored = Number(localStorage.getItem(WIDTH_KEY));
  if (stored >= 240 && stored <= 720) panel.style.setProperty("--chat-w", `${stored}px`);

  handle.addEventListener("mousedown", (event) => {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = panel.getBoundingClientRect().width;
    let pending = startWidth;
    let frame = null;

    const onMove = (move) => {
      // Dragging left widens the panel: the handle is on its leading edge.
      pending = Math.max(240, Math.min(720, startWidth + (startX - move.clientX)));
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = null;
        panel.style.setProperty("--chat-w", `${Math.round(pending)}px`);
      });
    };
    const onUp = () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      localStorage.setItem(WIDTH_KEY, String(Math.round(pending)));
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  });
}
