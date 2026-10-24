"""里程刷写分析模块（与参数验证完全独立）。

职责：分析一个数据目录里的轨迹/视频/点云数据，回答两个问题：
  1. **里程**：GPS 稳定里程、2M 里程、8M 里程、实际有效里程
  2. **覆盖**：2M / 8M 相机在各时段是否都有数据（缺一个即标记异常）

设计原则
--------
- 与参数验证（外参）完全解耦：独立的输入探测、独立的任务表、独立的 API 前缀、
  独立的输出目录，互不影响、互不引用。
- 复用项目里已经验证过的里程计算器（main.analyze_trajectory / trajectory_parser），
  不重写轨迹解析逻辑。
- 目录里可能同时有 $GPFPD 轨迹文件、2M/8M 设备目录，这里自动识别并分别处理。
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SERVICE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SERVICE_DIR.parent

# 找到里程计算器所在目录（main.py + trajectory_parser.py 等）。
# 两种部署形态：
#   1. 开发环境：service 与主项目源码同级 -> 用 PROJECT_DIR
#   2. 服务器部署：计算器被放在 verify_service/mileage_core/
_CORE_CANDIDATES = [
    SERVICE_DIR / "mileage_core",
    PROJECT_DIR,
    SERVICE_DIR,
]
MILEAGE_CORE_DIR = next(
    (p for p in _CORE_CANDIDATES if (p / "main.py").is_file() and (p / "trajectory_parser.py").is_file()),
    PROJECT_DIR,
)

# 复用主项目的里程计算器
if str(MILEAGE_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(MILEAGE_CORE_DIR))

from config import MIN_DEVICE_FILE_BYTES  # noqa: E402 - 依赖上面的 sys.path 注入

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

OUTPUT_DIR_NAME = "output"

# 设备文件每组约 60 秒，一个文件代表 [起始时刻, +60s] 这段有数据
GROUP_DURATION_S = 60

TRAJECTORY_EXTS = {".txt", ".csv"}
# pcap 的常见后缀；采集工具偶尔会写成 .pacp（拼写错误），两种都要认，
# 否则整个日期目录会被判成「没有轨迹来源」。
PCAP_EXTS = {".pcap", ".pacp"}
VIDEO_EXTS = {".mp4", ".avi", ".mkv", ".mov"}
POINT_EXTS = {".las", ".laz", ".pcd"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}

# 设备目录命名里用 2M / 8M 标识
DEVICE_PATTERNS = {
    "2M": re.compile(r"(^|[_\-\s])2M([_\-\s.]|$)", re.IGNORECASE),
    "8M": re.compile(r"(^|[_\-\s])8M([_\-\s.]|$)", re.IGNORECASE),
}

# 文件名里带时间戳，例如：
#   M57_2M_LYNK01_001_0917_1_20260917_142635_110.pack   （组起始时刻 + 序号）
#   clip_20260917_142635_000.mp4
#   1694601234.567.jpg                                  （秒.毫秒时间戳）
STAMP_RE = re.compile(r"(\d{8})[_-](\d{6})")
EPOCH_RE = re.compile(r"^(\d{10})\.(\d{1,6})$")
# 文件名末尾的「日期_时刻_序号」：兼容日期重复两次的设备命名，
# 例如 M57-2M_E5-001_20261003_20261003_180047_017.vin。
STAMP_TAIL_RE = re.compile(r"_(\d{8})[_-](\d{6})_(\d+)\.[^.]+$")


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------


@dataclass
class Coverage:
    """某类数据（2M/8M/相机）的时间覆盖。"""

    label: str = ""
    present: bool = False
    file_count: int = 0
    total_bytes: int = 0
    start: Optional[str] = None
    end: Optional[str] = None
    duration_s: float = 0.0
    folders: List[str] = field(default_factory=list)
    note: str = ""


@dataclass
class MileageAnalysis:
    task_id: str = ""
    status: str = "pending"
    data_root: str = ""
    output_root: str = ""

    # 里程结果（米）
    # 2M/8M 是传感器，本身没有「里程」概念：
    #   - 里程只来自轨迹（GPS 稳定里程）
    #   - 2M/8M 提供的是「时段覆盖」，用来判定哪些里程是实际有效的
    total_distance_m: float = 0.0        # 全部轨迹长度
    gps_stable_distance_m: float = 0.0   # GPS 稳定里程
    effective_distance_m: float = 0.0    # GPS稳定 ∩ 2M/8M 有数据 的里程
    distance_2m_covered_m: float = 0.0   # 落在 2M 覆盖时段内的 GPS 稳定里程
    distance_8m_covered_m: float = 0.0   # 落在 8M 覆盖时段内的 GPS 稳定里程
    distance_both_covered_m: float = 0.0  # 2M 与 8M 同时有数据
    distance_no_cover_m: float = 0.0     # GPS 稳定但 2M/8M 都没有数据
    raw_distance_m: float = 0.0
    ratio: float = 0.0
    point_count: int = 0
    trip_count: int = 0

    # 覆盖分析（时间区间）
    coverage_2m: Dict[str, Any] = field(default_factory=dict)
    coverage_8m: Dict[str, Any] = field(default_factory=dict)
    trajectory_coverage: Dict[str, Any] = field(default_factory=dict)

    # 判定
    verdict: str = ""
    issues: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    # 明细
    files: List[Dict[str, Any]] = field(default_factory=list)
    segments: List[Dict[str, Any]] = field(default_factory=list)
    artifacts: List[Dict[str, str]] = field(default_factory=list)

    log: List[str] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0
    duration_s: float = 0.0
    error: str = ""


@dataclass
class MileageOptions:
    baseline_tolerance_m: float = 0.5
    analyze_coverage: bool = True
    analyze_mileage: bool = True
    # 只看目录结构，不实际算里程
    probe_only: bool = False


# --------------------------------------------------------------------------
# 目录探测：识别哪些是轨迹文件、哪些属于 2M / 8M
# --------------------------------------------------------------------------


def _match_device(name: str, kind: str) -> bool:
    return bool(DEVICE_PATTERNS[kind].search(name))


def _parse_stamp_from_name(name: str) -> Optional[datetime]:
    """从文件名提取时间戳。

    设备文件常见命名是「组起始时刻 + 组序号」，例如
    M57_2M_..._20260917_142635_110.pack —— 142635 是这一组的起始时刻，
    末尾 110 是第几组（每组约 60 秒）。所以把序号乘回去才能得到该文件真实时刻。

    俄罗斯 E5 的命名日期会重复出现两次：
    M57-2M_E5-001_20261003_20261003_180047_017.vin
    这里必须先按「文件名末尾的 日期_时刻_序号」匹配（dataset_audit.START_RE 的思路）；
    否则普通 search 会命中「第一个日期 + 第二个日期的前 6 位」，算出的起始时刻
    会偏出十几个小时，2M/8M 覆盖区间跟着整体错位。
    """
    stem = os.path.splitext(name)[0]

    # 1) 秒.毫秒 形式的 unix 时间戳
    m = EPOCH_RE.match(stem)
    if m:
        try:
            return datetime.fromtimestamp(float(stem))
        except (ValueError, OSError, OverflowError):
            pass

    # 2) 文件名末尾的「日期_时刻_序号」（兼容日期重复两次的命名）
    tail_match = STAMP_TAIL_RE.search(name)
    if tail_match:
        try:
            base = datetime.strptime(
                tail_match.group(1) + tail_match.group(2), "%Y%m%d%H%M%S"
            )
        except ValueError:
            base = None
        if base is not None:
            try:
                offset = int(tail_match.group(3))
            except ValueError:
                offset = 0
            if 0 <= offset <= 100000:
                base = base + timedelta(seconds=60 * offset)
            return base

    # 3) 兜底：老式的「yyyymmdd_hhmmss」命名
    m = STAMP_RE.search(stem)
    if not m:
        return None
    try:
        base = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None

    # 时间戳之后若还有纯数字，视为组序号，每组约 60 秒
    tail = stem[m.end():]
    idx = re.search(r"(\d+)$", tail)
    if idx:
        try:
            offset = int(idx.group(1))
        except ValueError:
            offset = 0
        # 序号过大（如 20261001 这类）不当序号用
        if 0 <= offset <= 100000:
            base = base + timedelta(seconds=60 * offset)
    return base


# 设备目录下面偶尔会再套一层「会话」子目录，例如：
#   vin_device_2M/20260923-1_2M/M57-2M_E5-001_..._000.vin
#   vin_device_2M/20260923-2_2M/M57-2M_E5-001_..._040.vin
# 所以设备目录必须递归扫描，否则会把有真实数据的目录判成空的。
DEVICE_SCAN_MAX_DEPTH = 4
# 设备目录里可能混着惯导自己的数据目录，这些不参与相机覆盖统计。
DEVICE_SKIP_SUBDIR_PREFIXES = ("rtkimu", "qinji", "ap18", "output")


def _scan_folder(path: Path, exts: set, max_files: int = 200000) -> Dict[str, Any]:
    """递归统计设备目录下的文件数、大小、时间范围（最多 4 层子目录）。

    返回的 intervals 是该目录里每个文件对应的 [开始, 结束] 时间段
    （设备文件每组约 60 秒），用于和轨迹求交集判断「当时相机在不在录」。

    小于 ``MIN_DEVICE_FILE_BYTES`` 的文件只算「占位」：采集失败时目录里会留下
    4 字节的 .vin、308 字节的 .blf 这类空文件，它们只有文件名没有内容，
    不能当成「这一分钟在录」——否则有效里程会被算成非 0。
    """
    count = 0
    size = 0
    small_count = 0
    small_bytes = 0
    stamps: List[datetime] = []
    intervals: List[Tuple[datetime, datetime]] = []
    try:
        stack: List[Tuple[Path, int]] = [(path, 0)]
        while stack and count <= max_files:
            current, depth = stack.pop()
            try:
                entries = list(os.scandir(current))
            except OSError:
                continue
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if depth < DEVICE_SCAN_MAX_DEPTH and not entry.name.lower().startswith(
                            DEVICE_SKIP_SUBDIR_PREFIXES
                        ):
                            stack.append((Path(entry.path), depth + 1))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                suffix = os.path.splitext(entry.name)[1].lower()
                if suffix not in exts:
                    continue
                try:
                    file_size = entry.stat().st_size
                except OSError:
                    file_size = 0
                if file_size < MIN_DEVICE_FILE_BYTES:
                    small_count += 1
                    small_bytes += file_size
                    continue
                count += 1
                if count > max_files:
                    break
                size += file_size
                ts = _parse_stamp_from_name(entry.name)
                if ts:
                    stamps.append(ts)
                    intervals.append((ts, ts + timedelta(seconds=GROUP_DURATION_S)))
    except OSError as exc:
        return {"count": 0, "size": 0, "small_count": 0, "small_bytes": 0,
                "start": None, "end": None, "intervals": [], "error": str(exc)}
    return {
        "count": count,
        "size": size,
        "small_count": small_count,
        "small_bytes": small_bytes,
        "start": min(stamps) if stamps else None,
        "end": max(stamps) if stamps else None,
        "intervals": intervals,
        "error": None,
    }


def _read_cifs_mounts() -> List[Tuple[str, str]]:
    """读取服务器上的 CIFS 挂载，返回 [(share, mountpoint)]，如
    ("192.168.20.206/logs_new", "/mnt/logs_new")。

    直接读 /proc/mounts，所以以后运维新增了挂载，服务不用改代码也能映射。
    """
    mounts: List[Tuple[str, str]] = []
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3 or parts[2] != "cifs":
                    continue
                device = parts[0]  # //192.168.20.206/logs_new
                mountpoint = parts[1]
                # 反转义 /proc/mounts 里的八进制转义（\040 空格等）
                for code, ch in (("\\040", " "), ("\\011", "\t"), ("\\134", "\\")):
                    mountpoint = mountpoint.replace(code, ch)
                share = device.lstrip("/")
                mounts.append((share, mountpoint))
    except OSError:
        pass
    # 长的优先匹配，避免 /mnt/logs 抢了 /mnt/logs_new
    mounts.sort(key=lambda kv: len(kv[0]), reverse=True)
    return mounts


def normalize_path(raw: str) -> Path:
    """把各种写法统一成服务器上的真实路径。

    服务跑在 Linux 上，用户经常直接贴 Windows 共享路径。例如
    \\\\192.168.20.206\\logs_new 对应服务器上的 /mnt/logs_new。
    这里按服务器实际的 CIFS 挂载表做映射，而不是写死某几个盘。
    """
    text = (raw or "").strip().strip('"').strip("'")
    if not text:
        return Path(text)

    is_unc = text.startswith("\\\\") or text.startswith("//")
    if is_unc:
        # 统一成 server/share/rest 形式
        normalized = text.replace("\\", "/").lstrip("/")
        for share, mountpoint in _read_cifs_mounts():
            if normalized.lower() == share.lower():
                return Path(mountpoint)
            if normalized.lower().startswith(share.lower() + "/"):
                rest = normalized[len(share) + 1 :]
                return Path(f"{mountpoint}/{rest}")

        # 没有匹配到挂载：退回按「share 名同名目录」猜一个常见位置
        parts = normalized.split("/")
        if len(parts) >= 3:
            return Path("/mnt/" + "/".join(parts[1:]))

    return Path(text)


def _path_hint(raw: str) -> str:
    """当路径确实不存在时，给出可操作的建议。"""
    if raw.startswith("\\\\") or raw.startswith("//"):
        mounts = _read_cifs_mounts()
        examples = "；".join(f"\\\\{share.split('/')[0]}\\{share.split('/')[1]} -> {mp}"
                             for share, mp in mounts[:4] if "/" in share)
        return (
            "这是 Windows 共享路径，服务器上对应的是挂载点。"
            f"已知映射示例：{examples}"
        )
    return ""


def probe_mileage_dataset(data_root: Path) -> Dict[str, Any]:
    """探测目录里里程分析所需的数据，并区分 2M / 8M / 轨迹。"""
    info: Dict[str, Any] = {
        "data_root": str(data_root),
        "exists": data_root.exists(),
        "is_dir": data_root.is_dir(),
        "trajectory_files": [],
        "pcap_dirs": [],
        "pcap_files": [],
        "device_2m": {"folders": [], "files": 0, "bytes": 0, "start": None, "end": None, "intervals": []},
        "device_8m": {"folders": [], "files": 0, "bytes": 0, "start": None, "end": None, "intervals": []},
        "other_folders": [],
        "ready": False,
        "problems": [],
        "warnings": [],
    }
    if not data_root.exists():
        hint = _path_hint(str(data_root))
        info["problems"].append(f"目录不存在: {data_root}" + (f"。{hint}" if hint else ""))
        return info
    if not data_root.is_dir():
        info["problems"].append(f"不是目录: {data_root}")
        return info

    try:
        entries = sorted(data_root.iterdir())
    except OSError as exc:
        info["problems"].append(f"无法读取目录: {exc}")
        return info

    # 1. 顶层轨迹文件（跳过 0 字节空文件：有些目录会放「数据验证不可用.txt」这类占位）
    for entry in entries:
        if not entry.is_file():
            continue
        if entry.suffix.lower() in PCAP_EXTS:
            # 顶层直接放 pcap 的目录也认（用户可能把 AP18 目录指成根目录）
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            if size <= 0:
                info.setdefault("ignored_empty_files", []).append(entry.name)
                continue
            info["pcap_files"].append(str(entry))
            info["pcap_dirs"].append({"folder": ".", "files": 1, "bytes": size})
            continue
        if entry.suffix.lower() in TRAJECTORY_EXTS:
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            if size <= 0:
                info.setdefault("ignored_empty_files", []).append(entry.name)
                continue
            info["trajectory_files"].append({"name": entry.name, "size": size})

    # 2. 子目录分类：2M / 8M / 其他
    for entry in entries:
        if not entry.is_dir():
            continue
        is_2m = _match_device(entry.name, "2M")
        is_8m = _match_device(entry.name, "8M")
        # output 是我们自己写结果的地方，不能当成数据源，否则二次分析会读到自己产出的 CSV
        if entry.name == OUTPUT_DIR_NAME:
            info["output_dir"] = str(entry)
            continue
        if not (is_2m or is_8m):
            info["other_folders"].append(entry.name)
            # 其他子目录里若有 pcap，视为 AP18 惯导轨迹来源。
            # 注意采集工具偶尔把后缀写成 .pacp（拼写错误），也要认。
            pcap_files = sorted(
                path
                for path in entry.iterdir()
                if path.is_file() and path.suffix.lower() in PCAP_EXTS
            )
            if pcap_files:
                info["pcap_dirs"].append(
                    {
                        "folder": entry.name,
                        "files": len(pcap_files),
                        "bytes": sum(f.stat().st_size for f in pcap_files if f.is_file()),
                    }
                )
                info["pcap_files"].extend(str(f) for f in pcap_files)
            continue
        kind = "device_2m" if is_2m else "device_8m"
        stats = _scan_folder(entry, VIDEO_EXTS | POINT_EXTS | IMAGE_EXTS | {".pack", ".blf", ".vin", ".csv", ".txt"})
        info[kind]["folders"].append(entry.name)
        info[kind]["files"] += stats["count"]
        info[kind]["bytes"] += stats["size"]
        info[kind]["small_files"] = info[kind].get("small_files", 0) + stats.get("small_count", 0)
        info[kind]["small_bytes"] = info[kind].get("small_bytes", 0) + stats.get("small_bytes", 0)
        info[kind]["intervals"].extend(stats.get("intervals", []))
        for key in ("start", "end"):
            if stats[key]:
                cur = info[kind][key]
                if cur is None:
                    info[kind][key] = stats[key]
                elif key == "start":
                    info[kind][key] = min(cur, stats[key])
                else:
                    info[kind][key] = max(cur, stats[key])

    # 3. 也允许 2M/8M 标识出现在顶层的视频/点云文件里
    for entry in entries:
        if not entry.is_file() or entry.suffix.lower() not in (VIDEO_EXTS | POINT_EXTS):
            continue
        kind = None
        if _match_device(entry.name, "2M"):
            kind = "device_2m"
        elif _match_device(entry.name, "8M"):
            kind = "device_8m"
        if kind:
            info[kind]["files"] += 1
            try:
                info[kind]["bytes"] += entry.stat().st_size
            except OSError:
                pass
            ts = _parse_stamp_from_name(entry.name)
            if ts:
                info[kind]["intervals"].append((ts, ts + timedelta(seconds=GROUP_DURATION_S)))
                if info[kind]["start"] is None or ts < info[kind]["start"]:
                    info[kind]["start"] = ts
                if info[kind]["end"] is None or ts > info[kind]["end"]:
                    info[kind]["end"] = ts

    has_traj = bool(info["trajectory_files"])
    has_pcap = bool(info["pcap_files"])
    has_2m = info["device_2m"]["files"] > 0
    has_8m = info["device_8m"]["files"] > 0

    if not has_traj and not has_pcap:
        info["problems"].append(
            "没有找到轨迹来源：既没有 .txt/.csv 轨迹文件，也没有 AP18 pcap 目录"
        )
    if has_pcap:
        info["warnings"].append(
            f"轨迹来自 AP18 pcap（{len(info['pcap_files'])} 个文件），将解析其中的 GGA 语句，耗时较长"
        )
    if not has_2m:
        small_2m = info["device_2m"].get("small_files", 0)
        if small_2m:
            info["warnings"].append(
                f"2M 目录里只有 {small_2m} 个占位文件（<{MIN_DEVICE_FILE_BYTES // 1024} KB），"
                "没有真实数据，不能算覆盖"
            )
        else:
            info["warnings"].append("没有识别到 2M 数据目录/文件")
    if not has_8m:
        small_8m = info["device_8m"].get("small_files", 0)
        if small_8m:
            info["warnings"].append(
                f"8M 目录里只有 {small_8m} 个占位文件（<{MIN_DEVICE_FILE_BYTES // 1024} KB），"
                "没有真实数据，不能算覆盖"
            )
        else:
            info["warnings"].append("没有识别到 8M 数据目录/文件")
    if not has_2m and not has_8m:
        info["problems"].append("2M 和 8M 都没有，无法做覆盖分析")

    info["ready"] = not info["problems"]
    # dataclass 里的 datetime 不能被序列化，转字符串
    for kind in ("device_2m", "device_8m"):
        for key in ("start", "end"):
            val = info[kind][key]
            info[kind][key] = val.strftime("%Y-%m-%d %H:%M:%S") if isinstance(val, datetime) else None
    return info


# --------------------------------------------------------------------------
# 执行分析
# --------------------------------------------------------------------------


def _to_coverage(label: str, dev: Dict[str, Any], present: bool) -> Coverage:
    cov = Coverage(
        label=label,
        present=present,
        file_count=dev.get("files", 0),
        total_bytes=dev.get("bytes", 0),
        folders=list(dev.get("folders", [])),
        start=dev.get("start"),
        end=dev.get("end"),
    )
    if cov.start and cov.end:
        try:
            a = datetime.strptime(cov.start, "%Y-%m-%d %H:%M:%S")
            b = datetime.strptime(cov.end, "%Y-%m-%d %H:%M:%S")
            cov.duration_s = (b - a).total_seconds()
        except ValueError:
            pass
    return cov


def _covers(intervals, when: datetime) -> bool:
    """判断某个时刻是否落在任一 [start, end) 区间内（interval 可为 _SortedIntervals）。"""
    if not intervals:
        return False
    checker = intervals if isinstance(intervals, _SortedIntervals) else _SortedIntervals(intervals)
    return checker.covers(when)


class _SortedIntervals:
    """按区间起始时间排好序的区间集合，支持快速点覆盖判断。"""

    __slots__ = ("_starts", "_ends", "_max_end")

    def __init__(self, intervals: List[Tuple[datetime, datetime]]):
        items = sorted(intervals, key=lambda iv: iv[0])
        self._starts = [iv[0] for iv in items]
        self._ends = [iv[1] for iv in items]
        # 记录到每个位置为止的最大结束时间，二分后只需比较一次
        max_end = []
        cur = None
        for end in self._ends:
            cur = end if cur is None or end > cur else cur
            max_end.append(cur)
        self._max_end = max_end

    def covers(self, when: datetime) -> bool:
        import bisect

        idx = bisect.bisect_right(self._starts, when) - 1
        if idx < 0:
            return False
        return self._max_end[idx] > when


def run_mileage(
    task_id: str,
    data_root: Path,
    output_root: Path,
    options: Optional[MileageOptions] = None,
    progress=None,
) -> MileageAnalysis:
    """执行一次里程分析。与参数验证互不干扰，可并发跑。"""
    options = options or MileageOptions()
    result = MileageAnalysis(task_id=task_id, data_root=str(data_root), output_root=str(output_root))
    result.started_at = time.time()

    def log(msg: str) -> None:
        result.log.append(f"{time.strftime('%H:%M:%S')} {msg}")

    def report(step: str, percent: int) -> None:
        if progress:
            progress(step=step, percent=percent)

    info = probe_mileage_dataset(data_root)
    result.files = list(info["trajectory_files"])

    if not info["ready"]:
        result.status = "failed"
        result.verdict = "数据目录不可用于里程分析"
        result.issues.extend(info["problems"])
        result.finished_at = time.time()
        result.duration_s = round(result.finished_at - result.started_at, 2)
        return result

    result.warnings.extend(info["warnings"])

    try:
        output_root.mkdir(parents=True, exist_ok=True)

        # ---- 1. 覆盖分析 ----
        if options.analyze_coverage:
            report("覆盖分析", 10)
            log("开始 2M / 8M 覆盖分析")
            result.coverage_2m = asdict(_to_coverage("2M", info["device_2m"], info["device_2m"]["files"] > 0))
            result.coverage_8m = asdict(_to_coverage("8M", info["device_8m"], info["device_8m"]["files"] > 0))
            traj_start = info["device_2m"]["start"]
            result.trajectory_coverage = {
                "trajectory_files": len(info["trajectory_files"]),
            }
            log("覆盖分析完成")

        # ---- 2. 里程计算 ----
        if options.analyze_mileage:
            report("里程计算", 40)
            log("开始里程计算")
            from main import analyze_trajectory

            traj_paths = [
                data_root / f["name"]
                for f in info["trajectory_files"]
                if (data_root / f["name"]).is_file() and (data_root / f["name"]).stat().st_size > 0
            ]

            # AP18 pcap：先抽出 GGA 转成标准 CSV 轨迹，再走同一套里程计算
            if not traj_paths and info.get("pcap_files"):
                report("解析 AP18 pcap", 30)
                log(f"从 {len(info['pcap_files'])} 个 pcap 提取 GGA 轨迹")
                from pcap_gga import extract_gga_points

                points = extract_gga_points(info["pcap_files"])
                if not points:
                    raise RuntimeError("未能从 AP18 pcap 中提取到任何 GGA 定位语句")
                log(f"提取到 {len(points)} 个 GGA 轨迹点")

                csv_path = output_root / "ap18_gga_trajectory.csv"
                with open(csv_path, "w", newline="", encoding="utf-8") as fh:
                    import csv as _csv

                    w = _csv.writer(fh)
                    for p in points:
                        w.writerow(
                            [f"{p['ts']:.3f}", f"{p['lat']:.9f}", f"{p['lon']:.9f}",
                             f"{p['alt']:.3f}", p["quality"], ""]
                        )
                traj_paths = [csv_path]
                log(f"已转换轨迹: {csv_path.name}")

            all_segments: List[Dict[str, Any]] = []
            for path in traj_paths:
                if not path.is_file() or path.stat().st_size <= 0:
                    log(f"跳过空轨迹文件: {path.name}")
                    continue
                log(f"解析轨迹: {path.name}")
                analysis = analyze_trajectory(
                    str(path),
                    baseline_tolerance_m=options.baseline_tolerance_m,
                    verbose=False,
                )
                result.point_count += analysis.point_count
                result.trip_count += analysis.trip_count
                result.gps_stable_distance_m += analysis.gps_stable_distance_m
                result.total_distance_m += analysis.raw_distance_m
                result.raw_distance_m += analysis.raw_distance_m
                for seg in analysis.segments:
                    all_segments.append(
                        {
                            "file": path.name,
                            "trip_no": seg.trip_no,
                            "start_line": seg.start_line,
                            "end_line": seg.end_line,
                            "start_ts": seg.start_timestamp,
                            "end_ts": seg.end_timestamp,
                            "time_diff_s": round(seg.time_diff_s, 3),
                            "distance_m": round(seg.distance_m, 3),
                            "counted_gps_stable": seg.counted_gps_stable,
                            "abnormal": seg.abnormal,
                            "abnormal_reason": seg.abnormal_reason,
                        }
                    )
                # 轨迹文件自身的 2M/8M 基线提示与传感器覆盖无关，不作为异常
                result.issues = [r for r in result.issues if path.name not in r]

            # ---- 按时间交集计算「2M/8M 当时是否在录」----
            report("覆盖交集计算", 70)
            iv2 = info["device_2m"].get("intervals") or []
            iv8 = info["device_8m"].get("intervals") or []
            log(f"2M 有效时段 {len(iv2)} 段，8M 有效时段 {len(iv8)} 段")
            set2 = _SortedIntervals(iv2)
            set8 = _SortedIntervals(iv8)

            cov2 = cov8 = covboth = nocov = 0.0
            for seg in all_segments:
                if not seg["counted_gps_stable"]:
                    continue
                mid = (seg["start_ts"] + seg["end_ts"]) / 2
                seg_dt = datetime.fromtimestamp(mid)
                in2 = set2.covers(seg_dt)
                in8 = set8.covers(seg_dt)
                seg["in_2m"] = in2
                seg["in_8m"] = in8
                dist = seg["distance_m"]
                if in2:
                    cov2 += dist
                if in8:
                    cov8 += dist
                if in2 and in8:
                    covboth += dist
                if in2 or in8:
                    # 2M/8M 任一在录即视为实际有效
                    result.effective_distance_m += dist
                else:
                    nocov += dist

            result.distance_2m_covered_m = cov2
            result.distance_8m_covered_m = cov8
            result.distance_both_covered_m = covboth
            result.distance_no_cover_m = nocov
            result.segments = all_segments
            log(
                f"里程计算完成：GPS稳定 {result.gps_stable_distance_m/1000:.3f} km，"
                f"实际有效 {result.effective_distance_m/1000:.3f} km"
            )

        if result.gps_stable_distance_m > 0:
            result.ratio = result.effective_distance_m / result.gps_stable_distance_m * 100

        # ---- 3. 判定 ----
        report("汇总判定", 90)
        _build_mileage_verdict(result)

        # ---- 4. 导出（先算好耗时，报告里才能显示）----
        result.finished_at = time.time()
        result.duration_s = round(result.finished_at - result.started_at, 2)
        result.artifacts = _export_mileage(result, output_root)
        result.status = "success"

    except Exception as exc:
        result.status = "failed"
        result.error = f"{type(exc).__name__}: {exc}"
        result.issues.append(f"里程分析执行失败：{result.error}")
        log(traceback.format_exc())
        # 失败时也给一份报告，写明失败原因
        try:
            result.artifacts = _export_mileage(result, output_root)
        except Exception:
            pass

    result.finished_at = time.time()
    result.duration_s = round(result.finished_at - result.started_at, 2)
    return result


def _build_mileage_verdict(r: MileageAnalysis) -> None:
    """给出里程 + 覆盖的综合结论。"""
    has_2m = r.coverage_2m.get("present", False)
    has_8m = r.coverage_8m.get("present", False)

    if not has_2m:
        r.issues.append("缺少 2M 数据（覆盖分析判定为缺失）")
    if not has_8m:
        r.issues.append("缺少 8M 数据（覆盖分析判定为缺失）")

    if has_2m and has_8m:
        cov_part = "2M/8M 数据齐全"
    elif has_2m or has_8m:
        missing = "8M" if has_2m else "2M"
        cov_part = f"缺少 {missing} 数据"
    else:
        cov_part = "2M/8M 均缺失"

    km = r.effective_distance_m / 1000
    gps_km = r.gps_stable_distance_m / 1000

    if r.effective_distance_m > 0 and not r.issues:
        r.verdict = f"里程分析通过：{cov_part}，实际有效里程 {km:.3f} km"
    elif r.effective_distance_m > 0:
        r.verdict = f"里程分析存在异常：{cov_part}，实际有效里程 {km:.3f} km"
    else:
        r.verdict = (
            f"里程分析不通过：{cov_part}，"
            f"GPS 稳定 {gps_km:.3f} km 中没有落在 2M/8M 录制时段的里程"
        )


def build_html_report(r: MileageAnalysis) -> str:
    """生成自包含 HTML 报告（单文件，无外部依赖）。"""
    import html as _html

    def e(v) -> str:
        return _html.escape(str(v if v is not None else ""))

    def km(m: float) -> str:
        return f"{m / 1000:,.3f}"

    def pct(a: float, b: float) -> str:
        return f"{(a / b * 100):.2f}%" if b else "0.00%"

    def dur(sec: float) -> str:
        sec = int(sec or 0)
        h, rem = divmod(sec, 3600)
        m, s = divmod(rem, 60)
        return f"{h}小时{m:02d}分{s:02d}秒"

    def size(b: float) -> str:
        n = float(b or 0)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024:
                return f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} PB"

    ok = r.status == "success" and r.effective_distance_m > 0 and not r.issues
    badge_cls = "ok" if ok else "bad"
    badge_txt = "通过" if ok else ("执行失败" if r.status == "failed" else "存在异常")

    def issues_html() -> str:
        if not r.issues and not r.error:
            return ""
        items = list(r.issues)
        if r.error:
            items.insert(0, r.error)
        return "<div class='box bad'><strong>问题：</strong><br>" + "<br>".join(e(i) for i in items) + "</div>"

    def warns_html() -> str:
        if not r.warnings:
            return ""
        return "<div class='box note'><strong>说明：</strong><br>" + "<br>".join(e(w) for w in r.warnings) + "</div>"

    # 行程分段表
    trips: Dict[int, Dict[str, Any]] = {}
    for seg in r.segments:
        t = trips.setdefault(seg["trip_no"], {"dist": 0.0, "stable": 0.0, "eff": 0.0,
                                             "start": seg["start_ts"], "end": seg["end_ts"]})
        t["dist"] += seg["distance_m"]
        if seg["counted_gps_stable"]:
            t["stable"] += seg["distance_m"]
            if seg.get("in_2m") or seg.get("in_8m"):
                t["eff"] += seg["distance_m"]
        t["start"] = min(t["start"], seg["start_ts"])
        t["end"] = max(t["end"], seg["end_ts"])

    trip_rows = ""
    for no in sorted(trips):
        t = trips[no]
        trip_rows += (
            "<tr>"
            f"<td class='num'>{no}</td>"
            f"<td>{datetime.fromtimestamp(t['start']):%m-%d %H:%M:%S}</td>"
            f"<td>{datetime.fromtimestamp(t['end']):%m-%d %H:%M:%S}</td>"
            f"<td class='num'>{km(t['dist'])}</td>"
            f"<td class='num'>{km(t['stable'])}</td>"
            f"<td class='num'>{km(t['eff'])}</td>"
            f"<td class='num'>{pct(t['eff'], t['stable'])}</td>"
            "</tr>"
        )

    c2, c8 = r.coverage_2m, r.coverage_8m

    def cov_row(label: str, cov: Dict[str, Any]) -> str:
        if not cov or not cov.get("present"):
            return (f"<tr><td>{label}</td><td><span class='pill bad'>未检测到</span></td>"
                    "<td>—</td><td>—</td><td class='num'>0</td><td class='num'>0 B</td></tr>")
        return (
            f"<tr><td>{label}</td><td><span class='pill ok'>有数据</span></td>"
            f"<td>{e(cov.get('start') or '—')}</td>"
            f"<td>{e(cov.get('end') or '—')}</td>"
            f"<td class='num'>{dur(cov.get('duration_s', 0))}</td>"
            f"<td class='num'>{cov.get('file_count', 0):,} 个 / {size(cov.get('total_bytes', 0))}</td>"
            "</tr>"
        )

    # 覆盖条（按轨迹总时长画）
    def lane(label: str, cov: Dict[str, Any], cls: str) -> str:
        present = bool(cov and cov.get("present"))
        width = 100 if present else 0
        return (
            f"<div class='lane'><strong>{label}</strong>"
            f"<div class='track'><div class='fill {cls}' style='width:{width}%'>"
            f"{'有数据' if present else '无数据'}</div></div>"
            f"<span class='hint'>{e(cov.get('start') or '')} → {e(cov.get('end') or '')}</span></div>"
        )

    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>里程分析报告 · {e(r.data_root.split('/')[-1])}</title>
<style>
:root{{--bg:#f4f6f8;--panel:#fff;--text:#18212b;--muted:#667481;--line:#dfe5ea;--blue:#276ef1;--green:#16845b;--orange:#d97706;--red:#c43d3d}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.6 "Microsoft YaHei",Arial,sans-serif}}
.wrap{{max-width:1080px;margin:0 auto;padding:28px}}
h1{{font-size:25px;margin:0 0 4px}} .sub{{color:var(--muted);margin-bottom:20px;word-break:break-all}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px;margin-bottom:16px;box-shadow:0 4px 16px rgba(22,31,39,.05)}}
.card h2{{font-size:17px;margin:0 0 14px}}
.stats{{display:flex;gap:14px;flex-wrap:wrap}}
.stat{{flex:1 1 190px;border:1px solid var(--line);border-radius:10px;padding:14px}}
.stat .lb{{color:var(--muted);font-size:12px}} .stat .vl{{font-size:24px;font-weight:600;margin-top:2px}}
.big{{font-size:30px;color:var(--green)}}
table{{width:100%;border-collapse:collapse;margin-top:6px}}
th,td{{padding:9px 10px;border-bottom:1px solid var(--line);text-align:left;font-size:13px}}
th{{color:var(--muted);background:#f8fafb;font-weight:600}} .num{{text-align:right;font-variant-numeric:tabular-nums}}
.pill{{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;font-weight:600}}
.ok{{background:#e6f6ee;color:var(--green)}} .bad{{background:#fdeaea;color:var(--red)}} .warn{{background:#fff4e0;color:var(--orange)}}
.box{{border-left:4px solid;padding:11px 14px;border-radius:6px;margin:10px 0}}
.box.bad{{border-color:var(--red);background:#fff5f5}} .box.note{{border-color:var(--orange);background:#fff9ed}}
.box.good{{border-color:var(--green);background:#f2fbf7}}
.lane{{display:grid;grid-template-columns:56px 1fr 300px;gap:10px;align-items:center;margin:10px 0}}
.track{{height:24px;background:#edf1f4;border-radius:6px;overflow:hidden}}
.fill{{height:100%;display:flex;align-items:center;padding:0 8px;color:#fff;font-size:11px;border-radius:6px}}
.c2{{background:var(--blue)}} .c8{{background:var(--green)}}
.hint{{color:var(--muted);font-size:12px}}
.footer{{color:var(--muted);font-size:12px;margin:22px 0 6px}}
</style></head><body><div class="wrap">

<h1>里程分析报告</h1>
<div class="sub">数据目录：{e(r.data_root)}<br>生成时间：{generated}　耗时 {r.duration_s:.1f} 秒</div>

<div class="card">
  <h2>结论 <span class="pill {badge_cls}">{badge_txt}</span></h2>
  <div class="box {'good' if ok else 'bad'}"><strong>{e(r.verdict)}</strong></div>
  {issues_html()}
  {warns_html()}
</div>

<div class="card">
  <h2>里程结果</h2>
  <div class="stats">
    <div class="stat"><div class="lb">实际有效里程</div><div class="vl big">{km(r.effective_distance_m)} km</div><div class="hint">占 GPS 稳定里程 {r.ratio:.2f}%</div></div>
    <div class="stat"><div class="lb">GPS 稳定里程</div><div class="vl">{km(r.gps_stable_distance_m)} km</div><div class="hint">轨迹中定位稳定的部分</div></div>
    <div class="stat"><div class="lb">全部轨迹长度</div><div class="vl">{km(r.total_distance_m)} km</div><div class="hint">{r.point_count:,} 个点 / {r.trip_count} 段行程</div></div>
  </div>
  <div class="hint" style="margin-top:14px">
    实际有效里程 = 该段 GPS 稳定 <b>且</b> 当时 2M 或 8M 相机正在录制 的里程。
  </div>
  <table><thead><tr>
    <th>统计项</th><th class="num">里程 (km)</th><th class="num">占 GPS 稳定里程</th>
  </tr></thead><tbody>
    <tr><td>GPS 稳定且 2M 在录</td><td class="num">{km(r.distance_2m_covered_m)}</td><td class="num">{pct(r.distance_2m_covered_m, r.gps_stable_distance_m)}</td></tr>
    <tr><td>GPS 稳定且 8M 在录</td><td class="num">{km(r.distance_8m_covered_m)}</td><td class="num">{pct(r.distance_8m_covered_m, r.gps_stable_distance_m)}</td></tr>
    <tr><td>GPS 稳定且 2M/8M 同时在录</td><td class="num">{km(r.distance_both_covered_m)}</td><td class="num">{pct(r.distance_both_covered_m, r.gps_stable_distance_m)}</td></tr>
    <tr><td>GPS 稳定但无任何相机数据</td><td class="num">{km(r.distance_no_cover_m)}</td><td class="num">{pct(r.distance_no_cover_m, r.gps_stable_distance_m)}</td></tr>
    <tr><td><strong>实际有效里程</strong></td><td class="num"><strong>{km(r.effective_distance_m)}</strong></td><td class="num"><strong>{r.ratio:.2f}%</strong></td></tr>
  </tbody></table>
</div>

<div class="card">
  <h2>2M / 8M 相机覆盖时段</h2>
  <div class="hint" style="margin-bottom:10px">2M/8M 是传感器，本身没有「里程」概念，只提供录制时段。</div>
  {lane("2M", c2, "c2")}
  {lane("8M", c8, "c8")}
  <table><thead><tr>
    <th>传感器</th><th>状态</th><th>开始</th><th>结束</th><th class="num">时长</th><th class="num">文件数 / 容量</th>
  </tr></thead><tbody>
    {cov_row("2M", c2)}
    {cov_row("8M", c8)}
  </tbody></table>
</div>

<div class="card">
  <h2>分段里程</h2>
  <table><thead><tr>
    <th class="num">行程</th><th>开始</th><th>结束</th>
    <th class="num">轨迹 (km)</th><th class="num">GPS稳定 (km)</th><th class="num">实际有效 (km)</th><th class="num">有效率</th>
  </tr></thead><tbody>{trip_rows or '<tr><td colspan="7" class="hint">无数据</td></tr>'}</tbody></table>
</div>

<div class="footer">本报告由里程分析服务自动生成。GPS 稳定判定：GGA 定位质量为 1/2/4/5；设备每组数据约 60 秒。</div>
</div></body></html>"""


def _export_mileage(r: MileageAnalysis, output_root: Path) -> List[Dict[str, str]]:
    """只输出一份自包含 HTML 报告。"""
    output_root.mkdir(parents=True, exist_ok=True)
    path = output_root / "mileage_report.html"
    path.write_text(build_html_report(r), encoding="utf-8")
    return [{"name": path.name, "size": path.stat().st_size, "kind": "html"}]


# --------------------------------------------------------------------------
# 任务管理（独立于参数验证的任务表）
# --------------------------------------------------------------------------

MILEAGE_TASKS: Dict[str, MileageAnalysis] = {}
_MILEAGE_LOCK = threading.Lock()
_MILEAGE_PROGRESS: Dict[str, Dict[str, Any]] = {}


def create_mileage_task(
    data_root: Path,
    output_root: Path,
    options: Optional[MileageOptions] = None,
) -> str:
    task_id = uuid.uuid4().hex[:12]
    with _MILEAGE_LOCK:
        MILEAGE_TASKS[task_id] = MileageAnalysis(
            task_id=task_id, status="queued", data_root=str(data_root), output_root=str(output_root)
        )
    _MILEAGE_PROGRESS[task_id] = {"status": "queued", "percent": 0, "step": "排队中"}

    def _progress(**kw):
        state = _MILEAGE_PROGRESS.setdefault(task_id, {})
        state.update(kw)
        state["status"] = "running"
        state["updated_at"] = time.time()

    def _run():
        try:
            res = run_mileage(task_id, data_root, output_root, options, progress=_progress)
        except Exception as exc:
            with _MILEAGE_LOCK:
                t = MILEAGE_TASKS[task_id]
                t.status = "failed"
                t.error = str(exc)
            _MILEAGE_PROGRESS[task_id] = {"status": "failed", "message": str(exc), "percent": 0}
            return
        with _MILEAGE_LOCK:
            MILEAGE_TASKS[task_id] = res
        _MILEAGE_PROGRESS[task_id] = {
            "status": res.status,
            "percent": 100,
            "step": "完成",
            "message": res.verdict,
            "updated_at": time.time(),
        }

    threading.Thread(target=_run, name=f"mileage-{task_id}", daemon=True).start()
    return task_id


def get_mileage_task(task_id: str) -> Optional[MileageAnalysis]:
    return MILEAGE_TASKS.get(task_id)


def get_mileage_progress(task_id: str) -> Dict[str, Any]:
    return dict(_MILEAGE_PROGRESS.get(task_id, {}))


def list_mileage_tasks() -> List[Dict[str, Any]]:
    with _MILEAGE_LOCK:
        items = list(MILEAGE_TASKS.values())
    items.sort(key=lambda r: r.started_at or 0, reverse=True)
    return [
        {
            "task_id": r.task_id,
            "status": r.status,
            "verdict": r.verdict,
            "data_root": r.data_root,
            "started_at": r.started_at,
            "duration_s": r.duration_s,
        }
        for r in items
    ]
