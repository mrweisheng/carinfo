#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
代理功能测试脚本
测试代理配置和连接是否正常
"""

import json
import requests
import logging
import os
import random
import time

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def load_proxy_config():
    """加载代理配置文件"""
    config_file = 'proxy_config.json'
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
        logger.info(f"成功加载代理配置文件: {config_file}")
        return config
    except FileNotFoundError:
        logger.error(f"代理配置文件不存在: {config_file}")
        return None
    except json.JSONDecodeError as e:
        logger.error(f"代理配置文件格式错误: {e}")
        return None

def test_proxy(proxy_config):
    """测试单个代理"""
    proxy_name = proxy_config.get('name', 'unknown')
    proxies = {
        'http': proxy_config.get('http'),
        'https': proxy_config.get('https')
    }
    
    logger.info(f"测试代理: {proxy_name}")
    logger.info(f"代理配置: {proxies}")
    
    try:
        # 测试IP检查服务
        response = requests.get(
            'https://httpbin.org/ip',
            proxies=proxies,
            timeout=30
        )
        
        if response.status_code == 200:
            ip_info = response.json()
            logger.info(f"代理 {proxy_name} 测试成功")
            logger.info(f"当前IP: {ip_info.get('origin', 'unknown')}")
            return True
        else:
            logger.error(f"代理 {proxy_name} 测试失败，状态码: {response.status_code}")
            return False
            
    except Exception as e:
        logger.error(f"代理 {proxy_name} 测试失败: {e}")
        return False

def test_target_website(proxy_config):
    """测试目标网站连接"""
    proxy_name = proxy_config.get('name', 'unknown')
    proxies = {
        'http': proxy_config.get('http'),
        'https': proxy_config.get('https')
    }
    
    logger.info(f"测试代理 {proxy_name} 访问目标网站")
    
    try:
        # 测试目标网站
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        }
        
        response = requests.get(
            'https://dj1jklak2e.28car.com/sell_lst.php?h_f_ty=1&h_page=1',
            headers=headers,
            proxies=proxies,
            timeout=30
        )
        
        if response.status_code == 200:
            logger.info(f"代理 {proxy_name} 成功访问目标网站")
            logger.info(f"响应长度: {len(response.content)} 字节")
            
            # 检查是否被重定向到busy页面
            if 'msg_busy.php' in response.text or 'busy' in response.text.lower():
                logger.warning(f"代理 {proxy_name} 被重定向到busy页面")
                return False
            else:
                logger.info(f"代理 {proxy_name} 正常访问目标网站")
                return True
        else:
            logger.error(f"代理 {proxy_name} 访问目标网站失败，状态码: {response.status_code}")
            return False
            
    except Exception as e:
        logger.error(f"代理 {proxy_name} 访问目标网站失败: {e}")
        return False

def test_direct_connection():
    """测试直连"""
    logger.info("测试直连")
    
    try:
        # 测试IP检查服务
        response = requests.get('https://httpbin.org/ip', timeout=30)
        
        if response.status_code == 200:
            ip_info = response.json()
            logger.info(f"直连测试成功")
            logger.info(f"当前IP: {ip_info.get('origin', 'unknown')}")
            return True
        else:
            logger.error(f"直连测试失败，状态码: {response.status_code}")
            return False
            
    except Exception as e:
        logger.error(f"直连测试失败: {e}")
        return False

def main():
    """主函数"""
    print("=== 代理功能测试 ===")
    
    # 加载配置
    config = load_proxy_config()
    if not config:
        return
    
    proxies = config.get('proxies', [])
    if not proxies:
        logger.warning("没有配置代理，测试直连")
        test_direct_connection()
        return
    
    logger.info(f"找到 {len(proxies)} 个代理配置")
    
    # 测试直连
    print("\n=== 测试直连 ===")
    test_direct_connection()
    
    # 测试每个代理
    working_proxies = []
    for i, proxy in enumerate(proxies, 1):
        if not proxy.get('enabled', True):
            logger.info(f"跳过已禁用的代理: {proxy.get('name', f'proxy_{i}')}")
            continue
            
        print(f"\n=== 测试代理 {i}/{len(proxies)} ===")
        
        # 基础连接测试
        if test_proxy(proxy):
            # 目标网站测试
            if test_target_website(proxy):
                working_proxies.append(proxy)
            else:
                logger.warning(f"代理 {proxy.get('name')} 无法正常访问目标网站")
        else:
            logger.warning(f"代理 {proxy.get('name')} 基础连接测试失败")
        
        # 添加延迟，避免请求过快
        time.sleep(2)
    
    # 总结
    print(f"\n=== 测试总结 ===")
    logger.info(f"总共测试了 {len(proxies)} 个代理")
    logger.info(f"可用代理数量: {len(working_proxies)}")
    
    if working_proxies:
        logger.info("可用代理列表:")
        for proxy in working_proxies:
            logger.info(f"  - {proxy.get('name', 'unknown')}")
    else:
        logger.warning("没有可用的代理，建议检查代理配置")

if __name__ == "__main__":
    main()