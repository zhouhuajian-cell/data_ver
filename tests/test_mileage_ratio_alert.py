"""里程有效率预警（tools/mileage_ratio_alert.py）纯函数自检：不依赖数据库/网络。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location(
    "mileage_ratio_alert", ROOT / "tools" / "mileage_ratio_alert.py"
)
alert = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(alert)

failures = 0


def check(condition, name, detail=""):
    global failures
    if condition:
        print(f"  [ OK ] {name}")
    else:
        failures += 1
        print(f"  [FAIL] {name} - {detail}")


def make_record(day="20260924", ratio=3.1, *, gps=1000.0, c2=1000.0, c8=1000.0,
                effective=31.0, no_cover=0.0, cov2=None, cov8=None, issues=None,
                root="/mnt/logs_new/020_E5-001/Russia/Overseas/9/20260924"):
    return {
        "data_root": root,
        "day": day,
        "region": "独联体地区",
        "vehicle": "E5-001",
        "effective_ratio_pct": ratio,
        "result": {
            "mileage": {
                "gps_stable_m": gps,
                "covered_2m_m": c2,
                "covered_8m_m": c8,
                "effective_m": effective,
                "no_cover_m": no_cover,
            },
            "coverage": {
                "2m": cov2 if cov2 is not None else {"present": True, "ignored_small_files": 0},
                "8m": cov8 if cov8 is not None else {"present": True, "ignored_small_files": 0},
            },
            "issues": issues or [],
        },
    }


def test_explain():
    placeholder = make_record(
        c2=24469.0, c8=0.0, effective=0.0, gps=73689.0, no_cover=49220.0,
        cov2={"present": True, "ignored_small_files": 170},
        cov8={"present": False, "ignored_small_files": 4424},
        issues=["8M 目录里只有占位文件、没有真实数据，8M 覆盖与有效里程按 0 计"],
    )
    reasons = alert.explain_low_ratio(placeholder)
    check(any("8M 目录里只有占位文件" in item for item in reasons), "识别 8M 只有占位文件", str(reasons))
    check(any("2M 录制覆盖不足" in item for item in reasons), "识别 2M 覆盖不足", str(reasons))

    non_overlap = make_record(gps=1000.0, c2=1000.0, c8=1000.0, effective=10.0)
    reasons = alert.explain_low_ratio(non_overlap)
    check(any("几乎不重叠" in item for item in reasons), "识别 2M/8M 不重叠", str(reasons))

    low_gps = make_record(gps=0.0, c2=0.0, c8=0.0, effective=0.0)
    reasons = alert.explain_low_ratio(low_gps)
    check(any("GPS 稳定里程为 0" in item for item in reasons), "GPS 稳定为 0 的原因", str(reasons))

    slight = make_record(
        gps=178500.0, c2=146000.0, c8=178500.0, effective=146000.0, ratio=81.8,
        issues=["qinji/20261005.txt: 内容不是可识别的轨迹文本（二进制或格式不支持），已跳过"],
    )
    reasons = alert.explain_low_ratio(slight)
    check(any("2M 录制覆盖不足" in item for item in reasons), "覆盖 81.8% 也说明偏低", str(reasons))
    check(
        not any("已跳过" in item or "不可识别的轨迹" in item for item in reasons),
        "常规解析说明不作为低原因",
        str(reasons),
    )

    fallback = make_record(gps=1000.0, c2=1000.0, c8=1000.0, effective=999.0, ratio=99.9)
    reasons = alert.explain_low_ratio(fallback)
    check(len(reasons) >= 1, "兜底至少给一条原因", str(reasons))


def test_build_alert():
    records = [
        make_record("20260924", 3.1),
        make_record("20260923", 0.0, root="/mnt/logs_new/020_E5-001/Russia/Overseas/9/20260923"),
        make_record("20260921", 3.9, root="/mnt/logs_new/020_E5-001/Russia/Overseas/9/20260921"),
    ]
    new_records = records[:2]
    title, body, template = alert.build_alert(
        records, new_records, threshold=90, url="http://example:8901/mileage.html",
        generated_at="2026-10-23 12:00:00",
    )
    check("新增 2 天" in title and "<90%" in title, "标题含新增天数和阈值", title)
    check(template == "red", "有效率很低时红色卡片", template)
    check("各项目有效率偏低" in body, "正文按项目汇总", body[:400])
    check("独联体地区（E5-001）" in body, "正文有项目/车型", body[:400])
    check("新增 2 天 · 累计 3 天" in body, "项目行有新增/累计天数", body[:500])
    check("平均有效率" in body and "最低" in body, "项目行有平均/最低有效率", body[:500])
    check("主要问题：" in body, "项目行汇总主要问题", body[:600])
    check("2026-09-24" not in body, "不推具体日期明细", body[:600])
    check("原因：2M" not in body, "不推逐天明细原因", body[:600])
    check("合计" in body and "口径" in body, "正文有合计和口径说明", body[:700])
    check("http://example:8901/mileage.html" in body, "正文带里程页面链接")
    check("2026-10-23 12:00:00" in body, "正文带统计时间")

    title2, body2, template2 = alert.build_alert(
        records, [], threshold=90, generated_at="2026-10-23 12:00:00"
    )
    check("剩余 3 天" in title2, "没有新增时标题用剩余天数", title2)
    check(template2 == "red", "存在很低有效率时保持红色卡片", template2)
    check("新增 0 天" in body2, "无新增也显示 0")
    check(
        "独联体地区（E5-001）" in body2 and "新增 0 天 · 累计 3 天" in body2,
        "无新增时也按项目汇总",
        body2[:500],
    )

    multi = [
        make_record("20260924", 3.1),
        make_record("20260923", 0.0, root="/mnt/logs_new/020_E5-001/Russia/Overseas/9/20260923"),
        {
            **make_record("20261020", 69.4, root="/mnt/logs_new/018_LYNK01-001/Overseas_verify/20261020"),
            "region": "欧洲地区",
            "vehicle": "LYNK01-001",
        },
    ]
    _, body3, _ = alert.build_alert(multi, multi, threshold=90, generated_at="2026-10-23 12:00:00")
    check("独联体地区（E5-001）" in body3 and "欧洲地区（LYNK01-001）" in body3, "按项目分组汇总", body3[:600])
    check("新增 2 天 · 累计 2 天" in body3 and "新增 1 天 · 累计 1 天" in body3, "各项目天数正确", body3[:600])


def test_reason_category():
    check(alert.reason_category("2M 目录里只有占位文件（<64KB）") == "2M 占位文件", "原因归类：2M 占位")
    check(alert.reason_category("2M 录制覆盖不足：只覆盖 3.1%") == "2M 覆盖不足", "原因归类：2M 覆盖不足")
    check(alert.reason_category("2M 与 8M 录制时段几乎不重叠") == "2M/8M 重合度低", "原因归类：重合度低")
    check(alert.reason_category("其他奇怪原因") == "其他", "原因归类：兜底其他")


def test_mention():
    original = alert.viz.feishu_region_mention
    try:
        alert.viz.feishu_region_mention = (
            lambda region, **kwargs: "<at id=ou_test></at>" if region == "独联体地区" else ""
        )
        record = make_record()
        _, body, _ = alert.build_alert(
            [record], [record], threshold=90, generated_at="2026-10-23 12:00:00"
        )
        check("**相关人**：<at id=ou_test></at>" in body, "有效率预警按项目 @ 人", body[:500])
    finally:
        alert.viz.feishu_region_mention = original


def test_alert_key():
    record = make_record(root="\\\\192.168.20.206\\logs_new\\20260924")
    key = alert.alert_key(record)
    check(key.endswith("|20260924"), "key 以日期结尾", key)
    check("\\" not in key.split("|")[0], "key 路径统一成正斜杠", key)


def main():
    test_explain()
    test_build_alert()
    test_reason_category()
    test_mention()
    test_alert_key()
    if failures:
        print(f"结果：{failures} 项失败")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
