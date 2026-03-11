import json
import os
import random
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Tuple


@dataclass(frozen=True)
class TimeWindow:
    start_hm: Tuple[int, int]
    end_hm: Tuple[int, int]


class FileLock:
    """
    简单的跨进程互斥锁：
    - 通过 O_EXCL 创建 lock 文件（原子）
    - 文件内写 pid + 时间戳
    - 若锁文件存在且过旧（stale），允许自动清理
    """

    def __init__(self, path: str, stale_after: timedelta = timedelta(hours=8)):
        self.path = path
        self.stale_after = stale_after
        self._held = False

    def _read_lock(self) -> Optional[dict]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _is_stale(self) -> bool:
        info = self._read_lock()
        if not info:
            return True
        ts = info.get("created_at")
        if not ts:
            return True
        try:
            created = datetime.fromisoformat(ts)
        except Exception:
            return True
        return datetime.now() - created > self.stale_after

    def acquire(self) -> bool:
        if self._held:
            return True

        # 如果存在旧锁，尝试清理
        if os.path.exists(self.path) and self._is_stale():
            try:
                os.remove(self.path)
            except Exception:
                pass

        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            fd = os.open(self.path, flags)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(
                    {"pid": os.getpid(), "created_at": datetime.now().isoformat(timespec="seconds")},
                    f,
                    ensure_ascii=False,
                )
            self._held = True
            return True
        except FileExistsError:
            return False

    def release(self) -> None:
        if not self._held:
            return
        try:
            os.remove(self.path)
        except Exception:
            pass
        self._held = False


class CarinfoService:
    def __init__(self, lock_file: str = "carinfo_run.lock", state_file: str = ".carinfo_service_state.json"):
        self.lock = FileLock(lock_file)
        self.state_file = state_file
        self._running = True

        self.window_morning = TimeWindow((9, 0), (11, 0))
        self.window_afternoon = TimeWindow((16, 0), (20, 0))

        # 每次执行的页数配置
        self.pages_by_type = {1: 40, 2: 20, 3: 20, 4: 20, 5: 20}

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, *_):
        self._log("收到停止信号，准备退出（若正在执行任务，将在本轮结束后退出）", level="WARNING")
        self._running = False

    def _log(self, msg: str, level: str = "INFO"):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] [{level}] {msg}", flush=True)

    def _load_state(self) -> dict:
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self, state: dict) -> None:
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._log(f"保存状态文件失败: {e}", level="WARNING")

    def _window_bounds(self, day: datetime, window: TimeWindow) -> Tuple[datetime, datetime]:
        start = day.replace(hour=window.start_hm[0], minute=window.start_hm[1], second=0, microsecond=0)
        end = day.replace(hour=window.end_hm[0], minute=window.end_hm[1], second=0, microsecond=0)
        return start, end

    def _pick_random_time(self, now: datetime, window: TimeWindow) -> datetime:
        """
        从窗口中挑选一个随机时间点：
        - 如果当前已在窗口前：从窗口全段随机
        - 如果当前在窗口内：从 (now+1min) 到 window_end 随机
        - 如果当前已过窗口：选明天该窗口随机
        """
        today_start, today_end = self._window_bounds(now, window)

        if now < today_start:
            start = today_start
            end = today_end
        elif today_start <= now < today_end:
            start = now + timedelta(minutes=1)
            end = today_end
            if start >= end:
                tomorrow = now + timedelta(days=1)
                start, end = self._window_bounds(tomorrow, window)
        else:
            tomorrow = now + timedelta(days=1)
            start, end = self._window_bounds(tomorrow, window)

        seconds = int((end - start).total_seconds())
        if seconds <= 0:
            return end

        return start + timedelta(seconds=random.randint(0, seconds))

    def _ensure_next_times(self) -> Tuple[datetime, datetime]:
        state = self._load_state()
        now = datetime.now()

        def parse_dt(v: str) -> Optional[datetime]:
            try:
                return datetime.fromisoformat(v)
            except Exception:
                return None

        next_m = parse_dt(state.get("next_morning", ""))
        next_a = parse_dt(state.get("next_afternoon", ""))

        if not next_m or next_m <= now:
            next_m = self._pick_random_time(now, self.window_morning)
        if not next_a or next_a <= now:
            next_a = self._pick_random_time(now, self.window_afternoon)

        state["next_morning"] = next_m.isoformat(timespec="seconds")
        state["next_afternoon"] = next_a.isoformat(timespec="seconds")
        self._save_state(state)

        return next_m, next_a

    def _reschedule_after_skip(self, which: str) -> None:
        now = datetime.now()
        state = self._load_state()
        if which == "morning":
            state["next_morning"] = self._pick_random_time(now, self.window_morning).isoformat(timespec="seconds")
        else:
            state["next_afternoon"] = self._pick_random_time(now, self.window_afternoon).isoformat(timespec="seconds")
        self._save_state(state)

    def _run_one_job(self) -> None:
        if not self.lock.acquire():
            self._log("检测到已有任务在运行（lock存在），本次不启动新任务", level="WARNING")
            return

        start = time.time()
        self._log("=== 开始执行任务（按固定页数爬取并入库）===", level="INFO")

        try:
            if os.getcwd() not in sys.path:
                sys.path.insert(0, os.getcwd())

            import carinfo

            total = 0
            for t in (1, 2, 3, 4, 5):
                pages = self.pages_by_type[t]
                csv = f"car_data_{t}.csv"
                self._log(f"开始类型{t}，页数={pages}", level="INFO")
                total += carinfo.scrape_vehicle_type(t, pages, csv, start_page=1)

            self._log(f"爬取结束，累计抓取 {total} 条，准备入库...", level="INFO")
            carinfo.auto_import_to_database()

            cost = time.time() - start
            self._log(f"=== 任务完成，耗时 {cost/60:.1f} 分钟 ===", level="INFO")

        except Exception as e:
            self._log(f"任务执行异常: {e}", level="ERROR")
            import traceback

            traceback.print_exc()

        finally:
            self.lock.release()

    def run_forever(self) -> None:
        self._log("服务启动：立即执行一次任务（如果当前无任务运行）", level="INFO")
        self._run_one_job()

        self._log("进入调度循环：每天 09:00-11:00 / 16:00-20:00 各随机执行一次", level="INFO")

        while self._running:
            next_m, next_a = self._ensure_next_times()
            now = datetime.now()

            next_run = min(next_m, next_a)
            which = "morning" if next_run == next_m else "afternoon"

            wait_s = max(1, int((next_run - now).total_seconds()))
            self._log(
                f"下一次执行时间: {next_run.strftime('%Y-%m-%d %H:%M:%S')} ({which})，等待 {wait_s} 秒",
                level="INFO",
            )

            slept = 0
            step = 5
            while self._running and slept < wait_s:
                time.sleep(min(step, wait_s - slept))
                slept += step

            if not self._running:
                break

            if os.path.exists(self.lock.path) and not self.lock._is_stale():
                self._log("到达触发时间，但上一次任务仍在运行，跳过并顺延重排", level="WARNING")
                self._reschedule_after_skip(which)
                continue

            self._run_one_job()

            state = self._load_state()
            now2 = datetime.now()
            if which == "morning":
                state["next_morning"] = self._pick_random_time(now2, self.window_morning).isoformat(timespec="seconds")
            else:
                state["next_afternoon"] = self._pick_random_time(now2, self.window_afternoon).isoformat(
                    timespec="seconds"
                )
            self._save_state(state)

        self._log("服务已退出", level="INFO")
