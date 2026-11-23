"""交易日历：基于 AkShare calendar_trade_date_hist_sina，带本地缓存。

- 首次加载拉取全年交易日列表，缓存到本地 JSON（按年）。
- 判断 is_trade_day / 最近 N 个交易日 均基于真实日历。
- 接口失败时降级为"工作日"判断，保证调度不中断。
"""
import json
import logging
from datetime import datetime, timedelta, date
from pathlib import Path

from backend import config

log = logging.getLogger(__name__)

CACHE_DIR = config.BASE_DIR / "data" / "calendar"
_cache = {}
_missing_years = set()   # 真实日历不覆盖的年份（如 1989 及更早），避免逐日重复拉网络
_meta_file = CACHE_DIR / "_meta.json"


def _fetch_and_cache():
    """从 AkShare 拉取全量交易日历并写入按年缓存。返回 {year: set} 或 None。"""
    import akshare as ak
    df = ak.calendar_trade_date_hist_sina()
    dates = set(str(d)[:10] for d in df["trade_date"])
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    by_year = {}
    for d in dates:
        by_year.setdefault(d[:4], set()).add(d)
    for y, ds in by_year.items():
        (CACHE_DIR / f"trade_dates_{y}.json").write_text(
            json.dumps(sorted(ds)), encoding="utf-8")
    meta = {"refreshed_at": datetime.now().isoformat(timespec="seconds"),
            "last_trade_date": max(dates), "years": sorted(by_year)}
    _meta_file.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    log.info("交易日历已更新，共 %d 个交易日，覆盖到 %s", len(dates), max(dates))
    return by_year


def _cache_stale():
    """判断缓存是否过期：无 meta、超 TTL、或未覆盖当前年份（跨年）→ 需刷新。"""
    if not _meta_file.exists():
        return True
    try:
        meta = json.loads(_meta_file.read_text())
        refreshed = datetime.fromisoformat(meta["refreshed_at"])
        if (datetime.now() - refreshed).days >= config.CALENDAR_CACHE_TTL_DAYS:
            return True
        if str(date.today().year) not in meta.get("years", []):
            return True
    except Exception as e:
        log.warning("读取日历 meta 失败: %s", e)
        return True
    return False


def refresh(force=False):
    """主动刷新交易日历缓存。调度器 / 管理后台可调用。返回 (ok, msg)。"""
    global _cache
    if not force and not _cache_stale():
        return True, "缓存有效，无需刷新"
    try:
        by_year = _fetch_and_cache()
        _cache.clear()
        _cache.update(by_year)
        last = max(max(s) for s in by_year.values()) if by_year else "-"
        return True, f"已刷新，覆盖到 {last}"
    except Exception as e:
        log.error("交易日历刷新失败: %s", e)
        return False, str(e)


def cache_status():
    """返回日历缓存状态，供管理后台展示。"""
    if not _meta_file.exists():
        return {"refreshed_at": None, "last_trade_date": None, "stale": True, "years": []}
    try:
        meta = json.loads(_meta_file.read_text())
        return {"refreshed_at": meta.get("refreshed_at"),
                "last_trade_date": meta.get("last_trade_date"),
                "stale": _cache_stale(), "years": meta.get("years", [])}
    except Exception:
        return {"refreshed_at": None, "last_trade_date": None, "stale": True, "years": []}


def _load_year(year):
    if year in _cache:
        return _cache[year]
    cache_file = CACHE_DIR / f"trade_dates_{year}.json"
    # 缓存过期时先尝试整体刷新一次
    if _cache_stale():
        refresh()
    if cache_file.exists():
        try:
            _cache[year] = set(json.loads(cache_file.read_text()))
            return _cache[year]
        except Exception as e:
            log.warning("读取交易日历缓存失败: %s", e)
    # 本地缓存缺失，直接拉取（同一年只拉一次；日历不覆盖的年份记入 _missing_years）
    year = str(year)
    if year in _missing_years:
        return None
    try:
        by_year = _fetch_and_cache()
        _cache.update(by_year)
        if year not in _cache:
            _missing_years.add(year)
            return None
        return _cache[year]
    except Exception as e:
        log.error("交易日历接口失败，降级为工作日判断: %s", e)
        return None


def is_trade_day(d=None):
    """判断是否为交易日。d 可为 date/str，默认今天。"""
    d = d or date.today()
    ds = d.isoformat() if isinstance(d, (date, datetime)) else str(d)[:10]
    dates = _load_year(ds[:4])
    if dates is None:  # 降级
        return (d if isinstance(d, date) else datetime.strptime(ds, "%Y-%m-%d").date()).weekday() < 5
    return ds in dates


def last_n_trade_days(n, end=None):
    """返回截止 end（含）的最近 n 个交易日，日期倒序（最近在前）。"""
    end = end or date.today()
    if isinstance(end, str):
        end = datetime.strptime(end[:10], "%Y-%m-%d").date()
    out, d = [], end
    # 向前找够 n 个交易日，最多回溯 60 天
    for _ in range(90):
        if is_trade_day(d):
            out.append(d.isoformat())
            if len(out) >= n:
                break
        d -= timedelta(days=1)
    return out


def previous_trade_day(d=None):
    """返回 d 的前一个交易日；超出真实日历覆盖范围（如 1990 年首日）返回 None。

    必须限制回溯步数：日历最早只到 1990-12-19，无边界地往前找会逐年触发
    _fetch_and_cache()，把整批历史回填拖死。
    """
    d = d or date.today()
    if isinstance(d, str):
        d = datetime.strptime(d[:10], "%Y-%m-%d").date()
    d -= timedelta(days=1)
    for _ in range(30):
        year = str(d.year)
        if year in _missing_years:
            return None
        dates = _load_year(year)
        if dates is not None:
            if d.isoformat() in dates:
                return d.isoformat()
        elif d.weekday() < 5:  # 接口失败才降级为工作日；日历明确不覆盖的年份已提前返回
            return d.isoformat()
        d -= timedelta(days=1)
    return None
