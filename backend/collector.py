"""采集器：封装 AkShare 接口，带限流与重试。

接口清单（akshare 1.20.x）：
- index_zh_spot_em          指数实时行情（注意 1.20.44 改名，旧名 stock_zh_index_spot_em）
- stock_zh_index_daily_em   指数历史日线（回溯用）
- stock_zt_pool_em          涨停池
- stock_zt_pool_zbgc_em     炸板池
- stock_zt_pool_dtgc_em     跌停池
- stock_zt_pool_previous_em 昨日涨停股今日表现
- stock_market_activity_legu 市场活跃度（涨跌家数，乐咕源）
- stock_fund_flow_market_em  大盘资金流向（1.20.45 前叫 stock_market_fund_flow）
"""
import time
import json
import logging
import threading
import urllib.request
from datetime import datetime

import akshare as ak
import pandas as pd

from backend import config

log = logging.getLogger(__name__)

_last_call = 0.0
_rate_lock = threading.Lock()   # 采集会在后台线程 / 调度线程 / 请求线程池里并发发生


def _rate_limit():
    """相邻请求最小间隔，规避东财 push2 限流。

    检查、sleep、更新时间戳必须在同一临界区：否则多个线程同时通过 gap 检查后
    一起发请求，实际间隔远小于配置值，正是触发东财 IP 长时封禁的路径。
    """
    global _last_call
    with _rate_lock:
        gap = time.time() - _last_call
        if gap < config.REQUEST_MIN_INTERVAL:
            time.sleep(config.REQUEST_MIN_INTERVAL - gap)
        _last_call = time.time()


def safe_call(func, *args, **kwargs):
    """带退避重试的接口调用。"""
    last_err = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        _rate_limit()
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_err = e
            msg = str(e)
            if "RateLimit" in type(e).__name__ or "限流" in msg or "断开" in msg:
                wait = config.RETRY_BACKOFF * (2 ** (attempt - 1))
                log.warning("接口限流，第%d次重试，等待%.0fs: %s", attempt, wait, func.__name__)
                time.sleep(wait)
            else:
                log.warning("接口调用失败 %s 第%d次: %s", func.__name__, attempt, msg[:100])
                time.sleep(config.RETRY_BACKOFF * (attempt - 1))
    log.error("接口最终失败: %s -> %s", func.__name__, last_err)
    return None


def _ymd(date_str):
    return date_str.replace("-", "")


# ---------- 指数 ----------
# key -> 新浪指数代码
INDEX_CODES = {
    "sh": "sh000001", "sz": "sz399001", "cy": "sz399006",
    "hs": "sh000300", "z5": "sh000905", "z1": "sh000852", "kc": "sh000688",
}

# 缓存最近一次指数快照（新浪源），供 fetch_indices_spot 与 fetch_amount 共用，
# 避免同一次采集重复请求。
_index_cache = {"ts": 0.0, "df": None}


def _fetch_index_sina():
    import time
    now = time.time()
    # 60 秒内复用缓存
    if _index_cache["df"] is not None and now - _index_cache["ts"] < 60:
        return _index_cache["df"]
    df = safe_call(ak.index_zh_spot_sina)
    if df is not None and not df.empty:
        _index_cache.update(ts=now, df=df)
    return df


def fetch_indices_spot():
    """指数实时快照 {key:(点位, 涨跌幅%)}。新浪主源，腾讯 qt.gtimg.cn 热备。

    新浪源偶发字段缺失/超时（上午盘中曾出现涨跌幅列解析失败，
    导致 null 覆盖库里已有值），腾讯源一次请求拿全 7 大指数，
    实测 ~260ms，字段稳定，作为热备自动切换。
    """
    df = _fetch_index_sina()
    out = _parse_indices_sina(df)
    if out:
        return out
    return fetch_indices_tencent()


def _parse_indices_sina(df):
    out = {}
    if df is None or df.empty:
        return out
    for key, code in INDEX_CODES.items():
        row = df[df["代码"] == code]
        if row.empty:
            continue
        r = row.iloc[0]
        try:
            out[key] = (round(float(r["最新价"]), 2), round(float(r["涨跌幅"]), 2))
        except Exception:
            pass
    return out


def fetch_indices_tencent():
    """腾讯行情热备：qt.gtimg.cn 精简格式，s_ 前缀一次拿全 7 大指数。

    字段：名称~代码~最新价~涨跌额~涨跌幅~成交量(手)~成交额(万元)。
    """
    import urllib.request
    codes = {"sh": "s_sh000001", "sz": "s_sz399001", "cy": "s_sz399006",
             "hs": "s_sh000300", "z5": "s_sh000905", "z1": "s_sh000852",
             "kc": "s_sh000688"}
    url = "https://qt.gtimg.cn/q=" + ",".join(codes.values())
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = safe_call(lambda: urllib.request.urlopen(req, timeout=10).read().decode("gbk", "ignore"))
    out = {}
    if not data:
        return out
    for line in data.strip().split(";"):
        if "~" not in line or "=" not in line:
            continue
        var = line.split("=", 1)[0].strip()
        code = var.replace("v_s_", "")  # s_sh000001 → sh000001
        key = next((k for k, c in codes.items() if c == "s_" + code), None)
        if not key:
            continue
        f = line.split("=")[1].strip('"').split("~")
        try:
            # 精简字段：名称~代码~最新价~涨跌额~涨跌幅~成交量(手)~成交额(万元)
            price, pct = float(f[3]), float(f[5])
            out[key] = (round(price, 2), round(pct, 2))
        except Exception:
            continue
    return out


def fetch_amount_tencent():
    """腾讯热备：两市成交额（万亿）= 上证 + 深证成指成交额（万元 -> 万亿）。"""
    import urllib.request
    url = "https://qt.gtimg.cn/q=s_sh000001,s_sz399001"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = safe_call(lambda: urllib.request.urlopen(req, timeout=10).read().decode("gbk", "ignore"))
    if not data:
        return None
    total = 0.0
    for line in data.strip().split(";"):
        if "~" not in line:
            continue
        f = line.split("=")[1].strip('"').split("~")
        try:
            # f[7] 是成交额(万元)；f[6] 是成交量(手)，此前误当成成交额导致约 10 倍偏差
            total += float(f[7]) * 1e4  # 万元 -> 元
        except Exception:
            pass
    return round(total / 1e12, 2) if total else None


def fetch_index_history(symbol, start=None, end=None):
    """指数历史日线（新浪源，规避东财限流）。

    symbol: 新浪代码，如 sh000001 / sz399001。
    返回 DataFrame，列含 date/open/high/low/close/volume。
    """
    df = safe_call(ak.index_zh_daily_sina, symbol=symbol)
    if df is None or df.empty:
        return df
    df = df.copy()
    df["date"] = df["date"].astype(str).str[:10]
    if start:
        df = df[df["date"] >= start]
    if end:
        df = df[df["date"] <= end]
    return df


def index_history_to_spot(date_str, prev_date_str):
    """取某日 7 大指数的 (点位, 涨跌幅%)，涨跌幅由当日/前一日收盘价计算。

    返回 {key: (点位, 涨跌幅%)}，与 fetch_indices_spot 结构一致。
    """
    out = {}
    for key, code in INDEX_CODES.items():
        try:
            df = fetch_index_history(code, start=prev_date_str, end=date_str)
            if df is None or df.empty:
                continue
            df = df.sort_values("date")
            cur = df[df["date"] == date_str]
            if cur.empty:
                continue
            close = float(cur.iloc[0]["close"])
            pct = None
            hist = df[df["date"] < date_str]
            if not hist.empty:
                prev_close = float(hist.iloc[-1]["close"])
                if prev_close:
                    pct = round((close - prev_close) / prev_close * 100, 2)
            out[key] = (round(close, 2), pct)
        except Exception as e:
            log.debug("指数历史 %s %s 失败: %s", key, date_str, e)
            continue
    return out


# ---------- 涨停池 ----------
def fetch_zt_pool(date_str):
    return safe_call(ak.stock_zt_pool_em, date=_ymd(date_str))


def fetch_zb_pool(date_str):
    return safe_call(ak.stock_zt_pool_zbgc_em, date=_ymd(date_str))


def fetch_dt_pool(date_str):
    return safe_call(ak.stock_zt_pool_dtgc_em, date=_ymd(date_str))


def fetch_previous(date_str):
    return safe_call(ak.stock_zt_pool_previous_em, date=_ymd(date_str))


def fetch_activity():
    """市场活跃度（涨跌家数），返回 dict。仅实时，历史无法回溯。"""
    df = safe_call(ak.stock_market_activity_legu)
    if df is None or df.empty:
        return {}
    return {str(r["item"]): r["value"] for _, r in df.iterrows()}


# ---------- 成交额 ----------
def fetch_amount():
    """两市成交额（万亿）= 上证 + 深证成交额。新浪主源，腾讯热备。"""
    df = _fetch_index_sina()
    total = 0.0
    if df is not None and not df.empty:
        for code in ("sh000001", "sz399001"):
            row = df[df["代码"] == code]
            if not row.empty and "成交额" in row.columns:
                try:
                    total += float(row.iloc[0]["成交额"])
                except Exception:
                    pass
    if total:
        return round(total / 1e12, 2)
    return fetch_amount_tencent()


# ---------- 涨跌家数兜底 ----------
def fetch_breadth_em():
    """东财 clist 全市场扫描兜底：fuyao 快照与乐咕都挂时用（~56 页，约 12s）。

    返回 (up, flat, down)，失败返回 None。仅在盘中主源全部失败时调用。
    """
    import urllib.request
    up = down = 0
    total = None
    pn = 1
    while True:
        url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=%d&pz=100&po=1&np=1"
               "&fltt=2&invt=2&fid=f12&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fields=f3" % pn)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
        except Exception as e:
            log.warning("东财广度扫描第%d页失败: %s", pn, e)
            return None
        data = d.get("data") or {}
        if total is None:
            total = data.get("total") or 0
        diff = data.get("diff") or []
        if not diff:
            break
        for x in diff:
            f3 = x.get("f3")
            if not isinstance(f3, (int, float)):
                continue
            if f3 > 0:
                up += 1
            elif f3 < 0:
                down += 1
        if len(diff) < 100 or pn * 100 >= total:
            break
        pn += 1
    flat = max(0, total - up - down)
    return (up, flat, down) if total else None


# ---------- 成交额预估（同花顺同口径：按昨日同时段占比外推） ----------
_amt_curve_cache = {"date": None, "curve": None}


def _minute_no(hhmm):
    """'09:31' -> 交易分钟编号 0..239（跳过午休，13:01 续接 120）。"""
    t = int(hhmm[:2]) * 60 + int(hhmm[3:5])
    am_end, pm_start = 11 * 60 + 30, 13 * 60
    if t <= am_end:
        return max(0, t - (9 * 60 + 30))
    return 120 + max(0, t - pm_start)


def fetch_amt_ratio_curve():
    """昨日每交易分钟累计成交额占比曲线 [0..1]×240。

    东财 trends2 最近 2 日 1 分钟线（f58=成交额元）：上证+深证按分钟求和，
    取昨日（最后一个完整日）的累计占比。曲线当日不变，进程内缓存。
    同花顺/东财客户端的「预估成交额」即此口径，比线性外推准得多——
    开盘半小时量大、尾盘放量，线性会把全天估高 20%+。
    """
    import urllib.request
    today = datetime.now().strftime("%Y-%m-%d")
    if _amt_curve_cache["date"] == today:
        return _amt_curve_cache["curve"]

    daily_total = {}
    for secid in ("1.000001", "0.399001"):
        url = ("https://push2his.eastmoney.com/api/qt/stock/trends2/get?secid=%s"
               "&fields1=f1,f2,f3,f7,f8&fields2=f51,f53,f57&iscr=0&ndays=2" % secid)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
            trends = (d.get("data") or {}).get("trends") or []
        except Exception as e:
            log.warning("成交额占比曲线失败 %s: %s", secid, e)
            return None
        for line in trends:
            parts = line.split(",")
            # fields2=f51,f53,f57 -> [时间, 现价, 分钟成交额(元)]（f57 是**单分钟**
            # 成交额不是累计——开盘首分钟爆量、15:00 集合竞价一根可验证；
            # 两指数同一分钟相加 = 两市该分钟成交额）
            day, hhmm, amt = parts[0][:10], parts[0][11:16], parts[2]
            try:
                daily_total.setdefault(day, {})[_minute_no(hhmm)] = \
                    daily_total.setdefault(day, {}).get(_minute_no(hhmm), 0.0) + float(amt)
            except Exception:
                continue

    days = sorted(daily_total.keys())
    prev_days = [x for x in days if x < today]
    if not prev_days or today not in daily_total:
        return None
    prev = prev_days[-1]
    series = daily_total[prev]
    total = sum(series.values())  # 全天各分钟求和 = 全天总成交额（与库中 amt 吻合）
    if not total:
        return None
    curve = [0.0] * 240
    acc = 0.0
    for i in range(240):
        acc += series.get(i, 0.0)  # 缺分钟视为 0
        curve[i] = min(1.0, acc / total)
    _amt_curve_cache.update(date=today, curve=curve)
    return curve


def fetch_amt_estimate(cur_amt):
    """按昨日同时段占比预估今日全天成交额（万亿）。

    cur_amt: 当前累计成交额（万亿）。返回 (est, method)，失败返回 (None, None)。
    """
    curve = fetch_amt_ratio_curve()
    tmin = _traded_minutes()
    if curve and 10 <= tmin < 240:
        ratio = curve[min(239, tmin - 1)]
        if ratio > 0.02:
            return round(cur_amt / ratio, 2), "curve"
    # 降级：线性外推（开盘不足 10 分钟或曲线不可得）
    if tmin >= 10:
        return round(cur_amt / tmin * 240, 2), "linear"
    return None, None


def _traded_minutes(now=None):
    """A 股已交易分钟数（09:30-11:30 / 13:00-15:00 共 240 分钟）。"""
    now = now or datetime.now()
    t = now.hour * 60 + now.minute
    am_end, pm_start, pm_end = 11 * 60 + 30, 13 * 60, 15 * 60
    if t <= 9 * 60 + 30:
        return 0
    if t <= am_end:
        return t - (9 * 60 + 30)
    if t <= pm_start:
        return 120
    if t <= pm_end:
        return 120 + (t - pm_start)
    return 240


# ---------- 大盘资金流向 ----------
def fetch_fund_flow_realtime():
    """盘中实时主力资金：东财 push2 上证+深证 求和，与收盘日线同口径。

    akshare 的 stock_fund_flow_market_em 走 push2his daykline，盘中不含今日行，
    导致盘前/盘中主力净流入一直是空窗。push2 实时接口 f62(主力净额)/f66(超大单)
    /f72(大单)/f6(成交额) 即时可得，两市求和 = 日线的"沪深两市"口径。
    返回 {main_net, main_pct, super_net, big_net}（亿/百分比），失败返回 None。
    """
    import urllib.request
    url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2"
           "&secids=1.000001,0.399001&fields=f12,f62,f66,f72,f6")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
        diff = (d.get("data") or {}).get("diff") or []
    except Exception as e:
        log.warning("实时资金流失败: %s", e)
        return None
    if len(diff) < 2:
        return None
    sh, sz = diff
    try:
        net = float(sh["f62"]) + float(sz["f62"])
        amt = float(sh["f6"]) + float(sz["f6"])
        sup = float(sh["f66"]) + float(sz["f66"])
        big = float(sh["f72"]) + float(sz["f72"])
    except Exception:
        return None
    if not amt:
        return None
    return {
        "main_net": round(net / 1e8, 2),
        "main_pct": round(net / amt * 100, 2),
        "super_net": round(sup / 1e8, 2),
        "big_net": round(big / 1e8, 2),
    }


_fund_cache = {"ts": 0.0, "map": {}}


def fetch_fund_flow(force=False):
    """全市场主力资金流向，返回 {date: {...}}，金额单位：亿元。

    数据源：东方财富 push2his 日线接口（AkShare stock_fund_flow_market_em）。
    一次请求可取回近期全部交易日，进程内缓存 5 分钟，避免重复触发限流。
    """
    now = time.time()
    if not force and _fund_cache["map"] and now - _fund_cache["ts"] < 300:
        return _fund_cache["map"]

    fn = getattr(ak, "stock_fund_flow_market_em", None) or getattr(ak, "stock_market_fund_flow", None)
    if fn is None:
        log.error("AkShare 无大盘资金流向接口")
        return _fund_cache["map"]

    df = safe_call(fn)
    if df is None or df.empty:
        return _fund_cache["map"]

    out = {}
    for _, row in df.iterrows():
        d = str(row.get("日期", ""))[:10]
        if not d or d == "nan":
            continue

        def f(col):
            try:
                v = row[col]
                return None if pd.isna(v) else float(v)
            except Exception:
                return None

        net, pct = f("主力净流入-净额"), f("主力净流入-净占比")
        super_net, big_net = f("超大单净流入-净额"), f("大单净流入-净额")
        out[d] = {
            "main_net": round(net / 1e8, 2) if net is not None else None,
            "main_pct": round(pct, 2) if pct is not None else None,
            "super_net": round(super_net / 1e8, 2) if super_net is not None else None,
            "big_net": round(big_net / 1e8, 2) if big_net is not None else None,
        }
    _fund_cache.update(ts=now, map=out)
    log.info("大盘资金流向已加载 %d 个交易日", len(out))
    return out


# ---------- 大盘异动：盘口异动事件流 / 指数分时 / 行业映射 ----------

_changes_cache = {"ts": 0.0, "events": []}

# 与大盘异动标注相关的六类异动（拉升/跳水各三类），逐类请求——
# push2ex 的 type 传多值时只有第一个生效，必须一类型一请求
CHG_UP_TYPES = ("8201", "8207", "8209")   # 火箭发射 / 快速反弹 / 向上缺口
CHG_DN_TYPES = ("8208", "8194", "8210")   # 高台跳水 / 加速下跌 / 向下缺口


def fetch_stock_changes(ttl=60.0):
    """东财 push2ex 全市场盘口异动事件流，返回 [{t:'HH:MM:SS',code,name,type:int}]。

    实测返回当日全天事件（09:30 起），盘中重复拉取取全量即可、无需自己累加。
    60 秒进程内缓存；全部类型都失败时返回 []。
    """
    now = time.time()
    if _changes_cache["events"] and now - _changes_cache["ts"] < ttl:
        return _changes_cache["events"]
    evts = []
    for t in CHG_UP_TYPES + CHG_DN_TYPES:
        url = ("https://push2ex.eastmoney.com/getAllStockChanges?type=%s"
               "&ut=7eea3edcaed734bea9cbfc24409ed989&pageindex=0&pagesize=2000"
               "&dpt=wzchanges&_=%d" % (t, int(now * 1000)))
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/changes/"})
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=8).read().decode("utf-8"))
            rows = (d.get("data") or {}).get("allstock") or []
        except Exception as e:
            log.warning("盘口异动事件 type=%s 拉取失败: %s", t, e)
            continue
        for x in rows:
            tm = str(x.get("tm") or "").zfill(6)
            evts.append({"t": "%s:%s:%s" % (tm[:2], tm[2:4], tm[4:6]),
                         "code": x.get("c"), "name": x.get("n"), "type": x.get("t")})
    if evts:
        evts.sort(key=lambda e: e["t"])
        _changes_cache.update(ts=now, events=evts)
    return evts


_trends_cache = {"ts": 0.0, "data": None}


def fetch_index_trends(ttl=60.0):
    """上证指数当日 1 分钟分时 [(HH:MM, price)]，60 秒进程内缓存。

    复用 trends2（fields2=f51,f53,f57 里 f53 是现价），与成交额预估同一接口。
    """
    now = time.time()
    if _trends_cache["data"] and now - _trends_cache["ts"] < ttl:
        return _trends_cache["data"]
    url = ("https://push2his.eastmoney.com/api/qt/stock/trends2/get?secid=1.000001"
           "&fields1=f1,f2,f3,f7,f8&fields2=f51,f53&iscr=0&ndays=1")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
        trends = (d.get("data") or {}).get("trends") or []
    except Exception as e:
        log.warning("上证分时拉取失败: %s", e)
        return _trends_cache["data"] or []
    out = []
    for line in trends:
        parts = line.split(",")
        # f51 时间 '2026-11-20 09:30'、f53 现价
        out.append((parts[0][11:16], float(parts[1])))
    _trends_cache.update(ts=now, data=out)
    return out


_industry_cache = {"date": None, "map": None}


def fetch_industry_map():
    """全市场 代码→东财行业板块名（clist f100），每日缓存一次。

    用于把异动个股归到板块：优先用当日涨停族题材库，没有的股票落到这里。
    clist 单页最多 100 条，必须按 pn 翻页拉完全市场（~5400 只，55 页）。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    if _industry_cache["date"] == today and _industry_cache["map"]:
        return _industry_cache["map"]
    m = {}
    pn = 1
    # 用 requests：东财 push2 对 python-urllib 的 TLS 指纹会直接断连（2026-11-20 实测），
    # 同机同头 requests 正常。fetch_breadth_em 若同样遇拒也可照此切换。
    import requests
    ses = requests.Session()
    ses.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
    while pn <= 80:
        url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=%d&pz=100&po=0&np=1"
               "&fltt=2&invt=2&fid=f12&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
               "&fields=f12,f100" % pn)
        try:
            d = ses.get(url, timeout=12).json()
            data = d.get("data") or {}
            diff = data.get("diff") or []
        except Exception as e:
            log.warning("行业映射第 %d 页拉取失败: %s", pn, e)
            break
        if not diff:
            break
        for x in diff:
            code = str(x.get("f12") or "")
            ind = str(x.get("f100") or "").strip()
            if code and ind:
                m[code] = ind
        total = data.get("total") or 0
        if len(m) >= total or pn * 100 >= total:
            break
        pn += 1
        time.sleep(0.08)
    if len(m) > 500:  # 明显成批才算成功，避免半途失败污染当日缓存
        _industry_cache.update(date=today, map=m)
        log.info("行业映射已加载 %d 只", len(m))
    return m


def fetch_index_trends_history(ndays=5):
    """上证指数近 N 日逐日 1 分钟线 {'YYYY-MM-DD': [(HH:MM, price), ...]}。

    trends2 ndays>1 时 f51 带日期前缀（'YYYY-MM-DD HH:MM'），按日期分组。
    实测最多支持 ndays=5（再大返回 data:null），更早的历史拿不到——
    落库从上线之日起逐日积累。
    """
    url = ("https://push2his.eastmoney.com/api/qt/stock/trends2/get?secid=1.000001"
           "&fields1=f1,f2,f3,f7,f8&fields2=f51,f53&iscr=0&ndays=%d" % max(1, ndays))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=12).read().decode("utf-8"))
        trends = (d.get("data") or {}).get("trends") or []
    except Exception as e:
        log.warning("上证多日分时拉取失败: %s", e)
        return {}
    out = {}
    for line in trends:
        parts = line.split(",")
        day = parts[0][:10]
        out.setdefault(day, []).append((parts[0][11:16], float(parts[1])))
    return out


def fetch_index_kline_5min(lmt=1400):
    """上证指数 5 分钟 K 线 {'YYYY-MM-DD': [(HH:MM, close), ...]}。

    东财 1 分钟分时（trends2）只保留最近 5 个交易日，更早的历史拿不到；
    5 分钟 K 线实测可回溯约 28 个交易日（1300 条），用于历史日期的
    异动分时图回填（粒度 48 点/日，曲线略粗但走势完整）。
    注意：该端点对 python-urllib 的 TLS 指纹直接断连（Remote end closed），
    必须走 requests（实测 requests 同机同头 200）。
    """
    import requests
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.000001"
           "&klt=5&fqt=1&lmt=%d&end=20500101&fields1=f1,f2,f3,f7&fields2=f51,f53" % max(50, lmt))
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        d = r.json()
        klines = (d.get("data") or {}).get("klines") or []
    except Exception as e:
        log.warning("上证 5 分钟K线拉取失败: %s", e)
        return {}
    out = {}
    for line in klines:
        parts = line.split(",")
        day = parts[0][:10]
        tm = parts[0][11:16]
        try:
            out.setdefault(day, []).append((tm, float(parts[1])))
        except (ValueError, IndexError):
            continue
    return out


def fetch_pool_events_history(date_str):
    """历史日期的异动事件合成：akshare 涨停/跌停/炸板池的首次封板时间。

    东财 push2ex 盘口异动事件流只保留当日、无法回溯；但涨停三池接口
    支持按日期查询且带「首次封板时间 + 所属行业」。用封板事件近似当日
    盘口异动：涨停→拉升侧(9001)，跌停→跳水侧(9002)，炸板→跳水侧(9003)。
    实测池子可回溯约两周，更早返回空（该日就只有分时图无标签）。
    """
    ymd = _ymd(date_str)
    evts = []
    specs = (
        (fetch_zt_pool, 9001),   # 涨停（拉升侧）
        (fetch_dt_pool, 9002),   # 跌停（跳水侧）
        (fetch_zb_pool, 9003),   # 炸板（跳水侧）
    )
    for fn, etype in specs:
        df = safe_call(fn, date_str=date_str)
        if df is None or df.empty:
            continue
        for _, r in df.iterrows():
            raw = str(r.get("首次封板时间") or "").strip()
            if not raw or raw in ("nan", "None"):
                continue
            raw = raw.zfill(6)
            tm = "%s:%s:%s" % (raw[:2], raw[2:4], raw[4:6])
            evts.append({
                "t": tm,
                "code": str(r.get("代码") or "").zfill(6),
                "name": str(r.get("名称") or ""),
                "type": etype,
                "ind": str(r.get("所属行业") or "").strip(),
            })
    evts.sort(key=lambda e: e["t"])
    return evts
