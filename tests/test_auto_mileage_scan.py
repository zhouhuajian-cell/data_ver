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


def test_dedupe_day_analysis():
    import mileage_store

    with tempfile.TemporaryDirectory() as tmp:
        original_path = mileage_store.DB_PATH
        original_init = mileage_store._initialized
        try:
            mileage_store.DB_PATH = Path(tmp) / "dedupe.db"
            mileage_store._initialized = False
            panel = "/mnt/logs_new/018_LYNK01-001/overseas/Europe"
            mileage_store.save_analysis(
                f"{panel}/BG/20260924",
                {"mileage": {"effective_m": 1000.0, "gps_stable_m": 2000.0}},
            )
            mileage_store.save_analysis(
                f"{panel}/GR/20260924",
                {"mileage": {"effective_m": 5000.0, "gps_stable_m": 6000.0}},
            )
            mileage_store.save_analysis(
                f"/mnt/logs_new/020_E5-001/Russia/Overseas/9/20260924",
                {"mileage": {"effective_m": 3000.0, "gps_stable_m": 4000.0}},
            )
            removed = mileage_store.dedupe_day_analysis()
            check(removed == 1, "同地区同日期去重删除 1 条", str(removed))
            rows = mileage_store.list_history(limit=50)["items"]
            same_day = [row for row in rows if row["day"] == "20260924"]
            check(len(same_day) == 2, "不同地区同一天保留", str(len(same_day)))
            kept = next(
                (row for row in same_day if row["region"] == "欧洲地区"), None
            )
            check(
                kept is not None and round(float(kept["effective_m"])) == 5000,
                "去重保留有效里程高的那条",
                str(kept),
            )
        finally:
            mileage_store.DB_PATH = original_path
            mileage_store._initialized = original_init


def test_unc_path_normalization():
    import mileage_store

    with tempfile.TemporaryDirectory() as tmp:
        original_path = mileage_store.DB_PATH
        original_init = mileage_store._initialized
        try:
            mileage_store.DB_PATH = Path(tmp) / "unc.db"
            mileage_store._initialized = False
            unc_root = (
                r"\\192.168.20.206\logs_new\018_LYNK01-001\overseas\Europe\GR\20260927"
            )
            mnt_root = (
                "/mnt/logs_new/018_LYNK01-001/overseas/Europe/GR/20260927"
            )
            with mileage_store._connect() as conn:
                conn.execute(
                    "INSERT INTO day_analysis (data_root, day, computed_at, effective_m, "
                    "gps_stable_m, result_json) VALUES (?,?,?,?,?,?)",
                    (unc_root, "", "2026-10-28 10:00:00", 1000.0, 2000.0, "{}"),
                )
                conn.execute(
                    "INSERT INTO day_analysis (data_root, day, computed_at, effective_m, "
                    "gps_stable_m, result_json) VALUES (?,?,?,?,?,?)",
                    (mnt_root, "20260927", "2026-10-28 11:00:00", 500.0, 2000.0, "{}"),
                )
                mileage_store._migrate(conn)
            rows = mileage_store.list_history(limit=20)["items"]
            check(len(rows) == 1, "UNC 路径和 /mnt 路径合并成一条", str(len(rows)))
            check(
                rows and rows[0]["data_root"] == mnt_root,
                "历史记录统一成 /mnt 路径",
                str(rows[0] if rows else None),
            )
            check(
                rows and rows[0]["day"] == "20260927",
                "UTC 记录回填日期",
                str(rows[0]["day"] if rows else None),
            )
            check(
                rows and round(float(rows[0]["effective_m"])) == 1000,
                "合并时保留有效里程高的记录",
                str(rows[0]["effective_m"] if rows else None),
            )
        finally:
            mileage_store.DB_PATH = original_path
            mileage_store._initialized = original_init


def test_verify_day_states_latest_wins():
    import mileage_store

    with tempfile.TemporaryDirectory() as tmp:
        original_path = mileage_store.DB_PATH
        original_init = mileage_store._initialized
        try:
            mileage_store.DB_PATH = Path(tmp) / "verify_day.db"
            mileage_store._initialized = False
            root = "/mnt/Data_Platform/files/MaxieyeStructuredData/LYNK01-001_VSlam_InternalPro/LYNK01-001/138466_20261026"
            with mileage_store._connect() as conn:
                conn.execute(
                    "INSERT INTO verify_runs (data_root, updated_at, day, status, verdict, "
                    "vehicle, region, result_json) VALUES (?,?,?,?,?,?,?,?)",
                    (root + "-old", "2026-10-26 10:00:00", "20261026", "success",
                     "外参验证不通过：1/2 个相机有问题", "LYNK01-001", "欧洲地区", "{}"),
                )
                conn.execute(
                    "INSERT INTO verify_runs (data_root, updated_at, day, status, verdict, "
                    "vehicle, region, result_json) VALUES (?,?,?,?,?,?,?,?)",
                    (root + "-new", "2026-10-26 18:00:00", "20261026", "success",
                     "外参验证通过：2/2 个相机内参+外参齐全", "LYNK01-001", "欧洲地区", "{}"),
                )
            states = mileage_store.verify_day_states()
            state = states.get("欧洲地区|20261026") or {}
            check(
                state.get("state") == "passed" and state.get("available") is True,
                "同一天多次验证取最新结果（新的通过覆盖旧的未通过）",
                str(state),
            )
            check(state.get("total") == 2, "保留当天验证次数", str(state.get("total")))
        finally:
            mileage_store.DB_PATH = original_path
            mileage_store._initialized = original_init


def test_verify_day_manual_confirmation():
    import mileage_store

    with tempfile.TemporaryDirectory() as tmp:
        original_path = mileage_store.DB_PATH
        original_init = mileage_store._initialized
        try:
            mileage_store.DB_PATH = Path(tmp) / "verify_manual.db"
            mileage_store._initialized = False
            root = "/mnt/Data_Platform/files/MaxieyeStructuredData/EX5-001_VSLAM_jiaobenyanzheng/EX5-001/138737_20261024"
            with mileage_store._connect() as conn:
                conn.execute(
                    "INSERT INTO verify_runs (data_root, updated_at, day, status, verdict, "
                    "vehicle, region, result_json) VALUES (?,?,?,?,?,?,?,?)",
                    (root, "2026-10-24 10:00:00", "20261024", "existing",
                     "已有产出（自动扫描登记）", "EX5-001", "南美洲地区", "{}"),
                )
                for index in range(3):
                    conn.execute(
                        "INSERT INTO verify_confirmations (data_root, category, name, status, "
                        "updated_at) VALUES (?,?,?,?,?)",
                        (root, "quality", f"img{index}.jpg", "confirmed",
                         "2026-10-24 11:00:00"),
                    )
            state = mileage_store.verify_day_states().get("南美洲地区|20261024") or {}
            check(
                state.get("state") == "passed" and state.get("source") == "manual",
                "平台已有产出的数据集，人工确认全部通过后当天里程可用",
                str(state),
            )
            with mileage_store._connect() as conn:
                conn.execute(
                    "UPDATE verify_confirmations SET status='rejected', updated_at=? "
                    "WHERE data_root=? AND name='img0.jpg'",
                    ("2026-10-24 12:00:00", root),
                )
            state = mileage_store.verify_day_states().get("南美洲地区|20261024") or {}
            check(
                state.get("state") == "failed",
                "人工确认有未通过时当天里程不可用",
                str(state),
            )
        finally:
            mileage_store.DB_PATH = original_path
            mileage_store._initialized = original_init


def main():
    test_discover()
    test_decide()
    test_parse_time()
    test_load_roots()
    test_failure_cooldown()
    test_dedupe_day_analysis()
    test_unc_path_normalization()
    test_verify_day_states_latest_wins()
    test_verify_day_manual_confirmation()
    if failures:
        print(f"结果：{failures} 项失败")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
