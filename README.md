 ## carinfo-service 部署与运行
 
 ### 1) 安装依赖（推荐）
 
 在项目根目录：
 
 ```bash
 python -m pip install -U pip
 pip install -r requirements.txt
 ```
 
 或使用标准项目方式（可选）：
 
 ```bash
 pip install -e .
 ```
 
 ### 2) 配置环境变量
 
 复制 `env.example` 为 `.env` 并填写数据库配置（代理库、车辆库等）。
 
 ### 3) 启动调度服务（前台运行，实时日志）
 
不安装也能跑（推荐服务器直接用这个）：

```bash
python run_service.py
```

或使用标准项目方式（需要先 `pip install -e .`）：

 ```bash
 python -m carinfo_service
 ```
 
 或（如果安装了 `pip install -e .`）：
 
 ```bash
 carinfo-service
 ```
 
### 4) 调度规则

- **启动后**：立即执行一次
- **每天**：在配置的时间窗口内（如 08:00-18:00）随机执行一次
- **同一天不重复**：无论启动几次，当天只会执行一次
- **每次执行**：根据 `config.json` 中 `vehicle_types` 配置的页数爬取
- **防冲突**：若上一次任务未结束，会跳过并自动顺延到第二天

### 5) 配置文件说明

`config.json` 完整配置示例：

```json
{
  "scraping": {
    "vehicle_types": {
      "1": {"pages": 40},
      "2": {"pages": 20},
      "3": {"pages": 20},
      "4": {"pages": 20},
      "5": {"pages": 20}
    }
  },
  "schedule": {
    "times": ["08:00", "18:00"]
  }
}
```

| 配置项 | 说明 |
|--------|------|
| `vehicle_types.{id}.pages` | 每次执行爬取该类型的页数，设为 0 则跳过 |
| `schedule.times[0]` | 每日执行时间窗口开始时间 |
| `schedule.times[1]` | 每日执行时间窗口结束时间 |

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
python run_service.py

# 停止服务
# 按 Ctrl+C 或发送 SIGTERM 信号

# 查看日志
# 日志直接输出到控制台
```
