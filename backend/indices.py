"""核心指数：统一注册表 + 实时快照 + 分时（当日 / 近 5 日 / 落库回放）。

为什么单独一层：前端「核心指数」支持用户自选 6 个并排序，可选集合里既有 A 股
指数，也有港股/海外指数，各自的数据源、代码体系、交易时段都不同。把这层差异
收敛到本模块，前端只认 `key`，不再关心底层用新浪还是腾讯。

数据源实测结论（2026-11-21）：
- 实时快照：腾讯 qt.gtimg.cn 一次批量拿 10 个（含港股/美股），韩国综合走新浪
  `znb_KOSPI`（腾讯无该指数）。字段固定为 名称~代码~现价~涨跌额~涨跌幅。
- 当日分时：腾讯 `appstock/app/minute/query`，A 股 242 点、港股 332 点；美股在
  休市时只回 0~1 点，故美股分时按「有则显示、无则占位」处理。
- 近 5 日分时：腾讯 `appstock/app/day/query` 返回 5 个自然交易日的 1 分钟序列
  （A 股与港股均实测可用），用于历史日期回放。
- 更早分时：只能靠每日落库（index_minute_multi 表，从本版上线起逐日积累）。
  不伪造：查不到就返回 ok=False，前端统一占位。
- 韩国综合分时：仅东财 trends2（secid=100.KS11），该域常年 IP 限流，取不到就用占位。
"""
import datetime
import json
import logging
import threading
import time
import urllib.request

from backend import storage

log = logging.getLogger(__name__)

# key 为前端唯一标识，写入 localStorage 后不可随意改名
INDEX_META = [
    # A 股
    {"key": "sh",  "label": "上证指数", "short": "上证",   "market": "cn", "tc": "sh000001", "sina": "sh000001",  "digits": 2},
    {"key": "sz",  "label": "深证成指", "short": "深成",   "market": "cn", "tc": "sz399001", "sina": "sz399001",  "digits": 2},
    {"key": "cy",  "label": "创业板指", "short": "创业板", "market": "cn", "tc": "sz399006", "sina": "sz399006",  "digits": 2},
    {"key": "kc",  "label": "科创50",   "short": "科创50", "market": "cn", "tc": "sh000688", "sina": "sh000688",  "digits": 2},
    {"key": "hs",  "label": "沪深300",  "short": "沪深300","market": "cn", "tc": "sh000300", "sina": "sh000300",  "digits": 2},
    {"key": "z5",  "label": "中证500",  "short": "中证500","market": "cn", "tc": "sh000905", "sina": "sh000905",  "digits": 2},
    {"key": "z1",  "label": "中证1000", "short": "中证1000","market": "cn","tc": "sh000852", "sina": "sh000852",  "digits": 2},
    # 港股
    {"key": "hk",  "label": "恒生指数", "short": "恒生",   "market": "hk", "tc": "hkHSI",    "sina": "int_hangseng", "digits": 2},
    {"key": "hkt", "label": "恒生科技", "short": "恒科",   "market": "hk", "tc": "hkHSTECH", "sina": "rt_hkHSTECH",  "digits": 2},
    # 海外
    {"key": "ks",  "label": "韩国综合", "short": "KOSPI",  "market": "kr", "tc": "",         "sina": "znb_KOSPI",    "digits": 2},
    {"key": "ixic","label": "纳斯达克", "short": "纳指",   "market": "us", "tc": "usIXIC",   "sina": "int_nasdaq",   "digits": 2},
]
META_BY_KEY = {m["key"]: m for m in INDEX_META}
DEFAULT_KEYS = ["sh", "sz", "cy", "kc", "hs", "z1"]

_UA = {"User-Agent": "Mozilla/5.0"}


def meta_list():
    """给前端用的静态元信息（不含行情）。"""
    return [{"key": m["key"], "label": m["label"], "short": m["short"],
             "market": m["market"], "digits": m["digits"]} for m in INDEX_META]


# ---------------- 实时快照 ----------------
_spot_cache = {"ts": 0.0, "data": {}}
_spot_lock = threading.Lock()


def _http(url, timeout=10, coding="utf-8"):
    ref = "https://finance.sina.com.cn/" if "sinajs" in url else "https://gu.qq.com/"
    req = urllib.request.Request(url, headers={**_UA, "Referer": ref})
    return urllib.request.urlopen(req, timeout=timeout).read().decode(coding, "ignore")


def _parse_tencent(text):
    """解析 qt.gtimg.cn 批量返回：名称~代码~现价~涨跌额~涨跌幅~量~额。

    行首变量名才是请求时的代码（`v_s_sh000001=` → `sh000001`）；f[2] 里是纯数字
    代码（A 股）或 HSI/.IXIC（港美股），不能直接拿来匹配。
    """
    out = {}
    for line in text.split(";"):
        if "~" not in line or "=" not in line:
            continue
        head, body = line.split("=", 1)
        code = head.strip().split("v_s_")[-1].split("v_")[-1].strip()
        f = body.strip().strip('"').split("~")
        if len(f) < 6:
            continue
        try:
            v = float(f[3]); pct = float(f[5])
        except (TypeError, ValueError):
            continue
        out[code] = (round(v, 2), round(pct, 2))
    return out


def fetch_spot(ttl=30.0):
    """11 个指数的实时快照 {key: (点位, 涨跌幅%)}。30 秒进程内缓存。"""
    now = time.time()
    with _spot_lock:
        if _spot_cache["data"] and now - _spot_cache["ts"] < ttl:
            return _spot_cache["data"]
    out = {}
    tcs = [m["tc"] for m in INDEX_META if m["tc"]]
    if tcs:
        try:
            txt = _http("https://qt.gtimg.cn/q=" + ",".join("s_" + c for c in tcs), coding="gbk")
            parsed = _parse_tencent(txt)
            for m in INDEX_META:
                if m["tc"] and m["tc"] in parsed:
                    out[m["key"]] = parsed[m["tc"]]
        except Exception as e:
            log.warning("指数实时快照（腾讯）失败: %s", e)
    # 韩国综合：腾讯无覆盖，新浪 znb_ 接口兜底（名称,点位,涨跌额,涨跌幅）
    if "ks" not in out:
        try:
            txt = _http("https://hq.sinajs.cn/list=znb_KOSPI", coding="gbk")
            body = txt.split('"')[1] if '"' in txt else ""
            f = body.split(",")
            if len(f) >= 4 and f[1]:
                out["ks"] = (round(float(f[1]), 2), round(float(f[3]), 2))
        except Exception as e:
            log.debug("韩国综合快照失败: %s", e)
    # 补漏：新浪 int_ 接口（恒生/纳指），仅当腾讯缺失时
    for key, sina in (("hk", "int_hangseng"), ("ixic", "int_nasdaq")):
        if key in out:
            continue
        try:
            txt = _http(f"https://hq.sinajs.cn/list={sina}", coding="gbk")
            body = txt.split('"')[1] if '"' in txt else ""
            f = body.split(",")
            if len(f) >= 4 and f[1]:
                out[key] = (round(float(f[1]), 2), round(float(f[3]), 2))
        except Exception as e:
            log.debug("新浪兜底 %s 失败: %s", key, e)
    if out:
        with _spot_lock:
            _spot_cache.update(ts=now, data=out)
    return out or _spot_cache["data"]


# ---------------- 分时 ----------------
def _tencent_minute(tc_code):
    """当日 1 分钟分时：返回 (points[[HH:MM, price]], prevClose, date)。

    注意 date：非交易日（周末/节假日/开盘前）这个接口照样返回「最近一个交易日」
    的整段分时，不校验日期就会把上周五的走势挂到周六头上。
    """
    url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={tc_code}"
    txt = _http(url)
    i, j = txt.find("{"), txt.rfind("}")
    if i < 0:
        return [], None, ""
    d = json.loads(txt[i:j + 1]).get("data") or {}
    node = d.get(tc_code) or {}
    inner = node.get("data") or {}
    day = str(inner.get("date") or "")
    if len(day) == 8:
        day = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    rows = inner.get("data") or []
    prev = None
    qt = node.get("qt") or {}
    arr = qt.get(tc_code) or []
    if isinstance(arr, list) and len(arr) > 4:
        try:
            prev = float(arr[4])
        except (TypeError, ValueError):
            prev = None
    pts = []
    for r in rows:
        f = str(r).split()
        if len(f) < 2:
            continue
        t = f[0]
        if len(t) == 4:
            t = t[:2] + ":" + t[2:]
        try:
            pts.append([t, round(float(f[1]), 2)])
        except ValueError:
            continue
    return pts, prev, day


def _tencent_days(tc_code, want_date):
    """近 5 个交易日的 1 分钟分时：按日期取 want_date 当天。"""
    url = f"https://web.ifzq.gtimg.cn/appstock/app/day/query?code={tc_code}"
    txt = _http(url)
    i, j = txt.find("{"), txt.rfind("}")
    if i < 0:
        return [], None
    d = json.loads(txt[i:j + 1]).get("data") or {}
    node = d.get(tc_code) or {}
    prev = None
    qt = node.get("qt") or {}
    arr = qt.get(tc_code) or []
    if isinstance(arr, list) and len(arr) > 4:
        try:
            prev = float(arr[4])
        except (TypeError, ValueError):
            prev = None
    for day in node.get("data") or []:
        ds = str(day.get("date") or "")
        if len(ds) == 8:
            ds = f"{ds[:4]}-{ds[4:6]}-{ds[6:]}"
        if ds != want_date:
            continue
        pts = []
        for r in day.get("data") or []:
            f = str(r).split()
            if len(f) < 2:
                continue
            t = f[0]
            if len(t) == 4:
                t = t[:2] + ":" + t[2:]
            try:
                pts.append([t, round(float(f[1]), 2)])
            except ValueError:
                continue
        return pts, prev
    return [], prev


def _tencent_m5(tc_code, want_date):
    """腾讯 5 分钟 K 线兜底：取 want_date 当天的 5 分钟收盘价序列。

    ifzq 的 minute/day 两个端点都只回溯 5 个交易日，11-15 之前的日期必然空；
    但 mkline（m5）同一批接口能回溯约 17 个交易日（800 根 bar，48 点/日）。
    注意域名必须是 ifzq.gtimg.cn：web.ifzq.gtimg.cn 走的 CNAME 已失效，
    实测 connect 超时；Referer 用 gu.qq.com 才不被拒。
    返回 (points[[HH:MM, price]], prevClose)，prevClose 一律交给调用方从库里取，
    这里给 None（K 线里的首开不是昨收）。查不到该日返回 ([], None)。
    """
    url = f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={tc_code},m5,,800"
    try:
        txt = _http(url, timeout=12)
        i, j = txt.find("{"), txt.rfind("}")
        if i < 0:
            return [], None
        d = json.loads(txt[i:j + 1]).get("data") or {}
        node = d.get(tc_code) or {}
        pts = []
        want = want_date.replace("-", "")
        for bar in node.get("m5") or []:
            stamp = str(bar[0])
            if len(stamp) < 12 or stamp[:8] != want:
                continue
            tm = stamp[8:10] + ":" + stamp[10:12]
            try:
                pts.append([tm, round(float(bar[2]), 2)])  # bar[2] = 收盘价
            except (ValueError, IndexError):
                continue
        return pts, None
    except Exception as e:
        log.debug("腾讯 m5 分时 %s 失败: %s", tc_code, e)
        return [], None


def _em_minute(secid, ndays=1):
    """东财 trends2（韩国综合唯一的分钟源）。失败返回空。

    注意：该接口永远只返回「最近一段」分时，date 参数并不生效，
    所以必须把首根 bar 的日期带出来交给调用方校验，否则看历史日时
    会把今天的分时当成那天画出去。
    """
    url = ("https://push2his.eastmoney.com/api/qt/stock/trends2/get"
           f"?secid={secid}&fields1=f1,f2,f3,f7,f8&fields2=f51,f53&iscr=0&ndays={ndays}")
    try:
        txt = _http(url, timeout=12)
        d = json.loads(txt)
        trends = (d.get("data") or {}).get("trends") or []
        prev = (d.get("data") or {}).get("preClose") or (d.get("data") or {}).get("prePrice")
        pts, day = [], ""
        for line in trends:
            p = line.split(",")
            if len(p) < 2:
                continue
            if not day:
                day = p[0][:10]
            pts.append([p[0][11:16], round(float(p[1]), 2)])
        return pts, (float(prev) if prev else None), day
    except Exception as e:
        log.debug("东财分时 %s 失败: %s", secid, e)
        return [], None, ""


def _tencent_us_minute(tc_code):
    """腾讯美股分时（usMinute）：只给最近一个美股交易日，整段 09:30-16:00。

    返回 (pts, prevClose, session_date)，session_date 形如 2026-11-20。
    接口里 date 字段就是会话日，必须带出来让调用方判断该挂到哪个 A 股交易日。
    """
    url = f"https://web.ifzq.gtimg.cn/appstock/app/usMinute/query?code={tc_code}"
    txt = _http(url)
    i, j = txt.find("{"), txt.rfind("}")
    if i < 0:
        return [], None, ""
    try:
        node = (json.loads(txt[i:j + 1]).get("data") or {}).get(tc_code) or {}
    except ValueError:
        return [], None, ""
    inner = node.get("data") or {}
    sess = str(inner.get("date") or "")
    if len(sess) == 8:
        sess = f"{sess[:4]}-{sess[4:6]}-{sess[6:]}"
    prev = None
    arr = (node.get("qt") or {}).get(tc_code) or []
    if isinstance(arr, list) and len(arr) > 4:
        try:
            prev = float(arr[4])
        except (TypeError, ValueError):
            prev = None
    pts = []
    for r in inner.get("data") or []:
        f = str(r).split()
        if len(f) < 2:
            continue
        t = f[0]
        if len(t) == 4:
            t = t[:2] + ":" + t[2:]
        try:
            pts.append([t, round(float(f[1]), 2)])
        except ValueError:
            continue
    return pts, prev, sess


def _us_attr_date(session):
    """美股某交易日 → 归属的 A 股交易日。

    美股 16:00 收盘对应北京时间次日凌晨，所以那根收盘线只可能出现在
    「会话日之后的第一个 A 股交易日」上（与日线归档口径保持一致）。
    这里用 +1 跳过周末近似；遇节假日对不上时会自然回落成占位，不会张冠李戴。
    """
    try:
        d = datetime.date(int(session[:4]), int(session[5:7]), int(session[8:10]))
    except (ValueError, TypeError):
        return ""
    for _ in range(5):
        d += datetime.timedelta(days=1)
        if d.weekday() < 5:
            return d.strftime("%Y-%m-%d")
    return ""


_min_cache = {}            # (key, date) -> (存入时间, ttl, payload)
_min_lock = threading.Lock()
_MIN_MAX = 240             # 11 个指数 × 约 20 个交易日，够用且不会无限涨


def get_minute(key, date_str=""):
    """某指数某日的分时（带进程内 TTL 缓存）。

    为什么必须加这层缓存：这个接口一次要打腾讯/东财，实测 0.3~0.6 秒；
    而前端点一下指数卡片就会换一次 code，在 6 个自选之间来回点就是 6 次上游请求。
    腾讯 ifzq/gtimg 对密集请求会触发 WAF 封禁（1 小时起），东财 push2his 会 IP 限流——
    这里每一次都打穿，等于把封禁概率乘以访问量。
      - 当日：TTL 30 秒（盘中本来就每 30 秒更新一次）
      - 历史日：TTL 30 分钟（落库数据不会变，回填属于低频操作）
      - 取不到（非交易日、未收录）：TTL 2~10 分钟，避免周末每次刷新都去问一遍上游
    """
    m = META_BY_KEY.get(key)
    if not m:
        return {"ok": False, "error": "未知指数代码"}
    today = time.strftime("%Y-%m-%d")
    date_str = date_str or today
    ck = (key, date_str)
    now = time.time()
    with _min_lock:
        hit = _min_cache.get(ck)
        if hit and now - hit[0] < hit[1]:
            return hit[2]
    payload = _build_minute(key, date_str, today)
    if payload.get("ok"):
        ttl = 30 if date_str >= today else 1800
    else:
        ttl = 120 if date_str >= today else 600
    with _min_lock:
        if len(_min_cache) >= _MIN_MAX:
            # 先清过期的，还超量就按存入时间清最早的 1/4
            for k in [k for k, v in _min_cache.items() if now - v[0] >= v[1]]:
                _min_cache.pop(k, None)
            if len(_min_cache) >= _MIN_MAX:
                for k, _ in sorted(_min_cache.items(), key=lambda kv: kv[1][0])[: _MIN_MAX // 4]:
                    _min_cache.pop(k, None)
        _min_cache[ck] = (now, ttl, payload)
    return payload


def _build_minute(key, date_str, today):
    """实际取数（不含缓存），取值顺序见下方注释。"""
    m = META_BY_KEY[key]
    pts, prev, src = [], None, ""
    if m["market"] == "kr":
        pts, prev, day = _em_minute("100.KS11", 1)
        if day and day != date_str:     # 东财只给最近一段，日期不符就直接丢弃
            pts, prev = [], None
        src = "eastmoney"
    if m["market"] == "us" and m["tc"]:
        pts, prev, sess = _tencent_us_minute(m["tc"])
        if not sess or _us_attr_date(sess) != date_str:
            pts, prev = [], None      # 会话不属于这个 A 股交易日，宁可占位
        src = "tencent-us"
    if date_str >= today:
        if not pts and m["tc"]:
            pts, prev, live_day = _tencent_minute(m["tc"])
            if live_day and live_day != date_str:
                pts, prev = [], None   # 非交易日会返回上一交易日的整段，不能张冠李戴
            src = "tencent-live"
    if not pts and m["tc"]:
        pts, prev = _tencent_days(m["tc"], date_str)
        src = "tencent-5d"
    if not pts:
        pts = storage.load_index_minute(date_str, key)
        src = "db"
    # 旧表兜底：多指数分时表 2026-11-21 才上线，之前的历史分时在 anomaly 用的
    # index_minute 表里，且只有上证一条线（无 code 列）。腾讯 day 接口只回溯 5 日，
    # 11-15 之前的日期新表也是空的——不读旧表就全成了「未收录」。
    # 只在 key=='sh' 时回退：绝不能把上证曲线挂到深证/创业板名下。
    if not pts and key == "sh":
        pts = storage.load_index_minute_legacy(date_str)
        src = "db"
    # 其余核心指数（深证/创业板/科创/沪深300/中证500/1000）旧表没有，新表只有当日，
    # 腾讯 m5 能回溯约 17 个交易日（10-30 起）——用它补 11-15 之前的历史回放。
    if not pts and m["tc"] and m["market"] == "cn":
        pts, _ = _tencent_m5(m["tc"], date_str)
        src = "tencent-m5"
    if len(pts) < 10:
        return {"ok": False, "key": key, "name": m["label"], "date": date_str,
                "error": "该日分时未收录"}
    # 前收盘优先取库：腾讯 day/query 里的 qt 字段是「今天」的昨收，
    # 拿去画历史日会把当日涨跌幅算成 0。
    base = storage.get_index_prev_close(date_str, key)
    if base is None:
        base = storage.get_index_prev_close_snapshot(date_str, key)
    if base is None:
        base = prev
    return {"ok": True, "key": key, "name": m["label"], "date": date_str,
            "points": pts, "prevClose": base, "source": src}


def save_day(date_str):
    """收盘后落库各指数当日分时，供历史回放。只存有数据的指数。"""
    saved = 0
    for m in INDEX_META:
        try:
            if m["market"] == "kr":
                pts, _, day = _em_minute("100.KS11", 1)
                if day and day != date_str:     # 同上：日期不符不落库
                    pts = []
            elif m["market"] == "us":
                pts, _, sess = _tencent_us_minute(m["tc"])
                if not sess or _us_attr_date(sess) != date_str:
                    pts = []                    # 会话不归属今天就不落库，避免污染历史
            else:
                pts, _, live_day = _tencent_minute(m["tc"])
                if live_day and live_day != date_str:
                    pts = []                # 周末/节假日跑任务时别把上一个交易日写成今天
            if pts and len(pts) >= 10:
                storage.save_index_minute(date_str, m["key"], pts)
                saved += 1
        except Exception as e:
            log.debug("指数分时落库 %s 失败: %s", m["key"], e)
    return saved
