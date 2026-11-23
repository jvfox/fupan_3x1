"""定时调度：交易日收盘全量采集 + 盘中轻量轮询。

使用 APScheduler 的 cron / interval 触发器。
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from backend import config, pipeline, storage, calendar_util

log = logging.getLogger(__name__)

CST = ZoneInfo("Asia/Shanghai")


def now_cst():
    """统一用北京时间取日期/时段，避免服务器 TZ  cron 与业务判断错位。"""
    return datetime.now(CST)

scheduler = BackgroundScheduler(timezone="Asia/Shanghai")


def is_trade_day():
    """基于真实交易日历判断（含节假日）。"""
    return calendar_util.is_trade_day()


def job_full():
    from backend import anomaly, indices
    if not is_trade_day():
        log.info("非交易日，跳过全量采集")
        return
    today = now_cst().strftime("%Y-%m-%d")
    log.info("触发收盘全量采集: %s", today)
    ok, msg = pipeline.collect_day(today, mode="full", trigger_by="scheduler")
    log.info("全量采集结果: %s - %s", ok, msg)
    if ok:
        pipeline.export_json()
        # 收盘后落库当日异动事件流+上证分钟线，供历史日期重放大盘异动标注
        anomaly.daily_save(today)
        # 各核心指数当日分时落库（自选指数分时的历史回放依赖它逐日积累）
        try:
            n = indices.save_day(today)
            if n:
                log.info("核心指数分时落库 %s: %d 个指数", today, n)
            else:
                log.warning("核心指数分时落库 %s: 0 个指数（上游无数据或不足 10 点）", today)
        except Exception as e:
            log.warning("核心指数分时落库失败 %s: %s", today, e)


def job_intraday():
    if not is_trade_day():
        return
    now = now_cst()
    t = now.strftime("%H:%M")
    if not (config.INTRADAY_START <= t <= config.INTRADAY_END):
        return
    today = now.strftime("%Y-%m-%d")
    ok, msg = pipeline.collect_day(today, mode="intraday", trigger_by="scheduler")
    log.info("盘中轮询: %s - %s", ok, msg)
    if ok:
        pipeline.export_json()


def job_calendar_refresh():
    """每日刷新交易日历缓存（内部判断过期，未过期则跳过拉取）。"""
    ok, msg = calendar_util.refresh()
    log.info("交易日历检查: %s - %s", ok, msg)


def start():
    hh, mm = config.FULL_COLLECT_TIME.split(":")
    scheduler.add_job(job_full, CronTrigger(hour=int(hh), minute=int(mm), day_of_week="mon-fri"),
                      id="full", misfire_grace_time=600, coalesce=True, max_instances=1)

    interval = config.INTRADAY_POLL_MINUTES
    sh, sm = config.INTRADAY_START.split(":")
    eh, em = config.INTRADAY_END.split(":")
    scheduler.add_job(job_intraday, CronTrigger(
        minute=f"*/{interval}", hour=f"{sh}-{eh}", day_of_week="mon-fri"
    ), id="intraday", misfire_grace_time=300, coalesce=True, max_instances=1)

    ch, cm = config.CALENDAR_REFRESH_TIME.split(":")
    scheduler.add_job(job_calendar_refresh, CronTrigger(hour=int(ch), minute=int(cm)),
                      id="calendar", misfire_grace_time=3600, coalesce=True, max_instances=1)

    scheduler.start()
    log.info("调度器已启动: 全量 %s, 盘中每 %d 分钟, 日历 %s",
             config.FULL_COLLECT_TIME, interval, config.CALENDAR_REFRESH_TIME)


def stop():
    if scheduler.running:
        scheduler.shutdown(wait=False)
