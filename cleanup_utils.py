#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
清理工具模块 - 清理临时文件和调试文件
"""

import os
import glob
import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

class FileCleanup:
    def __init__(self):
        self.csv_dir = "."
        self.debug_dir = "debug_pages"
        self.csv_output_dir = "csv_output"
        
    def cleanup_after_success(self, keep_latest=False):
        """成功导入后清理文件"""
        try:
            # 清理CSV文件
            csv_cleaned = self._cleanup_csv_files(keep_latest)
            
            # 清理debug页面
            debug_cleaned = self._cleanup_debug_pages()
            
            # 清理csv_output目录
            output_cleaned = self._cleanup_csv_output_files(keep_latest)
            
            total_cleaned = csv_cleaned + debug_cleaned + output_cleaned
            
            if total_cleaned > 0:
                logger.info(f"清理完成：删除了 {total_cleaned} 个文件")
                logger.info(f"  - CSV文件: {csv_cleaned} 个")
                logger.info(f"  - Debug页面: {debug_cleaned} 个") 
                logger.info(f"  - 输出文件: {output_cleaned} 个")
            else:
                logger.info("无需清理文件")
                
            return total_cleaned
            
        except Exception as e:
            logger.error(f"清理文件时出错: {e}")
            return 0
    
    def _cleanup_csv_files(self, keep_latest=False):
        """清理主目录的CSV文件"""
        cleaned_count = 0
        try:
            csv_files = glob.glob(os.path.join(self.csv_dir, "car_data_*.csv"))
            
            if keep_latest and csv_files:
                # 按修改时间排序，保留最新的
                csv_files.sort(key=lambda x: os.path.getmtime(x))
                csv_files = csv_files[:-1]  # 保留最后一个（最新的）
            
            for csv_file in csv_files:
                try:
                    os.remove(csv_file)
                    cleaned_count += 1
                    logger.debug(f"删除CSV文件: {csv_file}")
                except Exception as e:
                    logger.warning(f"删除CSV文件失败 {csv_file}: {e}")
                    
        except Exception as e:
            logger.error(f"清理CSV文件时出错: {e}")
            
        return cleaned_count
    
    def _cleanup_debug_pages(self):
        """清理debug页面文件"""
        cleaned_count = 0
        try:
            if os.path.exists(self.debug_dir):
                debug_files = glob.glob(os.path.join(self.debug_dir, "*.html"))
                
                for debug_file in debug_files:
                    try:
                        os.remove(debug_file)
                        cleaned_count += 1
                        logger.debug(f"删除debug文件: {debug_file}")
                    except Exception as e:
                        logger.warning(f"删除debug文件失败 {debug_file}: {e}")
                        
        except Exception as e:
            logger.error(f"清理debug文件时出错: {e}")
            
        return cleaned_count
    
    def _cleanup_csv_output_files(self, keep_latest=False):
        """清理csv_output目录的文件"""
        cleaned_count = 0
        try:
            if os.path.exists(self.csv_output_dir):
                output_files = glob.glob(os.path.join(self.csv_output_dir, "*.csv"))
                
                if keep_latest and output_files:
                    # 按修改时间排序，保留最新的
                    output_files.sort(key=lambda x: os.path.getmtime(x))
                    output_files = output_files[:-1]  # 保留最后一个（最新的）
                
                for output_file in output_files:
                    try:
                        os.remove(output_file)
                        cleaned_count += 1
                        logger.debug(f"删除输出文件: {output_file}")
                    except Exception as e:
                        logger.warning(f"删除输出文件失败 {output_file}: {e}")
                        
        except Exception as e:
            logger.error(f"清理输出文件时出错: {e}")
            
        return cleaned_count
    
    def cleanup_old_files(self, days_old=7):
        """清理指定天数以前的文件"""
        cleaned_count = 0
        cutoff_time = datetime.now() - timedelta(days=days_old)
        
        try:
            # 清理旧的CSV文件
            csv_files = glob.glob(os.path.join(self.csv_dir, "car_data_*.csv"))
            for csv_file in csv_files:
                if os.path.getmtime(csv_file) < cutoff_time.timestamp():
                    try:
                        os.remove(csv_file)
                        cleaned_count += 1
                        logger.debug(f"删除旧CSV文件: {csv_file}")
                    except Exception as e:
                        logger.warning(f"删除旧CSV文件失败 {csv_file}: {e}")
            
            # 清理旧的debug文件
            if os.path.exists(self.debug_dir):
                debug_files = glob.glob(os.path.join(self.debug_dir, "*.html"))
                for debug_file in debug_files:
                    if os.path.getmtime(debug_file) < cutoff_time.timestamp():
                        try:
                            os.remove(debug_file)
                            cleaned_count += 1
                            logger.debug(f"删除旧debug文件: {debug_file}")
                        except Exception as e:
                            logger.warning(f"删除旧debug文件失败 {debug_file}: {e}")
            
            logger.info(f"清理 {days_old} 天前的文件完成：删除了 {cleaned_count} 个文件")
            
        except Exception as e:
            logger.error(f"清理旧文件时出错: {e}")
            
        return cleaned_count
    
    def get_disk_usage(self):
        """获取各目录的磁盘使用情况"""
        usage_info = {}
        
        try:
            # CSV文件大小
            csv_files = glob.glob(os.path.join(self.csv_dir, "car_data_*.csv"))
            csv_size = sum(os.path.getsize(f) for f in csv_files if os.path.exists(f))
            usage_info['csv_files'] = {'count': len(csv_files), 'size_mb': round(csv_size / 1024 / 1024, 2)}
            
            # Debug文件大小
            if os.path.exists(self.debug_dir):
                debug_files = glob.glob(os.path.join(self.debug_dir, "*.html"))
                debug_size = sum(os.path.getsize(f) for f in debug_files if os.path.exists(f))
                usage_info['debug_files'] = {'count': len(debug_files), 'size_mb': round(debug_size / 1024 / 1024, 2)}
            else:
                usage_info['debug_files'] = {'count': 0, 'size_mb': 0}
            
            # 输出文件大小
            if os.path.exists(self.csv_output_dir):
                output_files = glob.glob(os.path.join(self.csv_output_dir, "*.csv"))
                output_size = sum(os.path.getsize(f) for f in output_files if os.path.exists(f))
                usage_info['output_files'] = {'count': len(output_files), 'size_mb': round(output_size / 1024 / 1024, 2)}
            else:
                usage_info['output_files'] = {'count': 0, 'size_mb': 0}
            
            total_size = sum(info['size_mb'] for info in usage_info.values())
            total_count = sum(info['count'] for info in usage_info.values())
            
            usage_info['total'] = {'count': total_count, 'size_mb': round(total_size, 2)}
            
        except Exception as e:
            logger.error(f"获取磁盘使用情况时出错: {e}")
            
        return usage_info

def print_disk_usage(usage_info):
    """打印磁盘使用情况"""
    print("\n=== 磁盘使用情况 ===")
    print(f"CSV文件: {usage_info['csv_files']['count']} 个, {usage_info['csv_files']['size_mb']} MB")
    print(f"Debug文件: {usage_info['debug_files']['count']} 个, {usage_info['debug_files']['size_mb']} MB")
    print(f"输出文件: {usage_info['output_files']['count']} 个, {usage_info['output_files']['size_mb']} MB")
    print(f"总计: {usage_info['total']['count']} 个文件, {usage_info['total']['size_mb']} MB")

if __name__ == "__main__":
    # 测试清理功能
    import logging
    logging.basicConfig(level=logging.INFO)
    
    cleanup = FileCleanup()
    
    # 显示当前使用情况
    usage = cleanup.get_disk_usage()
    print_disk_usage(usage)
    
    # 执行清理
    cleaned = cleanup.cleanup_after_success()
    print(f"\n清理完成，删除了 {cleaned} 个文件")
    
    # 显示清理后使用情况
    usage_after = cleanup.get_disk_usage()
    print_disk_usage(usage_after)
