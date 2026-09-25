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
import threading
import pandas as pd
import requests
from curl_cffi import requests as curl_requests
from bs4 import BeautifulSoup
from datetime import datetime
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
DEFAULT_REQUEST_INTERVAL = (2.0, 3.0)  # 全局发送间隔（秒），区间内随机

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


def stringify_cell(value):
    """把数值单元格转成不带多余 .0 的字符串。

    不要用 str.replace('.0', '')：那会把 "10.05" 误伤成 "105"。
    这里只剥掉「整数 + .0」这种纯整数尾巴，其余原样输出。
    """
    if value is None:
        return ''
    if isinstance(value, float):
        if value != value:  # NaN
            return ''
        if value.is_integer():
            return str(int(value))
        return str(value)
    text = str(value)
    # 字符串形态的 "2023.0" / "68211289.0" 也要处理（爬取侧取到的是字符串，
    # pandas 往返也会保留这个尾巴）；但只认 ^-?\d+\.0$，真小数 "10.05" 不受影响
    if re.fullmatch(r'-?\d+\.0', text):
        return text[:-2]
    return text


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


class GlobalIntervalLimiter:
    """全局发送间隔限速器。

    所有工作线程共用一条"时间槽"轴：每次发送前在锁内预约下一个槽点
    （槽距在 [min_interval, max_interval] 内随机），再到锁外睡到自己的槽点。
    效果：无论并发多少线程、重试多少次，对站点的平均发送速率都不超过
    每 interval 一条，且相邻两条的间隔带随机抖动。
    """

    def __init__(self, min_interval, max_interval):
        self.min_interval = max(0.0, float(min_interval))
        self.max_interval = max(self.min_interval, float(max_interval))
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            slot = max(self._next_slot, now)
            self._next_slot = slot + random.uniform(self.min_interval, self.max_interval)
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)


class Car28Spider(BaseSpider):
    site_name = "28car"
    base_url = BASE_URL

    def __init__(self, PAGE, vehicle_type=1):
        self.PAGE = PAGE
        self.vehicle_type = vehicle_type
        self.car_data = []
        self.output_dir = "."

        # 一次性加载配置（避免每次 HTTP 请求都重复读盘）
        scraping_cfg = self._load_scraping_config()
        self.concurrency = scraping_cfg.get('concurrency', DEFAULT_CONCURRENCY)
        self.max_retries = scraping_cfg.get('max_retries', 3)
        self.retry_delay = scraping_cfg.get('retry_delay', 5)
        self.request_timeout = scraping_cfg.get('request_timeout', 90)

        # 全局发送间隔（config.json: scraping.request_interval）：
        # 数字=固定间隔，[min, max]=区间内随机；并发线程与重试共用
        interval_cfg = scraping_cfg.get('request_interval', DEFAULT_REQUEST_INTERVAL)
        if isinstance(interval_cfg, (list, tuple)) and len(interval_cfg) == 2:
            self.interval_min = float(interval_cfg[0])
            self.interval_max = float(interval_cfg[1])
        else:
            try:
                self.interval_min = self.interval_max = float(interval_cfg)
            except (TypeError, ValueError):
                logger.warning(f"request_interval 配置无效: {interval_cfg!r}，使用默认 2-3 秒")
                self.interval_min, self.interval_max = DEFAULT_REQUEST_INTERVAL
        self.rate_limiter = GlobalIntervalLimiter(self.interval_min, self.interval_max)
        logger.info(
            f"已加载爬取配置: concurrency={self.concurrency}, "
            f"max_retries={self.max_retries}, timeout={self.request_timeout}s, "
            f"interval={self.interval_min:.1f}-{self.interval_max:.1f}s"
        )

        self.anti_crawler_stop = False  # 反爬终止标记：连续busy耗尽冷却档位后触发

        # busy（被站点拒绝）处理配置：换IP重试预算 + 全局"连续失败链"冷却。
        # 约束（勿改坏）：fail_streak_threshold 必须 <= busy_max_retries，
        # 否则单请求会在冷却触发前耗尽预算；且必须 > 并发数，否则一次齐撞
        # 直接进冷却（提速并发时同步调大）
        busy_cfg = scraping_cfg.get('busy_handling', {})
        self.busy_max_retries = int(busy_cfg.get('busy_max_retries', 20))
        self.fail_streak_threshold = int(busy_cfg.get('fail_streak_threshold', 10))
        cooldown_minutes = busy_cfg.get('cooldown_minutes', [15, 30, 60])
        self.cooldown_seconds = [float(m) * 60.0 for m in cooldown_minutes]

        # 全局反爬共享状态：变更必须持 _anti_lock（busy 累加与成功清零并发）。
        # _busy_streak 连续busy计数（任何一次成功请求清零）；
        # _cooldown_level 已触发的冷却档位；_cooldown_until 冷却截止（monotonic）
        self._busy_streak = 0
        self._cooldown_level = 0
        self._cooldown_until = 0.0

        # 使用新的数据库代理管理器（每次启动随机抽取代理池）
        self.proxy_manager = get_proxy_manager(pool_size=DEFAULT_PROXY_POOL_SIZE)

        # 并发请求下的共享计数/自适应延迟保护（scrape_cars 用线程池并发跑）
        self._stats_lock = threading.Lock()
        self._anti_lock = threading.Lock()
        self.proxy_fail_count = 0

        self.vehicle_types = {
            1: {'name': '私家车', 'param': 'h_f_ty=1'},
            2: {'name': '客货车', 'param': 'h_f_ty=2'},
            3: {'name': '货车', 'param': 'h_f_ty=3'},
            4: {'name': '电单车', 'param': 'h_f_ty=4'},
            5: {'name': '经典车', 'param': 'h_f_ty=5'}
        }

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

    def _load_scraping_config(self):
        """一次性加载 config.json 中的 scraping 段。

        约定：**scraping 段存在即必须可用**。这里不吞异常 —— 反爬参数
        （concurrency / request_interval / busy_handling）一旦静默退回代码默认值，
        表现为"程序照常跑但拦不住反爬"，属于最危险的一类故障。宁可起不来，
        也不要带着错的节流参数上生产（与 load_config_from_file 的策略一致）。
        """
        if not os.path.exists('config.json'):
            logger.error("配置文件 config.json 不存在，无法加载爬取配置")
            raise FileNotFoundError("config.json 不存在，请创建配置文件后重试")

        try:
            with open('config.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            logger.error(f"config.json 解析失败（JSON 语法错误），程序中断: {e}")
            raise
        except OSError as e:
            logger.error(f"config.json 读取失败，程序中断: {e}")
            raise

        scraping_cfg = data.get('scraping')
        if not isinstance(scraping_cfg, dict):
            logger.warning(
                "config.json 缺少 scraping 段或类型不是对象，"
                "将使用代码内置的默认反爬参数（request_interval=2-3s, concurrency=5）"
            )
            return {}
        return scraping_cfg

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

    def _decode_response(self, response):
        """解码响应体。

        优先使用 HTTP Header / 页面 meta 声明的字符集；未声明时按站点默认
        big5 解码（与历史行为一致）。不能用 errors='replace' 逐个试探——
        replace 模式下 decode 永不抛异常，探测循环永远命中第一个编码。
        """
        content = response.content

        declared = None
        try:
            content_type = response.headers.get('Content-Type', '') or ''
        except Exception:
            content_type = ''
        m = re.search(r'charset=["\']?([\w-]+)', content_type, re.IGNORECASE)
        if m:
            declared = m.group(1)
        else:
            m = re.search(rb'charset=["\']?([\w-]+)', content[:2048], re.IGNORECASE)
            if m:
                declared = m.group(1).decode('ascii', errors='ignore')

        if declared:
            try:
                html = content.decode(declared)
                logger.info(f"按声明的 {declared} 编码解码成功")
                return html
            except (UnicodeDecodeError, LookupError):
                logger.warning(f"声明的 {declared} 编码解码失败，回退站点默认 big5")
        else:
            logger.info("响应未声明字符集，按站点默认 big5 解码")

        return content.decode('big5', errors='replace')

    def _wait_cooldown(self):
        """全局反爬冷却：未到冷却截止时刻就睡到截止。

        必须在每次发送（含重试）前调用：冷却期间攒下的重试要睡到冷却
        结束再发，否则冷却只对后续新请求生效，本请求已带着失败退场。
        """
        until = self._cooldown_until
        if until:
            delay = until - time.monotonic()
            if delay > 0:
                logger.warning(f"反爬冷却生效中，等待 {delay / 60:.1f} 分钟后继续发送...")
                time.sleep(delay)

    def _make_request_with_retry(self, url, headers, request_type='list'):
        """使用代理和重试机制发送HTTP请求。

        两类失败分开计数：网络型错误（超时/DNS/非200）上限 max_retries 次；
        busy（站点返回HTTP 200的拒绝页，换IP重试有意义）上限
        busy_max_retries 次，不消耗网络预算——共用一个预算会让"换IP"这个
        通常有效的手段被网络抖动提前耗尽。
        """
        timeout = self.request_timeout
        attempt = 0  # 网络型失败计数
        busy_count = 0  # busy换IP计数

        while (
            attempt <= self.max_retries
            and busy_count < self.busy_max_retries
            and not self.anti_crawler_stop  # 终止后本请求一次都不发，直接退场
        ):
            # 每轮重置：防首轮在绑定前抛异常导致 UnboundLocalError（列表页
            # 路径无 try 兜住会崩整轮），以及网络失败被记到上一轮代理头上
            proxy_name = None
            try:
                # 反爬冷却优先于限速：冷却期间的重试睡到冷却结束再排发送槽
                self._wait_cooldown()

                # 全局发送间隔限速：并发线程与重试共用一条时间槽轴，
                # 控制对站点的总发送速率（含重试，防重试风暴）
                self.rate_limiter.wait()

                # 从代理管理器获取随机代理
                proxy_info = self.proxy_manager.get_random_proxy()

                # 代理名放在局部变量：本方法在线程池中并发执行，
                # 共享实例字段会把失败记到别的请求刚换上的代理头上
                proxy_name = proxy_info.get('name') if proxy_info else None

                if proxy_info:
                    proxies = {
                        'http': proxy_info.get('http'),
                        'https': proxy_info.get('https')
                    }
                    logger.info(
                        f"尝试 (网络重试{attempt}/{self.max_retries}, "
                        f"busy换IP {busy_count}/{self.busy_max_retries}): "
                        f"使用代理 {proxy_name}"
                    )
                else:
                    logger.warning(
                        f"尝试 (网络重试{attempt}/{self.max_retries}): 无可用代理，使用直连"
                    )
                    proxies = None

                response = curl_requests.get(
                    url,
                    headers=headers,
                    proxies=proxies,
                    timeout=timeout,
                    allow_redirects=True,
                    impersonate="chrome"
                )

                if response.status_code == 200:
                    decoded_html = self._decode_response(response)

                    logger.info(f"响应状态码: {response.status_code}, 内容长度: {len(decoded_html)}")

                    # 只认明确的重定向特征；正文里普通 "busy" 单词（如車輛簡評的
                    # 英文描述）不是反爬信号
                    if 'msg_busy.php' in decoded_html:
                        # busy 页也是 HTTP 200：该代理必须记失败而非成功。
                        # success 标记在下方非 busy 分支——若记在 busy 判断之前，
                        # fail_count 的异步 +1/-1 回写互相抵消，代理拉黑永不生效
                        self.proxy_manager.mark_proxy_failed(proxy_name)
                        if not self._handle_anti_crawler(request_type):
                            self.anti_crawler_stop = True
                            return None
                        busy_count += 1
                        continue

                    # 真正的成功：记代理成功 + 清零全局反爬失败链与冷却档位
                    if proxy_name:
                        self.proxy_manager.mark_proxy_success(proxy_name)
                    with self._anti_lock:
                        if self._busy_streak or self._cooldown_level:
                            logger.info("请求成功，反爬连续失败链与冷却档位已清零")
                            self._busy_streak = 0
                            self._cooldown_level = 0
                    return decoded_html

                logger.warning(f"HTTP请求失败，状态码: {response.status_code}")
                raise requests.RequestException(f"HTTP {response.status_code}")

            except Exception as e:
                logger.error(f"请求失败 (网络重试 {attempt}/{self.max_retries}): {e}")

                with self._stats_lock:
                    self.proxy_fail_count += 1

                # 标记代理失败
                if proxy_name:
                    self.proxy_manager.mark_proxy_failed(proxy_name)

                attempt += 1
                if attempt <= self.max_retries:
                    logger.info(f"等待 {self.retry_delay} 秒后重试...")
                    time.sleep(self.retry_delay)
                else:
                    logger.error(f"网络重试预算耗尽，放弃请求: {url}")

        if busy_count >= self.busy_max_retries:
            logger.error(
                f"busy换IP预算耗尽（连续{busy_count}次均被站点拒绝），放弃请求: {url}"
            )
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
            list_info = self._parse_list_row(record)
            logger.info(f"记录 {i+1}: 日期={date_value}, 车辆ID={param_value}, 状态={sale_status}, "
                        f"浏览={list_info['view_count']}, 留言={list_info['comment_count']}")

            results.append({
                'date': date_value,
                'code': param_value,
                'sale_status': sale_status,
                **list_info,
            })

        logger.info(f"成功提取 {len(results)} 个车辆信息")
        return results

    def _parse_list_row(self, record):
        """从列表页记录行提取浏览数/留言数及结构化字段。

        大单元格内嵌表格列序（2026-09 实测固定）：
        0 車廠+型號, 1 燃炓, 2 座位, 3 動力, 4 傳動, 5 年份, 6 售價,
        7 留言數, 8 瀏覽數, 9 已售標記, 10 簡評+聯絡人。
        结构化字段同时作为详情页解析失败时的兜底数据源。
        """
        info = {
            'view_count': None, 'comment_count': None,
            'list_brand': '', 'list_model': '', 'list_fuel': '', 'list_seats': '',
            'list_engine': '', 'list_transmission': '', 'list_year': '', 'list_price': '',
        }
        main_td = record.find('td', onclick=lambda x: x and 'goDsp' in x)
        if main_td is None:
            return info

        cells = main_td.find_all('td')

        def cell_text(idx):
            return cells[idx].get_text(strip=True) if idx < len(cells) else ''

        if cell_text(7).isdigit():
            info['comment_count'] = int(cell_text(7))
        if cell_text(8).isdigit():
            info['view_count'] = int(cell_text(8))

        brand_model = cell_text(0).split()
        if brand_model:
            info['list_brand'] = brand_model[0]
            info['list_model'] = ' '.join(brand_model[1:])
        info['list_fuel'] = cell_text(1)
        info['list_seats'] = cell_text(2)
        info['list_engine'] = cell_text(3)
        info['list_transmission'] = cell_text(4)
        info['list_year'] = cell_text(5)
        info['list_price'] = cell_text(6)
        return info

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
        # 修复：此前只把扩展字段平铺进 car_data 顶层，build_rows 读取的
        # 'extra_fields' 键从未被赋值，导致爬取侧扩展字段从未入库
        car_data['extra_fields'] = extra_fields
        car_data.update(extra_fields)

        # h_vid 保留：详情页缺少「編號」时，build_rows 用它兜底生成 vehicle_id
        car_data['h_vid'] = h_vid

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

    def scrape_cars(self, items):
        """爬取多个车辆信息（并发版本）。items 为列表页记录 dict（含 code/sale_status/浏览留言等）。"""
        logger.info(f"开始爬取 {len(items)} 个车辆信息，并发数: {self.concurrency}")

        with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
            future_to_task = {
                executor.submit(self._scrape_single_car, item): item for item in items
            }

            completed = 0
            for future in as_completed(future_to_task):
                completed += 1
                item = future_to_task[future]
                h_vid = item.get('code')
                try:
                    car_info = future.result()
                    if car_info is not None:
                        self.car_data.append(car_info)
                        logger.info(f"[{completed}/{len(items)}] 成功: {car_info.get('車廠', '')} {car_info.get('型號', '')}")
                    else:
                        logger.warning(f"[{completed}/{len(items)}] 失败: h_vid={h_vid}")
                except Exception as e:
                    logger.error(f"[{completed}/{len(items)}] 异常: h_vid={h_vid}, 错误: {e}")

        logger.info(f"爬取完成，共获取 {len(self.car_data)}/{len(items)} 个车辆信息")

    def _scrape_single_car(self, item):
        """爬取单个车辆详情（供并发调用）。详情解析失败时用列表页字段兜底，不丢记录。"""
        h_vid = item.get('code')
        sale_status = item.get('sale_status')
        logger.info(f"正在处理车辆，h_vid: {h_vid}, 状态: {sale_status}")

        try:
            html_content = self.get_detail_content(h_vid)

            if html_content is None:
                # 反爬终止、busy换IP预算耗尽、网络失败都会走到这里
                logger.error(f"车辆 {h_vid} 请求失败而跳过")
                return None

            car_info = self.extract_car_info(html_content, h_vid, sale_status)

            if car_info is None:
                logger.warning(f"车辆 {h_vid} 详情解析失败，使用列表页字段兜底")
                car_info = self._build_fallback_car(item)

            car_info['_list'] = {
                'view_count': item.get('view_count'),
                'comment_count': item.get('comment_count'),
            }
            return car_info

        except Exception as e:
            logger.error(f"处理车辆 {h_vid} 时出错: {e}")
            return None

    def _build_fallback_car(self, item):
        """详情页解析失败时，用列表页结构化字段构造最小 car_data（缺簡評/图片/聯絡人）。"""
        h_vid = str(item.get('code', ''))
        list_price = item.get('list_price', '')
        current_price, original_price = parse_price(list_price)
        car_data = {
            '車類': '', '車廠': item.get('list_brand', ''), '型號': item.get('list_model', ''),
            '燃炓': item.get('list_fuel', ''), '座位': item.get('list_seats', ''),
            '容積': item.get('list_engine', ''), '傳動': item.get('list_transmission', ''),
            '年份': item.get('list_year', ''), '簡評': '', '售價': list_price,
            'current_price': current_price, 'original_price': original_price,
            '聯絡人資料': '', '更新日期': '',
            '網址': f"{BASE_URL}/sell_dsp.php?h_vid={h_vid}&h_vw=y",
            '图片URLs': [], 'sale_status': item.get('sale_status'),
        }
        car_data['extra_fields'] = {'list_only': True}
        car_data['h_vid'] = h_vid
        return car_data

    def build_rows(self):
        """把爬取到的车辆数据转换为统一的行格式（CSV 备份与直接入库共用）。"""
        rows = []
        current_timestamp = int(time.time())

        for i, car in enumerate(self.car_data, 1):
            current_price = car.get('current_price')
            original_price = car.get('original_price')
            # extra_fields = 描述提取特征 + 列表页浏览/留言数（合并写入 jsonb）
            extra_fields = dict(car.get('extra_fields') or {})
            list_meta = car.get('_list') or {}
            for key in ('view_count', 'comment_count'):
                if list_meta.get(key) is not None:
                    extra_fields[key] = list_meta[key]

            # vehicle_id 优先取详情页「編號」；缺失时回退列表页 h_vid。
            # 不回退的话 importer 会因主键为空把整条记录静默丢掉（无日志无计数）。
            native_id = car.get('h_vid')
            vehicle_id = car.get('編號', '') or (str(native_id) if native_id else '')
            if not vehicle_id:
                logger.error(
                    f"车辆既无「編號」也无 h_vid，无法生成 vehicle_id，本条已丢弃："
                    f"{car.get('網址', '')}"
                )

            rows.append({
                'vehicle_id': vehicle_id,
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
                'extra_fields': json.dumps(extra_fields, ensure_ascii=False) if extra_fields else ''
            })
        return rows

    def create_csv_file(self, filename=None, append_mode=False, rows=None):
        """写入CSV备份文件（入库不再依赖此文件，仅供排查/审计）。"""
        if rows is None:
            rows = self.build_rows()

        if not rows:
            logger.warning("没有数据可写入CSV")
            return None

        if filename is None:
            filename = f"car_data_{self.vehicle_type}.csv"

        csv_path = os.path.join(self.output_dir, filename)
        df = pd.DataFrame(rows)

        string_columns = ['year', 'phone_number', 'seats', 'engine_volume']
        for col in string_columns:
            if col in df.columns:
                df[col] = df[col].map(stringify_cell)

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

    def _process_extra_fields(self, car_data):
        """根据车辆类型处理扩展字段"""
        extra_fields = {}

        if car_data is None:
            return extra_fields

        description = car_data.get('簡評', '')

        # 通用提取：过户手数 / 里程 / 牌費到期 / 行水貨 / 中港牌 / 換車標記
        # 手数真实写法：0手/1手/2手、0首（"17製0首"），需排除"首次登記"误匹配
        hand = re.search(r'(?<![0-9])([0-9])\s*手', description) \
            or re.search(r'(?<![0-9])([0-9])\s*首(?!次)', description)
        if hand:
            extra_fields['hand_count'] = int(hand.group(1))
        elif '一手' in description:
            extra_fields['hand_count'] = 1
        elif '二手' in description:
            extra_fields['hand_count'] = 2

        # 里程：10多萬公里(低估) > 11.8萬公里 > 93000km
        mileage_wan_plus = re.search(r'([0-9]{1,3})\s*多\s*萬\s*(?:[kK][mM]|公里)', description)
        mileage_wan = re.search(r'([0-9][0-9,，.]{0,9})\s*萬\s*(?:[kK][mM]|公里)', description)
        mileage_plain = re.search(r'([0-9][0-9,，]{3,7})\s*(?:[kK][mM]|公里)', description)
        if mileage_wan_plus:
            extra_fields['mileage_km'] = int(mileage_wan_plus.group(1)) * 10000
        elif mileage_wan:
            extra_fields['mileage_km'] = int(float(mileage_wan.group(1).replace(',', '')) * 10000)
        elif mileage_plain:
            extra_fields['mileage_km'] = int(mileage_plain.group(1).replace(',', ''))

        license_match = re.search(r'牌\s*費\s*[到至]\s*([0-9]{4}\s*年?|[0-9]{1,2}\s*月)', description)
        if license_match:
            extra_fields['license_until'] = license_match.group(1).strip()

        if '行貨' in description:
            extra_fields['import_type'] = '行貨'
        elif '水貨' in description:
            extra_fields['import_type'] = '水貨'

        if '中港' in description:
            extra_fields['china_plate'] = True

        if re.search(r'\bswap\b', description, re.IGNORECASE) or '換車' in description:
            extra_fields['is_swap'] = True

        if self.vehicle_type == 1:
            mileage_match = re.search(r'(\d+[,，]?\d*)\s*(?:km|公里)', description)
            if mileage_match:
                extra_fields['mileage'] = f"{mileage_match.group(1)}km"

            color_match = re.search(r'([黑白红蓝银灰金棕绿紫])色(?:系)?', description)
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
            cargo_match = re.search(r'(\d+\.?\d*)\s*(?:吨|T)', description)
            if cargo_match:
                extra_fields['cargo_capacity'] = f"{cargo_match.group(1)}吨"

            length_match = re.search(r'(\d+\.?\d*)\s*(?:米|m)', description)
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

            mileage_match = re.search(r'(\d+[,，]?\d*)\s*(?:km|公里)', description)
            if mileage_match:
                extra_fields['mileage'] = f"{mileage_match.group(1)}km"

            color_match = re.search(r'([黑白红蓝银灰金棕绿紫])色(?:系)?', description)
            if color_match:
                extra_fields['color'] = f"{color_match.group(1)}色"

        elif self.vehicle_type == 5:
            year_match = re.search(r'(\d{4})', description)
            if year_match:
                extra_fields['classic_year'] = year_match.group(1)

            mileage_match = re.search(r'(\d+[,，]?\d*)\s*(?:km|公里)', description)
            if mileage_match:
                extra_fields['mileage'] = f"{mileage_match.group(1)}km"

            if '收藏' in description or '經典' in description:
                extra_fields['collection_value'] = '收藏级'

        return extra_fields if extra_fields else {}

    def _handle_anti_crawler(self, trigger_type):
        """记录一次busy撞击，判定是否触发全局冷却或终止（全程持锁）。

        判据是"连续失败链"：连续 fail_streak_threshold 次 busy 且中间无任何
        成功请求（成功会清零），等于实测证明"换IP无效"——按档位全局冷却
        15/30/60 分钟；档位用尽后再次攒满则终止本轮爬取。
        判断与升档必须在同一把锁内完成：多线程齐撞时若分离，每个线程都会
        看到"达到阈值"而各自升档，一次齐撞直接跳到终止。
        """
        with self._anti_lock:
            self._busy_streak += 1
            logger.warning(
                f"{trigger_type}页面撞busy（连续第{self._busy_streak}/"
                f"{self.fail_streak_threshold}次）"
            )

            if self._busy_streak < self.fail_streak_threshold:
                return True  # 换IP重试通常有效，继续

            if self._cooldown_level >= len(self.cooldown_seconds):
                logger.error("连续busy已耗尽全部冷却档位仍无成功，终止爬取")
                return False

            self._cooldown_level += 1
            cooldown = self.cooldown_seconds[self._cooldown_level - 1]
            self._cooldown_until = time.monotonic() + cooldown
            self._busy_streak = 0  # 冷却后重新计满一整条失败链才升下一档
            logger.warning(
                f"连续{self.fail_streak_threshold}次busy且无成功（换IP无效），"
                f"全局冷却{cooldown / 60:.0f}分钟"
                f"（第{self._cooldown_level}/{len(self.cooldown_seconds)}档）"
            )
            return True


def scrape_vehicle_type(vehicle_type, pages, csv_filename, start_page=1, db_importer=None,
                        page_hook=None):
    """爬取指定类型的车辆，逐页直接入库（CSV 仅作备份，入库不依赖它）。

    Args:
        vehicle_type: 车辆类型ID
        pages: 总共爬取多少页
        csv_filename: CSV备份文件名
        start_page: 起始页码（默认为1）
        db_importer: 复用的 FastCSVImporter；为 None 时内部创建
        page_hook: 每页开始时调用一次的可选回调（无参）。service 用它刷新运行锁的
            时间戳 —— 单轮 12 小时远超任何合理的 stale 阈值，靠固定阈值扛不住，
            必须在长跑过程中持续证明「我还活着」。回调异常不得影响爬取主流程。

    Returns:
        dict: 本类型的爬取与入库统计
    """
    print(f"\n=== 开始爬取 (类型{vehicle_type}) ===")

    crawl_start_time = time.time()
    pages_scraped = 0
    network_skipped_pages = 0
    total_vehicles = 0
    detail_errors = 0
    proxy_fail_count = 0
    anti_crawler_triggered = 0
    status = 'success'

    # 入库统计（内存传递，不再走环境变量）
    new_total = 0
    updated_total = 0
    error_total = 0
    import_seconds = 0.0

    # get_proxy_manager 单例首次创建时已从数据库随机抽取代理池，
    # 不再强制 refresh 二次连库（每个类型启动连两次库属冗余，且曾是卡死点）
    proxy_manager = get_proxy_manager(pool_size=DEFAULT_PROXY_POOL_SIZE)

    # 显示代理统计信息
    stats = proxy_manager.get_stats()
    print(f"[*] 代理池已就绪: {stats.get('pool_size', 0)}个代理")

    if db_importer is None:
        from carinfo.core.importer import FastCSVImporter
        db_importer = FastCSVImporter()

    append_mode = os.path.exists(csv_filename)
    if append_mode:
        print(f"发现现有CSV备份，将追加数据到: {csv_filename}")
    else:
        print(f"将创建CSV备份: {csv_filename}")

    vehicle_type_names = {1: '私家车', 2: '客货车', 3: '货车', 4: '电单车', 5: '经典车'}
    vehicle_type_name = vehicle_type_names.get(vehicle_type, f'类型{vehicle_type}')

    # spider 实例在整轮循环外创建一次：反爬触发计数 / 动态延迟 / 请求计数
    # 必须跨页累积，否则三级退避永远停在第一档、"超过3次终止"永不触发
    scraper = Car28Spider(pages, vehicle_type)

    for page in range(start_page, pages + 1):
        print(f'\n=== 正在处理第 {page} 页 ===')

        # 刷新运行锁时间戳（service 传入）。单轮 12 小时，若不持续 touch，
        # 锁会在中途被判 stale，次日调度就可能并发启动第二个进程。
        if page_hook is not None:
            try:
                page_hook()
            except Exception as e:
                logger.warning(f'page_hook 执行失败（不影响爬取）：{e}')

        # 每页清空，使 car_data 只含当前页结果（实例复用后不能累积）
        scraper.car_data = []
        proxy_fail_before = scraper.proxy_fail_count

        print(f'正在获取第 {page} 页车辆列表...')
        decoded_html = scraper.get_html_1(page)

        if decoded_html is None:
            if scraper.anti_crawler_stop:
                print(f'第 {page} 页确认触发反爬终止，停止爬取')
                anti_crawler_triggered += 1
                status = 'partial'
                break

            # 网络型失败（DNS/超时/5xx 等，无反爬证据）：不终止整个 run，
            # 等待后重试本页，仍失败则跳过该页继续（该页车辆由后续每日滚动爬取补齐）
            recovered = False
            for extra_attempt in (1, 2):
                wait_seconds = 60 * extra_attempt
                print(f'第 {page} 页网络型失败（非反爬），等待 {wait_seconds} 秒后重试本页'
                      f'（兜底 {extra_attempt}/2）...')
                time.sleep(wait_seconds)
                decoded_html = scraper.get_html_1(page)
                if decoded_html is not None:
                    recovered = True
                    break
                # ⚠️ 每轮重试都要判反爬终止：get_html_1 内部可能已把 anti_crawler_stop
                # 置真（busy 连续失败超阈值）。不判的话，反爬终止会被误标成
                # "网络跳页"，还会白等 60+120 秒才继续（实测两个秒退回仍照睡）。
                if scraper.anti_crawler_stop:
                    print(f'第 {page} 页重试期间确认触发反爬终止，停止爬取')
                    anti_crawler_triggered += 1
                    status = 'partial'
                    break

            if scraper.anti_crawler_stop:
                break

            if not recovered:
                print(f'第 {page} 页连续网络失败，跳过本页继续爬取')
                network_skipped_pages += 1
                status = 'partial'
                continue

        dateCode_i = scraper.get_date_code(decoded_html)

        if not dateCode_i:
            print(f'第 {page} 页没有找到车辆数据')
            continue

        print(f'第 {page} 页找到 {len(dateCode_i)} 个车辆')

        # 过滤掉未解析出 code 的条目（get_date_code 在 onclick 正则不匹配时返回 None）。
        # 列表条目 dict 与详情抓取结果同源传递，避免并行列表错位。
        valid_items = [it for it in dateCode_i if it.get('code')]
        if len(valid_items) < len(dateCode_i):
            print(f'第 {page} 页有 {len(dateCode_i) - len(valid_items)} 条记录缺少车辆ID，已跳过')

        scraper.scrape_cars(valid_items)

        detail_errors += len(valid_items) - len(scraper.car_data)
        # 复用实例后 proxy_fail_count 是累计值，这里只能取本页增量
        proxy_fail_count += scraper.proxy_fail_count - proxy_fail_before

        # anti_crawler_stop 必须在此读取：它只在详情请求路径被置位，
        # 只判 car_data 为空的话，本页恰巧全是已售车时终止信号会被吞掉
        if scraper.anti_crawler_stop or (
            len(scraper.car_data) == 0
            and any(it['sale_status'] == "未售" for it in valid_items)
        ):
            print(f'第 {page} 页详情爬取因反爬虫终止，停止爬取')
            anti_crawler_triggered += 1
            status = 'partial'
            break

        if scraper.car_data:
            rows = scraper.build_rows()

            # CSV 备份只写不读，供排查；失败不影响入库
            csv_path = scraper.create_csv_file(csv_filename, append_mode, rows)
            if csv_path:
                print(f'第 {page} 页数据已保存，获取 {len(rows)} 个车辆')
                total_vehicles += len(rows)
                append_mode = True
            else:
                print(f'第 {page} 页CSV备份写入失败')

            # 直接入库；失败不中断爬取（数据已留在CSV备份里）
            try:
                result = db_importer.import_rows(rows, vehicle_type)
                if result.get('success'):
                    new_total += result.get('new', 0)
                    updated_total += result.get('updated', 0)
                error_total += result.get('errors', 0)
                import_seconds += result.get('duration', 0.0)
            except Exception as e:
                print(f'[ERROR] 第 {page} 页入库异常（数据已保留在CSV备份）: {e}')
                error_total += len(rows)

        pages_scraped += 1

        if page < pages:
            delay = random.uniform(3.0, 6.0)
            print(f'休息 {delay:.1f} 秒，准备处理下一页...')
            time.sleep(delay)

    crawl_duration = time.time() - crawl_start_time
    print(f'\n爬取完成！车辆数: {total_vehicles}, 爬取页数: {pages_scraped}, '
          f'反爬触发: {anti_crawler_triggered}, 网络跳页: {network_skipped_pages}')
    print(f'入库统计！新增: {new_total}, 更新: {updated_total}, 错误: {error_total}')

    crawl_stats = {
        'vehicle_type': vehicle_type,
        'vehicle_type_name': vehicle_type_name,
        'pages_scraped': pages_scraped,
        'total_vehicles': total_vehicles,
        'detail_errors': detail_errors,
        'new_vehicles': new_total,
        'updated_vehicles': updated_total,
        'error_count': error_total + detail_errors,
        'proxy_used_count': stats.get('pool_size', 0),
        'proxy_fail_count': proxy_fail_count,
        'anti_crawler_triggered': anti_crawler_triggered,
        'network_skipped_pages': network_skipped_pages,
        'crawl_duration': round(crawl_duration, 2),
        'import_duration': round(import_seconds, 2),
        'status': status,
    }

    from carinfo.core.importer import record_crawl_log
    if record_crawl_log(db_importer, crawl_stats):
        print('[OK] 爬取日志已记录到 crawl_logs')

    # 特征表重算：本类型跑完、且确实有新数据进库时才做。
    # 为什么放在这里而不是 service 的整轮之后：整轮是「多类型串行」，跑完再算要等
    # 十几个小时；而每类型跑完就算，既让特征表尽早跟上，单次成本也只有几秒
    # （全量重算 ~4s）。用独立短连接，不碰 db_importer 的连接。
    # 失败只告警不抛出 —— 特征表是派生数据，晚一轮重算不影响爬虫正确性。
    if status == 'success' and (new_total > 0 or updated_total > 0):
        _rebuild_features_after_crawl(new_total, updated_total)

    return crawl_stats


def _rebuild_features_after_crawl(new_total: int, updated_total: int) -> None:
    """爬完一类后重算 market_stats / vehicle_features（独立连接，失败不致命）。"""
    t0 = time.time()
    try:
        import psycopg2

        from carinfo.search.features import main as rebuild_features
    except Exception as e:  # noqa: BLE001
        logger.warning(f"特征表重算不可用（import 失败），跳过: {e}")
        return

    print(f'[特征重算] 本类新增 {new_total} / 更新 {updated_total}，开始重算派生表...')
    try:
        # features.main() 自己读环境变量建连、自己打印，返回值 0 表示成功。
        # 这里用 argparse 之外的直接调用：main() 的 --dry-run 开关默认关闭。
        rc = rebuild_features(argv=[])
        logger.info(f"特征表重算完成 rc={rc}，耗时 {time.time() - t0:.1f}s")
        print(f'[特征重算] 完成，耗时 {time.time() - t0:.1f}s')
    except SystemExit as e:  # main() 内部用 raise SystemExit 退出
        logger.warning(f"特征表重算异常退出 code={e.code}，耗时 {time.time() - t0:.1f}s")
    except Exception as e:  # noqa: BLE001
        logger.error(f"特征表重算失败（不影响本次爬取结果）: {e}")


def main():
    """主函数：爬取并直接入库（CSV 仅作备份）"""
    import argparse

    parser = argparse.ArgumentParser(description='28car.com 车辆信息爬取工具')
    parser.add_argument('--config-file', default='config.json', help='配置文件路径')
    parser.add_argument('--start-page', type=int, default=1,
                        help='起始页码（断点续爬：如上次在第 360 页中断，则传 360）')

    args = parser.parse_args()

    print(f"=== 配置文件模式启动 ===")
    print(f"配置文件: {args.config_file}")

    enabled_types = load_config_from_file(args.config_file)

    if not enabled_types:
        print("[ERROR] 配置文件中没有需要爬取的车辆类型（所有类型的 pages 都为 0）")
        sys.exit(1)

    print(f"将爬取 {len(enabled_types)} 种车辆类型:")
    for type_id, type_config in enabled_types.items():
        print(f"  - {type_config['name']} (类型{type_id}): {type_config['pages']}页")

    from carinfo.core.importer import FastCSVImporter

    csv_dir = "data/csv"
    os.makedirs(csv_dir, exist_ok=True)

    db_importer = FastCSVImporter()
    total_vehicles = 0
    try:
        for type_id, type_config in enabled_types.items():
            csv_filename = os.path.join(csv_dir, f"car_data_{type_id}.csv")
            stats = scrape_vehicle_type(
                type_id, type_config['pages'], csv_filename,
                start_page=args.start_page, db_importer=db_importer
            )
            total_vehicles += stats['total_vehicles']
            print(f"\n类型{type_id}统计: 爬取{stats['total_vehicles']}条，"
                  f"新增{stats['new_vehicles']}，更新{stats['updated_vehicles']}，"
                  f"错误{stats['error_count']}，状态{stats['status']}")
    finally:
        db_importer.close()

    print(f"\n=== 所有类型爬取完成！ ===")
    print(f"总共获取车辆数: {total_vehicles}")


if __name__ == "__main__":
    main()
