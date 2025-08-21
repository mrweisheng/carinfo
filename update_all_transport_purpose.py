#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量更新脚本：为数据库中“所有货车（vehicle_type=3）”补全运输用途 transport_purpose。
- 若 vehicles.transport_purpose 已有值（非空），则跳过，避免不必要的 token 消耗
- 若无任何图片，则不调用模型，直接写入“其他”
- 否则通过 Gemini REST API 对前 MAX_IMAGES 张图片进行识别，写入识别结果
- 数据库、API Key 等配置从 .env 或系统环境变量读取
"""

import os
import logging
import time
from typing import List, Optional

from dotenv import load_dotenv
import pymysql
from pymysql import MySQLError

from test_gemini_transport_purpose import (
    TransportPurpose,
    fetch_image_urls,
    classify_transport_purpose_with_gemini_rest,
    get_db_connection,
    update_transport_purpose,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def load_config():
    load_dotenv()
    config = {
        "GEMINI_API_KEY": os.getenv("GEMINI_API_KEY", "").strip(),
        "DB_HOST": os.getenv("DB_HOST", "127.0.0.1"),
        "DB_PORT": int(os.getenv("DB_PORT", "3306")),
        "DB_USER": os.getenv("DB_USER", "root"),
        "DB_PASSWORD": os.getenv("DB_PASSWORD", ""),
        "DB_NAME": os.getenv("DB_NAME", "car_info_db"),
        "MAX_IMAGES": int(os.getenv("MAX_IMAGES", "6")),
        "BATCH_SIZE": int(os.getenv("BATCH_SIZE", "200")),
        "SLEEP_BETWEEN_CALLS": float(os.getenv("SLEEP_BETWEEN_CALLS", "0")),
        "ONLY_TRUCKS": os.getenv("ONLY_TRUCKS", "true").lower() != "false",
    }
    return config


def ensure_transport_purpose_column(conn, db_name: str) -> None:
    sql_check = """
        SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA=%s AND TABLE_NAME='vehicles' AND COLUMN_NAME='transport_purpose'
    """
    with conn.cursor() as cur:
        cur.execute(sql_check, (db_name,))
        exists = cur.fetchone()[0] > 0
    if not exists:
        logger.warning("vehicles 表缺少 transport_purpose 字段，尝试自动添加...")
        alter_sql = "ALTER TABLE vehicles ADD COLUMN transport_purpose VARCHAR(20) NULL DEFAULT NULL COMMENT '货车运输用途：冷凍/拖頭/密斗/其他'"
        try:
            with conn.cursor() as cur:
                cur.execute(alter_sql)
            conn.commit()
            logger.info("已添加字段 transport_purpose")
        except MySQLError as e:
            logger.error(f"添加 transport_purpose 字段失败，请手动处理后重试：{e}")
            raise


def iter_vehicle_ids(conn, only_trucks: bool = True, batch_size: int = 500):
    # 使用基于主键 id 的 keyset 分页，避免在更新过程中因为 WHERE 条件变化造成 OFFSET 跳跃漏扫
    base_where = "(transport_purpose IS NULL OR TRIM(transport_purpose)='')"
    if only_trucks:
        base_where += " AND vehicle_type=3"
    sql = f"SELECT id, vehicle_id FROM vehicles WHERE {base_where} AND id > %s ORDER BY id ASC LIMIT %s"
    last_id = 0
    while True:
        with conn.cursor() as cur:
            cur.execute(sql, (last_id, batch_size))
            rows = cur.fetchall()
        if not rows:
            break
        for row_id, vehicle_id in rows:
            last_id = row_id
            yield vehicle_id


def main():
    cfg = load_config()
    api_key = cfg["GEMINI_API_KEY"]
    if not api_key:
        logger.error("未检测到 GEMINI_API_KEY，请在 .env 或系统环境变量中配置")
        print("请在 .env 设置 GEMINI_API_KEY=your_api_key")
        return

    try:
        conn = get_db_connection(cfg["DB_HOST"], cfg["DB_USER"], cfg["DB_PASSWORD"], cfg["DB_NAME"], cfg["DB_PORT"])
        logger.info(f"成功连接数据库 {cfg['DB_HOST']}:{cfg['DB_PORT']}/{cfg['DB_NAME']}")
    except MySQLError as e:
        logger.error(f"连接数据库失败: {e}")
        return

    processed = 0
    updated = 0
    skipped_existing = 0
    no_image_set_other = 0
    api_called = 0

    try:
        # 确保字段存在
        ensure_transport_purpose_column(conn, cfg["DB_NAME"])

        # 用流式批次遍历
        for vehicle_id in iter_vehicle_ids(conn, only_trucks=cfg["ONLY_TRUCKS"], batch_size=cfg["BATCH_SIZE"]):
            processed += 1

            # 再次防御性检查：若已有值则跳过
            with conn.cursor() as cur:
                cur.execute("SELECT transport_purpose FROM vehicles WHERE vehicle_id=%s", (vehicle_id,))
                cur_val = cur.fetchone()
            if cur_val and cur_val[0] and str(cur_val[0]).strip():
                skipped_existing += 1
                if processed % 100 == 0:
                    logger.info(f"进度: processed={processed}, updated={updated}, skipped_existing={skipped_existing}, no_image_set_other={no_image_set_other}, api_called={api_called}")
                continue

            # 拉图
            urls = fetch_image_urls(conn, vehicle_id, limit=cfg["MAX_IMAGES"])

            # 无图，直接写入“其他”（不调用模型）
            if not urls:
                try:
                    update_transport_purpose(conn, vehicle_id, TransportPurpose.QITA.value)
                    updated += 1
                    no_image_set_other += 1
                except MySQLError as e:
                    logger.error(f"更新车辆 {vehicle_id} 失败: {e}")
                if processed % 100 == 0:
                    logger.info(f"进度: processed={processed}, updated={updated}, skipped_existing={skipped_existing}, no_image_set_other={no_image_set_other}, api_called={api_called}")
                continue

            # 调用模型
            result = classify_transport_purpose_with_gemini_rest(api_key, urls)
            api_called += 1

            # 写回
            try:
                update_transport_purpose(conn, vehicle_id, result)
                updated += 1
            except MySQLError as e:
                logger.error(f"更新车辆 {vehicle_id} 失败: {e}")

            if cfg["SLEEP_BETWEEN_CALLS"] > 0:
                time.sleep(cfg["SLEEP_BETWEEN_CALLS"])

            if processed % 50 == 0:
                logger.info(f"进度: processed={processed}, updated={updated}, skipped_existing={skipped_existing}, no_image_set_other={no_image_set_other}, api_called={api_called}")

        logger.info(f"完成: processed={processed}, updated={updated}, skipped_existing={skipped_existing}, no_image_set_other={no_image_set_other}, api_called={api_called}")
        print(f"完成: processed={processed}, updated={updated}, skipped_existing={skipped_existing}, no_image_set_other={no_image_set_other}, api_called={api_called}")

    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()