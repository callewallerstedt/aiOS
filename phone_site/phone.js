const $ = (id) => document.getElementById(id);

const shellEl = document.querySelector('.shell');
const stateEl = $('state');
const stateText = $('state-text');
const textEl = $('text');
const charCount = $('char-count');
const micBtn = $('mic');
const sendBtn = $('send');
const clearBtn = $('clear');
const screenEl = $('screen');
const screenStage = $('screen-stage');
const screenWrap = $('screen-wrap');
const screenCollapseBtn = $('screen-collapse');
const screenCollapseIcon = $('screen-collapse-icon');
const monitorSelect = $('monitor-select');
const zoomBtn = $('screen-zoom');
const operatorPanel = $('operator-panel');
const opStatus = $('op-status');
const opStream = $('op-stream');
const opDisclose = $('op-disclose');
const opBody = $('op-body');
const codexUsage = $('codex-usage');

const opInputs = {
  model: $('op-model'),
  reasoning: $('op-reasoning'),
  steps: $('op-steps'),
  delay: $('op-delay'),
  monitor: $('op-monitor'),
  voice: $('op-voice'),
  tts: $('op-tts'),
  shell: $('op-shell'),
  codex_auth: $('op-codex'),
};

const settingsBtn = $('settings-btn');
const settingsModal = $('settings-modal');
const settingsBackend = $('settings-backend');
const settingsRefresh = $('settings-refresh');
const settingsQuality = $('settings-quality');
const settingsSize = $('settings-size');
const settingsStream = $('settings-stream');
const settingsAutosend = $('settings-autosend');
const settingsHaptics = $('settings-haptics');
const settingsSave = $('settings-save');
const settingsCancel = $('settings-cancel');

const zoomModal = $('zoom-modal');
const zoomStage = $('zoom-stage');
const zoomImg = $('zoom-img');
const zoomClose = $('zoom-close');
const zoomLoading = $('zoom-loading');

const params = new URLSearchParams(window.location.search);
const queryBackend = params.get('backend');
if (queryBackend) localStorage.setItem('aiosBackendUrl', queryBackend.replace(/\/$/, ''));

const sameOriginBackend = !location.hostname.endsWith('.vercel.app');

const prefs = loadPrefs();
let apiBase = (queryBackend || (sameOriginBackend ? location.origin : localStorage.getItem('aiosBackendUrl') || '')).replace(/\/$/, '');
let target = 'operator';
let recorder = null;
let stream = null;
let chunks = [];
let mimeType = '';
let screenTimer = null;
let heartbeatTimer = null;
let statusTimer = null;
let activeMonitor = Number(localStorage.getItem('aiosScreenMonitor') || 1);
let monitors = [];
let operatorConfig = null;
let operatorPanelOpen = localStorage.getItem('aiosOpPanel') !== 'closed';
let screenCollapsed = localStorage.getItem('aiosScreenCollapsed') === '1';
let evtSource = null;
let evtSeenSize = 0;
let operatorPollTimer = null;
let operatorStreamStarting = false;
let stepEntries = new Map(); // n -> element
let lastEntryEl = null;
let recordingStartedAt = 0;

function loadPrefs() {
  let raw = {};
  try { raw = JSON.parse(localStorage.getItem('aiosPrefs') || '{}'); } catch (_e) {}
  return {
    refreshMs: clamp(Number(raw.refreshMs) || 900, 400, 10000),
    quality: clamp(Number(raw.quality) || 78, 20, 95),
    maxSize: clamp(Number(raw.maxSize) || 1600, 400, 3840),
    stream: raw.stream !== false,
    autoSend: !!raw.autoSend,
    haptics: raw.haptics !== false,
  };
}
function savePrefs() { localStorage.setItem('aiosPrefs', JSON.stringify(prefs)); }
function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

function haptic(kind = 'light') {
  if (!prefs.haptics) return;
  if (!('vibrate' in navigator)) return;
  const map = { light: 8, med: 18, heavy: 28, success: [10, 30, 10], err: [30, 50, 30] };
  try { navigator.vibrate(map[kind] || 8); } catch (_e) {}
}

/* Operator-only mode */
operatorPanel.hidden = false;
ensureOperatorConfig();
phoneStart();

stateEl.addEventListener('click', () => openSettings());
clearBtn.addEventListener('click', () => { textEl.value = ''; updateCharCount(); setState(apiBase ? 'ready' : 'backend'); haptic('light'); });
sendBtn.addEventListener('click', () => { haptic('med'); sendTranscript(); });
micBtn.addEventListener('click', () => {
  if (recorder && recorder.state === 'recording') stopRecording();
  else startRecording();
});

textEl.addEventListener('input', () => { updateCharCount(); autoGrowText(); });
function updateCharCount() {
  const n = textEl.value.length;
  charCount.textContent = n ? `${n}` : '';
}
function autoGrowText() {
  textEl.style.height = 'auto';
  const maxPx = Math.round(window.innerHeight * 0.38);
  const target = Math.min(textEl.scrollHeight + 2, maxPx);
  textEl.style.height = Math.max(64, target) + 'px';
}

monitorSelect.addEventListener('change', () => {
  activeMonitor = Number(monitorSelect.value);
  if (!Number.isFinite(activeMonitor)) activeMonitor = 1;
  localStorage.setItem('aiosScreenMonitor', String(activeMonitor));
  startScreen(true);
  haptic('light');
});

zoomBtn.addEventListener('click', () => openHQScreen());
screenEl.addEventListener('click', () => { if (screenEl.src) openHQScreen(); });

async function openHQScreen() {
  if (!apiBase) return;
  haptic('light');
  // Open modal immediately with loading; show the low-res mirror as instant preview, then swap to HQ
  if (screenEl.src) openZoom(screenEl.src, { showLoading: true });
  else openZoom('', { showLoading: true });
  try {
    const url = `/api/phone/screen?monitor=${encodeURIComponent(activeMonitor)}&q=95&max=3840&t=${Date.now()}`;
    const r = await fetch(apiUrl(url));
    if (!r.ok) throw new Error('hq fetch failed');
    const blob = await r.blob();
    const objUrl = URL.createObjectURL(blob);
    swapZoomSrc(objUrl);
  } catch (_e) {
    zoomLoading.textContent = 'failed to capture';
  }
}

screenCollapseBtn.addEventListener('click', () => {
  screenCollapsed = !screenCollapsed;
  localStorage.setItem('aiosScreenCollapsed', screenCollapsed ? '1' : '0');
  applyScreenCollapsed();
  haptic('light');
});
function applyScreenCollapsed() {
  shellEl.classList.toggle('screen-collapsed', screenCollapsed);
  screenWrap.classList.toggle('collapsed', screenCollapsed);
  if (screenCollapseIcon) {
    screenCollapseIcon.innerHTML = screenCollapsed
      ? '<path fill="currentColor" d="M11 5h2v14h-2zM5 11h14v2H5z"/>'  // plus
      : '<path fill="currentColor" d="M5 11h14v2H5z"/>';                // minus
  }
  if (screenCollapsed) stopScreen();
  else startScreen(true);
}
applyScreenCollapsed();

textEl.addEventListener('focus', () => {
  setTimeout(() => textEl.scrollIntoView({ block: 'center', behavior: 'smooth' }), 250);
});

settingsBtn.addEventListener('click', openSettings);
settingsCancel.addEventListener('click', closeSettings);
settingsSave.addEventListener('click', () => {
  const val = settingsBackend.value.trim();
  if (val) {
    apiBase = val.replace(/\/$/, '');
    localStorage.setItem('aiosBackendUrl', apiBase);
  }
  prefs.refreshMs = clamp(Number(settingsRefresh.value) || 900, 400, 10000);
  prefs.quality = clamp(Number(settingsQuality.value) || 78, 20, 95);
  prefs.maxSize = clamp(Number(settingsSize.value) || 1600, 400, 3840);
  prefs.stream = !!settingsStream.checked;
  prefs.autoSend = !!settingsAutosend.checked;
  prefs.haptics = !!settingsHaptics.checked;
  savePrefs();
  closeSettings();
  checkStatus();
  loadMonitors();
  startScreen(true);
  startHeartbeat();
  startStatusRefresh();
  if (target === 'operator') startOperatorStream();
  haptic('success');
});
settingsModal.addEventListener('click', (e) => { if (e.target === settingsModal) closeSettings(); });

opDisclose.addEventListener('click', () => {
  operatorPanelOpen = !operatorPanelOpen;
  operatorPanel.classList.toggle('collapsed', !operatorPanelOpen);
  localStorage.setItem('aiosOpPanel', operatorPanelOpen ? 'open' : 'closed');
  haptic('light');
});
if (!operatorPanelOpen) operatorPanel.classList.add('collapsed');

$('op-save').addEventListener('click', () => { haptic('med'); saveOperatorConfig(); });
$('op-stop').addEventListener('click', () => { haptic('heavy'); stopOperator(); });

window.addEventListener('pagehide', () => {
  if (apiBase) navigator.sendBeacon(apiUrl('/api/phone/stop'), new Blob(['{}'], { type: 'application/json' }));
});

document.addEventListener('visibilitychange', () => {
  if (document.hidden) { stopScreen(); stopOperatorStream(); }
  else { startScreen(true); if (target === 'operator') startOperatorStream(); }
});

init();

async function init() {
  await loadBackend();
  await checkStatus();
  await loadMonitors();
  startScreen();
  startHeartbeat();
  startStatusRefresh();
  startOperatorStream();
  autoGrowText();
}

function apiUrl(path) { return `${apiBase}${path}`; }

function setState(value, kind = '') {
  stateText.textContent = value;
  stateEl.classList.remove('live', 'err', 'warn');
  if (kind) stateEl.classList.add(kind);
}

async function loadBackend() {
  if (queryBackend || sameOriginBackend) return;
  try {
    const r = await fetch('/backend.json', { cache: 'no-store' });
    if (!r.ok) return;
    const data = await r.json();
    if (data.backend) {
      apiBase = String(data.backend).replace(/\/$/, '');
      localStorage.setItem('aiosBackendUrl', apiBase);
    }
  } catch (_e) { setState('backend', 'err'); }
}

function openSettings() {
  settingsBackend.value = apiBase || '';
  settingsRefresh.value = prefs.refreshMs;
  settingsQuality.value = prefs.quality;
  settingsSize.value = prefs.maxSize;
  settingsStream.checked = prefs.stream;
  settingsAutosend.checked = prefs.autoSend;
  settingsHaptics.checked = prefs.haptics;
  settingsModal.hidden = false;
}
function closeSettings() { settingsModal.hidden = true; }

/* VOICE - faster: send opus webm, smaller chunks */
function chooseMimeType() {
  const options = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/aac'];
  return options.find((t) => MediaRecorder.isTypeSupported(t)) || '';
}

async function startRecording() {
  if (!apiBase) { openSettings(); return; }
  if (!navigator.mediaDevices || !window.MediaRecorder) { setState('no mic', 'err'); return; }
  try {
    haptic('med');
    await phoneStart();
    chunks = [];
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, sampleRate: 16000, channelCount: 1 },
    });
    mimeType = chooseMimeType();
    const opts = mimeType ? { mimeType, audioBitsPerSecond: 32000 } : undefined;
    recorder = new MediaRecorder(stream, opts);
    recorder.addEventListener('dataavailable', (e) => { if (e.data && e.data.size > 0) chunks.push(e.data); });
    recorder.addEventListener('stop', uploadRecording);
    recorder.start(250);
    recordingStartedAt = Date.now();
    micBtn.classList.add('recording');
    setState('listening', 'live');
  } catch (_e) {
    setState('mic blocked', 'err');
    haptic('err');
  }
}

function stopRecording() {
  if (recorder && recorder.state === 'recording') {
    haptic('light');
    if (Date.now() - recordingStartedAt < 220) {
      setTimeout(() => { try { recorder.stop(); } catch (_e) {} }, 240);
    } else {
      recorder.stop();
    }
    setState('transcribing', 'warn');
  }
}

function stopTracks() {
  if (stream) stream.getTracks().forEach((t) => t.stop());
  stream = null; recorder = null;
}

async function uploadRecording() {
  micBtn.classList.remove('recording');
  stopTracks();
  if (!chunks.length) { setState('empty'); return; }
  const blob = new Blob(chunks, { type: mimeType || 'audio/webm' });
  chunks = [];
  const form = new FormData();
  const ext = (mimeType || '').includes('webm') ? 'webm' : 'mp4';
  form.append('audio', blob, `phone.${ext}`);
  form.append('target', 'none');
  try {
    const r = await fetch(apiUrl('/api/phone/transcribe'), { method: 'POST', body: form });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || 'failed');
    if (data.text) {
      const existing = textEl.value.trim();
      textEl.value = existing ? `${existing} ${data.text}` : data.text;
      updateCharCount();
      setState('ready', 'live');
      haptic('success');
      if (prefs.autoSend) sendTranscript();
    } else {
      setState('empty');
    }
  } catch (_e) {
    setState('error', 'err');
    haptic('err');
  }
}

async function sendTranscript() {
  const text = textEl.value.trim();
  if (!apiBase) { openSettings(); return; }
  if (!text) { setState('empty'); return; }
  setState('sending', 'warn');
  const body = { target, text };
  if (target === 'operator') {
    body.options = collectOperatorOptions();
    opStatus.textContent = 'booting...';
    opStatus.classList.add('live');
    startOperatorStream();
    appendUserPrompt(text);
  }
  try {
    await phoneStart();
    const r = await fetch(apiUrl('/api/phone/send'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (!r.ok || !data.ok) throw new Error(data.error || 'failed');
    setState('sent', 'live');
    if (target === 'operator') {
      opStatus.textContent = 'booting…';
      opStatus.classList.add('live');
    }
  } catch (_e) {
    setState('offline', 'err');
    haptic('err');
  }
}

async function checkStatus() {
  if (!apiBase) { setState('backend', 'err'); return; }
  try {
    const r = await fetch(apiUrl('/api/phone/status'), { cache: 'no-store' });
    const data = await r.json();
    if (data.helper) setState('ready', 'live');
    else setState('offline', 'err');
    if (data.operator && !operatorConfig) {
      operatorConfig = data.operator;
      applyOperatorConfig(operatorConfig);
    }
    renderCodexUsage(data.codex_usage);
  } catch (_e) { setState('offline', 'err'); }
}

function renderCodexUsage(data) {
  if (!codexUsage || !data || !Array.isArray(data.accounts)) return;
  codexUsage.innerHTML = '';
  for (const account of data.accounts.slice(0, 2)) {
    const pill = document.createElement('div');
    pill.className = 'usage-pill' + (account.active ? ' active' : '');
    const name = escapeHtml(account.short || '--');
    const primary = account.primary ? `${account.primary.remaining}` : '--';
    const secondary = account.secondary ? `${account.secondary.remaining}` : '--';
    pill.innerHTML = `<b>${name}</b><span>5h ${primary}</span><span>w ${secondary}</span>`;
    codexUsage.appendChild(pill);
  }
}

async function loadMonitors(verbose = false) {
  if (!apiBase) return;
  monitorSelect.innerHTML = '<option value="1">scanning…</option>';
  try {
    const r = await fetch(apiUrl('/api/phone/monitors'), { cache: 'no-store' });
    if (!r.ok) {
      const msg = r.status === 404 ? 'old backend · restart server.py' : `monitors ${r.status}`;
      monitorSelect.innerHTML = `<option value="1">${escapeHtml(msg)}</option>`;
      setState(msg, 'err');
      return;
    }
    const data = await r.json();
    monitors = (data.monitors || []).filter((m) => m && Number.isFinite(m.width) && Number.isFinite(m.height));
    monitorSelect.innerHTML = '';
    opInputs.monitor.innerHTML = '<option value="">auto</option>';
    if (!monitors.length) {
      monitorSelect.innerHTML = '<option value="1">no monitors found</option>';
      setState('no monitors', 'err');
      return;
    }
    for (const m of monitors) {
      const o = document.createElement('option');
      o.value = String(m.index);
      o.textContent = `${m.name} · ${m.width}×${m.height}`;
      monitorSelect.appendChild(o);
      if (m.index === 0) continue;
      const o2 = document.createElement('option');
      o2.value = m.label;
      o2.textContent = `${m.name} · ${m.width}×${m.height}`;
      opInputs.monitor.appendChild(o2);
    }
    const exists = monitors.some((m) => m.index === activeMonitor);
    if (!exists) activeMonitor = (monitors.find((m) => m.index >= 1) || monitors[0]).index;
    monitorSelect.value = String(activeMonitor);
    const physical = monitors.filter((m) => m.index >= 1).length;
    if (verbose) setState(`found ${physical} monitor${physical === 1 ? '' : 's'}`, physical >= 1 ? 'live' : 'warn');
    if (operatorConfig) applyOperatorConfig(operatorConfig);
    startScreen(true);
  } catch (e) {
    monitorSelect.innerHTML = '<option value="1">monitor scan failed</option>';
    setState('monitors error', 'err');
  }
}

async function ensureOperatorConfig() {
  if (operatorConfig) return;
  if (!apiBase) return;
  try {
    const r = await fetch(apiUrl('/api/phone/operator/config'), { cache: 'no-store' });
    const data = await r.json();
    if (data.ok) { operatorConfig = data.operator; applyOperatorConfig(operatorConfig); }
  } catch (_e) {}
}

function applyOperatorConfig(cfg) {
  if (!cfg) return;
  if (cfg.model) opInputs.model.value = cfg.model;
  if (cfg.reasoning) opInputs.reasoning.value = cfg.reasoning;
  if (cfg.steps) opInputs.steps.value = cfg.steps;
  if (cfg.delay) opInputs.delay.value = cfg.delay;
  if (cfg.voice) opInputs.voice.value = cfg.voice;
  opInputs.tts.checked = !!cfg.tts;
  opInputs.shell.checked = !!cfg.shell;
  opInputs.codex_auth.checked = !!cfg.codex_auth;
  if (cfg.monitor) {
    const exists = Array.from(opInputs.monitor.options).some((o) => o.value === cfg.monitor);
    if (exists) opInputs.monitor.value = cfg.monitor;
  }
}

function collectOperatorOptions() {
  return {
    model: opInputs.model.value.trim(),
    reasoning: opInputs.reasoning.value,
    steps: opInputs.steps.value,
    delay: opInputs.delay.value,
    monitor: opInputs.monitor.value,
    voice: opInputs.voice.value,
    tts: opInputs.tts.checked,
    shell: opInputs.shell.checked,
    codex_auth: opInputs.codex_auth.checked,
  };
}

async function saveOperatorConfig() {
  if (!apiBase) return;
  opStatus.textContent = 'saving';
  try {
    const r = await fetch(apiUrl('/api/phone/operator/config'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ operator: collectOperatorOptions() }),
    });
    const data = await r.json();
    if (!r.ok || !data.ok) throw new Error(data.error || 'failed');
    operatorConfig = data.operator;
    opStatus.textContent = 'saved';
    setTimeout(() => { if (opStatus.textContent === 'saved') opStatus.textContent = 'idle'; }, 1400);
  } catch (_e) { opStatus.textContent = 'save failed'; }
}

async function stopOperator() {
  if (!apiBase) return;
  opStatus.textContent = 'stopping';
  try {
    const r = await fetch(apiUrl('/api/phone/operator/stop'), { method: 'POST' });
    const data = await r.json();
    opStatus.textContent = data.ok ? 'stop sent' : 'stop failed';
  } catch (_e) { opStatus.textContent = 'offline'; }
}

async function phoneStart() {
  if (!apiBase) return;
  try {
    await fetch(apiUrl('/api/phone/start'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target: target === 'chat' ? 'AIOS' : 'OPERATOR' }),
    });
  } catch (_e) { setState('offline', 'err'); }
}

function startHeartbeat() {
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  if (!apiBase) return;
  phoneStart();
  heartbeatTimer = setInterval(phoneStart, 10000);
}

function startStatusRefresh() {
  if (statusTimer) clearInterval(statusTimer);
  if (!apiBase) return;
  statusTimer = setInterval(checkStatus, 10000);
}

function stopScreen() { if (screenTimer) { clearInterval(screenTimer); screenTimer = null; } }

function startScreen(immediate = false) {
  stopScreen();
  if (!apiBase || !prefs.stream || screenCollapsed) return;
  const refresh = () => {
    const url = `/api/phone/screen?monitor=${encodeURIComponent(activeMonitor)}&q=${prefs.quality}&max=${prefs.maxSize}&t=${Date.now()}`;
    screenEl.src = apiUrl(url);
  };
  screenEl.onload = () => screenWrap.classList.add('live');
  screenEl.onerror = () => screenWrap.classList.remove('live');
  if (immediate || !screenEl.src) refresh();
  screenTimer = setInterval(refresh, prefs.refreshMs);
}

/* OPERATOR STREAM */
function startOperatorStream() {
  if (operatorStreamStarting) return;
  stopOperatorStream();
  if (!apiBase) return;
  operatorStreamStarting = true;
  // Seed initial log
  fetch(apiUrl('/api/phone/operator/log'), { cache: 'no-store' })
    .then((r) => r.json())
    .then((data) => {
      if (!data || !data.events) return;
      opStream.innerHTML = '';
      stepEntries.clear();
      lastEntryEl = null;
      for (const evt of data.events) renderEvent(evt);
      evtSeenSize = data.size || 0;
      openSSE();
      startOperatorLogPoll();
    })
    .catch(() => {
      openSSE();
      startOperatorLogPoll();
    })
    .finally(() => { operatorStreamStarting = false; });
}

function openSSE() {
  try {
    evtSource = new EventSource(apiUrl(`/api/phone/operator/events?since=${evtSeenSize}`));
    evtSource.onmessage = (e) => {
      try {
        const evt = JSON.parse(e.data);
        renderEvent(evt);
        if (evt.size) evtSeenSize = Math.max(evtSeenSize, Number(evt.size) || 0);
      } catch (_err) {}
    };
    evtSource.addEventListener('reset', () => {
      opStream.innerHTML = '';
      stepEntries.clear();
      lastEntryEl = null;
    });
    evtSource.onerror = () => { /* will auto-retry per SSE */ };
  } catch (_e) {}
}

function startOperatorLogPoll() {
  if (operatorPollTimer) clearInterval(operatorPollTimer);
  const poll = () => {
    if (!apiBase) return;
    fetch(apiUrl(`/api/phone/operator/log?since=${evtSeenSize}`), { cache: 'no-store' })
      .then((r) => r.json())
      .then((data) => {
        if (!data || !data.events) return;
        if (data.reset) {
          opStream.innerHTML = '';
          stepEntries.clear();
          lastEntryEl = null;
        }
        for (const evt of data.events) renderEvent(evt);
        evtSeenSize = data.size || evtSeenSize;
      })
      .catch(() => {});
  };
  operatorPollTimer = setInterval(poll, 800);
}

function stopOperatorStream() {
  if (evtSource) { try { evtSource.close(); } catch (_e) {} evtSource = null; }
  if (operatorPollTimer) { clearInterval(operatorPollTimer); operatorPollTimer = null; }
}

function fmtTs(ts) {
  if (!ts) return '';
  try {
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  } catch (_e) { return ''; }
}

function appendUserPrompt(text) {
  const el = document.createElement('div');
  el.className = 'entry';
  el.innerHTML = `<div class="row"><span class="badge" style="background:rgba(110,168,255,.18);color:#b9d3ff;">prompt</span><span class="ts">now</span></div><div class="body">${escapeHtml(text)}</div>`;
  opStream.appendChild(el);
  scrollOpToBottom();
}

function scrollOpToBottom() {
  requestAnimationFrame(() => { opStream.scrollTop = opStream.scrollHeight; });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function renderEvent(evt) {
  if (!evt || !evt.type) return;
  const ts = fmtTs(evt.ts);

  if (evt.type === 'run_start') {
    opStream.innerHTML = '';
    stepEntries.clear();
    lastEntryEl = null;
    const el = document.createElement('div');
    el.className = 'entry step';
    el.innerHTML = `<div class="row"><span class="badge">run</span><span class="ts">${ts}</span></div><div class="body">operator boot · waiting for first thought</div>`;
    opStream.appendChild(el);
    opStatus.textContent = 'running';
    opStatus.classList.add('live');
    scrollOpToBottom();
    return;
  }

  if (evt.type === 'step_begin') {
    const el = document.createElement('div');
    el.className = 'entry step';
    el.dataset.step = evt.n;
    el.innerHTML = `<div class="row"><span class="badge">step ${evt.n ?? '?'}</span><span class="ts">${ts}</span></div>`;
    opStream.appendChild(el);
    stepEntries.set(evt.n, el);
    lastEntryEl = el;
    scrollOpToBottom();
    return;
  }

  if (evt.type === 'screenshot') {
    if (!evt.frame) return;
    const wrap = document.createElement('div');
    wrap.className = 'frame-wrap';
    const img = document.createElement('img');
    img.loading = 'lazy';
    img.alt = 'operator frame';
    img.src = apiUrl(`/api/phone/operator/frame/${evt.frame}`);
    img.addEventListener('click', (e) => { e.stopPropagation(); openZoom(img.src, { showLoading: false }); });
    wrap.appendChild(img);
    const target = lastEntryEl || opStream.lastElementChild;
    if (target) target.appendChild(wrap);
    else {
      const el = document.createElement('div');
      el.className = 'entry step';
      el.innerHTML = `<div class="row"><span class="badge">frame</span><span class="ts">${ts}</span></div>`;
      el.appendChild(wrap);
      opStream.appendChild(el);
      lastEntryEl = el;
    }
    scrollOpToBottom();
    return;
  }

  if (evt.type === 'thought') {
    const el = document.createElement('div');
    el.className = 'entry thought';
    const thought = (evt.thought || '').trim();
    const say = (evt.say || '').trim();
    const meta = `plan: ${evt.actions || 0} action(s) · ${evt.elapsed_ms || 0}ms`;
    el.innerHTML = `<div class="row"><span class="badge">thinking</span><span class="ts">${ts}</span></div>
      <div class="body">${escapeHtml(thought) || '<em style="color:#5d667a">no thought</em>'}</div>
      ${say ? `<div class="body" style="color:#cdd2dc;font-style:italic;">say · ${escapeHtml(say)}</div>` : ''}
      <div class="body" style="font-size:11px;color:#5d667a;">${escapeHtml(meta)}</div>`;
    opStream.appendChild(el);
    lastEntryEl = el;
    scrollOpToBottom();
    return;
  }

  if (evt.type === 'action_done') {
    const el = document.createElement('div');
    el.className = 'entry action' + (evt.ok ? '' : ' fail');
    const detail = (evt.detail || '').trim();
    const ms = evt.elapsed_ms != null ? ` ${evt.elapsed_ms}ms` : '';
    el.innerHTML = `<div class="row"><span class="badge">${escapeHtml(evt.action || '?')}</span><span class="ts">${ts}</span></div>
      <div class="body">${evt.ok ? '✓' : '✗'} ${escapeHtml(detail)}${escapeHtml(ms)}</div>
      ${evt.output ? `<div class="output">${escapeHtml(evt.output)}</div>` : ''}`;
    opStream.appendChild(el);
    lastEntryEl = el;
    scrollOpToBottom();
    return;
  }

  if (evt.type === 'step_end') {
    return; // implicit; step header already added
  }

  if (evt.type === 'done') {
    const el = document.createElement('div');
    el.className = 'entry done' + (evt.ok ? '' : ' fail');
    el.innerHTML = `<div class="row"><span class="badge">done</span><span class="ts">${ts}</span></div>
      <div class="body">${evt.ok ? '✓' : '✗'} ${escapeHtml(evt.message || '')} · ${evt.steps || 0} step(s)</div>`;
    opStream.appendChild(el);
    opStatus.textContent = evt.ok ? 'done' : 'failed';
    opStatus.classList.remove('live');
    if (!evt.ok) opStatus.classList.add('err'); else opStatus.classList.remove('err');
    haptic(evt.ok ? 'success' : 'err');
    scrollOpToBottom();
    return;
  }

  if (evt.type === 'ask') {
    const el = document.createElement('div');
    el.className = 'entry ask';
    el.innerHTML = `<div class="row"><span class="badge">ask</span><span class="ts">${ts}</span></div>
      <div class="body">${escapeHtml(evt.message || '')}</div>`;
    opStream.appendChild(el);
    lastEntryEl = el;
    scrollOpToBottom();
    return;
  }

  if (evt.type === 'log') {
    const el = document.createElement('div');
    el.className = 'entry';
    el.innerHTML = `<div class="body" style="color:#8a92a3;font-size:12px;">${escapeHtml(evt.msg || '')}</div>`;
    opStream.appendChild(el);
    scrollOpToBottom();
    return;
  }
}

/* ZOOM modal with pinch/pan */
let zoomScale = 1, zoomX = 0, zoomY = 0;
let zoomFitScale = 1;
let pinchActive = false, pinchStartDist = 0, pinchStartScale = 1, pinchCx = 0, pinchCy = 0;
let panActive = false, panStartX = 0, panStartY = 0, panStartTx = 0, panStartTy = 0;
let lastTap = 0;
let zoomImgReady = false;

function openZoom(src, opts = {}) {
  zoomImgReady = false;
  zoomImg.style.transform = 'translate3d(0,0,0) scale(0)';
  zoomImg.removeAttribute('src');
  if (src) zoomImg.src = src;
  zoomModal.hidden = false;
  zoomLoading.hidden = !opts.showLoading || !!src && !opts.alsoLoading;
  if (opts.showLoading) zoomLoading.textContent = 'capturing high-res frame…';
  document.body.style.overflow = 'hidden';
  haptic('light');
}

function swapZoomSrc(src) {
  zoomImgReady = false;
  zoomImg.src = src;
}

function fitZoomToStage() {
  const sw = zoomStage.clientWidth, sh = zoomStage.clientHeight;
  const iw = zoomImg.naturalWidth, ih = zoomImg.naturalHeight;
  if (!iw || !ih || !sw || !sh) return;
  zoomFitScale = Math.min(sw / iw, sh / ih);
  zoomScale = zoomFitScale;
  zoomX = (sw - iw * zoomScale) / 2;
  zoomY = (sh - ih * zoomScale) / 2;
  applyZoom();
  zoomImgReady = true;
}

zoomImg.addEventListener('load', () => {
  fitZoomToStage();
  zoomLoading.hidden = true;
});

zoomImg.addEventListener('error', () => {
  zoomLoading.hidden = false;
  zoomLoading.textContent = 'failed to load';
});

function closeZoom() {
  zoomModal.hidden = true;
  document.body.style.overflow = '';
  panActive = false; pinchActive = false;
}
zoomClose.addEventListener('click', closeZoom);

function applyZoom() {
  zoomScale = clamp(zoomScale, zoomFitScale * 0.5, zoomFitScale * 24);
  zoomImg.style.transform = `translate3d(${zoomX}px, ${zoomY}px, 0) scale(${zoomScale})`;
}

/* Touch handling */
function touchList(e) {
  return Array.from(e.touches || []).map((t) => ({ x: t.clientX, y: t.clientY }));
}

zoomStage.addEventListener('touchstart', (e) => {
  if (!zoomImgReady) return;
  e.preventDefault();
  const pts = touchList(e);
  if (pts.length === 1) {
    panActive = true; pinchActive = false;
    panStartX = pts[0].x; panStartY = pts[0].y;
    panStartTx = zoomX; panStartTy = zoomY;
    const now = Date.now();
    if (now - lastTap < 280) { toggleDoubleZoom(pts[0].x, pts[0].y); lastTap = 0; panActive = false; }
    else lastTap = now;
  } else if (pts.length >= 2) {
    panActive = false; pinchActive = true;
    pinchStartDist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1;
    pinchStartScale = zoomScale;
    pinchCx = (pts[0].x + pts[1].x) / 2;
    pinchCy = (pts[0].y + pts[1].y) / 2;
  }
}, { passive: false });

zoomStage.addEventListener('touchmove', (e) => {
  if (!zoomImgReady) return;
  e.preventDefault();
  const pts = touchList(e);
  if (pinchActive && pts.length >= 2) {
    const dist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y) || 1;
    const cx = (pts[0].x + pts[1].x) / 2;
    const cy = (pts[0].y + pts[1].y) / 2;
    const newScale = pinchStartScale * (dist / pinchStartDist);
    const k = newScale / zoomScale;
    zoomX = cx - k * (cx - zoomX) + (cx - pinchCx);
    zoomY = cy - k * (cy - zoomY) + (cy - pinchCy);
    pinchCx = cx; pinchCy = cy;
    zoomScale = newScale;
    applyZoom();
  } else if (panActive && pts.length === 1) {
    zoomX = panStartTx + (pts[0].x - panStartX);
    zoomY = panStartTy + (pts[0].y - panStartY);
    applyZoom();
  }
}, { passive: false });

zoomStage.addEventListener('touchend', (e) => {
  if ((e.touches || []).length === 0) { panActive = false; pinchActive = false; }
}, { passive: false });

/* Mouse + wheel for desktop */
zoomStage.addEventListener('mousedown', (e) => {
  if (!zoomImgReady) return;
  panActive = true;
  panStartX = e.clientX; panStartY = e.clientY;
  panStartTx = zoomX; panStartTy = zoomY;
});
window.addEventListener('mousemove', (e) => {
  if (!panActive) return;
  zoomX = panStartTx + (e.clientX - panStartX);
  zoomY = panStartTy + (e.clientY - panStartY);
  applyZoom();
});
window.addEventListener('mouseup', () => { panActive = false; });

zoomStage.addEventListener('wheel', (e) => {
  if (!zoomImgReady) return;
  e.preventDefault();
  const dir = e.deltaY > 0 ? 0.88 : 1.135;
  const newScale = zoomScale * dir;
  const rect = zoomStage.getBoundingClientRect();
  const cx = e.clientX - rect.left, cy = e.clientY - rect.top;
  const k = newScale / zoomScale;
  zoomX = cx - k * (cx - zoomX);
  zoomY = cy - k * (cy - zoomY);
  zoomScale = newScale;
  applyZoom();
}, { passive: false });

function toggleDoubleZoom(cx, cy) {
  const rect = zoomStage.getBoundingClientRect();
  const x = cx - rect.left, y = cy - rect.top;
  const target = zoomScale > zoomFitScale * 1.05 ? zoomFitScale : zoomFitScale * 3;
  const k = target / zoomScale;
  zoomX = x - k * (x - zoomX);
  zoomY = y - k * (y - zoomY);
  zoomScale = target;
  applyZoom();
}
