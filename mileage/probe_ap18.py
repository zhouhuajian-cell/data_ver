"""Small read-only probe for AP18 PCAP UDP payload layout."""

import argparse
import datetime as dt
import struct


def packets(path):
    with open(path, "rb") as f:
        header = f.read(24)
        if len(header) != 24:
            return
        magic = header[:4]
        endian = "<" if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1") else ">"
        nano = magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d")
        while True:
            record = f.read(16)
            if len(record) < 16:
                return
            sec, frac, captured, original = struct.unpack(endian + "IIII", record)
            frame = f.read(captured)
            if len(frame) < captured:
                return
            scale = 1_000_000_000 if nano else 1_000_000
            yield sec + frac / scale, frame, original


def udp_payload(frame):
    if len(frame) < 42 or frame[12:14] != b"\x08\x00":
        return None
    ip = 14
    ihl = (frame[ip] & 0x0F) * 4
    if frame[ip + 9] != 17:
        return None
    udp = ip + ihl
    length = struct.unpack_from("!H", frame, udp + 4)[0]
    return frame[udp + 8 : udp + length]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("file")
    parser.add_argument("--count", type=int, default=5)
    args = parser.parse_args()

    shown = 0
    for timestamp, frame, original in packets(args.file):
        payload = udp_payload(frame)
        if payload is None:
            continue
        print("=" * 72)
        print(dt.datetime.fromtimestamp(timestamp).isoformat(), "frame", len(frame), original, "payload", len(payload))
        print(payload.hex(" "))
        if len(payload) >= 115:
            print("aligned doubles@10", [round(struct.unpack_from("<d", payload, 10 + i * 8)[0], 12) for i in range(13)])
            print("tail status", payload[114])
        for offset in range(0, min(24, len(payload) - 8)):
            double = struct.unpack_from("<d", payload, offset)[0]
            if abs(double) < 1e12 and double == double:
                print(f"offset {offset:02d} <d {double:.12g}")
        print("floats@0", [round(value, 8) for value in struct.unpack_from("<" + "f" * (len(payload) // 4), payload[: len(payload) // 4 * 4])])
        shown += 1
        if shown >= args.count:
            break


if __name__ == "__main__":
    main()
