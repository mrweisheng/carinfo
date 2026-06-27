#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日志系统模块
提供统一的日志配置和管理功能，支持日志轮转、分级日志、统计指标
"""

import os
import logging
import time
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone, timedelta

# 北京时间时区
BEIJING_TZ = timezone(timedelta(hours=8))
from typing import Optional, Dict, Any
from pathlib import Path


class LoggerManager:
    """日志管理器"""
    
    _instance = None
    _initialized = False
    
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self, log_dir: str = '.', log_file: str = 'carinfo.log', 
                 max_bytes: int = 10*1024*1024, backup_count: int = 5,
                 console_level: int = logging.INFO, file_level: int = logging.DEBUG):
        if self._initialized:
            return
        
        self.log_dir = log_dir
        self.log_file = log_file
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.console_level = console_level
        self.file_level = file_level
        
        self.stats = {
            'requests': 0,
            'successes': 0,
            'failures': 0,
            'pages_processed': 0,
            'cars_extracted': 0,
            'errors': []
        }
        
        self._setup_logger()
        self._initialized = True
    
    def _setup_logger(self):
        """设置日志器"""
        self.logger = logging.getLogger('carinfo')
        self.logger.setLevel(logging.DEBUG)
        self.logger.handlers = []
        
        log_path = os.path.join(self.log_dir, self.log_file)
        
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=self.max_bytes,
            backupCount=self.backup_count,
            encoding='utf-8'
        )
        file_handler.setLevel(self.file_level)
        file_formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(file_formatter)
        
        console_handler = logging.StreamHandler()
        console_handler.setLevel(self.console_level)
        console_formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%H:%M:%S'
        )
        console_handler.setFormatter(console_formatter)
        
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)
    
    def get_logger(self, name: str = None) -> logging.Logger:
        """获取日志器"""
        if name:
            return logging.getLogger(f'carinfo.{name}')
        return self.logger
    
    def debug(self, message: str):
        self.logger.debug(message)
    
    def info(self, message: str):
        self.logger.info(message)
    
    def warning(self, message: str):
        self.logger.warning(message)
    
    def error(self, message: str):
        self.logger.error(message)
    
    def critical(self, message: str):
        self.logger.critical(message)
    
    def log_request(self, success: bool = True):
        """记录请求统计"""
        self.stats['requests'] += 1
        if success:
            self.stats['successes'] += 1
        else:
            self.stats['failures'] += 1
    
    def log_page_processed(self):
        """记录已处理页面"""
        self.stats['pages_processed'] += 1
    
    def log_cars_extracted(self, count: int):
        """记录提取的车辆数"""
        self.stats['cars_extracted'] += count
    
    def log_error(self, error_type: str, message: str):
        """记录错误"""
        self.stats['errors'].append({
            'type': error_type,
            'message': message,
            'timestamp': datetime.now(BEIJING_TZ).isoformat()
        })
        if len(self.stats['errors']) > 100:
            self.stats['errors'] = self.stats['errors'][-100:]
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        stats = self.stats.copy()
        if stats['requests'] > 0:
            stats['success_rate'] = stats['successes'] / stats['requests'] * 100
        else:
            stats['success_rate'] = 0
        return stats
    
    def reset_stats(self):
        """重置统计"""
        self.stats = {
            'requests': 0,
            'successes': 0,
            'failures': 0,
            'pages_processed': 0,
            'cars_extracted': 0,
            'errors': []
        }
    
    def print_stats(self):
        """打印统计信息"""
        stats = self.get_stats()
        print("\n=== 爬取统计 ===")
        print(f"总请求数: {stats['requests']}")
        print(f"成功: {stats['successes']}")
        print(f"失败: {stats['failures']}")
        print(f"成功率: {stats['success_rate']:.2f}%")
        print(f"处理页面: {stats['pages_processed']}")
        print(f"提取车辆: {stats['cars_extracted']}")
        if stats['errors']:
            print(f"错误数: {len(stats['errors'])}")
            print("最近错误:")
            for err in stats['errors'][-5:]:
                print(f"  - [{err['type']}] {err['message']}")


class ScrapingStats:
    """爬取统计类（轻量级）"""
    
    def __init__(self):
        self.start_time = None
        self.end_time = None
        self.pages = 0
        self.cars = 0
        self.errors = []
        self.success_count = 0
        self.failure_count = 0
    
    def start(self):
        """开始计时"""
        self.start_time = time.time()
    
    def end(self):
        """结束计时"""
        self.end_time = time.time()
    
    def add_page(self):
        """添加页面计数"""
        self.pages += 1
    
    def add_cars(self, count: int):
        """添加车辆计数"""
        self.cars += count
    
    def add_success(self):
        """添加成功计数"""
        self.success_count += 1
    
    def add_failure(self):
        """添加失败计数"""
        self.failure_count += 1
    
    def add_error(self, error: str):
        """添加错误"""
        self.errors.append({
            'time': datetime.now(BEIJING_TZ).strftime('%H:%M:%S'),
            'error': error
        })
        if len(self.errors) > 50:
            self.errors = self.errors[-50:]
    
    def get_duration(self) -> float:
        """获取持续时间（秒）"""
        if self.start_time is None:
            return 0
        end = self.end_time or time.time()
        return end - self.start_time
    
    def get_summary(self) -> Dict[str, Any]:
        """获取统计摘要"""
        return {
            'duration': self.get_duration(),
            'pages': self.pages,
            'cars': self.cars,
            'successes': self.success_count,
            'failures': self.failure_count,
            'success_rate': (self.success_count / (self.success_count + self.failure_count) * 100 
                           if (self.success_count + self.failure_count) > 0 else 0),
            'errors': len(self.errors)
        }
    
    def print_summary(self):
        """打印摘要"""
        summary = self.get_summary()
        print("\n" + "=" * 50)
        print("爬取统计摘要")
        print("=" * 50)
        print(f"持续时间: {summary['duration']:.1f} 秒")
        print(f"处理页面: {summary['pages']}")
        print(f"提取车辆: {summary['cars']}")
        print(f"请求成功: {summary['successes']}")
        print(f"请求失败: {summary['failures']}")
        print(f"成功率: {summary['success_rate']:.2f}%")
        print(f"错误数: {summary['errors']}")
        if self.errors:
            print("\n最近错误:")
            for err in self.errors[-5:]:
                print(f"  [{err['time']}] {err['error']}")
        print("=" * 50)


def get_logger(name: str = None) -> logging.Logger:
    """获取日志器的便捷函数"""
    manager = LoggerManager()
    return manager.get_logger(name)


def get_stats() -> Dict[str, Any]:
    """获取统计信息的便捷函数"""
    manager = LoggerManager()
    return manager.get_stats()
