#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""车源复核（revalidation）：按 h_vid 逐台复查「久未核实」的在售车，判三态并落库。

背景
====
深扫（1200 页）覆盖不到的车 —— 2026-09-26 实测占在售 42%（10,036/23,966）——
长期停在 ``vehicle_status = 1``，实际早已已售/下架。搜索把它们当在售返回，
后果是**客户打电话白跑**。本模块直连详情页复查，把死车从检索里摘出去。

三态判据（2026-09-27 线上抓样实证，不是推测）
============================================
站点对删帖 **不用 404**，而是 HTTP **200** + ``window.location='msg_noid.php'``
（正文 ~1433 字节、无 ``frm_l``/``frm_t``、无标题）。因此：

- **已删**：HTTP 200 且正文含 ``msg_noid.php``，或 HTTP 404/410。
- **已售**：正文含 ``由於已售，資料亦被保護中``（详情页「聯絡人資料」格）。
- **仍在售**：正文含详情表格（``frm_l`` + ``frm_t``）。
- **busy**：HTTP 200 的拒绝页（``msg_busy.php``）→ 换 IP 重试，不判三态。
- **其余**（无表格、网络失败、其它状态码）→ **跳过，绝不删**。宁可漏判，不可误删。

⚠️ **绝不能用子串「已售」判定已售** —— 正常在售页也含一次「已售」（无关模板区），
用它判会把活车误杀。`test_revalidator.py` 里有专门的反例断言锁死这条。

写库口径
========
- 仍在售 → ``extra_fields.last_verified = 今天``（取下次复核的排序依据）
- 已售    → ``vehicle_status = 2``
- 已删    → ``vehicle_status = 3``（新增状态；engine/features 只认 1，天然不影响检索）
  另留 ``extra_fields.removed_at`` / ``removed_reason='noid'`` 供追溯。

所有写库都是 DB 侧 **JSONB 合并** ``extra_fields = coalesce(extra_fields,'{}'::jsonb) || %s``
—— 只增不改，绝不会丢掉 ``list_date`` / ``llm_extracted_at`` 等既有键。
（走全字段 UPDATE 会把 LLM 提取的字段整片冲掉，这是不能复用 importer 的原因。）

依赖约定
========
本模块**顶层只 import stdlib**，重依赖（psycopg2 / curl_cffi / proxy）全部在函数内
懒加载。这样 ``features.py`` 可以安全地 ``from carinfo.core.revalidator import
VERIFY_WINDOW_DAYS``，而不会拉起代理池和爬虫。
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量：改判据前先读模块 docstring
# ---------------------------------------------------------------------------

#: 「多久没被核实」算久未核实（天）。这是标签与复核候选的**唯一阈值**。
VERIFY_WINDOW_DAYS = 30

#: 删帖标记：站点对已删除的帖子返回 HTTP 200 + JS 跳转，不是 404
MSG_NOID_MARKER = "msg_noid.php"
#: 反爬拒绝页（HTTP 200）—— 必须最先判，否则会被误判成「无表格 → UNKNOWN」
MSG_BUSY_MARKER = "msg_busy.php"

#: 已售标记（详情页「聯絡人資料」格）。繁简成对写全，漏一个就漏判。
PROTECTED_MARKERS = (
    "由於已售，資料亦被保護中",
    "由于已售，联络人资料亦被保护中",
    "資料亦被保護中",
    "资料亦被保护中",
)

#: 详情表格的选择器类名（extract_car_info 靠这两个类定位车辆信息表）
TABLE_CLASS_MARKERS = ("frm_l", "frm_t")

#: 浏览器伪装参数（与爬虫主路径保持一致）
IMPERSONATE = "chrome"

#: 新增的「已下架/已删」状态值。现有代码只 switch 1/2，3 是惰性扩展：
#: engine / features 均硬过滤 ``vehicle_status = 1``，所以 3 自动从检索里消失。
STATUS_ON_SALE = 1
STATUS_SOLD = 2
STATUS_REMOVED = 3

_HVID_RE = re.compile(r"h_vid=(\d+)")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def today_beijing() -> str:
    """北京时间当天（YYYY-MM-DD）。不依赖服务器 OS 时区。

    单点定义：`sites/car28.py` 的 `build_rows()` 也 import 这个函数，
    不允许再在别处各写一份 —— 「今天」这个值一旦有两个来源，
    `last_verified` 的写入日与标签的判定日就会错开一天。
    """
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")


def extract_h_vid(car_url: str | None) -> Optional[str]:
    """从详情页 URL 抠出 h_vid（复核的唯一寻址方式）。"""
    if not car_url:
        return None
    m = _HVID_RE.search(car_url)
    return m.group(1) if m else None


class DetailState(str, Enum):
    """详情页复核结论。"""

    ALIVE = "alive"        # 仍在售
    SOLD = "sold"          # 已售（页面还在，但聯絡人資料被保護）
    DELETED = "deleted"    # 已删（msg_noid / 404）
    BUSY = "busy"          # 被反爬拒绝，换 IP 重试
    UNKNOWN = "unknown"    # 判不出来 —— 一律不动


def classify_detail(status_code: Optional[int], body: Optional[str]) -> DetailState:
    """把一次详情页响应判成三态之一（纯函数，可离线测）。

    判定顺序**不可调换**：
    1. 没拿到响应 → UNKNOWN（网络问题，不是删帖）
    2. 404/410 → DELETED
    3. 非 200 → UNKNOWN（不拿 5xx 当删帖）
    4. busy → BUSY（busy 页无表格，先判否则落到 UNKNOWN）
    5. noid → DELETED
    6. 保護中 → SOLD（已售页**有**详情表格，必须先于第 7 步判）
    7. 有详情表格 → ALIVE
    8. 兜底 → UNKNOWN
    """
    if status_code is None or body is None:
        return DetailState.UNKNOWN
    if status_code in (404, 410):
        return DetailState.DELETED
    if status_code != 200:
        return DetailState.UNKNOWN
    if MSG_BUSY_MARKER in body:
        return DetailState.BUSY
    if MSG_NOID_MARKER in body:
        return DetailState.DELETED
    if any(marker in body for marker in PROTECTED_MARKERS):
        return DetailState.SOLD
    if all(marker in body for marker in TABLE_CLASS_MARKERS):
        return DetailState.ALIVE
    return DetailState.UNKNOWN


# ---------------------------------------------------------------------------
# 执行器
# ---------------------------------------------------------------------------

#: fetch(h_vid) -> (status_code | None, body | None)
Fetcher = Callable[[str], "tuple[Optional[int], Optional[str]]"]
#: writer(vehicle_id, state, today) -> None，只在非 dry-run 时调用
Writer = Callable[[str, DetailState, str], None]


@dataclass
class RevalidationReport:
    """一轮复核的统计（给日志/状态文件用）。"""

    considered: int = 0
    # ── 判定结果（「页面长什么样」）──
    alive: int = 0
    sold: int = 0
    deleted: int = 0
    unknown: int = 0          # UNKNOWN：判不出来，原样不动
    busy_gave_up: int = 0     # busy 重试预算耗尽，本台放弃（不改库）
    # ── 落库结果（「写进去没有」）—— 必须与判定结果分开记 ──
    #: 曾经把这两类混成一个 `skipped`，后果是「整轮写库全失败」看起来只是
    #: 「跳过多了一点」：alive/sold/deleted 在**写库之前**就加过了，报告照样全绿。
    #: 拆开后 wrote=0 而 alive/sold/deleted 很大 = 一眼可见的异常。
    wrote: int = 0
    write_failed: int = 0
    busy_retries: int = 0
    aborted: bool = False     # 连续 busy 过多，主动中止（保护性）
    elapsed_s: float = 0.0

    @property
    def skipped(self) -> int:
        """判定不出来的台数（UNKNOWN + busy 放弃）。写库失败不计入这里。"""
        return self.unknown + self.busy_gave_up

    def as_dict(self) -> dict:
        return {
            "considered": self.considered, "alive": self.alive,
            "sold": self.sold, "deleted": self.deleted,
            "unknown": self.unknown, "busy_gave_up": self.busy_gave_up,
            "wrote": self.wrote, "write_failed": self.write_failed,
            "busy_retries": self.busy_retries, "aborted": self.aborted,
            "elapsed_s": round(self.elapsed_s, 1),
        }

    def summary(self) -> str:
        s = (f"复核 {self.considered} 台：在售 {self.alive} / 已售 {self.sold} / "
             f"已删 {self.deleted} / 判不出 {self.skipped}"
             f"（其中 busy 放弃 {self.busy_gave_up}）"
             f"│ 落库成功 {self.wrote} / 失败 {self.write_failed} "
             f"（busy 重试 {self.busy_retries} 次，耗时 {self.elapsed_s / 60:.1f} 分钟）")
        if self.write_failed:
            s += f" ⚠ {self.write_failed} 条写库失败，这部分判定未生效"
        if self.aborted:
            s += " ⚠ 已主动中止"
        return s


def revalidate_ids(
    ids: Iterable[str],
    fetch: Fetcher,
    *,
    writer: Optional[Writer] = None,
    concurrency: int = 4,
    busy_pause: float = 60.0,
    busy_retry_per_id: int = 2,
    max_busy_streak: int = 8,
    on_progress: Optional[Callable[[int, int], None]] = None,
    dry_run: bool = True,
) -> RevalidationReport:
    """逐台复核。``fetch`` 与 ``writer`` 由调用方注入 —— 因此本函数可完全离线测。

    并发只为掩盖网络延迟：真正的发送速率由爬虫的全局限速器（rate_limiter）兜底，
    注入的 fetch 内部已经排过队，这里再加线程不会突破站点的节流上限。

    busy 处理：整轮共享一个「连续 busy」计数，连续超过 ``max_busy_streak`` 就中止
    —— 与爬虫主路径的反爬退避同源。**中止不是失败**：已处理的写入已提交，
    未处理的留待下一轮（候选查询按 last_verified 排序，天然续跑）。
    """
    id_list = list(ids)
    report = RevalidationReport(considered=len(id_list))
    if not id_list:
        return report

    today = today_beijing()
    started = time.time()
    lock = threading.Lock()
    state = {"busy_streak": 0, "abort": False, "done": 0}
    writer_fn = writer          # dry-run 时不调用，见下方 dry_run 分支

    def handle(h_vid: str) -> None:
        if state["abort"]:
            return
        attempt = 0
        while True:
            if state["abort"]:
                return
            status_code, body = fetch(h_vid)
            state_ = classify_detail(status_code, body)

            if state_ == DetailState.BUSY:
                with lock:
                    state["busy_streak"] += 1
                    report.busy_retries += 1
                    if state["busy_streak"] >= max_busy_streak:
                        state["abort"] = True
                        logger.error(
                            "连续 %d 次被反爬拒绝，中止本轮复核（已处理部分已提交，"
                            "下一轮按 last_verified 从前排继续）", state["busy_streak"],
                        )
                        return
                if attempt < busy_retry_per_id:
                    attempt += 1
                    time.sleep(busy_pause)
                    continue
                with lock:
                    report.busy_gave_up += 1
                return

            with lock:
                state["busy_streak"] = 0          # 拿到非 busy 响应即清零失败链
                if state_ == DetailState.ALIVE:
                    report.alive += 1
                elif state_ == DetailState.SOLD:
                    report.sold += 1
                elif state_ == DetailState.DELETED:
                    report.deleted += 1
                else:
                    report.unknown += 1
                state["done"] += 1

            if state_ in (DetailState.ALIVE, DetailState.SOLD, DetailState.DELETED):
                if dry_run:
                    pass                          # 预演：不落库，也不计落库数
                elif writer_fn is not None:
                    try:
                        writer_fn(h_vid, state_, today)
                        with lock:
                            report.wrote += 1
                    except Exception as e:         # 单条失败不拖垮整轮（连接由 writer 自己 rollback）
                        logger.error("写库失败 h_vid=%s state=%s: %s", h_vid, state_, e)
                        with lock:
                            report.write_failed += 1
            if on_progress is not None:
                try:
                    on_progress(state["done"], report.considered)
                except Exception:
                    pass
            return

    workers = max(1, int(concurrency))
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(handle, id_list))

    report.aborted = state["abort"]
    report.elapsed_s = time.time() - started
    return report


# ---------------------------------------------------------------------------
# 落库（JSONB 合并，只增不改）
# ---------------------------------------------------------------------------

#: 只在售车用（不动 vehicle_status）。
_UPDATE_ALIVE = """
UPDATE vehicles
SET extra_fields = coalesce(extra_fields, '{}'::jsonb) || %s::jsonb,
    updated_at = CURRENT_TIMESTAMP
WHERE vehicle_id = %s
"""

#: 降级用（已售→2 / 已删→3 共用，状态值是参数）。
#: 曾写成 _UPDATE_SOLD / _UPDATE_REMOVED 两份**字节完全相同**的 SQL —— 合并成一份。
#: `AND vehicle_status = 1` 是并发护栏：爬虫可能刚把同一台车写成别的状态，
#: 复核是"降级"动作，不该去覆盖一个更新的判定（只降级、不复活）。
_UPDATE_STATUS = """
UPDATE vehicles
SET vehicle_status = %s,
    extra_fields = coalesce(extra_fields, '{}'::jsonb) || %s::jsonb,
    updated_at = CURRENT_TIMESTAMP
WHERE vehicle_id = %s AND vehicle_status = 1
"""


def make_db_writer(conn, vehicle_id_of: Callable[[str], Optional[str]]):
    """构造写库回调：``h_vid -> vehicle_id`` 由调用方给定（复核按 h_vid 寻址）。

    两条硬约束（P0：不修会静默白跑整轮）：
    1. **必须串行化**。多个 worker 线程共用同一条 psycopg2 连接，而 psycopg2/libpq
       并不承诺一条连接可被多线程并发使用 —— 用一把锁把写库串起来，把这类问题整个消掉。
    2. **失败必须 rollback**。PG 连接一旦进 aborted 事务态，后续所有 execute 都会抛
       `InFailedSqlTransaction`；没有 rollback 的话，**之后整轮写库全部失败**，
       而调用方只看到"跳过多了一点"，报告照样显示大量「已售/已删」。所以这里
       rollback 后**重新抛出**，让上层计入 `write_failed`（与判定计数分开，一眼可见）。

    每条独立提交（复核是长跑，中途可能被中断/kill —— 已判定的结果必须落地）。
    """
    import json

    write_lock = threading.Lock()

    def writer(h_vid: str, state: DetailState, today: str) -> None:
        vid = vehicle_id_of(h_vid)
        if not vid:
            logger.warning("找不到 h_vid=%s 对应的 vehicle_id，跳过写库", h_vid)
            return
        if state == DetailState.ALIVE:
            sql, params = _UPDATE_ALIVE, (json.dumps({"last_verified": today}), vid)
        elif state == DetailState.SOLD:
            # ⚠️ 不回写 last_verified：它的口径是「最后一次被证实**仍在售**」，
            # 已售车写进去会让这台车日后被重新上架时带着一个假的"刚核实过"。
            sql, params = _UPDATE_STATUS, (
                STATUS_SOLD, json.dumps({"sold_at": today}), vid)
        elif state == DetailState.DELETED:
            sql, params = _UPDATE_STATUS, (
                STATUS_REMOVED,
                json.dumps({"removed_at": today, "removed_reason": "noid"}), vid)
        else:
            return

        with write_lock:
            cur = conn.cursor()
            try:
                cur.execute(sql, params)
                conn.commit()
            except Exception:
                # 关键：把连接从 aborted 事务态里救出来，否则后面全废
                try:
                    conn.rollback()
                except Exception as rb:
                    logger.error("rollback 也失败，本连接已不可用: %s", rb)
                raise
            finally:
                try:
                    cur.close()
                except Exception:
                    pass

    return writer


# ---------------------------------------------------------------------------
# 候选查询
# ---------------------------------------------------------------------------

CANDIDATE_SQL = """
WITH cand AS (
    SELECT v.vehicle_id, v.car_url, v.extra_fields->>'last_verified' AS lv,
           coalesce(f.is_dealer, false) AS is_dealer
    FROM vehicles v
    LEFT JOIN vehicle_features f ON f.vehicle_id = v.vehicle_id
    WHERE v.vehicle_status = 1
      AND (%(vtype)s IS NULL OR v.vehicle_type = %(vtype)s)
),
norm AS (
    SELECT vehicle_id, car_url, is_dealer,
           CASE WHEN lv ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' THEN lv::date END AS lvd
    FROM cand
)
SELECT vehicle_id, car_url
FROM norm
WHERE lvd IS NULL OR lvd < (now() AT TIME ZONE 'Asia/Hong_Kong')::date - %(window)s::int
ORDER BY is_dealer ASC, lvd NULLS FIRST, vehicle_id
LIMIT %(limit)s
"""


def fetch_candidates(conn, *, window_days: int = VERIFY_WINDOW_DAYS,
                     vehicle_type: Optional[int] = None,
                     limit: int = 3000) -> list[tuple[str, str]]:
    """取一批待复核车（vehicle_id, h_vid）。

    排序：**个人车优先（is_dealer ASC）** → 最久没核实的在前 → vehicle_id 兜底。
    - 个人车优先：搜车的价值几乎全在个人车上（车行死库存清了也就算收益），
      而实测 stale 池 10,036 台里个人车只有 2,303 台 —— 排前之后
      **首轮 3,000 台就能把个人车全部覆盖**；不排的话按 vehicle_id 排，
      个人车要等 2-3 轮（每轮 7 天）才轮到，等于把最该救的放在最后。
    - 用 **LEFT JOIN**（不是 INNER）：没有特征行的车也要留在候选里，
      INNER 会把它们从候选池静默剔除（复核永远轮不到）。
    - 幂等：每轮天然从「最久没核实」的开始，**不需要进度文件** ——
      已处理的会被写成今天，下一轮自动落在队尾。
    三层 CTE 是为了让 ``::date`` 只作用在正则已通过的值上（避免脏值抛异常）。
    """
    cur = conn.cursor()
    cur.execute(CANDIDATE_SQL, {"vtype": vehicle_type, "window": window_days,
                                "limit": int(limit)})
    out: list[tuple[str, str]] = []
    for vehicle_id, car_url in cur.fetchall():
        h_vid = extract_h_vid(car_url)
        if h_vid:
            out.append((vehicle_id, h_vid))
    cur.close()
    return out


# ---------------------------------------------------------------------------
# 漂移探针（golden probe）—— **只告警，绝不自动清库**
# ---------------------------------------------------------------------------

#: 每类抽多少台做一致性校验（在售 / 已售各一份）
PROBE_SAMPLE = 3

_PROBE_ALIVE_SQL = """
SELECT vehicle_id, car_url FROM vehicles
WHERE vehicle_status = 1 AND extra_fields->>'last_verified' IS NOT NULL
ORDER BY random() LIMIT %s
"""

_PROBE_SOLD_SQL = """
SELECT vehicle_id, car_url FROM vehicles
WHERE vehicle_status = 2 AND page_number > 0
  AND (contact_info LIKE %s OR contact_info LIKE %s)
ORDER BY random() LIMIT %s
"""


def _drift_reason(expected: DetailState, got: DetailState) -> Optional[str]:
    """判断「期望与实得不一致」究竟是不是**判据漂移**（而不是车本身状态变了）。

    为什么必须区分：探针拿库里的已知状态当预期答案，而**那个状态是几天前抓的**
    —— 车很可能早就卖了或被删了。若把「预期在售、实得已删」也算成漂移，
    探针会天天误报，几次之后就被无视，等于没做。

    漂移的真正定义 = **页面还在（HTTP 200 且正文非空），但我们的标记一个都对不上**：

    - 期望在售 → 实得 UNKNOWN：详情表格的类名/结构变了（`frm_l`/`frm_t` 失配）
    - 期望已售 → 实得 UNKNOWN：保护文案变了
    - 期望已售 → 实得 ALIVE ：保护文案失效。**最危险的一种** ——
      已售车会被当成活车刷新 `last_verified`，标签从此骗人

    不算漂移（判据其实工作得很好）：
    - 期望在售 → 实得 已售/已删；期望已售 → 实得 已删：车本身状态变了
    - 任一侧是 BUSY：反爬拒绝页，与判据无关
    """
    if got == DetailState.BUSY:
        return None
    if expected == DetailState.SOLD and got == DetailState.ALIVE:
        return "保护文案失效（已售页被当成在售页）"
    if got == DetailState.UNKNOWN:
        return "页面取到但判据一个都不匹配 —— 站点结构可能已改版"
    return None


def probe_drift(conn, fetch: Fetcher, *, sample: int = PROBE_SAMPLE) -> dict:
    """抽查线上真实页面，验证三态判据是否仍成立（**只读，不改任何数据**）。

    为什么需要它：三态判据是从 2026-09-27 的抓样反推的。站点一改版
    （换掉保护文案 / 换掉表格 class / 改掉 msg_noid），`classify_detail` 会**悄悄
    退化成「全判 UNKNOWN」**——复核从此一台都不动，而报告看起来只是"判不出来"。
    离线单测只能证明**代码没被改坏**，证明不了**线上页面没变**，这是它唯一的补位。

    做法：拿库里的**已知状态**当预期答案去对——
    - status=1 且近期核实过的车 → 详情页应当判 ALIVE
    - status=2 且 contact_info 含「保護中」的车 → 详情页应当判 SOLD
    不一致时再走 :func:`_drift_reason` 判断**是不是真的漂移**（见那里的说明）。

    **刻意不存指纹文件 / 不做字节哈希**：对**语义**（页面该长什么样）而不是字节，
    站点改一个无关的日期文案不会误报；而且没有"基线文件丢失/过期"这类运维负担。

    ⚠️ 探针**永远只告警，绝不自动清库** —— 判据漂移时按错的判据批量删车，
    正是最该避免的事。反过来，漂移也**不该直接中止复核**：实测过的失效模式是
    「什么都不写」或「标签写错」，没有一条会导致误删（判不准一律 UNKNOWN）。
    真正危险的是**没人知道它已经不准了**，所以这里只负责把话说出来。

    Returns: ``{"checked", "drift", "unreachable", "details": [...]}``
        - ``drift``：判据漂移条数（>0 应当告警）
        - ``unreachable``：网络/代理失败，与判据无关，单独计（否则会淹没真实漂移）
    """
    out: dict = {"checked": 0, "drift": 0, "unreachable": 0, "details": []}

    cur = conn.cursor()
    cur.execute(_PROBE_ALIVE_SQL, (sample,))
    cases = [(vid, url, DetailState.ALIVE) for vid, url in cur.fetchall()]
    cur.execute(_PROBE_SOLD_SQL, ("%保護中%", "%保护中%", sample))
    cases += [(vid, url, DetailState.SOLD) for vid, url in cur.fetchall()]
    cur.close()

    for vehicle_id, car_url, expected in cases:
        h_vid = extract_h_vid(car_url)
        if not h_vid:
            continue
        status_code, body = fetch(h_vid)
        out["checked"] += 1
        if status_code is None or body is None:
            out["unreachable"] += 1
            continue
        got = classify_detail(status_code, body)
        reason = _drift_reason(expected, got)
        if reason:
            out["drift"] += 1
            out["details"].append(
                f"{vehicle_id}: 期望 {expected.value}，实得 {got.value}"
                f"（HTTP {status_code}）—— {reason}"
            )
    return out


# ---------------------------------------------------------------------------
# CLI（默认 dry-run；--apply 才写库）
# ---------------------------------------------------------------------------

def _load_db_conn():
    """CLI 专用建连：**先加载 `.env`**，再按环境变量连库。

    ⚠️ 这一步不能省。本模块顶层刻意只依赖 stdlib（见模块 docstring），
    所以不会像 `core/proxy.py` 那样在 import 时顺手把 `.env` 读进来。
    漏了它，`python -m carinfo.core.revalidator` 会拿着 `localhost:5432` 的
    默认值去连 —— 而且**是在 import 全部成功之后**才炸 Connection refused，
    很容易被误判成"数据库挂了"而不是"没读 .env"（实测踩到）。
    走 `load_dotenv_once()`（幂等、允许 python-dotenv 缺席）而不是自己调
    `load_dotenv()`，避免又多出一个 env 加载点。
    """
    import os

    import psycopg2

    from carinfo.search.config import load_dotenv_once

    load_dotenv_once()

    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", 5432)),
        user=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASSWORD", ""),
        dbname=os.environ.get("DB_NAME", "mycar"),
        connect_timeout=30,
    )


def make_spider_fetcher(vehicle_type: int, request_interval, concurrency: int):
    """用爬虫实例做取页器：复用它的代理池、请求头与**全局限速器**。

    ⚠️ 这里**不设** `spider.concurrency` —— 并发由 `revalidate_ids()` 的线程池控制，
    而 `fetch_detail_raw` 是单次请求、不读 spider.concurrency。写了只会让
    Car28Spider 那行「已加载爬取配置: concurrency=5」的日志与实际线程数对不上，
    反而误导排查。`concurrency` 参数保留只为与调用方签名一致。
    """
    from carinfo.sites.car28 import Car28Spider

    spider = Car28Spider(1, vehicle_type=vehicle_type,
                         request_interval=request_interval)

    def fetch(h_vid: str):
        return spider.fetch_detail_raw(h_vid)

    return fetch


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ap = argparse.ArgumentParser(description="车源复核（默认 dry-run，加 --apply 才写库）")
    ap.add_argument("--apply", action="store_true", help="真的写库（默认只报告）")
    ap.add_argument("--type", type=int, default=1, help="车辆类型，默认 1（私家车）")
    ap.add_argument("--limit", type=int, default=200, help="本轮最多复核多少台")
    ap.add_argument("--window-days", type=int, default=VERIFY_WINDOW_DAYS)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--interval", type=float, nargs=2, default=(1.0, 1.5),
                    metavar=("MIN", "MAX"), help="发送间隔秒（全局限速）")
    ap.add_argument("--probe", action="store_true",
                    help="额外跑一次判据漂移探针（只读，抽查 3+3 台）")
    args = ap.parse_args(argv)

    conn = _load_db_conn()
    try:
        # 先探针（只读）：站点改版时判据会静默失效，这里把它变成一条告警
        if args.probe:
            fetch_p = make_spider_fetcher(args.type, tuple(args.interval), 2)
            p = probe_drift(conn, fetch_p)
            print(f"[探针] 抽查 {p['checked']} 台：判据漂移 {p['drift']} / "
                  f"取不到页 {p['unreachable']}")
            for d in p["details"]:
                print(f"  ⚠ {d}")

        cands = fetch_candidates(conn, window_days=args.window_days,
                                 vehicle_type=args.type, limit=args.limit)
        if not cands:
            print("没有待复核的车。")
            return 0
        print(f"待复核 {len(cands)} 台（window={args.window_days} 天，"
              f"{'写库' if args.apply else 'DRY-RUN'}）")

        vid_by_hvid = {h: vid for vid, h in cands}
        fetch = make_spider_fetcher(args.type, tuple(args.interval), args.concurrency)
        writer = None if not args.apply else make_db_writer(
            conn, lambda h: vid_by_hvid.get(h))

        def progress(done: int, total: int) -> None:
            if done % 25 == 0 or done == total:
                print(f"  进度 {done}/{total}")

        report = revalidate_ids(
            [h for _vid, h in cands], fetch,
            writer=writer, concurrency=args.concurrency,
            on_progress=progress, dry_run=not args.apply,
        )
        print(report.summary())
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
