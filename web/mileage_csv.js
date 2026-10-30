/**
 * 有效里程导出（CSV）。
 *
 * 这里只做纯数据转换，不碰 DOM，方便 node 单测；下载动作在 mileage.js 里。
 * CSV 用逗号分隔、CRLF 换行；下载时统一加 UTF-8 BOM，Excel 打开中文不乱码。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.MileageCsv = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  function cell(value) {
    if (value == null) return '';
    const text = String(value);
    if (/[",\r\n]/.test(text)) return `"${text.replace(/"/g, '""')}"`;
    return text;
  }

  function toCsv(rows) {
    return rows.map((row) => row.map(cell).join(',')).join('\r\n');
  }

  function km(meters) {
    const value = Number(meters);
    return Number.isFinite(value) ? (value / 1000).toFixed(3) : '';
  }

  function num(value, digits) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return '';
    return digits == null ? String(parsed) : parsed.toFixed(digits);
  }

  function pct(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed.toFixed(1) : '';
  }

  function seconds(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed.toFixed(1) : '';
  }

  function verifyStateText(verify) {
    const state = (verify || {}).state || 'unverified';
    if (state === 'passed') return '通过';
    if (state === 'failed') return '不通过';
    return '未完成';
  }

  function verifyUsableText(verify) {
    const state = (verify || {}).state || 'unverified';
    if (state === 'passed') return '可用';
    if (state === 'failed') return '不可用';
    return '待验证';
  }

  /** 单日结果 → CSV 文本（不含 BOM）。 */
  function buildSingleCsv(data) {
    const result = data || {};
    const mileage = result.mileage || {};
    const trajectory = result.trajectory || {};
    const coverage = result.coverage || {};
    const rows = [
      ['字段', '值'],
      ['目录', result.data_root || ''],
      ['数据来源', result.cached ? '数据库缓存' : '本次计算'],
      ['计算时间', result.computed_at || ''],
      ['参数验证', verifyStateText(result.verify)],
      ['当天里程可用', verifyUsableText(result.verify)],
      ['用时(s)', seconds(result.elapsed_s)],
      ['有效里程(km)', km(mileage.effective_m != null ? mileage.effective_m : mileage.both_m)],
      ['GPS覆盖里程(km)', km(mileage.gps_stable_m)],
      ['2M覆盖里程(km)', km(mileage.covered_2m_m)],
      ['8M覆盖里程(km)', km(mileage.covered_8m_m)],
      ['任一覆盖里程(km)', km(mileage.any_m)],
      ['无覆盖里程(km)', km(mileage.no_cover_m)],
      ['总轨迹长度(km)', km(mileage.total_m)],
      ['有效里程率(%)', pct(mileage.effective_ratio_pct != null ? mileage.effective_ratio_pct : mileage.both_ratio_pct)],
      ['GPS覆盖段数', num(mileage.stable_segment_count)],
      ['轨迹点数', num(trajectory.point_count)],
      ['轨迹开始时间', trajectory.start_time || ''],
      ['轨迹结束时间', trajectory.end_time || ''],
      ['设备时区(UTC小时)', num(coverage.device_timezone_offset_hours)],
      [],
      ['覆盖明细', '文件数', '容量(GB)', '开始时间', '结束时间', '时长(s)', '区段数', '占位文件(已忽略)'],
    ];
    [['2M', coverage['2m']], ['8M', coverage['8m']]].forEach(([label, item]) => {
      const info = item || {};
      rows.push([
        label,
        info.present ? num(info.file_count) : '未找到',
        info.present ? (Number(info.bytes || 0) / 1024 / 1024 / 1024).toFixed(2) : '',
        info.start || '',
        info.end || '',
        info.present ? seconds(info.duration_s) : '',
        info.present ? num(info.interval_count) : '',
        num(info.ignored_small_files),
      ]);
    });
    const warnings = Array.isArray(result.warnings) ? result.warnings : [];
    const issues = Array.isArray(result.issues) ? result.issues : [];
    if (warnings.length) {
      rows.push([], ['提示', '']);
      warnings.forEach((item) => rows.push(['', item]));
    }
    if (issues.length) {
      rows.push([], ['问题', '']);
      issues.forEach((item) => rows.push(['', item]));
    }
    return toCsv(rows);
  }

  /** 批量结果 → CSV 文本（不含 BOM）。 */
  function buildBatchCsv(results, batchRoot) {
    const list = Array.isArray(results) ? results : [];
    const rows = [
      ['日期', '目录', '有效里程(km)', 'GPS覆盖(km)', '2M覆盖(km)', '8M覆盖(km)',
        '任一覆盖(km)', '无覆盖(km)', '有效里程率(%)', '参数验证', '里程可用', '轨迹点数', '计算时间', '来源', '提示/问题'],
    ];
    let count = 0;
    const totals = { effective: 0, gps: 0, c2m: 0, c8m: 0, any: 0, no_cover: 0, points: 0 };
    list.forEach((day) => {
      if (!day || !day.ok) {
        rows.push([day && day.date ? day.date : '', day && day.path ? day.path : '', '失败', '', '', '', '', '', '', '', verifyStateText(day && day.verify), verifyUsableText(day && day.verify), '', '', '', day && day.error ? day.error : '']);
        return;
      }
      const mileage = day.mileage || {};
      const trajectory = day.trajectory || {};
      const notes = []
        .concat(Array.isArray(day.issues) ? day.issues : [])
        .concat(Array.isArray(day.warnings) ? day.warnings : [])
        .join('；');
      const effective = Number(mileage.effective_m != null ? mileage.effective_m : mileage.both_m) || 0;
      rows.push([
        day.date || '',
        day.path || '',
        km(effective),
        km(mileage.gps_stable_m),
        km(mileage.covered_2m_m),
        km(mileage.covered_8m_m),
        km(mileage.any_m),
        km(mileage.no_cover_m),
        pct(mileage.effective_ratio_pct != null ? mileage.effective_ratio_pct : mileage.both_ratio_pct),
        verifyStateText(day.verify),
        verifyUsableText(day.verify),
        num(trajectory.point_count),
        day.computed_at || '',
        day.cached ? '缓存' : '新算',
        notes,
      ]);
      count += 1;
      totals.effective += effective;
      totals.gps += Number(mileage.gps_stable_m) || 0;
      totals.c2m += Number(mileage.covered_2m_m) || 0;
      totals.c8m += Number(mileage.covered_8m_m) || 0;
      totals.any += Number(mileage.any_m) || 0;
      totals.no_cover += Number(mileage.no_cover_m) || 0;
      totals.points += Number(trajectory.point_count) || 0;
    });
    rows.push([
      `合计（${count} 天成功）`, batchRoot || '', km(totals.effective), km(totals.gps),
      km(totals.c2m), km(totals.c8m), km(totals.any), km(totals.no_cover), '', '', '', num(totals.points), '', '', '',
    ]);
    return toCsv(rows);
  }

  /** 历史列表（库中已有结果）→ CSV 文本：按当前筛选条件导出的全部记录。 */
  function buildHistoryCsv(items, title) {
    const list = Array.isArray(items) ? items : [];
    const rows = [
      ['日期', '地区', '车型', '有效里程(km)', 'GPS覆盖(km)', '2M覆盖(km)', '8M覆盖(km)',
        '任一覆盖(km)', '无覆盖(km)', '有效里程率(%)', '参数验证', '里程可用', '计算时间', '数据路径'],
    ];
    const totals = { effective: 0, gps: 0, c2m: 0, c8m: 0, any: 0, no_cover: 0 };
    list.forEach((item) => {
      const effective = Number(item.effective_m) || 0;
      rows.push([
        item.day || '',
        item.region || '',
        item.vehicle || '',
        km(effective),
        km(item.gps_stable_m),
        km(item.covered_2m_m),
        km(item.covered_8m_m),
        km(item.any_m),
        km(item.no_cover_m),
        pct(item.effective_ratio_pct),
        verifyStateText(item.verify),
        verifyUsableText(item.verify),
        item.computed_at || '',
        item.data_root || '',
      ]);
      totals.effective += effective;
      totals.gps += Number(item.gps_stable_m) || 0;
      totals.c2m += Number(item.covered_2m_m) || 0;
      totals.c8m += Number(item.covered_8m_m) || 0;
      totals.any += Number(item.any_m) || 0;
      totals.no_cover += Number(item.no_cover_m) || 0;
    });
    rows.push([
      `合计（${list.length} 条）`, '', '', km(totals.effective), km(totals.gps),
      km(totals.c2m), km(totals.c8m), km(totals.any), km(totals.no_cover), '', '', '', '', title || '',
    ]);
    return toCsv(rows);
  }

  /** 历史列表分页：返回当前页/总页数与前后页可用状态。 */
  function pageState(total, limit, offset) {
    const totalCount = Math.max(0, Math.floor(Number(total) || 0));
    const pageSize = Math.max(1, Math.floor(Number(limit) || 10));
    const rawOffset = Math.max(0, Math.floor(Number(offset) || 0));
    const pages = Math.max(1, Math.ceil(totalCount / pageSize));
    const page = Math.min(pages, Math.floor(rawOffset / pageSize) + 1);
    const safeOffset = Math.min(rawOffset, (pages - 1) * pageSize);
    return {
      total: totalCount,
      limit: pageSize,
      offset: safeOffset,
      pages,
      page,
      hasPrev: safeOffset > 0,
      hasNext: safeOffset + pageSize < totalCount,
    };
  }

  return { toCsv, buildSingleCsv, buildBatchCsv, buildHistoryCsv, pageState };
});
