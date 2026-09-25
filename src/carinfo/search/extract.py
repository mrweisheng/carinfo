"""描述字段 LLM 提取:把卖家手写的粤语/繁简混杂描述结构化进 extra_fields。

为什么用 LLM 而不是正则(car28._process_extra_fields 是正则):
- 28car 描述是自由文本,写法极不标准:「0字/1字」(粤语"字"=手)、「未出牌」(=0手)、
  「低咪5萬」(=里程5万)、「六萬五」、「里數65500」、「82xxx」——正则永远追不完,
  实测天花板 hand_count 34.5%,LLM + 黑话提示后同批样本零误判。
- 提取是模式转换任务,**关思考**跑(M3 `thinking.type=disabled`,实测生效):
  每批 20 台 2-4 秒,token 消耗降 ~70%。

数据安全四道闸(缺一不可,全部实测):
1. 白名单字段 + 类型/范围收敛(hand 0-12、mileage 100-100万、import_type 只认行貨/水貨)
2. evidence 原文锚定:每个值必须附「原文逐字片段」,锚不到就丢该字段(防幻觉的核心)
3. 语义哨兵:布尔/枚举字段的 evidence 还必须含强模式(is_swap 必须「換車|swap」,
   china_plate 必须「中港|兩地」)——「換左全車喇叭」这类换零件永远过不了这道闸
4. merge-only 写库:只补库里为 null 的键,正则/爬虫已提取的值**绝不覆盖**;
   写完打 `llm_extracted_at` 标记 → 断点续跑、增量只处理未打标的车

用法:
    uv run python -m carinfo.search.extract --dry-run    # 只统计候选
    uv run python -m carinfo.search.extract --limit 300  # 试跑 300 台
    uv run python -m carinfo.search.extract              # 全量(所有未打标的在售车)
    run_incremental()                                    # service 每轮爬完后的程序入口

⚠️ 依赖 importer 的 extra_fields merge(carinfo.core.importer):爬虫重爬老车时
用「新值优先、旧值补缺」合并,否则次日爬取会把这里补的字段整个覆盖掉。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

if hasattr(sys.stdout, "reconfigure"):   # Windows 控制台中文
    sys.stdout.reconfigure(encoding="utf-8")

#: 候选判定:在售、有描述、未打过 LLM 提取标记
_CANDIDATE_SQL = """
    SELECT vehicle_id, description, extra_fields
    FROM vehicles
    WHERE vehicle_status = 1
      AND description IS NOT NULL AND length(description) > 0
      AND (extra_fields->>'llm_extracted_at') IS NULL
    ORDER BY vehicle_id
    LIMIT %s
"""

#: 打了标、但之后被爬虫覆盖丢标的兜底:关键字段仍全缺的也进候选。
#: import_rows 的 merge 落地后理论上不会再发生,保留作为自愈通道。
_CANDIDATE_FALLBACK_SQL = """
    SELECT vehicle_id, description, extra_fields
    FROM vehicles
    WHERE vehicle_status = 1
      AND description IS NOT NULL AND length(description) > 0
      AND (extra_fields->>'hand_count') IS NULL
      AND (extra_fields->>'mileage_km') IS NULL
      AND (extra_fields->>'import_type') IS NULL
    ORDER BY vehicle_id
    LIMIT %s
"""

SYSTEM_PROMPT = """这些是**香港**二手车卖家手写的车源描述:粤语口语、繁简混杂、中英夹杂、行业黑话。
(例:「換咗/換左」=更换了、「俾」=给、「呔」=轮胎、「偈油」=机油、「牌費」=牌照税、
「里數」=里程、「N字」= N手、「留牌」=卖家保留车牌、「直版」=无事故原版。)
对每条描述提取以下字段。只输出一个 JSON 对象:{"items":[...]}。没提到或拿不准的字段填 null,绝不猜。

每台对象:{"id":"...","hand_count":0-12或null,"mileage_km":整数或null,
"import_type":"行貨"/"水貨"/null,"license_until":原文片段或null,
"china_plate":true/null,"is_swap":true/null,
"evidence":{"字段名":"原文逐字片段"}}

字段规则:
- hand_count:过户手数。「0字/1字/2字」=0/1/2手;「一手車主」=1;「未出牌/新車落地」=0;「二手」=2。
- mileage_km:公里数。「65500」「82xxx」→82000;「約110000km」;「2萬幾公里」→20000;「六萬五」→65000;「只行1100公里」;「里數65500」。
- import_type:「行貨」或「水貨」(繁简都算)。
- license_until:牌費到期,摘原文,如「26年12月」「5月中」「10月」。
- china_plate:提到「中港牌/中港車牌/兩地牌」为 true。

- is_swap —— **判断卖家是否想用这台车交换另一台车(以车易车)**:
  ✓ true:明确表达想换车,如「可換車」「歡迎換車」「換車自用」「可以同價換車」「swap 車」「換車可補差價」。
  ✗ 一定不是 swap(哪怕有「換」字):
    - 「換咗/換左/已換/新換/更換 + 零部件」(喇叭/偈油/電池/避震/呔/pump/Coil/皮帶...)——这是维修保养记录;
    - 「留牌/轉牌」——是车牌处理,不是换车;
    - 只描述车况、配置的任何其他「換」字用法。
  判不准就填 null。

铁律:evidence 必须是原文的逐字子串;每个非 null 字段都要给 evidence。"""

#: ---- 闸 1:字段白名单 / 类型 / 范围 ----
_INT_FIELDS = {"hand_count": (0, 12), "mileage_km": (100, 1_000_000)}
_ENUM_FIELDS = {"import_type": ("行貨", "水貨")}
_STR_FIELDS = ("license_until",)
_BOOL_FIELDS = ("china_plate", "is_swap")

#: ---- 闸 3:布尔/枚举字段的语义哨兵(evidence 必须命中强模式) ----
_SENTINELS: dict[str, tuple[re.Pattern, dict[str, re.Pattern]]] = {
    "is_swap": (re.compile(r"換車|换车|[Ss][Ww][Aa][Pp]"), {}),
    "china_plate": (re.compile(r"中港|兩地|两地"), {}),
    "import_type": (
        re.compile(r"行|水"),
        {"行貨": re.compile(r"行貨|行货"), "水貨": re.compile(r"水貨|水货")},
    ),
}

_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub("", text or "")


def validate_item(item: dict, vid: str, desc: str) -> tuple[dict, list[str]]:
    """四道闸里的前三道。返回 (干净的字段值, 丢弃原因列表)。"""
    clean: dict[str, Any] = {}
    drops: list[str] = []
    if item.get("id") != vid:
        return {}, [f"id_mismatch:{item.get('id')}"]
    ev = item.get("evidence") or {}
    desc_norm = _norm(desc)

    for k, (lo, hi) in _INT_FIELDS.items():
        v = item.get(k)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            drops.append(f"{k}:range:{v!r}")
            continue
        if not _anchored(ev.get(k), desc_norm):
            drops.append(f"{k}:anchor:{v!r}")
            continue
        clean[k] = v

    for k, allowed in _ENUM_FIELDS.items():
        v = item.get(k)
        if v is None:
            continue
        if v not in allowed:
            drops.append(f"{k}:enum:{v!r}")
            continue
        if not _anchored(ev.get(k), desc_norm):
            drops.append(f"{k}:anchor:{v!r}")
            continue
        pat, per_value = _SENTINELS[k]
        e = _norm(str(ev.get(k) or ""))
        if not pat.search(e) or (per_value and not per_value[v].search(e)):
            drops.append(f"{k}:sentinel:{v!r}")
            continue
        clean[k] = v

    v = item.get("license_until")
    if v is not None:
        if not isinstance(v, str) or not (1 <= len(v) <= 20):
            drops.append(f"license_until:type:{v!r}")
        elif not _anchored(ev.get("license_until"), desc_norm):
            drops.append("license_until:anchor")
        else:
            clean["license_until"] = v.strip()

    for k in _BOOL_FIELDS:
        v = item.get(k)
        if v is None:
            continue
        if v is not True:                      # 只接受显式 true;false 当 null 处理
            continue
        if not _anchored(ev.get(k), desc_norm):
            drops.append(f"{k}:anchor")
            continue
        pat, _ = _SENTINELS[k]
        if not pat.search(_norm(str(ev.get(k) or ""))):
            drops.append(f"{k}:sentinel")
            continue
        clean[k] = True

    return clean, drops


def _anchored(evidence, desc_norm: str) -> bool:
    """闸 2:evidence 是原文的逐字子串(容空白差异)。"""
    if not evidence or not isinstance(evidence, str):
        return False
    return _norm(evidence) in desc_norm


@dataclass
class ExtractStats:
    candidates: int = 0
    llm_ok_items: int = 0
    llm_fail_batches: int = 0
    updated_rows: int = 0
    filled: dict = field(default_factory=dict)
    drops: list = field(default_factory=list)

    def note_fill(self, k: str) -> None:
        self.filled[k] = self.filled.get(k, 0) + 1


def _process_batch(llm, batch: list[tuple[str, str, dict]]) -> tuple[list[tuple[str, dict, dict]], list[str]]:
    """调 LLM 提取一批。返回 [(vid, 现有ef, clean), ...] 与丢弃明细。"""
    user = "\n".join(f"[{vid}] {desc[:300]}" for vid, desc, _ef in batch)
    out = llm.chat_json(SYSTEM_PROMPT, f"提取以下 {len(batch)} 条:\n\n{user}")
    items = out.get("items") if isinstance(out, dict) else out
    if not isinstance(items, list):
        raise ValueError(f"输出形态异常: {str(out)[:120]}")
    by_vid = {b[0]: b for b in batch}
    results, drops = [], []
    for it in items:
        if not isinstance(it, dict):
            continue
        vid = it.get("id")
        if vid not in by_vid:
            drops.append(f"unknown_id:{vid}")
            continue
        _, desc, ef = by_vid[vid]
        clean, ds = validate_item(it, vid, desc)
        drops.extend(ds)
        results.append((vid, ef, clean))
    return results, drops


def write_back(conn, results: list[tuple[str, dict, dict]], stats: ExtractStats) -> None:
    """闸 4:merge-only 写库(只补 null 键 + 打标)。"""
    from psycopg2.extras import execute_batch

    # 带时区的北京时间:裸本地时间串会被 PG 按 session 时区误读(曾导致审计时
    # 判"特征表落后于提取"的假象),统一 isoformat 带 +08:00
    from datetime import datetime, timedelta, timezone
    ts = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    rows = []
    for vid, ef, clean in results:
        merged = dict(ef) if isinstance(ef, dict) else {}
        for k, v in clean.items():
            if merged.get(k) is None:          # 只补缺,绝不覆盖
                merged[k] = v
                stats.note_fill(k)
        merged["llm_extracted_at"] = ts
        rows.append((json.dumps(merged, ensure_ascii=False), vid))
    cur = conn.cursor()
    execute_batch(cur, "UPDATE vehicles SET extra_fields = %s WHERE vehicle_id = %s", rows)
    conn.commit()
    cur.close()
    stats.updated_rows += len(rows)


def run(conn, llm, limit: int | None = None, batch_size: int = 20,
        concurrency: int = 3, dry_run: bool = False, use_fallback: bool = False) -> ExtractStats:
    stats = ExtractStats()
    sql = _CANDIDATE_FALLBACK_SQL if use_fallback else _CANDIDATE_SQL
    cur = conn.cursor()
    cur.execute(sql, (limit or 1_000_000,))
    rows = [(r[0], r[1], r[2] if isinstance(r[2], dict) else (json.loads(r[2]) if r[2] else {}))
            for r in cur.fetchall()]
    cur.close()
    stats.candidates = len(rows)
    if dry_run:
        missing = {"hand_count": 0, "mileage_km": 0, "import_type": 0, "任一": 0}
        for _, _, ef in rows:
            h = any(ef.get(k) is None for k in ("hand_count", "mileage_km", "import_type"))
            missing["任一"] += h
            for k in ("hand_count", "mileage_km", "import_type"):
                missing[k] += ef.get(k) is None
        print(f"[dry-run] 候选 {len(rows)} 台;关键字段缺失: {missing}")
        return stats

    batches = [rows[i:i + batch_size] for i in range(0, len(rows), batch_size)]
    t0 = time.time()
    done_batches = 0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futs = {pool.submit(_process_batch, llm, b): b for b in batches}
        for fut in as_completed(futs):
            b = futs[fut]
            try:
                results, drops = fut.result()
                stats.llm_ok_items += len(results)
                stats.drops.extend(drops)
                if results:
                    write_back(conn, results, stats)   # 每批落库,断点安全
            except Exception as e:                     # 网络/解析失败:本批跳过(未打标,下轮自愈)
                stats.llm_fail_batches += 1
                print(f"    批次失败({len(b)} 台): {type(e).__name__}: {str(e)[:100]}", flush=True)
            done_batches += 1
            if done_batches % 25 == 0:
                el = time.time() - t0
                print(f"    进度 {done_batches}/{len(batches)} 批,"
                      f"已补 {stats.updated_rows} 台,耗时 {el:.0f}s", flush=True)
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
    """给 service 每轮爬完调的程序入口。异常全包,失败不影响爬虫。

    尾部**无条件重算特征表**(约 7 秒,features.main 已做原子切换,重算期间
    搜索不断服):爬虫改价/新车上架/LLM 补字段都改 vehicles,而搜索 JOIN 的
    vehicle_features 是派生表 —— 没有这一步,前面所有更新的价值都到不了搜索
    (新车不可见、车况分陈旧)。这也是全链路唯一一处重算触发点,爬虫+提取
    两个数据源一次覆盖。远程 API 进程的词表缓存靠 5 分钟 TTL 自然过期,
    不需要(也无法)在这里 reset。
    """
    try:
        from carinfo.search.config import load_llm_config
        from carinfo.search.llm import LLMClient
        cfg = load_llm_config()
        cfg.disable_thinking = True
        cfg.timeout = 45.0
        llm = LLMClient(cfg)
        if llm.configured:
            conn = _connect()
            try:
                stats = run(conn, llm)
            finally:
                conn.close()
            msg = (f"LLM 提取:候选 {stats.candidates},补字段 {stats.filled},"
                   f"落库 {stats.updated_rows},失败批 {stats.llm_fail_batches}")
        else:
            msg = "未配置 MINIMAX_API_KEY,跳过 LLM 提取"

        # 特征表重算(无条件):失败只记 WARNING,不判整体失败
        from carinfo.search.features import main as recompute_features
        rc = recompute_features([])
        if rc != 0:
            return True, msg + f";⚠ 特征表重算返回码 {rc}(可用 python -m carinfo.search.features 手动重算)"
        return True, msg + ";特征表已重算"
    except Exception as e:
        return False, f"LLM 提取异常(不影响爬取): {type(e).__name__}: {e}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LLM 描述字段提取(merge-only)")
    ap.add_argument("--dry-run", action="store_true", help="只统计候选,不调模型不写库")
    ap.add_argument("--limit", type=int, default=None, help="最多处理多少台")
    ap.add_argument("--batch-size", type=int, default=20)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--fallback", action="store_true",
                    help="跑「关键字段全缺」的兜底候选(标记被爬虫覆盖的自愈通道)")
    args = ap.parse_args(argv)

    from carinfo.search.config import load_llm_config
    from carinfo.search.llm import LLMClient
    cfg = load_llm_config()
    cfg.disable_thinking = True
    cfg.timeout = 45.0
    llm = LLMClient(cfg)
    if not llm.configured:
        print("未配置 MINIMAX_API_KEY(.env),无法提取")
        return 1

    conn = _connect()
    try:
        stats = run(conn, llm, limit=args.limit, batch_size=args.batch_size,
                    concurrency=args.concurrency, dry_run=args.dry_run,
                    use_fallback=args.fallback)
    finally:
        conn.close()

    print(f"\n[完成] 候选 {stats.candidates} | LLM 成功 {stats.llm_ok_items} | "
          f"落库 {stats.updated_rows} | 失败批 {stats.llm_fail_batches}")
    print(f"    补字段分布: {stats.filled}")
    if stats.drops:
        from collections import Counter
        kinds = Counter(d.split(":")[1] if ":" in d else d for d in stats.drops)
        print(f"    闸口丢弃 {len(stats.drops)} 项(按类型): {dict(kinds)}")
        for d in stats.drops[:10]:
            print(f"      - {d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
