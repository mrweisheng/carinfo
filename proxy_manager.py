#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据库代理管理器
从MySQL数据库动态加载和管理代理
"""

import random
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

# 北京时间时区
BEIJING_TZ = timezone(timedelta(hours=8))
from dotenv import load_dotenv
import mysql.connector

# 加载环境变量
load_dotenv()

logger = logging.getLogger(__name__)


class ProxyManager:
    """数据库代理管理器"""

    def __init__(self, pool_size=2000):
        """
        初始化代理管理器

        Args:
            pool_size: 内存中缓存的代理数量，默认2000
        """
        self.pool_size = pool_size
        self.proxy_pool = []  # 内存中的代理池
        self.failed_proxies = set()  # 本次会话失败的代理
        self.lock = threading.Lock()  # 线程锁
        self.last_load_time = None
        self.load_count = 0  # 加载次数统计

        # 数据库配置
        self.db_config = {
            'host': os.getenv('DB_HOST'),
            'port': int(os.getenv('DB_PORT', 3306)),
            'user': os.getenv('DB_USER'),
            'password': os.getenv('DB_PASSWORD'),
            'database': os.getenv('DB_NAME'),
            'charset': 'utf8mb4',
            'autocommit': False,
            'buffered': True,
            'connect_timeout': 30,
            'use_unicode': True,
            'sql_mode': ''
        }

        # 启动时立即加载代理
        self.load_proxies_from_db()

    def load_proxies_from_db(self):
        """从数据库随机加载代理到内存"""
        try:
            with self.lock:
                conn = mysql.connector.connect(**self.db_config)
                cursor = conn.cursor()

                # 优先使用失败次数少的健康代理，并随机排序
                sql = """
                    SELECT name, http_url, https_url, fail_count
                    FROM proxies
                    WHERE enabled = TRUE
                      AND is_healthy = TRUE
                      AND fail_count < 10
                    ORDER BY fail_count ASC, RAND()
                    LIMIT %s
                """

                cursor.execute(sql, (self.pool_size,))
                results = cursor.fetchall()

                self.proxy_pool = [
                    {
                        'name': row[0],
                        'http': row[1],
                        'https': row[2],
                        'fail_count': row[3]
                    }
                    for row in results
                ]

                self.failed_proxies.clear()
                self.last_load_time = datetime.now(BEIJING_TZ)
                self.load_count += 1

                cursor.close()
                conn.close()

                logger.info(f"✓ 从数据库加载了 {len(self.proxy_pool)} 个代理 (第{self.load_count}次加载)")

        except Exception as e:
            logger.error(f"✗ 加载代理失败: {e}")
            self.proxy_pool = []

    def get_random_proxy(self):
        """
        获取随机代理

        Returns:
            dict: {'http': url, 'https': url, 'name': name} 或 None
        """
        with self.lock:
            # 检查是否需要重新加载
            available_count = len(self.proxy_pool) - len(self.failed_proxies)

            # 当可用代理少于20%时重新加载
            if available_count < self.pool_size * 0.2:
                logger.info(f"可用代理不足({available_count}/{self.pool_size})，重新加载...")
                self.load_proxies_from_db()

            # 从未失败的代理中随机选择
            available = [
                p for p in self.proxy_pool
                if p['name'] not in self.failed_proxies
            ]

            if not available:
                logger.warning("所有缓存代理都失败，尝试重新加载...")
                self.load_proxies_from_db()

                # 重新尝试获取
                available = [
                    p for p in self.proxy_pool
                    if p['name'] not in self.failed_proxies
                ]

                if not available:
                    logger.error("代理池已耗尽！")
                    return None

            proxy = random.choice(available)
            return {
                'http': proxy['http'],
                'https': proxy['https'],
                'name': proxy['name']
            }

    def mark_proxy_failed(self, proxy_name):
        """
        标记代理失败（内存标记 + 异步更新数据库）

        Args:
            proxy_name: 代理名称
        """
        if not proxy_name:
            return

        with self.lock:
            self.failed_proxies.add(proxy_name)

        # 异步更新数据库（不阻塞主流程）
        def update_db():
            try:
                # 等待一小段时间，合并频繁的数据库更新
                time.sleep(0.5)

                conn = mysql.connector.connect(**self.db_config)
                cursor = conn.cursor()

                # 增加失败计数
                sql = """
                    UPDATE proxies
                    SET fail_count = fail_count + 1,
                        last_used = NOW()
                    WHERE name = %s
                """
                cursor.execute(sql, (proxy_name,))

                # 如果失败次数超过10次，标记为不健康
                sql = """
                    UPDATE proxies
                    SET is_healthy = FALSE
                    WHERE name = %s AND fail_count >= 10
                """
                cursor.execute(sql, (proxy_name,))

                conn.commit()
                cursor.close()
                conn.close()

            except Exception as e:
                logger.error(f"更新代理失败状态错误: {e}")

        thread = threading.Thread(target=update_db)
        thread.daemon = True
        thread.start()

    def mark_proxy_success(self, proxy_name):
        """
        标记代理成功（降低失败计数，让好代理更容易被选中）

        Args:
            proxy_name: 代理名称
        """
        if not proxy_name:
            return

        # 异步更新数据库
        def update_db():
            try:
                # 等待一小段时间，合并频繁的数据库更新
                time.sleep(0.5)

                conn = mysql.connector.connect(**self.db_config)
                cursor = conn.cursor()

                # 减少失败计数（最低为0）
                sql = """
                    UPDATE proxies
                    SET fail_count = GREATEST(0, fail_count - 1),
                        last_used = NOW(),
                        is_healthy = TRUE
                    WHERE name = %s
                """
                cursor.execute(sql, (proxy_name,))
                conn.commit()

                cursor.close()
                conn.close()

            except Exception as e:
                logger.error(f"更新代理成功状态错误: {e}")

        thread = threading.Thread(target=update_db)
        thread.daemon = True
        thread.start()

    def get_stats(self):
        """
        获取代理统计信息

        Returns:
            dict: 统计信息
        """
        try:
            conn = mysql.connector.connect(**self.db_config)
            cursor = conn.cursor()

            # 总代理数
            cursor.execute("SELECT COUNT(*) FROM proxies")
            total = cursor.fetchone()[0]

            # 启用的代理数
            cursor.execute("SELECT COUNT(*) FROM proxies WHERE enabled = TRUE")
            enabled = cursor.fetchone()[0]

            # 健康的代理数
            cursor.execute("SELECT COUNT(*) FROM proxies WHERE is_healthy = TRUE")
            healthy = cursor.fetchone()[0]

            # 内存池状态
            available_count = len(self.proxy_pool) - len(self.failed_proxies)

            stats = {
                'total_proxies': total,
                'enabled_proxies': enabled,
                'healthy_proxies': healthy,
                'pool_size': len(self.proxy_pool),
                'available_in_pool': available_count,
                'failed_in_session': len(self.failed_proxies),
                'load_count': self.load_count,
                'last_load_time': self.last_load_time
            }

            cursor.close()
            conn.close()

            return stats

        except Exception as e:
            logger.error(f"获取统计信息失败: {e}")
            return {}

    def refresh(self, new_pool_size=None):
        """
        强制重新加载代理池

        Args:
            new_pool_size: 新的代理池大小，如果为None则保持当前大小
        """
        if new_pool_size:
            self.pool_size = new_pool_size

        logger.info(f"强制刷新代理池（pool_size={self.pool_size}）...")
        self.load_proxies_from_db()

    def reset_all_fail_counts(self):
        """重置所有代理的失败计数（管理功能）"""
        try:
            conn = mysql.connector.connect(**self.db_config)
            cursor = conn.cursor()

            sql = """
                UPDATE proxies
                SET fail_count = 0,
                    is_healthy = TRUE
            """
            cursor.execute(sql)
            affected_rows = cursor.rowcount
            conn.commit()

            cursor.close()
            conn.close()

            logger.info(f"✓ 已重置 {affected_rows} 个代理的失败计数")
            return True

        except Exception as e:
            logger.error(f"重置失败计数错误: {e}")
            return False

    def close(self):
        """关闭连接池（可选）"""
        # 当前使用即时连接，无需特殊关闭
        pass


# 全局实例和锁
_proxy_manager_instance = None
_proxy_manager_lock = threading.Lock()


def get_proxy_manager(pool_size=2000):
    """
    获取代理管理器单例

    Args:
        pool_size: 代理池大小，默认2000

    Returns:
        ProxyManager: 代理管理器实例
    """
    global _proxy_manager_instance

    with _proxy_manager_lock:
        if _proxy_manager_instance is None:
            _proxy_manager_instance = ProxyManager(pool_size)
        else:
            # 如果池大小不同，重新初始化
            if _proxy_manager_instance.pool_size != pool_size:
                logger.info(f"代理池大小变更: {_proxy_manager_instance.pool_size} -> {pool_size}，重新初始化")
                _proxy_manager_instance = ProxyManager(pool_size)

    return _proxy_manager_instance


def reset_proxy_manager():
    """
    重置代理管理器单例（强制重新创建）
    """
    global _proxy_manager_instance
    with _proxy_manager_lock:
        if _proxy_manager_instance:
            _proxy_manager_instance.close()
        _proxy_manager_instance = None
    logger.info("代理管理器已重置")


if __name__ == '__main__':
    # 测试代码
    logging.basicConfig(level=logging.INFO)

    # 测试单例
    pm1 = get_proxy_manager(pool_size=2000)
    pm2 = get_proxy_manager(pool_size=2000)
    print(f"单例测试: {pm1 is pm2}")

    # 获取几个代理
    for i in range(5):
        proxy = pm1.get_random_proxy()
        if proxy:
            print(f"{i+1}. {proxy['name']}")

    # 显示统计信息
    stats = pm1.get_stats()
    print("\n代理统计:")
    for key, value in stats.items():
        print(f"  {key}: {value}")
