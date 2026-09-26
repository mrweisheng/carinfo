# CLAUDE.md — carinfo 项目

**语言：始终使用普通话中文汉字回复用户。代码、变量名、注释等可保留英文，但所有面向用户的文字必须使用中文。**

## 项目概述

carinfo 是一个香港汽车信息爬取与数据管理系统，从 28car.com 爬取二手车交易数据，存入 PostgreSQL 数据库。架构采用 `core/`（通用基础设施） + `sites/`（站点特定逻辑）分层，方便后续接入新站点。

## 技术栈

| 组件 | 技术 |
|---|---|
| 语言 | Python 3.12 (>=3.10) |
| HTTP 客户端 | `requests` + `curl_cffi`（TLS 指纹模拟） |
| HTML 解析 | BeautifulSoup4 |
| 数据处理 | pandas |
| 数据库 | PostgreSQL 17（psycopg2） |
| 代理管理 | 自建代理池（PostgreSQL 存储） |
| 并发 | concurrent.futures.ThreadPoolExecutor |
| 调度 | 自实现时间窗口调度器 + FileLock 互斥 |
| 配置 | python-dotenv + JSON 配置文件 |
| 搜索服务 | FastAPI + uvicorn（HTTP）、mcp 2.x（MCP） |
| 大模型 | MiniMax 国内版（`api.minimaxi.com`，M3） |
| 代码检查 | Ruff |
| 构建 | setuptools (pyproject.toml) |

## 目录结构

```
carinfo/
├── README.md
├── CLAUDE.md                       # 本文件
├── pyproject.toml                  # 项目元数据（包名 carinfo，入口 carinfo-service）
├── uv.lock                         # uv 依赖锁
├── env.example
├── .env                            # 数据库凭证（gitignored）
├── config.json                     # 爬取配置（gitignored）
├── run_service.py                  # 免安装启动入口
├── src/
│   └── carinfo/                    # 主包
│       ├── __init__.py
│       ├── __main__.py             # python -m carinfo 入口
│       ├── cli.py                  # 命令行接口
│       ├── service.py              # 调度服务（FileLock + 时间窗口）
│       ├── utils.py                # parse_price / parse_contact_info / extract_number
│       ├── core/                   # 通用爬虫基础设施
│       │   ├── __init__.py
│       │   ├── base_spider.py      # 站点爬虫抽象基类（4 抽象方法）
│       │   ├── proxy.py            # 代理池（原 proxy_manager.py）
│       │   └── importer.py         # 直接入库 import_rows()（主通道）+ CSV 手动补导（原 import_to_pg.py）
│       ├── search/                 # 智能搜索（只读，与爬虫解耦；详见下方专节）
│       │   ├── __init__.py         # 模块总览与设计铁律
│       │   ├── normalize.py        # 车名归一（四级路径）
│       │   ├── context.py          # 词表 + 库内真实键（TTL 缓存）
│       │   ├── features.py         # 重算 market_stats / vehicle_features（可执行）
│       │   ├── spec.py             # SearchSpec
│       │   ├── llm.py              # MiniMax 客户端
│       │   ├── parser.py           # 自然语言 → spec（含规则降级）
│       │   ├── engine.py           # 硬过滤 + 六维打分
│       │   ├── explain.py          # 标签与解释
│       │   ├── db.py               # 进程内连接池（自研）+ fetch() 重试
│       │   ├── auth.py             # X-API-Key 鉴权中间件
│       │   ├── api.py              # FastAPI
│       │   └── mcp_server.py       # MCP 工具
│       └── sites/                  # 各站点业务实现
│           ├── __init__.py
│           └── car28.py            # 28car.com 业务（Car28Spider）
├── _migration/                     # 迁移脚本 + 搜索子系统的验证脚本（test_*.py / eval_search.py）
├── data/
│   └── csv/                        # CSV 备份（只写不读，供排查/补导；gitignored）
└── archive/                        # 历史 dump / 调试遗留（gitignored）
```

## 核心模块与职责

### core/base_spider.py — 抽象基类

新站点继承 `BaseSpider`，只需实现 4 个抽象方法：

| 方法 | 职责 |
|---|---|
| `list_url(page)` | 构造列表页 URL |
| `detail_url(native_id)` | 构造详情页 URL |
| `parse_list(html)` | 从列表页提取所有详情页 ID |
| `parse_detail(html, native_id)` | 从详情页提取字段 dict |

基类提供 `vehicle_id(native_id)` 方法构造全局唯一 DB 主键。**28car 走特例分支**（保留 native_id 原值，因为历史数据 + 外键约束）；新站点用 `{site_name}_{native_id}`。详见方法 docstring。

HTTP 请求 / 代理 / 反爬退避 / CSV 写出 / DB 导入等基础设施**当前不在基类**——单站点凭空抽中间层是过度工程。等接入第二站、出现真实复用需求时再下沉到 `core/`。

### sites/car28.py — 28car 业务

- `Car28Spider(BaseSpider)`：28car 爬虫实现
- `BASE_URL = "https://dj1jklak2e.28car.com"` 常量（动态域名，需要时手动更新）
- `scrape_vehicle_type()` 函数：被调度器调用的入口，编排单类型车辆爬取（spider 实例跨页复用，反爬计数与动态延迟才能跨页累积）
- `build_rows()`：vehicle_id 优先取详情页「編號」，缺失时回退列表页 `h_vid`（否则主键为空会被 importer 静默丢弃）
- 列表页同时提取浏览数/留言数与结构化字段（`_parse_list_row`），详情页解析失败时用列表页字段兜底构造记录（`_build_fallback_car`，`extra_fields.list_only=true` 标记）
- 描述通用提取（写入 `extra_fields` jsonb）：hand_count 手数、mileage_km、license_until 牌費到期、import_type 行/水貨、china_plate、is_swap、view_count/comment_count
- 爬取结果逐页经 `core.importer.import_rows()` 直接入库；不再有 `auto_import_to_database()` 这一中间步骤
- 支持 5 种车辆类型：私家车(1)、客货车(2)、货车(3)、电单车(4)、经典车(5)
- 使用 `curl_cffi` 模拟 Chrome TLS 指纹
- 多编码自动检测（big5/utf-8/gbk/gb2312/latin1）
- 全局发送间隔限速：`scraping.request_interval`（默认 2-3 秒随机；数字=固定间隔，`[min, max]`=区间），并发线程与重试共用一条时间槽轴，控制对站点的总发送速率；旧的 `random_sleep`/`min_delay` 死代码已删除
- busy（站点拒绝页 `msg_busy.php`，HTTP 200）处理：换 IP 重试预算 `scraping.busy_handling.busy_max_retries`（默认 20，与网络重试预算解耦）+ 全局"连续失败链"判据——连续 `fail_streak_threshold`（默认 10）次 busy 且中间无任何成功（任何成功请求清零计数与档位）＝实证换 IP 无效，按 `cooldown_minutes`（默认 15/30/60 分钟）全局冷却；档位耗尽后再攒满一条失败链才终止本轮。冷却等待统一在发送前执行（`_wait_cooldown`），线程睡到截止时刻汇合继续，不存在"撞一次睡死一个线程"；busy 路径记代理失败、success 标记只在非 busy 成功时记（否则异步 +1/-1 抵消，代理拉黑永不生效）

### core/proxy.py — 代理池

- ProxyManager 单例（模块级全局变量 + 锁）
- 从 PostgreSQL `proxies` 表加载代理
- 健康检查：`enabled=TRUE AND is_healthy=TRUE AND fail_count<10`
- 失败超 10 次自动标记不健康
- 可用代理低于 20% 时自动重新加载

### core/importer.py — 数据导入

- DataValidator：校验 vehicle_id、价格、电话、年份、座位数（**注意：当前未被调用，接入时二选一：接入或删除**）
- ImportHistory：`import_history` 表（表结构会自动创建，`record_import` 当前未接线）
- CrawlLogManager：记录爬取统计到 `crawl_logs` 表（爬取主通道每类型记录一条）
- FastCSVImporter：批量 INSERT IGNORE + 分批 UPDATE；`import_rows(rows, type)` 是爬取主通道入口（懒连接 + 断线重连），`import_csv()` 是手动补导 CSV 的独立通道
- `record_crawl_log()`：把单类型爬取统计写入 crawl_logs
- 价格解析复用 `carinfo.utils.parse_price`

### service.py — 调度服务

- FileLock：`O_CREAT | O_EXCL | O_WRONLY` 跨进程互斥锁（`stale_after=2h` + 每页 `touch()`）
- CarinfoService：`schedule.windows` 声明多个候选时段，**每天随机挑一个窗口、窗口内随机一个时刻，只执行一次**
- `last_run_date` 写在状态文件里，是「今天已完成」的**唯一事实来源** —— 重启服务/重启机器都不重跑
- 优雅退出（SIGINT/SIGTERM）
- 启动时检查数据库健康状态

#### 调度语义（改动前必读）

```
config.json: schedule.windows = [上午, 下午, 晚上]     ← 三个「候选时段」，不是「跑三次」
状态文件:    .carinfo_service_state.json
             ├─ last_run_date   「今天已完成」的唯一依据
             ├─ last_run_at     上次完成的具体时刻
             ├─ next_run        下次计划时刻
             └─ next_run_window next_run 落在哪个窗口（可读性）
```

三条不变量（`_migration/test_schedule.py` 锁死，改调度必跑）：

1. **`_pick_random_time()` 必须返回严格晚于 `now` 的时刻。** `run_forever()` 每轮都会
   调它并写进 `next_run`；一旦可能返回过去时刻，等待秒数被 `max(1, ...)` 兜成 1 秒
   → **忙循环疯狂重排**。测试用 24h × 4 个分钟点共 96 次采样守这条。
2. **先随机选窗口、再在窗口内随机选时刻**，不能把所有窗口的可用区间拼成一条线抽签 ——
   拼线会让长窗口垄断抽签，10 分钟的短窗口几乎抽不到。测试用「10 分钟 vs 12 小时」两窗口
   各跑 2000 次守这条。
3. **今天已执行 → 一定排到明天**（`_plan_next_run(from_tomorrow=True)`），不能在今天剩余
   窗口里再排一次。
4. **「从明天起选」必须用 `_pick_random_time` 的 `start_day` 参数表达**，不能把 `now+1day`
   当当前时间传入 —— 后者用入参做 `end <= now` 过滤，**22:00 后重启会把目标日的窗口全部
   过滤掉、任务推到后天**（2026-09-25 服务器事故：22:45 重启 → next_run 落到 09-27，
   进程一次性 sleep 43 小时，09-26 全天空窗；白天重启只是巧合落在正确的一天，且被限制
   在晚上窗口）。测试 [12] 用 22:00/22:45/23:03 三个事故时刻锁死。

**没有窗口能安排未来时刻时会打 ERROR 并退回 1 小时后**（理论上不可达 —— 只要所有窗口
`end > start`，明天的全部窗口必然可用；真到了这里说明窗口配置全烂）。

#### 深扫与日常（2026-09-26 起）

每次任务触发（含启动即跑）由 `_resolve_job_mode()` 二选一，优先级：**未完成的深扫续跑 >
到期的新深扫 > 日常**。

- **日常**：每类爬 `vehicle_types.*.pages`（30 页 ≈ 当天全部更新，调研实测 26.9 页/天）。
- **深扫**：每 `deep_crawl.interval_days`（30 天）触发一次，每类爬到 `deep_crawl.pages`
  （1200 页，覆盖约 90% 在售），**替代当天日常**（深扫从第 1 页开始，天然覆盖活跃区）。
  双目的：补深处个人车源（车行霸占前排持续刷新，个人车沉底）+ 僵尸清理（长期未验证的
  「在售」行只有重爬才能改状态）。依据：2026-09-25 页数策略调研（`_migration/`）。
- **进度与续跑**：状态文件 `deep_crawl.progress[type] = {target, last_page}`，`page_hook(page)`
  每页开始时写入「page-1 已完成」。被反爬/重启打断 → 保留进度，次日触发自动从断点续跑
  （半页重做，绝不跳页）。全部类型到目标页 → `last_completed = 今天`、清进度。
- **提速**：深扫用独立的 `deep_crawl.request_interval`（[1.0,1.5]s，比日常紧一倍 → 约 9 小时
  跑完 1200 页，当天窗口内可完成）。安全边际：IP 每请求轮换（单 IP 命中率不变）、busy 由
  连续失败链自动冷却兜底、断点续跑保证中断无损。日常间隔仍是 [2.0,3.0]。
- **page_hook 签名已改为 `hook(page)`**（原无参）——与已知问题 10 的 FileLock touch 耦合，
  改一处要看另一处。

#### 配置解析的三条硬约定

- **`schedule.times` 已废弃，出现即抛 `ValueError`。** 旧格式 `["08:00","12:00","16:00","20:00"]`
  看着像「四个候选时刻」，代码却只读 `[0]`/`[1]` 当窗口起止 —— 后两个**静默丢弃**，而版本之间
  还换过含义。这种「配置写了但不生效还不报错」是运维事故的温床，所以换字段名并强制报错。
- **`_parse_hm()` 非法值返回 `None` 并告警**，不再像旧版 `_parse_time()` 那样静默返回 8:00
  —— 把 `"8:00-18:00"` 这种写错的字符串悄悄当成 8 点，人根本发现不了。
- **告警走 `self._warn()` 攒到 `_startup_warnings`，由 `run_forever()` 开头 flush。**
  为什么不在 `_load_config()` 里直接 print：CLI 是「构造 `CarinfoService` → 调 `run_forever()`」，
  `__init__` 期间打印的话，运维在 systemd 启动日志里的**同一时刻**也能看到，但**顺序会插在
  「服务已启动」之前而非之后**，容易和启动 banner 混在一起；更重要的是以后若有人给
  `__init__` 加 dry-run/校验模式，直接 print 会污染输出。攒着统一 flush 是唯一稳的写法。

#### 窗口配置的边界

- 窗口**不支持跨午夜**（`22:00 → 06:00` 会被 `start < end` 校验拦下）。单轮任务本身可能跑
  十几个小时、跨夜是常态 —— 它继续跑完当前这轮，但调度器不会在窗口外再起新一轮。
- 窗口**重叠不是错误**（随机选窗口的语义下重叠只是让重叠区命中概率变高，逻辑仍自洽），
  **但会打 WARNING** —— 运维配出重叠多半是压错了，提醒一句比默不作声好。
- `default` 三段窗口是 `DEFAULT_WINDOWS`（08-12 / 12-18 / 18-22）。缺 `schedule.windows`
  时**用它 + 打 WARNING**，不静默。

### 数据流

```
config.json + .env
    │
    ▼
service.py (run_service.py / python -m carinfo)
    │
    ▼
sites/car28.py:scrape_vehicle_type()
    │ 通过 core/proxy.py 取代理
    ├──────────────────────────────┐
    ▼                              ▼
data/csv/car_data_{type}.csv   core/importer.py:import_rows()（逐页直接入库）
（只写不读的备份，供排查）           │ 统计经返回值内存传递
                                   ▼
                               PostgreSQL (mycar) + crawl_logs
                                   │
                                   ▼（离线重算，与爬虫解耦）
                      search/features.py:重算 market_stats + vehicle_features
                                   │
                                   ▼
              search/{parser,engine,explain} → search/api.py / search/mcp_server.py
```

## 智能搜索子系统（`src/carinfo/search/`）

在既有爬取数据之上做自然语言检索 + 性价比排序。**与爬虫完全解耦**：只读数据库，
不触发任何抓取；搜索服务挂了不影响调度，反之亦然。

### 模块与职责

| 模块 | 职责 | 关键约束 |
|---|---|---|
| `normalize.py` | 自由文本车名 → 稳定车系键 | 四级路径 exact/compact/token/fallback；词表脏条目必须过滤 |
| `context.py` | 词表 + 库内真实键集合 | 5 分钟 TTL 缓存；`reset()` 在特征表重算后调 |
| `features.py` | 重算两张派生表 | 先 market_stats 再 vehicle_features（价格比依赖中位数） |
| `spec.py` | `SearchSpec`：检索条件唯一表示 | `from_dict` 只认白名单字段，防模型幻觉字段穿透 SQL；`*_near` 是软偏好，不进 `build_query` |
| `llm.py` | MiniMax 国内版客户端 | **失败也返回 HTTP 200**，错误码在 `base_resp.status_code` |
| `parser.py` | 自然语言 → SearchSpec | **键别名归一 + 白名单 + 模糊量守卫**三保险（prompt 只是软约束）；无 key 走规则解析 |
| `engine.py` | SQL 硬过滤 + 六维打分 + TopN | 缺维权重归一；所有值走占位符；软偏好绝不进 SQL |
| `explain.py` | 标签 + 人话解释 | 模板给事实，模型只润色；**每个数字都要能核对** |
| `db.py` | 进程内连接池 + `fetch()` 重试 | **自己写的池**，不用 `psycopg2.pool`（三条实测缺陷，详见「连接池」一节）；只读，归还前一律 rollback |
| `auth.py` | `X-API-Key` 请求头鉴权（中间件） | 三态 **fail-closed**；API 与 MCP-over-HTTP 共用同一中间件 |
| `api.py` | FastAPI 薄壳 | `/search`（NL）、`/search/spec`、`/vehicle/{id}`、`/models`、`/health` |
| `mcp_server.py` | MCP 薄壳（4 工具） | mcp 2.x：`from mcp.server.mcpserver import MCPServer` |
| `extract.py` | LLM 描述字段提取（存量+增量） | 见下方「LLM 字段提取」专节；M3 **关思考**跑，四道闸，merge-only |

### LLM 字段提取（`extract.py`，2026-09-25 上线）

28car 描述是粤语/繁简/黑话自由文本（「0字/1字」=N手、「未出牌」=0手、「低咪5萬」=5万公里、
「換左全車喇叭」=换了音响**不是**换车），正则天花板 hand_count 34.5%。改用 M3 提取：

- **关思考**：`thinking.type=disabled`（实测生效；`reasoning_effort`/`enable_thinking`
  两种写法 MiniMax 静默忽略）。模式转换任务不需要推理，快数倍、省 ~70% token。
  `LLMConfig.disable_thinking` 开关，只有 extract 用，parser/explain 保持思考开启。
- **四道闸**（全部实测，缺一不可）：白名单+类型范围收敛 → evidence 原文逐字锚定
  （防幻觉核心：每个值必须附原文片段，锚不到就丢）→ 语义哨兵（is_swap 的 evidence
  必须「換車|swap」、china_plate 必须「中港|兩地」——换零件的「換」永远过不了）→
  **merge-only 写库**（只补 null 键，绝不覆盖已有值；打 `llm_extracted_at` 标记）。
- **候选 = 未打标的在售车**（不是「缺字段的车」——提取过但值为 null 的描述里真没写，
  不能永远重跑）。失败批不落标，下轮自动重试（自愈）。
- **增量接线**：`service._run_one_job` 每轮爬完、写完 `last_run_date` **之后**调
  `run_incremental()`——放后面的原因：提取失败不能阻止完成标记（否则明天重爬整轮）。
  `run_incremental` 尾部**无条件重算特征表**（约 8 秒，原子切换不断服）：爬虫改价/
  新车上架/LLM 补字段都改 vehicles，搜索 JOIN 的 vehicle_features 是派生表，
  这是全链路唯一一处重算触发点，两个数据源一次覆盖；未配 MINIMAX key 也要重算
  （爬虫更新的 age_days/价格比同样需要它）。远程 API 的词表缓存靠 5 分钟 TTL
  自然过期，不依赖 reset。
- **importer 必须 merge extra_fields**（`{**old, **new}`，新值优先旧值补缺）：
  爬虫重爬老车时，正则提不到 LLM 补过的键，不合并会整个覆盖丢失。
- 跑法：`uv run python -m carinfo.search.extract --dry-run | --limit N | --fallback`。
  提取后**必须重算特征表**（condition/is_anomaly 依赖 hand_count/mileage_km/import_type）。

### 解析层：模型输出的两道防线（`parser.py`）

模型输出的键名**不能只靠 prompt 约束**。踩过的坑：`SYSTEM_PROMPT` 点明了 `brand 字段`，
却没给车型的键名，M3 就自己写成 `model` → 被白名单当幻觉字段丢掉 → 「搜阿尔法」退化成
全库扫描（实测 5 次里 4 次如此，是**偶发**的，抽检能过、生产会挂）。所以：

1. **`normalize_llm_keys()`** —— 过滤**之前**把同义键名换成规范键（`model`/`car_model`/
   `model_name` → `base_model`，`car_brand`/`brand_name`/`make` → `brand`，…）。规范键优先，
   不被同义键覆盖。
2. **`ALLOWED_LLM_FIELDS` 白名单** —— 过滤掉模型多给的字段，防幻觉字段穿透到 SQL。

两条都过完还拿不到任何字段 → 退回 `rule_based_parse`，并**在 notes 里写明真实的原生键名**，
方便定位是"模型不听话"还是"词表缺词"。

`ParseResult.raw_llm` 存的是**模型原生输出**（不过白名单）—— 排查时能看到模型到底说了什么。
**别改成存过滤后的 dict**，那会让这类 bug 无法诊断。

第三条防线在**值**上而不是键上：**模糊量守卫**（`_rule_near_anchor()`，见「模糊量」一节）。
关键名对了、值却把"五十万左右"写成硬区间，同样会静默砍掉候选池 —— 只是评测的 P@5 和
违例数都看不出来。

### 返回条数口径

**单点在 `spec.DEFAULT_LIMIT = 5` / `spec.MAX_LIMIT = 50`。** 别处不许各写一个数
（曾经 spec=20、config=20、MCP=10 三处不一致，config 里那份还是没人读的死配置）。
`test_serving.py` 有断言锁住 spec 与 MCP 签名默认值一致。

### 设计铁律

**大模型只做两头 —— 理解输入（parser）、润色输出（explain）。中间打分全是确定性算法。**
同样的输入 + 同样的库 → 永远同样的排序，每一分可解释、可回归、能加评测门禁。
绝不让模型对候选打分（不可复现，也无法调试）。

### 打分口径（`engine.WEIGHTS`，2026-09-25 第二次定标）

```
匹配 0.28 | 性价比 0.28 | 贴合度 0.08 | 时效 0.08 | 车况 0.20 | 热度 0.08
```

定标依据是外部审核报告（docs/搜索排序审核报告.md）的 D-1/D-2，**旧的
「前五维 × 0.8 等比 + near 0.20」口径已废弃**（等比缩放只保证无锚点查询不变，
没有为 0.20 提供依据）：

- **near 0.20→0.08**：0.20 与性价比等权让「软偏好」退化成准硬过滤 —— 实测说
  「50万左右」后捡漏车从第 2 名掉到 848 名、Top10 换血 0/10。boost 必须显著低于
  value（test_search [9] 锁 near ≤ 0.10）。修复后捡漏车回升到 318 名、Top10 恢复交集，
  且锚点 Top3 仍贴近 50 万（贴合度还在工作，只是不再碾压）。
- **fresh 0.16→0.08**：age_days 量的是「上次被爬到时的站点更新时间」，不是挂牌时长
  （断层 21-161 天 0 台、54% 的车时效分 ≤0.45）。降权止血；根治要改爬虫抓真实
  挂牌日（审核方案 A），后议。
- **condition 0.12→0.20**：车况覆盖已从 28.6% 升到 81.6%（LLM 字段提取），
  降权理由消失；且 D-4 修复后口径更干净。
- value 0.20→0.28：说「50万左右」想要的仍是「这个价位里性价比最高的」。
- 性价比锚点：中位价（ratio=1.00）得 0.70 分而不是 0.5 —— 与同款同价是正常交易。
  **U 形下限（D-3 修复）**：ratio ≤0.60 不再一律满分 —— 便宜 70%+ 反而降到 0.75
  （「便宜得可疑」先打问号，与「⚡ 需核实车况」标签语义一致）；峰值仍在 0.60。
- **缺维从分母去掉**（不是记 0 分）。记 0 分等于给缺数据的车无差别扣分，
  排序会被"数据齐全度"主导。无车型目标时匹配维整维缺席，不给常数分。
- 车况子项权重（features.py）：hand 0.50 / mileage 0.40 / import 0.10（import 从
  0.20 降，且**只有行貨单项时封顶 0.60** —— 「行貨」两个字不构成车况证据，
  修复前 1,000 台凭它拿满分）。
- `test_search.py [9]` 锁权重设计值 + near/fresh ≤0.10 两条硬约束，改权重先过它。

### 模糊量：「50 万左右 / 2015 年左右」（`*_near` 软锚点）

**「左右」是偏好，不是约束。** 它只产出 `price_near` / `year_near` 两个软锚点，参与
第 6 维「贴合度」打分，**绝不产生 `price_min/max`，也不进 `build_query()`**。

为什么不做成区间：硬切 ±15% 会把 39 万、62 万的车**直接从结果里删掉**，而用户的本意
只是"离 50 万近的排前面"——删掉是不可逆的损失。Google Cloud 自然语言查询理解的官方
做法同样把这类条件归入 soft filter（boost）而非 hard filter，理由是**硬过滤在排序之前
就砍掉候选池，排序无法挽回**。

容差口径 —— **价格按比例，年份按绝对年数，别统一成一种**：

| 锚点 | 贴合带（给满分） | 衰减到 0 |
|---|---|---|
| 价格 | 比锚点贵 ≤10% / 便宜 ≤20%（**上下不对称**） | 贵 35% / 便宜 70% |
| 年份 | ±2 年 | ±6 年 |

- 价格 15% 这一档是**实测常数**：Monroe(1971) 让 240 名消费者判断"两个价格是否相同"，
  参考价 $10 的阈限 $1.5、$100 的 $15、$1,000 的 $150 —— 恒定 15%，即 Weber–Fechner
  定律 ΔI/I=const。所以「10 万左右」的容差是 1.5 万而不是 15 万，**不能做成固定金额区间**。
- 上下不对称（贵了更敏感）出自 latitude of price acceptance / 前景理论，是文献的**定性**
  结论；10% / 20% 这两个具体数字是本项目的**工程取值，可调**。
- 年份是**等距标度**：2015 和 1995 的"左右"都是 ±2 年（约同一代车型 / facelift 的跨度），
  不按数值比例放大。

两条路径都必须遵守，`parser._rule_near_anchor()` 是共同裁判：

1. **规则路径** —— 金额/年份附近命中「左右 / 大约 / 大概 / 前后 / 约」→ 只写 `*_near`。
   明确边界词（以内 / 以下 / 打後…）优先级更高：两者同时出现时按硬边界走。
2. **LLM 路径** —— `_rule_near_anchor()` 会**交叉校验模型输出**：
   - 原文是模糊量、模型却给了 `price_max` → 丢掉区间、补上锚点
     （**原文 > 模型的写法选择**）。模型对"左右"的写法不可信，它随时可能把
     "五十万左右"写成 `price_max: 500000`，那就是硬过滤。
   - 原文**同时**有明确边界词（"五十万左右，不要超过八十万"）→ 区间是真的，保留；
     锚点照样补上。**两者共存是对的，不是矛盾。**

> ⚠️ `_MAX_WORDS` / `_MIN_WORDS` 的顺序**不能反**：`_MAX_WORDS` 必须先判。
> "不要超过三十万" 同时含「不要超过」（上限）和「超过」（下限），先判上限才对。
> 另外「超过」单独出现是下限（"超过三十万" = 三十万以上），所以上限表里只能放带否定
> 词的形式（不要超过 / 不得超过 / 唔好過），裸的「超过」进下限表。

`test_parse.py [1d]` 用**假模型注入离线**覆盖了第 2 条（不烧 token）；`[1e]` 断言「左右」
不减少候选。`eval_search.py` 里「左右」用例的 `checks` 必须是空的 —— 一旦被迫加上边界，
就说明它退化成硬过滤了。

### 行情基准（`features.py`）

两套键，**这是实测得出的结论，别合并**：
- 匹配键（宽）：`base_model` —— 搜"阿尔法"要能出 ALPHARD / ALPHARD 3.5 / ALPHARD 3.5 M
- 行情键（细）：`(base_model, year_bucket)` —— 实测 ALPHARD 按年代差 10 倍
  （2023+ 中位 59.8 万 vs 2008-2014 中位 5.8 万），而排量只差 21%。
  **年份段是硬约束，排量不是。**

三级降级链：`bucket`（本年份段）→ `near`（邻近 ±5 年段，取年份距离最近的，
同距取更老的那档以保守）→ `model`（全年代中位，标注可信度低）。
`market_level` 字段如实存降级档位，解释层会照实说 —— 不能让用户以为每个价格比都同样可靠。

### 特征表

两张**纯派生表**，随时可由 `vehicles` 重算。`ensure_tables()` 用 DROP + CREATE
（不是 IF NOT EXISTS），改结构时才不会静默保留旧列。

```bash
uv run python -m carinfo.search.features            # 重建并全量重算（约 5 秒）
uv run python -m carinfo.search.features --dry-run  # 只算不写，打印分布
```

### 验证与评测（改 search/ 后必跑）

```bash
uv run python _migration/test_normalize.py   # 归一：10 项断言（含反向断言防误杀）
uv run python _migration/test_search.py      # 内核：过滤正确性 + 确定性 + 缺维归一 + 权重等比锁/无锚点等价性
uv run python _migration/test_parse.py       # 解析：规则用例 + 「二手」泛指 + LLM 键归一/模糊量守卫(假模型注入) + 排量边界 + spec 脏值收敛 + 端到端
uv run python _migration/test_serving.py     # 解释层/API/MCP + 鉴权三态(含真跑 HTTP) + 连接池并发/重试 + LLM 不占连接 + 扫描护栏 + /health 降级 + ILIKE 转义 + total_matched + 特征表原子重算
uv run python _migration/test_importer.py    # 入库：FileLock.touch 原子续命 + list_only 兜底行不覆盖详情字段（写操作全回滚）
uv run python _migration/test_schedule.py    # 调度：窗口解析/告警 + _pick_random_time 未来性(防忙循环) + 窗口命中分布 + 过期重排 + 重启不重跑
uv run python _migration/eval_search.py              # 门禁：规则 + LLM 两种模式**分别**判定
uv run python _migration/eval_search.py --mode rules # 只验规则路径
uv run python _migration/eval_search.py --mode llm   # 只验模型路径（缺 key 直接失败，不降级）
```

> 改 `service.py` 的调度逻辑必跑 `test_schedule.py`（31 项断言，离线、不起服务、不碰数据库）。
> 它守的三条不变量见上文「service.py — 调度服务 / 调度语义」。

### 本轮审核修复的硬约定（2026-09-25）

**白名单只挡「键」，挡不住「值」。** `ALLOWED_LLM_FIELDS` 只管字段名，值可以是任何东西。
模型/调用方给 `seats='七座'`、`year_min='2015年'`、`hand_max=True` 这类脏值时，旧实现会
原样绑进 SQL 参数 → PostgreSQL `InvalidTextRepresentation` → **500**。现在 `SearchSpec.__post_init__`
里所有数值字段走 `_coerce_int/_coerce_float`，收敛不了置 `None`（=该维度不参与过滤）。
**`vehicle_type` 是唯一例外**：收敛不了**不能置 None** —— 那等于放开全车型，「最便宜的车」
会返回电单车；必须退回默认 `1`（私家车）。

**`extra_fields` 的两种形态都要认。** 爬虫内部直传的是 **dict**（`{'list_only': True}`），
CSV 回放才是 JSON 字符串。旧实现写死 `json.loads()`，传 dict 抛 `TypeError` 被裸
`except: pass` 静默吞掉 → `extra_fields` 变 `None` → **B-1 的 list_only 守卫从未生效过**
（兜底行照旧全字段 UPDATE，把老车的 description 冲成空）。这是"测试用 dict、代码只认 str"
两边契约没对齐的典型；`test_importer.py [2]` 现在带**对照断言**（正常详情行必须走全字段
更新），防止"什么都没写"造成的假绿。

**「连接只包住 SQL，不包住网络调用。」** `parse_query` 会调 MiniMax（最长 30s×3 重试）。
若整段包在 `with db()` 里，一次请求就有一条池连接被网络 IO 占着 —— 池只有 10 条、排队
5 秒，十个并发慢查询就能把池占满，后续请求全部 `PoolBusy`→503，而真正要 SQL 的部分不到
1 秒。`_do_search_nl` / `_do_search_cars` 现在是**分三段各自短借**（取上下文 / LLM / 检索）。
`test_serving.py [9]` 用慢模型打桩，断言调用期间 `in_use` 恒为 0。

**`ScanTooWide` → 400，不是 500。** 不带收窄条件的查询会扫全表，表现是"请求一直不返回"。
加 `MAX_SCAN_ROWS` 硬上限 + 业务异常 → 400，让"参数太宽"和"服务崩了"可区分。

**`total_matched` 用 `COUNT(*) OVER ()` 拿真实候选总数**，与 `len(items)` 语义分开。
旧实现拿被 LIMIT 截断的 `len(rows)` 当总数，用户永远看到"共 5 条"。

**ILIKE 的 `%` / `_` 必须转义** + `ESCAPE '\'`（反斜杠要最先转）。`test_serving.py [12]`
断言 `model_keyword='%'` 匹配 0 条 —— 不转义就是全库。

**`FileLock.touch()` 让 `stale_after` 只跟单页耗时挂钩。** 单轮 12 小时远超任何合理的
stale 阈值，靠固定阈值扛不住跨夜；现在 service 每页开头 `lock.touch()`（tmp + `os.replace`
原子替换），阈值取 2 小时 = 单页耗时的百倍余量。⚠️ 若把 `touch()` 调用删掉（或 page_hook
没接上），阈值必须调回 > 12 小时。

**特征表重算接在「每类型跑完」而不是「整轮跑完」，且必须是原子的。**
`scrape_vehicle_type` 尾部在 `status=='success'` 且有新数据时调
`_rebuild_features_after_crawl()`，全量约 **7 秒**。失败只告警 —— 派生数据晚一轮
不影响爬虫正确性。`features.main(argv=[])` 支持传参，连库失败返回 2 而不抛栈。

⚠️ **重算不能「DROP 正式表 + CREATE + 灌数据」** —— engine 是 `JOIN vehicle_features`，
那张空表窗口期内**所有搜索都返回 0 条**。实测并发 4 路跑一次重算，`total_matched`
取值集合是 `[0, 2030]`。这是**静默空结果**，比报错更难发现：前端不异常、监控不报警，
用户只看到"没搜到车"。现在改成三步：
1. `ensure_tables()` 建**影子表**（`*_new`），正式表全程可读；
2. `write_all()` 灌影子表；
3. `swap_tables()` 单事务 `DROP 正式表 + RENAME 影子表`，搜索要么旧表全量、要么新表全量。

`swap_tables()` 的两条护栏（都踩过）：
- **影子表为空时拒绝切换** —— 否则"上游算出空结果"会变成"把正式表换成空表"。
  派生表宁可保持旧值，不能被清空。
- **必须 `SET LOCAL lock_timeout='5s'`** —— 这里要 `ACCESS EXCLUSIVE`，而搜索请求
  持有读锁；若恰有长事务，`LOCK` 会**无限期挂住**。实测 5.3s 退让且正式表无损。

影子 DDL 由 `_shadow_ddl()` 从 `CREATE_SQL` **机械派生**，用**一次性正则**
替换 `TABLE/INDEX/REFERENCES/ON` 之后的标识符。别用链式 `str.replace`（踩过两次）：
索引名 `idx_market_stats_bucket` 里嵌着表名，表名替换会二次命中，产生
`idx_market_stats_new_bucket_new` 这种垃圾；**`ON` 这一支漏了更严重** ——
`CREATE INDEX ... ON vehicle_features` 不替换就会把索引建到正式表上（名字却带 `_new`），
随后 `DROP` 正式表把业务索引一起带走。`test_serving.py [14]` 断言了
「业务索引齐全 + 无 `_new` 残留 + 重算期间零空结果 + 空影子表被拒」。

**配置读取不许静默退默认值。** `_load_scraping_config()` 现在 `config.json` 不存在 /
JSON 语法错 → **直接抛**（反爬参数静默失效 = "程序照常跑但拦不住反爬"，最危险的一类故障）；
只有"缺 `scraping` 段"才退回代码默认并显式 warning。


**评测必须锁定被测模式 —— 这条是血的教训。** 脚本曾在 `.env` 没配 key 时静默降级成规则
解析，门禁照跑照绿；LLM 主路径一次都没被覆盖过，于是"车型键名对不上被白名单丢掉"这个
P0 从评测底下溜了过去，还被写进了交付总结。现在 `--mode llm` 缺 key **直接报错退出**，
不给假绿；默认 `--mode both`，两种模式分别出判定，不混着算。

`eval_search.py` 是**回归门禁**，不是人类相关性评测：评分基于硬事实（车系/预算/年份/
座位），主要价值在**违例计数**（抓过滤失效）与**空结果计数**（抓解析过严）。
P@5/NDCG@5 会饱和到 1.0（评分口径与 SQL 过滤同源），故额外报了"排序倾向"指标 ——
前 5 条的价格比均值须优于池内整体，这一项不受 SQL 过滤影响，是真正的排序信号。

### 配置

**非敏感调参**在 `config.json`：

```jsonc
"llm": {
  "base_url": "https://api.minimaxi.com/v1",        // 国内版！海外版是 api.minimax.io
  "model": "MiniMax-M3",
  "temperature": 0.2                                 // MiniMax 拒绝 0
},
"search": { "use_llm": true, "polish_summary": false }
```

**凭证只在 `.env`**，跟数据库凭证同一个文件，`config.json` 里不放任何 key：

```bash
MINIMAX_API_KEY=sk-xxxxxxxx        # MiniMax 国内版；不填则降级为规则解析

SEARCH_API_KEY=xxxxxxxx            # 检索服务请求头鉴权；生成见下
SEARCH_ALLOW_NO_AUTH=              # =1 时彻底关掉鉴权（只给本机调试）
DB_POOL_SIZE=10                    # 可选；每个服务进程的连接池大小，上限 50
```

生成一把服务密钥：

```bash
python -c "import secrets;print(secrets.token_urlsafe(32))"
```

读取链路：`config.load_api_key()` ← `os.environ["MINIMAX_API_KEY"]` ← `.env`（由 `config` 模块
导入时自动 `load_dotenv()` 灌入）。`LLMConfig.from_dict()` 不认 `api_key` 字段。

### 启动搜索服务（可选，与爬虫调度互不干扰）

```bash
uv run python -m carinfo.search.api                        # HTTP，默认 127.0.0.1:8088
uv run python -m carinfo.search.api --host 0.0.0.0 --port 8088

uv run python -m carinfo.search.mcp_server                 # MCP (stdio，默认)
uv run python -m carinfo.search.mcp_server --transport streamable-http --port 8000
```

两个入口都**自己 `argparse` + `uvicorn.run()`**，不再依赖外部 `uvicorn` 命令行 ——
默认绑 `127.0.0.1`（要公开必须显式 `--host 0.0.0.0`，多一道手动确认）。MCP 传输方式
也可由 `MCP_TRANSPORT` 环境变量给默认值。

### 鉴权（`auth.py`）—— 三态 fail-closed

| 情况 | 行为 |
|---|---|
| 配了 `SEARCH_API_KEY` | 每个请求必须带 `X-API-Key`，不匹配 → **401** |
| 没配，但 `SEARCH_ALLOW_NO_AUTH=1` | 全部放行（本机调试用，启动打 `log.warning`） |
| 没配，也没开关 | 全部 **503**，并返回怎么配的提示 |

三条实现上的要点，别改回去：

1. **默认拒绝，不默认放行。** 库里是真实车源和报价，"忘记配置"应当表现为**服务不可用**
   （立刻被发现），而不是"服务可用但没人守门"（悄无声息地裸奔）。
2. **用纯 ASGI 中间件，不用 FastAPI 的 `Depends`。** `Depends` 只盖住 path operation，
   `/docs`、`/openapi.json` 这类自动生成路由会**漏在外面**；中间件一个不漏（已实测：
   无头访问 `/docs` 也是 401）。
3. **`secrets.compare_digest` 而不是 `==`。** 避免比较耗时随前缀长度变化（时序侧信道）。

API 与 MCP-over-HTTP **共用同一个中间件**，避免两份实现走偏。`stdio` 传输没有 HTTP 请求头
可言 —— 进程边界本身就是鉴权（谁能启这个进程谁就能调），所以 `stdio` 不做鉴权；切到
`sse` / `streamable-http` 时 `mcp_server.main()` 强制要求配密钥。

`/health` 会回 `auth: {key_configured, allow_no_auth}`，方便确认当前处于哪一态。
**服务未配 key 且未开开关时直接 `raise SystemExit(NO_KEY_HINT)`**，起不来比裸奔好。

### 连接池（`db.py`）—— 自己写的池，不是 `psycopg2.pool`

原实现是"模块级单连接 + 断线重连"，两个问题：**所有请求共享同一个事务**（第一次 execute
就开启事务并一直挂成 `idle in transaction`，某条语句报错则整条连接 aborted，后续全挂）；
同一连接上两个 cursor 交替 execute 也没有隔离保证。psycopg2 说的 thread-safety level 2
只是"不会崩"，不等于可以并发共用。

**为什么不用 `psycopg2.pool` —— 三条全是实测出来的：**

1. `_putconn()` 只在 `len(_pool) < self.minconn` 时才放回，否则 `conn.close()`。
   常见写法 `(minconn=1, maxconn=N)` 在并发下**每次新建 + 关闭 TCP**；实测
   `ThreadedConnectionPool(1, 3)` 归还两条后池内只剩 1 条。本库远程 + SSL，
   **建连 331–360ms，比查询本身还贵**。→ 正确用法只有 `minconn == maxconn`。
2. `_getconn()` 在 `used == maxconn` 时**直接抛 `PoolError`，不排队**，并发一超就 500。
3. **最致命**：`_putconn()` 对坏连接先 `conn.rollback()`，这一步在已断开的连接上抛
   `OperationalError`，该异常无兜底 → 后面的 `del self._used[key]` 被跳过 →
   **池的记账永久污染，从 N 条退化到 1 条且回不来**（实测 3 → 1）。

自研池：`collections.deque` 存空闲连接 + `threading.BoundedSemaphore` 限并发。
**只在 `_idle` 为空时才 `psycopg2.connect()`**，保证 `idle + in_use <= size`；每条路径的
记账都是显式的，坏连接判定后直接 `close()` 丢弃。并发超池时**排队等待 5s**（`PoolBusy`
→ API 翻译成 **503**），不是 500。

**死连接预检不出来**：服务端掐断后 `conn.closed` 仍是 **0**（要等下次查询失败才变 2），
`transaction_status` 也是陈旧值。所以策略是**空闲超 30s 才 ping 一次**（实测一次往返
42ms，只在真正空闲后付这个钱），ping 失败就丢弃换新的。残留缺口（刚归还就被杀）由
`fetch()` 兜住。

**`fetch(fn)` 重试封装 —— 只用于只读操作**：只对 `OperationalError` / `InterfaceError`
（连接级）换一条重试**一次**；`ProgrammingError`（SQL 级）**不重试** —— 重试不会好，还掩盖
bug。写操作绝不能走 `fetch()`，可能把同一条记录写两次。有效性已严格对照验证：
服务端掐断池内全部连接后，裸 `db()` 6 轮失败 3 次（正好等于死连接数），`fetch()` 9/9 全过。

**不做启动预热**：连接按需创建，第一个请求付一次 ~350ms；突发并发时各请求并行建连，比启动
串行建 10 条（约 3.5s）更快，进程也能立刻起来。`warm_pool()` 只备池对象，不预建连接。

连接参数带**客户端 TCP keepalive**（`keepalives` / `keepalives_idle=30` / `interval=10` /
`count=3`，libpq 实测接受）。服务端 `idle_session_timeout=0`（不会杀空闲连接），但链路中间的
NAT / 防火墙会，让操作系统自己发现对端消失比等应用层超时快得多。

**本模块只读**：`db()` 归还前一律 rollback。将来要加写接口，必须在 `with` 块内显式
`commit()` —— 否则会被这里的 rollback 静默丢掉。

## 数据库

主数据库 `mycar`（PostgreSQL）：vehicles、vehicle_images、proxies、crawl_logs、import_history 等。

**注意**：本次重构**不动数据库 schema**。新站点接入时再单独评估是否要加 `source_site` 列。

## 启动方式

```bash
# uv（推荐）
uv sync
uv run python run_service.py
# 或安装后
uv run carinfo-service

# 或不用 uv
pip install -e .
python run_service.py
python -m carinfo
```

## 已知问题与注意事项

1. **历史包袱：vehicle_id 不带 28car 前缀** —— 现存 11 万+ 行数据 vehicle_id 无前缀，且 `vehicle_images` 有 FK + `ON UPDATE RESTRICT`，无法批量改写。新站点统一用 `{site_name}_{native_id}` 前缀；详见 `core/base_spider.py:vehicle_id()` docstring。
2. **BaseSpider 目前是空壳抽象**：4 个抽象方法定义了但调度流程没真正调用——`scrape_vehicle_type` 走的是 `get_html_1` / `get_date_code` / `extract_car_info`。接入第二站时需要把 HTTP/代理/反爬 等基础设施真的下沉到 core，并让 `scrape_vehicle_type` 改成基于 `spider.list_url()` / `spider.parse_list()` 的通用流程。
3. **`os.environ` 传爬取统计**：已修复 —— `scrape_vehicle_type()` 返回统计 dict，由 `service.py` / `car28.main()` 聚合后经 `record_crawl_log()` 写入 `crawl_logs`；`importer.main()`（CSV 补导通道）已不再读写环境变量，也不再写爬取日志。
4. **无测试**：项目没有常驻的单元测试（历次修复靠一次性脚本验证，建议后续补 pytest）。
5. **相对路径依赖**：`config.json`、状态文件、CSV 等都用相对路径，依赖 `run_service.py` 中的 `os.chdir(repo_root)`。
6. **CSV 现在只是备份**：爬取结果逐页通过 `import_rows()` 直接入库；`data/csv/*.csv` 只写不读，供排查/审计/手动补导（`python -m carinfo.core.importer` 仍可从 CSV 补导）。入库失败时该页数据仍在 CSV 里可补救。
7. **动态域名 BASE_URL**：28car 的真实域名（如 `dj1jklak2e.28car.com`）会变化，需手动更新 `sites/car28.py:BASE_URL`。
8. **行为变更提醒**：改为直接入库后，掉出配置页数范围（前 N 页）的旧车不再被每日刷新 `updated_at`——这是有意为之（恢复 updated_at 的"最近仍在售"语义）。若下游有依赖旧行为需回退。
9. **busy_handling 配置约束（改配置前必读）**：`fail_streak_threshold` 必须 ≤ `busy_max_retries`（否则单请求会在冷却触发前耗尽预算，退化成"烧光重试"，即 P0-2）；且必须 > 并发数（否则高并发下一次齐撞直接进冷却）。提高并发时必须同步调大该阈值。
10. **FileLock stale_after 已改为 2 小时 + 每页 `touch()`**（原为固定 8h < 整轮 12h，跨夜会被误判 stale）。
    现设计：阈值只跟**单页耗时**（约 1 分钟）挂钩，活进程每页刷新一次锁持续证明自己在。2 小时 = 单页耗时的百倍余量。
    ⚠️ **若哪天把 page_hook 里的 `self.lock.touch()` 删了**（或 `scrape_vehicle_type` 不再回调），阈值必须调回 > 单轮最坏耗时
    （12 小时），否则跨夜时会并发双跑。这一条与爬虫侧的 `page_hook` 参数是**耦合的**，改一处要看另一处。
11. **连续失败链判据的已知盲区**：站点"间歇放行"（半死不活、偶尔成功）时失败链被成功反复打断，永不触发冷却——行为是慢磨不终止、持续消耗代理拉黑额度。当前可接受；若实测出现此形态，再补"窗口内 busy 占比"兜底判据。
12. **搜索结果质量上限受数据覆盖制约**：车况类字段（手数/里程/行水货）覆盖仅 28.0%，浏览量 44.8%。引擎已用"缺维权重归一"避免误伤，但缺数据就是缺数据——无法为这些车算车况分。想提升排序质量，根子在爬虫侧的字段提取率，不在打分公式。
13. **约 379 台车无车系信息**：`car_model` 被卖家填成纯数字（如 '628000'、'2015'）或单字符，归一无法还原，各自成组拿不到行情基准。属源头数据缺陷，不修。
14. **`model_vocabulary` 是迁移来的固定数据，含脏条目**：已在下游过滤 21 条（11 条单字符假车系 + 10 条"车系+排量"粘连），但词表源头没有生成脚本，无法从根上修。新增车系仍需手工维护该表。
15. **`mileage` 文本字段噪声大**：形如 '180km' 的值明显不是总里程（把续航等数字抓进来了）。`mileage_km` 结构化字段干净得多，因此展示与过滤统一"优先 mileage_km，退回 mileage 文本"。更彻底的做法是修改爬虫侧的 `extra_fields` 提取正则（要求 ≥4 位数字），但那会影响存量数据。
16. **搜索服务的并发与鉴权已就位，但仍有两个边界**：（已解决）模块级单连接换成进程内连接池、
   加了 `X-API-Key` 请求头鉴权，详见「连接池」「鉴权」两节。**仍待处理**：(a) `SearchContext`
    的 5 分钟词表缓存是**每进程**的，特征表重算后**多进程同时跑时互不感知**（单进程无碍；
    要跨进程失效得引入版本号或通知机制）；(b) 池大小是每进程的，`DB_POOL_SIZE × 进程数`
    必须留在服务端 `max_connections=100` 预算内 —— 加到第 4、5 个进程前先算一下。
## 加新站点的步骤

> ⚠️ 目前 `BaseSpider` 的 4 个抽象方法实际未被调度流程调用（见已知问题 2）。真正接入第二站时，需要先把 `scrape_vehicle_type` 改造成通用流程，否则光实现 4 个方法跑不起来。

1. 在 `src/carinfo/sites/` 下新建 `xxx.py`
2. 定义 `class XxxSpider(BaseSpider)`，设置 `site_name` 和 `base_url`
3. 实现 4 个抽象方法（`list_url`/`detail_url`/`parse_list`/`parse_detail`）
4. 把 `car28.py` 中的 HTTP/代理/反爬/CSV/并发逻辑下沉到 `core/`（出现真实复用需求时）
5. 在 `service.py` 中调度该 spider
6. CSV 列名/DB 字段对齐既有 schema，或单独评估扩展

## 编辑注意事项

- 修改爬虫字段时，确保 `sites/car28.py` 的 `extract_car_info` 与 `core/importer.py` 的 SQL 语句保持一致
- 代理相关改动需同步 `core/proxy.py` 和 `sites/car28.py` 中的 `scrape_vehicle_type` 函数
- 不要提交 `.env`（含真实密码）和 `config.json`（含代理配置）
- 搜索子系统**只读**：不要在 `search/` 下加写操作；确需写入必须走 `core/importer.py` 的通道
- 改 `search/` 后必跑四套单测 + `eval_search.py`（见「验证与评测」），门禁绿灯才算改完
- `search/**` 一律用 `db.fetch()` 借连接，**不要**自己 `psycopg2.connect()`（`features.py`
  的一次性 CLI 重算脚本是唯一例外）；写操作**绝不**走 `fetch()`（重试会写两次）
