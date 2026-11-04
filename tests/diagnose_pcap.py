"""单文件诊断：这份数据解析出来到底能画出什么图。

用法：
    python tests/diagnose_pcap.py <pcap 或 txt> [更多文件...]

为什么需要它：有一种失败长得像"文件解析成功，界面却所有图表空白"——文件里确实有行，
但可绘制的字段全是空的。常见原因有两个：
  1. 设备输出配置变了（GSOF49 的页号/掩码/字段布局与解析器预期不一致），解析器读到的
     偏移量落在别的内容上，值本身是 NaN/Infinity；
  2. 这些行是从别的记录拼出来的（例如只有 RMS 记录），没有位置/速度/姿态值。
服务端会把 NaN/Infinity 统一归一成 null（`sanitize_json`），前端再按"空值"处理，所以
界面上只会看到一片空白 + "解析成功"，完全没有线索。这个脚本直接摊开每个字段组的真实
有效行数与 NaN 计数，用来判断"是数据的问题还是界面的问题"。
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

# 放在 tests/ 下：先把仓库根目录补进 sys.path，并让默认数据目录与 cwd 无关（见 _repo.py）
from _repo import repo_path
import ap18_visualizer as viz
from parse_ap18_pcap import decode_file

# (组名, 字段, 是否关键组)。"关键组"是判断"界面是不是全空白"的依据：
# 位置/高程、速度/姿态、位置 RMS 三组全空 = 所有时间曲线都画不出来。
# RMS 拆成三组：位置/速度/姿态 RMS 是三条独立的 GSOF50 记录内容，
# 经常只有位置 RMS 有值（设备配置不同），拆开才能一眼看出"到底哪一组有数据"。
FIELD_GROUPS = [
    ("位置/高程", ("latitude", "longitude", "altitude"), True),
    ("速度", ("north_velocity", "east_velocity", "down_velocity", "total_speed"), True),
    ("姿态", ("roll", "pitch", "heading", "track_angle"), True),
    ("位置 RMS", ("north_position_rms", "east_position_rms", "down_position_rms"), True),
    ("速度 RMS", ("north_velocity_rms", "east_velocity_rms", "down_velocity_rms"), False),
    ("姿态 RMS", ("roll_rms", "pitch_rms", "heading_rms"), False),
    ("角速度/加速度", ("angular_rate_x", "angular_rate_y", "angular_rate_z",
                       "acceleration_x", "acceleration_y", "acceleration_z"), False),
    ("GPS 时间轴", ("gps_week", "gps_ms"), False),
    ("搜星/差分/HDOP", ("satellites_used", "differential_age", "hdop"), False),
]

NAN_HINT = (
    "NaN/Infinity 通常是记录布局与解析器不一致（设备输出配置/页号/掩码变了），"
    "服务端会把它们统一归一成空值，所以界面上表现为"
)
BLANK_VERDICT = (
    "这些字段全为空 -> 界面上会是『所有图表空白、却提示解析成功』"
)


def load_rows(path: Path) -> tuple[list[dict], dict[str, int]]:
    if path.suffix.lower() == ".pcap":
        return decode_file(path)
    return viz.parse_txt_file(path), {}


def group_report(rows: list[dict], keys: tuple[str, ...]) -> tuple[int, int]:
    """返回 (任一分量有有限数值的行数, 出现 NaN/Infinity 的值的个数)。"""
    valid_rows = 0
    nonfinite = 0
    for row in rows:
        any_valid = False
        for key in keys:
            value = row.get(key)
            if isinstance(value, float) and not math.isfinite(value):
                nonfinite += 1
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                any_valid = True
        if any_valid:
            valid_rows += 1
    return valid_rows, nonfinite


def diagnose(path: Path) -> bool:
    """返回 True 表示"能画出图"。"""
    try:
        rows, counts = load_rows(path)
    except Exception as exc:  # noqa: BLE001 - 诊断工具，坏文件只报原因
        print(f"{path.name}: 解析失败：{viz.failure_text(exc)}")
        return False

    header = f"{path.name}：{len(rows):,} 行"
    if counts:
        header += (
            f" · GSOF49={counts.get('gsof49_records', 0):,}"
            f" GSOF1={counts.get('gsof1_records', 0):,}"
            f" GSOF50={counts.get('gsof50_records', 0):,}"
            f" GGA={counts.get('gga_sentences', 0):,}"
            f" 无效记录={counts.get('invalid_records', 0):,}"
        )
    print(header)
    if not rows:
        print("  -> 没有任何行：界面会把它当作解析失败（no decodable records）")
        return False

    empty_primary: list[str] = []
    nan_groups: list[str] = []
    for label, keys, primary in FIELD_GROUPS:
        valid_rows, nonfinite = group_report(rows, keys)
        mark = "OK " if valid_rows else "-- "
        print(f"  [{mark}] {label:14s} {valid_rows:>7,} / {len(rows):,} 行有值"
              + (f" · NaN/Infinity {nonfinite:,} 个" if nonfinite else ""))
        if primary and not valid_rows:
            empty_primary.append(label)
        if nonfinite:
            nan_groups.append(label)

    # 值域：光看"有多少行有值"还判断不出速度是不是真实数据（也可能全是 0），
    # 把几个关键字段的范围一起打出来最省事。
    for key, label in (("total_speed", "总速度(m/s)"), ("altitude", "高程(m)"), ("heading", "航向(deg)")):
        values = [row[key] for row in rows if isinstance(row[key], (int, float)) and not isinstance(row[key], bool)
                  and math.isfinite(row[key])]
        if values:
            print(f"         {label:14s} min={min(values):.3f} max={max(values):.3f} 中位={sorted(values)[len(values)//2]:.3f}")

    if empty_primary:
        print(f"  -> ⚠ {BLANK_VERDICT}：{'、'.join(empty_primary)}")
        print(f"     {NAN_HINT}：{'、'.join(nan_groups) if nan_groups else '（本文件无 NaN，需查记录类型）'}")
        return False
    print("  -> ✓ 可正常绘制")
    if nan_groups:
        print(f"     注意：{'、'.join(nan_groups)} 里出现 NaN/Infinity，已经被归一成空值，"
              "对应曲线在该时刻会断开")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="单文件诊断：解析出来能画出什么图")
    parser.add_argument("inputs", nargs="+", type=Path, help=".pcap 或 .txt 文件")
    args = parser.parse_args()

    results = [diagnose(path) for path in args.inputs]
    if len(results) > 1:
        ok = sum(1 for value in results if value)
        print(f"\n合计：{ok} / {len(results)} 个文件能正常绘制")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
