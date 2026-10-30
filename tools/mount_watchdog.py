#!/usr/bin/env python3
"""服务器网盘（CIFS）挂载看门狗。

流程：
1. 检查 /mnt/Data_Platform、/mnt/logs_new 是否可读（超时 8 秒）；
2. 发现不可用：先自动 umount -l 再 mount.cifs 重挂；
3. 挂载状态变化时推飞书告警（掉线 / 自动重挂失败 / 恢复），不重复刷屏；
4. 每次结果写 logs/mount_watchdog.log。

部署：deploy/ap18-mount-watchdog.service + .timer，每 2 分钟跑一次（root）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = Path("/var/lib/ap18/mount_state.json")
LOG_PATH = REPO_ROOT / "logs" / "mount_watchdog.log"
WEBHOOK_FILE = REPO_ROOT / "data" / "feishu_webhook.txt"

# domain 账号 ningbo_new 对这两个共享可读写（历史挂载脚本里验证过）
MOUNT_OPTS = "domain=MAXIEYEAD.com,username=ningbo_new,password=Maxieye@8,uid=1000,gid=1000"
MOUNTS = [
    {"src": "//192.168.20.206/Data_Platform", "mnt": "/mnt/Data_Platform"},
    {"src": "//192.168.20.206/logs_new", "mnt": "/mnt/logs_new"},
]


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def readable(mount: dict) -> bool:
    mnt = mount["mnt"]
    # 挂载点掉线后目录还在，光 ls 会误判正常；必须确认挂载存在 + 内容可读
    if not os.path.ismount(mnt):
        return False
    probe = os.path.join(mnt, mount.get("probe") or "")
    try:
        result = subprocess.run(
            ["timeout", "8", "ls", probe],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        return result.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def remount(mount: dict) -> bool:
    mnt = mount["mnt"]
    log(f"自动重挂：{mnt}")
    subprocess.run(["umount", "-l", mnt], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["mkdir", "-p", mnt], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    result = subprocess.run(
        ["mount", "-t", "cifs", "-o", MOUNT_OPTS, mount["src"], mnt],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log(f"重挂失败：{mnt} -> {(result.stderr or result.stdout).strip()[:300]}")
        return False
    ok = readable(mount)
    log(f"重挂{'成功' if ok else '后仍不可读'}：{mnt}")
    return ok


def feishu_webhook() -> str:
    env = os.environ.get("AP18_FEISHU_WEBHOOK", "").strip()
    if env:
        return env
    try:
        return WEBHOOK_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def notify(title: str, body: str, template: str = "red") -> None:
    url = feishu_webhook()
    if not url:
        log("飞书 webhook 未配置，跳过告警")
        return
    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": body}}],
        },
    }
    try:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            detail = response.read().decode("utf-8", "replace")[:200]
        log(f"飞书告警：发送成功 {detail}")
    except Exception as exc:  # noqa: BLE001
        log(f"飞书告警发送失败：{exc}")


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:  # noqa: BLE001
        log(f"写状态失败（忽略）：{exc}")


def main() -> int:
    state = load_state()
    prev_status = state.get("status") or "ok"
    prev_bad = list(state.get("bad") or [])

    bad = [mount for mount in MOUNTS if not readable(mount)]
    fixed: list[str] = []
    for mount in bad:
        if remount(mount):
            fixed.append(mount["mnt"])

    still_bad = [mount for mount in MOUNTS if not readable(mount)]
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    status = "down" if still_bad else "ok"

    if bad and prev_status == "ok":
        lines = [f"**时间**：{now}", "**不可用挂载点**："]
        for mount in bad:
            state_text = "已自动重挂恢复" if mount["mnt"] in fixed else "自动重挂失败"
            lines.append(f"· {mount['mnt']}（{state_text}）")
        lines.append(f"**当前状态**：{'仍有挂载点不可用，请检查' if still_bad else '已全部恢复'}")
        notify("【服务器告警】网盘掉线", "\n".join(lines), "red" if still_bad else "orange")
    elif still_bad and prev_status != "down":
        lines = [f"**时间**：{now}", "**不可用挂载点**："]
        for mount in still_bad:
            lines.append(f"· {mount['mnt']}（自动重挂失败）")
        notify("【服务器告警】网盘不可用", "\n".join(lines), "red")
    elif not still_bad and prev_status == "down":
        lines = [f"**时间**：{now}", "**已恢复挂载点**："]
        for mount in MOUNTS:
            lines.append(f"· {mount['mnt']}")
        notify("【服务器告警】网盘已恢复", "\n".join(lines), "green")

    save_state(
        {
            "status": status,
            "bad": [mount["mnt"] for mount in still_bad],
            "fixed": fixed,
            "checked_at": now,
            "prev_bad": prev_bad,
        }
    )
    log(f"检查完成：不可用 {len(bad)} 个，自动重挂成功 {len(fixed)} 个，当前状态 {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
