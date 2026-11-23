"""补齐历史交易日数据（用于数据源已过回溯窗口、但需展示更多行的场景）。

与 pipeline.collect_day 的区别：
- 指数点位/涨跌幅：用新浪历史日线（真实可回溯，规避东财限流）
- 涨跌家数：乐咕接口只有实时值，历史无法回溯 → 用「涨停数→红盘率」拟合估算
- 情绪分：用 indicators 拟合公式自算（非财联社真值）
- 涨停/跌停/炸板/昨涨停池、概念、梯队、连板：真实采集

因此补齐日的数据质量为「部分估算」，会在日志中明确标注。
"""
import sys
import json
from datetime import datetime, timedelta

from backend import config, collector, indicators, storage, calendar_util

# 红盘率估算：基于现有真实样本拟合 red% ≈ A*涨停数 + B，截断到 [5, 95]
RED_A = 0.945
RED_B = -12.4
TOTAL_STOCKS = 5170  # 全市场股票总数（涨+跌+平），用于按比例分配家数


def _week_of(date_str):
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return "周" + "一二三四五六日"[d.weekday()]


def _estimate_breadth(zt_cnt):
    """用涨停数估算涨跌家数 (up, flat, down)。"""
    red = max(5.0, min(95.0, RED_A * zt_cnt + RED_B))
    up = int(round(TOTAL_STOCKS * red / 100))
    flat = int(round(TOTAL_STOCKS * 0.01))  # 平盘约 1%
    down = max(0, TOTAL_STOCKS - up - flat)
    return up, flat, down


def backfill_one(date_str):
    """补齐单个历史交易日。返回 (ok, msg)。"""
    zt = collector.fetch_zt_pool(date_str)
    zb = collector.fetch_zb_pool(date_str)
    dt = collector.fetch_dt_pool(date_str)
    prev = collector.fetch_previous(date_str)
    if zt is None or zt.empty:
        return False, "涨停池为空，跳过"

    zt_cnt = len(zt)
    zb_cnt = len(zb) if zb is not None else 0
    dt_cnt = len(dt) if dt is not None else 0
    zbrate = round(zb_cnt / (zt_cnt + zb_cnt) * 100, 1) if (zt_cnt + zb_cnt) else 0.0

    lb_list = indicators.lianban_list_from_pool(zt)
    lb_max = max([x[1] for x in lb_list], default=1)
    lianban_cnt = len(lb_list)
    concepts = indicators.concept_heat_from_pool(zt)
    promo = indicators.promo_from_pools(zt, prev, zb)
    yp = indicators.yp_from_previous(prev)

    up, flat, down = _estimate_breadth(zt_cnt)
    mood = indicators.calc_mood(zt_cnt, up, down, zbrate, lb_max)

    # 指数：新浪历史（真实）
    prev_day = calendar_util.previous_trade_day(date_str)
    idx = collector.index_history_to_spot(date_str, prev_day)
    fund = collector.fetch_fund_flow().get(date_str, {})

    def ip(k):
        return idx.get(k, (None, None))[0]
    def ipct(k):
        return idx.get(k, (None, None))[1]

    rec = {
        "trade_date": date_str, "week": _week_of(date_str),
        "sh": ip("sh"), "sh_p": ipct("sh"), "sz": ip("sz"), "sz_p": ipct("sz"),
        "cy": ip("cy"), "cy_p": ipct("cy"), "hs": ip("hs"), "hs_p": ipct("hs"),
        "z5": ip("z5"), "z5_p": ipct("z5"), "z1": ip("z1"), "z1_p": ipct("z1"),
        "kc": ip("kc"), "kc_p": ipct("kc"),
        "amt": None, "amt_pct": None,
        "main_net": fund.get("main_net"), "main_pct": fund.get("main_pct"),
        "super_net": fund.get("super_net"), "big_net": fund.get("big_net"),
        "up_cnt": up, "flat_cnt": flat, "down_cnt": down,
        "zt_cnt": zt_cnt, "dt_cnt": dt_cnt, "zb_cnt": zb_cnt, "zb_rate": zbrate,
        "lb_max": lb_max, "lianban_cnt": lianban_cnt, "mood": mood,
        "yp_avg": yp["avg"], "yp_red": yp["red"], "yp_promo": yp["promo"],
        "concepts": [[c, n] for c, n in concepts[:5]],
        "promo": promo, "lianbanList": lb_list, "heat": concepts,
    }
    storage.save_day(rec, full=True)
    return True, (f"补齐完成 涨停{zt_cnt} 跌停{dt_cnt} 炸板{zb_cnt} 情绪{mood} "
                  f"| 涨跌家数为估算({up}/{down}) 指数{'真实' if idx.get('sh') else '缺失'}")


def main(dates):
    storage.init_db()
    for d in dates:
        log_id = storage.log_start(d, "full", "backfill_estimated")
        ok, msg = backfill_one(d)
        storage.log_finish(log_id, "success" if ok else "failed", rows=1 if ok else 0,
                           error=None if ok else msg)
        print(f"{d}: {'OK' if ok else 'FAIL'} - {msg}")
    n = len(storage.load_all())
    from backend import pipeline
    pipeline.export_json()
    print(f"\n导出完成，当前共 {n} 个交易日")
    return n


if __name__ == "__main__":
    dates = sys.argv[1:] or ["2026-10-30", "2026-11-02", "2026-11-03"]
    main(dates)
