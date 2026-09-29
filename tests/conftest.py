"""测试共享夹具。

**分层设计**（尽量离线）：
- 绝大多数用例**不连库、不联网**：用 `synth_ctx`（合成词表/键集合）+ `fake_llm`；
- 只有 `@pytest.mark.db` 的「黄金用例」连真库，**连不上自动 skip**（CI 无库也能跑）。

`synth_ctx` 刻意复刻真实库的**关键形状**：'740' 只作为 740I/740LI/740LIA 的前缀存在、
HINO 的 '300' 词表模式会把 '3000' 松匹配命中 —— 这两点正是两个 P0 的案发机理。
"""

from __future__ import annotations

import pytest

from carinfo.search.context import SearchContext
from carinfo.search.normalize import Vocabulary

#: (base_model, brand_norm, search_pattern) —— 体量极小但形状真实
_ROWS = [
    ("300", "HINO", "300%"),
    ("LM350", "LEXUS", "LM350%"),
    ("LM350H", "LEXUS", "LM350H%"),
    ("M3", "BMW", "M3%"),
    ("X5", "BMW", "X5%"),
    ("A6", "AUDI", "A6%"),
    ("MODEL 3", "TESLA", "MODEL 3%"),
    ("740I", "BMW", "740I%"),
    ("740LI", "BMW", "740LI%"),
    ("740LIA", "BMW", "740LIA%"),
    ("ALPHARD", "TOYOTA", "ALPHARD%"),
    ("S500", "MERCEDES-BENZ", "S500%"),
]


@pytest.fixture(scope="session")
def synth_ctx() -> SearchContext:
    """离线检索上下文：不读库，形状与真库一致。"""
    vocab = Vocabulary.from_rows(_ROWS)
    return SearchContext(
        vocab,
        {r[0] for r in _ROWS},
        {r[1] for r in _ROWS},
        {"LM350": ["LM350H"]},
    )


class FakeLLM:
    """注入式假模型：只认 `.configured` 与 `.chat_json`，**故意不带 `cfg` 属性** ——
    `parse_query` 据此不传 temperature（与真实注入桩的兼容路径一致）。"""

    configured = True

    def __init__(self, payload: dict):
        self.payload = payload

    def chat_json(self, system: str, user: str, **kwargs) -> dict:
        return self.payload


@pytest.fixture
def fake_llm():
    """返回 FakeLLM 类本身：`fake_llm({...})` 构造一个假模型。"""
    return FakeLLM


# ---------------------------------------------------------------------------
# 真库（黄金用例）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def db_fetch():
    """返回 `db.fetch`（借连接执行）。连不上库 → skip 所有 db 用例。"""
    try:
        from carinfo.search import db as dbmod
    except Exception as e:  # noqa: BLE001 —— 缺 psycopg2/配置都算「无库」
        pytest.skip(f"数据库模块不可用：{type(e).__name__}: {e}")
    try:
        dbmod.fetch(lambda conn: conn.cursor().execute("SELECT 1"))
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"数据库不可用：{type(e).__name__}: {e}")
    return dbmod.fetch


@pytest.fixture(scope="session")
def real_ctx(db_fetch) -> SearchContext:
    """真库检索上下文（含全量 1293 个键、真词表）。"""
    return db_fetch(lambda conn: SearchContext.load(conn, force=True))
