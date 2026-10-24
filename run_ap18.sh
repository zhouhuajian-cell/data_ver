#!/usr/bin/env bash
# Linux / macOS 启动脚本（对应 Windows 的 run_ap18_lan.ps1）。
#
#   ./run_ap18.sh                       # 0.0.0.0:8765
#   AP18_PORT=8901 ./run_ap18.sh       # 换端口
#   AP18_HOST=127.0.0.1 ./run_ap18.sh  # 只允许本机
#   PYTHON=/opt/conda/bin/python ./run_ap18.sh
#
# 只依赖 Python 标准库，不需要 pip install。
set -euo pipefail
cd "$(dirname "$0")"

HOST="${AP18_HOST:-0.0.0.0}"
PORT="${AP18_PORT:-8765}"
PYTHON="${PYTHON:-python3}"

exec "$PYTHON" ap18_visualizer.py --host "$HOST" --port "$PORT" "$@"
