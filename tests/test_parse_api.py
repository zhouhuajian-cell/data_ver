#!/usr/bin/env python3
"""端到端自检：验证 /api/parse 的多进程路径与 gzip 协商，且结果与直连解码逐字段一致。

用法（在 C:\\code\\pcap 下）：

    python tests/test_parse_api.py                      # 默认取 AP18 里前 3 个 pcap
    python tests/test_parse_api.py --files 6            # 每批 6 个
    python tests/test_parse_api.py --solo               # 默认并行度 + --workers 1 各跑一遍
    python tests/test_parse_api.py --no-parallel        # 关掉进程池再测一次（对照就地解析路径）

它做四件事：
  1. 起一个真实 HTTP 服务（临时端口，后台线程），走完整的 multipart 上传路径；
  2. 分别用 Accept-Encoding: gzip 和 identity 各请求一次，确认两种编码结果一致；
  3. 校验 HTTP 层确实用了 gzip（有 Content-Encoding 头，且线上字节数明显小于原始 JSON）；
  4. 把响应里的 rows 与 parse_ap18_pcap.decode_file() 的结果逐字段比对，
     确保进程池/结果回填没有改变任何数据（顺序、字段、None 都算）。

任何一项不通过都会打印 [FAIL] 并返回非零退出码，方便直接接进 CI 或冒烟流程。
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

# 放在 tests/ 下：先把仓库根目录补进 sys.path，并让默认数据目录与 cwd 无关（见 _repo.py）
from _repo import collect_pcaps, no_data_error
import ap18_visualizer as viz
from parse_ap18_pcap import decode_file

FAILURES: list[str] = []
# 每次请求的 (解压耗时, JSON 解析耗时, 解压后字节数)，用于报告客户端侧成本。
CLIENT_TIMING: list[tuple[float, float, int]] = []


def check(condition: bool, label: str, detail: str = "") -> None:
    if condition:
        print(f"  [ OK ] {label}")
        return
    FAILURES.append(label)
    print(f"  [FAIL] {label}{(' — ' + detail) if detail else ''}")


def build_multipart(paths: list[Path]) -> tuple[str, bytes]:
    boundary = "----AP18SelfTestBoundary"
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


def post(url: str, content_type: str, body: bytes, encoding: str) -> tuple[dict, bytes, bytes]:
    """POST 一次，返回 (解析后的 JSON, 线上的原始字节, 响应里的 Content-Encoding)。"""
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": content_type, "Accept-Encoding": encoding},
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        wire = response.read()
        content_encoding = response.headers.get("Content-Encoding", "")
    # 量一下"浏览器侧"要付的成本：解压 + JSON 解析。前端 response.json() 这两步
    # 都在主线程上，是首屏等待的一部分，所以必须跟服务端耗时一起看。
    client_started = time.perf_counter()
    payload = gzip.decompress(wire) if content_encoding == "gzip" else wire
    decompressed_bytes = len(payload)
    decompress_seconds = time.perf_counter() - client_started
    decode_started = time.perf_counter()
    document = json.loads(payload.decode("utf-8"))
    parse_seconds = time.perf_counter() - decode_started
    CLIENT_TIMING.append((decompress_seconds, parse_seconds, decompressed_bytes))
    return document, wire, content_encoding.encode("ascii")


def same_value(left: object, right: object) -> bool:
    """NaN 安全的相等判断。

    浮点字段一旦出现 NaN，`nan != nan` 恒为 True，直接用 != 比会把"数据完全一致"
    误报成不一致。除了 NaN，这里对其它类型都退回普通 ==。
    """
    if isinstance(left, float) and isinstance(right, float):
        if math.isnan(left) and math.isnan(right):
            return True
    return left == right


def rows_equal(actual: dict, expected: dict) -> bool:
    return set(actual) == set(expected) and all(
        same_value(actual[key], expected[key]) for key in actual
    )


def run_suite(url: str, content_type: str, body: bytes, targets: list[Path], label: str) -> None:
    """对已经起好的服务跑完整套断言。label 只用于输出前缀。"""
    print(f"[1] gzip 协商（{label}）")
    identity, identity_wire, identity_enc = post(url, content_type, body, "identity")
    compressed, compressed_wire, compressed_enc = post(url, content_type, body, "gzip")
    check(not identity.get("error") and not compressed.get("error"), f"{label}：两次请求都成功")
    check(identity_enc != b"gzip", f"{label}：Accept-Encoding: identity 时未压缩",
          identity_enc.decode("ascii", "replace"))
    # 响应只有超过 GZIP_MIN_BYTES 才会压缩，所以大文件上必须真的生效。
    compressed_expected = len(identity_wire) >= viz.GZIP_MIN_BYTES
    if compressed_expected:
        check(compressed_enc == b"gzip", f"{label}：Accept-Encoding: gzip 时启用了 gzip",
              compressed_enc.decode("ascii", "replace") or "(无 Content-Encoding)")
        check(len(compressed_wire) < len(identity_wire), f"{label}：线上字节数确实变小",
              f"{len(compressed_wire):,} vs {len(identity_wire):,}")
    else:
        print(f"        （本批 JSON 仅 {len(identity_wire):,} 字节，低于 {viz.GZIP_MIN_BYTES:,} 阈值，跳过压缩断言）")
    check(identity["rows"] == compressed["rows"], f"{label}：gzip 与 identity 的 rows 逐行相同")
    ratio = len(identity_wire) / max(len(compressed_wire), 1) if compressed_expected else 0.0

    print("\n[2] 响应结构")
    for field in ("columns", "files", "errors", "rows"):
        check(field in identity, f"响应包含 {field}")
    check(len(identity["files"]) == len(targets), "files 数量与请求一致")
    check(not identity.get("errors"), "没有解析错误",
          json.dumps(identity.get("errors", {}), ensure_ascii=False))

    print("\n[3] 与直连解码逐字段比对")
    expected_rows: list[dict] = []
    for path in targets:
        for row in decode_file(path)[0]:
            expected_rows.append({**row, "_source_file": path.name})
    expected_rows.sort(key=viz.gps_sort_key)
    actual_rows = identity["rows"]
    check(len(actual_rows) == len(expected_rows), "行数一致", f"{len(actual_rows)} vs {len(expected_rows)}")
    mismatches = 0
    first_detail = ""
    for index, (actual, expected) in enumerate(zip(actual_rows, expected_rows)):
        if not rows_equal(actual, expected):
            mismatches += 1
            if not first_detail:
                keys = sorted(set(actual) | set(expected))
                diff = [k for k in keys if not same_value(actual.get(k), expected.get(k))]
                first_detail = f"第 {index} 行字段不一致：{diff[:6]}"
    check(mismatches == 0, f"{len(actual_rows):,} 行内容完全一致", first_detail)
    order_ok = all(
        viz.gps_sort_key(actual_rows[i]) <= viz.gps_sort_key(actual_rows[i + 1])
        for i in range(len(actual_rows) - 1)
    )
    check(order_ok, "响应内 rows 按 GPS 时间有序")

    print("\n[4] 传输体积")
    volume = (
        f"        批 {len(targets)} 个文件 · {len(actual_rows):,} 行 · "
        f"JSON {len(identity_wire) / 1024 / 1024:,.2f} MB"
    )
    if compressed_expected:
        volume += f" · gzip 线上 {len(compressed_wire) / 1024 / 1024:,.2f} MB（约 {ratio:.1f}x）"
    print(volume)
    if CLIENT_TIMING:
        decompress_seconds, parse_seconds, decompressed = CLIENT_TIMING[-1]
        print(
            f"        客户端侧（本次请求）：解压 {decompress_seconds * 1000:,.0f} ms · "
            f"JSON.parse {parse_seconds * 1000:,.0f} ms · 解压后 {decompressed / 1024 / 1024:,.2f} MB"
        )


def test_nan_guard() -> None:
    """自检 json_bytes 的 NaN/Infinity 防线（不依赖真实数据里是否出现 NaN）。"""
    print("\n[5] 非有限浮点不会被写成非法 JSON")
    payload = {
        "rows": [{"latitude": 50.0, "north_position_rms": float("nan")}],
        "meta": [float("inf"), float("-inf"), 1.5, None, "ok"],
    }
    try:
        encoded = viz.json_bytes(payload)
    except ValueError as exc:
        check(False, "json_bytes 能序列化含 NaN 的载荷", str(exc))
        return
    check(True, "json_bytes 能序列化含 NaN 的载荷")
    text = encoded.decode("utf-8")
    check("NaN" not in text and "Infinity" not in text, "输出里没有裸 NaN/Infinity", text[:120])
    check(
        json.loads(text)["rows"][0]["north_position_rms"] is None
        and json.loads(text)["meta"][:2] == [None, None],
        "非有限浮点被归一成 null",
        text[:120],
    )


def with_server(content_type: str, body: bytes) -> None:
    """起一个临时端口的服务，跑完全套断言后关掉。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), viz.make_handler())
    server.request_logging = VERBOSE  # type: ignore[attr-defined]
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"测试服务：127.0.0.1:{port}")
    try:
        run_suite(f"http://127.0.0.1:{port}/api/parse", content_type, body, TARGETS, LABEL)
    finally:
        server.shutdown()
        server.server_close()


TARGETS: list[Path] = []
LABEL = ""
VERBOSE = False


def main() -> int:
    global TARGETS, LABEL, VERBOSE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="*", default=[], help="pcap 目录或文件（默认 AP18）")
    parser.add_argument("--files", type=int, default=3, help="参与自检的文件总数（默认 3，取自目录里最前面的几个）")
    parser.add_argument("--no-parallel", action="store_true", help="不建进程池，走就地解析路径")
    parser.add_argument("--solo", action="store_true", help="用 --workers 1 再跑一遍（进程池只有 1 个进程时比对顺序与数据）")
    parser.add_argument("--verbose", action="store_true", help="同时打印服务端每次请求的计时与体积行")
    args = parser.parse_args()
    VERBOSE = args.verbose

    targets, missing = collect_pcaps(args.inputs)
    targets = targets[: args.files]
    if not targets:
        no_data_error(parser, "test_parse_api.py", missing)

    TARGETS = targets
    print(f"目标文件：{[p.name for p in targets]}")
    test_nan_guard()
    content_type, body = build_multipart(targets)

    # 先用默认并行度，再（可选）用 1 个进程跑一遍：单进程时任务在同一条队列里，
    # 最容易被"按完成顺序回填"这类错误影响，两种配置结果一致才算真没问题。
    plans: list[tuple[str, int | None]] = [("默认进程数", None if not args.no_parallel else 0)]
    if args.solo:
        plans.append(("--workers 1", 1))

    for label, workers in plans:
        LABEL = label
        if workers == 0:
            viz.stop_decoder_pool()
        else:
            viz.stop_decoder_pool()
            viz.start_decoder_pool(workers)
        actual = viz.DECODER_WORKERS if viz.DECODER_POOL else 0
        print(f"\n===== {label}：{'进程池 ' + str(actual) + ' 个进程' if actual else '就地解析（无进程池）'} =====")
        with_server(content_type, body)

    print()
    if FAILURES:
        print(f"结果：{len(FAILURES)} 项失败 -> {FAILURES}")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
