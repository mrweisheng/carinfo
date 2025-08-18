#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能CSV数据导入MySQL数据库脚本
支持自动文件名识别，高效批量导入
"""

import pandas as pd
import mysql.connector
from mysql.connector import Error
import csv
import os
import logging
import glob
from datetime import datetime
import json
import re

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class SmartMySQLImporter:
    def __init__(self, host='127.0.0.1', user='root', password='1qaz!QAZ2wsx@WSX', database='car_info_db', port=3306):
        """初始化MySQL连接"""
        self.host = host
        self.user = user
        self.password = password
        self.database = database
        self.port = port
        self.connection = None
        self.cursor = None
        
        # 车辆类型配置
        self.vehicle_types = {
            '1_1': {'type': 1, 'status': 1, 'name': '私家车-未售'},
            '1_2': {'type': 1, 'status': 2, 'name': '私家车-已售'},
            '2_1': {'type': 2, 'status': 1, 'name': '客货车-未售'},
            '2_2': {'type': 2, 'status': 2, 'name': '客货车-已售'},
            '3_1': {'type': 3, 'status': 1, 'name': '货车-未售'},
            '3_2': {'type': 3, 'status': 2, 'name': '货车-已售'},
            '4_1': {'type': 4, 'status': 1, 'name': '电单车-未售'},
            '4_2': {'type': 4, 'status': 2, 'name': '电单车-已售'},
            '5_1': {'type': 5, 'status': 1, 'name': '经典车-未售'},
            '5_2': {'type': 5, 'status': 2, 'name': '经典车-已售'}
        }
        
    def connect(self):
        """连接到MySQL数据库"""
        try:
            self.connection = mysql.connector.connect(
                host=self.host,
                user=self.user,
                password=self.password,
                database=self.database,
                port=self.port,
                charset='utf8mb4',
                autocommit=False
            )
            self.cursor = self.connection.cursor()
            logger.info("成功连接到MySQL数据库")
            return True
        except Error as e:
            logger.error(f"连接MySQL数据库失败: {e}")
            return False
    
    def disconnect(self):
        """断开数据库连接"""
        if self.cursor:
            self.cursor.close()
        if self.connection:
            self.connection.close()
            logger.info("已断开MySQL数据库连接")
    
    def detect_csv_files(self):
        """自动检测所有CSV文件"""
        csv_files = []
        pattern = "car_data_*.csv"
        
        for file_path in glob.glob(pattern):
            filename = os.path.basename(file_path)
            # 解析文件名：car_data_2_1.csv -> type=2, status=1
            parts = filename.replace('.csv', '').split('_')
            if len(parts) >= 3:
                vehicle_type = parts[2]
                vehicle_status = parts[3] if len(parts) > 3 else '1'
                key = f"{vehicle_type}_{vehicle_status}"
                
                if key in self.vehicle_types:
                    config = self.vehicle_types[key]
                    csv_files.append({
                        'file_path': file_path,
                        'filename': filename,
                        'vehicle_type': config['type'],
                        'vehicle_status': config['status'],
                        'name': config['name']
                    })
                    logger.info(f"检测到CSV文件: {filename} -> {config['name']}")
        
        return csv_files
    
    def import_csv_to_mysql(self, csv_file_info):
        """将CSV文件数据导入到MySQL数据库"""
        file_path = csv_file_info['file_path']
        vehicle_type = csv_file_info['vehicle_type']
        vehicle_status = csv_file_info['vehicle_status']
        name = csv_file_info['name']
        
        if not os.path.exists(file_path):
            logger.error(f"CSV文件不存在: {file_path}")
            return False
        
        try:
            # 读取CSV文件
            data = []
            with open(file_path, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    data.append(row)
            
            if not data:
                logger.warning(f"{name} CSV文件中没有数据")
                return False
            
            logger.info(f"开始导入 {name} 的 {len(data)} 条车辆数据")
            
            # Prepare insert statements
            insert_vehicle_sql = """
            INSERT INTO vehicles (
                vehicle_id, vehicle_type, vehicle_status, page_number,
                car_number, car_url, car_category, car_brand, car_model,
                fuel_type, seats, engine_volume, transmission, year,
                description, price, current_price, original_price, contact_info, update_date, extra_fields
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            ) ON DUPLICATE KEY UPDATE
                car_number = VALUES(car_number),
                car_url = VALUES(car_url),
                car_category = VALUES(car_category),
                car_brand = VALUES(car_brand),
                car_model = VALUES(car_model),
                fuel_type = VALUES(fuel_type),
                seats = VALUES(seats),
                engine_volume = VALUES(engine_volume),
                transmission = VALUES(transmission),
                year = VALUES(year),
                description = VALUES(description),
                price = VALUES(price),
                current_price = VALUES(current_price),
                original_price = VALUES(original_price),
                contact_info = VALUES(contact_info),
                update_date = VALUES(update_date),
                extra_fields = VALUES(extra_fields),
                updated_at = CURRENT_TIMESTAMP
            """
            
            insert_image_sql = """
            INSERT INTO vehicle_images (vehicle_id, image_url, image_order) 
            VALUES (%s, %s, %s)
            """
            
            # Batch insert data
            imported_vehicles = 0
            imported_images = 0
            
            for row in data:
                try:
                    # 处理扩展字段（JSON格式）
                    extra_fields = self._process_extra_fields(row, vehicle_type)
                    
                    # 解析价格字段
                    price_str = row.get('price', '')
                    current_price, original_price = self._parse_price(price_str)
                    
                    # 1. Insert vehicle basic information
                    vehicle_data = (
                        row.get('vehicle_id', ''),
                        vehicle_type,
                        vehicle_status,
                        int(row.get('page_number', 1)),
                        row.get('car_number', ''),
                        row.get('car_url', ''),
                        row.get('car_category', ''),
                        row.get('car_brand', ''),
                        row.get('car_model', ''),
                        row.get('fuel_type', ''),
                        row.get('seats', ''),
                        row.get('engine_volume', ''),
                        row.get('transmission', ''),
                        row.get('year', ''),
                        row.get('description', ''),
                        row.get('price', ''),
                        current_price,
                        original_price,
                        row.get('contact_info', ''),
                        row.get('update_date', ''),
                        json.dumps(extra_fields) if extra_fields else None
                    )
                    
                    self.cursor.execute(insert_vehicle_sql, vehicle_data)
                    imported_vehicles += 1
                    
                    # 2. Process image information (supports multiple images)
                    image_urls = row.get('image_urls', '').split('\n') if row.get('image_urls') else []
                    
                    # Filter empty strings and de-duplicate within the current row's image list
                    valid_image_urls = []
                    for url in image_urls:
                        url = url.strip()
                        if url and url not in valid_image_urls:
                            valid_image_urls.append(url)
                    
                    # Insert image records
                    for i, image_url in enumerate(valid_image_urls):
                        image_data = (row.get('vehicle_id', ''), image_url, i)
                        self.cursor.execute(insert_image_sql, image_data)
                        imported_images += 1
                    
                    # Commit every 50 records
                    if imported_vehicles % 50 == 0:
                        self.connection.commit()
                        logger.info(f"已导入 {imported_vehicles} 条车辆数据，{imported_images} 条图片数据")
                
                except Error as e:
                    logger.error(f"导入车辆 {row.get('vehicle_id', 'unknown')} 失败: {e}")
                    continue
            
            # Final commit
            self.connection.commit()
            
            logger.info(f"{name} 数据导入完成！")
            logger.info(f"成功导入 {imported_vehicles} 条车辆数据")
            logger.info(f"成功导入 {imported_images} 条图片数据")
            
            return True
            
        except Error as e:
            logger.error(f"导入数据失败: {e}")
            self.connection.rollback()
            return False
    
    def _process_extra_fields(self, row, vehicle_type):
        """处理扩展字段，根据车辆类型提取特殊属性"""
        extra_fields = {}
        
        # 解析价格字段
        price_str = row.get('price', '')
        if price_str:
            current_price, original_price = self._parse_price(price_str)
            if current_price is not None:
                extra_fields['current_price'] = current_price
            if original_price is not None:
                extra_fields['original_price'] = original_price
        
        if vehicle_type in [2, 3]:  # 客货车和货车
            # 从描述中提取客货车和货车特有信息
            description = row.get('description', '')
            
            # 提取载重量
            cargo_match = re.search(r'(\d+\.?\d*)\s*[吨|T]', description)
            if cargo_match:
                extra_fields['cargo_capacity'] = f"{cargo_match.group(1)}吨"
            
            # 提取车厢长度
            length_match = re.search(r'(\d+\.?\d*)\s*[米|m]', description)
            if length_match:
                extra_fields['body_length'] = f"{length_match.group(1)}米"
            
            # 提取车高
            height_match = re.search(r'(\d+\.?\d*)\s*米.*高', description)
            if height_match:
                extra_fields['body_height'] = f"{height_match.group(1)}米"
            
            # 提取油耗
            fuel_match = re.search(r'(\d+\.?\d*)L/100km', description)
            if fuel_match:
                extra_fields['fuel_consumption'] = f"{fuel_match.group(1)}L/100km"
            
            # 提取特殊功能
            features = []
            if '升降尾板' in description or '尾板' in description:
                features.append('升降尾板')
            if '原廠斗' in description:
                features.append('原廠斗')
            if '活動網' in description:
                features.append('活動網')
            if 'HIAB' in description or '吊机' in description:
                features.append('HIAB吊机')
            if '凍機' in description or '冻机' in description:
                features.append('冷冻设备')
            if '纖維斗' in description or '纤维斗' in description:
                features.append('纤维斗')
            if '孖屋' in description:
                features.append('孖屋')
            if features:
                extra_fields['features'] = ', '.join(features)
            
            # 货车特有属性
            if vehicle_type == 3:  # 货车
                # 提取发动机型号
                engine_match = re.search(r'(\d+)\s*節機', description)
                if engine_match:
                    extra_fields['engine_sections'] = f"{engine_match.group(1)}节机"
                
                # 提取车厢类型
                if '夾車' in description:
                    extra_fields['body_type'] = '夹车'
                elif '冷凍車' in description or '冻车' in description:
                    extra_fields['body_type'] = '冷冻车'
                elif '貨車' in description:
                    extra_fields['body_type'] = '货车'
                
                # 提取载重吨位
                ton_match = re.search(r'(\d+\.?\d*)TON', description, re.IGNORECASE)
                if ton_match:
                    extra_fields['tonnage'] = f"{ton_match.group(1)}TON"
        
        elif vehicle_type == 1:  # 私家车
            description = row.get('description', '')
            
            # 提取里程
            mileage_match = re.search(r'(\d+[,，]?\d*)\s*[km|公里]', description)
            if mileage_match:
                extra_fields['mileage'] = f"{mileage_match.group(1)}km"
            
            # 提取颜色
            color_match = re.search(r'([黑白红蓝银灰金棕绿紫])[色|色系]', description)
            if color_match:
                extra_fields['color'] = f"{color_match.group(1)}色"
        
        return extra_fields if extra_fields else None
    
    def _parse_price(self, price_str):
        """
        解析价格字符串，提取现价和原价
        返回: (current_price, original_price)
        """
        if not price_str:
            return None, None
        
        # 移除HKD$前缀
        price_str = price_str.replace('HKD$', '').replace('HKD', '').strip()
        
        current_price = None
        original_price = None
        
        # 处理 "54,000[原價$57,000]" 格式
        if '[' in price_str and '原價' in price_str:
            # 提取现价部分（方括号前）
            current_part = price_str.split('[')[0].strip()
            current_price = self._extract_number(current_part)
            
            # 提取原价部分（方括号内）
            original_part = price_str.split('原價')[1].split(']')[0].strip()
            original_price = self._extract_number(original_part)
        
        # 处理只有现价的格式 "208,000"
        else:
            current_price = self._extract_number(price_str)
        
        return current_price, original_price
    
    def _extract_number(self, price_str):
        """从价格字符串中提取数字"""
        if not price_str:
            return None
        
        # 移除逗号、$符号并转换为数字
        clean_str = price_str.replace(',', '').replace('$', '').strip()
        try:
            return float(clean_str)
        except:
            return None
    
    def auto_import_all_csv(self):
        """自动导入所有CSV文件"""
        csv_files = self.detect_csv_files()
        
        if not csv_files:
            logger.warning("未检测到任何CSV文件")
            return False
        
        logger.info(f"检测到 {len(csv_files)} 个CSV文件，开始自动导入...")
        
        success_count = 0
        total_vehicles = 0
        
        for csv_file in csv_files:
            logger.info(f"正在导入: {csv_file['name']} ({csv_file['filename']})")
            
            if self.import_csv_to_mysql(csv_file):
                success_count += 1
                # 统计导入的数据量
                try:
                    with open(csv_file['file_path'], 'r', encoding='utf-8-sig') as f:
                        reader = csv.DictReader(f)
                        vehicle_count = sum(1 for row in reader)
                        total_vehicles += vehicle_count
                except:
                    pass
            else:
                logger.error(f"导入失败: {csv_file['name']}")
        
        logger.info(f"自动导入完成！")
        logger.info(f"成功导入 {success_count}/{len(csv_files)} 个文件")
        logger.info(f"总计导入 {total_vehicles} 条车辆数据")
        
        return success_count > 0
    
    def show_statistics(self):
        """显示数据库统计信息"""
        try:
            # Statistics for vehicles
            self.cursor.execute("SELECT COUNT(*) FROM vehicles")
            total_vehicles = self.cursor.fetchone()[0]
            
            # Statistics by type
            self.cursor.execute("""
                SELECT vehicle_type, vehicle_status, COUNT(*) 
                FROM vehicles 
                GROUP BY vehicle_type, vehicle_status
            """)
            type_stats = self.cursor.fetchall()
            
            # Statistics for images
            self.cursor.execute("SELECT COUNT(*) FROM vehicle_images")
            total_images = self.cursor.fetchone()[0]
            
            print(f"\n=== MySQL数据库统计信息 ===")
            print(f"总车辆数: {total_vehicles}")
            print(f"总图片数: {total_images}")
            
            print(f"\n按类型统计:")
            type_names = {
                (1, 1): "私家车-未售",
                (1, 2): "私家车-已售",
                (2, 1): "客货车-未售",
                (2, 2): "客货车-已售",
                (3, 1): "货车-未售",
                (3, 2): "货车-已售",
                (4, 1): "电单车-未售",
                (4, 2): "电单车-已售",
                (5, 1): "经典车-未售",
                (5, 2): "经典车-已售"
            }
            
            for vehicle_type, vehicle_status, count in type_stats:
                type_name = type_names.get((vehicle_type, vehicle_status), f"类型{vehicle_type}-状态{vehicle_status}")
                print(f"  {type_name}: {count} 个车辆")
                
        except Error as e:
            logger.error(f"获取统计信息失败: {e}")

def main():
    """主函数"""
    print("=== 智能CSV数据导入MySQL数据库 ===")
    print("数据库配置: 127.0.0.1:3306, 用户: root")
    
    # Create importer instance
    importer = SmartMySQLImporter(
        host='127.0.0.1',
        user='root', 
        password='1qaz!QAZ2wsx@WSX',
        database='car_info_db',
        port=3306
    )
    
    # Connect to database
    if not importer.connect():
        print("数据库连接失败，请检查连接信息")
        return
    
    try:
        # 检测CSV文件
        csv_files = importer.detect_csv_files()
        if csv_files:
            print(f"\n检测到 {len(csv_files)} 个CSV文件:")
            for csv_file in csv_files:
                print(f"  - {csv_file['filename']} -> {csv_file['name']}")
        else:
            print("\n未检测到任何CSV文件")
            return
        
        # Select operation
        print("\n请选择操作:")
        print("1. 自动导入所有CSV文件")
        print("2. 手动选择CSV文件导入")
        print("3. 查看数据库统计信息")
        print("4. 执行完整导入流程")
        
        choice = input("请输入选择 (1-4): ").strip()
        
        if choice == "1":
            # Auto import all CSV files
            if importer.auto_import_all_csv():
                print("✅ 自动导入完成")
            else:
                print("❌ 自动导入失败")
        
        elif choice == "2":
            # Manual select CSV file
            print("\n可用的CSV文件:")
            for i, csv_file in enumerate(csv_files, 1):
                print(f"{i}. {csv_file['filename']} -> {csv_file['name']}")
            
            try:
                file_choice = int(input("请选择文件编号: ")) - 1
                if 0 <= file_choice < len(csv_files):
                    selected_file = csv_files[file_choice]
                    if importer.import_csv_to_mysql(selected_file):
                        print("✅ CSV数据导入成功")
                    else:
                        print("❌ CSV数据导入失败")
                else:
                    print("无效选择")
            except ValueError:
                print("请输入有效数字")
        
        elif choice == "3":
            # View statistics
            importer.show_statistics()
        
        elif choice == "4":
            # Full import process
            print("\n开始完整导入流程...")
            
            # 1. Auto import all CSV files
            print("1. 自动导入所有CSV文件...")
            if not importer.auto_import_all_csv():
                print("❌ 自动导入失败")
            
            # 2. Show statistics
            print("2. 显示统计信息...")
            importer.show_statistics()
            
            print("\n✅ 完整导入流程完成")
        
        else:
            print("无效选择")
    
    finally:
        # Disconnect
        importer.disconnect()

if __name__ == "__main__":
    main() 