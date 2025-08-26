#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试脚本：根据指定 vehicle_id 的车辆图片，调用 Google Gemini REST API 识别货车运输用途（transport_purpose）。
分类枚举：冷凍、拖頭、密斗、其他。若无图片则默认为"其他"。

使用方式：
  直接修改脚本中的"配置区"变量（数据库连接、VEHICLE_ID、UPDATE_DB、MAX_IMAGES、GEMINI_API_KEY 等），保存后运行：
    python test_gemini_transport_purpose.py

注意：此脚本使用 Gemini REST API，无需安装 Google SDK，只需 requests 库。
"""

import os
import sys
import enum
import logging
import json
import base64
from typing import List, Optional, Tuple
import pymysql
pymysql.install_as_MySQLdb()
import requests
from pymysql import MySQLError
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# 日志配置
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class TransportPurpose(enum.Enum):
    LENGDONG = "冷凍"
    TUOTOU = "拖頭"
    MIDOU = "密斗"
    QITA = "其他"


def get_db_connection(host: str, user: str, password: str, database: str, port: int):
    return pymysql.connect(
        host=host,
        user=user,
        password=password,
        database=database,
        port=port,
        autocommit=False,
    )


def fetch_image_urls(conn, vehicle_id: str, limit: Optional[int] = None) -> List[str]:
    sql = """
        SELECT image_url
        FROM vehicle_images
        WHERE vehicle_id = %s
        ORDER BY image_order ASC, id ASC
    """
    urls: List[str] = []
    with conn.cursor() as cur:
        cur.execute(sql, (vehicle_id,))
        for row in cur.fetchall():
            urls.append(row[0])
            if limit and len(urls) >= limit:
                break
    return urls


def _guess_mime_from_url(url: str) -> str:
    u = url.lower()
    if u.endswith('.png'): return 'image/png'
    if u.endswith('.webp'): return 'image/webp'
    if u.endswith('.gif'): return 'image/gif'
    if u.endswith('.jpeg') or u.endswith('.jpg'): return 'image/jpeg'
    return 'image/jpeg'


def get_image_base64_from_url(url: str, timeout: int = 20) -> Tuple[str, str]:
    """通过URL获取图片并转换为base64编码，返回 (base64_data, mime_type)"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
    }
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    image_bytes = resp.content
    mime_type = resp.headers.get('Content-Type') or _guess_mime_from_url(url)
    base64_data = base64.b64encode(image_bytes).decode('utf-8')
    return base64_data, mime_type


def classify_transport_purpose_with_gemini_rest(api_key: str, image_urls: List[str], extra_hint: Optional[str] = None) -> str:
    """使用 Gemini REST API 进行分类识别"""
    
    # 构建系统提示
    system_prompt = (
        "你是一位车辆识别专家。请根据给定的货车图片，判断其运输用途，并在以下四类中严格选择一个：\n"
        "1) 冷凍：有冷藏/冷冻厢体、冷冻机组或明显冷链标识的冷冻车；\n"
        "2) 拖頭：半挂牵引车头（tractor head），通常仅有车头，用于拖挂半挂车；\n"
        "3) 密斗：密封厢式/箱式车（厢式货车、密斗车、货柜厢），非冷冻；\n"
        "4) 其他：不属于上述类别或图片无法明确辨识。\n"
        "如果不确定或图片不足，请选择'其他'。请只输出以下四个词之一：冷凍、拖頭、密斗、其他。不要输出任何解释或标点。"
    )
    
    if extra_hint:
        system_prompt += f"\n补充说明：{extra_hint}"
    
    # 构建请求内容
    contents = [{"parts": [{"text": system_prompt}]}]
    
    # 添加图片
    for i, url in enumerate(image_urls, start=1):
        try:
            logger.info(f"处理图片 {i}/{len(image_urls)}: {url}")
            base64_data, mime_type = get_image_base64_from_url(url)
            contents[0]["parts"].append({
                "inline_data": {
                    "mime_type": mime_type,
                    "data": base64_data
                }
            })
        except Exception as e:
            logger.warning(f"获取图片失败，跳过: {e}")
            continue
    
    # 如果没有成功获取任何图片，返回默认值
    if len(contents[0]["parts"]) == 1:  # 只有文本提示，没有图片
        logger.warning("所有图片获取失败，返回默认分类")
        return TransportPurpose.QITA.value
    
    # 图像数量日志与提示
    num_images = len(contents[0]["parts"]) - 1
    logger.info(f"准备调用 Gemini REST API，图片数: {num_images}")
    if num_images > 4:
        logger.warning("图片数量较多，可能导致请求体过大而被 API 拒绝(400)。可在 .env 中将 MAX_IMAGES 调小（如 3 或 2）以规避。")
    
    # 调用 Gemini REST API
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    
    payload = {
        "contents": contents,
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 1000,
            "topP": 0.8,
            "topK": 10
        }
    }
    
    headers = {
        "Content-Type": "application/json"
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        # 如果返回非 2xx，详细记录响应体以便排查
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as e:
            resp = getattr(e, 'response', None) or response
            try:
                logger.error(f"API 错误，状态码={resp.status_code}，响应体={resp.text}")
            except Exception:
                logger.error(f"API 错误: {e}")
            return TransportPurpose.QITA.value
        
        result_data = response.json()
        logger.debug(f"API 响应: {result_data}")
        
        # 解析响应
        if "candidates" in result_data and result_data["candidates"]:
            candidate = result_data["candidates"][0]
            if "content" in candidate and "parts" in candidate["content"]:
                text = candidate["content"]["parts"][0].get("text", "").strip()
                logger.info(f"Gemini REST API 识别结果: {text}")
                
                # 规范化：仅保留四个允许词之一
                allowed = {t.value for t in TransportPurpose}
                for word in allowed:
                    if word in text:
                        return word
                
                # 如果模型返回了其它内容，回退为"其他"
                logger.warning(f"模型返回未知分类 '{text}'，使用默认值")
                return TransportPurpose.QITA.value
        
        logger.error(f"API 响应格式异常: {result_data}")
        return TransportPurpose.QITA.value
        
    except requests.exceptions.RequestException as e:
        logger.error(f"API 调用失败: {e}")
        return TransportPurpose.QITA.value
    except json.JSONDecodeError as e:
        logger.error(f"API 响应解析失败: {e}")
        return TransportPurpose.QITA.value
    except Exception as e:
        logger.error(f"分类识别过程出错: {e}")
        return TransportPurpose.QITA.value


def update_transport_purpose(conn, vehicle_id: str, value: str) -> None:
    sql = "UPDATE vehicles SET transport_purpose = %s, updated_at = NOW() WHERE vehicle_id = %s"
    with conn.cursor() as cur:
        cur.execute(sql, (value, vehicle_id))
    conn.commit()


# === 配置区（直接修改以下变量即可，无需命令行参数）===
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = int(os.getenv("DB_PORT"))
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")
VEHICLE_ID = "s2603737"  # 需要识别的 vehicle_id
MAX_IMAGES = int(os.getenv("MAX_IMAGES", "6"))  # 最多使用图片数量
UPDATE_DB = True  # 是否写回数据库
EXTRA_HINT = None  # 可选的额外提示给模型
# === 配置区结束 ===


def main():
    # 使用脚本内置配置，无需命令行参数
    api_key = GEMINI_API_KEY
    if not api_key or api_key == "PUT_YOUR_GEMINI_API_KEY_HERE":
        logger.error("未检测到 GEMINI_API_KEY 环境变量或脚本内未设置 API Key，请先配置后再运行。")
        print("请设置环境变量: set GEMINI_API_KEY=your_api_key_here")
        print("或直接修改脚本中的 GEMINI_API_KEY 变量")
        sys.exit(1)

    # 连接数据库
    try:
        conn = get_db_connection(DB_HOST, DB_USER, DB_PASSWORD, DB_NAME, DB_PORT)
        logger.info(f"成功连接数据库 {DB_HOST}:{DB_PORT}/{DB_NAME}")
    except MySQLError as e:
        logger.error(f"连接数据库失败: {e}")
        sys.exit(1)

    try:
        # 获取图片URL
        urls = fetch_image_urls(conn, VEHICLE_ID, limit=MAX_IMAGES)
        logger.info(f"vehicle_id={VEHICLE_ID}，获取到图片 {len(urls)} 张")

        if not urls:
            # 无图片则默认其他
            result = TransportPurpose.QITA.value
            logger.info("该车辆没有图片，默认识别为：其他")
            print(result)
            if UPDATE_DB:
                try:
                    update_transport_purpose(conn, VEHICLE_ID, result)
                    logger.info("数据库已更新 transport_purpose=其他")
                except MySQLError as e:
                    logger.error(f"更新数据库失败: {e}")
            return

        # 调用 Gemini REST API 进行分类
        result = classify_transport_purpose_with_gemini_rest(api_key, urls, EXTRA_HINT)
        print(result)

        # 可选：写回数据库
        if UPDATE_DB and result:
            try:
                update_transport_purpose(conn, VEHICLE_ID, result)
                logger.info(f"数据库已更新 transport_purpose={result}")
            except MySQLError as e:
                logger.error(f"更新数据库失败: {e}")
                
    finally:
        try:
            conn.close()
            logger.info("数据库连接已关闭")
        except Exception:
            pass


if __name__ == "__main__":
    main()