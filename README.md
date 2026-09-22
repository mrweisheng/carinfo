 ## carinfo-service 部署与运行

 ### 1) 安装依赖

推荐使用 [uv](https://docs.astral.sh/uv/)（项目用 uv.lock 锁版本）：

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

- **启动时**：若今天还没执行过任务，则立即跑一次；若今天已执行则直接进入调度循环，等下一周期
- **每天**：在配置的时间窗口内（如 08:00-18:00）随机执行一次
- **同一天不重复**：无论启动几次，当天只会执行一次
- **每次执行**：根据 `config.json` 中 `vehicle_types` 配置的页数爬取
- **防冲突**：若上一次任务未结束（`carinfo_run.lock` 未过期），会跳过并自动顺延到下一窗口；锁文件默认 8 小时后视为 stale，可被新进程接管

### 5) 配置文件说明

`config.json` 完整配置示例：

```json
{
  "scraping": {
    "vehicle_types": {
      "1": {"name": "私家车", "pages": 40},
      "2": {"name": "客货车", "pages": 20},
      "3": {"name": "货车", "pages": 20},
      "4": {"name": "电单车", "pages": 20},
      "5": {"name": "经典车", "pages": 20}
    }
  },
  "schedule": {
    "times": ["08:00", "18:00"]
  }
}
```

| 配置项 | 必需 | 说明 |
|--------|------|------|
| `vehicle_types` | 必需 | 至少要有一个 type 的 `pages > 0`，否则启动失败 |
| `vehicle_types.{id}.pages` | 必需 | 每次执行爬取该类型的页数，设为 0 则跳过该类型 |
| `vehicle_types.{id}.name` | 可选 | 仅作日志显示，默认 `类型{id}` |
| `schedule.times` | 可选 | 默认 `["08:00", "18:00"]`；`[0]` 是窗口开始，`[1]` 是窗口结束 |

### 6) 状态文件

服务会自动创建 `.carinfo_service_state.json` 记录执行状态：

```json
{
  "last_run_date": "2026-03-11",
  "next_run": "2026-03-12T14:30:00"
}
```

| 字段 | 说明 |
|------|------|
| `last_run_date` | 上次执行日期（用于防止同一天重复执行） |
| `next_run` | 下次计划执行时间 |

### 7) 常用命令

```bash
# 启动服务（前台运行，实时日志）
uv run python run_service.py

# 停止服务
# 按 Ctrl+C 或发送 SIGTERM 信号

# 查看日志
# 日志直接输出到控制台
```
