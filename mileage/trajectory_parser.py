# trajectory_parser.py
import os
import math
from dataclasses import dataclass, field
from typing import Optional

from config import (
    BASELINE_TARGETS_M,
    BASELINE_TOLERANCE_M,
    GGA_STABLE_QUALITY_CODES,
    GPFPD_VARIANT_STABLE_STATUS_CODES,
    GPS_EPOCH_UNIX_SECONDS,
    GPS_UTC_LEAP_SECONDS,
    GSOF_STABLE_GNSS_CODES,
    GPS_STABLE_STATUS_CODES,
    INPUT_DIR,
    SEPARATORS,
)

# 状态来源标签。不同来源的状态码是不同编码空间，不能混用稳定集合：
#   GPFPD 解算状态（1/2/11/12）、GGA 定位质量（1/2/4/5）、CSV 未知来源（取并集）、
#   AP18 GSOF49 的 GNSS 状态（1/2/3/4/5，0 无定位、6 为 DR 推算），
#   以及 rtkimu 这类 GPFPD 变种（08 正常 / 09 静止）。
STATUS_LABEL_GPFPD = "GPFPD解算状态"
STATUS_LABEL_GPFPD_VARIANT = "GPFPD解算状态(变种)"
STATUS_LABEL_GGA = "GGA定位质量"
STATUS_LABEL_CSV = "CSV状态列"
STATUS_LABEL_GSOF = "AP18 GNSS状态"

# GPFPD 解算状态码（按 1/2/11/12 视为稳定，见 config.GPS_STABLE_STATUS_CODES）
SOLUTION_STATUS_TEXT = {
    0: "初始化",
    1: "GPS位置/速度/航向有效",
    2: "GPS位置/速度有效",
    3: "纯惯性模式",
    11: "GPS差分/速度/航向有效",
    12: "GPS差分有效",
}

# GPFPD 变种（rtkimu/俄罗斯 E5 等设备）状态文本：
# 驾驶中恒为 08，停车/静止时穿插 09（速度≈0、位置冻结）。
GPFPD_VARIANT_STATUS_TEXT = {
    0: "初始化",
    1: "GPS 有效",
    2: "差分有效",
    8: "组合导航正常",
    9: "静止/零速修正",
    11: "差分 GPS 有效",
    12: "差分有效",
}

# GGA 定位质量文本
GGA_QUALITY_TEXT = {
    0: "定位无效",
    1: "单点定位",
    2: "差分定位",
    4: "RTK固定",
    5: "RTK浮动",
    6: "定位估算",
}

# AP18 GSOF49 GNSS 状态文本（与 parse_ap18_pcap.GNSS_STATUS 保持一致）
GSOF_GNSS_TEXT = {
    0: "无定位",
    1: "GNSS SPS",
    2: "差分 GPS",
    3: "GNSS PPS",
    4: "RTK 固定",
    5: "RTK 浮动",
    6: "DR 航位推算",
}

# 文件格式标签
FORMAT_GPFPD = "GPFPD"
FORMAT_NMEA = "GGA"
FORMAT_CSV = "CSV"


@dataclass
class ParseIssue:
    line_no: int
    reason: str
    content: str


@dataclass
class TrajectoryLoadResult:
    points: list["TrajectoryPoint"] = field(default_factory=list)
    issues: list[ParseIssue] = field(default_factory=list)
    file_type: str = "未知"
    format: str = "未知"
    path: str = ""
    # 文件里出现过的状态码 -> 行数，用于“GPS状态是否存在/稳定”
    status_counts: dict = field(default_factory=dict)


class TrajectoryPoint:
    """单个WGS84位姿点数据结构，东南亚/欧洲通用"""

    def __init__(
        self,
        timestamp: float,
        lon: float,
        lat: float,
        alt: float,
        *,
        line_no: int = 0,
        baseline_m: Optional[float] = None,
        solution_status: Optional[int] = None,
        checksum_valid: Optional[bool] = None,
        parse_warnings: Optional[list[str]] = None,
        status_label: Optional[str] = None,
        has_status_field: bool = False,
    ):
        self.timestamp = timestamp
        self.lon = lon
        self.lat = lat
        self.alt = alt
        self.line_no = line_no
        self.baseline_m = baseline_m
        self.solution_status = solution_status
        self.checksum_valid = checksum_valid
        self.parse_warnings = parse_warnings or []
        self.status_label = status_label
        # 该行是否包含可解析的GPS状态字段（区分“解析失败”和“压根没有状态字段”）
        self.has_status_field = has_status_field

    @property
    def solution_status_text(self) -> str:
        if not self.has_status_field:
            return "无状态字段"
        if self.solution_status is None:
            return "状态无法解析"
        if self.status_label == STATUS_LABEL_GSOF:
            return GSOF_GNSS_TEXT.get(
                self.solution_status, f"未知GNSS状态({self.solution_status})"
            )
        if self.status_label == STATUS_LABEL_GPFPD_VARIANT:
            return GPFPD_VARIANT_STATUS_TEXT.get(
                self.solution_status, f"未知状态({self.solution_status})"
            )
        if self.status_label in (STATUS_LABEL_GGA, STATUS_LABEL_CSV):
            known = GGA_QUALITY_TEXT.get(self.solution_status)
            if known:
                return known
            if self.status_label == STATUS_LABEL_GGA:
                return f"未知定位质量({self.solution_status})"
        return SOLUTION_STATUS_TEXT.get(
            self.solution_status, f"未知状态({self.solution_status})"
        )

    @property
    def status_codes(self) -> set:
        """该点状态码的“稳定集合”。

        GGA 定位质量与 GPFPD 解算状态码是两套编码（GGA 没有 11/12，
        GPFPD 没有 4/5），AP18 GSOF49 的 GNSS 状态又是第三套（1-6，其中
        0 无定位、6 为 DR 推算），所以按来源区分，避免把不同编码空间的
        同一个数字误当成稳定。CSV 状态列来源未知，两种编码的稳定码合并，
        任一命中即稳定。
        """
        if self.status_label == STATUS_LABEL_GSOF:
            return GSOF_STABLE_GNSS_CODES
        if self.status_label == STATUS_LABEL_GPFPD_VARIANT:
            return GPFPD_VARIANT_STABLE_STATUS_CODES
        if self.status_label == STATUS_LABEL_GGA:
            return GGA_STABLE_QUALITY_CODES
        if self.status_label == STATUS_LABEL_CSV:
            return GPS_STABLE_STATUS_CODES | GGA_STABLE_QUALITY_CODES
        return GPS_STABLE_STATUS_CODES

    @property
    def gps_stable(self) -> bool:
        return (
            self.has_status_field
            and self.solution_status is not None
            and self.solution_status in self.status_codes
            and self.checksum_valid is not False
        )

    @property
    def checksum_ok(self) -> bool:
        """NMEA校验和没问题（没有校验和字段时视为可用）。"""
        return self.checksum_valid is not False

    def baseline_class(self, tolerance_m: float = BASELINE_TOLERANCE_M) -> Optional[str]:
        if self.baseline_m is None:
            return None
        matches = [
            (abs(self.baseline_m - target), label)
            for label, target in BASELINE_TARGETS_M.items()
            if abs(self.baseline_m - target) <= tolerance_m
        ]
        return min(matches)[1] if matches else None

    def __repr__(self):
        return (
            f"Point(t={self.timestamp}, lon={self.lon:.8f}, lat={self.lat:.8f}, "
            f"alt={self.alt:.2f}, status={self.solution_status}, baseline={self.baseline_m})"
        )


def auto_split_line(line: str) -> list:
    """自动识别分隔符切割一行数据，优先逗号"""
    line = line.strip()
    for sep in SEPARATORS:
        if sep in line:
            if sep == ",":
                # NMEA 是定长字段协议，空字段也必须保留，否则后续列会错位。
                return [x.strip() for x in line.split(sep)]
            return line.split()
    return line.split()


def _checksum_values(raw_line: str):
    """返回 (给定校验和, 标准异或值, 累加和值)，无法解析时返回 None。

    标准 NMEA 用「$ 与 * 之间字符的异或」；部分设备（俄罗斯 E5 的 rtkimu 等）
    用「从 $ 开始（含 $）到 * 之前所有字符的累加和 & 0xFF」。两种都算出来，
    由调用方决定接受哪一种。
    """
    if not raw_line.startswith("$") or "*" not in raw_line:
        return None
    star = raw_line.index("*")
    payload = raw_line[1:star]
    expected_text = raw_line[star + 1:].strip()[:2]
    if len(expected_text) != 2:
        return None
    try:
        given = int(expected_text, 16)
    except ValueError:
        return None
    xor_value = 0
    for char in payload:
        xor_value ^= ord(char)
    sum_value = 0
    for char in raw_line[:star]:  # 含开头的 '$'
        sum_value += ord(char)
    return given, xor_value, sum_value & 0xFF


def _nmea_checksum_valid(raw_line: str) -> Optional[bool]:
    """校验 NMEA 校验和；没有校验和时返回 None。

    同时接受标准异或与「累加和（含 $）」两种约定——后者是 rtkimu 这类
    GPFPD 变种设备的格式，标准 NMEA 数据不会因这条放宽而误判
    （累加和命中异或的概率约 1/256，且随机字节不会被当成 GPFPD 行）。
    """
    values = _checksum_values(raw_line)
    if values is None:
        return None if ("*" not in raw_line or not raw_line.startswith("$")) else False
    given, xor_value, sum_value = values
    return given == xor_value or given == sum_value


def _is_sum_checksum_variant(raw_line: str) -> bool:
    """该行是不是「累加和校验」的变种设备（而不是标准 NMEA 异或）。"""
    values = _checksum_values(raw_line)
    if values is None:
        return False
    given, xor_value, sum_value = values
    return given != xor_value and given == sum_value


def _clean_status_token(token: str) -> str:
    return token.split("*", 1)[0].strip()


def _nmea_seconds_of_day(data: list) -> Optional[float]:
    """从 GPGGA 的 HHMMSS.ss 字段解析当天秒数。"""
    token = data[1] if len(data) > 1 else ""
    token = _clean_status_token(token)
    if not token:
        return None
    raw = float(token)
    hours = int(raw // 10000)
    minutes = int((raw % 10000) // 100)
    seconds = raw % 100
    return hours * 3600 + minutes * 60 + seconds


def _parse_gpgga(raw_line: str, data: list, line_no: int) -> Optional[TrajectoryPoint]:
    """解析 $GPGGA/$GNGGA：时间、经纬度、定位质量。"""
    if len(data) < 10:
        return None
    seconds = _nmea_seconds_of_day(data)
    lat_raw = _clean_status_token(data[2])
    lon_raw = _clean_status_token(data[4])
    if seconds is None or not lat_raw or not lon_raw:
        return None

    def convert(value: str, hemisphere: str) -> float:
        numeric = float(value)
        degrees = int(numeric // 100)
        minutes = numeric - degrees * 100
        result = degrees + minutes / 60.0
        if hemisphere.upper() in ("S", "W"):
            result = -result
        return result

    lat = convert(lat_raw, _clean_status_token(data[3]))
    lon = convert(lon_raw, _clean_status_token(data[5]))
    try:
        quality = int(_clean_status_token(data[6]))
    except (ValueError, IndexError):
        quality = None
    try:
        alt = float(_clean_status_token(data[9]))
    except (ValueError, IndexError):
        alt = 0.0

    warnings = []
    if quality is None:
        warnings.append("GGA定位质量无法解析")
    return TrajectoryPoint(
        seconds,
        lon,
        lat,
        alt,
        line_no=line_no,
        solution_status=quality,
        status_label=STATUS_LABEL_GGA,
        has_status_field=True,
        checksum_valid=_nmea_checksum_valid(raw_line),
        parse_warnings=warnings,
    )


def _parse_gpfpd(raw_line: str, data: list, line_no: int) -> Optional[TrajectoryPoint]:
    """解析 $GPFPD：GPS周+周秒、经纬度、基线长度、解算状态。

    时间换算：GPS 周/周内秒是 GPS 时，必须加上 GPS 纪元（1980-01-06）并扣除
    GPS-UTC 闰秒才是 Unix 秒；只按 ``周*604800+周内秒`` 算会差约 10.7 年，
    和设备文件名时间求交集时永远对不上（俄罗斯 E5 的 rtkimu 数据就是这种情况）。

    状态口径：标准设备用 1/2/11/12；rtkimu 这类变种用 08/09，其中 08 表示
    组合导航正常、09 表示静止/零速修正。变种由「校验和是累加和」自动识别，
    两套状态码互不污染。
    """
    if len(data) < 16:
        return None
    week = float(data[1])
    sec_of_week = float(data[2])
    WEEK_SEC = 7 * 86400
    ts = (
        GPS_EPOCH_UNIX_SECONDS
        + week * WEEK_SEC
        + sec_of_week
        - GPS_UTC_LEAP_SECONDS
    )

    lat = float(data[6])
    lon = float(data[7])
    alt = float(data[8])
    warnings = []
    try:
        baseline_m = float(data[12])
    except ValueError:
        baseline_m = None
        warnings.append("基线长度无法解析")
    try:
        status = int(_clean_status_token(data[15]), 10)
    except ValueError:
        status = None
        warnings.append("GPS状态无法解析")

    variant = _is_sum_checksum_variant(raw_line)
    return TrajectoryPoint(
        ts,
        lon,
        lat,
        alt,
        line_no=line_no,
        baseline_m=baseline_m,
        solution_status=status,
        status_label=STATUS_LABEL_GPFPD_VARIANT if variant else STATUS_LABEL_GPFPD,
        has_status_field=True,
        checksum_valid=_nmea_checksum_valid(raw_line),
        parse_warnings=warnings,
    )


def _parse_csv(data: list, line_no: int) -> Optional[TrajectoryPoint]:
    """解析纯CSV：时间戳,纬度,经度,高度(,可选状态/基线)。

    状态列既可能是 GPFPD 解算状态码（1/2/11/12），也可能是 GGA 定位质量
    （1/2/4/5），这里按数值本身判定，使两种来源都能被识别为稳定。
    空的基线/状态列视为“未提供”，不产生解析异常。
    """
    if len(data) < 4:
        return None
    ts = float(data[0])
    lat = float(data[1])
    lon = float(data[2])
    alt = float(data[3])
    status = None
    baseline_m = None
    warnings = []
    has_status = False
    if len(data) >= 5:
        token = _clean_status_token(data[4])
        if token:
            try:
                status = int(float(token))
                has_status = True
            except ValueError:
                warnings.append("GPS状态无法解析")
    if len(data) >= 6:
        token = _clean_status_token(data[5])
        if token:
            try:
                baseline_m = float(token)
            except ValueError:
                warnings.append("基线长度无法解析")
    return TrajectoryPoint(
        ts,
        lon,
        lat,
        alt,
        line_no=line_no,
        baseline_m=baseline_m,
        solution_status=status,
        status_label=STATUS_LABEL_CSV,
        has_status_field=has_status,
        parse_warnings=warnings,
    )


def parse_line(line: str, line_no: int = 0):
    """
    自动识别单行格式：
    1. $GPFPD 开头 → 欧洲NMEA GPFPD格式
       data[1]=GPS周, data[2]=周内秒, data[6]=纬度, data[7]=经度, data[8]=高度,
       data[12]=基线长度, data[15]=解算状态
    2. $GPGGA/$GNGGA → 标准 NMEA 定位语句，带定位质量
    3. 纯数字逗号分割 → CSV，data[0]=时间戳, [1]=纬度, [2]=经度, [3]=高度，
       可选的 [4]=状态, [5]=基线
    return: TrajectoryPoint / None（脏数据）
    """
    raw_line = line.strip()
    if not raw_line:
        return None

    try:
        if raw_line.startswith("$GPFPD"):
            return _parse_gpfpd(raw_line, auto_split_line(raw_line), line_no)
        if raw_line.startswith(("$GPGGA", "$GNGGA")):
            return _parse_gpgga(raw_line, auto_split_line(raw_line), line_no)
        return _parse_csv(auto_split_line(raw_line), line_no)
    except (ValueError, IndexError):
        return None


def _point_validation_error(point: TrajectoryPoint) -> Optional[str]:
    if not all(
        math.isfinite(value)
        for value in (point.timestamp, point.lon, point.lat, point.alt)
    ):
        return "时间或坐标包含NaN/Infinity"
    if not -180 <= point.lon <= 180:
        return f"经度越界({point.lon})"
    if not -90 <= point.lat <= 90:
        return f"纬度越界({point.lat})"
    return None


def _detect_format(first_line: str) -> tuple[str, str]:
    stripped = first_line.strip()
    if stripped.startswith("$GPFPD"):
        return FORMAT_GPFPD, "NMEA $GPFPD（GPS周+周秒时间戳，含基线长度与解算状态）"
    if stripped.startswith(("$GPGGA", "$GNGGA")):
        return FORMAT_NMEA, "NMEA $GPGGA（含定位质量，无基线长度）"
    return FORMAT_CSV, "逗号CSV（绝对时间戳，状态列可选）"


def load_trajectory(
    file_name: str,
    include_diagnostics: bool = False,
    *,
    verbose: bool = True,
):
    """
    通用轨迹加载函数，自动识别三种格式：
    - $GPFPD NMEA（GPS周+周秒合成总时间戳）
    - $GPGGA NMEA（当天秒数时间戳）
    - 逗号CSV（绝对时间戳）
    """
    file_path = file_name if os.path.isabs(file_name) else os.path.join(INPUT_DIR, file_name)
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"轨迹文件不存在：{file_path}")

    result = TrajectoryLoadResult()
    result.path = os.path.abspath(file_path)
    file_format = None
    file_type = None
    with open(file_path, "r", encoding="utf-8-sig", errors="replace") as f:
        for line_idx, line in enumerate(f, start=1):
            if not line.strip():
                continue
            point = parse_line(line, line_no=line_idx)
            if point is None:
                result.issues.append(
                    ParseIssue(line_idx, "格式或数值解析失败", line.strip()[:200])
                )
                continue
            validation_error = _point_validation_error(point)
            if validation_error:
                result.issues.append(
                    ParseIssue(line_idx, validation_error, line.strip()[:200])
                )
                continue
            if point.checksum_valid is False:
                result.issues.append(ParseIssue(line_idx, "NMEA校验和错误", line.strip()[:200]))
            for warning in point.parse_warnings:
                result.issues.append(ParseIssue(line_idx, warning, line.strip()[:200]))
            # 识别文件类型（仅第一行判断）
            if file_format is None:
                file_format, file_type = _detect_format(line)
            if point.has_status_field:
                key = (
                    f"{point.status_label}:{point.solution_status}"
                    if point.solution_status is not None
                    else f"{point.status_label}:无法解析"
                )
                result.status_counts[key] = result.status_counts.get(key, 0) + 1
            result.points.append(point)
    if not result.points:
        raise RuntimeError("文件内未解析到有效轨迹点，请检查文件格式")
    result.format = file_format or "未知"
    result.file_type = file_type or "未知"
    if verbose:
        print("==== 文件识别完成 ====")
        print(f"文件类型：{result.file_type}")
        print(f"成功读取WGS84轨迹点总数：{len(result.points)}")
        print(f"解析/校验异常行数：{len(result.issues)}")
        print("========================\n")
    if include_diagnostics:
        return result
    return result.points
