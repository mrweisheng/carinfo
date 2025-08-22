module.exports = {
  apps: [
    {
      name: 'carinfo-scraper',
      script: 'carinfo.py',
      args: '--config',
      interpreter: 'python3',
      cwd: '/root/carinfo',
      instances: 1,
      autorestart: false,  // 单次运行，完成后不重启
      max_restarts: 3,
      min_uptime: '10s',
      max_memory_restart: '500M',
      env: {
        PYTHONUNBUFFERED: '1',
        PYTHONIOENCODING: 'utf-8',
        LANG: 'C.UTF-8',
        LC_ALL: 'C.UTF-8',
        TZ: 'Asia/Hong_Kong'
      },
      log_file: '/root/carinfo/logs/carinfo.log',
      out_file: '/root/carinfo/logs/carinfo-out.log',
      error_file: '/root/carinfo/logs/carinfo-error.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss'
    },
    {
      name: 'carinfo-scheduler',
      script: 'scheduler.py',
      interpreter: 'python3', 
      cwd: '/root/carinfo',
      instances: 1,
      autorestart: true,   // 调度器需要保持运行
      max_restarts: 10,
      min_uptime: '30s',
      max_memory_restart: '500M',
      env: {
        PYTHONUNBUFFERED: '1',
        PYTHONIOENCODING: 'utf-8',
        LANG: 'C.UTF-8',
        LC_ALL: 'C.UTF-8', 
        TZ: 'Asia/Hong_Kong'
      },
      log_file: '/root/carinfo/logs/scheduler.log',
      out_file: '/root/carinfo/logs/scheduler-out.log',
      error_file: '/root/carinfo/logs/scheduler-error.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss'
    }
  ]
};
