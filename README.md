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
 
 - 启动后：立即执行一次
 - 每天：早上 09:00-11:00 随机时间执行一次
 - 每天：下午 16:00-20:00 随机时间执行一次
 - 每次执行：
   - 私家车（类型1）：40页
   - 客货车/货车/电单车/经典车（类型2-5）：各20页
 - 防冲突：若上一次任务未结束，会跳过并自动顺延重排（不会并发跑两次）
