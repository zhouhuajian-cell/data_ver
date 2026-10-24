"""参数验证服务（外参验证 / 里程刷写数据验证）。

把 canshuyanzheng/FrameMap_DataQuality_Projection_Pipeline.py 的能力封装成 HTTP 服务：
用户把数据目录丢进来，服务自动跑「外参校验 + 数据质检」，直接给出结论和报告。

设计要点
--------
1. 原脚本的两个入口函数只读模块级全局变量（DATA_ROOT / POSE_FILE / OUTPUT_ROOT ...），
   不接受参数。所以每次任务前把全局变量临时改成当次数据目录，跑完再还原，
   用一把锁保证串行执行，避免并发改全局导致串数据。
2. 任务在后台线程池执行，前端轮询进度，不阻塞 HTTP。
3. 产出的 Excel / 图表 / JSON 通过 /api/tasks/{id}/files 暴露下载。
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------
# 加载原验证脚本（模块名为固定值，方便 sys.modules 复用）
# --------------------------------------------------------------------------

SERVICE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SERVICE_DIR.parent
# 验证脚本可能放在项目根目录或 service 同级，两处都找
_PIPELINE_CANDIDATES = [
    SERVICE_DIR / "canshuyanzheng" / "FrameMap_DataQuality_Projection_Pipeline.py",
    PROJECT_DIR / "canshuyanzheng" / "FrameMap_DataQuality_Projection_Pipeline.py",
]
PIPELINE_PATH = next((p for p in _PIPELINE_CANDIDATES if p.is_file()), _PIPELINE_CANDIDATES[0])
PIPELINE_MODULE_NAME = "framemap_pipeline"


def resolve_data_root(raw: "str | Path") -> Path:
    """把用户填的路径变成服务器上可读的路径。

    浏览器里用户经常直接粘 Windows 共享路径（\\\\192.168.20.206\\Data_Platform\\...），
    服务器上要用 /mnt/Data_Platform/...；复用里程模块的 CIFS 挂载映射。
    """
    text = str(raw).strip().strip('"').strip("'")
    path = Path(text).expanduser()
    if path.exists():
        return path
    try:
        import ap18_mileage  # noqa: PLC0415 - 延迟导入，避免启动时加载里程模块

        service = ap18_mileage._service()
        mapped = service.normalize_path(text)
        if str(mapped) and Path(str(mapped)).exists():
            return Path(str(mapped))
    except Exception:  # noqa: BLE001 - 映射失败就按原路径报错
        pass
    return path


def scan_verify_datasets(
    parent: "str | Path",
    *,
    max_depth: int = 2,
    max_items: int = 200,
) -> Dict[str, Any]:
    """扫描父目录，找出可用于参数验证的数据集（含 parameter_yaml 或已有产出）。

    只判断目录结构、不解析文件，所以很快；支持向下找两层（如
    ``.../E5-001/<数据集>`` 或 ``.../10/<数据集>``）。
    """
    root = Path(str(parent))
    datasets: List[Dict[str, Any]] = []
    queue: List[tuple[Path, int]] = [(root, 0)]
    while queue and len(datasets) < max_items:
        current, depth = queue.pop(0)
        try:
            children = sorted(
                child for child in current.iterdir() if child.is_dir() and not child.name.startswith(".")
            )
        except OSError:
            continue
        for child in children:
            has_param = (child / "parameter_yaml").is_dir()
            output_dir = child / "framemap_dataquality_projection"
            has_output = output_dir.is_dir()
            if has_param or has_output:
                datasets.append(
                    {
                        "path": str(child),
                        "name": child.name,
                        "has_parameter_yaml": has_param,
                        "has_output": has_output,
                        "output_dir": str(output_dir) if has_output else "",
                        "ready_hint": has_param,
                    }
                )
            elif depth < max_depth - 1:
                queue.append((child, depth + 1))
    datasets.sort(key=lambda item: item["path"])
    return {
        "root": str(root),
        "exists": root.exists(),
        "is_dir": root.is_dir(),
        "count": len(datasets),
        "ready_count": sum(1 for item in datasets if item["ready_hint"]),
        "analyzed_count": sum(1 for item in datasets if item["has_output"]),
        "datasets": datasets,
    }


def load_pipeline_module():
    """加载（或复用已加载的）验证脚本模块。"""
    import sys

    if PIPELINE_MODULE_NAME in sys.modules:
        return sys.modules[PIPELINE_MODULE_NAME]
    spec = importlib.util.spec_from_file_location(PIPELINE_MODULE_NAME, PIPELINE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载验证脚本: {PIPELINE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PIPELINE_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


# 串行执行：原脚本改的是全局变量，不能并发
_PIPELINE_LOCK = threading.Lock()
# 进度回调：由任务层注册，供前端轮询
_PROGRESS: Dict[str, Dict[str, Any]] = {}


def set_progress(task_id: str, **kwargs) -> None:
    state = _PROGRESS.setdefault(task_id, {})
    state.update(kwargs)
    state["updated_at"] = time.time()


def get_progress(task_id: str) -> Dict[str, Any]:
    return dict(_PROGRESS.get(task_id, {}))


# --------------------------------------------------------------------------
# 数据目录探测：判断一个目录是否像可验证的数据集
# --------------------------------------------------------------------------

CAMERA_PREFIX = "cam_"
LIDAR_PREFIX = "lidar_"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
POINT_EXTS = {".las", ".laz", ".pcd"}


def _parse_sensors_used_text(text: str) -> Dict[str, Any]:
    """极简 YAML 兜底：解析 sensors_used.yaml 的 sensors 段。

    生产环境装了 pyyaml 会走 yaml.safe_load；本机调试（或依赖缺失）时用这个，
    只认 ``nickname: device-1, '/topic'`` 这种格式，够覆盖标定文件。
    """
    sensors: Dict[str, Any] = {}
    in_sensors = False
    for raw in text.splitlines():
        if re.match(r"^sensors\s*:\s*$", raw):
            in_sensors = True
            continue
        if not in_sensors:
            continue
        if raw.strip() and not raw.startswith((" ", "\t")):
            break
        match = re.match(r"^\s+([\w.-]+)\s*:\s*(.+?)\s*$", raw)
        if match:
            sensors[match.group(1)] = match.group(2)
    return sensors


def _count_files_fast(folder: Path, exts: set) -> int:
    """只数目录项、不做 stat（SMB 上逐文件 stat 极慢，3000 个文件要几分钟）。"""
    count = 0
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                name = entry.name
                dot = name.rfind(".")
                if dot >= 0 and name[dot:].lower() in exts:
                    count += 1
    except OSError:
        return count
    return count


def _load_sensor_config(yaml_dir: Path) -> Dict[str, Any]:
    """按脚本的规则解析 sensors_used.yaml / topic_map.yaml。

    目录名约定：topic 路径去掉首尾斜杠后把 / 换成 _，
    例如 /cam/front -> cam_front，与脚本 topic_to_foldername 一致。
    """
    out: Dict[str, Any] = {"cameras": {}, "lidars": {}, "calib_date_folder": None}
    if not yaml_dir.is_dir():
        return out
    dates = sorted(d for d in yaml_dir.iterdir() if d.is_dir())
    if not dates:
        return out
    calib = dates[0]
    out["calib_date_folder"] = calib.name
    sensors_path = calib / "sensors_used.yaml"
    if not sensors_path.is_file():
        return out
    try:
        text = sensors_path.read_text(encoding="utf-8")
    except OSError:
        return out
    try:
        import yaml

        sensors_used = yaml.safe_load(text) or {}
        sensors = sensors_used.get("sensors", {}) or {}
    except Exception:
        # 没装 pyyaml 时用极简解析，保证目录预检仍可用
        sensors = _parse_sensors_used_text(text)
    for nickname, info in sensors.items():
        parts = str(info).split(",", 1)
        if len(parts) < 2:
            continue
        device_id = parts[0].strip()
        topic = parts[1].strip().strip("'\"[]")
        folder = topic.strip("/").replace("/", "_")
        if device_id.startswith("camera-"):
            out["cameras"][nickname] = folder
        elif device_id.startswith("lidar-"):
            out["lidars"][nickname] = folder
    return out


def probe_dataset(data_root: Path) -> Dict[str, Any]:
    """检查目录结构，给出能否验证以及缺什么。"""
    info: Dict[str, Any] = {
        "data_root": str(data_root),
        "exists": data_root.exists(),
        "is_dir": data_root.is_dir(),
        "parameter_yaml": False,
        "calib_date_folder": None,
        "sensors_yaml": False,
        "topic_map_yaml": False,
        "cameras": [],
        "lidars": [],
        "pose_files": [],
        "frame_map": False,
        "camera_image_counts": {},
        "lidar_point_counts": {},
        "missing_folders": [],
        "has_front_camera": False,
        "ready": False,
        "problems": [],
        "warnings": [],
    }
    if not data_root.exists():
        info["problems"].append(f"数据目录不存在: {data_root}")
        return info
    if not data_root.is_dir():
        info["problems"].append(f"不是目录: {data_root}")
        return info

    yaml_dir = data_root / "parameter_yaml"
    info["parameter_yaml"] = yaml_dir.is_dir()
    if yaml_dir.is_dir():
        sensor_cfg = _load_sensor_config(yaml_dir)
        info["calib_date_folder"] = sensor_cfg["calib_date_folder"]
        if info["calib_date_folder"]:
            calib = yaml_dir / info["calib_date_folder"]
            info["sensors_yaml"] = (calib / "sensors_used.yaml").is_file()
            info["topic_map_yaml"] = (calib / "topic_map.yaml").is_file()
        else:
            info["problems"].append("parameter_yaml 下没有标定日期目录")

        cameras = sensor_cfg["cameras"]
        lidars = sensor_cfg["lidars"]
        for nickname, folder in cameras.items():
            path = data_root / folder
            if path.is_dir():
                info["cameras"].append(folder)
                info["camera_image_counts"][folder] = _count_files_fast(path, IMAGE_EXTS)
            else:
                info["missing_folders"].append(f"相机 {nickname} -> {folder}")
        for nickname, folder in lidars.items():
            path = data_root / folder
            if path.is_dir():
                info["lidars"].append(folder)
                info["lidar_point_counts"][folder] = _count_files_fast(path, POINT_EXTS)
            else:
                info["missing_folders"].append(f"雷达 {nickname} -> {folder}")

        # 脚本硬性要求存在 front / front_wide / front_narrow 昵称
        info["has_front_camera"] = any(k in cameras for k in ("front", "front_wide", "front_narrow"))

        if not (info["sensors_yaml"] and info["topic_map_yaml"]):
            info["problems"].append("标定日期目录下缺少 sensors_used.yaml 或 topic_map.yaml")
        if not cameras:
            info["problems"].append("sensors_used.yaml 中没有 camera-* 传感器")
        if not lidars:
            info["problems"].append("sensors_used.yaml 中没有 lidar-* 传感器")
        if cameras and not info["has_front_camera"]:
            info["problems"].append(
                "缺少 front / front_wide / front_narrow 相机昵称（FrameMap 生成需要前视相机作基准），"
                f"当前: {', '.join(cameras)}"
            )
        if info["missing_folders"]:
            info["problems"].append("以下传感器目录不存在: " + "; ".join(info["missing_folders"]))
    else:
        info["problems"].append("缺少 parameter_yaml 目录（外参/内参标定文件）")

    # pose 文件：原脚本默认 rtkimu_qj_7683/Vehicle(...).pose
    # 注意不能 glob("**/*.pose")：数据目录里有几千个相机/点云文件，在 SMB 上会走遍
    # 整个树（二十多 GB），预检直接卡死。只扫顶层和已知的惯导目录。
    pose_roots: List[Path] = [data_root]
    try:
        for entry in sorted(data_root.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name.lower().startswith(("rtkimu", "imu", "ins", "gnss", "pose", "nav")):
                pose_roots.append(entry)
    except OSError:
        pass
    poses: List[Path] = []
    for root_dir in pose_roots:
        try:
            for path in root_dir.iterdir():
                if path.is_file() and path.suffix.lower() == ".pose":
                    poses.append(path)
        except OSError:
            continue
    info["pose_files"] = [str(p) for p in sorted(poses)[:5]]

    info["frame_map"] = (data_root / "FrameMap.json").is_file()

    if not info["pose_files"]:
        info["warnings"].append("未找到 .pose 位姿文件，反投影外参验证可能失败")
    if not info["frame_map"]:
        info["warnings"].append("未找到 FrameMap.json（服务会自动生成）")
    info["ready"] = not info["problems"]
    return info


# --------------------------------------------------------------------------
# 验证执行：临时覆写原脚本全局变量
# --------------------------------------------------------------------------

# 需要在执行前覆写的全局变量名
GLOBAL_KEYS = [
    "DATA_ROOT",
    "POSE_FILE",
    "OUTPUT_ROOT",
    "FRAMEMAP_PATH",
    "SENSOR_STATUS_REPORT",
    "QUALITY_OUTPUT_DIR",
    "LIDAR_FOLDER_OVERRIDE",
    "LIDAR_EXTRINSIC_ID_OVERRIDE",
    "ALIGN_PRE_TRANSFORM_FILE",
    "RUN_DATA_QUALITY",
    "RUN_FRAMEMAP_PROJECTION",
    "ENABLE_POINTCLOUD_REPROJECTION",
    "FORCE_REGENERATE_FRAMEMAP",
    "FRAME_IDS",
    "FRAME_START",
    "FRAME_END",
    "FRAME_STEP",
    "ONLY_CAMERAS",
    "SKIP_FISHEYE_CAMERAS",
]


@dataclass
class VerifyOptions:
    """一次验证的参数。"""

    run_data_quality: bool = True
    run_projection: bool = True
    enable_pointcloud_reprojection: bool = False  # 默认只校验外参可用性，不刷整帧点云（快）
    force_regenerate_framemap: bool = True
    frame_start: int = 100
    frame_end: Optional[int] = None
    frame_step: int = 20
    only_cameras: Optional[List[str]] = None


@dataclass
class VerifyResult:
    task_id: str
    status: str = "pending"
    dataset: Dict[str, Any] = field(default_factory=dict)
    verdict: str = ""
    issues: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)
    sensor_status: Dict[str, Any] = field(default_factory=dict)
    quality: Dict[str, Any] = field(default_factory=dict)
    artifacts: List[Dict[str, str]] = field(default_factory=list)
    output_root: str = ""
    log: List[str] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0
    duration_s: float = 0.0
    error: str = ""


def _list_artifacts(output_root: Path) -> List[Dict[str, str]]:
    """收集产出文件，供前端下载。"""
    artifacts: List[Dict[str, str]] = []
    if not output_root.exists():
        return artifacts
    for path in sorted(output_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(output_root)
        size = path.stat().st_size
        kind = {
            ".xlsx": "excel",
            ".json": "json",
            ".png": "chart",
            ".jpg": "image",
            ".csv": "csv",
        }.get(path.suffix.lower(), "file")
        artifacts.append(
            {
                "name": str(rel).replace("\\", "/"),
                "size": size,
                "kind": kind,
            }
        )
    return artifacts


def _read_json(path: Path) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def apply_compat_patches(module) -> List[str]:
    """修补原脚本与新版依赖的兼容问题（不改动原脚本文件）。

    1. matplotlib >= 3.9 把 Axes.boxplot(labels=...) 改名为 tick_labels，
       原脚本的 plot_size_boxplot 会抛 TypeError，导致整条质检流程中断。
       这里换个包一层，两种签名都能跑。
    返回已应用的补丁说明，便于在日志里留痕。
    """
    applied: List[str] = []
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.axes

        original_boxplot = matplotlib.axes.Axes.boxplot
        if not getattr(original_boxplot, "_verify_service_patched", False):

            def boxplot_compat(self, *args, **kwargs):
                if "labels" in kwargs:
                    labels = kwargs.pop("labels")
                    kwargs.setdefault("tick_labels", labels)
                    # 老版本没有 tick_labels，回退到 labels
                    try:
                        return original_boxplot(self, *args, **kwargs)
                    except TypeError:
                        kwargs.pop("tick_labels", None)
                        kwargs["labels"] = labels
                        return original_boxplot(self, *args, **kwargs)
                return original_boxplot(self, *args, **kwargs)

            boxplot_compat._verify_service_patched = True
            matplotlib.axes.Axes.boxplot = boxplot_compat
            applied.append("matplotlib Axes.boxplot(labels=) -> tick_labels 兼容补丁")
    except Exception as exc:  # 补丁失败不应阻断流程
        applied.append(f"matplotlib 补丁失败（忽略）: {exc}")
    return applied


def run_verify(
    task_id: str,
    data_root: Path,
    output_root: Path,
    options: Optional[VerifyOptions] = None,
) -> VerifyResult:
    """执行一次验证。串行执行，内部覆写原脚本全局变量。"""
    options = options or VerifyOptions()
    result = VerifyResult(task_id=task_id, dataset=probe_dataset(data_root))
    result.output_root = str(output_root)
    result.started_at = time.time()

    def log(msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        result.log.append(line)

    if not result.dataset["ready"]:
        result.status = "failed"
        result.verdict = "数据目录不可用于验证"
        result.issues.extend(result.dataset["problems"])
        result.finished_at = time.time()
        result.duration_s = result.finished_at - result.started_at
        set_progress(task_id, status="failed", message=result.verdict)
        return result

    result.warnings.extend(result.dataset["warnings"])

    with _PIPELINE_LOCK:
        module = load_pipeline_module()
        for note in apply_compat_patches(module):
            log(note)
        backup = {key: getattr(module, key) for key in GLOBAL_KEYS if hasattr(module, key)}
        try:
            output_root.mkdir(parents=True, exist_ok=True)

            # ---- 定位关键路径 ----
            pose_file = Path(result.dataset["pose_files"][0]) if result.dataset["pose_files"] else data_root / "rtkimu_qj_7683" / "Vehicle(imu1_2_body2).pose"
            lidar_folder = data_root / result.dataset["lidars"][0] if result.dataset["lidars"] else data_root / "lidar_unknown"
            # 有 *_cut 雷达目录时优先用它：点数少、投影快，平台融合图也用 cut 数据
            lidar_name = result.dataset["lidars"][0] if result.dataset["lidars"] else ""
            if lidar_name and not lidar_name.endswith("_cut"):
                cut_dir = data_root / f"{lidar_name}_cut"
                try:
                    has_cut = cut_dir.is_dir() and any(cut_dir.iterdir())
                except OSError:
                    has_cut = False
                if has_cut:
                    lidar_folder = cut_dir
            out = output_root
            # FrameMap 生成/读取都走输出目录，避免往只读的数据目录里写文件；
            # 数据集里已有的 FrameMap.json 会被复用（FORCE_REGENERATE 时才重建）。
            framemap_path = out / "FrameMap.json"

            # 投影帧数自适应：默认最多约 15 帧/相机（每帧读一次点云+投影，全量帧会非常慢）
            frame_step = max(1, int(options.frame_step or 20))
            if options.run_projection and options.enable_pointcloud_reprojection:
                camera_dirs = sorted(p for p in data_root.glob("cam_*") if p.is_dir())
                frame_total = 0
                if camera_dirs:
                    try:
                        frame_total = sum(1 for _ in camera_dirs[0].glob("*.jpg"))
                    except OSError:
                        frame_total = 0
                if frame_total > 0:
                    frame_step = max(frame_step, -(-frame_total // 15))

            module.DATA_ROOT = data_root
            module.POSE_FILE = pose_file
            module.OUTPUT_ROOT = out
            module.FRAMEMAP_PATH = framemap_path
            module.SENSOR_STATUS_REPORT = out / "sensor_status_report.json"
            module.QUALITY_OUTPUT_DIR = out / "data_quality"
            module.LIDAR_FOLDER_OVERRIDE = lidar_folder
            module.RUN_DATA_QUALITY = options.run_data_quality
            module.RUN_FRAMEMAP_PROJECTION = options.run_projection
            module.ENABLE_POINTCLOUD_REPROJECTION = options.enable_pointcloud_reprojection
            module.FORCE_REGENERATE_FRAMEMAP = options.force_regenerate_framemap
            module.FRAME_IDS = None
            module.FRAME_START = options.frame_start
            module.FRAME_END = options.frame_end
            module.FRAME_STEP = frame_step
            module.ONLY_CAMERAS = ",".join(options.only_cameras) if options.only_cameras else None
            module.SKIP_FISHEYE_CAMERAS = True

            # 数据集里已有的 FrameMap.json 默认复用（拷到输出目录），避免重复生成。
            module.GENERATE_FRAMEMAP_IF_MISSING = True
            source_framemap = data_root / "FrameMap.json"
            if not options.force_regenerate_framemap and source_framemap.is_file():
                try:
                    shutil.copy2(source_framemap, framemap_path)
                    log(f"复用数据集里的 FrameMap: {source_framemap}")
                except OSError as exc:  # 拷贝失败就让脚本自己生成
                    log(f"复用 FrameMap 失败（将重新生成）: {exc}")

            # ---- 外参 ID 选择 ----
            # 原脚本默认 LIDAR_EXTRINSIC_ID_OVERRIDE="body-2"（点云已转到 body 系）。
            # 但很多数据集点云仍在 lidar 原始系，外参文件名是 lidar-1_to_camera-1.yaml。
            # 这里按实际存在的标定文件自动选择，避免误判 missing_extrinsic。
            extrinsic_dir = None
            calib_name = result.dataset.get("calib_date_folder")
            if calib_name:
                extrinsic_dir = data_root / "parameter_yaml" / calib_name / "extrinsic"
            chosen_id = getattr(module, "LIDAR_EXTRINSIC_ID_OVERRIDE", None)
            if extrinsic_dir and extrinsic_dir.is_dir():
                if chosen_id and not list(extrinsic_dir.glob(f"{chosen_id}_to_*.yaml")):
                    # 默认 ID 没有对应文件，退回 lidar-<n>
                    fallback = None
                    for cand in sorted(extrinsic_dir.glob("*_to_*.yaml")):
                        stem = cand.stem.split("_to_", 1)[0]
                        if stem.startswith("lidar"):
                            fallback = stem
                            break
                    if fallback:
                        log(f"外参 ID 自动修正: {chosen_id} -> {fallback}（{extrinsic_dir} 下无 {chosen_id}_to_*.yaml）")
                        chosen_id = fallback
            module.LIDAR_EXTRINSIC_ID_OVERRIDE = chosen_id

            # 关键：align_pre_transform 默认指向硬编码 Linux 路径，存在才启用，否则置空
            pre_file = getattr(module, "ALIGN_PRE_TRANSFORM_FILE", None)
            if pre_file is not None and not Path(pre_file).exists():
                found = list((data_root / "parameter_yaml").glob("*/extrinsic/*_to_*.yaml")) if (data_root / "parameter_yaml").is_dir() else []
                module.ALIGN_PRE_TRANSFORM_FILE = found[0] if found else None
                if module.ALIGN_PRE_TRANSFORM_FILE:
                    log(f"预处理外参改用: {module.ALIGN_PRE_TRANSFORM_FILE}")

            # ---- 1. 数据质检 ----
            if options.run_data_quality:
                set_progress(task_id, status="running", step="数据质检", percent=10)
                log("开始数据质检（缺帧/文件大小/连续段）")
                module.run_data_quality_inspector()
                log("数据质检完成")

            # ---- 2. 外参校验 + 反投影 ----
            if options.run_projection:
                set_progress(task_id, status="running", step="外参校验与反投影", percent=50)
                log("开始外参校验与点云反投影")
                module.run_framemap_alignment_projection()
                log("外参校验与反投影完成")

            set_progress(task_id, status="running", step="汇总结果", percent=90)

            # ---- 3. 汇总 ----
            status_json = _read_json(module.SENSOR_STATUS_REPORT) or {}
            result.sensor_status = status_json
            result.quality = _read_quality_summary(module.QUALITY_OUTPUT_DIR)
            result.artifacts = _list_artifacts(out)
            _build_verdict(result)

            result.status = "success"
            set_progress(task_id, status="success", message=result.verdict, percent=100)
            log(f"验证完成：{result.verdict}")

        except Exception as exc:
            result.status = "failed"
            result.error = f"{type(exc).__name__}: {exc}"
            result.issues.append(f"验证执行失败：{result.error}")
            log(traceback.format_exc())
            set_progress(task_id, status="failed", message=result.error)
        finally:
            for key, value in backup.items():
                setattr(module, key, value)

    result.finished_at = time.time()
    result.duration_s = round(result.finished_at - result.started_at, 2)
    return result


def _read_quality_summary(quality_dir: Path) -> Dict[str, Any]:
    """从质检输出目录提取可读摘要（不依赖内部对象）。"""
    summary: Dict[str, Any] = {"output_dir": str(quality_dir), "charts": []}
    if not quality_dir.exists():
        return summary
    for p in quality_dir.glob("*.png"):
        summary["charts"].append(p.name)
    xlsx = quality_dir / "数据质检报告.xlsx"
    summary["excel"] = xlsx.name if xlsx.is_file() else None
    return summary


def _build_verdict(result: VerifyResult) -> None:
    """根据 sensor_status 与质检结果给出结论。"""
    status = result.sensor_status or {}
    cameras = status.get("cameras", {})
    issues = list(status.get("issues", []))

    total = len(cameras)
    ok_states = {"ready", "ready_no_projection"}
    ok = [name for name, info in cameras.items() if info.get("status") in ok_states]
    missing = {name: info for name, info in cameras.items() if info.get("status") not in ok_states}

    result.summary = {
        "camera_total": total,
        "camera_ok": len(ok),
        "camera_problem": len(missing),
        "camera_ok_list": ok,
        "camera_problem_detail": {
            name: {"status": info.get("status"), "reason": info.get("reason")}
            for name, info in missing.items()
        },
        "lidar": status.get("lidar", {}),
    }

    if total == 0:
        result.verdict = "未发现任何相机，无法验证外参"
        result.issues.append("sensor_status 中没有相机条目")
        return

    if not missing:
        result.verdict = f"外参验证通过：{len(ok)}/{total} 个相机内参+外参齐全"
    else:
        result.verdict = f"外参验证不通过：{len(missing)}/{total} 个相机存在问题"
        for name, info in missing.items():
            result.issues.append(f"{name}: {info.get('reason') or info.get('status')}")
    for issue in issues:
        if issue not in result.issues:
            result.issues.append(issue)


def result_to_dict(result: VerifyResult) -> Dict[str, Any]:
    return asdict(result)


# --------------------------------------------------------------------------
# 任务管理
# --------------------------------------------------------------------------

TASKS: Dict[str, VerifyResult] = {}
_TASK_LOCK = threading.Lock()


def _persist_result(result: VerifyResult) -> None:
    """把验证结果写进 SQLite，下次同一目录直接命中历史、不再重跑。"""
    data_root = str((result.dataset or {}).get("data_root") or "").strip()
    if not data_root:
        return
    try:
        import mileage_store  # noqa: PLC0415 - 与里程模块共用同一个库文件

        mileage_store.save_verify_run(
            data_root, result_to_dict(result), result.output_root
        )
    except Exception:  # noqa: BLE001 - 落库失败不影响本次结果
        pass


def register_cached_task(task_dict: Dict[str, Any]) -> str:
    """把历史结果注册成一个已完成的“任务”，让前端能复用产出文件的查看/下载接口。"""
    payload = dict(task_dict or {})
    task_id = str(payload.get("task_id") or "").strip()
    if not task_id:
        task_id = f"cached-{uuid.uuid4().hex[:8]}"
        payload["task_id"] = task_id
    allowed = set(VerifyResult.__dataclass_fields__.keys())
    result = VerifyResult(**{key: value for key, value in payload.items() if key in allowed})
    result.status = result.status or "success"
    with _TASK_LOCK:
        TASKS[task_id] = result
    return task_id


def create_task(data_root: Path, output_root: Path, options: Optional[VerifyOptions] = None) -> str:
    task_id = uuid.uuid4().hex[:12]
    with _TASK_LOCK:
        TASKS[task_id] = VerifyResult(task_id=task_id, status="queued")
    _PROGRESS[task_id] = {"status": "queued", "percent": 0, "message": "排队中"}

    def _run():
        try:
            result = run_verify(task_id, data_root, output_root, options)
        except Exception as exc:  # 兜底，避免线程静默死亡
            with _TASK_LOCK:
                r = TASKS[task_id]
                r.status = "failed"
                r.error = str(exc)
            set_progress(task_id, status="failed", message=str(exc))
            return
        with _TASK_LOCK:
            TASKS[task_id] = result
        _persist_result(result)

    threading.Thread(target=_run, name=f"verify-{task_id}", daemon=True).start()
    return task_id


def get_task(task_id: str) -> Optional[VerifyResult]:
    return TASKS.get(task_id)


def list_tasks() -> List[Dict[str, Any]]:
    with _TASK_LOCK:
        items = list(TASKS.values())
    items.sort(key=lambda r: r.started_at or 0, reverse=True)
    return [
        {
            "task_id": r.task_id,
            "status": r.status,
            "verdict": r.verdict,
            "dataset": r.dataset.get("data_root", ""),
            "started_at": r.started_at,
            "duration_s": r.duration_s,
        }
        for r in items
    ]
