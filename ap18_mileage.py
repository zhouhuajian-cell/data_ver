"""AP18 pcap → 有效里程（2M / 8M / GPS / 全覆盖）。

集成说明
--------
本模块把仓库里原本互不相干的两套东西接在一起：

* **轨迹来源**：仓库根目录 ``parse_ap18_pcap.decode_file()`` —— 原 AP18 可视化
  项目的 GSOF49 解析器。这里不重写 pcap 解析，直接复用它解析出的行，
  因此网页「原始记录表」里看到的与算里程用的是同一份数据。
* **里程计算**：``mileage/`` 下的里程计算器（TrajectoryPoint / Vincenty /
  逐相邻段判定），以及 ``mileage/verify_service/mileage.py`` 的覆盖区间推导
  与交集统计。不另写一套算法。

判定口径
--------
* **GPS 稳定**：GSOF49 的 GNSS 状态 ∈ {1 SPS, 2 DGPS, 3 PPS, 4 RTK 固定,
  5 RTK 浮动}；0（无定位）与 6（DR 航位推算）不算稳定。
  见 ``mileage/config.py:GSOF_STABLE_GNSS_CODES``。
* **2M / 8M**：传感器（相机）的**录制时段**，由设备文件名里的时间戳推导
  （组起始时刻 + 组序号 × 60 秒），每个文件代表约 60 秒有数据。
  AP18 pcap 的 GSOF49 没有 baseline 字段，所以轨迹点的 baseline 恒为 None，
  2M/8M 里程完全来自「覆盖区间 ∩ GPS 稳定段」。
* **里程只来自轨迹**：2M/8M 只回答「该段当时是否在录」，本身不产生距离。
* **全覆盖**：2M 与 8M **同时**都在录的 GPS 稳定里程（交集）。
* **实际有效（并集）**：2M 或 8M 任一在录的 GPS 稳定里程。

时间基准
--------
里程计算器用 ``datetime.fromtimestamp(段中点的秒数)`` 与设备文件名推导出的
**本地时间**比较，所以轨迹点时间必须是真正的 Unix 秒。GSOF49 的
``gps_time`` 是带时区的 UTC ISO 串（``gps_week``/``gps_ms`` 换算并扣除
GPS-UTC 闰秒），``datetime.fromisoformat(...).timestamp()`` 正好得到
Unix 秒，两者在同一本地时区下自然对齐。
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import math
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from parse_ap18_pcap import decode_file

ROOT = Path(__file__).resolve().parent
MILEAGE_DIR = ROOT / "mileage"
SERVICE_DIR = MILEAGE_DIR / "verify_service"

# 覆盖区间：(起点, 结束)，均为 naive 本地时间，与设备文件名里的时间同一时区。
Interval = Tuple[dt.datetime, dt.datetime]
# 单个 pcap 的解码结果：(行列表, 错误文本)；错误为空串表示成功。
DecodeResult = Tuple[List[Dict[str, Any]], str]

# 默认最多解析多少个 pcap：防止误填一个巨大目录把请求拖死。
DEFAULT_MAX_PCAP_FILES = 5000


class MileageAnalysisError(Exception):
    """数据目录不可用 / 解析不到轨迹等「用户可修正」的错误。"""


_SERVICE: Any = None


# --------------------------------------------------------------------------
# 延迟导入：里程计算器是一组扁平模块，导入时会往 sys.path 里加 mileage/
# --------------------------------------------------------------------------


def _service() -> Any:
    """加载 ``mileage/verify_service/mileage.py``（覆盖区间推导 + 交集统计）。

    用 importlib 按真实文件路径加载并注册成私有模块名，原因有两个：
    1. ``mileage/verify_service/app.py`` 依赖 fastapi，而网页后端是标准库
       HTTP 服务，不能整体导入 verify_service 包；
    2. 目录名 ``mileage`` 与 ``verify_service/mileage.py`` 模块名同名，
       直接 ``import mileage`` 会互相遮蔽。
    该模块在导入时会把 mileage/ 目录加进 sys.path，之后 ``trajectory_parser``
    / ``main`` 这些扁平模块才可导入（与它部署时的行为一致）。
    """
    global _SERVICE
    if _SERVICE is None:
        module_path = SERVICE_DIR / "mileage.py"
        spec = importlib.util.spec_from_file_location("_ap18_mileage_service", module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载里程覆盖模块: {module_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _SERVICE = module
    return _SERVICE


def _core() -> Any:
    """导入里程计算器主模块（``mileage/main.py``）。"""
    _service()  # 保证 mileage/ 已在 sys.path 上
    if str(MILEAGE_DIR) not in sys.path:
        sys.path.insert(0, str(MILEAGE_DIR))
    import main as core  # noqa: PLC0415 - 延迟导入，避免后端启动即加载

    return core


def _trajectory_types():
    """返回 (TrajectoryPoint, TrajectoryLoadResult, GSOF 状态标签)。"""
    _service()
    from trajectory_parser import (  # noqa: PLC0415
        STATUS_LABEL_GSOF,
        TrajectoryLoadResult,
        TrajectoryPoint,
    )

    return TrajectoryPoint, TrajectoryLoadResult, STATUS_LABEL_GSOF


# --------------------------------------------------------------------------
# 解析行 → 轨迹点
# --------------------------------------------------------------------------


def _row_to_point(row: Dict[str, Any], line_no: int, point_cls, status_label: str):
    """把 ``decode_file`` 的一行 GSOF49 记录转成 ``TrajectoryPoint``。

    返回 None 表示该行没有可用的定位（缺经纬度/时间，或数值非法）。
    """
    iso = row.get("gps_time")
    lat_raw = row.get("latitude")
    lon_raw = row.get("longitude")
    if iso is None or lat_raw is None or lon_raw is None:
        return None
    try:
        timestamp = dt.datetime.fromisoformat(str(iso)).timestamp()
        lat = float(lat_raw)
        lon = float(lon_raw)
    except (TypeError, ValueError):
        return None
    alt_raw = row.get("altitude")
    try:
        alt = float(alt_raw) if alt_raw is not None else 0.0
    except (TypeError, ValueError):
        alt = 0.0
    if not (math.isfinite(timestamp) and math.isfinite(lat) and math.isfinite(lon)):
        return None
    # 明显超出经纬度范围的行直接丢掉，避免脏点把 Vincenty 拖进无解迭代。
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return None

    raw_status = row.get("gnss_status")
    if isinstance(raw_status, bool) or not isinstance(raw_status, (int, float)):
        status: Optional[int] = None
    else:
        status = int(raw_status)

    return point_cls(
        timestamp,
        lon,
        lat,
        alt,
        line_no=line_no,
        baseline_m=None,
        solution_status=status,
        # GSOF 没有 NMEA 校验和，解析器已经把畸形记录挡掉了。
        checksum_valid=True,
        status_label=status_label,
        has_status_field=status is not None,
    )


def rows_to_points(rows: Sequence[Dict[str, Any]], *, first_line: int = 1) -> List[Any]:
    """``parse_ap18_pcap.decode_file`` 的行 → 按时间排序的轨迹点列表。"""
    point_cls, _, status_label = _trajectory_types()
    points: List[Any] = []
    for offset, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        point = _row_to_point(row, first_line + offset, point_cls, status_label)
        if point is not None:
            points.append(point)
    points.sort(key=lambda item: item.timestamp)
    return points


def _points_from_trajectory_file(path: Path) -> List[Any]:
    """复用里程计算器自带的 txt/csv 轨迹解析（目录里同时有轨迹文件时用）。"""
    _service()
    from trajectory_parser import load_trajectory  # noqa: PLC0415

    result = load_trajectory(str(path), include_diagnostics=True, verbose=False)
    points = getattr(result, "points", result)
    return list(points or [])


def _default_decode_many(paths: Sequence[str]) -> List[DecodeResult]:
    """逐个解码 pcap（无进程池时的退化实现）。"""
    out: List[DecodeResult] = []
    for path_text in paths:
        try:
            rows, _counts = decode_file(Path(path_text))
            out.append((rows, ""))
        except Exception as exc:  # noqa: BLE001 - 单文件失败不应拖垮整批
            out.append(([], f"{type(exc).__name__}: {exc}"))
    return out


# --------------------------------------------------------------------------
# 覆盖区间 → 四类里程
# --------------------------------------------------------------------------


def _coverage_payload(label: str, dev: Dict[str, Any]) -> Dict[str, Any]:
    """把探测结果里的 2M/8M 设备信息整理成可 JSON 化的摘要。

    注意 ``probe_mileage_dataset`` 的 ``end`` 取的是「最大**起始**时刻」，所以
    只录了一个文件时 start == end、时长会显示 0 秒。这里按区间重新算真实跨度：
    start = 最早区间起点，end = 最晚区间**结束**（起点 + 60 秒），
    时长 = end - start。这样单文件也能显示「60 秒」而不是「0 秒」。
    """
    intervals: List[Interval] = list(dev.get("intervals") or [])
    covered_seconds = 0.0
    starts: List[dt.datetime] = []
    ends: List[dt.datetime] = []
    for start, end in intervals:
        try:
            covered_seconds += max(0.0, (end - start).total_seconds())
        except TypeError:
            continue
        starts.append(start)
        ends.append(end)

    start_text = dev.get("start")
    end_text = dev.get("end")
    duration_s = 0.0
    if starts and ends:
        first, last = min(starts), max(ends)
        start_text = first.strftime("%Y-%m-%d %H:%M:%S")
        end_text = last.strftime("%Y-%m-%d %H:%M:%S")
        duration_s = max(0.0, (last - first).total_seconds())
    elif start_text and end_text:
        # 没有可解析区间时退回探测结果（老数据的 start/end 字符串）
        try:
            duration_s = (
                dt.datetime.strptime(end_text, "%Y-%m-%d %H:%M:%S")
                - dt.datetime.strptime(start_text, "%Y-%m-%d %H:%M:%S")
            ).total_seconds()
        except ValueError:
            duration_s = 0.0
    return {
        "label": label,
        "present": bool(dev.get("files")),
        "folders": list(dev.get("folders") or []),
        "file_count": int(dev.get("files") or 0),
        "bytes": int(dev.get("bytes") or 0),
        # 占位文件（<64 KB，采集失败留下的 4 字节 .vin 之类）不计入覆盖
        "ignored_small_files": int(dev.get("small_files") or 0),
        "ignored_small_bytes": int(dev.get("small_bytes") or 0),
        "start": start_text,
        "end": end_text,
        "duration_s": round(duration_s, 1),
        "interval_count": len(intervals),
        "covered_seconds": round(covered_seconds, 1),
    }


def _assign_coverage(segments: Sequence[Any], intervals_2m: Sequence[Interval],
                     intervals_8m: Sequence[Interval]) -> Dict[str, Any]:
    """按段的时间中点判断「当时 2M/8M 是否在录」，累加四类里程。

    只统计 ``counted_gps_stable`` 的段 —— 与 verify_service 的口径一致：
    GPS 不稳定 / 时间异常 / 跳点的段本来就不计入 GPS 稳定里程，自然也不能
    算进「覆盖到的有效里程」。
    """
    service = _service()
    set_2m = service._SortedIntervals(intervals_2m)
    set_8m = service._SortedIntervals(intervals_8m)

    covered_2m = covered_8m = both = either = no_cover = 0.0
    stable_segments = 0
    for seg in segments:
        if not getattr(seg, "counted_gps_stable", False):
            continue
        stable_segments += 1
        mid = (seg.start_timestamp + seg.end_timestamp) / 2.0
        try:
            when = dt.datetime.fromtimestamp(mid)
        except (OverflowError, OSError, ValueError):
            continue
        in_2m = set_2m.covers(when)
        in_8m = set_8m.covers(when)
        distance = seg.distance_m
        if in_2m:
            covered_2m += distance
        if in_8m:
            covered_8m += distance
        if in_2m and in_8m:
            both += distance
        if in_2m or in_8m:
            either += distance
        else:
            no_cover += distance
    return {
        "stable_segments": stable_segments,
        "covered_2m_m": covered_2m,
        "covered_8m_m": covered_8m,
        "both_m": both,
        "either_m": either,
        "no_cover_m": no_cover,
    }


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------


def _decode_pcap_files(
    paths: Sequence[str],
    decode_many: Optional[Callable[[Sequence[str]], List[DecodeResult]]],
) -> List[DecodeResult]:
    if decode_many is None:
        return _default_decode_many(paths)
    results = list(decode_many(paths))
    # 容错：外部实现返回长度不一致时补齐，避免 zip 静默截断。
    while len(results) < len(paths):
        results.append(([], "解码器没有返回该文件的结果"))
    return results[: len(paths)]


def _format_seconds(value: float) -> str:
    return dt.datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


# 时间轴最多回传多少个区间：设备文件合并后通常只有个位数，这个上限只防病态数据。
MAX_TIMELINE_SPANS = 5000


def _merge_naive_intervals(
    intervals: Sequence[Interval], *, gap_s: float = 0.5
) -> List[Interval]:
    """合并 naive 本地墙钟区间（同一组的 .avi/.pack/.vin/.blf 是同一时段）。

    设备目录里一组数据会同时有 4 种扩展名，不合并的话「重叠小时数」会翻几倍，
    覆盖判定也要多扫几倍区间。
    """
    items = sorted(intervals)
    merged: List[List[dt.datetime]] = []
    for start, end in items:
        if merged and (start - merged[-1][1]).total_seconds() <= gap_s:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(item[0], item[1]) for item in merged]


def _merge_spans(spans: Sequence[Tuple[float, float]], *, gap_s: float = 0.0) -> List[Tuple[float, float]]:
    """把 ``[(start, end)]``（epoch 秒）排序并合并；``gap_s`` 内的相邻区间视为连续。"""
    cleaned: List[Tuple[float, float]] = []
    for start, end in spans:
        try:
            start_f = float(start)
            end_f = float(end)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(start_f) and math.isfinite(end_f)) or end_f < start_f:
            continue
        cleaned.append((start_f, end_f))
    cleaned.sort()
    merged: List[List[float]] = []
    for start_f, end_f in cleaned:
        if merged and start_f <= merged[-1][1] + gap_s:
            merged[-1][1] = max(merged[-1][1], end_f)
        else:
            merged.append([start_f, end_f])
    return [(item[0], item[1]) for item in merged]


def _intersect_spans(
    left: Sequence[Tuple[float, float]], right: Sequence[Tuple[float, float]]
) -> List[Tuple[float, float]]:
    """两个已排序、互不重叠的区间列表求交（用于「2M 与 8M 同时录制」）。"""
    out: List[Tuple[float, float]] = []
    i = j = 0
    while i < len(left) and j < len(right):
        a_start, a_end = left[i]
        b_start, b_end = right[j]
        start = max(a_start, b_start)
        end = min(a_end, b_end)
        if end > start:
            out.append((start, end))
        if a_end < b_end:
            i += 1
        else:
            j += 1
    return out


def _pack_spans(spans: Sequence[Tuple[float, float]]) -> List[List[float]]:
    """转成 JSON 可用的 ``[[start, end], ...]``，并限制数量。"""
    if len(spans) > MAX_TIMELINE_SPANS:
        # 病态数据（一天切成上万个碎片）时均匀抽稀，保留整体覆盖形态。
        step = len(spans) / float(MAX_TIMELINE_SPANS)
        spans = [spans[int(i * step)] for i in range(MAX_TIMELINE_SPANS)]
    return [[round(start, 3), round(end, 3)] for start, end in spans]


def _timeline_payload(
    points: Sequence[Any],
    segments: Sequence[Any],
    intervals_2m: Sequence[Interval],
    intervals_8m: Sequence[Interval],
) -> Dict[str, Any]:
    """给前端画「覆盖时间轴对比图」用的区间数据（全部是 epoch 秒）。

    三条轨道的区间都先合并成连续段：一圈 100Hz 的稳定点如果逐段回传，
    前端要画几万个矩形；合并后通常只剩几段。
    """
    gps_spans = [
        (seg.start_timestamp, seg.end_timestamp)
        for seg in segments
        if getattr(seg, "counted_gps_stable", False)
    ]
    # 只用于画甘特图：10 秒内的短暂不稳定（停车/1 秒级状态切换）合并成一段，
    # 否则 5 小时的数据会被切出上万个碎片，图上看不出覆盖形态。
    # 里程统计不受这里影响（那是逐段精确累计的）。
    gps_merged = _merge_spans(gps_spans, gap_s=10.0)
    merged_2m = _merge_spans(
        [(start.timestamp(), end.timestamp()) for start, end in intervals_2m], gap_s=0.5
    )
    merged_8m = _merge_spans(
        [(start.timestamp(), end.timestamp()) for start, end in intervals_8m], gap_s=0.5
    )
    return {
        "start": round(points[0].timestamp, 3),
        "end": round(points[-1].timestamp, 3),
        "gps_stable": _pack_spans(gps_merged),
        "2m": _pack_spans(merged_2m),
        "8m": _pack_spans(merged_8m),
        "both": _pack_spans(_intersect_spans(merged_2m, merged_8m)),
    }


# 批量刷写时识别的日期目录名（YYYYMMDD）
DATE_DIR_RE = re.compile(r"^\d{8}$")
# 单次批量刷写最多处理多少个日期目录（防止误选一个巨大父目录）
MAX_BATCH_DAYS = 200


def scan_batch_dates(
    data_root: str | Path, *, max_days: int = MAX_BATCH_DAYS, since: str = ""
) -> Dict[str, Any]:
    """扫描父目录下的日期子目录（``YYYYMMDD``），给「批量刷写」做预检。

    只读目录结构、不解析 pcap，所以很快；返回每个日期目录有没有轨迹 / 2M / 8M，
    前端据此让用户勾选要刷哪些天。

    路径兼容两种写法：
      * 服务器本地/挂载路径（``/mnt/logs_new/...``）；
      * Windows 共享路径（``\\\\192.168.20.206\\logs_new\\...``），按 CIFS 挂载表自动映射。
    日期目录会向下找 **两层**：既能直接选 ``.../Overseas/9``，也能选 ``.../Overseas``。
    """
    service = _service()
    raw_text = str(data_root).strip().strip('"').strip("'")
    base = Path(raw_text).expanduser()
    if not base.exists():
        # 服务器上常直接粘 Windows 共享路径，按 /proc/mounts 的 CIFS 映射试一次。
        try:
            mapped = service.normalize_path(raw_text)
        except Exception:  # noqa: BLE001
            mapped = base
        if str(mapped) and str(mapped) != str(base):
            base = Path(mapped)
    if not base.exists():
        hint = ""
        if raw_text.startswith("\\\\") or raw_text.startswith("//"):
            hint = "（服务器上请用 /mnt/logs_new/... 形式；Windows 共享路径需要能匹配 CIFS 挂载）"
        raise MileageAnalysisError(f"目录不存在或不可访问: {base}{hint}")
    if not base.is_dir():
        raise MileageAnalysisError(f"不是目录: {base}")

    # 递归找日期目录（最多四层：Europe/CZ/data_ok/20260729、BR/2026/8/20260805 这类）
    date_dirs: List[Path] = []
    queue: List[Tuple[Path, int]] = [(base, 0)]
    while queue and len(date_dirs) < max_days:
        current, depth = queue.pop(0)
        try:
            children = sorted(current.iterdir())
        except OSError:
            continue
        for child in children:
            try:
                if not child.is_dir():
                    continue
            except OSError:
                continue
            if DATE_DIR_RE.match(child.name):
                date_dirs.append(child)
                if len(date_dirs) >= max_days:
                    break
            elif depth < 4 and not child.name.startswith("."):
                queue.append((child, depth + 1))

    dates: List[Dict[str, Any]] = []
    ignored_old = 0
    for entry in date_dirs:
        # since=YYYYMMDD：更早的日期目录直接忽略，不做预检（省 CIFS 扫描时间）
        if since and entry.name < since:
            ignored_old += 1
            continue
        try:
            info = service.probe_mileage_dataset(entry)
        except Exception as exc:  # noqa: BLE001 - 单天预检失败不阻断其它天
            dates.append(
                {
                    "date": entry.name,
                    "path": str(entry),
                    "ready": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue

        trajectory_files = int(len(info.get("trajectory_files") or []))
        pcap_files = int(len(info.get("pcap_files") or []))
        device_2m = dict(info.get("device_2m") or {})
        device_8m = dict(info.get("device_8m") or {})
        files_2m = int(device_2m.get("files") or 0)
        files_8m = int(device_8m.get("files") or 0)
        has_trajectory = bool(trajectory_files or pcap_files)
        # 轨迹可能藏在子目录里（如 rtkimu_qj_7683/20261003.txt），预检也要认。
        subdir_trajectory_files = 0
        if not has_trajectory:
            found, _binary_notes, _notes = _discover_subdir_trajectories(entry, info)
            subdir_trajectory_files = len(found)
            has_trajectory = bool(found)
        dates.append(
            {
                "date": entry.name,
                "path": str(entry),
                # 有轨迹就能算 GPS 稳定里程；缺 2M/8M 只影响覆盖类里程，不阻断。
                "ready": has_trajectory,
                "has_trajectory": has_trajectory,
                "trajectory_files": trajectory_files,
                "pcap_files": pcap_files,
                "subdir_trajectory_files": subdir_trajectory_files,
                "files_2m": files_2m,
                "files_8m": files_8m,
                "bytes_2m": int(device_2m.get("bytes") or 0),
                "bytes_8m": int(device_8m.get("bytes") or 0),
                "start_2m": device_2m.get("start"),
                "end_2m": device_2m.get("end"),
                "start_8m": device_8m.get("start"),
                "end_8m": device_8m.get("end"),
                "problems": list(info.get("problems") or []),
            }
        )

    return {
        "ok": True,
        "root": str(base),
        "count": len(dates),
        "ready_count": sum(1 for item in dates if item.get("ready")),
        "ignored_old": ignored_old,
        "dates": dates,
    }


# 轨迹文本扩展名 / 单次分析最多保留的轨迹点数。
TRAJECTORY_TEXT_EXTS = {".txt", ".csv"}
MAX_TRAJECTORY_POINTS = 400_000


def _sniff_trajectory_text(path: Path) -> Optional[str]:
    """粗判一个文本文件是不是轨迹，返回 'nmea' / 'csv' / None。

    只读前 4 KB，不碰 219 MB 的大文件。俄罗斯 E5 的 ``qinji/20261003.txt``
    实际是 NovAtel 二进制原始观测/星历/IMU（同步字 AA 44），没有位置解算，
    必须在这里挡掉，否则会被当成文本轨迹解析出一堆垃圾。
    """
    try:
        with path.open("rb") as stream:
            head = stream.read(4096)
    except OSError:
        return None
    if not head:
        return None
    if head[:2] == b"\xaa\x44" or head.find(b"\xaa\x44", 0, 256) >= 0:
        return None
    text = head.decode("utf-8", "ignore").lstrip("\ufeff").lstrip()
    if not text:
        return None
    if text.startswith(("$GP", "$GN", "$IN", "$GL", "$BD")):
        return "nmea"
    first_line = text.splitlines()[0] if text.splitlines() else ""
    if "," in first_line and first_line[:1].isdigit():
        return "csv"
    return None


def _discover_subdir_trajectories(
    root: Path, info: Dict[str, Any]
) -> Tuple[List[Path], List[Dict[str, Any]], List[str]]:
    """在非设备子目录里找轨迹文本（如 ``rtkimu_qj_7683/20261003.txt``）。

    返回 ``(轨迹文件, 二进制文件说明, 提示)``。2M/8M 设备目录、output 目录跳过。
    """
    found: List[Path] = []
    binary_notes: List[Dict[str, Any]] = []
    notes: List[str] = []
    device_folders = set()
    for key in ("device_2m", "device_8m"):
        device_folders.update((info.get(key) or {}).get("folders") or [])
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return found, binary_notes, notes
    for entry in entries:
        if not entry.is_dir() or entry.name in device_folders or entry.name == "output":
            continue
        try:
            files = sorted(
                path
                for path in entry.iterdir()
                if path.is_file() and path.suffix.lower() in TRAJECTORY_TEXT_EXTS
            )
        except OSError:
            continue
        for path in files:
            label = f"{entry.name}/{path.name}"
            kind = _sniff_trajectory_text(path)
            if kind:
                found.append(path)
                notes.append(f"{label}: 子目录里识别为 {kind.upper()} 轨迹")
            else:
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                binary_notes.append(
                    {
                        "name": label,
                        "bytes": size,
                        "reason": "内容不是可识别的轨迹文本（二进制或格式不支持），已跳过",
                    }
                )
    return found, binary_notes, notes


def _decimate_points(points: List[Any], limit: int = MAX_TRAJECTORY_POINTS) -> Tuple[List[Any], int]:
    """按固定步长抽稀轨迹点，返回 ``(抽稀后, 步长)``。

    100 Hz 的惯导一天就是上千万行，全量建对象会吃掉几个 GB 内存；
    10 Hz 对里程计算几乎没有影响（每段距离仍是 Vincenty 精确算）。
    """
    total = len(points)
    if total <= limit or limit <= 2:
        return points, 1
    step = int(math.ceil(total / float(limit)))
    sampled = points[::step]
    if sampled and sampled[-1] is not points[-1]:
        sampled.append(points[-1])
    return sampled, step


def _machine_utc_offset_hours() -> float:
    offset = dt.datetime.now().astimezone().utcoffset()
    return offset.total_seconds() / 3600.0 if offset else 0.0


def _shift_intervals(intervals: Sequence[Interval], offset_hours: Optional[int]) -> List[Interval]:
    """设备文件名时间 → 「机器本地墙钟」坐标系（默认不平移）。

    实测（俄罗斯 E5 的 vin_device_2M/8M）：文件名时刻 + 约 61 秒 == 文件 mtime，
    而 Windows 把 mtime 按本机时区（CST+8）显示，说明设备时钟就是北京时间，
    与本机时区一致，因此默认 **不做平移**，直接和 ``datetime.fromtimestamp`` 比。

    如果以后遇到设备时钟确实不是本机时区，可以在 config 里设
    ``DEVICE_TIMEZONE_OFFSET_HOURS``（例如设备用 UTC+6 就填 6），这里会按
    「机器本地 = 设备本地 - 设备时区 + 机器时区」平移。
    """
    if not intervals or offset_hours is None:
        return list(intervals)
    delta = dt.timedelta(hours=_machine_utc_offset_hours() - offset_hours)
    return [(start + delta, end + delta) for start, end in intervals]


def analyze_directory(
    data_root: str | Path,
    *,
    baseline_tolerance_m: float = 0.5,
    decode_many: Optional[Callable[[Sequence[str]], List[DecodeResult]]] = None,
    max_pcap_files: int = DEFAULT_MAX_PCAP_FILES,
) -> Dict[str, Any]:
    """分析一个服务器端数据目录，返回四类里程 + 覆盖摘要。

    ``data_root`` 里应当同时有：

    * 轨迹来源：AP18 pcap 目录（如 ``AP18/*.pcap``，或顶层 ``*.pcap``），
      或者顶层 ``*.txt`` / ``*.csv`` 轨迹文件；
    * 2M / 8M 设备目录（目录名里带 ``2M`` / ``8M``，如
      ``M57_2M_.../``、``M57_8M_.../``）。

    失败时抛 :class:`MileageAnalysisError`（带上可直接展示给用户的原因）。
    """
    started = time.perf_counter()
    service = _service()
    root = Path(str(data_root)).expanduser()
    # 部署在 Linux 服务器时，用户经常直接贴 Windows 共享路径
    # （\\192.168.20.206\logs_new\...）。本地路径能打开就原样用；打不开再交给
    # normalize_path 按服务器真实的 CIFS 挂载表映射到 /mnt/...。
    # 在 Windows 上跑时本地路径一定存在，不会被映射；UNC 路径也由系统自己解析。
    if not root.exists():
        raw_text = str(data_root).strip().strip('"').strip("'")
        try:
            mapped = service.normalize_path(raw_text)
        except Exception:  # noqa: BLE001 - 映射失败就保持原路径，交给下面报错
            mapped = root
        if str(mapped) and str(mapped) != str(root):
            root = Path(mapped)
    info = service.probe_mileage_dataset(root)

    if not info.get("exists"):
        hint = ""
        if str(data_root).strip().startswith(("\\\\", "//")):
            hint = "（服务器上请用 /mnt/logs_new/... 形式；Windows 共享路径需要能匹配 CIFS 挂载）"
        raise MileageAnalysisError(f"目录不存在或不可访问: {root}{hint}")
    if not info.get("is_dir"):
        raise MileageAnalysisError(f"不是目录: {root}")

    problems = list(info.get("problems") or [])
    # 非致命问题先收进 issues；「没有轨迹来源」要等子目录探测之后才能判定。
    issues: List[str] = [item for item in problems if "轨迹来源" not in item]
    # 探测模块的提示里有一条是给旧 pcap_gga 路径写的（"将解析 GGA 语句"），
    # 现在走 GSOF49，带出去会误导用户；缺 2M/8M 的提示也与下面的 issues 重复。
    warnings: List[str] = []
    files_meta: List[Dict[str, Any]] = []
    points: List[Any] = []

    trajectory_files = list(info.get("trajectory_files") or [])
    pcap_files = [str(p) for p in (info.get("pcap_files") or [])]
    binary_notes: List[Dict[str, Any]] = []

    # 顶层没有轨迹、也没有 pcap 时，去子目录里找。
    # 俄罗斯 E5 的轨迹是 rtkimu_qj_7683/20261003.txt（$GPFPD），不在顶层；
    # qinji/20261003.txt 是 NovAtel 二进制原始观测，会被嗅探挡掉。
    subdir_paths: List[Path] = []
    if not trajectory_files and not pcap_files:
        subdir_paths, binary_notes, discovery_notes = _discover_subdir_trajectories(root, info)
        issues.extend(discovery_notes)

    if not trajectory_files and not pcap_files and not subdir_paths:
        hint = ""
        if binary_notes:
            hint = "；" + "；".join(
                f"{note['name']} {note['reason']}" for note in binary_notes[:3]
            )
        raise MileageAnalysisError(
            "目录里既没有 AP18 pcap，也没有可解析的 .txt/.csv 轨迹文件，无法计算里程" + hint
        )

    if trajectory_files or subdir_paths:
        # 目录里给了 txt/csv 轨迹：沿用里程计算器自己的解析器。
        entries: List[Tuple[Path, str]] = [
            (root / entry["name"], entry["name"]) for entry in trajectory_files
        ]
        entries.extend((path, f"{path.parent.name}/{path.name}") for path in subdir_paths)
        for path, label in entries:
            try:
                loaded = _points_from_trajectory_file(path)
            except Exception as exc:  # noqa: BLE001
                issues.append(f"{label}: 轨迹解析失败（{exc}）")
                continue
            if not loaded:
                issues.append(f"{label}: 没有解析出有效轨迹点")
                continue
            points.extend(loaded)
            files_meta.append(
                {"name": label, "kind": "轨迹文件", "points": len(loaded)}
            )
        source_kind = "轨迹文件"
    else:
        source_kind = "AP18 pcap"
        total = len(pcap_files)
        if total > max_pcap_files:
            warnings.append(
                f"pcap 文件 {total} 个，超过单次上限 {max_pcap_files}，只解析前 {max_pcap_files} 个"
            )
            pcap_files = pcap_files[:max_pcap_files]
        decoded = _decode_pcap_files(pcap_files, decode_many)
        for path_text, (rows, failure) in zip(pcap_files, decoded):
            name = Path(path_text).name
            if failure:
                issues.append(f"{name}: pcap 解析失败（{failure}）")
                continue
            file_points = rows_to_points(rows)
            if not file_points:
                issues.append(f"{name}: 没有可用的 GSOF49 定位记录")
                continue
            points.extend(file_points)
            files_meta.append(
                {"name": name, "kind": "AP18 pcap", "rows": len(rows), "points": len(file_points)}
            )

    points.sort(key=lambda item: item.timestamp)
    if len(points) < 2:
        raise MileageAnalysisError(
            "有效轨迹点不足 2 个，无法计算里程"
            + (f"（{'；'.join(issues[:3])}）" if issues else "")
        )

    # 100 Hz 惯导一天上千万点，全量建对象会吃掉几个 GB；抽稀到 10 Hz 级别。
    original_point_count = len(points)
    points, decimate_step = _decimate_points(points)
    if decimate_step > 1:
        warnings.append(
            f"轨迹点 {original_point_count:,} 个，按 1/{decimate_step} 抽稀到 {len(points):,} 个"
            "（航向/里程误差可忽略）"
        )
    if binary_notes:
        for note in binary_notes[:5]:
            issues.append(f"{note['name']}: {note['reason']}")

    # ---- 里程计算（纯内存，零文件 I/O）----
    core = _core()
    _, load_cls, _ = _trajectory_types()
    status_counts: Dict[str, int] = {}
    for point in points:
        label = getattr(point, "status_label", "") or "未知"
        key = f"{label}:{getattr(point, 'solution_status', None)}"
        status_counts[key] = status_counts.get(key, 0) + 1

    load = load_cls(
        points=points,
        status_counts=status_counts,
        path=str(root),
        file_type=source_kind,
    )
    analysis = core.AnalysisResult(
        file_name="AP18 轨迹",
        file_type=source_kind,
        point_count=len(points),
    )
    core._analyze_points(analysis, load, baseline_tolerance_m)

    # ---- 覆盖交集（含设备时区自动推断）----
    device_2m = dict(info.get("device_2m") or {})
    device_8m = dict(info.get("device_8m") or {})
    coverage_2m = _coverage_payload("2M", device_2m)
    coverage_8m = _coverage_payload("8M", device_8m)
    raw_intervals_2m = _merge_naive_intervals(device_2m.get("intervals") or [])
    raw_intervals_8m = _merge_naive_intervals(device_8m.get("intervals") or [])
    # 设备文件名时间默认与本机时区一致（mtime 实测验证），因此不平移；
    # 若确有设备时钟不同，在 config.DEVICE_TIMEZONE_OFFSET_HOURS 里覆盖。
    from config import DEVICE_TIMEZONE_OFFSET_HOURS  # noqa: PLC0415 - 延迟导入

    tz_offset = DEVICE_TIMEZONE_OFFSET_HOURS
    intervals_2m = _shift_intervals(raw_intervals_2m, tz_offset)
    intervals_8m = _shift_intervals(raw_intervals_8m, tz_offset)
    covered = _assign_coverage(analysis.segments, intervals_2m, intervals_8m)
    effective_tz = tz_offset if tz_offset is not None else round(_machine_utc_offset_hours())
    for payload in (coverage_2m, coverage_8m):
        payload["device_timezone_offset_hours"] = effective_tz

    gps_stable = float(analysis.gps_stable_distance_m)
    both = float(covered["both_m"])
    if not coverage_2m["present"]:
        small = coverage_2m["ignored_small_files"]
        issues.append(
            "2M 目录里只有占位文件、没有真实数据，2M 覆盖与有效里程按 0 计"
            if small else "没有识别到 2M 设备数据，2M 覆盖与有效里程按 0 计"
        )
    if not coverage_8m["present"]:
        small = coverage_8m["ignored_small_files"]
        issues.append(
            "8M 目录里只有占位文件、没有真实数据，8M 覆盖与有效里程按 0 计"
            if small else "没有识别到 8M 设备数据，8M 覆盖与有效里程按 0 计"
        )
    if gps_stable <= 0:
        issues.append("没有 GPS 稳定的相邻段，GPS 稳定里程与有效里程为 0（检查 GNSS 状态与轨迹连续性）")
    elif both <= 0:
        issues.append(
            "GPS 稳定里程存在，但没有任何一段同时被 GPS、2M、8M 覆盖，有效里程为 0"
        )

    pcap_dirs = [d.get("folder") for d in (info.get("pcap_dirs") or []) if d.get("folder")]
    pcap_bytes = sum(int(d.get("bytes") or 0) for d in (info.get("pcap_dirs") or []))
    if not pcap_dirs and pcap_files:
        pcap_bytes = 0  # 顶层散装 pcap：没有目录统计，交给文件数说明

    trajectory_payload = {
        "point_count": len(points),
        "original_point_count": original_point_count,
        "decimate_step": decimate_step,
        "segment_count": len(analysis.segments),
        "trip_count": int(analysis.trip_count),
        "gps_stable_points": int(analysis.gps_stable_point_count),
        "duration_s": round(points[-1].timestamp - points[0].timestamp, 1),
        "start_time": _format_seconds(points[0].timestamp),
        "end_time": _format_seconds(points[-1].timestamp),
        "source": source_kind,
    }

    result = {
        "ok": True,
        "data_root": str(root),
        "elapsed_s": round(time.perf_counter() - started, 2),
        "trajectory": trajectory_payload,
        "mileage": {
            "gps_stable_m": round(gps_stable, 3),
            "total_m": round(float(analysis.raw_distance_m), 3),
            "covered_2m_m": round(float(covered["covered_2m_m"]), 3),
            "covered_8m_m": round(float(covered["covered_8m_m"]), 3),
            # 有效里程 = GPS 稳定 ∩ 2M 录制 ∩ 8M 录制（三者全覆盖的交集）
            "effective_m": round(both, 3),
            "full_m": round(both, 3),
            "both_m": round(both, 3),
            # 任一覆盖（2M 或 8M）只作为参考，不再作为「有效里程」
            "any_m": round(float(covered["either_m"]), 3),
            "no_cover_m": round(float(covered["no_cover_m"]), 3),
            "effective_ratio_pct": round(both / gps_stable * 100.0, 1) if gps_stable > 0 else None,
            "any_ratio_pct": round(
                float(covered["either_m"]) / gps_stable * 100.0, 1
            ) if gps_stable > 0 else None,
            "both_ratio_pct": round(both / gps_stable * 100.0, 1) if gps_stable > 0 else None,
            "stable_segment_count": int(covered["stable_segments"]),
            "baseline_tolerance_m": baseline_tolerance_m,
        },
        "coverage": {
            "2m": coverage_2m,
            "8m": coverage_8m,
            # 设备文件名时间按哪个时区解释（默认 = 本机时区，不平移）
            "device_timezone_offset_hours": effective_tz,
            # 覆盖时间轴对比图的原始区间（epoch 秒，已合并、已按时区对齐）
            "timeline": _timeline_payload(
                points,
                analysis.segments,
                intervals_2m,
                intervals_8m,
            ),
        },
        "pcaps": {
            "file_count": len(pcap_files),
            "bytes": pcap_bytes,
            "dirs": pcap_dirs,
        },
        "files": files_meta[:50],
        "file_count": len(files_meta),
        # 被嗅探挡掉的二进制文件（如 qinji/xxx.txt 是 NovAtel 原始观测）
        "skipped_binary_files": binary_notes[:20],
        # 只回传本模块自己的提示：探测模块那条「将解析 GGA 语句」是给旧
        # pcap_gga 路径写的，这里走 GSOF49，带出去会误导用户。
        "warnings": warnings,
        "issues": issues,
    }
    return result


__all__ = [
    "DEFAULT_MAX_PCAP_FILES",
    "MileageAnalysisError",
    "analyze_directory",
    "rows_to_points",
    "scan_batch_dates",
]
