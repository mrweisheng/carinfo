#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
汽车信息爬取脚本
从28car.com网站爬取车辆信息并生成Excel文件
"""

import random
import time
import gzip
import http.client
import json
import zlib
import brotli
import logging
import os
import re
import pandas as pd
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import urljoin
import subprocess
import sys

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def load_config_from_file(config_file='config.json'):
    """从配置文件加载爬取配置"""
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        # 提取车辆类型配置
        vehicle_types = config.get('scraping', {}).get('vehicle_types', {})
        enabled_types = {}
        
        for type_id, type_config in vehicle_types.items():
            if type_config.get('enabled', True) and type_config.get('pages', 0) > 0:
                enabled_types[int(type_id)] = {
                    'name': type_config.get('name', f'类型{type_id}'),
                    'pages': type_config.get('pages', 0)
                }
        
        logger.info(f"从配置文件 {config_file} 加载了 {len(enabled_types)} 个启用的车辆类型")
        return enabled_types
        
    except FileNotFoundError:
        logger.error(f"配置文件 {config_file} 不存在")
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"配置文件 {config_file} 格式错误: {e}")
        return {}
    except Exception as e:
        logger.error(f"加载配置文件失败: {e}")
        return {}


class CarScraper:
    def __init__(self, PAGE, vehicle_type=1):
        self.PAGE = PAGE
        self.vehicle_type = vehicle_type
        self.car_data = []
        self.base_url = "https://dj1jklak2e.28car.com"
        self.output_dir = "."  # 当前目录
        
        # 创建debug目录
        self.debug_dir = "debug_pages"
        if not os.path.exists(self.debug_dir):
            os.makedirs(self.debug_dir)
            logger.info(f"创建debug目录: {self.debug_dir}")
        
        # 反爬虫处理相关变量
        self.anti_crawler_triggered = False  # 是否触发过反爬虫
        self.list_trigger_count = 0  # 列表页面触发次数
        self.detail_trigger_count = 0  # 详情页面触发次数
        self.current_page = 1  # 当前页面号
        self.current_detail_index = 0  # 当前详情爬取进度
        
        # 代理管理相关变量
        self.proxy_config = self._load_proxy_config()
        self.available_proxies = []
        self.failed_proxies = set()  # 记录失败的代理
        self.current_proxy = None
        self._init_proxies()
        
        # 车辆类型配置
        self.vehicle_types = {
            1: {'name': '私家车', 'param': 'h_f_ty=1'},
            2: {'name': '客货车', 'param': 'h_f_ty=2'},
            3: {'name': '货车', 'param': 'h_f_ty=3'},
            4: {'name': '电单车', 'param': 'h_f_ty=4'},
            5: {'name': '经典车', 'param': 'h_f_ty=5'}
        }

    def random_delay(self, min_seconds=1, max_seconds=3):
        """随机延迟，模拟真实用户行为"""
        delay = random.uniform(min_seconds, max_seconds)
        time.sleep(delay)
        return delay
    
    def human_like_delay(self):
        """模拟人类行为的延迟模式"""
        # 70%概率短延迟，30%概率长延迟
        if random.random() < 0.7:
            delay = random.uniform(0.8, 2.5)
        else:
            delay = random.uniform(2.0, 5.0)
        
        time.sleep(delay)
        return delay
    
    def _load_proxy_config(self):
        """加载代理配置文件"""
        config_file = os.path.join(self.output_dir, 'proxy_config.json')
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
            logger.info(f"成功加载代理配置文件: {config_file}")
            return config
        except FileNotFoundError:
            logger.warning(f"代理配置文件不存在: {config_file}，将使用直连")
            return {'proxies': [], 'retry_settings': {'max_retries': 3, 'retry_delay': 5, 'timeout': 30}, 'fallback_to_direct': True}
        except json.JSONDecodeError as e:
            logger.error(f"代理配置文件格式错误: {e}，将使用直连")
            return {'proxies': [], 'retry_settings': {'max_retries': 3, 'retry_delay': 5, 'timeout': 30}, 'fallback_to_direct': True}
    
    def _init_proxies(self):
        """初始化可用代理列表"""
        self.available_proxies = []
        for proxy in self.proxy_config.get('proxies', []):
            if proxy.get('enabled', True):
                self.available_proxies.append(proxy)
        
        logger.info(f"初始化完成，共有 {len(self.available_proxies)} 个可用代理")
        if self.available_proxies:
            self.current_proxy = random.choice(self.available_proxies)
            logger.info(f"当前使用代理: {self.current_proxy.get('name', 'unknown')}")
    
    def _get_random_proxy(self):
        """获取随机代理"""
        # 过滤掉失败的代理
        working_proxies = [p for p in self.available_proxies if p.get('name') not in self.failed_proxies]
        
        if not working_proxies:
            if self.proxy_config.get('fallback_to_direct', True):
                logger.warning("所有代理都失败，使用直连")
                return None
            else:
                # 重置失败代理列表，重新尝试
                logger.info("重置失败代理列表，重新尝试")
                self.failed_proxies.clear()
                working_proxies = self.available_proxies
        
        if working_proxies:
            proxy = random.choice(working_proxies)
            self.current_proxy = proxy
            return {
                'http': proxy.get('http'),
                'https': proxy.get('https')
            }
        return None
    
    def _mark_proxy_failed(self, proxy_name):
        """标记代理为失败"""
        if proxy_name:
            self.failed_proxies.add(proxy_name)
            logger.warning(f"代理 {proxy_name} 标记为失败")
    
    def _make_request_with_retry(self, url, headers, request_type='list'):
        """使用代理和重试机制发送HTTP请求"""
        retry_settings = self.proxy_config.get('retry_settings', {})
        max_retries = retry_settings.get('max_retries', 3)
        retry_delay = retry_settings.get('retry_delay', 5)
        timeout = retry_settings.get('timeout', 30)
        
        for attempt in range(max_retries + 1):
            try:
                # 获取代理
                proxies = self._get_random_proxy()
                proxy_name = self.current_proxy.get('name', 'direct') if self.current_proxy else 'direct'
                
                if proxies:
                    logger.info(f"尝试 {attempt + 1}/{max_retries + 1}: 使用代理 {proxy_name}")
                else:
                    logger.info(f"尝试 {attempt + 1}/{max_retries + 1}: 使用直连")
                
                # 发送请求
                response = requests.get(
                    url,
                    headers=headers,
                    proxies=proxies,
                    timeout=timeout,
                    allow_redirects=True
                )
                
                # 检查响应状态
                if response.status_code == 200:
                    # 尝试多种编码方式解码
                    decoded_html = None
                    encodings = ['big5', 'utf-8', 'gbk', 'gb2312', 'latin1']
                    
                    for encoding in encodings:
                        try:
                            decoded_html = response.content.decode(encoding, errors='replace')
                            logger.info(f"成功使用 {encoding} 编码解码")
                            break
                        except UnicodeDecodeError:
                            continue
                    
                    if decoded_html is None:
                        # 如果所有编码都失败，使用latin1作为后备
                        decoded_html = response.content.decode('latin1', errors='replace')
                        logger.warning("使用latin1编码作为后备方案")
                    
                    logger.info(f"响应状态码: {response.status_code}")
                    logger.info(f"响应内容长度: {len(decoded_html)}")
                    
                    # 检查是否触发反爬虫
                    if 'msg_busy.php' in decoded_html or 'busy' in decoded_html.lower():
                        logger.warning(f"{request_type}页面被重定向到busy页面，触发反爬虫机制")
                        # 处理反爬虫
                        if not self._handle_anti_crawler(request_type):
                            return None  # 终止爬取
                        # 继续重试
                        continue
                    
                    return decoded_html
                else:
                    logger.warning(f"HTTP请求失败，状态码: {response.status_code}")
                    raise requests.RequestException(f"HTTP {response.status_code}")
                    
            except (requests.RequestException, requests.Timeout, Exception) as e:
                logger.error(f"请求失败 (尝试 {attempt + 1}/{max_retries + 1}): {e}")
                
                # 标记当前代理为失败
                if self.current_proxy:
                    self._mark_proxy_failed(self.current_proxy.get('name'))
                
                # 如果不是最后一次尝试，等待后重试
                if attempt < max_retries:
                    logger.info(f"等待 {retry_delay} 秒后重试...")
                    time.sleep(retry_delay)
                else:
                    logger.error(f"所有重试都失败，放弃请求: {url}")
                    return None
        
        return None

    def get_date_code(self, decoded_html):
        '''通用的车辆列表提取方法，适用于所有车辆类型'''
        soup = BeautifulSoup(decoded_html, 'html.parser')
        
        # 保存页面内容用于调试
        debug_filename = f"debug_list_page_{int(time.time())}.html"
        debug_path = os.path.join(self.debug_dir, debug_filename)
        with open(debug_path, 'w', encoding='utf-8') as f:
            f.write(decoded_html)
        logger.info(f"页面内容已保存到: {debug_path}")
        
        # 查找所有包含Record注释的tr元素
        records = []
        for comment in soup.find_all(string=lambda text: isinstance(text, str) and 'Record' in text):
            if comment.strip().startswith('Record'):
                next_tr = comment.find_next('tr')
                if next_tr:
                    records.append(next_tr)

        # 如果上面的方法无效，尝试其他选择器
        if len(records) == 0:
            logger.info("未找到Record注释，尝试其他选择器...")
            records = soup.select('tr:has(td[onclick*="goDsp"])')

        # 如果还是没找到，尝试更宽松的选择器
        if len(records) == 0:
            logger.info("未找到goDsp选择器，尝试查找所有包含onclick的td...")
            onclick_tds = soup.find_all('td', onclick=True)
            logger.info(f"找到 {len(onclick_tds)} 个包含onclick的td元素")
            
            # 查找包含goDsp的td
            goDsp_tds = soup.find_all('td', onclick=lambda x: x and 'goDsp' in x)
            logger.info(f"找到 {len(goDsp_tds)} 个包含goDsp的td元素")
            
            # 尝试从这些td中找到对应的tr
            for td in goDsp_tds:
                tr = td.find_parent('tr')
                if tr and tr not in records:
                    records.append(tr)

        logger.info(f"总共找到 {len(records)} 个车辆记录")

        results = []
        for i, record in enumerate(records):
            # 提取日期信息
            date_cell = record.select_one('font[style*="color:#999999"]')
            if date_cell:
                date_text = date_cell.get_text(strip=True)
                # 提取日期部分（格式20/07）
                date_match = re.search(r'(\d{1,2}/\d{1,2})', date_text)
                date_value = date_match.group(1) if date_match else None
            else:
                date_value = None

            # 提取onclick的第2个参数（车辆ID）
            onclick_cell = record.select_one('td[onclick*="goDsp"]')
            if onclick_cell and 'onclick' in onclick_cell.attrs:
                onclick_value = onclick_cell['onclick']
                # 提取onclick的第2个参数
                param_match = re.search(r'goDsp\(.*?,\s*(\d+)\s*,', onclick_value)
                param_value = param_match.group(1) if param_match else None
            else:
                param_value = None

            # 提取已售未售状态
            sale_status = self._extract_sale_status(record)

            logger.info(f"记录 {i+1}: 日期={date_value}, 车辆ID={param_value}, 状态={sale_status}")

            results.append({
                'date': date_value,
                'code': param_value,
                'sale_status': sale_status
            })

        logger.info(f"成功提取 {len(results)} 个车辆信息")
        return results

    def _extract_sale_status(self, record):
        """提取车辆销售状态"""
        record_html = str(record)
        
        # 检查是否包含已售标识（更精确的检查）
        sold_indicators = [
            'sold.gif',
            'sold.png', 
            'sold.jpg',
            '已售',
            '已賣',
            'SOLD',
            '由於已售，聯絡人資料亦被保護中',
            '由于已售，联络人资料亦被保护中',
            'class="sold"'
        ]
        
        # 特殊检查：灰色文字且包含已售相关内容
        if 'style="color:#999999"' in record_html and ('已售' in record_html or '已賣' in record_html or 'sold' in record_html.lower()):
            return "已售"
        
        for indicator in sold_indicators:
            if indicator in record_html:
                return "已售"
        
        return "未售"



    def get_html_1(self, page):
        '''获取请求概览页返回的数据，page表示第几页'''
        # 使用车辆类型参数
        vehicle_config = self.vehicle_types.get(self.vehicle_type, self.vehicle_types[1])
        params = vehicle_config['param'] + '&h_page=' + str(page)
        url = f"https://dj1jklak2e.28car.com/sell_lst.php?{params}"
        
        # 使用简单的请求头
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1'
        }
        
        logger.info(f"请求URL: {url}")
        logger.info(f"车辆类型: {self.vehicle_type}")
        
        return self._make_request_with_retry(url, headers, 'list')



    def get_detail_content(self, h_vid):
        """获取页面内容"""
        url = f"https://dj1jklak2e.28car.com/sell_dsp.php?h_vid={h_vid}&h_vw=y"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1'
        }
        
        logger.info(f"请求详情页面URL: {url}")
        return self._make_request_with_retry(url, headers, 'detail')

    def extract_car_info(self, html_content, h_vid, initial_sale_status=None):
        """从详细表格中中提取车辆信息"""
        if html_content is None:
            logger.warning(f"车辆 {h_vid} 的HTML内容为空，跳过")
            return None
            
        soup = BeautifulSoup(html_content, 'html.parser')
        car_data = {}  # 放车辆信息
        
        # 直接使用列表页的销售状态，不再从详情页判断
        car_data['sale_status'] = initial_sale_status
        
        logger.info(f"车辆 {h_vid} 销售状态：{initial_sale_status}（来自列表页）")
        
        # 定位目标表格
        target_table = soup.find('table', {'width': '100%', 'style': 'height:100%'})
        
        # 如果没找到目标表格，尝试其他选择器
        if target_table is None:
            # 尝试其他可能的表格选择器
            target_table = soup.find('table', {'width': '100%'})
        
        if target_table is None:
            # 尝试查找包含车辆信息的表格
            tables = soup.find_all('table')
            for table in tables:
                if table.find('td', class_='frm_l') and table.find('td', class_='frm_t'):
                    target_table = table
                    break
        
        # 如果还是没找到，记录错误并返回空数据
        if target_table is None:
            logger.error(f"无法找到车辆详情表格，h_vid: {h_vid}")
            logger.error(f"页面内容长度: {len(html_content)}")
            # 保存页面内容用于调试
            debug_file = f"debug_page_{h_vid}.html"
            debug_path = os.path.join(self.debug_dir, debug_file)
            with open(debug_path, 'w', encoding='utf-8') as f:
                f.write(html_content)
            logger.error(f"页面内容已保存到: {debug_path}")
            return None

        # 定义需要提取的字段
        fields = {
            '編號': '編號',
            '車類': '車類',
            '車廠': '車廠',
            '型號': '型號',
            '燃炓': '燃炓',
            '座位': '座位',
            '容積': '容積',
            '傳動': '傳動',
            '年份': '年份',
            '簡評': '簡評',
            '售價': '售價',
            '聯絡人資料': '聯絡人資料',
            '更新日期': '更新日期',
            '網址': '網址'
        }

        # 提取表格中的文本信息
        rows = target_table.find_all('tr')
        for row in rows:
            label_td = row.find('td', class_='frm_l')
            value_td = row.find('td', class_='frm_t')

            if label_td and value_td:
                field_name = label_td.get_text(strip=True)
                if field_name in fields:
                    car_data[fields[field_name]] = value_td.get_text(strip=True)
        
        # 提取图片URL
        car_data['图片URLs'] = self.extract_image_urls(soup)
        
        # 处理价格解析
        if '售價' in car_data:
            price_text = car_data['售價']
            current_price, original_price = self._parse_price(price_text)
            car_data['current_price'] = current_price
            car_data['original_price'] = original_price
            car_data['price'] = price_text
        
        # 处理联系人信息解析
        if '聯絡人資料' in car_data:
            contact_text = car_data['聯絡人資料']
            contact_name, phone_number = self._parse_contact_info(contact_text)
            car_data['contact_name'] = contact_name
            car_data['phone_number'] = phone_number
        
        # 处理额外字段
        extra_fields = self._process_extra_fields(car_data)
        if extra_fields and car_data is not None:
            car_data.update(extra_fields)
        
        # 添加车辆ID
        car_data['h_vid'] = h_vid
        
        # 本地图片路径（空列表，因为不下载图片）
        car_data['本地图片路径'] = []

        return car_data

    def extract_image_urls(self, soup):
        """提取表格中的图片URL"""
        image_urls = []
        image_b_urls = []
        # 查找所有图片标签
        img_tags = soup.find_all('img')
        for img in img_tags:
            src = img.get('src')
            if src:
                # 转换为完整URL
                if src.startswith('http'):
                    full_url = src
                else:
                    full_url = urljoin(self.base_url, src)
                # 过滤掉不要的图片，如广告图片
                if 'data/image/sell' in full_url and '28car.com' in full_url:
                    image_urls.append(full_url)

        # 替换为大图的url
        for url in image_urls:
            new_url = url.replace("_m", "_b").replace("_s", "_b")
            image_b_urls.append(new_url)

        return image_b_urls

    def scrape_cars(self, h_vid_list, sale_status_list):
        """爬取多个车辆信息"""
        logger.info(f"开始爬取 {len(h_vid_list)} 个车辆信息")
        
        for i, (h_vid, sale_status) in enumerate(zip(h_vid_list, sale_status_list), 1):
            logger.info(f"正在处理第 {i}/{len(h_vid_list)} 个车辆，h_vid: {h_vid}, 状态: {sale_status}")
            
            try:
                # 无论已售未售都要爬取详情页面获取完整信息
                logger.info(f"正在爬取车辆 {h_vid} 的详细信息（状态：{sale_status}）")
                
                # 获取车辆详细页面内容
                html_content = self.get_detail_content(h_vid)
                
                # 检查是否返回None（反爬虫终止）
                if html_content is None:
                    logger.error(f"车辆 {h_vid} 因反爬虫终止而跳过")
                    return  # 终止整个爬取过程
                
                # 检查是否被重定向到busy页面
                if 'msg_busy.php' in html_content or 'busy' in html_content.lower():
                    logger.warning(f"车辆 {h_vid} 被重定向到busy页面，可能触发了反爬虫机制")
                    logger.warning("建议增加请求间隔时间或减少并发请求")
                    continue
                
                # 提取车辆信息（传递初始销售状态进行验证）
                car_info = self.extract_car_info(html_content, h_vid, sale_status)
                logger.info(f"车辆 {h_vid} 信息提取结果: {type(car_info)}")
                
                # 随机跳过某些车辆，模拟用户选择行为
                if random.random() < 0.05:  # 5%概率跳过
                    logger.info(f"随机跳过车辆 {h_vid}，模拟用户选择行为")
                    continue
                
                if car_info is not None:
                    self.car_data.append(car_info)
                    final_status = car_info.get('sale_status', '未知')
                    logger.info(f"成功提取车辆信息: {car_info.get('車廠', '')} {car_info.get('型號', '')} (状态: {final_status})")
                else:
                    logger.warning(f"车辆 {h_vid} 提取失败，跳过")
                    
                # 根据反爬虫状态调整延迟时间
                if self.anti_crawler_triggered:
                    delay = random.uniform(30, 60)  # 30-60秒
                    logger.info(f"反爬虫模式：延迟 {delay:.1f} 秒...")
                else:
                    delay = random.uniform(1, 2)  # 5-10秒
                    logger.info(f"正常模式：延迟 {delay:.1f} 秒...")
                
                time.sleep(delay)
                    
            except Exception as e:
                logger.error(f"处理车辆 {h_vid} 时出错: {e}")
                continue

        logger.info(f"爬取完成，共获取 {len(self.car_data)} 个车辆信息")

    def create_csv_file(self, filename=None, append_mode=False):
        """创建CSV文件，用于数据库导入"""
        if not self.car_data:
            logger.warning("没有数据可写入CSV")
            return None
        
        # 生成文件名
        if filename is None:
            filename = f"car_data_{self.vehicle_type}.csv"  # 包含所有状态的车辆
        
        csv_path = os.path.join(self.output_dir, filename)
        
        # 准备CSV数据
        csv_data = []
        
        # 获取当前时间戳作为页面编号（避免重复）
        import time
        current_timestamp = int(time.time())
        
        for i, car in enumerate(self.car_data, 1):
            # 解析价格
            price_str = car.get('售價', '')
            current_price, original_price = self._parse_price(price_str)
            
            # 处理扩展字段（根据车辆类型）
            extra_fields = self._process_extra_fields(car) if car is not None else {}
            
            row = {
                'vehicle_id': car.get('編號', ''),
                'page_number': current_timestamp + i,  # 使用时间戳+序号作为页面编号
                'car_number': car.get('編號', ''),
                'car_url': car.get('網址', ''),
                'car_category': car.get('車類', ''),
                'car_brand': car.get('車廠', ''),
                'car_model': car.get('型號', ''),
                'fuel_type': car.get('燃炓', ''),
                'seats': car.get('座位', ''),
                'engine_volume': car.get('容積', ''),
                'transmission': car.get('傳動', ''),
                'year': car.get('年份', ''),
                'description': car.get('簡評', ''),
                'price': car.get('售價', ''),
                'current_price': current_price,
                'original_price': original_price,
                'contact_info': car.get('聯絡人資料', ''),
                'contact_name': car.get('contact_name', ''),
                'phone_number': car.get('phone_number', ''),
                'update_date': car.get('更新日期', ''),
                'image_urls': '\n'.join(car.get('图片URLs', [])),
                'sale_status': car.get('sale_status', '未知'),
                'extra_fields': json.dumps(extra_fields) if extra_fields else ''
            }
            csv_data.append(row)
        
        # 写入CSV文件
        if csv_data:
            df = pd.DataFrame(csv_data)
            
            # 确保特定字段保持字符串格式，避免pandas自动转换
            string_columns = ['year', 'phone_number', 'seats', 'engine_volume']
            for col in string_columns:
                if col in df.columns:
                    df[col] = df[col].astype(str).str.replace('.0', '', regex=False)
            
            if append_mode and os.path.exists(csv_path):
                # 追加模式：读取现有文件并追加新数据
                existing_df = pd.read_csv(csv_path, encoding='utf-8-sig')
                combined_df = pd.concat([existing_df, df], ignore_index=True)
                combined_df.to_csv(csv_path, index=False, encoding='utf-8-sig')
                logger.info(f"数据已追加到现有CSV文件: {csv_path}")
            else:
                # 新建模式：创建新文件
                df.to_csv(csv_path, index=False, encoding='utf-8-sig')
                logger.info(f"CSV文件已保存: {csv_path}")
            
            return csv_path
        else:
            logger.warning("没有数据可写入CSV")
            return None

    def _parse_price(self, price_str):
        """解析价格字符串，提取现价和原价"""
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
    
    def _parse_contact_info(self, contact_str):
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
    
    def _process_extra_fields(self, car_data):
        """根据车辆类型处理扩展字段"""
        extra_fields = {}
        
        # 防护：如果car_data为None，返回空字典
        if car_data is None:
            return extra_fields
            
        description = car_data.get('簡評', '')
        
        if self.vehicle_type == 1:  # 私家车
            # 提取里程
            mileage_match = re.search(r'(\d+[,，]?\d*)\s*[km|公里]', description)
            if mileage_match:
                extra_fields['mileage'] = f"{mileage_match.group(1)}km"
            
            # 提取颜色
            color_match = re.search(r'([黑白红蓝银灰金棕绿紫])[色|色系]', description)
            if color_match:
                extra_fields['color'] = f"{color_match.group(1)}色"
            
            # 提取车况
            if '一手' in description:
                extra_fields['condition'] = '一手'
            elif '二手' in description:
                extra_fields['condition'] = '二手'
            
            # 提取变速箱类型
            if '自動' in description or '自动' in description:
                extra_fields['transmission_type'] = '自动'
            elif '手動' in description or '手动' in description:
                extra_fields['transmission_type'] = '手动'
            
            # 提取车身类型
            body_types = ['房車', 'SUV', 'MPV', '跑車', '掀背', '旅行車', '開篷']
            for body_type in body_types:
                if body_type in description:
                    extra_fields['body_type'] = body_type
                    break
        
        elif self.vehicle_type in [2, 3]:  # 客货车和货车
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
            if self.vehicle_type == 3:  # 货车
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
        
        elif self.vehicle_type == 4:  # 电单车
            # 提取排量
            cc_match = re.search(r'(\d+)\s*cc', description, re.IGNORECASE)
            if cc_match:
                extra_fields['engine_cc'] = f"{cc_match.group(1)}cc"
            
            # 提取里程
            mileage_match = re.search(r'(\d+[,，]?\d*)\s*[km|公里]', description)
            if mileage_match:
                extra_fields['mileage'] = f"{mileage_match.group(1)}km"
            
            # 提取颜色
            color_match = re.search(r'([黑白红蓝银灰金棕绿紫])[色|色系]', description)
            if color_match:
                extra_fields['color'] = f"{color_match.group(1)}色"
        
        elif self.vehicle_type == 5:  # 经典车
            # 提取年份范围
            year_match = re.search(r'(\d{4})', description)
            if year_match:
                extra_fields['classic_year'] = year_match.group(1)
            
            # 提取里程
            mileage_match = re.search(r'(\d+[,，]?\d*)\s*[km|公里]', description)
            if mileage_match:
                extra_fields['mileage'] = f"{mileage_match.group(1)}km"
            
            # 提取收藏价值
            if '收藏' in description or '經典' in description:
                extra_fields['collection_value'] = '收藏级'
        
        return extra_fields if extra_fields else {}

    def _get_wait_time(self, trigger_count):
        """根据触发次数获取等待时间"""
        if trigger_count == 1:
            return 15 * 60  # 15分钟
        elif trigger_count == 2:
            return 30 * 60  # 30分钟
        elif trigger_count == 3:
            return 60 * 60  # 1小时
        else:
            return 0  # 终止

    def _handle_anti_crawler(self, trigger_type):
        """处理反爬虫触发"""
        if trigger_type == "list":
            self.list_trigger_count += 1
            trigger_count = self.list_trigger_count
        else:  # detail
            self.detail_trigger_count += 1
            trigger_count = self.detail_trigger_count
        
        # 检查是否超过最大尝试次数
        if trigger_count > 3:
            logger.error(f"{trigger_type}页面触发反爬虫超过3次，终止爬取")
            return False
        
        # 获取等待时间
        wait_time = self._get_wait_time(trigger_count)
        logger.warning(f"{trigger_type}页面第{trigger_count}次触发反爬虫，等待{wait_time//60}分钟后重试...")
        
        # 标记已触发反爬虫
        self.anti_crawler_triggered = True
        
        # 等待
        time.sleep(wait_time)
        logger.info(f"等待完成，准备恢复爬取...")
        
        return True


def auto_import_to_database():
    """自动执行数据库导入"""
    try:
        print("正在启动数据库导入脚本...")
        
        # 检查是否存在CSV文件
        import glob
        csv_files = glob.glob("car_data_*.csv")
        if not csv_files:
            print("[ERROR] 没有找到CSV文件，跳过数据库导入")
            return False
        
        print(f"找到 {len(csv_files)} 个CSV文件，开始导入...")
        
        # 调用导入脚本
        result = subprocess.run([sys.executable, "import_to_mysql.py"], 
                              capture_output=True, text=True, encoding='utf-8', errors='ignore')
        
        if result.returncode == 0:
            print("[OK] 数据库导入成功完成！")
            print("导入结果:")
            print(result.stdout)
            
            # 导入成功后自动清理文件
            cleaned_count = 0
            try:
                from cleanup_utils import FileCleanup
                cleanup = FileCleanup()
                print("\n=== 开始清理临时文件 ===")
                cleaned_count = cleanup.cleanup_after_success()
                if cleaned_count > 0:
                    print(f"[OK] 清理完成：删除了 {cleaned_count} 个临时文件")
                else:
                    print("[INFO] 无需清理文件")
            except Exception as e:
                print(f"[WARNING] 清理文件时出错: {e}")
            
            # 发送webhook通知 - 任务成功完成
            try:
                from webhook_notifier import WebhookNotifier
                notifier = WebhookNotifier()
                print("\n=== 发送完成通知 ===")
                
                # 获取车辆统计信息
                vehicle_counts = {}
                import glob
                for csv_file in glob.glob("car_data_*.csv"):
                    try:
                        # 从文件名提取车辆类型
                        type_id = csv_file.split('_')[2].split('.')[0]
                        # 简单统计行数（减1去掉标题行）
                        with open(csv_file, 'r', encoding='utf-8-sig') as f:
                            line_count = sum(1 for line in f) - 1
                        vehicle_counts[type_id] = max(0, line_count)
                    except:
                        pass
                
                # 发送通知
                success = notifier.notify_scraping_completed(
                    vehicle_counts=vehicle_counts,
                    import_success=True,
                    cleaned_files=cleaned_count,
                    log_file=None  # 可以后续添加日志文件路径
                )
                
                if success:
                    print("[OK] 下游业务方通知发送成功")
                else:
                    print("[WARNING] 下游业务方通知发送失败")
                    
            except Exception as e:
                print(f"[WARNING] 发送webhook通知时出错: {e}")
            
            return True
        else:
            print("[ERROR] 数据库导入失败")
            print("错误信息:")
            print(result.stderr)
            print("[INFO] 任务失败，不发送下游通知（无数据可处理）")
            return False
            
    except Exception as e:
        print(f"[ERROR] 执行数据库导入时发生错误: {e}")
        return False

def main():
    """主函数"""
    import argparse
    
    # 解析命令行参数
    parser = argparse.ArgumentParser(description='28car.com 车辆信息爬取工具')
    parser.add_argument('--auto', action='store_true', help='自动模式（非交互式）')
    parser.add_argument('--config', action='store_true', help='从配置文件读取并执行所有启用的类型')
    parser.add_argument('--type', type=int, choices=[1,2,3,4,5,6], help='车辆类型 (1-6)')
    parser.add_argument('--pages', type=str, help='爬取页数，如"3"或"3,10"')
    parser.add_argument('--config-file', default='config.json', help='配置文件路径')
    
    args = parser.parse_args()
    
    # 获取车辆类型名称
    vehicle_types = {
        1: '私家车',
        2: '客货车', 
        3: '货车',
        4: '电单车',
        5: '经典车'
    }
    
    # 如果没有指定任何参数，默认使用配置文件模式
    if not any([args.auto, args.config, args.type, args.pages]):
        print("未指定参数，默认使用配置文件模式")
        args.config = True
    
    if args.config:
        # 配置文件模式（从配置文件读取所有启用的类型）
        print(f"=== 配置文件模式启动 ===")
        print(f"配置文件: {args.config_file}")
        
        # 加载配置文件
        enabled_types = load_config_from_file(args.config_file)
        
        if not enabled_types:
            print("[ERROR] 配置文件中没有启用的车辆类型或配置文件加载失败")
            sys.exit(1)
        
        print(f"将爬取 {len(enabled_types)} 种车辆类型:")
        for type_id, type_config in enabled_types.items():
            print(f"  - {type_config['name']} (类型{type_id}): {type_config['pages']}页")
        
        # 执行配置文件中的所有类型
        total_vehicles = 0
        for type_id, type_config in enabled_types.items():
            type_name = type_config['name']
            pages = type_config['pages']
            
            print(f"\n=== 开始爬取 {type_name} (类型{type_id}) ===")
            
            # 自动检测文件模式
            csv_filename = f"car_data_{type_id}.csv"
            append_mode = os.path.exists(csv_filename)
            if append_mode:
                print(f"发现现有文件，将追加数据到: {csv_filename}")
            else:
                print(f"将创建新文件: {csv_filename}")
            
            # 逐页处理
            for page in range(1, pages + 1):
                print(f'\n=== 正在处理第 {page} 页 ===')
                
                # 创建实例（每页重新创建，避免数据累积）
                scraper = CarScraper(pages, type_id)
                
                # 获取当前页的车辆列表
                print(f'正在获取第 {page} 页车辆列表...')
                decoded_html = scraper.get_html_1(page)
                
                # 检查是否因反爬虫终止
                if decoded_html is None:
                    print(f'第 {page} 页因反爬虫终止，停止爬取')
                    break
                
                dateCode_i = scraper.get_date_code(decoded_html)
                
                if not dateCode_i:
                    print(f'第 {page} 页没有找到车辆数据')
                    continue
                    
                print(f'第 {page} 页找到 {len(dateCode_i)} 个车辆')
                
                # 获取当前页的销售状态列表
                sale_status_list = [it['sale_status'] for it in dateCode_i]
                
                # 立即爬取当前页的车辆详情
                vehicle_ids = [int(it['code']) for it in dateCode_i]
                scraper.scrape_cars(vehicle_ids, sale_status_list)
                
                # 检查是否因反爬虫终止
                if len(scraper.car_data) == 0 and any(status == "未售" for status in sale_status_list):
                    print(f'第 {page} 页详情爬取因反爬虫终止，停止爬取')
                    break
                
                # 立即保存当前页的数据到CSV
                if scraper.car_data:
                    csv_path = scraper.create_csv_file(csv_filename, append_mode)
                    if csv_path:
                        print(f'第 {page} 页数据已保存，获取 {len(scraper.car_data)} 个车辆')
                        total_vehicles += len(scraper.car_data)
                        # 后续页面使用追加模式
                        append_mode = True
                    else:
                        print(f'第 {page} 页CSV文件生成失败')
                
                # 每页之间添加随机间隔，模拟真实用户行为
                if page < pages:
                    delay = random.uniform(3.0, 6.0)
                    print(f'休息 {delay:.1f} 秒，准备处理下一页...')
                    time.sleep(delay)
            
            print(f'\n{type_name} 爬取完成！')
            print(f"CSV文件: {csv_filename}")
        
        print(f"\n=== 所有类型爬取完成！ ===")
        print(f"总共获取车辆数: {total_vehicles}")
        print(f"生成的CSV文件:")
        for type_id in enabled_types.keys():
            csv_filename = f"car_data_{type_id}.csv"
            if os.path.exists(csv_filename):
                print(f"  - {csv_filename}")
        print(f"所有CSV文件可直接用于数据库导入！")
        
        # 自动执行数据库导入
        print(f"\n=== 开始自动导入数据库 ===")
        auto_import_to_database()
        
        return

    elif args.auto:
        # 自动模式（非交互式）
        if not args.type or not args.pages:
            print("[ERROR] 自动模式需要指定 --type 和 --pages 参数")
            sys.exit(1)
        
        vehicle_type = args.type
        page_input = args.pages
        
        print(f"=== 自动模式启动 ===")
        print(f"车辆类型: {vehicle_type}")
        print(f"页数配置: {page_input}")
        
    else:
        # 交互模式
        print("=== 28car.com 车辆信息爬取工具 ===")
        print("支持5种车辆类型：")
        print("1. 私家车")
        print("2. 客货车") 
        print("3. 货车")
        print("4. 电单车")
        print("5. 经典车")
        print("6. 依次爬取所有类型")
        print()
        
        # 用户选择车辆类型
        while True:
            try:
                vehicle_type = int(input("请选择车辆类型 (1-6): "))
                if 1 <= vehicle_type <= 6:
                    break
                else:
                    print("请输入1-6之间的数字")
            except ValueError:
                print("请输入有效数字")
        
        # 获取爬取页数范围
        while True:
            try:
                page_input = input("请输入要爬取的页数（单个数字如'3'表示1-3页，范围如'3,10'表示3-10页）: ").strip()
                break
            except ValueError:
                print("请输入有效格式，如'3'或'3,10'")
    
    # 解析页数配置
    try:
        if ',' in page_input:
            # 范围输入，如"3,10"
            start_page, end_page = map(int, page_input.split(','))
            if start_page > 0 and end_page >= start_page:
                START_PAGE = start_page
                END_PAGE = end_page
                GLOBE_PAGE = end_page - start_page + 1
                print(f"将爬取第{START_PAGE}页到第{END_PAGE}页，共{GLOBE_PAGE}页")
            else:
                print("起始页必须大于0，结束页必须大于等于起始页")
                sys.exit(1)
        else:
            # 单个数字输入，如"3"
            end_page = int(page_input)
            if end_page > 0:
                START_PAGE = 1
                END_PAGE = end_page
                GLOBE_PAGE = end_page
                print(f"将爬取第1页到第{END_PAGE}页，共{GLOBE_PAGE}页")
            else:
                print("页数必须大于0")
                sys.exit(1)
    except ValueError:
        print("页数格式错误")
        sys.exit(1)
    
    if vehicle_type == 6:
        # 依次爬取所有类型
        print(f"\n开始依次爬取所有类型，每种类型 {GLOBE_PAGE} 页...")
        
        for type_id in range(1, 6):
            vehicle_name = vehicle_types.get(type_id, '未知类型')
            print(f"\n=== 开始爬取 {vehicle_name} (类型{type_id}) ===")
            
            # 自动检测文件模式
            csv_filename = f"car_data_{type_id}.csv"
            append_mode = os.path.exists(csv_filename)
            if append_mode:
                print(f"发现现有文件，将追加数据到: {csv_filename}")
            else:
                print(f"将创建新文件: {csv_filename}")
            
            # 逐页处理
            total_vehicles = 0
            for page in range(START_PAGE, END_PAGE + 1):
                print(f'\n=== 正在处理第 {page} 页 ===')
                
                # 创建实例（每页重新创建，避免数据累积）
                scraper = CarScraper(GLOBE_PAGE, type_id)
                
                # 获取当前页的车辆列表
                print(f'正在获取第 {page} 页车辆列表...')
                decoded_html = scraper.get_html_1(page)
                
                # 检查是否因反爬虫终止
                if decoded_html is None:
                    print(f'第 {page} 页因反爬虫终止，停止爬取')
                    break
                
                dateCode_i = scraper.get_date_code(decoded_html)
                
                if not dateCode_i:
                    print(f'第 {page} 页没有找到车辆数据')
                    continue
                    
                print(f'第 {page} 页找到 {len(dateCode_i)} 个车辆')
                
                # 获取当前页的销售状态列表
                sale_status_list = [it['sale_status'] for it in dateCode_i]
                
                # 立即爬取当前页的车辆详情
                vehicle_ids = [int(it['code']) for it in dateCode_i]
                scraper.scrape_cars(vehicle_ids, sale_status_list)
                
                # 检查是否因反爬虫终止
                if len(scraper.car_data) == 0 and any(status == "未售" for status in sale_status_list):
                    print(f'第 {page} 页详情爬取因反爬虫终止，停止爬取')
                    break
                
                # 立即保存当前页的数据到CSV
                if scraper.car_data:
                    csv_path = scraper.create_csv_file(csv_filename, append_mode)
                    if csv_path:
                        print(f'第 {page} 页数据已保存，获取 {len(scraper.car_data)} 个车辆')
                        total_vehicles += len(scraper.car_data)
                        # 后续页面使用追加模式
                        append_mode = True
                    else:
                        print(f'第 {page} 页CSV文件生成失败')
                
                # 每页之间添加随机间隔，模拟真实用户行为
                if page < END_PAGE:
                    delay = random.uniform(3.0, 6.0)
                    print(f'休息 {delay:.1f} 秒，准备处理下一页...')
                    time.sleep(delay)
            
            print(f'\n{vehicle_name} 爬取完成！')
            print(f"总共获取车辆数: {total_vehicles}")
            print(f"CSV文件: {csv_filename}")
        
        print(f"\n=== 所有类型爬取完成！ ===")
        print(f"每种类型爬取页数: {GLOBE_PAGE}")
        print(f"生成的CSV文件:")
        for type_id in range(1, 6):
            csv_filename = f"car_data_{type_id}.csv"
            if os.path.exists(csv_filename):
                print(f"  - {csv_filename}")
        print(f"所有CSV文件可直接用于数据库导入！")
        
        # 在自动模式下跳过数据库导入，由调度器统一处理
        if not args.auto:
            # 自动执行数据库导入
            print(f"\n=== 开始自动导入数据库 ===")
            auto_import_to_database()
        
    else:
        # 爬取单个类型
        vehicle_name = vehicle_types.get(vehicle_type, '未知类型')
        
        print(f"\n开始爬取 {vehicle_name} 数据，共 {GLOBE_PAGE} 页...")
        
        # 自动检测文件模式
        csv_filename = f"car_data_{vehicle_type}.csv"
        append_mode = os.path.exists(csv_filename)
        if append_mode:
            print(f"发现现有文件，将追加数据到: {csv_filename}")
        else:
            print(f"将创建新文件: {csv_filename}")
        
        # 逐页处理
        total_vehicles = 0
        for page in range(START_PAGE, END_PAGE + 1):
            print(f'\n=== 正在处理第 {page} 页 ===')
            
            # 创建实例（每页重新创建，避免数据累积）
            scraper = CarScraper(GLOBE_PAGE, vehicle_type)
            
            # 获取当前页的车辆列表
            print(f'正在获取第 {page} 页车辆列表...')
            decoded_html = scraper.get_html_1(page)
            
            # 检查是否因反爬虫终止
            if decoded_html is None:
                print(f'第 {page} 页因反爬虫终止，停止爬取')
                break
            
            dateCode_i = scraper.get_date_code(decoded_html)
            
            if not dateCode_i:
                print(f'第 {page} 页没有找到车辆数据')
                continue
                
            print(f'第 {page} 页找到 {len(dateCode_i)} 个车辆')
            
            # 获取当前页的销售状态列表
            sale_status_list = [it['sale_status'] for it in dateCode_i]
            
            # 立即爬取当前页的车辆详情
            vehicle_ids = [int(it['code']) for it in dateCode_i]
            scraper.scrape_cars(vehicle_ids, sale_status_list)
            
            # 检查是否因反爬虫终止
            if len(scraper.car_data) == 0 and any(status == "未售" for status in sale_status_list):
                print(f'第 {page} 页详情爬取因反爬虫终止，停止爬取')
                break
            
            # 立即保存当前页的数据到CSV
            if scraper.car_data:
                csv_path = scraper.create_csv_file(csv_filename, append_mode)
                if csv_path:
                    print(f'第 {page} 页数据已保存，获取 {len(scraper.car_data)} 个车辆')
                    total_vehicles += len(scraper.car_data)
                    # 后续页面使用追加模式
                    append_mode = True
                else:
                    print(f'第 {page} 页CSV文件生成失败')
            
            # 每页之间添加随机间隔，模拟真实用户行为
            if page < END_PAGE:
                delay = random.uniform(3.0, 6.0)
                print(f'休息 {delay:.1f} 秒，准备处理下一页...')
                time.sleep(delay)
        
        print(f'\n=== 爬取完成！ ===')
        print(f"车辆类型: {vehicle_name}")
        print(f"爬取页数范围: 第{START_PAGE}页到第{END_PAGE}页，共{GLOBE_PAGE}页")
        print(f"总共获取车辆数: {total_vehicles}")
        print(f"CSV文件: {csv_filename}")
        print(f"CSV文件可直接用于数据库导入！")
        
        # 在自动模式下跳过数据库导入，由调度器统一处理
        if not args.auto:
            # 自动执行数据库导入
            print(f"\n=== 开始自动导入数据库 ===")
            auto_import_to_database()


if __name__ == "__main__":
    main()