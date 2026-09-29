"""车源复核的门禁（**离线**：不联网、不连库、不烧 token）。

迁移自 `_migration/test_revalidator.py`，纳入版本控制（原脚本在 gitignore 内，
每次重构都会被静默打破 —— 见 2026-09-29 的教训）。

红线：**不许用子串「已售」判已售** —— 正常在售页也含一次「已售」，用它判会把
活车大批误杀（比不治理更糟）。这里用 fixture 把正确判据与那条反例都钉死，
改 `classify_detail` 必然在这里被拦下。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from carinfo.core.revalidator import (
    STATUS_SOLD,
    VERIFY_WINDOW_DAYS,
    DetailState,
    classify_detail,
    extract_h_vid,
    make_db_writer,
    probe_drift,
    revalidate_ids,
    today_beijing,
)

REPO = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# 真实页面构造的 fixture
# ---------------------------------------------------------------------------

# 正常在售详情页：有详情表格；**并含有一次「已售」子串**（无关模板区）——
# 这正是「不能用子串判定」的实证。
FIX_ALIVE = """<html><head><title>2018 Toyota Alphard</title></head><body>
<script>var t="已售出請聯絡車主";/* 模板里的无关文案 */</script>
<table width="100%"><tr><td class="frm_l">年份</td><td class="frm_t">2018</td></tr>
<tr><td class="frm_l">售價</td><td class="frm_t">$268,000</td></tr>
<tr><td class="frm_l">聯絡人資料</td><td class="frm_t">陳先生 98524136</td></tr></table>
</body></html>"""

# 已售但页面仍在：聯絡人資料格显示已被保護，**详情表格仍然存在**
FIX_SOLD = """<html><head><title>2018 Toyota Alphard</title></head><body>
<table width="100%"><tr><td class="frm_l">年份</td><td class="frm_t">2018</td></tr>
<tr><td class="frm_l">聯絡人資料</td><td class="frm_t">由於已售，資料亦被保護中。</td></tr></table>
</body></html>"""

# 已删：HTTP 200 + JS 跳转 msg_noid，无表格无标题
FIX_DELETED = """<html><head></head><body>
<script language="javascript">window.location='msg_noid.php';</script>
</body></html>"""

# 反爬拒绝页：HTTP 200
FIX_BUSY = """<html><head><title>請稍後再試</title></head><body>
<script>window.location='msg_busy.php';</script>
</body></html>"""


# ---------------------------------------------------------------------------
# [1]-[3] 三态判定（含关键反例）
# ---------------------------------------------------------------------------
def test_classify_three_states():
    assert classify_detail(200, FIX_ALIVE) == DetailState.ALIVE
    assert classify_detail(200, FIX_SOLD) == DetailState.SOLD
    assert classify_detail(200, FIX_DELETED) == DetailState.DELETED
    assert classify_detail(200, FIX_BUSY) == DetailState.BUSY


def test_alive_page_never_misclassified():
    """反例：在售页含一次「已售」子串，但**不许**判 SOLD/DELETED（子串不是判据）。"""
    assert classify_detail(200, FIX_ALIVE) != DetailState.SOLD
    assert classify_detail(200, FIX_ALIVE) != DetailState.DELETED
    # 「保護中」必须先于表格判定，否则已售页会被当成在售
    assert classify_detail(200, FIX_SOLD) != DetailState.ALIVE


def test_unknown_never_deleted():
    assert classify_detail(None, None) == DetailState.UNKNOWN
    # 404/410 是兜底（站点实际对删帖返回 200 + msg_noid）
    assert classify_detail(404, "") == DetailState.DELETED
    assert classify_detail(410, "") == DetailState.DELETED
    # 5xx / 空正文 / 无表格无标记 → UNKNOWN（绝不删）
    assert classify_detail(500, "server error") == DetailState.UNKNOWN
    assert classify_detail(200, "") == DetailState.UNKNOWN
    assert classify_detail(200, "<html><body>hello</body></html>") == DetailState.UNKNOWN
    # 简体的「保護中」也要认
    assert classify_detail(
        200,
        "<td class=frm_l></td><td class=frm_t>由于已售，联络人资料亦被保护中</td>",
    ) == DetailState.SOLD


def test_extract_h_vid():
    assert extract_h_vid(
        "https://x.28car.com/sell_dsp.php?h_vid=602982973&h_vw=y"
    ) == "602982973"
    assert extract_h_vid("https://x.28car.com/sell_lst.php?h_f_ty=1") is None
    assert extract_h_vid(None) is None


def test_verify_window_days_single_source():
    assert VERIFY_WINDOW_DAYS == 30


# ---------------------------------------------------------------------------
# [5] 执行器（注入假 fetch，离线）
# ---------------------------------------------------------------------------
SCRIPTS = {
    "a1": FIX_ALIVE, "a2": FIX_ALIVE,
    "s1": FIX_SOLD,
    "d1": FIX_DELETED,
    "x1": "<html><body>garbage</body></html>",
    "b1": FIX_BUSY,
}


def make_fetch(busy_forever=False):
    calls = []

    def fetch(h_vid):
        calls.append(h_vid)
        if busy_forever:
            return 200, FIX_BUSY
        body = SCRIPTS.get(h_vid)
        if body is None:
            return None, None
        return 200, body

    fetch.calls = calls
    return fetch


def test_revalidate_stats_and_writer_calls():
    written = []

    def writer(h_vid, state, today):
        written.append((h_vid, state.value, today))

    rep = revalidate_ids(["a1", "a2", "s1", "d1", "x1"], make_fetch(),
                         writer=writer, dry_run=False, concurrency=2)
    assert (rep.alive, rep.sold, rep.deleted) == (2, 1, 1)
    assert rep.skipped == 1
    assert rep.considered == 5
    # writer 只被三态调用（4 次，不含判不出的 x1）
    assert len(written) == 4
    assert all(h != "x1" for h, _, _ in written)
    assert all(len(d) == 10 and d[4] == "-" for _, _, d in written)
    assert any(h == "d1" and st == "deleted" for h, st, _ in written)


def test_revalidate_dry_run_never_writes():
    written = []
    rep = revalidate_ids(["a1", "s1"], make_fetch(),
                         writer=lambda *a: written.append(a), dry_run=True)
    assert written == []
    assert rep.alive == 1 and rep.sold == 1


def test_revalidate_busy_aborts_not_crashes():
    rep = revalidate_ids([f"b{i}" for i in range(20)], make_fetch(busy_forever=True),
                         dry_run=True, concurrency=1, busy_pause=0.0,
                         busy_retry_per_id=0, max_busy_streak=3)
    assert rep.aborted
    assert rep.alive == 0 and rep.sold == 0 and rep.deleted == 0
    assert rep.skipped > 0


def test_revalidate_empty_input():
    assert revalidate_ids([], make_fetch()).considered == 0


# ---------------------------------------------------------------------------
# [7] 报告口径：判定计数与落库计数必须分开（P0-2 的可观测性）
# ---------------------------------------------------------------------------
def test_write_failure_counters_separated():
    def boom(h_vid, state, today):
        raise RuntimeError("模拟写库炸了")

    rep = revalidate_ids(["a1", "a2", "s1"], make_fetch(),
                         writer=boom, dry_run=False, concurrency=1)
    assert rep.wrote == 0
    assert rep.write_failed == 3
    # 判定计数在写库之前就加了 —— 所以「整轮一条没写进去」会藏在这里
    assert rep.alive == 2 and rep.sold == 1
    assert rep.skipped == 0
    assert "写库失败" in rep.summary()

    rep_ok = revalidate_ids(["a1", "s1", "x1"], make_fetch(),
                            writer=lambda *a: None, dry_run=False)
    assert rep_ok.wrote == 2
    assert rep_ok.unknown == 1 and rep_ok.busy_gave_up == 0


# ---------------------------------------------------------------------------
# [8] 写库器：串行化 + 失败必 rollback（P0-2）
# ---------------------------------------------------------------------------
class FakeConn:
    """记录 execute/commit/rollback 的假连接（不连库）。"""

    def __init__(self, fail_execute=False, fetch_queue=None):
        self.executed = []          # [(sql, params)]
        self.rolled_back = 0
        self.committed = 0
        self.calls = []             # 语句首行，含 SELECT
        self.fail_execute = fail_execute
        self.fetch_queue = list(fetch_queue or [])
        self._depth = 0
        self.max_depth = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        c = self.conn
        c.calls.append(" ".join(sql.split())[:60])
        if sql.strip().upper().startswith("UPDATE"):
            c._depth += 1
            c.max_depth = max(c.max_depth, c._depth)
            try:
                time.sleep(0.004)        # 让出 GIL：没有锁的话并发必然重叠
                if c.fail_execute:
                    raise RuntimeError("模拟 PG 报错")
                c.executed.append((sql, params))
            finally:
                c._depth -= 1

    def fetchall(self):
        if not self.conn.fetch_queue:
            return []
        return self.conn.fetch_queue.pop(0)

    def close(self):
        pass


def test_db_writer_failure_rollbacks_and_raises():
    conn = FakeConn(fail_execute=True)
    w = make_db_writer(conn, lambda h: f"vid-{h}")
    with pytest.raises(RuntimeError):
        w("h1", DetailState.ALIVE, "2026-09-27")
    assert conn.rolled_back == 1
    assert conn.committed == 0


def test_db_writer_serialized_under_threads():
    conn = FakeConn()
    w = make_db_writer(conn, lambda h: f"vid-{h}")
    threads = [threading.Thread(target=w, args=(f"h{i}", DetailState.ALIVE, "2026-09-27"))
               for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert conn.max_depth == 1, "并发写库重入了 —— 一条 psycopg2 连接不支持多线程并发"
    assert conn.committed == 8 and len(conn.executed) == 8


def test_db_writer_sold_does_not_touch_last_verified():
    conn = FakeConn()
    make_db_writer(conn, lambda h: f"vid-{h}")("h9", DetailState.SOLD, "2026-09-27")
    sql_sold, params_sold = conn.executed[0]
    assert params_sold[0] == STATUS_SOLD
    # 已售 = 不在售了，不能回写 last_verified（它是「最后一次被证实在售」）
    assert "last_verified" not in params_sold[1]
    assert "vehicle_status = 1" in " ".join(sql_sold.split())


def test_db_writer_missing_vehicle_id_is_noop():
    conn = FakeConn()
    make_db_writer(conn, lambda h: None)("h404", DetailState.DELETED, "2026-09-27")
    assert conn.executed == []


# ---------------------------------------------------------------------------
# [9] 判据漂移探针：只告警，绝不改数据
# ---------------------------------------------------------------------------
U = "https://x.28car.com/sell_dsp.php?h_vid={}"


def probe_rows(alive=(), sold=()):
    return [list(alive), list(sold)]


def test_probe_drift_alive_expected_but_deleted_is_not_drift():
    conn = FakeConn(fetch_queue=probe_rows([("v1", U.format(111))],
                                           [("v2", U.format(222))]))

    def probe_fetch(h_vid):
        # v1 正常在售；v2 变成删帖页 —— 这是**车被删了**，不是判据漂移
        return (200, FIX_ALIVE) if h_vid == "111" else (200, FIX_DELETED)

    drift = probe_drift(conn, probe_fetch)
    assert drift["checked"] == 2
    assert drift["drift"] == 0
    assert all(c.upper().startswith("SELECT") for c in conn.calls), "探针只能 SELECT"


def test_probe_drift_marker_mismatch_counts():
    conn = FakeConn(fetch_queue=probe_rows([("v1", U.format(111))]))
    d = probe_drift(conn, lambda h: (200, "<html><body>全新改版页面</body></html>"))
    assert d["drift"] == 1
    assert d["details"] and "改版" in d["details"][0]


def test_probe_drift_sold_page_seen_as_alive_counts():
    conn = FakeConn(fetch_queue=probe_rows([], [("v2", U.format(222))]))
    d = probe_drift(conn, lambda h: (200, FIX_ALIVE))
    assert d["drift"] == 1


def test_probe_drift_unreachable_and_busy_not_drift():
    conn = FakeConn(fetch_queue=probe_rows([("v1", U.format(111))]))
    d = probe_drift(conn, lambda h: (None, None))
    assert d["unreachable"] == 1 and d["drift"] == 0

    conn_busy = FakeConn(fetch_queue=probe_rows([("v1", U.format(111))]))
    d_busy = probe_drift(conn_busy, lambda h: (200, FIX_BUSY))
    assert d_busy["drift"] == 0


def test_probe_drift_sold_expected_deleted_is_not_drift():
    conn = FakeConn(fetch_queue=probe_rows([], [("v2", U.format(222))]))
    d = probe_drift(conn, lambda h: (200, FIX_DELETED))
    assert d["drift"] == 0


# ---------------------------------------------------------------------------
# [10] P1-5：列表行已标「已售」→ 不再请求详情页
# ---------------------------------------------------------------------------
def test_sold_list_row_short_circuits_detail_fetch():
    from carinfo.sites.car28 import Car28Spider

    spider = object.__new__(Car28Spider)          # 绕开 __init__（它会去拉代理池）
    seen_details = []
    spider.get_detail_content = lambda h: (seen_details.append(h), FIX_ALIVE)[1]
    spider.extract_car_info = lambda html, hv, ss: {"sale_status": ss, "extra_fields": {}}

    item = {
        "code": "h1", "sale_status": "已售", "list_brand": "豐田",
        "list_model": "ALPHARD", "list_price": "$200,000",
        "date": "2026-09-20", "view_count": 3, "comment_count": 1,
    }
    out_sold = spider._scrape_single_car(dict(item))
    assert seen_details == [], "已售行不该请求详情页"
    assert out_sold["extra_fields"] == {"list_only": True}
    # _list 不能在短路路径里丢（否则 list_date 丢失 → 复核标签失真）
    assert out_sold["_list"]["list_date"] == "2026-09-20"
    assert out_sold["sale_status"] == "已售"

    out_unsold = spider._scrape_single_car(dict(item, sale_status="未售"))
    assert seen_details == ["h1"], "未售行照旧请求详情页"
    assert out_unsold["_list"] == {"view_count": 3, "comment_count": 1,
                                   "list_date": "2026-09-20"}


# ---------------------------------------------------------------------------
# [11] 单点定义：today_beijing 只能有一个来源
# ---------------------------------------------------------------------------
def test_today_beijing_single_source():
    from carinfo.sites import car28 as c28

    assert c28.today_beijing is today_beijing
    assert len(today_beijing()) == 10 and today_beijing()[4] == "-"


# ---------------------------------------------------------------------------
# [12] 日志不得双打（P2-4）
# ---------------------------------------------------------------------------
def test_logger_not_duplicated_after_basicconfig():
    code = (
        "import logging\n"
        "logging.basicConfig(level=logging.INFO, format='%(message)s')\n"
        "from carinfo.sites import car28\n"
        "car28.logger.info('DUPCHK')\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src") + os.pathsep + env.get("PYTHONPATH", "")
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=str(REPO), env=env)
    combined = (p.stdout or "") + (p.stderr or "")
    assert combined.count("DUPCHK") == 1, (
        f"DUPCHK 出现 {combined.count('DUPCHK')} 次（2 次 = 日志双行 bug 复发）；"
        f"rc={p.returncode} 输出={combined.strip()[:200]}"
    )


# ---------------------------------------------------------------------------
# [13] rebuild_default 的 rc 口径（P1-3）
# ---------------------------------------------------------------------------
def test_rebuild_default_rc_semantics(monkeypatch):
    from carinfo.search import features as feat

    monkeypatch.setattr(feat, "main", lambda argv=None: 0)
    ok, msg = feat.rebuild_default()
    assert ok and "原子切换" in msg

    monkeypatch.setattr(feat, "main", lambda argv=None: 3)
    ok, msg = feat.rebuild_default()
    assert (not ok) and "rc=3" in msg

    monkeypatch.setattr(feat, "main", lambda argv=None: 9)
    ok, msg = feat.rebuild_default()
    assert (not ok) and "rc=9" in msg

    def _raise(argv=None):
        raise RuntimeError("连库炸了")

    monkeypatch.setattr(feat, "main", _raise)
    ok, msg = feat.rebuild_default()
    assert (not ok) and "连库炸了" in msg, "异常不能向上传播"

    def _sysexit(argv=None):
        raise SystemExit(4)

    monkeypatch.setattr(feat, "main", _sysexit)
    ok, msg = feat.rebuild_default()
    assert (not ok) and "code=4" in msg, "SystemExit 不能炸调用方"
