"""把「已有参数验证产出」的数据集回填进 verify_runs，让单日页面的「库中已有结果」有历史。

用法：python tools/backfill_verify_history.py <父目录> [<父目录> ...]
说明：只回填 framemap_dataquality_projection 已存在、且库里没有记录的目录；
时间取产出目录的修改时间，便于按历史时间排序。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mileage_store  # noqa: E402


def backfill(parent: Path) -> dict:
    stats = {"scanned": 0, "added": 0, "skipped": 0}
    if not parent.is_dir():
        return stats
    for child in sorted(parent.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        output_dir = child / "framemap_dataquality_projection"
        if not output_dir.is_dir():
            continue
        stats["scanned"] += 1
        key = str(child)
        if mileage_store.get_verify_run(key):
            stats["skipped"] += 1
            continue
        try:
            mtime = datetime.fromtimestamp(output_dir.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except OSError:
            mtime = ""
        mileage_store.save_verify_run(
            key,
            {
                "status": "existing",
                "task_id": "",
                "dataset": {"data_root": key.replace("\\", "/")},
                "artifacts": [],
                "log": ["已存在的参数验证产出（回填）"],
            },
            str(output_dir),
            updated_at=mtime or None,
        )
        stats["added"] += 1
    return stats


def main() -> int:
    parents = [Path(item) for item in sys.argv[1:]]
    if not parents:
        print("用法: python tools/backfill_verify_history.py <父目录> [...]")
        return 2
    total = {"scanned": 0, "added": 0, "skipped": 0}
    for parent in parents:
        stats = backfill(parent)
        print(f"{parent}: {stats}")
        for key in total:
            total[key] += stats[key]
    print(f"合计: {total}")
    print(f"库中现在有 {mileage_store.list_verify_runs_page(limit=1)['total']} 条参数验证记录")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
