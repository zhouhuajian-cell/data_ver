"""批量里程刷写：给一个父目录，自动遍历下面的日期目录，按天分析并产出汇总报表。

设计
----
- 自动识别日期目录（名字形如 20260916，或任意子目录里能找到轨迹/2M/8M 结构）。
- 每一天独立跑一次里程分析，结果写到该天目录下的 output/mileage/。
- 最后在父目录下生成一份 **汇总 HTML**，列出每天的实际有效里程与覆盖情况。
- 串行执行，避免几百个 pcap 同时解析把机器打满。
"""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import mileage as m

# 日期目录名：20260916 / 20260916_1 / 20260914-2
DATE_DIR_RE = re.compile(r"^(\d{8})(?:[_-].*)?$")


@dataclass
class DayResult:
    """单日的分析结果摘要。"""

    name: str = ""
    path: str = ""
    date: str = ""
    status: str = "pending"
    skipped: bool = False
    skip_reason: str = ""

    total_distance_m: float = 0.0
    gps_stable_distance_m: float = 0.0
    effective_distance_m: float = 0.0
    distance_2m_covered_m: float = 0.0
    distance_8m_covered_m: float = 0.0
    ratio: float = 0.0
    point_count: int = 0
    trip_count: int = 0

    coverage_2m_present: bool = False
    coverage_8m_present: bool = False
    coverage_2m_start: str = ""
    coverage_2m_end: str = ""
    coverage_8m_start: str = ""
    coverage_8m_end: str = ""
    coverage_2m_files: int = 0
    coverage_8m_files: int = 0

    verdict: str = ""
    issues: List[str] = field(default_factory=list)
    report_path: str = ""
    duration_s: float = 0.0


@dataclass
class BatchResult:
    task_id: str = ""
    status: str = "pending"
    root: str = ""
    output_root: str = ""
    days: List[DayResult] = field(default_factory=list)
    summary_path: str = ""
    summary_html_path: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    duration_s: float = 0.0
    error: str = ""
    log: List[str] = field(default_factory=list)

    # 汇总
    total_days: int = 0
    ok_days: int = 0
    failed_days: int = 0
    skipped_days: int = 0
    total_distance_m: float = 0.0
    total_gps_stable_m: float = 0.0
    total_effective_m: float = 0.0


BATCH_TASKS: Dict[str, BatchResult] = {}
_BATCH_LOCK = threading.Lock()
_BATCH_PROGRESS: Dict[str, Dict[str, Any]] = {}


def discover_day_dirs(root: Path, max_depth: int = 4) -> List[Path]:
    """在父目录下找出所有「日期目录」。

    支持两种层级：
      root/20260916
      root/overseas/Europe/RO/20260916   （给更上层目录也能用）
    """
    found: List[Path] = []

    def walk(path: Path, depth: int):
        if depth > max_depth:
            return
        try:
            entries = sorted(p for p in path.iterdir() if p.is_dir())
        except OSError:
            return
        for entry in entries:
            if entry.name in (m.OUTPUT_DIR_NAME, "__pycache__"):
                continue
            if DATE_DIR_RE.match(entry.name):
                found.append(entry)
                continue
            walk(entry, depth + 1)

    walk(root, 0)
    return found


def _quick_check(day_dir: Path) -> Optional[str]:
    """快速判断这天能不能分析；返回 None 表示可以，否则返回跳过原因。"""
    info = m.probe_mileage_dataset(day_dir)
    if not info.get("ready"):
        return "；".join(info.get("problems", [])) or "目录不可用"

    # 只要有 pcap 就要确认里面真的有 GGA（经纬度）。
    # 有些地区的 AP18 只输出二进制 IMU 包，没有定位语句，跑完才知道失败。
    if info.get("pcap_files"):
        try:
            from pcap_gga import has_gga_sample

            if not has_gga_sample(info["pcap_files"]):
                return "AP18 pcap 中不含 GGA 定位语句（只有二进制 IMU 数据），无法计算里程"
        except Exception:
            # 抽查失败不阻断，交给实际分析阶段判断
            pass
    return None


def _to_day_result(day_dir: Path, res: m.MileageAnalysis) -> DayResult:
    dr = DayResult(
        name=day_dir.name,
        path=str(day_dir),
        date=(DATE_DIR_RE.match(day_dir.name).group(1) if DATE_DIR_RE.match(day_dir.name) else day_dir.name),
        status=res.status,
        total_distance_m=res.total_distance_m,
        gps_stable_distance_m=res.gps_stable_distance_m,
        effective_distance_m=res.effective_distance_m,
        distance_2m_covered_m=res.distance_2m_covered_m,
        distance_8m_covered_m=res.distance_8m_covered_m,
        ratio=res.ratio,
        point_count=res.point_count,
        trip_count=res.trip_count,
        verdict=res.verdict,
        issues=list(res.issues),
        duration_s=res.duration_s,
    )
    c2, c8 = res.coverage_2m or {}, res.coverage_8m or {}
    dr.coverage_2m_present = bool(c2.get("present"))
    dr.coverage_8m_present = bool(c8.get("present"))
    dr.coverage_2m_start = c2.get("start") or ""
    dr.coverage_2m_end = c2.get("end") or ""
    dr.coverage_8m_start = c8.get("start") or ""
    dr.coverage_8m_end = c8.get("end") or ""
    dr.coverage_2m_files = c2.get("file_count", 0)
    dr.coverage_8m_files = c8.get("file_count", 0)
    if res.artifacts:
        dr.report_path = str(Path(res.output_root) / res.artifacts[0]["name"])
    return dr


def run_batch(
    task_id: str,
    root: Path,
    output_root: Path,
    selected: Optional[List[str]] = None,
    progress=None,
) -> BatchResult:
    """执行批量分析。

    selected: 只分析这些日期目录名（如 ["20260916","20260917"]）。
              为 None 或空表示全部。
    """
    result = BatchResult(task_id=task_id, root=str(root), output_root=str(output_root))
    result.started_at = time.time()

    def log(msg: str) -> None:
        result.log.append(f"{time.strftime('%H:%M:%S')} {msg}")

    def report(step: str, percent: int, message: str = "") -> None:
        if progress:
            progress(step=step, percent=percent, message=message)

    try:
        output_root.mkdir(parents=True, exist_ok=True)

        report("扫描目录", 3)
        day_dirs = discover_day_dirs(root)
        log(f"发现 {len(day_dirs)} 个日期目录")

        if not day_dirs:
            result.status = "failed"
            result.error = f"在 {root} 下没有找到日期目录（形如 20260916）"
            raise RuntimeError(result.error)

        # 人工选择的日期优先；未指定则全部分析
        if selected:
            wanted = {s.strip() for s in selected if s and s.strip()}
            kept = [d for d in day_dirs if d.name in wanted]
            missing = wanted - {d.name for d in kept}
            if missing:
                log(f"注意：以下日期不存在，已忽略：{', '.join(sorted(missing))}")
            if not kept:
                result.status = "failed"
                result.error = f"选中的日期在 {root} 下都不存在：{', '.join(sorted(wanted))}"
                raise RuntimeError(result.error)
            day_dirs = kept
            log(f"按人工选择分析 {len(day_dirs)} 个目录：{', '.join(d.name for d in day_dirs)}")

        result.total_days = len(day_dirs)

        total = len(day_dirs)
        for idx, day_dir in enumerate(day_dirs, start=1):
            percent = 5 + int(idx / total * 85)
            report(f"分析 {day_dir.name}", percent, f"{idx}/{total}")

            skip_reason = _quick_check(day_dir)
            if skip_reason:
                dr = DayResult(
                    name=day_dir.name, path=str(day_dir), date=day_dir.name,
                    status="skipped", skipped=True, skip_reason=skip_reason,
                    verdict=f"跳过：{skip_reason}",
                )
                result.days.append(dr)
                result.skipped_days += 1
                log(f"[跳过] {day_dir.name}: {skip_reason}")
                continue

            day_out = day_dir / m.OUTPUT_DIR_NAME / "mileage" / f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            try:
                res = m.run_mileage(f"{task_id}-{day_dir.name}", day_dir, day_out)
                dr = _to_day_result(day_dir, res)
                result.days.append(dr)
                if dr.status == "success":
                    result.ok_days += 1
                    if dr.effective_distance_m <= 0:
                        dr.issues.append("GPS 稳定里程中没有落在 2M/8M 录制时段的里程")
                else:
                    result.failed_days += 1
                log(f"[完成] {day_dir.name}: 有效 {dr.effective_distance_m/1000:.3f} km")
            except Exception as exc:
                msg = str(exc)
                # 「包内没有 GGA / 没有轨迹点」属于数据本身不具备里程条件，
                # 归为跳过而不是失败，避免整批看起来全红。
                if "GGA" in msg or "未解析到有效轨迹点" in msg:
                    dr = DayResult(
                        name=day_dir.name, path=str(day_dir), date=day_dir.name,
                        status="skipped", skipped=True, skip_reason=msg,
                        verdict=f"跳过：{msg}",
                    )
                    result.days.append(dr)
                    result.skipped_days += 1
                    log(f"[跳过] {day_dir.name}: {msg}")
                else:
                    dr = DayResult(
                        name=day_dir.name, path=str(day_dir), date=day_dir.name,
                        status="failed", verdict=f"分析失败：{msg}",
                    )
                    dr.issues.append(msg)
                    result.days.append(dr)
                    result.failed_days += 1
                    log(f"[失败] {day_dir.name}: {msg}")

        # 汇总
        report("生成汇总", 93)
        for dr in result.days:
            result.total_distance_m += dr.total_distance_m
            result.total_gps_stable_m += dr.gps_stable_distance_m
            result.total_effective_m += dr.effective_distance_m

        result.summary_path = str(output_root / "batch_summary.csv")
        _write_summary_csv(result)

        result.summary_html_path = str(output_root / "batch_summary.html")
        Path(result.summary_html_path).write_text(build_batch_html(result), encoding="utf-8")

        result.status = "success"
        report("完成", 100, f"{result.ok_days} 天完成")
        log(f"批量完成：{result.ok_days} 天成功，{result.failed_days} 天失败，{result.skipped_days} 天跳过")

    except Exception as exc:
        if result.status != "failed":
            result.status = "failed"
            result.error = f"{type(exc).__name__}: {exc}"
        log(f"批量分析失败：{result.error}")

    result.finished_at = time.time()
    result.duration_s = round(result.finished_at - result.started_at, 2)
    return result


def _write_summary_csv(result: BatchResult) -> None:
    import csv

    path = Path(result.summary_path)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow([
            "日期", "状态", "全部轨迹(km)", "GPS稳定(km)", "实际有效(km)", "有效率",
            "轨迹点数", "行程数", "2M有数据", "8M有数据", "2M文件数", "8M文件数",
            "说明",
        ])
        for d in result.days:
            w.writerow([
                d.name, d.status,
                round(d.total_distance_m / 1000, 3),
                round(d.gps_stable_distance_m / 1000, 3),
                round(d.effective_distance_m / 1000, 3),
                f"{d.ratio:.2f}%",
                d.point_count, d.trip_count,
                "是" if d.coverage_2m_present else "否",
                "是" if d.coverage_8m_present else "否",
                d.coverage_2m_files, d.coverage_8m_files,
                d.verdict,
            ])
        w.writerow([])
        w.writerow(["合计", "",
                    round(result.total_distance_m / 1000, 3),
                    round(result.total_gps_stable_m / 1000, 3),
                    round(result.total_effective_m / 1000, 3),
                    f"{(result.total_effective_m / result.total_gps_stable_m * 100) if result.total_gps_stable_m else 0:.2f}%"])


def build_batch_html(result: BatchResult) -> str:
    """生成批量汇总 HTML。"""
    import html as _html

    def e(v) -> str:
        return _html.escape(str(v if v is not None else ""))

    def km(val: float) -> str:
        return f"{val / 1000:,.3f}"

    rows = ""
    for d in result.days:
        if d.status == "skipped":
            badge = "<span class='pill gray'>跳过</span>"
        elif d.status == "failed":
            badge = "<span class='pill bad'>失败</span>"
        elif d.effective_distance_m > 0:
            badge = "<span class='pill ok'>完成</span>"
        else:
            badge = "<span class='pill warn'>无有效里程</span>"

        c2 = "<span class='pill ok'>有</span>" if d.coverage_2m_present else "<span class='pill bad'>无</span>"
        c8 = "<span class='pill ok'>有</span>" if d.coverage_8m_present else "<span class='pill bad'>无</span>"

        link = f"<a href='file://{e(d.report_path)}'>明细</a>" if d.report_path else "—"

        rows += (
            f"<tr><td><a href='file://{e(d.path)}'>{e(d.name)}</a></td><td>{badge}</td>"
            f"<td class='num'>{km(d.total_distance_m)}</td>"
            f"<td class='num'>{km(d.gps_stable_distance_m)}</td>"
            f"<td class='num'><strong>{km(d.effective_distance_m)}</strong></td>"
            f"<td class='num'>{d.ratio:.2f}%</td>"
            f"<td class='num'>{d.point_count:,}</td>"
            f"<td class='num'>{d.coverage_2m_files:,}</td><td>{c2}</td>"
            f"<td class='num'>{d.coverage_8m_files:,}</td><td>{c8}</td>"
            f"<td class='hint'>{e(d.verdict[:60])}</td>"
            f"<td>{link}</td></tr>"
        )

    overall_ratio = (result.total_effective_m / result.total_gps_stable_m * 100) if result.total_gps_stable_m else 0
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>批量里程汇总 · {e(result.root)}</title>
<style>
:root{{--bg:#f4f6f8;--panel:#fff;--text:#18212b;--muted:#667481;--line:#dfe5ea;--green:#16845b;--orange:#d97706;--red:#c43d3d;--blue:#276ef1}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.6 "Microsoft YaHei",Arial,sans-serif}}
.wrap{{max-width:1280px;margin:0 auto;padding:28px}}
h1{{font-size:25px;margin:0 0 4px}} .sub{{color:var(--muted);margin-bottom:20px;word-break:break-all}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px;margin-bottom:16px;box-shadow:0 4px 16px rgba(22,31,39,.05)}}
.card h2{{font-size:17px;margin:0 0 14px}}
.stats{{display:flex;gap:14px;flex-wrap:wrap}}
.stat{{flex:1 1 180px;border:1px solid var(--line);border-radius:10px;padding:14px}}
.stat .lb{{color:var(--muted);font-size:12px}} .stat .vl{{font-size:24px;font-weight:600;margin-top:2px}}
.big{{font-size:30px;color:var(--green)}}
table{{width:100%;border-collapse:collapse;margin-top:6px}}
th,td{{padding:9px 10px;border-bottom:1px solid var(--line);text-align:left;font-size:13px}}
th{{color:var(--muted);background:#f8fafb;font-weight:600;white-space:nowrap}}
.num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
.pill{{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;font-weight:600;white-space:nowrap}}
.ok{{background:#e6f6ee;color:var(--green)}} .bad{{background:#fdeaea;color:var(--red)}}
.warn{{background:#fff4e0;color:var(--orange)}} .gray{{background:#eef2f5;color:var(--muted)}}
.hint{{color:var(--muted);font-size:12px}} a{{color:var(--blue);text-decoration:none}} a:hover{{text-decoration:underline}}
.footer{{color:var(--muted);font-size:12px;margin:22px 0 6px}}
</style></head><body><div class="wrap">

<h1>批量里程汇总</h1>
<div class="sub">根目录：{e(result.root)}<br>生成时间：{generated}　总耗时 {result.duration_s:.1f} 秒</div>

<div class="card">
  <div class="stats">
    <div class="stat"><div class="lb">实际有效里程合计</div><div class="vl big">{km(result.total_effective_m)} km</div><div class="hint">占 GPS 稳定里程 {overall_ratio:.2f}%</div></div>
    <div class="stat"><div class="lb">GPS 稳定里程合计</div><div class="vl">{km(result.total_gps_stable_m)} km</div></div>
    <div class="stat"><div class="lb">全部轨迹长度</div><div class="vl">{km(result.total_distance_m)} km</div></div>
    <div class="stat"><div class="lb">目录数</div><div class="vl">{result.total_days}</div>
      <div class="hint">完成 {result.ok_days}　失败 {result.failed_days}　跳过 {result.skipped_days}</div></div>
  </div>
</div>

<div class="card">
  <h2>按天明细</h2>
  <table><thead><tr>
    <th>日期目录</th><th>状态</th>
    <th class="num">轨迹 (km)</th><th class="num">GPS稳定 (km)</th><th class="num">实际有效 (km)</th><th class="num">有效率</th>
    <th class="num">轨迹点</th>
    <th class="num">2M文件</th><th>2M</th><th class="num">8M文件</th><th>8M</th>
    <th>说明</th><th>明细</th>
  </tr></thead><tbody>
  {rows or '<tr><td colspan="13" class="hint">没有可分析的数据</td></tr>'}
  </tbody></table>
</div>

<div class="card">
  <h2>口径</h2>
  <div class="hint">
    · <b>实际有效里程</b> = 该段轨迹 GPS 稳定 <b>且</b> 当时 2M 或 8M 相机正在录制 的里程。<br>
    · 2M/8M 是传感器，本身没有「里程」概念，只提供录制时段（设备每组约 60 秒）。<br>
    · GPS 稳定判定：GGA 定位质量为 1/2/4/5；轨迹来自 AP18 pcap 时自动解析其中的 GGA 语句。
  </div>
</div>

<div class="footer">每天的单独报告保存在对应日期目录的 output/mileage/ 下。</div>
</div></body></html>"""


def preview_day_dirs(root: Path) -> List[Dict[str, Any]]:
    """扫描日期目录并预检，供前端展示勾选列表。

    返回每项：name / path / analyzable / reason / 关键数据概览。
    """
    items: List[Dict[str, Any]] = []
    for day_dir in discover_day_dirs(root):
        info = m.probe_mileage_dataset(day_dir)
        analyzable = bool(info.get("ready"))
        reason = "" if analyzable else "；".join(info.get("problems", [])) or "目录不可用"

        # 预检：只要有 pcap 就要确认里面真的有 GGA，否则跑起来会失败
        if analyzable and info.get("pcap_files"):
            try:
                from pcap_gga import has_gga_sample

                if not has_gga_sample(info["pcap_files"]):
                    analyzable = False
                    reason = "AP18 pcap 中不含 GGA 定位语句（只有二进制 IMU 数据），无法计算里程"
            except Exception:
                pass

        item: Dict[str, Any] = {
            "name": day_dir.name,
            "path": str(day_dir),
            "date": (DATE_DIR_RE.match(day_dir.name).group(1)
                     if DATE_DIR_RE.match(day_dir.name) else day_dir.name),
            "analyzable": analyzable,
            "reason": reason,
            "trajectory_files": len(info.get("trajectory_files", [])),
            "pcap_files": len(info.get("pcap_files", [])),
            "device_2m_files": info.get("device_2m", {}).get("files", 0),
            "device_8m_files": info.get("device_8m", {}).get("files", 0),
            "coverage_2m": bool(info.get("device_2m", {}).get("files")),
            "coverage_8m": bool(info.get("device_8m", {}).get("files")),
        }
        items.append(item)
    return items


def create_batch_task(
    root: Path,
    output_root: Path,
    selected: Optional[List[str]] = None,
) -> str:
    task_id = uuid.uuid4().hex[:12]
    with _BATCH_LOCK:
        BATCH_TASKS[task_id] = BatchResult(task_id=task_id, status="queued", root=str(root), output_root=str(output_root))
    _BATCH_PROGRESS[task_id] = {"status": "queued", "percent": 0, "step": "排队中"}

    def _progress(**kw):
        state = _BATCH_PROGRESS.setdefault(task_id, {})
        state.update(kw)
        state["status"] = "running"
        state["updated_at"] = time.time()

    def _run():
        try:
            res = run_batch(task_id, root, output_root, selected=selected, progress=_progress)
        except Exception as exc:
            with _BATCH_LOCK:
                t = BATCH_TASKS[task_id]
                t.status = "failed"
                t.error = str(exc)
            _BATCH_PROGRESS[task_id] = {"status": "failed", "message": str(exc), "percent": 0}
            return
        with _BATCH_LOCK:
            BATCH_TASKS[task_id] = res
        _BATCH_PROGRESS[task_id] = {
            "status": res.status, "percent": 100, "step": "完成",
            "message": f"{res.ok_days} 天完成", "updated_at": time.time(),
        }

    threading.Thread(target=_run, name=f"batch-{task_id}", daemon=True).start()
    return task_id


def get_batch_task(task_id: str) -> Optional[BatchResult]:
    return BATCH_TASKS.get(task_id)


def get_batch_progress(task_id: str) -> Dict[str, Any]:
    return dict(_BATCH_PROGRESS.get(task_id, {}))


def list_batch_tasks() -> List[Dict[str, Any]]:
    with _BATCH_LOCK:
        items = list(BATCH_TASKS.values())
    items.sort(key=lambda r: r.started_at or 0, reverse=True)
    return [
        {
            "task_id": r.task_id, "status": r.status, "root": r.root,
            "total_days": r.total_days, "ok_days": r.ok_days,
            "effective_km": round(r.total_effective_m / 1000, 3),
            "started_at": r.started_at, "duration_s": r.duration_s,
        }
        for r in items
    ]
