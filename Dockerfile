# 使用Python 3.9作为基础镜像
FROM python:3.9-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=utf-8
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV TZ=Asia/Hong_Kong

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    build-essential \
    curl \
    vim \
    tzdata \
    locales \
    && rm -rf /var/lib/apt/lists/*

# 生成UTF-8 locale
RUN sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen && \
    sed -i '/zh_CN.UTF-8/s/^# //g' /etc/locale.gen && \
    locale-gen

# 设置时区
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 复制requirements.txt并安装Python依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制并设置入口脚本权限（在用户切换前）
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# 复制应用程序代码
COPY . .

# 创建必要的目录
RUN mkdir -p /app/debug_pages /app/logs /app/data /app/csv_output

# 设置Python脚本权限
RUN chmod +x *.py

# 创建非root用户并设置权限
RUN useradd -m -u 1000 carinfo && chown -R carinfo:carinfo /app
USER carinfo

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import requests; print('Health check passed')" || exit 1

# 暴露端口（如果需要Web服务）
EXPOSE 5000

# 设置入口点
ENTRYPOINT ["docker-entrypoint.sh"]

# 默认启动命令
CMD ["python", "carinfo.py", "--config"]
