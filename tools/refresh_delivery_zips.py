#!/usr/bin/env python3
"""把刷新过融合图的验证结果重新打进「交付包」zip。

背景：融合图刷新只覆盖了 output_dir 里的图片，数据集目录下的
<dataset>/delivery_package/<dataset>_参数验证交付.zip 仍是旧图快照。
本脚本找出已刷新的结果（带 .projection_full_lidar_v2 标记），按平台原来的
目录结构重打 zip 覆盖旧包；不改数据库、不推飞书、不发前端消息。

用法：
  python tools/refresh_delivery_zips.py --dry-run
  python tools/refresh_delivery_zips.py
"""

from __future__ import annotations

import argparse
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mileage_store  # noqa: E402

MARKER = ".projection_full_lidar_v2"
DELIVERY_DIR_NAME = "delivery_package"
ZIP_SUFFIX = "_参数验证交付.zip"
WANTED_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".xlsx", ".json", ".csv", ".md", ".docx"}
FOLDER_MAP = [
    ("projection/front", "01_front_x2f_点云投影叠加图", None),
    ("projection/front7", "02_front7_x8d_点云投影叠加图", None),
    ("data_quality", "03_数据质检", None),
    ("merged", "04_合成效果图_样例", 8),
]


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


def build_zip(output_dir: Path, zip_path: Path) -> int:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as archive:
        for sub, arc_dir, limit in FOLDER_MAP:
            folder = output_dir / sub
            if not folder.is_dir():
                continue
            files = [
                file
                for file in sorted(folder.iterdir())
                if file.is_file() and file.suffix.lower() in WANTED_EXTS
            ]
            if limit:
                files = files[:limit]
            for file in files:
                archive.write(file, arcname=f"{arc_dir}/{file.name}")
        for file in sorted(output_dir.iterdir()):
            if file.is_file() and file.suffix.lower() in {".docx", ".md", ".xlsx", ".json", ".csv"}:
                archive.write(file, arcname=file.name)
    return zip_path.stat().st_size if zip_path.is_file() else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="刷新交付包 zip（融合图更新后重打包）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    with mileage_store._connect() as conn:
        rows = conn.execute(
            "SELECT data_root, output_dir FROM verify_runs "
            "WHERE status = 'success' AND output_dir IS NOT NULL AND output_dir != ''"
        ).fetchall()

    rebuilt = skipped = failed = 0
    for row in rows:
        output_dir = Path(str(row["output_dir"] or ""))
        data_root = Path(str(row["data_root"] or ""))
        marker = output_dir / MARKER
        if not marker.is_file():
            continue
        delivery_dir = data_root / DELIVERY_DIR_NAME
        zips = sorted(delivery_dir.glob(f"*{ZIP_SUFFIX}")) if delivery_dir.is_dir() else []
        if not zips:
            skipped += 1
            log(f"跳过（没有交付包）：{data_root.name}")
            continue
        zip_path = zips[0]
        try:
            if zip_path.stat().st_mtime >= marker.stat().st_mtime:
                skipped += 1
                continue
        except OSError:
            pass
        log(f"重打交付包：{data_root.name} -> {zip_path}")
        if args.dry_run:
            rebuilt += 1
            continue
        started = time.time()
        try:
            size = build_zip(output_dir, zip_path)
        except OSError as exc:  # noqa: BLE001
            failed += 1
            log(f"  失败：{type(exc).__name__}: {exc}")
            continue
        rebuilt += 1
        log(f"  完成：{size / 1024 / 1024:.1f} MB，用时 {time.time() - started:.1f}s")

    log(f"结束：重打 {rebuilt} 个，跳过 {skipped} 个，失败 {failed} 个")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
