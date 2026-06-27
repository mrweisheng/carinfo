#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
工具函数模块
提供价格解析、联系人解析、数据清洗等通用工具函数
"""

import re
import json
from typing import Optional, Tuple, Any, Dict


def parse_price(price_str: str) -> Tuple[Optional[float], Optional[float]]:
    """
    解析价格字符串
    
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
    """
    从字符串中提取数字
    
    Args:
        price_str: 包含数字的字符串
    
    Returns:
        提取出的数字，失败返回None
    """
    if not price_str:
        return None
    
    clean_str = price_str.replace(',', '').replace('$', '').strip()
    try:
        return float(clean_str)
    except (ValueError, TypeError):
        return None


def parse_contact_info(contact_str: str) -> Tuple[Optional[str], Optional[str]]:
    """
    解析联系人信息
    
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


def clean_data(value: Any) -> str:
    """
    清理数据值
    
    Args:
        value: 待清理的值
    
    Returns:
        清理后的字符串
    """
    if value is None or value == '':
        return ''
    str_value = str(value).strip()
    if str_value.endswith('.0'):
        return str_value[:-2]
    return str_value


def parse_extra_fields(description: str, vehicle_type: int) -> Dict[str, str]:
    """
    根据车辆类型从描述中解析扩展字段
    
    Args:
        description: 车辆描述文本
        vehicle_type: 车辆类型 (1-5)
    
    Returns:
        解析出的扩展字段字典
    """
    extra_fields = {}
    
    if not description:
        return extra_fields
    
    if vehicle_type == 1:
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
    
    elif vehicle_type in [2, 3]:
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
            features.append('纖維斗')
        if '孖屋' in description:
            features.append('孖屋')
        if features:
            extra_fields['features'] = ', '.join(features)
        
        if vehicle_type == 3:
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
    
    elif vehicle_type == 4:
        cc_match = re.search(r'(\d+)\s*cc', description, re.IGNORECASE)
        if cc_match:
            extra_fields['engine_cc'] = f"{cc_match.group(1)}cc"
        
        mileage_match = re.search(r'(\d+[,，]?\d*)\s*[km|公里]', description)
        if mileage_match:
            extra_fields['mileage'] = f"{mileage_match.group(1)}km"
        
        color_match = re.search(r'([黑白红蓝银灰金棕绿紫])[色|色系]', description)
        if color_match:
            extra_fields['color'] = f"{color_match.group(1)}色"
    
    elif vehicle_type == 5:
        year_match = re.search(r'(\d{4})', description)
        if year_match:
            extra_fields['classic_year'] = year_match.group(1)
        
        mileage_match = re.search(r'(\d+[,，]?\d*)\s*[km|公里]', description)
        if mileage_match:
            extra_fields['mileage'] = f"{mileage_match.group(1)}km"
        
        if '收藏' in description or '經典' in description:
            extra_fields['collection_value'] = '收藏级'
    
    return extra_fields


def validate_vehicle_data(data: dict) -> Tuple[bool, list]:
    """
    验证车辆数据的有效性
    
    Args:
        data: 车辆数据字典
    
    Returns:
        (是否有效, 错误信息列表)
    """
    errors = []
    
    if not data.get('vehicle_id'):
        errors.append("缺少车辆ID")
    
    price = data.get('price', '')
    if price:
        current, original = parse_price(price)
        if current is None:
            errors.append(f"价格格式无效: {price}")
    
    phone = data.get('phone_number', '')
    if phone and not re.match(r'^\d{8}$', phone):
        errors.append(f"电话号码格式无效: {phone}")
    
    year = data.get('year', '')
    if year:
        try:
            year_val = int(year)
            if year_val < 1900 or year_val > 2030:
                errors.append(f"年份值超出合理范围: {year}")
        except ValueError:
            errors.append(f"年份格式无效: {year}")
    
    return len(errors) == 0, errors


def sanitize_filename(filename: str) -> str:
    """
    清理文件名，移除非法字符
    
    Args:
        filename: 原始文件名
    
    Returns:
        清理后的文件名
    """
    illegal_chars = r'[<>:"/\\|?*]'
    return re.sub(illegal_chars, '_', filename)


def parse_json_safe(json_str: str, default=None) -> Any:
    """
    安全解析JSON字符串
    
    Args:
        json_str: JSON字符串
        default: 解析失败时的默认值
    
    Returns:
        解析后的对象或默认值
    """
    if not json_str:
        return default
    try:
        return json.loads(json_str)
    except (json.JSONDecodeError, TypeError):
        return default


def truncate_string(s: str, max_length: int = 100, suffix: str = '...') -> str:
    """
    截断过长的字符串
    
    Args:
        s: 原始字符串
        max_length: 最大长度
        suffix: 截断后缀
    
    Returns:
        截断后的字符串
    """
    if not s or len(s) <= max_length:
        return s
    return s[:max_length - len(suffix)] + suffix
