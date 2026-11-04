#!/usr/bin/env python3
"""人工指定参数验证/里程可用性。

用途：某些日期的参数验证结论已知（例如欧洲除个别天外都通过），
但库里没有对应的自动验证/人工确认记录，用本脚本直接写「人工指定结论」。

优先级：人工指定（本脚本）> 自动验证/人工确认 > 地区默认。

示例：
  python tools/set_verify_availability.py --region 欧洲地区 \
      --failed-day 2026/07/30 --failed-day 2026/07/31 --failed-day 2026/08/01 \
      --failed-day 2026/08/08 --failed-day 2026/08/10 --failed-day 2026/08/11 \
      --failed-day 2026/08/20 --failed-day 2026/09/02 \
      --default passed
  python tools/set_verify_availability.py --region 欧洲地区 --list
  python tools/set_verify_availability.py --region 欧洲地区 --clear-day 20260808
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mileage_store  # noqa: E402


def norm_day(text: str) -> str:
    digits = "".join(ch for ch in str(text or "") if ch.isdigit())
    if len(digits) != 8:
        raise SystemExit(f"日期格式不对（需要 YYYYMMDD 或 YYYY/MM/DD）：{text}")
    return digits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="人工指定某地区某天的里程可用性")
    parser.add_argument("--region", required=True, help="项目地区，例如 欧洲地区")
    parser.add_argument("--failed-day", action="append", default=[], help="不通过的日期（可重复）")
    parser.add_argument("--passed-day", action="append", default=[], help="通过的日期（可重复）")
    parser.add_argument("--clear-day", action="append", default=[], help="清除该天的人工指定")
    parser.add_argument(
        "--default",
        choices=["passed", "failed", "unverified"],
        help="地区默认结论（该地区没有验证记录时使用）",
    )
    parser.add_argument("--clear-default", action="store_true", help="清除地区默认结论")
    parser.add_argument("--list", action="store_true", help="列出当前人工指定")
    parser.add_argument("--note", default="", help="备注")
    args = parser.parse_args(argv)

    region = args.region
    if args.list:
        overrides = mileage_store.list_verify_day_overrides()
        defaults = mileage_store.list_verify_region_defaults()
        print(f"地区：{region}")
        print("默认结论：", defaults.get(region, {}).get("state", "（未设置）"))
        print("人工指定日期：")
        for key, value in sorted(overrides.items()):
            if key.split("|", 1)[0] == region:
                print(" ", key.split("|", 1)[1], value.get("state"), value.get("verdict"))
        return 0

    if args.clear_default:
        with mileage_store._connect() as conn:
            conn.execute("DELETE FROM verify_region_defaults WHERE region=?", (region,))
        print("已清除地区默认结论")
    if args.default:
        mileage_store.set_verify_region_default(region, args.default, args.note)
        print(f"地区默认结论 -> {args.default}")

    for text in args.failed_day:
        day = norm_day(text)
        mileage_store.set_verify_day_override(region, day, "failed", args.note)
        print("不通过：", day)
    for text in args.passed_day:
        day = norm_day(text)
        mileage_store.set_verify_day_override(region, day, "passed", args.note)
        print("通过：", day)
    for text in args.clear_day:
        day = norm_day(text)
        mileage_store.clear_verify_day_override(region, day)
        print("清除：", day)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
