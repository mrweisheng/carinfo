"""检索上下文：词表 + 库内真实键集合 + 车系变体映射，带短 TTL 缓存。

为什么要这个东西：解析层需要判断「模型说的车型在库里到底有没有」。有，就用
归一键做等值匹配（快且准）；没有，就退成关键词模糊匹配（慢但不会空手而归）。
把词表和键集合缓存起来，API 每请求一次不用重读 1110 条词表。
"""

from __future__ import annotations

import threading
import time

from carinfo.search.normalize import Vocabulary

#: 车系变体尾缀白名单（同款车型的动力/驱动/写法后缀）。
#: **数字尾缀一律不收**：A3→A35、GT→GT3 是不同功率级/不同车。
#: R 尾缀（NSX→NSXR 性能版）保留 —— RANGE→RANGER 那例归一脏数据误伤
#: 由第四道「跨品牌主力防线」独立拦截（RANGER 在 FORD 有 13 台 ≥2），
#: 不需要牺牲 R 尾缀（一刀切去 R 会连带误杀 NSXR，实测教训）。
VARIANT_TAILS = frozenset({
    "H", "L", "A", "I", "D", "E", "S", "C", "V", "R",
    "HL", "IA", "DI", "CDI", "EV", "HYBRID", "SP", "SE", "TRD", "BT", "GT",
})


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
                 variant_map: dict[str, list[str]] | None = None):
        self.vocab = vocab
        self.models = models
        self.brands = brands
        #: base_model → 同款兄弟键（如 LM350 → [LM350H]）。命中精确车系时
        #: 解析层把它展开成列表一起查 —— 否则搜 LM350 会漏掉混动版 18 台。
        self.variant_map = variant_map or {}

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

            cls._instance = cls(vocab, models, brands, variant_map)
            cls._loaded_at = now
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        """特征表重算后调用，强制下次重新加载。"""
        with cls._lock:
            cls._instance = None
            cls._loaded_at = 0.0
