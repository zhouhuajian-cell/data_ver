#!/bin/bash
# 参数验证服务 启动脚本（服务器部署用）
set -e

APP_DIR="/home/algo/verify_service"
PY="/home/algo/miniconda3/envs/multi_traj/bin/python"
HOST="${VERIFY_HOST:-0.0.0.0}"
PORT="${VERIFY_PORT:-8801}"
LOG_DIR="$APP_DIR/logs"

mkdir -p "$LOG_DIR" "$APP_DIR/output/runs"
cd "$APP_DIR"

# 已有实例则先停掉，并等端口真正释放，避免新进程起不来或复用旧状态
stop_old() {
  if [ -f /tmp/verify_service.pid ]; then
    OLD_PID=$(cat /tmp/verify_service.pid)
    if kill -0 "$OLD_PID" 2>/dev/null; then
      echo "stopping old instance (pid $OLD_PID)"
      kill "$OLD_PID" 2>/dev/null || true
    fi
    rm -f /tmp/verify_service.pid
  fi
  # 兜底：清掉所有跑 app.py 的进程
  pkill -f "verify_service/app.py" 2>/dev/null || true
  pkill -f "python app.py" 2>/dev/null || true
  # 等端口释放（最多 15 秒）
  for _ in $(seq 1 30); do
    if ! ss -tln 2>/dev/null | grep -q ":$PORT "; then
      return 0
    fi
    sleep 0.5
  done
  echo "WARN: port $PORT still in use, forcing"
  ss -tlnp 2>/dev/null | grep ":$PORT " || true
  return 0
}

stop_old

export PYTHONIOENCODING=utf-8
export VERIFY_HOST="$HOST"
export VERIFY_PORT="$PORT"

nohup "$PY" app.py >> "$LOG_DIR/service.log" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > /tmp/verify_service.pid

sleep 4
if kill -0 "$NEW_PID" 2>/dev/null; then
  echo "started pid=$NEW_PID on $HOST:$PORT"
  echo "log: $LOG_DIR/service.log"
  # 确认端口确实由新进程监听
  if ! ss -tlnp 2>/dev/null | grep ":$PORT " | grep -q "pid=$NEW_PID"; then
    echo "WARN: port $PORT 监听者不是新进程 $NEW_PID"
    ss -tlnp 2>/dev/null | grep ":$PORT " || true
  fi
else
  echo "FAILED to start; see $LOG_DIR/service.log"
  tail -40 "$LOG_DIR/service.log"
  exit 1
fi
