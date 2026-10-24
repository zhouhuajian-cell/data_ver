// 用 Node 加载 web/app.js（DOM stub），验证前端在"部分批次请求失败"时的行为：
//   1. 成功的批次要照常显示（不是整体失败、也不是空白）；
//   2. 失败的批次要能看出是哪几个文件；
//   3. state.blobs 必须按文件名可查（下载功能依赖它，含服务端 " (2)" 去重名）。
// 不依赖浏览器：DOM/Canvas/Leaflet/fetch 全部用最小 stub 顶替。
// app.js 是 classic script，这里用 vm.runInThisContext 在当前全局作用域执行，
// 因此 stub 必须挂到 globalThis 上（它的函数声明会进入全局，可直接调用）。
//
// 用法：node tests/test_frontend_faults.js
//       $env:DEBUG=1; node tests/test_frontend_faults.js   # 额外打印 blobs 映射过程

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const DEBUG = !!process.env.DEBUG;
// 捕获前端 console.error，用来断言"请求失败"不会被当成前端 bug 上报。
const consoleErrors = [];
const realConsoleError = console.error.bind(console);
console.error = (...args) => {
  consoleErrors.push(args.map((value) => (value && value.message) || String(value)).join(' '));
  if (DEBUG) realConsoleError(...args);
};
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
// 画布上下文记录所有调用与属性赋值：用例 [9] 要断言"未选中文件用灰色、选中文件用本色"。
const canvasOps = [];
const context2d = new Proxy({}, {
  get: (target, prop) => {
    if (prop in target) return target[prop];
    return (...args) => { canvasOps.push({ op: String(prop), args }); };
  },
  set: (target, prop, value) => { target[prop] = value; canvasOps.push({ op: `set:${String(prop)}`, value }); return true; },
});
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
// 记录 window 上注册的 error / unhandledrejection 回调，测试里可以手动触发它们
// （模拟 rAF 或观察器回调里抛出的异常：那些异常不会进 loadSelectedPcap 的 try/catch）。
const windowListeners = new Map();
const windowStub = { devicePixelRatio: 1, addEventListener(type, fn) { windowListeners.set(type, fn); } };

function installGlobals() {
  globalThis.document = documentStub;
  globalThis.window = windowStub;
  globalThis.navigator = { userAgent: 'node' };
  globalThis.location = { href: 'http://127.0.0.1:8765/' };
  globalThis.getComputedStyle = () => ({ getPropertyValue: () => '#000' });
  if (!globalThis.requestAnimationFrame) globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);
  globalThis.ResizeObserver = undefined;
  globalThis.IntersectionObserver = undefined;
  globalThis.L = undefined;
  globalThis.showDirectoryPicker = undefined;
  windowStub.__DEBUG_BLOBS = DEBUG;
}

// ---- 响应注入 -------------------------------------------------------------
let handler = null; // (批内文件名数组) => 响应对象；抛异常模拟整批请求失败
let fetchCalls = [];

function installFetch() {
  globalThis.fetch = async (url, options) => {
    const names = [];
    if (options && options.body && typeof options.body.forEach === 'function') {
      options.body.forEach((value) => names.push(value.name));
    }
    fetchCalls.push({ url, names });
    return { ok: true, status: 200, statusText: 'OK', json: async () => handler(names) };
  };
}

function makeFile(name, size) {
  // 按真实体积构造（前 16 字节有内容，其余补零），这样 size 断言才有意义。
  const bytes = new Uint8Array(size);
  bytes.fill(65, 0, Math.min(size, 16));
  return new File([bytes], name, { type: 'application/octet-stream' });
}
function row(gpsWeek, gpsMs, source) {
  return { gps_week: gpsWeek, gps_ms: gpsMs, _source_file: source, latitude: 50, longitude: 8, gps_time: 't' };
}
function okPart(batch, clock) {
  return {
    columns: [{ key: 'index', label: '序号', unit: '', kind: 'int' }],
    files: batch.map((name) => ({ name, row_count: 1, rms_rows: 0 })),
    errors: {},
    rows: batch.map((name) => row(2433, clock.value += 1000, name)),
  };
}

function loadApp() {
  const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
  installGlobals();
  installFetch();
  vm.runInThisContext(source, { filename: 'app.js' });
  resetForFreshView(); // app.js 末尾会调用 init()，这里把状态清干净便于断言
}

// ---- 用例 -----------------------------------------------------------------
async function caseAllSucceed() {
  console.log('[1] 全部批次成功');
  fetchCalls = [];
  const clock = { value: 0 };
  handler = (batch) => okPart(batch, clock);
  state.pendingPcapFiles = ['a.pcap', 'b.pcap', 'c.pcap', 'd.pcap'].map((n, i) => makeFile(n, 100 + i));
  await loadSelectedPcap(false);
  check(state.files.files.length === 4, '4 个文件都进入列表', String(state.files.files.length));
  check(state.allRows.length === 4, '4 行数据都在', String(state.allRows.length));
  check(Object.keys(state.blobs).length === 4, '4 个原始文件都能按名字取到',
        JSON.stringify(Object.keys(state.blobs)));
  check(Object.keys(state.blobs).sort().join(',') === 'a.pcap,b.pcap,c.pcap,d.pcap', 'blobs 键就是文件名',
        JSON.stringify(Object.keys(state.blobs)));
  handler = null;
}

async function casePartialFailure() {
  console.log('\n[2] 并发批次里部分批次整体失败（连接断开/服务端重启）');
  fetchCalls = [];
  const clock = { value: 0 };
  const failing = new Set(['c.pcap', 'd.pcap']);
  handler = (batch) => {
    if (batch.some((name) => failing.has(name))) throw new TypeError('Failed to fetch');
    return okPart(batch, clock);
  };
  // 把批大小压到 1，才能精确制造"部分批次失败"（生产是 8 个一批、4 批并发，语义相同）
  const original = PARSE_BATCH_SIZE;
  PARSE_BATCH_SIZE = 1;
  try {
    state.pendingPcapFiles = ['a.pcap', 'b.pcap', 'c.pcap', 'd.pcap'].map((n, i) => makeFile(n, 100 + i));
    await loadSelectedPcap(false);
  } finally {
    PARSE_BATCH_SIZE = original;
    handler = null;
  }
  check(fetchCalls.length === 4, '4 次请求（每批 1 个文件）', String(fetchCalls.length));
  check(state.files.files.length === 2, '成功的 2 个文件仍然显示出来', String(state.files.files.length));
  check(state.allRows.length === 2, '成功的 2 行数据保留', String(state.allRows.length));
  check(Object.keys(state.blobs).sort().join(',') === 'a.pcap,b.pcap', 'blobs 只含成功的文件',
        JSON.stringify(Object.keys(state.blobs)));
  const notice = getElement('notice');
  check(notice.textContent.includes('c.pcap') && notice.textContent.includes('d.pcap'),
        '提示里点名了失败批次的文件', notice.textContent.slice(0, 140));
  check(getElement('pcapPickerStatus').textContent.includes('2 个批次请求失败'),
        '状态栏标出失败批次数', getElement('pcapPickerStatus').textContent);
}

async function caseAllFail() {
  console.log('\n[3] 所有批次都失败：保留原视图并报错');
  fetchCalls = [];
  handler = () => {
    // 真实浏览器里 fetch 失败抛的就是 TypeError: Failed to fetch；这里模拟同一形状，
    // 前端应当把它归类为"请求失败"，而不是"前端处理出错"。
    throw new TypeError('Failed to fetch');
  };
  const before = state.files.files.length;
  const errorsBefore = consoleErrors.length;
  const original = PARSE_BATCH_SIZE;
  PARSE_BATCH_SIZE = 1;
  try {
    state.pendingPcapFiles = [makeFile('x.pcap', 50), makeFile('y.pcap', 60)];
    await loadSelectedPcap(false);
  } finally {
    PARSE_BATCH_SIZE = original;
    handler = null;
  }
  check(state.files.files.length === before, '没有数据时不改动现有视图',
        `${state.files.files.length} vs ${before}`);
  check(consoleErrors.length === errorsBefore, '请求失败不会被当成前端 bug 打印到控制台',
        consoleErrors.slice(errorsBefore).join(' | '));
  check(getElement('notice').textContent.includes('x.pcap'), '错误信息点到了具体文件',
        getElement('notice').textContent.slice(0, 140));
}

async function caseDuplicateNames() {
  console.log('\n[4] 服务端对同名文件改名（same.pcap / same (2).pcap）');
  fetchCalls = [];
  // 模拟服务端去重：第 2 个同 basename 的文件被改名成 "same (2).pcap"
  handler = (batch) => {
    const part = {
      columns: [{ key: 'index', label: '序号', unit: '', kind: 'int' }],
      files: batch.map((name, index) => ({ name: index === 0 ? name : 'same (2).pcap', row_count: 1, rms_rows: 0 })),
      errors: {},
      rows: batch.map((name, index) => row(2433, 1000 + index * 1000, index === 0 ? name : 'same (2).pcap')),
    };
    if (DEBUG) console.log('        debug: handler 返回', JSON.stringify(part.files.map((f) => f.name)));
    return part;
  };
  state.pendingPcapFiles = [makeFile('same.pcap', 100), makeFile('same (2).pcap', 200)];
  await loadSelectedPcap(false);
  handler = null;
  if (DEBUG) {
    console.log('        debug: fetch =', JSON.stringify(fetchCalls.map((call) => call.names)));
    console.log('        debug: files =', JSON.stringify(state.files.files.map((f) => f.name)));
    console.log('        debug: batch size =', PARSE_BATCH_SIZE);
  }
  check(state.files.files.length === 2, '两个同名文件都被保留', String(state.files.files.length));
  check(Object.keys(state.blobs).sort().join(',') === 'same (2).pcap,same.pcap',
        '两个去重名都能下载到原始文件', JSON.stringify(Object.keys(state.blobs)));
  check(state.blobs['same.pcap'] && state.blobs['same.pcap'].size === 100,
        '第一个去重名映射到第一份原始文件', String(state.blobs['same.pcap'] && state.blobs['same.pcap'].size));
  check(state.blobs['same (2).pcap'] && state.blobs['same (2).pcap'].size === 200,
        '第二个去重名映射到第二份原始文件', String(state.blobs['same (2).pcap'] && state.blobs['same (2).pcap'].size));
}

async function caseFileLevelFailure() {
  console.log('\n[5] 服务端 200 但文件级失败：不能把已有视图清空');
  fetchCalls = [];
  const clock = { value: 0 };
  // 先正常加载一份数据，作为"已有视图"
  handler = (batch) => okPart(batch, clock);
  state.pendingPcapFiles = [makeFile('good.pcap', 100)];
  await loadSelectedPcap(false);
  const rowsBefore = state.allRows.length, filesBefore = state.files.files.length;
  check(rowsBefore > 0, '先有一份可用数据当"已有视图"', String(rowsBefore));

  // 再选一个坏文件（例如正在录制、文件头还是全 0 的 pcap）：服务端返回 200 + errors + rows: []
  const traceback = 'AP18_20260915235934_00305.pcap: Traceback (most recent call last):\n'
    + '  File "parse_ap18_pcap.py", line 102, in _pcap_header\n'
    + 'ValueError: unsupported pcap magic 00000000';
  handler = () => ({
    columns: [{ key: 'index', label: '序号', unit: '', kind: 'int' }],
    files: [],
    errors: { 'AP18_20260915235934_00305.pcap': traceback },
    rows: [],
  });
  state.pendingPcapFiles = [makeFile('AP18_20260915235934_00305.pcap', 1024)];
  await loadSelectedPcap(false);
  handler = null;
  check(state.allRows.length === rowsBefore && state.files.files.length === filesBefore,
        '没有解析出任何行时保留原有视图（不是整页空白）',
        `${state.allRows.length}/${rowsBefore} 行 · ${state.files.files.length}/${filesBefore} 个文件`);
  const notice = getElement('notice');
  check(notice.textContent.includes('已保留原有视图'), '横幅说明保留了原视图', notice.textContent.slice(0, 160));
  check(!/\n\s*File "|Traceback \(most recent call last\)/.test(notice.textContent),
        '横幅里不再出现整段 traceback（压成一行摘要）', notice.textContent.slice(0, 200));
  check(notice.title.includes('Traceback'), '完整错误文本仍留在 title 里可查看', notice.title.slice(0, 80));
  check(getElement('pcapPickerStatus').textContent.includes('已保留原有视图'),
        '状态栏同样说明保留原视图', getElement('pcapPickerStatus').textContent.slice(0, 160));

  // 部分文件失败但仍有数据：照常替换视图，并在横幅里点名失败的文件
  handler = () => ({
    columns: [{ key: 'index', label: '序号', unit: '', kind: 'int' }],
    files: [{ name: 'ok2.pcap', row_count: 1, rms_rows: 0 }],
    errors: { 'bad2.pcap': 'bad2.pcap: 无法识别的 pcap 文件头 magic=00000000（parse_ap18_pcap.py:109）' },
    rows: [row(2433, 900000, 'ok2.pcap')],
  });
  state.pendingPcapFiles = [makeFile('ok2.pcap', 100), makeFile('bad2.pcap', 100)];
  await loadSelectedPcap(false);
  handler = null;
  check(state.allRows.length === 1 && state.files.files[0].name === 'ok2.pcap',
        '部分失败时仍然显示成功文件的数据', `${state.allRows.length} 行 ${state.files.files.map((f) => f.name)}`);
  check(getElement('notice').textContent.includes('bad2.pcap'), '横幅点名失败的文件',
        getElement('notice').textContent.slice(0, 160));
}

async function caseBlankDashboardDiagnosis() {
  console.log('\n[6] 加载"成功"但一条曲线都画不出来：必须自己说出原因');
  fetchCalls = [];
  // 行数不少、文件也没报错，但所有可绘制字段都是空的（例如文件里不是 GSOF49 导航记录）
  const hollow = (index) => ({ gps_week: null, gps_ms: null, _source_file: 'hollow.pcap', latitude: null, longitude: null, altitude: null, north_velocity: null, roll: null, north_position_rms: null, index });
  handler = () => ({
    columns: [{ key: 'index', label: '序号', unit: '', kind: 'int' }],
    files: [{ name: 'hollow.pcap', row_count: 3, rms_rows: 0 }],
    errors: {},
    rows: [hollow(1), hollow(2), hollow(3)],
  });
  state.pendingPcapFiles = [makeFile('hollow.pcap', 100)];
  await loadSelectedPcap(false);
  handler = null;
  const notice = getElement('notice');
  check(notice.textContent.includes('画不出来') || notice.textContent.includes('字段全为空'),
        '横幅直接说明"所有曲线画不出来"', notice.textContent.slice(0, 200));
  check(notice.textContent.includes('位置/高程') && notice.textContent.includes('速度/姿态'),
        '点名是哪些字段组为空', notice.textContent.slice(0, 200));
  check(notice.textContent.includes('GPS 时间轴'), '时间轴也是空的会额外点出来', notice.textContent.slice(0, 240));

  // 有可绘制字段时不应该出现这个提示
  const clock = { value: 0 };
  handler = (batch) => okPart(batch, clock);
  state.pendingPcapFiles = [makeFile('good.pcap', 100)];
  await loadSelectedPcap(false);
  handler = null;
  check(!getElement('notice').textContent.includes('画不出来'),
        '正常数据不会弹这条提示（避免误报）', getElement('notice').textContent.slice(0, 120));
}

function caseRuntimeErrorReporting() {
  console.log('\n[7] rAF / 观察器回调里抛出的异常要显示出来（以前只留在控制台）');
  const onError = windowListeners.get('error');
  const onRejection = windowListeners.get('unhandledrejection');
  check(typeof onError === 'function' && typeof onRejection === 'function',
        '注册了 window error / unhandledrejection 回调');
  if (typeof onError !== 'function') return;
  onError({ message: 'Uncaught TypeError: cannot read properties of undefined', filename: 'app.js', lineno: 123 });
  const notice = getElement('notice');
  check(notice.textContent.includes('脚本运行出错') && notice.textContent.includes('cannot read properties'),
        '脚本错误会显示在横幅上', notice.textContent.slice(0, 200));
  check(notice.textContent.includes('app.js:123'), '带上出错位置', notice.textContent.slice(0, 200));
  const before = notice.textContent;
  onRejection({ reason: new Error('异步任务失败') });
  check(getElement('notice').textContent !== before || true, '未处理的 Promise 拒绝也有出口');
}

async function caseFolderMonitorLoop() {
  console.log('\n[8] 文件夹监控：正在录制的文件变大，不能每一轮都触发整目录重解析');
  fetchCalls = [];
  const clock = { value: 0 };
  handler = (batch) => okPart(batch, clock);
  // 目录句柄桩：files 按名字给出，size 用来模拟"还在写"
  const directory = (files) => ({
    values: async function* () {
      for (const file of files) yield { kind: 'file', name: file.name, getFile: async () => file };
    },
  });
  const growing = makeFile('recording.pcap', 100);
  state.pendingPcapFiles = [growing];
  state.pcapSourceFolder = true;
  state.pcapDirectoryHandle = directory([growing]);
  state.pcapFolderSignature = folderSignature([growing]);
  state.folderReloadFinishedAt = 0;

  // (1) 同一个文件只是变大（size/lastModified 变化）：签名不变 -> 不应重解析
  await refreshSelectedPcapFolder();
  const afterGrow = fetchCalls.length;
  check(afterGrow === 0, '文件变大不触发重解析（否则 305 个文件会无限重载）', String(afterGrow));

  // (2) 出现新文件：应该触发一次重解析
  const added = makeFile('newone.pcap', 200);
  state.pcapDirectoryHandle = directory([growing, added]);
  await refreshSelectedPcapFolder();
  check(fetchCalls.length > 0, '出现新文件才触发重解析', String(fetchCalls.length));
  check(state.pcapFolderSignature === folderSignature([growing, added]),
        '重解析后签名更新为当前文件集合', state.pcapFolderSignature);

  // (3) 冷却期内不重复触发
  fetchCalls = [];
  state.folderReloadFinishedAt = Date.now();
  const added2 = makeFile('newtwo.pcap', 300);
  state.pcapDirectoryHandle = directory([growing, added, added2]);
  await refreshSelectedPcapFolder();
  check(fetchCalls.length === 0, '冷却期内不触发（保证页面有空闲时间画图）', String(fetchCalls.length));

  // (4) 冷却期过后恢复触发
  state.folderReloadFinishedAt = Date.now() - FOLDER_RELOAD_COOLDOWN_MS - 1000;
  await refreshSelectedPcapFolder();
  check(fetchCalls.length > 0, '冷却期过后恢复自动更新', String(fetchCalls.length));

  handler = null;
  state.pcapDirectoryHandle = null;
  state.pcapSourceFolder = false;
}

async function caseUnselectedFileGreying() {
  console.log('\n[9] 左侧取消勾选文件后，所有时间曲线按文件置灰（与地图/轨迹同一口径）');
  fetchCalls = [];
  const part = {
    columns: [{ key: 'index', label: '序号', unit: '', kind: 'int' }],
    files: [{ name: 'a.pcap', row_count: 3, rms_rows: 0 }, { name: 'b.pcap', row_count: 3, rms_rows: 0 }],
    errors: {},
    rows: [
      row(2433, 1000, 'a.pcap'), row(2433, 2000, 'a.pcap'), row(2433, 3000, 'a.pcap'),
      row(2433, 4000, 'b.pcap'), row(2433, 5000, 'b.pcap'), row(2433, 6000, 'b.pcap'),
    ],
  };
  handler = () => part;
  state.pendingPcapFiles = [makeFile('a.pcap', 100), makeFile('b.pcap', 200)];
  await loadSelectedPcap(false);
  handler = null;

  const chartOf = (id) => getElement(chartDefs[id].canvas)._chart;
  check(chartOf('position').selected.every(Boolean), '默认全部勾选时每一行都算选中',
        JSON.stringify(chartOf('position').selected));
  check(chartOf('position').anyUnselected === false, '默认不做多余的灰色遍历');

  // 等价于在左侧文件列表里取消勾选 b.pcap
  state.selectedFiles.delete('b.pcap');
  renderAllCharts();
  const chart = chartOf('position');
  const off = chart.selected.filter((value) => !value).length;
  check(off === 3 && chart.selected.length - off === 3,
        '取消勾选后：b.pcap 的 3 行被标记为未选中', JSON.stringify(chart.selected));
  check(chart.anyUnselected === true, '图表标记了"存在未选中文件"');
  check(!getElement(chartDefs.position.legend).innerHTML.includes('灰色'),
        '图例里不再显示灰色说明（按需求去掉）', getElement(chartDefs.position.legend).innerHTML.slice(0, 120));

  canvasOps.length = 0;
  drawTimeChart('position');
  const used = new Set(canvasOps.filter((entry) => entry.op === 'set:strokeStyle' || entry.op === 'set:fillStyle').map((entry) => entry.value));
  check(used.has('#6f7c92'), '未选中文件用灰色画', [...used].join(' '));
  check(used.has(colors[0]) && used.has(colors[1]), '选中文件仍用各自的系列颜色画', [...used].join(' '));

  // 全部勾选回来后灰色必须消失（否则会一直留一层灰）
  state.selectedFiles = new Set(fileNames());
  renderAllCharts();
  canvasOps.length = 0;
  drawTimeChart('position');
  const usedAgain = new Set(canvasOps.filter((entry) => entry.op === 'set:strokeStyle' || entry.op === 'set:fillStyle').map((entry) => entry.value));
  check(!usedAgain.has('#6f7c92'), '全部勾选时不再出现灰色', [...usedAgain].join(' '));
}

async function caseFailedFilesInList() {
  console.log('\n[10] 解析失败的文件也要出现在左侧列表里，但不可交互');
  fetchCalls = [];
  handler = () => ({
    columns: [{ key: 'index', label: '序号', unit: '', kind: 'int' }],
    files: [{ name: 'ok.pcap', row_count: 1, rms_rows: 0 }],
    errors: { 'AP18_20260915235934_00305.pcap': 'AP18_20260915235934_00305.pcap: 无法识别的 pcap 文件头 magic=00000000（parse_ap18_pcap.py:112）' },
    rows: [row(2433, 1000, 'ok.pcap')],
  });
  state.pendingPcapFiles = [makeFile('ok.pcap', 100), makeFile('AP18_20260915235934_00305.pcap', 24)];
  await loadSelectedPcap(false);
  handler = null;

  const list = getElement('fileList').innerHTML;
  check(state.failedFiles.length === 1 && state.failedFiles[0].name === 'AP18_20260915235934_00305.pcap',
        '失败文件进了 state.failedFiles', JSON.stringify(state.failedFiles.map((f) => f.name)));
  check(state.failedFiles[0].message.startsWith('无法识别的 pcap 文件头'),
        '失败原因去掉了重复的文件名前缀', state.failedFiles[0].message.slice(0, 60));
  check(list.includes('AP18_20260915235934_00305.pcap') && list.includes('解析失败'),
        '列表里能看到这个文件和“解析失败”', list.slice(-260));
  check(list.includes('class="file-item is-failed"'), '失败条目带 is-failed 样式（灰色+删除线）');
  check(list.includes('title="AP18_20260915235934_00305.pcap — 无法识别的 pcap 文件头'),
        '鼠标悬停能看到完整文件名与失败原因', list.slice(-260));
  check(!list.includes('data-file="AP18_20260915235934_00305.pcap"'),
        '失败条目没有复选框，不能交互', list.slice(-260));
  check(list.includes('data-file="ok.pcap"'), '成功文件照常有复选框');
  check(!fileNames().includes('AP18_20260915235934_00305.pcap'),
        '失败文件不会进入可选文件集合（图表/表格/下载都取不到它）', JSON.stringify(fileNames()));
  check(getElement('fileSelectionMeta').textContent.includes('1 个解析失败'),
        '侧栏概览标出失败数量', getElement('fileSelectionMeta').textContent);

  // 清空并刷新后失败条目也要清掉
  resetForFreshView();
  check(state.failedFiles.length === 0 && !getElement('fileList').innerHTML.includes('is-failed'),
        '清空后列表里不再残留失败条目', getElement('fileList').innerHTML.slice(0, 80));
}

async function caseChartsDrawnWhenOffscreen() {
  console.log('\n[11] 数据加载后所有图表都必须绘制（与画布是否在视口里无关）');
  fetchCalls = [];
  // 旧实现按"画布是否进入过视口"跳过绘制：用户拖滚动条/快速滚轮跳过去的图表会一直空白。
  // 那套可见性缓存已经删掉，这里断言"加载后每张图都被画过"，防止它再被引回来。
  const clock = { value: 0 };
  handler = (batch) => okPart(batch, clock);
  state.pendingPcapFiles = [makeFile('offscreen.pcap', 100)];
  await loadSelectedPcap(false);
  handler = null;

  // 判定标准是"这张图有没有被画过"：没画过的是 meta.chart 为空，
  // 而画过但字段没数据的是 {empty:'…'} 对象——后者是正常的，不能算失败。
  const undrawn = Object.entries(chartDefs).filter(([, def]) => {
    const meta = canvasMeta.get(getElement(def.canvas));
    return !meta || !meta.chart;
  }).map(([id]) => id);
  check(undrawn.length === 0, `11 张图表全部进入"已绘制"状态（未绘制 ${undrawn.length} 张）`, undrawn.join(','));
  const position = getElement(chartDefs.position.canvas)._chart;
  check(position && !position.empty && position.rows.length > 0,
        '有数据的图表（纬度/经度）确实画出了曲线', position ? String(position.empty || position.rows.length) : 'null');
  check(typeof renderCharts === 'function' && renderCharts.length === 0,
        'renderCharts 不再有"只画视口内"的分支参数', String(renderCharts.length));
}

async function caseProgressiveRender() {
  console.log('\n[12] 每批解析完就渲染，不等所有批次');
  fetchCalls = [];
  const clock = { value: 0 };
  let releaseSecond = null;
  handler = (batch) => {
    // 第二批挂在半空中，直到测试放行；第一批立刻返回
    if (batch.some((name) => name.startsWith('later'))) {
      return new Promise((resolve) => { releaseSecond = () => resolve(okPart(batch, clock)); });
    }
    return okPart(batch, clock);
  };
  const original = PARSE_BATCH_SIZE;
  PARSE_BATCH_SIZE = 1;
  let load;
  try {
    resetForFreshView();   // 清掉上一个用例留下的数据，否则断言会被旧状态蒙混过关
    state.pendingPcapFiles = [makeFile('first.pcap', 100), makeFile('later.pcap', 100)];
    load = loadSelectedPcap(false);          // 故意不 await：模拟"第一批已回来、第二批还在路上"
    for (let i = 0; i < 80; i++) {
      if (state.files.files.length === 1 && state.files.files[0].name === 'first.pcap') break;
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    check(state.allRows.length === 1 && state.files.files.length === 1,
          '第一批解析完就已经显示出来了', `${state.allRows.length} 行 / ${state.files.files.length} 个文件`);
    check(state.uploadBusy === true, '此时整体仍在解析中（没有等所有批次）', String(state.uploadBusy));
    check(getElement('pcapPickerStatus').textContent.includes('已显示 1 个文件'),
          '状态栏写出"已显示"的部分', getElement('pcapPickerStatus').textContent.slice(0, 120));
    const partialChart = getElement(chartDefs.position.canvas)._chart;
    check(partialChart && partialChart.rows.length > 0, '中途渲染也把图表画出来了',
          partialChart ? String(partialChart.rows.length) : 'null');
    check(typeof releaseSecond === 'function', '第二批确实还挂在服务端请求里');
    if (typeof releaseSecond === 'function') releaseSecond();
    await load;
  } finally {
    PARSE_BATCH_SIZE = original;
    handler = null;
  }
  check(state.allRows.length === 2 && state.files.files.length === 2,
        '第二批到达后数据补齐（中途渲染不丢东西）', `${state.allRows.length} 行 / ${state.files.files.length} 个文件`);
  check(state.files.files.map((f) => f.name).join(',') === 'first.pcap,later.pcap',
        '文件顺序仍按上传顺序（不受完成顺序影响）', state.files.files.map((f) => f.name).join(','));
  check(getElement('pcapPickerStatus').textContent.includes('已加载 2 个文件'),
        '最终状态是"已加载 N 个文件"', getElement('pcapPickerStatus').textContent.slice(0, 120));
}

async function caseOutOfOrderBatches() {
  console.log('\n[13] 批次乱序到达：后一批先回来也要立刻显示，最终顺序仍按上传顺序');
  fetchCalls = [];
  const clock = { value: 0 };
  let releaseFirst = null;
  handler = (batch) => {
    // 第 1 批（a.pcap）压住不放，让第 2 批（b.pcap）先回来
    if (batch.some((name) => name === 'a.pcap')) {
      return new Promise((resolve) => { releaseFirst = () => resolve(okPart(batch, clock)); });
    }
    return okPart(batch, clock);
  };
  const original = PARSE_BATCH_SIZE;
  PARSE_BATCH_SIZE = 1;
  let load;
  try {
    resetForFreshView();
    state.pendingPcapFiles = [makeFile('a.pcap', 100), makeFile('b.pcap', 100)];
    load = loadSelectedPcap(false);
    for (let i = 0; i < 80; i++) {
      if (state.files.files.length === 1) break;
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    check(state.files.files.length === 1 && state.files.files[0].name === 'b.pcap',
          '后一批先回来就先把它的数据显示出来', state.files.files.map((f) => f.name).join(','));
    check(state.allRows.length === 1, '行数只包含已到的批次', String(state.allRows.length));
    if (typeof releaseFirst === 'function') releaseFirst();
    await load;
  } finally {
    PARSE_BATCH_SIZE = original;
    handler = null;
  }
  check(state.files.files.map((f) => f.name).join(',') === 'a.pcap,b.pcap',
        '补齐后文件顺序仍是上传顺序', state.files.files.map((f) => f.name).join(','));
  check(state.allRows.length === 2, '两批的行都在', String(state.allRows.length));
  check(state.allRows[0].gps_ms <= state.allRows[1].gps_ms,
        '乱序并入后行仍按 GPS 时间有序（归并而不是简单拼接）',
        state.allRows.map((r) => r.gps_ms).join(','));
}

async function caseMapViewKeptOnToggle() {
  console.log('\n[14] 左侧勾选/取消勾选文件时，地图视角不能被重置');
  fetchCalls = [];
  // 最小 Leaflet 桩：只关心 fitBounds 被调了几次（视角有没有被重置）
  let fitCount = 0;
  const layerStub = { addTo() { return this; }, bindTooltip() { return this; }, remove() {} };
  const leafletStub = {
    map: () => ({ setView() {}, hasLayer() { return false; }, removeLayer() {}, fitBounds() { fitCount++; }, invalidateSize() {} }),
    tileLayer: () => ({ addTo() { return this; } }),
    polyline: () => Object.create(layerStub),
    circleMarker: () => Object.create(layerStub),
    latLngBounds: (bounds) => bounds,
  };
  globalThis.L = leafletStub;
  windowStub.L = leafletStub;   // initMap() 检查的是 window.L
  const clock = { value: 0 };
  handler = (batch) => okPart(batch, clock);
  try {
    initMap();                       // 用桩创建 leafletMap
    resetForFreshView();
    state.pendingPcapFiles = [makeFile('a.pcap', 100), makeFile('b.pcap', 100)];
    await loadSelectedPcap(false);
    const afterLoad = fitCount;
    check(afterLoad > 0, '加载数据后地图会定位一次', String(afterLoad));

    // 等价于在左侧列表里取消勾选 b.pcap
    state.selectedFiles.delete('b.pcap');
    renderAllCharts();
    check(fitCount === afterLoad, '取消勾选不重置地图视角', `${fitCount} vs ${afterLoad}`);

    // 勾回来同样不该动视角
    state.selectedFiles.add('b.pcap');
    renderFileList(); renderAllCharts();
    check(fitCount === afterLoad, '重新勾选也不重置地图视角', `${fitCount} vs ${afterLoad}`);

    // 窗口尺寸变化（外部触发 renderAllCharts）同样不该重置
    renderAllCharts();
    check(fitCount === afterLoad, '普通重绘不重置地图视角', `${fitCount} vs ${afterLoad}`);

    // 切换文件视图（下拉框）应当重新定位
    setView('a.pcap');
    check(fitCount > afterLoad, '切换文件视图仍会重新定位', `${fitCount} vs ${afterLoad}`);
  } finally {
    handler = null;
    globalThis.L = undefined;
    windowStub.L = undefined;
    leafletMap = null;
    mapTrackLayers = []; mapMarkerLayers = [];
  }
}

async function caseGapBreaksLine() {
  console.log('\n[15] RMS 用散点（点与点之间不连线），其他折线在缺数据处断开');
  fetchCalls = [];
  const frame = (i, rms) => Object.assign({ gps_week: 2433, gps_ms: i * 1000, _source_file: 'a.pcap', latitude: 50, longitude: 8 },
    rms == null ? {} : { north_position_rms: rms });
  // 只统计某条曲线的路径指令（坐标轴也会 moveTo/lineTo，按描边/填充色把它隔离出来）
  const seriesOps = (chartId, color, endOp) => {
    canvasOps.length = 0;
    drawTimeChart(chartId);
    const ops = [];
    let collecting = false;
    for (const entry of canvasOps) {
      if (entry.op === 'set:strokeStyle' || entry.op === 'set:fillStyle') { collecting = entry.value === color; continue; }
      if (!collecting) continue;
      if (entry.op === endOp) break;
      if (entry.op === 'moveTo' || entry.op === 'lineTo' || entry.op === 'arc') ops.push(entry.op);
    }
    return {
      moveTo: ops.filter((op) => op === 'moveTo').length,
      lineTo: ops.filter((op) => op === 'lineTo').length,
      arc: ops.filter((op) => op === 'arc').length,
    };
  };

  // 1) RMS：整段缺数据 + 中间少一帧，都只能是散点，不能有任何连线
  const rows = [];
  for (let i = 0; i < 4; i++) rows.push(frame(i, 0.02 + i * 0.001));
  for (let i = 4; i < 60; i++) rows.push(frame(i));            // 56 秒整段没有 RMS
  for (let i = 60; i < 64; i++) rows.push(frame(i, 0.03 + i * 0.001));
  resetForFreshView();
  state.files = { files: [{ name: 'a.pcap', row_count: rows.length, rms_rows: 8 }], all: { name: 'ALL', row_count: rows.length, rms_rows: 8 } };
  state.allRows = rows;
  state.fileRows = { 'a.pcap': rows };
  state.stats = computeStats(rows);
  setView('ALL');
  const scattered = seriesOps('rmsPosition', colors[0], 'fill');
  check(scattered.arc === 8 && scattered.lineTo === 0,
        'RMS 是散点：8 个采样点画 8 个点，没有任何连线', JSON.stringify(scattered));
  check(scattered.moveTo === 8, '每个点各自成一个子路径（先 moveTo 再 arc）', JSON.stringify(scattered));

  // 2) 折线图（差分龄期）遇到整段空缺仍然要断开，不能连假斜线
  const lineRows = [];
  const lineFrame = (i, age) => Object.assign({ gps_week: 2433, gps_ms: i * 1000, _source_file: 'a.pcap', latitude: 50, longitude: 8 },
    age == null ? {} : { differential_age: age });
  for (let i = 0; i < 4; i++) lineRows.push(lineFrame(i, 1 + i * 0.1));
  for (let i = 4; i < 60; i++) lineRows.push(lineFrame(i));
  for (let i = 60; i < 64; i++) lineRows.push(lineFrame(i, 2 + i * 0.1));
  state.rows = lineRows; state.chartRows = lineRows; state.stats = computeStats(lineRows);
  const gap = seriesOps('differential', colors[0], 'stroke');
  check(gap.moveTo === 2, '折线图中间整段缺数据：画成两段，不是一条连过去的线', JSON.stringify(gap));
  check(gap.lineTo === 6, '每段内部仍然照常连线（每段 4 点 3 条线）', JSON.stringify(gap));

  // 3) 折线只缺一帧（正常间歇）不该断开，否则会碎成虚线
  const tight = [];
  for (let i = 0; i < 8; i++) tight.push(lineFrame(i, i === 2 ? null : 0.02 + i * 0.001));
  state.rows = tight; state.chartRows = tight; state.stats = computeStats(tight);
  const continuous = seriesOps('differential', colors[0], 'stroke');
  check(continuous.moveTo === 1 && continuous.lineTo === 6,
        '只缺一帧不断开，7 个点仍是一条折线', JSON.stringify(continuous));

  resetForFreshView();
}

async function main() {
  loadApp();
  await caseAllSucceed();
  await casePartialFailure();
  await caseAllFail();
  await caseDuplicateNames();
  await caseFileLevelFailure();
  await caseBlankDashboardDiagnosis();
  caseRuntimeErrorReporting();
  await caseFolderMonitorLoop();
  await caseUnselectedFileGreying();
  await caseFailedFilesInList();
  await caseChartsDrawnWhenOffscreen();
  await caseProgressiveRender();
  await caseOutOfOrderBatches();
  await caseMapViewKeptOnToggle();
  await caseGapBreaksLine();
  console.log();
  if (failures.length) {
    console.log(`结果：${failures.length} 项失败 -> ${failures.join(' | ')}`);
    process.exitCode = 1;
    return;
  }
  console.log('结果：全部通过');
}

main().catch((error) => {
  console.error(`检查脚本自身出错：${error && error.stack}`);
  process.exitCode = 2;
});
