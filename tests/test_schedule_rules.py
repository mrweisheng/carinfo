"""调度窗口与作业模式决策的离线门禁（**不连库、不起服务**）。

迁移自 `_migration/test_schedule.py`，纳入版本控制（原脚本在 gitignore 内，
每次重构都会被静默打破 —— 见 2026-09-29 的教训）。

覆盖：
  [1] config.json 真配置解析   [2] 窗口写法 / 非法值    [3] 旧 schedule.times 报错
  [4] 缺 windows 告警+默认      [5] _pick_random_time 永远未来（防忙循环）
  [6] 窗口命中分布（防长窗口垄断）
  [7] 部分窗口已过              [8] last_run_date       [9] next_run 过期重排
  [10] run_forever 骨架         [11] 深扫决策           [12] from_tomorrow 回归
  [13] 复核调度                 [14] 复核返回状态决定是否写 last_completed
"""

from __future__ import annotations

import itertools
import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from carinfo.service import (
    BEIJING_TZ,
    DEFAULT_WINDOWS,
    CarinfoService,
    now_beijing,
)

REPO = Path(__file__).resolve().parent.parent
CONFIG_JSON = REPO / "config.json"
needs_real_config = pytest.mark.skipif(
    not CONFIG_JSON.exists(), reason="仓库未提供 config.json（gitignored）"
)

BASE = {"scraping": {"vehicle_types": {"1": {"pages": 1}}}}
DEEP_CFG = {
    "scraping": {
        "vehicle_types": {"1": {"pages": 30}},
        "deep_crawl": {"pages": 1200, "interval_days": 30, "request_interval": [1.0, 1.5]},
    }
}
THREE_WIN = {**BASE, "schedule": {"windows": [
    {"name": "上午", "start": "08:00", "end": "12:00"},
    {"name": "下午", "start": "12:00", "end": "18:00"},
    {"name": "晚上", "start": "18:00", "end": "22:00"},
]}}
REVAL_CFG = {
    # 显式给 schedule：否则「缺 windows」的告警会混进来，掩盖真正要断言的告警数
    "schedule": {"windows": [{"name": "全天", "start": "08:00", "end": "22:00"}]},
    "scraping": {
        "vehicle_types": {"1": {"pages": 30}},
        "deep_crawl": {"pages": 1200, "interval_days": 30, "request_interval": [1.0, 1.5]},
        "revalidation": {"enabled": True, "interval_days": 7, "batch_size": 3000,
                         "concurrency": 4, "vehicle_type": 1,
                         "request_interval": [1.0, 1.5]},
    },
}


def dstr(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


def rv(days_ago: int) -> dict:
    return {"last_completed": dstr(days_ago)}


@pytest.fixture
def make_service(tmp_path, monkeypatch):
    """在独立临时目录里造 CarinfoService：临时 config.json + 临时状态文件。

    DB 健康检查是唯一会连库的地方 —— 这里 monkeypatch 成 no-op，保持
    「不起服务、不碰数据库」的承诺。每次调用给一份全新目录，互不串扰。
    """
    counter = itertools.count()

    def _make(config: dict, state: dict | None = None) -> CarinfoService:
        monkeypatch.setattr(CarinfoService, "_check_database", lambda self: None)
        d = tmp_path / f"svc{next(counter)}"
        d.mkdir()
        monkeypatch.chdir(d)
        (d / "config.json").write_text(
            json.dumps(config, ensure_ascii=False), encoding="utf-8")
        st = d / "state.json"
        if state is not None:
            st.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        return CarinfoService(lock_file=str(d / "run.lock"), state_file=str(st))

    return _make


def _real_service(tmp_path, monkeypatch) -> CarinfoService:
    monkeypatch.setattr(CarinfoService, "_check_database", lambda self: None)
    monkeypatch.chdir(REPO)
    return CarinfoService(lock_file=str(tmp_path / "l.lock"),
                          state_file=str(tmp_path / "s.json"))


# ---------------------------------------------------------------------------
# [1] config.json 真配置解析
# ---------------------------------------------------------------------------
@needs_real_config
def test_real_config_windows(tmp_path, monkeypatch):
    svc = _real_service(tmp_path, monkeypatch)
    assert len(svc.windows) == 3
    assert [(w.name, w.start_hm, w.end_hm) for w in svc.windows] == [
        ("上午", (8, 0), (12, 0)), ("下午", (12, 0), (18, 0)), ("晚上", (18, 0), (22, 0))]
    assert svc._startup_warnings == []


# ---------------------------------------------------------------------------
# [2] 窗口解析：多种写法 + 非法值
# ---------------------------------------------------------------------------
def test_window_parsing_variants(make_service):
    s = make_service({**BASE, "schedule": {"windows": [
        {"name": "A", "start": "07:30", "end": [11, 45]},
        {"name": "B", "start": 13, "end": "15:00"},
        {"name": "C", "start": "16：20", "end": "17:00"},   # 全角冒号
    ]}})
    assert [(w.start_hm, w.end_hm) for w in s.windows] == [
        ((7, 30), (11, 45)), ((13, 0), (15, 0)), ((16, 20), (17, 0))]
    assert s._startup_warnings == []


def test_window_parsing_invalid_entries(make_service):
    s = make_service({**BASE, "schedule": {"windows": [
        {"name": "好", "start": "08:00", "end": "10:00"},
        {"name": "坏", "start": "八点", "end": "10:00"},     # 解析不出
        {"name": "缺", "end": "12:00"},                       # 缺 start
        "garbage",                                            # 非对象
    ]}})
    assert len(s.windows) == 1 and s.windows[0].name == "好"
    assert len(s._startup_warnings) == 3, "每个非法条目都要有告警"


def test_window_out_of_range_falls_back_with_warning(make_service):
    s = make_service({**BASE, "schedule": {"windows": [
        {"name": "X", "start": "25:00", "end": "26:00"}]}})
    assert len(s.windows) == 3 and len(s._startup_warnings) >= 1


def test_window_start_ge_end_raises(make_service):
    with pytest.raises(ValueError) as ei:
        make_service({**BASE, "schedule": {"windows": [
            {"name": "倒", "start": "18:00", "end": "08:00"}]}})
    assert "start < end" in str(ei.value)


# ---------------------------------------------------------------------------
# [3] 旧 schedule.times 显式报错（不静默丢弃）
# ---------------------------------------------------------------------------
def test_legacy_schedule_times_raises(make_service):
    with pytest.raises(ValueError) as ei:
        make_service({**BASE, "schedule": {"times": ["08:00", "12:00", "16:00", "20:00"]}})
    assert "times" in str(ei.value) and "windows" in str(ei.value)


# ---------------------------------------------------------------------------
# [4] 缺 schedule.windows → 告警 + 默认三段
# ---------------------------------------------------------------------------
def test_missing_windows_defaults_with_warning(make_service):
    s = make_service(BASE)
    assert s.windows == DEFAULT_WINDOWS
    assert len(s._startup_warnings) == 1
    s2 = make_service({**BASE, "schedule": {}})
    assert len(s2._startup_warnings) == 1


# ---------------------------------------------------------------------------
# [5] _pick_random_time 永远返回未来时刻（防忙循环 —— 最要命的不变量）
# ---------------------------------------------------------------------------
def test_pick_random_time_always_future_and_in_window(make_service):
    random.seed(20260925)
    s = make_service(THREE_WIN)
    bad, outside = [], []
    for h in range(24):
        for m in (0, 1, 30, 59):
            now = datetime(2026, 9, 25, h, m, tzinfo=BEIJING_TZ)
            got = s._pick_random_time(now)
            if got <= now:
                bad.append((now.isoformat(), got.isoformat()))
            if s._describe_window(got) == "":
                outside.append((now.time().isoformat(), got.isoformat()))
    assert not bad, f"返回了过去时刻（run_forever 会忙循环重排）: {bad[:5]}"
    assert not outside, f"返回时刻不落在任何窗口内: {outside[:5]}"


def test_pick_random_time_naive_now(make_service):
    s = make_service(THREE_WIN)
    naive = datetime(2026, 9, 25, 10, 0)
    got = s._pick_random_time(naive)
    assert got.tzinfo is not None and got > naive.replace(tzinfo=BEIJING_TZ)


# ---------------------------------------------------------------------------
# [6] 三段窗口命中分布（各窗口都要被抽到，不能垄断）
# ---------------------------------------------------------------------------
def test_window_hit_distribution(make_service):
    random.seed(7)
    s = make_service(THREE_WIN)
    now = datetime(2026, 9, 25, 0, 1, tzinfo=BEIJING_TZ)   # 凌晨 → 三窗口全可用
    hits = {"上午": 0, "下午": 0, "晚上": 0}
    for _ in range(3000):
        hits[s._describe_window(s._pick_random_time(now))] += 1
    assert all(v > 500 for v in hits.values()), hits
    assert all(700 < v < 1300 for v in hits.values()), hits


def test_short_window_not_monopolized(make_service):
    """短窗口不该被长窗口吃掉（拼成一条线抽签会让 10 分钟窗口几乎抽不到）。"""
    random.seed(11)
    s2 = make_service({**BASE, "schedule": {"windows": [
        {"name": "短", "start": "08:00", "end": "08:10"},
        {"name": "长", "start": "09:00", "end": "21:00"}]}})
    now = datetime(2026, 9, 25, 0, 1, tzinfo=BEIJING_TZ)
    h2 = {"短": 0, "长": 0}
    for _ in range(2000):
        h2[s2._describe_window(s2._pick_random_time(now))] += 1
    assert 800 < h2["短"] < 1200, h2


# ---------------------------------------------------------------------------
# [7] 部分窗口已过 → 只在剩余窗口里挑
# ---------------------------------------------------------------------------
def test_partial_windows_passed(make_service):
    random.seed(3)
    s = make_service(THREE_WIN)
    now = datetime(2026, 9, 25, 13, 0, tzinfo=BEIJING_TZ)   # 上午已过
    seen = {s._describe_window(s._pick_random_time(now)) for _ in range(500)}
    assert seen == {"下午", "晚上"}, seen

    now2 = datetime(2026, 9, 25, 21, 30, tzinfo=BEIJING_TZ)  # 只剩晚上 30 分钟
    t = s._pick_random_time(now2)
    assert s._describe_window(t) == "晚上" and t > now2

    now3 = datetime(2026, 9, 25, 23, 0, tzinfo=BEIJING_TZ)   # 今天全过 → 明天
    assert s._pick_random_time(now3).date() == datetime(2026, 9, 26).date()


# ---------------------------------------------------------------------------
# [8] 状态文件可读
# ---------------------------------------------------------------------------
def test_state_last_run_date_readable(make_service):
    s = make_service(
        {**BASE, "schedule": {"windows": [
            {"name": "上午", "start": "08:00", "end": "12:00"}]}},
        state={"last_run_date": "2026-09-25", "next_run": "2026-09-25T09:00:00"})
    assert s._load_state().get("last_run_date") == "2026-09-25"


# ---------------------------------------------------------------------------
# [9] next_run 过期 → 重排；未过期 → 沿用
# ---------------------------------------------------------------------------
def test_ensure_next_time_reuse_and_reschedule(make_service):
    one_win = {**BASE, "schedule": {"windows": [
        {"name": "全天", "start": "08:00", "end": "22:00"}]}}

    # 用真实当前时间构造「未来」值：_ensure_next_time 内部拿 now_beijing() 判断，
    # 写死日期跨天后会变过去时刻（踩过：09-25 写死的「未来」在 09-26 引爆）
    now = now_beijing()
    future = (now + timedelta(hours=2)).isoformat(timespec="seconds")

    s4 = make_service(one_win, state={"next_run": future})
    picked = s4._ensure_next_time()
    assert picked.isoformat(timespec="seconds") == future, "未过期的 next_run 应被沿用"

    s5 = make_service(one_win, state={"next_run": "2020-01-01T00:00:00"})
    picked = s5._ensure_next_time()
    assert picked > datetime.now(BEIJING_TZ) - timedelta(minutes=1)
    assert s5._load_state().get("next_run") == picked.isoformat(timespec="seconds")

    s6 = make_service(one_win)
    assert s6._ensure_next_time() > datetime.now(BEIJING_TZ) - timedelta(minutes=1)


# ---------------------------------------------------------------------------
# [10] 端到端：已执行则跳过、未执行则跑且只跑一次
# ---------------------------------------------------------------------------
def test_run_forever_skips_when_already_done(tmp_path, monkeypatch):
    class SvcProbe(CarinfoService):
        def __init__(self, *a, **kw):
            self.ran = 0
            super().__init__(*a, **kw)

        def _run_one_job(self):
            self.ran += 1
            state = self._load_state()
            state["last_run_date"] = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
            self._save_state(state)

        def run_forever(self):          # 只跑启动段，不进调度循环
            self._flush_startup_warnings()
            state = self._load_state()
            today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
            if state.get("last_run_date", "") == today:
                self._log(f"今天({today})已执行过任务，启动后等待下一周期，不重复执行")
                self._plan_next_run(datetime.now(BEIJING_TZ), from_tomorrow=True)
            else:
                self._log("启动时检测今天未执行，立即执行任务")
                self._run_one_job()
                self._plan_next_run(datetime.now(BEIJING_TZ), from_tomorrow=False)

    today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    cfg = {**BASE, "schedule": {"windows": [{"name": "全天", "start": "08:00", "end": "22:00"}]}}

    monkeypatch.setattr(CarinfoService, "_check_database", lambda self: None)
    d = tmp_path / "e2e"
    d.mkdir()
    monkeypatch.chdir(d)
    (d / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    st_path = d / "state.json"

    p1 = SvcProbe(lock_file=str(d / "l.lock"), state_file=str(st_path))
    p1.run_forever()
    assert p1.ran == 1, "首次启动：今天未执行 → 应跑一次"
    assert json.loads(st_path.read_text(encoding="utf-8"))["last_run_date"] == today

    p2 = SvcProbe(lock_file=str(d / "l.lock"), state_file=str(st_path))
    p2.run_forever()
    assert p2.ran == 0, "重启后：今天已执行 → 一次都不跑"
    nr = json.loads(st_path.read_text(encoding="utf-8"))["next_run"]
    assert datetime.fromisoformat(nr).date() == (
        datetime.now(BEIJING_TZ) + timedelta(days=1)).date()


# ---------------------------------------------------------------------------
# [11] 深扫决策：到期触发 / 续跑优先 / 禁用 / 配置解析
# ---------------------------------------------------------------------------
def test_deep_crawl_config_and_mode(make_service):
    s = make_service(DEEP_CFG)
    assert (s.deep_pages, s.deep_interval_days, s.deep_request_interval) == (1200, 30, [1.0, 1.5])

    assert s._resolve_job_mode({})["mode"] == "deep"                       # 从未深扫
    assert s._resolve_job_mode(
        {"deep_crawl": {"last_completed": dstr(3), "progress": {}}})["mode"] == "daily"
    assert s._resolve_job_mode(
        {"deep_crawl": {"last_completed": dstr(30), "progress": {}}})["mode"] == "deep"
    # 有未完成进度 → 续跑（优先级高于间隔）
    assert s._resolve_job_mode({"deep_crawl": {
        "last_completed": dstr(1),
        "progress": {"1": {"target": 1200, "last_page": 447}}}}).get("resume") is True
    # 进度已到目标 → 不算未完成
    assert s._resolve_job_mode({"deep_crawl": {
        "last_completed": dstr(3),
        "progress": {"1": {"target": 1200, "last_page": 1200}}}})["mode"] == "daily"
    # last_completed 非法 → 视为到期
    assert s._resolve_job_mode(
        {"deep_crawl": {"last_completed": "垃圾", "progress": {}}})["mode"] == "deep"


def test_deep_pages_zero_disables(make_service):
    s2 = make_service({**DEEP_CFG, "scraping": {
        "vehicle_types": {"1": {"pages": 30}}, "deep_crawl": {"pages": 0}}})
    assert s2.deep_pages == 0
    assert s2._resolve_job_mode({})["mode"] == "daily"


def test_deep_progress_roundtrip_and_completion(make_service):
    s3 = make_service(DEEP_CFG)
    s3._save_deep_progress(1, 447)
    saved = json.loads(Path(s3.state_file).read_text(encoding="utf-8"))
    assert saved["deep_crawl"]["progress"]["1"] == {"target": 1200, "last_page": 447}
    assert s3._resolve_job_mode(saved).get("resume") is True

    s3._mark_deep_completed()
    after = json.loads(Path(s3.state_file).read_text(encoding="utf-8"))
    assert after["deep_crawl"]["last_completed"] == date.today().isoformat()
    assert after["deep_crawl"]["progress"] == {}
    assert s3._resolve_job_mode(after)["mode"] == "daily"


@needs_real_config
def test_real_config_deep(tmp_path, monkeypatch):
    svc = _real_service(tmp_path, monkeypatch)
    assert (svc.deep_pages, svc.deep_interval_days, svc.deep_request_interval) == (
        1200, 30, [1.0, 1.5])


# ---------------------------------------------------------------------------
# [12] from_tomorrow 回归：>=22:00 重启不再把任务推到后天（2026-09-25 事故）
# ---------------------------------------------------------------------------
def test_from_tomorrow_never_skips_a_day(make_service):
    s = make_service(THREE_WIN)
    for hh, mm in ((22, 45), (23, 3), (22, 0)):
        nr = s._plan_next_run(
            datetime(2026, 9, 25, hh, mm, tzinfo=BEIJING_TZ), from_tomorrow=True)
        assert nr.date() == datetime(2026, 9, 26).date(), (hh, mm, nr.isoformat())

    # 白天重启：明天全部窗口可选（修复前只会命中明天的晚上窗口）
    seen_morning = False
    for _ in range(300):
        r = s._plan_next_run(
            datetime(2026, 9, 25, 15, 0, tzinfo=BEIJING_TZ), from_tomorrow=True)
        assert r.date() == datetime(2026, 9, 26).date()
        if r.hour < 12:
            seen_morning = True
    assert seen_morning


def test_ensure_next_time_today_done_goes_tomorrow(make_service):
    today_real = date.today().isoformat()
    s = make_service(THREE_WIN, state={
        "last_run_date": today_real, "next_run": f"{today_real}T09:00:00"})
    picked = s._ensure_next_time()
    assert picked.date() == date.today() + timedelta(days=1)


# ---------------------------------------------------------------------------
# [13] 车源复核调度：优先级 / 开关 / 缺省关闭 / 完成标记
# ---------------------------------------------------------------------------
def test_revalidation_config_and_default_off(make_service):
    s = make_service(REVAL_CFG)
    assert (s.reval_enabled, s.reval_interval_days, s.reval_batch_size,
            s.reval_concurrency, s.reval_vehicle_type, s.reval_request_interval) == (
        True, 7, 3000, 4, 1, [1.0, 1.5])
    assert s._startup_warnings == []

    # 缺省必须关闭：复核会真的改库（把已售/已删的车移出检索）
    assert make_service(DEEP_CFG).reval_enabled is False
    assert make_service({**REVAL_CFG, "scraping": {
        **REVAL_CFG["scraping"], "revalidation": {"enabled": False}}}).reval_enabled is False
    s_bad = make_service({**REVAL_CFG, "scraping": {
        **REVAL_CFG["scraping"], "revalidation": "垃圾"}})
    assert s_bad.reval_enabled is False and len(s_bad._startup_warnings) == 1


def test_revalidation_mode_and_priority(make_service):
    s = make_service(REVAL_CFG)
    deep_ok = {"deep_crawl": {"last_completed": dstr(3), "progress": {}}}
    assert s._resolve_job_mode(deep_ok)["mode"] == "revalidate"          # 从未复核
    assert s._resolve_job_mode({**deep_ok, "revalidation": rv(3)})["mode"] == "daily"
    assert s._resolve_job_mode({**deep_ok, "revalidation": rv(7)})["mode"] == "revalidate"
    assert s._resolve_job_mode(
        {**deep_ok, "revalidation": {"last_completed": "垃圾"}})["mode"] == "revalidate"
    # 优先级：深扫到期 > 复核到期 > 日常
    assert s._resolve_job_mode({"deep_crawl": {"last_completed": dstr(30), "progress": {}},
                                "revalidation": rv(30)})["mode"] == "deep"
    assert s._resolve_job_mode({"deep_crawl": {"last_completed": dstr(1), "progress": {}},
                                "revalidation": rv(30)})["mode"] == "revalidate"


def test_deep_disabled_does_not_swallow_revalidation(make_service):
    s_dd = make_service({**REVAL_CFG, "scraping": {
        **REVAL_CFG["scraping"], "deep_crawl": {"pages": 0}}})
    assert s_dd._resolve_job_mode({"revalidation": rv(30)})["mode"] == "revalidate"
    assert s_dd._resolve_job_mode({"revalidation": rv(1)})["mode"] == "daily"


def test_reval_completed_roundtrip(make_service):
    s3 = make_service(REVAL_CFG)
    s3._mark_reval_completed()
    after = json.loads(Path(s3.state_file).read_text(encoding="utf-8"))
    assert after["revalidation"]["last_completed"] == date.today().isoformat()
    assert s3._resolve_job_mode(
        {"deep_crawl": {"last_completed": dstr(1), "progress": {}},
         "revalidation": after["revalidation"]})["mode"] == "daily"


def test_verify_window_single_source():
    from carinfo.core import revalidator as rvmod
    assert rvmod.VERIFY_WINDOW_DAYS == 30


@needs_real_config
def test_real_config_revalidation(tmp_path, monkeypatch):
    svc = _real_service(tmp_path, monkeypatch)
    assert (svc.reval_enabled, svc.reval_interval_days, svc.reval_batch_size) == (True, 7, 3000)
    assert svc._startup_warnings == []
    real = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
    assert "window_days" not in (real.get("scraping", {}).get("revalidation") or {}), \
        "窗口必须在 revalidator.VERIFY_WINDOW_DAYS 单点定义，防两处漂移"


# ---------------------------------------------------------------------------
# [14] 复核返回**状态**，据此决定要不要写 last_completed（P0-1）
# ---------------------------------------------------------------------------
def test_revalidation_return_status_gates_completion(make_service):
    today = date.today().isoformat()
    base_state = {"deep_crawl": {"last_completed": dstr(1), "progress": {}},
                  "revalidation": rv(30)}          # 30 天前复核过 → 本轮到期

    for result, expect_mark in (("success", True), ("aborted", False), ("error", False)):
        svc = make_service(REVAL_CFG, dict(base_state))
        svc._run_revalidation_job = lambda r=result: r
        logs = []
        svc._log = lambda msg, level="INFO": logs.append((level, str(msg)))
        svc._run_one_job()
        st = json.loads(Path(svc.state_file).read_text(encoding="utf-8"))

        want = today if expect_mark else dstr(30)
        assert (st.get("revalidation") or {}).get("last_completed") == want, result
        # last_run_date 与 last_completed 是两件事：任何返回值都要写，否则同日反复触发
        assert st.get("last_run_date") == today, result
        if expect_mark:
            assert not any("未完成" in m for _l, m in logs)
        else:
            assert any(l == "WARNING" and "未完成" in m and "last_completed" in m
                       for l, m in logs), (result, logs)


def test_run_revalidation_job_returns_status_not_counts():
    import inspect

    doc = inspect.getdoc(CarinfoService._run_revalidation_job) or ""
    assert all(k in doc for k in ('"success"', '"aborted"', '"error"'))
    ret_lines = [ln.strip() for ln in
                 inspect.getsource(CarinfoService._run_revalidation_job).splitlines()
                 if ln.strip().startswith("return ")]
    assert ret_lines and all(
        ln in ('return "error"', 'return "success"', 'return "aborted"')
        for ln in ret_lines), ret_lines
