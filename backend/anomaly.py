"""大盘异动标注：把盘口异动事件流聚类到上证分时的跳变点上。

复刻同花顺 App「大盘异动」的效果——分时图上标注拉升/跳动的题材标签。
数据源全部零鉴权：
- 事件流：东财 push2ex getAllStockChanges（火箭发射/高台跳水等 22 类，带时间戳）
- 分时：东财 trends2 上证 1 分钟现价（与成交额预估同一接口）
- 题材归属：当日涨停族题材库（concept_heat members 反查 + 连板股行业）优先，
  回落到东财行业板块 f100（全市场每日一缓存）

历史积累：交易日 15:30 全量采集时把事件流+分钟线落库（anomaly_event/index_minute），
之后任意有数据的日期都可重放标注；盘中则为实时计算。
"""
import logging
import time
from collections import Counter, defaultdict
from datetime import datetime

from backend import collector, storage

log = logging.getLogger(__name__)

UP_TYPES = {8201: "火箭发射", 8207: "快速反弹", 8209: "向上缺口",
            9001: "涨停"}
DN_TYPES = {8208: "高台跳水", 8194: "加速下跌", 8210: "向下缺口",
            9002: "跌停", 9003: "炸板"}
# 90xx 为历史回填合成事件：push2ex 事件流无法回溯，用 akshare 涨停/跌停/炸板池
# 的「首次封板时间」近似当日盘口异动（见 collector.fetch_pool_events_history）

# 跳变检测参数：5 分钟窗口涨跌幅 >= 0.10% 视为一次异动候选，
# 相邻标注点至少隔 15 分钟（与同花顺截图的密度接近），单日最多 14 个
_JUMP_WIN = 5
_JUMP_TH = 0.10
_JUMP_GAP_MIN = 15
_MAX_LABELS = 14
# 事件归属窗口：跳变时刻往前 4 分钟 ~ 当下（事件先于/伴随跳变发生）
_EVENT_WIN_BEFORE = 4
_EVENT_WIN_AFTER = 0
_MIN_STOCKS = 2   # 一个题材至少 2 只股异动才算板块级

_cache = {"key": None, "ts": 0.0, "data": None}


def _tmin(hhmm):
    """'HH:MM' -> 分钟序号（相对 09:30）。"""
    try:
        h, m = hhmm.split(":")
        v = int(h) * 60 + int(m)
    except Exception:
        return -1
    # 13:00 起顺延（上午 121~130 分钟不存在，直接按钟面时间算即可，单调性够用）
    return v


def _themes_for(date_str):
    """个股名 -> 首选题材。涨停族题材库优先，回落东财行业。"""
    tmap = storage.theme_name_map(date_str)
    inds = collector.fetch_industry_map()
    def pick(name, code):
        lst = tmap.get(name)
        if lst:
            return lst[0]
        return inds.get(code or "", "") or "其他"
    return pick


def _infer_step(times):
    """推断分钟点间隔（1 或 5）：历史回填用 5 分钟 K 线、当日用 1 分钟分时。

    取相邻点间隔的中位数（剔除午休 11:30→13:00 的 90 分钟跳变）。
    """
    gaps = []
    for a, b in zip(times, times[1:]):
        d = _tmin(b) - _tmin(a)
        if 0 < d <= 20:
            gaps.append(d)
    if not gaps:
        return 1
    gaps.sort()
    return max(1, gaps[len(gaps) // 2])


def detect_jumps(times, prices, step=1):
    """上证分钟线跳变点检测：返回按时间升序的索引列表。

    step 为分钟粒度：斜率窗口固定按「≈5 分钟」折算成根数
    （1 分钟线 5 根、5 分钟 K 线 1 根），阈值口径两档一致。
    """
    win = max(1, int(round(5.0 / step)))
    n = len(prices)
    slope = {}
    for i in range(win, n):
        base = prices[i - win]
        if not base:
            continue
        slope[i] = (prices[i] - prices[i - win]) / base * 100.0
    cand = [i for i, s in slope.items() if abs(s) >= _JUMP_TH]
    picked = []
    for i in sorted(cand, key=lambda x: -abs(slope[x])):
        if all(abs(_tmin(times[j]) - _tmin(times[i])) >= _JUMP_GAP_MIN for j in picked):
            picked.append(i)
        if len(picked) >= _MAX_LABELS * 2:
            break
    picked.sort()
    # 同方向连续候选只留斜率最大的一个
    out = []
    for i in picked:
        s = slope[i]
        up = s > 0
        if out and (slope[out[-1]] > 0) == up and abs(_tmin(times[i]) - _tmin(times[out[-1]])) < _JUMP_GAP_MIN:
            if abs(s) > abs(slope[out[-1]]):
                out[-1] = i
            continue
        out.append(i)
    return out[:_MAX_LABELS]


def build(date_str, events=None, minute_points=None):
    """聚合某日大盘异动标注。events/minute_points 传 None 则实时拉取（仅当日有效）。

    历史重放可只传 minute_points（events=[]）：更早的日期拿不到事件流回溯，
    分时图照常渲染、异动标签为空。
    返回 {date, points:[[HH:MM,price]...], labels:[{t,dir,theme,etype,cnt,stocks}...]} 或 None。
    """
    if events is None:
        events = collector.fetch_stock_changes()
    if minute_points is None:
        minute_points = collector.fetch_index_trends()
    if not minute_points or len(minute_points) < 10:
        return None
    if not events:
        # 无事件（历史回放无事件流）：只渲染分时、无标签
        return {"date": date_str, "points": minute_points, "labels": []}
    times = [t for t, _ in minute_points]
    prices = [p for _, p in minute_points]
    step = _infer_step(times)
    pick_theme = _themes_for(date_str)

    labels = []
    for i in detect_jumps(times, prices, step):
        t = times[i]
        up = (prices[i] - prices[max(0, i - max(1, int(round(5.0 / step))))]) > 0
        typs = UP_TYPES if up else DN_TYPES
        m = _tmin(t)
        # 事件归属窗随粒度缩放：跳变点所在 bar 的封板事件 + 之前约两个 bar
        # （1 分钟线 6 分钟、5 分钟 K 线 14 分钟，覆盖 09:25 集合竞价封板）
        win_before = _EVENT_WIN_BEFORE + step * 2
        win = [e for e in events
               if e["type"] in typs and -win_before <= _tmin(e["t"][:5]) - m <= _EVENT_WIN_AFTER]
        if not win:
            continue
        cnt = Counter()
        stocks_by_theme = defaultdict(list)
        for e in win:
            # 历史回填事件自带当日所属行业(ind)，优先用；实时事件流走题材库/行业映射
            th = e.get("ind") or pick_theme(e["name"], e["code"])
            cnt[th] += 1
            stocks_by_theme[th].append(e)
        theme, c = cnt.most_common(1)[0]
        if c < _MIN_STOCKS:
            continue
        stocks = []
        seen = set()
        for e in stocks_by_theme[theme]:
            if e["code"] in seen:
                continue
            seen.add(e["code"])
            stocks.append([e["name"], UP_TYPES.get(e["type"], DN_TYPES.get(e["type"], ""))])
        labels.append({
            "t": t, "dir": "up" if up else "dn", "theme": theme, "cnt": c,
            "etypes": sorted({e["type"] for e in stocks_by_theme[theme]}),
            "stocks": stocks[:12],
        })
    return {"date": date_str, "points": minute_points, "labels": labels}


def get_anomaly(date_str=""):
    """对外接口：date 为空取最近收盘交易日；当日(或最新日)实时计算，历史日落库重放。

    返回 {ok, ...} / {ok: False, error}。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    if not date_str:
        d = storage.latest_full_date()
        date_str = d or today
    is_today = date_str == today
    key = date_str
    ttl = 60.0 if is_today else 86400.0
    if _cache["key"] == key and _cache["data"] and time.time() - _cache["ts"] < ttl:
        return _cache["data"]

    data = None
    if is_today:
        try:
            data = build(date_str)
        except Exception as e:
            log.exception("当日大盘异动计算失败: %s", e)
            data = None
    if data is None:
        # 历史日（或当日实时源失败）→ 落库数据重放；仅有分钟线也可（无标签）
        events, points = storage.load_anomaly_day(date_str)
        if points:
            try:
                data = build(date_str, events=events or [], minute_points=points)
            except Exception as e:
                log.exception("落库异动重放失败: %s", e)
    if not data or not data.get("points"):
        return {"ok": False, "date": date_str, "error": "该日期无分时数据（东财分钟线仅保留近几个交易日，更早日期随每日落库逐步积累）"}
    out = {"ok": True, **data}
    _cache.update(key=key, ts=time.time(), data=out)
    return out


def daily_save(date_str):
    """收盘后落库当日事件流与分钟线，供历史重放。失败只记日志不抛。"""
    try:
        events = collector.fetch_stock_changes()
        points = collector.fetch_index_trends()
        if events and points:
            storage.save_anomaly_day(date_str, events, points)
            log.info("大盘异动落库 %s: 事件 %d 条, 分钟线 %d 点", date_str, len(events), len(points))
            return True
        log.warning("大盘异动落库 %s: 事件/分时为空，跳过", date_str)
    except Exception as e:
        log.exception("大盘异动落库失败: %s", e)
    return False
