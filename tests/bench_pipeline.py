#!/usr/bin/env python3
"""量测 AP18 解析流水线各阶段耗时，定位"慢"到底慢在哪一步。

用法（在 C:\\code\\pcap 下）：

    python tests/bench_pipeline.py                       # 默认扫描仓库根目录下的 AP18
    python tests/bench_pipeline.py AP18 AP18(2)          # 指定目录
    python tests/bench_pipeline.py --limit 4             # 只测前 4 个文件，快速摸底
    python tests/bench_pipeline.py --json out.json       # 顺便把结果写成 JSON

测的是服务端这一侧的四段成本，全部与 web/app.js 的实际路径一一对应：

  1. decode      —— 直接调 decode_file(pcap)，纯解析成本（单线程基线）
  2. parallel    —— 用 ProcessPoolExecutor 多进程解析同一批文件，验证能拿到几倍加速
  3. multipart   —— 用 ap18_visualizer.multipart_files() 跑一遍 email 解析器，
                    量出"整包读进内存 + BytesParser + 每段再复制一次"的额外成本
  4. json        —— 用 json.dumps(..., ensure_ascii=False) 序列化整批 rows，
                    得到回传体积与编码耗时

最后一节把四段相加，给出"一批 8 个文件"的端到端下界估计，
并按实测比例指出瓶颈排序，避免凭感觉优化。
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

# 放在 tests/ 下：先把仓库根目录补进 sys.path，并让默认数据目录与 cwd 无关（见 _repo.py）
from _repo import collect_pcaps, no_data_error
from ap18_visualizer import multipart_files
from parse_ap18_pcap import decode_file


def _decode_worker(path_str: str) -> int:
    """子进程入口：只返回行数，不把 rows 传回父进程（避免 IPC 反噬）。"""
    return len(decode_file(Path(path_str))[0])


def collect_targets(items: list[str]) -> list[Path]:
    targets: list[Path] = []
    for item in items:
        path = Path(item)
        if path.is_dir():
            targets.extend(sorted(path.glob("*.pcap")))
        elif path.is_file():
            targets.append(path)
    return targets


def ms(seconds: float) -> str:
    return f"{seconds * 1000:,.0f} ms"


def mb(size: int) -> str:
    return f"{size / 1024 / 1024:,.1f} MB"


def fake_multipart_header() -> str:
    return "----AP18BenchBoundary"


def build_multipart_body(paths: list[Path]) -> tuple[str, bytes]:
    """按浏览器 FormData 的形态拼一个 multipart 请求体（用于测解析开销）。"""
    boundary = fake_multipart_header()
    chunks: list[bytes] = []
    for path in paths:
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="files"; filename="{path.name}"\r\n'.encode()
        )
        chunks.append(b"Content-Type: application/octet-stream\r\n\r\n")
        chunks.append(path.read_bytes())
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="*", default=[], help="pcap 文件或目录（默认 AP18）")
    parser.add_argument("--limit", type=int, default=0, help="只看前 N 个文件（0 = 全部）")
    parser.add_argument("--batch-size", type=int, default=8, help="模拟前端的每批文件数")
    parser.add_argument("--workers", type=int, default=0, help="并行进程数（0 = CPU 核数）")
    parser.add_argument("--json", type=Path, default=None, help="把结果写成 JSON")
    args = parser.parse_args()

    targets, missing = collect_pcaps(args.inputs)
    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        no_data_error(parser, "bench_pipeline.py", missing)

    workers = args.workers or (os.cpu_count() or 4)
    print(f"目标文件 {len(targets)} 个 · 每批 {args.batch_size} 个 · 并行进程 {workers}")
    print(f"CPU 核数 {os.cpu_count()} · Python {sys.version.split()[0]}\n")

    # ---- 1. 串行解析 ------------------------------------------------------
    print("[1] 串行 decode_file（单线程基线）")
    per_file: list[float] = []
    total_rows = 0
    total_packets = 0
    rows_by_file: dict[str, list[dict]] = {}
    serial_started = time.perf_counter()
    for path in targets:
        started = time.perf_counter()
        rows, counts = decode_file(path)
        elapsed = time.perf_counter() - started
        per_file.append(elapsed)
        total_rows += len(rows)
        total_packets += counts["pcap_packets"]
        rows_by_file[path.name] = rows
        print(
            f"    {path.name:<42} {ms(elapsed):>10}  "
            f"{len(rows):>6} 行 / {counts['pcap_packets']:>6} 包  "
            f"{counts['pcap_packets'] / elapsed:>10,.0f} 包/s"
        )
    serial_total = time.perf_counter() - serial_started
    print(
        f"    -> 合计 {ms(serial_total)} · {total_rows:,} 行 · {total_packets:,} 包 · "
        f"中位 {ms(statistics.median(per_file))}/文件\n"
    )

    # ---- 2. 多进程解析 ----------------------------------------------------
    print(f"[2] ProcessPoolExecutor 多进程解析（{workers} 进程）")
    parallel_total = 0.0
    if len(targets) > 1 and workers > 1:
        started = time.perf_counter()
        with ProcessPoolExecutor(max_workers=workers) as pool:
            row_counts = list(pool.map(_decode_worker, [str(p) for p in targets]))
        parallel_total = time.perf_counter() - started
        speedup = serial_total / parallel_total if parallel_total else 0
        print(
            f"    合计 {ms(parallel_total)} · 加速 {speedup:.2f}x · "
            f"校验行数一致：{sum(row_counts) == total_rows}\n"
        )
    else:
        print("    跳过（需要 >1 个文件且 >1 个进程）\n")

    # ---- 3. multipart 解析 -----------------------------------------------
    batch = targets[: args.batch_size]
    print(f"[3] multipart 解析开销（每批 {len(batch)} 个文件）")
    content_type, body = build_multipart_body(batch)
    raw_bytes = sum(p.stat().st_size for p in batch)
    started = time.perf_counter()
    files = multipart_files(content_type, body)
    multipart_seconds = time.perf_counter() - started
    extracted = sum(len(payload) for _name, payload in files)
    print(
        f"    请求体 {mb(len(body))}（原文件 {mb(raw_bytes)}）· 解出 {len(files)} 段 · "
        f"耗时 {ms(multipart_seconds)} · {len(body) / max(multipart_seconds, 1e-9) / 1024 / 1024:,.0f} MB/s"
    )
    print(f"    峰值多占内存 ≈ {mb(len(body) + extracted)}（body + 每段 decode=True 的副本）\n")

    # ---- 4. 响应 JSON -----------------------------------------------------
    print("[4] 响应 JSON 编码（一批的全部 rows）")
    batch_rows: list[dict] = []
    for path in batch:
        batch_rows.extend({**row, "_source_file": path.name} for row in rows_by_file.get(path.name, []))
    started = time.perf_counter()
    payload = json.dumps(
        {"columns": [], "files": [], "errors": {}, "rows": batch_rows},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    json_seconds = time.perf_counter() - started
    per_row = len(payload) / max(len(batch_rows), 1)
    print(
        f"    {len(batch_rows):,} 行 -> {mb(len(payload))} · 编码 {ms(json_seconds)} · "
        f"{per_row:,.0f} 字节/行"
    )
    if total_rows:
        projected = per_row * total_rows
        print(f"    按全部 {total_rows:,} 行外推：单次回传体积约 {mb(int(projected))}")
    print(f"    JSON.parse 侧的粗略下界（按 80 MB/s 估算）：约 {ms(len(payload) / (80 * 1024 * 1024))}\n")

    # ---- 5. gzip ----------------------------------------------------------
    # 服务端就是这一句：gzip.compress(payload, 1)。这里量的是"回传体积能砍掉多少"
    # 以及"压缩本身要付多少 CPU"，两者一起决定这一改动的净收益。
    print("[5] 响应 gzip（compress level 1，服务端实际使用的档位）")
    started = time.perf_counter()
    squashed = gzip.compress(payload, 1)
    gzip_seconds = time.perf_counter() - started
    print(
        f"    {mb(len(payload))} -> {mb(len(squashed))} · 压缩比 {len(payload) / max(len(squashed), 1):.1f}x · "
        f"耗时 {ms(gzip_seconds)}"
    )
    print(f"    按当前网络推算（1 Gbps ≈ 118 MB/s）：省下约 {ms((len(payload) - len(squashed)) / (118 * 1024 * 1024))}\n")

    # ---- 汇总 -------------------------------------------------------------
    batch_serial = sum(per_file[: len(batch)])
    print("[6] 单批端到端下界估计（服务端侧，不含网络与前端）")
    stages = [
        ("multipart 解析", multipart_seconds),
        ("decode_file 串行", batch_serial),
        ("rows JSON 编码", json_seconds),
        ("响应 gzip", gzip_seconds),
    ]
    for name, seconds in stages:
        share = seconds / max(sum(s for _n, s in stages), 1e-9) * 100
        print(f"    {name:<20} {ms(seconds):>10}  {share:>5.1f}%")
    print("    改造前串行合计：", ms(sum(seconds for _n, seconds in stages)))
    if parallel_total:
        # 改造后：解码段被进程池摊薄（按 [2] 实测的倍数），其余段仍在父进程里串行。
        optimized = multipart_seconds + batch_serial / (serial_total / parallel_total) + json_seconds + gzip_seconds
        print(f"    改造后估计合计：{ms(optimized)}（解码段按实测 {serial_total / parallel_total:.2f}x 摊薄，其余不变）")
    print("    排序：", " > ".join(name for name, _ in sorted(stages, key=lambda item: -item[1])))
    print(
        "\n提示：[2] 相对 [1] 的倍数就是多进程能拿到的上限；"
        "[4] 的字节/行乘总行数，就是必须改传输格式的证据；"
        "[5] 的压缩比决定 gzip 能省多少回传时间。"
    )

    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "files": [p.name for p in targets],
                    "per_file_seconds": per_file,
                    "serial_seconds": serial_total,
                    "parallel_seconds": parallel_total,
                    "rows": total_rows,
                    "packets": total_packets,
                    "batch_rows": len(batch_rows),
                    "batch_json_bytes": len(payload),
                    "bytes_per_row": per_row,
                    "multipart_seconds": multipart_seconds,
                    "json_seconds": json_seconds,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n结果已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
