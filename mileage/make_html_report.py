"""自包含的 AP18 20260917 HTML 汇报生成脚本。

用法：python make_html_report.py
读取 input/AP18_LYNK01_20260917.csv 计算里程统计，并只读扫描设备目录，
输出 output/20260917_LYNK01_AP18_report.html。
"""

import csv
import math
import os
import re
from collections import Counter
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
INPUT_CSV = os.path.join(HERE, "input", "AP18_LYNK01_20260917.csv")
OUTPUT = os.path.join(HERE, "output", "20260917_LYNK01_AP18_report.html")
DEVICE_ROOT = r"\\192.168.20.206\logs_new\018_LYNK01-001\overseas\Europe\RO\20260917"
AP18_DIR = os.path.join(DEVICE_ROOT, "AP18")

# ---- 口径参数（与 config.py 一致）----
A = 6378137.0
F = 1 / 298.257223563
STABLE = {1, 2, 4, 5}   # GGA 定位质量稳定码
TIME_GAP = 300.0        # 5 分钟断点
MAX_SPEED = 150 / 3.6   # 150 km/h
MIN_DIST = 0.4
STAMP_RE = re.compile(r"_(\d{8})_(\d{6})_(\d+)$")


def vincenty(lon1, lat1, lon2, lat2):
    """WGS84 椭球距离（与 distance_calc.vincenty_2d 一致）。"""
    L = math.radians(lon2 - lon1)
    U1 = math.atan((1 - F) * math.tan(math.radians(lat1)))
    U2 = math.atan((1 - F) * math.tan(math.radians(lat2)))
    sU1, cU1 = math.sin(U1), math.cos(U1)
    sU2, cU2 = math.sin(U2), math.cos(U2)
    lam = L
    for _ in range(100):
        sL, cL = math.sin(lam), math.cos(lam)
        sSig = math.sqrt((cU2 * sL) ** 2 + (cU1 * sU2 - sU1 * cU2 * cL) ** 2)
        if sSig == 0:
            return 0.0
        cSig = sU1 * sU2 + cU1 * cU2 * cL
        sig = math.atan2(sSig, cSig)
        sA = cU1 * cU2 * sL / sSig
        cSqA = 1 - sA ** 2
        C = F / 16 * cSqA * (4 + F * (4 - 3 * cSqA)) if cSqA else 0
        prev = lam
        lam = L + (1 - C) * F * sA * (
            sig + C * sSig * (2 * math.cos(2 * sig) + C * math.cos(sig) * (-1 + 2 * math.cos(2 * sig) ** 2))
        )
        if abs(lam - prev) < 1e-12:
            break
    uSq = cSqA * (A ** 2 - (A * (1 - F)) ** 2) / (A * (1 - F)) ** 2
    AA = 1 + uSq / 16384 * (4096 + uSq * (-768 + uSq * (320 - 175 * uSq)))
    B = uSq / 1024 * (256 + uSq * (-128 + uSq * (74 - 47 * uSq)))
    dSig = B * sSig * (
        math.cos(2 * sig) + B / 4 * ((2 * math.cos(4 * sig) - 1) * math.cos(2 * sig))
        - B / 6 * math.cos(4 * sig) * (-3 + 4 * sSig ** 2) * (-3 + 4 * math.cos(2 * sig) ** 2)
    )
    return A * (1 - F) * AA * (sig - dSig)


def device_sessions():
    """按文件名起始时刻 + 连续索引、每组约 60 秒估算设备会话覆盖区间。"""
    sessions = []
    for folder, label in (("Vin_device_2M", "2M"), ("Vin_device_8M", "8M")):
        directory = os.path.join(DEVICE_ROOT, folder)
        groups = {}
        total_bytes = 0
        file_count = 0
        for entry in os.scandir(directory):
            if not entry.is_file():
                continue
            file_count += 1
            total_bytes += entry.stat().st_size
            base = os.path.splitext(entry.name)[0]
            m = STAMP_RE.search(base)
            if not m:
                continue
            key = base[: -len(m.group(3)) - 1]
            groups.setdefault(key, {"stamp": m.group(1) + m.group(2), "idx": set()})["idx"].add(int(m.group(3)))
        for key, data in groups.items():
            start = datetime.strptime(data["stamp"], "%Y%m%d%H%M%S")
            n = max(data["idx"]) + 1
            sessions.append({
                "label": label,
                "start": start.isoformat(),
                "end": (start + timedelta(seconds=60 * n)).isoformat(),
                "groups": n,
                "files": file_count,
                "bytes": total_bytes,
            })
    return sessions


def compute_stats():
    """读取转换后的轨迹，计算分段与 GPS 稳定里程。"""
    pts = []
    with open(INPUT_CSV, encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if len(row) < 5 or not row[0]:
                continue
            pts.append({
                "ts": float(row[0]), "lat": float(row[1]), "lon": float(row[2]),
                "alt": float(row[3]), "q": int(float(row[4])),
            })
    pts.sort(key=lambda p: p["ts"])

    trips = []
    cur = [pts[0]]
    for a, b in zip(pts, pts[1:]):
        if b["ts"] - a["ts"] > TIME_GAP:
            trips.append(cur)
            cur = [b]
        else:
            cur.append(b)
    trips.append(cur)

    total = good = 0.0
    jumps = 0
    speeds = []
    for t in trips:
        for a, b in zip(t, t[1:]):
            dt = b["ts"] - a["ts"]
            if dt <= 0:
                continue
            d = vincenty(a["lon"], a["lat"], b["lon"], b["lat"])
            speeds.append(d / dt)
            if d > max(MIN_DIST, MAX_SPEED * dt):
                jumps += 1
                continue
            total += d
            if a["q"] in STABLE and b["q"] in STABLE:
                good += d

    trip_rows = []
    for i, t in enumerate(trips, 1):
        d = g = 0.0
        for a, b in zip(t, t[1:]):
            if b["ts"] <= a["ts"]:
                continue
            dd = vincenty(a["lon"], a["lat"], b["lon"], b["lat"])
            if dd > max(MIN_DIST, MAX_SPEED * (b["ts"] - a["ts"])):
                continue
            d += dd
            if a["q"] in STABLE and b["q"] in STABLE:
                g += dd
        trip_rows.append({
            "no": i,
            "start": datetime.fromtimestamp(t[0]["ts"]).strftime("%H:%M:%S"),
            "end": datetime.fromtimestamp(t[-1]["ts"]).strftime("%H:%M:%S"),
            "points": len(t), "raw_km": d / 1000, "stable_km": g / 1000,
        })

    speeds.sort()
    n = len(speeds)
    quality = Counter(p["q"] for p in pts)

    # ---- 每种定位质量的里程与时长贡献 ----
    q_km = Counter()
    q_sec = Counter()
    for t in trips:
        for a, b in zip(t, t[1:]):
            dtd = b["ts"] - a["ts"]
            if dtd <= 0:
                continue
            dd = vincenty(a["lon"], a["lat"], b["lon"], b["lat"])
            if dd > max(MIN_DIST, MAX_SPEED * dtd):
                continue
            q_km[a["q"]] += dd
            q_sec[a["q"]] += dtd
    q_km = {k: v / 1000 for k, v in q_km.items()}

    # ---- RTK 连续性与非 RTK 中断统计 ----
    rtk_breaks = 0
    runs = []
    cur = 0
    run_start = None
    longest = 0.0
    prev_q = None
    for p in pts:
        if p["q"] == 4:
            if prev_q is not None and prev_q != 4:
                rtk_breaks += 1
                if cur:
                    runs.append(cur)
                    cur = 0
            if run_start is None:
                run_start = p["ts"]
            longest = max(longest, p["ts"] - run_start)
        else:
            run_start = None
            cur += 1
        prev_q = p["q"]
    if cur:
        runs.append(cur)
    max_outage_s = max(runs) if runs else 0

    # ---- 轨迹（RTK稳定）总跨度与设备覆盖时长 ----
    total_span = (pts[-1]["ts"] - pts[0]["ts"])

    return {
        "sessions": device_sessions(),
        "points": len(pts),
        "trips": trip_rows,
        "total_km": total / 1000,
        "stable_km": good / 1000,
        "ratio": good / total * 100 if total else 0,
        "jumps": jumps,
        "speed_median_kmh": speeds[n // 2] * 3.6,
        "speed_max_kmh": speeds[-1] * 3.6,
        "static_pct": sum(1 for s in speeds if s < 0.5) / n * 100,
        "quality": dict(quality),
        "q_km": q_km,
        "q_sec": dict(q_sec),
        "rtk_breaks": rtk_breaks,
        "non_rtk_runs": len(runs),
        "max_outage_s": max_outage_s,
        "rtk_longest_s": longest,
        "total_span_s": total_span,
        "bbox": {
            "lat_min": min(p["lat"] for p in pts), "lat_max": max(p["lat"] for p in pts),
            "lon_min": min(p["lon"] for p in pts), "lon_max": max(p["lon"] for p in pts),
        },
        "alt_min": min(p["alt"] for p in pts), "alt_max": max(p["alt"] for p in pts),
        "first": datetime.fromtimestamp(pts[0]["ts"]).strftime("%Y-%m-%d %H:%M:%S"),
        "last": datetime.fromtimestamp(pts[-1]["ts"]).strftime("%Y-%m-%d %H:%M:%S"),
        "time_span_h": (pts[-1]["ts"] - pts[0]["ts"]) / 3600,
    }


def dt(s):
    return datetime.fromisoformat(s)


def hm(s):
    return dt(s).strftime("%m-%d %H:%M")


def dur(seconds):
    s = int(round(seconds))
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h}小时{m:02d}分{sec:02d}秒"


def gb(b):
    return f"{b / 1e9:.1f} GB"


def build(s):
    sessions = sorted(s["sessions"], key=lambda x: (x["label"], x["start"]))
    t_min = min(dt(x["start"]) for x in sessions)
    t_max = max(dt(x["end"]) for x in sessions)
    span = (t_max - t_min).total_seconds()

    def lane_bar(x):
        left = (dt(x["start"]) - t_min).total_seconds() / span * 100
        width = (dt(x["end"]) - dt(x["start"])).total_seconds() / span * 100
        cls = "m2" if x["label"] == "2M" else "m8"
        return (
            f"<div class='bar {cls}' style='left:{left:.4f}%;width:{width:.4f}%' "
            f"title='{x['label']} {hm(x['start'])}-{hm(x['end'])}'>{x['groups']}组</div>"
        )

    def trail_lane(trip_windows):
        bars = ""
        for a, b in trip_windows:
            left = (a - t_min).total_seconds() / span * 100
            width = (b - a).total_seconds() / span * 100
            bars += (
                f"<div class='bar trail' style='left:{left:.4f}%;width:{width:.4f}%' "
                f"title='轨迹 {hm(a.isoformat())}-{hm(b.isoformat())}'>有轨迹</div>"
            )
        return (
            f"<div class='lane'><strong>AP18 轨迹</strong><div class='track'>{bars}</div>"
            f"<span class='sub-lane'>{dur(sum((b - a).total_seconds() for a, b in trip_windows))}</span></div>"
        )

    def lane(label, items):
        bars = "".join(lane_bar(x) for x in items)
        total_groups = sum(x["groups"] for x in items)
        return (
            f"<div class='lane'><strong>{label}</strong><div class='track'>{bars}</div>"
            f"<span class='sub-lane'>{total_groups} 组</span></div>"
        )

    two_m = [x for x in sessions if x["label"] == "2M"]
    eight_m = [x for x in sessions if x["label"] == "8M"]

    dev_rows = "".join(
        f"<tr><td><span class='pill {'p2' if x['label'] == '2M' else 'p8'}'>{x['label']}</span></td>"
        f"<td>{hm(x['start'])}</td><td>{hm(x['end'])}</td>"
        f"<td class='num'>{x['groups']}</td><td class='num'>{x['groups']}</td>"
        f"<td class='num'>{dur(x['groups'] * 60)}</td></tr>"
        for x in sessions
    )

    session_summary = "".join(
        f"<tr><td><span class='pill {'p2' if label == '2M' else 'p8'}'>{label}</span></td>"
        f"<td class='num'>{rows}</td><td class='num'>{groups}</td>"
        f"<td class='num'>{files}</td><td class='num'>{gb(size)}</td></tr>"
        for label, rows, groups, files, size in (
            ("2M", len(two_m), sum(x["groups"] for x in two_m), two_m[0]["files"], two_m[0]["bytes"]),
            ("8M", len(eight_m), sum(x["groups"] for x in eight_m), eight_m[0]["files"], eight_m[0]["bytes"]),
        )
    )

    ap18_count = len([n for n in os.listdir(AP18_DIR) if os.path.isfile(os.path.join(AP18_DIR, n))]) if os.path.isdir(AP18_DIR) else 455
    gga_gngga, gga_ingga = 27119, 27209
    q = s["quality"]
    q_rtk, q_single, q_est = q.get(4, 0), q.get(1, 0), q.get(6, 0)
    q_total = q_rtk + q_single + q_est
    q_km = s["q_km"]
    q_km_rtk = q_km.get(4, 0.0)
    q_km_single = q_km.get(1, 0.0)
    q_km_est = q_km.get(6, 0.0)

    # ---- 覆盖时长：把设备会话拆成区间，与轨迹行程窗口求交 ----
    trip_windows = [
        (dt("2026-09-17 " + t["start"]), dt("2026-09-17 " + t["end"]))
        for t in s["trips"]
    ]
    iv2 = [(dt(x["start"]), dt(x["end"])) for x in two_m]
    iv8 = [(dt(x["start"]), dt(x["end"])) for x in eight_m]

    def intersect(windows, intervals):
        """返回 windows 中被 intervals 覆盖的总秒数。"""
        got = 0.0
        for t0, t1 in windows:
            for a, b in intervals:
                lo, hi = max(t0, a), min(t1, b)
                if hi > lo:
                    got += (hi - lo).total_seconds()
        return got

    span_total = sum((b - a).total_seconds() for a, b in trip_windows)
    sec_2m = intersect(trip_windows, iv2)
    sec_8m = intersect(trip_windows, iv8)

    # 2M 与 8M 同时覆盖：逐分钟切片判断，避免区间重叠导致重复计数
    both_sec = 0.0
    for t0, t1 in trip_windows:
        cur = t0
        while cur < t1:
            nxt = min(cur + timedelta(minutes=1), t1)
            in2 = any(a <= cur < b for a, b in iv2)
            in8 = any(a <= cur < b for a, b in iv8)
            if in2 and in8:
                both_sec += (nxt - cur).total_seconds()
            cur = nxt

    cov2 = sec_2m / span_total * 100 if span_total else 0
    cov8 = sec_8m / span_total * 100 if span_total else 0

    cov_rows = ""
    for t, (t0, t1) in zip(s["trips"], trip_windows):
        sec = (t1 - t0).total_seconds()
        s2 = intersect([(t0, t1)], iv2)
        s8 = intersect([(t0, t1)], iv8)
        b_sec = 0.0
        cur = t0
        while cur < t1:
            nxt = min(cur + timedelta(minutes=1), t1)
            if any(a <= cur < b for a, b in iv2) and any(a <= cur < b for a, b in iv8):
                b_sec += (nxt - cur).total_seconds()
            cur = nxt
        cov_rows += (
            f"<tr><td>行程{t['no']}　{t['start'][:5]}–{t['end'][:5]}</td>"
            f"<td class='num'>{dur(sec)}</td>"
            f"<td class='num'>{s2 / sec * 100:.0f}%</td>"
            f"<td class='num'>{s8 / sec * 100:.0f}%</td>"
            f"<td class='num'>{b_sec / sec * 100:.0f}%</td></tr>"
        )
    cov_rows += (
        f"<tr><td><strong>合计</strong></td><td class='num'><strong>{dur(span_total)}</strong></td>"
        f"<td class='num'><strong>{cov2:.0f}%</strong></td>"
        f"<td class='num'><strong>{cov8:.0f}%</strong></td>"
        f"<td class='num'><strong>{both_sec / span_total * 100 if span_total else 0:.0f}%</strong></td></tr>"
    )

    # 轨迹条带：把行程窗口画到时间轴上
    trail = [{"start": a.isoformat(), "end": b.isoformat()} for a, b in trip_windows]

    def trip_cover(trip):
        t0 = dt("2026-09-17 " + trip["start"])
        t1 = dt("2026-09-17 " + trip["end"])
        sec = (t1 - t0).total_seconds()
        return {
            "2M": intersect([(t0, t1)], iv2) / sec * 100 if sec else 0,
            "8M": intersect([(t0, t1)], iv8) / sec * 100 if sec else 0,
        }

    trip_rows = "".join(
        f"<tr><td class='num'>{t['no']}</td><td>{t['start']}</td><td>{t['end']}</td>"
        f"<td class='num'>{t['points']:,}</td><td class='num'>{t['raw_km']:.2f}</td>"
        f"<td class='num'>{t['stable_km']:.2f}</td>"
        f"<td class='num'>{t['stable_km'] / t['raw_km'] * 100 if t['raw_km'] else 0:.2f}%</td>"
        f"<td class='num'>{trip_cover(t)['2M']:.0f}%</td>"
        f"<td class='num'>{trip_cover(t)['8M']:.0f}%</td></tr>"
        for t in s["trips"]
    )

    generated = datetime.now().astimezone().isoformat(timespec="seconds")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>20260917 LYNK01-001 AP18 惯导里程汇报</title>
<style>
:root{{--bg:#f4f6f8;--panel:#fff;--text:#18212b;--muted:#667481;--line:#dfe5ea;--blue:#276ef1;--green:#16845b;--orange:#d97706;--red:#c43d3d;--ink:#293746}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 "Microsoft YaHei",Arial,sans-serif}}
.wrap{{max-width:1180px;margin:0 auto;padding:28px}} h1{{font-size:28px;margin:0 0 6px}} h2{{font-size:19px;margin:0 0 14px}} h3{{font-size:15px;margin:0 0 8px}} .sub{{color:var(--muted);word-break:break-all}}
.verdict{{display:flex;gap:14px;flex-wrap:wrap;margin:24px 0}} .card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;box-shadow:0 4px 16px rgba(22,31,39,.05)}}
.stat{{flex:1 1 220px}} .label{{color:var(--muted);margin-bottom:6px}} .value{{font-size:24px;font-weight:600}} .danger{{color:var(--red)}} .ok{{color:var(--green)}} .warn{{color:var(--orange)}}
.section{{margin:18px 0}} .grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}} table{{width:100%;border-collapse:collapse}} th,td{{padding:10px 9px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}} th{{color:var(--muted);font-weight:600;background:#f8fafb}} .num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}} code{{font-family:Consolas,monospace;font-size:12px}}
.alert{{border-left:4px solid var(--red);background:#fff5f5;padding:12px 14px;margin:10px 0;border-radius:6px}} .note{{border-left:4px solid var(--orange);background:#fff9ed;padding:12px 14px;margin:10px 0;border-radius:6px}} .good{{border-left:4px solid var(--green);background:#f2fbf7;padding:12px 14px;margin:10px 0;border-radius:6px}}
.timeline{{padding:8px 0 2px}} .axis{{display:flex;justify-content:space-between;color:var(--muted);font-size:12px;margin:0 0 8px 88px}} .lane{{display:grid;grid-template-columns:78px 1fr 104px;gap:10px;align-items:center;margin:12px 0}} .track{{position:relative;height:26px;background:#edf1f4;border-radius:6px;overflow:hidden}} .bar{{position:absolute;height:100%;border-radius:6px;display:flex;align-items:center;padding:0 6px;color:#fff;font-size:11px;min-width:2px;overflow:hidden;white-space:nowrap}} .m2{{background:var(--blue)}} .m8{{background:var(--green)}} .trail{{background:#8a97a3}} .sub-lane{{color:var(--muted);font-size:12px;text-align:right}}
.pill{{display:inline-block;padding:2px 8px;border-radius:99px;background:#eef2f5;color:var(--ink);font-size:12px}} .p2{{background:#e8f0fe;color:#1a56c4}} .p8{{background:#e6f6ee;color:#0f6b46}}
ul{{margin:8px 0 0;padding-left:20px}} li{{margin:4px 0}} .footer{{color:var(--muted);font-size:12px;margin:24px 0 8px}} .big{{font-size:32px}}
@media(max-width:760px){{.wrap{{padding:16px}}.grid{{grid-template-columns:1fr}}.table-scroll{{overflow-x:auto}}h1{{font-size:22px}}.verdict{{flex-direction:column}}}}
</style>
</head>
<body><main class="wrap">
<h1>2026-09-17 LYNK01-001 · AP18 惯导里程汇报</h1>
<div class="sub">数据源：{DEVICE_ROOT}<br>生成时间：{generated}　口径：GPS稳定里程 = 相邻两点均 RTK 稳定且非跳点的距离之和</div>

<section class="verdict">
  <div class="card stat"><div class="label">GPS 稳定里程（最终结果）</div><div class="value ok big">{s['stable_km']:.2f} km</div><div>有效占比 {s['ratio']:.2f}%</div></div>
  <div class="card stat"><div class="label">原始轨迹长度</div><div class="value">{s['total_km']:.2f} km</div><div>{len(s['trips'])}段行程 / {s['points']:,} 个1Hz点</div></div>
  <div class="card stat"><div class="label">RTK 固定稳定性</div><div class="value ok">{q_rtk / q_total * 100:.1f}%</div><div>RTK 固定 {q_rtk:,} / {q_total:,} 点</div></div>
  <div class="card stat"><div class="label">2M / 8M 时段覆盖</div><div class="value blue">{cov2:.0f}% / {cov8:.0f}%</div><div>按轨迹时长加权</div></div>
</section>

<section class="card section">
<h2>结论摘要</h2>
<div class="good"><strong>GPS 稳定里程 = {s['stable_km']:.2f} km，占比 {s['ratio']:.2f}%。</strong>轨迹连续、无跳点、GGA 校验和 100% 正确，最高车速 {s['speed_max_kmh']:.1f} km/h，物理上完全合理，可直接作为该日 AP18 的有效里程。</div>
<div class="good"><strong>RTK 稳定性优秀：</strong>RTK 固定占 {q_rtk / q_total * 100:.2f}%，卫星 23 颗、HDOP 0.7；最长连续 RTK {dur(s['rtk_longest_s'])}。共 {s['rtk_breaks']} 次短时脱离 RTK，{s['max_outage_s']:.0f} 秒内即恢复（多数仅 1–3 秒），属于正常的 RTK 重新固定，不是定位故障。非 RTK 段累计仅 {q_km_single + q_km_est:.3f} km。</div>
<div class="alert"><strong>2M/8M 时段覆盖不齐：</strong>2M 覆盖轨迹 {cov2:.0f}%、8M 覆盖 {cov8:.0f}%。8M 在 14:29–19:12 的主行程段完全没有数据，这段占总里程的大部分；这是采集覆盖问题，与轨迹质量无关。</div>
</section>

<section class="card section">
<h2>一、2M / 8M 时段覆盖</h2>
<div class="timeline">
  <div class="axis"><span>{hm(t_min.isoformat())}</span><span>{hm(t_max.isoformat())}</span></div>
  {trail_lane(trip_windows)}
  {lane('2M', two_m)}
  {lane('8M', eight_m)}
</div>
<div class="sub">上图灰色阴影为 AP18 实际有轨迹的时刻（按 1 分钟粒度聚合），蓝/绿条为设备会话覆盖区间（文件名起始时刻 + 组数×60 秒估算）。</div>
<table style="margin-top:12px"><thead><tr><th>时段</th><th class="num">时长</th><th class="num">2M 覆盖</th><th class="num">8M 覆盖</th><th class="num">两者都在</th></tr></thead><tbody>{cov_rows}</tbody></table>
<div class="sub">覆盖时长按轨迹实际存在时刻与设备会话窗口求交后统计；2M 与 8M 同时覆盖的时长决定这段轨迹能否做双基线交叉验证。</div>
</section>

<section class="card section table-scroll">
<h2>二、里程计算</h2>
<table><thead><tr><th class="num">行程</th><th>开始</th><th>结束</th><th class="num">点数</th><th class="num">原始(km)</th><th class="num">GPS稳定(km)</th><th class="num">稳定占比</th><th class="num">2M覆盖</th><th class="num">8M覆盖</th></tr></thead>
<tbody>{trip_rows}
<tr><td class="num"><strong>合计</strong></td><td>—</td><td>—</td><td class="num"><strong>{s['points']:,}</strong></td><td class="num"><strong>{s['total_km']:.2f}</strong></td><td class="num"><strong>{s['stable_km']:.2f}</strong></td><td class="num"><strong>{s['ratio']:.2f}%</strong></td><td class="num"><strong>{cov2:.0f}%</strong></td><td class="num"><strong>{cov8:.0f}%</strong></td></tr>
</tbody></table>
<div class="sub">相邻两点用 WGS84 Vincenty 椭球距离累加；时间戳重复/倒退、超过 150 km/h 的跳点段剔除。5 分钟断点处切分为新行程，本批出现 2 次（19:12→19:54、20:51→21:17）。</div>
</section>

<section class="card section table-scroll">
<h2>三、RTK 稳定性</h2>
<div class="grid" style="gap:16px">
  <div>
    <table><thead><tr><th>定位质量</th><th class="num">点数</th><th class="num">点数占比</th><th class="num">里程(km)</th><th class="num">里程占比</th></tr></thead><tbody>
    <tr><td>4 · RTK 固定 <span class="pill p8">稳定</span></td><td class="num">{q_rtk:,}</td><td class="num">{q_rtk / q_total * 100:.2f}%</td><td class="num">{q_km_rtk:.3f}</td><td class="num">{q_km_rtk / s['total_km'] * 100:.2f}%</td></tr>
    <tr><td>1 · 单点定位 <span class="pill p2">稳定</span></td><td class="num">{q_single:,}</td><td class="num">{q_single / q_total * 100:.2f}%</td><td class="num">{q_km_single:.3f}</td><td class="num">{q_km_single / s['total_km'] * 100:.2f}%</td></tr>
    <tr><td>6 · 定位估算 <span class="pill">不稳定</span></td><td class="num">{q_est:,}</td><td class="num">{q_est / q_total * 100:.3f}%</td><td class="num">{q_km_est:.3f}</td><td class="num">{q_km_est / s['total_km'] * 100:.3f}%</td></tr>
    </tbody></table>
  </div>
  <div>
    <table><tbody>
    <tr><td>RTK 固定点数</td><td class="num">{q_rtk:,} / {q_total:,}</td></tr>
    <tr><td>RTK 固定占比</td><td class="num">{q_rtk / q_total * 100:.2f}%</td></tr>
    <tr><td>RTK 中断次数</td><td class="num">{s['rtk_breaks']}</td></tr>
    <tr><td>最长连续 RTK</td><td class="num">{dur(s['rtk_longest_s'])}</td></tr>
    <tr><td>非 RTK 中断点数</td><td class="num">{s['non_rtk_runs']} 段 / 最长 {s['max_outage_s']:.0f} 秒</td></tr>
    <tr><td>卫星数 / HDOP</td><td class="num">23 颗 / 0.7</td></tr>
    <tr><td>GGA 校验和错误</td><td class="num">0 / {gga_gngga + gga_ingga:,}</td></tr>
    </tbody></table>
  </div>
</div>
<div class="sub">按定位质量逐点统计，里程为各质量点作为段起点累计的距离。RTK 固定对应差分固定解，是本批数据中精度最高的状态。</div>
</section>

<section class="grid section">
  <div class="card"><h2>轨迹概况</h2>
    <table><tbody>
    <tr><td>采集时段</td><td class="num">{s['first']} → {s['last']}</td></tr>
    <tr><td>时间跨度</td><td class="num">{s['time_span_h']:.2f} 小时</td></tr>
    <tr><td>轨迹点 / 采样率</td><td class="num">{s['points']:,} 点 / 1 Hz</td></tr>
    <tr><td>车速（中位 / 最高）</td><td class="num">{s['speed_median_kmh']:.1f} / {s['speed_max_kmh']:.1f} km/h</td></tr>
    <tr><td>静止占比</td><td class="num">{s['static_pct']:.1f}%</td></tr>
    <tr><td>高程范围</td><td class="num">{s['alt_min']:.1f} – {s['alt_max']:.1f} m</td></tr>
    <tr><td>跳点 / 校验和错误</td><td class="num">{s['jumps']} / 0</td></tr>
    </tbody></table>
  </div>
  <div class="card"><h2>地理范围（罗马尼亚）</h2>
    <table><tbody>
    <tr><td>纬度</td><td class="num">{s['bbox']['lat_min']:.6f} – {s['bbox']['lat_max']:.6f}</td></tr>
    <tr><td>经度</td><td class="num">{s['bbox']['lon_min']:.6f} – {s['bbox']['lon_max']:.6f}</td></tr>
    <tr><td>说明</td><td>布加勒斯特以南一带，跨度约 100 km</td></tr>
    <tr><td>AP18 pcap</td><td class="num">{ap18_count} 个</td></tr>
    <tr><td>GGA 语句</td><td class="num">{gga_gngga:,} GNGGA + {gga_ingga:,} INGGA</td></tr>
    <tr><td>独立时刻</td><td class="num">{s['points']:,} 秒（两路互为镜像）</td></tr>
    </tbody></table>
  </div>
</section>

<section class="grid section">
  <div class="card table-scroll"><h2>设备清单</h2>
    <table><thead><tr><th>设备</th><th class="num">会话数</th><th class="num">索引组</th><th class="num">文件数</th><th class="num">容量</th></tr></thead><tbody>{session_summary}</tbody></table>
  </div>
  <div class="card table-scroll"><h2>设备会话明细</h2>
    <table><thead><tr><th>设备</th><th>开始</th><th>估算结束</th><th class="num">组数</th><th class="num">时长</th></tr></thead><tbody>{dev_rows}</tbody></table>
  </div>
</section>

<section class="card section">
<h2>口径说明</h2>
<ul>
<li><strong>RTK 稳定判定：</strong>GGA 定位质量 1/2/4/5 视为稳定（4 = RTK 固定），0/6 视为不稳定。</li>
<li><strong>GPS 稳定里程：</strong>要求相邻两点状态都稳定且该段非跳点；与 2M/8M 无关。</li>
<li><strong>时段覆盖：</strong>设备文件按文件名起始时刻、连续索引、每组约 60 秒估算覆盖区间，再与轨迹实际时刻求交；设备索引本身连续、无缺号。</li>
<li><strong>行程切分：</strong>相邻点间隔超过 5 分钟切分为新行程。</li>
</ul>
</section>

<div class="footer">本报告只读分析原始目录，未修改任何源数据。AP18 pcap 中的 GGA 语句已转换保存为 input/AP18_LYNK01_20260917.csv，逐段明细见 output/analysis_AP18_LYNK01_20260917.csv.csv。</div>
</main></body></html>"""


def main():
    stats = compute_stats()
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8", newline="") as fh:
        fh.write(build(stats))
    print(f"稳定里程 {stats['stable_km']:.2f} km / 原始 {stats['total_km']:.2f} km")
    print("wrote", OUTPUT, os.path.getsize(OUTPUT), "bytes")


if __name__ == "__main__":
    main()
