#!/usr/bin/env python3
"""每 4 小时汇总各项目参数验证待办，有待办就推送到飞书群（10:00 / 15:00 / 19:00，7 点和 23 点不推）。

流程：
1. 读取历史库里的参数验证结果，统计每个地区/车型的待确认数量；
2. 与上次发送时的状态比对，标出**新增待办**（新数据集，或某数据集待确认张数变多）；
3. **只要有待办就发**飞书卡片；没有待办才跳过。加 `--only-new` 可恢复「只有新增才发」；
4. 推送时段 08:00~22:00（可用 `--active-start/--active-end` 改），07:00 和 23:00 及夜间不打扰；
5. 发送成功后更新状态文件；失败不更新，下一轮会重试。

用法：
    python tools/todo_notify.py                # 正常一轮（有待办就发）
    python tools/todo_notify.py --dry-run      # 只打印将要发送的内容，不发飞书
    python tools/todo_notify.py --only-new     # 只有新增待办才发
    python tools/todo_notify.py --force        # 没有待办也发一次汇总
    python tools/todo_notify.py --state data/todo_notify_state.json

部署：见 deploy/ap18-todo-notify.service / .timer（systemd 每天 10/15/19 点各触发一次）。
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

DEFAULT_URL = "http://10.2.248.6:8901/mileage.html"


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


def _norm_key(raw_root: str) -> str:
    return os.path.normpath(str(raw_root or "")).replace("\\", "/")


def collect_pending() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """返回（待确认数据集列表，按地区的统计）。"""
    try:
        runs = mileage_store.list_verify_runs_for_region("", limit=2000)
        totals = mileage_store.region_totals()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"读取历史库失败：{exc}") from exc

    pending: list[dict[str, Any]] = []
    by_region: dict[str, dict[str, Any]] = {}
    for item in runs:
        raw_root = item.get("data_root") or ""
        if not raw_root:
            continue
        name = raw_root.rstrip("/").replace("\\", "/").rsplit("/", 1)[-1]
        try:
            review = viz._verify_review_state(item)
        except Exception:  # noqa: BLE001
            review = None
        if not review:
            # 没有可确认的交付图（产出目录缺失/还没跑出图）：不列为待办
            continue
        state = review.get("state") or "unknown"
        region = item.get("region") or ""
        if not region:
            _vehicle, region = viz._vehicle_meta(raw_root)
        region = region or "未匹配项目/地区"
        bucket = by_region.setdefault(region, {"pending": 0, "done": 0, "vehicles": set()})
        if item.get("vehicle"):
            bucket["vehicles"].add(item["vehicle"])
        if state == "done":
            bucket["done"] += 1
            continue
        total = int((review or {}).get("total") or 0)
        passed = int((review or {}).get("passed") or 0)
        rejected = int((review or {}).get("rejected") or 0)
        pending.append(
            {
                "key": _norm_key(raw_root),
                "name": name,
                "data_root": raw_root,
                "region": region,
                "vehicle": item.get("vehicle") or "",
                "date": viz._dataset_date(name),
                "updated_at": item.get("updated_at") or "",
                "state": state,
                "total": total,
                "passed": passed,
                "rejected": rejected,
                "waiting": max(total - passed - rejected, 0),
            }
        )
        bucket["pending"] += 1

    day_info = (totals or {}).get("days") or {}
    ordered: dict[str, dict[str, Any]] = {}
    regions_in_order = list(viz.REGION_OPTIONS) + sorted(
        (set(by_region) | set(day_info)) - set(viz.REGION_OPTIONS)
    )
    for region in regions_in_order:
        bucket = by_region.get(region, {"pending": 0, "done": 0, "vehicles": set()})
        days = (day_info.get(region) or {}).get("days", 0)
        if not bucket["pending"] and not bucket["done"] and not days:
            continue
        ordered[region] = {
            "pending": bucket["pending"],
            "done": bucket["done"],
            "vehicles": sorted(bucket["vehicles"]),
            "mileage_days": days,
        }
    return pending, ordered


def find_new(pending: list[dict[str, Any]], state: dict[str, Any]) -> list[dict[str, Any]]:
    """新增待办：上次没见过的数据集，或待确认张数比上次多的数据集。"""
    seen = state.get("seen") or {}
    new_items: list[dict[str, Any]] = []
    for item in pending:
        previous = seen.get(item["key"])
        if not previous:
            new_items.append(item)
            continue
        try:
            previous_waiting = int(previous.get("waiting") or 0)
        except (TypeError, ValueError):
            previous_waiting = 0
        if item["waiting"] > previous_waiting:
            new_items.append(item)
    return new_items


def build_summary(
    pending: list[dict[str, Any]],
    new_items: list[dict[str, Any]],
    by_region: dict[str, dict[str, Any]],
    *,
    url: str = DEFAULT_URL,
    generated_at: str = "",
) -> tuple[str, str, str]:
    """生成飞书卡片（标题、正文、颜色）。

    只推各项目待办汇总，不列具体数据集明细（明细在平台上看）。
    """
    now = generated_at or time.strftime("%Y-%m-%d %H:%M:%S")
    total_pending = len(pending)
    newly = len(new_items)
    title = (
        f"【参数验证待办】新增 {newly} 个 · 共 {total_pending} 个待确认"
        if newly else f"【参数验证待办】剩余 {total_pending} 个待确认"
    )

    lines: list[str] = []
    lines.append("**各项目待办**")
    if by_region:
        for region, info in by_region.items():
            vehicles = "、".join(info.get("vehicles") or []) or "、".join(viz._region_vehicles(region)) or "—"
            pending_days = int(info.get("pending") or 0)
            done_days = int(info.get("done") or 0)
            mileage_total = int(info.get("mileage_days") or 0)
            mileage_new = int(info.get("mileage_new") or 0)
            mileage_existing = info.get("mileage_existing")
            if mileage_existing is None:
                mileage_existing = max(mileage_total - mileage_new, 0)
            mileage_existing = int(mileage_existing)
            lines.append(f"**{region}（{vehicles}）**")
            lines.append(
                f"参数验证：待确认 {pending_days} 天 · 已完成 {done_days} 天 · "
                f"累计 {pending_days + done_days} 天"
            )
            lines.append(
                f"有效里程：新增 {mileage_new} 天 · 已有 {mileage_existing} 天 · "
                f"累计 {mileage_total} 天"
            )
            lines.append("")
    else:
        lines.append("· 暂无参数验证记录")
    while lines and lines[-1] == "":
        lines.pop()
    lines.append("")

    total_pending_days = sum(int(info.get("pending") or 0) for info in by_region.values())
    total_done_days = sum(int(info.get("done") or 0) for info in by_region.values())
    total_mileage = sum(int(info.get("mileage_days") or 0) for info in by_region.values())
    total_mileage_new = sum(int(info.get("mileage_new") or 0) for info in by_region.values())
    lines.append(
        f"**参数验证合计**：{total_pending_days + total_done_days} 天"
        f"（待确认 {total_pending_days} · 已完成 {total_done_days}）"
    )
    lines.append(f"**有效里程合计**：累计 {total_mileage} 天（新增 {total_mileage_new} 天）")
    lines.append(f"**统计时间**：{now}")
    lines.append(f"请到平台查看并确认：{url}")
    body = "\n".join(lines)

    if new_items:
        template = "orange"
    elif total_pending:
        template = "blue"
    else:
        template = "grey"
    return title, body, template


def load_state(path: Path) -> dict[str, Any]:
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def build_state(
    pending: list[dict[str, Any]],
    *,
    seen_at: str,
    last_sent_at: str | None = None,
) -> dict[str, Any]:
    """记录当前待办快照；last_sent_at 只在真正发送成功时前移。"""
    seen: dict[str, dict[str, Any]] = {}
    for item in pending:
        seen[item["key"]] = {
            "name": item["name"],
            "region": item["region"],
            "waiting": item["waiting"],
            "seen_at": seen_at,
        }
    return {
        "seen": seen,
        "last_sent_at": seen_at if last_sent_at is None else last_sent_at,
        "last_pending": len(pending),
    }


def attach_mileage_progress(by_region: dict[str, dict[str, Any]], *, since: str) -> None:
    """给每个地区补上「里程新增/已有天数」（新增=上次发送后新算的里程天数）。"""
    try:
        new_days = mileage_store.region_mileage_days_since(since)
    except Exception as exc:  # noqa: BLE001
        log(f"读取新增里程天数失败（按 0 处理）：{exc}")
        new_days = {}
    for region, info in by_region.items():
        total_days = int(info.get("mileage_days") or 0)
        if since:
            new_count = min(int(new_days.get(region) or 0), total_days)
        else:
            # 没有上次发送记录：全部里程天数都算「新增」
            new_count = total_days
        info["mileage_new"] = new_count
        info["mileage_existing"] = max(total_days - new_count, 0)


def in_active_hours(hour: int, start: int, end: int) -> bool:
    """是否在允许推送的时段内（含起止小时，默认 7:00~23:00）。"""
    return start <= hour <= end


def should_send(
    pending: list[dict[str, Any]],
    new_items: list[dict[str, Any]],
    *,
    force: bool = False,
    only_new: bool = False,
) -> tuple[bool, str]:
    """是否要发飞书：默认「只要有待办就发」；only_new 时只有新增才发。"""
    if not pending and not force:
        return False, "没有待办，跳过发送飞书"
    if only_new and not new_items and not force:
        return False, "没有新增待办（只在有新增时推送），跳过发送飞书"
    return True, ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="汇总参数验证待办，有待办就发飞书群")
    parser.add_argument("--state", default=str(REPO_ROOT / "data" / "todo_notify_state.json"))
    parser.add_argument("--url", default=os.environ.get("AP18_WEB_URL", DEFAULT_URL))
    parser.add_argument("--force", action="store_true", help="没有待办也发一次汇总")
    parser.add_argument("--only-new", action="store_true", help="只有新增待办才发（默认有待办就发）")
    parser.add_argument("--active-start", type=int, default=8, help="允许推送的起始小时（默认 8）")
    parser.add_argument("--active-end", type=int, default=22, help="允许推送的结束小时（默认 22）")
    parser.add_argument("--dry-run", action="store_true", help="只打印内容，不发飞书、不写状态")
    args = parser.parse_args(argv)

    hour = time.localtime().tm_hour
    if not in_active_hours(hour, args.active_start, args.active_end):
        log(
            f"当前 {hour} 点不在推送时段（{args.active_start}:00-{args.active_end}:00），跳过"
        )
        return 0

    state_path = Path(args.state)
    try:
        pending, by_region = collect_pending()
    except Exception as exc:  # noqa: BLE001
        log(f"汇总失败：{exc}")
        return 1

    state = load_state(state_path)
    new_items = find_new(pending, state)
    log(f"待确认数据集 {len(pending)} 个，其中新增 {len(new_items)} 个")

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    since = str(state.get("last_sent_at") or "")
    attach_mileage_progress(by_region, since=since)

    send, reason = should_send(pending, new_items, force=args.force, only_new=args.only_new)
    if not send:
        # 不发飞书：只更新状态（去掉已经确认完的），last_sent_at 不前移
        save_state(
            state_path,
            build_state(pending, seen_at=now, last_sent_at=state.get("last_sent_at") or ""),
        )
        log(reason)
        return 0

    title, body, template = build_summary(
        pending,
        new_items,
        by_region,
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
    save_state(state_path, build_state(pending, seen_at=now))
    log(f"已发送飞书：新增 {len(new_items)} 个 / 共 {len(pending)} 个待确认（{detail}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
