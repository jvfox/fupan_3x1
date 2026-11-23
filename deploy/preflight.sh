#!/usr/bin/env bash
# 部署前检查：面向公众开放前跑一遍，列出所有不安全的默认值和缺失项
# 用法: bash deploy/preflight.sh
set -uo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"

RED='\033[0;31m'; GREEN='\033[0;32m'; YEL='\033[0;33m'; NC='\033[0m'
PASS=0; WARN=0; FAIL=0

ok(){ echo -e "  ${GREEN}[OK]${NC} $1"; PASS=$((PASS+1)); }
warn(){ echo -e "  ${YEL}[WARN]${NC} $1"; WARN=$((WARN+1)); }
fail(){ echo -e "  ${RED}[FAIL]${NC} $1"; FAIL=$((FAIL+1)); }

# 只读解析 KEY=VALUE，不 source、不打印值，避免密钥进入 shell 历史或日志
load_env_file(){
  local file="$1" line key val
  [ -f "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    case "$line" in
      ''|'#'*) continue ;;
      *=*) ;;
      *) continue ;;
    esac
    key="${line%%=*}"; val="${line#*=}"
    key="${key#"${key%%[![:space:]]*}"}"; key="${key%"${key##*[![:space:]]}"}"
    val="${val#"${val%%[![:space:]]*}"}"; val="${val%"${val##*[![:space:]]}"}"
    val="${val#\"}"; val="${val%\"}"; val="${val#\'}"; val="${val%\'}"
    [ -n "$key" ] && export "$key=$val"
  done < "$file"
}
load_env_file "$APP_DIR/data/secrets.env"

echo "=== A股复盘工具 · 部署前检查 ==="
echo ""

echo "[1] 安全配置（面向公众部署必须修改）"
check_secret(){
  local name="$1" val="$2"; shift 2
  if [ -z "$val" ]; then fail "$name 未设置，公网部署禁止使用默认值"; return; fi
  for default in "$@"; do
    if [ "$val" = "$default" ]; then fail "$name 仍是默认值，必须修改！"; return; fi
  done
  ok "$name 已自定义"
}
check_flag(){
  local name="$1" expect="$2" val="${!1:-}"
  if [ "$val" = "$expect" ]; then ok "$name=$expect"; else fail "$name 必须为 $expect（当前未按要求设置）"; fi
}
check_secret "ADMIN_PASSWORD" "${ADMIN_PASSWORD:-}" "admin123" "请改成强密码" "CHANGE_ME"
# 覆盖 deploy/.env.example 与 data/.env.example 两套模板的全部占位串，
# 否则按中文模板复制却没改时会被误判为通过（ADMIN_SECRET 是会话签名密钥，
# 用占位值等于任何人都能离线伪造管理员 Cookie）。
check_secret "ADMIN_SECRET"   "${ADMIN_SECRET:-}"   "dev-secret-change-me" "CHANGE_ME_TO_RANDOM_STRING" "请改成随机密钥" "CHANGE_ME"
# 单账号：只校验用户名不是占位值（角色体系已于 2026-11-23 移除）
case "${ADMIN_USER:-admin}" in
  admin|CHANGE_ME*|"") warn "ADMIN_USER 仍是默认/占位值，建议改成自己的用户名" ;;
  *) ok "ADMIN_USER 已自定义" ;;
esac
if [ "${DB_DRIVER:-sqlite}" = "mysql" ]; then
  check_secret "DB_PASSWORD" "${DB_PASSWORD:-}" "ashare_pwd" "CHANGE_ME"
else
  ok "DB_DRIVER=sqlite（本地/验证环境，跳过 MySQL 口令检查）"
fi
check_flag SECURE_MODE 1
check_flag SECURE_COOKIE 1
check_flag TRUST_XFF 1
if [ "${ADMIN_PATH:-admin}" = "admin" ]; then
  warn "ADMIN_PATH 用默认值 admin，建议改成不易猜测的路径"
else ok "ADMIN_PATH 已自定义"; fi

echo ""
echo "[1b] systemd 单元"
if grep -Eq 'CHANGE_ME|0\.0\.0\.0' deploy/ashare-review.service; then
  fail "systemd 单元仍含占位密钥或 0.0.0.0 监听"
else ok "systemd 单元无占位密钥，且只监听 127.0.0.1"; fi
if grep -q 'EnvironmentFile=.*secrets.env' deploy/ashare-review.service; then
  ok "systemd 使用 EnvironmentFile 读取密钥"
else fail "systemd 未使用 EnvironmentFile，请移除内联口令"; fi

echo ""
echo "[2] Python 环境与依赖"
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; warn "未找到 .venv，用系统 python3"; fi
DB_DRIVER="${DB_DRIVER:-sqlite}" $PY - <<'PY'
import importlib, os, sys
mods = ["akshare","fastapi","uvicorn","apscheduler","multipart","requests","numpy"]
if os.getenv("DB_DRIVER") == "mysql":
    mods.append("pymysql")
bad = []
for m in mods:
    try: importlib.import_module(m)
    except Exception as e: bad.append((m, str(e)))
if bad:
    for m,e in bad: print(f"  \033[0;31m[FAIL]\033[0m 缺少依赖 {m}: {e}")
    sys.exit(1)
print(f"  \033[0;32m[OK]\033[0m 依赖齐全 ({len(mods)} 个)")
PY
[ $? -eq 0 ] && PASS=$((PASS+1)) || FAIL=$((FAIL+1))

echo ""
echo "[3] 数据与配置文件"
for f in web/index.html web/data/records.json web/data/records.seed.json \
         backend/schema.sql backend/indicators.py deploy/nginx.conf deploy/ashare-review.service; do
  if [ -f "$f" ]; then ok "$f 存在"; else fail "$f 缺失"; fi
done

echo ""
echo "[4] 交易日历缓存"
if [ -d data/calendar ] && [ -f data/calendar/_meta.json ]; then
  ok "日历缓存存在（部署后会自动检查刷新）"
else
  warn "日历缓存缺失，首次运行会自动拉取（需联网）"
fi

echo ""
echo "[5] 定时调度器"
echo "  - 确认 systemd 单元 ENABLE_SCHEDULER=1（单 worker）"
echo "  - 多 worker 时仅一个设 1，其余设 0，避免重复采集"
ok "调度器检查项已列出"

echo ""
echo "=== 结果: ${PASS} 通过 / ${WARN} 警告 / ${FAIL} 失败 ==="
if [ $FAIL -gt 0 ]; then
  echo -e "${RED}存在必须修复的问题，请勿直接对公众开放！${NC}"
  exit 1
fi
if [ $WARN -gt 0 ]; then
  echo -e "${YEL}有警告项，建议处理后上线${NC}"
fi
echo -e "${GREEN}检查通过${NC}"
