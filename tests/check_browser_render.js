// 真实浏览器渲染自检（headless Chrome/Edge + CDP，无需人工点选文件）。
//
// 为什么需要它：Node 里的 DOM stub 只能证明"数据进了 state、图表对象建好了"，证明不了
// "画布上真的画出了东西"。这里用无头浏览器打开页面，通过 CDP 把真实文件塞进 <input type=file>，
// 等解析完成，然后**直接读画布像素**判断每张图表是否真的绘制了 —— 这类问题（图表空白、
// 数据加载了却什么都没画）只有在真浏览器里才暴露得出来。
//
// 用法：
//   node tests/check_browser_render.js                 # 用 AP18 目录的前 8 个文件
//   node tests/check_browser_render.js --data "C:\path" --count 28
//   node tests/check_browser_render.js --app http://127.0.0.1:8765/   # 用已经在跑的服务
// 退出码 0 = 全部通过；1 = 有图表没画出来/页面报错。

const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const CHROME_CANDIDATES = [
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  process.env.LOCALAPPDATA ? path.join(process.env.LOCALAPPDATA, 'Google', 'Chrome', 'Application', 'chrome.exe') : '',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
];
const PYTHON_CANDIDATES = [
  path.join(os.homedir(), 'AppData', 'Local', 'Programs', 'Python', 'Python312', 'python.exe'),
  'python',
];

const failures = [];
function check(condition, label, detail) {
  console.log(`  ${condition ? '[ OK ]' : '[FAIL]'} ${label}${detail ? ' — ' + detail : ''}`);
  if (!condition) failures.push(label);
}
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function parseArgs(argv) {
  const args = { data: path.join(__dirname, '..', 'AP18'), count: 8, port: 8799, app: null, browser: null, timeout: 300, shot: null };
  for (let i = 0; i < argv.length; i += 2) {
    const key = argv[i].replace(/^--/, ''), value = argv[i + 1];
    if (key === 'data') args.data = value;
    else if (key === 'count') args.count = Number(value);
    else if (key === 'port') args.port = Number(value);
    else if (key === 'app') args.app = value;
    else if (key === 'browser') args.browser = value;
    else if (key === 'timeout') args.timeout = Number(value);
    else if (key === 'shot') args.shot = value;   // 截图存到这个路径（人眼复核排版用）
  }
  return args;
}

async function waitForHttp(url, attempts, gapMs) {
  for (let i = 0; i < attempts; i++) {
    try { const response = await fetch(url); if (response.ok) return true; } catch (_) { /* 还没起来 */ }
    await sleep(gapMs);
  }
  return false;
}

function startServer(port) {
  for (const candidate of PYTHON_CANDIDATES) {
    try {
      const child = spawn(candidate, ['ap18_visualizer.py', '--port', String(port), '--local-only'], {
        cwd: path.join(__dirname, '..'), stdio: 'ignore', detached: false,
      });
      return child;
    } catch (_) { /* 试下一个解释器 */ }
  }
  return null;
}

class Cdp {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 1;
    this.pending = new Map();
    this.errors = [];
    socket.addEventListener('message', (event) => {
      const message = JSON.parse(event.data);
      if (message.id && this.pending.has(message.id)) { this.pending.get(message.id)(message); this.pending.delete(message.id); return; }
      if (message.method === 'Runtime.exceptionThrown') {
        const detail = message.params.exceptionDetails || {};
        this.errors.push(`[异常] ${detail.text || ''} ${(detail.exception && (detail.exception.description || detail.exception.value)) || ''} @${detail.url || ''}:${detail.lineNumber}`);
      }
      if (message.method === 'Runtime.consoleAPICalled' && message.params.type === 'error') {
        this.errors.push(`[console.error] ${(message.params.args || []).map((a) => a.value || a.description || '').join(' ')}`);
      }
      if (message.method === 'Log.entryAdded') {
        const entry = message.params.entry || {};
        // favicon 404 之类与渲染无关，忽略（URL 在 entry.url 里，text 里没有）
        const description = `${entry.text || ''} ${entry.url || ''}`;
        if (entry.level === 'error' && !/favicon/i.test(description)) this.errors.push(`[log.error] ${entry.text} ${entry.url || ''}`);
      }
    });
  }
  send(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = this.nextId++;
      this.pending.set(id, (message) => message.error ? reject(new Error(`${method}: ${message.error.message}`)) : resolve(message.result));
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }
  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    if (result.exceptionDetails) throw new Error(`页面求值失败：${result.exceptionDetails.text}`);
    return result.result.value;
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const browserPath = args.browser || CHROME_CANDIDATES.find((candidate) => candidate && fs.existsSync(candidate));
  if (!browserPath) { console.log('找不到 Chrome/Edge，可用 --browser <exe> 指定'); return 1; }
  if (!fs.existsSync(args.data)) {
    console.log(`数据目录不存在：${args.data}`);
    console.log('仓库里的示例数据 AP18 可能已被清掉，请用 --data 指定自己的采集目录，例如：');
    console.log('    node tests/check_browser_render.js --data "D:\\你的采集目录" --count 28');
    return 1;
  }

  let server = null, serverStarted = false;
  let app = args.app;
  if (!app) {
    server = startServer(args.port);
    if (!server) { console.log('找不到 python，无法自动起服务；可用 --app <url> 指向已在运行的服务'); return 1; }
    serverStarted = true;
    app = `http://127.0.0.1:${args.port}/`;
    if (!await waitForHttp(app, 40, 500)) { console.log(`服务没在 ${app} 起来`); server.kill(); return 1; }
  }

  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'ap18_browser_'));
  const debugPort = args.port + 1000;
  const browser = spawn(browserPath, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--window-size=1600,1000', `--user-data-dir=${profile}`, `--remote-debugging-port=${debugPort}`, 'about:blank',
  ], { stdio: 'ignore' });

  try {
    if (!await waitForHttp(`http://127.0.0.1:${debugPort}/json/version`, 60, 500)) { console.log('DevTools 端口没起来'); return 1; }
    await fetch(`http://127.0.0.1:${debugPort}/json/new?${encodeURIComponent(app)}`, { method: 'PUT' }).catch(() => null);
    await sleep(1500);
    const targets = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
    const page = targets.find((t) => t.type === 'page' && t.url.startsWith('http')) || targets.find((t) => t.type === 'page');
    const socket = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((resolve) => socket.addEventListener('open', resolve));
    const cdp = new Cdp(socket);
    await cdp.send('Runtime.enable');
    await cdp.send('Log.enable');
    await cdp.send('DOM.enable');
    await sleep(2500);

    const names = fs.readdirSync(args.data).filter((name) => /\.(pcap|txt)$/i.test(name)).sort().slice(0, args.count);
    const paths = names.map((name) => path.join(args.data, name));
    console.log(`浏览器：${path.basename(browserPath)} · 数据：${args.data} · ${paths.length} 个文件`);

    const root = await cdp.send('DOM.getDocument', { depth: -1 });
    const input = await cdp.send('DOM.querySelector', { nodeId: root.root.nodeId, selector: '#pcapFilesInput' });
    await cdp.send('DOM.setFileInputFiles', { files: paths, nodeId: input.nodeId });
    await sleep(700);
    if (!await cdp.evaluate('!!(state && state.uploadBusy)')) {
      await cdp.evaluate("document.getElementById('pcapFilesInput').dispatchEvent(new Event('change'))");
    }

    console.log('[1] 等待解析完成');
    const started = Date.now();
    let loaded = false;
    let firstDataAt = null, sawPartialRender = false;
    while ((Date.now() - started) / 1000 < args.timeout) {
      await sleep(1000);
      const snapshot = await cdp.evaluate('JSON.stringify({busy:!!(state&&state.uploadBusy),files:state?state.files.files.length:-1,rows:state?state.allRows.length:-1})').then(JSON.parse).catch(() => ({ busy: true, files: -1, rows: -1 }));
      if (snapshot.rows > 0 && firstDataAt === null) firstDataAt = (Date.now() - started) / 1000;
      if (snapshot.busy && snapshot.rows > 0) sawPartialRender = true;
      if (!snapshot.busy && snapshot.files >= 0 && snapshot.rows > 0) { loaded = true; break; }
      if (!snapshot.busy && snapshot.files === 0 && (Date.now() - started) / 1000 > 10) break;
    }
    const totalSeconds = (Date.now() - started) / 1000;
    check(loaded, `解析完成且拿到数据（${totalSeconds.toFixed(1)}s）`);
    console.log(`  首屏数据 ${firstDataAt === null ? '—' : firstDataAt.toFixed(1) + 's'}（全部 ${totalSeconds.toFixed(1)}s）`);
    // 多批时应当出现"还在解析、但已经有数据"的中间状态：即分批渲染生效。
    if (paths.length > 8) check(sawPartialRender, '每批解析完就渲染（没有等全部批次）', '未观察到中间渲染');

    console.log('[2] 不滚动，直接检查每张图表的画布像素');
    const report = await cdp.evaluate(`(() => {
      const painted = (canvas) => {
        const ctx = canvas.getContext('2d');
        try {
          const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
          const background = [data[0], data[1], data[2], data[3]].join(',');
          let different = 0;
          for (let i = 0; i < data.length; i += 4) if ([data[i], data[i+1], data[i+2], data[i+3]].join(',') !== background) different++;
          return different;
        } catch (error) { return -1; }
      };
      const charts = [];
      for (const [id, def] of Object.entries(chartDefs)) {
        const canvas = document.getElementById(def.canvas);
        const meta = canvasMeta.get(canvas);
        const chart = meta && meta.chart;
        charts.push({ id, drawn: !!chart, empty: chart ? !!chart.empty : false, rows: chart && chart.rows ? chart.rows.length : 0, painted: painted(canvas) });
      }
      return {
        charts,
        trajectory: painted(document.getElementById('trajectoryCanvas')),
        status: (document.getElementById('pcapPickerStatus') || {}).textContent,
        notice: (document.getElementById('notice') || {}).textContent,
        rows: state.allRows.length,
        failures: (state.failedFiles || []).length,
        noTimeSlider: !document.getElementById('rangeStart') && !document.getElementById('rangeEnd'),
        rmsPanel: (document.getElementById('rmsExceedTable') || {}).textContent || '',
        rmsNote: (document.getElementById('rmsExceedNote') || {}).textContent || '',
        rmsCardsGone: !document.querySelector('[id^="statRmsOver"]'),
        // 位置 RMS 曲线的采样点数与路径结构：点数太少说明稀疏序列被抽掉了，
        // moves > 1 说明在空缺处断开了（而不是一条假直线连过去）。
        rmsSeries: (() => {
          const canvas = document.getElementById(chartDefs.rmsPosition.canvas);
          const meta = canvasMeta.get(canvas);
          const chart = meta && meta.chart;
          if (!chart || chart.empty) return { valid: 0, moves: 0, lines: 0 };
          let valid = 0;
          for (const row of chart.rows) if (typeof row.north_position_rms === 'number' && Number.isFinite(row.north_position_rms)) valid++;
          // 曲线画在离屏缓存层（meta.base）上，所以钩那块的 2D 上下文才能数到路径指令
          const bctx = meta.base && meta.base.getContext('2d');
          if (!bctx) return { valid, moves: -1, lines: -1, arcs: -1 };
          const moveTo = bctx.moveTo, lineTo = bctx.lineTo, arc = bctx.arc;
          let moves = 0, lines = 0, arcs = 0, seenArc = false;
          bctx.moveTo = function (...args) { moves++; return moveTo.apply(this, args); };
          // 坐标轴网格也画在同一个上下文上（在散点之前），所以只统计"第一个散点之后"的连线：
          // 散点序列本身一条线都不该有。
          bctx.lineTo = function (...args) { if (seenArc) lines++; return lineTo.apply(this, args); };
          bctx.arc = function (...args) { seenArc = true; arcs++; return arc.apply(this, args); };
          try { drawTimeChart('rmsPosition'); } finally { delete bctx.moveTo; delete bctx.lineTo; delete bctx.arc; }
          return { valid, moves, lines, arcs };
        })(),
        panelAfterPositionRms: (() => {
          const position = document.getElementById(chartDefs.position.canvas);
          const panel = document.getElementById('rmsExceedTable');
          const positionRms = document.getElementById('rmsPositionCanvas');
          if (!position || !panel || !positionRms) return false;
          // 面板必须排在"位置 RMS"画布之后（用文档顺序比较）
          return positionRms.compareDocumentPosition(panel) & Node.DOCUMENT_POSITION_FOLLOWING ? true : false;
        })(),
      };
    })()`);

    for (const chart of report.charts) {
      if (chart.empty) { console.log(`  [skip] ${chart.id.padEnd(14)} 该数据集没有这些字段（画了空图）`); continue; }
      check(chart.drawn && chart.painted > 200, `${chart.id.padEnd(14)} 已绘制 rows=${chart.rows} painted=${chart.painted}`);
    }
    check(report.trajectory > 200, `局部平面轨迹 painted=${report.trajectory}`);
    check(report.noTimeSlider, '界面上已经没有时间窗口滑块');
    check(report.panelAfterPositionRms, 'RMS 超限比例面板排在位置 RMS 图之后');
    // 把实际渲染出来的 RMS 超限统计打印出来：两个阈值都必须有值，不能只统计 >0.1
    const panelText = String(report.rmsPanel).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
    console.log(`  RMS 明细表：${panelText}`);
    check(/> 0\.1 m 帧数/.test(report.rmsPanel) && /> 0\.05 m 帧数/.test(report.rmsPanel)
          && /> 0\.1 m 占比/.test(report.rmsPanel) && /> 0\.05 m 占比/.test(report.rmsPanel)
          && /运动帧（分母 > 5 km\/h）/.test(report.rmsPanel) && /已排除 ≤ 5 km\/h/.test(report.rmsPanel),
          'RMS 明细表同时统计 >0.1/>0.05 的帧数与占比，并只算运动帧（>5 km/h）', panelText.slice(0, 260));
    check(/只统计运动帧/.test(String(report.rmsNote)) && /车速 > 5 km\/h/.test(String(report.rmsNote)),
          '表尾标注了运动帧口径（车速 > 5 km/h）', String(report.rmsNote).slice(0, 160));
    check(report.rmsCardsGone, '概览区里没有重复的 RMS 超限卡片');
    const series = report.rmsSeries || { valid: 0, moves: 0, lines: 0, arcs: 0 };
    console.log(`  位置 RMS 散点：采样点数=${series.valid} · 画点=${series.arcs} · 连线=${series.lines}`);
    check(series.valid > 1000, '位置 RMS 稀疏序列没有被抽样丢掉（点数足够）', String(series.valid));
    check(series.lines === 0 && series.arcs >= series.valid,
          'RMS 是散点：每个采样一个点，没有任何连线（空缺处直接没有点）',
          `arcs=${series.arcs} lines=${series.lines} valid=${series.valid}`);

    // 左侧勾选/取消勾选文件时，地图视角不能被重置（fitBounds 会把用户拖拽缩放冲掉）
    const before = await cdp.evaluate('JSON.stringify({lat:leafletMap.getCenter().lat,lng:leafletMap.getCenter().lng,zoom:leafletMap.getZoom()})').then(JSON.parse);
    const toggled = await cdp.evaluate(`(() => {
      const input = document.querySelector('#fileList input.file-toggle');
      if (!input) return false;
      input.checked = !input.checked;
      input.dispatchEvent(new Event('change', { bubbles: true }));
      return true;
    })()`);
    await sleep(600);
    const after = await cdp.evaluate('JSON.stringify({lat:leafletMap.getCenter().lat,lng:leafletMap.getCenter().lng,zoom:leafletMap.getZoom()})').then(JSON.parse);
    check(toggled, '找到了可切换的文件复选框');
    check(before.lat === after.lat && before.lng === after.lng && before.zoom === after.zoom,
          '勾选/取消勾选文件不会重置地图视角',
          `改前 ${before.lat.toFixed(5)},${before.lng.toFixed(5)} z${before.zoom} → 改后 ${after.lat.toFixed(5)},${after.lng.toFixed(5)} z${after.zoom}`);

    console.log('[3] 页面状态');
    console.log(`  ${String(report.status || '').replace(/\s+/g, ' ').slice(0, 160)}`);
    if (report.notice) console.log(`  横幅：${String(report.notice).replace(/\s+/g, ' ').slice(0, 200)}`);
    check(cdp.errors.length === 0, '页面没有脚本错误', cdp.errors.slice(0, 2).join(' | '));

    if (args.shot) {
      const box = await cdp.evaluate(`(() => {
        const rect = document.getElementById('rmsExceedTable').closest('article').getBoundingClientRect();
        return JSON.stringify({ x: Math.max(0, rect.left - 12), y: Math.max(0, rect.top + window.scrollY - 12), width: Math.min(rect.width + 24, 1600), height: rect.height + 24 });
      })()`).then(JSON.parse);
      const shot = await cdp.send('Page.captureScreenshot', { format: 'png', clip: { ...box, scale: 1 }, captureBeyondViewport: true });
      fs.writeFileSync(args.shot, Buffer.from(shot.data, 'base64'));
      console.log(`  截图已保存：${args.shot}`);
    }

    console.log();
    if (failures.length) { console.log(`结果：${failures.length} 项失败 -> ${failures.join(' | ')}`); return 1; }
    console.log('结果：全部通过');
    return 0;
  } finally {
    browser.kill();
    if (serverStarted && server) server.kill();
    await sleep(300);
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (_) { /* ignore */ }
  }
}

main().then((code) => { process.exitCode = code; }).catch((error) => { console.error(`检查脚本自身出错：${error && error.stack}`); process.exitCode = 2; });
