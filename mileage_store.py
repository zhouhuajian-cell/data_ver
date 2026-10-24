"""有效里程结果持久化（SQLite）。

为什么用 SQLite：
  * Python 标准库自带 sqlite3，服务器不需要额外安装/运维；
  * 数据量是「每天一行汇总 + 一份结果 JSON」，单文件足够；
  * WAL 模式支持「多线程读 + 串行写」，正好匹配当前 ThreadingHTTPServer；
  * 备份就是拷一个文件。

表结构：
  day_analysis       每个 (数据目录, 日期) 一条最新结果；唯一索引保证 upsert
  analysis_history   每次计算/重算追加一条审计记录（历史可追溯）
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from vehicle_regions import REGION_OPTIONS, dataset_day as _dataset_day
from vehicle_regions import region_vehicles
from vehicle_regions import vehicle_meta as _vehicle_meta

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("AP18_MILEAGE_DB") or (APP_DIR / "data" / "mileage.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS day_analysis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data_root TEXT NOT NULL,
    day TEXT NOT NULL DEFAULT '',
    vehicle TEXT,
    region TEXT,
    computed_at TEXT NOT NULL,
    elapsed_s REAL,
    file_count INTEGER,
    point_count INTEGER,
    start_time TEXT,
    end_time TEXT,
    gps_stable_m REAL,
    covered_2m_m REAL,
    covered_8m_m REAL,
    effective_m REAL,
    any_m REAL,
    no_cover_m REAL,
    effective_ratio_pct REAL,
    issue_count INTEGER,
    warning_count INTEGER,
    result_json TEXT NOT NULL,
    UNIQUE(data_root, day)
);
CREATE INDEX IF NOT EXISTS idx_day_analysis_computed ON day_analysis(computed_at);
CREATE INDEX IF NOT EXISTS idx_day_analysis_root ON day_analysis(data_root);

CREATE TABLE IF NOT EXISTS analysis_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data_root TEXT NOT NULL,
    day TEXT NOT NULL DEFAULT '',
    computed_at TEXT NOT NULL,
    elapsed_s REAL,
    effective_m REAL,
    any_m REAL,
    gps_stable_m REAL,
    result_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_computed ON analysis_history(computed_at);

CREATE TABLE IF NOT EXISTS verify_confirmations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data_root TEXT NOT NULL,
    category TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    note TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(data_root, category, name)
);
CREATE INDEX IF NOT EXISTS idx_verify_conf_root ON verify_confirmations(data_root);

CREATE TABLE IF NOT EXISTS verify_runs (
    data_root TEXT PRIMARY KEY,
    updated_at TEXT NOT NULL,
    day TEXT,
    status TEXT,
    verdict TEXT,
    duration_s REAL,
    output_dir TEXT,
    vehicle TEXT,
    region TEXT,
    result_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verify_runs_updated ON verify_runs(updated_at);

CREATE TABLE IF NOT EXISTS verify_notifications (
    data_root TEXT PRIMARY KEY,
    updated_at TEXT NOT NULL,
    channel TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verify_reviews (
    data_root TEXT PRIMARY KEY,
    updated_at TEXT NOT NULL,
    status TEXT,
    total INTEGER DEFAULT 0,
    passed INTEGER DEFAULT 0,
    rejected INTEGER DEFAULT 0
);
"""

_init_lock = threading.Lock()
_initialized = False


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _migrate(conn: sqlite3.Connection) -> None:
    """老库补上 vehicle/region/day 列，并按路径回填历史数据。"""
    for table in ("day_analysis", "verify_runs"):
        _ensure_column(conn, table, "vehicle", "TEXT")
        _ensure_column(conn, table, "region", "TEXT")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_region ON {table}(region)")
    _ensure_column(conn, "verify_runs", "day", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_verify_runs_day ON verify_runs(day)")
    for table in ("day_analysis", "verify_runs"):
        rows = conn.execute(
            f"SELECT data_root FROM {table} WHERE region IS NULL OR region=''"
        ).fetchall()
        for row in rows:
            vehicle, region = _vehicle_meta(row["data_root"])
            if region:
                conn.execute(
                    f"UPDATE {table} SET vehicle=?, region=? WHERE data_root=?",
                    (vehicle, region, row["data_root"]),
                )
    rows = conn.execute(
        "SELECT data_root FROM verify_runs WHERE day IS NULL OR day=''"
    ).fetchall()
    for row in rows:
        day = _dataset_day(row["data_root"])
        if day:
            conn.execute(
                "UPDATE verify_runs SET day=? WHERE data_root=?", (day, row["data_root"])
            )
    conn.commit()


def _raw_connect() -> sqlite3.Connection:
    global _initialized
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if not _initialized:
        with _init_lock:
            if not _initialized:
                conn.executescript(_SCHEMA)
                _migrate(conn)
                _initialized = True
    return conn


@contextlib.contextmanager
def _connect():
    """连接上下文：退出时提交并**关闭**连接。

    sqlite3 的 `with conn:` 只提交/回滚、不关闭连接；在 ThreadingHTTPServer
    里每次请求都新开连接，不关闭会漏句柄（Windows 上还会锁住数据库文件）。
    """
    conn = _raw_connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _norm_key(path_text: str) -> str:
    """路径比较用：统一分隔符、去掉尾部斜杠。

    Windows 下 normpath 给的是反斜杠，Linux 是正斜杠；批量扫描的父目录和
    单日分析的路径必须能对上，所以比较前统一成正斜杠。
    """
    return str(path_text).replace("\\", "/").rstrip("/")


def day_of(path_text: str) -> str:
    """从数据目录路径里取日期目录名（YYYYMMDD），取不到返回空串。"""
    name = Path(str(path_text).rstrip("/\\")).name
    return name if len(name) == 8 and name.isdigit() else ""


def _norm_day(text: str) -> str:
    """把 2026-10-21 / 20261021 统一成 20261021；不是合法日期返回空串。"""
    digits = "".join(ch for ch in str(text or "") if ch.isdigit())
    return digits if len(digits) == 8 else ""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _summary_from_result(result: Dict[str, Any]) -> Dict[str, Any]:
    mileage = result.get("mileage") or {}
    trajectory = result.get("trajectory") or {}
    issues = result.get("issues") or []
    warnings = result.get("warnings") or []
    return {
        "file_count": result.get("file_count"),
        "point_count": trajectory.get("point_count"),
        "start_time": trajectory.get("start_time"),
        "end_time": trajectory.get("end_time"),
        "gps_stable_m": mileage.get("gps_stable_m"),
        "covered_2m_m": mileage.get("covered_2m_m"),
        "covered_8m_m": mileage.get("covered_8m_m"),
        "effective_m": mileage.get("effective_m", mileage.get("both_m")),
        "any_m": mileage.get("any_m"),
        "no_cover_m": mileage.get("no_cover_m"),
        "effective_ratio_pct": mileage.get(
            "effective_ratio_pct", mileage.get("both_ratio_pct")
        ),
        "issue_count": len(issues),
        "warning_count": len(warnings),
    }


def save_analysis(data_root: str, result: Dict[str, Any], *, elapsed_s: float = 0.0) -> str:
    """写入/更新一条结果，返回计算时间戳。"""
    day = day_of(data_root)
    computed_at = _now()
    vehicle, region = _vehicle_meta(data_root)
    summary = _summary_from_result(result)
    payload = json.dumps(result, ensure_ascii=False)
    history_payload = json.dumps(result, ensure_ascii=False)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO day_analysis (
                data_root, day, vehicle, region, computed_at, elapsed_s, file_count, point_count,
                start_time, end_time, gps_stable_m, covered_2m_m, covered_8m_m,
                effective_m, any_m, no_cover_m, effective_ratio_pct,
                issue_count, warning_count, result_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(data_root, day) DO UPDATE SET
                vehicle=excluded.vehicle,
                region=excluded.region,
                computed_at=excluded.computed_at,
                elapsed_s=excluded.elapsed_s,
                file_count=excluded.file_count,
                point_count=excluded.point_count,
                start_time=excluded.start_time,
                end_time=excluded.end_time,
                gps_stable_m=excluded.gps_stable_m,
                covered_2m_m=excluded.covered_2m_m,
                covered_8m_m=excluded.covered_8m_m,
                effective_m=excluded.effective_m,
                any_m=excluded.any_m,
                no_cover_m=excluded.no_cover_m,
                effective_ratio_pct=excluded.effective_ratio_pct,
                issue_count=excluded.issue_count,
                warning_count=excluded.warning_count,
                result_json=excluded.result_json
            """,
            (
                data_root,
                day,
                vehicle,
                region,
                computed_at,
                elapsed_s,
                summary["file_count"],
                summary["point_count"],
                summary["start_time"],
                summary["end_time"],
                summary["gps_stable_m"],
                summary["covered_2m_m"],
                summary["covered_8m_m"],
                summary["effective_m"],
                summary["any_m"],
                summary["no_cover_m"],
                summary["effective_ratio_pct"],
                summary["issue_count"],
                summary["warning_count"],
                payload,
            ),
        )
        conn.execute(
            """
            INSERT INTO analysis_history
                (data_root, day, computed_at, elapsed_s, effective_m, any_m, gps_stable_m, result_json)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                data_root,
                day,
                computed_at,
                elapsed_s,
                summary["effective_m"],
                summary["any_m"],
                summary["gps_stable_m"],
                history_payload,
            ),
        )
    return computed_at


def get_analysis(data_root: str, *, include_result: bool = True) -> Optional[Dict[str, Any]]:
    """取某个数据目录的最新一条结果；不存在返回 None。"""
    day = day_of(data_root)
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM day_analysis WHERE data_root=? AND day=?", (data_root, day)
        ).fetchone()
    if row is None:
        return None
    record = {key: row[key] for key in row.keys() if key != "result_json"}
    record["data_root"] = row["data_root"]
    record["day"] = row["day"]
    if include_result:
        try:
            record["result"] = json.loads(row["result_json"])
        except (TypeError, json.JSONDecodeError):
            record["result"] = None
    return record


def cached_day_summaries(data_root: str) -> Dict[str, Dict[str, Any]]:
    """某个父目录下所有日期的缓存摘要，用于批量扫描标注「已算」。"""
    prefix = _norm_key(data_root) + "/"
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT data_root, day, computed_at, effective_m, gps_stable_m,
                   effective_ratio_pct, issue_count
            FROM day_analysis
            """
        ).fetchall()
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not _norm_key(row["data_root"]).startswith(prefix):
            continue
        out[row["day"]] = {
            "computed_at": row["computed_at"],
            "effective_m": row["effective_m"],
            "gps_stable_m": row["gps_stable_m"],
            "effective_ratio_pct": row["effective_ratio_pct"],
            "issue_count": row["issue_count"],
        }
    return out


def _days_cutoff(days: int) -> str:
    """返回 N 天前的时间字符串（本地时间，和表里的时间格式一致）；days<=0 表示不限。"""
    if not days or days <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days * 86400))


def list_history(
    *,
    query: str = "",
    data_root: str = "",
    region: str = "",
    days: int = 0,
    date: str = "",
    date_from: str = "",
    date_to: str = "",
    vehicles: Optional[List[str]] = None,
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    """按路径/数据日期检索历史结果（供页面关闭后仍可查询）。"""
    clauses: List[str] = []
    params: List[Any] = []
    if data_root:
        clauses.append("REPLACE(data_root, '\\', '/') = ?")
        params.append(_norm_key(data_root))
    day_filter = _norm_day(date)
    if day_filter:
        clauses.append("day = ?")
        params.append(day_filter)
    day_from = _norm_day(date_from)
    if day_from:
        clauses.append("day >= ?")
        params.append(day_from)
    day_to = _norm_day(date_to)
    if day_to:
        clauses.append("day <= ?")
        params.append(day_to)
    if query:
        like = f"%{query}%"
        clauses.append("(data_root LIKE ? OR day LIKE ?)")
        params.extend([like, like])
    if region and str(region).strip() not in ("", "全部地区"):
        clauses.append("region = ?")
        params.append(str(region).strip())
    elif vehicles:
        # 兼容老调用：按车辆过滤（路径里带车辆编号即可）
        vehicle_clause = " OR ".join("data_root LIKE ?" for _ in vehicles)
        clauses.append(f"({vehicle_clause})")
        params.extend(f"%{vehicle}%" for vehicle in vehicles)
    cutoff = _days_cutoff(days)
    if cutoff:
        clauses.append("computed_at >= ?")
        params.append(cutoff)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    with _connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM day_analysis{where}", params
        ).fetchone()["c"]
        rows = conn.execute(
            f"""
            SELECT id, data_root, day, vehicle, region, computed_at, elapsed_s, file_count, point_count,
                   start_time, end_time, gps_stable_m, covered_2m_m, covered_8m_m,
                   effective_m, any_m, no_cover_m, effective_ratio_pct,
                   issue_count, warning_count
            FROM day_analysis{where}
            ORDER BY day DESC, computed_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            params + [max(1, min(limit, 500)), max(0, offset)],
        ).fetchall()
    items = [{key: row[key] for key in row.keys()} for row in rows]
    return {"ok": True, "total": total, "limit": limit, "offset": offset, "items": items}


def get_history_result(record_id: int) -> Optional[Dict[str, Any]]:
    """按 day_analysis.id 取完整结果（供历史列表点开查看）。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM day_analysis WHERE id=?", (record_id,)
        ).fetchone()
    if row is None:
        return None
    record = {key: row[key] for key in row.keys() if key != "result_json"}
    try:
        record["result"] = json.loads(row["result_json"])
    except (TypeError, json.JSONDecodeError):
        record["result"] = None
    return record


def delete_analysis(record_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM day_analysis WHERE id=?", (record_id,))
        return cur.rowcount > 0


# --------------------------------------------------------------------------
# 参数验证交付图的人工确认状态
# --------------------------------------------------------------------------

VERIFY_CONFIRM_STATUSES = {"confirmed", "rejected"}


def save_verify_confirmations(
    data_root: str,
    category: str,
    names: list,
    status: str,
    note: str = "",
) -> str:
    """记录/更新人工确认状态；status='pending' 表示清空（恢复待确认）。"""
    root = _norm_key(data_root)
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in VERIFY_CONFIRM_STATUSES and normalized_status != "pending":
        raise ValueError(f"不支持的状态: {status}")
    now = _now()
    with _connect() as conn:
        for name in names:
            text = str(name or "").strip()
            if not text:
                continue
            if normalized_status == "pending":
                conn.execute(
                    "DELETE FROM verify_confirmations WHERE data_root=? AND category=? AND name=?",
                    (root, category, text),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO verify_confirmations
                        (data_root, category, name, status, note, updated_at)
                    VALUES (?,?,?,?,?,?)
                    ON CONFLICT(data_root, category, name) DO UPDATE SET
                        status=excluded.status,
                        note=excluded.note,
                        updated_at=excluded.updated_at
                    """,
                    (root, category, text, normalized_status, note or "", now),
                )
    return now


def list_verify_confirmations(data_root: str) -> Dict[str, Dict[str, Any]]:
    """返回 {category|name: {status, note, updated_at}}。"""
    root = _norm_key(data_root)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT category, name, status, note, updated_at"
            " FROM verify_confirmations WHERE data_root=?",
            (root,),
        ).fetchall()
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = f"{row['category']}|{row['name']}"
        out[key] = {
            "status": row["status"],
            "note": row["note"] or "",
            "updated_at": row["updated_at"],
        }
    return out


def verify_confirmation_stats(data_root: str) -> Dict[str, Any]:
    root = _norm_key(data_root)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM verify_confirmations"
            " WHERE data_root=? GROUP BY status",
            (root,),
        ).fetchall()
    counts = {row["status"]: row["c"] for row in rows}
    return {
        "confirmed": counts.get("confirmed", 0),
        "rejected": counts.get("rejected", 0),
    }


# --------------------------------------------------------------------------
# 参数验证结果（历史命中：分析过就不再重跑）
# --------------------------------------------------------------------------


def save_verify_run(
    data_root: str,
    task: Dict[str, Any],
    output_dir: str = "",
    updated_at: Optional[str] = None,
) -> str:
    """保存一次验证结果（成功/失败都记），key 用数据目录。"""
    root = _norm_key(data_root)
    if not root:
        raise ValueError("data_root 不能为空")
    now = updated_at or _now()
    vehicle, region = _vehicle_meta(root)
    day = _dataset_day(root)
    payload = json.dumps(task, ensure_ascii=False)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO verify_runs
                (data_root, updated_at, day, status, verdict, duration_s, output_dir, vehicle, region, result_json)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(data_root) DO UPDATE SET
                updated_at=excluded.updated_at,
                day=excluded.day,
                status=excluded.status,
                verdict=excluded.verdict,
                duration_s=excluded.duration_s,
                output_dir=excluded.output_dir,
                vehicle=excluded.vehicle,
                region=excluded.region,
                result_json=excluded.result_json
            """,
            (
                root,
                now,
                day,
                str(task.get("status") or ""),
                str(task.get("verdict") or ""),
                task.get("duration_s"),
                output_dir or str(task.get("output_root") or ""),
                vehicle,
                region,
                payload,
            ),
        )
    return now


def get_verify_run(data_root: str) -> Optional[Dict[str, Any]]:
    """取某个数据目录的历史验证结果；没有返回 None。"""
    root = _norm_key(data_root)
    if not root:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM verify_runs WHERE data_root=?", (root,)
        ).fetchone()
    if row is None:
        return None
    record = {key: row[key] for key in row.keys() if key != "result_json"}
    record["data_root"] = row["data_root"]
    try:
        record["task"] = json.loads(row["result_json"])
    except (TypeError, json.JSONDecodeError):
        record["task"] = None
    return record


def list_verify_runs() -> Dict[str, Dict[str, Any]]:
    """返回 {data_root: {updated_at, status, verdict, output_dir}}，用于批量列表标注。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT data_root, updated_at, status, verdict, output_dir FROM verify_runs"
        ).fetchall()
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        out[_norm_key(row["data_root"])] = {
            "updated_at": row["updated_at"],
            "status": row["status"],
            "verdict": row["verdict"],
            "output_dir": row["output_dir"],
        }
    return out


def list_verify_runs_page(
    *,
    query: str = "",
    region: str = "",
    days: int = 0,
    date: str = "",
    date_from: str = "",
    date_to: str = "",
    vehicles: Optional[List[str]] = None,
    limit: int = 10,
    offset: int = 0,
) -> Dict[str, Any]:
    """分页列出参数验证历史（单日页面「库中已有结果」用）."""
    clauses: List[str] = []
    params: List[Any] = []
    if query:
        like = f"%{query}%"
        clauses.append("(data_root LIKE ? OR status LIKE ?)")
        params.extend([like, like])
    day_filter = _norm_day(date)
    if day_filter:
        clauses.append("day = ?")
        params.append(day_filter)
    day_from = _norm_day(date_from)
    if day_from:
        clauses.append("day >= ?")
        params.append(day_from)
    day_to = _norm_day(date_to)
    if day_to:
        clauses.append("day <= ?")
        params.append(day_to)
    if region and str(region).strip() not in ("", "全部地区"):
        clauses.append("region = ?")
        params.append(str(region).strip())
    elif vehicles:
        vehicle_clause = " OR ".join("data_root LIKE ?" for _ in vehicles)
        clauses.append(f"({vehicle_clause})")
        params.extend(f"%{vehicle}%" for vehicle in vehicles)
    cutoff = _days_cutoff(days)
    if cutoff:
        clauses.append("updated_at >= ?")
        params.append(cutoff)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    page_limit = max(1, min(int(limit or 10), 500))
    page_offset = max(0, int(offset or 0))
    with _connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM verify_runs{where}", params
        ).fetchone()["c"]
        rows = conn.execute(
            f"""
            SELECT data_root, day, updated_at, status, output_dir, vehicle, region
            FROM verify_runs{where}
            ORDER BY day DESC, updated_at DESC, data_root ASC
            LIMIT ? OFFSET ?
            """,
            params + [page_limit, page_offset],
        ).fetchall()
    items = []
    for row in rows:
        data_root = row["data_root"]
        name = data_root.rstrip("/").replace("\\", "/").rsplit("/", 1)[-1]
        day_value = str(row["day"] or "") or _dataset_day(name)
        vehicle = row["vehicle"] or ""
        region_value = row["region"] or ""
        if not region_value:
            vehicle, region_value = _vehicle_meta(data_root)
            if not vehicle:
                region_value = ""
        items.append(
            {
                "data_root": data_root,
                "name": name,
                "day": day_value,
                "updated_at": row["updated_at"],
                "status": row["status"],
                "output_dir": row["output_dir"],
                "vehicle": vehicle,
                "region": region_value,
            }
        )
    return {
        "total": total,
        "limit": page_limit,
        "offset": page_offset,
        "items": items,
    }


def delete_verify_run(data_root: str) -> bool:
    root = _norm_key(data_root)
    with _connect() as conn:
        cur = conn.execute("DELETE FROM verify_runs WHERE data_root=?", (root,))
        return cur.rowcount > 0


def region_totals() -> Dict[str, Any]:
    """按地区汇总：参数验证条数/最近时间 + 里程天数/里程合计（待办面板用）。"""
    with _connect() as conn:
        run_rows = conn.execute(
            "SELECT region, COUNT(*) AS c, MAX(updated_at) AS last_at"
            " FROM verify_runs GROUP BY region"
        ).fetchall()
        day_rows = conn.execute(
            "SELECT region, COUNT(*) AS c, SUM(effective_m) AS m, MAX(computed_at) AS last_at"
            " FROM day_analysis GROUP BY region"
        ).fetchall()
    runs = {
        (row["region"] or ""): {"count": row["c"], "last_at": row["last_at"]}
        for row in run_rows
    }
    days = {
        (row["region"] or ""): {
            "days": row["c"],
            "effective_m": row["m"] or 0,
            "last_at": row["last_at"],
        }
        for row in day_rows
    }
    return {"runs": runs, "days": days}


def region_day_dates() -> Dict[str, List[str]]:
    """按地区列出里程结果覆盖的日期（YYYYMMDD），用于算「还没做参数验证」的天数。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT region, day FROM day_analysis WHERE day IS NOT NULL AND day != ''"
        ).fetchall()
    out: Dict[str, List[str]] = {}
    for row in rows:
        day = str(row["day"] or "").strip()
        if not day:
            continue
        out.setdefault(row["region"] or "", []).append(day)
    return out


def region_mileage_days_since(since: str) -> Dict[str, int]:
    """统计 computed_at 晚于 since 的里程天数（按地区），用于待办汇总的「新增 x 天」。

    since 为空表示没有上次发送记录：调用方按「全部算新增」处理。
    """
    text = str(since or "").strip()
    with _connect() as conn:
        if text:
            rows = conn.execute(
                "SELECT region, COUNT(*) AS c FROM day_analysis"
                " WHERE computed_at > ? GROUP BY region",
                (text,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT region, COUNT(*) AS c FROM day_analysis GROUP BY region"
            ).fetchall()
    return {(row["region"] or ""): int(row["c"]) for row in rows}


def dashboard_mileage(
    *,
    days: int = 90,
    region: str = "",
    date_from: str = "",
    date_to: str = "",
) -> Dict[str, Any]:
    """里程看板：总览、按项目汇总、最近日期趋势。"""
    cutoff = _days_cutoff(days)
    clauses: List[str] = []
    params: List[Any] = []
    if cutoff:
        clauses.append("computed_at >= ?")
        params.append(cutoff)
    day_from = _norm_day(date_from)
    if day_from:
        clauses.append("day >= ?")
        params.append(day_from)
    day_to = _norm_day(date_to)
    if day_to:
        clauses.append("day <= ?")
        params.append(day_to)
    text = str(region or "").strip()
    if text and text not in ("", "全部地区"):
        clauses.append("region = ?")
        params.append(text)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT region, vehicle, day, computed_at, effective_m, gps_stable_m,
                   any_m, no_cover_m, effective_ratio_pct, result_json
            FROM day_analysis{where}
            ORDER BY day DESC, computed_at DESC
            """,
            params,
        ).fetchall()

    vehicles_by_region: Dict[str, set] = {}
    region_buckets: Dict[str, Dict[str, Any]] = {}
    day_buckets: Dict[str, Dict[str, Any]] = {}
    total_effective = total_gps = total_any = total_no_cover = 0.0
    low_days = 0
    last_at = ""
    for row in rows:
        region_name = row["region"] or "未匹配项目/地区"
        effective = float(row["effective_m"] or 0.0)
        gps = float(row["gps_stable_m"] or 0.0)
        ratio = row["effective_ratio_pct"]
        day = str(row["day"] or "").strip()
        total_effective += effective
        total_gps += gps
        total_any += float(row["any_m"] or 0.0)
        total_no_cover += float(row["no_cover_m"] or 0.0)
        if ratio is not None and float(ratio) < 90:
            low_days += 1
        last_at = max(last_at, str(row["computed_at"] or ""))

        bucket = region_buckets.setdefault(
            region_name,
            {"region": region_name, "days": 0, "effective_m": 0.0, "gps_m": 0.0,
             "low_days": 0, "last_at": ""},
        )
        bucket["days"] += 1
        bucket["effective_m"] += effective
        bucket["gps_m"] += gps
        if ratio is not None and float(ratio) < 90:
            bucket["low_days"] += 1
        bucket["last_at"] = max(bucket["last_at"], str(row["computed_at"] or ""))
        if row["vehicle"]:
            vehicles_by_region.setdefault(region_name, set()).add(row["vehicle"])

        if day:
            day_bucket = day_buckets.setdefault(
                day, {"day": day, "days": 0, "effective_m": 0.0, "gps_m": 0.0}
            )
            day_bucket["days"] += 1
            day_bucket["effective_m"] += effective
            day_bucket["gps_m"] += gps

    regions: List[Dict[str, Any]] = []
    order = list(REGION_OPTIONS) + sorted(set(region_buckets) - set(REGION_OPTIONS))
    for name in order:
        bucket = region_buckets.get(name)
        if not bucket:
            continue
        vehicles = sorted(vehicles_by_region.get(name) or [])
        if not vehicles:
            vehicles = list(region_vehicles(name))
        regions.append(
            {
                **bucket,
                "vehicles": vehicles,
                "ratio_pct": round(bucket["effective_m"] / bucket["gps_m"] * 100.0, 1)
                if bucket["gps_m"] > 0 else None,
            }
        )

    trend: List[Dict[str, Any]] = []
    for day in sorted(day_buckets, reverse=True)[:14]:
        bucket = day_buckets[day]
        trend.append(
            {
                "day": day,
                "days": bucket["days"],
                "effective_m": round(bucket["effective_m"], 1),
                "ratio_pct": round(bucket["effective_m"] / bucket["gps_m"] * 100.0, 1)
                if bucket["gps_m"] > 0 else None,
            }
        )

    return {
        "days": len(rows),
        "effective_m": round(total_effective, 1),
        "gps_stable_m": round(total_gps, 1),
        "any_m": round(total_any, 1),
        "no_cover_m": round(total_no_cover, 1),
        "ratio_pct": round(total_effective / total_gps * 100.0, 1) if total_gps > 0 else None,
        "low_ratio_days": low_days,
        "last_at": last_at,
        "regions": regions,
        "trend": trend,
    }


def low_ratio_days(threshold: float = 90.0, limit: int = 200) -> List[Dict[str, Any]]:
    """有效率低于 threshold 的里程天数（含完整结果，供「有效率偏低」推送分析原因）。"""
    value = float(threshold)
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, data_root, day, vehicle, region, computed_at, file_count,
                   point_count, gps_stable_m, covered_2m_m, covered_8m_m,
                   effective_m, any_m, no_cover_m, effective_ratio_pct, result_json
            FROM day_analysis
            WHERE effective_ratio_pct IS NOT NULL AND effective_ratio_pct < ?
            ORDER BY day DESC, computed_at DESC
            LIMIT ?
            """,
            (value, max(1, min(int(limit or 200), 2000))),
        ).fetchall()
    records: List[Dict[str, Any]] = []
    for row in rows:
        record = {key: row[key] for key in row.keys() if key != "result_json"}
        try:
            record["result"] = json.loads(row["result_json"])
        except (TypeError, json.JSONDecodeError):
            record["result"] = None
        records.append(record)
    return records


def list_verify_runs_for_region(region: str = "", limit: int = 500) -> List[Dict[str, Any]]:
    """取某地区（或全部）验证记录的精简信息（用于统计待确认数量）。"""
    clauses: List[str] = []
    params: List[Any] = []
    text = str(region or "").strip()
    if text and text != "全部地区":
        clauses.append("region = ?")
        params.append(text)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT data_root, updated_at, status, output_dir, vehicle, region
            FROM verify_runs{where}
            ORDER BY day DESC, updated_at DESC
            LIMIT ?
            """,
            params + [max(1, min(int(limit or 500), 2000))],
        ).fetchall()
    return [
        {
            "data_root": row["data_root"],
            "updated_at": row["updated_at"],
            "status": row["status"],
            "output_dir": row["output_dir"],
            "vehicle": row["vehicle"] or "",
            "region": row["region"] or "",
        }
        for row in rows
    ]


def get_verify_notification(data_root: str) -> Optional[Dict[str, Any]]:
    """取某个数据目录的飞书推送记录（用来避免重复推送）。"""
    """取某个数据目录的飞书推送记录（用来避免重复推送）。"""
    root = _norm_key(data_root)
    if not root:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT data_root, updated_at, channel, payload FROM verify_notifications WHERE data_root=?",
            (root,),
        ).fetchone()
    if row is None:
        return None
    return {
        "data_root": row["data_root"],
        "updated_at": row["updated_at"],
        "channel": row["channel"],
        "payload": row["payload"],
    }


def list_verify_notifications() -> Dict[str, str]:
    """返回 {data_root: 推送时间}，历史列表用来展示「验证时间（推送结果时间）」。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT data_root, updated_at FROM verify_notifications"
        ).fetchall()
    return {_norm_key(row["data_root"]): row["updated_at"] for row in rows}


def save_verify_notification(data_root: str, channel: str, payload: str) -> str:
    root = _norm_key(data_root)
    now = _now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO verify_notifications (data_root, updated_at, channel, payload)
            VALUES (?,?,?,?)
            ON CONFLICT(data_root) DO UPDATE SET
                updated_at=excluded.updated_at,
                channel=excluded.channel,
                payload=excluded.payload
            """,
            (root, now, channel, payload),
        )
    return now


def save_verify_review(
    data_root: str,
    *,
    status: str = "",
    total: int = 0,
    passed: int = 0,
    rejected: int = 0,
    updated_at: Optional[str] = None,
) -> str:
    """记录「确认」动作：平台留痕，同一数据集多次确认以最新时间为准。"""
    root = _norm_key(data_root)
    now = updated_at or _now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO verify_reviews (data_root, updated_at, status, total, passed, rejected)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(data_root) DO UPDATE SET
                updated_at=excluded.updated_at,
                status=excluded.status,
                total=excluded.total,
                passed=excluded.passed,
                rejected=excluded.rejected
            """,
            (root, now, status, int(total), int(passed), int(rejected)),
        )
    return now


def get_verify_review(data_root: str) -> Optional[Dict[str, Any]]:
    root = _norm_key(data_root)
    with _connect() as conn:
        row = conn.execute(
            "SELECT data_root, updated_at, status, total, passed, rejected"
            " FROM verify_reviews WHERE data_root=?",
            (root,),
        ).fetchone()
    return dict(row) if row else None


def list_verify_reviews() -> Dict[str, Dict[str, Any]]:
    """返回 {data_root: 确认记录}，历史列表展示「验证时间」用。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT data_root, updated_at, status, total, passed, rejected FROM verify_reviews"
        ).fetchall()
    return {_norm_key(row["data_root"]): dict(row) for row in rows}


def stats() -> Dict[str, Any]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS days, SUM(effective_m) AS effective_total,"
            " MAX(computed_at) AS last_at FROM day_analysis"
        ).fetchone()
        hist = conn.execute("SELECT COUNT(*) AS c FROM analysis_history").fetchone()["c"]
    return {
        "ok": True,
        "db_path": str(DB_PATH),
        "days": row["days"] or 0,
        "effective_total_m": row["effective_total"] or 0.0,
        "last_computed_at": row["last_at"],
        "history_runs": hist,
    }
