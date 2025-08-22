#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生产级定时任务脚本
稳定可靠的汽车信息爬取调度器，适用于服务器部署
"""

import time
import subprocess
import sys
import os
import json
import signal
import threading
from datetime import datetime, timedelta
from pathlib import Path

class CarScheduler:
    def __init__(self):
        # 从配置文件读取间隔时间，默认4小时
        self.interval_hours = self._load_interval_config()
        self.log_file = "scheduler.log"
        self.pid_file = "scheduler.pid"
        self.running = True
        self.task_running = False
        
        # 设置信号处理（优雅停止）
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _load_interval_config(self):
        """从配置文件读取执行间隔"""
        try:
            with open('config.json', 'r', encoding='utf-8') as f:
                config = json.load(f)
            interval = config.get('schedule', {}).get('interval_hours', 4)
            return max(1, interval)  # 最少1小时
        except:
            return 4  # 默认4小时
    
    def _signal_handler(self, signum, frame):
        """处理停止信号"""
        self.log("收到停止信号，等待当前任务完成后退出...")
        self.running = False
    
    def log(self, message, level="INFO"):
        """记录日志到文件和控制台"""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log_msg = f"[{timestamp}] [{level}] {message}"
        
        # 打印到控制台
        print(log_msg)
        
        # 写入日志文件
        try:
            with open(self.log_file, 'a', encoding='utf-8') as f:
                f.write(log_msg + '\n')
        except:
            pass  # 日志写入失败不影响主流程
    
    def check_environment(self):
        """检查运行环境"""
        # 检查必要文件
        required_files = ['carinfo.py', 'config.json', 'import_to_mysql.py']
        missing_files = [f for f in required_files if not Path(f).exists()]
        
        if missing_files:
            self.log(f"缺少必要文件: {missing_files}", "ERROR")
            return False
        
        # 检查配置文件
        try:
            with open('config.json', 'r', encoding='utf-8') as f:
                config = json.load(f)
            
            vehicle_types = config.get('scraping', {}).get('vehicle_types', {})
            enabled_types = [t for t in vehicle_types.values() 
                           if t.get('enabled', True) and t.get('pages', 0) > 0]
            
            if not enabled_types:
                self.log("配置文件中没有启用的车辆类型", "WARNING")
                return False
            
            self.log(f"环境检查通过，发现 {len(enabled_types)} 个启用的车辆类型")
            
        except Exception as e:
            self.log(f"配置文件检查失败: {e}", "ERROR")
            return False
        
        return True
    
    def is_already_running(self):
        """检查是否已有调度器在运行"""
        if not Path(self.pid_file).exists():
            return False
        
        try:
            with open(self.pid_file, 'r') as f:
                pid = int(f.read().strip())
            
            # Windows和Linux的进程检查
            try:
                if os.name == 'nt':  # Windows
                    import psutil
                    return psutil.pid_exists(pid)
                else:  # Linux/Unix
                    os.kill(pid, 0)
                    return True
            except:
                # 进程不存在，删除PID文件
                os.remove(self.pid_file)
                return False
        except:
            return False
    
    def create_pid_file(self):
        """创建PID文件"""
        try:
            with open(self.pid_file, 'w') as f:
                f.write(str(os.getpid()))
        except Exception as e:
            self.log(f"创建PID文件失败: {e}", "WARNING")
    
    def remove_pid_file(self):
        """删除PID文件"""
        try:
            if Path(self.pid_file).exists():
                os.remove(self.pid_file)
        except:
            pass
    
    def run_task(self):
        """执行一次爬取任务"""
        if self.task_running:
            self.log("上一个任务还在运行中，跳过本次执行", "WARNING")
            return False
        
        self.task_running = True
        self.log("=== 开始执行爬取任务 ===")
        start_time = time.time()
        
        try:
            self.log("正在执行爬取任务，实时进度如下:")
            self.log("─" * 50)
            
            # 调用carinfo.py - 不捕获输出，直接显示
            result = subprocess.run(
                [sys.executable, 'carinfo.py'],
                text=True,
                encoding='utf-8',
                errors='ignore',
                timeout=7200,  # 2小时超时
                cwd=os.getcwd()
            )
            
            duration = time.time() - start_time
            
            self.log("─" * 50)
            
            if result.returncode == 0:
                self.log(f"✓ 爬取任务完成，耗时 {duration/60:.1f} 分钟")
                self.consecutive_failures = 0  # 重置失败计数
                self.task_running = False
                return True
            else:
                self.log(f"✗ 爬取任务失败，返回码: {result.returncode}，耗时 {duration/60:.1f} 分钟", "ERROR")
                self.consecutive_failures += 1
                self.task_running = False
                return False
            
        except subprocess.TimeoutExpired:
            self.log("✗ 爬取任务超时（超过2小时）", "ERROR")
            return False
        except FileNotFoundError:
            self.log("✗ 找不到carinfo.py文件", "ERROR")
            return False
        except Exception as e:
            self.log(f"✗ 爬取任务异常: {e}", "ERROR")
            return False
        finally:
            self.task_running = False
    
    def start(self):
        """启动调度器"""
        self.log("汽车信息爬取调度器启动")
        self.log(f"执行间隔: {self.interval_hours}小时")
        self.log(f"工作目录: {os.getcwd()}")
        self.log(f"PID: {os.getpid()}")
        
        # 环境检查
        if not self.check_environment():
            self.log("环境检查失败，退出", "ERROR")
            return False
        
        # 检查是否已在运行
        if self.is_already_running():
            self.log("检测到调度器已在运行，退出", "ERROR")
            return False
        
        # 创建PID文件
        self.create_pid_file()
        
        consecutive_failures = 0
        max_failures = 3
        
        try:
            # 启动时立即执行一次
            self.log("启动时立即执行一次任务...")
            success = self.run_task()
            
            if not success:
                consecutive_failures += 1
                self.log(f"首次执行失败 ({consecutive_failures}/{max_failures})", "WARNING")
            else:
                consecutive_failures = 0
            
            # 主循环
            while self.running:
                try:
                    # 计算下次执行时间
                    next_time = datetime.now() + timedelta(hours=self.interval_hours)
                    self.log(f"下次执行时间: {next_time.strftime('%Y-%m-%d %H:%M:%S')}")
                    
                    # 分段休眠，便于响应停止信号
                    total_sleep = self.interval_hours * 3600
                    sleep_interval = 60  # 每分钟检查一次
                    
                    while total_sleep > 0 and self.running:
                        sleep_time = min(sleep_interval, total_sleep)
                        time.sleep(sleep_time)
                        total_sleep -= sleep_time
                    
                    # 检查是否需要退出
                    if not self.running:
                        break
                    
                    # 执行任务
                    success = self.run_task()
                    
                    if success:
                        consecutive_failures = 0
                    else:
                        consecutive_failures += 1
                        self.log(f"连续失败次数: {consecutive_failures}/{max_failures}", "WARNING")
                        
                        # 连续失败太多次，增加等待时间
                        if consecutive_failures >= max_failures:
                            self.log("连续失败次数过多，等待30分钟后重试", "ERROR")
                            time.sleep(1800)  # 等待30分钟
                            consecutive_failures = 0  # 重置计数
                    
                except Exception as e:
                    self.log(f"主循环异常: {e}", "ERROR")
                    if self.running:
                        self.log("等待5分钟后重试...")
                        time.sleep(300)
        
        finally:
            self.remove_pid_file()
            self.log("调度器已停止")
        
        return True

def main():
    """主函数"""
    scheduler = CarScheduler()
    scheduler.start()

if __name__ == '__main__':
    main()
