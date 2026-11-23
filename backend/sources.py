"""数据源注册表与健康探测。

把「数据来自哪里、谁主谁备、各自能干什么」显式化，供管理后台的数据源页展示，
也让采集链路的降级关系有单一事实来源（pipeline 按 PRIMARY_SOURCE 走）。

探测策略：
  fuyao / 新浪 / 乐咕 支持主动探测（轻量、无限流风险）；
  东财 push2 系列主动探测会触发 IP 限流，平时标记为「按需」，只在深度探测时打一次。
"""
import os
import time
import glob
import json
import logging
from datetime import datetime

from backend import config, fuyao, storage

log = logging.getLogger(__name__)

# ---------- 数据源注册表 ----------
SOURCES = [
    {
        "id": "fuyao",
        "name": "同花顺金融数据",
        "host": "fuyao.aicubes.cn",
        "role": "primary",
        "priority": 1,
        "desc": "涨停族与市场广度主源，历史可回溯 1 年以上",
        "capabilities": [
            {"name": "涨停/跌停/炸板池", "level": "full", "detail": "含涨停时间、原因、封单额、开板次数"},
            {"name": "连板天梯", "level": "full", "detail": "近 30 个交易日一次返回"},
            {"name": "封板率统计", "level": "full", "detail": "封板率、开板家数、连板档位"},
            {"name": "全市场快照", "level": "full", "detail": "5584 只，6 页约 1.8 秒"},
            {"name": "热榜 / 龙虎榜", "level": "full", "detail": "热股榜、飙升榜、异动原因"},
        ],
        "gaps": ["主力资金（客户端专用 code=2004）", "历史分时（未开放外部接入）"],
        "probe": "fuyao",
    },
    {
        "id": "sina",
        "name": "新浪财经",
        "host": "hq.sinajs.cn / finance.sina.com.cn",
        "role": "backup",
        "priority": 2,
        "desc": "指数实时与历史、个股日 K 与分时的稳定源",
        "capabilities": [
            {"name": "指数实时", "level": "full", "detail": "上证/深证/创业板/沪深300 等"},
            {"name": "指数历史日线", "level": "full", "detail": "历史回填用，不碰东财"},
            {"name": "个股日 K", "level": "full", "detail": "悬浮图表，前复权"},
            {"name": "个股分时", "level": "full", "detail": "按交易日取分钟线"},
        ],
        "gaps": ["涨停池", "涨跌家数"],
        "probe": "sina",
    },
    {
        "id": "eastmoney",
        "name": "东方财富",
        "host": "push2.eastmoney.com / push2his",
        "role": "backup",
        "priority": 3,
        "desc": "主力资金唯一可用源，但限流严格，仅按需调用",
        "capabilities": [
            {"name": "大盘主力资金", "level": "full", "detail": "净额/净占比，121 个交易日"},
            {"name": "涨停/跌停/炸板池", "level": "fallback", "detail": "同花顺失败时兜底"},
            {"name": "概念/行业热度", "level": "fallback", "detail": "按所属行业统计"},
        ],
        "gaps": ["高频调用会触发 IP 限流（5~40 分钟）"],
        "probe": "eastmoney",
    },
    {
        "id": "legu",
        "name": "乐咕乐股",
        "host": "legulegu.com",
        "role": "backup",
        "priority": 4,
        "desc": "市场活跃度兜底，仅实时值、无法回溯",
        "capabilities": [
            {"name": "涨跌家数", "level": "fallback", "detail": "同花顺快照失败时兜底"},
        ],
        "gaps": ["只有实时值", "无历史"],
        "probe": "legu",
    },
]

BY_ID = {s["id"]: s for s in SOURCES}


# ---------- 探测 ----------
def _probe_fuyao(deep):
    r = fuyao.ping()
    return bool(r.get("ok")), r.get("latency_ms"), None if r.get("ok") else "无返回数据"


def _probe_sina(deep):
    from backend import collector
    t0 = time.time()
    idx = collector.fetch_indices_spot()
    ms = int((time.time() - t0) * 1000)
    return bool(idx), ms, None if idx else "指数接口无数据"


def _probe_legu(deep):
    from backend import collector
    t0 = time.time()
    act = collector.fetch_activity()
    ms = int((time.time() - t0) * 1000)
    ok = bool(act and act.get("上涨") is not None)
    return ok, ms, None if ok else "活跃度接口无数据"


def _probe_eastmoney(deep):
    """东财默认不探测：主动打 push2his 会触发限流。深度探测才打一次。"""
    if not deep:
        return None, None, None
    from backend import collector
    t0 = time.time()
    m = collector.fetch_fund_flow(force=True)
    ms = int((time.time() - t0) * 1000)
    ok = bool(m)
    return ok, ms, None if ok else "资金流向无数据（可能已限流）"


PROBES = {
    "fuyao": _probe_fuyao,
    "sina": _probe_sina,
    "legu": _probe_legu,
    "eastmoney": _probe_eastmoney,
}


def probe(source_id, deep=False):
    fn = PROBES.get(source_id)
    if not fn:
        return None
    t0 = time.time()
    try:
        ok, ms, err = fn(deep)
    except Exception as e:
        ok, ms, err = False, int((time.time() - t0) * 1000), str(e)[:160]
    if ok is None:
        return None
    status = "online" if ok else "offline"
    try:
        storage.save_health(BY_ID[source_id]["name"], status, ms, err)
    except Exception:
        pass
    return {"status": status, "latency_ms": ms, "error": err}


def recent_health():
    """每个源最近一次探测结果。"""
    try:
        rows = storage.list_health(limit=120)
    except Exception:
        return {}
    out = {}
    for r in rows:
        name = r.get("source_name")
        if name and name not in out:
            out[name] = {
                "status": r.get("status"),
                "latency_ms": r.get("latency_ms"),
                "checked_at": str(r.get("checked_at"))[:19],
                "error": r.get("error_msg"),
            }
    return out


def registry(deep=False, live=True):
    """完整数据源清单 + 探测结果。

    live=False 时完全不打网络，只用库里最近一次健康记录（页面秒开）；
    点「深度探测」才真正逐个请求上游。
    """
    probed = {}
    if live:
        for s in SOURCES:
            r = probe(s["id"], deep=deep)
            if r:
                probed[s["name"]] = r
    health = recent_health()
    out = []
    for s in SOURCES:
        h = dict(health.get(s["name"]) or {})
        h.update(probed.get(s["name"]) or {})
        lat = h.pop("latency_ms", None)
        status = h.pop("status", None) or "unknown"
        if status == "unknown" and s["id"] == "eastmoney":
            status = "ondemand"
        out.append({
            **{k: s[k] for k in ("id", "name", "host", "role", "priority", "desc",
                                 "capabilities", "gaps")},
            "status": status,
            "latency_ms": lat,
            "checked_at": h.get("checked_at"),
            "error": h.get("error"),
        })
    return out


# ---------- 本地数据源 ----------
def _dir_size(path, pattern="*"):
    files = glob.glob(os.path.join(path, pattern))
    size = 0
    for f in files:
        try:
            size += os.path.getsize(f)
        except OSError:
            pass
    return len(files), size


def _mb(n):
    return round(n / 1024 / 1024, 2)


def local_sources():
    """本地库与本地缓存的真实统计（对应截图里的「本地数据源」）。"""
    out = []

    # 业务数据库
    db_rows = {"days": 0, "latest": None, "zt_rows": 0, "lb_rows": 0, "concept_rows": 0,
               "promo_rows": 0, "ok_tasks": 0, "fail_tasks": 0}
    size = None
    try:
        with storage.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM daily_snapshot")
            db_rows["days"] = cur.fetchone()["c"]
            cur.execute("SELECT MAX(trade_date) AS d FROM daily_snapshot WHERE data_status=2")
            v = cur.fetchone()["d"]
            db_rows["latest"] = v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else (str(v)[:10] if v else None)
            for tbl, key in (("promo_stock", "promo_rows"), ("lianban_stock", "lb_rows"),
                             ("concept_heat", "concept_rows")):
                cur.execute(f"SELECT COUNT(*) AS c FROM {tbl}")
                db_rows[key] = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM collect_log WHERE status='success'")
            db_rows["ok_tasks"] = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM collect_log WHERE status='failed'")
            db_rows["fail_tasks"] = cur.fetchone()["c"]
    except Exception as e:
        log.warning("读取库统计失败: %s", e)

    if storage.USE_SQLITE and config.SQLITE_PATH.exists():
        size = _mb(config.SQLITE_PATH.stat().st_size)

    out.append({
        "id": "db", "name": "业务数据库",
        "path": str(config.SQLITE_PATH) if storage.USE_SQLITE else f"{config.DB_HOST}/{config.DB_NAME}",
        "engine": "SQLite" if storage.USE_SQLITE else "MySQL",
        "mode": "读写",
        "metrics": [
            {"k": "交易日", "v": db_rows["days"]},
            {"k": "最新全量", "v": db_rows["latest"] or "—"},
            {"k": "涨停行", "v": db_rows["promo_rows"]},
            {"k": "连板行", "v": db_rows["lb_rows"]},
            {"k": "题材行", "v": db_rows["concept_rows"]},
            {"k": "体积", "v": f"{size} MB" if size is not None else "—"},
        ],
        "note": f"采集成功 {db_rows['ok_tasks']} 次 · 失败 {db_rows['fail_tasks']} 次",
    })

    # 前端数据缓存
    data_dir = str(config.WEB_DATA_DIR)
    n_files, n_size = _dir_size(data_dir, "*.json")
    rec_cnt = seed_cnt = 0
    try:
        with open(config.OUTPUT_FILE, encoding="utf-8") as f:
            rec_cnt = len(json.load(f))
    except Exception:
        pass
    try:
        with open(config.SEED_FILE, encoding="utf-8") as f:
            seed_cnt = len(json.load(f))
    except Exception:
        pass
    samples = 0
    try:
        sp = config.WEB_DATA_DIR / "mood_samples.json"
        with open(sp, encoding="utf-8") as f:
            d = json.load(f)
        samples = len(d.get("samples") or []) if isinstance(d, dict) else len(d)
    except Exception:
        pass
    out.append({
        "id": "export", "name": "前端数据缓存",
        "path": data_dir, "engine": "JSON 静态导出", "mode": "只读",
        "metrics": [
            {"k": "导出条数", "v": rec_cnt},
            {"k": "种子条数", "v": seed_cnt},
            {"k": "情绪样本", "v": samples},
            {"k": "文件数", "v": n_files},
            {"k": "体积", "v": f"{_mb(n_size)} MB"},
        ],
        "note": "看板直接 fetch，无需后端参与",
    })

    # 图表缓存
    chart_dir = config.BASE_DIR / "data" / "charts"
    n_charts, chart_size = _dir_size(str(chart_dir), "*.json") if chart_dir.exists() else (0, 0)
    chart_latest = None
    if n_charts:
        try:
            newest = max(glob.glob(os.path.join(str(chart_dir), "*.json")), key=os.path.getmtime)
            chart_latest = datetime.fromtimestamp(os.path.getmtime(newest)).strftime("%m-%d %H:%M")
        except Exception:
            pass
    out.append({
        "id": "charts", "name": "个股图表缓存",
        "path": str(chart_dir), "engine": "文件缓存", "mode": "读写",
        "metrics": [
            {"k": "缓存标的", "v": n_charts},
            {"k": "体积", "v": f"{_mb(chart_size)} MB"},
            {"k": "最近写入", "v": chart_latest or "—"},
        ],
        "note": "悬浮 K 线/分时结果落盘，避免重复请求新浪",
    })

    # 交易日历缓存
    cal_dir = config.BASE_DIR / "data" / "calendar"
    n_cal, _ = _dir_size(str(cal_dir), "*.json") if cal_dir.exists() else (0, 0)
    out.append({
        "id": "calendar", "name": "交易日历缓存",
        "path": str(cal_dir), "engine": "文件缓存", "mode": "只读",
        "metrics": [
            {"k": "缓存文件", "v": n_cal},
            {"k": "数据至", "v": db_rows["latest"] or "—"},
        ],
        "note": "跨年或过期自动重新拉取",
    })
    return out


def overview(sources=None, locals_=None):
    """顶部概览：当前主源 / 在线数 / 覆盖标的 / 最近探测。"""
    sources = sources if sources is not None else registry()
    online = [s for s in sources if s["status"] == "online"]
    lats = [s["latency_ms"] for s in online if s.get("latency_ms")]
    # 覆盖标的：本地名称→代码映射表条数（首次悬停图表时生成，无需联网）
    covered = None
    try:
        from backend import chart
        p = chart.CODE_CACHE
        if p.exists():
            covered = len(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        pass
    checked = [s.get("checked_at") for s in sources if s.get("checked_at")]
    return {
        "primary": config.PRIMARY_SOURCE,
        "primary_name": BY_ID.get(config.PRIMARY_SOURCE, {}).get("name", config.PRIMARY_SOURCE),
        "online": len(online),
        "total": len(sources),
        "avg_latency_ms": round(sum(lats) / len(lats), 1) if lats else None,
        "covered": covered,
        "last_checked": max(checked) if checked else None,
        "fuyao_stats": fuyao.stats(),
    }
