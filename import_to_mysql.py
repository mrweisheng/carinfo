#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
简化高性能CSV数据导入MySQL脚本
"""

import mysql.connector
import csv
import os
import json
import glob
from datetime import datetime
import time
import requests

class FastCSVImporter:
    def __init__(self):
        """初始化数据库连接"""
        self.connection = None
        self.cursor = None
        
    def connect(self):
        """连接数据库"""
        print("正在连接数据库...")
        try:
            self.connection = mysql.connector.connect(
                host='103.117.122.192',
                user='root',
                password='1qaz!QAZ2wsx@WSX',
                database='car_info_db',
                port=3306,
                charset='utf8mb4',
                autocommit=False,
                buffered=True,
                connect_timeout=30,
                use_unicode=True,
                sql_mode=''
            )
            self.cursor = self.connection.cursor(buffered=True)
            
            # 性能优化设置
            optimizations = [
                "SET SESSION unique_checks = 0",                # 禁用唯一性检查
                "SET SESSION sql_log_bin = 0",                  # 禁用二进制日志
                "SET SESSION innodb_lock_wait_timeout = 300",   # 增加锁等待超时
                "SET SESSION bulk_insert_buffer_size = 67108864"   # 设置批量插入缓冲
            ]
            
            for opt in optimizations:
                try:
                    self.cursor.execute(opt)
                except Exception as e:
                    print(f"警告: 优化设置失败 - {e}")
                    
            self.connection.commit()
            print("[OK] 数据库连接成功")
            return True
        except Exception as e:
            print(f"[ERROR] 数据库连接失败: {e}")
            return False
    
    def get_existing_ids(self, vehicle_ids):
        """批量获取已存在的车辆ID"""
        if not vehicle_ids:
            return set()
            
        existing_ids = set()
        batch_size = 10000  # 增加批次大小
        
        for i in range(0, len(vehicle_ids), batch_size):
            batch = vehicle_ids[i:i + batch_size]
            placeholders = ','.join(['%s'] * len(batch))
            query = f"SELECT vehicle_id FROM vehicles WHERE vehicle_id IN ({placeholders})"
            self.cursor.execute(query, batch)
            existing_ids.update(row[0] for row in self.cursor.fetchall())
            
        return existing_ids
    
    def parse_price(self, price_str):
        """解析价格字符串"""
        if not price_str:
            return None, None
            
        try:
            # 移除HKD$前缀
            price_str = str(price_str).replace('HKD$', '').replace('HKD', '').strip()
            
            current_price = None
            original_price = None
            
            # 处理 "54,000[原價$57,000]" 格式
            if '[' in price_str and '原價' in price_str:
                # 提取现价部分（方括号前）
                current_part = price_str.split('[')[0].strip()
                current_price = self._extract_price_number(current_part)
                
                # 提取原价部分（方括号内）
                original_part = price_str.split('原價')[1].split(']')[0].strip()
                original_price = self._extract_price_number(original_part)
            
            # 处理只有现价的格式 "60,000"
            else:
                current_price = self._extract_price_number(price_str)
            
            return current_price, original_price
        except:
            pass
        return None, None
    
    def _extract_price_number(self, price_str):
        """从价格字符串中提取数字"""
        if not price_str:
            return None
        
        # 移除逗号、$符号并转换为数字
        clean_str = price_str.replace(',', '').replace('$', '').strip()
        try:
            return float(clean_str)
        except:
            return None
    
    def clean_data(self, value):
        """清理数据"""
        if value is None or value == '':
            return ''
        str_value = str(value).strip()
        if str_value.endswith('.0'):
            return str_value[:-2]
        return str_value
    
    def import_csv(self, csv_file):
        """导入单个CSV文件"""
        if not os.path.exists(csv_file):
            print(f"[ERROR] 文件不存在: {csv_file}")
            return False
            
        print(f"\n开始导入: {csv_file}")
        
        try:
            # 读取CSV数据
            with open(csv_file, 'r', encoding='utf-8-sig') as f:
                data = list(csv.DictReader(f))
            
            if not data:
                print("[ERROR] CSV文件为空")
                return False
                
            print(f"读取到 {len(data)} 条记录")
            
            # 检查必要字段
            if 'vehicle_id' not in data[0] or 'sale_status' not in data[0]:
                print("[ERROR] CSV文件缺少必要字段 (vehicle_id, sale_status)")
                return False
            
            # 获取车辆类型
            filename = os.path.basename(csv_file)
            vehicle_type = 3  # 默认类型
            if 'car_data_' in filename:
                try:
                    vehicle_type = int(filename.split('_')[2].split('.')[0])
                except:
                    pass
            
            # 获取已存在的ID
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 开始查询已存在的车辆ID...")
            start_query = time.time()
            vehicle_ids = [row.get('vehicle_id', '') for row in data]
            existing_ids = self.get_existing_ids(vehicle_ids)
            query_time = time.time() - start_query
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 查询完成，发现 {len(existing_ids)} 个已存在的记录，耗时 {query_time:.2f} 秒")
            
            # 准备数据
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 开始准备数据...")
            start_prepare = time.time()
            new_vehicles = []
            update_vehicles = []
            new_images = []
            
            for row in data:
                vehicle_id = row.get('vehicle_id', '')
                if not vehicle_id:
                    continue
                    
                # 解析价格
                current_price, original_price = self.parse_price(row.get('price', ''))
                
                # 处理状态
                vehicle_status = 2 if row.get('sale_status', '未售') == '已售' else 1
                
                # 处理扩展字段
                extra_fields = None
                if row.get('extra_fields'):
                    try:
                        extra_fields = json.loads(row.get('extra_fields'))
                    except:
                        pass
                
                # 准备车辆数据
                vehicle_data = (
                    vehicle_id, vehicle_type, vehicle_status,
                    int(row.get('page_number', 1)),
                    self.clean_data(row.get('car_number')),
                    row.get('car_url', ''),
                    row.get('car_category', ''),
                    row.get('car_brand', ''),
                    row.get('car_model', ''),
                    row.get('fuel_type', ''),
                    self.clean_data(row.get('seats')),
                    row.get('engine_volume', ''),
                    row.get('transmission', ''),
                    self.clean_data(row.get('year')),
                    row.get('description', ''),
                    row.get('price', ''),
                    current_price, original_price,
                    row.get('contact_info', ''),
                    row.get('update_date', ''),
                    json.dumps(extra_fields) if extra_fields else None,
                    row.get('contact_name', ''),
                    self.clean_data(row.get('phone_number'))
                )
                
                if vehicle_id in existing_ids:
                    update_vehicles.append(vehicle_data)
                else:
                    new_vehicles.append(vehicle_data)
                    
                    # 只为新车辆处理图片
                    image_urls = row.get('image_urls', '').split('\n') if row.get('image_urls') else []
                    for i, url in enumerate(image_urls):
                        url = url.strip()
                        if url:
                            new_images.append((vehicle_id, url, i))
            
            prepare_time = time.time() - start_prepare
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 数据准备完成，新增: {len(new_vehicles)} 条，更新: {len(update_vehicles)} 条，图片: {len(new_images)} 条，耗时 {prepare_time:.2f} 秒")
            
            # 执行批量操作
            start_time = datetime.now()
            print(f"[{start_time.strftime('%H:%M:%S')}] 开始执行数据库操作...")
            
            # 插入新车辆
            if new_vehicles:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 正在插入 {len(new_vehicles)} 条新车辆记录...")
                start_insert = time.time()
                insert_sql = """
                INSERT IGNORE INTO vehicles (
                    vehicle_id, vehicle_type, vehicle_status, page_number,
                    car_number, car_url, car_category, car_brand, car_model,
                    fuel_type, seats, engine_volume, transmission, year,
                    description, price, current_price, original_price, 
                    contact_info, update_date, extra_fields, contact_name, phone_number
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 
                    %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """
                
                # 一次性插入所有新车辆
                self.cursor.executemany(insert_sql, new_vehicles)
                self.connection.commit()
                insert_time = time.time() - start_insert
                print(f"[{datetime.now().strftime('%H:%M:%S')}] [OK] 新增 {len(new_vehicles)} 条车辆记录，耗时 {insert_time:.2f} 秒")
            
            # 更新现有车辆（使用API批量更新）
            if update_vehicles:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 正在通过API更新 {len(update_vehicles)} 条车辆记录...")
                start_update = time.time()
                
                # 分批处理，每次100条
                batch_size = 100
                total_batches = (len(update_vehicles) + batch_size - 1) // batch_size
                success_count = 0
                error_count = 0
                
                for batch_num in range(total_batches):
                    start_idx = batch_num * batch_size
                    end_idx = min(start_idx + batch_size, len(update_vehicles))
                    batch_vehicles = update_vehicles[start_idx:end_idx]
                    
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 处理批次 {batch_num + 1}/{total_batches}，{len(batch_vehicles)} 条记录...")
                    
                    try:
                        # 准备API请求数据，按照API文档格式
                        api_updates = []
                        for vehicle_data in batch_vehicles:
                            # 构建更新字段，移除None值
                            fields = {}
                            if vehicle_data[1]: fields["vehicle_type"] = vehicle_data[1]
                            if vehicle_data[2] is not None: fields["vehicle_status"] = int(vehicle_data[2])
                            if vehicle_data[3]: fields["page_number"] = vehicle_data[3]
                            if vehicle_data[4]: fields["car_number"] = vehicle_data[4]
                            if vehicle_data[5]: fields["car_url"] = vehicle_data[5]
                            if vehicle_data[6]: fields["car_category"] = vehicle_data[6]
                            if vehicle_data[7]: fields["car_brand"] = vehicle_data[7]
                            if vehicle_data[8]: fields["car_model"] = vehicle_data[8]
                            if vehicle_data[9]: fields["fuel_type"] = vehicle_data[9]
                            if vehicle_data[10] is not None: fields["seats"] = vehicle_data[10]
                            if vehicle_data[11]: fields["engine_volume"] = vehicle_data[11]
                            if vehicle_data[12]: fields["transmission"] = vehicle_data[12]
                            if vehicle_data[13] is not None: fields["year"] = vehicle_data[13]
                            if vehicle_data[14]: fields["description"] = vehicle_data[14]
                            if vehicle_data[15]: fields["price"] = vehicle_data[15]
                            if vehicle_data[16] is not None: fields["current_price"] = float(vehicle_data[16])
                            if vehicle_data[17] is not None: fields["original_price"] = float(vehicle_data[17])
                            if vehicle_data[18]: fields["contact_info"] = vehicle_data[18]
                            if vehicle_data[19]: fields["update_date"] = vehicle_data[19]
                            if vehicle_data[21]: fields["contact_name"] = vehicle_data[21]
                            if vehicle_data[22]: fields["phone_number"] = vehicle_data[22]
                            
                            api_updates.append({
                                "vehicle_id": vehicle_data[0],
                                "fields": fields
                            })
                        
                        # 调用API
                        api_url = "https://www.eazycar.top/server/api/vehicles/batch-update"
                        payload = {"updates": api_updates}
                        
                        response = requests.post(api_url, json=payload, timeout=60)
                        
                        if response.status_code == 200:
                            result = response.json()
                            batch_success = result.get('data', {}).get('success_count', 0)
                            batch_error = result.get('data', {}).get('error_count', 0)
                            success_count += batch_success
                            error_count += batch_error
                            
                            print(f"[{datetime.now().strftime('%H:%M:%S')}] [OK] 批次 {batch_num + 1} 完成: 成功 {batch_success}, 失败 {batch_error}")
                            
                            if batch_error > 0:
                                errors = result.get('data', {}).get('errors', [])
                                for error in errors[:3]:  # 只显示前3个错误
                                    print(f"  错误: {error}")
                        else:
                            print(f"[{datetime.now().strftime('%H:%M:%S')}] [ERROR] API调用失败: {response.status_code}")
                            print(f"  响应: {response.text[:200]}...")
                            
                            # 回退到数据库更新
                            print(f"[{datetime.now().strftime('%H:%M:%S')}] 回退到数据库更新...")
                            self._update_via_database(batch_vehicles)
                            success_count += len(batch_vehicles)
                            
                    except requests.exceptions.RequestException as e:
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] [ERROR] API请求异常: {e}")
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] 回退到数据库更新...")
                        self._update_via_database(batch_vehicles)
                        success_count += len(batch_vehicles)
                    except Exception as e:
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] [ERROR] 批次处理失败: {e}")
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] 回退到数据库更新...")
                        self._update_via_database(batch_vehicles)
                        success_count += len(batch_vehicles)
                
                update_time = time.time() - start_update
                print(f"[{datetime.now().strftime('%H:%M:%S')}] [OK] 更新完成: 成功 {success_count}, 失败 {error_count}, 总耗时 {update_time:.2f} 秒")
            
            # 插入图片（仅新车辆，更新车辆不处理图片）
            if new_images:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 正在插入 {len(new_images)} 条图片记录（仅新车辆）...")
                start_images = time.time()
                image_sql = "INSERT IGNORE INTO vehicle_images (vehicle_id, image_url, image_order) VALUES (%s, %s, %s)"
                self.cursor.executemany(image_sql, new_images)
                self.connection.commit()
                images_time = time.time() - start_images
                print(f"[{datetime.now().strftime('%H:%M:%S')}] [OK] 新增 {len(new_images)} 条图片记录，耗时 {images_time:.2f} 秒")
            else:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 无需处理图片（无新车辆）")
            
            # 计算耗时
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            print(f"[OK] 导入完成！总计处理 {len(data)} 条记录，耗时 {duration:.1f} 秒")
            return True
            
        except Exception as e:
            print(f"[ERROR] 导入失败: {e}")
            if self.connection:
                self.connection.rollback()
            return False
    
    def import_all_csv(self):
        """导入所有CSV文件"""
        csv_files = glob.glob("car_data_*.csv")
        if not csv_files:
            print("[ERROR] 未找到任何 car_data_*.csv 文件")
            return
        
        print(f"找到 {len(csv_files)} 个CSV文件")
        for csv_file in csv_files:
            print(f"- {csv_file}")
        
        success_count = 0
        start_time = datetime.now()
        
        for csv_file in csv_files:
            if self.import_csv(csv_file):
                success_count += 1
        
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()
        
        print(f"\n=== 导入完成 ===")
        print(f"成功导入: {success_count}/{len(csv_files)} 个文件")
        print(f"总耗时: {duration:.1f} 秒")
    
    def show_stats(self):
        """显示统计信息"""
        try:
            self.cursor.execute("SELECT COUNT(*) FROM vehicles")
            total_vehicles = self.cursor.fetchone()[0]
            
            self.cursor.execute("SELECT COUNT(*) FROM vehicle_images")
            total_images = self.cursor.fetchone()[0]
            
            self.cursor.execute("""
                SELECT vehicle_status, COUNT(*) 
                FROM vehicles 
                GROUP BY vehicle_status
            """)
            status_stats = self.cursor.fetchall()
            
            print(f"\n=== 数据库统计 ===")
            print(f"总车辆数: {total_vehicles:,}")
            print(f"总图片数: {total_images:,}")
            
            for status, count in status_stats:
                status_name = "已售" if status == 2 else "未售"
                print(f"{status_name}: {count:,} 辆")
                
        except Exception as e:
            print(f"[ERROR] 获取统计失败: {e}")
    
    def _update_via_database(self, batch_vehicles):
        """数据库更新备选方案"""
        try:
            update_sql = """
            UPDATE vehicles SET 
                vehicle_status = %s, price = %s, current_price = %s, original_price = %s,
                contact_info = %s, update_date = %s, contact_name = %s, phone_number = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE vehicle_id = %s
            """
            
            update_data = []
            for vehicle_data in batch_vehicles:
                simplified = (
                    vehicle_data[2],   # vehicle_status
                    vehicle_data[15],  # price
                    vehicle_data[16],  # current_price
                    vehicle_data[17],  # original_price
                    vehicle_data[18],  # contact_info
                    vehicle_data[19],  # update_date
                    vehicle_data[21],  # contact_name
                    vehicle_data[22],  # phone_number
                    vehicle_data[0]    # vehicle_id
                )
                update_data.append(simplified)
            
            self.cursor.executemany(update_sql, update_data)
            self.connection.commit()
            
        except Exception as e:
            print(f"数据库更新失败: {e}")
            if self.connection:
                self.connection.rollback()
    
    def close(self):
        """关闭连接"""
        if self.cursor:
            self.cursor.close()
        if self.connection:
            self.connection.close()
        print("[OK] 数据库连接已关闭")

def main():
    """主函数"""
    print("=== 高性能CSV导入工具（优化版） ===")
    print("优化特性:")
    print("- 增加批量操作大小")
    print("- 优化MySQL性能参数")
    print("- 分批更新避免大事务")
    print("- 只更新变化的字段")
    print("- 图片仅处理新车辆")
    
    importer = FastCSVImporter()
    
    if not importer.connect():
        return
    
    try:
        # 检查CSV文件
        csv_files = glob.glob("car_data_*.csv")
        if not csv_files:
            print("\n[ERROR] 当前目录下没有找到 car_data_*.csv 文件")
            return
        
        print(f"\n找到 {len(csv_files)} 个CSV文件:")
        for f in csv_files:
            print(f"  - {f}")
        
        # 直接开始导入
        print("\n开始自动导入...")
        importer.import_all_csv()
        
        # 显示统计
        importer.show_stats()
        
    finally:
        importer.close()

if __name__ == "__main__":
    main()
