#!/bin/bash
set -e

# 创建必要的目录
mkdir -p /app/debug_pages /app/logs /app/data /app/csv_output

# 设置权限
chmod -R 755 /app/debug_pages /app/logs /app/data /app/csv_output

# 检查配置文件是否存在
if [ ! -f "/app/config.json" ]; then
    echo "警告: config.json 不存在，将使用默认配置"
    echo '{"scraping": {"vehicle_types": {"1": {"name": "私家车", "enabled": true, "pages": 1}}}}' > /app/config.json
fi

if [ ! -f "/app/proxy_config.json" ]; then
    echo "警告: proxy_config.json 不存在，将使用直连模式"
    echo '{"proxies": [], "retry_settings": {"max_retries": 3, "retry_delay": 5, "timeout": 30}, "fallback_to_direct": true}' > /app/proxy_config.json
fi

# 等待数据库连接
echo "检查数据库连接..."
until python -c "
import mysql.connector
import os
try:
    conn = mysql.connector.connect(
        host=os.getenv('MYSQL_HOST', 'host.docker.internal'),
        user=os.getenv('MYSQL_USER', 'root'),
        password=os.getenv('MYSQL_PASSWORD', '1qaz!QAZ2wsx@WSX'),
        database=os.getenv('MYSQL_DATABASE', 'car_info_db'),
        port=int(os.getenv('MYSQL_PORT', '3306')),
        connect_timeout=10
    )
    conn.close()
    print('数据库连接成功')
except Exception as e:
    print(f'数据库连接失败: {e}')
    exit(1)
"; do
    echo "等待数据库连接..."
    sleep 5
done

echo "启动应用程序..."

# 执行传入的命令
exec "$@"
