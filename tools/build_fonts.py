#!/usr/bin/env python3
"""重建页面的本地中文子集字体（思源黑体 / Noto Sans SC，SIL OFL 1.1）。

为什么需要重建：子集只包含当前界面文案 + records.json 里出现过的汉字。
将来数据里出现新股票名、新概念，超出子集的那个字会回落到系统黑体；
跑一次本脚本即可把它们补进去。

用法：
    python tools/build_fonts.py            # 重新下载子集并打印 @font-face
    python tools/build_fonts.py --check    # 只检查现有子集是否覆盖当前字符集

依赖：仅标准库。字体来自 Google Fonts 的 text= 接口，产物放在 web/vendor/fonts/，
许可证原文见 web/vendor/fonts/OFL.txt。
"""
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FONT_DIR = ROOT / "web" / "vendor" / "fonts"
CHUNK = 440
UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")}
PUNCT = "，。、；：？！（）【】《》〈〉“”‘’—…·"


def collect_chars():
    """界面文案 + **热数据**（records.json）里出现过的汉字。

    刻意**不扫** web/data/hist/ 的历史月分片：全量历史有 1947 个汉字，扫进去要 5 片
    字体（约 700KB，比现在多 318KB），而多出来的 400 多个字只在「翻到几年前」时出现，
    回落到系统黑体（字体栈里已经排了苹方/鸿蒙/雅黑）完全够用。
    带宽优先，别为了几个陈年股票名的字形多下 300KB——真需要时再临时跑一次全量。
    """
    chars = set()
    files = [ROOT / "web" / "index.html",
             ROOT / "web" / "data" / "records.json",
             ]
    for path in files:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            try:
                data = json.loads(text)
                text = json.dumps(data, ensure_ascii=False)
            except ValueError:
                pass
        chars.update(c for c in text if "\u4e00" <= c <= "\u9fff")
    chars.update(PUNCT)
    return "".join(sorted(chars))


def unicode_range(chunk):
    cps = sorted({ord(c) for c in chunk})
    out, i = [], 0
    while i < len(cps):
        j = i
        while j + 1 < len(cps) and cps[j + 1] == cps[j] + 1:
            j += 1
        out.append("U+%X" % cps[i] if i == j else "U+%X-%X" % (cps[i], cps[j]))
        i = j + 1
    return ", ".join(out)


def fetch_subset(chunk):
    query = urllib.parse.quote(chunk, safe="")
    css_url = ("https://fonts.googleapis.com/css2?family=Noto+Sans+SC:"
               "wght@400..700&text=%s&display=swap" % query)
    css = urllib.request.urlopen(
        urllib.request.Request(css_url, headers=UA), timeout=40).read().decode()
    m = re.search(r"url\((https://[^)]+)\)", css)
    if not m:
        raise RuntimeError("Google Fonts 未返回 woff2 地址：" + css[:200])
    return urllib.request.urlopen(m.group(1), timeout=60).read()


def main():
    chars = collect_chars()
    print("字符集：%d 字" % len(chars))
    expected = (len(chars) + CHUNK - 1) // CHUNK
    if "--check" in sys.argv:
        files = sorted(FONT_DIR.glob("notosc-*.woff2"))
        print("现有分片：%d（应为 %d）" % (len(files), expected))
        return 0 if len(files) == expected else 1
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    total = 0
    for i in range(0, len(chars), CHUNK):
        chunk = chars[i:i + CHUNK]
        name = "notosc-%d.woff2" % (i // CHUNK + 1)
        data = fetch_subset(chunk)
        (FONT_DIR / name).write_bytes(data)
        total += len(data)
        print("%s  %d 字  %.1f KB" % (name, len(chunk), len(data) / 1024))
    print("合计 %.1f KB" % (total / 1024))
    print("\n把下面这段放进 web/index.html 的 <style> 顶部：\n")
    for i in range(0, len(chars), CHUNK):
        name = "notosc-%d.woff2" % (i // CHUNK + 1)
        print("@font-face{font-family:'Noto Sans SC';"
              "src:url('vendor/fonts/%s') format('woff2');"
              "font-weight:400 700;font-style:normal;font-display:swap;"
              "unicode-range:%s;}" % (name, unicode_range(chars[i:i + CHUNK])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
