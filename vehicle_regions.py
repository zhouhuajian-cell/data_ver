"""车辆 -> 地区映射（里程分析、参数验证、飞书推送共用）。

不同地区由不同同事负责，同一辆车固定属于一个地区；这里集中维护，
数据库落库时把 vehicle/region 一起存下来，列表就能直接按地区筛选。
"""
from __future__ import annotations

import re

# 车辆 -> 项目/地区（与业务给的对照表一致）
VEHICLE_GROUPS = {
    "HS5-001": "M57项目",
    "MG6-002": "M57项目",
    "R11": "M57项目",
    "HS5-002": "东南亚地区",
    "GS4-002": "东南亚地区",
    "LYNK01-001": "欧洲地区",
    "E5-001": "独联体地区",
    "EX5-001": "南美洲地区",
}
VEHICLE_FALLBACK_GROUP = "未匹配项目/地区"
REGION_OPTIONS = ["M57项目", "东南亚地区", "欧洲地区", "独联体地区", "南美洲地区"]


def vehicle_meta(name: str) -> tuple[str, str]:
    """从路径/目录名里认出车辆，并映射到项目/地区。返回 (车辆, 项目/地区)。"""
    text = str(name or "")
    upper = text.upper()
    for vehicle, group in sorted(
        VEHICLE_GROUPS.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if vehicle.upper() in upper:
            return vehicle, group
    match = re.search(r"[A-Za-z]{1,8}\d{1,4}-\d{1,4}", text)
    if match:
        return match.group(0).upper(), VEHICLE_FALLBACK_GROUP
    return "", VEHICLE_FALLBACK_GROUP


def region_vehicles(region: str) -> list[str]:
    """地区 -> 该地区下的车辆列表；空/全部地区返回空列表（不过滤）。"""
    text = str(region or "").strip()
    if not text or text == "全部地区":
        return []
    return [vehicle for vehicle, group in VEHICLE_GROUPS.items() if group == text]


def dataset_date(name: str) -> str:
    """数据集目录名里的源数据日期（如 ..._20261017 -> 2026-10-17）；兼容 6 位 YYMMDD。"""
    day = dataset_day(name)
    if not day:
        return ""
    return f"{day[0:4]}-{day[4:6]}-{day[6:8]}"


def dataset_day(name: str) -> str:
    """数据集目录名里的源数据日期（8 位，如 20261017）；兼容 6 位 YYMMDD（如 260910）。"""
    text = str(name or "")
    matches = re.findall(r"(20\d{6})", text)
    if matches:
        return matches[-1]
    # 6 位 YYMMDD：260910 -> 20260910（排除 137657 这类编号，月份/日期不合法就跳过）
    for chunk in re.findall(r"(?<!\d)(\d{6})(?!\d)", text):
        year, month, day = int(chunk[:2]), int(chunk[2:4]), int(chunk[4:6])
        if 20 <= year <= 49 and 1 <= month <= 12 and 1 <= day <= 31:
            return f"20{chunk}"
    return ""
