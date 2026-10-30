#!/usr/bin/env python3
"""定期遍历固定父目录，自动分析新增/变化的日期目录里程（留好的自动化口子）。

规则：
1. 配置里的每个父目录下，递归（最多 4 层）找形如 `YYYYMMDD` 的日期目录；
   支持日志盘常见布局：`BR/2026/8/20260805`、`Europe/CZ/20260729`、`Overseas/10/20261001`；
2. 跳过「未验证 / 不可用 / 其他文件 / 标定文件 / 模板 / error」等目录；
3. 最近 `--min-age-min` 分钟内还在修改的目录跳过（数据可能还在写入）；
4. **完整性预检**：日期目录缺轨迹 / 2M / 8M 任一就不算，日志里记「数据不完整，跳过」；
5. 库里已有结果、且数据没再变化的跳过（**增量**，不重复算）；
6. 新目录 / 数据有更新的目录自动跑里程分析并写库（与页面「开始分析」同一套计算器）；
7. 单轮最多分析 `--limit` 天，避免一次跑太久；失败目录 6 小时内不重试。

配置：
    data/auto_mileage_roots.txt       # 一行一个父目录（# 注释），可加 `since=YYYYMMDD` 只看该日期及之后
                                       #   /mnt/logs_new/.../BR since=20261023
    data/auto_mileage_state.json      # 失败状态（自动生成）

用法：
    python tools/auto_mileage_scan.py                  # 正常一轮
    python tools/auto_mileage_scan.py --dry-run        # 只扫描/打印，不算不写库
    python tools/auto_mileage_scan.py --root /mnt/...  # 临时指定父目录（可重复）
    python tools/auto_mileage_scan.py --force          # 忽略缓存，把候选目录全部重算
    python tools/auto_mileage_scan.py --limit 1        # 本轮最多算几天

部署：systemd 每 24 小时遍历一轮，见 deploy/ap18-auto-mileage.service / .timer。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import ap18_visualizer as viz  # noqa: E402
import mileage_store  # noqa: E402

DAY_RE = re.compile(r"(20\d{6})")
SKIP_TOKENS = ("不可用", "无数据", "unavailable", "占位")
# 这些目录下的日期数据不参与自动刷里程（未验证/其他文件/模板/培训等）
SKIP_PATH_TOKENS = (
    "未验证", "不可用", "其他文件", "标定文件", "司机培训录制", "模板",
    "tsr_test", "error", "backup",
)
DEFAULT_CONFIG = REPO_ROOT / "data" / "auto_mileage_roots.txt"
DEFAULT_STATE = REPO_ROOT / "data" / "auto_mileage_state.json"
FAILURE_COOLDOWN_S = 6 * 3600
PERMANENT_COOLDOWN_S = 7 * 24 * 3600
PERMANENT_ERROR_TOKENS = ("既没有 AP18 pcap", "无法计算里程", "不是目录", "目录不存在")
STALE_TOLERANCE_S = 60.0


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


def load_roots(config_path: Path) -> list[dict[str, Any]]:
    """配置格式：一行一个父目录，可选 since=YYYYMMDD（只看该日期及之后的数据）。"""
    roots: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return roots
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        parts = text.split()
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


def _skip_path(path: Path) -> bool:
    lowered = {part.lower() for part in path.parts}
    return any(token.lower() in lowered for token in SKIP_PATH_TOKENS)


def discover_days(roots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """递归找日期目录（最多 4 层）；只做目录遍历，不做数据预检（预检只在真正要分析时做）。"""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in roots:
        root = entry["root"]
        since = str(entry.get("since") or "")
        if not root.is_dir():
            log(f"跳过不存在/不可读的目录：{root}")
            continue
        found: list[Path] = []
        queue: list[tuple[Path, int]] = [(root, 0)]
        while queue:
            current, depth = queue.pop(0)
            try:
                children = sorted(current.iterdir())
            except OSError:
                continue
            for child in children:
                try:
                    if not child.is_dir():
                        continue
                except OSError:
                    continue
                name = child.name
                if len(name) == 8 and name.isdigit():
                    found.append(child)
                elif depth < 4 and not name.startswith("."):
                    queue.append((child, depth + 1))
        ignored_old = sum(
            1 for path in found if since and path.name < since
        )
        dates = [path for path in found if not since or path.name >= since]
        log(
            f"{root} -> 发现 {len(dates)} 个日期目录"
            + (f"（since={since} 忽略 {ignored_old} 个更早的）" if since else "")
        )
        for path in dates:
            if _skip_path(path):
                continue
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            items.append({"path": key, "date": path.name})
    items.sort(key=lambda item: str(item.get("date") or ""), reverse=True)
    return items


def check_day_complete(path: Path) -> dict[str, Any]:
    """里程文件完整性预检：轨迹 / 2M / 8M。只在确定要分析时调用（CIFS 上比较慢）。"""
    from ap18_mileage import _discover_subdir_trajectories, _service

    try:
        info = _service().probe_mileage_dataset(path)
    except Exception as exc:  # noqa: BLE001
        return {"has_trajectory": False, "files_2m": 0, "files_8m": 0, "problems": [str(exc)]}

    trajectory_files = int(len(info.get("trajectory_files") or []))
    pcap_files = int(len(info.get("pcap_files") or []))
    device_2m = dict(info.get("device_2m") or {})
    device_8m = dict(info.get("device_8m") or {})
    files_2m = int(device_2m.get("files") or 0)
    files_8m = int(device_8m.get("files") or 0)
    has_trajectory = bool(trajectory_files or pcap_files)
    if not has_trajectory:
        try:
            found, _binary_notes, _notes = _discover_subdir_trajectories(path, info)
            has_trajectory = bool(found)
        except Exception:  # noqa: BLE001
            pass
    return {
        "has_trajectory": has_trajectory,
        "files_2m": files_2m,
        "files_8m": files_8m,
        "problems": list(info.get("problems") or []),
    }


def newest_mtime(path: Path, *, depth: int = 2, budget: int = 4000) -> float:
    """目录及两层子项的最近修改时间（CIFS 上只浅扫，避免整盘遍历）。"""
    newest = 0.0
    seen = 0
    stack: list[tuple[Path, int]] = [(path, 0)]
    while stack and seen < budget:
        current, level = stack.pop()
        try:
            newest = max(newest, current.stat().st_mtime)
        except OSError:
            continue
        if level >= depth:
            continue
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    seen += 1
                    if seen >= budget:
                        break
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append((Path(entry.path), level + 1))
                        else:
                            newest = max(newest, entry.stat().st_mtime)
                    except OSError:
                        continue
        except OSError:
            continue
    return newest


def parse_time(text: str) -> float:
    try:
        return time.mktime(time.strptime(str(text or ""), "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def decide(path: Path, cached: dict[str, Any] | None, *, now: float, min_age_min: float) -> tuple[str, str]:
    """返回 (动作, 原因)：动作 ∈ {analyze, skip, wait}。"""
    newest = newest_mtime(path)
    age_min = (now - newest) / 60.0 if newest else 9999.0
    if age_min < min_age_min:
        return "wait", f"数据还在写入（最近修改 {age_min:.0f} 分钟前 < {min_age_min:.0f} 分钟）"
    if cached is None:
        return "analyze", "库中还没有结果"
    computed = parse_time(cached.get("computed_at"))
    if newest > computed + STALE_TOLERANCE_S:
        return "analyze", f"数据有更新（{time.strftime('%Y-%m-%d %H:%M', time.localtime(newest))} 晚于上次计算）"
    return "skip", f"已是最新（计算于 {cached.get('computed_at')}）"


def failure_cooldown_s(error: str) -> float:
    """失败原因的冷却时间：数据缺失类（永久性）7 天后再试，其余 6 小时。"""
    text = str(error or "")
    if any(token in text for token in PERMANENT_ERROR_TOKENS):
        return PERMANENT_COOLDOWN_S
    return FAILURE_COOLDOWN_S


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


def run_one(path: Path) -> tuple[str, float, dict[str, Any]]:
    """分析一天，返回 (computed_at, 耗时秒, 概要)。"""
    from ap18_mileage import analyze_directory

    started = time.perf_counter()
    result = analyze_directory(path, decode_many=viz.decode_many_in_pool)
    elapsed = time.perf_counter() - started
    computed_at = mileage_store.save_analysis(str(path), result, elapsed_s=elapsed)
    mileage = result.get("mileage") or {}
    summary = {
        "effective_m": mileage.get("effective_m", mileage.get("both_m", 0)),
        "gps_stable_m": mileage.get("gps_stable_m", 0),
        "points": (result.get("trajectory") or {}).get("point_count", 0),
    }
    return computed_at, elapsed, summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="定期遍历固定目录，自动分析新增数据的有效里程")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="父目录配置文件（一行一个）")
    parser.add_argument("--root", action="append", default=[], help="临时指定父目录（可重复，优先于配置）")
    parser.add_argument("--state", default=str(DEFAULT_STATE), help="失败状态文件")
    parser.add_argument("--limit", type=int, default=3, help="本轮最多分析几天")
    parser.add_argument("--min-age-min", type=float, default=30.0, help="最近修改超过多少分钟才认为数据稳定")
    parser.add_argument("--force", action="store_true", help="忽略缓存，候选目录全部重算")
    parser.add_argument("--ignore-baseline", action="store_true", help="忽略基线，处理所有已发现的日期目录（补历史用）")
    parser.add_argument("--dry-run", action="store_true", help="只扫描和打印，不分析、不写库")
    args = parser.parse_args(argv)

    if args.root:
        roots = [{"root": Path(item), "since": ""} for item in args.root]
    else:
        roots = load_roots(Path(args.config))
    if not roots:
        log(f"没有配置扫描路径（--root 或 {args.config}；模板见 tools/auto_mileage_roots.example.txt）")
        return 2

    state_path = Path(args.state)
    state = load_state(state_path)
    days = discover_days(roots)
    if not days:
        log("没有发现日期目录")
        return 0

    # 首次运行：把当前已有的日期目录记为基线（历史数据不处理）；
    # 之后「新增的目录」才分析——不管它的日期是哪天（例如后来才拷进来的 9.28 也会分析）。
    now_text = time.strftime("%Y-%m-%d %H:%M:%S")
    baseline = state.get("baseline")
    if args.ignore_baseline:
        baseline_paths: set[str] = set()
    elif not isinstance(baseline, dict) or not baseline.get("paths"):
        baseline_paths = sorted(
            {str(item.get("path") or "") for item in days if item.get("path")}
        )
        if args.dry_run:
            log(f"[dry-run] 首次运行会把现有 {len(baseline_paths)} 个日期目录记为基线，不分析")
            return 0
        state["baseline"] = {"created_at": now_text, "paths": baseline_paths}
        save_state(state_path, state)
        log(
            f"首次运行：把现有 {len(baseline_paths)} 个日期目录记为基线（历史数据不处理），"
            "以后只分析新增目录（不设日期门槛）"
        )
        return 0
    else:
        baseline_paths = set(baseline.get("paths") or [])

    if not args.dry_run:
        # 复用可视化服务的解码进程池：大量 pcap 时并行解码（Windows/macOS 用 spawn，安全）
        try:
            viz.start_decoder_pool(None)
            import atexit

            atexit.register(viz.stop_decoder_pool)
            log(f"解码进程池已启动（{viz.DECODER_WORKERS} 个进程）")
        except Exception as exc:  # noqa: BLE001 - 池起不来就退回串行
            log(f"解码进程池启动失败，改为串行解码：{exc}")

    now = time.time()
    analyzed = skipped = waited = failed = incomplete = 0
    for item in days:
        if analyzed >= args.limit:
            log(f"本轮已达上限 {args.limit}，剩余下轮处理")
            break
        path = Path(str(item.get("path") or ""))
        key = str(path)
        failure = state.get(key) or {}
        if not args.force:
            failed_at = parse_time(failure.get("failed_at"))
            cooldown = failure_cooldown_s(failure.get("error"))
            if failure.get("failed_at") and now - failed_at < cooldown:
                hours = cooldown / 3600
                log(f"最近失败过（{hours:.0f} 小时内不重试），跳过：{path.name}（{failure.get('error') or '未知原因'}）")
                failed += 1
                continue
        cached = None
        try:
            cached = mileage_store.get_analysis(str(path), include_result=False)
        except Exception as exc:  # noqa: BLE001
            log(f"读库失败（按未算处理）：{path.name} -> {exc}")
        if key in baseline_paths and not cached:
            skipped += 1
            log(f"基线内的历史目录，跳过（只分析新增）：{path.name}")
            continue
        action, reason = decide(path, cached, now=now, min_age_min=args.min_age_min)
        if args.force and action != "wait":
            action, reason = "analyze", "强制重算"
        if action == "skip":
            skipped += 1
            log(f"跳过：{path.name} -> {reason}")
            continue
        if action == "wait":
            waited += 1
            log(f"等待：{path.name} -> {reason}")
            continue
        # 真正要分析时才做完整性预检（轨迹 + 2M + 8M 缺一样就跳过，等数据补齐）
        info = check_day_complete(path)
        missing: list[str] = []
        if not info.get("has_trajectory"):
            missing.append("轨迹")
        if not int(info.get("files_2m") or 0):
            missing.append("2M")
        if not int(info.get("files_8m") or 0):
            missing.append("8M")
        if missing:
            incomplete += 1
            problems = "；".join(str(p) for p in (info.get("problems") or []) if p)
            log(
                f"数据不完整，跳过：{path.name}（缺 {'、'.join(missing)}）"
                + (f" · {problems}" if problems else "")
            )
            continue
        if args.dry_run:
            log(f"[dry-run] 会分析：{path.name} -> {reason}")
            continue
        log(f"开始分析：{path.name}（{reason}）")
        try:
            computed_at, elapsed, summary = run_one(path)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            state[key] = {"failed_at": time.strftime("%Y-%m-%d %H:%M:%S"), "error": str(exc)}
            save_state(state_path, state)
            log(f"分析失败：{path.name} -> {exc}")
            continue
        analyzed += 1
        state.pop(key, None)
        save_state(state_path, state)
        log(
            f"分析完成：{path.name} · 有效里程 {summary['effective_m'] / 1000:.3f} km · "
            f"{summary['points']:,} 点 · {elapsed:.1f}s · 计算于 {computed_at}"
        )

    save_state(state_path, state)
    log(
        f"完成：日期目录 {len(days)} 个，新分析 {analyzed} 个，已最新跳过 {skipped} 个，"
        f"数据不完整跳过 {incomplete} 个，等待写入 {waited} 个，失败 {failed} 个"
    )
    if not args.dry_run:
        # 重复日期（同地区同一天，比如欧洲不同国家同一天）只保留有效里程最高的那条
        try:
            removed = mileage_store.dedupe_day_analysis()
            if removed:
                log(f"重复日期去重：删除 {removed} 条（同地区同日期保留有效里程最高的）")
        except Exception as exc:  # noqa: BLE001
            log(f"重复日期去重失败（忽略）：{exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
