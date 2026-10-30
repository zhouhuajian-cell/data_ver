#!/usr/bin/env python3
"""定时扫描父目录：确认数据完整后自动跑参数验证，结果写入历史库（供人工在平台确认）。

流程：
1. 列出父目录下所有数据集（含 parameter_yaml）；
2. 跳过历史库里已分析过的（或数据目录里已有产出的，登记为 existing）；
3. 校验数据完整性：标定 yaml、pose、相机帧数、雷达文件数，且最后修改时间已“稳定”；
4. 完整才跑参数验证（默认不做点云重投影，复用数据集里的 FrameMap，很快）；
5. 结果写入 verify_runs（平台「库中已有结果」直接可见，等待人工确认）。

用法：
    python tools/auto_verify_scan.py --root /mnt/.../E5-001
    python tools/auto_verify_scan.py --root ... --dry-run
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

import mileage_store  # noqa: E402
import verifier  # noqa: E402
from vehicle_regions import dataset_day, vehicle_meta  # noqa: E402

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp"}
LIDAR_EXTS = {".pcd", ".bin", ".las", ".laz"}
POSE_EXTS = {".pose"}
POSE_DIR_PREFIX = "rtkimu"  # E5 是 rtkimu_qj_7683，LYNK 等车型是 rtkimu_lvx_125 等，统一按前缀识别
WEB_URL = os.environ.get("AP18_WEB_URL", "http://10.2.248.6:8901/mileage.html")
BASELINE_PATH = REPO_ROOT / "data" / "auto_verify_baseline.json"


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


def _item_label(item: dict[str, Any]) -> str:
    """卡片标题里的简短标识：优先车型（如 E5-001），没车型用数据集名。"""
    return str(item.get("vehicle") or "").strip() or str(item.get("name") or "").strip()


def notify_new_datasets(items: list[dict[str, Any]]) -> bool:
    """新增数据分析完成立即推飞书：按项目汇总，提醒去页面确认融合图（不含自动结论）。"""
    if not items:
        return False
    try:
        import ap18_visualizer as viz  # 延迟导入：测试/--dry-run 不必加载大模块
    except Exception as exc:  # noqa: BLE001
        log(f"飞书推送跳过（导入失败）：{exc}")
        return False

    # 按项目分组（模板参考待办汇总：地区（车型）+ 参数验证待确认/已完成/累计）
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        region = str(item.get("region") or "").strip() or "未匹配项目/地区"
        grouped.setdefault(region, []).append(item)

    by_region: dict[str, dict[str, Any]] = {}
    try:
        import todo_notify

        _pending, by_region = todo_notify.collect_pending()
    except Exception as exc:  # noqa: BLE001 - 统计失败就只列新增数据集
        log(f"读取项目统计失败（卡片只列新增）：{exc}")
        by_region = {}

    regions: list[str] = []
    lines: list[str] = []
    for region, rows in grouped.items():
        regions.append(region)
        vehicles = sorted(
            {str(row.get("vehicle") or "").strip() for row in rows if row.get("vehicle")}
        )
        if not vehicles:
            vehicles = list(viz._region_vehicles(region)) if region != "未匹配项目/地区" else []
        head = f"**{region}" + (f"（{'、'.join(vehicles)}）" if vehicles else "") + "**"
        lines.append(head)
        names = []
        days: list[str] = []
        for row in rows:
            name = str(row.get("name") or "").strip()
            names.append(name)
            day = str(row.get("day") or "").strip()
            if day and day not in days:
                days.append(day)
        lines.append(f"新增 {len(rows)} 个：{'、'.join(names)}")
        if days:
            lines.append(f"数据采集时间：{'、'.join(days)}")
        stat = by_region.get(region) or {}
        pending_days = int(stat.get("pending") or 0)
        done_days = int(stat.get("done") or 0)
        lines.append(
            f"参数验证：待确认 {pending_days} 天 · 已完成 {done_days} 天 · "
            f"累计 {pending_days + done_days} 天"
        )
        lines.append("")

    mentions = [viz.feishu_region_mention(region) for region in regions]
    mentions = [text for text in mentions if text]
    if mentions:
        lines.append("**责任人**：" + " ".join(mentions))
    lines.append(f"请到平台确认融合图效果：{WEB_URL}")
    all_existing = all(str(item.get("status") or "") == "existing" for item in items)
    if all_existing:
        title = (
            f"参数验证：新增 {len(items)} 个数据集待确认（平台已有产出），请及时确认融合图效果"
            if len(items) > 1
            else f"参数验证：新增数据集待确认 · {_item_label(items[0])}（平台已有产出），请及时确认融合图效果"
        )
    else:
        title = (
            f"参数验证：新增 {len(items)} 个数据集已分析，请及时确认融合图效果"
            if len(items) > 1
            else f"参数验证：新增数据集已分析 · {_item_label(items[0])}，请及时确认融合图效果"
        )
    try:
        ok, detail = viz._send_feishu_card(title, "\n".join(lines), template="turquoise")
    except Exception as exc:  # noqa: BLE001
        log(f"飞书推送失败：{exc}")
        return False
    log(f"飞书推送：{'成功' if ok else '未发送'} {detail}")
    return bool(ok)


def notify_failures(items: list[dict[str, Any]]) -> bool:
    """参数验证分析失败也立即推飞书（红卡），提醒人工检查数据。"""
    if not items:
        return False
    try:
        import ap18_visualizer as viz
    except Exception as exc:  # noqa: BLE001
        log(f"飞书推送跳过（导入失败）：{exc}")
        return False
    regions: list[str] = []
    lines: list[str] = ["**以下数据集参数验证失败，请检查数据**", ""]
    for item in items:
        region = str(item.get("region") or "").strip()
        vehicle = str(item.get("vehicle") or "").strip()
        if region and region not in regions:
            regions.append(region)
        day = str(item.get("day") or "").strip()
        if len(day) == 8:
            day = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        who = " · ".join(part for part in (region, vehicle) if part) or "未匹配项目"
        lines.append(
            f"**{item.get('name')}**（{who}" + (f" · 数据采集日期 {day}" if day else "") + "）"
        )
        lines.append(f"失败原因：{item.get('error') or '未知错误'}")
        lines.append("")
    mentions = [viz.feishu_region_mention(region) for region in regions]
    mentions = [text for text in mentions if text]
    if mentions:
        lines.append("**责任人**：" + " ".join(mentions))
    lines.append(f"请到平台检查数据：{WEB_URL}")
    title = f"参数验证：{len(items)} 个数据集分析失败，请检查"
    try:
        ok, detail = viz._send_feishu_card(title, "\n".join(lines), template="red")
    except Exception as exc:  # noqa: BLE001
        log(f"飞书失败推送异常：{exc}")
        return False
    log(f"飞书失败推送：{'成功' if ok else '未发送'} {detail}")
    return bool(ok)


def _count_files(folder: Path, exts: set[str], limit: int) -> int:
    """数文件（够 limit 就停，CIFS 上避免全目录遍历）。"""
    count = 0
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    if entry.is_file() and Path(entry.name).suffix.lower() in exts:
                        count += 1
                        if count >= limit:
                            break
                except OSError:
                    continue
    except OSError:
        return 0
    return count


def _sensor_dirs(root: Path, prefix: str, *, skip_tokens: tuple[str, ...] = ()) -> list[Path]:
    found: list[Path] = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                name = entry.name
                if not name.startswith(prefix):
                    continue
                if any(token in name for token in skip_tokens):
                    continue
                found.append(Path(entry.path))
    except OSError:
        return []
    return sorted(found)


def _has_calibration(root: Path) -> bool:
    param_dir = root / "parameter_yaml"
    if not param_dir.is_dir():
        return False
    try:
        for current, dirnames, filenames in os.walk(param_dir):
            depth = len(Path(current).relative_to(param_dir).parts)
            for name in filenames:
                if name.lower().endswith((".yaml", ".yml")):
                    return True
            if depth >= 2:
                dirnames[:] = []
    except OSError:
        return False
    return False


def _pose_dirs(root: Path) -> list[Path]:
    """定位文件目录：rtkimu_qj_7683 / rtkimu_lvx_125 等，按 rtkimu 前缀识别。"""
    return _sensor_dirs(root, POSE_DIR_PREFIX)


def _pose_file_count(root: Path) -> int:
    total = 0
    for pose_dir in _pose_dirs(root):
        total += _count_files(pose_dir, POSE_EXTS, 1)
        if total >= 1:
            break
    return total


def _newest_mtime(paths: list[Path]) -> float:
    newest = 0.0
    for path in paths:
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def check_complete(
    dataset: Path,
    *,
    min_age_min: float,
    min_cam_frames: int,
    min_lidar_files: int,
    now: float | None = None,
) -> dict[str, Any]:
    """判断一个数据集是否可以开始分析。返回 ok + 原因 + 统计信息。"""
    now = time.time() if now is None else now
    problems: list[str] = []

    if not _has_calibration(dataset):
        problems.append("缺少 parameter_yaml 标定文件")
    pose_dirs = _pose_dirs(dataset)
    if _pose_file_count(dataset) <= 0:
        problems.append("缺少 rtkimu_* 目录下的 .pose 定位文件")

    cameras = _sensor_dirs(dataset, "cam_", skip_tokens=("rectified",))
    lidars = _sensor_dirs(dataset, "lidar_", skip_tokens=("rectified", "_cut"))
    cam_counts = {path.name: _count_files(path, IMAGE_EXTS, min_cam_frames) for path in cameras}
    lidar_counts = {path.name: _count_files(path, LIDAR_EXTS, min_lidar_files) for path in lidars}
    ready_cams = [name for name, count in cam_counts.items() if count >= min_cam_frames]
    ready_lidars = [name for name, count in lidar_counts.items() if count >= min_lidar_files]
    if not ready_cams:
        problems.append(f"没有相机目录达到 {min_cam_frames} 帧（实际：{cam_counts or '无'}）")
    if not ready_lidars:
        problems.append(f"没有雷达目录达到 {min_lidar_files} 个文件（实际：{lidar_counts or '无'}）")

    watched = [dataset, dataset / "parameter_yaml", *pose_dirs[:1], *ready_cams[:2]]
    watched += ready_lidars[:1]
    newest = _newest_mtime([Path(path) for path in watched])
    age_min = (now - newest) / 60.0 if newest else 9999.0
    if age_min < min_age_min:
        problems.append(f"数据还在写入（最近修改 {age_min:.0f} 分钟前 < {min_age_min:.0f} 分钟）")

    return {
        "ok": not problems,
        "problems": problems,
        "cameras": cam_counts,
        "lidars": lidar_counts,
        "age_min": round(age_min, 1),
    }


def load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:  # noqa: BLE001
        log(f"写状态文件失败（忽略）：{exc}")


def load_baseline(path: Path) -> set[str] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    paths = data.get("paths") if isinstance(data, dict) else None
    if not isinstance(paths, list):
        return None
    return {str(item) for item in paths}


def build_baseline(roots: list[dict[str, Any]]) -> list[str]:
    paths: list[str] = []
    for entry in roots:
        root = entry["root"]
        if not root.is_dir():
            continue
        try:
            listing = verifier.scan_verify_datasets(root, max_depth=1, max_items=5000)
        except Exception as exc:  # noqa: BLE001
            log(f"基线扫描失败 {root}：{exc}")
            continue
        for item in listing.get("datasets", []):
            if item.get("has_parameter_yaml"):
                paths.append(str(Path(item["path"])))
    return sorted(set(paths))


def save_baseline(path: Path, paths: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"created_at": time.strftime("%Y-%m-%d %H:%M:%S"), "paths": paths},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def register_existing(dataset: Path) -> None:
    """数据目录里已有产出但库里没有：登记为已有产出，避免重复跑。"""
    output_dir = dataset / "framemap_dataquality_projection"
    task = {
        "status": "existing",
        "verdict": "已有产出（自动扫描登记）",
        "duration_s": None,
        "output_root": str(output_dir),
    }
    mileage_store.save_verify_run(str(dataset), task, str(output_dir))


def run_one(dataset: Path, output_root: Path, *, timeout_min: float) -> dict[str, Any]:
    task_id = f"auto-{int(time.time())}-{dataset.name}"
    options = verifier.VerifyOptions(
        run_data_quality=True,
        run_projection=True,
        enable_pointcloud_reprojection=True,  # 必须开：False 时管线只做 sensor status，不产出融合图
        force_regenerate_framemap=False,
        frame_step=20,
    )
    started = time.time()
    result = verifier.run_verify(task_id, dataset, output_root, options)
    task = verifier.result_to_dict(result)
    mileage_store.save_verify_run(str(dataset), task, str(output_root))
    elapsed = time.time() - started
    if elapsed > timeout_min * 60:
        log(f"警告：{dataset.name} 耗时 {elapsed / 60:.1f} 分钟，超过预期 {timeout_min:.0f} 分钟")
    return task


def load_roots(config_path: Path) -> list[dict[str, Any]]:
    """从配置文件读取扫描路径；一行一个，# 开头为注释。

    可选 `since=YYYYMMDD`：只分析该日期及之后的数据集（更早的忽略），例如：
        /mnt/.../LYNK01-001 since=20261021
    """
    roots: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return roots
    for line in lines:
        text_value = line.strip()
        if not text_value or text_value.startswith("#"):
            continue
        parts = text_value.split()
        raw_path = parts[0]
        since = ""
        for extra in parts[1:]:
            if extra.startswith("since="):
                candidate = extra.split("=", 1)[1].strip()
                if len(candidate) == 8 and candidate.isdigit():
                    since = candidate
        path = Path(raw_path).expanduser()
        key = f"{path}|{since}"
        if key in seen:
            continue
        seen.add(key)
        roots.append({"root": path, "since": since})
    return roots


def filter_candidates(candidates: list[Path], since: str) -> tuple[list[Path], int]:
    """按 since（YYYYMMDD）过滤数据集；返回（保留的候选，忽略的旧数据集数量）。"""
    text = str(since or "").strip()
    if not text:
        return candidates, 0
    kept: list[Path] = []
    skipped = 0
    for dataset in candidates:
        day = dataset_day(dataset.name)
        if day and day < text:
            skipped += 1
            continue
        kept.append(dataset)
    return kept, skipped


def scan_root(
    root: Path,
    args,
    state: dict,
    state_path: Path,
    budget: int,
    *,
    since: str = "",
    baseline_paths: set[str] | None = None,
) -> tuple[int, int, list[dict[str, Any]], list[dict[str, Any]]]:
    """扫描一个父目录；返回（检查数量，新开始分析数量，新增已完成的分析列表，失败列表）。"""
    checked = 0
    started_runs = 0
    new_items: list[dict[str, Any]] = []
    failed_items: list[dict[str, Any]] = []
    try:
        listing = verifier.scan_verify_datasets(root, max_depth=1, max_items=2000)
    except Exception as exc:  # noqa: BLE001
        log(f"扫描失败 {root}：{exc}")
        return 0, 0, [], []
    candidates = [Path(item["path"]) for item in listing.get("datasets", []) if item.get("has_parameter_yaml")]
    candidates, skipped_old = filter_candidates(candidates, since)
    log(
        f"{root} -> 发现 {len(candidates)} 个含 parameter_yaml 的数据集"
        + (f"（since={since} 忽略 {skipped_old} 个更早的数据集）" if since else "")
    )

    for dataset in candidates:
        key = str(dataset)
        entry = state.get(key) or {}
        existing = mileage_store.get_verify_run(key)
        if existing and str(existing.get("status") or "") in {"success", "existing"}:
            continue
        if entry.get("action") == "failed":
            try:
                failed_at = time.mktime(time.strptime(str(entry.get("failed_at")), "%Y-%m-%d %H:%M:%S"))
            except (ValueError, TypeError):
                failed_at = 0
            if time.time() - failed_at < 6 * 3600:
                continue
        if baseline_paths and key in baseline_paths:
            # 基线内的历史数据集：只标记跳过，不登记入库、不分析、不推送
            if entry.get("action") != "baseline":
                state[key] = {
                    "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "action": "baseline",
                }
                log(f"基线数据集，跳过（不入库、不推送）：{dataset.name}")
            continue
        if (dataset / "framemap_dataquality_projection").is_dir():
            if args.dry_run:
                log(f"[dry-run] 已有产出，无需分析：{dataset.name}")
                continue
            register_existing(dataset)
            state[key] = {"checked_at": time.strftime("%Y-%m-%d %H:%M:%S"), "action": "registered-existing"}
            log(f"已有产出，登记跳过：{dataset.name}")
            # 平台已产出融合图的新数据集也要推一次（否则用户感知不到新增）
            vehicle, region = vehicle_meta(str(dataset))
            new_items.append(
                {
                    "name": dataset.name,
                    "path": str(dataset),
                    "vehicle": vehicle,
                    "region": region,
                    "day": dataset_day(dataset.name),
                    "status": "existing",
                    "analyzed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            continue
        if started_runs >= budget:
            log(f"达到本轮上限 {args.limit}，剩余下轮处理")
            break

        info = check_complete(
            dataset,
            min_age_min=args.min_age_min,
            min_cam_frames=args.min_cam_frames,
            min_lidar_files=args.min_lidar_files,
        )
        checked += 1
        state[key] = {
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "complete": info["ok"],
            "problems": info["problems"],
            "cameras": info["cameras"],
            "lidars": info["lidars"],
            "age_min": info["age_min"],
        }
        if not info["ok"]:
            log(f"未完成/数据不完整，跳过：{dataset.name} -> {'；'.join(info['problems'])}")
            continue
        if args.dry_run:
            log(f"[dry-run] 数据完整，可分析：{dataset.name}（{info['age_min']} 分钟未修改）")
            continue

        output_root = REPO_ROOT / "data" / "verify" / f"auto_{dataset.name}"
        log(f"开始分析：{dataset.name}")
        started_runs += 1
        try:
            task = run_one(dataset, output_root, timeout_min=15)
            state[key] = {
                **state[key],
                "action": "analyzed",
                "status": task.get("status"),
                "verdict": task.get("verdict"),
                "duration_s": task.get("duration_s"),
                "analyzed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            log(
                f"分析完成：{dataset.name} 状态={task.get('status')} 耗时={task.get('duration_s')}s "
                f"结论={task.get('verdict')}"
            )
            if str(task.get("status") or "") == "success":
                vehicle, region = vehicle_meta(str(dataset))
                new_items.append(
                    {
                        "name": dataset.name,
                        "path": str(dataset),
                        "vehicle": vehicle,
                        "region": region,
                        "day": dataset_day(dataset.name),
                        "status": task.get("status"),
                        "analyzed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            state[key] = {**state[key], "action": "failed", "error": str(exc), "failed_at": time.strftime("%Y-%m-%d %H:%M:%S")}
            log(f"分析失败：{dataset.name} -> {exc}")
            vehicle, region = vehicle_meta(str(dataset))
            failed_items.append(
                {
                    "name": dataset.name,
                    "path": str(dataset),
                    "vehicle": vehicle,
                    "region": region,
                    "day": dataset_day(dataset.name),
                    "error": str(exc),
                }
            )
        finally:
            save_state(state_path, state)
    return checked, started_runs, new_items, failed_items


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="定时扫描多个父目录并自动做参数验证")
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        help="父目录（可重复指定）；不指定则读 --config 配置文件",
    )
    parser.add_argument(
        "--config",
        default=str(REPO_ROOT / "data" / "auto_verify_roots.txt"),
        help="扫描路径配置文件，一行一个路径，# 开头为注释",
    )
    parser.add_argument("--state", default=str(REPO_ROOT / "data" / "auto_verify_state.json"))
    parser.add_argument("--min-age-min", type=float, default=30.0, help="最近修改超过多少分钟才认为数据稳定")
    parser.add_argument("--min-cam-frames", type=int, default=10, help="每个相机至少多少帧")
    parser.add_argument("--min-lidar-files", type=int, default=3, help="每个雷达至少多少文件")
    parser.add_argument("--limit", type=int, default=4, help="单次最多分析几个新数据集（所有路径合计）")
    parser.add_argument("--dry-run", action="store_true", help="只检查不分析")
    parser.add_argument("--no-notify", action="store_true", help="新增数据分析完不推飞书")
    parser.add_argument(
        "--baseline",
        default=str(BASELINE_PATH),
        help="基线文件：首次运行把现有数据集记为基线，之后只分析新增数据集",
    )
    args = parser.parse_args(argv)

    if args.root:
        roots = [{"root": Path(item), "since": ""} for item in args.root]
    else:
        roots = load_roots(Path(args.config))
    if not roots:
        log(f"没有配置扫描路径（--root 或 {args.config}）")
        return 2

    baseline_file = Path(args.baseline)
    baseline_paths = load_baseline(baseline_file)
    if baseline_paths is None:
        # 首次运行：把当前已有的数据集全部记为基线（历史不分析、不推送）；
        # 之后只要目录是新出现的，不管日期是哪天都会分析（例如后来才拷进来的 9.28）。
        existing_paths = build_baseline(roots)
        if args.dry_run:
            log(f"[dry-run] 首次运行会把现有 {len(existing_paths)} 个数据集记为基线，不分析")
            return 0
        save_baseline(baseline_file, existing_paths)
        log(
            f"首次运行：把现有 {len(existing_paths)} 个数据集记为基线（历史数据不处理），"
            "以后只分析新增数据集（不设日期门槛）"
        )
        return 0

    state_path = Path(args.state)
    state = load_state(state_path)
    log(f"本轮扫描 {len(roots)} 个路径（min_age={args.min_age_min:g}min, 单次上限={args.limit}）")

    total_checked = 0
    total_started = 0
    new_items: list[dict[str, Any]] = []
    failed_items: list[dict[str, Any]] = []
    for item in roots:
        root = item["root"]
        since = str(item.get("since") or "")
        if not root.is_dir():
            log(f"跳过不存在/不可读的目录：{root}")
            continue
        remaining = args.limit - total_started
        if remaining <= 0:
            log("本轮已达分析上限，剩余路径下轮处理")
            break
        checked, started, items, failed = scan_root(
            root, args, state, state_path, remaining, since=since, baseline_paths=baseline_paths
        )
        total_checked += checked
        total_started += started
        new_items.extend(items)
        failed_items.extend(failed)

    save_state(state_path, state)
    log(f"完成：共 {len(roots)} 个路径，检查 {total_checked} 个，新开始分析 {total_started} 个")
    if args.no_notify:
        return 0
    # 流程要求：参数验证有任何更新都立刻推飞书（新增分析完成 / 失败都要推，不等待办汇总）
    if new_items:
        notify_new_datasets(new_items)
    if failed_items:
        notify_failures(failed_items)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
