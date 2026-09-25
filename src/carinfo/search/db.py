"""数据库连接池：进程内 N 条连接，按请求借还。

**为什么不是「全局单连接」**（本项目上一版 `api.py` / `mcp_server.py` 的做法）：
两个服务各自持有一条模块级连接，所有请求共用。后果：
1. **共享同一个事务**。没有显式 commit/rollback 时，第一次 execute 就开启事务，然后一直
   挂着（`idle in transaction`），把所有请求裹进同一个快照；一旦某个语句报错，整条连接
   进入 aborted 状态，后续请求全部失败，只能靠"关掉重连"兜底。
2. **查询互相串**。同一连接上两个 cursor 交替 execute，没有隔离保证。
psycopg2 文档说的 thread-safety level 2（连接可跨线程传递）只说明**不会崩**，不等于可以
并发共用。

**为什么不用 `psycopg2.pool`（三条都是实测出来的）**：
1. `_putconn()` 只在 `len(self._pool) < self.minconn` 时才把连接放回池子，否则
   `conn.close()` —— 常见写法 `(minconn=1, maxconn=10)` 在并发下会**每次新建 + 关闭
   TCP**。实测 `ThreadedConnectionPool(1, 3)` 归还两条后池内只剩 1 条、另一条 `closed=1`。
   本库是远程 + SSL，实测**建连 331–360ms**，比查询本身还贵。
2. `_getconn()` 在 `len(_used) == maxconn` 时**直接抛 `PoolError("connection pool
   exhausted")`**，不排队。并发一超就 500。
3. **最致命**：`_putconn()` 对坏连接会先 `conn.rollback()`，而这一步在已断开的连接上会抛
   `OperationalError`；该异常没有兜底，后面的 `del self._used[key]` 被跳过 ——
   **池子的记账永久多留一条**，池会从 N 条一路退化到 1 条且再也回不来。
   实测：3 条池、把连接全掐断后走一遍 → 只剩 1 条。

所以这里自己写池：`deque` 存空闲连接 + `BoundedSemaphore` 限并发（`_Pool` 约 90 行）。
好处是 **每一条路径的记账都是显式的**，坏连接判定后直接 `close()` 丢弃，没有副作用。

**死连接无法预检**（实测）：服务端掐断连接后 `conn.closed` 仍是 **0**，要等下一次查询失败
才变成 2。所以策略是「空闲超过 `PING_AFTER_IDLE_SECONDS` 才 ping 一次」（实测一次
`SELECT 1` 往返 42ms，只在真正空闲之后付这个钱），ping 失败就丢弃换新的。

**不做启动预热**：连接是**按需创建**的，第一个请求付一次 350ms；突发并发时各请求并行建连，
比启动时串行建 10 条（3.5s）更快，也让进程能立刻起来。

本模块**只读**：`db()` 归还前一律 rollback，保证不把事务/快照带进池子。将来要加写接口，
必须在 `with` 块内显式 commit —— 否则会被这里的 rollback 静默丢掉。
"""

from __future__ import annotations

import collections
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg2

from carinfo.search.config import load_dotenv_once

#: 池大小 = 最大并发借用数。可用 DB_POOL_SIZE 覆盖。
#: 预算：服务端 max_connections=100，当前常年 <10 在用；检索 API + MCP 各 10 也才 20。
DEFAULT_POOL_SIZE = 10
ENV_POOL_SIZE = "DB_POOL_SIZE"
MAX_POOL_SIZE = 50

#: 借连接的最长等待。超了抛 PoolBusy（API 层翻译成 503），而不是 500。
ACQUIRE_TIMEOUT_SECONDS = 5.0

#: 连接空闲超过这么久，下次借出前先 ping 一次确认链路还在。
#: 实测一次往返 42ms，所以只在空闲之后付。
PING_AFTER_IDLE_SECONDS = 30.0

#: 客户端 TCP keepalive。服务端 `idle_session_timeout=0`（不会主动杀空闲连接），但链路
#: 中间可能有 NAT/防火墙；让操作系统自己发现对端消失，比等应用层超时快得多。
#: 实测 libpq 接受这组参数。
KEEPALIVE_KWARGS = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
}


class DbUnavailable(RuntimeError):
    """连不上库 / 连接参数缺失。API 层翻译成 503。"""


class PoolBusy(RuntimeError):
    """池子被占满且等待超时。API 层翻译成 503。"""


def pool_size() -> int:
    raw = (os.environ.get(ENV_POOL_SIZE) or "").strip()
    try:
        n = int(raw)
    except ValueError:
        n = DEFAULT_POOL_SIZE
    return max(1, min(n, MAX_POOL_SIZE))


def _conn_kwargs() -> dict[str, Any]:
    load_dotenv_once()
    try:
        return {
            "host": os.environ["DB_HOST"],
            "port": int(os.environ.get("DB_PORT", 5432)),
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
            "dbname": os.environ["DB_NAME"],
            "connect_timeout": 20,
            "application_name": "carinfo-search",   # pg_stat_activity 里能认出来是谁
            **KEEPALIVE_KWARGS,
        }
    except KeyError as e:
        raise DbUnavailable(f"缺少数据库环境变量 {e}（检查项目根目录的 .env）") from e


class _Pool:
    """空闲连接队列 + 并发闸门。

    不变量：`len(_idle) + 在借数量 <= size`。因为**只在 `_idle` 为空时才新建**，
    而新建时在借数量 = 已占的信号量数（含自己），所以新建后总数不超过 size。
    """

    def __init__(self, size: int) -> None:
        self.size = size
        self.slots = threading.BoundedSemaphore(size)
        self._idle: collections.deque = collections.deque()
        self._lock = threading.Lock()
        self._in_use = 0
        self._created = 0
        #: 池的出生时刻。某条连接没有单独的"空闲时刻"记录时用它当基准 ——
        #: 刚建的连接不需要 ping，闲置超时才需要。
        self.born_at = time.monotonic()
        self.closed = False

    def acquire(self, timeout: float):
        if not self.slots.acquire(timeout=timeout):
            raise PoolBusy(
                f"数据库连接池已满（{self.size} 条）且等待超过 {timeout:.0f} 秒。"
                f"调大 {ENV_POOL_SIZE} 或降低调用并发。"
            )
        with self._lock:
            self._in_use += 1
            if self._idle:
                return self._idle.pop()
        try:
            conn = psycopg2.connect(**_conn_kwargs())
        except Exception:
            self.release_slot_only()
            raise
        with self._lock:
            self._created += 1
        return conn

    def release_slot_only(self) -> None:
        with self._lock:
            self._in_use = max(0, self._in_use - 1)
        self.slots.release()

    def release(self, conn, broken: bool) -> None:
        keep = False
        if not broken and not self.closed:
            with self._lock:
                if len(self._idle) < self.size:
                    self._idle.append(conn)
                    keep = True
        if not keep:
            # 坏连接 / 池已关：直接丢，没有别的记账要维护 —— 这就是自己写池的好处
            try:
                conn.close()
            except Exception:
                pass
        self.release_slot_only()

    def discard_all(self) -> None:
        with self._lock:
            self.closed = True
            idle, self._idle = list(self._idle), collections.deque()
        for c in idle:
            try:
                c.close()
            except Exception:
                pass

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "size": self.size,
                "idle": len(self._idle),
                "in_use": self._in_use,
                "created_total": self._created,
            }


_pool: _Pool | None = None
_pool_lock = threading.Lock()

#: 归还时刻，key = id(conn)。只用于判断"这条连接闲太久了，借出前 ping 一下"。
_idle_since: dict[int, float] = {}
_meta_lock = threading.Lock()


def get_pool() -> _Pool:
    """懒建池。**不预先建连接**（见模块 docstring：突发并发时并行建连更快）。"""
    global _pool
    with _pool_lock:
        if _pool is None or _pool.closed:
            _pool = _Pool(pool_size())
        return _pool


def warm_pool() -> None:
    """启动钩子。池是懒建连接的，这里只是把池对象备好，顺带验证配置齐全。

    失败**不抛** —— 让服务先起来，DB 恢复后第一个真实请求会自己重试建池。
    """
    try:
        get_pool()
    except Exception:
        pass


def close_pool() -> None:
    """丢弃所有空闲连接（进程退出/测试收尾用）。在借的连接归还会被直接关掉。"""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.discard_all()
            _pool = None
    with _meta_lock:
        _idle_since.clear()


def _ping(conn) -> bool:
    """确认这条连接还能用。返回 False 表示该丢掉。"""
    try:
        if conn.closed:
            return False
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        cur.close()
        return True
    except Exception:
        return False


@contextmanager
def db(*, timeout: float = ACQUIRE_TIMEOUT_SECONDS) -> Iterator[Any]:
    """借一条连接，用完自动归还。

        with db() as conn:
            cur = conn.cursor(); ...

    借出时：池满 → 排队等 `timeout` 秒，超时抛 `PoolBusy`。
            连接空闲超过 `PING_AFTER_IDLE_SECONDS` → 先 ping，ping 挂了就换一条。
    归还时：rollback 结束事务；已经坏掉的连接直接丢弃。
    """
    pool = get_pool()
    conn = pool.acquire(timeout)
    try:
        with _meta_lock:
            # 没有单独记录就用池的出生时刻：刚建池 = 刚建连接，不用 ping
            last = _idle_since.get(id(conn), pool.born_at)
        if (time.monotonic() - last) > PING_AFTER_IDLE_SECONDS and not _ping(conn):
            # ⚠️ 顺序不能反：**先拿到新连接，再放旧的**。
            # 若先 release(broken=True) 再 acquire，而 acquire 抛 PoolBusy，
            # finally 会对同一个 conn 再 release 一次 —— 信号量多放一个，
            # _in_use 记账失真，并发上限在「DB 重启」场景下被永久侵蚀。
            # 现在：acquire 抛错时旧连接还没放，finally 恰好只放一次，记账守恒。
            new_conn = pool.acquire(timeout)
            pool.release(conn, broken=True)
            with _meta_lock:
                _idle_since.pop(id(conn), None)
            conn = new_conn
        yield conn
    finally:
        broken = True
        try:
            if not conn.closed:
                conn.rollback()      # 只读服务：归还前结束事务，别把快照带回去
                broken = False
        except Exception:
            broken = True            # rollback 都失败 → 连接废了，丢弃
        pool.release(conn, broken=broken)
        # 空闲计时跟着连接走：坏连接丢弃时把它的记录一并清掉，
        # 否则 id() 被回收复用会给新连接安上一个假的"空闲起点"。
        with _meta_lock:
            if broken:
                _idle_since.pop(id(conn), None)
            else:
                _idle_since[id(conn)] = time.monotonic()


def fetch(fn):
    """借连接执行 `fn(conn)`，遇到**连接级**错误就换一条重试一次。

        return fetch(lambda conn: _do_search(conn, spec))

    为什么需要它：死连接**预检不出来**（见模块 docstring）。最现实的场景是「数据库重启
    / 网络抖动 → 池里 N 条连接同时失效」，那一刻的头 N 个请求会各失败一次。没有重试的话
    用户看到的就是一串 500，而其实换条连接立刻就好。

    只对 `OperationalError` / `InterfaceError`（连接级）重试；SQL 语法/约束错误**不重试**
    —— 重试也不会变好，还会掩盖 bug。

    ⚠️ 只在**只读**操作上用。写操作重试可能把同一条记录写两次。
    """
    last: Exception | None = None
    for attempt in (1, 2):
        try:
            with db() as conn:
                return fn(conn)
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            last = e
            if attempt == 2:
                raise
    assert last is not None      # 理论不可达：两次都失败会在上面 raise
    raise last


def pool_stats() -> dict[str, Any]:
    """给 /health 用。池还没建时也返回得出来。"""
    with _meta_lock:
        pending = len(_idle_since)
    p = _pool      # 先取本地引用：close_pool() 可能把它置空
    built = p is not None and not p.closed
    stats: dict[str, Any] = {
        "size": p.size if built and p is not None else pool_size(),
        "built": built,
        "acquire_timeout_seconds": ACQUIRE_TIMEOUT_SECONDS,
        "ping_after_idle_seconds": PING_AFTER_IDLE_SECONDS,
        "idle_tracked": pending,
    }
    if built and p is not None:
        stats.update(p.stats())
    return stats


__all__ = [
    "DbUnavailable",
    "PoolBusy",
    "close_pool",
    "db",
    "fetch",
    "get_pool",
    "pool_size",
    "pool_stats",
    "warm_pool",
]
