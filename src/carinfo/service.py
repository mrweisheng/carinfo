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
    """一个「允许执行」的时间段（含起止，北京时间，同一天内）。

    `name` 只用于日志（上午/下午/晚上），不参与逻辑。
    """

    start_hm: Tuple[int, int]
    end_hm: Tuple[int, int]
    name: str = "窗口"

    def bounds(self, day: datetime) -> Tuple[datetime, datetime]:
        """把窗口投影到 `day` 这一天上，返回 (起, 止)。"""
        start = day.replace(
            hour=self.start_hm[0], minute=self.start_hm[1], second=0, microsecond=0
        )
        end = day.replace(
            hour=self.end_hm[0], minute=self.end_hm[1], second=0, microsecond=0
        )
        return start, end

    def label(self) -> str:
        return f"{self.name}({self.start_hm[0]:02d}:{self.start_hm[1]:02d}-{self.end_hm[0]:02d}:{self.end_hm[1]:02d})"


# 兜底调度方案：schedule 段缺失/非法时使用。
# **只在显式告警后使用** —— 项目约定「配置不许静默失效」，见 _load_config()。
DEFAULT_WINDOWS: Tuple[TimeWindow, ...] = (
    TimeWindow((8, 0), (12, 0), "上午"),
    TimeWindow((12, 0), (18, 0), "下午"),
    TimeWindow((18, 0), (22, 0), "晚上"),
)


class FileLock:
    """
    简单的跨进程互斥锁：
    - 通过 O_EXCL 创建 lock 文件（原子）
    - 文件内写 pid + 时间戳
    - 若锁文件存在且过旧（stale），允许自动清理
    """

    def __init__(self, path: str, stale_after: timedelta = timedelta(hours=2)):
        """`stale_after` = 「多久没刷新就认为持有者已死」。

        **有了 `touch()` 之后，这个阈值只跟单页耗时挂钩**（约 1 分钟），不再跟整轮
        时长（约 12 小时）挂钩 —— service 每页开头都会 `lock.touch()` 一次，活的
        进程会持续证明自己存在。取 2 小时 = 单页耗时的百倍余量，既能容忍偶发的
        慢页/长退避，又能在进程真崩了之后较快回收锁。

        ⚠️ 若把 `touch()` 的调用删掉（或 page_hook 没接上），阈值必须重新调回
        > 单轮最坏耗时（12 小时），否则跨夜时会并发双跑。
        """
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

    def is_locked(self) -> bool:
        """锁文件存在且未过期（即：有另一个实例正在执行任务）。"""
        return os.path.exists(self.path) and not self._is_stale()

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

    def touch(self) -> None:
        """刷新锁文件的时间戳，证明持有者仍活着。

        长跑任务（单轮约 12 小时）必须在过程中反复调用，否则 `_is_stale()` 会把
        仍在运行的锁判成过期、被别的进程清掉。写完再 rename 保证原子上限：
        读方要么看到旧的完整内容，要么看到新的完整内容，不会读到半截 JSON。
        """
        if not self._held:
            return
        try:
            payload = {
                "pid": os.getpid(),
                "created_at": now_beijing().isoformat(timespec="seconds"),
            }
            tmp = f"{self.path}.tmp.{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, self.path)   # 同目录 rename，POSIX/Windows 都原子
        except Exception:
            # touch 失败不该拖垮爬取 —— 最坏情况退回到「靠 stale_after 兜底」
            pass

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

        # schedule 段的告警先攒起来。**不能在这里直接 print** —— CLI 是构造完对象
        # 才调 run_forever()，而本函数在 run_forever() 之前就已经跑完了。攒进列表
        # 由 run_forever() 开头统一 flush，确保运维在服务启动时（而不是任务真正
        # 触发时）就能看到配置问题。
        self._startup_warnings: list[str] = []

        # 从配置文件加载时间和页数配置
        self._load_config()

        # 启动时检查数据库连接
        self._check_database()

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _warn(self, msg: str) -> None:
        """记录一条「启动时必须让人看见」的告警（真正打印推迟到 run_forever()）。"""
        self._startup_warnings.append(msg)

    def _flush_startup_warnings(self) -> None:
        for msg in self._startup_warnings:
            self._log(msg, level="WARNING")
        self._startup_warnings.clear()

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

        # 加载调度配置
        self.windows = self._load_windows(config)

    # ------------------------------------------------------------------
    # 调度窗口解析
    # ------------------------------------------------------------------
    def _load_windows(self, config: dict) -> Tuple[TimeWindow, ...]:
        """解析 `schedule.windows`，返回按开始时间升序的窗口元组。

        配置形态（`schedule` 段，整段可选）：

            "schedule": {
              "windows": [
                {"name": "上午", "start": "08:00", "end": "12:00"},
                {"name": "下午", "start": "12:00", "end": "18:00"},
                {"name": "晚上", "start": "18:00", "end": "22:00"}
              ]
            }

        语义：**每天在其中一个窗口里随机挑一个时刻执行一次**（不是每个窗口都执行）。

        为什么不兼容旧的 `schedule.times`：旧格式 `["08:00","12:00","16:00","20:00"]`
        看着像「四个候选时刻」，代码却只读前两个当「窗口起止」—— 后两个被**静默丢弃**，
        版本之间还换过含义。这种「配置写了但没生效还不报错」是运维事故的温床，因此
        本轮直接换字段名，旧键出现时**显式报错**，逼配置方确认迁移。
        """
        schedule = config.get("schedule", {})
        if not isinstance(schedule, dict):
            self._warn(
                f"config.json 的 schedule 段不是对象（实际 {type(schedule).__name__}），"
                f"已退回默认窗口：{self._windows_label(DEFAULT_WINDOWS)}"
            )
            return DEFAULT_WINDOWS

        if "times" in schedule:
            raise ValueError(
                "config.json 的 schedule.times 已废弃（旧格式只读前两个值，其余会被静默丢弃）。"
                "请改用 schedule.windows，例如："
                '{"windows": [{"name": "上午", "start": "08:00", "end": "12:00"}, '
                '{"name": "下午", "start": "12:00", "end": "18:00"}, '
                '{"name": "晚上", "start": "18:00", "end": "22:00"}]}'
            )

        raw = schedule.get("windows")
        if raw is None:
            self._warn(
                "config.json 缺少 schedule.windows，已退回默认窗口："
                f"{self._windows_label(DEFAULT_WINDOWS)}（建议显式写进 config.json）"
            )
            return DEFAULT_WINDOWS

        if not isinstance(raw, list) or not raw:
            self._warn(
                f"config.json 的 schedule.windows 不是非空数组（实际 {raw!r}），"
                f"已退回默认窗口：{self._windows_label(DEFAULT_WINDOWS)}"
            )
            return DEFAULT_WINDOWS

        windows = []
        for i, item in enumerate(raw):
            win = self._parse_window(item, i)
            if win is not None:
                windows.append(win)

        if not windows:
            self._warn(
                f"config.json 的 schedule.windows 里没有一条合法配置，"
                f"已退回默认窗口：{self._windows_label(DEFAULT_WINDOWS)}"
            )
            return DEFAULT_WINDOWS

        # 按开始时间排序，保证「先出现的一天先被安排」的直觉一致（纯可读性，不影响正确性）
        windows.sort(key=lambda w: w.start_hm)

        # 逐条校验窗口本身合法（起 < 止）
        for w in windows:
            if w.start_hm >= w.end_hm:
                raise ValueError(
                    f"config.json 的 schedule.windows[{w.name}] 起止时间非法："
                    f"({w.start_hm[0]:02d}:{w.start_hm[1]:02d} → "
                    f"{w.end_hm[0]:02d}:{w.end_hm[1]:02d})，要求 start < end"
                )

        # 窗口重叠 = 配置意图不清（重叠区里随机选哪个窗口？），但**不算错误**：
        # 随机选窗口的语义下重叠只是让重叠区被选中的概率变高，结果仍然自洽。
        # 所以只告警，不改行为。
        for a, b in zip(windows, windows[1:]):
            if b.start_hm < a.end_hm:
                self._warn(
                    f"config.json 的 schedule.windows 存在重叠：{a.label()} 与 {b.label()}；"
                    "重叠时段被选中执行的概率会偏高，建议改成互不重叠"
                )

        return tuple(windows)

    def _parse_window(self, item, index: int) -> Optional[TimeWindow]:
        """解析单条窗口配置，非法则告警并返回 None（跳过而不是整段崩）。"""
        if not isinstance(item, dict):
            # 容忍 ["08:00-12:00"] 这种紧凑写法：退化成「起止对」列表
            if isinstance(item, (list, tuple)) and len(item) == 2:
                item = {"start": item[0], "end": item[1]}
            else:
                self._warn(
                    f"schedule.windows[{index}] 不是对象（或二元数组），实际 {item!r}，已跳过"
                )
                return None

        start_raw = item.get("start")
        end_raw = item.get("end")
        if start_raw is None or end_raw is None:
            self._warn(
                f"schedule.windows[{index}] 缺少 start/end 字段（实际 {item!r}），已跳过"
            )
            return None

        start_hm = self._parse_hm(start_raw, f"schedule.windows[{index}].start")
        end_hm = self._parse_hm(end_raw, f"schedule.windows[{index}].end")
        if start_hm is None or end_hm is None:
            return None

        name = str(item.get("name") or f"窗口{index + 1}")
        return TimeWindow(start_hm, end_hm, name)

    def _parse_hm(self, value, where: str) -> Optional[Tuple[int, int]]:
        """解析 "HH:MM" / [H, M] / 单个整数小时 → (hour, minute)。

        **非法值返回 None 并告警**，不再像旧版 `_parse_time()` 那样静默返回 8:00
        —— 把 "8:00-18:00" 这种写错的字符串悄悄当成 8 点，人根本发现不了。
        """
        if isinstance(value, (list, tuple)) and len(value) == 2:
            try:
                h, m = int(value[0]), int(value[1])
            except (TypeError, ValueError):
                self._warn(f"{where} 无法解析为时间（实际 {value!r}）")
                return None
            return (h, m) if 0 <= h <= 23 and 0 <= m <= 59 else (
                self._warn(f"{where} 超范围（实际 {value!r}，要求 00:00-23:59）") or None
            )

        if isinstance(value, int) and not isinstance(value, bool):
            return (value, 0) if 0 <= value <= 23 else (
                self._warn(f"{where} 小时超范围（实际 {value!r}）") or None
            )

        if isinstance(value, str):
            parts = value.strip().replace("：", ":").split(":")
            if len(parts) == 1 and parts[0].isdigit():
                parts = [parts[0], "0"]
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                h, m = int(parts[0]), int(parts[1])
                if 0 <= h <= 23 and 0 <= m <= 59:
                    return (h, m)
                self._warn(f"{where} 超范围（实际 {value!r}，要求 00:00-23:59）")
                return None

        self._warn(f"{where} 无法解析为 HH:MM（实际 {value!r}）")
        return None

    @staticmethod
    def _windows_label(windows: Tuple[TimeWindow, ...]) -> str:
        return "、".join(w.label() for w in windows)

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

    def _pick_random_time(
        self, now: datetime, windows: Optional[Tuple[TimeWindow, ...]] = None
    ) -> datetime:
        """挑一个「未来最近一次」的执行时刻。

        语义（三段窗口 "各随机一次" 的实现）：**在同一个自然日里随机挑一个窗口，
        再在该窗口内随机挑一个时刻**；若今天就只剩得下部分窗口可用，则只在可用的
        那几个里挑。今天所有窗口都已过 → 顺延到明天，在明天全部窗口里挑。

        ⚠️ 关键性质：**返回的时刻严格大于 `now`**。`_ensure_next_time()` 每轮都会
        调本函数、并把它写进 `next_run`，若可能返回过去时刻，`run_forever()` 就会
        拿到负的等待秒数 → `max(1, ...)` 兜成 1 秒 → **忙循环疯狂重排**。
        """
        wins = windows or self.windows
        now = self._ensure_beijing(now)

        for base_day in (now, now + timedelta(days=1)):
            candidates = []
            for w in wins:
                start, end = w.bounds(base_day)
                if end <= now:
                    continue                    # 该窗口今天已整个过去
                candidates.append((max(start, now + timedelta(minutes=1)), end, w))

            if not candidates:
                continue                        # 今天没窗口了，看明天

            # 允许的当前时刻：同一天内第一次调用能落进两个窗口的重叠区时，重叠窗口
            # 各被选中的概率相同 —— 这是「随机挑窗口」的正常后果，不是 bug。
            # 但「今天只剩部分窗口」和「整天全可用」的权重必须公平：先随机挑窗口，
            # 再从该窗口的可用区间里随机挑时刻，而不是把所有区间拼成一条线抽签
            # （拼成一条线会让长窗口垄断抽签，短窗口几乎抽不到）。
            _, end, win = random.choice(candidates)
            start, end = win.bounds(base_day)
            start = max(start, now + timedelta(minutes=1))

            seconds = int((end - start).total_seconds())
            if seconds <= 0:
                continue                        # 退化窗口（起=止且已到点），换明天的

            return start + timedelta(seconds=random.randint(0, seconds))

        # 理论上不可达：明天必然有至少一个 future 窗口（只要所有窗口 end > start，
        # 这一点已由 _load_windows() 的校验保证）。真到了这里说明窗口配置全烂。
        self._log("所有窗口均无法安排未来时刻，退回 1 小时后执行", level="ERROR")
        return now + timedelta(hours=1)

    @staticmethod
    def _ensure_beijing(dt: datetime) -> datetime:
        """把 naive datetime 视作北京时间；已带时区的原样返回。"""
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=BEIJING_TZ)

    def _plan_next_run(self, now: datetime, from_tomorrow: bool = False) -> datetime:
        """计算下一个执行时刻并写进状态文件。

        - `from_tomorrow=True`：今天已经跑过，直接在明天的窗口里随机
        - `from_tomorrow=False`：今天还没跑，从「此刻之后」的剩余窗口里随机
        """
        now = self._ensure_beijing(now)
        state = self._load_state()

        if from_tomorrow:
            target = now + timedelta(days=1)
            next_run = self._pick_random_time(target)
        else:
            next_run = self._pick_random_time(now)

        state["next_run"] = next_run.isoformat(timespec="seconds")
        state["next_run_window"] = self._describe_window(next_run)
        self._save_state(state)
        return next_run

    def _describe_window(self, moment: datetime) -> str:
        """反查某个时刻落在哪个窗口里，纯为日志/状态文件可读性。"""
        for w in self.windows:
            start, end = w.bounds(moment)
            if start <= moment <= end:
                return w.name
        return ""

    def _ensure_next_time(self) -> datetime:
        """确保下次执行时间（确保同一天不重复执行）。"""
        state = self._load_state()
        now = now_beijing()
        today_str = now.strftime("%Y-%m-%d")

        def parse_dt(v: str) -> Optional[datetime]:
            try:
                return self._ensure_beijing(datetime.fromisoformat(v))
            except Exception:
                return None

        # 今天已经跑过 → 直接在明天窗口里重排
        if state.get("last_run_date", "") == today_str:
            self._log(f"今天({today_str})已执行过任务，安排明天执行", level="INFO")
            return self._plan_next_run(now, from_tomorrow=True)

        # 今天还没跑：沿用状态文件里的 next_run，但必须校验它仍然「在未来」
        next_run = parse_dt(state.get("next_run", ""))
        if not next_run or next_run <= now:
            if next_run:
                self._log(
                    f"状态文件里的 next_run({next_run.strftime('%Y-%m-%d %H:%M:%S')}) "
                    f"已过期，重新安排",
                    level="INFO",
                )
            next_run = self._pick_random_time(now)
            state["next_run"] = next_run.isoformat(timespec="seconds")
            state["next_run_window"] = self._describe_window(next_run)
            self._save_state(state)

        return next_run

    def _reschedule_after_skip(self) -> None:
        """到达触发点但上一轮还在跑 —— 顺延重排，不在同一个窗口里死等。"""
        now = now_beijing()
        state = self._load_state()
        last_run_date = state.get("last_run_date", "")

        if last_run_date == now.strftime("%Y-%m-%d"):
            self._log("今天已完成过任务，跳过并顺延到明天", level="WARNING")
            self._plan_next_run(now, from_tomorrow=True)
        else:
            self._log("今天尚未完成，跳过并顺延到今日剩余窗口", level="WARNING")
            self._plan_next_run(now, from_tomorrow=False)

    def _run_one_job(self) -> None:
        if not self.lock.acquire():
            self._log(
                "检测到已有任务在运行（lock存在），本次不启动新任务", level="WARNING"
            )
            return

        start = time.time()
        self._log("=== 开始执行任务（爬取配置从 config.json 读取，逐页直接入库）===", level="INFO")

        try:
            if os.getcwd() not in sys.path:
                sys.path.insert(0, os.getcwd())

            from carinfo.sites import car28 as carinfo
            from carinfo.core.importer import FastCSVImporter

            csv_dir = "data/csv"
            os.makedirs(csv_dir, exist_ok=True)

            db_importer = FastCSVImporter()
            total = 0
            try:
                for t, pages in sorted(self.pages_by_type.items()):
                    if pages <= 0:
                        self._log(f"跳过类型{t}（pages=0）", level="INFO")
                        continue

                    csv = os.path.join(csv_dir, f"car_data_{t}.csv")
                    self._log(f"开始类型{t}，页数={pages}", level="INFO")
                    stats = carinfo.scrape_vehicle_type(
                        t, pages, csv, start_page=1, db_importer=db_importer,
                        # 每页刷新运行锁：单轮 12 小时 > 任何合理 stale 阈值，
                        # 靠固定阈值会在中途失效（见 FileLock.touch 的说明）
                        page_hook=self.lock.touch,
                    )
                    total += stats['total_vehicles']
                    self._log(
                        f"类型{t}完成: 爬取{stats['total_vehicles']}条，"
                        f"新增{stats['new_vehicles']}，更新{stats['updated_vehicles']}，"
                        f"错误{stats['error_count']}，状态{stats['status']}",
                        level="INFO",
                    )
            finally:
                db_importer.close()

            if total == 0:
                self._log("没有需要爬取的车辆类型或未抓到数据", level="WARNING")

            cost = time.time() - start
            self._log(f"=== 任务完成，累计抓取 {total} 条，耗时 {cost / 60:.1f} 分钟 ===", level="INFO")

            # 记录本次执行日期，防止同一天重复执行。
            # ⚠️ **这条标记是「今天已完成」的唯一事实来源**：写在状态文件里，
            # 服务重启/机器重启都读它 → 当天不会再跑第二次。因此它的写入时机
            # 只能在「本轮全部类型都爬完」之后，不能提前到开头。
            state = self._load_state()
            state["last_run_date"] = now_beijing().strftime("%Y-%m-%d")
            state["last_run_at"] = now_beijing().isoformat(timespec="seconds")
            self._save_state(state)
            self._log(
                f"已在 {self.state_file} 标记今天({state['last_run_date']})已完成，"
                f"后续重启不会再执行本日任务",
                level="INFO",
            )

        except Exception as e:
            self._log(f"任务执行异常: {e}", level="ERROR")
            import traceback

            traceback.print_exc()

        finally:
            self.lock.release()

    def run_forever(self) -> None:
        # 把构造期攒下的配置告警打出来 —— 必须在任何调度动作之前，
        # 这样运维在 systemd 启动的日志里就能看到「窗口配置没生效」。
        self._flush_startup_warnings()

        # 启动时检查今天是否已执行
        state = self._load_state()
        now = now_beijing()
        today_str = now.strftime("%Y-%m-%d")
        last_run_date = state.get("last_run_date", "")

        self._log(
            f"调度窗口：{self._windows_label(self.windows)}"
            f"（每天随机挑一个窗口、窗口内随机一个时刻，只执行一次）",
            level="INFO",
        )

        if last_run_date == today_str:
            self._log(
                f"今天({today_str})已执行过任务，启动后等待下一周期，不重复执行", level="INFO"
            )
        else:
            if last_run_date:
                self._log(
                    f"上次执行日期为 {last_run_date}，今天({today_str})尚未执行",
                    level="INFO",
                )
            else:
                self._log("状态文件中无执行记录，视为今天尚未执行", level="INFO")
            self._log("启动时检测今天未执行，立即执行任务", level="INFO")
            self._run_one_job()
            # 刚跑完（无论成功失败）立刻重排下次时间：成功的会排到明天，
            # 失败的今天还有机会（last_run_date 未写），避免忙循环。
            self._plan_next_run(now_beijing(), from_tomorrow=False)

        while self._running:
            next_run = self._ensure_next_time()
            now = now_beijing()

            wait_s = max(1, int((next_run - now).total_seconds()))
            self._log(
                f"下一次执行时间: {next_run.strftime('%Y-%m-%d %H:%M:%S')}"
                f"（{self._describe_window(next_run) or '窗口外'}），等待 {wait_s} 秒",
                level="INFO",
            )

            slept = 0
            step = 5
            while self._running and slept < wait_s:
                chunk = min(step, wait_s - slept)
                time.sleep(chunk)
                slept += chunk

            if not self._running:
                break

            if self.lock.is_locked():
                self._log(
                    "到达触发时间，但上一次任务仍在运行，跳过并顺延重排",
                    level="WARNING",
                )
                self._reschedule_after_skip()
                continue

            self._run_one_job()

        self._log("服务已退出", level="INFO")
