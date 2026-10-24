/* 有效里程独立页面逻辑（mileage.html）。
 *
 * 数据全部来自后端：
 *   POST /api/mileage       单日目录 → 四类里程 + 覆盖区间（含 timeline）
 *   POST /api/mileage/scan  父目录 → 日期目录预检（不解析 pcap）
 * 批量刷写由前端逐天调用 /api/mileage，这样每天都有真实进度，也不会被
 * 单个超长请求拖死。计算全部在后端：轨迹解析用原项目 parse_ap18_pcap（GSOF49），
 * 里程与覆盖口径用 mileage/ 里的计算器。
 */
'use strict';

// ---- 基础工具 --------------------------------------------------------------

const $ = (id) => document.getElementById(id);
const finite = (value) => Number.isFinite(value);

const COLOR = {
  cyan: '#54d6d2',
  blue: '#64a8ff',
  green: '#72dc9b',
  purple: '#a692ff',
  amber: '#f3bb67',
  red: '#ff7e8b',
  text: '#e9efff',
  muted: '#9cabc7',
  line: '#2b3a5a',
  lane: '#151f36',
};

const state = {
  single: null,
  busy: false,
  batchDays: [],
  batchSelected: new Set(),
  batchResults: [],
  batchBusy: false,
  batchRoot: '',
  historyItems: [],
  historyBusy: false,
  historyLimit: 10,
  historyOffset: 0,
  historyTotal: 0,
  tab: 'single',
};

// ---- sheet（标签页）切换 ----------------------------------------------------

const SHEET_NAMES = ['overseasDash', 'single', 'batch', 'verify', 'batchVerify'];

function sheetId(prefix, key) {
  return prefix + key.charAt(0).toUpperCase() + key.slice(1);
}

function switchTab(tab) {
  const name = SHEET_NAMES.includes(tab) ? tab : 'overseasDash';
  state.tab = name;
  SHEET_NAMES.forEach((key) => {
    const sheet = $(sheetId('sheet', key));
    if (sheet) sheet.hidden = key !== name;
    const button = $(sheetId('tab', key));
    if (button) {
      button.classList.toggle('is-active', key === name);
      button.setAttribute('aria-selected', key === name ? 'true' : 'false');
    }
  });
  try { localStorage.setItem('mileage.tab', name); } catch (_) { /* 忽略 */ }
  requestAnimationFrame(() => redrawCharts());
  // 只加载当前 sheet 需要的列表（切 tab 才请求，避免首屏一次发 5~6 个请求）
  if (typeof loadTabData === 'function') loadTabData(name);
}

function loadTabData(name) {
  if (name === 'overseasDash') {
    if (typeof window.loadDashboard === 'function') window.loadDashboard();
    return;
  }
  if (name === 'single' || name === 'batch') {
    const view = mileageHistoryViews.find((item) => item.key === name);
    if (view) loadHistoryFor(view, true);
    return;
  }
  if (name === 'verify' && typeof window.loadVerifyView === 'function') {
    window.loadVerifyView('verify');
    return;
  }
  if (name === 'batchVerify' && typeof window.loadVerifyView === 'function') {
    window.loadVerifyView('verifyBatch');
  }
}

function esc(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));
}

function setText(id, text) {
  const el = $(id);
  if (el) el.textContent = text;
}

function errorDetail(message) {
  const text = String(message == null ? '' : message).trim();
  return text || '未知错误';
}

function formatBytes(bytes) {
  const value = Number(bytes);
  if (bytes == null || !finite(value) || value < 0) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let size = value;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  const digits = index === 0 || size >= 100 ? 0 : size >= 10 ? 1 : 2;
  return `${size.toFixed(digits)} ${units[index]}`;
}

function fmtKm(meters) {
  const value = Number(meters);
  if (meters == null || !finite(value)) return '—';
  return `${(value / 1000).toFixed(3)} km`;
}

function fmtPct(value) {
  const number = Number(value);
  if (value == null || !finite(number)) return '—';
  return `${number.toFixed(1)}%`;
}

function fmtCount(value) {
  const number = Number(value);
  if (value == null || !finite(number)) return '—';
  return number.toLocaleString();
}

function fmtText(value) {
  return value == null || value === '' ? '—' : esc(value);
}

function fmtDuration(seconds) {
  const value = Number(seconds);
  if (seconds == null || !finite(value)) return '—';
  const total = Math.max(0, Math.round(value));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = total % 60;
  if (hours) return `${hours} 小时 ${minutes} 分`;
  if (minutes) return `${minutes} 分 ${rest} 秒`;
  return `${rest} 秒`;
}

function pad2(value) {
  return String(value).padStart(2, '0');
}

/** epoch 秒 → 本地 HH:MM(:SS)；跨度小于 1 小时时带秒。 */
function fmtClock(seconds, withSeconds) {
  const value = Number(seconds);
  if (!finite(value)) return '—';
  const date = new Date(value * 1000);
  const hh = pad2(date.getHours());
  const mm = pad2(date.getMinutes());
  if (!withSeconds) return `${hh}:${mm}`;
  return `${hh}:${mm}:${pad2(date.getSeconds())}`;
}

function fmtDateTime(seconds) {
  const value = Number(seconds);
  if (!finite(value)) return '—';
  const date = new Date(value * 1000);
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${fmtClock(value, true)}`;
}

// ---- Canvas 基础 -----------------------------------------------------------

function prepareCanvas(canvas) {
  if (!canvas) return null;
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(240, Math.round(rect.width || canvas.clientWidth || 0));
  const height = Math.max(120, Math.round(rect.height || canvas.clientHeight || 0));
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  const ctx = canvas.getContext('2d');
  if (!ctx) return null;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return { ctx, width, height };
}

function drawEmpty(ctx, width, height, text) {
  ctx.fillStyle = COLOR.muted;
  ctx.font = '12px "Segoe UI", "Microsoft YaHei", sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(text, width / 2, height / 2);
}

// ---- 图 1：覆盖里程对比（横向柱状） ----------------------------------------

function drawCoverageBar(mileage) {
  const canvas = $('coverageBarCanvas');
  const prepared = prepareCanvas(canvas);
  if (!prepared) return;
  const { ctx, width, height } = prepared;
  const source = mileage || {};
  const rows = [
    { label: 'GPS 稳定', value: source.gps_stable_m, color: COLOR.cyan },
    { label: '2M 覆盖', value: source.covered_2m_m, color: COLOR.blue },
    { label: '8M 覆盖', value: source.covered_8m_m, color: COLOR.purple },
    { label: '有效里程 三者交集', value: source.effective_m != null ? source.effective_m : source.both_m, color: COLOR.green },
    { label: '任一覆盖 2M∪8M', value: source.any_m, color: COLOR.amber },
    { label: '无覆盖', value: source.no_cover_m, color: COLOR.red },
  ];
  const values = rows.map((row) => Math.max(0, Number(row.value) || 0));
  const maxValue = Math.max(1e-6, ...values);

  const left = 118;
  const right = 96;
  const top = 14;
  const bottom = 10;
  const plotWidth = Math.max(10, width - left - right);
  const rowHeight = (height - top - bottom) / rows.length;
  const barHeight = Math.min(20, rowHeight * 0.62);

  ctx.textBaseline = 'middle';
  ctx.font = '12px "Segoe UI", "Microsoft YaHei", sans-serif';
  rows.forEach((row, index) => {
    const centerY = top + rowHeight * index + rowHeight / 2;
    const barWidth = (values[index] / maxValue) * plotWidth;

    ctx.fillStyle = COLOR.muted;
    ctx.textAlign = 'right';
    ctx.fillText(row.label, left - 10, centerY);

    ctx.fillStyle = COLOR.lane;
    ctx.fillRect(left, centerY - barHeight / 2, plotWidth, barHeight);

    ctx.fillStyle = row.color;
    ctx.fillRect(left, centerY - barHeight / 2, Math.max(2, barWidth), barHeight);

    ctx.fillStyle = COLOR.text;
    ctx.textAlign = 'left';
    ctx.fillText(fmtKm(row.value), left + plotWidth + 10, centerY);
  });
}

// ---- 图 2：覆盖时间轴对比（甘特） ------------------------------------------

function drawCoverageTimeline(timeline) {
  const canvas = $('coverageTimelineCanvas');
  const prepared = prepareCanvas(canvas);
  if (!prepared) return;
  const { ctx, width, height } = prepared;
  const note = $('coverageTimelineNote');

  const data = timeline || {};
  const start = Number(data.start);
  const end = Number(data.end);
  const lanes = [
    { key: 'gps_stable', label: 'GPS 稳定', color: COLOR.cyan },
    { key: '2m', label: '2M 录制', color: COLOR.blue },
    { key: '8m', label: '8M 录制', color: COLOR.purple },
    { key: 'both', label: '全覆盖 2M∩8M', color: COLOR.green },
  ];
  const hasSpans = lanes.some((lane) => Array.isArray(data[lane.key]) && data[lane.key].length > 0);
  if (!finite(start) || !finite(end) || end <= start || !hasSpans) {
    drawEmpty(ctx, width, height, '没有可画的时间轴数据（缺少轨迹或 2M/8M 设备目录）');
    if (note) note.textContent = '无时间轴数据';
    return;
  }

  const left = 108;
  const right = 16;
  const top = 8;
  const bottom = 34;
  const plotWidth = Math.max(10, width - left - right);
  const plotHeight = Math.max(10, height - top - bottom);
  const laneHeight = plotHeight / lanes.length;
  const xOf = (seconds) => left + ((Number(seconds) - start) / (end - start)) * plotWidth;
  const withSeconds = (end - start) < 3600;

  ctx.font = '11px "Segoe UI", "Microsoft YaHei", sans-serif';
  ctx.textBaseline = 'middle';

  lanes.forEach((lane, index) => {
    const laneTop = top + laneHeight * index;
    ctx.fillStyle = index % 2 === 0 ? COLOR.lane : 'rgba(21,31,54,.45)';
    ctx.fillRect(left, laneTop, plotWidth, laneHeight);

    ctx.fillStyle = COLOR.muted;
    ctx.textAlign = 'right';
    ctx.fillText(lane.label, left - 10, laneTop + laneHeight / 2);

    const spans = Array.isArray(data[lane.key]) ? data[lane.key] : [];
    ctx.fillStyle = lane.color;
    spans.forEach((span) => {
      if (!Array.isArray(span) || span.length < 2) return;
      let x1 = xOf(span[0]);
      let x2 = xOf(span[1]);
      if (!finite(x1) || !finite(x2)) return;
      x1 = Math.max(left, x1);
      x2 = Math.min(left + plotWidth, x2);
      if (x2 <= x1) x2 = x1 + 1;
      ctx.fillRect(x1, laneTop + laneHeight * 0.18, x2 - x1, laneHeight * 0.64);
    });
  });

  // 时间刻度
  const ticks = 5;
  ctx.strokeStyle = COLOR.line;
  ctx.fillStyle = COLOR.muted;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  ctx.font = '11px "Segoe UI", "Microsoft YaHei", sans-serif';
  for (let i = 0; i < ticks; i += 1) {
    const ratio = i / (ticks - 1);
    const seconds = start + (end - start) * ratio;
    const x = left + plotWidth * ratio;
    ctx.beginPath();
    ctx.moveTo(x, top);
    ctx.lineTo(x, top + plotHeight);
    ctx.stroke();
    const label = i === 0 ? fmtDateTime(seconds) : fmtClock(seconds, withSeconds);
    ctx.fillText(label, Math.min(Math.max(x, left), left + plotWidth), top + plotHeight + 8);
  }

  if (note) {
    const counts = lanes.map((lane) => `${lane.label} ${(data[lane.key] || []).length} 段`).join(' · ');
    note.textContent = `时间范围 ${fmtDateTime(start)} ~ ${fmtDateTime(end)}（${fmtDuration(end - start)}）· ${counts}`;
  }
}

// ---- 图 3：批量刷写结果对比（纵向分组柱状） --------------------------------

function drawBatchBar(results) {
  const canvas = $('batchBarCanvas');
  const prepared = prepareCanvas(canvas);
  if (!prepared) return;
  const { ctx, width, height } = prepared;
  const ok = (results || []).filter((day) => day && day.ok);
  if (!ok.length) {
    drawEmpty(ctx, width, height, '没有成功的刷写结果');
    return;
  }
  const series = [
    { key: 'effective_m', label: '有效里程 三者交集', color: COLOR.green },
    { key: 'any_m', label: '任一覆盖 2M∪8M', color: COLOR.amber },
  ];
  const values = [];
  ok.forEach((day) => {
    series.forEach((item) => {
      values.push(Number(((day.mileage || {})[item.key]) || 0));
    });
  });
  const maxValue = Math.max(1e-6, ...values);

  const left = 56;
  const right = 14;
  const top = 26;
  const bottom = 34;
  const plotWidth = Math.max(10, width - left - right);
  const plotHeight = Math.max(10, height - top - bottom);
  const groupWidth = plotWidth / ok.length;
  const barWidth = Math.max(3, Math.min(22, groupWidth * 0.3));

  // 图例
  ctx.textBaseline = 'middle';
  ctx.font = '11px "Segoe UI", "Microsoft YaHei", sans-serif';
  let legendX = left;
  series.forEach((item) => {
    ctx.fillStyle = item.color;
    ctx.fillRect(legendX, 6, 10, 8);
    ctx.fillStyle = COLOR.muted;
    ctx.textAlign = 'left';
    ctx.fillText(item.label, legendX + 14, 10);
    legendX += 14 + ctx.measureText(item.label).width + 18;
  });

  // Y 轴与网格
  ctx.strokeStyle = COLOR.line;
  for (let i = 0; i <= 3; i += 1) {
    const y = top + plotHeight - (plotHeight * i) / 3;
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(left + plotWidth, y);
    ctx.stroke();
    ctx.fillStyle = COLOR.muted;
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    const km = (maxValue * i) / 3 / 1000;
    ctx.fillText(km.toFixed(km > 0 && km < 1 ? 2 : 1), left - 8, y);
  }

  ok.forEach((day, index) => {
    const groupLeft = left + groupWidth * index + groupWidth / 2;
    series.forEach((item, seriesIndex) => {
      const value = Number(((day.mileage || {})[item.key]) || 0);
      const barHeight = (value / maxValue) * plotHeight;
      const x = groupLeft - barWidth - 2 + seriesIndex * (barWidth + 4);
      ctx.fillStyle = item.color;
      ctx.fillRect(x, top + plotHeight - barHeight, barWidth, barHeight);
    });
    ctx.fillStyle = COLOR.muted;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    const label = String(day.date || '').slice(4) || '—';
    ctx.fillText(label, groupLeft, top + plotHeight + 8);
  });
}

// ---- 图例辅助 --------------------------------------------------------------

function redrawCharts() {
  if (state.single) {
    drawCoverageBar(state.single.mileage || {});
    drawCoverageTimeline((state.single.coverage || {}).timeline || {});
  }
  if (state.batchResults.length) drawBatchBar(state.batchResults);
}

// ---- 单日分析渲染 ----------------------------------------------------------

function renderAlerts(id, title, items) {
  const box = $(id);
  if (!box) return;
  const list = (Array.isArray(items) ? items : [])
    .filter((item) => item != null && String(item).trim() !== '');
  box.hidden = list.length === 0;
  box.innerHTML = list.length
    ? `<h4>${esc(title)}（${list.length}）</h4><ul>${list.map((item) => `<li>${esc(item)}</li>`).join('')}</ul>`
    : '';
}

function coverageHtml(coverage) {
  const source = coverage || {};
  const rows = [['2m', '2M'], ['8m', '8M']].map(([key, label]) => {
    const item = source[key] || {};
    const present = item.present === true;
    const folders = present && Array.isArray(item.folders) && item.folders.length
      ? `<div class="mileage-subtle">${item.folders.map(esc).join(' · ')}</div>`
      : '';
    const cells = present
      ? [
          fmtCount(item.file_count),
          formatBytes(item.bytes),
          fmtText(item.start),
          fmtText(item.end),
          fmtDuration(item.duration_s),
          fmtCount(item.interval_count)
        ]
      : ['—', '—', '—', '—', '—', '—'];
    const small = Number(item.ignored_small_files || 0);
    const missingHtml = present
      ? ''
      : (small > 0
        ? `<div class="mileage-subtle">只有 ${small} 个占位文件（&lt;64 KB），已忽略</div>`
        : '<div class="mileage-subtle">未找到</div>');
    const labelHtml = `${esc(label)}${folders}${missingHtml}`;
    return `<tr${present ? '' : ' class="mileage-missing"'}><th>${labelHtml}</th>`
      + cells.map((cell) => `<td>${cell}</td>`).join('') + '</tr>';
  });
  return '<table class="mini-table"><thead><tr>'
    + '<th>覆盖</th><th>文件数</th><th>容量</th><th>开始时间</th><th>结束时间</th><th>时长</th><th>区段数</th>'
    + `</tr></thead><tbody>${rows.join('')}</tbody></table>`;
}

function trajectoryHtml(trajectory) {
  const source = trajectory || {};
  const rows = [
    ['轨迹来源', fmtText(source.source)],
    ['轨迹点数', fmtCount(source.point_count)],
    ['GPS 稳定点数', fmtCount(source.gps_stable_points)],
    ['相邻段数', fmtCount(source.segment_count)],
    ['行程数', fmtCount(source.trip_count)],
    ['时间跨度', `${fmtText(source.start_time)} ~ ${fmtText(source.end_time)}`],
    ['轨迹时长', fmtDuration(source.duration_s)]
  ];
  return `<table class="mini-table"><tbody>${rows
    .map(([label, value]) => `<tr><th>${esc(label)}</th><td>${value}</td></tr>`)
    .join('')}</tbody></table>`;
}

function pcapsHtml(pcaps) {
  const source = pcaps || {};
  const dirs = (Array.isArray(source.dirs) ? source.dirs : [])
    .filter((dir) => dir != null && String(dir) !== '');
  const rows = [
    ['PCAP 文件数', fmtCount(source.file_count)],
    ['总容量', formatBytes(source.bytes)],
    ['所在目录', dirs.length ? dirs.map(esc).join(' · ') : '—']
  ];
  return `<table class="mini-table"><tbody>${rows
    .map(([label, value]) => `<tr><th>${esc(label)}</th><td>${value}</td></tr>`)
    .join('')}</tbody></table>`;
}

function renderResult(data) {
  const mileage = data.mileage || {};
  const trajectory = data.trajectory || {};
  const coverage = data.coverage || {};
  const pcaps = data.pcaps || {};

  // 有效里程放最前面（hero）：GPS 稳定 ∩ 2M 录制 ∩ 8M 录制
  const effectiveM = mileage.effective_m != null ? mileage.effective_m : mileage.both_m;
  const effectiveRatio = mileage.effective_ratio_pct != null
    ? mileage.effective_ratio_pct
    : mileage.both_ratio_pct;
  setText('mileageEffective', fmtKm(effectiveM));
  setText(
    'mileageEffectiveSub',
    `GPS 稳定 ${fmtKm(mileage.gps_stable_m)} · 三者覆盖率 ${fmtPct(effectiveRatio)}`
  );
  $('mileageHeroSide').innerHTML = [
    ['任一覆盖（2M∪8M）', fmtKm(mileage.any_m)],
    ['无覆盖里程', fmtKm(mileage.no_cover_m)],
    ['GPS 稳定段数', fmtCount(mileage.stable_segment_count)]
  ].map(([label, value]) => `<div class="hero-stat"><span>${esc(label)}</span><strong>${value}</strong></div>`).join('');

  setText('mileageGps', fmtKm(mileage.gps_stable_m));
  setText('mileageCover2m', fmtKm(mileage.covered_2m_m));
  setText('mileageCover8m', fmtKm(mileage.covered_8m_m));
  setText('mileageAny', fmtKm(mileage.any_m));

  $('mileageSecondary').innerHTML = [
    ['总轨迹长度', fmtKm(mileage.total_m)],
    ['GPS 稳定段数', fmtCount(mileage.stable_segment_count)],
    ['基線容差', mileage.baseline_tolerance_m == null ? '—' : `${Number(mileage.baseline_tolerance_m).toFixed(2)} m`]
  ].map(([label, value]) => `<span class="chip">${esc(label)} <strong>${value}</strong></span>`).join('');

  $('mileageCoverageTable').innerHTML = coverageHtml(coverage);
  $('mileageTrajectory').innerHTML = trajectoryHtml(trajectory);
  $('mileagePcaps').innerHTML = pcapsHtml(pcaps);
  renderAlerts('mileageWarnings', '提示', data.warnings);
  renderAlerts('mileageIssues', '问题', data.issues);

  const tzOffset = coverage.device_timezone_offset_hours;
  const tzText = Number.isFinite(Number(tzOffset)) && tzOffset != null
    ? ` · 设备文件名按 UTC${Number(tzOffset) >= 0 ? '+' : ''}${Number(tzOffset)} 解释`
    : '';
  const cacheText = data.cached
    ? ` · 来自数据库缓存（计算于 ${data.computed_at || '—'}，点「重算」刷新）`
    : ` · 已写入数据库（${data.computed_at || '—'}）`;
  setText(
    'mileageNote',
    `目录 ${data.data_root || '—'} · 用时 ${fmtDuration(data.elapsed_s)} · `
    + '有效里程 = GPS 稳定 ∩ 2M 录制 ∩ 8M 录制（三者交集）；任一覆盖只作参考'
    + tzText
    + cacheText
  );

  $('mileageResult').hidden = false;
  state.single = data;
  setExportEnabled('exportSingleButton', true);
  // 结果从 hidden 变可见后再画，否则 canvas 取到宽度 0。
  requestAnimationFrame(() => {
    drawCoverageBar(mileage);
    drawCoverageTimeline(coverage.timeline || {});
  });
}

// ---- 单日分析请求 ----------------------------------------------------------

function setStatus(text, tone) {
  const status = $('mileageStatus');
  if (!status) return;
  status.className = `mileage-status${tone ? ` is-${tone}` : ''}`;
  status.textContent = text;
}

function clearResult() {
  const result = $('mileageResult');
  if (result) result.hidden = true;
  state.single = null;
  setExportEnabled('exportSingleButton', false);
  ['mileageEffective', 'mileageEffectiveSub', 'mileageGps', 'mileageCover2m',
    'mileageCover8m', 'mileageAny', 'mileageHeroSide', 'mileageSecondary',
    'mileageCoverageTable', 'mileageTrajectory', 'mileagePcaps', 'mileageNote']
    .forEach((id) => { const el = $(id); if (el) el.innerHTML = ''; });
  ['mileageWarnings', 'mileageIssues'].forEach((id) => {
    const box = $(id);
    if (!box) return;
    box.hidden = true;
    box.innerHTML = '';
  });
}

async function postJson(path, body) {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  let data = null;
  try {
    data = await response.json();
  } catch (parseError) {
    data = null;
  }
  if (!response.ok || !data || data.error) {
    const reason = data && data.error
      ? data.error
      : `服务端返回 ${response.status || '—'}${response.statusText ? ` ${response.statusText}` : ''}`;
    throw new Error(reason);
  }
  return data;
}

async function runAnalysis(forceRefresh) {
  if (state.busy) return;
  const input = $('mileageRootInput');
  const button = $('mileageRunButton');
  const refreshButton = $('mileageRefreshButton');
  const dataRoot = String(input && input.value != null ? input.value : '').trim();
  if (!dataRoot) {
    clearResult();
    setStatus('请先填写服务器上的数据目录，例如 D:\\data\\20260917 或 /mnt/logs_new/...', 'error');
    if (input && input.focus) input.focus();
    return;
  }

  state.busy = true;
  if (button) { button.disabled = true; button.textContent = '分析中…'; }
  if (refreshButton) refreshButton.disabled = true;
  const startedAt = Date.now();
  const refresh = forceRefresh === true;
  setStatus(
    refresh
      ? `正在重新解析 ${dataRoot} …（忽略数据库缓存）`
      : `正在分析 ${dataRoot} … 已算过的天会直接读数据库；未算过时 pcap 较多需要几十秒。`,
    'busy'
  );
  const timer = setInterval(() => {
    setStatus(`正在分析 ${dataRoot} … 已用 ${Math.round((Date.now() - startedAt) / 1000)} 秒`, 'busy');
  }, 1000);

  try {
    const data = await postJson('/api/mileage', { data_root: dataRoot, refresh });
    renderResult(data);
    const warningCount = Array.isArray(data.warnings) ? data.warnings.length : 0;
    const issueCount = Array.isArray(data.issues) ? data.issues.length : 0;
    const head = data.cached
      ? `读取缓存 · 计算于 ${data.computed_at || '—'}`
      : `分析完成 · 已写入数据库（${data.computed_at || '—'}）`;
    setStatus(
      `${head} · 有效里程 ${fmtKm((data.mileage || {}).effective_m != null ? (data.mileage || {}).effective_m : (data.mileage || {}).both_m)} · `
      + `${fmtCount((data.trajectory || {}).point_count)} 点 · 用时 ${Number(data.elapsed_s || 0).toFixed(1)} s`
      + (warningCount ? ` · ${warningCount} 条提示` : '')
      + (issueCount ? ` · ${issueCount} 条问题` : ''),
      'ok'
    );
    loadAllMileageHistory(true);
      } catch (error) {
    clearResult();
    setStatus(`里程分析失败：${errorDetail(error && error.message ? error.message : String(error))}`, 'error');
  } finally {
    clearInterval(timer);
    state.busy = false;
    if (button) { button.disabled = false; button.textContent = '开始分析'; }
    if (refreshButton) refreshButton.disabled = false;
  }
}

// ---- 历史记录（服务端 SQLite，刷新/关页面都不丢） --------------------------

async function getJson(path) {
  const response = await fetch(path, { headers: { Accept: 'application/json' } });
  let data = null;
  try {
    data = await response.json();
  } catch (_) {
    data = null;
  }
  if (!response.ok || !data || data.error) {
    const reason = data && data.error
      ? data.error
      : `服务端返回 ${response.status || '—'}${response.statusText ? ` ${response.statusText}` : ''}`;
    throw new Error(reason);
  }
  return data;
}

// 「全部时间 / 本月 / 上月 / 自定义」→ { from, to }（YYYY-MM-DD；空串=不限）
function historyDateRange(selectId, startId, endId) {
  const select = $(selectId);
  const mode = select ? String(select.value || '') : '';
  const format = (date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  const today = new Date();
  if (mode === 'this-month') {
    return { from: format(new Date(today.getFullYear(), today.getMonth(), 1)), to: format(today) };
  }
  if (mode === 'last-month') {
    return {
      from: format(new Date(today.getFullYear(), today.getMonth() - 1, 1)),
      to: format(new Date(today.getFullYear(), today.getMonth(), 0)),
    };
  }
  if (mode === 'custom') {
    const start = $(startId);
    const end = $(endId);
    return {
      from: start && start.value ? String(start.value) : '',
      to: end && end.value ? String(end.value) : '',
    };
  }
  return { from: '', to: '' };
}

// 只有选「自定义」时才显示开始/结束日期框
function toggleHistoryRange(selectId, startId, sepId, endId) {
  const select = $(selectId);
  const show = Boolean(select && select.value === 'custom');
  [startId, sepId, endId].forEach((id) => {
    const el = $(id);
    if (el) el.hidden = !show;
  });
}

// 库中已有结果：单日 + 批量两个 sheet 各一份，共用一套渲染/分页逻辑
const mileageHistoryViews = [
  {
    key: 'single',
    table: 'historyTable',
    status: 'historyStatus',
    pager: 'historyPager',
    prev: 'historyPrev',
    next: 'historyNext',
    pageInfo: 'historyPageInfo',
    query: 'historyQuery',
    time: 'historyTimeFilter',
    timeStart: 'historyTimeStart',
    timeEnd: 'historyTimeEnd',
  },
  {
    key: 'batch',
    table: 'batchHistoryTable',
    status: 'batchHistoryStatus',
    pager: 'batchHistoryPager',
    prev: 'batchHistoryPrev',
    next: 'batchHistoryNext',
    pageInfo: 'batchHistoryPageInfo',
    query: 'batchHistoryQuery',
    time: 'batchHistoryTimeFilter',
    timeStart: 'batchHistoryTimeStart',
    timeEnd: 'batchHistoryTimeEnd',
  },
];
const mileageHistoryState = {};
mileageHistoryViews.forEach((view) => {
  mileageHistoryState[view.key] = { offset: 0, total: 0, busy: false };
});

function renderHistoryFor(view, items) {
  const stateForView = mileageHistoryState[view.key];
  const container = $(view.table);
  if (!container) return;
  if (!items || !items.length) {
    container.innerHTML = '<div class="mileage-subtle">库里还没有结果：分析一天后会存进这里，刷新页面也不会丢。</div>';
    setText(view.status, stateForView.total ? `共 ${stateForView.total} 条 · 当前筛选为空` : '库中暂无结果');
    const pager = $(view.pager);
    if (pager) pager.hidden = true;
    return;
  }
  const header = '<table class="mini-table"><thead><tr>'
    + '<th>日期</th><th>地区</th><th>有效里程</th><th>GPS 稳定</th><th>2M 覆盖</th><th>8M 覆盖</th>'
    + '<th>有效里程率</th><th>计算时间</th><th>源数据路径</th><th>操作</th>'
    + '</tr></thead><tbody>';
  const rows = items.map((item) => {
    const ratio = item.effective_ratio_pct == null ? '—' : `${Number(item.effective_ratio_pct).toFixed(1)}%`;
    const regionTag = item.region
      ? `<span class="cache-tag">${esc(item.region)}${item.vehicle ? ` · ${esc(item.vehicle)}` : ''}</span>`
      : '—';
    return `<tr>`
      + `<td>${esc(item.day || '—')}</td>`
      + `<td>${regionTag}</td>`
      + `<td><strong>${fmtKm(item.effective_m)}</strong></td>`
      + `<td>${fmtKm(item.gps_stable_m)}</td>`
      + `<td>${fmtKm(item.covered_2m_m)}</td>`
      + `<td>${fmtKm(item.covered_8m_m)}</td>`
      + `<td>${ratio}</td>`
      + `<td>${esc(item.computed_at || '—')}</td>`
      + `<td class="history-root" title="${esc(item.data_root)}"><span class="path-ellipsis">${esc(item.data_root)}</span></td>`
      + `<td><button class="ghost-button history-load" type="button" data-id="${item.id}">载入</button></td>`
      + `</tr>`;
  }).join('');
  container.innerHTML = `${header}${rows}</tbody></table>`;
  container.querySelectorAll('.history-load').forEach((button) => {
    button.addEventListener('click', () => openHistoryRecord(button.getAttribute('data-id')));
  });
  const shown = items.length;
  const total = stateForView.total;
  setText(
    view.status,
    `库中已有 ${total == null ? shown : total} 条`
    + (total && total > shown ? ` · 第 ${(Math.floor(stateForView.offset / state.historyLimit) + 1)}/${Math.ceil(total / state.historyLimit)} 页` : '')
  );
  // 分页条：只在总条数超过一页时显示
  const pager = $(view.pager);
  if (pager) {
    const page = window.MileageCsv && window.MileageCsv.pageState
      ? window.MileageCsv.pageState(total == null ? shown : total, state.historyLimit, stateForView.offset)
      : null;
    if (!page || page.pages <= 1) {
      pager.hidden = true;
    } else {
      pager.hidden = false;
      setText(view.pageInfo, `第 ${page.page}/${page.pages} 页 · 每页 ${page.limit} 条`);
      const prev = $(view.prev);
      const next = $(view.next);
      if (prev) prev.disabled = !page.hasPrev;
      if (next) next.disabled = !page.hasNext;
    }
  }
}

// 首屏固定不带筛选（防止浏览器恢复表单导致看起来"筛选没清掉"）；用户操作后才用界面上的条件
let historyFiltersPrimed = false;

async function loadHistoryFor(view, resetOffset) {
  const stateForView = mileageHistoryState[view.key];
  if (stateForView.busy) return;
  if (resetOffset === true) stateForView.offset = 0;
  stateForView.busy = true;
  const input = $(view.query);
  const query = historyFiltersPrimed
    ? String(input && input.value != null ? input.value : '').trim()
    : '';
  const dateRange = historyFiltersPrimed
    ? historyDateRange(view.time, view.timeStart, view.timeEnd)
    : { from: '', to: '' };
  const region = historyFiltersPrimed ? currentGlobalRegion() : '';
  try {
    const data = await getJson(
      `/api/mileage/history?limit=${state.historyLimit}&offset=${stateForView.offset}`
      + `&q=${encodeURIComponent(query)}`
      + `&region=${encodeURIComponent(region)}`
      + `&date_from=${encodeURIComponent(dateRange.from)}`
      + `&date_to=${encodeURIComponent(dateRange.to)}`
    );
    // offset 越界（例如记录被删/筛选变少）时回退到最后一页重新取一次
    const page = window.MileageCsv && window.MileageCsv.pageState
      ? window.MileageCsv.pageState(data.total, state.historyLimit, stateForView.offset)
      : null;
    if (page && page.offset !== stateForView.offset) {
      stateForView.offset = page.offset;
      stateForView.busy = false;
      await loadHistoryFor(view, true);
      return;
    }
    stateForView.total = Number(data.total) || 0;
    stateForView.items = data.items || [];
    renderHistoryFor(view, stateForView.items);
  } catch (error) {
    setText(view.status, `历史读取失败：${errorDetail(error && error.message ? error.message : String(error))}`);
  } finally {
    stateForView.busy = false;
  }
}

function loadAllMileageHistory(reset) {
  mileageHistoryViews.forEach((view) => loadHistoryFor(view, reset));
}

function clearAllTextInputs() {
  // 页面里所有文本框（搜索框、父目录、数据目录）都不保留上次内容
  document.querySelectorAll('.mileage-page input[type="text"]').forEach((el) => { el.value = ''; });
}

function resetMileageHistoryFilters(reload) {
  historyFiltersPrimed = false;
  clearAllTextInputs();
  const globalSelect = $('globalRegionFilter');
  if (globalSelect) globalSelect.value = '';
  mileageHistoryViews.forEach((view) => {
    const queryInput = $(view.query);
    if (queryInput) queryInput.value = '';
    const timeSelect = $(view.time);
    if (timeSelect) timeSelect.value = '';
    const timeStart = $(view.timeStart);
    if (timeStart) timeStart.value = '';
    const timeEnd = $(view.timeEnd);
    if (timeEnd) timeEnd.value = '';
    toggleHistoryRange(view.time, view.timeStart, view.time.replace('Filter', 'Sep'), view.timeEnd);
  });
  // 有些浏览器会在 load 之后才恢复表单，延迟再清几次
  [300, 1000, 2000].forEach((delay) => {
    window.setTimeout(() => {
      clearAllTextInputs();
      const gs = $('globalRegionFilter');
      if (gs) gs.value = '';
      mileageHistoryViews.forEach((view) => {
        const q = $(view.query);
        if (q) q.value = '';
        const t = $(view.time);
        if (t) t.value = '';
        const ts = $(view.timeStart);
        if (ts) ts.value = '';
        const te = $(view.timeEnd);
        if (te) te.value = '';
        toggleHistoryRange(view.time, view.timeStart, view.time.replace('Filter', 'Sep'), view.timeEnd);
      });
    }, delay);
  });
  if (reload) {
    loadTodo();
    loadTabData(state.tab);
  }
}

// 浏览器刷新/回退恢复表单后，再清一次，保证默认不带上次筛选
window.addEventListener('pageshow', (event) => {
  resetMileageHistoryFilters(Boolean(event.persisted));
});
window.addEventListener('load', () => resetMileageHistoryFilters(false));

async function openHistoryRecord(id) {
  if (!id) return;
  setStatus('正在读取历史结果…', 'busy');
  try {
    const record = await getJson(`/api/mileage/record?id=${encodeURIComponent(id)}`);
    const result = record.result || {};
    result.cached = true;
    result.computed_at = record.computed_at;
    if (result.data_root == null) result.data_root = record.data_root;
    switchTab('single');
    renderResult(result);
    const input = $('mileageRootInput');
    if (input && record.data_root) input.value = record.data_root;
    setStatus(`已载入历史结果（计算于 ${record.computed_at || '—'}）· 有效里程 ${fmtKm((result.mileage || {}).effective_m)}`, 'ok');
    const panel = $('mileageResult');
    if (panel && panel.scrollIntoView) panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (error) {
    setStatus(`读取历史结果失败：${errorDetail(error && error.message ? error.message : String(error))}`, 'error');
  }
}

// ---- 导出 CSV --------------------------------------------------------------

function downloadText(filename, text) {
  // 加 UTF-8 BOM：Windows Excel 直接双击打开不乱码。
  const blob = new Blob(['\ufeff' + text], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function timestampSlug() {
  const now = new Date();
  const pad = (value) => String(value).padStart(2, '0');
  return `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
}

function pathSlug(pathText) {
  const text = String(pathText || '').replace(/[\\/]+$/, '');
  const parts = text.split(/[\\/]/);
  return (parts[parts.length - 1] || 'data').replace(/[^\w.-]+/g, '_');
}

function setExportEnabled(id, enabled) {
  const button = $(id);
  if (button) button.disabled = !enabled;
}

function exportSingle() {
  const data = state.single;
  if (!data) {
    setStatus('还没有结果可导出：先分析一天。', 'error');
    return;
  }
  if (!window.MileageCsv) {
    setStatus('导出脚本未加载（mileage_csv.js），请刷新页面。', 'error');
    return;
  }
  const filename = `mileage_${pathSlug(data.data_root)}_${timestampSlug()}.csv`;
  downloadText(filename, window.MileageCsv.buildSingleCsv(data));
  setStatus(`已导出 ${filename}`, 'ok');
}

function exportBatch() {
  if (!state.batchResults || !state.batchResults.length) {
    setBatchStatus('还没有批量结果可导出：先刷写几天。', 'error');
    return;
  }
  if (!window.MileageCsv) {
    setBatchStatus('导出脚本未加载（mileage_csv.js），请刷新页面。', 'error');
    return;
  }
  const filename = `mileage_batch_${pathSlug(state.batchRoot)}_${timestampSlug()}.csv`;
  downloadText(filename, window.MileageCsv.buildBatchCsv(state.batchResults, state.batchRoot));
  setBatchStatus(`已导出 ${filename}（${state.batchResults.length} 天）`, 'ok');
}

// ---- 批量刷写 --------------------------------------------------------------

function setBatchStatus(text, tone) {
  const status = $('batchStatus');
  if (!status) return;
  status.className = `mileage-status${tone ? ` is-${tone}` : ''}`;
  status.textContent = text;
}

function updateBatchPickInfo() {
  const selected = state.batchSelected.size;
  const total = state.batchDays.length;
  const readyCount = state.batchDays.filter((day) => day.ready).length;
  setText('batchPickInfo', `已选 ${selected} / ${total} 天（可刷 ${readyCount} 天）`);
  const button = $('batchRunButton');
  if (button) button.disabled = state.batchBusy || selected === 0;
}

function renderBatchDays() {
  const container = $('batchDaysTable');
  if (!container) return;
  $('batchPick').hidden = false;
  if (!state.batchDays.length) {
    container.innerHTML = '<div class="mileage-subtle">这个父目录下没有 YYYYMMDD 命名的日期目录。</div>';
    updateBatchPickInfo();
    return;
  }
  const header = '<table class="mini-table"><thead><tr>'
    + '<th class="batch-check"><input type="checkbox" id="batchHeadCheck"></th>'
    + '<th>日期</th><th>地区</th><th>状态</th><th>库里已有</th><th>已存有效里程</th>'
    + '<th>轨迹</th><th>PCAP</th><th>2M 文件</th><th>8M 文件</th><th>2M 起止</th><th>8M 起止</th>'
    + '</tr></thead><tbody>';
  const rows = state.batchDays.map((day) => {
    const checked = state.batchSelected.has(day.date) ? ' checked' : '';
    const disabled = day.ready ? '' : ' disabled';
    const status = day.ready
      ? (day.files_2m && day.files_8m ? '可刷' : (day.files_2m || day.files_8m ? '缺一侧' : '无 2M/8M'))
      : (day.error ? '不可用' : '无轨迹');
    const rowClass = day.ready ? '' : ' class="mileage-missing"';
    const analyzed = day.analyzed === true;
    const cachedCell = analyzed
      ? `<span class="cache-tag">已算 ${esc(String(day.computed_at || '').slice(5, 16))}</span>`
      : '<span class="cache-tag is-new">未算</span>';
    const cachedMileage = analyzed ? fmtKm(day.cached_effective_m) : '—';
    const range2 = day.start_2m ? `${esc(String(day.start_2m).slice(11))}~${esc(String(day.end_2m || '').slice(11))}` : '—';
    const range8 = day.start_8m ? `${esc(String(day.start_8m).slice(11))}~${esc(String(day.end_8m || '').slice(11))}` : '—';
    return `<tr${rowClass}>`
      + `<td class="batch-check"><input type="checkbox" class="batch-day-check" data-date="${esc(day.date)}"${checked}${disabled}></td>`
      + `<td>${esc(day.date)}</td>`
      + `<td>${day.region ? `<span class="cache-tag">${esc(day.region)}</span>` : '—'}</td>`
      + `<td>${esc(status)}</td>`
      + `<td>${cachedCell}</td>`
      + `<td>${cachedMileage}</td>`
      + `<td>${day.has_trajectory ? (day.trajectory_files || '0') : '—'}</td>`
      + `<td>${fmtCount(day.pcap_files)}</td>`
      + `<td>${fmtCount(day.files_2m)}</td>`
      + `<td>${fmtCount(day.files_8m)}</td>`
      + `<td>${range2}</td><td>${range8}</td></tr>`;
  }).join('');
  container.innerHTML = header + rows + '</tbody></table>';

  const head = $('batchHeadCheck');
  if (head) {
    head.checked = state.batchDays.length > 0 && state.batchSelected.size === state.batchDays.filter((d) => d.ready).length;
    head.addEventListener('change', () => {
      state.batchSelected.clear();
      if (head.checked) {
        state.batchDays.filter((day) => day.ready).forEach((day) => state.batchSelected.add(day.date));
      }
      renderBatchDays();
    });
  }
  container.querySelectorAll('.batch-day-check').forEach((box) => {
    box.addEventListener('change', () => {
      const date = box.getAttribute('data-date');
      if (box.checked) state.batchSelected.add(date);
      else state.batchSelected.delete(date);
      updateBatchPickInfo();
    });
  });
  updateBatchPickInfo();
}

async function scanBatch() {
  if (state.batchBusy) return;
  const input = $('batchRootInput');
  const button = $('batchScanButton');
  const root = String(input && input.value != null ? input.value : '').trim();
  if (!root) {
    setBatchStatus('请先填写父目录，例如 D:\\data 或 \\\\192.168.20.206\\logs_new\\...\\202609', 'error');
    if (input && input.focus) input.focus();
    return;
  }
  if (button) { button.disabled = true; button.textContent = '扫描中…'; }
  setBatchStatus(`正在扫描 ${root} …`, 'busy');
  try {
    const data = await postJson('/api/mileage/scan', { data_root: root });
    state.batchRoot = root;
    state.batchDays = Array.isArray(data.dates) ? data.dates : [];
    // 默认只选「可刷 + 库里还没有」的天；已算过的天增量跳过（需更新时勾「强制重算」）
    state.batchSelected = new Set(
      state.batchDays.filter((day) => day.ready && day.analyzed !== true).map((day) => day.date)
    );
    state.batchResults = [];
    $('batchResult').hidden = true;
    setExportEnabled('batchExportButton', false);
    renderBatchDays();
    const analyzedCount = state.batchDays.filter((day) => day.analyzed === true).length;
    setBatchStatus(
      `扫描完成：${data.count || 0} 个日期目录，${data.ready_count || 0} 天可刷，`
      + `${analyzedCount} 天库里已有结果（默认跳过，需更新请勾「强制重算」）。勾选后点「开始刷写」。`,
      'ok'
    );
  } catch (error) {
    state.batchDays = [];
    state.batchSelected = new Set();
    $('batchPick').hidden = true;
    $('batchResult').hidden = true;
    setExportEnabled('batchExportButton', false);
    setBatchStatus(`扫描失败：${errorDetail(error && error.message ? error.message : String(error))}`, 'error');
  } finally {
    if (button) { button.disabled = false; button.textContent = '扫描日期'; }
  }
}

function selectBatch(kind) {
  if (kind === 'none') state.batchSelected.clear();
  if (kind === 'ready') {
    // 只选未算过的（增量刷写）
    state.batchSelected = new Set(
      state.batchDays.filter((day) => day.ready && day.analyzed !== true).map((day) => day.date)
    );
  }
  if (kind === 'all') {
    state.batchSelected = new Set(state.batchDays.filter((day) => day.ready).map((day) => day.date));
  }
  if (kind === 'invert') {
    const next = new Set();
    state.batchDays.filter((day) => day.ready).forEach((day) => {
      if (!state.batchSelected.has(day.date)) next.add(day.date);
    });
    state.batchSelected = next;
  }
  renderBatchDays();
}

function batchResultHtml(results) {
  const header = '<table class="mini-table"><thead><tr>'
    + '<th>日期</th><th>地区</th><th>状态</th><th>来源</th><th>有效里程</th><th>2M∩8M</th><th>GPS 稳定</th>'
    + '<th>2M 覆盖</th><th>8M 覆盖</th><th>点数</th><th>备注</th>'
    + '</tr></thead><tbody>';
  const rows = results.map((day) => {
    const regionCell = day.region ? esc(day.region) : '—';
    if (!day.ok) {
      return `<tr class="batch-failed"><td>${esc(day.date)}</td><td>${regionCell}</td><td>失败</td>`
        + '<td>—</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td>'
        + `<td>${esc(day.error || '')}</td></tr>`;
    }
    const mileage = day.mileage || {};
    const trajectory = day.trajectory || {};
    const notes = []
      .concat(Array.isArray(day.issues) ? day.issues : [])
      .concat(Array.isArray(day.warnings) ? day.warnings : [])
      .join('；');
    const flag = (Array.isArray(day.issues) && day.issues.length) ? '有提示' : '正常';
    const source = day.cached
      ? `<span class="cache-tag">缓存 ${esc(String(day.computed_at || '').slice(5, 16))}</span>`
      : '<span class="cache-tag is-new">新算</span>';
    return `<tr><td>${esc(day.date)}</td><td>${regionCell}</td><td>${flag}</td><td>${source}</td>`
      + `<td>${fmtKm(mileage.effective_m != null ? mileage.effective_m : mileage.both_m)}</td>`
      + `<td>${fmtKm(mileage.both_m)}</td>`
      + `<td>${fmtKm(mileage.gps_stable_m)}</td>`
      + `<td>${fmtKm(mileage.covered_2m_m)}</td>`
      + `<td>${fmtKm(mileage.covered_8m_m)}</td>`
      + `<td>${fmtCount(trajectory.point_count)}</td>`
      + `<td class="batch-note">${esc(notes || '—')}</td></tr>`;
  }).join('');

  // 合计行：只累加成功的天
  const totals = results.filter((day) => day.ok).reduce((acc, day) => {
    const mileage = day.mileage || {};
    ['effective_m', 'any_m', 'gps_stable_m', 'covered_2m_m', 'covered_8m_m'].forEach((key) => {
      acc[key] += Number(mileage[key]) || 0;
    });
    acc.points += Number((day.trajectory || {}).point_count) || 0;
    return acc;
  }, { effective_m: 0, any_m: 0, gps_stable_m: 0, covered_2m_m: 0, covered_8m_m: 0, points: 0 });
  const okCount = results.filter((day) => day.ok).length;
  const failCount = results.length - okCount;
  const totalRow = `<tr class="batch-total"><td>合计（${okCount} 天成功${failCount ? `，${failCount} 天失败` : ''}）</td><td>—</td><td>—</td><td>—</td>`
    + `<td>${fmtKm(totals.effective_m)}</td>`
    + `<td>${fmtKm(totals.both_m)}</td>`
    + `<td>${fmtKm(totals.gps_stable_m)}</td>`
    + `<td>${fmtKm(totals.covered_2m_m)}</td>`
    + `<td>${fmtKm(totals.covered_8m_m)}</td>`
    + `<td>${fmtCount(totals.points)}</td><td>—</td></tr>`;
  return `${header}${rows}${totalRow}</tbody></table>`;
}

function renderBatchResult(results) {
  state.batchResults = results;
  $('batchResultTable').innerHTML = batchResultHtml(results);
  const okCount = results.filter((day) => day.ok).length;
  const failCount = results.length - okCount;
  setText('batchResultInfo', `成功 ${okCount} 天${failCount ? ` · 失败 ${failCount} 天` : ''} · 共 ${results.length} 天`);
  $('batchResult').hidden = false;
  setExportEnabled('batchExportButton', results.length > 0);
  const issues = [];
  const warnings = [];
  results.forEach((day) => {
    if (!day.ok) issues.push(`${day.date}: ${day.error || '分析失败'}`);
    else (day.issues || []).forEach((item) => issues.push(`${day.date}: ${item}`));
    if (day.ok) (day.warnings || []).forEach((item) => warnings.push(`${day.date}: ${item}`));
  });
  renderAlerts('batchWarnings', '提示', warnings);
  renderAlerts('batchIssues', '问题', issues);
  requestAnimationFrame(() => drawBatchBar(results));
}

async function runBatch() {
  if (state.batchBusy || state.batchSelected.size === 0) return;
  const dates = state.batchDays
    .filter((day) => state.batchSelected.has(day.date))
    .map((day) => day.date);
  const forceRefresh = !!($('batchForceRefresh') && $('batchForceRefresh').checked);
  const results = [];
  state.batchBusy = true;
  const button = $('batchRunButton');
  if (button) button.disabled = true;
  const startedAt = Date.now();
  try {
    for (let index = 0; index < dates.length; index += 1) {
      const date = dates[index];
      const elapsed = Math.round((Date.now() - startedAt) / 1000);
      const mode = forceRefresh ? '强制重算' : '增量';
      setBatchStatus(
        `正在刷写 ${index + 1}/${dates.length}：${date}（${mode}）… 已用 ${elapsed} 秒`,
        'busy'
      );
      const dayInfo = state.batchDays.find((day) => day.date === date);
      const path = dayInfo && dayInfo.path
        ? dayInfo.path
        : `${state.batchRoot.replace(/[\\/]+$/, '')}/${date}`;
      try {
        const data = await postJson('/api/mileage', { data_root: path, refresh: forceRefresh });
        results.push({
          date,
          path,
          data_root: path,
          ok: true,
          cached: data.cached === true,
          computed_at: data.computed_at,
          mileage: data.mileage,
          trajectory: data.trajectory,
          coverage: data.coverage,
          warnings: data.warnings,
          issues: data.issues
        });
      } catch (error) {
        results.push({
          date,
          ok: false,
          error: errorDetail(error && error.message ? error.message : String(error))
        });
      }
      // 每完成一天就刷新结果表，长批次也能看到进度。
      renderBatchResult(results);
    }
    const cachedCount = results.filter((day) => day.ok && day.cached).length;
    setBatchStatus(
      `刷写完成：${results.filter((day) => day.ok).length}/${results.length} 天成功`
      + `（其中 ${cachedCount} 天直接读库），用时 ${Math.round((Date.now() - startedAt) / 1000)} 秒。`,
      'ok'
    );
    loadAllMileageHistory(true);
      } finally {
    state.batchBusy = false;
    if (button) button.disabled = false;
    updateBatchPickInfo();
  }
}

// ---- 初始化 ----------------------------------------------------------------

function clearAll() {
  clearResult();
  state.batchDays = [];
  state.batchSelected = new Set();
  state.batchResults = [];
  $('batchPick').hidden = true;
  $('batchResult').hidden = true;
  setExportEnabled('exportSingleButton', false);
  setExportEnabled('batchExportButton', false);
  setStatus('已清空', '');
  setBatchStatus('', '');
}

// ---- 顶部全局项目筛选 + 各项目待办 -----------------------------------------

function currentGlobalRegion() {
  const select = $('globalRegionFilter');
  return select ? String(select.value || '') : '';
}

function applyGlobalRegion(value) {
  historyFiltersPrimed = true;
  if (window.primeVerifyFilters) window.primeVerifyFilters();
  const region = String(value == null ? '' : value);
  const globalSelect = $('globalRegionFilter');
  if (globalSelect && globalSelect.value !== region) globalSelect.value = region;
  // 列表不再各自带筛选：统一按顶部全局筛选刷新（只刷新当前 tab）
  loadTodo();
  loadTabData(state.tab);
}

function renderTodo(data) {
  const container = $('todoCards');
  if (!container) return;
  const active = currentGlobalRegion();
  const allRegions = (data && data.regions) || [];
  // 顶部选了项目 -> 只看这个项目的待办；全部项目 -> 展示全部
  const regions = active ? allRegions.filter((item) => item.region === active) : allRegions;
  if (!regions.length) {
    container.innerHTML = `<div class="mileage-subtle">${active ? `${esc(active)}暂无待办` : '暂无待办'}</div>`;
    return;
  }
  container.innerHTML = regions.map((item) => {
    const vehicles = (item.vehicles || []).join(' / ');
    const cls = active && active === item.region ? ' is-active' : '';
    return `<article class="todo-card${cls}" data-region="${esc(item.region)}" tabindex="0">
      <div class="todo-card-head">
        <strong>${esc(item.region)}</strong>
        <span class="mileage-subtle">${esc(vehicles)}</span>
      </div>
      <div class="todo-stats">
        <span class="todo-stat"><b>${fmtCount(item.pending_confirm || 0)}</b> 待确认参数验证</span>
        <span class="todo-stat"><b>${fmtCount(item.review_done || 0)}</b> 已验证</span>
        <span class="todo-stat"><b>${fmtCount(item.mileage_days || 0)}</b> 里程天数</span>
      </div>
      <div class="todo-foot">${item.last_at ? `最近：${esc(item.last_at)}` : ''}</div>
    </article>`;
  }).join('');
  container.querySelectorAll('.todo-card').forEach((card) => {
    const open = () => {
      applyGlobalRegion(card.getAttribute('data-region'));
      switchTab('verify');
      const panel = $('verifyHistoryPanel');
      if (panel && panel.scrollIntoView) panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    };
    card.addEventListener('click', open);
    card.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); open(); }
    });
  });
}

async function loadTodo() {
  const container = $('todoCards');
  if (!container) return;
  try {
    const data = await getJson('/api/todo');
    renderTodo(data);
  } catch (error) {
    const container = $('todoCards');
    if (container) container.innerHTML = '<div class="mileage-subtle">待办读取失败</div>';
  }
}
window.refreshTodo = loadTodo;

function init() {
  // 顶部全局项目筛选：不做记忆，刷新/回退后默认「全部项目」
  try {
    localStorage.removeItem('mileage.globalRegion');
    localStorage.removeItem('mileage.root');
    localStorage.removeItem('mileage.batchRoot');
    localStorage.removeItem('mileage.history.single.query');
    localStorage.removeItem('mileage.history.single.days');
    localStorage.removeItem('mileage.history.batch.query');
    localStorage.removeItem('mileage.history.batch.days');
  } catch (_) { /* 忽略 */ }
  const globalRegionSelect = $('globalRegionFilter');
  if (globalRegionSelect) {
    globalRegionSelect.value = '';
    globalRegionSelect.addEventListener('change', () => applyGlobalRegion(globalRegionSelect.value));
  }
  const clearFilters = $('clearFiltersButton');
  if (clearFilters) {
    clearFilters.addEventListener('click', () => {
      resetMileageHistoryFilters(true);
    });
  }
  loadTodo();

  // 搜索/筛选不保留：刷新/回退后默认清空
  resetMileageHistoryFilters(false);

  // 页面自动刷新：每 60 秒刷新待办 + 各列表（切回页面时也立刻刷一次）
  const autoRefresh = () => {
    if (document.hidden) return;
    loadTodo();
    loadTabData(state.tab);
  };
  let autoRefreshTimer = setInterval(autoRefresh, 60000);
  let autoRefreshOn = true;
  let autoRefreshButton = null;
  try { autoRefreshButton = $('autoRefreshButton'); } catch (_) { /* 忽略 */ }
  if (autoRefreshButton) {
    autoRefreshButton.addEventListener('click', () => {
      autoRefreshOn = !autoRefreshOn;
      if (autoRefreshOn) {
        autoRefreshTimer = setInterval(autoRefresh, 60000);
        autoRefreshButton.textContent = '自动刷写：开';
      } else {
        clearInterval(autoRefreshTimer);
        autoRefreshButton.textContent = '自动刷写：关';
      }
    });
  }
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && autoRefreshOn) autoRefresh();
  });

  const runButton = $('mileageRunButton');
  const runInput = $('mileageRootInput');
  if (runButton) runButton.addEventListener('click', () => runAnalysis(false));
  const refreshButton = $('mileageRefreshButton');
  if (refreshButton) refreshButton.addEventListener('click', () => runAnalysis(true));
  if (runInput) {
    runInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); runAnalysis(false); }
    });
  }

  const tabOverseasDash = $('tabOverseasDash');
  if (tabOverseasDash) tabOverseasDash.addEventListener('click', () => switchTab('overseasDash'));
  const tabSingle = $('tabSingle');
  if (tabSingle) tabSingle.addEventListener('click', () => switchTab('single'));
  const tabBatch = $('tabBatch');
  if (tabBatch) tabBatch.addEventListener('click', () => switchTab('batch'));
  const tabVerify = $('tabVerify');
  if (tabVerify) tabVerify.addEventListener('click', () => switchTab('verify'));
  const tabBatchVerify = $('tabBatchVerify');
  if (tabBatchVerify) tabBatchVerify.addEventListener('click', () => switchTab('batchVerify'));

  const scanButton = $('batchScanButton');
  const batchInput = $('batchRootInput');
  if (scanButton) scanButton.addEventListener('click', scanBatch);
  if (batchInput) {
    batchInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); scanBatch(); }
    });
  }
  const selectReady = $('batchSelectReady');
  if (selectReady) selectReady.addEventListener('click', () => selectBatch('ready'));
  const selectAll = $('batchSelectAll');
  if (selectAll) selectAll.addEventListener('click', () => selectBatch('all'));
  const selectNone = $('batchSelectNone');
  if (selectNone) selectNone.addEventListener('click', () => selectBatch('none'));
  const invert = $('batchInvert');
  if (invert) invert.addEventListener('click', () => selectBatch('invert'));
  const runBatchButton = $('batchRunButton');
  if (runBatchButton) runBatchButton.addEventListener('click', runBatch);

  const exportSingleButton = $('exportSingleButton');
  if (exportSingleButton) exportSingleButton.addEventListener('click', exportSingle);
  const batchExportButton = $('batchExportButton');
  if (batchExportButton) batchExportButton.addEventListener('click', exportBatch);

  // 库中已有结果（单日 + 批量两个列表）：输入即筛、时间下拉、分页
  mileageHistoryViews.forEach((view) => {
    const queryInput = $(view.query);
    if (queryInput) {
      let historyTimer = null;
      queryInput.addEventListener('input', () => {
        historyFiltersPrimed = true;
        if (historyTimer) clearTimeout(historyTimer);
        historyTimer = setTimeout(() => {
          historyTimer = null;
          loadHistoryFor(view, true);
        }, 300);
      });
      queryInput.addEventListener('keydown', (event) => {
        historyFiltersPrimed = true;
        if (event.key === 'Enter') { event.preventDefault(); loadHistoryFor(view, true); }
      });
    }
    const timeSelect = $(view.time);
    if (timeSelect) {
      timeSelect.addEventListener('change', () => {
        historyFiltersPrimed = true;
        toggleHistoryRange(view.time, view.timeStart, view.time.replace('Filter', 'Sep'), view.timeEnd);
        loadHistoryFor(view, true);
      });
    }
    [view.timeStart, view.timeEnd].forEach((id) => {
      const input = $(id);
      if (input) {
        input.addEventListener('change', () => {
          historyFiltersPrimed = true;
          loadHistoryFor(view, true);
        });
      }
    });
    const prev = $(view.prev);
    if (prev) {
      prev.addEventListener('click', () => {
        const viewState = mileageHistoryState[view.key];
        viewState.offset = Math.max(0, viewState.offset - state.historyLimit);
        loadHistoryFor(view);
      });
    }
    const next = $(view.next);
    if (next) {
      next.addEventListener('click', () => {
        const viewState = mileageHistoryState[view.key];
        viewState.offset += state.historyLimit;
        loadHistoryFor(view);
      });
    }
    // 首屏只加载当前 tab 的列表（见 switchTab/loadTabData）
  });

  const reset = $('resetButton');
  if (reset) reset.addEventListener('click', clearAll);

  let resizeTimer = null;
  window.addEventListener('resize', () => {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      resizeTimer = null;
      redrawCharts();
    }, 150);
  });

  // 只恢复上次的 sheet；输入框不保留上次内容
  try {
    const savedTab = localStorage.getItem('mileage.tab');
    if (savedTab) switchTab(savedTab);
    else switchTab('overseasDash');
  } catch (_) { /* 隐私模式忽略 */ }
  }

init();
