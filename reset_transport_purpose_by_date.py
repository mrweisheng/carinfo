#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
根据指定日期，批量将 vehicles 表中 created_at 在该日期之后的数据的 transport_purpose 字段置为 NULL。

使用说明：
1) 在 .env 或系统环境变量中配置数据库连接：
   - DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME
2) 运行脚本并传入日期参数（支持 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS）：
   python reset_transport_purpose_by_date.py 2025-08-15
   或设置环境变量 RESET_AFTER_DATE：
   set RESET_AFTER_DATE=2025-08-15 && python reset_transport_purpose_by_date.py

注意：仅执行单条 UPDATE 语句，直接将符合条件的数据的 transport_purpose 置为 NULL。
"""

import os
import sys
import logging
from datetime import datetime
from typing import Optional

import pymysql
from pymysql import MySQLError
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def parse_input_datetime(value: str) -> str:
    """解析输入的日期/时间字符串，返回 MySQL 可识别的 'YYYY-MM-DD HH:MM:SS' 字符串。
    支持两种格式：
    - YYYY-MM-DD
    - YYYY-MM-DD HH:MM:SS
    """
    value = value.strip()
    dt: Optional[datetime] = None
    # 先尝试完整时间
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        raise ValueError("日期格式不正确，请使用 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS")
    # 若只有日期，则默认 00:00:00
    if len(value) == 10:
        return dt.strftime("%Y-%m-%d 00:00:00")
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def get_db_connection(host: str, user: str, password: str, database: str, port: int):
    return pymysql.connect(
        host=host,
        user=user,
        password=password,
        database=database,
        port=port,
        autocommit=False,
    )


def main():
    # 读取日期参数
    input_date = None
    if len(sys.argv) >= 2:
        input_date = sys.argv[1]
    if not input_date:
        input_date = os.getenv("RESET_AFTER_DATE")

    if not input_date:
        print("用法: python reset_transport_purpose_by_date.py <YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS>")
        print("或设置环境变量 RESET_AFTER_DATE 后运行。")
        sys.exit(1)

    try:
        cutoff_ts = parse_input_datetime(input_date)
    except ValueError as e:
        logger.error(str(e))
        sys.exit(1)

    # 读取数据库配置
    DB_HOST = os.getenv("DB_HOST")
    DB_PORT = int(os.getenv("DB_PORT", "3306"))
    DB_USER = os.getenv("DB_USER")
    DB_PASSWORD = os.getenv("DB_PASSWORD")
    DB_NAME = os.getenv("DB_NAME")

    missing = [k for k, v in {
        "DB_HOST": DB_HOST,
        "DB_PORT": DB_PORT,
        "DB_USER": DB_USER,
        "DB_PASSWORD": DB_PASSWORD,
        "DB_NAME": DB_NAME,
    }.items() if v in (None, "")]
    if missing:
        logger.error(f"缺少数据库环境变量: {missing}")
        sys.exit(1)

    # 连接数据库
    try:
        conn = get_db_connection(DB_HOST, DB_USER, DB_PASSWORD, DB_NAME, DB_PORT)
        logger.info(f"成功连接数据库 {DB_HOST}:{DB_PORT}/{DB_NAME}")
    except MySQLError as e:
        logger.error(f"连接数据库失败: {e}")
        sys.exit(1)

    try:
        sql = (
            "UPDATE vehicles "
            "SET transport_purpose = NULL, updated_at = NOW() "
            "WHERE created_at > %s and vehicle_type=3"
        )
        with conn.cursor() as cur:
            logger.info(f"执行更新：将 created_at > {cutoff_ts} 的记录的 transport_purpose 置为 NULL ...")
            cur.execute(sql, (cutoff_ts,))
            affected = cur.rowcount
        conn.commit()
        logger.info(f"更新完成，受影响行数: {affected}")
        print(f"OK - affected_rows={affected}")
    except MySQLError as e:
        logger.error(f"更新失败，已回滚: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        sys.exit(1)
    finally:
        try:
            conn.close()
            logger.info("数据库连接已关闭")
        except Exception:
            pass


if __name__ == "__main__":
    main()