/**
 * 参数验证 sheet：外参校验 + 数据质检 + 点云反投影。
 *
 * 后端接口：
 *   POST /api/verify/run      创建后台任务（串行执行）
 *   GET  /api/verify/task?id= 任务状态 + 进度 + 结果
 *   GET  /api/verify/file?id=&name=  产出文件下载/预览
 *
 * 复用 mileage.js 里的 $ / setText / esc / fmtCount / formatBytes / postJson / getJson。
 */
(function () {
  const state = { taskId: '', timer: null, busy: false, dataset: null };

  function vStatus(text, tone) {
    const el = $('verifyStatus');
    if (!el) return;
    el.className = `mileage-status${tone ? ` is-${tone}` : ''}`;
    el.textContent = text;
  }

  function setProgress(percent, text, visible) {
    const wrap = $('verifyProgressWrap');
    if (!wrap) return;
    wrap.hidden = visible === false;
    const bar = $('verifyProgressBar');
    if (bar) bar.style.width = `${Math.max(0, Math.min(100, Number(percent) || 0))}%`;
    setText('verifyProgressText', `${Math.max(0, Math.min(100, Number(percent) || 0)).toFixed(0)}% · ${text || ''}`);
  }

  function optionInt(id, fallback) {
    const el = $(id);
    const value = parseInt(el && el.value, 10);
    return Number.isFinite(value) ? value : fallback;
  }

  function optionChecked(id) {
    const el = $(id);
    return !!(el && el.checked);
  }

  function renderTask(task) {
    const failed = task.status === 'failed';
    if (failed) {
      vStatus(`验证失败：${task.error || (task.issues || []).join('；') || '未知错误'}`, 'error');
    } else {
      vStatus('验证完成，请在下方确认交付图', 'ok');
    }
    // 任务结束后自动加载「交付图人工确认」面板（优先用数据目录里已有的产出）
    loadOverview();
    loadAllHistory(true);
  }

  function stopPolling() {
    if (state.timer) {
      clearTimeout(state.timer);
      state.timer = null;
    }
    state.busy = false;
    const button = $('verifyRunButton');
    if (button) {
      button.disabled = false;
      button.textContent = '开始验证';
    }
    const forceButton = $('verifyForceRunButton');
    if (forceButton) {
      forceButton.disabled = false;
      forceButton.textContent = '强制重新分析';
    }
  }

  async function poll() {
    if (!state.taskId) return;
    try {
      const data = await getJson(`/api/verify/task?id=${encodeURIComponent(state.taskId)}`);
      const task = data.task || {};
      const progress = data.progress || {};
      const percent = Number(progress.percent || 0);
      setProgress(percent, progress.message || progress.step || task.status || '运行中');
      if (task.status === 'success' || task.status === 'failed') {
        setProgress(task.status === 'success' ? 100 : percent,
          task.status === 'success' ? '完成' : (task.error || '失败'));
        renderTask(task);
        stopPolling();
        return;
      }
      vStatus(
        `${progress.step || '验证中'}${progress.message ? ` · ${progress.message}` : ''}`
        + `（${percent.toFixed(0)}%）`,
        'busy'
      );
    } catch (error) {
      // 轮询失败不立即放弃：任务可能还在跑，给几次重试
      vStatus(`读取进度失败：${errorDetail(error && error.message ? error.message : String(error))}`, 'error');
    }
    state.timer = setTimeout(poll, 1500);
  }

  async function runVerify(force) {
    if (state.busy) return;
    const input = $('verifyRootInput');
    const root = String(input && input.value != null ? input.value : '').trim();
    if (!root) {
      vStatus('请先填数据目录（含 parameter_yaml / cam_* / lidar_* / pose）', 'error');
      if (input && input.focus) input.focus();
      return;
    }

    const body = {
      data_root: root,
      run_data_quality: optionChecked('verifyOptQuality'),
      run_projection: optionChecked('verifyOptProjection'),
      only_cameras: String(($('verifyOnlyCameras') || {}).value || '').trim(),
      refresh: Boolean(force),
    };
    if (!body.run_data_quality && !body.run_projection) {
      vStatus('数据质检和外参校验至少要选一个', 'error');
      return;
    }

    state.busy = true;
    const button = $('verifyRunButton');
    const forceButton = $('verifyForceRunButton');
    if (button) {
      button.disabled = true;
      button.textContent = '验证中…';
    }
    if (forceButton) {
      forceButton.disabled = true;
      forceButton.textContent = '重新分析中…';
    }
    setProgress(0, '提交任务…', true);
    vStatus(`正在提交验证任务：${root}`, 'busy');
    try {
      const data = await postJson('/api/verify/run', body);
      state.taskId = data.task_id;
      state.dataset = data.dataset || null;
      if (data.cached && data.task) {
        // 库里已有这个目录的结果：直接展示交付图，不再跑管线
        setProgress(100, '读取历史结果');
        vStatus(`读取历史结果（计算于 ${data.computed_at || '—'}），请在下方确认交付图`, 'ok');
        renderTask(data.task);
        stopPolling();
        return;
      }
      vStatus(
        `任务已创建（${data.task_id}）· 产出目录 ${data.output_root || '—'}，后台执行中…`,
        'busy'
      );
      setProgress(1, '任务已创建，等待执行…');
      poll();
    } catch (error) {
      stopPolling();
      setProgress(0, '提交失败', false);
      vStatus(`参数验证失败：${errorDetail(error && error.message ? error.message : String(error))}`, 'error');
    }
  }

  // ---- 交付图人工确认 -----------------------------------------------------

  const confirmState = {
    root: '',
    groups: [],
    confirmations: {},
    loading: false,
  };

  function confirmKey(category, name) {
    return `${category}|${name}`;
  }

  function sourceUrl(category, name, thumbWidth) {
    const root = confirmState.root || '';
    const width = thumbWidth ? `&w=${thumbWidth}` : '';
    return `/api/verify/source?data_root=${encodeURIComponent(root)}`
      + `&path=${encodeURIComponent(name)}${width}`;
  }

  function statusOf(category, name) {
    const item = confirmState.confirmations[confirmKey(category, name)];
    return (item && item.status) || 'pending';
  }

  function statusChip(category, name) {
    const status = statusOf(category, name);
    if (status === 'confirmed') return '<span class="confirm-chip is-ok">通过</span>';
    if (status === 'rejected') return '<span class="confirm-chip is-bad">不通过</span>';
    return '<span class="confirm-chip">待确认</span>';
  }

  function groupCounts(group) {
    let confirmed = 0;
    let rejected = 0;
    group.items.forEach((item) => {
      const status = statusOf(group.key, item.name);
      if (status === 'confirmed') confirmed += 1;
      else if (status === 'rejected') rejected += 1;
    });
    return { confirmed, rejected, pending: group.items.length - confirmed - rejected };
  }

  function renderConfirm() {
    const container = $('verifyConfirmGroups');
    if (!container) return;
    if (!confirmState.groups.length) {
      container.innerHTML = '<div class="mileage-subtle">没有找到投影图/质检图产出。</div>';
      setText('verifyConfirmSummary', '—');
      return;
    }
    let totalConfirmed = 0;
    let totalRejected = 0;
    let totalItems = 0;
    container.innerHTML = confirmState.groups.map((group) => {
      const counts = groupCounts(group);
      totalConfirmed += counts.confirmed;
      totalRejected += counts.rejected;
      totalItems += group.items.length;
      const sampled = group.kind === 'projection'
        ? `（共 ${group.total} 张，按时间戳均匀抽 ${group.items.length} 张）`
        : `（共 ${group.total} 张，全部展示）`;
      const itemsHtml = group.items.map((item) => {
        const status = statusOf(group.key, item.name);
        const record = confirmState.confirmations[confirmKey(group.key, item.name)] || {};
        const reason = status === 'rejected' ? String(record.note || '').trim() : '';
        const cls = status === 'confirmed' ? 'is-ok' : status === 'rejected' ? 'is-bad' : '';
        const captionHtml = group.kind === 'projection'
          ? `<span class="confirm-caption" title="${esc(item.name)}">`
            + `<span class="confirm-caption-time">${esc(item.time || '')}</span>`
            + `<span class="confirm-caption-file">${esc(item.file || item.name || '')}</span>`
            + '</span>'
          : `<span class="confirm-caption" title="${esc(item.name)}">`
            + `<span class="confirm-caption-file">${esc(item.file || item.name || '')}</span>`
            + '</span>';
        return `<figure class="confirm-item ${cls}"
                 data-category="${esc(group.key)}" data-name="${esc(item.name)}">
          <label class="confirm-check"><input type="checkbox" class="confirm-select"
                 data-category="${esc(group.key)}" data-name="${esc(item.name)}"></label>
          <a href="${sourceUrl(group.key, item.name)}" target="_blank" rel="noopener">
            <img src="${sourceUrl(group.key, item.name, 720)}"
                 data-full="${sourceUrl(group.key, item.name)}"
                 alt="${esc(item.file)}" loading="lazy" decoding="async">
          </a>
          <figcaption>
            ${captionHtml}
            <span class="confirm-status">${statusChip(group.key, item.name)}</span>
          </figcaption>
          ${reason ? `<div class="confirm-note" title="${esc(reason)}">原因：${esc(reason)}</div>` : ''}
          <div class="confirm-actions">
            <button class="ghost-button" type="button" data-action="confirm"
                    data-category="${esc(group.key)}" data-name="${esc(item.name)}">通过</button>
            <button class="ghost-button" type="button" data-action="reject"
                    data-category="${esc(group.key)}" data-name="${esc(item.name)}">不通过</button>
          </div>
        </figure>`;
      }).join('');
      const resampleButton = group.kind === 'projection'
        ? `<button class="ghost-button verify-resample" type="button" data-action="resample"
                   data-category="${esc(group.key)}" title="从全部投影图中按时间戳随机重新抽 5 张，避开空旷路段">换 5 张</button>`
        : '';
      return `<div class="confirm-group" data-group="${esc(group.key)}">
        <div class="confirm-group-head">
          <h4>${esc(group.title)} <span class="mileage-subtle">${sampled}</span></h4>
          <span class="confirm-group-counts">通过 ${counts.confirmed} · 不通过 ${counts.rejected} · 待确认 ${counts.pending}</span>
          <label class="batch-check"><input type="checkbox" class="confirm-select-all"> 全选本组</label>
          ${resampleButton}
          <button class="ghost-button" type="button" data-action="batch-confirm" data-category="${esc(group.key)}">通过所选</button>
          <button class="ghost-button" type="button" data-action="batch-reject" data-category="${esc(group.key)}">不通过所选</button>
        </div>
        <div class="confirm-grid">${itemsHtml}</div>
      </div>`;
    }).join('');
    setText(
      'verifyConfirmSummary',
      `通过 ${totalConfirmed} · 不通过 ${totalRejected} · 待确认 ${totalItems - totalConfirmed - totalRejected}`
    );
    updateNotifyButton();
  }

  async function loadOverview(options) {
    const silent = !!(options && options.silent);
    if (confirmState.loading) return;
    const input = $('verifyRootInput');
    const root = String(input && input.value != null ? input.value : '').trim();
    if (!root) {
      if (!silent) setText('verifyConfirmStatus', '请先填数据目录');
      return;
    }
    confirmState.loading = true;
    if (!silent) setText('verifyConfirmStatus', `正在读取交付图：${root} …`);
    const dateEl = $('verifyDataDate');
    if (!silent && dateEl) {
      dateEl.hidden = true;
      dateEl.textContent = '';
    }
    try {
      const data = await postJson('/api/verify/overview', { data_root: root });
      confirmState.root = data.data_root || root;
      confirmState.groups = Array.isArray(data.groups) ? data.groups : [];
      confirmState.confirmations = data.confirmations || {};
      $('verifyConfirmPanel').hidden = false;
      const removeBtn = $('verifyRemoveButton');
      if (removeBtn) removeBtn.hidden = true;
      renderConfirm();
      // 数据采集日期取自数据本身（FrameMap / 图片文件名的时间戳），不再用目录名
      const loadedRoot = data.data_root || root;
      const fallbackMatch = String(loadedRoot).match(/(20\d{6})/g);
      const fallbackDate = fallbackMatch
        ? `${fallbackMatch[fallbackMatch.length - 1].slice(0, 4)}-${fallbackMatch[fallbackMatch.length - 1].slice(4, 6)}-${fallbackMatch[fallbackMatch.length - 1].slice(6, 8)}`
        : '';
      const dateText = String(data.data_date || '').trim() || fallbackDate;
      if (dateEl) {
        dateEl.hidden = !dateText;
        dateEl.innerHTML = dateText ? `数据采集日期：<b>${esc(dateText)}</b>` : '';
      }
      if (!silent) setText('verifyConfirmStatus', '');
      // 不是从历史列表点进来的（队列里没有这个目录）就当单独一条
      if (!reviewNav.queue.includes(loadedRoot)) {
        reviewNav.queue = [loadedRoot];
        reviewNav.index = 0;
        reviewNav.viewKey = '';
        updateReviewNav();
      }
    } catch (error) {
      // 记下当前目录，确认面板里的「强制重新分析」才能在读不到产出时直接重跑
      if (!confirmState.root) confirmState.root = root;
      const message = `读取交付图失败：${errorDetail(error && error.message ? error.message : String(error))}`;
      if (!silent) {
        if (dateEl) dateEl.hidden = true;
        setText('verifyConfirmStatus', message);
        const removeBtn = $('verifyRemoveButton');
        if (removeBtn) removeBtn.hidden = !message.includes('数据目录不存在');
      }
    } finally {
      confirmState.loading = false;
    }
  }

  async function setConfirm(category, names, status, reason) {
    if (!confirmState.root || !names.length) return;
    const verdictText = status === 'rejected' ? '不通过' : '通过';
    setText('verifyConfirmStatus', `正在记录 ${verdictText} ${names.length} 张…`);
    try {
      const data = await postJson('/api/verify/confirm', {
        data_root: confirmState.root,
        category,
        names,
        status,
        note: reason || '',
      });
      confirmState.confirmations = data.items || confirmState.confirmations;
      renderConfirm();
      loadAllHistory();
      if (window.refreshTodo) window.refreshTodo();
      const totalItems = (confirmState.groups || []).reduce(
        (sum, group) => sum + (group.items || []).length, 0
      );
      const summary = data.summary || {};
      const decided = (Number(summary.confirmed) || 0) + (Number(summary.rejected) || 0);
      const allDecided = totalItems > 0 && decided >= totalItems;
      if (allDecided && data.notification) {
        const note = data.notification;
        if (note.pending) {
          reviewNav.waitingDialog = true;
          openNotifyDialog({ ...note, root: confirmState.root });
          setText(
            'verifyConfirmStatus',
            `结果已全部给出（通过 ${note.passed || 0} · 不通过 ${note.rejected || 0}），请在弹窗中确认验证结果`
          );
        } else if (note.sent) {
          setText(
            'verifyConfirmStatus',
            `结果已全部给出（通过 ${note.passed || 0} · 不通过 ${note.rejected || 0}），已发送飞书（${note.at || ''}）`
          );
          maybeAdvanceReview();
        } else if (note.reason === '结果未变化，无需重复推送') {
          setText('verifyConfirmStatus', '结果已全部给出（与上次发送一致，无需重复发送）');
          maybeAdvanceReview();
        } else {
          setText('verifyConfirmStatus', `结果已全部给出；发送失败：${note.reason || '未知原因'}`);
        }
      } else {
        setText('verifyConfirmStatus', `已标记 ${verdictText} ${names.length} 张（${data.updated_at || ''}）`);
      }
    } catch (error) {
      setText('verifyConfirmStatus', `保存失败：${errorDetail(error && error.message ? error.message : String(error))}`);
    }
  }

  function selectedNames(container, category) {
    return Array.from(container.querySelectorAll('.confirm-select'))
      .filter((box) => box.checked && box.getAttribute('data-category') === category)
      .map((box) => box.getAttribute('data-name'));
  }

  // ---- 不通过：必须填写问题原因 ------------------------------------------

  let reasonResolver = null;

  function initReasonDialog() {
    if (document.getElementById('verifyReasonMask')) return;
    const mask = document.createElement('div');
    mask.id = 'verifyReasonMask';
    mask.className = 'notify-mask';
    mask.hidden = true;
    mask.innerHTML = `
      <div class="notify-dialog" role="dialog" aria-modal="true" aria-labelledby="verifyReasonTitle">
        <h4 id="verifyReasonTitle">填写问题原因</h4>
        <div class="notify-stats" id="verifyReasonTargets"></div>
        <textarea class="reason-input" id="verifyReasonInput" rows="4"
                  placeholder="必填：请描述具体问题，例如投影错位、点云与图像不同步、质检图异常等"></textarea>
        <div class="notify-actions">
          <button class="ghost-button" type="button" data-reason="cancel">取消</button>
          <button class="primary-button" type="button" data-reason="ok">确认不通过</button>
        </div>
        <div class="mileage-status is-error" id="verifyReasonStatus" role="status" aria-live="polite"></div>
      </div>`;
    document.body.appendChild(mask);
    mask.addEventListener('click', (event) => {
      if (event.target === mask) finishReasonDialog(null);
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !mask.hidden) finishReasonDialog(null);
    });
    mask.querySelectorAll('[data-reason]').forEach((button) => {
      button.addEventListener('click', () => {
        const action = button.getAttribute('data-reason');
        if (action === 'cancel') {
          finishReasonDialog(null);
          return;
        }
        const input = $('verifyReasonInput');
        const text = String((input && input.value) || '').trim();
        if (!text) {
          setText('verifyReasonStatus', '问题原因不能为空，请填写后再提交');
          if (input) input.focus();
          return;
        }
        finishReasonDialog(text);
      });
    });
  }

  function finishReasonDialog(value) {
    const mask = $('verifyReasonMask');
    if (mask) mask.hidden = true;
    const resolve = reasonResolver;
    reasonResolver = null;
    if (resolve) resolve(value);
  }

  function askReason(targetsText, initialText) {
    initReasonDialog();
    const mask = $('verifyReasonMask');
    if (!mask) return Promise.resolve(null);
    setText('verifyReasonStatus', '');
    setText('verifyReasonTargets', targetsText || '');
    const input = $('verifyReasonInput');
    if (input) input.value = initialText ? String(initialText) : '';
    mask.hidden = false;
    if (input) setTimeout(() => input.focus(), 30);
    return new Promise((resolve) => { reasonResolver = resolve; });
  }

  async function resampleGroup(category) {
    const group = (confirmState.groups || []).find((item) => item.key === category);
    if (!group) return;
    const current = (group.items || []).map((item) => item.name);
    setText('verifyConfirmStatus', '正在按时间戳重新抽取 5 张…');
    try {
      const data = await postJson('/api/verify/resample', {
        data_root: confirmState.root,
        category,
        exclude: current,
      });
      const items = Array.isArray(data.items) ? data.items : [];
      if (!items.length) {
        setText('verifyConfirmStatus', '没有可抽取的投影图');
        return;
      }
      group.items = items;
      renderConfirm();
      const times = items.map((item) => item.time).filter(Boolean);
      setText(
        'verifyConfirmStatus',
        `已重新抽取 ${items.length} 张${times.length ? `（${times[0]} ~ ${times[times.length - 1]}）` : ''}，请确认这 ${items.length} 张`
      );
    } catch (error) {
      setText('verifyConfirmStatus', `重新抽取失败：${errorDetail(error && error.message ? error.message : String(error))}`);
    }
  }

  function bindConfirmEvents() {
    const container = $('verifyConfirmGroups');
    if (!container) return;
    container.addEventListener('click', (event) => {
      const button = event.target.closest('[data-action]');
      if (!button) return;
      const action = button.getAttribute('data-action');
      const category = button.getAttribute('data-category');
      if (!category) return;
      if (action === 'resample') {
        resampleGroup(category);
        return;
      }
      if (action === 'confirm' || action === 'reject') {
        const name = button.getAttribute('data-name');
        if (action === 'confirm') {
          setConfirm(category, [name], 'confirmed');
          return;
        }
        // 不通过必须填写问题原因
        const record = confirmState.confirmations[confirmKey(category, name)] || {};
        askReason(`不通过：${name}`, String(record.note || '')).then((reason) => {
          if (reason) setConfirm(category, [name], 'rejected', reason);
        });
        return;
      }
      if (action === 'batch-confirm' || action === 'batch-reject') {
        const names = selectedNames(container, category);
        if (!names.length) {
          setText('verifyConfirmStatus', '本组还没有勾选图片');
          return;
        }
        if (action === 'batch-confirm') {
          setConfirm(category, names, 'confirmed');
          return;
        }
        askReason(`不通过所选 ${names.length} 张`).then((reason) => {
          if (reason) setConfirm(category, names, 'rejected', reason);
        });
      }
    });
    container.addEventListener('change', (event) => {
      const target = event.target;
      if (target.classList && target.classList.contains('confirm-select-all')) {
        const group = target.closest('.confirm-group');
        if (!group) return;
        group.querySelectorAll('.confirm-select').forEach((box) => { box.checked = target.checked; });
      }
    });
  }

  // ---- 交付包下载 ---------------------------------------------------------

  async function loadDeliveries() {
    const panel = $('verifyDeliveryPanel');
    if (panel) panel.hidden = false;
    const table = $('verifyDeliveryTable');
    if (table) table.innerHTML = '<div class="mileage-subtle">读取中…</div>';
    try {
      const data = await getJson('/api/verify/deliveries');
      const items = Array.isArray(data.items) ? data.items : [];
      setText('verifyDeliverySummary', items.length ? `${items.length} 个交付包` : '暂无交付包');
      if (!items.length) {
        table.innerHTML = `<div class="mileage-subtle">服务器交付目录里还没有 zip：${esc(data.dir || '')}</div>`;
        return;
      }
      const header = '<table class="mini-table"><thead><tr>'
        + '<th>日期</th><th>交付包</th><th>数据目录</th><th>大小</th><th>上传时间</th><th>操作</th>'
        + '</tr></thead><tbody>';
      const rows = items.map((item) => (
        `<tr><td>${esc(item.date || '—')}</td>`
        + `<td class="batch-note" title="${esc(item.title || item.name)}">${esc(item.name)}</td>`
        + `<td class="history-root" title="${esc(item.data_root || '')}">${esc(item.data_root || '—')}</td>`
        + `<td>${formatBytes(item.size)}</td>`
        + `<td>${esc(item.mtime || '—')}</td>`
        + `<td><a class="ghost-button" href="${esc(item.download_url || '#')}" download>下载</a></td></tr>`
      )).join('');
      table.innerHTML = `${header}${rows}</tbody></table>`;
    } catch (error) {
      setText('verifyDeliverySummary', '读取失败');
      if (table) table.innerHTML = `<div class="mileage-subtle">${esc(errorDetail(error && error.message ? error.message : String(error)))}</div>`;
    }
  }

  // ---- 批量验证（父目录自动遍历） -----------------------------------------

  const batchState = { root: '', datasets: [], selected: new Set(), results: {}, busy: false };

  function setBatchStatus(text, tone) {
    const el = $('verifyBatchStatus');
    if (!el) return;
    el.className = `mileage-status${tone ? ` is-${tone}` : ''}`;
    el.textContent = text;
  }

  function updateBatchPickInfo() {
    setText('verifyBatchPickInfo', `已选 ${batchState.selected.size} / ${batchState.datasets.length} 个`);
    const button = $('verifyBatchRunButton');
    if (button) button.disabled = batchState.busy || batchState.selected.size === 0;
  }

  function datasetHint(dataset) {
    if (dataset.analyzed) {
      return `已分析${dataset.analyzed_at ? `（${dataset.analyzed_at}）` : ''}`;
    }
    if (dataset.has_parameter_yaml) return '可验证（未分析）';
    if (dataset.has_output) return '仅已有产出';
    return '缺 parameter_yaml';
  }

  function renderBatchDatasets() {
    const table = $('verifyBatchTable');
    if (!table) return;
    if (!batchState.datasets.length) {
      table.innerHTML = '<div class="mileage-subtle">父目录下没有找到含 parameter_yaml 或已有产出的数据集。</div>';
      updateBatchPickInfo();
      return;
    }
    const header = '<table class="mini-table"><thead><tr>'
      + '<th class="batch-check"></th><th>数据集</th><th>地区</th><th>路径</th><th>参数</th><th>已有产出</th>'
      + '<th>历史结果</th><th>说明</th>'
      + '</tr></thead><tbody>';
    const rows = batchState.datasets.map((dataset, index) => {
      const checked = batchState.selected.has(dataset.path) ? ' checked' : '';
      const analyzedCell = dataset.analyzed
        ? `<span class="cache-tag">已分析 ${esc(String(dataset.analyzed_at || '').slice(5, 16))}</span>`
        : '<span class="cache-tag is-new">未分析</span>';
      return `<tr>
        <td class="batch-check"><input type="checkbox" class="verify-batch-check" data-path="${esc(dataset.path)}"${checked}></td>
        <td>${esc(dataset.name)}</td>
        <td>${dataset.region ? `<span class="cache-tag">${esc(dataset.region)} · ${esc(dataset.vehicle || '')}</span>` : '—'}</td>
        <td class="history-root" title="${esc(dataset.path)}">${esc(dataset.path)}</td>
        <td>${dataset.has_parameter_yaml ? '✔' : '—'}</td>
        <td>${dataset.has_output ? '✔' : '—'}</td>
        <td>${analyzedCell}</td>
        <td>${esc(datasetHint(dataset))}</td>
      </tr>`;
    }).join('');
    table.innerHTML = `${header}${rows}</tbody></table>`;
    table.querySelectorAll('.verify-batch-check').forEach((box) => {
      box.addEventListener('change', () => {
        const path = box.getAttribute('data-path');
        if (box.checked) batchState.selected.add(path);
        else batchState.selected.delete(path);
        updateBatchPickInfo();
      });
    });
    updateBatchPickInfo();
  }

  function selectBatchDatasets(kind) {
    batchState.selected.clear();
    if (kind === 'all') {
      batchState.datasets.forEach((dataset) => batchState.selected.add(dataset.path));
    } else if (kind === 'ready') {
      // 增量：只选「可验证且库里没分析过」的
      batchState.datasets
        .filter((dataset) => dataset.has_parameter_yaml && !dataset.analyzed)
        .forEach((dataset) => batchState.selected.add(dataset.path));
    }
    renderBatchDatasets();
  }

  async function scanVerifyBatch() {
    if (batchState.busy) return;
    const input = $('verifyBatchRootInput');
    const root = String(input && input.value != null ? input.value : '').trim();
    if (!root) {
      setBatchStatus('请先填父目录', 'error');
      return;
    }
    const button = $('verifyBatchScanButton');
    if (button) { button.disabled = true; button.textContent = '扫描中…'; }
    setBatchStatus(`正在扫描 ${root} …`, 'busy');
    try {
      const data = await postJson('/api/verify/scan', { data_root: root });
      batchState.root = data.root || root;
      batchState.datasets = Array.isArray(data.datasets) ? data.datasets : [];
      // 增量：默认只选「可验证且库里没分析过」的，已分析的不重复跑
      batchState.selected = new Set(
        batchState.datasets
          .filter((dataset) => dataset.has_parameter_yaml && !dataset.analyzed)
          .map((dataset) => dataset.path)
      );
      batchState.results = {};
      $('verifyBatchPick').hidden = false;
      $('verifyBatchResult').hidden = true;
      renderBatchDatasets();
      const analyzedCount = batchState.datasets.filter((dataset) => dataset.analyzed).length;
      setBatchStatus(
        `扫描完成：${data.count || 0} 个数据集，可验证 ${batchState.datasets.filter((dataset) => dataset.has_parameter_yaml).length} 个，`
        + `库里已分析 ${analyzedCount} 个（默认跳过，直接读历史结果）。`
        + `本次默认选中 ${batchState.selected.size} 个。`,
        'ok'
      );
    } catch (error) {
      batchState.datasets = [];
      batchState.selected = new Set();
      $('verifyBatchPick').hidden = true;
      $('verifyBatchResult').hidden = true;
      setBatchStatus(`扫描失败：${errorDetail(error && error.message ? error.message : String(error))}`, 'error');
    } finally {
      if (button) { button.disabled = false; button.textContent = '扫描数据集'; }
    }
  }

  async function waitVerifyTask(taskId) {
    for (let index = 0; index < 1200; index += 1) {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      const data = await getJson(`/api/verify/task?id=${encodeURIComponent(taskId)}`);
      const task = data.task || {};
      const progress = data.progress || {};
      setBatchStatus(
        `${progress.step || '验证中'} · ${progress.message || ''}（${Number(progress.percent || 0).toFixed(0)}%）`,
        'busy'
      );
      if (task.status === 'success' || task.status === 'failed') return task;
    }
    return { status: 'failed', error: '批量验证超时' };
  }

  function renderBatchResults() {
    const table = $('verifyBatchResultTable');
    if (!table) return;
    const paths = Object.keys(batchState.results);
    if (!paths.length) {
      $('verifyBatchResult').hidden = true;
      return;
    }
    const header = '<table class="mini-table"><thead><tr>'
      + '<th>数据集</th><th>状态</th><th>来源</th><th>耗时</th><th>操作</th>'
      + '</tr></thead><tbody>';
    const rows = paths.map((path) => {
      const item = batchState.results[path];
      const ok = item.status === 'success';
      const failedText = item.error ? ` title="${esc(item.error)}"` : '';
      return `<tr class="${ok ? '' : 'batch-failed'}">
        <td title="${esc(path)}">${esc(path.split('/').pop())}</td>
        <td${failedText}>${ok ? '成功' : (item.status === 'failed' ? '失败' : '运行中')}</td>
        <td>${ok ? (item.cached ? '<span class="cache-tag">历史</span>' : '<span class="cache-tag is-new">新算</span>') : '—'}</td>
        <td>${item.duration_s ? `${Number(item.duration_s).toFixed(1)} s` : '—'}</td>
        <td>${ok ? `<button class="ghost-button verify-batch-open" type="button" data-path="${esc(path)}">查看/确认图</button>` : '—'}</td>
      </tr>`;
    }).join('');
    table.innerHTML = `${header}${rows}</tbody></table>`;
    table.querySelectorAll('.verify-batch-open').forEach((button) => {
      button.addEventListener('click', () => {
        const input = $('verifyRootInput');
        if (input) input.value = button.getAttribute('data-path');
        // 「查看/确认图」在单日参数验证 sheet 里，先切过去再加载
        if (typeof switchTab === 'function') switchTab('verify');
        loadOverview();
        const panel = $('verifyConfirmPanel');
        if (panel && panel.scrollIntoView) panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    });
    const okCount = paths.filter((path) => batchState.results[path].status === 'success').length;
    setText('verifyBatchResultInfo', `成功 ${okCount} / ${paths.length} 个数据集`);
    $('verifyBatchResult').hidden = false;
  }

  async function runVerifyBatch() {
    if (batchState.busy || !batchState.selected.size) return;
    batchState.busy = true;
    const button = $('verifyBatchRunButton');
    if (button) { button.disabled = true; button.textContent = '批量验证中…'; }
    const reproject = false;
    const forceRefresh = Boolean($('verifyBatchForce') && $('verifyBatchForce').checked);
    const paths = batchState.datasets
      .filter((dataset) => batchState.selected.has(dataset.path))
      .map((dataset) => dataset.path);
    try {
      for (let index = 0; index < paths.length; index += 1) {
        const path = paths[index];
        setBatchStatus(`正在验证 ${index + 1}/${paths.length}：${path}`, 'busy');
        batchState.results[path] = { status: 'running' };
        renderBatchResults();
        try {
          const created = await postJson('/api/verify/run', {
            data_root: path,
            run_data_quality: true,
            run_projection: true,
            enable_pointcloud_reprojection: reproject,
            force_regenerate_framemap: false,
            refresh: forceRefresh,
            frame_start: 100,
            frame_step: 20,
          });
          if (created.cached && created.task) {
            // 库里已有：直接采用历史结果，不重复分析
            const task = created.task;
            batchState.results[path] = {
              status: 'success',
              cached: true,
              duration_s: task.duration_s || 0,
              task_id: created.task_id,
            };
            renderBatchResults();
            continue;
          }
          const task = await waitVerifyTask(created.task_id);
          batchState.results[path] = {
            status: task.status || 'failed',
            error: task.error || '',
            duration_s: task.duration_s || 0,
            task_id: created.task_id,
          };
        } catch (error) {
          batchState.results[path] = {
            status: 'failed',
            error: errorDetail(error && error.message ? error.message : String(error)),
          };
        }
        renderBatchResults();
      }
      const okCount = Object.values(batchState.results).filter((item) => item.status === 'success').length;
      const cachedCount = Object.values(batchState.results).filter((item) => item.cached).length;
      setBatchStatus(
        `批量验证完成：${okCount} / ${paths.length} 个成功`
        + (cachedCount ? `（其中 ${cachedCount} 个直接读历史库）` : '')
        + '。',
        'ok'
      );
    } finally {
      batchState.busy = false;
      if (button) { button.disabled = false; button.textContent = '开始批量验证'; }
      updateBatchPickInfo();
    }
  }

  // ---- 图片放大（灯箱）：单击放大，双击 / Esc 返回，滚轮缩放 + 拖动平移 ----

  function initLightbox() {
    if (document.getElementById('imageLightbox')) return;
    const box = document.createElement('div');
    box.id = 'imageLightbox';
    box.className = 'image-lightbox';
    box.hidden = true;
    box.innerHTML = '<img id="imageLightboxImg" alt="">'
      + '<div class="image-lightbox-hint">滚轮缩放 · 按住拖动 · 双击或按 Esc 返回</div>';
    document.body.appendChild(box);
    const img = box.querySelector('img');
    let scale = 1;
    let tx = 0;
    let ty = 0;
    let dragging = false;
    let startX = 0;
    let startY = 0;

    const apply = () => {
      img.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`;
    };
    const close = () => {
      box.hidden = true;
      img.removeAttribute('src');
      scale = 1;
      tx = 0;
      ty = 0;
      apply();
    };
    const open = (src, alt) => {
      img.src = src;
      img.alt = alt || '';
      box.hidden = false;
      scale = 1;
      tx = 0;
      ty = 0;
      apply();
    };

    box.addEventListener('dblclick', (event) => {
      event.preventDefault();
      event.stopPropagation();
      close();
    });
    box.addEventListener('wheel', (event) => {
      event.preventDefault();
      scale = Math.min(8, Math.max(1, scale * (event.deltaY < 0 ? 1.15 : 0.87)));
      if (scale <= 1.001) { tx = 0; ty = 0; }
      apply();
    }, { passive: false });
    box.addEventListener('mousedown', (event) => {
      if (event.target !== img) return;
      dragging = true;
      startX = event.clientX - tx;
      startY = event.clientY - ty;
      event.preventDefault();
    });
    window.addEventListener('mousemove', (event) => {
      if (!dragging) return;
      tx = event.clientX - startX;
      ty = event.clientY - startY;
      apply();
    });
    window.addEventListener('mouseup', () => { dragging = false; });
    box.addEventListener('click', (event) => {
      if (event.target === box) close();
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !box.hidden) close();
    });
    document.addEventListener('click', (event) => {
      const target = event.target.closest('#sheetVerify img');
      if (!target || target.closest('#imageLightbox')) return;
      event.preventDefault();
      open(target.dataset.full || target.src, target.alt);
    });
  }

  // ---- 库中已有结果（参数验证历史）：单日/批量两个 sheet 各一份 ---------------

  const historyViews = [
    {
      key: 'verify',
      status: 'verifyHistoryStatus',
      table: 'verifyHistoryTable',
      pager: 'verifyHistoryPager',
      pageInfo: 'verifyHistoryPageInfo',
      prev: 'verifyHistoryPrev',
      next: 'verifyHistoryNext',
      query: 'verifyHistoryQuery',
      time: 'verifyHistoryTimeFilter',
      timeStart: 'verifyHistoryTimeStart',
      timeEnd: 'verifyHistoryTimeEnd',
      review: 'verifyHistoryReviewFilter',
    },
    {
      key: 'verifyBatch',
      status: 'verifyBatchHistoryStatus',
      table: 'verifyBatchHistoryTable',
      pager: 'verifyBatchHistoryPager',
      pageInfo: 'verifyBatchHistoryPageInfo',
      prev: 'verifyBatchHistoryPrev',
      next: 'verifyBatchHistoryNext',
      query: 'verifyBatchHistoryQuery',
      time: 'verifyBatchHistoryTimeFilter',
      timeStart: 'verifyBatchHistoryTimeStart',
      timeEnd: 'verifyBatchHistoryTimeEnd',
      review: 'verifyBatchHistoryReviewFilter',
    },
  ];
  const historyState = {};
  historyViews.forEach((view) => {
    historyState[view.key] = { limit: 10, offset: 0, total: 0, busy: false };
  });

  function verifyHistoryDate(name) {
    const text = String(name || '');
    const matches = text.match(/(20\d{6})/g);
    if (matches) return matches[matches.length - 1];
    // 兼容 6 位 YYMMDD（如 134277_260910 -> 20260910）
    const shorts = text.match(/(?<!\d)(\d{6})(?!\d)/g) || [];
    for (let index = shorts.length - 1; index >= 0; index -= 1) {
      const chunk = shorts[index];
      const year = Number(chunk.slice(0, 2));
      const month = Number(chunk.slice(2, 4));
      const day = Number(chunk.slice(4, 6));
      if (year >= 20 && year <= 49 && month >= 1 && month <= 12 && day >= 1 && day <= 31) {
        return `20${chunk}`;
      }
    }
    return '—';
  }

  function renderVerifyReview(review) {
    if (!review) return '<span class="mileage-subtle">—</span>';
    if (review.state === 'pending') {
      return '<span class="confirm-chip">待确认</span>';
    }
    if (review.state === 'partial') {
      const decided = Number(review.passed || 0) + Number(review.rejected || 0);
      return `<span class="confirm-chip">部分确认 ${decided}/${Number(review.total) || 0}</span>`;
    }
    const rejected = Number(review.rejected) || 0;
    const cls = rejected > 0 ? 'is-bad' : 'is-ok';
    return `<span class="confirm-chip ${cls}">通过 ${Number(review.passed) || 0} · 不通过 ${rejected}</span>`;
  }

  // ---- 批量人工确认导航：上一条 / 下一条 / 评完自动跳下一天 -----------------

  const reviewNav = { queue: [], index: -1, viewKey: '', auto: true, waitingDialog: false };

  function updateReviewNav() {
    setText(
      'verifyNavInfo',
      reviewNav.queue.length ? `第 ${reviewNav.index + 1}/${reviewNav.queue.length} 条` : '—'
    );
    const prev = $('verifyPrevButton');
    const next = $('verifyNextButton');
    if (prev) prev.disabled = reviewNav.index <= 0;
    if (next) next.disabled = reviewNav.index + 1 >= reviewNav.queue.length;
  }

  function loadReviewItem(root) {
    const input = $('verifyRootInput');
    if (input) input.value = root;
    if (typeof switchTab === 'function') switchTab('verify');
    loadOverview();
    const panel = $('verifyConfirmPanel');
    if (panel && panel.scrollIntoView) panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function openHistoryDataset(root, viewKey) {
    const viewState = viewKey ? historyState[viewKey] : null;
    if (viewState && Array.isArray(viewState.items) && viewState.items.length) {
      reviewNav.queue = viewState.items.map((item) => item.data_root);
      reviewNav.viewKey = viewKey;
      reviewNav.index = Math.max(0, reviewNav.queue.indexOf(root));
    } else {
      reviewNav.queue = [root];
      reviewNav.viewKey = '';
      reviewNav.index = 0;
    }
    reviewNav.waitingDialog = false;
    updateReviewNav();
    loadReviewItem(root);
  }

  async function goNextReview() {
    if (reviewNav.index < 0 || !reviewNav.queue.length) return;
    if (reviewNav.index + 1 < reviewNav.queue.length) {
      reviewNav.index += 1;
      updateReviewNav();
      loadReviewItem(reviewNav.queue[reviewNav.index]);
      return;
    }
    // 当前页最后一条：自动翻到库里下一页继续
    const view = historyViews.find((item) => item.key === reviewNav.viewKey);
    if (view) {
      const stateForView = historyState[view.key];
      if (stateForView.offset + stateForView.limit < stateForView.total) {
        stateForView.offset += stateForView.limit;
        await loadHistory(view);
        const items = stateForView.items || [];
        if (items.length) {
          reviewNav.queue = items.map((item) => item.data_root);
          reviewNav.index = 0;
          updateReviewNav();
          loadReviewItem(reviewNav.queue[0]);
          return;
        }
      }
    }
    setText('verifyConfirmStatus', '已经是最后一条了');
  }

  function goPrevReview() {
    if (reviewNav.index > 0) {
      reviewNav.index -= 1;
      updateReviewNav();
      loadReviewItem(reviewNav.queue[reviewNav.index]);
    }
  }

  let advanceTimer = null;

  function maybeAdvanceReview() {
    const autoBox = $('verifyAutoNext');
    if (autoBox && !autoBox.checked) return;
    if (!reviewNav.queue.length) return;
    // 只保留一个待执行的自动跳转：对话框关闭 + 保存完成同时触发时会连跳两条
    if (advanceTimer) window.clearTimeout(advanceTimer);
    advanceTimer = window.setTimeout(() => {
      advanceTimer = null;
      goNextReview();
    }, 700);
  }

  function renderHistory(view, items) {
    const state = historyState[view.key];
    const table = $(view.table);
    if (!table) return;
    if (!items.length) {
      table.innerHTML = '<div class="mileage-subtle">库里还没有参数验证结果。</div>';
      setText(view.status, state.total ? `共 ${state.total} 条 · 当前筛选为空` : '库中暂无结果');
      const pager = $(view.pager);
      if (pager) pager.hidden = true;
      return;
    }
    const header = '<table class="mini-table"><thead><tr>'
      + '<th>日期</th><th>地区</th><th>分析时间</th><th>分析状态</th>'
      + '<th>验证时间</th><th>验证状态</th><th>源数据路径</th><th>操作</th>'
      + '</tr></thead><tbody>';
    const rows = items.map((item) => {
      const ok = item.status === 'success';
      const regionCell = item.region
        ? `<span class="cache-tag">${esc(item.region)}${item.vehicle ? ` · ${esc(item.vehicle)}` : ''}</span>`
        : '—';
      return `<tr>
        <td>${esc(item.day || verifyHistoryDate(item.name))}</td>
        <td>${regionCell}</td>
        <td>${esc(item.updated_at || '—')}</td>
        <td>${ok || item.status === 'existing' ? '已完成' : '未完成'}</td>
        <td>${esc(item.verified_at || item.notified_at || '—')}</td>
        <td>${renderVerifyReview(item.review)}</td>
        <td class="history-root" title="${esc(item.data_root || item.name)}"><span class="path-ellipsis">${esc(item.data_root || item.name)}</span></td>
        <td class="verify-history-actions">
          <button class="ghost-button verify-history-open" type="button"
                  data-root="${esc(item.data_root)}">载入确认图</button>
          ${item.delivery_url
            ? `<a class="ghost-button" href="${esc(item.delivery_url)}" title="${esc(item.delivery_name || '')}">下载交付包</a>`
            : (item.review ? `<button class="ghost-button verify-package" type="button" data-root="${esc(item.data_root)}">生成交付包</button>` : '')}
        </td>
      </tr>`;
    }).join('');
    table.innerHTML = `${header}${rows}</tbody></table>`;
    table.querySelectorAll('.verify-history-open').forEach((button) => {
      button.addEventListener('click', () => openHistoryDataset(button.getAttribute('data-root'), view.key));
    });
    table.querySelectorAll('.verify-package').forEach((button) => {
      button.addEventListener('click', async () => {
        const root = button.getAttribute('data-root');
        button.disabled = true;
        button.textContent = '打包中…';
        try {
          const data = await postJson('/api/verify/package', { data_root: root });
          setText(view.status, `已生成交付包：${data.name || ''}（${formatBytes(data.size || 0)}）`);
          await loadHistory(view, true);
          if (window.refreshTodo) window.refreshTodo();
        } catch (error) {
          button.disabled = false;
          button.textContent = '生成交付包';
          setText(view.status, `生成交付包失败：${errorDetail(error && error.message ? error.message : String(error))}`);
        }
      });
    });
    const page = window.MileageCsv && window.MileageCsv.pageState
      ? window.MileageCsv.pageState(state.total, state.limit, state.offset)
      : { pages: 1, page: 1, hasPrev: false, hasNext: false, limit: state.limit };
    setText(
      view.status,
      `库中已有 ${state.total} 条` + (page.pages > 1 ? ` · 第 ${page.page}/${page.pages} 页` : '')
    );
    const pager = $(view.pager);
    if (pager) {
      pager.hidden = page.pages <= 1;
      setText(view.pageInfo, `第 ${page.page}/${page.pages} 页 · 每页 ${page.limit} 条`);
      const prev = $(view.prev);
      const next = $(view.next);
      if (prev) prev.disabled = !page.hasPrev;
      if (next) next.disabled = !page.hasNext;
    }
  }

  // 首屏固定不带筛选（防止浏览器恢复表单导致"筛选没清掉"）
  let verifyHistoryPrimed = false;

  async function loadHistory(view, reset) {
    const state = historyState[view.key];
    if (state.busy) return;
    if (reset === true) state.offset = 0;
    state.busy = true;
    const input = $(view.query);
    const query = verifyHistoryPrimed
      ? String(input && input.value != null ? input.value : '').trim()
      : '';
    // 项目/地区统一用顶部全局筛选
    const region = verifyHistoryPrimed && typeof currentGlobalRegion === 'function'
      ? currentGlobalRegion()
      : '';
    const dateRange = verifyHistoryPrimed && typeof historyDateRange === 'function'
      ? historyDateRange(view.time, view.timeStart, view.timeEnd)
      : { from: '', to: '' };
    const reviewSelect = $(view.review);
    const review = verifyHistoryPrimed
      ? String(reviewSelect && reviewSelect.value != null ? reviewSelect.value : '').trim()
      : '';
    try {
      const data = await getJson(
        `/api/verify/history?limit=${state.limit}`
        + `&offset=${state.offset}&q=${encodeURIComponent(query)}`
        + `&region=${encodeURIComponent(region)}`
        + `&date_from=${encodeURIComponent(dateRange.from)}`
        + `&date_to=${encodeURIComponent(dateRange.to)}`
        + `&review=${encodeURIComponent(review)}`
      );
      state.total = Number(data.total) || 0;
      state.items = data.items || [];
      renderHistory(view, state.items);
    } catch (error) {
      setText(view.status, `读取失败：${errorDetail(error && error.message ? error.message : String(error))}`);
    } finally {
      state.busy = false;
    }
  }

  function loadAllHistory(reset) {
    historyViews.forEach((view) => loadHistory(view, reset));
  }

  function resetVerifyHistoryFilters(reload) {
    verifyHistoryPrimed = false;
    if (typeof clearAllTextInputs === 'function') clearAllTextInputs();
    try {
      localStorage.removeItem('mileage.verifyHistory.verify.query');
      localStorage.removeItem('mileage.verifyHistory.verify.days');
      localStorage.removeItem('mileage.verifyHistory.verify.review');
      localStorage.removeItem('mileage.verifyHistory.verifyBatch.query');
      localStorage.removeItem('mileage.verifyHistory.verifyBatch.days');
      localStorage.removeItem('mileage.verifyHistory.verifyBatch.review');
      localStorage.removeItem('mileage.verifyRoot');
      localStorage.removeItem('mileage.verifyBatchRoot');
    } catch (_) { /* 忽略 */ }
    const clear = () => {
      historyViews.forEach((view) => {
        const queryInput = $(view.query);
        if (queryInput) queryInput.value = '';
        const timeSelect = $(view.time);
        if (timeSelect) timeSelect.value = '';
        const timeStart = $(view.timeStart);
        if (timeStart) timeStart.value = '';
        const timeEnd = $(view.timeEnd);
        if (timeEnd) timeEnd.value = '';
        if (typeof toggleHistoryRange === 'function') {
          toggleHistoryRange(view.time, view.timeStart, view.time.replace('Filter', 'Sep'), view.timeEnd);
        }
        const reviewSelect = $(view.review);
        if (reviewSelect) reviewSelect.value = '';
      });
    };
    clear();
    // 有些浏览器会在 load 之后才恢复表单，延迟再清几次
    [300, 1000, 2000].forEach((delay) => window.setTimeout(() => {
      if (typeof clearAllTextInputs === 'function') clearAllTextInputs();
      clear();
    }, delay));
    if (reload) loadAllHistory(true);
  }

  window.primeVerifyFilters = () => { verifyHistoryPrimed = true; };

  // 浏览器刷新/回退恢复表单后，再清一次，保证默认不带上次筛选
  window.addEventListener('pageshow', (event) => {
    resetVerifyHistoryFilters(Boolean(event.persisted));
  });
  window.addEventListener('load', () => resetVerifyHistoryFilters(false));
  // 按 sheet 单独加载（首屏/切 tab 只请求当前视图）
  window.loadVerifyView = (key) => {
    const view = historyViews.find((item) => item.key === key);
    if (view) loadHistory(view, true);
  };
  // 供页面自动刷新/全局筛选统一刷新两个 sheet 的历史列表（reset=true 回第一页）
  window.refreshVerifyHistories = (reset) => historyViews.forEach((view) => {
    if (reset === true) historyState[view.key].offset = 0;
    loadHistory(view);
  });

  function initHistoryView(view) {
    const queryInput = $(view.query);
    if (queryInput) {
      let timer = null;
      queryInput.addEventListener('input', () => {
        clearTimeout(timer);
        timer = setTimeout(() => loadHistory(view, true), 250);
      });
      queryInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
          event.preventDefault();
          loadHistory(view, true);
        }
      });
    }
    const timeSelect = $(view.time);
    if (timeSelect) {
      timeSelect.addEventListener('change', () => {
        verifyHistoryPrimed = true;
        if (typeof toggleHistoryRange === 'function') {
          toggleHistoryRange(view.time, view.timeStart, view.time.replace('Filter', 'Sep'), view.timeEnd);
        }
        loadHistory(view, true);
      });
    }
    [view.timeStart, view.timeEnd].forEach((id) => {
      const input = $(id);
      if (input) {
        input.addEventListener('change', () => {
          verifyHistoryPrimed = true;
          loadHistory(view, true);
        });
      }
    });
    const reviewSelect = $(view.review);
    if (reviewSelect) {
      reviewSelect.addEventListener('change', () => { verifyHistoryPrimed = true; loadHistory(view, true); });
    }
    const prev = $(view.prev);
    if (prev) {
      prev.addEventListener('click', () => {
        const state = historyState[view.key];
        state.offset = Math.max(0, state.offset - state.limit);
        loadHistory(view);
      });
    }
    const next = $(view.next);
    if (next) {
      next.addEventListener('click', () => {
        const state = historyState[view.key];
        if (state.offset + state.limit < state.total) {
          state.offset += state.limit;
          loadHistory(view);
        }
      });
    }
    // 首屏/切 tab 时才加载（见 window.loadVerifyView），这里不主动请求
  }

  // ---- 结果确认 + 发送飞书：全部评完后两步确认 ----------------------------

  let notifyPending = null;

  function notifyStatsHtml(pending) {
    const total = Number(pending.total) || 0;
    const passed = Number(pending.passed) || 0;
    const rejected = Number(pending.rejected) || 0;
    return ''
      + `<span class="confirm-chip is-ok">通过 ${passed}</span>`
      + `<span class="confirm-chip ${rejected > 0 ? 'is-bad' : ''}">不通过 ${rejected}</span>`
      + `<span class="confirm-chip">合计 ${total}</span>`;
  }

  // 第一步：确认验证结果（这一步不发送）
  function initNotifyDialog() {
    if (document.getElementById('verifyNotifyMask')) return;
    const mask = document.createElement('div');
    mask.id = 'verifyNotifyMask';
    mask.className = 'notify-mask';
    mask.hidden = true;
    mask.innerHTML = `
      <div class="notify-dialog" role="dialog" aria-modal="true" aria-labelledby="verifyNotifyTitle">
        <h4 id="verifyNotifyTitle">验证结果确认</h4>
        <div class="notify-stats" id="verifyNotifyStats"></div>
        <pre class="notify-preview" id="verifyNotifyPreview"></pre>
        <div class="notify-actions">
          <button class="ghost-button" type="button" data-notify="cancel">取消</button>
          <button class="primary-button" type="button" data-notify="confirm">确认</button>
        </div>
        <div class="mileage-status" id="verifyNotifyStatus" role="status" aria-live="polite"></div>
      </div>`;
    document.body.appendChild(mask);
    mask.addEventListener('click', (event) => {
      if (event.target === mask) closeNotifyDialog('已取消（结果已保存，未发送飞书）');
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !mask.hidden) closeNotifyDialog('已取消（结果已保存，未发送飞书）');
    });
    mask.querySelectorAll('[data-notify]').forEach((button) => {
      button.addEventListener('click', () => {
        const action = button.getAttribute('data-notify');
        if (action === 'cancel') {
          closeNotifyDialog('已取消（结果已保存，未发送飞书）');
          return;
        }
        // 确认之后再问一次是否发送飞书；先把本次确认时间记到平台（多次确认取最新）
        recordReview();
        openSendDialog();
      });
    });
  }

  function openNotifyDialog(pending) {
    initNotifyDialog();
    notifyPending = pending || null;
    const mask = $('verifyNotifyMask');
    if (!mask) return;
    $('verifyNotifyStats').innerHTML = notifyStatsHtml(pending);
    $('verifyNotifyPreview').textContent = pending.text || pending.title || '';
    setText('verifyNotifyStatus', '确认后将询问是否把验证结果发送飞书。');
    mask.hidden = false;
  }

  function closeNotifyDialog(message) {
    const mask = $('verifyNotifyMask');
    if (mask) mask.hidden = true;
    notifyPending = null;
    if (message) setText('verifyConfirmStatus', message);
    if (reviewNav.waitingDialog) {
      reviewNav.waitingDialog = false;
      // 这一天评完了：自动跳下一条（可关掉「评完自动下一条」）
      maybeAdvanceReview();
    }
  }

  // 第二步：是否把验证结果发送飞书
  function initSendDialog() {
    if (document.getElementById('verifySendMask')) return;
    const mask = document.createElement('div');
    mask.id = 'verifySendMask';
    mask.className = 'notify-mask';
    mask.hidden = true;
    mask.innerHTML = `
      <div class="notify-dialog" role="dialog" aria-modal="true" aria-labelledby="verifySendTitle">
        <h4 id="verifySendTitle">是否把验证结果发送飞书？</h4>
        <div class="notify-stats" id="verifySendStats"></div>
        <div class="notify-actions">
          <button class="ghost-button" type="button" data-send="cancel">暂不发送</button>
          <button class="primary-button" type="button" data-send="ok">发送飞书</button>
        </div>
        <div class="mileage-status" id="verifySendStatus" role="status" aria-live="polite"></div>
      </div>`;
    document.body.appendChild(mask);
    mask.addEventListener('click', (event) => {
      if (event.target === mask) closeSendDialog('结果已保存，未发送飞书');
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !mask.hidden) closeSendDialog('结果已保存，未发送飞书');
    });
    mask.querySelectorAll('[data-send]').forEach((button) => {
      button.addEventListener('click', () => {
        if (button.getAttribute('data-send') === 'cancel') {
          closeSendDialog('结果已保存，未发送飞书');
          return;
        }
        sendNotify();
      });
    });
  }

  // 「确认」留痕：点确认就把本次验证时间记到平台（同一数据多次确认以最新为准）
  async function recordReview() {
    const root = confirmState.root || '';
    if (!root) return;
    try {
      await postJson('/api/verify/review', { data_root: root });
    } catch (_) { /* 留痕失败不影响确认流程 */ }
  }

  function openSendDialog() {
    const notifyMask = $('verifyNotifyMask');
    if (notifyMask) notifyMask.hidden = true;
    initSendDialog();
    const mask = $('verifySendMask');
    if (!mask) return;
    $('verifySendStats').innerHTML = notifyStatsHtml(notifyPending || {});
    setText('verifySendStatus', '');
    mask.hidden = false;
  }

  function closeSendDialog(message) {
    const mask = $('verifySendMask');
    if (mask) mask.hidden = true;
    notifyPending = null;
    if (message) setText('verifyConfirmStatus', message);
    if (reviewNav.waitingDialog) {
      reviewNav.waitingDialog = false;
      maybeAdvanceReview();
    }
  }

  async function sendNotify() {
    const root = (notifyPending && notifyPending.root) || confirmState.root;
    if (!root) return;
    setText('verifySendStatus', '正在发送飞书…');
    try {
      const data = await postJson('/api/verify/notify', { data_root: root });
      const note = data.notification || {};
      if (note.sent) {
        closeSendDialog(
          `结果已发送飞书（${note.at || ''}）：通过 ${note.passed || 0} · 不通过 ${note.rejected || 0}`
        );
      } else if (note.pending) {
        setText('verifySendStatus', '还有交付图未给出结果，不能发送');
      } else if (note.reason === '结果未变化，无需重复推送') {
        closeSendDialog('结果与上次一致，无需重复发送');
      } else {
        setText('verifySendStatus', `发送失败：${note.reason || '未知原因'}`);
      }
    } catch (error) {
      setText('verifySendStatus', `发送失败：${errorDetail(error && error.message ? error.message : String(error))}`);
    }
  }

  async function notifyCurrent() {
    if (!confirmState.root) {
      setText('verifyConfirmStatus', '先加载交付图');
      return;
    }
    setText('verifyConfirmStatus', '正在发送飞书…');
    try {
      const data = await postJson('/api/verify/notify', { data_root: confirmState.root });
      const note = data.notification || {};
      if (note.sent) {
        setText('verifyConfirmStatus', `已发送飞书（${note.at || ''}）：通过 ${note.passed || 0} · 不通过 ${note.rejected || 0}`);
      } else if (note.pending) {
        setText('verifyConfirmStatus', '还有交付图未给出结果，暂时不能发送');
      } else {
        setText('verifyConfirmStatus', `发送失败：${note.reason || '未知原因'}`);
      }
    } catch (error) {
      setText('verifyConfirmStatus', `发送失败：${errorDetail(error && error.message ? error.message : String(error))}`);
    }
  }

  function updateNotifyButton() {
    const button = $('verifyNotifyButton');
    if (!button) return;
    const totalItems = (confirmState.groups || []).reduce(
      (sum, group) => sum + (group.items || []).length, 0
    );
    let decided = 0;
    (confirmState.groups || []).forEach((group) => {
      (group.items || []).forEach((item) => {
        const status = statusOf(group.key, item.name);
        if (status === 'confirmed' || status === 'rejected') decided += 1;
      });
    });
    button.hidden = !(totalItems > 0 && decided >= totalItems);
  }

  // 强制重新分析：忽略数据库缓存，重跑当前目录（用于历史产出目录丢了/数据更新过）
  async function forceReanalyze(root) {
    const input = $('verifyRootInput');
    if (input && root) input.value = root;
    await runVerify(true);
  }

  // 数据目录已不存在时（历史记录指向被删/挪走的目录）：移除这条无效记录
  async function removeInvalidRecord() {
    const root = confirmState.root || String(($('verifyRootInput') || {}).value || '').trim();
    if (!root) return;
    if (!window.confirm(`确定移除这条无效记录？\n${root}\n（数据目录已不存在，只删数据库记录）`)) return;
    setText('verifyConfirmStatus', '正在移除…');
    try {
      await postJson('/api/verify/remove', { data_root: root });
      setText('verifyConfirmStatus', '已移除无效记录');
      const panel = $('verifyConfirmPanel');
      if (panel) panel.hidden = true;
      confirmState.root = '';
      confirmState.groups = [];
      confirmState.confirmations = {};
      if (window.refreshVerifyHistories) window.refreshVerifyHistories(true);
      if (window.refreshTodo) window.refreshTodo();
    } catch (error) {
      setText('verifyConfirmStatus', `移除失败：${errorDetail(error && error.message ? error.message : String(error))}`);
    }
  }

  function init() {
    const button = $('verifyRunButton');
    if (button) button.addEventListener('click', () => runVerify(false));
    const forceButton = $('verifyForceRunButton');
    if (forceButton) forceButton.addEventListener('click', () => runVerify(true));
    const forceRerunButton = $('verifyForceRerunButton');
    if (forceRerunButton) {
      forceRerunButton.addEventListener('click', () => forceReanalyze(confirmState.root));
    }
    const removeButton = $('verifyRemoveButton');
    if (removeButton) removeButton.addEventListener('click', removeInvalidRecord);
    const input = $('verifyRootInput');
    if (input) {
      input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
          event.preventDefault();
          runVerify();
        }
      });
    }
    const overviewButton = $('verifyOverviewButton');
    if (overviewButton) overviewButton.addEventListener('click', loadOverview);
    const prevReviewButton = $('verifyPrevButton');
    if (prevReviewButton) prevReviewButton.addEventListener('click', goPrevReview);
    const nextReviewButton = $('verifyNextButton');
    if (nextReviewButton) nextReviewButton.addEventListener('click', goNextReview);
    updateReviewNav();

    // 没有「刷新」按钮：交付图确认面板每 60 秒静默自动刷新一次确认状态。
    // 用户正在勾选/灯箱查看/弹窗时不刷，避免打断操作。
    const autoRefreshConfirm = () => {
      if (document.hidden) return;
      const panel = $('verifyConfirmPanel');
      if (!panel || panel.hidden) return;
      if (reviewNav.waitingDialog) return;
      const groups = $('verifyConfirmGroups');
      if (groups && groups.querySelector('.confirm-select:checked')) return;
      loadOverview({ silent: true });
    };
    setInterval(autoRefreshConfirm, 60000);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) autoRefreshConfirm();
    });

    const batchScan = $('verifyBatchScanButton');
    if (batchScan) batchScan.addEventListener('click', scanVerifyBatch);
    const batchInput = $('verifyBatchRootInput');
    if (batchInput) {
      batchInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') { event.preventDefault(); scanVerifyBatch(); }
      });
    }
    const batchReady = $('verifyBatchSelectReady');
    if (batchReady) batchReady.addEventListener('click', () => selectBatchDatasets('ready'));
    const batchAll = $('verifyBatchSelectAll');
    if (batchAll) batchAll.addEventListener('click', () => selectBatchDatasets('all'));
    const batchNone = $('verifyBatchSelectNone');
    if (batchNone) batchNone.addEventListener('click', () => selectBatchDatasets('none'));
    const batchRun = $('verifyBatchRunButton');
    if (batchRun) batchRun.addEventListener('click', runVerifyBatch);

    // 搜索/筛选不保留：刷新/回退后默认清空（空搜索 + 全部时间 + 全部状态）
    resetVerifyHistoryFilters(false);
    // 库中已有结果（单日 + 批量两个 sheet）：筛选 + 翻页
    historyViews.forEach(initHistoryView);

    initLightbox();
    initNotifyDialog();
    bindConfirmEvents();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
