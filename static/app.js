const $ = id => document.getElementById(id);
let current = null, lastResults = [], pollTimer = null, batchStartTime = null;

/* ─── helpers ─── */

function updateCounts() {
  const count = id => ($(id)?.value || '').split(/\n/).map(x => x.trim()).filter(Boolean).length;
  const tc = $('tokenCount'), pc = $('proxyCount');
  if (tc) tc.textContent = count('tokens');
  if (pc) pc.textContent = count('proxies');
}
['tokens', 'proxies'].forEach(id => $(id)?.addEventListener('input', updateCounts));

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
  ideal_nl:  { label: '荷兰·iDEAL',       channel: 'ideal',   country: 'NL', currency: 'EUR' },
  momo_vn:   { label: '越南·MoMo',        channel: 'momo',    country: 'VN', currency: 'VND' },
  gopay_id:  { label: '印度尼西亚·GoPay', channel: 'gopay',   country: 'ID', currency: 'IDR' },
  upi_in:    { label: '印度·UPI',         channel: 'upi',     country: 'IN', currency: 'INR' },
  blik_pl:   { label: '波兰·BLIK',        channel: 'blik',    country: 'PL', currency: 'PLN' },
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
    if (r.error) return `<span class="no">${label} 失败</span>`;
    return r.qualified
      ? `<span class="yes">${label} 可用</span>`
      : `<span class="no">${label} 未发布</span>`;
  }).join(' ');
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
  $('total').textContent = data.total;
  $('completed').textContent = data.completed;

  const rows = data.results.filter(Boolean);
  $('qualified').textContent = rows.filter(isAnyQualified).length;
  $('failed').textContent = rows.filter(x => !x.ok).length;

  // Progress bar
  updateProgress(data.completed, data.total);

  lastResults = rows;

  $('results').innerHTML = data.results.map((x, i) => {
    if (!x) return `<tr><td>${i + 1}</td><td colspan="6">检测中…</td></tr>`;
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
    const detail = x.account_email
      ? `<button class="copy detail-btn" data-email="${esc(x.account_email)}" data-token="${esc(x.access_token || '')}" data-submitted="${esc(x.submitted_row || x.access_token || '')}">查看详情</button>`
      : '-';
    return `<tr><td>${i + 1}</td><td>${state}</td><td>${detail}</td><td>${esc(x.checkout_session_id || '-')}</td><td>${esc(x.currency || '-')}</td><td>${esc(channelText || x.evidence || x.error || '-')}</td></tr>`;
  }).join('');
}

/* ─── progress bar ─── */

function updateProgress(completed, total) {
  const bar = $('progressBar');
  const label = $('progressLabel');
  const wrap = $('progressWrap');
  if (!bar) return;
  if (wrap) wrap.hidden = false;
  if (total === 0) { bar.style.width = '0%'; if (label) label.textContent = ''; return; }
  const pct = Math.min(100, Math.round((completed / total) * 100));
  bar.style.width = pct + '%';
  if (label) label.textContent = `${completed} / ${total} (${pct}%)`;
}

function resetProgress() {
  const bar = $('progressBar');
  const label = $('progressLabel');
  const wrap = $('progressWrap');
  if (bar) bar.style.width = '0%';
  if (label) label.textContent = '';
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
  if (start) start.disabled = false;
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
  const selected_channels = [...document.querySelectorAll('input[name="channels"]:checked')].map(x => x.value);
  const workers = Math.max(1, Math.min(32, Number.parseInt($('workers')?.value || '4', 10) || 4));
  const primary = regions[0];
  const customRegion = regions.find(r => r.preset === 'custom');

  start.disabled = true;
  if (stopBtn) stopBtn.hidden = false;
  setMessage('⏳ 正在提交…');
  resetProgress();

  try {
    const body = {
      tokens, proxies, channel_proxies, workers,
      with_promo: $('withPromo').checked,
      visitor: false,
      cdk: $('cdk')?.value?.trim() || '',
      regions,
      channels: selected_channels,
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
    batchStartTime = Date.now();
    if (elapsedLabel) { elapsedLabel.hidden = false; elapsedLabel.textContent = '0秒'; }
    setMessage(`已提交 ${d.total} 条，开始检测…`);
    poll();
  } catch (e) {
    setMessage(`❌ ${e.message}`);
    stopPolling();
  }
};

if (stopBtn) stopBtn.onclick = stopPollingUI;

/* ─── modal ─── */

const modal = $('accountModal');
document.addEventListener('click', e => {
  const detail = e.target.closest('.detail-btn');
  if (detail) {
    $('modalEmail').textContent = detail.dataset.email || '';
    $('modalToken').value = detail.dataset.submitted || detail.dataset.token || '';
    modal.removeAttribute('hidden');
    modal.classList.add('open');
    return;
  }
  if (e.target.closest('#closeModal') || e.target === modal) {
    modal.classList.remove('open');
    modal.setAttribute('hidden', '');
    return;
  }
  if (e.target.closest('#copyModalToken')) {
    const button = e.target.closest('#copyModalToken');
    const text = $('modalToken').value || '';
    const done = () => { button.textContent = '已复制'; setTimeout(() => button.textContent = '复制', 1200); };
    const fallback = () => {
      const area = document.createElement('textarea');
      area.value = text;
      area.setAttribute('readonly', '');
      area.style.position = 'fixed';
      area.style.left = '-9999px';
      area.style.top = '0';
      document.body.appendChild(area);
      area.focus();
      area.select();
      area.setSelectionRange(0, area.value.length);
      let copied = false;
      try { copied = document.execCommand('copy'); } finally { area.remove(); }
      if (!copied) throw Error('浏览器拒绝了复制操作，请手动选择内容');
      return copied;
    };
    if (!text) { setMessage('没有可复制的内容'); return; }
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(() => {
        try { fallback(); done(); } catch (err) { setMessage(err.message); }
      });
    } else {
      try { fallback(); done(); } catch (err) { setMessage(err.message); }
    }
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

function copyText(text) {
  if (!text) return Promise.reject(new Error('没有可复制的有资格 Token'));
  const fallback = () => {
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.left = '-9999px';
    area.style.top = '0';
    document.body.appendChild(area);
    area.focus();
    area.select();
    area.setSelectionRange(0, area.value.length);
    let copied = false;
    try { copied = document.execCommand('copy'); } finally { area.remove(); }
    if (!copied) throw new Error('浏览器拒绝了复制操作，请手动复制结果');
  };
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text).catch(fallback);
  fallback();
  return Promise.resolve();
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