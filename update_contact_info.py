#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
联系人信息解析和数据库更新脚本
用于处理存量数据中的联系人信息
"""

import re
import pandas as pd
import logging
from datetime import datetime

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class ContactInfoProcessor:
    def __init__(self):
        self.processed_count = 0
        self.updated_count = 0
        self.error_count = 0
    
    def parse_contact_info(self, contact_str):
        """解析联系人信息，提取联系人和电话号码"""
        if not contact_str or pd.isna(contact_str):
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
    
    def process_csv_file(self, csv_file_path, output_file_path=None):
        """处理CSV文件中的联系人信息"""
        try:
            # 读取CSV文件
            logger.info(f"正在读取CSV文件: {csv_file_path}")
            df = pd.read_csv(csv_file_path, encoding='utf-8-sig')
            
            # 检查是否存在contact_info字段
            if 'contact_info' not in df.columns:
                logger.error("CSV文件中没有找到 'contact_info' 字段")
                return None
            
            # 添加新字段
            if 'contact_name' not in df.columns:
                df['contact_name'] = ''
            if 'phone_number' not in df.columns:
                df['phone_number'] = ''
            
            # 处理每一行
            for index, row in df.iterrows():
                self.processed_count += 1
                
                contact_info = row.get('contact_info', '')
                if contact_info and not pd.isna(contact_info):
                    contact_name, phone_number = self.parse_contact_info(contact_info)
                    
                    if contact_name or phone_number:
                        df.at[index, 'contact_name'] = contact_name if contact_name else ''
                        df.at[index, 'phone_number'] = phone_number if phone_number else ''
                        self.updated_count += 1
                        logger.info(f"行 {index + 1}: 联系人={contact_name}, 电话={phone_number}")
                    else:
                        logger.warning(f"行 {index + 1}: 无法解析联系人信息: {contact_info}")
                        self.error_count += 1
                else:
                    logger.debug(f"行 {index + 1}: 联系人信息为空")
            
            # 保存更新后的文件
            if output_file_path is None:
                # 在原文件名基础上添加时间戳
                base_name = csv_file_path.rsplit('.', 1)[0]
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                output_file_path = f"{base_name}_updated_{timestamp}.csv"
            
            df.to_csv(output_file_path, index=False, encoding='utf-8-sig')
            logger.info(f"更新后的文件已保存: {output_file_path}")
            
            return df
            
        except Exception as e:
            logger.error(f"处理CSV文件时出错: {e}")
            return None
    
    def generate_sql_update_script(self, df, table_name='vehicles'):
        """生成SQL更新脚本"""
        sql_script = f"-- 联系人信息更新脚本 - 生成时间: {datetime.now()}\n"
        sql_script += f"-- 处理记录数: {self.processed_count}, 更新记录数: {self.updated_count}, 错误记录数: {self.error_count}\n\n"
        
        # 首先添加新字段（如果不存在）
        sql_script += f"-- 添加新字段（如果不存在）\n"
        sql_script += f"-- 检查并添加 contact_name 字段\n"
        sql_script += f"SET @sql = (SELECT IF(\n"
        sql_script += f"    (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS \n"
        sql_script += f"     WHERE TABLE_SCHEMA = DATABASE() \n"
        sql_script += f"     AND TABLE_NAME = '{table_name}' \n"
        sql_script += f"     AND COLUMN_NAME = 'contact_name') = 0,\n"
        sql_script += f"    'ALTER TABLE {table_name} ADD COLUMN contact_name VARCHAR(100) COMMENT ''联系人姓名'';',\n"
        sql_script += f"    'SELECT ''contact_name column already exists'' as message;'\n"
        sql_script += f"));\n"
        sql_script += f"PREPARE stmt FROM @sql;\n"
        sql_script += f"EXECUTE stmt;\n"
        sql_script += f"DEALLOCATE PREPARE stmt;\n\n"
        
        sql_script += f"-- 检查并添加 phone_number 字段\n"
        sql_script += f"SET @sql = (SELECT IF(\n"
        sql_script += f"    (SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS \n"
        sql_script += f"     WHERE TABLE_SCHEMA = DATABASE() \n"
        sql_script += f"     AND TABLE_NAME = '{table_name}' \n"
        sql_script += f"     AND COLUMN_NAME = 'phone_number') = 0,\n"
        sql_script += f"    'ALTER TABLE {table_name} ADD COLUMN phone_number VARCHAR(20) COMMENT ''联系电话'';',\n"
        sql_script += f"    'SELECT ''phone_number column already exists'' as message;'\n"
        sql_script += f"));\n"
        sql_script += f"PREPARE stmt FROM @sql;\n"
        sql_script += f"EXECUTE stmt;\n"
        sql_script += f"DEALLOCATE PREPARE stmt;\n\n"
        
        # 生成更新语句
        sql_script += f"-- 更新联系人信息\n"
        
        for index, row in df.iterrows():
            vehicle_id = row.get('vehicle_id', '')
            contact_name = row.get('contact_name', '')
            phone_number = row.get('phone_number', '')
            
            if contact_name or phone_number:
                # 转义单引号
                contact_name = contact_name.replace("'", "''") if contact_name else ''
                phone_number = phone_number.replace("'", "''") if phone_number else ''
                
                sql_script += f"UPDATE {table_name} SET "
                if contact_name:
                    sql_script += f"contact_name = '{contact_name}'"
                if phone_number:
                    if contact_name:
                        sql_script += f", phone_number = '{phone_number}'"
                    else:
                        sql_script += f"phone_number = '{phone_number}'"
                
                sql_script += f" WHERE vehicle_id = '{vehicle_id}';\n"
        
        return sql_script


def main():
    """主函数"""
    print("=== 联系人信息解析和数据库更新工具 ===")
    print()
    
    # 用户输入
    csv_file = input("请输入CSV文件路径: ").strip()
    
    if not csv_file:
        print("请输入有效的CSV文件路径")
        return
    
    # 创建处理器
    processor = ContactInfoProcessor()
    
    # 处理CSV文件
    print(f"\n开始处理文件: {csv_file}")
    df = processor.process_csv_file(csv_file)
    
    if df is not None:
        print(f"\n=== 处理完成 ===")
        print(f"总记录数: {processor.processed_count}")
        print(f"更新记录数: {processor.updated_count}")
        print(f"错误记录数: {processor.error_count}")
        print(f"成功率: {processor.updated_count/processor.processed_count*100:.1f}%")
        
        # 生成SQL脚本
        table_name = input("\n请输入数据库表名 (默认: vehicles): ").strip() or 'vehicles'
        sql_script = processor.generate_sql_update_script(df, table_name)
        
        # 保存SQL脚本
        sql_file = f"update_contact_info_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sql"
        with open(sql_file, 'w', encoding='utf-8') as f:
            f.write(sql_script)
        
        print(f"\nSQL更新脚本已保存: {sql_file}")
        print("\n请检查SQL脚本后执行数据库更新")
        
        # 显示前几条更新记录
        print(f"\n=== 更新记录示例 ===")
        updated_records = df[(df['contact_name'] != '') | (df['phone_number'] != '')]
        if not updated_records.empty:
            for i, (_, record) in enumerate(updated_records.head(5).iterrows()):
                print(f"记录 {i+1}: 联系人={record['contact_name']}, 电话={record['phone_number']}")
        else:
            print("没有找到可更新的记录")
    
    else:
        print("处理失败，请检查文件路径和格式")


if __name__ == "__main__":
    main()
