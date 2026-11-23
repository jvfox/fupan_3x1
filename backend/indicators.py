"""指标计算：把 AkShare 原始数据转换为前端 records 结构。

核心是市场热度（情绪分）——基于 demo 12 天财联社真实值拟合的线性压缩模型，
参数集中在 MOOD_FIT，可随时调整。设计目标：贴近财联社、0~70、不依赖其黑箱接口。
"""
import logging

log = logging.getLogger(__name__)

# ---------- 市场热度算法（基于 demo 12 天财联社真实值拟合）----------
# 模型: mood = clip(A × 涨停数 + B × 红盘率 + C, 0, CAP)
#   红盘率 = 上涨 / (上涨 + 下跌)
# 拟合结果 MAE≈4.8（旧四因子加权模型 MAE≈21.6，已弃用）。
# 财联社热度呈"高位压缩"特性（极少超过 60），故 CAP 设为 70。
MOOD_FIT = {
    "A": 0.13,   # 涨停数系数
    "B": 0.46,   # 红盘率系数(%)
    "C": 5.0,    # 截距
    "CAP": 65.0, # 上限压缩
}

# 旧模型参数（保留供参考/回退）
MOOD_WEIGHTS = {"zt": 0.35, "red": 0.30, "seal": 0.25, "height": 0.10}
ZT_FULL, ZB_GOOD, ZB_BAD, LB_FULL = 90, 20.0, 65.0, 7


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def calc_mood(zt, up, down, zb_rate=None, lb_max=None, fit=None):
    """返回 0~CAP 的整数市场热度分（拟合模型）。"""
    f = fit or MOOD_FIT
    red = (up / (up + down) * 100) if (up + down) else 50.0
    score = f["A"] * zt + f["B"] * red + f["C"]
    return int(round(_clamp(score, 0, f["CAP"])))


def calc_mood_legacy(zt, up, down, zb_rate, lb_max, weights=None):
    """旧四因子加权模型（已弃用，保留供对比）。"""
    w = weights or MOOD_WEIGHTS
    zt_score = _clamp(zt / ZT_FULL * 100)
    red_score = _clamp(up / (up + down) * 100) if (up + down) else 50.0
    if zb_rate <= ZB_GOOD:
        seal_score = 100.0
    elif zb_rate >= ZB_BAD:
        seal_score = 0.0
    else:
        seal_score = (ZB_BAD - zb_rate) / (ZB_BAD - ZB_GOOD) * 100
    height_score = _clamp(lb_max / LB_FULL * 100)
    score = w["zt"] * zt_score + w["red"] * red_score + w["seal"] * seal_score + w["height"] * height_score
    return int(round(_clamp(score)))


# 分档切点按近 15 个交易日实际分布标定（mood 实际区间 14~65）。
# 原来的 80/60/40/20 与 CAP=65 冲突："亢奋"永远达不到，"震荡"也几乎不出现。
MOOD_TAGS = ((58, "亢奋"), (44, "偏暖"), (32, "震荡"), (20, "退潮"))


def mood_tag(m):
    for th, name in MOOD_TAGS:
        if m >= th:
            return name
    return "冰点"


# ---------- 从涨停池提取结构 ----------
def lianban_list_from_pool(df):
    """连板天梯：连板数>=2 的个股 -> [[名称, 连板数, 行业], ...] 按板数降序。"""
    if df is None or df.empty or "连板数" not in df.columns:
        return []
    sub = df[df["连板数"] >= 2].copy()
    sub = sub.sort_values("连板数", ascending=False)
    out = []
    for _, row in sub.iterrows():
        industry = str(row.get("所属行业", ""))[:8]
        out.append([str(row["名称"]), int(row["连板数"]), industry])
    return out


def concept_heat_from_pool(df, top=None):
    """概念/行业热度：按所属行业统计涨停家数 -> [[行业, 家数, [个股名...]], ...] 降序。

    第 3 元素为该行业当日涨停个股名单，供前端悬浮展示；列缺失时为空列表。
    """
    if df is None or df.empty or "所属行业" not in df.columns:
        return []
    ind = df["所属行业"].astype(str).str.strip()
    valid = df[ind.ne("") & ind.ne("nan")].copy()
    valid["所属行业"] = ind[ind.ne("") & ind.ne("nan")]
    has_name = "名称" in valid.columns
    out = []
    for k, g in valid.groupby("所属行业"):
        names = [str(n) for n in g["名称"].tolist()] if has_name else []
        out.append([str(k), int(len(g)), names])
    out.sort(key=lambda v: -v[1])
    return out[:top] if top else out


def yp_from_previous(df):
    """昨涨停今表现：{avg, red, promo}。df 为 stock_zt_pool_previous_em 结果。"""
    if df is None or df.empty or "涨跌幅" not in df.columns:
        return {"avg": 0.0, "red": 0, "promo": 0}
    avg = round(float(df["涨跌幅"].mean()), 2)
    red = int((df["涨跌幅"] > 0).sum())
    # 晋级：今日仍涨停（涨跌幅接近各自涨停幅度，用 >=9.8% 近似主板；
    # 更严谨可用"涨停价"列判断：最新价>=涨停价）
    if "涨停价" in df.columns and "最新价" in df.columns:
        promo = int((df["最新价"] >= df["涨停价"] - 0.001).sum())
    else:
        promo = int((df["涨跌幅"] >= 9.8).sum())
    return {"avg": avg, "red": red, "promo": promo}


def promo_from_pools(zt_df, prev_df, zb_df):
    """涨停晋级梯队。

    prev_df: 昨日涨停股今日表现，含"昨日连板数"与"涨跌幅"/"涨停价"。
    对每只昨日涨停股，按昨日连板数 n 归入 "n进n+1" 梯队：
      - 今日仍封板 -> status=1（成）
      - 今日炸板   -> status=2（炸）
      - 其余       -> status=0（败）
    首板梯队：今日涨停池中 连板数==1 的个股，status 均为 1。
            首板当日不构成"晋级"，其结果要等下一交易日才知道，
            因此该梯队 win/rate 一律为 None，避免输出 "N/N · 100%" 的假晋级率。
    """
    tiers = {}

    def tier_of(n):
        return f"{n}进{n + 1}"

    if prev_df is not None and not prev_df.empty and "昨日连板数" in prev_df.columns:
        # 炸板股代码集合
        zb_codes = set()
        if zb_df is not None and not zb_df.empty and "代码" in zb_df.columns:
            zb_codes = set(zb_df["代码"].astype(str))

        for _, row in prev_df.iterrows():
            # 连板数兼容 int/float/字符串（pandas 可能给 "3.0"，isdigit 会误判成 1 板）
            try:
                n = int(float(row["昨日连板数"]))
            except (TypeError, ValueError):
                n = 1
            label = tier_of(n)
            code = str(row.get("代码", ""))
            name = str(row.get("名称", ""))
            industry = str(row.get("所属行业", ""))[:8]
            # 今日是否封板
            if "涨停价" in prev_df.columns and "最新价" in prev_df.columns:
                sealed = float(row["最新价"]) >= float(row["涨停价"]) - 0.001
            else:
                sealed = float(row["涨跌幅"]) >= 9.8
            status = 1 if sealed else (2 if code in zb_codes else 0)
            pct = row.get("涨跌幅")
            try:
                pct = float(pct)
            except (TypeError, ValueError):
                pct = None
            t = tiers.setdefault(label, {"win": 0, "total": 0, "stocks": [], "pcts": []})
            t["total"] += 1
            if sealed:
                t["win"] += 1
            if pct is not None:
                t["pcts"].append(pct)
            t["stocks"].append([name, status, industry])

    # 首板：今日涨停池中连板数==1（当日不构成晋级，win/rate 留空）
    if zt_df is not None and not zt_df.empty and "连板数" in zt_df.columns:
        first = zt_df[zt_df["连板数"] == 1]
        t = tiers.setdefault("首板", {"win": None, "total": 0, "stocks": []})
        for _, row in first.iterrows():
            industry = str(row.get("所属行业", ""))[:8]
            t["total"] += 1
            t["stocks"].append([str(row["名称"]), 1, industry])

    # 排序：板数高的梯队在前，首板最后
    def order(label):
        if label == "首板":
            return -1
        try:
            return int(label.split("进")[0])
        except Exception:
            return 0

    out = []
    for label in sorted(tiers, key=order, reverse=True):
        t = tiers[label]
        win = t["win"]
        pcts = t.get("pcts") or []
        out.append({
            "label": label,
            "win": win,
            "total": t["total"],
            # rate 只在有真实晋级结果时给；首板为 None，前端据此隐藏晋级率
            "rate": None if win is None else f"{win}/{t['total']}",
            # 该梯队昨日涨停股今日的表现：均涨 / 红盘家数（首板无意义 → None）
            "ypAvg": round(sum(pcts) / len(pcts), 2) if pcts else None,
            "ypRed": sum(1 for p in pcts if p > 0) if pcts else None,
            "stocks": t["stocks"],
        })
    return out
