#!/usr/bin/env python3
"""里程有效率低于阈值（默认 90%）时汇总推送飞书，说明为什么低；增量推送、不重复。

口径：
    有效率 = 有效里程（GPS 稳定 ∩ 2M 录制 ∩ 8M 录制）/ GPS 稳定里程 × 100%
    （day_analysis.effective_ratio_pct）

规则：
1. 从结果库里找出 effective_ratio_pct < --threshold 的里程天数；
2. 只推**还没推送过**的天（按 data_root+日期 去重，同一天只推一次，不重复）；
3. 卡片里逐天写：有效率、GPS/2M/8M 覆盖情况、低的原因（占位文件、录制覆盖不足、
   2M/8M 不重叠、无覆盖等，来自结果里的 coverage/issues）；
4. 有新增才发；没有新增只记日志。

用法：
    python tools/mileage_ratio_alert.py                # 正常一轮（有新增才发）
    python tools/mileage_ratio_alert.py --dry-run      # 只看内容，不发、不写状态
    python tools/mileage_ratio_alert.py --force        # 不管有没有新增都发一次汇总
    python tools/mileage_ratio_alert.py --threshold 80 # 改阈值

部署：deploy/ap18-mileage-alert.service / .timer（每 4 小时）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import ap18_visualizer as viz  # noqa: E402
import mileage_store  # noqa: E402

DEFAULT_STATE = REPO_ROOT / "data" / "mileage_alert_state.json"
DEFAULT_THRESHOLD = 90.0
DEFAULT_URL = "http://10.2.248.6:8901/mileage.html"


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


def alert_key(record: dict[str, Any]) -> str:
    root = os.path.normpath(str(record.get("data_root") or "")).replace("\\", "/")
    day = str(record.get("day") or "").strip()
    return f"{root}|{day}"


def _coverage_pct(part: float, whole: float) -> float:
    return (part / whole * 100.0) if whole > 0 else 0.0


def explain_low_ratio(record: dict[str, Any], *, threshold: float = DEFAULT_THRESHOLD) -> list[str]:
    """根据结果里的 mileage/coverage/issues 解释有效率为什么低（最多 4 条）。"""
    result = record.get("result") or {}
    mileage = result.get("mileage") or {}
    coverage = result.get("coverage") or {}
    issues = [str(item) for item in (result.get("issues") or [])]

    gps = float(mileage.get("gps_stable_m") or 0.0)
    covered_2m = float(mileage.get("covered_2m_m") or 0.0)
    covered_8m = float(mileage.get("covered_8m_m") or 0.0)
    effective = mileage.get("effective_m")
    if effective is None:
        effective = mileage.get("both_m")
    effective = float(effective or 0.0)
    no_cover = float(mileage.get("no_cover_m") or 0.0)
    target = float(threshold or DEFAULT_THRESHOLD)

    cov2 = coverage.get("2m") or {}
    cov8 = coverage.get("8m") or {}
    pct2 = _coverage_pct(covered_2m, gps)
    pct8 = _coverage_pct(covered_8m, gps)
    pct_both = _coverage_pct(effective, gps)

    reasons: list[str] = []
    if gps <= 0:
        reasons.append("GPS 稳定里程为 0：轨迹本身没有可用定位，无法算有效里程")
    else:
        placeholder2 = (not cov2.get("present")) and int(cov2.get("ignored_small_files") or 0) > 0
        placeholder8 = (not cov8.get("present")) and int(cov8.get("ignored_small_files") or 0) > 0
        if placeholder2:
            reasons.append("2M 目录里只有占位文件（<64KB），没有真实录制，2M 覆盖为 0")
        elif pct2 < target:
            reasons.append(f"2M 录制覆盖不足：只覆盖 GPS 稳定里程的 {pct2:.1f}%")
        if placeholder8:
            reasons.append("8M 目录里只有占位文件（<64KB），没有真实录制，8M 覆盖为 0")
        elif pct8 < target:
            reasons.append(f"8M 录制覆盖不足：只覆盖 GPS 稳定里程的 {pct8:.1f}%")
        if not placeholder2 and not placeholder8 and pct2 >= target and pct8 >= target and pct_both < target:
            reasons.append(f"2M 与 8M 录制时段几乎不重叠：同时录制只占 {pct_both:.1f}%")
        elif not reasons and no_cover > 0:
            reasons.append(f"有 {no_cover / 1000:.1f} km GPS 稳定里程在 2M/8M 都没录制")

    # 结果里已经写明的具体问题：只挑真正能解释「有效率低」的，过滤掉常规解析说明
    skip_tokens = ("识别为 NMEA", "已跳过", "二进制/非文本", "内容不是可识别的轨迹文本")
    keywords = ("占位文件", "跳点", "时间异常", "校验和", "覆盖为 0", "没有真实数据")
    for issue in issues:
        if len(reasons) >= 4:
            break
        if any(token in issue for token in skip_tokens):
            continue
        if not any(token in issue for token in keywords):
            continue
        if any(token in issue for token in ("占位文件",)) and any("占位文件" in item for item in reasons):
            continue
        if issue not in reasons:
            reasons.append(issue)

    if not reasons:
        reasons.append(
            f"2M/8M 录制与 GPS 稳定时段重合度低：同时录制只占 {pct_both:.1f}%"
        )
    return reasons[:4]


REASON_LABELS = (
    ("2M 目录里只有占位文件", "2M 占位文件"),
    ("8M 目录里只有占位文件", "8M 占位文件"),
    ("2M 录制覆盖不足", "2M 覆盖不足"),
    ("8M 录制覆盖不足", "8M 覆盖不足"),
    ("不重叠", "2M/8M 重合度低"),
    ("重合度低", "2M/8M 重合度低"),
    ("GPS 稳定里程为 0", "GPS 无稳定里程"),
    ("都没录制", "无覆盖里程"),
)


def reason_category(reason: str) -> str:
    """把具体原因归到一个简短类别，便于按项目汇总。"""
    text = str(reason or "")
    for token, label in REASON_LABELS:
        if token in text:
            return label
    return "其他"


def _record_region(record: dict[str, Any]) -> tuple[str, str]:
    region = str(record.get("region") or "").strip()
    vehicle = str(record.get("vehicle") or "").strip()
    if not region:
        name = str(record.get("data_root") or "").rstrip("/").replace("\\", "/").rsplit("/", 1)[-1]
        vehicle_guess, region_guess = viz._vehicle_meta(name)
        region = region_guess
        vehicle = vehicle or vehicle_guess
    return region or "未匹配项目/地区", vehicle


def group_by_region(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按项目/地区分组，返回有序、带车型集合的分组。"""
    groups: dict[str, dict[str, Any]] = {}
    for record in records:
        region, vehicle = _record_region(record)
        group = groups.setdefault(region, {"region": region, "vehicles": set(), "records": []})
        if vehicle:
            group["vehicles"].add(vehicle)
        group["records"].append(record)
    order = list(viz.REGION_OPTIONS) + sorted(set(groups) - set(viz.REGION_OPTIONS))
    return {region: groups[region] for region in order if region in groups}


def build_alert(
    records: list[dict[str, Any]],
    new_records: list[dict[str, Any]],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    url: str = DEFAULT_URL,
    generated_at: str = "",
) -> tuple[str, str, str]:
    """生成飞书卡片：只按项目汇总，不列具体哪几天（明细在平台看）。"""
    now = generated_at or time.strftime("%Y-%m-%d %H:%M:%S")
    total = len(records)
    newly = len(new_records)
    label = f"{threshold:g}%"
    title = (
        f"【里程有效率偏低】新增 {newly} 天（<{label}）"
        if newly
        else f"【里程有效率偏低】剩余 {total} 天（<{label}）"
    )

    all_by_region = group_by_region(records)
    new_by_region = group_by_region(new_records)
    # 有新增就展示新增涉及的项目；强制发送/无新增时展示全部偏低项目
    display_by_region = new_by_region if newly else all_by_region

    lines: list[str] = [f"**各项目有效率偏低（<{label}）**"]
    worst_overall = 100.0
    for region, info in display_by_region.items():
        vehicles = "、".join(sorted(info["vehicles"])) or "、".join(viz._region_vehicles(region)) or "—"
        new_count = len((new_by_region.get(region) or {}).get("records") or [])
        total_count = len((all_by_region.get(region) or {}).get("records") or [])
        ratios = [float(record.get("effective_ratio_pct") or 0.0) for record in info["records"]]
        avg_ratio = sum(ratios) / len(ratios) if ratios else 0.0
        worst = min(ratios) if ratios else 0.0
        worst_overall = min(worst_overall, worst)

        category_counts: dict[str, int] = {}
        for record in info["records"]:
            for reason in explain_low_ratio(record, threshold=threshold):
                category = reason_category(reason)
                category_counts[category] = category_counts.get(category, 0) + 1
        top_categories = sorted(category_counts.items(), key=lambda item: (-item[1], item[0]))[:3]

        lines.append(f"**{region}（{vehicles}）**")
        lines.append(
            f"新增 {new_count} 天 · 累计 {total_count} 天 · "
            f"平均有效率 {avg_ratio:.1f}% · 最低 {worst:.1f}%"
        )
        if top_categories:
            lines.append(
                "主要问题：" + "、".join(f"{name} {count} 天" for name, count in top_categories)
            )
        lines.append("")

    lines.append(f"**合计**：新增 {newly} 天 · 累计 {total} 天（<{label}）")
    mention_texts: list[str] = []
    for region in display_by_region:
        mention = viz.feishu_region_mention(region)
        if mention and mention not in mention_texts:
            mention_texts.append(mention)
    if mention_texts:
        lines.append(f"**相关人**：{' '.join(mention_texts)}")
    lines.append("口径：有效率 = 有效里程（GPS稳定 ∩ 2M ∩ 8M）/ GPS稳定里程")
    lines.append(f"**统计时间**：{now}")
    lines.append(f"里程分析页：{url}")
    body = "\n".join(lines)

    template = "red" if worst_overall < 50 else "orange"
    return title, body, template


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="里程有效率低于阈值的汇总推送（增量、不重复）")
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--limit", type=int, default=200, help="最多检查多少条低有效率记录")
    parser.add_argument("--url", default=os.environ.get("AP18_WEB_URL", DEFAULT_URL))
    parser.add_argument("--force", action="store_true", help="不管有没有新增都发一次汇总")
    parser.add_argument("--dry-run", action="store_true", help="只打印内容，不发飞书、不写状态")
    args = parser.parse_args(argv)

    try:
        records = mileage_store.low_ratio_days(args.threshold, limit=args.limit)
    except Exception as exc:  # noqa: BLE001
        log(f"读取低有效率数据失败：{exc}")
        return 1

    state_path = Path(args.state)
    state = load_state(state_path)
    notified = state.get("notified") or {}
    new_records = [record for record in records if alert_key(record) not in notified]
    log(f"有效率低于 {args.threshold:g}% 共 {len(records)} 天，其中新增 {len(new_records)} 天")

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    if not new_records and not args.force:
        save_state(state_path, {"notified": notified})
        log("没有新增低有效率数据，跳过发送飞书")
        return 0

    title, body, template = build_alert(
        records,
        new_records,
        threshold=args.threshold,
        url=args.url,
        generated_at=now,
    )
    if args.dry_run:
        log("[dry-run] 将发送飞书卡片：")
        print(f"标题：{title}\n模板：{template}\n{body}", flush=True)
        return 0

    ok, detail = viz._send_feishu_card(title, body, template)
    if not ok:
        log(f"发送飞书失败：{detail}")
        return 1
    for record in new_records:
        notified[alert_key(record)] = {
            "day": record.get("day"),
            "ratio": record.get("effective_ratio_pct"),
            "notified_at": now,
        }
    save_state(state_path, {"notified": notified, "last_sent_at": now})
    log(f"已发送飞书：新增 {len(new_records)} 天 / 共 {len(records)} 天有效率低于 {args.threshold:g}%（{detail}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
