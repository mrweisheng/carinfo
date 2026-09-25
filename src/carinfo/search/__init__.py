"""智能搜索：在既有爬取数据之上做自然语言检索与性价比排序。

模块划分（互相独立、可单独验证）：

- normalize   车型/品牌归一（自由文本 → 稳定车系键）。四级路径：
              exact(98.1%) → compact(0.3%) → token(0.3%) → fallback(1.4%)
- context     词表 + 库内真实键集合，带 5 分钟 TTL 缓存（给解析层判存在性）
- features    行情基准与每车派生特征 → market_stats / vehicle_features 两张派生表
- spec        SearchSpec：检索条件的唯一内部表示，所有入口都转成它
- llm         MiniMax 国内版客户端（含"HTTP 200 里藏业务错误码"的处理）
- parser      自然语言 → SearchSpec（有模型走模型，无模型走别名+正则降级）
- engine      SQL 硬过滤 + 五维确定性打分 + TopN
- explain     标签与"为什么是它"的人话解释（模板为主，模型只做可选润色）
- api         FastAPI 薄壳（GET /search 自然语言、POST /search/spec、/vehicle/{id}、/models）
- mcp_server  MCP 薄壳（4 个工具，与 api 共用 engine.search）

**设计铁律：大模型只做两头（理解输入、润色输出），中间打分全部是确定性算法。**
同样的输入 + 同样的库 → 永远同样的排序，每一分都可解释、可回归。

关键数据事实（决定了上面这些设计，改动前请先复核）：
- 在售约 2.1 万台；car_model 是自由文本（ALPHARD 一个车系 258 种写法）
- 浏览量覆盖 44.8%、车况类字段覆盖 28.0% → **缺维必须从权重分母里去掉**，
  否则排序会被"数据齐全度"而不是"车的优劣"主导
- update_date 是 varchar 但 100% 是 ISO 串，必须在 SQL 里转，别在 Python 里减字符串

跑一遍全部验证：
    uv run python _migration/test_normalize.py   # 归一（只读）
    uv run python _migration/test_search.py      # 检索内核（只读）
    uv run python _migration/test_parse.py       # 解析 + 端到端（只读）
    uv run python _migration/test_serving.py     # 解释层/API/MCP（只读，不起服务）
    uv run python _migration/eval_search.py      # 回归门禁：规则 + LLM 两种模式分别判定
                                                 # 加 --mode llm 只验模型路径（缺 key 直接失败）

重算特征表（会 DROP 并重建两张派生表）：
    uv run python -m carinfo.search.features
"""
