/* aiOS coding page: continuous Claude / Codex sessions from the phone. */

const chatEl = document.getElementById('chat');
const titleEl = document.getElementById('title');
const subtitleEl = document.getElementById('subtitle');
const homeBtn = document.getElementById('home');
const ttsBtn = document.getElementById('tts');
const sessionsBtn = document.getElementById('sessions');
const micBtn = document.getElementById('mic');
const stopBtn = document.getElementById('stop');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('send');
const overlayEl = document.getElementById('overlay');
const overlayClose = document.getElementById('overlay-close');
const sessionListEl = document.getElementById('session-list');
const newClaudeBtn = document.getElementById('new-claude');
const newCodexBtn = document.getElementById('new-codex');
const projectPickerEl = document.getElementById('project-picker');
const projectListEl = document.getElementById('project-list');

const params = new URLSearchParams(window.location.search);
const queryBackend = params.get('backend');
if (queryBackend) localStorage.setItem('aiosBackendUrl', queryBackend.replace(/\/$/, ''));
const sameOriginBackend = !location.hostname.endsWith('.vercel.app');
let apiBase = (queryBackend || (sameOriginBackend ? location.origin : localStorage.getItem('aiosBackendUrl') || '')).replace(/\/$/, '');

let session = null;          // meta of current session
let logPos = 0;              // byte offset into events.jsonl
let eventSource = null;
let pollTimer = null;
let workingEl = null;
let ttsEnabled = localStorage.getItem('aiosTts') !== '0';
let recorder = null;
let micStream = null;
let chunks = [];
let mimeType = '';
let pendingCli = '';         // cli chosen in "new session" flow
let lastSpokenTs = 0;

updateTtsButton();
init();

function apiUrl(path) { return `${apiBase}${path}`; }

async function loadBackend() {
  if (queryBackend || sameOriginBackend) return;
  try {
    const res = await fetch('/backend.json', { cache: 'no-store' });
    if (!res.ok) return;
    const data = await res.json();
    if (data.backend) {
      apiBase = String(data.backend).replace(/\/$/, '');
      localStorage.setItem('aiosBackendUrl', apiBase);
    }
  } catch (_) { /* keep stored value */ }
}

async function init() {
  await loadBackend();
  homeBtn.addEventListener('click', goHome);
  ttsBtn.addEventListener('click', toggleTts);
  sessionsBtn.addEventListener('click', () => openOverlay());
  overlayClose.addEventListener('click', closeOverlay);
  overlayEl.addEventListener('click', (e) => { if (e.target === overlayEl) closeOverlay(); });
  sendBtn.addEventListener('click', sendFromInput);
  stopBtn.addEventListener('click', stopTurn);
  micBtn.addEventListener('click', () => {
    if (recorder && recorder.state === 'recording') stopRecording();
    else startRecording();
  });
  inputEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendFromInput(); }
  });
  inputEl.addEventListener('input', autoGrow);

  const sid = params.get('sid');
  const cli = (params.get('cli') || '').toLowerCase();
  if (sid) {
    await openSession(sid);
  } else {
    await openLatestOrCreate(cli === 'codex' ? 'codex' : 'claude');
  }
}

function goHome() {
  speechSynthesis.cancel();
  window.location.href = '/';
}

/* ---------- session bootstrap ---------- */

async function fetchSessions() {
  const res = await fetch(apiUrl('/api/phone/coding/sessions'), { cache: 'no-store' });
  const data = await res.json();
  return data.sessions || [];
}

async function openLatestOrCreate(cli) {
  try {
    const sessions = await fetchSessions();
    const latest = sessions.find((s) => s.cli === cli);
    if (latest) { await openSession(latest.id); return; }
    await createSession(cli, '');
  } catch (err) {
    renderHint(`Cannot reach the PC backend.\n${err.message || err}`);
  }
}

async function createSession(cli, project) {
  const res = await fetch(apiUrl('/api/phone/coding/sessions'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cli, project }),
  });
  const data = await res.json();
  if (!data.ok) { renderHint(data.error || 'Could not create session'); return; }
  await openSession(data.session.id);
}

async function openSession(sid) {
  closeOverlay();
  stopStreaming();
  chatEl.innerHTML = '';
  logPos = 0;
  workingEl = null;
  const res = await fetch(apiUrl(`/api/phone/coding/sessions/${sid}/log`), { cache: 'no-store' });
  if (!res.ok) { renderHint('Session not found.'); return; }
  const data = await res.json();
  session = data.meta;
  logPos = data.size || 0;
  lastSpokenTs = Date.now() / 1000; // don't re-speak history
  applyMeta();
  if (!data.events.length) {
    renderHint(`New ${session.cli} session in\n${session.project}\n\nTap the mic and start talking.`);
  } else {
    data.events.forEach((ev) => renderEvent(ev, false));
    scrollToBottom();
  }
  const url = new URL(window.location);
  url.searchParams.set('sid', sid);
  url.searchParams.delete('cli');
  history.replaceState(null, '', url);
  startStreaming();
}

function applyMeta() {
  if (!session) return;
  const name = session.cli === 'codex' ? 'Codex' : 'Claude';
  titleEl.innerHTML = '';
  titleEl.append(sessionTitle(session));
  const dot = document.createElement('span');
  dot.className = 'dot' + (session.status === 'running' ? ' running' : '');
  titleEl.append(dot);
  const color = session.cli === 'codex' ? 'var(--codex)' : 'var(--claude)';
  document.querySelectorAll('.msg.result').forEach((el) => { el.style.borderLeftColor = color; });
  subtitleEl.textContent = [name, session.project_name || projectFolder(session.project)].filter(Boolean).join(' · ');
  setWorking(session.status === 'running');
}

function projectFolder(value) {
  return String(value || '').replace(/[\\/]+$/, '').split(/[\\/]/).pop() || '';
}

function sessionTitle(value) {
  const explicit = String(value?.title || '').trim();
  if (explicit) return explicit;
  const summary = String(value?.last_summary || '').replace(/\s+/g, ' ').trim();
  if (summary) return summary.length > 64 ? `${summary.slice(0, 61)}…` : summary;
  const provider = value?.cli === 'codex' ? 'Codex' : 'Claude';
  const project = String(value?.project_name || projectFolder(value?.project)).trim();
  return project ? `${provider} in ${project}` : `${provider} session`;
}

/* ---------- streaming ---------- */

function startStreaming() {
  if (!session) return;
  stopStreaming();
  const url = apiUrl(`/api/phone/coding/sessions/${session.id}/events?since=${logPos}`);
  try {
    eventSource = new EventSource(url);
    eventSource.onmessage = (e) => {
      try {
        const ev = JSON.parse(e.data);
        if (ev.size) logPos = ev.size;
        renderEvent(ev, true);
      } catch (_) { /* ignore */ }
    };
    eventSource.addEventListener('reset', () => { logPos = 0; });
    eventSource.onerror = () => {
      // EventSource retries by itself; also poll as a safety net.
      startPolling();
    };
  } catch (_) {
    startPolling();
  }
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    if (!session) return;
    try {
      const res = await fetch(apiUrl(`/api/phone/coding/sessions/${session.id}/log?since=${logPos}`), { cache: 'no-store' });
      const data = await res.json();
      if (data.reset) { logPos = 0; return; }
      logPos = data.size || logPos;
      (data.events || []).forEach((ev) => renderEvent(ev, true));
      if (data.meta) { session = data.meta; applyMeta(); }
    } catch (_) { /* offline */ }
  }, 1500);
}

function stopStreaming() {
  if (eventSource) { eventSource.close(); eventSource = null; }
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

/* ---------- rendering ---------- */

function renderHint(text) {
  chatEl.innerHTML = '';
  const el = document.createElement('div');
  el.className = 'empty-hint';
  el.textContent = text;
  chatEl.appendChild(el);
}

function clearHint() {
  const hint = chatEl.querySelector('.empty-hint');
  if (hint) hint.remove();
}

function renderEvent(ev, live) {
  if (!ev || !ev.role) return;
  clearHint();
  const nearBottom = chatEl.scrollHeight - chatEl.scrollTop - chatEl.clientHeight < 140;
  switch (ev.role) {
    case 'user':
      addMsg('user', ev.text);
      break;
    case 'assistant':
      addRichMsg('assistant', ev.text);
      if (live) maybeSpeak(ev, true);
      break;
    case 'thinking':
      addMsg('thinking', ev.text);
      break;
    case 'tool':
      addMsg('tool', ev.text);
      break;
    case 'result':
      renderResult(ev);
      if (live) { setWorking(false); maybeSpeak(ev, false); refreshMeta(); }
      break;
    case 'error':
      addMsg('error', ev.text || 'error');
      if (live) { setWorking(false); refreshMeta(); }
      break;
    case 'status':
      if (ev.text === 'working') { if (live) setWorking(true); }
      else { addMsg('status', ev.text); if (live) setWorking(false); }
      break;
    default:
      addMsg('status', ev.text || ev.role);
  }
  if (nearBottom || live) scrollToBottom();
}

function addMsg(cls, text) {
  if (!text) return null;
  const el = document.createElement('div');
  el.className = `msg ${cls}`;
  el.textContent = text;
  chatEl.appendChild(el);
  return el;
}

function addRichMsg(cls, text) {
  if (!text) return null;
  const el = document.createElement('div');
  el.className = `msg ${cls}`;
  el.innerHTML = renderMarkdown(text);
  chatEl.appendChild(el);
  return el;
}

function renderResult(ev) {
  if (!ev.text && !ev.usage) return;
  const el = document.createElement('div');
  el.className = 'msg result';
  if (session && session.cli === 'codex') el.style.borderLeftColor = 'var(--codex)';
  const label = document.createElement('div');
  label.className = 'result-label';
  label.textContent = 'SUMMARY';
  el.appendChild(label);
  if (ev.text) {
    const body = document.createElement('div');
    body.innerHTML = renderMarkdown(ev.text);
    el.appendChild(body);
  }
  const metaBits = [];
  if (ev.duration_ms) metaBits.push(`${Math.round(ev.duration_ms / 1000)}s`);
  if (ev.cost_usd) metaBits.push(`$${ev.cost_usd}`);
  if (metaBits.length) {
    const meta = document.createElement('div');
    meta.className = 'result-meta';
    meta.textContent = metaBits.join(' · ');
    el.appendChild(meta);
  }
  if (!ev.text && !metaBits.length) return;
  chatEl.appendChild(el);
}

function renderMarkdown(text) {
  const escape = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const parts = String(text).split(/```/);
  let html = '';
  parts.forEach((part, i) => {
    if (i % 2 === 1) {
      const body = part.replace(/^[a-zA-Z0-9_-]*\n/, '');
      html += `<pre><code>${escape(body)}</code></pre>`;
    } else {
      let seg = escape(part);
      seg = seg.replace(/`([^`\n]+)`/g, '<code>$1</code>');
      seg = seg.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
      html += seg;
    }
  });
  return html;
}

function setWorking(on) {
  const dot = titleEl.querySelector('.dot');
  if (dot) dot.classList.toggle('running', !!on);
  stopBtn.hidden = !on;
  if (on) {
    if (!workingEl) {
      workingEl = document.createElement('div');
      workingEl.className = 'working';
      workingEl.innerHTML = '<i></i><i></i><i></i>';
    }
    chatEl.appendChild(workingEl); // keep it at the bottom
  } else if (workingEl && workingEl.parentNode) {
    workingEl.remove();
  }
}

function scrollToBottom() {
  chatEl.scrollTop = chatEl.scrollHeight;
}

async function refreshMeta() {
  if (!session) return;
  try {
    const res = await fetch(apiUrl(`/api/phone/coding/sessions/${session.id}`), { cache: 'no-store' });
    const data = await res.json();
    if (data.ok) { session = data.session; applyMeta(); }
  } catch (_) { /* ignore */ }
}

/* ---------- sending ---------- */

async function sendText(text) {
  text = String(text || '').trim();
  if (!text || !session) return;
  setWorking(true);
  try {
    const res = await fetch(apiUrl(`/api/phone/coding/sessions/${session.id}/send`), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    const data = await res.json();
    if (!data.ok) { addMsg('error', data.error || 'send failed'); setWorking(false); }
  } catch (err) {
    addMsg('error', 'Cannot reach the PC. Check the tunnel/backend.');
    setWorking(false);
  }
  scrollToBottom();
}

function sendFromInput() {
  const text = inputEl.value.trim();
  if (!text) return;
  inputEl.value = '';
  autoGrow();
  sendText(text);
}

async function stopTurn() {
  if (!session) return;
  try {
    await fetch(apiUrl(`/api/phone/coding/sessions/${session.id}/stop`), { method: 'POST' });
  } catch (_) { /* ignore */ }
}

function autoGrow() {
  inputEl.style.height = 'auto';
  inputEl.style.height = `${Math.min(inputEl.scrollHeight, 120)}px`;
}

/* ---------- voice input ---------- */

function chooseMimeType() {
  const options = ['audio/mp4', 'audio/aac', 'audio/webm;codecs=opus', 'audio/webm'];
  return options.find((t) => MediaRecorder.isTypeSupported(t)) || '';
}

async function startRecording() {
  if (!navigator.mediaDevices || !window.MediaRecorder) return;
  speechSynthesis.cancel();
  try {
    chunks = [];
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    mimeType = chooseMimeType();
    recorder = new MediaRecorder(micStream, mimeType ? { mimeType } : undefined);
    recorder.addEventListener('dataavailable', (e) => { if (e.data && e.data.size) chunks.push(e.data); });
    recorder.addEventListener('stop', uploadRecording);
    recorder.start();
    micBtn.classList.add('recording');
  } catch (_) {
    addMsg('error', 'Microphone blocked.');
  }
}

function stopRecording() {
  if (recorder && recorder.state === 'recording') recorder.stop();
}

async function uploadRecording() {
  micBtn.classList.remove('recording');
  if (micStream) { micStream.getTracks().forEach((t) => t.stop()); micStream = null; }
  recorder = null;
  if (!chunks.length) return;
  const blob = new Blob(chunks, { type: mimeType || 'audio/mp4' });
  chunks = [];
  const form = new FormData();
  form.append('audio', blob, `phone.${mimeType.includes('webm') ? 'webm' : 'mp4'}`);
  form.append('target', 'none');
  const pending = addMsg('status', 'transcribing…');
  try {
    const res = await fetch(apiUrl('/api/phone/transcribe'), { method: 'POST', body: form });
    const data = await res.json();
    if (pending) pending.remove();
    if (data.text) sendText(data.text);
  } catch (_) {
    if (pending) pending.remove();
    addMsg('error', 'Transcription failed.');
  }
}

/* ---------- TTS ---------- */

function updateTtsButton() {
  ttsBtn.classList.toggle('off', !ttsEnabled);
}

function toggleTts() {
  ttsEnabled = !ttsEnabled;
  localStorage.setItem('aiosTts', ttsEnabled ? '1' : '0');
  if (!ttsEnabled) speechSynthesis.cancel();
  updateTtsButton();
}

function stripForSpeech(text) {
  return String(text || '')
    .replace(/```[\s\S]*?```/g, ' Code block. ')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/[*_#>|]/g, ' ')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/https?:\/\/\S+/g, ' link ')
    .replace(/\s+/g, ' ')
    .trim();
}

function maybeSpeak(ev, isAssistant) {
  if (!ttsEnabled || !('speechSynthesis' in window)) return;
  // For Claude, the "result" event repeats the final assistant message —
  // prefer the result (it is the summary). For Codex, results are empty and
  // the assistant message carries the answer.
  if (isAssistant && session && session.cli === 'claude') return;
  const text = stripForSpeech(ev.text);
  if (!text || ev.ts <= lastSpokenTs) return;
  lastSpokenTs = ev.ts;
  speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(text.slice(0, 1600));
  utterance.rate = 1.05;
  speechSynthesis.speak(utterance);
}

/* ---------- sessions overlay ---------- */

async function openOverlay() {
  overlayEl.hidden = false;
  pendingCli = '';
  projectPickerEl.hidden = true;
  newClaudeBtn.classList.remove('selected');
  newCodexBtn.classList.remove('selected');
  newClaudeBtn.onclick = () => beginNewSession('claude');
  newCodexBtn.onclick = () => beginNewSession('codex');
  sessionListEl.innerHTML = '<div class="empty-hint">loading…</div>';
  try {
    const sessions = await fetchSessions();
    renderSessionList(sessions);
  } catch (_) {
    sessionListEl.innerHTML = '<div class="empty-hint">offline</div>';
  }
}

function closeOverlay() {
  overlayEl.hidden = true;
}

function renderSessionList(sessions) {
  sessionListEl.innerHTML = '';
  if (!sessions.length) {
    sessionListEl.innerHTML = '<div class="empty-hint">No sessions yet.</div>';
    return;
  }
  sessions.forEach((s) => {
    const item = document.createElement('div');
    item.className = 'session-item' + (session && s.id === session.id ? ' active' : '');
    const line1 = document.createElement('div');
    line1.className = 'session-line1';
    const badge = document.createElement('span');
    badge.className = `cli-badge ${s.cli}`;
    badge.textContent = s.cli.toUpperCase();
    const title = document.createElement('span');
    title.className = 'session-title';
    title.textContent = sessionTitle(s);
    const when = document.createElement('span');
    when.className = 'session-time';
    when.textContent = timeAgo(s.updated_at);
    line1.append(badge, title, when);
    const sub = document.createElement('div');
    sub.className = 'session-sub';
    sub.textContent = s.last_summary || s.project_name || s.project || '';
    const del = document.createElement('button');
    del.className = 'session-del';
    del.textContent = 'delete';
    del.addEventListener('click', async (e) => {
      e.stopPropagation();
      await fetch(apiUrl(`/api/phone/coding/sessions/${s.id}`), { method: 'DELETE' });
      item.remove();
    });
    item.append(line1, sub, del);
    item.addEventListener('click', () => openSession(s.id));
    sessionListEl.appendChild(item);
  });
}

async function beginNewSession(cli) {
  pendingCli = cli;
  newClaudeBtn.classList.toggle('selected', cli === 'claude');
  newCodexBtn.classList.toggle('selected', cli === 'codex');
  projectPickerEl.hidden = false;
  projectListEl.innerHTML = '<div class="empty-hint">loading…</div>';
  try {
    const res = await fetch(apiUrl('/api/phone/projects'), { cache: 'no-store' });
    const data = await res.json();
    projectListEl.innerHTML = '';
    const rootBtn = document.createElement('button');
    rootBtn.className = 'project-item';
    rootBtn.textContent = '📁 Projects root';
    rootBtn.addEventListener('click', () => createSession(pendingCli, ''));
    projectListEl.appendChild(rootBtn);
    (data.projects || []).forEach((name) => {
      const btn = document.createElement('button');
      btn.className = 'project-item';
      btn.textContent = name;
      btn.addEventListener('click', () => createSession(pendingCli, name));
      projectListEl.appendChild(btn);
    });
  } catch (_) {
    projectListEl.innerHTML = '<div class="empty-hint">cannot list projects</div>';
  }
}

function timeAgo(ts) {
  if (!ts) return '';
  const seconds = Math.max(0, Date.now() / 1000 - ts);
  if (seconds < 90) return 'now';
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}
