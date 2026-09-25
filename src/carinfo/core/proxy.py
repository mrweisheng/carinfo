#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据库代理管理器
从PostgreSQL数据库动态加载和管理代理
"""

import atexit
import logging
import os
import queue
import random
import threading
import time
from datetime import datetime, timedelta, timezone

import psycopg2
from dotenv import load_dotenv

# 北京时间时区
BEIJING_TZ = timezone(timedelta(hours=8))

# 加载环境变量
load_dotenv()

logger = logging.getLogger(__name__)


class _ProxyWriteWorker:
    """代理计数回写：单后台线程 + 队列 + 按代理名合并。

    为什么不再「每次标记起一个线程 + 各建一条 PG 连接」：
    `mark_proxy_failed/success` 是**每请求**都会调的高频操作（busy 风暴下每请求
    最多 1 + busy_max_retries 次）。原来的实现每个标记 = 新线程 + 新建连接 +
    两条 UPDATE，注释里写的"等待 0.5 秒合并"其实什么都没合并。
    在 5 并发、busy 预算 20 时，建连频率足以逼近 `max_connections=100`，
    打满后标记静默失败、代理拉黑直接失效。

    现在：内存里按 `proxy_name` 累加 delta，一个常驻线程每 `FLUSH_INTERVAL`
    秒或队列积压到阈值时一次性回写，**连接常驻复用**。
    合并在内存完成后，队列只负责唤醒，不承担数据传递 —— 这样即使队列丢消息
    也不会丢计数（下次 flush 会带上）。

    正确性说明：`fail_count` 是统计量不是余额，**丢失一次回写只会让某个代理的
    失败计数少一次**（下一次成功/失败会修正），不会造成拉黑逻辑错乱 ——
    与"每条标记各写一次"相比，弱化的只是计数精度，换来的是连接数恒定。
    """

    FLUSH_INTERVAL = 1.0     # 秒：最长多久回写一次
    FLUSH_MAX_PENDING = 200  # 累积多少个标记就立刻回写

    def __init__(self, db_config: dict):
        self._db_config = db_config
        self._pending: dict[str, int] = {}      # name -> delta（正=失败，负=成功）
        self._succ: dict[str, int] = {}         # name -> 成功次数（用于恢复健康）
        self._lock = threading.Lock()
        self._wake = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._conn = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="proxy-writer", daemon=True)
        self._thread.start()
        atexit.register(self.stop)

    def stop(self) -> None:
        self._stop.set()
        try:
            self._wake.put_nowait(True)
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        self._close_conn()

    # ---- 入队（内存累加，不传数据） ----
    def record(self, name: str, failed: bool) -> None:
        if not name:
            return
        with self._lock:
            if failed:
                self._pending[name] = self._pending.get(name, 0) + 1
            else:
                self._pending[name] = self._pending.get(name, 0) - 1
                self._succ[name] = self._succ.get(name, 0) + 1
            pending_total = sum(abs(v) for v in self._pending.values())
        if pending_total >= self.FLUSH_MAX_PENDING:
            self._signal()

    def _signal(self) -> None:
        try:
            self._wake.put_nowait(True)
        except queue.Full:
            pass   # 已有待处理信号，本来就该 flush

    # ---- 后台循环 ----
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._wake.get(timeout=self.FLUSH_INTERVAL)
            except queue.Empty:
                pass
            if self._stop.is_set():
                break
            self._flush()

    def _flush(self) -> None:
        with self._lock:
            if not self._pending:
                return
            pending, succ = self._pending, self._succ
            self._pending, self._succ = {}, {}
        try:
            conn = self._ensure_conn()
            if conn is None:
                self._requeue(pending, succ)
                return
            cur = conn.cursor()
            # fail_count 可能被扣成负数，GREATEST 夹回 0；
            # is_healthy 只在真正降到阈值下才恢复（与 mark_proxy_success 旧口径一致）
            for name, delta in pending.items():
                cur.execute(
                    "UPDATE proxies SET fail_count = GREATEST(0, fail_count + %s),"
                    " last_used = NOW() WHERE name = %s",
                    (delta, name),
                )
                if succ.get(name):
                    cur.execute(
                        "UPDATE proxies SET is_healthy = TRUE"
                        " WHERE name = %s AND fail_count < 10",
                        (name,),
                    )
            conn.commit()
            cur.close()
        except Exception as e:  # noqa: BLE001
            logger.error(f"代理计数回写失败: {type(e).__name__}: {e}")
            self._close_conn()
            self._requeue(pending, succ)

    def _requeue(self, pending: dict, succ: dict) -> None:
        """回写失败就把计数放回去，等下一轮重试（不丢账）。"""
        with self._lock:
            for k, v in pending.items():
                self._pending[k] = self._pending.get(k, 0) + v
            for k, v in succ.items():
                self._succ[k] = self._succ.get(k, 0) + v

    def _ensure_conn(self):
        if self._conn is not None and self._conn.closed == 0:
            return self._conn
        try:
            self._conn = psycopg2.connect(**self._db_config)
        except Exception as e:  # noqa: BLE001
            logger.error(f"代理回写线程建连失败: {type(e).__name__}: {e}")
            self._conn = None
        return self._conn

    def _close_conn(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None


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
        self._last_load_fail_ts = 0.0  # 上次加载失败时刻（链路抖动时限流重试）
        # 必须用可重入锁：get_random_proxy 持锁期间会调用 load_proxies_from_db，
        # 普通 Lock 同线程重复加锁会永久死锁（可用代理低于池 20% 时必触发）
        self.lock = threading.RLock()
        self.last_load_time = None
        self.load_count = 0  # 加载次数统计

        # 数据库配置
        self.db_config = {
            "host": os.getenv("DB_HOST"),
            "port": int(os.getenv("DB_PORT", 5432)),
            "user": os.getenv("DB_USER"),
            "password": os.getenv("DB_PASSWORD"),
            "dbname": os.getenv("DB_NAME"),
            "connect_timeout": 30,
            # 本机到远程 VPS 的链路偶发静默卡死（连接正常但查询结果永不到达）：
            # keepalive 约 1 分钟识别死链；statement_timeout 让挂起查询 10 秒后
            # 报错而非永久等待——报错被 load_proxies_from_db 捕获后走空池降级，
            # 后续每个请求会自动重试加载，等于天然重试。
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 3,
            "options": "-c statement_timeout=10000",
        }

        # 计数回写 worker：单线程 + 常驻连接（替代"每标记起一个线程+建一条连接"）
        self._writer = _ProxyWriteWorker(self.db_config)
        self._writer.start()

        # 启动加载：链路抖动（statement_timeout 10s 封顶）时最多重试一次，
        # 仍失败则先以空池启动——后续每个请求会自动重试加载，不会卡死启动
        for attempt in (1, 2):
            self.load_proxies_from_db()
            if self.proxy_pool:
                break
            if attempt == 1:
                logger.warning("首次加载代理为空，3 秒后重试一次...")
                time.sleep(3)

    def load_proxies_from_db(self):
        """从数据库随机加载代理到内存"""
        try:
            with self.lock:
                conn = psycopg2.connect(**self.db_config)
                cursor = conn.cursor()

                # 优先使用失败次数少的健康代理，并随机排序
                sql = """
                    SELECT name, http_url, https_url, fail_count
                    FROM proxies
                    WHERE enabled = TRUE
                      AND is_healthy = TRUE
                      AND fail_count < 10
                    ORDER BY fail_count ASC, random()
                    LIMIT %s
                """

                cursor.execute(sql, (self.pool_size,))
                results = cursor.fetchall()

                self.proxy_pool = [
                    {
                        "name": row[0],
                        "http": row[1],
                        "https": row[2],
                        "fail_count": row[3],
                    }
                    for row in results
                ]

                self.failed_proxies.clear()
                self.last_load_time = datetime.now(BEIJING_TZ)
                self.load_count += 1

                # ⚠️ **加载成功但 0 行**也要限流。原先只在 except 分支记失败时刻，
                # 于是"查询成功但返回空"（所有代理都被禁用/拉黑）这种状态不限流，
                # 此后**每个请求**都满足重载条件 → 每个请求打一次库。
                # 空池和加载失败在"该不该限流"上是同一类事：都是没拿到可用代理。
                if not self.proxy_pool:
                    self._last_load_fail_ts = time.monotonic()

                cursor.close()
                conn.close()

                logger.info(
                    f"✓ 从数据库加载了 {len(self.proxy_pool)} 个代理 (第{self.load_count}次加载)"
                )

        except Exception as e:
            # 报错类型是关键诊断证据：QueryCanceled=服务器侧取消（库忙/超时），
            # OperationalError=链路死亡/中断。带类型记入日志，便于定位链路问题
            logger.error(f"✗ 加载代理失败: {type(e).__name__}: {e}")
            self.proxy_pool = []
            self._last_load_fail_ts = time.monotonic()

    def get_random_proxy(self):
        """
        获取随机代理

        Returns:
            dict: {'http': url, 'https': url, 'name': name} 或 None
        """
        with self.lock:
            # 链路抖动时限流：加载失败后 60 秒内不反复连库重试，
            # 避免"每个请求都付出一次超时代价"（宁可先用空池/直连过渡）
            can_reload = time.monotonic() - self._last_load_fail_ts > 60

            # 检查是否需要重新加载
            available_count = len(self.proxy_pool) - len(self.failed_proxies)

            # 当可用代理少于20%时重新加载
            if available_count < self.pool_size * 0.2 and can_reload:
                logger.info(
                    f"可用代理不足({available_count}/{self.pool_size})，重新加载..."
                )
                self.load_proxies_from_db()

            # 从未失败的代理中随机选择
            available = [
                p for p in self.proxy_pool if p["name"] not in self.failed_proxies
            ]

            if not available:
                if can_reload:
                    logger.warning("所有缓存代理都失败，尝试重新加载...")
                    self.load_proxies_from_db()

                    # 重新尝试获取
                    available = [
                        p for p in self.proxy_pool if p["name"] not in self.failed_proxies
                    ]

                if not available:
                    logger.error("代理池已耗尽！本次请求将直连")
                    return None

            proxy = random.choice(available)
            return {
                "http": proxy["http"],
                "https": proxy["https"],
                "name": proxy["name"],
            }

    def mark_proxy_failed(self, proxy_name):
        """标记代理失败：内存拉黑 + 交给后台 worker 合并回写数据库。

        回写是**异步且合并**的（每 FLUSH_INTERVAL 秒或积压 200 条时一次），
        调用方不会被数据库往返阻塞，连接数也恒定 —— 详见 _ProxyWriteWorker。
        """
        if not proxy_name:
            return
        with self.lock:
            self.failed_proxies.add(proxy_name)
        self._writer.record(proxy_name, failed=True)

    def mark_proxy_success(self, proxy_name):
        """标记代理成功：降低失败计数，让好代理更容易被选中。

        只有降到阈值以下才恢复 `is_healthy`。原先无条件置 TRUE 会造出
        「is_healthy=TRUE 但 fail_count>=10」的代理：状态显示健康，却被
        load_proxies_from_db 的查询条件排除。恢复逻辑在 worker 里，
        与 mark_proxy_failed 的阈值判定对称。
        """
        if not proxy_name:
            return
        self._writer.record(proxy_name, failed=False)

    def get_stats(self):
        """
        获取代理统计信息

        Returns:
            dict: 统计信息
        """
        try:
            conn = psycopg2.connect(**self.db_config)
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
                "total_proxies": total,
                "enabled_proxies": enabled,
                "healthy_proxies": healthy,
                "pool_size": len(self.proxy_pool),
                "available_in_pool": available_count,
                "failed_in_session": len(self.failed_proxies),
                "load_count": self.load_count,
                "last_load_time": self.last_load_time,
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
            conn = psycopg2.connect(**self.db_config)
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
        """停止后台回写线程并释放常驻连接。"""
        self._writer.stop()


def check_db_connection():
    """
    启动时检查数据库连接是否正常（轻量级，不初始化代理池）

    Returns:
        tuple: (success: bool, message: str)
    """
    try:
        host = os.getenv("DB_HOST")
        user = os.getenv("DB_USER")
        password = os.getenv("DB_PASSWORD")
        database = os.getenv("DB_NAME")

        if not all([host, user, password, database]):
            missing = [
                k
                for k, v in {
                    "DB_HOST": host,
                    "DB_USER": user,
                    "DB_PASSWORD": password,
                    "DB_NAME": database,
                }.items()
                if not v
            ]
            return False, f"数据库配置缺失: {', '.join(missing)}，请检查 .env 文件"

        db_config = {
            "host": host,
            "port": int(os.getenv("DB_PORT", 5432)),
            "user": user,
            "password": password,
            "dbname": database,
            "connect_timeout": 5,
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 3,
            "options": "-c statement_timeout=10000",
        }

        conn = psycopg2.connect(**db_config)
        cursor = conn.cursor()

        cursor.execute(
            "SELECT COUNT(*) FROM proxies WHERE enabled = TRUE AND is_healthy = TRUE AND fail_count < 10"
        )
        healthy_count = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM proxies")
        total_count = cursor.fetchone()[0]

        cursor.close()
        conn.close()

        if healthy_count > 0:
            return (
                True,
                f"数据库连接正常 (总计 {total_count} 个代理，可用 {healthy_count} 个)",
            )
        else:
            return (
                False,
                f"数据库连接正常，但无可用代理 (总计 {total_count} 个，无健康代理)",
            )

    except psycopg2.Error as e:
        return False, f"数据库连接失败: {e}"
    except Exception as e:
        return False, f"数据库检查异常: {e}"


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
                logger.info(
                    f"代理池大小变更: {_proxy_manager_instance.pool_size} -> {pool_size}，重新初始化"
                )
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


if __name__ == "__main__":
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
            print(f"{i + 1}. {proxy['name']}")

    # 显示统计信息
    stats = pm1.get_stats()
    print("\n代理统计:")
    for key, value in stats.items():
        print(f"  {key}: {value}")
