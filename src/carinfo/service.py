import json
import os
import random
import signal
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
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

        # 深扫（周期性全量）配置：scraping.deep_crawl，整段可选。
        # 深扫 = 每 interval_days 天把每类爬到 pages 页（替代当天的日常小批量），
        # 目的有二：补足日常覆盖不到的深处个人车源 + 刷新长期没验证的存量
        # （僵尸清理：库里「在售」但实际已售的行只有重爬才能改状态）。
        # 依据：2026-09-25 页数策略调研（1200 页覆盖约 90% 在售，之后边际收益骤降；
        # 车行霸占前排持续刷新，个人车沉底，深处恰是个人车目标池）。
        deep_cfg = config.get("scraping", {}).get("deep_crawl", {})
        if not isinstance(deep_cfg, dict):
            self._warn(
                f"scraping.deep_crawl 不是对象（实际 {type(deep_cfg).__name__}），"
                "深扫已禁用，只按日常 pages 调度"
            )
            deep_cfg = {}
        self.deep_pages, pages_ok = self._coerce_positive(deep_cfg.get("pages"), 1200)
        self.deep_interval_days, days_ok = self._coerce_positive(
            deep_cfg.get("interval_days"), 30
        )
        self.deep_request_interval, interval_ok = self._coerce_interval(
            deep_cfg.get("request_interval"), [1.0, 1.5]
        )
        # 配置不许静默失效：键存在但值非法时，用了默认值必须让人看见
        if not pages_ok:
            self._warn(f"scraping.deep_crawl.pages 非法（{deep_cfg.get('pages')!r}），"
                       f"已用默认 {self.deep_pages}")
        if not days_ok:
            self._warn(f"scraping.deep_crawl.interval_days 非法"
                       f"（{deep_cfg.get('interval_days')!r}），已用默认 {self.deep_interval_days}")
        if not interval_ok:
            self._warn(f"scraping.deep_crawl.request_interval 非法"
                       f"（{deep_cfg.get('request_interval')!r}），已用默认 "
                       f"{self.deep_request_interval}")
        if self.deep_pages <= 0:
            self._warn("scraping.deep_crawl.pages<=0，深扫已禁用，只按日常 pages 调度")

        # 车源复核配置：scraping.revalidation，**整段可选，缺省=关闭**。
        # 为什么默认关闭：复核不是只读操作 —— 它会把「久未核实且已售/已删」的车
        # 移出检索（status 2/3）。能一键停掉是运维底线。
        # 「多久算久未核实」刻意**不在这里配**：检索侧的标签用的是
        # revalidator.VERIFY_WINDOW_DAYS，两处各配一个值必然漂移
        # （标签说久未核实、复核却不来查它）。单点定义，见 core/revalidator.py。
        reval_cfg = config.get("scraping", {}).get("revalidation", {})
        if not isinstance(reval_cfg, dict):
            self._warn(
                f"scraping.revalidation 不是对象（实际 {type(reval_cfg).__name__}），"
                "车源复核已关闭"
            )
            reval_cfg = {}
        self.reval_enabled = bool(reval_cfg.get("enabled", False))
        self.reval_interval_days, rd_ok = self._coerce_positive(
            reval_cfg.get("interval_days"), 7)
        self.reval_batch_size, rb_ok = self._coerce_positive(
            reval_cfg.get("batch_size"), 3000)
        self.reval_concurrency, rc_ok = self._coerce_positive(
            reval_cfg.get("concurrency"), 4)
        self.reval_vehicle_type, rv_ok = self._coerce_positive(
            reval_cfg.get("vehicle_type"), 1)
        self.reval_request_interval, ri_ok = self._coerce_interval(
            reval_cfg.get("request_interval"), [1.0, 1.5])
        if self.reval_enabled:
            for ok, name, val in (
                (rd_ok, "interval_days", self.reval_interval_days),
                (rb_ok, "batch_size", self.reval_batch_size),
                (rc_ok, "concurrency", self.reval_concurrency),
                (rv_ok, "vehicle_type", self.reval_vehicle_type),
                (ri_ok, "request_interval", self.reval_request_interval),
            ):
                if not ok:
                    self._warn(f"scraping.revalidation.{name} 非法，已用默认 {val!r}")

        # 加载调度配置
        self.windows = self._load_windows(config)

    @staticmethod
    def _coerce_positive(value, default: int) -> tuple:
        """非负整数收敛：非法/负值退默认。返回 (值, 是否原值合法)——非法由调用方告警。"""
        if value is None:
            return default, True          # 键缺失 = 用默认，不算配置错误
        try:
            n = int(value)
        except (TypeError, ValueError):
            return default, False
        return (n, True) if n >= 0 else (default, False)

    @staticmethod
    def _coerce_interval(value, default) -> tuple:
        """发送间隔收敛：[min,max] 二元列表。返回 (值, 是否原值合法)。"""
        if value is None:
            return default, True
        try:
            lo, hi = float(value[0]), float(value[1])
            if 0 < lo <= hi:
                return [lo, hi], True
        except (TypeError, ValueError, IndexError):
            pass
        return default, False

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
        self, now: datetime, windows: Optional[Tuple[TimeWindow, ...]] = None,
        start_day: Optional[datetime] = None,
    ) -> datetime:
        """挑一个「未来最近一次」的执行时刻。

        语义（三段窗口 "各随机一次" 的实现）：**在同一个自然日里随机挑一个窗口，
        再在该窗口内随机挑一个时刻**；若今天就只剩得下部分窗口可用，则只在可用的
        那几个里挑。今天所有窗口都已过 → 顺延到明天，在明天全部窗口里挑。

        `start_day`：候选的**第一天**（默认 now 所在日）。「从明天起排」的调用方
        （_plan_next_run(from_tomorrow=True)）传明天的时刻。⚠️ 不能靠把 `now`
        抬一天来模拟「明天」——本函数把入参当**真实当前时间**做 `end <= now`
        过滤，抬一天会把目标日的窗口全部过滤掉、顺延到后天：2026-09-25 22:45
        重启把任务推到 09-27 的事故就是这个写法（22:00 后重启必触发）。

        ⚠️ 关键性质：**返回的时刻严格大于 `now`**。`_ensure_next_time()` 每轮都会
        调本函数、并把它写进 `next_run`，若可能返回过去时刻，`run_forever()` 就会
        拿到负的等待秒数 → `max(1, ...)` 兜成 1 秒 → **忙循环疯狂重排**。
        """
        wins = windows or self.windows
        now = self._ensure_beijing(now)
        first_day = self._ensure_beijing(start_day) if start_day is not None else now

        for base_day in (first_day, first_day + timedelta(days=1)):
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

        - `from_tomorrow=True`：今天已经跑过，在**明天的全部窗口**里随机
          （不能只限「明天此刻之后」——22:00 后重启时明天的窗口会全被
          `end <= now` 过滤掉，任务被推到后天，2026-09-25 事故）
        - `from_tomorrow=False`：今天还没跑，从「此刻之后」的剩余窗口里随机
        """
        now = self._ensure_beijing(now)
        state = self._load_state()

        if from_tomorrow:
            next_run = self._pick_random_time(now, start_day=now + timedelta(days=1))
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

    def _resolve_job_mode(self, state: dict) -> dict:
        """决定本轮跑「深扫」/「车源复核」/「日常」中的哪一种，以及深扫的续跑状态。

        优先级：**未完成的深扫续跑 > 到期的新深扫 > 到期/首次的车源复核 > 日常**。
        续跑优先保证被反爬/重启打断的深扫最终能完成，期间其它模式暂停——深扫从
        第 1 页开始爬，天然覆盖日常 30 页的活跃区，不存在「深扫期间漏掉日常更新」。

        复核为什么排在日常之前：日常只是刷新前排 30 页，复核是**把已经死掉的车
        从检索里摘掉**（客户白跑一趟的代价比"少刷新一天"大得多）。
        复核为什么排在深扫之后：深扫把 1200 页全刷一遍 ≈ 顺带做了一轮大范围核实，
        先做它更划算，也不必再花一次请求去复查那些刚被刷到的车。

        到期判定：距上次完成 >= 各自 interval_days（从未跑过视为到期）。
        状态结构（.carinfo_service_state.json）：
            {"deep_crawl":   {"last_completed": "YYYY-MM-DD",
                              "progress": {"1": {"target": 1200, "last_page": 447}}},
             "revalidation": {"last_completed": "YYYY-MM-DD"}}
        """
        deep = state.get("deep_crawl") or {}
        progress = deep.get("progress") or {}
        deep_reason = "深扫已禁用"

        if self.deep_pages > 0:
            # 有未完成进度 → 续跑（哪怕间隔未到）
            for type_id, prog in progress.items():
                try:
                    if int(prog.get("last_page", 0)) < int(prog.get("target") or self.deep_pages):
                        return {"mode": "deep", "resume": True}
                except (TypeError, ValueError):
                    continue

            last_completed = deep.get("last_completed") or ""
            if not last_completed:
                return {"mode": "deep", "resume": False, "reason": "从未深扫"}
            try:
                # 用北京时间算间隔：服务器 OS 时区若非 +8，date.today() 会有 8 小时偏差
                elapsed = (now_beijing().date() - date.fromisoformat(last_completed)).days
            except ValueError:
                return {"mode": "deep", "resume": False, "reason": "last_completed 非法，视为到期"}
            if elapsed >= self.deep_interval_days:
                return {"mode": "deep", "resume": False, "reason": f"距上次深扫 {elapsed} 天"}
            deep_reason = f"距上次深扫仅 {elapsed} 天"

        # 深扫本轮不需要跑，才轮到车源复核（开关关闭时整段跳过）
        if self.reval_enabled:
            reval = state.get("revalidation") or {}
            last_reval = reval.get("last_completed") or ""
            if not last_reval:
                return {"mode": "revalidate", "reason": "从未复核"}
            try:
                r_days = (now_beijing().date() - date.fromisoformat(last_reval)).days
            except ValueError:
                return {"mode": "revalidate", "reason": "复核 last_completed 非法，视为到期"}
            if r_days >= self.reval_interval_days:
                return {"mode": "revalidate", "reason": f"距上次复核 {r_days} 天"}

        return {"mode": "daily", "reason": deep_reason}

    def _save_deep_progress(self, type_id: int, last_page: int) -> None:
        """记录深扫进度（每页开始时由 page_hook 调用，last_page=本页之前已完成页）。"""
        state = self._load_state()
        deep = state.get("deep_crawl") or {}
        progress = deep.get("progress") or {}
        progress[str(type_id)] = {"target": self.deep_pages, "last_page": int(last_page)}
        deep["progress"] = progress
        state["deep_crawl"] = deep
        self._save_state(state)

    def _mark_deep_completed(self) -> None:
        state = self._load_state()
        state["deep_crawl"] = {
            "last_completed": now_beijing().strftime("%Y-%m-%d"),
            "progress": {},
        }
        self._save_state(state)

    def _mark_reval_completed(self) -> None:
        """记录复核完成日期（决定下次复核何时触发）。"""
        state = self._load_state()
        state["revalidation"] = {
            "last_completed": now_beijing().strftime("%Y-%m-%d"),
        }
        self._save_state(state)

    def _run_revalidation_job(self) -> str:
        """车源复核：按 h_vid 复查「久未核实」的在售车，判三态并落库。

        与爬取的三点不同：
        1. **不按车辆类型循环** —— 候选是一条跨类型、按「最久没核实」排序的查询。
        2. **没有进度文件** —— 候选查询天然幂等：已处理的被写成今天，下一轮自动
           落在队尾。所以被中断/被 batch_size 截断都只是"下次接着跑"，不需要续跑状态。
           （这正是它比深扫省事的地方：深扫必须记 last_page，因为页码会漂。）
        3. **必须持续 touch 锁** —— 一批 3000 台约 1 小时，远超 stale_after(2h) 的
           安全余量；不 touch 就会踩已知问题 10（锁被判过期 → 并发起第二个进程）。

        ⚠️ 返回**状态**而不是计数（P0-1 修正）。此前返回
        ``alive + sold + deleted``，调用方只能看到"非零 = 成功"，于是
        「被反爬中止、一台都没处理」与「正常跑完」在调用方眼里完全一样 ——
        中止也会去写 ``last_completed``，**下一轮要等整整 interval_days(7 天)**
        才重来，而这恰恰是最该立刻重试的情况。计数照旧进日志与 crawl_logs。

        Returns:
            "success"  本轮正常结束（含「没有候选」）→ 可标记 last_completed
            "aborted"  连续 busy 触发保护性中止 → **不可**标记，下一轮继续
            "error"    连不上库等入口故障 → 不可标记
        """
        from carinfo.core import revalidator
        from carinfo.core.importer import FastCSVImporter, record_crawl_log

        importer = FastCSVImporter()
        if not importer.connect():
            self._log("复核：数据库连接失败，本轮跳过", level="ERROR")
            return "error"

        conn = importer.connection
        try:
            window = revalidator.VERIFY_WINDOW_DAYS
            cands = revalidator.fetch_candidates(
                conn, window_days=window,
                vehicle_type=self.reval_vehicle_type,
                limit=self.reval_batch_size,
            )
            if not cands:
                # 没有候选 = 全库都在窗口内核实过 = 本轮目的已达成 → 算成功
                self._log(f"复核：没有需要复核的车（{window} 天内都核实过）", level="INFO")
                return "success"

            interval = self.reval_request_interval
            self._log(
                f"复核候选 {len(cands)} 台（窗口 {window} 天，类型 "
                f"{self.reval_vehicle_type}，并发 {self.reval_concurrency}，"
                f"间隔 {interval[0]}-{interval[1]}s）",
                level="INFO",
            )

            vid_by_hvid = {h: vid for vid, h in cands}
            fetch = revalidator.make_spider_fetcher(
                self.reval_vehicle_type, tuple(interval), self.reval_concurrency,
            )

            # ── 判据漂移探针（只读，抽查 3+3 台）──
            # 三态判据是从线上抓样反推的：站点一改版，classify_detail 会**静默
            # 退化成「全判 UNKNOWN」**，复核从此一台不动，而报告只显示"判不出来"。
            # 离线单测只能证明代码没被改坏，证不了线上页面没变 —— 这是唯一的补位。
            # 放在候选非空之后：没车要复核时没必要花这 6 个请求。
            # 只告警、**不中止**：实测过的失效模式是"什么都不写"或"标签写错"，
            # 没有一条会导致误删（判不准一律 UNKNOWN）。真正危险的是没人知道它不准了。
            try:
                probe = revalidator.probe_drift(conn, fetch)
                msg = (f"判据探针：抽查 {probe['checked']} 台，漂移 {probe['drift']}，"
                       f"取不到页 {probe['unreachable']}")
                if probe["drift"]:
                    self._log(f"⚠ {msg} —— 三态判据可能已失效，复核结果可疑！",
                              level="ERROR")
                    for d in probe["details"]:
                        self._log(f"  ⚠ {d}", level="ERROR")
                else:
                    self._log(msg, level="INFO")
            except Exception as e:  # noqa: BLE001
                # 探针本身出问题绝不能挡住复核（它只是观测手段）
                self._log(f"判据探针异常（不影响复核）: {e}", level="WARNING")

            writer = revalidator.make_db_writer(conn, lambda h: vid_by_hvid.get(h))

            last_touch = {"t": time.time()}

            def progress(done: int, total: int) -> None:
                now = time.time()
                if done % 50 == 0 or now - last_touch["t"] > 120:
                    self.lock.touch()
                    last_touch["t"] = now
                    self._log(f"复核进度 {done}/{total}", level="INFO")

            report = revalidator.revalidate_ids(
                [h for _vid, h in cands], fetch,
                writer=writer, concurrency=self.reval_concurrency,
                on_progress=progress, dry_run=False,
            )
            self._log(report.summary(),
                      level="WARNING" if report.aborted else "INFO")

            # ── 写 crawl_logs（复核此前完全不在审计链里）──
            # 不写的话：复核改了上万台的 vehicle_status，而 crawl_logs 只有"爬取"的
            # 记录 —— 事后对账时这批变更**没有任何出处**，也会把「台/页」基线算歪
            # （pages_scraped=0 却带着几万台）。vehicle_type 用独立值，便于与爬取区分。
            record_crawl_log(importer, {
                "vehicle_type_name": "复核",
                "pages_scraped": 0,
                "total_vehicles": report.considered,
                "new_vehicles": 0,
                # 实际落库行数（含"仍在售→刷新 last_verified"），不是判定台数
                "updated_vehicles": report.wrote,
                "error_count": report.write_failed,
                "proxy_used_count": 0,
                "proxy_fail_count": 0,
                # 连续 busy 中止 = 站点反爬起效，落到这一列才看得见
                "anti_crawler_triggered": 1 if report.aborted else 0,
                "crawl_duration": round(report.elapsed_s, 2),
                "import_duration": 0.0,
                "status": "aborted" if report.aborted else "success",
                "error_details": (report.summary() if report.write_failed
                                  else None),
            })

            # ── 重算派生表（P1-3）──
            # market_stats / vehicle_features 都是从 vehicles 派生的：复核把车
            # 写成已售/已删之后，不重算的话行情基准里还混着死车、`is_unverified`
            # 标签也停在旧值（刚核实过的车当天仍显示「久未核实」）。
            # 爬取路径的重算在 _run_one_job 尾部（total>0 触发，2026-09-30 补回），
            # 复核路径在这里触发 —— 两条写库路径各有自己的重算触发点。
            # 条件用 wrote > 0：没写进任何行就是什么都没变，不必白跑 8 秒。
            # 位置放在 mark_completed 之前：重算途中挂掉 → last_completed 没写 →
            # 下次启动会重跑（复核是幂等的，成本只是再扫一遍候选）。
            if report.wrote > 0:
                self._rebuild_features(f"复核写入 {report.wrote} 行")

            if report.aborted:
                self._log(
                    "复核被反爬中止，**不**写 last_completed —— 下一轮会继续复核",
                    level="WARNING",
                )
                return "aborted"
            return "success"
        finally:
            importer.close()

    def _rebuild_features(self, reason: str) -> None:
        """重算 market_stats / vehicle_features（失败只告警，绝不影响主流程）。"""
        try:
            from carinfo.search.features import rebuild_default
        except Exception as e:  # noqa: BLE001
            self._log(f"派生表重算不可用（import 失败）: {e}", level="WARNING")
            return
        self._log(f"开始重算派生表（{reason}）...", level="INFO")
        ok, msg = rebuild_default()
        self._log(msg, level="INFO" if ok else "WARNING")


    def _run_one_job(self) -> None:
        if not self.lock.acquire():
            self._log(
                "检测到已有任务在运行（lock存在），本次不启动新任务", level="WARNING"
            )
            return

        start = time.time()
        state = self._load_state()
        plan = self._resolve_job_mode(state)
        is_deep = plan["mode"] == "deep"
        is_reval = plan["mode"] == "revalidate"

        if is_deep:
            if plan.get("resume"):
                self._log("=== 本轮：深扫续跑（上次被中断，从进度页继续）===", level="INFO")
            else:
                self._log(
                    f"=== 本轮：深扫启动（{plan.get('reason', '')}，"
                    f"每类爬到第 {self.deep_pages} 页，间隔 "
                    f"{self.deep_request_interval[0]}-{self.deep_request_interval[1]}s）===",
                    level="INFO",
                )
        elif is_reval:
            self._log(f"=== 本轮：车源复核（{plan.get('reason', '')}）===", level="INFO")
        else:
            self._log(f"=== 开始执行日常任务（{plan.get('reason', '')}）===", level="INFO")

        try:
            if os.getcwd() not in sys.path:
                sys.path.insert(0, os.getcwd())

            # ── 车源复核：不爬列表页，只按 h_vid 复查久未核实的在售车 ──
            # 与爬取共用「今天已完成」标记（last_run_date），但不跑最后的
            # LLM 增量提取 —— 复核不产生新车，没有新描述要提取。
            # （派生表重算在 _run_revalidation_job 内部按「真有写入」触发。）
            if is_reval:
                result = self._run_revalidation_job()
                # P0-1：只有**正常结束**才记「复核已完成」。中止/故障时不记，
                # 否则下一轮复核要等整整 interval_days 天 —— 而中止恰恰意味着
                # 还有一大批车没查，是最该尽快重来的情形。
                # last_run_date 照写：它是「今天不再跑第二次」的开关，与「这一轮
                # 复核完成了没」是两件事 —— 今天不重跑，明天照常触发。
                if result == "success":
                    self._mark_reval_completed()
                else:
                    self._log(
                        f"复核本轮未完成（{result}）→ **不**写 revalidation.last_completed，"
                        f"下一轮触发时继续复核（不会等满 {self.reval_interval_days} 天）",
                        level="WARNING",
                    )
                state = self._load_state()
                state["last_run_date"] = now_beijing().strftime("%Y-%m-%d")
                state["last_run_at"] = now_beijing().isoformat(timespec="seconds")
                self._save_state(state)
                self._log(
                    f"已在 {self.state_file} 标记今天({state['last_run_date']})已完成，"
                    f"后续重启不会再执行本日任务",
                    level="INFO",
                )
                return

            from carinfo.sites import car28 as carinfo
            from carinfo.core.importer import FastCSVImporter

            csv_dir = "data/csv"
            os.makedirs(csv_dir, exist_ok=True)

            db_importer = FastCSVImporter()
            total = 0
            try:
                for t, daily_pages in sorted(self.pages_by_type.items()):
                    if daily_pages <= 0:
                        self._log(f"跳过类型{t}（pages=0）", level="INFO")
                        continue

                    if is_deep:
                        prog = (state.get("deep_crawl") or {}).get("progress", {}).get(str(t)) or {}
                        start_page = int(prog.get("last_page", 0)) + 1
                        pages = int(prog.get("target") or self.deep_pages)
                        if start_page > pages:
                            self._log(f"类型{t} 深扫已完成（{pages} 页），跳过", level="INFO")
                            continue
                        interval = list(self.deep_request_interval)
                        self._log(
                            f"开始类型{t} 深扫：第 {start_page}-{pages} 页"
                            f"（间隔 {interval[0]}-{interval[1]}s）",
                            level="INFO",
                        )
                    else:
                        start_page, pages, interval = 1, daily_pages, None
                        self._log(f"开始类型{t}，页数={pages}", level="INFO")

                    csv = os.path.join(csv_dir, f"car_data_{t}.csv")
                    deep_track = {"last_started": 0}

                    def page_hook(page: int, _t=t) -> None:
                        # 每页刷新运行锁：单轮可达 12 小时 > 任何合理 stale 阈值，
                        # 靠固定阈值会在中途失效（见 FileLock.touch 的说明）。
                        self.lock.touch()
                        # 深扫进度：page 开始 = page-1 页已完成。进程被杀时
                        # 状态文件里始终是「最后整页」，续跑会重做被杀的半页。
                        if is_deep:
                            deep_track["last_started"] = page
                            self._save_deep_progress(_t, page - 1)

                    stats = carinfo.scrape_vehicle_type(
                        t, pages, csv, start_page=start_page, db_importer=db_importer,
                        page_hook=page_hook,
                        request_interval=tuple(interval) if interval else None,
                    )
                    total += stats['total_vehicles']
                    self._log(
                        f"类型{t}完成: 爬取{stats['total_vehicles']}条，"
                        f"新增{stats['new_vehicles']}，更新{stats['updated_vehicles']}，"
                        f"错误{stats['error_count']}，状态{stats['status']}",
                        level="INFO",
                    )

                    if is_deep:
                        # 成功跑完 → 按页码推算；被中断（partial/异常）→
                        # 退回「最后开始的页之前」，续跑时重做半页，绝不跳页
                        if stats['status'] == 'success':
                            done_page = start_page + stats['pages_scraped'] - 1
                        else:
                            done_page = max(start_page - 1, deep_track["last_started"] - 1)
                        self._save_deep_progress(t, done_page)
                        self._log(f"类型{t} 深扫进度: 已完成到第 {done_page}/{pages} 页", level="INFO")
            finally:
                db_importer.close()

            if total == 0:
                self._log("没有需要爬取的车辆类型或未抓到数据", level="WARNING")

            # 深扫全部类型到目标页 → 落完成标记、清进度；没到（被中断）→
            # 保留进度，明天触发时自动续跑
            if is_deep:
                progress = (self._load_state().get("deep_crawl") or {}).get("progress") or {}
                enabled = [t for t, p in self.pages_by_type.items() if p > 0]
                if all(
                    int((progress.get(str(t)) or {}).get("last_page", 0)) >= self.deep_pages
                    for t in enabled
                ):
                    self._mark_deep_completed()
                    self._log(
                        f"=== 深扫完成：全部 {len(enabled)} 类到第 {self.deep_pages} 页，"
                        f"下次深扫在 {self.deep_interval_days} 天后 ===",
                        level="INFO",
                    )

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

            # LLM 字段提取(增量):补本轮新爬车辆描述里正则提不出的字段
            # (hand_count/mileage/import_type 等,详见 carinfo.search.extract)。
            # 放在写 last_run_date **之后**:提取失败绝不能阻止「今天已完成」
            # 标记,否则明天会重爬整轮;没打标的车下一轮增量会自动补上。
            # run_incremental 内部已把异常全包住,这里再兜一层以防导入失败。
            try:
                from carinfo.search.extract import run_incremental

                ok, msg = run_incremental()
                self._log(msg, level="INFO" if ok else "WARNING")
            except Exception as e:
                self._log(f"LLM 字段提取入口异常(不影响调度): {e}", level="WARNING")

            # 车名别名生成(增量):只给本轮新出现的车系补中文别名(如 步威→STEPWGN)。
            # 放这里同理——生成失败不能阻止「今天已完成」标记;没生成的下轮自愈。
            try:
                from carinfo.search.aliases import run_incremental as alias_incremental

                ok, msg = alias_incremental()
                self._log(msg, level="INFO" if ok else "WARNING")
            except Exception as e:
                self._log(f"车名别名生成入口异常(不影响调度): {e}", level="WARNING")

            # 派生表重算（2026-09-30 补回爬取路径的触发点）：
            # vehicle_features 是检索的 INNER JOIN 对象，爬取写库后不重算的话
            # **新车连派生行都没有**——不止缺 body_type，是整个不可搜，要等
            # 下一次复核路径（interval_days=7 天）触发重算才进检索池。
            # _run_revalidation_job 里的注释声称"爬取路径有
            # _rebuild_features_after_crawl"，该函数在后续演进中已丢失。
            # 放在 extract/alias **之后**：本轮 LLM 提取的 hand/mileage 当轮即
            # 进 condition_score；放在 last_run_date 之后：重算失败只告警，
            # 不阻止「今天已完成」标记（与 extract/alias 同模式），下轮自愈。
            if total > 0:
                try:
                    self._rebuild_features(f"爬取写入 {total} 条")
                except Exception as e:  # noqa: BLE001
                    self._log(f"派生表重算入口异常(不影响调度): {e}", level="WARNING")

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
