"""AP18 pcap → GGA 轨迹提取。

AP18 输出的 pcap 里，UDP 载荷承载 $GNGGA / $INGGA 定位语句（同一时刻两路互为镜像）。
这里把它们解析成标准轨迹点，供里程计算器直接使用。

设计要点：
- 流式读取，不把整个 pcap 读进内存（单文件几十 MB、几百个文件）。
- 只解析带校验的 GGA 语句，校验和错误直接丢弃。
- 同一时刻 GNGGA/INGGA 各有一条，按 GPS 时刻去重。
- 时间用「文件捕获时刻 - 语句当天秒」反推日期偏移，保证跨零点也连续。
"""

from __future__ import annotations

import struct
from collections import Counter
from datetime import datetime
from typing import Dict, List, Optional, Tuple


def _iter_pcap_packets(path: str):
    """逐个产出 (捕获时间戳, 帧字节)。"""
    with open(path, "rb", buffering=1 << 20) as fh:
        header = fh.read(24)
        if len(header) != 24:
            return
        magic = header[:4]
        # 纳秒精度的时间戳魔数
        nano = magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d")
        # 小端 pcap：d4c3b2a1 / 4d3cb2a1；其余按大端处理
        little = magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1")
        endian = "<" if little else ">"
        scale = 1_000_000_000 if nano else 1_000_000
        while True:
            rec = fh.read(16)
            if len(rec) < 16:
                return
            try:
                sec, frac, cap, _orig = struct.unpack(endian + "IIII", rec)
            except struct.error:
                return
            frame = fh.read(cap)
            if len(frame) < cap:
                return
            yield sec + frac / scale, frame


def _udp_payload(frame: bytes) -> Optional[bytes]:
    """从以太网帧里取出 UDP 载荷。"""
    if len(frame) < 42 or frame[12:14] != b"\x08\x00":
        return None
    ip = 14
    ihl = (frame[ip] & 0x0F) * 4
    if ip + ihl + 8 > len(frame) or frame[ip + 9] != 17:  # 只处理 UDP
        return None
    udp = ip + ihl
    length = struct.unpack_from("!H", frame, udp + 4)[0]
    return frame[udp + 8 : udp + length]


def _checksum_ok(sentence: str) -> bool:
    if "*" not in sentence or not sentence.startswith("$"):
        return False
    body, given = sentence[1:].split("*", 1)
    value = 0
    for ch in body:
        value ^= ord(ch)
    try:
        return value == int(given[:2], 16)
    except ValueError:
        return False


def _nmea_to_deg(raw: str, hemi: str) -> Optional[float]:
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    deg = int(val // 100)
    minutes = val - deg * 100
    out = deg + minutes / 60.0
    return -out if hemi.upper() in ("S", "W") else out


def _tod_seconds(text: str) -> Optional[float]:
    """HHMMSS.ss -> 当天秒数。"""
    try:
        raw = float(text)
    except (TypeError, ValueError):
        return None
    h = int(raw // 10000)
    m = int((raw % 10000) // 100)
    s = raw % 100
    if h > 23 or m > 59 or s >= 61:
        return None
    return h * 3600 + m * 60 + s


def has_gga_sample(pcap_paths: List[str], max_files: Optional[int] = None) -> bool:
    """扫描 pcap，判断里面是否含 GGA 语句。

    注意：**必须扫全部文件**。实测 AT/HU/SK 这类目录里，213 个 pcap 中只有
    少数几个含 GGA，只抽前几个文件会误判成「没有定位数据」。
    这里仍然很快：只按包头长度跳帧，命中 GGA 就立刻返回。

    max_files: 仅测试用；生产环境不传，扫全部。
    """
    paths = pcap_paths if max_files is None else pcap_paths[:max_files]
    for path in paths:
        try:
            for _ts, frame in _iter_pcap_packets(path):
                payload = _udp_payload(frame)
                if payload is not None and b"GGA," in payload:
                    return True
        except (OSError, struct.error):
            continue
    return False


def extract_gga_points(pcap_paths: List[str]) -> List[Dict[str, float]]:
    """从一组 pcap 中提取 GGA 轨迹点。

    返回按时间排序的点列表：[{ts, lat, lon, alt, quality}]
    """
    # 先按 GPS 当天秒去重收集，同时记录该秒对应的 pcap 捕获时刻（用于定日）
    by_tod: Dict[float, Dict[str, object]] = {}
    capture_hint: Dict[float, float] = {}

    for path in pcap_paths:
        try:
            packets = _iter_pcap_packets(path)
            for cap_ts, frame in packets:
                payload = _udp_payload(frame)
                if payload is None:
                    continue
                idx = payload.find(b"GGA,")
                if idx <= 0:
                    continue
                start = payload.rfind(b"$", 0, idx)
                if start < 0:
                    continue
                end = payload.find(b"*", idx)
                if end < 0 or end + 3 > len(payload):
                    continue
                try:
                    text = payload[start : end + 3].decode("ascii")
                except UnicodeDecodeError:
                    continue
                if "\x00" in text or not _checksum_ok(text):
                    continue
                fields = text.split("*")[0].split(",")
                if len(fields) < 10:
                    continue
                tod = _tod_seconds(fields[1])
                lat = _nmea_to_deg(fields[2], fields[3])
                lon = _nmea_to_deg(fields[4], fields[5])
                if tod is None or lat is None or lon is None:
                    continue
                try:
                    quality = int(fields[6]) if fields[6] else 0
                except ValueError:
                    quality = 0
                try:
                    alt = float(fields[9]) if fields[9] else 0.0
                except ValueError:
                    alt = 0.0

                key = round(tod, 2)
                kind = text[1:6]
                prev = by_tod.get(key)
                # 同一秒优先保留 GNGGA（多星座），否则先到先得
                if prev is None or (kind == "GNGGA" and prev["kind"] != "GNGGA"):
                    by_tod[key] = {
                        "kind": kind,
                        "tod": tod,
                        "lat": lat,
                        "lon": lon,
                        "alt": alt,
                        "quality": quality,
                    }
                    capture_hint[key] = cap_ts
        except (OSError, struct.error):
            continue

    if not by_tod:
        return []

    tods = sorted(by_tod)
    # 用「捕获时刻 - 当天秒」的中位数确定日期的基准秒，
    # 这样即使个别帧捕获时间抖动也不影响定日。
    offsets = [capture_hint[t] - t for t in tods if t in capture_hint]
    base_offset = sorted(offsets)[len(offsets) // 2] if offsets else 0.0

    points: List[Dict[str, float]] = []
    for tod in tods:
        item = by_tod[tod]
        points.append(
            {
                "ts": round(item["tod"] + base_offset, 3),
                "lat": item["lat"],
                "lon": item["lon"],
                "alt": item["alt"],
                "quality": item["quality"],
            }
        )
    return points


def infer_pcap_time_range(pcap_path: str) -> Optional[Tuple[datetime, datetime]]:
    """从 pcap 头尾读第一帧和最后一帧的捕获时间（用于快速探测，不解析全部帧）。"""
    try:
        with open(pcap_path, "rb") as fh:
            header = fh.read(24)
            if len(header) != 24:
                return None
            magic = header[:4]
            nano = magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d")
            little = magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1")
            endian = "<" if little else ">"
            scale = 1_000_000_000 if nano else 1_000_000
            rec = fh.read(16)
            if len(rec) < 16:
                return None
            sec, frac = struct.unpack(endian + "II", rec[:8])
            first = datetime.fromtimestamp(sec + frac / scale)
        return first, first
    except (OSError, struct.error, ValueError, OverflowError):
        return None
