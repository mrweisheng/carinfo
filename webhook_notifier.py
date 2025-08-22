#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Webhook通知模块 - 通知下游业务方任务完成情况
"""

import requests
import json
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

class WebhookNotifier:
    def __init__(self):
        """初始化Webhook通知器"""
        self.webhook_url = "http://35.206.68.99:7878/webhook/update"
        self.auth_token = "ovBuPMJfJiBUn58h0r18TKYd"
        self.headers = {
            "Authorization": f"Bearer {self.auth_token}",
            "Content-Type": "application/json"
        }
        self.timeout = 30  # 30秒超时
        
    def notify_task_completion(self, task_type="scraping", status="completed", 
                             details=None, log_file=None):
        """
        通知任务完成
        
        Args:
            task_type: 任务类型 (scraping, import, etc.)
            status: 任务状态 (completed, failed, started)
            details: 详细信息字典
            log_file: 日志文件路径
        """
        try:
            # 生成时间戳
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            
            # 构建通知数据
            notification_data = {
                "message": f"{task_type} {status}",
                "ok": status in ["completed", "started"],
                "start_time": timestamp,
                "task_type": task_type,
                "status": status
            }
            
            # 添加日志文件信息
            if log_file and os.path.exists(log_file):
                notification_data["log_file"] = log_file
            
            # 添加详细信息
            if details:
                notification_data.update(details)
            
            # 添加进程ID
            notification_data["pid"] = os.getpid()
            
            logger.info(f"正在发送webhook通知到: {self.webhook_url}")
            logger.info(f"通知内容: {notification_data}")
            
            # 发送POST请求
            response = requests.post(
                self.webhook_url,
                headers=self.headers,
                json=notification_data,
                timeout=self.timeout
            )
            
            # 检查响应 (200-299都认为是成功)
            if 200 <= response.status_code < 300:
                logger.info(f"[OK] Webhook通知发送成功 (状态码: {response.status_code})")
                logger.info(f"响应: {response.text}")
                return True
            else:
                logger.warning(f"[WARNING] Webhook通知响应异常: {response.status_code}")
                logger.warning(f"响应内容: {response.text}")
                return False
                
        except requests.exceptions.Timeout:
            logger.error(f"[ERROR] Webhook通知超时 (>{self.timeout}秒)")
            return False
        except requests.exceptions.ConnectionError:
            logger.error(f"[ERROR] Webhook通知连接失败: {self.webhook_url}")
            return False
        except requests.exceptions.RequestException as e:
            logger.error(f"[ERROR] Webhook通知请求失败: {e}")
            return False
        except Exception as e:
            logger.error(f"[ERROR] Webhook通知发送异常: {e}")
            return False
    
    def notify_scraping_completed(self, vehicle_counts=None, import_success=True, 
                                cleaned_files=0, log_file=None):
        """
        通知爬取任务完成
        
        Args:
            vehicle_counts: 各类型车辆数量统计
            import_success: 导入是否成功
            cleaned_files: 清理的文件数量
            log_file: 日志文件路径
        """
        details = {
            "import_success": import_success,
            "cleaned_files": cleaned_files
        }
        
        if vehicle_counts:
            details["vehicle_counts"] = vehicle_counts
            details["total_vehicles"] = sum(vehicle_counts.values())
        
        status = "completed" if import_success else "failed"
        
        return self.notify_task_completion(
            task_type="scraping",
            status=status,
            details=details,
            log_file=log_file
        )
    
    def notify_import_completed(self, import_stats=None, log_file=None):
        """
        通知数据导入完成
        
        Args:
            import_stats: 导入统计信息
            log_file: 日志文件路径
        """
        details = {}
        if import_stats:
            details.update(import_stats)
        
        return self.notify_task_completion(
            task_type="import",
            status="completed",
            details=details,
            log_file=log_file
        )
    
    def notify_scheduler_started(self, log_file=None):
        """
        通知调度器启动
        
        Args:
            log_file: 日志文件路径
        """
        return self.notify_task_completion(
            task_type="scheduler",
            status="started",
            log_file=log_file
        )
    
    def test_webhook(self):
        """测试Webhook连接"""
        try:
            test_data = {
                "message": "webhook test",
                "ok": True,
                "test": True,
                "timestamp": datetime.now().strftime("%Y%m%d_%H%M%S"),
                "pid": os.getpid()
            }
            
            print(f"测试Webhook连接: {self.webhook_url}")
            print(f"测试数据: {test_data}")
            
            response = requests.post(
                self.webhook_url,
                headers=self.headers,
                json=test_data,
                timeout=self.timeout
            )
            
            if 200 <= response.status_code < 300:
                print(f"[OK] Webhook测试成功 (状态码: {response.status_code})")
                print(f"响应: {response.text}")
                return True
            else:
                print(f"[ERROR] Webhook测试失败: {response.status_code}")
                print(f"响应: {response.text}")
                return False
                
        except Exception as e:
            print(f"[ERROR] Webhook测试异常: {e}")
            return False

def main():
    """测试函数"""
    import logging
    logging.basicConfig(level=logging.INFO)
    
    notifier = WebhookNotifier()
    
    print("=== Webhook通知器测试 ===")
    
    # 测试连接
    if notifier.test_webhook():
        print("\n测试爬取完成通知...")
        notifier.notify_scraping_completed(
            vehicle_counts={"1": 100, "2": 50, "3": 200},
            import_success=True,
            cleaned_files=5
        )
    else:
        print("Webhook连接测试失败")

if __name__ == "__main__":
    main()
