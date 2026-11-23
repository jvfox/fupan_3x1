"""首次启动数据初始化：种子导入 + 兜底回溯。

开源版随仓库只带最近 30 个交易日的快照（web/data/records.seed.json），
不含全历史行情。空库首次启动时自动导入这批种子并导出前端 JSON；
若种子文件缺失，则按 AUTO_BACKFILL_DAYS（默认 15）回溯最近交易日。
重复执行按交易日覆盖，不会污染已有真实采集数据。
"""
import json
import logging

from backend import config, storage

log = logging.getLogger(__name__)


def import_seed():
    data = json.loads(config.SEED_FILE.read_text(encoding="utf-8"))
    cnt = 0
    for r in data:
        rec = {
            "trade_date": r["date"], "week": r["week"],
            "sh": r["sh"], "sh_p": r["shP"], "sz": r["sz"], "sz_p": r["szP"],
            "cy": r["cy"], "cy_p": r["cyP"], "hs": r["hs"], "hs_p": r["hsP"],
            "z5": r["z5"], "z5_p": r["z5P"], "z1": r["z1"], "z1_p": r["z1P"],
            "kc": r.get("kc"), "kc_p": r.get("kcP"),
            "amt": float(r["amt"]) if r.get("amt") else None, "amt_pct": r.get("amtPct"),
            "up_cnt": r["up"], "flat_cnt": r["flat"], "down_cnt": r["down"],
            "zt_cnt": r["zt"], "dt_cnt": r["dt"], "zb_cnt": r["zb"], "zb_rate": r["zbrate"],
            "lb_max": r["lb"], "lianban_cnt": r["lianban"], "mood": r["mood"],
            "yp_avg": r["yp"]["avg"], "yp_red": r["yp"]["red"], "yp_promo": r["yp"]["promo"],
            "concepts": r["concepts"], "promo": r["promo"],
            "lianbanList": r["lianbanList"], "heat": r["heat"],
        }
        storage.save_day(rec, full=True)
        cnt += 1
    log.info("种子数据导入完成: %d 天", cnt)
    return cnt


def _snapshot_count():
    with storage.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM daily_snapshot")
        return int(cur.fetchone()["c"])


def ensure_initial_data():
    """仅空库执行：导入种子（或回溯补全），再导出前端 JSON。返回导入天数。"""
    if _snapshot_count() > 0:
        return 0
    from backend import pipeline
    if config.SEED_FILE.exists():
        n = import_seed()
    elif config.AUTO_BACKFILL_DAYS > 0:
        log.info("种子文件缺失，首次启动回溯最近 %d 个交易日", config.AUTO_BACKFILL_DAYS)
        result = pipeline.backfill(config.AUTO_BACKFILL_DAYS)
        n = result.get("ok", 0)
    else:
        log.warning("种子文件缺失且 AUTO_BACKFILL_DAYS=0，首次启动无历史数据")
        n = 0
    pipeline.export_json()
    return n


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    storage.init_db()
    ensure_initial_data()
