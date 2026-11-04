#!/usr/bin/env python3
"""容错专项测试：同一批次里部分文件坏掉时，解析结果是否仍然可用。

构造一个批次，里面**故意混入各种坏文件**，然后走真实 HTTP 请求，验证：

  1. 好的文件必须照常返回（rows 完整、可排序），而不是被坏文件牵连；
  2. 每个坏文件各有一条 errors 记录，且能定位到原因；
  3. HTTP 状态仍是 200（批次级成功），行数与"只传好文件"完全一致；
  4. 全坏的一批不是 500，而是 200 + 全量 errors + 空 rows（前端可以据此提示）；
  5. 坏文件与好文件的判定与执行顺序、文件在批次内的位置无关。

坏文件覆盖：空文件、截断的全局头、magic 错误（含 pcapng）、
合法头 + 截断的包记录、合法头 + 包长度超出实际内容、非 pcap 的纯文本。
"""

from __future__ import annotations

import argparse
import gzip
import json
import struct
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

# 放在 tests/ 下：先把仓库根目录补进 sys.path，并让默认数据目录与 cwd 无关（见 _repo.py）
from _repo import collect_pcaps, no_data_error
import ap18_visualizer as viz
from parse_ap18_pcap import decode_file

FAILURES: list[str] = []
PROBE_DIR = Path(__file__).resolve().parent / "_fault_probe"


def check(condition: bool, label: str, detail: str = "") -> None:
    if condition:
        print(f"  [ OK ] {label}")
        return
    FAILURES.append(label)
    print(f"  [FAIL] {label}{(' — ' + detail) if detail else ''}")


def build_bad_files() -> dict[str, Path]:
    """造一批各不相同的坏文件。"""
    PROBE_DIR.mkdir(exist_ok=True)
    libpcap_le = b"\xd4\xc3\xb2\xa1" + struct.pack("<HHIIII", 2, 4, 0, 0, 65535, 1)
    packet = struct.pack("<IIII", 1_700_000_000, 0, 64, 64) + b"\x00" * 64

    files: dict[str, Path] = {}

    paths = {
        "empty.pcap": b"",
        "truncated_header.pcap": libpcap_le[:10],
        "bad_magic.pcap": b"THIS-IS-NOT-A-PCAP-AT-ALL" + b"\x00" * 40,
        "pcapng.pcap": b"\x0a\x0d\x0d\x0a" + b"\x1c\x00\x00\x00" + b"\x00" * 24,
        "truncated_record.pcap": libpcap_le + packet[:30],
        "record_overrun.pcap": libpcap_le + struct.pack("<IIII", 1_700_000_000, 0, 4096, 4096) + b"\x00" * 64,
        "text_named_pcap.pcap": b"just some text, definitely not a capture\n" * 20,
        # 抓包工具刚创建、还没写入内容的文件（前端实际遇到的那种：magic=00000000）
        "zero_magic.pcap": b"\x00" * 1024,
    }
    for name, content in paths.items():
        path = PROBE_DIR / name
        path.write_bytes(content)
        files[name] = path
    return files


def build_duplicate_pairs(good: Path) -> dict[str, tuple[bytes, bytes]]:
    """构造"同名但内容不同"的文件对，用于验证服务端的重名去重。

    每一对的两个文件 basename 完全相同：一个是真 pcap，另一个是坏内容。
    没有去重时，errors 只会留下一条、第二份数据会被静默丢掉。
    """
    libpcap_le = b"\xd4\xc3\xb2\xa1" + struct.pack("<HHIIII", 2, 4, 0, 0, 65535, 1)
    real = good.read_bytes()
    return {
        "dup_a.pcap": (real, b""),  # 好文件 + 空文件
        "dup_b.pcap": (real, b"NOT-A-PCAP" + b"\x00" * 32),  # 好文件 + magic 错误
        "dup_c.pcap": (real, libpcap_le[:8]),  # 好文件 + 截断全局头
    }


def build_multipart(paths: list[Path], boundary: str = "----FaultProbe") -> tuple[str, bytes]:
    chunks: list[bytes] = []
    for path in paths:
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="files"; filename="{path.name}"\r\n\r\n'.encode()
        )
        chunks.append(path.read_bytes())
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


def post(url: str, content_type: str, body: bytes) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": content_type, "Accept-Encoding": "gzip"},
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            status = response.status
            wire = response.read()
            encoding = response.headers.get("Content-Encoding", "")
    except urllib.error.HTTPError as exc:  # 4xx/5xx 也要把 JSON 读出来看
        status = exc.code
        wire = exc.read()
        encoding = exc.headers.get("Content-Encoding", "")
    return status, json.loads(gzip.decompress(wire) if encoding == "gzip" else wire)


def main() -> int:
    parser = argparse.ArgumentParser(description="容错自检：坏文件不能影响同批其它文件")
    parser.add_argument("inputs", nargs="*", default=[],
                        help="好文件所在的目录或文件（默认 仓库根目录/AP18；示例数据被删掉时请指定自己的采集目录）")
    args = parser.parse_args()

    pool, missing = collect_pcaps(args.inputs)
    if len(pool) < 3:
        no_data_error(parser, "test_fault_tolerance.py", missing)
    good_files = pool[:3]
    bad_files = build_bad_files()
    bad_names = sorted(bad_files)

    # 基准：只传好文件
    baseline_ctype, baseline_body = build_multipart(good_files)
    # 混合批次：好文件与坏文件交错，顺序刻意打乱
    mixed = [good_files[0], bad_files["empty.pcap"], good_files[1],
             bad_files["bad_magic.pcap"], bad_files["pcapng.pcap"], good_files[2],
             bad_files["truncated_header.pcap"], bad_files["truncated_record.pcap"],
             bad_files["record_overrun.pcap"], bad_files["text_named_pcap.pcap"],
             bad_files["zero_magic.pcap"]]
    mixed_ctype, mixed_body = build_multipart(mixed)
    # 全坏批次
    all_bad_ctype, all_bad_body = build_multipart([bad_files[name] for name in bad_names])

    viz.start_decoder_pool(None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), viz.make_handler())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/api/parse"
    print(f"解码进程 {viz.DECODER_WORKERS} · 好文件 {len(good_files)} · 构造坏文件 {len(bad_files)}\n")

    try:
        print("[1] 基准：只传 3 个好文件（进程池路径）")
        status, baseline = post(url, baseline_ctype, baseline_body)
        check(status == 200, "HTTP 200")
        check(not baseline.get("errors"), "没有错误", json.dumps(baseline.get("errors", {}), ensure_ascii=False)[:160])
        baseline_rows = len(baseline["rows"])
        check(baseline_rows > 0, f"返回 {baseline_rows:,} 行")

        print(f"\n[2] 混合批次：3 个好文件 + {len(bad_names)} 个坏文件交错")
        status, mixed_doc = post(url, mixed_ctype, mixed_body)
        check(status == 200, "HTTP 200（坏文件不升级为请求级失败）", f"status={status}")
        check(len(mixed_doc["files"]) == 3, "只把 3 个好文件算作成功",
              f"实际 {len(mixed_doc['files'])}：{[f['name'] for f in mixed_doc['files']]}")
        check(len(mixed_doc["rows"]) == baseline_rows,
              f"rows 行数与基准一致（{baseline_rows:,}）", f"实际 {len(mixed_doc['rows']):,}")
        check(mixed_doc["rows"] == baseline["rows"], "rows 内容与基准逐行相同")
        check(len(mixed_doc["errors"]) == len(bad_names),
              f"{len(bad_names)} 个坏文件各有错误记录", f"实际 {len(mixed_doc['errors'])}")
        missing = [name for name in bad_names if name not in mixed_doc["errors"]]
        check(not missing, "每个坏文件都被点名", f"缺失 {missing}")
        detailed = {name: text for name, text in mixed_doc["errors"].items() if len(text) > 60}
        check(len(detailed) == len(mixed_doc["errors"]), "每条错误都带定位信息",
              f"只有 {len(detailed)}/{len(mixed_doc['errors'])} 条够详细")
        # 坏文件是"数据问题"而不是解析器 bug：错误必须是一行原因（+ 抛出位置），
        # 不能把整段 Python traceback 甩给前端——那会把界面横幅撑爆且看不出原因。
        traced = {name: text for name, text in mixed_doc["errors"].items() if "Traceback" in text}
        check(not traced, "坏文件的错误不再包含 traceback", f"{len(traced)} 条仍带 traceback：{list(traced)[:3]}")
        check(all("parse_ap18_pcap.py:" in text or "ap18_visualizer.py:" in text
                  for text in mixed_doc["errors"].values()),
              "每条错误都带抛出位置（文件:行号）",
              str([text[-40:] for text in list(mixed_doc["errors"].values())[:2]]))
        magic_message = mixed_doc["errors"].get("bad_magic.pcap", "")
        check("文件头" in magic_message and "pcap" in magic_message,
              "magic 错误说的是人话（点出文件头不对）", magic_message[:120])
        placeholder = mixed_doc["errors"].get("zero_magic.pcap", "")
        check("录制" in placeholder or "全为 0" in placeholder,
              "全 0 文件头提示可能是正在录制的文件", placeholder[:160])
        # 顺序无关：成功文件按上传顺序排列（好文件在混合批次里的相对顺序是 0,1,2）
        check([f["name"] for f in mixed_doc["files"]] == [p.name for p in good_files],
              "成功文件仍按上传顺序排列", str([f["name"] for f in mixed_doc["files"]]))
        source_files = sorted({row["_source_file"] for row in mixed_doc["rows"]})
        check(source_files == sorted(p.name for p in good_files),
              "_source_file 只出现好文件", str(source_files))

        print(f"\n[3] 全坏批次：{len(bad_names)} 个坏文件")
        status, all_bad = post(url, all_bad_ctype, all_bad_body)
        check(status == 200, "HTTP 200（不是 500）", f"status={status}")
        check(not all_bad["files"], "成功列表为空")
        check(not all_bad["rows"], "rows 为空")
        check(len(all_bad["errors"]) == len(bad_names),
              f"{len(bad_names)} 个文件全部有错误记录", f"实际 {len(all_bad['errors'])}")

        print("\n[4] 与直连解码对照（坏文件应逐个失败，不影响好文件）")
        for path in good_files:
            direct = decode_file(path)[0]
            subset = [row for row in mixed_doc["rows"] if row["_source_file"] == path.name]
            check(len(direct) == len(subset), f"{path.name} 行数 {len(direct):,}", f"返回 {len(subset):,}")
        direct_failures = []
        for name in bad_names:
            try:
                decode_file(bad_files[name])
                direct_failures.append(name)
            except Exception:  # noqa: BLE001 - 这里就是要它抛
                pass
        check(not direct_failures,
              f"{len(bad_names)} 个构造的坏文件在直连解码下全部抛异常（空文件也一样）",
              f"没抛的：{direct_failures}")

        print("\n[5] 同批次重名文件：必须各自独立（错误不覆盖、数据不合并）")
        duplicate_pairs = build_duplicate_pairs(good_files[0])
        expected_good_rows = len(decode_file(good_files[0])[0])
        chunks: list[bytes] = []
        boundary = "----DupProbe"
        for name, (first, second) in duplicate_pairs.items():
            for content in (first, second):
                chunks.append(f"--{boundary}\r\n".encode())
                chunks.append(
                    f'Content-Disposition: form-data; name="files"; filename="{name}"\r\n\r\n'.encode()
                )
                chunks.append(content)
                chunks.append(b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode())
        status, dup_doc = post(url, f"multipart/form-data; boundary={boundary}", b"".join(chunks))
        check(status == 200, "HTTP 200")
        dup_names = [meta["name"] for meta in dup_doc["files"]]
        check(len(dup_names) == len(duplicate_pairs), f"{len(duplicate_pairs)} 个好文件都被保留",
              f"实际 {len(dup_names)}：{dup_names}")
        check(len(set(dup_names)) == len(dup_names), "返回的文件名互不重复", str(dup_names))
        check(len(dup_doc["errors"]) == len(duplicate_pairs),
              f"{len(duplicate_pairs)} 个坏文件各有独立错误记录", f"实际 {len(dup_doc['errors'])}")
        total_dup_rows = len(dup_doc["rows"])
        check(total_dup_rows == expected_good_rows * len(duplicate_pairs),
              f"行数 = {len(duplicate_pairs)} × {expected_good_rows:,}（两份数据没有被合并/覆盖）",
              f"实际 {total_dup_rows:,}")
        per_source = {}
        for row in dup_doc["rows"]:
            per_source[row["_source_file"]] = per_source.get(row["_source_file"], 0) + 1
        check(sorted(per_source.values()) == [expected_good_rows] * len(duplicate_pairs),
              "每个来源各占一份完整数据", str(per_source))

        print("\n[6] 错误文本采样")
        for name in bad_names:
            text = mixed_doc["errors"].get(name, "")
            last = [line for line in text.splitlines() if line.strip()][-1:]
            print(f"        {name:<26} {last[0].strip()[:110] if last else '(空)'}")
    finally:
        server.shutdown()
        server.server_close()
        viz.stop_decoder_pool()
        for path in bad_files.values():
            path.unlink(missing_ok=True)
        PROBE_DIR.rmdir()

    print()
    if FAILURES:
        print(f"结果：{len(FAILURES)} 项失败 -> {FAILURES}")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
