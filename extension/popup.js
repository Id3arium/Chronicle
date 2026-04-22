// Chronicle Export — popup script.
// Strictly DOM work + message passing. Never uses innerHTML with variables.
// Reads persisted state from chrome.storage.local so progress survives the
// popup closing mid-run.

const els = {
  version: document.getElementById('version'),
  startDate: document.getElementById('startDate'),
  endDate: document.getElementById('endDate'),
  rangeField: document.getElementById('rangeField'),
  exportBtn: document.getElementById('exportBtn'),
  incrementalBtn: document.getElementById('incrementalBtn'),
  retryBtn: document.getElementById('retryBtn'),
  status: document.getElementById('status'),
  progressBar: document.getElementById('progressBar'),
  log: document.getElementById('log')
};

// ──────────────────────────── state helpers ────────────────────────────

function readState() {
  return new Promise((resolve) => chrome.storage.local.get(['currentRun', 'lastRun', 'runLog'], (r) => resolve(r)));
}

function todayIso() {
  const d = new Date();
  return d.toISOString().slice(0, 10);
}

function monthAgoIso() {
  const d = new Date();
  d.setDate(d.getDate() - 30);
  return d.toISOString().slice(0, 10);
}

function setProgress(completed, total) {
  const pct = total > 0 ? Math.min(100, Math.round((completed / total) * 100)) : 0;
  els.progressBar.style.width = `${pct}%`;
}

function setStatusText(text) {
  els.status.textContent = text;
}

// ──────────────────────────── log rendering ────────────────────────────

function clearLog() {
  while (els.log.firstChild) els.log.removeChild(els.log.firstChild);
}

function renderLog(entries) {
  clearLog();
  // Show most recent last (natural append order).
  for (const e of entries.slice(-50)) {
    const row = document.createElement('div');
    row.className = `log-entry ${e.level || 'ok'}`;

    const mark = document.createElement('span');
    mark.className = 'mark';
    mark.textContent = e.level === 'error' ? '✗' : e.level === 'warn' ? '⚠' : '✓';
    row.appendChild(mark);

    const body = document.createElement('span');
    body.className = 'body';
    const parts = [];
    if (e.uuid) parts.push(e.uuid.slice(0, 8));
    if (e.project) parts.push(e.project);
    if (e.msg) parts.push(e.msg);
    if (e.retry) parts.push(`retry ${e.retry}`);
    if (e.status) parts.push(`HTTP ${e.status}`);
    body.textContent = parts.join(' · ');
    row.appendChild(body);

    // Optional details row for the diagnostic body snippet.
    if (e.bodySnippet) {
      const wrap = document.createElement('details');
      const sum = document.createElement('summary');
      sum.textContent = 'response';
      const pre = document.createElement('div');
      pre.className = 'details-row';
      pre.textContent = e.bodySnippet;
      wrap.appendChild(sum);
      wrap.appendChild(pre);
      row.appendChild(wrap);
    }

    els.log.appendChild(row);
  }
  // Auto-scroll to bottom.
  els.log.scrollTop = els.log.scrollHeight;
}

// ──────────────────────────── rendering by state ────────────────────────────

function renderFromState(state) {
  const { currentRun, lastRun, runLog = [] } = state;
  renderLog(runLog);

  if (currentRun) {
    els.exportBtn.disabled = true;
    els.incrementalBtn.disabled = true;
    els.retryBtn.classList.add('hidden');
    const { completed = 0, total = 0 } = currentRun;
    setStatusText(total > 0 ? `Fetching ${completed}/${total}…` : 'Starting export…');
    setProgress(completed, total);
    return;
  }

  els.exportBtn.disabled = false;
  els.incrementalBtn.disabled = false;
  setProgress(0, 0);

  if (lastRun) {
    if (lastRun.aborted) {
      setStatusText(`Last run aborted: ${lastRun.error || 'unknown error'}`);
    } else {
      const parts = [`Last run: ${lastRun.succeeded} ok`];
      if (lastRun.failed > 0) parts.push(`${lastRun.failed} failed`);
      if (lastRun.outputFile) parts.push(`→ ${lastRun.outputFile}`);
      setStatusText(parts.join(' · '));
    }
    if (lastRun.failed && lastRun.failed > 0) {
      els.retryBtn.classList.remove('hidden');
    } else {
      els.retryBtn.classList.add('hidden');
    }
  } else {
    setStatusText('Ready.');
  }
}

// ──────────────────────────── actions ────────────────────────────

function ensureContentScript() {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage({ action: 'ensureContentScript' }, (res) => {
      if (chrome.runtime.lastError) return reject(chrome.runtime.lastError);
      if (!res || !res.success) return reject(new Error(res && res.error ? res.error : 'Could not reach claude.ai tab.'));
      resolve(res.tabId);
    });
  });
}

function sendToContent(tabId, message) {
  return new Promise((resolve, reject) => {
    chrome.tabs.sendMessage(tabId, message, (res) => {
      if (chrome.runtime.lastError) return reject(chrome.runtime.lastError);
      resolve(res);
    });
  });
}

async function startExport(retryUuids) {
  const startDate = els.startDate.value;
  const endDate = els.endDate.value;
  const rangeField = els.rangeField.value;

  if (!startDate || !endDate) {
    setStatusText('Pick both a start and an end date.');
    return;
  }
  if (startDate > endDate) {
    setStatusText('Start date must be on or before end date.');
    return;
  }

  els.exportBtn.disabled = true;
  els.retryBtn.classList.add('hidden');
  setStatusText('Connecting to claude.ai…');

  try {
    const tabId = await ensureContentScript();
    await sendToContent(tabId, {
      action: 'exportRange',
      startDate,
      endDate,
      rangeField,
      retryUuids: retryUuids || null
    });
    setStatusText('Export started. You can close this popup — it will keep running.');
  } catch (err) {
    els.exportBtn.disabled = false;
    setStatusText(`Could not start export: ${err.message}. Open claude.ai in the active tab and try again.`);
  }
}

async function startIncremental() {
  els.exportBtn.disabled = true;
  els.incrementalBtn.disabled = true;
  els.retryBtn.classList.add('hidden');
  setStatusText('Connecting to claude.ai…');

  try {
    const tabId = await ensureContentScript();
    await sendToContent(tabId, { action: 'exportIncremental' });
    setStatusText('Checking for updates. You can close this popup — it will keep running.');
  } catch (err) {
    els.exportBtn.disabled = false;
    els.incrementalBtn.disabled = false;
    setStatusText(`Could not start export: ${err.message}. Open claude.ai in the active tab and try again.`);
  }
}

async function retryFailed() {
  const { lastRun, runLog = [] } = await readState();
  if (!lastRun) return;
  const failed = runLog.filter((e) => e.level === 'error' && e.uuid).map((e) => e.uuid);
  if (failed.length === 0) {
    setStatusText('Nothing to retry.');
    return;
  }
  // Replay the same range the last run used.
  els.startDate.value = lastRun.rangeStart || els.startDate.value;
  els.endDate.value = lastRun.rangeEnd || els.endDate.value;
  els.rangeField.value = lastRun.rangeField || els.rangeField.value;
  startExport(failed);
}

// ──────────────────────────── init ────────────────────────────

function savePrefs() {
  chrome.storage.local.set({
    prefs: {
      startDate: els.startDate.value,
      endDate: els.endDate.value,
      rangeField: els.rangeField.value
    }
  });
}

function loadPrefs() {
  return new Promise((resolve) => chrome.storage.local.get(['prefs'], (r) => resolve(r.prefs || {})));
}

document.addEventListener('DOMContentLoaded', async () => {
  els.version.textContent = `v${chrome.runtime.getManifest().version}`;

  const prefs = await loadPrefs();
  els.startDate.value = prefs.startDate || monthAgoIso();
  els.endDate.value = prefs.endDate || todayIso();
  if (prefs.rangeField) els.rangeField.value = prefs.rangeField;

  const state = await readState();
  renderFromState(state);

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== 'local') return;
    readState().then(renderFromState);
  });

  // Persist on change so selections survive popup close.
  els.startDate.addEventListener('change', savePrefs);
  els.endDate.addEventListener('change', savePrefs);
  els.rangeField.addEventListener('change', savePrefs);

  // Force native date picker to open on click anywhere in the input
  // (Firefox's built-in hotspot is narrow; showPicker() makes the whole
  // input clickable).
  for (const input of [els.startDate, els.endDate]) {
    input.addEventListener('click', () => {
      if (typeof input.showPicker === 'function') {
        try { input.showPicker(); } catch (_) { /* ignore if not allowed */ }
      }
    });
  }

  els.exportBtn.addEventListener('click', () => startExport(null));
  els.incrementalBtn.addEventListener('click', () => startIncremental());
  els.retryBtn.addEventListener('click', () => retryFailed());
});
