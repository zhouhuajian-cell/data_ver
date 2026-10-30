// 交付自检（Node 运行，不依赖可用的 PowerShell）；脚本在 tests/ 下，路径按仓库根目录解析：
//   1. 用 Node 的 process.execPath 子进程跑 python -m py_compile，校验所有 .py 语法；
//   2. 用 `node --check` 校验 web/app.js 语法；
//   3. 静态断言关键实现点是否都在（进程池、gzip 协商、前端并发批次）。
// 子进程输出重定向到临时文件，避免受限模式下"管道捕获 stdio"被拦（EPERM）。
// 任一项失败 -> 非零退出码。

const { spawn, execFile } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

// 脚本自己就在 tests/ 下，仓库根目录是上一级
const root = path.join(__dirname, '..');
const failures = [];
const logDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ap18_check_'));

function runToFile(command, args, outName) {
  return new Promise((resolve) => {
    const outFile = path.join(logDir, outName);
    const handle = fs.openSync(outFile, 'w');
    const child = spawn(command, args, { cwd: root, stdio: ['ignore', handle, handle] });
    child.on('error', (error) => {
      fs.closeSync(handle);
      resolve({ status: -1, output: `spawn failed: ${error.message}`, file: outFile });
    });
    child.on('close', (code) => {
      fs.closeSync(handle);
      let output = '';
      try { output = fs.readFileSync(outFile, 'utf8'); } catch (_) { /* ignore */ }
      resolve({ status: code, output: output.trim(), file: outFile });
    });
  });
}

function assert(condition, label, detail) {
  if (condition) {
    console.log(`  [ OK ] ${label}`);
    return true;
  }
  failures.push(label);
  console.log(`  [FAIL] ${label}${detail ? ' — ' + detail : ''}`);
  return false;
}

async function main() {
  const pythonCandidates = [
    path.join(os.homedir(), 'AppData', 'Local', 'Programs', 'Python', 'Python312', 'python.exe'),
    'python',
  ];
  const pyFiles = ['ap18_visualizer.py', 'parse_ap18_pcap.py', 'tests/bench_pipeline.py', 'tests/bench_concurrency.py',
    'tests/test_parse_api.py', 'tests/test_fault_tolerance.py', 'tests/check_track.py', 'tests/diagnose_pcap.py'];

  console.log('[1] Python 语法（python -m py_compile）');
  let usedPython = null;
  for (const candidate of pythonCandidates) {
    const probe = await runToFile(candidate, ['-c', 'print(1)'], 'probe.txt');
    if (probe.status === 0) { usedPython = candidate; break; }
    console.log(`        试 ${candidate} 失败：${probe.output || probe.status}`);
  }
  if (!usedPython) {
    assert(false, '找到可用的 python 解释器', '所有候选都失败；说明本会话无法启动 python');
  } else {
    console.log(`        使用解释器：${usedPython}`);
    for (const file of pyFiles) {
      // 文件名里带 tests/ 这样的分隔符会破坏临时日志路径，先压成扁平名字
      const logName = `pyc_${file.replace(/[\\/]/g, '_')}.txt`;
      const result = await runToFile(usedPython, ['-m', 'py_compile', file], logName);
      assert(result.status === 0, `py_compile ${file}`, result.output.split('\n').slice(-4).join(' / '));
    }
  }

  console.log('\n[2] 前端 JS 语法（node --check）');
  const jsCheck = await runToFile(process.execPath, ['--check', path.join(root, 'web', 'app.js')], 'js.txt');
  assert(jsCheck.status === 0, 'node --check web/app.js', jsCheck.output.split('\n').slice(-4).join(' / '));
  const browserCheck = await runToFile(process.execPath, ['--check', path.join(root, 'tests', 'check_browser_render.js')], 'js_browser.txt');
  assert(browserCheck.status === 0, 'node --check check_browser_render.js', browserCheck.output.split('\n').slice(-4).join(' / '));

  console.log('\n[3] 关键实现点静态断言');
  const read = (rel) => fs.readFileSync(path.join(root, rel), 'utf8');
  const backend = read('ap18_visualizer.py');
  const frontend = read('web/app.js');
  const styles = read('web/style.css');
  const readme = read('README_AP18_VISUALIZER.md');

  assert(/ProcessPoolExecutor\(max_workers=workers, mp_context=ctx\)/.test(backend), '后端创建解码进程池（spawn）');
  assert(/DECODER_POOL\.submit\(decode_pcap_task, str\(temp_path\)\)/.test(backend), 'pcap 解码提交到进程池');
  assert(/DECODER_POOL\.submit\(\s*\n?\s*parse_txt_text_task/.test(backend), 'txt 解析提交到进程池');
  assert(/rows, failure = future\.result\(\)/.test(backend) && /rows_by_index\[index\] = \(rows, failure or ""\)/.test(backend), '结果按上传下标回填（不依赖完成顺序）');
  assert(/def start_decoder_pool/.test(backend) && /def stop_decoder_pool/.test(backend), '进程池有创建/销毁生命周期');
  assert(/gzip\.compress\(body, 1\)/.test(backend), '响应按 gzip 压缩');
  assert(/gzip_ok and gzip_requested and len\(body\) >= GZIP_MIN_BYTES/.test(backend), 'gzip 只在协商且超过阈值时启用');
  assert(/headers\["Content-Encoding"\] = "gzip"/.test(backend), '设置 Content-Encoding: gzip');
  assert(/lambda row: int\(row\.get\("gps_week"\)/.test(backend) || /def gps_sort_key/.test(backend), '同批内仍按 GPS 时间排序');
  assert(/server\.request_logging = args\.verbose/.test(backend), '--verbose 计时日志接线');
  assert(/def failure_text\(exc: BaseException\)/.test(backend), '坏文件只回传一行原因（failure_text）');
  assert(/INPUT_ERRORS = \(ValueError, OSError, EOFError, struct\.error\)/.test(backend), '数据问题的异常集合（traceback 只留给解析器 bug）');
  assert(/errors\[name\] = named_error\(name, failure\)/.test(backend), '错误文本里文件名只出现一次');

  assert(/const MAX_PARALLEL_BATCHES = 4;/.test(frontend), '前端并发批次数常量');
  assert(/position_rms_exceed: positionRmsExceedOf\(rows\)/.test(frontend), '前端统计含位置 RMS 超限比例');
  assert(/RMS_POSITION_THRESHOLDS = \[\{ value: 0\.1/.test(frontend), '位置 RMS 超限阈值为 0.1 m / 0.05 m');
  assert(/const RMS_MOVING_SPEED_KMH = 5;/.test(frontend)
        && /if \(!\(speed > RMS_MOVING_SPEED_MPS\)\) return;/.test(frontend)
        && /ratio\[t\.text\] = moving \? over\[t\.text\] \/ moving : 0/.test(frontend),
        'RMS 超限比例只统计运动帧（车速 > 5 km/h），静止/低速帧不计入分子分母');
  assert(/只统计运动帧/.test(frontend) && /车速 > \$\{speedKmh\} km\/h/.test(frontend), '表尾标注了运动帧口径（车速 > 5 km/h）');
  assert(/车速 &gt; 5 km\/h 且有位置 RMS 的帧数/.test(read('web/index.html')), '面板标题标注了车速条件');
  assert(/const SCATTER_RADIUS = 1\.7;/.test(frontend) && /if \(chart\.def && chart\.def\.scatter\)/.test(frontend),
        'RMS 用散点渲染（点不连线）');
  assert((frontend.match(/scatter: true/g) || []).length === 3, '位置/速度/姿态 RMS 三张图都标了 scatter');
  assert(/const GAP_BREAK_FACTOR = 6;/.test(frontend) && /t - prevTime > breakGap/.test(frontend),
        '折线在明显缺数据处断开（不再连假斜线）');
  assert(/indices\.length\*2>rows\.length\) continue;/.test(frontend),
        '只有真的稀疏的字段才整行保留（否则 RMS 行会被抽样抽掉）');
  assert(/const useEnvelope = !gappy && drawnCount > plotW \* 1\.5;/.test(frontend),
        '有缺口的序列走折线、密集序列走包络带');
  assert(/function ratioClass\(value\)/.test(frontend) && /\.pct-high \{ color: #ff7e8b/.test(styles), 'RMS 百分比按严重程度高亮（灰/蓝/橙/红）');
  assert(/function emptyViewHint\(rows\)/.test(frontend), '加载成功但画不出曲线时前端自己说出原因');
  assert(/function setupRuntimeErrorReporting\(\)/.test(frontend), 'rAF/观察器里的异常会显示出来（不再静默空白）');
  assert(/function drawTimeSeriesPass\(ctx, chart, series, color, alpha, wantSelected\)/.test(frontend), '时间曲线按文件分两遍画（灰色=未选中、本色=选中）');
  assert(/anyUnselected/.test(frontend), '全部勾选时跳过灰色遍历');
  assert(!/灰色=左侧未勾选的文件/.test(frontend), '图例不显示灰色说明（按需求去掉）');
  // 超限比例面板必须紧跟在"位置 RMS"图后面（不能留到最后面的信息条里）
  const panelHtml = read('web/index.html');
  const positionAt = panelHtml.indexOf('rmsPositionCanvas');
  const exceedAt = panelHtml.indexOf('rmsExceedTable');
  const velocityAt = panelHtml.indexOf('rmsVelocityCanvas');
  assert(positionAt >= 0 && exceedAt > positionAt && exceedAt < velocityAt,
        '超限比例面板紧跟在位置 RMS 图后面', `位置=${positionAt} 超限=${exceedAt} 速度=${velocityAt}`);
  assert(/function folderSignature\(files\)/.test(frontend) && /FOLDER_RELOAD_COOLDOWN_MS/.test(frontend), '文件夹监控只认新增文件且有冷却期');
  assert(/MAX_TRACK_POINTS = 200000/.test(frontend), '轨迹点有上限（按屏幕分辨率抽样）');
  assert(/function mergeSortedRows\(left, right\)/.test(frontend), '分批渲染用归并保持行有序');
  assert(/const PROGRESS_RENDER_MS = 1500;/.test(frontend), '分批渲染有节流');
  assert(/absorb\(index\);\s*\n\s*scheduleProgress\(\)/.test(frontend), '每批解析完就并入视图并调度渲染');
  assert(/applyParsed\(\{columns:merged\.columns/.test(frontend) && /banners:false/.test(frontend), '中途渲染不弹横幅');
  assert(!/timeWindowBounds|state\.start|state\.end|rangeStart/.test(frontend), '时间窗口功能已彻底移除');
  assert(!/timeWindowBounds|rangeStart/.test(read('web/index.html')), '界面上没有时间窗口控件');
  assert(/function renderCharts\(\) \{\s*\n\s*Object\.keys\(chartDefs\)\.forEach\(id => drawTimeChart\(id\)\)/.test(frontend), '图表始终全部绘制（没有视口可见性分支）');
  assert(/if\(state\.mapNeedsFit\)\{ leafletMap\.fitBounds/.test(frontend), '只有换数据集/换视图时才重新定位地图（勾选文件不重置视角）');
  assert(/state.failedFiles=Object.entries\(body.errors/.test(frontend), '解析失败的文件进入侧栏列表');
  assert(/file-item is-failed/.test(frontend), '失败条目不可交互（无复选框）');
  assert(/\.file-name \{[^}]*overflow-wrap: anywhere/.test(styles), '左侧文件名完整显示（换行，不截断）');
  assert(!/\.file-name \{[^}]*text-overflow: ellipsis/.test(styles), '文件名不再用省略号截断');
  assert(/\.workspace \{ display: grid; grid-template-columns: 300px/.test(styles), '侧栏加宽到 300px，名字一行能放下');
  assert(/await Promise\.all\(Array\.from\(\{length:concurrency\},\s*\(\)=>runBatch\(\)\)\)/.test(frontend), '前端批次并发执行');
  assert(/if\(!merged\.rows\.length && \(state\.allRows\|\|\[\]\)\.length\)/.test(frontend), '一个可用文件都没有时保留原有视图');
  assert(/merged\.rows=mergeSortedRows\(merged\.rows,part\.rows\)/.test(frontend), '跨批次合并用归并保证全局有序');
  assert(/if\(firstError&&!hasAnyResult\)throw firstError;/.test(frontend), '只有全部批次都失败才整体报错（部分失败照样显示已到的数据）');
  assert(/function errorDetail\(text\)/.test(frontend), '错误横幅压成一行摘要（traceback 只留结论）');

  assert(/--workers/.test(readme) && /--verbose/.test(readme), 'README 记录 --workers / --verbose');
  assert(/验收清单/.test(readme), 'README 有验收清单');

  console.log(`\n日志目录：${logDir}`);
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
