#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据库联系人信息解析和更新脚本
直接处理数据库中的存量数据
"""

import re
import logging
import pymysql
from datetime import datetime

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class DatabaseContactProcessor:
    def __init__(self, host='localhost', port=3306, user='root', password='', database='car_info_db'):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.processed_count = 0
        self.updated_count = 0
        self.error_count = 0
    
    def connect_db(self):
        """连接数据库"""
        try:
            connection = pymysql.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database=self.database,
                charset='utf8mb4'
            )
            logger.info(f"成功连接到数据库: {self.database}")
            return connection
        except Exception as e:
            logger.error(f"数据库连接失败: {e}")
            return None
    
    def parse_contact_info(self, contact_str):
        """解析联系人信息，提取联系人和电话号码"""
        if not contact_str:
            return None, None
        
        contact_name = None
        phone_number = None
        
        # 处理格式："Ho Wun 電話:64316494" 或 "BEN NG 電話:93832082"
        if '電話:' in contact_str:
            # 分割联系人姓名和电话号码
            parts = contact_str.split('電話:')
            if len(parts) == 2:
                contact_name = parts[0].strip()
                phone_number = parts[1].strip()
        
        # 如果没有找到"電話:"，尝试其他格式
        elif '電話' in contact_str:
            # 处理可能的其他格式
            phone_match = re.search(r'電話\s*(\d+)', contact_str)
            if phone_match:
                phone_number = phone_match.group(1)
                # 提取联系人姓名（电话号码前的部分）
                name_part = contact_str.split('電話')[0].strip()
                if name_part:
                    contact_name = name_part
        
        # 处理其他可能的格式
        else:
            # 尝试提取电话号码（8位数字）
            phone_match = re.search(r'(\d{8})', contact_str)
            if phone_match:
                phone_number = phone_match.group(1)
                # 提取联系人姓名（电话号码前的部分）
                name_part = contact_str.replace(phone_number, '').strip()
                if name_part:
                    contact_name = name_part
        
        return contact_name, phone_number
    
    def process_database(self):
        """处理数据库中的联系人信息"""
        connection = self.connect_db()
        if not connection:
            return False
        
        try:
            cursor = connection.cursor()
            
            # 1. 首先确保字段存在
            logger.info("检查并添加联系人字段...")
            self.ensure_columns_exist(cursor)
            
            # 2. 获取所有需要处理的记录
            logger.info("获取需要处理的记录...")
            cursor.execute("""
                SELECT vehicle_id, contact_info 
                FROM vehicles 
                WHERE contact_info IS NOT NULL 
                AND contact_info != ''
                AND (contact_name IS NULL OR contact_name = '')
            """)
            
            records = cursor.fetchall()
            logger.info(f"找到 {len(records)} 条需要处理的记录")
            
            # 3. 处理每条记录
            for vehicle_id, contact_info in records:
                self.processed_count += 1
                
                try:
                    contact_name, phone_number = self.parse_contact_info(contact_info)
                    
                    if contact_name or phone_number:
                        # 更新数据库
                        update_sql = """
                            UPDATE vehicles 
                            SET contact_name = %s, phone_number = %s 
                            WHERE vehicle_id = %s
                        """
                        cursor.execute(update_sql, (
                            contact_name if contact_name else None,
                            phone_number if phone_number else None,
                            vehicle_id
                        ))
                        
                        self.updated_count += 1
                        logger.info(f"更新记录 {vehicle_id}: 联系人={contact_name}, 电话={phone_number}")
                    else:
                        logger.warning(f"无法解析联系人信息: {contact_info}")
                        self.error_count += 1
                        
                except Exception as e:
                    logger.error(f"处理记录 {vehicle_id} 时出错: {e}")
                    self.error_count += 1
            
            # 4. 提交更改
            connection.commit()
            logger.info("数据库更新完成")
            
            return True
            
        except Exception as e:
            logger.error(f"处理数据库时出错: {e}")
            connection.rollback()
            return False
        finally:
            cursor.close()
            connection.close()
    
    def ensure_columns_exist(self, cursor):
        """确保联系人字段存在"""
        try:
            # 检查 contact_name 字段
            cursor.execute("""
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS 
                WHERE TABLE_SCHEMA = DATABASE() 
                AND TABLE_NAME = 'vehicles' 
                AND COLUMN_NAME = 'contact_name'
            """)
            if cursor.fetchone()[0] == 0:
                cursor.execute("""
                    ALTER TABLE vehicles 
                    ADD COLUMN contact_name VARCHAR(100) COMMENT '联系人姓名'
                """)
                logger.info("添加 contact_name 字段")
            
            # 检查 phone_number 字段
            cursor.execute("""
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS 
                WHERE TABLE_SCHEMA = DATABASE() 
                AND TABLE_NAME = 'vehicles' 
                AND COLUMN_NAME = 'phone_number'
            """)
            if cursor.fetchone()[0] == 0:
                cursor.execute("""
                    ALTER TABLE vehicles 
                    ADD COLUMN phone_number VARCHAR(20) COMMENT '联系电话'
                """)
                logger.info("添加 phone_number 字段")
            
            # 创建索引
            cursor.execute("""
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS 
                WHERE TABLE_SCHEMA = DATABASE() 
                AND TABLE_NAME = 'vehicles' 
                AND INDEX_NAME = 'idx_contact_name'
            """)
            if cursor.fetchone()[0] == 0:
                cursor.execute("CREATE INDEX idx_contact_name ON vehicles(contact_name)")
                logger.info("创建 contact_name 索引")
            
            cursor.execute("""
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS 
                WHERE TABLE_SCHEMA = DATABASE() 
                AND TABLE_NAME = 'vehicles' 
                AND INDEX_NAME = 'idx_phone_number'
            """)
            if cursor.fetchone()[0] == 0:
                cursor.execute("CREATE INDEX idx_phone_number ON vehicles(phone_number)")
                logger.info("创建 phone_number 索引")
                
        except Exception as e:
            logger.error(f"确保字段存在时出错: {e}")
    
    def show_statistics(self):
        """显示处理统计"""
        connection = self.connect_db()
        if not connection:
            return
        
        try:
            cursor = connection.cursor()
            
            # 统计信息
            cursor.execute("""
                SELECT 
                    COUNT(*) as total_records,
                    COUNT(CASE WHEN contact_info IS NOT NULL AND contact_info != '' THEN 1 END) as has_contact_info,
                    COUNT(CASE WHEN contact_name IS NOT NULL AND contact_name != '' THEN 1 END) as has_contact_name,
                    COUNT(CASE WHEN phone_number IS NOT NULL AND phone_number != '' THEN 1 END) as has_phone_number
                FROM vehicles
            """)
            
            stats = cursor.fetchone()
            total, has_contact_info, has_contact_name, has_phone_number = stats
            
            print(f"\n=== 数据库统计信息 ===")
            print(f"总记录数: {total}")
            print(f"有联系人信息的记录: {has_contact_info}")
            print(f"已解析联系人姓名的记录: {has_contact_name}")
            print(f"已解析电话号码的记录: {has_phone_number}")
            
            # 显示示例
            cursor.execute("""
                SELECT vehicle_id, contact_info, contact_name, phone_number
                FROM vehicles 
                WHERE contact_name IS NOT NULL 
                AND contact_name != ''
                LIMIT 5
            """)
            
            examples = cursor.fetchall()
            if examples:
                print(f"\n=== 解析示例 ===")
                for i, (vehicle_id, contact_info, contact_name, phone_number) in enumerate(examples, 1):
                    print(f"示例 {i}: {contact_info} → 联系人:{contact_name}, 电话:{phone_number}")
            
        except Exception as e:
            logger.error(f"获取统计信息时出错: {e}")
        finally:
            cursor.close()
            connection.close()


def main():
    """主函数"""
    print("=== 数据库联系人信息解析工具 ===")
    print()
    
    # 数据库连接配置
    host = input("数据库主机 (默认: localhost): ").strip() or 'localhost'
    port = int(input("数据库端口 (默认: 3306): ").strip() or '3306')
    user = input("数据库用户名 (默认: root): ").strip() or 'root'
    password = input("数据库密码: ").strip()
    database = input("数据库名 (默认: car_info_db): ").strip() or 'car_info_db'
    
    # 创建处理器
    processor = DatabaseContactProcessor(host, port, user, password, database)
    
    # 显示当前统计
    print(f"\n正在连接数据库并获取当前统计...")
    processor.show_statistics()
    
    # 确认是否继续
    confirm = input(f"\n是否开始处理数据库中的联系人信息? (y/N): ").strip().lower()
    if confirm != 'y':
        print("操作已取消")
        return
    
    # 处理数据库
    print(f"\n开始处理数据库...")
    success = processor.process_database()
    
    if success:
        print(f"\n=== 处理完成 ===")
        print(f"总处理记录数: {processor.processed_count}")
        print(f"成功更新记录数: {processor.updated_count}")
        print(f"错误记录数: {processor.error_count}")
        if processor.processed_count > 0:
            print(f"成功率: {processor.updated_count/processor.processed_count*100:.1f}%")
        
        # 显示更新后的统计
        print(f"\n更新后的统计信息:")
        processor.show_statistics()
    else:
        print("处理失败，请检查数据库连接和权限")


if __name__ == "__main__":
    main()
