const stateEl = document.getElementById('state');
const textEl = document.getElementById('text');
const micBtn = document.getElementById('mic');
const sendBtn = document.getElementById('send');
const clearBtn = document.getElementById('clear');
const screenEl = document.getElementById('screen');
const screenWrap = document.querySelector('.screen');
const targetBtns = Array.from(document.querySelectorAll('.target-btn'));
const agentThreadEl = document.getElementById('agent-thread');
const agentStopBtn = document.getElementById('agent-stop');

const params = new URLSearchParams(window.location.search);
const queryBackend = params.get('backend');
if (queryBackend) localStorage.setItem('aiosBackendUrl', queryBackend.replace(/\/$/, ''));

const sameOriginBackend = !location.hostname.endsWith('.vercel.app');
let apiBase = (queryBackend || (sameOriginBackend ? location.origin : localStorage.getItem('aiosBackendUrl') || '')).replace(/\/$/, '');
let target = 'chat';
const PHONE_START_LABEL = { chat: 'AIOS', voice: 'AGENT', operator: 'OPERATOR' };
let recorder = null;
let stream = null;
let chunks = [];
let transcript = '';
let mimeType = '';
let screenTimer = null;
let heartbeatTimer = null;

targetBtns.forEach((btn) => {
  btn.addEventListener('click', () => {
    if (btn.dataset.cli) {
      openCoding(btn.dataset.cli);
      return;
    }
    target = btn.dataset.target;
    targetBtns.forEach((item) => {
      if (!item.dataset.cli) item.classList.toggle('active', item === btn);
    });
    applyTargetMode();
    phoneStart();
  });
});

// --- voice agent -----------------------------------------------------------
// The AGENT target talks to the same resident agent as the PC's dictation key,
// so the phone joins the conversation already running on the desktop.
let agentSource = null;
let agentSince = 0;
let agentReplyEl = null;

function applyTargetMode() {
  const isAgent = target === 'voice';
  agentThreadEl.hidden = !isAgent;
  agentStopBtn.hidden = !isAgent;
  screenWrap.classList.toggle('hidden', isAgent);
  if (isAgent) startAgentStream();
  else stopAgentStream();
}

function agentBubble(role, text) {
  const el = document.createElement('div');
  el.className = `agent-msg ${role}`;
  el.textContent = text;
  agentThreadEl.appendChild(el);
  while (agentThreadEl.children.length > 60) agentThreadEl.removeChild(agentThreadEl.firstChild);
  agentThreadEl.scrollTop = agentThreadEl.scrollHeight;
  return el;
}

async function loadAgentHistory() {
  if (!apiBase) return;
  try {
    const response = await fetch(apiUrl('/api/phone/voice/log'), { cache: 'no-store' });
    const data = await response.json();
    agentThreadEl.innerHTML = '';
    (data.events || []).forEach((event) => {
      if (event.type === 'turn_start') agentBubble('you', event.text || '');
      else if (event.type === 'turn_done') agentBubble(event.error ? 'error' : 'agent', event.text || '');
    });
    agentSince = Number(data.size) || 0;
  } catch (error) {
    /* the stream will catch up on its own */
  }
}

async function startAgentStream() {
  if (!apiBase || agentSource) return;
  await loadAgentHistory();
  agentSource = new EventSource(apiUrl(`/api/phone/voice/events?since=${agentSince}`));
  agentSource.addEventListener('reset', () => {
    agentSince = 0;
    loadAgentHistory();
  });
  agentSource.onmessage = (message) => {
    let event;
    try {
      event = JSON.parse(message.data);
    } catch (error) {
      return;
    }
    if (event.size) agentSince = event.size;
    handleAgentEvent(event);
  };
  agentSource.onerror = () => setState('reconnecting');
}

function handleAgentEvent(event) {
  switch (event.type) {
    case 'turn_start':
      agentReplyEl = null;
      agentBubble('you', event.text || '');
      setState('thinking');
      break;
    case 'status':
      if (event.text) setState(event.text);
      break;
    case 'tool_start':
    case 'tool_done':
      if (event.text) setState(event.text);
      break;
    case 'reply_start':
      agentReplyEl = agentBubble('agent', '');
      break;
    case 'reply_delta':
      if (!agentReplyEl) agentReplyEl = agentBubble('agent', '');
      agentReplyEl.textContent += event.text || '';
      agentThreadEl.scrollTop = agentThreadEl.scrollHeight;
      break;
    case 'turn_done':
      // The streamed bubble already holds the text; only fill in if it didn't stream.
      if (!agentReplyEl && event.text) agentBubble(event.error ? 'error' : 'agent', event.text);
      else if (agentReplyEl && event.error) agentReplyEl.classList.add('error');
      agentReplyEl = null;
      setState('ready');
      break;
    default:
      break;
  }
}

function stopAgentStream() {
  if (agentSource) {
    agentSource.close();
    agentSource = null;
  }
  agentReplyEl = null;
}

agentStopBtn.addEventListener('click', async () => {
  if (!apiBase) return;
  setState('stopping');
  try {
    await fetch(apiUrl('/api/phone/voice/stop'), { method: 'POST' });
    setState('stopped');
  } catch (error) {
    setState('offline');
  }
});

function openCoding(cli) {
  const suffix = sameOriginBackend || !apiBase ? '' : `&backend=${encodeURIComponent(apiBase)}`;
  window.location.href = `/coding?cli=${cli}${suffix}`;
}

function detectCodingCommand(text) {
  const match = /\b(?:start|open|launch|starta|öppna)\s+(claude|codex)\b/i.exec(text || '');
  return match ? match[1].toLowerCase() : '';
}

stateEl.addEventListener('click', setBackend);

clearBtn.addEventListener('click', () => {
  transcript = '';
  renderText();
  setState(apiBase ? 'ready' : 'backend');
});

sendBtn.addEventListener('click', sendTranscript);
micBtn.addEventListener('click', () => {
  if (recorder && recorder.state === 'recording') stopRecording();
  else startRecording();
});

window.addEventListener('pagehide', () => {
  if (apiBase) navigator.sendBeacon(apiUrl('/api/phone/stop'), new Blob(['{}'], { type: 'application/json' }));
});

init();

async function init() {
  await loadBackend();
  await checkStatus();
  applyTargetMode();
  startScreen();
  startHeartbeat();
}

function apiUrl(path) {
  return `${apiBase}${path}`;
}

async function fetchWithTimeout(url, options = {}, timeoutMs = 45000) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    window.clearTimeout(timer);
  }
}

function setState(value) {
  stateEl.textContent = value;
}

async function loadBackend() {
  if (queryBackend || sameOriginBackend) return;
  try {
    const response = await fetch('/backend.json', { cache: 'no-store' });
    if (!response.ok) return;
    const data = await response.json();
    if (data.backend) {
      apiBase = String(data.backend).replace(/\/$/, '');
      localStorage.setItem('aiosBackendUrl', apiBase);
    }
  } catch (error) {
    setState('backend');
  }
}

function setBackend() {
  const value = window.prompt('aiOS backend URL', apiBase || 'https://');
  if (!value) return;
  apiBase = value.replace(/\/$/, '');
  localStorage.setItem('aiosBackendUrl', apiBase);
  checkStatus();
  startScreen();
  startHeartbeat();
}

function renderText() {
  textEl.textContent = transcript.trim();
  textEl.scrollTop = textEl.scrollHeight;
}

function chooseMimeType() {
  const options = [
    'audio/mp4',
    'audio/aac',
    'audio/webm;codecs=opus',
    'audio/webm',
  ];
  return options.find((type) => MediaRecorder.isTypeSupported(type)) || '';
}

async function startRecording() {
  if (!apiBase) {
    setBackend();
    if (!apiBase) {
      setState('backend');
      return;
    }
  }
  if (!navigator.mediaDevices || !window.MediaRecorder) {
    setState('no mic');
    return;
  }
  try {
    await phoneStart();
    chunks = [];
    stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    mimeType = chooseMimeType();
    recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    recorder.addEventListener('dataavailable', (event) => {
      if (event.data && event.data.size > 0) chunks.push(event.data);
    });
    recorder.addEventListener('stop', uploadRecording);
    recorder.start();
    micBtn.classList.add('recording');
    setState('listening');
  } catch (error) {
    setState('mic blocked');
  }
}

function stopRecording() {
  if (recorder && recorder.state === 'recording') {
    recorder.stop();
    setState('transcribing');
  }
}

function stopTracks() {
  if (stream) {
    stream.getTracks().forEach((track) => track.stop());
  }
  stream = null;
  recorder = null;
}

async function uploadRecording() {
  micBtn.classList.remove('recording');
  stopTracks();
  if (!chunks.length) {
    setState('empty');
    return;
  }
  const blob = new Blob(chunks, { type: mimeType || 'audio/mp4' });
  chunks = [];
  const form = new FormData();
  const ext = mimeType.includes('webm') ? 'webm' : 'mp4';
  form.append('audio', blob, `phone.${ext}`);
  form.append('target', 'none');
  try {
    const response = await fetchWithTimeout(apiUrl('/api/phone/transcribe'), { method: 'POST', body: form });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'failed');
    if (data.text) {
      const cli = detectCodingCommand(data.text);
      if (cli) {
        setState(`opening ${cli}`);
        openCoding(cli);
        return;
      }
      transcript = `${transcript} ${data.text}`.trim();
      renderText();
      setState('ready');
    } else {
      setState('empty');
    }
  } catch (error) {
    setState(error.name === 'AbortError' ? 'timeout' : 'error');
  }
}

async function sendTranscript() {
  const text = transcript.trim();
  if (!apiBase) {
    setBackend();
    if (!apiBase) {
      setState('backend');
      return;
    }
  }
  if (!text) {
    setState('empty');
    return;
  }
  setState('sending');
  try {
    await phoneStart();
    const response = await fetch(apiUrl('/api/phone/send'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target, text }),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || 'failed');
    if (target === 'voice') {
      // The agent echoes the turn into the thread, so the compose box is done.
      transcript = '';
      renderText();
      setState('thinking');
    } else {
      setState('sent');
    }
  } catch (error) {
    setState('offline');
  }
}

async function checkStatus() {
  if (!apiBase) {
    setState('backend');
    return;
  }
  try {
    const response = await fetch(apiUrl('/api/phone/status'), { cache: 'no-store' });
    const data = await response.json();
    setState(data.helper ? 'ready' : 'offline');
  } catch (error) {
    setState('offline');
  }
}

async function phoneStart() {
  if (!apiBase) return;
  try {
    await fetch(apiUrl('/api/phone/start'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target: PHONE_START_LABEL[target] || 'AIOS' }),
    });
  } catch (error) {
    setState('offline');
  }
}

function startHeartbeat() {
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  if (!apiBase) return;
  phoneStart();
  heartbeatTimer = setInterval(phoneStart, 10000);
}

function startScreen() {
  if (screenTimer) clearInterval(screenTimer);
  if (!apiBase) return;
  const refresh = () => {
    screenEl.src = apiUrl(`/api/phone/screen?t=${Date.now()}`);
  };
  screenEl.onload = () => screenWrap.classList.add('live');
  screenEl.onerror = () => screenWrap.classList.remove('live');
  refresh();
  screenTimer = setInterval(refresh, 1200);
}
