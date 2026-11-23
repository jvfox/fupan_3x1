"""采集主流程：采集 -> 计算指标 -> 入库 -> 导出 JSON -> 记录日志。

对外暴露：
- collect_day(date_str, mode='full', trigger_by='scheduler')  采集单个交易日
- backfill(days)  回溯最近 N 个交易日
- export_json()   从库里导出前端 records.json
"""
import logging
import os
import threading
import time
from datetime import datetime, timedelta

from backend import config, collector, indicators, storage, calendar_util, fuyao

log = logging.getLogger(__name__)

# 涨停族节流状态（进程内）：上次成功采集的时间戳
_zt_poll = {"ts": 0.0}


def _trading_days_back(n):
    """返回最近 n 个真实交易日（倒序，从旧到新的调用方会 reverse）。"""
    return calendar_util.last_n_trade_days(n)


def _week_of(date_str):
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return "周" + "一二三四五六日"[d.weekday()]


def collect_day(date_str, mode="full", trigger_by="scheduler", keep_fields=None):
    """采集单个交易日。mode: full=收盘全量 / intraday=盘中轻量。返回 (ok, msg)。

    keep_fields: 需要沿用库中已有值的字段列表（如历史涨跌家数无法回溯时保留种子值）。
    """
    log_id = storage.log_start(date_str, mode, trigger_by)
    try:
        if mode == "intraday":
            ok, msg = _collect_intraday(date_str)
        else:
            ok, msg = _collect_full(date_str, keep_fields=keep_fields)
        storage.log_finish(log_id, "success" if ok else "failed",
                           rows=1 if ok else 0, error=None if ok else msg)
        return ok, msg
    except Exception as e:
        log.exception("采集异常 %s", date_str)
        storage.log_finish(log_id, "failed", error=str(e))
        return False, str(e)


def _parts_from_fuyao(date_str, prev_date):
    """主源：同花顺。涨停/跌停/炸板池 + 封板统计 + 连板 + 晋级梯队一次算齐。"""
    return fuyao.build_day(date_str, prev_date)


def _parts_from_akshare(date_str, prev_date, keep_fields, existing):
    """降级源：AkShare（东财涨停池 + 昨涨停池 + 炸板池）。"""
    zt = collector.fetch_zt_pool(date_str)
    zb = collector.fetch_zb_pool(date_str)
    dt = collector.fetch_dt_pool(date_str)
    prev = collector.fetch_previous(date_str)
    if zt is None or zt.empty:
        raise RuntimeError("涨停池为空（非交易日或接口未更新）")

    zt_cnt = len(zt)
    zb_cnt = len(zb) if zb is not None else 0
    dt_cnt = len(dt) if dt is not None else 0
    if "zb_cnt" in keep_fields and existing and existing.get("zb_cnt"):
        zb_cnt = int(existing["zb_cnt"])
    if "dt_cnt" in keep_fields and existing and existing.get("dt_cnt"):
        dt_cnt = int(existing["dt_cnt"])
    zb_rate = round(zb_cnt / (zt_cnt + zb_cnt) * 100, 1) if (zt_cnt + zb_cnt) else 0.0

    lb_list = indicators.lianban_list_from_pool(zt)
    concepts = indicators.concept_heat_from_pool(zt)
    return {
        "zt_cnt": zt_cnt, "dt_cnt": dt_cnt, "zb_cnt": zb_cnt,
        "zb_rate": zb_rate, "seal_rate": round(100 - zb_rate, 1),
        "lianbanList": lb_list,
        "lb_max": max([x[1] for x in lb_list], default=1),
        "lianban_cnt": len(lb_list),
        "concepts": [[c, n] for c, n in concepts[:5]],
        "heat": concepts,
        "promo": indicators.promo_from_pools(zt, prev, zb),
        "yp": indicators.yp_from_previous(prev),
        "source": "akshare",
    }


def _collect_full(date_str, keep_fields=None):
    """收盘全量：涨跌停族（同花顺优先 / AkShare 降级）+ 广度 + 指数 + 资金。"""
    keep_fields = keep_fields or []
    latest = (calendar_util.last_n_trade_days(1) or [None])[0]
    is_latest = date_str == latest
    # 历史日期不能写实时指数/成交额/涨跌家数；盘中实时值只属于最新交易日
    existing = storage.get_snapshot(date_str) if (keep_fields or not is_latest) else None
    prev_date = calendar_util.previous_trade_day(date_str)

    parts = None
    if config.FUYAO_ENABLED and config.PRIMARY_SOURCE == "fuyao":
        try:
            parts = _parts_from_fuyao(date_str, prev_date)
        except Exception as e:
            log.warning("fuyao 采集失败，降级 AkShare [%s]: %s", date_str, e)
    if parts is None:
        parts = _parts_from_akshare(date_str, prev_date, keep_fields, existing)

    zt_cnt = parts["zt_cnt"]
    zb_cnt = parts["zb_cnt"]
    dt_cnt = parts["dt_cnt"]
    zb_rate = parts["zb_rate"]
    lb_list = parts["lianbanList"]
    lb_max = parts["lb_max"]
    lianban_cnt = parts["lianban_cnt"]
    concepts = parts["concepts"]
    heat = parts["heat"]
    promo = parts["promo"]
    yp = parts["yp"]

    if is_latest:
        idx = collector.fetch_indices_spot()
        # 广度：同花顺全市场快照（真实统计）优先，乐咕降级
        up = flat = down = None
        if config.FUYAO_ENABLED:
            try:
                b = fuyao.market_breadth()
                if b.get("total"):
                    up, flat, down = b["up"], b["flat"], b["down"]
            except Exception as e:
                log.warning("fuyao 全市场快照失败，降级乐咕: %s", e)
        if up is None:
            act = collector.fetch_activity()
            up = int(act.get("上涨", 0) or 0)
            down = int(act.get("下跌", 0) or 0)
            flat = int(act.get("平盘", 0) or 0)
    else:
        idx = collector.index_history_to_spot(date_str, prev_date)
        # 全市场快照只有实时值，历史涨跌家数沿用库中已有真值
        up = int((existing or {}).get("up_cnt") or 0)
        down = int((existing or {}).get("down_cnt") or 0)
        flat = int((existing or {}).get("flat_cnt") or 0)
    fund = collector.fetch_fund_flow()
    fund_day = fund.get(date_str, {})
    if not idx and existing:
        # 新浪历史接口偶发失败时，沿用库中已有的真实历史点位，绝不回退到实时值
        idx = {k: (existing.get(k), existing.get(k + "_p"))
               for k in ("sh", "sz", "cy", "hs", "z5", "z1", "kc") if existing.get(k) is not None}

    # 回退：历史日期活跃度/池子接口失败，沿用库中已有值
    if "up_cnt" in keep_fields and existing and existing.get("up_cnt"):
        up, flat, down = int(existing["up_cnt"]), int(existing["flat_cnt"] or 0), int(existing["down_cnt"])
    mood = indicators.calc_mood(zt_cnt, up, down, zb_rate, lb_max)
    # 历史日期沿用库中已有的情绪真值，避免用自算值覆盖
    if not is_latest and existing and existing.get("mood") is not None:
        mood = int(existing["mood"])
    # 若指定保留 mood（如沿用财联社种子值），直接覆盖
    if "mood" in keep_fields and existing and existing.get("mood") is not None:
        mood = int(existing["mood"])


    def ip(key):
        v = idx.get(key)
        return v[0] if v else None
    def ipct(key):
        v = idx.get(key)
        return v[1] if v else None

    amt = collector.fetch_amount() if is_latest else (existing or {}).get("amt")

    rec = {
        "trade_date": date_str, "week": _week_of(date_str),
        "sh": ip("sh"), "sh_p": ipct("sh"), "sz": ip("sz"), "sz_p": ipct("sz"),
        "cy": ip("cy"), "cy_p": ipct("cy"), "hs": ip("hs"), "hs_p": ipct("hs"),
        "z5": ip("z5"), "z5_p": ipct("z5"), "z1": ip("z1"), "z1_p": ipct("z1"),
        "kc": ip("kc"), "kc_p": ipct("kc"),
        "amt": amt, "amt_pct": None,
        # 注意：fetch_fund_flow 的日期字典里这四个键恒存在，上游缺值时为 None。
        # 用 dict.get(key, default) 会在「键存在但值为 None」时保留 None，
        # 反而把库里的真值覆盖掉，所以必须逐字段判 is not None 再回退。
        "main_net": fund_day.get("main_net") if fund_day.get("main_net") is not None
                    else (existing or {}).get("main_net"),
        "main_pct": fund_day.get("main_pct") if fund_day.get("main_pct") is not None
                    else (existing or {}).get("main_pct"),
        "super_net": fund_day.get("super_net") if fund_day.get("super_net") is not None
                     else (existing or {}).get("super_net"),
        "big_net": fund_day.get("big_net") if fund_day.get("big_net") is not None
                   else (existing or {}).get("big_net"),
        "up_cnt": up, "flat_cnt": flat, "down_cnt": down,
        "zt_cnt": zt_cnt, "dt_cnt": dt_cnt, "zb_cnt": zb_cnt, "zb_rate": zb_rate,
        "lb_max": lb_max, "lianban_cnt": lianban_cnt, "mood": mood,
        "yp_avg": yp["avg"], "yp_red": yp["red"], "yp_promo": yp["promo"],
        "concepts": concepts[:5],
        "promo": promo, "lianbanList": lb_list, "heat": heat,
    }
    storage.save_day(rec, full=True)
    return True, (f"全量采集完成[{parts.get('source', 'fuyao')}] 涨停{zt_cnt} "
                  f"跌停{dt_cnt} 炸板{zb_cnt} 封板率{parts.get('seal_rate')}% 情绪{mood}")


def _collect_intraday(date_str):
    """盘中轮询（2026-11 升级）：不再只刷指数，而是「盘中全量-lite」。

    实测东财涨停/炸板/跌停池与昨涨停表现盘中实时可用，fuyao 涨停池亦然，
    因此盘中即产出涨停族/连板天梯/题材热度/晋级梯队（含个股名单），
    data_status 仍为 1（半成品：收盘后 15:30 全量采集会覆盖为终值）。
    核心字段（指数/成交额/广度）任一失败则整体放弃写入，
    避免用 null 覆盖上一次成功的快照（上午曾因此页面大量字段空窗）。
    """
    # REPLACE INTO 会覆盖整行；资金流向：盘中走东财 push2 实时接口（日线
    # 接口盘中无今日行），失败时沿用已有值
    old = storage.get_snapshot(date_str) or {}
    fund_rt = collector.fetch_fund_flow_realtime() or {
        "main_net": old.get("main_net"), "main_pct": old.get("main_pct"),
        "super_net": old.get("super_net"), "big_net": old.get("big_net"),
    }
    idx = collector.fetch_indices_spot()
    if not idx:
        return False, "指数接口无数据（新浪+腾讯均失败）"
    amt = collector.fetch_amount()
    if amt is None:
        return False, "成交额接口无数据（新浪+腾讯均失败），放弃写入"
    # 全天成交额预估：东财 1 分钟线按昨日同时段占比外推（同花顺同口径），
    # 比线性外推准得多；失败退回线性。存 amt_est 供导出层直接透传。
    amt_est = None
    try:
        amt_est, _ = collector.fetch_amt_estimate(amt)
    except Exception as e:
        log.warning("成交额预估失败: %s", e)
    # 广度：同花顺全市场快照优先，乐咕降级，东财 clist 扫描兜底
    up = flat = down = None
    if config.FUYAO_ENABLED:
        try:
            b = fuyao.market_breadth()
            if b.get("total"):
                up, flat, down = b["up"], b["flat"], b["down"]
        except Exception as e:
            log.warning("盘中 fuyao 快照失败，降级乐咕: %s", e)
    if up is None:
        act = collector.fetch_activity()
        up = int(act.get("上涨", 0) or 0)
        down = int(act.get("下跌", 0) or 0)
        flat = int(act.get("平盘", 0) or 0)
        if not (up or down):
            em = collector.fetch_breadth_em()
            if em:
                up, flat, down = em
    if not (up or down):
        return False, "涨跌家数全部源失败（fuyao/乐咕/东财），放弃写入"

    # 涨停族：fuyao 主源（题材含个股名单），东财池子降级（盘中实时可用）。
    # 重接口做 120 秒节流：1 分钟轮询时约隔一轮才刷涨停族，轻量字段（指数/
    # 成交额/预估/资金流/广度）每轮都刷；被节流的轮次沿用库中上次值。
    prev_date = calendar_util.previous_trade_day(date_str)
    parts = None
    now_ts = time.time()
    if now_ts - _zt_poll.get("ts", 0) < 110:
        parts = None  # 节流窗口内：跳过涨停族
    elif config.FUYAO_ENABLED and config.PRIMARY_SOURCE == "fuyao":
        try:
            parts = fuyao.build_day(date_str, prev_date)
            _zt_poll["ts"] = now_ts
        except Exception as e:
            log.warning("盘中 fuyao 涨停族失败，降级东财池子: %s", e)
    if parts is None and now_ts - _zt_poll.get("ts", 0) >= 110:
        try:
            parts = _parts_from_akshare(date_str, prev_date, [], None)
            _zt_poll["ts"] = now_ts
        except Exception as e:
            log.warning("盘中东财池子也失败，本轮只刷基础字段: %s", e)
            parts = None

    if parts:
        zt_cnt = parts["zt_cnt"]
        dt_cnt = parts["dt_cnt"]
        zb_cnt = parts["zb_cnt"]
        zb_rate = parts["zb_rate"]
        lb_max = parts["lb_max"]
        lianban_cnt = parts["lianban_cnt"]
        lb_list = parts["lianbanList"]
        heat = parts["heat"]
        promo = parts["promo"]
        yp = parts["yp"]
        concepts = parts["concepts"]
        # 盘中涨跌停族齐了，情绪用与收盘一致的真实口径（不再红盘率估算，
        # 保证近15日均值序列连续可比）
        mood = indicators.calc_mood(zt_cnt, up, down, zb_rate, lb_max)
    else:
        # 节流轮/涨停族接口失败：沿用库中上次成功的值，绝不写 None 覆盖
        zt_cnt = old.get("zt_cnt"); dt_cnt = old.get("dt_cnt")
        zb_cnt = old.get("zb_cnt"); zb_rate = old.get("zb_rate")
        lb_max = old.get("lb_max"); lianban_cnt = old.get("lianban_cnt")
        mood = old.get("mood")
        yp = {"avg": old.get("yp_avg"), "red": old.get("yp_red"), "promo": old.get("yp_promo")}
        lb_list = heat = promo = []
        concepts = old.get("concepts") or []

    # 新浪/腾讯偶发只返回部分指数：缺失项沿用库内上一轮成功值，
    # 不能用 None 覆盖好数据（2026-11-22 审查发现的实际污染路径）。
    def ip(k):
        return idx[k][0] if k in idx else old.get(k)

    def ipct(k):
        return idx[k][1] if k in idx else old.get(k + "_p")
    rec = {
        "trade_date": date_str, "week": _week_of(date_str),
        "data_time": datetime.now().strftime("%H:%M"),
        "sh": ip("sh"), "sh_p": ipct("sh"), "sz": ip("sz"), "sz_p": ipct("sz"),
        "cy": ip("cy"), "cy_p": ipct("cy"), "hs": ip("hs"), "hs_p": ipct("hs"),
        "z5": ip("z5"), "z5_p": ipct("z5"), "z1": ip("z1"), "z1_p": ipct("z1"),
        "kc": ip("kc"), "kc_p": ipct("kc"), "amt": amt, "amt_pct": None, "amt_est": amt_est,
        "main_net": fund_rt.get("main_net"), "main_pct": fund_rt.get("main_pct"),
        "super_net": fund_rt.get("super_net"), "big_net": fund_rt.get("big_net"),
        "up_cnt": up, "flat_cnt": flat, "down_cnt": down,
        "zt_cnt": zt_cnt, "dt_cnt": dt_cnt, "zb_cnt": zb_cnt, "zb_rate": zb_rate,
        "lb_max": lb_max, "lianban_cnt": lianban_cnt, "mood": mood,
        "yp_avg": yp.get("avg"), "yp_red": yp.get("red"), "yp_promo": yp.get("promo"),
        "concepts": concepts[:5], "promo": promo, "lianbanList": lb_list, "heat": heat,
    }
    storage.upsert_snapshot({**rec, "data_status": 1})
    if parts:
        # 盘中涨停族子表也写入（题材含个股名单），收盘全量会覆盖为终值；
        # parts 为空时不写，保留上一次成功轮询的子表数据
        storage.replace_children(date_str, rec)
    n = zt_cnt if zt_cnt is not None else "?"
    return True, (f"盘中刷新完成 涨停{n} 上涨{up} 下跌{down} 情绪{mood} "
                  f"[{parts.get('source','—') if parts else '基础'}]")


def backfill(days=None):
    """回溯采集最近 N 个交易日（倒序，从旧到新写）。"""
    days = days or config.HISTORY_DAYS
    ok_cnt = fail_cnt = 0
    for d in reversed(_trading_days_back(days)):
        ok, msg = collect_day(d, mode="full", trigger_by="backfill")
        log.info("回溯 %s: %s - %s", d, "OK" if ok else "FAIL", msg)
        ok_cnt += ok
        fail_cnt += not ok
    export_json()
    return {"ok": ok_cnt, "fail": fail_cnt}


def export_json():
    """从数据库导出前端数据。

    开源版只内置最近 30 个交易日（config.RECENT_DAYS），不随仓库分发全历史行情。
    导出仍分三份，便于后续运行时自然增长：
      - records.json          最近 RECENT_DAYS 个交易日（默认 30）
      - hist/YYYY-MM.json     按月分片（额外带上月最后一个交易日，保证首日环比可算）
      - records.months.json   月份 → 天数索引
    前端翻到窗口之外时再按月拉取，合并后按日期去重排序。
    """
    import json
    recs = storage.load_all()
    # 计算 amtPct / moodDelta（recs 为日期倒序，前一天是 i+1）
    for i, r in enumerate(recs):
        prev = recs[i + 1] if i + 1 < len(recs) else None
        # 盘中记录（data_status=1）：amt 是截至当前的累计值，直接与昨日全天比
        # 会得出 -34% 这种假缩量。预估口径优先用盘中采集算好的 amt_est
        # （东财 1 分钟线按昨日同时段占比外推，与同花顺客户端一致），
        # 库里没有时退回线性外推；开盘不足 10 分钟都不给。
        intraday = r.get("data_status") == 1
        cur_amt = float(r["amt"]) if r.get("amt") is not None else None
        est = float(r["amtEst"]) if r.get("amtEst") is not None else None
        if intraday and cur_amt:
            tmin = collector._traded_minutes()
            if est is None and tmin >= 10:
                est = round(cur_amt / tmin * 240, 2)
            if tmin < 10:
                est = None
        r["amtEst"] = est
        if est is not None:
            cur_amt = est
        # 严谨性：盘中记录若拿不到预估（开盘 <10 分钟、接口失败），累计成交额
        # 与昨日全天没有可比性（09:35 会算出 -97% 假缩量），环比必须置空
        if prev and cur_amt and prev.get("amt") and (est is not None or not intraday):
            r["amtPct"] = round((cur_amt - float(prev["amt"])) / float(prev["amt"]) * 100, 1)
        else:
            r["amtPct"] = None
        r["moodDelta"] = (r["mood"] - prev["mood"]) if (prev and r.get("mood") is not None and prev.get("mood") is not None) else None
        # 补齐前端字段名
        r.setdefault("date", r.pop("date", None))

    total = len(recs)
    # 最近 30 个交易日：表格 15 行/页 = 2 页，够「近 5 日柱」与矩阵用；可按环境变量调。
    recent_n = config.RECENT_DAYS
    config.OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    # ---- 1) 热数据 ----
    recent = recs[:recent_n]
    _write_json(config.OUTPUT_FILE, recent)

    # ---- 2) 按月分片（含上月最后一个交易日）----
    hist_dir = config.HIST_DIR
    hist_dir.mkdir(parents=True, exist_ok=True)
    months, written = [], 0
    i = 0
    while i < total:
        ym = (recs[i].get("date") or "")[:7]
        if not ym:
            i += 1
            continue
        j = i
        while j < total and (recs[j].get("date") or "")[:7] == ym:
            j += 1
        slice_ = recs[i:j]
        # 该月之后（日期更早）紧邻的那一条 = 上月最后一个交易日；带上它，
        # 「该月第一个交易日」的环比/连续性才算得出来
        if j < total:
            slice_ = slice_ + [recs[j]]
        months.append({"ym": ym, "n": j - i})
        if _write_json(hist_dir / f"{ym}.json", slice_, only_if_changed=True):
            written += 1
        i = j

    # ---- 3) 月份索引 ----
    _write_json(config.MONTHS_FILE,
                {"total": total, "recent": len(recent), "months": months},
                only_if_changed=True)

    log.info("已导出 %d 条（热数据 %d 条、%d 个月分片，本次重写 %d 片）-> %s",
             total, len(recent), len(months), written, config.OUTPUT_FILE)
    return total


def _write_json(path, data, only_if_changed=False):
    """写 JSON；only_if_changed 时内容没变就不动文件（430 个月分片不必天天重写）。"""
    import json
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if only_if_changed:
        try:
            if path.stat().st_size == len(payload.encode("utf-8")) and \
                    path.read_text(encoding="utf-8") == payload:
                return False
        except OSError:
            pass
    # 临时文件名必须唯一：手动采集线程与调度线程可能同时导出，共用同一个
    # records.json.tmp 会互相覆盖，写出一半 A 一半 B 的损坏 JSON。
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)          # 原子替换：导出过程中访问到的永远是完整文件
    return True

