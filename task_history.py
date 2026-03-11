#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
调度器任务历史记录模块
提供任务执行历史记录、状态统计等功能
"""

import os
import json
import time
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from pathlib import Path


class TaskHistory:
    """任务历史记录管理器"""
    
    def __init__(self, history_file: str = 'task_history.json', max_records: int = 100):
        self.history_file = history_file
        self.max_records = max_records
        self.history = self._load_history()
    
    def _load_history(self) -> List[Dict]:
        """加载历史记录"""
        if not os.path.exists(self.history_file):
            return []
        
        try:
            with open(self.history_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return []
    
    def _save_history(self):
        """保存历史记录"""
        try:
            with open(self.history_file, 'w', encoding='utf-8') as f:
                json.dump(self.history, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    
    def add_task(self, start_time: datetime, end_time: datetime, success: bool,
                 vehicle_count: int = 0, error_message: str = None):
        """添加任务记录"""
        record = {
            'start_time': start_time.isoformat(),
            'end_time': end_time.isoformat(),
            'duration_seconds': (end_time - start_time).total_seconds(),
            'success': success,
            'vehicle_count': vehicle_count,
            'error_message': error_message
        }
        
        self.history.insert(0, record)
        
        # 限制记录数量
        if len(self.history) > self.max_records:
            self.history = self.history[:self.max_records]
        
        self._save_history()
    
    def get_recent_tasks(self, count: int = 10) -> List[Dict]:
        """获取最近的任务记录"""
        return self.history[:count]
    
    def get_stats(self, days: int = 7) -> Dict[str, Any]:
        """获取统计信息"""
        since = datetime.now() - timedelta(days=days)
        recent_tasks = [
            t for t in self.history 
            if datetime.fromisoformat(t['start_time']) >= since
        ]
        
        total = len(recent_tasks)
        success = sum(1 for t in recent_tasks if t['success'])
        failures = total - success
        total_duration = sum(t['duration_seconds'] for t in recent_tasks)
        total_vehicles = sum(t.get('vehicle_count', 0) for t in recent_tasks)
        
        return {
            'period_days': days,
            'total_tasks': total,
            'success_count': success,
            'failure_count': failures,
            'success_rate': (success / total * 100) if total > 0 else 0,
            'total_duration_minutes': total_duration / 60,
            'total_vehicles': total_vehicles,
            'avg_duration_minutes': (total_duration / total / 60) if total > 0 else 0,
            'avg_vehicles': (total_vehicles / total) if total > 0 else 0
        }
    
    def get_last_task(self) -> Optional[Dict]:
        """获取最后一次任务记录"""
        return self.history[0] if self.history else None
    
    def get_failed_tasks(self, count: int = 5) -> List[Dict]:
        """获取最近失败的任务"""
        return [t for t in self.history if not t['success']][:count]
    
    def clear_old_records(self, days: int = 30):
        """清除旧的记录"""
        since = datetime.now() - timedelta(days=days)
        self.history = [
            t for t in self.history 
            if datetime.fromisoformat(t['start_time']) >= since
        ]
        self._save_history()


class SchedulerStatus:
    """调度器状态管理器"""
    
    def __init__(self, status_file: str = 'scheduler.status'):
        self.status_file = status_file
        self.status = self._load_status()
    
    def _load_status(self) -> Dict:
        """加载状态"""
        if not os.path.exists(self.status_file):
            return self._default_status()
        
        try:
            with open(self.status_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return self._default_status()
    
    def _default_status(self) -> Dict:
        """默认状态"""
        return {
            'running': False,
            'pid': None,
            'start_time': None,
            'last_task_time': None,
            'last_task_result': None,
            'consecutive_failures': 0,
            'total_runs': 0,
            'total_successes': 0
        }
    
    def _save_status(self):
        """保存状态"""
        try:
            with open(self.status_file, 'w', encoding='utf-8') as f:
                json.dump(self.status, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    
    def start(self, pid: int):
        """记录调度器启动"""
        self.status['running'] = True
        self.status['pid'] = pid
        self.status['start_time'] = datetime.now().isoformat()
        self._save_status()
    
    def stop(self):
        """记录调度器停止"""
        self.status['running'] = False
        self.status['pid'] = None
        self._save_status()
    
    def task_started(self):
        """记录任务开始"""
        self.status['current_task_start'] = datetime.now().isoformat()
        self._save_status()
    
    def task_completed(self, success: bool, vehicle_count: int = 0):
        """记录任务完成"""
        now = datetime.now()
        self.status['last_task_time'] = now.isoformat()
        self.status['last_task_result'] = 'success' if success else 'failure'
        self.status['last_vehicle_count'] = vehicle_count
        self.status['total_runs'] = self.status.get('total_runs', 0) + 1
        
        if success:
            self.status['total_successes'] = self.status.get('total_successes', 0) + 1
            self.status['consecutive_failures'] = 0
        else:
            self.status['consecutive_failures'] = self.status.get('consecutive_failures', 0) + 1
        
        if 'current_task_start' in self.status:
            del self.status['current_task_start']
        
        self._save_status()
    
    def get_status(self) -> Dict:
        """获取当前状态"""
        return self.status.copy()
    
    def is_running(self) -> bool:
        """检查是否在运行"""
        return self.status.get('running', False)
    
    def get_uptime(self) -> Optional[float]:
        """获取运行时间（秒）"""
        if not self.status.get('running') or not self.status.get('start_time'):
            return None
        
        start = datetime.fromisoformat(self.status['start_time'])
        return (datetime.now() - start).total_seconds()
