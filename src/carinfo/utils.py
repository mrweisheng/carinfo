#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通用工具函数：价格 / 联系人解析。

只保留实际被调用的函数。其余历史函数（parse_extra_fields /
validate_vehicle_data / sanitize_filename / parse_json_safe /
truncate_string / clean_data）已删除——需要时翻 git 历史。
"""

import re
from typing import Optional, Tuple


def parse_price(price_str: str) -> Tuple[Optional[float], Optional[float]]:
    """
    解析价格字符串。

    Args:
        price_str: 价格字符串，如 "54,000[原價$57,000]" 或 "60,000"

    Returns:
        (当前价格, 原价) 元组
    """
    if not price_str:
        return None, None

    try:
        price_str = str(price_str).replace('HKD$', '').replace('HKD', '').strip()

        current_price = None
        original_price = None

        if '[' in price_str and '原價' in price_str:
            current_part = price_str.split('[')[0].strip()
            current_price = extract_number(current_part)

            original_part = price_str.split('原價')[1].split(']')[0].strip()
            original_price = extract_number(original_part)
        else:
            current_price = extract_number(price_str)

        return current_price, original_price
    except Exception:
        return None, None


def extract_number(price_str: str) -> Optional[float]:
    """从字符串中提取数字，失败返回 None。"""
    if not price_str:
        return None

    clean_str = price_str.replace(',', '').replace('$', '').strip()
    try:
        return float(clean_str)
    except (ValueError, TypeError):
        return None


def parse_contact_info(contact_str: str) -> Tuple[Optional[str], Optional[str]]:
    """
    解析联系人信息。

    Args:
        contact_str: 联系人信息字符串

    Returns:
        (联系人姓名, 电话号码) 元组
    """
    if not contact_str:
        return None, None

    contact_name = None
    phone_number = None

    if '電話:' in contact_str:
        parts = contact_str.split('電話:')
        if len(parts) == 2:
            contact_name = parts[0].strip()
            phone_number = parts[1].strip()
    elif '電話' in contact_str:
        phone_match = re.search(r'電話\s*(\d+)', contact_str)
        if phone_match:
            phone_number = phone_match.group(1)
            name_part = contact_str.split('電話')[0].strip()
            if name_part:
                contact_name = name_part
    else:
        phone_match = re.search(r'(\d{8})', contact_str)
        if phone_match:
            phone_number = phone_match.group(1)
            name_part = contact_str.replace(phone_number, '').strip()
            if name_part:
                contact_name = name_part

    return contact_name, phone_number
