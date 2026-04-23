// Chronicle Export — popup script.
// Strictly DOM work + message passing. Never uses innerHTML with variables.
// Reads persisted state from chrome.storage.local so progress survives the
// popup closing mid-run.

const els = {
  version: document.getElementById('version'),
  startDate: document.getElementById('startDate'),
  endDate: document.getElementById('endDate'),
  rangeField: document.getElementById('rangeField'),
  fetchAll: document.getElementById('fetchAll'),
  dateRow: document.getElementById('dateRow'),
  exportBtn: document.getElementById('exportBtn'),
  retryBtn: document.getElementById('retryBtn'),
  cancelBtn: document.getElementById('cancelBtn'),
  elapsed: document.getElementById('elapsed'),
  status: document.getElementById('status'),
  progressBar: document.getElementById('progressBar'),
  log: document.getElementById('log')
};

let elapsedTimer = null;

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

function formatElapsed(ms) {
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return m > 0 ? `${m}m ${rem}s` : `${s}s`;
}

function startElapsedTicker(startedAt) {
  stopElapsedTicker();
  const tick = () => {
    const ms = Date.now() - new Date(startedAt).getTime();
    els.elapsed.textContent = `Elapsed ${formatElapsed(ms)}`;
  };
  tick();
  elapsedTimer = setInterval(tick, 1000);
}

function stopElapsedTicker() {
  if (elapsedTimer) {
    clearInterval(elapsedTimer);
    elapsedTimer = null;
  }
}

function applyFetchAllState() {
  if (els.fetchAll.checked) {
    els.dateRow.classList.add('dates-disabled');
  } else {
    els.dateRow.classList.remove('dates-disabled');
  }
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
    els.retryBtn.classList.add('hidden');
    els.cancelBtn.classList.remove('hidden');
    if (currentRun.startedAt) startElapsedTicker(currentRun.startedAt);
    const { completed = 0, total = 0 } = currentRun;
    setStatusText(total > 0 ? `Fetching ${completed}/${total}…` : 'Starting export…');
    setProgress(completed, total);
    return;
  }

  stopElapsedTicker();
  els.cancelBtn.classList.add('hidden');
  els.exportBtn.disabled = false;
  setProgress(0, 0);

  if (lastRun) {
    if (lastRun.aborted) {
      setStatusText(`Last run aborted: ${lastRun.error || 'unknown error'}`);
    } else if (lastRun.noChanges) {
      setStatusText('Nothing new since last run.');
    } else {
      const parts = [`Last run: ${lastRun.succeeded} ok`];
      if (lastRun.failed > 0) parts.push(`${lastRun.failed} failed`);
      if (lastRun.deletedCount) parts.push(`${lastRun.deletedCount} deleted`);
      if (lastRun.outputFile) parts.push(`→ ${lastRun.outputFile}`);
      setStatusText(parts.join(' · '));
    }
    if (lastRun.durationMs) {
      els.elapsed.textContent = `Took ${formatElapsed(lastRun.durationMs)}`;
    } else {
      els.elapsed.textContent = '';
    }
    if (lastRun.failed && lastRun.failed > 0) {
      els.retryBtn.classList.remove('hidden');
    } else {
      els.retryBtn.classList.add('hidden');
    }
  } else {
    setStatusText('Ready.');
    els.elapsed.textContent = '';
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

async function startExport(onlyUuids) {
  const fetchAll = els.fetchAll.checked;
  const rangeField = els.rangeField.value;
  const startDate = fetchAll ? null : els.startDate.value;
  const endDate = fetchAll ? null : els.endDate.value;

  if (!fetchAll && !onlyUuids) {
    if (!startDate || !endDate) {
      setStatusText('Pick both a start and an end date, or check "Fetch all".');
      return;
    }
    if (startDate > endDate) {
      setStatusText('Start date must be on or before end date.');
      return;
    }
  }

  els.exportBtn.disabled = true;
  els.retryBtn.classList.add('hidden');
  setStatusText('Connecting to claude.ai…');

  try {
    const tabId = await ensureContentScript();
    await sendToContent(tabId, {
      action: 'exportRange',
      fetchAll,
      startDate,
      endDate,
      rangeField,
      onlyUuids: onlyUuids || null
    });
    setStatusText('Export started. You can close this popup — it will keep running.');
  } catch (err) {
    els.exportBtn.disabled = false;
    setStatusText(`Could not start export: ${err.message}. Open claude.ai in the active tab and try again.`);
  }
}

async function cancelRun() {
  await new Promise((resolve) => chrome.storage.local.set({ cancelRequested: true }, resolve));
  setStatusText('Cancelling…');
}

async function retryFailed() {
  const { lastRun, runLog = [] } = await readState();
  if (!lastRun) return;
  const failed = runLog.filter((e) => e.level === 'error' && e.uuid).map((e) => e.uuid);
  if (failed.length === 0) {
    setStatusText('Nothing to retry.');
    return;
  }
  // Replay the same range the last run used. onlyUuids overrides filters
  // in content.js, so fetchAll/date values are just for status display.
  if (lastRun.rangeStart) els.startDate.value = lastRun.rangeStart;
  if (lastRun.rangeEnd) els.endDate.value = lastRun.rangeEnd;
  if (lastRun.rangeField) els.rangeField.value = lastRun.rangeField;
  startExport(failed);
}

// ──────────────────────────── init ────────────────────────────

function savePrefs() {
  chrome.storage.local.set({
    prefs: {
      startDate: els.startDate.value,
      endDate: els.endDate.value,
      rangeField: els.rangeField.value,
      fetchAll: els.fetchAll.checked
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
  els.fetchAll.checked = !!prefs.fetchAll;
  applyFetchAllState();

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
  els.fetchAll.addEventListener('change', () => { applyFetchAllState(); savePrefs(); });

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
  els.cancelBtn.addEventListener('click', () => cancelRun());
  els.retryBtn.addEventListener('click', () => retryFailed());
});
