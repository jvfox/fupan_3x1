#!/usr/bin/env bash
# 数据备份脚本：备份 MySQL 数据库 + 导出的 JSON + 交易日历缓存
# 用法: bash deploy/backup.sh [保留天数,默认7]
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
KEEP="${1:-7}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
TS="$(date +%Y%m%d_%H%M%S)"
TARGET="$BACKUP_DIR/$TS"
# 备份目录与文件默认仅服务账户可读
OLD_UMASK="$(umask)"
umask 077
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR" 2>/dev/null || true
TARGET="$BACKUP_DIR/$TS"
mkdir -p "$TARGET"
chmod 700 "$TARGET" 2>/dev/null || true

DB_USER="${DB_USER:-ashare}"
DB_PASSWORD="${DB_PASSWORD:-}"
DB_NAME="${DB_NAME:-ashare_review}"

echo "==> 备份到 $TARGET"

# MySQL 备份（若装了 mysqldump 且提供密码）
if command -v mysqldump >/dev/null 2>&1 && [ -n "$DB_PASSWORD" ]; then
  # MYSQL_PWD 不经命令行参数传递，避免 ps aux 泄露口令
  MYSQL_PWD="$DB_PASSWORD" mysqldump -u"$DB_USER" "$DB_NAME" | gzip > "$TARGET/db.sql.gz"
  chmod 600 "$TARGET/db.sql.gz" 2>/dev/null || true
  echo "  MySQL 数据库已备份"
else
  echo "  跳过 MySQL 备份（未配置 mysqldump 或密码）"
fi

# SQLite 备份（本地验证环境）
if [ -f "$APP_DIR/data/ashare.db" ]; then
  cp "$APP_DIR/data/ashare.db" "$TARGET/ashare.db"
  echo "  SQLite 数据库已备份"
fi

# 导出的 JSON 与日历缓存
cp -r "$APP_DIR/web/data" "$TARGET/web_data" 2>/dev/null || true
cp -r "$APP_DIR/data/calendar" "$TARGET/calendar" 2>/dev/null || true
echo "  JSON 与缓存已备份"

# 清理过期备份
find "$BACKUP_DIR" -maxdepth 1 -type d -name "20*" -mtime +"$KEEP" -exec rm -rf {} + 2>/dev/null || true
umask "$OLD_UMASK"
echo "==> 完成。保留最近 $KEEP 天"
