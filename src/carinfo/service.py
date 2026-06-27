import json
import os
import random
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

# 北京时间时区
BEIJING_TZ = timezone(timedelta(hours=8))


def now_beijing():
    """获取当前北京时间"""
    return datetime.now(BEIJING_TZ)


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
        return now_beijing() - created > self.stale_after

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
                    {
                        "pid": os.getpid(),
                        "created_at": now_beijing().isoformat(timespec="seconds"),
                    },
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
    def __init__(
        self,
        lock_file: str = "carinfo_run.lock",
        state_file: str = ".carinfo_service_state.json",
    ):
        self.lock = FileLock(lock_file)
        self.state_file = state_file
        self._running = True

        # 从配置文件加载时间和页数配置
        self._load_config()

        # 启动时检查数据库连接
        self._check_database()

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _load_config(self):
        """从配置文件加载配置"""
        config_file = "config.json"
        if not os.path.exists(config_file):
            raise FileNotFoundError(
                f"配置文件 {config_file} 不存在，请创建配置文件后重试"
            )

        try:
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"配置文件 {config_file} 格式错误: {e}")

        # 加载爬取页数配置
        vehicle_types = config.get("scraping", {}).get("vehicle_types", {})
        if not vehicle_types:
            raise ValueError("配置文件中缺少 vehicle_types 配置")

        self.pages_by_type = {}
        for type_id, type_config in vehicle_types.items():
            pages = type_config.get("pages", 0)
            self.pages_by_type[int(type_id)] = pages

        # 加载定时任务配置（从 schedule 配置中读取）
        # 默认使用早上8点到下午6点之间
        schedule = config.get("schedule", {})
        times = schedule.get("times", ["08:00", "18:00"])

        if len(times) >= 2:
            start_time = self._parse_time(times[0])
            end_time = self._parse_time(times[1])
        else:
            start_time = (8, 0)
            end_time = (18, 0)

        self.window = TimeWindow(start_time, end_time)

    def _parse_time(self, time_str: str) -> Tuple[int, int]:
        """解析时间字符串为 (hour, minute)"""
        try:
            parts = time_str.split(":")
            return int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            return 8, 0  # 默认早上8点

    def _check_database(self):
        """启动时检查数据库连接和代理可用性"""
        try:
            from carinfo.core.proxy import check_db_connection

            success, message = check_db_connection()

            if success:
                self._log(f"✓ {message}")
            else:
                self._log(f"✗ {message}", level="ERROR")
                self._log(
                    "数据库不可用，爬取任务将无法使用代理，请尽快修复", level="WARNING"
                )
        except ImportError as e:
            self._log(f"✗ 无法导入 carinfo.core.proxy 模块: {e}", level="ERROR")
        except Exception as e:
            self._log(f"✗ 数据库健康检查异常: {e}", level="ERROR")

    def _handle_signal(self, *_):
        self._log(
            "收到停止信号，准备退出（若正在执行任务，将在本轮结束后退出）",
            level="WARNING",
        )
        self._running = False

    def _log(self, msg: str, level: str = "INFO"):
        ts = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
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

    def _window_bounds(
        self, day: datetime, window: TimeWindow
    ) -> Tuple[datetime, datetime]:
        start = day.replace(
            hour=window.start_hm[0], minute=window.start_hm[1], second=0, microsecond=0
        )
        end = day.replace(
            hour=window.end_hm[0], minute=window.end_hm[1], second=0, microsecond=0
        )
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

    def _ensure_next_time(self) -> datetime:
        """确保下次执行时间（确保同一天不重复执行）"""
        state = self._load_state()
        now = now_beijing()
        today_str = now.strftime("%Y-%m-%d")

        def parse_dt(v: str) -> Optional[datetime]:
            try:
                return datetime.fromisoformat(v)
            except Exception:
                return None

        # 检查今天是否已经执行过
        last_run_date = state.get("last_run_date", "")
        if last_run_date == today_str:
            # 今天已执行，安排明天的随机时间
            tomorrow = now + timedelta(days=1)
            next_run = self._pick_random_time(tomorrow, self.window)
            self._log(f"今天({today_str})已执行过任务，安排明天执行", level="INFO")
        else:
            # 今天还未执行，检查当前是否有待执行的时间
            next_run = parse_dt(state.get("next_run", ""))
            if not next_run or next_run <= now:
                next_run = self._pick_random_time(now, self.window)

        state["next_run"] = next_run.isoformat(timespec="seconds")
        self._save_state(state)

        return next_run

    def _reschedule_after_skip(self) -> None:
        now = now_beijing()
        state = self._load_state()
        # 跳过时也检查是否今天已执行
        today_str = now.strftime("%Y-%m-%d")
        last_run_date = state.get("last_run_date", "")
        if last_run_date == today_str:
            # 今天已执行，安排明天
            tomorrow = now + timedelta(days=1)
            next_run = self._pick_random_time(tomorrow, self.window)
        else:
            next_run = self._pick_random_time(now, self.window)
        state["next_run"] = next_run.isoformat(timespec="seconds")
        self._save_state(state)

    def _run_one_job(self) -> None:
        if not self.lock.acquire():
            self._log(
                "检测到已有任务在运行（lock存在），本次不启动新任务", level="WARNING"
            )
            return

        start = time.time()
        self._log("=== 开始执行任务（从配置文件读取爬取配置）===", level="INFO")

        try:
            if os.getcwd() not in sys.path:
                sys.path.insert(0, os.getcwd())

            from carinfo.sites import car28 as carinfo

            csv_dir = "data/csv"
            os.makedirs(csv_dir, exist_ok=True)

            total = 0
            for t in (1, 2, 3, 4, 5):
                pages = self.pages_by_type.get(t, 0)
                if pages <= 0:
                    self._log(f"跳过类型{t}（pages=0）", level="INFO")
                    continue

                csv = os.path.join(csv_dir, f"car_data_{t}.csv")
                self._log(f"开始类型{t}，页数={pages}", level="INFO")
                total += carinfo.scrape_vehicle_type(t, pages, csv, start_page=1)

            if total == 0:
                self._log(
                    "没有需要爬取的车辆类型（所有类型pages都为0）", level="WARNING"
                )

            self._log(f"爬取结束，累计抓取 {total} 条，准备入库...", level="INFO")
            carinfo.auto_import_to_database()

            cost = time.time() - start
            self._log(f"=== 任务完成，耗时 {cost / 60:.1f} 分钟 ===", level="INFO")

            # 记录本次执行日期，防止同一天重复执行
            state = self._load_state()
            state["last_run_date"] = now_beijing().strftime("%Y-%m-%d")
            self._save_state(state)

        except Exception as e:
            self._log(f"任务执行异常: {e}", level="ERROR")
            import traceback

            traceback.print_exc()

        finally:
            self.lock.release()

    def run_forever(self) -> None:
        # 启动时检查今天是否已执行
        state = self._load_state()
        now = now_beijing()
        today_str = now.strftime("%Y-%m-%d")
        last_run_date = state.get("last_run_date", "")

        if last_run_date == today_str:
            self._log(
                f"今天({today_str})已执行过任务，启动后等待下一周期", level="INFO"
            )
        else:
            self._log("启动时检测今天未执行，立即执行任务", level="INFO")
            self._run_one_job()

        start_h, start_m = self.window.start_hm
        end_h, end_m = self.window.end_hm
        self._log(
            f"进入调度循环：每天 {start_h:02d}:{start_m:02d}-{end_h:02d}:{end_m:02d} 之间随机执行一次",
            level="INFO",
        )

        while self._running:
            next_run = self._ensure_next_time()
            now = now_beijing()

            wait_s = max(1, int((next_run - now).total_seconds()))
            self._log(
                f"下一次执行时间: {next_run.strftime('%Y-%m-%d %H:%M:%S')}，等待 {wait_s} 秒",
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
                self._log(
                    "到达触发时间，但上一次任务仍在运行，跳过并顺延重排",
                    level="WARNING",
                )
                self._reschedule_after_skip()
                continue

            self._run_one_job()

        self._log("服务已退出", level="INFO")
