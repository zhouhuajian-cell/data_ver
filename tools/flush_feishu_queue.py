#!/usr/bin/env python3
"""补发飞书失败队列：发送成功才出队，失败继续留着，定时任务每 5 分钟跑一次。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ap18_visualizer as viz  # noqa: E402


def main() -> int:
    sent, failed = viz.flush_feishu_queue(limit=10)
    if sent or failed:
        print(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} 飞书补发：成功 {sent} 条，失败 {failed} 条（继续留在队列）",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
