"""车型归一：把 car_model 的自由文本归一到稳定车系键（base_model）。

28car 的 car_model 是非标准化的自由文本（车系 + 排量 + 版本 + 配置词），实测同一
车系可有数百种写法（丰田 ALPHARD 一个车系 293 种 / 2269 台）。检索与行情基准都
需要一个稳定的车系键，本模块负责产出它：

- 主路径：用 model_vocabulary 的 1131 条 prefix 模式做「最长匹配」
  （实测命中 99.4% 的在售车；归一后满足「≥5 台样本」的车从 64.2% 提到 88.8%）
- 二次机会：整串匹配不上时按词元再匹配一次（'ZG ALPHARD 2.5' / '2016 TIGUAN 1.4 TSI'）
- 兜底：取第一个词元（28car 的排布是「车系 排量 版本」，首词元即车系名），
  只有 0.6%（121/21061）走到这里
- 品牌：car_brand 形如「豐田\\xa0TOYOTA」中英双写，取英文段；「任何 ANY」视为未知

已知遗留（不修，成本 > 收益）：兜底键里约 410 台是「卖家只填了数字」（如
car_model='628000'），无车系信息，只能各自成组、拿不到行情基准。

关于中文车名：customer 说「阿尔法」要能查到 ALPHARD，但这个映射【不在数据里】
（词表 0 条中文词条，description 里「阿爾法」仅 2 条）——它是 LLM 的世界知识，
主路径由 search/parser.py 负责。本模块的 MODEL_ALIASES 只是无 LLM 时的兜底。
"""

import re
from dataclasses import dataclass, field

# car_brand / car_model 里的不可见分隔符（28car 用 NBSP 分隔中英文）
_NBSP = "\xa0"
_WS_RE = re.compile(r"[\s\xa0]+")
_ASCII_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-\.]*")

# 这些品牌值代表「未提供品牌」，不能当品牌用（实测 664 台）
_UNKNOWN_BRAND_PREFIXES = ("任何", "其他", "OTHER", "UNKNOWN")

# 年份词元：'2016 TIGUAN 1.4 TSI' 这种把年份写进 car_model 的，兜底时要跳过
_YEAR_TOKEN_RE = re.compile(r"(?:19|20)\d{2}")
_COMPACT_RE = re.compile(r"[\s\-]+")
# 粘连排量：字母紧跟「数字.数字」（'ALPHARD2.5Z' / 'SPADE1.5' / 'NQ9.0T'）
_GLUED_VERSION_RE = re.compile(r"[A-Z]\d+\.\d+")


def clean_text(text: str | None) -> str:
    """统一大小写与空白：去 NBSP、压缩连续空白、转大写、去首尾。

    大小写统一是必须的——词表 pattern 是大写（'ALPHARD%'），而 car_model
    里大小写混用（'Alphard 3.5'），PG 的 LIKE 又区分大小写。
    """
    if not text:
        return ""
    return _WS_RE.sub(" ", text.replace(_NBSP, " ")).strip().upper()


def normalize_brand(car_brand: str | None) -> str | None:
    """归一品牌名：`豐田\\xa0TOYOTA` → `TOYOTA`；`任何\\xa0ANY` → None。

    291 条纯英文品牌（MAXUS / SMART / JEEP / INFINITI…）不是脏值，原样返回。
    """
    cleaned = clean_text(car_brand)
    if not cleaned:
        return None
    if cleaned.startswith(_UNKNOWN_BRAND_PREFIXES):
        return None
    words = _ASCII_WORD_RE.findall(cleaned)
    if words:
        return " ".join(words)
    return cleaned  # 纯中文品牌：保留原文，总比丢掉强


def _is_junk_base(base: str) -> bool:
    """判断词表条目是不是脏条目 —— 它们的 pattern 更长，会压过正确车系。两类：

    1. **单字符车系**：'F' / 'N' / 'T' / 'S'。pattern 'F%' 会把 Jaguar 的 F-PACE
       和 F-TYPE 吸成同一个假车系（实测 236 台车被 11 个单字符键吸走）。真实车系
       没有单字符的（BMW 是 M2/M3/M4，Audi 是 Q2..Q8，都不是裸 'M'/'Q'）。
    2. **车系+排量粘连**：'ALPHARD2.5Z' / 'VELLFIRE3.5' / 'SPADE1.5' /
       'SCLROCCO2.0TSI' / 'NQ9.0T'（实测 15 台 ALPHARD 被 'ALPHARD2.5*' 拆成 5 个键）。

    判据只用一条：名字里带「字母紧跟数字.数字」的排量。

    为什么规则 2 不能写成「有规范车系名做前缀 + 尾巴是纯数字」：车型名天然前缀
    嵌套，'S3' 是 'S320' 的前缀、'Z4' 是 'Z400' 的前缀、'S6' 是 'S60' 的前缀，
    那样会一次性误杀 S320/S500/S60/Z300/Z400/YAMAHA125 一整批合法车系
    （实测剔掉 60+ 条）。纯数字尾巴≠排量，小数点才是排量的可靠特征。

    不误伤：'ID.4'（'D' 后面是小数点不是数字）、'CX-5' / 'MODEL 3' / 'X5'（无小数点）、
    '3.0 CSL'（数字开头，前面没有字母）。
    """
    compact = _COMPACT_RE.sub("", base)
    if len(compact) < 2:
        return True
    return bool(_GLUED_VERSION_RE.search(compact))


@dataclass
class Vocabulary:
    """车型词表：按 pattern 前缀长度降序排列，命中即返回（长的更具体，优先）。

    两套索引是必须的，不是冗余：
    - 原文索引：pattern 'LAND CRUISER%' 匹配原文 'LAND CRUISER PRADO'
    - 压平索引：把空格/连字符去掉再比 —— 词表里存的是**连写**形式
      （'FTYPE' / 'TCROSS' / 'EPACE'），而 28car 原文常写成 'F TYPE COUPE' /
      'T CROSS 1.0T'。只做原文匹配会让这些车掉到兜底，键变成 'F' 这种烂值。
      压平后 'FTYPECOUPE'.startswith('FTYPE') 才成立。
    """

    # [(prefix, base_model, brand_norm), ...] 全量、已排序
    entries: list[tuple[str, str, str | None]] = field(default_factory=list)
    # 被剔掉的脏条目 [(base_model, prefix), ...]，仅用于离线验证时核对
    dropped: list[tuple[str, str]] = field(default_factory=list)
    # 按首字符分桶，避免每个 car_model 都扫全表（2 万行 × 1121 条 ≈ 2300 万次比较）
    _buckets: dict[str, list[tuple[str, str, str | None]]] = field(default_factory=dict)
    _compact_buckets: dict[str, list[tuple[str, str, str | None]]] = field(default_factory=dict)

    @classmethod
    def from_rows(cls, rows) -> "Vocabulary":
        """rows: [(base_model, brand, search_pattern), ...]（直接来自 model_vocabulary）。

        先剔脏条目，再按前缀长度降序排序 —— 顺序不能反：脏条目 pattern 更长，
        排在前面会直接压过正确的车系条目。
        """
        raw: list[tuple[str, str, str | None]] = []
        for base_model, brand, pattern in rows:
            prefix = clean_text(pattern).rstrip("%").strip()
            base = clean_text(base_model)
            if not prefix or not base:
                continue
            raw.append((prefix, base, normalize_brand(brand)))

        entries = [e for e in raw if not _is_junk_base(e[1])]
        dropped = [(e[1], e[0]) for e in raw if _is_junk_base(e[1])]

        # 长前缀优先：'MODEL 3' 应先于 'MODEL' 命中
        entries.sort(key=lambda e: len(e[0]), reverse=True)

        buckets: dict[str, list[tuple[str, str, str | None]]] = {}
        compact: dict[str, list[tuple[str, str, str | None]]] = {}
        for e in entries:
            buckets.setdefault(e[0][0], []).append(e)
            cp = _COMPACT_RE.sub("", e[0])
            if cp:
                compact.setdefault(cp[0], []).append(e)
        # 压平索引要单独按压平长度排序：'LANDCRUISER' 与 'LAND CRUISER' 长度不同
        for lst in compact.values():
            lst.sort(key=lambda e: len(_COMPACT_RE.sub("", e[0])), reverse=True)

        return cls(entries=entries, dropped=dropped, _buckets=buckets, _compact_buckets=compact)

    def match(self, car_model: str | None) -> tuple[str, str | None] | None:
        """原文最长前缀匹配，返回 (base_model, brand_norm)；未命中返回 None。"""
        text = clean_text(car_model)
        if not text:
            return None
        for prefix, base, brand in self._buckets.get(text[0]) or ():
            if text.startswith(prefix):
                return base, brand
        return None

    def match_compact(self, car_model: str | None) -> tuple[str, str | None] | None:
        """压平空格/连字符后再做最长前缀匹配。

        打通「词表连写 vs 原文分词」的差异：'F TYPE COUPE' → 'FTYPECOUPE'
        匹配 pattern 'FTYPE%' → 归到 FTYPE，而不是掉到兜底变成 'F'。
        """
        text = _COMPACT_RE.sub("", clean_text(car_model))
        if len(text) < 2:
            return None
        for prefix, base, brand in self._compact_buckets.get(text[0]) or ():
            if text.startswith(_COMPACT_RE.sub("", prefix)):
                return base, brand
        return None

    def __len__(self) -> int:
        return len(self.entries)


#: 归一路径标签（用于离线验证时看各级命中比例）
PATH_EXACT = "exact"        # 原文最长前缀匹配
PATH_COMPACT = "compact"    # 压平空格/连字符后匹配
PATH_TOKEN = "token"        # 整串失败，但某个词元命中词表
PATH_FALLBACK = "fallback"  # 走首词元兜底
PATHS = (PATH_EXACT, PATH_COMPACT, PATH_TOKEN, PATH_FALLBACK)


def normalize_model_ex(
    car_model: str | None, vocab: Vocabulary
) -> tuple[str | None, str | None, bool, str]:
    """归一单个 car_model，额外返回走了哪条路径。

    四级路径（前面的命中就不走后面）：
      1. exact    原文最长前缀 —— 常规情况
      2. compact  压平空格/连字符 —— 打通词表连写（FTYPE）与原文分词（F TYPE）
      3. token    逐词元匹配，取**最长**命中 —— 'ZG ALPHARD 2.5' 这类车系不在首位
      4. fallback 首词元兜底 —— 长尾车系，宁可粗一点也不能丢车

    Returns:
        (base_model, brand_hint, matched, path)
        - matched=False 表示走了 token/fallback（未真正命中词表完整条目）
    """
    text = clean_text(car_model)
    if not text:
        return None, None, False, PATH_FALLBACK

    hit = vocab.match(text)
    if hit is not None:
        return hit[0], hit[1], True, PATH_EXACT

    hit = vocab.match_compact(text)
    if hit is not None:
        return hit[0], hit[1], True, PATH_COMPACT

    # 二次机会：车系名不一定在第一个词元。实测 'ZG ALPHARD 2.5' / 'SC ALPHARD' /
    # '2016 TIGUAN 1.4 TSI' / '2018 Q7 45 TFSI QUATTRO' 这类整串匹配不上，但拆成
    # 词元就有命中。取**最长**命中（'SC ALPHARD' 里 'SC' 也可能命中，用长度保证
    # 选到真车系而不是版本装饰词）。单字符词元不试 —— 裸 'F'/'N'/'T' 不是车系。
    best: tuple[str, str | None] | None = None
    for token in text.split():
        if len(token) < 2 or token[0].isdigit():
            continue  # 单字符 / 纯数字 / 排量 / 年份都不可能起头命中车系
        token_hit = vocab.match(token) or vocab.match_compact(token)
        if token_hit is not None and (best is None or len(token_hit[0]) > len(best[0])):
            best = token_hit
    if best is not None:
        return best[0], best[1], True, PATH_TOKEN

    # 兜底：28car 的 car_model 排布是「车系 排量 版本」，首词元即车系名。
    # 长尾车系（词表 795 条 frequency<5）靠这里兜住，宁可粗一点也不能丢车。
    tokens = text.split()
    if len(tokens) > 1 and _YEAR_TOKEN_RE.fullmatch(tokens[0]):
        tokens = tokens[1:]  # '2016 TIGUAN 1.4 TSI' → 从 TIGUAN 起算
    if not tokens:
        return text, None, False, PATH_FALLBACK
    # 首词元只有 1 个字符时带上第二词元：'F PACE'≠'F TYPE'、'N BOX'≠'N ONE'、
    # 'T ROC'≠'T CROSS'，裸 'F' 会把它们混成一个假车系（实测 236 台）。
    # 数字首词元同理：'1 BRABUS' / '3 BRABUS' 是不同车，不该共用键 '1'。
    if len(tokens) > 1 and len(tokens[0]) == 1:
        return f"{tokens[0]} {tokens[1]}", None, False, PATH_FALLBACK
    return tokens[0], None, False, PATH_FALLBACK


def normalize_model(
    car_model: str | None, vocab: Vocabulary
) -> tuple[str | None, str | None, bool]:
    """归一单个 car_model（常用形态）。

    Returns:
        (base_model, brand_hint, matched)
        - base_model  归一后的车系键；car_model 为空时返回 None
        - brand_hint  词表里该车系所属品牌（可用于补全「任何 ANY」的 664 台）
        - matched     是否命中词表（False 表示走了首词元兜底）
    """
    base, brand, matched, _ = normalize_model_ex(car_model, vocab)
    return base, brand, matched


# ---------------------------------------------------------------------------
# 中文/粤语车名别名 —— 仅作无 LLM 时的兜底
#
# 主路径是 parser.py 里的 LLM（它的世界知识远比一张静态表全）。这里只放
# 高频且确信的写法，覆盖香港 / 内地 / 粤语三套叫法，避免模型不可用时整个
# 搜索失效。
#
# 两张表职责必须分开，不能有交集：
#   MODEL_ALIASES 只放【车系】→ 返回值填 base_model 位（"阿尔法" → ALPHARD）
#   BRAND_ALIASES 只放【品牌】→ 返回值填 brand 位（"平治" → MERCEDES-BENZ）
# 否则「平治」会先命中车系表，被当成车系名去等值匹配，一条都查不到。
# ---------------------------------------------------------------------------

MODEL_ALIASES: dict[str, str] = {
    # MPV：港/中/粤叫法差异最大，也是本业务的主力车型
    "阿尔法": "ALPHARD", "阿爾法": "ALPHARD", "埃尔法": "ALPHARD", "埃爾法": "ALPHARD",
    "阿法": "ALPHARD", "愛爾法": "ALPHARD",
    "威尔法": "VELLFIRE", "威爾法": "VELLFIRE", "威法": "VELLFIRE",
    "海狮": "HIACE", "海獅": "HIACE", "客货车": "HIACE", "客貨車": "HIACE",
    "诺亚": "NOAH", "諾亞": "NOAH",
    "奥德赛": "ODYSSEY", "奧德賽": "ODYSSEY",
    "塞纳": "SIENNA", "賽納": "SIENNA",
    "普瑞维亚": "PREVIA", "大霸王": "PREVIA",
    # 轿车 / SUV 常用中文名
    "佳美": "CAMRY",
    "卡罗拉": "COROLLA", "花冠": "COROLLA",
    "思域": "CIVIC", "雅阁": "ACCORD", "雅廓": "ACCORD",
    "天籁": "TEANA",
    "飞度": "FIT",
    "森林人": "FORESTER",
    "陆地巡洋舰": "LAND CRUISER", "陸地巡洋艦": "LAND CRUISER",
    "皇冠": "CROWN",
}

# 已刻意不收录（实测库内 0 台，映射了也是空结果，交给 LLM 去答"没货"）：
#   轩逸 SYLPHY / 普拉多 PRADO / 汉兰达 HIGHLANDER —— 内地专供名，香港没这车
# 注意 CROWN / CAMRY / COROLLA / CIVIC / PREVIA 虽然**词表里没有**条目，但库内
# 有车（走首词元兜底，键就是同名大写），所以别名依然有效，不要因为「词表没有」而删。

# 品牌别名（客户只说品牌时就够了，不需要落到车系）
BRAND_ALIASES: dict[str, str] = {
    "平治": "MERCEDES-BENZ", "奔驰": "MERCEDES-BENZ", "奔馳": "MERCEDES-BENZ",
    "寶馬": "BMW", "宝马": "BMW",
    "奧迪": "AUDI", "奥迪": "AUDI",
    "保時捷": "PORSCHE", "保时捷": "PORSCHE",
    "凌志": "LEXUS", "雷克薩斯": "LEXUS", "雷克萨斯": "LEXUS",
    "福士": "VOLKSWAGEN", "大眾": "VOLKSWAGEN", "大众": "VOLKSWAGEN",
    "萬事得": "MAZDA", "万事得": "MAZDA", "馬自達": "MAZDA", "马自达": "MAZDA",
    "富士": "SUBARU", "速霸陸": "SUBARU", "斯巴鲁": "SUBARU",
    "越野路華": "LAND ROVER", "越野路华": "LAND ROVER", "路虎": "LAND ROVER",
    "富豪": "VOLVO", "沃尔沃": "VOLVO",
    "積架": "JAGUAR", "捷豹": "JAGUAR",
    "豐田": "TOYOTA", "丰田": "TOYOTA",
    "日產": "NISSAN", "日产": "NISSAN",
    "鈴木": "SUZUKI", "铃木": "SUZUKI",
    "五十鈴": "ISUZU", "五十铃": "ISUZU",
    "現代": "HYUNDAI", "现代": "HYUNDAI",
    "起亞": "KIA", "起亚": "KIA",
    "標緻": "PEUGEOT", "标致": "PEUGEOT",
    "雪鐵龍": "CITROEN", "雪铁龙": "CITROEN",
    "雷諾": "RENAULT", "雷诺": "RENAULT",
    "快意": "FIAT", "菲亞特": "FIAT", "菲亚特": "FIAT",
    "大發": "DAIHATSU", "大发": "DAIHATSU",
    "愛快羅密歐": "ALFA ROMEO", "爱快罗密欧": "ALFA ROMEO",
    "瑪莎拉蒂": "MASERATI", "玛莎拉蒂": "MASERATI",
    "賓利": "BENTLEY", "宾利": "BENTLEY",
    "勞斯萊斯": "ROLLS-ROYCE", "劳斯莱斯": "ROLLS-ROYCE",
    "林寶堅尼": "LAMBORGHINI", "兰博基尼": "LAMBORGHINI",
    "比亞迪": "BYD", "比亚迪": "BYD",
    "小鵬": "XPENG", "小鹏": "XPENG",
    "蔚來": "NIO", "蔚来": "NIO",
    "特斯拉": "TESLA", "本田": "HONDA", "三菱": "MITSUBISHI",
    "迷你": "MINI", "吉普": "JEEP", "法拉利": "FERRARI",
}


def resolve_alias(user_text: str | None) -> tuple[str | None, str | None]:
    """把客户输入的中文/粤语车名映射成 (base_model, brand)。

    仅作兜底：parser.py 的 LLM 是主路径。都查不到时返回 (None, None)。
    """
    cleaned = clean_text(user_text)
    if not cleaned:
        return None, None
    if cleaned in MODEL_ALIASES:
        return MODEL_ALIASES[cleaned], None
    if cleaned in BRAND_ALIASES:
        return None, BRAND_ALIASES[cleaned]
    return None, None
