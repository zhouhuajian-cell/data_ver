#!/usr/bin/env python3
"""批量刷新已有参数验证结果的融合图（点云反投影改用完整点云）。

背景：旧版本平台反投影优先用 lidar_*_cut 裁剪点云，和人工脚本（完整点云）结果不一致；
本脚本把已有「有投影图」的验证结果重新生成一遍，只覆盖图片，不改数据库、不推飞书、
不触发前端消息（后台静默刷新）。

用法：
  python tools/refresh_projection_images.py --dry-run
  python tools/refresh_projection_images.py --limit 5
  nohup python tools/refresh_projection_images.py > logs/refresh_projection.log 2>&1 &
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mileage_store  # noqa: E402
import verifier  # noqa: E402

MARKER = ".projection_full_lidar_v2"


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)


def projection_images(output_dir: Path) -> list[Path]:
    return sorted(output_dir.glob("projection/*/*.jpg"))


def refresh_one(data_root_text: str, output_dir_text: str) -> tuple[bool, str, float, int]:
    """刷新单个验证结果的投影图（供串行/多进程共用）。"""
    data_root = Path(data_root_text)
    output_dir = Path(output_dir_text)
    started = time.time()
    options = verifier.VerifyOptions(
        run_data_quality=False,
        run_projection=True,
        enable_pointcloud_reprojection=True,
        force_regenerate_framemap=False,
        frame_start=100,
        frame_end=None,
        frame_step=20,
    )
    try:
        result = verifier.run_verify(
            f"refresh_projection_{output_dir.name}", data_root, output_dir, options
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}", time.time() - started, 0
    elapsed = time.time() - started
    if result.status != "success":
        return False, result.error or result.status, elapsed, 0
    try:
        (output_dir / MARKER).write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    except OSError:
        pass
    return True, "", elapsed, len(projection_images(output_dir))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批量刷新验证结果里的融合图（完整点云）")
    parser.add_argument("--limit", type=int, default=0, help="最多刷新几个结果（0=全部）")
    parser.add_argument("--workers", type=int, default=1, help="并发进程数（完整点云较慢，可开 3-4 个）")
    parser.add_argument("--dry-run", action="store_true", help="只列出要刷新的结果")
    args = parser.parse_args(argv)

    with mileage_store._connect() as conn:
        rows = conn.execute(
            "SELECT data_root, output_dir, updated_at FROM verify_runs "
            "WHERE status = 'success' AND output_dir IS NOT NULL AND output_dir != '' "
            "ORDER BY updated_at"
        ).fetchall()

    candidates: list[tuple[Path, Path, int]] = []
    for row in rows:
        data_root = Path(str(row["data_root"] or ""))
        output_dir = Path(str(row["output_dir"] or ""))
        if not data_root.is_dir() or not output_dir.is_dir():
            continue
        images = projection_images(output_dir)
        if not images:
            continue
        if (output_dir / MARKER).is_file():
            continue
        candidates.append((data_root, output_dir, len(images)))

    log(
        f"共 {len(rows)} 条 success 验证结果，其中 {len(candidates)} 条已有投影图待刷新"
    )
    if args.limit and args.limit > 0:
        candidates = candidates[: args.limit]
        log(f"本次限制刷新 {len(candidates)} 条")

    if args.dry_run:
        for data_root, output_dir, count in candidates:
            log(f"[dry-run] {output_dir} · {count} 张 · {data_root.name}")
        return 0

    succeeded = failed = 0
    if args.workers > 1 and len(candidates) > 1:
        from concurrent.futures import ProcessPoolExecutor, as_completed

        log(f"并发刷新：{args.workers} 个进程，共 {len(candidates)} 个结果")
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(refresh_one, str(data_root), str(output_dir)): (data_root, output_dir, count)
                for data_root, output_dir, count in candidates
            }
            finished = 0
            for future in as_completed(futures):
                data_root, output_dir, count = futures[future]
                finished += 1
                try:
                    ok, error, elapsed, new_count = future.result()
                except Exception as exc:  # noqa: BLE001
                    ok, error, elapsed, new_count = False, f"{type(exc).__name__}: {exc}", 0.0, 0
                if ok:
                    succeeded += 1
                    log(
                        f"[{finished}/{len(candidates)}] 完成 {data_root.name}："
                        f"{new_count} 张，用时 {elapsed:.1f}s"
                    )
                else:
                    failed += 1
                    log(f"[{finished}/{len(candidates)}] 失败 {data_root.name}：{error}")
        log(f"全部结束：成功 {succeeded} 条，失败 {failed} 条")
        return 0 if failed == 0 else 1

    for index, (data_root, output_dir, count) in enumerate(candidates, 1):
        log(f"[{index}/{len(candidates)}] 刷新 {data_root.name} · 现有 {count} 张投影图")
        ok, error, elapsed, new_count = refresh_one(str(data_root), str(output_dir))
        if ok:
            succeeded += 1
            log(f"  完成：{new_count} 张投影图，用时 {elapsed:.1f}s")
        else:
            failed += 1
            log(f"  失败：{error}")

    log(f"全部结束：成功 {succeeded} 条，失败 {failed} 条")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
