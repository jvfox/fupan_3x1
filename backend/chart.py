"""个股悬浮卡：名称→代码映射 + 批量实时快照。

前端悬浮卡直接加载新浪静态图（image.sinajs.cn/newchart/*.gif），后端只需回答两件事：
  1. 这只票的新浪代码（resolve_symbol / code_map，本地映射表一次加载）
  2. 现在多少钱（peek → _hq，一次批量报价，5 秒内存缓存）

历史上这里还有一整套本地 K 线抓取 + 按日落盘 + warm 预热链路（get_chart/is_cached/
_fetch_daily/_fetch_minutes/_symbol_data/_build），在悬浮卡改用静态图后已整体下线，
2026-11-22 清理。完整旧版备份在 data/backup/chart.py.full.bak，需要恢复交互明细时从那里取。
"""
import json
import re
import time
import threading
import logging

import akshare as ak
import requests

from backend import config

log = logging.getLogger(__name__)

CODE_CACHE = config.BASE_DIR / "backend" / "data" / "stock_codes.json"

_CODE = None
_code_lock = threading.Lock()

_FW = str.maketrans(
    "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ",
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_NORMMAP = None

_session = requests.Session()
_session.trust_env = False
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Referer": "https://finance.sina.com.cn",
})


def code_map():
    """名称 -> 新浪代码（sh/sz + 6位）。进程内只读一次盘，避免高频调用反复 IO。"""
    global _CODE
    if _CODE is not None:
        return _CODE
    with _code_lock:
        if _CODE is not None:
            return _CODE
        if CODE_CACHE.exists():
            try:
                _CODE = json.loads(CODE_CACHE.read_text(encoding="utf-8"))
                return _CODE
            except Exception:
                pass
        df = ak.stock_info_a_code_name()
        out = {}
        for _, row in df.iterrows():
            code = str(row["code"]).zfill(6)
            name = str(row["name"]).strip()
            if not name:
                continue
            prefix = "sh" if code.startswith("6") else ("sz" if code[0] in "03" else "bj")
            out[name] = prefix + code
        CODE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        CODE_CACHE.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        log.info("股票名称代码映射已缓存 %d 条", len(out))
        _CODE = out
        return out


def _norm(s):
    """老股票名在代码表里是全角「深深房Ａ」，榜单数据里是半角「深深房A」。"""
    return str(s).translate(_FW).replace(" ", "").replace("ST", "").upper()


def resolve_symbol(name):
    m = code_map()
    sym = m.get(str(name).strip())
    if sym:
        return sym
    global _NORMMAP
    if _NORMMAP is None:
        _NORMMAP = {_norm(k): v for k, v in m.items()}
    return _NORMMAP.get(_norm(name))


# ---------------- 批量快照 ----------------
# 关键：名称→代码是本地查表（毫秒级），快照要打新浪 hq（外网，冷连接实测 5 秒）。
# 悬停卡真正要等的是「代码」——拿到代码就能立刻显示新浪静态图（日K/分时）。
# 因此报价必须异步补：peek 只同步返回代码 + 缓存里已有的价，缺价交给后台线程预热，
# 下一次悬停自然命中。绝不让 5 秒的上游超时挡住图片，否则用户鼠标一晃就关了卡，
# 表现就是「悬停显示不了分时和日K」。
HQ_URL = "https://hq.sinajs.cn/list="
HQ_TTL = 8
_hq_mem = {}
_hq_lock = threading.Lock()
_hq_pending = set()          # 正在后台补价的 symbol，去重防止重复开线程
_HQ_RE = re.compile(r'var hq_str_([a-z]{2}\d{6})="(.*)";')


def _hq_fetch(symbols):
    """后台实拉一次批量报价并写入缓存；失败也不抛，前端退化成只显示静态图。"""
    try:
        r = _session.get(HQ_URL + ",".join(symbols[:60]), timeout=4)
        r.encoding = "gbk"
        now = time.time()
        with _hq_lock:
            for line in r.text.splitlines():
                m = _HQ_RE.match(line.strip())
                if not m:
                    continue
                sym, raw = m.group(1), m.group(2)
                f = raw.split(",")
                if len(f) < 6:
                    continue
                try:
                    prev = float(f[2])
                    px = float(f[3]) or prev
                    q = {"price": round(px, 2), "prev": round(prev, 2),
                         "high": round(float(f[4]), 2), "low": round(float(f[5]), 2),
                         "pct": round((px - prev) / prev * 100, 2) if prev else None}
                except (TypeError, ValueError):
                    continue
                _hq_mem[sym] = (now, q)
    except Exception as e:
        log.warning("快照获取失败: %s", e)


def _hq_warm(symbols):
    """异步补价：只在缓存缺失且没有并发任务时开一个后台线程，回来的值写进 _hq_mem。"""
    now = time.time()
    with _hq_lock:
        need = [s for s in dict.fromkeys(symbols)
                if s and not (s in _hq_mem and now - _hq_mem[s][0] < HQ_TTL)
                and s not in _hq_pending]
        for s in need:
            _hq_pending.add(s)
    if not need:
        return

    def worker():
        try:
            _hq_fetch(need)
        finally:
            with _hq_lock:
                for s in need:
                    _hq_pending.discard(s)
    threading.Thread(target=worker, daemon=True, name="stock-hq-warm").start()


def _hq_cached(symbols):
    """只读缓存，不碰网络；缺失的直接不返回，交给 _hq_warm 后台补。"""
    now = time.time()
    out = {}
    with _hq_lock:
        for s in symbols:
            hit = _hq_mem.get(s)
            if hit and now - hit[0] < HQ_TTL:
                out[s] = hit[1]
    return out


def peek(names):
    """名称 -> {symbol, [price, prev, pct...]}。本地查表同步出代码，报价后台补、下次命中。"""
    items, syms = {}, {}
    for n in names:
        s = resolve_symbol(n)
        if s:
            syms[n] = s
    quotes = _hq_cached(syms.values()) if syms else {}
    if syms:
        _hq_warm([s for s in syms.values() if s not in quotes])
    for n, s in syms.items():
        item = {"symbol": s}
        q = quotes.get(s)
        if q:
            item.update(q)
        items[n] = item
    return {"items": items}
