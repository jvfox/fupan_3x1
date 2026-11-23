<div align="center">

# fupan_3x1

收盘后跑一次，涨停梯队、连板高度、题材热度和市场情绪，一页看完。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009485.svg)](https://fastapi.tiangolo.com/)
[![Live Demo](https://img.shields.io/badge/Live-Demo-brightgreen.svg)](https://fupan.3x1.cn)

[在线演示](https://fupan.3x1.cn) · [作者官网](https://www.3x1.cn)

</div>

---

## 目录

- [项目简介](#项目简介)
- [功能](#功能)
- [本地快速开始](#本地快速开始)
- [数据说明](#数据说明)
- [配置](#配置)
- [目录结构](#目录结构)
- [接口](#接口)
- [开发约定](#开发约定)
- [部署到 Linux 服务器](#部署到-linux-服务器)
- [免责声明](#免责声明)
- [许可证](#许可证)

---

## 项目简介

收盘后不想挨个网站翻数据，就想一页看全当天发生了什么——这东西就是干这个的。前端只有一个 HTML 文件，不用构建；后端是 FastAPI 加调度器，数据默认存 SQLite，也能切 MySQL。一台普通电脑就能跑。

技术栈：Python 3.11+ / FastAPI / APScheduler / SQLite（可选 MySQL）/ 原生 JS + 裁剪版 ECharts。

---

## 功能

打开根路径就是看板，四个标签页：

| 入口 | 内容 |
|---|---|
| 每日数据 | 核心指数、成交额和环比、情绪分、涨跌家数 / 涨跌停 / 炸板率 / 连板数、昨日涨停今日表现、热门题材、交易日历 |
| 涨停晋级 | 4进5 / 3进4 / 2进3 / 1进2 / 首板五档梯队，盘中显示实时进度 |
| 连板天梯 | 2 板以上的个股按连板高度分列，标出首次涨停日 |
| 概念热度 | 涨停原因按题材归堆，看家数排名，也能横向比近 N 天 |

四页共用一份 JSON，切标签只是重排 DOM，不会再发请求。指数可以自己挑 6 个、调顺序，选择记在浏览器本地。鼠标停在股票名上会弹出分时和日 K 的静态图。手机上宽表藏起来了，想看就点「电脑视图」。

后台默认在 `/admin/`，路径可以改。一个账号，五页：概览、采集任务、数据源、运行日志、系统。所有写操作都做同源校验。

---

## 本地快速开始

在自己电脑上跑，Windows / macOS / Linux 都行。需要 Python 3.11+，推荐 3.13。

### 1. 装依赖

```bash
# Windows 用户可用 py -3 代替 python
python -m venv .venv

# Windows PowerShell：
#   .venv\Scripts\Activate.ps1
# Windows CMD / Git Bash：
#   .venv\Scripts\activate
# macOS / Linux：
source .venv/bin/activate

pip install -r backend/requirements.txt
```

### 2. 设置后台账号

```bash
cp data/.env.example data/secrets.env
```

打开 `data/secrets.env`，这三行至少改掉：

```ini
ADMIN_USER=admin
ADMIN_PASSWORD=换成你自己的强密码
ADMIN_SECRET=用 python -c "import secrets;print(secrets.token_hex(24))" 生成的随机串
```

`SECURE_MODE=1` 下弱口令直接拒绝启动。纯本机调试可以改成 `SECURE_MODE=0` 和 `SECURE_COOKIE=0`，公网千万别这么干。`data/secrets.env` 已经在 `.gitignore` 里，不会被提交。

### 3. 起服务

```bash
# 建库、导入最近 30 个交易日的种子、导出前端 JSON
# 这步可以跳过，服务首次启动会自己做
python -c "from backend import storage; storage.init_db()"
python -m backend.seed_import

# 只监听本机
python -m uvicorn backend.admin.app:app --host 127.0.0.1 --port 8000
```

然后打开浏览器：

- 看板：<http://127.0.0.1:8000/>
- 后台：<http://127.0.0.1:8000/admin/>

`Ctrl+C` 停。只想看静态页面、不想让它自动采集，就把 `ENABLE_SCHEDULER` 设成 `0`。

---

## 数据说明

仓库里没有全历史行情，只有这些：

- `web/data/records.seed.json`：最近 30 个交易日的快照，用来开箱预览；
- `web/data/records.json` / `records.months.json` / `hist/`：从种子导出的前端数据。

数据库是空的时候，首次启动会自动导入种子。种子文件要是丢了，就按 `AUTO_BACKFILL_DAYS`（默认 15）从公开接口补最近几个交易日。之后调度器每天往上加，数据慢慢就攒起来了。

`records.json` 只放最近 `RECORDS_RECENT_DAYS` 个交易日（默认 30），更早的按月拆到 `web/data/hist/YYYY-MM.json`，前端翻到那边才去取。

数据源这块：涨停、连板、题材用 AkShare，指数用腾讯和新浪，分时用腾讯。有个坑要留意——非交易日上游会返回上一交易日的整段数据，所以所有「当日」接口都带日期回来做校验。fuyao 是可选的增强源，默认关着，要用得自己有 Key。

---

## 配置

配置都读环境变量，也可以写进 `data/secrets.env`，同名的环境变量优先级更高。模板在 `data/.env.example`。

| 变量 | 默认 | 说明 |
|---|---|---|
| `DB_DRIVER` | `sqlite` | `sqlite` / `mysql` |
| `DB_HOST` `DB_PORT` `DB_USER` `DB_NAME` | `127.0.0.1` `3306` `ashare` `ashare_review` | MySQL 连接 |
| `DB_PASSWORD` | 空 | MySQL 口令，没有内置默认值 |
| `ADMIN_PATH` | `admin` | 后台路径，建议改成不好猜的值 |
| `ADMIN_USER` / `ADMIN_PASSWORD` | `admin` / 仅本机开发值 | 唯一后台账号；弱值在 `SECURE_MODE=1` 下拒绝启动 |
| `ADMIN_SECRET` | 仅本机开发值 | 会话签名密钥，用随机串生成 |
| `SECURE_MODE` / `SECURE_COOKIE` / `TRUST_XFF` | `1` / `0` / `0` | 弱口令拒启 / HTTPS Cookie / 可信反代 XFF |
| `RECORDS_RECENT_DAYS` | `30` | 前端热数据保留的交易日数 |
| `AUTO_BACKFILL_DAYS` | `15` | 种子缺失时首次启动回溯的交易日数，0 关闭 |
| `PRIMARY_SOURCE` | `akshare` | 主数据源，失败自动降级 |
| `FUYAO_ENABLED` / `FUYAO_KEY` | `0` / 空 | 可选增强源，需要你自己的 Key |
| `FULL_COLLECT_TIME` | `15:30` | 收盘后全量采集时间 |
| `INTRADAY_START` / `INTRADAY_END` / `INTRADAY_POLL_MINUTES` | `09:30` / `15:00` / `1` | 盘中轮询 |
| `ENABLE_SCHEDULER` | `1` | 单 worker 部署；多实例只让一个开 |
| `REQUEST_MIN_INTERVAL` / `MAX_RETRIES` | `1.2` / `3` | 上游限流保护 |

---

## 目录结构

```text
backend/
  admin/             FastAPI 应用 + 后台 5 页
  collector.py       上游抓取（限流、重试、解析）
  indices.py         核心指数：实时 / 分时 / 日线
  indicators.py      涨停 / 连板 / 情绪分计算
  pipeline.py        采集编排与 JSON 导出
  storage.py         双驱动存储层
  scheduler.py       全量 / 盘中 / 日历三个定时任务
  seed_import.py     首次初始化（种子导入 + 兜底回溯）
  config.py          全部配置项
web/
  index.html         看板单文件（CSS / JS 全内联，无构建）
  data/              records.json + 30 日种子 + 月份分片
  vendor/            裁剪版 ECharts、字体子集
deploy/              部署脚本与 nginx / systemd 模板
tools/               字体与 ECharts 构建脚本
docs/deploy.md       Linux 服务器部署说明
```

---

## 接口

公开：`GET /`、`GET /data/records.json`、`GET /data/hist/YYYY-MM.json`、`GET /api/indices`、`GET /api/index/minute`、`GET /api/index/anomaly`、`GET /api/stock/peek`

后台（需登录）：`/api/me` `/api/stats` `/api/logs` `/api/logs/stats` `/api/health` `/api/sources` `/api/system` `/api/collect` `/api/tasks/{id}` `/api/calendar/refresh` `/api/health/check`

交互式 API 文档关掉了（`docs_url` / `redoc_url` / `openapi_url` 都是 `None`）。

---

## 开发约定

几条踩过坑才记住的：

- 密钥只放 `data/secrets.env`，别写进源码和文档；
- 单 worker。ECharts 是裁剪过的，加新图表类型要重跑 `tools/echarts-build`，否则组件会被静默裁掉；
- 日期用 `getFullYear/getMonth/getDate` 手拼，别用 `toISOString()`，UTC 会少一天；
- `display:flex` 别直接写在 `<td>` 上；后台脚本顺序是 body → `admin-common.js` → 内联脚本；
- 导出 JSON 先写临时文件再原子替换，免得并发时写到一半。

---

## 部署到 Linux 服务器

本机跑顺了再考虑上线，完整步骤在 [`docs/deploy.md`](docs/deploy.md)。几个要点：

```bash
bash deploy/preflight.sh      # 上线前自检：弱口令、缺失开关、文件权限
bash deploy/deploy.sh init    # 建 venv、建库、导种子、导出 JSON
bash deploy/deploy.sh start   # 只监听 127.0.0.1，由 nginx 对外提供 HTTPS
```

公网部署时 `SECURE_MODE`、`SECURE_COOKIE`、`TRUST_XFF` 三项都得是 `1`；worker 只开一个（`--workers 1`）；密钥文件 600、数据库 640、数据目录 750；nginx 反代要传 `X-Forwarded-Proto $scheme`，不然后台写操作会被 CSRF 拦掉。

---

## 免责声明

情绪分是程序自己算的，看看就行，别拿它做买卖决定。数据来自公开接口，延迟、缺失、口径不一致都可能遇到。

---

## 许可证

[MIT](LICENSE) © fupan_3x1 contributors
