#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
汽车信息爬取脚本
从28car.com网站爬取车辆信息并生成CSV文件
"""

import random
import time
import json
import logging
import os
import re
import sys
import glob
import pandas as pd
import requests
from curl_cffi import requests as curl_requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed

from carinfo.core.proxy import get_proxy_manager
from carinfo.core.base_spider import BaseSpider
from carinfo.utils import parse_price, parse_contact_info

# 设置日志时区为北京时间
class BeijingTimeFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        # 获取本地时间（自动跟随系统时区）
        dt = datetime.fromtimestamp(record.created)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S")

handler = logging.StreamHandler()
handler.setFormatter(BeijingTimeFormatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(handler)
logger = logging.getLogger(__name__)

# 并发配置
DEFAULT_CONCURRENCY = 5  # 默认并发数
DEFAULT_PROXY_POOL_SIZE = 2000  # 默认代理池大小

# 28car 站点根 URL（实际是动态域名，需要时手动更新）
BASE_URL = "https://dj1jklak2e.28car.com"


# User-Agent 池 - 模拟多种浏览器
USER_AGENT_POOL = [
    # Chrome on Windows
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36',
    # Chrome on Mac
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
    # Firefox on Windows
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0',
    # Firefox on Mac
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0',
    # Edge on Windows
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0',
    # Safari on Mac
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15',
    # Chrome on Linux
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
]


def get_random_user_agent():
    """随机获取一个 User-Agent"""
    return random.choice(USER_AGENT_POOL)


def load_config_from_file(config_file='config.json'):
    """从配置文件加载爬取配置"""
    if not os.path.exists(config_file):
        logger.error(f"配置文件 {config_file} 不存在，程序中断")
        raise FileNotFoundError(f"配置文件 {config_file} 不存在，请创建配置文件后重试")

    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)

        vehicle_types = config.get('scraping', {}).get('vehicle_types', {})
        if not vehicle_types:
            logger.error(f"配置文件 {config_file} 中缺少 vehicle_types 配置，程序中断")
            raise ValueError(f"配置文件 {config_file} 中缺少 vehicle_types 配置")

        enabled_types = {}

        for type_id, type_config in vehicle_types.items():
            pages = type_config.get('pages', 0)
            if pages > 0:
                enabled_types[int(type_id)] = {
                    'name': type_config.get('name', f'类型{type_id}'),
                    'pages': pages
                }

        if not enabled_types:
            logger.warning(f"配置文件 {config_file} 中没有需要爬取的车辆类型（所有类型的 pages 都为 0）")

        logger.info(f"从配置文件 {config_file} 加载了 {len(enabled_types)} 个需要爬取的车辆类型")
        return enabled_types

    except json.JSONDecodeError as e:
        logger.error(f"配置文件 {config_file} 格式错误: {e}，程序中断")
        raise ValueError(f"配置文件 {config_file} 格式错误，请检查JSON格式")
    except Exception as e:
        logger.error(f"加载配置文件失败: {e}，程序中断")
        raise RuntimeError(f"加载配置文件 {config_file} 失败: {e}")


class Car28Spider(BaseSpider):
    site_name = "28car"
    base_url = BASE_URL

    def __init__(self, PAGE, vehicle_type=1):
        self.PAGE = PAGE
        self.vehicle_type = vehicle_type
        self.car_data = []
        self.output_dir = "."

        # 并发设置
        self.concurrency = self._load_concurrency()

        self.anti_crawler_triggered = False
        self.list_trigger_count = 0
        self.detail_trigger_count = 0
        self.current_page = 1
        self.current_detail_index = 0

        # 使用新的数据库代理管理器（每次启动随机抽取代理池）
        self.proxy_manager = get_proxy_manager(pool_size=DEFAULT_PROXY_POOL_SIZE)
        self.current_proxy = None

        self.vehicle_types = {
            1: {'name': '私家车', 'param': 'h_f_ty=1'},
            2: {'name': '客货车', 'param': 'h_f_ty=2'},
            3: {'name': '货车', 'param': 'h_f_ty=3'},
            4: {'name': '电单车', 'param': 'h_f_ty=4'},
            5: {'name': '经典车', 'param': 'h_f_ty=5'}
        }

        # 智能限速相关
        self.request_count = 0  # 请求计数
        self.min_delay = 1.0  # 最小延迟（秒）
        self.max_delay = 3.0  # 最大延迟（秒）
        self.consecutive_failures = 0  # 连续失败次数
        self.failure_threshold = 3  # 连续失败多少次后降速

    # ==== BaseSpider 接口实现（薄包装） ====

    def list_url(self, page: int) -> str:
        params = self.vehicle_types[self.vehicle_type]['param'] + f'&h_page={page}'
        return f"{BASE_URL}/sell_lst.php?{params}"

    def detail_url(self, native_id: str) -> str:
        return f"{BASE_URL}/sell_dsp.php?h_vid={native_id}&h_vw=y"

    def parse_list(self, html: str) -> list:
        """从列表页 HTML 提取所有 h_vid。"""
        records = self.get_date_code(html)
        return [r['code'] for r in records if r.get('code')]

    def parse_detail(self, html: str, native_id: str):
        """从详情页 HTML 提取标准化字段 dict（失败返 None）。"""
        return self.extract_car_info(html, native_id)

    def _load_concurrency(self):
        """从配置文件加载并发数"""
        try:
            with open('config.json', 'r', encoding='utf-8') as f:
                config = json.load(f)
            concurrency = config.get('scraping', {}).get('concurrency', DEFAULT_CONCURRENCY)
            logger.info(f"从配置文件加载并发数: {concurrency}")
            return concurrency
        except Exception as e:
            logger.warning(f"加载并发配置失败，使用默认值: {e}")
            return DEFAULT_CONCURRENCY

    def _adjust_delay(self):
        """根据连续失败次数动态调整延迟"""
        if self.consecutive_failures >= self.failure_threshold:
            # 连续失败过多，大幅增加延迟
            self.min_delay = min(5.0, self.min_delay + 0.5)
            self.max_delay = min(10.0, self.max_delay + 1.0)
            logger.warning(f"检测到连续失败，延迟调整为: {self.min_delay:.1f}s - {self.max_delay:.1f}s")
        elif self.consecutive_failures == 0 and self.max_delay > 3.0:
            # 连续成功，降低延迟
            self.max_delay = max(3.0, self.max_delay - 0.5)
            self.min_delay = max(1.0, self.min_delay - 0.25)
            logger.info(f"请求稳定，延迟优化为: {self.min_delay:.1f}s - {self.max_delay:.1f}s")

    def _build_headers(self, referer=None):
        """构建完整的请求头"""
        headers = {
            'User-Agent': get_random_user_agent(),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8,zh-TW;q=0.7',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none' if referer is None else 'same-origin',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0',
        }
        if referer:
            headers['Referer'] = referer
        return headers

    def random_sleep(self, min_seconds=None, max_seconds=None):
        """随机延迟，模拟真实用户行为（使用动态延迟）"""
        # 使用动态延迟
        if min_seconds is None:
            min_seconds = self.min_delay
        if max_seconds is None:
            max_seconds = self.max_delay

        if random.random() < 0.3:
            delay = random.uniform(max_seconds, max_seconds * 2)
        else:
            delay = random.uniform(min_seconds, max_seconds)
        time.sleep(delay)
        return delay

    def _make_request_with_retry(self, url, headers, request_type='list'):
        """使用代理和重试机制发送HTTP请求"""
        # 从配置文件读取超时设置
        try:
            with open('config.json', 'r', encoding='utf-8') as f:
                config = json.load(f)
            scraping_config = config.get('scraping', {})
            max_retries = scraping_config.get('max_retries', 3)
            retry_delay = scraping_config.get('retry_delay', 5)
            timeout = scraping_config.get('request_timeout', 90)
        except:
            max_retries = 3
            retry_delay = 5
            timeout = 90  # 默认90秒，适合住宅代理

        for attempt in range(max_retries + 1):
            try:
                # 从代理管理器获取随机代理
                proxy_info = self.proxy_manager.get_random_proxy()

                if proxy_info:
                    proxy_name = proxy_info.get('name', 'unknown')
                    proxies = {
                        'http': proxy_info.get('http'),
                        'https': proxy_info.get('https')
                    }
                    self.current_proxy = proxy_info
                    logger.info(f"尝试 {attempt + 1}/{max_retries + 1}: 使用代理 {proxy_name}")
                else:
                    logger.warning(f"尝试 {attempt + 1}/{max_retries + 1}: 无可用代理，使用直连")
                    proxies = None
                    self.current_proxy = None

                response = curl_requests.get(
                    url,
                    headers=headers,
                    proxies=proxies,
                    timeout=timeout,
                    allow_redirects=True,
                    impersonate="chrome"
                )

                if response.status_code == 200:
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
                        decoded_html = response.content.decode('latin1', errors='replace')
                        logger.warning("使用latin1编码作为后备方案")

                    logger.info(f"响应状态码: {response.status_code}, 内容长度: {len(decoded_html)}")

                    # 请求成功，标记代理成功，重置失败计数
                    if proxy_name:
                        self.proxy_manager.mark_proxy_success(proxy_name)

                    # 触发延迟调整
                    self.request_count += 1
                    self.consecutive_failures = 0
                    self._adjust_delay()

                    if 'msg_busy.php' in decoded_html or 'busy' in decoded_html.lower():
                        logger.warning(f"{request_type}页面被重定向到busy页面，触发反爬虫机制")
                        if not self._handle_anti_crawler(request_type):
                            return None
                        continue

                    return decoded_html
                else:
                    logger.warning(f"HTTP请求失败，状态码: {response.status_code}")
                    raise requests.RequestException(f"HTTP {response.status_code}")

            except (requests.RequestException, requests.Timeout, Exception) as e:
                logger.error(f"请求失败 (尝试 {attempt + 1}/{max_retries + 1}): {e}")

                # 请求失败，增加失败计数
                self.consecutive_failures += 1

                # 标记代理失败
                if self.current_proxy:
                    proxy_name = self.current_proxy.get('name')
                    self.proxy_manager.mark_proxy_failed(proxy_name)

                if attempt < max_retries:
                    logger.info(f"等待 {retry_delay} 秒后重试...")
                    time.sleep(retry_delay)
                else:
                    logger.error(f"所有重试都失败，放弃请求: {url}")
                    return None

        return None

    def get_date_code(self, decoded_html):
        """提取车辆列表信息"""
        soup = BeautifulSoup(decoded_html, 'html.parser')

        records = []
        for comment in soup.find_all(string=lambda text: isinstance(text, str) and 'Record' in text):
            if comment.strip().startswith('Record'):
                next_tr = comment.find_next('tr')
                if next_tr:
                    records.append(next_tr)

        if len(records) == 0:
            logger.info("未找到Record注释，尝试其他选择器...")
            records = soup.select('tr:has(td[onclick*="goDsp"])')

        if len(records) == 0:
            goDsp_tds = soup.find_all('td', onclick=lambda x: x and 'goDsp' in x)
            logger.info(f"找到 {len(goDsp_tds)} 个包含goDsp的td元素")
            for td in goDsp_tds:
                tr = td.find_parent('tr')
                if tr and tr not in records:
                    records.append(tr)

        logger.info(f"总共找到 {len(records)} 个车辆记录")

        results = []
        for i, record in enumerate(records):
            date_cell = record.select_one('font[style*="color:#999999"]')
            date_value = None
            if date_cell:
                date_text = date_cell.get_text(strip=True)
                date_match = re.search(r'(\d{1,2}/\d{1,2})', date_text)
                if date_match:
                    date_value = date_match.group(1)

            onclick_cell = record.select_one('td[onclick*="goDsp"]')
            param_value = None
            if onclick_cell and 'onclick' in onclick_cell.attrs:
                onclick_value = onclick_cell['onclick']
                param_match = re.search(r'goDsp\(.*?,\s*(\d+)\s*,', onclick_value)
                if param_match:
                    param_value = param_match.group(1)

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

        sold_indicators = [
            'sold.gif', 'sold.png', 'sold.jpg',
            '已售', '已賣', 'SOLD',
            '由於已售，聯絡人資料亦被保護中',
            '由于已售，联络人资料亦被保护中',
            'class="sold"'
        ]

        if 'style="color:#999999"' in record_html and ('已售' in record_html or '已賣' in record_html or 'sold' in record_html.lower()):
            return "已售"

        for indicator in sold_indicators:
            if indicator in record_html:
                return "已售"

        return "未售"

    def get_html_1(self, page):
        """获取车辆列表页"""
        url = self.list_url(page)

        # 模拟从首页进入列表页
        headers = self._build_headers(referer=BASE_URL)

        logger.info(f"请求URL: {url}, 车辆类型: {self.vehicle_type}")

        return self._make_request_with_retry(url, headers, 'list')

    def get_detail_content(self, h_vid):
        """获取车辆详情页"""
        url = self.detail_url(h_vid)

        # 模拟从列表页进入详情页
        vehicle_config = self.vehicle_types.get(self.vehicle_type, self.vehicle_types[1])
        list_url = f"{BASE_URL}/sell_lst.php?{vehicle_config['param']}"
        headers = self._build_headers(referer=list_url)

        logger.info(f"请求详情页面URL: {url}")
        return self._make_request_with_retry(url, headers, 'detail')

    def extract_car_info(self, html_content, h_vid, initial_sale_status=None):
        """从详情页中提取车辆信息"""
        if html_content is None:
            logger.warning(f"车辆 {h_vid} 的HTML内容为空，跳过")
            return None

        soup = BeautifulSoup(html_content, 'html.parser')
        car_data = {}

        car_data['sale_status'] = initial_sale_status
        logger.info(f"车辆 {h_vid} 销售状态：{initial_sale_status}（来自列表页）")

        target_table = soup.find('table', {'width': '100%', 'style': 'height:100%'})

        if target_table is None:
            target_table = soup.find('table', {'width': '100%'})

        if target_table is None:
            tables = soup.find_all('table')
            for table in tables:
                if table.find('td', class_='frm_l') and table.find('td', class_='frm_t'):
                    target_table = table
                    break

        if target_table is None:
            logger.error(f"无法找到车辆详情表格，h_vid: {h_vid}")
            return None

        fields = {
            '編號': '編號', '車類': '車類', '車廠': '車廠', '型號': '型號',
            '燃炓': '燃炓', '座位': '座位', '容積': '容積', '傳動': '傳動',
            '年份': '年份', '簡評': '簡評', '售價': '售價',
            '聯絡人資料': '聯絡人資料', '更新日期': '更新日期', '網址': '網址'
        }

        rows = target_table.find_all('tr')
        for row in rows:
            label_td = row.find('td', class_='frm_l')
            value_td = row.find('td', class_='frm_t')

            if label_td and value_td:
                field_name = label_td.get_text(strip=True)
                if field_name in fields:
                    car_data[fields[field_name]] = value_td.get_text(strip=True)

        car_data['图片URLs'] = self.extract_image_urls(soup)

        if '售價' in car_data:
            price_text = car_data['售價']
            current_price, original_price = parse_price(price_text)
            car_data['current_price'] = current_price
            car_data['original_price'] = original_price
            car_data['price'] = price_text

        if '聯絡人資料' in car_data:
            contact_text = car_data['聯絡人資料']
            contact_name, phone_number = parse_contact_info(contact_text)
            car_data['contact_name'] = contact_name
            car_data['phone_number'] = phone_number

        extra_fields = self._process_extra_fields(car_data)
        if extra_fields and car_data is not None:
            car_data.update(extra_fields)

        car_data['h_vid'] = h_vid
        car_data['本地图片路径'] = []

        return car_data

    def extract_image_urls(self, soup):
        """提取图片URL"""
        image_urls = []
        img_tags = soup.find_all('img')
        for img in img_tags:
            src = img.get('src')
            if src:
                if src.startswith('http'):
                    full_url = src
                else:
                    full_url = urljoin(self.base_url, src)
                if 'data/image/sell' in full_url and '28car.com' in full_url:
                    image_urls.append(full_url)

        image_b_urls = [url.replace("_m", "_b").replace("_s", "_b") for url in image_urls]
        return image_b_urls

    def scrape_cars(self, h_vid_list, sale_status_list):
        """爬取多个车辆信息（并发版本）"""
        logger.info(f"开始爬取 {len(h_vid_list)} 个车辆信息，并发数: {self.concurrency}")

        # 为每个车辆创建任务
        tasks = list(zip(h_vid_list, sale_status_list))

        # 使用线程池并发执行
        with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
            # 提交所有任务
            future_to_task = {
                executor.submit(self._scrape_single_car, h_vid, sale_status): (h_vid, sale_status)
                for h_vid, sale_status in tasks
            }

            # 收集结果
            completed = 0
            for future in as_completed(future_to_task):
                completed += 1
                h_vid, sale_status = future_to_task[future]
                try:
                    car_info = future.result()
                    if car_info is not None:
                        self.car_data.append(car_info)
                        logger.info(f"[{completed}/{len(h_vid_list)}] 成功: {car_info.get('車廠', '')} {car_info.get('型號', '')}")
                    else:
                        logger.warning(f"[{completed}/{len(h_vid_list)}] 失败: h_vid={h_vid}")
                except Exception as e:
                    logger.error(f"[{completed}/{len(h_vid_list)}] 异常: h_vid={h_vid}, 错误: {e}")

        logger.info(f"爬取完成，共获取 {len(self.car_data)}/{len(h_vid_list)} 个车辆信息")

    def _scrape_single_car(self, h_vid, sale_status):
        """爬取单个车辆详情（供并发调用）"""
        logger.info(f"正在处理车辆，h_vid: {h_vid}, 状态: {sale_status}")

        try:
            # 获取详情页
            html_content = self.get_detail_content(h_vid)

            if html_content is None:
                logger.error(f"车辆 {h_vid} 因反爬虫终止而跳过")
                return None

            if 'msg_busy.php' in html_content or 'busy' in html_content.lower():
                logger.warning(f"车辆 {h_vid} 被重定向到busy页面，可能触发了反爬虫机制")
                return None

            # 提取车辆信息
            car_info = self.extract_car_info(html_content, h_vid, sale_status)

            if car_info is not None:
                return car_info
            else:
                logger.warning(f"车辆 {h_vid} 提取失败，跳过")
                return None

        except Exception as e:
            logger.error(f"处理车辆 {h_vid} 时出错: {e}")
            return None

    def create_csv_file(self, filename=None, append_mode=False):
        """创建CSV文件"""
        if not self.car_data:
            logger.warning("没有数据可写入CSV")
            return None

        if filename is None:
            filename = f"car_data_{self.vehicle_type}.csv"

        csv_path = os.path.join(self.output_dir, filename)
        csv_data = []
        current_timestamp = int(time.time())

        for i, car in enumerate(self.car_data, 1):
            price_str = car.get('售價', '')
            current_price = car.get('current_price')
            original_price = car.get('original_price')
            extra_fields = car.get('extra_fields', {})

            row = {
                'vehicle_id': car.get('編號', ''),
                'page_number': current_timestamp + i,
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

        if csv_data:
            df = pd.DataFrame(csv_data)

            string_columns = ['year', 'phone_number', 'seats', 'engine_volume']
            for col in string_columns:
                if col in df.columns:
                    df[col] = df[col].astype(str).str.replace('.0', '', regex=False)

            if append_mode and os.path.exists(csv_path):
                existing_df = pd.read_csv(csv_path, encoding='utf-8-sig')
                
                # 去重：基于vehicle_id保留最新的记录
                if 'vehicle_id' in existing_df.columns and 'vehicle_id' in df.columns:
                    # 合并数据
                    combined_df = pd.concat([existing_df, df], ignore_index=True)
                    # 按vehicle_id去重，保留最后一条（即最新的）
                    combined_df = combined_df.drop_duplicates(subset=['vehicle_id'], keep='last')
                    combined_df.to_csv(csv_path, index=False, encoding='utf-8-sig')
                    logger.info(f"数据已追加并去重，原始{len(existing_df)}条 + 新增{len(df)}条 = 去重后{len(combined_df)}条")
                else:
                    combined_df = pd.concat([existing_df, df], ignore_index=True)
                    combined_df.to_csv(csv_path, index=False, encoding='utf-8-sig')
                    logger.info(f"数据已追加到现有CSV文件: {csv_path}")
            else:
                df.to_csv(csv_path, index=False, encoding='utf-8-sig')
                logger.info(f"CSV文件已保存: {csv_path}")

            return csv_path
        else:
            logger.warning("没有数据可写入CSV")
            return None

    def _process_extra_fields(self, car_data):
        """根据车辆类型处理扩展字段"""
        extra_fields = {}

        if car_data is None:
            return extra_fields

        description = car_data.get('簡評', '')

        if self.vehicle_type == 1:
            mileage_match = re.search(r'(\d+[,，]?\d*)\s*[km|公里]', description)
            if mileage_match:
                extra_fields['mileage'] = f"{mileage_match.group(1)}km"

            color_match = re.search(r'([黑白红蓝银灰金棕绿紫])[色|色系]', description)
            if color_match:
                extra_fields['color'] = f"{color_match.group(1)}色"

            if '一手' in description:
                extra_fields['condition'] = '一手'
            elif '二手' in description:
                extra_fields['condition'] = '二手'

            if '自動' in description or '自动' in description:
                extra_fields['transmission_type'] = '自动'
            elif '手動' in description or '手动' in description:
                extra_fields['transmission_type'] = '手动'

            body_types = ['房車', 'SUV', 'MPV', '跑車', '掀背', '旅行車', '開篷']
            for body_type in body_types:
                if body_type in description:
                    extra_fields['body_type'] = body_type
                    break

        elif self.vehicle_type in [2, 3]:
            cargo_match = re.search(r'(\d+\.?\d*)\s*[吨|T]', description)
            if cargo_match:
                extra_fields['cargo_capacity'] = f"{cargo_match.group(1)}吨"

            length_match = re.search(r'(\d+\.?\d*)\s*[米|m]', description)
            if length_match:
                extra_fields['body_length'] = f"{length_match.group(1)}米"

            height_match = re.search(r'(\d+\.?\d*)\s*米.*高', description)
            if height_match:
                extra_fields['body_height'] = f"{height_match.group(1)}米"

            fuel_match = re.search(r'(\d+\.?\d*)L/100km', description)
            if fuel_match:
                extra_fields['fuel_consumption'] = f"{fuel_match.group(1)}L/100km"

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

            if self.vehicle_type == 3:
                engine_match = re.search(r'(\d+)\s*節機', description)
                if engine_match:
                    extra_fields['engine_sections'] = f"{engine_match.group(1)}节机"

                if '夾車' in description:
                    extra_fields['body_type'] = '夹车'
                elif '冷凍車' in description or '冻车' in description:
                    extra_fields['body_type'] = '冷冻车'
                elif '貨車' in description:
                    extra_fields['body_type'] = '货车'

                ton_match = re.search(r'(\d+\.?\d*)TON', description, re.IGNORECASE)
                if ton_match:
                    extra_fields['tonnage'] = f"{ton_match.group(1)}TON"

        elif self.vehicle_type == 4:
            cc_match = re.search(r'(\d+)\s*cc', description, re.IGNORECASE)
            if cc_match:
                extra_fields['engine_cc'] = f"{cc_match.group(1)}cc"

            mileage_match = re.search(r'(\d+[,，]?\d*)\s*[km|公里]', description)
            if mileage_match:
                extra_fields['mileage'] = f"{mileage_match.group(1)}km"

            color_match = re.search(r'([黑白红蓝银灰金棕绿紫])[色|色系]', description)
            if color_match:
                extra_fields['color'] = f"{color_match.group(1)}色"

        elif self.vehicle_type == 5:
            year_match = re.search(r'(\d{4})', description)
            if year_match:
                extra_fields['classic_year'] = year_match.group(1)

            mileage_match = re.search(r'(\d+[,，]?\d*)\s*[km|公里]', description)
            if mileage_match:
                extra_fields['mileage'] = f"{mileage_match.group(1)}km"

            if '收藏' in description or '經典' in description:
                extra_fields['collection_value'] = '收藏级'

        return extra_fields if extra_fields else {}

    def _get_wait_time(self, trigger_count):
        """根据触发次数获取等待时间"""
        if trigger_count == 1:
            return 15 * 60
        elif trigger_count == 2:
            return 30 * 60
        elif trigger_count == 3:
            return 60 * 60
        else:
            return 0

    def _handle_anti_crawler(self, trigger_type):
        """处理反爬虫触发"""
        if trigger_type == "list":
            self.list_trigger_count += 1
            trigger_count = self.list_trigger_count
        else:
            self.detail_trigger_count += 1
            trigger_count = self.detail_trigger_count

        if trigger_count > 3:
            logger.error(f"{trigger_type}页面触发反爬虫超过3次，终止爬取")
            return False

        wait_time = self._get_wait_time(trigger_count)
        logger.warning(f"{trigger_type}页面第{trigger_count}次触发反爬虫，等待{wait_time//60}分钟后重试...")

        self.anti_crawler_triggered = True

        time.sleep(wait_time)
        logger.info(f"等待完成，准备恢复爬取...")

        return True


def scrape_vehicle_type(vehicle_type, pages, csv_filename, start_page=1):
    """爬取指定类型的车辆
    Args:
        vehicle_type: 车辆类型ID
        pages: 总共爬取多少页
        csv_filename: CSV文件名
        start_page: 起始页码（默认为1）
    """
    print(f"\n=== 开始爬取 (类型{vehicle_type}) ===")

    # 爬取统计
    crawl_start_time = time.time()
    anti_crawler_triggered = 0
    proxy_fail_count = 0
    pages_scraped = 0
    error_count = 0

    # 每次定时任务启动时，刷新代理池（重新随机抽取代理池）
    proxy_manager = get_proxy_manager(pool_size=DEFAULT_PROXY_POOL_SIZE)
    print(f"正在刷新代理池（从数据库随机抽取{DEFAULT_PROXY_POOL_SIZE}个代理）...")
    proxy_manager.refresh()

    # 显示代理统计信息
    stats = proxy_manager.get_stats()
    print(f"[*] 代理池已就绪: {stats.get('pool_size', 0)}个代理")

    append_mode = os.path.exists(csv_filename)
    if append_mode:
        print(f"发现现有文件，将追加数据到: {csv_filename}")
    else:
        print(f"将创建新文件: {csv_filename}")

    total_vehicles = 0
    vehicle_type_names = {1: '私家车', 2: '客货车', 3: '货车', 4: '电单车', 5: '经典车'}
    vehicle_type_name = vehicle_type_names.get(vehicle_type, f'类型{vehicle_type}')

    for page in range(start_page, pages + 1):
        print(f'\n=== 正在处理第 {page} 页 ===')

        scraper = Car28Spider(pages, vehicle_type)

        print(f'正在获取第 {page} 页车辆列表...')
        decoded_html = scraper.get_html_1(page)

        if decoded_html is None:
            print(f'第 {page} 页因反爬虫终止，停止爬取')
            anti_crawler_triggered += 1
            break

        dateCode_i = scraper.get_date_code(decoded_html)

        if not dateCode_i:
            print(f'第 {page} 页没有找到车辆数据')
            continue

        print(f'第 {page} 页找到 {len(dateCode_i)} 个车辆')

        sale_status_list = [it['sale_status'] for it in dateCode_i]
        vehicle_ids = [int(it['code']) for it in dateCode_i]
        scraper.scrape_cars(vehicle_ids, sale_status_list)

        if len(scraper.car_data) == 0 and any(status == "未售" for status in sale_status_list):
            print(f'第 {page} 页详情爬取因反爬虫终止，停止爬取')
            anti_crawler_triggered += 1
            break

        if scraper.car_data:
            csv_path = scraper.create_csv_file(csv_filename, append_mode)
            if csv_path:
                print(f'第 {page} 页数据已保存，获取 {len(scraper.car_data)} 个车辆')
                total_vehicles += len(scraper.car_data)
                append_mode = True
            else:
                print(f'第 {page} 页CSV文件生成失败')

        pages_scraped += 1

        if page < pages:
            delay = random.uniform(3.0, 6.0)
            print(f'休息 {delay:.1f} 秒，准备处理下一页...')
            time.sleep(delay)

    crawl_duration = time.time() - crawl_start_time
    print(f'\n爬取完成！车辆数: {total_vehicles}, 爬取页数: {pages_scraped}, 反爬触发: {anti_crawler_triggered}')

    # 设置环境变量供导入脚本使用
    os.environ['CRAWL_VEHICLE_TYPE'] = vehicle_type_name
    os.environ['CRAWL_PAGES_SCRAPED'] = str(pages_scraped)
    os.environ['CRAWL_TOTAL_VEHICLES'] = str(total_vehicles)
    os.environ['CRAWL_PROXY_USED'] = str(stats.get('pool_size', 0))
    os.environ['CRAWL_PROXY_FAIL'] = str(proxy_fail_count)
    os.environ['CRAWL_ANTI_CRAWLER'] = str(anti_crawler_triggered)
    os.environ['CRAWL_DURATION'] = str(round(crawl_duration, 2))
    os.environ['CRAWL_ERROR_COUNT'] = str(error_count)

    return total_vehicles


def auto_import_to_database():
    """自动执行数据库导入（直接调用 core.importer.main，不再走 subprocess）"""
    try:
        print("正在启动数据库导入...")

        csv_files = glob.glob("data/csv/car_data_*.csv")
        if not csv_files:
            print("[ERROR] 没有找到CSV文件，跳过数据库导入")
            return False

        print(f"找到 {len(csv_files)} 个CSV文件，开始导入...")

        from carinfo.core import importer
        importer.main()
        print("[OK] 数据库导入成功完成！")
        return True

    except Exception as e:
        print(f"[ERROR] 执行数据库导入时发生错误: {e}")
        return False


def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description='28car.com 车辆信息爬取工具')
    parser.add_argument('--config', action='store_true', help='从配置文件读取并执行所有启用的类型')
    parser.add_argument('--config-file', default='config.json', help='配置文件路径')

    args = parser.parse_args()

    if not args.config:
        print("未指定参数，默认使用配置文件模式")
        args.config = True

    if args.config:
        print(f"=== 配置文件模式启动 ===")
        print(f"配置文件: {args.config_file}")

        enabled_types = load_config_from_file(args.config_file)

        if not enabled_types:
            print("[ERROR] 配置文件中没有需要爬取的车辆类型（所有类型的 pages 都为 0）")
            sys.exit(1)

        print(f"将爬取 {len(enabled_types)} 种车辆类型:")
        for type_id, type_config in enabled_types.items():
            print(f"  - {type_config['name']} (类型{type_id}): {type_config['pages']}页")

        total_vehicles = 0
        csv_dir = "data/csv"
        os.makedirs(csv_dir, exist_ok=True)
        for type_id, type_config in enabled_types.items():
            type_name = type_config['name']
            pages = type_config['pages']
            csv_filename = os.path.join(csv_dir, f"car_data_{type_id}.csv")

            total_vehicles += scrape_vehicle_type(type_id, pages, csv_filename)

        print(f"\n=== 所有类型爬取完成！ ===")
        print(f"总共获取车辆数: {total_vehicles}")
        print(f"生成的CSV文件:")
        for type_id in enabled_types.keys():
            csv_filename = os.path.join(csv_dir, f"car_data_{type_id}.csv")
            if os.path.exists(csv_filename):
                print(f"  - {csv_filename}")
        print(f"所有CSV文件可直接用于数据库导入！")

        print(f"\n=== 开始自动导入数据库 ===")
        auto_import_to_database()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
