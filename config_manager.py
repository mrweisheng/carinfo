#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置管理模块
提供配置的加载、验证、环境变量支持等功能
"""

import os
import json
import logging
from typing import Dict, Any, Optional, List
from pathlib import Path


logger = logging.getLogger(__name__)


class ConfigManager:
    """配置管理器"""
    
    DEFAULT_CONFIG = {
        'scraping': {
            'vehicle_types': {
                '1': {'name': '私家车', 'enabled': True, 'pages': 0},
                '2': {'name': '客货车', 'enabled': True, 'pages': 0},
                '3': {'name': '货车', 'enabled': True, 'pages': 0},
                '4': {'name': '电单车', 'enabled': True, 'pages': 0},
                '5': {'name': '经典车', 'enabled': True, 'pages': 0}
            }
        },
        'schedule': {
            'mode': 'times',
            'interval_hours': 4,
            'times': ['08:00', '12:00', '16:00', '20:00']
        }
    }
    
    REQUIRED_FIELDS = {
        'scraping': ['vehicle_types'],
        'schedule': ['mode', 'times']
    }
    
    def __init__(self, config_file: str = 'config.json'):
        self.config_file = config_file
        self.config = {}
        self._load_config()
    
    def _load_config(self):
        """加载配置文件"""
        if not os.path.exists(self.config_file):
            logger.error(f"配置文件 {self.config_file} 不存在，程序中断")
            raise FileNotFoundError(f"配置文件 {self.config_file} 不存在，请创建配置文件后重试")

        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                self.config = json.load(f)

            self._apply_env_overrides()
            logger.info(f"成功加载配置文件: {self.config_file}")
        except json.JSONDecodeError as e:
            logger.error(f"配置文件格式错误: {e}，程序中断")
            raise ValueError(f"配置文件 {self.config_file} 格式错误，请检查JSON格式")
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}，程序中断")
            raise RuntimeError(f"加载配置文件 {self.config_file} 失败: {e}")
    
    def _apply_env_overrides(self):
        """应用环境变量覆盖"""
        env_mappings = {
            'DB_HOST': ('database', 'host'),
            'DB_PORT': ('database', 'port'),
            'DB_USER': ('database', 'user'),
            'DB_PASSWORD': ('database', 'password'),
            'DB_NAME': ('database', 'database'),
            'SCHEDULE_MODE': ('schedule', 'mode'),
            'SCHEDULE_TIMES': ('schedule', 'times'),
        }
        
        for env_key, (section, key) in env_mappings.items():
            env_value = os.environ.get(env_key)
            if env_value is not None:
                if section not in self.config:
                    self.config[section] = {}
                
                if env_key == 'SCHEDULE_TIMES':
                    try:
                        self.config[section][key] = json.loads(env_value)
                    except json.JSONDecodeError:
                        logger.warning(f"环境变量 {env_key} 格式错误，跳过")
                else:
                    if env_key == 'DB_PORT':
                        try:
                            self.config[section][key] = int(env_value)
                        except ValueError:
                            logger.warning(f"环境变量 {env_key} 应为整数，跳过")
                    else:
                        self.config[section][key] = env_value
                
                logger.debug(f"应用环境变量覆盖: {env_key} -> {section}.{key}")
    
    def get(self, section: str, key: str = None, default: Any = None) -> Any:
        """
        获取配置值
        
        Args:
            section: 配置章节
            key: 配置键（可选）
            default: 默认值
        
        Returns:
            配置值
        """
        section_data = self.config.get(section, {})
        
        if key is None:
            return section_data
        
        return section_data.get(key, default)
    
    def get_enabled_vehicle_types(self) -> Dict[int, Dict[str, Any]]:
        """获取需要爬取的车辆类型配置（pages > 0）"""
        vehicle_types = self.config.get('scraping', {}).get('vehicle_types', {})
        if not vehicle_types:
            logger.error("配置文件中缺少 vehicle_types 配置，程序中断")
            raise ValueError("配置文件中缺少 vehicle_types 配置")

        enabled_types = {}

        for type_id, type_config in vehicle_types.items():
            pages = type_config.get('pages', 0)
            if pages > 0:
                enabled_types[int(type_id)] = {
                    'name': type_config.get('name', f'类型{type_id}'),
                    'pages': pages
                }

        if not enabled_types:
            logger.warning("配置文件中没有需要爬取的车辆类型（所有类型的 pages 都为 0）")

        return enabled_types
    
    def get_schedule_config(self) -> Dict[str, Any]:
        """获取调度配置"""
        return self.config.get('schedule', self.DEFAULT_CONFIG.get('schedule', {}))
    
    def get_database_config(self) -> Dict[str, Any]:
        """获取数据库配置（优先从环境变量读取）"""
        db_config = self.config.get('database', {})
        
        return {
            'host': os.environ.get('DB_HOST', db_config.get('host', 'localhost')),
            'port': int(os.environ.get('DB_PORT', db_config.get('port', 3306))),
            'user': os.environ.get('DB_USER', db_config.get('user', 'root')),
            'password': os.environ.get('DB_PASSWORD', db_config.get('password', '')),
            'database': os.environ.get('DB_NAME', db_config.get('database', 'car_info_db'))
        }
    
    def validate(self) -> Tuple[bool, List[str]]:
        """
        验证配置有效性
        
        Returns:
            (是否有效, 错误信息列表)
        """
        errors = []
        
        for section, fields in self.REQUIRED_FIELDS.items():
            if section not in self.config:
                errors.append(f"缺少配置章节: {section}")
                continue
            
            for field in fields:
                if field not in self.config[section]:
                    errors.append(f"缺少配置字段: {section}.{field}")
        
        vehicle_types = self.config.get('scraping', {}).get('vehicle_types', {})
        for type_id, type_config in vehicle_types.items():
            if type_config.get('enabled', True):
                pages = type_config.get('pages', 0)
                if pages < 0:
                    errors.append(f"类型{type_id}的页数不能为负数")
        
        schedule_config = self.config.get('schedule', {})
        if schedule_config.get('mode') == 'times':
            times = schedule_config.get('times', [])
            for time_str in times:
                try:
                    parts = time_str.split(':')
                    hour = int(parts[0])
                    minute = int(parts[1]) if len(parts) > 1 else 0
                    if not (0 <= hour <= 23 and 0 <= minute <= 59):
                        errors.append(f"时间格式错误: {time_str}")
                except (ValueError, IndexError):
                    errors.append(f"时间格式错误: {time_str}")
        
        return len(errors) == 0, errors
    
    def save(self, config_file: str = None):
        """保存配置到文件"""
        save_path = config_file or self.config_file
        
        try:
            with open(save_path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            logger.info(f"配置已保存到: {save_path}")
            return True
        except Exception as e:
            logger.error(f"保存配置失败: {e}")
            return False
    
    def reload(self):
        """重新加载配置"""
        self._load_config()
    
    @classmethod
    def create_default_config(cls, config_file: str = 'config.json'):
        """创建默认配置文件"""
        default_config = cls.DEFAULT_CONFIG.copy()
        
        try:
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(default_config, f, ensure_ascii=False, indent=2)
            logger.info(f"默认配置文件已创建: {config_file}")
            return True
        except Exception as e:
            logger.error(f"创建默认配置文件失败: {e}")
            return False


def load_config_from_file(config_file: str = 'config.json') -> Dict[int, Dict[str, Any]]:
    """
    从配置文件加载爬取配置（兼容旧接口）
    
    Args:
        config_file: 配置文件路径
    
    Returns:
        启用的车辆类型字典
    """
    manager = ConfigManager(config_file)
    return manager.get_enabled_vehicle_types()


# 类型提示导入
from typing import Tuple
