#!/usr/bin/env python3
"""量测真实 HTTP 路径的端到端耗时：前端并发 4 批 vs 串行逐批。

`bench_pipeline.py` 量的是服务端各段成本（进程内调用），
这个脚本量的是**前端实际会遇到的墙钟时间**：起真实服务，把 N 个文件按 8 个一批
切分，然后分别用"并发 4 批"和"串行逐批"两种方式跑完整流程（multipart 上传 →
进程池解码 → JSON 序列化 → gzip → 回传解压），报告墙钟时间与加速比。

用法（在 C:\\code\\pcap 下）：

    python tests/bench_concurrency.py                # 默认 AP18 全部文件
    python tests/bench_concurrency.py --limit 16     # 只看前 16 个
    python tests/bench_concurrency.py --batch-size 4 --concurrency 4
"""

from __future__ import annotations

import argparse
import gzip
import json
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path

# 放在 tests/ 下：先把仓库根目录补进 sys.path，并让默认数据目录与 cwd 无关（见 _repo.py）
from _repo import collect_pcaps, no_data_error
import ap18_visualizer as viz


def build_multipart(paths: list[Path], boundary: str) -> tuple[str, bytes]:
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


def post_once(url: str, content_type: str, body: bytes) -> tuple[int, int]:
    """发一次请求，返回 (行数, 线上响应字节数)。"""
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": content_type, "Accept-Encoding": "gzip"},
    )
    with urllib.request.urlopen(request, timeout=1800) as response:
        wire = response.read()
        content_encoding = response.headers.get("Content-Encoding", "")
    # urllib 不会自动解 Content-Encoding：gzip 响应要手动解压再解析 JSON。
    payload = gzip.decompress(wire) if content_encoding == "gzip" else wire
    document = json.loads(payload.decode("utf-8"))
    if document.get("error"):
        raise RuntimeError(document["error"])
    return len(document.get("rows") or []), len(wire)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="*", default=[], help="pcap 目录或文件（默认 AP18）")
    parser.add_argument("--limit", type=int, default=0, help="只看前 N 个文件（0 = 全部）")
    parser.add_argument("--batch-size", type=int, default=8, help="每批文件数（前端是 8）")
    parser.add_argument("--concurrency", type=int, default=4, help="并发批次数（前端是 4）")
    parser.add_argument("--workers", type=int, default=None, help="解码进程数（默认按 CPU 自动）")
    args = parser.parse_args()

    targets, missing = collect_pcaps(args.inputs)
    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        no_data_error(parser, "bench_concurrency.py", missing)

    batches = [
        targets[i : i + args.batch_size] for i in range(0, len(targets), args.batch_size)
    ]
    payloads = [build_multipart(batch, f"----AP18Conc{index}") for index, batch in enumerate(batches)]
    total_bytes = sum(len(body) for _ctype, body in payloads)

    viz.start_decoder_pool(args.workers)
    server = ThreadingHTTPServer(("127.0.0.1", 0), viz.make_handler())
    server.request_logging = False
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/api/parse"
    print(
        f"文件 {len(targets)} 个 · 每批 {args.batch_size} · 共 {len(batches)} 批 · "
        f"上传总计 {total_bytes / 1024 / 1024:.1f} MB · 解码进程 {viz.DECODER_WORKERS}"
    )

    try:
        # 串行：等价于改造前的前端行为（等一批回来再发下一批）。
        started = time.perf_counter()
        serial_rows = 0
        serial_wire = 0
        for content_type, body in payloads:
            rows, wire = post_once(url, content_type, body)
            serial_rows += rows
            serial_wire += wire
        serial_seconds = time.perf_counter() - started

        # 并发：等价于改造后的前端行为。
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            results = list(pool.map(lambda item: post_once(url, item[0], item[1]), payloads))
        concurrent_seconds = time.perf_counter() - started
        concurrent_rows = sum(rows for rows, _wire in results)
        concurrent_wire = sum(wire for _rows, wire in results)
    finally:
        server.shutdown()
        server.server_close()
        viz.stop_decoder_pool()

    print(f"\n  串行逐批    {serial_seconds:6.2f}s · {serial_rows:,} 行")
    print(f"  并发 {args.concurrency} 批    {concurrent_seconds:6.2f}s · {concurrent_rows:,} 行")
    if concurrent_rows != serial_rows:
        print(f"  ** 行数不一致：{concurrent_rows} vs {serial_rows}")
        return 1
    print(f"  端到端加速  {serial_seconds / max(concurrent_seconds, 1e-9):.2f}x")
    print(
        f"  （上传 {total_bytes / 1024 / 1024:.1f} MB · 回传 {concurrent_rows:,} 行 · "
        f"gzip 线上 {concurrent_wire / 1024 / 1024:,.1f} MB ≈ "
        f"{concurrent_rows * 1021 / max(concurrent_wire, 1):.1f}x 压缩）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
