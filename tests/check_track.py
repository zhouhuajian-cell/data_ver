#!/usr/bin/env python3
"""诊断 AP18 PCAP 的轨迹质量：经纬度跨度、野点、跳变、GNSS/IMU 状态分布。

用来回答"轨迹为什么很乱 / 地图上为什么看不到轨迹"：

* 如果打印出很大的跨度、或存在越界/零点，说明是野点污染了 min/max 归一化，
  局部平面轨迹会被压成一点、地图 fitBounds 会被拉到跨洲级 —— 属于渲染需要加固。
* 如果范围正常但是 GNSS 状态大量落在 Fix not available / DR Mode，
  说明是设备本身没有固定解、位置在真实漂移 —— 属于数据问题。

用法：
    python tests/check_track.py AP18\\*.pcap
    python tests/check_track.py                      # 默认扫描仓库根目录下的 AP18 目录
"""

from __future__ import annotations

import argparse
import collections
import math
from pathlib import Path

# 放在 tests/ 下：先把仓库根目录补进 sys.path，并让默认数据目录与 cwd 无关（见 _repo.py）
from _repo import collect_pcaps, no_data_error
from parse_ap18_pcap import decode_file

JUMP_METERS = 100.0  # 相邻两点超过这个距离就计一次跳变


def _meters_per_degree_lon(latitude: float) -> float:
    return 111320.0 * math.cos(math.radians(latitude))


def check(path: Path) -> None:
    rows, counts = decode_file(path)
    print(f"\n=== {path.name} ===")
    print(
        f"导航记录 {len(rows)} | GSOF49={counts['gsof49_records']} GSOF1={counts['gsof1_records']} "
        f"GSOF50={counts['gsof50_records']} GGA={counts['gga_sentences']} "
        f"GGA匹配行={counts['gga_matched_rows']}"
    )
    if not rows:
        print("没有可解析的导航记录")
        return

    lats = [float(row["latitude"]) for row in rows]
    lons = [float(row["longitude"]) for row in rows]
    mid_lat = sum(lats) / len(lats)
    lat_span = (max(lats) - min(lats)) * 111320.0
    lon_span = (max(lons) - min(lons)) * _meters_per_degree_lon(mid_lat)
    print(f"纬度 min={min(lats):.7f} max={max(lats):.7f} 跨度≈{lat_span:,.1f} m")
    print(f"经度 min={min(lons):.7f} max={max(lons):.7f} 跨度≈{lon_span:,.1f} m")

    invalid = [
        index
        for index, row in enumerate(rows)
        if not -90.0 <= float(row["latitude"]) <= 90.0
        or not -180.0 <= float(row["longitude"]) <= 180.0
        or (abs(float(row["latitude"])) < 1e-9 and abs(float(row["longitude"])) < 1e-9)
    ]
    if invalid:
        shown = [rows[index]["index"] for index in invalid[:5]]
        print(f"** 越界/零坐标：{len(invalid)} 条，前几个序号 {shown}")
    else:
        print("越界/零坐标：0 条")

    jumps = 0
    worst = 0.0
    for previous, current in zip(rows, rows[1:]):
        dy = (float(current["latitude"]) - float(previous["latitude"])) * 111320.0
        dx = (
            (float(current["longitude"]) - float(previous["longitude"]))
            * _meters_per_degree_lon(float(previous["latitude"]))
        )
        distance = math.hypot(dx, dy)
        if distance > JUMP_METERS:
            jumps += 1
            worst = max(worst, distance)
    print(f"相邻点跳变 >{JUMP_METERS:.0f} m：{jumps} 次 / {len(rows) - 1} 段，最大单步 {worst:,.1f} m")

    print("GNSS 状态：", dict(collections.Counter(row["gnss_status_text"] for row in rows)))
    print("IMU 对准状态：", dict(collections.Counter(row["imu_status_text"] for row in rows)))
    print(
        "搜星数来源：",
        dict(collections.Counter(str(row.get("satellites_source")) for row in rows)),
    )
    satellite_values = [
        int(row["satellites_used"]) for row in rows if isinstance(row.get("satellites_used"), int)
    ]
    if satellite_values:
        print(f"搜星数 min={min(satellite_values)} max={max(satellite_values)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="*", type=Path, help="pcap 文件或目录")
    args = parser.parse_args()

    targets, missing = collect_pcaps(args.inputs)
    if not targets:
        no_data_error(parser, "check_track.py", missing)

    for path in targets:
        try:
            check(path)
        except Exception as exc:  # noqa: BLE001 - 单个文件失败不影响其它文件
            print(f"\n=== {path.name} ===\n解析失败：{exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
