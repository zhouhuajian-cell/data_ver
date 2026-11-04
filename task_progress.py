#!/usr/bin/env python3
"""里程刷写任务进度：落盘到 data/mileage_progress，平台顶部任务进度下拉展示。"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent
PROGRESS_DIR = ROOT / "data" / "mileage_progress"


def task_id_for(data_root: str) -> str:
    return hashlib.sha1(str(data_root).encode("utf-8", "replace")).hexdigest()[:16]


def _write(task_id: str, payload: Dict[str, Any]) -> None:
    try:
        PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
        payload = dict(payload)
        payload["task_id"] = task_id
        payload["pid"] = os.getpid()
        payload["updated_at"] = time.time()
        (PROGRESS_DIR / f"{task_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


def start(data_root: str, **extra: Any) -> str:
    task_id = task_id_for(data_root)
    _write(
        task_id,
        {
            "status": "running",
            "step": "分析里程",
            "message": extra.pop("message", "正在分析轨迹与 2M/8M 覆盖"),
            "source": extra.pop("source", "manual"),
            "started_at": time.time(),
            "dataset": Path(str(data_root)).name,
            "data_root": str(data_root),
            **extra,
        },
    )
    return task_id


def finish(task_id: str, status: str = "success", message: str = "", **extra: Any) -> None:
    path = PROGRESS_DIR / f"{task_id}.json"
    payload: Dict[str, Any] = {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    payload.update(
        {
            "status": status,
            "message": message,
            "finished_at": time.time(),
            **extra,
        }
    )
    _write(task_id, payload)


def list_tasks(max_age_s: float = 24 * 3600) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if not PROGRESS_DIR.is_dir():
        return items
    now = time.time()
    for path in PROGRESS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        updated = float(data.get("updated_at") or 0)
        if now - updated > max_age_s:
            try:
                path.unlink()
            except OSError:
                pass
            continue
        if str(data.get("status") or "") in ("running", "queued"):
            pid = int(data.get("pid") or 0)
            alive = False
            if pid > 0:
                try:
                    os.kill(pid, 0)
                    alive = True
                except ProcessLookupError:
                    alive = False
                except PermissionError:
                    alive = True
                except OSError:
                    alive = False
            if not alive:
                data["status"] = "failed"
                data["message"] = "任务进程已退出（可能被中断）"
        items.append(data)
    items.sort(key=lambda item: float(item.get("updated_at") or 0), reverse=True)
    return items
