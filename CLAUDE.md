# CLAUDE.md — carinfo 项目

**语言：始终使用普通话中文汉字回复用户。代码、变量名、注释等可保留英文，但所有面向用户的文字必须使用中文。**

## 项目概述

carinfo 是一个香港汽车信息爬取与数据管理系统，从 28car.com 爬取二手车交易数据，存入 MySQL 数据库。架构采用 `core/`（通用基础设施） + `sites/`（站点特定逻辑）分层，方便后续接入新站点。

## 技术栈

| 组件 | 技术 |
|---|---|
| 语言 | Python 3.12 (>=3.10) |
| HTTP 客户端 | `requests` + `curl_cffi`（TLS 指纹模拟） |
| HTML 解析 | BeautifulSoup4 |
| 数据处理 | pandas |
| 数据库 | MySQL 8.4（mysql-connector-python + PyMySQL） |
| 代理管理 | 自建代理池（MySQL 存储） |
| 并发 | concurrent.futures.ThreadPoolExecutor |
| 调度 | 自实现时间窗口调度器 + FileLock 互斥 |
| 配置 | python-dotenv + JSON 配置文件 |
| 代码检查 | Ruff |
| 构建 | setuptools (pyproject.toml) |

## 目录结构

```
carinfo/
├── README.md
├── CLAUDE.md                       # 本文件
├── pyproject.toml                  # 项目元数据（包名 carinfo，入口 carinfo-service）
├── requirements.txt
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
│       ├── config_manager.py       # 配置管理（预留，未接入）
│       ├── logger.py               # 日志（预留，未接入）
│       ├── utils.py                # parse_price / parse_contact_info 等通用工具
│       ├── task_history.py         # 任务历史（预留，未接入）
│       ├── core/                   # 通用爬虫基础设施
│       │   ├── __init__.py
│       │   ├── base_spider.py      # 站点爬虫抽象基类（4 抽象方法）
│       │   ├── proxy.py            # 代理池（原 proxy_manager.py）
│       │   └── importer.py         # CSV → MySQL 导入（原 import_to_mysql.py）
│       └── sites/                  # 各站点业务实现
│           ├── __init__.py
│           └── car28.py            # 28car.com 业务（Car28Spider）
├── data/
│   └── csv/                        # CSV 爬取产物（gitignored）
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
- `scrape_vehicle_type()` 函数：被调度器调用的入口，编排单类型车辆爬取
- `auto_import_to_database()`：爬完后直接调用 `core.importer.main()` 入库
- 支持 5 种车辆类型：私家车(1)、客货车(2)、货车(3)、电单车(4)、经典车(5)
- 使用 `curl_cffi` 模拟 Chrome TLS 指纹
- 多编码自动检测（big5/utf-8/gbk/gb2312/latin1）
- 三级反爬退避（15min/30min/60min）

### core/proxy.py — 代理池

- ProxyManager 单例（模块级全局变量 + 锁）
- 从 MySQL `proxies` 表加载代理
- 健康检查：`enabled=TRUE AND is_healthy=TRUE AND fail_count<10`
- 失败超 10 次自动标记不健康
- 可用代理低于 20% 时自动重新加载

### core/importer.py — 数据导入

- DataValidator：校验 vehicle_id、价格、电话、年份、座位数
- ImportHistory：记录导入历史到 `import_history` 表
- CrawlLogManager：记录爬取统计到 `crawl_logs` 表
- FastCSVImporter：批量 INSERT IGNORE + 分批 UPDATE
- 价格解析复用 `carinfo.utils.parse_price`

### service.py — 调度服务

- FileLock：`O_CREAT | O_EXCL` 跨进程互斥锁
- CarinfoService：每天时间窗口内随机执行一次，同一天不重复
- 优雅退出（SIGINT/SIGTERM）
- 启动时检查数据库健康状态

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
    ▼
data/csv/car_data_{type}.csv
    │ auto_import_to_database()
    ▼
core/importer.py:main()
    ▼
MySQL (car_info_db)
```

## 数据库

主数据库 `car_info_db`：vehicles、vehicle_images、proxies、crawl_logs、import_history 等。

**注意**：本次重构**不动数据库 schema**。新站点接入时再单独评估是否要加 `source_site` 列。

## 启动方式

```bash
# 直接启动（推荐）
python run_service.py

# 或安装后
pip install -e .
python -m carinfo
carinfo-service
```

## 已知问题与注意事项

1. **历史包袱：vehicle_id 不带 28car 前缀** —— 现存 11 万+ 行数据 vehicle_id 无前缀，且 `vehicle_images` 有 FK + `ON UPDATE RESTRICT`，无法批量改写。新站点统一用 `{site_name}_{native_id}` 前缀；详见 `core/base_spider.py:vehicle_id()` docstring。
2. **死代码模块**：`config_manager.py` / `logger.py` / `task_history.py` 目前未被任何模块 import，保留备用。
3. **无测试**：项目没有任何单元测试或集成测试。
4. **相对路径依赖**：`config.json`、状态文件、CSV 等都用相对路径，依赖 `run_service.py` 中的 `os.chdir(repo_root)`。
5. **CSV 中间格式**：爬虫先写 `data/csv/*.csv` 再导入 MySQL，是有意设计（便于补导入和审计）。
6. **动态域名 BASE_URL**：28car 的真实域名（如 `dj1jklak2e.28car.com`）会变化，需手动更新 `sites/car28.py:BASE_URL`。

## 加新站点的步骤

1. 在 `src/carinfo/sites/` 下新建 `xxx.py`
2. 定义 `class XxxSpider(BaseSpider)`，设置 `site_name` 和 `base_url`
3. 实现 4 个抽象方法（`list_url`/`detail_url`/`parse_list`/`parse_detail`）
4. 在 `service.py` 或新 runner 中调度该 spider
5. CSV 列名/DB 字段对齐既有 schema，或单独评估扩展

## 编辑注意事项

- 修改爬虫字段时，确保 `sites/car28.py` 的 `extract_car_info`、`utils.py` 的 `validate_vehicle_data`、`core/importer.py` 的 SQL 语句三者保持一致
- 代理相关改动需同步 `core/proxy.py` 和 `sites/car28.py` 中的 `scrape_vehicle_type` 函数
- 不要提交 `.env`（含真实密码）和 `config.json`（含代理配置）
