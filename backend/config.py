"""全局配置：数据库、采集节奏、限流、路径。

环境变量优先，便于 Linux 服务器上用 systemd 的 Environment 覆盖。
"""
import os
import logging
from pathlib import Path

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_secrets(path: Path):
    """从本地 secrets.env 加载密钥到环境变量（进程环境优先，不覆盖已有值）。

    生产环境推荐用 systemd Environment / 平台注入；本文件仅方便本地与轻量部署，
    且已被 .gitignore 排除，不会进入版本库。
    """
    try:
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v:
                os.environ.setdefault(k, v)
    except Exception as e:
        # 密钥文件读取/解析失败必须可见，否则进程会带着默认值悄悄启动
        log.warning("读取 %s 失败，将沿用进程环境变量或内置开发默认值: %s", path, e)


_load_secrets(BASE_DIR / "data" / "secrets.env")

WEB_DATA_DIR = BASE_DIR / "web" / "data"
SEED_FILE = WEB_DATA_DIR / "records.seed.json"
OUTPUT_FILE = WEB_DATA_DIR / "records.json"
# 按月分片（web/data/hist/YYYY-MM.json）与月份索引：把 1990 年至今的历史从首屏
# 拆出去，records.json 只留最近 RECENT_DAYS（默认 30）个交易日（见 pipeline.export_json）
HIST_DIR = WEB_DATA_DIR / "hist"
# 开源版只保留最近 30 个交易日（可通过 RECORDS_RECENT_DAYS 调整）
RECENT_DAYS = max(1, int(os.getenv("RECORDS_RECENT_DAYS", "30")))
# 种子文件缺失时，首次启动自动回溯的交易日数；设 0 关闭
AUTO_BACKFILL_DAYS = max(0, int(os.getenv("AUTO_BACKFILL_DAYS", "15")))
MONTHS_FILE = WEB_DATA_DIR / "records.months.json"

# ---------- MySQL ----------
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "ashare")
# 不提供可用默认口令：MySQL 部署必须通过环境变量或 secrets.env 显式设置
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "ashare_review")
DB_CHARSET = "utf8mb4"
DB_DRIVER = os.getenv("DB_DRIVER", "sqlite").lower()  # mysql | sqlite（sqlite 供本地验证）
SQLITE_PATH = BASE_DIR / "data" / "ashare.db"

# ---------- 采集 ----------
HISTORY_DAYS = int(os.getenv("HISTORY_DAYS", "60"))  # 历史回溯交易日数

# 收盘后全量采集时间（交易日）
FULL_COLLECT_TIME = os.getenv("FULL_COLLECT_TIME", "15:30")

# 盘中轮询间隔（分钟）。轻量字段（指数/成交额/预估/资金流/广度）每轮都刷；
# 涨停族重接口（fuyao+东财池子约 10 个请求）内部做 120 秒节流，约 2 分钟一轮
INTRADAY_POLL_MINUTES = int(os.getenv("INTRADAY_POLL_MINUTES", "1"))
INTRADAY_START = os.getenv("INTRADAY_START", "09:30")
INTRADAY_END = os.getenv("INTRADAY_END", "15:00")

# ---------- 限流保护（东财 push2 有严格限流）----------
REQUEST_MIN_INTERVAL = float(os.getenv("REQUEST_MIN_INTERVAL", "1.2"))  # 相邻请求最小间隔(秒)
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RETRY_BACKOFF = float(os.getenv("RETRY_BACKOFF", "5"))  # 退避基数(秒)，指数增长

# ---------- 同花顺 fuyao 数据源 ----------
# 提供涨停/跌停/炸板池、封板统计、30日连板天梯、全市场快照、热榜；
# 不提供主力资金与分时（属同花顺AI客户端专用，接口返回 2004）。
FUYAO_ENABLED = os.getenv("FUYAO_ENABLED", "0").lower() in ("1", "true", "yes")
# 开源版默认关闭：fuyao 是可选第三方源，需要你自己的 API Key；未配置时自动走 AkShare。
FUYAO_KEY = os.getenv("FUYAO_KEY", "")
FUYAO_BASE = os.getenv("FUYAO_BASE", "https://fuyao.aicubes.cn")
FUYAO_MIN_INTERVAL = float(os.getenv("FUYAO_MIN_INTERVAL", "0.18"))  # 相邻请求最小间隔(秒)
FUYAO_TIMEOUT = float(os.getenv("FUYAO_TIMEOUT", "25"))
# 数据源角色：fuyao=主源（涨停族+广度），akshare=备用/兜底
PRIMARY_SOURCE = os.getenv("PRIMARY_SOURCE", "akshare").lower()

# ---------- 日志 ----------
LOG_DIR = BASE_DIR / "logs"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ---------- 调度器 ----------
# 是否在此进程内启动定时调度。多 worker 部署时只让一个 worker 开启（见 README），
# 否则每个 worker 都会跑一遍采集，造成重复请求与限流。
ENABLE_SCHEDULER = os.getenv("ENABLE_SCHEDULER", "1").lower() in ("1", "true", "yes")

# 交易日历刷新时间（每天检查一次，缓存过期或跨年时自动重新拉取）
CALENDAR_REFRESH_TIME = os.getenv("CALENDAR_REFRESH_TIME", "02:00")
# 日历缓存有效期（天）；到期后下次访问会重新拉取。跨年时强制失效。
CALENDAR_CACHE_TTL_DAYS = int(os.getenv("CALENDAR_CACHE_TTL_DAYS", "30"))
