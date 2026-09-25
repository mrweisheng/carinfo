## carinfo-service 部署与运行

### 1) 安装依赖

推荐使用 [uv](https://docs.astral.sh/uv/)（项目用 `uv.lock` 锁版本）：

```bash
uv sync
```

或者用 pip（基于 pyproject.toml）：

```bash
python -m pip install -U pip
pip install -e .
```

### 2) 配置环境变量

复制 `env.example` 为 `.env` 并填写数据库配置（代理库、车辆库等）。

### 3) 启动调度服务（前台运行，实时日志）

uv 方式：

```bash
uv run python run_service.py
# 或安装后直接用 entry point
uv run carinfo-service
```

不用 uv：

```bash
python run_service.py
# 或 pip install -e . 后
python -m carinfo
carinfo-service
```

### 4) 调度规则

- **每天只跑一次**：在 `schedule.windows` 列出的窗口里**随机挑一个窗口**，再在该窗口内**随机挑一个时刻**执行
- **「三个窗口」不是「跑三次」**：上午/下午/晚上是**三个候选时段**，服务每天随机命中其中一个
- **启动时**：查询状态文件的 `last_run_date`——
  - 等于今天 → 今天已完工，**直接进入调度循环**，不再执行
  - 不等于今天（含从未跑过）→ **立即执行一次**，然后重排次日时间
- **同一天不重复**：状态文件里写死 `last_run_date`，**重启服务/重启机器都不影响**，当天不会再跑第二次
- **标记时机**：`last_run_date` 只在**本轮全部类型都爬完**之后写入，不是一开始就写
- **每次执行**：根据 `config.json` 中 `vehicle_types` 配置的页数爬取，**爬完一页即直接入库**（不再等全部爬完再统一导入）
- **防冲突**：若上一次任务未结束（`carinfo_run.lock` 未过期），会跳过并自动顺延重排；锁文件默认 2 小时未刷新视为 stale（服务每爬一页会刷新一次锁，见 `FileLock.touch`）

> ⚠️ **窗口必须 `start < end` 且落在同一天内**（不支持 `22:00 → 06:00` 这种跨午夜窗口）。
> 单轮任务本身可能跑十几个小时，跨夜是常态 —— 跨夜时它继续跑完当前这轮，
> 但调度器不会在窗口外再起新一轮。

<details>
<summary>查看完整调度时序（点击展开）</summary>

```
进程启动
  ├─ 读 config.json → 解析 schedule.windows
  │    └─ 缺段/非法 → 【打印 WARNING】+ 用默认三段窗口
  ├─ 读 .carinfo_service_state.json
  └─ last_run_date == 今天 ?
       ├─ 是 → 只打日志「今天已执行过任务」，不跑
       └─ 否 → 立即跑一轮
              ├─ 逐类型逐页爬取（每页刷新锁）
              ├─ 全部爬完 → 写 last_run_date = 今天
              └─ 重排 next_run

调度循环
  ├─ _ensure_next_time()：若 next_run 已过期 → 在「今天剩余窗口」里重排
  ├─ sleep 到 next_run
  ├─ 到达时若锁还被占（上一轮没跑完）→ 顺延重排，不在原地死等
  └─ 否则跑一轮
```

**关键点：`last_run_date` 是「今天已完成」的唯一事实来源**，存在状态文件里，
所以 `systemctl restart` / 机器重启 / 手动重跑都不会让当天重复执行。

</details>

### 5) 数据流与产物

```
config.json + .env
    ↓
scrape_vehicle_type()  逐页爬取
    ├─→ data/csv/car_data_{type}.csv   仅写不读的备份，供排查/审计
    └─→ import_rows()                  直接入库 PostgreSQL（vehicles / vehicle_images）
                                       当次统计写入 crawl_logs
```

- `data/csv/*.csv` 是**备份产物**，入库不依赖它；某页入库失败时数据仍留在 CSV 里，可事后手动补导。
- 需要补导时执行 `python -m carinfo.core.importer`，它会读取 `data/csv/car_data_*.csv` 回灌数据库。

### 6) 配置文件说明

`config.json` 完整配置示例：

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
    "vehicle_types": {
      "1": {"name": "私家车", "pages": 40},
      "2": {"name": "客货车", "pages": 20},
      "3": {"name": "货车", "pages": 20},
      "4": {"name": "电单车", "pages": 20},
      "5": {"name": "经典车", "pages": 20}
    }
  }
}
```

| 配置项 | 必需 | 说明 |
|--------|------|------|
| `vehicle_types` | 必需 | 至少要有一个 type 的 `pages > 0`，否则启动失败 |
| `vehicle_types.{id}.pages` | 必需 | 每次执行爬取该类型的页数，设为 0 则跳过该类型 |
| `vehicle_types.{id}.name` | 可选 | 仅作日志显示，默认 `类型{id}` |
| `schedule.windows` | 推荐 | 每天随机挑一个窗口执行一次；缺省时用默认三段窗口（08-12 / 12-18 / 18-22）**并打 WARNING** |
| `schedule.windows[].name` | 可选 | 仅作日志显示，默认 `窗口N` |
| `schedule.windows[].start` / `.end` | 必需 | `"HH:MM"` / `[H, M]` / 整数小时都行。必须 `start < end`，否则启动失败 |

> ⚠️ **旧版的 `schedule.times` 已废弃**，写了会**直接启动失败**并提示迁移写法。
> 旧格式（如 `["08:00","12:00","16:00","20:00"]`）看着像"四个候选时刻"，代码却**只读前两个**
> 当窗口起止，后两个被静默丢弃 —— 这种「配置写了但不生效还不报错」是运维事故的温床，
> 所以本轮直接换字段名并强制报错。

### 7) 状态文件

服务会自动创建 `.carinfo_service_state.json` 记录执行状态：

```json
{
  "last_run_date": "2026-03-11",
  "last_run_at": "2026-03-11T14:30:07+08:00",
  "next_run": "2026-03-12T14:30:00+08:00",
  "next_run_window": "下午"
}
```

| 字段 | 说明 |
|------|------|
| `last_run_date` | **上次成功执行完成的日期** —— 防重复执行的唯一依据 |
| `last_run_at` | 上次执行完成的具体时刻（排查用） |
| `next_run` | 下次计划执行时间 |
| `next_run_window` | `next_run` 落在哪个窗口（可读性用） |

**运维动作**：

```bash
# 看今天到底跑没跑
cat .carinfo_service_state.json

# 想让今天再跑一次（异常排查用）：删掉 last_run_date 再重启服务
# ⚠️ 注意会真的再爬一轮，会消耗代理额度
python - <<'PY'
import json, pathlib
p = pathlib.Path(".carinfo_service_state.json")
d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
d.pop("last_run_date", None)
p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
PY
sudo systemctl restart carinfo-service
```

### 8) 常用命令

```bash
# 启动服务（前台运行，实时日志）
uv run python run_service.py

# 从 CSV 备份补导入数据库
uv run python -m carinfo.core.importer

# 停止服务
# 按 Ctrl+C 或发送 SIGTERM 信号

# 查看日志
# 日志直接输出到控制台
```

> 📄 **服务器部署（NGINX / systemd / 自动部署）** 见项目根目录的 **`DEPLOY.md`**。
