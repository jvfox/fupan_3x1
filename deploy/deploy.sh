#!/usr/bin/env bash
# Linux 服务器一键部署/更新脚本
# 用法: bash deploy/deploy.sh [init|start|stop|update]
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$APP_DIR/.venv/bin/python"
PIP="$APP_DIR/.venv/bin/pip"
PORT="${PORT:-8000}"

cd "$APP_DIR"

create_venv() {
  if [ ! -d .venv ]; then
    echo "==> 创建虚拟环境"
    python3 -m venv .venv
  fi
  echo "==> 安装依赖"
  "$PIP" install -q --upgrade pip
  "$PIP" install -q -r backend/requirements.txt
}

init_db_mysql() {
  echo "==> 初始化 MySQL 表结构"
  $PY - <<'PY'
from backend import config, storage
# 用 MySQL 驱动初始化
config.DB_DRIVER = 'mysql'
import importlib; importlib.reload(storage)
storage.init_db()
print('MySQL 初始化完成')
PY
}

case "${1:-init}" in
  init)
    create_venv
    echo "==> 初始化 SQLite（本地验证）"
    DB_DRIVER=sqlite $PY -c "from backend import storage; storage.init_db(); print('SQLite OK')"
    echo "==> 空库时导入最近 30 日种子数据（幂等）"
    DB_DRIVER=sqlite $PY -m backend.seed_import
    DB_DRIVER=sqlite $PY -c "from backend import pipeline; pipeline.export_json(); print('导出 OK')"
    echo "==> 完成。启动: bash deploy/deploy.sh start"
    ;;
  start)
    echo "==> 部署前安全检查"
    bash deploy/preflight.sh || { echo "预检未通过，已中止启动"; exit 1; }
    echo "==> 启动服务（仅本机 $PORT，由 Nginx 对外提供 HTTPS）"
    nohup "$PY" -m uvicorn backend.admin.app:app --host 127.0.0.1 --port "$PORT" > logs/server.log 2>&1 &
    echo "服务已启动 PID=$!，日志: logs/server.log"
    ;;
  stop)
    echo "==> 停止服务"
    pkill -f "uvicorn backend.admin.app:app" || true
    ;;
  update)
    echo "==> 拉取代码并重启"
    git pull
    create_venv
    bash deploy/deploy.sh stop
    bash deploy/deploy.sh start
    ;;
  *)
    echo "用法: bash deploy/deploy.sh [init|start|stop|update]"
    ;;
esac
