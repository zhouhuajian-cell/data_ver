/**
 * 有效里程 CSV 导出：纯函数单测（不需要浏览器/服务端）。
 */
const fs = require('fs');
const path = require('path');

const csv = require(path.join(__dirname, '..', 'web', 'mileage_csv.js'));

let failures = 0;
function check(ok, label, detail) {
  if (ok) {
    console.log(`  [ OK ] ${label}`);
  } else {
    failures += 1;
    console.log(`  [FAIL] ${label}${detail ? ` - ${detail}` : ''}`);
  }
}

function lines(text) {
  return text.split('\r\n');
}

function main() {
  console.log('[1] 单日导出');
  const single = {
    data_root: '\\\\192.168.20.206\\logs_new\\020_E5-001\\Russia\\Overseas\\9\\20260923',
    cached: false,
    computed_at: '2026-10-21 19:01:47',
    elapsed_s: 21.6,
    mileage: {
      effective_m: 45074.713,
      both_m: 45074.713,
      gps_stable_m: 45783.733,
      covered_2m_m: 45153.477,
      covered_8m_m: 45074.713,
      any_m: 45153.477,
      no_cover_m: 630.256,
      total_m: 46000,
      effective_ratio_pct: 98.5,
      stable_segment_count: 141563,
    },
    trajectory: {
      point_count: 224866,
      start_time: '2026-09-10 15:54:47',
      end_time: '2026-09-10 17:10:53',
    },
    coverage: {
      device_timezone_offset_hours: 8,
      '2m': { present: true, file_count: 46355, bytes: 32540000000, start: '2026-09-10 15:53:14',
        end: '2026-09-10 17:10:14', duration_s: 4620, interval_count: 154, ignored_small_files: 2 },
      '8m': { present: false, file_count: 0, ignored_small_files: 4424 },
    },
    warnings: ['轨迹点 449,731 个，按 1/2 抽稀'],
    issues: ['8M 目录里只有占位文件、没有真实数据'],
  };
  const text = csv.buildSingleCsv(single);
  const rows = lines(text);
  const flat = rows.join('\n');
  check(flat.includes('字段,值'), '带表头');
  check(flat.includes('有效里程(km),45.075'), '有效里程按 km 保留 3 位', flat.slice(0, 200));
  check(flat.includes('GPS稳定里程(km),45.784'), 'GPS 稳定里程');
  check(flat.includes('数据来源,本次计算'), '数据来源');
  check(flat.includes('覆盖明细'), '带覆盖明细段');
  check(flat.includes('2M,46355,30.31'), '2M 明细（容量 GB）', rows.find((r) => r.startsWith('2M,')));
  check(flat.includes('8M,未找到'), '8M 缺失标记');
  check(flat.includes('占位文件(已忽略)'), '占位文件列');
  check(flat.includes('提示'), '带提示段');
  check(flat.includes('问题'), '带问题段');
  check(rows.some((row) => row.startsWith('2M,') && row.includes('30.31')), '容量换算 30.31 GB', '');
  check(text.includes('\r\n'), '使用 CRLF 换行');
  check(!text.includes('\ufeff'), '纯函数不带 BOM（下载时再加）');

  console.log('[2] 批量导出');
  const results = [
    { date: '20260910', path: '/mnt/logs_new/a/20260910', ok: true, cached: true,
      computed_at: '2026-10-21 19:01:47',
      mileage: { effective_m: 45074.713, gps_stable_m: 45783, covered_2m_m: 45153, covered_8m_m: 45074,
        any_m: 45153, no_cover_m: 630, effective_ratio_pct: 98.5 },
      trajectory: { point_count: 224866 }, warnings: [], issues: [] },
    { date: '20260911', path: '/mnt/logs_new/a/20260911', ok: false, error: '目录不存在' },
  ];
  const batch = csv.buildBatchCsv(results, '/mnt/logs_new/a');
  const batchRows = lines(batch);
  check(batch.includes('日期,目录,有效里程(km)'), '批量表头');
  check(batch.includes('20260910,/mnt/logs_new/a/20260910,45.075'), '批量数据行');
  check(batch.includes('20260911,/mnt/logs_new/a/20260911,失败'), '失败行');
  check(batch.includes('缓存'), '来源列');
  check(batchRows[batchRows.length - 1].startsWith('合计（1 天成功）'), '合计行', batchRows[batchRows.length - 1]);
  check(batchRows[batchRows.length - 1].includes('45.075'), '合计有效里程');

  console.log('[3] 转义');
  const escaped = csv.toCsv([['a,b', 'say "hi"', 'line1\nline2']]);
  check(escaped === '"a,b","say ""hi""","line1\nline2"', '逗号/引号/换行都转义', escaped);

  console.log('[4] 历史分页');
  const p1 = csv.pageState(7, 10, 0);
  check(p1.pages === 1 && p1.page === 1 && !p1.hasPrev && !p1.hasNext, '7 条不足一页');
  const p2 = csv.pageState(23, 10, 10);
  check(p2.pages === 3 && p2.page === 2 && p2.hasPrev && p2.hasNext, '23 条第 2 页', JSON.stringify(p2));
  const p3 = csv.pageState(23, 10, 25);
  check(p3.offset === 20 && p3.page === 3 && !p3.hasNext, '越界 offset 收敛到最后一页', JSON.stringify(p3));
  const p4 = csv.pageState(0, 10, 0);
  check(p4.pages === 1 && p4.total === 0, '空列表');

  if (failures) {
    console.log(`\n结果：${failures} 项失败`);
    process.exitCode = 1;
  } else {
    console.log('\n结果：全部通过');
  }
}

main();
