# Linux 部署说明

以 Ubuntu/Debian + nginx + systemd 为例。示例安装目录 `/opt/fupan_3x1`，运行用户 `www-data`，域名 `your-domain.com`。

## 1. 准备

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip nginx
sudo mkdir -p /opt/fupan_3x1 /var/log/fupan-3x1
sudo chown -R www-data:www-data /opt/fupan_3x1 /var/log/fupan-3x1
# 把仓库内容放到 /opt/fupan_3x1
```

创建虚拟环境并安装依赖：

```bash
cd /opt/fupan_3x1
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
```

## 2. 配置密钥

复制 `deploy/.env.example` 为 `data/secrets.env`，至少修改 `ADMIN_USER`、`ADMIN_PASSWORD`、`ADMIN_SECRET`，公网部署保持三项安全开关为 1。

```bash
sudo cp deploy/.env.example data/secrets.env
sudo vim data/secrets.env            # 填入你自己的强口令与随机密钥
sudo chmod 600 data/secrets.env
sudo chmod 750 data
sudo chown www-data:www-data data/secrets.env
```

> 密钥只存在于这个文件；不要写进源码、文档或提交到 Git。

## 3. 初始化数据

```bash
sudo -u www-data env DB_DRIVER=sqlite .venv/bin/python -c \
  "from backend import storage; storage.init_db()"
sudo -u www-data env DB_DRIVER=sqlite .venv/bin/python -m backend.seed_import
```

首次启动应用时也会自动做同样的事：空库导入最近 30 日种子并导出 JSON。若种子文件缺失，会回溯最近 15 个交易日（可用 `AUTO_BACKFILL_DAYS` 调整）。

## 4. systemd

把 `deploy/ashare-review.service` 复制到 `/etc/systemd/system/fupan-3x1.service`，按实际路径调整 `WorkingDirectory`、`EnvironmentFile`、`ExecStart` 后：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now fupan-3x1
sudo systemctl status fupan-3x1
```

**务必单 worker**（`--workers 1`）：调度器随进程启动，多 worker 会重复采集与通知。

## 5. nginx

以 `deploy/nginx.conf` 为参考，替换域名并放在 `/etc/nginx/conf.d/`，检查后重载：

```bash
sudo nginx -t && sudo systemctl reload nginx
```

反代必须传 `X-Forwarded-Proto $scheme` 和 `X-Forwarded-For $remote_addr`，否则后台写操作的 CSRF/限流判断会出错。

用 certbot 签发 HTTPS 证书，并开启 80 → 443 跳转。不要在公网长期以纯 HTTP 运行。

## 6. 验收

```bash
bash deploy/preflight.sh
for p in / /data/records.json /api/indices /admin/login /docs /openapi.json; do
  printf "%-22s %s\n" "$p" "$(curl -s -o /dev/null -w '%{http_code}' https://your-domain.com$p)"
done
# 期望：200 200 200 200 404 404
```

登录后台检查「概览 / 采集任务 / 数据源 / 运行日志 / 系统」五页是否正常。

## 7. 备份

```bash
bash deploy/backup.sh 7
```

备份数据库、导出的 JSON 与交易日历缓存到 `backups/`，按天轮转。建议挂 crontab 每日执行。

## 常见问题

- **后台登录后操作全部 403**：nginx 没传 `X-Forwarded-Proto`，或 `TRUST_XFF` 配置不符。
- **图表/字体 404**：先确认 `web/vendor` 目录存在（后端仅在目录存在时挂载 `/vendor`），再看 404 是 nginx 还是后端返回的。
- **后台样式正常但数据全空**：数据库初始化失败时服务仍会启动，查日志里的数据库初始化错误。
