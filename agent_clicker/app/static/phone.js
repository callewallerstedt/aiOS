const stateEl = document.getElementById('state');
const textEl = document.getElementById('text');
const micBtn = document.getElementById('mic');
const sendBtn = document.getElementById('send');
const clearBtn = document.getElementById('clear');
const screenEl = document.getElementById('screen');
const screenWrap = document.querySelector('.screen');
const targetBtns = Array.from(document.querySelectorAll('.target-btn'));

const params = new URLSearchParams(window.location.search);
const queryBackend = params.get('backend');
if (queryBackend) localStorage.setItem('aiosBackendUrl', queryBackend.replace(/\/$/, ''));

const sameOriginBackend = !location.hostname.endsWith('.vercel.app');
let apiBase = (queryBackend || (sameOriginBackend ? location.origin : localStorage.getItem('aiosBackendUrl') || '')).replace(/\/$/, '');
let target = 'chat';
let recorder = null;
let stream = null;
let chunks = [];
let transcript = '';
let mimeType = '';
let screenTimer = null;
let heartbeatTimer = null;

targetBtns.forEach((btn) => {
  btn.addEventListener('click', () => {
    target = btn.dataset.target;
    targetBtns.forEach((item) => item.classList.toggle('active', item === btn));
    phoneStart();
  });
});

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
  startScreen();
  startHeartbeat();
}

function apiUrl(path) {
  return `${apiBase}${path}`;
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
    const response = await fetch(apiUrl('/api/phone/transcribe'), { method: 'POST', body: form });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'failed');
    if (data.text) {
      transcript = `${transcript} ${data.text}`.trim();
      renderText();
      setState('ready');
    } else {
      setState('empty');
    }
  } catch (error) {
    setState('error');
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
    setState('sent');
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
      body: JSON.stringify({ target: target === 'chat' ? 'AIOS' : 'OPERATOR' }),
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
