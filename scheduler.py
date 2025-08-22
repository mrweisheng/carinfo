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
        # 从配置文件读取调度配置
        schedule_config = self._load_schedule_config()
        self.schedule_mode = schedule_config.get('mode', 'interval')  # interval 或 times
        self.interval_hours = schedule_config.get('interval_hours', 4)
        self.schedule_times = schedule_config.get('times', ['08:00', '12:00', '16:00', '20:00'])
        
        self.log_file = "scheduler.log"
        self.pid_file = "scheduler.pid"
        self.running = True
        self.task_running = False
        self.last_run_date = None  # 记录最后运行日期，避免重复执行
        
        # 设置信号处理（优雅停止）
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _load_schedule_config(self):
        """从配置文件读取调度配置"""
        try:
            with open('config.json', 'r', encoding='utf-8') as f:
                config = json.load(f)
            schedule_config = config.get('schedule', {})
            
            # 默认配置
            default_config = {
                'mode': 'times',  # 默认使用时间点模式
                'interval_hours': 4,
                'times': ['08:00', '12:00', '16:00', '20:00']
            }
            
            # 合并配置
            for key, default_value in default_config.items():
                if key not in schedule_config:
                    schedule_config[key] = default_value
            
            return schedule_config
        except:
            # 如果配置文件读取失败，返回默认配置
            return {
                'mode': 'times',
                'interval_hours': 4,
                'times': ['08:00', '12:00', '16:00', '20:00']
            }
    
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
                
                # 成功后自动清理文件
                try:
                    from cleanup_utils import FileCleanup
                    cleanup = FileCleanup()
                    self.log("开始清理临时文件...")
                    cleaned_count = cleanup.cleanup_after_success()
                    if cleaned_count > 0:
                        self.log(f"清理完成：删除了 {cleaned_count} 个临时文件")
                    else:
                        self.log("无需清理文件")
                except Exception as e:
                    self.log(f"清理文件时出错: {e}", "WARNING")
                
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
    
    def should_run_now(self):
        """检查当前时间是否应该执行任务"""
        now = datetime.now()
        current_time = now.strftime('%H:%M')
        current_date = now.strftime('%Y-%m-%d')
        
        if self.schedule_mode == 'times':
            # 时间点模式
            for schedule_time in self.schedule_times:
                schedule_hour, schedule_minute = map(int, schedule_time.split(':'))
                schedule_datetime = now.replace(hour=schedule_hour, minute=schedule_minute, second=0, microsecond=0)
                
                # 计算时间差
                time_diff = (now - schedule_datetime).total_seconds()
                
                # 如果在执行时间点后的5分钟内（允许执行窗口）
                if 0 <= time_diff <= 300:  # 0-5分钟内
                    task_key = f"{current_date}_{schedule_time}"
                    
                    # 检查这个时间点是否已经执行过
                    if self.last_run_date != task_key:
                        # 检查当前是否有任务在运行
                        if not self.task_running:
                            self.last_run_date = task_key
                            return True, f"定时执行 {schedule_time}"
                        else:
                            # 有任务在运行，跳过这个时间点
                            self.log(f"跳过 {schedule_time} 时间点：上一次任务仍在执行中", "WARNING")
                            self.last_run_date = task_key  # 标记为已处理，避免重复提示
                            return False, f"跳过 {schedule_time}（任务进行中）"
                
                # 如果超过执行窗口（5分钟后），标记该时间点为已跳过
                elif time_diff > 300:
                    task_key = f"{current_date}_{schedule_time}"
                    if self.last_run_date != task_key:
                        self.log(f"跳过 {schedule_time} 时间点：错过执行窗口", "INFO")
                        self.last_run_date = task_key  # 标记为已跳过
            
            return False, "未到执行时间"
        else:
            # 间隔模式（原有逻辑）
            return True, "间隔模式执行"
    
    def start(self):
        """启动调度器"""
        self.log("汽车信息爬取调度器启动")
        
        if self.schedule_mode == 'times':
            self.log(f"执行模式: 定时执行")
            self.log(f"执行时间: {', '.join(self.schedule_times)}")
        else:
            self.log(f"执行模式: 间隔执行")
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
            startup_success = self.run_task()
            if startup_success:
                self.log("启动任务执行成功")
            else:
                self.log("启动任务执行失败", "WARNING")
                consecutive_failures += 1
            
            self.log("调度器开始监控...")
            
            while self.running:
                try:
                    # 检查是否应该执行
                    should_run, reason = self.should_run_now()
                    
                    if should_run and not self.task_running:
                        self.log(f"触发任务执行: {reason}")
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
                    
                    # 检查间隔（每分钟检查一次）
                    time.sleep(60)
                    
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
