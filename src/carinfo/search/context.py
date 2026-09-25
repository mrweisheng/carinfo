"""检索上下文：词表 + 库内真实键集合，带短 TTL 缓存。

为什么要这个东西：解析层需要判断「模型说的车型在库里到底有没有」。有，就用
归一键做等值匹配（快且准）；没有，就退成关键词模糊匹配（慢但不会空手而归）。
把词表和键集合缓存起来，API 每请求一次不用重读 1110 条词表。
"""

from __future__ import annotations

import threading
import time

from carinfo.search.normalize import Vocabulary


class SearchContext:
    _lock = threading.Lock()
    _instance: "SearchContext | None" = None
    _loaded_at: float = 0.0
    #: 词表是迁移来的固定数据，键集合却随每次爬取变化 —— 给个短 TTL 平衡
    TTL_SECONDS = 300.0

    def __init__(self, vocab: Vocabulary, models: set[str], brands: set[str]):
        self.vocab = vocab
        self.models = models
        self.brands = brands

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
            cur.close()

            cls._instance = cls(vocab, models, brands)
            cls._loaded_at = now
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        """特征表重算后调用，强制下次重新加载。"""
        with cls._lock:
            cls._instance = None
            cls._loaded_at = 0.0
