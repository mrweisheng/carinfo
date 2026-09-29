"""检索上下文：词表 + 库内真实键集合 + 车系变体映射，带短 TTL 缓存。

为什么要这个东西：解析层需要判断「模型说的车型在库里到底有没有」。有，就用
归一键做等值匹配（快且准）；没有，就退成关键词模糊匹配（慢但不会空手而归）。
把词表和键集合缓存起来，API 每请求一次不用重读 1110 条词表。
"""

from __future__ import annotations

import threading
import time

from carinfo.search.aliases import SEED_ALIASES, cjk_runs, load_rows, normalize_alias, to_pinyin
from carinfo.search.normalize import Vocabulary

#: 拼音匹配的最短长度（≥ 2 个汉字，如「步威」= buwei=5）。单字拼音歧义太大，不参与。
_MIN_PINYIN_LEN = 4

#: 车系变体尾缀白名单（同款车型的动力/驱动/写法后缀）。
#: **数字尾缀一律不收**：A3→A35、GT→GT3 是不同功率级/不同车。
#: R 尾缀（NSX→NSXR 性能版）保留 —— RANGE→RANGER 那例归一脏数据误伤
#: 由第四道「跨品牌主力防线」独立拦截（RANGER 在 FORD 有 13 台 ≥2），
#: 不需要牺牲 R 尾缀（一刀切去 R 会连带误杀 NSXR，实测教训）。
VARIANT_TAILS = frozenset({
    "H", "L", "A", "I", "D", "E", "S", "C", "V", "R",
    "HL", "IA", "DI", "CDI", "EV", "HYBRID", "SP", "SE", "TRD", "BT", "GT",
})


def _build_alias_maps(
    rows: list[tuple[str, str, str, str]],
) -> tuple[dict[str, str], dict[str, str]]:
    """构建 (别名→车系, 拼音→车系) 两张映射。

    - 静态种子 (`SEED_ALIASES`) 先入，`model_aliases` 表后入；同名时**种子优先**
      （`setdefault`），避免生成结果覆盖人工确认条目。
    - 拼音映射只保留**唯一**指向的：两个不同车系共用同一拼音时整条剔除
      （如「赛瑞纳」与其它同音词 → 匹配会指向错误车系，宁可漏）。
    """
    alias_map: dict[str, str] = {}
    for alias, base in SEED_ALIASES.items():
        a = normalize_alias(alias)
        if a:
            alias_map.setdefault(a, base)
    for alias, _py, base, _src in rows:
        a = normalize_alias(alias)
        if a:
            alias_map.setdefault(a, base)

    pinyin_bases: dict[str, set[str]] = {}
    for a, base in alias_map.items():
        py = to_pinyin(a)
        if len(py) >= _MIN_PINYIN_LEN:
            pinyin_bases.setdefault(py, set()).add(base)
    pinyin_map = {py: next(iter(bases)) for py, bases in pinyin_bases.items() if len(bases) == 1}
    return alias_map, pinyin_map


def build_variant_map(rows: list[tuple[str, str, int]]) -> dict[str, list[str]]:
    """从 (base_model, brand_norm, 在售台数) 构建同款变体映射。

    三道约束（2026-09-27 调研定稿，全库枚举 211 个前缀组收敛到 34 组真变体）：
    1. **同品牌** —— C4(雪铁龙) 不并 C43(奔驰AMG)、S6(奥迪) 不并 S600(奔驰)；
    2. **尾缀在白名单** —— 只认动力/驱动/写法变体（H 混动/L 长轴/A·I·IA 波箱
       写法/S 性能版等），数字尾缀（A3→A35 跨功率级）与 R（RANGER 误伤）不收；
    3. **父键在售 >=5 台** —— 'X'/'N'/'MODE' 这类 1 台残缺键不配当锚点。

    第四道防线：兄弟键若在**其他品牌**下已是主力车型（>=2 台），视为独立
    车型不并入 —— 挡住归一脏数据（RANGER 在福特 13 台，即使尾缀混进来也会
    被这条拦下）。
    """
    from collections import defaultdict
    by_brand: dict[str, list[tuple[str, int]]] = defaultdict(list)
    brand_of: dict[str, set[str]] = defaultdict(set)
    count_of: dict[tuple[str, str], int] = {}
    for bm, bn, n in rows:
        by_brand[bn].append((bm, n))
        brand_of[bm].add(bn)
        count_of[(bm, bn)] = n

    out: dict[str, list[str]] = {}
    for bn, kvs in by_brand.items():
        keys = [k for k, _n in kvs]
        for bm, n in kvs:
            if n < 5:
                continue
            sibs = []
            for other in keys:
                if other == bm or not other.startswith(bm):
                    continue
                if other[len(bm):] not in VARIANT_TAILS:
                    continue
                # 跨品牌主力防线：该兄弟键在别的品牌下也大量出现 = 独立车型
                if any(count_of.get((other, ob), 0) >= 2
                       for ob in brand_of[other] if ob != bn):
                    continue
                sibs.append(other)
            if sibs:
                out.setdefault(bm, []).extend(sibs)
    return out


class SearchContext:
    _lock = threading.Lock()
    _instance: "SearchContext | None" = None
    _loaded_at: float = 0.0
    #: 词表是迁移来的固定数据，键集合却随每次爬取变化 —— 给个短 TTL 平衡
    TTL_SECONDS = 300.0

    def __init__(self, vocab: Vocabulary, models: set[str], brands: set[str],
                 variant_map: dict[str, list[str]] | None = None,
                 alias_map: dict[str, str] | None = None,
                 pinyin_map: dict[str, str] | None = None):
        self.vocab = vocab
        self.models = models
        self.brands = brands
        #: base_model → 同款兄弟键（如 LM350 → [LM350H]）。命中精确车系时
        #: 解析层把它展开成列表一起查 —— 否则搜 LM350 会漏掉混动版 18 台。
        self.variant_map = variant_map or {}
        #: 中文别名 → base_model（种子 + model_aliases 表）。静态种子先入、表后入，
        #: 同别名时种子优先。
        self.alias_map = alias_map or {}
        #: 无调拼音 → base_model（同音错别字/繁简兜底）。**有歧义的拼音已剔除**
        #: （两个不同车系共用同一拼音时不匹配，宁可漏不可错）。
        self.pinyin_map = pinyin_map or {}

    @classmethod
    def load(cls, conn, force: bool = False) -> "SearchContext":
        now = time.monotonic()
        with cls._lock:
            fresh = cls._instance is not None and (now - cls._loaded_at) < cls.TTL_SECONDS
            if fresh and not force:
                return cls._instance

            cur = conn.cursor()
            cur.execute("""
                SELECT base_model, brand, search_pattern
                FROM model_vocabulary WHERE status = 'active'
            """)
            vocab = Vocabulary.from_rows(cur.fetchall())

            # 归一之后真实存在于特征表里的键 —— 拿它判断「模型说的车型库里有吗」
            cur.execute("SELECT DISTINCT base_model FROM vehicle_features WHERE base_model IS NOT NULL")
            models = {r[0] for r in cur.fetchall()}
            cur.execute("SELECT DISTINCT brand_norm FROM vehicle_features WHERE brand_norm IS NOT NULL")
            brands = {r[0] for r in cur.fetchall()}
            cur.execute("""
                SELECT f.base_model, f.brand_norm, count(*)
                FROM vehicle_features f JOIN vehicles v USING(vehicle_id)
                WHERE v.vehicle_status = 1 AND f.base_model IS NOT NULL
                  AND f.brand_norm IS NOT NULL
                GROUP BY 1, 2
            """)
            variant_map = build_variant_map(cur.fetchall())
            cur.close()

            # 别名表可能尚未建（首次部署）：load_rows 表不存在时返回 []，退回静态种子，
            # 绝不让检索侧 500。
            alias_map, pinyin_map = _build_alias_maps(load_rows(conn))

            cls._instance = cls(vocab, models, brands, variant_map, alias_map, pinyin_map)
            cls._loaded_at = now
            return cls._instance

    def is_model_code(self, kw: str | None) -> bool:
        """关键词是否**指向库内车型**：等值键、某键的前缀（≥2 字符）、或词表命中。

        放宽阶梯用它区分「真型号」与「解析噪声」：真型号永不丢弃（丢了就是顶包），
        只有噪声才允许丢。

        **必须按前缀判，不能只做等值。** 纯数字型号（'740'）不在 `models` 里 ——
        库里只有 740I / 740LI / 740LIA，等值判定会把真型号误判成噪声
        （2026-09-28 实测：`'740' not in ctx.models` 为真，旧注释声称的
        「740 会先在 ctx.models 等值命中」不成立）。

        ⚠️ **这是「宽判」不是「精判」，会返回若干 True 的假阳性 —— 调用方必须自己
        兜底**（2026-09-29 实测订正，旧注释举的 'LM'/'30'/'2015' 反例**全错**）：

        - `'LM'` → **True**（`'LM350'.startswith('LM')`）。靠 `_group_to_spec` 的
          截断守卫（len<3 且是更长 token 的 <2/3 片段 → 丢弃）拦掉，**不是**靠这里；
        - `'30'` / `'2015'` → **True**（库里确有 '30'、'2015' 这些脏键，且是前缀）；
        - `'3000'` → **True**（词表松匹配 `'3000'.startswith('300')` 命中 HINO 的
          '300' 前缀模式）。故规则层额外按「长度 ≥3 + 排量写法过滤」兜底
          （见 `parser` 的 `_QUANTITY_RE`）；
        - `'Z8'` / `'M760'` → **False**（不在库、无前缀、词表也不命中）—— 这两个
          是**库外型号**，由 `engine.is_identity_code`（含字母的 ASCII 码）与规则层
          的「型号形状」兜底（两边取或）。
        """
        if not kw:
            return False
        k = kw.strip().upper()
        if len(k) < 2:
            return False
        if k in self.models or self.vocab.match_compact(k):
            return True
        return any(m.startswith(k) for m in self.models)

    def resolve_model_alias(self, text: str | None) -> str | None:
        """中文别名（含**同音错字/繁简差异**）→ 库内 base_model；命中不了返回 None。

        两步：① 精确别名；② 无调拼音（`步威`↔`布威`、`阿爾法`↔`阿尔法`）。
        两者都要求目标键确实在 `self.models` 里（库里没有的车系不能用别名硬指）。
        """
        key = normalize_alias(text)
        if not key:
            return None
        bm = self.alias_map.get(key)
        if bm and bm in self.models:
            return bm
        py = to_pinyin(key)
        if len(py) >= _MIN_PINYIN_LEN:
            bm = self.pinyin_map.get(py)
            if bm and bm in self.models:
                return bm
        return None

    def find_model_alias(self, text: str | None) -> str | None:
        """从**整句**里找别名（规则路径用）。先精确子串，再对中文段做滑窗拼音匹配。

        规则路径拿到的是整句（「我想搵布威」），要能定位其中的车名。滑窗限制 2-5 字，
        既能覆盖「埃尔法」「陆地巡洋舰」，又不至于把整句当拼音去撞。
        """
        if not text:
            return None
        for alias, bm in self.alias_map.items():
            if alias in text and bm in self.models:
                return bm
        for run in cjk_runs(text):
            n = len(run)
            for i in range(n):
                for j in range(i + 2, min(i + 6, n) + 1):
                    py = to_pinyin(run[i:j])
                    if len(py) >= _MIN_PINYIN_LEN:
                        bm = self.pinyin_map.get(py)
                        if bm and bm in self.models:
                            return bm
        return None

    @classmethod
    def reset(cls) -> None:
        """特征表重算后调用，强制下次重新加载。"""
        with cls._lock:
            cls._instance = None
            cls._loaded_at = 0.0
