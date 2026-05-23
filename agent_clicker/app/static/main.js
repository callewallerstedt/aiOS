const form = document.getElementById('form');
const modeSel = document.getElementById('mode-sel');
const cropLbl = document.getElementById('crop-lbl');
function syncCropLabel() {
  if (modeSel.value === 'raw') cropLbl.classList.remove('hidden');
  else cropLbl.classList.add('hidden');
}
modeSel.addEventListener('change', syncCropLabel);
syncCropLabel();
const statusEl = document.getElementById('status');
const roundsEl = document.getElementById('rounds');
const finalEl = document.getElementById('final');
const finalInfo = document.getElementById('final-info');
const finalImg = document.getElementById('final-img');
const finalCoords = document.getElementById('final-coords');

let es = null;
let startTime = 0;
let timerHandle = null;

const TOOL_COLORS = {
  ocr: '#ff5252',
  set_of_marks: '#ff8a52',
  grid: '#52d97a',
  crop: '#52d2ff',
  color_mask: '#f0c14b',
  find_icons: '#62a0ff',
  describe: '#cf94ff',
  sam3: '#ff52d9',
  commit: '#62e08a',
  commit_mark: '#62e08a',
};

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  roundsEl.innerHTML = '';
  finalEl.classList.add('hidden');
  statusEl.textContent = 'Uploading...';
  const btn = form.querySelector('button');
  btn.disabled = true;

  try {
    const fd = new FormData(form);
    const r = await fetch('/api/run', { method: 'POST', body: fd });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'upload failed');
    statusEl.textContent = `Running... (image ${j.image_size[0]}x${j.image_size[1]})`;
    startTime = Date.now();
    if (timerHandle) clearInterval(timerHandle);
    timerHandle = setInterval(() => {
      const t = ((Date.now() - startTime) / 1000).toFixed(1);
      const tEl = document.getElementById('total-timer');
      if (tEl) tEl.textContent = t + 's';
    }, 100);
    listen(j.job_id, () => { btn.disabled = false; if (timerHandle) clearInterval(timerHandle); });
  } catch (err) {
    statusEl.textContent = 'Error: ' + err.message;
    btn.disabled = false;
  }
});

function listen(jobId, onEnd) {
  if (es) es.close();
  es = new EventSource(`/api/stream/${jobId}`);

  es.onmessage = (msg) => {
    const ev = JSON.parse(msg.data);
    if (ev.type === 'start') {
      const modeTag = ev.mode === 'raw'
        ? `<span class="tag" style="background:#5a2a8a;color:#fff">RAW${ev.allow_crop ? ' +crop' : ''}</span>`
        : `<span class="tag" style="background:#1f3a5f;color:#9ec3ff">FULL</span>`;
      statusEl.innerHTML = `${modeTag} <b>Task:</b> "${escapeHtml(ev.task)}" &nbsp; <b>Model:</b> ${ev.model} &nbsp; <b>Image:</b> ${ev.image_size[0]}×${ev.image_size[1]} &nbsp; <b>Elapsed:</b> <span id="total-timer">0.0s</span>`;
    } else if (ev.type === 'thinking') {
      const card = ensureRound(ev.round);
      setTool(card, ev.tool);
      card.querySelector('.thought').textContent = ev.thought || '(no thought)';
      card.querySelector('pre.args').textContent = JSON.stringify(ev.args, null, 2);
      card.querySelector('.spin').classList.remove('hidden');
      statusEl.querySelector('b:nth-of-type(1)') && (document.title = `R${ev.round} ${ev.tool}…`);
      scrollToCard(card);
    } else if (ev.type === 'round') {
      const r = ev.round;
      const card = ensureRound(r.n);
      setTool(card, r.tool);
      card.querySelector('.meta').textContent = `Round ${r.n} · ${r.elapsed_ms} ms`;
      card.querySelector('.thought').textContent = r.thought || '';
      card.querySelector('pre.args').textContent = JSON.stringify(r.args, null, 2);
      card.querySelector('.summary').textContent = r.result_summary || '';
      card.querySelector('.spin').classList.add('hidden');
      // raw JSON viewer
      const raw = card.querySelector('pre.raw');
      raw.textContent = r.raw_response || '';
      if (r.error) {
        let e = card.querySelector('.error');
        if (!e) { e = document.createElement('div'); e.className = 'error'; card.appendChild(e); }
        e.textContent = r.error;
      }
      if (r.result_image_b64) {
        let wrap = card.querySelector('.img-wrap');
        if (!wrap) {
          wrap = document.createElement('div');
          wrap.className = 'img-wrap';
          const btn = document.createElement('button');
          btn.className = 'copy-btn';
          btn.textContent = '📋';
          btn.title = 'Copy image to clipboard';
          const img = document.createElement('img');
          img.className = 'result';
          btn.addEventListener('click', () => copyImage(img, btn));
          wrap.appendChild(btn);
          wrap.appendChild(img);
          card.appendChild(wrap);
        }
        wrap.querySelector('img').src = 'data:image/png;base64,' + r.result_image_b64;
      }
      if (r.tool === 'commit' || r.tool === 'commit_mark') card.classList.add('commit');
    } else if (ev.type === 'done') {
      if (ev.image_b64 !== undefined && ev.x !== undefined) {
        finalEl.classList.remove('hidden');
        finalCoords.textContent = `(${ev.x}, ${ev.y})`;
        finalInfo.innerHTML = `<b>Reason:</b> ${escapeHtml(ev.reason || '(none)')}`;
        finalImg.src = 'data:image/png;base64,' + ev.image_b64;
        const total = ((Date.now() - startTime) / 1000).toFixed(1);
        statusEl.innerHTML += ` &nbsp; <span class="ok">✓ Done in ${total}s</span>`;
        document.title = `Clicked (${ev.x}, ${ev.y})`;
        finalEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
      } else if (ev.error) {
        statusEl.innerHTML += ` <span class="err">✗ ${escapeHtml(ev.error)}</span>`;
      }
    } else if (ev.type === 'fatal') {
      statusEl.innerHTML += ` <span class="err">✗ Fatal: ${escapeHtml(ev.error)}</span>`;
    } else if (ev.type === 'end') {
      es.close(); es = null; onEnd && onEnd();
    }
  };
  es.onerror = () => { statusEl.innerHTML += ' <span class="err">stream closed</span>'; es && es.close(); es = null; onEnd && onEnd(); };
}

function setTool(card, tool) {
  const tagTool = card.querySelector('.tag.tool');
  tagTool.textContent = tool;
  tagTool.style.background = TOOL_COLORS[tool] || '#444';
  tagTool.style.color = '#0b0d12';
}

function ensureRound(n) {
  let card = document.getElementById('round-' + n);
  if (card) return card;
  card = document.createElement('div');
  card.className = 'round';
  card.id = 'round-' + n;
  card.innerHTML = `
    <h3>
      <span class="tag">#${n}</span>
      <span class="tag tool">…</span>
      <span class="spin">⏳</span>
    </h3>
    <div class="meta">Round ${n}</div>
    <div class="section-label">Thought</div>
    <div class="thought"></div>
    <div class="section-label">Tool args</div>
    <pre class="args"></pre>
    <div class="section-label">Tool result (summary)</div>
    <div class="summary"></div>
    <button class="copy-btn debug-btn" title="Copy this round's debug as text">📋 copy debug</button>
    <details>
      <summary>raw model JSON</summary>
      <pre class="raw"></pre>
    </details>
  `;
  roundsEl.appendChild(card);
  return card;
}

document.addEventListener('click', (e) => {
  const btn = e.target.closest('.round .debug-btn');
  if (!btn) return;
  const card = btn.closest('.round');
  const txt = [
    '=== ' + card.querySelector('h3').innerText.replace(/\s+/g, ' ') + ' ===',
    'meta: ' + card.querySelector('.meta').innerText,
    'thought: ' + card.querySelector('.thought').innerText,
    'args: ' + card.querySelector('pre.args').innerText,
    'summary: ' + card.querySelector('.summary').innerText,
    'raw: ' + (card.querySelector('pre.raw')?.innerText || ''),
    'error: ' + (card.querySelector('.error')?.innerText || ''),
  ].join('\n');
  copyText(txt, btn);
});

function scrollToCard(card) {
  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function copyImage(imgEl, btnEl) {
  const oldText = btnEl ? btnEl.textContent : null;
  try {
    const res = await fetch(imgEl.src);
    const blob = await res.blob();
    // Ensure png (clipboard requires image/png on most browsers)
    let pngBlob = blob;
    if (blob.type !== 'image/png') {
      const bmp = await createImageBitmap(blob);
      const canvas = document.createElement('canvas');
      canvas.width = bmp.width; canvas.height = bmp.height;
      canvas.getContext('2d').drawImage(bmp, 0, 0);
      pngBlob = await new Promise(r => canvas.toBlob(r, 'image/png'));
    }
    await navigator.clipboard.write([new ClipboardItem({ 'image/png': pngBlob })]);
    if (btnEl) { btnEl.textContent = '✓'; setTimeout(() => btnEl.textContent = oldText, 1200); }
  } catch (e) {
    if (btnEl) { btnEl.textContent = '✗'; setTimeout(() => btnEl.textContent = oldText, 1500); }
    console.error('copy failed', e);
  }
}

// wire up any static copy buttons (final image)
document.addEventListener('click', (e) => {
  const btn = e.target.closest('.copy-btn[data-copy-target]');
  if (!btn) return;
  const img = document.querySelector(btn.dataset.copyTarget);
  if (img) copyImage(img, btn);
});

function copyText(text, btnEl) {
  navigator.clipboard.writeText(text).then(() => {
    if (btnEl) {
      const o = btnEl.textContent;
      btnEl.textContent = '✓';
      setTimeout(() => btnEl.textContent = o, 1200);
    }
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
