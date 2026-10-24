const state = { files: [], file: 'ALL', rows: [], trackSegments: [], selectedFiles: new Set(), selectionInitialized: false, revision: null, sourceMode: 'txt', pollTimer: null, pollBusy: false, pendingPcapFiles: [], pcapDirectoryHandle: null, pcapFolderSignature: '', pcapFolderTimer: null, pcapSourceFolder: false, uploadBusy: false, folderReloadFinishedAt: 0, mapNeedsFit: true, columns: [], stats: null, page: 1, pageSize: 50, search: '', chartHover: new Map(), selection: null, allRows: [], fileRows: {}, chartRows: [], blobs: {}, failedFiles: [] };
let tableTooltipPinned = false;
const MAX_CHART_POINTS = 12000;
// 这些字段只出现在部分行上（GSOF50 间歇发送、GGA 为 1 Hz），降采样时必须整行保留，
// 否则曲线会被抽成空白。
const SPARSE_KEYS = ['north_position_rms', 'differential_age', 'satellites_used'];
// 密集曲线的包络带不透明度：整条曲线只用一个值，保证沿线浓度一致。
// 0.85 ≈ 看上去是实色，只在多条曲线重叠处留一点点层次；改成 1 就是完全不透明。
const ENVELOPE_ALPHA = .85;
// 轨迹（地图 + 局部平面轨迹）的点预算。单个文件 6000 个点早就超过屏幕像素分辨率，
// 但 305 个文件全量就是 180 万个点：地图要为每个点建一个 LatLng，局部轨迹要为每个点
// 做一次投影，每次重绘都新分配几百 MB，浏览器标签页会被拖垮（表现就是"提示解析成功，
// 但图表全空/页面卡死"）。曲线图本来就有 MAX_CHART_POINTS 兜底，轨迹同样设上限：
// 合计 20 万点，每个文件至少 200 点，保证多文件时每条轨迹形状完整、终点仍在。
const MAX_TRACK_POINTS = 200000;
const MIN_TRACK_POINTS_PER_SEGMENT = 200;
// 位置 RMS 超限统计：分子是该分量 RMS 大于阈值的帧数，分母是**总帧数**（导航记录
// 行数，含未附带 GSOF50 的行）。分母刻意不用"RMS 有效行数"：GSOF50 只间歇发送，
// 拿有效行当分母会把"大部分帧根本没有 RMS"这件事掩盖掉。阈值顺序与页面文案一致。
const RMS_POSITION_FIELDS = [
  { key: 'north_position_rms', label: '北位置RMS', short: '北' },
  { key: 'east_position_rms', label: '东位置RMS', short: '东' },
  { key: 'down_position_rms', label: '下位置RMS', short: '下' }
];
const RMS_POSITION_THRESHOLDS = [{ value: 0.1, text: '0.1' }, { value: 0.05, text: '0.05' }];
// RMS 超限比例的车速过滤：只有"运动帧"参与统计（车速 > 5 km/h = 1.39 m/s）。
// 停车/低速时 RMS 天然更小，混进分母会把动态精度指标做漂亮，所以在分子分母里都剔除。
const RMS_MOVING_SPEED_KMH = 5;
const RMS_MOVING_SPEED_MPS = RMS_MOVING_SPEED_KMH / 3.6;
let leafletMap = null, mapTrackLayers = [], mapMarkerLayers = [], mapSelectionMarker = null;
const colors = ['#64a8ff', '#54d6d2', '#72dc9b', '#f3bb67', '#ed8bc5', '#a692ff', '#ff7e8b', '#9cabc7'];
const chartDefs = {
  position: { canvas: 'positionCanvas', legend: 'legend-position', series: [{key:'latitude',label:'纬度',unit:'deg'},{key:'longitude',label:'经度',unit:'deg'}], yLabel:'纬度 / 经度 (deg)' },
  altitude: { canvas: 'altitudeCanvas', legend: 'legend-altitude', series: [{key:'altitude',label:'高程',unit:'m'}], yLabel:'椭球高 (m)' },
  velocity: { canvas: 'velocityCanvas', legend: 'legend-velocity', series: [{key:'north_velocity',label:'北速度',unit:'m/s'},{key:'east_velocity',label:'东速度',unit:'m/s'},{key:'down_velocity',label:'下速度',unit:'m/s'},{key:'total_speed',label:'总速度',unit:'m/s'}], yLabel:'速度 (m/s)' },
  attitude: { canvas: 'attitudeCanvas', legend: 'legend-attitude', series: [{key:'roll',label:'横滚',unit:'deg'},{key:'pitch',label:'俯仰',unit:'deg'},{key:'heading',label:'航向',unit:'deg'},{key:'track_angle',label:'航迹角',unit:'deg'}], yLabel:'姿态 (deg)' },
  inertial: { canvas: 'inertialCanvas', legend: 'legend-inertial', series: [{key:'angular_rate_x',label:'角速度X',unit:'deg/s'},{key:'angular_rate_y',label:'角速度Y',unit:'deg/s'},{key:'angular_rate_z',label:'角速度Z',unit:'deg/s'},{key:'acceleration_x',label:'加速度X',unit:'m/s²'},{key:'acceleration_y',label:'加速度Y',unit:'m/s²'},{key:'acceleration_z',label:'加速度Z',unit:'m/s²'}], yLabel:'角速度 / 加速度（单位见图例）' },
  satellites: { canvas: 'satellitesCanvas', legend: 'legend-satellites', series: [{key:'satellites_used',label:'定位使用卫星数',unit:'颗'}], yLabel:'搜星数 (颗)' },
  differential: { canvas: 'differentialCanvas', legend: 'legend-differential', series: [{key:'differential_age',label:'差分龄期',unit:'s'}], yLabel:'差分龄期 (s)' },
  hdop: { canvas: 'hdopCanvas', legend: 'legend-hdop', series: [{key:'hdop',label:'HDOP',unit:''}], yLabel:'HDOP' },
  rmsPosition: { canvas: 'rmsPositionCanvas', legend: 'legend-rms-position', scatter: true, series: [{key:'north_position_rms',label:'北位置RMS',unit:'m'},{key:'east_position_rms',label:'东位置RMS',unit:'m'},{key:'down_position_rms',label:'下位置RMS',unit:'m'}], yLabel:'位置 RMS (m)' },
  rmsVelocity: { canvas: 'rmsVelocityCanvas', legend: 'legend-rms-velocity', scatter: true, series: [{key:'north_velocity_rms',label:'北速度RMS',unit:'m/s'},{key:'east_velocity_rms',label:'东速度RMS',unit:'m/s'},{key:'down_velocity_rms',label:'下速度RMS',unit:'m/s'}], yLabel:'速度 RMS (m/s)' },
  rmsAttitude: { canvas: 'rmsAttitudeCanvas', legend: 'legend-rms-attitude', scatter: true, series: [{key:'roll_rms',label:'横滚RMS',unit:'deg'},{key:'pitch_rms',label:'俯仰RMS',unit:'deg'},{key:'heading_rms',label:'航向RMS',unit:'deg'}], yLabel:'姿态 RMS (deg)' }
};
for (const id of Object.keys(chartDefs)) chartDefs[id].visible = new Set(chartDefs[id].series.map(s => s.key));
const $ = id => document.getElementById(id);
const finite = value => typeof value === 'number' && Number.isFinite(value);
const fmt = (value, digits=6) => value == null || !finite(Number(value)) ? '—' : Number(value).toFixed(digits);
const fmtCompact = value => { if (value == null || !finite(Number(value))) return '—'; const n=Number(value); return Math.abs(n)>=100 ? n.toFixed(2) : Math.abs(n)>=1 ? n.toFixed(3) : n.toFixed(6); };
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function api(path, options={}) { const response = await fetch(path, options); const body = await response.json(); if (!response.ok || body.error) throw new Error(body.error || response.statusText); return body; }
// 横幅只放压平 + 截断后的一行摘要：一条服务端解析错误以前会把整段 Python traceback
// 塞进横幅（十几行内部栈帧），把页面顶下去还看不出真正原因。完整文本留在 title 里，
// 鼠标悬停仍能看到全部细节。
const ERROR_DETAIL_LIMIT = 200;
function errorDetail(text) {
  let flat = String(text == null ? '' : text).replace(/\s+/g, ' ').trim();
  // 服务端现在只发一行原因；这里再兜一道：万一拿到的是 traceback（老版本服务端、
  // 或者真的出了解析器 bug），也只留"哪个文件 + 最后那句异常 + 抛出的文件:行号"，
  // 中间的栈帧对用户没有意义。完整原文始终留在 title 里。
  const marker = 'Traceback (most recent call last):';
  const at = flat.indexOf(marker);
  if (at >= 0) {
    const prefix = flat.slice(0, at).replace(/[:\s]+$/, '').trim();
    const body = flat.slice(at + marker.length);
    const exception = (body.match(/[A-Za-z_][\w.]*(?:Error|Exception)\s*:\s.*$/) || [])[0] || body.trim().slice(-120);
    const frames = [...body.matchAll(/File "([^"]+)", line (\d+)/g)];
    const where = frames.length ? `${frames[frames.length - 1][1]}:${frames[frames.length - 1][2]}` : '';
    flat = [prefix, exception, where ? `（${where}）` : ''].filter(Boolean).join('：').replace(/：（/g, '（');
  }
  return flat.length > ERROR_DETAIL_LIMIT ? `${flat.slice(0, ERROR_DETAIL_LIMIT)}…` : flat;
}
function showError(message, fullText) {
  const notice = $('notice');
  notice.textContent = message;
  notice.title = fullText ? String(fullText) : '';
  notice.classList.add('show');
}
// 多条失败（每个文件一条）时只列前 3 条：横幅要能一眼看完，剩下的进 title 与状态栏。
function showErrorList(heading, details, extra) {
  const list = (details || []).filter(Boolean);
  if (!list.length) { if (extra) showError(extra); return; }
  const shown = list.slice(0, 3);
  const rest = list.length - shown.length;
  showError([heading, ...shown.map(errorDetail), rest > 0 ? `（其余 ${rest} 条略，悬停查看完整信息）` : '', extra || ''].filter(Boolean).join('\n'), list.join('\n'));
}
function clearError() {
  // 顺便清掉文本：只加/去 .show 会把上一次的报错留在 DOM 与无障碍树里，
  // 看起来像"这次也报错了"（真实文本还在，只是被 CSS 隐藏）。
  const notice = $('notice');
  notice.classList.remove('show');
  notice.textContent = '';
  notice.title = '';
}
// rAF / ResizeObserver / IntersectionObserver 回调里抛出的异常不会进 loadSelectedPcap 的
// try/catch：以前它们只留在浏览器控制台，界面上就表现为"数据显示成功，但所有图表空白"。
// 这里把它们兜住并显示到横幅上，用户能直接把原因贴出来，而不是只看到一片空白。
let runtimeErrorReports = 0;
function reportRuntimeError(kind, message, where) {
  if (runtimeErrorReports++ > 2) return;  // 只提示前几次，避免每帧刷屏
  showError(`${kind}：${errorDetail(message)}${where ? `（${where}）` : ''}`);
}
function setupRuntimeErrorReporting() {
  if (!window || typeof window.addEventListener !== 'function') return;
  window.addEventListener('error', event => {
    const message = (event && (event.message || (event.error && event.error.message))) || '未知脚本错误';
    const where = event && event.filename ? `${event.filename}:${event.lineno || 0}` : '';
    reportRuntimeError('脚本运行出错（请反馈这条信息）', message, where);
  });
  window.addEventListener('unhandledrejection', event => {
    const reason = event && event.reason;
    reportRuntimeError('异步任务出错（请反馈这条信息）', (reason && (reason.stack || reason.message)) || String(reason || '未知原因'), '');
  });
}
function currentFileMeta() { return state.file === 'ALL' ? state.files.all : state.files.files.find(f => f.name === state.file); }
function setText(id, value) { $(id).textContent = value; }
function fileNames() { return (state.files.files || []).map(f => f.name); }
function renderFileList() {
  const files = state.files.files || [];
  const selectedCount = files.filter(f => state.selectedFiles.has(f.name)).length;
  const failed = state.failedFiles || [];
  // 解析失败的文件也列出来（否则用户只能从横幅里看到它们，列表看起来像"文件被吞了"），
  // 但不给复选框、不给任何交互：它们没有数据行，进不了图表、表格和下载。
  const failedHtml = failed.map(f =>
    `<div class="file-item is-failed" title="${esc(f.name)} — ${esc(f.message || '解析失败')}"><span class="file-dot"></span><span class="file-name">${esc(f.name.replace(/\.txt$/,''))}</span><span class="file-count">解析失败</span></div>`
  ).join('');
  $('fileList').innerHTML = (files.map(f => {
    const selected = state.selectedFiles.has(f.name), current = state.file === f.name;
    return `<div class="file-item ${selected ? '' : 'is-off'} ${current ? 'current' : ''}"><label><input class="file-toggle" type="checkbox" data-file="${esc(f.name)}" ${selected ? 'checked' : ''} aria-label="显示 ${esc(f.name)} 轨迹"><span class="file-dot"></span><span class="file-name" title="${esc(f.name)}">${esc(f.name.replace(/\.txt$/,''))}</span></label><span class="file-count">${f.row_count.toLocaleString()}</span></div>`;
  }).join('') + failedHtml) || '<div class="empty">无数据文件</div>';
  setText('fileSelectionMeta', `${selectedCount} / ${files.length} 段高亮 · 灰色为未选中` + (failed.length ? ` · ${failed.length} 个解析失败（无数据）` : ''));
}

function pickFilesOnly(files) { return Array.from(files || []).filter(file=>/\.(pcap|pacp|txt)$/i.test(file.name)); }
function formatBytes(bytes) { if(bytes<1024*1024)return `${(bytes/1024).toFixed(1)} KB`; if(bytes<1024*1024*1024)return `${(bytes/1024/1024).toFixed(1)} MB`; return `${(bytes/1024/1024/1024).toFixed(2)} GB`; }
// 文件夹自动监控的签名只认"文件集合"（名字），不认体积与修改时间。
// 以前签名里带 size:lastModified，正在录制的那个 pcap 每写一包就变一次 → 每 3 秒的
// 轮询都判定"文件夹变了" → 而一次整目录重解析（305 个文件）要 80 秒以上，于是上一次
// 刚结束就立刻开始下一次：页面永远停在"正在解析"，主线程被反复占满，图表看起来就是
// 一直不显示，而横幅还留着上一次成功解析的提示。README 里这个功能本来就写作
// "检测新文件"，所以只比对文件集合才是对的语义。
function folderSignature(files) { return (files || []).map(file=>file.name).sort().join('|'); }
// 两次自动重解析之间的最小间隔：上一次自动重解析结束后至少让页面空闲这么久，
// 保证浏览器有机会把图表画出来、用户有机会操作。
const FOLDER_RELOAD_COOLDOWN_MS = 20000;
// 轮询间隔：只负责发现"出现新文件"，成本极低。
const FOLDER_POLL_MS = 3000;
// 每批上传的文件数：服务端一次请求内必须同时持有一批的全部行才能做全局排序，
// 100 个文件一次性提交会到 GB 级内存峰值。分批后服务端峰值只与单批有关，
// 同时让前端能逐批回报进度。用 let 而非 const：自动化测试需要把它压到 1，
// 才能在同一批里精确制造"部分批次失败"的场景。
let PARSE_BATCH_SIZE = 8;
// 同时在飞的批次数：批内串行发送时服务端的解码进程池利用率很低，
// 并发几批正好把多个核填满；上限不要超过服务端的解码进程数太多。
const MAX_PARALLEL_BATCHES = 4;
// 每批解析完就并入视图（renderProgress），但整份数据每并入一次都要重建派生状态
// （fileRows / trackSegments / 统计 / 降采样）并重画地图与轨迹，所以按这个间隔节流，
// 同时保证最后一批一定渲染。1.5 秒是"看起来是实时的"和"别把主线程占满"之间的折中。
const PROGRESS_RENDER_MS = 1500;
function setPcapProgress(done, total, label) {
  const bar = $('pcapProgress'), fill = $('pcapProgressBar');
  if (label !== undefined) setText('pcapPickerStatus', label);
  if (!bar || !fill) return;
  if (!total || done >= total) { bar.hidden = true; fill.style.width = '0%'; return; }
  bar.hidden = false;
  fill.style.width = `${Math.round((done / total) * 100)}%`;
}
function gpsSortKey(row) { return (Number(row.gps_week) || 0) * 604800 + (Number(row.gps_ms) || 0) / 1000; }
// 两段都已按 GPS 时间排好序的行 → 归并成一段（O(n+m)）。
// 分批渲染时每来一批就要并入一次，而时间窗口、曲线 x 轴、hover 的二分查找都要求
// 全局有序；整体重排 180 万行要几秒，所以只做归并。
function mergeSortedRows(left, right) {
  if (!left.length) return right;
  if (!right.length) return left;
  const out = new Array(left.length + right.length);
  let i = 0, j = 0, k = 0;
  while (i < left.length && j < right.length) {
    out[k++] = gpsSortKey(right[j]) < gpsSortKey(left[i]) ? right[j++] : left[i++];
  }
  while (i < left.length) out[k++] = left[i++];
  while (j < right.length) out[k++] = right[j++];
  return out;
}
function renderPcapPicker() {
  const files=state.pendingPcapFiles, busy=state.uploadBusy;
  $('pickPcapFiles').disabled=busy; $('pickPcapFolder').disabled=busy;
  if(busy){ if(files.length)setText('pcapPickerStatus',`正在解析 ${files.length} 个文件…`); return; }
  setPcapProgress(0, 0);
  if(!files.length){ setText('pcapPickerStatus','选择 .pcap / .txt 文件或文件夹后会自动开始解析并加载。'); return; }
  const source=state.pcapSourceFolder?'文件夹':'文件选择';
  setText('pcapPickerStatus',`${source}：${files.length} 个文件 · ${formatBytes(files.reduce((sum,file)=>sum+file.size,0))}，正在自动解析…`);
}
async function filesFromDirectory(handle) {
  const files=[];
  async function walk(directory, prefix='') {
    for await (const entry of directory.values()) {
      if(entry.kind==='file'&&/\.(pcap|pacp|txt)$/i.test(entry.name))files.push(await entry.getFile());
      else if(entry.kind==='directory')await walk(entry,`${prefix}${entry.name}/`);
    }
  }
  await walk(handle); return files;
}
function setPendingPcapFiles(files, directoryHandle=null) {
  state.pendingPcapFiles=pickFilesOnly(files); state.pcapDirectoryHandle=directoryHandle; state.pcapFolderSignature=directoryHandle?folderSignature(state.pendingPcapFiles):''; schedulePcapFolderPolling(); renderPcapPicker();
}
async function choosePcapFolder() {
  if(window.showDirectoryPicker){
    try { const handle=await window.showDirectoryPicker({mode:'read'}); const files=await filesFromDirectory(handle); state.pcapSourceFolder=true; setPendingPcapFiles(files,handle); if(!state.pendingPcapFiles.length){showError('所选文件夹中没有 .pcap/.txt 文件。');return;} await loadSelectedPcap(false); return; }
    catch(error) { if(error.name==='AbortError')return; showError(`读取文件夹失败：${error.message}`); return; }
  }
  $('pcapFolderInput').click();
}
async function loadSelectedPcap(automatic=false) {
  if(state.uploadBusy) return;
  const files=pickFilesOnly(state.pendingPcapFiles); if(!files.length){ if(!automatic)showError('请先选择一个或多个 .pcap/.txt 文件。'); return; }
  state.uploadBusy=true; renderPcapPicker(); if(!automatic)clearError();
  const batches=[]; for(let i=0;i<files.length;i+=PARSE_BATCH_SIZE)batches.push(files.slice(i,i+PARSE_BATCH_SIZE));
  let done=0;
  let finalStatus=`正在解析 ${files.length} 个文件…`;
  const startedAt=Date.now();
  try {
    // 批次之间并发：后端 /api/parse 用进程池解码，串行发批会让线程池和进程池
    // 大部分时间空转（原来是 await 一批再发下一批）。并发上限取 4 是因为
    // 每个请求自身要在一个进程里持有整批的 multipart body + 解析结果，
    // 再多会开始吃内存而不是省时间。
    const concurrency=Math.min(MAX_PARALLEL_BATCHES,batches.length);
    // 并发返回的批次顺序不确定，但文件列表顺序、fileSelect 顺序和每条轨迹的取色
    // 都从数组下标推导（colors[index%colors.length]），所以**不能**按完成顺序
    // append。这里按批次下标落位。
    const results=new Array(batches.length);
    const batchFiles=new Array(batches.length);
    const failedBatches=[];
    let next=0, firstError=null;
    // 任何一批回来就立刻并入视图（不要求按上传顺序到齐）：第一批数据 1~2 秒就能上屏，
    // 不用等 305 个文件全部解析完（那要 80 秒以上）。顺序问题用两个办法解决：
    //   1) 文件列表每次按"批次下标"重建 → 与上传顺序一致（还没到的批次先不出现在列表里）；
    //   2) 行用双指针归并（每批内部服务端已经排好序）→ 时间窗口/二分查找依赖的有序性始终成立。
    // 代价是加载过程中后到的批次可能把"文件颜色下标"整体挪动一次，加载完就稳定了。
    const absorbed=new Set();
    const merged={columns:null,files:[],errors:{},rows:[]};
    let shownFiles=0, shownRows=0;
    let lastProgressRender=0, progressTimer=null, loadFinished=false;
    function progressLabel(){
      return `正在解析 ${done}/${files.length} 个文件…`
        + (shownFiles?`（已显示 ${shownFiles} 个文件 · ${shownRows.toLocaleString()} 行）`:'')
        + (concurrency>1?` · ${concurrency} 批并行`:'');
    }
    function rebuildFiles(){
      merged.files=[];
      for(let index=0;index<batches.length;index++){
        if(!absorbed.has(index))continue;
        const part=results[index]; if(!part)continue;
        for(const file of part.files)merged.files.push(file);
      }
    }
    function absorb(index){
      const part=results[index]; if(!part||absorbed.has(index))return;
      absorbed.add(index);
      if(!merged.columns||!merged.columns.length)merged.columns=part.columns;
      rebuildFiles();
      Object.assign(merged.errors,part.errors);
      merged.rows=mergeSortedRows(merged.rows,part.rows);
    }
    // 原始 File 按文件名建索引：下载按钮用 state.blobs[state.file] 取原文件，
    // 而服务端对同名文件会加 " (2)" 后缀，所以两种名字都要能查到。
    // 同名文件必须错开认领：第一个占原名，第二个占 " (2)"，否则两份都指向同一份原始文件。
    // 按批次下标升序重建，结果与"全部到齐后一次算"完全一致。
    function blobsForAbsorbed(){
      const blobs={}, takenNames=new Set();
      for(let index=0;index<batches.length;index++){
        if(!absorbed.has(index))continue;
        const batch=batchFiles[index], part=results[index];
        if(!batch||!part)continue;
        // 本批里"没被直接认领的服务端文件名"候选池，按返回顺序与同 stem 的 File 配对。
        const unused=[];
        part.files.forEach(meta=>{
          const file=batch.find(item=>item.name===meta.name);
          if(file&&!takenNames.has(meta.name)){ blobs[meta.name]=file; takenNames.add(meta.name); return; }
          unused.push(meta.name);
        });
        batch.forEach(file=>{
          if(!unused.length)return;
          // 服务端把重名文件改名成 "stem (2).pcap"，按顺序认领一个同 stem 的名字。
          const stem=file.name.replace(/\.[^.]+$/,'');
          const position=unused.findIndex(name=>name.replace(/\.[^.]+$/,'')===stem||name.startsWith(`${stem} (`));
          if(position>=0){ const claimed=unused.splice(position,1)[0]; blobs[claimed]=file; takenNames.add(claimed); }
        });
      }
      return blobs;
    }
    // 中途渲染：每并入一批就重画一次。整份数据每并入一次都要重建派生状态
    // （fileRows / trackSegments / 统计 / 降采样）并重画地图与轨迹，所以按
    // PROGRESS_RENDER_MS 节流；收尾那一次一定会渲染（见下面的 loadFinished）。
    function renderProgress(){
      if(loadFinished)return;
      // 这一批之前一批数据都没有（全失败）：保留原视图，等最终渲染给结论。
      if(!merged.rows.length && (state.allRows||[]).length)return;
      applyParsed({columns:merged.columns,files:merged.files,errors:merged.errors,rows:merged.rows},files,blobsForAbsorbed(),{banners:false});
      shownFiles=merged.files.length; shownRows=merged.rows.length;
      lastProgressRender=Date.now();
      setPcapProgress(done,files.length,progressLabel());
    }
    function scheduleProgress(){
      if(loadFinished)return;
      const wait=Math.max(0,PROGRESS_RENDER_MS-(Date.now()-lastProgressRender));
      if(wait===0){ if(progressTimer){clearTimeout(progressTimer);progressTimer=null;} renderProgress(); return; }
      if(progressTimer)return;   // 已经排了一次收尾渲染，不再叠加
      progressTimer=setTimeout(()=>{ progressTimer=null; renderProgress(); },wait);
    }
    async function runBatch(){
      while(true){
        const index=next++; const batch=batches[index]; if(!batch)return;
        batchFiles[index]=batch;
        try{
          const form=new FormData(); batch.forEach(file=>form.append('files',file,file.name));
          const response=await fetch('/api/parse',{method:'POST',body:form});
          const part=await response.json(); if(!response.ok||part.error)throw new Error(part.error||response.statusText);
          results[index]={columns:part.columns||[],files:part.files||[],errors:part.errors||{},rows:part.rows||[]};
        }catch(error){
          // 带上批次里的文件名：批次级失败（连接断开、服务端重启）时前端只拿到
          // 一句网络错误，不知道是哪几个文件一起提交的那一批，没法排查。
          // 保留原始错误类型：下游要靠 TypeError 判断"这是请求失败"而不是前端 bug。
          const names=batch.map(file=>file.name).join('、');
          if(!firstError){
            const message=`${error.message}（第 ${index+1} 批：${names}）`;
            firstError=error instanceof TypeError?new TypeError(message):new Error(message);
          }
          failedBatches.push(`第 ${index+1} 批（${names}）：${error.message}`);
        }
        done+=batch.length;
        // 先按"上传进度"更新进度条与文案，再决定要不要立刻并入视图。
        setPcapProgress(done,files.length,progressLabel());
        absorb(index);
        scheduleProgress();
      }
    }
    await Promise.all(Array.from({length:concurrency},()=>runBatch()));
    // 收尾：停掉挂起的节流渲染，把全部批次（含失败）并进视图后给结论。
    loadFinished=true;
    if(progressTimer){clearTimeout(progressTimer);progressTimer=null;}
    for(let index=0;index<batches.length;index++)absorb(index);
    // 有批次失败时不再直接放弃：其它批次已经拿到的数据先显示出来，
    // 失败的批次单独提示。只有全部批次都失败才当作整体失败、保留原有视图。
    const hasAnyResult=results.some(part=>part&&part.rows.length);
    if(firstError&&!hasAnyResult)throw firstError;
    const blobs=blobsForAbsorbed();
    const seconds=((Date.now()-startedAt)/1000).toFixed(1);
    const errDetails=Object.values(merged.errors||{});
    const errCount=errDetails.length;
    // 选中的文件一个都没解析出数据时（全都失败，或者只是正在录制、还没写头的空壳
    // 文件）不要清空已有视图：以前这里会无条件替换，结果是一个坏 pcap 就让整页图表
    // 全变空白，只剩一条错误横幅。保留旧数据并说明原因，想重置可以点"清空并刷新"。
    if(!merged.rows.length && (state.allRows||[]).length){
      const kept=`${(state.files.files||[]).length} 个文件 / ${state.allRows.length.toLocaleString()} 行`;
      finalStatus=`本次选择没有解析出可用数据${errCount?`（${errCount} 个文件解析失败）`:''} · 已保留原有视图（${kept}）；如需清空请点"清空并刷新"`;
      if(automatic) setText('pcapPickerStatus', `${finalStatus} · 明细：${errDetails.length?errorDetail(errDetails[0]):'没有可解析的记录'}`);
      else showErrorList(`本次选择没有解析出可用数据，已保留原有视图（${kept}）`, errDetails.length?errDetails:['所选文件里没有可解析的记录']);
      return;
    }
    applyParsed(merged, files, blobs);
    finalStatus=(state.pcapSourceFolder
      ? (state.pcapDirectoryHandle ? `已加载 ${merged.files.length} 个文件 · 耗时 ${seconds}s · 文件夹自动监控已开启：每 ${FOLDER_POLL_MS/1000} 秒检测新增文件（文件变大不算），两次自动重解析至少间隔 ${FOLDER_RELOAD_COOLDOWN_MS/1000} 秒` : `已加载 ${merged.files.length} 个文件 · 耗时 ${seconds}s · 此浏览器不支持文件夹自动监控，新增文件后请重新选择文件夹`)
      : `已加载 ${merged.files.length} 个文件 · 耗时 ${seconds}s · 选择其它文件/文件夹可更换数据`)
      + (errCount?` · ${errCount} 个文件解析失败`:'')
      + (failedBatches.length?` · ${failedBatches.length} 个批次请求失败（已显示其余数据）`:'');
    if(failedBatches.length){
      if(!automatic)showErrorList(`${failedBatches.length} 个批次请求失败（其余数据已显示）`, failedBatches);
      else setText('pcapPickerStatus',`${finalStatus} · 明细：${errorDetail(failedBatches[0])}`);
    }
  } catch(error) {
    // 区分"请求失败"和"我们的代码抛了异常"：批次级失败的 Error 是从 fetch 抛出的
    // TypeError，而代码 bug 会带着 stack。以前两者都报成"解析失败：xxx"，
    // 一个前端 bug（比如变量名写错）会被伪装成网络问题，很难查。
    // TypeError 是 fetch 失败的形状；代码 bug 通常是 ReferenceError/TypeError 但带
    // 自己的栈，所以只认"网络味道"的消息，避免把前端 bug 伪装成网络问题。
    // 用 ^ 而不是整串匹配：firstError 的消息会被追加批次与文件名（见 runBatch）。
    const isRequestFailure=error instanceof TypeError&&/^\s*(failed to fetch|networkerror|load failed|network request failed)/i.test(error.message);
    finalStatus=(isRequestFailure?'解析请求失败：':'前端处理出错（请反馈这条信息）：')+error.message;
    if(!isRequestFailure)console.error('AP18 前端处理异常',error);
    if(!automatic)showError(errorDetail(finalStatus), error && error.stack ? error.stack : finalStatus);
  }
  finally {
    state.uploadBusy=false; setPcapProgress(0,0); renderPcapPicker(); setText('pcapPickerStatus',finalStatus);
    // 自动重解析结束后记下时间：文件夹轮询要等冷却期过了才会再触发下一次。
    if(automatic)state.folderReloadFinishedAt=Date.now();
  }
}
// 文件夹监控：只在"出现新文件"时自动重解析（整目录重传重解，见 README 的取舍说明），
// 并且距上一次自动重解析结束不足 FOLDER_RELOAD_COOLDOWN_MS 时不再触发——否则大文件夹
// 会变成"上一轮刚完、下一轮立刻开始"的死循环，页面永远画不出来。
async function refreshSelectedPcapFolder() {
  if(!state.pcapDirectoryHandle||state.uploadBusy)return;
  if(Date.now()-(state.folderReloadFinishedAt||0)<FOLDER_RELOAD_COOLDOWN_MS)return;
  try {
    const files=await filesFromDirectory(state.pcapDirectoryHandle), signature=folderSignature(files);
    if(signature!==state.pcapFolderSignature){
      state.pendingPcapFiles=files; state.pcapFolderSignature=signature; state.pcapSourceFolder=true; renderPcapPicker();
      if(files.length)await loadSelectedPcap(true);
    }
  }
  catch(error) { setText('pcapPickerStatus',`文件夹自动更新失败：${error.message}`); }
}
function schedulePcapFolderPolling() { if(state.pcapFolderTimer)clearInterval(state.pcapFolderTimer); state.pcapFolderTimer=state.pcapDirectoryHandle?setInterval(refreshSelectedPcapFolder,FOLDER_POLL_MS):null; }
function downsampleRows(rows, maxPoints) {
  if(rows.length<=maxPoints) return rows;
  // 只有"在这份数据里**真的稀疏**"的字段才整行保留。SPARSE_KEYS 是按设备输出配置写死的，
  // 但某些数据集里 GGA 一匹配上，satellites_used / differential_age 其实每行都有值；
  // 把它们也算成"必须保留"会让 required 覆盖全部行，接着的均匀抽样就会把真正稀疏的
  // RMS 行一并抽掉（位置 RMS 曲线只剩零星几个点，中间的空缺还会被连成一条假直线）。
  // 判据：有值的行数不超过一半才算稀疏；稀疏字段合起来超预算时再按比例抽稀。
  const required=new Set();
  for(const key of SPARSE_KEYS){
    const indices=[];
    for(let i=0;i<rows.length;i++){ const v=rows[i][key]; if(v!=null && finite(Number(v))) indices.push(i); }
    if(!indices.length || indices.length*2>rows.length) continue;
    for(const i of indices) required.add(i);
  }
  if(required.size>=maxPoints){ const list=Array.from(required).sort((a,b)=>a-b); const idx=[]; for(let i=0;i<maxPoints;i++) idx.push(list[Math.round(i*(list.length-1)/(maxPoints-1))]); return Array.from(new Set(idx)).sort((a,b)=>a-b).map(i=>rows[i]); }
  const budget=maxPoints-required.size;
  const candidates=[]; for(let i=0;i<rows.length;i++) if(!required.has(i)) candidates.push(i);
  let indices;
  if(budget>=candidates.length) indices=new Set([...required,...candidates]);
  else if(budget===1) indices=new Set([...required,candidates[0]]);
  else { indices=new Set(required); for(let i=0;i<budget;i++) indices.add(candidates[Math.round(i*(candidates.length-1)/(budget-1))]); }
  return Array.from(indices).sort((a,b)=>a-b).map(i=>rows[i]);
}
// 轨迹点按屏幕分辨率抽样：一边遍历一边决定去留，不额外缓存 180 万个中间数组
// （以前的实现是先把全量点建出来再交给地图，等于把内存峰值翻倍）。
// 返回 { points, total }，total 是原始有效点数，界面用它说明"这是抽样后的点数"。
function trackPointsFromRows(rows, maxPoints) {
  const list = rows || [];
  let total = 0;
  for (const row of list) if (finite(row.latitude) && finite(row.longitude)) total++;
  const stride = (maxPoints && total > maxPoints) ? total / maxPoints : 0;
  const points = [];
  let seen = 0;
  for (const row of list) {
    const lat = row.latitude, lon = row.longitude;
    if (!finite(lat) || !finite(lon)) continue;
    seen++;
    // 只在跨越 stride 整数倍时保留；最后一个点强制保留（地图与轨迹要标终点）。
    if (!stride || seen === total || Math.floor((seen - 1) / stride) !== Math.floor((seen - 2) / stride)) points.push([lat, lon, xValue(row)]);
  }
  return { points, total };
}
function rangeSummaryOf(rows, key) {
  let min=null,max=null;
  rows.forEach(row=>{ const v=row[key]; if(!finite(v))return; if(min==null||v<min)min=v; if(max==null||v>max)max=v; });
  return {min,max};
}
function statusCountsOf(rows, key) {
  const counts={};
  rows.forEach(row=>{ const v=row[key]; if(v!=null){ const text=String(v); counts[text]=(counts[text]||0)+1; } });
  return counts;
}
// 判断"数据加载成功但一条曲线都画不出来"。以前这种情况界面只是全空白，用户只看到
// "解析成功"的提示，根本不知道是数据里根本没有可绘制的字段（例如文件里不是 GSOF49
// 导航记录、或者设备的输出配置变了）。这里只抽样几千行，成本与总行数无关。
const PLOT_FIELD_GROUPS = [
  { label: '位置/高程', keys: ['latitude', 'longitude', 'altitude'], primary: true },
  { label: '速度/姿态', keys: ['north_velocity', 'east_velocity', 'down_velocity', 'total_speed', 'roll', 'pitch', 'heading', 'track_angle'], primary: true },
  { label: 'RMS', keys: ['north_position_rms', 'east_position_rms', 'down_position_rms'], primary: true },
  { label: '角速度/加速度', keys: ['angular_rate_x', 'angular_rate_y', 'angular_rate_z', 'acceleration_x', 'acceleration_y', 'acceleration_z'], primary: false },
  { label: 'GPS 时间轴（周 / 周内毫秒）', keys: ['gps_week', 'gps_ms'], primary: false }
];
function emptyViewHint(rows) {
  const total = (rows || []).length;
  if (!total) return null;
  const sample = total > 5000 ? rows.slice(0, 2500).concat(rows.slice(-2500)) : rows;
  const hasData = group => sample.some(row => group.keys.some(key => finite(row[key])));
  if (PLOT_FIELD_GROUPS.some(group => group.primary && hasData(group))) return null;  // 有关键曲线可画：正常
  const empty = PLOT_FIELD_GROUPS.filter(group => !hasData(group)).map(group => group.label);
  const timeMissing = !hasData(PLOT_FIELD_GROUPS[PLOT_FIELD_GROUPS.length - 1]);
  return `已解析 ${total.toLocaleString()} 行，但「${empty.join('、')}」这些字段全为空，所有曲线都画不出来：`
    + '文件里的记录与解析器预期的不一致（设备输出配置 / 记录布局变了），或者字段值本身是 NaN'
    + '（服务端会把 NaN/Infinity 归一成空值，界面上就只剩空白）'
    + (timeMissing ? '；连 GPS 时间轴都是空的' : '')
    + '。原始记录表仍可查看；也可以在命令行跑 python tests/diagnose_pcap.py <该文件>，看每个字段真实有多少行有值。';
}
// 比例只保留能看清量级的位数：1/6003 这种千分之几的比例用两位小数会显示成 0.00%，
// 看上去像"一次都没超限"，所以小于 0.1% 时多给一位。
function fmtRatio(value) {
  if (value == null || !finite(value)) return '—';
  const percent = value * 100;
  return `${(percent >= 10 ? percent.toFixed(1) : percent >= 0.1 ? percent.toFixed(2) : percent.toFixed(3))}%`;
}
// 百分比高亮：0（没有任何超限）用灰色，有超限用强调色，比例大的再用橙/红标出来 ——
// 这样扫一眼就能看出哪个分量、哪个阈值有问题，而不用逐个读数字。
// 分档：<1% 蓝（个别帧超限）· ≥1% 橙（明显超限）· ≥5% 红（严重）。
function ratioClass(value) {
  if (!finite(value) || value <= 0) return 'pct-zero';
  if (value >= 0.05) return 'pct-high';
  if (value >= 0.01) return 'pct-mid';
  return 'pct-low';
}
// 每个位置 RMS 分量分别统计：有效帧数（有数值的帧）、超限帧数、超限帧数 / 有效帧数。
// **没有 RMS 的帧直接不进分母**：GSOF50 是间歇发送的（约每 20 帧才带一次 RMS），把那些
// 根本读不到 RMS 的帧也算进分母，比例会被稀释成一个很小的数，看着像"几乎没超限"，
// 反而掩盖真实情况。非数值（null、NaN、旧 TXT 里的空列）同样既不算分子也不算有效帧。
function positionRmsExceedOf(rows) {
  const list = rows || [], total = list.length;
  const thresholds = RMS_POSITION_THRESHOLDS;
  const fields = RMS_POSITION_FIELDS.map(field => {
    const over = {}, ratio = {};
    thresholds.forEach(t => { over[t.text] = 0; ratio[t.text] = 0; });
    let valid = 0, moving = 0, noSpeed = 0;
    list.forEach(row => {
      const value = row[field.key];
      if (!finite(value)) return;
      valid++;
      // 车速过滤：分子分母都只统计"运动帧"（车速 > 5 km/h）。停车时 RMS 天然更小
      // （实测北位置 RMS 中位 0.0288 m vs 运动 0.0379 m），混进分母会让动态精度指标变好看。
      const speed = row.total_speed;
      if (!finite(speed)) { noSpeed++; return; }        // 读不到车速：无法证明在运动，不计入分母
      if (!(speed > RMS_MOVING_SPEED_MPS)) return;      // ≤5 km/h 的静止/低速帧：排除
      moving++;
      thresholds.forEach(t => { if (value > t.value) over[t.text]++; });
    });
    thresholds.forEach(t => { ratio[t.text] = moving ? over[t.text] / moving : 0; });
    return { key: field.key, label: field.label, short: field.short, unit: 'm',
             valid, moving, noSpeed, excluded: valid - moving, total, over, ratio };
  });
  return { total, movingSpeedKmh: RMS_MOVING_SPEED_KMH, thresholds, fields };
}
function computeStats(rows) {
  if(!rows.length) return { row_count:0, rms_rows:0, satellite_rows:0, gga_rows:0, differential_rows:0, position_rms_exceed:positionRmsExceedOf([]), satellites_used:{min:null,max:null}, differential_age:{min:null,max:null}, hdop:{min:null,max:null}, satellites_source:{}, satellite_quality:{}, differential_age_text:{}, hdop_text:{}, imu_status:{}, gnss_status:{}, duration_seconds:null, latitude:{min:null,max:null}, longitude:{min:null,max:null}, altitude:{min:null,max:null}, total_speed:{min:null,max:null}, start_time:null, end_time:null };
  const first=rows[0], last=rows[rows.length-1];
  let duration_seconds=null;
  if(first.gps_week!=null&&last.gps_week!=null) duration_seconds=(Number(last.gps_week)-Number(first.gps_week))*604800+(Number(last.gps_ms||0)-Number(first.gps_ms||0))/1000;
  let rms_rows=0, satellite_rows=0, gga_rows=0, differential_rows=0;
  rows.forEach(row=>{
    if(row.north_position_rms!=null)rms_rows++;
    if(finite(row.satellites_used))satellite_rows++;
    if(row.differential_age_text!=null)gga_rows++;
    if(finite(row.differential_age))differential_rows++;
  });
  return {
    row_count: rows.length, rms_rows, satellite_rows, gga_rows, differential_rows,
    position_rms_exceed: positionRmsExceedOf(rows),
    satellites_used: rangeSummaryOf(rows,'satellites_used'),
    differential_age: rangeSummaryOf(rows,'differential_age'),
    hdop: rangeSummaryOf(rows,'hdop'),
    satellites_source: statusCountsOf(rows,'satellites_source'),
    imu_status: statusCountsOf(rows,'imu_status_text'),
    gnss_status: statusCountsOf(rows,'gnss_status_text'),
    satellite_quality: statusCountsOf(rows,'satellite_quality_text'),
    differential_age_text: statusCountsOf(rows,'differential_age_text'),
    hdop_text: statusCountsOf(rows,'hdop_text'),
    duration_seconds, latitude: rangeSummaryOf(rows,'latitude'), longitude: rangeSummaryOf(rows,'longitude'),
    altitude: rangeSummaryOf(rows,'altitude'), total_speed: rangeSummaryOf(rows,'total_speed'),
    start_time: first.gps_time||null, end_time: last.gps_time||null
  };
}
function updateFileMeta() {
  const meta = state.file==='ALL' ? (state.files.all||null) : (state.files.files||[]).find(f=>f.name===state.file) || null;
  if(!meta){ setText('fileMeta','—'); return; }
  setText('fileMeta', `${meta.row_count.toLocaleString()} 行 · RMS ${meta.rms_rows.toLocaleString()} 行`);
  $('downloadButton').textContent = state.file==='ALL' ? '下载当前文件' : (/\.pcap$/i.test(state.file)?'下载当前 PCAP':'下载当前 TXT');
  setText('datasetCaption', state.file==='ALL' ? `全部 ${state.files.files.length} 个文件 · 轨迹按文件独立绘制 · 原始记录表保留全量数据` : state.file);
}
function rebuildFileSelect() {
  const select=$('fileSelect');
  const files=state.files.files||[];
  select.innerHTML=`<option value="ALL">全部文件（${files.length} 个）</option>`+files.map(f=>`<option value="${esc(f.name)}">${esc(f.name)}</option>`).join('');
  select.value=state.file;
}
// options.banners=false 用于"分批进度渲染"：中途渲染只负责把已经拿到的数据画出来，
// 不弹横幅（否则每一批都会刷一次"已保留原有视图/解析失败"，而且中途的失败结论可能
// 在最后一批到达后被推翻）。最终渲染才给结论。
function applyParsed(body, blobs, options) {
  const showBanners = !options || options.banners !== false;
  clearError();
  state.columns=body.columns||[];
  const list=body.files||[];
  const rmsAll=list.reduce((sum,f)=>sum+(f.rms_rows||0),0);
  state.files={ files:list, all:{name:'ALL', row_count:(body.rows||[]).length, rms_rows:rmsAll} };
  // 原始 File 按文件名建索引：下载按钮用 state.blobs[state.file] 取原文件。
  // 这里不用 Array.isArray 判断：不同 realm（例如自动化测试里 vm 加载的 app.js）
  // 的数组构造函数与宿主不是同一个，isArray 会给出意外结果，直接把映射当数组跳过。
  // 改成按值判断——File/Blob 才带 name，脏值（索引键之类）一律忽略。
  const blobMap={};
  if(blobs&&typeof blobs==='object'){
    const candidates=Object.keys(blobs).map(key=>blobs[key]).filter(value=>value&&typeof value.name==='string');
    (list||[]).forEach(meta=>{
      const file=candidates.find(value=>value.name===meta.name);
      if(file)blobMap[meta.name]=file;
    });
  }
  state.blobs=blobMap;
  state.allRows=body.rows||[];
  // 解析失败的文件名与原因：左侧列表会把它们列出来（灰色、带删除线、不可交互），
  // 这样"哪个文件没进来"一眼可见，而不用去翻横幅。
  state.failedFiles=Object.entries(body.errors||{}).map(([name,message])=>{
    const prefix=`${name}: `, text=String(message==null?'':message);
    return { name, message: text.startsWith(prefix)?text.slice(prefix.length):text };
  });
  state.fileRows={};
  state.allRows.forEach(row=>{ const key=row._source_file||''; (state.fileRows[key]=state.fileRows[key]||[]).push(row); });
  // 轨迹点预算按文件数平摊：文件少时几乎不抽样，文件多时也不会让单个文件被抽得太狠。
  const trackBudget=Math.max(MIN_TRACK_POINTS_PER_SEGMENT,Math.floor(MAX_TRACK_POINTS/Math.max(1,list.length)));
  state.trackSegments=list.map(f=>{
    const track=trackPointsFromRows(state.fileRows[f.name]||[],trackBudget);
    return { name:f.name, points:track.points, total_points:track.total };
  });
  rebuildFileSelect();
  setView('ALL');
  if(showBanners){
    const errs=body.errors||{};
    const emptyHint=emptyViewHint(state.allRows||[]);
    if(Object.keys(errs).length){
      showErrorList(`${Object.keys(errs).length} 个文件解析失败（其余文件已正常显示）`, Object.values(errs), emptyHint);
    } else if(emptyHint){
      showError(`⚠ ${emptyHint}`);
    }
  }
  setText('topStatus', `${list.length} 个数据文件 · ${(state.files.all.row_count||0).toLocaleString()} 条导航记录 · 浏览器本地会话，后端无状态解析`);
}
function resetForFreshView() {
  state.file='ALL'; state.files={files:[],all:null}; state.columns=[]; state.allRows=[]; state.fileRows={}; state.rows=[]; state.chartRows=[]; state.trackSegments=[]; state.blobs={}; state.failedFiles=[]; state.stats=computeStats([]);
  state.selectionInitialized=false; state.selectedFiles=new Set(); state.page=1; state.search=''; $('tableSearch').value='';
  state.selection=null; tableTooltipPinned=false; hideTooltip(); document.querySelectorAll('.table-row-active').forEach(el=>el.classList.remove('table-row-active')); $('selectionBar').hidden=true;
  state.pendingPcapFiles=[]; state.pcapDirectoryHandle=null; state.pcapFolderSignature=''; state.pcapSourceFolder=false; state.folderReloadFinishedAt=0; state.mapNeedsFit=true; $('pcapFilesInput').value=''; $('pcapFolderInput').value=''; schedulePcapFolderPolling(); renderPcapPicker();
  rebuildFileSelect();
  renderFileList(); setText('fileMeta','—'); setText('datasetCaption','—'); setText('topStatus','等待选择 .pcap / .txt 文件');
  renderSummary(); renderAllCharts(); loadTable();
}
function setView(name) {
  state.file=name;
  state.selectedFiles=name==='ALL'?new Set(fileNames()):new Set([name]);
  state.mapNeedsFit=true;   // 换视图（下拉框切换、新数据加载）才重新定位地图
  state.rows=name==='ALL'?state.allRows:(state.fileRows[name]||[]);
  state.chartRows=downsampleRows(state.rows,MAX_CHART_POINTS);
  state.stats=computeStats(state.rows);
  state.page=1; state.search=''; $('tableSearch').value='';
  state.selection=null; tableTooltipPinned=false; hideTooltip(); document.querySelectorAll('.table-row-active').forEach(el=>el.classList.remove('table-row-active')); $('selectionBar').hidden=true;
  renderFileList(); updateFileMeta(); renderSummary(); renderAllCharts(); loadTable();
}
function renderSummary() {
  const s=state.stats||computeStats(state.rows||[]);
  setText('statRows', (s.row_count||0).toLocaleString()); setText('statRowsContext', `GSOF49 · RMS ${(s.rms_rows||0).toLocaleString()} 条`);
  const duration=s.duration_seconds; setText('statDuration', duration == null ? '—' : duration >= 60 ? `${(duration/60).toFixed(2)} min` : `${duration.toFixed(2)} s`);
  setText('statTime', `${s.start_time || '—'} → ${s.end_time || '—'}`);
  setText('statLat', `${fmtCompact((s.latitude||{}).min)} ~ ${fmtCompact((s.latitude||{}).max)}`);
  setText('statLon', `${fmtCompact((s.longitude||{}).min)} ~ ${fmtCompact((s.longitude||{}).max)}`);
  setText('statAlt', `${fmtCompact((s.altitude||{}).min)} ~ ${fmtCompact((s.altitude||{}).max)}`);
  setText('statSpeed', fmtCompact((s.total_speed||{}).max));
  const satRange=s.satellites_used||{}, satMin=satRange.min, satMax=satRange.max;
  const satValue=satMin==null?'—':satMin===satMax?String(Math.round(satMin)):`${Math.round(satMin)} ~ ${Math.round(satMax)}`;
  setText('statSatellites', satValue);
  const satSources=Object.keys(s.satellites_source||{}).filter(Boolean);
  setText('statSatellitesContext', (s.satellite_rows||0) ? `${s.satellite_rows.toLocaleString()} 条有效 · 来源 ${satSources.join(' + ')||'—'}` : 'GSOF1 / GGA 均未输出');
  const ageRange=s.differential_age||{}, ageMin=ageRange.min, ageMax=ageRange.max;
  const ageValue=ageMin==null?'—':ageMin===ageMax?`${Number(ageMin).toFixed(1)} s`:`${Number(ageMin).toFixed(1)} ~ ${Number(ageMax).toFixed(1)} s`;
  setText('statDiffAge', ageValue);
  setText('statDiffAgeContext', (s.gga_rows||0) ? ((s.differential_rows||0) ? `${s.differential_rows.toLocaleString()} 条有龄期值 · GGA` : 'GGA 已解析但未输出龄期值') : 'PCAP 中无 GGA 句子');
  renderRmsExceed(s.position_rms_exceed || positionRmsExceedOf(state.rows||[]));
  renderChips('imuChips', s.imu_status||{}); renderChips('gnssChips', s.gnss_status||{}); renderChips('satelliteChips', s.satellite_quality||{}); renderChips('differentialChips', s.differential_age_text||{}); renderChips('hdopChips', s.hdop_text||{});
  const shown=state.chartRows?state.chartRows.length:0, total=state.rows?state.rows.length:0;
  const trackFull=(state.trackSegments||[]).reduce((sum,seg)=>sum+(seg.total_points||0),0);
  const trackShown=(state.trackSegments||[]).reduce((sum,seg)=>sum+(seg.points||[]).length,0);
  const trackNote=trackFull>trackShown?`；地图与轨迹 ${trackShown.toLocaleString()} / ${trackFull.toLocaleString()} 点（按屏幕分辨率抽样）`:'';
  setText('samplingInfo', (shown<total ? `图表显示 ${shown.toLocaleString()} / ${total.toLocaleString()} 点；表格为全量` : `图表显示全部 ${total.toLocaleString()} 点`)+trackNote);
}
function renderChips(id, counts) { $(id).innerHTML = Object.entries(counts || {}).map(([label,count]) => `<span class="chip"><strong>${count.toLocaleString()}</strong> ${esc(label)}</span>`).join('') || '<span class="subtle">无数据</span>'; }
// 位置 RMS 超限比例只画一处：**"位置 RMS"图下方的那张明细表**（概览区里曾经有三张
// 重复的卡片，和表格信息完全一样，已经去掉；百分比高亮直接做在表格的占比列上）。
function renderRmsExceed(exceed) {
  const thresholds = (exceed && exceed.thresholds && exceed.thresholds.length) ? exceed.thresholds : RMS_POSITION_THRESHOLDS;
  const total = (exceed && exceed.total) || 0, fields = (exceed && exceed.fields) || [];
  const speedKmh = (exceed && exceed.movingSpeedKmh) || RMS_MOVING_SPEED_KMH;
  // 图表下方的明细表：每个分量一行、每个阈值两列（帧数 + 占比），数字右对齐不会截断。
  // 分母是"运动帧"（车速 > 5 km/h），另外把有 RMS 的帧数与已排除的低速帧也列出来，方便核对口径。
  const box = $('rmsExceedTable');
  if (box) {
    const head = ['分量'].concat(thresholds.map(t => `&gt; ${esc(t.text)} m 帧数`)).concat(thresholds.map(t => `&gt; ${esc(t.text)} m 占比`))
      .concat([`运动帧（分母 &gt; ${speedKmh} km/h）`, '有 RMS 的帧', `已排除 ≤ ${speedKmh} km/h`, '总帧数'])
      .map(label => `<th>${label}</th>`).join('');
    const body = fields.map(field => {
      const over = field.over || {}, ratio = field.ratio || {}, valid = field.valid || 0;
      const moving = field.moving || 0, excluded = field.excluded || 0;
      const cells = thresholds.map(t => `<td>${moving ? (over[t.text] || 0).toLocaleString() : '—'}</td>`)
        .concat(thresholds.map(t => `<td class="pct ${moving ? ratioClass(ratio[t.text]) : 'pct-zero'}">${moving ? fmtRatio(ratio[t.text]) : '—'}</td>`))
        .concat([`<td>${moving.toLocaleString()}</td>`, `<td>${valid.toLocaleString()}</td>`,
                 `<td>${excluded.toLocaleString()}</td>`, `<td>${total.toLocaleString()}</td>`]).join('');
      return `<tr><th>${esc(field.label)}</th>${cells}</tr>`;
    }).join('');
    box.innerHTML = fields.length
      ? `<table class="mini-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`
      : '<div class="subtle">无数据</div>';
  }
  const first = fields[0] || {};
  const noSpeedNote = first.noSpeed ? `（另有 ${first.noSpeed.toLocaleString()} 帧读不到车速，同样不计入分母）` : '';
  setText('rmsExceedNote', total
    ? `**只统计运动帧**：分母 = 有位置 RMS 且车速 > ${speedKmh} km/h（${(speedKmh / 3.6).toFixed(2)} m/s）的帧；`
      + `分子 = 该分量超过阈值的运动帧数（严格大于）。≤ ${speedKmh} km/h 的静止/低速帧已排除，未附带 GSOF50 的帧也不计入。`
      + `本视图 RMS 有效帧 ${(first.valid || 0).toLocaleString()} 帧 = 运动 ${(first.moving || 0).toLocaleString()} + 排除 ${(first.excluded || 0).toLocaleString()}${noSpeedNote}；`
      + `总帧数 ${total.toLocaleString()} 帧。占比配色：灰 = 0（无超限）· 蓝 = <1% · 橙 = ≥1% · 红 = ≥5%。`
    : '本视图没有任何导航记录；请先导入 PCAP/TXT 数据。');
}

function xValue(row) { return Number(row.gps_week || 0) * 604800 + Number(row.gps_ms || 0) / 1000; }
function xLabel(seconds, base) { const d=seconds-base; return d >= 60 ? `${(d/60).toFixed(1)}m` : `${d.toFixed(1)}s`; }
// 每块画布的渲染元数据：尺寸缓存、离屏缓存层、当前图表上下文。
// 旧实现每次重绘都 getBoundingClientRect() + 重设 canvas.width：前者触发同步布局，
// 后者会重新分配整块后备存储（dpr=2 时约 4.5 MB），每帧一次是卡顿主因之一。
const canvasMeta = new WeakMap();
let pendingTooltip = new Map();
const sizeObserver = (typeof ResizeObserver !== 'undefined') ? new ResizeObserver(entries => {
  for (const entry of entries) {
    const meta = canvasMeta.get(entry.target); if (!meta) continue;
    const w = entry.contentRect.width, h = entry.contentRect.height;
    if (meta.cssW === w && meta.cssH === h) continue;
    const known = meta.cssW > 0 && meta.cssH > 0;
    meta.cssW = w; meta.cssH = h;
    // 首次观测只记录尺寸；之后的真实变化才丢弃缓存层并安排补画。
    if (known) { meta.chart = null; meta.base = null; const id = entry.target.dataset.chartId; if (id) scheduleFullChart(id); }
  }
}) : null;
function canvasMetaFor(canvas) {
  let meta = canvasMeta.get(canvas);
  if (!meta) {
    meta = { ctx: canvas.getContext('2d'), cssW: 0, cssH: 0, chart: null, base: null };
    canvasMeta.set(canvas, meta);
    if (sizeObserver) sizeObserver.observe(canvas);
  }
  return meta;
}
function setupCanvas(canvas) {
  const meta = canvasMetaFor(canvas), dpr = window.devicePixelRatio || 1;
  if (!meta.cssW || !meta.cssH) { const rect = canvas.getBoundingClientRect(); meta.cssW = rect.width; meta.cssH = rect.height; }
  const w = Math.max(260, meta.cssW), h = Math.max(170, meta.cssH);
  const bw = Math.round(w * dpr), bh = Math.round(h * dpr);
  if (canvas.width !== bw || canvas.height !== bh) { canvas.width = bw; canvas.height = bh; meta.chart = null; meta.base = null; }
  meta.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx: meta.ctx, w, h, meta };
}
let cachedCanvasStyles = null;
function canvasStyles() {
  if (cachedCanvasStyles) return cachedCanvasStyles;
  const style = getComputedStyle(document.documentElement);
  cachedCanvasStyles = { text: style.getPropertyValue('--text'), muted: style.getPropertyValue('--muted'), line: style.getPropertyValue('--line'), surface: style.getPropertyValue('--surface') };
  return cachedCanvasStyles;
}
function drawAxes(ctx, box, xMin, xMax, yMin, yMax, yLabel, base) {
  const c=canvasStyles(); ctx.clearRect(0,0,box.w,box.h); ctx.fillStyle=c.surface; ctx.fillRect(0,0,box.w,box.h); ctx.strokeStyle=c.line; ctx.lineWidth=1; ctx.font='11px Segoe UI, Microsoft YaHei, sans-serif';
  ctx.fillStyle=c.muted; ctx.textAlign='right'; ctx.textBaseline='middle';
  for(let i=0;i<=4;i++){ const y=box.bottom-(i/4)*(box.bottom-box.top); ctx.beginPath(); ctx.moveTo(box.left,y+.5); ctx.lineTo(box.w-box.right,y+.5); ctx.stroke(); const value=yMin+(i/4)*(yMax-yMin); ctx.fillText(fmtAxis(value),box.left-7,y); }
  ctx.textAlign='center'; ctx.textBaseline='top'; for(let i=0;i<=4;i++){ const x=box.left+(i/4)*(box.w-box.left-box.right); ctx.fillText(xLabel(xMin+(i/4)*(xMax-xMin),base),x,box.bottom+8); }
  ctx.strokeStyle=c.line; ctx.strokeRect(box.left+.5,box.top+.5,box.w-box.left-box.right,box.bottom-box.top);
  ctx.save(); ctx.translate(12,(box.top+box.bottom)/2); ctx.rotate(-Math.PI/2); ctx.textAlign='center'; ctx.fillStyle=c.muted; ctx.fillText(yLabel,0,0); ctx.restore();
}
function fmtAxis(value) { const n=Math.abs(value); return n>=100 ? value.toFixed(0) : n>=10 ? value.toFixed(1) : n>=1 ? value.toFixed(2) : value.toFixed(4); }
function zoomButtonHtml(){ return '<button type="button" class="zoom-btn" title="点击放大查看">⤢ 放大</button>'; }
// 图例与曲线必须用同一个下标取色：按曲线在 def.series 中的固定位置取。
// 不能用"可见子集"的下标——隐藏任意一条曲线后可见下标会整体前移，颜色就和图例错位。
function seriesColor(def, series) { const i = def.series.indexOf(series); return colors[(i < 0 ? 0 : i) % colors.length]; }
function makeLegend(id, def) { const box=$(id); box.innerHTML=def.series.map((s,i)=>`<button type="button" class="${def.visible.has(s.key)?'':'off'}" data-chart="${id}" data-key="${s.key}"><span class="swatch" style="color:${colors[i%colors.length]}"></span>${esc(s.label)}</button>`).join('')+zoomButtonHtml(); box.querySelectorAll('button[data-key]').forEach(btn=>btn.addEventListener('click',()=>{ const key=btn.dataset.key; def.visible.has(key)?def.visible.delete(key):def.visible.add(key); makeLegend(id,def); drawTimeChart(Object.keys(chartDefs).find(k=>chartDefs[k]===def)); })); }
function renderLegends() { Object.entries(chartDefs).forEach(([id,def])=>makeLegend(def.legend,def)); const lm=$('legend-map'),lt=$('legend-trajectory'); lm.innerHTML='<span class="subtle">彩色=选中 · 灰色=未选中</span>'; lt.innerHTML='<span class="subtle">彩色=选中 · 灰色=未选中</span>'; lm.insertAdjacentHTML('beforeend',zoomButtonHtml()); lt.insertAdjacentHTML('beforeend',zoomButtonHtml()); }
function streetTile(){ return L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}',{maxZoom:19,attribution:'Tiles &copy; Esri &mdash; Source: Esri, DeLorme, NAVTEQ, USGS, Intermap, iPC, NRCAN, Esri Japan, METI, Esri China (Hong Kong), Esri (Thailand), TomTom'}); }
function satelliteTile(){ return L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{maxZoom:19,attribution:'Tiles &copy; Esri &mdash; Esri, Maxar, Earthstar Geographics, and the GIS User Community'}); }
function initMap(){
  const el=$('leafletMap');
  if(!window.L){ el.innerHTML='<div class="empty">地图组件加载失败，仍可使用下方局部轨迹图。</div>'; setText('mapStatus','Leaflet 未加载；请检查网络或 CDN 访问。'); return; }
  leafletMap=L.map(el,{preferCanvas:true,zoomControl:true,attributionControl:true});
  const baseLayers={ street:streetTile(), sat:satelliteTile() };
  baseLayers.street.addTo(leafletMap);
  leafletMap.setView([50.14,8.74],12);
  const buttons=$('mapLayerSwitch').querySelectorAll('button');
  buttons.forEach(btn=>btn.addEventListener('click',()=>{
    const layer=btn.dataset.layer;
    if(!baseLayers[layer] || leafletMap.hasLayer(baseLayers[layer])) return;
    buttons.forEach(b=>b.classList.toggle('active',b===btn));
    Object.values(baseLayers).forEach(base=>{ if(leafletMap.hasLayer(base)) leafletMap.removeLayer(base); });
    baseLayers[layer].addTo(leafletMap);
  }));
}
function renderMap(){
  if(!leafletMap)return;
  mapTrackLayers.forEach(layer=>layer.remove()); mapMarkerLayers.forEach(layer=>layer.remove()); mapTrackLayers=[]; mapMarkerLayers=[];
  updateMapSelection();
  const bounds=[]; let pointCount=0, visibleSegments=0;
  state.trackSegments.forEach((segment,index)=>{
    const points=(segment.points || []).filter(p=>Array.isArray(p)&&p.length>=2&&Number.isFinite(Number(p[0]))&&Number.isFinite(Number(p[1]))).map(p=>[Number(p[0]),Number(p[1])]);
    if(!points.length)return;
    visibleSegments++; pointCount+=points.length; points.forEach(point=>bounds.push(point));
    const selected=state.selectedFiles.has(segment.name), color=selected?colors[index%colors.length]:'#6f7c92', opacity=selected?.92:.35;
    if(points.length>=2){
      const line=L.polyline(points,{color,weight:selected?3:2,opacity,smoothFactor:1,interactive:true}).addTo(leafletMap);
      const sampled = (segment.total_points || points.length) > points.length;
      line.bindTooltip(`${esc(segment.name.replace(/\.txt$/,''))} · ${points.length.toLocaleString()} 点${sampled?`（全量 ${segment.total_points.toLocaleString()}，已按屏幕分辨率抽样）`:''}`); mapTrackLayers.push(line);
    }
    const start=L.circleMarker(points[0],{radius:selected?5:4,color,weight:2,fillColor:color,fillOpacity:opacity}).bindTooltip(`${segment.name.replace(/\.txt$/,'')} · 起点`).addTo(leafletMap);
    const end=L.circleMarker(points[points.length-1],{radius:selected?5:4,color,weight:2,fillColor:color,fillOpacity:opacity}).bindTooltip(`${segment.name.replace(/\.txt$/,'')} · 终点`).addTo(leafletMap);
    mapMarkerLayers.push(start,end);
  });
  if(!bounds.length){setText('mapStatus','没有可显示的轨迹点');return;}
  // 只在"换了数据集 / 换了视图"之后重新定位一次。取消勾选文件、窗口缩放、放大视图这些
  // 重绘都不该动视角：以前每次 renderMap 都 fitBounds，所以在左侧勾/取消勾选文件时地图
  // 会跳回全览，把用户手动拖拽缩放好的视角冲掉。
  if(state.mapNeedsFit){ leafletMap.fitBounds(L.latLngBounds(bounds),{padding:[28,28],maxZoom:18}); state.mapNeedsFit=false; }
  const selectedCount=state.trackSegments.filter(segment=>state.selectedFiles.has(segment.name)).length;
  setText('mapStatus',`显示 ${visibleSegments} 个独立 PCAP 段 · ${pointCount.toLocaleString()} 个轨迹点 · 高亮 ${selectedCount} 段 · 灰色为未选中`);
  updateMapSelection();
}
// 密集序列按像素列聚合 min/max：包络带的厚度就是该时刻的真实幅度范围。
// 点数从"每行一个点"降到"每像素一段"，且尖峰不会像等间隔抽样那样被抽掉。
// 稀疏序列仍走折线，保持原有观感。
//
// 一条曲线分两遍画：未选中文件的行先画（灰色、半透明），选中文件的行后画（曲线本色）。
// 这样左侧文件列表取消勾选时，所有时间曲线与地图/局部轨迹口径一致：彩色=选中、灰色=未选中。
// 两遍之间必须断笔（skipped 的行当作缺口），否则灰色会在整段选中区间上连出一条假直线。
const UNSELECTED_COLOR = '#6f7c92';
const UNSELECTED_ALPHA = .3;
// 折线在"多大间隔"处断笔：以**这条曲线自己的平均点距**为基准，超过这个倍数才算中断。
// 为什么不用"遇到空值就断"：GSOF50 这类序列本来就是间歇发送的（约每 20 帧才一个点，
// 中间那些帧天生没有 RMS），一遇空值就断会把正常曲线画成虚线；反过来完全不断笔，
// 又会把中间整段缺数据的地方连成一条斜线（用户报的就是这个）。用平均点距当基准，
// 正常采样间隔（1 倍）不断、明显中断（几十倍）必断。
const GAP_BREAK_FACTOR = 6;
// 散点半径（CSS 像素）：RMS 这类"每次采样一个瞬时值"的序列用散点画，点不连线，
// 于是"哪一段没有数据"直接表现为那一段没有点，而不是被一条斜线连过去。
const SCATTER_RADIUS = 1.7;
function drawTimeSeriesPass(ctx, chart, series, color, alpha, wantSelected) {
  const {rows, xs, xMin, xMax, yMin, yMax, box, w} = chart;
  const xSpan = (xMax - xMin) || 1, ySpan = (yMax - yMin) || 1;
  const plotLeft = box.left, plotW = w - box.left - box.right, plotH = box.bottom - box.top;
  const py = v => box.bottom - (v - yMin) / ySpan * plotH;
  const flags = chart.selected;
  const wanted = i => !flags || flags[i] === wantSelected;
  // 散点序列（GSOF50 的位置/速度/姿态 RMS）：每个有效采样画一个点，不做任何连线。
  // 所有点攒进同一条路径一次 fill()，比逐点 arc()+fill() 快一个量级。
  if (chart.def && chart.def.scatter) {
    ctx.fillStyle = color; ctx.globalAlpha = alpha; ctx.beginPath();
    for (let i = 0; i < rows.length; i++) {
      if (!wanted(i)) continue;
      const v = rows[i][series.key]; if (!finite(v)) continue;
      const x = plotLeft + (xs[i] - xMin) / xSpan * plotW, y = py(v);
      ctx.moveTo(x + SCATTER_RADIUS, y);
      ctx.arc(x, y, SCATTER_RADIUS, 0, Math.PI * 2);
    }
    ctx.fill();
    ctx.globalAlpha = 1;
    return;
  }
  // 渲染方式：**有缺口**的序列走折线（本来就是"采样点连线"，缺一段就在那里断开）；
  // 只有每行都有值的密集序列才走包络带（每像素一段 min/max，尖峰不丢、也不会连假线）。
  // 位置 RMS / 差分龄期这类间歇发送的序列点数虽多，但它们仍然是"采样点"，用带子画
  // 既看不出空缺、也没法在中断处断开。
  const drawnCount = chart.counts[series.key] || 0;
  const gappy = drawnCount < rows.length;
  const useEnvelope = !gappy && drawnCount > plotW * 1.5;
  if (!useEnvelope) {
    // 阈值必须和 xs 同单位（秒），不能拿它去比屏幕像素坐标 —— 否则每两个点之间都会被判成"中断"，
    // 曲线会碎成一堆孤立的点。平均点距 = xSpan / 点数。
    const breakGap = drawnCount > 1 ? (xSpan / drawnCount) * GAP_BREAK_FACTOR : Infinity;
    ctx.strokeStyle = color; ctx.lineWidth = 1.45; ctx.globalAlpha = alpha; ctx.beginPath();
    let pen = false, prevTime = 0;
    for (let i = 0; i < rows.length; i++) {
      if (!wanted(i)) { pen = false; continue; }
      const v = rows[i][series.key]; if (!finite(v)) continue;
      const t = xs[i];
      if (pen && t - prevTime > breakGap) pen = false;   // 中间缺了一大段：断开，不要连假线
      const x = plotLeft + (t - xMin) / xSpan * plotW;
      if (pen) ctx.lineTo(x, py(v)); else { ctx.moveTo(x, py(v)); pen = true; }
      prevTime = t;
    }
    ctx.stroke();
    ctx.globalAlpha = 1;
    return;
  }
  const cols = Math.max(1, Math.floor(plotW));
  const lo = new Float64Array(cols).fill(Infinity), hi = new Float64Array(cols).fill(-Infinity);
  const cnt = new Int32Array(cols);
  for (let i = 0; i < rows.length; i++) {
    if (!wanted(i)) continue;
    const v = rows[i][series.key]; if (!finite(v)) continue;
    let c = ((xs[i] - xMin) / xSpan * plotW) | 0;
    if (c < 0) c = 0; else if (c >= cols) c = cols - 1;
    if (v < lo[c]) lo[c] = v;
    if (v > hi[c]) hi[c] = v;
    cnt[c]++;
  }
  // 只画包络带：整条曲线只有一个不透明度，不再叠一层全不透明的均值线，
  // 因此不会出现"中间实、外圈虚"的两层浓度差。
  // 闭合多边形一次 fill()：逐列描边在 2 倍屏上会留下抗锯齿细缝，叠加后形成纵向条纹。
  ctx.fillStyle = color; ctx.globalAlpha = alpha;
  let seg = 0;
  while (seg < cols) {
    if (!cnt[seg]) { seg++; continue; }
    let end = seg; while (end + 1 < cols && cnt[end + 1]) end++;
    ctx.beginPath();
    for (let k = seg; k <= end; k++) { const x = plotLeft + k, y = py(hi[k]); k === seg ? ctx.moveTo(x, y) : ctx.lineTo(x, y); }
    // 下沿反向回来；单点时区间为零高度，强制给 1px 保证可见。
    for (let k = end; k >= seg; k--) { const top = py(hi[k]); ctx.lineTo(plotLeft + k, Math.max(py(lo[k]), top + 1)); }
    ctx.closePath(); ctx.fill();
    seg = end + 1;
  }
  ctx.globalAlpha = 1;
}
function drawTimeSeries(ctx, chart, series, color) {
  // 全部文件都被勾选时（最常见的情况）只画一遍，不做多余的灰色遍历。
  if (chart.anyUnselected) drawTimeSeriesPass(ctx, chart, series, UNSELECTED_COLOR, UNSELECTED_ALPHA, false);
  drawTimeSeriesPass(ctx, chart, series, color, 1, true);
}
function buildTimeChart(id, def, w, h, box) {
  const rows = state.chartRows || [];
  if (!rows.length) return { id, yLabel: def.yLabel, empty: '暂无数据' };
  const xs = rows.map(xValue);
  const xMin = xs[0], xMax = xs[xs.length - 1] === xMin ? xMin + 1 : xs[xs.length - 1];
  const visible = def.series.filter(s => def.visible.has(s.key));
  let yMin = Infinity, yMax = -Infinity;
  // 同时统计每条曲线自己的有效点数：密集判定必须按"本曲线有多少点"而不是"窗口有多少行"，
  // 否则 GSOF50 这类稀疏序列会被误判成密集，绘成断点。
  const counts = {};
  for (const s of visible) counts[s.key] = 0;
  for (let i = 0; i < rows.length; i++) for (const s of visible) { const v = rows[i][s.key]; if (finite(v)) { counts[s.key]++; if (v < yMin) yMin = v; if (v > yMax) yMax = v; } }
  if (!(yMin < Infinity)) return { id, yLabel: def.yLabel, empty: '该字段暂无有效数据' };
  if (yMin === yMax) { const pad = Math.max(1, Math.abs(yMin) * .05); yMin -= pad; yMax += pad; }
  else { const pad = (yMax - yMin) * .08; yMin -= pad; yMax += pad; }
  // 每行属于哪个文件、该文件当前是否被勾选：时间曲线也要与地图/局部轨迹一致地
  // "彩色=选中、灰色=未选中"。没有 _source_file 的行（理论上不应出现）按选中处理，
  // 避免整张图被误判成全部未选中而变成灰色。
  const selected = rows.map(row => { const source = row._source_file; return !source || state.selectedFiles.has(source); });
  const anyUnselected = selected.some(value => !value);
  return { id, def, yLabel: def.yLabel, rows, xs, xMin, xMax, yMin, yMax, box, visible, counts, selected, anyUnselected, w, h };
}
function renderTimeChartOverlay(ctx, chart, w, h, box) {
  const xSpan = (chart.xMax - chart.xMin) || 1, ySpan = (chart.yMax - chart.yMin) || 1;
  const plotW = w - box.left - box.right, plotH = box.bottom - box.top;
  const px = t => box.left + (t - chart.xMin) / xSpan * plotW;
  const py = v => box.bottom - (v - chart.yMin) / ySpan * plotH;
  const rows = chart.rows;
  const hover = state.chartHover.get(chart.id);
  if (hover != null && hover >= 0 && hover < rows.length) {
    const x = px(chart.xs[hover]);
    ctx.strokeStyle = 'rgba(233,239,255,.38)'; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x, box.top); ctx.lineTo(x, box.bottom); ctx.stroke();
    chart.visible.forEach(s => { const nearest = nearestFiniteIndex(rows, hover, s.key); if (nearest < 0) return; ctx.fillStyle = seriesColor(chart.def, s); ctx.beginPath(); ctx.arc(x, py(rows[nearest][s.key]), 3.5, 0, Math.PI * 2); ctx.fill(); });
  }
  const sel = state.selection;
  if (sel && finite(sel.time) && sel.time >= chart.xMin && sel.time <= chart.xMax) {
    const x = px(sel.time);
    ctx.save();
    ctx.strokeStyle = '#ff7e8b'; ctx.lineWidth = 1.7; ctx.setLineDash([6, 4]); ctx.beginPath(); ctx.moveTo(x, box.top); ctx.lineTo(x, box.bottom); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = '#ff7e8b'; ctx.beginPath(); ctx.moveTo(x - 5, box.top + 1); ctx.lineTo(x + 5, box.top + 1); ctx.lineTo(x, box.top + 8); ctx.closePath(); ctx.fill();
    chart.visible.forEach(s => { const v = sel.row ? sel.row[s.key] : null; if (!finite(v) || v < chart.yMin || v > chart.yMax) return; ctx.fillStyle = seriesColor(chart.def, s); ctx.beginPath(); ctx.arc(x, py(v), 4, 0, Math.PI * 2); ctx.fill(); ctx.strokeStyle = '#ffffff'; ctx.lineWidth = 1.4; ctx.stroke(); });
    ctx.restore();
  }
}
// overlay=true 时只重画交互层（悬停十字线、选中标记），复用离屏缓存层，
// 单帧成本从"重画上万段线"降到"一次位图拷贝 + 几条线"。
function drawTimeChart(id, target, overlay) {
  const def = chartDefs[id], canvas = target || $(def.canvas);
  const { ctx, w, h, meta } = setupCanvas(canvas);
  const box = { left: 58, right: 18, top: 23, bottom: h - 30 };
  let chart = meta.chart;
  if (!overlay || !chart || chart.empty || chart.w !== w || chart.h !== h) {
    chart = buildTimeChart(id, def, w, h, box);
    meta.chart = chart;
    canvas._chart = chart.empty ? null : chart;
    if (chart.empty) {
      meta.base = null;
      ctx.clearRect(0, 0, w, h); ctx.fillStyle = canvasStyles().surface; ctx.fillRect(0, 0, w, h);
      ctx.fillStyle = canvasStyles().muted; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(chart.empty, w / 2, h / 2);
      return;
    }
    const dpr = window.devicePixelRatio || 1;
    const base = meta.base || (meta.base = document.createElement('canvas'));
    base.width = Math.round(w * dpr); base.height = Math.round(h * dpr);
    const baseCtx = base.getContext('2d');
    baseCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    drawAxes(baseCtx, { ...box, w, h }, chart.xMin, chart.xMax, chart.yMin, chart.yMax, chart.yLabel, chart.xMin);
    chart.visible.forEach(s => drawTimeSeries(baseCtx, chart, s, seriesColor(chart.def, s)));
    chart.base = base;
  }
  ctx.clearRect(0, 0, w, h);
  ctx.drawImage(chart.base, 0, 0, w, h);
  renderTimeChartOverlay(ctx, chart, w, h, box);
}
function drawTrajectory(target) {
  const canvas=target || $('trajectoryCanvas'), {ctx,w,h}=setupCanvas(canvas), c=canvasStyles(); const box={left:58,right:24,top:20,bottom:h-38};
  ctx.clearRect(0,0,w,h);ctx.fillStyle=c.surface;ctx.fillRect(0,0,w,h);
  const segments=state.trackSegments.map((segment,index)=>({segment,index,points:(segment.points||[]).filter(p=>Array.isArray(p)&&p.length>=2&&Number.isFinite(Number(p[0]))&&Number.isFinite(Number(p[1])))})).filter(item=>item.points.length);
  const allPoints=segments.flatMap(item=>item.points); if(!allPoints.length){ctx.fillStyle=c.muted;ctx.fillText('无有效经纬度',w/2-25,h/2);return;}
  const lat=allPoints.map(p=>Number(p[0])),lon=allPoints.map(p=>Number(p[1])),lat0=lat.reduce((a,b)=>a+b,0)/lat.length,lon0=lon.reduce((a,b)=>a+b,0)/lon.length,cos=Math.cos(lat0*Math.PI/180);
  const projected=allPoints.map(p=>({x:(Number(p[1])-lon0)*111320*cos,y:(Number(p[0])-lat0)*111320}));
  // 必须用循环取极值：Math.min(...arr) 把每个元素展开成实参，约 20 万点就会
  // RangeError: Maximum call stack size exceeded。每个 pcap 约 6000 点，
  // 也就是选中 30 多个文件时轨迹图会直接抛异常（被上层 catch 吞成"解析失败"）。
  let xmin=Infinity,xmax=-Infinity,ymin=Infinity,ymax=-Infinity;
  for(const pt of projected){ if(pt.x<xmin)xmin=pt.x; if(pt.x>xmax)xmax=pt.x; if(pt.y<ymin)ymin=pt.y; if(pt.y>ymax)ymax=pt.y; }
  if(xmin===xmax){xmin-=1;xmax+=1;}if(ymin===ymax){ymin-=1;ymax+=1;}const px=(xmax-xmin)*.08,py=(ymax-ymin)*.08;xmin-=px;xmax+=px;ymin-=py;ymax+=py;
  ctx.strokeStyle=c.line;ctx.lineWidth=1;ctx.font='11px Segoe UI, Microsoft YaHei, sans-serif';ctx.fillStyle=c.muted; for(let i=0;i<=4;i++){const x=box.left+i/4*(w-box.left-box.right),y=box.bottom-i/4*(box.bottom-box.top);ctx.beginPath();ctx.moveTo(x,box.top);ctx.lineTo(x,box.bottom);ctx.moveTo(box.left,y);ctx.lineTo(w-box.right,y);ctx.stroke();ctx.textAlign='center';ctx.fillText(fmtAxis(xmin+i/4*(xmax-xmin)),x,box.bottom+9);ctx.textAlign='right';ctx.fillText(fmtAxis(ymin+i/4*(ymax-ymin)),box.left-7,y);}ctx.strokeRect(box.left+.5,box.top+.5,w-box.left-box.right,box.bottom-box.top);ctx.fillStyle=c.muted;ctx.textAlign='center';ctx.fillText('东向 / East (m)',(box.left+w-box.right)/2,h-10);ctx.save();ctx.translate(13,(box.top+box.bottom)/2);ctx.rotate(-Math.PI/2);ctx.fillText('北向 / North (m)',0,0);ctx.restore();
  const sx=p=>box.left+(p.x-xmin)/(xmax-xmin)*(w-box.left-box.right),sy=p=>box.bottom-(p.y-ymin)/(ymax-ymin)*(box.bottom-box.top); let cursor=0;
  segments.forEach(item=>{const selected=state.selectedFiles.has(item.segment.name),color=selected?colors[item.index%colors.length]:'#6f7c92',opacity=selected?1:.35,points=item.points.map(p=>{const point={x:(Number(p[1])-lon0)*111320*cos,y:(Number(p[0])-lat0)*111320};return Number.isFinite(point.x)&&Number.isFinite(point.y)?point:null;}).filter(Boolean);ctx.strokeStyle=color;ctx.globalAlpha=opacity;ctx.lineWidth=selected?2.2:1.5;ctx.beginPath();points.forEach((p,i)=>i?ctx.lineTo(sx(p),sy(p)):ctx.moveTo(sx(p),sy(p)));ctx.stroke();if(points.length){ctx.fillStyle=color;ctx.beginPath();ctx.arc(sx(points[0]),sy(points[0]),selected?3.5:2.5,0,Math.PI*2);ctx.fill();ctx.beginPath();ctx.arc(sx(points[points.length-1]),sy(points[points.length-1]),selected?3.5:2.5,0,Math.PI*2);ctx.fill();}cursor+=points.length;});ctx.globalAlpha=1;
  const sel=state.selection;
  if(sel&&finite(sel.time)){const r=sel.row||{},slat=r.latitude,slon=r.longitude;if(finite(slat)&&finite(slon)){const pt={x:(slon-lon0)*111320*cos,y:(slat-lat0)*111320};ctx.save();ctx.strokeStyle='#ff7e8b';ctx.fillStyle='rgba(255,126,139,.16)';ctx.lineWidth=2;ctx.beginPath();ctx.arc(sx(pt),sy(pt),10,0,Math.PI*2);ctx.fill();ctx.stroke();ctx.strokeStyle='#ffffff';ctx.fillStyle='#ff7e8b';ctx.beginPath();ctx.arc(sx(pt),sy(pt),5,0,Math.PI*2);ctx.fill();ctx.stroke();ctx.restore();}}
  canvas._trajectory={segments,box};
}
// 交互层绘制用 rAF 合帧：mousemove / 滑块 input 一秒可触发上百次，每帧最多画一次。
let overlayFrame = 0; const overlayQueue = new Map();
function scheduleOverlayDraw(id, canvas) {
  overlayQueue.set(id, canvas || null);
  if (overlayFrame) return;
  overlayFrame = requestAnimationFrame(() => {
    overlayFrame = 0;
    const jobs = [...overlayQueue]; overlayQueue.clear();
    for (const [chartId, target] of jobs) {
      drawTimeChart(chartId, target, true);
      const tip = pendingTooltip.get(chartId);
      if (tip) { pendingTooltip.delete(chartId); showTooltip(tip.x, tip.y, tip.chart, tip.lo); }
    }
  });
}
let fullFrame = 0; const fullQueue = new Set();
function scheduleFullChart(id) {
  fullQueue.add(id);
  if (fullFrame) return;
  fullFrame = requestAnimationFrame(() => { fullFrame = 0; const ids = [...fullQueue]; fullQueue.clear(); for (const chartId of ids) drawTimeChart(chartId); });
}
// force=true 表示"数据变了，把所有图表都画一遍"。
// 滚动可见性缓存（IntersectionObserver）只适合交互路径省一点算力（拖时间窗口、悬停），
// 用在数据加载上会留下空白：加载完成时不在视口里的画布会被 invalidateChart 置空，而
// 观察器只在"进入/离开视口"时补画——用户如果是拖滚动条、快速滚轮或点导航按钮跳过去的
// （画布从未进入过视口），这些图表就一直是空白的，看起来正是"解析完成后所有图表都不显示"。
// 每次都画全部图表。曾经按"画布是否在视口里"跳过屏幕外的图表（IntersectionObserver +
// invalidateChart），但那条路径会让用户跳着滚过去的图表一直空白（"解析完成后所有图表
// 都不显示"），收益（省几百毫秒）远不如风险大，所以整套可见性缓存已删除。
function renderCharts() {
  Object.keys(chartDefs).forEach(id => drawTimeChart(id));
}
function renderSpatial() { renderMap(); drawTrajectory(); }
function renderAllCharts(){ renderLegends(); renderCharts(); renderSpatial(); }
function nearestFiniteIndex(rows,index,key){if(index>=0&&index<rows.length&&finite(rows[index][key]))return index;for(let step=1;step<rows.length;step++){const left=index-step,right=index+step;if(left>=0&&finite(rows[left][key]))return left;if(right<rows.length&&finite(rows[right][key]))return right;}return -1;}
function chartIndexAt(canvas, chart, clientX){ const rect=canvas.getBoundingClientRect(), x=clientX-rect.left; const ratio=Math.max(0,Math.min(1,(x-chart.box.left)/(rect.width-chart.box.left-chart.box.right))); const target=chart.xMin+ratio*(chart.xMax-chart.xMin); let lo=0,hi=chart.xs.length-1;while(lo<hi){const mid=(lo+hi)>>1;if(chart.xs[mid]<target)lo=mid+1;else hi=mid;}if(lo>0&&Math.abs(chart.xs[lo-1]-target)<Math.abs(chart.xs[lo]-target))lo--;return lo;}
function redrawWithHover(id, event){ const canvas=$(chartDefs[id].canvas), chart=canvas._chart; if(!chart)return; const lo=chartIndexAt(canvas,chart,event.clientX); state.chartHover.set(id,lo); pendingTooltip.set(id,{x:event.clientX,y:event.clientY,chart,lo}); scheduleOverlayDraw(id); }
function positionTooltip(x,y){const tip=$('tooltip');tip.style.left=`${Math.max(8,Math.min(window.innerWidth-325,x+14))}px`;tip.style.top=`${Math.max(8,Math.min(window.innerHeight-180,y+14))}px`;}
function showTooltip(x,y,chart,index){const tip=$('tooltip'),row=chart.rows[index];tip.innerHTML=`<div class="tooltip-time">${esc(row.gps_time || '')}</div>`+chart.visible.map(s=>{const nearest=nearestFiniteIndex(chart.rows,index,s.key),value=nearest<0?null:chart.rows[nearest][s.key];return `<div class="tooltip-row"><span>${esc(s.label)}</span><strong>${finite(value)?fmt(value,6):'—'} ${esc(s.unit)}</strong></div>`;}).join('');tip.style.display='block';positionTooltip(x,y);}
function showTableTooltip(event,row,pinned=false){tableTooltipPinned=pinned;const tip=$('tooltip'),source=row._source_file || (state.file==='ALL'?'未知文件':state.file),packet=row.pcap_packet_index == null || row.pcap_packet_index === '' ? '—' : row.pcap_packet_index,record=row.index == null || row.index === '' ? '—' : row.index,raw=row.raw_timestamp || '—',gps=row.gps_time || '—';tip.innerHTML=`<div class="tooltip-time">所属 PCAP：${esc(source)}</div><div class="tooltip-row"><span>PCAP 包序号</span><strong>${esc(packet)}</strong></div><div class="tooltip-row"><span>文件内记录序号</span><strong>${esc(record)}</strong></div><div class="tooltip-row"><span>原始时间戳</span><strong>${esc(raw)}</strong></div><div class="tooltip-row"><span>GPS 时间</span><strong>${esc(gps)}</strong></div>`;tip.style.display='block';positionTooltip(event.clientX,event.clientY);}
function hideTooltip(){ $('tooltip').style.display='none'; }
function selectionRowKey(row){ return `${row._source_file || state.file}\u0000${row.index == null ? '' : row.index}`; }
function refreshSelectionVisuals(){ Object.keys(chartDefs).forEach(id=>scheduleOverlayDraw(id)); drawTrajectory(); updateMapSelection(); }
function clearSelection(){
  state.selection=null;
  tableTooltipPinned=false; hideTooltip();
  document.querySelectorAll('.table-row-active').forEach(el=>el.classList.remove('table-row-active'));
  $('selectionBar').hidden=true;
  refreshSelectionVisuals();
}
function selectRow(row){
  const key=selectionRowKey(row), time=xValue(row);
  if(state.selection && state.selection.key===key){ clearSelection(); return false; }
  if(state.selection){ tableTooltipPinned=false; hideTooltip(); document.querySelectorAll('.table-row-active').forEach(el=>el.classList.remove('table-row-active')); }
  state.selection={key,row,time};
  const source=(row._source_file || state.file).replace(/\.(txt|pcap)$/i,'');
  $('selectionChip').innerHTML=`<strong>${esc(row.gps_time || '—')}</strong> · ${esc(source)}${row.index==null?'':` · #${esc(row.index)}`}`;
  $('selectionBar').hidden=false;
  refreshSelectionVisuals();
  return true;
}
function updateMapSelection(){
  if(mapSelectionMarker){ mapSelectionMarker.remove(); mapSelectionMarker=null; }
  if(!leafletMap || !state.selection) return;
  const sel=state.selection, lat=sel.row.latitude, lon=sel.row.longitude;
  if(!finite(lat)||!finite(lon)) return;
  const source=(sel.row._source_file || state.file).replace(/\.(txt|pcap)$/i,'');
  mapSelectionMarker=L.circleMarker([lat,lon],{radius:9,color:'#ffffff',weight:2.4,fillColor:'#ff7e8b',fillOpacity:.95}).addTo(leafletMap);
  mapSelectionMarker.bindTooltip(`选中：${esc(source)} · ${esc(sel.row.gps_time || '')}`,{direction:'top'});
}
let zoomKind=null, zoomId=null, zoomLeafletMap=null, zoomSelectionMarker=null;
function zoomInfoFor(panel){
  if(panel.querySelector('#leafletMap')) return {kind:'map',id:'map'};
  const canvas=panel.querySelector('canvas');
  if(!canvas) return null;
  if(canvas.id==='trajectoryCanvas') return {kind:'trajectory',id:'trajectory'};
  const found=Object.keys(chartDefs).find(key=>chartDefs[key].canvas===canvas.id);
  return found?{kind:'time',id:found}:null;
}
function openZoomFromPanel(panel){
  const info=zoomInfoFor(panel); if(!info) return;
  closeZoomView();
  zoomKind=info.kind; zoomId=info.id;
  $('zoomTitle').textContent=(panel.querySelector('h4')||{}).textContent||'图表';
  $('zoomBody').innerHTML=info.kind==='map'?'<div id="zoomMap"></div>':'<canvas id="zoomCanvas"></canvas>';
  $('zoomOverlay').hidden=false;
  requestAnimationFrame(()=>requestAnimationFrame(()=>{ if(zoomKind==='map')initZoomMap(); else { drawZoomView(); bindZoomCanvasHover(); } }));
}
function closeZoomView(){
  $('zoomOverlay').hidden=true;
  if(zoomLeafletMap){ zoomLeafletMap.remove(); zoomLeafletMap=null; }
  zoomSelectionMarker=null;
  $('zoomBody').innerHTML='';
  tableTooltipPinned=false; hideTooltip();
  zoomKind=null; zoomId=null;
}
function drawZoomView(){
  const canvas=$('zoomCanvas'); if(!canvas) return;
  if(zoomKind==='time' && chartDefs[zoomId]) drawTimeChart(zoomId, canvas);
  else if(zoomKind==='trajectory') drawTrajectory(canvas);
}
function bindZoomCanvasHover(){
  if(zoomKind!=='time') return;
  const canvas=$('zoomCanvas'); if(!canvas || canvas._zoomBound) return;
  canvas._zoomBound=true;
  let lastIndex=-1;
  canvas.addEventListener('mousemove',e=>{
    const c=$('zoomCanvas'); if(!c || !c._chart) return;
    const chart=c._chart, lo=chartIndexAt(c,chart,e.clientX);
    if(lo===lastIndex) return;
    lastIndex=lo;
    state.chartHover.set(zoomId,lo);
    drawTimeChart(zoomId,c,true);
    showTooltip(e.clientX,e.clientY,chart,lo);
  });
  canvas.addEventListener('mouseleave',()=>{ lastIndex=-1; state.chartHover.delete(zoomId); hideTooltip(); });
}
function initZoomMap(){
  const el=$('zoomMap'); if(!el || !window.L || zoomLeafletMap) return;
  const active=document.querySelector('#mapLayerSwitch button.active');
  const tiles=((active && active.dataset.layer)==='sat') ? satelliteTile() : streetTile();
  zoomLeafletMap=L.map(el,{preferCanvas:true,zoomControl:true,attributionControl:true});
  tiles.addTo(zoomLeafletMap);
  drawZoomMapContent();
  setTimeout(()=>{ if(zoomLeafletMap) zoomLeafletMap.invalidateSize(); },80);
}
function drawZoomMapContent(){
  if(!zoomLeafletMap) return;
  if(zoomSelectionMarker){ zoomSelectionMarker.remove(); zoomSelectionMarker=null; }
  const bounds=[];
  state.trackSegments.forEach((segment,index)=>{
    const points=(segment.points||[]).filter(p=>Array.isArray(p)&&p.length>=2&&Number.isFinite(Number(p[0]))&&Number.isFinite(Number(p[1]))).map(p=>[Number(p[0]),Number(p[1])]);
    if(!points.length) return;
    points.forEach(point=>bounds.push(point));
    const selected=state.selectedFiles.has(segment.name), color=selected?colors[index%colors.length]:'#6f7c92', opacity=selected?.92:.35;
    if(points.length>=2) L.polyline(points,{color,weight:selected?3:2,opacity,smoothFactor:1}).addTo(zoomLeafletMap);
    L.circleMarker(points[0],{radius:selected?5:4,color,weight:2,fillColor:color,fillOpacity:opacity}).addTo(zoomLeafletMap);
    L.circleMarker(points[points.length-1],{radius:selected?5:4,color,weight:2,fillColor:color,fillOpacity:opacity}).addTo(zoomLeafletMap);
  });
  if(bounds.length) zoomLeafletMap.fitBounds(L.latLngBounds(bounds),{padding:[28,28],maxZoom:18});
  const sel=state.selection;
  if(sel){ const lat=sel.row.latitude, lon=sel.row.longitude;
    if(finite(lat)&&finite(lon)){
      zoomSelectionMarker=L.circleMarker([lat,lon],{radius:9,color:'#ffffff',weight:2.4,fillColor:'#ff7e8b',fillOpacity:.95}).addTo(zoomLeafletMap);
    } }
}
function setupChartEvents(){
  Object.entries(chartDefs).forEach(([id,def])=>{
    const canvas=$(def.canvas);
    canvas.dataset.chartId=id;
    canvas.addEventListener('mousemove',e=>redrawWithHover(id,e));
    canvas.addEventListener('mouseleave',()=>{state.chartHover.delete(id);pendingTooltip.delete(id);scheduleOverlayDraw(id);hideTooltip();});
    canvas.addEventListener('click',e=>{
      const chart=canvas._chart; if(!chart)return;
      const lo=chartIndexAt(canvas,chart,e.clientX);
      if(lo<0 || lo>=chart.rows.length)return;
      if(selectRow(chart.rows[lo])) hideTooltip();
    });
  });
  $('trajectoryCanvas').addEventListener('mouseleave',hideTooltip);
}

function loadTable() {
  tableTooltipPinned=false; hideTooltip();
  const allRows=state.rows||[];
  let rows=allRows;
  if(state.search){
    const q=state.search.toLowerCase(), keys=state.columns.map(c=>c.key);
    rows=allRows.filter(row=>q===''||keys.some(k=>String(row[k]==null?'':row[k]).toLowerCase().includes(q)));
  }
  const total=rows.length, pages=Math.max(1,Math.ceil(total/state.pageSize));
  if(state.page>pages) state.page=pages;
  const start=(state.page-1)*state.pageSize;
  const pageRows=rows.slice(start,start+state.pageSize);
  const head=$('rawTable').querySelector('thead'), body=$('rawTable').querySelector('tbody');
  head.innerHTML=`<tr>${state.columns.map(c=>`<th>${esc(c.label)}</th>`).join('')}</tr>`;
  body.innerHTML=pageRows.map((row,rowIndex)=>`<tr data-row-index="${rowIndex}">${state.columns.map(c=>`<td>${c.kind==='number'?fmt(row[c.key],6):esc(row[c.key] ?? '')}</td>`).join('')}</tr>`).join('');
  body.querySelectorAll('tr[data-row-index]').forEach(rowElement=>{
    const row=pageRows[Number(rowElement.dataset.rowIndex)];
    rowElement.addEventListener('mouseenter',event=>{tableTooltipPinned=false;rowElement.classList.add('table-row-active');showTableTooltip(event,row,false);});
    rowElement.addEventListener('mousemove',event=>{if(!tableTooltipPinned)positionTooltip(event.clientX,event.clientY);});
    rowElement.addEventListener('mouseleave',()=>{rowElement.classList.remove('table-row-active');if(!tableTooltipPinned)hideTooltip();});
    rowElement.addEventListener('click',event=>{ if(selectRow(row)){ rowElement.classList.add('table-row-active'); showTableTooltip(event,row,true); } });
  });
  $('tableEmpty').hidden=pageRows.length>0;
  setText('pageInfo',`${state.page} / ${pages} 页 · ${total.toLocaleString()} 行`);
  $('prevPage').disabled=state.page<=1; $('nextPage').disabled=state.page>=pages;
}
function initEvents(){
  $('pickPcapFiles').addEventListener('click',()=>{$('pcapFilesInput').click();});
  $('pickPcapFolder').addEventListener('click',choosePcapFolder);
  $('pcapFilesInput').addEventListener('change',e=>{ const files=Array.from(e.target.files||[]); e.target.value=''; if(!files.length||state.uploadBusy)return; state.pcapSourceFolder=false; setPendingPcapFiles(files,null); if(state.pendingPcapFiles.length) loadSelectedPcap(false); else showError('所选文件都不是 .pcap/.txt 文件。'); });
  $('pcapFolderInput').addEventListener('change',e=>{ const files=Array.from(e.target.files||[]); e.target.value=''; if(!files.length||state.uploadBusy)return; state.pcapSourceFolder=true; setPendingPcapFiles(files,null); if(state.pendingPcapFiles.length) loadSelectedPcap(false); else showError('所选文件夹中没有 .pcap/.txt 文件。'); });
  $('fileSelect').addEventListener('change',e=>{ setView(e.target.value); });
  $('fileList').addEventListener('change',e=>{const input=e.target.closest('.file-toggle');if(!input)return;const name=input.dataset.file;if(input.checked)state.selectedFiles.add(name);else state.selectedFiles.delete(name);renderFileList();renderAllCharts();});
  $('reloadButton').addEventListener('click',()=>{ resetForFreshView(); });
  // 有效里程是独立窗口/独立页面（mileage.html），这里只负责开窗，不参与本页渲染。
  $('openMileageButton').addEventListener('click',()=>{ window.open('mileage.html','_blank','noopener'); });
  $('downloadButton').addEventListener('click',()=>{ if(state.file==='ALL'){showError('请选择单个 PCAP/TXT 文件后再下载。');return;} const blob=state.blobs[state.file]; if(!blob){showError('当前浏览器会话中未保留该原始文件，请重新选择后再下载。');return;} const url=URL.createObjectURL(blob), a=document.createElement('a'); a.href=url; a.download=blob.name; document.body.appendChild(a); a.click(); a.remove(); setTimeout(()=>URL.revokeObjectURL(url),5000); });
  $('clearSelectionButton').addEventListener('click',()=>{clearSelection();});
  document.addEventListener('click',e=>{ const btn=e.target.closest('.zoom-btn'); if(!btn) return; const panel=btn.closest('.chart-panel'); if(panel) openZoomFromPanel(panel); });
  $('zoomClose').addEventListener('click',closeZoomView);
  $('zoomOverlay').addEventListener('click',e=>{ if(e.target===$('zoomOverlay')) closeZoomView(); });
  document.addEventListener('keydown',e=>{ if(e.key==='Escape' && !$('zoomOverlay').hidden) closeZoomView(); });
  let searchTimer; $('tableSearch').addEventListener('input',e=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{state.search=e.target.value;state.page=1;loadTable();},240);});
  $('prevPage').addEventListener('click',()=>{if(state.page>1){state.page--;loadTable();}});$('nextPage').addEventListener('click',()=>{state.page++;loadTable();});
  document.addEventListener('click',event=>{ if(event.target.closest('#rawTable, .chart-panel, .toolbar, .nav, .zoom-overlay')) return; if(state.selection){clearSelection();return;} tableTooltipPinned=false; hideTooltip(); document.querySelectorAll('.table-row-active').forEach(item=>item.classList.remove('table-row-active')); });
  document.querySelectorAll('.nav button').forEach(btn=>btn.addEventListener('click',()=>{document.querySelectorAll('.nav button').forEach(x=>x.classList.remove('active'));btn.classList.add('active');document.getElementById(btn.dataset.target).scrollIntoView({behavior:'smooth',block:'start'});}));
  setupChartEvents();
  window.addEventListener('resize',()=>{window.requestAnimationFrame(()=>{ if($('zoomOverlay').hidden){ renderAllCharts(); if(leafletMap)leafletMap.invalidateSize(); } else if(zoomKind==='map'){ if(zoomLeafletMap)zoomLeafletMap.invalidateSize(); } else window.requestAnimationFrame(drawZoomView); });});
}
async function init(){
  initMap(); initEvents(); setupRuntimeErrorReporting();
  resetForFreshView();
  schedulePcapFolderPolling();
}
init();
