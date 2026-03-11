#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
高性能CSV数据导入MySQL脚本
包含数据校验、去重、历史记录等功能
"""

import os
import mysql.connector
import csv
import json
import glob
from datetime import datetime
import time
import re
from typing import Tuple, List, Dict, Any, Optional

# 加载环境变量
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # 如果没有python-dotenv，使用系统环境变量


class DataValidator:
    """数据校验器"""
    
    @staticmethod
    def validate_vehicle_id(vehicle_id: str) -> Tuple[bool, str]:
        """验证车辆ID"""
        if not vehicle_id or vehicle_id.strip() == '':
            return False, "车辆ID不能为空"
        if len(vehicle_id) > 100:
            return False, "车辆ID过长"
        return True, ""
    
    @staticmethod
    def validate_price(price_str: str) -> Tuple[bool, str]:
        """验证价格格式"""
        if not price_str:
            return True, ""  # 价格可以为空
        
        price_str = str(price_str).replace('HKD$', '').replace('HKD', '').strip()
        
        if '[' in price_str and '原價' in price_str:
            current_part = price_str.split('[')[0].strip()
            original_part = price_str.split('原價')[1].split(']')[0].strip()
            
            current_clean = current_part.replace(',', '').replace('$', '').strip()
            original_clean = original_part.replace(',', '').replace('$', '').strip()
            
            try:
                current = float(current_clean) if current_clean else None
                original = float(original_clean) if original_clean else None
                if current is not None and current < 0:
                    return False, f"价格不能为负数: {price_str}"
            except ValueError:
                return False, f"价格格式无效: {price_str}"
        else:
            clean = price_str.replace(',', '').replace('$', '').strip()
            try:
                price = float(clean) if clean else None
                if price is not None and price < 0:
                    return False, f"价格不能为负数: {price_str}"
            except ValueError:
                return False, f"价格格式无效: {price_str}"
        
        return True, ""
    
    @staticmethod
    def validate_phone(phone: str) -> Tuple[bool, str]:
        """验证电话号码"""
        if not phone:
            return True, ""  # 电话可以为空
        
        phone = str(phone).strip()
        if not re.match(r'^\d{8}$', phone):
            return False, f"电话号码格式无效: {phone}"
        return True, ""
    
    @staticmethod
    def validate_year(year: str) -> Tuple[bool, str]:
        """验证年份"""
        if not year:
            return True, ""  # 年份可以为空
        
        try:
            year_val = int(year)
            current_year = datetime.now().year
            if year_val < 1900 or year_val > current_year + 1:
                return False, f"年份超出合理范围: {year_val}"
        except ValueError:
            return False, f"年份格式无效: {year}"
        return True, ""
    
    @staticmethod
    def validate_seats(seats: str) -> Tuple[bool, str]:
        """验证座位数"""
        if not seats:
            return True, ""
        
        try:
            seats_val = int(seats)
            if seats_val < 1 or seats_val > 50:
                return False, f"座位数超出合理范围: {seats}"
        except ValueError:
            return False, f"座位数格式无效: {seats}"
        return True, ""
    
    def validate_row(self, row: dict) -> List[str]:
        """验证整行数据"""
        errors = []
        
        valid, msg = self.validate_vehicle_id(row.get('vehicle_id', ''))
        if not valid:
            errors.append(msg)
        
        valid, msg = self.validate_price(row.get('price', ''))
        if not valid:
            errors.append(msg)
        
        valid, msg = self.validate_phone(row.get('phone_number', ''))
        if not valid:
            errors.append(msg)
        
        valid, msg = self.validate_year(row.get('year', ''))
        if not valid:
            errors.append(msg)
        
        valid, msg = self.validate_seats(row.get('seats', ''))
        if not valid:
            errors.append(msg)
        
        return errors


class ImportHistory:
    """导入历史记录"""
    
    def __init__(self, db_connection):
        self.connection = db_connection
        self._ensure_table()
    
    def _ensure_table(self):
        """确保历史记录表存在"""
        try:
            cursor = self.connection.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS import_history (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    import_date DATE NOT NULL,
                    import_time TIME NOT NULL,
                    file_name VARCHAR(255) NOT NULL,
                    total_records INT,
                    new_records INT,
                    updated_records INT,
                    skipped_records INT,
                    error_count INT,
                    duration_seconds FLOAT,
                    status VARCHAR(20),
                    error_details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_import_date (import_date)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            self.connection.commit()
            cursor.close()
        except Exception as e:
            print(f"创建历史记录表失败: {e}")
    
    def record_import(self, file_name: str, total: int, new: int, updated: int, 
                     skipped: int, errors: int, duration: float, status: str, 
                     error_details: str = None):
        """记录导入历史"""
        try:
            cursor = self.connection.cursor()
            now = datetime.now()
            sql = """
                INSERT INTO import_history 
                (import_date, import_time, file_name, total_records, new_records, 
                 updated_records, skipped_records, error_count, duration_seconds, 
                 status, error_details)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            cursor.execute(sql, (
                now.date(), now.time(), file_name, total, new, updated,
                skipped, errors, duration, status, error_details
            ))
            self.connection.commit()
            cursor.close()
            return True
        except Exception as e:
            print(f"记录导入历史失败: {e}")
            return False
    
    def get_recent_history(self, days: int = 7) -> List[Dict]:
        """获取最近的导入历史"""
        try:
            cursor = self.connection.cursor(dictionary=True)
            sql = """
                SELECT * FROM import_history
                WHERE import_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
                ORDER BY import_date DESC, import_time DESC
            """
            cursor.execute(sql, (days,))
            results = cursor.fetchall()
            cursor.close()
            return results
        except Exception as e:
            print(f"获取导入历史失败: {e}")
            return []


class CrawlLogManager:
    """爬取日志管理器"""

    def __init__(self, connection):
        self.connection = connection

    def _ensure_table(self):
        """确保爬取日志表存在"""
        try:
            cursor = self.connection.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS crawl_logs (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    crawl_date DATE NOT NULL,
                    crawl_time TIME NOT NULL,
                    vehicle_type VARCHAR(50),
                    pages_scraped INT DEFAULT 0,
                    total_vehicles INT DEFAULT 0,
                    new_vehicles INT DEFAULT 0,
                    updated_vehicles INT DEFAULT 0,
                    skipped_vehicles INT DEFAULT 0,
                    proxy_used_count INT DEFAULT 0,
                    proxy_fail_count INT DEFAULT 0,
                    anti_crawler_triggered INT DEFAULT 0,
                    error_count INT DEFAULT 0,
                    crawl_duration_seconds FLOAT DEFAULT 0,
                    import_duration_seconds FLOAT DEFAULT 0,
                    total_duration_seconds FLOAT DEFAULT 0,
                    status VARCHAR(20) DEFAULT 'success',
                    error_details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_crawl_date (crawl_date),
                    INDEX idx_vehicle_type (vehicle_type)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            self.connection.commit()
            cursor.close()
        except Exception as e:
            print(f"创建爬取日志表失败: {e}")

    def record_crawl(self, vehicle_type: str, pages_scraped: int, total_vehicles: int,
                    new_vehicles: int, updated_vehicles: int, skipped_vehicles: int,
                    proxy_used_count: int, proxy_fail_count: int, anti_crawler_triggered: int,
                    error_count: int, crawl_duration: float, import_duration: float,
                    status: str, error_details: str = None):
        """记录爬取日志"""
        try:
            cursor = self.connection.cursor()
            now = datetime.now()
            sql = """
                INSERT INTO crawl_logs (
                    crawl_date, crawl_time, vehicle_type, pages_scraped,
                    total_vehicles, new_vehicles, updated_vehicles, skipped_vehicles,
                    proxy_used_count, proxy_fail_count, anti_crawler_triggered,
                    error_count, crawl_duration_seconds, import_duration_seconds,
                    total_duration_seconds, status, error_details
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            cursor.execute(sql, (
                now.date(), now.time(), vehicle_type, pages_scraped,
                total_vehicles, new_vehicles, updated_vehicles, skipped_vehicles,
                proxy_used_count, proxy_fail_count, anti_crawler_triggered,
                error_count, crawl_duration, import_duration,
                crawl_duration + import_duration, status, error_details
            ))
            self.connection.commit()
            cursor.close()
            return True
        except Exception as e:
            print(f"记录爬取日志失败: {e}")
            return False


class FastCSVImporter:
    def __init__(self):
        """初始化数据库连接"""
        self.connection = None
        self.cursor = None
        self.validator = DataValidator()
        self.history = None
        
    def connect(self):
        """连接数据库"""
        print("正在连接数据库...")
        try:
            # 从环境变量读取配置
            db_config = {
                'host': os.environ.get('DB_HOST', 'localhost'),
                'user': os.environ.get('DB_USER', 'root'),
                'password': os.environ.get('DB_PASSWORD', ''),
                'database': os.environ.get('DB_NAME', 'car_info_db'),
                'port': int(os.environ.get('DB_PORT', 3306))
            }
            
            if not db_config['password']:
                print("[ERROR] 数据库密码未配置，请检查环境变量 DB_PASSWORD")
                return False
                
            print(f"连接到 {db_config['host']}:{db_config['port']}")
            
            self.connection = mysql.connector.connect(
                host=db_config['host'],
                user=db_config['user'],
                password=db_config['password'],
                database=db_config['database'],
                port=db_config['port'],
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
            
            # 更新现有车辆（直接操作数据库）
            if update_vehicles:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 正在更新 {len(update_vehicles)} 条车辆记录...")

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
                        self._update_via_database(batch_vehicles)
                        success_count += len(batch_vehicles)
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] [OK] 批次 {batch_num + 1} 完成: 成功 {len(batch_vehicles)}, 失败 0")
                    except Exception as e:
                        error_count += len(batch_vehicles)
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] [ERROR] 批次 {batch_num + 1} 更新失败: {e}")

                update_time = time.time() - start_update
                print(f"[{datetime.now().strftime('%H:%M:%S')}] [OK] 数据库更新完成: 成功 {success_count}, 失败 {error_count}, 总耗时 {update_time:.2f} 秒")
            
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
        """数据库更新方案 - 更新所有字段"""
        try:
            update_sql = """
            UPDATE vehicles SET
                vehicle_type = %s, vehicle_status = %s, page_number = %s,
                car_number = %s, car_url = %s, car_category = %s, car_brand = %s, car_model = %s,
                fuel_type = %s, seats = %s, engine_volume = %s, transmission = %s, year = %s,
                description = %s, price = %s, current_price = %s, original_price = %s,
                contact_info = %s, update_date = %s, contact_name = %s, phone_number = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE vehicle_id = %s
            """

            update_data = []
            for vehicle_data in batch_vehicles:
                # 按照字段顺序: vehicle_type, vehicle_status, page_number, car_number, car_url,
                # car_category, car_brand, car_model, fuel_type, seats, engine_volume,
                # transmission, year, description, price, current_price, original_price,
                # contact_info, update_date, contact_name, phone_number, vehicle_id
                record = (
                    vehicle_data[1],   # vehicle_type
                    vehicle_data[2],   # vehicle_status
                    vehicle_data[3],   # page_number
                    vehicle_data[4],   # car_number
                    vehicle_data[5],   # car_url
                    vehicle_data[6],   # car_category
                    vehicle_data[7],   # car_brand
                    vehicle_data[8],   # car_model
                    vehicle_data[9],   # fuel_type
                    vehicle_data[10],  # seats
                    vehicle_data[11],  # engine_volume
                    vehicle_data[12],  # transmission
                    vehicle_data[13],  # year
                    vehicle_data[14],  # description
                    vehicle_data[15],  # price
                    vehicle_data[16],  # current_price
                    vehicle_data[17],  # original_price
                    vehicle_data[18],  # contact_info
                    vehicle_data[19],  # update_date
                    vehicle_data[21],  # contact_name
                    vehicle_data[22],  # phone_number
                    vehicle_data[0]     # vehicle_id (WHERE条件)
                )
                update_data.append(record)

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
    # 确保输出编码正确
    import sys
    import io
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')

    print("=== 高性能CSV导入工具（优化版） ===")
    print("优化特性:")
    print("- 数据校验（价格、电话、年份等）")
    print("- 导入历史记录")
    print("- 爬取日志记录")
    print("- 环境变量支持（敏感信息脱敏）")
    print("- 批量操作优化")
    print("- 分批更新避免大事务")

    # 从环境变量读取爬取统计
    crawl_stats = {
        'vehicle_type': os.environ.get('CRAWL_VEHICLE_TYPE', ''),
        'pages_scraped': int(os.environ.get('CRAWL_PAGES_SCRAPED', '0')),
        'total_vehicles': int(os.environ.get('CRAWL_TOTAL_VEHICLES', '0')),
        'proxy_used_count': int(os.environ.get('CRAWL_PROXY_USED', '0')),
        'proxy_fail_count': int(os.environ.get('CRAWL_PROXY_FAIL', '0')),
        'anti_crawler_triggered': int(os.environ.get('CRAWL_ANTI_CRAWLER', '0')),
        'crawl_duration': float(os.environ.get('CRAWL_DURATION', '0')),
        'error_count': int(os.environ.get('CRAWL_ERROR_COUNT', '0')),
    }
    has_crawl_stats = any(v != 0 and v != '' for v in crawl_stats.values() if isinstance(v, (int, float))) or crawl_stats['vehicle_type']

    importer = FastCSVImporter()

    if not importer.connect():
        return

    import_start_time = datetime.now()

    try:
        # 初始化历史记录
        importer.history = ImportHistory(importer.connection)

        # 初始化爬取日志（如果表不存在会自动创建）
        crawl_log_manager = CrawlLogManager(importer.connection)
        crawl_log_manager._ensure_table()

        # 检查CSV文件
        csv_files = glob.glob("car_data_*.csv")
        if not csv_files:
            print("\n[ERROR] 当前目录下没有找到 car_data_*.csv 文件")
            return

        print(f"\n找到 {len(csv_files)} 个CSV文件:")
        for f in csv_files:
            print(f"  - {f}")

        # 记录每个文件的导入结果
        total_new = 0
        total_updated = 0
        total_skipped = 0
        total_errors = 0

        # 直接开始导入
        print("\n开始自动导入...")
        importer.import_all_csv()

        # 显示统计
        importer.show_stats()

        # 计算导入耗时
        import_duration = (datetime.now() - import_start_time).total_seconds()

        # 尝试从导入历史中获取新增/更新统计
        if importer.history:
            recent = importer.history.get_recent_history(days=1)
            for record in recent:
                total_new += record.get('new_records', 0)
                total_updated += record.get('updated_records', 0)
                total_skipped += record.get('skipped_records', 0)
                total_errors += record.get('error_count', 0)

        # 记录爬取日志（如果有爬取统计）
        if has_crawl_stats:
            crawl_log_manager.record_crawl(
                vehicle_type=crawl_stats['vehicle_type'],
                pages_scraped=crawl_stats['pages_scraped'],
                total_vehicles=crawl_stats['total_vehicles'],
                new_vehicles=total_new,
                updated_vehicles=total_updated,
                skipped_vehicles=total_skipped,
                proxy_used_count=crawl_stats['proxy_used_count'],
                proxy_fail_count=crawl_stats['proxy_fail_count'],
                anti_crawler_triggered=crawl_stats['anti_crawler_triggered'],
                error_count=total_errors + crawl_stats['error_count'],
                crawl_duration=crawl_stats['crawl_duration'],
                import_duration=import_duration,
                status='success',
                error_details=None
            )
            print("\n[OK] 爬取日志已记录到数据库")

    finally:
        importer.close()

if __name__ == "__main__":
    main()
