/**
 * 海外数据看板（里程 + 参数验证整合）。
 *
 * 数据接口：
 *   GET /api/dashboard/mileage?days=&region=
 *   GET /api/dashboard/verify?days=&region=
 *
 * 复用 mileage.js 里的 $ / getJson / fmtCount / currentGlobalRegion 等全局函数。
 */
(function () {
  function $(id) {
    return document.getElementById(id);
  }

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[ch]));
  }

  function fmtKm(meters) {
    const km = Number(meters || 0) / 1000;
    if (km >= 100) return `${km.toFixed(0)} km`;
    if (km >= 10) return `${km.toFixed(1)} km`;
    return `${km.toFixed(2)} km`;
  }

  function fmtPct(value) {
    if (value == null || value === '') return '—';
    const number = Number(value);
    return Number.isFinite(number) ? `${number.toFixed(1)}%` : '—';
  }

  function timeRange() {
    return typeof historyDateRange === 'function'
      ? historyDateRange('dashTimeFilter', 'dashTimeStart', 'dashTimeEnd')
      : { from: '', to: '' };
  }

  function currentRegion() {
    return typeof currentGlobalRegion === 'function' ? currentGlobalRegion() : '';
  }

  function status(text) {
    const el = $('dashStatus');
    if (el) el.textContent = text;
  }

  function gaugeColor(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return '#8b98a8';
    if (number < 50) return '#ff6b6b';
    if (number < 90) return '#ffc15e';
    return '#54d6d2';
  }

  function ratioClass(value) {
    return `style="color:${gaugeColor(value)};font-weight:600"`;
  }

  function renderKpis(cards) {
    const el = $('dashKpis');
    if (!el) return;
    el.innerHTML = cards.map((card) => (
      '<div class="dash-kpi">'
      + `<div class="dash-kpi-label">${esc(card.label)}</div>`
      + `<div class="dash-kpi-value">${esc(card.value)}</div>`
      + `<div class="dash-kpi-sub">${esc(card.sub || '')}</div>`
      + '</div>'
    )).join('');
  }

  function renderGauge(containerId, value, label, foot) {
    const el = $(containerId);
    if (!el) return;
    const number = Number(value);
    const pct = Number.isFinite(number) ? Math.max(0, Math.min(100, number)) : 0;
    const color = gaugeColor(value);
    el.innerHTML = `
      <div class="gauge-ring" style="--pct:${pct};--gauge-color:${color}">
        <div class="gauge-inner">
          <strong style="color:${color}">${fmtPct(value)}</strong>
          <span>${esc(label)}</span>
        </div>
      </div>
      <div class="gauge-foot">${esc(foot || '')}</div>`;
  }

  function renderMileageTables(data) {
    const table = $('dashMileageTable');
    if (table) {
      const regions = data.regions || [];
      table.innerHTML = !regions.length
        ? '<div class="mileage-subtle">还没有里程结果。</div>'
        : '<table class="mini-table"><thead><tr>'
          + '<th>项目</th><th>车型</th><th>天数</th><th>有效里程</th><th>GPS稳定</th>'
          + '<th>有效里程率</th><th>低有效率天</th><th>最近</th>'
          + '</tr></thead><tbody>'
          + regions.map((item) => (
            `<tr><td>${esc(item.region)}</td>`
            + `<td>${esc((item.vehicles || []).join(' / ') || '—')}</td>`
            + `<td>${fmtCount(item.days)}</td>`
            + `<td>${fmtKm(item.effective_m)}</td>`
            + `<td>${fmtKm(item.gps_m)}</td>`
            + `<td ${ratioClass(item.ratio_pct)}>${fmtPct(item.ratio_pct)}</td>`
            + `<td>${fmtCount(item.low_days)}</td>`
            + `<td>${esc(item.last_at || '—')}</td></tr>`
          )).join('')
          + '</tbody></table>';
    }

    const trend = $('dashMileageTrend');
    if (trend) {
      const rows = data.trend || [];
      trend.innerHTML = !rows.length
        ? '<div class="mileage-subtle">暂无趋势数据。</div>'
        : '<table class="mini-table"><thead><tr>'
          + '<th>日期</th><th>天数</th><th>有效里程</th><th>有效里程率</th>'
          + '</tr></thead><tbody>'
          + rows.map((item) => (
            `<tr><td>${esc(item.day)}</td>`
            + `<td>${fmtCount(item.days)}</td>`
            + `<td>${fmtKm(item.effective_m)}</td>`
            + `<td ${ratioClass(item.ratio_pct)}>${fmtPct(item.ratio_pct)}</td></tr>`
          )).join('')
          + '</tbody></table>';
    }
  }

  function verifyResultChip(item) {
    const state = item.state;
    const rejected = Number(item.rejected) || 0;
    if (state === 'done') {
      return rejected > 0
        ? '<span class="confirm-chip is-bad">不通过</span>'
        : '<span class="confirm-chip is-ok">通过</span>';
    }
    if (state === 'partial') return '<span class="confirm-chip">部分确认</span>';
    if (state === 'unknown') return '<span class="confirm-chip">无交付图</span>';
    return '<span class="confirm-chip">待确认</span>';
  }

  function renderVerifyTables(data) {
    const table = $('dashVerifyTable');
    if (table) {
      const regions = data.regions || [];
      table.innerHTML = !regions.length
        ? '<div class="mileage-subtle">还没有参数验证记录。</div>'
        : '<table class="mini-table"><thead><tr>'
          + '<th>项目</th><th>车型</th><th>数据集</th><th>已验证</th><th>通过</th>'
          + '<th>不通过</th><th>待确认</th><th>通过率</th><th>最近</th>'
          + '</tr></thead><tbody>'
          + regions.map((item) => (
            `<tr><td>${esc(item.region)}</td>`
            + `<td>${esc((item.vehicles || []).join(' / ') || '—')}</td>`
            + `<td>${fmtCount(item.datasets)}</td>`
            + `<td>${fmtCount(item.done)}</td>`
            + `<td>${fmtCount(item.passed)}</td>`
            + `<td>${fmtCount(item.rejected)}</td>`
            + `<td>${fmtCount(item.pending)}</td>`
            + `<td ${ratioClass(item.pass_rate_pct)}>${fmtPct(item.pass_rate_pct)}</td>`
            + `<td>${esc(item.last_at || '—')}</td></tr>`
          )).join('')
          + '</tbody></table>';
    }

    const recent = $('dashVerifyRecent');
    if (recent) {
      const rows = data.recent || [];
      recent.innerHTML = !rows.length
        ? '<div class="mileage-subtle">暂无验证记录。</div>'
        : '<table class="mini-table"><thead><tr>'
          + '<th>日期</th><th>项目</th><th>车型</th><th>结果</th><th>更新时间</th>'
          + '</tr></thead><tbody>'
          + rows.map((item) => (
            `<tr><td>${esc(item.day || '—')}</td>`
            + `<td>${esc(item.region || '—')}</td>`
            + `<td>${esc(item.vehicle || '—')}</td>`
            + `<td>${verifyResultChip(item)}</td>`
            + `<td>${esc(item.updated_at || '—')}</td></tr>`
          )).join('')
          + '</tbody></table>';
    }
  }

  async function loadDashboard() {
    status('读取中…');
    const range = timeRange();
    const query = `days=0&region=${encodeURIComponent(currentRegion())}`
      + `&date_from=${encodeURIComponent(range.from)}`
      + `&date_to=${encodeURIComponent(range.to)}`;
    try {
      const [mileage, verify] = await Promise.all([
        getJson(`/api/dashboard/mileage?${query}`),
        getJson(`/api/dashboard/verify?${query}`),
      ]);
      const verifyTotals = verify.totals || {};

      renderKpis([
        { label: '有效里程合计', value: fmtKm(mileage.effective_m), sub: 'GPS稳定 ∩ 2M ∩ 8M' },
        { label: 'GPS 稳定里程', value: fmtKm(mileage.gps_stable_m), sub: '轨迹本身的有效长度' },
        { label: '里程天数', value: fmtCount(mileage.days), sub: `低有效率(<90%) ${fmtCount(mileage.low_ratio_days)} 天` },
        { label: '参数验证数据集', value: fmtCount(verifyTotals.datasets), sub: `待确认 ${fmtCount(verifyTotals.pending)} · 无交付图 ${fmtCount(verifyTotals.no_images)}` },
        { label: '验证通过', value: fmtCount(verifyTotals.passed), sub: `不通过 ${fmtCount(verifyTotals.rejected)} · 已验证 ${fmtCount(verifyTotals.done)}` },
        { label: '参数验证通过率', value: fmtPct(verifyTotals.pass_rate_pct), sub: '通过 / 已验证' },
      ]);

      renderGauge(
        'dashMileageGauge', mileage.ratio_pct, '有效里程率',
        `${fmtKm(mileage.effective_m)} / ${fmtKm(mileage.gps_stable_m)}`
      );
      renderGauge(
        'dashVerifyGauge', verifyTotals.pass_rate_pct, '参数验证通过率',
        `通过 ${fmtCount(verifyTotals.passed)} / 已验证 ${fmtCount(verifyTotals.done)}`
      );

      renderMileageTables(mileage);
      renderVerifyTables(verify);
      status(
        `统计于 ${mileage.generated_at || '—'}${mileage.region ? ` · ${mileage.region}` : ''}`
      );
    } catch (error) {
      status(`读取失败：${error && error.message ? error.message : error}`);
    }
  }

  window.loadDashboard = loadDashboard;

  function bind() {
    const refresh = $('dashRefresh');
    if (refresh) refresh.addEventListener('click', loadDashboard);
    const filter = $('dashTimeFilter');
    if (filter) {
      filter.addEventListener('change', () => {
        if (typeof toggleHistoryRange === 'function') {
          toggleHistoryRange('dashTimeFilter', 'dashTimeStart', 'dashTimeSep', 'dashTimeEnd');
        }
        loadDashboard();
      });
    }
    ['dashTimeStart', 'dashTimeEnd'].forEach((id) => {
      const input = $(id);
      if (input) input.addEventListener('change', loadDashboard);
    });
    const sheet = $('sheetOverseasDash');
    if (sheet && !sheet.hidden) loadDashboard();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
})();
