"""参数验证 + 里程分析服务（两个模块完全独立）。

模块划分
--------
- **参数验证** `/api/param/*`：外参/内参校验、数据质检（canshuyanzheng 管线）
- **里程分析** `/api/mileage/*`：GPS覆盖里程、2M/8M 里程、2M/8M 相机覆盖

两个模块有各自的数据目录探测、任务表、输出目录和 API 前缀，互不干扰。

启动：
    python verify_service/app.py
    uvicorn verify_service.app:app --host 0.0.0.0 --port 8801
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

SERVICE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SERVICE_DIR))

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import batch as b
import mileage as m
import verifier as v
from verifier import VerifyOptions, create_task, get_progress, get_task, list_tasks, probe_dataset

BASE_DIR = SERVICE_DIR
DEFAULT_OUTPUT_ROOT = BASE_DIR / "output"

app = FastAPI(title="验证服务", description="参数验证（外参）+ 里程分析（2M/8M 覆盖）")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==========================================================================
# 请求模型
# ==========================================================================


class ProbeRequest(BaseModel):
    data_root: str


class VerifyRequest(BaseModel):
    data_root: str
    output_root: Optional[str] = None
    run_data_quality: bool = True
    run_projection: bool = True
    enable_pointcloud_reprojection: bool = False
    force_regenerate_framemap: bool = True
    frame_start: int = 100
    frame_end: Optional[int] = None
    frame_step: int = 20
    only_cameras: Optional[List[str]] = None


class MileageProbeRequest(BaseModel):
    data_root: str


class MileageRequest(BaseModel):
    data_root: str
    output_root: Optional[str] = None
    analyze_coverage: bool = True
    analyze_mileage: bool = True
    baseline_tolerance_m: float = 0.5


class BatchRequest(BaseModel):
    root: str
    output_root: Optional[str] = None
    # 只分析这些日期目录（如 ["20260916","20260917"]）；不传表示全部
    selected: Optional[List[str]] = None


# ==========================================================================
# 页面
# ==========================================================================


@app.get("/", response_class=HTMLResponse)
def index():
    page = BASE_DIR / "static" / "index.html"
    if page.is_file():
        return HTMLResponse(page.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>验证服务</h1><p>缺少 static/index.html</p>")


@app.get("/report/{task_id}", response_class=HTMLResponse)
def param_report_page(task_id: str):
    """参数验证结果页（同源渲染）。"""
    if get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _render_page({"__TASK_ID__": task_id, "__MODULE__": "param"})


@app.get("/mileage/report/{task_id}", response_class=HTMLResponse)
def mileage_report_page(task_id: str):
    """里程分析结果页（同源渲染）。"""
    if m.get_mileage_task(task_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _render_page({"__TASK_ID__": task_id, "__MODULE__": "mileage"})


def _render_page(inject_vars: dict) -> HTMLResponse:
    page = BASE_DIR / "static" / "index.html"
    html_text = page.read_text(encoding="utf-8") if page.is_file() else "<html></html>"
    script = "<script>window.__BOOT__=" + _json_dumps(inject_vars) + ";</script>"
    return HTMLResponse(html_text.replace("</body>", script + "</body>"))


def _json_dumps(obj) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


# ==========================================================================
# 公共
# ==========================================================================


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "modules": {
            "param": {
                "pipeline": str(v.PIPELINE_PATH),
                "pipeline_exists": v.PIPELINE_PATH.is_file(),
            },
            "mileage": {
                "core_dir": str(m.MILEAGE_CORE_DIR),
                "calculator": str(m.MILEAGE_CORE_DIR / "main.py"),
                "calculator_exists": (m.MILEAGE_CORE_DIR / "main.py").is_file(),
                "parser_exists": (m.MILEAGE_CORE_DIR / "trajectory_parser.py").is_file(),
            },
        },
        "default_output_root": str(DEFAULT_OUTPUT_ROOT),
    }


@app.get("/api/modules")
def modules():
    """前端用：列出两个模块及其说明。"""
    return [
        {
            "key": "param",
            "name": "参数验证",
            "desc": "外参/内参校验、传感器数据质检（缺帧、大小异常、连续段）",
            "api": "/api/param",
        },
        {
            "key": "mileage",
            "name": "里程分析",
            "desc": "GPS覆盖里程、2M/8M 里程、2M/8M 相机覆盖",
            "api": "/api/mileage",
        },
    ]


# ==========================================================================
# 模块一：参数验证  /api/param/*
# ==========================================================================


@app.post("/api/param/probe")
def param_probe(req: ProbeRequest):
    return probe_dataset(Path(req.data_root))


@app.post("/api/param/verify")
def param_verify(req: VerifyRequest):
    data_root = Path(req.data_root)
    info = probe_dataset(data_root)
    if not info["ready"]:
        raise HTTPException(
            status_code=400,
            detail={"message": "数据目录不可用于参数验证", "problems": info["problems"], "dataset": info},
        )
    # 结果默认写回被分析的数据目录下的 output/<模块>/，跟着数据走
    output_root = Path(req.output_root) if req.output_root else data_root / "output" / "param"
    task_dir = output_root / f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    options = VerifyOptions(
        run_data_quality=req.run_data_quality,
        run_projection=req.run_projection,
        enable_pointcloud_reprojection=req.enable_pointcloud_reprojection,
        force_regenerate_framemap=req.force_regenerate_framemap,
        frame_start=req.frame_start,
        frame_end=req.frame_end,
        frame_step=req.frame_step,
        only_cameras=req.only_cameras,
    )
    task_id = create_task(data_root, task_dir, options)
    return {"task_id": task_id, "dataset": info, "output_root": str(task_dir)}


@app.get("/api/param/tasks")
def param_tasks():
    return list_tasks()


@app.get("/api/param/tasks/{task_id}")
def param_task_detail(task_id: str):
    task = get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return v.result_to_dict(task)


@app.get("/api/param/tasks/{task_id}/progress")
def param_task_progress(task_id: str):
    if get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return get_progress(task_id)


@app.get("/api/param/tasks/{task_id}/files")
def param_task_files(task_id: str):
    task = get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task.artifacts


# ==========================================================================
# 模块二：里程分析  /api/mileage/*
# ==========================================================================


@app.post("/api/mileage/probe")
def mileage_probe(req: MileageProbeRequest):
    return m.probe_mileage_dataset(m.normalize_path(req.data_root))


@app.post("/api/mileage/analyze")
def mileage_analyze(req: MileageRequest):
    data_root = m.normalize_path(req.data_root)
    info = m.probe_mileage_dataset(data_root)
    if not info["ready"]:
        raise HTTPException(
            status_code=400,
            detail={"message": "数据目录不可用于里程分析", "problems": info["problems"], "dataset": info},
        )
    # 结果默认写回被分析的数据目录下的 output/mileage/，跟着数据走
    output_root = Path(req.output_root) if req.output_root else data_root / "output" / "mileage"
    task_dir = output_root / f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    options = m.MileageOptions(
        baseline_tolerance_m=req.baseline_tolerance_m,
        analyze_coverage=req.analyze_coverage,
        analyze_mileage=req.analyze_mileage,
    )
    task_id = m.create_mileage_task(data_root, task_dir, options)
    return {"task_id": task_id, "dataset": info, "output_root": str(task_dir)}


@app.get("/api/mileage/tasks")
def mileage_tasks():
    return m.list_mileage_tasks()


@app.get("/api/mileage/tasks/{task_id}")
def mileage_task_detail(task_id: str):
    from dataclasses import asdict

    task = m.get_mileage_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return asdict(task)


@app.get("/api/mileage/tasks/{task_id}/progress")
def mileage_task_progress(task_id: str):
    if m.get_mileage_task(task_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return m.get_mileage_progress(task_id)


@app.get("/api/mileage/tasks/{task_id}/files")
def mileage_task_files(task_id: str):
    task = m.get_mileage_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task.artifacts


# ==========================================================================
# 模块三：批量里程刷写  /api/batch/*
# ==========================================================================


@app.post("/api/batch/discover")
def batch_discover(req: BatchRequest):
    """扫描父目录，列出日期目录及预检结果，供人工勾选。"""
    root = m.normalize_path(req.root)
    if not root.exists():
        raise HTTPException(status_code=400, detail={"message": f"目录不存在: {root}"})
    days = b.preview_day_dirs(root)
    return {
        "root": str(root),
        "count": len(days),
        "analyzable_count": sum(1 for d in days if d["analyzable"]),
        "days": days,
    }


@app.post("/api/batch/analyze")
def batch_analyze(req: BatchRequest):
    """批量分析：遍历子目录，按天刷里程，产出汇总报表。

    传 selected 时只分析选中的日期目录。
    """
    root = m.normalize_path(req.root)
    if not root.exists():
        raise HTTPException(status_code=400, detail={"message": f"目录不存在: {root}"})
    days = b.discover_day_dirs(root)
    if not days:
        raise HTTPException(status_code=400, detail={"message": f"在 {root} 下没有找到日期目录（形如 20260916）"})

    # 人工选择校验
    selected = None
    if req.selected:
        existing = {d.name for d in days}
        wanted = [s.strip() for s in req.selected if s and s.strip()]
        invalid = [s for s in wanted if s not in existing]
        if invalid:
            raise HTTPException(
                status_code=400,
                detail={"message": f"以下日期目录不存在：{', '.join(invalid)}",
                        "available": sorted(existing)},
            )
        selected = wanted

    # 汇总写回父目录下的 output/batch/
    output_root = Path(req.output_root) if req.output_root else root / m.OUTPUT_DIR_NAME / "batch"
    task_dir = output_root / f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    task_id = b.create_batch_task(root, task_dir, selected=selected)
    targets = selected if selected else [d.name for d in days]
    return {
        "task_id": task_id,
        "root": str(root),
        "days_found": len(days),
        "days_selected": len(targets),
        "days": targets,
        "output_root": str(task_dir),
    }


@app.get("/api/batch/tasks")
def batch_tasks():
    return b.list_batch_tasks()


@app.get("/api/batch/tasks/{task_id}")
def batch_task_detail(task_id: str):
    from dataclasses import asdict

    task = b.get_batch_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return asdict(task)


@app.get("/api/batch/tasks/{task_id}/progress")
def batch_task_progress(task_id: str):
    if b.get_batch_task(task_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return b.get_batch_progress(task_id)


@app.get("/api/batch/tasks/{task_id}/download")
def batch_download(task_id: str, name: str = Query(...)):
    task = b.get_batch_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _serve_file(task.output_root, name)


@app.get("/batch/report/{task_id}", response_class=HTMLResponse)
def batch_report_page(task_id: str):
    """批量汇总报表（直接渲染）。"""
    task = b.get_batch_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.summary_html_path and Path(task.summary_html_path).is_file():
        return HTMLResponse(Path(task.summary_html_path).read_text(encoding="utf-8"))
    raise HTTPException(status_code=404, detail="汇总报表尚未生成")


# ==========================================================================
# 下载（两个模块共用，按 module 区分根目录，防止越界）
# ==========================================================================


@app.get("/api/param/tasks/{task_id}/download")
def param_download(task_id: str, name: str = Query(...)):
    task = get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _serve_file(task.output_root, name)


@app.get("/api/mileage/tasks/{task_id}/download")
def mileage_download(task_id: str, name: str = Query(...)):
    task = m.get_mileage_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _serve_file(task.output_root, name)


def _serve_file(root_str: str, name: str):
    root = Path(root_str or DEFAULT_OUTPUT_ROOT).resolve()
    target = (root / name).resolve()
    if root not in target.parents and target != root:
        raise HTTPException(status_code=400, detail="非法路径")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(target, filename=target.name)


static_dir = BASE_DIR / "static"
if static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


def main():
    import uvicorn

    port = int(os.environ.get("VERIFY_PORT", "8801"))
    host = os.environ.get("VERIFY_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
