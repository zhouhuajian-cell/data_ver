"""自动里程扫描（tools/auto_mileage_scan.py）纯函数自检：不依赖数据库/网络。"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location(
    "auto_mileage_scan", ROOT / "tools" / "auto_mileage_scan.py"
)
scan = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(scan)

failures = 0


def check(condition, name, detail=""):
    global failures
    if condition:
        print(f"  [ OK ] {name}")
    else:
        failures += 1
        print(f"  [FAIL] {name} - {detail}")


def test_discover():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        (base / "20260101").mkdir()
        (base / "20261010").mkdir()
        (base / "20260927-不可用").mkdir()
        (base / "not_a_date").mkdir()
        # 日志盘真实布局：BR/2026/8/20260805（4 层）
        (base / "BR" / "2026" / "8" / "20260805").mkdir(parents=True)
        (base / "未验证" / "20260804").mkdir(parents=True)
        items = scan.discover_days([{"root": base, "since": ""}])
        names = [item["date"] for item in items]
        check(names[0] == "20261010", "日期目录按新到旧排序", str(names))
        check("20260101" in names, "识别 8 位日期目录", str(names))
        check("20260927-不可用" not in names, "跳过名字带不可用的目录", str(names))
        check("not_a_date" not in names, "跳过非日期目录", str(names))
        check("20260805" in names, "深层日期目录也能找到（BR/2026/8/20260805）", str(names))
        check("20260804" not in names, "未验证目录下的日期跳过", str(names))
        since_items = scan.discover_days([{"root": base, "since": "20261001"}])
        since_names = [item["date"] for item in since_items]
        check(since_names == ["20261010"], "since 只保留该日期及之后", str(since_names))
        empty = next(item for item in items if item["date"] == "20261010")
        check(empty.get("has_trajectory") is False, "空日期目录预检为不完整", str(empty))


def _touch_old(path: Path, age_s: float) -> None:
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))


def test_decide():
    with tempfile.TemporaryDirectory() as tmp:
        day = Path(tmp) / "20260101"
        (day / "AP18").mkdir(parents=True)
        pcap = day / "AP18" / "a.pcap"
        pcap.write_bytes(b"x")
        now = time.time()

        action, reason = scan.decide(day, None, now=now, min_age_min=30)
        check(action == "wait", "刚写入的数据先等待", f"{action} / {reason}")

        _touch_old(pcap, 3 * 3600)
        _touch_old(day / "AP18", 3 * 3600)
        _touch_old(day, 3 * 3600)
        _, _ = scan.decide(day, None, now=now, min_age_min=30)
        action, reason = scan.decide(day, None, now=now, min_age_min=30)
        check(action == "analyze", "稳定且库里没有结果时分析", f"{action} / {reason}")

        recent = time.strftime("%Y-%m-%d %H:%M:%S")
        action, reason = scan.decide(
            day, {"computed_at": recent}, now=now, min_age_min=30
        )
        check(action == "skip", "数据没变且已算过时跳过", f"{action} / {reason}")

        action, reason = scan.decide(
            day, {"computed_at": "2000-01-01 00:00:00"}, now=now, min_age_min=30
        )
        check(action == "analyze", "数据比上次计算新时重算", f"{action} / {reason}")


def test_failure_cooldown():
    check(scan.failure_cooldown_s("") == 6 * 3600, "普通失败 6 小时后再试")
    check(
        scan.failure_cooldown_s("目录里既没有 AP18 pcap，也没有可解析的 .txt/.csv 轨迹文件") == 7 * 24 * 3600,
        "数据缺失类失败 7 天后再试",
    )


def test_parse_time():
    check(scan.parse_time("2026-10-23 09:00:00") > 0, "时间字符串可解析")
    check(scan.parse_time("") == 0.0, "空时间回退 0")
    check(scan.parse_time("bad") == 0.0, "坏时间回退 0")


def test_load_roots():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "roots.txt"
        cfg.write_text(
            "# 注释\n/mnt/a\n\n/mnt/b since=20261021\n/mnt/b since=20261021\n/mnt/a\n",
            encoding="utf-8",
        )
        roots = scan.load_roots(cfg)
        check(
            [item["root"] for item in roots] == [Path("/mnt/a"), Path("/mnt/b")],
            "配置文件去重且忽略注释",
            str(roots),
        )
        check(roots[0]["since"] == "" and roots[1]["since"] == "20261021", "since 解析正确", str(roots))
        check(scan.load_roots(Path(tmp) / "missing.txt") == [], "配置文件不存在返回空")


def main():
    test_discover()
    test_decide()
    test_parse_time()
    test_load_roots()
    test_failure_cooldown()
    if failures:
        print(f"结果：{failures} 项失败")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
