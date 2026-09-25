# carinfo 服务器部署文档

> **目标读者**：运维 / 服务器管理员
> **目标环境**：Ubuntu 22.04 / 24.04（Debian 系同理；RHEL 系见 §8 差异说明）
> **域名**：`searchcar.eazycar.top`
> **服务形态**：常驻后台服务（systemd），开机自启，崩溃自拉起

本文档覆盖用户要求的四件事：

| # | 事项 | 章节 |
|---|---|---|
| 1 | NGINX 反向代理 + HTTPS 证书 | §4 |
| 2 | 系统级常驻服务（开机自启） | §3 |
| 3 | 每日任务机制（何时执行、是否已执行） | §5 |
| 4 | ~~GitHub Webhook 自动部署~~ **已废弃** | §6 |

---

## 0. 架构速览

这个项目在服务器上跑**两个互不干扰的进程**：

```
                       ┌──────────────────────────────┐
   公网 443 ──────────▶│  NGINX（searchcar.eazycar.top）│
                       └──────────────┬───────────────┘
                                      │ proxy_pass 127.0.0.1:8088
                                      ▼
                    ┌──────────────────────────────────────┐
                    │ carinfo-api.service                   │
                    │   智能搜索 HTTP API（FastAPI/uvicorn） │
                    │   只读，不触发抓取                     │
                    └──────────────────────────────────────┘

                    ┌──────────────────────────────────────┐
                    │ carinfo-service.service               │
                    │   爬虫调度服务（run_service.py）       │
                    │   常驻，每天随机跑一轮，写数据库       │
                    └──────────────────────────────────────┘
```

⚠️ **两个服务是完全独立的**：
- 搜索 API 挂了**不影响**爬虫调度，爬虫挂了**不影响**搜索（前者读库，后者写库）
- 搜索 API **需要** NGINX 反代（对外提供 HTTPS）
- 调度服务**不监听任何端口**，**不需要** NGINX —— 它只是往数据库写数据

---

## 1. 前置准备（一次性）

### 1.1 系统依赖

```bash
sudo apt update
sudo apt install -y git curl nginx python3.12 python3.12-venv postgresql-client
```

> `postgresql-client` 只为排查用（`psql` 连库手查）。数据库**不在**这台机器上也没关系。

### 1.2 安装 uv

项目用 [uv](https://docs.astral.sh/uv/) 管理依赖（有 `uv.lock` 锁版本，**不要**用 pip/venv 手工装）：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

装完**重新登录 shell** 或 `source ~/.bashrc`，确认：

```bash
uv --version     # 应输出 0.11.x 或更高
```

> ⚠️ uv 默认装在 `~/.local/bin`。**systemd 不会读 `~/.bashrc`**，所以 §3 的服务单元里
> 必须写**绝对路径** `/home/<部署用户>/.local/bin/uv`。这是最常见的踩坑点。

### 1.3 创建部署用户与目录

```bash
# 用独立用户跑服务（不用 root，最小权限原则）
sudo useradd --create-home --shell /bin/bash carinfo

# 代码目录（下面用 /opt/carinfo，可按实际改）
sudo mkdir -p /opt/carinfo
sudo chown -R carinfo:carinfo /opt/carinfo
```

> 📌 下文所有 `/opt/carinfo` 和 `carinfo` 用户请按实际环境替换。
> 若换路径，务必同步改 §3 的 systemd 单元与 §6 的部署脚本。

### 1.4 拉取代码

```bash
sudo -u carinfo -i
git clone <你的仓库地址> /opt/carinfo
cd /opt/carinfo
```

---

## 2. 配置（必读，**不要跳过**）

### 2.1 三个配置文件都**不在** git 里

项目 `.gitignore` 刻意忽略了这些文件（含密钥），**clone 下来是空的，必须手工创建**：

| 源模板 | 目标文件 | 内容 |
|---|---|---|
| `env.example` | `.env` | 数据库账号密码、API key |
| `config.json.example` | `config.json` | 爬取页数、调度窗口 |

```bash
cd /opt/carinfo

# .env：从模板复制后填真值
cp env.example .env
chmod 600 .env          # 含密码，收紧权限
vim .env

# config.json：从模板复制后按需调整（见 §2.2）
cp config.json.example config.json
vim config.json
```

`.env` 需要填的字段：

| 变量 | 必需 | 说明 |
|---|---|---|
| `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | ✅ | PostgreSQL 连接信息 |
| `SEARCH_API_KEY` | ✅ | 搜索 API 的请求头密钥，见 §2.2 |
| `MINIMAX_API_KEY` | 建议 | 自然语言解析用。**不填也能跑**，会自动降级为规则解析（功能不瘫，语义精度下降） |
| `SEARCH_ALLOW_NO_AUTH` | ❌ | **生产环境千万别设！** 设了等于关掉鉴权 |
| `DB_POOL_SIZE` | ❌ | 每进程连接池大小，默认 10。见 §7 容量预算 |

生成 `SEARCH_API_KEY`：

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

把结果填进 `.env` 的 `SEARCH_API_KEY=`，**同时记下来给调用方**（前端/调用程序要带这个 key）。

> 🔒 **鉴权是 fail-closed 的**：不配 `SEARCH_API_KEY` 时，服务**对任何请求都返回 503**
> —— 这是故意的，「忘记配置」应该表现为服务不可用（立刻被发现），而不是「可用但没人守门」。
> 本机调试想临时关掉才用 `SEARCH_ALLOW_NO_AUTH=1`，**绝不要用在公网部署**。

### 2.2 `config.json`

直接从模板复制，只需按需调整 `pages` 与 `schedule.windows`：

```bash
cp config.json.example config.json
vim config.json
```

完整内容（与 `config.json.example` 一致）：

```json
{
  "schedule": {
    "windows": [
      {"name": "上午", "start": "08:00", "end": "12:00"},
      {"name": "下午", "start": "12:00", "end": "18:00"},
      {"name": "晚上", "start": "18:00", "end": "22:00"}
    ]
  },
  "scraping": {
    "request_interval": [2.0, 3.0],
    "busy_handling": {
      "busy_max_retries": 20,
      "fail_streak_threshold": 10,
      "cooldown_minutes": [15, 30, 60]
    },
    "vehicle_types": {
      "1": {"name": "私家车", "pages": 800},
      "2": {"name": "客货车", "pages": 0},
      "3": {"name": "货车",   "pages": 0},
      "4": {"name": "电单车", "pages": 0},
      "5": {"name": "经典车", "pages": 0}
    }
  },
  "llm": {
    "base_url": "https://api.minimaxi.com/v1",
    "model": "MiniMax-M3",
    "temperature": 0.2,
    "timeout_seconds": 30,
    "max_retries": 2
  },
  "search": {"use_llm": true, "polish_summary": false}
}
```

**关键调参**：

| 项 | 说明 |
|---|---|
| `vehicle_types.{id}.pages` | **每轮爬取的页数**。设为 `0` 则跳过该类型。`1` 号（私家车）是主力 |
| `schedule.windows` | 每天随机挑一个窗口执行一次，详见 §5 |
| `scraping.request_interval` | 请求间隔秒数。`[2.0, 3.0]` = 2~3 秒随机。**调小会被封**，慎改 |

> ℹ️ `_comment` 字段是给运维看的说明，程序会忽略，保留着就行。
> ⚠️ 但**不要**保留 `config.json.example` 里的 `_comment` 之外的内容差异 ——
> 两份配置文件由人工同步，容易漂移（见附录 B）。

> ⚠️ `pages: 800` 的整轮耗时约 **12 小时**（实测约 2.6 秒/请求、每页 21 个请求）。
> 部署前请按实际 `pages` 估算单轮时长，并对照 §5.3 的窗口选择建议。

### 2.3 装依赖

```bash
sudo -u carinfo -i
cd /opt/carinfo
uv sync                # 按 uv.lock 精确安装，含 .venv/
```

### 2.4 验证配置（起服务前先做这一步）

```bash
cd /opt/carinfo

# 1) 数据库连通性
uv run python -c "
from dotenv import load_dotenv; load_dotenv()
from carinfo.core.proxy import check_db_connection
print(check_db_connection())
"

# 2) config.json 能被正确解析（窗口配置有没有写错，这里会报）
uv run python -c "
import sys; sys.path.insert(0,'src')
from carinfo.service import CarinfoService
svc = CarinfoService.__new__(CarinfoService)
import json; svc.windows = svc._load_windows(json.load(open('config.json',encoding='utf-8')))
for w in svc.windows: print(' 窗口:', w.label())
print('告警:', svc._startup_warnings or '无')
"
```

第 2 步若输出 `告警: [...]` 或在 `schedule.windows` 上报错，**先改对再往下走**。

---

## 3. 系统级常驻服务（systemd）

要配**两个**服务单元。都在 `/etc/systemd/system/`。

### 3.1 爬虫调度服务

创建 `/etc/systemd/system/carinfo-service.service`：

```ini
[Unit]
Description=carinfo 爬虫调度服务（每天随机窗口执行一次）
Documentation=file:///opt/carinfo/DEPLOY.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=carinfo
Group=carinfo

# ⚠️ WorkingDirectory 必不可少 —— config.json / 状态文件 / 锁文件都是相对路径，
#    没有它服务会报「配置文件 config.json 不存在」而启动失败
WorkingDirectory=/opt/carinfo

# 密钥从 .env 读（程序内部 load_dotenv()）。
# 这里只补 PATH —— systemd 不会读用户的 ~/.bashrc
Environment="PATH=/home/carinfo/.local/bin:/usr/local/bin:/usr/bin:/bin"

# ⚠️ uv 必须写绝对路径：systemd 的 PATH 里没有 ~/.local/bin
ExecStart=/home/carinfo/.local/bin/uv run --no-sync python run_service.py

# 崩溃自动重启。注意：本服务「正常工作时」会长时间 sleep 等着，
# 所以 Restart=always 与「进程存活 = 服务健康」不冲突
Restart=always
RestartSec=15

# 优雅退出：程序处理 SIGTERM 后会在「本轮任务跑完」才退出。
# 所以别把超时设太短 —— 单轮可能跑十几个小时，硬杀会留下半轮数据。
# 设为 0 = 无限等（推荐，因为任务有幂等的 last_run_date 标记，硬杀反而更麻烦）
KillSignal=SIGTERM
TimeoutStopSec=infinity

# 日志走 journald
StandardOutput=journal
StandardError=journal
SyslogIdentifier=carinfo-service

# 资源上限（按机器调整；爬虫是 IO 密集，CPU 很低）
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
```

> `uv run --no-sync` 的 `--no-sync` 很关键：防止每次重启都去检查/同步依赖（启动变慢，
> 且服务器若连不上 PyPI 会直接启动失败）。**依赖变更需手工跑 `uv sync`**（见 §6.3）。

### 3.2 搜索 API 服务

创建 `/etc/systemd/system/carinfo-api.service`：

```ini
[Unit]
Description=carinfo 智能搜索 HTTP API
Documentation=file:///opt/carinfo/DEPLOY.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=carinfo
Group=carinfo
WorkingDirectory=/opt/carinfo

Environment="PATH=/home/carinfo/.local/bin:/usr/local/bin:/usr/bin:/bin"

# ⚠️ 显式绑 127.0.0.1 —— 只允许 NGINX 反代访问，不直接暴露公网。
#    公网入口由 NGINX 的 443 提供（§4）
ExecStart=/home/carinfo/.local/bin/uv run --no-sync python -m carinfo.search.api --host 127.0.0.1 --port 8088

Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30

StandardOutput=journal
StandardError=journal
SyslogIdentifier=carinfo-api

# 本服务是网络服务，起步较晚；给足文件描述符
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
```

### 3.3 启用与开机自启

```bash
# 重载 unit 定义
sudo systemctl daemon-reload

# 开机自启 + 立即启动
sudo systemctl enable --now carinfo-service.service
sudo systemctl enable --now carinfo-api.service

# 查看状态
systemctl status carinfo-service --no-pager
systemctl status carinfo-api --no-pager
```

`systemctl enable` 做的正是「开机自启」—— 重启机器后服务会自动拉起。

### 3.4 日常运维命令

```bash
# 状态 / 启停
systemctl status  carinfo-service
systemctl restart carinfo-service      # 重启（不会导致当天重复爬，见 §5.2）
systemctl stop    carinfo-service

# 实时看日志（调试时最常用）
journalctl -u carinfo-service -f

# 看今天的日志
journalctl -u carinfo-service --since today

# 看最近 200 行
journalctl -u carinfo-service -n 200 --no-pager

# 只看告警/错误（配置写错时这里会报）
journalctl -u carinfo-service -p warning --since today --no-pager

# 搜索 API 的日志
journalctl -u carinfo-api -f
```

---

## 4. NGINX 反向代理 + HTTPS

### 4.1 确认域名解析

```bash
dig +short searchcar.eazycar.top
# 应输出服务器的公网 IP
```

**证书签发（§4.3）依赖此解析生效** —— 解析没生效 certbot 会失败。

### 4.2 配置 NGINX

创建 `/etc/nginx/sites-available/searchcar.eazycar.top`：

```nginx
upstream carinfo_api {
    server 127.0.0.1:8088;
    keepalive 16;                 # 复用后端连接，降低建连开销
}

server {
    listen 80;
    listen [::]:80;
    server_name searchcar.eazycar.top;

    # certbot 会在这里插 ACME 校验；其余全部跳 HTTPS
    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name searchcar.eazycar.top;

    # ↓↓↓ 这两行由 certbot 自动写入，先留占位，certbot 会替换/追加
    # ssl_certificate     /etc/letsencrypt/live/searchcar.eazycar.top/fullchain.pem;
    # ssl_certificate_key /etc/letsencrypt/live/searchcar.eazycar.top/privkey.pem;

    # 安全响应头
    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "SAMEORIGIN" always;

    # 访问日志（排查调用方问题用）
    access_log /var/log/nginx/searchcar.access.log;
    error_log  /var/log/nginx/searchcar.error.log;

    # 请求体上限：本 API 只有查询参数，不需要大 body
    client_max_body_size 1m;

    location / {
        proxy_pass http://carinfo_api;
        proxy_http_version 1.1;

        # keepalive 到 upstream 需要清空 Connection
        proxy_set_header Connection "";

        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # ⚠️ X-API-Key 必须透传给后端 —— 鉴权在这一层之后做。
        #    NGINX 默认就透传自定义请求头，这里显式写出来是防呆：
        #    将来若有人加了 proxy_set_header 白名单，别把鉴权头漏掉。
        proxy_pass_request_headers on;

        # 超时：LLM 解析最长约 30s（config.json 的 llm.timeout_seconds），
        # 留足余量；再长就说明后端真出问题了
        proxy_connect_timeout 5s;
        proxy_send_timeout    60s;
        proxy_read_timeout    60s;

        # 后端 5xx / 超时时不要吞掉 —— 交给调用方看到真实状态码
        proxy_next_upstream error timeout http_502 http_503 http_504;
    }

    # 健康检查单独开，方便监控直接打
    location = /health {
        proxy_pass http://carinfo_api/health;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        access_log off;
    }
}
```

启用并检查：

```bash
sudo ln -sf /etc/nginx/sites-available/searchcar.eazycar.top \
            /etc/nginx/sites-enabled/searchcar.eazycar.top

# 删掉默认站点，避免 server_name 冲突（若不想删就确保它不含本域名）
sudo rm -f /etc/nginx/sites-enabled/default

sudo nginx -t              # 必须 syntax is ok / test is successful
sudo systemctl reload nginx
```

### 4.3 签发 Let's Encrypt 证书

```bash
sudo apt install -y certbot python3-certbot-nginx
```

**先确认 HTTP 已能访问**（certbot 要用 80 端口做校验）：

```bash
curl -sI http://searchcar.eazycar.top/health   # 应返回 301 或 503，不是超时
```

> 如果此时返回 **502**，说明 NGINX 通了但后端 API 没起来 —— 先看 §3.4 的 API 日志。
> certbot 只关心 80 端口能连通，502 也能过校验。

签发：

```bash
sudo certbot --nginx -d searchcar.eazycar.top \
     --agree-tos -m <你的邮箱> --redirect --no-eff-email
```

`certbot --nginx` 会自动：
- 写入 `ssl_certificate` / `ssl_certificate_key`
- 把 HTTP 全部 301 到 HTTPS
- 安装 systemd timer 做**自动续期**（Let's Encrypt 证书 90 天有效）

**验证自动续期**：

```bash
# 查续期定时器
systemctl list-timers | grep certbot

# 干跑演练（不真续，只验证流程能通过）
sudo certbot renew --dry-run
```

### 4.4 验证

```bash
# HTTP → HTTPS 跳转
curl -sI http://searchcar.eazycar.top/ | head -3

# 无 key → 必须 401（这是正确行为，不是故障）
curl -s -o /dev/null -w "%{http_code}\n" https://searchcar.eazycar.top/health

# 带 key → 应 200
curl -s -H "X-API-Key: <你的SEARCH_API_KEY>" https://searchcar.eazycar.top/health

# 真搜一次
curl -s -H "X-API-Key: <你的SEARCH_API_KEY>" \
     "https://searchcar.eazycar.top/search?q=%E9%98%BF%E5%B0%94%E6%B3%95&limit=3"
```

> 顺带说明：`/health` 也无条件走鉴权（中间件盖住全部路由，含 `/docs`、`/openapi.json`）。
> 想外部监控健康状态就把 key 配到监控系统里，**不要**为此关掉鉴权。

---

## 5. 每日任务机制（重点核查项）

### 5.1 什么时间执行

在 `config.json` 的 `schedule.windows` 里声明**多个候选时段**：

```json
"schedule": {
  "windows": [
    {"name": "上午", "start": "08:00", "end": "12:00"},
    {"name": "下午", "start": "12:00", "end": "18:00"},
    {"name": "晚上", "start": "18:00", "end": "22:00"}
  ]
}
```

**语义：每天随机挑一个窗口，再在该窗口内随机挑一个时刻，只执行一次。**

⚠️ 三个窗口**不是**「跑三次」，是「三个候选时段」。当天随机命中其中一个。
（这正是用户要的「上午/下午/晚上随机某一个时间执行」。）

**约束**：
- 窗口必须 `start < end`，且**不支持跨午夜**（`22:00 → 06:00` 会在启动时报错）
- 窗口建议**不要重叠**（重叠不报错，但会打 WARNING，且重叠时段被选中的概率偏高）
- 改完 `schedule` 段**必须重启服务**才生效：`sudo systemctl restart carinfo-service`

**启动日志会明确告诉你窗口生效没**：

```
[2026-09-25 10:00:00] [INFO] 调度窗口：上午(08:00-12:00)、下午(12:00-18:00)、晚上(18:00-22:00)（每天随机挑一个窗口、窗口内随机一个时刻，只执行一次）
```

若窗口配置写错，**启动时就会打 WARNING 或直接启动失败**（不会静默用默认值）：

```
[2026-09-25 10:00:00] [WARNING] config.json 缺少 schedule.windows，已退回默认窗口：上午(08:00-12:00)、下午(12:00-18:00)、晚上(18:00-22:00)（建议显式写进 config.json）
```

### 5.2 「今天是否已执行」怎么判定 + 重启行为

**核心机制**：状态文件 `/opt/carinfo/.carinfo_service_state.json`

```json
{
  "last_run_date": "2026-09-25",
  "last_run_at": "2026-09-25T14:30:07+08:00",
  "next_run": "2026-09-26T14:30:00+08:00",
  "next_run_window": "下午"
}
```

| 字段 | 说明 |
|---|---|
| `last_run_date` | **上次成功跑完的日期** —— 判断「今天要不要跑」的唯一依据 |
| `last_run_at` | 上次跑完的具体时刻（排查用） |
| `next_run` | 下次计划执行时刻 |
| `next_run_window` | `next_run` 落在哪个窗口 |

**服务启动时的判定逻辑**：

```
读 .carinfo_service_state.json
  └─ last_run_date == 今天 ?
       ├─ 是 → 【跳过】只打日志「今天已执行过任务，启动后等待下一周期，不重复执行」
       └─ 否 → 【立即跑一轮】
```

**重启相关行为（用户明确关心的点）**：

| 场景 | 行为 |
|---|---|
| 当天已跑完，`systemctl restart` | **不会重复跑**，直接等下一周期 |
| 机器重启 / 断电重启 | 同上，状态文件在磁盘上，**重启不受影响** |
| 当天没跑过就重启了 | **立即跑一轮**（不是等窗口到点） |
| 跑的过程中被强杀（`stop` 超时 / OOM） | `last_run_date` **没写**（它只在整轮跑完才写）→ 重启后会**再跑一轮** |

> ⚠️ 最后一行是有意设计：`last_run_date` 只在**整轮全部类型爬完之后**写入，
> 不是一开始就写。宁可重复跑一轮（数据是幂等 upsert，不会写脏），也不要「标记已完成
> 但实际啥都没爬到」——后者会让人以为数据是新的，实际已停更。

**验证今天跑没跑**：

```bash
cat /opt/carinfo/.carinfo_service_state.json

# 或从日志确认
journalctl -u carinfo-service --since today --no-pager | grep -E "任务完成|今天.*已执行"
```

### 5.3 关于「跨夜长跑」的重要说明

`pages: 800` 的整轮任务约 **12 小时**。这意味着：

- 若命中**晚上**窗口（如 21:30），任务会一直跑到次日早上 —— 这是正常的，不中断
- 跨夜期间，服务不会在次日窗口再起第二个进程（上一轮的锁还在，且 `last_run_date`
  尚未写入 → 次日它会**立即跑一轮**，这时锁可能还占着，于是跳过并顺延重排，
  **不会并发双跑**）

**若想让单轮在窗口内跑完，两条路**：

| 方案 | 做法 | 取舍 |
|---|---|---|
| A. 缩短单轮 | 调小 `vehicle_types.1.pages`（如 `800` → `300`，约 4.5 小时） | 覆盖的车源变少（只爬前 N 页 = 最新 N×20 台） |
| B. 只保留晚窗口 | `windows` 只留 `{"name":"夜间","start":"20:00","end":"23:00"}` | 每天执行时刻不再是「上午/下午/晚上随机」，而是固定在夜间 |

> 📌 **当前推荐 A + 保留三段窗口**：因为 `pages` 是「每轮爬取页数」，只要每天跑一轮、
> 每轮都从第 1 页（最新）开始爬，站点上新的车就会被覆盖；老车靠 `update_date`
> 判定下架。所以不必坚持 800 页全量。

---

## 6. GitHub Webhook 自动部署(已废弃)

> **本节方案已废弃(2026-09-25)**:服务器端的自动部署由部署方自己的方案负责。
> 原方案(git pull + `deploy.sh` + adnanh/webhook 监听器)连同仓库里的 `deploy.sh`
> 一并移除 —— 该文件留在仓库里会被部署流程检出,与服务器侧实际方案冲突,曾导致
> 部署异常。需要原内容时从 git 历史找回(309fb53 及之前的版本)。

## 7. 数据库连接预算（加进程前必读）

检索服务的连接池是**每进程**的（`DB_POOL_SIZE`，默认 10）：

| 进程 | 池大小 |
|---|---|
| `carinfo-api` | 10 |
| `carinfo-service`（爬虫） | 自己管连接，不走这个池 |
| MCP（若部署） | 10 |

**服务端 `max_connections=100`**（实测值）。当前 2 个服务 = 20 条，余量充足。
**往服务器上加第 3、4 个检索进程前，先算 `DB_POOL_SIZE × 进程数 < 100`。**

---

## 8. RHEL / CentOS / Rocky 差异

| 项 | Debian/Ubuntu | RHEL 系 |
|---|---|---|
| 包管理 | `apt install nginx certbot python3-certbot-nginx` | `dnf install nginx certbot python3-certbot-nginx`（certbot 需先启用 EPEL） |
| NGINX 配置目录 | `/etc/nginx/sites-available` + `sites-enabled` | 没有这两个目录，直接写 `/etc/nginx/conf.d/searchcar.eazycar.top.conf`，去掉 `ln -s` 那步 |
| 防火墙 | `ufw allow 80,443/tcp` | `firewall-cmd --permanent --add-service={http,https} && firewall-cmd --reload` |
| SELinux | 不适用 | ⚠️ 需额外放行 NGINX 反代：`setsebool -P httpd_can_network_connect 1`，否则一律 502 |

---

## 9. 故障排查速查表

| 现象 | 最可能的原因 | 处理 |
|---|---|---|
| 服务起不来，日志「配置文件 config.json 不存在」 | systemd 缺 `WorkingDirectory` | 检查 §3 单元里的 `WorkingDirectory=/opt/carinfo` |
| 服务起不来，日志找不到 `uv` | `ExecStart` 用了相对路径 / `~` | 改成绝对路径 `/home/carinfo/.local/bin/uv` |
| 服务起不来，`config.json` 报 `schedule.times 已废弃` | 用了旧版配置格式 | 改成 `schedule.windows`（§2.2） |
| 服务起不来，`schedule.windows[..] 起止时间非法` | 窗口 `start >= end` | 修正窗口（不支持跨午夜） |
| 启动日志有「缺少 schedule.windows」WARNING | 配置段没写 | 按 §2.2 补上，重启服务 |
| NGINX 返回 502 | 后端 API 没起来 / 端口不对 | `systemctl status carinfo-api`；`curl 127.0.0.1:8088/health` |
| 所有请求返回 503 | **没配 `SEARCH_API_KEY`**（fail-closed） | 配 key 后 `systemctl restart carinfo-api` |
| 请求返回 401 | 少带 / 带错 `X-API-Key` | 核对调用方的 key |
| `/health` 返回 503（带对了 key） | 后端连不上数据库 | 检查 `.env` 的 DB 配置、网络、数据库白名单 |
| 搜索返回 400「候选集过宽」 | 查询条件太宽（没给车型/预算） | **不是故障**，让调用方加条件 |
| SSL 证书过期预警 | 自动续期没跑 | `systemctl status certbot.timer`；`sudo certbot renew --dry-run` |
| 每天爬两次 | 状态文件被删 / 两个服务实例 | `cat .carinfo_service_state.json`；`ps -ef \| grep run_service` |
| 爬取量明显变少 | 站点域名变了 | 更新 `src/carinfo/sites/car28.py` 的 `BASE_URL` |

**日志一把抓**：

```bash
# 最近 1 小时所有 carinfo 相关日志
journalctl -u carinfo-service -u carinfo-api -u carinfo-webhook --since "1 hour ago" --no-pager
```

---

## 10. 部署检查清单

部署时逐项打勾：

- [ ] 系统依赖装好（git / curl / nginx / python3.12 / uv）
- [ ] `carinfo` 用户 + `/opt/carinfo` 目录就位，属主正确
- [ ] 代码已 clone
- [ ] `.env` 已从 `env.example` 创建并填真值（`chmod 600`）
- [ ] `SEARCH_API_KEY` 已生成并记下（给调用方）
- [ ] `config.json` 已创建，`pages` 与 `schedule.windows` 按需配好
- [ ] `uv sync` 成功
- [ ] §2.4 的两步验证通过（数据库连通 + 窗口解析无告警）
- [ ] 两个 systemd 单元已创建、`daemon-reload`、`enable --now`
- [ ] `systemctl status` 两个服务都是 `active (running)`
- [ ] 域名解析生效（`dig +short`）
- [ ] NGINX 站点已启用、`nginx -t` 通过、`reload` 完成
- [ ] `certbot --nginx` 签发成功、`certbot renew --dry-run` 通过
- [ ] `https://searchcar.eazycar.top/health` 带 key 返回 200
- [ ] 自动部署按服务器侧实际方案验证一次 push 生效（原 §6 方案已废弃）
- [ ] 重启机器验证两个服务 + webhook 都能自启
- [ ] `cat .carinfo_service_state.json` 确认 `last_run_date` 机制在工作

---

## 附录 A：为什么服务要绑 `127.0.0.1`

后端 API 与 webhook 都只监听 `127.0.0.1`，公网只能通过 NGINX 的 443 进入。好处：

1. **单一入口**：TLS、访问日志、限流、安全头都在一处配置
2. **`X-API-Key` 走 HTTPS**：不绑本机的话，key 会以明文 HTTP 在公网传输（等于没鉴权）
3. **误暴露保险**：即使有人手滑改了后端端口，公网依然进不来

## 附录 B：Windows 上的 `config.json` 与服务器不一致怎么办

`config.json` 不在 git 里（含爬取参数，且历史上有代理配置），所以**本地与服务器是两份**。

需要同步时**手工比对**，不要依赖自动部署来覆盖配置。
若这个痛点变大，可考虑把 `config.json` 的**非敏感部分**（`schedule` / `scraping` / `search`）
抽成一个入库的 `config.default.json`，由程序做「默认值 + 本地覆盖」的合并 —— 目前没做。
