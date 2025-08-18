#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能CSV数据导入MySQL数据库脚本
支持新的CSV格式（包含sale_status字段），实现更新策略
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
            # 解析文件名：car_data_3.csv -> type=3 (新格式，包含所有状态)
            parts = filename.replace('.csv', '').split('_')
            if len(parts) >= 3:
                vehicle_type = int(parts[2])
                
                # 新格式：car_data_3.csv 包含该类型的所有车辆（已售+未售）
                csv_files.append({
                    'file_path': file_path,
                    'filename': filename,
                    'vehicle_type': vehicle_type,
                    'name': f"车辆类型{vehicle_type}（包含已售未售）"
                })
                logger.info(f"检测到CSV文件: {filename} -> 车辆类型{vehicle_type}")
        
        return csv_files
    
    def update_existing_vehicle_status(self):
        """更新存量数据，将所有车辆的vehicle_status设置为未售状态"""
        try:
            # 更新所有vehicle_status为NULL或0的记录为1（未售）
            update_sql = """
            UPDATE vehicles 
            SET vehicle_status = 1, updated_at = CURRENT_TIMESTAMP 
            WHERE vehicle_status IS NULL OR vehicle_status = 0 OR vehicle_status NOT IN (1, 2)
            """
            
            self.cursor.execute(update_sql)
            updated_count = self.cursor.rowcount
            self.connection.commit()
            
            logger.info(f"已更新 {updated_count} 条存量数据的vehicle_status为未售状态")
            return updated_count
            
        except Error as e:
            logger.error(f"更新存量数据失败: {e}")
            self.connection.rollback()
            return 0
    
    def import_csv_to_mysql(self, csv_file_info):
        """将CSV文件数据导入到MySQL数据库"""
        file_path = csv_file_info['file_path']
        vehicle_type = csv_file_info['vehicle_type']
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
            
            # 检查CSV是否包含sale_status字段
            if 'sale_status' not in data[0]:
                logger.error(f"CSV文件缺少sale_status字段，无法确定车辆状态")
                return False
            
            # Prepare insert/update statements
            upsert_vehicle_sql = """
            INSERT INTO vehicles (
                vehicle_id, vehicle_type, vehicle_status, page_number,
                car_number, car_url, car_category, car_brand, car_model,
                fuel_type, seats, engine_volume, transmission, year,
                description, price, current_price, original_price, contact_info, update_date, extra_fields,
                contact_name, phone_number
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            ) ON DUPLICATE KEY UPDATE
                vehicle_status = VALUES(vehicle_status),
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
                contact_name = VALUES(contact_name),
                phone_number = VALUES(phone_number),
                updated_at = CURRENT_TIMESTAMP
            """
            
            # 只插入新图片，不更新已存在的图片
            insert_image_sql = """
            INSERT IGNORE INTO vehicle_images (vehicle_id, image_url, image_order) 
            VALUES (%s, %s, %s)
            """
            
            # Batch insert data
            imported_vehicles = 0
            updated_vehicles = 0
            imported_images = 0
            
            for row in data:
                try:
                    # 处理扩展字段（JSON格式）
                    extra_fields = self._process_extra_fields(row, vehicle_type)
                    
                    # 解析价格字段
                    price_str = row.get('price', '')
                    current_price, original_price = self._parse_price(price_str)
                    
                    # 转换sale_status为数据库格式
                    sale_status = row.get('sale_status', '未售')
                    vehicle_status = 2 if sale_status == '已售' else 1
                    
                    # 1. Insert/Update vehicle basic information
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
                        json.dumps(extra_fields) if extra_fields else None,
                        row.get('contact_name', ''),
                        row.get('phone_number', '')
                    )
                    
                    # 检查是否已存在该车辆
                    self.cursor.execute("SELECT vehicle_id FROM vehicles WHERE vehicle_id = %s", (row.get('vehicle_id', ''),))
                    exists = self.cursor.fetchone()
                    
                    self.cursor.execute(upsert_vehicle_sql, vehicle_data)
                    
                    if exists:
                        updated_vehicles += 1
                    else:
                        imported_vehicles += 1
                    
                    # 2. Process image information (只插入新图片，不更新已存在的)
                    image_urls = row.get('image_urls', '').split('\n') if row.get('image_urls') else []
                    
                    # Filter empty strings and de-duplicate within the current row's image list
                    valid_image_urls = []
                    for url in image_urls:
                        url = url.strip()
                        if url and url not in valid_image_urls:
                            valid_image_urls.append(url)
                    
                    # Insert image records (使用INSERT IGNORE避免重复)
                    for i, image_url in enumerate(valid_image_urls):
                        image_data = (row.get('vehicle_id', ''), image_url, i)
                        self.cursor.execute(insert_image_sql, image_data)
                        if self.cursor.rowcount > 0:  # 只有新插入的才计数
                            imported_images += 1
                    
                    # Commit every 50 records
                    if (imported_vehicles + updated_vehicles) % 50 == 0:
                        self.connection.commit()
                        logger.info(f"已处理 {imported_vehicles + updated_vehicles} 条车辆数据（新增:{imported_vehicles}, 更新:{updated_vehicles}），{imported_images} 条图片数据")
                
                except Error as e:
                    logger.error(f"处理车辆 {row.get('vehicle_id', 'unknown')} 失败: {e}")
                    continue
            
            # Final commit
            self.connection.commit()
            
            logger.info(f"{name} 数据导入完成！")
            logger.info(f"新增车辆: {imported_vehicles} 条")
            logger.info(f"更新车辆: {updated_vehicles} 条")
            logger.info(f"新增图片: {imported_images} 条")
            
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
        logger.info(f"总计处理 {total_vehicles} 条车辆数据")
        
        return success_count > 0
    
    def show_statistics(self):
        """显示数据库统计信息"""
        try:
            # Statistics for vehicles
            self.cursor.execute("SELECT COUNT(*) FROM vehicles")
            total_vehicles = self.cursor.fetchone()[0]
            
            # Statistics by type and status
            self.cursor.execute("""
                SELECT vehicle_type, vehicle_status, COUNT(*) 
                FROM vehicles 
                GROUP BY vehicle_type, vehicle_status
                ORDER BY vehicle_type, vehicle_status
            """)
            type_stats = self.cursor.fetchall()
            
            # Statistics for images
            self.cursor.execute("SELECT COUNT(*) FROM vehicle_images")
            total_images = self.cursor.fetchone()[0]
            
            print(f"\n=== MySQL数据库统计信息 ===")
            print(f"总车辆数: {total_vehicles}")
            print(f"总图片数: {total_images}")
            
            print(f"\n按类型和状态统计:")
            type_names = {
                1: "私家车",
                2: "客货车", 
                3: "货车",
                4: "电单车",
                5: "经典车"
            }
            status_names = {
                1: "未售",
                2: "已售"
            }
            
            for vehicle_type, vehicle_status, count in type_stats:
                type_name = type_names.get(vehicle_type, f"类型{vehicle_type}")
                status_name = status_names.get(vehicle_status, f"状态{vehicle_status}")
                print(f"  {type_name}-{status_name}: {count} 个车辆")
                
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
        print("5. 处理存量数据（设置所有车辆为未售状态）")
        
        choice = input("请输入选择 (1-5): ").strip()
        
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
            
            # 1. 处理存量数据
            print("1. 处理存量数据...")
            updated_count = importer.update_existing_vehicle_status()
            print(f"   已更新 {updated_count} 条存量数据")
            
            # 2. Auto import all CSV files
            print("2. 自动导入所有CSV文件...")
            if not importer.auto_import_all_csv():
                print("❌ 自动导入失败")
            
            # 3. Show statistics
            print("3. 显示统计信息...")
            importer.show_statistics()
            
            print("\n✅ 完整导入流程完成")
        
        elif choice == "5":
            # Handle existing data
            print("\n开始处理存量数据...")
            updated_count = importer.update_existing_vehicle_status()
            print(f"✅ 已更新 {updated_count} 条存量数据的vehicle_status为未售状态")
        
        else:
            print("无效选择")
    
    finally:
        # Disconnect
        importer.disconnect()

if __name__ == "__main__":
    main()
