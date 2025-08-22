#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Docker环境配置管理模块
"""

import os
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

def get_env_var(key, default=None, var_type=str):
    """获取环境变量并转换类型"""
    value = os.getenv(key, default)
    if value is None:
        return None
    
    try:
        if var_type == bool:
            return value.lower() in ('true', '1', 'yes', 'on')
        elif var_type == int:
            return int(value)
        elif var_type == float:
            return float(value)
        else:
            return str(value)
    except (ValueError, TypeError):
        logger.warning(f"无法转换环境变量 {key}={value} 为 {var_type.__name__}，使用默认值")
        return default

def get_database_config():
    """获取数据库配置"""
    # 在Docker环境中，如果数据库在同一台服务器上，使用host.docker.internal或172.17.0.1
    default_host = 'host.docker.internal' if is_docker_environment() else '103.117.122.192'
    
    return {
        'host': get_env_var('MYSQL_HOST', default_host),
        'port': get_env_var('MYSQL_PORT', 3306, int),
        'user': get_env_var('MYSQL_USER', 'root'),
        'password': get_env_var('MYSQL_PASSWORD', '1qaz!QAZ2wsx@WSX'),
        'database': get_env_var('MYSQL_DATABASE', 'car_info_db')
    }

def get_app_config():
    """获取应用配置"""
    return {
        'log_level': get_env_var('LOG_LEVEL', 'INFO'),
        'timezone': get_env_var('TZ', 'Asia/Hong_Kong'),
        'scraper_delay_min': get_env_var('SCRAPER_DELAY_MIN', 1, float),
        'scraper_delay_max': get_env_var('SCRAPER_DELAY_MAX', 3, float),
        'anti_crawler_delay_min': get_env_var('ANTI_CRAWLER_DELAY_MIN', 30, float),
        'anti_crawler_delay_max': get_env_var('ANTI_CRAWLER_DELAY_MAX', 60, float),
        'config_file': get_env_var('CONFIG_FILE', '/app/config.json'),
        'proxy_config_file': get_env_var('PROXY_CONFIG_FILE', '/app/proxy_config.json'),
        'csv_output_dir': get_env_var('CSV_OUTPUT_DIR', '/app'),
        'debug_pages_dir': get_env_var('DEBUG_PAGES_DIR', '/app/debug_pages'),
        'logs_dir': get_env_var('LOGS_DIR', '/app/logs')
    }

def get_api_config():
    """获取API配置"""
    return {
        'host': get_env_var('API_HOST', '0.0.0.0'),
        'port': get_env_var('API_PORT', 5000, int),
        'debug': get_env_var('API_DEBUG', False, bool)
    }

def ensure_directories():
    """确保必要的目录存在"""
    app_config = get_app_config()
    directories = [
        app_config['debug_pages_dir'],
        app_config['logs_dir'],
        '/app/data',
        '/app/csv_output'
    ]
    
    for directory in directories:
        Path(directory).mkdir(parents=True, exist_ok=True)
        logger.info(f"确保目录存在: {directory}")

def load_json_config(config_file, default_config=None):
    """加载JSON配置文件"""
    if default_config is None:
        default_config = {}
    
    try:
        if os.path.exists(config_file):
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
            logger.info(f"成功加载配置文件: {config_file}")
            return config
        else:
            logger.warning(f"配置文件不存在: {config_file}，使用默认配置")
            return default_config
    except Exception as e:
        logger.error(f"加载配置文件失败: {e}，使用默认配置")
        return default_config

def get_scraping_config():
    """获取爬取配置"""
    app_config = get_app_config()
    config_file = app_config['config_file']
    
    default_config = {
        'scraping': {
            'vehicle_types': {
                '1': {'name': '私家车', 'enabled': True, 'pages': 1},
                '2': {'name': '客货车', 'enabled': False, 'pages': 0},
                '3': {'name': '货车', 'enabled': False, 'pages': 0},
                '4': {'name': '电单车', 'enabled': False, 'pages': 0},
                '5': {'name': '经典车', 'enabled': False, 'pages': 0}
            }
        }
    }
    
    return load_json_config(config_file, default_config)

def get_proxy_config():
    """获取代理配置"""
    app_config = get_app_config()
    proxy_config_file = app_config['proxy_config_file']
    
    default_config = {
        'proxies': [],
        'retry_settings': {
            'max_retries': 3,
            'retry_delay': 5,
            'timeout': 30
        },
        'fallback_to_direct': True
    }
    
    return load_json_config(proxy_config_file, default_config)

def setup_logging():
    """设置日志配置"""
    app_config = get_app_config()
    log_level = getattr(logging, app_config['log_level'].upper(), logging.INFO)
    
    # 创建logs目录
    logs_dir = Path(app_config['logs_dir'])
    logs_dir.mkdir(parents=True, exist_ok=True)
    
    # 配置日志格式
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # 文件处理器
    file_handler = logging.FileHandler(
        logs_dir / 'carinfo.log',
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(log_level)
    
    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(log_level)
    
    # 配置根日志器
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    
    logger.info(f"日志系统已配置，级别: {app_config['log_level']}")

def is_docker_environment():
    """检查是否在Docker环境中运行"""
    return os.path.exists('/.dockerenv') or os.getenv('DOCKER_CONTAINER') == 'true'

def init_docker_environment():
    """初始化Docker环境"""
    if is_docker_environment():
        logger.info("检测到Docker环境，初始化配置...")
        
        # 确保目录存在
        ensure_directories()
        
        # 设置日志
        setup_logging()
        
        # 输出配置信息
        db_config = get_database_config()
        logger.info(f"数据库配置: {db_config['host']}:{db_config['port']}/{db_config['database']}")
        
        app_config = get_app_config()
        logger.info(f"应用配置: 日志级别={app_config['log_level']}, 时区={app_config['timezone']}")
        
        logger.info("Docker环境初始化完成")
    else:
        logger.info("非Docker环境，使用标准配置")

if __name__ == "__main__":
    # 测试配置加载
    init_docker_environment()
    
    print("数据库配置:", get_database_config())
    print("应用配置:", get_app_config())
    print("API配置:", get_api_config())
    print("爬取配置:", get_scraping_config())
    print("代理配置:", get_proxy_config())
