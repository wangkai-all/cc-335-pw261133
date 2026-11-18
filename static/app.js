'use strict';

const $ = (sel) => document.querySelector(sel);
const state = {
  overview: null,
  report: null,
  reportEpoch: null,
  openCards: new Set(),
  sample: 24,
};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  let body = {};
  try { body = await res.json(); } catch (e) { body = { error: 'invalid response' }; }
  if (!res.ok) throw Object.assign(new Error(body.error || res.statusText), { body, status: res.status });
  return body;
}

function toast(msg, isErr = false) {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast show' + (isErr ? ' err' : '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.className = 'toast'; }, 4200);
}

function esc(v) {
  return String(v == null ? '' : v)
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}
function fmtTime(ts) {
  if (!ts) return '–';
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString('zh-CN', { hour12: false });
}
function fmtAge(ts) {
  if (!ts) return '–';
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return `${s.toFixed(0)} 秒前`;
  if (s < 3600) return `${(s / 60).toFixed(1)} 分钟前`;
  return `${(s / 3600).toFixed(1)} 小时前`;
}
function pct(v) { return v == null ? '–' : `${(v * 100).toFixed(1)}%`; }
function shortLabel(rule) { return rule || '?'; }

function originBadge(origin, status, isCanary) {
  const cls = { baseline: 'baseline', canary: 'canary', promotion: 'promotion', recovery: 'recovery' }[origin] || '';
  const names = { baseline: '基线', canary: '灰度', promotion: '转正', recovery: '恢复' };
  let extra = '';
  if (isCanary && status === 'finalized') extra = ' · 已批准';
  if (status === 'rolled_back') extra = ' · 已回滚';
  if (status === 'closed') extra = ' · 已关闭';
  return `<span class="badge ${cls}">${names[origin] || origin}${extra}</span>`;
}

// ---------------------------------------------------------------------------
// rendering
// ---------------------------------------------------------------------------
function render() {
  const o = state.overview;
  if (!o) return;
  $('#live-dot').className = 'dot' + (o.processes.site.running ? ' live' : '');
  const c = o.counts;
  $('#s-versions').textContent = c.versions ?? '–';
  $('#s-published').textContent = c.published_total ?? '–';
  $('#s-held').textContent = c.held_by_canary ?? '–';
  $('#s-ready').textContent = c.ready_unpublished ?? '–';
  const active = o.active_epoch;
  $('#s-rule').textContent = active ? active.rule_label : '无活跃规则';
  $('#s-epoch').textContent = active ? `epoch #${active.id} · ${active.origin}` : '';
  $('#s-ready-hint').textContent = c.ready_unpublished > 5 ? '解析快于发布？点击“立即发布一轮”' : '';

  renderEpochs(o);
  renderCanary(o);
  renderParsers(o);
  renderProcesses(o);
  renderCheckpoints(o);
  renderEvents(o);
  renderInventory(o);
  const site = o.processes.site;
  $('#site-variant').textContent = site.variant || '–';
  $('#site-edition').textContent = site.edition ?? '–';
  $('#site-items').textContent = site.items ?? '–';
  $('#site-requests').textContent = site.requests ?? '–';
  $('#clock').textContent = '更新于 ' + fmtTime(o.at);
}

function renderEpochs(o) {
  const rows = o.epochs.map((ep) => {
    const counts = `${ep.parsed_pages} 页已解析 / ${ep.published_versions} 版本已发布`;
    return `<tr>
      <td>#${ep.id}</td>
      <td>${originBadge(ep.origin, ep.status, ep.is_canary)}</td>
      <td>${esc(ep.rule_label)}</td>
      <td class="num">${ep.sample_percent}%</td>
      <td>${esc(ep.status)}</td>
      <td>${counts}</td>
      <td class="muted">${fmtTime(ep.opened_at)} → ${ep.closed_at ? fmtTime(ep.closed_at) : '进行中'}</td>
    </tr>`;
  }).join('');
  $('#epoch-table').innerHTML = `<table>
    <thead><tr><th>#</th><th>类型</th><th>规则</th><th class="num">抽样</th><th>状态</th><th>覆盖</th><th>时间</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="7" class="muted">尚无纪元；点击上方“启动全部进程”。</td></tr>'}</tbody>
  </table>`;
}

function renderCanary(o) {
  const body = $('#canary-body');
  const canary = o.canary;
  if (canary) {
    const summary = state.report && state.report.summary;
    const st = summary ? summary.status : 'unknown';
    const stText = { pass: '对照通过', review: '需人工复核', fail: '对照未通过', unknown: '尚未对照' }[st];
    const stCls = { pass: 'pass', review: 'review', fail: 'fail' }[st] || '';
    const loss = (summary?.field_loss || []).map((f) => `<li><b>${esc(f.field)}</b> 在 ${f.pages_lost} 个页面上新规则为空</li>`).join('');
    const rowIssues = summary?.row_issue_kinds?.length
      ? `<li>列表结构问题：${summary.row_issue_kinds.map(esc).join('、')}（${summary.row_issue_count} 处）</li>` : '';
    const coverage = summary?.coverage || {};
    body.innerHTML = `<div class="canary-box">
      <div class="summary-box ${stCls}">
        <div class="kv">
          <span class="k">灰度纪元</span><span>#${canary.id} · ${esc(canary.rule_label)} · ${canary.sample_percent}% 商品页</span>
          <span class="k">对照结论</span><span><b>${stText}</b> · 抽样 ${summary ? summary.sample_size : '–'} 页 / 配对 ${coverage.paired_versions ?? '–'} 版本</span>
          <span class="k">切换边界</span><span>fetch #${coverage.boundary_fetch_id ?? '–'} 之前抓到的版本全部纳入对照</span>
          <span class="k">暂扣发布</span><span>${canary.published_versions || 0} 个版本已入库（只有已批准的纪元可写入）</span>
        </div>
        ${loss || rowIssues ? `<ul class="plain">${loss}${rowIssues}</ul>` : ''}
      </div>
      <div class="actions-row">
        <button class="btn primary" id="btn-approve">批准并转正</button>
        <button class="btn ghost" id="btn-rollback">回滚到旧规则</button>
        <button class="btn ghost" id="btn-refresh-inline">重新抽样对照</button>
      </div>
      <div class="scope-note">批准后：暂扣版本与已处理但未发布的版本都会用新规则补齐，且不会与已发布版本重复计数。</div>
    </div>`;
    $('#btn-approve').onclick = approve;
    $('#btn-rollback').onclick = rollback;
    $('#btn-refresh-inline').onclick = () => refreshReport(true);
    return;
  }
  const rules = o.rules.filter((r) => r.label !== 'parse-old');
  const options = rules.map((r) => `<option value="${esc(r.label)}">${esc(r.label)}</option>`).join('') ||
    '<option value="parse-new">parse-new</option>';
  const activeOrigin = o.active_epoch?.origin;
  body.innerHTML = `<div class="canary-box">
    <div class="slider-row">
      <input type="range" id="pct-range" min="5" max="100" step="5" value="30">
      <span class="pct" id="pct-label">30%</span>
    </div>
    <div class="field-row"><label>候选规则</label><select id="canary-rule">${options}</select></div>
    <div class="scope-note">按页面身份做稳定抽样：同一页面不会一半在灰度、一半在旧规则。列表页始终抓取，以便发现新商品页。</div>
    <div class="actions-row"><button class="btn primary" id="btn-start-canary">开灰度</button></div>
    <div class="scope-note">当前活跃：${activeOrigin ? esc(activeOrigin) : '无'}。灰度期间旧规则继续发布抽样范围外的页面。</div>
  </div>`;
  $('#pct-range').oninput = (e) => { $('#pct-label').textContent = `${e.target.value}%`; };
  $('#btn-start-canary').onclick = startCanary;
}

function renderParsers(o) {
  const live = o.processes.processes.filter((p) => p.kind === 'parser');
  const inst = o.instances;
  const cards = live.map((p) => {
    const prog = o.progress.find((x) => x.rule === p.rule) || {};
    const done = prog.processed || 0;
    const total = prog.total || 1;
    const pctDone = Math.min(100, Math.round((done / total) * 100));
    const lastInst = inst.find((i) => i.id === p.name) || {};
    const status = p.alive ? '<span class="badge ok">运行中</span>'
      : (p.expected_stop ? '<span class="badge warn">已停止</span>' : '<span class="badge bad">已中断</span>');
    return `<div class="parser-card">
      <div class="row1"><div><b>${esc(p.rule)}</b> <span class="muted">· ${esc(p.name)} · pid ${p.pid ?? '–'}</span></div>${status}</div>
      <div class="progress ${pctDone >= 100 ? 'done' : ''}"><i style="width:${pctDone}%"></i></div>
      <div class="meta">
        <span>进度 ${done}/${total} 页版本</span>
        <span>最后处理版本 #${prog.last_processed_fetch_id || '–'}${prog.next_fetch_id ? ` · 下一版 #${prog.next_fetch_id}` : ' · 已追平'}</span>
        <span>错误 ${lastInst.error_count ?? 0}</span>
      </div>
    </div>`;
  }).join('');
  const crashed = inst.filter((i) => i.status === 'crashed' && !live.some((p) => p.name === i.id));
  const crashedCards = crashed.map((i) => `<div class="parser-card">
      <div class="row1"><div><b>${esc(i.id)}</b> <span class="muted">· pid ${i.pid}</span></div>
      <span class="badge bad">已中断</span></div>
      <div class="meta"><span>最后处理版本 #${i.last_fetch_id}</span><span>已处理 ${i.parsed_count}</span></div>
      ${i.last_error ? `<div class="muted">${esc(i.last_error.split('\n').slice(-2).join(' '))}</div>` : ''}
    </div>`).join('');
  $('#parsers').innerHTML = cards + crashedCards ||
    '<p class="muted">没有解析进程在运行。</p>';
}

function renderProcesses(o) {
  const rows = o.processes.processes.map((p) => `<tr>
      <td>${esc(p.name)}</td><td>${esc(p.kind)}</td>
      <td>${p.alive ? '<span class="badge ok">运行</span>' : '<span class="badge bad">退出</span>'}</td>
      <td class="num">${p.pid ?? '–'}</td>
      <td class="muted">${p.exit_code == null ? '–' : p.exit_code}</td>
      <td class="muted">${fmtAge(p.started_at)}</td>
    </tr>`).join('');
  $('#processes').innerHTML = `<table><thead><tr><th>名称</th><th>类型</th><th>状态</th><th class="num">PID</th><th>退出码</th><th>启动</th></tr></thead><tbody>${rows || '<tr><td colspan="6" class="muted">未启动</td></tr>'}</tbody></table>`;
}

function renderCheckpoints(o) {
  const rows = o.checkpoints.map((c) => `<tr>
      <td>#${c.id}</td><td>${esc(c.rule)}</td>
      <td>${esc(c.note)}</td>
      <td class="num">${c.processed}/${c.total_versions}</td>
      <td class="num">${c.latest_fetch_id}</td>
      <td class="muted">${fmtTime(c.at)}</td></tr>`).join('');
  $('#checkpoints').innerHTML = `<table><thead><tr><th>#</th><th>规则</th><th>边界</th><th class="num">已解析</th><th class="num">最后版本</th><th>时间</th></tr></thead><tbody>${rows || '<tr><td colspan="6" class="muted">尚无检查点</td></tr>'}</tbody></table>`;
}

function renderEvents(o) {
  const rows = o.events.map((e) => {
    let cls = '';
    if (e.kind.includes('crash') || e.kind.includes('fail') || e.kind.includes('error')) cls = 'bad';
    if (e.kind.includes('approved') || e.kind.includes('publish')) cls = 'ok';
    const rej = e.kind === 'publish_drain' && e.rejected ? ` <span class="reject">(${e.rejected} 次拒绝)</span>` : '';
    return `<div class="event"><span class="t">${fmtTime(e.at)}</span><span class="kind ${cls}">${esc(e.kind)}</span><span>${esc(e.message)}${rej}</span></div>`;
  }).join('');
  $('#events').innerHTML = rows || '<p class="muted">暂无事件</p>';
}

function renderInventory(o) {
  const items = o.inventory.filter((r) => r.data.kind === 'item');
  const rows = items.map((r) => {
    const f = r.data.fields || {};
    return `<tr>
      <td><b>${esc(r.page_id)}</b></td>
      <td>${esc(f.title)}</td>
      <td class="num">${esc(f.price)}</td>
      <td>${esc(f.brand)}</td>
      <td class="num">${esc(f.sku)}</td>
      <td class="num">${esc(f.stock)}</td>
      <td><span class="badge">${esc(r.rule)}</span></td>
      <td class="muted">v${r.fetch_id} · ${fmtAge(r.updated_at)}</td>
    </tr>`;
  }).join('');
  $('#inventory').innerHTML = `<table><thead><tr><th>页面</th><th>标题</th><th class="num">价格</th><th>品牌</th><th class="num">SKU</th><th class="num">库存</th><th>规则</th><th>版本</th></tr></thead><tbody>${rows || '<tr><td colspan="8" class="muted">尚无已发布条目</td></tr>'}</tbody></table>`;
}

// ---------------------------------------------------------------------------
// diff report
// ---------------------------------------------------------------------------
function renderReport(report) {
  state.report = report;
  if (!report || report.error) {
    $('#report-meta').innerHTML = `<span>${esc(report?.error || '暂无对照')}</span>`;
    $('#field-table').innerHTML = '';
    $('#row-issues').innerHTML = '';
    $('#page-diff').innerHTML = '';
    return;
  }
  const s = report.summary;
  const cov = report.coverage;
  const stName = { pass: '通过', review: '待复核', fail: '未通过' }[s.status] || s.status;
  $('#report-meta').innerHTML = `
    <span>结论 <b>${stName}</b></span>
    <span>抽样 <b>${s.sample_size}</b> 页 / 配对 <b>${cov.paired_versions}</b> 版本</span>
    <span>边界 fetch <b>#${cov.boundary_fetch_id}</b></span>
    <span>缺失（新规则无文档）<b>${s.missing_in_new}</b></span>
    <span>字段一致率下限 <b>${pct(s.min_field_agreement)}</b></span>
    <span>列表结构问题 <b>${s.row_issue_count}</b></span>`;

  const fields = report.fields.filter((f) => !f.field.startsWith('__'));
  const rows = fields.map((f) => {
    const lost = f.old_present - f.new_present;
    const ag = f.agreement;
    const barCls = ag == null ? '' : (ag > 0.999 ? '' : (ag > .95 ? 'mid' : 'low'));
    const bar = ag == null ? '<span class="muted">仅一侧有值</span>'
      : `<span class="agree ${barCls}"><i style="width:${Math.round(ag * 100)}%"></i></span>${pct(ag)}`;
    return `<tr>
      <td>${esc(f.field)}</td>
      <td class="num">${f.old_present}</td>
      <td class="num">${f.new_present}</td>
      <td class="num ${lost > 0 ? 'miss' : ''}">${lost > 0 ? '-' + lost : (lost < 0 ? '+' + (-lost) : '0')}</td>
      <td>${bar}</td>
      <td class="muted">${(f.examples || []).slice(0, 1).map((ex) => `${esc(ex.url || '')}${ex.key ? '#' + esc(ex.key) : ''}: 「${esc(ex.old)}」→「${esc(ex.new)}」`).join('')}</td>
    </tr>`;
  }).join('');
  $('#field-table').innerHTML = `<table>
    <thead><tr><th>字段</th><th class="num">旧规则有值</th><th class="num">新规则有值</th><th class="num">增减</th><th>一致率</th><th>示例</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="6" class="muted">没有可比较的字段</td></tr>'}</tbody></table>`;

  const issues = (report.row_issues || []).slice(0, 6).map((r) => {
    if (r.kind === 'duplicate_keys') {
      return `<li><b>列表拆行/重复键</b> ${esc(r.url)}：旧规则 ${r.old_rows} 行（重复键 ${r.old_duplicates}），新规则 ${r.new_rows} 行（重复键 ${r.new_duplicates}）</li>`;
    }
    if (r.kind === 'row_count') {
      return `<li><b>行数变化</b> ${esc(r.url)}：旧 ${r.old_rows} 行 → 新 ${r.new_rows} 行</li>`;
    }
    if (r.kind === 'key_set') {
      return `<li><b>键集合变化</b> ${esc(r.url)}：仅旧 ${esc(JSON.stringify(r.only_old))}；仅新 ${esc(JSON.stringify(r.only_new))}</li>`;
    }
    return `<li>${esc(r.kind)} ${esc(r.url)}</li>`;
  }).join('');
  $('#row-issues').innerHTML = issues
    ? `<h3 style="margin-top:14px">列表结构差异</h3><ul class="plain">${issues}</ul>`
    : '<p class="muted" style="margin-top:10px">列表行结构一致。</p>';

  renderPageDiff(report);
}

function renderPageDiff(report) {
  const pages = report.pages || [];
  const container = $('#page-diff');
  container.innerHTML = pages.map((p) => {
    const open = state.openCards.has(p.fetch_id);
    if (p.kind === 'list') {
      const summary = `列表页 · 旧 ${p.old_rows.length} 行 / 新 ${p.new_rows.length} 行${p.row_count_eq ? '' : ' ⚠'}`;
      return `<div class="page-card ${open ? 'open' : ''}" data-fid="${p.fetch_id}">
        <header><span><span class="pid">${esc(p.page_id)}</span> <span class="muted">${esc(p.url)}</span></span><span class="muted">${summary}</span></header>
        <div class="body">${rowsHtml(p)}</div></div>`;
    }
    const diffFields = ['title', 'price', 'brand', 'sku', 'stock', 'description'];
    const diffs = diffFields.filter((f) => String(p.old_fields[f] || '') !== String(p.new_fields[f] || ''));
    const badge = diffs.length ? `<span class="badge bad">${diffs.length} 字段不一致</span>` : '<span class="badge ok">一致</span>';
    return `<div class="page-card ${open ? 'open' : ''}" data-fid="${p.fetch_id}">
      <header><span><span class="pid">${esc(p.page_id)}</span> <span class="muted">${esc(p.url)}</span></span><span>${badge}</span></header>
      <div class="body">
        <table class="diff-table"><thead><tr><th>字段</th><th>旧规则</th><th>新规则</th></tr></thead><tbody>
        ${diffFields.map((f) => {
          const ov = String(p.old_fields[f] ?? '');
          const nv = String(p.new_fields[f] ?? '');
          const changed = ov !== nv;
          const flat = (!nv && ov) ? 'flat' : '';
          return `<tr><td>${f}</td><td class="old ${changed ? 'changed' : ''}">${esc(ov) || '<span class="miss">空</span>'}</td>
            <td class="new ${changed ? 'changed' : ''} ${flat}">${esc(nv) || '<span class="miss">空</span>'}</td></tr>`;
        }).join('')}
        </tbody></table>
      </div></div>`;
  }).join('') || '<p class="muted">没有可对照页面。</p>';
  container.querySelectorAll('.page-card > header').forEach((h) => {
    h.onclick = () => {
      const card = h.parentElement;
      const fid = Number(card.dataset.fid);
      card.classList.toggle('open');
      if (card.classList.contains('open')) state.openCards.add(fid); else state.openCards.delete(fid);
    };
  });
}

function rowsHtml(p) {
  const line = (r, side) => {
    const f = r.fields || {};
    const missing = !f.title || !f.price || !f.url;
    return `<div class="${missing ? 'miss' : ''}">${esc(r.key || '?')}: ${esc(f.title) || '∅'} · ${esc(f.price) || '∅'}${f.url ? '' : ' · 无链接'}</div>`;
  };
  return `<div class="rows-cols">
    <div><b class="muted">旧规则行 (${p.old_rows.length})</b><pre>${p.old_rows.map((r) => line(r, 'old')).join('\n') || '（空）'}</pre></div>
    <div><b class="muted">新规则行 (${p.new_rows.length})</b><pre>${p.new_rows.map((r) => line(r, 'new')).join('\n') || '（空）'}</pre></div>
  </div>`;
}

// ---------------------------------------------------------------------------
// actions
// ---------------------------------------------------------------------------
async function refreshOverview() {
  try {
    state.overview = await api('/api/overview');
    render();
    const canary = state.overview.canary;
    const sig = canary ? `${canary.id}:${canary.parsed_pages}:${state.overview.counts.versions}` : '';
    if (canary && (!state.report || state.reportSig !== sig)) {
      state.reportSig = sig;
      await refreshReport(false, true);
    } else if (!canary && state.report) {
      state.report = null;
      renderReport(null);
    }
  } catch (e) {
    toast('刷新失败：' + e.message, true);
  }
}

async function refreshReport(save = false, quiet = false) {
  try {
    const epoch = state.overview?.canary?.id;
    if (!epoch) {
      if (!quiet) toast('当前没有打开的灰度，无法对照');
      if (state.report) { state.report = null; renderReport(null); }
      return;
    }
    let data;
    if (save) {
      const r = await api('/api/report/refresh', { method: 'POST', body: JSON.stringify({ epoch_id: epoch, sample: state.sample }) });
      data = r.report;
    } else {
      data = await api(`/api/report?epoch_id=${epoch}&sample=${state.sample}`);
    }
    renderReport(data);
    if (!quiet) toast('已重新抽样 ' + data.summary.sample_size + ' 页');
  } catch (e) {
    if (!quiet) toast('对照失败：' + e.message, true);
  }
}

async function startCanary() {
  const percent = Number($('#pct-range')?.value || 30);
  const rule = $('#canary-rule')?.value || 'parse-new';
  try {
    if (rule !== 'parse-new') {
      await api('/api/rules/start-draft', { method: 'POST', body: JSON.stringify({ label: rule, percent }) });
      // the canary will start on that label after the parser has some coverage
      toast('已启动 ' + rule + ' 解析进程；稍等几秒再开灰度');
      await new Promise((r) => setTimeout(r, 1200));
      const ov = await api('/api/overview');
      const ruleRow = ov.rules.find((x) => x.label === rule);
      if (!ruleRow) throw new Error('规则未注册');
      await api('/api/canary/start', { method: 'POST', body: JSON.stringify({ percent, rule_id: ruleRow.id }) });
    } else {
      await api('/api/canary/start', { method: 'POST', body: JSON.stringify({ percent }) });
    }
    toast('灰度已开启');
    await refreshOverview();
    await refreshReport(true);
  } catch (e) {
    toast('开灰度失败：' + (e.body?.error || e.message), true);
  }
}

async function approve() {
  if (!confirm('批准该灰度并把未发布的版本全部转正？已发布的版本不会重复入库。')) return;
  try {
    const r = await api('/api/canary/approve', { method: 'POST', body: JSON.stringify({}) });
    toast(`已转正：纪元 #${r.epoch_id}，补齐发布 ${r.drain.published} 个版本`);
    state.report = null;
    await refreshOverview();
  } catch (e) {
    if (e.status === 409 && e.body?.blocked) {
      const s = e.body.summary;
      const loss = (s.field_loss || []).map((f) => `${f.field}(-${f.pages_lost})`).join('、');
      const msg = '对照未通过，已阻止转正：' + (loss || (s.row_issue_kinds || []).join('、'));
      if (confirm(msg + '\n\n仍要强制转正吗？')) {
        try {
          const forced = await api('/api/canary/approve', { method: 'POST', body: JSON.stringify({ force: true, note: 'forced override' }) });
          toast(`已强制转正，补齐发布 ${forced.drain.published} 个版本`);
          state.report = null;
          await refreshOverview();
        } catch (e2) { toast('强制转正失败：' + (e2.body?.error || e2.message), true); }
      } else {
        toast(msg, true);
      }
      return;
    }
    toast('批准失败：' + (e.body?.error || e.message), true);
  }
}

async function rollback() {
  if (!confirm('回滚灰度？已处理但未发布的新页面会用旧规则重新发布；已发布版本保持不变。')) return;
  try {
    const r = await api('/api/canary/rollback', { method: 'POST', body: JSON.stringify({}) });
    toast(`已回滚：恢复纪元 #${r.epoch_id} 补齐 ${r.drain.published} 个版本`);
    state.report = null;
    await refreshOverview();
  } catch (e) {
    toast('回滚失败：' + (e.body?.error || e.message), true);
  }
}

async function simulateCrash() {
  try {
    const r = await api('/api/parser/crash', { method: 'POST', body: JSON.stringify({ name: 'parse-new' }) });
    toast(r.killed ? '已向解析进程发送 SIGKILL' : '未能中断解析进程', !r.killed);
    await refreshOverview();
  } catch (e) { toast('中断失败：' + e.message, true); }
}

async function recoverParser() {
  try {
    const r = await api('/api/parser/restart', { method: 'POST', body: JSON.stringify({ name: 'parse-new', rule: 'parse-new' }) });
    toast('解析进程已重启，进度将从已持久化的解析结果续跑');
    await refreshOverview();
  } catch (e) { toast('恢复失败：' + e.message, true); }
}

async function tickPublisher() {
  try {
    const r = await api('/api/publisher/tick', { method: 'POST', body: JSON.stringify({}) });
    toast(`发布 ${r.drain.published} 个版本，拒绝 ${r.drain.rejected} 个`);
    await refreshOverview();
  } catch (e) { toast('发布失败：' + e.message, true); }
}

async function startServices() {
  try {
    await api('/api/service/start', { method: 'POST', body: JSON.stringify({}) });
    toast('已启动站点、抓取、两个解析进程与发布器');
    await refreshOverview();
  } catch (e) { toast('启动失败：' + e.message, true); }
}

function bindTools() {
  $('#btn-services').onclick = startServices;
  $('#btn-tick').onclick = tickPublisher;
  $('#btn-refresh-report').onclick = () => refreshReport(true);
  $('#btn-crash').onclick = simulateCrash;
  $('#btn-recover').onclick = recoverParser;
  $('#sample-select').onchange = (e) => { state.sample = Number(e.target.value); refreshReport(false); };

  $('#btn-set-variant').onclick = async () => {
    const variant = $('#tool-variant').value;
    try {
      await api('/api/site/variant', { method: 'POST', body: JSON.stringify({ variant }) });
      toast('版面已切换到 ' + variant);
      await refreshOverview();
    } catch (e) { toast(e.message, true); }
  };
  $('#btn-add-series').onclick = async () => {
    const letter = $('#tool-letter').value.trim();
    try {
      const r = await api('/api/site/series', { method: 'POST', body: JSON.stringify({ letter }) });
      $('#tool-out').textContent = '新增：' + r.created.join(', ') + '\n分类页：' + JSON.stringify(r.lists);
      toast('新增 ' + r.created.length + ' 个商品页');
      await refreshOverview();
    } catch (e) { toast(e.message, true); }
  };
  $('#btn-set-price').onclick = async () => {
    const page_id = $('#tool-page').value.trim();
    const price = $('#tool-price').value.trim();
    try {
      await api('/api/site/price', { method: 'POST', body: JSON.stringify({ page_id, price }) });
      toast(`已改价 ${page_id} → ${price}`);
      await refreshOverview();
    } catch (e) { toast(e.message, true); }
  };
  $('#btn-draft').onclick = async () => {
    try {
      const r = await api('/api/rules/start-draft', { method: 'POST', body: JSON.stringify({}) });
      toast('已装入 ' + r.label + ' 并启动解析进程');
      await refreshOverview();
    } catch (e) { toast(e.message, true); }
  };
  $('#btn-canary-draft').onclick = async () => {
    try {
      await api('/api/rules/start-draft', { method: 'POST', body: JSON.stringify({}) });
      await new Promise((r) => setTimeout(r, 1500));
      await api('/api/canary/start', { method: 'POST', body: JSON.stringify({ percent: 40 }) });
      toast('已用 parse-draft 开灰度');
      await refreshOverview();
      await refreshReport(true);
    } catch (e) { toast('失败：' + (e.body?.error || e.message), true); }
  };
  $('#btn-seed').onclick = async () => {
    try {
      const r = await api('/api/sim/seed-canary', { method: 'POST', body: JSON.stringify({}) });
      toast('资料站切到 v2 并新增系列 b；灰度已开启');
      await refreshOverview();
      await refreshReport(true);
    } catch (e) { toast(e.message, true); }
  };
}

bindTools();
refreshOverview();
setInterval(refreshOverview, 2500);
