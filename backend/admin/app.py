"""FastAPI 服务：根路径公开看板 + 可配置路径的管理后台。

面向公众部署时：
  /                    公开复盘看板（无需登录）
  /data/records.json   看板数据（公开）
  /{ADMIN_PATH}        管理后台入口（需密码登录），默认 admin

启动：
    uvicorn backend.admin.app:app --host 0.0.0.0 --port 8000

环境变量：
    ADMIN_PATH      管理后台路径，默认 admin（生产建议在 secrets.env 改成不易猜测的值）
    ADMIN_PASSWORD  管理密码（开发示例值；SECURE_MODE=1 时弱值会拒绝启动）
    ADMIN_SECRET    会话签名密钥（开发示例值；SECURE_MODE=1 时弱值会拒绝启动）
"""
import os
import time
import threading
import logging
import hashlib
import hmac
import secrets
import mimetypes
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import urlparse
from pathlib import Path

from fastapi import FastAPI, APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from backend import config, pipeline, storage, collector, scheduler, calendar_util, chart as stock_chart

# Windows 的注册表里没有 .woff2，mimetypes 会回落成 application/octet-stream；
# Firefox 等浏览器按规范要求字体必须是 font/woff2，否则拒绝使用。这里显式注册。
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("font/woff", ".woff")

log = logging.getLogger(__name__)

WEB_DIR = config.BASE_DIR / "web"
ADMIN_DIR = Path(__file__).resolve().parent

# ---------- 配置 ----------
ADMIN_PATH = os.getenv("ADMIN_PATH", "admin").strip("/")  # 管理后台目录名
# 后台只有一个账号，无角色体系；用户名可通过 ADMIN_USER 覆盖。
ADMIN_SINGLE_USER = os.getenv("ADMIN_USER", "admin").strip() or "admin"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "dev-secret-change-me")
SESSION_COOKIE = "ashare_admin"
SESSION_TTL = 8 * 3600  # 8 小时
ADMIN_BASE = f"/{ADMIN_PATH}"

# 生产安全自检：默认口令/密钥绝不允许在公网部署时生效。
# 2026-11-22 起 SECURE_MODE 默认开启：检测到弱默认直接拒绝启动，需显式 SECURE_MODE=0
# 才允许本地开发带弱值运行。占位口令 CHANGE_ME 系列同样视为弱值。
_WEAK_PASSWORDS = {"admin123", "请改成强密码", "CHANGE_ME", "ashare_pwd"}
_WEAK_SECRETS = {"dev-secret-change-me", "CHANGE_ME_TO_RANDOM_STRING"}
_WEAK_DEFAULTS = []
if ADMIN_SECRET in _WEAK_SECRETS:
    _WEAK_DEFAULTS.append("ADMIN_SECRET 仍是开发/占位值")
if ADMIN_PASSWORD in _WEAK_PASSWORDS:
    _WEAK_DEFAULTS.append("ADMIN_PASSWORD 仍是开发/占位值")
if config.DB_DRIVER == "mysql" and (config.DB_PASSWORD in _WEAK_PASSWORDS or not config.DB_PASSWORD):
    _WEAK_DEFAULTS.append("DB_PASSWORD 未设置或仍是占位值")
SECURE_MODE = os.getenv("SECURE_MODE", "1").lower() in ("1", "true", "yes")
# HTTPS 反代部署时设为 1，给会话 Cookie 加 Secure 标志；本地 http 部署保持 0
SECURE_COOKIE = os.getenv("SECURE_COOKIE", "0").lower() in ("1", "true", "yes")
# 仅在有可信反代时信任 X-Forwarded-For 作为限流 IP，否则直接用对端 IP（防伪造绕过限流）
TRUST_XFF = os.getenv("TRUST_XFF", "0").lower() in ("1", "true", "yes")
if _WEAK_DEFAULTS:
    _warn = "安全告警：管理后台仍在使用开发默认值 " + ", ".join(_WEAK_DEFAULTS) + \
            "。上线前请通过环境变量或 data/secrets.env 修改！"
    if SECURE_MODE:
        raise RuntimeError(_warn + "（SECURE_MODE=1 已阻止启动）")
    logging.getLogger(__name__).warning(_warn)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动时先做幂等建表/迁移，再拉起定时调度器，关闭时优雅停止。"""
    try:
        storage.init_db()
        stale = storage.reset_stale_runs(timeout_min=30)
        if stale:
            log.warning("启动看门狗：重置 %d 条超时未结束的采集日志", stale)
    except Exception as e:
        log.error("数据库初始化失败: %s", e)
    try:
        from backend import seed_import
        n = seed_import.ensure_initial_data()
        if n:
            log.info("首次启动已初始化 %d 个交易日数据", n)
    except Exception as e:
        log.error("首次数据初始化失败: %s", e)
    if config.ENABLE_SCHEDULER:
        try:
            scheduler.start()
            log.info("定时调度器已随应用启动")
        except Exception as e:
            log.error("调度器启动失败: %s", e)
    else:
        log.info("ENABLE_SCHEDULER=0，跳过调度器启动")
    # 悬浮卡已改成新浪静态图（浏览器直连 CDN），K 线预生成与 warm 模块已于 2026-11-21 整体下线，
    # data/charts 缓存已清理。
    yield
    if config.ENABLE_SCHEDULER:
        try:
            scheduler.stop()
            log.info("定时调度器已停止")
        except Exception as e:
            log.warning("调度器停止异常: %s", e)


# 生产关闭交互式 API 文档：openapi.json 会列出全部 40 条路由（含 30 条后台管理接口），
# 等于给攻击者一张攻击面地图。本项目前端是单文件、不依赖 openapi，直接禁用。
app = FastAPI(title="A股复盘工具", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

# GZip 压缩：HTML 220KB→~69KB、echarts 1MB→~327KB、records.json 现在只含最近 90 个
# 交易日（625KB→~129KB）。全站流量大头都在 JSON/JS/HTML 上，压缩比 3-6 倍。
app.add_middleware(GZipMiddleware, minimum_size=1024)


# 安全响应头：静态站也值得加，成本为零。
# CSP 里 script/style 仍放开 'unsafe-inline'——页面是单文件、内联样式与内联脚本，
# 收紧到 nonce 需要把整份 HTML 改成模板渲染。即便如此，这条 CSP 仍然挡住了
# 「注入一个外域 script / 把表单提交到别处 / 被嵌到别人的框架里」这几类最常见的攻击。
CSP = ("default-src 'self'; "
       # image.sinajs.cn 是悬停卡的静态图（日K/分时 gif），浏览器直连，不经本站
       "img-src 'self' data: https://image.sinajs.cn; "
       "font-src 'self'; "
       "style-src 'self' 'unsafe-inline'; "
       "script-src 'self' 'unsafe-inline'; "
       "connect-src 'self'; "
       "form-action 'self'; "
       "frame-ancestors 'self'; "
       "base-uri 'self'; "
       "object-src 'none'")


# 公开只读接口的浏览器缓存时长（秒）。都是「短缓存」：
# 盘中数据本来就要刷新，长缓存会把过期的行情钉在用户屏幕上。
_API_TTL = (
    ("/api/indices", 20),
    ("/api/index/minute", 30),
    ("/api/index/anomaly", 60),
    ("/api/stock/peek", 5),
)


# ---------- 限流 / CSRF 公共工具 ----------
def client_ip(request: Request):
    """统一取限流 IP。TRUST_XFF=1 时只信可信反代覆盖后的 XFF 最右值。

    nginx 模板用 proxy_set_header X-Forwarded-For $remote_addr 覆盖（不是追加），
    所以最右值是反代看到的真实客户端；取最左值会被攻击者伪造绕过。
    """
    if TRUST_XFF:
        forwarded = request.headers.get("x-forwarded-for", "")
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else "unknown"


_LIMIT_MAP_CAP = 20000


def _trim_limit_map(store, now, expired):
    """给内存限流表加容量上限，防止伪造 IP 无限增长。"""
    if len(store) <= _LIMIT_MAP_CAP:
        return
    for key in list(store.keys()):
        if expired(store[key], now):
            store.pop(key, None)
    while len(store) > _LIMIT_MAP_CAP:
        store.pop(next(iter(store)))


def _csrf_blocked(request: Request):
    """后台状态变更接口的同源校验：拒绝跨站/同站异源表单与 fetch。

    不带 Origin / Sec-Fetch-Site 的非浏览器客户端（curl、脚本）保持兼容；
    浏览器发起的 POST/PUT/PATCH/DELETE 必须带同源 Origin。
    """
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return False
    path = request.url.path
    if not (path.startswith(ADMIN_BASE + "/api/") or path == ADMIN_BASE + "/logout"):
        return False
    if request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
        return True
    origin = request.headers.get("origin")
    if not origin:
        return False
    try:
        parsed = urlparse(origin)
        proto = request.headers.get("x-forwarded-proto", request.url.scheme) if TRUST_XFF else request.url.scheme
        host = request.headers.get("host", "")
        return parsed.scheme != proto or parsed.netloc.lower() != host.lower()
    except Exception:
        return True


@app.middleware("http")
async def security_headers(request: Request, call_next):
    if _csrf_blocked(request):
        return JSONResponse({"detail": "跨站请求已被拒绝"}, status_code=403)
    resp = await call_next(request)
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "SAMEORIGIN")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=(), payment=()")
    h.setdefault("Content-Security-Policy", CSP)
    if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
        h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    # 公开只读接口给一个很短的浏览器缓存：指数卡片来回点、切日期再切回来，
    # 都不必每次都打到服务端（服务端各自还有 30 秒~30 分钟的进程内缓存）。
    if resp.status_code == 200:
        for prefix, ttl in _API_TTL:
            if request.url.path.startswith(prefix):
                h.setdefault("Cache-Control", f"public, max-age={ttl}")
                break
    return resp


class ImmutableStatic(StaticFiles):
    """给 /vendor、/data/hist 下的静态资源加长缓存。

    echarts.slim.js 508KB、字体子集、历史月分片（生成后不再变化），都只在升级/重导时变。
    默认 StaticFiles 只发 ETag/Last-Modified，每次刷新仍要一次 304 往返；内网多设备
    调试时这一步很扎眼。
    """

    def __init__(self, *args, max_age: int = 604800, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_age = max_age

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if resp.status_code in (200, 304):
            resp.headers.setdefault("Cache-Control", f"public, max-age={self.max_age}, immutable")
        return resp


class BoardStatic(StaticFiles):
    """看板数据目录：给一个短 TTL 而不是「只有 ETag」。

    浏览器没有明确 Cache-Control 时会做启发式缓存（各浏览器策略不一，有的干脆不缓存）。
    这里明确 60 秒：同一分钟内重复打开、多标签页切换都不用再问一次；
    超过 60 秒走 ETag 协商，没变化就是 304（几十字节）。
    """

    def __init__(self, *args, max_age: int = 60, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_age = max_age

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if resp.status_code in (200, 304):
            resp.headers.setdefault("Cache-Control", f"public, max-age={self.max_age}")
        return resp


# ---------- 公开看板 ----------
# 顺序有讲究：更具体的挂载必须写在前面，否则会被 /data 先匹配走
if (WEB_DIR / "data" / "hist").exists():
    # 历史月分片：一次生成、之后不再变化，浏览器缓存一天
    app.mount("/data/hist", ImmutableStatic(directory=str(WEB_DIR / "data" / "hist"), max_age=86400),
              name="board-hist")
app.mount("/data", BoardStatic(directory=str(WEB_DIR / "data")), name="board-data")
if (WEB_DIR / "assets").exists():
    # 图片类资产（二维码等）基本不变：1 天强缓存，省掉每次的 304 往返
    app.mount("/assets", ImmutableStatic(directory=str(WEB_DIR / "assets"), max_age=86400), name="board-assets")
if (WEB_DIR / "vendor").exists():
    app.mount("/vendor", ImmutableStatic(directory=str(WEB_DIR / "vendor")), name="vendor")


@app.get("/", response_class=HTMLResponse)
def board():
    """根路径默认显示复盘看板（公开）。

    no-cache：HTML 动态读文件、无 ETag，明确 no-cache 避免浏览器启发式缓存
    导致「发新版不生效」；gzip 后仅 ~30KB，代价可接受。
    """
    resp = HTMLResponse((WEB_DIR / "index.html").read_text(encoding="utf-8"))
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/robots.txt")
def robots_txt():
    """搜索引擎规则：允许收录看板，屏蔽管理后台与数据目录（2026-11-22 新增）。"""
    return FileResponse(WEB_DIR / "robots.txt", media_type="text/plain")


app.mount(f"{ADMIN_BASE}/static", StaticFiles(directory=str(ADMIN_DIR / "static")), name="admin-static")


# 悬停卡第一层：名称 → 新浪代码 + 最新快照（纯查表 + 一次批量报价）
# 前端拿到代码后直接去 image.sinajs.cn 取日K/分时 gif，K 线抓取与解析全部省掉
@app.get("/api/stock/peek")
def stock_peek_api(names: str = ""):
    ns = [x.strip() for x in names[:400].split(",")][:40]
    return stock_chart.peek([n for n in ns if n and len(n) <= 32])


# 悬浮个股图表路由已下线（前端改用新浪静态图直连 CDN），get_chart/预热/warm 模块一并移除。


# 大盘异动标注：上证分时跳变点 + 板块异动题材标签（公开；当日实时，历史日落库重放）
@app.get("/api/index/anomaly")
def index_anomaly_api(date: str = ""):
    from backend import anomaly
    return anomaly.get_anomaly(date)


# 核心指数：可选清单 + 实时快照（前端「设置」弹层与最新交易日卡片用）
@app.get("/api/indices")
def indices_api():
    from backend import indices
    return {"ok": True, "list": indices.meta_list(), "spot": indices.fetch_spot(),
            "default": indices.DEFAULT_KEYS}


# 单指数分时：当日实时 / 近 5 日回放 / 落库重放，按索引选择切换
@app.get("/api/index/minute")
def index_minute_api(code: str = "sh", date: str = ""):
    from backend import indices
    if code not in indices.META_BY_KEY:
        raise HTTPException(400, "未知指数代码")
    if len(date) > 10:
        raise HTTPException(400, "日期参数过长")
    return indices.get_minute(code, date)


# 手动采集任务状态（内存）
_tasks = {}
_tasks_lock = threading.Lock()


# ---------- 会话认证 ----------
# 登录失败限流：同 IP+用户名 连续失败 8 次锁定 10 分钟；第 3 位为最后失败时间，便于清理。
_login_fails = {}  # (ip, username) -> (fail_count, lock_until_ts, last_fail_ts)
_LOGIN_MAX_FAIL = 8
_LOGIN_LOCK_SEC = 600
_LOGIN_LOCK = threading.Lock()


def _login_locked(ip, username=""):
    now = time.time()
    with _LOGIN_LOCK:
        cnt, until, last = _login_fails.get((ip, username), (0, 0, 0))
        if until > now:
            return True, int(until - now)
        if cnt and (until or now - last > _LOGIN_LOCK_SEC):
            _login_fails.pop((ip, username), None)
        return False, 0


def _login_fail(ip, username=""):
    """记一次失败，返回「还可尝试次数」。触发锁定返回 0（调用方据此提示已锁定）。"""
    now = time.time()
    with _LOGIN_LOCK:
        cnt, until, _ = _login_fails.get((ip, username), (0, 0, 0))
        cnt += 1
        locked = cnt >= _LOGIN_MAX_FAIL
        if locked:
            until = now + _LOGIN_LOCK_SEC
            cnt = 0
        _login_fails[(ip, username)] = (cnt, until, now)
        _trim_limit_map(_login_fails, now,
                        lambda v, n: v[1] <= n and n - v[2] > _LOGIN_LOCK_SEC)
        return 0 if locked else _LOGIN_MAX_FAIL - cnt


def _login_ok(ip, username=""):
    with _LOGIN_LOCK:
        _login_fails.pop((ip, username), None)


def _sign(payload):
    return hmac.new(ADMIN_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _password_fingerprint():
    """当前账号密码的短哈希：密码一改，所有旧会话立即失效。"""
    blob = f"{ADMIN_SINGLE_USER}:{ADMIN_PASSWORD}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


_SESSION_REVOKED = {}  # jti -> 过期时间；登出即撤销，直到 TTL 到期
_SESSION_REVOKED_LOCK = threading.Lock()


def make_session(username):
    """token = ts.用户名.随机jti.密码指纹.签名。

    2026-11-22 加固：加入随机 jti（支持登出撤销）与密码指纹（改密即全端失效）。
    """
    ts = int(time.time())
    jti = secrets.token_urlsafe(12)
    payload = f"{ts}.{username}.{jti}.{_password_fingerprint()}"
    return f"{payload}.{_sign(payload)}"


def revoke_session(token):
    """登出时把该 token 的 jti 拉黑，避免仅删 Cookie 导致旧令牌仍可用。

    安全：先验签再拉黑。否则攻击者可伪造任意 token，把猜中的 jti 提前拉黑，
    造成管理员会话被强制失效（DoS）。伪造 token 的 jti 无法污染有效会话。
    """
    if not valid_session(token):
        return
    parts = (token or "").split(".")
    if len(parts) != 5:
        return
    with _SESSION_REVOKED_LOCK:
        now = time.time()
        for jti, exp in list(_SESSION_REVOKED.items()):
            if exp <= now:
                _SESSION_REVOKED.pop(jti, None)
        _SESSION_REVOKED[parts[2]] = now + SESSION_TTL


def valid_session(token):
    """返回用户名表示有效，None 表示无效。"""
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 5:
        return None
    ts, username, jti, fingerprint, sig = parts
    if not username or not ts or not jti:
        return None
    try:
        age = time.time() - int(ts)
        if age < -60 or age > SESSION_TTL:  # 不接受未来时间戳
            return None
    except ValueError:
        return None
    if not hmac.compare_digest(fingerprint, _password_fingerprint()):
        return None
    if not hmac.compare_digest(sig, _sign(f"{ts}.{username}.{jti}.{fingerprint}")):
        return None
    with _SESSION_REVOKED_LOCK:
        exp = _SESSION_REVOKED.get(jti)
        if exp:
            if exp > time.time():
                return None
            _SESSION_REVOKED.pop(jti, None)
    # 只认唯一在册账号（改 ADMIN_USER 后旧会话立即失效）
    if username != ADMIN_SINGLE_USER:
        return None
    return username


def require_auth(request: Request):
    if not valid_session(request.cookies.get(SESSION_COOKIE)):
        raise HTTPException(status_code=401, detail="未登录")
    return True


# ---------- 管理后台（挂载到 /{ADMIN_PATH}）----------
admin = APIRouter(prefix=ADMIN_BASE)

# 后台静态资源挂在管理路径下（Mount 不吃 router prefix，必须在 app 上挂）



@admin.get("/login", response_class=HTMLResponse)
def login_page():
    html = (ADMIN_DIR / "static" / "login.html").read_text(encoding="utf-8")
    resp = HTMLResponse(html.replace("__ADMIN_BASE__", ADMIN_BASE))
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@admin.post("/login")
def login(username: str = Form(...), password: str = Form(...), request: Request = None):
    username = (username or "").strip()
    ip = client_ip(request) if request else "unknown"
    locked, remain = _login_locked(ip, username)
    if locked:
        return JSONResponse({"ok": False, "msg": f"尝试次数过多，请 {remain} 秒后再试"},
                            status_code=429)
    # 编码成 bytes 后比较：hmac.compare_digest 对非 ASCII 字符串会抛 TypeError，
    # 既会让中文密码永远登录失败，也会形成「存在该用户名→500」的枚举 oracle。
    def _eq(a, b):
        return hmac.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))

    # 无条件计算两次比较再合并：避免 `and` 短路造成「用户名错=1次比较、对=2次」
    # 的时间差侧信道（可用于枚举唯一在册账号）。用 & 而非 and 保证两次都执行。
    u_ok = _eq(username, ADMIN_SINGLE_USER)
    p_ok = _eq(password, ADMIN_PASSWORD)
    ok = u_ok & p_ok
    if not ok:
        left = _login_fail(ip, username)
        if left <= 0:
            return JSONResponse({"ok": False,
                                 "msg": f"尝试次数过多，已锁定 {_LOGIN_LOCK_SEC // 60} 分钟"},
                                status_code=429)
        return JSONResponse({"ok": False,
                             "msg": f"用户名或密码错误，还可尝试 {left} 次"}, status_code=401)
    _login_ok(ip, username)
    resp = RedirectResponse(ADMIN_BASE + "/", status_code=303)
    # samesite=Lax 缓解 CSRF；secure 由环境变量控制（HTTPS 部署时开启，否则本地 http 登录会失效）
    resp.set_cookie(SESSION_COOKIE, make_session(username), httponly=True,
                    samesite="lax", secure=SECURE_COOKIE, max_age=SESSION_TTL,
                    path=ADMIN_BASE)
    return resp


@admin.post("/logout")
def logout(request: Request):
    """登出：撤销服务端会话，避免只删 Cookie 后旧 token 继续有效。"""
    revoke_session(request.cookies.get(SESSION_COOKIE))
    resp = RedirectResponse(ADMIN_BASE + "/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path=ADMIN_BASE)
    return resp


def _admin_page(request: Request, filename: str):
    """后台独立页面通用渲染：登录校验 + no-cache + 占位符替换。"""
    if not valid_session(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse(ADMIN_BASE + "/login", status_code=302)
    html = (ADMIN_DIR / "static" / filename).read_text(encoding="utf-8")
    resp = HTMLResponse(html.replace("__ADMIN_BASE__", ADMIN_BASE))
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@admin.get("/", response_class=HTMLResponse)
def admin_index(request: Request):
    return _admin_page(request, "overview.html")


@admin.get("/tasks", response_class=HTMLResponse)
def admin_tasks(request: Request):
    return _admin_page(request, "tasks.html")


@admin.get("/logs", response_class=HTMLResponse)
def admin_logs(request: Request):
    return _admin_page(request, "logs.html")


@admin.get("/system", response_class=HTMLResponse)
def admin_system(request: Request):
    return _admin_page(request, "system.html")


@admin.get("/api/me")
def api_me(request: Request):
    username = valid_session(request.cookies.get(SESSION_COOKIE))
    if not username:
        raise HTTPException(status_code=401, detail="未登录")
    return {"username": username}


@admin.get("/api/stats")
def api_stats(_=Depends(require_auth)):
    return storage.dashboard_stats()


@admin.get("/api/logs")
def api_logs(page: int = 1, page_size: int = 20, status: str = "",
             task_type: str = "", trade_date: str = "", src: str = "",
             _=Depends(require_auth)):
    """采集日志分页。src 按来源分组：daily/scheduler/admin/manual/backfill。"""
    return storage.list_logs(page=page, page_size=page_size, status=status or None,
                             task_type=task_type or None, trade_date=trade_date or None,
                             src=src or None)


@admin.get("/api/logs/stats")
def api_logs_stats(task_type: str = "", src: str = "", _=Depends(require_auth)):
    return storage.log_stats(src=src or None, task_type=task_type or None)


@admin.get("/api/health")
def api_health(_=Depends(require_auth)):
    return storage.list_health(limit=50)


# ---------- 数据源状态页 ----------
@admin.get("/sources", response_class=HTMLResponse)
def admin_sources(request: Request):
    return _admin_page(request, "sources.html")


@admin.get("/api/sources")
def api_sources(deep: int = 0, live: int = 1, _=Depends(require_auth)):
    """数据源注册表 + 可选实时探测 + 本地库/缓存统计。

    live=0 只读库里的健康记录，毫秒级返回；live=1 会真实请求上游（可能较慢）。
    deep=1 会并发探测全部上游，可能触发限流。
    """
    from backend import sources as src
    items = src.registry(deep=bool(deep), live=bool(live))
    locals_ = src.local_sources()
    return {
        "overview": src.overview(items),
        "sources": items,
        "locals": locals_,
    }


@admin.get("/api/system")
def api_system(_=Depends(require_auth)):
    """调度器与交易日历状态（只读监控，专家可见）。"""
    jobs = []
    try:
        if scheduler.scheduler.running:
            jobs = [{"id": j.id, "next_run": str(j.next_run_time)[:19]}
                    for j in scheduler.scheduler.get_jobs()]
    except Exception:
        pass
    return {
        "scheduler_running": bool(jobs) or scheduler.scheduler.running,
        "scheduler_enabled": config.ENABLE_SCHEDULER,
        "jobs": jobs,
        "calendar": calendar_util.cache_status(),
        "full_collect_time": config.FULL_COLLECT_TIME,
        "intraday_minutes": config.INTRADAY_POLL_MINUTES,
    }


@admin.post("/api/calendar/refresh")
def api_calendar_refresh(_=Depends(require_auth)):
    """手动刷新交易日历缓存。"""
    ok, msg = calendar_util.refresh(force=True)
    return {"ok": ok, "msg": msg, "status": calendar_util.cache_status()}


@admin.get("/api/tasks/{task_id}")
def api_task(task_id: str, _=Depends(require_auth)):
    with _tasks_lock:
        t = _tasks.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return t


@admin.post("/api/collect")
def api_collect(req: dict, _=Depends(require_auth)):
    """手动触发采集。body: {date:'YYYY-MM-DD', mode:'full'|'intraday'}"""
    if not isinstance(req, dict):
        raise HTTPException(400, "请求格式错误")
    raw_date = str(req.get("date") or scheduler.now_cst().strftime("%Y-%m-%d"))
    try:
        day = datetime.strptime(raw_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "日期格式必须是 YYYY-MM-DD")
    if not (datetime(2000, 1, 1).date() <= day <= datetime(2100, 12, 31).date()):
        raise HTTPException(400, "日期超出允许范围")
    date = day.isoformat()
    mode = str(req.get("mode", "full"))
    if mode not in ("full", "intraday"):
        raise HTTPException(400, "mode 只能是 full 或 intraday")

    # 同日期/模式已有任务在跑时直接复用，避免重复请求打爆上游
    with _tasks_lock:
        for tid, t in _tasks.items():
            if t.get("status") == "running" and t.get("date") == date and t.get("mode") == mode:
                return {"task_id": tid, "status": "running", "duplicate": True}
        if len(_tasks) >= 200:  # 任务表保留最近 200 条
            done = [k for k, v in _tasks.items() if v.get("status") != "running"]
            for k in done[:len(_tasks) - 199]:
                _tasks.pop(k, None)
        task_id = "task-" + secrets.token_hex(6)
        _tasks[task_id] = {"id": task_id, "date": date, "mode": mode,
                           "status": "running", "msg": "", "started": datetime.now().strftime("%H:%M:%S")}

    def run():
        try:
            ok, msg = pipeline.collect_day(date, mode=mode, trigger_by="admin")
            if ok:
                pipeline.export_json()
            with _tasks_lock:
                _tasks[task_id].update(status="success" if ok else "failed", msg=msg,
                                       finished=datetime.now().strftime("%H:%M:%S"))
        except Exception as e:
            with _tasks_lock:
                _tasks[task_id].update(status="failed", msg=str(e),
                                       finished=datetime.now().strftime("%H:%M:%S"))

    threading.Thread(target=run, daemon=True).start()
    return {"task_id": task_id, "status": "running"}


@admin.post("/api/health/check")
def api_health_check(_=Depends(require_auth)):
    """探测各数据源连通性，结果落库并返回。"""
    results = []
    checks = [
        ("涨停池 stock_zt_pool_em", lambda: collector.fetch_zt_pool(datetime.now().strftime("%Y-%m-%d"))),
        ("指数 index_zh_spot_sina", collector.fetch_indices_spot),
        ("市场活跃度 stock_market_activity_legu", collector.fetch_activity),
    ]
    for name, fn in checks:
        t0 = time.time()
        try:
            data = fn()
            ok = data is not None and (not hasattr(data, "empty") or not data.empty)
            latency = int((time.time() - t0) * 1000)
            storage.save_health(name, "ok" if ok else "fail", latency,
                                None if ok else "返回空数据")
            results.append({"name": name, "status": "ok" if ok else "fail", "latency_ms": latency})
        except Exception as e:
            latency = int((time.time() - t0) * 1000)
            storage.save_health(name, "fail", latency, str(e)[:200])
            results.append({"name": name, "status": "fail", "latency_ms": latency, "error": str(e)[:120]})
    return results


app.include_router(admin)
