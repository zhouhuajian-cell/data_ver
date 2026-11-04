// 用 Node 加载 web/app.js（DOM stub），验证"位置 RMS 超限比例"的统计口径：
//   1. 分母必须是**总帧数**（GSOF49 导航记录行数），不是 RMS 有效行数；
//   2. "超过" 是严格大于：正好等于 0.1 / 0.05 的帧不算超限；
//   3. 空值 / 非数值既不算分子也不算有效帧，但仍计入分母；
//   4. 概览卡片与 RMS 面板写的 id 必须真实存在于 web/index.html。
// 不依赖浏览器：DOM/Canvas/Leaflet/getComputedStyle 全部用最小 stub 顶替。
//
// 用法：node tests/test_frontend_stats.js

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const failures = [];
function check(condition, label, detail) {
  if (condition) {
    console.log(`  [ OK ] ${label}`);
    return;
  }
  failures.push(label);
  console.log(`  [FAIL] ${label}${detail ? ' — ' + detail : ''}`);
}

// ---- DOM stub -------------------------------------------------------------
const context2d = new Proxy({}, { get: () => () => {}, set: () => true });
function makeElement(id) {
  return {
    id,
    hidden: false,
    disabled: false,
    value: '',
    textContent: '',
    innerHTML: '',
    dataset: {},
    style: {},
    children: [],
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener() {},
    removeEventListener() {},
    appendChild(child) { this.children.push(child); return child; },
    removeChild() {},
    remove() {},
    insertAdjacentHTML() {},
    querySelector: () => makeElement(`${id}-child`),
    querySelectorAll: () => [],
    closest: () => null,
    getContext: () => context2d,
    getBoundingClientRect: () => ({ width: 640, height: 240, top: 0, left: 0 }),
    focus() {},
    click() {},
  };
}
const elements = new Map();
const getElement = (id) => {
  if (!elements.has(id)) elements.set(id, makeElement(id));
  return elements.get(id);
};
const documentStub = {
  getElementById: getElement,
  createElement: (tag) => makeElement(tag),
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener() {},
  documentElement: {},
  body: makeElement('body'),
};

function loadApp() {
  globalThis.document = documentStub;
  globalThis.window = { devicePixelRatio: 1, addEventListener() {} };
  globalThis.navigator = { userAgent: 'node' };
  globalThis.location = { href: 'http://127.0.0.1:8765/' };
  globalThis.getComputedStyle = () => ({ getPropertyValue: () => '#000' });
  if (!globalThis.requestAnimationFrame) globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);
  globalThis.ResizeObserver = undefined;
  globalThis.IntersectionObserver = undefined;
  globalThis.L = undefined;
  globalThis.showDirectoryPicker = undefined;
  globalThis.fetch = async () => ({ ok: true, status: 200, statusText: 'OK', json: async () => ({}) });
  vm.runInThisContext(fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8'), { filename: 'app.js' });
}

// ---- 造数据 ---------------------------------------------------------------
// 10 帧里只有 4 帧带 GSOF50 位置 RMS（真实数据约 1/20），专门用来区分
// "分母=总帧数" 和 "分母=有效行数" 两种口径。
function makeRows() {
  return [
    // 运动帧（10 m/s = 36 km/h）
    { gps_week: 2433, gps_ms: 1000, north_position_rms: 0.11, east_position_rms: 0.2, down_position_rms: 0.06, total_speed: 10 },
    { gps_week: 2433, gps_ms: 2000, north_position_rms: 0.05, east_position_rms: 0.05, down_position_rms: 0.1, total_speed: 10 },
    // 静止帧（0 m/s）：有 RMS 但要被车速过滤掉
    { gps_week: 2433, gps_ms: 3000, north_position_rms: 0.1, total_speed: 0 },
    // 低速帧（2 m/s = 7.2 km/h）：> 5 km/h，算运动帧
    { gps_week: 2433, gps_ms: 4000, north_position_rms: 0.051, total_speed: 2 },
    { gps_week: 2433, gps_ms: 5000, total_speed: 10 },
    { gps_week: 2433, gps_ms: 6000, total_speed: 10 },
    { gps_week: 2433, gps_ms: 7000, total_speed: 10 },
    { gps_week: 2433, gps_ms: 8000, total_speed: 10 },
    // 有 RMS 但读不到车速：不计入分母
    { gps_week: 2433, gps_ms: 9000, north_position_rms: 0.3, east_position_rms: NaN },
    // 非数值 RMS：既不算分子也不算有效帧
    { gps_week: 2433, gps_ms: 10000, north_position_rms: '0.2', east_position_rms: NaN, down_position_rms: Infinity, total_speed: 10 },
  ];
}
function fieldOf(stats, key) {
  return (stats.position_rms_exceed.fields || []).find((field) => field.key === key) || {};
}

function caseMath() {
  console.log('[1] 统计口径：分子=超限运动帧数，分母=车速>5km/h 且有 RMS 的帧数');
  const rows = makeRows();
  const stats = computeStats(rows);
  const north = fieldOf(stats, 'north_position_rms');
  const east = fieldOf(stats, 'east_position_rms');
  const down = fieldOf(stats, 'down_position_rms');

  check(stats.position_rms_exceed.total === 10, '总帧数仍然统计出来（10 帧）',
        String(stats.position_rms_exceed.total));
  check(stats.row_count === 10, '总帧数就是导航记录行数', String(stats.row_count));
  check(stats.position_rms_exceed.movingSpeedKmh === 5, '车速门槛是 5 km/h',
        String(stats.position_rms_exceed.movingSpeedKmh));

  check(north.valid === 5, '北位置 RMS 有效帧 5（null / 字符串不计；第 9 帧有 RMS 但没车速）', String(north.valid));
  check(north.moving === 3, '其中运动帧 3（10 m/s ×2 + 2 m/s ×1；0 m/s 与无车速的排除）', String(north.moving));
  check(north.excluded === 2, '排除帧 2（静止 1 + 无车速 1）', String(north.excluded));
  check(north.noSpeed === 1, '无车速的帧单独计数，便于在界面上说明', String(north.noSpeed));

  check(north.over['0.1'] === 1, '北位置 RMS > 0.1 m 只有 1 个运动帧（0.11；静止帧的 0.1 不计）', String(north.over['0.1']));
  check(north.over['0.05'] === 2, '北位置 RMS > 0.05 m 有 2 个运动帧（0.11 / 0.051；0.05 等于阈值不算）', String(north.over['0.05']));
  check(Math.abs(north.ratio['0.1'] - 1 / 3) < 1e-12, '比例 = 1 / 3（分母是 3 个运动帧）', String(north.ratio['0.1']));
  check(Math.abs(north.ratio['0.05'] - 2 / 3) < 1e-12, '>0.05 比例 = 2 / 3 个运动帧', String(north.ratio['0.05']));

  check(north.moving === 3 && north.valid === 5 && north.total === 10,
        '每行都带着 valid / moving / total，界面能标注出分母是哪一个',
        JSON.stringify({ valid: north.valid, moving: north.moving, total: north.total }));

  check(east.valid === 2 && east.moving === 2 && east.over['0.1'] === 1 && east.over['0.05'] === 1,
        '等于阈值的 0.05 不算"超过"（东向运动帧只数 0.2）',
        JSON.stringify({ valid: east.valid, moving: east.moving, over01: east.over['0.1'] }));
  check(down.valid === 2 && down.moving === 2 && down.over['0.1'] === 0 && down.over['0.05'] === 2,
        '等于阈值的 0.1 不算 >0.1，但仍算 >0.05（下向 0.06 与 0.1）',
        JSON.stringify({ valid: down.valid, over01: down.over['0.1'], over005: down.over['0.05'] }));

  check(stats.position_rms_exceed.thresholds.map((item) => item.text).join(',') === '0.1,0.05',
        '阈值档位是 0.1 m 与 0.05 m',
        JSON.stringify(stats.position_rms_exceed.thresholds));

  // 车速边界：正好 5 km/h 不算运动帧（严格大于）
  const exact = computeStats([{ gps_week: 2433, gps_ms: 1000, north_position_rms: 0.2, total_speed: 5 / 3.6 }]);
  check(exact.position_rms_exceed.fields[0].moving === 0,
        '车速正好 5 km/h：不算运动帧（严格大于才算）',
        JSON.stringify(exact.position_rms_exceed.fields[0].moving));

  const empty = computeStats([]);
  check(empty.position_rms_exceed.total === 0 && empty.position_rms_exceed.fields.length === 3,
        '空数据：分母 0、三个分量都在，且不抛异常',
        JSON.stringify({ total: empty.position_rms_exceed.total, fields: empty.position_rms_exceed.fields.length }));
  check(empty.position_rms_exceed.fields.every((field) => field.ratio['0.1'] === 0 && field.ratio['0.05'] === 0),
        '空数据的比例是 0 而不是 NaN', JSON.stringify(empty.position_rms_exceed.fields.map((f) => f.ratio)));
}

function caseFormat() {
  console.log('\n[2] 比例文本：小比例不会被截成 0.00%');
  check(fmtRatio(0) === '0.000%', '0 明确显示 0.000%', fmtRatio(0));
  check(fmtRatio(1 / 6003) === '0.017%', '1/6003 显示 0.017%（两位小数会变成 0.02%）', fmtRatio(1 / 6003));
  check(fmtRatio(0.3) === '30.0%', '≥10% 保留一位小数', fmtRatio(0.3));
  check(fmtRatio(0.0123) === '1.23%', '1%~10% 保留两位小数', fmtRatio(0.0123));
  check(fmtRatio(null) === '—' && fmtRatio(NaN) === '—', '无值显示 —', `${fmtRatio(null)} / ${fmtRatio(NaN)}`);
}

function caseRender() {
  console.log('\n[3] 页面接线：面板用的是 index.html 里真实存在的 id');
  const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
  const ids = new Set([...html.matchAll(/id="([^"]+)"/g)].map((match) => match[1]));
  const used = ['rmsExceedTable', 'rmsExceedNote'];
  const missing = used.filter((id) => !ids.has(id));
  check(missing.length === 0, '2 个面板 id 都在 web/index.html 里', missing.join(','));
  check(!/statRmsOver/.test(html), '概览区不再有重复的 RMS 超限卡片（只在图表下方画一处）');

  const rows = makeRows();
  state.files = { files: [{ name: 'a.pcap', row_count: rows.length, rms_rows: 4 }], all: { name: 'ALL', row_count: rows.length, rms_rows: 4 } };
  state.allRows = rows;
  state.fileRows = { 'a.pcap': rows };
  setView('ALL');
  check(Math.abs(state.stats.position_rms_exceed.fields[0].ratio['0.1'] - 1 / 3) < 1e-12,
        'setView 后 state.stats 带上了超限统计（分母 = 3 个运动帧）',
        JSON.stringify(state.stats.position_rms_exceed.fields[0].ratio));

  const table = getElement('rmsExceedTable').innerHTML;
  check(table.includes('&gt; 0.1 m 帧数') && table.includes('&gt; 0.05 m 帧数')
        && table.includes('&gt; 0.1 m 占比') && table.includes('&gt; 0.05 m 占比')
        && table.includes('运动帧（分母 &gt; 5 km/h）') && table.includes('有 RMS 的帧')
        && table.includes('已排除 ≤ 5 km/h') && table.includes('总帧数'),
        '明细表列出两个阈值的帧数与占比，并标出运动帧分母/已排除帧', table.slice(0, 300));
  check(table.includes('<th>北位置RMS</th><td>1</td><td>2</td><td class="pct pct-high">33.3%</td><td class="pct pct-high">66.7%</td><td>3</td><td>5</td><td>2</td><td>10</td>'),
        '北位置一行：>0.1 是 1/3(33.3%)、>0.05 是 2/3(66.7%)，运动帧 3 / 有 RMS 5 / 排除 2 / 总 10',
        table.replace(/\s+/g, ' ').slice(0, 320));
  check(table.includes('<th>下位置RMS</th><td>0</td><td>2</td><td class="pct pct-zero">0.000%</td><td class="pct pct-high">100.0%</td><td>2</td><td>2</td><td>0</td><td>10</td>'),
        '下位置一行：>0.1 为 0、>0.05 是 2/2(100.0%)，两帧都是运动帧',
        table.replace(/\s+/g, ' ').slice(0, 320));
  check(ratioClass(0) === 'pct-zero' && ratioClass(0.005) === 'pct-low' && ratioClass(0.02) === 'pct-mid' && ratioClass(0.08) === 'pct-high',
        '高亮分档：0 灰 / <1% 蓝 / ≥1% 橙 / ≥5% 红',
        [ratioClass(0), ratioClass(0.005), ratioClass(0.02), ratioClass(0.08)].join(','));
  check(getElement('rmsExceedNote').textContent.includes('只统计运动帧')
        && getElement('rmsExceedNote').textContent.includes('车速 > 5 km/h')
        && getElement('rmsExceedNote').textContent.includes('运动 3 + 排除 2')
        && getElement('rmsExceedNote').textContent.includes('1 帧读不到车速')
        && getElement('rmsExceedNote').textContent.includes('灰 = 0（无超限）· 蓝 = <1%'),
        '面板标注了分母口径（运动帧 / 已排除帧 / 无车速帧）', getElement('rmsExceedNote').textContent);
}

function main() {
  loadApp();
  caseMath();
  caseFormat();
  caseRender();
  console.log();
  if (failures.length) {
    console.log(`结果：${failures.length} 项失败 -> ${failures.join(' | ')}`);
    process.exitCode = 1;
    return;
  }
  console.log('结果：全部通过');
}

main();
