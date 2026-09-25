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

#: 28car「聯絡人資料」行的邮箱（電郵型卖家只留邮箱，实测占在售 7.3%）
_EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')


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


def parse_contact_info(contact_str: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    解析联系人信息。

    Args:
        contact_str: 联系人信息字符串，两种形态（28car 实测零混合但代码要都对）：
            'Chan 電話:98524136'      → 电话型
            '張小姐 電郵:xxx@y.com.hk' → 邮箱型

    Returns:
        (联系人姓名, 电话号码, 邮箱) 三元组，无则 None

    防伪电话（2026-09 存量实测 93 条教训）：邮箱里的 8 连数字会被旧版
    误提为电话（如 ivan53247219@gmail.com → phone='53247219'）。
    所以**邮箱优先判定**：只要原文含邮箱，电话就只认「電話」标签，
    绝不从剩余文本/邮箱名里捞数字。
    """
    if not contact_str:
        return None, None, None

    text = str(contact_str).strip()
    if not text:
        return None, None, None

    email_match = _EMAIL_RE.search(text)
    if email_match:
        email = email_match.group(0)
        rest = _EMAIL_RE.sub('', text)

        phone = None
        phone_match = re.search(r'電話\s*[:：]?\s*([0-9]{8})', rest)
        if phone_match:
            phone = phone_match.group(1)
            rest = rest[:phone_match.start()] + rest[phone_match.end():]

        rest = re.sub(r'電郵\s*[:：]?\s*$', '', rest.strip())
        name = rest.strip(' ，,;；.、-：:') or None
        return name, phone, email

    # 无邮箱：沿用原有电话解析逻辑（存量 19926 条电话型 100% 由它解析）
    if '電話:' in text:
        parts = text.split('電話:')
        if len(parts) == 2:
            name = parts[0].strip() or None
            phone = parts[1].strip() or None
            return name, phone, None
    elif '電話' in text:
        phone_match = re.search(r'電話\s*(\d+)', text)
        if phone_match:
            name_part = text.split('電話')[0].strip()
            return (name_part or None), phone_match.group(1), None
    else:
        phone_match = re.search(r'(\d{8})', text)
        if phone_match:
            name_part = text.replace(phone_match.group(1), '').strip()
            return (name_part or None), phone_match.group(1), None

    return None, None, None
