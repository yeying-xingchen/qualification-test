const $ = id => document.getElementById(id);
let current = null, lastResults = [];

function updateCounts() {
  const count = id => ($(id)?.value || '').split(/\n/).map(x => x.trim()).filter(Boolean).length;
  const tokenCount = $('tokenCount'), proxyCount = $('proxyCount');
  if (tokenCount) tokenCount.textContent = count('tokens');
  if (proxyCount) proxyCount.textContent = count('proxies');
}
['tokens', 'proxies'].forEach(id => $(id)?.addEventListener('input', updateCounts));

function syncMode(mode) {
  const visitor = mode === 'visitor', card = $('modeCard'), field = $('cdkField');
  if (card) card.hidden = !visitor;
  if (field) field.hidden = !visitor;
}
syncMode(document.body.dataset.serviceMode || 'self');
fetch('/api/admin/status').then(r => r.json()).then(s => syncMode(s.mode)).catch(() => { });
updateCounts();

function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// Extract the per-channel availability map the backend appends to channel_details.
function selectedAvailability(x) {
  const detailList = Array.isArray(x?.channel_details) ? x.channel_details : [];
  for (let i = detailList.length - 1; i >= 0; i--) {
    const c = detailList[i];
    if (c && typeof c === 'object' && c.selected && typeof c.selected === 'object') return c.selected;
  }
  return null;
}

// A token is considered qualified when at least one selected channel is available.
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

function render(data) {
  $('stats').hidden = false;
  $('resultsCard').hidden = false;
  $('total').textContent = data.total;
  $('completed').textContent = data.completed;
  const rows = data.results.filter(Boolean);
  $('qualified').textContent = rows.filter(isAnyQualified).length;
  $('failed').textContent = rows.filter(x => !x.ok).length;
  lastResults = rows;
  $('results').innerHTML = data.results.map((x, i) => {
    if (!x) return `<tr><td>${i + 1}</td><td colspan="4">检测中…</td></tr>`;
    const targetChannel = esc(x.channel || x.target_channel || '渠道');
    const qualifiedChans = qualifiedChannels(x);
    const anyQualified = qualifiedChans.length > 0;
    const state = !x.ok ? '<span class="err">失败</span>'
      : anyQualified ? `<span class="yes">${qualifiedChans.map(esc).join(', ')} 可用</span>`
        : `<span class="no">未发现 ${targetChannel}</span>`;
    const detailList = Array.isArray(x.channel_details) ? x.channel_details : [];
    const availableList = Array.isArray(x.available_channels) ? x.available_channels : [];
    const channelText = detailList.length
      ? detailList.filter(c => !(c && typeof c === 'object' && c.selected && typeof c.selected === 'object'))
        .map(c => {
          if (typeof c === 'string') return c;
          if (!c || typeof c !== 'object') return String(c ?? '');
          const name = c.name || c.raw_type || c.id || '-', id = c.id && c.id !== name ? ` (${c.id})` : '';
          return `${name}${id}`;
        }).join(', ')
      : availableList.map(c => typeof c === 'string' ? c : (!c || typeof c !== 'object' ? String(c ?? '') : (c.name || c.id || '-'))).join(', ');
    const detail = x.account_email
      ? `<button class="copy detail-btn" data-email="${esc(x.account_email)}" data-token="${esc(x.access_token || '')}" data-submitted="${esc(x.submitted_row || x.access_token || '')}">查看详情</button>`
      : '-';
    return `<tr><td>${i + 1}</td><td>${state}</td><td>${detail}</td><td>${esc(x.checkout_session_id || '-')}</td><td>${esc(x.currency || '-')}</td><td>${esc(channelText || x.evidence || x.error || '-')}</td></tr>`;
  }).join('');
}

async function poll() {
  if (!current) return;
  const r = await fetch(`/api/gcash/batch/${current}`, { cache: 'no-store' }), d = await r.json();
  if (!r.ok) throw Error(d.error || '查询失败');
  render(d);
  if (d.status === 'completed') {
    $('message').textContent = '批量检测完成';
    if (start) start.disabled = false;
    return;
  }
  setTimeout(poll, 1000);
}

const start = $('start');
if (start) start.onclick = async () => {
  const tokens = ($('tokens')?.value || '').trim(),
    proxies = ($('proxies')?.value || '').trim(),
    channelLines = ($('channelProxies')?.value || '').split(/\n/).map(x => x.trim()).filter(Boolean),
    channel_proxies = {};
  for (const line of channelLines) {
    const split = line.indexOf('=');
    if (split < 1) continue;
    const channel = line.slice(0, split).trim().toLowerCase(), proxy = line.slice(split + 1).trim();
    if (channel && proxy) (channel_proxies[channel] ??= []).push(proxy);
  }
  if (!tokens || (!proxies && !Object.keys(channel_proxies).length)) {
    $('message').textContent = '请填写 Token 和目标国家代理';
    return;
  }
  const preset = $('preset').value,
    target_channel = preset === 'custom'
      ? $('customChannel').value.trim()
      : ({ gcash: 'gcash', card: 'card', paypal_uk: 'paypal', ideal_nl: 'ideal', momo_vn: 'momo', gopay_id: 'gopay', upi_in: 'upi', blik_pl: 'blik' }[preset] || preset),
    selected_channels = [...document.querySelectorAll('input[name="channels"]:checked')].map(x => x.value),
    workers = Math.max(1, Math.min(32, Number.parseInt($('workers')?.value || '4', 10) || 4));
  start.disabled = true;
  $('message').textContent = '正在提交…';
  try {
    const r = await fetch('/api/gcash/batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tokens, proxies, channel_proxies, workers,
        with_promo: $('withPromo').checked,
        visitor: false,
        cdk: $('cdk')?.value?.trim() || '',
        preset, target_channel,
        channels: selected_channels,
        presets: { custom: { channel: target_channel, plan: 'plus' } }
      })
    }), d = await r.json();
    if (!r.ok) throw Error(d.error || '提交失败');
    current = d.job_id;
    $('message').textContent = `已提交 ${d.total} 条`;
    poll();
  } catch (e) {
    $('message').textContent = e.message;
    $('start').disabled = false;
  }
};

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
    const button = e.target.closest('#copyModalToken'), text = $('modalToken').value || '';
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
    if (!text) { $('message').textContent = '没有可复制的内容'; return; }
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(() => {
        try { fallback(); done(); } catch (error) { $('message').textContent = error.message; }
      });
    } else {
      try { fallback(); done(); } catch (error) { $('message').textContent = error.message; }
    }
  }
});

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
  if (!tokens.length) { $('message').textContent = '当前没有有资格 Token，请先等待检测完成'; return; }
  copyQualified.disabled = true;
  copyQualified.textContent = `正在复制 ${tokens.length} 条…`;
  try {
    await copyText(tokens.join('\n'));
    $('message').textContent = `已复制 ${tokens.length} 条有资格 Token`;
    copyQualified.textContent = `已复制 ${tokens.length} 条`;
  } catch (e) {
    $('message').textContent = `复制失败：${e.message}`;
    copyQualified.textContent = '复制失败';
  } finally {
    setTimeout(() => { copyQualified.disabled = false; copyQualified.textContent = '复制全部有资格 Token'; }, 1600);
  }
});

$('submitQualified').onclick = async () => {
  const endpoint = $('submitEndpoint').value.trim(), tokens = qualifiedTokens();
  if (!endpoint) { $('message').textContent = '请先填写 API 地址'; return; }
  if (!tokens.length) { $('message').textContent = '当前没有可提交的有资格 Token'; return; }
  const button = $('submitQualified');
  button.disabled = true;
  $('message').textContent = `正在提交 ${tokens.length} 条…`;
  try {
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tokens, count: tokens.length })
    });
    const text = await response.text();
    if (!response.ok) throw Error(`API 返回 ${response.status}${text ? `: ${text.slice(0, 160)}` : ''}`);
    $('message').textContent = `已提交 ${tokens.length} 条有资格 Token`;
    button.textContent = '提交成功';
    setTimeout(() => button.textContent = '提交全部成功 Token', 1600);
  } catch (e) {
    $('message').textContent = `提交失败：${e.message}`;
  } finally {
    button.disabled = false;
  }
};

$('export').onclick = () => {
  const blob = new Blob([JSON.stringify(lastResults, null, 2)], { type: 'application/json' }),
    a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `gcash-results-${Date.now()}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
};
