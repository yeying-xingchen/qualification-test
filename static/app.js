const $ = id => document.getElementById(id);
let current = null, lastResults = [], pollTimer = null, batchStartTime = null, lastJobId = null;

/* ─── helpers ─── */

function updateCounts() {
  const count = id => ($(id)?.value || '').split(/\n/).map(x => x.trim()).filter(Boolean).length;
  const tc = $('tokenCount'), pc = $('proxyCount');
  if (tc) tc.textContent = count('tokens');
  if (pc) pc.textContent = count('proxies');
}
['tokens', 'proxies'].forEach(id => $(id)?.addEventListener('input', updateCounts));
['workers', 'retries'].forEach(id => $(id)?.addEventListener('input', e => e.currentTarget.removeAttribute('aria-invalid')));

function syncMode(mode) {
  const visitor = mode === 'visitor', card = $('modeCard'), field = $('cdkField');
  if (card) card.hidden = !visitor;
  if (field) field.hidden = !visitor;
}
syncMode(document.body.dataset.serviceMode || 'self');
fetch('/api/admin/status').then(r => r.json()).then(s => syncMode(s.mode)).catch(() => {});
updateCounts();

function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
}

/* ─── multi-channel availability ─── */

function selectedAvailability(x) {
  const list = Array.isArray(x?.channel_details) ? x.channel_details : [];
  for (let i = list.length - 1; i >= 0; i--) {
    const c = list[i];
    if (c && typeof c === 'object' && c.selected && typeof c.selected === 'object') return c.selected;
  }
  return null;
}

function isAnyQualified(x) {
  if (!x || !x.ok) return false;
  if (x.qualified) return true;
  const avail = selectedAvailability(x);
  if (avail) return Object.values(avail).some(Boolean);
  return false;
}

function qualifiedChannels(x) {
  const avail = selectedAvailability(x);
  if (!avail) return x.qualified ? [x.channel || x.target_channel || ''] : [];
  return Object.entries(avail).filter(([, v]) => v).map(([k]) => k);
}

/* ─── multi-region selection ─── */

const REGION_META = {
  gcash:     { label: '菲律宾·GCash',     channel: 'gcash',   country: 'PH', currency: 'PHP' },
  card:      { label: '菲律宾·Card',      channel: 'card',    country: 'PH', currency: 'PHP' },
  paypal_uk: { label: '英国·PayPal',      channel: 'paypal',  country: 'GB', currency: 'GBP' },
  paypal_nl: { label: '荷兰·PayPal',      channel: 'paypal',  country: 'NL', currency: 'EUR' },
  ideal_nl:  { label: '荷兰·iDEAL',       channel: 'ideal',   country: 'NL', currency: 'EUR' },
  momo_vn:   { label: '越南·MoMo',        channel: 'momo',    country: 'VN', currency: 'VND' },
  gopay_id:  { label: '印度尼西亚·GoPay', channel: 'gopay',   country: 'ID', currency: 'IDR' },
  upi_in:    { label: '印度·UPI',         channel: 'upi',     country: 'IN', currency: 'INR' },
  blik_pl:   { label: '波兰·BLIK',        channel: 'blik',    country: 'PL', currency: 'PLN' },
  pix_br:    { label: '巴西·PIX',         channel: 'pix',     country: 'BR', currency: 'BRL' },
};

const customRegionBox = document.querySelector('input[name="regions"][value="custom"]');
const customRegionPanel = $('customRegion');
function syncCustomRegion() {
  if (customRegionPanel) customRegionPanel.hidden = !(customRegionBox?.checked ?? false);
}
if (customRegionBox) customRegionBox.addEventListener('change', syncCustomRegion);
syncCustomRegion();

function buildRegions() {
  const regions = [];
  for (const el of document.querySelectorAll('input[name="regions"]:checked')) {
    const preset = el.value;
    if (preset === 'custom') {
      const channel = ($('customChannel')?.value || '').trim().toLowerCase();
      const country = ($('customCountry')?.value || '').trim().toUpperCase();
      const currency = ($('customCurrency')?.value || '').trim().toUpperCase();
      if (!channel) continue;
      regions.push({ name: `自定义·${channel}`, preset: 'custom', channel, country: country || 'PH', currency: currency || 'PHP' });
    } else {
      const meta = REGION_META[preset] || { label: preset, channel: preset, country: 'PH', currency: 'PHP' };
      regions.push({ name: meta.label, preset, channel: meta.channel, country: meta.country, currency: meta.currency });
    }
  }
  return regions;
}

/* ─── render results ─── */

function regionBadges(x) {
  const regions = Array.isArray(x.regions) ? x.regions : [];
  if (!regions.length) return null;
  return regions.map(r => {
    const label = esc(r.name || r.channel || '地区');
    if (r.error) return `<span class="no" title="${esc(r.error)}">${label} 失败</span>`;
    return r.qualified
      ? `<span class="yes">${label} 可用</span>`
      : `<span class="no">${label} 未发布</span>`;
  }).join(' ');
}

/* Specific failure reason(s) for a row: failed regions first, then the
   whole-row error from the single-preset flow. Empty string when nothing failed. */
function errorText(x) {
  if (!x) return '';
  const regions = Array.isArray(x.regions) ? x.regions : [];
  const failed = regions.filter(r => r && r.error);
  if (failed.length) return failed.map(r => `${r.name || r.channel || '地区'}：${r.error}`).join('；');
  return x.ok ? '' : (x.error || '检测失败');
}

function regionChannelText(x) {
  const regions = Array.isArray(x.regions) ? x.regions : [];
  if (!regions.length) return null;
  const multi = regions.length > 1;
  const parts = [];
  for (const r of regions) {
    const list = Array.isArray(r.available_channels) ? r.available_channels : [];
    if (!list.length) continue;
    const prefix = multi ? `${r.name || r.channel || ''}·` : '';
    for (const c of list) parts.push(`${prefix}${esc(String(c))}`);
  }
  return parts.join(', ') || '-';
}

function legacyChannelText(x) {
  const detailList = Array.isArray(x.channel_details) ? x.channel_details : [];
  const availableList = Array.isArray(x.available_channels) ? x.available_channels : [];
  if (detailList.length) {
    return detailList.filter(c => !(c && typeof c === 'object' && c.selected && typeof c.selected === 'object'))
      .map(c => {
        if (typeof c === 'string') return c;
        if (!c || typeof c !== 'object') return String(c ?? '');
        const name = c.name || c.raw_type || c.id || '-', id = c.id && c.id !== name ? ` (${c.id})` : '';
        return `${name}${id}`;
      }).join(', ');
  }
  return availableList.map(c => typeof c === 'string' ? c : (!c || typeof c !== 'object' ? String(c ?? '') : (c.name || c.id || '-'))).join(', ');
}

function render(data) {
  $('stats').hidden = false;
  $('resultsCard').hidden = false;
  const hasResults = data.results?.some(Boolean);
  if ($('copyQualified')) $('copyQualified').disabled = !hasResults;
  if ($('export')) $('export').disabled = !hasResults;
  $('total').textContent = data.total;
  $('completed').textContent = data.completed;

  const rows = data.results.filter(Boolean);
  $('qualified').textContent = rows.filter(isAnyQualified).length;
  $('failed').textContent = rows.filter(x => !x.ok).length;

  // Progress bar
  updateProgress(data.completed, data.total);

  lastResults = rows;

  $('results').innerHTML = data.results.map((x, i) => {
    if (!x) return `<tr class="pending-row"><td>${i + 1}</td><td colspan="7"><span class="run">检测中…</span></td></tr>`;
    const badges = regionBadges(x);
    let state;
    if (badges) state = badges;
    else if (!x.ok) state = '<span class="err">失败</span>';
    else {
      const targetChannel = esc(x.channel || x.target_channel || '渠道');
      const qc = qualifiedChannels(x);
      state = qc.length ? `<span class="yes">${qc.map(esc).join(', ')} 可用</span>` : `<span class="no">未发现 ${targetChannel}</span>`;
    }
    const channelText = regionChannelText(x) ?? legacyChannelText(x);
    const detail = (x.account_email || x.access_token)
      ? `<button class="copy detail-btn" data-email="${esc(x.account_email)}" data-token="${esc(x.access_token || '')}" data-submitted="${esc(x.submitted_row || x.access_token || '')}">查看详情</button>`
      : '-';
    const err = errorText(x);
    const retryBtn = data.status === 'completed'
      ? `<button class="retry-btn" data-index="${i}" type="button" aria-label="重试第 ${i + 1} 条">重试</button>`
      : '';
    return `<tr><td>${i + 1}</td><td>${state}</td><td>${detail}</td><td>${esc(x.checkout_session_id || '-')}</td><td>${esc(x.currency || '-')}</td><td>${esc(channelText || x.evidence || '-')}</td><td class="err-col">${err ? `<span class="err">${esc(err)}</span>` : '-'}</td><td>${retryBtn}</td></tr>`;
  }).join('');
}

/* ─── progress bar ─── */

function updateProgress(completed, total) {
  const bar = $('progressBar');
  const label = $('progressLabel');
  const wrap = $('progressWrap');
  const progressBar = document.querySelector('.progress-bar');
  if (!bar) return;
  if (wrap) wrap.hidden = false;
  if (total === 0) { bar.style.width = '0%'; if (label) label.textContent = ''; if (progressBar) progressBar.setAttribute('aria-valuemax', '0'); return; }
  const pct = Math.min(100, Math.round((completed / total) * 100));
  bar.style.width = pct + '%';
  if (progressBar) {
    progressBar.setAttribute('aria-valuemax', String(total));
    progressBar.setAttribute('aria-valuenow', String(completed));
    progressBar.setAttribute('aria-valuetext', `${completed} / ${total}，${pct}%`);
  }
  if (label) label.textContent = `${completed} / ${total} (${pct}%)`;
}

function resetProgress() {
  const bar = $('progressBar');
  const label = $('progressLabel');
  const wrap = $('progressWrap');
  const progressBar = document.querySelector('.progress-bar');
  if (bar) bar.style.width = '0%';
  if (label) label.textContent = '';
  if (progressBar) { progressBar.setAttribute('aria-valuemax', '0'); progressBar.setAttribute('aria-valuenow', '0'); progressBar.removeAttribute('aria-valuetext'); }
  if (wrap) wrap.hidden = true;
}

/* ─── elapsed time ─── */

function formatElapsed(ms) {
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return m > 0 ? `${m}分${sec}秒` : `${sec}秒`;
}

function updateElapsed() {
  const el = $('elapsed');
  if (!el || !batchStartTime) return;
  el.textContent = formatElapsed(Date.now() - batchStartTime);
}

/* ─── polling ─── */

async function poll() {
  if (!current) return;
  try {
    const r = await fetch(`/api/gcash/batch/${current}`, { cache: 'no-store' });
    const d = await r.json();
    if (!r.ok) throw Error(d.error || '查询失败');
    render(d);
    updateElapsed();
    if (d.status === 'completed') {
      setMessage('✅ 批量检测完成');
      stopPolling();
      loadHistory();
      return;
    }
    pollTimer = setTimeout(poll, 1000);
  } catch (e) {
    setMessage(`❌ 轮询失败：${e.message}`);
    stopPolling();
  }
}

function stopPolling() {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  current = null;
  batchStartTime = null;
  if (start) { start.disabled = false; start.classList.remove('is-loading'); start.removeAttribute('aria-busy'); }
  if (stopBtn) stopBtn.hidden = true;
  if (elapsedLabel) elapsedLabel.hidden = true;
}

function stopPollingUI() {
  if (current) {
    setMessage('⏹ 已取消轮询（服务端任务仍在执行）');
  }
  stopPolling();
}

/* ─── message helper ─── */

function setMessage(text) {
  const el = $('message');
  if (el) el.textContent = text;
}

/* ─── start button ─── */

const start = $('start');
const stopBtn = $('stopBatch');
const elapsedLabel = $('elapsed');

if (start) start.onclick = async () => {
  // Stop any previous polling
  stopPolling();

  const tokens = ($('tokens')?.value || '').trim();
  const proxies = ($('proxies')?.value || '').trim();
  const channelLines = ($('channelProxies')?.value || '').split(/\n/).map(x => x.trim()).filter(Boolean);
  const channel_proxies = {};
  for (const line of channelLines) {
    const split = line.indexOf('=');
    if (split < 1) continue;
    const channel = line.slice(0, split).trim().toLowerCase();
    const proxy = line.slice(split + 1).trim();
    if (channel && proxy) (channel_proxies[channel] ??= []).push(proxy);
  }
  if (!tokens || (!proxies && !Object.keys(channel_proxies).length)) {
    setMessage('请填写 Token 和目标国家代理');
    return;
  }

  const regions = buildRegions();
  if (!regions.length) {
    setMessage('请至少选择一个检测地区');
    return;
  }
  const workersInput = $('workers');
  const retriesInput = $('retries');
  const workers = Number.parseInt(workersInput?.value || '4', 10);
  const retries = Number.parseInt(retriesInput?.value || '2', 10);
  const invalidSetting = (value, min, max) => !Number.isInteger(value) || value < min || value > max;
  if (invalidSetting(workers, 1, 32) || invalidSetting(retries, 1, 20)) {
    setMessage('并发数需为 1–32，重试次数需为 1–20');
    const invalid = invalidSetting(workers, 1, 32) ? workersInput : retriesInput;
    invalid?.focus();
    invalid?.setAttribute('aria-invalid', 'true');
    return;
  }
  workersInput?.removeAttribute('aria-invalid');
  retriesInput?.removeAttribute('aria-invalid');
  const primary = regions[0];
  const customRegion = regions.find(r => r.preset === 'custom');

  lastResults = [];
  if ($('copyQualified')) $('copyQualified').disabled = true;
  if ($('export')) $('export').disabled = true;
  start.disabled = true;
  start.classList.add('is-loading');
  start.setAttribute('aria-busy', 'true');
  if (stopBtn) stopBtn.hidden = false;
  setMessage('⏳ 正在提交任务…');
  resetProgress();

  try {
    const body = {
      tokens, proxies, channel_proxies, workers, retries,
      with_promo: $('withPromo').checked,
      visitor: false,
      cdk: $('cdk')?.value?.trim() || '',
      regions,
      preset: primary.preset,
      target_channel: primary.channel,
    };
    if (customRegion) {
      body.presets = { custom: { channel: customRegion.channel, country: customRegion.country, currency: customRegion.currency, plan: 'plus' } };
    }
    const r = await fetch('/api/gcash/batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    const d = await r.json();
    if (!r.ok) throw Error(d.error || '提交失败');

    current = d.job_id;
    lastJobId = d.job_id;
    batchStartTime = Date.now();
    if (elapsedLabel) { elapsedLabel.hidden = false; elapsedLabel.textContent = '0秒'; }
    start.classList.remove('is-loading');
    start.removeAttribute('aria-busy');
    setMessage(`已提交 ${d.total} 条，开始检测…`);
    loadHistory();
    poll();
  } catch (e) {
    setMessage(`❌ ${e.message}`);
    stopPolling();
  }
};

if (stopBtn) stopBtn.onclick = stopPollingUI;

/* ─── single-row retry ─── */

async function retryRow(index) {
  if (!lastJobId) { setMessage('当前没有可重试的已完成任务'); return; }
  try {
    const r = await fetch(`/api/gcash/batch/${lastJobId}/retry`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ index })
    });
    const d = await r.json();
    if (!r.ok) throw Error(d.error || '重试提交失败');
    // Resume polling the same job: the row will show "检测中…" until the retry
    // writes its result back in place, then the job returns to completed.
    current = lastJobId;
    batchStartTime = Date.now();
    if (elapsedLabel) { elapsedLabel.hidden = false; elapsedLabel.textContent = '0秒'; }
    if (stopBtn) stopBtn.hidden = false;
    setMessage(`↻ 正在重试第 ${index + 1} 条…`);
    poll();
  } catch (e) {
    setMessage(`❌ 重试失败：${e.message}`);
  }
}

/* ─── modal ─── */

const modal = $('accountModal');
let modalTrigger = null;
function closeAccountModal() {
  if (!modal || modal.hasAttribute('hidden')) return;
  modal.classList.remove('open');
  modal.setAttribute('hidden', '');
  modalTrigger?.focus();
  modalTrigger = null;
}
document.addEventListener('click', e => {
  const retry = e.target.closest('.retry-btn');
  if (retry) {
    const index = Number.parseInt(retry.dataset.index, 10);
    if (Number.isInteger(index)) {
      retry.disabled = true;
      retry.classList.add('is-loading');
      retryRow(index).finally(() => { retry.disabled = false; retry.classList.remove('is-loading'); });
    }
    return;
  }
  const detail = e.target.closest('.detail-btn');
  if (detail) {
    modalTrigger = detail;
    const email = detail.dataset.email || '';
    $('modalEmail').textContent = email || '（未解析到邮箱）';
    $('modalEmail').classList.toggle('empty', !email);
    $('modalAT').value = detail.dataset.token || '';
    $('modalToken').value = detail.dataset.submitted || detail.dataset.token || '';
    modal.removeAttribute('hidden');
    modal.classList.add('open');
    $('closeModal')?.focus();
    return;
  }
  if (e.target.closest('#closeModal') || e.target === modal) {
    closeAccountModal();
    return;
  }
  if (e.target.closest('#copyModalAT')) {
    const button = e.target.closest('#copyModalAT');
    const text = $('modalAT').value || '';
    const done = () => { button.textContent = '已复制'; setTimeout(() => button.textContent = '一键复制 AT', 1200); };
    if (!text) { setMessage('没有可复制的 Access Token'); return; }
    copyText(text).then(done).catch(err => setMessage(err.message));
    return;
  }
  if (e.target.closest('#copyModalToken')) {
    const button = e.target.closest('#copyModalToken');
    const text = $('modalToken').value || '';
    const done = () => { button.textContent = '已复制'; setTimeout(() => button.textContent = '复制', 1200); };
    if (!text) { setMessage('没有可复制的内容'); return; }
    copyText(text).then(done).catch(err => setMessage(err.message));
  }
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && modal && !modal.hasAttribute('hidden')) {
    e.preventDefault();
    closeAccountModal();
    return;
  }
  const row = e.target.closest?.('.history-row');
  if (row && !e.target.closest('button') && (e.key === 'Enter' || e.key === ' ')) {
    e.preventDefault();
    if (row.dataset.jobId) openHistory(row.dataset.jobId);
  }
});

/* ─── qualified token actions ─── */

function qualifiedTokens() {
  const seen = new Set();
  return lastResults
    .filter(x => x && x.ok && isAnyQualified(x))
    .map(x => x.submitted_row || x.access_token || x.token || '')
    .map(x => String(x).trim())
    .filter(x => x && !seen.has(x) && seen.add(x));
}

function copyViaTextarea(text) {
  // Create a tiny invisible textarea, select its content and fire execCommand.
  // Must run synchronously inside the click gesture: Chrome drops the transient
  // user activation once an await / microtask boundary is crossed, and then
  // both execCommand('copy') and the Clipboard API get rejected.
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.style.position = 'fixed';
  area.style.top = '0';
  area.style.left = '0';
  area.style.width = '2px';
  area.style.height = '2px';
  area.style.opacity = '0';
  area.style.border = '0';
  area.style.padding = '0';
  area.style.background = 'transparent';
  document.body.appendChild(area);
  area.focus();
  area.select();
  area.setSelectionRange(0, area.value.length);
  let ok = false;
  try { ok = document.execCommand('copy'); } finally { area.remove(); }
  return ok;
}

function copyText(text) {
  if (!text) return Promise.reject(new Error('没有可复制的内容'));
  // 1) Synchronous execCommand first — works on insecure origins and in
  //    sandboxed iframes where navigator.clipboard is unavailable/blocked.
  try {
    if (copyViaTextarea(text)) return Promise.resolve();
  } catch (e) { /* fall through to the async API */ }
  // 2) Async Clipboard API (secure contexts, allowed permission policy).
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text).then(
      () => true,
      () => { throw new Error('复制被浏览器阻止，请手动选中后复制'); }
    );
  }
  return Promise.reject(new Error('复制被浏览器阻止，请手动选中后复制'));
}

const copyQualified = $('copyQualified');
if (copyQualified) copyQualified.addEventListener('click', async () => {
  const tokens = qualifiedTokens();
  if (!tokens.length) { setMessage('当前没有有资格 Token，请先等待检测完成'); return; }
  copyQualified.disabled = true;
  copyQualified.textContent = `正在复制 ${tokens.length} 条…`;
  try {
    await copyText(tokens.join('\n'));
    setMessage(`已复制 ${tokens.length} 条有资格 Token`);
    copyQualified.textContent = `已复制 ${tokens.length} 条`;
  } catch (e) {
    setMessage(`复制失败：${e.message}`);
    copyQualified.textContent = '复制失败';
  } finally {
    setTimeout(() => { copyQualified.disabled = false; copyQualified.textContent = '复制全部有资格 Token'; }, 1600);
  }
});

$('submitQualified').onclick = async () => {
  const endpoint = $('submitEndpoint').value.trim();
  const tokens = qualifiedTokens();
  if (!endpoint) { setMessage('请先填写 API 地址'); return; }
  if (!tokens.length) { setMessage('当前没有可提交的有资格 Token'); return; }
  const button = $('submitQualified');
  button.disabled = true;
  setMessage(`正在提交 ${tokens.length} 条…`);
  try {
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tokens, count: tokens.length })
    });
    const text = await response.text();
    if (!response.ok) throw Error(`API 返回 ${response.status}${text ? `: ${text.slice(0, 160)}` : ''}`);
    setMessage(`✅ 已提交 ${tokens.length} 条有资格 Token`);
    button.textContent = '提交成功';
    setTimeout(() => button.textContent = '提交全部有资格 Token', 1600);
  } catch (e) {
    setMessage(`❌ 提交失败：${e.message}`);
  } finally {
    button.disabled = false;
  }
};

$('export').onclick = () => {
  const blob = new Blob([JSON.stringify(lastResults, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `gcash-results-${Date.now()}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
};

/* ─── history ─── */

const HISTORY_LIMIT = 20;
let historyPage = 0, historyTotal = 0;

function formatTime(iso) {
  if (!iso) return '-';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return esc(iso);
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

async function loadHistory() {
  const tbody = $('historyList');
  if (!tbody) return;
  try {
    const r = await fetch(`/api/gcash/batch?limit=${HISTORY_LIMIT}&offset=${historyPage * HISTORY_LIMIT}`, { cache: 'no-store' });
    const d = await r.json();
    if (!r.ok) throw Error(d.error || '加载历史任务失败');
    historyTotal = d.total || 0;
    renderHistory(d.tasks || []);
    updateHistoryPagination();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="8" class="history-empty">❌ ${esc(e.message)}</td></tr>`;
  }
}

function renderHistory(tasks) {
  const tbody = $('historyList');
  if (!tbody) return;
  if (!tasks.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="history-empty">暂无历史任务</td></tr>`;
    return;
  }
  tbody.innerHTML = tasks.map(t => {
    const progress = `${t.completed}/${t.total}`;
    const status = t.status === 'completed'
      ? '<span class="yes">已完成</span>'
      : (t.status === 'running' ? '<span class="run">进行中</span>' : `<span class="no">${esc(t.status)}</span>`);
    const finished = formatTime(t.finished_at);
    return `<tr class="history-row" tabindex="0" role="button" aria-label="查看任务 ${esc(t.job_id.slice(0, 8))}" data-job-id="${esc(t.job_id)}">
      <td>${status}</td>
      <td>${formatTime(t.created_at)}</td>
      <td>${finished}</td>
      <td>${progress}</td>
      <td>${t.qualified_count}</td>
      <td>${t.failed_count}</td>
      <td>${esc(t.target_channel || '-')}</td>
      <td><button class="history-del-btn" data-job-id="${esc(t.job_id)}" type="button">删除</button></td>
    </tr>`;
  }).join('');
}

function updateHistoryPagination() {
  const info = $('historyPageInfo');
  if (info) info.textContent = historyTotal ? `共 ${historyTotal} 条 · 第 ${historyPage + 1} 页` : '';
  const prev = $('historyPrev'), next = $('historyNext');
  if (prev) prev.disabled = historyPage === 0;
  if (next) next.disabled = (historyPage + 1) * HISTORY_LIMIT >= historyTotal;
}

async function openHistory(jobId) {
  try {
    const r = await fetch(`/api/gcash/batch/${encodeURIComponent(jobId)}`, { cache: 'no-store' });
    const d = await r.json();
    if (!r.ok) throw Error(d.error || '加载任务失败');
    stopPolling();
    lastJobId = jobId;
    render(d);
    setMessage(`已载入历史任务（${jobId.slice(0, 8)}…）`);
  } catch (e) {
    setMessage(`❌ 载入历史任务失败：${e.message}`);
  }
}

document.addEventListener('click', e => {
  const del = e.target.closest('.history-del-btn');
  if (del) {
    e.stopPropagation();
    const id = del.dataset.jobId;
    if (!id) return;
    if (!confirm(`确定删除任务 ${id.slice(0, 8)}… 的历史记录吗？`)) return;
    del.disabled = true;
    del.classList.add('is-loading');
    fetch(`/api/gcash/batch/${encodeURIComponent(id)}`, { method: 'DELETE' })
      .then(r => r.json())
      .then(d => {
        if (!d.ok) throw Error(d.error || '删除失败');
        setMessage(`已删除历史任务 ${id.slice(0, 8)}…`);
        loadHistory();
      })
      .catch(err => setMessage(`❌ 删除失败：${err.message}`))
      .finally(() => { del.disabled = false; del.classList.remove('is-loading'); });
    return;
  }
  const row = e.target.closest('.history-row');
  if (row && row.dataset.jobId) openHistory(row.dataset.jobId);
});

$('refreshHistory')?.addEventListener('click', async e => {
  const button = e.currentTarget;
  button.disabled = true;
  button.classList.add('is-loading');
  try { await loadHistory(); } finally { button.disabled = false; button.classList.remove('is-loading'); }
});
$('historyPrev')?.addEventListener('click', () => { historyPage = Math.max(0, historyPage - 1); loadHistory(); });
$('historyNext')?.addEventListener('click', () => { historyPage += 1; loadHistory(); });

loadHistory();