#!/usr/bin/env python3
"""Decode AP18 LVX GSOF records and NMEA GGA sentences from classic libpcap files.

The AP18 captures contain Ethernet/IPv4/UDP packets carrying Trimble GSOF
Report Package 0x40 and, in newer device configurations, NMEA 0183 GGA
sentences.  This decoder writes one UTF-8 TSV text file per pcap.

搜星数优先取 GSOF Type 1（Position Time）；设备不再输出 GSOF1 时回退到 GGA 第 7 字段。
差分龄期（age of differential data）与差分参考站 ID 只能来自 GGA 第 13 / 14 字段。
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import math
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


GPS_EPOCH = dt.datetime(1980, 1, 6, tzinfo=dt.timezone.utc)
GPS_UTC_LEAP_SECONDS = 18

IMU_STATUS = {
    0: "GPS Only",
    1: "Coarse leveling",
    2: "Degraded",
    3: "Aligned",
    4: "Full Nav",
}

GNSS_STATUS = {
    0: "Fix not available",
    1: "GNSS SPS Mode",
    2: "Differential GPS, SPS",
    3: "GNSS PPS Mode",
    4: "Fixed RTK Mode",
    5: "Float RTK",
    6: "DR Mode",
}

# NMEA GGA 第 6 字段：定位质量。只有 2/4/5 表示在使用差分/RTK 改正量。
GGA_QUALITY = {
    0: "定位无效",
    1: "单点 SPS",
    2: "差分 DGPS",
    3: "PPS",
    4: "RTK 固定",
    5: "RTK 浮动",
    6: "航位推算",
    7: "手动输入",
    8: "模拟定位",
}

# GGA 只有 UTC 日内时刻、没有日期，因此用"日内秒"就近匹配 GSOF49 的行时间。
# GGA 为 1 Hz 时该容差刚好把每条 GGA 覆盖到它前后约半秒内的所有 GSOF49 行；
# 超过容差说明 GGA 中断，对应行留空（而不是沿用陈旧值）。
GGA_MATCH_TOLERANCE_SECONDS = 1.1
SECONDS_PER_DAY = 86400.0
# NMEA 文本按流处理：一个 UDP 包可能含多句，一句也可能跨包。
NMEA_FLOW_BUFFER_LIMIT = 4096
# talker（GP/GN/GL/IN/…）不固定，按第 3–5 字符 GGA 识别，不锁死具体前缀。
# 两个版本：bytes 版用于载荷预筛，文本版用于在解码后的字符串里定位句子起点
# （解码会丢弃非 ASCII 字节、改变偏移，所以必须在解码后的文本上重新匹配）。
GGA_MARKER = re.compile(rb"\$[A-Z]{1,2}GGA,")
GGA_MARKER_TEXT = re.compile(r"\$[A-Z]{1,2}GGA,")


@dataclass
class CapturePacket:
    timestamp: dt.datetime
    raw_timestamp: str
    packet_index: int
    frame: bytes


@dataclass
class GsofRecord:
    record_type: int
    payload: bytes


def _pcap_header(path: Path) -> tuple[str, int, int]:
    with path.open("rb") as stream:
        header = stream.read(24)
    if len(header) != 24:
        raise ValueError(
            f"{path}: pcap 全局头不完整（只读到 {len(header)} 字节，需要 24 字节）"
            "：文件可能仍在录制、被截断，或者根本不是 pcap"
        )

    magic = header[:4]
    if magic == b"\xd4\xc3\xb2\xa1":
        return "<", 1_000_000, struct.unpack_from("<I", header, 20)[0]
    if magic == b"\xa1\xb2\xc3\xd4":
        return ">", 1_000_000, struct.unpack_from(">I", header, 20)[0]
    if magic == b"\x4d\x3c\xb2\xa1":
        return "<", 1_000_000_000, struct.unpack_from("<I", header, 20)[0]
    if magic == b"\xa1\xb2\x3c\x4d":
        return ">", 1_000_000_000, struct.unpack_from(">I", header, 20)[0]
    # 全 0 的文件头是最常见的一种：抓包工具刚创建文件还没写入内容（正在录制），
    # 或者文件被清零/只分配了空间。单独点出来，用户才知道该等录制结束再选。
    hint = (
        "：文件头前 4 字节全为 0，文件可能正在录制、被清零或只是一个占位文件"
        if magic == b"\x00\x00\x00\x00"
        else "：不是有效的 pcap 文件（pcapng 请先转成 pcap）"
    )
    raise ValueError(f"{path}: 无法识别的 pcap 文件头 magic={magic.hex()}{hint}")


def iter_pcap_packets(
    path: Path, *, tolerate_truncated: bool = False
) -> Iterator[CapturePacket]:
    endian, tick_rate, _snaplen = _pcap_header(path)
    with path.open("rb") as stream:
        stream.seek(24)
        packet_index = 0
        while True:
            packet_header = stream.read(16)
            if not packet_header:
                return
            if len(packet_header) != 16:
                if tolerate_truncated:
                    return
                raise ValueError(
                    f"{path}: 数据包头不完整（文件可能还在录制或未拷贝完整）"
                )
            seconds, ticks, captured_length, _original_length = struct.unpack(
                endian + "IIII", packet_header
            )
            frame = stream.read(captured_length)
            if len(frame) != captured_length:
                if tolerate_truncated:
                    return
                raise ValueError(
                    f"{path}: 数据包内容不完整（文件可能还在录制或未拷贝完整）"
                )
            timestamp = dt.datetime.fromtimestamp(seconds + ticks / tick_rate, tz=dt.timezone.utc)
            tick_digits = 6 if tick_rate == 1_000_000 else 9
            raw_timestamp = f"{seconds}.{ticks:0{tick_digits}d}"
            packet_index += 1
            yield CapturePacket(timestamp, raw_timestamp, packet_index, frame)


def udp_datagram(frame: bytes) -> tuple[int, int, bytes] | None:
    """Return (source_port, destination_port, payload) for IPv4/UDP frames."""
    if len(frame) < 14:
        return None
    ether_type = struct.unpack_from("!H", frame, 12)[0]
    offset = 14
    while ether_type in (0x8100, 0x88A8, 0x9100):
        if len(frame) < offset + 4:
            return None
        ether_type = struct.unpack_from("!H", frame, offset + 2)[0]
        offset += 4
    if ether_type != 0x0800 or len(frame) < offset + 20:
        return None

    ip_header_length = (frame[offset] & 0x0F) * 4
    if (frame[offset] >> 4) != 4 or ip_header_length < 20:
        return None
    if len(frame) < offset + ip_header_length + 8:
        return None
    if frame[offset + 9] != 17:  # UDP
        return None
    udp_offset = offset + ip_header_length
    source_port, destination_port, udp_length, _checksum = struct.unpack_from(
        "!HHHH", frame, udp_offset
    )
    if udp_length < 8 or len(frame) < udp_offset + udp_length:
        return None
    return source_port, destination_port, frame[udp_offset + 8 : udp_offset + udp_length]


def looks_like_gsof(payload: bytes) -> bool:
    """Identify AP18 GSOF by payload structure instead of UDP ports.

    Both the device's source port (observed 1025 and 1026) and the receiving
    socket's destination port (28001, 5017, ...) vary between setups, so ports
    are not a reliable discriminator.  A Report Package 0x40 starts with STX
    0x02 and carries 0x40 at index 2; report_records() validates the whole
    report (bounds, ETX, checksum) afterwards.
    """
    return len(payload) >= 9 and payload[0] == 0x02 and payload[2] == 0x40


def udp_payload(frame: bytes) -> bytes | None:
    """Return the payload of an IPv4/UDP frame when it carries a GSOF report."""
    datagram = udp_datagram(frame)
    if datagram is None:
        return None
    payload = datagram[2]
    return payload if looks_like_gsof(payload) else None


def nmea_stream(payload: bytes) -> str | None:
    """Return the payload as an NMEA text stream, or None when it is binary data."""
    if b"$" not in payload:
        return None
    printable = sum(1 for byte in payload if 32 <= byte <= 126 or byte in (10, 13))
    if printable < len(payload) * 0.95:
        return None
    return payload.decode("ascii", "ignore")


def embedded_nmea_sentences(payload: bytes) -> list[str]:
    """少数固件把 GGA 追加在二进制 GSOF 报文之后，这里单独把句子切出来。

    这类载荷不能进文本流缓冲（二进制里的 0x0A 会打乱句子边界），因此按
    ``$??GGA,`` 标记定位真正的句子开头。**不能用第一个 ``$``**：二进制数据里
    经常先出现一个孤立的 0x24，从那里切会让整句校验失败而被整条丢掉。
    """
    if GGA_MARKER.search(payload) is None:
        return []
    text = payload.decode("ascii", "ignore")
    sentences: list[str] = []
    for match in GGA_MARKER_TEXT.finditer(text):
        end = text.find("\n", match.start())
        sentences.append(text[match.start():] if end < 0 else text[match.start():end])
    return sentences


def report_records(payload: bytes) -> tuple[int, int, list[GsofRecord]] | None:
    """Decode one GSOF Report Package 0x40 and validate its checksum."""
    if len(payload) < 9 or payload[0] != 0x02 or payload[2] != 0x40:
        return None
    data_length = payload[3]
    end = 4 + data_length
    if end + 2 > len(payload) or payload[end + 1] != 0x03:
        raise ValueError("invalid GSOF report bounds or ETX")
    checksum = sum(payload[1:end]) & 0xFF
    if checksum != payload[end]:
        raise ValueError(
            f"invalid GSOF report checksum: calculated 0x{checksum:02x}, "
            f"received 0x{payload[end]:02x}"
        )

    transmission_number = payload[4]
    page_index = payload[5]
    max_page_index = payload[6]
    records: list[GsofRecord] = []
    offset = 7
    while offset < end:
        if offset + 2 > end:
            raise ValueError("truncated GSOF record header")
        record_type = payload[offset]
        record_length = payload[offset + 1]
        record_end = offset + 2 + record_length
        if record_end > end:
            raise ValueError("truncated GSOF record payload")
        records.append(GsofRecord(record_type, payload[offset:record_end]))
        offset = record_end
    return transmission_number, page_index, records


def unpack_gsof49(record: GsofRecord) -> dict[str, object]:
    if record.record_type != 0x31 or len(record.payload) < 106:
        raise ValueError("GSOF49 record has an unexpected length")
    b = record.payload
    return {
        "gps_week": struct.unpack_from(">H", b, 2)[0],
        "gps_ms": struct.unpack_from(">I", b, 4)[0],
        "imu_status": b[8],
        "gnss_status": b[9],
        "latitude": struct.unpack_from(">d", b, 10)[0],
        "longitude": struct.unpack_from(">d", b, 18)[0],
        "altitude": struct.unpack_from(">d", b, 26)[0],
        "north_velocity": struct.unpack_from(">f", b, 34)[0],
        "east_velocity": struct.unpack_from(">f", b, 38)[0],
        "down_velocity": struct.unpack_from(">f", b, 42)[0],
        "total_speed": struct.unpack_from(">f", b, 46)[0],
        "roll": struct.unpack_from(">d", b, 50)[0],
        "pitch": struct.unpack_from(">d", b, 58)[0],
        "heading": struct.unpack_from(">d", b, 66)[0],
        "track_angle": struct.unpack_from(">d", b, 74)[0],
        "angular_rate_x": struct.unpack_from(">f", b, 82)[0],
        "angular_rate_y": struct.unpack_from(">f", b, 86)[0],
        "angular_rate_z": struct.unpack_from(">f", b, 90)[0],
        "acceleration_x": struct.unpack_from(">f", b, 94)[0],
        "acceleration_y": struct.unpack_from(">f", b, 98)[0],
        "acceleration_z": struct.unpack_from(">f", b, 102)[0],
    }


def unpack_gsof1(record: GsofRecord) -> dict[str, object]:
    """Decode GSOF Type 1 Position Time satellite-count information."""
    if record.record_type != 0x01 or len(record.payload) < 12:
        raise ValueError("GSOF1 record has an unexpected length")
    b = record.payload
    return {
        "gps_ms": struct.unpack_from(">I", b, 2)[0],
        "gps_week": struct.unpack_from(">H", b, 6)[0],
        "satellites_used": b[8],
        "position_flags_1": b[9],
        "position_flags_2": b[10],
        "initialized_number": b[11],
    }


def unpack_gsof50(record: GsofRecord) -> dict[str, object]:
    # 最后一个字段在偏移 42 处读 4 字节 float，因此至少需要 46 字节。
    # 写成 45 会让 45 字节的载荷落到 struct.error（不是 ValueError），
    # 既拿不到预期的"长度异常"语义，也会让整份文件解析失败。
    if record.record_type != 0x32 or len(record.payload) < 46:
        raise ValueError("GSOF50 record has an unexpected length")
    b = record.payload
    return {
        "gps_week": struct.unpack_from(">H", b, 2)[0],
        "gps_ms": struct.unpack_from(">I", b, 4)[0],
        "imu_status": b[8],
        "gnss_status": b[9],
        "north_position_rms": struct.unpack_from(">f", b, 10)[0],
        "east_position_rms": struct.unpack_from(">f", b, 14)[0],
        "down_position_rms": struct.unpack_from(">f", b, 18)[0],
        "north_velocity_rms": struct.unpack_from(">f", b, 22)[0],
        "east_velocity_rms": struct.unpack_from(">f", b, 26)[0],
        "down_velocity_rms": struct.unpack_from(">f", b, 30)[0],
        "roll_rms": struct.unpack_from(">f", b, 34)[0],
        "pitch_rms": struct.unpack_from(">f", b, 38)[0],
        "heading_rms": struct.unpack_from(">f", b, 42)[0],
    }


def gps_datetime_utc(gps_week: int, gps_ms: int) -> dt.datetime:
    gps_time = GPS_EPOCH + dt.timedelta(weeks=gps_week, milliseconds=gps_ms)
    return gps_time - dt.timedelta(seconds=GPS_UTC_LEAP_SECONDS)


def satellite_quality_text(satellites_used: object) -> str:
    if not isinstance(satellites_used, int):
        return "未输出搜星数（GSOF1/GGA 均无）"
    if satellites_used < 5:
        return "卫星不足（<5颗）"
    return "搜星正常（≥5颗）"


def _nmea_number(text: str) -> float | None:
    """GGA 的数值字段：空字段与占位符都返回 None，而不是 0。"""
    text = text.strip()
    if not text or text in {"-", "--"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def hhmmss_to_sod(text: str) -> float | None:
    """把 GGA 的 UTC 时刻 hhmmss.sss 换算成日内秒。"""
    text = text.strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    whole = int(value)
    hour, minute = whole // 10000, (whole // 100) % 100
    second = whole % 100 + (value - whole)
    if hour > 23 or minute > 59 or second >= 61:
        return None
    return hour * 3600 + minute * 60 + second


def unpack_nmea_gga(sentence: str) -> dict[str, object] | None:
    """Decode one NMEA GGA sentence, or return None when it is not a valid GGA.

    GGA 字段（按逗号切分后的下标）：
      1 UTC 时刻 / 6 定位质量 / 7 用于定位的卫星数 / 8 HDOP
      9 海拔 / 11 大地水准面差距 / 13 差分龄期(s) / 14 差分参考站 ID
    """
    start = sentence.find("$")
    if start < 0:
        return None
    sentence = sentence[start:].strip()
    # 先切掉 *hh 再按逗号切分：否则末尾的空字段会把校验和并进参考站 ID，
    # 整个字段序列会因此错位一位。
    body, star, checksum = sentence.partition("*")
    if not star:
        return None  # 没有校验和的句子按残缺处理，避免把二进制里的巧合字节当真句
    calculated = 0
    for char in body[1:]:
        calculated ^= ord(char)
    try:
        if calculated != int(checksum[:2], 16):
            return None
    except ValueError:
        return None

    fields = body.split(",")
    # 兼容老版本 GGA：v2.0/2.1 没有 13/14 字段，最短只到 index 12。
    if len(fields) < 13 or not fields[0].endswith("GGA"):
        return None
    sod = hhmmss_to_sod(fields[1])
    if sod is None:
        return None
    quality = fields[6].strip()
    satellites = fields[7].strip()
    return {
        "sod": sod,
        "quality": int(quality) if quality.isdigit() else None,
        "satellites_used": int(satellites) if satellites.isdigit() else None,
        "hdop": _nmea_number(fields[8]),
        "altitude": _nmea_number(fields[9]),
        "geoid_separation": _nmea_number(fields[11]),
        "differential_age": _nmea_number(fields[13]) if len(fields) > 13 else None,
        "differential_station_id": (fields[14].strip() or None) if len(fields) > 14 else None,
    }


def differential_age_text(record: dict[str, object] | None) -> str | None:
    """把 GGA 的差分字段归纳成离散评估文本（表格列与统计 chips 都按它聚合）。"""
    if record is None:
        return None
    quality = record.get("quality")
    if quality is None:
        return "GGA 未输出定位质量"
    label = GGA_QUALITY.get(quality, f"quality={quality}")
    if quality not in (2, 4, 5):
        return f"未使用差分改正（{label}）"
    age = record.get("differential_age")
    if age is None:
        return f"{label} · 未输出差分龄期"
    if age < 0:
        return f"{label} · 差分龄期无效（负值）"
    if age <= 1:
        return "差分龄期 ≤1 s（正常）"
    if age <= 3:
        return "差分龄期 1~3 s"
    if age <= 10:
        return "差分龄期 3~10 s（偏大）"
    return "差分龄期 >10 s（改正量已过期）"


def nearest_gga(
    records: list[dict[str, object]], sods: list[float], row_sod: float
) -> dict[str, object] | None:
    """按日内秒就近匹配 GGA；跨零点时按 24 h 环绕取最短距离。"""
    index = bisect.bisect_left(sods, row_sod)
    candidates = {0, len(sods) - 1}
    for offset in (-1, 0, 1):
        position = index + offset
        if 0 <= position < len(sods):
            candidates.add(position)
    best_position: int | None = None
    best_distance: float | None = None
    for position in candidates:
        distance = abs(sods[position] - row_sod)
        distance = min(distance, SECONDS_PER_DAY - distance)
        if distance > GGA_MATCH_TOLERANCE_SECONDS:
            continue
        if best_distance is None or distance < best_distance:
            best_position, best_distance = position, distance
    return None if best_position is None else records[best_position]


def hdop_text(hdop: object) -> str | None:
    """把 HDOP 归纳成离散的几何强度评估（表格列与统计 chips 都按它聚合）。

    HDOP 是纯几何量：只反映卫星在天空中的分布好坏，与实际定位误差无关。
    """
    if not isinstance(hdop, float) or not math.isfinite(hdop):
        return None
    if hdop <= 0:
        return "HDOP 无效（≤0）"
    if hdop <= 1:
        return "HDOP ≤1（几何优）"
    if hdop <= 2:
        return "HDOP 1~2（良）"
    if hdop <= 5:
        return "HDOP 2~5（中）"
    if hdop <= 10:
        return "HDOP 5~10（差）"
    return "HDOP >10（很差）"


def attach_gga_fields(
    rows: list[dict[str, object]], gga_records: list[dict[str, object]]
) -> int:
    """把就近的 GGA 差分/几何/高程信息挂到各行 GSOF49 上，返回匹配成功的行数。"""
    usable = sorted(gga_records, key=lambda record: float(record["sod"]))
    sods = [float(record["sod"]) for record in usable]
    matched = 0
    for row in rows:
        # _gps_sod 是解码过程中的临时字段，无论是否匹配成功都要摘掉。
        row_sod = row.pop("_gps_sod", None)
        if not sods or row_sod is None:
            continue
        record = nearest_gga(usable, sods, float(row_sod))
        if record is None:
            continue
        matched += 1
        row["differential_age"] = record.get("differential_age")
        row["differential_station_id"] = record.get("differential_station_id")
        row["differential_age_text"] = differential_age_text(record)
        row["hdop"] = record.get("hdop")
        row["hdop_text"] = hdop_text(record.get("hdop"))
        # GSOF49 的 altitude 是 WGS84 椭球高，GGA 的 geoid_separation 是椭球面与
        # 大地水准面的高差，两者相减才是地图/水准意义上的海拔（正高）。
        geoid = record.get("geoid_separation")
        row["geoid_separation"] = geoid
        row["gga_altitude"] = record.get("altitude")
        ellipsoid = row.get("altitude")
        row["orthometric_altitude"] = (
            ellipsoid - geoid
            if isinstance(ellipsoid, float)
            and isinstance(geoid, float)
            and math.isfinite(ellipsoid)
            and math.isfinite(geoid)
            else None
        )
        if row.get("satellites_used") is None and record.get("satellites_used") is not None:
            row["satellites_used"] = record["satellites_used"]
            row["satellites_source"] = "GGA"
        row["satellite_quality_text"] = satellite_quality_text(row.get("satellites_used"))
    return matched


def fmt(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.9f}"
    return str(value)


COLUMNS = [
    ("序号", "index"),
    ("抓包时间(UTC)", "capture_time"),
    ("原始时间戳", "raw_timestamp"),
    ("PCAP包序号", "pcap_packet_index"),
    ("GPS时间(UTC)", "gps_time"),
    ("GPS周", "gps_week"),
    ("GPS周内毫秒", "gps_ms"),
    ("IMU对准状态", "imu_status_text"),
    ("GNSS状态", "gnss_status_text"),
    ("搜星数(用于定位)", "satellites_used"),
    ("搜星数来源", "satellites_source"),
    ("搜星评估", "satellite_quality_text"),
    ("HDOP", "hdop"),
    ("HDOP评估", "hdop_text"),
    ("差分龄期(s)", "differential_age"),
    ("差分参考站ID", "differential_station_id"),
    ("差分龄期评估", "differential_age_text"),
    ("纬度(deg)", "latitude"),
    ("经度(deg)", "longitude"),
    ("高程(m)", "altitude"),
    ("大地水准面差距(m)", "geoid_separation"),
    ("海拔(m)", "orthometric_altitude"),
    ("GGA海拔(m)", "gga_altitude"),
    ("北速度(m/s)", "north_velocity"),
    ("东速度(m/s)", "east_velocity"),
    ("下速度(m/s)", "down_velocity"),
    ("总速度(m/s)", "total_speed"),
    ("横滚(deg)", "roll"),
    ("俯仰(deg)", "pitch"),
    ("航向(deg)", "heading"),
    ("航迹角(deg)", "track_angle"),
    ("角速度X(deg/s)", "angular_rate_x"),
    ("角速度Y(deg/s)", "angular_rate_y"),
    ("角速度Z(deg/s)", "angular_rate_z"),
    ("加速度X(m/s2)", "acceleration_x"),
    ("加速度Y(m/s2)", "acceleration_y"),
    ("加速度Z(m/s2)", "acceleration_z"),
    ("北位置RMS(m)", "north_position_rms"),
    ("东位置RMS(m)", "east_position_rms"),
    ("下位置RMS(m)", "down_position_rms"),
    ("北速度RMS(m/s)", "north_velocity_rms"),
    ("东速度RMS(m/s)", "east_velocity_rms"),
    ("下速度RMS(m/s)", "down_velocity_rms"),
    ("横滚RMS(deg)", "roll_rms"),
    ("俯仰RMS(deg)", "pitch_rms"),
    ("航向RMS(deg)", "heading_rms"),
]


def collect_nmea_sentence(
    raw: str, counts: dict[str, int], sink: list[dict[str, object]]
) -> None:
    """从一行里找出所有 `$` 开头的候选句子，逐个尝试解析并计入统计。

    只试第一个 `$` 是不够的：文本流里也可能在真句子前混入孤立的 `$` 字节，
    那样整句会被当成坏句丢掉。
    """
    position = raw.find("$")
    while position >= 0:
        counts["nmea_sentences"] += 1
        record = unpack_nmea_gga(raw[position:])
        if record is not None:
            sink.append(record)
            counts["gga_sentences"] += 1
        position = raw.find("$", position + 1)


def decode_file(
    path: Path, *, tolerate_truncated: bool = False
) -> tuple[list[dict[str, object]], dict[str, int]]:
    rows: list[dict[str, object]] = []
    satellite_counts: dict[tuple[int, int], int] = {}
    gga_records: list[dict[str, object]] = []
    nmea_buffers: dict[tuple[int, int], str] = {}
    counts = {
        "pcap_packets": 0,
        "udp_gsof_packets": 0,
        "gsof49_records": 0,
        "gsof1_records": 0,
        "gsof50_records": 0,
        "nmea_packets": 0,
        "nmea_sentences": 0,
        "gga_sentences": 0,
        "gga_matched_rows": 0,
        "invalid_packets": 0,
        "invalid_records": 0,
    }
    for packet in iter_pcap_packets(path, tolerate_truncated=tolerate_truncated):
        counts["pcap_packets"] += 1
        datagram = udp_datagram(packet.frame)
        if datagram is None:
            continue
        source_port, destination_port, payload = datagram

        if looks_like_gsof(payload):
            counts["udp_gsof_packets"] += 1
            try:
                report = report_records(payload)
            except ValueError:
                counts["invalid_packets"] += 1
                report = None
            if report is not None:
                _transmission_number, _page_index, records = report
                rec1 = next((r for r in records if r.record_type == 0x01), None)
                rec49 = next((r for r in records if r.record_type == 0x31), None)
                rec50 = next((r for r in records if r.record_type == 0x32), None)
                if rec1 is not None:
                    try:
                        time_record = unpack_gsof1(rec1)
                    except ValueError:
                        time_record = None
                    if time_record is not None:
                        key = (int(time_record["gps_week"]), int(time_record["gps_ms"]))
                        satellite_counts[key] = int(time_record["satellites_used"])
                        counts["gsof1_records"] += 1
                if rec49 is not None:
                    try:
                        nav = unpack_gsof49(rec49)
                    except (ValueError, struct.error):
                        # 单条记录畸形不应让整份文件失败
                        counts["invalid_records"] += 1
                        nav = None
                    if nav is not None:
                        if rec50 is not None:
                            try:
                                rms = unpack_gsof50(rec50)
                            except (ValueError, struct.error):
                                counts["invalid_records"] += 1
                                rms = {}
                            else:
                                counts["gsof50_records"] += 1
                        else:
                            rms = {}
                        counts["gsof49_records"] += 1
                        gps_moment = gps_datetime_utc(nav["gps_week"], nav["gps_ms"])
                        matched_satellites = satellite_counts.get(
                            (int(nav["gps_week"]), int(nav["gps_ms"]))
                        )
                        row = {
                            **nav,
                            **rms,
                            "index": len(rows) + 1,
                            "capture_time": packet.timestamp.isoformat(timespec="milliseconds"),
                            "raw_timestamp": packet.raw_timestamp,
                            "pcap_packet_index": packet.packet_index,
                            "gps_time": gps_moment.isoformat(timespec="milliseconds"),
                            "imu_status_text": f"{nav['imu_status']} ({IMU_STATUS.get(nav['imu_status'], 'Unknown')})",
                            "gnss_status_text": f"{nav['gnss_status']} ({GNSS_STATUS.get(nav['gnss_status'], 'Unknown')})",
                            "satellites_used": matched_satellites,
                            # 临时字段：只用于 GGA 就近匹配，匹配完成后统一摘掉。
                            "_gps_sod": (
                                gps_moment.hour * 3600
                                + gps_moment.minute * 60
                                + gps_moment.second
                                + gps_moment.microsecond / 1_000_000
                            ),
                        }
                        if matched_satellites is not None:
                            row["satellites_source"] = "GSOF1"
                        row["satellite_quality_text"] = satellite_quality_text(matched_satellites)
                        rows.append(row)

        # 部分固件会把 NMEA 句子追加在 GSOF 报文之后，因此这里不 continue，
        # 无论上一步是否命中 GSOF 都再尝试提取 NMEA。
        stream = nmea_stream(payload)
        if stream is not None:
            counts["nmea_packets"] += 1
            flow = (source_port, destination_port)
            buffered = nmea_buffers.get(flow, "") + stream
            parts = buffered.split("\n")
            # 最后一段可能是被截断的半句，留到下一个包继续拼接。
            nmea_buffers[flow] = parts.pop()[-NMEA_FLOW_BUFFER_LIMIT:]
            for raw in parts:
                collect_nmea_sentence(raw, counts, gga_records)
            continue
        embedded = embedded_nmea_sentences(payload)
        if embedded:
            counts["nmea_packets"] += 1
            for raw in embedded:
                collect_nmea_sentence(raw, counts, gga_records)

    # 第二遍：先用 GSOF1 的精确 (gps_week, gps_ms) 对齐补齐，再用 GGA 兜底。
    for row in rows:
        if row.get("satellites_used") is None:
            row["satellites_used"] = satellite_counts.get(
                (int(row["gps_week"]), int(row["gps_ms"]))
            )
            if row["satellites_used"] is not None:
                row["satellites_source"] = "GSOF1"
        row["satellite_quality_text"] = satellite_quality_text(row.get("satellites_used"))
    counts["gga_matched_rows"] = attach_gga_fields(rows, gga_records)
    return rows, counts


def write_output(input_path: Path, output_path: Path) -> dict[str, int]:
    rows, counts = decode_file(input_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as stream:
        stream.write("# AP18 LVX GSOF 解析结果\n")
        stream.write(f"# source_pcap: {input_path.name}\n")
        stream.write(f"# pcap_packets: {counts['pcap_packets']}\n")
        stream.write(f"# udp_gsof_packets: {counts['udp_gsof_packets']}\n")
        stream.write(f"# gsof49_navigation_records: {counts['gsof49_records']}\n")
        stream.write(f"# gsof1_satellite_records: {counts['gsof1_records']}\n")
        stream.write(f"# gsof50_rms_records: {counts['gsof50_records']}\n")
        stream.write(f"# nmea_packets: {counts['nmea_packets']}\n")
        stream.write(f"# nmea_sentences: {counts['nmea_sentences']}\n")
        stream.write(f"# gga_sentences: {counts['gga_sentences']}\n")
        stream.write(f"# gga_matched_rows: {counts['gga_matched_rows']}\n")
        stream.write(f"# invalid_gsof_packets: {counts['invalid_packets']}\n")
        stream.write(f"# invalid_gsof_records: {counts['invalid_records']}\n")
        stream.write(
            "# GPS时间(UTC)按 GPS-UTC = 18 秒换算；空白 RMS 表示该条 Report 未附带 GSOF50；"
            "空白搜星数表示 GSOF1 与 GGA 都没有输出；空白差分龄期表示该行未匹配到 GGA 或 GGA 未输出龄期。\n"
        )
        stream.write("\t".join(name for name, _key in COLUMNS) + "\n")
        for row in rows:
            stream.write("\t".join(fmt(row.get(key)) for _name, key in COLUMNS) + "\n")
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="*", type=Path, help="pcap files or directories")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="directory for txt files (default: next to each pcap)",
    )
    args = parser.parse_args()

    inputs: list[Path] = []
    for item in args.inputs or [Path("AP18")]:
        if item.is_dir():
            inputs.extend(sorted(item.glob("*.pcap")))
        else:
            inputs.append(item)
    if not inputs:
        parser.error("no pcap files found")

    for input_path in inputs:
        output_dir = args.output_dir if args.output_dir is not None else input_path.parent
        output_path = output_dir / f"{input_path.stem}.txt"
        counts = write_output(input_path, output_path)
        print(
            f"{input_path} -> {output_path} | "
            f"GSOF49={counts['gsof49_records']} "
            f"GSOF1={counts['gsof1_records']} "
            f"GSOF50={counts['gsof50_records']} "
            f"GGA={counts['gga_sentences']} "
            f"GGA匹配行={counts['gga_matched_rows']} "
            f"NMEA句={counts['nmea_sentences']} "
            f"invalid={counts['invalid_packets']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
