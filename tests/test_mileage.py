#!/usr/bin/env python3
"""端到端自检：AP18 解析 → 有效里程（2M / 8M / GPS / 全覆盖）。

不依赖真实采集数据：现场合成一份 AP18 pcap（GSOF49 记录）和 2M/8M 设备目录，
然后同时验证两件事：

1. **直连计算**：``ap18_mileage.analyze_directory()`` 的结果与手算的里程口径一致
   （GPS 覆盖里程、2M/8M 覆盖里程、全覆盖交集、并集与无覆盖里程的守恒关系）；
2. **HTTP 接口**：起一个真实服务，POST /api/mileage，验证响应结构、数值与错误分支。

用法：

    python tests/test_mileage.py

任何一项不通过都会打印 [FAIL] 并返回非零退出码。
"""

from __future__ import annotations

import datetime as dt
import json
import math
import struct
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from _repo import ROOT  # noqa: F401  (把仓库根目录补进 sys.path)

import ap18_mileage
import ap18_visualizer as viz

# Windows 控制台默认 GBK，遇到 ⇒ / ≈ 这类字符会直接抛 UnicodeEncodeError，
# 让自检结果消失在一堆乱码里。这里统一按 UTF-8 输出并对不可编码字符降级。
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        pass

FAILURES: list[str] = []

GPS_EPOCH = dt.datetime(1980, 1, 6, tzinfo=dt.timezone.utc)
GPS_UTC_LEAP_SECONDS = 18

# 合成轨迹参数：10 Hz、10 m/s，共 60 秒 => 600 点、599 段、约 599 m。
POINT_COUNT = 600
STEP_SECONDS = 0.1
SPEED_MPS = 10.0
START_LAT = 31.0
START_LON = 121.0
# 前 500 点 GNSS 固定解（RTK fixed = 4），后 100 点 DR 推算（6）=> 不稳定。
STABLE_POINTS = 500
STABLE_GNSS = 4
UNSTABLE_GNSS = 6
METERS_PER_DEG_LAT = 111_320.0


def check(condition: bool, label: str, detail: str = "") -> None:
    if condition:
        print(f"  [ OK ] {label}")
        return
    FAILURES.append(label)
    print(f"  [FAIL] {label}{(' - ' + detail) if detail else ''}")


# --------------------------------------------------------------------------
# 合成 pcap
# --------------------------------------------------------------------------


def gps_week_ms(epoch_seconds: float) -> tuple[int, int]:
    """Unix 秒 → (GPS 周, 周内毫秒)。与 parse_ap18_pcap.gps_datetime_utc 互逆。"""
    utc = dt.datetime.fromtimestamp(epoch_seconds, tz=dt.timezone.utc)
    gps = utc + dt.timedelta(seconds=GPS_UTC_LEAP_SECONDS)
    delta = gps - GPS_EPOCH
    week = delta.days // 7
    remainder = delta - dt.timedelta(weeks=week)
    return week, int(round(remainder.total_seconds() * 1000))


def gsof49_record(lat: float, lon: float, alt: float, gnss_status: int, epoch_seconds: float) -> bytes:
    """一条 GSOF49（0x31）导航记录：2 字节记录头 + 104 字节数据。"""
    week, ms = gps_week_ms(epoch_seconds)
    data = bytearray(104)
    struct.pack_into(">H", data, 0, week)
    struct.pack_into(">I", data, 2, ms)
    data[6] = 4  # imu_status: Full Nav
    data[7] = gnss_status
    struct.pack_into(">d", data, 8, lat)
    struct.pack_into(">d", data, 16, lon)
    struct.pack_into(">d", data, 24, alt)
    for offset in (32, 36, 40):  # north / east / down velocity
        struct.pack_into(">f", data, offset, 0.0)
    struct.pack_into(">f", data, 44, SPEED_MPS)  # total speed
    return bytes([0x31, len(data)]) + bytes(data)


def gsof_report(records: list[bytes]) -> bytes:
    """GSOF Report Package 0x40：STX + status + 0x40 + 长度 + 记录 + 校验和 + ETX。"""
    body = b"".join(records)
    data_length = 3 + len(body)
    payload = bytearray()
    payload += b"\x02\x00\x40"
    payload.append(data_length)
    payload += b"\x01\x00\x00"  # transmission / page index / max page index
    payload += body
    end = 4 + data_length
    payload.append(sum(payload[1:end]) & 0xFF)  # 校验和
    payload.append(0x03)  # ETX
    return bytes(payload)


def udp_frame(payload: bytes) -> bytes:
    ethernet = b"\x00" * 12 + b"\x08\x00"
    udp_length = 8 + len(payload)
    ip_total = 20 + udp_length
    ip = struct.pack(
        ">BBHHHBBH4s4s",
        0x45, 0, ip_total, 0, 0, 64, 17, 0,
        b"\x0a\x00\x00\x01", b"\x0a\x00\x00\x02",
    )
    udp = struct.pack(">HHHH", 1025, 28001, udp_length, 0)
    return ethernet + ip + udp + payload


def write_pcap(path: Path, packets: list[tuple[float, bytes]]) -> None:
    """写小端 pcap（微秒时间戳），与 parse_ap18_pcap.iter_pcap_packets 兼容。"""
    with path.open("wb") as fh:
        fh.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        for epoch_seconds, frame in packets:
            seconds = int(epoch_seconds)
            micros = int(round((epoch_seconds - seconds) * 1_000_000))
            fh.write(struct.pack("<IIII", seconds, micros, len(frame), len(frame)))
            fh.write(frame)


def build_dataset(root: Path) -> tuple[Path, float]:
    """在 root 下造 AP18 pcap + 2M/8M 设备目录，返回 (pcap 路径, 起点 epoch 秒)。"""
    # 本地时间 14:30:00 起算：里程计算器用 datetime.fromtimestamp() 与设备文件名比较，
    # 两者都走本地时区，因此这里也用 naive 本地时间生成 epoch。
    start_epoch = dt.datetime(2026, 9, 17, 14, 30, 0).timestamp()

    packets: list[tuple[float, bytes]] = []
    for index in range(POINT_COUNT):
        epoch = start_epoch + index * STEP_SECONDS
        lat = START_LAT + (index * SPEED_MPS * STEP_SECONDS) / METERS_PER_DEG_LAT
        gnss = STABLE_GNSS if index < STABLE_POINTS else UNSTABLE_GNSS
        record = gsof49_record(lat, START_LON, 10.0, gnss, epoch)
        packets.append((epoch, udp_frame(gsof_report([record]))))

    ap18_dir = root / "AP18"
    ap18_dir.mkdir(parents=True, exist_ok=True)
    pcap_path = ap18_dir / "synthetic_20260917_143000.pcap"
    write_pcap(pcap_path, packets)

    # 2M：从 14:30:00 起录，覆盖整条轨迹。
    dev_2m = root / "M57_2M_LYNK01_001_0917_1"
    dev_2m.mkdir(parents=True, exist_ok=True)
    # 设备文件要大于 MIN_DEVICE_FILE_BYTES（64 KB），否则会被当成占位文件忽略。
    placeholder_pad = b"\0" * (128 * 1024)
    (dev_2m / "M57_2M_LYNK01_001_0917_1_20260917_143000_0.pack").write_bytes(b"2m" + placeholder_pad)

    # 8M：14:30:25 起录，只覆盖后半段。
    dev_8m = root / "M57_8M_LYNK01_001_0917_1"
    dev_8m.mkdir(parents=True, exist_ok=True)
    (dev_8m / "M57_8M_LYNK01_001_0917_1_20260917_143025_0.pack").write_bytes(b"8m" + placeholder_pad)
    return pcap_path, start_epoch


# --------------------------------------------------------------------------
# 自检项
# --------------------------------------------------------------------------


def test_rows_to_points(pcap_path: Path) -> None:
    print("[1] decode_file → rows_to_points")
    from parse_ap18_pcap import decode_file

    rows, _counts = decode_file(pcap_path)
    check(len(rows) == POINT_COUNT, f"解析出 {POINT_COUNT} 行 GSOF49", f"实际 {len(rows)}")
    points = ap18_mileage.rows_to_points(rows)
    check(len(points) == POINT_COUNT, "全部行都转成了轨迹点", f"实际 {len(points)}")
    check(
        all(points[i].timestamp <= points[i + 1].timestamp for i in range(len(points) - 1)),
        "轨迹点按时间排序",
    )
    check(points[0].solution_status == STABLE_GNSS, "GNSS 状态透传（起点为固定解）")
    check(points[0].gps_stable is True, "固定解判定为 GPS 覆盖")
    check(points[-1].gps_stable is False, "DR 推算判定为不稳定")
    check(points[0].baseline_m is None, "AP18 轨迹没有 baseline（2M/8M 由覆盖时段决定）")


def test_analyze(data_root: Path) -> dict:
    print("[2] analyze_directory 四类里程口径")
    result = ap18_mileage.analyze_directory(data_root)
    check(result.get("ok") is True, "分析返回 ok")
    trajectory = result["trajectory"]
    mileage = result["mileage"]

    check(trajectory["point_count"] == POINT_COUNT, "点数正确", str(trajectory["point_count"]))
    # 前 500 点稳定 → 499 个稳定相邻段。期望值不再用「米/度」近似算，
    # 而是用同一套 Vincenty 求和：这样断言的是「聚合口径」而不是大地测量常数。
    ap18_mileage._service()  # 确保 mileage/ 已在 sys.path 上
    from distance_calc import vincenty_2d  # noqa: PLC0415

    def lat_at(index: int) -> float:
        return START_LAT + (index * SPEED_MPS * STEP_SECONDS) / METERS_PER_DEG_LAT

    def expected_sum(count: int) -> float:
        return sum(
            vincenty_2d(START_LON, lat_at(i), START_LON, lat_at(i + 1))
            for i in range(count - 1)
        )

    stable_segments = mileage["stable_segment_count"]
    check(stable_segments == STABLE_POINTS - 1, "GPS 覆盖段数 = 稳定点数-1", str(stable_segments))
    expected_stable = expected_sum(STABLE_POINTS)
    check(
        abs(mileage["gps_stable_m"] - expected_stable) < 0.01,
        f"GPS 覆盖里程 = 稳定段 Vincenty 求和（{expected_stable:.3f} m）",
        f"实际 {mileage['gps_stable_m']}",
    )
    expected_total = expected_sum(POINT_COUNT)
    check(
        abs(mileage["total_m"] - expected_total) < 0.01,
        f"总轨迹长度 = 全部相邻段求和（{expected_total:.3f} m，含不稳定段）",
        str(mileage["total_m"]),
    )

    # 2M 覆盖整条轨迹 → 2M 覆盖里程 == GPS 覆盖里程
    check(
        abs(mileage["covered_2m_m"] - mileage["gps_stable_m"]) < 1e-3,
        "2M 覆盖整条轨迹 ⇒ 2M 覆盖里程 = GPS 覆盖里程",
        f"{mileage['covered_2m_m']} vs {mileage['gps_stable_m']}",
    )
    # 8M 从第 25 秒起录 → 只覆盖后半段
    check(
        0 < mileage["covered_8m_m"] < mileage["covered_2m_m"],
        "8M 只覆盖后半段，里程小于 2M",
        f"8M={mileage['covered_8m_m']} 2M={mileage['covered_2m_m']}",
    )
    # 8M 区间完全落在 2M 区间内 → 全覆盖 == 8M 覆盖里程
    check(
        abs(mileage["both_m"] - mileage["covered_8m_m"]) < 1e-3,
        "8M 区间是 2M 子集 ⇒ 全覆盖里程 = 8M 覆盖里程",
        f"both={mileage['both_m']} 8m={mileage['covered_8m_m']}",
    )
    # 守恒关系：并集 = 2M + 8M - 交集；GPS覆盖 = 并集 + 无覆盖
    check(
        abs(mileage["any_m"] - (mileage["covered_2m_m"] + mileage["covered_8m_m"] - mileage["both_m"])) < 1e-3,
        "实际有效里程 = 2M ∪ 8M（容斥）",
        str(mileage["any_m"]),
    )
    check(
        abs(mileage["any_m"] + mileage["no_cover_m"] - mileage["gps_stable_m"]) < 1e-3,
        "并集 + 无覆盖 = GPS 覆盖里程",
        f"{mileage['any_m']} + {mileage['no_cover_m']} vs {mileage['gps_stable_m']}",
    )
    check(
        abs(mileage["effective_m"] - mileage["both_m"]) < 1e-3,
        "有效里程 = 三者交集（GPS覆盖 ∩ 2M ∩ 8M）",
        f"effective={mileage['effective_m']} both={mileage['both_m']}",
    )
    check(
        mileage["effective_m"] <= mileage["any_m"] + 1e-6,
        "有效里程（交集）不超过任一覆盖（并集）",
        f"{mileage['effective_m']} vs {mileage['any_m']}",
    )
    check(mileage["no_cover_m"] == 0.0, "2M 覆盖整条轨迹 ⇒ 无覆盖里程为 0")

    coverage = result["coverage"]
    check(coverage["2m"]["present"] is True, "识别到 2M 设备目录")
    check(coverage["8m"]["present"] is True, "识别到 8M 设备目录")
    check(coverage["2m"]["interval_count"] == 1, "2M 推导出 1 个录制区间")
    check(coverage["8m"]["start"].endswith("14:30:25"), "8M 起始时刻按文件名推导", str(coverage["8m"]["start"]))
    # 单文件覆盖时长必须按「区间起点 → 区间结束」算成 60 秒，不能因为
    # probe 的 end 取的是最大起始时刻而显示成 0 秒。
    check(
        coverage["2m"]["duration_s"] == 60.0,
        "单文件 2M 的覆盖时长 = 60 秒（不是 0）",
        str(coverage["2m"]["duration_s"]),
    )
    check(
        coverage["8m"]["end"].endswith("14:31:25"),
        "8M 结束时刻 = 起点 + 60 秒",
        str(coverage["8m"]["end"]),
    )
    check(
        coverage["8m"]["covered_seconds"] == 60.0,
        "8M 覆盖总秒数 = 60",
        str(coverage["8m"]["covered_seconds"]),
    )

    # ---- 覆盖时间轴数据（前端画 2M/8M/GPS 对比图用）----
    timeline = coverage.get("timeline") or {}
    spans_2m = timeline.get("2m") or []
    spans_8m = timeline.get("8m") or []
    spans_gps = timeline.get("gps_stable") or []
    spans_both = timeline.get("both") or []
    check(bool(timeline), "返回覆盖时间轴数据")
    check(timeline.get("start", 0) < timeline.get("end", 0), "时间轴起止顺序正确")
    check(len(spans_2m) == 1, "2M 单文件合并成 1 段", str(len(spans_2m)))
    check(
        abs((spans_2m[0][1] - spans_2m[0][0]) - 60.0) < 0.01,
        "2M 时间轴区间长度 = 60 秒",
        str(spans_2m),
    )
    check(len(spans_8m) == 1, "8M 单文件合并成 1 段", str(len(spans_8m)))
    check(
        len(spans_gps) >= 1,
        "GPS 覆盖段合并后非空",
        str(len(spans_gps)),
    )
    check(
        abs((spans_gps[-1][1] - spans_gps[0][0]) - (STABLE_POINTS - 1) * STEP_SECONDS) < 0.2,
        "GPS 覆盖段总跨度 ≈ 稳定点数 × 采样间隔",
        str(spans_gps),
    )
    # 2M = [14:30:00, 14:31:00)，8M = [14:30:25, 14:31:25) ⇒ 交集 = [14:30:25, 14:31:00) = 35 秒
    check(
        bool(spans_both) and abs((spans_both[0][1] - spans_both[0][0]) - 35.0) < 0.01,
        "全覆盖区间 = 2M∩8M（本用例应为 35 秒）",
        str(spans_both),
    )
    check(
        bool(spans_both) and abs(spans_both[0][0] - spans_8m[0][0]) < 0.01,
        "全覆盖区间起点 = 8M 起点",
        str(spans_both),
    )
    check(result["pcaps"]["file_count"] == 1, "统计到 1 个 pcap")
    return result


def test_errors(tmp: Path, pcap_path: Path) -> None:
    print("[3] 错误分支 / 缺传感器时的降级")
    missing = tmp / "not-exist"
    try:
        ap18_mileage.analyze_directory(missing)
        check(False, "不存在的目录应报错")
    except ap18_mileage.MileageAnalysisError as exc:
        check("目录不存在" in str(exc), "不存在的目录给出可读错误", str(exc))

    empty = tmp / "empty_dir"
    empty.mkdir(parents=True, exist_ok=True)
    try:
        ap18_mileage.analyze_directory(empty)
        check(False, "空目录应报错")
    except ap18_mileage.MileageAnalysisError as exc:
        check(bool(str(exc)), "空目录给出可读错误", str(exc))

    # 只有 pcap、没有 2M/8M：不阻断，GPS 覆盖里程照算，缺传感器写进 issues
    partial_root = tmp / "only_pcap"
    (partial_root / "AP18").mkdir(parents=True, exist_ok=True)
    (partial_root / "AP18" / "only.pcap").write_bytes(pcap_path.read_bytes())
    partial = ap18_mileage.analyze_directory(partial_root)
    check(partial["coverage"]["2m"]["present"] is False, "缺 2M 时 present=false")
    check(partial["coverage"]["8m"]["present"] is False, "缺 8M 时 present=false")
    check(partial["mileage"]["both_m"] == 0.0, "缺覆盖时全覆盖里程为 0")
    check(partial["mileage"]["gps_stable_m"] > 0, "缺传感器仍能算出 GPS 覆盖里程")
    check(
        any("2M" in item for item in partial["issues"]) and any("8M" in item for item in partial["issues"]),
        "缺 2M/8M 会列进 issues",
        str(partial["issues"]),
    )


def test_scan(tmp: Path) -> None:
    print("[5] 批量刷写扫描")
    result = ap18_mileage.scan_batch_dates(tmp)
    check(result.get("ok") is True, "扫描返回 ok")
    check(result.get("count") == 1, "识别到 1 个日期目录", str(result.get("count")))
    day = result["dates"][0]
    check(day.get("date") == "20260917", "日期目录名正确", str(day.get("date")))
    check(day.get("ready") is True, "该天可刷")
    check(
        day.get("pcap_files") == 1 and day.get("files_2m") == 1 and day.get("files_8m") == 1,
        "预检统计到 pcap / 2M / 8M",
        str(day),
    )

    (tmp / "not_a_date").mkdir(exist_ok=True)
    (tmp / "2026-09-18").mkdir(exist_ok=True)
    result2 = ap18_mileage.scan_batch_dates(tmp)
    check(result2.get("count") == 1, "忽略非 YYYYMMDD 目录", str(result2.get("count")))

    try:
        ap18_mileage.scan_batch_dates(tmp / "no-such-parent")
        check(False, "不存在的父目录应报错")
    except ap18_mileage.MileageAnalysisError:
        check(True, "不存在的父目录给出可读错误")


def test_http(data_root: Path, expected: dict) -> None:
    print("[4] POST /api/mileage（真实 HTTP）")
    import mileage_store  # noqa: PLC0415

    # 把 SQLite 指到临时目录，避免污染仓库 data/mileage.db
    store_dir = data_root.parent / "store"
    store_dir.mkdir(parents=True, exist_ok=True)
    mileage_store.DB_PATH = store_dir / "mileage.db"
    mileage_store._initialized = False

    server = ThreadingHTTPServer(("127.0.0.1", 0), viz.make_handler())
    server.request_logging = False  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    try:
        def post(body: dict | bytes, path: str = "/api/mileage") -> tuple[int, dict]:
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            request = urllib.request.Request(
                f"{base}{path}", data=raw, method="POST",
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    return response.status, json.loads(response.read().decode())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read().decode())

        status, payload = post({"data_root": str(data_root)})
        check(status == 200, "200 OK", f"status={status}")
        check(payload.get("ok") is True, "响应 ok=true")
        check(
            abs(payload["mileage"]["both_m"] - expected["mileage"]["both_m"]) < 1e-6,
            "HTTP 结果与直连计算一致",
            f"{payload['mileage']['both_m']} vs {expected['mileage']['both_m']}",
        )
        check(payload["coverage"]["2m"]["present"] is True, "HTTP 响应带覆盖明细")
        check(payload["trajectory"]["point_count"] == POINT_COUNT, "HTTP 响应带轨迹摘要")
        check(payload.get("cached") is False, "首次分析标记为新算")
        check(bool(payload.get("computed_at")), "返回计算时间")

        # ---- SQLite：缓存命中 / 强制重算 / 历史检索 ----
        status, cached_payload = post({"data_root": str(data_root)})
        check(status == 200 and cached_payload.get("cached") is True, "重复分析命中数据库缓存")
        check(
            abs(cached_payload["mileage"]["effective_m"] - payload["mileage"]["effective_m"]) < 1e-6,
            "缓存结果与首次一致",
        )
        status, refreshed = post({"data_root": str(data_root), "refresh": True})
        check(refreshed.get("cached") is False, "refresh=true 强制重算")

        with urllib.request.urlopen(f"{base}/api/mileage/history?limit=10", timeout=30) as response:
            history = json.loads(response.read().decode())
        check(history.get("ok") is True and history.get("total", 0) >= 1, "历史接口返回记录")
        record_id = history["items"][0]["id"]
        with urllib.request.urlopen(f"{base}/api/mileage/record?id={record_id}", timeout=30) as response:
            record = json.loads(response.read().decode())
        check(
            bool((record.get("result") or {}).get("ok")),
            "历史记录可载入完整结果",
        )
        with urllib.request.urlopen(f"{base}/api/mileage/stats", timeout=30) as response:
            stats = json.loads(response.read().decode())
        check(stats.get("days", 0) >= 1 and stats.get("history_runs", 0) >= 1, "数据库状态接口正常")

        status, payload = post({"data_root": str(data_root / "nope")})
        check(status == 400, "不存在的目录返回 400", f"status={status}")
        check(bool(payload.get("error")), "错误响应带 error 字段")

        status, payload = post(b"not-json")
        check(status == 400, "非法 JSON 返回 400", f"status={status}")

        status, payload = post({})
        check(status == 400, "缺少 data_root 返回 400", f"status={status}")

        status, scan = post({"data_root": str(data_root.parent)}, path="/api/mileage/scan")
        check(status == 200 and scan.get("ok") is True, "批量扫描接口 200", f"status={status}")
        check(scan.get("count") == 1 and scan.get("ready_count") == 1, "批量扫描识别 1 天且可刷")
        check(
            scan["dates"][0].get("analyzed") is True,
            "扫描结果标注库里已有结果（增量刷写依据）",
            str(scan["dates"][0].get("analyzed")),
        )
        status, scan_bad = post({"data_root": str(data_root / "nope")}, path="/api/mileage/scan")
        check(status == 400, "批量扫描不存在的目录返回 400", f"status={status}")

        # 页面与独立脚本本身可访问
        with urllib.request.urlopen(f"{base}/mileage.html", timeout=30) as response:
            html = response.read().decode()
        check(response.status == 200 and "有效里程" in html, "独立页面 /mileage.html 可访问")
        with urllib.request.urlopen(f"{base}/mileage.js", timeout=30) as response:
            script = response.read().decode()
        check("/api/mileage" in script, "独立页面脚本指向 /api/mileage")
        with urllib.request.urlopen(f"{base}/index.html", timeout=30) as response:
            index_html = response.read().decode()
        check("openMileageButton" in index_html, "原页面带「有效里程」入口按钮")
    finally:
        server.shutdown()
        server.server_close()


def test_ins_variant() -> None:
    """俄罗斯 E5 / rtkimu 这类 GPFPD 变种：累加和校验、08/09 状态码、GPS 时间基准、双日期文件名。"""
    import types

    print("[6] 惯导变种（rtkimu / E5）适配")
    svc = ap18_mileage._service()
    from trajectory_parser import (  # noqa: PLC0415
        STATUS_LABEL_GPFPD,
        STATUS_LABEL_GPFPD_VARIANT,
        parse_line,
    )

    def gpfpd_line(status: int, *, additive: bool) -> str:
        payload = (
            "GPFPD,2438,561365.510,112.127,-1.71,-0.25,59.93968031,30.45374909,"
            f"22.40,-0.008,-0.003,-0.004,0.000, 0,38,{status:02d}"
        )
        if additive:
            checksum = sum(ord(ch) for ch in "$" + payload) & 0xFF
        else:
            checksum = 0
            for ch in payload:
                checksum ^= ord(ch)
        return f"${payload}*{checksum:02X}"

    point8 = parse_line(gpfpd_line(8, additive=True), line_no=1)
    check(point8 is not None, "累加和 GPFPD 能解析")
    check(point8.checksum_valid is True, "累加和校验通过")
    check(point8.status_label == STATUS_LABEL_GPFPD_VARIANT, "识别为 GPFPD 变种状态口径")
    check(point8.solution_status_text == "组合导航正常", "状态 08 文本正确")
    check(point8.gps_stable is True, "状态 08 判为 GPS 覆盖")

    point9 = parse_line(gpfpd_line(9, additive=True), line_no=2)
    check(point9 is not None and point9.gps_stable is False, "状态 09（静止）判为不稳定")

    standard = parse_line(gpfpd_line(12, additive=False), line_no=3)
    check(standard is not None and standard.status_label == STATUS_LABEL_GPFPD, "标准异或仍走原口径")
    check(standard.gps_stable is True, "标准 GPFPD 状态 12 仍稳定")

    expected_ts = 315964800 + 2438 * 7 * 86400 + 561365.510 - 18
    check(
        abs(point8.timestamp - expected_ts) < 0.01,
        "GPS 周/周内秒换成 Unix 秒（含 GPS 纪元与闰秒）",
        f"{point8.timestamp} vs {expected_ts}",
    )

    stamp = svc._parse_stamp_from_name("M57-2M_E5-001_20261003_20261003_180047_017.vin")
    check(
        stamp == dt.datetime(2026, 10, 3, 18, 0, 47) + dt.timedelta(minutes=17),
        "双日期文件名取最后一个日期+时刻（+组号×60s）",
        str(stamp),
    )
    legacy = svc._parse_stamp_from_name("M57_2M_LYNK01_001_0917_1_20260917_142635_110.pack")
    check(
        legacy == dt.datetime(2026, 9, 17, 14, 26, 35) + dt.timedelta(minutes=110),
        "老命名仍然正确",
        str(legacy),
    )

    # 设备文件名时间默认按本机时区解释（实测设备时钟=北京时间），不做平移。
    from config import DEVICE_TIMEZONE_OFFSET_HOURS  # noqa: PLC0415

    check(
        DEVICE_TIMEZONE_OFFSET_HOURS is None,
        "默认不做设备时区平移（设备时钟=本机时区）",
        str(DEVICE_TIMEZONE_OFFSET_HOURS),
    )

def test_pacp_extension(tmp: Path) -> None:
    """采集工具把后缀写成 .pacp 时也要能识别（真实案例：SK/20260908/AP18）。"""
    print("[7] .pacp 后缀兼容")
    root = tmp / "pacp_day"
    root.mkdir(parents=True, exist_ok=True)
    build_dataset(root)
    original = next((root / "AP18").glob("*.pcap"))
    renamed = original.with_suffix(".pacp")
    original.rename(renamed)

    svc = ap18_mileage._service()
    info = svc.probe_mileage_dataset(root)
    check(len(info["pcap_files"]) == 1, "探测器识别 .pacp 轨迹文件", str(info["pcap_files"]))
    result = ap18_mileage.analyze_directory(root)
    check(result["trajectory"]["point_count"] == POINT_COUNT, ".pacp 能正常解析出轨迹点")
    check(result["mileage"]["gps_stable_m"] > 0, ".pacp 能算出 GPS 覆盖里程")

    top_level = tmp / "pacp_top"
    (top_level / "AP18").mkdir(parents=True, exist_ok=True)
    (top_level / "AP18" / "x.pacp").write_bytes(renamed.read_bytes())
    top_info = svc.probe_mileage_dataset(top_level)
    check(len(top_info["pcap_files"]) == 1, "子目录里的 .pacp 被识别")
    (top_level / "AP18" / "x.pacp").rename(top_level / "x.pacp")
    direct = svc.probe_mileage_dataset(top_level)
    check(len(direct["pcap_files"]) == 1, "顶层 .pacp 也被识别（用户直接选 AP18 目录的场景）")


def test_placeholder_device_files(tmp: Path) -> None:
    """设备目录里只有占位文件（<64 KB）时不能算覆盖，有效里程必须是 0。

    真实案例：E5 Russia 20260923 的 vin_device_2M 是空的、vin_device_8M 里
    4424 个文件总共只有 6.6 MB（.vin 4 字节、.blf 308 字节），以前会被当成
    「全程在录」，把有效里程算成 61 km。
    """
    print("[8] 占位设备文件不计入覆盖")
    root = tmp / "placeholder_day"
    root.mkdir(parents=True, exist_ok=True)
    build_dataset(root)
    # 把真实设备文件换成 4 字节占位文件
    for device, stamp in (("M57_2M_LYNK01_001_0917_1", "20260917_143000"),):
        folder = root / device
        for path in folder.iterdir():
            path.unlink()
        (folder / f"M57_2M_LYNK01_001_0917_1_{stamp}_0.pack").write_bytes(b"tiny")

    result = ap18_mileage.analyze_directory(root)
    mileage = result["mileage"]
    coverage = result["coverage"]
    check(coverage["2m"]["present"] is False, "2M 占位文件不算覆盖")
    check(coverage["2m"]["ignored_small_files"] >= 1, "统计到被忽略的占位文件")
    check(mileage["effective_m"] == 0.0, "有效里程（三者交集）为 0", str(mileage["effective_m"]))
    check(mileage["gps_stable_m"] > 0, "GPS 覆盖里程仍然算得出来")
    check(
        any("占位" in item for item in result["issues"]),
        "issues 里说明是占位文件",
        str(result["issues"]),
    )


def test_nested_device_dirs(tmp: Path) -> None:
    """设备目录里再套一层会话子目录也要能扫到（E5 Russia 的 vin_device_2M 就是这种）。"""
    print("[9] 设备目录嵌套会话子目录")
    root = tmp / "nested_day"
    root.mkdir(parents=True, exist_ok=True)
    build_dataset(root)
    device = root / "M57_2M_LYNK01_001_0917_1"
    session = device / "20260917-1_2M"
    session.mkdir()
    for path in list(device.iterdir()):
        if path.is_file():
            path.rename(session / path.name)
    # 设备目录里混进的惯导子目录要被跳过
    (session / "rtkimu_qj_7683").mkdir()
    (session / "rtkimu_qj_7683" / "traj_20260917_143000_0.txt").write_bytes(b"x" * (128 * 1024))

    result = ap18_mileage.analyze_directory(root)
    coverage = result["coverage"]
    check(coverage["2m"]["present"] is True, "嵌套子目录里的 2M 文件被扫到")
    check(coverage["2m"]["file_count"] >= 1, "2M 文件数大于 0", str(coverage["2m"]["file_count"]))
    check(coverage["2m"]["interval_count"] >= 1, "2M 推导出录制区间")
    check(result["mileage"]["effective_m"] > 0, "嵌套目录下仍能算出有效里程")


def test_nested_scan(tmp: Path) -> None:
    """批量扫描要能向下找两层日期目录（.../Overseas/9/20260923 这种）。"""
    print("[10] 批量扫描递归找日期目录")
    day = tmp / "nested_parent" / "9" / "20260917"
    day.mkdir(parents=True, exist_ok=True)
    build_dataset(day)
    scan = ap18_mileage.scan_batch_dates(tmp / "nested_parent")
    check(scan["count"] == 1, "父目录下两层的日期目录被扫到", f"count={scan['count']}")
    check(scan["ready_count"] == 1, "该天可刷")
    check(
        Path(scan["dates"][0]["path"]) == day,
        "返回的 path 是日期目录本身（批量刷写用它拼请求）",
        scan["dates"][0]["path"],
    )
    result = ap18_mileage.analyze_directory(scan["dates"][0]["path"])
    check(result["trajectory"]["point_count"] == POINT_COUNT, "按扫描返回的 path 能直接分析")
    # 不存在 + UNC 路径要给可读提示
    try:
        ap18_mileage.scan_batch_dates(r"\\\\192.168.20.206\\logs_new\\not_exist")
    except ap18_mileage.MileageAnalysisError as exc:
        check("logs_new" in str(exc) or "目录不存在" in str(exc), "UNC 路径不存在时有可读报错", str(exc))
    else:
        check(False, "不存在的 UNC 路径应报错")


def test_auto_align_8m() -> None:
    base = dt.datetime(2026, 9, 27, 16, 0, 0)
    intervals_2m = [
        (base, base + dt.timedelta(minutes=10)),
        (base + dt.timedelta(hours=2), base + dt.timedelta(hours=2, minutes=10)),
    ]
    shifted = [
        (start + dt.timedelta(hours=16), end + dt.timedelta(hours=16))
        for start, end in intervals_2m
    ]
    aligned, hours = ap18_mileage._auto_align_8m(intervals_2m, shifted)
    check(hours == -16, "8M 时钟偏差自动识别为 -16 小时", f"hours={hours}")
    check(aligned[0][0] == intervals_2m[0][0], "自动对齐后 8M 时间轴和 2M 一致", str(aligned[:1]))
    same, hours_same = ap18_mileage._auto_align_8m(intervals_2m, list(intervals_2m))
    check(hours_same == 0 and same == intervals_2m, "本来就对齐时不乱平移", f"hours={hours_same}")


def main() -> int:
    print("AP18 有效里程自检（合成数据，不依赖真实采集）\n")
    with tempfile.TemporaryDirectory(prefix="ap18_mileage_test_") as temp_dir:
        root = Path(temp_dir) / "20260917"
        root.mkdir(parents=True, exist_ok=True)
        pcap_path, _start = build_dataset(root)

        test_rows_to_points(pcap_path)
        expected = test_analyze(root)
        test_errors(Path(temp_dir), pcap_path)
        test_scan(Path(temp_dir))
        test_http(root, expected)
        test_ins_variant()
        test_pacp_extension(Path(temp_dir))
        test_placeholder_device_files(Path(temp_dir))
        test_nested_device_dirs(Path(temp_dir))
        test_nested_scan(Path(temp_dir))
        test_auto_align_8m()

    print()
    if FAILURES:
        print(f"结果：{len(FAILURES)} 项失败")
        for label in FAILURES:
            print(f"  - {label}")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
