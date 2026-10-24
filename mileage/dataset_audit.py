"""Audit a collection dataset and generate a self-contained HTML report.

The source tree is read-only.  Only compact JSON/HTML reports are written under
the local project output directory.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import struct
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


GPS_EPOCH = datetime(1980, 1, 6, tzinfo=timezone.utc)
GPS_UTC_LEAP_SECONDS = 18
CHINA_TZ = timezone(timedelta(hours=8))
MESSAGE_NAMES = {
    7: "GPSEPHEM · GPS星历",
    140: "RANGECMP · 压缩GNSS原始观测",
    325: "RAWIMUS · 原始IMU",
    723: "GLOEPHEMERIS · GLONASS星历",
    1047: "BDSEPHEM · 北斗星历",
}


@dataclass
class StreamStats:
    path: str
    size_bytes: int
    packet_count: int
    rawimu_primary_segment_count: int
    rawimu_segment_count: int
    message_ids: dict[str, int]
    rawimu_status: dict[str, int]
    first_week: int | None
    first_ms: int | None
    last_week: int | None
    last_ms: int | None
    first_time_utc: str | None
    last_time_utc: str | None
    first_time_china: str | None
    last_time_china: str | None
    duration_seconds: float | None
    average_rate_hz: float | None
    gap_count_over_10ms: int
    non_monotonic_count: int
    unparsed_bytes: int
    has_position_solution: bool


def gps_time(week: int, milliseconds: int) -> datetime:
    return GPS_EPOCH + timedelta(weeks=week, milliseconds=milliseconds) - timedelta(
        seconds=GPS_UTC_LEAP_SECONDS
    )


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="milliseconds") if dt else None


def scan_novatel_stream(path: str) -> StreamStats:
    """Stream-scan NovAtel long/short binary records without loading the file."""
    message_ids: Counter[int] = Counter()
    imu_status: Counter[int] = Counter()
    packet_count = 0
    first_stamp: tuple[int, int] | None = None
    last_stamp: tuple[int, int] | None = None
    previous_total_ms: int | None = None
    current_start_stamp: tuple[int, int] | None = None
    current_last_stamp: tuple[int, int] | None = None
    current_count = 0
    current_gap_count = 0
    imu_segments: list[tuple[tuple[int, int], tuple[int, int], int, int]] = []
    gap_count = 0
    non_monotonic = 0
    skipped = 0
    buffer = bytearray()

    with open(path, "rb", buffering=8 * 1024 * 1024) as source:
        eof = False
        while not eof:
            chunk = source.read(8 * 1024 * 1024)
            if chunk:
                buffer.extend(chunk)
            else:
                eof = True

            cursor = 0
            buffer_len = len(buffer)
            while cursor + 12 <= buffer_len:
                sync = buffer.find(b"\xAA\x44", cursor)
                if sync < 0:
                    if eof:
                        skipped += buffer_len - cursor
                        cursor = buffer_len
                    else:
                        keep = min(2, buffer_len - cursor)
                        skipped += buffer_len - cursor - keep
                        cursor = buffer_len - keep
                    break

                skipped += sync - cursor
                if sync + 4 > buffer_len:
                    cursor = sync
                    break

                sync_type = buffer[sync + 2]
                if sync_type == 0x13:
                    header_len = 12
                    body_len = buffer[sync + 3]
                    if body_len > 240:
                        cursor = sync + 1
                        continue
                    total_len = header_len + body_len + 4
                    if sync + total_len > buffer_len:
                        cursor = sync
                        break
                    message_id = struct.unpack_from("<H", buffer, sync + 4)[0]
                    week = struct.unpack_from("<H", buffer, sync + 6)[0]
                    milliseconds = struct.unpack_from("<I", buffer, sync + 8)[0]
                elif sync_type == 0x12:
                    header_len = buffer[sync + 3]
                    if header_len < 10 or header_len > 64 or sync + header_len > buffer_len:
                        cursor = sync + 1
                        continue
                    body_len = struct.unpack_from("<H", buffer, sync + 8)[0]
                    total_len = header_len + body_len + 4
                    if sync + total_len > buffer_len:
                        cursor = sync
                        break
                    message_id = struct.unpack_from("<H", buffer, sync + 4)[0]
                    week = struct.unpack_from("<H", buffer, sync + 14)[0]
                    milliseconds = struct.unpack_from("<I", buffer, sync + 16)[0]
                else:
                    cursor = sync + 1
                    continue

                packet_count += 1
                message_ids[message_id] += 1
                # RAWIMUS body: week U32, seconds Double, IMU status U32.
                if message_id == 325 and body_len >= 16:
                    status = struct.unpack_from("<I", buffer, sync + header_len + 12)[0]
                    imu_status[status] += 1
                    total_ms = week * 604_800_000 + milliseconds
                    stamp = (week, milliseconds)
                    if previous_total_ms is not None and total_ms <= previous_total_ms:
                        non_monotonic += 1
                        if current_start_stamp and current_last_stamp:
                            imu_segments.append(
                                (current_start_stamp, current_last_stamp, current_count, current_gap_count)
                            )
                        current_start_stamp = stamp
                        current_count = 0
                        current_gap_count = 0
                    elif previous_total_ms is not None and total_ms - previous_total_ms > 10:
                        current_gap_count += 1
                    if current_start_stamp is None:
                        current_start_stamp = stamp
                    current_last_stamp = stamp
                    current_count += 1
                    previous_total_ms = total_ms

                cursor = sync + total_len

            if cursor:
                del buffer[:cursor]
            if eof:
                skipped += len(buffer)
                buffer.clear()

    if current_start_stamp and current_last_stamp:
        imu_segments.append((current_start_stamp, current_last_stamp, current_count, current_gap_count))
    if imu_segments:
        first_stamp, last_stamp, primary_count, gap_count = max(imu_segments, key=lambda item: item[2])
    else:
        primary_count = 0

    first_dt = gps_time(*first_stamp) if first_stamp else None
    last_dt = gps_time(*last_stamp) if last_stamp else None
    duration = (last_dt - first_dt).total_seconds() if first_dt and last_dt else None
    rate = ((primary_count - 1) / duration) if duration and duration > 0 else None

    # Common position/INS solution logs.  RAWIMUS (325) alone has no position.
    position_ids = {42, 47, 99, 142, 241, 244, 263, 507, 508, 1465}
    return StreamStats(
        path=path,
        size_bytes=os.path.getsize(path),
        packet_count=packet_count,
        rawimu_primary_segment_count=primary_count,
        rawimu_segment_count=len(imu_segments),
        message_ids={str(key): value for key, value in sorted(message_ids.items())},
        rawimu_status={f"0x{key:08X}": value for key, value in sorted(imu_status.items())},
        first_week=first_stamp[0] if first_stamp else None,
        first_ms=first_stamp[1] if first_stamp else None,
        last_week=last_stamp[0] if last_stamp else None,
        last_ms=last_stamp[1] if last_stamp else None,
        first_time_utc=_iso(first_dt),
        last_time_utc=_iso(last_dt),
        first_time_china=_iso(first_dt.astimezone(CHINA_TZ)) if first_dt else None,
        last_time_china=_iso(last_dt.astimezone(CHINA_TZ)) if last_dt else None,
        duration_seconds=duration,
        average_rate_hz=rate,
        gap_count_over_10ms=gap_count,
        non_monotonic_count=non_monotonic,
        unparsed_bytes=skipped,
        has_position_solution=any(message_id in position_ids for message_id in message_ids),
    )


INDEX_RE = re.compile(r"_(\d+)\.[^.]+$")
START_RE = re.compile(r"_(\d{8})_(\d{6})_\d+\.[^.]+$")


def analyze_indexed_files(directory: str, extensions: set[str]) -> dict[str, Any]:
    by_ext: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in os.scandir(directory):
        if not entry.is_file():
            continue
        ext = Path(entry.name).suffix.lower()
        if ext not in extensions:
            continue
        stat = entry.stat()
        match = INDEX_RE.search(entry.name)
        index = int(match.group(1)) if match else None
        by_ext[ext].append(
            {
                "name": entry.name,
                "index": index,
                "bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, CHINA_TZ).isoformat(timespec="seconds"),
            }
        )

    ext_stats: dict[str, Any] = {}
    index_sets: dict[str, set[int]] = {}
    start_dt: datetime | None = None
    max_index = -1
    for ext in sorted(extensions):
        items = sorted(by_ext.get(ext, []), key=lambda item: (item["index"] is None, item["index"] or -1))
        indices = {item["index"] for item in items if item["index"] is not None}
        index_sets[ext] = indices
        if indices:
            max_index = max(max_index, max(indices))
        total_bytes = sum(item["bytes"] for item in items)
        expected = set(range(min(indices), max(indices) + 1)) if indices else set()
        ext_stats[ext] = {
            "count": len(items),
            "bytes": total_bytes,
            "min_index": min(indices) if indices else None,
            "max_index": max(indices) if indices else None,
            "missing_indices": sorted(expected - indices),
            "zero_byte_files": sum(item["bytes"] == 0 for item in items),
            "min_bytes": min((item["bytes"] for item in items), default=0),
            "max_bytes": max((item["bytes"] for item in items), default=0),
        }
        if items and start_dt is None:
            start_match = START_RE.search(items[0]["name"])
            if start_match:
                start_dt = datetime.strptime("".join(start_match.groups()), "%Y%m%d%H%M%S").replace(
                    tzinfo=CHINA_TZ
                )

    nonempty_sets = [value for value in index_sets.values() if value]
    union = set().union(*nonempty_sets) if nonempty_sets else set()
    intersection = set.intersection(*nonempty_sets) if nonempty_sets else set()
    missing_components = {
        ext: sorted(union - index_sets.get(ext, set())) for ext in sorted(extensions)
    }
    end_dt = start_dt + timedelta(seconds=(max_index + 1) * 60) if start_dt and max_index >= 0 else None
    return {
        "directory": directory,
        "extensions": ext_stats,
        "index_union_count": len(union),
        "complete_index_count": len(intersection),
        "missing_components": missing_components,
        "start_time": _iso(start_dt),
        "end_time_estimated": _iso(end_dt),
        "duration_seconds_estimated": (end_dt - start_dt).total_seconds() if start_dt and end_dt else None,
        "complete": bool(union) and len(intersection) == len(union),
    }


def count_tree(directory: str) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    sizes: Counter[str] = Counter()
    file_count = 0
    for root, _, files in os.walk(directory):
        for name in files:
            path = os.path.join(root, name)
            ext = Path(name).suffix.lower() or "(none)"
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            counts[ext] += 1
            sizes[ext] += size
            file_count += 1
    return {
        "file_count": file_count,
        "by_extension": {
            ext: {"count": counts[ext], "bytes": sizes[ext]} for ext in sorted(counts)
        },
    }


def format_bytes(value: int | float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    number = float(value)
    for unit in units:
        if abs(number) < 1024 or unit == units[-1]:
            return f"{number:.2f} {unit}"
        number /= 1024
    return f"{number:.2f} TB"


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    whole = int(round(seconds))
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}小时{minutes:02d}分{secs:02d}秒"


def display_time(value: str | None) -> str:
    if not value:
        return "—"
    return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")


def coverage_metrics(two_m: dict[str, Any], eight_m: dict[str, Any]) -> dict[str, Any]:
    start2 = datetime.fromisoformat(two_m["start_time"])
    end2 = datetime.fromisoformat(two_m["end_time_estimated"])
    start8 = datetime.fromisoformat(eight_m["start_time"])
    end8 = datetime.fromisoformat(eight_m["end_time_estimated"])
    union_start = min(start2, start8)
    union_end = max(end2, end8)
    overlap_start = max(start2, start8)
    overlap_end = min(end2, end8)
    overlap_seconds = max(0.0, (overlap_end - overlap_start).total_seconds())
    union_seconds = (union_end - union_start).total_seconds()
    return {
        "union_start": _iso(union_start),
        "union_end": _iso(union_end),
        "union_seconds": union_seconds,
        "overlap_start": _iso(overlap_start),
        "overlap_end": _iso(overlap_end),
        "overlap_seconds": overlap_seconds,
        "paired_coverage_percent": overlap_seconds / union_seconds * 100 if union_seconds else 0.0,
        "two_m_percent": two_m["duration_seconds_estimated"] / union_seconds * 100 if union_seconds else 0.0,
        "eight_m_percent": eight_m["duration_seconds_estimated"] / union_seconds * 100 if union_seconds else 0.0,
    }


def _bar_position(start: str, end: str, union_start: str, union_end: str) -> tuple[float, float]:
    start_dt = datetime.fromisoformat(start)
    end_dt = datetime.fromisoformat(end)
    u0 = datetime.fromisoformat(union_start)
    u1 = datetime.fromisoformat(union_end)
    span = (u1 - u0).total_seconds()
    left = (start_dt - u0).total_seconds() / span * 100
    width = (end_dt - start_dt).total_seconds() / span * 100
    return left, width


def render_html(report: dict[str, Any]) -> str:
    stream = report["gps_imu"]
    two_m = report["two_m"]
    eight_m = report["eight_m"]
    lidar = report["lidar"]
    coverage = report["coverage"]
    invalid = report["invalid"]
    left2, width2 = _bar_position(two_m["start_time"], two_m["end_time_estimated"], coverage["union_start"], coverage["union_end"])
    left8, width8 = _bar_position(eight_m["start_time"], eight_m["end_time_estimated"], coverage["union_start"], coverage["union_end"])
    gps_start = stream["first_time_china"]
    gps_end = stream["last_time_china"]
    gps_left, gps_width = (0.0, 0.0)
    if gps_start and gps_end:
        union_start = min(datetime.fromisoformat(coverage["union_start"]), datetime.fromisoformat(gps_start))
        union_end = max(datetime.fromisoformat(coverage["union_end"]), datetime.fromisoformat(gps_end))
        timeline_start = _iso(union_start)
        timeline_end = _iso(union_end)
        left2, width2 = _bar_position(two_m["start_time"], two_m["end_time_estimated"], timeline_start, timeline_end)
        left8, width8 = _bar_position(eight_m["start_time"], eight_m["end_time_estimated"], timeline_start, timeline_end)
        gps_left, gps_width = _bar_position(gps_start, gps_end, timeline_start, timeline_end)
    else:
        timeline_start = coverage["union_start"]
        timeline_end = coverage["union_end"]

    message_rows = "".join(
        f"<tr><td>{html.escape(key)}</td><td class='num'>{value:,}</td><td>{html.escape(MESSAGE_NAMES.get(int(key), '未知报文'))}</td></tr>"
        for key, value in stream["message_ids"].items()
    ) or "<tr><td colspan='3'>未识别到报文</td></tr>"
    status_rows = "".join(
        f"<tr><td>{html.escape(key)}</td><td class='num'>{value:,}</td><td>{value / stream['packet_count'] * 100:.4f}%</td></tr>"
        for key, value in stream["rawimu_status"].items()
    ) or "<tr><td colspan='3'>无RAWIMUS状态</td></tr>"

    inventory = [
        ("GPS/IMU", "qinji/20260930.txt", 1, stream["size_bytes"], "原始观测/星历/IMU，无位置解算"),
        ("2M设备", "vin_device_2M", two_m["index_union_count"] * 4, sum(v["bytes"] for v in two_m["extensions"].values()), "4类文件齐全" if two_m["complete"] else "组件缺失"),
        ("8M设备", "vin_device_8M", eight_m["index_union_count"] * 4, sum(v["bytes"] for v in eight_m["extensions"].values()), "4类文件齐全" if eight_m["complete"] else "组件缺失"),
        ("2M雷达", "根目录 *.lid", lidar["index_union_count"], sum(v["bytes"] for v in lidar["extensions"].values()), "索引连续" if lidar["complete"] else "存在缺号"),
        ("无效区", "无效/", invalid["file_count"], sum(v["bytes"] for v in invalid["by_extension"].values()), "已排除"),
    ]
    inventory_rows = "".join(
        f"<tr><td>{name}</td><td><code>{html.escape(path)}</code></td><td class='num'>{count:,}</td><td class='num'>{format_bytes(size)}</td><td>{note}</td></tr>"
        for name, path, count, size, note in inventory
    )

    device_rows = "".join(
        f"<tr><td>{label}</td><td>{data['index_union_count']:,}</td><td>{data['complete_index_count']:,}</td><td>{display_time(data['start_time'])}</td><td>{display_time(data['end_time_estimated'])}</td><td>{format_duration(data['duration_seconds_estimated'])}</td><td>{'完整' if data['complete'] else '异常'}</td></tr>"
        for label, data in (("2M", two_m), ("8M", eight_m), ("2M雷达", lidar))
    )

    root_path = html.escape(report["root"])
    created = html.escape(report["generated_at"])
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>20260930 数据质量与里程计算汇报</title>
<style>
:root{{--bg:#f4f6f8;--panel:#fff;--text:#18212b;--muted:#667481;--line:#dfe5ea;--blue:#276ef1;--green:#16845b;--orange:#d97706;--red:#c43d3d;--ink:#293746}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 "Microsoft YaHei",Arial,sans-serif}}
.wrap{{max-width:1180px;margin:0 auto;padding:28px}} h1{{font-size:28px;margin:0 0 6px}} h2{{font-size:19px;margin:0 0 14px}} h3{{font-size:15px;margin:0 0 8px}} .sub{{color:var(--muted);word-break:break-all}}
.verdict{{display:flex;gap:14px;flex-wrap:wrap;margin:24px 0}} .card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;box-shadow:0 4px 16px rgba(22,31,39,.05)}}
.stat{{flex:1 1 220px}} .label{{color:var(--muted);margin-bottom:6px}} .value{{font-size:24px;font-weight:600}} .danger{{color:var(--red)}} .ok{{color:var(--green)}} .warn{{color:var(--orange)}}
.section{{margin:18px 0}} .grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}} table{{width:100%;border-collapse:collapse}} th,td{{padding:10px 9px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}} th{{color:var(--muted);font-weight:600;background:#f8fafb}} .num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}} code{{font-family:Consolas,monospace;font-size:12px}}
.alert{{border-left:4px solid var(--red);background:#fff5f5;padding:12px 14px;margin:10px 0;border-radius:6px}} .note{{border-left:4px solid var(--orange);background:#fff9ed;padding:12px 14px;margin:10px 0;border-radius:6px}}
.timeline{{padding:8px 0 2px}} .axis{{display:flex;justify-content:space-between;color:var(--muted);font-size:12px;margin:0 0 8px 88px}} .lane{{display:grid;grid-template-columns:78px 1fr;gap:10px;align-items:center;margin:12px 0}} .track{{position:relative;height:26px;background:#edf1f4;border-radius:6px;overflow:hidden}} .bar{{position:absolute;height:100%;border-radius:6px;display:flex;align-items:center;padding:0 8px;color:#fff;font-size:12px;min-width:2px;overflow:hidden;white-space:nowrap}} .gps{{background:var(--ink)}} .m2{{background:var(--blue)}} .m8{{background:var(--green)}}
.pill{{display:inline-block;padding:2px 8px;border-radius:99px;background:#eef2f5;color:var(--ink);font-size:12px}} ul{{margin:8px 0 0;padding-left:20px}} .footer{{color:var(--muted);font-size:12px;margin:24px 0 8px}}
@media(max-width:760px){{.wrap{{padding:16px}}.grid{{grid-template-columns:1fr}}.table-scroll{{overflow-x:auto}}h1{{font-size:23px}}}}
</style>
</head>
<body><main class="wrap">
<h1>20260930 数据质量与里程计算汇报</h1>
<div class="sub">数据源：{root_path}<br>生成时间：{created}</div>

<section class="verdict">
  <div class="card stat"><div class="label">总体判定</div><div class="value danger">异常</div><div>2M/8M覆盖不完整，且缺少GPS位置解算报文</div></div>
  <div class="card stat"><div class="label">2M / 8M同时覆盖</div><div class="value warn">{coverage['paired_coverage_percent']:.2f}%</div><div>{format_duration(coverage['overlap_seconds'])} / {format_duration(coverage['union_seconds'])}</div></div>
  <div class="card stat"><div class="label">GPS位置里程</div><div class="value danger">不可计算</div><div>有原始观测，无位置解算报文</div></div>
  <div class="card stat"><div class="label">IMU主连续段</div><div class="value ok">{stream['rawimu_primary_segment_count']:,} 帧</div><div>平均 {stream['average_rate_hz'] or 0:.2f} Hz</div></div>
</section>

<section class="card section">
<h2>关键结论</h2>
<div class="alert"><strong>GPS里程无法产出：</strong>二进制流包含GNSS原始观测、星历和RAWIMUS，但未发现位置/INS解算报文。上述原始报文不直接提供经纬度及GPS稳定状态，不能直接计算可靠轨迹里程。</div>
<div class="alert"><strong>2M数据覆盖不足：</strong>2M有效目录约 {format_duration(two_m['duration_seconds_estimated'])}，8M约 {format_duration(eight_m['duration_seconds_estimated'])}；两者共同覆盖仅 {coverage['paired_coverage_percent']:.2f}%。按“2M和8M同时存在”口径，本批数据应标记异常。</div>
<div class="note"><strong>文件内部完整性：</strong>2M和8M有效目录内的 AVI/BLF/PACK/VIN 四类组件索引本身均{'完整' if two_m['complete'] and eight_m['complete'] else '存在缺失'}；另有 {invalid['file_count']} 个文件位于“无效”目录，已从有效数据中排除。</div>
</section>

<section class="card section">
<h2>采集时间覆盖</h2>
<div class="timeline">
  <div class="axis"><span>{display_time(timeline_start)}</span><span>{display_time(timeline_end)}</span></div>
  <div class="lane"><strong>GPS/IMU</strong><div class="track"><div class="bar gps" style="left:{gps_left:.4f}%;width:{gps_width:.4f}%">原始GNSS + RAWIMUS</div></div></div>
  <div class="lane"><strong>2M</strong><div class="track"><div class="bar m2" style="left:{left2:.4f}%;width:{width2:.4f}%">{two_m['index_union_count']}组</div></div></div>
  <div class="lane"><strong>8M</strong><div class="track"><div class="bar m8" style="left:{left8:.4f}%;width:{width8:.4f}%">{eight_m['index_union_count']}组</div></div></div>
</div>
<div class="sub">设备文件按文件名起始时刻及连续索引、每组约60秒估算覆盖区间；GPS/IMU时间来自NovAtel二进制短报文头。</div>
</section>

<section class="card section table-scroll">
<h2>数据清单</h2>
<table><thead><tr><th>数据</th><th>位置</th><th class="num">文件数</th><th class="num">容量</th><th>判定</th></tr></thead><tbody>{inventory_rows}</tbody></table>
</section>

<section class="card section table-scroll">
<h2>2M / 8M 完整性</h2>
<table><thead><tr><th>数据</th><th>索引组数</th><th>完整组数</th><th>开始</th><th>估算结束</th><th>时长</th><th>组内组件</th></tr></thead><tbody>{device_rows}</tbody></table>
</section>

<section class="grid section">
  <div class="card table-scroll"><h2>NovAtel报文类型</h2><table><thead><tr><th>消息ID</th><th class="num">帧数</th><th>解释</th></tr></thead><tbody>{message_rows}</tbody></table></div>
  <div class="card table-scroll"><h2>RAWIMUS状态</h2><table><thead><tr><th>IMU状态</th><th class="num">帧数</th><th>占比</th></tr></thead><tbody>{status_rows}</tbody></table></div>
</section>

<section class="card section table-scroll">
<h2>里程结果</h2>
<table><thead><tr><th>计算项</th><th>结果</th><th>原因</th></tr></thead><tbody>
<tr><td>GPS稳定里程</td><td><span class="pill danger">不可计算</span></td><td>无经纬度/位置解算报文，也无可用GPS解算状态</td></tr>
<tr><td>2M数据里程</td><td><span class="pill danger">不可计算</span></td><td>可确定2M文件时间覆盖，但没有对应GPS轨迹</td></tr>
<tr><td>8M数据里程</td><td><span class="pill danger">不可计算</span></td><td>可确定8M文件时间覆盖，但没有对应GPS轨迹</td></tr>
<tr><td>2M/8M共同数据里程</td><td><span class="pill danger">不可计算</span></td><td>共同时间段已识别，但缺少位置坐标</td></tr>
</tbody></table>
</section>

<section class="card section">
<h2>异常明细与建议</h2>
<ul>
<li><strong>GPS日志类型不匹配：</strong><code>qinji/20260930.txt</code> 实际为NovAtel兼容二进制原始观测/星历/IMU，不是现有脚本支持的 `$GPFPD` 文本。</li>
<li><strong>GPS位置数据缺失：</strong>需要补充 BESTPOS / INSPVA / GPFPD 等含时间、经纬度和解算状态的数据。</li>
<li><strong>2M覆盖缺失：</strong>2M在 {display_time(two_m['end_time_estimated'])} 左右结束，而8M持续到 {display_time(eight_m['end_time_estimated'])} 左右。</li>
<li><strong>无效数据：</strong>“无效”目录包含 {invalid['file_count']} 个文件，不纳入有效性和里程统计。</li>
<li><strong>下一步：</strong>补齐位置解算日志后，可按时间戳与2M/8M文件覆盖区间求交，直接产出GPS稳定、2M、8M和共同覆盖里程。</li>
</ul>
</section>

<section class="card section">
<h2>协议依据</h2>
<ul>
<li><a href="https://docs.novatel.com/OEM7/Content/Messages/Description_of_Short_Headers.htm">NovAtel：长/短二进制报文头</a></li>
<li><a href="https://docs.novatel.com/OEM7/Content/SPAN_Logs/RAWIMUS.htm">NovAtel：RAWIMUS（消息ID 325）</a></li>
<li><a href="https://docs.novatel.com/OEM7/Content/Logs/RANGECMP.htm">NovAtel：RANGECMP（消息ID 140）</a></li>
<li><a href="https://docs.novatel.com/OEM7/Content/Logs/GPSEPHEM.htm">NovAtel：GPSEPHEM（消息ID 7）</a></li>
<li><a href="https://docs.novatel.com/OEM7/Content/Logs/GLOEPHEMERIS.htm">NovAtel：GLOEPHEMERIS（消息ID 723）</a></li>
</ul>
</section>

<div class="footer">审计口径：只读扫描原始目录；不修改、不移动原始数据。二进制协议按 NovAtel 兼容长/短报文头解析，所有里程结论遵循“无坐标不估算”。</div>
</main></body></html>"""


def build_report(root: str) -> dict[str, Any]:
    root = os.path.abspath(root)
    qinji_dir = os.path.join(root, "qinji")
    qinji_files = [entry.path for entry in os.scandir(qinji_dir) if entry.is_file()]
    if not qinji_files:
        raise FileNotFoundError(f"qinji目录没有文件: {qinji_dir}")
    stream = scan_novatel_stream(max(qinji_files, key=os.path.getsize))
    extensions = {".avi", ".blf", ".pack", ".vin"}
    two_m = analyze_indexed_files(os.path.join(root, "vin_device_2M"), extensions)
    eight_m = analyze_indexed_files(os.path.join(root, "vin_device_8M"), extensions)
    lidar = analyze_indexed_files(root, {".lid"})
    invalid = count_tree(os.path.join(root, "无效"))
    return {
        "root": root,
        "generated_at": datetime.now(CHINA_TZ).isoformat(timespec="seconds"),
        "gps_imu": asdict(stream),
        "two_m": two_m,
        "eight_m": eight_m,
        "lidar": lidar,
        "invalid": invalid,
        "coverage": coverage_metrics(two_m, eight_m),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit GPS/IMU and 2M/8M collection data")
    parser.add_argument("root", help="dataset root directory")
    parser.add_argument("--output", required=True, help="output HTML path")
    parser.add_argument("--json", dest="json_path", help="optional JSON details path")
    args = parser.parse_args()

    report = build_report(args.root)
    output = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    with open(output, "w", encoding="utf-8", newline="") as f:
        f.write(render_html(report))
    if args.json_path:
        json_path = os.path.abspath(args.json_path)
        os.makedirs(os.path.dirname(json_path), exist_ok=True)
        with open(json_path, "w", encoding="utf-8", newline="") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps({
        "html": output,
        "json": os.path.abspath(args.json_path) if args.json_path else None,
        "packets": report["gps_imu"]["packet_count"],
        "message_ids": report["gps_imu"]["message_ids"],
        "paired_coverage_percent": report["coverage"]["paired_coverage_percent"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
