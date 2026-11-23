"""同花顺金融数据（fuyao）客户端。

能力边界（实测 2026-11-19，AkShare 1.20.65 同期对照）：
  可用 —— 涨停池 / 跌停池 / 炸板池（可回溯 1 年以上，东财仅约 1 个月）
          涨停市场统计（封板率、开板家数、连板档位）
          连板天梯（近 30 个交易日，一次返回全量）
          全市场行情快照（5584 只，6 次分页约 1.8 秒 → 真实涨跌家数）
          热榜 / 飙升榜 / 异动原因 / 龙虎榜
  不可用 —— 主力资金（code=2004，同花顺AI客户端专用）
            高频分时（同页注明未开放外部接入）

因此本项目里 fuyao 承担「涨停族 + 市场广度」，主力资金仍走东财、分时仍走新浪。
所有请求走 X-api-key，进程内限速 + 重试，失败抛 FuyaoError 由上层降级到 AkShare。
"""
import json
import os
import time
import threading
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from backend import config

log = logging.getLogger(__name__)
CST = timezone(timedelta(hours=8))

# 安全：X-api-key 走 header，若 FUYAO_BASE 被污染成攻击者域名，密钥会被外带。
# 白名单默认为示例服务；自建服务可用 FUYAO_ALLOWED_HOST 覆盖（逗号分隔）。
_FUYAO_ALLOWED_HOSTS = tuple(
    h.strip() for h in os.getenv("FUYAO_ALLOWED_HOST", "fuyao.aicubes.cn").split(",") if h.strip()
)

def _valid_base(url):
    try:
        p = urlparse(url or "")
        return (p.scheme == "https"
                and p.hostname in _FUYAO_ALLOWED_HOSTS
                and p.port in (None, 443)
                and not p.username and not p.password)
    except Exception:
        return False

# 接口耗时/成功率的进程内统计，供后台「数据源状态」页展示
STATS = {
    "calls": 0, "ok": 0, "fail": 0,
    "latency_ms": [], "last_ok_at": None, "last_error": None, "last_error_at": None,
}
_lock = threading.Lock()
_last_call = 0.0


class FuyaoError(Exception):
    """业务错误码或网络错误。code 为接口返回的 code，网络错误为 -1。"""

    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


# 需要降级处理、不值得重试的业务码
_FATAL = {1001, 1002, 1003, 1004, 2001, 2003, 2004, 3001, 3002, 3004}


def _throttle():
    global _last_call
    with _lock:
        gap = time.time() - _last_call
        wait = config.FUYAO_MIN_INTERVAL - gap
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()


def _record(ok, latency, err=None):
    with _lock:
        STATS["calls"] += 1
        STATS["latency_ms"].append(latency)
        if len(STATS["latency_ms"]) > 200:
            STATS["latency_ms"] = STATS["latency_ms"][-200:]
        if ok:
            STATS["ok"] += 1
            STATS["last_ok_at"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
        else:
            STATS["fail"] += 1
            STATS["last_error"] = str(err)[:200]
            STATS["last_error_at"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def stats():
    """当前统计快照（后台展示用）。"""
    with _lock:
        lat = list(STATS["latency_ms"])
        return {
            "calls": STATS["calls"], "ok": STATS["ok"], "fail": STATS["fail"],
            "avg_latency_ms": round(sum(lat) / len(lat), 1) if lat else None,
            "last_ok_at": STATS["last_ok_at"],
            "last_error": STATS["last_error"],
            "last_error_at": STATS["last_error_at"],
        }


def _get(path, retries=2, **params):
    """GET /api/**，返回 data 字段。失败抛 FuyaoError。"""
    if not config.FUYAO_ENABLED or not config.FUYAO_KEY:
        raise FuyaoError(-2, "fuyao 未启用或未配置 FUYAO_KEY")
    if not _valid_base(config.FUYAO_BASE):
        raise FuyaoError(-3, "FUYAO_BASE 非受信域名，已拒绝请求（防止密钥外带）")
    url = config.FUYAO_BASE.rstrip("/") + path
    if params:
        url += "?" + urlencode(params)

    last = None
    for attempt in range(retries + 1):
        _throttle()
        t0 = time.time()
        try:
            req = Request(url, headers={"X-api-key": config.FUYAO_KEY,
                                        "Accept": "application/json"})
            with urlopen(req, timeout=config.FUYAO_TIMEOUT) as r:
                body = json.loads(r.read().decode("utf-8"))
            cost = int((time.time() - t0) * 1000)
        except Exception as e:
            cost = int((time.time() - t0) * 1000)
            last = FuyaoError(-1, f"{type(e).__name__}: {e}")
            _record(False, cost, last)
            # 429/网络问题退避重试
            if attempt < retries:
                time.sleep(0.6 * (attempt + 1))
                continue
            raise last

        code = body.get("code")
        if code == 0:
            _record(True, cost)
            return body.get("data") or {}

        msg = str(body.get("message") or "")
        last = FuyaoError(code, msg)
        _record(False, cost, last)
        if code in _FATAL or code == 2004:
            raise last
        if code == 4001 and attempt < retries:  # 频率超限
            time.sleep(1.2 * (attempt + 1))
            continue
        if attempt < retries:
            time.sleep(0.6 * (attempt + 1))
            continue
        raise last
    raise last


# ---------- 时间转换 ----------
def date_ms(date_str):
    """'YYYY-MM-DD' -> 上海时区当日零点的毫秒时间戳。"""
    d = datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=CST)
    return int(d.timestamp() * 1000)


def ms_date(ms):
    return datetime.fromtimestamp(ms / 1000, CST).strftime("%Y-%m-%d")


# ---------- 涨跌停族 ----------
def _pool(path, date_str, size=200, drop_st=False):
    """通用池子拉取：自动翻页，返回 item 列表。

    drop_st=True 时剔除 ST/*ST：同花顺「涨停市场统计」口径不含 ST，
    剔除后涨停家数与东财/财联社历史口径一致（2026-11-19 实测 26→23）。
    """
    out, page = [], 1
    while True:
        data = _get(path, date_ms=date_ms(date_str), page=page, size=size)
        items = data.get("item") or []
        out.extend(items)
        pg = data.get("pagination") or {}
        if not items or page >= int(pg.get("pages") or 1):
            break
        page += 1
        if page > 10:  # 安全阀
            break
    if drop_st:
        out = [x for x in out if not x.get("is_st")]
    return out


def limit_up_pool(date_str, drop_st=True):
    return _pool("/api/a-share/special-data/limit-up-pool", date_str, drop_st=drop_st)


def limit_down_pool(date_str):
    return _pool("/api/a-share/special-data/limit-down-pool", date_str)


def limit_break_pool(date_str):
    return _pool("/api/a-share/special-data/limit-break-pool", date_str)


def limit_up_statistics(date_str):
    """封板率 / 开板家数 / 连板档位统计。"""
    return _get("/api/a-share/special-data/limit-up-statistics", date_ms=date_ms(date_str))


def ladder():
    """近 30 个交易日连板天梯，无入参。"""
    return _get("/api/a-share/special-data/limit-up-ladder")


# ---------- 行情 ----------
def snapshot(codes):
    """批量快照。codes: ['600519.SH', ...]。返回 {ticker: {...}}。"""
    out = {}
    codes = [c for c in codes if c]
    for i in range(0, len(codes), 100):
        chunk = codes[i:i + 100]
        data = _get("/api/a-share/prices/snapshot", thscodes=",".join(chunk))
        for it in data.get("item") or []:
            out[it.get("ticker")] = it
    return out


def market_breadth():
    """全市场涨跌家数（仅当前交易日，快照无日期参数）。

    5584 只分 6 页，约 1.8 秒。返回 {up, flat, down, total, limit_up, ts}。
    """
    up = flat = down = limit_up = 0
    total, offset, pages = 0, 0, 0
    while True:
        data = _get("/api/a-share/prices/snapshot", limit=1000, offset=offset)
        items = data.get("item") or []
        total = int(data.get("total") or total)
        if not items:
            break
        for it in items:
            pct = it.get("price_change_ratio_pct")
            pct = 0.0 if pct is None else float(pct)
            if pct > 0:
                up += 1
            elif pct < 0:
                down += 1
            else:
                flat += 1
            if pct >= 9.8:
                limit_up += 1
        offset += 1000
        pages += 1
        if offset >= total or pages > 12:
            break
    return {"up": up, "flat": flat, "down": down, "total": total,
            "limit_up": limit_up, "ts": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")}


# ---------- 其他 ----------
def hot_stocks():
    return (_get("/api/a-share/special-data/hot-stock-list").get("item") or [])


def skyrocket():
    return (_get("/api/a-share/special-data/skyrocket-list").get("item") or [])


def ping():
    """轻量探活：一次批量快照，判断 key 与网络是否正常。"""
    t0 = time.time()
    data = _get("/api/a-share/prices/snapshot", thscodes="600519.SH")
    items = data.get("item") or []
    return {
        "ok": bool(items),
        "latency_ms": int((time.time() - t0) * 1000),
        "sample": items[0].get("last_price") if items else None,
    }


# ---------- 归一化：把 fuyao 数据转成看板需要的结构 ----------
def _split_reason(reason):
    """把「存储芯片+拟收购嘉合劲威+智慧城市」拆成题材列表。"""
    if not reason:
        return []
    raw = str(reason).replace("＋", "+").replace("、", "+").replace("，", "+").replace(",", "+")
    parts = [p.strip() for p in raw.split("+")]
    return [p for p in parts if p and len(p) <= 18]


def build_day(date_str, prev_date=None, with_ladder=None):
    """拉取一个交易日的全部 fuyao 指标，返回看板可直接使用的结构。

    返回 dict：
      zt_cnt/dt_cnt/zb_cnt/zb_rate/seal_rate/open_cnt/touched_cnt
      lianbanList/lb_max/lianban_cnt
      heat/concepts（按涨停原因题材聚合）
      promo（晋级梯队）/ yp（昨涨停今表现）
      zt_codes/zb_codes
    """
    zt = limit_up_pool(date_str)
    zb = limit_break_pool(date_str)
    dt = limit_down_pool(date_str)
    try:
        st = limit_up_statistics(date_str) or {}
        # 该接口实测忽略 date_ms，永远返回最新交易日；口径不符时弃用，
        # 否则历史日期会被写成同一天的封板率（15 天全 48.9% 的坑）。
        if str(st.get("trade_date"))[:10] != date_str:
            st = {}
    except FuyaoError as e:
        log.warning("封板统计失败 %s: %s", date_str, e)
        st = {}

    zt_cnt = len(zt)
    zb_cnt = len(zb)
    dt_cnt = len(dt)
    # 封板率优先用官方口径：涨停数 /（涨停数 + 开板家数）
    seal_rate = st.get("seal_rate")
    if seal_rate is not None:
        seal_rate = round(float(seal_rate) * 100, 1)
        zb_rate = round(100 - seal_rate, 1)
    else:
        zb_rate = round(zb_cnt / (zt_cnt + zb_cnt) * 100, 1) if (zt_cnt + zb_cnt) else 0.0
        seal_rate = round(100 - zb_rate, 1)

    zt_codes = [str(x.get("ticker")) for x in zt]
    zb_codes = [str(x.get("ticker")) for x in zb]

    # 连板天梯（连板数 >= 2）
    lianban = []
    for x in zt:
        n = int(x.get("continue_day_cnt") or 1)
        if n >= 2:
            reason = _split_reason(x.get("limit_up_reason"))
            lianban.append([str(x.get("name")), n, reason[0] if reason else ""])
    lianban.sort(key=lambda v: -v[1])
    lb_max = max([v[1] for v in lianban], default=1)

    # 题材热度：按涨停原因拆分聚合，顺带记录个股名（第 3 元素），供前端悬浮展示
    counter = {}
    for x in zt:
        nm = str(x.get("name") or "")
        for tag in set(_split_reason(x.get("limit_up_reason"))):
            c = counter.setdefault(tag, [0, []])
            c[0] += 1
            if nm and nm not in c[1]:
                c[1].append(nm)
    heat = [[k, v[0], v[1]] for k, v in counter.items() if v[0] >= 2]
    heat.sort(key=lambda v: (-v[1], v[0]))

    # 昨涨停今表现 & 晋级梯队
    promo, yp = [], {"avg": 0.0, "red": 0, "promo": 0}
    if prev_date:
        try:
            prev = limit_up_pool(prev_date)
        except FuyaoError as e:
            log.warning("昨日涨停池失败 %s: %s", prev_date, e)
            prev = []
        if prev:
            codes = [str(x.get("thscode")) for x in prev if x.get("thscode")]
            try:
                quotes = snapshot(codes)
            except FuyaoError as e:
                log.warning("昨涨停批量行情失败: %s", e)
                quotes = {}
            pcts, reds = [], 0
            tiers = {}
            for x in prev:
                n = int(x.get("continue_day_cnt") or 1)
                label = f"{n}进{n + 1}"
                ticker = str(x.get("ticker"))
                q = quotes.get(ticker) or {}
                pct = q.get("price_change_ratio_pct")
                pct = float(pct) if pct is not None else 0.0
                pcts.append(pct)
                if pct > 0:
                    reds += 1
                sealed = ticker in set(zt_codes)
                status = 1 if sealed else (2 if ticker in set(zb_codes) else 0)
                t = tiers.setdefault(label, {"win": 0, "total": 0, "stocks": [], "pcts": []})
                t["total"] += 1
                t["win"] += 1 if sealed else 0
                t["pcts"].append(pct)
                reason = _split_reason(x.get("limit_up_reason"))
                t["stocks"].append([str(x.get("name")), status, reason[0] if reason else ""])
            yp = {
                "avg": round(sum(pcts) / len(pcts), 2) if pcts else 0.0,
                "red": reds,
                "promo": sum(1 for x in prev if str(x.get("ticker")) in set(zt_codes)),
            }
            # 首板梯队 = 今日涨停中连板数 1
            first = [x for x in zt if int(x.get("continue_day_cnt") or 1) == 1]
            if first:
                # win 置 None：首板当日不构成晋级，写 0 会被渲染成 "0/N · 0%"，
                # 写 N 会被渲染成 "N/N · 100%"，两种都是错的。
                t = tiers.setdefault("首板", {"win": None, "total": 0, "stocks": []})
                for x in first:
                    reason = _split_reason(x.get("limit_up_reason"))
                    t["total"] += 1
                    t["stocks"].append([str(x.get("name")), 1, reason[0] if reason else ""])

            def order(label):
                if label == "首板":
                    return -1
                try:
                    return int(label.split("进")[0])
                except Exception:
                    return 0

            for label in sorted(tiers, key=order, reverse=True):
                t = tiers[label]
                pcts = t.get("pcts") or []
                win = t["win"]
                promo.append({
                    "label": label, "win": win, "total": t["total"],
                    "rate": None if win is None else f"{win}/{t['total']}",
                    "ypAvg": round(sum(pcts) / len(pcts), 2) if pcts else None,
                    "ypRed": sum(1 for p in pcts if p > 0) if pcts else None,
                    "stocks": t["stocks"],
                })

    return {
        "date": date_str, "source": "fuyao",
        "zt_cnt": zt_cnt, "dt_cnt": dt_cnt, "zb_cnt": zb_cnt,
        "zb_rate": zb_rate, "seal_rate": seal_rate,
        "open_cnt": st.get("open_stock_count"), "touched_cnt": st.get("touched_limit_up_stock_count"),
        "lianbanList": lianban, "lb_max": lb_max, "lianban_cnt": len(lianban),
        "heat": heat, "concepts": heat[:5],
        "promo": promo, "yp": yp,
        "zt_codes": zt_codes, "zb_codes": zb_codes,
    }
