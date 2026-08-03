/* Unified aiOS CODE dashboard: Codex, Claude Code, and Cursor Agent. */

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const queryBackend = params.get('backend');
if (queryBackend) localStorage.setItem('aiosBackendUrl', queryBackend.replace(/\/$/, ''));
const sameOriginBackend = !location.hostname.endsWith('.vercel.app');
const apiBase = (queryBackend || (sameOriginBackend ? location.origin : localStorage.getItem('aiosBackendUrl') || '')).replace(/\/$/, '');
const api = (path) => `${apiBase}${path}`;

let capabilities = { providers: [] };
let jobs = [];
let selectedId = params.get('job') || '';
let selectedJob = null;
let logSize = 0;
let eventSource = null;
let refreshTimer = null;
let followupAttachments = [];
let speaking = localStorage.getItem('aiosCodeSpeak') !== '0';
const activityNodes = new Map();
const activityState = new Map();
const activityTypes = new Map();
const activityFiles = new Set();
let currentActivityTitle = '';
let currentActivityId = '';
let legacyActivitySequence = 0;
let turnAssistantText = '';

document.addEventListener('DOMContentLoaded', init);

async function init() {
  $('new-job').addEventListener('click', openCreate);
  $('empty-new').addEventListener('click', openCreate);
  $('close-create').addEventListener('click', closeCreate);
  $('cancel-create').addEventListener('click', closeCreate);
  $('create-form').addEventListener('submit', launchJob);
  $('provider-picker').addEventListener('change', updateModelChoices);
  $('refresh').addEventListener('click', () => loadCapabilities(true));
  $('setup-agent').addEventListener('click', () => setupProvider(document.querySelector('input[name="provider"]:checked')?.value || 'codex'));
  $('filter').addEventListener('change', renderJobs);
  $('send').addEventListener('click', sendMessage);
  $('message').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); sendMessage(); }
  });
  $('message').addEventListener('input', autoGrow);
  $('stop').addEventListener('click', stopJob);
  $('delete').addEventListener('click', deleteJob);
  $('speak').addEventListener('click', toggleSpeak);
  $('followup-files').addEventListener('change', () => prepareFollowupFiles($('followup-files').files));
  $('create-files').addEventListener('change', () => {
    const count = $('create-files').files.length;
    $('create-file-label').textContent = count ? `${count} file${count === 1 ? '' : 's'} selected` : 'Choose files';
  });
  $('job-title').parentElement.addEventListener('click', () => {
    if (matchMedia('(max-width: 760px)').matches) clearSelection();
  });
  updateSpeakButton();
  await Promise.all([loadCapabilities(false), loadJobs()]);
  if (selectedId) await selectJob(selectedId);
  refreshTimer = setInterval(loadJobs, 2500);
  setInterval(updateRunStrip, 1000);
}

async function request(path, options = {}) {
  const response = await fetch(api(path), { cache: 'no-store', ...options });
  let data;
  try { data = await response.json(); } catch (_) { data = { ok: false, error: `HTTP ${response.status}` }; }
  if (!response.ok && !data.error) data.error = `HTTP ${response.status}`;
  return data;
}

async function loadCapabilities(force) {
  $('refresh').disabled = true;
  try {
    capabilities = await request(`/api/code/capabilities${force ? '?refresh=1' : ''}`);
    renderCapabilities();
    updateModelChoices();
  } catch (error) {
    $('provider-health').textContent = `Backend unavailable: ${error.message || error}`;
  } finally {
    $('refresh').disabled = false;
  }
}

function providerCapability(name) {
  return (capabilities.providers || []).find((item) => item.provider === name) || { provider: name, ready: false, message: 'Unavailable', models: [] };
}

function renderCapabilities() {
  const health = $('provider-health');
  health.replaceChildren();
  for (const provider of capabilities.providers || []) {
    const chip = document.createElement(provider.ready ? 'span' : 'button');
    chip.className = `health-chip ${provider.ready ? 'ready' : ''}`;
    chip.title = provider.message || '';
    if (!provider.ready) {
      chip.type = 'button';
      chip.setAttribute('aria-label', `Set up ${capital(provider.provider)}. ${provider.message || ''}`);
      chip.addEventListener('click', () => setupProvider(provider.provider));
    }
    const dot = document.createElement('i');
    chip.append(dot, document.createTextNode(capital(provider.provider)));
    health.appendChild(chip);
    const formHealth = document.querySelector(`[data-health="${provider.provider}"]`);
    if (formHealth) {
      formHealth.textContent = provider.ready ? 'Ready' : provider.message || 'Unavailable';
      formHealth.className = provider.ready ? 'ready' : 'blocked';
    }
  }
}

function updateModelChoices() {
  const provider = document.querySelector('input[name="provider"]:checked')?.value || 'codex';
  const info = providerCapability(provider);
  const modelSelect = $('model');
  const previous = modelSelect.value;
  modelSelect.replaceChildren();
  for (const model of info.models || []) {
    const option = document.createElement('option');
    option.value = model.id;
    option.textContent = model.label || model.id;
    option.dataset.reasoning = JSON.stringify(model.reasoning || []);
    option.dataset.defaultReasoning = model.default_reasoning || 'medium';
    option.dataset.fast = model.fast ? '1' : '0';
    if (model.default) option.selected = true;
    modelSelect.appendChild(option);
  }
  if (previous && [...modelSelect.options].some((o) => o.value === previous)) modelSelect.value = previous;
  if (!modelSelect.value && modelSelect.options.length) modelSelect.selectedIndex = 0;
  updateReasoningChoices();
  modelSelect.onchange = updateReasoningChoices;
  $('launch').disabled = !info.ready || !modelSelect.options.length;
  $('setup-agent').hidden = info.ready;
  $('setup-agent').textContent = `Sign in to ${capital(provider)}`;
  $('fast').disabled = modelSelect.selectedOptions[0]?.dataset.fast !== '1';
  if ($('fast').disabled) $('fast').checked = false;
}

async function setupProvider(provider) {
  const result = await request(`/api/code/providers/${encodeURIComponent(provider)}/setup`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  });
  if (!result.ok) {
    alert(result.error || `Could not open ${capital(provider)} sign-in.`);
    return;
  }
  alert(result.message || `${capital(provider)} sign-in opened. Refresh CODE when it is complete.`);
}

function updateReasoningChoices() {
  const option = $('model').selectedOptions[0];
  let efforts = [];
  try { efforts = JSON.parse(option?.dataset.reasoning || '[]'); } catch (_) { efforts = []; }
  const select = $('reasoning');
  const previous = select.value;
  select.replaceChildren();
  for (const effort of efforts.length ? efforts : ['medium']) {
    const row = document.createElement('option');
    row.value = effort;
    row.textContent = capital(effort);
    select.appendChild(row);
  }
  const wanted = option?.dataset.defaultReasoning || previous;
  if ([...select.options].some((o) => o.value === wanted)) select.value = wanted;
  $('fast').disabled = option?.dataset.fast !== '1';
  if ($('fast').disabled) $('fast').checked = false;
}

async function loadJobs() {
  try {
    const data = await request('/api/code/jobs?limit=250');
    if (!data.ok) return;
    jobs = data.jobs || [];
    renderSummary();
    renderJobs();
    if (selectedId) {
      const current = jobs.find((job) => job.id === selectedId);
      if (current) { selectedJob = current; renderJobHeader(); }
      await fetchNewEvents();
    }
  } catch (_) { /* transient offline; preserve current UI */ }
}

function renderSummary() {
  $('count-active').textContent = jobs.filter((j) => ['queued', 'running'].includes(j.status)).length;
  $('count-waiting').textContent = jobs.filter((j) => j.status === 'waiting_user').length;
  $('count-done').textContent = jobs.filter((j) => j.status === 'completed').length;
}

function matchesFilter(job, filter) {
  if (filter === 'all') return true;
  if (filter === 'active') return ['queued', 'running'].includes(job.status);
  return job.status === filter;
}

function renderJobs() {
  const list = $('job-list');
  list.replaceChildren();
  const visible = jobs.filter((job) => matchesFilter(job, $('filter').value));
  if (!visible.length) {
    const empty = document.createElement('div');
    empty.className = 'job-list-empty';
    empty.textContent = jobs.length ? 'No sessions in this view.' : 'No CODE sessions yet. Start one from here or ask the voice agent.';
    list.appendChild(empty);
    return;
  }
  for (const job of visible) {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = `job-card ${job.id === selectedId ? 'selected' : ''}`;
    card.addEventListener('click', () => selectJob(job.id));
    const dot = document.createElement('i');
    dot.className = `provider-dot ${job.provider}`;
    const copy = document.createElement('div');
    copy.className = 'job-copy';
    const title = document.createElement('b');
    title.textContent = job.title || `${capital(job.provider)} job`;
    const detail = document.createElement('span');
    detail.textContent = `${job.project_name || job.cwd || ''} · ${job.model || ''}`;
    copy.append(title, detail);
    const state = document.createElement('span');
    state.className = `job-state ${job.status}`;
    state.textContent = stateLabel(job.status);
    card.append(dot, copy, state);
    list.appendChild(card);
  }
}

async function selectJob(id) {
  selectedId = id;
  selectedJob = jobs.find((job) => job.id === id) || null;
  logSize = 0;
  followupAttachments = [];
  renderAttachmentList();
  resetActivityView();
  $('timeline').replaceChildren();
  $('empty-view').hidden = true;
  $('job-view').hidden = false;
  document.querySelector('.workspace').classList.add('has-selection');
  const url = new URL(location.href);
  url.searchParams.set('job', id);
  history.replaceState(null, '', url);
  renderJobs();
  renderJobHeader();
  const data = await request(`/api/code/jobs/${encodeURIComponent(id)}/log?since=0`);
  if (!data.ok) { clearSelection(); return; }
  selectedJob = data.job;
  logSize = data.size || 0;
  for (const event of data.events || []) appendEvent(event, false);
  if (!(data.events || []).length) renderTimelineEmpty();
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const timeline = $('timeline');
    timeline.scrollTop = timeline.scrollHeight;
  }));
  renderJobHeader();
  startEvents();
}

function clearSelection() {
  stopEvents();
  resetActivityView();
  selectedId = '';
  selectedJob = null;
  $('job-view').hidden = true;
  $('empty-view').hidden = false;
  document.querySelector('.workspace').classList.remove('has-selection');
  const url = new URL(location.href);
  url.searchParams.delete('job');
  history.replaceState(null, '', url);
  renderJobs();
}

function renderJobHeader() {
  if (!selectedJob) return;
  $('job-provider').textContent = capital(selectedJob.provider);
  $('job-provider-dot').className = `provider-dot ${selectedJob.provider}`;
  $('job-title').textContent = selectedJob.title || `${capital(selectedJob.provider)} job`;
  $('job-status').textContent = stateLabel(selectedJob.status);
  $('job-status').className = `status-pill ${selectedJob.status}`;
  $('job-meta').textContent = `${selectedJob.cwd || ''}  ·  ${selectedJob.model || ''} / ${selectedJob.reasoning || ''}${selectedJob.fast ? ' / fast' : ''}  ·  ${shortId(selectedJob.native_session_id)}`;
  const active = ['queued', 'running', 'waiting_user'].includes(selectedJob.status);
  $('stop').hidden = !active;
  $('delete').disabled = active;
  $('delete').title = active ? 'Stop this session before deleting it' : 'Delete session';
  const question = selectedJob.pending_question || '';
  $('pending-question').hidden = !question;
  $('pending-question').textContent = question ? `Agent question: ${question}` : '';
  updateRunStrip();
}

function renderTimelineEmpty() {
  if ($('timeline').children.length) return;
  const empty = document.createElement('div');
  empty.className = 'timeline-empty';
  empty.textContent = 'Waiting for the first provider event…';
  $('timeline').appendChild(empty);
}

function appendEvent(event, live = true) {
  if (!event || !event.kind) return;
  if (event.kind === 'assistant_delta') {
    event = { ...event, kind: 'assistant', text: event.delta ?? event.text ?? '' };
  }
  const timeline = $('timeline');
  const followOutput = live && timelineNearBottom(timeline);
  timeline.querySelector('.timeline-empty')?.remove();
  const text = event.text || event.kind;

  if (event.state && selectedJob) selectedJob.status = event.state;
  if (event.state && ['completed', 'failed', 'stopped', 'interrupted'].includes(event.state)) {
    finalizeActivities(event.state);
  }
  if (event.kind === 'provider_switch') {
    if (selectedJob) {
      selectedJob.provider = event.to_provider || selectedJob.provider;
      selectedJob.model = event.to_model || selectedJob.model;
      selectedJob.reasoning = event.to_reasoning || selectedJob.reasoning;
      selectedJob.fast = Boolean(event.to_fast);
      selectedJob.native_session_id = '';
      renderJobHeader();
    }
    const row = document.createElement('div');
    row.className = 'event provider-switch';
    const marker = document.createElement('span');
    marker.className = 'switch-marker';
    marker.textContent = 'HANDOFF';
    const copy = document.createElement('div');
    const title = document.createElement('b');
    title.textContent = text;
    const detail = document.createElement('span');
    detail.textContent = 'New native provider session · context and working tree transferred by aiOS';
    copy.append(title, detail);
    row.append(marker, copy);
    timeline.appendChild(row);
    scrollTimelineIfFollowing(timeline, followOutput);
    if (live && event.notify) notifyEvent(event);
    return;
  }
  if (event.kind === 'activity') {
    upsertActivity(event);
    scrollTimelineIfFollowing(timeline, followOutput);
    if (live && event.notify) notifyEvent(event);
    return;
  }
  if (['tool', 'thinking', 'approval'].includes(event.kind)) {
    appendLegacyActivity(event);
    scrollTimelineIfFollowing(timeline, followOutput);
    if (live && event.notify) notifyEvent(event);
    return;
  }

  if (event.kind === 'result') {
    if (live && event.notify) notifyEvent(event);
    const normalize = (value) => String(value || '').replace(/\s+/g, '');
    if (turnAssistantText && normalize(turnAssistantText) === normalize(text)) return;
    // Providers without streamed prose still get a normal assistant message,
    // never a second, specially labelled final-report card.
    event = { ...event, kind: 'assistant', notify: false };
  }

  if (event.kind === 'user') turnAssistantText = '';
  if (event.kind === 'assistant') turnAssistantText += text;

  // Codex emits agent text as word-sized deltas. Keep those deltas in one
  // growing message instead of turning every token into a separate row.
  const previous = timeline.lastElementChild;
  if (event.kind === 'assistant' && previous?.classList.contains('assistant')) {
    previous.dataset.markdown = `${previous.dataset.markdown || ''}${text}`;
    renderMarkdown(previous.querySelector('.markdown'), previous.dataset.markdown);
    scrollTimelineIfFollowing(timeline, followOutput);
    if (live && event.notify) notifyEvent(event);
    return;
  }

  const row = document.createElement('div');
  row.className = `event ${event.kind}`;
  if (['question', 'error'].includes(event.kind)) {
    const label = document.createElement('span');
    label.className = 'event-label';
    label.textContent = event.kind.toUpperCase();
    row.appendChild(label);
  }
  if (['assistant', 'result'].includes(event.kind)) {
    row.dataset.markdown = text;
    const markdown = document.createElement('div');
    markdown.className = 'markdown';
    renderMarkdown(markdown, text);
    row.appendChild(markdown);
  } else {
    row.appendChild(document.createTextNode(text));
  }
  timeline.appendChild(row);
  scrollTimelineIfFollowing(timeline, followOutput);
  if (live && event.notify) notifyEvent(event);
}

function timelineNearBottom(timeline) {
  return timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 4;
}

function scrollTimelineIfFollowing(timeline, follow) {
  if (follow) timeline.scrollTop = timeline.scrollHeight;
}

function appendInlineMarkdown(parent, source) {
  const text = String(source || '');
  const pattern = /(`[^`\n]+`|\[[^\]\n]+\]\([^)\s]+\)|\*\*[^*\n]+\*\*|__[^_\n]+__|(?<![\w*])\*[^*\n]+\*(?![\w*])|(?<![\w_])_[^_\n]+_(?![\w_]))/g;
  let cursor = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > cursor) parent.appendChild(document.createTextNode(text.slice(cursor, match.index)));
    const token = match[0];
    let node;
    if (token.startsWith('`')) {
      node = document.createElement('code');
      node.textContent = token.slice(1, -1);
    } else if (token.startsWith('[')) {
      const split = token.indexOf('](');
      const label = token.slice(1, split);
      const target = token.slice(split + 2, -1);
      node = document.createElement('a');
      node.textContent = label;
      try {
        const url = new URL(target, location.href);
        if (['http:', 'https:', 'file:'].includes(url.protocol)) {
          node.href = url.href;
          if (url.protocol !== 'file:') { node.target = '_blank'; node.rel = 'noopener noreferrer'; }
        }
      } catch (_) { /* render label without an unsafe destination */ }
    } else if (token.startsWith('**') || token.startsWith('__')) {
      node = document.createElement('strong');
      node.textContent = token.slice(2, -2);
    } else {
      node = document.createElement('em');
      node.textContent = token.slice(1, -1);
    }
    parent.appendChild(node);
    cursor = match.index + token.length;
  }
  if (cursor < text.length) parent.appendChild(document.createTextNode(text.slice(cursor)));
}

function markdownTableCells(line) {
  return String(line || '').trim().replace(/^\||\|$/g, '').split('|').map((cell) => cell.trim());
}

function isMarkdownTableDivider(line, columns) {
  const cells = markdownTableCells(line);
  return cells.length === columns && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

function renderMarkdown(container, source) {
  if (!container) return;
  container.replaceChildren();
  const lines = String(source || '').replace(/\r\n/g, '\n').split('\n');
  let index = 0;
  const appendBlock = (tag, text, className = '') => {
    const block = document.createElement(tag);
    if (className) block.className = className;
    appendInlineMarkdown(block, text);
    container.appendChild(block);
    return block;
  };
  while (index < lines.length) {
    const line = lines[index];
    const trimmed = line.trim();
    if (!trimmed) { index += 1; continue; }
    if (trimmed.startsWith('```')) {
      const language = trimmed.slice(3).trim();
      const code = [];
      index += 1;
      while (index < lines.length && !lines[index].trim().startsWith('```')) code.push(lines[index++]);
      if (index < lines.length) index += 1;
      const wrapper = document.createElement('div');
      wrapper.className = 'markdown-code';
      if (language) { const label = document.createElement('span'); label.textContent = language; wrapper.appendChild(label); }
      const pre = document.createElement('pre');
      const codeNode = document.createElement('code');
      codeNode.textContent = code.join('\n');
      pre.appendChild(codeNode); wrapper.appendChild(pre); container.appendChild(wrapper);
      continue;
    }
    if (trimmed.includes('|') && index + 1 < lines.length) {
      const header = markdownTableCells(trimmed);
      if (header.length >= 2 && isMarkdownTableDivider(lines[index + 1], header.length)) {
        const wrapper = document.createElement('div');
        wrapper.className = 'markdown-table-wrap';
        const table = document.createElement('table');
        const thead = document.createElement('thead');
        const headRow = document.createElement('tr');
        header.forEach((cell) => { const th = document.createElement('th'); appendInlineMarkdown(th, cell); headRow.appendChild(th); });
        thead.appendChild(headRow); table.appendChild(thead);
        const tbody = document.createElement('tbody');
        index += 2;
        while (index < lines.length && lines[index].trim() && lines[index].includes('|')) {
          const row = document.createElement('tr');
          const cells = markdownTableCells(lines[index]);
          header.forEach((_cell, column) => { const td = document.createElement('td'); appendInlineMarkdown(td, cells[column] || ''); row.appendChild(td); });
          tbody.appendChild(row); index += 1;
        }
        table.appendChild(tbody); wrapper.appendChild(table); container.appendChild(wrapper);
        continue;
      }
    }
    const heading = trimmed.match(/^(#{1,6})\s+(.*)$/);
    if (heading) { appendBlock(`h${Math.min(4, heading[1].length)}`, heading[2]); index += 1; continue; }
    if (/^([-*_])\s*\1\s*\1[-*_\s]*$/.test(trimmed)) { container.appendChild(document.createElement('hr')); index += 1; continue; }
    if (trimmed.startsWith('> ')) { appendBlock('blockquote', trimmed.slice(2)); index += 1; continue; }
    const listMatch = trimmed.match(/^([-*+]|\d+[.)])\s+(.*)$/);
    if (listMatch) {
      const ordered = /^\d/.test(listMatch[1]);
      const list = document.createElement(ordered ? 'ol' : 'ul');
      while (index < lines.length) {
        const current = lines[index].trim().match(/^([-*+]|\d+[.)])\s+(.*)$/);
        if (!current || /^\d/.test(current[1]) !== ordered) break;
        const item = document.createElement('li');
        const task = current[2].match(/^\[([ xX])\]\s+(.*)$/);
        if (task) {
          item.className = 'markdown-task';
          const box = document.createElement('span'); box.textContent = task[1].toLowerCase() === 'x' ? '☑' : '☐'; item.appendChild(box);
          appendInlineMarkdown(item, task[2]);
        } else appendInlineMarkdown(item, current[2]);
        list.appendChild(item); index += 1;
      }
      container.appendChild(list); continue;
    }
    const paragraph = [];
    while (index < lines.length && lines[index].trim()) {
      const candidate = lines[index].trim();
      if (paragraph.length && (/^#{1,6}\s/.test(candidate) || candidate.startsWith('```') || candidate.startsWith('> ') || /^([-*+]|\d+[.)])\s+/.test(candidate))) break;
      if (paragraph.length && candidate.includes('|') && index + 1 < lines.length && isMarkdownTableDivider(lines[index + 1], markdownTableCells(candidate).length)) break;
      paragraph.push(candidate); index += 1;
    }
    const p = document.createElement('p');
    paragraph.forEach((part, lineIndex) => { if (lineIndex) p.appendChild(document.createElement('br')); appendInlineMarkdown(p, part); });
    container.appendChild(p);
  }
}

function resetActivityView() {
  activityNodes.clear();
  activityState.clear();
  activityTypes.clear();
  activityFiles.clear();
  currentActivityTitle = '';
  currentActivityId = '';
  legacyActivitySequence = 0;
  turnAssistantText = '';
  updateActivitySummary();
}

// Providers narrate one tool call several times ("Run command", "Running…",
// "Ran command"). Fold those phrasings onto a single key so the card updates
// in place instead of stacking a new row per phase.
function legacyActivityKey(type, raw) {
  const normalized = String(raw || '')
    .replace(/^\$\s*/, '')
    .replace(/^(run|ran|running|check|checked|checking)\s+/i, '')
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase();
  // The narration changes the guessed type between phases, so only reasoning
  // is namespaced; everything else keys on the command text alone.
  return `${type === 'thinking' ? 'thinking' : 'work'}:${normalized.slice(0, 160) || ++legacyActivitySequence}`;
}

function appendLegacyActivity(event) {
  const raw = String(event.text || 'Working');
  let activityType = event.kind === 'thinking' ? 'thinking' : 'tool';
  if (event.tool === 'command' || raw.startsWith('$ ')) activityType = 'command';
  if (event.tool === 'files' || /^Edited\b/i.test(raw)) activityType = 'files';
  const title = activityType === 'thinking' ? 'Thought through the approach'
    : activityType === 'command' ? 'Ran command'
      : activityType === 'files' ? raw : event.kind === 'approval' ? 'Approved permission' : raw.split(':')[0];
  upsertActivity({
    kind: 'activity',
    activity_id: legacyActivityKey(activityType, raw),
    activity_type: activityType,
    phase: 'completed',
    title,
    detail: activityType === 'files' ? '' : raw,
    command: activityType === 'command' ? raw.replace(/^\$\s*/, '') : '',
    summary: activityType === 'thinking' ? raw : '',
    ts: event.ts,
  });
}

function upsertActivity(event) {
  const id = String(event.activity_id || `activity-${event.ts || Date.now()}-${++legacyActivitySequence}`);
  const previous = activityState.get(id) || {
    id, type: 'tool', phase: 'started', title: 'Working', detail: '', command: '', cwd: '',
    output: '', summary: '', diff: '', error: '', files: [], changes: [], steps: [], arguments: null,
  };
  const incomingType = String(event.activity_type || previous.type || 'tool');
  previous.type = incomingType === 'tool' && previous.type !== 'tool' ? previous.type : incomingType;
  previous.phase = normalizeActivityPhase(event.phase || previous.phase);
  previous.title = String(event.title || event.text || previous.title || 'Working');
  if (event.detail != null && String(event.detail)) previous.detail = String(event.detail);
  if (event.command != null && String(event.command)) previous.command = String(event.command);
  if (event.cwd != null && String(event.cwd)) previous.cwd = String(event.cwd);
  if (event.output != null && String(event.output)) previous.output = String(event.output);
  if (event.summary != null && String(event.summary)) previous.summary = String(event.summary);
  if (event.diff != null) previous.diff = String(event.diff || '');
  if (event.error != null && String(event.error)) previous.error = String(event.error);
  if (Array.isArray(event.files) && event.files.length) previous.files = event.files.map(String);
  if (Array.isArray(event.changes) && event.changes.length) {
    previous.changes = event.changes;
    if (!previous.files.length) previous.files = event.changes.map((change) => String(change.path || '')).filter(Boolean);
    if (!previous.diff) previous.diff = event.changes.map((change) => String(change.diff || '')).filter(Boolean).join('\n');
  }
  if (Array.isArray(event.steps)) previous.steps = event.steps;
  if (event.arguments && typeof event.arguments === 'object') previous.arguments = event.arguments;
  if (event.exit_code != null) previous.exitCode = event.exit_code;
  if (event.duration_ms != null) previous.durationMs = Number(event.duration_ms);
  if (event.elapsed_seconds != null) previous.elapsedSeconds = Number(event.elapsed_seconds);
  previous.ts = Number(event.ts || previous.ts || Date.now() / 1000);
  if (event.delta != null) {
    const delta = String(event.delta || '');
    if (event.stream === 'summary') previous.summary += delta;
    else if (event.stream === 'plan') previous.detail += delta;
    else previous.output += delta;
  }
  activityState.set(id, previous);
  activityTypes.set(id, previous.type);
  previous.files.forEach((path) => activityFiles.add(path));
  if (['started', 'update'].includes(previous.phase)) {
    currentActivityId = id;
    currentActivityTitle = previous.title;
  } else if (currentActivityId === id) {
    currentActivityId = '';
    currentActivityTitle = '';
  }
  renderActivity(previous);
  updateActivitySummary();
  updateRunStrip();
}

function renderActivity(activity) {
  let card = activityNodes.get(activity.id);
  const wasOpen = Boolean(card?.open);
  if (!card) {
    card = document.createElement('details');
    card.dataset.activityId = activity.id;
    activityNodes.set(activity.id, card);
    $('timeline').appendChild(card);
  }
  card.className = `activity-card ${activity.type} ${activity.phase}`;
  card.open = wasOpen;

  const summary = document.createElement('summary');
  const icon = document.createElement('span');
  icon.className = 'activity-icon';
  icon.setAttribute('aria-hidden', 'true');
  const copy = document.createElement('span');
  copy.className = 'activity-copy';
  const title = document.createElement('b');
  title.textContent = activity.title;
  const preview = document.createElement('span');
  preview.textContent = activityPreview(activity);
  copy.append(title, preview);
  const meta = document.createElement('span');
  meta.className = 'activity-meta';
  if (['started', 'update'].includes(activity.phase)) {
    const live = document.createElement('span');
    live.className = 'live-chip';
    live.textContent = 'live';
    meta.appendChild(live);
  }
  const phase = document.createElement('span');
  phase.className = 'activity-time';
  phase.textContent = activityMeta(activity);
  meta.appendChild(phase);
  const chevron = document.createElement('span');
  chevron.className = 'activity-chevron';
  chevron.textContent = '›';
  summary.append(icon, copy, meta, chevron);
  summary.addEventListener('click', (clickEvent) => {
    clickEvent.preventDefault();
    const timeline = $('timeline');
    const scrollTop = timeline.scrollTop;
    const nextOpen = !card.open;
    setTimeout(() => {
      card.open = nextOpen;
      const behavior = timeline.style.scrollBehavior;
      timeline.style.scrollBehavior = 'auto';
      timeline.scrollTop = scrollTop;
      timeline.style.scrollBehavior = behavior;
      requestAnimationFrame(() => { timeline.scrollTop = scrollTop; });
    }, 0);
  });

  const body = document.createElement('div');
  body.className = 'activity-body';
  let sections = 0;
  if (activity.command) {
    appendActivitySection(body, 'Command', activity.command, 'activity-code activity-command', activity.command);
    if (activity.cwd) {
      const cwd = document.createElement('div');
      cwd.className = 'activity-cwd';
      cwd.textContent = activity.cwd;
      body.lastElementChild.appendChild(cwd);
    }
    sections += 1;
  }
  if (activity.files.length) {
    const section = activitySection('Files');
    const chips = document.createElement('div');
    chips.className = 'file-chips';
    activity.files.forEach((path) => {
      const chip = document.createElement('span');
      chip.className = 'file-chip';
      chip.textContent = path;
      chips.appendChild(chip);
    });
    section.appendChild(chips);
    body.appendChild(section);
    sections += 1;
  }
  const readable = activity.summary || (activity.type === 'thinking' ? activity.detail : '');
  if (readable) {
    appendActivitySection(body, 'Reasoning summary', readable, 'activity-output', readable);
    sections += 1;
  }
  if (activity.output) {
    appendActivitySection(body, activity.phase === 'failed' ? 'Error output' : 'Output', activity.output, 'activity-output', activity.output);
    sections += 1;
  }
  if (activity.error && !activity.output.includes(activity.error)) {
    appendActivitySection(body, 'Error', activity.error, 'activity-output', activity.error);
    sections += 1;
  }
  if (activity.diff) {
    const section = activitySection('Diff', activity.diff);
    section.appendChild(renderDiff(activity.diff));
    body.appendChild(section);
    sections += 1;
  }
  if (activity.steps.length) {
    const section = activitySection('Plan');
    const list = document.createElement('ol');
    list.className = 'plan-list';
    activity.steps.forEach((step) => {
      const item = document.createElement('li');
      item.className = String(step.status || 'pending');
      item.textContent = String(step.step || step.text || step);
      list.appendChild(item);
    });
    section.appendChild(list);
    body.appendChild(section);
    sections += 1;
  }
  if (activity.arguments && !activity.command && !activity.files.length) {
    const args = JSON.stringify(activity.arguments, null, 2);
    appendActivitySection(body, 'Tool input', args, 'activity-code', args);
    sections += 1;
  }
  if (!sections) {
    const empty = document.createElement('div');
    empty.className = 'activity-empty';
    empty.textContent = ['started', 'update'].includes(activity.phase) ? 'Live details will appear here.' : 'No additional output.';
    body.appendChild(empty);
  }
  card.replaceChildren(summary, body);
}

function activitySection(label, copyText = '') {
  const section = document.createElement('section');
  section.className = 'activity-section';
  const head = document.createElement('div');
  head.className = 'activity-section-head';
  const text = document.createElement('span');
  text.textContent = label;
  head.appendChild(text);
  if (copyText) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = 'Copy';
    button.addEventListener('click', (clickEvent) => {
      clickEvent.preventDefault();
      clickEvent.stopPropagation();
      globalThis.navigator?.clipboard?.writeText?.(copyText);
      button.textContent = 'Copied';
      setTimeout(() => { button.textContent = 'Copy'; }, 1200);
    });
    head.appendChild(button);
  }
  section.appendChild(head);
  return section;
}

function appendActivitySection(body, label, value, className, copyText = '') {
  const section = activitySection(label, copyText);
  const pre = document.createElement('pre');
  pre.className = className;
  pre.textContent = value;
  section.appendChild(pre);
  body.appendChild(section);
}

function renderDiff(diff) {
  const pre = document.createElement('pre');
  pre.className = 'diff-output';
  const lines = String(diff || '').split('\n');
  const visible = lines.slice(0, 5000);
  visible.forEach((line) => {
    const span = document.createElement('span');
    span.className = `diff-line ${line.startsWith('+++') || line.startsWith('---') || line.startsWith('diff ') ? 'file' : line.startsWith('+') ? 'add' : line.startsWith('-') ? 'del' : line.startsWith('@@') ? 'hunk' : ''}`;
    span.textContent = `${line}\n`;
    pre.appendChild(span);
  });
  if (lines.length > visible.length) {
    const clipped = document.createElement('span');
    clipped.className = 'diff-line hunk';
    clipped.textContent = `… ${lines.length - visible.length} more lines\n`;
    pre.appendChild(clipped);
  }
  return pre;
}

function activityPreview(activity) {
  if (activity.command) return commandPreview(activity.command);
  if (activity.detail) return activity.detail;
  if (activity.files.length) return activity.files.map(fileName).slice(0, 3).join(', ');
  if (activity.type === 'thinking') return activity.summary || '';
  if (activity.type === 'diff') return '';
  if (activity.type === 'plan' && activity.steps.length) return `${activity.steps.length} step${activity.steps.length === 1 ? '' : 's'}`;
  const source = activity.output || activity.summary || activity.command || '';
  return String(source).split(/\r?\n/).map((line) => line.trim()).filter(Boolean).at(-1) || activityTypeLabel(activity.type);
}

function finalizeActivities(jobState) {
  activityState.forEach((activity) => {
    if (!['started', 'update'].includes(activity.phase)) return;
    activity.phase = jobState === 'failed' ? 'failed' : 'completed';
    if (activity.type === 'diff') activity.title = 'Changes ready';
    else if (activity.type === 'thinking') activity.title = 'Thought through the approach';
    else if (activity.type === 'plan') activity.title = 'Plan completed';
    renderActivity(activity);
  });
  currentActivityId = '';
  currentActivityTitle = '';
}

function commandPreview(command) {
  const text = String(command || '');
  const marker = text.match(/powershell(?:\.exe)?["']?\s+-Command\s+(.+)$/i);
  return marker ? marker[1].replace(/^['"]|['"]$/g, '') : text;
}

function activityMeta(activity) {
  if (Number.isFinite(activity.durationMs)) return `${formatDuration(activity.durationMs / 1000)} · ${activityTypeLabel(activity.type)}`;
  if (Number.isFinite(activity.elapsedSeconds)) return `${formatDuration(activity.elapsedSeconds)} · ${activityTypeLabel(activity.type)}`;
  if (activity.exitCode != null) return `exit ${activity.exitCode} · ${activityTypeLabel(activity.type)}`;
  return activityTypeLabel(activity.type);
}

function updateActivitySummary() {
  const counts = {};
  activityTypes.forEach((type) => { counts[type] = (counts[type] || 0) + 1; });
  const parts = [];
  if (counts.command) parts.push(`${counts.command} command${counts.command === 1 ? '' : 's'}`);
  if (activityFiles.size) parts.push(`${activityFiles.size} file${activityFiles.size === 1 ? '' : 's'}`);
  const other = [...activityTypes.values()].filter((type) => !['command', 'files', 'thinking', 'plan', 'diff'].includes(type)).length;
  if (other) parts.push(`${other} tool${other === 1 ? '' : 's'}`);
  const target = $('activity-summary');
  if (target) target.textContent = parts.join(' · ') || 'No activity yet';
}

function updateRunStrip() {
  const strip = $('run-strip');
  if (!strip || !selectedJob) return;
  const state = String(selectedJob.status || 'idle');
  strip.dataset.state = state;
  const provider = capital(selectedJob.provider || 'agent');
  const labels = {
    queued: `${provider} is queued`, running: `${provider} is working`, waiting_user: `${provider} needs your input`,
    completed: `${provider} finished`, failed: `${provider} hit an error`, stopped: `${provider} stopped`, interrupted: `${provider} was interrupted`,
  };
  $('run-label').textContent = labels[state] || `${provider} is ready`;
  const terminal = ['completed', 'failed', 'stopped', 'interrupted'].includes(state);
  $('run-detail').textContent = selectedJob.pending_question || (terminal ? selectedJob.last_summary : currentActivityTitle) || (state === 'completed' ? 'All requested work is complete' : 'Waiting for the next instruction');
  const start = Number(selectedJob.started_at || selectedJob.created_at || 0);
  const end = terminal ? Number(selectedJob.completed_at || selectedJob.updated_at || Date.now() / 1000) : Date.now() / 1000;
  $('run-elapsed').textContent = start ? formatDuration(Math.max(0, end - start)) : '0s';
}

function normalizeActivityPhase(value) {
  const text = String(value || '').toLowerCase().replace(/[^a-z]/g, '');
  if (['failed', 'error', 'declined', 'cancelled', 'canceled'].includes(text)) return 'failed';
  if (['completed', 'complete', 'success', 'succeeded', 'done'].includes(text)) return 'completed';
  if (['started', 'inprogress', 'running', 'pending'].includes(text)) return 'started';
  return text === 'update' ? 'update' : 'completed';
}

function activityTypeLabel(type) {
  return ({ command: 'terminal', files: 'file edit', thinking: 'reasoning', plan: 'plan', diff: 'diff', search: 'search', read: 'file read', web: 'web', tool: 'tool' })[type] || String(type || 'tool');
}

function fileName(path) { return String(path || '').split(/[\\/]/).filter(Boolean).at(-1) || String(path || 'file'); }
function formatDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return `${minutes}m ${rest}s`;
}

function startEvents() {
  stopEvents();
  if (!selectedId) return;
  try {
    eventSource = new EventSource(api(`/api/code/jobs/${encodeURIComponent(selectedId)}/events?since=${logSize}`));
    eventSource.onmessage = (message) => {
      try {
        const event = JSON.parse(message.data);
        logSize = event.size || logSize;
        appendEvent(event, true);
        loadSelectedMeta();
      } catch (_) { /* ignore malformed event */ }
    };
    eventSource.addEventListener('reset', () => { logSize = 0; });
  } catch (_) { eventSource = null; }
}

function stopEvents() {
  if (eventSource) eventSource.close();
  eventSource = null;
}

async function fetchNewEvents() {
  if (!selectedId) return;
  const data = await request(`/api/code/jobs/${encodeURIComponent(selectedId)}/log?since=${logSize}`);
  if (!data.ok) return;
  if (data.reset) { await selectJob(selectedId); return; }
  logSize = data.size || logSize;
  for (const event of data.events || []) appendEvent(event, true);
  if (data.job) { selectedJob = data.job; renderJobHeader(); }
}

async function loadSelectedMeta() {
  if (!selectedId) return;
  const data = await request(`/api/code/jobs/${encodeURIComponent(selectedId)}`);
  if (data.ok) { selectedJob = data.job; renderJobHeader(); }
}

function openCreate() {
  $('create-error').hidden = true;
  const requested = (params.get('provider') || '').toLowerCase();
  if (requested && document.querySelector(`input[name="provider"][value="${requested}"]`)) {
    document.querySelector(`input[name="provider"][value="${requested}"]`).checked = true;
  }
  updateModelChoices();
  $('create-dialog').showModal();
  setTimeout(() => $('brief').focus(), 60);
}

function closeCreate() { $('create-dialog').close(); }

async function uploadFiles(fileList) {
  if (!fileList || !fileList.length) return [];
  const body = new FormData();
  [...fileList].forEach((file) => body.append('files', file));
  const response = await fetch(api('/api/code/uploads'), { method: 'POST', body });
  const data = await response.json();
  if (!data.ok) throw new Error(data.error || 'Upload failed');
  return data.attachments || [];
}

async function launchJob(event) {
  event.preventDefault();
  const provider = document.querySelector('input[name="provider"]:checked')?.value || '';
  const info = providerCapability(provider);
  const error = $('create-error');
  error.hidden = true;
  if (!info.ready) {
    error.textContent = info.message || `${capital(provider)} is not ready.`;
    error.hidden = false;
    return;
  }
  $('launch').disabled = true;
  $('launch').textContent = 'Launching…';
  try {
    const attachments = await uploadFiles($('create-files').files);
    for (const line of $('links').value.split(/\r?\n/).map((v) => v.trim()).filter(Boolean)) {
      if (/^https?:\/\//i.test(line)) attachments.push({ kind: 'url', url: line, label: line });
    }
    const payload = {
      provider,
      cwd: $('cwd').value.trim(),
      brief: $('brief').value.trim(),
      model: $('model').value,
      reasoning: $('reasoning').value,
      fast: $('fast').checked,
      attachments,
    };
    const data = await request('/api/code/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    if (!data.ok) throw new Error(data.error || 'Could not start the job');
    closeCreate();
    $('brief').value = '';
    $('links').value = '';
    $('create-files').value = '';
    $('create-file-label').textContent = 'Choose files';
    await loadJobs();
    await selectJob(data.job.id);
  } catch (launchError) {
    error.textContent = launchError.message || String(launchError);
    error.hidden = false;
  } finally {
    $('launch').disabled = !providerCapability(provider).ready;
    $('launch').textContent = 'Launch agent';
  }
}

async function prepareFollowupFiles(files) {
  if (!files?.length) return;
  try {
    followupAttachments.push(...await uploadFiles(files));
    renderAttachmentList();
  } catch (error) { alert(error.message || error); }
  $('followup-files').value = '';
}

function renderAttachmentList() {
  const list = $('attachment-list');
  list.replaceChildren();
  followupAttachments.forEach((attachment, index) => {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'attachment-chip';
    chip.textContent = `${attachment.label || 'attachment'} ×`;
    chip.addEventListener('click', () => { followupAttachments.splice(index, 1); renderAttachmentList(); });
    list.appendChild(chip);
  });
}

async function sendMessage() {
  if (!selectedId) return;
  const text = $('message').value.trim();
  if (!text) return;
  $('send').disabled = true;
  const payload = { text, urgent: $('urgent').checked, attachments: followupAttachments };
  const data = await request(`/api/code/jobs/${encodeURIComponent(selectedId)}/messages`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  });
  $('send').disabled = false;
  if (!data.ok) { alert(data.error || 'Could not send'); return; }
  $('message').value = '';
  $('urgent').checked = false;
  followupAttachments = [];
  renderAttachmentList();
  autoGrow.call($('message'));
  await fetchNewEvents();
}

async function stopJob() {
  if (!selectedId) return;
  const data = await request(`/api/code/jobs/${encodeURIComponent(selectedId)}/stop`, { method: 'POST' });
  if (!data.ok) alert(data.error || 'Could not stop');
  await loadJobs();
}

async function deleteJob() {
  if (!selectedId || !confirm('Delete this CODE session and its local transcript? Project files are not deleted.')) return;
  const deletingId = selectedId;
  const data = await request(`/api/code/jobs/${encodeURIComponent(deletingId)}`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ confirm: deletingId }),
  });
  if (!data.ok) { alert(data.error || 'Could not delete'); return; }
  clearSelection();
  await loadJobs();
}

function toggleSpeak() {
  speaking = !speaking;
  localStorage.setItem('aiosCodeSpeak', speaking ? '1' : '0');
  if (!speaking) speechSynthesis.cancel();
  updateSpeakButton();
}

function updateSpeakButton() { $('speak').textContent = speaking ? 'Voice on' : 'Voice off'; }

function notifyEvent(event) {
  if (!['result', 'error', 'question', 'warning', 'provider_switch'].includes(event.kind)) return;
  const title = event.kind === 'result' ? `${capital(selectedJob?.provider)} finished` : event.kind === 'question' ? 'CODE needs your input' : event.kind === 'provider_switch' ? `CODE switched to ${capital(event.to_provider)}` : `CODE ${event.kind}`;
  if (document.hidden && 'Notification' in window && Notification.permission === 'granted') {
    new Notification(title, { body: String(event.text || '').slice(0, 220) });
  }
  if (speaking && 'speechSynthesis' in window) {
    const spoken = event.kind === 'result' ? `${capital(selectedJob?.provider)} finished. ${event.text || ''}` : `${title}. ${event.text || ''}`;
    speechSynthesis.speak(new SpeechSynthesisUtterance(spoken.slice(0, 700)));
  }
}

function autoGrow() {
  this.style.height = 'auto';
  this.style.height = `${Math.min(150, this.scrollHeight)}px`;
}

function capital(value) { const text = String(value || ''); return text ? text[0].toUpperCase() + text.slice(1) : ''; }
function stateLabel(value) { return ({ waiting_user: 'needs input', completed: 'finished', interrupted: 'interrupted' })[value] || value || 'unknown'; }
function shortId(value) { return value ? `native ${String(value).slice(0, 9)}` : 'starting native session'; }
