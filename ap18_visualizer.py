"""AP18 前端可视化（形态 B：前后端分离 + 无状态解析 API）。

启动：python ap18_visualizer.py --open

后端只做三件事：
1. 把 web/ 目录下的静态前端（index.html / style.css / app.js）发给浏览器；
2. POST /api/parse：一次请求解析上传的 .pcap / .txt，把解析结果 JSON 返回后立即丢弃，
   服务端不维护任何数据集状态。每个浏览器各自持有并展示自己上传的数据，互不影响。
3. POST /api/mileage：按**服务器端数据目录**计算有效里程（AP18 pcap 走原项目的
   GSOF49 解析器，2M/8M 覆盖来自设备文件名推导的录制时段）。这一步要读服务器
   本地路径，不参与上面的无状态上传流程；不需要时用 --no-mileage 关闭。

旧的 --data-dir / --pcap-dir / --pcap 服务器数据目录模式已移除。
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import mimetypes
import multiprocessing
import os
import random
import re
import socket
import struct
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
import webbrowser
from concurrent.futures import ProcessPoolExecutor
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
import urllib.request

from parse_ap18_pcap import decode_file, satellite_quality_text

# 结果持久化（SQLite）。导入失败也不影响主流程，只是不做缓存/历史。
try:
    import mileage_store

    STORE_ENABLED = True
except Exception:  # noqa: BLE001 - 例如只读文件系统
    mileage_store = None
    STORE_ENABLED = False

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"

MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB
# 只有超过这个体积的响应才值得 gzip：小响应压缩后的收益抵不过 CPU 与延迟。
GZIP_MIN_BYTES = 1 << 20

# 解码进程池：decode_file 是纯 Python CPU 密集循环，ThreadingHTTPServer 的线程
# 会被 GIL 串行化，多核等于闲置；放进进程池才能真正并发。池在 main() 里创建。
DECODER_POOL: ProcessPoolExecutor | None = None
DECODER_WORKERS = 0
# 关闭期间 handler 线程可能还在收请求，submit 会抛 RuntimeError；用这个标志
# 提前退化为就地解析，而不是把异常抛出 do_POST 掐断连接。
SHUTTING_DOWN = False

# 里程分析接口：会按用户给的路径读服务器本地目录，因此单独给一个开关。
# 默认开启（这是内部工具，数据就在本机/网络盘上）；--no-mileage 可关闭。
MILEAGE_ENABLED = True
# 请求体只有路径与容差，给 64 KB 足够，避免被当成上传接口。
MILEAGE_MAX_BODY_BYTES = 64 * 1024

# 参数验证（外参校验 + 数据质检 + 点云反投影）：同样读服务器本地目录，单独开关。
VERIFY_ENABLED = True
VERIFY_MAX_BODY_BYTES = 64 * 1024
# 验证产出（Excel/图表/投影图）默认放在仓库 data/verify 下，可用 --verify-dir 覆盖。
VERIFY_OUTPUT_ROOT = ROOT / "data" / "verify"
THUMB_CACHE_ROOT = ROOT / "data" / "thumbs"

FIELD_DEFS = [
    ("index", "序号", "", "int"),
    ("capture_time", "抓包时间(UTC)", "", "text"),
    ("raw_timestamp", "原始时间戳", "Unix epoch", "text"),
    ("pcap_packet_index", "PCAP包序号", "", "int"),
    ("gps_time", "GPS时间(UTC)", "", "text"),
    ("gps_week", "GPS周", "", "int"),
    ("gps_ms", "GPS周内毫秒", "ms", "int"),
    ("imu_status_text", "IMU对准状态", "", "text"),
    ("gnss_status_text", "GNSS状态", "", "text"),
    ("satellites_used", "搜星数(用于定位)", "颗", "int"),
    ("satellites_source", "搜星数来源", "", "text"),
    ("satellite_quality_text", "搜星评估", "", "text"),
    ("hdop", "HDOP", "", "number"),
    ("hdop_text", "HDOP评估", "", "text"),
    ("differential_age", "差分龄期(s)", "s", "number"),
    ("differential_station_id", "差分参考站ID", "", "text"),
    ("differential_age_text", "差分龄期评估", "", "text"),
    ("latitude", "纬度(deg)", "deg", "number"),
    ("longitude", "经度(deg)", "deg", "number"),
    ("altitude", "高程(m)", "m", "number"),
    ("geoid_separation", "大地水准面差距(m)", "m", "number"),
    ("orthometric_altitude", "海拔(m)", "m", "number"),
    ("gga_altitude", "GGA海拔(m)", "m", "number"),
    ("north_velocity", "北速度(m/s)", "m/s", "number"),
    ("east_velocity", "东速度(m/s)", "m/s", "number"),
    ("down_velocity", "下速度(m/s)", "m/s", "number"),
    ("total_speed", "总速度(m/s)", "m/s", "number"),
    ("roll", "横滚(deg)", "deg", "number"),
    ("pitch", "俯仰(deg)", "deg", "number"),
    ("heading", "航向(deg)", "deg", "number"),
    ("track_angle", "航迹角(deg)", "deg", "number"),
    ("angular_rate_x", "角速度X(deg/s)", "deg/s", "number"),
    ("angular_rate_y", "角速度Y(deg/s)", "deg/s", "number"),
    ("angular_rate_z", "角速度Z(deg/s)", "deg/s", "number"),
    ("acceleration_x", "加速度X(m/s2)", "m/s2", "number"),
    ("acceleration_y", "加速度Y(m/s2)", "m/s2", "number"),
    ("acceleration_z", "加速度Z(m/s2)", "m/s2", "number"),
    ("north_position_rms", "北位置RMS(m)", "m", "number"),
    ("east_position_rms", "东位置RMS(m)", "m", "number"),
    ("down_position_rms", "下位置RMS(m)", "m", "number"),
    ("north_velocity_rms", "北速度RMS(m/s)", "m/s", "number"),
    ("east_velocity_rms", "东速度RMS(m/s)", "m/s", "number"),
    ("down_velocity_rms", "下速度RMS(m/s)", "m/s", "number"),
    ("roll_rms", "横滚RMS(deg)", "deg", "number"),
    ("pitch_rms", "俯仰RMS(deg)", "deg", "number"),
    ("heading_rms", "航向RMS(deg)", "deg", "number"),
]
FIELD_KINDS = {key: kind for key, _label, _unit, kind in FIELD_DEFS}
FIELD_UNITS = {key: unit for key, _label, unit, _kind in FIELD_DEFS}


def parse_value(key: str, value: str):
    if value == "":
        return None
    if FIELD_KINDS[key] == "int":
        return int(value)
    if FIELD_KINDS[key] == "number":
        return float(value)
    return value


def parse_txt_text(text: str) -> list[dict]:
    """解析与旧版一致的 TSV 文本（表头使用字段中文名，跳过 # 行）。"""
    rows: list[dict] = []
    content = (line for line in text.splitlines() if line.strip() and not line.startswith("#"))
    reader = csv.DictReader(content, delimiter="\t")
    for source_row in reader:
        row = {
            key: parse_value(key, source_row.get(label, ""))
            for key, label, _unit, _kind in FIELD_DEFS
        }
        row["satellite_quality_text"] = satellite_quality_text(row.get("satellites_used"))
        rows.append(row)
    return rows


def field_payload() -> list[dict]:
    return [
        {"key": key, "label": label, "unit": unit, "kind": kind}
        for key, label, unit, kind in FIELD_DEFS
    ]


def gps_sort_key(row: dict) -> tuple[int, int]:
    return (int(row.get("gps_week") or 0), int(row.get("gps_ms") or 0))


def json_bytes(value) -> bytes:
    # allow_nan=False 是绊线：GSOF50 某些字段在设备侧可能是全 1 哨兵值，一旦出现
    # NaN/Infinity，默认的 json.dumps 会写出裸 NaN —— 那不是合法 JSON，前端
    # response.json() 会直接抛错并让整批失败。sanitize_json 已把它们归一成 null，
    # 这里再挡一道，宁可服务端 500 也不给浏览器一个解析不了的响应。
    return json.dumps(
        sanitize_json(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def sanitize_json(value):
    """把非有限浮点（NaN/±Infinity）换成 None，其余原样递归返回。"""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: sanitize_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_json(item) for item in value]
    return value


def unique_name(name: str, used: set[str]) -> str:
    """给重复的 basename 加序号，保证同一批里文件名唯一。

    errors 与行上的 _source_file 都按名字归类，两个目录下的同名 .pcap 否则会
    被当成同一个文件：错误记录互相覆盖、两段轨迹合并显示成一段。
    文件夹选择器很容易批量选中不同目录的同名文件，所以必须在这里去重。
    """
    if name not in used:
        used.add(name)
        return name
    stem, suffix = Path(name).stem, Path(name).suffix
    index = 2
    while f"{stem} ({index}){suffix}" in used:
        index += 1
    deduped = f"{stem} ({index}){suffix}"
    used.add(deduped)
    return deduped


def multipart_files(content_type: str, body: bytes) -> list[tuple[str, bytes]]:
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    message = BytesParser(policy=email_policy).parsebytes(header + body)
    if not message.is_multipart():
        raise ValueError("请求不是 multipart/form-data")
    files: list[tuple[str, bytes]] = []
    used_names: set[str] = set()
    for part in message.iter_parts():
        filename = part.get_filename()
        if not filename:
            continue
        safe_name = Path(filename.replace("\\", "/")).name
        if Path(safe_name).suffix.lower() not in (".pcap", ".pacp", ".txt"):
            continue
        payload = part.get_payload(decode=True) or b""
        # 不能用 `if payload:` 过滤：0 字节的文件（空文件、上传中断）会被静默丢掉，
        # 前端只看到"少了一个文件"，既没有 errors 记录也不知道原因。保留空内容，
        # 让它走到解析阶段变成一条明确的错误。
        files.append((unique_name(safe_name, used_names), payload))
    return files


def decode_pcap_worker(path_text: str) -> list[dict]:
    """进程池入口（只返回行，异常照常抛出）。

    必须放在模块顶层（Windows 的 spawn 启动方式用它做 pickle 定位）。
    工作进程自己从磁盘读文件，主进程因此不必把 pcap 字节再 pickling 一份；
    返回值仍要经 IPC 传回，所以 rows 只在父进程里做最终排序与序列化。

    HTTP 服务不直接用这个函数，而用 decode_pcap_task —— 后者会把 traceback 文本
    一起带回来，父进程因此拿得到真正的失败位置（见 error_text）。
    """
    return decode_file(Path(path_text))[0]


def worker_failure() -> tuple[list[dict], str]:
    """工作进程侧的失败出口：把完整 traceback 文本带回父进程。

    父进程栈上只剩 concurrent.futures 的 re-raise 帧，真正的出错位置只在
    _RemoteTraceback（exc.__cause__）里，所以必须在这里就地格式化。
    """
    return [], traceback.format_exc()


# "文件本身有问题"的异常：空文件、magic 不对、截断、不是 pcap。这些是数据问题而不是
# 解析器 bug，界面上需要的只是"哪个文件、为什么"，而不是十几行 Python traceback ——
# 以前前端横幅里塞的就是整段 traceback，一屏全是内部栈帧，真正的原因在最后一行。
# 其它异常（AttributeError/KeyError…）仍然按 traceback 回传：宁可吵一点，也不能让
# 解析器的 bug 伪装成"文件坏了"。
INPUT_ERRORS = (ValueError, OSError, EOFError, struct.error)


def failure_text(exc: BaseException) -> str:
    """单文件失败时给浏览器看的一行文字（含文件名与抛出位置，便于定位）。"""
    if isinstance(exc, INPUT_ERRORS):
        where = ""
        frame = exc.__traceback__
        while frame is not None:  # 最后一帧就是真正抛出的地方（文件名 + 行号）
            where = f"{Path(frame.tb_frame.f_code.co_filename).name}:{frame.tb_lineno}"
            frame = frame.tb_next
        message = clean_error_text(str(exc))
        return clean_error_text(f"{message}（{where}）") if where else message
    return error_text(exc)


def named_error(name: str, failure: str) -> str:
    """拼出"文件名: 原因"，并让文件名只出现一次。

    解析器抛出的消息自带文件名（clean_error_text 已抹掉临时目录与 0000_ 序号前缀），
    这里再拼一次就会出现 "bad.pcap: bad.pcap: ..."。两边的名字不一致时（例如去重后的
    "same (2).pcap"）以调用方的这个名字为准。
    """
    text = clean_error_text(failure)
    prefix = f"{name}: "
    if text.startswith(prefix):
        text = text[len(prefix):]
    return f"{name}: {text}" if text else name


def decode_pcap_task(path_text: str) -> tuple[list[dict], str]:
    """进程池入口：解析 .pcap，返回 (rows, 错误文本)。"""
    try:
        return decode_file(Path(path_text))[0], ""
    except Exception as exc:  # noqa: BLE001 - 单文件失败不影响其它文件，错误文本回传
        return [], failure_text(exc)


def decode_many_in_pool(paths) -> list[tuple[list[dict], str]]:
    """按顺序解码一批 pcap：有进程池就并行，池不可用时就地串行。

    返回顺序必须与 ``paths`` 一一对应——里程统计要靠文件名归属错误信息，
    按完成顺序追加会错位（与 /api/parse 的处理一致）。
    """
    results: list[tuple[list[dict], str]] = []
    futures = []
    if DECODER_POOL is not None and not SHUTTING_DOWN:
        for path_text in paths:
            try:
                futures.append(DECODER_POOL.submit(decode_pcap_task, path_text))
            except RuntimeError:
                # 池已开始关闭：已提交的照常等结果，剩下的退化为就地解析。
                break
    for future in futures:
        try:
            results.append(future.result())
        except Exception as exc:  # noqa: BLE001
            results.append(([], error_text(exc)))
    for path_text in paths[len(results):]:
        results.append(decode_pcap_task(path_text))
    return results


def parse_txt_text_task(text: str) -> tuple[list[dict], str]:
    """进程池入口：解析 TSV 文本内容，返回 (rows, 错误文本)。"""
    try:
        return parse_txt_text(text), ""
    except Exception as exc:  # noqa: BLE001
        return [], failure_text(exc)


def parse_txt_file(path: Path) -> list[dict]:
    """UTF-8（含 BOM）TSV 解析，直接吃磁盘文件，避免大文件整份驻留内存。"""
    return parse_txt_text(path.read_text(encoding="utf-8-sig", errors="replace"))


# 错误文本里会把服务端临时目录的绝对路径（ap18_parse_xxxx）一起带出来：对用户
# 没意义，还会随每个请求变化。统一在回传前抹掉，只留真正有用的文件名与行号。
_TEMP_DIR_RE = re.compile(r"(?:[A-Za-z]:)?[^\s\"']*ap18_parse_[A-Za-z0-9_]+[\\/]?\d*_?")


def clean_error_text(text: str) -> str:
    """抹掉错误文本里的临时目录路径（保留文件名），并压缩空白。"""
    return _TEMP_DIR_RE.sub("", text).strip()


def error_text(exc: BaseException) -> str:
    """把异常压成前端能显示的一行，优先给出真正出错的那几帧。

    进程池会用 _RemoteTraceback 把工作进程的 traceback 挂在 __cause__ 上，
    而 format_exception 的输出是"原因在前、最外层异常在最后"，直接取末尾几行
    只会拿到 concurrent/futures 里的 re-raise 帧。所以有 __cause__ 时优先用它。
    """
    def tail(text: str, count: int = 3) -> str:
        lines = [line.rstrip() for line in text.strip().splitlines() if line.strip()]
        return " ← ".join(lines[-count:])

    cause = exc.__cause__
    if cause is not None:
        frames = tail(str(cause))
        if frames:
            return clean_error_text(f"{exc} ｜ {frames}")
    detail = tail("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    return clean_error_text(f"{exc} ｜ {detail}") if detail else clean_error_text(str(exc))


def accepts_gzip(header_value: str) -> bool:
    """按 Accept-Encoding 的语法判断客户端是否接受 gzip。

    不能用 `"gzip" in value`：`x-gzip` 会被误判成接受，而 `gzip;q=0`（客户端明确
    拒绝 gzip）反而会被当成接受。两者都被这里的逐项解析排除掉。
    """
    for item in header_value.split(","):
        token, _, params = item.partition(";")
        if token.strip().lower() not in ("gzip", "x-gzip"):
            continue
        quality = 1.0
        for parameter in params.split(";"):
            key, _, value = parameter.partition("=")
            if key.strip().lower() == "q":
                try:
                    quality = float(value.strip())
                except ValueError:
                    quality = 0.0
        if quality > 0:
            return True
    return False


# 参数验证（外参校验 + 数据质检 + 点云反投影）：同样读服务器本地目录，单独开关。
VERIFY_ENABLED = True
VERIFY_MAX_BODY_BYTES = 64 * 1024
# 验证产出（Excel/图表/投影图）默认放在仓库 data/verify 下，可用 --verify-dir 覆盖。
VERIFY_OUTPUT_ROOT = ROOT / "data" / "verify"
# 平台上的「交付包」目录：把 zip 放这里即可在页面上按目录/日期下载（--delivery-dir 可改）。
VERIFY_DELIVERY_ROOT = ROOT / "data" / "deliveries"

# ---- 飞书推送：交付图全部人工确认后自动汇总到机器人 --------------------------
# webhook 不写进仓库：优先环境变量 AP18_FEISHU_WEBHOOK，其次 data/feishu_webhook.txt 第一行。
FEISHU_WEBHOOK_ENV = "AP18_FEISHU_WEBHOOK"
FEISHU_WEBHOOK_FILE = ROOT / "data" / "feishu_webhook.txt"
FEISHU_SINK_ENV = "AP18_FEISHU_WEBHOOK_FILE"  # 自测用：把消息写文件而不是真发
# 群消息 @ 人：{"独联体地区": {"name": "赵国政", "open_id": "ou_xxx"}}；
# 自定义机器人只支持用 open_id @ 人（卡片 markdown 语法 <at id=ou_xxx></at>）
FEISHU_MENTIONS_FILE = ROOT / "data" / "feishu_mentions.json"


# 车辆 -> 项目/地区（里程、参数验证、飞书推送共用；数据库里也存这两列）
from vehicle_regions import (  # noqa: E402 - 与里程/验证模块共用同一份映射
    REGION_OPTIONS,
    VEHICLE_FALLBACK_GROUP,
    VEHICLE_GROUPS,
    dataset_date as _dataset_date,
    dataset_day as _dataset_day,
    region_vehicles as _region_vehicles,
    vehicle_meta as _vehicle_meta,
)


def _annotate_region(item: dict, path_key: str = "data_root") -> dict:
    """给历史/扫描结果补上「车辆 + 地区」标注（库里已有就用库里的）。"""
    path = str(item.get(path_key) or "")
    vehicle = str(item.get("vehicle") or "")
    region = str(item.get("region") or "")
    if not region:
        vehicle, region = _vehicle_meta(path)
        if not vehicle:
            region = ""
    item["vehicle"] = vehicle
    item["region"] = region
    return item


_VERIFY_DAY_CACHE_KEY = "verify:daymap"
_EMPTY_VERIFY_STATE = {
    "state": "unverified",
    "available": False,
    "label": "参数验证未完成",
    "total": 0,
    "passed": 0,
    "failed": 0,
    "unknown": 0,
    "verdict": "",
    "updated_at": "",
    "datasets": [],
}


def _verify_day_map() -> dict:
    """参数验证结论按 (地区, 日期) 汇总；30 秒缓存，避免列表里每条都查库。"""
    cached = _cache_get(_VERIFY_DAY_CACHE_KEY, 30.0)
    if cached is not _CACHE_MISS:
        return cached
    data: dict = {}
    if STORE_ENABLED and mileage_store is not None:
        try:
            data = mileage_store.verify_day_states()
        except Exception:  # noqa: BLE001 - 汇总失败不能影响里程数据展示
            data = {}
    _cache_set(_VERIFY_DAY_CACHE_KEY, data)
    return data


_VERIFY_OVERRIDE_CACHE_KEY = "verify:overrides"
_VERIFY_DEFAULT_CACHE_KEY = "verify:region-defaults"


def _verify_overrides() -> dict:
    """人工指定的「地区+日期」结论（优先级最高）。"""
    cached = _cache_get(_VERIFY_OVERRIDE_CACHE_KEY, 30.0)
    if cached is not _CACHE_MISS:
        return cached
    data: dict = {}
    if STORE_ENABLED and mileage_store is not None:
        try:
            data = mileage_store.list_verify_day_overrides()
        except Exception:  # noqa: BLE001
            data = {}
    _cache_set(_VERIFY_OVERRIDE_CACHE_KEY, data)
    return data


def _verify_region_defaults() -> dict:
    cached = _cache_get(_VERIFY_DEFAULT_CACHE_KEY, 30.0)
    if cached is not _CACHE_MISS:
        return cached
    data: dict = {}
    if STORE_ENABLED and mileage_store is not None:
        try:
            data = mileage_store.list_verify_region_defaults()
        except Exception:  # noqa: BLE001
            data = {}
    _cache_set(_VERIFY_DEFAULT_CACHE_KEY, data)
    return data


def _annotate_verify_day(item: dict, path_key: str = "data_root") -> dict:
    """给里程结果补上「参数验证结论 / 当天里程是否可用」。

    优先级：人工指定（override）> 自动验证/人工确认结果 > 地区默认。
    """
    region = str(item.get("region") or "")
    day = str(item.get("day") or "")
    if not day:
        path = str(item.get(path_key) or "")
        day = _dataset_day(path.replace("\\", "/").rsplit("/", 1)[-1]) if path else ""
    if not region or not day:
        item["verify"] = dict(_EMPTY_VERIFY_STATE)
        return item
    key = f"{region}|{day}"
    override = _verify_overrides().get(key)
    if override:
        item["verify"] = dict(override)
        return item
    state = _verify_day_map().get(key)
    if state:
        item["verify"] = dict(state)
        return item
    default = _verify_region_defaults().get(region)
    item["verify"] = dict(default) if default else dict(_EMPTY_VERIFY_STATE)
    return item


def _feishu_webhook_url() -> str:
    url = os.environ.get(FEISHU_WEBHOOK_ENV, "").strip()
    if url:
        return url
    try:
        if FEISHU_WEBHOOK_FILE.is_file():
            for raw in FEISHU_WEBHOOK_FILE.read_text(encoding="utf-8-sig").splitlines():
                line = raw.strip()
                if line and not line.startswith("#"):
                    return line
    except OSError:
        pass
    return ""


FEISHU_RETRY_CODES = {11232}  # 11232: frequency limited（频率限制，等一会儿重试）


def _feishu_payload_ok(body: str) -> tuple[bool, str]:
    """飞书 webhook 返回 HTTP 200 不代表成功，要看 body 的 code/StatusCode 是否为 0。"""
    text = (body or "").strip()
    if not text:
        return True, body
    try:
        data = json.loads(text)
    except ValueError:
        return True, body  # 非 JSON（极罕见）：按成功处理
    if not isinstance(data, dict):
        return True, body
    code = data.get("code", data.get("StatusCode", 0))
    status_code = data.get("StatusCode", code)
    try:
        ok = int(code) == 0 and int(status_code) == 0
    except (TypeError, ValueError):
        ok = True
    return (True, body) if ok else (False, text[:300])


def _post_feishu_payload(payload: dict) -> tuple[bool, str]:
    """发飞书 webhook（自测时写文件）。遇到网关超时/5xx/频率限制自动重试。"""
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sink = os.environ.get(FEISHU_SINK_ENV, "").strip()
    if sink:
        try:
            with open(sink, "a", encoding="utf-8") as fh:
                fh.write(encoded.decode("utf-8") + "\n")
            return True, "test-sink"
        except OSError as exc:
            return False, str(exc)
    url = _feishu_webhook_url()
    if not url:
        return False, "未配置飞书 webhook（AP18_FEISHU_WEBHOOK 或 data/feishu_webhook.txt）"
    attempts = 3
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(
                url,
                data=encoded,
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                body = response.read().decode("utf-8", "replace")
            ok, detail = _feishu_payload_ok(body)
            if ok:
                return True, body[:300]
            last_error = detail
            # 频率限制：等几秒再试；其它业务错误直接返回失败
            try:
                code = int(json.loads(body).get("code", 0))
            except (ValueError, TypeError, AttributeError):
                code = 0
            if code not in FEISHU_RETRY_CODES:
                return False, last_error
        except urllib.error.HTTPError as exc:
            last_error = error_text(exc)
            # 4xx（webhook 失效/参数问题）重试没意义；5xx / 504 网关超时重试
            if 400 <= int(exc.code) < 500:
                return False, last_error
        except Exception as exc:  # noqa: BLE001 - 超时、连接重置等
            last_error = error_text(exc)
        if attempt < attempts:
            time.sleep(2 * attempt)
    if last_error:
        return False, f"飞书网关异常（已重试 {attempts} 次）：{last_error}"
    return False, "推送失败"


def _send_feishu_text(text: str) -> tuple[bool, str]:
    """发飞书文本消息；成功返回 (True, 响应片段)。"""
    return _post_feishu_payload({"msg_type": "text", "content": {"text": text}})


def _send_feishu_card(title: str, body: str, template: str = "green") -> tuple[bool, str]:
    """发飞书卡片消息：带颜色标题栏 + 正文（比纯文本好看）。"""
    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": template,
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": body}},
            ],
        },
    }
    return _post_feishu_payload(payload)


def _feishu_mentions() -> dict[str, dict[str, str]]:
    """读取 data/feishu_mentions.json：{地区: {"name": "...", "open_id": "ou_..."}}。"""
    try:
        payload = json.loads(FEISHU_MENTIONS_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    result: dict[str, dict[str, str]] = {}
    if isinstance(payload, dict):
        for region, value in payload.items():
            if not isinstance(value, dict):
                continue
            open_id = str(value.get("open_id") or "").strip()
            if not open_id:
                continue
            result[str(region).strip()] = {
                "name": str(value.get("name") or "").strip(),
                "open_id": open_id,
            }
    return result


def feishu_region_mention(region: str, *, fallback: str = "") -> str:
    """返回卡片 markdown 里的 @ 片段；未配置返回空串。

    注意：自定义机器人（webhook）只支持用 open_id @人，语法是 <at id=ou_xxx></at>。
    """
    mentions = _feishu_mentions()
    key = str(region or "").strip()
    info = mentions.get(key) or mentions.get(str(fallback or "").strip())
    if not info:
        return ""
    return f"<at id={info['open_id']}></at>"

# ---- 参数验证：交付图浏览 / 人工确认 ---------------------------------------

VERIFY_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def _verify_ts_from_name(name: str) -> float | None:
    """投影图文件名就是时间戳（如 1792230590.188.jpg），用来按时间排序/均匀抽样。"""
    try:
        value = float(Path(name).stem)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _even_sample(items: list, count: int = 5) -> list:
    """按顺序均匀抽 count 个元素（首尾都包含）。"""
    total = len(items)
    if total <= count or count <= 1:
        return list(items)
    step = (total - 1) / (count - 1)
    return [items[round(index * step)] for index in range(count)]


def _iter_verify_images(directory: Path) -> list[Path]:
    try:
        entries = [
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() in VERIFY_IMAGE_EXTS
        ]
    except OSError:
        return []
    return sorted(entries, key=lambda path: path.name)


def _format_epoch(ts: float | None) -> str:
    if ts is None:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except (OSError, OverflowError, ValueError):
        return ""


def _collect_verify_items(base: Path) -> list[dict]:
    """扫描验证产出，返回完整分组（投影组不抽样、含全部图片；质检组全量）。"""
    groups: list[dict] = []
    projections = (
        (base / "projection" / "front", "projection_front", "front（x2f）点云投影叠加图"),
        (base / "projection" / "front7", "projection_front7", "front7（x8d）点云投影叠加图"),
    )
    for folder, key, title in projections:
        items = []
        for path in _iter_verify_images(folder):
            ts = _verify_ts_from_name(path.name)
            items.append(
                {
                    "name": f"projection/{folder.name}/{path.name}",
                    "file": path.name,
                    "timestamp": ts,
                    "time": _format_epoch(ts),
                }
            )
        items.sort(key=lambda item: (item["timestamp"] is None, item["timestamp"] or 0.0, item["name"]))
        groups.append(
            {
                "key": key,
                "title": title,
                "kind": "projection",
                "total": len(items),
                "items": items,
            }
        )
    quality = []
    for path in _iter_verify_images(base / "data_quality"):
        quality.append(
            {
                "name": f"data_quality/{path.name}",
                "file": path.name,
                "timestamp": None,
                "time": "",
            }
        )
    groups.append(
        {
            "key": "quality",
            "title": "数据质检",
            "kind": "quality",
            "total": len(quality),
            "items": quality,
        }
    )
    return groups


def _build_verify_groups(base: Path) -> list[dict]:
    """扫描一次验证产出目录，生成「投影图每组均匀抽 5 张 + 质检图全量」的分组。"""
    groups = _collect_verify_items(base)
    for group in groups:
        if group.get("kind") == "projection":
            group["items"] = _even_sample(group["items"], 5)
    return groups


def _random_sample_verify_items(base: Path, category: str, *, count: int = 5, exclude: list[str] | None = None) -> list[dict]:
    """按时间戳随机抽 count 张投影图（可排除当前已显示的），返回按时间排序的条目。"""
    groups = {group["key"]: group for group in _collect_verify_items(base)}
    group = groups.get(category)
    if not group or group.get("kind") != "projection":
        raise ValueError("只有投影图分组支持重新抽取")
    excluded = set(str(name) for name in (exclude or []))
    candidates = [item for item in group["items"] if item["name"] not in excluded]
    if len(candidates) < count:
        candidates = list(group["items"])
    if not candidates:
        return []
    picked = random.SystemRandom().sample(candidates, min(count, len(candidates)))
    picked.sort(key=lambda item: (item["timestamp"] is None, item["timestamp"] or 0.0, item["name"]))
    return picked



def _resolve_verify_output(root: Path) -> Path:
    """找验证产出目录：优先有投影图的最新任务输出，其次数据目录里平台已有的产出。"""
    cached = None
    try:
        cached = mileage_store.get_verify_run(str(root)) if STORE_ENABLED and mileage_store else None
    except Exception:  # noqa: BLE001
        cached = None
    output_dir = ""
    if isinstance(cached, dict):
        output_dir = str(cached.get("output_dir") or "").strip()
    # 1) 最新任务输出里有投影图 → 用它（刚跑完的完整验证）
    if output_dir:
        candidate = Path(output_dir)
        if (candidate / "projection").is_dir():
            return candidate
    # 2) 数据目录里平台已生成的融合图 → 用它（老数据/平台跑过的）
    legacy = root / "framemap_dataquality_projection"
    if (legacy / "projection").is_dir() or (legacy / "data_quality").is_dir():
        return legacy
    # 3) 任务输出只有质检图（历史 sensor-only 验证）→ 退回它
    if output_dir:
        candidate = Path(output_dir)
        if (candidate / "data_quality").is_dir():
            return candidate
    # 4) 数据目录本身
    if (root / "projection").is_dir() or (root / "data_quality").is_dir():
        return root
    return root / "framemap_dataquality_projection"


def _verify_data_date(root: Path, base: Path | None = None) -> str:
    """数据采集日期（YYYY-MM-DD）：优先 FrameMap 首帧时间戳，其次相机文件名时间戳，最后退回目录名日期。"""
    candidates: list[Path] = []
    for candidate in (root, base):
        if isinstance(candidate, Path) and candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        framemap = candidate / "FrameMap.json"
        if not framemap.is_file():
            continue
        try:
            payload = json.loads(framemap.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        frames = payload.get("frames") if isinstance(payload, dict) else None
        for frame in frames or []:
            raw = frame.get("timestamp") if isinstance(frame, dict) else frame
            try:
                text = _format_epoch(float(raw))
            except (TypeError, ValueError):
                continue
            if text:
                return text[:10]
    if isinstance(root, Path) and root.is_dir():
        try:
            cam_dirs = sorted(
                path for path in root.iterdir()
                if path.is_dir() and path.name.startswith("cam_")
            )
        except OSError:
            cam_dirs = []
        for cam_dir in cam_dirs:
            try:
                names = sorted(path.name for path in cam_dir.iterdir() if path.is_file())
            except OSError:
                continue
            for name in names:
                text = _format_epoch(_verify_ts_from_name(name))
                if text:
                    return text[:10]
    return _dataset_date(str(root)) if isinstance(root, Path) else ""


_CACHE_MISS = object()
_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_LOCK = threading.Lock()


def _cache_get(key: str, ttl: float):
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
    if entry is not None and now - entry[0] <= ttl:
        return entry[1]
    return _CACHE_MISS


def _cache_set(key: str, value: Any) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), value)


def _cache_drop(prefix: str = "") -> None:
    with _CACHE_LOCK:
        for key in [name for name in _CACHE if name.startswith(prefix)]:
            _CACHE.pop(key, None)


def _verify_review_state(item: dict) -> dict | None:
    raw_root = item.get("data_root") or ""
    output_dir = item.get("output_dir") or ""
    cache_key = f"review:{raw_root}|{output_dir}"
    cached = _cache_get(cache_key, 45.0)
    if cached is not _CACHE_MISS:
        return cached
    value = _verify_review_state_uncached(item)
    _cache_set(cache_key, value)
    return value


def _verify_review_state_uncached(item: dict) -> dict | None:
    """统计某个目录的交付图人工确认进度（历史列表、待办统计共用）。"""
    raw_root = item.get("data_root") or ""
    output_dir = item.get("output_dir") or ""
    base = None
    groups: list[dict] = []
    # 和「查看/确认交付图」保持同一份产出：优先 _resolve_verify_output（数据库最新任务/平台产出），
    # 否则会出现确认面板看的是任务目录、验证状态统计的是数据目录，导致「都确认了但仍部分确认」。
    bases: list[Path] = []
    if raw_root:
        try:
            bases.append(_resolve_verify_output(Path(raw_root)))
        except Exception:  # noqa: BLE001
            pass
    if output_dir:
        bases.append(Path(output_dir))
    if raw_root:
        bases.append(Path(raw_root))
    for candidate in bases:
        try:
            if not candidate.is_dir():
                continue
        except OSError:
            continue
        candidate_groups = _build_verify_groups(candidate)
        if sum(len(group.get("items") or []) for group in candidate_groups):
            base, groups = candidate, candidate_groups
            break
    if base is None:
        return None
    total = sum(len(group.get("items") or []) for group in groups)
    if not total:
        return None
    confirmations = {}
    if STORE_ENABLED and mileage_store is not None:
        try:
            confirmations = mileage_store.list_verify_confirmations(raw_root)
        except Exception:  # noqa: BLE001
            confirmations = {}
    passed = rejected = 0
    for group in groups:
        for image in group.get("items") or []:
            key = f"{group['key']}|{image['name']}"
            status = (confirmations.get(key) or {}).get("status")
            if status == "confirmed":
                passed += 1
            elif status == "rejected":
                rejected += 1
    decided = passed + rejected
    if decided == 0:
        state = "pending"
    elif decided >= total:
        state = "done"
    else:
        state = "partial"
    return {
        "state": state,
        "total": total,
        "passed": passed,
        "rejected": rejected,
        "pending": total - decided,
    }


def _delivery_entries(directory: Path) -> list[dict]:
    """列出平台上可下载的交付包（zip + 可选同名 .json 元信息）。"""
    entries: list[dict] = []
    try:
        zips = sorted(
            (path for path in directory.iterdir() if path.is_file() and path.suffix.lower() == ".zip"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return entries
    for path in zips:
        try:
            stat = path.stat()
        except OSError:
            continue
        meta: dict = {}
        sidecar = path.with_suffix(".json")
        if sidecar.is_file():
            try:
                # utf-8-sig：兼容 Windows 记事本/PowerShell 写出的带 BOM 的 json
                meta = json.loads(sidecar.read_text(encoding="utf-8-sig")) or {}
            except (OSError, json.JSONDecodeError):
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        date = str(meta.get("date") or "")
        if not date:
            matches = re.findall(r"(20\d{6})", path.stem)
            date = matches[-1] if matches else ""
        entries.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
                "date": date,
                "data_root": str(meta.get("data_root") or ""),
                "title": str(meta.get("title") or ""),
                "download_url": f"/api/verify/delivery?name={quote(path.name)}",
            }
        )
    return entries


DELIVERY_DIR_NAME = "delivery_package"


def _dataset_delivery_entries(root: Path) -> list[dict]:
    cache_key = f"deliv:{root}"
    cached = _cache_get(cache_key, 120.0)
    if cached is not _CACHE_MISS:
        return cached
    value = _dataset_delivery_entries_uncached(root)
    _cache_set(cache_key, value)
    return value


def _dataset_delivery_entries_uncached(root: Path) -> list[dict]:
    """数据集目录下的交付包：优先 <数据目录>/delivery_package/，其次数据目录根下。"""
    entries: list[dict] = []
    seen: set[str] = set()
    candidates = [(root / DELIVERY_DIR_NAME, DELIVERY_DIR_NAME + "/"), (root, "")]
    for folder, prefix in candidates:
        if not folder.is_dir():
            continue
        try:
            zips = sorted(
                (
                    path
                    for path in folder.iterdir()
                    if path.is_file() and path.suffix.lower() == ".zip"
                ),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            continue
        for path in zips:
            if path.name in seen:
                continue
            seen.add(path.name)
            try:
                stat = path.stat()
            except OSError:
                continue
            matches = re.findall(r"(20\d{6})", path.stem)
            entries.append(
                {
                    "name": path.name,
                    "rel": prefix + path.name,
                    "size": stat.st_size,
                    "mtime": time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)
                    ),
                    "date": matches[-1] if matches else "",
                    "data_root": str(root),
                    "title": root.name,
                    "download_url": (
                        "/api/verify/delivery?data_root="
                        + quote(str(root), safe="")
                        + "&name="
                        + quote(path.name, safe="")
                    ),
                }
            )
    return entries


def _thumb_path(source: Path, width: int) -> Path | None:
    """生成/复用缩略图（缓存到 data/thumbs），失败返回 None 让调用方回退原图。"""
    try:
        stat = source.stat()
    except OSError:
        return None
    key = hashlib.sha1(f"{source}|{stat.st_mtime_ns}|{stat.st_size}|{width}".encode("utf-8")).hexdigest()
    cache_dir = THUMB_CACHE_ROOT
    target = cache_dir / f"{key}.jpg"
    if target.is_file():
        return target
    try:
        from PIL import Image  # noqa: PLC0415 - 只有看图时才需要
    except ImportError:
        return None
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        with Image.open(source) as image:
            image = image.convert("RGB")
            image.thumbnail((width, width * 2))
            image.save(target, "JPEG", quality=80, optimize=True)
        return target
    except (OSError, ValueError):
        return None


def _zip_is_valid(path: Path) -> bool:
    """快速判断 zip 是否可用（只读中央目录，不解压整包）。"""
    try:
        with zipfile.ZipFile(path) as archive:
            return bool(archive.namelist())
    except (OSError, zipfile.BadZipFile):
        return False


def make_handler() -> type[BaseHTTPRequestHandler]:

    MIME = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".ico": "image/x-icon",
    }

    class Handler(BaseHTTPRequestHandler):
        server_version = "AP18Stateless/1.0"
        # HTTP/1.1 + keep-alive：页面一次加载几十个请求，复用连接明显更快
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *_args) -> None:  # noqa: N802
            return

        def request_log(self, message: str) -> None:
            # 只有 --verbose 才说话：默认保持静默，避免高频小请求刷屏。
            # 用 getattr 而不是直接取属性：外部用 make_handler() 起服务时未必会设置
            # 这个标志，缺了它也不该让整个请求以 AttributeError 收场。
            if getattr(self.server, "request_logging", False):
                print(message, flush=True)

        def send_bytes(self, body: bytes, status: int, content_type: str, extra: dict | None = None, gzip_ok: bool = False) -> int:
            headers = dict(extra or {})
            # 回传体积是解析流水线的主要成本之一（12 万行 × 45 字段的 JSON），
            # 而 JSON 文本压缩比通常 8~12×，所以这里按 Accept-Encoding 协商 gzip。
            gzip_requested = accepts_gzip(self.headers.get("Accept-Encoding", ""))
            wire_size = len(body)
            if gzip_ok and gzip_requested and len(body) >= GZIP_MIN_BYTES:
                squashed = gzip.compress(body, 1)
                if len(squashed) < len(body):
                    body = squashed
                    headers["Content-Encoding"] = "gzip"
                    wire_size = -len(body)  # 负数表示"已压缩，绝对值是压缩后字节数"
            if gzip_ok:
                # 两个变体都要声明：只给压缩版加 Vary 会让中间缓存把 identity 版本
                # 当成压缩版本的等价物。当前有 no-store，仍然按规范写全。
                headers["Vary"] = "Accept-Encoding"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
            return wire_size

        def send_file(self, path: Path, content_type: str, *, inline: bool = False) -> None:
            """流式发送文件（大文件不读进内存：交付 zip 有几百 MB）。"""
            try:
                size = path.stat().st_size
            except OSError as exc:
                self.send_error_json(f"读取文件失败：{exc}", 500)
                return
            disposition = "inline" if inline else "attachment"
            # 文件名可能是中文：HTTP 头只能是 latin-1，用 RFC 5987 的 filename*=UTF-8''
            ascii_name = path.name.encode("ascii", "ignore").decode("ascii") or "download.bin"
            disposition_header = (
                f"{disposition}; filename=\"{ascii_name}\"; "
                f"filename*=UTF-8''{quote(path.name, safe='')}"
            )
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Disposition", disposition_header)
            self.end_headers()
            try:
                with path.open("rb") as stream:
                    while True:
                        chunk = stream.read(1024 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def send_json(self, value, status: int = 200, gzip_ok: bool = False) -> int:
            return self.send_bytes(
                json_bytes(value), status, "application/json; charset=utf-8", gzip_ok=gzip_ok
            )

        def send_error_json(self, message: str, status: int = 400) -> None:
            self.send_json({"error": message}, status)

        def serve_static(self, rel_path: str) -> None:
            target = (WEB_DIR / rel_path).resolve()
            root = str(WEB_DIR.resolve())
            if not (str(target) == root or str(target).startswith(root + os.sep)) or not target.is_file():
                self.send_bytes(b"Not found", 404, "text/plain; charset=utf-8")
                return
            ext = target.suffix.lower()
            ctype = MIME.get(ext) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            try:
                body = target.read_bytes()
            except OSError:
                self.send_bytes(b"Read error", 500, "text/plain; charset=utf-8")
                return
            self.send_bytes(body, 200, ctype)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/mileage/history":
                self.handle_mileage_history(parsed)
                return
            if parsed.path == "/api/mileage/record":
                self.handle_mileage_record(parsed)
                return
            if parsed.path == "/api/mileage/stats":
                self.handle_mileage_stats()
                return
            if parsed.path == "/api/verify/tasks":
                self.handle_verify_tasks()
                return
            if parsed.path == "/api/verify/task":
                self.handle_verify_task(parsed)
                return
            if parsed.path == "/api/verify/progress":
                self.handle_verify_progress(parsed)
                return
            if parsed.path == "/api/verify/file":
                self.handle_verify_file(parsed)
                return
            if parsed.path == "/api/verify/source":
                self.handle_verify_source(parsed)
                return
            if parsed.path == "/api/verify/confirmations":
                self.handle_verify_confirmations(parsed)
                return
            if parsed.path == "/api/verify/history":
                self.handle_verify_history(parsed)
                return
            if parsed.path == "/api/todo":
                self.handle_todo(parsed)
                return
            if parsed.path == "/api/dashboard/mileage":
                self.handle_dashboard_mileage(parsed)
                return
            if parsed.path == "/api/dashboard/verify":
                self.handle_dashboard_verify(parsed)
                return
            if parsed.path == "/api/verify/deliveries":
                self.handle_verify_deliveries()
                return
            if parsed.path == "/api/verify/delivery":
                self.handle_verify_delivery(parsed)
                return
            if parsed.path in ("/", "/index.html"):
                self.serve_static("index.html")
                return
            rel = unquote(parsed.path).lstrip("/")
            if rel and not rel.startswith("api/"):
                self.serve_static(rel)
                return
            self.send_bytes(b"Not found", 404, "text/plain; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/parse":
                self.handle_parse()
                return
            if parsed.path == "/api/mileage":
                self.handle_mileage()
                return
            if parsed.path == "/api/mileage/scan":
                self.handle_mileage_scan()
                return
            if parsed.path == "/api/verify/probe":
                self.handle_verify_probe()
                return
            if parsed.path == "/api/verify/run":
                self.handle_verify_run()
                return
            if parsed.path == "/api/verify/overview":
                self.handle_verify_overview()
                return
            if parsed.path == "/api/verify/resample":
                self.handle_verify_resample()
                return
            if parsed.path == "/api/verify/scan":
                self.handle_verify_scan()
                return
            if parsed.path == "/api/verify/confirm":
                self.handle_verify_confirm()
                return
            if parsed.path == "/api/verify/remove":
                self.handle_verify_remove()
                return
            if parsed.path == "/api/verify/review":
                self.handle_verify_review()
                return
            if parsed.path == "/api/verify/notify":
                self.handle_verify_notify()
                return
            if parsed.path == "/api/verify/package":
                self.handle_verify_package()
                return
            self.send_bytes(b"Not found", 404, "text/plain; charset=utf-8")

        def handle_parse(self) -> None:
            # 各阶段独立计时：解码已经并行化了，剩下的是读包/多部分解析/JSON 序列化。
            # 不拆开量就只能看到"总计 1.4s、解码 0.24s"这种没法优化的结论。
            request_started = time.perf_counter()
            read_seconds = 0.0
            parse_seconds = 0.0
            body_size = -1
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length <= 0:
                    raise ValueError("没有收到文件内容")
                if content_length > MAX_UPLOAD_BYTES:
                    raise ValueError("本次上传超过 2 GB 限制")
                body = self.rfile.read(content_length)
                read_seconds = time.perf_counter() - request_started
                body_size = len(body)
                multipart_started = time.perf_counter()
                files = multipart_files(self.headers.get("Content-Type", ""), body)
                parse_seconds = time.perf_counter() - multipart_started
                del body
                if not files:
                    raise ValueError("请选择 .pcap / .pacp 或 .txt 文件")
            except (ValueError, OSError) as exc:
                self.send_error_json(str(exc))
                if getattr(self.server, "request_logging", False):
                    self.request_log(
                        f"[parse] 400 {exc} · 读包 {read_seconds:.2f}s · 多部分 {parse_seconds:.2f}s · "
                        f"请求体 {body_size / 1024 / 1024:.1f} MB"
                    )
                return

            # 解析阶段尽量把活分出去：
            #   .txt  —— 反序列化本身就是纯 Python 循环，同样走进程池；
            #   .pcap —— 交给 decode_pcap_task，多个文件真正并行。
            # 唯一的硬性要求是同一批文件按顺序拿回结果，否则前端的进度、文件列表
            # 和错误归属都会错位，所以这里按上传下标回填而不是按完成顺序追加。
            parallel = DECODER_POOL is not None and not SHUTTING_DOWN
            futures: dict[int, object] = {}
            submitted: set[int] = set()
            # 每个槽位放 (rows, 错误文本)：失败原因来自工作进程自带的 traceback 文本，
            # 父进程的栈上只剩 concurrent.futures 的 re-raise 帧。
            rows_by_index: list[tuple[list[dict], str]] = [([], "") for _ in files]
            with tempfile.TemporaryDirectory(prefix="ap18_parse_") as temp_dir:
                for index, (name, payload) in enumerate(files):
                    temp_path = Path(temp_dir) / f"{index:04d}_{Path(name).name}"
                    try:
                        temp_path.write_bytes(payload)
                    except OSError as exc:
                        rows_by_index[index] = ([], f"临时文件写入失败：{exc}")
                        continue
                    if not parallel:
                        continue
                    try:
                        # 进程池已开始关闭时 submit 会抛 RuntimeError：退化为就地解析，
                        # 而不是让异常逃出 do_POST 把连接直接掐断（浏览器只会显示
                        # "Failed to fetch"，看不到任何原因）。
                        if name.lower().endswith((".pcap", ".pacp")):
                            futures[index] = DECODER_POOL.submit(decode_pcap_task, str(temp_path))
                        else:
                            futures[index] = DECODER_POOL.submit(
                                parse_txt_text_task, temp_path.read_text(encoding="utf-8-sig", errors="replace")
                            )
                        submitted.add(index)
                    except RuntimeError:
                        parallel = False

                if futures:
                    for index, future in futures.items():
                        try:
                            rows, failure = future.result()
                            # future 本身也可能抛（例如池被关闭时任务被取消），
                            # 所以这里同时兜住"任务内失败"和"任务没跑成"两种情况。
                            rows_by_index[index] = (rows, failure or "")
                        except Exception as exc:  # noqa: BLE001 - 单文件失败不影响其它文件
                            rows_by_index[index] = ([], error_text(exc))
                if not parallel:
                    # 没有进程池（或缺进程池时退化为就地解析）：直接在当前线程读盘解析。
                    for index, (name, _payload) in enumerate(files):
                        if index in submitted:
                            continue  # 已经提交出去的任务保留它的结果，避免重复解析
                        temp_path = Path(temp_dir) / f"{index:04d}_{Path(name).name}"
                        if not temp_path.is_file():
                            continue
                        try:
                            rows = (
                                decode_file(temp_path)[0]
                                if name.lower().endswith((".pcap", ".pacp"))
                                else parse_txt_file(temp_path)
                            )
                            rows_by_index[index] = (rows, "")
                        except Exception as exc:  # noqa: BLE001
                            # 与进程池路径保持同一种口径：坏文件只给一行原因，
                            # 不要把 traceback 甩到前端（见 failure_text）。
                            rows_by_index[index] = ([], failure_text(exc))

                decode_seconds = time.perf_counter() - request_started
                parsed_files: list[dict] = []
                parsed_rows: list[dict] = []
                errors: dict[str, str] = {}
                for index, (name, _payload) in enumerate(files):
                    rows, failure = rows_by_index[index]
                    if failure:
                        # failure 可能来自工作进程（已是字符串）或父进程异常对象，
                        # 两条路都统一抹掉临时目录路径、并避免文件名重复出现。
                        errors[name] = named_error(name, failure)
                        continue
                    if not rows:
                        errors[name] = named_error(name, "no decodable records")
                        continue
                    parsed_files.append(
                        {
                            "name": name,
                            "row_count": len(rows),
                            "rms_rows": sum(row.get("north_position_rms") is not None for row in rows),
                        }
                    )
                    parsed_rows.extend({**row, "_source_file": name} for row in rows)
                parsed_rows.sort(key=gps_sort_key)
                serialize_started = time.perf_counter()
                wire_size = self.send_json(
                    {
                        "columns": field_payload(),
                        "files": parsed_files,
                        "errors": errors,
                        "rows": parsed_rows,
                    },
                    gzip_ok=True,
                )
                serialize_seconds = time.perf_counter() - serialize_started
                if getattr(self.server, "request_logging", False):
                    # 负数 wire_size = 已 gzip，绝对值是线上字节数；正数 = 未压缩。
                    if wire_size < 0:
                        transfer = f"gzip {abs(wire_size) / 1024 / 1024:.2f} MB"
                    else:
                        transfer = f"raw {wire_size / 1024 / 1024:.2f} MB"
                    self.request_log(
                        f"[parse] {len(parsed_files)}/{len(files)} 文件 · {len(parsed_rows):,} 行 · "
                        f"并行={DECODER_WORKERS if parallel else 0} · "
                        f"读包 {read_seconds:.2f}s · 多部分 {parse_seconds:.2f}s · "
                        f"解码+合并 {decode_seconds:.2f}s · JSON+gzip {serialize_seconds:.2f}s · "
                        f"总计 {time.perf_counter() - request_started:.2f}s · {transfer}"
                        + (f" · {len(errors)} 个文件失败" if errors else "")
                    )

        def _read_json_payload(self, max_bytes: int | None = None):
            """读取并校验 JSON 请求体；失败时已经回写错误响应，返回 None。"""
            limit = MILEAGE_MAX_BODY_BYTES if max_bytes is None else max_bytes
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                self.send_error_json("Content-Length 不合法")
                return None
            if content_length <= 0:
                self.send_error_json("没有收到请求内容")
                return None
            if content_length > limit:
                self.send_error_json("请求体过大（只接受 JSON 路径与参数）")
                return None
            try:
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self.send_error_json(f"请求体不是合法 JSON：{exc}")
                return None
            if not isinstance(payload, dict):
                self.send_error_json("请求体必须是 JSON 对象")
                return None
            return payload

        def handle_mileage(self) -> None:
            """按服务器端数据目录算有效里程（2M / 8M / GPS / 全覆盖）。

            与 /api/parse 的区别：这一步不接收上传内容，而是读服务端本地目录。
            轨迹仍然走原项目的 GSOF49 解析器（parse_ap18_pcap.decode_file），
            再用 mileage/ 里的计算器算里程，不另写一套算法。
            """
            request_started = time.perf_counter()
            if not MILEAGE_ENABLED:
                self.send_error_json("本次启动已禁用里程分析（--no-mileage）", 403)
                return
            payload = self._read_json_payload()
            if payload is None:
                return

            data_root = payload.get("data_root")
            if not isinstance(data_root, str) or not data_root.strip():
                self.send_error_json("请提供 data_root：服务器上的数据目录路径")
                return
            # 标准化路径：前端批量刷写是「父目录 + /日期」，单日是用户手填，
            # 两者必须落成同一个 key，缓存才能命中。
            data_root = os.path.normpath(data_root.strip())
            refresh = bool(payload.get("refresh"))
            raw_tolerance = payload.get("baseline_tolerance", 0.5)
            try:
                tolerance = float(raw_tolerance)
            except (TypeError, ValueError):
                self.send_error_json("baseline_tolerance 必须是数字")
                return
            if not math.isfinite(tolerance) or tolerance < 0:
                self.send_error_json("baseline_tolerance 必须是非负有限数")
                return

            # 增量：库里已有这一天的结果就直接返回（除非显式 refresh）。
            if not refresh and STORE_ENABLED and mileage_store is not None:
                try:
                    cached = mileage_store.get_analysis(data_root)
                except Exception as exc:  # noqa: BLE001 - 缓存坏了不能影响计算
                    cached = None
                    if getattr(self.server, "request_logging", False):
                        self.request_log(f"[mileage] 读缓存失败：{error_text(exc)}")
                if cached and cached.get("result"):
                    payload_cached = dict(cached["result"])
                    payload_cached["cached"] = True
                    payload_cached["computed_at"] = cached.get("computed_at")
                    payload_cached["data_root"] = cached.get("data_root") or data_root
                    _annotate_region(payload_cached)
                    _annotate_verify_day(payload_cached)
                    self.send_json(payload_cached)
                    if getattr(self.server, "request_logging", False):
                        mileage_cached = payload_cached.get("mileage") or {}
                        self.request_log(
                            f"[mileage] cache hit · 有效里程 "
                            f"{mileage_cached.get('effective_m', 0) / 1000:.3f} km · "
                            f"计算于 {cached.get('computed_at')} · "
                            f"{time.perf_counter() - request_started:.2f}s"
                        )
                    return

            # 延迟导入：里程计算器要往 sys.path 里加 mileage/ 并加载一组扁平模块，
            # 只有真正调用接口时才付这笔开销。
            from ap18_mileage import MileageAnalysisError, analyze_directory

            try:
                result = analyze_directory(
                    data_root,
                    baseline_tolerance_m=tolerance,
                    decode_many=decode_many_in_pool,
                )
            except MileageAnalysisError as exc:
                self.send_error_json(str(exc))
                if getattr(self.server, "request_logging", False):
                    self.request_log(f"[mileage] 400 {exc} · {time.perf_counter() - request_started:.2f}s")
                return
            except Exception as exc:  # noqa: BLE001 - 任何意外都要回一个可读错误
                self.send_error_json(f"里程分析失败：{error_text(exc)}", 500)
                if getattr(self.server, "request_logging", False):
                    self.request_log(f"[mileage] 500 {error_text(exc)}")
                return

            elapsed = time.perf_counter() - request_started
            result["cached"] = False
            if STORE_ENABLED and mileage_store is not None:
                try:
                    result["computed_at"] = mileage_store.save_analysis(
                        data_root, result, elapsed_s=elapsed
                    )
                except Exception as exc:  # noqa: BLE001 - 落库失败不影响本次结果
                    result["computed_at"] = None
                    result["store_error"] = error_text(exc)
                    if getattr(self.server, "request_logging", False):
                        self.request_log(f"[mileage] 写库失败：{error_text(exc)}")

            _annotate_region(result)
            _annotate_verify_day(result)
            self.send_json(result)
            if getattr(self.server, "request_logging", False):
                mileage = result.get("mileage") or {}
                trajectory = result.get("trajectory") or {}
                self.request_log(
                    f"[mileage] ok {trajectory.get('point_count', 0):,} 点 · "
                    f"GPS覆盖 {mileage.get('gps_stable_m', 0) / 1000:.3f} km · "
                    f"2M {mileage.get('covered_2m_m', 0) / 1000:.3f} km · "
                    f"8M {mileage.get('covered_8m_m', 0) / 1000:.3f} km · "
                    f"全覆盖 {mileage.get('both_m', 0) / 1000:.3f} km · "
                    f"总计 {time.perf_counter() - request_started:.2f}s"
                )

        def handle_mileage_scan(self) -> None:
            """扫描父目录下的日期子目录，给「批量刷写」做预检。

            只读目录结构、不解析 pcap，所以很快返回；前端拿到每个日期的
            轨迹/2M/8M 有无后，让用户勾选要刷哪几天，再逐天调用 /api/mileage。
            """
            request_started = time.perf_counter()
            if not MILEAGE_ENABLED:
                self.send_error_json("本次启动已禁用里程分析（--no-mileage）", 403)
                return
            payload = self._read_json_payload()
            if payload is None:
                return
            data_root = payload.get("data_root")
            if not isinstance(data_root, str) or not data_root.strip():
                self.send_error_json("请提供 data_root：要扫描的父目录路径")
                return

            from ap18_mileage import MileageAnalysisError, scan_batch_dates

            try:
                result = scan_batch_dates(data_root.strip())
            except MileageAnalysisError as exc:
                self.send_error_json(str(exc))
                return
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(f"批量扫描失败：{error_text(exc)}", 500)
                return

            # 给每个日期标注「库里是否已有结果」，前端默认只选未算过的（增量刷写）。
            if STORE_ENABLED and mileage_store is not None:
                try:
                    cached_map = mileage_store.cached_day_summaries(
                        os.path.normpath(data_root.strip())
                    )
                except Exception:  # noqa: BLE001
                    cached_map = {}
                for day in result.get("dates") or []:
                    info = cached_map.get(day.get("date"))
                    if info:
                        day["analyzed"] = True
                        day["computed_at"] = info.get("computed_at")
                        day["cached_effective_m"] = info.get("effective_m")
                    else:
                        day["analyzed"] = False
                for day in result.get("dates") or []:
                    _annotate_region(day, "path")
                result["analyzed_count"] = sum(
                    1 for day in (result.get("dates") or []) if day.get("analyzed")
                )

            self.send_json(result)
            if getattr(self.server, "request_logging", False):
                self.request_log(
                    f"[mileage/scan] {result.get('ready_count', 0)}/{result.get('count', 0)} 天可刷 · "
                    f"已算 {result.get('analyzed_count', 0)} 天 · "
                    f"{time.perf_counter() - request_started:.2f}s"
                )

        # ---- 历史记录 / 数据库状态（GET） ---------------------------------

        @staticmethod
        def _query_int(parsed, name: str, default: int, low: int, high: int) -> int:
            try:
                values = parse_qs(parsed.query).get(name)
                value = int(values[0]) if values else default
            except (TypeError, ValueError):
                return default
            return max(low, min(high, value))

        def _require_store(self) -> bool:
            if not STORE_ENABLED or mileage_store is None:
                self.send_error_json("数据库未启用（mileage_store 导入失败）", 503)
                return False
            return True

        def handle_mileage_history(self, parsed) -> None:
            """检索已保存的结果：按路径或日期关键字搜索，分页返回。"""
            if not self._require_store():
                return
            query = (parse_qs(parsed.query).get("q", [""])[0] or "").strip()
            data_root = (parse_qs(parsed.query).get("data_root", [""])[0] or "").strip()
            region = (parse_qs(parsed.query).get("region", [""])[0] or "").strip()
            days = self._query_int(parsed, "days", 0, 0, 3650)
            date = (parse_qs(parsed.query).get("date", [""])[0] or "").strip()
            date_from = (parse_qs(parsed.query).get("date_from", [""])[0] or "").strip()
            date_to = (parse_qs(parsed.query).get("date_to", [""])[0] or "").strip()
            limit = self._query_int(parsed, "limit", 100, 1, 500)
            offset = self._query_int(parsed, "offset", 0, 0, 100000)
            try:
                payload = mileage_store.list_history(
                    query=query,
                    data_root=os.path.normpath(data_root) if data_root else "",
                    region=region,
                    days=days,
                    date=date,
                    date_from=date_from,
                    date_to=date_to,
                    limit=limit,
                    offset=offset,
                )
                for item in payload.get("items") or []:
                    _annotate_region(item)
                    _annotate_verify_day(item)
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(f"查询历史失败：{error_text(exc)}", 500)
                return
            self.send_json(payload)

        def handle_mileage_record(self, parsed) -> None:
            """按记录 id 取完整结果（历史列表点「载入」用）。"""
            if not self._require_store():
                return
            record_id = self._query_int(parsed, "id", 0, 0, 10**9)
            if record_id <= 0:
                self.send_error_json("缺少 id")
                return
            try:
                record = mileage_store.get_history_result(record_id)
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(f"读取历史结果失败：{error_text(exc)}", 500)
                return
            if record is None:
                self.send_error_json("记录不存在", 404)
                return
            result = record.get("result")
            if isinstance(result, dict):
                _annotate_region(result, path_key="data_root")
                _annotate_verify_day(result, path_key="data_root")
                if "data_root" in record and not result.get("data_root"):
                    result["data_root"] = record.get("data_root")
            if "data_root" in record:
                _annotate_region(record)
                _annotate_verify_day(record)
            self.send_json(record)

        def handle_mileage_stats(self) -> None:
            if not self._require_store():
                return
            try:
                self.send_json(mileage_store.stats())
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(f"读取数据库状态失败：{error_text(exc)}", 500)

        # ---- 参数验证（外参校验 + 数据质检 + 点云反投影） --------------------

        @staticmethod
        def _load_verifier():
            """延迟导入：验证模块会 import cv2/laspy/scipy/matplotlib，首次调用才付这笔开销。"""
            import verifier  # noqa: PLC0415

            return verifier

        @staticmethod
        def _query_text(parsed, name: str, default: str = "") -> str:
            values = parse_qs(parsed.query).get(name)
            return (values[0] if values else default) or default

        def handle_verify_probe(self) -> None:
            """参数验证：目录预检（外参/相机/雷达/pose 是否齐全）。"""
            if not VERIFY_ENABLED:
                self.send_error_json("本次启动已禁用参数验证（--no-verify）", 403)
                return
            payload = self._read_json_payload(VERIFY_MAX_BODY_BYTES)
            if payload is None:
                return
            raw = payload.get("data_root")
            if not isinstance(raw, str) or not raw.strip():
                self.send_error_json("请提供 data_root：服务器上的数据目录路径")
                return
            try:
                verifier = self._load_verifier()
                root = verifier.resolve_data_root(raw)
                dataset = verifier.probe_dataset(root)
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(f"参数验证目录预检失败：{error_text(exc)}", 500)
                return
            dataset["requested_root"] = raw
            self.send_json({"ok": True, "dataset": dataset})

        def handle_verify_run(self) -> None:
            """参数验证：创建后台任务（串行执行，前端轮询进度）。"""
            if not VERIFY_ENABLED:
                self.send_error_json("本次启动已禁用参数验证（--no-verify）", 403)
                return
            payload = self._read_json_payload(VERIFY_MAX_BODY_BYTES)
            if payload is None:
                return
            raw = payload.get("data_root")
            if not isinstance(raw, str) or not raw.strip():
                self.send_error_json("请提供 data_root：服务器上的数据目录路径")
                return
            try:
                verifier = self._load_verifier()
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(
                    "参数验证模块不可用（依赖缺失？需要 opencv/laspy/pyyaml/scipy/matplotlib/pandas/openpyxl）："
                    + error_text(exc),
                    500,
                )
                return

            root = verifier.resolve_data_root(raw)
            refresh = bool(payload.get("refresh"))
            # 历史命中：这个目录验证过就直接读库，不重复分析（除非勾了强制重验）
            if not refresh and STORE_ENABLED and mileage_store is not None:
                try:
                    cached = mileage_store.get_verify_run(str(root))
                except Exception as exc:  # noqa: BLE001 - 读历史失败就继续跑，不影响功能
                    cached = None
                    if getattr(self.server, "request_logging", False):
                        self.request_log(f"[verify/run] 读历史失败：{error_text(exc)}")
                if cached and cached.get("task"):
                    task_dict = dict(cached["task"])
                    task_id = verifier.register_cached_task(task_dict)
                    dataset = dict(task_dict.get("dataset") or {})
                    dataset["requested_root"] = raw
                    self.send_json(
                        {
                            "ok": True,
                            "cached": True,
                            "task_id": task_id,
                            "dataset": dataset,
                            "output_root": cached.get("output_dir") or task_dict.get("output_root", ""),
                            "computed_at": cached.get("updated_at"),
                            "task": task_dict,
                        }
                    )
                    if getattr(self.server, "request_logging", False):
                        self.request_log(
                            f"[verify/run] cache hit · {root} · 计算于 {cached.get('updated_at')}"
                        )
                    return

            dataset = verifier.probe_dataset(root)
            dataset["requested_root"] = raw
            if not dataset.get("ready"):
                problems = dataset.get("problems") or ["数据目录不可用于验证"]
                self.send_error_json("；".join(problems))
                return

            def _int(name: str, default: int) -> int:
                try:
                    return int(payload.get(name, default))
                except (TypeError, ValueError):
                    return default

            only_cameras = payload.get("only_cameras")
            if isinstance(only_cameras, str):
                only_cameras = [item.strip() for item in only_cameras.split(",") if item.strip()]
            elif isinstance(only_cameras, list):
                only_cameras = [str(item).strip() for item in only_cameras if str(item).strip()]
            else:
                only_cameras = None

            frame_end = payload.get("frame_end")
            try:
                frame_end = int(frame_end) if frame_end not in (None, "") else None
            except (TypeError, ValueError):
                frame_end = None

            options = verifier.VerifyOptions(
                run_data_quality=bool(payload.get("run_data_quality", True)),
                run_projection=bool(payload.get("run_projection", True)),
                # 默认开启点云反投影：否则管线只做 sensor status，不产出融合图（确认面板会读不到）
                enable_pointcloud_reprojection=bool(
                    payload.get("enable_pointcloud_reprojection", True)
                ),
                force_regenerate_framemap=bool(payload.get("force_regenerate_framemap", False)),
                frame_start=max(0, _int("frame_start", 100)),
                frame_end=frame_end,
                frame_step=max(1, _int("frame_step", 20)),
                only_cameras=only_cameras or None,
            )

            try:
                VERIFY_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
                task_dir = VERIFY_OUTPUT_ROOT / (
                    f"task_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
                )
                task_id = verifier.create_task(root, task_dir, options)
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(f"创建参数验证任务失败：{error_text(exc)}", 500)
                return

            self.send_json(
                {
                    "ok": True,
                    "task_id": task_id,
                    "dataset": dataset,
                    "output_root": str(task_dir),
                }
            )
            if getattr(self.server, "request_logging", False):
                self.request_log(
                    f"[verify] task {task_id} started · {root} · 质检={options.run_data_quality} "
                    f"反投影={options.run_projection} 点云重投影={options.enable_pointcloud_reprojection}"
                )

        def handle_verify_tasks(self) -> None:
            try:
                verifier = self._load_verifier()
                self.send_json({"ok": True, "tasks": verifier.list_tasks()})
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(f"读取验证任务失败：{error_text(exc)}", 500)

        def handle_verify_task(self, parsed) -> None:
            task_id = self._query_text(parsed, "id")
            try:
                verifier = self._load_verifier()
                task = verifier.get_task(task_id) if task_id else None
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(f"读取验证任务失败：{error_text(exc)}", 500)
                return
            if task is None:
                self.send_error_json("任务不存在或已过期", 404)
                return
            self.send_json(
                {
                    "ok": True,
                    "task": verifier.result_to_dict(task),
                    "progress": verifier.get_progress(task_id),
                }
            )

        def handle_verify_progress(self, parsed) -> None:
            task_id = self._query_text(parsed, "id")
            try:
                verifier = self._load_verifier()
                task = verifier.get_task(task_id) if task_id else None
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(f"读取验证进度失败：{error_text(exc)}", 500)
                return
            if task is None:
                self.send_error_json("任务不存在或已过期", 404)
                return
            self.send_json({"ok": True, "progress": verifier.get_progress(task_id)})

        def handle_verify_file(self, parsed) -> None:
            """下载/预览验证产出（只允许任务输出目录内的文件）。"""
            task_id = self._query_text(parsed, "id")
            name = self._query_text(parsed, "name")
            if not task_id or not name:
                self.send_error_json("缺少 id 或 name")
                return
            try:
                verifier = self._load_verifier()
                task = verifier.get_task(task_id)
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(f"读取验证任务失败：{error_text(exc)}", 500)
                return
            if task is None:
                self.send_error_json("任务不存在或已过期", 404)
                return
            root = Path(task.output_root).resolve()
            target = (root / name).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                self.send_error_json("非法的文件路径", 403)
                return
            if not target.is_file():
                self.send_error_json("文件不存在", 404)
                return
            content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            if target.suffix.lower() == ".xlsx":
                content_type = (
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
            inline = content_type.startswith(("image/", "text/", "application/json"))
            self.send_file(target, content_type, inline=inline)

        # ---- 交付图浏览 / 人工确认 / 交付包下载 ---------------------------

        def _verify_base_and_root(self, payload: dict):
            """确定「产出目录」和用于关联确认状态的 data_root 文本。"""
            task_id = str(payload.get("task_id") or "").strip()
            if task_id:
                try:
                    verifier = self._load_verifier()
                    task = verifier.get_task(task_id)
                except Exception as exc:  # noqa: BLE001
                    raise ValueError(f"读取验证任务失败：{error_text(exc)}") from exc
                if task is None:
                    raise ValueError("任务不存在或已过期")
                base = Path(task.output_root)
                root_text = str((task.dataset or {}).get("data_root") or base)
                return base, root_text
            raw = payload.get("data_root")
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError("请提供 data_root 或 task_id")
            verifier = self._load_verifier()
            root = verifier.resolve_data_root(raw)
            return _resolve_verify_output(root), str(root)

        def handle_verify_scan(self) -> None:
            """批量验证：扫描父目录下的可验证数据集（含 parameter_yaml 或已有产出）。"""
            if not VERIFY_ENABLED:
                self.send_error_json("本次启动已禁用参数验证（--no-verify）", 403)
                return
            payload = self._read_json_payload(VERIFY_MAX_BODY_BYTES)
            if payload is None:
                return
            raw = payload.get("data_root")
            if not isinstance(raw, str) or not raw.strip():
                self.send_error_json("请提供 data_root：要扫描的父目录")
                return
            try:
                verifier = self._load_verifier()
                root = verifier.resolve_data_root(raw)
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(f"扫描失败：{error_text(exc)}", 500)
                return
            if not root.exists():
                self.send_error_json(f"目录不存在或不可访问: {root}")
                return
            if not root.is_dir():
                self.send_error_json(f"不是目录: {root}")
                return
            try:
                result = verifier.scan_verify_datasets(root)
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(f"扫描失败：{error_text(exc)}", 500)
                return

            # 标注「库里是否已验证过」：批量默认跳过已分析的目录（增量）
            if STORE_ENABLED and mileage_store is not None:
                try:
                    runs = mileage_store.list_verify_runs()
                except Exception:  # noqa: BLE001
                    runs = {}
                analyzed = 0
                for dataset in result.get("datasets") or []:
                    key = os.path.normpath(dataset.get("path") or "").replace("\\", "/")
                    info = runs.get(key)
                    if info:
                        dataset["analyzed"] = True
                        dataset["analyzed_at"] = info.get("updated_at")
                        dataset["analyzed_status"] = info.get("status")
                        dataset["analyzed_verdict"] = info.get("verdict")
                        analyzed += 1
                    else:
                        dataset["analyzed"] = False
                result["analyzed_count_db"] = analyzed
                result["ready_count"] = sum(
                    1 for item in (result.get("datasets") or [])
                    if item.get("ready_hint") and not item.get("analyzed")
                )
            for dataset in result.get("datasets") or []:
                _annotate_region(dataset, "path")
            self.send_json({"ok": True, **result})

        def handle_verify_overview(self) -> None:
            """交付图总览：投影图每组按时间戳均匀抽 5 张，质检图全展示。"""
            if not VERIFY_ENABLED:
                self.send_error_json("本次启动已禁用参数验证（--no-verify）", 403)
                return
            payload = self._read_json_payload(VERIFY_MAX_BODY_BYTES)
            if payload is None:
                return
            try:
                base, root_text = self._verify_base_and_root(payload)
            except ValueError as exc:
                self.send_error_json(str(exc))
                return
            if not base.is_dir():
                self.send_error_json(
                    f"没有找到验证产出目录: {base}（先跑一次验证，或确认数据目录下有 framemap_dataquality_projection）"
                )
                return
            groups = _build_verify_groups(base)
            confirmations = {}
            if STORE_ENABLED and mileage_store is not None:
                try:
                    confirmations = mileage_store.list_verify_confirmations(root_text)
                except Exception as exc:  # noqa: BLE001
                    if getattr(self.server, "request_logging", False):
                        self.request_log(f"[verify/overview] 读确认状态失败：{error_text(exc)}")
            self.send_json(
                {
                    "ok": True,
                    "data_root": root_text,
                    "output_dir": str(base),
                    "data_date": _verify_data_date(Path(root_text), base),
                    "groups": groups,
                    "confirmations": confirmations,
                }
            )

        def handle_verify_resample(self) -> None:
            """重新抽取一组投影图（按时间戳随机抽 5 张），用于避开空旷/无明显标志物的时段。"""
            if not VERIFY_ENABLED:
                self.send_error_json("本次启动已禁用参数验证（--no-verify）", 403)
                return
            payload = self._read_json_payload(VERIFY_MAX_BODY_BYTES)
            if payload is None:
                return
            try:
                base, root_text = self._verify_base_and_root(payload)
            except ValueError as exc:
                self.send_error_json(str(exc))
                return
            if not base.is_dir():
                self.send_error_json(f"没有找到验证产出目录: {base}")
                return
            category = str(payload.get("category") or "").strip()
            if not category:
                self.send_error_json("缺少 category")
                return
            raw_exclude = payload.get("exclude")
            exclude: list[str] = []
            if isinstance(raw_exclude, list):
                exclude = [str(item) for item in raw_exclude]
            elif isinstance(raw_exclude, str) and raw_exclude:
                exclude = [raw_exclude]
            try:
                items = _random_sample_verify_items(base, category, count=5, exclude=exclude)
            except ValueError as exc:
                self.send_error_json(str(exc))
                return
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(f"重新抽取失败：{error_text(exc)}", 500)
                return
            groups = {group["key"]: group for group in _collect_verify_items(base)}
            total = (groups.get(category) or {}).get("total", 0)
            self.send_json(
                {
                    "ok": True,
                    "data_root": root_text,
                    "category": category,
                    "total": total,
                    "items": items,
                }
            )

        def handle_verify_source(self, parsed) -> None:
            """预览/下载已有验证产出里的图片（限定在产出目录内）。"""
            raw = self._query_text(parsed, "data_root")
            task_id = self._query_text(parsed, "task_id")
            name = self._query_text(parsed, "path")
            if not name:
                self.send_error_json("缺少 path")
                return
            try:
                base, _root_text = self._verify_base_and_root(
                    {"data_root": raw, "task_id": task_id}
                )
            except ValueError as exc:
                self.send_error_json(str(exc))
                return
            base = base.resolve()
            target = (base / name).resolve()
            try:
                target.relative_to(base)
            except ValueError:
                self.send_error_json("非法的文件路径", 403)
                return
            if not target.is_file():
                self.send_error_json("文件不存在", 404)
                return
            content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            # 缩略图：确认面板一次要显示 15 张大图，直接传原图会明显卡顿
            width_text = self._query_text(parsed, "w")
            if width_text and content_type.startswith("image/"):
                try:
                    width = max(120, min(1600, int(width_text)))
                except ValueError:
                    width = 720
                thumb = _thumb_path(target, width)
                if thumb is not None:
                    try:
                        body = thumb.read_bytes()
                    except OSError:
                        thumb = None
                    else:
                        self.send_bytes(body, 200, "image/jpeg")
                        return
            try:
                body = target.read_bytes()
            except OSError as exc:
                self.send_error_json(f"读取文件失败：{exc}", 500)
                return
            self.send_bytes(body, 200, content_type)

        def handle_verify_history(self, parsed) -> None:
            """参数验证历史（库中已有结果）：分页 + 关键字筛选。"""
            if not self._require_store():
                return
            query = (parse_qs(parsed.query).get("q", [""])[0] or "").strip()
            region = (parse_qs(parsed.query).get("region", [""])[0] or "").strip()
            days = self._query_int(parsed, "days", 0, 0, 3650)
            date = (parse_qs(parsed.query).get("date", [""])[0] or "").strip()
            date_from = (parse_qs(parsed.query).get("date_from", [""])[0] or "").strip()
            date_to = (parse_qs(parsed.query).get("date_to", [""])[0] or "").strip()
            review_filter = (parse_qs(parsed.query).get("review", [""])[0] or "").strip().lower()
            limit = self._query_int(parsed, "limit", 10, 1, 100)
            offset = self._query_int(parsed, "offset", 0, 0, 100000)
            if review_filter not in ("done", "pending", "passed", "rejected"):
                review_filter = ""
            try:
                if review_filter:
                    # 需要先算每条的人工确认状态再过滤，所以一次取全量（上限 2000）后切片
                    all_payload = mileage_store.list_verify_runs_page(
                        query=query,
                        region=region,
                        days=days,
                        date=date,
                        date_from=date_from,
                        date_to=date_to,
                        limit=2000,
                        offset=0,
                    )
                    matched: list[dict] = []
                    for item in all_payload.get("items") or []:
                        _annotate_region(item)
                        try:
                            item["review"] = self._verify_review_summary(item)
                        except Exception:  # noqa: BLE001
                            item["review"] = None
                        review = item.get("review") or {}
                        state = review.get("state")
                        rejected = int(review.get("rejected") or 0)
                        if review_filter == "done" and state == "done":
                            matched.append(item)
                        elif review_filter == "pending" and review and state != "done":
                            matched.append(item)
                        elif review_filter == "passed" and state == "done" and rejected == 0:
                            matched.append(item)
                        elif review_filter == "rejected" and state == "done" and rejected > 0:
                            matched.append(item)
                    total = len(matched)
                    payload = {
                        "ok": True,
                        "total": total,
                        "limit": limit,
                        "offset": offset,
                        "items": matched[offset : offset + limit],
                    }
                else:
                    payload = mileage_store.list_verify_runs_page(
                        query=query,
                        region=region,
                        days=days,
                        date=date,
                        date_from=date_from,
                        date_to=date_to,
                        limit=limit,
                        offset=offset,
                    )
                    # 每条历史附上「车辆/地区」和「参数验证」人工确认进度
                    for item in payload.get("items") or []:
                        _annotate_region(item)
                        try:
                            item["review"] = self._verify_review_summary(item)
                        except Exception:  # noqa: BLE001
                            item["review"] = None
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(f"读取参数验证历史失败：{error_text(exc)}", 500)
                return
            # 每条历史附上「推送时间」= 结果推送飞书的时间（没推过为空）
            try:
                notified_map = mileage_store.list_verify_notifications()
            except Exception:  # noqa: BLE001
                notified_map = {}
            try:
                reviews_map = mileage_store.list_verify_reviews()
            except Exception:  # noqa: BLE001
                reviews_map = {}
            for item in payload.get("items") or []:
                root_key = item.get("data_root") or ""
                item["notified_at"] = notified_map.get(root_key, "")
                # 「验证时间」：优先用「确认」留痕（同一数据多次确认取最新），
                # 其次用评完的最后一次人工确认时间，再退回本次分析完成时间
                saved = reviews_map.get(root_key) or {}
                verified_at = str(saved.get("updated_at") or "")
                review = item.get("review") or {}
                if not verified_at and review.get("state") == "done":
                    try:
                        confirmations = mileage_store.list_verify_confirmations(root_key)
                        stamps = [
                            str(entry.get("updated_at") or "")
                            for entry in confirmations.values()
                            if entry.get("status") in ("confirmed", "rejected")
                        ]
                        verified_at = max(stamps) if stamps else ""
                    except Exception:  # noqa: BLE001
                        verified_at = ""
                item["verified_at"] = verified_at
            # 每条历史附上它对应的交付包：优先数据目录下的 delivery_package（按天放，不占服务器盘）
            legacy_map: dict[str, dict] = {}
            try:
                legacy_map = {
                    (entry.get("data_root") or "").replace("\\", "/").rstrip("/"): entry
                    for entry in _delivery_entries(VERIFY_DELIVERY_ROOT)
                    if entry.get("data_root")
                }
            except Exception:  # noqa: BLE001
                legacy_map = {}
            for item in payload.get("items") or []:
                key = (item.get("data_root") or "").replace("\\", "/").rstrip("/")
                entry = None
                dataset_dir = Path(item.get("data_root") or "")
                if dataset_dir.is_dir():
                    found = _dataset_delivery_entries(dataset_dir)
                    entry = found[0] if found else None
                if entry is None:
                    entry = legacy_map.get(key)
                if entry:
                    item["delivery_name"] = entry.get("name") or ""
                    item["delivery_size"] = entry.get("size") or 0
                    item["delivery_date"] = entry.get("date") or ""
                    item["delivery_url"] = entry.get("download_url") or ""
                else:
                    item["delivery_name"] = ""
                    item["delivery_size"] = 0
                    item["delivery_url"] = ""
            self.send_json({"ok": True, **payload})

        def _verify_review_summary(self, item: dict) -> dict | None:
            """统计某个历史目录的交付图人工确认进度（复用模块级实现）。"""
            return _verify_review_state(item)

        def handle_todo(self, parsed) -> None:
            """各项目待办：每个地区待人工确认的参数验证数量 + 里程进度。"""
            if not self._require_store():
                return
            region_filter = (parse_qs(parsed.query).get("region", [""])[0] or "").strip()
            todo_cache_key = f"todo:{region_filter}"
            cached_todo = _cache_get(todo_cache_key, 20.0)
            if cached_todo is not _CACHE_MISS:
                self.send_json(cached_todo)
                return
            try:
                totals = mileage_store.region_totals()
                runs = mileage_store.list_verify_runs_for_region("", limit=2000)
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(f"读取待办失败：{error_text(exc)}", 500)
                return
            by_region: dict[str, list[dict]] = {}
            for item in runs:
                raw_root = item.get("data_root") or ""
                name = raw_root.rstrip("/").replace("\\", "/").rsplit("/", 1)[-1]
                region = item.get("region") or ""
                if not region:
                    _vehicle, region = _vehicle_meta(raw_root)
                    item["region"] = region if _vehicle else ""
                item["name"] = name
                try:
                    item["review"] = _verify_review_state(item)
                except Exception:  # noqa: BLE001
                    item["review"] = None
                by_region.setdefault(item.get("region") or "", []).append(item)

            regions: list[dict] = []
            for region in REGION_OPTIONS:
                items = by_region.get(region, [])
                run_info = totals["runs"].get(region, {})
                day_info = totals["days"].get(region, {})
                if not items and not day_info:
                    continue
                pending_items = [
                    {
                        "name": item["name"],
                        "data_root": item["data_root"],
                        "updated_at": item.get("updated_at") or "",
                        "review": item.get("review"),
                    }
                    for item in items
                    if item.get("review") and item["review"]["state"] != "done"
                ]
                pending = len(pending_items)
                done = sum(
                    1 for item in items if item.get("review") and item["review"]["state"] == "done"
                )
                last_at = max(
                    [item.get("updated_at") or "" for item in items]
                    + [day_info.get("last_at") or "", run_info.get("last_at") or ""]
                )
                regions.append(
                    {
                        "region": region,
                        "vehicles": _region_vehicles(region),
                        "verify_runs": len(items),
                        "pending_confirm": pending,
                        "review_done": done,
                        "mileage_days": day_info.get("days", 0),
                        "effective_m": day_info.get("effective_m", 0) or 0,
                        "last_at": last_at or "",
                        "pending_items": pending_items[:5],
                    }
                )
            if region_filter and region_filter != "全部地区":
                regions = [item for item in regions if item["region"] == region_filter]
            todo_payload = {
                "ok": True,
                "regions": regions,
                "totals": {
                    "verify_runs": sum(item["verify_runs"] for item in regions),
                    "pending_confirm": sum(item["pending_confirm"] for item in regions),
                    "mileage_days": sum(item["mileage_days"] for item in regions),
                },
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            _cache_set(todo_cache_key, todo_payload)
            self.send_json(todo_payload)

        def handle_dashboard_mileage(self, parsed) -> None:
            """里程看板：总览 + 按项目汇总 + 最近日期趋势。"""
            if not self._require_store():
                return
            region = (parse_qs(parsed.query).get("region", [""])[0] or "").strip()
            days = self._query_int(parsed, "days", 90, 0, 3650)
            date_from = (parse_qs(parsed.query).get("date_from", [""])[0] or "").strip()
            date_to = (parse_qs(parsed.query).get("date_to", [""])[0] or "").strip()
            cache_key = f"dashboard:mileage:{region}:{days}:{date_from}:{date_to}"
            cached = _cache_get(cache_key, 20.0)
            if cached is not _CACHE_MISS:
                self.send_json(cached)
                return
            try:
                payload = mileage_store.dashboard_mileage(
                    days=days, region=region, date_from=date_from, date_to=date_to
                )
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(f"读取里程看板失败：{error_text(exc)}", 500)
                return
            result = {
                "ok": True,
                "region": region,
                "days": days,
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                **payload,
            }
            _cache_set(cache_key, result)
            self.send_json(result)

        def handle_dashboard_verify(self, parsed) -> None:
            """参数验证看板：总览 + 按项目汇总 + 最近验证记录。"""
            if not self._require_store():
                return
            region = (parse_qs(parsed.query).get("region", [""])[0] or "").strip()
            days = self._query_int(parsed, "days", 90, 0, 3650)
            date_from = (parse_qs(parsed.query).get("date_from", [""])[0] or "").strip()
            date_to = (parse_qs(parsed.query).get("date_to", [""])[0] or "").strip()
            cache_key = f"dashboard:verify:{region}:{days}:{date_from}:{date_to}"
            cached = _cache_get(cache_key, 20.0)
            if cached is not _CACHE_MISS:
                self.send_json(cached)
                return
            try:
                page = mileage_store.list_verify_runs_page(
                    query="",
                    region=region,
                    days=days,
                    date_from=date_from,
                    date_to=date_to,
                    limit=2000,
                    offset=0,
                )
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(f"读取参数验证看板失败：{error_text(exc)}", 500)
                return
            items = page.get("items") or []
            totals = {"datasets": len(items), "done": 0, "passed": 0, "rejected": 0, "pending": 0, "no_images": 0}
            buckets: dict[str, dict] = {}
            recent: list[dict] = []
            for item in items:
                _annotate_region(item)
                try:
                    review = _verify_review_state(item)
                except Exception:  # noqa: BLE001
                    review = None
                state = (review or {}).get("state") or "unknown"
                rejected_count = int((review or {}).get("rejected") or 0)
                region_name = item.get("region") or "未匹配项目/地区"
                bucket = buckets.setdefault(
                    region_name,
                    {
                        "region": region_name,
                        "datasets": 0,
                        "done": 0,
                        "passed": 0,
                        "rejected": 0,
                        "pending": 0,
                        "no_images": 0,
                        "vehicles": set(),
                        "last_at": "",
                    },
                )
                bucket["datasets"] += 1
                if item.get("vehicle"):
                    bucket["vehicles"].add(item["vehicle"])
                bucket["last_at"] = max(bucket["last_at"], item.get("updated_at") or "")
                if not review:
                    totals["no_images"] += 1
                    bucket["no_images"] += 1
                elif state == "done":
                    totals["done"] += 1
                    bucket["done"] += 1
                    if rejected_count > 0:
                        totals["rejected"] += 1
                        bucket["rejected"] += 1
                    else:
                        totals["passed"] += 1
                        bucket["passed"] += 1
                else:
                    totals["pending"] += 1
                    bucket["pending"] += 1
                if len(recent) < 10:
                    raw_root = item.get("data_root") or ""
                    name = raw_root.rstrip("/").replace("\\", "/").rsplit("/", 1)[-1]
                    recent.append(
                        {
                            "name": name,
                            "data_root": raw_root,
                            "region": region_name,
                            "vehicle": item.get("vehicle") or "",
                            "state": state,
                            "rejected": rejected_count,
                            "updated_at": item.get("updated_at") or "",
                            "day": _dataset_date(name),
                        }
                    )
            order = list(REGION_OPTIONS) + sorted(set(buckets) - set(REGION_OPTIONS))
            regions_payload: list[dict] = []
            for name in order:
                bucket = buckets.get(name)
                if not bucket:
                    continue
                vehicles = sorted(bucket["vehicles"]) or list(_region_vehicles(name))
                done = bucket["done"]
                regions_payload.append(
                    {
                        "region": name,
                        "vehicles": vehicles,
                        "datasets": bucket["datasets"],
                        "done": done,
                        "passed": bucket["passed"],
                        "rejected": bucket["rejected"],
                        "pending": bucket["pending"],
                        "no_images": bucket["no_images"],
                        "pass_rate_pct": round(bucket["passed"] / done * 100.0, 1) if done else None,
                        "last_at": bucket["last_at"],
                    }
                )
            done_total = totals["done"]
            totals["pass_rate_pct"] = round(totals["passed"] / done_total * 100.0, 1) if done_total else None
            result = {
                "ok": True,
                "region": region,
                "days": days,
                "totals": totals,
                "regions": regions_payload,
                "recent": recent,
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            _cache_set(cache_key, result)
            self.send_json(result)

        def handle_verify_confirmations(self, parsed) -> None:
            if not self._require_store():
                return
            raw = self._query_text(parsed, "data_root")
            if not raw:
                self.send_error_json("缺少 data_root")
                return
            try:
                items = mileage_store.list_verify_confirmations(raw)
                summary = mileage_store.verify_confirmation_stats(raw)
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(f"读取确认状态失败：{error_text(exc)}", 500)
                return
            self.send_json({"ok": True, "items": items, "summary": summary})

        def _notify_verify_complete(self, root_text: str, base: Path, send: bool = False) -> dict:
            """交付图全部给出结论（通过/不通过）后，返回待发送汇总；send=True 才真正推飞书。"""
            if not (STORE_ENABLED and mileage_store is not None):
                return {"sent": False, "reason": "库未启用"}
            try:
                groups = _build_verify_groups(base)
                confirmations = mileage_store.list_verify_confirmations(root_text)
            except Exception as exc:  # noqa: BLE001
                return {"sent": False, "reason": error_text(exc)}
            total = sum(len(group.get("items") or []) for group in groups)
            if not total:
                return {"sent": False, "reason": "没有交付图"}
            passed = rejected = 0
            rejected_by_group: dict[str, int] = {}
            rejected_notes: list[str] = []
            decisions: list[str] = []
            for group in groups:
                for item in group.get("items") or []:
                    name = item.get("name") or ""
                    key = f"{group['key']}|{name}"
                    record = confirmations.get(key) or {}
                    status = record.get("status") or "pending"
                    note_text = str(record.get("note") or "").strip()
                    decisions.append(f"{group['key']}|{name}|{status}|{note_text}")
                    if status == "confirmed":
                        passed += 1
                    elif status == "rejected":
                        rejected += 1
                        group_key = group.get("key") or ""
                        rejected_by_group[group_key] = rejected_by_group.get(group_key, 0) + 1
                        if note_text and note_text not in rejected_notes:
                            rejected_notes.append(note_text)
            decided = passed + rejected
            if decided < total:
                return {
                    "sent": False,
                    "total": total,
                    "passed": passed,
                    "rejected": rejected,
                    "pending": total - decided,
                }
            signature = hashlib.sha1("\n".join(sorted(decisions)).encode("utf-8")).hexdigest()
            existing = None
            try:
                existing = mileage_store.get_verify_notification(root_text)
            except Exception:  # noqa: BLE001
                existing = None
            if existing:
                try:
                    previous = json.loads(existing.get("payload") or "{}")
                except (TypeError, ValueError):
                    previous = {}
                if previous.get("signature") == signature:
                    return {
                        "sent": False,
                        "reason": "结果未变化，无需重复推送",
                        "at": existing.get("updated_at"),
                        "total": total,
                        "passed": passed,
                        "rejected": rejected,
                    }
            labels = {
                "projection_front": "front(x2f) 投影",
                "projection_front7": "front7(x8d) 投影",
                "quality": "数据质检",
            }
            parts = [
                f"{labels.get(group.get('key'), group.get('key'))} {len(group.get('items') or [])}"
                for group in groups
            ]
            name = root_text.rstrip("/").replace("\\", "/").rsplit("/", 1)[-1]
            # 用完整路径识别项目/车型：LYNK 的数据集目录名（如 138123_20261022）不带车型
            vehicle, region = _vehicle_meta(root_text)
            if not vehicle or not region:
                try:
                    cached_run = mileage_store.get_verify_run(root_text) or {}
                except Exception:  # noqa: BLE001
                    cached_run = {}
                vehicle = vehicle or str(cached_run.get("vehicle") or "")
                region = region or str(cached_run.get("region") or "")
            region = region or "未匹配项目/地区"
            data_date = _dataset_date(name)
            finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
            result_text = "通过" if rejected == 0 else "不通过"
            title_parts = [region]
            if vehicle:
                title_parts.append(vehicle)
            title_parts.append(f"参数验证{result_text}")
            title = "【车辆参数验证】" + " · ".join(title_parts)
            body_lines = [
                f"**数据集**：{name}",
                f"**结果**：通过 {passed} · 不通过 {rejected} · 合计 {total}",
                f"**分组**：{' · '.join(parts)}",
            ]
            if rejected_by_group:
                # 只说是哪一项不通过，不列具体图片文件名
                rejected_text = "、".join(
                    f"{labels.get(group_key, group_key)} {count} 张"
                    for group_key, count in rejected_by_group.items()
                    if count
                )
                body_lines.append(f"**不通过**：{rejected_text}")
            if rejected_notes:
                joined_notes = "；".join(rejected_notes[:5])
                if len(rejected_notes) > 5:
                    joined_notes += " 等"
                body_lines.append(f"**不通过原因**：{joined_notes}")
            if data_date:
                body_lines.append(f"**数据采集时间**：{data_date}")
            mention = feishu_region_mention(region)
            if mention:
                body_lines.append(f"**责任人**：{mention}")
            body_lines.append(f"**确认时间**：{finished_at}")
            body = "\n".join(body_lines)
            text = f"{title}\n{body}"
            template = "green" if rejected == 0 else "red"
            if not send:
                # 等用户在前端弹窗里确认后再发
                return {
                    "pending": True,
                    "sent": False,
                    "signature": signature,
                    "title": title,
                    "text": text,
                    "template": template,
                    "total": total,
                    "passed": passed,
                    "rejected": rejected,
                }
            ok, detail = _send_feishu_card(title, body, template)
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            if ok:
                try:
                    mileage_store.save_verify_notification(
                        root_text,
                        "feishu",
                        json.dumps(
                            {"signature": signature, "text": text},
                            ensure_ascii=False,
                        ),
                    )
                except Exception:  # noqa: BLE001
                    pass
                return {
                    "sent": True,
                    "at": now,
                    "total": total,
                    "passed": passed,
                    "rejected": rejected,
                }
            return {
                "sent": False,
                "reason": f"推送失败：{detail}",
                "total": total,
                "passed": passed,
                "rejected": rejected,
            }

        def handle_verify_package(self) -> None:
            """把某天的验证产出打包到 <数据目录>/delivery_package/（按天放 NAS，不占服务器盘）。"""
            if not VERIFY_ENABLED:
                self.send_error_json("本次启动已禁用参数验证（--no-verify）", 403)
                return
            payload = self._read_json_payload(VERIFY_MAX_BODY_BYTES)
            if payload is None:
                return
            raw = payload.get("data_root")
            if not isinstance(raw, str) or not raw.strip():
                self.send_error_json("缺少 data_root")
                return
            force = bool(payload.get("force"))
            try:
                verifier = self._load_verifier()
                root = verifier.resolve_data_root(raw.strip())
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(f"数据目录不可用：{error_text(exc)}", 400)
                return
            if not root.is_dir():
                self.send_error_json(f"数据目录不存在: {root}", 404)
                return
            base = _resolve_verify_output(root)
            if not base.is_dir():
                self.send_error_json("还没有验证产出，无法打包", 400)
                return
            out_dir = root / DELIVERY_DIR_NAME
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.send_error_json(f"创建交付目录失败：{error_text(exc)}", 500)
                return
            zip_name = f"{root.name}_参数验证交付.zip"
            zip_path = out_dir / zip_name
            # 该 CIFS 共享不允许 rename/删除：直接写最终文件；已存在且完整就复用
            if zip_path.is_file() and _zip_is_valid(zip_path) and not force:
                try:
                    size = zip_path.stat().st_size
                except OSError:
                    size = 0
            else:
                wanted_exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".xlsx", ".json", ".csv", ".md", ".docx"}
                # 与历史交付包保持一致的中文目录结构；合成效果图只放样例，避免包太大
                folder_map = [
                    ("projection/front", "01_front_x2f_点云投影叠加图", None),
                    ("projection/front7", "02_front7_x8d_点云投影叠加图", None),
                    ("data_quality", "03_数据质检", None),
                    ("merged", "04_合成效果图_样例", 8),
                ]
                try:
                    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as archive:
                        for sub, arc_dir, limit in folder_map:
                            folder = base / sub
                            if not folder.is_dir():
                                continue
                            files = [
                                file
                                for file in sorted(folder.iterdir())
                                if file.is_file() and file.suffix.lower() in wanted_exts
                            ]
                            if limit:
                                files = files[:limit]
                            for file in files:
                                archive.write(file, arcname=f"{arc_dir}/{file.name}")
                        # 顶层报告/汇总文件放进包根目录
                        for file in sorted(base.iterdir()):
                            if file.is_file() and file.suffix.lower() in {".docx", ".md", ".xlsx", ".json", ".csv"}:
                                archive.write(file, arcname=file.name)
                except OSError as exc:
                    self.send_error_json(
                        f"打包失败（该目录可能不允许覆盖/删除旧文件）：{error_text(exc)}", 500
                    )
                    return
                try:
                    size = zip_path.stat().st_size
                except OSError:
                    size = 0
            url = (
                "/api/verify/delivery?data_root="
                + quote(str(root), safe="")
                + "&name="
                + quote(zip_name, safe="")
            )
            _cache_drop("deliv:")
            _cache_drop("todo:")
            self.send_json({"ok": True, "name": zip_name, "size": size, "url": url})

        def handle_verify_notify(self) -> None:
            """用户在前端弹窗确认后，才把参数验证结果推到飞书。"""
            if not self._require_store():
                return
            payload = self._read_json_payload(VERIFY_MAX_BODY_BYTES)
            if payload is None:
                return
            raw = payload.get("data_root")
            if not isinstance(raw, str) or not raw.strip():
                self.send_error_json("缺少 data_root")
                return
            try:
                base, root_text = self._verify_base_and_root({"data_root": raw.strip()})
            except ValueError as exc:
                self.send_error_json(str(exc))
                return
            if not base.is_dir():
                self.send_error_json("还没有验证产出，无法推送")
                return
            # 「确认/发送」留痕：记录最新确认时间（多次操作以最新为准）
            review = self._verify_review_summary(
                {"data_root": root_text, "output_dir": str(base)}
            ) or {}
            try:
                mileage_store.save_verify_review(
                    root_text,
                    status=str(review.get("state") or ""),
                    total=int(review.get("total") or 0),
                    passed=int(review.get("passed") or 0),
                    rejected=int(review.get("rejected") or 0),
                )
            except Exception:  # noqa: BLE001 - 留痕失败不影响推送
                pass
            result = self._notify_verify_complete(root_text, base, send=True)
            self.send_json({"ok": True, "notification": result})

        def handle_verify_review(self) -> None:
            """「确认」留痕：记录确认时间/统计；同一数据多次确认以最新时间为准。"""
            if not self._require_store():
                return
            payload = self._read_json_payload(VERIFY_MAX_BODY_BYTES)
            if payload is None:
                return
            raw = payload.get("data_root")
            if not isinstance(raw, str) or not raw.strip():
                self.send_error_json("缺少 data_root")
                return
            try:
                base, root_text = self._verify_base_and_root({"data_root": raw.strip()})
            except ValueError as exc:
                self.send_error_json(str(exc))
                return
            review = self._verify_review_summary(
                {"data_root": root_text, "output_dir": str(base)}
            ) or {}
            try:
                updated_at = mileage_store.save_verify_review(
                    root_text,
                    status=str(review.get("state") or ""),
                    total=int(review.get("total") or 0),
                    passed=int(review.get("passed") or 0),
                    rejected=int(review.get("rejected") or 0),
                )
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(f"记录确认失败：{error_text(exc)}", 500)
                return
            self.send_json({"ok": True, "updated_at": updated_at, **review})

        def handle_verify_remove(self) -> None:
            """移除一条无效的验证记录（数据目录已不存在时用）。"""
            if not self._require_store():
                return
            payload = self._read_json_payload(VERIFY_MAX_BODY_BYTES)
            if payload is None:
                return
            raw = payload.get("data_root")
            if not isinstance(raw, str) or not raw.strip():
                self.send_error_json("缺少 data_root")
                return
            root = raw.strip().replace("\\", "/").rstrip("/")
            try:
                removed = mileage_store.delete_verify_run(root)
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(f"移除失败：{error_text(exc)}", 500)
                return
            self.send_json({"ok": True, "removed": bool(removed)})

        def handle_verify_confirm(self) -> None:
            """人工确认：单张/多选批量确认或退回（status=confirmed/rejected/pending）。"""
            if not self._require_store():
                return
            payload = self._read_json_payload(VERIFY_MAX_BODY_BYTES)
            if payload is None:
                return
            data_root = payload.get("data_root")
            category = payload.get("category")
            names = payload.get("names")
            status = payload.get("status")
            note = str(payload.get("note") or "")
            status_text = str(status or "").strip()
            if not isinstance(data_root, str) or not data_root.strip():
                self.send_error_json("缺少 data_root")
                return
            if not isinstance(category, str) or not category.strip():
                self.send_error_json("缺少 category")
                return
            if isinstance(names, str):
                names = [names]
            if not isinstance(names, list) or not names:
                self.send_error_json("缺少 names（要确认的图片列表）")
                return
            if status_text not in ("confirmed", "rejected", "pending"):
                self.send_error_json("status 只能是 confirmed / rejected / pending")
                return
            if status_text == "rejected" and not note.strip():
                self.send_error_json("不通过必须填写问题原因")
                return
            if status_text != "rejected":
                note = ""
            try:
                updated_at = mileage_store.save_verify_confirmations(
                    data_root.strip(), category.strip(), [str(item) for item in names], status_text, note
                )
            except ValueError as exc:
                self.send_error_json(str(exc))
                return
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(f"保存确认状态失败：{error_text(exc)}", 500)
                return
            _cache_drop("review:")
            _cache_drop("todo:")
            notification = None
            if str(status).strip() in ("confirmed", "rejected"):
                try:
                    base, root_text = self._verify_base_and_root({"data_root": data_root.strip()})
                    if base.is_dir():
                        notification = self._notify_verify_complete(root_text, base)
                    else:
                        notification = {"sent": False, "reason": "还没有验证产出"}
                except ValueError as exc:
                    notification = {"sent": False, "reason": str(exc)}
                except Exception as exc:  # noqa: BLE001
                    notification = {"sent": False, "reason": error_text(exc)}
            self.send_json(
                {
                    "ok": True,
                    "updated_at": updated_at,
                    "items": mileage_store.list_verify_confirmations(data_root.strip()),
                    "summary": mileage_store.verify_confirmation_stats(data_root.strip()),
                    "notification": notification,
                }
            )

        def handle_verify_deliveries(self) -> None:
            """平台上可下载的交付包列表：数据目录下的 delivery_package + 服务器交付目录。"""
            entries: list[dict] = []
            try:
                entries.extend(_delivery_entries(VERIFY_DELIVERY_ROOT))
            except Exception:  # noqa: BLE001
                pass
            try:
                if STORE_ENABLED and mileage_store is not None:
                    for item in mileage_store.list_verify_runs_for_region("", limit=2000):
                        root = Path(item.get("data_root") or "")
                        if root.is_dir():
                            entries.extend(_dataset_delivery_entries(root))
            except Exception as exc:  # noqa: BLE001
                self.send_error_json(f"读取交付包失败：{error_text(exc)}", 500)
                return
            entries.sort(key=lambda entry: (entry.get("date") or "", entry.get("mtime") or ""), reverse=True)
            self.send_json(
                {
                    "ok": True,
                    "dir": str(VERIFY_DELIVERY_ROOT),
                    "items": entries,
                }
            )

        def handle_verify_delivery(self, parsed) -> None:
            """下载交付包：支持数据目录下 delivery_package 的 zip，也兼容服务器交付目录。"""
            name = self._query_text(parsed, "name")
            if not name:
                self.send_error_json("缺少 name")
                return
            raw_root = self._query_text(parsed, "data_root")
            if raw_root:
                try:
                    verifier = self._load_verifier()
                    root = verifier.resolve_data_root(raw_root).resolve()
                except Exception as exc:  # noqa: BLE001
                    self.send_error_json(f"数据目录不可用：{error_text(exc)}", 400)
                    return
                if not root.is_dir():
                    self.send_error_json(f"数据目录不存在: {root}", 404)
                    return
                candidates = [root / DELIVERY_DIR_NAME / name, root / name]
                target = next(
                    (path for path in candidates if path.is_file() and path.suffix.lower() == ".zip"),
                    None,
                )
                if target is None:
                    self.send_error_json("交付包不存在", 404)
                    return
                target = target.resolve()
                try:
                    target.relative_to(root)
                except ValueError:
                    self.send_error_json("非法的文件路径", 403)
                    return
                self.send_file(target, "application/zip")
                return
            root = VERIFY_DELIVERY_ROOT.resolve()
            target = (root / name).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                self.send_error_json("非法的文件路径", 403)
                return
            if not target.is_file() or target.suffix.lower() != ".zip":
                self.send_error_json("交付包不存在", 404)
                return
            self.send_file(target, "application/zip")


    return Handler


def local_ipv4_addresses() -> list[str]:
    addresses: set[str] = set()
    try:
        for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = result[4][0]
            if not address.startswith("127."):
                addresses.add(address)
    except OSError:
        pass
    return sorted(addresses)


def default_worker_count() -> int:
    """解码进程数：吃满多核（4 核以上取 核数，上限 8；4 核及以下留 1 核给父进程）。"""
    cores = os.cpu_count() or 2
    return max(1, min(cores, 8) if cores > 4 else cores - 1)


def start_decoder_pool(requested: int | None):
    """创建解码进程池，并回报 CPU 密集段最多能用几个核。

    spawn 是显式选择的：Windows 与 macOS 上 fork 都不可用或不可靠，而且
    “每个工作进程重新 import 模块”正好让子进程不会继承父进程的 socket 与线程。
    """
    global DECODER_POOL, DECODER_WORKERS, SHUTTING_DOWN
    SHUTTING_DOWN = False
    workers = requested if requested and requested > 0 else default_worker_count()
    ctx = multiprocessing.get_context("spawn")
    DECODER_POOL = ProcessPoolExecutor(max_workers=workers, mp_context=ctx)
    DECODER_WORKERS = workers
    return DECODER_POOL


def stop_decoder_pool() -> None:
    global DECODER_POOL, DECODER_WORKERS, SHUTTING_DOWN
    SHUTTING_DOWN = True
    if DECODER_POOL is not None:
        DECODER_POOL.shutdown(wait=False, cancel_futures=True)
        DECODER_POOL = None
        DECODER_WORKERS = 0


def main() -> int:
    global MILEAGE_ENABLED, VERIFY_ENABLED, VERIFY_OUTPUT_ROOT, VERIFY_DELIVERY_ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=None, help="监听地址（默认 0.0.0.0；--local-only 使用 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--lan", action="store_true", help="允许局域网设备访问（默认行为）")
    parser.add_argument("--local-only", action="store_true", help="仅允许本机访问，监听 127.0.0.1")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help=f"解码进程数（默认按 CPU 核数自动选择，当前建议 {default_worker_count()}）",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="每次 /api/parse 打印一行计时与体积（文件数/行数/解码耗时/线上字节数）",
    )
    parser.add_argument(
        "--no-mileage",
        action="store_true",
        help="禁用 POST /api/mileage（该接口按用户给的路径读取服务器本地数据目录）",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="结果数据库路径（默认 <仓库>/data/mileage.db，也可用环境变量 AP18_MILEAGE_DB）",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="禁用参数验证接口 /api/verify/*（外参校验 + 数据质检 + 点云反投影）",
    )
    parser.add_argument(
        "--verify-dir",
        default=None,
        help="参数验证产出目录（默认 <仓库>/data/verify）",
    )
    parser.add_argument(
        "--delivery-dir",
        default=None,
        help="平台上可下载的交付包目录（默认 <仓库>/data/deliveries）",
    )
    args = parser.parse_args()
    MILEAGE_ENABLED = not args.no_mileage
    VERIFY_ENABLED = not args.no_verify
    if args.verify_dir:
        VERIFY_OUTPUT_ROOT = Path(args.verify_dir).expanduser()
    if args.delivery_dir:
        VERIFY_DELIVERY_ROOT = Path(args.delivery_dir).expanduser()
    if args.db and STORE_ENABLED and mileage_store is not None:
        mileage_store.DB_PATH = Path(args.db).expanduser()
        mileage_store._initialized = False

    if not WEB_DIR.is_dir() or not (WEB_DIR / "index.html").is_file():
        parser.error(f"未找到前端目录 web/（缺少 index.html）：{WEB_DIR}")

    bind_host = args.host or ("127.0.0.1" if args.local_only else "0.0.0.0")
    server = ThreadingHTTPServer((bind_host, args.port), make_handler())
    server.request_logging = args.verbose  # type: ignore[attr-defined]
    start_decoder_pool(args.workers)
    browser_host = "127.0.0.1" if bind_host in ("0.0.0.0", "::") else bind_host
    url = f"http://{browser_host}:{args.port}/"
    print(f"AP18 visualizer (stateless): {url}")
    print(
        f"解码并行度：{DECODER_WORKERS} 个进程（CPU {os.cpu_count()} 核）· "
        "响应体积超过 1 MB 时按 Accept-Encoding 协商 gzip"
    )
    print("浏览器本地解析模式：选择 .pcap / .txt 后由各自浏览器上传解析，服务端不保存数据集。")
    print(
        "有效里程（独立页面 /mileage.html）：POST /api/mileage 读取服务器本地数据目录，"
        + ("已启用" if MILEAGE_ENABLED else "已由 --no-mileage 禁用")
    )
    if MILEAGE_ENABLED:
        if STORE_ENABLED and mileage_store is not None:
            print(
                f"结果数据库：{mileage_store.DB_PATH}（SQLite，增量缓存 + 历史检索 "
                "/api/mileage/history）"
            )
        else:
            print("结果数据库：未启用（mileage_store 导入失败，本次不做缓存/历史）")
    print(
        "参数验证（页面「参数验证」sheet）：POST /api/verify/run 跑外参校验 + 数据质检 + 点云反投影，"
        + ("已启用" if VERIFY_ENABLED else "已由 --no-verify 禁用")
    )
    if VERIFY_ENABLED:
        print(f"参数验证产出目录：{VERIFY_OUTPUT_ROOT}")
        print(f"交付包下载目录：{VERIFY_DELIVERY_ROOT}（放 zip 即可在页面按目录/日期下载）")
    if bind_host in ("0.0.0.0", "::") or args.lan:
        addresses = local_ipv4_addresses()
        if addresses:
            for address in addresses:
                print(f"LAN access: http://{address}:{args.port}/")
        else:
            print("LAN access: run ipconfig to find this computer's IPv4 address")
        print("If another device cannot connect, allow this port through the Windows Domain/Private network firewall profile.")
    else:
        print("Local-only mode: use --lan to allow access from another device on the LAN.")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
        stop_decoder_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
