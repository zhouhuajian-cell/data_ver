#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FrameMap + DataQualityInspector + pointcloud projection 二合一脚本。

来源：
  - FrameMap_Align_projection_Pipeline.py
  - DataQualityInspector.py

离线多机使用要求：
  1. 本脚本和 simhei.ttf 放在同一目录。
  2. 所有 config 参数都在脚本顶部“硬编码配置区”修改，不用 argparse/环境变量传参。
  3. 字体只使用脚本同级 simhei.ttf，不依赖 /home/hy 字体路径或系统字体。

运行：
  python3 FrameMap_DataQuality_Projection_Pipeline.py
"""

# ===== BEGIN: FrameMap_Align_projection_Pipeline definitions =====
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Change History:
2026-01-14: 修改相机文件夹匹配逻辑，改为根据位置后缀（Location）进行匹配，忽略中间的型号标识符。
"""

import bisect
import glob
import json
import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import laspy
import numpy as np
import pandas as pd
import yaml
from scipy.optimize import linear_sum_assignment
from scipy.spatial.transform import Rotation as R
from scipy.spatial.transform import Slerp

LOG = logging.getLogger("framemap_align_projection")


# ============================================================
#  硬编码配置区：多机离线使用时只改这里，不用 args / env 传参
#  测试数据：HS5-002 / 1454_test_260617_HS5-002_L2I_2_GND
# ============================================================
SCRIPT_DIR = Path(__file__).resolve().parent
SIBLING_FONT_PATH = SCRIPT_DIR / "simhei.ttf"


DATA_ROOT = Path("/mnt/Data_Platform/files/MaxieyeStructuredData/E5-001_VSLAM_yanzheng/E5-001/137501_E5-001_yanzhen_20261001")
POSE_FILE = DATA_ROOT / "rtkimu_qj_7683" / "Vehicle(imu1_2_body2).pose"
OUTPUT_ROOT = DATA_ROOT / "framemap_dataquality_projection"
FRAMEMAP_PATH = DATA_ROOT / "FrameMap.json"

# 主流程开关
RUN_DATA_QUALITY = True           #是否数据质检
RUN_FRAMEMAP_PROJECTION = True     #是否刷反投影
ENABLE_POINTCLOUD_REPROJECTION = True   # False: 只做质检 + sensor status；True: 实际点云反投影

# FrameMap / 传感器状态 / 点云投影配置
GENERATE_FRAMEMAP_IF_MISSING = True  
FORCE_REGENERATE_FRAMEMAP = True
LIDAR_EXTRINSIC_ID_OVERRIDE = "body-2"     #lidar的设备坐标系
LIDAR_FOLDER_OVERRIDE = DATA_ROOT / "lidar_hs_pandar128_topmiddle"     #点云文件夹名称


# 坐标系转换配置：当前点云坐标系按 body-2 使用，默认不做 pre/post transform。
ALIGN_ENABLE_PRE_TRANSFORM = False              
ALIGN_PRE_TRANSFORM_FILE = DATA_ROOT / "parameter_yaml" / "20260920" / "extrinsic" / "lidar-1_to_imu-2.yaml"  #参数文件夹
ALIGN_ENABLE_POST_TRANSFORM = False
ALIGN_POST_TRANSFORM_FILE = None

# 投影帧/相机筛选配置
FRAME_IDS = None              # 例如 "0,10,20"；None 时使用范围采样
FRAME_START = 100
FRAME_END = None   # None 表示跑到最后一帧
FRAME_STEP = 20
ONLY_CAMERAS = None           # 例如 "front,front_narrow"；None 表示全部

# 相机模型规则
FISHEYE_NAME_KEYWORDS = ["avm"]
NARROW_KEYWORD = "narrow"
DEFAULT_PINHOLE_DISTORTED = True
SKIP_FISHEYE_CAMERAS = True
# REUSE_ALIGNED 已废弃：现在不保存 aligned 点云，统一从原始点云现场去畸变投影。
SENSOR_STATUS_REPORT = OUTPUT_ROOT / "sensor_status_report.json"

# corrected annotation/pose/video/sync copy，默认关闭；这次需求不是 copy_sync。
ENABLE_COPY_ANNOTATIONS_POSE_SYNC = False
COPY_INPUT_ROOT = Path("/mnt/done_data_88/28_overseas/gnd/Test")
COPY_DONE_ROOT = DATA_ROOT
COPY_COUNTRY = None
COPY_REPORT_OUT = None
COPY_DRY_RUN = False
COPY_SCRIPT_PATH = Path("/mnt/share_88/panshuainan/20241203/demo/nvidia-vla/copy_annotations_pose_update_sync.py")

# DataQualityInspector 配置
QUALITY_OUTPUT_DIR = OUTPUT_ROOT / "data_quality"
QUALITY_CLIP_THRESHOLDS = [30, 70, 140]
QUALITY_GAP_THRESHOLD = 0.18
QUALITY_EXPECTED_INTERVAL = 0.1
QUALITY_SMALL_FILE_THRESHOLD_KB = 200
QUALITY_SIZE_STD_FACTOR = 3.0
QUALITY_SCAN_WORKERS = min(8, os.cpu_count() or 4)
# sensor_timeline 横轴策略：True = 使用所有传感器全局 min/max，避免某个传感器独有的前/后段被坐标轴裁掉。
TIMELINE_USE_GLOBAL_XLIM = True
TIMELINE_X_MIN = None
TIMELINE_X_MAX = None


@dataclass
class CameraProjectionConfig:
    image_is_undistorted: Optional[bool] = None
    force_fisheye: bool = False
    horizontal_fov_override: Optional[float] = None
    vertical_fov_override: Optional[float] = None


class FrameMapGenerator:
    def __init__(self, data_root: str):
        self.data_root = data_root
        self.parameter_yaml_dir = os.path.join(data_root, "parameter_yaml")

        self.SEARCH_MIN_OFFSET = 0.105
        self.SEARCH_MAX_OFFSET = -0.010
        self.IDEAL_OFFSET = 0.050
        self.EXPECTED_INTERVAL_MIN = 0.060
        self.EXPECTED_INTERVAL_MAX = 0.140
        self.LIDAR_RANGE_OFFSET = 0.100
        self.OVERLAP_EXPAND = 0.120

        self.OTHER_CAMERA_WINDOW = 0.050
        self.MAX_HUNGARIAN_CHUNK = 1000  # 单次 Hungarian 匹配的最大帧数，防止 OOM

    def parse_timestamp(self, filename: str) -> Optional[float]:
        try:
            name = os.path.basename(filename)
            parts = name.split(".")
            return float(f"{parts[0]}.{parts[1]}")
        except Exception:
            return None

    def format_timestamp(self, ts: Optional[float]) -> Optional[str]:
        if ts is None:
            return None
        return f"{ts:.3f}"

    def load_yaml_config(self):
        calib_folders = [
            d
            for d in os.listdir(self.parameter_yaml_dir)
            if os.path.isdir(os.path.join(self.parameter_yaml_dir, d))
        ]
        if not calib_folders:
            raise ValueError(f"No calibration date folder found in {self.parameter_yaml_dir}")

        calib_date_folder = os.path.join(self.parameter_yaml_dir, calib_folders[0])
        sensors_used_path = os.path.join(calib_date_folder, "sensors_used.yaml")
        topic_map_path = os.path.join(calib_date_folder, "topic_map.yaml")

        LOG.info("Loading config from: %s", calib_date_folder)

        with open(sensors_used_path, "r", encoding="utf-8") as f:
            sensors_used = yaml.safe_load(f)
        with open(topic_map_path, "r", encoding="utf-8") as f:
            topic_map = yaml.safe_load(f)

        return sensors_used, topic_map

    def topic_to_foldername(self, topic_path: str) -> str:
        return topic_path.strip("/").replace("/", "_")

    def find_actual_folder(self, expected_name: str, sensor_type: str = "camera") -> Optional[str]:
        prefix = "cam_" if sensor_type == "camera" else "lidar_"
        exact_path = os.path.join(self.data_root, expected_name)
        if os.path.exists(exact_path):
            return expected_name

        all_folders = [
            d
            for d in os.listdir(self.data_root)
            if os.path.isdir(os.path.join(self.data_root, d)) and d.startswith(prefix)
        ]

        # New logic: Match by location suffix (cam_model1_model2_location...)
        if sensor_type == "camera":
            expected_parts = expected_name.split("_")
            # Check if it follows "cam_X_Y_Location" structure (at least 4 parts)
            if len(expected_parts) >= 4 and expected_parts[0] == "cam":
                # Extract location (everything after the 3rd part)
                target_location = "_".join(expected_parts[3:])
                for folder in all_folders:
                    folder_parts = folder.split("_")
                    if len(folder_parts) >= 4:
                        folder_location = "_".join(folder_parts[3:])
                        if folder_location == target_location:
                            LOG.info(
                                "Matched camera folder by location '%s': %s -> %s",
                                target_location,
                                expected_name,
                                folder,
                            )
                            return folder

        expected_parts = expected_name.split("_")
        best_match = None
        best_score = 0

        for folder in all_folders:
            folder_parts = folder.split("_")
            common = sum(1 for p in expected_parts if p in folder_parts)
            score = common / max(len(expected_parts), len(folder_parts))
            if score > best_score:
                best_score = score
                best_match = folder

        if best_score > 0.7:
            return best_match
        return None

    def parse_sensors_config(self, sensors_used, topic_map):
        sensors = sensors_used.get("sensors", {})
        topic_dict = topic_map.get("topic", {})

        camera_config = {}
        lidar_config = {}

        for nickname, sensor_info in sensors.items():
            parts = str(sensor_info).split(",", 1)
            if len(parts) < 2:
                continue
            device_id = parts[0].strip()
            topic_path = parts[1].strip().strip("'\"[]")
            expected_folder = self.topic_to_foldername(topic_path)

            if device_id.startswith("camera-"):
                actual_folder = self.find_actual_folder(expected_folder, "camera")
                if actual_folder:
                    camera_config[nickname] = actual_folder
            elif device_id.startswith("lidar-"):
                actual_folder = self.find_actual_folder(expected_folder, "lidar")
                if actual_folder:
                    lidar_config[nickname] = actual_folder

        return camera_config, lidar_config

    def load_sensor_files(self, folder_name: str, extension: str):
        folder_path = os.path.join(self.data_root, folder_name)
        if not os.path.exists(folder_path):
            return []

        files_data = []
        search_pattern = os.path.join(glob.escape(folder_path), f"*{extension}")
        for filepath in glob.glob(search_pattern):
            ts = self.parse_timestamp(filepath)
            if ts is not None:
                entry = {"path": filepath, "name": os.path.basename(filepath), "ts": ts}
                if extension.lower() == ".las":
                    entry["gpstime_min"] = ts - self.LIDAR_RANGE_OFFSET
                    entry["gpstime_max"] = ts
                files_data.append(entry)

        files_data.sort(key=lambda x: x["ts"])
        return files_data

    def is_within_lidar_range(self, img_ts, lidar_min_ts, lidar_max_ts):
        left_ok = True if lidar_min_ts is None else img_ts > lidar_min_ts
        right_ok = True if lidar_max_ts is None else img_ts <= lidar_max_ts
        return left_ok and right_ok

    def get_lidar_gpstime_range(self, lidar_info):
        min_ts = lidar_info.get("gpstime_min")
        max_ts = lidar_info.get("gpstime_max")
        ts = lidar_info.get("ts")
        if ts is not None:
            max_ts = ts if max_ts is None else max_ts
            min_ts = (ts - self.LIDAR_RANGE_OFFSET) if min_ts is None else min_ts
        return min_ts, max_ts

    def find_candidates(self, lidar_ts, image_ts_list):
        min_t = lidar_ts - self.SEARCH_MIN_OFFSET
        max_t = lidar_ts - self.SEARCH_MAX_OFFSET
        start_idx = bisect.bisect_left(image_ts_list, min_t)
        end_idx = bisect.bisect_right(image_ts_list, max_t)
        return list(range(start_idx, end_idx))

    def build_regular_segments(self, data_list):
        segments = []
        if not data_list:
            return segments
        start_idx = 0
        for idx in range(1, len(data_list)):
            gap = data_list[idx]["ts"] - data_list[idx - 1]["ts"]
            if gap < self.EXPECTED_INTERVAL_MIN or gap > self.EXPECTED_INTERVAL_MAX:
                segments.append((start_idx, idx - 1))
                start_idx = idx
        segments.append((start_idx, len(data_list) - 1))
        return segments

    def compute_overlap_ranges(self, lidar_segments, image_segments, lidar_data, image_data):
        overlaps = []
        for l_start, l_end in lidar_segments:
            l_start_ts = lidar_data[l_start]["ts"]
            l_end_ts = lidar_data[l_end]["ts"]
            for i_start, i_end in image_segments:
                i_start_ts = image_data[i_start]["ts"]
                i_end_ts = image_data[i_end]["ts"]
                start_ts = max(l_start_ts, i_start_ts) - self.OVERLAP_EXPAND
                end_ts = min(l_end_ts, i_end_ts) + self.OVERLAP_EXPAND
                if start_ts > end_ts:
                    continue
                lidar_indices = [
                    idx
                    for idx in range(l_start, l_end + 1)
                    if start_ts <= lidar_data[idx]["ts"] <= end_ts
                ]
                image_indices = [
                    idx
                    for idx in range(i_start, i_end + 1)
                    if start_ts <= image_data[idx]["ts"] <= end_ts
                ]
                lidar_indices = self.extend_with_neighbors(lidar_indices, l_start, l_end)
                image_indices = self.extend_with_neighbors(image_indices, i_start, i_end)
                if lidar_indices and image_indices:
                    overlaps.append({"lidar_indices": lidar_indices, "image_indices": image_indices})
        return overlaps

    def extend_with_neighbors(self, indices, segment_start, segment_end):
        if not indices:
            return []
        expanded = list(indices)
        first = expanded[0]
        last = expanded[-1]
        if first - 1 >= segment_start:
            expanded.insert(0, first - 1)
        if last + 1 <= segment_end:
            expanded.append(last + 1)
        return sorted(set(expanded))

    def split_segments_by_candidates(self, lidar_indices, candidates_map, lidar_data):
        segments = []
        current = []
        prev_idx = None
        for idx in lidar_indices:
            if idx not in candidates_map:
                if current:
                    segments.append(current)
                    current = []
                prev_idx = None
                continue
            if current:
                continuous = idx == prev_idx + 1
                if continuous:
                    gap = lidar_data[idx]["ts"] - lidar_data[prev_idx]["ts"]
                    if gap < self.EXPECTED_INTERVAL_MIN or gap > self.EXPECTED_INTERVAL_MAX:
                        continuous = False
                if not continuous:
                    segments.append(current)
                    current = [idx]
                else:
                    current.append(idx)
            else:
                current = [idx]
            prev_idx = idx
        if current:
            segments.append(current)
        return segments

    def select_start_frame_image(self, l_idx, local_candidates, used_image_indices, lidar_data, image_data):
        lidar_range_min, lidar_range_max = self.get_lidar_gpstime_range(lidar_data[l_idx])
        start_candidates = [
            img
            for img in local_candidates.get(l_idx, [])
            if self.is_within_lidar_range(image_data[img]["ts"], lidar_range_min, lidar_range_max)
        ]
        if not start_candidates:
            return None
        usable_start = [img for img in start_candidates if img not in used_image_indices]
        pool_start = usable_start if usable_start else start_candidates
        best_start_img = min(
            pool_start,
            key=lambda x: abs(lidar_data[l_idx]["ts"] - image_data[x]["ts"] - self.IDEAL_OFFSET),
        )
        return best_start_img

    def match_lidar_front_camera(self, lidar_data, front_camera_data):
        LOG.info("Matching LiDAR with front camera using Hungarian algorithm...")

        image_ts_list = [x["ts"] for x in front_camera_data]
        n_lidar = len(lidar_data)

        base_candidates = {}
        for i in range(n_lidar):
            cands = self.find_candidates(lidar_data[i]["ts"], image_ts_list)
            if cands:
                base_candidates[i] = cands

        lidar_segments = self.build_regular_segments(lidar_data)
        image_segments = self.build_regular_segments(front_camera_data)
        overlap_ranges = self.compute_overlap_ranges(lidar_segments, image_segments, lidar_data, front_camera_data)
        if not overlap_ranges:
            overlap_ranges = [
                {"lidar_indices": list(range(n_lidar)), "image_indices": list(range(len(front_camera_data)))}
            ]

        final_matches = {}
        used_image_indices = set()

        for overlap in overlap_ranges:
            allowed_images = set(overlap["image_indices"])
            local_candidates = {}
            for l_idx in overlap["lidar_indices"]:
                if l_idx not in base_candidates:
                    continue
                filtered = [img_idx for img_idx in base_candidates[l_idx] if img_idx in allowed_images]
                if filtered:
                    local_candidates[l_idx] = filtered
            if not local_candidates:
                continue

            segments = self.split_segments_by_candidates(overlap["lidar_indices"], local_candidates, lidar_data)

            # 将大段拆分为小块，防止 Hungarian 代价矩阵 OOM
            _chunked = []
            for _seg in segments:
                if len(_seg) <= self.MAX_HUNGARIAN_CHUNK:
                    _chunked.append(_seg)
                else:
                    LOG.info("段长度 %d 超过阈值 %d，拆分为 %d 个子段",
                             len(_seg), self.MAX_HUNGARIAN_CHUNK,
                             (len(_seg) + self.MAX_HUNGARIAN_CHUNK - 1) // self.MAX_HUNGARIAN_CHUNK)
                    for _i in range(0, len(_seg), self.MAX_HUNGARIAN_CHUNK):
                        _chunked.append(_seg[_i:_i + self.MAX_HUNGARIAN_CHUNK])
            segments = _chunked

            for seg in segments:
                seg_len = len(seg)
                if not seg_len:
                    continue

                if seg_len == 1:
                    l_idx = seg[0]
                    best_img = self.select_start_frame_image(
                        l_idx, local_candidates, used_image_indices, lidar_data, front_camera_data
                    )
                    if best_img is None:
                        continue
                    final_matches[l_idx] = best_img
                    used_image_indices.add(best_img)
                    continue

                start_l_idx = seg[0]
                best_start_img = self.select_start_frame_image(
                    start_l_idx, local_candidates, used_image_indices, lidar_data, front_camera_data
                )
                if best_start_img is not None:
                    final_matches[start_l_idx] = best_start_img
                    used_image_indices.add(best_start_img)

                remaining_l_indices = seg[1:]
                if not remaining_l_indices:
                    continue

                candidate_images = set()
                for l_idx in remaining_l_indices:
                    for img_idx in local_candidates[l_idx]:
                        if img_idx not in used_image_indices:
                            candidate_images.add(img_idx)
                if not candidate_images:
                    continue

                candidate_list = sorted(list(candidate_images))
                cost_matrix = np.full((len(remaining_l_indices), len(candidate_list)), 1000.0)

                for r, l_idx in enumerate(remaining_l_indices):
                    l_ts = lidar_data[l_idx]["ts"]
                    l_cands = set(local_candidates[l_idx])
                    for c, img_idx in enumerate(candidate_list):
                        if img_idx in l_cands:
                            i_ts = front_camera_data[img_idx]["ts"]
                            diff = l_ts - i_ts
                            diff_to_ideal = abs(diff - self.IDEAL_OFFSET)
                            cost = diff_to_ideal
                            if diff > self.IDEAL_OFFSET + 0.050:
                                cost *= 1.1
                            if diff < 0:
                                cost *= 1.3
                            cost_matrix[r, c] = cost

                row_ind, col_ind = linear_sum_assignment(cost_matrix)
                for r, c in zip(row_ind, col_ind):
                    if cost_matrix[r, c] < 0.5:
                        l_real = remaining_l_indices[r]
                        i_real = candidate_list[c]
                        final_matches[l_real] = i_real
                        used_image_indices.add(i_real)

        LOG.info("Matched %d LiDAR frames with front camera", len(final_matches))
        return final_matches

    def match_other_camera(self, front_camera_matches, other_camera_data, front_camera_data):
        matches = {}
        other_ts_list = [x["ts"] for x in other_camera_data]

        for lidar_idx, front_img_idx in front_camera_matches.items():
            front_ts = front_camera_data[front_img_idx]["ts"]

            min_t = front_ts - self.OTHER_CAMERA_WINDOW
            max_t = front_ts + self.OTHER_CAMERA_WINDOW

            start_idx = bisect.bisect_left(other_ts_list, min_t)
            end_idx = bisect.bisect_right(other_ts_list, max_t)

            candidates = list(range(start_idx, end_idx))
            if not candidates:
                continue

            best_candidate = min(candidates, key=lambda x: abs(other_camera_data[x]["ts"] - front_ts))
            matches[lidar_idx] = best_candidate

        return matches

    def generate_framemap(self, output_path: str):
        sensors_used, topic_map = self.load_yaml_config()
        camera_config, lidar_config = self.parse_sensors_config(sensors_used, topic_map)

        if not camera_config or not lidar_config:
            raise ValueError("No camera or lidar configuration found")

        front_wide_key = None
        front_keys = ["front", "front_wide", "front_narrow"]
        for key in front_keys:
            if key in camera_config:
                front_wide_key = key
                break

        if not front_wide_key:
            raise ValueError("No front camera found in configuration")

        lidar_key = list(lidar_config.keys())[0]

        lidar_data = self.load_sensor_files(lidar_config[lidar_key], ".las")
        all_camera_data = {}
        for cam_name, cam_folder in camera_config.items():
            cam_data = self.load_sensor_files(cam_folder, ".jpg")
            all_camera_data[cam_name] = cam_data

        if not lidar_data or front_wide_key not in all_camera_data:
            raise ValueError("Failed to load LiDAR or front camera data")

        front_camera_data = all_camera_data[front_wide_key]
        lidar_front_matches = self.match_lidar_front_camera(lidar_data, front_camera_data)

        all_matches = {front_wide_key: lidar_front_matches}
        for cam_name, cam_data in all_camera_data.items():
            if cam_name == front_wide_key:
                continue
            matches = self.match_other_camera(lidar_front_matches, cam_data, front_camera_data)
            all_matches[cam_name] = matches

        sensor_topic_config = {"camera": {}, "lidar": {}}
        for cam_name, cam_folder in camera_config.items():
            sensor_topic_config["camera"][cam_name] = cam_folder
        for lidar_name, lidar_folder in lidar_config.items():
            sensor_topic_config["lidar"][lidar_name] = lidar_folder

        frames = []
        expected_interval = 0.100

        for i, lidar_info in enumerate(lidar_data):
            lidar_idx = i
            lidar_ts = lidar_info["ts"]

            if i > 0:
                prev_ts = lidar_data[i - 1]["ts"]
                interval = lidar_ts - prev_ts
                if interval > self.EXPECTED_INTERVAL_MAX:
                    missing_frames = int(round(interval / expected_interval)) - 1
                    for _ in range(missing_frames):
                        missing_frame = {
                            "frame_id": len(frames),
                            "timestamp": self.format_timestamp(None),
                            "frames_timestamp": {"camera": {}, "lidar": {}},
                        }
                        for cam_name in camera_config.keys():
                            missing_frame["frames_timestamp"]["camera"][cam_name] = self.format_timestamp(None)
                        missing_frame["frames_timestamp"]["lidar"][lidar_key] = self.format_timestamp(None)
                        frames.append(missing_frame)

            frame = {
                "frame_id": len(frames),
                "timestamp": self.format_timestamp(lidar_ts),
                "frames_timestamp": {"camera": {}, "lidar": {}},
            }

            for cam_name in camera_config.keys():
                if cam_name in all_matches and lidar_idx in all_matches[cam_name]:
                    cam_img_idx = all_matches[cam_name][lidar_idx]
                    cam_ts = all_camera_data[cam_name][cam_img_idx]["ts"]
                    frame["frames_timestamp"]["camera"][cam_name] = self.format_timestamp(cam_ts)
                else:
                    frame["frames_timestamp"]["camera"][cam_name] = self.format_timestamp(None)

            frame["frames_timestamp"]["lidar"][lidar_key] = self.format_timestamp(lidar_ts)
            frames.append(frame)

        framemap = {
            "metadata": {
                "version": "1.0.0",
                "frame_rate": 10.0,
                "frames_count": len(frames),
                "sensor_topic_config": sensor_topic_config,
            },
            "frames": frames,
        }

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(framemap, f, indent=4, ensure_ascii=False)


def find_parameter_folder(parameter_yaml_dir: Path) -> Path:
    if not parameter_yaml_dir.exists():
        raise FileNotFoundError(f"parameter_yaml not found: {parameter_yaml_dir}")
    subdirs = [p for p in parameter_yaml_dir.iterdir() if p.is_dir()]
    if not subdirs:
        raise FileNotFoundError(f"No parameter folder in {parameter_yaml_dir}")
    return subdirs[0]


def load_sensors_used(parameter_folder: Path) -> Dict[str, str]:
    sensors_used_path = parameter_folder / "sensors_used.yaml"
    if not sensors_used_path.exists():
        raise FileNotFoundError(f"sensors_used.yaml not found: {sensors_used_path}")
    with open(sensors_used_path, "r", encoding="utf-8") as f:
        sensors_used = yaml.safe_load(f) or {}
    sensors = sensors_used.get("sensors", {})
    mapping = {}
    for nickname, sensor_info in sensors.items():
        parts = str(sensor_info).split(",", 1)
        if not parts:
            continue
        device_id = parts[0].strip()
        mapping[nickname] = device_id
    return mapping


def find_extrinsic_file(extrinsic_dir: Path, lidar_id: str, camera_id: str) -> Optional[Path]:
    if not extrinsic_dir.exists():
        return None
    preferred = extrinsic_dir / f"{lidar_id}_to_{camera_id}.yaml"
    if preferred.exists():
        return preferred
    alt = extrinsic_dir / f"{camera_id}_to_{lidar_id}.yaml"
    if alt.exists():
        return alt
    pattern = f"*{lidar_id}*{camera_id}*.yaml"
    matches = sorted(extrinsic_dir.glob(pattern))
    return matches[0] if matches else None


def parse_timestamp_value(ts_value: Optional[str]) -> Optional[float]:
    if ts_value is None:
        return None
    if isinstance(ts_value, (int, float)):
        return float(ts_value)
    ts_str = str(ts_value).strip()
    if not ts_str or ts_str.lower() == "none":
        return None
    try:
        return float(ts_str)
    except ValueError:
        return None


def format_timestamp(ts_value: Optional[float]) -> Optional[str]:
    if ts_value is None:
        return None
    return f"{ts_value:.3f}"


def resolve_file_by_timestamp(folder: Path, ts_str: str, exts: Tuple[str, ...]) -> Optional[Path]:
    for ext in exts:
        candidate = folder / f"{ts_str}{ext}"
        if candidate.exists():
            return candidate
    patterns = [str(folder / f"{ts_str}*{ext}") for ext in exts]
    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            return Path(sorted(matches)[0])
    return None


@dataclass
class CameraParam:
    camera_name: str = ""
    data_dir: str = ""
    jpg_file_dir: str = ""
    out_file_dir: str = ""
    is_ok: bool = False
    jpg_file_name_list: List[str] = None
    timestamp_list: List[float] = None
    object2_camera_param: np.ndarray = None
    intrinsic: np.ndarray = None
    distortion: np.ndarray = None
    img_shape: Tuple[int, int] = None
    horizontal_fov_deg: Optional[float] = None
    vertical_fov_deg: Optional[float] = None
    is_image_undistorted: Optional[bool] = None


class PointCloudUndistortion:
    def __init__(
        self,
        pose_file,
        enable_pre_transform=False,
        pre_transform_file=None,
        enable_post_transform=False,
        post_transform_file=None,
    ):
        self.pose_file = pose_file
        self.poses = None

        # 去畸变前的坐标系转换（将点云转到pose坐标系）
        self.enable_pre_transform = enable_pre_transform
        self.pre_transform_file = pre_transform_file
        self.pre_transform_matrix = None

        # 去畸变后的坐标系转换（将点云转到目标坐标系）
        self.enable_post_transform = enable_post_transform
        self.post_transform_file = post_transform_file
        self.post_transform_matrix = None

    def load_transform_params(self):
        """加载去畸变前后的坐标系转换参数"""
        success = True

        if self.enable_pre_transform and self.pre_transform_file:
            try:
                with open(self.pre_transform_file, "r") as f:
                    transform_data = yaml.safe_load(f)
                self.pre_transform_matrix = np.array(transform_data["transformation"], dtype=np.float64)
                LOG.info("已加载去畸变前转换矩阵: %s", self.pre_transform_file)
            except Exception as exc:
                LOG.error("加载去畸变前转换参数失败 %s: %s", self.pre_transform_file, exc)
                success = False

        if self.enable_post_transform and self.post_transform_file:
            try:
                with open(self.post_transform_file, "r") as f:
                    transform_data = yaml.safe_load(f)
                self.post_transform_matrix = np.array(transform_data["transformation"], dtype=np.float64)
                LOG.info("已加载去畸变后转换矩阵: %s", self.post_transform_file)
            except Exception as exc:
                LOG.error("加载去畸变后转换参数失败 %s: %s", self.post_transform_file, exc)
                success = False

        return success

    def apply_pre_transform(self, points):
        """去畸变前转换：将点云从原始坐标系转到pose坐标系"""
        if not self.enable_pre_transform or self.pre_transform_matrix is None:
            return points
        points_homogeneous = np.hstack((points, np.ones((points.shape[0], 1))))
        transformed_points = (self.pre_transform_matrix @ points_homogeneous.T).T
        return transformed_points[:, :3]

    def apply_post_transform(self, points):
        """去畸变后转换：将点云从pose坐标系转到目标坐标系"""
        if not self.enable_post_transform or self.post_transform_matrix is None:
            return points
        points_homogeneous = np.hstack((points, np.ones((points.shape[0], 1))))
        transformed_points = (self.post_transform_matrix @ points_homogeneous.T).T
        return transformed_points[:, :3]

    def load_poses(self):
        LOG.info("加载pose文件: %s", self.pose_file)
        try:
            self.poses = pd.read_csv(
                self.pose_file,
                header=None,
                sep=",",
                names=["timestamp", "utm_x", "utm_y", "utm_z", "qx", "qy", "qz", "qw"],
                dtype={"timestamp": np.float64},
            )
        except Exception:
            self.poses = pd.read_csv(
                self.pose_file,
                header=None,
                sep=r"\s+",
                names=["timestamp", "utm_x", "utm_y", "utm_z", "qx", "qy", "qz", "qw"],
                dtype={"timestamp": np.float64},
            )
        self.poses = self.poses.sort_values("timestamp").reset_index(drop=True)
        LOG.info("加载了 %d 个pose数据", len(self.poses))

    def interpolate_pose(self, timestamp):
        if self.poses is None or len(self.poses) == 0:
            LOG.error("poses数据未加载或为空")
            return None, None
        if timestamp < self.poses["timestamp"].min() or timestamp > self.poses["timestamp"].max():
            LOG.warning("时间戳 %.6f 超出pose数据范围", timestamp)
            return None, None

        idx = np.searchsorted(np.array(self.poses["timestamp"].values), timestamp)
        if idx == 0:
            pose = self.poses.iloc[0]
            position = np.array([pose["utm_x"], pose["utm_y"], pose["utm_z"]])
            quaternion = np.array([pose["qx"], pose["qy"], pose["qz"], pose["qw"]])
            return position, quaternion
        if idx == len(self.poses):
            pose = self.poses.iloc[-1]
            position = np.array([pose["utm_x"], pose["utm_y"], pose["utm_z"]])
            quaternion = np.array([pose["qx"], pose["qy"], pose["qz"], pose["qw"]])
            return position, quaternion

        pose0 = self.poses.iloc[idx - 1]
        pose1 = self.poses.iloc[idx]
        t0 = pose0["timestamp"]
        t1 = pose1["timestamp"]
        weight = (timestamp - t0) / (t1 - t0)

        pos0 = np.array([pose0["utm_x"], pose0["utm_y"], pose0["utm_z"]])
        pos1 = np.array([pose1["utm_x"], pose1["utm_y"], pose1["utm_z"]])
        position = pos0 * (1 - weight) + pos1 * weight

        quat0 = np.array([pose0["qx"], pose0["qy"], pose0["qz"], pose0["qw"]])
        quat1 = np.array([pose1["qx"], pose1["qy"], pose1["qz"], pose1["qw"]])
        if np.dot(quat0, quat1) < 0:
            quat1 = -quat1
        rotations = R.from_quat([quat0, quat1])
        slerp = Slerp([0, 1], rotations)
        interpolated_rotation = slerp(weight)
        quaternion = interpolated_rotation.as_quat()
        return position, quaternion

    def batch_interpolate_poses(self, timestamps):
        unique_timestamps, inverse_indices = np.unique(timestamps, return_inverse=True)
        unique_positions = np.zeros((len(unique_timestamps), 3))
        unique_rotations = []
        LOG.info("批量插值 %d 个唯一时间戳", len(unique_timestamps))
        for i, ts in enumerate(unique_timestamps):
            pos, quat = self.interpolate_pose(ts)
            if pos is not None:
                unique_positions[i] = pos
                unique_rotations.append(R.from_quat(quat))
            else:
                unique_positions[i] = np.array([0, 0, 0])
                unique_rotations.append(R.identity())

        positions = unique_positions[inverse_indices]
        rotations = [unique_rotations[i] for i in inverse_indices]
        return positions, rotations

    def remove_distortion_single_frame(self, points, timestamps, time_start, time_end, target_timestamp):
        undistorted_points = points.copy()
        time_interval = time_end - time_start
        if time_interval <= 0:
            LOG.warning("时间间隔为0或负数，跳过去畸变")
            return undistorted_points

        target_pos, target_quat = self.interpolate_pose(target_timestamp)
        if target_pos is None:
            LOG.warning("无法获取目标时间戳位姿，跳过去畸变")
            return undistorted_points

        target_rotation = R.from_quat(target_quat)
        valid_time_mask = (timestamps >= time_start) & (timestamps <= time_end)
        valid_timestamps = timestamps[valid_time_mask]
        valid_points = points[valid_time_mask]
        if len(valid_timestamps) == 0:
            LOG.warning("没有有效时间范围内的点")
            return undistorted_points

        LOG.info("处理 %d 个点", len(valid_timestamps))
        point_positions, point_rotations = self.batch_interpolate_poses(valid_timestamps)
        LOG.info("开始向量化变换...")

        undistorted_valid_points = np.zeros_like(valid_points)
        chunk_size = 10000
        num_chunks = (len(valid_points) + chunk_size - 1) // chunk_size
        for chunk_idx in range(num_chunks):
            start_idx = chunk_idx * chunk_size
            end_idx = min((chunk_idx + 1) * chunk_size, len(valid_points))
            chunk_points = valid_points[start_idx:end_idx]
            chunk_positions = point_positions[start_idx:end_idx]
            chunk_rotations = point_rotations[start_idx:end_idx]
            for i in range(len(chunk_points)):
                point_rotation = chunk_rotations[i]
                global_point = point_rotation.apply(chunk_points[i]) + chunk_positions[i]
                undistorted_valid_points[start_idx + i] = target_rotation.inv().apply(global_point - target_pos)
            if num_chunks > 1:
                LOG.info("完成块 %d/%d", chunk_idx + 1, num_chunks)

        undistorted_points[valid_time_mask] = undistorted_valid_points
        return undistorted_points


class LasProjectToImagePipeline:
    def __init__(
        self,
        las_dir,
        jpg_dir,
        output_dir,
        intrinsic_file,
        extrinsic_file,
        intensity_min=0,
        intensity_max=255,
        max_distance=255,
        min_distance=0.0,
        start_timestamp=None,
        num_frames=None,
        z_rotation_degrees=0,
        enable_coordinate_transform=False,
        coordinate_transform_file=None,
        time_tolerance=0.1,
        enable_distance_coloring=True,
        enable_fov_filter=True,
        horizontal_fov_override=None,
        vertical_fov_override=None,
        image_is_undistorted: Optional[bool] = None,
        intensity_mode: str = "global",
        intensity_p_lo: float = 2.0,
        intensity_p_hi: float = 98.0,
    ):
        self.las_dir = las_dir
        self.jpg_dir = jpg_dir
        self.output_dir = output_dir
        self.intrinsic_file = intrinsic_file
        self.extrinsic_file = extrinsic_file
        self.intensity_min = intensity_min
        self.intensity_max = intensity_max
        self.max_distance = max_distance
        self.min_distance = min_distance
        self.camera_param = None
        self.start_timestamp = start_timestamp
        self.num_frames = num_frames
        self.z_rotation_degrees = z_rotation_degrees
        self.enable_distance_coloring = enable_distance_coloring
        self.enable_fov_filter = enable_fov_filter
        self.horizontal_fov_override = horizontal_fov_override
        self.vertical_fov_override = vertical_fov_override
        self.image_is_undistorted = image_is_undistorted
        self.intensity_mode = intensity_mode
        self.intensity_p_lo = intensity_p_lo
        self.intensity_p_hi = intensity_p_hi

        self.enable_coordinate_transform = enable_coordinate_transform
        self.coordinate_transform_file = coordinate_transform_file
        self.coordinate_transform_matrix = None

        angle_rad = np.radians(self.z_rotation_degrees)
        self.z_rotation_matrix = np.array(
            [
                [np.cos(angle_rad), -np.sin(angle_rad), 0, 0],
                [np.sin(angle_rad), np.cos(angle_rad), 0, 0],
                [0, 0, 1, 0],
                [0, 0, 0, 1],
            ]
        )

        self.semantic_labels = {0: "non_ground", 9: "ground", 20: "road_marking"}
        self.semantic_colors = {
            "non_ground": [200, 128, 128],
            "ground": [100, 200, 200],
            "road_marking": [0, 0, 255],
        }

    def is_fisheye_camera(self, camera_param=None):
        target_param = camera_param if camera_param is not None else self.camera_param
        if not target_param or not target_param.camera_name:
            return False
        name = target_param.camera_name.lower()
        return "_avm" in name or "fisheye" in name

    def load_coordinate_transform_params(self):
        if not self.enable_coordinate_transform or not self.coordinate_transform_file:
            return True
        try:
            with open(self.coordinate_transform_file, "r") as f:
                transform_data = yaml.safe_load(f)
            self.coordinate_transform_matrix = np.array(transform_data["transformation"], dtype=np.float64)
            return True
        except Exception as exc:
            LOG.error("加载坐标系转换参数失败 %s: %s", self.coordinate_transform_file, exc)
            return False

    def transform_point_cloud_coordinates(self, points):
        if not self.enable_coordinate_transform or self.coordinate_transform_matrix is None:
            return points
        points_homogeneous = np.hstack((points, np.ones((points.shape[0], 1))))
        transformed_points = (self.coordinate_transform_matrix @ points_homogeneous.T).T
        return transformed_points[:, :3]

    def determine_fov_angles(self, width, height, intrinsic, distortion, camera_param=None):
        image_undistorted = self.image_is_undistorted
        if image_undistorted is None:
            image_undistorted = self.is_probably_undistorted(distortion)
        is_fisheye = self.is_fisheye_camera(camera_param)
        if image_undistorted:
            horizontal_fov, vertical_fov = self.compute_pinhole_fov(width, height, intrinsic)
        else:
            horizontal_fov, vertical_fov = self.compute_distorted_fov(
                width, height, intrinsic, distortion, is_fisheye=is_fisheye
            )
        return horizontal_fov, vertical_fov, image_undistorted

    @staticmethod
    def is_probably_undistorted(distortion, eps=1e-6):
        if distortion is None:
            return True
        dist_array = np.asarray(distortion, dtype=np.float64)
        return np.all(np.abs(dist_array) < eps)

    @staticmethod
    def compute_pinhole_fov(width, height, intrinsic):
        if intrinsic is None:
            return None, None
        fx = float(intrinsic[0, 0]) if intrinsic[0, 0] != 0 else 0.0
        fy = float(intrinsic[1, 1]) if intrinsic[1, 1] != 0 else 0.0
        if fx == 0 or fy == 0:
            return None, None
        horizontal_fov = 2 * np.degrees(np.arctan(width / (2 * fx)))
        vertical_fov = 2 * np.degrees(np.arctan(height / (2 * fy)))
        return horizontal_fov, vertical_fov

    def compute_distorted_fov(self, width, height, intrinsic, distortion, is_fisheye=False, sample_points=200):
        if intrinsic is None or distortion is None:
            return self.compute_pinhole_fov(width, height, intrinsic)
        dist_array = np.asarray(distortion, dtype=np.float64).reshape(-1)
        if dist_array.size == 0:
            return self.compute_pinhole_fov(width, height, intrinsic)
        horizontal = self._estimate_axis_fov(
            width, height, intrinsic, dist_array, axis="horizontal", is_fisheye=is_fisheye, sample_points=sample_points
        )
        vertical = self._estimate_axis_fov(
            width, height, intrinsic, dist_array, axis="vertical", is_fisheye=is_fisheye, sample_points=sample_points
        )
        return horizontal, vertical

    def _estimate_axis_fov(self, width, height, intrinsic, distortion, axis="horizontal", is_fisheye=False, sample_points=200):
        if axis == "horizontal":
            coords = np.linspace(0, width - 1, sample_points)
            pts = np.stack([coords, np.full_like(coords, height / 2.0)], axis=1)
        else:
            coords = np.linspace(0, height - 1, sample_points)
            pts = np.stack([np.full_like(coords, width / 2.0), coords], axis=1)

        undistorted = self._undistort_points(pts, intrinsic, distortion, is_fisheye=is_fisheye)
        if undistorted is None or undistorted.size == 0:
            return None
        coords = undistorted[:, 0] if axis == "horizontal" else undistorted[:, 1]
        max_abs = np.max(np.abs(coords))
        return 2 * np.degrees(np.arctan(max_abs))

    def _undistort_points(self, pts, intrinsic, distortion, is_fisheye=False):
        if intrinsic is None or distortion is None:
            return None
        try:
            K = intrinsic.astype(np.float64)
            if is_fisheye:
                dist = np.asarray(distortion[:4], dtype=np.float64)
                undist = cv2.fisheye.undistortPoints(pts.reshape(-1, 1, 2), K, dist)
            else:
                dist = np.asarray(distortion, dtype=np.float64).reshape(-1)
                undist = cv2.undistortPoints(pts.reshape(-1, 1, 2), K, dist)
            return undist.reshape(-1, 2)
        except Exception as err:
            LOG.warning("计算畸变FOV时undistort失败: %s", err)
            return None

    def read_las_file_optimized(self, filename):
        try:
            las = laspy.read(filename)
            points_xyz = np.vstack((las.x, las.y, las.z)).T
            points_intensity = las.intensity
            return points_xyz, points_intensity
        except Exception as exc:
            LOG.error("读取LAS文件 %s 时出错: %s", filename, exc)
            return None, None

    def generate_hsv_colors(self, distances, intensities, intensity_lo=None, intensity_hi=None):
        if self.enable_distance_coloring:
            # 基于深度映射颜色，按深度从近到远表现为：绿色 -> 黄色 -> 红色
            min_dist, max_dist = 0, self.max_distance
            norm_distances = np.clip(distances, min_dist, max_dist)
            # 色调映射：120(绿) -> 0(红)，随深度增加
            hue = 120 - (norm_distances - min_dist) / (max_dist - min_dist) * 120
            # 饱和度最大，明度固定高亮
            saturation = np.full_like(hue, 255)
            value = np.full_like(hue, 220)  # 固定高亮显示
        else:
            # 基于强度映射颜色，按强度从低到高表现为：蓝色 -> 绿色 -> 黄色 -> 红色
            if intensity_lo is not None and intensity_hi is not None:
                min_intensity, max_intensity = intensity_lo, intensity_hi
            else:
                min_intensity, max_intensity = self.intensity_min, self.intensity_max
            norm_intensities = np.clip(intensities, min_intensity, max_intensity)
            # 色调映射：240(蓝) -> 0(红)，随强度增加
            hue = 240 - (norm_intensities - min_intensity) / (max_intensity - min_intensity) * 240
            # 饱和度最大，明度固定高亮
            saturation = np.full_like(hue, 255)
            value = np.full_like(hue, 220)  # 固定高亮显示

        hsv = np.stack([hue, saturation, value], axis=-1).astype(np.uint8)
        hsv_image = hsv.reshape(-1, 1, 3)
        bgr_image = cv2.cvtColor(hsv_image, cv2.COLOR_HSV2BGR)
        return bgr_image.reshape(-1, 3)

    def compute_fov_mask(self, points_camera):
        mask = np.ones(points_camera.shape[0], dtype=bool)
        horizontal_fov = self.horizontal_fov_override
        vertical_fov = self.vertical_fov_override
        if horizontal_fov is None and self.camera_param:
            horizontal_fov = getattr(self.camera_param, "horizontal_fov_deg", None)
        if vertical_fov is None and self.camera_param:
            vertical_fov = getattr(self.camera_param, "vertical_fov_deg", None)

        forward_mask = points_camera[:, 2] > 0
        mask &= forward_mask

        if horizontal_fov is not None:
            half_horizontal = horizontal_fov / 2.0
            horizontal_angles = np.degrees(np.arctan2(points_camera[:, 0], points_camera[:, 2]))
            mask &= (horizontal_angles >= -half_horizontal) & (horizontal_angles <= half_horizontal)

        if vertical_fov is not None:
            half_vertical = vertical_fov / 2.0
            vertical_angles = np.degrees(np.arctan2(points_camera[:, 1], points_camera[:, 2]))
            mask &= (vertical_angles >= -half_vertical) & (vertical_angles <= half_vertical)

        return mask

    def load_transformation_matrix(self, camera_param):
        try:
            with open(self.extrinsic_file, "r") as f:
                extrinsic_data = yaml.safe_load(f)
            transformation = np.eye(4)
            if "transformation" in extrinsic_data:
                trans_data = extrinsic_data["transformation"]
                if len(trans_data) == 4 and all(len(row) == 4 for row in trans_data):
                    for i in range(4):
                        for j in range(4):
                            transformation[i, j] = float(trans_data[i][j])
                else:
                    for i, row in enumerate(trans_data):
                        values = re.findall(r"-?\d+\.\d+e?[+-]?\d*", str(row))
                        if len(values) == 4:
                            for j, val in enumerate(values):
                                transformation[i, j] = float(val)
            camera_param.object2_camera_param = transformation
            return True
        except Exception as exc:
            LOG.error("加载变换矩阵时出错: %s", exc)
            return False

    def load_camera_intrinsics(self, camera_param):
        try:
            with open(self.intrinsic_file, "r") as f:
                intrinsic_data = yaml.safe_load(f)
            intrinsic = np.eye(3)
            if "intrinsic" in intrinsic_data:
                intrinsic_list = intrinsic_data["intrinsic"]
                if isinstance(intrinsic_list, list) and len(intrinsic_list) == 3:
                    for i, row in enumerate(intrinsic_list):
                        if isinstance(row, list) and len(row) == 3:
                            for j, val in enumerate(row):
                                intrinsic[i, j] = float(val)
            distortion = np.zeros(14)
            if "distortion" in intrinsic_data:
                dist_list = intrinsic_data["distortion"]
                if isinstance(dist_list, list):
                    for i, val in enumerate(dist_list[:14]):
                        if val is not None:
                            distortion[i] = float(val)
            width = intrinsic_data.get("width", 3840)
            height = intrinsic_data.get("height", 2160)

            horizontal_fov_auto = None
            horizontal_fov_final = None
            vertical_fov = None
            image_undistorted_flag = None
            if width and height:
                horizontal_fov_auto, vertical_fov, image_undistorted_flag = self.determine_fov_angles(
                    width, height, intrinsic, distortion, camera_param
                )

            override_used = False
            if self.horizontal_fov_override is not None:
                horizontal_fov_final = self.horizontal_fov_override
                override_used = True
            else:
                horizontal_fov_final = horizontal_fov_auto

            camera_param.intrinsic = intrinsic
            camera_param.distortion = distortion
            camera_param.img_shape = (width, height)
            camera_param.horizontal_fov_deg = horizontal_fov_final
            camera_param.vertical_fov_deg = vertical_fov
            camera_param.is_image_undistorted = image_undistorted_flag

            if override_used:
                LOG.info("使用预设水平FOV: %.2f°", self.horizontal_fov_override)
            return True
        except Exception as exc:
            LOG.error("加载相机内参时出错: %s", exc)
            return False

    def load_camera_config(self):
        camera_param = CameraParam()
        camera_param.jpg_file_dir = self.jpg_dir
        camera_param.out_file_dir = self.output_dir
        camera_param.camera_name = os.path.basename(self.jpg_dir)
        if not self.load_camera_intrinsics(camera_param):
            return None
        if not self.load_transformation_matrix(camera_param):
            return None
        camera_param.is_ok = True
        return camera_param

    def project_points_with_depth_intensity(self, points, intensities, image):
        intrinsic_mat = np.ascontiguousarray(self.camera_param.intrinsic, dtype=np.float64)
        distortion = np.ascontiguousarray(self.camera_param.distortion, dtype=np.float64)
        r_eigen = self.camera_param.object2_camera_param[:3, :3].astype(np.float64, copy=False)
        r_vec, _ = cv2.Rodrigues(r_eigen)
        r_vec = np.ascontiguousarray(r_vec.reshape(3, 1), dtype=np.float64)
        t_vec = np.ascontiguousarray(self.camera_param.object2_camera_param[:3, 3].reshape(3, 1), dtype=np.float64)
        is_fisheye = self.is_fisheye_camera()

        points_homogeneous = np.hstack((points, np.ones((points.shape[0], 1))))
        points_camera = points_homogeneous @ self.camera_param.object2_camera_param.T
        distances = np.linalg.norm(points_camera[:, :3], axis=1)

        if is_fisheye:
            points_fisheye = points.astype(np.float32).reshape(-1, 1, 3)
            projected_points, _ = cv2.fisheye.projectPoints(points_fisheye, r_vec, t_vec, intrinsic_mat, distortion)
        else:
            projected_points, _ = cv2.projectPoints(points.astype(np.float32), r_vec, t_vec, intrinsic_mat, distortion)

        projected_points = projected_points.reshape(-1, 2)
        img_height, img_width = image.shape[:2]
        valid_indices = (
            (projected_points[:, 0] >= 0)
            & (projected_points[:, 0] < img_width)
            & (projected_points[:, 1] >= 0)
            & (projected_points[:, 1] < img_height)
        )
        valid_points = projected_points[valid_indices].astype(int)
        valid_distances = distances[valid_indices]
        valid_intensities = intensities[valid_indices]

        # 根据强度模式动态计算强度归一化范围（参考 reproject_lidar_maxieye.py）
        intensity_lo = None
        intensity_hi = None
        if not self.enable_distance_coloring and self.intensity_mode != 'global':
            valid_cam_z = points_camera[valid_indices, 2]
            if self.intensity_mode == 'road_mask':
                img_height = image.shape[0]
                road_sel = (
                    (valid_points[:, 1] > img_height * 0.45)
                    & (valid_cam_z > 1.0)
                    & (valid_cam_z < 80.0)
                )
                ref_intensities = valid_intensities[road_sel] if np.any(road_sel) else valid_intensities
                intensity_lo = float(np.percentile(ref_intensities, self.intensity_p_lo))
                intensity_hi = float(np.percentile(ref_intensities, self.intensity_p_hi))
            elif self.intensity_mode == 'percentile':
                intensity_lo = float(np.percentile(valid_intensities, self.intensity_p_lo))
                intensity_hi = float(np.percentile(valid_intensities, self.intensity_p_hi))

        colors = self.generate_hsv_colors(valid_distances, valid_intensities, intensity_lo, intensity_hi)
        for i in range(len(valid_points)):
            x, y = valid_points[i]
            color = colors[i].tolist()
            radius = 2 if valid_distances[i] < 20 else 1
            cv2.circle(image, (x, y), radius, color, -1)

    def process_point_cloud(self, las_file, jpg_file, timestamp=None):
        jpg_image = cv2.imread(jpg_file)
        if jpg_image is None:
            LOG.error("读取JPG文件失败: %s", jpg_file)
            return

        points_xyz, points_intensity = self.read_las_file_optimized(las_file)
        if points_xyz is None or len(points_xyz) == 0:
            LOG.error("读取LAS文件失败或无有效点: %s", las_file)
            return

        if self.enable_coordinate_transform:
            points_xyz = self.transform_point_cloud_coordinates(points_xyz)

        if self.z_rotation_degrees != 0:
            points_homogeneous = np.hstack((points_xyz, np.ones((points_xyz.shape[0], 1))))
            points_homogeneous = points_homogeneous @ self.z_rotation_matrix.T
            points_xyz = points_homogeneous[:, :3]

        points_homogeneous = np.hstack((points_xyz, np.ones((points_xyz.shape[0], 1))))
        points_camera = points_homogeneous @ self.camera_param.object2_camera_param.T

        # 显示全量点云，不按距离过滤
        valid_indices = np.ones(len(points_camera), dtype=bool)
        if self.enable_fov_filter:
            fov_mask = self.compute_fov_mask(points_camera)
            valid_indices &= fov_mask

        if np.sum(valid_indices) == 0:
            LOG.warning("没有有效的点云可投影: %s", las_file)
            return

        valid_points = points_xyz[valid_indices]
        valid_intensities = points_intensity[valid_indices]

        self.project_points_with_depth_intensity(valid_points, valid_intensities, jpg_image)

        if timestamp:
            basename = f"{timestamp:.3f}.jpg"
        else:
            basename = os.path.basename(jpg_file)

        output_path = os.path.join(self.output_dir, basename)
        os.makedirs(self.output_dir, exist_ok=True)
        ok = cv2.imwrite(output_path, jpg_image)
        if ok:
            LOG.info("已保存投影结果: %s", output_path)
        else:
            LOG.error("保存投影结果失败: %s", output_path)
        
        return jpg_image

    def process_point_cloud_from_data(self, points_xyz, points_intensity, jpg_file, timestamp=None):
        """使用预加载的点云数据进行投影，避免重复读取LAS文件。"""
        jpg_image = cv2.imread(jpg_file)
        if jpg_image is None:
            LOG.error("读取JPG文件失败: %s", jpg_file)
            return

        if points_xyz is None or len(points_xyz) == 0:
            LOG.error("点云数据为空")
            return

        points_xyz = points_xyz.copy()

        if self.enable_coordinate_transform:
            points_xyz = self.transform_point_cloud_coordinates(points_xyz)

        if self.z_rotation_degrees != 0:
            points_homogeneous = np.hstack((points_xyz, np.ones((points_xyz.shape[0], 1))))
            points_homogeneous = points_homogeneous @ self.z_rotation_matrix.T
            points_xyz = points_homogeneous[:, :3]

        points_homogeneous = np.hstack((points_xyz, np.ones((points_xyz.shape[0], 1))))
        points_camera = points_homogeneous @ self.camera_param.object2_camera_param.T

        valid_indices = np.ones(len(points_camera), dtype=bool)
        if self.enable_fov_filter:
            fov_mask = self.compute_fov_mask(points_camera)
            valid_indices &= fov_mask

        if np.sum(valid_indices) == 0:
            LOG.warning("没有有效的点云可投影")
            return

        valid_points = points_xyz[valid_indices]
        valid_intensities = points_intensity[valid_indices]

        self.project_points_with_depth_intensity(valid_points, valid_intensities, jpg_image)

        if timestamp:
            basename = f"{timestamp:.3f}.jpg"
        else:
            basename = os.path.basename(jpg_file)

        output_path = os.path.join(self.output_dir, basename)
        os.makedirs(self.output_dir, exist_ok=True)
        ok = cv2.imwrite(output_path, jpg_image)
        if ok:
            LOG.info("已保存投影结果: %s", output_path)
        else:
            LOG.error("保存投影结果失败: %s", output_path)

        return jpg_image


# 画布中相机ROI位置映射表, 格式: "相机位置": (画布左上角点x, 画布左上角点y, 画布右下角点x, 画布右下角点y)
CANVAS_CONFIG = {
    # 环视 - 左侧列
    "front_avm": (0, 0, 1000, 800),
    "left_avm": (0, 800, 1000, 1600),
    "right_avm": (0, 1600, 1000, 2400),
    "rear_avm": (0, 2400, 1000, 3200),

    # 前侧视 + 前视窄角 - 顶部行
    "left_front": (1000, 0, 2280, 720),
    "front_narrow": (2280, 0, 3560, 720),
    "right_front": (3560, 0, 4840, 720),

    # 前视广角 - 中间行 (占据主要视野)
    "front_wide": (1000, 720, 4840, 2480),
    "front": (1000, 720, 4840, 2480),  # 兼容 front 命名

    # 后侧视 + 后视 - 底部行
    "left_back": (1000, 2480, 2280, 3200),
    "back": (2280, 2480, 3560, 3200),
    "right_back": (3560, 2480, 4840, 3200),
}


def merge_camera_views(frame_images: Dict[str, np.ndarray], output_path: Path):
    """
    将多路相机的图像合并成一张大图。
    参考布局：
    左侧：4个AVM (1000宽)
    右侧上：左前、前窄、右前
    右侧中：前广
    右侧下：左后、后、右后
    总尺寸：4840 x 3200
    """
    total_width = 4840
    total_height = 3200
    canvas = np.zeros((total_height, total_width, 3), dtype=np.uint8)

    for cam_name, img in frame_images.items():
        # 尝试匹配配置键
        cfg_key = cam_name
        # 处理可能的命名差异
        if cfg_key not in CANVAS_CONFIG:
            name_lower = cfg_key.lower()
            # HS5/GND 这类双前视命名：front + front7。原逻辑会把 front7 也映射到 front_wide，覆盖 front。
            # 这里显式把 front7/camera-7 放到 front_narrow 区域，让 merged 中两个相机都可见。
            if name_lower in {"front7", "front_7", "camera7", "camera_7"} or "camera-7" in name_lower:
                cfg_key = "front_narrow"
            # 先处理side_*命名模式（优先级最高，避免被front/rear规则误匹配）
            elif "side_left_front" in cfg_key or "left_front" in cfg_key:
                cfg_key = "left_front"
            elif "side_right_front" in cfg_key or "right_front" in cfg_key:
                cfg_key = "right_front"
            elif "side_left_rear" in cfg_key or ("side_left_back" in cfg_key) or "left_back" in cfg_key or "left_rear" in cfg_key:
                cfg_key = "left_back"
            elif "side_right_rear" in cfg_key or ("side_right_back" in cfg_key) or "right_back" in cfg_key or "right_rear" in cfg_key:
                cfg_key = "right_back"
            # 再处理front/rear命名（front_120等 -> front_wide）
            elif "front" in cfg_key and "narrow" not in cfg_key and "avm" not in cfg_key:
                cfg_key = "front_wide"
            elif "rear" in cfg_key and "avm" not in cfg_key: # rear -> back
                cfg_key = "back"

        if cfg_key not in CANVAS_CONFIG:
            LOG.warning("No canvas layout for camera: %s", cam_name)
            continue
            
        x1, y1, x2, y2 = CANVAS_CONFIG[cfg_key]
        target_w = x2 - x1
        target_h = y2 - y1
        
        if target_w <= 0 or target_h <= 0:
            continue
            
        # Resize image to fit target area
        resized_img = cv2.resize(img, (target_w, target_h))
        canvas[y1:y2, x1:x2] = resized_img
        
        # Draw border
        cv2.rectangle(canvas, (x1, y1), (x2, y2), (255, 255, 255), 2)
        # Put text label
        cv2.putText(canvas, cam_name, (x1 + 10, y1 + 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)

    # Save merged image
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(output_path), canvas)
    LOG.info("Saved merged view: %s", output_path)


class FrameAligner:
    def __init__(
        self,
        pose_file: Path,
        enable_pre_transform: bool = False,
        pre_transform_file: Optional[Path] = None,
        enable_post_transform: bool = False,
        post_transform_file: Optional[Path] = None,
    ):
        self.processor = PointCloudUndistortion(
            pose_file=str(pose_file),
            enable_pre_transform=enable_pre_transform,
            pre_transform_file=str(pre_transform_file) if pre_transform_file else None,
            enable_post_transform=enable_post_transform,
            post_transform_file=str(post_transform_file) if post_transform_file else None,
        )
        self.processor.load_poses()
        if enable_pre_transform or enable_post_transform:
            if not self.processor.load_transform_params():
                raise RuntimeError("Failed to load coordinate transform parameters")

    def align_las_to_timestamp(self, las_file: Path, target_timestamp: float, output_path: Path) -> bool:
        LOG.info("Aligning %s to %.3f", las_file, target_timestamp)
        try:
            las_data = laspy.read(str(las_file))
            points = np.column_stack([np.array(las_data.x), np.array(las_data.y), np.array(las_data.z)])

            if hasattr(las_data, "gps_time"):
                timestamps = np.array(las_data.gps_time)
            elif hasattr(las_data, "time"):
                timestamps = np.array(las_data.time)
            else:
                LOG.error("No timestamp field in %s", las_file)
                return False

            valid_mask = ~np.isnan(timestamps) & (timestamps > 0)
            if not np.any(valid_mask):
                LOG.error("No valid timestamps in %s", las_file)
                return False

            points = points[valid_mask]
            timestamps = timestamps[valid_mask]

            time_start = float(np.min(timestamps))
            time_end = float(np.max(timestamps))

            if self.processor.poses is None or len(self.processor.poses) == 0:
                LOG.error("Pose data not loaded")
                return False

            pose_time_start = self.processor.poses["timestamp"].min()
            pose_time_end = self.processor.poses["timestamp"].max()
            if target_timestamp < pose_time_start or target_timestamp > pose_time_end:
                LOG.warning(
                    "Target timestamp %.6f out of pose range [%.6f, %.6f]",
                    target_timestamp,
                    pose_time_start,
                    pose_time_end,
                )
                return False

            points_in_pose_range = (timestamps >= pose_time_start) & (timestamps <= pose_time_end)
            if not np.any(points_in_pose_range):
                LOG.warning("No points in pose range for %s", las_file)
                return False

            # 阶段1：去畸变前转换（将点云从原始坐标系转到pose坐标系）
            if self.processor.enable_pre_transform:
                LOG.info("应用去畸变前坐标系转换")
                points = self.processor.apply_pre_transform(points)

            # 阶段2：去畸变（在pose坐标系下进行）
            undistorted_points = self.processor.remove_distortion_single_frame(
                points, timestamps, time_start, time_end, target_timestamp
            )

            # 阶段3：去畸变后转换（将点云从pose坐标系转到目标坐标系）
            if self.processor.enable_post_transform:
                LOG.info("应用去畸变后坐标系转换")
                undistorted_points = self.processor.apply_post_transform(undistorted_points)

            try:
                create_func = getattr(laspy, "create", None)
                if create_func:
                    import inspect

                    sig = inspect.signature(create_func)
                    if "like" in sig.parameters:
                        las_out = create_func(like=las_data)
                    else:
                        raise TypeError("like parameter not supported")
                else:
                    raise TypeError("create function not found")
            except (TypeError, AttributeError):
                las_out = laspy.create(
                    point_format=las_data.header.point_format,
                    file_version=las_data.header.version,
                )
                las_out.header.scale = las_data.header.scale
                las_out.header.offset = las_data.header.offset

            las_out.x = undistorted_points[:, 0]
            las_out.y = undistorted_points[:, 1]
            las_out.z = undistorted_points[:, 2]

            if hasattr(las_data, "intensity") and hasattr(las_out, "intensity"):
                las_out.intensity = np.array(las_data.intensity)[valid_mask]
            if hasattr(las_data, "return_number") and hasattr(las_out, "return_number"):
                las_out.return_number = np.array(las_data.return_number)[valid_mask]
            if hasattr(las_data, "number_of_returns") and hasattr(las_out, "number_of_returns"):
                las_out.number_of_returns = np.array(las_data.number_of_returns)[valid_mask]
            if hasattr(las_data, "classification") and hasattr(las_out, "classification"):
                las_out.classification = np.array(las_data.classification)[valid_mask]

            unified_time = np.full(len(undistorted_points), target_timestamp)
            if hasattr(las_out, "gps_time"):
                las_out.gps_time = unified_time
            elif hasattr(las_out, "time"):
                las_out.time = unified_time

            output_path.parent.mkdir(parents=True, exist_ok=True)
            las_out.write(str(output_path))
            return True
        except Exception as exc:
            LOG.exception("Failed to align %s: %s", las_file, exc)
            return False

    def align_las_to_timestamp_inmem(self, las_file: Path, target_timestamp: float):
        """对原始点云做运动补偿，去畸变到 target_timestamp，直接返回内存中的点（不写盘）。
        返回 (undistorted_xyz: Nx3 float, intensity: N) 或 (None, None)。
        与 align_las_to_timestamp 的去畸变逻辑保持完全一致，仅去掉写/读 LAS 步骤。
        """
        try:
            las_data = laspy.read(str(las_file))
            points = np.column_stack([np.array(las_data.x), np.array(las_data.y), np.array(las_data.z)])

            if hasattr(las_data, "gps_time"):
                timestamps = np.array(las_data.gps_time)
            elif hasattr(las_data, "time"):
                timestamps = np.array(las_data.time)
            else:
                LOG.error("No timestamp field in %s", las_file)
                return None, None

            valid_mask = ~np.isnan(timestamps) & (timestamps > 0)
            if not np.any(valid_mask):
                LOG.error("No valid timestamps in %s", las_file)
                return None, None

            points = points[valid_mask]
            timestamps = timestamps[valid_mask]
            if hasattr(las_data, "intensity"):
                intensity = np.array(las_data.intensity)[valid_mask]
            else:
                intensity = np.zeros(len(points))

            time_start = float(np.min(timestamps))
            time_end = float(np.max(timestamps))

            if self.processor.poses is None or len(self.processor.poses) == 0:
                LOG.error("Pose data not loaded")
                return None, None

            pose_time_start = self.processor.poses["timestamp"].min()
            pose_time_end = self.processor.poses["timestamp"].max()
            if target_timestamp < pose_time_start or target_timestamp > pose_time_end:
                LOG.warning(
                    "Target timestamp %.6f out of pose range [%.6f, %.6f]",
                    target_timestamp,
                    pose_time_start,
                    pose_time_end,
                )
                return None, None

            points_in_pose_range = (timestamps >= pose_time_start) & (timestamps <= pose_time_end)
            if not np.any(points_in_pose_range):
                LOG.warning("No points in pose range for %s", las_file)
                return None, None

            # 阶段1：去畸变前转换（将点云从原始坐标系转到pose坐标系）
            if self.processor.enable_pre_transform:
                points = self.processor.apply_pre_transform(points)

            # 阶段2：去畸变（在pose坐标系下进行）
            undistorted_points = self.processor.remove_distortion_single_frame(
                points, timestamps, time_start, time_end, target_timestamp
            )

            # 阶段3：去畸变后转换（将点云从pose坐标系转到目标坐标系）
            if self.processor.enable_post_transform:
                undistorted_points = self.processor.apply_post_transform(undistorted_points)

            return undistorted_points, intensity
        except Exception as exc:
            LOG.exception("Failed to align(inmem) %s: %s", las_file, exc)
            return None, None


def build_projector(
    camera_name: str,
    lidar_dir: Path,
    camera_dir: Path,
    output_dir: Path,
    intrinsic_file: Path,
    extrinsic_file: Path,
    config: CameraProjectionConfig,
    projection_common: Dict,
) -> Optional[LasProjectToImagePipeline]:
    if not intrinsic_file.exists():
        LOG.error("Intrinsic file not found for %s: %s", camera_name, intrinsic_file)
        return None
    if not extrinsic_file.exists():
        LOG.error("Extrinsic file not found for %s: %s", camera_name, extrinsic_file)
        return None

    pipeline = LasProjectToImagePipeline(
        las_dir=str(lidar_dir),
        jpg_dir=str(camera_dir),
        output_dir=str(output_dir),
        intrinsic_file=str(intrinsic_file),
        extrinsic_file=str(extrinsic_file),
        image_is_undistorted=config.image_is_undistorted,
        horizontal_fov_override=config.horizontal_fov_override,
        vertical_fov_override=config.vertical_fov_override,
        **projection_common,
    )

    if projection_common.get("enable_coordinate_transform"):
        if not pipeline.load_coordinate_transform_params():
            LOG.error("Failed to load projection coordinate transform for %s", camera_name)
            return None

    pipeline.camera_param = pipeline.load_camera_config()
    if not pipeline.camera_param:
        LOG.error("Failed to load camera config for %s", camera_name)
        return None

    camera_param_name = camera_name
    if config.force_fisheye and "fisheye" not in camera_param_name.lower() and "_avm" not in camera_param_name.lower():
        camera_param_name = f"{camera_param_name}_fisheye"
    pipeline.camera_param.camera_name = camera_param_name
    return pipeline


def parse_frame_ids(raw) -> Optional[List[int]]:
    if not raw:
        return None
    if isinstance(raw, (list, tuple)):
        return [int(x) for x in raw]
    if isinstance(raw, int):
        return [raw]
    if isinstance(raw, float):
        return [int(raw)]
    frame_ids = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        frame_ids.append(int(part))
    return frame_ids if frame_ids else None


def build_camera_config_from_name(
    camera_name: str,
    fisheye_name_keywords: List[str],
    narrow_keyword: str,
    default_pinhole_distorted: bool,
    manual_overrides: Optional[Dict[str, CameraProjectionConfig]] = None,
) -> CameraProjectionConfig:
    if manual_overrides and camera_name in manual_overrides:
        return manual_overrides[camera_name]
    name_lower = camera_name.lower()
    if any(keyword in name_lower for keyword in fisheye_name_keywords):
        return CameraProjectionConfig(image_is_undistorted=False, force_fisheye=True)
    if narrow_keyword in name_lower:
        return CameraProjectionConfig(image_is_undistorted=True, force_fisheye=False)
    image_is_undistorted = None if default_pinhole_distorted is None else (not default_pinhole_distorted)
    return CameraProjectionConfig(image_is_undistorted=image_is_undistorted, force_fisheye=False)


def select_frames(frames: List[Dict], frame_ids: Optional[List[int]], start: Optional[int], end: Optional[int], step: int):
    if frame_ids is not None:
        return [frame for frame in frames if frame.get("frame_id") in set(frame_ids)]
    if start is None and end is None:
        return frames[::step]
    start = start or 0
    end = end if end is not None else len(frames) - 1
    return [
        frame
        for frame in frames
        if start <= frame.get("frame_id", -1) <= end and (frame.get("frame_id", 0) - start) % step == 0
    ]



def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == '':
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'y', 'on'}


def env_int_optional(name: str, default: Optional[int]) -> Optional[int]:
    raw = os.environ.get(name)
    if raw is None or raw == '' or raw.strip().lower() in {'none', 'null'}:
        return default
    return int(raw)


def env_float_optional(name: str, default: Optional[float]) -> Optional[float]:
    raw = os.environ.get(name)
    if raw is None or raw == '' or raw.strip().lower() in {'none', 'null'}:
        return default
    return float(raw)


def env_str_optional(name: str, default: Optional[str]) -> Optional[str]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = raw.strip()
    return None if raw == '' or raw.lower() in {'none', 'null'} else raw


def count_timestamped_files(folder: Path, exts: Tuple[str, ...]) -> int:
    if not folder.exists():
        return 0
    total = 0
    for ext in exts:
        total += len(list(folder.glob(f'*{ext}')))
    return total


def write_json_report(path: Path, payload: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def run_framemap_alignment_projection():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    # ========= Config Section: 基础路径 =========
    # data_root: 数据集根目录（包含 parameter_yaml 与传感器文件夹）
    # pose_file: 位姿文件（坐标系需与点云一致，或通过对齐坐标系转换修正）
    # output_root: 输出根目录（会在此下生成 aligned 与 projection）
    data_root = DATA_ROOT.resolve()
    pose_file = POSE_FILE.resolve()
    output_root = OUTPUT_ROOT.resolve()

    # ========= Config Section: FrameMap 生成 =========
    # framemap_path: framemap.json 输出位置
    # generate_framemap_if_missing: framemap 不存在时才生成
    # force_regenerate_framemap: 强制重新生成 framemap（优先级最高）
    framemap_path = FRAMEMAP_PATH.resolve()
    generate_framemap_if_missing = GENERATE_FRAMEMAP_IF_MISSING
    force_regenerate_framemap = FORCE_REGENERATE_FRAMEMAP

    # ========= Config Section: 坐标系/外参选择 =========
    # lidar_extrinsic_id_override: 点云若已转到 body 等坐标系，填对应 ID（如 "body-2"）
    # lidar_folder_override: 点云目录与 framemap 中不一致时手动指定
    # lidar_extrinsic_id_override: 点云已转到body等坐标系时，指定对应ID（如 "body-2"）
    # 若为 None，则使用 framemap 的 lidar 昵称 + sensors_used.yaml 的映射
    lidar_extrinsic_id_override = LIDAR_EXTRINSIC_ID_OVERRIDE    ##投影时，用什么to_world投影
    # lidar_folder_override: 点云存放路径不在 framemap 指定文件夹时，手动指定
    lidar_folder_override = LIDAR_FOLDER_OVERRIDE

    # ========= Config Section: 对齐坐标系转换 =========
    # 完整的坐标系转换流程：
    # 1. 去畸变前转换：将点云从原始坐标系（如 lidar-1）转换到 pose 坐标系（如 imu-2）
    # 2. 去畸变：在 pose 坐标系下进行运动补偿
    # 3. 去畸变后转换：将点云从 pose 坐标系转换回投影所需的坐标系（如 lidar-1）
    #
    # 典型场景1：点云在 lidar-1，pose 是 imu-2_to_world，投影外参是 lidar-1_to_camera
    #   - align_enable_pre_transform = True
    #   - align_pre_transform_file = "lidar-1_to_imu-2.yaml"  # 去畸变前：转到 pose 坐标系
    #   - align_enable_post_transform = True
    #   - align_post_transform_file = "imu-2_to_lidar-1.yaml"  # 去畸变后：转回 lidar-1
    #
    # 典型场景2：点云在 lidar-1，pose 是 lidar-1_to_world，投影外参是 lidar-1_to_camera
    #   - align_enable_pre_transform = False  # 坐标系已对齐，无需转换
    #   - align_enable_post_transform = False
    #
    # 典型场景3：点云在 lidar-1，pose 是 imu-2_to_world，投影外参是 imu-2_to_camera
    #   - align_enable_pre_transform = True
    #   - align_pre_transform_file = "lidar-1_to_imu-2.yaml"  # 去畸变前：转到 pose 坐标系
    #   - align_enable_post_transform = False  # 保持在 imu-2，无需转回

    align_enable_pre_transform = ALIGN_ENABLE_PRE_TRANSFORM
    align_pre_transform_file = str(ALIGN_PRE_TRANSFORM_FILE) if ALIGN_PRE_TRANSFORM_FILE else None

    align_enable_post_transform = ALIGN_ENABLE_POST_TRANSFORM
    align_post_transform_file = str(ALIGN_POST_TRANSFORM_FILE) if ALIGN_POST_TRANSFORM_FILE else None

    # ========= Config Section: 帧选择 =========
    # 两种模式，指定帧或者指定范围，如果只有frame_ids有值而另外两个为None，则说明指反投影指定帧
    # frame_ids: 指定帧列表（可用 "0,10,20" 或单个整数）
    # frame_start/frame_end/frame_step: 用范围采样帧
    # frame_ids: "0,10,20"；frame_start/frame_end/frame_step: 范围采样
    frame_ids = FRAME_IDS
    frame_start = FRAME_START
    frame_end = FRAME_END
    frame_step = FRAME_STEP

    # ========= Config Section: 相机筛选 =========
    # only_cameras: 仅处理这些相机（逗号分隔），None 表示全部
    # 例如 "front,front_narrow,avm"，None 表示全部
    only_cameras = ONLY_CAMERAS

    # ========= Config Section: 相机类型规则 =========
    # fisheye_name_keywords: 相机名包含这些关键字视为鱼眼（当前跳过鱼眼）
    # narrow_keyword: 相机名包含该关键字视为去畸变针孔
    # default_pinhole_distorted: 其它相机按“含畸变针孔”处理
    # skip_fisheye_cameras: True 时直接跳过鱼眼相机投影
    # 规则优先级：manual overrides > fisheye keyword > narrow keyword > 默认针孔
    # fisheye_name_keywords: 若相机名含这些关键字，则按鱼眼含畸变处理
    # narrow_keyword: 若相机名含该关键字，则按去畸变针孔处理
    fisheye_name_keywords = FISHEYE_NAME_KEYWORDS
    narrow_keyword = NARROW_KEYWORD
    default_pinhole_distorted = DEFAULT_PINHOLE_DISTORTED
    skip_fisheye_cameras = SKIP_FISHEYE_CAMERAS

    enable_pointcloud_reprojection = ENABLE_POINTCLOUD_REPROJECTION
    sensor_status_report_path = SENSOR_STATUS_REPORT.resolve()

    # ========= Config Section: corrected annotation/pose/video/sync copy =========
    # Set ENABLE_COPY_ANNOTATIONS_POSE_SYNC=1 to run the previously separate
    # copy_annotations_pose_update_sync.py workflow before sensor validation / projection.
    # This keeps the overall workflow in one script while preserving an explicit switch.
    enable_copy_annotations_pose_sync = ENABLE_COPY_ANNOTATIONS_POSE_SYNC
    copy_input_root = COPY_INPUT_ROOT
    copy_done_root = COPY_DONE_ROOT
    copy_country = COPY_COUNTRY
    copy_report_out = str(COPY_REPORT_OUT) if COPY_REPORT_OUT else None
    copy_dry_run = COPY_DRY_RUN

    # ========= Optional pre-step: corrected annotation/pose/video/sync copy =========
    if enable_copy_annotations_pose_sync:
        copy_script_path = COPY_SCRIPT_PATH
        if not copy_script_path.exists():
            raise FileNotFoundError(f"copy_annotations_pose_update_sync.py not found: {copy_script_path}")
        import importlib.util
        spec = importlib.util.spec_from_file_location("copy_annotations_pose_update_sync", copy_script_path)
        copy_mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(copy_mod)
        report_out = Path(copy_report_out) if copy_report_out else output_root / f"copy_annotations_pose_update_sync_{now_stamp()}.xlsx"
        LOG.info("Running integrated copy/update-sync step: input=%s done_root=%s report=%s dry_run=%s",
                 copy_input_root, copy_done_root, report_out, copy_dry_run)
        rc = copy_mod.run(
            input_root=copy_input_root,
            done_root=copy_done_root,
            country=copy_country,
            report_out=report_out,
            dry_run=copy_dry_run,
        )
        if rc != 0:
            raise RuntimeError(f"copy/update-sync step failed with exit code {rc}")

    # ========= Config Section: FrameMap =========
    if force_regenerate_framemap or (generate_framemap_if_missing and not framemap_path.exists()):
        LOG.info("Generating FrameMap: %s", framemap_path)
        framemap_generator = FrameMapGenerator(str(data_root))
        framemap_generator.generate_framemap(str(framemap_path))
    else:
        LOG.info("Using existing FrameMap: %s", framemap_path)

    with open(framemap_path, "r", encoding="utf-8") as f:
        framemap = json.load(f)

    sensor_topic_config = framemap.get("metadata", {}).get("sensor_topic_config", {})
    camera_folders = sensor_topic_config.get("camera", {})
    lidar_folders = sensor_topic_config.get("lidar", {})
    if not camera_folders or not lidar_folders:
        raise RuntimeError("Missing camera or lidar entries in FrameMap metadata")

    # ========= Config Section: Calibration =========
    # parameter_yaml_dir: 标定参数目录
    # sensor_device_ids: 传感器昵称到设备 ID 映射（来自 sensors_used.yaml）
    parameter_yaml_dir = data_root / "parameter_yaml"
    parameter_folder = find_parameter_folder(parameter_yaml_dir)
    sensor_device_ids = load_sensors_used(parameter_folder)
    lidar_name = list(lidar_folders.keys())[0]
    lidar_device_id = sensor_device_ids.get(lidar_name)
    if not lidar_device_id:
        raise RuntimeError(f"LiDAR device id not found for {lidar_name}")
    if lidar_extrinsic_id_override:
        LOG.info("Override lidar extrinsic id: %s -> %s", lidar_device_id, lidar_extrinsic_id_override)
        lidar_device_id = lidar_extrinsic_id_override

    intrinsic_dir = parameter_folder / "intrinsic"
    extrinsic_dir = parameter_folder / "extrinsic"
    if lidar_folder_override:
        lidar_folder = Path(lidar_folder_override)
    else:
        lidar_folder = data_root / lidar_folders[lidar_name]

    # ========= Config Section: Alignment =========
    # aligner: 点云补偿到图像时间，支持去畸变前后的坐标系转换
    aligner = FrameAligner(
        pose_file=pose_file,
        enable_pre_transform=align_enable_pre_transform,
        pre_transform_file=Path(align_pre_transform_file) if align_pre_transform_file else None,
        enable_post_transform=align_enable_post_transform,
        post_transform_file=Path(align_post_transform_file) if align_post_transform_file else None,
    )

    # ========= Config Section: Projection Common =========
    # 点云投影公共参数：距离阈值、视场过滤、强度配色等
    projection_common = {
        "intensity_min": 4,
        "intensity_max": 15,
        "max_distance": 50,
        "min_distance": 0.0,
        "start_timestamp": None,
        "num_frames": None,
        "z_rotation_degrees": 0,
        "enable_coordinate_transform": False,
        "coordinate_transform_file": None,
        "time_tolerance": 0.1,
        "enable_distance_coloring": False,
        "enable_fov_filter": True,
        "intensity_mode": "road_mask",
        "intensity_p_lo": 2.0,
        "intensity_p_hi": 98.0,
    }

    # ========= Config Section: Camera Overrides =========
    # 手动覆盖某些相机的投影配置（优先级最高）
    camera_overrides: Dict[str, CameraProjectionConfig] = {
        # 示例（如需强制覆盖）:
        # "front": CameraProjectionConfig(image_is_undistorted=False),
        # "front_narrow": CameraProjectionConfig(image_is_undistorted=True),
        # "front_avm": CameraProjectionConfig(force_fisheye=True),
    }

    if only_cameras:
        only_cameras = {name.strip() for name in only_cameras.split(",") if name.strip()}

    sensor_status = {
        "data_root": str(data_root),
        "output_root": str(output_root),
        "framemap_path": str(framemap_path),
        "pose_file": str(pose_file),
        "enable_pointcloud_reprojection": enable_pointcloud_reprojection,
        "lidar": {
            "name": lidar_name,
            "folder": str(lidar_folder),
            "device_id": lidar_device_id,
            "file_count": count_timestamped_files(lidar_folder, (".las", ".laz")),
        },
        "cameras": {},
        "issues": [],
    }

    camera_pipelines: Dict[str, LasProjectToImagePipeline] = {}
    for camera_name, camera_folder in camera_folders.items():
        if only_cameras and camera_name not in only_cameras:
            continue
        camera_dir = data_root / camera_folder
        camera_device_id = sensor_device_ids.get(camera_name)
        camera_report = {
            "folder": str(camera_dir),
            "device_id": camera_device_id,
            "image_count": count_timestamped_files(camera_dir, (".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif")),
            "intrinsic_file": None,
            "extrinsic_file": None,
            "projection_enabled": enable_pointcloud_reprojection,
            "status": "pending",
            "reason": "",
        }
        sensor_status["cameras"][camera_name] = camera_report
        if not camera_device_id:
            camera_report["status"] = "missing_device_id"
            camera_report["reason"] = f"Camera device id not found for {camera_name}"
            sensor_status["issues"].append(camera_report["reason"])
            LOG.warning(camera_report["reason"])
            continue

        intrinsic_file = intrinsic_dir / f"{camera_device_id}.yaml"
        extrinsic_file = find_extrinsic_file(extrinsic_dir, lidar_device_id, camera_device_id)
        camera_report["intrinsic_file"] = str(intrinsic_file)
        camera_report["extrinsic_file"] = str(extrinsic_file) if extrinsic_file else None
        if not intrinsic_file.exists():
            camera_report["status"] = "missing_intrinsic"
            camera_report["reason"] = f"Intrinsic file not found for {camera_name}: {intrinsic_file}"
            sensor_status["issues"].append(camera_report["reason"])
            LOG.warning(camera_report["reason"])
            continue
        if extrinsic_file is None:
            camera_report["status"] = "missing_extrinsic"
            camera_report["reason"] = f"Extrinsic file not found for {camera_name} (lidar={lidar_device_id})"
            sensor_status["issues"].append(camera_report["reason"])
            LOG.warning(camera_report["reason"])
            continue

        override = build_camera_config_from_name(
            camera_name=camera_name,
            fisheye_name_keywords=fisheye_name_keywords,
            narrow_keyword=narrow_keyword,
            default_pinhole_distorted=default_pinhole_distorted,
            manual_overrides=camera_overrides,
        )
        camera_report["force_fisheye"] = override.force_fisheye
        camera_report["image_is_undistorted"] = override.image_is_undistorted
        if skip_fisheye_cameras and override.force_fisheye:
            camera_report["status"] = "skipped_fisheye"
            camera_report["reason"] = "skip_fisheye_cameras=True"
            LOG.info("Skip fisheye camera: %s", camera_name)
            continue

        if not enable_pointcloud_reprojection:
            camera_report["status"] = "ready_no_projection"
            continue

        output_dir = output_root / "projection" / camera_name
        pipeline = build_projector(
            camera_name=camera_name,
            lidar_dir=lidar_folder,
            camera_dir=camera_dir,
            output_dir=output_dir,
            intrinsic_file=intrinsic_file,
            extrinsic_file=extrinsic_file,
            config=override,
            projection_common=projection_common,
        )
        if pipeline:
            camera_pipelines[camera_name] = pipeline
            camera_report["status"] = "ready"
        else:
            camera_report["status"] = "pipeline_init_failed"
            camera_report["reason"] = "build_projector returned None"
            sensor_status["issues"].append(f"Projection pipeline init failed for {camera_name}")

    write_json_report(sensor_status_report_path, sensor_status)
    LOG.info("Wrote sensor status report: %s", sensor_status_report_path)

    if not enable_pointcloud_reprojection:
        LOG.info("ENABLE_POINTCLOUD_REPROJECTION=0, sensor status validation only; skip alignment/projection")
        return

    if not camera_pipelines:
        raise RuntimeError("No camera pipelines initialized")

    frames = framemap.get("frames", [])
    frame_ids = parse_frame_ids(frame_ids)
    selected_frames = select_frames(frames, frame_ids, frame_start, frame_end, frame_step)

    merged_root = output_root / "merged"

    for frame in selected_frames:
        frame_id = frame.get("frame_id")
        frames_timestamp = frame.get("frames_timestamp", {})
        lidar_ts_value = frames_timestamp.get("lidar", {}).get(lidar_name)
        lidar_ts = parse_timestamp_value(lidar_ts_value)
        if lidar_ts is None:
            LOG.info("Frame %s has no lidar timestamp, skipping", frame_id)
            continue

        lidar_ts_str = format_timestamp(lidar_ts)
        lidar_file = resolve_file_by_timestamp(lidar_folder, lidar_ts_str, (".las", ".laz"))
        if not lidar_file:
            LOG.warning("LiDAR file not found for %s in %s", lidar_ts_str, lidar_folder)
            continue

        # 每个相机使用各自的时间戳进行点云对齐（运动补偿）
        # 按时间戳缓存，避免相同时间戳重复对齐
        aligned_cache = {}  # camera_ts_str -> (points_xyz, points_intensity)

        current_frame_images = {}

        for camera_name, pipeline in camera_pipelines.items():
            camera_ts_value = frames_timestamp.get("camera", {}).get(camera_name)
            camera_ts = parse_timestamp_value(camera_ts_value)
            if camera_ts is None:
                continue

            camera_ts_str = format_timestamp(camera_ts)

            # 不保存 aligned 点云：统一从原始点云现场去畸变到各相机时间戳，仅内存复用
            if camera_ts_str in aligned_cache:
                aligned_points_xyz, aligned_points_intensity = aligned_cache[camera_ts_str]
            else:
                LOG.info("Aligning(in-mem) LAS to camera %s timestamp %s", camera_name, camera_ts_str)
                aligned_points_xyz, aligned_points_intensity = aligner.align_las_to_timestamp_inmem(
                    lidar_file, camera_ts
                )
                if aligned_points_xyz is None or len(aligned_points_xyz) == 0:
                    LOG.warning("Alignment(in-mem) failed for frame %s camera %s", frame_id, camera_name)
                    continue
                aligned_cache[camera_ts_str] = (aligned_points_xyz, aligned_points_intensity)

            camera_folder = data_root / camera_folders[camera_name]
            image_file = resolve_file_by_timestamp(camera_folder, camera_ts_str, (".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif"))
            if not image_file:
                LOG.warning("Image not found for %s in %s", camera_ts_str, camera_folder)
                continue

            img = pipeline.process_point_cloud_from_data(
                aligned_points_xyz, aligned_points_intensity, str(image_file), timestamp=camera_ts
            )
            if img is not None:
                current_frame_images[camera_name] = img

        if current_frame_images:
            merged_file = merged_root / f"{frame_id:06d}_merged.jpg"
            merge_camera_views(current_frame_images, merged_file)


# ===== END: FrameMap section =====

# ===== BEGIN: DataQualityInspector definitions =====
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
数据质检脚本 - DataQualityInspector
对采集数据做全面质检，输出 Excel 报告 + matplotlib 图表。

复用:
  - FrameMap_Align_projection_Pipeline.py 的 FrameMapGenerator（传感器解析 + FrameMap 生成）
  - MulitCopyToDistribution.py 的异常检测逻辑（缺帧gap检测、文件大小异常检测、连续段clips分析）
"""

import json
import logging
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import yaml

LOG = logging.getLogger("FrameMapDataQualityProjection")


# ============================================================
#  SensorScanner - 扫描传感器文件夹，提取时间戳和文件大小
# ============================================================
class SensorScanner:
    """扫描传感器文件夹，收集时间戳、文件大小、文件路径。"""

    def __init__(self, data_root: str, scan_workers: int = 0):
        self.data_root = data_root
        self.parameter_yaml_dir = os.path.join(data_root, "parameter_yaml")
        self.scan_workers = scan_workers

    # ---------- YAML 配置加载（复用 FrameMapGenerator 逻辑） ----------
    def load_yaml_config(self) -> Tuple[dict, dict]:
        calib_folders = [
            d for d in os.listdir(self.parameter_yaml_dir)
            if os.path.isdir(os.path.join(self.parameter_yaml_dir, d))
        ]
        if not calib_folders:
            raise ValueError(f"parameter_yaml 下未找到校准日期文件夹: {self.parameter_yaml_dir}")

        calib_date_folder = os.path.join(self.parameter_yaml_dir, calib_folders[0])
        sensors_used_path = os.path.join(calib_date_folder, "sensors_used.yaml")
        topic_map_path = os.path.join(calib_date_folder, "topic_map.yaml")

        LOG.info("加载配置: %s", calib_date_folder)
        with open(sensors_used_path, "r", encoding="utf-8") as f:
            sensors_used = yaml.safe_load(f)
        with open(topic_map_path, "r", encoding="utf-8") as f:
            topic_map = yaml.safe_load(f)
        return sensors_used, topic_map

    @staticmethod
    def topic_to_foldername(topic_path: str) -> str:
        return topic_path.strip("/").replace("/", "_")

    def find_actual_folder(self, expected_name: str, sensor_type: str = "camera") -> Optional[str]:
        prefix = "cam_" if sensor_type == "camera" else "lidar_"
        exact_path = os.path.join(self.data_root, expected_name)
        if os.path.exists(exact_path):
            return expected_name

        all_folders = [
            d for d in os.listdir(self.data_root)
            if os.path.isdir(os.path.join(self.data_root, d)) and d.startswith(prefix)
        ]

        if sensor_type == "camera":
            expected_parts = expected_name.split("_")
            if len(expected_parts) >= 4 and expected_parts[0] == "cam":
                target_location = "_".join(expected_parts[3:])
                for folder in all_folders:
                    folder_parts = folder.split("_")
                    if len(folder_parts) >= 4:
                        folder_location = "_".join(folder_parts[3:])
                        if folder_location == target_location:
                            return folder

        expected_parts = expected_name.split("_")
        best_match = None
        best_score = 0
        for folder in all_folders:
            folder_parts = folder.split("_")
            common = sum(1 for p in expected_parts if p in folder_parts)
            score = common / max(len(expected_parts), len(folder_parts))
            if score > best_score:
                best_score = score
                best_match = folder
        if best_score > 0.7:
            return best_match
        return None

    def parse_sensors_config(self, sensors_used: dict, topic_map: dict) -> Tuple[Dict[str, str], Dict[str, str]]:
        sensors = sensors_used.get("sensors", {})
        camera_config: Dict[str, str] = {}
        lidar_config: Dict[str, str] = {}

        for nickname, sensor_info in sensors.items():
            parts = str(sensor_info).split(",", 1)
            if len(parts) < 2:
                continue
            device_id = parts[0].strip()
            topic_path = parts[1].strip().strip("'\"[]")
            expected_folder = self.topic_to_foldername(topic_path)

            if device_id.startswith("camera-"):
                actual_folder = self.find_actual_folder(expected_folder, "camera")
                if actual_folder:
                    camera_config[nickname] = actual_folder
            elif device_id.startswith("lidar-"):
                actual_folder = self.find_actual_folder(expected_folder, "lidar")
                if actual_folder:
                    lidar_config[nickname] = actual_folder
        return camera_config, lidar_config

    # ---------- 时间戳解析 ----------
    @staticmethod
    def parse_timestamp(filename: str) -> Optional[float]:
        """从文件名解析时间戳。
        支持: '1234567890.123_xxx.jpg' 格式（脚本一）
        以及正则匹配（脚本二）。
        """
        name = os.path.basename(filename)
        # 方式一: 脚本一的方式 — 文件名以 seconds.milliseconds 开头
        dot_parts = name.split(".")
        if len(dot_parts) >= 2:
            try:
                return float(f"{dot_parts[0]}.{dot_parts[1]}")
            except ValueError:
                pass
        # 方式二: 正则（脚本二）
        patterns = [
            r'(\d{10}\.\d{3})',
            r'(\d{10}\.\d{6})',
            r'(\d{10})',
            r'(\d{13})',
        ]
        for pattern in patterns:
            match = re.search(pattern, name)
            if match:
                ts_str = match.group(1)
                if len(ts_str) == 13:
                    return float(ts_str) / 1000
                return float(ts_str)
        return None

    # ---------- 扫描单个传感器文件夹 ----------
    def scan_sensor_folder(self, folder_name: str, extension: str) -> List[dict]:
        """扫描一个传感器文件夹，返回按时间戳排序的文件列表。
        每项: {path, name, ts, size}
        """
        folder_path = os.path.join(self.data_root, folder_name)
        if not os.path.exists(folder_path):
            LOG.warning("传感器文件夹不存在: %s", folder_path)
            return []

        files_data = []
        try:
            with os.scandir(folder_path) as it:
                for entry in it:
                    if not entry.is_file():
                        continue
                    # Skip MAXI marker/sidecar files
                    if entry.name.lower().endswith(".maxi"):
                        continue
                    if not entry.name.endswith(extension):
                        continue
                    ts = self.parse_timestamp(entry.name)
                    if ts is None:
                        continue
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        continue
                    files_data.append({
                        "path": entry.path,
                        "name": entry.name,
                        "ts": ts,
                        "size": size,
                    })
        except FileNotFoundError:
            LOG.warning("传感器文件夹不存在: %s", folder_path)
            return []
        files_data.sort(key=lambda x: x["ts"])
        return files_data

    # ---------- 扫描所有传感器 ----------
    def scan_all(self) -> dict:
        """扫描所有传感器，返回结构化数据。
        返回:
          {
            'camera_config': {nickname: folder_name},
            'lidar_config':  {nickname: folder_name},
            'sensor_data':   {folder_name: [file_entries]},
          }
        """
        sensors_used, topic_map = self.load_yaml_config()
        camera_config, lidar_config = self.parse_sensors_config(sensors_used, topic_map)

        sensor_data: Dict[str, List[dict]] = {}

        scan_jobs = []
        for nickname, folder_name in camera_config.items():
            LOG.info("扫描相机 [%s] -> %s", nickname, folder_name)
            scan_jobs.append((folder_name, ".jpg"))
        for nickname, folder_name in lidar_config.items():
            LOG.info("扫描LiDAR [%s] -> %s", nickname, folder_name)
            scan_jobs.append((folder_name, ".las"))

        if self.scan_workers and self.scan_workers > 1 and len(scan_jobs) > 1:
            with ThreadPoolExecutor(max_workers=self.scan_workers) as ex:
                future_map = {
                    ex.submit(self.scan_sensor_folder, folder_name, ext): (folder_name, ext)
                    for folder_name, ext in scan_jobs
                }
                for fut in as_completed(future_map):
                    folder_name, _ext = future_map[fut]
                    sensor_data[folder_name] = fut.result()
        else:
            for folder_name, ext in scan_jobs:
                sensor_data[folder_name] = self.scan_sensor_folder(folder_name, ext)

        return {
            "camera_config": camera_config,
            "lidar_config": lidar_config,
            "sensor_data": sensor_data,
        }


# ============================================================
#  FrameMapAnalyzer - 生成/加载 FrameMap，分析交集
# ============================================================
class FrameMapAnalyzer:
    """加载或生成 FrameMap，统计各传感器帧匹配情况。"""

    def __init__(self, data_root: str, camera_config: dict, lidar_config: dict):
        self.data_root = data_root
        self.camera_config = camera_config
        self.lidar_config = lidar_config
        self.framemap_path = os.path.join(data_root, "FrameMap.json")
        self.framemap: Optional[dict] = None

    def ensure_framemap(self):
        """如果 FrameMap.json 不存在则调用脚本一的 FrameMapGenerator 生成。"""
        if os.path.exists(self.framemap_path):
            LOG.info("加载已有 FrameMap: %s", self.framemap_path)
            with open(self.framemap_path, "r", encoding="utf-8") as f:
                self.framemap = json.load(f)
            return

        LOG.info("FrameMap 不存在，自动生成...")
        gen = FrameMapGenerator(self.data_root)
        gen.generate_framemap(self.framemap_path)
        with open(self.framemap_path, "r", encoding="utf-8") as f:
            self.framemap = json.load(f)
        LOG.info("FrameMap 生成完毕，共 %d 帧", self.framemap["metadata"]["frames_count"])

    def analyze(self) -> dict:
        """分析 FrameMap 中的帧匹配情况。"""
        self.ensure_framemap()
        frames = self.framemap.get("frames", [])
        metadata = self.framemap.get("metadata", {})
        total_frames = len(frames)

        # 收集所有传感器名
        camera_names = list(self.camera_config.keys())
        lidar_names = list(self.lidar_config.keys())
        all_sensor_names = camera_names + lidar_names

        # 统计每个传感器有效帧数
        valid_counts: Dict[str, int] = {name: 0 for name in all_sensor_names}
        # 完全交集帧数（所有传感器都有数据）
        full_intersection_count = 0
        # 各传感器对之间的交集帧数
        pair_intersection: Dict[str, Dict[str, int]] = {
            a: {b: 0 for b in all_sensor_names} for a in all_sensor_names
        }

        for frame in frames:
            ft = frame.get("frames_timestamp", {})
            cam_ts = ft.get("camera", {})
            lid_ts = ft.get("lidar", {})

            sensor_valid = {}
            for cam_name in camera_names:
                ts_val = cam_ts.get(cam_name)
                is_valid = ts_val is not None and str(ts_val).lower() != "none"
                sensor_valid[cam_name] = is_valid
                if is_valid:
                    valid_counts[cam_name] += 1
            for lid_name in lidar_names:
                ts_val = lid_ts.get(lid_name)
                is_valid = ts_val is not None and str(ts_val).lower() != "none"
                sensor_valid[lid_name] = is_valid
                if is_valid:
                    valid_counts[lid_name] += 1

            if all(sensor_valid.values()):
                full_intersection_count += 1

            for a in all_sensor_names:
                for b in all_sensor_names:
                    if sensor_valid.get(a) and sensor_valid.get(b):
                        pair_intersection[a][b] += 1

        return {
            "total_frames": total_frames,
            "valid_counts": valid_counts,
            "full_intersection_count": full_intersection_count,
            "pair_intersection": pair_intersection,
            "camera_names": camera_names,
            "lidar_names": lidar_names,
        }


# ============================================================
#  QualityInspector - 核心质检逻辑
# ============================================================
class QualityInspector:
    """对每个传感器执行缺帧检测、文件大小异常检测、连续段(clips)统计。"""

    def __init__(
        self,
        gap_threshold: float = 0.18,
        expected_interval: float = 0.1,
        small_file_threshold_kb: int = 200,
        size_std_factor: float = 3.0,
        clip_thresholds: Optional[List[int]] = None,
    ):
        self.gap_threshold = gap_threshold
        self.expected_interval = expected_interval
        self.small_file_threshold_bytes = small_file_threshold_kb * 1024
        self.size_std_factor = size_std_factor
        self.clip_thresholds = clip_thresholds or [30, 70, 140]

    # ---------- 缺帧检测 ----------
    def detect_gaps(self, files_data: List[dict]) -> List[dict]:
        """检测相邻帧时间戳差 > gap_threshold 的缺帧。
        返回列表: [{index, prev_file, curr_file, prev_ts, curr_ts, gap}]
        """
        gaps = []
        for i in range(1, len(files_data)):
            prev = files_data[i - 1]
            curr = files_data[i]
            diff = curr["ts"] - prev["ts"]
            if diff > self.gap_threshold:
                gaps.append({
                    "index": i,
                    "prev_file": prev["name"],
                    "curr_file": curr["name"],
                    "prev_ts": prev["ts"],
                    "curr_ts": curr["ts"],
                    "gap": diff,
                })
        return gaps

    # ---------- 文件大小异常检测 ----------
    def detect_size_anomalies(self, files_data: List[dict]) -> List[dict]:
        """检测文件大小异常。
        - 绝对阈值: < small_file_threshold_bytes
        - 统计阈值: 偏离中位数超过 size_std_factor 倍标准差
        """
        if not files_data:
            return []

        sizes = np.array([f["size"] for f in files_data], dtype=np.float64)
        median_size = float(np.median(sizes))
        std_size = float(np.std(sizes))

        anomalies = []
        for f in files_data:
            reasons = []
            if f["size"] < self.small_file_threshold_bytes:
                reasons.append(f"小于{self.small_file_threshold_bytes // 1024}KB")
            if std_size > 0 and abs(f["size"] - median_size) > self.size_std_factor * std_size:
                reasons.append(f"偏离中位数超{self.size_std_factor}倍标准差")
            if reasons:
                anomalies.append({
                    "file": f["name"],
                    "path": f["path"],
                    "ts": f["ts"],
                    "size": f["size"],
                    "median_size": median_size,
                    "std_size": std_size,
                    "reasons": reasons,
                })
        return anomalies

    # ---------- 连续段 (clips) 统计 ----------
    def compute_clips(self, files_data: List[dict]) -> List[dict]:
        """按 gap_threshold 将时间戳序列分割为连续段。
        返回: [{start_ts, end_ts, duration, frame_count, start_file, end_file}]
        """
        if not files_data:
            return []

        clips = []
        clip_start = 0
        for i in range(1, len(files_data)):
            diff = files_data[i]["ts"] - files_data[i - 1]["ts"]
            if diff > self.gap_threshold:
                clips.append(self._make_clip(files_data, clip_start, i - 1))
                clip_start = i
        clips.append(self._make_clip(files_data, clip_start, len(files_data) - 1))
        return clips

    @staticmethod
    def _make_clip(files_data: List[dict], start_idx: int, end_idx: int) -> dict:
        return {
            "start_ts": files_data[start_idx]["ts"],
            "end_ts": files_data[end_idx]["ts"],
            "duration": files_data[end_idx]["ts"] - files_data[start_idx]["ts"],
            "frame_count": end_idx - start_idx + 1,
            "start_file": files_data[start_idx]["name"],
            "end_file": files_data[end_idx]["name"],
        }

    def classify_clips(self, clips: List[dict]) -> Dict[int, List[dict]]:
        """按三档阈值分类 clips。
        返回: {threshold: [clips whose duration >= threshold]}
        """
        result = {}
        for thr in self.clip_thresholds:
            result[thr] = [c for c in clips if c["duration"] >= thr]
        return result

    # ---------- 对单个传感器做全面质检 ----------
    def inspect_sensor(self, sensor_name: str, files_data: List[dict]) -> dict:
        """对单个传感器执行全部质检，返回结果字典。"""
        if not files_data:
            return {
                "sensor": sensor_name,
                "file_count": 0,
                "time_range": None,
                "total_duration": 0,
                "avg_size": 0,
                "min_size": 0,
                "max_size": 0,
                "gaps": [],
                "size_anomalies": [],
                "clips": [],
                "clips_classified": {},
            }

        sizes = [f["size"] for f in files_data]
        time_range = (files_data[0]["ts"], files_data[-1]["ts"])
        total_duration = time_range[1] - time_range[0]

        gaps = self.detect_gaps(files_data)
        size_anomalies = self.detect_size_anomalies(files_data)
        clips = self.compute_clips(files_data)
        clips_classified = self.classify_clips(clips)

        return {
            "sensor": sensor_name,
            "file_count": len(files_data),
            "time_range": time_range,
            "total_duration": total_duration,
            "avg_size": float(np.mean(sizes)),
            "min_size": min(sizes),
            "max_size": max(sizes),
            "gaps": gaps,
            "size_anomalies": size_anomalies,
            "clips": clips,
            "clips_classified": clips_classified,
        }

    def inspect_all(self, sensor_data: Dict[str, List[dict]]) -> Dict[str, dict]:
        results = {}
        for sensor_name, files_data in sensor_data.items():
            LOG.info("质检传感器: %s (%d 文件)", sensor_name, len(files_data))
            results[sensor_name] = self.inspect_sensor(sensor_name, files_data)
        return results


# ============================================================
#  ReportGenerator - 生成 Excel 报告
# ============================================================
class ReportGenerator:
    """使用 openpyxl 生成 xlsx 报告。"""

    def __init__(self, output_path: str):
        self.output_path = output_path

    @staticmethod
    def _ts_str(ts: Optional[float]) -> str:
        if ts is None:
            return ""
        return f"{ts:.3f}"

    @staticmethod
    def _size_str(size_bytes: float) -> str:
        if size_bytes < 1024:
            return f"{size_bytes:.0f} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        else:
            return f"{size_bytes / (1024 * 1024):.2f} MB"

    def generate(
        self,
        data_root: str,
        scan_result: dict,
        framemap_analysis: dict,
        inspection_results: Dict[str, dict],
        clip_thresholds: List[int],
    ):
        from openpyxl import Workbook
        from openpyxl.styles import Font, Alignment, PatternFill, Border, Side

        wb = Workbook()
        header_font = Font(bold=True, size=11)
        header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
        header_font_white = Font(bold=True, size=11, color="FFFFFF")
        thin_border = Border(
            left=Side(style="thin"),
            right=Side(style="thin"),
            top=Side(style="thin"),
            bottom=Side(style="thin"),
        )

        def write_header(ws, row, headers):
            for col, h in enumerate(headers, 1):
                cell = ws.cell(row=row, column=col, value=h)
                cell.font = header_font_white
                cell.fill = header_fill
                cell.alignment = Alignment(horizontal="center", wrap_text=True)
                cell.border = thin_border

        def write_row(ws, row, values):
            for col, v in enumerate(values, 1):
                cell = ws.cell(row=row, column=col, value=v)
                cell.border = thin_border

        # ===== Sheet 1: 总览 =====
        ws = wb.active
        ws.title = "总览"
        ws.column_dimensions["A"].width = 25
        ws.column_dimensions["B"].width = 50

        overview_data = [
            ("数据根目录", data_root),
            ("相机数量", len(scan_result["camera_config"])),
            ("LiDAR数量", len(scan_result["lidar_config"])),
            ("传感器列表（相机）", ", ".join(f"{k}={v}" for k, v in scan_result["camera_config"].items())),
            ("传感器列表（LiDAR）", ", ".join(f"{k}={v}" for k, v in scan_result["lidar_config"].items())),
            ("FrameMap总帧数", framemap_analysis["total_frames"]),
            ("完全交集帧数", framemap_analysis["full_intersection_count"]),
        ]
        for sensor_name, cnt in framemap_analysis["valid_counts"].items():
            overview_data.append((f"{sensor_name} 有效帧数", cnt))

        # 传感器时间范围
        for sensor_name, result in inspection_results.items():
            tr = result.get("time_range")
            if tr:
                overview_data.append((
                    f"{sensor_name} 时间范围",
                    f"{self._ts_str(tr[0])} ~ {self._ts_str(tr[1])} (时长 {result['total_duration']:.1f}s)"
                ))

        write_header(ws, 1, ["项目", "值"])
        for i, (k, v) in enumerate(overview_data, 2):
            write_row(ws, i, [k, str(v)])

        # ===== Sheet 2: 各传感器详情 =====
        ws2 = wb.create_sheet("各传感器详情")
        headers2 = [
            "传感器文件夹", "文件数", "起始时间戳", "结束时间戳", "总时长(s)",
            "平均大小", "最小大小", "最大大小", "缺帧数", "大小异常数",
        ]
        for thr in clip_thresholds:
            headers2.append(f"≥{thr}s clips数")
            headers2.append(f"≥{thr}s clips总时长(s)")
        write_header(ws2, 1, headers2)

        row = 2
        for sensor_name, result in inspection_results.items():
            tr = result.get("time_range")
            values = [
                sensor_name,
                result["file_count"],
                self._ts_str(tr[0] if tr else None),
                self._ts_str(tr[1] if tr else None),
                f"{result['total_duration']:.1f}",
                self._size_str(result["avg_size"]),
                self._size_str(result["min_size"]) if result["file_count"] > 0 else "",
                self._size_str(result["max_size"]) if result["file_count"] > 0 else "",
                len(result["gaps"]),
                len(result["size_anomalies"]),
            ]
            for thr in clip_thresholds:
                cls = result["clips_classified"].get(thr, [])
                values.append(len(cls))
                values.append(f"{sum(c['duration'] for c in cls):.1f}")
            write_row(ws2, row, values)
            row += 1

        # ===== Sheet 3: 缺帧详情 =====
        ws3 = wb.create_sheet("缺帧详情")
        headers3 = ["传感器文件夹", "前一帧文件", "后一帧文件", "前一帧时间戳", "后一帧时间戳", "间隔(s)"]
        write_header(ws3, 1, headers3)
        row = 2
        for sensor_name, result in inspection_results.items():
            for gap in result["gaps"]:
                write_row(ws3, row, [
                    sensor_name,
                    gap["prev_file"],
                    gap["curr_file"],
                    self._ts_str(gap["prev_ts"]),
                    self._ts_str(gap["curr_ts"]),
                    f"{gap['gap']:.4f}",
                ])
                row += 1

        # ===== Sheet 4: 大小异常详情 =====
        ws4 = wb.create_sheet("大小异常详情")
        headers4 = ["传感器文件夹", "文件名", "文件大小", "中位数大小", "标准差", "异常原因"]
        write_header(ws4, 1, headers4)
        row = 2
        for sensor_name, result in inspection_results.items():
            for a in result["size_anomalies"]:
                write_row(ws4, row, [
                    sensor_name,
                    a["file"],
                    self._size_str(a["size"]),
                    self._size_str(a["median_size"]),
                    self._size_str(a["std_size"]),
                    "; ".join(a["reasons"]),
                ])
                row += 1

        # ===== Sheet 5: Clips统计 =====
        ws5 = wb.create_sheet("Clips统计")
        headers5 = ["传感器文件夹", "阈值(s)", "起始时间戳", "结束时间戳", "时长(s)", "帧数", "起始文件", "结束文件"]
        write_header(ws5, 1, headers5)
        row = 2
        for sensor_name, result in inspection_results.items():
            for thr in clip_thresholds:
                for clip in result["clips_classified"].get(thr, []):
                    write_row(ws5, row, [
                        sensor_name,
                        thr,
                        self._ts_str(clip["start_ts"]),
                        self._ts_str(clip["end_ts"]),
                        f"{clip['duration']:.1f}",
                        clip["frame_count"],
                        clip["start_file"],
                        clip["end_file"],
                    ])
                    row += 1

        # ===== Sheet 6: FrameMap交集 =====
        ws6 = wb.create_sheet("FrameMap交集")
        all_names = framemap_analysis["camera_names"] + framemap_analysis["lidar_names"]
        headers6 = [""] + all_names
        write_header(ws6, 1, headers6)
        for i, name_a in enumerate(all_names, 2):
            values = [name_a]
            for name_b in all_names:
                values.append(framemap_analysis["pair_intersection"].get(name_a, {}).get(name_b, 0))
            write_row(ws6, i, values)

        # 调整列宽
        for ws_item in [ws2, ws3, ws4, ws5, ws6]:
            for col_cells in ws_item.columns:
                max_len = 0
                for cell in col_cells:
                    if cell.value:
                        max_len = max(max_len, len(str(cell.value)))
                ws_item.column_dimensions[col_cells[0].column_letter].width = min(max_len + 4, 40)

        wb.save(self.output_path)
        LOG.info("Excel 报告已保存: %s", self.output_path)


# ============================================================
#  ChartGenerator - 生成 matplotlib 图表
# ============================================================
class ChartGenerator:
    """使用 matplotlib 生成质检图表（PNG），中文标签。"""

    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        self._setup_chinese_font()

    def _setup_chinese_font(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager

        self.font_name = None
        self._font_prop = None  # FontProperties 实例，用于直接传给绘图函数

        # 策略: 直接搜索系统中可用的中文字体文件
        import subprocess
        font_file = None
        search_patterns = [
            "NotoSansCJKsc-Regular*",
            "NotoSansCJK-Regular*",
            "*NotoSansCJK*SC*",
            "*NotoSerifCJK*SC*",
            "SimHei*",
            "wqy-microhei*",
            "wqy-zenhei*",
            "DroidSansFallback*",
        ]
        for pattern in search_patterns:
            result = subprocess.run(
                ["find", "/usr/share/fonts", "-name", pattern, "-type", "f"],
                capture_output=True, text=True, timeout=5,
            )
            files = [l for l in result.stdout.strip().split("\n") if l]
            if files:
                font_file = files[0]
                break

        if font_file:
            font_manager.fontManager.addfont(font_file)
            self._font_prop = font_manager.FontProperties(fname=font_file)
            self.font_name = self._font_prop.get_name()
            plt.rcParams["font.family"] = "sans-serif"
            plt.rcParams["font.sans-serif"] = [self.font_name, "DejaVu Sans"]
            LOG.info("使用中文字体: %s (%s)", self.font_name, font_file)
        else:
            plt.rcParams["font.sans-serif"] = ["DejaVu Sans"]
            LOG.warning("未找到中文字体，图表中文可能显示为方块")
        plt.rcParams["axes.unicode_minus"] = False

    # ---------- 图1: 传感器时间线图 ----------
    def plot_sensor_timeline(
        self,
        inspection_results: Dict[str, dict],
        filename: str = "sensor_timeline.png",
    ):
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches

        sensor_names = list(inspection_results.keys())
        if not sensor_names:
            return

        fig, ax = plt.subplots(figsize=(16, max(4, len(sensor_names) * 0.8)))

        for i, sensor_name in enumerate(sensor_names):
            result = inspection_results[sensor_name]
            clips = result.get("clips", [])
            gaps = result.get("gaps", [])

            y = i
            # 画连续段（蓝色色块）
            for clip in clips:
                ax.barh(y, clip["duration"], left=clip["start_ts"],
                        height=0.6, color="#4472C4", alpha=0.7, edgecolor="none")
            # 画 gap（红色标记）
            for gap in gaps:
                ax.barh(y, gap["gap"], left=gap["prev_ts"],
                        height=0.6, color="red", alpha=0.8, edgecolor="#8B0000", linewidth=0.6)
                # 加强可见性：在 gap 两端和中点画醒目的竖线/点
                y0, y1 = y - 0.35, y + 0.35
                ax.vlines([gap["prev_ts"], gap["curr_ts"]], y0, y1, color="#C00000", linewidth=1.6)
                mid = gap["prev_ts"] + gap["gap"] / 2
                ax.plot(mid, y, marker="o", markersize=4, color="#C00000", markeredgecolor="white", markeredgewidth=0.6)

        ax.set_yticks(range(len(sensor_names)))
        ax.set_yticklabels([self._short_name(s) for s in sensor_names], fontsize=8)
        ax.set_xlabel("时间戳 (GPS秒)")
        ax.set_title("传感器时间线 (蓝色=有数据, 红色=缺帧gap)")

        # 固定/全局横轴：避免 matplotlib 自动边界在某些情况下只显示局部时间段。
        x_min = TIMELINE_X_MIN
        x_max = TIMELINE_X_MAX
        if TIMELINE_USE_GLOBAL_XLIM and (x_min is None or x_max is None):
            all_ranges = [r.get("time_range") for r in inspection_results.values() if r.get("time_range")]
            if all_ranges:
                if x_min is None:
                    x_min = min(r[0] for r in all_ranges)
                if x_max is None:
                    x_max = max(r[1] for r in all_ranges)
        if x_min is not None and x_max is not None and x_max > x_min:
            pad = max((x_max - x_min) * 0.01, 0.5)
            ax.set_xlim(x_min - pad, x_max + pad)
            ax.ticklabel_format(useOffset=False, style="plain", axis="x")

        ax.invert_yaxis()

        blue_patch = mpatches.Patch(color="#4472C4", alpha=0.7, label="有数据")
        red_patch = mpatches.Patch(color="red", alpha=0.8, label="缺帧gap")
        ax.legend(handles=[blue_patch, red_patch], loc="upper right")

        plt.tight_layout()
        path = os.path.join(self.output_dir, filename)
        fig.savefig(path, dpi=150)
        plt.close(fig)
        LOG.info("图表已保存: %s", path)

    # ---------- 图2: Clips分布柱状图 ----------
    def plot_clips_bar(
        self,
        inspection_results: Dict[str, dict],
        clip_thresholds: List[int],
        filename: str = "clips_count_bar.png",
    ):
        import matplotlib.pyplot as plt

        sensor_names = list(inspection_results.keys())
        if not sensor_names:
            return

        x = np.arange(len(sensor_names))
        n_groups = len(clip_thresholds)
        width = 0.8 / n_groups

        fig, ax = plt.subplots(figsize=(max(10, len(sensor_names) * 1.2), 6))
        colors = ["#4472C4", "#ED7D31", "#70AD47"]

        alert_min_ratio = 80.0
        alert_thresholds = {70, 140}

        for gi, thr in enumerate(clip_thresholds):
            ratios = []
            for sn in sensor_names:
                cls = inspection_results[sn]["clips_classified"].get(thr, [])
                total_dur = inspection_results[sn].get("total_duration", 0) or 0
                dur = sum(c["duration"] for c in cls)
                ratios.append((dur / total_dur * 100) if total_dur > 0 else 0)
            bars = ax.bar(x + gi * width, ratios, width, label=f"≥{thr}s", color=colors[gi % len(colors)])

            # Highlight low coverage for key thresholds (e.g. ≥70s/≥140s)
            if thr in alert_thresholds:
                for i, bar in enumerate(bars):
                    if ratios[i] < alert_min_ratio:
                        bar.set_edgecolor("#C00000")
                        bar.set_linewidth(2.0)
                        bar.set_hatch("//")
                        ax.text(
                            bar.get_x() + bar.get_width() / 2,
                            bar.get_height() + 1.0,
                            "!",
                            ha="center",
                            va="bottom",
                            fontsize=8,
                            color="#C00000",
                        )

        ax.set_xticks(x + width * (n_groups - 1) / 2)
        ax.set_xticklabels([self._short_name(s) for s in sensor_names], rotation=45, ha="right", fontsize=8)
        ax.set_ylabel("总时长占比(%)")
        ax.set_title(f"各传感器 Clips 时长占总时长比例（< {alert_min_ratio:.0f}% 标记）")
        ax.legend()

        plt.tight_layout()
        path = os.path.join(self.output_dir, filename)
        fig.savefig(path, dpi=150)
        plt.close(fig)
        LOG.info("图表已保存: %s", path)

    # ---------- 图3: Clips时长堆叠图 ----------
    def plot_clips_duration_stacked(
        self,
        inspection_results: Dict[str, dict],
        clip_thresholds: List[int],
        filename: str = "clips_duration_stacked.png",
    ):
        import matplotlib.pyplot as plt

        sensor_names = list(inspection_results.keys())
        if not sensor_names:
            return

        sorted_thresholds = sorted(clip_thresholds)
        min_thr = sorted_thresholds[0]

        total_duration = []
        total_valid = []
        dur_ge = {thr: [] for thr in sorted_thresholds}

        for sn in sensor_names:
            total_dur = inspection_results[sn].get("total_duration", 0) or 0
            total_duration.append(total_dur)
            clips = inspection_results[sn].get("clips", [])
            valid_dur = sum(c["duration"] for c in clips)
            total_valid.append(valid_dur)
            for thr in sorted_thresholds:
                cls = inspection_results[sn]["clips_classified"].get(thr, [])
                dur_ge[thr].append(sum(c["duration"] for c in cls))

        # 计算独占区间（≥30且<70，≥70且<140，≥140）
        exclusive = []
        labels = []
        colors = ["#4472C4", "#ED7D31", "#70AD47"]
        for i, thr in enumerate(sorted_thresholds):
            if i < len(sorted_thresholds) - 1:
                next_thr = sorted_thresholds[i + 1]
                vals = [max(dur_ge[thr][j] - dur_ge[next_thr][j], 0) for j in range(len(sensor_names))]
                labels.append(f"≥{thr}s<{next_thr}s")
            else:
                vals = dur_ge[thr]
                labels.append(f"≥{thr}s")
            exclusive.append(vals)

        # 短于最小阈值
        short_vals = [max(total_valid[i] - dur_ge[min_thr][i], 0) for i in range(len(sensor_names))]
        # 缺失/间断
        missing_vals = [max(total_duration[i] - total_valid[i], 0) for i in range(len(sensor_names))]

        # 转成占总时长百分比
        def to_pct(values):
            out = []
            for i, v in enumerate(values):
                td = total_duration[i]
                out.append((v / td * 100) if td > 0 else 0)
            return out

        exclusive_pct = [to_pct(vals) for vals in exclusive]
        short_pct = to_pct(short_vals)
        missing_pct = to_pct(missing_vals)

        fig, ax = plt.subplots(figsize=(max(10, len(sensor_names) * 1.2), 6))
        x = np.arange(len(sensor_names))
        bottom = np.zeros(len(sensor_names))

        for i, vals in enumerate(exclusive_pct):
            ax.bar(x, vals, bottom=bottom, label=labels[i], color=colors[i % len(colors)], alpha=0.85)
            bottom += np.array(vals)

        if any(v > 0 for v in short_vals):
            ax.bar(x, short_pct, bottom=bottom, label=f"<{min_thr}s", color="#A5A5A5", alpha=0.85)
            bottom += np.array(short_pct)

        if any(v > 0 for v in missing_vals):
            ax.bar(x, missing_pct, bottom=bottom, label="缺失/间断", color="#C00000", alpha=0.85)
            bottom += np.array(missing_pct)

        ax.set_xticks(x)
        ax.set_xticklabels([self._short_name(s) for s in sensor_names], rotation=45, ha="right", fontsize=8)
        ax.set_ylabel("占总时长比例(%)")
        ax.set_title("各传感器总有效时长占比（按阈值分段）")
        ax.set_ylim(0, 105)
        ax.legend(ncol=2)

        # 标注总有效时长占比
        for i, td in enumerate(total_duration):
            pct = (total_valid[i] / td * 100) if td > 0 else 0
            ax.text(i, min(pct + 1.5, 102), f"{pct:.1f}%", ha="center", va="bottom", fontsize=8)

        plt.tight_layout()
        path = os.path.join(self.output_dir, filename)
        fig.savefig(path, dpi=150)
        plt.close(fig)
        LOG.info("图表已保存: %s", path)

    # ---------- 图4: 文件大小分布箱线图 ----------
    def plot_size_boxplot(
        self,
        sensor_data: Dict[str, List[dict]],
        filename: str = "file_size_boxplot.png",
    ):
        import matplotlib.pyplot as plt

        sensor_names = list(sensor_data.keys())
        if not sensor_names:
            return

        data_for_box = []
        labels = []
        for sn in sensor_names:
            sizes_kb = [f["size"] / 1024 for f in sensor_data[sn]]
            if sizes_kb:
                data_for_box.append(sizes_kb)
                labels.append(self._short_name(sn))

        if not data_for_box:
            return

        fig, ax = plt.subplots(figsize=(max(10, len(labels) * 1.2), 6))
        bp = ax.boxplot(data_for_box, labels=labels, showfliers=True, patch_artist=True)
        for patch in bp["boxes"]:
            patch.set_facecolor("#4472C4")
            patch.set_alpha(0.6)

        ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
        ax.set_ylabel("文件大小 (KB)")
        ax.set_title("各传感器文件大小分布")

        plt.tight_layout()
        path = os.path.join(self.output_dir, filename)
        fig.savefig(path, dpi=150)
        plt.close(fig)
        LOG.info("图表已保存: %s", path)

    # ---------- 图5: 传感器可用/不可用帧占比 ----------
    def plot_sensor_availability(
        self,
        framemap_analysis: dict,
        filename: str = "sensor_availability_stacked.png",
    ):
        import matplotlib.pyplot as plt

        total_frames = framemap_analysis.get("total_frames", 0)
        valid_counts = framemap_analysis.get("valid_counts", {})
        if total_frames <= 0 or not valid_counts:
            return

        sensor_names = list(valid_counts.keys())
        available = [valid_counts.get(s, 0) for s in sensor_names]
        unavailable = [max(total_frames - a, 0) for a in available]

        x = np.arange(len(sensor_names))
        fig, ax = plt.subplots(figsize=(max(10, len(sensor_names) * 1.2), 6))

        ax.bar(x, available, color="#4472C4", label="可用帧")
        ax.bar(x, unavailable, bottom=available, color="#C00000", label="不可用帧")

        ax.set_xticks(x)
        ax.set_xticklabels([self._short_name(s) for s in sensor_names], rotation=45, ha="right", fontsize=8)
        ax.set_ylabel("帧数")
        ax.set_title("各传感器可用/不可用帧占比")
        ax.legend()

        # 标注可用率
        for i, a in enumerate(available):
            pct = a / total_frames * 100 if total_frames > 0 else 0
            ax.text(i, a + unavailable[i] + total_frames * 0.005, f"{pct:.1f}%", ha="center", va="bottom", fontsize=8)

        plt.tight_layout()
        path = os.path.join(self.output_dir, filename)
        fig.savefig(path, dpi=150)
        plt.close(fig)
        LOG.info("图表已保存: %s", path)

    @staticmethod
    def _short_name(name: str, max_len: int = 20) -> str:
        if len(name) <= max_len:
            return name
        return name[:max_len - 3] + "..."


# ============================================================
#  main
# ============================================================
# ===== END: DataQuality section =====


# ============================================================
#  Offline sibling font patch
# ============================================================


def _setup_sibling_chinese_font(self):
    """Use only the cached sibling simhei.ttf for offline multi-machine consistency."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    self.font_name = None
    self._font_prop = None
    if not SIBLING_FONT_PATH.exists():
        raise FileNotFoundError(
            f"缺少脚本同级字体文件: {SIBLING_FONT_PATH}\n"
            "请把 simhei.ttf 和本脚本放在同一目录后再运行。"
        )
    font_manager.fontManager.addfont(str(SIBLING_FONT_PATH))
    self._font_prop = font_manager.FontProperties(fname=str(SIBLING_FONT_PATH))
    self.font_name = self._font_prop.get_name()
    plt.rcParams["font.family"] = "sans-serif"
    plt.rcParams["font.sans-serif"] = [self.font_name]
    plt.rcParams["axes.unicode_minus"] = False
    LOG.info("使用脚本同级中文字体: %s (%s)", self.font_name, SIBLING_FONT_PATH)


# Patch DataQualityInspector.ChartGenerator font setup.
ChartGenerator._setup_chinese_font = _setup_sibling_chinese_font


# ============================================================
#  Combined entrypoint
# ============================================================
def _combined_env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def run_data_quality_inspector():
    """Run DataQualityInspector with the hardcoded config block above."""
    data_root = DATA_ROOT.resolve()
    quality_output_dir = QUALITY_OUTPUT_DIR.resolve()
    quality_output_dir.mkdir(parents=True, exist_ok=True)

    clip_thresholds = QUALITY_CLIP_THRESHOLDS
    gap_threshold = QUALITY_GAP_THRESHOLD
    expected_interval = QUALITY_EXPECTED_INTERVAL
    small_file_threshold_kb = QUALITY_SMALL_FILE_THRESHOLD_KB
    size_std_factor = QUALITY_SIZE_STD_FACTOR
    scan_workers = QUALITY_SCAN_WORKERS
    framemap_path = FRAMEMAP_PATH.resolve()

    LOG.info("====== DataQuality Step 1: 扫描传感器文件夹 ======")
    scanner = SensorScanner(str(data_root), scan_workers=scan_workers)
    scan_result = scanner.scan_all()
    camera_config = scan_result["camera_config"]
    lidar_config = scan_result["lidar_config"]
    sensor_data = scan_result["sensor_data"]

    for folder_name, files in sensor_data.items():
        LOG.info("  %s: %d 文件", folder_name, len(files))

    LOG.info("====== DataQuality Step 2: FrameMap 分析 ======")
    fma = FrameMapAnalyzer(str(data_root), camera_config, lidar_config)
    fma.framemap_path = str(framemap_path)
    framemap_analysis = fma.analyze()
    LOG.info("  总帧数: %d, 完全交集: %d", framemap_analysis["total_frames"], framemap_analysis["full_intersection_count"])

    LOG.info("====== DataQuality Step 3: 质检 ======")
    inspector = QualityInspector(
        gap_threshold=gap_threshold,
        expected_interval=expected_interval,
        small_file_threshold_kb=small_file_threshold_kb,
        size_std_factor=size_std_factor,
        clip_thresholds=clip_thresholds,
    )
    inspection_results = inspector.inspect_all(sensor_data)

    for sensor_name, result in inspection_results.items():
        LOG.info(
            "  %s: %d文件, %d缺帧, %d大小异常, clips[%s]",
            sensor_name,
            result["file_count"],
            len(result["gaps"]),
            len(result["size_anomalies"]),
            "/".join(f"≥{t}s:{len(result['clips_classified'].get(t, []))}" for t in clip_thresholds),
        )

    LOG.info("====== DataQuality Step 4: 生成 Excel 报告 ======")
    report_path = quality_output_dir / "数据质检报告.xlsx"
    report_gen = ReportGenerator(str(report_path))
    report_gen.generate(str(data_root), scan_result, framemap_analysis, inspection_results, clip_thresholds)

    LOG.info("====== DataQuality Step 5: 生成图表 ======")
    chart_gen = ChartGenerator(str(quality_output_dir))
    chart_gen.plot_sensor_timeline(inspection_results)
    chart_gen.plot_clips_bar(inspection_results, clip_thresholds)
    chart_gen.plot_clips_duration_stacked(inspection_results, clip_thresholds)
    chart_gen.plot_size_boxplot(sensor_data)
    chart_gen.plot_sensor_availability(framemap_analysis)

    LOG.info("====== DataQuality 完成 ======")
    LOG.info("报告: %s", report_path)
    LOG.info("图表: %s", quality_output_dir)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    run_data_quality = RUN_DATA_QUALITY
    run_framemap_projection = RUN_FRAMEMAP_PROJECTION

    if not run_data_quality and not run_framemap_projection:
        raise ValueError("RUN_DATA_QUALITY 和 RUN_FRAMEMAP_PROJECTION 不能同时为 0")

    if run_data_quality:
        run_data_quality_inspector()
    if run_framemap_projection:
        run_framemap_alignment_projection()


if __name__ == "__main__":
    main()
