"""车名别名表：把中文/粤语口语车名映射到库内英文车系键（base_model）。

**为什么需要它**：LLM 主路径能认出多数中文车名（阿尔法→ALPHARD），但它对生僻叫法
会**漏**（不知道 STE/WGN 叫「步威」）或**编**（凭空造「阶梯」）；而无 LLM 时的静态
`MODEL_ALIASES` 只手写了高频几条。别名表把「别名 → base_model」沉淀成**库内数据**，
并且用**拼音**（pypinyin，无调）兜住同音错别字与繁简差异：

- 库里只要有「步威 → STEPWGN」，用户打「布威」（同音 buwei）也能命中；
- 「阿爾法 / 阿尔法 / 阿尔发」无调拼音都是 `aerfa`，繁简与错字一并覆盖。

来源分级（`source`），**先到先得**（INSERT ... ON CONFLICT DO NOTHING）：
- `seed`      —— 静态种子（`normalize.MODEL_ALIASES` + 本项目人工确认的香港叫法），
                 `ensure_table()` 时写入，优先级最高、永不被生成覆盖；
- `llm`       —— 批量生成（`run()`/`run_incremental()`），只补 seed 没有的；
- `manual`    —— 运营手工加（直接 SQL/后续工具）。

设计要点：
- 生成**只针对库内真实存在的 base_model**（vehicle_features 里的键），规模天然可控
  （~1100 条），不需要枚举全网车型。
- 生成结果过**结构校验闸**（纯中文、长度 2-8、不与任何 base_model 冲突、别名全局唯一、
  黑名单），挡掉「字母数字 / 空串 / 跨车系抢名」这类脏数据。
- 与检索**解耦**：本模块的写操作只在 CLI / service 增量入口发生；检索侧只读
  （`context.SearchContext` 借连接读 `model_aliases`，表不存在时静默退回静态表）。

用法：
    uv run python -m carinfo.search.aliases --dry-run      # 只列待生成的车型，不调模型
    uv run python -m carinfo.search.aliases                # 增量：只给没有别名的车型生成
    uv run python -m carinfo.search.aliases --force        # 全部重跑（仍不覆盖已有别名）
    run_incremental()                                      # service 每轮爬完后的程序入口
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

if hasattr(sys.stdout, "reconfigure"):  # Windows 控制台中文
    sys.stdout.reconfigure(encoding="utf-8")

from pypinyin import lazy_pinyin

from carinfo.search.normalize import BRAND_ALIASES, MODEL_ALIASES

#: 别名表 DDL。**幂等**（IF NOT EXISTS），可在任何进程安全执行。
#: alias 是主键 —— 一个别名只能指向一个车系（跨车系抢名由校验闸拦下）。
DDL = """
CREATE TABLE IF NOT EXISTS model_aliases (
  alias        varchar(64)  PRIMARY KEY,
  alias_pinyin varchar(128) NOT NULL DEFAULT '',
  base_model   varchar(100) NOT NULL,
  source       varchar(16)  NOT NULL DEFAULT 'llm',
  updated_at   timestamptz  DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_model_aliases_pinyin ON model_aliases (alias_pinyin);
CREATE INDEX IF NOT EXISTS idx_model_aliases_base ON model_aliases (base_model);
"""

#: 「香港/内地确实有人这么叫」的人工确认条目 —— 补 LLM 会漏的生僻/音译叫法。
#: 键是别名、值是库内车系键。繁简由拼音兜底，这里只写常用写法即可。
#: 注意：值若不在库里（base_model 不存在），解析时会被 `in ctx.models` 过滤掉，无害。
CURATED_SEED: dict[str, str] = {
    # STEPWGN：官方中文译名「步威」（LLM 不知道，实测它会否认有中文名）。
    # 「布威」是同音错字，靠别名表的拼音匹配自动兜住，不必重复写。
    "步威": "STEPWGN",
    # 主流 MPV 的内地/音译叫法（LLM 时对时错，种子里固化）
    "艾力绅": "ELYSION", "艾力神": "ELYSION", "爱力绅": "ELYSION",
    "赛瑞纳": "SERENA", "赛丽娜": "SERENA",
    "得利卡": "DELICA", "德利卡": "DELICA",
    "君爵": "ELGRAND", "贵士": "QUEST",
    # 「开曼」= Porsche **Cayman**（硬顶跑车）。LLM 生成时错安到了 BOXSTER 上
    # （2026-09-30 压测抓到：开曼→BOXSTER），种子固化正确映射；错行已在库里删除。
    "开曼": "CAYMAN",
}

#: 别名 → base_model 的静态种子（合并 MODEL_ALIASES 与人工条目）。
#: 检索侧在**没有别名表 / 表为空**时也用它，保证核心别名永远可用。
SEED_ALIASES: dict[str, str] = {**MODEL_ALIASES, **CURATED_SEED}

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_PURE_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
#: 纯四位年份的脏车系键（'2012'/'2015' 这类从卖家乱填的 car_model 兜出来的）——
#: 给它生成中文别名纯属浪费，直接跳过。
_YEAR_KEY_RE = re.compile(r"(?:19|20)\d{2}")


def _worth_alias(base_model: str) -> bool:
    """这个车系键值不值得生成中文别名。过滤：过短、纯四位年份的脏键。"""
    bm = (base_model or "").strip()
    if len(bm) < 2:
        return False
    return not _YEAR_KEY_RE.fullmatch(bm)


def cjk_runs(text: str | None) -> list[str]:
    """抽出文本里的连续汉字段（供规则路径做别名/拼音滑窗匹配）。"""
    return _CJK_RUN_RE.findall(text or "")

#: 已知的 LLM 幻觉/泛用词 —— 会被生成出来但语义不对（如把 FREED 译成「自由」、
#: 把 STEPWGN 译成「阶梯」，或把 FIT 的「飞度」安到 FREED 上）。只作**兜底**
#: （生成提示词已要求"不确定就空"），不是主力防线。
_ALIAS_BLACKLIST: frozenset[str] = frozenset({
    "自由", "阶梯", "步顶", "波顶", "步神", "士多福", "士多污", "飞度",
    "汽车", "新车", "旧车", "二手车", "便宜", "自动", "手动", "七人车",
    # 泛用/座位/车型类别词 —— 不是某个车系的名字，命中会大面积劫持查询
    # （「双座」「皮卡」「五系」被安到某一台车上）。
    "四座", "双座", "五座", "皮卡", "五系", "七系", "三系", "一系",
    # 「类目词复合别名」实测回归（2026-09-30 车身类型上线压测）：「野马跑车→MUSTANG」
    # 「小跑车→ROADSTER」这类**别名里已经含类目词**的条目，在规则兜底路径会与
    # body_type 扫描叠加成矛盾条件（MUSTANG 车身未分类、ROADSTER 是 CONVERTIBLE，
    # 再 AND body_type=COUPE 全部 0 命中）。类目词一律走 parser 的 body 词表，
    # 不许进车系别名表。
    "跑车", "小跑车",
})


def to_pinyin(text: str | None) -> str:
    """中文别名 → 无调拼音（只保留汉字部分）。「步威」「布威」→ `buwei`。

    只吃汉字：拉丁字母/数字/空格先剥掉。空串或非中文返回 ""。
    """
    if not text:
        return ""
    han = "".join(_CJK_RE.findall(text))
    if not han:
        return ""
    return "".join(lazy_pinyin(han))


def normalize_alias(text: str | None) -> str:
    """别名归一：去所有空白。中文不分大小写，拉丁别名已在校验闸排除。"""
    if not text:
        return ""
    return re.sub(r"\s+", "", str(text)).strip()


def load_rows(conn) -> list[tuple[str, str, str, str]]:
    """读全部别名，返回 [(alias, alias_pinyin, base_model, source), ...]。

    **表不存在时静默返回 []** —— 检索侧只读，不能因为别名表还没建就 500。
    失败时 rollback（只读连接被 abort 后归还前必须清干净）。
    """
    import psycopg2
    cur = conn.cursor()
    try:
        cur.execute("SELECT alias, alias_pinyin, base_model, source FROM model_aliases")
        return [tuple(r) for r in cur.fetchall()]
    except psycopg2.errors.UndefinedTable:
        conn.rollback()
        return []
    finally:
        cur.close()


def ensure_table(conn) -> int:
    """建表 + 灌种子（幂等）。返回写入的种子条数（已存在的不计）。"""
    cur = conn.cursor()
    cur.execute(DDL)
    n = 0
    for alias, base in SEED_ALIASES.items():
        a = normalize_alias(alias)
        if not a:
            continue
        cur.execute(
            "INSERT INTO model_aliases (alias, alias_pinyin, base_model, source) "
            "VALUES (%s, %s, %s, 'seed') ON CONFLICT (alias) DO NOTHING",
            (a, to_pinyin(a), base),
        )
        n += cur.rowcount
    conn.commit()
    cur.close()
    return n


# ---------------------------------------------------------------------------
# 批量生成
# ---------------------------------------------------------------------------

GENERATION_SYSTEM_PROMPT = """你是香港二手车平台的车名别名审核员。给定一批「英文车系 + 品牌」，
判断每个车系是否存在中国内地/香港/粤语口语里**真实通用**的中文叫法（官方中文名、常见音译、口语简称）。

铁律：
- 只当这个名字是**真实通用的叫法**时才列出；不确定、或只是你自己的直译 → aliases 留空数组 []。
- 宁可留空，绝不编造 —— 编造会导致用户搜错车（把 A 车的名字安到 B 车上是严重错误），比没有别名更糟。
- 可以补上同一叫法的**常见同音错别字**（如「阿尔法/阿尔发」）。
- **不要列品牌名**（「本田」「丰田」「马自达」是品牌，不是车型名）。
- **不要列属于其它车型的名字**（如「飞度」是 FIT 的别名，不能安到 FREED 上）。
- 每个别名必须是**纯中文**、2-8 个字、不含字母数字。
- base_model 必须**原样回填**成输入里的英文车系本身，**不要带品牌、不要加括号**。
- 只输出 JSON：{"items":[{"base_model":"...","aliases":["..."]}]}。"""


@dataclass
class GenStats:
    candidates: int = 0
    requested_models: int = 0
    llm_ok_items: int = 0
    llm_fail_batches: int = 0
    inserted: int = 0
    drops: list = field(default_factory=list)


def _valid_alias(alias: str, base_model: str, known_models: set[str],
                 alias_to_base: dict[str, str]) -> str | None:
    """结构校验闸。返回归一后的别名，非法返回 None。"""
    a = normalize_alias(alias)
    if not (2 <= len(a) <= 8):
        return None
    if not _PURE_CJK_RE.fullmatch(a):        # 必须纯中文（无字母/数字/标点）
        return None
    if a in known_models:                    # 与库内车系键重名 → 会污染等值匹配
        return None
    if a in BRAND_ALIASES:
        # 品牌名（本田/丰田/马自达…）绝不能当**车系**别名：那会让「本田」落到
        # base_model='HONDA'（库里只是几台把品牌填进车型的车），而不是走品牌匹配
        # 返回全部本田 —— 是明确的错误结果。品牌由 BRAND_ALIASES 单独负责。
        return None
    # 子串黑名单：逮住「本田飞度」（含「飞度」）这类把黑名单词包在里面的拼凑别名。
    if any(bl in a for bl in _ALIAS_BLACKLIST):
        return None
    existing = alias_to_base.get(a)
    if existing is not None and existing != base_model:
        return None                          # 一个别名只能指向一个车系
    return a


def _process_batch(llm, models: list[tuple[str, str | None]],
                   known_models: set[str], alias_to_base: dict[str, str],
                   lock) -> tuple[list[tuple[str, str, str]], list[str]]:
    """调 LLM 生成一批车型的别名。返回 ([(alias, pinyin, base_model)], drops)。"""
    lines = []
    for i, (bm, brand) in enumerate(models, 1):
        b = (brand or "").replace("\xa0", " ").strip()
        lines.append(f"{i}. {bm}" + (f" ({b})" if b else ""))
    # temperature=0：这是往库里写事实性映射，要确定性、少发挥（幻觉的温床是高温）。
    out = llm.chat_json(GENERATION_SYSTEM_PROMPT, "给以下车系生成别名：\n" + "\n".join(lines),
                        temperature=0.0)
    items = out.get("items") if isinstance(out, dict) else out
    if not isinstance(items, list):
        raise ValueError(f"输出形态异常: {str(out)[:120]}")

    valid_bms = {bm for bm, _ in models}
    seen: set[str] = set()
    results: list[tuple[str, str, str]] = []
    drops: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        # 模型常把 base_model 回填成「STEPWGN (HONDA)」（照抄输入里的品牌括号）——
        # 必须剥掉尾部括号再比对，否则整批被 unknown_model 丢弃。
        raw_bm = str(it.get("base_model") or "").strip().upper()
        bm = re.sub(r"\s*[（(].*?[)）]\s*$", "", raw_bm).strip()
        if bm not in valid_bms:
            drops.append(f"unknown_model:{raw_bm}")
            continue
        aliases = it.get("aliases")
        if not isinstance(aliases, list):
            continue
        for raw in aliases:
            if not isinstance(raw, str):
                continue
            # 共享 dict 的读改写必须串行，否则并发批次会各自看到旧值而互相抢名
            with lock:
                a = _valid_alias(raw, bm, known_models, alias_to_base)
                if a is None:
                    drops.append(f"invalid:{raw}")
                    continue
                if a in seen:                 # 同一批内去重
                    continue
                seen.add(a)
                results.append((a, to_pinyin(a), bm))
                alias_to_base[a] = bm
    return results, drops


def run(conn, llm, *, limit: int | None = None, batch_size: int = 25,
        concurrency: int = 3, force: bool = False, dry_run: bool = False) -> GenStats:
    """增量（或 --force 全量）生成别名。已存在的别名一律不覆盖。"""
    import threading

    stats = GenStats()
    cur = conn.cursor()
    cur.execute(
        "SELECT f.base_model, min(f.brand_norm) "
        "FROM vehicle_features f "
        "WHERE f.base_model IS NOT NULL AND f.base_model <> '' "
        "GROUP BY f.base_model ORDER BY f.base_model"
    )
    all_models = [(str(bm), bn) for bm, bn in cur.fetchall()]
    cur.close()
    alias_to_base = {a: bm for a, _py, bm, _s in load_rows(conn)}

    known_models = {bm for bm, _ in all_models}
    have = set(alias_to_base.values())
    todo = [(bm, bn) for bm, bn in all_models if _worth_alias(bm)]
    if not force:
        todo = [(bm, bn) for bm, bn in todo if bm not in have]
    if limit:
        todo = todo[:limit]

    stats.candidates = len(all_models)
    stats.requested_models = len(todo)
    if dry_run:
        print(f"[dry-run] 库内车系 {len(all_models)} 个；待生成别名 {len(todo)} 个"
              f"（已有别名的车系 {len(set(alias_to_base.values()))} 个）")
        for bm, bn in todo[:50]:
            print(f"  - {bm} ({bn})")
        return stats

    ensure_table(conn)                       # 写操作；dry-run 不走这里
    if not todo:
        print("所有车系都已有别名，无需生成")
        return stats

    lock = threading.Lock()
    batches = [todo[i:i + batch_size] for i in range(0, len(todo), batch_size)]
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futs = {pool.submit(_process_batch, llm, b, known_models, alias_to_base, lock): b
                for b in batches}
        for fut in as_completed(futs):
            try:
                results, drops = fut.result()
                stats.llm_ok_items += len(results)
                stats.drops.extend(drops)
                if results:
                    cur = conn.cursor()
                    for a, py, bm in results:
                        cur.execute(
                            "INSERT INTO model_aliases (alias, alias_pinyin, base_model, source) "
                            "VALUES (%s, %s, %s, 'llm') ON CONFLICT (alias) DO NOTHING",
                            (a, py, bm),
                        )
                        stats.inserted += cur.rowcount
                    conn.commit()
                    cur.close()
            except Exception as e:  # 网络/解析失败：本批跳过，下轮自愈
                conn.rollback()
                stats.llm_fail_batches += 1
                print(f"    批次失败: {type(e).__name__}: {str(e)[:100]}", flush=True)
    return stats


def _connect():
    import psycopg2
    from dotenv import load_dotenv
    load_dotenv()
    return psycopg2.connect(
        host=os.environ["DB_HOST"], port=int(os.environ.get("DB_PORT", 5432)),
        user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
        dbname=os.environ["DB_NAME"], connect_timeout=20,
    )


def run_incremental() -> tuple[bool, str]:
    """给 service 每轮爬完调的程序入口：只给**新出现的车系**生成别名。

    异常全包，失败不影响爬虫。没有新车型时**不调模型**（零成本）。
    检索侧词表缓存 5 分钟 TTL，新别名最迟 5 分钟后生效。
    """
    try:
        from carinfo.search.config import load_llm_config
        from carinfo.search.llm import LLMClient
        cfg = load_llm_config()
        cfg.disable_thinking = True
        cfg.timeout = 60.0
        llm = LLMClient(cfg)
        conn = _connect()
        try:
            if not llm.configured:
                n = ensure_table(conn)
                return True, f"未配置 MINIMAX_API_KEY，仅建表/灌种子（新增 {n} 条）"
            stats = run(conn, llm)
        finally:
            conn.close()
        if stats.requested_models == 0:
            return True, "车名别名：无新车型，跳过"
        return True, (f"车名别名：新增车型 {stats.requested_models}，"
                      f"落库别名 {stats.inserted}，失败批 {stats.llm_fail_batches}")
    except Exception as e:
        return False, f"车名别名生成异常（不影响爬取）：{type(e).__name__}: {e}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="车名别名批量生成（LLM）")
    ap.add_argument("--dry-run", action="store_true", help="只列待生成车型，不调模型")
    ap.add_argument("--force", action="store_true", help="全部车系都重跑（仍不覆盖已有别名）")
    ap.add_argument("--limit", type=int, default=None, help="最多处理多少个车型")
    ap.add_argument("--batch-size", type=int, default=25)
    ap.add_argument("--concurrency", type=int, default=3)
    args = ap.parse_args(argv)

    if args.dry_run:
        conn = _connect()
        try:
            run(conn, None, limit=args.limit, dry_run=True)
        finally:
            conn.close()
        return 0

    from carinfo.search.config import load_llm_config
    from carinfo.search.llm import LLMClient
    cfg = load_llm_config()
    cfg.disable_thinking = True
    cfg.timeout = 60.0
    llm = LLMClient(cfg)
    if not llm.configured:
        print("未配置 MINIMAX_API_KEY（.env），无法生成别名")
        return 1

    conn = _connect()
    try:
        stats = run(conn, llm, limit=args.limit, batch_size=args.batch_size,
                    concurrency=args.concurrency, force=args.force)
    finally:
        conn.close()

    print(f"\n[完成] 库内车系 {stats.candidates} | 待生成 {stats.requested_models} | "
          f"落库别名 {stats.inserted} | 失败批 {stats.llm_fail_batches}")
    if stats.drops:
        from collections import Counter
        kinds = Counter(d.split(":")[0] for d in stats.drops)
        print(f"    校验闸丢弃 {len(stats.drops)} 项（按类型）：{dict(kinds)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
