"""存储层：MySQL(pymysql) 生产 / SQLite 本地回退。

通过环境变量 DB_DRIVER=mysql|sqlite 切换。SQL 统一使用 REPLACE INTO，
两种库都支持，避免 ON DUPLICATE KEY 的方言差异。
"""
import sqlite3
import json
import os
from datetime import datetime, timedelta
import logging
from contextlib import contextmanager

from backend import config

log = logging.getLogger(__name__)

USE_SQLITE = config.DB_DRIVER == "sqlite" if hasattr(config, "DB_DRIVER") else False
SQLITE_PATH = config.SQLITE_PATH if hasattr(config, "SQLITE_PATH") else config.BASE_DIR / "data" / "ashare.db"


def _conn():
    if USE_SQLITE:
        SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(SQLITE_PATH), timeout=15)  # busy_timeout：多进程并发写（回填工具 vs 服务）防锁
        c.row_factory = sqlite3.Row
        return c
    import pymysql
    # autocommit=False：replace_children 是「先 DELETE 四张子表再逐条 INSERT」，
    # 自动提交会让 DELETE 立即生效，一旦中途失败就永久丢数据（SQLite 分支是整体回滚）。
    # 交给 cursor() 在成功时 commit、异常时 rollback，两种驱动的事务语义才一致。
    return pymysql.connect(
        host=config.DB_HOST, port=config.DB_PORT, user=config.DB_USER,
        password=config.DB_PASSWORD, database=config.DB_NAME,
        charset=config.DB_CHARSET, autocommit=False,
    )


@contextmanager
def cursor():
    c = _conn()
    try:
        cur = c.cursor()
        yield cur
        c.commit()
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass
        raise
    finally:
        c.close()


def init_db():
    """初始化表结构。MySQL 用 schema.sql；SQLite 用等价简化版。"""
    # 新增字段的幂等迁移（老库也能直接升级）
    fund_cols = {
        "main_net": "DECIMAL(10,2)", "main_pct": "DECIMAL(6,2)",
        "super_net": "DECIMAL(10,2)", "big_net": "DECIMAL(10,2)",
        # 盘中全天成交额预估（按昨日同时段占比外推，2026-11 新增）
        "amt_est": "DECIMAL(8,2)",
        # 盘中数据采集时间 HH:MM（2026-11 新增）
        "data_time": "VARCHAR(8)",
    }
    # 晋级梯队按"梯队"拆分的昨日涨停今表现（2026-11 新增）
    tier_cols = {"yp_avg": "DECIMAL(8,2)", "yp_red": "INT"}
    # 概念热度悬浮明细：该题材当日涨停个股名单 JSON（2026-11 新增）
    heat_cols = {"members": "TEXT"}
    if not USE_SQLITE:
        sql = (config.BASE_DIR / "backend" / "schema.sql").read_text(encoding="utf-8")
        # 按分号切分执行
        with cursor() as cur:
            for stmt in sql.split(";"):
                if stmt.strip():
                    cur.execute(stmt)
            rows = cur.execute("SHOW COLUMNS FROM daily_snapshot").fetchall()
            existing = {r[0] if not isinstance(r, dict) else r["Field"] for r in rows}
            for name, typ in fund_cols.items():
                if name not in existing:
                    cur.execute(f"ALTER TABLE daily_snapshot ADD COLUMN {name} {typ}")
            rows = cur.execute("SHOW COLUMNS FROM promo_tier").fetchall()
            existing = {r[0] if not isinstance(r, dict) else r["Field"] for r in rows}
            for name, typ in tier_cols.items():
                if name not in existing:
                    cur.execute(f"ALTER TABLE promo_tier ADD COLUMN {name} {typ}")
            rows = cur.execute("SHOW COLUMNS FROM concept_heat").fetchall()
            existing = {r[0] if not isinstance(r, dict) else r["Field"] for r in rows}
            for name, typ in heat_cols.items():
                if name not in existing:
                    cur.execute(f"ALTER TABLE concept_heat ADD COLUMN {name} {typ}")
            # 大盘异动两表（2026-11 新增，MySQL 建表 + ind 迁移）
            cur.execute("""CREATE TABLE IF NOT EXISTS anomaly_event (
                id INT AUTO_INCREMENT PRIMARY KEY, trade_date VARCHAR(10), tm VARCHAR(8),
                code VARCHAR(10), name VARCHAR(20), etype INT, ind VARCHAR(30),
                INDEX idx_anom_date (trade_date))""")
            rows = cur.execute("SHOW COLUMNS FROM anomaly_event").fetchall()
            existing = {r[0] if not isinstance(r, dict) else r["Field"] for r in rows}
            if "ind" not in existing:
                cur.execute("ALTER TABLE anomaly_event ADD COLUMN ind VARCHAR(30)")
            cur.execute("""CREATE TABLE IF NOT EXISTS index_minute (
                trade_date VARCHAR(10) PRIMARY KEY, points MEDIUMTEXT)""")
            # 多指数分时 + 指数日线（2026-11-21 新增，支持核心指数自选）
            cur.execute("""CREATE TABLE IF NOT EXISTS index_minute_multi (
                trade_date VARCHAR(10), code VARCHAR(10), points MEDIUMTEXT,
                PRIMARY KEY (trade_date, code))""")
            cur.execute("""CREATE TABLE IF NOT EXISTS index_extras (
                trade_date VARCHAR(10), code VARCHAR(10), close DECIMAL(12,2), pct DECIMAL(8,2),
                PRIMARY KEY (trade_date, code))""")
        log.info("MySQL schema 初始化完成")
        return

    with cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS daily_snapshot (
            trade_date TEXT PRIMARY KEY, week TEXT, sh REAL, sh_p REAL, sz REAL, sz_p REAL,
            cy REAL, cy_p REAL, hs REAL, hs_p REAL, z5 REAL, z5_p REAL, z1 REAL, z1_p REAL,
            kc REAL, kc_p REAL, amt REAL, amt_pct REAL, amt_est REAL,
            main_net REAL, main_pct REAL, super_net REAL, big_net REAL,
            up_cnt INT, flat_cnt INT, down_cnt INT,
            zt_cnt INT, dt_cnt INT, zb_cnt INT, zb_rate REAL, lb_max INT, lianban_cnt INT,
            mood INT, yp_avg REAL, yp_red INT, yp_promo INT, data_status INT,
            data_time TEXT, updated_at TEXT
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS promo_tier (
            id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT, label TEXT,
            win_cnt INT, total_cnt INT, yp_avg REAL, yp_red INT, sort_no INT,
            UNIQUE(trade_date,label))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS promo_stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT, label TEXT, code TEXT,
            name TEXT, status INT, industry TEXT)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS lianban_stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT, code TEXT, name TEXT,
            boards INT, industry TEXT)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS concept_heat (
            id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT, concept TEXT,
            zt_cnt INT, rank_no INT, members TEXT, UNIQUE(trade_date,concept))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS collect_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT, task_type TEXT,
            status TEXT, started_at TEXT, finished_at TEXT, duration_s INT,
            rows_written INT, error_msg TEXT, trigger_by TEXT)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS source_health (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_name TEXT, checked_at TEXT,
            status TEXT, latency_ms INT, error_msg TEXT)""")
        # 大盘异动：盘口异动事件流（交易日落库积累历史，2026-11 新增）
        cur.execute("""CREATE TABLE IF NOT EXISTS anomaly_event (
            id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT, tm TEXT,
            code TEXT, name TEXT, etype INT)""")
        cur.execute("""CREATE INDEX IF NOT EXISTS idx_anom_date ON anomaly_event(trade_date)""")
        # ind：历史回填合成事件自带的当日所属行业（涨停/跌停/炸板池），2026-11-20 新增
        anom_cols = {r["name"] for r in cur.execute("PRAGMA table_info(anomaly_event)").fetchall()}
        if "ind" not in anom_cols:
            cur.execute("ALTER TABLE anomaly_event ADD COLUMN ind TEXT")
        # 大盘分时：上证 1 分钟价格序列 JSON（[(HH:MM,price)]，2026-11 新增）
        cur.execute("""CREATE TABLE IF NOT EXISTS index_minute (
            trade_date TEXT PRIMARY KEY, points TEXT)""")
        # 多指数分时 + 指数日线（2026-11-21 新增，支持核心指数自选）
        cur.execute("""CREATE TABLE IF NOT EXISTS index_minute_multi (
            trade_date TEXT, code TEXT, points TEXT, PRIMARY KEY (trade_date, code))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS index_extras (
            trade_date TEXT, code TEXT, close REAL, pct REAL, PRIMARY KEY (trade_date, code))""")
        existing = {r["name"] for r in cur.execute("PRAGMA table_info(daily_snapshot)").fetchall()}
        for name, typ in fund_cols.items():
            if name not in existing:
                cur.execute(f"ALTER TABLE daily_snapshot ADD COLUMN {name} {typ}")
        existing = {r["name"] for r in cur.execute("PRAGMA table_info(promo_tier)").fetchall()}
        for name, typ in tier_cols.items():
            if name not in existing:
                # SQLite 的 ALTER 不接受 DECIMAL/INT 之外的修饰，直接用基础类型
                cur.execute(f"ALTER TABLE promo_tier ADD COLUMN {name} {'REAL' if 'DECIMAL' in typ else 'INTEGER'}")
        existing = {r["name"] for r in cur.execute("PRAGMA table_info(concept_heat)").fetchall()}
        for name, typ in heat_cols.items():
            if name not in existing:
                cur.execute(f"ALTER TABLE concept_heat ADD COLUMN {name} {typ}")
    log.info("SQLite schema 初始化完成: %s", SQLITE_PATH)


def _vals(row_dict, cols):
    return [row_dict.get(c) for c in cols]


def upsert_snapshot(r):
    cols = ["trade_date","week","sh","sh_p","sz","sz_p","cy","cy_p","hs","hs_p",
            "z5","z5_p","z1","z1_p","kc","kc_p","amt","amt_pct","amt_est",
            "main_net","main_pct","super_net","big_net",
            "up_cnt","flat_cnt",
            "down_cnt","zt_cnt","dt_cnt","zb_cnt","zb_rate","lb_max","lianban_cnt",
            "mood","yp_avg","yp_red","yp_promo","data_status","data_time"]
    ph = ",".join(["%s"] * len(cols)) if not USE_SQLITE else ",".join(["?"] * len(cols))
    sql = f"REPLACE INTO daily_snapshot ({','.join(cols)}) VALUES ({ph})"
    with cursor() as cur:
        cur.execute(sql, _vals(r, cols))


def replace_children(trade_date, r):
    """覆盖式写入某日的子表数据。"""
    with cursor() as cur:
        for tbl in ("promo_tier", "promo_stock", "lianban_stock", "concept_heat"):
            if USE_SQLITE:
                cur.execute(f"DELETE FROM {tbl} WHERE trade_date=?", (trade_date,))
            else:
                cur.execute(f"DELETE FROM {tbl} WHERE trade_date=%s", (trade_date,))

        for i, t in enumerate(r.get("promo", [])):
            # 首板没有晋级结果（win=None）。MySQL 的 win_cnt 是 NOT NULL，
            # 统一存 0 占位，读取时由 load_all 按 label 还原成 None。
            win_val = t["win"] if t["win"] is not None else 0
            yp_avg = t.get("ypAvg")
            yp_red = t.get("ypRed")
            if USE_SQLITE:
                cur.execute("INSERT INTO promo_tier (trade_date,label,win_cnt,total_cnt,yp_avg,yp_red,sort_no) VALUES (?,?,?,?,?,?,?)",
                            (trade_date, t["label"], win_val, t["total"], yp_avg, yp_red, i))
            else:
                cur.execute("INSERT INTO promo_tier (trade_date,label,win_cnt,total_cnt,yp_avg,yp_red,sort_no) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                            (trade_date, t["label"], win_val, t["total"], yp_avg, yp_red, i))
            for s in t.get("stocks", []):
                name, status, tag = s[0], s[1], s[2] if len(s) > 2 else ""
                if USE_SQLITE:
                    cur.execute("INSERT INTO promo_stock (trade_date,label,code,name,status,industry) VALUES (?,?,?,?,?,?)",
                                (trade_date, t["label"], "", name, status, tag))
                else:
                    cur.execute("INSERT INTO promo_stock (trade_date,label,code,name,status,industry) VALUES (%s,%s,%s,%s,%s,%s)",
                                (trade_date, t["label"], "", name, status, tag))
        for n, lb, tag in r.get("lianbanList", []):
            if USE_SQLITE:
                cur.execute("INSERT INTO lianban_stock (trade_date,code,name,boards,industry) VALUES (?,?,?,?,?)",
                            (trade_date, "", n, lb, tag))
            else:
                cur.execute("INSERT INTO lianban_stock (trade_date,code,name,boards,industry) VALUES (%s,%s,%s,%s,%s)",
                            (trade_date, "", n, lb, tag))
        for rank, item in enumerate(r.get("heat", [])):
            name, cnt = item[0], item[1]
            members = item[2] if len(item) > 2 else None
            mem_json = json.dumps(members, ensure_ascii=False) if members else None
            if USE_SQLITE:
                cur.execute("INSERT INTO concept_heat (trade_date,concept,zt_cnt,rank_no,members) VALUES (?,?,?,?,?)",
                            (trade_date, name, cnt, rank, mem_json))
            else:
                cur.execute("INSERT INTO concept_heat (trade_date,concept,zt_cnt,rank_no,members) VALUES (%s,%s,%s,%s,%s)",
                            (trade_date, name, cnt, rank, mem_json))


def save_day(r, full=True):
    r = dict(r)
    r["data_status"] = 2 if full else 1
    upsert_snapshot(r)
    if full:
        replace_children(r["trade_date"], r)


def load_all():
    """读取全部快照，按日期倒序，装配子表数据，返回前端 records 结构。"""
    with cursor() as cur:
        cur.execute("SELECT * FROM daily_snapshot ORDER BY trade_date DESC")
        rows = [dict(x) for x in cur.fetchall()]

        # 一次性读出所有子表，按日期分组
        cur.execute("SELECT * FROM promo_tier ORDER BY sort_no")
        tiers_by_date = {}
        for x in cur.fetchall():
            x = dict(x)
            dd = x["trade_date"]
            dd = dd.strftime("%Y-%m-%d") if hasattr(dd, "strftime") else str(dd)[:10]
            tiers_by_date.setdefault(dd, []).append(x)

        cur.execute("SELECT * FROM promo_stock")
        stocks_by_date = {}
        for x in cur.fetchall():
            x = dict(x)
            dd = x["trade_date"]
            dd = dd.strftime("%Y-%m-%d") if hasattr(dd, "strftime") else str(dd)[:10]
            stocks_by_date.setdefault((dd, x["label"]), []).append(x)

        cur.execute("SELECT * FROM lianban_stock ORDER BY boards DESC")
        lb_by_date = {}
        for x in cur.fetchall():
            x = dict(x)
            dd = x["trade_date"]
            dd = dd.strftime("%Y-%m-%d") if hasattr(dd, "strftime") else str(dd)[:10]
            lb_by_date.setdefault(dd, []).append(x)

        cur.execute("SELECT * FROM concept_heat ORDER BY rank_no")
        heat_by_date = {}
        for x in cur.fetchall():
            x = dict(x)
            dd = x["trade_date"]
            dd = dd.strftime("%Y-%m-%d") if hasattr(dd, "strftime") else str(dd)[:10]
            heat_by_date.setdefault(dd, []).append(x)

    # 港股/海外指数日线（恒生、恒生科技、韩国综合、纳斯达克），按日合并进 records
    try:
        ext_by_date = load_index_extras_map()
    except Exception:
        ext_by_date = {}

    out = []
    for r in rows:
        d = r["trade_date"]
        ds = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]

        # 晋级梯队
        promo = []
        for t in tiers_by_date.get(ds, []):
            stocks = [
                [s["name"], s["status"], s["industry"]]
                for s in stocks_by_date.get((ds, t["label"]), [])
            ]
            promo.append({
                "label": t["label"],
                # 首板当日不构成晋级，win/rate 无意义：库里历史行存的是 win_cnt==total_cnt，
                # 直接透传会渲染成 "N/N · 100%" 的假晋级率，故在导出层统一置空。
                "win": None if t["label"] == "首板" else t["win_cnt"],
                "total": t["total_cnt"],
                "rate": None if t["label"] == "首板" else f"{t['win_cnt']}/{t['total_cnt']}",
                # 该梯队昨日涨停股今日的平均涨幅/红盘家数；首板无意义，恒为 None
                "ypAvg": None if t["label"] == "首板" else t.get("yp_avg"),
                "ypRed": None if t["label"] == "首板" else t.get("yp_red"),
                "stocks": stocks,
            })

        lb_list = [[x["name"], x["boards"], x["industry"]] for x in lb_by_date.get(ds, [])]
        heat = []
        for x in heat_by_date.get(ds, []):
            mem = x.get("members")
            if mem:
                try:
                    mem = json.loads(mem)
                except Exception:
                    mem = None
            heat.append([x["concept"], x["zt_cnt"]] + ([mem] if mem else []))
        concepts = heat[:5]

        out.append({
            "date": ds, "week": r["week"],
            "sh": r["sh"], "shP": r["sh_p"], "sz": r["sz"], "szP": r["sz_p"],
            "cy": r["cy"], "cyP": r["cy_p"], "hs": r["hs"], "hsP": r["hs_p"],
            "z5": r["z5"], "z5P": r["z5_p"], "z1": r["z1"], "z1P": r["z1_p"],
            "kc": r["kc"], "kcP": r["kc_p"],
            "ext": ext_by_date.get(ds) or {},
            "amt": r["amt"], "amtPct": r["amt_pct"], "amtEst": r.get("amt_est"),
            "mainNet": r["main_net"], "mainPct": r["main_pct"],
            "superNet": r["super_net"], "bigNet": r["big_net"],
            "up": r["up_cnt"], "flat": r["flat_cnt"], "down": r["down_cnt"],
            "zt": r["zt_cnt"], "dt": r["dt_cnt"], "zb": r["zb_cnt"], "zbrate": r["zb_rate"],
            "lb": r["lb_max"], "lianban": r["lianban_cnt"], "mood": r["mood"],
            "yp": {"avg": r["yp_avg"], "red": r["yp_red"], "promo": r["yp_promo"]},
            "concepts": concepts, "promo": promo, "lianbanList": lb_list, "heat": heat,
            "data_status": r["data_status"], "dataTime": r.get("data_time"),
        })
    return out


# ---------- 大盘异动（事件流 + 指数分钟线） ----------
def save_anomaly_day(trade_date, events, minute_points):
    """交易日收盘后落库：当日异动事件流 + 上证分钟线，覆盖式写入。"""
    with cursor() as cur:
        ph = "?" if USE_SQLITE else "%s"
        cur.execute(f"DELETE FROM anomaly_event WHERE trade_date={ph}", (trade_date,))
        for e in events:
            cur.execute(
                f"INSERT INTO anomaly_event (trade_date,tm,code,name,etype,ind) VALUES ({ph},{ph},{ph},{ph},{ph},{ph})",
                (trade_date, e["t"], e["code"], e["name"], int(e["type"]), e.get("ind") or None))
        cur.execute(
            f"REPLACE INTO index_minute (trade_date,points) VALUES ({ph},{ph})",
            (trade_date, json.dumps(minute_points, ensure_ascii=False, separators=(",", ":"))))


def load_anomaly_day(trade_date):
    """读取某日落库的异动事件与分钟线；分钟线缺失返回 (None, None)。

    事件可能为空列表（历史分钟线回填的日期没有事件流可回溯）。
    """
    with cursor() as cur:
        ph = "?" if USE_SQLITE else "%s"
        cur.execute(f"SELECT tm,code,name,etype,ind FROM anomaly_event WHERE trade_date={ph}", (trade_date,))
        # 映射成 anomaly.build 期望的事件结构（t/type），etype 转回 int
        events = [{"t": x["tm"], "code": x["code"], "name": x["name"], "type": int(x["etype"]),
                   "ind": (x["ind"] or "") if "ind" in x.keys() else ""}
                  for x in cur.fetchall()]
        cur.execute(f"SELECT points FROM index_minute WHERE trade_date={ph}", (trade_date,))
        row = cur.fetchone()
    if not row:
        return None, None
    try:
        points = json.loads(row["points"] if not isinstance(row, dict) else row["points"])
    except Exception:
        return None, None
    return events, points


def theme_name_map(trade_date):
    """某日 个股名→[题材列表]：概念热度 members（含名单）反查 + 连板股行业。"""
    out = {}
    with cursor() as cur:
        ph = "?" if USE_SQLITE else "%s"
        cur.execute(f"SELECT concept,members FROM concept_heat WHERE trade_date={ph}", (trade_date,))
        for x in cur.fetchall():
            x = dict(x)
            mem = x.get("members")
            if not mem:
                continue
            try:
                mem = json.loads(mem)
            except Exception:
                continue
            for n in mem or []:
                if isinstance(n, str) and n:
                    out.setdefault(n, [])
                    if x["concept"] not in out[n]:
                        out[n].append(x["concept"])
        cur.execute(f"SELECT name,industry FROM lianban_stock WHERE trade_date={ph}", (trade_date,))
        for x in cur.fetchall():
            x = dict(x)
            if x.get("name") and x.get("industry"):
                out.setdefault(x["name"], [])
                if x["industry"] not in out[x["name"]]:
                    out[x["name"]].append(x["industry"])
    return out


def latest_full_date():
    """最近一个收盘全量交易日（没有则退最新记录日）。"""
    with cursor() as cur:
        ph = "?" if USE_SQLITE else "%s"
        cur.execute(f"SELECT MAX(trade_date) AS d FROM daily_snapshot WHERE data_status=2", ())
        r = cur.fetchone()
        d = (dict(r) if not isinstance(r, dict) else r).get("d") if r else None
    if d:
        return str(d)[:10]
    return None


# ---------- 采集日志（管理后台） ----------
def log_start(trade_date, task_type, trigger_by="scheduler"):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ph = "?,?,?,?,?" if USE_SQLITE else "%s,%s,%s,%s,%s"
    with cursor() as cur:
        cur.execute(
            f"INSERT INTO collect_log (trade_date,task_type,status,started_at,trigger_by) VALUES ({ph})",
            (trade_date, task_type, "running", now, trigger_by),
        )
        return cur.lastrowid


def log_finish(log_id, status, rows=0, error=None):
    now_dt = datetime.now()
    now = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    duration = None
    with cursor() as cur:
        if USE_SQLITE:
            cur.execute("SELECT started_at FROM collect_log WHERE id=?", (log_id,))
        else:
            cur.execute("SELECT started_at FROM collect_log WHERE id=%s", (log_id,))
        row = cur.fetchone()
        if row and row[0]:
            try:
                duration = max(0, int((now_dt - datetime.strptime(str(row[0])[:19], "%Y-%m-%d %H:%M:%S")).total_seconds()))
            except Exception:
                pass
        if USE_SQLITE:
            cur.execute(
                "UPDATE collect_log SET status=?,finished_at=?,rows_written=?,error_msg=?,duration_s=? WHERE id=?",
                (status, now, rows, error, duration, log_id))
        else:
            cur.execute(
                "UPDATE collect_log SET status=%s,finished_at=%s,rows_written=%s,error_msg=%s,duration_s=%s WHERE id=%s",
                (status, now, rows, error, duration, log_id))


def reset_stale_runs(timeout_min=30):
    """启动时把超过 timeout_min 仍为 running 的采集日志标记为失败（看门狗）。

    进程重启/线程异常可能导致 running 永不收尾；保留这些记录会让后台误以为任务还在跑。
    """
    cutoff = (datetime.now() - timedelta(minutes=timeout_min)).strftime("%Y-%m-%d %H:%M:%S")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with cursor() as cur:
        if USE_SQLITE:
            cur.execute(
                "UPDATE collect_log SET status='failed',finished_at=?,error_msg=?,"
                "duration_s=? WHERE status='running' AND started_at < ?",
                (now, "超时未完成（启动看门狗重置）", timeout_min * 60, cutoff))
        else:
            cur.execute(
                "UPDATE collect_log SET status='failed',finished_at=%s,error_msg=%s,"
                "duration_s=%s WHERE status='running' AND started_at < %s",
                (now, "超时未完成（启动看门狗重置）", timeout_min * 60, cutoff))
        return cur.rowcount


def _log_where(status=None, task_type=None, trade_date=None, src=None):
    """拼采集日志的 WHERE 子句，返回 (sql片段, 参数列表)。

    src（来源分组）把库里杂乱的 trigger_by 归成四类：
      daily     日常任务（排除历史回补，后台默认视图）
      scheduler 定时调度
      admin     后台手动触发
      manual    手动补采（manual / manual-est / manual-fund …）
      backfill  历史回补（backfill / backfill_year / backfill_history …）
    trigger_by 可能为 NULL，统一 IFNULL 兜底，否则 NOT LIKE 会把老记录一起漏掉。
    """
    ph = "?" if USE_SQLITE else "%s"
    where, args = [], []
    if status in ("success", "failed", "running"):
        where.append(f"status={ph}")
        args.append(status)
    if task_type in ("full", "intraday"):
        where.append(f"task_type={ph}")
        args.append(task_type)
    if trade_date:
        where.append(f"trade_date={ph}")
        args.append(trade_date)
    if src == "daily":
        where.append(f"IFNULL(trigger_by,'') NOT LIKE {ph}")
        args.append("backfill%")
    elif src in ("scheduler", "admin"):
        where.append(f"trigger_by={ph}")
        args.append(src)
    elif src in ("manual", "backfill"):
        where.append(f"IFNULL(trigger_by,'') LIKE {ph}")
        args.append(src + "%")
    return ((" WHERE " + " AND ".join(where)) if where else ""), args


def list_logs(page=1, page_size=20, status=None, task_type=None, trade_date=None, src=None):
    """采集日志分页查询，返回 {rows,total,page,page_size,pages}。"""
    ph = "?" if USE_SQLITE else "%s"
    w, args = _log_where(status, task_type, trade_date, src)
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = min(200, max(5, int(page_size)))
    except (TypeError, ValueError):
        page_size = 20
    with cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS c FROM collect_log{w}", tuple(args))
        total = cur.fetchone()["c"] or 0
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    offset = (page - 1) * page_size
    with cursor() as cur:
        cur.execute(
            f"SELECT * FROM collect_log{w} ORDER BY started_at DESC, id DESC "
            f"LIMIT {ph} OFFSET {ph}",
            tuple(args + [page_size, offset]))
        rows = [dict(x) for x in cur.fetchall()]
    return {"rows": rows, "total": total, "page": page,
            "page_size": page_size, "pages": pages}


def log_stats(src=None, task_type=None):
    """采集日志聚合：状态/类型/来源计数 + 最近一次成功与失败时间。

    顶层字段是全局口径（供概览卡用）；filtered 是按当前筛选（来源/类型）算的，
    供筛选 chip 上的数字用——否则「全部 9041」和表格「共 273 条」会对不上。
    """
    ph = "?" if USE_SQLITE else "%s"
    out = {"total": 0, "success": 0, "failed": 0, "running": 0,
           "by_type": {}, "daily": 0, "backfill": 0,
           "last_success": None, "last_failed": None}
    with cursor() as cur:
        cur.execute("SELECT status, COUNT(*) AS c FROM collect_log GROUP BY status")
        for r in cur.fetchall():
            s, c = r["status"], r["c"] or 0
            out["total"] += c
            if s in ("success", "failed", "running"):
                out[s] = c
        cur.execute("SELECT task_type, COUNT(*) AS c FROM collect_log GROUP BY task_type")
        out["by_type"] = {r["task_type"] or "unknown": r["c"] for r in cur.fetchall()}
        # backfill* 是历史回填，占绝大多数；单独拎出来，后台默认视图不看它
        cur.execute("SELECT COUNT(*) AS c FROM collect_log WHERE IFNULL(trigger_by,'') LIKE "
                    + ph, ("backfill%",))
        out["backfill"] = cur.fetchone()["c"] or 0
        out["daily"] = out["total"] - out["backfill"]
        cur.execute("SELECT MAX(started_at) AS t FROM collect_log WHERE status='success'")
        out["last_success"] = cur.fetchone()["t"]
        cur.execute("SELECT MAX(started_at) AS t FROM collect_log WHERE status='failed'")
        out["last_failed"] = cur.fetchone()["t"]

        # 按筛选条件再算一份
        w, args = _log_where(task_type=task_type, src=src)
        filt = {"total": 0, "success": 0, "failed": 0, "running": 0}
        cur.execute(f"SELECT status, COUNT(*) AS c FROM collect_log{w} GROUP BY status", tuple(args))
        for r in cur.fetchall():
            s, c = r["status"], r["c"] or 0
            filt["total"] += c
            if s in ("success", "failed", "running"):
                filt[s] = c
        out["filtered"] = filt
    out["last_success"] = str(out["last_success"])[:19] if out["last_success"] else None
    out["last_failed"] = str(out["last_failed"])[:19] if out["last_failed"] else None
    return out


def save_health(name, status, latency=None, error=None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with cursor() as cur:
        if USE_SQLITE:
            cur.execute("INSERT INTO source_health (source_name,checked_at,status,latency_ms,error_msg) VALUES (?,?,?,?,?)",
                        (name, now, status, latency, error))
        else:
            cur.execute("INSERT INTO source_health (source_name,checked_at,status,latency_ms,error_msg) VALUES (%s,%s,%s,%s,%s)",
                        (name, now, status, latency, error))


def list_health(limit=50):
    with cursor() as cur:
        cur.execute(f"SELECT * FROM source_health ORDER BY checked_at DESC LIMIT {int(limit)}")
        return [dict(x) for x in cur.fetchall()]


def dashboard_stats():
    """管理后台概览统计。"""
    with cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM daily_snapshot")
        total_days = cur.fetchone()["c"]
        cur.execute("SELECT MAX(trade_date) AS d FROM daily_snapshot WHERE data_status=2")
        latest = cur.fetchone()["d"]
        cur.execute("SELECT COUNT(*) AS c FROM collect_log WHERE status='success'")
        ok = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM collect_log WHERE status='failed'")
        fail = cur.fetchone()["c"]
    latest = latest.strftime("%Y-%m-%d") if hasattr(latest, "strftime") else (str(latest)[:10] if latest else None)
    return {"total_days": total_days, "latest_full_date": latest,
            "success_tasks": ok, "failed_tasks": fail}


def get_snapshot(trade_date):
    """读取某交易日快照（用于回填时保留已有字段）。"""
    with cursor() as cur:
        if USE_SQLITE:
            cur.execute("SELECT * FROM daily_snapshot WHERE trade_date=?", (trade_date,))
        else:
            cur.execute("SELECT * FROM daily_snapshot WHERE trade_date=%s", (trade_date,))
        row = cur.fetchone()
        return dict(row) if row else None


# ---------- 多指数分时与收盘价（2026-11-21 新增，支持自选核心指数） ----------
def save_index_minute(trade_date, code, points):
    """某指数当日分钟线覆盖式写入（points=[[HH:MM, price], ...]）。"""
    with cursor() as cur:
        ph = "?" if USE_SQLITE else "%s"
        cur.execute(f"REPLACE INTO index_minute_multi (trade_date,code,points) VALUES ({ph},{ph},{ph})",
                    (trade_date, code, json.dumps(points, ensure_ascii=False, separators=(",", ":"))))


def load_index_minute(trade_date, code):
    """读取某指数某日落库的分钟线；无数据返回 []。"""
    with cursor() as cur:
        ph = "?" if USE_SQLITE else "%s"
        cur.execute(f"SELECT points FROM index_minute_multi WHERE trade_date={ph} AND code={ph}",
                    (trade_date, code))
        row = cur.fetchone()
    if not row:
        return []
    try:
        raw = row["points"] if not isinstance(row, dict) else row["points"]
        return json.loads(raw)
    except Exception:
        return []


def load_index_minute_legacy(trade_date):
    """读取旧版 index_minute 表（2026-11-21 多指数改造前由大盘异动落库）。

    该表以 trade_date 为主键、**只存上证指数一条分钟线**，没有 code 列。
    多指数表 index_minute_multi 上线前的所有历史分时都在这里（实测 10-13~11-20，
    早期 48 点/日、临近 241 点/日）。调用方必须只在 key=='sh' 时用，
    绝不能把上证曲线当深证/创业板返回。无数据返回 []。
    """
    with cursor() as cur:
        ph = "?" if USE_SQLITE else "%s"
        cur.execute(f"SELECT points FROM index_minute WHERE trade_date={ph}", (trade_date,))
        row = cur.fetchone()
    if not row:
        return []
    try:
        raw = row["points"] if not isinstance(row, dict) else row["points"]
        return json.loads(raw)
    except Exception:
        return []


def get_index_prev_close(trade_date, code):
    """某指数某日的前收盘（= 上一个有数据的交易日的收盘）。"""
    with cursor() as cur:
        ph = "?" if USE_SQLITE else "%s"
        cur.execute(f"SELECT close FROM index_extras WHERE code={ph} AND trade_date<{ph} "
                    f"ORDER BY trade_date DESC LIMIT 1", (code, trade_date))
        row = cur.fetchone()
    return float(row["close"]) if row and row["close"] is not None else None


def save_index_extras(rows):
    """批量写入指数日线 [{trade_date, code, close, pct}, ...]，覆盖式。"""
    if not rows:
        return 0
    ph = "?" if USE_SQLITE else "%s"
    with cursor() as cur:
        for r in rows:
            cur.execute(f"REPLACE INTO index_extras (trade_date,code,close,pct) VALUES ({ph},{ph},{ph},{ph})",
                        (r["trade_date"], r["code"], r.get("close"), r.get("pct")))
    return len(rows)


def load_index_extras_map():
    """全部指数日线，返回 {date: {code: [close, pct]}}，供导出合并进 records。"""
    out = {}
    with cursor() as cur:
        cur.execute("SELECT trade_date,code,close,pct FROM index_extras")
        for r in cur.fetchall():
            d = r["trade_date"]
            d = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
            out.setdefault(d, {})[r["code"]] = [r["close"], r["pct"]]
    return out


# A 股指数 key -> daily_snapshot 列名（白名单，避免拼接用户输入）
_INDEX_SNAP_COL = {"sh": "sh", "sz": "sz", "cy": "cy", "kc": "kc",
                   "hs": "hs", "z5": "z5", "z1": "z1"}


def get_index_prev_close_snapshot(trade_date, code):
    """A 股指数某日前收盘，取自 daily_snapshot 已有列（港美股不在其中）。"""
    col = _INDEX_SNAP_COL.get(code)
    if not col:
        return None
    with cursor() as cur:
        ph = "?" if USE_SQLITE else "%s"
        cur.execute(f"SELECT {col} AS v FROM daily_snapshot WHERE trade_date<{ph} "
                    f"AND {col} IS NOT NULL ORDER BY trade_date DESC LIMIT 1", (trade_date,))
        row = cur.fetchone()
    try:
        return float(row["v"]) if row and row["v"] is not None else None
    except Exception:
        return None
